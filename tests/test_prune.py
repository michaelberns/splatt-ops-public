"""
Tests for daemon/prune.py.

Most of these cover the ways a prune can look as if it worked while
deleting nothing: reading only one page, not sorting, filtering in Python,
swallowing failed deletes, or using a date field the collection does not
have.

The fake PocketBase behaves like the real one in the ways that matter
here: it pages by page and perPage, returns records in insertion order
unless asked to sort, and answers HTTP 400 for a filter or sort on a
field the collection does not have.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daemon import prune as prune_module
from daemon.prune import CANONICAL, TYPO, cutoff_iso, old_records, prune, which_collection


NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def stamp(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S.000Z")


class FakePB:
    """A PocketBase that pages and sorts the way the real one does.

    Records are kept in insertion order and are only sorted when a query
    asks for it, so a test can tell whether the prune asked.
    """

    def __init__(self, records=None, collections=("sync_log",), fail_deletes=False,
                 fields=("id", "created"), schema_readable=True):
        self.records = list(records or [])
        self.known = set(collections)
        self.deleted = []
        self.fail_deletes = fail_deletes
        self.queries = []
        # Which fields the collection has. Real PocketBase returns 400 for a
        # sort or filter on a missing field, and the sync_log in the schema
        # has no `created`, so the fake has to model both shapes.
        self.fields = set(fields)
        self.schema_readable = schema_readable

    def field_names(self, collection):
        if not self.schema_readable:
            return set()
        return set(self.fields)

    def list(self, collection, filter_str="", sort="", page=1, per_page=200, expand=""):
        if collection not in self.known:
            raise RuntimeError("no such collection: %s" % collection)
        self.queries.append(
            {"collection": collection, "filter": filter_str, "sort": sort,
             "page": page, "per_page": per_page})

        items = list(self.records)
        if filter_str:
            # Only the one filter shape the prune produces is understood,
            # which keeps the fake simple enough to trust.
            field, _, rest = filter_str.partition(" < ")
            assert rest.startswith('"'), filter_str
            # The 400 a real server gives for a missing field.
            if field not in self.fields:
                raise RuntimeError(
                    "400 | no such field %r on %s" % (field, collection))
            cutoff = rest.split('"')[1]
            items = [r for r in items if r.get(field, "") < cutoff]
        if sort:
            if sort.lstrip("-+") not in self.fields:
                raise RuntimeError(
                    "400 | no such field %r on %s" % (sort, collection))
            items.sort(key=lambda r: r.get(sort, ""))

        total = len(items)
        pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        return {"items": items[start:start + per_page],
                "totalItems": total, "totalPages": pages, "page": page}

    def delete(self, collection, record_id):
        if self.fail_deletes:
            raise RuntimeError("forbidden")
        self.records = [r for r in self.records if r["id"] != record_id]
        self.deleted.append(record_id)
        return True


def logbook(old=0, fresh=0):
    """A collection with a given number of stale and current records.

    Stale and fresh records are interleaved, so a prune that only works
    when the old records happen to come first would fail.
    """
    out = []
    for i in range(max(old, fresh)):
        if i < old:
            out.append({"id": "old%04d" % i, "created": stamp(60 + i)})
        if i < fresh:
            out.append({"id": "new%04d" % i, "created": stamp(1)})
    return out


# ==================================================================
# Paging, sorting, server-side filtering and failure counting
# ==================================================================

def test_it_deletes_more_than_one_page_in_a_single_run():
    # 500 old records span three pages of 200. All of them are deleted in
    # one run, so a daily prune keeps up with a collection that gains more
    # than one page a day.
    pb = FakePB(logbook(old=500))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=5000, write=True)

    assert result["deleted"] == 500, result
    assert len(pb.records) == 0


def test_it_asks_for_the_oldest_first():
    # Every query sorts on the date field, so the oldest records come
    # first and a run never sees only recent records.
    pb = FakePB(logbook(old=10, fresh=10))

    old_records(pb, "sync_log", cutoff_iso(30, NOW), limit=5000)

    assert all(q["sort"] == "created" for q in pb.queries), pb.queries


def test_it_filters_on_the_server_not_in_python():
    # Every query carries the cutoff filter, so only records to be
    # deleted are fetched.
    pb = FakePB(logbook(old=5, fresh=5))

    old_records(pb, "sync_log", cutoff_iso(30, NOW), limit=5000)

    assert all(q["filter"].startswith('created < "') for q in pb.queries), pb.queries


def test_a_failed_delete_is_counted_not_swallowed():
    # A delete that raises is counted in `failed` and listed in
    # `failures`, so a refusing server cannot look like a clean run.
    pb = FakePB(logbook(old=5), fail_deletes=True)

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=5000, write=True)

    assert result["deleted"] == 0
    assert result["failed"] > 0
    assert result["failures"], "a failing run reported no reason"


def test_it_gives_up_when_every_delete_is_failing():
    # Twenty failures with nothing deleted stop the run, and the stop is
    # reported.
    pb = FakePB(logbook(old=1000), fail_deletes=True)

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=5000, write=True)

    assert result["failed"] == 20, result["failed"]
    assert any("stopped after 20" in f for f in result["failures"])


# ==================================================================
# What it does and does not touch
# ==================================================================

def test_records_inside_the_keep_window_survive():
    pb = FakePB(logbook(old=5, fresh=7))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=5000, write=True)

    assert result["deleted"] == 5
    assert len(pb.records) == 7
    assert all(r["id"].startswith("new") for r in pb.records)


def test_a_collection_of_only_recent_records_is_left_alone():
    pb = FakePB(logbook(fresh=50))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=5000, write=True)

    assert result["found"] == 0
    assert result["deleted"] == 0
    assert pb.deleted == []


def test_the_cutoff_lands_where_the_keep_window_says():
    # The cutoff is exactly keep_days before now.
    assert cutoff_iso(30, NOW) == "2026-07-11 12:00:00"
    assert cutoff_iso(1, NOW) == "2026-08-09 12:00:00"


def test_the_cutoff_is_formatted_the_way_pocketbase_wants_it():
    # PocketBase filters want "YYYY-MM-DD HH:MM:SS" with a space, not the
    # ISO "T" separator.
    value = cutoff_iso(30, NOW)

    assert "T" not in value, value
    assert value.count("-") == 2 and value.count(":") == 2


# ==================================================================
# Nothing is deleted unless it is asked for
# ==================================================================

def test_a_plain_run_deletes_nothing():
    # write=False deletes nothing. This is the only command in the daemon
    # that deletes, so the default is the safe one, as for sync and
    # reverse.
    pb = FakePB(logbook(old=20))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=5000, write=False)

    assert result["deleted"] == 0
    assert pb.deleted == []
    assert len(pb.records) == 20


def test_a_plain_run_still_says_how_much_there_is():
    # A dry run still reports how many records it found.
    pb = FakePB(logbook(old=20))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=5000, write=False)

    assert result["found"] == 20
    assert result["wrote"] is False


# ==================================================================
# The cap
# ==================================================================

def test_the_cap_limits_one_run():
    pb = FakePB(logbook(old=300))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=100, write=True)

    assert result["deleted"] == 100
    assert len(pb.records) == 200


def test_hitting_the_cap_is_reported():
    # A run that reaches the cap says so in `capped`.
    pb = FakePB(logbook(old=300))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=100, write=True)

    assert result["capped"] is True


def test_a_run_that_finishes_is_not_flagged_as_capped():
    pb = FakePB(logbook(old=10))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), limit=100, write=True)

    assert result["capped"] is False


def test_repeated_runs_clear_a_backlog():
    # A capped run followed by more runs clears the whole backlog, because
    # each run starts from the oldest record still there.
    pb = FakePB(logbook(old=250))

    for _ in range(3):
        prune(pb, "sync_log", cutoff_iso(30, NOW), limit=100, write=True)

    assert len(pb.records) == 0


# ==================================================================
# Which collection it finds
# ==================================================================

def test_it_finds_the_collection_this_repo_declares():
    pb = FakePB(collections=("sync_log",))

    assert which_collection(pb) == CANONICAL


def test_it_falls_back_to_the_misspelled_collection():
    # sync_1og (digit one instead of letter l) is found when it is the
    # only collection present.
    pb = FakePB(collections=("sync_1og",))

    assert which_collection(pb) == TYPO


def test_the_correct_spelling_wins_when_both_exist():
    pb = FakePB(collections=("sync_log", "sync_1og"))

    assert which_collection(pb) == CANONICAL


def test_no_collection_at_all_returns_nothing_rather_than_raising():
    pb = FakePB(collections=())

    assert which_collection(pb) is None


def test_the_two_names_differ_only_by_the_character_that_caused_this():
    # TYPO is CANONICAL with "l" replaced by "1", as the prune.py comment
    # describes.
    assert CANONICAL.replace("l", "1") == TYPO
    assert CANONICAL != TYPO


# ==================================================================
# Wiring
# ==================================================================

def test_the_command_is_registered():
    # `prune` is a registered subcommand: argparse exits 0 on --help only
    # if it exists.
    from daemon import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["prune", "--help"])

    assert exc.value.code == 0


def parsed(argv, monkeypatch):
    """Run the command line as far as the arguments and stop there."""
    from daemon import cli

    seen = {}

    def capture(args, conf, log):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli.prune_module, "cmd_prune", capture)
    monkeypatch.setattr(cli, "settings", lambda: object())
    cli.main(argv)
    return seen["args"]


def test_the_command_does_not_write_unless_told_to(monkeypatch):
    # Without --write the command does not delete. The flag is --write,
    # the same word sync and reverse use.
    args = parsed(["prune"], monkeypatch)

    assert args.write is False


def test_the_write_flag_turns_deleting_on(monkeypatch):
    args = parsed(["prune", "--write"], monkeypatch)

    assert args.write is True


def test_the_keep_window_and_cap_are_settings_not_constants():
    # The keep window and the cap are set in config/settings.yaml, so they
    # can be changed without a code change.
    from pathlib import Path

    body = (Path(__file__).resolve().parent.parent
            / "config" / "settings.yaml").read_text()

    assert "sync_log_keep_days" in body
    assert "sync_log_max_deletes" in body


# ==================================================================
# Which date field the collection has
# ==================================================================
# The sync_log in the schema has id, event_type, detail and occurred_at,
# and no `created`. The prune asks the collection which date field it has
# rather than assuming `created`.

def test_the_date_field_is_taken_from_the_collection_not_assumed():
    pb = FakePB(fields=("id", "event_type", "detail", "occurred_at"))

    assert prune_module.which_date_field(pb, "sync_log") == "occurred_at"


def test_created_wins_when_the_collection_has_both():
    # PocketBase fills in `created` itself, while occurred_at can be blank,
    # so `created` is preferred when both exist.
    pb = FakePB(fields=("id", "created", "occurred_at"))

    assert prune_module.which_date_field(pb, "sync_log") == "created"


def test_a_collection_with_no_date_field_is_refused_not_guessed():
    pb = FakePB(fields=("id", "event_type", "detail"))

    assert prune_module.which_date_field(pb, "sync_log") is None


def test_a_prune_with_nothing_to_measure_by_deletes_nothing_and_says_so():
    # With no date field the prune deletes nothing, sets refused=True and
    # lists a reason in `failures`.
    pb = FakePB(records=logbook(old=50), fields=("id", "event_type"))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), 5000, write=True)

    assert result["refused"] is True
    assert result["deleted"] == 0
    assert pb.deleted == []
    assert result["failures"], "a refusal has to be reported, not returned quietly"


def test_a_refusal_is_a_broken_run_not_a_clean_one(monkeypatch, capsys):
    # cmd_prune returns EXIT_BROKEN for a refusal, so the loop counts it as
    # a failed pass and it shows in the heartbeat.
    class Args:
        write = False
        json = False

    class Conf:
        def pocketbase(self):
            return {}

        def get(self, key, default=None):
            return default

    class Log:
        def __getattr__(self, name):
            return lambda *a, **k: None

    pb = FakePB(records=logbook(old=5), fields=("id", "event_type"))
    monkeypatch.setattr(prune_module, "PocketBaseClient", lambda **kw: pb)
    pb.health = lambda: True
    pb.auth_admin = lambda: None
    pb.close = lambda: None

    code = prune_module.cmd_prune(Args(), Conf(), Log())

    assert code == prune_module.EXIT_BROKEN
    out = capsys.readouterr().out
    assert "no date field" in out


def test_a_collection_measured_by_occurred_at_is_actually_pruned():
    # End to end on the schema's shape: fifty stale records and five fresh
    # ones in a collection that has occurred_at and no created.
    records = []
    for i in range(50):
        records.append({"id": "old%02d" % i, "occurred_at": stamp(60 + i)})
    for i in range(5):
        records.append({"id": "new%02d" % i, "occurred_at": stamp(1)})
    pb = FakePB(records=records, fields=("id", "occurred_at"))

    result = prune(pb, "sync_log", cutoff_iso(30, NOW), 5000, write=True)

    assert result["field"] == "occurred_at"
    assert result["deleted"] == 50
    assert sorted(pb.deleted) == sorted("old%02d" % i for i in range(50))
    assert {r["id"] for r in pb.records} == {"new%02d" % i for i in range(5)}


def test_the_query_names_the_field_the_collection_actually_has():
    # Checks the query itself, not just the count, because the count
    # would also be right if the filtering happened in Python.
    pb = FakePB(records=logbook(old=3), fields=("id", "occurred_at"))
    for record in pb.records:
        record["occurred_at"] = record.pop("created")

    prune(pb, "sync_log", cutoff_iso(30, NOW), 5000, write=False)

    assert pb.queries, "nothing was asked of the server at all"
    for query in pb.queries:
        assert query["filter"].startswith('occurred_at < "'), query
        assert query["sort"] == "occurred_at", query


def test_an_unreadable_schema_falls_back_rather_than_refusing():
    # An unreadable schema is not the same as a schema with no date field.
    # The prune falls back to `created`; a wrong guess fails at the query
    # with a 400 rather than deleting anything.
    pb = FakePB(fields=("id", "created"), schema_readable=False)

    assert prune_module.which_date_field(pb, "sync_log") == "created"
