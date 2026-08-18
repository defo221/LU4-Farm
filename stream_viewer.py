"""
stream_viewer.py - runs on the main PC.

Composes the 9 slave streams into a grid and turns mouse/keyboard activity over
a tile into high-level commands for that slave, which its Arduino replays as
real USB HID events.  This process injects nothing anywhere.

Why Tkinter and not cv2.imshow: cv2 windows report key *presses* only, so
click-and-hold and held movement keys are impossible.  Tk gives KeyPress and
KeyRelease separately, which is what MOUSE_DOWN/UP and KEY_DOWN/UP need.

Coordinate mapping is the whole reason this exists instead of VNC.  Every frame
carries the slave-absolute region it covers (rx, ry, rw, rh), so a click at
tile-local (cx, cy) maps back exactly:

    slave_x = rx + cx * rw / displayed_width

That holds at any tile scale, and stays correct even if the sender changes its
downscale factor while frames are in flight.

Controls
    hover                 activates a tile immediately; no click required to select
    left click            send a click to the active slave
    middle click          toggle max-FPS lock on that tile (blue border stays, never forwarded)
    right drag            camera pan (press, hold, move, release)
    wheel                 scroll
    Ctrl+Tab              cycle to the next tile
    Ctrl+Shift+Tab        cycle to the previous tile
    Alt+1…Alt+0           jump directly to tile 1–10 (0 = tenth)
    ` (backtick)          zoom the active tile to fill the window
    +/-                   raise/lower the active tile frame rate
    other keys            sent to the active tile, including Escape
"""

import argparse
import json
import os
import pathlib
import socket
import struct
import sys
import threading
import time
import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageTk

import stream_proto as proto
from stream_proto import Channel

# ── cursor art ────────────────────────────────────────────────────────────────

