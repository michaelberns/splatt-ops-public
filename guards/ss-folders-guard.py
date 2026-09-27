#!/usr/bin/env python3
"""
SS Folders guard: moves stray PROJECT files back into the project folders.

What it does
    Every project has one folder, SS Folders/<Client>/<Project>/, holding its
    PROJECT.md (the project log) and PROJECT-overview.html. If a file with
    either name turns up anywhere else (Desktop, Downloads, Documents, /tmp),
    this guard moves it into SS Folders.

How a pass works
    1. Walk the scan roots (SCAN_ROOTS) for files named PROJECT.md or
       PROJECT-overview.html, skipping anything already inside SS Folders.
    2. Work out where each one belongs from its first heading or <title>,
       which is written as "Client - Project".
    3. Copy it there, then delete the original. Nothing is ever overwritten:
       - if the destination already holds identical bytes, the stray is just
         a duplicate and is deleted;
       - if it holds different content, the stray goes to quarantine
         (SS Folders/_guard_quarantine/) with a timestamp, to be merged by
         hand;
       - if the owner cannot be read from the file, it also goes to
         quarantine.
    4. If the original cannot be deleted, it is renamed with a
       .moved-to-ss-folders suffix so the next pass does not pick it up
       again.

Why it exists
    The files server (server/project-files-server.js) only writes inside
    SS Folders. This guard covers everything that does not go through the
    server: manual saves, one-off scripts, drag and drop, or an agent that
    writes to the wrong path.

Modes
    --once     run one pass, print a JSON summary and exit
    --audit    report what would move, change nothing
    --daemon   (default) run a pass every SS_GUARD_INTERVAL seconds (60)

Configuration
    SS_FOLDERS_PATH     the SS Folders root, default ~/Documents/Splatt/SS Folders
    SS_GUARD_INTERVAL   seconds between passes in daemon mode
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                        if both are set, a message is sent when a file is
                        moved or an audit finds strays; clean passes are silent

Logging
    To stderr (bin/start redirects this to logs/ss-folders-guard.log) and to
    guards/logs/ss-folders-guard.log. The list of strays already reported is
    kept in guards/.ss_folders_guard_state.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Iterable
from urllib import request as urlrequest
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOME = Path(os.environ.get("HOME", str(Path.home())))

# The SS Folders root. Override with SS_FOLDERS_PATH.
SS_FOLDERS = Path(
    os.environ.get("SS_FOLDERS_PATH")
    or HOME / "Documents" / "Splatt" / "SS Folders"
)

# Where to look for stray PROJECT files. Kept to the places files usually
# land by mistake; walking the whole home directory would be slow.
SCAN_ROOTS: list[Path] = [
    HOME / "Desktop",
    HOME / "Downloads",
    HOME / "Documents",            # SS Folders is inside this; it is skipped during the walk
    HOME / "SS Folders",           # a copy of the tree directly under home
    HOME / "Splatt",
    Path("/tmp"),
]

# Filenames that should ONLY exist under SS Folders.
PROTECTED_NAMES = {"PROJECT.md", "PROJECT-overview.html"}

# Directory names never walked into: hidden folders, dependency and cache
# folders, and pytest's temporary trees.
#
# pytest-of-<user> matters because /tmp is a scan root and this repo's own
# tests write a PROJECT.md for "Test Client" into pytest's temporary folder.
# Without the skip, running the tests would create a real "Test Client"
# folder in SS Folders. (On macOS the temp folder is usually under
# /var/folders, but it is under /tmp whenever TMPDIR is unset.)
SKIP_DIR_NAMES = {"node_modules", "venv", "__pycache__"}
SKIP_DIR_PREFIXES = ("pytest-of-",)


def _skip_dir(name: str) -> bool:
    """True for a folder the walk should not enter."""
    return (
        name.startswith(".")
        or name in SKIP_DIR_NAMES
        or name.startswith(SKIP_DIR_PREFIXES)
    )

# Seconds between passes in daemon mode.
SCAN_INTERVAL = int(os.environ.get("SS_GUARD_INTERVAL", "60"))

# The strays seen on the last pass, so new ones can be told apart from ones
# already reported.
SCRIPT_DIR = Path(__file__).parent
STATE_PATH = SCRIPT_DIR / ".ss_folders_guard_state.json"
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Where a stray goes when its owner cannot be read from the file, or when
# its destination already holds different content. It is inside SS Folders,
# so nothing is lost, and nothing in a project folder is overwritten.
QUARANTINE_DIR = SS_FOLDERS / "_guard_quarantine"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "ss-folders-guard.log"),
    ],
)
log = logging.getLogger("ss-guard")

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _telegram_send(text: str) -> None:
    """Send a Telegram message if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.

    Best effort: a failure is logged and never stops the pass.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urlencode({"chat_id": chat, "text": text}).encode("utf-8")
        req = urlrequest.Request(url, data=data, method="POST")
        with urlrequest.urlopen(req, timeout=10) as r:
            r.read()
    except Exception as e:  # noqa: BLE001 (best-effort notification)
        log.warning("Telegram notify failed: %s", e)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """The saved state, or an empty one if the file is missing or unreadable."""
    if not STATE_PATH.exists():
        return {"known_strays": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"known_strays": []}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to save state: %s", e)


