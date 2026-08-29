"""
config.py  -  All bot settings in one place.
"""

import os

_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Machine / Telegram identity
# ---------------------------------------------------------------------------
PC_NUMBER  = "01-00"           # prefixed to every Telegram notification
TG_TOKEN   = "8411193522:AAGGEfaom9zwPtCMuuLVVTNANYhVbR1PMOg"
TG_CHAT_ID = "-5452091278"

# ---------------------------------------------------------------------------
# Pause hotkey
# ---------------------------------------------------------------------------
PAUSE_KEY = "CapsLock"       # "ScrollLock"  or  "CapsLock"

# ---------------------------------------------------------------------------
# Arduino
# ---------------------------------------------------------------------------
ARDUINO_PORT = None            # None = auto-detect by "Arduino" keyword in port description
                               # set to e.g. "COM12" to force a specific port
ARDUINO_BAUD = 9600

# ---------------------------------------------------------------------------
# Game windows
#
# title        - substring of the window title (case-insensitive match)
# char_role    - "DD" (default) — only DD logic is implemented
# enabled      - set to False to skip this window and run in single-window mode
# taskbar_pos  - if set (1-based integer), the bot switches to this window by
#                pressing Win+N via Arduino instead of minimize→restore.
#                Much lower latency. Requires:
#                  1. Game exe pinned to taskbar at that position.
#                  2. Firmware flashed with gui/win key support (already done).
#                Set to None to keep using the minimize→restore approach.
# ---------------------------------------------------------------------------
WINDOWS = {
    "win1": {
        "title":       "Snitch",
        "char_role":   "DD",
        "enabled":     True,
        "taskbar_pos": 1,               # Win+1 — first icon after the search bar
    },
    "win2": {
        "title":       "Qwinsa",
        "char_role":   "DD",
        "enabled":     False,
        "taskbar_pos": 2,               # Win+2
    },
}

# ---------------------------------------------------------------------------
# Window switching
# ---------------------------------------------------------------------------
WIN_SETTLE_MS_MIN = 150        # ms to wait after each window switch (min)
WIN_SETTLE_MS_MAX = 200        # ms to wait after each window switch (max)

# ---------------------------------------------------------------------------
# Resolution profile
#
# QHD   -  assets in  assets/           (GUI Scale enabled, e.g. 2560x1440)
# FHD   -  assets in  assets/fullhd/    (no GUI Scale, 1920x1080)
# ASUS  -  assets in  assets/asus/      (1440x900, ASUS monitor)
# FHDS  -  assets in  assets/fullhd_small/  (FHD small UI variant)
# ---------------------------------------------------------------------------
RESOLUTION = "QHD"

# ─── SHARED SETTINGS — managed by updater.py, do not edit below this line ───
# Sections: Core → Assist → NextTarget → Hotkey Assist

