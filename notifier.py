"""
notifier.py - Telegram notifications with per-client, per-event cooldowns.

Notifications are dispatched on a background daemon thread via a queue so the
main bot loop is never blocked by Telegram API latency or outages.

Queue items are callables so both text and photo messages share the same worker.
"""

import io
import time
import queue
import threading

try:
    import requests
except Exception:
    requests = None

from logger import info, warn

_SENTINEL = None  # pushed to queue to stop the worker


class Notifier:
    def __init__(self, token, group_id, timeout=10.0):
        self.token = token
        self.group_id = group_id
        self.timeout = timeout
        self._last_sent = {}          # (client, event) -> timestamp
        self._lock = threading.Lock() # guards _last_sent across threads
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ---- background worker ---------------------------------------------------
    def _worker(self):
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                break
            item()  # each queued item is a zero-argument callable

    def _send_blocking(self, text, retries=2):
        if requests is None:
            warn("[TG] requests not available - cannot send")
            return False
        if not self.token or self.token.startswith("PUT_YOUR"):
            info(f"[TG] (disabled) {text}")
            return False
        params = {"chat_id": self.group_id, "text": text}
        last_err = None
        for attempt in range(retries + 1):
            try:
                r = requests.get(self._url("sendMessage"), params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return True
                last_err = f"HTTP {r.status_code}: {r.text[:120]}"
            except Exception as e:
                last_err = str(e)
            if attempt < retries:
                time.sleep(0.5)
        warn(f"[TG] send failed (all {retries + 1} attempts): {last_err}")
        return False

    def _send_photo_blocking(self, image_bytes, caption, retries=2):
        if requests is None:
            warn("[TG] requests not available - cannot send photo")
            return False
        if not self.token or self.token.startswith("PUT_YOUR"):
            info(f"[TG] (disabled photo) {caption}")
            return False
        data = {"chat_id": self.group_id, "caption": caption}
        last_err = None
        for attempt in range(retries + 1):
            try:
                files = {"photo": ("screenshot.png", io.BytesIO(image_bytes), "image/png")}
                r = requests.post(self._url("sendPhoto"), data=data, files=files, timeout=30.0)
                if r.status_code == 200:
                    return True
                last_err = f"HTTP {r.status_code}: {r.text[:120]}"
            except requests.exceptions.ReadTimeout:
                # Read timeout means the request reached Telegram — do not retry
                # to avoid duplicate messages.
                warn("[TG] send photo timed out waiting for response (message likely delivered)")
                return True
            except Exception as e:
                last_err = str(e)
            if attempt < retries:
                time.sleep(0.5)
        warn(f"[TG] send photo failed (all {retries + 1} attempts): {last_err}")
        return False

    def _url(self, method):
        return f"https://api.telegram.org/bot{self.token}/{method}"

    # ---- public API (non-blocking, called from any thread) -------------------
    def send(self, text, retries=2):
        """Enqueue a text message for background delivery. Returns immediately."""
        self._queue.put(lambda: self._send_blocking(text, retries))

    def send_photo(self, image_bytes, caption="", retries=2):
        """Enqueue a photo for background delivery. image_bytes is raw PNG bytes."""
        self._queue.put(lambda: self._send_photo_blocking(image_bytes, caption, retries))

    def notify(self, client_name, event_key, text, cooldown):
        """Enqueue a text message only if this (client, event) cooldown has elapsed."""
        now = time.time()
        key = (client_name, event_key)
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if now - last < cooldown:
                return False
            self._last_sent[key] = now
        self._queue.put(lambda: self._send_blocking(text, 2))
        return True

    def notify_with_photo(self, client_name, event_key, caption, image_bytes, cooldown):
        """Enqueue a photo+caption only if this (client, event) cooldown has elapsed."""
        now = time.time()
        key = (client_name, event_key)
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if now - last < cooldown:
                return False
            self._last_sent[key] = now
        self._queue.put(lambda: self._send_photo_blocking(image_bytes, caption, 2))
        return True

    def in_cooldown(self, client_name, event_key, cooldown):
        """Return True if this (client, event) is still within its cooldown window."""
        with self._lock:
            return time.time() - self._last_sent.get((client_name, event_key), 0.0) < cooldown

    def reset_event(self, client_name, event_key):
        """Clear a cooldown so the next occurrence notifies immediately."""
        with self._lock:
            self._last_sent.pop((client_name, event_key), None)

    def stop(self):
        """Signal the worker thread to exit cleanly (called on bot shutdown)."""
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=2.0)
