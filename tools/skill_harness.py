#!/usr/bin/env python3
"""
Skill harness: keeps a record in PocketBase of the agent's skills, the
hard-coded references inside them, and which skills each kind of request
should load.

A skill is a folder under skills/ holding a SKILL.md (instructions for the
agent) and sometimes scripts. Skills name things that can go stale, such as
Todoist project and section ids. The harness finds those references, checks
them against the live service, and can block an operation whose skills
point at something that no longer exists.

Collections (created by pb_migrations/)
    skills             one row per skill: name, content hash, file count,
                       size, status, working and live paths
    skill_refs         references found in a skill's files, each with a
                       status of valid, broken, stale or unknown
    skill_routes       operation type -> skill ids, with regex trigger
                       patterns (seeded from ROUTE_SEED)
    skill_invocations  append-only log of skill loads and preflight checks

Usage
    python -m tools.skill_harness <command>

    bootstrap               check that the collections exist and sign-in works
    list                    print the skills registry
    status                  counts of skills, refs and routes by status
    register <skill>        upsert one skill record (content hash, frontmatter)
    sync-all                register every skill in skills/ (and in
                            SKILLS_READONLY_DIRS, if set)
    audit                   scan every skill for references, upsert skill_refs
    validate [skill]        check references against their live source
    sweep                   sync-all, audit and validate; for broken refs,
                            create Todoist tasks and send a Telegram summary
    preflight --operation OP | --phrase TEXT
                            before an operation, check the refs of every skill
                            in the matching route; exit 3 means block it
    routes load             write ROUTE_SEED into skill_routes
    routes lookup <phrase>  which routes, and so which skills, match a phrase
    resolve <skill>         print a skill's refs and their status
    invoke <skill> ...      log one skill invocation
    event <skill> ...       re-register a skill after its files changed (for a
                            file watcher; none ships with this repo)

What the reference check covers today
    `audit` recognises Todoist ids only: 16-character ids starting with 6
    (TODOIST_ID_RE). Each id is classified as a project, section or label
    from the words around it (score_todoist_kind). EMAIL_RE, PATH_RE and
    URL_RE are defined for other kinds of reference but are not used yet,
    so emails, file paths and URLs in a skill are not recorded.

    `validate` and `preflight` look each Todoist id up through the Todoist
    API. Without TODOIST_API_TOKEN nothing can be looked up, every ref comes
    back 'unknown', and 'unknown' never blocks an operation.

Configuration (the environment first, then the repo's .env)
    POCKETBASE_URL                      default http://127.0.0.1:8090
    PB_ADMIN_EMAIL, PB_ADMIN_PASSWORD   PocketBase superuser login
    TODOIST_API_TOKEN                   needed to validate Todoist refs
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  optional sweep and preflight alerts
    SKILLS_WORKING_DIR      default: skills/ in this repo
    SKILLS_LIVE_DIR         where Claude loads skills, default ~/.claude/skills
    SKILLS_READONLY_DIRS    optional, comma separated extra skill folders to
                            register read-only (for example plugin skills)
    SKILL_HARNESS_BUFFER    queue for writes made while PocketBase is down,
                            default ~/.claude/skill-harness-buffer.jsonl
    SKILL_HARNESS_TODOIST_PROJECT  project for tasks created by `sweep`
                            (default: the Todoist inbox)
    SPLATT_ENV_FILE         read this file instead of the repo's .env

Exit codes
    0  ok
    1  usage error, unknown skill, or no matching route
    2  PocketBase unreachable or sign-in failed (writes are buffered)
    3  broken or stale refs found: the caller should block the operation

docs/HARNESSES.md explains how this fits with the validator, the write
ledger and the playbooks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

PB_URL = os.environ.get("POCKETBASE_URL", "http://127.0.0.1:8090").rstrip("/")
PB_EMAIL = os.environ.get("PB_ADMIN_EMAIL", "")
PB_PASSWORD = os.environ.get("PB_ADMIN_PASSWORD", "")

HOME = Path.home()
REPO_ROOT = Path(__file__).resolve().parent.parent

# The skills the harness registers and audits: this repo's skills/ folder,
# so the instructions are checked in the same commit as the code they
# describe.
WORKING_DIR = Path(
    os.environ.get("SKILLS_WORKING_DIR", str(REPO_ROOT / "skills"))
)
# Where Claude loads skills from. bin/sync-skills copies WORKING_DIR here.
LIVE_DIR = Path(os.environ.get("SKILLS_LIVE_DIR", str(HOME / ".claude" / "skills")))
# Writes that fail while PocketBase is down are appended here and replayed
# by the next command that reaches PocketBase (see buffer_flush).
BUFFER_FILE = Path(
    os.environ.get(
        "SKILL_HARNESS_BUFFER", str(HOME / ".claude" / "skill-harness-buffer.jsonl")
    )
)

# Extra skill folders to register read-only, for example skills delivered
# by a plugin. They are recorded in the registry and audited, but never
# edited. Comma separated; empty (the default) means none.
READONLY_SKILL_DIRS = [
    Path(p)
    for p in os.environ.get("SKILLS_READONLY_DIRS", "").split(",")
    if p.strip()
]

# The .env to read credentials from: SPLATT_ENV_FILE when set, otherwise
# the repo's own .env. An override is the only candidate, so pointing it
# at a file that does not exist loads no env file at all (the tests rely
# on this). Values already in the environment always win over the file.
SPLATT_ENV_CANDIDATES: list[Path] = []
if os.environ.get("SPLATT_ENV_FILE"):
    SPLATT_ENV_CANDIDATES.append(Path(os.environ["SPLATT_ENV_FILE"]))
else:
    SPLATT_ENV_CANDIDATES.append(REPO_ROOT / ".env")


def _find_splatt_env() -> Path | None:
    """Return the first .env candidate that exists, or None."""
    for p in SPLATT_ENV_CANDIDATES:
        if p.exists():
            return p
    return None


SPLATT_ENV = _find_splatt_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")

# The Todoist project where `sweep` creates tasks for broken refs. Empty
# means the Todoist inbox.
SKILL_HARNESS_TODOIST_PROJECT = os.environ.get("SKILL_HARNESS_TODOIST_PROJECT", "")


def _load_env_file(path: Path) -> None:
    """Read the PocketBase, Telegram and Todoist credentials from a .env file.

    A value already set in the environment is kept; the file only fills in
    what is missing.
    """
    global PB_EMAIL, PB_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TODOIST_API_TOKEN
    if not path.exists():
        return
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == "PB_ADMIN_EMAIL" and not PB_EMAIL:
            PB_EMAIL = v
        elif k == "PB_ADMIN_PASSWORD" and not PB_PASSWORD:
            PB_PASSWORD = v
        elif k == "TELEGRAM_BOT_TOKEN" and not TELEGRAM_BOT_TOKEN:
            TELEGRAM_BOT_TOKEN = v
        elif k == "TELEGRAM_CHAT_ID" and not TELEGRAM_CHAT_ID:
            TELEGRAM_CHAT_ID = v
        elif k == "TODOIST_API_TOKEN" and not TODOIST_API_TOKEN:
            TODOIST_API_TOKEN = v


if SPLATT_ENV is not None:
    _load_env_file(SPLATT_ENV)

# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000Z")


def log(level: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    color = {"info": "\033[32m", "warn": "\033[33m", "err": "\033[31m", "dim": "\033[2m"}.get(
        level, ""
    )
    reset = "\033[0m"
    sys.stderr.write(f"\033[2m[{ts}]\033[0m {color}[harness]{reset} {msg}\n")


def telegram_send(text: str) -> None:
    """Best-effort Telegram notification. Never raises."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = parse.urlencode(
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            }
        ).encode()
        request.urlopen(url, data=data, timeout=5)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Skill scanning
