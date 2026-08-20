#!/usr/bin/env python3
"""
updater.py — PXM_LU4 file checker / updater.

─── On the DEV machine (after making changes) ───────────────────────────────
  python updater.py --make
      Scans all tracked files, computes SHA-256 hashes, writes manifest.json.
      Run this every time you want to publish a new version to other machines.

─── On every OTHER machine ──────────────────────────────────────────────────
  python updater.py --source PATH
      PATH = path to the dev-machine copy (USB drive, network share, etc.)
      Example: python updater.py --source "E:\\PXM_LU4"
               python updater.py --source "\\\\SERVER\\share\\PXM_LU4"

  Reports:
    • Which code files are missing or outdated → offers to copy them.
    • config.py shared section (everything after the SHARED_MARKER line) is
      compared separately and can be auto-updated while the machine-specific
      header (WINDOWS, PC_NUMBER, RESOLUTION, …) is always preserved.
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
MANIFEST_FILE = "manifest.json"
LOCAL_ROOT    = Path(__file__).resolve().parent

# The report below is drawn with box characters. A Windows console renders them,
# but a redirected pipe falls back to the ANSI code page (cp1251 here) and raises
# UnicodeEncodeError before any work is done, so force UTF-8 where possible.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Marker line that splits machine-specific header from shared settings in config.py.
# Everything AFTER this line (inclusive of the line itself) is auto-updatable.
CONFIG_SHARED_MARKER = "# ─── SHARED SETTINGS"

# Code / batch files that are identical on every machine.
# config.py is deliberately excluded — it is machine-specific.
TRACKED_CODE = [
    "bot.py",
    "hotkey_assist.py",
    "arduino_hid.py",
    "vision.py",
    "window_manager.py",
    "logger.py",
    "notifier.py",
    "capslock.py",
    "hp_monitor.py",
    "input_recorder.py",
    "rmb_anchor_timer.py",
    "debug_templates.py",
    "run_bot.bat",
    "run_hotkey_assist.bat",
    "run_hp_monitor.bat",
    "run_recorder.bat",
    "run_rmb_timer.bat",
    "updater.py",
    "make_manifest.bat",
    "run_updater.bat",
    "start_fileserver.bat",
    "fileserver.py",
    # Remote-control fleet viewer. The sender and the protocol are needed on
    # every slave; the viewer is only used on the main PC but is tracked anyway
    # so any machine can drive the fleet. stream_slaves.json is deliberately
    # excluded — it holds this LAN's host list, like config.py holds machine
    # settings. mouse.ino is tracked so the firmware source stays in sync, but
    # copying it does NOT flash a board; that is still done by hand per Arduino.
    "stream_proto.py",
    "stream_sender.py",
    "stream_viewer.py",
    "minimap_orient.py",
    # Capture benchmark: run it on a slave to confirm dxcam is installed and
    # working there, and to see that machine's frame-rate ceiling.
    "bench_capture.py",
    "minimap_orient.bat",
    "minimap_align.bat",
    "run_stream_sender.bat",
    "run_stream_sender_with_bot.bat",
    "run_stream_viewer.bat",
    "setup_slave_firewall.bat",
    "setup_slave_firewall.ps1",
    "mouse.ino",
    "l2cursor.cur",
]

# Asset files to skip when scanning assets/ (debug / reference images only
# used for offset calculation — not needed on other machines).
_ASSET_IGNORE = {
    "debug_hp_region.png",
    "asus_char_bars.png",
    "asus_mob_bars.png",
    "open_bag.png",          # legacy name, kept for reference only
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_assets(root: Path) -> list[str]:
    """Return relative paths (forward slashes) of all tracked asset images."""
    result = []
    assets_dir = root / "assets"
    if not assets_dir.is_dir():
        return result
    for p in sorted(assets_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".png", ".jpg", ".bmp"):
            continue
        if p.name in _ASSET_IGNORE:
            continue
        rel = p.relative_to(root).as_posix()
        result.append(rel)
    return result


def _config_keys(path: Path) -> set[str]:
    """
    Extract all top-level variable names defined in a config.py.
    Matches lines like:  SOME_KEY = ...  (identifier at column 0).
    """
    keys = set()
    pattern = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=", re.MULTILINE)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in pattern.finditer(text):
            keys.add(m.group(1))
    except Exception as e:
        print(f"  [!] Could not read {path}: {e}")
    return keys


def _normalize(text: str) -> str:
    """Normalize line endings to \\n so mixing sources never causes double-newlines."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_config(text: str) -> tuple[str, str]:
    """
    Split config.py text at CONFIG_SHARED_MARKER.
    Returns (header, shared) where header is everything BEFORE the marker line
    (not including it) and shared is the marker line + everything after.
    If no marker is found, returns (text, "").
    """
    text = _normalize(text)
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith(CONFIG_SHARED_MARKER):
            header = "".join(lines[:i])
            shared = "".join(lines[i:])
            return header, shared
    return text, ""


