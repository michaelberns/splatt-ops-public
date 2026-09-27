"""
Tests for PocketBaseClient's token renewal and for `field_names`.

Token renewal: on a 401 or 403 the client logs in again and repeats the
call exactly once. Long-running processes (the daemon, the MCP server)
outlive their admin token, and without renewal their writes would start
failing with an error that looks like a credentials problem. The tests
pin down both halves: the retry happens, and it happens only for 401/403,
only once, and never for the login call itself (a 400 names a bad field
and must reach the caller unchanged).

`field_names`: the set of fields a collection has, read from the
collection definition rather than from its records.

All HTTP traffic goes through FakeHTTP, a queue of canned responses.
"""

from __future__ import annotations

import pytest

from core.pb import PocketBaseClient, PocketBaseError


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._payload


class FakeHTTP:
    """Answers with a queued list of responses and records what was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def request(self, method, url, headers=None, json=None, params=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "json": json})
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def client_with(responses):
    pb = PocketBaseClient("http://pb.test", admin_email="a@b.c",
                          admin_password="secret", enforce_schema=False,
                          verify_writes=False)
    pb.http = FakeHTTP(responses)
    pb.token = "old-token"
    return pb


def test_expired_token_is_renewed_and_the_call_retried():
    pb = client_with([
        FakeResponse(401, {"message": "token is expired"}),
        FakeResponse(200, {"token": "fresh-token"}),
        FakeResponse(200, {"id": "abc"}),
    ])

    record = pb.create("interactions", {"summary": "hello"})

    assert record == {"id": "abc"}
    assert pb.token == "fresh-token"

    paths = [call["url"] for call in pb.http.calls]
    assert "auth-with-password" in paths[1]
    # The retry carries the new token, not the expired one.
    assert pb.http.calls[2]["headers"]["Authorization"] == "fresh-token"
    assert pb.http.calls[2]["json"] == {"summary": "hello"}


def test_a_400_is_not_retried():
    """A 400 is PocketBase naming a bad field. It is raised straight away,
    with no login and no retry."""
    pb = client_with([FakeResponse(400, {"data": {"nope": {"message": "unknown"}}})])

    with pytest.raises(PocketBaseError) as caught:
        pb.create("interactions", {"nope": "x"})

    assert caught.value.status == 400
    assert len(pb.http.calls) == 1


def test_it_retries_only_once():
    """A server that keeps answering 401 gets one renewal and one retry,
    then the 401 is raised, so there is no endless loop."""
    pb = client_with([
        FakeResponse(401, {}),
        FakeResponse(200, {"token": "fresh-token"}),
        FakeResponse(401, {"message": "still no"}),
    ])

    with pytest.raises(PocketBaseError) as caught:
        pb.list("interactions")

    assert caught.value.status == 401
    assert len(pb.http.calls) == 3


def test_bad_credentials_do_not_loop():
    """A 401 from the login call itself is not retried, so wrong
    credentials are reported as such."""
    pb = PocketBaseClient("http://pb.test", admin_email="a@b.c",
                          admin_password="wrong", enforce_schema=False)
    pb.http = FakeHTTP([FakeResponse(401, {"message": "invalid credentials"})])

    with pytest.raises(PocketBaseError):
        pb.auth_admin()

    assert len(pb.http.calls) == 1


def test_no_credentials_means_no_retry():
    """A client with no admin credentials cannot renew its token, so the
    401 is raised on the first attempt."""
    pb = PocketBaseClient("http://pb.test", enforce_schema=False)
    pb.http = FakeHTTP([FakeResponse(401, {})])

    with pytest.raises(PocketBaseError):
        pb.list("clients")

    assert len(pb.http.calls) == 1


# ==================================================================
# Asking a collection what fields it has
# ==================================================================
# Callers such as the prune use field_names() before sorting on a field
# like `created`, which a collection may not have (sorting on a missing
# field returns a 400 that does not name the field). The collection
# definition is read, because an empty collection has no records whose
# keys could be inspected.

def test_the_fields_of_a_collection_are_read_from_its_schema():
    pb = client_with([
        FakeResponse(200, {"name": "sync_log", "type": "base", "fields": [
            {"name": "id"}, {"name": "event_type"},
            {"name": "detail"}, {"name": "occurred_at"},
        ]}),
    ])

    assert pb.field_names("sync_log") == {
        "id", "event_type", "detail", "occurred_at"}


def test_it_asks_the_collection_rather_than_the_records():
    # The request goes to the collection definition, not to /records: an
    # empty collection would return an empty page and reveal no fields.
    pb = client_with([FakeResponse(200, {"fields": [{"name": "id"}]})])

    pb.field_names("sync_log")

    url = pb.http.calls[0]["url"]
    assert url.endswith("/api/collections/sync_log")
    assert "/records" not in url


def test_an_older_server_that_calls_them_schema_still_works():
    # Older PocketBase versions list fields under `schema` instead of
    # `fields`. Both are accepted, otherwise an older server would appear
    # to have collections with no fields at all.
    pb = client_with([
        FakeResponse(200, {"schema": [{"name": "occurred_at"}]}),
    ])

    assert pb.field_names("sync_log") == {"occurred_at"}


def test_a_collection_that_cannot_be_read_answers_nothing_rather_than_guessing():
    # An unreadable definition gives an empty set, which callers treat as
    # "unknown", distinct from a known list that lacks the field.
    pb = client_with([FakeResponse(404, {"message": "not found"})])

    assert pb.field_names("nope") == set()


def test_a_field_with_no_name_is_not_counted():
    pb = client_with([
        FakeResponse(200, {"fields": [{"name": "id"}, {"type": "text"}]}),
    ])

    assert pb.field_names("sync_log") == {"id"}
