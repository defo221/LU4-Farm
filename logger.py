"""
logger.py - rotating file logger + console output.

Format mirrors the reference bot:
    YYYY/MM/DD HH:MM:SS | LEVEL | message

Rotation: keeps 5 completed files + 1 currently-being-written = 6 on disk max.
Each completed file is capped at 10 MB. All runs in a session append to the same
file; a visible run-separator is written at each bot start.
"""

import os
import sys
import time
import threading

MAX_LOG_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per completed file
MAX_COMPLETED_FILES = 5               # not counting the one being written

_lock = threading.Lock()
_handle = None
_current_path = None
_logs_dir = None
_tls = threading.local()  # thread-local storage for per-thread client name


def _client_name():
    return getattr(_tls, "current_client", "")


def _base_dir():
    pxm_base = getattr(sys, "_PXM_BASE", None)
    if pxm_base:
        return pxm_base
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def _ensure_dir():
    global _logs_dir
    if _logs_dir is None:
        _logs_dir = os.path.join(_base_dir(), "logs")
    os.makedirs(_logs_dir, exist_ok=True)
    return _logs_dir


def _list_logs():
    """All pxm_*.log files sorted oldest first."""
    d = _ensure_dir()
    files = []
    for name in os.listdir(d):
        if name.startswith("pxm_") and name.endswith(".log"):
            p = os.path.join(d, name)
            files.append((os.path.getmtime(p), p))
    files.sort()
    return [p for _, p in files]


def _rotate(current_path=None):
    """Delete oldest files until at most MAX_COMPLETED_FILES exist
    (excluding the file currently being written)."""
    files = _list_logs()
    if current_path:
        files = [f for f in files if f != current_path]
    while len(files) > MAX_COMPLETED_FILES:
        try:
            os.remove(files[0])
        except Exception:
            pass
        files.pop(0)


def _open():
    global _handle, _current_path
    if _current_path and os.path.exists(_current_path):
        if os.path.getsize(_current_path) >= MAX_LOG_FILE_SIZE:
            _close()
    if _handle is None:
        _ensure_dir()
        if _current_path is None or not os.path.exists(_current_path):
            # create new file; purge old ones first (don't count new file yet)
            _rotate(current_path=None)
            ts = time.strftime("%Y%m%d_%H%M%S")
            _current_path = os.path.join(_logs_dir, f"pxm_{ts}.log")
        try:
            _handle = open(_current_path, "a", encoding="utf-8")
        except Exception as e:
            print(f"[LOGGER] open failed: {e}")
            _handle = None
    return _handle


def _close():
    global _handle, _current_path
    if _handle is not None:
        try:
            _handle.close()
        except Exception:
            pass
    _handle = None
    _current_path = None


def set_client(name):
    _tls.current_client = name or ""


def clear_client():
    _tls.current_client = ""


def log(message, level="INFO"):
    # auto-prepend [ClientName] unless already present
    prefix = f"[{_client_name()}] " if _client_name() else ""
    if prefix and not message.startswith(prefix):
        message = prefix + message
    stamp_console = time.strftime("%H:%M:%S")
    print(f"[{stamp_console}] {message}")
    with _lock:
        h = _open()
        if h is not None:
            try:
                stamp = time.strftime("%Y/%m/%d %H:%M:%S")
                h.write(f"{stamp} | {level} | {message}\n")
                h.flush()
            except Exception as e:
                print(f"[LOGGER] write failed: {e}")


def run_start():
    """Write a visible separator at the beginning of each bot run."""
    sep = "=" * 60
    stamp = time.strftime("%Y/%m/%d %H:%M:%S")
    with _lock:
        h = _open()
        if h is not None:
            try:
                h.write(f"\n{sep}\n")
                h.write(f"  PXM v1.0  RUN STARTED  {stamp}\n")
                h.write(f"{sep}\n")
                h.flush()
            except Exception:
                pass
    stamp_console = time.strftime("%H:%M:%S")
    print(f"[{stamp_console}] {'=' * 40}")
    print(f"[{stamp_console}]  PXM v1.0  RUN STARTED")
    print(f"[{stamp_console}] {'=' * 40}")


def info(msg):
    log(msg, "INFO")


def warn(msg):
    log(msg, "WARN")


def error(msg):
    log(msg, "ERROR")


def debug(msg):
    log(msg, "DEBUG")