def _shared_sha256(text: str) -> str:
    """SHA256 of just the shared portion of a config.py text."""
    _, shared = _split_config(text)
    return hashlib.sha256(shared.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# --make  (run on dev machine)
# ─────────────────────────────────────────────────────────────────────────────

def cmd_make() -> None:
    tracked = TRACKED_CODE + _scan_assets(LOCAL_ROOT)
    manifest: dict[str, dict] = {}
    missing: list[str] = []

    for rel in tracked:
        p = LOCAL_ROOT / rel
        if p.exists():
            manifest[rel] = {
                "sha256": _sha256(p),
                "size":   p.stat().st_size,
            }
        else:
            missing.append(rel)

    # config.py: store SHA256 of shared portion only so other machines can
    # auto-update that section while keeping their machine-specific header.
    cfg = LOCAL_ROOT / "config.py"
    if cfg.exists():
        cfg_text = cfg.read_text(encoding="utf-8", errors="replace")
        manifest["config.py"] = {
            "sha256":        _sha256(cfg),          # full-file hash (informational)
            "shared_sha256": _shared_sha256(cfg_text),  # shared-section hash
            "size":          cfg.stat().st_size,
            "partial_update": True,   # updater replaces only the shared portion
        }

    out = LOCAL_ROOT / MANIFEST_FILE
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"[MAKE] manifest.json written — {len(manifest)} entries.")
    if missing:
        print(f"[MAKE] {len(missing)} file(s) not found locally (skipped):")
        for m in missing:
            print(f"         {m}")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_http(s: str) -> bool:
    return s.lower().startswith(("http://", "https://"))


def _http_get_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read()
    except urllib.error.URLError as e:
        print(f"[ERROR] HTTP request failed: {url}\n        {e}")
        sys.exit(1)


def _join_url(base: str, rel: str) -> str:
    return base.rstrip("/") + "/" + rel.replace("\\", "/")


# ─────────────────────────────────────────────────────────────────────────────
# --source  (run on other machines)
# ─────────────────────────────────────────────────────────────────────────────

