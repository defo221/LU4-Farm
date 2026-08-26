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


def _build_valid_rects(
        fw: int, fh: int,
        excl_rois: List[Tuple[int, int, int, int]],
        min_w: int = 1,
        min_h: int = 1,
) -> List[Tuple[int, int, int, int]]:
    """Sweep-line decomposition of (fw × fh) minus *excl_rois*.

    Returns a list of non-overlapping (x1, y1, x2, y2) rectangles (exclusive
    upper bound) that together cover every pixel NOT inside any exclusion ROI.
    Strips narrower than *min_w* or shorter than *min_h* are dropped.
    """
    y_breaks = sorted(set([0, fh]
                          + [r[1] for r in excl_rois]
                          + [r[3] for r in excl_rois]))
    result: List[Tuple[int, int, int, int]] = []
    for i in range(len(y_breaks) - 1):
        y1, y2 = y_breaks[i], y_breaks[i + 1]
        if y2 - y1 < min_h:
            continue
        blocked: List[Tuple[int, int]] = []
        for ex1, ey1, ex2, ey2 in excl_rois:
            if ey1 < y2 and ey2 > y1:
                blocked.append((max(0, ex1), min(fw, ex2)))
        blocked.sort()
        merged: List[List[int]] = []
        for bx1, bx2 in blocked:
            if merged and bx1 <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], bx2)
            else:
                merged.append([bx1, bx2])
        vx = 0
        for bx1, bx2 in merged:
            if bx1 - vx >= min_w:
                result.append((vx, y1, bx1, y2))
            vx = max(vx, bx2)
        if fw - vx >= min_w:
            result.append((vx, y1, fw, y2))
    return result


_VALID_RECTS_CACHE: dict = {}   # keyed by (fw, fh); computed once per resolution


