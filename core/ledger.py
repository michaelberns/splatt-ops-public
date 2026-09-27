"""
Write ledger: an append-only record of every database write the system claims to have made.

What it does
    Each time code creates, updates, upserts or deletes a record through
    the ops code path, one JSON line (a "claim") is appended to
    `state/writes.jsonl` in the repo. A claim says which run made it,
    what action, which collection and record id, and which fields were
    set:

        {"run_id": "3f9a1c2e", "at": "2026-08-08T09:15:00+00:00",
         "action": "update", "collection": "jobs", "record_id": "rec000000000001",
         "fields": ["status"], "source": "daemon.sync", "note": ""}

How it is used
    1. `RecordingClient` wraps the normal `PocketBaseClient`. Its create,
       update, upsert and delete call the real client and then append a
       claim, so recording happens automatically and a caller cannot
       forget it.
    2. Every process tags its claims with the run id from
       `core.logging_setup.RUN_ID`, the same id that appears on its log
       lines.
    3. At the end of a run the validator reads the claims for that run id
       and re-reads each record from PocketBase (or Todoist, for
       `TODOIST_COLLECTION` claims). If a claimed record is missing, a
       claimed delete is still present, or a claimed field is empty, the
       `writes_landed` rule in config/validation-rules.yaml fails.

Why a separate file
    The code that does the work should not be the only thing that
    confirms the work. `core/pb.py` already checks each write as it
    happens; the ledger lets an independent checker (the validator)
    confirm the same writes later, from a file that survives a crash and
    can be audited after the run.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .logging_setup import RUN_ID

REPO_ROOT = Path(__file__).resolve().parent.parent

# The one ledger file, relative to the repo root. Writers and the
# validator both get it from ledger_path(), and no other module spells the
# file name (tests/test_ledger_wiring.py enforces that). If a writer and
# the reader pointed at different files, the validator would see no claims
# for the run and pass it without checking anything.
LEDGER_REL = Path("state") / "writes.jsonl"

# The collection name used for a claim whose record lives in Todoist
# rather than PocketBase. The overdue engine (daemon/overdue.py) writes
# these when it moves or reschedules a task.
#
# Writer and reader must spell it identically, and a mismatch raises no
# error: the validator would look the name up in PocketBase, find no such
# collection and report every Todoist claim as a missing record. So both
# sides import this constant rather than typing the string.
TODOIST_COLLECTION = "todoist_task"


def ledger_path(conf=None):
    """Where the ledger lives: `<repo>/state/writes.jsonl`.

    Pass the settings object if you have one. Its `repo_root` moves the
    whole ledger with the repo, but no caller can choose a different file.
    """
    root = getattr(conf, "repo_root", None) or REPO_ROOT
    return Path(root) / LEDGER_REL


def _now():
    return datetime.now(timezone.utc).isoformat()


class WriteLedger:
    """Appends claims to, and reads claims from, one JSON-lines file.

    Appends are serialised with a lock so threads in one process do not
    interleave lines.
    """

    def __init__(self, path, run_id=None):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or RUN_ID
        self._lock = threading.Lock()

    def record(self, action, collection, record_id, fields_claimed, source="", note=""):
        """Append one claim and return it as a dict.

        `action` is create, update, upsert or delete. `fields_claimed` is
        the list of field names the caller says it set; those are the
        fields the validator re-reads. `source` defaults to the
        SPLATT_SOURCE environment variable when not given.
        """
        entry = {
            "run_id": self.run_id,
            "at": _now(),
            "action": action,
            "collection": collection,
            "record_id": record_id,
            "fields": sorted(fields_claimed or []),
            "source": source or os.environ.get("SPLATT_SOURCE", ""),
            "note": note,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return entry

    def entries(self, run_id=None):
        """The claims for one run, oldest first.

        Defaults to this ledger's own run id, which is what end-of-run
        validation wants. Pass "*" for every run. Lines that are not valid
        JSON are skipped.
        """
        if not self.path.exists():
            return []
        wanted = run_id or self.run_id
        out = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if wanted in (None, "*") or entry.get("run_id") == wanted:
                out.append(entry)
        return out

    def all_entries(self):
        return self.entries(run_id="*")

    def prune(self, keep_lines=20000):
        """Trim the file to its newest `keep_lines` lines so it cannot grow
        without limit. Returns how many lines were dropped."""
        if not self.path.exists():
            return 0
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= keep_lines:
            return 0
        dropped = len(lines) - keep_lines
        self.path.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
        return dropped


class RecordingClient:
    """A `PocketBaseClient` wrapper that records every write in the ledger.

    Writes go to the real client first. The claim is appended only after
    the call returns, which (because the real client verifies writes)
    means only after PocketBase has stored the values. Reads and any
    other attribute are passed straight through to the wrapped client.

    Recording lives in the wrapper rather than in each caller, so any code
    that writes through this client is auditable without having to
    remember to log anything.
    """

    def __init__(self, client, ledger, source=""):
        self.client = client
        self.ledger = ledger
        self.source = source

    def __getattr__(self, name):
        return getattr(self.client, name)

    def create(self, collection, payload):
        result = self.client.create(collection, payload)
        self.ledger.record(
            "create", collection, result.get("id", ""), list(payload.keys()), self.source
        )
        return result

    def update(self, collection, record_id, payload):
        result = self.client.update(collection, record_id, payload)
        self.ledger.record(
            "update", collection, record_id, list(payload.keys()), self.source
        )
        return result

    def upsert(self, collection, match_field, value, payload):
        result = self.client.upsert(collection, match_field, value, payload)
        # The match field is set on a create even when it is not in the
        # payload, so it is claimed too.
        fields = list(payload.keys())
        if match_field not in fields:
            fields.append(match_field)
        self.ledger.record(
            "upsert", collection, result.get("id", ""), fields, self.source
        )
        return result

    def delete(self, collection, record_id):
        result = self.client.delete(collection, record_id)
        self.ledger.record("delete", collection, record_id, [], self.source)
        return result
