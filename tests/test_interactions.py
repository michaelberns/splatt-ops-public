"""
Tests for core/interactions.save and for the write read-back in core/pb.py.

Two guarantees are pinned down:
  1. Idempotence. Saving the same (source, id) any number of times leaves
     one record, including when two runs race to create it.
  2. Verification. A write that PocketBase accepted but did not store
     (a silently dropped field) raises WriteNotLanded, naming the field,
     instead of being reported as saved.

The first half uses FakePB below, which mimics PocketBase's habit of
ignoring unknown fields. The second half drives the real
PocketBaseClient with a stub transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import interactions
from core.interactions import InteractionError, save, source_key
from core.pb import PocketBaseClient, PocketBaseError, WriteNotLanded, values_match


class FakePB:
    """An in-memory PocketBase that, like the real one, silently ignores
    fields it does not know. `drop` names extra fields to lose, to
    simulate a write that does not land."""

    KNOWN = interactions.WRITABLE | {"source_key", "id"}

    def __init__(self, drop=()):
        self.records = {}
        self.next_id = 1
        self.drop = set(drop)
        self.creates = 0
        self.updates = 0

    def find_one(self, collection, filter_str):
        wanted = filter_str.split("='", 1)[1].rstrip("'")
        for record in self.records.values():
            if record.get("source_key") == wanted:
                return record
        return None

    def _store(self, record, payload):
        for field, value in payload.items():
            if field in self.drop or field not in self.KNOWN:
                continue
            record[field] = value

    def create(self, collection, payload):
        record = {"id": "r%d" % self.next_id}
        self.next_id += 1
        self.creates += 1
        self._store(record, payload)
        self.records[record["id"]] = record
        return self._confirm(payload, record)

    def update(self, collection, record_id, payload):
        self.updates += 1
        record = self.records[record_id]
        self._store(record, payload)
        return self._confirm(payload, record)

    @staticmethod
    def _confirm(payload, record):
        missed = [f for f, v in payload.items() if not values_match(v, record.get(f))]
        if missed:
            raise WriteNotLanded("did not save: %s" % ", ".join(missed))
        return record


class RacingPB(FakePB):
    """Plays the losing side of a create race.

    Two runs both look up the key, both find nothing and both create. The
    unique index on source_key refuses the second create. Here the first
    create call stores the other run's record and then raises the
    refusal, so the record exists by the time save() looks again.
    """

    def __init__(self, refusal=None):
        super().__init__()
        self.refusals = 1
        self.refusal = refusal or PocketBaseError(
            "POST", "/api/collections/interactions/records", 400,
            {"message": "Failed to create record.",
             "data": {"source_key": {"code": "validation_not_unique",
                                     "message": "Value must be unique."}}},
        )

    def create(self, collection, payload):
        if self.refusals > 0:
            self.refusals -= 1
            # The other run's record is stored just before this create is refused.
            record = {"id": "r_other"}
            self._store(record, payload)
            self.records[record["id"]] = record
            raise self.refusal
        return super().create(collection, payload)


class FakeLedger:
    def __init__(self):
        self.entries = []

    def record(self, action, collection, record_id, fields, source, note=""):
        self.entries.append((action, collection, record_id, fields, source, note))


# the source key
def test_the_key_names_the_thing_it_mirrors():
    assert source_key("todoist", "6cXq4") == "todoist:6cXq4"
    assert source_key("Gmail", "18f2a9") == "gmail:18f2a9"
    assert source_key("todoist", 12345) == "todoist:12345"


def test_a_key_with_no_id_is_refused():
    """A key such as 'todoist:' identifies nothing and would collide with
    every other record lacking an id, so it is refused."""
    for bad in ("", None, "   "):
        with pytest.raises(InteractionError):
            source_key("todoist", bad)


def test_an_unknown_source_is_refused_rather_than_accepted_quietly():
    with pytest.raises(InteractionError):
        source_key("slack", "abc")
    with pytest.raises(InteractionError):
        source_key("", "abc")


# idempotence
def test_running_the_same_sync_ten_times_leaves_one_record():
    """Ten saves of the same Todoist task give one create and nine updates."""
    pb = FakePB()
    for _ in range(10):
        save(pb, "todoist", "6cXq4", summary="cablecorp / wes jordan, source quotes")

    assert len(pb.records) == 1
    assert pb.creates == 1
    assert pb.updates == 9


def test_a_later_pass_updates_the_record_rather_than_adding_one():
    pb = FakePB()
    save(pb, "todoist", "6cXq4", summary="chase Coastline")
    save(pb, "todoist", "6cXq4", summary="chase Coastline, now answered", type="note")

    record = list(pb.records.values())[0]
    assert record["summary"] == "chase Coastline, now answered"
    assert record["type"] == "note"


def test_two_different_tasks_are_two_different_records():
    pb = FakePB()
    save(pb, "todoist", "aaa", summary="first")
    save(pb, "todoist", "bbb", summary="second")
    assert len(pb.records) == 2


def test_the_same_id_from_two_systems_does_not_collide():
    pb = FakePB()
    save(pb, "todoist", "123", summary="a task")
    save(pb, "gmail", "123", summary="an email")
    assert len(pb.records) == 2


# supplier links
def test_an_interaction_can_point_at_a_supplier():
    pb = FakePB()
    record = save(pb, "gmail", "18f2a9", summary="QXP pickup delayed",
                  supplier="sup_qxp")
    assert record["supplier"] == "sup_qxp"


def test_an_interaction_can_point_at_a_client_and_a_supplier_at_once():
    """A freight email concerns both a client and a supplier, so both
    links are kept on one record."""
    pb = FakePB()
    record = save(pb, "gmail", "18f2b0", summary="Coastline pickup for Rialto Filling",
                  client="cli_rialto", supplier="sup_coastline")
    assert record["client"] == "cli_rialto"
    assert record["supplier"] == "sup_coastline"


# caller mistakes
def test_a_field_that_is_not_on_an_interaction_is_named_not_dropped():
    pb = FakePB()
    with pytest.raises(InteractionError) as caught:
        save(pb, "todoist", "aaa", summary="x", clientt="typo")
    assert "clientt" in str(caught.value)


def test_an_interaction_that_says_nothing_is_refused():
    pb = FakePB()
    with pytest.raises(InteractionError):
        save(pb, "todoist", "aaa", client="cli_1")


def test_a_missing_value_does_not_wipe_one_that_is_already_there():
    """A None value means "not known", not "clear it", so it is not sent
    and a client linked earlier (for example by hand) is kept."""
    pb = FakePB()
    save(pb, "todoist", "aaa", summary="first", client="cli_1")
    save(pb, "todoist", "aaa", summary="second", client=None)

    record = list(pb.records.values())[0]
    assert record["client"] == "cli_1"
    assert record["summary"] == "second"


def test_every_write_reaches_the_ledger():
    pb = FakePB()
    ledger = FakeLedger()
    save(pb, "todoist", "aaa", summary="first", ledger=ledger)
    save(pb, "todoist", "aaa", summary="second", ledger=ledger)

    assert [e[0] for e in ledger.entries] == ["create", "update"]
    assert {e[1] for e in ledger.entries} == {"interactions"}
    assert {e[5] for e in ledger.entries} == {"todoist:aaa"}


# writes that report success but did not store the value
def test_a_silently_dropped_field_stops_the_run():
    """PocketBase answers 200 even when it drops a field. The read-back
    turns that into a WriteNotLanded exception."""
    pb = FakePB(drop={"summary"})
    with pytest.raises(WriteNotLanded):
        save(pb, "todoist", "aaa", summary="this will vanish")


def test_the_failure_names_the_field_that_did_not_save():
    pb = FakePB(drop={"subject"})
    with pytest.raises(WriteNotLanded) as caught:
        save(pb, "todoist", "aaa", summary="fine", subject="this will vanish")
    assert "subject" in str(caught.value)


# two runs at once
def test_losing_the_race_to_create_still_ends_with_one_record():
    """When the unique index refuses a create because another run has just
    created the same key, save() finds that record and updates it, ending
    with one record and no exception."""
    pb = RacingPB()
    record = save(pb, "todoist", "6cXq4", summary="chase Coastline")

    assert len(pb.records) == 1
    assert record["summary"] == "chase Coastline"
    assert pb.updates == 1


def test_a_400_about_something_else_is_not_swallowed():
    """Only the unique-index rejection of source_key is retried as an
    update. Any other 400 (here a missing required value) is raised, so a
    real bad field is never hidden."""
    other = PocketBaseError(
        "POST", "/api/collections/interactions/records", 400,
        {"message": "Failed to create record.",
         "data": {"summary": {"code": "validation_required",
                              "message": "Missing required value."}}},
    )
    pb = RacingPB(refusal=other)
    with pytest.raises(PocketBaseError):
        save(pb, "todoist", "6cXq4", summary="chase Coastline")


def test_a_server_error_is_not_swallowed():
    boom = PocketBaseError("POST", "/api/collections/interactions/records", 500,
                           {"message": "Something went wrong."})
    pb = RacingPB(refusal=boom)
    with pytest.raises(PocketBaseError):
        save(pb, "todoist", "6cXq4", summary="chase Coastline")


def test_a_refusal_with_no_record_behind_it_is_not_swallowed():
    """If the index says the key is taken but no record can be found, the
    original error is raised, since an update has nothing to update."""
    class VanishingPB(RacingPB):
        def create(self, collection, payload):
            self.refusals -= 1
            raise self.refusal

    pb = VanishingPB()
    with pytest.raises(PocketBaseError):
        save(pb, "todoist", "6cXq4", summary="chase Coastline")


# what counts as the same value coming back
def test_the_same_value_written_two_ways_is_not_a_failure():
    assert values_match("2026-08-08", "2026-08-08 00:00:00.000Z")
    assert values_match(5, 5.0)
    assert values_match(5, "5")
    assert values_match("  spaced  ", "spaced")
    assert values_match(None, "")
    assert values_match("", None)
    assert values_match(True, True)
    assert values_match(["a", "b"], ["a", "b"])


def test_a_real_difference_is_still_a_failure():
    assert not values_match("chase Coastline", "chase QXP")
    assert not values_match("2026-08-08", "2026-08-09")
    assert not values_match(5, 6)
    assert not values_match("something", "")
    assert not values_match("", "something")
    assert not values_match(True, False)
    assert not values_match(["a"], ["a", "b"])


def test_zero_and_false_are_values_not_nothing():
    """0 and False are real values, not blanks: an amount of 0 and an
    empty amount mean different things."""
    assert not values_match(0, "")
    assert not values_match(False, "")


def test_a_link_that_comes_back_expanded_is_the_same_link():
    """With `expand`, PocketBase returns the whole related record instead
    of its id. That is the same link in another format, so it matches."""
    assert values_match("cli_123", {"id": "cli_123", "name": "Clearwater Bottling"})
    assert values_match({"id": "cli_123"}, "cli_123")
    assert values_match(["cli_1", "cli_2"],
                        [{"id": "cli_1"}, {"id": "cli_2"}])


def test_an_expanded_link_to_the_wrong_record_is_still_a_failure():
    """The leniency covers only the format of the link. A different id is
    still a mismatch."""
    assert not values_match("cli_123", {"id": "cli_999", "name": "Someone Else"})
    assert not values_match(["cli_1"], [{"id": "cli_2"}])


# the read-back in PocketBaseClient, for create and update
class StubTransport:
    """Replaces PocketBaseClient._request so the real client can be tested.

    It echoes the payload back as the stored record, minus any field named
    in `drop`, which simulates PocketBase silently losing that field.
    """

    def __init__(self, drop=()):
        self.drop = set(drop)
        self.calls = []

    def __call__(self, method, path, payload=None, params=None):
        self.calls.append((method, path))
        stored = {k: v for k, v in (payload or {}).items() if k not in self.drop}
        stored["id"] = "r1"
        return stored


def _client(drop=()):
    pb = PocketBaseClient("http://localhost:8090", enforce_schema=False)
    pb._request = StubTransport(drop=drop)
    return pb


def test_a_create_that_landed_is_returned():
    pb = _client()
    record = pb.create("interactions", {"summary": "chase Coastline"})
    assert record["summary"] == "chase Coastline"


def test_a_create_that_did_not_land_raises():
    pb = _client(drop={"summary"})
    with pytest.raises(WriteNotLanded) as caught:
        pb.create("interactions", {"summary": "this will vanish"})
    assert "summary" in str(caught.value)


def test_an_update_that_landed_is_returned():
    pb = _client()
    record = pb.update("interactions", "r1", {"summary": "chase Coastline"})
    assert record["summary"] == "chase Coastline"


def test_an_update_that_did_not_land_raises_the_same_way():
    """Updates are read back the same way as creates. With source keys in
    place, most interaction writes are updates."""
    pb = _client(drop={"client"})
    with pytest.raises(WriteNotLanded) as caught:
        pb.update("interactions", "r1", {"summary": "fine", "client": "cli_1"})
    assert "client" in str(caught.value)


def test_the_failure_says_what_was_asked_for_and_what_is_there():
    pb = _client(drop={"client"})
    with pytest.raises(WriteNotLanded) as caught:
        pb.update("interactions", "r1", {"client": "cli_1"})
    message = str(caught.value)
    assert "cli_1" in message
    assert "r1" in message


def test_the_read_back_can_be_turned_off_but_is_on_by_default():
    assert PocketBaseClient("http://localhost:8090").verify_writes is True

    pb = PocketBaseClient("http://localhost:8090", enforce_schema=False,
                          verify_writes=False)
    pb._request = StubTransport(drop={"summary"})
    record = pb.update("interactions", "r1", {"summary": "this will vanish"})
    assert "summary" not in record


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