def _valid_search_rects(frame: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Return precomputed valid-search rects for *frame*'s resolution.

    Runs the sweep-line decomposition of SA_EXCL_ROIS_FHD once and caches
    the result.  These rects are passed to _match_best_in_rects and
    _sa_find_blue_dots, which expand each rect by the template half-size
    before calling matchTemplate so that every possible template-center
    position within the rect is reachable — no blind strips at boundaries.
    Non-FHD frames get an empty list (full-frame fallback).
    """
    fh, fw = frame.shape[:2]
    key = (fw, fh)
    if key not in _VALID_RECTS_CACHE:
        excl = (list(getattr(cfg, "SA_EXCL_ROIS_FHD", []))
                if fw == 1920 and fh == 1080 else [])
        rects = _build_valid_rects(fw, fh, excl) if excl else []
        _VALID_RECTS_CACHE[key] = rects
        if rects:
            total = sum((x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in rects)
            pct   = 100 * total // (fw * fh)
            logger.info(
                f"[PERF] valid_search_rects cached for {fw}×{fh}:"
                f" {len(rects)} rects, {total // 1000}K px candidate area"
                f" ({pct}% of full frame)"
            )
    return _VALID_RECTS_CACHE[key]


def _match_best_in_rects(
        frame: np.ndarray,
        tmpl: np.ndarray,
        valid_rects: List[Tuple[int, int, int, int]],
) -> Tuple[float, int, int]:
    """Run TM_CCOEFF_NORMED on each valid-rect crop; return global best.

    Each crop is expanded by the template half-size (hw = tw//2, hh = th//2)
    before matchTemplate so that templates centered at the very edge of a valid
    rect are reachable.  After obtaining the per-crop best, the global center
    is checked against the half-open invariant:

        vx1 <= gcx < vx2   and   vy1 <= gcy < vy2

    This is the authoritative guard: it handles even-sized templates (where the
    expanded result map has one extra column), frame-edge clamping, and any
    position that matchTemplate returns outside the intended center region.
    Adjacent rects tile continuously — a center exactly on a shared boundary is
    accepted by the right-hand rect (vx1 <= gcx).

    Returns (best_score, global_cx, global_cy) in frame coordinates.
    Falls back to a full-frame search when *valid_rects* is empty.
    """
    th, tw = tmpl.shape[:2]
    fh, fw = frame.shape[:2]
    hw, hh = tw // 2, th // 2
    best_score = -2.0
    best_cx = best_cy = 0
    search = valid_rects or [(0, 0, fw, fh)]
    for vx1, vy1, vx2, vy2 in search:
        cx1 = max(0, vx1 - hw)
        cy1 = max(0, vy1 - hh)
        cx2 = min(fw, vx2 + hw)
        cy2 = min(fh, vy2 + hh)
        if cy2 - cy1 < th or cx2 - cx1 < tw:
            continue
        res = cv2.matchTemplate(frame[cy1:cy2, cx1:cx2], tmpl, cv2.TM_CCOEFF_NORMED)
        _, sc, _, loc = cv2.minMaxLoc(res)
        if sc > best_score:
            gcx = cx1 + loc[0] + hw
            gcy = cy1 + loc[1] + hh
            if vx1 <= gcx < vx2 and vy1 <= gcy < vy2:
                best_score = sc
                best_cx    = gcx
                best_cy    = gcy
    return best_score, best_cx, best_cy


def _sa_find_blue_dots(frame: np.ndarray,
                       tmpl: Optional[np.ndarray],
                       conf: float = 0.75,
                       nms_dist: int = 8,
                       ) -> List[Tuple[int, int]]:
    """Return (cx, cy) centres for every template detection in *frame*.

    Single full-frame cv2.matchTemplate call.  Before thresholding, result-map
    cells whose template centre falls inside any SA_EXCL_ROIS_FHD region are
    set to -1 so they are never returned as candidates.  NMS is applied to the
    surviving hits.
    """
    if tmpl is None or frame is None:
        return []
    th, tw = tmpl.shape[:2]
    fh, fw = frame.shape[:2]
    hw, hh = tw // 2, th // 2
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    excl: list = (list(getattr(cfg, "SA_EXCL_ROIS_FHD", []))
                  if fw == 1920 and fh == 1080 else [])
    if excl:
        rh, rw = res.shape
        for ex1, ey1, ex2, ey2 in excl:
            rx1 = max(0, ex1 - hw);  rx2 = min(rw, ex2 - hw + 1)
            ry1 = max(0, ey1 - hh);  ry2 = min(rh, ey2 - hh + 1)
            if rx2 > rx1 and ry2 > ry1:
                res[ry1:ry2, rx1:rx2] = -1.0
    ys, xs = np.where(res >= conf)
    raw: List[Tuple[int, int]] = [(int(rx) + hw, int(ry) + hh)
                                  for rx, ry in zip(xs, ys)]
    raw.sort(key=lambda p: p[0])
    return _sa_nms(raw, nms_dist)


def _push_outside_excl(px: int, py: int,
                       rois: List[Tuple[int, int, int, int]],
                       ) -> Tuple[int, int]:
    """If (px, py) is inside any exclusion ROI, push it 2 px past the nearest edge.

    Uses the shortest displacement so the corrected point stays as close as
    possible to the original intent.  Only the first matching ROI is corrected
    per call; ROIs in SA_EXCL_ROIS_FHD do not overlap so one pass is enough.
    """
    for x1, y1, x2, y2 in rois:
        if x1 <= px <= x2 and y1 <= py <= y2:
            orig_px, orig_py = px, py
            dist_left  = px - x1
            dist_right = x2 - px
            dist_top   = py - y1
            dist_bot   = y2 - py
            min_d = min(dist_left, dist_right, dist_top, dist_bot)
            if min_d == dist_left:
                px = x1 - 2
            elif min_d == dist_right:
                px = x2 + 2
            elif min_d == dist_top:
                py = y1 - 2
            else:
                py = y2 + 2
            logger.info(
                f"[BOT] click ({orig_px},{orig_py}) was inside excl ROI"
                f" ({x1},{y1})-({x2},{y2}) — pushed to ({px},{py})")
            break
    return px, py


def _cursor_pos() -> Tuple[int, int]:
    """Return the current OS cursor position (read-only observation)."""
    import ctypes

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    p = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _grab_screen_with_cursor() -> Optional[np.ndarray]:
    """Capture the full screen with the real OS mouse cursor composited in.

    mss/dxcam never include the cursor — Windows draws it outside the captured
    surface — so the screen is BitBlt'd into a memory DC and the live cursor
    bitmap is stamped on with DrawIconEx at its true hotspot-corrected
    position.  Read-only screen/cursor observation; no input is generated.

    Returns a BGR ndarray, or None if any GDI step fails.
    """
    import ctypes
    from ctypes import wintypes as wt

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

    SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
    DI_NORMAL, CURSOR_SHOWING, BI_RGB = 0x0003, 0x0001, 0

    # Explicit signatures are mandatory: on 64-bit Windows GDI/USER handles do
    # not fit ctypes' default c_int and get truncated, which corrupts every
    # subsequent call that receives them.
    VP = ctypes.c_void_p
    user32.GetDC.restype                    = VP
    user32.GetDC.argtypes                   = [VP]
    user32.ReleaseDC.argtypes               = [VP, VP]
    user32.GetSystemMetrics.argtypes        = [ctypes.c_int]
    user32.GetCursorInfo.argtypes           = [VP]
    user32.GetIconInfo.argtypes             = [VP, VP]
    user32.DrawIconEx.argtypes              = [VP, ctypes.c_int, ctypes.c_int,
                                               VP, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_uint, VP, ctypes.c_uint]
    gdi32.CreateCompatibleDC.restype        = VP
    gdi32.CreateCompatibleDC.argtypes       = [VP]
    gdi32.CreateCompatibleBitmap.restype    = VP
    gdi32.CreateCompatibleBitmap.argtypes   = [VP, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype              = VP
    gdi32.SelectObject.argtypes             = [VP, VP]
    gdi32.BitBlt.argtypes                   = [VP, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, VP,
                                               ctypes.c_int, ctypes.c_int,
                                               ctypes.c_uint]
    gdi32.GetDIBits.argtypes                = [VP, VP, ctypes.c_uint,
                                               ctypes.c_uint, VP, VP,
                                               ctypes.c_uint]
    gdi32.DeleteObject.argtypes             = [VP]
    gdi32.DeleteDC.argtypes                 = [VP]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                    ("hCursor", wt.HANDLE), ("ptScreenPos", wt.POINT)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                    ("yHotspot", wt.DWORD), ("hbmMask", wt.HANDLE),
                    ("hbmColor", wt.HANDLE)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        return None
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp    = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old     = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, 0, 0,
                            SRCCOPY | CAPTUREBLT):
            return None

        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & CURSOR_SHOWING):
            ii = ICONINFO()
            if user32.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
                user32.DrawIconEx(hdc_mem,
                                  ci.ptScreenPos.x - ii.xHotspot,
                                  ci.ptScreenPos.y - ii.yHotspot,
                                  ci.hCursor, 0, 0, 0, None, DI_NORMAL)
                if ii.hbmMask:
                    gdi32.DeleteObject(ii.hbmMask)
                if ii.hbmColor:
                    gdi32.DeleteObject(ii.hbmColor)

        bmi = BITMAPINFOHEADER()
        bmi.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth       = w
        bmi.biHeight      = -h          # negative → top-down rows
        bmi.biPlanes      = 1
        bmi.biBitCount    = 32
        bmi.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf,
                               ctypes.byref(bmi), 0):
            return None
        arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape((h, w, 4))
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


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
                 max_y: Optional[int] = None,
                 roi: Optional[Tuple[int, int, int, int]] = None):
        self.path       = image_path
        self.confidence = confidence
        self.padding    = padding
        # max_y: when set, full-screen searches are restricted to rows 0..max_y-1.
        # Cached hits are still accepted at any position (they were already
        # verified, so restricting them would only cause unnecessary cache misses).
        self.max_y      = max_y
        # roi = (x1, y1, x2, y2): when set, full searches are restricted to this
        # rectangle instead of the full frame / max_y strip.  Returned coordinates
        # are always in absolute screen space.  Overrides max_y for full searches.
        self.roi        = roi  # Optional[Tuple[x1, y1, x2, y2]]
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
        # Crop the search region: ROI wins over max_y when both are set.
        if self.roi is not None:
            rx1, ry1, rx2, ry2 = self.roi
            fh, fw = frame.shape[:2]
            rx1 = max(0, rx1); ry1 = max(0, ry1)
            rx2 = min(fw, rx2); ry2 = min(fh, ry2)
            search_frame = frame[ry1:ry2, rx1:rx2]
            offset_x, offset_y = rx1, ry1
            region_str = f"ROI ({rx1},{ry1})–({rx2},{ry2})"
        elif self.max_y is not None:
            search_frame = frame[:self.max_y]
            offset_x, offset_y = 0, 0
            region_str = f"top {self.max_y}px"
        else:
            search_frame = frame
            offset_x, offset_y = 0, 0
            region_str = "full frame"

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
            self._cx = offset_x + loc[0] + self._tw // 2
            self._cy = offset_y + loc[1] + self._th // 2
            if not silent:
                logger.info(f"[ANCHOR] {self._name} found at {(self._cx, self._cy)}"
                            f"  score={score:.3f}")
            return (self._cx, self._cy)
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
    # True  — "ac" mode: crosshair was calibrated at startup; assist_point is used
    #          for RMB clicks and the old phase-based approach (_single_assist_cycle_ac).
    # False — "a"  mode: ma1/ma2 images are used for targeting; no crosshair.
    assist_use_crosshair:  bool                      = False
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
        _mob_roi = (cfg.BAG_MOB_ANCHOR_ROI_FHD
                    if cfg.RESOLUTION == "FHD"
                       and getattr(cfg, "BAG_MOB_ANCHOR_ROI_FHD", None) is not None
                    else None)
        self._mob_anchor   = AnchorFinder(_ap("bag_mob_anchor.png"),
                                          max_y=(_top if _mob_roi is None else None),
                                          roi=_mob_roi)
        self._char_anchor  = AnchorFinder(_ap("char_bars_anchor.png"), max_y=_top)
        self._mob_dead_f   = AnchorFinder(_ap("mob_dead.png"),
                                          max_y=(_top if _mob_roi is None else None),
                                          roi=_mob_roi)
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

        # MA-anchor images (ma1.png / ma2.png) for the default "a" assist mode.
        # The viewer toggles which one is active.  Position is cached after each
        # _check_buff_and_death() pass so the bot never re-grabs just for the RMB point.
        def _load_ma(name: str) -> Optional[np.ndarray]:
            p = _ap(name)
            t = cv2.imread(p)
            if t is None:
                logger.warn(f"[BOT] {name} not found — ma-assist may fall back to assist_point")
            else:
                logger.info(f"[BOT] {name} loaded ({t.shape[1]}×{t.shape[0]}): {p}")
            return t

        self._ma1_tmpl: Optional[np.ndarray] = _load_ma("ma1.png")
        self._ma2_tmpl: Optional[np.ndarray] = _load_ma("ma2.png")
        self._ma_anchor_tmpl: Optional[np.ndarray] = _load_ma("ma_anchor.png")
        self._ma_select: int = 1                          # 1 or 2; toggled by viewer
        self._ma_pos: Optional[Tuple[int, int]] = None   # cached RMB point (ma1/ma2)

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
                        self._invalidate_all_caches()
                        continue
                    try:
                        self._run_split_assist()
                        break   # only reached if _run_split_assist returns normally
                    except capslock.CapsLockPause:
                        logger.info(f"[BOT] Paused via {cfg.PAUSE_KEY}")
                        capslock.wait_off()
                        logger.info("[BOT] Resumed — continuing split-assist")
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
                    f"  [a]  Assist     (ma1/ma2 image anchor — no crosshair)"
                    f"  [ac] Assist+XH  (right-click crosshair — calibrated now): "
                ).strip().lower()
                if choice in ("a", "ac", "n", "nc"):
                    break
                print("  Please type 'n', 'nc', 'a', or 'ac'.")
            if choice in ("a", "ac"):
                slot.targeting_mode = "assist"
                slot.assist_use_crosshair = (choice == "ac")
            elif choice == "nc":
                slot.targeting_mode = "nc"
            else:
                slot.targeting_mode = "nexttarget"
            logger.info(f"[BOT] '{slot.title}' targeting mode: {slot.targeting_mode}")

        # If both chose assist, ask party grouping
        assist_slots = [s for s in enabled if s.targeting_mode == "assist"]
        ac_slots     = [s for s in assist_slots if s.assist_use_crosshair]
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

        # ── Phase 2: crosshair calibration — only for "ac" slots ────────────
        for slot in ac_slots:
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

    def set_ma_select(self, n: int) -> None:
        """Called by stream_sender when the viewer switches the active MA image (1 or 2)."""
        self._ma_select = int(n)
        self._ma_pos = None   # invalidate cached position — image changed
        logger.info(f"[BOT] MA anchor: switched to ma{self._ma_select}.png")

    def _detect_ma_pos(self, frame: np.ndarray) -> Optional[Tuple[int, int]]:
        """Find the active ma1/ma2 template in *frame*.  Returns centre pixel or None."""
        tmpl = self._ma1_tmpl if self._ma_select == 1 else self._ma2_tmpl
        if tmpl is None:
            return None
        res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        th, tw = tmpl.shape[:2]
        cx, cy = loc[0] + tw // 2, loc[1] + th // 2
        if score < cfg.SA_MA_CONFIDENCE:
            logger.info(
                f"[BOT] ma{self._ma_select} NOT found — best score={score:.3f}"
                f" at ({cx},{cy}), threshold={cfg.SA_MA_CONFIDENCE}"
            )
            return None
        logger.info(
            f"[BOT] ma{self._ma_select} found at ({cx},{cy}), score={score:.3f}"
        )
        return cx, cy

    def _detect_ma_anchor_pos(self, frame: np.ndarray) -> Optional[Tuple[int, int]]:
        """Find the ma_anchor template in *frame*.

        ma_anchor.png is the in-world visual marker that tracks the mob's position
        on screen.  It drives the ground-click area centre, the <SA_MA_CLOSE_PX
        close-zone, and the anchor-lost / camera-rotate logic.

        Single full-frame cv2.matchTemplate call.  Result-map cells whose
        template centre falls inside any SA_EXCL_ROIS_FHD region are set to -1
        before minMaxLoc so that UI elements are never returned as the winner.
        """
        tmpl = self._ma_anchor_tmpl
        if tmpl is None:
            return None
        th, tw = tmpl.shape[:2]
        fh, fw = frame.shape[:2]
        hw, hh = tw // 2, th // 2

        res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
        excl: list = (list(getattr(cfg, "SA_EXCL_ROIS_FHD", []))
                      if fw == 1920 and fh == 1080 else [])
        if excl:
            rh, rw = res.shape
            for ex1, ey1, ex2, ey2 in excl:
                rx1 = max(0, ex1 - hw);  rx2 = min(rw, ex2 - hw + 1)
                ry1 = max(0, ey1 - hh);  ry2 = min(rh, ey2 - hh + 1)
                if rx2 > rx1 and ry2 > ry1:
                    res[ry1:ry2, rx1:rx2] = -1.0
        _, score, _, loc = cv2.minMaxLoc(res)
        cx, cy = loc[0] + hw, loc[1] + hh

        if score < cfg.SA_MA_CONFIDENCE:
            logger.info(
                f"[BOT] ma_anchor NOT found — best score={score:.3f}"
                f" at ({cx},{cy}), threshold={cfg.SA_MA_CONFIDENCE}"
            )
            return None

        self._ma_frame_id = getattr(self, "_ma_frame_id", 0) + 1
        fid = self._ma_frame_id
        logger.info(f"[BOT] ma_anchor found at ({cx},{cy}), score={score:.3f}"
                    f"  frame_id={fid}")

        if getattr(cfg, "SA_MA_ANCHOR_DEBUG", False):
            # For rival-candidate enumeration we need the full result map.
            # This extra matchTemplate only runs when debug mode is on.
            res_dbg = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
            loc = (cx - tw // 2, cy - th // 2)

            peaks = []
            _r = res_dbg.copy()
            for _ in range(5):
                _, _s, _, _l = cv2.minMaxLoc(_r)
                if _s < 0.5:
                    break
                _pcx, _pcy = _l[0] + tw // 2, _l[1] + th // 2
                peaks.append((_pcx, _pcy, float(_s)))
                _sx1, _sy1 = max(0, _l[0] - 25), max(0, _l[1] - 25)
                _sx2 = min(_r.shape[1], _l[0] + 25)
                _sy2 = min(_r.shape[0], _l[1] + 25)
                _r[_sy1:_sy2, _sx1:_sx2] = -1.0
            logger.info(
                "[MA DEBUG] top candidates: "
                + ", ".join(f"({px},{py})={ps:.3f}" for px, py, ps in peaks)
            )
            for px, py, ps in peaks[1:]:
                if math.hypot(px - cx, py - cy) > 40 and ps >= cfg.SA_MA_CONFIDENCE:
                    logger.warn(
                        f"[MA DEBUG] AMBIGUOUS: rival candidate ({px},{py})"
                        f"={ps:.3f} is {math.hypot(px-cx, py-cy):.0f}px from the"
                        f" chosen ({cx},{cy})={score:.3f}"
                    )

            import datetime as _dt
            ts      = _dt.datetime.now().strftime("%H-%M-%S.%f")[:-3]
            out_dir = os.path.join("logs", "ma_debug")
            os.makedirs(out_dir, exist_ok=True)
            dpath   = os.path.join(out_dir, f"DET_f{fid:05d}_{ts}"
                                            f"_x{cx}_y{cy}.png")
            _d = frame.copy()
            cv2.rectangle(_d, (loc[0], loc[1]), (loc[0] + tw, loc[1] + th),
                          (0, 255, 255), 1)
            cv2.circle(_d, (cx, cy), 20, (0, 0, 255), 2)
            cv2.putText(_d, f"WIN {score:.3f}", (cx + 24, cy - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
            for px, py, ps in peaks[1:]:
                cv2.circle(_d, (px, py), 14, (255, 160, 0), 2)
                cv2.putText(_d, f"{ps:.3f}", (px + 17, py - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 160, 0), 1,
                            cv2.LINE_AA)
            cv2.putText(_d, f"DETECTION FRAME fid={fid} win=({cx},{cy})"
                            f" score={score:.3f}",
                        (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 255, 255), 2, cv2.LINE_AA)
            cv2.imwrite(dpath, _d)
            logger.info(f"[MA DEBUG] detection frame saved: {dpath}")

            self._ma_detect_dbg = {
                "frame_id": fid,
                "rect":     (loc[0], loc[1], loc[0] + tw, loc[1] + th),
                "centre":   (cx, cy),
                "score":    float(score),
                "t":        time.perf_counter(),
            }

        return cx, cy

    def _sa_pick_ma_pt(
            self,
            sc_x: int, sc_y: int,
            ma_x: int, ma_y: int,
            y_min: Optional[int] = None,
            y_max: Optional[int] = None,
    ) -> Optional[Tuple[int, int]]:
        """Return a random point inside SA_MA_CLICK_AREA for an MA ground click.

        Two placement modes depending on distance from screen centre to anchor:

        ── Normal mode (dist < SA_MA_OFFSET_TRIGGER_PX) ──────────────────────
          SA_MA_CLICK_AREA is centred directly on (ma_x, ma_y).

        ── Directional-offset mode (dist ≥ SA_MA_OFFSET_TRIGGER_PX) ──────────
          The click region is shifted beyond ma_anchor in the direction from the
          screen centre toward ma_anchor.  The horizontal component of that
          direction is weighted by SA_MA_DIRECTION_X_WEIGHT before normalisation
          (same calculation validated in test_ma_calibration.py).

          Region centre = anchor
                          + (SA_MA_REGION_OFFSET_PX + CLICK_AREA//2) × unit_dir

          SA_MA_REGION_OFFSET_PX is the guaranteed empty gap between the anchor
          centre and the nearest EDGE of SA_MA_CLICK_AREA.

        SA_MA_MIN_CLICK_PX minimum distance from the screen centre is enforced
        in both modes.  Returns None if rejection sampling exhausts 200 tries.
        """
        half = cfg.SA_MA_CLICK_AREA // 2
        dist_to_anchor = math.hypot(ma_x - sc_x, ma_y - sc_y)

        # ── Choose region centre ────────────────────────────────────────────
        if dist_to_anchor >= cfg.SA_MA_OFFSET_TRIGGER_PX:
            # Weighted unit vector from screen centre toward anchor.
            dx = ma_x - sc_x
            dy = ma_y - sc_y
            wdx = dx * cfg.SA_MA_DIRECTION_X_WEIGHT
            wdy = dy
            wlen = math.hypot(wdx, wdy)
            if wlen < 1e-6:           # anchor exactly at screen centre
                nx, ny = 0.0, 1.0
            else:
                nx, ny = wdx / wlen, wdy / wlen
            offset = cfg.SA_MA_REGION_OFFSET_PX + half
            cx = ma_x + nx * offset
            cy = ma_y + ny * offset
        else:
            cx = float(ma_x)
            cy = float(ma_y)

        box_x = int(round(cx))
        box_y = int(round(cy))

        # ── Rejection sampling inside the (possibly offset) square ──────────
        for _ in range(200):
            rx = random.randint(box_x - half, box_x + half)
            ry = random.randint(box_y - half, box_y + half)
            if math.hypot(rx - sc_x, ry - sc_y) < cfg.SA_MA_MIN_CLICK_PX:
                continue
            if y_min is not None and ry < y_min:
                continue
            if y_max is not None and ry > y_max:
                continue
            return rx, ry
        return None

    def _sa_pick_click2_pt(
            self,
            prev_pt: Tuple[int, int],
            y_min: Optional[int] = None,
            y_max: Optional[int] = None,
    ) -> Optional[Tuple[int, int]]:
        """Pick the 2nd+ ground-click point relative to the previous click.

        Samples at a random distance in [SA_FALLBACK_CLICK_PROX_MIN,
        SA_FALLBACK_CLICK_PROX_MAX] from *prev_pt* using rejection sampling
        over a random angle.  Only the optional Y-direction constraint
        (y_min / y_max) is applied — there is no area-box restriction.
        """
        lo = cfg.SA_FALLBACK_CLICK_PROX_MIN
        hi = cfg.SA_FALLBACK_CLICK_PROX_MAX
        for _ in range(200):
            angle = random.uniform(0.0, 2.0 * math.pi)
            dist  = random.uniform(lo, hi)
            rx = int(round(prev_pt[0] + dist * math.cos(angle)))
            ry = int(round(prev_pt[1] + dist * math.sin(angle)))
            if y_min is not None and ry < y_min:
                continue
            if y_max is not None and ry > y_max:
                continue
            return rx, ry
        return None

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

    def _rotate_camera_smart(self, nearest: bool = False) -> None:
        """Align the camera to a configured orientation.

        When _camera_orient_1 is set (via viewer UI):
          1. Detect the current minimap-arrow angle.
          2. Choose the target:
               nearest=False (default) — whichever of {orient_1, orient_2} is
                 farther away (switch to opposite side).
               nearest=True           — whichever of {orient_1, orient_2} is
                 nearest (fine-correct after a coarse blind drag).
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

        err_1 = min((cur - orient_1) % 360, (orient_1 - cur) % 360)
        err_2 = min((cur - orient_2) % 360, (orient_2 - cur) % 360)
        if nearest:
            # Fine-correct: go to whichever configured angle we are closest to.
            target = orient_1 if err_1 <= err_2 else orient_2
        else:
            # Switch: go to whichever configured angle is farthest (opposite side).
            target = orient_2 if err_1 <= err_2 else orient_1
        logger.info(f"[BOT] SA: camera {cur}deg -> target {target}deg "
                    f"(orient1={orient_1}, orient2={orient_2}, nearest={nearest})")

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
        # Pre-attack delay commented out — retained for other roles.
        # if cfg.SA_ATTACK_BEFORE_APPROACH:
        #     if random.random() < cfg.SA_PRE_ATTACK_LONG_CHANCE:
        #         pre_delay = random.uniform(cfg.SA_PRE_ATTACK_DELAY_LONG_MIN,
        #                                    cfg.SA_PRE_ATTACK_DELAY_LONG_MAX)
        #         logger.info(f"[BOT] SA: pre-attack long delay {pre_delay:.2f}s (10% roll)")
        #     else:
        #         pre_delay = random.uniform(cfg.SA_PRE_ATTACK_DELAY_MIN,
        #                                    cfg.SA_PRE_ATTACK_DELAY_MAX)
        #         logger.info(f"[BOT] SA: pre-attack delay {pre_delay:.2f}s")
        #     capslock.interruptible_sleep(pre_delay)
        #     capslock.raise_if_on()
        #     logger.info("[BOT] SA: mob confirmed — immediate attack press")
        #     self._press_attack()
        #     capslock.raise_if_on()
        if cfg.SA_ATTACK_BEFORE_APPROACH:
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

    # ------------------------------------------------------------------
    # MA-anchor approach (default "a" mode — no crosshair calibration)
    # ------------------------------------------------------------------

    def _sa_ma_wait_for_anchor(
            self,
            mob_acquired: bool,
    ) -> Tuple[Tuple[int, int], np.ndarray]:
        """Rotate the camera once, then poll every 1 s until ma_anchor.png reappears.

        Each poll iteration runs the full safety checks (buff / death /
        disconnect / notifications) so no important events are missed while
        waiting.  Returns (centre, frame) — the frame is handed back so the
        caller can act on the detection immediately instead of re-grabbing and
        re-matching a position it already has.
        Raises CapsLockPause or StopBot as normal if the pause key is pressed
        or a fatal condition is detected.

        Fallback actions (executed once, immediately after the first failed
        re-check following the camera rotation):

        • mob_acquired=False  →  LMB ×3 on the cached ma1/ma2 position to
                                  start moving toward the MA, then
                                  ASSIST_ATTACK_COUNT (F1 ×N).
        • mob_acquired=True   →  ASSIST_ATTACK_COUNT immediately.

        When mob_acquired=True every subsequent poll also checks the distance
        to in_target_blue/red.  If the mob is within SA_APPROACH_STOP_PX the
        attack burst is repeated before waiting for the next poll.
        """
        logger.info("[BOT] SA-MA: ma_anchor lost — rotating camera, then waiting")
        self._rotate_camera_smart()
        capslock.raise_if_on()
        self._ma_pos = None   # stale ma1/ma2 position cleared

        mon  = self.sct.monitors[1]
        sc_x = mon['width']  // 2
        sc_y = mon['height'] // 2

        def _wait_dot_dist(f: np.ndarray) -> Optional[float]:
            """Distance from screen centre to the closest blue/red dot."""
            dots = _sa_find_blue_dots(f, self._dot_blue_tmpl,
                                      conf=cfg.NC_CONFIDENCE,
                                      nms_dist=cfg.NC_NMS_DIST)
            if not dots:
                dots = _sa_find_blue_dots(f, self._dot_red_tmpl,
                                          conf=cfg.NC_CONFIDENCE,
                                          nms_dist=cfg.NC_NMS_DIST)
            if not dots:
                return None
            return math.hypot(dots[0][0] - sc_x, dots[0][1] - sc_y)

        fallback_done = False
        poll_n = 0
        while True:
            poll_n += 1
            capslock.raise_if_on()
            # Full safety pass — also updates self._ma_pos if ma template visible.
            self._check_buff_and_death()
            frame      = self._grab()
            anchor_pos = self._detect_ma_anchor_pos(frame)
            if anchor_pos is not None:
                logger.info(f"[BOT] SA-MA: ma_anchor reappeared at {anchor_pos}"
                            f" (poll #{poll_n})")
                return anchor_pos, frame

            # ── Fallback actions on first failed re-check ──────────────────
            if not fallback_done:
                fallback_done = True
                rmb_pt = self._ma_pos or self._active.assist_point
                if not mob_acquired:
                    # LMB ×3 toward the MA party-bar icon to start approaching,
                    # then ASSIST_ATTACK_COUNT to take assist + attack.
                    if rmb_pt is not None:
                        logger.info(
                            f"[BOT] SA-MA wait: ma_anchor still absent,"
                            f" mob not acquired — LMB ×3 on ({rmb_pt[0]},{rmb_pt[1]})")
                        for _ in range(3):
                            self.hid.move_to(rmb_pt[0], rmb_pt[1])
                            self.hid.click_left_hold()
                            capslock.interruptible_sleep(0.1)
                    else:
                        logger.info("[BOT] SA-MA wait: ma_anchor still absent,"
                                    " mob not acquired — no ma pos, skipping LMB ×3")
                    logger.info("[BOT] SA-MA wait: pressing ASSIST_ATTACK_COUNT"
                                " after LMB ×3")
                    self._press_attack()
                else:
                    # Mob already targeted — attack immediately while waiting.
                    logger.info("[BOT] SA-MA wait: ma_anchor still absent,"
                                " mob acquired — pressing ASSIST_ATTACK_COUNT")
                    self._press_attack()

            # ── Repeat attack burst when mob is close (mob_acquired path) ──
            if mob_acquired:
                dist = _wait_dot_dist(frame)
                if dist is not None and dist < cfg.SA_APPROACH_STOP_PX:
                    logger.info(
                        f"[BOT] SA-MA wait: within SA_APPROACH_STOP_PX"
                        f" (dist={dist:.0f}px) — pressing ASSIST_ATTACK_COUNT")
                    self._press_attack()

            logger.info(f"[BOT] SA-MA: waiting for ma_anchor (poll #{poll_n})…")
            capslock.interruptible_sleep(1.0)

    def _sa_ma_ground_click(
            self,
            trigger_reason: str,
            mob_acquired: bool,
            close_zone: bool,
            hint_anchor: Optional[Tuple[int, int]] = None,
    ) -> Tuple[Optional[np.ndarray], Optional[Tuple[int, int]],
               Optional[Tuple[int, int]]]:
        """Perform at most one MA ground click, deriving the coordinate itself.

        This is the only function in the "a" assist path permitted to emit a
        ground LMB.  It deliberately accepts no (x, y): callers decide *when* a
        click may happen and can never influence *where* it lands.  Nothing
        runs between the anchor detection and the physical click that could
        substitute or adjust a coordinate.

        *hint_anchor*: when the poll loop already detected ma_anchor on a fresh
        frame (at most SA_APPROACH_POLL_MS ms ago) the caller may pass that
        result here.  The grab+matchTemplate inside this function are then
        skipped entirely, saving ~415 ms per click on an i5 2400.  Only pass a
        value from the immediately preceding poll iteration.

            (if hint_anchor is None)
            fresh full-screen capture → fresh _detect_ma_anchor_pos()
            point drawn only from SA_MA_CLICK_AREA around that anchor
            → physical LMB

        With SA_MA_CLICK_AREA = 0 the final click therefore always equals the
        freshly detected (or just-polled) ma_anchor exactly.

        Returns (frame, anchor, click_pt).  *frame* is the one the coordinate
        was derived from (None when hint_anchor shortcut is used), *anchor* is
        the position used, *click_pt* is None when no LMB was issued.
        """
        if hint_anchor is not None:
            anchor = hint_anchor
            frame  = None
            mon    = self.sct.monitors[1]
            fw, fh = mon['width'], mon['height']
            logger.info("[PERF] capture+match inside click: skipped (poll hint reused)")
        else:
            frame  = self._grab()
            anchor = self._detect_ma_anchor_pos(frame)
            if anchor is None:
                logger.info(f"[BOT] SA-MA: click suppressed — ma_anchor not found"
                            f"  trigger_reason={trigger_reason}")
                return frame, None, None
            fh, fw = frame.shape[:2]

        click_pt = self._sa_pick_ma_pt(fw // 2, fh // 2, anchor[0], anchor[1])
        if click_pt is None:
            logger.warn(f"[BOT] SA-MA: click suppressed — no valid point in"
                        f" SA_MA_CLICK_AREA  trigger_reason={trigger_reason}")
            return frame, anchor, None

        cx, cy = click_pt

        # Push click outside any UI exclusion zone if it landed in one.
        excl = getattr(cfg, "SA_EXCL_ROIS_FHD", []) if fw == 1920 and fh == 1080 else []
        if excl:
            cx, cy = _push_outside_excl(cx, cy, excl)
        click_pt = (cx, cy)

        self.hid.move_to(cx, cy)
        act_x, act_y = _cursor_pos()

        logger.info(
            f"[BOT] SA-MA click:"
            f"  trigger_reason={trigger_reason}"
            f"  mob_acquired={mob_acquired}"
            f"  close_zone={close_zone}"
            f"  fresh_anchor=({anchor[0]},{anchor[1]})"
            f"  final_click=({cx},{cy})"
            f"  cursor_actual=({act_x},{act_y})"
        )

        if getattr(cfg, "SA_MA_ANCHOR_DEBUG", False):
            self._ma_save_click_frame(anchor, (cx, cy), (act_x, act_y))

        self.hid.click_left_hold()
        return frame, anchor, click_pt

    def _ma_save_click_frame(self,
                             anchor: Tuple[int, int],
                             click_pt: Tuple[int, int],
                             cursor: Tuple[int, int]) -> None:
        """Save a pre-LMB screenshot containing the real OS cursor.

        Written immediately before the click so the genuine cursor bitmap can be
        compared against the ma_anchor visible on the same capture.
        """
        import datetime

        live = _grab_screen_with_cursor()
        if live is None:
            logger.warn("[MA DEBUG] GDI capture failed — no image saved")
            return

        dbg = getattr(self, "_ma_detect_dbg", None)
        fid = dbg["frame_id"] if dbg else -1
        cv2.circle(live, anchor, 22, (0, 0, 255), 2)
        cv2.putText(live,
                    f"fid={fid} anchor=({anchor[0]},{anchor[1]})"
                    f" clk=({click_pt[0]},{click_pt[1]})"
                    f" cur=({cursor[0]},{cursor[1]})",
                    (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 2, cv2.LINE_AA)

        out_dir = os.path.join("logs", "ma_debug")
        os.makedirs(out_dir, exist_ok=True)
        ts    = datetime.datetime.now().strftime("%H-%M-%S.%f")[:-3]
        fpath = os.path.join(out_dir,
                             f"f{fid:05d}_{ts}_a{anchor[0]}-{anchor[1]}"
                             f"_clk{click_pt[0]}-{click_pt[1]}.png")
        cv2.imwrite(fpath, live)
        logger.info(f"[MA DEBUG] saved before LMB: {fpath}")
        self._ma_detect_dbg = None

    def _sa_ma_approach(self) -> bool:
        """MA-image-based targeting and approach for the default assist mode.

        Entry point: called at the start of every new cycle (after mob_dead or
        after a CapsLock pause is released).

        Flow
        ----
        1.  Grab a fresh frame; detect active ma1/ma2 anchor.
        2.  RMB on the detected ma position (fall back to assist_point if absent).
        3.  Wait SA_RMB_WAIT_MS, grab again.
        4.  Check mob_anchor and blue/red dots.
            • dots within SA_APPROACH_SKIP_PX → attack immediately → return True.
        5.  Ground-click approach loop:
            • Pick a point inside SA_MA_CLICK_AREA × SA_MA_CLICK_AREA centred on
              ma_anchor, min SA_MA_MIN_CLICK_PX from the screen centre.
            • Perform a validated ground click (blue-glow confirmation).
            • While mob not yet acquired: RMB on ma_pos, check mob_anchor (Esc on miss).
            • Poll until d_to_ma ≤ d_remaining + SA_MA_LEAD_PX (next-click trigger)
              or the safety cap expires.
            • If ma_anchor within SA_MA_CLOSE_PX: 80 % poll-only, 20 % 1-2 fallback
              clicks centred on ma_anchor (using SA_FALLBACK_CLICK_AREA / EXCL).
            • If ma_anchor disappears:
                – mob_anchor present → attack + rotate camera, then continue loop.
                – mob_anchor absent  → rotate camera, then continue loop.
        Exit conditions (from anywhere inside the loop):
            A.  blue/red dots within SA_APPROACH_STOP_PX → attack → return True.
        """
        frame = self._grab()
        fh, fw = frame.shape[:2]
        sc_x, sc_y = fw // 2, fh // 2

        # ── Camera coarse/fine state ──────────────────────────────────────────
        # After any RMB where ma_anchor is detected below screen centre, a
        # single blind drag (coarse) is performed immediately.  _cam_fine_needed
        # is then set so that _maybe_fine_align() fires after the next click,
        # running _rotate_camera_smart(nearest=True) to bring the orientation
        # within CAMERA_ORIENT_TOL_DEG of the nearest configured angle.
        _cam_fine_needed: bool = False

        def _coarse_if_below(apos: Optional[Tuple[int, int]]) -> None:
            """If anchor is in the bottom 33% of the screen → blind 180° drag; arm fine align."""
            nonlocal _cam_fine_needed
            threshold = int(fh * 0.67)
            if apos is not None and apos[1] > threshold:
                logger.info(
                    f"[BOT] SA-MA: ma_anchor in bottom 33% ({apos[1]} > {threshold})"
                    " — coarse camera rotation")
                self.hid.drag_camera(cfg.SA_CAMERA_ROTATE_DX)
                _cam_fine_needed = True

        def _maybe_fine_align() -> None:
            """If a coarse rotation was done, fine-align to the nearest configured angle."""
            nonlocal _cam_fine_needed
            if _cam_fine_needed:
                logger.info("[BOT] SA-MA: fine camera alignment after click")
                self._rotate_camera_smart(nearest=True)
                _cam_fine_needed = False

        def _dot_center(f: np.ndarray) -> Optional[Tuple[int, int]]:
            # Short-circuit: search blue first; skip red when blue is found.
            dots = _sa_find_blue_dots(f, self._dot_blue_tmpl,
                                      conf=cfg.NC_CONFIDENCE,
                                      nms_dist=cfg.NC_NMS_DIST)
            if dots:
                return dots[0]
            dots = _sa_find_blue_dots(f, self._dot_red_tmpl,
                                      conf=cfg.NC_CONFIDENCE,
                                      nms_dist=cfg.NC_NMS_DIST)
            return dots[0] if dots else None

        def _dc(pt: Tuple[int, int]) -> float:
            return math.hypot(pt[0] - sc_x, pt[1] - sc_y)

        # ── Step 1: get ma_pos (RMB target — cached; detect once if not yet set) ──
        # self._ma_pos is normally populated by _check_buff_and_death() before the
        # first approach cycle.  If it still isn't set (very first call at session
        # start before any buff-check ran), detect it now from the already-grabbed
        # frame.  After this, the cached value is reused everywhere — no repeated
        # template searches inside the loop.
        if self._ma_pos is None:
            self._ma_pos = self._detect_ma_pos(frame)
        ma_pos = self._ma_pos or self._active.assist_point

        # ── Step 2: initial RMB ────────────────────────────────────────────
        rmb_target = ma_pos or self._active.assist_point
        if rmb_target is not None:
            logger.info(f"[BOT] SA-MA: initial RMB at {rmb_target}")
            _t0 = time.perf_counter()
            self.hid.move_and_right_click(rmb_target[0], rmb_target[1], wait_after=0)
            logger.info(f"[PERF] RMB cmd: {(time.perf_counter()-_t0)*1000:.0f} ms")
        else:
            logger.warn("[BOT] SA-MA: no ma_pos and no assist_point — skipping RMB")

        # ── Step 3: overlap — ma_anchor on Capture #1 counts toward the wait ─────
        # frame is Capture #1, taken just before the RMB.  ma_anchor position is
        # not changed by an RMB click, so the pre-RMB frame is valid for detection.
        # The detection time absorbs part of SA_RMB_WAIT_MS; only the remainder
        # is slept explicitly.  bag_mob_anchor is checked on Capture #2 below —
        # no second ma_anchor search is performed on that fresh frame.
        _rmb_t = time.perf_counter()
        _t0 = time.perf_counter()
        anchor_pos = self._detect_ma_anchor_pos(frame)
        logger.info(f"[PERF] ma_anchor match: {(time.perf_counter()-_t0)*1000:.0f} ms")
        # Coarse camera rotation if anchor is below screen centre.
        # The drag absorbs part of the remaining wait; no extra verification.
        _coarse_if_below(anchor_pos)
        _remaining = max(0.0, cfg.SA_RMB_WAIT_MS / 1000.0
                         - (time.perf_counter() - _rmb_t))
        if _remaining > 0:
            _t0 = time.perf_counter()
            capslock.interruptible_sleep(_remaining)
            logger.info(
                f"[PERF] RMB wait (remaining): {(time.perf_counter()-_t0)*1000:.0f} ms")
        capslock.raise_if_on()

        # ── Step 4: post-wait capture — bag_mob_anchor only (no ma_anchor re-search)
        _t0 = time.perf_counter()
        frame  = self._grab()
        logger.info(f"[PERF] capture: {(time.perf_counter()-_t0)*1000:.0f} ms")

        mob_acquired = False
        self._mob_anchor.invalidate()
        if self._mob_anchor.find(frame) is not None:
            self._mob_dead_f.invalidate()
            if self._mob_dead_f.find(frame, silent=True) is None:
                mob_acquired = True
                logger.info("[BOT] SA-MA: mob acquired after initial RMB")
            else:
                logger.info("[BOT] SA-MA: bag_mob_anchor + mob_dead — Esc")
                _press(self.hid, "esc")
        else:
            logger.info("[BOT] SA-MA: mob_anchor not found after initial RMB — Esc")
            _press(self.hid, "esc")

        # ── Step 4: SKIP distance check — only after mob is confirmed ─────
        # blue/red dots are not a valid mob indicator until bag_mob_anchor
        # has been found.  Skip entirely if mob_acquired is still False.
        if mob_acquired:
            pair_center = _dot_center(frame)
            if pair_center is not None:
                dist_dots = _dc(pair_center)
                logger.info(f"[BOT] SA-MA: blue/red at {pair_center}, dist={dist_dots:.0f}px")
                if dist_dots < cfg.SA_APPROACH_SKIP_PX:
                    logger.info("[BOT] SA-MA: within SA_APPROACH_SKIP_PX — attacking immediately")
                    self._press_attack()
                    _maybe_fine_align()
                    return True

        # ── Step 5: ground-click approach loop ────────────────────────────
        # Three separate anchors:
        #   ma_pos      — ma1/ma2 template position; used ONLY for RMB clicks.
        #   anchor_pos  — ma_anchor.png position; drives ground-click area,
        #                 close-zone (<SA_MA_CLOSE_PX), and anchor-lost logic.
        #   pair_center — in_target_blue/red midpoint; used ONLY for
        #                 SA_APPROACH_SKIP_PX and SA_APPROACH_STOP_PX checks.
        _in_close_zone     = False
        _close_do_fallback = False   # rolled once when close zone is entered
        _click_ts: float = 0.0       # perf_counter() at the moment of last click
        _reuse_poll_frame = False    # True when poll exited with anchor still valid;
                                     # outer-loop top skips grab+detect when set
        # pair_center already measured on the frame being reused, so the outer
        # loop never repeats the (expensive) blue/red dot search on the same
        # pixels.  None means "not measured for this frame".
        _poll_dots: Optional[Tuple[int, int]] = None
        _poll_dots_valid = False

        while True:
            capslock.raise_if_on()

            # For the 2nd+ ground click the poll loop already confirmed anchor_pos
            # on a fresh frame — reuse both to save one capture + matchTemplate.
            # Always grab fresh for the first click or after any anchor-loss.
            _used_poll_frame = _reuse_poll_frame
            if _reuse_poll_frame:
                _reuse_poll_frame = False
                logger.info("[PERF] capture+match: skipped (reusing poll frame/anchor)")
            else:
                _poll_dots_valid = False   # new frame → cached dots are stale
                _t0    = time.perf_counter()
                frame  = self._grab()
                logger.info(f"[PERF] capture: {(time.perf_counter()-_t0)*1000:.0f} ms")
                _t0    = time.perf_counter()
                anchor_pos = self._detect_ma_anchor_pos(frame)
                logger.info(f"[PERF] ma_anchor match: {(time.perf_counter()-_t0)*1000:.0f} ms")

            # Re-detect ma1/ma2 RMB target if it was cleared externally.  This
            # fires when the viewer switches the MA selection mid-approach (which
            # sets self._ma_pos = None via set_ma_select) or after anchor-loss
            # recovery if _check_buff_and_death() failed to find the template.
            if self._ma_pos is None:
                _t0 = time.perf_counter()
                self._ma_pos = self._detect_ma_pos(frame)
                logger.info(
                    f"[PERF] ma{self._ma_select} re-detect (mid-loop):"
                    f" {(time.perf_counter()-_t0)*1000:.0f} ms"
                    f" → {'found' if self._ma_pos else 'not found'}")
                ma_pos = self._ma_pos or self._active.assist_point

            # ── ma_anchor lost ─────────────────────────────────────────────
            if anchor_pos is None:
                _reuse_poll_frame = False
                self._mob_anchor.invalidate()
                has_mob = self._mob_anchor.find(frame) is not None
                self._mob_dead_f.invalidate()
                mob_dead_now = has_mob and (
                    self._mob_dead_f.find(frame, silent=True) is not None)
                if has_mob and not mob_dead_now:
                    logger.info("[BOT] SA-MA: ma_anchor lost, mob_anchor present"
                                " — attack + rotate + continue")
                    self._press_attack()
                else:
                    logger.info("[BOT] SA-MA: ma_anchor lost — rotate + continue")

                # Invariant: if bag_mob_anchor is now confirmed absent (or dead),
                # mob_acquired must be reset so RMB attempts resume after recovery.
                if mob_acquired and (not has_mob or mob_dead_now):
                    logger.info("[BOT] SA-MA: bag_mob_anchor gone while ma_anchor"
                                " lost — resetting mob_acquired=False")
                    mob_acquired = False

                # Reuse the frame the anchor was actually found on: re-grabbing
                # and re-detecting here only discards a result that is already
                # ~450 ms old and delays restoring SA_MA_CLOSE_PX further.
                anchor_pos, frame = self._sa_ma_wait_for_anchor(mob_acquired)
                _reuse_poll_frame = True
                _poll_dots_valid  = False
                continue

            d_anchor = _dc(anchor_pos)

            # Unconditional per-iteration state dump: makes it visible on every
            # pass whether the bot is holding inside the close zone or is due to
            # keep approaching, without having to infer it from state changes.
            _cz = d_anchor < cfg.SA_MA_CLOSE_PX
            logger.info(
                f"[BOT] SA-MA poll:"
                f"  ma_anchor=({anchor_pos[0]},{anchor_pos[1]})"
                f"  dist_to_center={d_anchor:.0f}"
                f"  SA_MA_CLOSE_PX={cfg.SA_MA_CLOSE_PX}"
                f"  close_zone={_cz}"
                f"  mob_acquired={mob_acquired}"
                f"  action={'hold_position' if _cz else 'continue_MA_clicking'}"
            )

            # ── STOP distance check — only after mob is confirmed ─────────
            if mob_acquired:
                if _used_poll_frame and _poll_dots_valid:
                    # Same frame the poll loop already searched — reuse result.
                    pair_center = _poll_dots
                    logger.info("[PERF] blue/red dots match: reused (same frame)")
                else:
                    _t0 = time.perf_counter()
                    pair_center = _dot_center(frame)
                    logger.info(f"[PERF] blue/red dots match:"
                                f" {(time.perf_counter()-_t0)*1000:.0f} ms")
                _dot_dist_val  = _dc(pair_center) if pair_center is not None else None
                _dot_will_atk  = (_dot_dist_val is not None
                                   and _dot_dist_val < cfg.SA_APPROACH_STOP_PX)
                logger.info(
                    f"[BOT] SA-MA STOP check (outer):"
                    f"  dot_pos={pair_center}"
                    f"  dist_to_center="
                    f"{'N/A' if _dot_dist_val is None else f'{_dot_dist_val:.0f}'}"
                    f"  SA_APPROACH_STOP_PX={cfg.SA_APPROACH_STOP_PX}"
                    f"  attack={_dot_will_atk}"
                )
                if _dot_will_atk:
                    _maybe_fine_align()
                    self._press_attack()
                    return True

                # Dots not visible — mob is already acquired, attack immediately.
                if pair_center is None:
                    logger.info("[BOT] SA-MA: mob acquired, no dots detected — attacking")
                    _maybe_fine_align()
                    self._press_attack()
                    return True

            # ── Close-zone: ma_anchor within SA_MA_CLOSE_PX ───────────────
            if d_anchor < cfg.SA_MA_CLOSE_PX:
                if not _in_close_zone:
                    _in_close_zone     = True
                    _close_do_fallback = (random.random() < cfg.SA_MA_FALLBACK_CHANCE)
                    if _close_do_fallback:
                        logger.info(f"[BOT] SA-MA: ma_anchor within {cfg.SA_MA_CLOSE_PX}px"
                                    " — fallback clicks")
                    else:
                        logger.info(f"[BOT] SA-MA: ma_anchor within {cfg.SA_MA_CLOSE_PX}px"
                                    " — polling only")

                if _close_do_fallback:
                    # Only reachable when SA_MA_FALLBACK_CHANCE > 0: the roll
                    # above is `random.random() < chance`, and random() is
                    # always >= 0.0, so a chance of 0 can never win.
                    _close_do_fallback = False   # only once per close-zone entry
                    n_fb = random.randint(1, 2)
                    for fb_i in range(n_fb):
                        if fb_i > 0:
                            gap_s = random.uniform(
                                0, cfg.SA_FALLBACK_CLICK2_GAP_MAX / 1000.0)
                            capslock.interruptible_sleep(gap_s)
                        frame, cur_anchor, _fb_pt = self._sa_ma_ground_click(
                            trigger_reason=f"close-zone-fallback#{fb_i + 1}",
                            mob_acquired=mob_acquired,
                            close_zone=True)
                        # Ground click is "the next click" after any pending
                        # coarse rotation — fire fine alignment here.
                        _maybe_fine_align()
                        if cur_anchor is None:
                            break
                        anchor_pos = cur_anchor
                else:
                    # Close-zone: ground clicks suppressed.
                    # While mob not yet acquired → RMB → wait → grab → check.
                    # Once mob_acquired → just grab and check STOP; no more RMBs.
                    if not mob_acquired:
                        # Overlap: run ma_anchor on the current pre-RMB frame so
                        # the detection time absorbs part of SA_RMB_WAIT_MS.
                        # A fresh frame is captured afterward only for bag_mob_anchor.
                        rmb_pt = self._ma_pos or self._active.assist_point
                        _rmb_t = time.perf_counter()
                        if rmb_pt is not None:
                            _t0 = time.perf_counter()
                            self.hid.move_and_right_click(
                                rmb_pt[0], rmb_pt[1], wait_after=0)
                            logger.info(
                                f"[PERF] RMB cmd: {(time.perf_counter()-_t0)*1000:.0f} ms")
                        _t0        = time.perf_counter()
                        cur_anchor = self._detect_ma_anchor_pos(frame)
                        logger.info(f"[PERF] ma_anchor match (RMB wait overlap):"
                                    f" {(time.perf_counter()-_t0)*1000:.0f} ms")
                        # This close-zone RMB is "the next click" after any
                        # previous coarse rotation — run fine alignment now.
                        # Then check the freshly-detected anchor for a new coarse.
                        _maybe_fine_align()
                        _coarse_if_below(cur_anchor)
                        _remaining = max(0.0, cfg.SA_RMB_WAIT_MS / 1000.0
                                         - (time.perf_counter() - _rmb_t))
                        if _remaining > 0:
                            _t0 = time.perf_counter()
                            capslock.interruptible_sleep(_remaining)
                            logger.info(f"[PERF] RMB wait (remaining):"
                                        f" {(time.perf_counter()-_t0)*1000:.0f} ms")
                        capslock.raise_if_on()
                        # Post-wait capture: bag_mob_anchor only; no second ma_anchor.
                        _t0   = time.perf_counter()
                        frame = self._grab()
                        logger.info(f"[PERF] capture: {(time.perf_counter()-_t0)*1000:.0f} ms")
                    else:
                        # mob_acquired: no RMB, no wait — grab fresh for STOP check.
                        capslock.raise_if_on()
                        _t0   = time.perf_counter()
                        frame = self._grab()
                        logger.info(f"[PERF] capture: {(time.perf_counter()-_t0)*1000:.0f} ms")
                        _t0        = time.perf_counter()
                        cur_anchor = self._detect_ma_anchor_pos(frame)
                        logger.info(f"[PERF] ma_anchor match:"
                                    f" {(time.perf_counter()-_t0)*1000:.0f} ms")
                    # Keep anchor_pos fresh so the outer loop can reuse this frame
                    if cur_anchor is not None:
                        anchor_pos = cur_anchor
                        if _dc(cur_anchor) >= cfg.SA_MA_CLOSE_PX:
                            _in_close_zone = False
                    else:
                        anchor_pos = None   # outer loop will handle anchor-lost

                    if not mob_acquired:
                        self._mob_anchor.invalidate()
                        if self._mob_anchor.find(frame) is not None:
                            self._mob_dead_f.invalidate()
                            if self._mob_dead_f.find(frame, silent=True) is None:
                                mob_acquired = True
                                logger.info(
                                    "[BOT] SA-MA: mob acquired in close-zone poll")
                                _t0 = time.perf_counter()
                                pair_center = _dot_center(frame)
                                logger.info(f"[PERF] blue/red dots match (acq):"
                                            f" {(time.perf_counter()-_t0)*1000:.0f} ms")
                                # Cache so the next outer-loop iteration can reuse
                                # without repeating the full-frame search.
                                _poll_dots, _poll_dots_valid = pair_center, True
                                _dot_dist_val = (_dc(pair_center)
                                                 if pair_center is not None else None)
                                _dot_will_atk = (_dot_dist_val is not None
                                                 and _dot_dist_val
                                                 < cfg.SA_APPROACH_STOP_PX)
                                logger.info(
                                    f"[BOT] SA-MA STOP check (close-zone acq):"
                                    f"  dot_pos={pair_center}"
                                    f"  dist_to_center="
                                    f"{'N/A' if _dot_dist_val is None else f'{_dot_dist_val:.0f}'}"
                                    f"  SA_APPROACH_STOP_PX={cfg.SA_APPROACH_STOP_PX}"
                                    f"  attack={_dot_will_atk}"
                                )
                                if _dot_will_atk:
                                    _maybe_fine_align()
                                    self._press_attack()
                                    return True
                            else:
                                logger.info("[BOT] SA-MA: close-zone:"
                                            " bag_mob_anchor + mob_dead — Esc")
                                _press(self.hid, "esc")
                        else:
                            logger.info(
                                "[BOT] SA-MA: close-zone: mob_anchor not found — Esc")
                            _press(self.hid, "esc")
                    else:
                        # mob_acquired was True — re-verify bag_mob_anchor is
                        # still present (it can be lost after camera rotations /
                        # Esc presses in anchor-lost recovery).
                        self._mob_anchor.invalidate()
                        if self._mob_anchor.find(frame) is not None:
                            self._mob_dead_f.invalidate()
                            if self._mob_dead_f.find(frame, silent=True) is None:
                                # still targeted — check STOP
                                _t0 = time.perf_counter()
                                pair_center = _dot_center(frame)
                                logger.info(f"[PERF] blue/red dots match:"
                                            f" {(time.perf_counter()-_t0)*1000:.0f} ms")
                                # Cache for the next outer-loop pass, which reuses
                                # this exact frame — otherwise its STOP check would
                                # repeat the same two full-screen searches.
                                _poll_dots, _poll_dots_valid = pair_center, True
                                _dot_dist_val = (_dc(pair_center)
                                                 if pair_center is not None else None)
                                _dot_will_atk = (_dot_dist_val is not None
                                                 and _dot_dist_val
                                                 < cfg.SA_APPROACH_STOP_PX)
                                logger.info(
                                    f"[BOT] SA-MA STOP check (close-zone acq=True):"
                                    f"  dot_pos={pair_center}"
                                    f"  dist_to_center="
                                    f"{'N/A' if _dot_dist_val is None else f'{_dot_dist_val:.0f}'}"
                                    f"  SA_APPROACH_STOP_PX={cfg.SA_APPROACH_STOP_PX}"
                                    f"  attack={_dot_will_atk}"
                                )
                                if _dot_will_atk:
                                    _maybe_fine_align()
                                    self._press_attack()
                                    return True
                            else:
                                logger.info("[BOT] SA-MA: close-zone: acquired but"
                                            " mob_dead — Esc, reset mob_acquired")
                                _press(self.hid, "esc")
                                mob_acquired = False
                        else:
                            logger.info("[BOT] SA-MA: close-zone: bag_mob_anchor"
                                        " lost (was acquired) — Esc, reset mob_acquired")
                            _press(self.hid, "esc")
                            mob_acquired = False

                    # Reuse this frame and anchor in the next outer-loop iteration
                    # to avoid a redundant grab+detect after continue.
                    _reuse_poll_frame = (anchor_pos is not None)
                continue

            # ── Normal approach: ground click toward ma_anchor ─────────────
            _in_close_zone = False

            # Once the mob is acquired the character is already attacking.
            # Do not issue any further ground clicks — just keep polling the
            # STOP-distance check on the next iteration.
            if mob_acquired:
                capslock.raise_if_on()
                continue

            # ── Request a click ─────────────────────────────────────────────
            # When the poll loop already detected ma_anchor on a fresh frame
            # (_used_poll_frame=True), pass that anchor as a hint so
            # _sa_ma_ground_click can skip its own grab+matchTemplate (~415 ms).
            if _click_ts:
                logger.info(f"[PERF] click → next click: {(time.perf_counter()-_click_ts)*1000:.0f} ms")

            _hint = anchor_pos if _used_poll_frame else None
            frame, fresh_anchor, click_pt = self._sa_ma_ground_click(
                trigger_reason="approach",
                mob_acquired=mob_acquired,
                close_zone=False,
                hint_anchor=_hint)
            _click_ts = time.perf_counter()

            if fresh_anchor is None:
                # ma_anchor vanished at click time — let the outer loop's
                # anchor-lost handling deal with it on the next iteration.
                anchor_pos        = None
                _reuse_poll_frame = False
                continue

            anchor_pos  = fresh_anchor
            d_remaining = (math.hypot(click_pt[0] - fresh_anchor[0],
                                      click_pt[1] - fresh_anchor[1])
                           if click_pt is not None else 0.0)

            # Ground click is "the next click" after any previous coarse rotation.
            # Fire fine alignment here; coarse is triggered by RMBs only, not
            # ground clicks, so no _coarse_if_below call at this point.
            _maybe_fine_align()

            capslock.raise_if_on()

            # ── Post-click RMB if mob not yet acquired ─────────────────────
            if not mob_acquired:
                rmb_pt = self._ma_pos or self._active.assist_point
                if rmb_pt is not None:
                    _t0 = time.perf_counter()
                    self.hid.move_and_right_click(rmb_pt[0], rmb_pt[1], wait_after=0)
                    logger.info(f"[PERF] RMB cmd: {(time.perf_counter()-_t0)*1000:.0f} ms")
                # anchor_pos == fresh_anchor: valid position before RMB (RMB does
                # not move the anchor).  Use it to decide if a coarse rotation is
                # needed; the drag absorbs part of SA_RMB_WAIT_MS.
                _coarse_if_below(anchor_pos)
                _t0 = time.perf_counter()
                capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
                logger.info(f"[PERF] RMB wait: {(time.perf_counter()-_t0)*1000:.0f} ms")
                capslock.raise_if_on()
                _t0   = time.perf_counter()
                frame = self._grab()
                logger.info(f"[PERF] capture: {(time.perf_counter()-_t0)*1000:.0f} ms")
                self._mob_anchor.invalidate()
                if self._mob_anchor.find(frame) is not None:
                    self._mob_dead_f.invalidate()
                    if self._mob_dead_f.find(frame, silent=True) is None:
                        mob_acquired = True
                        logger.info("[BOT] SA-MA: mob acquired after ground-click RMB")
                    else:
                        logger.info("[BOT] SA-MA: bag_mob_anchor + mob_dead — Esc")
                        _press(self.hid, "esc")
                else:
                    logger.info("[BOT] SA-MA: mob_anchor not found after RMB — Esc")
                    _press(self.hid, "esc")

            # ── Poll loop: wait for lead trigger or timeout ────────────────
            # Assume anchor will stay valid; cleared inside poll on loss.
            _reuse_poll_frame = True
            max_wait_ms  = random.randint(cfg.SA_APPROACH_MAX_WAIT_MIN_MS,
                                          cfg.SA_APPROACH_MAX_WAIT_MAX_MS)
            poll_deadline = time.perf_counter() + max_wait_ms / 1000.0

            while time.perf_counter() < poll_deadline:
                _t0 = time.perf_counter()
                capslock.interruptible_sleep(cfg.SA_APPROACH_POLL_MS / 1000.0)
                logger.info(f"[PERF] poll wait: {(time.perf_counter()-_t0)*1000:.0f} ms")
                capslock.raise_if_on()
                _t0   = time.perf_counter()
                frame = self._grab()
                logger.info(f"[PERF] capture: {(time.perf_counter()-_t0)*1000:.0f} ms")
                _poll_dots_valid = False   # fresh frame → cached dots are stale

                # ma_anchor drives lead trigger and close-zone
                # ma_pos (ma1/ma2) is cached — never re-searched in poll loop
                _t0        = time.perf_counter()
                new_anchor = self._detect_ma_anchor_pos(frame)
                logger.info(f"[PERF] ma_anchor match: {(time.perf_counter()-_t0)*1000:.0f} ms")
                if new_anchor is None:
                    logger.info("[BOT] SA-MA: ma_anchor lost in poll — exiting poll")
                    _reuse_poll_frame = False   # outer loop must re-grab
                    break

                anchor_pos = new_anchor
                d_now = _dc(anchor_pos)

                _cz = d_now < cfg.SA_MA_CLOSE_PX
                logger.info(
                    f"[BOT] SA-MA poll:"
                    f"  ma_anchor=({anchor_pos[0]},{anchor_pos[1]})"
                    f"  dist_to_center={d_now:.0f}"
                    f"  SA_MA_CLOSE_PX={cfg.SA_MA_CLOSE_PX}"
                    f"  close_zone={_cz}"
                    f"  mob_acquired={mob_acquired}"
                    f"  action={'hold_position' if _cz else 'continue_MA_clicking'}"
                )

                # STOP check — only after mob is confirmed
                if mob_acquired:
                    _t0 = time.perf_counter()
                    pair_center = _dot_center(frame)
                    logger.info(f"[PERF] blue/red dots match:"
                                f" {(time.perf_counter()-_t0)*1000:.0f} ms")
                    # Cache for the outer loop — it reuses this exact frame.
                    _poll_dots, _poll_dots_valid = pair_center, True
                    if pair_center is not None:
                        dist_dots = _dc(pair_center)
                        if dist_dots < cfg.SA_APPROACH_STOP_PX:
                            logger.info(f"[BOT] SA-MA: within SA_APPROACH_STOP_PX"
                                        f" in poll (dist={dist_dots:.0f}px) — attacking")
                            _maybe_fine_align()
                            self._press_attack()
                            return True

                if d_now < cfg.SA_MA_CLOSE_PX:
                    logger.info(f"[BOT] SA-MA: entered close zone in poll"
                                f" (d={d_now:.0f}px) — exiting poll")
                    break   # anchor still valid → _reuse_poll_frame stays True

                if d_now <= d_remaining + cfg.SA_MA_LEAD_PX:
                    logger.info(f"[BOT] SA-MA: next-click trigger"
                                f" (d_now={d_now:.0f}"
                                f" ≤ d_rem={d_remaining:.0f}"
                                f" + lead={cfg.SA_MA_LEAD_PX})")
                    break   # anchor still valid → _reuse_poll_frame stays True
            else:
                logger.info(f"[BOT] SA-MA: poll cap ({max_wait_ms} ms) reached"
                            " — forcing next click")
                # deadline: anchor was valid on last poll iteration
            # end poll loop — outer while continues

    # ------------------------------------------------------------------
    # Ground-click fallback helpers
    # ------------------------------------------------------------------

    def _sa_pick_fallback_pt(
            self,
            sc_x: int, sc_y: int,
            prev_pt: Optional[Tuple[int, int]] = None,
    ) -> Tuple[int, int]:
        """Return a random point in the SA_FALLBACK_CLICK_AREA centred square.

        Excludes a SA_FALLBACK_EXCL_W × SA_FALLBACK_EXCL_H zone directly
        below the screen centre.  If *prev_pt* is given the new point must
        be SA_FALLBACK_CLICK_PROX_MIN–MAX px away and still inside the area.
        """
        half   = cfg.SA_FALLBACK_CLICK_AREA // 2
        ex_hw  = cfg.SA_FALLBACK_EXCL_W // 2

        for _ in range(120):
            rx = random.randint(sc_x - half, sc_x + half)
            ry = random.randint(sc_y - half, sc_y + half)
            # exclusion zone: strip directly below screen centre
            if abs(rx - sc_x) <= ex_hw and sc_y <= ry <= sc_y + cfg.SA_FALLBACK_EXCL_H:
                continue
            if prev_pt is not None:
                d = math.hypot(rx - prev_pt[0], ry - prev_pt[1])
                if not (cfg.SA_FALLBACK_CLICK_PROX_MIN <= d
                        <= cfg.SA_FALLBACK_CLICK_PROX_MAX):
                    continue
            return rx, ry
        # rejection sampling exhausted — safe fallback
        return sc_x, sc_y + half

    def _sa_validated_ground_click(self, cx: int, cy: int,
                                   source: str = "unknown") -> bool:
        """Perform a single LMB ground-click at (cx, cy) — "ac" mode only.

        Reserved for the crosshair/corridor assist mode (_single_assist_cycle_ac),
        which computes its own screen-centred fallback points.  The "a" (MA)
        path must never call this: it routes every ground LMB through
        _sa_ma_ground_click(), which accepts no coordinate at all.

        *source* names the mechanic that produced this coordinate so every
        ground click in the log is attributable.  Always returns True.
        """
        self.hid.move_to(cx, cy)
        self.hid.click_left_hold()
        logger.info(f"[BOT] SA: ground click at ({cx},{cy})  source={source}")
        return True

    # The methods _sa_f5_loop, _sa_phase3_rmb_f5_loop, _sa_center_fallback,
    # and _sa_healer_area_then_f5 have been removed.  Recovery is now handled
    # entirely by _single_assist_cycle's ground-click fallback loop.

    def _sa_f5_loop_REMOVED(self) -> bool:  # kept as tombstone — not called
        pass

    def _single_assist_cycle(self) -> bool:
        """Dispatch to the correct assist targeting implementation.

        "ac" mode (assist_use_crosshair=True):  classic phase-based loop with
            crosshair calibration → _single_assist_cycle_ac().
        "a"  mode (assist_use_crosshair=False): ma1/ma2 image-anchor approach
            → _sa_ma_approach().
        """
        if self._active.assist_use_crosshair:
            return self._single_assist_cycle_ac()
        return self._sa_ma_approach()

    def _single_assist_cycle_ac(self) -> bool:
        """Phase-based target search for single-window assist (crosshair / "ac" mode).

        Linear flow (no outer loop; _cycle() re-calls this for each new mob):

          Phase 1 — SA_RMB_ATTEMPTS RMB clicks at assist_point.
                    Esc after every failed attempt.
                    Found (bag_mob_anchor, no mob_dead) -> phase4 -> return True.

          Phase 2 — ground-click fallback (runs once if Phase 1 fails):
                    random pre-delay -> optional 1-2 validated ground clicks
                    (SA_FALLBACK_SKIP_CHANCE chance of skipping entirely).

          Phase 3 — RMB-only recovery loop (after Phase 2, no more ground clicks):
                    RMB -> wait -> check, repeat until bag_mob_anchor found
                    (with no mob_dead) -> phase4 -> return True.

        Because this method returns as soon as a mob is acquired and phase4
        completes, the next call from _cycle() always starts fresh at Phase 1,
        making the ground-click fallback available again for the next mob.
        """
        pt    = self._active.assist_point
        frame = self._grab()
        fh, fw = frame.shape[:2]
        sc_x, sc_y = fw // 2, fh // 2

        # ---- Phase 1: SA_RMB_ATTEMPTS RMB clicks --------------------------------
        for rmb_i in range(1, cfg.SA_RMB_ATTEMPTS + 1):
            capslock.raise_if_on()
            if pt is not None:
                self.hid.move_and_right_click(pt[0], pt[1], wait_after=0)
            else:
                logger.warn("[BOT] SA: assist_point not set -- skipping RMB")
            capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
            frame = self._grab()
            logger.info(
                f"[BOT] SA phase-1 RMB #{rmb_i}/{cfg.SA_RMB_ATTEMPTS}")
            self._mob_anchor.invalidate()
            if self._mob_anchor.find(frame) is not None:
                self._mob_dead_f.invalidate()
                if self._mob_dead_f.find(frame, silent=True) is not None:
                    logger.info("[BOT] SA phase-1: bag_mob_anchor + mob_dead"
                                " -- dead target, not counting as live")
                else:
                    logger.info("[BOT] SA: bag_mob_anchor found after RMB")
                    return self._sa_phase4(frame)
            capslock.raise_if_on()
            _press(self.hid, "esc")

        # ---- Phase 2: ground-click fallback (runs once) -------------------------
        capslock.raise_if_on()
        pre_delay = random.uniform(cfg.SA_FALLBACK_DELAY_MIN,
                                   cfg.SA_FALLBACK_DELAY_MAX)
        logger.info(f"[BOT] SA fallback: pre-delay {pre_delay:.1f}s")
        capslock.interruptible_sleep(pre_delay)

        if random.random() < cfg.SA_FALLBACK_SKIP_CHANCE:
            logger.info("[BOT] SA fallback: skipping ground clicks this round"
                        f" ({cfg.SA_FALLBACK_SKIP_CHANCE*100:.0f}% chance roll)")
        else:
            n_clicks = random.randint(1, 2)
            logger.info(f"[BOT] SA fallback: {n_clicks} ground click(s)")

            # Click 1 -- retry until blue glow confirmed
            capslock.raise_if_on()
            attempt = 0
            while True:
                attempt += 1
                pt1 = self._sa_pick_fallback_pt(sc_x, sc_y)
                logger.info(
                    f"[BOT] SA fallback: click 1 attempt {attempt} at {pt1}")
                if self._sa_validated_ground_click(pt1[0], pt1[1],
                                                   source="ac-fallback#1"):
                    break
                capslock.raise_if_on()

            # Click 2 (optional) -- retry until confirmed; proximity relative to pt1
            if n_clicks >= 2:
                capslock.raise_if_on()
                gap_s = random.uniform(
                    0, cfg.SA_FALLBACK_CLICK2_GAP_MAX / 1000.0)
                capslock.interruptible_sleep(gap_s)
                attempt = 0
                while True:
                    attempt += 1
                    pt2 = self._sa_pick_fallback_pt(sc_x, sc_y, prev_pt=pt1)
                    logger.info(
                        f"[BOT] SA fallback: click 2 attempt {attempt} at {pt2}")
                    if self._sa_validated_ground_click(pt2[0], pt2[1],
                                                       source="ac-fallback#2"):
                        break
                    capslock.raise_if_on()

        # ---- Phase 3: RMB-only recovery loop ------------------------------------
        # No more ground clicks — just keep right-clicking until a live target
        # appears.  The next call to _single_assist_cycle() (for the next mob)
        # will start fresh at Phase 1, so the ground-click fallback is available
        # again immediately.
        logger.info("[BOT] SA: entering RMB-only recovery loop")
        rmb_n = 0
        while True:
            capslock.raise_if_on()
            rmb_n += 1
            if pt is not None:
                self.hid.move_and_right_click(pt[0], pt[1], wait_after=0)
            else:
                logger.warn(
                    "[BOT] SA: assist_point not set -- skipping recovery RMB")
            capslock.interruptible_sleep(cfg.SA_RMB_WAIT_MS / 1000.0)
            frame = self._grab()
            logger.info(f"[BOT] SA recovery RMB #{rmb_n}")
            self._mob_anchor.invalidate()
            if self._mob_anchor.find(frame) is not None:
                self._mob_dead_f.invalidate()
                if self._mob_dead_f.find(frame, silent=True) is not None:
                    logger.info("[BOT] SA recovery: bag_mob_anchor + mob_dead"
                                " -- dead target, continuing loop")
                    _press(self.hid, "esc")
                    continue
                logger.info("[BOT] SA: bag_mob_anchor found in recovery loop")
                return self._sa_phase4(frame)
            # mob_anchor not found -> clear any accidental selection
            _press(self.hid, "esc")

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

            # RMB miss in assist mode: clear any accidental selection before retry.
            if mode == "assist":
                _press(self.hid, "esc")

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

        # Cache ma1/ma2 position — only search when not yet found (startup or after
        # viewer changes the selection, which clears self._ma_pos via set_ma_select).
        if (self._active.targeting_mode == "assist"
                and not self._active.assist_use_crosshair
                and self._ma_pos is None):
            new_ma = self._detect_ma_pos(frame)
            if new_ma is not None:
                self._ma_pos = new_ma

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
            # Assist mode issues no LMB here under any condition — the stall is
            # resolved purely by returning "stalled" so _cycle restarts targeting.
            if (self._active.targeting_mode == "assist"
                    and not hp_ever_dropped
                    and time.time() > stall_deadline):
                nick = self._active.title
                jitter = random.uniform(cfg.HP_STALL_JITTER_MIN, cfg.HP_STALL_JITTER_MAX)
                logger.info(
                    f"[BOT] [{nick}] Assist HP stall: mob at ≥{cfg.HP_STALL_PCT}%"
                    f" for {cfg.HP_STALL_S}s — jitter {jitter:.1f}s, then restart"
                )
                capslock.interruptible_sleep(jitter)
                wait_s = random.uniform(cfg.SA_STALL_WAIT_MIN, cfg.SA_STALL_WAIT_MAX)
                logger.info(f"[BOT] [{nick}] Pre-restart pause {wait_s:.1f}s")
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
