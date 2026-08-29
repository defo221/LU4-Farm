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
import threading
import time

from logger import info

# VK codes
_VK_CAPSLOCK   = 0x14
_VK_SCROLLLOCK = 0x91

_PAUSE_VK = _VK_SCROLLLOCK   # default; overridden by set_pause_key()

# Optional viewer-manual event.  When set, the viewer has manual focus on this
# PC's tile; the bot should yield as if the pause key were pressed.
# Register it once at startup via register_viewer_manual(); all existing
# raise_if_on() / wait_off() / interruptible_sleep() calls will honour it
# automatically without any further changes to the bot's code.
_viewer_manual: "threading.Event | None" = None

# Optional viewer fps-lock event.  When set, the tile is [MAX]-locked in the
# Viewer (middle-click); the bot pauses exactly like CapsLock.  Independent of
# _viewer_manual so that un-hovering a tile does not accidentally un-pause a
# MAX-locked bot.
_viewer_fps_lock: "threading.Event | None" = None

# When True, raise_if_on() / interruptible_sleep() are no-ops.
# Used by "LastHit Only" mode so that this process's bot ignores the pause key
# while still suppressing ground clicks internally.
_bypass: bool = False


def register_viewer_manual(event: threading.Event) -> None:
    """Register the Event used by the sender to signal viewer manual focus."""
    global _viewer_manual
    _viewer_manual = event


def register_viewer_fps_lock(event: threading.Event) -> None:
    """Register the Event used by the sender to signal [MAX] fps-lock pause."""
    global _viewer_fps_lock
    _viewer_fps_lock = event


def set_bypass(enabled: bool) -> None:
    """Enable or disable the pause-key bypass for this process.

    When True, raise_if_on() and interruptible_sleep() become no-ops so the
    bot keeps running even while the pause key is toggled on.  Used by
    "LastHit Only" mode so that selected bots ignore CapsLock while the rest
    remain fully paused.
    """
    global _bypass
    _bypass = bool(enabled)


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


def _key_on() -> bool:
    """Return True if the hardware pause key is toggled on."""
    try:
        return bool(ctypes.windll.user32.GetKeyState(_PAUSE_VK) & 0x0001)
    except Exception:
        return False


def is_on() -> bool:
    """Return True if paused for any reason: pause key, viewer focus, or MAX lock."""
    if _key_on():
        return True
    vm = _viewer_manual
    if vm is not None and vm.is_set():
        return True
    fl = _viewer_fps_lock
    return fl is not None and fl.is_set()


def pause_reason() -> str:
    """Return why the bot is currently paused, or '' if it is running.

    'key'    – hardware pause key (CapsLock / ScrollLock) is toggled on
    'viewer' – the viewer has manual focus on this tile
    'max'    – the tile is [MAX]-locked in the Viewer (middle-click)
    ''       – not paused
    """
    if _key_on():
        return "key"
    vm = _viewer_manual
    if vm is not None and vm.is_set():
        return "viewer"
    fl = _viewer_fps_lock
    if fl is not None and fl.is_set():
        return "max"
    return ""


def raise_if_on():
    """Raise CapsLockPause if paused (pause key or viewer manual focus).

    No-op when the bypass is active (e.g. "LastHit Only" mode).
    """
    if _bypass:
        return
    if is_on():
        raise CapsLockPause()


def raise_if_lasthit_paused(hp_only: bool) -> None:
    """Raise CapsLockPause when the bot should yield to LastHit-Only mode.

    Unlike raise_if_on(), this fires even while the bypass is active — it is
    intended for targeting / approach loops that must hand control back to
    _lastHit_only_cycle() when the pause key is toggled on mid-flow.

    Call pattern (in every while-True iteration of a long-running loop):
        capslock.raise_if_on()                   # normal pause path
        capslock.raise_if_lasthit_paused(self._hp_only)  # LastHit-Only path
    """
    if hp_only and is_on():
        raise CapsLockPause()


def wait_off():
    """Block until all pause sources are cleared: pause key, viewer focus, MAX lock.

    Logs on entry and exit.  No-op if already unpaused or if the bypass is active.
    """
    if _bypass or not is_on():
        return
    reason = pause_reason()
    if reason == "key":
        info("CapsLock - waiting for release...")
    elif reason == "max":
        info("Viewer [MAX] lock — bot paused, waiting for unlock...")
    else:
        info("Viewer focus — bot paused, waiting for viewer to release...")
    while is_on() and not _bypass:
        time.sleep(0.2)
    info("Resumed")


def interruptible_sleep(seconds, interval=0.2):
    """Sleep for *seconds* in small increments.

    Raises CapsLockPause immediately if paused (pause key or viewer focus)
    mid-sleep.  The remaining sleep is abandoned — the outer loop restarts fresh.
    """
    end = time.time() + seconds
    while time.time() < end:
        chunk = min(interval, max(0.0, end - time.time()))
        time.sleep(chunk)
        raise_if_on()
