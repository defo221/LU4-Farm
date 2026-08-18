"""
capslock.py - Configurable pause-key mechanism for the main bot thread.

The pause key defaults to ScrollLock but can be changed to CapsLock via
set_pause_key() (called from config.py / bot startup).

Daemon threads are never affected — they continue silently while the main
thread is paused here.

When the pause key is toggled on during any long-running flow, a
CapsLockPause exception is raised immediately, unwinding the call stack
back to the outer loop which waits for the key to be released.
"""

import ctypes
import time

from logger import info

# VK codes
_VK_CAPSLOCK   = 0x14
_VK_SCROLLLOCK = 0x91

_PAUSE_VK = _VK_SCROLLLOCK   # default; overridden by set_pause_key()


def set_pause_key(key_name: str) -> None:
    """Set the pause toggle key. key_name: 'ScrollLock' or 'CapsLock'."""
    global _PAUSE_VK
    mapping = {
        "scrolllock": _VK_SCROLLLOCK,
        "capslock":   _VK_CAPSLOCK,
    }
    vk = mapping.get(key_name.lower().replace(" ", "").replace("_", ""))
    if vk is None:
        info(f"[PAUSE] Unknown key '{key_name}', keeping current ({hex(_PAUSE_VK)})")
        return
    _PAUSE_VK = vk
    info(f"[PAUSE] Pause key set to {key_name} (VK {hex(vk)})")


class CapsLockPause(Exception):
    """Raised when the pause key is toggled on mid-flow."""


def is_on() -> bool:
    """Return True if the pause key is currently toggled on (Windows)."""
    try:
        return bool(ctypes.windll.user32.GetKeyState(_PAUSE_VK) & 0x0001)
    except Exception:
        return False


def raise_if_on():
    """Raise CapsLockPause if CapsLock is currently on."""
    if is_on():
        raise CapsLockPause()


def wait_off():
    """Block the calling thread until CapsLock is released.
    Logs once on entry and once on exit.  No-op if already off.
    """
    if not is_on():
        return
    info("CapsLock - waiting for release...")
    while is_on():
        time.sleep(0.3)
    info("CapsLock released - resuming from foreground client")


def interruptible_sleep(seconds, interval=0.3):
    """Sleep for *seconds* in small increments.

    Raises CapsLockPause immediately if CapsLock is toggled on mid-sleep.
    The remaining sleep is abandoned — the outer loop restarts fresh.
    """
    end = time.time() + seconds
    while time.time() < end:
        chunk = min(interval, max(0.0, end - time.time()))
        time.sleep(chunk)
        raise_if_on()
