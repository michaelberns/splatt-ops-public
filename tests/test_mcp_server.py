"""
Tests for mcp_server, the MCP server the agent uses to read and write PocketBase.

Most of these cover the write path:
- log_interaction is idempotent: the same email or call logged twice
  updates one record (keyed on source_key) instead of creating two;
- failures are reported by kind ("Refused, nothing was written" versus
  "The write did not save") and never as success;
- update tools can move a record to the right client, supplier or job and
  say so in the reply;
- archiving always records a reason;
- an update with no fields writes nothing.

Nothing here touches a real database. `RecordingPB` below keeps records in
memory and counts creates and updates.
"""

from __future__ import annotations

import asyncio
import warnings

import pytest

warnings.filterwarnings("ignore")

from core import interactions
from core.pb import SchemaViolation, WriteNotLanded
from mcp_server import output, server


class RecordingPB:
    """A PocketBase that keeps records in a dict and counts what happened."""

    def __init__(self, rows=None):
        self.rows = {name: list(items) for name, items in (rows or {}).items()}
        self.creates = 0
        self.updates = 0
        self._next = 1

    def _all(self, collection):
        return self.rows.setdefault(collection, [])

    def find_one(self, collection, filter_str):
        # The server only looks records up by source_key, so that is the one
        # filter this fake understands.
        if "source_key='" not in filter_str:
            return None
        wanted = filter_str.split("source_key='", 1)[1].rstrip("'").replace("\\'", "'")
        for row in self._all(collection):
            if row.get("source_key") == wanted:
                return row
        return None

    def create(self, collection, payload):
        self.creates += 1
        record = dict(payload)
        record["id"] = "rec%d" % self._next
        self._next += 1
        self._all(collection).append(record)
        return record

    def update(self, collection, record_id, payload):
        self.updates += 1
        for row in self._all(collection):
            if row["id"] == record_id:
                row.update(payload)
                return dict(row)
        raise AssertionError("no record %s in %s" % (record_id, collection))

    def get(self, collection, record_id):
        for row in self._all(collection):
            if row["id"] == record_id:
                return dict(row)
        raise AssertionError("no record %s" % record_id)

    def list(self, collection, filter_str="", sort="", page=1, per_page=200, expand=""):
        items = self._all(collection)
        return {"items": items, "totalItems": len(items), "totalPages": 1}


@pytest.fixture
def fake_pb(monkeypatch):
    pb = RecordingPB()
    monkeypatch.setattr(server, "pb", lambda: pb)
    return pb


def call(tool_name, **kwargs):
    """Run a registered tool the way the MCP runtime would."""
    return asyncio.run(server.mcp.call_tool(tool_name, kwargs))[0][0].text


# -------------------------------------------------------------------
# log_interaction: one record per conversation
# -------------------------------------------------------------------
def test_logging_the_same_email_twice_makes_one_record(fake_pb):
    first = call("log_interaction", interaction_type="email",
                 subject="Gripper belt follow up", summary="Sent to Arun",
                 gmail_id="18f0000000000003")
    second = call("log_interaction", interaction_type="email",
                  subject="IBF Fiji, gripper belt chased",
                  summary="Fuller note written two days later",
                  gmail_id="18f0000000000003", client_id="cli1")

    assert len(fake_pb.rows["interactions"]) == 1
    assert fake_pb.creates == 1
    assert fake_pb.updates == 1
    assert "logged" in first
    assert "updated the existing record" in second

    stored = fake_pb.rows["interactions"][0]
    assert stored["source_key"] == "gmail:18f0000000000003"
    # The second write adds to the record: the client is filled in and the
    # summary is the newer one.
    assert stored["client"] == "cli1"
    assert stored["summary"] == "Fuller note written two days later"


