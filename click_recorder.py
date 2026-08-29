"""click_recorder.py — record every LMB and RMB click at OS level.

Installs a low-level Windows mouse hook (WH_MOUSE_LL) that fires on every
left- and right-button-down event, regardless of whether the click came from a
physical mouse, an Arduino HID device, or any other source.

Output goes to the console and is also appended to logs/click_record.log.
Stop with CapsLock (toggles pause) or Ctrl+C.
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
import datetime

# ── Win32 constants ────────────────────────────────────────────────────────────
WH_MOUSE_LL    = 14
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
HC_ACTION      = 0

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# Correct signatures so 64-bit handles are not truncated
user32.SetWindowsHookExW.restype  = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     ctypes.c_void_p, wt.DWORD]
user32.CallNextHookEx.restype     = ctypes.c_long
user32.CallNextHookEx.argtypes    = [ctypes.c_void_p, ctypes.c_int,
                                     wt.WPARAM, wt.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          wt.POINT),
        ("mouseData",   wt.DWORD),
        ("flags",       wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

# ── Output ─────────────────────────────────────────────────────────────────────
LOGS_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE  = os.path.join(LOGS_DIR, "click_record.log")

_log_fh    = open(LOG_FILE, "a", encoding="utf-8")
_count_l   = 0
_count_r   = 0
_paused    = False

def _write(line: str) -> None:
    print(line)
    _log_fh.write(line + "\n")
    _log_fh.flush()

# ── Hook callback ──────────────────────────────────────────────────────────────
def _hook_proc(nCode: int, wParam: int, lParam: int) -> int:
    global _count_l, _count_r, _paused

    if nCode == HC_ACTION and wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN):
        # Check CapsLock for pause toggle
        caps = user32.GetKeyState(0x14) & 0x0001
        if caps:
            if not _paused:
                _paused = True
                _write(f"[{_ts()}] --- PAUSED (CapsLock) ---")
        else:
            if _paused:
                _paused = False
                _write(f"[{_ts()}] --- RESUMED ---")

        if not _paused:
            info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            x, y = info.pt.x, info.pt.y
            if wParam == WM_LBUTTONDOWN:
                _count_l += 1
                _write(f"[{_ts()}] LMB #{_count_l:>4}  x={x:>5}  y={y:>5}")
            else:
                _count_r += 1
                _write(f"[{_ts()}] RMB #{_count_r:>4}  x={x:>5}  y={y:>5}")

    return user32.CallNextHookEx(None, nCode, wParam, lParam)


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    _write(f"[{_ts()}] click_recorder started — logging to {LOG_FILE}")
    _write(f"[{_ts()}] {'TIME':12}  {'BTN':<5}  {'#':>5}  {'X':>5}  {'Y':>5}")
    _write(f"[{_ts()}] {'-'*12}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*5}")

    callback   = HOOKPROC(_hook_proc)
    # WH_MOUSE_LL is a global hook that runs in the installing thread;
    # hMod must be NULL (not the exe handle) per MSDN.
    hook_id    = user32.SetWindowsHookExW(WH_MOUSE_LL, callback, None, 0)

    if not hook_id:
        sys.exit("[ERROR] SetWindowsHookEx failed — try running as Administrator")

    msg = wt.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnhookWindowsHookEx(hook_id)
        _log_fh.close()
        print(f"\n[{_ts()}] Stopped. {_count_l} LMB + {_count_r} RMB recorded → {LOG_FILE}")


if __name__ == "__main__":
    main()
