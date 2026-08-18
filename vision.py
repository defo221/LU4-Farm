"""
vision.py - screen READING only (template matching via screen capture).

pyautogui.locate* uses screen capture under the hood — reading the screen, not
injecting input. All resulting clicks go to the Arduino HID layer. We never call
pyautogui.click / pyautogui.press.

Every image check and every click attempt is logged (mirrors the reference bot's
"ui.autohunt_disabled with confidence 0.9 - ✓/✗" style).
"""

import os
import pyautogui as _pag
import logger

try:
    _IMG_NOT_FOUND = _pag.ImageNotFoundException
except Exception:
    _IMG_NOT_FOUND = Exception


def _label(image_path):
    """Return a short human-readable label for logging."""
    return os.path.basename(image_path) if image_path else "?"


def screen_size():
    """Return (width, height) of the primary screen."""
    try:
        return _pag.size()
    except Exception:
        return 1920, 1080


def screenshot(region=None):
    """Capture a single screenshot (PIL Image). Pass to *_in() functions to avoid
    repeated screen grabs for multiple checks in the same iteration."""
    try:
        return _pag.screenshot(region=region)
    except Exception as e:
        logger.warn(f"[VISION] screenshot failed: {e}")
        return None


def locate(image_path, confidence=0.8, region=None, log=True):
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        result = _pag.locateOnScreen(image_path, confidence=confidence, region=region)
        if log:
            mark = "✓" if result else "✗"
            logger.info(f"{_label(image_path)} with confidence {confidence} - {mark}")
        return result
    except _IMG_NOT_FOUND:
        if log:
            logger.info(f"{_label(image_path)} with confidence {confidence} - ✗")
        return None
    except Exception as e:
        logger.warn(f"[VISION] locate error {_label(image_path)}: {e}")
        return None


def locate_in(captured, image_path, confidence=0.8, log=True):
    """Search image_path inside an already-captured PIL screenshot (no new grab)."""
    if captured is None:
        return locate(image_path, confidence=confidence, log=log)
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        result = _pag.locate(image_path, captured, confidence=confidence)
        if log:
            mark = "✓" if result else "✗"
            logger.info(f"{_label(image_path)} with confidence {confidence} - {mark}")
        return result
    except _IMG_NOT_FOUND:
        if log:
            logger.info(f"{_label(image_path)} with confidence {confidence} - ✗")
        return None
    except Exception as e:
        logger.warn(f"[VISION] locate_in error {_label(image_path)}: {e}")
        return None


def locate_center(image_path, confidence=0.8, region=None, log=True):
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        result = _pag.locateCenterOnScreen(image_path, confidence=confidence, region=region)
        if log:
            mark = "✓" if result else "✗"
            logger.info(f"{_label(image_path)} with confidence {confidence} - {mark}")
        return result
    except _IMG_NOT_FOUND:
        if log:
            logger.info(f"{_label(image_path)} with confidence {confidence} - ✗")
        return None
    except Exception as e:
        logger.warn(f"[VISION] locate_center error {_label(image_path)}: {e}")
        return None


def exists(image_path, confidence=0.8, region=None, log=True):
    return locate(image_path, confidence=confidence, region=region, log=log) is not None


def exists_in(captured, image_path, confidence=0.8, log=True):
    """Check existence using a pre-captured screenshot. Falls back to live grab if None."""
    return locate_in(captured, image_path, confidence=confidence, log=log) is not None


def find_and_click(hid, image_path, confidence=0.8, region=None,
                   hold_min=20, hold_max=50):
    """Locate image and click its center via Arduino. Returns True on click."""
    pt = locate_center(image_path, confidence=confidence, region=region)
    if pt is None:
        return False
    logger.info(f"Clicking Point(x={pt.x}, y={pt.y}). Image: {_label(image_path)}")
    return hid.move_and_click(int(pt.x), int(pt.y), hold_min, hold_max)


def find_and_double_click(hid, image_path, confidence=0.8, region=None,
                          gap_min=80, gap_max=160, y_shift=0):
    """Locate image once, move there (+y_shift px down), then double-click via Arduino."""
    pt = locate_center(image_path, confidence=confidence, region=region)
    if pt is None:
        return False
    target_y = int(pt.y) + y_shift
    logger.info(f"Double-clicking Point(x={pt.x}, y={target_y}). Image: {_label(image_path)}"
                + (f" (y_shift={y_shift:+d})" if y_shift else ""))
    return hid.double_click_at(int(pt.x), target_y, gap_min, gap_max)


def find_and_click_offset(hid, image_path, confidence=0.8, region=None,
                          off_min=3, off_max=9, hold_min=20, hold_max=50):
    pt = locate_center(image_path, confidence=confidence, region=region)
    if pt is None:
        return False
    logger.info(f"Clicking Point(x={pt.x}, y={pt.y}). Image: {_label(image_path)} (with offset)")
    return hid.move_and_click_offset(int(pt.x), int(pt.y), off_min, off_max,
                                     hold_min, hold_max)
