"""
bot.py  -  DD farming bot.

All keyboard / mouse input is produced exclusively by the Arduino HID device.
Screen reading uses mss + OpenCV (read-only, no input injection).

Flow per cycle
--------------
1.  Target search  (F5 loop) in current window  → F1 x2
2.  If win2 enabled: switch, target search, F1 x2, check buffs/death, switch back, check buffs/death
    Else: check buffs/death once
3.  Wait until mob HP is in [MOB_HP_LOW_PCT .. MOB_HP_HIGH_PCT]
    - Every 5 checks alternate windows (if win2 enabled)
    - Also read char HP/MP from same screenshot → F6/F7/F2 as needed
    - Timeout: recovery + restart; second consecutive timeout → stop
4.  Press F2 in both windows
5.  Wait for mob_dead
    - Every 5 checks alternate windows
    - Timeout: recovery + restart
6.  Repeat
"""

import glob
import math
import os
import sys
import random
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np
import mss as _mss_mod

import config as cfg
import capslock
import logger
from arduino_hid import ArduinoHID, find_arduino_port, rsleep
from notifier import Notifier
from window_manager import (find_windows, get_window, activate, minimize,
                             get_window_region, window_hwnd,
                             get_window_from_hwnd)

try:
    import win32gui
except ImportError:
    win32gui = None

# ---------------------------------------------------------------------------
# Startup: apply pause key from config
# ---------------------------------------------------------------------------
capslock.set_pause_key(cfg.PAUSE_KEY)

# ---------------------------------------------------------------------------
# Disable Quick Edit Mode so that clicking/selecting in the console window
# does not pause the bot process.
# ---------------------------------------------------------------------------
def _disable_quick_edit() -> None:
    try:
        import ctypes
        import ctypes.wintypes
        STD_INPUT_HANDLE    = ctypes.c_uint(-10)
        ENABLE_QUICK_EDIT   = 0x0040
        ENABLE_EXTENDED     = 0x0080   # required to modify Quick Edit flag
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(STD_INPUT_HANDLE)
        mode   = ctypes.wintypes.DWORD()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            new_mode = (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED
            kernel.SetConsoleMode(handle, new_mode)
    except Exception:
        pass   # non-Windows or no console attached — silently ignore

_disable_quick_edit()


# ---------------------------------------------------------------------------
# Set ForegroundLockTimeout = 0 so SetForegroundWindow always works without
# user input having happened recently (the Windows default of 200 000 ms
# causes the orange taskbar flash instead of actually switching the window).
# Reverts to the original value when the process exits.
# ---------------------------------------------------------------------------
def _set_foreground_lock(timeout_ms: int) -> int:
    """
    Set the foreground lock timeout via SystemParametersInfo — takes effect
    immediately (unlike the registry key which requires logoff/logon).
    Returns the old timeout value, or -1 on failure.
    """
    try:
        import ctypes
        SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
        SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
        SPIF_SENDCHANGE = 0x0002
        user32 = ctypes.windll.user32
        old = ctypes.c_ulong(0)
        user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT,
                                     0, ctypes.byref(old), 0)
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT,
                                     0, timeout_ms,
                                     SPIF_SENDCHANGE)
        return int(old.value)
    except Exception:
        return -1

_flt_original = _set_foreground_lock(0)
if _flt_original >= 0:
    import atexit as _atexit
    _atexit.register(_set_foreground_lock, _flt_original)


# ---------------------------------------------------------------------------
# HSV colour ranges for bar fill detection
# ---------------------------------------------------------------------------
# HP bar fill pixels sit at H≈140–178 (dark crimson/burgundy, wrap-around red).
# The H 0–15 range (orange-red) is NOT used by any HP bar — it only matches
# reddish outdoor backgrounds, so it is intentionally excluded.
# Single range covers both fill rows (H≈140–167) and border rows (H≈168–178).
_RED_LO  = np.array([140, 100,  50], dtype=np.uint8)  # deep/magenta-red  (mob HP bar)
_RED_HI  = np.array([180, 255, 255], dtype=np.uint8)
_RED2_LO = np.array([  0, 120,  80], dtype=np.uint8)  # bright orange-red  (char HP bar)
_RED2_HI = np.array([ 10, 255, 255], dtype=np.uint8)
_BLUE_LO = np.array([100,  80,  60], dtype=np.uint8)
_BLUE_HI = np.array([135, 255, 255], dtype=np.uint8)

_RED_COL_THRESHOLD  = 0.15   # fraction of bar height that must be red per column
_BLUE_COL_THRESHOLD = 0.15


# ---------------------------------------------------------------------------
# Single-assist target-approach helpers  (no UI, no overlay)
# ---------------------------------------------------------------------------

def _sa_nms(pts: List[Tuple[int, int]], min_dist: int = 8) -> List[Tuple[int, int]]:
    """Non-maximum suppression: drop detections within min_dist of an earlier one."""
    kept: List[Tuple[int, int]] = []
    for p in pts:
        if all(math.hypot(p[0] - k[0], p[1] - k[1]) >= min_dist for k in kept):
            kept.append(p)
    return kept


