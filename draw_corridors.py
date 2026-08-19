import cv2
import numpy as np
import math

BASE = r"C:\Users\Den\AppData\Roaming\Cursor\User\workspaceStorage\195f4bcdff443d6a823152791e6f24b0\images"
OUT  = r"C:\PXM_LU4\logs"

images = [
    ("image-046884dd-0515-4c9c-90c4-a9be325cb49f.png", "img1_corridor.png"),
    ("image-46adb6c1-d9fe-4e27-8481-620dcc4265b4.png", "img2_corridor.png"),
    ("image-f1ceb711-c336-4e13-ba1b-07941c873baa.png", "img3_corridor.png"),
    ("image-6042c6b1-9d87-4a7e-9776-b1fa127d23a0.png", "img4_corridor.png"),
]

# Manually estimated positions: (sc_x, sc_y, dot_x, dot_y)
# sc = character centre (Snitch body); dot = midpoint of two blue in_target_blue dots
params = [
    (155, 430, 155,  50),   # img1: mob directly above (12 o'clock)
    (175, 500, 475,  50),   # img2: mob upper-right (~1 o'clock)
    (225, 470, 850, 140),   # img3: mob upper-right (~2 o'clock)
    (220, 475, 875, 195),   # img4: mob far right   (~3 o'clock)
]

SA_APPROACH_DOWN_OFFSET_MAX = 80
CHAR_SPEED_PX_PER_MS        = 0.2
DCLK_DELAY_1_MAX_MS         = 1500


def adjusted_target(sc_x, sc_y, tx, ty, down_max):
    dx, dy   = tx - sc_x, ty - sc_y
    length   = math.hypot(dx, dy) or 1
    h_factor = abs(dx) / length
    down_px  = int(h_factor * down_max)
    return tx, ty + down_px, h_factor, down_px


def point_along(sc_x, sc_y, tx, ty, dist):
    dx, dy = tx - sc_x, ty - sc_y
    length = math.hypot(dx, dy) or 1
    return (int(sc_x + dx / length * dist),
            int(sc_y + dy / length * dist))


def label(img, txt, pt, col=(0, 230, 100)):
    x, y = pt[0] + 8, pt[1] - 6
    cv2.putText(img, txt, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                0.42, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, txt, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                0.42, col, 1, cv2.LINE_AA)


for (src, dst), (sc_x, sc_y, dot_x, dot_y) in zip(images, params):
    img = cv2.imread(f"{BASE}\\{src}")
    if img is None:
        import os; print(f"Files: {os.listdir(BASE)[:5]}")
    if img is None:
        print(f"Cannot load {src}")
        continue

    eff_tx, eff_ty, h_fac, dpx = adjusted_target(
        sc_x, sc_y, dot_x, dot_y, SA_APPROACH_DOWN_OFFSET_MAX)

    eff_len = math.hypot(eff_tx - sc_x, eff_ty - sc_y)
    min_d   = int(DCLK_DELAY_1_MAX_MS * CHAR_SPEED_PX_PER_MS)  # 300 px
    cap     = eff_len * 0.85
    lo      = min(float(min_d), cap * 0.95)

    pt_near = point_along(sc_x, sc_y, eff_tx, eff_ty, lo)
    pt_far  = point_along(sc_x, sc_y, eff_tx, eff_ty, cap)

    # Raw direction (grey, dashed visual)
    cv2.line(img, (sc_x, sc_y), (dot_x, dot_y), (110, 110, 110), 1, cv2.LINE_AA)

    # Adjusted corridor axis
    cv2.line(img, (sc_x, sc_y), (eff_tx, eff_ty), (0, 220, 255), 2, cv2.LINE_AA)

    # Click-range segment (thick green)
    cv2.line(img, pt_near, pt_far, (0, 255, 160), 5, cv2.LINE_AA)
    cv2.circle(img, pt_near, 6, (0, 255, 80),  -1)
    cv2.circle(img, pt_far,  6, (0, 200, 60),  -1)

    # Key points
    cv2.circle(img, (dot_x,  dot_y),  7, (0, 160, 255), -1)   # raw dots  (orange)
    cv2.circle(img, (eff_tx, eff_ty), 7, (0, 220,   0), -1)   # adj target (green)
    cv2.circle(img, (sc_x,   sc_y),   8, (0,   0, 220), -1)   # character  (red)

    # Down-offset indicator
    cv2.arrowedLine(img, (dot_x, dot_y), (eff_tx, eff_ty),
                    (0, 255, 255), 2, cv2.LINE_AA, tipLength=0.3)

    label(img, f"h={h_fac:.2f}  down={dpx}px",
          (eff_tx, eff_ty), (0, 230, 100))
    label(img, f"near {int(lo)}px",  pt_near, (0, 255, 160))
    label(img, f"far  {int(cap):.0f}px", pt_far,  (0, 255, 160))

    out_path = f"{OUT}\\{dst}"
    cv2.imwrite(out_path, img)
    print(f"Saved: {out_path}")

print("Done")
