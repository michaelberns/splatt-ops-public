"""
Tests for the two install helpers: tools/seed_demo.py and tools/todoist_setup.py.

Neither test touches the network. The seed tests check the demo data
against core/schema.py offline, which is the same check core/pb.py makes
before a real write. If someone changes the schema and forgets the demo,
these tests fail before a new user ever sees a half-created demo.
"""

from __future__ import annotations

from core import interactions, schema
from tools import seed_demo, todoist_setup


# ── seed_demo ────────────────────────────────────────────────────────────

def _resolve_all():
    """Resolve every demo record in creation order, using fake ids.

    Returns a list of (collection, resolved payload). Resolving in order
    also proves that no record points at something created after it.
    """
    ids = {}
    out = []
    for collection, items in seed_demo.PLAN:
        for key, payload in items:
            resolved = seed_demo.resolve(payload, ids)
            ids[(collection, key)] = "id_%s_%s" % (collection, key)
            out.append((collection, resolved))
    for key, payload in seed_demo.financial_lines():
        out.append(("project_financials", seed_demo.resolve(payload, ids)))
    for _, fields in seed_demo.INTERACTIONS:
        out.append(("interactions", seed_demo.resolve(fields, ids)))
    return out


def test_every_demo_record_passes_the_schema_check():
    for collection, payload in _resolve_all():
        problems = schema.validate_payload(collection, payload)
        assert problems == [], (collection, payload.get("name") or payload.get("title"), problems)


def test_no_demo_record_refers_to_something_created_later():
    # _resolve_all raises KeyError on a forward reference.
    assert len(_resolve_all()) > 50


def test_demo_interactions_only_use_fields_the_interaction_writer_accepts():
    for _, fields in seed_demo.INTERACTIONS:
        assert set(fields) <= interactions.WRITABLE


def test_resolve_refuses_a_reference_to_a_missing_record():
    try:
        seed_demo.resolve({"client": "@clients:nobody"}, {})
    except KeyError as exc:
        assert "@clients:nobody" in str(exc)
    else:
        raise AssertionError("a dangling reference must raise")


def test_the_pin_line_is_the_format_the_real_due_parser_reads():
    from core import realdue
    line = seed_demo.pin(-4)
    parsed = realdue.parse(line)
    assert parsed is not None
    assert str(getattr(parsed, "date", parsed)).startswith(seed_demo.day(-4))


def test_dry_run_writes_nothing_and_needs_no_database(capsys):
    assert seed_demo.main([]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_demo_data_is_fictional():
    text = repr(seed_demo.CLIENTS + seed_demo.CONTACTS + seed_demo.SUPPLIERS).lower()
    for domain_part in ("@", "http"):
        for value in text.split():
            if domain_part in value and "example" not in value and "@clients" not in value:
                raise AssertionError("non-example address in demo data: %s" % value)


# ── todoist_setup ────────────────────────────────────────────────────────

def test_sections_are_found_by_the_words_in_their_names():
    existing = [
        {"id": "1", "name": "📌 Today"},
        {"id": "2", "name": "Overdue"},
        {"id": "5", "name": "⏳ Waiting on client"},
        {"id": "6", "name": "📦 Waiting on Supplier"},
    ]
    found = todoist_setup.match_sections(existing)
    assert found == {"today": "1", "overdue": "2", "waiting_client": "5", "waiting_supplier": "6"}


def test_created_section_names_are_recognised_by_the_same_matcher():
    created = [{"id": str(i), "name": name} for i, (_, name, _) in enumerate(todoist_setup.SECTIONS)]
    found = todoist_setup.match_sections(created)
    assert len(found) == len(todoist_setup.SECTIONS)


def test_save_only_changes_the_id_lines(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "# a comment that must survive\n"
        "todoist:\n"
        "  token_env: TODOIST_API_TOKEN\n"
        '  project_id: ""   # filled in by tools.todoist_setup\n'
        "  project_name: Splatt Ops\n"
        "  sections:\n"
        '    today: ""\n'
        '    overdue: ""\n'
        "  timeout_seconds: 15\n"
        "telegram:\n"
        '  project_id: "not this one"\n',
        encoding="utf-8",
    )
    todoist_setup.save_ids("P1", {"today": "S1", "overdue": "S2"}, path=path)
    text = path.read_text(encoding="utf-8")
    assert "# a comment that must survive" in text
    assert '  project_id: "P1"   # filled in by tools.todoist_setup' in text
    assert '    today: "S1"' in text
    assert '    overdue: "S2"' in text
    assert '  project_id: "not this one"' in text  # outside the todoist block
    assert "  timeout_seconds: 15" in text


def test_the_yaml_block_lists_all_seven_sections():
    block = todoist_setup.yaml_block("P1", {"today": "S1"})
    for key, _, _ in todoist_setup.SECTIONS:
        assert "    %s:" % key in block
