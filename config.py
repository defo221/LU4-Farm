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
        "title":       "Anorien",
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
# Target search
# ---------------------------------------------------------------------------
TARGET_NOT_FOUND_TIMEOUT = 120   # seconds before "no new mobs" Telegram + stop

# ---------------------------------------------------------------------------
# Mob HP monitoring
# ---------------------------------------------------------------------------
MOB_HP_LOW_PCT  = 5    # lower end of "kill zone" range
MOB_HP_HIGH_PCT = 30    # upper end of "kill zone" range

HP_CHECK_INTERVAL   = 0.0   # seconds between mob HP reads
HP_SWITCH_EVERY     = 5     # switch windows every N checks

LOW_HP_TIMEOUT_MIN  = 20   # random timeout waiting for mob HP to enter range
LOW_HP_TIMEOUT_MAX  = 25

HP_STALL_S   =  8     # if mob HP has not dropped below HP_STALL_PCT within this many
                      # seconds, restart target search (no recovery, no timeout count)
HP_STALL_PCT = 99.5   # HP% treated as "not yet damaged"
HP_STALL_JITTER_MIN = 0   # random extra wait (seconds) before the stall action fires,
HP_STALL_JITTER_MAX = 3   # so multiple characters don't all react at the exact same time
# Single-window assist stall: wait after the LMB burst before restarting Phase 1
SA_STALL_WAIT_MIN      = 3    # seconds
SA_STALL_WAIT_MAX      = 6
# Interval between F5 presses in the recovery F5 loop.
# During this gap the bot runs buff/death/party-anchor checks.
SA_F5_LOOP_INTERVAL_S  = 5.0  # seconds

HP_ANCHOR_MISS_LIMIT = 2   # consecutive checks without bag_mob_anchor → restart cycle

# ---------------------------------------------------------------------------
# Mob death monitoring
# ---------------------------------------------------------------------------
DEATH_CHECK_INTERVAL = 0.005   # seconds between mob_dead image checks
DEATH_SWITCH_EVERY   = 5
DEATH_TIMEOUT        = 15.0  # seconds before "waited too long to die" handling

# ---------------------------------------------------------------------------
# Anchor template matching
# ---------------------------------------------------------------------------
ANCHOR_CONFIDENCE         = 0.80  # matchTemplate threshold for all anchors
PARTY_ANCHOR_CONFIDENCE   = 0.75  # lower threshold for party_pl_anchor — template varies across machines
DC_CONFIDENCE             = 0.80  # matchTemplate threshold for disconnect.png
ANCHOR_CACHE_PADDING = 90    # px padding around cached anchor for fast re-verify
# bag_mob_anchor and char_bars_anchor are only searched within the top
# ANCHOR_TOP_REGION_PX rows of the screen (full-screen search only; cached
# hits are always accepted regardless of position).
ANCHOR_TOP_REGION_PX = 200

# ---------------------------------------------------------------------------
# Character HP / mana thresholds
# ---------------------------------------------------------------------------
CHAR_HP_CRITICAL_PCT =  20   # below this → press F7 (strong potion)
CHAR_HP_LOW_PCT      =  50   # below this (but >= CRITICAL) → press F6
CHAR_MANA_HIGH_PCT   =  80   # above this AND mob HP > 70% → press F2
POTION_COOLDOWN_S    =  15   # minimum seconds between consecutive F6 or F7 presses
BUFF_NOTIFY_COOLDOWN_S = 300   # minimum seconds between "full buff expired" Telegram messages

# ---------------------------------------------------------------------------
# Key press timing defaults
# ---------------------------------------------------------------------------
KEY_HOLD_MIN_MS  =  75    # default random hold range for all keys
KEY_HOLD_MAX_MS  = 150
WASD_HOLD_MAX_MS = 200    # WASD keys during timeout recovery can hold longer

# Nexttarget-mode attack: F1 spam after target acquisition
NEXTTARGET_ATTACK_COUNT_MIN      =   2    # F1 presses per target
NEXTTARGET_ATTACK_COUNT_MAX      =   2
NEXTTARGET_ATTACK_HOLD_MIN_MS    =  70    # hold duration for each F1 press (ms)
NEXTTARGET_ATTACK_HOLD_MAX_MS    = 120
NEXTTARGET_ATTACK_INTERVAL_MIN_MS=  50    # gap between consecutive F1 presses (ms)
NEXTTARGET_ATTACK_INTERVAL_MAX_MS= 100

