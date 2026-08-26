"""
rmb_anchor_timer.py
-------------------
Measures how many milliseconds after a configurable key or mouse button
PRESS the bag_mob_anchor image first appears on screen.

  At startup the script asks you to press the key or mouse button you want
  to track — any keyboard key or left / right / middle mouse button.

  • Uses WH_KEYBOARD_LL + WH_MOUSE_LL to catch the DOWN event (not release).
  • Polls the top ANCHOR_TOP_REGION_PX rows of the primary monitor every
    50 ms until the anchor is found or 1 s passes.
  • Prints elapsed ms + confidence for every measurement, then a summary
    when you press F12.

Run as Administrator if the game process is elevated.
"""

import ctypes
import ctypes.wintypes as wt
import os
import statistics
import sys
import threading
import time
from typing import Optional

import cv2
import numpy as np
import mss

# ---------------------------------------------------------------------------
# Load project config + anchor template
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg

_anchor_path = os.path.join(cfg.PROFILE["assets_dir"], "bag_mob_anchor.png")
_anchor_tmpl = cv2.imread(_anchor_path, cv2.IMREAD_COLOR)
if _anchor_tmpl is None:
    sys.exit(f"[ERROR] Cannot load anchor image: {_anchor_path}")

_a_h, _a_w = _anchor_tmpl.shape[:2]
print(f"Anchor: {_anchor_path}  ({_a_w}x{_a_h} px)")

# ---------------------------------------------------------------------------
# Windows constants / ctypes
# ---------------------------------------------------------------------------
WH_KEYBOARD_LL = 13
WH_MOUSE_LL    = 14
HC_ACTION      = 0
WM_KEYDOWN     = 0x0100
WM_SYSKEYDOWN  = 0x0104
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
VK_F12         = 0x7B

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    ctypes.c_int,
    ctypes.c_ulonglong,
    ctypes.c_longlong,
)

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      wt.DWORD),
        ("scanCode",    wt.DWORD),
        ("flags",       wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]

# VK → label for display
_VK_NAMES: dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter",
    0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt",
    0x1B: "Escape", 0x20: "Space",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    **{0x70 + i: f"F{i+1}" for i in range(12)},
    **{0x30 + i: str(i)    for i in range(10)},
    **{0x41 + i: chr(0x41 + i) for i in range(26)},
    0xA0: "LShift", 0xA1: "RShift", 0xA2: "LCtrl", 0xA3: "RCtrl",
    0xA4: "LAlt",   0xA5: "RAlt",
    0x5B: "Win",
}
_MS_NAMES = {
    WM_LBUTTONDOWN: "Left mouse",
    WM_RBUTTONDOWN: "Right mouse",
    WM_MBUTTONDOWN: "Middle mouse",
}

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.SetWindowsHookExW.restype  = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                      ctypes.c_void_p, wt.DWORD]
user32.CallNextHookEx.restype     = ctypes.c_longlong
user32.CallNextHookEx.argtypes    = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_ulonglong, ctypes.c_longlong]
user32.UnhookWindowsHookEx.restype  = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

# ---------------------------------------------------------------------------
# Trigger configuration — filled in during the setup phase
# ---------------------------------------------------------------------------
# One of: {"kind": "key",   "vk": <int>,  "label": <str>}
#         {"kind": "mouse", "wm": <int>,  "label": <str>}
_trigger_cfg: Optional[dict] = None
_setup_event  = threading.Event()   # fired when trigger is configured

# ---------------------------------------------------------------------------
# Measurement state
# ---------------------------------------------------------------------------
_kb_hook = None
_ms_hook = None
_stop    = False

_trigger_t0: Optional[float] = None
_measure_event = threading.Event()
_busy          = threading.Lock()

_results:  list[float] = []
_timeouts: int         = 0

# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------
def _kb_callback(nCode: int, wParam: int, lParam: int) -> int:
    global _stop, _trigger_t0, _trigger_cfg
    if nCode == HC_ACTION and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode

        if vk == VK_F12:
            _stop = True
            user32.PostQuitMessage(0)
            return user32.CallNextHookEx(_kb_hook, nCode, wParam, lParam)

        # Setup phase: first non-F12 key configures the trigger
        if _trigger_cfg is None:
            label = _VK_NAMES.get(vk, f"VK {vk:#04x}")
            _trigger_cfg = {"kind": "key", "vk": vk, "label": label}
            _setup_event.set()
            return user32.CallNextHookEx(_kb_hook, nCode, wParam, lParam)

        # Measurement phase
        if _trigger_cfg["kind"] == "key" and vk == _trigger_cfg["vk"]:
            _trigger_t0 = time.perf_counter()
            _measure_event.set()

    return user32.CallNextHookEx(_kb_hook, nCode, wParam, lParam)


def _ms_callback(nCode: int, wParam: int, lParam: int) -> int:
    global _trigger_t0, _trigger_cfg
    if nCode == HC_ACTION and wParam in (WM_LBUTTONDOWN,
                                          WM_RBUTTONDOWN,
                                          WM_MBUTTONDOWN):
        # Setup phase: first mouse button configures the trigger
        if _trigger_cfg is None:
            label = _MS_NAMES[wParam]
            _trigger_cfg = {"kind": "mouse", "wm": wParam, "label": label}
            _setup_event.set()
            return user32.CallNextHookEx(_ms_hook, nCode, wParam, lParam)

        # Measurement phase
        if _trigger_cfg["kind"] == "mouse" and wParam == _trigger_cfg["wm"]:
            _trigger_t0 = time.perf_counter()
            _measure_event.set()

    return user32.CallNextHookEx(_ms_hook, nCode, wParam, lParam)


