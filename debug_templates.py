"""Diagnostic: print exactly which file each template resolves to at runtime."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg
import cv2

A = cfg.PROFILE["assets_dir"]
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

print(f"RESOLUTION  : {cfg.RESOLUTION}")
print(f"A (profile) : {A}")
print(f"R (root)    : {R}")
print(f"A == R      : {A == R}")
print()

names = [
    "bag_mob_anchor.png", "char_bars_anchor.png",
    "mob_dead.png", "death_screen.png",
    "full_buff_check.png", "full_buff_check1.png",
    "party_pl_anchor.png", "in_target_red.png", "in_target_blue.png",
]

for name in names:
    p = os.path.join(A, name)
    if os.path.isfile(p):
        img = cv2.imread(p)
        src = "PROFILE"
        sz  = f"{img.shape[1]}x{img.shape[0]}" if img is not None else "EXISTS but CORRUPT"
        path_used = p
    else:
        fb = os.path.join(R, name)
        img = cv2.imread(fb)
        src = "ROOT fallback"
        sz  = f"{img.shape[1]}x{img.shape[0]}" if img is not None else "MISSING EVERYWHERE"
        path_used = fb
    print(f"  [{src:14s}]  {sz:22s}  {name}")
    print(f"                       -> {path_used}")
