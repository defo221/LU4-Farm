"""
gamma_tuner.py — drag the trackbar to find the best gamma for a region.

Usage:
    python gamma_tuner.py image.png x0 y0 x1 y1

Example (the dark dialog box from the death screenshot):
    python gamma_tuner.py logs\Lu4_death.png 453 195 572 256
"""

import sys
import numpy as np
import cv2

def main():
    if len(sys.argv) < 6:
        print("Usage: gamma_tuner.py <image> <x0> <y0> <x1> <y1>")
        sys.exit(1)

    path      = sys.argv[1]
    x0, y0    = int(sys.argv[2]), int(sys.argv[3])
    x1, y1    = int(sys.argv[4]), int(sys.argv[5])

    orig = cv2.imread(path)
    if orig is None:
        print(f"Cannot open: {path}")
        sys.exit(1)

    win = "Gamma tuner  (Q = quit, S = save)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, min(orig.shape[1], 1200), min(orig.shape[0], 700))

    # Trackbar: 10–500  →  gamma = value / 100  (1.0 – 5.0, default 1.0)
    cv2.createTrackbar("gamma x100", win, 100, 500, lambda _: None)

    lut_cache: dict[int, np.ndarray] = {}

    def make_lut(gamma100: int) -> np.ndarray:
        if gamma100 not in lut_cache:
            g = max(gamma100, 1) / 100.0
            table = np.array([((i / 255.0) ** (1.0 / g)) * 255
                              for i in range(256)], dtype=np.uint8)
            lut_cache[gamma100] = table
        return lut_cache[gamma100]

    while True:
        gamma100 = cv2.getTrackbarPos("gamma x100", win)
        lut      = make_lut(gamma100)

        frame = orig.copy()
        region = frame[y0:y1, x0:x1]
        frame[y0:y1, x0:x1] = cv2.LUT(region, lut)

        # Draw box and label
        cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 220, 0), 1)
        label = f"gamma = {gamma100/100:.2f}"
        cv2.putText(frame, label, (x0, max(y0-6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1)

        cv2.imshow(win, frame)
        key = cv2.waitKey(30) & 0xFF

        if key == ord('q') or key == 27:
            break
        if key == ord('s'):
            out = f"gamma_{gamma100/100:.2f}.png"
            cv2.imwrite(out, frame)
            print(f"Saved: {out}")

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
