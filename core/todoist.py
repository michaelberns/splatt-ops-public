"""
Todoist client for the unified API v1 (`https://api.todoist.com/api/v1`).

What it does
    Reads and writes tasks, sections, comments and projects in the one
    Todoist project that serves as "the board": the operator's task list,
    split into seven sections (today, overdue, upcoming, waiting_client,
    waiting_supplier, stalled, backlog). The project id and section ids
    come from `config/settings.yaml`; nothing is hardcoded.

API details handled here
    - The older REST v2 endpoints answer 410 Gone, so every call uses
      `/api/v1/`. tests/test_todoist_api_version.py checks that no caller
      in the repo still uses v2.
    - v1 lists are paginated with a cursor. `_paged` follows `next_cursor`
      to the end, and reads the list from `results` or `items`, because
      different endpoints use different keys.
    - A task's creation time is `added_at` (v2 used `created_at`), and a
      completed task has `checked: true`.
    - Errors raise `TodoistError` with the response body attached, so the
      reason Todoist gave is kept.

What it does not do
    Policy. Deciding what to do with an overdue task lives in
    daemon/overdue.py. This module only talks to the API.
"""

from __future__ import annotations


from datetime import date, datetime, timezone

import httpx

API = "https://api.todoist.com/api/v1"


class TodoistError(Exception):
    """A Todoist call failed. Carries the body so the reason survives."""

    def __init__(self, method, url, status, body):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        super().__init__("%s %s -> %s %s" % (method, url, status, str(body)[:400]))


