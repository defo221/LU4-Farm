"""
hotkey_assist.py  —  Standalone hotkey-triggered window-assist script.

Press a trigger key while a game window is in the foreground:
the corresponding action runs on the *opposite* game window via Arduino HID.
The triggering keypress is consumed and NOT forwarded to the game.

No screen capture, image detection, or combat logic is performed.
All keyboard / mouse output is produced exclusively by the Arduino.

Default trigger → action mapping (configure in config.py, section HOTKEY_*):

  Trigger  Action
  -------  -----------------------------------------------
  F1       opposite win: RMB burst → F1 × 1-2
  F2       opposite win: RMB burst → F2 × 1-2
  F3       opposite win: F4 × 1
  F5       opposite win: LMB burst → F1 × 1-2
  F6       opposite win: F5 × 1
  F12      Exit hotkey_assist

Exit: press HOTKEY_STOP_KEY (default F12) or Ctrl+C.

Reused from the main script
---------------------------
  ArduinoHID.press_key / press_key_combo / move_and_right_click / move_and_click
  KEY_HOLD_MIN/MAX_MS, WIN_SETTLE_MS_MIN/MAX
  ASSIST_RMB_COUNT_MIN/MAX, ASSIST_RMB_INTERVAL_MIN/MAX_MS
  ASSIST_ATTACK_COUNT_MIN/MAX, ASSIST_ATTACK_HOLD_MIN/MAX_MS, ASSIST_ATTACK_INTERVAL_MIN/MAX_MS
  ARDUINO_PORT, ARDUINO_BAUD
  WINDOWS (title / taskbar_pos entries)
"""

import ctypes
import ctypes.wintypes as _wt
import queue
import random
import sys
import threading
import time
from typing import Optional

import config as cfg
from arduino_hid import ArduinoHID, find_arduino_port
import logger

try:
    import pyautogui as _pag
except ImportError:
    _pag = None

try:
    import win32gui as _w32
except ImportError:
    _w32 = None


# ---------------------------------------------------------------------------
# Console hardening (mirrors bot.py)
# ---------------------------------------------------------------------------
def _disable_quick_edit() -> None:
    try:
        k32 = ctypes.windll.kernel32
        h   = k32.GetStdHandle(ctypes.c_uint(-10))   # STD_INPUT_HANDLE
        m   = _wt.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(m)):
            k32.SetConsoleMode(h, (m.value & ~0x0040) | 0x0080)
    except Exception:
        pass


_disable_quick_edit()


# ---------------------------------------------------------------------------
# ForegroundLockTimeout = 0 so Win+N switches land immediately (mirrors bot.py)
# ---------------------------------------------------------------------------
def _set_flt(ms: int) -> int:
    try:
        u32 = ctypes.windll.user32
        old = ctypes.c_ulong(0)
        u32.SystemParametersInfoW(0x2000, 0, ctypes.byref(old), 0)
        u32.SystemParametersInfoW(0x2001, 0, ms, 0x0002)
        return int(old.value)
    except Exception:
        return -1


_flt_orig = _set_flt(0)
if _flt_orig >= 0:
    import atexit
    atexit.register(_set_flt, _flt_orig)


# ---------------------------------------------------------------------------
# VK-code lookup table
# ---------------------------------------------------------------------------
_VK_MAP: dict[str, int] = {
    "f1":  0x70, "f2":  0x71, "f3":  0x72, "f4":  0x73,
    "f5":  0x74, "f6":  0x75, "f7":  0x76, "f8":  0x77,
    "f9":  0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "esc": 0x1B, "escape": 0x1B,
    "delete": 0x2E, "insert": 0x2D,
    "scrolllock": 0x91, "capslock": 0x14,
}


def _vk(name: str) -> int:
    """Convert a human-readable key name (e.g. 'f1', 'esc') to a VK code."""
    n = name.lower().replace(" ", "").replace("_", "")
    if n in _VK_MAP:
        return _VK_MAP[n]
    if len(n) == 1:
        vk = ctypes.windll.user32.VkKeyScanA(ord(n)) & 0xFF
        if vk:
            return vk
    raise ValueError(f"[HOTKEY] Unknown key name in config: {name!r}")