_PROFILES = {
    "QHD": {
        "assets_dir": os.path.join(_DIR, "assets"),
        # Mob HP bar (offset from bag_mob_anchor CENTER to bar TOP-LEFT)
        # Measured from mob_frame_qhd.png (493x160): anchor (63,145), bar X84-470 Y102-119
        "mob_bar_offset_x":  21,
        "mob_bar_offset_y": -43,
        "mob_bar_w":        387,
        "mob_bar_h":         18,
        # Char HP bar (offset from char_bars_anchor CENTER to bar TOP-LEFT)
        # Measured from char_bars_full.png (446x68): anchor center (11,11)
        "char_hp_offset_x":  56,
        "char_hp_offset_y":  17,
        "char_hp_w":        364,
        "char_hp_h":         19,
        # Char MP bar
        "char_mp_offset_x":  56,
        "char_mp_offset_y":  36,
        "char_mp_w":        376,
        "char_mp_h":         19,
    },
    "FHD": {
        "assets_dir": os.path.join(_DIR, "assets", "fullhd"),
        # Mob HP bar - measured from FHD target frame (393x103): anchor (34,88), bar X57-377 Y50-64
        "mob_bar_offset_x":  23,
        "mob_bar_offset_y": -38,
        "mob_bar_w":        321,
        "mob_bar_h":         15,
        # Char bars - measured from char_bars_full.png (378x58), anchor 34x34 at (0,0), center (17,17)
        # HP bar X56-363 Y24-39 | MP bar X56-373 Y40-55
        "char_hp_offset_x":  39,
        "char_hp_offset_y":   7,
        "char_hp_w":        308,
        "char_hp_h":         16,
        "char_mp_offset_x":  39,
        "char_mp_offset_y":  23,
        "char_mp_w":        318,
        "char_mp_h":         16,
        # Death-screen respawn dialog (fixed position, 1920x1080)
        # Outer dark box — top-left (856,331), size 208x139
        "death_dialog_x":      856,
        "death_dialog_y":      331,
        "death_dialog_w":      208,
        "death_dialog_h":      139,
        # "To Village" button inside the box — excluded from pixel painting
        "death_dialog_skip_x": 908,
        "death_dialog_skip_y": 355,
        "death_dialog_skip_w": 104,
        "death_dialog_skip_h":  21,
        # Background color of the dark box; pixels matching this (±tol) → painted white
        "death_dialog_bg_hex": "#252625",
        "death_dialog_bg_tol":   2,
    },
    "ASUS": {
        "assets_dir": os.path.join(_DIR, "assets", "asus"),
        # Mob HP bar - measured from asus_mob_bars (345x86): anchor center (22,71),
        # red bar rows 36-50 cols 44-337
        "mob_bar_offset_x":  22,
        "mob_bar_offset_y": -35,
        "mob_bar_w":        294,
        "mob_bar_h":         15,
        # Char bars - measured from asus_char_bars (383x102): anchor center (8,10),
        # HP bar rows 67-81 cols 100-368 | MP bar rows 85-99 cols 100-382
        "char_hp_offset_x":  92,
        "char_hp_offset_y":  57,
        "char_hp_w":        269,
        "char_hp_h":         15,
        "char_mp_offset_x":  92,
        "char_mp_offset_y":  75,
        "char_mp_w":        283,
        "char_mp_h":         15,
    },
    "FHDS": {
        "assets_dir": os.path.join(_DIR, "assets", "fullhd_small"),
        # Mob HP bar - measured from mob_frame (347x74): anchor center (15,64),
        # red bar rows 28-42 cols 36-341
        "mob_bar_offset_x":  21,
        "mob_bar_offset_y": -36,
        "mob_bar_w":        306,
        "mob_bar_h":         15,
        # Char bars - measured from char_frame (387x105): anchor center (12,13),
        # HP bar rows 70-84 cols 104-371 | MP bar rows 88-101 cols 104-383
        "char_hp_offset_x":  92,
        "char_hp_offset_y":  57,
        "char_hp_w":        268,
        "char_hp_h":         15,
        "char_mp_offset_x":  92,
        "char_mp_offset_y":  75,
        "char_mp_w":        280,
        "char_mp_h":         14,
    },
}

PROFILE = _PROFILES[RESOLUTION]

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
WIN_SETTLE_MS_MIN = 150        # ms to wait after each window switch (min)
WIN_SETTLE_MS_MAX = 200        # ms to wait after each window switch (max)

TARGET_NOT_FOUND_TIMEOUT = 120   # seconds before "no new mobs" Telegram + stop

RECOVERY_WAIT_MIN = 3    # seconds to wait after post-timeout recovery
RECOVERY_WAIT_MAX = 6

