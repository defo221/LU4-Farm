"""Standalone calibration tool: visualise the future SA_MA_CLICK_AREA placement.

Usage
-----
- Toggle CapsLock OFF  →  capture one annotated screenshot and save it.
- Toggle CapsLock ON   →  pause (nothing happens while ON).
- Ctrl+C               →  quit.

No mouse clicks, key presses, or any other input events are generated.

The two calibration constants at the top of this file are what you will tune:

  SA_MA_DIRECTION_X_WEIGHT
      Controls the angle at which the click region continues beyond ma_anchor.
      The raw direction is the vector from the screen centre to the anchor.
      The horizontal component of that vector is multiplied by this weight
      before normalisation.  Increase to bias the direction more sideways;
      decrease to bias it more vertical.

  SA_MA_REGION_OFFSET_PX
      Guaranteed empty gap between the centre of ma_anchor and the NEAREST EDGE
      of the click region.  Independent of the direction constant.

Once you find the ideal values, transfer them to the Assist bot.
"""

import ctypes
import math
import pathlib
import time

import cv2
import mss
import numpy as np

# ---------------------------------------------------------------------------
# Calibration constants — tune these, then copy to the bot
# ---------------------------------------------------------------------------

SA_MA_DIRECTION_X_WEIGHT: float = 1.0
# Weight applied to the horizontal component of the screen-centre → anchor
# vector before normalisation.  Pure angle/direction calibration — does not
# change how far the region is placed.
#
# Examples for an anchor above-left of screen centre:
#   < 1.0  →  region too high / too vertical (under-weighted horizontal)
#   = 1.0  →  raw direction preserved
#   > 1.0  →  region more horizontal / more to the left (over-weighted)
#
# For a purely left or right anchor the direction is always horizontal
# regardless of this value.

SA_MA_REGION_OFFSET_PX: int = 50
# Distance in pixels from the centre of ma_anchor to the NEAREST EDGE of the
# click region.  Must be an integer ≥ 0.
#
#   screen centre
#       ↓  (guide line)
#   ma_anchor centre
#       ↓  SA_MA_REGION_OFFSET_PX px of empty space
#   nearest edge of click region
#       ↓  SA_MA_CLICK_AREA px of click square
#   far edge of click region

# ---------------------------------------------------------------------------
# Detection config — must match the running bot's config.py exactly
# ---------------------------------------------------------------------------

MA_ANCHOR_TEMPLATE = r"C:\PXM_LU4\assets\fullhd\ma_anchor.png"
SA_MA_CONFIDENCE   = 0.80   # matchTemplate threshold
SA_MA_CLICK_AREA   = 50     # side (px) of the click square; 0 = single-point click

# UI exclusion zones applied when the captured frame is 1920×1080.
# Keep in sync with SA_EXCL_ROIS_FHD in config.py.
_EXCL_ROIS_FHD = [
    (   0,    0,  384,   56),   # top-left corner (character / party bars)
    ( 844,    0, 1249,  110),   # top-center band (target window)
    (1642,    0, 1919,  275),   # top-right strip (minimap + buff icons)
    (   0,  200,  260,  686),   # left strip — narrow segment
    (   0,  686,  381, 1048),   # left strip — wide segment
    ( 752,  837, 1273, 1048),   # bottom-center block (inventory / skills)
    (1868,  922, 1919,  990),   # bottom-right small stub
    (1504,  992, 1919, 1048),   # bottom-right large block
    (   0, 1049, 1919, 1079),   # bottom strip (system bar)
]

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

SAVE_DIR = r"C:\PXM_LU4\logs\ma_calibration"

# ---------------------------------------------------------------------------
# Windows CapsLock helper
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32


def capslock_on() -> bool:
    return bool(_user32.GetKeyState(0x14) & 0x0001)


# ---------------------------------------------------------------------------
# Detection helpers  (mirrors _zero_excl_in_res / _detect_ma_anchor_pos)
# ---------------------------------------------------------------------------

