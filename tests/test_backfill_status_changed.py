"""
Tests for tools/backfill_status_changed.py.

The key property: it only fills a blank. Running it again, or after the
trigger engine has stamped a job properly, must not write an estimate over
a real value. Also covered: the updated/created fallback, jobs with no
timestamp, the dry-run text, and the "backfill" marker.
"""

from __future__ import annotations

from tools import backfill_status_changed as backfill


def job(job_id="j1", status="quoted", updated="2026-09-01 10:00:00.000Z",
        created="2026-01-01 00:00:00.000Z", changed=""):
    return {
        "id": job_id, "title": "Filler upgrade", "status": status,
        "updated": updated, "created": created,
        "status_changed_at": changed,
    }


def test_a_job_with_no_stamp_takes_the_date_from_updated():
    item = backfill.plan([job()])
    assert [row["stamp"] for row in item["todo"]] == ["2026-09-01 10:00:00.000Z"]
    assert item["todo"][0]["from"] == "updated"


def test_a_job_that_is_already_stamped_is_left_exactly_as_it_is():
    """A job that already has status_changed_at is left exactly as it is."""
    item = backfill.plan([job(changed="2026-08-14 09:00:00.000Z")])
    assert item["todo"] == []
    assert len(item["already"]) == 1


def test_created_is_the_fallback_when_there_is_no_updated():
    item = backfill.plan([job(updated="")])
    assert item["todo"][0]["from"] == "created"
    assert item["todo"][0]["stamp"] == "2026-01-01 00:00:00.000Z"


def test_a_job_with_no_timestamp_at_all_is_reported_not_invented():
    """A job with neither updated nor created is reported under
    no_source, not given an invented date."""
    item = backfill.plan([job(updated="", created="")])
    assert item["todo"] == []
    assert len(item["no_source"]) == 1


def test_the_dry_run_says_plainly_that_it_wrote_nothing():
    text = backfill.describe(backfill.plan([job()]), write=False)
    assert "Nothing was written" in text
    assert "--write" in text


def test_the_stamp_says_it_was_a_backfill_and_not_a_witness():
    """status_changed_by is "backfill", marking the date as an estimate
    made afterwards."""
    class Recorder:
        def __init__(self):
            self.writes = []

        def update(self, collection, record_id, payload):
            self.writes.append((collection, record_id, payload))
            return payload

    client = Recorder()
    done, failures = backfill.apply(client, backfill.plan([job()]))

    assert (done, failures) == (1, [])
    collection, record_id, payload = client.writes[0]
    assert (collection, record_id) == ("jobs", "j1")
    assert payload["status_changed_by"] == "backfill"
