"""
stream_sender.py - runs on each slave PC.

Two loops share one TCP connection to the main PC's viewer:

  1. capture loop    screen grab -> optional downscale -> JPEG -> push to viewer
  2. command loop    receive high-level JSON -> translate to Arduino serial commands

STRICT RULE: every click, cursor move, scroll and keypress is produced by the
Arduino USB HID device.  This process never injects input.  It only *reads*
state that it is not allowed to write:

  * dxcam / mss screen grabs               (reading pixels)
  * GetCursorPos                           (reading where the cursor is, so the
                                            Arduino's relative move can be aimed)
  * SetProcessDpiAwareness                 (so those reads are in real pixels)

There is deliberately no pyautogui / pynput / SendInput / AHK anywhere, and no
fallback path that injects input if the Arduino is missing - if the Arduino is
not connected, input commands are refused and reported to the viewer.

Usage (one .bat per slave, see run_stream_sender.bat):
    python stream_sender.py --name PC1 --port 8772 --arduino COM12
"""

import argparse
import ctypes
import socket
import sys
import threading
import time

import cv2
import numpy as np

try:
    import dxcam                                           # DXGI fast path
except Exception:
    dxcam = None

try:
    import mss                                             # GDI fallback
except Exception:
    mss = None

import stream_proto as proto
from stream_proto import Channel

try:
    import logger
except Exception:                                          # standalone deploy
    class logger:                                          # noqa: N801
        @staticmethod
        def _p(level, msg):
            print(f"[{time.strftime('%H:%M:%S')}] {level} | {msg}", flush=True)
        info = staticmethod(lambda m: logger._p("INFO", m))
        warn = staticmethod(lambda m: logger._p("WARN", m))
        error = staticmethod(lambda m: logger._p("ERROR", m))

try:
    import serial
except Exception:
    serial = None


# ── defaults ──────────────────────────────────────────────────────────────────
# Fleet infrastructure is grouped in the 877x range: 8770 is the PXM_RB
# coordinator API, so the streamers take 8772. Avoids 5000, which any Flask or
# Node dev server on a slave would grab first.
DEFAULT_PORT = 8772
DEFAULT_BAUD = 9600

IDLE_FPS = 1.0                 # tiles nobody is looking at
ACTIVE_FPS = 12.0              # tile currently being interacted with
DEFAULT_SCALE = 0.34           # 1920x1080 -> ~653x367, fits a 3x3 grid tile
DEFAULT_QUALITY = 60

# Frame pacing. Spreading each frame over a slice of its interval keeps the
# average bitrate identical while removing the line-rate burst that overruns
# small switch buffers. See Channel.send_frame_paced.
PACE_FRACTION = 0.8            # of the frame interval
PACE_MAX_S = 0.05              # hard cap, so idle tiles stay responsive

REMOTE_STEP_SIZE = 40          # px per HID report; 10 is far too slow for panning
MOVE_SETTLE_TIMEOUT = 0.40     # s to wait for the cursor to reach the target
MOVE_TOLERANCE = 2             # px; closer than this counts as arrived
MOVE_CORRECTIONS = 3           # extra MOVE passes allowed to close the gap

WATCHDOG_SILENCE = 8.0         # s without any viewer message -> release + drop

VALID_KEYS = set(
    "abcdefghijklmnopqrstuvwxyz0123456789"
    "`-=[]\\;',./~!@#$%^&*()_+{}|:\"<>? "
) | {
    "enter", "return", "esc", "escape", "tab", "backspace", "delete", "del",
    "insert", "ins", "home", "end", "pageup", "pgup", "pagedown", "pgdn",
    "up", "down", "left", "right", "space",
    "ctrl", "lctrl", "rctrl", "alt", "lalt", "ralt",
    "shift", "lshift", "rshift", "gui", "win", "lwin", "rwin",
} | {f"f{i}" for i in range(1, 13)}

VALID_BUTTONS = {"left", "right", "middle"}


# ── reading (never writing) host input state ──────────────────────────────────
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_user32 = ctypes.windll.user32 if sys.platform == "win32" else None


