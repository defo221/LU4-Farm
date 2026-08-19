"""
test_dual_dot_overlay.py  -  transparent fullscreen overlay with red dot between
                              detected in_target_blue pairs.

A magenta (#ff00ff) background is made fully transparent by Windows so only the
drawn elements are visible on top of the game.  The detection runs in a background
thread; the overlay updates via tkinter's after() loop.

Press Esc to quit.

Usage:
    python test_dual_dot_overlay.py [--conf 0.75]
                                    [--tmpl path/to/in_target_blue.png]
                                    [--hz 20]   capture/redraw rate (default 20)
"""
import argparse
import os
import sys
import threading
import time
import tkinter as tk

import cv2
import numpy as np
import mss

DEFAULT_TMPL = os.path.join(os.path.dirname(__file__), "assets", "in_target_blue.png")
DEFAULT_CONF = 0.75
DEFAULT_HZ   = 20

# Colour that Windows treats as "transparent" in the overlay window.
# Must not appear in any drawn element.
CHROMA_KEY   = "#ff00ff"

MAX_PAIR_DY  = 20
MIN_PAIR_DX  = 30


# ── detection helpers ─────────────────────────────────────────────────────────
def nms_points(pts, min_dist):
    result = []
    for p in pts:
        if all(abs(p[0]-r[0]) > min_dist or abs(p[1]-r[1]) > min_dist for r in result):
            result.append(p)
    return result


def find_dots(frame, tmpl, conf, nms_dist):
    th, tw = tmpl.shape[:2]
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= conf)
    raw = [(int(x + tw//2), int(y + th//2)) for x, y in zip(xs, ys)]
    return nms_points(raw, nms_dist)


def pair_dots(dots):
    used, pairs = set(), []
    for i, a in enumerate(dots):
        if i in used:
            continue
        for j, b in enumerate(dots):
            if j <= i or j in used:
                continue
            if abs(a[1]-b[1]) <= MAX_PAIR_DY and abs(a[0]-b[0]) >= MIN_PAIR_DX:
                left  = a if a[0] < b[0] else b
                right = b if a[0] < b[0] else a
                pairs.append((left, right))
                used.add(i); used.add(j)
                break
    return pairs


# ── capture thread ─────────────────────────────────────────────────────────────
class Detector:
    def __init__(self, tmpl, conf, nms_dist, hz):
        self.tmpl     = tmpl
        self.conf     = conf
        self.nms_dist = nms_dist
        self.interval = 1.0 / hz
        self.lock     = threading.Lock()
        self._dots    = []
        self._pairs   = []
        self._fps     = 0.0
        self._stop    = threading.Event()

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self):
        self._stop.set()

    def get(self):
        with self.lock:
            return list(self._dots), list(self._pairs), self._fps

    def _run(self):
        with mss.mss() as sct:
            mon = sct.monitors[1]
            while not self._stop.is_set():
                t0    = time.perf_counter()
                raw   = sct.grab(mon)
                frame = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
                dots  = find_dots(frame, self.tmpl, self.conf, self.nms_dist)
                pairs = pair_dots(dots)
                dt    = time.perf_counter() - t0
                with self.lock:
                    self._dots  = dots
                    self._pairs = pairs
                    self._fps   = 1.0 / dt if dt > 0 else 0
                sleep = self.interval - dt
                if sleep > 0:
                    time.sleep(sleep)


# ── overlay window ─────────────────────────────────────────────────────────────
class Overlay:
    DOT_R    = 7     # red dot radius
    LINE_CLR = "#ffa500"
    DOT_CLR  = "#ff0000"
    RING_CLR = "#ffffff"
    BOX_CLR  = "#00dd00"
    TXT_CLR  = "#ff4444"

    def __init__(self, root, detector, tmpl_wh, interval_ms):
        self.root       = root
        self.detector   = detector
        self.tw, self.th = tmpl_wh
        self.interval   = interval_ms

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()

        root.geometry(f"{sw}x{sh}+0+0")
        root.overrideredirect(True)           # no title bar / borders
        root.attributes("-topmost",         True)
        root.attributes("-transparentcolor", CHROMA_KEY)
        root.configure(bg=CHROMA_KEY)
        root.bind("<Escape>", lambda e: self._quit())

        self.canvas = tk.Canvas(root, bg=CHROMA_KEY, highlightthickness=0,
                                width=sw, height=sh)
        self.canvas.pack()

        self._redraw()

    def _quit(self):
        self.detector.stop()
        self.root.destroy()

    def _redraw(self):
        c = self.canvas
        c.delete("all")

        dots, pairs, fps = self.detector.get()

        # Green boxes around each dot
        for cx, cy in dots:
            c.create_rectangle(cx - self.tw//2, cy - self.th//2,
                               cx + self.tw//2, cy + self.th//2,
                               outline=self.BOX_CLR, width=1)

        # Orange line + red dot + white ring for each pair
        for left, right in pairs:
            mid = ((left[0]+right[0])//2, (left[1]+right[1])//2)
            mx, my = mid

            c.create_line(left[0], left[1], right[0], right[1],
                          fill=self.LINE_CLR, width=1)
            r = self.DOT_R
            c.create_oval(mx-r, my-r, mx+r, my+r,
                          fill=self.DOT_CLR, outline=self.RING_CLR, width=1)
            c.create_text(mx+r+4, my, anchor="w", text=str(mid),
                          fill=self.TXT_CLR, font=("Consolas", 9))

        # FPS counter (top-left, always visible)
        c.create_text(8, 8, anchor="nw",
                      text=f"{fps:.0f} fps  dots:{len(dots)}  pairs:{len(pairs)}",
                      fill="#cccccc", font=("Consolas", 10))

        self.root.after(self.interval, self._redraw)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmpl", default=DEFAULT_TMPL)
    ap.add_argument("--conf", type=float, default=DEFAULT_CONF)
    ap.add_argument("--hz",   type=int,   default=DEFAULT_HZ)
    args = ap.parse_args()

    tmpl = cv2.imread(args.tmpl)
    if tmpl is None:
        print(f"ERROR: template not found: {args.tmpl}", file=sys.stderr)
        sys.exit(1)
    th, tw   = tmpl.shape[:2]
    nms_dist = max(tw, th) // 2

    print(f"Template : {args.tmpl}  {tw}x{th}  conf={args.conf}  hz={args.hz}")
    print("Overlay  : Esc to quit\n")

    detector = Detector(tmpl, args.conf, nms_dist, args.hz)
    detector.start()

    root = tk.Tk()
    Overlay(root, detector, (tw, th), interval_ms=1000 // args.hz)
    root.mainloop()


if __name__ == "__main__":
    main()