_KB_PROC: HOOKPROC = HOOKPROC(_kb_callback)
_MS_PROC: HOOKPROC = HOOKPROC(_ms_callback)

# ---------------------------------------------------------------------------
# Polling thread
# ---------------------------------------------------------------------------
POLL_MS    = 50
TIMEOUT_S  = 1.0
CONFIDENCE = cfg.ANCHOR_CONFIDENCE


def _poll_thread() -> None:
    global _timeouts

    # Wait until trigger is configured before starting measurements
    _setup_event.wait()

    with mss.mss() as sct:
        mon = sct.monitors[1]
        _roi = (cfg.BAG_MOB_ANCHOR_ROI_FHD
                if cfg.RESOLUTION == "FHD"
                   and getattr(cfg, "BAG_MOB_ANCHOR_ROI_FHD", None) is not None
                else None)
        if _roi is not None:
            rx1, ry1, rx2, ry2 = _roi
            region = {
                "left":   mon["left"] + rx1,
                "top":    mon["top"]  + ry1,
                "width":  rx2 - rx1,
                "height": ry2 - ry1,
            }
        else:
            region = {
                "left":   mon["left"],
                "top":    mon["top"],
                "width":  mon["width"],
                "height": min(mon["height"], cfg.ANCHOR_TOP_REGION_PX),
            }

        idx = 0

        while not _stop:
            fired = _measure_event.wait(timeout=0.1)
            if not fired or _stop:
                continue
            _measure_event.clear()

            t0 = _trigger_t0
            if t0 is None:
                continue

            with _busy:
                idx     += 1
                found    = False
                poll_n   = 0
                deadline = t0 + TIMEOUT_S
                score    = 0.0

                while time.perf_counter() < deadline and not _stop:
                    # Newer press supersedes current measurement
                    if _trigger_t0 != t0:
                        t0       = _trigger_t0
                        deadline = t0 + TIMEOUT_S
                        poll_n   = 0
                        _measure_event.clear()

                    poll_n += 1
                    next_at = t0 + poll_n * POLL_MS / 1000.0
                    sleep_s = next_at - time.perf_counter()
                    if sleep_s > 0:
                        time.sleep(sleep_s)

                    raw   = sct.grab(region)
                    frame = np.frombuffer(raw.raw, dtype=np.uint8)
                    frame = frame.reshape((raw.height, raw.width, 4))
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                    res = cv2.matchTemplate(frame, _anchor_tmpl,
                                            cv2.TM_CCOEFF_NORMED)
                    _, score, _, _ = cv2.minMaxLoc(res)
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0

                    if score >= CONFIDENCE:
                        _results.append(elapsed_ms)
                        print(f"  [{idx:>3}]  found  {elapsed_ms:>7.1f} ms  "
                              f"score={score:.3f}  poll #{poll_n}")
                        found = True
                        break

                if not found and not _stop:
                    _timeouts += 1
                    print(f"  [{idx:>3}]  timeout after {TIMEOUT_S*1000:.0f} ms"
                          f"  (score {score:.3f} < {CONFIDENCE})")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def _print_summary() -> None:
    label = _trigger_cfg["label"] if _trigger_cfg else "?"
    total = len(_results) + _timeouts
    print(f"\n{'='*56}")
    print(f"  [{label}] -> bag_mob_anchor latency  ({total} measurements)")
    print(f"{'='*56}")
    if _results:
        print(f"  Found   : {len(_results):>3}  "
              f"min={min(_results):.1f} ms  "
              f"avg={statistics.mean(_results):.1f} ms  "
              f"max={max(_results):.1f} ms")
    if _timeouts:
        print(f"  Timeout : {_timeouts:>3}  (anchor never appeared within "
              f"{TIMEOUT_S*1000:.0f} ms)")
    print(f"{'='*56}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global _kb_hook, _ms_hook

    _kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _KB_PROC, None, 0)
    _ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL,    _MS_PROC, None, 0)

    if not _kb_hook or not _ms_hook:
        err = kernel32.GetLastError()
        sys.exit(
            f"[ERROR] Failed to install hooks (error {err}).\n"
            "Run as Administrator: right-click run_rmb_timer.bat → Run as administrator"
        )

    t = threading.Thread(target=_poll_thread, daemon=True, name="poll")
    t.start()

    print("\nbag_mob_anchor latency timer")
    print(f"  Confidence  : {CONFIDENCE}")
    _roi_desc = (
        f"ROI {cfg.BAG_MOB_ANCHOR_ROI_FHD}"
        if cfg.RESOLUTION == "FHD"
           and getattr(cfg, "BAG_MOB_ANCHOR_ROI_FHD", None) is not None
        else f"top {cfg.ANCHOR_TOP_REGION_PX} px"
    )
    print(f"  Search area : {_roi_desc} of primary monitor")
    print(f"  Poll every  : {POLL_MS} ms  |  Timeout : {TIMEOUT_S:.0f} s")
    print()
    print("  >> Press the KEY or MOUSE BUTTON you want to track <<")
    print("     (any keyboard key, or left / right / middle mouse button)")
    print("     F12 to stop and print summary.\n")

    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            # Print confirmation once setup fires
            if _setup_event.is_set() and _trigger_cfg is not None:
                if not hasattr(main, "_announced"):
                    main._announced = True  # type: ignore[attr-defined]
                    print(f"  Tracking: [{_trigger_cfg['label']}]  "
                          f"Press it in-game to measure.  F12 to stop.\n")
    except KeyboardInterrupt:
        pass
    finally:
        if _kb_hook:
            user32.UnhookWindowsHookEx(_kb_hook)
        if _ms_hook:
            user32.UnhookWindowsHookEx(_ms_hook)

    _print_summary()


if __name__ == "__main__":
    main()
