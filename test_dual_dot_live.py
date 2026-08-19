"""
test_dual_dot_live.py  -  print the center between paired blue dots in real time.

No preview window. Grabs the full primary monitor with mss, finds in_target_blue
pairs, and prints the midpoint coordinates to the console whenever a pair is
detected.

Press Ctrl+C to stop.

Usage:
    python test_dual_dot_live.py [--conf 0.75] [--tmpl path/to/in_target_blue.png]
"""
import argparse
import os
import sys
import time

import cv2
import numpy as np
import mss

DEFAULT_TMPL = os.path.join(os.path.dirname(__file__), "assets", "in_target_blue.png")
DEFAULT_CONF = 0.75
MAX_PAIR_DY  = 20
MIN_PAIR_DX  = 30


def nms_points(pts, min_dist):
    result = []
    for p in pts:
        if all(abs(p[0]-r[0]) > min_dist or abs(p[1]-r[1]) > min_dist for r in result):
            result.append(p)
    return result


def find_dots(frame, tmpl, conf, nms_dist):
    th, tw = tmpl.shape[:2]
    res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= conf)
    raw = [(int(x + tw//2), int(y + th//2)) for x, y in zip(xs, ys)]
    return nms_points(raw, nms_dist)


def pair_dots(dots):
    used, pairs = set(), []
    for i, a in enumerate(dots):
        if i in used:
            continue
        for j, b in enumerate(dots):
            if j <= i or j in used:
                continue
            if abs(a[1]-b[1]) <= MAX_PAIR_DY and abs(a[0]-b[0]) >= MIN_PAIR_DX:
                left  = a if a[0] < b[0] else b
                right = b if a[0] < b[0] else a
                pairs.append((left, right))
                used.add(i); used.add(j)
                break
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tmpl", default=DEFAULT_TMPL)
    ap.add_argument("--conf", type=float, default=DEFAULT_CONF)
    args = ap.parse_args()

    tmpl = cv2.imread(args.tmpl)
    if tmpl is None:
        print(f"ERROR: template not found: {args.tmpl}", file=sys.stderr)
        sys.exit(1)
    th, tw = tmpl.shape[:2]
    nms_dist = max(tw, th) // 2

    print(f"Template : {args.tmpl}  {tw}x{th}  conf={args.conf}")
    print("Running  — Ctrl+C to stop.\n")

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        while True:
            t0    = time.perf_counter()
            raw   = sct.grab(monitor)
            frame = cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)

            dots  = find_dots(frame, tmpl, args.conf, nms_dist)
            pairs = pair_dots(dots)

            dt  = time.perf_counter() - t0
            fps = 1.0 / dt if dt > 0 else 0

            if pairs:
                centers = [((l[0]+r[0])//2, (l[1]+r[1])//2) for l, r in pairs]
                print(f"{fps:5.1f} fps | dots:{len(dots)} | "
                      f"centers: {centers}")
            else:
                print(f"{fps:5.1f} fps | dots:{len(dots)} | no pairs", end="\r")


if __name__ == "__main__":
    main()
