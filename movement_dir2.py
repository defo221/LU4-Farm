"""
movement_dir2.py
----------------
Phase-correlate multi-region movement direction detector.

Method
------
The screen is divided into 8 large background regions arranged around the
character (N, NE, E, SE, S, SW, W, NW).  For each region:

  crop_A, crop_B  — same region clipped from frame A and frame B
  (dx, dy), response = cv2.phaseCorrelate(crop_A.float(), crop_B.float(), hann)

phaseCorrelate uses FFT cross-correlation, so no feature detection or patch
selection is needed.  Every region gets a displacement vector and a confidence
score without any per-pixel work.

Valid vectors (response > threshold, magnitude in [DISP_MIN, DISP_MAX]) are
combined as a response-weighted average to yield the background drift.

Movement direction = opposite of drift:
  0°  = toward top of screen  (12 o'clock)
  90° = toward right           ( 3 o'clock)
 180° = toward bottom          ( 6 o'clock)
 270° = toward left            ( 9 o'clock)

Perspective note
----------------
In a 3D game, forward movement creates a divergence pattern — not a uniform
drift.  The weighted average still points in the correct direction because the
per-region magnitudes are asymmetric (regions farther from the focus-of-
expansion drift more than regions near it).  A "divergence score" is computed
and displayed to help diagnose cases where the pattern is expansion vs.
pure lateral translation.

Controls
--------
  q  — quit
  d  — toggle per-region debug lines in the console
  f  — freeze / unfreeze
  s  — save current frame pair to logs/movement_dir2/
  +  — widen corridor 5°
  -  — narrow corridor 5°

Usage
-----
  python movement_dir2.py
  python movement_dir2.py --monitor 2
  python movement_dir2.py --interval 80
"""

import argparse
import ctypes
import math
import os
import threading
import time

import cv2
import mss
import numpy as np


# ---------------------------------------------------------------------------
# CapsLock (Windows pause key)
# ---------------------------------------------------------------------------

def _capslock_on() -> bool:
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 0x0001)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FRAME_INTERVAL_MS   =  80     # ms between frame A and frame B

# Region definitions at 1920×1080 baseline: (name, cx, cy, w, h)
# Scaled proportionally to the actual screen resolution at startup.
_REGION_DEFS_FHD = [
    ('N',    960,  255,  280, 165),
    ('NE', 1455,  280,  210, 165),
    ('E',  1700,  540,  190, 210),
    ('SE', 1455,  800,  210, 165),
    ('S',    960,  780,  280, 155),
    ('SW',  505,  800,  210, 165),
    ('W',    320,  540,  190, 210),
    ('NW',  465,  280,  210, 165),
]

# UI exclusion zones (same as SA_EXCL_ROIS_FHD, baseline 1920×1080)
_EXCL_FHD = [
    (   0,    0,  384,   56),
    ( 844,    0, 1249,  110),
    (1642,    0, 1919,  275),
    (   0,  200,  260,  686),
    (   0,  686,  381, 1048),
    ( 752,  837, 1273, 1048),
    (1868,  922, 1919,  990),
    (1504,  992, 1919, 1048),
    (   0, 1049, 1919, 1079),
]

CENTER_EXCL_R        = 130    # px at 1920×1080 baseline

REGION_MIN_VALID_FRAC = 0.50  # skip region if > 50 % of its pixels are in UI zones

RESPONSE_THRESHOLD   = 0.015  # minimum phaseCorrelate response to accept a vector
DISP_MIN             =  0.15  # px — smaller = stationary noise
DISP_MAX             = 70.0   # px — larger = something other than normal movement

MIN_VALID_REGIONS    =  2     # need at least this many passing regions for a reading

# Smoothing
SMOOTH_ALPHA_MIN     = 0.30   # used when only MIN_VALID_REGIONS pass
SMOOTH_ALPHA_MAX     = 0.65   # used when all 8 regions pass
STALE_RESET_COUNT    =  5     # resets smoothed angle after N consecutive misses

CORRIDOR_HALF_DEG    = 45.0   # ± corridor half-width (adjustable at runtime)

# Display
PREVIEW_W  = 640
COMPASS_SZ = 280
LIVE_MS    =  16


# ---------------------------------------------------------------------------
# Mask + regions
# ---------------------------------------------------------------------------