# ---------------------------------------------------------------------------
# Routing: work out where a stray PROJECT file belongs
# ---------------------------------------------------------------------------

# How a PROJECT file names its owner, tried in order. A route needs both a
# client and a project, for example:
#   "# Clearwater Bottling - Filler Upgrade"                Markdown heading
#   "<title>Clearwater Bottling - Filler Upgrade</title>"   HTML overview
# The third pattern captures only a client name, so on its own it never
# produces a route.
CLIENT_PATTERNS = [
    re.compile(r"^#\s+(.+?)\s+[-–—]\s+(.+?)$", re.MULTILINE),  # "# Client - Project"
    re.compile(r"<title>(.+?)\s+[-–—]\s+(.+?)</title>", re.IGNORECASE),
    re.compile(r"client[\s_-]*name[\"'\s:=]+([^\n\"<,]+)", re.IGNORECASE),
]


def _route_for(file_path: Path) -> Optional[Path]:
    """Return SS Folders/<Client>/<Project>/<filename> for a stray, or None.

    Only the first 8192 characters are read. The client and project come
    from the first pattern in CLIENT_PATTERNS that captures both; None means
    the owner cannot be told and the caller quarantines the file.
    """
    try:
        text = file_path.read_text(errors="replace")[:8192]
    except Exception:
        return None

    for pat in CLIENT_PATTERNS:
        m = pat.search(text)
        if m and len(m.groups()) >= 2:
            client = m.group(1).strip().strip("#").strip()
            project = m.group(2).strip().strip("#").strip()
            if client and project:
                return SS_FOLDERS / client / project / file_path.name

    return None


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def _real(p: Path) -> Path:
    """p with symlinks resolved, or p unchanged if that fails."""
    try:
        return Path(os.path.realpath(str(p)))
    except Exception:
        return p


def _is_inside(p: Path, root: Path) -> bool:
    """True if p is root or inside it, after resolving symlinks on both sides.

    Comparing resolved paths means a file reached through a symlink or a
    mounted alias of SS Folders is recognised as already inside it, so the
    guard never moves a file that is already in the right place.
    """
    try:
        rp = _real(p)
        rr = _real(root)
        if rp == rr:
            return True
        rp.relative_to(rr)
        return True
    except Exception:
        return False


def _fingerprint(p: Path) -> Optional[tuple]:
    """(inode, size, mtime in ns, basename), or None if p cannot be stat'ed.

    Two paths with the same fingerprint are the same file on disk, even when
    reached through a mount where realpath does not show the link. It needs
    only a stat, not a read.
    """
    try:
        s = p.stat()
        return (s.st_ino, s.st_size, s.st_mtime_ns, p.name)
    except Exception:
        return None


