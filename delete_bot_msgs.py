"""
delete_bot_msgs.py  –  delete the bot's N most recent messages from the main group.

Uses the Telegram Bot API only – no user account or phone number needed.

How it works:
  1. Sends a silent probe message to learn the current max message_id.
  2. Deletes the probe immediately.
  3. Sweeps message IDs backwards, trying deleteMessage for each one.
     - Success  → it was a bot message (bots can only delete their own messages
                   unless they're admins; see note below).
     - Failure  → someone else's message or already deleted – silently skipped.
  4. Stops once N bot messages have been deleted.

NOTE: if the bot is a group admin with "Delete messages" permission it can
delete ANY message, not just its own.  In that case, confirm you want to
proceed when the script warns you.
"""

import os
import sys
import time

# ── locate coordinator config ──────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "PXM_RB"))

try:
    import coordinator.config as _base_cfg
    _local = os.path.join(_HERE, "PXM_RB", "coordinator", "config_local.py")
    if os.path.exists(_local):
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("config_local", _local)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        for _k, _v in vars(_mod).items():
            if not _k.startswith("_"):
                setattr(_base_cfg, _k, _v)
    TG_TOKEN      = _base_cfg.TG_TOKEN
    TG_CHAT       = str(_base_cfg.TG_CHAT_ID)
    TG_DEBUG_CHAT = str(getattr(_base_cfg, "TG_DEBUG_CHAT_ID", "") or "")
except Exception as _e:
    print(f"[warn] Could not load coordinator config ({_e}); enter manually.")
    TG_TOKEN      = input("Bot token: ").strip()
    TG_CHAT       = input("Main group chat_id (e.g. -1001234567890): ").strip()
    TG_DEBUG_CHAT = input("Debug group chat_id (leave blank to use main): ").strip()

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

BASE = f"https://api.telegram.org/bot{TG_TOKEN}"
# ── choose target group ────────────────────────────────────────────────────
print(f"\nTarget: MAIN group ({TG_CHAT})")
_raw = TG_CHAT
chat_id = int(_raw) if _raw.lstrip("-").isdigit() else _raw


def api(method, **kwargs):
    r = requests.post(f"{BASE}/{method}", json=kwargs, timeout=10)
    try:
        return r.json()
    except Exception:
        return {}


# ── check bot admin status ─────────────────────────────────────────────────
me = api("getMe").get("result", {})
bot_id   = me.get("id")
bot_name = me.get("username", "?")
print(f"\nBot: @{bot_name} (id={bot_id})")

member = api("getChatMember", chat_id=chat_id, user_id=bot_id).get("result", {})
status = member.get("status", "")
can_delete = member.get("can_delete_messages", False)

if status in ("administrator", "creator") and can_delete:
    print("\n⚠️  WARNING: this bot is a GROUP ADMIN with 'Delete messages' permission.")
    print("    It can delete ANY message, not just its own.")
    confirm = input("    Type YES to continue: ").strip()
    if confirm.upper() != "YES":
        print("Aborted.")
        sys.exit(0)
else:
    print("Bot is not an admin – will only delete its own messages.")

# ── ask how many to delete ─────────────────────────────────────────────────
def _ask_n():
    while True:
        n_str = input("\nHow many of the bot's most recent messages to delete? "
                      "(or 'q' to quit) ").strip()
        if n_str.lower() in ("q", "quit", "exit"):
            return None
        try:
            n = int(n_str)
            assert n > 0
            return n
        except Exception:
            print("  Enter a positive number.")

# ── find starting message_id from coordinator DB (no probe needed) ──────────
_CACHE = os.path.join(_HERE, ".last_msg_id")

def _max_id_from_db():
    """Return the highest known message_id from the coordinator DB, or None."""
    try:
        import sqlite3
        # Use the path from config if available, otherwise guess.
        db_path = getattr(_base_cfg, "DB_PATH",
                          os.path.join(_HERE, "PXM_RB", "data", "rb.sqlite3"))
        if not os.path.exists(db_path):
            return None
        con = sqlite3.connect(db_path)
        candidates = []
        # Try every column that might hold a Telegram message_id.
        for table, col in [
            ("boss_state", "spawn_msg_id"),
            ("screenshots", "msg_id"),
            ("screenshot_buttons", "msg_id"),
        ]:
            try:
                row = con.execute(
                    f"SELECT MAX({col}) FROM {table} WHERE {col} IS NOT NULL"
                ).fetchone()
                if row and row[0]:
                    candidates.append(int(row[0]))
            except Exception:
                pass
        con.close()
        return max(candidates) if candidates else None
    except Exception:
        return None



candidates = []

# 1. Cached value from the last run.
if os.path.exists(_CACHE):
    try:
        candidates.append(int(open(_CACHE).read().strip()))
    except Exception:
        pass

# 2. Coordinator DB (spawn_msg_id, screenshot msg_ids, …).
db_id = _max_id_from_db()
if db_id:
    candidates.append(db_id)

# 3. Always send a silent probe to capture any messages sent since the last
#    run (the cache alone cannot know about those).  The probe is deleted
#    immediately and is never visible to users.
probe = api("sendMessage", chat_id=chat_id, text=".",
            disable_notification=True)
if probe.get("ok"):
    probe_id = probe["result"]["message_id"]
    api("deleteMessage", chat_id=chat_id, message_id=probe_id)
    candidates.append(probe_id)
    print(f"Probe: got message_id {probe_id} (deleted immediately)")
else:
    print(f"WARNING: probe failed – {probe.get('description', probe)}"
          f"\n         Starting position may be stale.")

if not candidates:
    print("ERROR: could not determine current message_id."); sys.exit(1)

max_id = max(candidates)
print(f"\nStarting message_id: {max_id}")

# ── interactive deletion loop ───────────────────────────────────────────────
# msg_id tracks the current sweep position across iterations so each new
# deletion round continues from where the last one left off.
msg_id = max_id - 1

while True:
    n = _ask_n()
    if n is None:
        print("Bye.")
        break

    MAX_SWEEP = max(n * 50, 500)
    deleted   = 0
    swept     = 0

    print(f"Sweeping up to {MAX_SWEEP} IDs backwards (from {msg_id}) "
          f"to find {n} bot messages…")

    while deleted < n and swept < MAX_SWEEP and msg_id > 0:
        result = api("deleteMessage", chat_id=chat_id, message_id=msg_id)
        ok   = result.get("ok")
        res  = result.get("result")
        desc = result.get("description", "")
        if ok and res is True:
            deleted += 1
            print(f"  [{msg_id}] DELETED  ({deleted}/{n})")
        elif ok is False:
            # Show every failure so we can see which IDs exist but can't be deleted.
            print(f"  [{msg_id}] skip – {desc or result}")
        # ok=True but result is not True → already deleted / doesn't exist, silent.
        swept  += 1
        msg_id -= 1
        time.sleep(0.05)

    # Save the current position so the next session starts from here.
    try:
        open(_CACHE, "w").write(str(msg_id))
    except Exception:
        pass

    print(f"Done – deleted {deleted} of {n} requested messages "
          f"(swept {swept} IDs).")
    if deleted < n:
        print(f"Only {deleted} bot messages found in the last {MAX_SWEEP} IDs.")