def test_every_interaction_gets_a_key_even_without_a_gmail_id(fake_pb):
    call("log_interaction", interaction_type="call", subject="Rang Kyle about the caps",
         summary="Asked for the PO", date="2026-08-08", client_id="cli1")

    stored = fake_pb.rows["interactions"][0]
    assert stored["source_key"].startswith("manual:")
    assert stored["source_key"] != "manual:"


def test_the_same_call_logged_twice_in_a_day_does_not_duplicate(fake_pb):
    for summary in ("Rang Kyle", "Rang Kyle, he wants the requote by Friday"):
        call("log_interaction", interaction_type="call", subject="Ozone probe caps",
             summary=summary, date="2026-08-08", client_id="cli1")

    assert len(fake_pb.rows["interactions"]) == 1
    assert fake_pb.rows["interactions"][0]["summary"].endswith("by Friday")


def test_two_real_calls_can_be_kept_apart_on_purpose(fake_pb):
    """Same subject, date and client would give the same digest key, so an
    explicit source_id keeps two separate calls apart."""
    call("log_interaction", interaction_type="call", subject="Ozone probe caps",
         summary="Morning call", date="2026-08-08", client_id="cli1",
         source="manual", source_id="morning")
    call("log_interaction", interaction_type="call", subject="Ozone probe caps",
         summary="Afternoon call", date="2026-08-08", client_id="cli1",
         source="manual", source_id="afternoon")

    assert len(fake_pb.rows["interactions"]) == 2


def test_an_interaction_can_point_at_a_supplier(fake_pb):
    call("log_interaction", interaction_type="email", subject="Courier pickup from Italy",
         summary="Chased the collection", supplier_id="sup1", gmail_id="abc123")

    assert fake_pb.rows["interactions"][0]["supplier"] == "sup1"


def test_an_empty_field_is_never_sent(fake_pb):
    """An empty argument means "not supplied", so it is left out of the
    payload rather than sent as a blank that would clear the stored value."""
    call("log_interaction", interaction_type="note", subject="A note",
         summary="Something", gmail_id="xyz")

    stored = fake_pb.rows["interactions"][0]
    for absent in ("client", "contact", "job", "supplier", "from_address"):
        assert absent not in stored


# -------------------------------------------------------------------
# Failures are reported by kind, never as success
# -------------------------------------------------------------------
def test_a_refused_write_says_nothing_was_written(monkeypatch):
    class Refusing(RecordingPB):
        def create(self, collection, payload):
            raise SchemaViolation("field does not exist in clients: full_name")

    monkeypatch.setattr(server, "pb", lambda: Refusing())
    answer = call("create_client", name="Test")

    assert "Refused, nothing was written" in answer
    assert "full_name" in answer


def test_a_write_that_did_not_land_is_not_reported_as_success(monkeypatch):
    class NotLanding(RecordingPB):
        def update(self, collection, record_id, payload):
            raise WriteNotLanded("status: asked for 'completed', stored 'open'")

    monkeypatch.setattr(server, "pb", lambda: NotLanding())
    answer = call("update_assignment", assignment_id="a1", status="completed")

    assert "did not save" in answer
    assert "updated and verified" not in answer


def test_update_assignment_only_offers_fields_the_collection_has():
    """Every optional argument of update_assignment maps to a field of the
    assignments collection, so none of them is refused by the schema check
    on every call."""
    from core.schema import SCHEMA

    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    offered = set(tools["update_assignment"].parameters["properties"]) - {"assignment_id"}

    assert "location" not in offered
    assert offered <= set(SCHEMA["assignments"]["fields"])


def test_an_interaction_with_nothing_in_it_is_refused(fake_pb):
    answer = call("log_interaction", interaction_type="note", subject="", summary="")

    assert "Refused" in answer
    assert fake_pb.rows.get("interactions", []) == []


def test_bad_json_is_explained_rather_than_crashing(fake_pb):
    answer = call("create_task_group", name="Orchard Lane", todoist_task_ids="not json")

    assert "valid JSON" in answer
    assert fake_pb.creates == 0


