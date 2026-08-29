"""
minimap_orient.py
-----------------
Minimap arrow orientation detector + single-shot camera aligner.

Angle convention (clock-based, CW positive):
  0deg = 12 o'clock (camera facing North)
 90deg =  3 o'clock (camera facing East)
180deg =  6 o'clock (camera facing South)
270deg =  9 o'clock (camera facing West)

Modes (choose with --mode):
  live   - continuous debug window showing detected angle (default)
  align  - interactive console: enter target angle, script does one drag and reports result

Usage:
  python minimap_orient.py              # live display
  python minimap_orient.py --mode align          # interactive align
  python minimap_orient.py --mode align --port COM5
  python minimap_orient.py --mode align --settle 0.8

Live window controls:
  q - quit
  s - save current grab to logs/
"""

import argparse
import os
import sys
import time

import cv2
import mss
import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimap arrow region per screen width.
# Add new entries here when supporting additional resolutions.
ARROW_REGIONS: dict[int, dict] = {
    1920: {"left": 1766, "top": 142, "width": 15, "height": 15},  # FHD 1920×1080
    1366: {"left": 1293, "top": 136, "width": 15, "height": 15},  # Asus 1366×768
}
# Default (FHD) kept for backward-compat with external callers that import it directly.
ARROW_REGION = ARROW_REGIONS[1920]


def _get_arrow_region(sct) -> dict:
    """Return the ARROW_REGIONS entry whose key is closest to the primary monitor width."""
    w = sct.monitors[1]["width"]
    best = min(ARROW_REGIONS, key=lambda k: abs(k - w))
    return ARROW_REGIONS[best]

# Template matching
UPSCALE   = 4   # 15 -> 60 px; improves rotation and matching precision
STEP_DEG  = 5   # angular resolution; gives +-2.5 deg precision

# Live display
DISPLAY_PX = 120   # pixel size of each pane (nearest-neighbour scaled)
LIVE_MS    = 50    # cv2.waitKey interval ms

# Camera drag calibration
#   Measured: 360 deg full rotation = 1150 px of horizontal mouse drag.
#   Positive dx -> drag right -> camera rotates CW (angle increases).
PIXELS_PER_360 = 1150
DRAG_START_X   = 400    # safe screen coordinate for drag anchor (away from game UI)
DRAG_START_Y   = 700
DRAG_SETTLE_S  = 0.6    # seconds to wait after drag before re-reading the arrow

# Iterative alignment
ALIGN_TOL_DEG  = 5      # stop when |error| <= this value
ALIGN_MAX_ITER = 8      # safety cap to prevent infinite loops

# ---------------------------------------------------------------------------
# Arduino HID import  (optional – only needed for align mode)
# ---------------------------------------------------------------------------

_HID_AVAILABLE = False
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "PXM_RB"))
    from common.arduino_hid import ArduinoHID, find_arduino_port  # noqa: F401
    _HID_AVAILABLE = True
except Exception:
    pass

# ---------------------------------------------------------------------------
# Paths  (reference images live in minimap_refs/ next to this script)
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))

_REF_FILES = {
      0: os.path.join(_HERE, "minimap_refs", "arrow_12.png"),
     90: os.path.join(_HERE, "minimap_refs", "arrow_3.png"),
    180: os.path.join(_HERE, "minimap_refs", "arrow_6.png"),
    270: os.path.join(_HERE, "minimap_refs", "arrow_9.png"),
}

# ---------------------------------------------------------------------------
# Template bank
# ---------------------------------------------------------------------------