def cursor_pos():
    """Read the OS cursor position. A read, never a write."""
    if _user32 is None:
        return 0, 0
    p = _POINT()
    if _user32.GetCursorPos(ctypes.byref(p)):
        return int(p.x), int(p.y)
    return 0, 0


def keyboard_layout():
    """Current keyboard layout of the foreground window as a 2-letter ISO tag.

    Uses GetForegroundWindow → GetWindowThreadProcessId → GetKeyboardLayout to
    read the layout that belongs to the game window's thread, not the sender's
    own thread.  GetKeyboardLayoutNameW would only reflect the sender thread's
    layout and stays 'EN' even after the game switches to 'RU'.
    """
    if sys.platform != "win32" or _user32 is None:
        return ""
    try:
        u32 = _user32
        u32.GetForegroundWindow.restype        = ctypes.c_void_p
        u32.GetWindowThreadProcessId.restype   = ctypes.c_uint
        u32.GetKeyboardLayout.restype          = ctypes.c_void_p
        hwnd = u32.GetForegroundWindow()
        tid  = u32.GetWindowThreadProcessId(hwnd, None)
        hkl  = u32.GetKeyboardLayout(tid)      # HKL handle for that thread
        lcid = (hkl or 0) & 0xFFFF            # low WORD is the locale/language ID
        lang_buf = ctypes.create_unicode_buffer(16)
        # LOCALE_SISO639LANGNAME (0x0059) → "en", "ru", "de", …
        ctypes.windll.kernel32.GetLocaleInfoW(lcid, 0x0059, lang_buf, 16)
        return lang_buf.value.upper()          # → "EN", "RU", "DE", …
    except Exception:
        return ""


def raise_timer_resolution():
    """Ask Windows for 1 ms timer granularity.

    The default is about 15.6 ms, which is coarser than the gaps frame pacing
    sleeps for - without this every pace sleep would overshoot and the frame
    would go out as a burst anyway. Reading/setting our own process timing is
    not input injection.
    """
    if sys.platform != "win32":
        return False
    try:
        return ctypes.windll.winmm.timeBeginPeriod(1) == 0
    except Exception:
        return False


def make_dpi_aware():
    """Per-monitor DPI awareness so grabs and cursor reads are in real pixels."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4)
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


def pointer_precision_warning():
    """'Enhance pointer precision' breaks relative aiming. Report it, don't change it."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse") as k:
            val, _ = winreg.QueryValueEx(k, "MouseSpeed")
        if str(val).strip() != "0":
            return ("'Enhance pointer precision' is ON (MouseSpeed="
                    f"{val}). Relative Arduino moves will land short or long. "
                    "Turn it off in Mouse settings > Pointer Options.")
    except Exception:
        return None
    return None


