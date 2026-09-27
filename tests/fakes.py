"""
In-memory stand-ins for PocketBase and Todoist, so the validator and the
engines can be tested without a server.

The fakes are intentionally simple: they store what they are given and
hand it back. Any logic added to a fake is logic the real client may not
have, and a test would then be checking the fake instead of the code.
"""

from __future__ import annotations


#: Matches the only filter shape the code under test passes to find_one:
#: a single  field='value'  or  field="value"  term. Anything richer would
#: turn the fake into a query engine of its own.
_ONE_TERM = __import__("re").compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"](.*)['\"]\s*$"
)


class FakePB:
    """A dict of collections that answers like PocketBaseClient.

    `data` maps a collection name to a list of record dicts. Reads return
    them; create, update and delete change them and are also logged in
    `writes` as (action, collection, id, payload), so a test can assert on
    exactly what an engine wrote (the trigger engine, for example, creates
    assignments and interactions and updates jobs).

    No schema check, no read-back and no filtering beyond one equality
    term: those belong to the real client and are tested there.
    """

    def __init__(self, data=None, collections=None):
        self.data = data or {}
        self._collections = collections or []
        self.writes = []
        self._next_id = 0

    def list_all(self, collection, filter_str="", sort=""):
        return list(self.data.get(collection, []))

    def collections(self):
        return list(self._collections)

    def health(self):
        return True

    def auth_admin(self):
        return True

    def close(self):
        pass

    def field_names(self, collection):
        names = set()
        for record in self.data.get(collection, []):
            names.update(record)
        return sorted(names)

    def get(self, collection, record_id):
        for record in self.data.get(collection, []):
            if record.get("id") == record_id:
                return record
        return None

    def find_one(self, collection, filter_str):
        match = _ONE_TERM.match(str(filter_str or ""))
        if not match:
            return None
        field, value = match.group(1), match.group(2)
        for record in self.data.get(collection, []):
            if str(record.get(field) or "") == value:
                return record
        return None

    # writes
    def create(self, collection, payload):
        self._next_id += 1
        record = dict(payload)
        record.setdefault("id", "fake%d" % self._next_id)
        self.data.setdefault(collection, []).append(record)
        self.writes.append(("create", collection, record["id"], dict(payload)))
        return record

    def update(self, collection, record_id, payload):
        record = self.get(collection, record_id)
        if record is None:
            raise KeyError("no %s record %s" % (collection, record_id))
        record.update(payload)
        self.writes.append(("update", collection, record_id, dict(payload)))
        return record

    def delete(self, collection, record_id):
        rows = self.data.get(collection, [])
        self.data[collection] = [r for r in rows if r.get("id") != record_id]
        self.writes.append(("delete", collection, record_id, {}))
        return True


#: The seven sections of the board, with stand-in ids. The overdue engine
#: refuses to run when a section it needs is not configured, so every
#: engine test needs all seven.
SECTIONS = {
    "today": "sec_today",
    "overdue": "sec_overdue",
    "upcoming": "sec_upcoming",
    "waiting_client": "sec_waiting_client",
    "waiting_supplier": "sec_waiting_supplier",
    "stalled": "sec_stalled",
    "backlog": "sec_backlog",
}


class FakeTodoist:
    """A task list that records moves, reschedules, completions, new tasks
    and comments instead of calling Todoist."""

    def __init__(self, tasks=None, sections=None):
        self.sections = dict(SECTIONS if sections is None else sections)
        self._tasks = tasks or []
        self.moved = []
        self.rescheduled = []
        self.completed = []
        self.added = []
        self.comments = []

    def tasks(self, project_id=None, section_id=None):
        return list(self._tasks)

    def task_exists(self, task_id):
        return any(str(t.get("id")) == str(task_id) for t in self._tasks)

    def add_task(self, content, **kwargs):
        task = {"id": "6new%s" % len(self.added), "content": content}
        task.update(kwargs)
        self.added.append(task)
        self._tasks.append(task)
        return task

    def update_task(self, task_id, **fields):
        self.rescheduled.append((task_id, fields))
        return {"id": task_id}

    def reschedule(self, task_id, due_string):
        return self.update_task(task_id, due_string=due_string)

    def move_to_section(self, task_id, section):
        self.moved.append((task_id, section))
        return True

    def section_name_for(self, section_id):
        """Same lookup as the real client, so validator checks can ask which
        section a task is in without a network."""
        for name, value in self.sections.items():
            if str(value) == str(section_id):
                return name
        return None

    def add_comment(self, task_id, content):
        self.comments.append((task_id, content))
        return True

    def complete_task(self, task_id):
        self.completed.append(task_id)
        return True

    def close(self):
        pass


def collection(name, fields):
    """A minimal collection definition, as `PocketBaseClient.collections()`
    returns it."""
    return {"name": name, "fields": [{"name": f} for f in fields]}