# Anchor template matching
ANCHOR_CONFIDENCE         = 0.80  # matchTemplate threshold for all anchors
PARTY_ANCHOR_CONFIDENCE   = 0.75  # lower threshold for party_pl_anchor
DC_CONFIDENCE             = 0.80  # matchTemplate threshold for disconnect.png
ANCHOR_CACHE_PADDING      =   90  # px padding around cached anchor for fast re-verify
# bag_mob_anchor is only searched within the top ANCHOR_TOP_REGION_PX rows of
# the screen (full-screen search only; cached hits are accepted regardless).
ANCHOR_TOP_REGION_PX      =  200
# Tight ROI for bag_mob_anchor on Full HD — replaces the full 1920×200 strip.
# (x1, y1, x2, y2) absolute pixel coordinates; None = fall back to max_y search.
BAG_MOB_ANCHOR_ROI_FHD:             tuple = (692, 0, 1387, 150)
BAG_MOB_ANCHOR_ROI_FHD_NEXTTARGET: tuple = (1520, 0, 1920, 140)  # used only in 'n'/NextTarget mode
BAG_MOB_ANCHOR_ROI_ASUS: tuple = (581, 0, 1091, 130)  # (x1,y1,x2,y2) for 1366×768

# Mob HP monitoring
MOB_HP_LOW_PCT       =   5    # lower end of the "kill zone" HP range
MOB_HP_HIGH_PCT      =  30    # upper end
MOB_HP_HIGH_PCT_FROM_VIEWER = True  # False / True, Viewer-defined HP% overrides the value above
HP_CHECK_INTERVAL    = 0.0    # seconds between mob HP reads
HP_SWITCH_EVERY      =   5    # switch windows every N checks
LOW_HP_TIMEOUT_MIN   =  20    # random timeout waiting for mob HP to enter range (s)
LOW_HP_TIMEOUT_MAX   =  25
HP_STALL_S           =   8    # if HP has not dropped below HP_STALL_PCT within this
                               # many seconds, restart target search
HP_STALL_PCT         = 99.5   # HP% treated as "not yet damaged"
HP_STALL_JITTER_MIN  =   0    # random extra wait before stall fires (s) — staggers bots
HP_STALL_JITTER_MAX  =   3
HP_ANCHOR_MISS_LIMIT =   2    # consecutive misses without bag_mob_anchor → restart cycle

# Mob death monitoring
DEATH_CHECK_INTERVAL = 0.005  # seconds between mob_dead checks
DEATH_SWITCH_EVERY   =   5
DEATH_TIMEOUT        = 15.0   # seconds before "waited too long to die" handling

# Character HP / mana thresholds
CHAR_HP_CRITICAL_PCT   =  20  # below this → F7 (strong potion)
CHAR_HP_LOW_PCT        =  50  # below this (but ≥ CRITICAL) → F6
CHAR_MANA_HIGH_PCT     =  80  # above this AND mob HP > 70 % → F2
POTION_COOLDOWN_S      =  15  # min seconds between consecutive F6/F7 presses
BUFF_NOTIFY_COOLDOWN_S = 300  # min seconds between "full buff expired" Telegram msgs

# Key press timing defaults
KEY_HOLD_MIN_MS  =  75    # default random hold range for all keys (ms)
KEY_HOLD_MAX_MS  = 150
WASD_HOLD_MAX_MS = 200    # WASD keys during timeout recovery

# ---------------------------------------------------------------------------
# Assist
# ---------------------------------------------------------------------------

# --- Two-window assist targeting ---
ASSIST_RMB_COUNT_MIN          =   4    # right-clicks per targeting burst
ASSIST_RMB_COUNT_MAX          =   5
ASSIST_RMB_INTERVAL_MIN_MS    =  40    # gap between clicks within a burst (ms)
ASSIST_RMB_INTERVAL_MAX_MS    =  75
ASSIST_RMB_MAX_ATTEMPTS       =   2    # burst retries before switching windows
ASSIST_SEARCH_RETRY_MIN_MS    =   0    # random delay before first RMB burst (ms)
ASSIST_SEARCH_RETRY_MAX_MS    = 800
ASSIST_REQUIRE_PARTY_ANCHOR   = True   # stop if party_pl_anchor disappears
# When True: both windows follow separate party leaders (independent kill phase).
ASSIST_INDEPENDENT            = False

# --- Two-window assist attack ---
ASSIST_ATTACK_COUNT_MIN       =   3    # F1/F2 presses per visit
ASSIST_ATTACK_COUNT_MAX       =   4
ASSIST_ATTACK_HOLD_MIN_MS     =  60    # hold duration per press (ms)
ASSIST_ATTACK_HOLD_MAX_MS     = 120
ASSIST_ATTACK_INTERVAL_MIN_MS =  60    # gap between consecutive presses (ms)
ASSIST_ATTACK_INTERVAL_MAX_MS = 100