# Assist-mode targeting: right-click burst (recorded avg 3-4 clicks)
ASSIST_RMB_COUNT_MIN             =   4    # right-clicks per targeting burst
ASSIST_RMB_COUNT_MAX             =   5
ASSIST_RMB_INTERVAL_MIN_MS       =  40    # gap between right-clicks within a burst (ms)
ASSIST_RMB_INTERVAL_MAX_MS       =  75
ASSIST_RMB_MAX_ATTEMPTS          =   2    # burst retries per visit before switching to the other window
ASSIST_SEARCH_RETRY_MIN_MS       = 0    # random delay before each first RMB burst (ms)
ASSIST_SEARCH_RETRY_MAX_MS       = 800
ASSIST_REQUIRE_PARTY_ANCHOR      = True # True → stop if party_pl_anchor disappears; False → skip check, keep cycling

# When True: both-assist windows follow *separate* party leaders.
# Each window alternates every HP_SWITCH_EVERY checks; F2 is pressed only on
# the window whose mob enters the kill zone; no synchronized kill phase.
ASSIST_INDEPENDENT               = False

# Assist-mode attack: F2 spam after right-click targeting
ASSIST_ATTACK_COUNT_MIN          =   2    # key presses per visit (F1 or F2 depending on setup)
ASSIST_ATTACK_COUNT_MAX          =   3
ASSIST_ATTACK_HOLD_MIN_MS        =  60    # hold duration per press (ms)
ASSIST_ATTACK_HOLD_MAX_MS        = 120
ASSIST_ATTACK_INTERVAL_MIN_MS    =  60    # gap between consecutive presses (ms)
ASSIST_ATTACK_INTERVAL_MAX_MS    = 100

# ---------------------------------------------------------------------------
# Single-window assist — phase-based target acquisition (SA_*)
# ---------------------------------------------------------------------------
# Phase 1: single RMB click at assist_point, wait, check bag_mob_anchor
SA_RMB_ATTEMPTS      =    2    # RMB clicks per normal-mode attempt
SA_RMB_WAIT_MS       =  450    # ms to wait after each RMB before checking

# Phase 2: press F5, wait, check bag_mob_anchor
SA_F5_ATTEMPTS       =    1    # F5 presses per normal-mode attempt
SA_F5_WAIT_MS        =  450    # ms to wait after each F5 before checking

# Phase 3: healer-area fallback when both phases 1 & 2 failed
SA_HEALER_CLICK_AREA      =  200    # side (px) of the click area centred on healer_farm_anchor
SA_HEALER_PRE_DELAY_MIN   =  0.1    # minimum random pause before starting healer clicks (s)
SA_HEALER_PRE_DELAY_MAX   =  3.0    # maximum random pause before starting healer clicks (s)
SA_HEALER_POST_PAUSE_MIN  =  1.0    # pause after last click before F5 loop starts (s)
SA_HEALER_POST_PAUSE_MAX  =  3.0
SA_HEALER_CLICK_PROX_MIN  =   50    # each click ≥ this many px from the previous one
SA_HEALER_CLICK_PROX_MAX  =  100    # each click ≤ this many px from the previous one
# Perspective-aware vertical bounds
SA_HEALER_UPPER_EXTEND    =   50    # max px ABOVE healer when it is in the upper screen half
SA_HEALER_LOWER_EXTEND    =  150    # max px BELOW healer when it is in the lower screen half
# Exclusion zone directly below the healer centre (applied on every click)
SA_HEALER_EXCL_W          =   30    # width  of the below-healer exclusion zone (px)
SA_HEALER_EXCL_H          =   60    # height of the below-healer exclusion zone (px)
SA_CAMERA_ROTATE_DX       =  550    # horizontal drag distance for a 180° camera rotation (blind fallback)
# Smart camera orientation (minimap arrow detection)
CAMERA_ORIENT_TOL_DEG    =    5    # stop correcting when |error| <= this value (deg)
CAMERA_ORIENT_MAX_ITER   =    8    # safety cap on correction iterations
CAMERA_ORIENT_SETTLE_S   =  0.3    # wait after each drag before re-reading arrow (s)
# No-healer fallback (used when healer_farm_anchor is not found even after rotation)
SA_FALLBACK_DELAY_MIN     =  0.1    # minimum random pause before fallback clicks (s)
SA_FALLBACK_DELAY_MAX     =  3.0    # maximum random pause before fallback clicks (s)
SA_FALLBACK_CLICK_AREA    =  200    # side (px) of the centered clickable square
SA_FALLBACK_EXCL_W        =   40    # width  of centre exclusion zone for 1st click
SA_FALLBACK_EXCL_H        =   80    # height of centre exclusion zone for 1st click
SA_FALLBACK_CLICK2_GAP_MAX=  500    # max ms between 1st and 2nd fallback click
SA_FALLBACK_CLICK_PROX_MIN=   50    # min px distance between the two fallback clicks
SA_FALLBACK_CLICK_PROX_MAX=  100    # max px distance between the two fallback clicks

