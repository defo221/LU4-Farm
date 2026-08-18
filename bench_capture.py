"""Compare capture backends and colour paths for stream_sender's frame pipeline.

    python bench_capture.py                # run every variant
    python bench_capture.py dxcam-bgr      # run one

Each variant runs in its own process: dxcam refuses a second camera for the same
output, and releasing one crashes comtypes on teardown, so reusing a process
would either fail or poison the next measurement.

Reports both numbers that matter, per frame:
  wall  - sets the FPS ceiling (how long the capture loop is busy or blocked)
  cpu   - what competes with the bot agents for cores (kernel + user, all threads)

Frames are paced so the duplicator always has a fresh frame ready, otherwise
dxcam deduplicates and the averages become meaningless.
"""

import ctypes
import statistics
import subprocess
import sys
import time
from ctypes import wintypes

import cv2
import numpy as np

try:
    import dxcam
except Exception:
    dxcam = None
try:
    import mss
except Exception:
    mss = None

SCALE = 0.34            # grid tile: 1920x1080 -> ~653x367
QUALITY = 60
PACE = 1 / 15.0
N = 60

VARIANTS = ["dxcam-bgr", "dxcam-bgra-resize-first", "mss", "threads"]


def setup():
    try:
        ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4)
    except Exception:
        pass
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def cpu_seconds():
    """Total CPU (kernel + user) charged to this process, all threads.

    process_time() would do, but it is quantised to the 15.6 ms scheduler tick
    on Windows, which is coarser than a single frame.
    """
    if sys.platform != "win32":
        return time.process_time()
    k = ctypes.windll.kernel32
    k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.GetProcessTimes.restype = wintypes.BOOL
    k.GetCurrentProcess.restype = ctypes.c_void_p
    c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
    if not k.GetProcessTimes(k.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                             ctypes.byref(kt), ctypes.byref(ut)):
        return time.process_time()
    def secs(f):
        return ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7
    return secs(kt) + secs(ut)


def measure(label, build, n=N, pace=PACE):
    for _ in range(5):                      # warm caches and thread pools
        build()
    walls = []
    got = 0
    c0 = cpu_seconds()
    w0 = time.perf_counter()
    for _ in range(n):
        t0 = time.perf_counter()
        if build() is not None:
            got += 1
        walls.append((time.perf_counter() - t0) * 1000)
        rest = pace - (time.perf_counter() - t0)
        if rest > 0:
            time.sleep(rest)
    cpu = (cpu_seconds() - c0) / n * 1000
    span = time.perf_counter() - w0
    print("  %-30s frames %2d/%2d  wall %6.2f ms  cpu %6.2f ms  -> ceiling %4.0f fps"
          % (label, got, n, statistics.median(walls), cpu,
             1000.0 / max(statistics.median(walls), 0.01)))
    return span


def encode(img):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), QUALITY])
    return buf if ok else None


def run_dxcam_bgr():
    """dxcam converts BGRA->BGR itself (processor_backend defaults to cv2)."""
    cam = dxcam.create(output_idx=0, output_color="BGR")
    w, h = cam.width, cam.height
    dw, dh = int(w * SCALE), int(h * SCALE)
    print("dxcam output_color=BGR  %dx%d" % (w, h))

    measure("grab only", lambda: cam.grab())

    def grid():
        f = cam.grab()
        if f is None:
            return None
        return encode(cv2.resize(f, (dw, dh), interpolation=cv2.INTER_AREA))

    def full():
        f = cam.grab()
        if f is None:
            return None
        return encode(f)

    measure("grid  (resize %.2f + encode)" % SCALE, grid)
    measure("zoom  (encode full)", full)


def run_dxcam_bgra_resize_first():
    """Keep the alpha channel and downscale before converting.

    Converting after the resize touches ~9x fewer pixels at SCALE=0.34, so if
    the colour conversion is a real cost this variant should win in grid mode.
    """
    cam = dxcam.create(output_idx=0, output_color="BGRA")
    w, h = cam.width, cam.height
    dw, dh = int(w * SCALE), int(h * SCALE)
    print("dxcam output_color=BGRA  %dx%d" % (w, h))

    measure("grab only (no convert)", lambda: cam.grab())

    def grid():
        f = cam.grab()
        if f is None:
            return None
        small = cv2.resize(f, (dw, dh), interpolation=cv2.INTER_AREA)
        return encode(cv2.cvtColor(small, cv2.COLOR_BGRA2BGR))

    def full():
        f = cam.grab()
        if f is None:
            return None
        return encode(cv2.cvtColor(f, cv2.COLOR_BGRA2BGR))

    measure("grid  (resize4ch + cvt + enc)", grid)
    measure("zoom  (cvt + encode full)", full)


def run_mss():
    s = mss.MSS()
    mon = s.monitors[1]
    w, h = mon["width"], mon["height"]
    dw, dh = int(w * SCALE), int(h * SCALE)
    box = {"left": mon["left"], "top": mon["top"], "width": w, "height": h}
    print("mss GDI BitBlt  %dx%d" % (w, h))

    measure("grab only (+cvt)",
            lambda: cv2.cvtColor(np.asarray(s.grab(box)), cv2.COLOR_BGRA2BGR))

    def grid():
        img = cv2.cvtColor(np.asarray(s.grab(box)), cv2.COLOR_BGRA2BGR)
        return encode(cv2.resize(img, (dw, dh), interpolation=cv2.INTER_AREA))

    def full():
        return encode(cv2.cvtColor(np.asarray(s.grab(box)), cv2.COLOR_BGRA2BGR))

    measure("grid  (cvt + resize + encode)", grid)
    measure("zoom  (cvt + encode full)", full)


def run_threads():
    """Sweep OpenCV's thread count for the chosen dxcam pipeline.

    resize and imencode fan out across every core by default. On a slave that is
    also running the game and a bot agent, spending 16 threads to save a few ms
    of wall time is a bad trade - this shows where the knee is.
    """
    cam = dxcam.create(output_idx=0, output_color="BGR")
    w, h = cam.width, cam.height
    dw, dh = int(w * SCALE), int(h * SCALE)
    print("dxcam BGR %dx%d, sweeping cv2 thread count" % (w, h))

    def grid():
        f = cam.grab()
        if f is None:
            return None
        return encode(cv2.resize(f, (dw, dh), interpolation=cv2.INTER_AREA))

    def full():
        f = cam.grab()
        if f is None:
            return None
        return encode(f)

    for n in (1, 2, 4, 8, 0):
        cv2.setNumThreads(n if n else cv2.getNumberOfCPUs())
        tag = "threads=%d" % (n if n else cv2.getNumberOfCPUs())
        measure("%-11s grid" % tag, grid, n=40)
        measure("%-11s zoom" % tag, full, n=40)


def main():
    if len(sys.argv) > 1:
        setup()
        name = sys.argv[1]
        if name == "dxcam-bgr":
            run_dxcam_bgr()
        elif name == "dxcam-bgra-resize-first":
            run_dxcam_bgra_resize_first()
        elif name == "mss":
            run_mss()
        elif name == "threads":
            run_threads()
        else:
            print("unknown variant:", name)
        return

    print("cv2 threads: %d\n" % cv2.getNumThreads())
    for v in VARIANTS:
        if v.startswith("dxcam") and dxcam is None:
            print("%s: dxcam not installed\n" % v)
            continue
        if v == "mss" and mss is None:
            print("mss not installed\n")
            continue
        subprocess.run([sys.executable, __file__, v])
        print()


if __name__ == "__main__":
    main()