# --------------------------------------------------------------------------


# Names that are never part of a skill, at any depth.
SKIP_NAMES = {".DS_Store", ".git", ".gitignore", "sync-backups", "sync.sh", "skill_harness.py"}


def iter_skill_dirs(root: Path) -> list[Path]:
    """Every folder directly under `root` that holds a SKILL.md, sorted."""
    if not root.exists():
        return []
    out: list[Path] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        if p.name.startswith(".") or p.name in SKIP_NAMES:
            continue
        if not (p / "SKILL.md").exists():
            continue
        out.append(p)
    return out


def iter_skill_files(skill: Path) -> list[Path]:
    """All tracked files under one skill directory."""
    out: list[Path] = []
    if not skill.exists():
        return out
    for f in sorted(skill.rglob("*")):
        if not f.is_file():
            continue
        if f.name in SKIP_NAMES:
            continue
        out.append(f)
    return out


def hash_skill(skill: Path) -> tuple[str, int, int]:
    """Return (sha256_hex, file_count, total_size_bytes) for a skill tree.

    The hash covers each file's relative path and contents, so renaming a
    file changes it as well as editing one.
    """
    h = hashlib.sha256()
    count = 0
    total = 0
    for f in iter_skill_files(skill):
        rel = f.relative_to(skill).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        data = f.read_bytes()
        h.update(data)
        count += 1
        total += len(data)
    return h.hexdigest(), count, total


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(skill: Path) -> dict[str, str]:
    """Extract YAML frontmatter from SKILL.md (just the top-level string keys)."""
    skill_md = skill / "SKILL.md"
    if not skill_md.exists():
        return {}
    text = skill_md.read_text(errors="replace")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        if line.startswith(" ") or line.startswith("\t"):
            continue  # skip nested keys
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# --------------------------------------------------------------------------
# Reference extraction
# --------------------------------------------------------------------------


# Todoist project, section and label ids are 16-character base62 strings,
# and the ones in these skills all start with 6. This is the only pattern
# scan_refs uses.
TODOIST_ID_RE = re.compile(r"\b(6[A-Za-z0-9]{15})\b")

# Defined for future kinds of reference; scan_refs does not use them yet.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PATH_RE = re.compile(r"(?<![\w./-])(~?/[A-Za-z0-9_./-]{3,})")
URL_RE = re.compile(r"https?://[^\s)\]}`'\"<>]+")


def score_todoist_kind(current_line: str, wider_ctx: str) -> dict[str, int]:
    """Return weighted votes for what kind of Todoist id this is.

    Strong signals (`Section:`, `projectId`, `(section)`) count only on the
    line where the id appears, so neighbouring rows of a Markdown table do
    not vote for each other. Weak signals (the plain words "section" or
    "project") also count, with less weight, anywhere in the three-line
    window around the id.

    >>> votes = score_todoist_kind("| Waiting | Section: 6Xsection0000004 |", "")
    >>> max(votes, key=votes.get)
    'todoist_section'
    """
    cur = current_line.lower()
    wide = wider_ctx.lower()
    scores = {"todoist_project": 0, "todoist_section": 0, "todoist_label": 0}

    # Strong section signals, same line only.
    if "section:" in cur or "sectionid" in cur or "section_id" in cur:
        scores["todoist_section"] += 10
    if "(section)" in cur:
        scores["todoist_section"] += 8

    # Strong project signals, same line only.
    if "projectid" in cur or "project_id" in cur or "project id" in cur:
        scores["todoist_project"] += 10
    if "(project)" in cur or "project ♒" in cur or "old project" in cur:
        scores["todoist_project"] += 8

    # Weak signals: "section" or "project" as a plain word. Lower weight,
    # so a strong signal always wins.
    if re.search(r"\bsection\b", cur):
        scores["todoist_section"] += 3
    elif re.search(r"\bsection\b", wide):
        scores["todoist_section"] += 1
    if re.search(r"\bproject\b", cur):
        scores["todoist_project"] += 3
    elif re.search(r"\bproject\b", wide):
        scores["todoist_project"] += 1

    # Labels
    if "labelid" in cur or "label_id" in cur or re.search(r"\blabel:\b", cur):
        scores["todoist_label"] += 10

    return scores


def nearby_label(context: str, id_: str) -> str:
    """A readable label for an id: its line with the id and Markdown table
    and list markup removed, at most 180 characters."""
    line = context.strip()
    # Strip markdown pipe-table leading markers
    line = line.strip("|").strip()
    # Remove the ID itself
    line = line.replace(f"`{id_}`", "").replace(id_, "").strip()
    # Trim obvious markdown noise
    line = re.sub(r"^[-*#]+\s*", "", line)
    line = re.sub(r"\s{2,}", " ", line)
    # Cap length
    return line[:180]


def scan_refs(skill: Path) -> list[dict[str, Any]]:
    """Find the hard-coded Todoist ids in every text file of a skill.

    Each id is recorded once per skill, however often it appears. Its kind
    (project, section or label) is the one with the most votes summed over
    every occurrence, and its label is the longest one seen. Returns dicts
    ready to become skill_refs records, all with status "unknown" until
    validated.
    """
    # id -> {"scores": {kind: votes}, "label": str, "files": [relative paths]}
    agg: dict[str, dict[str, Any]] = {}

    text_exts = {".md", ".py", ".sh", ".txt", ".json", ".yaml", ".yml", ""}
    for f in iter_skill_files(skill):
        if f.suffix.lower() not in text_exts:
            continue
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            for m in TODOIST_ID_RE.finditer(line):
                id_ = m.group(1)
                # Context window: previous line, this line, next line.
                ctx_parts = []
                if lineno >= 2:
                    ctx_parts.append(lines[lineno - 2])
                ctx_parts.append(line)
                if lineno < len(lines):
                    ctx_parts.append(lines[lineno])
                ctx = " ".join(ctx_parts)

                entry = agg.setdefault(
                    id_,
                    {
                        "scores": {"todoist_project": 0, "todoist_section": 0, "todoist_label": 0},
                        "label": "",
                        "files": [],
                    },
                )
                votes = score_todoist_kind(line, ctx)
                for k, v in votes.items():
                    entry["scores"][k] += v

                # Keep the longest (most informative) label seen.
                candidate = nearby_label(line, id_)
                if candidate and len(candidate) > len(entry["label"]):
                    entry["label"] = candidate

                rel = str(f.relative_to(skill))
                if rel not in entry["files"]:
                    entry["files"].append(rel)

    # Resolve winning kind per ID.
    out: list[dict[str, Any]] = []
    for id_, entry in agg.items():
        scores = entry["scores"]
        best_kind, best_score = max(scores.items(), key=lambda kv: kv[1])
        if best_score == 0:
            # No signal at all: default to project, the most common kind of
            # bare id in these skills.
            best_kind = "todoist_project"
        out.append(
            {
                "ref_key": f"todoist_{best_kind.split('_')[1]}_{id_}",
                "ref_type": best_kind,
                "current_value": id_,
                "label": entry["label"][:200],
                "status": "unknown",
                "source_files": entry["files"],
            }
        )
    return out