# Phase 4: approach via double-clicks when in_target_blue is too far from center
SA_APPROACH_PX             =  100   # distance threshold (px) — closer → attack immediately
SA_APPROACH_MAX_DCLK       =    4   # max double-clicks before attacking anyway
# Downward corridor bias that compensates for the isometric perspective.
# When the mob is at 3/9 o'clock the blue dots sit above the mob's ground
# position; shifting the corridor target down brings clicks closer to the
# actual body.  Scales with abs(sin(angle_from_vertical)) = |ux|:
#   12/6 o'clock → 0 px offset
#   3/9  o'clock → SA_APPROACH_DOWN_OFFSET_MAX px offset
SA_APPROACH_DOWN_OFFSET_MAX =   40   # px — tune to your camera angle
SA_CORRIDOR_W        =    100    # perpendicular spread of the click corridor (px).
                               # 0 = click exactly along the line from screen center
                               # to mob — recommended for isometric games where
                               # vertical offset maps to 3D depth, not sideways movement.
# Delays between consecutive double-clicks (uniform random, ms)
SA_DCLK_DELAY_1_MAX  = 2000    # between 1st and 2nd double-click
SA_DCLK_DELAY_2_MAX  = 1000    # between 2nd and 3rd double-click
SA_DCLK_DELAY_3_MAX  =  500    # between 3rd and 4th double-click
# Polling interval inside approach delays: re-check distance + mob presence every N ms
SA_APPROACH_POLL_MS  =  50    # poll interval during approach delays (ms); exits early if mob reached or mob gone
# Character walk speed used to ensure each click is placed far enough ahead
# that the character cannot reach it before the next double-click fires.
# 1 px per 5 ms  =  0.2 px/ms
SA_CHAR_SPEED_PX_PER_MS = 0.15

# ---------------------------------------------------------------------------
# NC (name-click) targeting mode
# ---------------------------------------------------------------------------
# In "nc" mode the bot finds mob name templates on screen, filters out any
# that already have a targeted_red / targeted_blue dot, and Shift+clicks the
# nearest unoccupied name relative to the configured screen center.
NC_CENTER_OFFSET_X     =   30    # shift reference center this many px to the left (0 = screen mid)
NC_CLICK_BELOW_PX      =  30    # click this many px below the detected name center
NC_WAIT_AFTER_CLICK_MS = 250    # ms to wait after clicking before checking bag_mob_anchor
NC_CONFIDENCE          =  0.80  # matchTemplate threshold for mob_name and dot templates
NC_DOT_HALF_W          = 150    # half-width of the dot-check region around each name center X
NC_DOT_HEIGHT          =  14    # height of the dot-check region  (1px margin + 12px dot + 1px)
NC_NMS_DIST            =  50    # min px distance between two accepted name hits (dedup)
NC_WAIT_NO_MOB_MS      = 200    # ms to wait when no valid mob name is found before retry

# ---------------------------------------------------------------------------
# Post-timeout recovery
# ---------------------------------------------------------------------------
RECOVERY_WAIT_MIN = 3    # seconds to wait after timeout handler
RECOVERY_WAIT_MAX = 6

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
HOTKEY_WIN1_ASSIST = (158, 158)   # ← set to actual party-bar coords
HOTKEY_WIN2_ASSIST = (158, 158)   # ← set to actual party-bar coords