# ── Arduino transport ─────────────────────────────────────────────────────────
class HidLink:
    """Serial link to the Arduino, with press/release state tracking.

    Separate from the bot's arduino_hid.ArduinoHID on purpose: this one is
    interactive (needs MOUSE_DOWN/UP, KEY_DOWN/UP), must never block on the
    bot's CapsLock pause logic, and must not pull in pyautogui.
    """

    def __init__(self, port, baud=DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self._ser = None
        self._lock = threading.Lock()
        self._held_buttons = set()
        self._held_keys = set()
        # False when the board runs firmware predating the press/release commands.
        self.firmware_ok = False

    def connect(self):
        if serial is None:
            logger.error("[HID] pyserial not installed - no input possible")
            return False
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.3)
            time.sleep(2.0)                       # bootloader + USB HID enumeration
            self._ser.reset_input_buffer()
        except Exception as e:
            logger.error(f"[HID] open {self.port} failed: {e}")
            self._ser = None
            return False
        # Old firmware has no PING and no STEP_SIZE: unknown commands fall off the
        # end of its if-chain without replying, so a silent no-answer here means
        # press/release and RELEASE_ALL are missing too. Detect it now rather than
        # letting pan and keystrokes quietly do nothing.
        ping = self._ask("PING")
        step = self._ask(f"STEP_SIZE,{REMOTE_STEP_SIZE}")
        self.firmware_ok = (ping == "1" and step == str(REMOTE_STEP_SIZE))
        if self.firmware_ok:
            logger.info(f"[HID] connected {self.port}, step size {REMOTE_STEP_SIZE}px")
        else:
            logger.warn(f"[HID] {self.port} answered PING={ping!r} STEP_SIZE={step!r} - "
                        f"this board needs the updated mouse.ino. Clicks and scroll "
                        f"will work; drag-pan and keystrokes will not.")
        return True

    @property
    def connected(self):
        return self._ser is not None

    def close(self):
        self.release_all()
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # ---- raw ----
    def _write(self, line):
        if self._ser is None:
            return False
        try:
            self._ser.write((line + "\n").encode())
            self._ser.flush()
            return True
        except Exception as e:
            logger.error(f"[HID] write failed: {e}")
            self._ser = None
            return False

    def _ask(self, line):
        """Send a command that replies with one line, and return that line."""
        with self._lock:
            if self._ser is None:
                return ""
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass
            if not self._write(line):
                return ""
            try:
                return self._ser.readline().decode(errors="ignore").strip()
            except Exception:
                return ""

    def _tell(self, line):
        """Send a command that produces no reply."""
        with self._lock:
            return self._write(line)

    # ---- movement ----
    def move_abs(self, x, y):
        """Aim the cursor at absolute (x, y).

        The Arduino can only move relatively, so we read the current position,
        send the delta, then verify.  Verification matters here: pointer
        acceleration, screen-edge clamping and rounding all make a single
        blind delta land in the wrong place, and a remote click that misses by
        20 px is worse than a slow one.
        """
        if self._ser is None:
            return False
        for _ in range(1 + MOVE_CORRECTIONS):
            cx, cy = cursor_pos()
            dx, dy = int(x) - cx, int(y) - cy
            if abs(dx) <= MOVE_TOLERANCE and abs(dy) <= MOVE_TOLERANCE:
                return True
            self._tell(f"MOVE,{dx},{dy}")
            self._await_settle(x, y, max(abs(dx), abs(dy)))
        cx, cy = cursor_pos()
        if abs(int(x) - cx) > MOVE_TOLERANCE or abs(int(y) - cy) > MOVE_TOLERANCE:
            logger.warn(f"[HID] aim missed: wanted ({x},{y}) got ({cx},{cy})")
        return True

    def _await_settle(self, x, y, distance):
        """Poll the cursor until it reaches the target or stops moving."""
        # Each step is one ~1 ms USB frame plus the firmware's 200 us pause.
        expected = (distance / max(REMOTE_STEP_SIZE, 1)) * 0.0013 + 0.008
        deadline = time.perf_counter() + min(expected * 3 + 0.05, MOVE_SETTLE_TIMEOUT)
        last = None
        still = 0
        while time.perf_counter() < deadline:
            pos = cursor_pos()
            if abs(pos[0] - int(x)) <= MOVE_TOLERANCE and \
               abs(pos[1] - int(y)) <= MOVE_TOLERANCE:
                return
            if pos == last:
                still += 1
                if still >= 3:                    # cursor parked: clamped or done
                    return
            else:
                still = 0
                last = pos
            time.sleep(0.002)

    def move_rel(self, dx, dy):
        if dx == 0 and dy == 0:
            return True
        ok = self._tell(f"MOVE,{int(dx)},{int(dy)}")
        steps = max(abs(int(dx)), abs(int(dy))) / max(REMOTE_STEP_SIZE, 1)
        time.sleep(steps * 0.0013 + 0.004)
        return ok

    # ---- buttons ----
    def click(self, btn="left", hold_min=25, hold_max=55):
        if btn == "left":
            return self._ask(f"CLICK_LEFT_HOLD,{hold_min},{hold_max}") != ""
        if btn == "right":
            return self._ask(f"CLICK_RIGHT_HOLD,{hold_min},{hold_max}") != ""
        return self._tell("CLICK_MIDDLE")

    def button_down(self, btn):
        if self._ask(f"MOUSE_DOWN,{btn}") == "1":
            self._held_buttons.add(btn)
            return True
        return False

    def button_up(self, btn):
        ok = self._ask(f"MOUSE_UP,{btn}") == "1"
        self._held_buttons.discard(btn)
        return ok

    def drag(self, dx, dy, btn="right"):
        """One atomic press-move-release, for gestures batched by the viewer."""
        if btn == "right":
            return self._tell(f"DRAG_RIGHT,{int(dx)},{int(dy)}")
        self.button_down(btn)
        self.move_rel(dx, dy)
        return self.button_up(btn)

    def scroll(self, steps):
        return self._tell(f"SCROLL,{int(steps)}")

    # ---- keyboard ----
    def key_down(self, key):
        if self._ask(f"KEY_DOWN,{key}") == "1":
            self._held_keys.add(key)
            return True
        return False

    def key_up(self, key):
        ok = self._ask(f"KEY_UP,{key}") == "1"
        self._held_keys.discard(key)
        return ok

    def key_tap(self, key, hold_ms=45):
        return self._tell(f"KEY,{key},{int(hold_ms)}")

    def key_combo(self, keys, hold_ms=45):
        return self._tell(f"KEY_COMBO,{int(hold_ms)}," + ",".join(keys))

    def release_all(self):
        if self._ser is None:
            return False
        held = bool(self._held_buttons or self._held_keys)
        self._held_buttons.clear()
        self._held_keys.clear()
        ok = self._ask("RELEASE_ALL") == "1"
        if held:
            logger.info("[HID] released everything that was still held")
        return ok

    @property
    def anything_held(self):
        return bool(self._held_buttons or self._held_keys)