def _build_ss_index() -> set[tuple]:
    """Fingerprints of every PROJECT file already in SS Folders, collected in
    one walk, so a candidate that is the same file seen by another path can be
    skipped."""
    fps: set[tuple] = set()
    if not SS_FOLDERS.exists():
        return fps
    for dirpath, dirnames, filenames in os.walk(SS_FOLDERS, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn in PROTECTED_NAMES:
                fp = _fingerprint(Path(dirpath) / fn)
                if fp is not None:
                    fps.add(fp)
    return fps


# This repository itself is never scanned. It contains example project
# files (examples/SS Folders/) and test fixtures that are meant to stay where
# they are, and the repo may well be cloned somewhere inside a scan root such
# as ~/Documents.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _iter_strays() -> Iterable[Path]:
    """Yield every PROJECT.md or PROJECT-overview.html under the scan roots
    that is not already in SS Folders.

    "Already in SS Folders" is checked two ways: by resolved path, and by
    fingerprint (for aliases where the resolved path does not reveal it).
    Each file is yielded once even if two scan roots reach it.
    """
    ss_index = _build_ss_index()
    seen: set[Path] = set()
    ss_real = _real(SS_FOLDERS)
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        # A scan root that is, or resolves into, SS Folders is skipped whole.
        if _is_inside(root, SS_FOLDERS):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dp = Path(dirpath)
            if _is_inside(dp, SS_FOLDERS) or _is_inside(dp, REPO_ROOT):
                dirnames[:] = []
                continue
            # Prune folders that are never worth walking (see _skip_dir).
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            for fn in filenames:
                if fn in PROTECTED_NAMES:
                    p = (dp / fn)
                    rp = _real(p)
                    # Already in SS Folders by resolved path.
                    try:
                        rp.relative_to(ss_real)
                        continue
                    except Exception:
                        pass
                    # Already in SS Folders under another path (same
                    # fingerprint), for mounts that realpath cannot see through.
                    fp = _fingerprint(p)
                    if fp is not None and fp in ss_index:
                        continue
                    if rp in seen:
                        continue
                    seen.add(rp)
                    yield p


def _same_content(a: Path, b: Path) -> bool:
    """True if a and b are byte-for-byte identical (sizes compared first)."""
    try:
        sa, sb = a.stat(), b.stat()
        if sa.st_size != sb.st_size:
            return False
        with a.open("rb") as fa, b.open("rb") as fb:
            while True:
                ba = fa.read(65536)
                bb = fb.read(65536)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except Exception:
        return False


def _soft_delete(p: Path) -> bool:
    """Delete p, or if deleting is not allowed, rename it to
    <name>.moved-to-ss-folders so the next pass does not pick it up again.

    Returns True if p no longer exists under its original name.
    """
    try:
        p.unlink()
        return True
    except Exception:
        try:
            marker = p.with_name(p.name + ".moved-to-ss-folders")
            p.rename(marker)
            log.warning(
                "could not unlink %s, renamed to %s so it is not picked up again",
                p, marker.name,
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.error("could not delete or rename %s: %s", p, e)
            return False


def _move_stray(stray: Path, audit_only: bool) -> tuple[str, Optional[Path]]:
    """Move one stray into SS Folders. Returns (status, destination or None).

    Status values:
      moved        copied to the destination and the original removed
      duplicate    the destination already held identical bytes; original removed
      would-move   audit mode, nothing changed
      copy-only    copied, but the original could not be deleted or renamed
      error        the copy failed
    """
    dest = _route_for(stray)
    if dest is None:
        # Owner unknown: quarantine, named after the folder it was found in.
        dest = QUARANTINE_DIR / f"{stray.parent.name}__{stray.name}__{int(time.time())}"

    if audit_only:
        return ("would-move", dest)

    # The destination already holds the same bytes, so the stray is a
    # duplicate. Delete it rather than filling quarantine with copies.
    if dest.exists() and _same_content(stray, dest):
        if _soft_delete(stray):
            return ("duplicate", dest)
        return ("error", None)

    if dest.exists():
        # The destination holds different content. Never overwrite: park the
        # stray in quarantine with a timestamp so the operator can compare
        # and merge the two by hand.
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = QUARANTINE_DIR / f"{dest.parent.name}__{stray.name}__conflict-{ts}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Copy first, then delete the original. If the delete fails, the
        # data is already safe in SS Folders and _soft_delete renames the
        # original so it is not processed again.
        shutil.copy2(str(stray), str(dest))
        if _soft_delete(stray):
            return ("moved", dest)
        return ("copy-only", dest)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to copy %s -> %s: %s", stray, dest, e)
        return ("error", None)


# ---------------------------------------------------------------------------
# Run a single pass
# ---------------------------------------------------------------------------

def run_pass(*, audit_only: bool = False) -> dict:
    """Find and move every stray once, save state, notify, and return a summary.

    With audit_only, nothing is moved and the state file is not updated.
    """
    if not SS_FOLDERS.exists():
        log.error("SS_FOLDERS does not exist: %s, skipping this pass", SS_FOLDERS)
        return {"error": f"missing: {SS_FOLDERS}"}

    state = _load_state()
    known = set(state.get("known_strays", []))
    found: list[str] = []
    moved: list[tuple[str, str]] = []
    duplicates: list[str] = []
    copy_only: list[tuple[str, str]] = []
    errors: list[str] = []
    new_strays: list[str] = []

    # Collect every stray before moving any, so the walk never sees a tree
    # that is changing under it.
    strays = list(_iter_strays())

    for stray in strays:
        s = str(stray)
        found.append(s)
        if s not in known:
            new_strays.append(s)
        status, dest = _move_stray(stray, audit_only=audit_only)
        if status == "moved" and dest is not None:
            moved.append((s, str(dest)))
            log.warning("Moved stray %s -> %s", s, dest)
        elif status == "duplicate" and dest is not None:
            duplicates.append(s)
            log.info("Duplicate of existing SS Folders file removed: %s", s)
        elif status == "copy-only" and dest is not None:
            copy_only.append((s, str(dest)))
            log.warning(
                "Copied %s -> %s (source could not be removed; left as soft-marker)",
                s, dest,
            )
        elif status == "would-move":
            log.info("AUDIT stray (would route to %s): %s", dest, s)
        elif status == "error":
            errors.append(s)

    if not audit_only:
        # Strays still at their original path are remembered as already seen.
        cleared = {src for src, _ in moved} | set(duplicates)
        leftover = [s for s in found if s not in cleared]
        state["known_strays"] = leftover
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)

    summary = {
        "ss_folders": str(SS_FOLDERS),
        "found": len(found),
        "moved": len(moved),
        "duplicates": len(duplicates),
        "copy_only": len(copy_only),
        "errors": len(errors),
        "new_strays": new_strays,
        "moved_pairs": moved,
        "copy_only_pairs": copy_only,
        "audit_only": audit_only,
    }

    # Notify only when a live pass changed something or hit an error, or an
    # audit found strays. Clean passes stay silent.
    if not audit_only and (moved or errors or copy_only):
        lines = [f"SS Folders Guard pass at {datetime.now().strftime('%H:%M:%S')}"]
        for src, dest in moved:
            lines.append(f"moved: {Path(src).name} <- {Path(src).parent}")
            lines.append(f"   -> {Path(dest).relative_to(SS_FOLDERS) if _is_inside(Path(dest), SS_FOLDERS) else dest}")
        for src, dest in copy_only:
            lines.append(f"copy-only: {Path(src).name} (source not deletable)")
            lines.append(f"   -> {Path(dest).relative_to(SS_FOLDERS) if _is_inside(Path(dest), SS_FOLDERS) else dest}")
        if duplicates:
            lines.append(f"duplicates removed: {len(duplicates)}")
        if errors:
            lines.append(f"errors: {len(errors)}")
        _telegram_send("\n".join(lines))
    elif audit_only and found:
        lines = [f"SS Folders Guard AUDIT: {len(found)} stray PROJECT file(s) found"]
        for s in found[:10]:
            lines.append(f"  {s}")
        _telegram_send("\n".join(lines))

    log.info(
        "pass done: found=%d moved=%d errors=%d audit=%s",
        len(found), len(moved), len(errors), audit_only,
    )
    return summary


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(signum, frame):  # noqa: ARG001
    """Stop the daemon loop after the current second on SIGINT or SIGTERM."""
    global _running
    _running = False
    log.info("signal %s, shutting down", signum)


def daemon_loop():
    """Run a pass at start-up, then one every SCAN_INTERVAL seconds until stopped.

    A pass that raises is logged and the loop carries on.
    """
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "ss-folders-guard starting: root=%s interval=%ss scan=%s",
        SS_FOLDERS, SCAN_INTERVAL,
        ", ".join(str(p) for p in SCAN_ROOTS if p.exists()),
    )

    # Pass at start-up, so strays left while the guard was not running are handled.
    run_pass(audit_only=False)

    while _running:
        for _ in range(SCAN_INTERVAL):
            if not _running:
                break
            time.sleep(1)
        if not _running:
            break
        try:
            run_pass(audit_only=False)
        except Exception as e:  # noqa: BLE001 (one bad pass must not stop the daemon)
            log.exception("pass failed: %s", e)

    log.info("ss-folders-guard stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--once", action="store_true", help="run one pass and exit")
    grp.add_argument("--audit", action="store_true", help="report-only, do not move anything")
    grp.add_argument("--daemon", action="store_true", help="loop forever (default)")
    args = parser.parse_args()

    if args.audit:
        summary = run_pass(audit_only=True)
        print(json.dumps(summary, indent=2))
        return 0
    if args.once:
        summary = run_pass(audit_only=False)
        print(json.dumps(summary, indent=2))
        return 0

    daemon_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
