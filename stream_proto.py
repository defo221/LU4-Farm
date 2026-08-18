"""
stream_proto.py - wire protocol shared by stream_sender.py (slave) and
stream_viewer.py (main PC).

One TCP connection per slave carries both directions:
    slave  -> viewer : video frames
    viewer -> slave  : input commands

Wire format, every message:

    [1 byte type][4 bytes payload length, big-endian uint32][payload]

MSG_FRAME payload:

    [int32 rx][int32 ry][int32 rw][int32 rh][JPEG bytes]

rx/ry/rw/rh is the screen region on the slave that this JPEG covers, in slave
absolute pixels.  Carrying it inside the frame instead of in a separate "here is
my current geometry" message means a click is always mapped with the geometry of
the exact frame the user was looking at, even if the region or scale changed
while frames were in flight.

MSG_JSON payload:
    UTF-8 JSON object with a "t" (type) field.  See COMMANDS below.
"""

import json
import socket
import struct
import threading
import time

MSG_FRAME = 0x01
MSG_JSON = 0x02

_HDR = struct.Struct(">BI")        # type, payload length
_GEO = struct.Struct(">iiii")      # rx, ry, rw, rh

MAX_PAYLOAD = 32 * 1024 * 1024     # sanity ceiling, refuse anything larger

# Bytes per write when pacing a frame. Small enough that one chunk is a
# fraction of a millisecond on the wire, large enough that a few hundred KB
# still needs only ~20 writes.
PACE_CHUNK = 16 * 1024

# Commands the viewer may send (all MSG_JSON):
#
#   {"t":"click",  "x":int, "y":int, "btn":"left"|"right"|"middle"}
#   {"t":"mdown",  "x":int, "y":int, "btn":...}   move there, then press and hold
#   {"t":"mup",    "btn":...}                     release
#   {"t":"move",   "x":int, "y":int}              move only
#   {"t":"moverel","dx":int, "dy":int}            relative nudge (live drag)
#   {"t":"drag",   "x":int, "y":int, "dx":int, "dy":int, "btn":...}
#   {"t":"scroll", "steps":int, "x":int, "y":int} x/y optional, moves first
#   {"t":"kdown",  "key":str}
#   {"t":"kup",    "key":str}
#   {"t":"key",    "key":str, "hold_ms":int}      tap
#   {"t":"combo",  "keys":[str,...], "hold_ms":int}
#   {"t":"release_all"}
#   {"t":"fps",    "v":float}                     capture rate
#   {"t":"scale",  "v":float}                     sender-side downscale, 0<v<=1
#   {"t":"quality","v":int}                       JPEG quality 1..100
#   {"t":"region", "x":int,"y":int,"w":int,"h":int}   w<=0 means full screen
#   {"t":"ping"}
#
# Messages the slave may send:
#
#   {"t":"hello","name":str,"sw":int,"sh":int,"arduino":bool,"warn":[str,...]}
#   {"t":"pong"}
#   {"t":"error","msg":str}
#   {"t":"cur","cx":int,"cy":int}
#       slave-absolute cursor position, sent once per frame so the overlay
#       tracks at full frame rate and stays in sync with the captured pixels
#   {"t":"stat","ms":float,"kb":float,"drop":int}
#       ms/kb   cost and size of one frame, for the viewer's load readout
#       drop    frames skipped in the last second because the paced sender was
#               still busy; a steady non-zero value means the requested fps
#               cannot be delivered within its pacing budget


class ProtoError(Exception):
    pass


class Channel:
    """Message framing over one TCP socket.

    Sending is mutex-protected so a frame writer thread and a command writer
    thread can share the socket.  Receiving is expected to happen on a single
    thread and is not locked.
    """

    def __init__(self, sock):
        self.sock = sock
        self._send_lock = threading.Lock()
        self._buf = bytearray()
        self._closed = False
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    # ---- sending -------------------------------------------------------------
    def _write_packet(self, pkt):
        try:
            with self._send_lock:
                self.sock.sendall(pkt)
            return True
        except OSError:
            self._closed = True
            return False

    def _send_raw(self, mtype, payload):
        if self._closed:
            return False
        return self._write_packet(_HDR.pack(mtype, len(payload)) + payload)

    def send_frame(self, rx, ry, rw, rh, jpeg):
        return self._send_raw(MSG_FRAME, _GEO.pack(rx, ry, rw, rh) + jpeg)

    def send_frame_paced(self, rx, ry, rw, rh, jpeg, budget_s,
                         chunk=PACE_CHUNK):
        """Send one frame spread evenly over budget_s instead of at line rate.

        One sendall() of a few hundred KB leaves the NIC as ~230 back-to-back
        packets: about 3 ms of solid traffic at 1 Gbps, then silence until the
        next frame. The average is modest but the peak is the full line rate,
        and the peak is what overruns the small shared buffers in unmanaged
        switches - dropping other machines' packets along with our own.
        Spreading the writes leaves the average identical and removes the peak.

        The entire frame is written while holding the send lock, so a
        concurrent send_json() cannot interleave and desync the framing.
        """
        if self._closed:
            return False
        payload = _GEO.pack(rx, ry, rw, rh) + jpeg
        pkt = _HDR.pack(MSG_FRAME, len(payload)) + payload
        if budget_s <= 0 or len(pkt) <= chunk:
            return self._write_packet(pkt)

        count = (len(pkt) + chunk - 1) // chunk
        gap = budget_s / count
        try:
            with self._send_lock:
                start = time.perf_counter()
                for i in range(count):
                    self.sock.sendall(pkt[i * chunk:(i + 1) * chunk])
                    if i + 1 == count:
                        break
                    # Schedule against an absolute start time, so a write that
                    # blocks shortens the next wait instead of stretching the
                    # whole frame past its budget.
                    slack = start + (i + 1) * gap - time.perf_counter()
                    if slack > 0:
                        time.sleep(slack)
            return True
        except OSError:
            self._closed = True
            return False

    def send_json(self, obj):
        return self._send_raw(MSG_JSON, json.dumps(obj).encode("utf-8"))

    # ---- receiving -----------------------------------------------------------
    def _recv_exact(self, n):
        """Read exactly n bytes. Returns None on clean EOF or socket error."""
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                self._closed = True
                return None
            if not chunk:
                self._closed = True
                return None
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv(self):
        """Return (mtype, payload), or None once the peer is gone."""
        head = self._recv_exact(_HDR.size)
        if head is None:
            return None
        mtype, length = _HDR.unpack(head)
        if length > MAX_PAYLOAD:
            self._closed = True
            raise ProtoError(f"payload too large: {length}")
        payload = self._recv_exact(length) if length else b""
        if payload is None:
            return None
        return mtype, payload

    @property
    def closed(self):
        return self._closed

    def close(self):
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def unpack_frame(payload):
    """Split a MSG_FRAME payload into ((rx, ry, rw, rh), jpeg_bytes)."""
    if len(payload) < _GEO.size:
        raise ProtoError("frame payload shorter than geometry header")
    return _GEO.unpack(payload[:_GEO.size]), payload[_GEO.size:]


def decode_json(payload):
    return json.loads(payload.decode("utf-8"))