def _sa_find_blue_dots(frame: np.ndarray,
                       tmpl: Optional[np.ndarray],
                       conf: float = 0.75,
                       nms_dist: int = 8) -> List[Tuple[int, int]]:
    """Return (cx, cy) centres for every in_target_blue detection in *frame*.

    Returns an empty list when the template is None or no match is found.
    """
    if tmpl is None or frame is None:
        return []
    th, tw = tmpl.shape[:2]
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= conf)
    raw = [(int(x + tw // 2), int(y + th // 2)) for x, y in zip(xs, ys)]
    raw.sort(key=lambda p: p[0])           # left-to-right for stable pairing
    return _sa_nms(raw, nms_dist)


def _sa_blue_pair_center(dots: List[Tuple[int, int]],
                         max_dy: int = 6,
                         min_dx: int = 4) -> Optional[Tuple[int, int]]:
    """Return the target point derived from in_target_blue detections.

    Priority:
      1. Midpoint of the first valid left-right pair (same pairing logic as
         test_dual_dot_overlay.py — same row, separated horizontally).
      2. If no pair qualifies, fall back to the first single dot's position so
         the approach corridor can still be built toward it.
      3. Returns None only when no dots were detected at all.
    """
    if not dots:
        return None
    for i, left in enumerate(dots):
        for right in dots[i + 1:]:
            dx = right[0] - left[0]
            dy = abs(right[1] - left[1])
            if dx >= min_dx and dy <= max_dy:
                return (left[0] + right[0]) // 2, (left[1] + right[1]) // 2
    # No valid pair — use the single (or nearest) dot as the target
    return dots[0]


def _sa_corridor_point(sx: int, sy: int,
                       tx: int, ty: int,
                       half_w: int,
                       min_dist_px: int = 0,
                       max_dist_px: int = 0) -> Tuple[int, int]:
    """Return a random point inside the *half_w*-px-half-width strip running
    from screen centre (sx, sy) toward target (tx, ty).

    min_dist_px: minimum distance from (sx, sy) — ensures the character
                 cannot reach the click point before the next action fires.
    max_dist_px: upper bound on distance; 0 = 85% of the total distance to
                 target (avoids overshooting).
    """
    dx, dy = tx - sx, ty - sy
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length   # unit vector toward target
    perp_x, perp_y = -uy, ux            # perpendicular unit vector

    cap = max_dist_px if max_dist_px > 0 else length * cfg.SA_CORRIDOR_MAX_RATIO
    lo  = max(0.0, min(float(min_dist_px), cap * 0.95))
    d   = random.uniform(lo, cap)

    off = random.uniform(-half_w, half_w)
    cx  = int(sx + ux * d + perp_x * off)
    cy  = int(sy + uy * d + perp_y * off)
    return cx, cy


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class StopBot(Exception):
    """Raised to cleanly stop the entire bot."""


class CycleTimeout(Exception):
    """Raised when a wait-phase times out (handled by the outer cycle loop)."""


# ---------------------------------------------------------------------------
# AnchorFinder  -  fast cached matchTemplate search
# ---------------------------------------------------------------------------
class AnchorFinder:
    """
    Finds a template image inside a full-screen numpy frame.

    On each call to find():
      - If a cached position exists: verifies it with a tiny sub-frame crop.
      - On cache miss (or after invalidate()): runs matchTemplate on the full frame.

    This keeps per-tick cost to ~1 ms when the anchor hasn't moved,
    while recovering automatically when the game window is repositioned.
    """

    def __init__(self, image_path: str, confidence: float = cfg.ANCHOR_CONFIDENCE,
                 padding: int = cfg.ANCHOR_CACHE_PADDING,
                 max_y: Optional[int] = None):
        self.path       = image_path
        self.confidence = confidence
        self.padding    = padding
        # max_y: when set, full-screen searches are restricted to rows 0..max_y-1.
        # Cached hits are still accepted at any position (they were already
        # verified, so restricting them would only cause unnecessary cache misses).
        self.max_y      = max_y
        self._tmpl      = cv2.imread(image_path)
        if self._tmpl is None:
            raise FileNotFoundError(f"Anchor image not found: {image_path}")
        self._th, self._tw = self._tmpl.shape[:2]
        self._cx: Optional[int] = None
        self._cy: Optional[int] = None
        self._name      = os.path.splitext(os.path.basename(image_path))[0]
        self._last_score: float = 0.0   # score from the most recent full search

    def invalidate(self) -> None:
        """Force a full-frame search on the next find() call."""
        self._cx = None
        self._cy = None

    def find(self, frame: np.ndarray,
             silent: bool = False) -> Optional[tuple[int, int]]:
        """Return (cx, cy) of the anchor in frame, or None.

        silent=True suppresses all log output (useful for high-frequency checks
        where logging would flood the output, e.g. mob_dead during HP polling).
        """
        if self._cx is not None:
            result = self._verify_cached(frame)
            if result is not None:
                return result

        return self._full_search(frame, silent=silent)

    def _verify_cached(self, frame: np.ndarray) -> Optional[tuple[int, int]]:
        fh, fw = frame.shape[:2]
        x1 = max(0, self._cx - self.padding)
        y1 = max(0, self._cy - self.padding)
        x2 = min(fw, self._cx + self.padding + self._tw)
        y2 = min(fh, self._cy + self.padding + self._th)
        sub = frame[y1:y2, x1:x2]
        if sub.size == 0:
            return None
        res = cv2.matchTemplate(sub, self._tmpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score >= self.confidence:
            self._cx = x1 + loc[0] + self._tw // 2
            self._cy = y1 + loc[1] + self._th // 2
            logger.info(f"[ANCHOR] {self._name} found (cached) at"
                        f" {(self._cx, self._cy)}  score={score:.3f}")
            return (self._cx, self._cy)
        self._cx = None
        self._cy = None
        return None

    def _full_search(self, frame: np.ndarray,
                     silent: bool = False) -> Optional[tuple[int, int]]:
        # Restrict to top N rows so duplicate UI elements lower on screen are ignored.
        search_frame = frame[:self.max_y] if self.max_y is not None else frame
        if search_frame.shape[0] < self._th or search_frame.shape[1] < self._tw:
            # Search region smaller than template — cannot match.
            logger.warn(f"[ANCHOR] {self._name} search region too small"
                        f" ({search_frame.shape[1]}x{search_frame.shape[0]})"
                        f"  template={self._tw}x{self._th}")
            return None
        res = cv2.matchTemplate(search_frame, self._tmpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        self._last_score = score
        if score >= self.confidence:
            self._cx = loc[0] + self._tw // 2
            self._cy = loc[1] + self._th // 2
            if not silent:
                logger.info(f"[ANCHOR] {self._name} found at {(self._cx, self._cy)}"
                            f"  score={score:.3f}")
            return (self._cx, self._cy)
        region_str = f"top {self.max_y}px" if self.max_y is not None else "full frame"
        if not silent:
            logger.warn(f"[ANCHOR] {self._name} NOT found in {region_str}"
                        f"  best score={score:.3f}  threshold={self.confidence:.2f}")
        self._cx = None
        self._cy = None
        return None


# ---------------------------------------------------------------------------
# Bar reading helpers
# ---------------------------------------------------------------------------

_BAR_INSET        = 2   # px trimmed from left/right edges before sampling
_BAR_BOTTOM_ROWS  = 3   # rows sampled from the bottom of the bar (text-free)
_BAR_BOTTOM_SKIP  = 0   # extra rows to skip at the very bottom (set >0 if bar has a border)

def _bar_pct(frame: np.ndarray, anchor_cx: int, anchor_cy: int,
             off_x: int, off_y: int, bar_w: int, bar_h: int,
             color: str, col_threshold: float = 0.15) -> Optional[float]:
    """
    Sample the bottom _BAR_BOTTOM_ROWS rows of the bar (left/right edges
    inset by _BAR_INSET px).  The color range targets the bar's exact fill
    color (H 140–180), which excludes orange-red backgrounds (H 0–30).
    color: 'red' | 'blue'
    """
    fh, fw = frame.shape[:2]
    xi = off_x + _BAR_INSET
    wi = bar_w - 2 * _BAR_INSET
    yi = off_y + bar_h - _BAR_BOTTOM_SKIP - _BAR_BOTTOM_ROWS
    hi = _BAR_BOTTOM_ROWS
    if wi <= 0 or hi <= 0:
        return None
    x = max(0, min(anchor_cx + xi, fw - wi))
    y = max(0, min(anchor_cy + yi, fh - hi))
    bar = frame[y: y + hi, x: x + wi]
    if bar.size == 0:
        return None

    hsv = cv2.cvtColor(bar, cv2.COLOR_BGR2HSV)
    if color == "red":
        # Mob HP bar: narrow deep-red range only (avoids orange backgrounds)
        mask = cv2.inRange(hsv, _RED_LO, _RED_HI)
    elif color == "red_char":
        # Char HP bar: both deep-red and bright orange-red (fixed UI, no BG risk)
        mask = (cv2.inRange(hsv, _RED_LO, _RED_HI) |
                cv2.inRange(hsv, _RED2_LO, _RED2_HI))
    else:  # blue (mana)
        mask = cv2.inRange(hsv, _BLUE_LO, _BLUE_HI)

    col_ratio = mask.sum(axis=0).astype(float) / (hi * 255)
    filled    = np.where(col_ratio >= col_threshold)[0]
    if len(filled) == 0:
        return 0.0
    return min((int(filled[-1]) + 1) / wi * 100.0, 100.0)



def _image_present(frame: np.ndarray, finder: AnchorFinder) -> bool:
    """Return True if finder's template is visible in frame."""
    return finder.find(frame) is not None


# ---------------------------------------------------------------------------
# Window slot
# ---------------------------------------------------------------------------
@dataclass
class WindowSlot:
    key:            str
    title:          str
    role:           str
    enabled:        bool
    taskbar_pos:    Optional[int]             = None  # 1-based taskbar position, or None
    taskbar_key:    Optional[str]             = None  # Win+key ("1"-"9" or "0" for pos 10)
    alive:          bool                      = True
    hwnd:           Optional[int]             = None
    # Per-window targeting mode — chosen interactively at startup.
    targeting_mode:        str                       = "nexttarget"  # "nexttarget" | "assist"
    # Assist-mode calibration: screen point the user clicked during startup.
    # Right-clicking here targets the party leader's mob.  Reset each run.
    assist_point:          Optional[tuple[int, int]] = None
    # Party anchor position cached from the last buff/death-check frame.
    # Reused for recovery left-clicks so we don't need a dedicated grab.
    last_party_pos:        Optional[tuple[int, int]] = None

    def nickname(self) -> str:
        """Use the full window title as the character identifier."""
        return self.title

    def find_and_attach(self) -> bool:
        wins = find_windows(self.title)
        if not wins:
            logger.warn(f"[BOT] Window not found: '{self.title}'")
            return False
        self.hwnd = window_hwnd(wins[0])
        return self.hwnd is not None

    def _window_obj(self):
        """Return a pygetwindow object for this slot (hwnd-first, title fallback)."""
        if self.hwnd:
            w = get_window_from_hwnd(self.hwnd)
            if w is not None:
                return w
        wins = find_windows(self.title)
        return wins[0] if wins else None

    def activate(self, settle_s: float = 0.0) -> bool:
        w = self._window_obj()
        if w is None:
            return False
        return activate(w, settle=settle_s)

    def minimize_window(self) -> bool:
        w = self._window_obj()
        if w is None:
            return False
        return minimize(w)

    def is_foreground(self) -> bool:
        if win32gui is None or self.hwnd is None:
            return False
        try:
            return win32gui.GetForegroundWindow() == self.hwnd
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Key press helpers (Arduino HID)
# ---------------------------------------------------------------------------

def _rhold() -> int:
    return random.randint(cfg.KEY_HOLD_MIN_MS, cfg.KEY_HOLD_MAX_MS)


def _press(hid: ArduinoHID, key: str,
           hold_min: int = None, hold_max: int = None) -> None:
    """Press one key with random hold. Checks pause key before pressing."""
    capslock.raise_if_on()
    hold = random.randint(
        hold_min if hold_min is not None else cfg.KEY_HOLD_MIN_MS,
        hold_max if hold_max is not None else cfg.KEY_HOLD_MAX_MS,
    )
    hid.press_key(key, hold_ms=hold)


def _press_n(hid: ArduinoHID, key: str, count: int,
             hold_min: int = None, hold_max: int = None) -> None:
    """Press key N times with random hold and random interval between."""
    for i in range(count):
        _press(hid, key, hold_min, hold_max)
        if i < count - 1:
            time.sleep(_rhold() / 1000.0)


def _press_wasd(hid: ArduinoHID) -> None:
    """Press one random WASD key for 75–200 ms."""
    _press(hid, random.choice(["w", "a", "s", "d"]),
           hold_min=cfg.KEY_HOLD_MIN_MS, hold_max=cfg.WASD_HOLD_MAX_MS)


# ---------------------------------------------------------------------------
# Telegram wrapper (non-blocking, prefixes PC_NUMBER)
# ---------------------------------------------------------------------------

class TG:
    def __init__(self, notifier: Notifier):
        self._n = notifier

    def send(self, text: str) -> None:
        self._n.send(f"{cfg.PC_NUMBER}: {text}")


# ---------------------------------------------------------------------------
# Main Bot
# ---------------------------------------------------------------------------

class FarmBot:
    def __init__(self, shared_arduino=None, viewer_manual=None):
        if shared_arduino is not None:
            # Shared instance provided by stream_sender — don't open/close it.
            self.hid = shared_arduino
            self._shared_arduino = True
        else:
            port = cfg.ARDUINO_PORT or find_arduino_port("Arduino")
            self.hid = ArduinoHID(port, cfg.ARDUINO_BAUD)
            self._shared_arduino = False

        if viewer_manual is not None:
            capslock.register_viewer_manual(viewer_manual)
        self._notifier = Notifier(cfg.TG_TOKEN, cfg.TG_CHAT_ID)
        self.tg   = TG(self._notifier)
        self.sct  = _mss_mod.MSS()

        # Build window slots
        self.slots: dict[str, WindowSlot] = {}
        for key, wcfg in cfg.WINDOWS.items():
            tp = wcfg.get("taskbar_pos", None)
            # Convert 1-based position to the actual key: positions 1-9 → "1"-"9",
            # position 10 → "0" (Win+0 is the 10th taskbar item on Windows).
            tk = None if tp is None else ("0" if tp == 10 else str(tp))
            self.slots[key] = WindowSlot(
                key         = key,
                title       = wcfg["title"],
                role        = wcfg.get("char_role", "DD"),
                enabled     = wcfg.get("enabled", True),
                taskbar_pos = tp,
                taskbar_key = tk,
            )

        self._active: Optional[WindowSlot] = None  # currently focused slot

        # Potion cooldown tracking (prevents spamming F6/F7 every 200ms)
        self._last_f6: float = 0.0
        self._last_f7: float = 0.0

        # Load anchors
        A = cfg.PROFILE["assets_dir"]
        R = os.path.join(os.path.dirname(__file__), "assets")  # root fallback (QHD)

        def _ap(name: str) -> str:
            """Resolution-specific path, falling back to root assets/ if not present."""
            p = os.path.join(A, name)
            if os.path.isfile(p):
                return p
            fb = os.path.join(R, name)
            if A != R:
                logger.warn(f"[BOT] {name} not in profile folder — using root assets/ fallback")
            return fb

        _top = cfg.ANCHOR_TOP_REGION_PX   # mob/char anchors are only in the top N px
        self._mob_anchor   = AnchorFinder(_ap("bag_mob_anchor.png"),  max_y=_top)
        self._char_anchor  = AnchorFinder(_ap("char_bars_anchor.png"), max_y=_top)
        self._mob_dead_f   = AnchorFinder(_ap("mob_dead.png"))
        self._death_f      = AnchorFinder(_ap("death_screen.png"),    confidence=0.85)
        self._disconnect_f = AnchorFinder(_ap("disconnect.png"),      confidence=cfg.DC_CONFIDENCE)
        self._buff_f       = AnchorFinder(_ap("full_buff_check.png"),  confidence=0.85)
        self._buff_f1      = AnchorFinder(_ap("full_buff_check1.png"), confidence=0.85)
        self._party_f      = AnchorFinder(_ap("party_pl_anchor.png"),
                                          confidence=cfg.PARTY_ANCHOR_CONFIDENCE)

        # mob_skip: any file matching assets/mob_skip*.png triggers an extra F5
        # (skip that target) immediately after bag_mob_anchor is detected.
        skip_paths = sorted(glob.glob(os.path.join(A, "mob_skip*.png")))
        self._mob_skip_finders: list[AnchorFinder] = [
            AnchorFinder(p, max_y=cfg.ANCHOR_TOP_REGION_PX) for p in skip_paths
        ]
        if self._mob_skip_finders:
            logger.info(f"[BOT] mob_skip: {len(self._mob_skip_finders)} image(s) loaded")

        # NC mode: mob name templates  (assets/mob_name*.png)
        # Each entry: (bgr_array, (height, width))
        nc_name_paths = sorted(glob.glob(os.path.join(A, "mob_name*.png")))
        self._mob_name_tmpls: list[tuple[np.ndarray, tuple[int, int]]] = []
        for p in nc_name_paths:
            t = cv2.imread(p)
            if t is not None:
                self._mob_name_tmpls.append((t, t.shape[:2]))
                logger.info(f"[BOT] mob_name template: {os.path.basename(p)}"
                            f"  ({t.shape[1]}x{t.shape[0]})")
        if not self._mob_name_tmpls:
            logger.warn("[BOT] NC mode: no mob_name*.png templates found in assets dir")

        # NC mode: dot templates to detect already-targeted mobs
        def _load_opt(name: str) -> Optional[np.ndarray]:
            path = _ap(name)
            t = cv2.imread(path)
            if t is None:
                logger.warn(f"[BOT] NC dot template not found: {path}")
            return t

        self._dot_red_tmpl:  Optional[np.ndarray] = _load_opt("in_target_red.png")
        self._dot_blue_tmpl: Optional[np.ndarray] = _load_opt("in_target_blue.png")

        # Single-assist phase-3: healer fallback anchor (optional)
        _healer_path = _ap("healer_farm_anchor.png")
        _healer_tmpl = cv2.imread(_healer_path)
        if _healer_tmpl is not None:
            self._healer_anchor: Optional[AnchorFinder] = AnchorFinder(
                _healer_path, confidence=0.60)
            logger.info(f"[BOT] healer_farm_anchor loaded: {_healer_path}")
        else:
            self._healer_anchor = None
            logger.info("[BOT] healer_farm_anchor.png not found — phase-3 will rotate camera only")

        # True while the bot is in the healer-area recovery loop.
        # Cleared as soon as a post-recovery RMB succeeds.
        self._sa_recovery_mode: bool = False

        # Camera orientation (set from viewer UI via stream_sender).
        # None = use the old blind SA_CAMERA_ROTATE_DX drag.
        # When set, the two allowed orientations are:
        #   orient_1 = self._camera_orient_1
        #   orient_2 = (self._camera_orient_1 + 180) % 360
        self._camera_orient_1: Optional[int] = None
        self._orient_bank = None   # lazy-loaded minimap template bank

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.run_start()
        logger.info("[BOT] DD Farming Bot starting")
        logger.info(f"[BOT] Pause key  : {cfg.PAUSE_KEY}")
        logger.info(f"[BOT] Resolution : {cfg.RESOLUTION}")
        logger.info(f"[BOT] Windows    : " +
                    ", ".join(f"{k}={'ON' if s.enabled else 'OFF'}"
                              for k, s in self.slots.items()))

        if not self._shared_arduino and not self.hid.connect():
            logger.error("[BOT] Arduino not connected — aborting")
            return

        for slot in self.slots.values():
            if slot.enabled:
                slot.find_and_attach()

        consecutive_timeouts = 0
        try:
            # Allow the user to pause before the taskbar probe so they have time
            # to arrange windows/positions while the bot waits.
            if capslock.is_on():
                logger.info(f"[BOT] Paused via {cfg.PAUSE_KEY} — waiting before startup probe…")
                capslock.wait_off()
                logger.info("[BOT] Resumed — starting taskbar verification")

            # Startup checks inside try so StopBot reaches finally → notifier flushes
            self._probe_taskbar_positions()

            # Per-window mode selection + assist calibration.
            self._select_modes()

            # Determine starting active window (whichever is currently foreground)
            self._active = self._detect_foreground_slot() or self._first_alive_slot()
            if self._active is None:
                logger.error("[BOT] No enabled window found — aborting")
                raise StopBot

            logger.info(f"[BOT] Starting in window: '{self._active.title}'")

            # Independent-assist mode: two windows follow separate party leaders.
            # Handled by its own self-contained loop; never returns to this one.
            _alive = [s for s in self.slots.values() if s.enabled and s.alive]
            if (cfg.ASSIST_INDEPENDENT
                    and len(_alive) == 2
                    and all(s.targeting_mode == "assist" for s in _alive)):
                logger.info("[BOT] ASSIST_INDEPENDENT — entering split-assist loop")
                while True:
                    try:
                        capslock.raise_if_on()
                    except capslock.CapsLockPause:
                        logger.info(f"[BOT] Paused via {cfg.PAUSE_KEY}")
                        capslock.wait_off()
                        logger.info("[BOT] Resumed — continuing split-assist")
                        self._sa_recovery_mode = False
                        self._invalidate_all_caches()
                        continue
                    try:
                        self._run_split_assist()
                        break   # only reached if _run_split_assist returns normally
                    except capslock.CapsLockPause:
                        logger.info(f"[BOT] Paused via {cfg.PAUSE_KEY}")
                        capslock.wait_off()
                        logger.info("[BOT] Resumed — continuing split-assist")
                        self._sa_recovery_mode = False
                        self._invalidate_all_caches()
                        continue  # restart the outer while loop, not fall to StopBot
                raise StopBot

            while True:
                # --- pause-key check (outer loop so resume never re-opens HID) ---
                try:
                    capslock.raise_if_on()
                except capslock.CapsLockPause:
                    logger.info(f"[BOT] Paused via {cfg.PAUSE_KEY}")
                    capslock.wait_off()
                    logger.info("[BOT] Resumed — continuing cycle")
                    self._sa_recovery_mode = False
                    self._invalidate_all_caches()
                    continue

                try:
                    result = self._cycle()
                    if result == "timeout":
                        consecutive_timeouts += 1
                        if consecutive_timeouts >= 2:
                            self.tg.send(
                                f"{self._active.nickname()} waited too long "
                                f"for the mob HP / death — 2nd time in a row! Stopping."
                            )
                            raise StopBot
                    else:
                        consecutive_timeouts = 0
                except capslock.CapsLockPause:
                    logger.info(f"[BOT] Paused via {cfg.PAUSE_KEY}")
                    capslock.wait_off()
                    logger.info("[BOT] Resumed — restarting from target search")
                    self._sa_recovery_mode = False
                    self._invalidate_all_caches()
                except CycleTimeout as e:
                    logger.warn(f"[BOT] CycleTimeout: {e}")
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 2:
                        self.tg.send(
                            f"{self._active.nickname()} timed out 2nd time in a row! Stopping."
                        )
                        raise StopBot

        except StopBot:
            logger.info("[BOT] Bot stopped.")
        except KeyboardInterrupt:
            logger.info("[BOT] Interrupted by user.")
        finally:
            if not self._shared_arduino:
                self.hid.close()
            self._notifier.stop()
            self.sct.close()

    # ------------------------------------------------------------------
    # Independent assist main loop (ASSIST_INDEPENDENT = True)
    # ------------------------------------------------------------------

    def _run_split_assist(self) -> None:
        """
        Separate-group assist mode: each window follows its own party leader.

        State machine per window:
          "search"  — RMB bursts, up to ASSIST_RMB_MAX_ATTEMPTS per visit.
                      On success → "monitor".
                      After ASSIST_RMB_MAX_ATTEMPTS without target → party anchor
                      check, then switch to the other window (will retry next visit).
          "monitor" — read mob HP for up to HP_SWITCH_EVERY × HP_CHECK_INTERVAL.
                      Kill zone hit → F2 on this window → back to "search".
                      Anchor gone (HP_ANCHOR_MISS_LIMIT consecutive misses) → "search".
                      After HP_SWITCH_EVERY checks → switch window.

        Party anchor disappears → that window is disabled. One slot left →
        continues as single-window until party anchor vanishes there too → StopBot.
        """
        slots: list[WindowSlot] = [s for s in self.slots.values() if s.enabled and s.alive]
        state:      dict[str, str]  = {s.title: "search" for s in slots}
        # out_of_turn: set after mob_dead fires mid-monitor.  The next search visit
        # skips buff/death check and the full 5-check monitor — just confirms the
        # anchor is present and immediately switches to the other window.
        out_of_turn: dict[str, bool] = {s.title: False for s in slots}

        # Start on whichever window is currently active
        idx = next((i for i, s in enumerate(slots) if s is self._active), 0)

        while True:
            capslock.raise_if_on()

            # Rebuild alive list in case a slot was disabled mid-loop
            slots = [s for s in self.slots.values() if s.enabled and s.alive]
            if not slots:
                raise StopBot
            idx = idx % len(slots)
            slot = slots[idx]

            # Ensure new slots have a state entry
            if slot.title not in state:
                state[slot.title]       = "search"
                out_of_turn[slot.title] = False

            # Switch to this slot if not already active
            if slot is not self._active:
                self._switch_to(slot)

            # ----------------------------------------------------------------
            # TARGET SEARCH phase
            # ----------------------------------------------------------------
            if state[slot.title] == "search":
                found = self._target_search()
                if found:
                    self._press_attack()
                    if out_of_turn[slot.title]:
                        # Out-of-turn acquisition after mob_dead: skip buff/death
                        # check and the full monitor round — just confirm anchor is
                        # present and switch to the other window immediately.
                        out_of_turn[slot.title] = False
                        state[slot.title] = "monitor"
                        logger.info(
                            f"[BOT] [{slot.title}] Out-of-turn target confirmed"
                            f" — switching window"
                        )
                        # fall through → idx increments → switch window
                    else:
                        # Normal acquisition — buff/death check, then monitor.
                        self._check_buff_and_death()
                        state[slot.title] = "monitor"
                        # Stay on this window for HP monitoring (don't switch yet)
                        continue
                else:
                    # 2 bursts exhausted — optionally check party anchor
                    if cfg.ASSIST_REQUIRE_PARTY_ANCHOR:
                        frame = self._grab()
                        if (self._party_f.find(frame) is None
                                and not self._recheck_party_anchor()):
                            msg = (f"{slot.nickname()}: party leader not detected"
                                   f" — stopping window.")
                            logger.warn(f"[BOT] {msg}")
                            self.tg.send(msg)
                            self._on_death(slot)
                            idx = 0
                            continue
                    logger.info(
                        f"[BOT] [{slot.title}] No target after"
                        f" {cfg.ASSIST_RMB_MAX_ATTEMPTS} bursts — switching window"
                    )

            # ----------------------------------------------------------------
            # HP MONITOR phase (or fall-through after failed search → switch)
            # ----------------------------------------------------------------
            elif state[slot.title] == "monitor":
                anchor_misses = 0
                for _ in range(cfg.HP_SWITCH_EVERY):
                    capslock.raise_if_on()

                    frame   = self._grab()
                    mob_pos = self._mob_anchor.find(frame)

                    # mob_dead and bag_mob_anchor coexist in the same target frame —
                    # check every grab regardless of whether mob_pos was found.
                    if self._mob_dead_f.find(frame, silent=True) is not None:
                        logger.info(
                            f"[BOT] [{slot.title}] mob_dead detected"
                            f" — immediate search (out of turn)"
                        )
                        state[slot.title] = "search"
                        break

                    if mob_pos is None:
                        anchor_misses += 1
                        if anchor_misses >= cfg.HP_ANCHOR_MISS_LIMIT:
                            logger.info(
                                f"[BOT] [{slot.title}] bag_mob_anchor"
                                f" gone — back to search"
                            )
                            state[slot.title] = "search"
                            break
                        time.sleep(cfg.HP_CHECK_INTERVAL)
                        continue

                    anchor_misses = 0
                    p      = cfg.PROFILE
                    mob_hp = _bar_pct(frame, mob_pos[0], mob_pos[1],
                                      p["mob_bar_offset_x"], p["mob_bar_offset_y"],
                                      p["mob_bar_w"], p["mob_bar_h"], "red")

                    char_pos = self._char_anchor.find(frame)
                    if char_pos:
                        char_hp = _bar_pct(frame, char_pos[0], char_pos[1],
                                           p["char_hp_offset_x"], p["char_hp_offset_y"],
                                           p["char_hp_w"], p["char_hp_h"], "red_char")
                        char_mp = _bar_pct(frame, char_pos[0], char_pos[1],
                                           p["char_mp_offset_x"], p["char_mp_offset_y"],
                                           p["char_mp_w"], p["char_mp_h"], "blue")
                        self._handle_char_bars(char_hp, char_mp, mob_hp)

                    if mob_hp is not None:
                        logger.info(
                            f"[BOT] [{slot.title}] Mob HP {mob_hp:.1f}%"
                            + (f"  char HP {char_hp:.1f}% MP {char_mp:.1f}%"
                               if char_pos else "")
                        )
                        if mob_hp <= cfg.MOB_HP_HIGH_PCT:
                            _press(self.hid, "f2")
                            logger.info(
                                f"[BOT] F2 in '{slot.title}' (split finisher)"
                            )
                            state[slot.title] = "search"
                            break

                    time.sleep(cfg.HP_CHECK_INTERVAL)

                # mob_dead detected mid-monitor: stay on this window for an
                # immediate out-of-turn RMB burst (skip idx increment).
                if state[slot.title] == "search":
                    out_of_turn[slot.title] = True
                    continue   # skip idx increment — search fires on this window next

            # Advance to next window
            idx = (idx + 1) % len(slots)

    # ------------------------------------------------------------------
    # Full cycle
    # ------------------------------------------------------------------

    def _cycle(self) -> str:
        # In mixed mode (one nexttarget + one assist), always open the cycle
        # with the nexttarget window.  If we start in the assist window it would
        # right-click the party bar while the nexttarget window still has no mob
        # selected — producing nothing for 15 s until the assist timeout fires.
        opp = self._opposite()
        if (opp is not None
                and self._active.targeting_mode == "assist"
                and opp.targeting_mode in ("nexttarget", "nc")):
            logger.info(
                f"[BOT] Reordering cycle: nexttarget window '{opp.title}' goes first"
            )
            self._switch_to(opp)

        # ---- 1. Target search in current window ----
        opp = self._opposite()
        if opp is None and self._active.targeting_mode == "assist":
            # Single-window assist: phase-based targeting loop.
            # _single_assist_cycle() handles all RMB / F5 / healer / approach
            # logic and calls _press_attack() internally on success.
            # Each failed iteration: check buff/death + party leader presence.
            while True:
                if self._single_assist_cycle():
                    break
                capslock.raise_if_on()
                self._check_buff_and_death()
                if cfg.ASSIST_REQUIRE_PARTY_ANCHOR:
                    frame = self._grab()
                    if self._party_f.find(frame) is None and not self._recheck_party_anchor():
                        msg = (f"{self._active.nickname()}: party leader not detected"
                               f" — stopping.")
                        logger.warn(f"[BOT] {msg}")
                        self.tg.send(msg)
                        raise StopBot
                logger.info("[BOT] Assist: no target, party present — retrying")
        else:
            win1_targeted = self._target_search()
            if win1_targeted:
                self._press_attack()
            elif self._active.targeting_mode == "assist":
                # Win1 assist gave up after 2 bursts — same check as win2
                self._check_buff_and_death()
                if cfg.ASSIST_REQUIRE_PARTY_ANCHOR:
                    frame = self._grab()
                    if self._party_f.find(frame) is None and not self._recheck_party_anchor():
                        msg = (f"{self._active.nickname()}: party leader not detected"
                               f" — stopping.")
                        logger.warn(f"[BOT] {msg}")
                        self.tg.send(msg)
                        raise StopBot
                logger.info("[BOT] Win1 assist: no target, party present — trying win2")

        opp = self._opposite()

        both_assist = (opp is not None
                       and self._active.targeting_mode == "assist"
                       and opp.targeting_mode == "assist")

        if opp is not None:
            # ---- 2a. Switch, target search, attack in opposite window ----
            self._switch_to(opp)
            win2_targeted = self._target_search()
            if win2_targeted:
                self._press_attack()    # skips automatically for both-assist

            # ---- 3a. Check buffs/death in current (win2) ----
            self._check_buff_and_death()

            # If win2 assist targeting failed, optionally verify party leader is visible.
            if not win2_targeted and self._active.targeting_mode == "assist":
                if cfg.ASSIST_REQUIRE_PARTY_ANCHOR:
                    frame = self._grab()
                    if self._party_f.find(frame) is None and not self._recheck_party_anchor():
                        msg = (f"{self._active.nickname()}: party leader not detected"
                               f" — stopping.")
                        logger.warn(f"[BOT] {msg}")
                        self.tg.send(msg)
                        raise StopBot
                logger.info("[BOT] Win2 assist: no target, party present — restarting cycle")
                return "ok"   # skip kill phase entirely; no timeout count

            if not both_assist:
                # ---- 4. Switch back ----
                self._switch_to(self._opposite())   # back to win1

                # ---- 3b. Check buffs/death in win1 ----
                self._check_buff_and_death()
            # else: both-assist — stay on win2 for the kill phase.
            # win1's buff/death check is skipped; bot monitors from win2.
        else:
            # ---- 3. Single window: check buffs/death once ----
            self._check_buff_and_death()

        # ---- 5. Wait for mob HP in low range (loop on stall to pick new target) ----
        while True:
            hp_result = self._wait_low_hp()
            if hp_result == "ok":
                break
            if hp_result == "dead":
                # Mob died before reaching kill zone (party leader finished early).
                # Skip F2 + _wait_death and restart the whole cycle immediately.
                logger.info("[BOT] Mob dead early — skipping finisher, restarting cycle")
                return "ok"
            if hp_result == "timeout":
                return "timeout"
            # "stalled": restart target search.
            # For single-window assist: reset recovery state and run the full
            # phase-based cycle (_single_assist_cycle handles _press_attack internally).
            # For all other modes: use the legacy _target_search path.
            logger.info("[BOT] Stall — restarting target search")
            opp_stall = self._opposite()
            if opp_stall is None and self._active.targeting_mode == "assist":
                self._sa_recovery_mode = False
                self._single_assist_cycle()
            else:
                if self._target_search():
                    self._press_attack()
            # loop back to _wait_low_hp()

        # ---- 6. Finisher F2 ----
        _press(self.hid, "f2")
        logger.info(f"[BOT] F2 in '{self._active.title}'")
        if both_assist:
            # Both-assist: press F2 on the opposite window as well so both
            # characters contribute the finishing hit.
            opp = self._opposite()
            self._switch_to(opp)
            _press(self.hid, "f2")
            logger.info(f"[BOT] F2 in '{self._active.title}' (both-assist finisher)")

        # ---- 7. Wait for mob death ----
        ok = self._wait_death()
        if not ok:
            return "timeout"

        logger.info("[BOT] Mob dead — starting new cycle")
        return "ok"

    # ------------------------------------------------------------------
    # Assist-mode calibration
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_point_with_crosshair(window_title: str,
                                    center_x: int = 130,
                                    center_y: int = 240) -> Optional[tuple[int, int]]:
        """Show a small draggable crosshair window.

        The user moves it so the centre aligns with the target point on screen
        and presses Enter to confirm.  Escape cancels and returns None.
        Coordinates are derived from the window's own screen position — no mouse
        position reads, so anti-cheat mouse hooks are irrelevant.

        center_x / center_y set the initial screen position of the crosshair's
        centre dot (the red dot = the point that will be recorded).
        """
        import tkinter as tk

        SIZE  = 80          # outer size of the crosshair window (px)
        HALF  = SIZE // 2
        result: list[Optional[tuple[int, int]]] = [None]

        root = tk.Tk()
        root.title(window_title)
        # Place at a temporary position first, then reposition after update so
        # the actual title-bar height is known and the centre dot lands exactly
        # on (center_x, center_y).
        root.geometry(f"{SIZE}x{SIZE}+0+0")
        root.update_idletasks()
        title_bar_h = root.winfo_rooty() - root.winfo_y()
        win_x = center_x - HALF
        win_y = center_y - HALF - title_bar_h
        root.geometry(f"{SIZE}x{SIZE}+{win_x}+{win_y}")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        canvas = tk.Canvas(root, width=SIZE, height=SIZE,
                           bg="#111111", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        # Crosshair lines
        canvas.create_line(HALF, 0, HALF, SIZE, fill="#00ff00", width=1)
        canvas.create_line(0, HALF, SIZE, HALF, fill="#00ff00", width=1)
        # Centre dot
        r = 3
        canvas.create_oval(HALF - r, HALF - r, HALF + r, HALF + r,
                           fill="red", outline="")
        # Hint text
        canvas.create_text(HALF, SIZE - 7, text="Enter=OK   Esc=cancel",
                           fill="#aaaaaa", font=("Arial", 6))

        # Drag support
        _drag: dict[str, int] = {"x": 0, "y": 0}

        def _start_drag(e: tk.Event) -> None:
            _drag["x"] = e.x
            _drag["y"] = e.y

        def _do_drag(e: tk.Event) -> None:
            root.geometry(
                f"+{root.winfo_x() + e.x - _drag['x']}"
                f"+{root.winfo_y() + e.y - _drag['y']}"
            )

        def _confirm(e: tk.Event = None) -> None:
            # Use the canvas's absolute screen position so the title bar height
            # is automatically excluded — the crosshair centre is always correct.
            result[0] = (
                canvas.winfo_rootx() + HALF,
                canvas.winfo_rooty() + HALF,
            )
            root.destroy()

        def _cancel(e: tk.Event = None) -> None:
            root.destroy()

        canvas.bind("<ButtonPress-1>", _start_drag)
        canvas.bind("<B1-Motion>",     _do_drag)
        root.bind("<Return>",          _confirm)
        root.bind("<KP_Enter>",        _confirm)
        root.bind("<Escape>",          _cancel)
        # Defer focus grab until after the window is fully mapped so the OS
        # actually hands it focus without requiring a click first.
        root.after(0, lambda: (root.lift(), root.focus_force()))
        root.mainloop()
        return result[0]

    @staticmethod
    def _focus_console() -> None:
        """Bring the bot's own console window to the foreground."""
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.1)
        except Exception:
            pass

    def _select_modes(self) -> None:
        """Per-window targeting-mode selection and assist calibration.

        Phase 1 — all text prompts (no window switching):
          • targeting mode for each enabled window  (n / nc / a)
          • if both chose assist: same party or independent?

        Phase 2 — crosshair calibration (one per assist window in sequence):
          • switch to the game window, show the draggable crosshair on the
            left side of the screen (near party bars), user drags & presses Enter.
        """
        enabled = [s for s in self.slots.values() if s.enabled]

        # ── Phase 1: collect all text inputs ────────────────────────────────
        self._focus_console()
        for slot in enabled:
            while True:
                choice = input(
                    f"\n[{slot.title}]  Targeting —"
                    f"  [n]  NextTarget (F5)"
                    f"  [nc] NameClick  (Shift+click mob name)"
                    f"  [a]  Assist     (right-click party bar): "
                ).strip().lower()
                if choice in ("a", "n", "nc"):
                    break
                print("  Please type 'n', 'nc', or 'a'.")
            if choice == "a":
                slot.targeting_mode = "assist"
            elif choice == "nc":
                slot.targeting_mode = "nc"
            else:
                slot.targeting_mode = "nexttarget"
            logger.info(f"[BOT] '{slot.title}' targeting mode: {slot.targeting_mode}")

        # If both chose assist, ask party grouping
        assist_slots = [s for s in enabled if s.targeting_mode == "assist"]
        if len(enabled) == 2 and len(assist_slots) == 2:
            print()
            while True:
                choice = input(
                    "  Both windows are in Assist mode.\n"
                    "  Are they in the SAME party (synchronized kill) or"
                    " SEPARATE parties (independent)?\n"
                    "  [s]  Same party   — synchronized F2 finisher\n"
                    "  [i]  Independent  — each window follows its own leader: "
                ).strip().lower()
                if choice in ("s", "i"):
                    break
                print("  Please type 's' or 'i'.")
            cfg.ASSIST_INDEPENDENT = (choice == "i")
            mode_label = ("INDEPENDENT (separate leaders)"
                          if cfg.ASSIST_INDEPENDENT else "SYNCHRONIZED (same party)")
            logger.info(f"[BOT] Both-assist mode: {mode_label}")

        # ── Phase 2: crosshair calibration for each assist window ───────────
        for slot in assist_slots:
            print(f"\n  Switching to '{slot.title}'...")
            self._switch_to(slot)
            print(f"  A crosshair window will appear on the left side of the screen.")
            print(f"  Drag it over the party bar for '{slot.title}' and press Enter.")
            while True:
                pt = self._pick_point_with_crosshair(f"Assist — {slot.title}")
                if pt is not None:
                    break
                print("  Cancelled — try again.")
            slot.assist_point = pt
            logger.info(f"[ASSIST] '{slot.title}' calibrated → {slot.assist_point}")
            print(f"  Saved: {slot.assist_point}")

        # After calibration always return to the first enabled window so the
        # physical foreground and self._active are in sync when the bot starts.
        if assist_slots:
            first_slot = enabled[0]
            if self._active is not first_slot:
                print(f"\n  Returning to '{first_slot.title}' to start the bot...")
                self._switch_to(first_slot)

        print()

    # ------------------------------------------------------------------
    # Target search
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # NC mode helpers
    # ------------------------------------------------------------------

    def _find_mob_name_candidates(self, frame: np.ndarray) -> list[tuple[int, int]]:
        """Return (cx, cy) for every mob_name template match on screen (deduplicated)."""
        hits: list[tuple[int, int]] = []
        for tmpl, (th, tw) in self._mob_name_tmpls:
            if frame.shape[0] < th or frame.shape[1] < tw:
                continue
            res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= cfg.NC_CONFIDENCE)
            for x, y in zip(xs.tolist(), ys.tolist()):
                hits.append((x + tw // 2, y + th // 2))
        return self._nms_points(hits, cfg.NC_NMS_DIST)

    @staticmethod
    def _nms_points(pts: list[tuple[int, int]], min_dist: int) -> list[tuple[int, int]]:
        """Keep one representative point per cluster of hits within min_dist px."""
        result: list[tuple[int, int]] = []
        for pt in pts:
            if all(
                abs(pt[0] - r[0]) > min_dist or abs(pt[1] - r[1]) > min_dist
                for r in result
            ):
                result.append(pt)
        return result

    def _has_target_dot(self, frame: np.ndarray,
                        name_cx: int, name_cy: int) -> bool:
        """Return True if an in_target_red or in_target_blue dot is visible near this name.

        Checks the region:  x  in [name_cx - NC_DOT_HALF_W,  name_cx + NC_DOT_HALF_W]
                            y  in [name_cy - NC_DOT_HEIGHT//2, name_cy + NC_DOT_HEIGHT//2]
        """
        fh, fw = frame.shape[:2]
        x0 = max(0, name_cx - cfg.NC_DOT_HALF_W)
        y0 = max(0, name_cy - cfg.NC_DOT_HEIGHT // 2)
        x1 = min(fw, name_cx + cfg.NC_DOT_HALF_W)
        y1 = min(fh, y0 + cfg.NC_DOT_HEIGHT)
        sub = frame[y0:y1, x0:x1]
        if sub.size == 0:
            return False
        for tmpl in (self._dot_red_tmpl, self._dot_blue_tmpl):
            if tmpl is None:
                continue
            th, tw = tmpl.shape[:2]
            if sub.shape[0] < th or sub.shape[1] < tw:
                continue
            res = cv2.matchTemplate(sub, tmpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, _ = cv2.minMaxLoc(res)
            if score >= cfg.NC_CONFIDENCE:
                return True
        return False

    def _target_search_nc(self) -> bool:
        """NC mode target search: Shift+click the nearest unoccupied mob name.

        Loops until bag_mob_anchor is found or TARGET_NOT_FOUND_TIMEOUT expires.
        Returns True when a target is acquired; raises StopBot on timeout.
        """
        deadline = time.time() + cfg.TARGET_NOT_FOUND_TIMEOUT
        logger.info(f"[BOT] Target search (nc) in '{self._active.title}'")

        while True:
            capslock.raise_if_on()

            frame = self._grab()
            fh, fw = frame.shape[:2]
            center_x = fw // 2 - cfg.NC_CENTER_OFFSET_X
            center_y = fh // 2

            candidates = self._find_mob_name_candidates(frame)
            valid = [
                (cx, cy) for (cx, cy) in candidates
                if not self._has_target_dot(frame, cx, cy)
            ]

            if valid:
                nearest = min(valid,
                              key=lambda p: (p[0] - center_x) ** 2
                                          + (p[1] - center_y) ** 2)
                click_x = nearest[0]
                click_y = nearest[1] + cfg.NC_CLICK_BELOW_PX
                logger.info(
                    f"[BOT] [nc] {len(candidates)} name(s) found,"
                    f" {len(valid)} valid — Shift+clicking ({click_x}, {click_y})"
                )
                self.hid.move_and_shift_click(click_x, click_y)
                time.sleep(cfg.NC_WAIT_AFTER_CLICK_MS / 1000.0)

                frame2 = self._grab()
                pos = self._mob_anchor.find(frame2)
                if pos is not None:
                    logger.info(f"[BOT] [nc] bag_mob_anchor found at {pos}")
                    return True
                # anchor not found after click — try again immediately
                continue

            # No valid names on screen — wait briefly and retry
            logger.info(f"[BOT] [nc] no valid mob names on screen — waiting")
            time.sleep(cfg.NC_WAIT_NO_MOB_MS / 1000.0)

            if time.time() > deadline:
                msg = "New mobs did not appear for too long. The bot has been stopped."
                logger.warn(f"[BOT] {msg}")
                self.tg.send(msg)
                raise StopBot

    # ------------------------------------------------------------------
    # Target search (dispatcher)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Single-window assist: phase-based target acquisition + approach
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Camera orientation control
    # ------------------------------------------------------------------

    def set_camera_orient(self, deg: int) -> None:
        """Called by stream_sender when the viewer broadcasts a new camera orientation.

        Sets orient_1 to *deg*; orient_2 is always (deg + 180) % 360.
        The template bank does not depend on the angle and is NOT invalidated.
        """
        self._camera_orient_1 = int(deg) % 360
        logger.info(f"[BOT] Camera orientations updated: "
                    f"{self._camera_orient_1}deg / "
                    f"{(self._camera_orient_1 + 180) % 360}deg")

    def _get_orient_bank(self):
        """Lazy-load and return the minimap template bank."""
        if self._orient_bank is None:
            import minimap_orient as _mo
            self._orient_bank = _mo.build_template_bank()
            logger.info("[BOT] Minimap orientation template bank built")
        return self._orient_bank

    def _rotate_camera_smart(self) -> None:
        """Switch the camera to the next allowed orientation.

        When _camera_orient_1 is set (via viewer UI):
          1. Detect the current minimap-arrow angle.
          2. Choose the target: whichever of {orient_1, orient_2} is farther away.
          3. Iteratively drag + re-detect until within ±CAMERA_ORIENT_TOL_DEG.

        Falls back to the blind SA_CAMERA_ROTATE_DX drag if orientation is not
        configured or if arrow detection fails.
        """
        if self._camera_orient_1 is None:
            logger.info("[BOT] SA: camera orient not configured — blind 180 drag")
            self.hid.drag_camera(cfg.SA_CAMERA_ROTATE_DX)
            return

        import minimap_orient as _mo

        orient_1 = self._camera_orient_1
        orient_2 = (orient_1 + 180) % 360
        bank     = self._get_orient_bank()

        def _detect() -> Optional[int]:
            try:
                bgr     = _mo.grab_arrow_bgr(self.sct, _mo.ARROW_REGION)
                gray_up = _mo._upscale_gray(bgr)
                ang, sc = _mo.match_angle(gray_up, bank)
                logger.info(f"[BOT] SA: arrow={ang}deg score={sc:.3f}")
                return ang
            except Exception as exc:
                logger.warn(f"[BOT] SA: arrow detect error — {exc}")
                return None

        cur = _detect()
        if cur is None:
            logger.warn("[BOT] SA: arrow detection failed — blind 180 drag")
            self.hid.drag_camera(cfg.SA_CAMERA_ROTATE_DX)
            return

        # Choose the orientation that is farther from the current angle
        err_1 = min((cur - orient_1) % 360, (orient_1 - cur) % 360)
        err_2 = min((cur - orient_2) % 360, (orient_2 - cur) % 360)
        target = orient_2 if err_1 <= err_2 else orient_1
        logger.info(f"[BOT] SA: camera {cur}deg -> target {target}deg "
                    f"(orient1={orient_1}, orient2={orient_2})")

        for iteration in range(1, cfg.CAMERA_ORIENT_MAX_ITER + 1):
            delta = (target - cur + 180) % 360 - 180   # signed, (-180, +180]
            if abs(delta) <= cfg.CAMERA_ORIENT_TOL_DEG:
                logger.info(f"[BOT] SA: camera aligned in {iteration - 1} step(s), "
                            f"final={cur}deg err={delta:+d}deg")
                break
            dx = round(delta * _mo.PIXELS_PER_360 / 360)
            logger.info(f"[BOT] SA: camera iter {iteration}: "
                        f"delta={delta:+d}deg dx={dx:+d}px")
            capslock.raise_if_on()
            self.hid.drag_camera(dx, settle_s=cfg.CAMERA_ORIENT_SETTLE_S)
            new = _detect()
            if new is None:
                break
            cur = new
        else:
            logger.warn(f"[BOT] SA: camera align hit max iterations, final={cur}deg")

    def _sa_phase4(self, frame: np.ndarray) -> bool:
        """in_target_blue approach + attack (phase 4).

        Called once bag_mob_anchor is confirmed on *frame*.

        Two modes controlled by SA_ATTACK_BEFORE_APPROACH:
          True  — press ASSIST_ATTACK_COUNT immediately, then approach.
          False — detect distance first; attack immediately only if already
                  within SA_APPROACH_SKIP_PX, otherwise approach first and
                  attack only once within SA_APPROACH_STOP_PX.
        """
        fh, fw = frame.shape[:2]
        sc_x, sc_y = fw // 2, fh // 2

        def _dot_center_from(f: np.ndarray) -> Optional[Tuple[int, int]]:
            """Detect in_target_blue OR in_target_red — both treated equally."""
            dots = _sa_find_blue_dots(f, self._dot_blue_tmpl,
                                      conf=cfg.NC_CONFIDENCE,
                                      nms_dist=cfg.NC_NMS_DIST)
            dots += _sa_find_blue_dots(f, self._dot_red_tmpl,
                                       conf=cfg.NC_CONFIDENCE,
                                       nms_dist=cfg.NC_NMS_DIST)
            if len(dots) > 1:
                dots.sort(key=lambda p: p[0])
                dots = _sa_nms(dots, cfg.NC_NMS_DIST)
            return _sa_blue_pair_center(dots)

        # Step 1 — optional immediate attack depending on mode.
        if cfg.SA_ATTACK_BEFORE_APPROACH:
            if random.random() < cfg.SA_PRE_ATTACK_LONG_CHANCE:
                pre_delay = random.uniform(cfg.SA_PRE_ATTACK_DELAY_LONG_MIN,
                                           cfg.SA_PRE_ATTACK_DELAY_LONG_MAX)
                logger.info(f"[BOT] SA: pre-attack long delay {pre_delay:.2f}s (10% roll)")
            else:
                pre_delay = random.uniform(cfg.SA_PRE_ATTACK_DELAY_MIN,
                                           cfg.SA_PRE_ATTACK_DELAY_MAX)
                logger.info(f"[BOT] SA: pre-attack delay {pre_delay:.2f}s")
            capslock.interruptible_sleep(pre_delay)
            capslock.raise_if_on()
            logger.info("[BOT] SA: mob confirmed — immediate attack press")
            self._press_attack()
            capslock.raise_if_on()

        frame = self._grab()
        pair_center = _dot_center_from(frame)

        if pair_center is None:
            logger.info("[BOT] SA: in_target_blue/red not found — rotating camera")
            self._rotate_camera_smart()
            capslock.raise_if_on()
            frame = self._grab()
            pair_center = _dot_center_from(frame)
            if pair_center is None:
                logger.info("[BOT] SA: in_target_blue/red still not found — normal attack")
                self._press_attack()
                return True

        dist = math.hypot(pair_center[0] - sc_x, pair_center[1] - sc_y)
        logger.info(f"[BOT] SA: in_target_blue/red at {pair_center}, dist={dist:.0f}px")

        if dist < cfg.SA_APPROACH_SKIP_PX:
            logger.info(f"[BOT] SA: already within SA_APPROACH_SKIP_PX"
                        f" ({dist:.0f}px) — skipping ground clicks, attacking")
            self._press_attack()
            return True

        # Approach via double-clicks in corridor.
        # Timing is distance-driven: after each double-click the bot polls every
        # SA_APPROACH_POLL_MS ms and fires the next click once:
        #   d_now  ≤  d_remaining + SA_NEXT_CLICK_LEAD_PX
        # where d_remaining = pixel distance from the click point to the mob's
        # dot centre at the moment of clicking, and d_now = current distance
        # from screen centre to the mob's dot centre.
        pt     = self._active.assist_point
        poll_s = cfg.SA_APPROACH_POLL_MS / 1000.0
        reached = False   # set True when dist < SA_APPROACH_STOP_PX

        # Choose the click budget randomly upfront so different mobs get a
        # different number of movement clicks (1–SA_APPROACH_MAX_DCLK).
        n_clicks = random.randint(cfg.SA_APPROACH_MIN_DCLK,
                                  cfg.SA_APPROACH_MAX_DCLK)
        logger.info(f"[BOT] SA: approach budget = {n_clicks} double-click(s)")

        for dclk_i in range(n_clicks):
            capslock.raise_if_on()

            if dclk_i == 0:
                # Click #1: reuse the pair_center already computed above — the mob
                # hasn't moved and re-grabbing + re-running three matchTemplate calls
                # only adds 50–150 ms of unnecessary latency before the first click.
                # bag_mob_anchor was confirmed by the caller, so no extra check here.
                pass
            else:
                # Clicks #2+: character is now moving; grab a fresh frame.
                frame = self._grab()

                # Mob vanished mid-approach: RMB reacquire + attack, stop clicking.
                self._mob_anchor.invalidate()
                if self._mob_anchor.find(frame) is None:
                    logger.info(f"[BOT] SA: bag_mob_anchor lost at approach click"
                                f" #{dclk_i + 1} — RMB reacquire + attack, no more clicks")
                    if pt is not None:
                        self.hid.move_and_right_click(pt[0], pt[1], wait_after=0)
                        capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
                    else:
                        logger.warn("[BOT] SA: assist_point not set — skipping RMB reacquire")
                    self._press_attack()
                    return True

                pair_center = _dot_center_from(frame)
                if pair_center is None:
                    logger.info("[BOT] SA: in_target_blue/red lost before approach"
                                f" click #{dclk_i + 1} — attacking")
                    break

            dist = math.hypot(pair_center[0] - sc_x, pair_center[1] - sc_y)
            if dist < cfg.SA_APPROACH_STOP_PX:
                logger.info(f"[BOT] SA: within SA_APPROACH_STOP_PX before click"
                            f" #{dclk_i + 1} (dist={dist:.0f}px) — attacking")
                reached = True
                break

            # Minimum distance: first click ≥ SA_FIRST_CLICK_MIN_PX from screen
            # centre; subsequent clicks ≥ SA_NEXT_CLICK_MIN_PX.  This prevents
            # placing a click so close that the character arrives before the
            # next poll cycle fires.
            min_dist = (cfg.SA_FIRST_CLICK_MIN_PX if dclk_i == 0
                        else cfg.SA_NEXT_CLICK_MIN_PX)

            # Perspective correction: shift the corridor target downward by an
            # amount proportional to how far the mob is from the 12/6 o'clock
            # axis.  abs(ux) = 0 at vertical, 1 at horizontal (3/9 o'clock).
            raw_dx   = pair_center[0] - sc_x
            raw_dy   = pair_center[1] - sc_y
            raw_len  = math.hypot(raw_dx, raw_dy) or 1.0
            h_factor = abs(raw_dx) / raw_len
            down_px  = int(h_factor * cfg.SA_APPROACH_DOWN_OFFSET_MAX)
            eff_ty   = pair_center[1] + down_px

            click_pt = _sa_corridor_point(sc_x, sc_y,
                                          pair_center[0], eff_ty,
                                          cfg.SA_CORRIDOR_W // 2,
                                          min_dist_px=min_dist)

            # d_remaining: distance from click point to mob dot centre at the
            # moment of clicking.  The poll loop fires the next click once
            # d_now ≤ d_remaining + LEAD_PX (character nearly at click dest).
            d_remaining = math.hypot(click_pt[0] - pair_center[0],
                                     click_pt[1] - pair_center[1])

            logger.info(f"[BOT] SA: approach double-click #{dclk_i + 1}"
                        f" at {click_pt}, dist={dist:.0f}px,"
                        f" d_remaining={d_remaining:.0f}px,"
                        f" h_factor={h_factor:.2f}, down={down_px}px")
            self.hid.double_click_at(click_pt[0], click_pt[1])

            # Poll until the character arrives or mob vanishes.
            # Safety cap: random 5–8 s to avoid infinite loops.
            # Use a wall-clock deadline so grab/matchTemplate overhead
            # does not silently extend the wait beyond the cap.
            max_wait_ms  = random.randint(cfg.SA_APPROACH_MAX_WAIT_MIN_MS,
                                          cfg.SA_APPROACH_MAX_WAIT_MAX_MS)
            click_deadline = time.perf_counter() + max_wait_ms / 1000.0
            while time.perf_counter() < click_deadline:
                capslock.interruptible_sleep(poll_s)
                capslock.raise_if_on()

                frame = self._grab()
                self._mob_anchor.invalidate()
                if self._mob_anchor.find(frame) is None:
                    logger.info(f"[BOT] SA: bag_mob_anchor lost during approach"
                                f" after click #{dclk_i + 1}"
                                " — RMB reacquire + attack, no more clicks")
                    if pt is not None:
                        self.hid.move_and_right_click(pt[0], pt[1], wait_after=0)
                        capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
                    else:
                        logger.warn("[BOT] SA: assist_point not set"
                                    " — skipping RMB reacquire")
                    self._press_attack()
                    return True

                pc = _dot_center_from(frame)
                if pc is not None:
                    d_now = math.hypot(pc[0] - sc_x, pc[1] - sc_y)
                    if d_now < cfg.SA_APPROACH_STOP_PX:
                        logger.info(f"[BOT] SA: within SA_APPROACH_STOP_PX during"
                                    f" approach after click #{dclk_i + 1}"
                                    f" (dist={d_now:.0f}px)")
                        reached = True
                        break
                    if d_now <= d_remaining + cfg.SA_NEXT_CLICK_LEAD_PX:
                        logger.info(f"[BOT] SA: next-click trigger after"
                                    f" #{dclk_i + 1} (d_now={d_now:.0f}"
                                    f" ≤ d_rem={d_remaining:.0f}"
                                    f" + lead={cfg.SA_NEXT_CLICK_LEAD_PX})")
                        break
            else:
                logger.info(f"[BOT] SA: max wait ({max_wait_ms} ms)"
                            f" reached after click #{dclk_i + 1} — proceeding")

            if reached:
                break

        # ---- Final fallback if SA_APPROACH_PX not yet reached ---------------
        if not reached:
            frame       = self._grab()
            pc_final    = _dot_center_from(frame)
            if pc_final is not None:
                half  = cfg.SA_APPROACH_FINAL_AREA // 2
                ex_hw = cfg.SA_APPROACH_FINAL_EXCL_W // 2
                px, py = pc_final
                # Click area: SA_APPROACH_FINAL_AREA × SA_APPROACH_FINAL_AREA
                # box whose top edge is at the dot centre (i.e. fully below it).
                # Exclusion: SA_APPROACH_FINAL_EXCL_W × SA_APPROACH_FINAL_EXCL_H
                # strip at the top of the area, directly around the dot centre.
                for _ in range(60):
                    fx = random.randint(px - half, px + half)
                    fy = random.randint(py, py + cfg.SA_APPROACH_FINAL_AREA)
                    if (abs(fx - px) <= ex_hw
                            and fy <= py + cfg.SA_APPROACH_FINAL_EXCL_H):
                        continue
                    break
                else:
                    fx, fy = px, py + half   # safe fallback point
                logger.info(f"[BOT] SA: final fallback double-click at ({fx},{fy})")
                self.hid.double_click_at(fx, fy)

                # Wait SA_APPROACH_FINAL_WAIT_MIN/MAX_MS, polling for SA_APPROACH_STOP_PX.
                # Wall-clock deadline prevents grab/matchTemplate overhead from
                # silently multiplying the actual wait duration.
                wait_s         = random.uniform(
                    cfg.SA_APPROACH_FINAL_WAIT_MIN_MS / 1000.0,
                    cfg.SA_APPROACH_FINAL_WAIT_MAX_MS / 1000.0)
                final_deadline = time.perf_counter() + wait_s
                while time.perf_counter() < final_deadline:
                    capslock.interruptible_sleep(poll_s)
                    capslock.raise_if_on()
                    frame = self._grab()
                    pc = _dot_center_from(frame)
                    if pc is not None:
                        d = math.hypot(pc[0] - sc_x, pc[1] - sc_y)
                        if d < cfg.SA_APPROACH_STOP_PX:
                            logger.info(f"[BOT] SA: within SA_APPROACH_STOP_PX"
                                        f" during final wait (dist={d:.0f}px)")
                            break
                else:
                    logger.info("[BOT] SA: final wait elapsed without reaching"
                                " SA_APPROACH_STOP_PX — attacking anyway")
            else:
                logger.info("[BOT] SA: dots lost before final fallback click")

        self._press_attack()
        return True

    def _sa_f5_loop(self) -> bool:
        """Recovery F5 loop: press F5, wait SA_F5_WAIT_MS, check bag_mob_anchor.

        When bag_mob_anchor is not found, runs buff/death and party-anchor checks
        and then sleeps for the remainder of SA_F5_LOOP_INTERVAL_S before the
        next F5 press.  CapsLock is honoured on every iteration.

        Loops until a target is found, then calls _sa_phase4() and returns True.
        """
        logger.info("[BOT] SA: entering F5 recovery loop"
                    f" (interval {cfg.SA_F5_LOOP_INTERVAL_S:.0f}s)")
        f5_count = 0
        while True:
            capslock.raise_if_on()
            f5_count += 1
            _press(self.hid, "f5")
            capslock.interruptible_sleep(cfg.SA_F5_WAIT_MS / 1000.0)
            frame = self._grab()
            self._mob_anchor.invalidate()
            if self._mob_anchor.find(frame) is not None:
                logger.info(f"[BOT] SA F5 loop: target found after {f5_count} F5(s)")
                return self._sa_phase4(frame)

            # Not found — run safety checks, then fill the rest of the interval.
            t_checks = time.time()

            # 1. Buff + death check
            capslock.raise_if_on()
            self._check_buff_and_death()

            # 2. Party-leader check
            capslock.raise_if_on()
            if cfg.ASSIST_REQUIRE_PARTY_ANCHOR:
                frame = self._grab()
                if self._party_f.find(frame) is None and not self._recheck_party_anchor():
                    msg = (f"{self._active.nickname()}: party leader not detected"
                           f" — stopping.")
                    logger.warn(f"[BOT] {msg}")
                    self.tg.send(msg)
                    raise StopBot

            # 3. Disconnect check
            capslock.raise_if_on()
            frame = self._grab()
            if self._disconnect_f.find(frame) is not None:
                nick = self._active.nickname()
                msg  = f"{cfg.PC_NUMBER}: {nick} — disconnect screen detected!"
                logger.warn(f"[BOT] {msg}")
                self.tg.send(msg)
                raise StopBot

            # Sleep the remaining portion of the configured interval
            capslock.raise_if_on()
            elapsed = time.time() - t_checks
            remaining = cfg.SA_F5_LOOP_INTERVAL_S - elapsed
            if remaining > 0:
                capslock.interruptible_sleep(remaining)

    def _sa_phase3_rmb_f5_loop(self, frame: np.ndarray) -> bool:
        """RMB-first F5 loop used inside Phase-3 recovery.

        Per iteration:
          1. RMB at screen centre → delay SA_RMB_WAIT_MS → check bag_mob_anchor.
             Found → clear _sa_recovery_mode, _sa_phase4(), return True.
          2. x1 Esc → x1 F5 → delay SA_F5_WAIT_MS → check + full safety checks.
             Found via F5 → kill (_sa_phase4) → F5-kill sub-loop until F5 yields
                            no target → sleep SA_HEALER_PRE_DELAY_MAX → return False.
             Not found    → sleep SA_HEALER_POST_PAUSE_MIN → repeat from step 1.

        Returns True  – RMB acquisition succeeded; recovery mode cleared; phase4 called.
        Returns False – F5 kill-chain exhausted; caller should restart healer-area clicks.
        """
        pt = self._active.assist_point
        iteration = 0

        while True:
            capslock.raise_if_on()
            iteration += 1
            logger.info(f"[BOT] SA phase-3 RMB+F5 iter {iteration}")

            # --- Step 1: RMB at crosshair (assist_point) -------------------------
            if pt is not None:
                self.hid.move_and_right_click(pt[0], pt[1], wait_after=0)
            else:
                logger.warn("[BOT] SA phase-3: assist_point not set — skipping RMB")
            capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
            frame = self._grab()
            self._mob_anchor.invalidate()

            if self._mob_anchor.find(frame) is not None:
                logger.info("[BOT] SA phase-3: target via RMB → exit recovery")
                self._sa_recovery_mode = False
                self._sa_phase4(frame)
                return True

            # --- Step 2: Esc + F5 ------------------------------------------------
            capslock.raise_if_on()
            _press(self.hid, "esc")
            _press(self.hid, "f5")
            capslock.interruptible_sleep(cfg.SA_F5_WAIT_MS / 1000.0)
            frame = self._grab()
            self._mob_anchor.invalidate()
            found_via_f5 = self._mob_anchor.find(frame) is not None

            # Safety checks (always run)
            capslock.raise_if_on()
            self._check_buff_and_death()

            capslock.raise_if_on()
            if cfg.ASSIST_REQUIRE_PARTY_ANCHOR:
                chk = self._grab()
                if (self._party_f.find(chk) is None
                        and not self._recheck_party_anchor()):
                    msg = (f"{self._active.nickname()}: party leader not"
                           " detected — stopping.")
                    logger.warn(f"[BOT] {msg}")
                    self.tg.send(msg)
                    raise StopBot

            capslock.raise_if_on()
            chk = self._grab()
            if self._disconnect_f.find(chk) is not None:
                nick = self._active.nickname()
                msg  = f"{cfg.PC_NUMBER}: {nick} — disconnect screen detected!"
                logger.warn(f"[BOT] {msg}")
                self.tg.send(msg)
                raise StopBot

            if found_via_f5:
                logger.info("[BOT] SA phase-3: target via F5 — killing")

                def _do_kill_cycle(label: str) -> bool:
                    """Attack + full kill cycle for one mob in the F5 chain.

                    Mirrors _cycle()'s flow: phase4 → _wait_low_hp → F2 →
                    _wait_death.  Returns False if a timeout breaks the chain
                    so the caller can exit the sub-loop early.
                    """
                    self._sa_phase4(frame)
                    hp_result = self._wait_low_hp()
                    if hp_result == "dead":
                        logger.info(f"[BOT] SA phase-3 {label}: mob died early"
                                    " — skipping finisher")
                        return True
                    if hp_result == "timeout":
                        logger.info(f"[BOT] SA phase-3 {label}: HP wait timeout"
                                    " — breaking F5 chain")
                        return False
                    # "ok" or "stalled" — press finisher and wait for death
                    _press(self.hid, "f2")
                    logger.info(f"[BOT] F2 in '{self._active.title}'")
                    self._wait_death()
                    return True

                if not _do_kill_cycle("kill #1"):
                    pre_delay = random.uniform(cfg.SA_HEALER_PRE_DELAY_MIN,
                                               cfg.SA_HEALER_PRE_DELAY_MAX)
                    capslock.interruptible_sleep(pre_delay)
                    return False

                # F5-kill sub-loop: keep pressing F5 until no more targets
                kill_n = 1
                while True:
                    capslock.raise_if_on()
                    _press(self.hid, "f5")
                    capslock.interruptible_sleep(cfg.SA_F5_WAIT_MS / 1000.0)
                    frame = self._grab()
                    self._mob_anchor.invalidate()
                    if self._mob_anchor.find(frame) is not None:
                        kill_n += 1
                        logger.info(f"[BOT] SA phase-3: F5 kill #{kill_n}")
                        if not _do_kill_cycle(f"kill #{kill_n}"):
                            break
                    else:
                        logger.info(
                            f"[BOT] SA phase-3: F5 chain done after {kill_n} kill(s)")
                        break

                # Wait before returning to healer stage
                pre_delay = random.uniform(cfg.SA_HEALER_PRE_DELAY_MIN,
                                           cfg.SA_HEALER_PRE_DELAY_MAX)
                logger.info(
                    f"[BOT] SA phase-3: pre-healer wait {pre_delay:.1f}s")
                capslock.interruptible_sleep(pre_delay)
                return False

            # Neither RMB nor F5 found target — wait and loop
            capslock.raise_if_on()
            logger.info(
                f"[BOT] SA phase-3: no target, waiting"
                f" {cfg.SA_HEALER_POST_PAUSE_MIN:.1f}s")
            capslock.interruptible_sleep(cfg.SA_HEALER_POST_PAUSE_MIN)

    def _sa_center_fallback(self, frame: np.ndarray) -> bool:
        """No-healer fallback: 0–3 s delay, 1–2 centred clicks, then phase-3 RMB+F5 loop.

        Click area  : SA_FALLBACK_CLICK_AREA × SA_FALLBACK_CLICK_AREA centred
                      on the screen.
        1st click   : anywhere in the area except the SA_FALLBACK_EXCL_W ×
                      SA_FALLBACK_EXCL_H central exclusion zone.
        2nd click   : SA_FALLBACK_CLICK_PROX_MIN..MAX px from the first click,
                      still within the area; preceded by a 0..SA_FALLBACK_CLICK2_GAP_MAX ms gap.
        """
        fh, fw = frame.shape[:2]
        sc_x, sc_y = fw // 2, fh // 2
        half  = cfg.SA_FALLBACK_CLICK_AREA // 2
        ex_hw = cfg.SA_FALLBACK_EXCL_W  // 2   # exclusion half-width
        ex_hh = cfg.SA_FALLBACK_EXCL_H  // 2   # exclusion half-height

        # ---- Pre-delay ---------------------------------------------------------
        delay = random.uniform(cfg.SA_FALLBACK_DELAY_MIN, cfg.SA_FALLBACK_DELAY_MAX)
        logger.info(f"[BOT] SA fallback: pre-delay {delay:.1f}s")
        capslock.interruptible_sleep(delay)

        # ---- First click (avoid centre exclusion zone) -------------------------
        capslock.raise_if_on()
        for _ in range(50):
            rx = sc_x + random.randint(-half, half)
            ry = sc_y + random.randint(-half, half)
            if abs(rx - sc_x) > ex_hw or abs(ry - sc_y) > ex_hh:
                break   # outside exclusion zone
        logger.info(f"[BOT] SA fallback: click 1 at ({rx},{ry})")
        self.hid.move_and_click(rx, ry)
        first = (rx, ry)

        # ---- Optional second click (50–100 px from first, within area) ---------
        if random.random() < 0.5:    # 50 % chance of a second click
            capslock.raise_if_on()
            capslock.interruptible_sleep(
                random.uniform(0, cfg.SA_FALLBACK_CLICK2_GAP_MAX / 1000.0))
            for _ in range(50):
                angle = random.uniform(0, 2 * math.pi)
                dist  = random.uniform(cfg.SA_FALLBACK_CLICK_PROX_MIN,
                                       cfg.SA_FALLBACK_CLICK_PROX_MAX)
                cx = int(first[0] + math.cos(angle) * dist)
                cy = int(first[1] + math.sin(angle) * dist)
                if abs(cx - sc_x) <= half and abs(cy - sc_y) <= half:
                    break   # within area
            logger.info(f"[BOT] SA fallback: click 2 at ({cx},{cy})")
            self.hid.move_and_click(cx, cy)

        capslock.raise_if_on()
        return self._sa_phase3_rmb_f5_loop(frame)

    def _sa_healer_area_then_f5(self, frame: np.ndarray) -> bool:
        """Phase-3 outer loop: healer-area clicks → _sa_phase3_rmb_f5_loop.

        Each pass:
          1. Find healer_farm_anchor (rotate camera 180° if not found).
          2a. Still not found → _sa_center_fallback (centred clicks +
              _sa_phase3_rmb_f5_loop).
          2b. Found → 0–SA_HEALER_PRE_DELAY_MAX s delay → 1–3 proximity-
              constrained clicks in SA_HEALER_CLICK_AREA → SA_HEALER_POST_PAUSE
              → _sa_phase3_rmb_f5_loop.
          If _sa_phase3_rmb_f5_loop returns True  → return True (recovery exited
              via RMB).
          If _sa_phase3_rmb_f5_loop returns False → grab fresh frame and loop
              back to step 1 (redo healer-area clicks).
        """
        def _find_healer(f):
            return (self._healer_anchor.find(f)
                    if self._healer_anchor is not None else None)

        def _healer_bounds(hp: Tuple[int, int], f: np.ndarray):
            h   = cfg.SA_HEALER_CLICK_AREA
            hh  = h // 2
            above = hp[1] < f.shape[0] // 2
            xn = hp[0] - hh
            xx = hp[0] + hh
            if above:
                # Anchor in upper half → click area entirely below the anchor.
                # Exclusion zone blocks the strip directly beneath the icon.
                yn   = hp[1]
                yx   = hp[1] + h
                ehw  = cfg.SA_HEALER_EXCL_W // 2
                ebot = hp[1] + cfg.SA_HEALER_EXCL_H
            else:
                # Anchor in lower half → click area entirely above the anchor.
                # No exclusion zone needed (clicks never overlap the icon).
                yn   = hp[1] - h
                yx   = hp[1]
                ehw  = 0
                ebot = hp[1]
            return xn, xx, yn, yx, ehw, ebot

        def _pick_healer_pt(xn, xx, yn, yx, ehw, ebot, hp,
                            prev_pt, require_prox: bool):
            for _ in range(60):
                cx = random.randint(xn, xx)
                cy = random.randint(yn, yx)
                if abs(cx - hp[0]) <= ehw and hp[1] <= cy <= ebot:
                    continue
                if require_prox and prev_pt is not None:
                    d = math.hypot(cx - prev_pt[0], cy - prev_pt[1])
                    if not (cfg.SA_HEALER_CLICK_PROX_MIN <= d
                            <= cfg.SA_HEALER_CLICK_PROX_MAX):
                        continue
                return cx, cy
            return hp

        while True:
            capslock.raise_if_on()

            # Invalidate cache so a fresh detection is performed each pass.
            if self._healer_anchor is not None:
                self._healer_anchor.invalidate()

            healer_pos = _find_healer(frame)
            if healer_pos is None:
                logger.info(
                    "[BOT] SA phase-3: healer anchor not found — rotating camera")
                self._rotate_camera_smart()
                capslock.raise_if_on()
                frame = self._grab()
                if self._healer_anchor is not None:
                    self._healer_anchor.invalidate()
                healer_pos = _find_healer(frame)
                if healer_pos is None:
                    logger.info(
                        "[BOT] SA phase-3: healer still not found — centre fallback")
                    result = self._sa_center_fallback(frame)
                    if result:
                        return True
                    # _sa_phase3_rmb_f5_loop already slept SA_HEALER_PRE_DELAY_MAX
                    frame = self._grab()
                    continue

            # ---- Healer anchor found -------------------------------------------
            pre_delay = random.uniform(cfg.SA_HEALER_PRE_DELAY_MIN,
                                       cfg.SA_HEALER_PRE_DELAY_MAX)
            logger.info(f"[BOT] SA phase-3: healer at {healer_pos},"
                        f" pre-delay {pre_delay:.1f}s")
            capslock.interruptible_sleep(pre_delay)

            clicks = random.randint(1, 2)
            logger.info(f"[BOT] SA phase-3: {clicks} click(s) in"
                        f" {cfg.SA_HEALER_CLICK_AREA}px area")

            def _do_extra_click(click_n: int, prev_pt: Tuple[int, int],
                                prev_hp: Tuple[int, int],
                                prev_frame: np.ndarray,
                                gap_max_s: float) -> Tuple[int, int]:
                capslock.interruptible_sleep(random.uniform(0, gap_max_s))
                capslock.raise_if_on()
                f = self._grab()
                self._healer_anchor.invalidate()
                hp = _find_healer(f)
                if hp is None:
                    hp = prev_hp
                    f  = prev_frame
                    logger.info(
                        f"[BOT] SA phase-3: healer anchor lost before click"
                        f" {click_n} — reusing previous position")
                xn, xx, yn, yx, ehw, ebot = _healer_bounds(hp, f)
                pt = _pick_healer_pt(xn, xx, yn, yx, ehw, ebot,
                                     hp, prev_pt, require_prox=True)
                if pt == hp:
                    logger.info(
                        f"[BOT] SA phase-3: proximity relaxed for click {click_n}"
                        " (area too far from previous click)")
                    pt = _pick_healer_pt(xn, xx, yn, yx, ehw, ebot,
                                         hp, None, require_prox=False)
                logger.info(f"[BOT] SA phase-3: click {click_n}/{clicks} at {pt}"
                            f"  [bounds x:{xn}..{xx} y:{yn}..{yx}]")
                self.hid.move_and_click(pt[0], pt[1])
                return pt

            # Click 1
            capslock.raise_if_on()
            xn1, xx1, yn1, yx1, ehw1, ebot1 = _healer_bounds(healer_pos, frame)
            rx, ry = _pick_healer_pt(xn1, xx1, yn1, yx1, ehw1, ebot1,
                                     healer_pos, None, require_prox=False)
            logger.info(f"[BOT] SA phase-3: click 1/{clicks} at ({rx},{ry})"
                        f"  [bounds x:{xn1}..{xx1} y:{yn1}..{yx1}]")
            self.hid.move_and_click(rx, ry)
            prev1 = (rx, ry)

            if clicks >= 2:
                prev2 = _do_extra_click(2, prev1, healer_pos, frame, gap_max_s=2.0)
            if clicks >= 3:
                _do_extra_click(3, prev2, healer_pos, frame, gap_max_s=1.0)

            # Post-pause
            post_pause = random.uniform(cfg.SA_HEALER_POST_PAUSE_MIN,
                                        cfg.SA_HEALER_POST_PAUSE_MAX)
            logger.info(f"[BOT] SA phase-3: post-click pause {post_pause:.1f}s")
            capslock.interruptible_sleep(post_pause)

            capslock.raise_if_on()
            result = self._sa_phase3_rmb_f5_loop(frame)
            if result:
                return True
            # F5 chain exhausted — grab fresh frame and redo healer clicks
            frame = self._grab()

    def _single_assist_cycle(self) -> bool:
        """Phase-based target search + in_target_blue approach for single-window assist.

        Normal mode  (self._sa_recovery_mode == False):
          Phase 1: SA_RMB_ATTEMPTS RMB clicks at assist_point.
          Phase 2: SA_F5_ATTEMPTS  F5 presses.
          If all fail → set _sa_recovery_mode = True → healer-area recovery.

        Recovery mode (self._sa_recovery_mode == True):
          1 RMB click + check.
          Found  → clear _sa_recovery_mode → phase 4 → return True.
          Not found → stay in recovery → healer-area recovery.

        Healer-area recovery:
          pre-delay → proximity clicks in SA_HEALER_CLICK_AREA →
          post-pause → infinite F5 loop until target found →
          phase 4 → return True.
          (if healer anchor not visible: rotate camera 180° → return False)

        Returns True  → target acquired and _press_attack() called.
        Returns False → no target this iteration; outer loop should retry.
        """
        pt = self._active.assist_point

        if not self._sa_recovery_mode:
            # ---- Phase 1: SA_RMB_ATTEMPTS RMB clicks ---------------------------
            # mob_dead visible after an RMB is not treated as a stale-target
            # condition any more; the attempt is always counted so the phase
            # advances at a predictable pace.
            for rmb_i in range(1, cfg.SA_RMB_ATTEMPTS + 1):
                capslock.raise_if_on()
                if pt is not None:
                    self.hid.move_and_right_click(pt[0], pt[1], wait_after=0)
                else:
                    logger.warn("[BOT] SA: assist_point not set — skipping RMB")
                capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
                frame = self._grab()
                logger.info(
                    f"[BOT] SA phase-1 RMB #{rmb_i}/{cfg.SA_RMB_ATTEMPTS}")
                self._mob_anchor.invalidate()
                if self._mob_anchor.find(frame) is not None:
                    self._mob_dead_f.invalidate()
                    if self._mob_dead_f.find(frame, silent=True) is not None:
                        logger.info("[BOT] SA phase-1: bag_mob_anchor + mob_dead"
                                    " — dead target, not counting as live")
                    else:
                        logger.info("[BOT] SA: bag_mob_anchor found after RMB")
                        return self._sa_phase4(frame)

            # ---- Phase 2: SA_F5_ATTEMPTS F5 presses ----------------------------
            for i in range(cfg.SA_F5_ATTEMPTS):
                capslock.raise_if_on()
                logger.info(f"[BOT] SA phase-2 F5 #{i + 1}/{cfg.SA_F5_ATTEMPTS}")
                _press(self.hid, "f5")
                capslock.interruptible_sleep(cfg.SA_F5_WAIT_MS / 1000.0)
                frame = self._grab()
                self._mob_anchor.invalidate()
                if self._mob_anchor.find(frame) is not None:
                    self._mob_dead_f.invalidate()
                    if self._mob_dead_f.find(frame, silent=True) is not None:
                        logger.info("[BOT] SA phase-2: bag_mob_anchor + mob_dead"
                                    " — dead target, not counting as live")
                    else:
                        logger.info("[BOT] SA: bag_mob_anchor found after F5")
                        return self._sa_phase4(frame)

            # All normal attempts exhausted → enter recovery
            logger.info("[BOT] SA: entering recovery mode")
            self._sa_recovery_mode = True
            return self._sa_healer_area_then_f5(frame)

        else:
            # ---- Recovery mode: single RMB check -------------------------------
            capslock.raise_if_on()
            if pt is not None:
                logger.info("[BOT] SA recovery: 1 RMB check")
                self.hid.move_and_right_click(pt[0], pt[1], wait_after=0)
            else:
                logger.warn("[BOT] SA: assist_point not set — skipping recovery RMB")
            capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
            frame = self._grab()
            self._mob_anchor.invalidate()
            if self._mob_anchor.find(frame) is not None:
                logger.info("[BOT] SA: target found — exiting recovery mode")
                self._sa_recovery_mode = False
                return self._sa_phase4(frame)

            # Still no target — stay in recovery
            logger.info("[BOT] SA: RMB failed in recovery — continuing healer loop")
            return self._sa_healer_area_then_f5(frame)

    def _target_search(self) -> bool:
        """Acquire a target then verify via bag_mob_anchor.

        nc mode        — Shift+click nearest unoccupied mob name template.
        nexttarget mode — press F5 until bag_mob_anchor appears.
        assist mode     — right-click burst up to ASSIST_RMB_MAX_ATTEMPTS times;
                          returns False if all attempts fail (caller switches back
                          to win1 and skips the attack step).

        nc / nexttarget: raises StopBot on timeout.
        assist:          returns False after max attempts (no Telegram, no stop).
        """
        self._mob_anchor.invalidate()
        mode = self._active.targeting_mode

        if mode == "nc":
            return self._target_search_nc()

        deadline  = time.time() + cfg.TARGET_NOT_FOUND_TIMEOUT
        attempts  = 0
        logger.info(f"[BOT] Target search ({mode}) in '{self._active.title}'")

        while True:
            capslock.raise_if_on()

            mode = self._active.targeting_mode   # re-read in case downgraded mid-loop
            if mode == "assist":
                pt = self._active.assist_point
                if pt is not None:
                    if attempts == 0:
                        capslock.interruptible_sleep(random.uniform(
                            cfg.ASSIST_SEARCH_RETRY_MIN_MS / 1000.0,
                            cfg.ASSIST_SEARCH_RETRY_MAX_MS / 1000.0,
                        ))
                    attempts += 1
                    logger.info(
                        f"[BOT] Assist RMB burst #{attempts}"
                        f" on '{self._active.title}' → {pt}"
                    )
                    burst = random.randint(
                        cfg.ASSIST_RMB_COUNT_MIN, cfg.ASSIST_RMB_COUNT_MAX
                    )
                    for i in range(burst):
                        self.hid.move_and_right_click(pt[0], pt[1],
                                                      wait_after=0)
                        if i < burst - 1:
                            time.sleep(random.uniform(
                                cfg.ASSIST_RMB_INTERVAL_MIN_MS / 1000.0,
                                cfg.ASSIST_RMB_INTERVAL_MAX_MS / 1000.0,
                            ))
                    # Brief pause before grab (same range as inter-click gap)
                    time.sleep(random.uniform(
                        cfg.ASSIST_RMB_INTERVAL_MIN_MS / 1000.0,
                        cfg.ASSIST_RMB_INTERVAL_MAX_MS / 1000.0,
                    ))
                else:
                    logger.warn("[BOT] Assist point not calibrated — skipping right-click")
                    attempts += 1   # still counts as an attempt so we don't spin forever
            else:
                _press(self.hid, "f5")
                time.sleep(_rhold() / 1000.0)

            frame = self._grab()
            pos   = self._mob_anchor.find(frame)

            if pos is not None:
                # Check if this target should be skipped (nexttarget / nc mode)
                if (self._active.targeting_mode != "assist"
                        and self._mob_skip_finders
                        and any(f.find(frame) is not None
                                for f in self._mob_skip_finders)):
                    logger.info("[BOT] mob_skip matched — pressing next target")
                    _press(self.hid, "f5")
                    time.sleep(_rhold() / 1000.0)
                    self._mob_anchor.invalidate()
                    continue

                # Assist mode mob_skip: LMB burst then Shift+RMB burst to re-assist.
                # Counts as an attempt so ASSIST_RMB_MAX_ATTEMPTS still limits the loop.
                if (self._active.targeting_mode == "assist"
                        and self._mob_skip_finders
                        and any(f.find(frame) is not None
                                for f in self._mob_skip_finders)):
                    pt = self._active.assist_point
                    attempts += 1
                    if pt is not None:
                        logger.info(
                            f"[BOT] mob_skip matched in assist mode"
                            f" (attempt {attempts}/{cfg.ASSIST_RMB_MAX_ATTEMPTS})"
                            f" — LMB burst + Shift+RMB burst to re-assist"
                        )
                        burst = random.randint(
                            cfg.ASSIST_RMB_COUNT_MIN, cfg.ASSIST_RMB_COUNT_MAX
                        )
                        iv_lo = cfg.ASSIST_RMB_INTERVAL_MIN_MS / 1000.0
                        iv_hi = cfg.ASSIST_RMB_INTERVAL_MAX_MS / 1000.0
                        # LMB burst
                        for i in range(burst):
                            self.hid.move_and_click(pt[0], pt[1],
                                                    hold_min=40, hold_max=80)
                            if i < burst - 1:
                                time.sleep(random.uniform(iv_lo, iv_hi))
                        time.sleep(random.uniform(iv_lo, iv_hi))
                        # Single Shift+RMB to re-assist
                        self.hid.move_and_shift_right_click(pt[0], pt[1])
                    if attempts >= cfg.ASSIST_RMB_MAX_ATTEMPTS:
                        logger.info(
                            f"[BOT] Assist mob_skip: gave up after"
                            f" {attempts} attempts — returning to win1"
                        )
                        return False
                    self._mob_anchor.invalidate()
                    continue

                logger.info(f"[BOT] bag_mob_anchor found at {pos}")
                return True

            # Assist: give up after max attempts and return to win1
            if mode == "assist" and attempts >= cfg.ASSIST_RMB_MAX_ATTEMPTS:
                logger.info(
                    f"[BOT] Assist targeting gave up after {attempts} attempts"
                    f" in '{self._active.title}' — returning to win1"
                )
                return False

            if time.time() > deadline:
                msg = "New mobs did not appear for too long. The bot has been stopped."
                logger.warn(f"[BOT] {msg}")
                self.tg.send(msg)
                raise StopBot

    # ------------------------------------------------------------------
    # Buff / death check
    # ------------------------------------------------------------------

    def _check_buff_and_death(self) -> None:
        """
        Check check_full_buff and death_screen in the currently active window.
        Also updates slot.last_party_pos from the same frame so recovery clicks
        don't need a separate grab.  Calls _on_death() if death detected.
        """
        capslock.raise_if_on()
        frame    = self._grab()
        nickname = self._active.nickname()

        # Cache party anchor position for recovery left-clicks (no extra grab needed).
        party_pos = self._party_f.find(frame)
        if party_pos is not None:
            self._active.last_party_pos = party_pos

        # Buff check: at least one of the two images must be present
        buff_present = (self._buff_f.find(frame) is not None
                        or self._buff_f1.find(frame) is not None)
        if not buff_present:
            self._notifier.notify(
                nickname, "buff_expired",
                f"{cfg.PC_NUMBER}: The full buff on {nickname} has expired and needs to be refreshed!",
                cooldown=cfg.BUFF_NOTIFY_COOLDOWN_S,
            )
            logger.warn(f"[BOT] Full buff not present on '{nickname}'")

        # Death check (presence = problem)
        death_present = self._death_f.find(frame) is not None
        if death_present:
            self.tg.send(f"{nickname} has died!")
            logger.warn(f"[BOT] Death screen detected on '{nickname}'")
            self._on_death(self._active)

    def _on_death(self, slot: WindowSlot) -> None:
        """Disable the dead slot. If none left, raise StopBot."""
        slot.alive = False
        alive = self._alive_slots()
        if not alive:
            logger.warn("[BOT] All windows dead — stopping")
            raise StopBot
        logger.info(f"[BOT] '{slot.title}' disabled; continuing with remaining window(s)")
        # Switch to an alive window and reset anchor caches
        self._active = alive[0]
        self._mob_anchor.invalidate()
        self._char_anchor.invalidate()

    def _may_switch_during_kill(self) -> bool:
        """Return True if the bot is allowed to switch windows during the kill
        phase (_wait_low_hp / _wait_death).

        Rule:
          - nexttarget window NEVER switches during kill — it stays on its mob
            until the next target-search cycle.
          - assist window switches only when paired with a nexttarget opposite
            (the assist player is the support; the nexttarget player focuses).
          - If both are assist, neither switches during kill.
        """
        opp = self._opposite()
        if opp is None:
            return False
        return (self._active.targeting_mode == "assist" and
                opp.targeting_mode == "nexttarget")

    # ------------------------------------------------------------------
    # Wait for low mob HP
    # ------------------------------------------------------------------

    def _wait_low_hp(self) -> str:
        """
        Check mob HP every HP_CHECK_INTERVAL seconds, switching windows every
        HP_SWITCH_EVERY checks. Also reads char HP/MP from same grab.

        Returns:
          "ok"      — HP entered the kill zone [MOB_HP_LOW_PCT .. MOB_HP_HIGH_PCT]
          "stalled" — HP never dropped below HP_STALL_PCT within HP_STALL_S seconds
                      (caller should switch targets without recovery)
          "timeout" — full LOW_HP_TIMEOUT reached (recovery already performed)
        """
        timeout        = random.uniform(cfg.LOW_HP_TIMEOUT_MIN, cfg.LOW_HP_TIMEOUT_MAX)
        deadline       = time.time() + timeout
        stall_deadline = time.time() + cfg.HP_STALL_S
        hp_ever_dropped  = False
        check_n          = 0
        anchor_misses    = 0
        logger.info(f"[BOT] Waiting for mob HP {cfg.MOB_HP_LOW_PCT}-{cfg.MOB_HP_HIGH_PCT}%"
                    f" (timeout {timeout:.1f}s, stall {cfg.HP_STALL_S}s)")

        while True:
            capslock.raise_if_on()

            frame     = self._grab()
            mob_pos   = self._mob_anchor.find(frame, silent=True)
            mob_hp    = None

            if mob_pos:
                anchor_misses = 0
                p = cfg.PROFILE
                mob_hp = _bar_pct(frame, mob_pos[0], mob_pos[1],
                                  p["mob_bar_offset_x"], p["mob_bar_offset_y"],
                                  p["mob_bar_w"], p["mob_bar_h"], "red")

                # --- Char HP / MP from same grab ---
                char_pos = self._char_anchor.find(frame, silent=True)
                if char_pos:
                    char_hp = _bar_pct(frame, char_pos[0], char_pos[1],
                                       p["char_hp_offset_x"], p["char_hp_offset_y"],
                                       p["char_hp_w"], p["char_hp_h"], "red_char")
                    char_mp = _bar_pct(frame, char_pos[0], char_pos[1],
                                       p["char_mp_offset_x"], p["char_mp_offset_y"],
                                       p["char_mp_w"], p["char_mp_h"], "blue")
                    self._handle_char_bars(char_hp, char_mp, mob_hp)

                if mob_hp is not None:
                    logger.info(f"[BOT] Mob HP {mob_hp:.1f}%"
                                + (f"  char HP {char_hp:.1f}% MP {char_mp:.1f}%"
                                   if char_pos else ""))
                    if mob_hp < cfg.HP_STALL_PCT:
                        hp_ever_dropped = True
                    if mob_hp == 0.0:
                        logger.info("[BOT] Mob HP 0% — mob already dead, restarting cycle")
                        return "ok"
                    if mob_hp <= cfg.MOB_HP_HIGH_PCT:
                        return "ok"

            # Check mob_dead on every grab — skull and bag_mob_anchor coexist in the
            # same target frame so this fires even when mob_pos was found.
            if self._mob_dead_f.find(frame, silent=True) is not None:
                logger.info("[BOT] mob_dead detected during HP wait — restarting cycle")
                return "dead"

            if mob_pos is None:
                anchor_misses += 1
                if anchor_misses >= cfg.HP_ANCHOR_MISS_LIMIT:
                    logger.info(
                        f"[BOT] bag_mob_anchor missing for {anchor_misses} consecutive"
                        f" checks — target lost, restarting cycle"
                    )
                    return "ok"

            # Stall check (nexttarget / nc): mob never took damage → switch target.
            if (self._active.targeting_mode != "assist"
                    and not hp_ever_dropped
                    and time.time() > stall_deadline):
                jitter = random.uniform(cfg.HP_STALL_JITTER_MIN, cfg.HP_STALL_JITTER_MAX)
                logger.info(
                    f"[BOT] HP stall: mob at ~100% for {cfg.HP_STALL_S}s"
                    f" — waiting {jitter:.1f}s jitter, then switching target"
                )
                capslock.interruptible_sleep(jitter)
                return "stalled"

            # Stall check (assist): mob HP stuck at ≥ HP_STALL_PCT for HP_STALL_S seconds.
            # 0–3 s jitter → LMB burst at assist_point → 5–10 s wait → return "stalled"
            # so _cycle can reset recovery state and restart from Phase 1.
            if (self._active.targeting_mode == "assist"
                    and not hp_ever_dropped
                    and time.time() > stall_deadline):
                pt   = self._active.assist_point
                nick = self._active.title
                jitter = random.uniform(cfg.HP_STALL_JITTER_MIN, cfg.HP_STALL_JITTER_MAX)
                logger.info(
                    f"[BOT] [{nick}] Assist HP stall: mob at ≥{cfg.HP_STALL_PCT}%"
                    f" for {cfg.HP_STALL_S}s — jitter {jitter:.1f}s, then LMB burst"
                )
                capslock.interruptible_sleep(jitter)
                if pt is not None:
                    burst = random.randint(
                        cfg.ASSIST_RMB_COUNT_MIN, cfg.ASSIST_RMB_COUNT_MAX
                    )
                    for i in range(burst):
                        self.hid.move_and_click(pt[0], pt[1],
                                                hold_min=40, hold_max=80)
                        if i < burst - 1:
                            time.sleep(random.uniform(
                                cfg.ASSIST_RMB_INTERVAL_MIN_MS / 1000.0,
                                cfg.ASSIST_RMB_INTERVAL_MAX_MS / 1000.0,
                            ))
                wait_s = random.uniform(cfg.SA_STALL_WAIT_MIN, cfg.SA_STALL_WAIT_MAX)
                logger.info(f"[BOT] [{nick}] Post-burst pause {wait_s:.1f}s")
                capslock.interruptible_sleep(wait_s)
                return "stalled"

            # Full timeout
            if time.time() > deadline:
                if self._active.targeting_mode == "assist":
                    logger.info("[BOT] HP wait timeout in assist mode — restarting cycle")
                    return "ok"
                self._timeout_recovery("waited too long for mob HP to drop")
                return "timeout"

            # Alternate windows only when the kill-phase switch rule allows it
            check_n += 1
            if self._may_switch_during_kill() and check_n % cfg.HP_SWITCH_EVERY == 0:
                self._switch_to(self._opposite())

            time.sleep(cfg.HP_CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Wait for mob death
    # ------------------------------------------------------------------

    def _wait_death(self) -> bool:
        """
        Check mob_dead every DEATH_CHECK_INTERVAL seconds.
        Returns True when dead. Returns False on timeout (recovery done).
        """
        deadline     = time.time() + cfg.DEATH_TIMEOUT
        check_n      = 0
        zero_hp_hits = 0   # consecutive 0% HP reads → mob dead despite missing mob_dead event
        logger.info(f"[BOT] Waiting for mob death (timeout {cfg.DEATH_TIMEOUT:.0f}s)")

        while True:
            capslock.raise_if_on()

            frame    = self._grab()
            is_dead  = self._mob_dead_f.find(frame, silent=True) is not None

            if is_dead:
                logger.info("[BOT] mob_dead detected")
                return True

            # If the mob target indicator vanished (mob_dead never appeared but
            # bag_mob_anchor is gone too), treat it as dead and start a new cycle.
            mob_pos = self._mob_anchor.find(frame, silent=True)
            if mob_pos is None:
                logger.info("[BOT] bag_mob_anchor gone without mob_dead — treating as dead")
                return True

            # Fallback for game bug: mob HP stuck at 0% but mob_dead never fires.
            # Require 2 consecutive 0% reads to avoid a single misread causing an
            # early exit.
            p      = cfg.PROFILE
            mob_hp = _bar_pct(frame, mob_pos[0], mob_pos[1],
                              p["mob_bar_offset_x"], p["mob_bar_offset_y"],
                              p["mob_bar_w"], p["mob_bar_h"], "red")
            if mob_hp == 0.0:
                zero_hp_hits += 1
                if zero_hp_hits >= 2:
                    logger.info("[BOT] Mob HP 0% for 2 consecutive reads"
                                " — treating as dead (mob_dead not fired)")
                    return True
            else:
                zero_hp_hits = 0

            if time.time() > deadline:
                if self._active.targeting_mode == "assist":
                    logger.info("[BOT] Death wait timeout in assist mode — restarting cycle")
                    return True
                self._timeout_recovery("waited too long for mob to die")
                return False

            check_n += 1
            if self._may_switch_during_kill() and check_n % cfg.DEATH_SWITCH_EVERY == 0:
                self._switch_to(self._opposite())

            time.sleep(cfg.DEATH_CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Char HP / MP handling
    # ------------------------------------------------------------------

    def _handle_char_bars(self, char_hp: Optional[float],
                           char_mp: Optional[float],
                           mob_hp: Optional[float]) -> None:
        now = time.time()
        if char_hp is not None:
            if char_hp < cfg.CHAR_HP_CRITICAL_PCT:
                if now - self._last_f7 >= cfg.POTION_COOLDOWN_S:
                    logger.info(f"[BOT] Char HP {char_hp:.1f}% critical — F7")
                    _press(self.hid, "f7")
                    self._last_f7 = now
                else:
                    logger.info(f"[BOT] Char HP {char_hp:.1f}% critical — F7 on cooldown")
            elif char_hp < cfg.CHAR_HP_LOW_PCT:
                if now - self._last_f6 >= cfg.POTION_COOLDOWN_S:
                    logger.info(f"[BOT] Char HP {char_hp:.1f}% low — F6")
                    _press(self.hid, "f6")
                    self._last_f6 = now
                else:
                    logger.info(f"[BOT] Char HP {char_hp:.1f}% low — F6 on cooldown")

        if (char_mp is not None and mob_hp is not None
                and char_mp > cfg.CHAR_MANA_HIGH_PCT
                and mob_hp > 70.0):
            logger.info(f"[BOT] Mana {char_mp:.1f}% high, mob HP {mob_hp:.1f}% — F2")
            _press(self.hid, "f2")

    # ------------------------------------------------------------------
    # Timeout recovery
    # ------------------------------------------------------------------

    def _timeout_recovery(self, reason: str) -> None:
        nickname = self._active.nickname()
        logger.warn(f"[BOT] Timeout: {reason} on '{nickname}'")
        self.tg.send(f"{nickname} {reason}.")

        # In assist mode, downgrade to nexttarget so the next cycle uses F5.
        if self._active.targeting_mode == "assist":
            msg = f"{nickname}: assist mode disabled after timeout — switching to nexttarget (F5)."
            logger.warn(f"[BOT] {msg}")
            self.tg.send(msg)
            self._active.targeting_mode = "nexttarget"

        # 1. Random WASD + ESC x2-4
        _press_wasd(self.hid)
        _press_n(self.hid, "esc", random.randint(2, 4))

        # 2. Click party anchor offset (94, 23) x3-5
        self._click_party_offset(random.randint(3, 5))

        # 3. Recovery wait
        rsleep(cfg.RECOVERY_WAIT_MIN, cfg.RECOVERY_WAIT_MAX, reason="recovery")

        # 4. 50 % chance: another WASD + ESC burst
        if random.random() < 0.5:
            _press_wasd(self.hid)
            _press_n(self.hid, "esc", random.randint(2, 4))

        # Invalidate caches (will force full-screen re-search)
        self._mob_anchor.invalidate()
        self._char_anchor.invalidate()
        self._party_f.invalidate()

    def _click_party_offset(self, count: int) -> None:
        """Left-click (party_anchor_center + ASSIST_CLICK_OFFSET) N times.

        Uses slot.last_party_pos (cached from the last buff/death frame) to avoid
        an extra screen grab.  Falls back to a fresh template search if not cached.
        """
        pos = self._active.last_party_pos
        if pos is None:
            frame = self._grab()
            pos   = self._party_f.find(frame)
            if pos is not None:
                self._active.last_party_pos = pos
        if pos is None:
            logger.warn("[BOT] party_pl_anchor not found — skipping clicks")
            return
        tx = pos[0] + 94
        ty = pos[1] + 23
        for _ in range(count):
            self.hid.move_and_click(tx, ty, hold_min=20, hold_max=50)
        logger.info(f"[BOT] Clicked party offset ({tx},{ty}) x{count}")

    # ------------------------------------------------------------------
    # Attack after target acquisition
    # ------------------------------------------------------------------

    def _press_attack(self) -> None:
        """Press the attack key(s) matching the current window's targeting mode.

        nexttarget / nc            → F1 × N  (standard engage)
        assist + opp nexttarget/nc → F2 × N  (opp is the finisher; assist adds F2)
        assist + opp assist        → F1 × N  (both-assist: F1 after RMB burst)
        assist + single window     → F1 × N  (no partner: F1 after RMB burst)
        """
        if self._active.targeting_mode == "assist":
            opp = self._opposite()
            if opp is not None and opp.targeting_mode in ("nexttarget", "nc"):
                # Opposite is the finisher — press F2
                count = random.randint(
                    cfg.ASSIST_ATTACK_COUNT_MIN, cfg.ASSIST_ATTACK_COUNT_MAX
                )
                logger.info(f"[BOT] F2 x{count} in '{self._active.title}' (assist attack)")
                for i in range(count):
                    _press(self.hid, "f2",
                           hold_min=cfg.ASSIST_ATTACK_HOLD_MIN_MS,
                           hold_max=cfg.ASSIST_ATTACK_HOLD_MAX_MS)
                    if i < count - 1:
                        time.sleep(random.uniform(
                            cfg.ASSIST_ATTACK_INTERVAL_MIN_MS / 1000.0,
                            cfg.ASSIST_ATTACK_INTERVAL_MAX_MS / 1000.0,
                        ))
            else:
                # both-assist or single — F1 after RMB burst
                count = random.randint(
                    cfg.ASSIST_ATTACK_COUNT_MIN, cfg.ASSIST_ATTACK_COUNT_MAX
                )
                logger.info(f"[BOT] F1 x{count} in '{self._active.title}' (assist attack)")
                for i in range(count):
                    _press(self.hid, "f1",
                           hold_min=cfg.ASSIST_ATTACK_HOLD_MIN_MS,
                           hold_max=cfg.ASSIST_ATTACK_HOLD_MAX_MS)
                    if i < count - 1:
                        time.sleep(random.uniform(
                            cfg.ASSIST_ATTACK_INTERVAL_MIN_MS / 1000.0,
                            cfg.ASSIST_ATTACK_INTERVAL_MAX_MS / 1000.0,
                        ))
        else:
            count = random.randint(
                cfg.NEXTTARGET_ATTACK_COUNT_MIN, cfg.NEXTTARGET_ATTACK_COUNT_MAX
            )
            logger.info(f"[BOT] F1 x{count} in '{self._active.title}' (nexttarget attack)")
            for i in range(count):
                _press(self.hid, "f1",
                       hold_min=cfg.NEXTTARGET_ATTACK_HOLD_MIN_MS,
                       hold_max=cfg.NEXTTARGET_ATTACK_HOLD_MAX_MS)
                if i < count - 1:
                    time.sleep(random.uniform(
                        cfg.NEXTTARGET_ATTACK_INTERVAL_MIN_MS / 1000.0,
                        cfg.NEXTTARGET_ATTACK_INTERVAL_MAX_MS / 1000.0,
                    ))

    # ------------------------------------------------------------------
    # Window switching
    # ------------------------------------------------------------------

    def _recheck_party_anchor(self) -> bool:
        """When party_pl_anchor is not found, press Win+3 (neutral window),
        switch back to the current window, and search for the anchor again.

        Returns True if the anchor is found on the second attempt, False otherwise.
        """
        slot = self._active
        logger.info(
            f"[BOT] party_pl_anchor not found in '{slot.title}'"
            f" — pressing Win+3 and retrying"
        )
        settle = random.uniform(cfg.WIN_SETTLE_MS_MIN, cfg.WIN_SETTLE_MS_MAX) / 1000.0
        self.hid.press_key_combo("gui", "3", hold_ms=25, wait_after_s=0)
        time.sleep(settle)
        if slot.taskbar_key:
            self.hid.press_key_combo("gui", slot.taskbar_key, hold_ms=25, wait_after_s=0)
            logger.info(f"[BOT] Win+{slot.taskbar_key} → '{slot.title}' (party recheck)")
        else:
            slot.activate()
        time.sleep(settle)
        frame = self._grab()
        found = self._party_f.find(frame) is not None
        if found:
            logger.info(f"[BOT] party_pl_anchor found after refresh in '{slot.title}'")
        else:
            logger.warn(f"[BOT] party_pl_anchor still not found in '{slot.title}' after refresh")
        return found

    def _switch_to(self, slot: WindowSlot) -> None:
        if slot is self._active:
            return   # already there
        settle = random.uniform(cfg.WIN_SETTLE_MS_MIN, cfg.WIN_SETTLE_MS_MAX) / 1000.0
        logger.info(f"[BOT] Switching to '{slot.title}' (settle {settle*1000:.0f}ms)")

        if slot.taskbar_key is not None:
            # Win+N — instant OS-level task switch, no minimize→restore needed.
            logger.info(f"[BOT] Win+{slot.taskbar_key} → '{slot.title}'")
            self.hid.press_key_combo("gui", slot.taskbar_key,
                                      hold_ms=25, wait_after_s=0)
            capslock.interruptible_sleep(settle)
        else:
            # Minimize the outgoing window first — SW_RESTORE on a minimized
            # window grants focus without needing SetForegroundWindow
            # (no foreground lock issue).
            if self._active is not None:
                self._active.minimize_window()
            slot.activate(settle_s=settle)

        self._active = slot
        # Invalidate anchor caches — anchor positions change per window
        self._mob_anchor.invalidate()
        self._char_anchor.invalidate()
        self._mob_dead_f.invalidate()
        self._death_f.invalidate()
        self._disconnect_f.invalidate()
        self._buff_f.invalidate()
        self._buff_f1.invalidate()

    def _opposite(self) -> Optional[WindowSlot]:
        alive = self._alive_slots()
        others = [s for s in alive if s is not self._active]
        return others[0] if others else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _invalidate_all_caches(self) -> None:
        for finder in (self._mob_anchor, self._char_anchor,
                       self._mob_dead_f, self._death_f,
                       self._buff_f, self._buff_f1, self._party_f):
            finder.invalidate()

    def _alive_slots(self) -> list[WindowSlot]:
        return [s for s in self.slots.values() if s.enabled and s.alive]

    def _first_alive_slot(self) -> Optional[WindowSlot]:
        alive = self._alive_slots()
        return alive[0] if alive else None

    def _detect_foreground_slot(self) -> Optional[WindowSlot]:
        if win32gui is None:
            return None
        try:
            fg = win32gui.GetForegroundWindow()
        except Exception:
            return None
        for slot in self.slots.values():
            if slot.enabled and slot.hwnd == fg:
                return slot
        return None

    def _probe_taskbar_positions(self, settle_s: float = 0.4) -> None:
        """
        Verify that each configured taskbar_pos actually contains the expected
        game window.  Only presses Win+N for positions that are explicitly set
        in config — never scans beyond those.

        Slots without a configured taskbar_pos are left for the minimize→restore
        path (taskbar_key remains None).

        Raises StopBot if any window is missing from its configured slot.
        """
        if win32gui is None:
            return

        configured = [s for s in self.slots.values()
                      if s.enabled and s.taskbar_pos is not None and s.hwnd]
        if not configured:
            return

        logger.info("[BOT] Verifying taskbar positions — window focus will switch briefly")

        # Focus the console window first so no game window is currently in the
        # foreground.  Win+N is a toggle — if the target is already focused it
        # would minimize instead of focus, giving a false negative.
        try:
            import ctypes as _ct
            console_hwnd = _ct.windll.kernel32.GetConsoleWindow()
            if console_hwnd:
                win32gui.SetForegroundWindow(console_hwnd)
                time.sleep(0.2)
        except Exception:
            pass

        for slot in configured:
            key = slot.taskbar_key          # already set from config in __init__
            self.hid.press_key_combo("gui", key, hold_ms=25, wait_after_s=0)
            time.sleep(settle_s)

            try:
                fg = win32gui.GetForegroundWindow()
            except Exception:
                fg = None

            if fg == slot.hwnd:
                logger.info(f"[BOT] '{slot.title}' confirmed at taskbar pos "
                            f"{slot.taskbar_pos} (Win+{key})")
            else:
                # Something else came to front — window is not where we expect
                actual_title = ""
                try:
                    actual_title = f" (got '{win32gui.GetWindowText(fg)}')"
                except Exception:
                    pass
                msg = (f"'{slot.title}' is NOT at taskbar position "
                       f"{slot.taskbar_pos}{actual_title}. "
                       f"Please make sure the game clients are open and in the "
                       f"correct taskbar slots. Bot stopped.")
                logger.error(f"[BOT] {msg}")
                self.tg.send(msg)
                raise StopBot

    def _grab(self) -> np.ndarray:
        """Grab the full primary monitor and return a BGR numpy array."""
        raw = self.sct.grab(self.sct.monitors[1])
        return cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    FarmBot().run()


if __name__ == "__main__":
    main()