# ---------------------------------------------------------------------------
# Window info derived from config.WINDOWS
# ---------------------------------------------------------------------------
def _build_win_info() -> dict:
    """Build {wk: {title, key, assist}} for enabled windows that have taskbar_pos."""
    result: dict[str, dict] = {}
    for wk, wcfg in cfg.WINDOWS.items():
        if not wcfg.get("enabled", True):
            continue
        tp = wcfg.get("taskbar_pos")
        if tp is None:
            logger.warn(
                f"[HOTKEY] '{wk}' has no taskbar_pos — "
                f"Win+N switching unavailable for this window; skipping."
            )
            continue
        tk = str(tp) if tp != 10 else "0"
        assist_attr = f"HOTKEY_{wk.upper()}_ASSIST"
        assist = getattr(cfg, assist_attr, (100, 350))
        result[wk] = {"title": wcfg["title"], "key": tk, "assist": assist}
    return result


_WIN_INFO = _build_win_info()
_WIN_KEYS = list(_WIN_INFO.keys())   # ordered: ["win1", "win2"]

if len(_WIN_KEYS) < 2:
    print(
        "[HOTKEY] ERROR: at least 2 enabled windows with taskbar_pos are required.\n"
        "         Check config.py → WINDOWS and set taskbar_pos for both entries."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Arduino HID connection
# ---------------------------------------------------------------------------
_hid: Optional[ArduinoHID] = None


def _connect_hid() -> ArduinoHID:
    port = cfg.ARDUINO_PORT or find_arduino_port("Arduino")
    if not port:
        logger.error("[HOTKEY] Arduino not found — check USB connection and config.")
        sys.exit(1)
    hid = ArduinoHID(port, cfg.ARDUINO_BAUD)
    if not hid.connect():
        logger.error(f"[HOTKEY] Could not open Arduino on {port}.")
        sys.exit(1)
    return hid


# ---------------------------------------------------------------------------
# Low-level input helpers (all HID-only; mirrors bot.py _press / _switch_to)
# ---------------------------------------------------------------------------

def _press_key(key: str, hold_min: int = None, hold_max: int = None) -> None:
    hold = random.randint(
        hold_min if hold_min is not None else cfg.KEY_HOLD_MIN_MS,
        hold_max if hold_max is not None else cfg.KEY_HOLD_MAX_MS,
    )
    _hid.press_key(key, hold_ms=hold)


def _press_n(key: str, count: int,
             hold_min: int = None, hold_max: int = None) -> None:
    """Press key N times with configured random hold and gap between presses."""
    iv_lo = cfg.ASSIST_ATTACK_INTERVAL_MIN_MS / 1000.0
    iv_hi = cfg.ASSIST_ATTACK_INTERVAL_MAX_MS / 1000.0
    for i in range(count):
        _press_key(key, hold_min, hold_max)
        if i < count - 1:
            time.sleep(random.uniform(iv_lo, iv_hi))


def _win_switch(win_key: str) -> None:
    """Press Win+N to bring the given window to the foreground, then settle."""
    tk     = _WIN_INFO[win_key]["key"]
    settle = random.uniform(cfg.WIN_SETTLE_MS_MIN, cfg.WIN_SETTLE_MS_MAX) / 1000.0
    logger.info(f"[HOTKEY] Win+{tk} → '{_WIN_INFO[win_key]['title']}'")
    _hid.press_key_combo("gui", tk, hold_ms=25, wait_after_s=0)
    time.sleep(settle)


def _rmb_burst(win_key: str) -> None:
    """One RMB burst at the configured assist point for win_key."""
    pt    = _WIN_INFO[win_key]["assist"]
    burst = random.randint(cfg.ASSIST_RMB_COUNT_MIN, cfg.ASSIST_RMB_COUNT_MAX)
    iv_lo = cfg.ASSIST_RMB_INTERVAL_MIN_MS / 1000.0
    iv_hi = cfg.ASSIST_RMB_INTERVAL_MAX_MS / 1000.0
    logger.info(f"[HOTKEY] RMB burst ×{burst} at {pt}")
    for i in range(burst):
        _hid.move_and_right_click(pt[0], pt[1], wait_after=0)
        if i < burst - 1:
            time.sleep(random.uniform(iv_lo, iv_hi))
    # Brief post-burst pause (same range as inter-click gap, mirrors bot.py)
    time.sleep(random.uniform(iv_lo, iv_hi))


def _lmb_burst(win_key: str) -> None:
    """One LMB burst at the configured assist point for win_key."""
    pt    = _WIN_INFO[win_key]["assist"]
    burst = random.randint(cfg.ASSIST_RMB_COUNT_MIN, cfg.ASSIST_RMB_COUNT_MAX)
    iv_lo = cfg.ASSIST_RMB_INTERVAL_MIN_MS / 1000.0
    iv_hi = cfg.ASSIST_RMB_INTERVAL_MAX_MS / 1000.0
    logger.info(f"[HOTKEY] LMB burst ×{burst} at {pt}")
    for i in range(burst):
        _hid.move_and_click(pt[0], pt[1], hold_min=40, hold_max=80)
        if i < burst - 1:
            time.sleep(random.uniform(iv_lo, iv_hi))


def _attack_count() -> int:
    return random.randint(cfg.ASSIST_ATTACK_COUNT_MIN, cfg.ASSIST_ATTACK_COUNT_MAX)


def _opposite(from_wk: str) -> str:
    return next(k for k in _WIN_KEYS if k != from_wk)


def _save_cursor() -> Optional[tuple[int, int]]:
    """Return current cursor position, or None if pyautogui is unavailable."""
    if _pag is None:
        return None
    try:
        p = _pag.position()
        return (int(p.x), int(p.y))
    except Exception:
        return None


def _restore_cursor(pos: Optional[tuple[int, int]]) -> None:
    """Move cursor back to saved position via Arduino."""
    if pos is None or _hid is None:
        return
    _hid.move_to(pos[0], pos[1])
    logger.info(f"[HOTKEY] Cursor restored → {pos}")


# ---------------------------------------------------------------------------
# Action sequences
# ---------------------------------------------------------------------------

def _seq_rmb_key(from_wk: str, opp_wk: str, key: str) -> None:
    """Switch → RMB burst → key × N → switch back → restore cursor."""
    saved = _save_cursor()
    _win_switch(opp_wk)
    _rmb_burst(opp_wk)
    n = _attack_count()
    logger.info(
        f"[HOTKEY] {key.upper()} ×{n} in '{_WIN_INFO[opp_wk]['title']}'"
    )
    _press_n(key, n,
             hold_min=cfg.ASSIST_ATTACK_HOLD_MIN_MS,
             hold_max=cfg.ASSIST_ATTACK_HOLD_MAX_MS)
    _win_switch(from_wk)
    _restore_cursor(saved)


def _seq_press_key(from_wk: str, opp_wk: str, key: str) -> None:
    """Switch → single key press → switch back → restore cursor."""
    saved = _save_cursor()
    _win_switch(opp_wk)
    logger.info(f"[HOTKEY] {key.upper()} in '{_WIN_INFO[opp_wk]['title']}'")
    _press_key(key)
    _win_switch(from_wk)
    _restore_cursor(saved)


def _seq_lmb_key(from_wk: str, opp_wk: str, key: str) -> None:
    """Switch → LMB burst → key × N → switch back → restore cursor."""
    saved = _save_cursor()
    _win_switch(opp_wk)
    _lmb_burst(opp_wk)
    n = _attack_count()
    logger.info(
        f"[HOTKEY] {key.upper()} ×{n} in '{_WIN_INFO[opp_wk]['title']}'"
    )
    _press_n(key, n,
             hold_min=cfg.ASSIST_ATTACK_HOLD_MIN_MS,
             hold_max=cfg.ASSIST_ATTACK_HOLD_MAX_MS)
    _win_switch(from_wk)
    _restore_cursor(saved)


_ACTION_TYPE_FN = {
    "rmb":   _seq_rmb_key,
    "lmb":   _seq_lmb_key,
    "press": _seq_press_key,
}


# ---------------------------------------------------------------------------
# Foreground window detection
# ---------------------------------------------------------------------------

def _foreground_win_key() -> Optional[str]:
    """Return the config key ("win1"/"win2") of the currently active game window."""
    if _w32 is None:
        return _WIN_KEYS[0]   # pywin32 not available — assume win1
    try:
        hwnd  = _w32.GetForegroundWindow()
        title = _w32.GetWindowText(hwnd).lower()
    except Exception:
        return None
    for wk, wi in _WIN_INFO.items():
        if wi["title"].lower() in title:
            return wk
    logger.info(f"[HOTKEY] Foreground title: '{title}'"
                f"  — expected one of: "
                + str([wi['title'] for wi in _WIN_INFO.values()]))
    return None   # foreground is not a configured game window


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_stop_event  = threading.Event()
_busy        = threading.Event()           # set while a sequence is executing
_task_queue: queue.Queue = queue.Queue(maxsize=1)

# Built in main() after config is validated:
_TRIGGER_VK: dict[int, tuple] = {}         # vk_code → (action_type, target_key)
_stop_vk:    int             = 0          # VK code for HOTKEY_STOP_KEY

# ctypes callback pointer — kept at module level to prevent GC while hook is live
_hook_cb_ptr    = None
_hook_handle    = None
_main_thread_id: int = 0   # set in main(); used by hook to post WM_QUIT


# ---------------------------------------------------------------------------
# Worker thread: executes action sequences off the hook thread
# ---------------------------------------------------------------------------

def _worker() -> None:
    while not _stop_event.is_set():
        try:
            from_wk, opp_wk, action_type, target_key = _task_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        _busy.set()
        try:
            fn = _ACTION_TYPE_FN.get(action_type, _seq_press_key)
            fn(from_wk, opp_wk, target_key)
        except Exception as exc:
            logger.error(f"[HOTKEY] Action '{action_type}:{target_key}' failed: {exc}")
        finally:
            _busy.clear()
            _task_queue.task_done()


# ---------------------------------------------------------------------------
# Low-level keyboard hook (64-bit safe; mirrors input_recorder.py)
# ---------------------------------------------------------------------------
_WH_KEYBOARD_LL = 13
_HC_ACTION      = 0
_WM_KEYDOWN     = 0x0100
_WM_SYSKEYDOWN  = 0x0104

_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,    # LRESULT
    ctypes.c_int,         # nCode
    ctypes.c_ulonglong,   # WPARAM
    ctypes.c_longlong,    # LPARAM
)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode",      _wt.DWORD),
        ("scanCode",    _wt.DWORD),
        ("flags",       _wt.DWORD),
        ("time",        _wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(_wt.ULONG)),
    ]