def _load_cur(path):
    """
    Parse a Windows .cur file and return (rgba_ndarray, hotspot_x, hotspot_y).
    Supports 8-bpp (paletted) and 32-bpp DIB frames.  Returns None if the file
    is missing or unreadable.
    """
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) < 22:
        return None
    reserved, img_type, count = struct.unpack_from("<HHH", raw, 0)
    if img_type != 2 or count < 1:
        return None
    w, h, cc, _res, hx, hy, sz, doff = struct.unpack_from("<BBBBHHIi", raw, 6)
    img = raw[doff: doff + sz]
    bi_size, bi_w, bi_h, _planes, bpp = struct.unpack_from("<IiiHH", img, 0)
    real_h = abs(bi_h) // 2       # DIBs in .cur stack XOR + AND masks vertically

    if bpp == 32:
        px = bi_size
        row_b = bi_w * 4
        rows = [img[px + y * row_b: px + (y + 1) * row_b] for y in range(real_h)]
        rows.reverse()
        arr = np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(real_h, bi_w, 4)
        rgba = arr[:, :, [2, 1, 0, 3]].copy()
    elif bpp == 8:
        n_colors = cc if cc > 0 else 256
        pal = [(img[bi_size + i*4 + 2], img[bi_size + i*4 + 1], img[bi_size + i*4])
               for i in range(n_colors)]
        row_b  = ((bi_w * 8  + 31) // 32) * 4
        arow_b = ((bi_w      + 31) // 32) * 4
        px_off = bi_size + n_colors * 4
        rgba = np.zeros((real_h, bi_w, 4), np.uint8)
        for y in range(real_h):
            sy = real_h - 1 - y     # flip bottom-up
            xrow = img[px_off + sy * row_b: px_off + sy * row_b + row_b]
            arow = img[px_off + real_h * row_b + sy * arow_b:
                       px_off + real_h * row_b + sy * arow_b + arow_b]
            for x in range(bi_w):
                r, g, b = pal[xrow[x]]
                and_bit = (arow[x // 8] >> (7 - x % 8)) & 1
                rgba[y, x] = (r, g, b, 0 if and_bit else 255)
    else:
        return None
    return rgba, hx, hy


# Path relative to this script so it works regardless of cwd.
_CUR_PATH = pathlib.Path(__file__).with_name("l2cursor.cur")
_CURSOR_DATA = _load_cur(_CUR_PATH)   # (rgba, hx, hy) or None

# ── tuning ────────────────────────────────────────────────────────────────────
IDLE_FPS = 5.0                 # tiles you are not touching
ACTIVE_FPS = 18.0              # the selected tile; +/- retunes this live
FPS_LIMITS = (1.0, 30.0)
UI_HZ = 30                     # redraw rate
PAN_SEND_HZ = 25               # cap on moverel messages while right-dragging
RECONNECT_DELAY = 3.0
PING_EVERY = 2.0               # keeps the slave's watchdog quiet
JPEG_QUALITY_IDLE = 55
JPEG_QUALITY_ACTIVE = 75

CONFIG_FILE = "stream_slaves.json"
DEFAULT_PORT = 8772           # must match stream_sender.DEFAULT_PORT

TILE_BORDER = 2
LABEL_H = 18

# Tk keysym -> firmware key name. Anything not listed falls back to the
# character the key produced, so plain letters and digits need no entry.
KEYSYM_MAP = {
    "Return": "enter", "KP_Enter": "enter", "Escape": "esc", "Tab": "tab",
    "BackSpace": "backspace", "Delete": "delete", "Insert": "insert",
    "Home": "home", "End": "end", "Prior": "pageup", "Next": "pagedown",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "space": "space", "Control_L": "ctrl", "Control_R": "rctrl",
    "Alt_L": "alt", "Alt_R": "ralt", "Shift_L": "shift", "Shift_R": "rshift",
    "Super_L": "win", "Super_R": "rwin",
    "comma": ",", "period": ".", "slash": "/", "backslash": "\\",
    "semicolon": ";", "apostrophe": "'", "bracketleft": "[",
    "bracketright": "]", "minus": "-", "equal": "=", "grave": "`",
}
KEYSYM_MAP.update({f"F{i}": f"f{i}" for i in range(1, 13)})


def keysym_to_name(event):
    """Translate a Tk key event into a firmware key name, or None.

    Letter and digit keys are resolved by physical scan code (Windows VK code
    stored in event.keycode), not by the character the current layout produces.
    This matches how games handle input: pressing the key labelled W always
    sends 'w' regardless of whether the active layout is English, Russian, etc.

    VK_A=65..VK_Z=90 are always the 26 QWERTY letter positions.
    VK_0=48..VK_9=57 are always the ten digit positions on the main row.

    Special keys (arrows, F1-F12, Tab, …) use the keysym table and are already
    layout-independent in Tk.  Punctuation falls back to the character produced
    by the layout, since OEM VK codes differ across keyboard hardware.
    """
    ks = event.keysym
    if ks in KEYSYM_MAP:
        return KEYSYM_MAP[ks]
    kc = event.keycode                # Windows VK code (layout-independent)
    if 65 <= kc <= 90:                # VK_A..VK_Z → 'a'..'z'
        return chr(kc + 32)
    if 48 <= kc <= 57:                # VK_0..VK_9 → '0'..'9'
        return chr(kc)
    # Punctuation fallback: use whatever the layout produces.
    if len(ks) == 1 and ks.isprintable():
        return ks.lower()
    ch = event.char
    if ch and len(ch) == 1 and ch.isprintable():
        return ch.lower()
    return None


# ── one slave connection ──────────────────────────────────────────────────────
class SlaveLink:
    """Owns the socket and the decode thread for a single slave."""

    def __init__(self, name, host, port):
        self.name = name
        self.host = host
        self.port = port

        self._chan = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

        self.status = "offline"        # offline | connecting | online
        self.note = ""                # last error / warning from the slave
        self.screen = None            # (sx, sy, sw, sh) reported at hello
        self.arduino = False

        # latest decoded frame, replaced wholesale so readers never tear
        self._frame = None            # (geom, rgb_ndarray)
        self._frame_seq = 0
        self.fps_seen = 0.0
        self._stamps = []
        self.build_ms = 0.0       # slave-reported cost of one frame
        self.frame_kb = 0.0
        self.dropped = 0          # frames/sec the sender skipped to stay paced
        self.unchanged = 0        # frames/sec skipped because the screen was still
        self.capture = ""         # slave's capture backend: dxcam | mss
        self.lang = ""            # slave's current keyboard layout: "EN", "RU", …

        # what the sender should produce; resent whenever the layout changes
        self.want_scale = 0.34
        self.want_fps = IDLE_FPS
        self.want_quality = JPEG_QUALITY_IDLE

    # ---- lifecycle ----
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._lock:
            if self._chan:
                self._chan.close()

    def _run(self):
        while not self._stop.is_set():
            self.status = "connecting"
            try:
                sock = socket.create_connection((self.host, self.port), timeout=4.0)
                sock.settimeout(None)
            except OSError as e:
                self.status = "offline"
                self.note = str(e)
                if self._stop.wait(RECONNECT_DELAY):
                    return
                continue
            chan = Channel(sock)
            with self._lock:
                self._chan = chan
            try:
                self._pump(chan)
            except Exception as e:
                self.note = str(e)
            finally:
                chan.close()
                with self._lock:
                    self._chan = None
                self.status = "offline"
                self._frame = None
            if self._stop.wait(RECONNECT_DELAY):
                return

    def _pump(self, chan):
        # Pinging on its own thread, because recv() below blocks: an idle tile
        # at 1 fps must still keep the slave's input watchdog fed.
        threading.Thread(target=self._pinger, args=(chan,), daemon=True).start()
        while not self._stop.is_set():
            msg = chan.recv()
            if msg is None:
                return
            mtype, payload = msg
            if mtype == proto.MSG_FRAME:
                self._on_frame(payload)
            elif mtype == proto.MSG_JSON:
                self._on_json(proto.decode_json(payload))

    def _pinger(self, chan):
        while not chan.closed and not self._stop.is_set():
            chan.send_json({"t": "ping"})
            if self._stop.wait(PING_EVERY):
                return

    def _on_json(self, obj):
        t = obj.get("t")
        if t == "hello":
            self.screen = (obj.get("sx", 0), obj.get("sy", 0),
                           obj.get("sw", 1920), obj.get("sh", 1080))
            self.arduino = bool(obj.get("arduino"))
            self.capture = str(obj.get("cap", ""))
            warns = obj.get("warn") or []
            self.note = warns[0] if warns else ("" if self.arduino else "no Arduino")
            self.status = "online"
            self.push_settings()
        elif t == "stat":
            self.build_ms = float(obj.get("ms", 0.0))
            self.frame_kb = float(obj.get("kb", 0.0))
            self.dropped = int(obj.get("drop", 0))
            self.unchanged = int(obj.get("same", 0))
            self.capture = str(obj.get("cap", self.capture))
            v = obj.get("lang", "")
            if v:
                self.lang = str(v)
        elif t == "error":
            self.note = obj.get("msg", "")

    def _on_frame(self, payload):
        geom, jpeg = proto.unpack_frame(payload)
        arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return
        # Decode and colour-convert here, on this slave's own thread, so the UI
        # thread only has to hand a ready buffer to Tk.
        rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
        self._frame = (geom, rgb)
        self._frame_seq += 1

        now = time.perf_counter()
        self._stamps.append(now)
        if len(self._stamps) > 12:
            self._stamps.pop(0)
        if len(self._stamps) > 1:
            span = self._stamps[-1] - self._stamps[0]
            self.fps_seen = (len(self._stamps) - 1) / span if span > 0 else 0.0

    # ---- outbound ----
    def send(self, obj):
        with self._lock:
            chan = self._chan
        if chan is None or chan.closed:
            return False
        return chan.send_json(obj)

    def push_settings(self):
        self.send({"t": "scale", "v": round(self.want_scale, 4)})
        self.send({"t": "fps", "v": self.want_fps})
        self.send({"t": "quality", "v": self.want_quality})

    def set_active(self, active, active_fps=ACTIVE_FPS):
        fps = active_fps if active else IDLE_FPS
        quality = JPEG_QUALITY_ACTIVE if active else JPEG_QUALITY_IDLE
        if fps == self.want_fps and quality == self.want_quality:
            return
        self.want_fps, self.want_quality = fps, quality
        self.send({"t": "fps", "v": fps})
        self.send({"t": "quality", "v": quality})

    def set_scale_for(self, disp_w):
        """Ask the sender to encode at roughly the size we display it at."""
        if not self.screen:
            return
        scale = max(0.05, min(disp_w / float(self.screen[2]), 1.0))
        if abs(scale - self.want_scale) < 0.02:
            return
        self.want_scale = scale
        self.send({"t": "scale", "v": round(scale, 4)})

    @property
    def frame(self):
        """Latest (geom, rgb). Replaced as a whole, so it never reads half-updated."""
        return self._frame

    @property
    def frame_seq(self):
        return self._frame_seq


# ── one grid cell ─────────────────────────────────────────────────────────────
class Tile:
    """Where a slave is drawn, and what geometry that drawing had."""

    def __init__(self, link):
        self.link = link
        self.rect = (0, 0, 0, 0)          # cell box on the canvas
        self.shown = None                 # (geom, ox, oy, dw, dh) of drawn image
        self.seq = -1
        self.photo = None
        self.img_id = None
        self.text_id = None
        self.border_id = None
        self.fps_locked = False           # MMB-locked to active FPS regardless of hover

    def canvas_to_slave(self, cx, cy):
        """Map a canvas point to slave-absolute pixels, or None if outside."""
        if not self.shown:
            return None
        (rx, ry, rw, rh), ox, oy, dw, dh = self.shown
        lx, ly = cx - ox, cy - oy
        if not (0 <= lx < dw and 0 <= ly < dh):
            return None
        return int(rx + lx * rw / dw), int(ry + ly * rh / dh)

    def canvas_delta_to_slave(self, dx, dy):
        """Scale a canvas-space drag delta into slave pixels."""
        if not self.shown:
            return 0, 0
        (_, _, rw, rh), _, _, dw, dh = self.shown
        return int(round(dx * rw / dw)), int(round(dy * rh / dh))


# ── the viewer ────────────────────────────────────────────────────────────────
class Viewer:
    def __init__(self, root, links, cols, rows):
        self.root = root
        self.cols, self.rows = cols, rows
        self.tiles = [Tile(l) for l in links]

        self.selected = None          # index into self.tiles
        self.zoomed = False
        self.held_keys = set()
        self.active_fps = ACTIVE_FPS

        self._pan = None              # active right-drag state
        self._layout_key = None
        self._alt_held = False        # Alt is down on the viewer keyboard
        self._alt_combo = False       # this Alt hold was used as Alt+digit

        root.title("PXM fleet viewer")
        root.configure(bg="#101010")
        root.geometry("1600x950")

        self.canvas = tk.Canvas(root, bg="#101010", highlightthickness=0,
                                cursor="none")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.status = tk.Label(root, anchor="w", bg="#1c1c1c", fg="#d0d0d0",
                               font=("Consolas", 9), padx=6)
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

        # Fixed-size L2 cursor that follows the viewer's own mouse.
        # Converted once at startup; never rescaled so size is constant
        # regardless of tile zoom or grid layout.
        self._cur_id = None
        self._cur_hx = 0
        self._cur_hy = 0
        if _CURSOR_DATA is not None:
            rgba, hx, hy = _CURSOR_DATA
            self._cur_photo = ImageTk.PhotoImage(Image.fromarray(rgba))
            self._cur_hx, self._cur_hy = hx, hy
            self._cur_id = self.canvas.create_image(
                0, 0, anchor="nw", image=self._cur_photo, state="hidden")
        else:
            self._cur_photo = None

        self._bind()
        for l in links:
            l.start()
        self._tick()

    # ---- bindings ----
    def _bind(self):
        c = self.canvas
        c.bind("<Button-1>", self._on_left_down)
        c.bind("<Button-2>", self._on_middle_down)
        c.bind("<Button-3>", self._on_right_down)
        c.bind("<B3-Motion>", self._on_right_motion)
        c.bind("<ButtonRelease-3>", self._on_right_up)
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Motion>", self._on_mouse_move)
        c.bind("<Leave>", self._on_mouse_leave)

        self.root.bind("<KeyPress>", self._on_key_down)
        self.root.bind("<KeyRelease>", self._on_key_up)
        # Dedicated Alt_L/Alt_R bindings are MORE SPECIFIC than <KeyPress>, so
        # Tkinter dispatches them first and returns "break" before Windows' own
        # system-menu hook can consume the key.  This is the primary fix for
        # Alt+digit combos missing on the first attempt.
        self.root.bind("<Alt_L>", self._on_alt_down)
        self.root.bind("<Alt_R>", self._on_alt_down)
        # Explicit Alt+digit bindings so they are captured even when Tkinter's
        # window-menu system would otherwise swallow the Alt prefix on Windows.
        for d in "0123456789":
            self.root.bind(f"<Alt-Key-{d}>",
                           lambda e, k=d: self._on_alt_digit(k))
        # Re-claim keyboard focus whenever the mouse enters the canvas so that
        # hotkeys work immediately after hovering in from another application.
        c.bind("<Enter>", lambda e: self.canvas.focus_set())
        self.canvas.focus_set()

    # ---- layout ----
    def _layout(self):
        w = max(self.canvas.winfo_width(), 1)
        h = max(self.canvas.winfo_height(), 1)
        key = (w, h, self.zoomed, self.selected)
        if key == self._layout_key:
            return
        self._layout_key = key

        if self.zoomed and self.selected is not None:
            for i, t in enumerate(self.tiles):
                t.rect = (0, 0, w, h) if i == self.selected else (0, 0, 0, 0)
                if i != self.selected:
                    t.shown = None
        else:
            cw, ch = w // self.cols, h // self.rows
            for i, t in enumerate(self.tiles):
                col, row = i % self.cols, i // self.cols
                t.rect = (col * cw, row * ch, cw, ch)

        # Tell each sender to encode near the size we will actually draw it.
        for t in self.tiles:
            if t.rect[2] > 0:
                t.link.set_scale_for(t.rect[2] - 2 * TILE_BORDER)

    # ---- draw ----
    def _tick(self):
        try:
            self._layout()
            self._draw()
            self._update_status()
            # Keep the L2 cursor on top of all tile content after every redraw.
            if self._cur_id is not None:
                self.canvas.tag_raise(self._cur_id)
        except Exception as e:
            print(f"[UI] {e}", file=sys.stderr)
        self.root.after(int(1000 / UI_HZ), self._tick)

    def _draw(self):
        for i, t in enumerate(self.tiles):
            bx, by, bw, bh = t.rect
            if bw <= 0 or bh <= 0:
                for item in (t.img_id, t.text_id, t.border_id):
                    if item:
                        self.canvas.itemconfigure(item, state="hidden")
                # Nothing is drawn, so nothing may be clicked into.
                t.shown = None
                continue

            selected = (i == self.selected)
            colour = "#3fa7ff" if (selected or t.fps_locked) else "#303030"
            if t.border_id is None:
                t.border_id = self.canvas.create_rectangle(0, 0, 0, 0, width=TILE_BORDER)
            self.canvas.coords(t.border_id, bx + 1, by + 1, bx + bw - 1, by + bh - 1)
            self.canvas.itemconfigure(t.border_id, outline=colour, state="normal")

            self._draw_frame(t, bx, by, bw, bh)
            self._draw_label(t, bx, by, bw, bh, selected)

    def _draw_frame(self, t, bx, by, bw, bh):
        got = t.link.frame
        inner_w = bw - 2 * TILE_BORDER
        inner_h = bh - 2 * TILE_BORDER - LABEL_H
        if got is None or inner_w <= 0 or inner_h <= 0:
            if t.img_id:
                self.canvas.itemconfigure(t.img_id, state="hidden")
            t.shown = None
            return

        geom, rgb = got
        sh, sw = rgb.shape[:2]
        # Fit inside the cell without distorting, so mapping stays uniform.
        k = min(inner_w / sw, inner_h / sh)
        dw, dh = max(int(sw * k), 1), max(int(sh * k), 1)
        ox = bx + TILE_BORDER + (inner_w - dw) // 2
        oy = by + TILE_BORDER + LABEL_H + (inner_h - dh) // 2

        unchanged = (t.seq == t.link.frame_seq and t.shown
                     and t.shown[1:] == (ox, oy, dw, dh))
        if unchanged:
            return
        t.seq = t.link.frame_seq

        if (dw, dh) != (sw, sh):
            interp = cv2.INTER_AREA if dw < sw else cv2.INTER_LINEAR
            rgb = cv2.resize(rgb, (dw, dh), interpolation=interp)
        img = Image.fromarray(rgb)

        # Reusing the PhotoImage buffer avoids reallocating 9 of them per redraw.
        if t.photo is not None and (t.photo.width(), t.photo.height()) == (dw, dh):
            t.photo.paste(img)
        else:
            t.photo = ImageTk.PhotoImage(img)
            if t.img_id is None:
                t.img_id = self.canvas.create_image(0, 0, anchor="nw", image=t.photo)
            else:
                self.canvas.itemconfigure(t.img_id, image=t.photo)
        self.canvas.coords(t.img_id, ox, oy)
        self.canvas.itemconfigure(t.img_id, state="normal")
        self.canvas.tag_raise(t.border_id, t.img_id)

        t.shown = (geom, ox, oy, dw, dh)

    def _draw_label(self, t, bx, by, bw, bh, selected):
        link = t.link
        bits = [link.name, link.status]
        if link.status == "online":
            bits.append(f"{link.fps_seen:4.1f}fps")
            if link.build_ms > 0:
                # ms per frame x frames per second / 10 = percent of one core.
                # This is the number that says whether the rate can go higher.
                core = link.build_ms * link.fps_seen / 10.0
                bits.append(f"{link.build_ms:.0f}ms {core:.0f}%core")
            if link.dropped:
                bits.append(f"-{link.dropped}/s")
            if link.lang:
                bits.append(f"[{link.lang}]")
            if link.capture == "mss":
                bits.append("SLOW CAPTURE (pip install dxcam)")
            if not link.arduino:
                bits.append("NO ARDUINO")
        if t.fps_locked:
            bits.append("[MAX]")
        if link.note:
            bits.append(link.note[:60])
        text = "  ".join(bits)
        fg = {"online": "#8fdc8f", "connecting": "#d8c46a"}.get(link.status, "#c56b6b")
        if selected or t.fps_locked:
            fg = "#8fd0ff"
        if t.text_id is None:
            t.text_id = self.canvas.create_text(0, 0, anchor="nw",
                                                font=("Consolas", 9))
        self.canvas.coords(t.text_id, bx + TILE_BORDER + 3, by + TILE_BORDER + 2)
        self.canvas.itemconfigure(t.text_id, text=text, fill=fg, state="normal")

    # ---- viewer cursor (L2 art, follows viewer mouse) ----
    def _on_mouse_move(self, event):
        # Hover-to-select: entering a tile activates it immediately.
        idx = self._tile_at(event.x, event.y)
        if idx is not None and idx != self.selected:
            self._select(idx)

        if self._cur_id is None:
            return
        self.canvas.coords(self._cur_id,
                           event.x - self._cur_hx,
                           event.y - self._cur_hy)
        self.canvas.itemconfigure(self._cur_id, state="normal")
        self.canvas.tag_raise(self._cur_id)

    def _on_mouse_leave(self, event):
        if self._cur_id is not None:
            self.canvas.itemconfigure(self._cur_id, state="hidden")

    def _update_status(self):
        sel = "none" if self.selected is None else self.tiles[self.selected].link.name
        online = sum(1 for t in self.tiles if t.link.status == "online")
        self.status.configure(
            text=f" active: {sel}   online: {online}/{len(self.tiles)}"
                 f"   asking {self.active_fps:.0f}fps"
                 f"   |  hover=select  click=action  right-drag=pan  wheel=scroll"
                 f"   Ctrl+Tab=next  Ctrl+Shift+Tab=prev  Alt+1-0=pick"
                 f"   Z=zoom  +/-=fps")

    # ---- hit testing ----
    def _tile_at(self, cx, cy):
        for i, t in enumerate(self.tiles):
            bx, by, bw, bh = t.rect
            if bw > 0 and bx <= cx < bx + bw and by <= cy < by + bh:
                return i
        return None

    def _select(self, idx):
        if idx == self.selected:
            return
        if self.selected is not None:
            old_tile = self.tiles[self.selected]
            self._release_keys(old_tile.link)
            # Keep locked tiles at active FPS even when deselected.
            if not old_tile.fps_locked:
                old_tile.link.set_active(False)
        self.selected = idx
        if idx is not None:
            self.tiles[idx].link.set_active(True, self.active_fps)
        self._layout_key = None       # border colour + zoom target changed

    # ---- mouse ----
    def _on_left_down(self, event):
        idx = self._tile_at(event.x, event.y)
        if idx is None:
            return
        # First click on a tile only focuses it. Prevents a stray click in the
        # grid from landing in a live game window.
        if idx != self.selected:
            self._select(idx)
            return
        t = self.tiles[idx]
        pos = t.canvas_to_slave(event.x, event.y)
        if pos is None:
            return
        t.link.send({"t": "click", "x": pos[0], "y": pos[1], "btn": "left"})

    def _on_middle_down(self, event):
        """Toggle per-tile max-FPS lock.  Never forwarded to the slave."""
        idx = self._tile_at(event.x, event.y)
        if idx is None:
            return "break"
        t = self.tiles[idx]
        t.fps_locked = not t.fps_locked
        if t.fps_locked:
            # Immediately run at active FPS regardless of whether it is selected.
            t.link.set_active(True, self.active_fps)
        elif idx != self.selected:
            # Unlocked and not the hovered tile → drop back to idle.
            t.link.set_active(False)
        # If unlocked but still selected, hover-based logic keeps it active.
        self._layout_key = None        # force border colour refresh
        return "break"

    def _on_right_down(self, event):
        idx = self._tile_at(event.x, event.y)
        if idx is None or idx != self.selected:
            if idx is not None:
                self._select(idx)
            return
        t = self.tiles[idx]
        pos = t.canvas_to_slave(event.x, event.y)
        if pos is None:
            return
        t.link.send({"t": "mdown", "x": pos[0], "y": pos[1], "btn": "right"})
        self._pan = {"tile": idx, "last": (event.x, event.y),
                     "pending": (0.0, 0.0), "sent_at": time.perf_counter(),
                     "cursor_start": (event.x, event.y)}
        # Hide the L2 cursor for the duration of the drag, like the game does.
        if self._cur_id is not None:
            self.canvas.itemconfigure(self._cur_id, state="hidden")

    def _on_right_motion(self, event):
        if self._pan is None:
            return
        t = self.tiles[self._pan["tile"]]
        lx, ly = self._pan["last"]
        self._pan["last"] = (event.x, event.y)
        # Slow the drag by 30% in grid view so small tiles don't over-scroll.
        speed = 1.0 if self.zoomed else 0.75
        px, py = self._pan["pending"]
        self._pan["pending"] = (px + (event.x - lx) * speed,
                                py + (event.y - ly) * speed)

        now = time.perf_counter()
        if now - self._pan["sent_at"] < 1.0 / PAN_SEND_HZ:
            return
        self._flush_pan(t)

    def _flush_pan(self, t):
        px, py = self._pan["pending"]
        dx, dy = t.canvas_delta_to_slave(px, py)
        if dx == 0 and dy == 0:
            return
        # Keep the sub-pixel remainder so slow drags still accumulate.
        (_, _, rw, _), _, _, dw, _ = t.shown
        k = rw / dw if dw else 1.0
        self._pan["pending"] = (px - dx / k, py - dy / k)
        self._pan["sent_at"] = time.perf_counter()
        t.link.send({"t": "moverel", "dx": dx, "dy": dy})

    def _on_right_up(self, event):
        if self._pan is None:
            return
        t = self.tiles[self._pan["tile"]]
        self._pan["sent_at"] = 0.0
        self._flush_pan(t)
        t.link.send({"t": "mup", "btn": "right"})
        # Restore the L2 cursor at the position where the drag began.
        if self._cur_id is not None:
            sx, sy = self._pan["cursor_start"]
            # Warp the invisible OS pointer back so the next real <Motion>
            # event originates from the start point — no visual jump.
            self.canvas.event_generate("<Motion>", warp=1, x=sx, y=sy)
            self.canvas.coords(self._cur_id,
                               sx - self._cur_hx,
                               sy - self._cur_hy)
            self.canvas.itemconfigure(self._cur_id, state="normal")
            self.canvas.tag_raise(self._cur_id)
        self._pan = None

    def _on_wheel(self, event):
        idx = self._tile_at(event.x, event.y)
        if idx is None or idx != self.selected:
            return
        t = self.tiles[idx]
        pos = t.canvas_to_slave(event.x, event.y)
        if pos is None:
            return
        steps = int(event.delta / 120) or (1 if event.delta > 0 else -1)
        t.link.send({"t": "scroll", "steps": steps, "x": pos[0], "y": pos[1]})

    # ---- keyboard ----
    def _alt_down(self, event=None):
        """True if Alt is held, even when Tk omits the modifier bit on the digit."""
        if self._alt_held:
            return True
        if event is not None and event.state & 0x20008:
            return True
        return False

    def _pick_tile_by_digit(self, digit):
        """Alt+1…Alt+0: switch tile. Never send the combo to the slave.

        Called even when that tile is already selected, so a repeat Alt+2 stays
        fully swallowed instead of leaking '2' (or a leftover Alt) into the game.
        """
        self._alt_combo = True
        # If Alt already went out (a previous non-digit key flushed it), lift
        # it so the slave cannot see Alt+digit together.
        self._unsend_keys(("alt", "ralt"))
        n = (int(digit) - 1) % 10
        if n < len(self.tiles):
            self._select(n)

    def _unsend_keys(self, names):
        link = self._selected_link()
        for name in names:
            if name not in self.held_keys:
                continue
            self.held_keys.discard(name)
            if link is not None:
                link.send({"t": "kup", "key": name})

    def _flush_alt_to_slave(self):
        """Send the deferred Alt-down so in-game Alt+other-key still works."""
        if "alt" in self.held_keys:
            return
        link = self._selected_link()
        if link is None:
            return
        self.held_keys.add("alt")
        link.send({"t": "kdown", "key": "alt"})

    def _on_alt_down(self, event):
        """Dedicated Alt_L / Alt_R handler.

        Binding <Alt_L> directly is more specific than <KeyPress>, so Tkinter
        dispatches it first and the "break" return prevents Windows' system-menu
        hook from consuming the key — which was the root cause of Alt+digit
        combos missing on fast presses.
        """
        self._alt_held = True
        if bool(event.state & 0x1):          # Shift already held → layout switch
            self._flush_alt_to_slave()
        return "break"

    def _on_key_down(self, event):
        ctrl  = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        ks    = event.keysym

        # ── Always-intercepted viewer controls ───────────────────────────────
        # Escape is not one of them: it goes to the game like any other key.
        # Alt_L / Alt_R are handled by the dedicated _on_alt_down binding above
        # (more specific → fires before this handler → "break" already returned).
        # If for any reason that binding is missed, handle it here as a fallback.
        if ks in ("Alt_L", "Alt_R"):
            return self._on_alt_down(event)

        # Ctrl+Tab / Ctrl+Shift+Tab cycle through tiles.
        # Not gated on Alt state: if the user held Alt before pressing Ctrl+Tab,
        # the old guard failed, _flush_alt_to_slave() ran, and the slave received
        # Alt+Tab.  Clear any pending Alt here so it is never flushed afterwards.
        if ctrl and ks in ("Tab", "ISO_Left_Tab"):
            if self._alt_held and not self._alt_combo:
                self._alt_held = False   # discard pending Alt silently
            self._cycle(-1 if (shift or ks == "ISO_Left_Tab") else 1)
            return "break"

        # Alt+1…Alt+0: always consume, even if this tile is already active.
        if self._alt_down(event) and len(ks) == 1 and ks.isdigit():
            self._pick_tile_by_digit(ks)
            return "break"

        # ` (backtick/grave, VK 192) toggles zoom.  Match both the keysym AND
        # the physical key code so the binding works in any keyboard layout.
        # Trade-off: ё (same physical key in Russian layout) cannot be typed
        # through the viewer — it will toggle zoom instead.
        # Z / Я and every other letter key pass straight through.
        if ks == "grave" or event.keycode == 192:
            self.zoomed = not self.zoomed
            self._layout_key = None
            return "break"

        # +/- retune the active-tile frame rate.
        if ks in ("plus", "equal", "KP_Add"):
            self._bump_fps(+1.0)
            return "break"
        if ks in ("minus", "KP_Subtract"):
            self._bump_fps(-1.0)
            return "break"

        # Alt + anything else: now it is not a tile switch, so send Alt down.
        if self._alt_held and not self._alt_combo:
            self._flush_alt_to_slave()

        # ── Forward everything else to the active slave ───────────────────────
        link = self._selected_link()
        if link is None:
            return "break"
        name = keysym_to_name(event)
        if name is None or name in self.held_keys:
            return "break"        # Tk repeats KeyPress while held; send once
        if name in ("alt", "ralt"):
            return "break"
        self.held_keys.add(name)
        link.send({"t": "kdown", "key": name})
        return "break"

    def _on_key_up(self, event):
        ks = event.keysym

        if ks in ("Alt_L", "Alt_R"):
            used = self._alt_combo
            self._alt_held = False
            self._alt_combo = False
            if used:
                # Tile switch: swallow Alt-up as well. Lift a flushed Alt if
                # one somehow went out.
                self._unsend_keys(("alt", "ralt"))
                return "break"

        # Digit that belonged to Alt+N must not produce a leftover key-up.
        if (self._alt_held or self._alt_combo) and len(ks) == 1 and ks.isdigit():
            return "break"

        link = self._selected_link()
        name = keysym_to_name(event)
        if link is None or name is None or name not in self.held_keys:
            return "break"
        self.held_keys.discard(name)
        link.send({"t": "kup", "key": name})
        return "break"

    def _on_alt_digit(self, digit):
        """Select tile by number via Alt+1…Alt+0 (0 = tenth tile)."""
        self._alt_held = True
        self._pick_tile_by_digit(digit)
        return "break"

    def _bump_fps(self, delta):
        lo, hi = FPS_LIMITS
        self.active_fps = min(hi, max(lo, self.active_fps + delta))
        sel_link = self._selected_link()
        if sel_link is not None:
            sel_link.set_active(True, self.active_fps)
        # Keep fps_locked tiles in sync with the new active rate.
        for t in self.tiles:
            if t.fps_locked and t.link is not sel_link:
                t.link.set_active(True, self.active_fps)

    def _selected_link(self):
        if self.selected is None:
            return None
        return self.tiles[self.selected].link

    def _release_keys(self, link):
        if link is None:
            self.held_keys.clear()
            return
        for name in list(self.held_keys):
            link.send({"t": "kup", "key": name})
        self.held_keys.clear()

    def _panic(self):
        """Let go of everything on every slave. Used on viewer shutdown."""
        self.held_keys.clear()
        self._pan = None
        for t in self.tiles:
            t.link.send({"t": "release_all"})

    def _cycle(self, step):
        n = len(self.tiles)
        if n == 0:
            return
        start = 0 if self.selected is None else (self.selected + step) % n
        self._select(start)

    def shutdown(self):
        self._panic()
        time.sleep(0.15)
        for t in self.tiles:
            t.link.stop()


# ── config ────────────────────────────────────────────────────────────────────
def load_slaves(path, cli_spec):
    if cli_spec:
        out = []
        for i, item in enumerate(cli_spec.split(","), 1):
            item = item.strip()
            if not item:
                continue
            host, _, port = item.partition(":")
            out.append({"name": f"PC{i}", "host": host,
                        "port": int(port) if port else DEFAULT_PORT})
        return out, 3, 3

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        grid = cfg.get("grid", [3, 3])
        return cfg.get("slaves", []), int(grid[0]), int(grid[1])

    print(f"No {path} and no --slaves given.", file=sys.stderr)
    return [], 3, 3


def main():
    ap = argparse.ArgumentParser(description="PXM fleet viewer")
    ap.add_argument("--config", default=CONFIG_FILE)
    ap.add_argument("--slaves", default="",
                    help="comma list host:port, overrides the config file")
    args = ap.parse_args()

    slaves, cols, rows = load_slaves(args.config, args.slaves)
    if not slaves:
        return 1

    links = [SlaveLink(s.get("name", f"PC{i}"), s["host"],
                       int(s.get("port", DEFAULT_PORT)))
             for i, s in enumerate(slaves, 1)]

    root = tk.Tk()
    viewer = Viewer(root, links, cols, rows)

    def on_close():
        viewer.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        on_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