def _zero_excl_in_res(res: np.ndarray, tw: int, th: int,
                      rois: list) -> None:
    """Zero result-map cells whose template centre falls inside any exclusion ROI."""
    rh, rw = res.shape[:2]
    for ex1, ey1, ex2, ey2 in rois:
        rx1 = max(0, ex1 - tw // 2)
        rx2 = min(rw, ex2 - tw // 2 + 1)
        ry1 = max(0, ey1 - th // 2)
        ry2 = min(rh, ey2 - th // 2 + 1)
        if rx2 > rx1 and ry2 > ry1:
            res[ry1:ry2, rx1:rx2] = -1.0


def detect_anchor(frame: np.ndarray,
                  tmpl: np.ndarray) -> tuple | None:
    """Return (cx, cy, score) or None if anchor not found."""
    fh, fw = frame.shape[:2]
    th, tw = tmpl.shape[:2]
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    if fw == 1920 and fh == 1080:
        _zero_excl_in_res(res, tw, th, _EXCL_ROIS_FHD)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < SA_MA_CONFIDENCE:
        return None
    cx = loc[0] + tw // 2
    cy = loc[1] + th // 2
    return cx, cy, score


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _weighted_unit(dx: float, dy: float, x_weight: float) -> tuple[float, float]:
    """Weighted unit vector in the screen-centre → anchor direction."""
    wdx = dx * x_weight
    wdy = dy
    length = math.hypot(wdx, wdy)
    if length < 1e-6:
        return 0.0, 1.0   # anchor at screen centre: default straight down
    return wdx / length, wdy / length


def compute_region(ax: int, ay: int,
                   sc_x: int, sc_y: int) -> dict:
    """Return geometry needed for drawing and labelling.

    Keys
    ----
    nx, ny          weighted unit direction (screen centre → anchor)
    region_cx/cy    centre of the SA_MA_CLICK_AREA square
    half_side       SA_MA_CLICK_AREA // 2
    """
    dx = ax - sc_x
    dy = ay - sc_y
    nx, ny = _weighted_unit(dx, dy, SA_MA_DIRECTION_X_WEIGHT)

    half_side = SA_MA_CLICK_AREA // 2
    # Centre of region = anchor + (offset + half_side) × unit vector
    dist_to_centre = SA_MA_REGION_OFFSET_PX + half_side
    region_cx = ax + nx * dist_to_centre
    region_cy = ay + ny * dist_to_centre

    return {
        "nx": nx, "ny": ny,
        "region_cx": region_cx, "region_cy": region_cy,
        "half_side": half_side,
    }


# ---------------------------------------------------------------------------
# Visualisation
# ---------------------------------------------------------------------------

_FONT      = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SM   = 0.40
_FONT_MD   = 0.45
_CLR_WHITE = (255, 255, 255)
_CLR_YELL  = (0,   220, 255)   # anchor  (BGR: yellow-ish)
_CLR_GREEN = (0,   200,  80)   # region
_CLR_CYAN  = (255, 200,  80)   # guide line sc→anchor
_CLR_LBLUE = ( 80, 200, 255)   # guide line anchor→region


def _label(img, text, x, y, color, scale=_FONT_SM):
    cv2.putText(img, text, (x, y), _FONT, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), _FONT, scale, color,    1, cv2.LINE_AA)


def draw_overlay(frame: np.ndarray,
                 sc_x: int, sc_y: int,
                 ax: int, ay: int,
                 score: float) -> np.ndarray:
    vis = frame.copy()
    geo = compute_region(ax, ay, sc_x, sc_y)
    rcx = int(round(geo["region_cx"]))
    rcy = int(round(geo["region_cy"]))
    hs  = geo["half_side"]

    # ── semi-transparent filled click region ────────────────────────────────
    if hs > 0:
        overlay = vis.copy()
        cv2.rectangle(overlay, (rcx - hs, rcy - hs),
                                (rcx + hs, rcy + hs),
                      _CLR_GREEN, cv2.FILLED)
        cv2.addWeighted(overlay, 0.35, vis, 0.65, 0, vis)
        cv2.rectangle(vis, (rcx - hs, rcy - hs),
                           (rcx + hs, rcy + hs),
                      (0, 240, 100), 1)
    else:
        # SA_MA_CLICK_AREA = 0  →  single-point marker
        cv2.drawMarker(vis, (rcx, rcy), (0, 240, 100),
                       cv2.MARKER_CROSS, 14, 1, cv2.LINE_AA)

    # ── guide lines ────────────────────────────────────────────────────────
    # screen centre → anchor (warm yellow)
    cv2.line(vis, (sc_x, sc_y), (ax, ay), _CLR_CYAN, 1, cv2.LINE_AA)
    # anchor → region centre (light blue)
    cv2.line(vis, (ax, ay), (rcx, rcy), _CLR_LBLUE, 1, cv2.LINE_AA)

    # nearest-edge tick mark (shows where the offset gap ends)
    if SA_MA_REGION_OFFSET_PX > 0:
        edge_x = int(round(ax + geo["nx"] * SA_MA_REGION_OFFSET_PX))
        edge_y = int(round(ay + geo["ny"] * SA_MA_REGION_OFFSET_PX))
        # short perpendicular tick
        perp_x = int(round(-geo["ny"] * 6))
        perp_y = int(round( geo["nx"] * 6))
        cv2.line(vis,
                 (edge_x - perp_x, edge_y - perp_y),
                 (edge_x + perp_x, edge_y + perp_y),
                 _CLR_LBLUE, 1, cv2.LINE_AA)

    # ── screen centre marker ────────────────────────────────────────────────
    cv2.circle(vis, (sc_x, sc_y), 9, _CLR_WHITE, 1, cv2.LINE_AA)
    cv2.drawMarker(vis, (sc_x, sc_y), _CLR_WHITE,
                   cv2.MARKER_CROSS, 18, 1, cv2.LINE_AA)

    # ── anchor marker (filled yellow circle) ───────────────────────────────
    cv2.circle(vis, (ax, ay), 7, _CLR_YELL, cv2.FILLED, cv2.LINE_AA)
    cv2.circle(vis, (ax, ay), 7, (0, 255, 255), 1,       cv2.LINE_AA)

    # ── region centre dot ───────────────────────────────────────────────────
    cv2.circle(vis, (rcx, rcy), 3, (200, 255, 200), cv2.FILLED, cv2.LINE_AA)

    # ── labels ──────────────────────────────────────────────────────────────
    _label(vis, "screen centre",
           sc_x + 12, sc_y - 8, _CLR_WHITE)
    _label(vis, f"ma_anchor ({ax},{ay})  score={score:.3f}",
           ax + 10, ay - 10, _CLR_YELL)
    _label(vis, f"region centre ({rcx},{rcy})",
           rcx + 10, rcy + 16, (120, 255, 150))

    # parameter summary in the top-left corner
    params = (f"X_WEIGHT={SA_MA_DIRECTION_X_WEIGHT:.2f}   "
              f"OFFSET={SA_MA_REGION_OFFSET_PX}px   "
              f"CLICK_AREA={SA_MA_CLICK_AREA}px   "
              f"confidence={SA_MA_CONFIDENCE}")
    _label(vis, params, 12, 26, (255, 255, 100), scale=_FONT_MD)

    return vis


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    tmpl = cv2.imread(MA_ANCHOR_TEMPLATE)
    if tmpl is None:
        raise FileNotFoundError(
            f"ma_anchor template not found: {MA_ANCHOR_TEMPLATE}\n"
            f"Edit MA_ANCHOR_TEMPLATE at the top of this script.")

    out_dir = pathlib.Path(SAVE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 62)
    print("  MA anchor click-region calibration tool")
    print("=" * 62)
    print(f"  Template     : {MA_ANCHOR_TEMPLATE}")
    print(f"  Confidence   : {SA_MA_CONFIDENCE}")
    print(f"  CLICK_AREA   : {SA_MA_CLICK_AREA} px")
    print()
    print(f"  X_WEIGHT     : {SA_MA_DIRECTION_X_WEIGHT}")
    print(f"  OFFSET_PX    : {SA_MA_REGION_OFFSET_PX} px")
    print()
    print(f"  Output dir   : {out_dir}")
    print()
    print("  CapsLock OFF  →  capture & save one annotated frame")
    print("  CapsLock ON   →  pause")
    print("  Ctrl+C        →  quit")
    print("=" * 62)
    print()

    counter      = 0
    cl_was_on    = capslock_on()   # current state at startup

    if cl_was_on:
        print("[PAUSED]  Toggle CapsLock OFF to capture.")
    else:
        # Start in an implicitly paused state so the first OFF→ON→OFF
        # cycle is needed to capture.  Treat current OFF as "was ON" so
        # the first real capture requires a genuine toggle.
        print("[READY]   CapsLock is already OFF.")
        print("          Toggle CapsLock ON, then OFF to capture your first frame.")
        cl_was_on = True   # pretend we were paused

    with mss.mss() as sct:
        mon = sct.monitors[1]
        sw, sh = mon["width"], mon["height"]
        sc_x, sc_y = sw // 2, sh // 2

        while True:
            cl_is_on = capslock_on()

            # ── CapsLock just turned OFF  →  exit pause  →  capture ─────────
            if cl_was_on and not cl_is_on:
                print("\n[RESUMED] Capturing...", flush=True)
                t0 = time.perf_counter()

                shot  = sct.grab(mon)
                frame = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                    (shot.height, shot.width, 4))
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                t_cap = (time.perf_counter() - t0) * 1000

                t0     = time.perf_counter()
                result = detect_anchor(frame, tmpl)
                t_det  = (time.perf_counter() - t0) * 1000

                if result is None:
                    print(f"  ma_anchor NOT detected  "
                          f"(best score < {SA_MA_CONFIDENCE})  "
                          f"[cap={t_cap:.0f}ms det={t_det:.0f}ms]")
                else:
                    ax, ay, score = result
                    vis    = draw_overlay(frame, sc_x, sc_y, ax, ay, score)
                    geo    = compute_region(ax, ay, sc_x, sc_y)
                    rcx    = int(round(geo["region_cx"]))
                    rcy    = int(round(geo["region_cy"]))

                    counter += 1
                    ts    = time.strftime("%H-%M-%S")
                    fname = out_dir / f"cal_{counter:04d}_{ts}_ax{ax}_ay{ay}.png"
                    cv2.imwrite(str(fname), vis)

                    print(f"  ma_anchor    = ({ax}, {ay})  score={score:.3f}  "
                          f"[cap={t_cap:.0f}ms det={t_det:.0f}ms]")
                    print(f"  region_centre= ({rcx}, {rcy})  "
                          f"half_side={geo['half_side']}px")
                    print(f"  direction    = nx={geo['nx']:+.3f}  ny={geo['ny']:+.3f}  "
                          f"(screen→anchor then weighted)")
                    print(f"  saved → {fname}")

                print("[PAUSED]  Toggle CapsLock OFF again to capture next frame.",
                      flush=True)

            # ── CapsLock just turned ON  →  enter pause ─────────────────────
            elif not cl_was_on and cl_is_on:
                pass   # message was already printed at capture time above

            cl_was_on = cl_is_on
            time.sleep(0.04)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