_u32 = ctypes.windll.user32
_u32.SetWindowsHookExW.restype    = ctypes.c_void_p
_u32.SetWindowsHookExW.argtypes   = [ctypes.c_int, _HOOKPROC,
                                      ctypes.c_void_p, _wt.DWORD]
_u32.CallNextHookEx.restype       = ctypes.c_longlong
_u32.CallNextHookEx.argtypes      = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_ulonglong, ctypes.c_longlong]
_u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_u32.GetMessageW.argtypes         = [ctypes.c_void_p, _wt.HWND,
                                      _wt.UINT, _wt.UINT]
_u32.PostThreadMessageW.argtypes  = [_wt.DWORD, _wt.UINT,
                                      ctypes.c_ulonglong, ctypes.c_longlong]
ctypes.windll.kernel32.GetCurrentThreadId.restype = _wt.DWORD

_WM_QUIT = 0x0012


def _hook_cb(nCode: int, wParam: int, lParam: int) -> int:
    if nCode == _HC_ACTION and wParam in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
        kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode

        # Stop key — post WM_QUIT to unblock GetMessageW and exit cleanly
        if vk == _stop_vk:
            _stop_event.set()
            _u32.PostThreadMessageW(_main_thread_id, _WM_QUIT, 0, 0)
            return 1   # consumed; do not forward

        entry = _TRIGGER_VK.get(vk)
        if entry is not None:
            # Honour CapsLock as a global pause: when it is toggled on, pass
            # the trigger through to the game unchanged (same semantics as bot.py).
            caps_on = bool(_u32.GetKeyState(0x14) & 1)
            from_wk = _foreground_win_key()
            if not caps_on and from_wk is not None and not _busy.is_set():
                # Idle — consume the user's trigger and start a sequence.
                action_type, target_key = entry
                try:
                    _task_queue.put_nowait(
                        (from_wk, _opposite(from_wk), action_type, target_key)
                    )
                except queue.Full:
                    pass  # safety; _busy check should prevent this
                return 1   # consumed; do not forward trigger to the game
            # Busy OR not a game window → pass the key through so the game
            # (or the Arduino's HID press in the opposite window) works normally.

    return _u32.CallNextHookEx(_hook_handle, nCode, wParam, lParam)


