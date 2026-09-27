#!/usr/bin/env python3
"""
SS Deliverables guard: moves stray deliverable documents back into SS Folders.

What it does
    Deliverables are the documents that belong to a client project or a
    supplier: quotes, RFQs, equipment options documents, letters, invoices.
    If a .docx, .pdf, .xlsx, .xlsm or .pptx file whose name marks it as one
    turns up in Desktop, Downloads, Documents or /tmp, this guard moves it
    into the right folder under SS Folders.

    It is the companion of ss-folders-guard.py, which does the same for
    PROJECT.md and PROJECT-overview.html. It is a separate script because
    the routing is different: those two files have fixed names and say who
    they belong to in their first heading, while a deliverable can only be
    routed from its filename.

How a pass works
    1. Index SS Folders: every client folder with its project folders, and
       every supplier folder under _Suppliers/.
    2. Walk the scan roots for files with a deliverable extension whose name
       looks like a Splatt document (_looks_like_deliverable). Anything else,
       such as personal documents in Downloads, is left where it is.
    3. Route each one (_route_for):
       - a known supplier prefix (for example "dfs-" for Delta Filling
         Systems) goes to _Suppliers/<supplier>/attachments/;
       - otherwise a client name found in the filename goes to that client's
         project folder (or the client folder, if the project is unclear);
       - otherwise it goes to quarantine,
         SS Folders/_guard_quarantine/deliverables/.
    4. Copy, compare, then delete the original, with the same rules as the
       folders guard: never overwrite (a different file at the destination
       sends the stray to quarantine), delete a byte-identical duplicate,
       and rename an original that cannot be deleted to
       <name>.moved-to-ss-folders.

Modes
    --once     run one pass, print a JSON summary and exit
    --audit    report what would move, change nothing
    --daemon   (default) run a pass every SS_DELIVERABLES_INTERVAL seconds (60)

Configuration
    SS_FOLDERS_PATH             the SS Folders root, default ~/Documents/Splatt/SS Folders
    SS_DELIVERABLES_INTERVAL    seconds between passes in daemon mode
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                                if both are set, a message is sent when a file
                                is moved or an audit finds candidates

Logging
    To stderr (bin/start redirects this to logs/ss-deliverables-guard.log)
    and to guards/logs/ss-deliverables-guard.log. Candidates already reported
    are kept in guards/.ss_deliverables_guard_state.json.
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
from typing import Iterable, Optional
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

# Where to look for stray deliverables. The same places as ss-folders-guard.py.
SCAN_ROOTS: list[Path] = [
    HOME / "Desktop",
    HOME / "Downloads",
    HOME / "Documents",
    HOME / "SS Folders",
    HOME / "Splatt",
    Path("/tmp"),
]

# File types that can be deliverables. Anything else is left alone, whatever
# its name.
DELIVERABLE_EXTS = {".docx", ".pdf", ".xlsx", ".xlsm", ".pptx"}

# Directory names never walked into. The same list as ss-folders-guard.py,
# plus any folder named Library (application data, not documents).
#
# pytest-of-<user> is skipped because /tmp is a scan root and pytest keeps
# its temporary trees there; a test fixture that looks like a deliverable
# must not be filed into SS Folders.
SKIP_DIR_NAMES = {"node_modules", "venv", "__pycache__", "Library"}
SKIP_DIR_PREFIXES = ("pytest-of-",)


def _skip_dir(name: str) -> bool:
    """True for a folder the walk should not enter."""
    return (
        name.startswith(".")
        or name in SKIP_DIR_NAMES
        or name.startswith(SKIP_DIR_PREFIXES)
    )

# Filenames ignored even when the extension matches.
IGNORE_FILENAMES = {
    "desktop.ini",
    ".ds_store",
}

# Filename substrings that mark a file as NOT a deliverable, checked before
# anything else.
IGNORE_SUBSTRINGS = (
    # Generic personal/admin docs
    "payslip", "tax-return", "personal-",
    # Screenshots
    "screenshot",
)

# Seconds between passes in daemon mode. Override with SS_DELIVERABLES_INTERVAL.
SCAN_INTERVAL = int(os.environ.get("SS_DELIVERABLES_INTERVAL", "60"))

SCRIPT_DIR = Path(__file__).parent
STATE_PATH = SCRIPT_DIR / ".ss_deliverables_guard_state.json"
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Where a deliverable goes when it cannot be routed, or when its destination
# already holds a different file. Kept apart from the PROJECT-file
# quarantine so the operator can tell the two kinds apart.
QUARANTINE_DIR = SS_FOLDERS / "_guard_quarantine" / "deliverables"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "ss-deliverables-guard.log"),
    ],
)
log = logging.getLogger("ss-deliverables-guard")


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
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram notify failed: %s", e)


# ---------------------------------------------------------------------------
# State: candidates already seen, so new ones can be told apart
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
# Index: the clients, projects and suppliers that exist in SS Folders
# ---------------------------------------------------------------------------

# Folders at the SS Folders root that are not clients.
NON_CLIENT_DIRS = {
    "_Suppliers", "_Equipment Library", "_Planning", "_howtotasks",
    "_shipments", "_guard_quarantine",
}


def _norm(s: str) -> str:
    """Lower-case a name and drop everything but letters and digits.

    >>> _norm("Riverbend Wine Co.")
    'riverbendwineco'
    """
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# Company-name words dropped when matching a client name against a filename,
# so "Riverbend Wine Co" matches "Riverbend-Wine-Quote.pdf" although the
# filename leaves out "Co". The same goes for Ltd, Limited, NZ, Pty and so on.
_CLIENT_SUFFIX_TOKENS = {
    "co", "ltd", "limited", "nz", "inc", "incorporated",
    "pty", "pte", "llc", "gmbh", "company", "corp", "corporation",
    "international", "intl", "group",
}


def _client_name_tokens(real_name: str) -> list[str]:
    """Split a client name into lower-case words, without company suffixes.

    >>> _client_name_tokens("Clearwater Bottling (NZ) Ltd")
    ['clearwater', 'bottling']
    """
    raw = re.split(r"[^A-Za-z0-9]+", real_name.lower())
    return [t for t in raw if t and t not in _CLIENT_SUFFIX_TOKENS]


def _build_index() -> dict:
    """Read the top two levels of SS Folders into a routing index:

    {
      'clients':   { norm_name: { 'real_name': str, 'projects': { norm_proj: real_proj } } },
      'suppliers': { norm_name: real_name },
    }

    Keys are _norm() forms, values are the real folder names.
    """
    idx = {"clients": {}, "suppliers": {}}
    if not SS_FOLDERS.exists():
        return idx

    # Suppliers
    suppliers_root = SS_FOLDERS / "_Suppliers"
    if suppliers_root.exists():
        for entry in suppliers_root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                idx["suppliers"][_norm(entry.name)] = entry.name

    # Clients
    for entry in SS_FOLDERS.iterdir():
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in NON_CLIENT_DIRS:
            continue
        client_record = {"real_name": entry.name, "projects": {}}
        for sub in entry.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                client_record["projects"][_norm(sub.name)] = sub.name
        idx["clients"][_norm(entry.name)] = client_record

    return idx


# ---------------------------------------------------------------------------
# Filename classifier: is this a deliverable, and where does it belong?
# ---------------------------------------------------------------------------

# Supplier filename prefixes. A file whose name starts with one of these is a
# supplier document and goes to _Suppliers/<folder>/attachments/, but only
# if that supplier folder exists (see _route_supplier).
# Map: regex for the prefix -> supplier folder name under _Suppliers/.
SUPPLIER_PREFIX_HINTS = {
    r"^dfs[-_]": "Delta Filling Systems",
    r"^lumo[-_]": "Lumo Milano",
    r"^brm[-_]": "BRM",
    r"^polaris[-_]": "Falco-Polaris",
    r"^falco[-_]": "Falco-Polaris",
    r"^canline[-_]": "Canline Europa",
    r"^rialto[-_]": "RIALTO Filling",
    r"^taponera[-_]": "Taponera",
}

# Filename fragments that mark a Splatt deliverable, for example
# "equipment-options-sep.docx", "filler-quote.pdf" or "splatt-rfq-caps.xlsx".
DELIVERABLE_HINTS = (
    "equipment-options",
    "options-may", "options-jun", "options-jul", "options-aug",
    "options-sep", "options-oct", "options-nov", "options-dec",
    "options-jan", "options-feb", "options-mar", "options-apr",
    "-quote", "quote-",
    "-rfq", "rfq-",
    "splatt-",
)


def _looks_like_deliverable(name_lower: str) -> bool:
    """True if the filename looks like a document Splatt produces or receives.

    Conservative on purpose: when in doubt it returns False and the file
    stays where it is. An ignore substring always wins.
    """
    if any(s in name_lower for s in IGNORE_SUBSTRINGS):
        return False
    if any(s in name_lower for s in DELIVERABLE_HINTS):
        return True
    # A known supplier prefix also counts.
    for pat in SUPPLIER_PREFIX_HINTS:
        if re.match(pat, name_lower):
            return True
    return False


def _route_supplier(name_lower: str, idx: dict) -> Optional[Path]:
    """_Suppliers/<supplier>/attachments/ for a file with a known supplier
    prefix, or None.

    The supplier folder must already exist; the guard never creates a new
    supplier folder, so an unknown one falls through to client matching and
    then to quarantine.
    """
    for pat, canonical in SUPPLIER_PREFIX_HINTS.items():
        if re.match(pat, name_lower):
            # Confirm the supplier folder actually exists in the index.
            real = idx["suppliers"].get(_norm(canonical))
            if real:
                return SS_FOLDERS / "_Suppliers" / real / "attachments"
    return None


def _route_client(name_lower: str, idx: dict) -> Optional[Path]:
    """Find the client a filename belongs to. Returns the destination folder
    (without the filename), or None if no client matches.

    Three passes, most specific first; a later pass runs only if the earlier
    ones found nothing:
      1. The whole normalised client name appears in the normalised
         filename: "Harbourside Craft Distillers" ('harboursidecraftdistillers')
         in "Harbourside-Craft-Distillers-Quote.pdf". The longest match wins,
         and names shorter than 4 characters are ignored.
      2. The name without company suffixes: "Riverbend Wine Co" becomes
         'riverbendwine', which matches "Riverbend-Wine-Quote.pdf".
      3. The first two words only ('clearwaterone', 'harboursidecraft'), for
         filenames that shorten the company name. At least 6 characters.

    Then the project: a client with one project folder gets that folder; with
    several, a project name found in the filename picks one; otherwise the
    file goes to the client folder rather than a guessed project.
    """
    file_norm = _norm(name_lower)
    best_client_norm: Optional[str] = None

    # Pass 1: full normalised name substring (longest match wins)
    for cn in idx["clients"]:
        # At least 4 characters, so a very short name cannot match inside
        # unrelated words.
        if len(cn) >= 4 and cn in file_norm:
            if best_client_norm is None or len(cn) > len(best_client_norm):
                best_client_norm = cn

    # Pass 2: stripped corporate suffix
    if best_client_norm is None:
        for cn, record in idx["clients"].items():
            tokens = _client_name_tokens(record["real_name"])
            if not tokens:
                continue
            stripped = "".join(tokens)
            if len(stripped) >= 4 and stripped in file_norm:
                if best_client_norm is None or len(stripped) > len(best_client_norm or ""):
                    best_client_norm = cn

    # Pass 3: first-two-tokens (handles abbreviations)
    if best_client_norm is None:
        for cn, record in idx["clients"].items():
            tokens = _client_name_tokens(record["real_name"])
            if len(tokens) < 2:
                continue
            two = tokens[0] + tokens[1]
            if len(two) >= 6 and two in file_norm:
                if best_client_norm is None or len(two) > len(best_client_norm or ""):
                    best_client_norm = cn

    if best_client_norm is None:
        return None

    record = idx["clients"][best_client_norm]
    client_dir = SS_FOLDERS / record["real_name"]
    projects = record["projects"]

    # A client with one project: use that project folder.
    if len(projects) == 1:
        only = next(iter(projects.values()))
        return client_dir / only

    # Several projects: look for a project name in the filename.
    best_proj: Optional[str] = None
    for pn, real_proj in projects.items():
        if len(pn) >= 4 and pn in file_norm:
            if best_proj is None or len(pn) > len(_norm(best_proj)):
                best_proj = real_proj
    if best_proj is not None:
        return client_dir / best_proj

    # Project unclear: use the client folder rather than guess.
    return client_dir


def _route_for(file_path: Path, idx: dict) -> Optional[Path]:
    """The destination folder for a candidate, or None to quarantine it.

    Order: ignore rules, then a supplier prefix, then a client name. The
    caller appends the filename.
    """
    name_lower = file_path.name.lower()

    # Hard ignores
    if name_lower in IGNORE_FILENAMES:
        return None
    if file_path.suffix.lower() not in DELIVERABLE_EXTS:
        return None
    if not _looks_like_deliverable(name_lower):
        return None

    # 1) A supplier prefix is the strongest signal.
    sup = _route_supplier(name_lower, idx)
    if sup is not None:
        return sup

    # 2) A client name in the filename.
    cli = _route_client(name_lower, idx)
    if cli is not None:
        return cli

    # 3) A deliverable, but whose is unclear: the caller quarantines it.
    return None


# ---------------------------------------------------------------------------
# Filesystem helpers (the same as in ss-folders-guard.py)
# ---------------------------------------------------------------------------

def _real(p: Path) -> Path:
    """p with symlinks resolved, or p unchanged if that fails."""
    try:
        return Path(os.path.realpath(str(p)))
    except Exception:
        return p


def _is_inside(p: Path, root: Path) -> bool:
    """True if p is root or inside it, after resolving symlinks on both sides."""
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
    """(size, mtime in ns, basename), or None if p cannot be stat'ed.

    Unlike the folders guard this leaves out the inode, so a copy that kept
    its size, timestamp and name (for example one made with cp -p) also
    counts as already filed.
    """
    try:
        s = p.stat()
        return (s.st_size, s.st_mtime_ns, p.name)
    except Exception:
        return None


def _build_ss_fingerprint_index() -> set[tuple]:
    """Fingerprints of every deliverable-type file already in SS Folders, so
    a candidate that is the same file seen by another path can be skipped."""
    fps: set[tuple] = set()
    if not SS_FOLDERS.exists():
        return fps
    for dirpath, dirnames, filenames in os.walk(SS_FOLDERS, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in DELIVERABLE_EXTS:
                fp = _fingerprint(Path(dirpath) / fn)
                if fp is not None:
                    fps.add(fp)
    return fps


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


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

# This repository itself is never scanned. It contains example project
# files (examples/SS Folders/) and test fixtures that are meant to stay where
# they are, and the repo may well be cloned somewhere inside a scan root such
# as ~/Documents.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _iter_candidates(idx: dict) -> Iterable[Path]:
    """Yield every file under the scan roots that has a deliverable extension,
    looks like a Splatt document, and is not already in SS Folders (checked
    by resolved path and by fingerprint). Each file is yielded once."""
    ss_index = _build_ss_fingerprint_index()
    seen: set[Path] = set()
    ss_real = _real(SS_FOLDERS)
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        if _is_inside(root, SS_FOLDERS):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dp = Path(dirpath)
            if _is_inside(dp, SS_FOLDERS) or _is_inside(dp, REPO_ROOT):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in DELIVERABLE_EXTS:
                    continue
                if not _looks_like_deliverable(fn.lower()):
                    continue
                p = dp / fn
                rp = _real(p)
                # Already in SS Folders by resolved path.
                try:
                    rp.relative_to(ss_real)
                    continue
                except Exception:
                    pass
                fp = _fingerprint(p)
                if fp is not None and fp in ss_index:
                    continue
                if rp in seen:
                    continue
                seen.add(rp)
                yield p


def _move_candidate(candidate: Path, idx: dict, audit_only: bool) -> tuple[str, Optional[Path]]:
    """Move one candidate into SS Folders. Returns (status, destination or None).

    Status values are the same as in ss-folders-guard.py: moved, duplicate,
    would-move (audit), copy-only (original could not be removed) and error.
    """
    dest_dir = _route_for(candidate, idx)
    if dest_dir is None:
        # A deliverable with no known owner: quarantine it inside SS Folders
        # with a timestamp prefix.
        dest = QUARANTINE_DIR / f"{int(time.time())}__{candidate.name}"
    else:
        dest = dest_dir / candidate.name

    if audit_only:
        return ("would-move", dest)

    # The destination already holds the same bytes: the stray is a duplicate.
    if dest.exists() and _same_content(candidate, dest):
        if _soft_delete(candidate):
            return ("duplicate", dest)
        return ("error", None)

    # The destination holds a different file. Never overwrite: park the
    # stray in quarantine with a timestamp so the operator can compare them.
    if dest.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = QUARANTINE_DIR / f"{dest.parent.name}__{candidate.name}__conflict-{ts}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Copy first, then delete the original, so the data is always in at
    # least one place.
    try:
        shutil.copy2(str(candidate), str(dest))
        if _soft_delete(candidate):
            return ("moved", dest)
        return ("copy-only", dest)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to copy %s -> %s: %s", candidate, dest, e)
        return ("error", None)


# ---------------------------------------------------------------------------
# Run a single pass
# ---------------------------------------------------------------------------

def run_pass(*, audit_only: bool = False) -> dict:
    """Find and move every candidate once, save state, notify, and return a summary.

    With audit_only, nothing is moved and the state file is not updated.
    """
    if not SS_FOLDERS.exists():
        log.error("SS_FOLDERS does not exist: %s, skipping this pass", SS_FOLDERS)
        return {"error": f"missing: {SS_FOLDERS}"}

    idx = _build_index()
    state = _load_state()
    known = set(state.get("known_strays", []))

    found: list[str] = []
    moved: list[tuple[str, str]] = []
    duplicates: list[str] = []
    quarantined: list[tuple[str, str]] = []
    copy_only: list[tuple[str, str]] = []
    errors: list[str] = []
    new_strays: list[str] = []

    candidates = list(_iter_candidates(idx))

    for c in candidates:
        s = str(c)
        found.append(s)
        if s not in known:
            new_strays.append(s)
        status, dest = _move_candidate(c, idx, audit_only=audit_only)
        if status == "moved" and dest is not None:
            moved.append((s, str(dest)))
            log.warning("Moved deliverable %s -> %s", s, dest)
            if dest.parent.name == "deliverables" and "_guard_quarantine" in str(dest):
                quarantined.append((s, str(dest)))
        elif status == "duplicate" and dest is not None:
            duplicates.append(s)
            log.info("Duplicate of existing SS-Folders file removed: %s", s)
        elif status == "copy-only" and dest is not None:
            copy_only.append((s, str(dest)))
            log.warning(
                "Copied %s -> %s (source could not be removed; left as soft-marker)",
                s, dest,
            )
        elif status == "would-move":
            log.info("AUDIT candidate (would route to %s): %s", dest, s)
        elif status == "error":
            errors.append(s)

    if not audit_only:
        cleared = {src for src, _ in moved} | set(duplicates)
        leftover = [s for s in found if s not in cleared]
        state["known_strays"] = leftover
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)

    summary = {
        "ss_folders": str(SS_FOLDERS),
        "scanned_clients": len(idx["clients"]),
        "scanned_suppliers": len(idx["suppliers"]),
        "found": len(found),
        "moved": len(moved),
        "duplicates": len(duplicates),
        "quarantined": len(quarantined),
        "copy_only": len(copy_only),
        "errors": len(errors),
        "new_strays": new_strays,
        "moved_pairs": moved,
        "audit_only": audit_only,
    }

    if not audit_only and (moved or errors or copy_only):
        lines = [f"SS Deliverables Guard pass at {datetime.now().strftime('%H:%M:%S')}"]
        for src, dest in moved:
            try:
                rel = Path(dest).relative_to(SS_FOLDERS)
            except Exception:
                rel = dest
            lines.append(f"moved: {Path(src).name}")
            lines.append(f"   -> {rel}")
        for src, dest in copy_only:
            lines.append(f"copy-only (source not deletable): {Path(src).name}")
        if duplicates:
            lines.append(f"duplicates removed: {len(duplicates)}")
        if errors:
            lines.append(f"errors: {len(errors)}")
        _telegram_send("\n".join(lines))
    elif audit_only and found:
        lines = [f"SS Deliverables Guard AUDIT: {len(found)} candidate(s)"]
        for s in found[:10]:
            lines.append(f"  {s}")
        _telegram_send("\n".join(lines))

    log.info(
        "pass done: found=%d moved=%d errors=%d audit=%s",
        len(found), len(moved), len(errors), audit_only,
    )
    return summary


# ---------------------------------------------------------------------------
# Daemon
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
        "ss-deliverables-guard starting: root=%s interval=%ss scan=%s",
        SS_FOLDERS, SCAN_INTERVAL,
        ", ".join(str(p) for p in SCAN_ROOTS if p.exists()),
    )
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
        except Exception as e:  # noqa: BLE001
            log.exception("pass failed: %s", e)
    log.info("ss-deliverables-guard stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--once", action="store_true", help="run one pass and exit")
    grp.add_argument("--audit", action="store_true", help="report-only, do not move")
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