# ── screen capture ────────────────────────────────────────────────────────────
# Two read-only backends. DXGI Desktop Duplication (dxcam) maps the surface the
# compositor has already produced, so a grab costs ~1 ms; mss goes through a GDI
# BitBlt readback and costs ~20 ms on the same machine. mss stays as a fallback
# because dxcam needs a real DXGI output: it is unavailable over RDP, on some
# virtualised GPUs, and on a slave where nobody has pip-installed it yet.
# Falling back is fine for *reading* pixels - input has no fallback and is
# Arduino-only by design.

CAPTURE_STALE_S = 2.0          # re-encode this often even if nothing moved

# OpenCV fans resize and imencode out across every core by default. Measured on
# a 2560x1440 slave (bench_capture.py, 120 frames/cell): encoding a full-size
# frame is just as fast on one thread as on sixteen (13.2 ms vs 13.0 ms wall)
# because JPEG entropy coding barely parallelises, but it costs half the CPU
# (13.8 ms vs 25.3 ms). Downscaled grid frames do get faster with more threads,
# yet an idle tile at 1 fps has no use for the wall time. Cores handed back here
# go to the game and the bot agent instead.
CV_THREADS = 2

# DXGI duplication really does break at runtime - a resolution change, a driver
# reset or a game taking exclusive fullscreen all invalidate it. After this many
# consecutive errors the slave gives up and finishes the session on mss, which
# the viewer shows as SLOW CAPTURE. Serving the last cached frame forever would
# leave a frozen tile that still claims to be online.
DX_FAIL_LIMIT = 5

_dx_camera = None
_dx_broken = False
_dx_fails = 0
_dx_last = None                # newest full-screen BGR array, outlives sessions


def force_backend(name):
    """Pin the capture backend, for diagnosing one slave (--capture)."""
    global _dx_broken
    if name == "mss":
        _dx_broken = True


def dx_camera():
    """Create the process-wide dxcam camera once, or None if unavailable.

    dxcam refuses a second camera for the same output, and Sessions come and go
    as viewers reconnect, so the camera has to outlive any single Session.
    """
    global _dx_camera, _dx_broken
    if _dx_camera is not None or _dx_broken:
        return _dx_camera
    if dxcam is None:
        _dx_broken = True
        return None
    try:
        # BGR straight from the duplicator: imencode wants BGR anyway, so this
        # removes the BGRA->BGR conversion that mss needs.
        _dx_camera = dxcam.create(output_idx=0, output_color="BGR")
    except Exception as e:
        logger.warn(f"[CAP] dxcam init failed: {e}")
        _dx_camera = None
    if _dx_camera is None:
        _dx_broken = True
    return _dx_camera