def _load_ref_gray(deg: int) -> np.ndarray:
    path = _REF_FILES[deg]
    img  = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(f"Reference image missing: {path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _rotate(img: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rotate img CW by angle_deg around its centre (same canvas size)."""
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), -angle_deg, 1.0)
    return cv2.warpAffine(img, M, (w, h),
                          flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def build_template_bank(step_deg: int = STEP_DEG,
                        upscale: int  = UPSCALE) -> list:
    """
    Return list of (angle_deg, gray_template_upscaled) for 0-359 at step_deg.

    For each target angle the closest real cardinal reference image is used
    and rotated by the small residual delta (<=45 deg), keeping synthetic
    rotation artefacts minimal.
    """
    cardinal_gray = {}
    for deg in (0, 90, 180, 270):
        gray = _load_ref_gray(deg)
        h, w = gray.shape[:2]
        cardinal_gray[deg] = cv2.resize(gray, (w * upscale, h * upscale),
                                        interpolation=cv2.INTER_LANCZOS4)
    bank: list = []
    for a in range(0, 360, step_deg):
        best_card = min((0, 90, 180, 270),
                        key=lambda r: min((a - r) % 360, (r - a) % 360))
        delta = (a - best_card + 180) % 360 - 180   # signed, <=45
        tmpl  = _rotate(cardinal_gray[best_card], delta)
        bank.append((a, tmpl))
    return bank

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _upscale_gray(bgr_img: np.ndarray, upscale: int = UPSCALE) -> np.ndarray:
    gray = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    return cv2.resize(gray, (w * upscale, h * upscale),
                      interpolation=cv2.INTER_LANCZOS4)


def match_angle(gray_up: np.ndarray, bank: list) -> tuple:
    """NCC match against every template. Returns (best_angle_deg, best_score)."""
    best_score = -2.0
    best_angle = 0
    for angle, tmpl in bank:
        res   = cv2.matchTemplate(gray_up, tmpl, cv2.TM_CCOEFF_NORMED)
        score = float(res[0, 0])
        if score > best_score:
            best_score = score
            best_angle = angle
    return best_angle, best_score


def grab_arrow_bgr(sct, region: dict) -> np.ndarray:
    shot = sct.grab(region)
    return np.array(shot)[:, :, :3]


def detect_once(bank: list, sct) -> tuple:
    """Single screen grab -> (angle_deg, score)."""
    bgr     = grab_arrow_bgr(sct, _get_arrow_region(sct))
    gray_up = _upscale_gray(bgr)
    return match_angle(gray_up, bank)

# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test(bank: list) -> bool:
    print("\nSelf-test against the 4 reference images:")
    ok_all = True
    for true_deg in (0, 90, 180, 270):
        gray    = _load_ref_gray(true_deg)
        gray_up = _upscale_gray(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
        pred, score = match_angle(gray_up, bank)
        err  = min((pred - true_deg) % 360, (true_deg - pred) % 360)
        ok   = "OK  " if err <= STEP_DEG else "FAIL"
        clock = {0: "12", 90: "3", 180: "6", 270: "9"}[true_deg]
        print(f"  [{ok}]  true={true_deg:3d}deg ({clock} o'clock)"
              f"  -> pred={pred:3d}deg  err={err:2d}deg  score={score:.3f}")
        if ok.strip() != "OK":
            ok_all = False
    return ok_all

# ---------------------------------------------------------------------------
# Camera drag helpers
# ---------------------------------------------------------------------------

def _angle_delta(current: int, target: int) -> int:
    """Signed shortest angular difference in (-180, +180].
    Positive = CW, negative = CCW."""
    return (target - current + 180) % 360 - 180


def _delta_to_px(delta_deg: int) -> int:
    """Convert angular delta to drag pixels (positive = right = CW)."""
    return round(delta_deg * PIXELS_PER_360 / 360)


def _do_camera_drag(hid, dx: int, settle_s: float = DRAG_SETTLE_S) -> None:
    """Move cursor to neutral position, fire one DRAG_RIGHT, then return."""
    hid.move_to(DRAG_START_X, DRAG_START_Y)
    time.sleep(0.05)
    hid._send(f"DRAG_RIGHT,{dx},0")
    time.sleep(settle_s)
    hid.move_to(DRAG_START_X, DRAG_START_Y)
    time.sleep(0.05)

# ---------------------------------------------------------------------------
# Live display mode
# ---------------------------------------------------------------------------

def _nn(img: np.ndarray, size: int) -> np.ndarray:
    return cv2.resize(img, (size, size), interpolation=cv2.INTER_NEAREST)


def _clock_label(deg: int) -> str:
    table = {0: "12:00", 45: "1:30", 90: "3:00", 135: "4:30",
             180: "6:00", 225: "7:30", 270: "9:00", 315: "10:30"}
    return table.get(deg, f"{deg}deg")


def run_live(bank: list) -> None:
    print("\nLive detection started.  Press 'q' to quit, 's' to save grab.\n")
    pane = DISPLAY_PX
    with mss.mss() as sct:
        while True:
            t0      = time.perf_counter()
            bgr     = grab_arrow_bgr(sct, _get_arrow_region(sct))
            gray_up = _upscale_gray(bgr)
            angle, score = match_angle(gray_up, bank)
            dt_ms   = (time.perf_counter() - t0) * 1000

            print(f"\r  {angle:3d}deg  {_clock_label(angle):<7s}"
                  f"  score={score:.3f}  {dt_ms:.0f}ms   ", end="", flush=True)

            live_gray = _nn(gray_up, pane)
            live_bgr  = cv2.cvtColor(live_gray, cv2.COLOR_GRAY2BGR)

            tmpl_bgr = live_bgr.copy()
            for a, tmpl in bank:
                if a == angle:
                    tmpl_bgr = cv2.cvtColor(_nn(tmpl, pane), cv2.COLOR_GRAY2BGR)
                    break

            colour_pane = cv2.resize(bgr, (pane, pane), interpolation=cv2.INTER_NEAREST)
            gap   = np.zeros((pane, 6, 3), dtype=np.uint8)
            panel = np.hstack([live_bgr, gap, tmpl_bgr, gap, colour_pane])

            # Direction arrow overlay on live pane
            cx, cy = pane // 2, pane // 2
            r   = pane // 2 - 8
            rad = np.radians(angle)
            ax  = int(cx + r * np.sin(rad))
            ay  = int(cy - r * np.cos(rad))
            cv2.arrowedLine(panel, (cx, cy), (ax, ay), (0, 255, 0), 2, tipLength=0.4)

            bar = np.zeros((28, panel.shape[1], 3), dtype=np.uint8)
            cv2.putText(bar,
                        f"{angle}deg ({_clock_label(angle)})  score={score:.3f}  {dt_ms:.0f}ms",
                        (4, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 255, 0), 1)

            col_labels = np.zeros((16, panel.shape[1], 3), dtype=np.uint8)
            for i, lbl in enumerate(["Live (gray)", "Best template", "Colour grab"]):
                cv2.putText(col_labels, lbl, (i * (pane + 6) + 4, 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (160, 160, 160), 1)

            cv2.imshow("Minimap Orient", np.vstack([col_labels, panel, bar]))
            key = cv2.waitKey(LIVE_MS) & 0xFF
            if key == ord('q'):
                break
            if key == ord('s'):
                os.makedirs("logs", exist_ok=True)
                ts   = int(time.time())
                path = f"logs/arrow_grab_{ts}_{angle}deg.png"
                cv2.imwrite(path, colour_pane)
                print(f"\n  Saved: {path}")

    cv2.destroyAllWindows()
    print()

# ---------------------------------------------------------------------------
# Align mode
# ---------------------------------------------------------------------------

def run_align(bank: list, port: str = None, settle_s: float = DRAG_SETTLE_S) -> None:
    """
    Interactive loop:
      1. Detect current angle.
      2. Prompt for target angle.
      3. Calculate shortest-path delta -> pixels.
      4. Execute ONE camera drag.
      5. Re-detect and report error.
    """
    if not _HID_AVAILABLE:
        print("ERROR: Arduino HID module not found (PXM_RB/common/arduino_hid.py).")
        print("       Align mode requires the Arduino to be connected.")
        return

    resolved_port = port or find_arduino_port()
    if not resolved_port:
        print("ERROR: Arduino not found. Connect it or pass --port COM#.")
        return

    hid = ArduinoHID(resolved_port)
    if not hid.connect():
        print(f"ERROR: Could not connect to Arduino on {resolved_port}.")
        return

    print(f"Arduino connected on {resolved_port}")
    print(f"Calibration: 360 deg = {PIXELS_PER_360} px  "
          f"(1 deg = {PIXELS_PER_360/360:.2f} px)")
    print(f"Settle delay after drag: {settle_s:.2f} s")
    print(f"Template step: {STEP_DEG} deg  (precision: +-{STEP_DEG//2} deg)")
    print()
    print("NOTE: positive delta -> CW rotation -> drag RIGHT.")
    print("      If directions are inverted, negate dx in _do_camera_drag().")
    print()

    with mss.mss() as sct:
        while True:
            # --- detect current ---
            cur, cur_score = detect_once(bank, sct)
            print(f"Current orientation: {cur:3d}deg  ({_clock_label(cur)})  score={cur_score:.3f}")

            # --- prompt ---
            raw = input("Target angle (0-359, Enter=re-detect, q=quit): ").strip()
            if raw.lower() == "q":
                break
            if raw == "":
                continue
            try:
                target = int(raw) % 360
            except ValueError:
                print("  Invalid input – enter an integer 0-359.")
                continue

            # --- iterative correction loop ---
            current = cur
            for iteration in range(1, ALIGN_MAX_ITER + 1):
                delta = _angle_delta(current, target)
                if abs(delta) <= ALIGN_TOL_DEG:
                    print(f"  [iter {iteration}] Already within tolerance "
                          f"(err={delta:+d}deg <= +-{ALIGN_TOL_DEG}deg). Done.")
                    break

                dx        = _delta_to_px(delta)
                direction = "right (CW) " if dx >= 0 else "left  (CCW)"
                print(f"  [iter {iteration}] current={current:3d}deg  "
                      f"delta={delta:+4d}deg  dx={dx:+5d}px  {direction}",
                      end="  dragging...", flush=True)

                _do_camera_drag(hid, dx, settle_s=settle_s)

                current, score = detect_once(bank, sct)
                remaining = _angle_delta(target, current)
                print(f" -> {current:3d}deg  score={score:.3f}  "
                      f"err={remaining:+d}deg")

                if abs(remaining) <= ALIGN_TOL_DEG:
                    print(f"  Converged after {iteration} segment(s).  "
                          f"Final error: {remaining:+d}deg")
                    break
            else:
                print(f"  Reached iteration limit ({ALIGN_MAX_ITER}).  "
                      f"Final: {current}deg  err={_angle_delta(target, current):+d}deg")
            print()

    hid.close()
    print("Done.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Minimap arrow detector + camera aligner")
    ap.add_argument("--mode",   choices=["live", "align"], default="live",
                    help="live = continuous display (default); align = interactive camera alignment")
    ap.add_argument("--port",   default=None,
                    help="Arduino COM port for align mode (auto-detected if omitted)")
    ap.add_argument("--settle", type=float, default=DRAG_SETTLE_S,
                    help=f"Seconds to wait after drag before re-reading arrow (default {DRAG_SETTLE_S})")
    args = ap.parse_args()

    print("Building template bank...")
    bank = build_template_bank()
    print(f"  {len(bank)} templates  ({STEP_DEG}deg step, {UPSCALE}x upscale)")

    passed = self_test(bank)
    if not passed:
        print("\n[WARNING] Self-test failed – matching may be unreliable.")
    print()

    if args.mode == "align":
        run_align(bank, port=args.port, settle_s=args.settle)
    else:
        run_live(bank)


if __name__ == "__main__":
    main()