# ---------------------------------------------------------------------------
# Startup info display
# ---------------------------------------------------------------------------

def _print_trigger_table() -> None:
    _desc = {
        "press": "→ {tgt} on opposite window",
        "rmb":   "→ RMB burst + {tgt} × N on opposite window",
        "lmb":   "→ LMB burst + {tgt} × N on opposite window",
    }
    rows: list[tuple[str, str]] = []
    for trig, atype, tkey in cfg.HOTKEY_MAPPINGS:
        desc = _desc.get(atype, "→ {tgt}").format(tgt=tkey.upper())
        rows.append((trig, desc))
    rows.append((cfg.HOTKEY_STOP_KEY, "Exit hotkey_assist"))
    w = max(len(r[0]) for r in rows) + 2
    print("\n  Trigger   Action")
    print("  " + "-" * 54)
    for trig, desc in rows:
        print(f"  {trig.upper():<{w}} {desc}")
    print()
    w1 = _WIN_INFO[_WIN_KEYS[0]]
    w2 = _WIN_INFO[_WIN_KEYS[1]]
    print(
        f"  Win1 '{w1['title']}' — Win+{w1['key']}  "
        f"assist {w1['assist']}\n"
        f"  Win2 '{w2['title']}' — Win+{w2['key']}  "
        f"assist {w2['assist']}\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _hid, _TRIGGER_VK, _stop_vk, _hook_handle, _hook_cb_ptr, _main_thread_id

    logger.info("[HOTKEY] hotkey_assist starting")

    # Connect to Arduino
    _hid = _connect_hid()
    logger.info("[HOTKEY] Arduino connected")

    # Build VK → (action_type, target_key) map from HOTKEY_MAPPINGS
    _stop_vk = _vk(cfg.HOTKEY_STOP_KEY)
    _TRIGGER_VK = {}
    for trig, atype, tkey in cfg.HOTKEY_MAPPINGS:
        if atype not in _ACTION_TYPE_FN:
            logger.error(
                f"[HOTKEY] Unknown action_type {atype!r} for trigger {trig!r} — "
                f"use 'press', 'rmb', or 'lmb'."
            )
            sys.exit(1)
        _TRIGGER_VK[_vk(trig)] = (atype, tkey)
    # Sanity-check: stop key must not overlap a trigger
    if _stop_vk in _TRIGGER_VK:
        logger.error("[HOTKEY] HOTKEY_STOP_KEY overlaps a trigger key — fix config.")
        sys.exit(1)

    # Record the thread ID so the hook callback can post WM_QUIT to us
    _main_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

    # Worker thread
    threading.Thread(target=_worker, daemon=True, name="ha-worker").start()

    # Install hook — keep HOOKPROC wrapper at module level to prevent GC
    # while Windows still holds the function pointer.
    _hook_cb_ptr = _HOOKPROC(_hook_cb)
    _hook_handle = _u32.SetWindowsHookExW(
        _WH_KEYBOARD_LL, _hook_cb_ptr, None, 0
    )
    if not _hook_handle:
        logger.error("[HOTKEY] SetWindowsHookExW failed — try running as Administrator.")
        sys.exit(1)

    logger.info("[HOTKEY] Keyboard hook installed — listening for triggers")
    _print_trigger_table()

    # Blocking message loop — identical pattern to input_recorder.py.
    # GetMessageW keeps the thread in a proper message-pump state at all times,
    # which is required for WH_KEYBOARD_LL callbacks to be dispatched reliably.
    # PeekMessageW + Sleep would leave gaps where the hook thread is unreachable,
    # causing Windows to silently remove the hook after LowLevelHooksTimeout.
    msg = _wt.MSG()
    try:
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        logger.info("[HOTKEY] Ctrl+C received")
    finally:
        logger.info("[HOTKEY] Shutting down…")
        _stop_event.set()
        if _hook_handle:
            _u32.UnhookWindowsHookEx(_hook_handle)
        if _hid:
            _hid.close()
        logger.info("[HOTKEY] Done.")


if __name__ == "__main__":
    main()