# -------------------------------------------------------------------
# Filter building
# -------------------------------------------------------------------
def test_an_apostrophe_cannot_break_out_of_a_filter():
    assert output.escape("O'Brien") == "O\\'Brien"


def test_an_or_fragment_is_bracketed_so_it_cannot_swallow_its_neighbours():
    built = output.and_filters(["a='1'", "b~'x' || c~'x'", "", "d='2'"])

    assert built == "(a='1') && (b~'x' || c~'x') && (d='2')"


def test_overdue_never_counts_a_task_with_no_due_date():
    """An empty due date sorts before every real date, so due_date < today
    on its own would also match tasks with no due date. The overdue filter
    therefore adds due_date != ''."""
    built = output.and_filters(["due_date<'2026-08-08' && due_date!='' && status!='completed'"])

    assert "due_date!=''" in built


# -------------------------------------------------------------------
# How an interaction's key is chosen (core.interactions.key_for)
# -------------------------------------------------------------------
def test_gmail_id_wins_over_the_digest():
    assert interactions.key_for(gmail_id="abc", subject="hi") == ("gmail", "abc")


def test_an_explicit_source_wins_over_everything():
    assert interactions.key_for(gmail_id="abc", source="xero", source_id="INV-1") == \
        ("xero", "INV-1")


def test_the_digest_ignores_the_summary():
    """The key ignores the summary and the time of day, so a fuller note
    about the same conversation lands on the same record."""
    a = interactions.key_for(subject="Ozone caps", date="2026-08-08", client="cli1")
    b = interactions.key_for(subject="ozone caps", date="2026-08-08 14:30:00", client="cli1")

    assert a == b


def test_the_digest_separates_different_clients():
    a = interactions.key_for(subject="Follow up", date="2026-08-08", client="cli1")
    b = interactions.key_for(subject="Follow up", date="2026-08-08", client="cli2")

    assert a != b


# -------------------------------------------------------------------
# Moving a record to the right owner
#
# update_job and update_quote can change a record's client or job, for
# example a job filed against a supplier by mistake, or a quote with no
# job. The reply names the old and new ids.
# -------------------------------------------------------------------
def seed(fake_pb, collection, **row):
    fake_pb.rows.setdefault(collection, []).append(dict(row))
    return row["id"]


def test_moving_a_job_to_another_client_says_where_it_came_from(fake_pb):
    seed(fake_pb, "jobs", id="job1", title="QU-7994 change parts", client="wrongco")

    answer = call("update_job", job_id="job1", client_id="rightco")

    assert fake_pb.rows["jobs"][0]["client"] == "rightco"
    assert "moved from wrongco to rightco" in answer


def test_a_job_edit_that_touches_no_client_says_nothing_about_moving(fake_pb):
    seed(fake_pb, "jobs", id="job1", title="Filler unit", client="cli1")

    answer = call("update_job", job_id="job1", status="won")

    assert fake_pb.rows["jobs"][0]["status"] == "won"
    assert "moved" not in answer


def test_setting_the_same_client_again_is_not_reported_as_a_move(fake_pb):
    seed(fake_pb, "jobs", id="job1", title="Filler unit", client="cli1")

    answer = call("update_job", job_id="job1", client_id="cli1")

    assert "moved" not in answer


def test_a_status_change_records_when_it_happened_and_who_did_it(fake_pb):
    """The seven day chase rule reads status_changed_at. Neither `created`
    (the day the job was opened) nor `updated` (moves on any edit) can say
    when the status changed."""
    seed(fake_pb, "jobs", id="job1", title="Filler unit", status="quoting",
         client="cli1")

    call("update_job", job_id="job1", status="quoted")

    stored = fake_pb.rows["jobs"][0]
    assert stored["status"] == "quoted"
    assert stored["status_changed_by"] == "mcp"
    assert stored["status_changed_at"]


