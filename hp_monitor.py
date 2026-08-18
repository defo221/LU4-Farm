"""
hp_monitor.py  -  Mob HP bar reader (screen read-only, no input injection).

Strategy
--------
1. One mss grab of SEARCH_REGION (the target-frame area) per tick.
2. cv2.matchTemplate finds the bag icon anchor inside that grab.
3. HP bar sub-image is sliced from the same grab (no second capture).
4. Red fill detected via HSV; rightmost red column / bar width = HP%.
5. Print + log at POLL_INTERVAL.

Why mss instead of pyautogui.screenshot?
  pyautogui uses GDI BitBlt: ~32 ms fixed overhead regardless of region size.
  mss uses DirectX/WGC:       ~6 ms for 600x250, scales with pixels captured.
  One mss grab = ~6 ms total; old two-pyautogui-grab approach = ~65 ms total.

Calibration
-----------
Run with  --debug  to save debug_hp_region.png each tick (HP bar slice with
red pixels highlighted) so you can confirm offsets / HSV thresholds.

SEARCH_REGION must contain the full target frame.  If no target is selected
the anchor (bag icon) will not be found and the tick is skipped cleanly.
"""

import os
import sys
import time
import argparse

import cv2
import numpy as np
import mss as _mss_mod

import logger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DIR         = os.path.dirname(os.path.abspath(__file__))
ANCHOR_IMAGE = os.path.join(_DIR, "assets", "bag_mob_anchor.png")

# ---------------------------------------------------------------------------
# Config  -  tune to match your game resolution / UI layout
# ---------------------------------------------------------------------------

# Screen region that always contains the target frame (left, top, width, height).
# Keep it tight: smaller = faster.  Must be large enough to show the full frame.
# Set to None to search the full screen (slow, ~65 ms/tick via pyautogui fallback).
SEARCH_REGION = (0, 0, 2560, 200)  # top 200 px — same limit used by the bot

ANCHOR_CONFIDENCE = 0.80           # template-match threshold (0-1)

# Pixel offsets from anchor CENTER to HP bar TOP-LEFT.
# QHD profile (from config.py):
HP_BAR_OFFSET_X =  21   # HP bar is 21 px to the RIGHT of bag center
HP_BAR_OFFSET_Y = -43   # HP bar is 43 px ABOVE  bag center

HP_BAR_W = 387           # full width of the HP bar (pixels)
HP_BAR_H =  18           # height of the HP bar (pixels)

POLL_INTERVAL = 1.0      # seconds between readings

# HSV colour ranges for the red/crimson HP fill.
# Red wraps around hue=0, so two ranges are needed.
_RED_LO1 = np.array([  0, 160,  80], dtype=np.uint8)   # S≥160 excludes brownish backgrounds
_RED_HI1 = np.array([  8, 255, 255], dtype=np.uint8)
_RED_LO2 = np.array([172, 160,  80], dtype=np.uint8)
_RED_HI2 = np.array([180, 255, 255], dtype=np.uint8)

# A column counts as "filled" only if >= this fraction of its height is red.
# Guards against stray red pixels from portrait frames / UI decorations.
RED_COL_THRESHOLD = 0.15

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_anchor_tmpl: np.ndarray | None = None   # loaded once at startup


def _load_anchor() -> np.ndarray | None:
    if not os.path.exists(ANCHOR_IMAGE):
        logger.error(f"[HP] Anchor image missing: {ANCHOR_IMAGE}")
        return None
    img = cv2.imread(ANCHOR_IMAGE)
    if img is None:
        logger.error(f"[HP] cv2 could not load anchor: {ANCHOR_IMAGE}")
    return img


# ---------------------------------------------------------------------------
# Core: single-grab tick
# ---------------------------------------------------------------------------

def _mss_region() -> dict:
    if SEARCH_REGION:
        l, t, w, h = SEARCH_REGION
        return {"left": l, "top": t, "width": w, "height": h}
    # fallback: full primary monitor (handled by caller)
    return None