class TodoistClient:
    """Todoist API client bound to one project.

    `sections` maps section names used in code ("today", "overdue", ...)
    to Todoist section ids. Methods that take a `section` accept either a
    name from that map or a raw id.
    """

    def __init__(self, token, project_id="", sections=None, timeout=15.0):
        self.token = token
        self.project_id = project_id
        self.sections = sections or {}
        self.http = httpx.Client(timeout=timeout)

    def _headers(self):
        return {"Authorization": "Bearer %s" % self.token}

    def _request(self, method, path, payload=None, params=None):
        url = "%s%s" % (API, path)
        resp = self.http.request(
            method, url, headers=self._headers(), json=payload, params=params
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:500]
            raise TodoistError(method, url, resp.status_code, body)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # Paged responses do not use one key for the list: /tasks and
    # /sections use "results", the completed-task endpoints use "items".
    # Reading only one key would return an empty list with a 200 for the
    # others, which looks like a valid "nothing found" to every caller.
    PAGE_KEYS = ("results", "items")

    def _paged(self, path, params=None, limit=200):
        """GET every page of a list endpoint and return all items.

        Follows `next_cursor` until it is empty. A bare JSON list is
        accepted as a single page. A response with neither known list key
        contributes nothing rather than raising.
        """
        params = dict(params or {})
        params["limit"] = limit
        items = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", path, params=params)
            if isinstance(data, list):
                items.extend(data)
                break
            for key in self.PAGE_KEYS:
                if key in data:
                    items.extend(data.get(key) or [])
                    break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return items

    # reads
    def tasks(self, project_id=None, section_id=None):
        """Open tasks in the project (or one section of it). Completed
        tasks are filtered out."""
        params = {"project_id": project_id or self.project_id}
        if section_id:
            params["section_id"] = self._section_id(section_id)
        return [t for t in self._paged("/tasks", params) if not t.get("checked")]

    def task(self, task_id):
        return self._request("GET", "/tasks/%s" % task_id)

    def task_exists(self, task_id):
        """True if the task exists and is not deleted.

        Used by the validator to confirm that an assignment still points at
        a real task. A 404 (or 400 for a malformed id) is treated as the
        answer "no", not as an error; other failures still raise.
        """
        try:
            data = self._request("GET", "/tasks/%s" % task_id)
            return not data.get("is_deleted", False)
        except TodoistError as exc:
            if exc.status in (404, 400):
                return False
            raise

    def sections_list(self, project_id=None):
        return self._paged("/sections", {"project_id": project_id or self.project_id})

    def project_name(self, project_id=None):
        """The project's display name, or "" if it cannot be read.

        This is what the sync writes into the `project_name` field of an
        assignment, so the field holds a readable name rather than an id.
        An empty answer is safe: the sync's difference check treats a blank
        name as "nothing to say" rather than as an instruction to erase.
        """
        try:
            data = self._request("GET", "/projects/%s" % (project_id or self.project_id))
        except TodoistError:
            return ""
        return (data or {}).get("name") or ""

    def completed_since(self, since_iso, until_iso=None):
        """Tasks in the project completed between `since_iso` and
        `until_iso` (default: now).

        The daily summary uses this to count work finished today, and so
        that tasks closed in Todoist but not yet marked done in PocketBase
        are not reported as still open.
        """
        params = {
            "since": since_iso,
            "until": until_iso or datetime.now(timezone.utc).isoformat(),
            "project_id": self.project_id,
        }
        return self._paged("/tasks/completed/by_completion_date", params, limit=100)

    # writes
    def add_task(self, content, description="", due_string="", priority=1,
                 section=None, labels=None, project_id=None):
        payload = {
            "content": content,
            "project_id": project_id or self.project_id,
            "priority": priority,
        }
        if description:
            payload["description"] = description
        if due_string:
            payload["due_string"] = due_string
        if labels:
            payload["labels"] = labels
        section_id = self._section_id(section)
        if section_id:
            payload["section_id"] = section_id
        return self._request("POST", "/tasks", payload=payload)

    def update_task(self, task_id, **fields):
        return self._request("POST", "/tasks/%s" % task_id, payload=fields)

    def reschedule(self, task_id, due_string):
        return self.update_task(task_id, due_string=due_string)

    def move_to_section(self, task_id, section):
        """Move a task to another section of the board.

        This uses the dedicated `/tasks/{id}/move` endpoint because the
        ordinary update call ignores `section_id`: it returns success and
        the task stays where it was.
        """
        section_id = self._section_id(section)
        if not section_id:
            raise TodoistError("POST", "/tasks/move", 0, "unknown section '%s'" % section)
        return self._request(
            "POST", "/tasks/%s/move" % task_id, payload={"section_id": str(section_id)}
        )

    def add_comment(self, task_id, content):
        """Add a comment to a task. Returns True if it was saved.

        The reverse pass (daemon/reverse.py) uses this to copy the reason a
        task was archived or completed in the dashboard onto the Todoist
        task itself, so the explanation is visible to anyone looking at
        Todoist. The trigger engine uses it the same way.

        Failure returns False rather than raising, because callers would
        rather close a task without a note than leave it open because the
        note failed.
        """
        if not task_id or not content:
            return False
        try:
            self._request("POST", "/comments",
                          payload={"task_id": str(task_id), "content": content})
            return True
        except TodoistError:
            return False

    def complete_task(self, task_id):
        self._request("POST", "/tasks/%s/close" % task_id)
        return True

    def reopen_task(self, task_id):
        self._request("POST", "/tasks/%s/reopen" % task_id)
        return True

    # helpers
    def _section_id(self, section):
        """A section id for a configured name, or the value itself if it is
        not a known name (so raw ids pass through)."""
        if not section:
            return None
        return self.sections.get(section, section)

    def section_name_for(self, section_id):
        """The configured name for a section id, or "" if the id is not in
        `sections`."""
        for name, value in self.sections.items():
            if str(value) == str(section_id):
                return name
        return ""

    @staticmethod
    def created_at(task):
        """A task's creation time. v1 calls it `added_at` and v2 called it
        `created_at`; both are accepted so a cached task of either shape
        works."""
        return (task or {}).get("added_at") or (task or {}).get("created_at")

    @staticmethod
    def days_overdue(task, today=None):
        """Days past the task's Todoist due date, or None if it has none.

        Todoist sends either a plain date (`2026-08-08`) or a datetime
        (`2026-08-08T09:00:00Z`); both are parsed here, because that is an
        API detail rather than overdue policy. A negative result means the
        task is due in the future.
        """
        due = (task or {}).get("due") or {}
        raw = due.get("date")
        if not raw:
            return None
        try:
            if len(raw) == 10:
                due_date = date.fromisoformat(raw)
            else:
                due_date = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None
        today = today or datetime.now(timezone.utc).date()
        return (today - due_date).days

    def close(self):
        self.http.close()