def dx_grab():
    """(full_screen_bgr, fresh) from the duplicator.

    fresh is False when DXGI had no new frame and this is the cached one. The
    cache is module-level so a reconnecting viewer gets pixels immediately
    instead of waiting for something on screen to move.
    """
    global _dx_last, _dx_fails
    cam = dx_camera()
    if cam is None:
        return None, False
    try:
        f = cam.grab()
    except Exception as e:
        _dx_fails += 1
        logger.warn(f"[CAP] dxcam grab failed ({_dx_fails}): {e}")
        # Skip this frame rather than re-serving the cache: one missed frame is
        # invisible at 12 fps, a permanently stale one is not.
        return None, False
    _dx_fails = 0
    if f is None:
        return _dx_last, False
    _dx_last = f
    return f, True


def dx_dead():
    return _dx_fails >= DX_FAIL_LIMIT


class Capturer:
    """Grabs one region, scales it and JPEG-encodes it. Single-threaded.

    grab() returns None when there is nothing worth sending: the screen has not
    changed since the viewer's last frame, or encoding failed. The capture loop
    already treats None as "skip this tick".
    """

    def __init__(self):
        self.scale = DEFAULT_SCALE
        self.quality = DEFAULT_QUALITY
        self.unchanged = 0         # grabs skipped because nothing moved

        self._sct = None
        cam = dx_camera()
        if cam is not None:
            self.backend = "dxcam"
            self.screen = (0, 0, int(cam.width), int(cam.height))
        elif mss is not None:
            # mss.mss() is deprecated in mss 10 but MSS is missing from old ones.
            self._sct = (getattr(mss, "MSS", None) or mss.mss)()
            mon = self._sct.monitors[1]           # [0] is the union of all screens
            self.backend = "mss"
            self.screen = (mon["left"], mon["top"], mon["width"], mon["height"])
        else:
            raise RuntimeError("no capture backend - pip install dxcam")
        self.region = self.screen
        logger.info(f"[CAP] {self.backend} {self.screen[2]}x{self.screen[3]}")

        self._sent_key = None
        self._sent_at = 0.0

    def set_region(self, x, y, w, h):
        if w <= 0 or h <= 0:
            self.region = self.screen
        else:
            self.region = (int(x), int(y), int(w), int(h))

    def _fall_back_to_mss(self):
        """Finish the session on GDI after DXGI has given up."""
        if mss is None:
            logger.error("[CAP] dxcam died and mss is not installed - no capture")
            return False
        self._sct = (getattr(mss, "MSS", None) or mss.mss)()
        self.backend = "mss"
        logger.warn("[CAP] dxcam failed repeatedly - switched to mss for this session")
        return True

    def grab(self):
        """((rx, ry, rw, rh), jpeg_bytes), or None if there is nothing to send."""
        if self._sct is None:
            full, fresh = dx_grab()
            if full is None:
                if dx_dead() and not self._fall_back_to_mss():
                    raise RuntimeError("capture backend lost")
                return None
            h, w = full.shape[:2]
            rx, ry, rw, rh = self._clip(self.region, w, h)
            if rw <= 0 or rh <= 0:
                return None
            key = (rx, ry, rw, rh, round(self.scale, 4), int(self.quality))
            # Re-encoding an unchanged screen is pure waste unless the viewer
            # asked for something different or its frame is going stale.
            if not fresh and key == self._sent_key and \
                    time.perf_counter() - self._sent_at < CAPTURE_STALE_S:
                self.unchanged += 1
                return None
            img = full[ry:ry + rh, rx:rx + rw]
        else:
            rx, ry, rw, rh = self.region
            shot = self._sct.grab({"left": rx, "top": ry,
                                   "width": rw, "height": rh})
            # mss gives BGRA; cvtColor also hands back a contiguous buffer, which
            # imencode needs (a [:, :, :3] slice is a non-contiguous view).
            img = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)
            key = (rx, ry, rw, rh, round(self.scale, 4), int(self.quality))

        if self.scale < 0.999:
            dw = max(int(rw * self.scale), 16)
            dh = max(int(rh * self.scale), 16)
            # INTER_AREA costs a few ms more than INTER_LINEAR but keeps small
            # UI text legible, which is the whole point of looking at the tile.
            img = cv2.resize(img, (dw, dh), interpolation=cv2.INTER_AREA)
        elif not img.flags["C_CONTIGUOUS"]:
            # An unscaled dxcam crop is a view into the cached full frame.
            img = np.ascontiguousarray(img)

        ok, buf = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), int(self.quality)])
        if not ok:
            return None
        self._sent_key = key
        self._sent_at = time.perf_counter()
        return (rx, ry, rw, rh), buf.tobytes()

    @staticmethod
    def _clip(region, w, h):
        """Clip an absolute region to the captured frame, as (x, y, w, h).

        The viewer maps clicks with whatever geometry we report, so a clipped
        grab has to report the clipped region rather than the requested one.
        """
        rx, ry, rw, rh = region
        x0, y0 = max(0, int(rx)), max(0, int(ry))
        x1 = min(w, int(rx) + int(rw))
        y1 = min(h, int(ry) + int(rh))
        return x0, y0, x1 - x0, y1 - y0

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
        # The dxcam camera is shared and deliberately outlives this Session.


