"""Standalone test: capture a region around the cursor after every LMB click
and immediately report whether the ground-click animation was visible.

Detection method: HSV thresholding for the bright blue/white glow that the
game renders on the ground when a click lands.  No template image needed.

Config at the top of this file:
    DELAY_S          – seconds to wait after LMB press before capturing
    REGION_W/H       – size of the captured region (px), centred on click point
    SAVE_DIR         – folder where screenshots are saved (set to "" to skip saving)

Detection tuning:
    HUE_LO / HUE_HI  – hue range of the blue glow (OpenCV: 0–180)
    SAT_LO            – minimum saturation
    VAL_LO            – minimum brightness
    MIN_PX            – minimum number of matching pixels to count as "detected"
"""

import ctypes
import pathlib
import time

import cv2
import mss
import numpy as np

# ---------------------------------------------------------------------------
# Config — capture
# ---------------------------------------------------------------------------
DELAY_S  = 0.3     # seconds to wait after LMB before capturing
REGION_W = 100        # capture width  (px)
REGION_H = 100        # capture height (px)
SAVE_DIR = r"C:\PXM_LU4\logs\ground_click_samples"   # "" = do not save

# ---------------------------------------------------------------------------
# Config — detection (HSV thresholds for the bright blue/white glow)
# ---------------------------------------------------------------------------
# Blue glow — only blue pixels count; white is excluded because character
# name labels (e.g. "Dorin") are rendered as white text and would cause
# false positives.
HUE_LO  =  90   # blue hue lower bound  (OpenCV 0–180)
HUE_HI  = 135   # blue hue upper bound
SAT_LO  =  50   # minimum saturation — glow pixels are relatively desaturated
SAT_HI  = 130   # maximum saturation — icon pixels avg 139–206, glow avg 110–115;
                 # this cap eliminates the bulk of icon pixels while keeping glow
VAL_LO  = 140   # minimum brightness  (0–255)
MIN_PX  =   3   # blue pixel count threshold — fewer → NO, equal/more → YES
# Detection is performed only inside a central crop of the captured region.
# The ground-click glow always appears at the click point (centre of the
# capture), while in_target_blue icons float above characters and land in
# the upper portion of the frame.  Limiting the search area to a small
# central window excludes the icon without any colour-based tricks.
CHECK_W = 40    # width  of the central crop to check (px)
CHECK_H = 40    # height of the central crop to check (px)

# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

_user32 = ctypes.windll.user32

def cursor_pos():
    p = _POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y

def lmb_down() -> bool:
    return bool(_user32.GetAsyncKeyState(0x01) & 0x8000)

def capslock_on() -> bool:
    return bool(_user32.GetKeyState(0x14) & 0x0001)

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def detect_ground_click(img_bgr: np.ndarray) -> tuple[bool, int]:
    """Return (detected, blue_px) for the ground-click glow.

    Only the central CHECK_W × CHECK_H crop is examined so that
    in_target_blue icons floating above characters (which land in the
    upper portion of the capture) are ignored.
    """
    h, w  = img_bgr.shape[:2]
    cy, cx = h // 2, w // 2
    y0 = max(0, cy - CHECK_H // 2)
    y1 = min(h, cy + CHECK_H // 2)
    x0 = max(0, cx - CHECK_W // 2)
    x1 = min(w, cx + CHECK_W // 2)
    crop = img_bgr[y0:y1, x0:x1]

    hsv  = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv,
                       np.array([HUE_LO, SAT_LO, VAL_LO]),
                       np.array([HUE_HI, SAT_HI, 255   ]))
    b = int(cv2.countNonZero(mask))
    return b >= MIN_PX, b

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    out_dir = pathlib.Path(SAVE_DIR) if SAVE_DIR else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Monitoring LMB clicks (delay={DELAY_S}s, region={REGION_W}x{REGION_H})")
    print(f"Detection: hue={HUE_LO}-{HUE_HI}  sat={SAT_LO}-{SAT_HI}  val>={VAL_LO}  "
          f"min_px={MIN_PX}  check={CHECK_W}x{CHECK_H} centre crop")
    if out_dir:
        print(f"Saving captures to: {out_dir}")
    print("CapsLock ON = paused.  Press Ctrl+C to stop.\n")

    counter  = 0
    was_down = False

    with mss.mss() as sct:
        while True:
            # ---- CapsLock pause ------------------------------------------------
            if capslock_on():
                if not getattr(main, "_paused", False):
                    print("[PAUSED] CapsLock is ON — waiting...")
                    main._paused = True
                time.sleep(0.1)
                was_down = lmb_down()
                continue
            if getattr(main, "_paused", False):
                print("[RESUMED] CapsLock is OFF — monitoring clicks.")
                main._paused = False

            # ---- Edge detection: LMB just pressed ------------------------------
            pressed = lmb_down()
            if pressed and not was_down:
                cx, cy = cursor_pos()          # position at click time
                time.sleep(DELAY_S)            # wait for animation to appear

                half_w = REGION_W // 2
                half_h = REGION_H // 2
                monitor = {
                    "left":   cx - half_w,
                    "top":    cy - half_h,
                    "width":  REGION_W,
                    "height": REGION_H,
                }
                shot = sct.grab(monitor)
                img  = np.frombuffer(shot.raw, dtype=np.uint8).reshape(
                    (shot.height, shot.width, 4))
                img  = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

                detected, b = detect_ground_click(img)
                counter += 1

                result = "YES" if detected else "NO "
                print(f"[{counter:04d}] cursor=({cx},{cy})  "
                      f"blue_px={b:3d}  "
                      f"Ground click detected: {result}")

                if out_dir:
                    fname = out_dir / f"capture_{counter:04d}.png"
                    cv2.imwrite(str(fname), img)

            was_down = pressed
            time.sleep(0.005)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
