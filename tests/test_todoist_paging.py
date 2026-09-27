"""
Tests for TodoistClient._paged, which reads every page of a list endpoint.

Todoist names the list "results" on /tasks and /sections and "items" on
the completed-task endpoints. Reading only one name returns an empty
list with a 200 for the others, which callers cannot tell apart from a
genuine "nothing found" (the daily summary's "finished today" count
would always be zero). These tests pin down that both names, a bare
list and cursor paging are all handled.
"""

from __future__ import annotations

from core.todoist import TodoistClient


class Recorded(TodoistClient):
    """A client that answers from a script instead of the network."""

    def __init__(self, pages):
        super().__init__("token", "project", {}, 1.0)
        self.pages = list(pages)
        self.asked = []

    def _request(self, method, path, payload=None, params=None):
        self.asked.append(dict(params or {}))
        return self.pages.pop(0)


def test_a_list_called_results_is_read():
    client = Recorded([{"results": [{"id": "1"}], "next_cursor": None}])

    assert client._paged("/tasks") == [{"id": "1"}]


def test_a_list_called_items_is_read_too():
    """The completed-task endpoints use "items"; it must be read too."""
    client = Recorded([{"items": [{"id": "1"}], "next_cursor": None}])

    assert client._paged("/tasks/completed/by_completion_date") == [{"id": "1"}]


def test_a_bare_list_is_read():
    client = Recorded([[{"id": "1"}]])

    assert client._paged("/sections") == [{"id": "1"}]


def test_the_cursor_is_followed_to_the_end():
    client = Recorded([
        {"results": [{"id": "1"}], "next_cursor": "abc"},
        {"results": [{"id": "2"}], "next_cursor": None},
    ])

    assert [r["id"] for r in client._paged("/tasks")] == ["1", "2"]
    assert client.asked[1]["cursor"] == "abc"


def test_an_answer_with_neither_name_comes_back_empty_rather_than_raising():
    """An unknown response shape yields an empty list rather than an
    exception, so one odd endpoint cannot stop a whole pass."""
    client = Recorded([{"something_else": [{"id": "1"}], "next_cursor": None}])

    assert client._paged("/tasks") == []


def test_completed_since_asks_for_the_window_and_the_project():
    client = Recorded([{"items": [], "next_cursor": None}])
    client.completed_since("2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z")

    asked = client.asked[0]
    assert asked["since"] == "2026-08-08T00:00:00Z"
    assert asked["until"] == "2026-08-09T00:00:00Z"
    assert asked["project_id"] == "project"