# ── one viewer connection ─────────────────────────────────────────────────────
class Session:
    def __init__(self, chan, hid, name, warnings):
        self.chan = chan
        self.hid = hid
        self.name = name
        self.warnings = warnings
        self.cap = None
        self.fps = IDLE_FPS
        self.stop = threading.Event()
        self.last_msg = time.time()
        # Smoothed cost of building one frame (grab + convert + resize + encode).
        # Reported to the viewer so you can see how much of a core this is using
        # before raising the frame rate on a machine that is already busy.
        self._build_ms = 0.0
        self._stat_at = 0.0
        # One-slot handoff to the paced sender thread. Latest frame wins; a
        # queue here would trade latency for throughput, which is the wrong
        # trade for a remote control.
        self._outbox = None
        self._outbox_cv = threading.Condition()
        self._dropped = 0

    # ---- command handling ----
    def _handle(self, cmd):
        t = cmd.get("t")

        # view control needs no Arduino
        if t == "fps":
            self.fps = max(0.2, min(float(cmd.get("v", IDLE_FPS)), 30.0))
            return
        if t == "scale":
            self.cap.scale = max(0.05, min(float(cmd.get("v", DEFAULT_SCALE)), 1.0))
            return
        if t == "quality":
            self.cap.quality = max(1, min(int(cmd.get("v", DEFAULT_QUALITY)), 100))
            return
        if t == "region":
            self.cap.set_region(cmd.get("x", 0), cmd.get("y", 0),
                                cmd.get("w", 0), cmd.get("h", 0))
            return
        if t == "ping":
            self.chan.send_json({"t": "pong"})
            return

        if not self.hid.connected:
            self.chan.send_json({"t": "error", "msg": "Arduino not connected; "
                                                      "input refused"})
            return

        if t == "click":
            btn = self._btn(cmd)
            if btn:
                self.hid.move_abs(cmd["x"], cmd["y"])
                self.hid.click(btn)
        elif t == "mdown":
            btn = self._btn(cmd)
            if btn:
                if "x" in cmd and "y" in cmd:
                    self.hid.move_abs(cmd["x"], cmd["y"])
                self.hid.button_down(btn)
        elif t == "mup":
            btn = self._btn(cmd)
            if btn:
                self.hid.button_up(btn)
        elif t == "move":
            self.hid.move_abs(cmd["x"], cmd["y"])
        elif t == "moverel":
            self.hid.move_rel(cmd.get("dx", 0), cmd.get("dy", 0))
        elif t == "drag":
            btn = self._btn(cmd)
            if btn:
                if "x" in cmd and "y" in cmd:
                    self.hid.move_abs(cmd["x"], cmd["y"])
                self.hid.drag(cmd.get("dx", 0), cmd.get("dy", 0), btn)
        elif t == "scroll":
            if "x" in cmd and "y" in cmd:
                self.hid.move_abs(cmd["x"], cmd["y"])
            self.hid.scroll(cmd.get("steps", 0))
        elif t == "kdown":
            k = self._key(cmd)
            if k:
                self.hid.key_down(k)
        elif t == "kup":
            k = self._key(cmd)
            if k:
                self.hid.key_up(k)
        elif t == "key":
            k = self._key(cmd)
            if k:
                self.hid.key_tap(k, cmd.get("hold_ms", 45))
        elif t == "combo":
            keys = [str(k).lower() for k in cmd.get("keys", [])]
            keys = [k for k in keys if k in VALID_KEYS]
            if keys:
                self.hid.key_combo(keys, cmd.get("hold_ms", 45))
        elif t == "release_all":
            self.hid.release_all()
        else:
            logger.warn(f"[NET] unknown command: {t}")

    def _btn(self, cmd):
        btn = str(cmd.get("btn", "left")).lower()
        if btn not in VALID_BUTTONS:
            logger.warn(f"[NET] bad button: {btn!r}")
            return None
        return btn

    def _key(self, cmd):
        k = str(cmd.get("key", "")).lower()
        if k not in VALID_KEYS:
            logger.warn(f"[NET] bad key: {k!r}")
            return None
        return k

    # ---- threads ----
    def _reader(self):
        try:
            while not self.stop.is_set():
                msg = self.chan.recv()
                if msg is None:
                    break
                mtype, payload = msg
                if mtype != proto.MSG_JSON:
                    continue
                self.last_msg = time.time()
                try:
                    self._handle(proto.decode_json(payload))
                except Exception as e:
                    logger.error(f"[NET] command failed: {e}")
        except Exception as e:
            logger.error(f"[NET] reader died: {e}")
        finally:
            self.stop.set()

    def _watchdog(self):
        """A viewer that dies mid-drag must not leave a button held down."""
        while not self.stop.wait(1.0):
            if self.hid.anything_held and \
                    time.time() - self.last_msg > WATCHDOG_SILENCE:
                logger.warn(f"[NET] silent for {WATCHDOG_SILENCE:.0f}s while "
                            f"holding input - releasing")
                self.hid.release_all()

    def run(self):
        self.cap = Capturer()
        sx, sy, sw, sh = self.cap.screen
        self.chan.send_json({
            "t": "hello", "name": self.name,
            "sx": sx, "sy": sy, "sw": sw, "sh": sh,
            "arduino": self.hid.connected,
            "cap": self.cap.backend,
            "warn": self.warnings,
        })
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._sender, daemon=True).start()

        next_at = time.perf_counter()
        try:
            while not self.stop.is_set():
                now = time.perf_counter()
                if now < next_at:
                    time.sleep(min(next_at - now, 0.05))
                    continue
                period = 1.0 / max(self.fps, 0.05)
                next_at = max(now, next_at + period)
                t0 = time.perf_counter()
                try:
                    got = self.cap.grab()
                except Exception as e:
                    logger.error(f"[CAP] grab failed: {e}")
                    time.sleep(0.5)
                    continue
                if got is None:
                    continue
                build_ms = (time.perf_counter() - t0) * 1000.0
                self._build_ms = (build_ms if self._build_ms == 0.0
                                  else self._build_ms * 0.8 + build_ms * 0.2)

                # Hand off to the sender thread rather than writing here.
                # Capture already fills most of the frame interval, so the two
                # have to overlap for there to be any time to pace into.
                with self._outbox_cv:
                    if self._outbox is not None:
                        # Still pacing the previous frame. Newest frame wins:
                        # dropping one is better than letting latency grow.
                        self._dropped += 1
                    self._outbox = got
                    self._outbox_cv.notify()
        finally:
            self.stop.set()
            with self._outbox_cv:
                self._outbox_cv.notify_all()
            self.cap.close()
            # Whatever the viewer was holding, let go of it.
            self.hid.release_all()
            self.chan.close()

    def _sender(self):
        """Write frames out, spread across the frame interval.

        Kept off the capture thread on purpose: a single sendall() of a few
        hundred KB is a line-rate microburst, and pacing it needs wall time
        that the capture loop does not have to spare.
        """
        while not self.stop.is_set():
            with self._outbox_cv:
                while self._outbox is None and not self.stop.is_set():
                    self._outbox_cv.wait(0.2)
                if self.stop.is_set():
                    return
                geom_jpeg = self._outbox
                self._outbox = None

            (rx, ry, rw, rh), jpeg = geom_jpeg

            # Never delay a frame by more than PACE_MAX_S: at IDLE_FPS the
            # interval is a whole second, and spreading over that would make
            # selecting a tile feel broken.
            budget = min(PACE_FRACTION / max(self.fps, 0.05), PACE_MAX_S)
            if not self.chan.send_frame_paced(rx, ry, rw, rh, jpeg, budget):
                self.stop.set()
                return

            wall = time.time()
            if wall - self._stat_at >= 1.0:
                self._stat_at = wall
                dropped, self._dropped = self._dropped, 0
                same, self.cap.unchanged = self.cap.unchanged, 0
                self.chan.send_json({
                        "t": "stat",
                        "ms": round(self._build_ms, 1),
                        "kb": round(len(jpeg) / 1024.0, 1),
                        "drop": dropped,
                        "same": same,
                        # Live, not just at hello: capture can fail over to mss
                        # mid-session and the label should follow.
                        "cap": self.cap.backend,
                        "lang": keyboard_layout(),
                    })


