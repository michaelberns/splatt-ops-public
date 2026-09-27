"""
Context: the read-only view of the data that one validation run works from.

What it holds
    - the PocketBase client, with every collection fetched once and cached
    - the Todoist client, with the open tasks fetched once and cached
    - the write ledger (core/ledger.py) for the run, if there is one
    - an index of every file under the client project folders
    - `notes`, lines the checks add for the report ("Todoist not configured")

Why cache
    A run evaluates dozens of rules against the same few collections.
    Fetching each collection once keeps a full run to a handful of API
    calls, and every rule sees the same snapshot of the data.

Read-only
    Nothing here writes. The validator makes no changes to PocketBase or
    Todoist; its only writes are local files made by the CLI (the report
    in reports/, bypasses and the Telegram heartbeat in state/).
"""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from pathlib import Path


def parse_time(value):
    """Parse a PocketBase or ISO timestamp into an aware datetime, or None.

    >>> parse_time("2026-08-08 01:02:03.000Z")
    datetime.datetime(2026, 8, 8, 1, 2, 3, tzinfo=datetime.timezone.utc)

    A value with no timezone is taken to be UTC.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        try:
            stamp = datetime.fromisoformat(text[:19])
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


class Context:
    """Cached access to PocketBase, Todoist, the ledger and the project folders.

    `project_root` is the folder that holds one sub-folder per client
    (`paths.projects` in config/settings.yaml). The engine also attaches
    `ruleset` and `gate_resolver` to the context before a run.
    """

    def __init__(self, pb, todoist=None, ledger=None, settings=None,
                 project_root=None, started_at=None, run_id=""):
        self.pb = pb
        self.todoist = todoist
        self.ledger = ledger
        self.settings = settings
        self.project_root = Path(project_root).expanduser() if project_root else None
        self.started_at = started_at or datetime.now(timezone.utc)
        self.run_id = run_id
        self._records = {}
        self._todoist_tasks = None
        self._live_collections = None
        self._file_index = None
        self.notes = []

    # PocketBase
    def records(self, collection):
        """Every record in a collection, cached, following pagination."""
        if collection not in self._records:
            self._records[collection] = self.pb.list_all(collection)
        return self._records[collection]

    def record(self, collection, record_id):
        for item in self.records(collection):
            if item.get("id") == record_id:
                return item
        return None

    def index(self, collection, field="id"):
        return {r.get(field): r for r in self.records(collection) if r.get(field)}

    def related(self, collection, field, value):
        """Records in a collection whose relation field points at value.
        Handles both single relations and multi-relation lists."""
        out = []
        for item in self.records(collection):
            held = item.get(field)
            if held == value:
                out.append(item)
            elif isinstance(held, list) and value in held:
                out.append(item)
        return out

    def live_collections(self):
        """The collection definitions on the server, for the schema drift check."""
        if self._live_collections is None:
            self._live_collections = self.pb.collections()
        return self._live_collections

    # Todoist
    def todoist_tasks(self):
        """Open tasks in the configured Todoist project, or [] with a note
        when Todoist is not configured."""
        if self._todoist_tasks is None:
            if not self.todoist:
                self._todoist_tasks = []
                self.notes.append("Todoist not configured, task checks were skipped")
            else:
                self._todoist_tasks = self.todoist.tasks()
        return self._todoist_tasks

    def todoist_task_ids(self):
        return {str(t.get("id")) for t in self.todoist_tasks()}

    # Files
    def project_files(self):
        """Flat index of every file under the projects folder, as
        (path, name, mtime) tuples. Built once, because walking the whole
        folder tree for each check would be slow."""
        if self._file_index is None:
            index = []
            if self.project_root and self.project_root.exists():
                for path in self.project_root.rglob("*"):
                    if path.is_file():
                        try:
                            mtime = path.stat().st_mtime
                        except OSError:
                            continue
                        index.append((path, path.name, mtime))
            else:
                self.notes.append(
                    "project folder not reachable, file checks were skipped"
                )
            self._file_index = index
        return self._file_index

    def find_files(self, patterns, within=None, modified_after=None):
        """Files whose name matches any glob pattern (case-insensitive),
        optionally under a given folder and optionally modified after a
        timestamp."""
        cutoff = None
        if modified_after:
            stamp = parse_time(modified_after)
            if stamp:
                cutoff = stamp.timestamp()
        subfolder = str(within).lower() if within else None
        hits = []
        for path, name, mtime in self.project_files():
            if subfolder and subfolder not in str(path).lower():
                continue
            if cutoff and mtime < cutoff:
                continue
            for pattern in patterns:
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.lower(), pattern.lower()):
                    hits.append(path)
                    break
        return hits

    @staticmethod
    def _folder_key(value):
        """Lower case with spaces, underscores and hyphens removed, so
        "Orchard Lane" and "orchard_lane" compare equal."""
        return str(value or "").lower().replace(" ", "").replace("_", "").replace("-", "")

    def folder_for(self, client_name, aliases=""):
        """The client's project folder, or None if no folder matches.

        Returning None is deliberate: a file check that fell back to
        searching the whole tree could pass on another client's paperwork.

        Matching order
            1. The registered name, then each alias in the order the client
               record lists them (comma separated).
            2. For each candidate, an exact match on the normalised name
               wins over a substring match. So a client registered as
               "Island Beverages (Fiji) PTE LTD" with the alias "IBF Fiji"
               finds the folder "IBF Fiji", and a folder that merely
               contains "ibffiji" in a longer name cannot win over it.
            3. Candidates shorter than four characters are ignored, because
               a two-letter alias is a substring of many folder names.
        """
        if not self.project_root or not self.project_root.exists():
            return None

        candidates = []
        for value in [client_name] + [
            part.strip() for part in str(aliases or "").split(",")
        ]:
            key = self._folder_key(value)
            if key and len(key) >= 4 and key not in candidates:
                candidates.append(key)
        if not candidates:
            return None

        folders = [path for path in self.project_root.iterdir() if path.is_dir()]
        keyed = [(self._folder_key(path.name), path) for path in folders]

        for wanted in candidates:
            for name, path in keyed:
                if name == wanted:
                    return path
            for name, path in keyed:
                if wanted in name or name in wanted:
                    return path
        return None

    def folder_for_client(self, client):
        """The project folder for a client record, aliases included."""
        client = client or {}
        return self.folder_for(client.get("name", ""), client.get("aliases", ""))

    def note(self, text):
        self.notes.append(str(text))