def tick(sct: "_mss_mod.MSS", debug: bool = False,
         sample: bool = False) -> float | None:
    """
    Capture SEARCH_REGION once, find the anchor, slice the HP bar, return HP%.
    Returns None if anchor not found or on error.
    """
    region = _mss_region()
    try:
        raw = sct.grab(region or sct.monitors[1])
    except Exception as e:
        logger.warn(f"[HP] mss grab failed: {e}")
        return None

    # mss returns BGRA; convert to BGR for OpenCV
    frame = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)

    # --- find anchor (bag icon) inside the grabbed frame ---
    res    = cv2.matchTemplate(frame, _anchor_tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)

    if score < ANCHOR_CONFIDENCE:
        return None   # no target selected / frame not visible

    ah, aw = _anchor_tmpl.shape[:2]
    anchor_cx = loc[0] + aw // 2
    anchor_cy = loc[1] + ah // 2

    # --- slice HP bar from the same frame ---
    bar_x = anchor_cx + HP_BAR_OFFSET_X
    bar_y = anchor_cy + HP_BAR_OFFSET_Y
    fh, fw = frame.shape[:2]
    bar_x = max(0, min(bar_x, fw - HP_BAR_W))
    bar_y = max(0, min(bar_y, fh - HP_BAR_H))

    bar = frame[bar_y : bar_y + HP_BAR_H, bar_x : bar_x + HP_BAR_W]
    if bar.size == 0:
        logger.warn("[HP] HP bar slice is empty -- check offsets")
        return None

    # --- HSV red detection ---
    hsv  = cv2.cvtColor(bar, cv2.COLOR_BGR2HSV)
    mask = cv2.bitwise_or(
        cv2.inRange(hsv, _RED_LO1, _RED_HI1),
        cv2.inRange(hsv, _RED_LO2, _RED_HI2),
    )

    col_ratio  = mask.sum(axis=0).astype(float) / (HP_BAR_H * 255)
    filled     = np.where(col_ratio >= RED_COL_THRESHOLD)[0]
    hp_percent = (int(filled[-1]) + 1) / HP_BAR_W * 100.0 if len(filled) else 0.0

    if debug:
        dbg      = bar.copy()
        dbg[mask > 0] = (255, 255, 255)
        cv2.rectangle(dbg, (0, 0), (HP_BAR_W - 1, HP_BAR_H - 1), (0, 255, 0), 1)
        cv2.imwrite(os.path.join(_DIR, "debug_hp_region.png"), dbg)

    if sample:
        # Print min/max/mean HSV across the whole bar slice
        h_ch = hsv[:, :, 0]
        s_ch = hsv[:, :, 1]
        v_ch = hsv[:, :, 2]
        print(f"  HSV sample  H: {h_ch.min():3d}–{h_ch.max():3d} mean={h_ch.mean():.0f}"
              f"  S: {s_ch.min():3d}–{s_ch.max():3d} mean={s_ch.mean():.0f}"
              f"  V: {v_ch.min():3d}–{v_ch.max():3d} mean={v_ch.mean():.0f}"
              f"  red_px={int(mask.sum()/255)}/{HP_BAR_W*HP_BAR_H}")
        # Save the raw bar slice for visual inspection
        cv2.imwrite(os.path.join(_DIR, "debug_hp_region.png"), bar)

    return min(hp_percent, 100.0)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _bar_visual(pct: float, width: int = 20) -> str:
    n = round(pct / 100 * width)
    return "\u2588" * n + "\u2591" * (width - n)


def main():
    parser = argparse.ArgumentParser(description="Mob HP monitor")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug_hp_region.png each tick for calibration")
    parser.add_argument("--sample", action="store_true",
                        help="Print HSV min/max/mean of the bar slice each tick")
    args = parser.parse_args()

    logger.run_start()
    logger.info("[HP] HP Monitor started -- Ctrl+C to stop")
    logger.info(f"[HP] Anchor      : {ANCHOR_IMAGE}  (threshold {ANCHOR_CONFIDENCE})")
    logger.info(f"[HP] Search region: {SEARCH_REGION}")
    logger.info(f"[HP] Bar offset  : ({HP_BAR_OFFSET_X:+d}, {HP_BAR_OFFSET_Y:+d})"
                f"  size {HP_BAR_W}x{HP_BAR_H} px")
    logger.info(f"[HP] Poll interval: {POLL_INTERVAL} s")
    if args.debug:
        logger.info("[HP] Debug ON -- writing debug_hp_region.png each tick")
    if args.sample:
        logger.info("[HP] Sample ON -- printing HSV stats each tick")

    global _anchor_tmpl
    _anchor_tmpl = _load_anchor()
    if _anchor_tmpl is None:
        sys.exit(1)

    with _mss_mod.MSS() as sct:
        while True:
            try:
                hp = tick(sct, debug=args.debug, sample=args.sample)
                if hp is None:
                    logger.warn("[HP] Target frame not found -- no mob selected?")
                else:
                    logger.info(f"[HP] {hp:5.1f}%  [{_bar_visual(hp)}]")
            except KeyboardInterrupt:
                logger.info("[HP] Stopped by user.")
                break
            except Exception as e:
                logger.warn(f"[HP] Unexpected error: {e}")

            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