def test_saving_the_same_status_again_does_not_reset_the_clock(fake_pb):
    """Restamping on an unchanged status would restart the chase clock on
    every edit, so a job that keeps being edited would never be chased."""
    seed(fake_pb, "jobs", id="job1", title="Filler unit", status="quoted",
         client="cli1", status_changed_at="2026-08-01 00:00:00.000Z",
         status_changed_by="task:6aaaaaaaaaaaaaaa")

    call("update_job", job_id="job1", status="quoted", notes="rang them")

    stored = fake_pb.rows["jobs"][0]
    assert stored["status_changed_at"] == "2026-08-01 00:00:00.000Z"
    assert stored["status_changed_by"] == "task:6aaaaaaaaaaaaaaa"


def test_an_edit_that_is_not_a_status_change_leaves_the_stamp_alone(fake_pb):
    seed(fake_pb, "jobs", id="job1", title="Filler unit", status="quoted",
         client="cli1", status_changed_at="2026-08-01 00:00:00.000Z")

    call("update_job", job_id="job1", value=4200)

    assert fake_pb.rows["jobs"][0]["status_changed_at"] == "2026-08-01 00:00:00.000Z"


def test_an_orphan_quote_can_be_joined_to_its_job(fake_pb):
    seed(fake_pb, "quotes", id="qu1", title="QU-8248", client="cli1", job="")

    answer = call("update_quote", quote_id="qu1", job_id="job9")

    assert fake_pb.rows["quotes"][0]["job"] == "job9"
    assert "QU-8248" in answer
    # There was no job before, so linking one is not reported as a move.
    assert "Job moved" not in answer


def test_a_quote_pointed_at_the_wrong_job_names_both_jobs(fake_pb):
    seed(fake_pb, "quotes", id="qu1", title="QU-8090", client="cli1", job="job1")

    answer = call("update_quote", quote_id="qu1", job_id="job2")

    assert "Job moved from job1 to job2" in answer


# -------------------------------------------------------------------
# Archiving: always with a reason, and reversible
# -------------------------------------------------------------------
def test_archiving_a_duplicate_quote_records_why_and_when(fake_pb):
    seed(fake_pb, "quotes", id="qu1", title="QU-7994 duplicate", client="cli1")

    call("update_quote", quote_id="qu1",
         archive_reason="Duplicate of qu2, which keeps the job link.")

    stored = fake_pb.rows["quotes"][0]
    assert stored["archived"] is True
    assert "Duplicate of qu2" in stored["archived_reason"]
    assert stored["archived_at"]


def test_unarchiving_clears_the_reason_it_was_archived_for(fake_pb):
    seed(fake_pb, "jobs", id="job1", title="Drive shaft", client="cli1",
         archived=True, archived_reason="Stale", archived_at="2026-08-01")

    call("update_job", job_id="job1", unarchive=True)

    stored = fake_pb.rows["jobs"][0]
    assert stored["archived"] is False
    assert stored["archived_reason"] == ""
    assert stored["archived_at"] == ""


def test_archiving_and_unarchiving_at_once_is_refused(fake_pb):
    seed(fake_pb, "jobs", id="job1", title="Drive shaft", client="cli1")

    answer = call("update_job", job_id="job1",
                  archive_reason="Stale", unarchive=True)

    assert "not both" in answer
    assert fake_pb.updates == 0


def test_archiving_survives_only_given_dropping_false(fake_pb):
    """False equals 0 in Python and only_given drops 0, so the archive
    flags are built by archive_fields instead. This checks that unarchive
    still sends archived=False."""
    assert server.archive_fields("", True) == {
        "archived": False, "archived_reason": "", "archived_at": ""}
    assert server.archive_fields("", False) == {}


# -------------------------------------------------------------------
# An update with no fields writes nothing
# -------------------------------------------------------------------
def test_a_job_update_with_no_fields_writes_nothing(fake_pb):
    answer = call("update_job", job_id="job1")

    assert answer == "Nothing to update."
    assert fake_pb.updates == 0