# --- Single-window assist — RMB acquisition ---
SA_RMB_ATTEMPTS   =   1    # RMB clicks per normal-mode attempt
SA_RMB_WAIT_MS    = 375    # ms to wait after each RMB before checking

# Stall handling for single-window assist
SA_STALL_WAIT_MIN =   3    # wait after LMB burst before restarting Phase 1 (s)
SA_STALL_WAIT_MAX =   6

# --- SA approach thresholds (shared by both "a" and "ac" modes) ---
# SA_APPROACH_SKIP_PX: before any ground clicks — if bag_mob_anchor is found
#   and pair_center is already within this distance, skip movement and attack.
# SA_APPROACH_STOP_PX: during approach polling — stop all ground clicks and
#   attack as soon as pair_center closes within this distance.
SA_APPROACH_SKIP_PX = 150
SA_APPROACH_STOP_PX = 350

# --- SA camera ---
SA_CAMERA_ROTATE_DX    =  750  # drag px for a blind 180° camera turn (fallback)
CAMERA_ORIENT_TOL_DEG  =    5  # stop correcting when |error| ≤ this (deg)
CAMERA_ORIENT_MAX_ITER =    8  # safety cap on correction iterations
CAMERA_ORIENT_SETTLE_S =  0.2  # wait after each drag before re-reading arrow (s)

# --- SA ground-click blue-glow verification ---
SA_GROUND_CHECK_DELAY_MS =  300  # ms to wait after click before capturing
SA_GROUND_CHECK_W        =  100  # capture region width  around click point (px)
SA_GROUND_CHECK_H        =  100  # capture region height around click point (px)
SA_GROUND_HUE_LO         =   90  # blue hue lower bound (OpenCV 0–180)
SA_GROUND_HUE_HI         =  135  # blue hue upper bound
SA_GROUND_SAT_LO         =   50  # min saturation — glow is relatively desaturated
SA_GROUND_SAT_HI         =  130  # max saturation — excludes in_target_blue (avg 139–206)
SA_GROUND_VAL_LO         =  140  # min brightness
SA_GROUND_MIN_PX         =    3  # min blue pixels to count as confirmed ground click
SA_MA_ANCHOR_DEBUG       = False  # save the detection frame whenever ma_anchor is found, to logs/ma_debug/

# --- SA fallback ground clicks (when SA_RMB_ATTEMPTS all fail) ---
SA_FALLBACK_DELAY_MIN      =  0.0  # pre-delay before fallback clicks (s)
SA_FALLBACK_DELAY_MAX      =  5.0
SA_FALLBACK_CLICK_AREA     =  50  # side (px) of the centred click area
SA_FALLBACK_EXCL_W         =   25  # exclusion zone width  directly below area centre
SA_FALLBACK_EXCL_H         =   40  # exclusion zone height directly below area centre
SA_FALLBACK_CLICK2_GAP_MAX =  500  # max ms between 1st and 2nd fallback click
SA_FALLBACK_CLICK_PROX_MIN =   20  # min px from click 1 for click 2
SA_FALLBACK_CLICK_PROX_MAX =   50  # max px from click 1 for click 2
SA_FALLBACK_SKIP_CHANCE    = 0.15  # probability of skipping ground clicks entirely

# --- SA "a" mode — MA-anchor approach (ma_anchor.png drives movement) ---
# ma1/ma2  → detected during buff/death checks; used only as the RMB point.
# ma_anchor → separately detected; centres the 150×150 ground-click area.
# pair_center (blue/red dots) → used only for SKIP_PX / STOP_PX distance checks.
SA_MA_CONFIDENCE      = 0.80  # matchTemplate threshold for ma1/ma2 and ma_anchor
SA_MA_CLICK_AREA      =  50  # side (px) of ground-click area centred on ma_anchor
SA_MA_LEAD_PX         =  800  # trigger next click when d_anchor ≤ d_remaining + LEAD
SA_MA_MIN_CLICK_PX    =  10  # minimum click distance from screen centre (px)
SA_MA_CLOSE_PX        =  100  # stop normal ground clicks when ma_anchor is within this dist
SA_MA_FALLBACK_CHANCE = 0  # chance to perform 1–2 close-zone fallback clicks

