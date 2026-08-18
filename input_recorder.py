"""
input_recorder.py
-----------------
Records keyboard key press/hold timings and mouse button click timings.
Uses Windows low-level system hooks (WH_KEYBOARD_LL / WH_MOUSE_LL) via
ctypes — no third-party library required.  Works with most games because
the hooks run at the OS level, below where DirectInput/Raw Input operate.

Must be run as Administrator if the game process runs elevated.

Output columns
  time    – seconds since recording started
  type    – KEY or MOUSE
  name    – key / button label
  hold ms – how long the key/button was held down
  gap ms  – idle time between the previous release and this press

Press Escape to stop.  Summary + CSV are written to logs/ on exit.
"""

import csv
import ctypes
import ctypes.wintypes as wt
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# Windows constants
# ---------------------------------------------------------------------------
WH_KEYBOARD_LL  = 13
WH_MOUSE_LL     = 14
HC_ACTION       = 0

WM_KEYDOWN      = 0x0100
WM_KEYUP        = 0x0101
WM_SYSKEYDOWN   = 0x0104
WM_SYSKEYUP     = 0x0105

WM_LBUTTONDOWN  = 0x0201
WM_LBUTTONUP    = 0x0202
WM_RBUTTONDOWN  = 0x0204
WM_RBUTTONUP    = 0x0205
WM_MBUTTONDOWN  = 0x0207
WM_MBUTTONUP    = 0x0208

VK_ESCAPE       = 0x1B

# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      wt.DWORD),
        ("scanCode",    wt.DWORD),
        ("flags",       wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          wt.POINT),
        ("mouseData",   wt.DWORD),
        ("flags",       wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]

# On 64-bit Windows lParam/wParam are 64-bit pointer-sized values.
# Using c_long (32-bit) causes "int too long to convert" for high addresses.
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,   # LRESULT
    ctypes.c_int,        # nCode
    ctypes.c_ulonglong,  # WPARAM
    ctypes.c_longlong,   # LPARAM
)

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Declare argtypes/restype so ctypes never tries to fit 64-bit values
# into a 32-bit c_long return by default.
user32.SetWindowsHookExW.restype  = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                      ctypes.c_void_p, wt.DWORD]
user32.CallNextHookEx.restype     = ctypes.c_longlong
user32.CallNextHookEx.argtypes    = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_ulonglong, ctypes.c_longlong]
user32.UnhookWindowsHookEx.restype  = wt.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetMessageW.argtypes         = [ctypes.c_void_p, wt.HWND,
                                        wt.UINT, wt.UINT]

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_start_at:      float            = time.perf_counter()
_events:        list[dict]       = []
_pending_keys:  dict[str, float] = {}   # vk_str → press perf_counter
_pending_mouse: dict[str, float] = {}   # btn_str → press perf_counter
_last_release:  float            = 0.0
_stop:          bool             = False
_kb_hook:       ctypes.c_long    = None  # type: ignore[assignment]
_ms_hook:       ctypes.c_long    = None  # type: ignore[assignment]

# Virtual-key → human label for common keys
_VK_NAMES: dict[int, str] = {
    0x01: "lbutton", 0x02: "rbutton", 0x04: "mbutton",
    0x08: "backspace", 0x09: "tab", 0x0D: "enter",
    0x10: "shift", 0x11: "ctrl", 0x12: "alt",
    0x1B: "esc", 0x20: "space",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2E: "delete", 0x2D: "insert", 0x24: "home", 0x23: "end",
    0x21: "pageup", 0x22: "pagedown",
    **{0x70 + i: f"f{i+1}" for i in range(12)},       # F1-F12
    **{0x30 + i: str(i) for i in range(10)},           # 0-9
    **{0x41 + i: chr(0x61 + i) for i in range(26)},   # a-z
    0xA0: "lshift", 0xA1: "rshift",
    0xA2: "lctrl",  0xA3: "rctrl",
    0xA4: "lalt",   0xA5: "ralt",
    0xBB: "=",  0xBD: "-",  0xBE: ".",  0xBC: ",",
    0xBA: ";",  0xBF: "/",  0xC0: "`",
    0xDB: "[",  0xDC: "\\", 0xDD: "]",  0xDE: "'",
    0x6B: "num+", 0x6D: "num-", 0x6A: "num*", 0x6F: "num/",
    **{0x60 + i: f"num{i}" for i in range(10)},
}


def _vk_label(vk: int) -> str:
    return _VK_NAMES.get(vk, f"vk{vk:#04x}")


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------

def _record(kind: str, name: str, press_t: float, release_t: float) -> None:
    global _last_release
    hold_ms = round((release_t - press_t) * 1000)
    gap_ms  = round((press_t - _last_release) * 1000) if _last_release else 0
    ev = {
        "time_s":  round(press_t - _start_at, 3),
        "type":    kind,
        "name":    name,
        "hold_ms": hold_ms,
        "gap_ms":  max(0, gap_ms),
    }
    _events.append(ev)
    _last_release = release_t
    print(f"  t={ev['time_s']:>8.3f}s  {kind:<5}  {name:<20}  "
          f"hold={hold_ms:>4}ms   gap={max(0, gap_ms):>5}ms")


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------