def cmd_check(source_str: str) -> None:
    http_mode = _is_http(source_str)
    W = 60

    # ── Load manifest ─────────────────────────────────────────────────────────
    if http_mode:
        print(f"  Connecting to {source_str} …")
        manifest_raw = _http_get_bytes(_join_url(source_str, MANIFEST_FILE))
        manifest: dict[str, dict] = json.loads(manifest_raw)
        # Also fetch source config.py text for key comparison
        try:
            src_cfg_text: str | None = _http_get_bytes(
                _join_url(source_str, "config.py")
            ).decode("utf-8", errors="replace")
        except SystemExit:
            src_cfg_text = None
    else:
        source = Path(source_str)
        if not source.exists():
            print(f"[ERROR] Source path not found: {source}")
            sys.exit(1)
        manifest_path = source / MANIFEST_FILE
        if not manifest_path.exists():
            print(f"[ERROR] manifest.json not found in: {source}")
            print("        Run  python updater.py --make  on the dev machine first.")
            sys.exit(1)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        src_cfg = source / "config.py"
        src_cfg_text = src_cfg.read_text(encoding="utf-8", errors="replace") \
            if src_cfg.exists() else None

    print(f"\n{'═'*W}")
    print(f"  PXM_LU4 Updater")
    print(f"  Source : {source_str}")
    print(f"  Local  : {LOCAL_ROOT}")
    print(f"{'═'*W}\n")

    # ── File comparison ───────────────────────────────────────────────────────
    outdated: list[tuple[str, str]] = []
    ok:       list[str]             = []

    config_outdated = False   # tracked separately for partial-update flow

    for rel, info in manifest.items():
        if info.get("partial_update"):
            # config.py — compare shared section only
            dst_p = LOCAL_ROOT / rel
            if not dst_p.exists():
                print(f"  [WARN] Local {rel} not found!")
                config_outdated = True
            else:
                local_text = dst_p.read_text(encoding="utf-8", errors="replace")
                if _shared_sha256(local_text) != info.get("shared_sha256", ""):
                    config_outdated = True
            continue                    # handled separately below

        dst_p = LOCAL_ROOT / rel

        if not http_mode:
            src_p = Path(source_str) / rel
            if not src_p.exists():
                continue                # source itself missing → skip

        if not dst_p.exists():
            outdated.append((rel, "MISSING"))
        elif _sha256(dst_p) != info["sha256"]:
            outdated.append((rel, "OUTDATED"))
        else:
            ok.append(rel)

    print(f"  Up to date : {len(ok):3d} file(s)")
    if outdated:
        print(f"  Need update: {len(outdated):3d} file(s)\n")
        for rel, reason in outdated:
            print(f"    [{reason:8s}]  {rel}")
    else:
        print("  All tracked files are up to date.")

    # ── Config.py shared-section check ───────────────────────────────────────
    local_cfg = LOCAL_ROOT / "config.py"

    print(f"\n{'─'*W}")
    print("  config.py  (header preserved, shared section auto-updatable)")
    print(f"{'─'*W}")

    if src_cfg_text is None:
        print("  [SKIP] config.py not available from source.")
    elif not local_cfg.exists():
        print("  [WARN] Local config.py not found!")
    else:
        if config_outdated:
            print("  [OUTDATED] Shared settings section differs from source.")
        else:
            print("  Shared settings section is up to date.")

        # Key check (covers both header + shared keys)
        src_keys   = set(re.findall(r"^([A-Z_][A-Z0-9_]*)\s*=",
                                    src_cfg_text, re.MULTILINE))
        local_keys = _config_keys(local_cfg)
        missing_keys = sorted(src_keys - local_keys)
        extra_keys   = sorted(local_keys - src_keys)

        if missing_keys:
            print(f"\n  Keys in source but MISSING locally"
                  f" (will be fixed by shared-section update if listed below,"
                  f" otherwise add by hand):\n")
            for k in missing_keys:
                for line in src_cfg_text.splitlines():
                    if re.match(rf"^{re.escape(k)}\s*=", line):
                        print(f"    {line}")
                        break
                else:
                    print(f"    {k}  =  ???")
        else:
            print("  All config keys are present locally.")

        if extra_keys:
            print(f"\n  Keys in local config but NOT in source (probably fine):")
            for k in extra_keys:
                print(f"    {k}")

    # ── Offer to copy / download outdated files ───────────────────────────────
    if outdated:
        print(f"\n{'─'*W}")
        verb = "Download" if http_mode else "Copy"
        try:
            answer = input(
                f"  {verb} {len(outdated)} file(s) from source? [y/N]  "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""

        if answer == "y":
            copied = failed = 0
            for rel, _ in outdated:
                dst_p = LOCAL_ROOT / rel
                try:
                    if http_mode:
                        url = _join_url(source_str, rel)
                        data = _http_get_bytes(url)
                        dst_p.parent.mkdir(parents=True, exist_ok=True)
                        dst_p.write_bytes(data)
                    else:
                        src_p = Path(source_str) / rel
                        dst_p.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_p, dst_p)
                    print(f"    ✓  {rel}")
                    copied += 1
                except Exception as exc:
                    print(f"    ✗  {rel}  —  {exc}")
                    failed += 1
            print(f"\n  Done. {copied} copied, {failed} failed.")
        else:
            print("  No files were copied.")

    # ── Offer partial update of config.py shared section ─────────────────────
    if config_outdated and src_cfg_text is not None and local_cfg.exists():
        print(f"\n{'─'*W}")
        try:
            answer = input(
                "  Update config.py shared section (header preserved)? [y/N]  "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""

        if answer == "y":
            try:
                local_text = local_cfg.read_text(encoding="utf-8", errors="replace")
                local_header, _ = _split_config(local_text)   # _split_config normalizes
                _, src_shared   = _split_config(src_cfg_text)
                if not src_shared:
                    print("  [WARN] Marker not found in source config.py — skipping.")
                else:
                    new_text = local_header + src_shared
                    # Write with explicit \n so Python doesn't double-convert on Windows
                    local_cfg.write_text(new_text, encoding="utf-8", newline="\n")
                    print("  ✓  config.py shared section updated (header unchanged).")
            except Exception as exc:
                print(f"  ✗  Failed to update config.py: {exc}")
        else:
            print("  config.py shared section was not changed.")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PXM_LU4 file updater / config validator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--make", action="store_true",
        help="Generate manifest.json (run on the dev machine after changes).",
    )
    group.add_argument(
        "--source", metavar="PATH",
        help="Path to the source/master PXM_LU4 folder (USB, network share, …).",
    )
    args = parser.parse_args()

    if args.make:
        cmd_make()
    else:
        cmd_check(args.source)


if __name__ == "__main__":
    main()
