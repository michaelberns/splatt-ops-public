"""
Tests for core/schema.py, the generated copy of the PocketBase schema.

The first six run anywhere and pin down `validate_payload`, the check
core/pb.py runs before every write. The last one compares schema.py with
a live PocketBase in both directions. It skips, with the reason shown,
when no server or credentials are configured (see
`tests.conftest_splatt.live_pocketbase`).
"""

from __future__ import annotations

from core import schema
from tests.conftest_splatt import live_pocketbase


def test_every_collection_has_fields():
    assert schema.SCHEMA, "schema.py is empty"
    for name, spec in schema.SCHEMA.items():
        assert spec.get("fields"), "collection %s has no fields" % name


def test_validate_payload_catches_unknown_field():
    problems = schema.validate_payload("clients", {"name": "Test", "code": "TST"})
    assert any("code" in p for p in problems), problems


def test_validate_payload_catches_the_original_bug():
    """Every field that does not exist on `clients` is reported by name.

    PocketBase would accept this payload with a 200 and silently drop
    full_name, primary_contact and entity_type, so the local check is the
    only thing that can refuse it.
    """
    payload = {
        "full_name": "Clearwater Bottling",
        "aliases": ["CB"],
        "primary_contact": "someone@example.com",
        "status": "active",
        "entity_type": "client",
        "notes": "",
    }
    problems = schema.validate_payload("clients", payload)
    for bad in ("full_name", "primary_contact", "entity_type"):
        assert any(bad in p for p in problems), "%s should have been rejected" % bad


def test_validate_payload_accepts_a_good_client():
    problems = schema.validate_payload(
        "clients", {"name": "Clearwater Bottling", "status": "active", "notes": ""}
    )
    assert problems == [], problems


def test_validate_payload_catches_bad_select_value():
    problems = schema.validate_payload("jobs", {"title": "x", "status": "invoyced"})
    assert any("status" in p for p in problems), problems


def test_required_fields_are_enforced_on_create():
    problems = schema.validate_payload("clients", {"status": "active"})
    assert any("name" in p for p in problems), problems


def test_no_schema_drift_against_live():
    """schema.py and the live server list the same fields, both directions.

    A field added in the PocketBase admin UI without regenerating
    schema.py (tools/introspect_schema.py) makes this fail, before a write
    that relies on the stale copy is refused or silently loses a value.
    """
    pb = live_pocketbase()

    live = {}
    for coll in pb.collections():
        name = coll.get("name")
        if not name or name.startswith("_"):
            continue
        live[name] = {
            f.get("name")
            for f in (coll.get("fields") or coll.get("schema") or [])
            if f.get("name")
        } - {"id", "created", "updated", "collectionId", "collectionName"}
    pb.close()

    problems = []
    for collection, spec in schema.SCHEMA.items():
        if collection not in live:
            problems.append("%s is in schema.py but not on the server" % collection)
            continue
        coded = set(spec["fields"])
        for field in sorted(coded - live[collection]):
            problems.append("%s.%s is in code but not on the server" % (collection, field))
        for field in sorted(live[collection] - coded):
            problems.append("%s.%s is on the server but not in code" % (collection, field))

    assert not problems, "schema drift:\n  " + "\n  ".join(problems)