def _kb_callback(nCode: int, wParam: int, lParam: int) -> ctypes.c_longlong:
    global _stop
    if nCode == HC_ACTION:
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            label = _vk_label(vk)
            if label not in _pending_keys:
                _pending_keys[label] = time.perf_counter()
        elif wParam in (WM_KEYUP, WM_SYSKEYUP):
            label = _vk_label(vk)
            if vk == VK_ESCAPE:
                _stop = True
                user32.PostQuitMessage(0)
                return 0
            if label in _pending_keys:
                _record("KEY", label,
                        _pending_keys.pop(label), time.perf_counter())
    return user32.CallNextHookEx(_kb_hook, nCode, wParam, lParam)


_MS_DOWN_UP: dict[int, tuple[str, str]] = {
    WM_LBUTTONDOWN: ("left",   "lbutton"),
    WM_LBUTTONUP:   ("left",   "lbutton"),
    WM_RBUTTONDOWN: ("right",  "rbutton"),
    WM_RBUTTONUP:   ("right",  "rbutton"),
    WM_MBUTTONDOWN: ("middle", "mbutton"),
    WM_MBUTTONUP:   ("middle", "mbutton"),
}

def _ms_callback(nCode: int, wParam: int, lParam: int) -> ctypes.c_longlong:
    if nCode == HC_ACTION and wParam in _MS_DOWN_UP:
        label, key = _MS_DOWN_UP[wParam]
        if wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            if key not in _pending_mouse:
                _pending_mouse[key] = time.perf_counter()
        else:
            if key in _pending_mouse:
                _record("MOUSE", label,
                        _pending_mouse.pop(key), time.perf_counter())
    return user32.CallNextHookEx(_ms_hook, nCode, wParam, lParam)


# ---------------------------------------------------------------------------
# Summary / CSV
# ---------------------------------------------------------------------------

def _print_summary() -> None:
    if not _events:
        print("\nNo events recorded.")
        return
    print("\n" + "=" * 72)
    print("  SUMMARY  (per key / button)")
    print("=" * 72)
    print(f"  {'name':<20} {'n':>4}  "
          f"{'hold min':>8}  {'hold avg':>8}  {'hold max':>8}  "
          f"{'gap avg':>8}  {'gap max':>8}")
    print("-" * 72)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for ev in _events:
        grouped[f"{ev['type']}:{ev['name']}"].append(ev)
    for label in sorted(grouped):
        evs   = grouped[label]
        holds = [e["hold_ms"] for e in evs]
        gaps  = [e["gap_ms"]  for e in evs if e["gap_ms"] > 0]
        name  = label.split(":", 1)[1]
        print(f"  {name:<20} {len(evs):>4}  "
              f"{min(holds):>7}ms  "
              f"{statistics.mean(holds):>7.0f}ms  "
              f"{max(holds):>7}ms  "
              f"{statistics.mean(gaps) if gaps else 0:>7.0f}ms  "
              f"{max(gaps) if gaps else 0:>7}ms")
    print("=" * 72)
    all_holds = [e["hold_ms"] for e in _events]
    all_gaps  = [e["gap_ms"]  for e in _events if e["gap_ms"] > 0]
    print(f"\nTotal events : {len(_events)}")
    print(f"Overall hold : min={min(all_holds)}ms  "
          f"avg={statistics.mean(all_holds):.0f}ms  max={max(all_holds)}ms")
    if all_gaps:
        print(f"Overall gap  : min={min(all_gaps)}ms  "
              f"avg={statistics.mean(all_gaps):.0f}ms  max={max(all_gaps)}ms")


def _save_csv() -> None:
    os.makedirs("logs", exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("logs", f"input_record_{ts}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time_s", "type", "name",
                                          "hold_ms", "gap_ms"])
        w.writeheader()
        w.writerows(_events)
    print(f"\nSaved → {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

import atexit as _atexit

# Keep callback wrappers alive at module level so Python's GC never
# collects them while Windows still holds a function pointer to them.
_KB_PROC: HOOKPROC = HOOKPROC(_kb_callback)
_MS_PROC: HOOKPROC = HOOKPROC(_ms_callback)


def main() -> None:
    global _kb_hook, _ms_hook

    print("Input Recorder  (WH_KEYBOARD_LL + WH_MOUSE_LL)")
    print("Press Escape to stop.\n")
    print(f"  {'time':>10}  {'type':<5}  {'name':<20}  "
          f"{'hold':>8}   {'gap':>8}")
    print("-" * 65)

    # hmod must be NULL (0) for low-level global hooks — passing the module
    # handle causes SetWindowsHookExW to fail on many Windows versions.
    _kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _KB_PROC, None, 0)
    _ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL,    _MS_PROC, None, 0)

    if not _kb_hook or not _ms_hook:
        err = kernel32.GetLastError()
        sys.exit(
            f"Failed to install hooks (error {err}).\n"
            "Run this script as Administrator:\n"
            "  Right-click run_recorder.bat → Run as administrator"
        )

    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        pass
    finally:
        if _kb_hook:
            user32.UnhookWindowsHookEx(_kb_hook)
        if _ms_hook:
            user32.UnhookWindowsHookEx(_ms_hook)

    _print_summary()
    if _events:
        _save_csv()


if __name__ == "__main__":
    main()