# Directional click-region offset — applied when ma_anchor is far from the
# screen centre (dist ≥ SA_MA_OFFSET_TRIGGER_PX).  Positions SA_MA_CLICK_AREA
# beyond ma_anchor instead of centring it on ma_anchor, compensating for the
# isometric perspective.  Set SA_MA_REGION_OFFSET_PX = 0 to disable the offset
# while keeping the trigger active.
SA_MA_DIRECTION_X_WEIGHT: float = 1.0   # horizontal weight on the centre→anchor
                                         # direction vector before normalisation.
                                         # 1.0 = raw direction; higher = more
                                         # sideways; no effect at 12/6 o'clock.
SA_MA_REGION_OFFSET_PX: int   =  50     # px from anchor centre to the NEAREST
                                         # EDGE of SA_MA_CLICK_AREA
SA_MA_OFFSET_TRIGGER_PX: int  = 200     # min dist (screen centre → anchor) for
                                         # the directional offset to activate;
                                         # closer → normal centred-on-anchor click

# Exclusion zones shared by ma_anchor AND in_target_blue/red detection.
# Applied only when the captured frame is exactly 1920×1080 (FHD).
# Each entry is (x1, y1, x2, y2) in inclusive screen-pixel coordinates.
# Detections whose centre falls inside any of these rectangles are suppressed.
# Ground-click points generated inside an excluded zone are pushed 2 px past
# the nearest boundary of that zone before the LMB is sent.
SA_EXCL_ROIS_FHD: list = [
    (   0,    0,  384,   56),  # top-left corner (character/party bars)
    ( 844,    0, 1249,  110),  # top-center band (target window)
    (1642,    0, 1919,  275),  # top-right strip (minimap + buffs)
    (   0,  200,  260,  686),  # left strip — narrow segment
    (   0,  686,  381, 1048),  # left strip — wide segment
    ( 752,  837, 1273, 1048),  # bottom-center block (inventory / skills)
    (1868,  922, 1919,  990),  # bottom-right small stub
    (1504,  992, 1919, 1048),  # bottom-right large block
    (   0, 1049, 1919, 1079),  # bottom strip (system bar)
]
SA_EXCL_ROIS_ASUS: list = [
    (   0,   0,  392,  98),    # top-left block
    ( 638,   0, 1045, 103),    # top-center block (target window)
    (1178,   0, 1439, 259),    # top-right block (minimap + buffs)
    (1040, 812, 1439, 830),    # bottom-right upper step
    ( 542, 831, 1439, 864),    # bottom-right / center middle step
    (   0, 865, 1439, 899),    # full bottom strip
]

# Exclusion regions used ONLY for target_check detection in 'n' NextTarget mode.
# Smaller than SA_EXCL_ROIS_FHD — excludes UI chrome but leaves most of the
# game world (including skill/inventory bars) available so target_check can be
# detected anywhere the UI is not.
TC_EXCL_ROIS_FHD: list = [
    (   0,    0,  384,   56),  # top-left corner (character/party bars)
    (1642,    0, 1919,  275),  # top-right strip (minimap + buffs)
    (1868,  922, 1919,  990),  # bottom-right small stub
    (1504,  992, 1919, 1048),  # bottom-right large block
]

# ms to wait after pressing F9 before grabbing a frame for target_check validation
TARGET_CHECK_F9_WAIT_MS = 375

# Legacy single-ROI kept for reference; superseded by SA_EXCL_ROIS_FHD above.
# SA_MA_ANCHOR_EXCL_ROI: tuple = (875, 0, 1225, 101)

# --- SA "ac" mode — crosshair corridor approach (in_target_blue/red drives movement) ---
SA_ATTACK_BEFORE_APPROACH   = True   # True  → attack immediately on confirmation, then approach
                                     # False → approach first; attack only once within STOP_PX
