"""
arduino_hid.py - Arduino mouse HID transport.

STRICT RULE: every click / cursor move / scroll is produced by the Arduino USB
HID device. The host never injects input. pyautogui is used ONLY to READ the
current cursor position so we can compute a relative move for the Arduino.
"""

import time
import random

import capslock as _capslock

try:
    import serial
    import serial.tools.list_ports as _list_ports
except Exception:
    serial = None
    _list_ports = None

import pyautogui as _pag
import logger


def find_arduino_port(name_hint="Arduino"):
    """Scan all COM ports and return the first whose description contains *name_hint*.

    Returns the port string (e.g. 'COM12') or None if not found.
    Logs every candidate so the user can diagnose mismatches.
    """
    if _list_ports is None:
        logger.warn("pyserial not available - cannot auto-detect Arduino port")
        return None
    ports = list(_list_ports.comports())
    if not ports:
        logger.warn("No COM ports found on this system")
        return None
    hint_lower = name_hint.lower()
    for p in ports:
        desc = (p.description or "").strip()
        logger.info(f"COM scan: {p.device} — {desc}")
        if hint_lower in desc.lower():
            logger.info(f"Arduino auto-detected: {p.device} ({desc})")
            return p.device
    logger.warn(f"No COM port found matching '{name_hint}'. "
                f"Available: {[p.device for p in ports]}")
    return None


