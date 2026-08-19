"""
test_dual_dot.py  -  detect both in_target_blue dots and find the center between them.

Run:  python test_dual_dot.py [--tmpl path/to/in_target_blue.png] [--conf 0.75]
                              [--image path/to/screenshot.png]  (skip live capture)

With no --image flag the script grabs a live screenshot of the primary monitor.
A window opens showing:
  - green rectangle around every matched dot
  - red crosshair at the midpoint of each left/right pair
  - yellow midpoint coordinates printed in the console

Close the window with any key or Q.
"""
import argparse
import os
import sys

import cv2
import numpy as np
import mss

# ── config defaults ─────────────────────────────────────────────────────────
DEFAULT_TMPL  = os.path.join(os.path.dirname(__file__), "assets", "in_target_blue.png")
DEFAULT_CONF  = 0.75   # matchTemplate score threshold
MAX_PAIR_DY   = 20     # dots must be within this many px vertically to be paired
MIN_PAIR_DX   = 30     # dots must be at least this far apart horizontally to be a pair


# ── helpers ──────────────────────────────────────────────────────────────────
def nms_points(pts, min_dist=12):
    """Simple non-maximum suppression: keep one point per cluster."""
    result = []
    for p in pts:
        if all(abs(p[0]-r[0]) > min_dist or abs(p[1]-r[1]) > min_dist for r in result):
            result.append(p)
    return result


def find_all_dots(frame_bgr, tmpl_bgr, conf):
    """Return list of (cx, cy) for every match above conf."""
    th, tw = tmpl_bgr.shape[:2]
    res = cv2.matchTemplate(frame_bgr, tmpl_bgr, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= conf)
    raw = [(int(x + tw//2), int(y + th//2)) for x, y in zip(xs, ys)]
    return nms_points(raw, min_dist=max(tw, th) // 2)


def pair_dots(dots, max_dy=MAX_PAIR_DY, min_dx=MIN_PAIR_DX):
    """
    Group dots into (left, right) pairs that are on the same horizontal band.
    Returns list of ((lx,ly), (rx,ry)).
    """
    used = set()
    pairs = []
    for i, a in enumerate(dots):
        if i in used:
            continue
        for j, b in enumerate(dots):
            if j <= i or j in used:
                continue
            dy = abs(a[1] - b[1])
            dx = abs(a[0] - b[0])
            if dy <= max_dy and dx >= min_dx:
                left  = a if a[0] < b[0] else b
                right = b if a[0] < b[0] else a
                pairs.append((left, right))
                used.add(i)
                used.add(j)
                break
    return pairs


def midpoint(left, right):
    return ((left[0] + right[0]) // 2, (left[1] + right[1]) // 2)


def draw_crosshair(img, pt, color=(0,0,255), size=16, thickness=2):
    x, y = pt
    cv2.line(img, (x - size, y), (x + size, y), color, thickness)
    cv2.line(img, (x, y - size), (x, y + size), color, thickness)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmpl",  default=DEFAULT_TMPL)
    ap.add_argument("--conf",  type=float, default=DEFAULT_CONF)
    ap.add_argument("--image", default=None,
                    help="path to a saved screenshot instead of live capture")
    args = ap.parse_args()

    # Load template
    tmpl = cv2.imread(args.tmpl)
    if tmpl is None:
        print(f"ERROR: template not found: {args.tmpl}", file=sys.stderr)
        sys.exit(1)
    print(f"Template: {args.tmpl}  size={tmpl.shape[1]}x{tmpl.shape[0]}")

    # Capture or load frame
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            print(f"ERROR: cannot read image: {args.image}", file=sys.stderr)
            sys.exit(1)
        print(f"Using saved image: {args.image}  size={frame.shape[1]}x{frame.shape[0]}")
    else:
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
        frame = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)
        print(f"Live screenshot: {frame.shape[1]}x{frame.shape[0]}")

    # Detect dots
    dots = find_all_dots(frame, tmpl, args.conf)
    print(f"\nDots found ({len(dots)}):")
    for d in dots:
        print(f"  {d}")

    # Pair them
    pairs = pair_dots(dots)
    print(f"\nPairs found ({len(pairs)}):")
    for left, right in pairs:
        mid = midpoint(left, right)
        print(f"  left={left}  right={right}  CENTER={mid}")

    # Annotate image
    vis = frame.copy()
    th, tw = tmpl.shape[:2]
    for cx, cy in dots:
        cv2.rectangle(vis,
                      (cx - tw//2, cy - th//2),
                      (cx + tw//2, cy + th//2),
                      (0, 220, 0), 1)

    for left, right in pairs:
        mid = midpoint(left, right)
        # Line between the two dots
        cv2.line(vis, left, right, (0, 200, 255), 1)
        # Crosshair at center
        draw_crosshair(vis, mid, color=(0, 0, 255), size=20)
        # Label
        cv2.putText(vis, f"{mid}", (mid[0] + 5, mid[1] - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    # Show
    win = "Dual-dot detection  (any key to close)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    h, w = vis.shape[:2]
    cv2.resizeWindow(win, min(w, 1600), min(h, 900))
    cv2.imshow(win, vis)
    key = cv2.waitKey(0)
    cv2.destroyAllWindows()

    if not pairs:
        print("\nNo pairs found. Try lowering --conf (current: "
              f"{args.conf}) or check that the correct template is used.")


if __name__ == "__main__":
    main()