# Pre-attack delay (SA_ATTACK_BEFORE_APPROACH = True only).
# 90 % chance → [MIN, MAX] s;  10 % chance → [LONG_MIN, LONG_MAX] s.
SA_PRE_ATTACK_DELAY_MIN      =  0.0
SA_PRE_ATTACK_DELAY_MAX      =  2
SA_PRE_ATTACK_DELAY_LONG_MIN =  2.0
SA_PRE_ATTACK_DELAY_LONG_MAX =  4.0
SA_PRE_ATTACK_LONG_CHANCE    = 0.10

# Isometric perspective correction: shift corridor target down when mob is
# near 3/9 o'clock.  Scales with |ux| (0 at 12/6, 1 at 3/9 o'clock).
SA_APPROACH_DOWN_OFFSET_MAX    =  35   # direction-dependent down shift (px); tune to camera angle
SA_APPROACH_FIXED_DOWN_OFFSET  =  28   # fixed down shift from target_anchor bottom-centre (px)
SA_CORRIDOR_W                =  80   # perpendicular spread of the click corridor (px)
SA_CORRIDOR_MAX_RATIO        = 0.80  # clicks placed at most this fraction of path to mob
# Time-based next-click trigger.
# After a ground click at d_click px from screen centre the bot waits:
#   wait_ms = max(0, d_click - SA_APPROACH_LEAD_PX) * SA_APPROACH_MS_PER_PX
# before firing the next click.
# SA_APPROACH_LEAD_PX — pixels subtracted from d_click before scaling;
#   increasing it makes the next click fire sooner.
# SA_APPROACH_MS_PER_PX — ms per pixel of effective distance; tune to match
#   the character's actual screen-pixel movement speed.
SA_APPROACH_LEAD_PX          = 200   # px subtracted from d_click before timing
SA_APPROACH_MS_PER_PX        =   3   # ms per effective pixel (≈ 125 px/s)
SA_APPROACH_POLL_MS          =   0   # poll interval inside the wait; 0 = as fast as possible
# Global approach timeout: if SA_APPROACH_STOP_PX is not reached within this
# window, all ground clicks stop and the bot attacks immediately.
SA_APPROACH_TIMEOUT_MIN_MS   =  6000  # min total approach time before forced attack
SA_APPROACH_TIMEOUT_MAX_MS   = 10000  # max total approach time before forced attack
# Legacy ac-mode approach wait caps (used only by _single_assist_cycle_ac).
SA_APPROACH_MAX_WAIT_MIN_MS  = 1500
SA_APPROACH_MAX_WAIT_MAX_MS  = 2500
SA_FIRST_CLICK_MIN_PX        = 200   # min px from screen centre for the very first click
SA_NEXT_CLICK_MIN_PX         = 200   # min px from screen centre for subsequent clicks

# --- SA corridor approach — phaseCorrelate direction detection ---
# SA_DIR_INTERVAL_MS: gap between frame A and B fed to cv2.phaseCorrelate.
#   80 ms is the minimum that gives a detectable pixel shift at normal running
#   speed; reduce to 50 ms for faster detection at the cost of signal quality.
SA_DIR_INTERVAL_MS  =  80    # ms between the two frames
# SA_DIR_CORR_FRAC: corridor-origin correction expressed as a fraction of the
#   corridor half-width (SA_CORRIDOR_W / 2).
#   Cx =  CORR_FRAC · (W/2) · cos(θ)
#   Cy = −CORR_FRAC · (W/2) · sin²(θ)
#   Tuned with movement_dir2.py: -0.3 centred ground clicks for this camera.
SA_DIR_CORR_FRAC    = -0.3   # fraction of corridor half-width
# SA_DIR_CHECKS_MAX: random number of phaseCorrelate checks (0…N) run before
#   the very first corridor click to establish movement direction.
#   Each check costs ≈SA_DIR_INTERVAL_MS + ~50 ms capture/compute.
SA_DIR_CHECKS_MAX   =   4    # max checks (actual count drawn uniformly from 0..N)