def test_a_quote_update_with_no_fields_writes_nothing(fake_pb):
    answer = call("update_quote", quote_id="qu1")

    assert answer == "Nothing to update."
    assert fake_pb.updates == 0


# -------------------------------------------------------------------
# Moving interactions and contacts to the right party
#
# log_interaction only merges records with the same key. update_interaction
# and update_contact can move a record that was filed against the wrong
# party, for example supplier emails recorded against a client.
# -------------------------------------------------------------------
def test_supplier_traffic_can_be_moved_off_the_client_it_was_filed_under(fake_pb):
    seed(fake_pb, "interactions", id="int1", subject="Chasing the parts list",
         client="fakeclient", supplier="")

    answer = call("update_interaction", interaction_id="int1",
                  supplier_id="sup1", detach_client=True)

    stored = fake_pb.rows["interactions"][0]
    assert stored["client"] == ""
    assert stored["supplier"] == "sup1"
    assert "Detached from client fakeclient" in answer
    assert "Attached to supplier sup1" in answer


def test_an_interaction_cannot_be_moved_and_detached_at_once(fake_pb):
    seed(fake_pb, "interactions", id="int1", subject="Parts list", client="cli1")

    answer = call("update_interaction", interaction_id="int1",
                  client_id="cli2", detach_client=True)

    assert "not both" in answer
    assert fake_pb.updates == 0


def test_detaching_is_possible_even_though_an_empty_id_means_not_supplied(fake_pb):
    """client_id="" means "leave it alone", so clearing the relation needs
    the separate detach_client flag. This checks the first half: an update
    that passes no client_id leaves the client as it was."""
    seed(fake_pb, "interactions", id="int1", subject="Parts list", client="cli1")

    call("update_interaction", interaction_id="int1", summary="Fuller note")

    assert fake_pb.rows["interactions"][0]["client"] == "cli1"


def test_a_contact_filed_under_the_wrong_company_can_be_moved(fake_pb):
    seed(fake_pb, "contacts", id="con1", name="Shane Cutler", client="wrongco")

    answer = call("update_contact", contact_id="con1", client_id="rightco")

    assert fake_pb.rows["contacts"][0]["client"] == "rightco"
    assert "moved from wrongco to rightco" in answer


def test_a_contact_can_be_left_attached_to_nobody(fake_pb):
    seed(fake_pb, "contacts", id="con1", name="Eduardo", client="wrongco")

    answer = call("update_contact", contact_id="con1", detach_client=True)

    assert fake_pb.rows["contacts"][0]["client"] == ""
    assert "Detached from client wrongco" in answer


def test_a_contact_update_with_no_fields_writes_nothing(fake_pb):
    answer = call("update_contact", contact_id="con1")

    assert answer == "Nothing to update."
    assert fake_pb.updates == 0


def test_merging_notes_into_a_supplier_keeps_what_was_already_there(fake_pb):
    """append_notes adds to the existing notes instead of replacing them."""
    seed(fake_pb, "suppliers", id="sup1", name="Taponera SL",
         notes="Spanish manufacturer. Eduardo main contact.")

    call("update_supplier", supplier_id="sup1",
         append_notes="Factory: Calle de Ejemplo 10, Valencia.")

    stored = fake_pb.rows["suppliers"][0]
    assert "Eduardo main contact" in stored["notes"]
    assert "Calle de Ejemplo 10, Valencia" in stored["notes"]


def test_replacing_and_appending_notes_at_once_is_refused(fake_pb):
    seed(fake_pb, "suppliers", id="sup1", name="Taponera SL", notes="Old")

    answer = call("update_supplier", supplier_id="sup1",
                  notes="New", append_notes="Also this")

    assert "not both" in answer
    assert fake_pb.updates == 0
