"""
movement_dir.py
---------------
Real-time character movement direction detector using template-patch matching.

Method
------
1. Grab frame A (full resolution grayscale).
2. Wait FRAME_INTERVAL_MS.
3. Grab frame B (full resolution grayscale).
4. Choose up to N_PATCHES candidate locations in the background (trackable mask,
   away from UI and center character disk).  Prefer high-variance patches
   (rich texture → reliable match).
5. For each chosen patch:
     a. Extract a PATCH_SIZE × PATCH_SIZE crop from frame A.
     b. Search for it in a (PATCH_SIZE + 2*SEARCH_PAD)² window centred on the
        same position in frame B using cv2.matchTemplate (TM_CCOEFF_NORMED).
     c. Accept the match only if NCC score ≥ MATCH_MIN_SCORE and the resulting
        displacement magnitude is within [DISP_MIN, DISP_MAX].
6. Angular consistency filter: use the component-wise median of surviving
   displacement vectors as the consensus direction; reject vectors deviating
   more than ANGULAR_FILTER_DEG from it (removes moving entities / anomalies).
7. Average the remaining inlier vectors → background drift.
8. Movement direction = opposite of drift (0° = up, 90° = right, …).

Angle convention (same as minimap_orient):
  0°  = moving toward top of screen  (12 o'clock)
  90° = moving toward right of screen ( 3 o'clock)
 180° = moving toward bottom of screen ( 6 o'clock)
 270° = moving toward left of screen  ( 9 o'clock)

Controls:
  q  — quit
  f  — freeze / unfreeze (hold last result, stop grabbing)
  s  — save current frame pair to logs/movement_dir/
  d  — toggle console debug output (cand / score_ok / disp_ok / inliers)
  +  — widen corridor by 5°
  -  — narrow corridor by 5°

Usage:
  python movement_dir.py
  python movement_dir.py --monitor 2
  python movement_dir.py --interval 150
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


# CapsLock state check (Windows only)
def _capslock_on() -> bool:
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 0x0001)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Frame pair interval (ms between grab A and grab B)
FRAME_INTERVAL_MS = 100

# --- Template patch parameters ---
PATCH_SIZE       =  48    # px — square patch extracted from frame A
SEARCH_PAD       =  40    # px — search window = (PATCH_SIZE + 2*SEARCH_PAD)² in B
PATCH_GRID_STEP  = 150    # px — grid spacing when sampling candidate patch centres
PATCH_MIN_VAR    = 120.0  # minimum pixel variance — skip flat / featureless patches
MATCH_MIN_SCORE  =  0.55  # minimum NCC score to accept a displacement
N_PATCHES        =  12    # maximum patches to track per frame pair

# --- Displacement filtering ---
DISP_MIN         =  0.5   # px — smaller than this is stationary noise
DISP_MAX         = 35.0   # px — larger than this is a fast-moving entity

# --- Angular consistency filter ---
ANGULAR_FILTER_DEG = 55   # reject vectors deviating more than this from the median

# --- Minimum accepted patches ---
FLOW_MIN_PTS     =   4    # minimum inliers for a reliable direction reading

# Exclusion zones (from config.SA_EXCL_ROIS_FHD, baseline 1920×1080)
_EXCL_FHD = [
    (   0,    0,  384,   56),   # top-left corner  (character / party bars)
    ( 844,    0, 1249,  110),   # top-center band  (target window)
    (1642,    0, 1919,  275),   # top-right strip  (minimap + buffs)
    (   0,  200,  260,  686),   # left strip — narrow
    (   0,  686,  381, 1048),   # left strip — wide
    ( 752,  837, 1273, 1048),   # bottom-center block (inventory / skills)
    (1868,  922, 1919,  990),   # bottom-right small stub
    (1504,  992, 1919, 1048),   # bottom-right large block
    (   0, 1049, 1919, 1079),   # bottom strip (system bar)
]

# Exclude a disk around screen centre (character model + targeting reticle)
CENTER_EXCL_R = 130    # px at 1920×1080 baseline; scaled proportionally

# Smoothing: confidence-weighted EMA.
# When all N_PATCHES patches agree, SMOOTH_ALPHA_MAX is used → fast convergence.
# When only the minimum FLOW_MIN_PTS patches pass, SMOOTH_ALPHA_MIN is used → stable.
# First reading after a stale reset always snaps instantly (no blending).
SMOOTH_ALPHA_MIN = 0.25   # used when n_inlier == FLOW_MIN_PTS (shaky reading)
SMOOTH_ALPHA_MAX = 0.70   # used when n_inlier == N_PATCHES   (strong reading)

# If no valid direction is produced for this many consecutive updates, reset the
# smoothed angle to None so the NEXT valid reading snaps in immediately.
STALE_RESET_COUNT = 5

# Corridor half-width displayed (adjustable at runtime with +/-)
CORRIDOR_HALF_DEG_DEFAULT = 45

# Display
PREVIEW_W  = 640   # game-frame preview width (height auto from aspect ratio)
COMPASS_SZ = 280   # compass rose square size
LIVE_MS    =  16   # cv2.waitKey interval (ms) — display framerate


# ---------------------------------------------------------------------------
# Mask builder
# ---------------------------------------------------------------------------

def _build_mask(screen_h: int, screen_w: int) -> np.ndarray:
    """
    uint8 mask: 255 = trackable background, 0 = excluded (UI / character).
    UI zones are scaled linearly from the 1920×1080 baseline.
    """
    sx = screen_w / 1920.0
    sy = screen_h / 1080.0
    mask = np.full((screen_h, screen_w), 255, dtype=np.uint8)
    for x1, y1, x2, y2 in _EXCL_FHD:
        cv2.rectangle(mask,
                      (int(x1 * sx), int(y1 * sy)),
                      (int(x2 * sx), int(y2 * sy)),
                      0, -1)
    r = int(CENTER_EXCL_R * min(sx, sy))
    cv2.circle(mask, (screen_w // 2, screen_h // 2), r, 0, -1)
    return mask


# ---------------------------------------------------------------------------
# Template-patch tracking
# ---------------------------------------------------------------------------

def _compute_patch_flow(gray_a: np.ndarray,
                        gray_b: np.ndarray,
                        mask: np.ndarray):
    """
    Find PATCH_SIZE patches in gray_a, match them in gray_b, return drift.

    Returns
    -------
    drift    : (dx, dy) mean background drift of inliers, or None if unreliable
    n_inlier : inlier patch count used for final average
    patches  : list of (center_a, center_b, is_inlier) for display,
               where centers are (x, y) tuples in full-resolution screen coords
    dbg      : dict with per-stage counts for console debug output
    """
    h, w = gray_a.shape
    hp = PATCH_SIZE // 2          # half patch size
    margin = hp + SEARCH_PAD + 2  # minimum distance from any edge

    dbg = {'candidates': 0, 'score_ok': 0, 'disp_ok': 0, 'inliers': 0,
           'best_score': 0.0, 'best_disp': 0.0}

    # ── Step 1: sample candidate patch centres on a grid ─────────────────
    candidates: list[tuple[int, int, float]] = []
    y = margin
    while y <= h - margin:
        x = margin
        while x <= w - margin:
            if mask[y, x] != 0:
                patch = gray_a[y - hp:y + hp, x - hp:x + hp]
                if patch.shape == (PATCH_SIZE, PATCH_SIZE):
                    var = float(np.var(patch))
                    if var >= PATCH_MIN_VAR:
                        candidates.append((x, y, var))
            x += PATCH_GRID_STEP
        y += PATCH_GRID_STEP

    dbg['candidates'] = len(candidates)
    if not candidates:
        return None, 0, [], dbg

    # Keep the N_PATCHES highest-variance candidates (richest texture)
    candidates.sort(key=lambda c: -c[2])
    selected = candidates[:N_PATCHES]

    # ── Step 2: matchTemplate for each selected patch ─────────────────────
    disp_vecs: list[tuple[float, float]] = []
    raw_patches: list[tuple[tuple, tuple, float]] = []  # (ca, cb, score)

    for cx, cy, _ in selected:
        patch = gray_a[cy - hp:cy + hp, cx - hp:cx + hp]

        # Search region in B (clipped to frame bounds)
        sx1 = max(0, cx - hp - SEARCH_PAD)
        sy1 = max(0, cy - hp - SEARCH_PAD)
        sx2 = min(w, cx + hp + SEARCH_PAD)
        sy2 = min(h, cy + hp + SEARCH_PAD)
        search = gray_b[sy1:sy2, sx1:sx2]

        if search.shape[0] < PATCH_SIZE or search.shape[1] < PATCH_SIZE:
            continue

        result = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED)
        _, score, _, max_loc = cv2.minMaxLoc(result)

        dbg['best_score'] = max(dbg['best_score'], score)
        if score < MATCH_MIN_SCORE:
            continue
        dbg['score_ok'] += 1

        # Convert max_loc (top-left of match in search crop) to full-frame centre
        bx = sx1 + max_loc[0] + hp
        by = sy1 + max_loc[1] + hp

        dx = float(bx - cx)
        dy = float(by - cy)
        mag = math.hypot(dx, dy)
        dbg['best_disp'] = max(dbg['best_disp'], mag)

        if DISP_MIN <= mag <= DISP_MAX:
            disp_vecs.append((dx, dy))
            raw_patches.append(((cx, cy), (bx, by), score))
            dbg['disp_ok'] += 1

    n_valid = len(disp_vecs)
    if n_valid < FLOW_MIN_PTS:
        patches = [(*p[:2], False) for p in raw_patches]
        return None, n_valid, patches, dbg

    # ── Step 3: angular consistency filter ───────────────────────────────
    # Use component-wise median as consensus — immune to < 50 % outliers.
    med_dx = float(np.median([v[0] for v in disp_vecs]))
    med_dy = float(np.median([v[1] for v in disp_vecs]))

    if math.hypot(med_dx, med_dy) < DISP_MIN:
        # Median near zero → character stationary, no reliable direction.
        patches = [(*p[:2], False) for p in raw_patches]
        return None, 0, patches, dbg

    consensus_rad = math.atan2(med_dy, med_dx)
    thr_rad = math.radians(ANGULAR_FILTER_DEG)

    inlier_vecs: list[tuple[float, float]] = []
    patches: list = []
    for (dx, dy), (ca, cb, score) in zip(disp_vecs, raw_patches):
        vec_rad = math.atan2(dy, dx)
        diff = abs(math.atan2(math.sin(vec_rad - consensus_rad),
                              math.cos(vec_rad - consensus_rad)))
        inlier = diff <= thr_rad
        if inlier:
            inlier_vecs.append((dx, dy))
        patches.append((ca, cb, inlier))

    n_inlier = len(inlier_vecs)
    dbg['inliers'] = n_inlier

    # Fallback: if angular filter is too aggressive, use all valid vectors.
    if n_inlier < FLOW_MIN_PTS:
        fin_vecs = disp_vecs
        patches = [(*p[:2], True) for p in raw_patches]
        n_inlier = n_valid
    else:
        fin_vecs = inlier_vecs

    fin_dx = sum(v[0] for v in fin_vecs) / n_inlier
    fin_dy = sum(v[1] for v in fin_vecs) / n_inlier
    return (fin_dx, fin_dy), n_inlier, patches, dbg


# ---------------------------------------------------------------------------
# Direction helpers
# ---------------------------------------------------------------------------

def _drift_to_dir(dx: float, dy: float) -> float:
    """
    Background drift (dx, dy) → character movement direction in degrees.
    Convention: 0° = up (toward y=0), 90° = right, 180° = down, 270° = left.

    Derivation:
      character moves in (-dx, -dy).
      "CW from 12 o'clock" angle = atan2(move_x, -move_y) = atan2(-dx, dy).
    """
    return math.degrees(math.atan2(-dx, dy)) % 360.0


def _smooth_angle(prev: float | None, new: float, alpha: float) -> float:
    """Circular EMA so the angle wraps cleanly through 0/360."""
    if prev is None:
        return new
    pr = math.radians(prev)
    nr = math.radians(new)
    x = (1.0 - alpha) * math.cos(pr) + alpha * math.cos(nr)
    y = (1.0 - alpha) * math.sin(pr) + alpha * math.sin(nr)
    return math.degrees(math.atan2(y, x)) % 360.0


# ---------------------------------------------------------------------------
# Compass rose renderer
# ---------------------------------------------------------------------------

def _draw_compass(direction: float | None,
                  n_pts: int,
                  magnitude: float,
                  corridor_half: float,
                  sz: int = COMPASS_SZ) -> np.ndarray:
    canvas = np.zeros((sz, sz, 3), dtype=np.uint8)
    cx, cy = sz // 2, sz // 2
    r = sz // 2 - 20

    cv2.circle(canvas, (cx, cy), r, (55, 55, 55), 1)
    cv2.circle(canvas, (cx, cy), r + 1, (30, 30, 30), 1)

    for deg, lbl in [(0, 'N'), (90, 'E'), (180, 'S'), (270, 'W')]:
        rad = math.radians(deg)
        ix = int(cx + r * math.sin(rad))
        iy = int(cy - r * math.cos(rad))
        ox = int(cx + (r - 10) * math.sin(rad))
        oy = int(cy - (r - 10) * math.cos(rad))
        cv2.line(canvas, (ox, oy), (ix, iy), (70, 70, 70), 2)
        lx = int(cx + (r + 12) * math.sin(rad))
        ly = int(cy - (r + 12) * math.cos(rad))
        cv2.putText(canvas, lbl, (lx - 5, ly + 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (100, 100, 100), 1)

    for deg in range(45, 360, 90):
        rad = math.radians(deg)
        ix = int(cx + r * math.sin(rad))
        iy = int(cy - r * math.cos(rad))
        ox = int(cx + (r - 6) * math.sin(rad))
        oy = int(cy - (r - 6) * math.cos(rad))
        cv2.line(canvas, (ox, oy), (ix, iy), (50, 50, 50), 1)

    if direction is not None and n_pts >= FLOW_MIN_PTS:
        angles = np.linspace(
            math.radians(direction - corridor_half),
            math.radians(direction + corridor_half),
            32,
        )
        pts = [(cx, cy)]
        for a in angles:
            pts.append((cx + int(r * math.sin(a)),
                        cy - int(r * math.cos(a))))
        pts_arr = np.array(pts, dtype=np.int32)
        overlay = canvas.copy()
        cv2.fillConvexPoly(overlay, pts_arr, (0, 80, 0))
        cv2.addWeighted(overlay, 0.45, canvas, 0.55, 0, canvas)

        for off in (-corridor_half, corridor_half):
            ra = math.radians(direction + off)
            bx = cx + int(r * math.sin(ra))
            by = cy - int(r * math.cos(ra))
            cv2.line(canvas, (cx, cy), (bx, by), (0, 140, 0), 1)

        rad = math.radians(direction)
        ax = cx + int(r * math.sin(rad))
        ay = cy - int(r * math.cos(rad))
        cv2.arrowedLine(canvas, (cx, cy), (ax, ay),
                        (0, 240, 0), 2, tipLength=0.25)

        mag_r = int(r * 0.25 * min(magnitude / DISP_MAX, 1.0))
        if mag_r > 1:
            cv2.circle(canvas, (cx, cy), mag_r, (0, 160, 200), -1)

        cv2.putText(canvas, f"{direction:.0f}\u00b0",
                    (cx - 18, cy + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 230, 0), 1)
    else:
        cv2.putText(canvas, "NO SIGNAL", (cx - 36, cy + 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.44, (80, 80, 80), 1)
        if n_pts > 0:
            cv2.putText(canvas, f"({n_pts} pts)", (cx - 22, cy + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (60, 60, 60), 1)

    return canvas


# ---------------------------------------------------------------------------
# Shared state + background worker
# ---------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.direction: float | None = None
        self.magnitude: float = 0.0
        self.n_pts: int = 0       # inlier count
        self.n_total: int = 0     # matched patches before angular filter
        self.patches: list = []   # (center_a, center_b, is_inlier)
        self.frame_bgr: np.ndarray | None = None
        self.frozen: bool = False
        self.save_requested: bool = False
        self.frame_a_save: np.ndarray | None = None
        self.frame_b_save: np.ndarray | None = None
        self.dbg: dict = {}       # per-stage debug counts
        self.stale_count: int = 0  # consecutive updates without a valid direction


def _worker(state: _State,
            monitor: dict,
            mask: np.ndarray,
            interval_ms: float,
            stop_evt: threading.Event) -> None:
    """Background thread: grabs frame pairs and updates shared state."""
    with mss.MSS() as sct:
        while not stop_evt.is_set():
            with state.lock:
                frozen = state.frozen

            if frozen or _capslock_on():
                time.sleep(0.05)
                continue

            # Grab frame A
            shot_a = sct.grab(monitor)
            bgr_a = np.array(shot_a)[:, :, :3]
            gray_a = cv2.cvtColor(bgr_a, cv2.COLOR_BGR2GRAY)

            time.sleep(interval_ms / 1000.0)

            # Grab frame B
            shot_b = sct.grab(monitor)
            bgr_b = np.array(shot_b)[:, :, :3]
            gray_b = cv2.cvtColor(bgr_b, cv2.COLOR_BGR2GRAY)

            drift, n_inlier, patches, dbg = _compute_patch_flow(
                gray_a, gray_b, mask
            )

            with state.lock:
                state.frame_bgr = bgr_b
                state.n_pts = n_inlier
                state.n_total = len(patches)
                state.patches = patches
                state.dbg = dbg

                if drift is not None:
                    raw_dir = _drift_to_dir(*drift)
                    # Confidence-weighted alpha: more inliers → blend faster.
                    confidence = min(1.0, n_inlier / max(N_PATCHES, 1))
                    alpha = (SMOOTH_ALPHA_MIN
                             + (SMOOTH_ALPHA_MAX - SMOOTH_ALPHA_MIN) * confidence)
                    state.direction = _smooth_angle(state.direction, raw_dir, alpha)
                    state.magnitude = math.hypot(*drift)
                    state.stale_count = 0
                else:
                    state.stale_count += 1
                    if state.stale_count >= STALE_RESET_COUNT:
                        # Reset so the next valid reading snaps in instantly.
                        state.direction = None
                        state.stale_count = 0

                if state.save_requested:
                    state.frame_a_save = bgr_a.copy()
                    state.frame_b_save = bgr_b.copy()
                    state.save_requested = False


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _scaled_excl_rects(sw: int, sh: int, pw: int, ph: int):
    sx, sy = pw / sw, ph / sh
    return [
        (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))
        for x1, y1, x2, y2 in _EXCL_FHD
    ]


def _render_frame(state: _State,
                  screen_w: int, screen_h: int,
                  corridor_half: float) -> np.ndarray | None:
    with state.lock:
        frame_bgr = state.frame_bgr
        direction = state.direction
        magnitude = state.magnitude
        n_pts     = state.n_pts
        n_total   = state.n_total
        patches   = list(state.patches)
        frozen    = state.frozen

    if frame_bgr is None:
        return None

    # ── Game preview ──────────────────────────────────────────────────────
    preview_h = int(PREVIEW_W * screen_h / screen_w)
    preview = cv2.resize(frame_bgr, (PREVIEW_W, preview_h),
                         interpolation=cv2.INTER_LINEAR)
    sx = PREVIEW_W / screen_w
    sy = preview_h / screen_h

    # Exclusion overlay (dark tint)
    ov = preview.copy()
    for x1, y1, x2, y2 in _scaled_excl_rects(screen_w, screen_h,
                                               PREVIEW_W, preview_h):
        cv2.rectangle(ov, (x1, y1), (x2, y2), (70, 0, 90), -1)
    cv2.addWeighted(ov, 0.22, preview, 0.78, 0, preview)

    # Centre exclusion disk outline
    cr = int(CENTER_EXCL_R * min(sx, sy))
    pcx, pcy = PREVIEW_W // 2, preview_h // 2
    cv2.circle(preview, (pcx, pcy), cr, (50, 50, 0), 1)

    # Patch boxes + displacement arrows
    # green = inlier (used for direction), red = rejected by angular filter
    phalf_px = max(2, int(PATCH_SIZE * sx / 2))
    for ca, cb, inlier in patches:
        pax, pay = int(ca[0] * sx), int(ca[1] * sy)
        pbx, pby = int(cb[0] * sx), int(cb[1] * sy)
        col = (0, 200, 60) if inlier else (0, 60, 220)
        cv2.rectangle(preview,
                      (pax - phalf_px, pay - phalf_px),
                      (pax + phalf_px, pay + phalf_px),
                      col, 1)
        if abs(pbx - pax) > 0 or abs(pby - pay) > 0:
            cv2.arrowedLine(preview, (pax, pay), (pbx, pby),
                            col, 1, tipLength=0.5)

    # Big direction arrow + corridor on preview
    if direction is not None and n_pts >= FLOW_MIN_PTS:
        arrow_r = min(PREVIEW_W, preview_h) // 4
        rad = math.radians(direction)
        ax = pcx + int(arrow_r * math.sin(rad))
        ay = pcy - int(arrow_r * math.cos(rad))
        for off in (-corridor_half, corridor_half):
            ra = math.radians(direction + off)
            bx = pcx + int(arrow_r * math.sin(ra))
            by = pcy - int(arrow_r * math.cos(ra))
            cv2.line(preview, (pcx, pcy), (bx, by), (0, 160, 0), 1)
        cv2.arrowedLine(preview, (pcx, pcy), (ax, ay),
                        (0, 240, 0), 2, tipLength=0.22)

    # ── Compass rose ──────────────────────────────────────────────────────
    compass = _draw_compass(direction, n_pts, magnitude, corridor_half)

    pad_t = max(0, (preview_h - COMPASS_SZ) // 2)
    pad_b = max(0, preview_h - COMPASS_SZ - pad_t)
    if pad_t or pad_b:
        compass = np.vstack([
            np.zeros((pad_t, COMPASS_SZ, 3), dtype=np.uint8),
            compass,
            np.zeros((pad_b, COMPASS_SZ, 3), dtype=np.uint8),
        ])
    compass = compass[:preview_h]

    gap = np.zeros((preview_h, 10, 3), dtype=np.uint8)
    row = np.hstack([preview, gap, compass])

    # ── Status bar ────────────────────────────────────────────────────────
    bar = np.zeros((26, row.shape[1], 3), dtype=np.uint8)
    paused = _capslock_on()

    if paused:
        status = "  CAPS PAUSED"
        color  = (0, 100, 220)
    elif direction is not None and n_pts >= FLOW_MIN_PTS:
        lo = (direction - corridor_half) % 360.0
        hi = (direction + corridor_half) % 360.0
        inlier_pct = int(100 * n_pts / n_total) if n_total > 0 else 0
        status = (f"  dir={direction:.1f}\u00b0  "
                  f"corridor [{lo:.0f}\u00b0\u2013{hi:.0f}\u00b0]  "
                  f"mag={magnitude:.1f}px  "
                  f"patches={n_pts}/{n_total} ({inlier_pct}%)"
                  + ("  [FROZEN]" if frozen else ""))
        color  = (0, 220, 0)
    else:
        status = (f"  NO SIGNAL  matched={n_total}"
                  + ("  [FROZEN]" if frozen else ""))
        color  = (60, 60, 180)

    cv2.putText(bar, status, (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)

    hint = "q=quit  f=freeze  s=save  d=debug  +/-=corridor"
    (tw, _), _ = cv2.getTextSize(hint, cv2.FONT_HERSHEY_SIMPLEX, 0.35, 1)
    cv2.putText(bar, hint, (row.shape[1] - tw - 8, 18),
                cv2.FONT_HERSHEY_SIMPLEX, 0.35, (70, 70, 70), 1)

    return np.vstack([row, bar])


# ---------------------------------------------------------------------------
# Main live loop
# ---------------------------------------------------------------------------

def run_live(monitor_idx: int, interval_ms: float) -> None:
    with mss.MSS() as sct:
        monitors = sct.monitors
        if monitor_idx < 1 or monitor_idx >= len(monitors):
            print(f"Monitor {monitor_idx} not found. "
                  f"Available: 1–{len(monitors)-1}")
            return
        mon = monitors[monitor_idx]

    sw, sh = mon['width'], mon['height']
    monitor = {'left': mon['left'], 'top': mon['top'],
               'width': sw, 'height': sh}

    print(f"Monitor {monitor_idx}: {sw}×{sh}  @ ({mon['left']},{mon['top']})")
    print(f"Frame interval : {interval_ms:.0f} ms  "
          f"(~{1000/interval_ms:.1f} updates/sec)")
    print(f"Patch size     : {PATCH_SIZE}px,  "
          f"search window : {PATCH_SIZE + 2*SEARCH_PAD}px")
    print(f"Max patches    : {N_PATCHES},  min inliers: {FLOW_MIN_PTS}")
    print(f"Centre excl. r : "
          f"{int(CENTER_EXCL_R * min(sw/1920, sh/1080))}px at this resolution")
    print("\nPress q to quit.\n")

    mask     = _build_mask(sh, sw)
    state    = _State()
    stop_evt = threading.Event()
    corridor_half = float(CORRIDOR_HALF_DEG_DEFAULT)

    worker = threading.Thread(
        target=_worker,
        args=(state, monitor, mask, interval_ms, stop_evt),
        daemon=True,
    )
    worker.start()

    cv2.namedWindow("Movement Direction", cv2.WINDOW_AUTOSIZE)
    debug_mode = True   # start with debug on so first run is immediately informative

    while True:
        panel = _render_frame(state, sw, sh, corridor_half)
        if panel is not None:
            cv2.imshow("Movement Direction", panel)

        if debug_mode:
            with state.lock:
                dbg = dict(state.dbg)
                d   = state.direction
                n   = state.n_pts
                nt  = state.n_total
            if dbg:
                print(
                    f"\r  cand={dbg.get('candidates',0):3d} "
                    f"| score_ok={dbg.get('score_ok',0):2d} (best={dbg.get('best_score',0):.2f}) "
                    f"| disp_ok={dbg.get('disp_ok',0):2d} (best={dbg.get('best_disp',0):.1f}px) "
                    f"| inliers={dbg.get('inliers',0):2d}/{nt:2d} "
                    f"| dir={f'{d:.0f}°' if d is not None and n >= FLOW_MIN_PTS else '---':>6s}   ",
                    end="", flush=True,
                )

        key = cv2.waitKey(LIVE_MS) & 0xFF
        if key == ord('q'):
            break

        if key == ord('d'):
            debug_mode = not debug_mode
            if not debug_mode:
                print()   # newline after the rolling debug line

        if key == ord('f'):
            with state.lock:
                state.frozen = not state.frozen
            print(f"\r  [{'FROZEN' if state.frozen else 'RUNNING'}]"
                  + " " * 20, end="", flush=True)

        if key == ord('s'):
            with state.lock:
                state.save_requested = True
            time.sleep(0.4)
            with state.lock:
                fa, fb = state.frame_a_save, state.frame_b_save
            if fa is not None and fb is not None:
                os.makedirs("logs/movement_dir", exist_ok=True)
                ts = int(time.time() * 1000)
                cv2.imwrite(f"logs/movement_dir/{ts}_a.png", fa)
                cv2.imwrite(f"logs/movement_dir/{ts}_b.png", fb)
                print(f"\n  Saved logs/movement_dir/{ts}_a/b.png")

        if key in (ord('+'), ord('=')):
            corridor_half = min(corridor_half + 5.0, 90.0)
            print(f"\r  corridor ±{corridor_half:.0f}°" + " "*10,
                  end="", flush=True)

        if key == ord('-'):
            corridor_half = max(corridor_half - 5.0, 10.0)
            print(f"\r  corridor ±{corridor_half:.0f}°" + " "*10,
                  end="", flush=True)

    stop_evt.set()
    worker.join(timeout=2.0)
    cv2.destroyAllWindows()
    print("\nDone.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Real-time movement direction via template-patch matching"
    )
    ap.add_argument("--monitor", type=int, default=1,
                    help="Monitor index (1-based, default 1 = primary)")
    ap.add_argument("--interval", type=float, default=float(FRAME_INTERVAL_MS),
                    metavar="MS",
                    help=f"Frame pair interval ms (default {FRAME_INTERVAL_MS})")
    args = ap.parse_args()
    run_live(args.monitor, args.interval)


if __name__ == "__main__":
    main()