# ---------------------------------------------------------------------------
# NextTarget
# ---------------------------------------------------------------------------

# NextTarget attack (F1 spam after target acquisition)
NEXTTARGET_ATTACK_COUNT_MIN       =   3   # F1 presses per target
NEXTTARGET_ATTACK_COUNT_MAX       =   3
NEXTTARGET_ATTACK_HOLD_MIN_MS     =  70   # hold duration for each F1 press (ms)
NEXTTARGET_ATTACK_HOLD_MAX_MS     = 120
NEXTTARGET_ATTACK_INTERVAL_MIN_MS =  50   # gap between consecutive F1 presses (ms)
NEXTTARGET_ATTACK_INTERVAL_MAX_MS = 100

# NC (name-click) targeting: Shift+click nearest unoccupied mob name template
NC_CENTER_OFFSET_X     =  0   # shift reference centre this many px left (0 = screen mid)
NC_CLICK_BELOW_PX      =  15   # click this many px below the detected name centre
NC_WAIT_AFTER_CLICK_MS = 375   # ms to wait after Shift+clicking before checking bag_mob_anchor
# If any valid mob_name* is within this square region (px × px) centred on the
# screen, the bot presses F5 first and checks mob_anchor after NC_F5_WAIT_MS.
# Only falls back to Shift+click if F5 did not produce a confirmed target.
NC_CENTER_F5_REGION_PX = 400   # side length of the centre F5 trigger zone (px)
NC_F5_WAIT_MS          = 375   # ms to wait after F5 before checking bag_mob_anchor
NC_CONFIDENCE              = 0.70  # matchTemplate threshold for mob_name templates
TARGET_ANCHOR_CONFIDENCE   = 0.90  # matchTemplate threshold for target_anchor detection
TC_CONFIDENCE          = 0.70  # matchTemplate threshold specifically for target_check
TC_DEBUG_SAVE          = True # save full frame on every failed target_check detection
NC_DOT_HALF_W          = 150   # half-width of the dot-check region around each name X
NC_DOT_HEIGHT          =  14   # height of the dot-check region
NC_NMS_DIST            =  50   # min px distance between two accepted name hits (dedup)
NC_WAIT_NO_MOB_MS      = 200   # ms to wait when no valid mob name is found before retry

# ---------------------------------------------------------------------------
# Hotkey Assist  (hotkey_assist.py — standalone script)
#
# Press a trigger key while a game window is in the foreground:
# the corresponding action runs on the *opposite* game window via Arduino.
# The triggering keypress is consumed and not forwarded to the game.
#
# Each entry: (trigger_key, action_type, target_key)
#   trigger_key — key you press  (f1..f12, esc, 1..9, a..z, …)
#   action_type — "press" : press target_key once on opposite window
#                 "rmb"   : RMB burst then press target_key × N on opposite
#                 "lmb"   : LMB burst then press target_key × N on opposite
#   target_key  — key sent to the opposite window (f1..f12, 1..9, a..z, …)
# ---------------------------------------------------------------------------
HOTKEY_MAPPINGS = [
    ("f1",  "rmb",   "1"),    # press F1  → RMB burst + "1" on opposite window
    ("f2",  "rmb",   "2"),    # press F2  → RMB burst + "2" on opposite window
    ("f3",  "press", "f4"),   # press F3  → send F4 to opposite window
    ("f5",  "lmb",   "1"),   # press F5  → LMB burst + "1" × N on opposite
    ("f6",  "press", "f5"),   # press F6  → send F5 to opposite window
]
HOTKEY_STOP_KEY = "f12"       # exit hotkey_assist cleanly

# Absolute screen (x, y) of the party-bar assist point in each window.
# Right-clicks (RMB burst) and left-clicks (LMB burst) land here.
# Match the key names used in the WINDOWS dict above ("win1", "win2", …).
HOTKEY_WIN1_ASSIST = (134, 290)   # ← set to actual party-bar coords
HOTKEY_WIN2_ASSIST = (134, 290)   # ← set to actual party-bar coords