# ── main ──────────────────────────────────────────────────────────────────────
def serve(name, port, arduino_port):
    warnings = []
    w = pointer_precision_warning()
    if w:
        warnings.append(w)
        logger.warn(f"[SYS] {w}")

    hid = HidLink(arduino_port)
    if not hid.connect():
        warnings.append(f"Arduino not available on {arduino_port}. "
                        "Streaming only, input disabled.")
        logger.warn("[HID] running view-only: no Arduino, and no fallback exists")
    elif not hid.firmware_ok:
        warnings.append("OLD FIRMWARE - flash mouse.ino. Clicks work; "
                        "drag-pan and keystrokes do not.")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(2)
    logger.info(f"[NET] {name} listening on 0.0.0.0:{port}")

    current = None
    try:
        while True:
            sock, addr = srv.accept()
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass
            if current is not None and not current.stop.is_set():
                logger.warn("[NET] new viewer connected - dropping the old one")
                current.stop.set()
                current.chan.close()
            logger.info(f"[NET] viewer connected from {addr[0]}:{addr[1]}")
            current = Session(Channel(sock), hid, name, warnings)
            t = threading.Thread(target=current.run, daemon=True)
            t.start()
    except KeyboardInterrupt:
        logger.info("[NET] shutting down")
    finally:
        if current is not None:
            current.stop.set()
        hid.close()
        srv.close()


def main():
    ap = argparse.ArgumentParser(description="PXM slave screen streamer + Arduino HID bridge")
    ap.add_argument("--name", default=socket.gethostname(), help="label shown in the viewer")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port to listen on")
    ap.add_argument("--arduino", default="", help="Arduino COM port, e.g. COM12")
    ap.add_argument("--capture", default="auto", choices=("auto", "dxcam", "mss"),
                    help="force a capture backend instead of preferring dxcam")
    args = ap.parse_args()

    force_backend(args.capture)
    make_dpi_aware()
    if not raise_timer_resolution():
        logger.warn("[NET] no 1ms timer - frame pacing will be coarse")
    cv2.setNumThreads(CV_THREADS)

    arduino_port = args.arduino
    if not arduino_port:
        try:
            import serial.tools.list_ports as lp
            for p in lp.comports():
                if "arduino" in (p.description or "").lower():
                    arduino_port = p.device
                    logger.info(f"[HID] auto-detected {p.device} ({p.description})")
                    break
        except Exception:
            pass
    if not arduino_port:
        arduino_port = "COM3"
        logger.warn(f"[HID] no Arduino found, will try {arduino_port}")

    serve(args.name, args.port, arduino_port)


if __name__ == "__main__":
    main()
