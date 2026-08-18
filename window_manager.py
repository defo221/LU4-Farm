"""
window_manager.py - locate / activate / minimize game windows.

OS window management only (not input injection): finding windows by title and
restoring/minimizing/foregrounding them. All actual mouse input goes exclusively
through the Arduino HID layer.
"""

import time

import capslock
import logger

try:
    import pygetwindow as gw
except Exception:
    gw = None

try:
    import win32gui
    import win32con
    import win32process
    import win32api as _win32api_thread
except Exception:
    win32gui = None
    win32con = None
    win32process = None
    _win32api_thread = None

try:
    import psutil as _psutil
except Exception:
    _psutil = None


def find_windows(title_substring):
    """Return list of window objects whose title contains the substring."""
    if gw is None:
        return []
    try:
        return [w for w in gw.getAllWindows()
                if title_substring.lower() in (w.title or "").lower()]
    except Exception:
        return []


def get_window(title_substring):
    wins = find_windows(title_substring)
    return wins[0] if wins else None


def get_window_exact(title):
    """Return a window whose title matches *exactly* (case-insensitive)."""
    if gw is None:
        return None
    try:
        for w in gw.getAllWindows():
            if (w.title or "").strip().lower() == title.strip().lower():
                return w
    except Exception:
        pass
    return None


def get_window_for_pid(pid):
    """Return the first visible window that belongs to the given process PID.

    Uses win32gui.EnumWindows to cross-reference window thread/process IDs.
    Falls back to None if win32 is unavailable.
    """
    if win32gui is None or win32process is None or not pid:
        return None
    found_hwnd = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid == pid:
                found_hwnd.append(hwnd)
                return False   # stop enumeration at first match
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass

    if not found_hwnd:
        return None
    return get_window_from_hwnd(found_hwnd[0])


def get_window_for_pid_tree(pid):
    """Return the first visible window belonging to *pid* or any of its children.

    Some launchers spawn a child process that owns the actual UI window.
    Searching the whole process tree makes PID-based activation reliable
    regardless of how the launcher is structured.
    """
    if pid is None:
        return None

    # Collect the launcher PID and all its descendants.
    pids = [pid]
    if _psutil is not None:
        try:
            parent = _psutil.Process(pid)
            pids += [c.pid for c in parent.children(recursive=True)]
        except Exception:
            pass

    for p in pids:
        w = get_window_for_pid(p)
        if w is not None:
            return w
    return None


def _hwnd_of(window):
    for attr in ("_hWnd", "_hwnd"):
        h = getattr(window, attr, None)
        if h:
            return h
    return None


def window_hwnd(window):
    """Return the native hwnd for a pygetwindow object, or None."""
    return _hwnd_of(window)


def is_window_expanded(window):
    """True if the window exists and is not minimized."""
    if window is None:
        return False
    try:
        if getattr(window, "isMinimized", False):
            return False
    except Exception:
        pass
    if win32gui is not None:
        hwnd = _hwnd_of(window)
        if hwnd:
            try:
                return not win32gui.IsIconic(hwnd)
            except Exception:
                pass
    return True


def get_window_region(window, hwnd=None):
    """Return (left, top, width, height) for screen-scoped template matching."""
    if win32gui is None:
        return None
    hwnd = hwnd or _hwnd_of(window)
    if not hwnd:
        return None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width > 0 and height > 0:
            return (left, top, width, height)
    except Exception:
        pass
    return None


def get_hwnd_by_pid(pid):
    """Return the hwnd of the first visible top-level window owned by pid."""
    if pid is None or win32gui is None or win32process is None:
        return None
    found = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid == pid:
                found.append(hwnd)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return found[0] if found else None


def get_window_from_hwnd(hwnd):
    """Wrap a raw hwnd in a pygetwindow Win32Window so existing code can use it."""
    if hwnd is None:
        return None
    if gw is not None:
        try:
            return gw.Win32Window(hwnd)
        except Exception:
            pass
    return None


def get_pid_by_title(title_substring):
    """Return PID of the first window whose title matches. None on failure."""
    try:
        windows = find_windows(title_substring)
        if not windows:
            return None
        hwnd = _hwnd_of(windows[0])
        if not hwnd:
            return None
        if win32process is None:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception as e:
        logger.error(f"Failed to get PID by title: {e}")
        return None


def activate(window, settle=0.0):
    """Restore + bring a window to foreground. Logs activation details.

    Uses pygetwindow first (restore→activate), falling back to win32gui.
    The caller is expected to have minimized the *previous* window before
    calling this, so the target window is in a minimized state and
    SW_RESTORE grants focus without needing the foreground lock.
    """
    if window is None:
        return False

    logger.info(f"Activating window: {window!r}")

    ok = False
    try:
        if getattr(window, "isMinimized", False):
            logger.info(f"Restoring window: {window.title}")
            window.restore()
        window.activate()
        ok = True
    except Exception:
        ok = False

    if not ok and win32gui is not None:
        hwnd = _hwnd_of(window)
        if hwnd:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                ok = True
            except Exception:
                ok = False

    if settle > 0:
        logger.info(f"Waiting {settle:.2f} seconds for activating client")
        capslock.interruptible_sleep(settle)
    return ok


def activate_by_hwnd(hwnd, settle=0.0):
    """Bring a raw hwnd to the foreground (fallback when no window object)."""
    if hwnd is None or win32gui is None:
        return False
    try:
        logger.info(f"Activating window handle: {hwnd}")
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        if settle > 0:
            logger.info(f"Waiting {settle:.2f} seconds for activating client")
            capslock.interruptible_sleep(settle)
        return True
    except Exception as e:
        logger.error(f"activate_by_hwnd failed: {e}")
        return False


def force_activate_hwnd(hwnd, settle=0.0):
    """Bring hwnd to the foreground even when our process doesn't own focus.

    Windows blocks SetForegroundWindow from background processes.  The
    AttachThreadInput trick temporarily connects our thread to the thread that
    owns the current foreground window, allowing BringWindowToTop to succeed.
    Falls back to activate_by_hwnd if win32api is unavailable.
    """
    if hwnd is None or win32gui is None:
        return False

    logger.info(f"Force-activating window handle: {hwnd}")
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        if _win32api_thread is not None and win32process is not None:
            fg_hwnd = win32gui.GetForegroundWindow()
            fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
            our_tid = _win32api_thread.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != our_tid:
                try:
                    win32process.AttachThreadInput(our_tid, fg_tid, True)
                    attached = True
                except Exception:
                    pass
            try:
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    try:
                        win32process.AttachThreadInput(our_tid, fg_tid, False)
                    except Exception:
                        pass
        else:
            win32gui.SetForegroundWindow(hwnd)

        if settle > 0:
            logger.info(f"Waiting {settle:.2f} seconds for activating client")
            capslock.interruptible_sleep(settle)
        return True
    except Exception as e:
        logger.warn(f"force_activate_hwnd failed: {e}")
        return activate_by_hwnd(hwnd, settle=settle)


def minimize(window, settle=0.0):
    if window is None:
        return False
    ok = False
    try:
        window.minimize()
        ok = True
    except Exception:
        pass
    if not ok and win32gui is not None:
        hwnd = _hwnd_of(window)
        if hwnd:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                ok = True
            except Exception:
                pass
    if ok and settle > 0:
        logger.info(f"Waiting {settle:.2f} seconds for minimizing client")
        capslock.interruptible_sleep(settle)
    return ok


def activate_by_title(title_substring, settle=0.0):
    w = get_window(title_substring)
    if w is None:
        return None
    return w if activate(w, settle=settle) else None