# --------------------------------------------------------------------------
# PocketBase client
# --------------------------------------------------------------------------


class PBError(Exception):
    pass


class PB:
    """A minimal PocketBase REST client for the four skill collections.

    Uses urllib only, so the harness has no dependencies outside the
    standard library. Every error is raised as PBError.
    """

    def __init__(self) -> None:
        self.token: str | None = None

    def _req(
        self,
        method: str,
        path: str,
        data: Any = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{PB_URL}{path}"
        if params:
            url += "?" + parse.urlencode(params)
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = self.token
        body: bytes | None = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=body, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=8) as r:
                raw = r.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise PBError(f"HTTP {e.code} {method} {path}: {err_body}") from e
        except error.URLError as e:
            raise PBError(f"URLError {method} {path}: {e.reason}") from e

    def auth(self) -> None:
        if not PB_EMAIL or not PB_PASSWORD:
            # Name the files searched as well as the variables, because the
            # usual cause is a missing or misplaced .env, not missing values.
            looked = "\n    ".join(str(p) for p in SPLATT_ENV_CANDIDATES)
            loaded = str(SPLATT_ENV) if SPLATT_ENV else "none of them existed"
            raise PBError(
                "PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD not set.\n"
                f"  env file used: {loaded}\n"
                f"  looked in:\n    {looked}"
            )
        resp = self._req(
            "POST",
            "/api/collections/_superusers/auth-with-password",
            data={"identity": PB_EMAIL, "password": PB_PASSWORD},
        )
        self.token = resp.get("token")
        if not self.token:
            raise PBError("PB auth did not return a token")

    # skills

    def find_skill(self, name: str) -> dict[str, Any] | None:
        filt = parse.quote(f'name="{name}"')
        resp = self._req(
            "GET", f"/api/collections/skills/records", params={"filter": f'name="{name}"', "perPage": "1"}
        )
        items = resp.get("items", [])
        return items[0] if items else None

    def list_skills(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._req(
                "GET",
                "/api/collections/skills/records",
                params={"perPage": "200", "page": str(page), "sort": "name"},
            )
            items = resp.get("items", [])
            out.extend(items)
            if len(items) < 200:
                break
            page += 1
        return out

    def upsert_skill(self, record: dict[str, Any]) -> dict[str, Any]:
        existing = self.find_skill(record["name"])
        if existing:
            return self._req(
                "PATCH", f"/api/collections/skills/records/{existing['id']}", data=record
            )
        return self._req("POST", "/api/collections/skills/records", data=record)

    # skill_refs

    def list_refs(self, skill_id: str | None = None) -> list[dict[str, Any]]:
        params = {"perPage": "500"}
        if skill_id:
            params["filter"] = f'skill="{skill_id}"'
        resp = self._req("GET", "/api/collections/skill_refs/records", params=params)
        return resp.get("items", [])

    def find_ref(self, skill_id: str, ref_key: str) -> dict[str, Any] | None:
        resp = self._req(
            "GET",
            "/api/collections/skill_refs/records",
            params={"filter": f'skill="{skill_id}" && ref_key="{ref_key}"', "perPage": "1"},
        )
        items = resp.get("items", [])
        return items[0] if items else None

    def upsert_ref(self, record: dict[str, Any]) -> dict[str, Any]:
        existing = self.find_ref(record["skill"], record["ref_key"])
        if existing:
            return self._req(
                "PATCH", f"/api/collections/skill_refs/records/{existing['id']}", data=record
            )
        return self._req("POST", "/api/collections/skill_refs/records", data=record)

    def delete_ref(self, ref_id: str) -> None:
        self._req("DELETE", f"/api/collections/skill_refs/records/{ref_id}")

    # skill_routes

    def list_routes(self) -> list[dict[str, Any]]:
        resp = self._req(
            "GET",
            "/api/collections/skill_routes/records",
            params={"perPage": "200", "sort": "priority,operation_type"},
        )
        return resp.get("items", [])

    def find_route(self, operation_type: str) -> dict[str, Any] | None:
        resp = self._req(
            "GET",
            "/api/collections/skill_routes/records",
            params={"filter": f'operation_type="{operation_type}"', "perPage": "1"},
        )
        items = resp.get("items", [])
        return items[0] if items else None

    def upsert_route(self, record: dict[str, Any]) -> dict[str, Any]:
        existing = self.find_route(record["operation_type"])
        if existing:
            return self._req(
                "PATCH",
                f"/api/collections/skill_routes/records/{existing['id']}",
                data=record,
            )
        return self._req("POST", "/api/collections/skill_routes/records", data=record)

    # skill_invocations

    def log_invocation(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/api/collections/skill_invocations/records", data=record)


# --------------------------------------------------------------------------
# Buffer for writes while PocketBase is down
# --------------------------------------------------------------------------


def buffer_append(item: dict[str, Any]) -> None:
    """Queue one write ({"kind": ..., "payload": ...}) for later."""
    BUFFER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BUFFER_FILE.open("a") as f:
        f.write(json.dumps(item) + "\n")


def buffer_flush(pb: PB) -> int:
    """Replay queued writes in order. Stops at the first failure and keeps
    it and everything after it for next time. Returns the number sent."""
    if not BUFFER_FILE.exists():
        return 0
    lines = BUFFER_FILE.read_text().splitlines()
    remaining: list[str] = []
    flushed = 0
    for idx, line in enumerate(lines):
        try:
            item = json.loads(line)
        except Exception:
            continue
        kind = item.get("kind")
        payload = item.get("payload", {})
        try:
            if kind == "skill":
                pb.upsert_skill(payload)
            elif kind == "ref":
                pb.upsert_ref(payload)
            elif kind == "invocation":
                pb.log_invocation(payload)
            elif kind == "route":
                pb.upsert_route(payload)
            flushed += 1
        except Exception:
            remaining.extend(lines[idx:])
            break
    if remaining:
        BUFFER_FILE.write_text("\n".join(remaining) + "\n")
    else:
        try:
            BUFFER_FILE.unlink()
        except FileNotFoundError:
            pass
    return flushed


# --------------------------------------------------------------------------
# Skill record builder
# --------------------------------------------------------------------------


# The category recorded for each skill. A skill not listed is "other", and
# a read-only skill gets ":readonly" appended.
CATEGORY_MAP = {
    "splatt-ss-agent": "splatt",
    "splatt-todoist": "splatt",
    "splatt-email": "splatt",
    "splatt-clients": "splatt",
    "splatt-suppliers": "splatt",
    "splatt-project-files": "splatt",
}


def build_skill_record(
    skill_dir: Path, status: str = "unknown", editable: bool = True
) -> dict[str, Any]:
    """The skills record for one skill folder, ready to upsert."""
    name = skill_dir.name
    fm = parse_frontmatter(skill_dir)
    content_hash, file_count, size_bytes = hash_skill(skill_dir)
    description = fm.get("description", "")[:4000]
    category = CATEGORY_MAP.get(name, "other")
    if not editable:
        category = f"{category}:readonly"
    return {
        "name": name,
        "description": description,
        "category": category,
        "working_path": str(skill_dir),
        "live_path": str(LIVE_DIR / name) if editable else str(skill_dir),
        "content_hash": content_hash,
        "file_count": file_count,
        "size_bytes": size_bytes,
        "status": status,
        "triggers": [],  # populated by audit from frontmatter keywords
        "dependencies": [],
        "last_synced_at": now_iso(),
    }


def all_skill_sources() -> list[tuple[Path, bool]]:
    """Every skill folder the harness tracks, as (skill_dir, editable).

    Skills in WORKING_DIR are editable. Skills in READONLY_SKILL_DIRS are
    tracked but never edited, and a read-only skill with the same name as
    a working one is skipped, so nothing is registered twice.
    """
    out: list[tuple[Path, bool]] = []
    for d in iter_skill_dirs(WORKING_DIR):
        out.append((d, True))
    seen = {d[0].name for d in out}
    for ro_root in READONLY_SKILL_DIRS:
        for d in iter_skill_dirs(ro_root):
            # The working copy wins over a read-only one of the same name.
            if d.name in seen:
                continue
            out.append((d, False))
            seen.add(d.name)
    return out


# --------------------------------------------------------------------------
# Route seed
# --------------------------------------------------------------------------
#
# The dispatch table that `routes load` writes into skill_routes. Skills are
# listed by name here and resolved to record ids when loaded, so each named
# skill must already be registered (sync-all). Every name must be a folder
# in skills/ (tests/test_skill_harness.py checks this).
#
# trigger_patterns are case-insensitive regular expressions matched against
# the operator's request; when several routes match, the highest priority
# wins. est_context_bytes is a rough size of the skills the route loads.

ROUTE_SEED = [
    {
        "operation_type": "ops",
        "trigger_patterns": [
            "^ops$", "run ops", "^operations$", "do ops", "go ops", "operate",
            "full ops", "ops pass", "ops run", "run operations",
        ],
        "skill_names": [
            "splatt-ss-agent",
            "splatt-todoist",
            "splatt-email",
            "splatt-clients",
            "splatt-suppliers",
            "splatt-project-files",
        ],
        "priority": 100,
        "notes": "Full operations pass. Loads the core splatt-* skills.",
        "est_context_bytes": 110000,
        "enabled": True,
    },
    {
        "operation_type": "morning",
        "trigger_patterns": [
            "^morning$", "morning check", "morning commands", "rundown",
            "run down", "what'?s on", "what do i need", "daily review",
            "overview", "catch me up", "what did i miss",
        ],
        "skill_names": [
            "splatt-morning-commands",
            "splatt-todoist",
            "splatt-email",
        ],
        "priority": 90,
        "notes": "Morning sequence: open tasks first, then sent mail and inbox. Lighter than ops.",
        "est_context_bytes": 25000,
        "enabled": True,
    },
    {
        "operation_type": "email",
        "trigger_patterns": [
            "draft email", "write email", "compose email", "send email",
            "reply to", "respond to",
        ],
        "skill_names": ["splatt-email"],
        "priority": 80,
        "notes": "Email drafting. Add clients/suppliers only if a name is mentioned.",
        "est_context_bytes": 6000,
        "enabled": True,
    },
    {
        "operation_type": "client_check",
        "trigger_patterns": [
            "what'?s happening with", "check on client", "client status",
            "tell me about.*client",
        ],
        "skill_names": ["splatt-clients", "splatt-todoist"],
        "priority": 70,
        "notes": "Client lookup. Optional: splatt-suppliers.",
        "est_context_bytes": 28000,
        "enabled": True,
    },
    {
        "operation_type": "supplier_check",
        "trigger_patterns": [
            "who supplies", "find supplier", "source.*part", "supplier for",
        ],
        "skill_names": ["splatt-suppliers"],
        "priority": 70,
        "notes": "Supplier lookup only.",
        "est_context_bytes": 11000,
        "enabled": True,
    },
    {
        "operation_type": "invoice",
        "trigger_patterns": [
            "create invoice", "send invoice", "bill the client", "xero invoice",
        ],
        "skill_names": ["splatt-ss-agent", "splatt-clients"],
        "priority": 75,
        "notes": "Invoice flow. Uses the SS agent for Xero and clients for contact info.",
        "est_context_bytes": 33000,
        "enabled": True,
    },
]


# --------------------------------------------------------------------------
# Todoist validator
# --------------------------------------------------------------------------


class TodoistNotFound(Exception):
    """Raised when a Todoist resource does not exist (404, or 400 with
    INVALID_ARGUMENT_VALUE)."""


class Todoist:
    """Existence checks against the Todoist API v1, plus creating a task.

    Only used when TODOIST_API_TOKEN is set.
    """

    BASE = "https://api.todoist.com/api/v1"

    def __init__(self, token: str) -> None:
        self.token = token

    def _get(self, path: str) -> Any:
        if not self.token:
            raise RuntimeError("TODOIST_API_TOKEN not set")
        req = request.Request(
            f"{self.BASE}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode())
        except error.HTTPError as e:
            # API v1 answers 400 with INVALID_ARGUMENT_VALUE, rather than
            # 404, for an id that does not exist or is malformed. Both mean
            # "not found".
            if e.code == 404:
                raise TodoistNotFound(path) from None
            if e.code == 400:
                try:
                    body = json.loads(e.read().decode())
                except Exception:
                    body = {}
                tag = (body or {}).get("error_tag") or ""
                if tag == "INVALID_ARGUMENT_VALUE":
                    raise TodoistNotFound(path) from None
            raise

    def _post(self, path: str, data: dict) -> Any:
        if not self.token:
            raise RuntimeError("TODOIST_API_TOKEN not set")
        body = json.dumps(data).encode()
        req = request.Request(
            f"{self.BASE}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())

    def project_exists(self, project_id: str) -> bool:
        try:
            self._get(f"/projects/{project_id}")
            return True
        except TodoistNotFound:
            return False

    def section_exists(self, section_id: str) -> bool:
        try:
            self._get(f"/sections/{section_id}")
            return True
        except TodoistNotFound:
            return False

    def label_exists(self, label_id: str) -> bool:
        try:
            self._get(f"/labels/{label_id}")
            return True
        except TodoistNotFound:
            return False

    def create_task(self, content: str, project_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"content": content}
        if project_id:
            payload["project_id"] = project_id
        return self._post("/tasks", payload)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_bootstrap(pb: PB, args: argparse.Namespace) -> int:
    """Sign in and check that the four skill collections exist."""
    try:
        pb.auth()
    except PBError as e:
        print(f"auth failed: {e}", file=sys.stderr)
        return 2
    for c in ("skills", "skill_refs", "skill_routes", "skill_invocations"):
        try:
            pb._req("GET", f"/api/collections/{c}/records", params={"perPage": "1"})
            log("info", f"collection ok: {c}")
        except PBError as e:
            log("err", f"collection missing: {c} ({e})")
            return 2
    print("bootstrap ok")
    return 0


def cmd_list(pb: PB, args: argparse.Namespace) -> int:
    pb.auth()
    skills = pb.list_skills()
    if not skills:
        print("(no skills registered: run `python -m tools.skill_harness sync-all`)")
        return 0
    print(f"{'name':<28} {'status':<10} {'cat':<10} {'files':>6} {'size':>10} {'hash':<12} validated")
    for s in skills:
        hsh = (s.get("content_hash") or "")[:10]
        print(
            f"{s['name']:<28} {s.get('status',''):<10} {s.get('category',''):<10} "
            f"{s.get('file_count',0):>6} {s.get('size_bytes',0):>10} {hsh:<12} "
            f"{s.get('last_validated_at','') or '-'}"
        )
    return 0


def cmd_status(pb: PB, args: argparse.Namespace) -> int:
    pb.auth()
    skills = pb.list_skills()
    refs = pb.list_refs()
    routes = pb.list_routes()
    total = len(skills)
    by_status: dict[str, int] = {}
    for s in skills:
        by_status[s.get("status", "unknown")] = by_status.get(s.get("status", "unknown"), 0) + 1
    ref_by_status: dict[str, int] = {}
    for r in refs:
        ref_by_status[r.get("status", "unknown")] = ref_by_status.get(r.get("status", "unknown"), 0) + 1
    print("skill harness status")
    print(f"skills:  {total} total, {dict(by_status)}")
    print(f"refs:    {len(refs)} total, {dict(ref_by_status)}")
    print(f"routes:  {len(routes)} total")
    print(f"PB:      {PB_URL}")
    print(f"working: {WORKING_DIR}")
    print(f"live:    {LIVE_DIR}")
    return 0


def cmd_register(pb: PB, args: argparse.Namespace) -> int:
    """Upsert the record for one skill in WORKING_DIR."""
    pb.auth()
    buffer_flush(pb)
    skill_dir = WORKING_DIR / args.skill
    if not skill_dir.exists():
        log("err", f"no such skill: {args.skill}")
        return 1
    rec = build_skill_record(skill_dir, status="unknown")
    try:
        res = pb.upsert_skill(rec)
        log("info", f"registered {rec['name']} ({rec['file_count']} files, hash {rec['content_hash'][:10]})")
        print(res["id"])
        return 0
    except PBError as e:
        log("warn", f"buffered {rec['name']} ({e})")
        buffer_append({"kind": "skill", "payload": rec})
        return 2


def cmd_sync_all(pb: PB, args: argparse.Namespace) -> int:
    """Upsert a record for every skill (working and read-only). Returns 2
    if any write had to be buffered."""
    pb.auth()
    buffer_flush(pb)
    sources = all_skill_sources()
    if not sources:
        log("warn", f"no skills found in {WORKING_DIR} or read-only dirs")
        return 0
    ok = 0
    failed = 0
    editable_count = sum(1 for _, e in sources if e)
    readonly_count = len(sources) - editable_count
    log("info", f"found {editable_count} editable + {readonly_count} read-only skills")
    for skill_dir, editable in sources:
        rec = build_skill_record(skill_dir, status="unknown", editable=editable)
        try:
            pb.upsert_skill(rec)
            tag = "editable" if editable else "read-only"
            log("info", f"✔ {rec['name']:<28} ({tag})")
            ok += 1
        except PBError as e:
            log("err", f"✖ {rec['name']}: {e}")
            buffer_append({"kind": "skill", "payload": rec})
            failed += 1
    log("info", f"sync-all: {ok} ok, {failed} buffered")
    return 0 if failed == 0 else 2


def cmd_audit(pb: PB, args: argparse.Namespace) -> int:
    """Scan every skill for Todoist ids and upsert them as skill_refs.

    Afterwards, an id recorded with different kinds in different skills is
    made consistent (see the reconciliation step below).
    """
    pb.auth()
    buffer_flush(pb)
    sources = all_skill_sources()
    total_refs = 0
    for s, editable in sources:
        skill_rec = pb.find_skill(s.name)
        if not skill_rec:
            # Register the skill first if it is not in the registry yet.
            skill_rec = pb.upsert_skill(
                build_skill_record(s, status="unknown", editable=editable)
            )
        skill_id = skill_rec["id"]
        refs = scan_refs(s)
        tag = "" if editable else " [read-only]"
        log("info", f"{s.name}{tag}: found {len(refs)} refs")

        for r in refs:
            rec = {
                "skill": skill_id,
                "ref_key": r["ref_key"],
                "ref_type": r["ref_type"],
                "current_value": r["current_value"],
                "label": r["label"],
                "status": "unknown",
            }
            try:
                pb.upsert_ref(rec)
                log("dim", f"    ↳ {r['ref_type']:<18} {r['ref_key'][:60]}")
                total_refs += 1
            except PBError as e:
                log("warn", f"    ↳ buffered {r['ref_key']}: {e}")
                buffer_append({"kind": "ref", "payload": rec})
    log("info", f"audit complete: {total_refs} refs saved across {len(sources)} skills")

    # Cross-skill reconciliation. When the same id is classified differently
    # in different skills, every record takes the most specific kind:
    # section, then label, then project. Project is the default when a skill
    # has no evidence, so strong evidence in one skill overrides it.
    log("info", "reconciling ref types across skills")
    all_refs = pb.list_refs()
    by_value: dict[str, list[dict[str, Any]]] = {}
    for r in all_refs:
        val = r.get("current_value", "")
        if not val:
            continue
        by_value.setdefault(val, []).append(r)
    type_rank = {"todoist_section": 3, "todoist_label": 2, "todoist_project": 1}
    reconciled = 0
    for val, group in by_value.items():
        if len(group) < 2:
            continue
        # Pick the highest-ranked type in the group.
        best = max(group, key=lambda r: type_rank.get(r.get("ref_type", ""), 0))
        target_type = best.get("ref_type")
        target_key = best.get("ref_key")
        for r in group:
            if r.get("ref_type") != target_type:
                try:
                    # Delete and re-create rather than patch, because the
                    # ref_key (which includes the kind) is part of the
                    # unique index.
                    pb.delete_ref(r["id"])
                    pb.upsert_ref(
                        {
                            "skill": r["skill"],
                            "ref_key": target_key,
                            "ref_type": target_type,
                            "current_value": val,
                            "label": r.get("label") or best.get("label", ""),
                            "status": "unknown",
                        }
                    )
                    reconciled += 1
                    log(
                        "dim",
                        f"    ↻ {val} promoted to {target_type} in skill {r['skill'][:8]}",
                    )
                except PBError as e:
                    log("warn", f"reconcile failed for {val}: {e}")
    if reconciled:
        log("info", f"reconciled {reconciled} mis-typed ref records")
    return 0


def cmd_validate(pb: PB, args: argparse.Namespace) -> int:
    """Check every ref (or one skill's refs) against its live source and
    save the result on the ref. Returns 3 if any ref is stale or broken.

    Todoist refs need TODOIST_API_TOKEN; without it they are marked
    'unknown'. A file_path ref is checked on disk. A url ref, and any other
    kind, is marked 'unknown'. An error while checking marks the ref
    'stale'.
    """
    pb.auth()
    buffer_flush(pb)
    # All refs, or one skill's.
    if args.skill:
        skill_rec = pb.find_skill(args.skill)
        if not skill_rec:
            log("err", f"skill not registered: {args.skill}")
            return 1
        refs = pb.list_refs(skill_id=skill_rec["id"])
    else:
        refs = pb.list_refs()
    if not refs:
        log("warn", "no refs to validate (run audit first)")
        return 0
    todo = Todoist(TODOIST_API_TOKEN) if TODOIST_API_TOKEN else None
    stale: list[dict] = []
    for ref in refs:
        kind = ref.get("ref_type")
        val = ref.get("current_value", "")
        status = "valid"
        last_err = ""
        try:
            if kind == "todoist_project":
                if not todo:
                    status = "unknown"
                elif not todo.project_exists(val):
                    status = "broken"
                    last_err = "Todoist project not found"
            elif kind == "todoist_section":
                if not todo:
                    status = "unknown"
                elif not todo.section_exists(val):
                    status = "broken"
                    last_err = "Todoist section not found"
            elif kind == "todoist_label":
                if not todo:
                    status = "unknown"
                elif not todo.label_exists(val):
                    status = "broken"
                    last_err = "Todoist label not found"
            elif kind == "file_path":
                p = Path(os.path.expanduser(val))
                if not p.exists():
                    status = "broken"
                    last_err = "path does not exist"
            elif kind == "url":
                status = "unknown"  # URLs are not fetched
            else:
                status = "unknown"
        except Exception as e:
            status = "stale"
            last_err = str(e)[:500]
        # Save the result on the ref.
        patch = {
            "status": status,
            "last_validated_at": now_iso(),
            "last_error": last_err,
        }
        try:
            pb._req("PATCH", f"/api/collections/skill_refs/records/{ref['id']}", data=patch)
        except PBError as e:
            log("warn", f"could not patch ref {ref['id']}: {e}")
        if status in ("stale", "broken"):
            stale.append({**ref, **patch})
            log("err", f"✖ {ref.get('ref_key')} → {status} ({last_err})")
        else:
            log("dim", f"✔ {ref.get('ref_key')} → {status}")
    log("info", f"validate: {len(refs)} checked, {len(stale)} stale/broken")
    if stale:
        return 3
    return 0


def cmd_sweep(pb: PB, args: argparse.Namespace) -> int:
    """Full health check: sync-all, audit and validate.

    When refs are stale or broken, a Todoist task is created for each (if a
    Todoist token is set), a Telegram summary is sent, each skill's status
    is set to broken, stale or healthy, and the exit code is 3. Otherwise
    every skill is marked healthy.
    """
    telegram_send("*skill-harness*: starting health sweep")
    # 1. re-register
    rc = cmd_sync_all(pb, argparse.Namespace())
    if rc not in (0, 2):
        return rc
    # 2. audit
    cmd_audit(pb, argparse.Namespace())
    # 3. validate
    vrc = cmd_validate(pb, argparse.Namespace(skill=None))
    if vrc == 3:
        # Stale or broken refs: create a Todoist task for each.
        refs = pb.list_refs()
        stale = [r for r in refs if r.get("status") in ("stale", "broken")]
        todo = Todoist(TODOIST_API_TOKEN) if TODOIST_API_TOKEN else None
        created = 0
        if todo and stale:
            for r in stale:
                skill_name = "unknown"
                try:
                    skill_rec = pb._req(
                        "GET", f"/api/collections/skills/records/{r['skill']}"
                    )
                    skill_name = skill_rec.get("name", "unknown")
                except Exception:
                    pass
                content = (
                    f"Skill harness: stale ref in {skill_name}: "
                    f"{r.get('ref_type')} `{r.get('current_value')}` "
                    f"({r.get('label','')}). Error: {r.get('last_error','')}"
                )
                try:
                    todo.create_task(
                        content[:500],
                        project_id=SKILL_HARNESS_TODOIST_PROJECT or None,
                    )
                    created += 1
                except Exception as e:
                    log("warn", f"could not create Todoist task: {e}")
        telegram_send(
            f"*skill-harness*: sweep found {len(stale)} stale refs, "
            f"created {created} Todoist tasks"
        )
        # Set each skill's status from its worst ref.
        broken_skill_ids = {r["skill"] for r in stale if r.get("status") == "broken"}
        stale_skill_ids = {r["skill"] for r in stale}
        skills_all = pb.list_skills()
        for s in skills_all:
            if s["id"] in broken_skill_ids:
                new_status = "broken"
            elif s["id"] in stale_skill_ids:
                new_status = "stale"
            else:
                new_status = "healthy"
            try:
                pb._req(
                    "PATCH",
                    f"/api/collections/skills/records/{s['id']}",
                    data={"status": new_status, "last_validated_at": now_iso()},
                )
            except PBError as e:
                log("warn", f"could not update skill status {s['name']}: {e}")
        return 3
    # Nothing stale or broken: every skill is healthy.
    skills_all = pb.list_skills()
    for s in skills_all:
        try:
            pb._req(
                "PATCH",
                f"/api/collections/skills/records/{s['id']}",
                data={"status": "healthy", "last_validated_at": now_iso()},
            )
        except PBError:
            pass
    telegram_send("*skill-harness*: sweep complete, all refs healthy")
    log("info", "sweep complete, all refs healthy")
    return 0


def cmd_preflight(pb: PB, args: argparse.Namespace) -> int:
    """A gate run before an operation: are the refs its skills rely on valid?

    1. Find the route, by operation type or by matching a phrase against
       the routes' trigger patterns (highest priority wins).
    2. Check every ref of every skill in that route, as cmd_validate does,
       saving any status that changed.
    3. Log a skill_invocations row ("pending", or "blocked") so every
       preflight is on record.
    4. Print a JSON report and a short summary; on a block, also send a
       Telegram alert naming the broken refs.

    Exit codes
      0  proceed: no broken refs ('unknown' refs do not block)
      3  block: at least one broken ref, listed for the operator
      2  proceed with a warning: PocketBase was unreachable, or checking
         raised errors and no ref was confirmed valid
      1  no route matched
    """
    try:
        pb.auth()
    except PBError as e:
        # PocketBase is down, so nothing can be checked. This fails open
        # (exit 2, proceed with a warning) rather than stopping every
        # operation whenever the database is unavailable.
        log("warn", f"preflight: PocketBase unreachable ({e}), cannot validate, proceeding")
        print(
            json.dumps(
                {
                    "operation": args.operation,
                    "phrase": args.phrase,
                    "result": "warn",
                    "reason": "pocketbase_unreachable",
                    "message": f"PocketBase is down ({e}). Proceeding without validation.",
                },
                indent=2,
            )
        )
        return 2

    buffer_flush(pb)

    # 1. Resolve the route from an operation type or a phrase.
    route: dict[str, Any] | None = None
    if args.operation:
        route = pb.find_route(args.operation)
    elif args.phrase:
        phrase = args.phrase.lower()
        matches: list[tuple[int, dict[str, Any]]] = []
        for r in pb.list_routes():
            if not r.get("enabled"):
                continue
            for pattern in r.get("trigger_patterns") or []:
                try:
                    if re.search(pattern, phrase, re.IGNORECASE):
                        matches.append((r.get("priority", 0), r))
                        break
                except re.error:
                    if pattern.lower() in phrase:
                        matches.append((r.get("priority", 0), r))
                        break
        matches.sort(key=lambda x: -x[0])
        if matches:
            route = matches[0][1]

    if not route:
        log("err", f"preflight: no route for operation={args.operation!r} phrase={args.phrase!r}")
        print(
            json.dumps(
                {
                    "operation": args.operation,
                    "phrase": args.phrase,
                    "result": "no_route",
                    "message": "No skill_routes entry matched. Run `python -m tools.skill_harness routes load` first or pass a known operation type.",
                },
                indent=2,
            )
        )
        return 1

    # 2. The route's skills, by name, and all of their refs.
    skills_by_id = {s["id"]: s for s in pb.list_skills()}
    skill_ids: list[str] = list(route.get("skill_ids") or [])
    skill_names = [skills_by_id.get(sid, {}).get("name", sid) for sid in skill_ids]

    all_refs: list[dict[str, Any]] = []
    for sid in skill_ids:
        all_refs.extend(pb.list_refs(skill_id=sid))

    # 3. Check every ref, as cmd_validate does, but only write to the
    #    database when a ref's status or error changed.
    todo = Todoist(TODOIST_API_TOKEN) if TODOIST_API_TOKEN else None
    warnings: list[str] = []
    broken: list[dict[str, Any]] = []
    ok: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for ref in all_refs:
        kind = ref.get("ref_type", "")
        val = ref.get("current_value", "")
        entry = {
            "skill": skills_by_id.get(ref.get("skill", ""), {}).get("name", "?"),
            "ref_key": ref.get("ref_key"),
            "ref_type": kind,
            "current_value": val,
            "label": ref.get("label", ""),
        }
        new_status = "valid"
        err = ""
        try:
            if kind == "todoist_project":
                if not todo:
                    new_status = "unknown"
                elif not todo.project_exists(val):
                    new_status = "broken"
                    err = "Todoist project not found"
            elif kind == "todoist_section":
                if not todo:
                    new_status = "unknown"
                elif not todo.section_exists(val):
                    new_status = "broken"
                    err = "Todoist section not found"
            elif kind == "todoist_label":
                if not todo:
                    new_status = "unknown"
                elif not todo.label_exists(val):
                    new_status = "broken"
                    err = "Todoist label not found"
            elif kind == "file_path":
                p = Path(os.path.expanduser(val))
                if not p.exists():
                    new_status = "broken"
                    err = "path does not exist"
            else:
                new_status = "unknown"
        except Exception as e:
            new_status = "unknown"
            err = str(e)[:200]
            warnings.append(f"{entry['ref_key']}: {err}")

        entry["status"] = new_status
        entry["error"] = err

        # Save the status only if it changed. Best effort.
        if new_status != ref.get("status") or err != ref.get("last_error", ""):
            try:
                pb._req(
                    "PATCH",
                    f"/api/collections/skill_refs/records/{ref['id']}",
                    data={
                        "status": new_status,
                        "last_validated_at": now_iso(),
                        "last_error": err,
                    },
                )
            except PBError:
                pass  # the preflight result does not depend on this write

        if new_status == "valid":
            ok.append(entry)
        elif new_status == "broken":
            broken.append(entry)
        else:
            unknown.append(entry)

    # 4. The outcome.
    if broken:
        result = "blocked"
        exit_code = 3
    elif warnings and not ok:
        # Checking failed and nothing was confirmed valid: warn.
        result = "warn"
        exit_code = 2
    else:
        result = "ok"
        exit_code = 0

    # 5. Log a skill_invocations row for every preflight, against the
    #    route's first skill.
    inv_payload: dict[str, Any] = {
        "skill_name": skill_names[0] if skill_names else (args.operation or ""),
        "trigger_phrase": args.phrase or "",
        "operation_type": route.get("operation_type", args.operation or ""),
        "result": "pending" if result == "ok" else ("blocked" if result == "blocked" else "pending"),
        "context_bytes_used": route.get("est_context_bytes", 0) or 0,
        "notes": f"preflight: {len(ok)} ok, {len(broken)} broken, {len(unknown)} unknown"
        + (f"; broken refs: {', '.join(b['ref_key'] for b in broken)}" if broken else ""),
    }
    if skill_ids:
        inv_payload["skill"] = skill_ids[0]
    try:
        pb.log_invocation(inv_payload)
    except PBError as e:
        buffer_append({"kind": "invocation", "payload": inv_payload})
        log("warn", f"preflight: invocation buffered ({e})")

    # 6. The JSON report on stdout, for the agent.
    report = {
        "operation": route.get("operation_type"),
        "phrase": args.phrase,
        "result": result,
        "est_context_bytes": route.get("est_context_bytes"),
        "skills": skill_names,
        "refs": {
            "ok": len(ok),
            "broken": len(broken),
            "unknown": len(unknown),
            "total": len(all_refs),
        },
        "broken_refs": broken,
        "unknown_refs": unknown if warnings else [],
    }
    print(json.dumps(report, indent=2))

    # 7. A short summary on stderr, for a person.
    if result == "ok":
        log("info", f"preflight ok: {route['operation_type']} ({len(ok)}/{len(all_refs)} refs valid)")
    elif result == "blocked":
        log(
            "err",
            f"preflight BLOCKED: {route['operation_type']}: {len(broken)} broken ref(s)",
        )
        for b in broken:
            log("err", f"  ✖ {b['skill']}/{b['ref_key']}: {b['error']}")
        telegram_send(
            "*skill-harness*: preflight BLOCKED\n"
            f"operation: `{route['operation_type']}`\n"
            f"broken: {len(broken)}\n"
            + "\n".join(f"- `{b['skill']}/{b['ref_key']}`: {b['error']}" for b in broken[:5])
        )
    else:
        log("warn", f"preflight WARN: {route['operation_type']}: could not validate all refs")

    return exit_code


def cmd_routes_load(pb: PB, args: argparse.Namespace) -> int:
    """Upsert every ROUTE_SEED entry into skill_routes. A skill name that is
    not registered is left out of the route and noted in its notes."""
    pb.auth()
    buffer_flush(pb)
    # Resolve skill names to record ids.
    name_to_id: dict[str, str] = {}
    for s in pb.list_skills():
        name_to_id[s["name"]] = s["id"]
    loaded = 0
    skipped = 0
    for route in ROUTE_SEED:
        resolved: list[str] = []
        missing: list[str] = []
        for name in route["skill_names"]:
            if name in name_to_id:
                resolved.append(name_to_id[name])
            else:
                missing.append(name)
        rec = {
            "operation_type": route["operation_type"],
            "trigger_patterns": route["trigger_patterns"],
            "skill_ids": resolved,
            "priority": route["priority"],
            "notes": route["notes"] + (f" (missing: {', '.join(missing)})" if missing else ""),
            "est_context_bytes": route["est_context_bytes"],
            "enabled": route["enabled"],
            "context_condition": "",
        }
        try:
            pb.upsert_route(rec)
            log(
                "info",
                f"route {route['operation_type']}: {len(resolved)} skills "
                + (f"({len(missing)} missing)" if missing else ""),
            )
            loaded += 1
        except PBError as e:
            log("err", f"route {route['operation_type']} failed: {e}")
            skipped += 1
    log("info", f"routes load: {loaded} upserted, {skipped} failed")
    return 0 if skipped == 0 else 2


def cmd_routes_lookup(pb: PB, args: argparse.Namespace) -> int:
    """Print, as JSON, every enabled route whose trigger patterns match the
    phrase, highest priority first. A pattern that is not a valid regular
    expression is matched as plain text."""
    pb.auth()
    phrase = args.phrase.lower()
    routes = pb.list_routes()
    matches: list[tuple[int, dict]] = []
    for route in routes:
        if not route.get("enabled"):
            continue
        for pattern in route.get("trigger_patterns") or []:
            try:
                if re.search(pattern, phrase, re.IGNORECASE):
                    matches.append((route.get("priority", 0), route))
                    break
            except re.error:
                # Not a valid regex: match it as plain text.
                if pattern.lower() in phrase:
                    matches.append((route.get("priority", 0), route))
                    break
    matches.sort(key=lambda x: -x[0])
    if not matches:
        print(json.dumps({"phrase": args.phrase, "matches": []}))
        return 0
    name_by_id: dict[str, str] = {s["id"]: s["name"] for s in pb.list_skills()}
    out = {
        "phrase": args.phrase,
        "matches": [
            {
                "operation_type": m["operation_type"],
                "priority": m.get("priority", 0),
                "skills": [name_by_id.get(sid, sid) for sid in (m.get("skill_ids") or [])],
                "est_context_bytes": m.get("est_context_bytes"),
                "notes": m.get("notes", ""),
            }
            for _, m in matches
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_resolve(pb: PB, args: argparse.Namespace) -> int:
    """Print one skill's refs and their status as JSON."""
    pb.auth()
    skill = pb.find_skill(args.skill)
    if not skill:
        log("err", f"skill not registered: {args.skill}")
        return 1
    refs = pb.list_refs(skill_id=skill["id"])
    out = {
        "skill": args.skill,
        "status": skill.get("status"),
        "refs": [
            {
                "ref_key": r["ref_key"],
                "ref_type": r["ref_type"],
                "current_value": r["current_value"],
                "label": r.get("label"),
                "status": r.get("status"),
            }
            for r in refs
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_invoke(pb: PB, args: argparse.Namespace) -> int:
    """Log one skill invocation and update the skill's last_invoked_at.
    Buffered for later if PocketBase is unreachable."""
    try:
        pb.auth()
    except PBError as e:
        buffer_append(
            {
                "kind": "invocation",
                "payload": {
                    "skill_name": args.skill,
                    "trigger_phrase": args.phrase,
                    "operation_type": args.operation or "",
                    "result": args.result,
                    "context_bytes_used": args.bytes_used or 0,
                    "notes": args.notes or "",
                },
            }
        )
        log("warn", f"buffered invocation for {args.skill} ({e})")
        return 2
    buffer_flush(pb)
    skill_rec = pb.find_skill(args.skill)
    rec = {
        "skill_name": args.skill,
        "trigger_phrase": args.phrase,
        "operation_type": args.operation or "",
        "result": args.result,
        "context_bytes_used": args.bytes_used or 0,
        "notes": args.notes or "",
    }
    if skill_rec:
        rec["skill"] = skill_rec["id"]
    try:
        pb.log_invocation(rec)

        if skill_rec:
            pb._req(
                "PATCH",
                f"/api/collections/skills/records/{skill_rec['id']}",
                data={"last_invoked_at": now_iso()},
            )
        log("info", f"logged invocation {args.skill} → {args.result}")
        return 0
    except PBError as e:
        buffer_append({"kind": "invocation", "payload": rec})
        log("warn", f"buffered invocation for {args.skill}: {e}")
        return 2


def cmd_event(pb: PB, args: argparse.Namespace) -> int:
    """Re-register a skill after its files changed.

    Meant for a file watcher to call on every create, update or delete in a
    skill folder (none ships with this repo). A skill whose folder is gone
    is marked "deleted". Buffered for later if PocketBase is unreachable.
    """
    try:
        pb.auth()
    except PBError as e:
        log("warn", f"PB unreachable, buffering sync event for {args.skill}")
        buffer_append(
            {
                "kind": "skill",
                "payload": {
                    "name": args.skill,
                    "last_synced_at": now_iso(),
                    "status": "unknown",
                },
            }
        )
        return 2
    buffer_flush(pb)
    skill_dir = WORKING_DIR / args.skill
    if not skill_dir.exists():
        # The folder is gone: mark the record deleted.
        existing = pb.find_skill(args.skill)
        if existing:
            pb._req(
                "PATCH",
                f"/api/collections/skills/records/{existing['id']}",
                data={"status": "deleted", "last_synced_at": now_iso()},
            )
            log("info", f"marked deleted: {args.skill}")
        return 0
    rec = build_skill_record(skill_dir, status="unknown")
    try:
        pb.upsert_skill(rec)
        log("info", f"event {args.action}: {args.skill}")
        return 0
    except PBError as e:
        log("warn", f"event for {args.skill} failed: {e}, buffering")
        buffer_append({"kind": "skill", "payload": rec})
        return 2


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    """Parse the command line and run one command. PBError from any command
    becomes exit code 2."""
    parser = argparse.ArgumentParser(prog="skill_harness.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bootstrap")
    sub.add_parser("list")
    sub.add_parser("status")

    p_reg = sub.add_parser("register")
    p_reg.add_argument("skill")

    sub.add_parser("sync-all")
    sub.add_parser("audit")

    p_val = sub.add_parser("validate")
    p_val.add_argument("skill", nargs="?", default=None)

    sub.add_parser("sweep")

    p_rt = sub.add_parser("routes")
    rt_sub = p_rt.add_subparsers(dest="routes_cmd", required=True)
    rt_sub.add_parser("load")
    p_rt_lookup = rt_sub.add_parser("lookup")
    p_rt_lookup.add_argument("phrase")

    p_res = sub.add_parser("resolve")
    p_res.add_argument("skill")

    p_inv = sub.add_parser("invoke")
    p_inv.add_argument("skill")
    p_inv.add_argument("--phrase", default="")
    p_inv.add_argument("--operation", default="")
    p_inv.add_argument("--result", default="success")
    p_inv.add_argument("--bytes-used", type=int, default=0, dest="bytes_used")
    p_inv.add_argument("--notes", default="")

    p_evt = sub.add_parser("event")
    p_evt.add_argument("skill")
    p_evt.add_argument("--action", default="update")

    p_pre = sub.add_parser(
        "preflight",
        help="check a route's refs before an operation; exit 3 means block",
    )
    p_pre.add_argument(
        "--operation",
        default="",
        help="operation type (for example ops, morning, email, client_check)",
    )
    p_pre.add_argument(
        "--phrase",
        default="",
        help="the request as typed, matched against the routes' trigger patterns",
    )

    args = parser.parse_args()

    pb = PB()
    dispatch = {
        "bootstrap": cmd_bootstrap,
        "list": cmd_list,
        "status": cmd_status,
        "register": cmd_register,
        "sync-all": cmd_sync_all,
        "audit": cmd_audit,
        "validate": cmd_validate,
        "sweep": cmd_sweep,
        "resolve": cmd_resolve,
        "invoke": cmd_invoke,
        "event": cmd_event,
        "preflight": cmd_preflight,
    }
    if args.cmd == "routes":
        if args.routes_cmd == "load":
            return cmd_routes_load(pb, args)
        if args.routes_cmd == "lookup":
            return cmd_routes_lookup(pb, args)
        return 1
    fn = dispatch.get(args.cmd)
    if not fn:
        print(f"unknown command: {args.cmd}", file=sys.stderr)
        return 1
    try:
        return fn(pb, args)
    except PBError as e:
        log("err", f"PB error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