class ArduinoHID:
    def __init__(self, port, baud=9600, wait_after_click=0.05, wait_after_scroll=0.05):
        self.port = port
        self.baud = baud
        self.wait_after_click = wait_after_click
        self.wait_after_scroll = wait_after_scroll
        self._ser = None

    # ---- connection ----------------------------------------------------------
    def connect(self):
        if serial is None:
            logger.error("[ARDUINO] pyserial not available")
            return False
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=0.2)
            time.sleep(2.0)
            self._ser.reset_input_buffer()
            logger.info(f"[ARDUINO] connected on {self.port}")
            return True
        except Exception as e:
            logger.error(f"[ARDUINO] connect failed on {self.port}: {e}")
            self._ser = None
            return False

    @property
    def connected(self):
        return self._ser is not None

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # ---- low level -----------------------------------------------------------
    def _send(self, line):
        if self._ser is None:
            logger.warn(f"[ARDUINO] not connected - dropped: {line!r}")
            return False
        try:
            self._ser.write((line + "\n").encode())
            self._ser.flush()
            return True
        except Exception as e:
            logger.error(f"[ARDUINO] write failed: {e}")
            return False

    def _read_int(self, attempts=3):
        if self._ser is None:
            return 0
        for _ in range(attempts):
            try:
                resp = self._ser.readline().decode(errors="ignore").strip()
                if resp.isdigit():
                    return int(resp)
            except Exception:
                pass
        return 0

    # ---- movement ------------------------------------------------------------
    _MOVE_TOLERANCE   = 2   # px; within this distance counts as arrived
    _MOVE_CORRECTIONS = 3   # extra correction passes after the first move

    def move_to(self, x, y):
        """Move cursor to absolute (x, y) via a relative Arduino move.

        Waits for the firmware's 'OK' reply after each MOVE command so the OS
        cursor position is stable before the next check or click.  Falls back
        to up to _MOVE_CORRECTIONS extra passes if the first move undershoots.
        """
        if self._ser is None:
            logger.warn("[ARDUINO] not connected - move skipped")
            return False
        for _ in range(1 + self._MOVE_CORRECTIONS):
            try:
                cur_x, cur_y = _pag.position()
            except Exception:
                cur_x, cur_y = 0, 0
            dx = int(x - cur_x)
            dy = int(y - cur_y)
            if abs(dx) <= self._MOVE_TOLERANCE and abs(dy) <= self._MOVE_TOLERANCE:
                return True
            self._send(f"MOVE,{dx},{dy}")
            try:
                self._ser.readline()   # blocks until firmware replies "OK\n"
            except Exception:
                pass
            time.sleep(0.008)  # allow last USB HID frame to reach the OS
        try:
            fx, fy = _pag.position()
            if (abs(int(x) - fx) > self._MOVE_TOLERANCE or
                    abs(int(y) - fy) > self._MOVE_TOLERANCE):
                logger.info(f"[HID] aim missed: wanted ({x},{y}) got ({fx},{fy})")
        except Exception:
            pass
        return True

    # ---- clicks --------------------------------------------------------------
    def click_left(self):
        ok = self._send("CLICK_LEFT")
        time.sleep(self.wait_after_click)
        return ok

    def click_left_hold(self, hold_min=20, hold_max=50):
        if self._ser is None:
            logger.warn("[ARDUINO] not connected - click_left_hold skipped")
            return 0
        self._ser.reset_input_buffer()
        self._send(f"CLICK_LEFT_HOLD,{hold_min},{hold_max}")
        ms = self._read_int()
        if ms == 0:
            ms = (hold_min + hold_max) // 2
        time.sleep(self.wait_after_click)
        return ms

    def click_right_hold(self, hold_min=20, hold_max=50, wait_after: float = None):
        if self._ser is None:
            logger.warn("[ARDUINO] not connected - click_right_hold skipped")
            return 0
        self._ser.reset_input_buffer()
        self._send(f"CLICK_RIGHT_HOLD,{hold_min},{hold_max}")
        ms = self._read_int()
        if ms == 0:
            ms = (hold_min + hold_max) // 2
        delay = self.wait_after_click if wait_after is None else wait_after
        if delay > 0:
            time.sleep(delay)
        return ms

    def triple_click(self, hold_min=20, hold_max=50, gap_min=20, gap_max=50):
        if self._ser is None:
            logger.warn("[ARDUINO] not connected - triple_click skipped")
            return 0
        self._ser.reset_input_buffer()
        self._send(f"TRIPLE_CLICK,{hold_min},{hold_max},{gap_min},{gap_max}")
        ms = self._read_int()
        if ms == 0:
            ms = (hold_min + hold_max) // 2 * 3 + (gap_min + gap_max) // 2 * 2
        return ms

    # ---- high level ----------------------------------------------------------
    def move_and_click(self, x, y, hold_min=20, hold_max=50):
        self.move_to(x, y)
        held = self.click_left_hold(hold_min, hold_max)
        return held > 0

    def move_and_right_click(self, x, y, hold_min=40, hold_max=80,
                             wait_after: float = None):
        self.move_to(x, y)
        held = self.click_right_hold(hold_min, hold_max, wait_after=wait_after)
        return held > 0

    def shift_left_click_hold(self, hold_min=40, hold_max=80):
        """Hold Left Shift, press LMB for a random duration, release both.

        Requires SHIFT_CLICK_LEFT firmware command.
        Returns actual hold duration in ms (or estimated midpoint on timeout).
        """
        if self._ser is None:
            logger.warn("[ARDUINO] not connected - shift_left_click_hold skipped")
            return 0
        self._ser.reset_input_buffer()
        self._send(f"SHIFT_CLICK_LEFT,{hold_min},{hold_max}")
        ms = self._read_int()
        if ms == 0:
            ms = (hold_min + hold_max) // 2
        time.sleep(self.wait_after_click)
        return ms

    def move_and_shift_click(self, x, y, hold_min=40, hold_max=80):
        """Move to (x, y) then perform a Shift+left-click."""
        self.move_to(x, y)
        return self.shift_left_click_hold(hold_min, hold_max) > 0

    def shift_right_click_hold(self, hold_min=40, hold_max=80):
        """Hold Left Shift, press RMB for a random duration, release both.

        Requires SHIFT_CLICK_RIGHT firmware command.
        Returns actual hold duration in ms (or estimated midpoint on timeout).
        """
        if self._ser is None:
            logger.warn("[ARDUINO] not connected - shift_right_click_hold skipped")
            return 0
        self._ser.reset_input_buffer()
        self._send(f"SHIFT_CLICK_RIGHT,{hold_min},{hold_max}")
        ms = self._read_int()
        if ms == 0:
            ms = (hold_min + hold_max) // 2
        time.sleep(self.wait_after_click)
        return ms

    def move_and_shift_right_click(self, x, y, hold_min=40, hold_max=80):
        """Move to (x, y) then perform a Shift+right-click."""
        self.move_to(x, y)
        return self.shift_right_click_hold(hold_min, hold_max) > 0

    def drag_camera(self, dx: int, dy: int = 0, settle_s: float = 0.30):
        """Hold RMB, drag (dx, dy) pixels from the screen centre, release RMB.

        The cursor is moved to the screen centre before the drag so the full
        pixel distance is always available regardless of where the cursor was.
        After the drag the cursor is returned to screen centre.

        dx > 0  → camera pans left  (character rotates right in L2)
        settle_s: seconds to wait after the drag for the camera to stop moving.
        """
        if self._ser is None:
            logger.warn("[ARDUINO] not connected - drag_camera skipped")
            return False
        try:
            sw, sh = _pag.size()
        except Exception:
            sw, sh = 1920, 1080
        cx, cy = sw // 2, sh // 2
        self.move_to(cx, cy)
        time.sleep(0.05)
        self._send(f"DRAG_RIGHT,{int(dx)},{int(dy)}")
        time.sleep(settle_s)
        self.move_to(cx, cy)
        time.sleep(0.05)
        return True

    def double_click_at(self, x, y, gap_min=80, gap_max=160, y_shift=0):
        """Move to (x, y + y_shift) then send a single DOUBLE_CLICK command.

        y_shift lets the caller land a fixed number of pixels below the
        detected image center (e.g. +30 to click into a slot below a header).
        The Arduino handles both clicks and the gap entirely on-board —
        no serial round-trip between the two clicks, so the OS always
        registers it as a real double-click.
        """
        self.move_to(x, y + y_shift)
        gap_ms = random.randint(gap_min, gap_max)
        return self._send(f"DOUBLE_CLICK,{gap_ms}")

    def multi_click_at(self, x, y, count_min=15, count_max=20,
                       interval_min_ms=80, interval_max_ms=120):
        """Move to (x, y) then fire count rapid left-clicks with random intervals."""
        self.move_to(x, y)
        count = random.randint(count_min, count_max)
        for _ in range(count):
            self._send("CLICK_LEFT")
            time.sleep(random.randint(interval_min_ms, interval_max_ms) / 1000.0)
        return True

    def move_and_click_offset(self, x, y, off_min=3, off_max=9, hold_min=20, hold_max=50):
        ox = random.randint(off_min, off_max) * random.choice((1, -1))
        oy = random.randint(off_min, off_max) * random.choice((1, -1))
        self.move_to(int(x + ox), int(y + oy))
        held = self.click_left_hold(hold_min, hold_max)
        return held > 0

    def scroll(self, steps):
        ok = self._send(f"SCROLL,{int(steps)}")
        time.sleep(self.wait_after_scroll)
        return ok

    # ---- keyboard ------------------------------------------------------------
    def press_key(self, key_name, hold_ms=50):
        """Press and release a single key via Arduino keyboard HID.

        key_name examples: 'enter', 'tab', 'esc', 'f1'..'f12',
                           'ctrl', 'alt', 'shift', 'space',
                           or any single printable char: 'a', '1', '/', ...
        """
        ok = self._send(f"KEY,{key_name},{hold_ms}")
        time.sleep(hold_ms / 1000.0 + 0.05)
        return ok

    def press_key_combo(self, *keys, hold_ms=50, wait_after_s: float = 0.05):
        """Press keys simultaneously and release all.

        Example: press_key_combo('ctrl', 'a')  →  KEY_COMBO,50,ctrl,a

        wait_after_s: extra sleep after the hold (default 50 ms).  Pass 0 when
        an explicit settle delay follows immediately (e.g. Win+N switches).
        """
        if not keys:
            return False
        keys_str = ",".join(str(k) for k in keys)
        ok = self._send(f"KEY_COMBO,{hold_ms},{keys_str}")
        time.sleep(hold_ms / 1000.0 + wait_after_s)
        return ok


def rsleep(a, b, reason=""):
    """Sleep for a random duration in [a, b] seconds, logging the wait.

    Raises CapsLockPause immediately if CapsLock is pressed mid-sleep,
    unwinding the current flow so loop.py can restart from client 1.
    """
    duration = random.uniform(a, b)
    msg = f"Waiting {duration:.2f}s"
    if reason:
        msg += f" for {reason}"
    logger.info(msg)
    _capslock.interruptible_sleep(duration)