def _build_mask(sw: int, sh: int) -> np.ndarray:
    sx, sy = sw / 1920.0, sh / 1080.0
    mask = np.full((sh, sw), 255, dtype=np.uint8)
    for x1, y1, x2, y2 in _EXCL_FHD:
        cv2.rectangle(mask,
                      (int(x1 * sx), int(y1 * sy)),
                      (int(x2 * sx), int(y2 * sy)),
                      0, -1)
    r = int(CENTER_EXCL_R * min(sx, sy))
    cv2.circle(mask, (sw // 2, sh // 2), r, 0, -1)
    return mask


def _build_regions(sw: int, sh: int, mask: np.ndarray) -> list[dict]:
    """Scale region definitions to actual screen resolution; compute valid fractions."""
    sx, sy = sw / 1920.0, sh / 1080.0
    regions = []
    for name, cx0, cy0, w0, h0 in _REGION_DEFS_FHD:
        cx = int(cx0 * sx)
        cy = int(cy0 * sy)
        w  = max(64, int(w0 * sx))
        h  = max(64, int(h0 * sy))
        # Force even dimensions (FFT prefers power-of-2 but even is sufficient)
        w += w % 2
        h += h % 2
        x1 = max(0, cx - w // 2)
        y1 = max(0, cy - h // 2)
        x2 = min(sw, x1 + w)
        y2 = min(sh, y1 + h)
        aw, ah = x2 - x1, y2 - y1
        if aw < 64 or ah < 64:
            continue
        region_mask = mask[y1:y2, x1:x2]
        valid_frac = float(np.count_nonzero(region_mask)) / (aw * ah)
        regions.append(dict(
            name=name,
            x1=x1, y1=y1, x2=x2, y2=y2,
            cx=(x1 + x2) // 2,
            cy=(y1 + y2) // 2,
            valid_frac=valid_frac,
        ))
    return regions


# ---------------------------------------------------------------------------
# Hanning window cache
# ---------------------------------------------------------------------------

_hann_cache: dict[tuple[int, int], np.ndarray] = {}

def _hann(w: int, h: int) -> np.ndarray:
    key = (w, h)
    if key not in _hann_cache:
        _hann_cache[key] = cv2.createHanningWindow((w, h), cv2.CV_32F)
    return _hann_cache[key]


# ---------------------------------------------------------------------------
# Phase-correlate flow computation
# ---------------------------------------------------------------------------

def _compute(gray_a: np.ndarray,
             gray_b: np.ndarray,
             regions: list[dict]) -> tuple[tuple | None, int, float, list[dict], dict]:
    """
    Run phaseCorrelate on each region.

    Returns
    -------
    drift       : (dx, dy) weighted-average background drift, or None
    n_valid     : number of regions that contributed
    confidence  : mean response of contributing regions
    region_data : per-region results for display
    dbg         : debug dict
    """
    region_data = []
    valid_vecs  = []     # [(dx, dy, response, cx, cy), ...]
    dbg = dict(total=len(regions), skipped_mask=0, skipped_response=0,
               skipped_disp=0, valid=0, best_response=0.0)

    for reg in regions:
        x1, y1, x2, y2 = reg['x1'], reg['y1'], reg['x2'], reg['y2']

        entry = dict(name=reg['name'],
                     x1=reg['x1'], y1=reg['y1'],
                     x2=reg['x2'], y2=reg['y2'],
                     cx=reg['cx'], cy=reg['cy'],
                     dx=0.0, dy=0.0, response=0.0,
                     valid_frac=reg['valid_frac'], status='mask')

        if reg['valid_frac'] < REGION_MIN_VALID_FRAC:
            dbg['skipped_mask'] += 1
            region_data.append(entry)
            continue

        crop_a = gray_a[y1:y2, x1:x2].astype(np.float32)
        crop_b = gray_b[y1:y2, x1:x2].astype(np.float32)
        h, w = crop_a.shape
        win = _hann(w, h)

        (dx, dy), response = cv2.phaseCorrelate(crop_a, crop_b, win)
        entry['dx'] = dx
        entry['dy'] = dy
        entry['response'] = response
        dbg['best_response'] = max(dbg['best_response'], response)

        if response < RESPONSE_THRESHOLD:
            entry['status'] = 'response'
            dbg['skipped_response'] += 1
            region_data.append(entry)
            continue

        mag = math.hypot(dx, dy)
        if not (DISP_MIN <= mag <= DISP_MAX):
            entry['status'] = 'disp'
            dbg['skipped_disp'] += 1
            region_data.append(entry)
            continue

        entry['status'] = 'ok'
        dbg['valid'] += 1
        valid_vecs.append((dx, dy, response, reg['cx'], reg['cy']))
        region_data.append(entry)

    n_valid = len(valid_vecs)
    if n_valid < MIN_VALID_REGIONS:
        return None, n_valid, 0.0, region_data, dbg

    total_w  = sum(v[2] for v in valid_vecs)
    avg_dx   = sum(v[0] * v[2] for v in valid_vecs) / total_w
    avg_dy   = sum(v[1] * v[2] for v in valid_vecs) / total_w
    avg_resp = total_w / n_valid

    # Divergence score: measures expansion vs translation pattern.
    # dot(region_to_center_unit_vec, drift_vec) > 0 → expansion from center.
    # We skip the center itself and weight by magnitude so weak regions don't dominate.
    scx = gray_a.shape[1] // 2
    scy = gray_a.shape[0] // 2
    div_dots = []
    for dx, dy, resp, cx, cy in valid_vecs:
        rcx, rcy = cx - scx, cy - scy
        rlen = math.hypot(rcx, rcy)
        if rlen > 0:
            ux, uy = rcx / rlen, rcy / rlen
            div_dots.append((dx * ux + dy * uy) * resp)
    div_score = sum(div_dots) / total_w if div_dots else 0.0

    dbg['div_score'] = div_score
    dbg['avg_resp']  = avg_resp

    return (avg_dx, avg_dy), n_valid, avg_resp, region_data, dbg


# ---------------------------------------------------------------------------
# Direction helpers
# ---------------------------------------------------------------------------

def _drift_to_dir(dx: float, dy: float) -> float:
    """
    Background drift (dx, dy) → character movement direction.
    0° = up / north, 90° = right / east, etc.
    """
    return math.degrees(math.atan2(-dx, dy)) % 360.0


def _smooth_angle(prev: float | None, new: float, alpha: float) -> float:
    if prev is None:
        return new
    pr, nr = math.radians(prev), math.radians(new)
    x = (1.0 - alpha) * math.cos(pr) + alpha * math.cos(nr)
    y = (1.0 - alpha) * math.sin(pr) + alpha * math.sin(nr)
    return math.degrees(math.atan2(y, x)) % 360.0


# ---------------------------------------------------------------------------
# Compass rose
# ---------------------------------------------------------------------------

def _draw_compass(direction: float | None,
                  n_valid: int,
                  confidence: float,
                  corridor_half: float,
                  sz: int = COMPASS_SZ) -> np.ndarray:
    canvas = np.zeros((sz, sz, 3), dtype=np.uint8)
    cx, cy, r = sz // 2, sz // 2, sz // 2 - 20

    cv2.circle(canvas, (cx, cy), r, (55, 55, 55), 1)
    for deg, lbl in [(0, 'N'), (90, 'E'), (180, 'S'), (270, 'W')]:
        rad = math.radians(deg)
        ix = int(cx + r * math.sin(rad));  iy = int(cy - r * math.cos(rad))
        ox = int(cx + (r-10) * math.sin(rad)); oy = int(cy - (r-10) * math.cos(rad))
        cv2.line(canvas, (ox, oy), (ix, iy), (70, 70, 70), 2)
        lx = int(cx + (r+12) * math.sin(rad)); ly = int(cy - (r+12) * math.cos(rad))
        cv2.putText(canvas, lbl, (lx-5, ly+5), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (100, 100, 100), 1)
    for deg in range(45, 360, 90):
        rad = math.radians(deg)
        ix = int(cx + r*math.sin(rad)); iy = int(cy - r*math.cos(rad))
        ox = int(cx + (r-6)*math.sin(rad)); oy = int(cy - (r-6)*math.cos(rad))
        cv2.line(canvas, (ox, oy), (ix, iy), (50, 50, 50), 1)

    if direction is not None and n_valid >= MIN_VALID_REGIONS:
        angles = np.linspace(math.radians(direction - corridor_half),
                             math.radians(direction + corridor_half), 32)
        pts = [(cx, cy)] + [(cx + int(r*math.sin(a)), cy - int(r*math.cos(a)))
                            for a in angles]
        overlay = canvas.copy()
        cv2.fillConvexPoly(overlay, np.array(pts, np.int32), (0, 80, 0))
        cv2.addWeighted(overlay, 0.45, canvas, 0.55, 0, canvas)

        for off in (-corridor_half, corridor_half):
            ra = math.radians(direction + off)
            cv2.line(canvas, (cx, cy),
                     (cx + int(r*math.sin(ra)), cy - int(r*math.cos(ra))),
                     (0, 140, 0), 1)

        rad = math.radians(direction)
        cv2.arrowedLine(canvas,
                        (cx, cy),
                        (cx + int(r*math.sin(rad)), cy - int(r*math.cos(rad))),
                        (0, 240, 0), 2, tipLength=0.25)

        conf_r = int(r * 0.22 * min(confidence / 0.15, 1.0))
        if conf_r > 1:
            cv2.circle(canvas, (cx, cy), conf_r, (0, 160, 200), -1)

        cv2.putText(canvas, f"{direction:.0f}\u00b0",
                    (cx - 18, cy + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 230, 0), 1)
    else:
        cv2.putText(canvas, "NO SIGNAL",
                    (cx - 36, cy + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.44,
                    (80, 80, 80), 1)
        if n_valid > 0:
            cv2.putText(canvas, f"({n_valid} regions)",
                        (cx - 28, cy + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                        (60, 60, 60), 1)

    return canvas


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.lock        = threading.Lock()
        self.direction   : float | None = None
        self.confidence  : float = 0.0
        self.n_valid     : int   = 0
        self.region_data : list  = []
        self.frame_bgr   : np.ndarray | None = None
        self.div_score   : float = 0.0
        self.frozen      : bool  = False
        self.save_req    : bool  = False
        self.save_a      : np.ndarray | None = None
        self.save_b      : np.ndarray | None = None
        self.dbg         : dict  = {}
        self.stale_count : int   = 0


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _worker(state: _State,
            monitor: dict,
            regions: list[dict],
            interval_ms: float,
            stop_evt: threading.Event) -> None:
    with mss.MSS() as sct:
        while not stop_evt.is_set():
            with state.lock:
                frozen = state.frozen
            if frozen or _capslock_on():
                time.sleep(0.05)
                continue

            shot_a = sct.grab(monitor)
            bgr_a  = np.array(shot_a)[:, :, :3]
            gray_a = cv2.cvtColor(bgr_a, cv2.COLOR_BGR2GRAY)

            time.sleep(interval_ms / 1000.0)

            shot_b = sct.grab(monitor)
            bgr_b  = np.array(shot_b)[:, :, :3]
            gray_b = cv2.cvtColor(bgr_b, cv2.COLOR_BGR2GRAY)

            drift, n_valid, confidence, region_data, dbg = _compute(
                gray_a, gray_b, regions
            )

            with state.lock:
                state.frame_bgr   = bgr_b
                state.n_valid     = n_valid
                state.confidence  = confidence
                state.region_data = region_data
                state.div_score   = dbg.get('div_score', 0.0)
                state.dbg         = dbg

                if drift is not None:
                    raw_dir = _drift_to_dir(*drift)
                    conf_ratio = min(1.0, n_valid / max(len(regions), 1))
                    alpha = (SMOOTH_ALPHA_MIN
                             + (SMOOTH_ALPHA_MAX - SMOOTH_ALPHA_MIN) * conf_ratio)
                    state.direction  = _smooth_angle(state.direction, raw_dir, alpha)
                    state.stale_count = 0
                else:
                    state.stale_count += 1
                    if state.stale_count >= STALE_RESET_COUNT:
                        state.direction   = None
                        state.stale_count = 0

                if state.save_req:
                    state.save_a = bgr_a.copy()
                    state.save_b = bgr_b.copy()
                    state.save_req = False


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATUS_COLOR = {
    'ok':       (0,   200,  60),   # green  — used
    'disp':     (0,   140, 220),   # blue   — displacement out of range
    'response': (0,    80, 180),   # dark blue — low response
    'mask':     (60,   60,  60),   # dark gray  — masked out
}


def _render(state: _State,
            sw: int, sh: int,
            corridor_half: float) -> np.ndarray | None:
    with state.lock:
        frame_bgr   = state.frame_bgr
        direction   = state.direction
        confidence  = state.confidence
        n_valid     = state.n_valid
        region_data = list(state.region_data)
        div_score   = state.div_score
        frozen      = state.frozen

    if frame_bgr is None:
        return None

    # ── Preview ──────────────────────────────────────────────────────────
    ph = int(PREVIEW_W * sh / sw)
    preview = cv2.resize(frame_bgr, (PREVIEW_W, ph),
                         interpolation=cv2.INTER_LINEAR)
    psx, psy = PREVIEW_W / sw, ph / sh

    # Region boxes and displacement arrows
    ARROW_SCALE = 6.0   # amplify small drifts for visibility
    for reg in region_data:
        x1p = int(reg['x1'] * psx);  y1p = int(reg['y1'] * psy)
        x2p = int(reg['x2'] * psx);  y2p = int(reg['y2'] * psy)
        cxp = int(reg['cx'] * psx);  cyp = int(reg['cy'] * psy)
        col = _STATUS_COLOR.get(reg['status'], (100, 100, 100))

        cv2.rectangle(preview, (x1p, y1p), (x2p, y2p), col, 1)
        # Region name + response
        cv2.putText(preview, f"{reg['name']} {reg['response']:.3f}",
                    (x1p + 3, y1p + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, col, 1)

        if reg['status'] == 'ok':
            adx = int(reg['dx'] * ARROW_SCALE)
            ady = int(reg['dy'] * ARROW_SCALE)
            tip_x = min(max(cxp + adx, 0), PREVIEW_W - 1)
            tip_y = min(max(cyp + ady, 0), ph - 1)
            if abs(adx) > 1 or abs(ady) > 1:
                cv2.arrowedLine(preview, (cxp, cyp), (tip_x, tip_y),
                                (0, 255, 100), 2, tipLength=0.35)
            else:
                cv2.circle(preview, (cxp, cyp), 3, (0, 180, 80), -1)

    # Global direction arrow from screen centre
    pcx, pcy = PREVIEW_W // 2, ph // 2
    if direction is not None and n_valid >= MIN_VALID_REGIONS:
        arrow_r = min(PREVIEW_W, ph) // 4
        rad = math.radians(direction)
        ax = pcx + int(arrow_r * math.sin(rad))
        ay = pcy - int(arrow_r * math.cos(rad))
        for off in (-corridor_half, corridor_half):
            ra = math.radians(direction + off)
            cv2.line(preview, (pcx, pcy),
                     (pcx + int(arrow_r*math.sin(ra)),
                      pcy - int(arrow_r*math.cos(ra))),
                     (0, 160, 0), 1)
        cv2.arrowedLine(preview, (pcx, pcy), (ax, ay),
                        (0, 240, 0), 3, tipLength=0.22)

    # Centre exclusion circle
    cr = int(CENTER_EXCL_R * min(psx, psy))
    cv2.circle(preview, (pcx, pcy), cr, (50, 50, 0), 1)

    # ── Compass ──────────────────────────────────────────────────────────
    compass = _draw_compass(direction, n_valid, confidence, corridor_half)
    if compass.shape[0] != ph:
        pad_t = max(0, (ph - COMPASS_SZ) // 2)
        pad_b = max(0, ph - COMPASS_SZ - pad_t)
        compass = np.vstack([np.zeros((pad_t, COMPASS_SZ, 3), np.uint8),
                             compass,
                             np.zeros((pad_b, COMPASS_SZ, 3), np.uint8)])
    compass = compass[:ph]

    row = np.hstack([preview, np.zeros((ph, 10, 3), np.uint8), compass])

    # ── Status bar ───────────────────────────────────────────────────────
    bar = np.zeros((26, row.shape[1], 3), np.uint8)
    paused = _capslock_on()
    if paused:
        txt   = "  CAPS PAUSED"
        color = (0, 100, 220)
    elif direction is not None and n_valid >= MIN_VALID_REGIONS:
        lo = (direction - corridor_half) % 360
        hi = (direction + corridor_half) % 360
        abs_div = abs(div_score)
        if abs_div > 0.4:
            mode = f"EXPAND({div_score:+.2f})" if div_score > 0 else f"CONTRACT({div_score:+.2f})"
        else:
            mode = f"TRANS({div_score:+.2f})"
        txt   = (f"  dir={direction:.1f}\u00b0  "
                 f"[{lo:.0f}\u00b0\u2013{hi:.0f}\u00b0]  "
                 f"conf={confidence:.3f}  regions={n_valid}  {mode}"
                 + ("  [FROZEN]" if frozen else ""))
        color = (0, 220, 0)
    else:
        txt   = (f"  NO SIGNAL  valid_regions={n_valid}"
                 + ("  [FROZEN]" if frozen else ""))
        color = (60, 60, 180)

    cv2.putText(bar, txt, (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)

    hint = "q=quit  d=debug  f=freeze  s=save  +/-=corridor"
    (tw, _), _ = cv2.getTextSize(hint, cv2.FONT_HERSHEY_SIMPLEX, 0.33, 1)
    cv2.putText(bar, hint, (row.shape[1] - tw - 6, 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.33, (70, 70, 70), 1)

    return np.vstack([row, bar])


# ---------------------------------------------------------------------------
# Live loop
# ---------------------------------------------------------------------------

def run_live(monitor_idx: int, interval_ms: float) -> None:
    with mss.MSS() as sct:
        mons = sct.monitors
        if monitor_idx < 1 or monitor_idx >= len(mons):
            print(f"Monitor {monitor_idx} not found (available 1–{len(mons)-1})")
            return
        mon = mons[monitor_idx]

    sw, sh = mon['width'], mon['height']
    monitor = {'left': mon['left'], 'top': mon['top'],
               'width': sw, 'height': sh}

    mask    = _build_mask(sw, sh)
    regions = _build_regions(sw, sh, mask)

    print(f"Monitor {monitor_idx}: {sw}×{sh}")
    print(f"Frame interval : {interval_ms:.0f} ms  "
          f"(~{1000/interval_ms:.1f} updates/sec)")
    print("Regions:")
    for r in regions:
        print(f"  {r['name']:2s}  ({r['x1']:4d},{r['y1']:4d})–({r['x2']:4d},{r['y2']:4d})"
              f"  valid={r['valid_frac']:.0%}")
    print()
    print("Press q to quit.  d=debug  f=freeze  s=save  +/-=corridor\n")

    state      = _State()
    stop_evt   = threading.Event()
    corridor   = CORRIDOR_HALF_DEG
    debug_mode = True

    worker = threading.Thread(
        target=_worker,
        args=(state, monitor, regions, interval_ms, stop_evt),
        daemon=True,
    )
    worker.start()

    cv2.namedWindow("Movement Dir 2", cv2.WINDOW_AUTOSIZE)

    while True:
        panel = _render(state, sw, sh, corridor)
        if panel is not None:
            cv2.imshow("Movement Dir 2", panel)

        if debug_mode:
            with state.lock:
                dbg = dict(state.dbg)
                d   = state.direction
                nv  = state.n_valid
            if dbg:
                skipped = (dbg.get('skipped_mask', 0),
                           dbg.get('skipped_response', 0),
                           dbg.get('skipped_disp', 0))
                print(
                    f"\r  total={dbg.get('total',0)}  "
                    f"skip(mask={skipped[0]} resp={skipped[1]} disp={skipped[2]})  "
                    f"valid={dbg.get('valid',0)}  "
                    f"best_resp={dbg.get('best_response',0):.4f}  "
                    f"div={dbg.get('div_score',0):+.2f}  "
                    f"dir={f'{d:.0f}°' if d is not None and nv >= MIN_VALID_REGIONS else '---':>6s}   ",
                    end="", flush=True,
                )

        key = cv2.waitKey(LIVE_MS) & 0xFF
        if key == ord('q'):
            break

        if key == ord('d'):
            debug_mode = not debug_mode
            if not debug_mode:
                print()

        if key == ord('f'):
            with state.lock:
                state.frozen = not state.frozen
            print(f"\r  [{'FROZEN' if state.frozen else 'RUNNING'}]" + " "*30,
                  end="", flush=True)

        if key == ord('s'):
            with state.lock:
                state.save_req = True
            time.sleep(0.3)
            with state.lock:
                fa, fb = state.save_a, state.save_b
            if fa is not None:
                os.makedirs("logs/movement_dir2", exist_ok=True)
                ts = int(time.time() * 1000)
                cv2.imwrite(f"logs/movement_dir2/{ts}_a.png", fa)
                cv2.imwrite(f"logs/movement_dir2/{ts}_b.png", fb)
                print(f"\n  Saved logs/movement_dir2/{ts}_a/b.png")

        if key in (ord('+'), ord('=')):
            corridor = min(corridor + 5.0, 90.0)
            print(f"\r  corridor ±{corridor:.0f}°" + " "*20, end="", flush=True)

        if key == ord('-'):
            corridor = max(corridor - 5.0, 10.0)
            print(f"\r  corridor ±{corridor:.0f}°" + " "*20, end="", flush=True)

    stop_evt.set()
    worker.join(timeout=2.0)
    cv2.destroyAllWindows()
    print("\nDone.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Phase-correlate multi-region movement direction detector"
    )
    ap.add_argument("--monitor", type=int, default=1)
    ap.add_argument("--interval", type=float, default=float(FRAME_INTERVAL_MS),
                    metavar="MS")
    args = ap.parse_args()
    run_live(args.monitor, args.interval)


if __name__ == "__main__":
    main()
