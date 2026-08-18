"""Connect to a running stream_sender like the viewer does, and report.

    python probe_sender.py [host] [port] [seconds]

Exercises the paths the viewer drives - hello, scale/fps/quality, region, and a
zoom-in - and prints the frames and stats that come back. Useful for checking a
slave without opening the full grid.
"""

import socket
import sys
import time

import stream_proto as proto
from stream_proto import Channel


def probe(host, port, seconds, scale, fps, quality, label):
    sock = socket.create_connection((host, port), timeout=5.0)
    sock.settimeout(None)
    ch = Channel(sock)

    frames = 0
    total = 0
    stats = []
    hello = None
    geom = None

    ch.send_json({"t": "scale", "v": scale})
    ch.send_json({"t": "fps", "v": fps})
    ch.send_json({"t": "quality", "v": quality})

    end = time.perf_counter() + seconds
    first = None
    while time.perf_counter() < end:
        msg = ch.recv()
        if msg is None:
            break
        mtype, payload = msg
        if mtype == proto.MSG_FRAME:
            g, jpeg = proto.unpack_frame(payload)
            geom = g
            frames += 1
            total += len(jpeg)
            if first is None:
                first = time.perf_counter()
        else:
            obj = proto.decode_json(payload)
            if obj.get("t") == "hello":
                hello = obj
            elif obj.get("t") == "stat":
                stats.append(obj)

    span = time.perf_counter() - (first or time.perf_counter() - seconds)
    ch.close()

    if hello:
        print("hello: name=%s screen=%dx%d capture=%s arduino=%s"
              % (hello.get("name"), hello.get("sw"), hello.get("sh"),
                 hello.get("cap"), hello.get("arduino")))
        for w in hello.get("warn") or []:
            print("  warn: %s" % w)
    print("%-14s asked %4.1f fps scale %.2f q%d -> got %5.2f fps  %6.1f KB/frame  "
          "%5.2f Mbps  geom=%s"
          % (label, fps, scale, quality, frames / max(span, 0.001),
             total / max(frames, 1) / 1024.0,
             total * 8 / max(span, 0.001) / 1e6, geom))
    if stats:
        last = stats[-1]
        print("               slave stat: build %.1f ms  %.1f KB  drop %s  same %s"
              % (last.get("ms", 0), last.get("kb", 0),
                 last.get("drop"), last.get("same")))
    return frames


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8772
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0

    print("probing %s:%d\n" % (host, port))
    probe(host, port, secs, 0.34, 1.0, 45, "idle grid")
    probe(host, port, secs, 0.34, 15.0, 70, "active grid")
    probe(host, port, secs, 1.0, 15.0, 70, "zoomed")
    probe(host, port, secs, 1.0, 30.0, 70, "zoomed 30fps")


if __name__ == "__main__":
    main()
