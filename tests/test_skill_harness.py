"""
Tests for tools/skill_harness.py.

Most of the harness talks to a live PocketBase, so these tests cover the
parts that decide what it looks at, plus a few behaviours that can be
checked with a stand-in database:

- which skills folder it reads, and that every route and category it knows
  names a skill that exists,
- which .env it reads credentials from, and what it says when there are
  none,
- that extra read-only skill folders are opt-in,
- what reference extraction does and does not find (Todoist ids only),
- that without a Todoist token refs are marked 'unknown' rather than
  broken.

The module computes its settings at import time, so each test imports a
fresh copy after setting the environment it needs.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tools" / "skill_harness.py"


def load():
    """Import the harness fresh and hand back the module.

    Loaded by path on every call rather than imported once, because the
    settings under test are worked out at import time from the environment.
    """
    was_off = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            "skill_harness_under_test", HARNESS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = was_off
    return module


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("SKILLS_WORKING_DIR", "SKILLS_LIVE_DIR", "SKILLS_READONLY_DIRS",
                 "SPLATT_ENV_FILE", "POCKETBASE_URL", "PB_ADMIN_EMAIL",
                 "PB_ADMIN_PASSWORD", "TODOIST_API_TOKEN", "TELEGRAM_BOT_TOKEN",
                 "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)


# ==================================================================
# Where it looks for skills
# ==================================================================

def test_it_reads_the_skills_in_this_repo(clean_env):
    module = load()

    assert module.WORKING_DIR == REPO / "skills", module.WORKING_DIR


def test_the_folder_it_reads_actually_exists(clean_env):
    module = load()

    assert module.WORKING_DIR.is_dir(), \
        "the harness points at %s, which is not here" % module.WORKING_DIR


def test_it_finds_the_skills_that_are_there(clean_env):
    # A real but empty folder would pass the test above and register
    # nothing, so check that it sees actual skills.
    module = load()
    found = [d.name for d in module.iter_skill_dirs(module.WORKING_DIR)]

    assert "splatt-ss-agent" in found
    assert len(found) >= 9, found


def test_every_route_names_a_skill_in_this_repo(clean_env):
    # `routes load` resolves names to registered skills; a name with no
    # folder would silently drop out of its route.
    module = load()

    for route in module.ROUTE_SEED:
        for name in route["skill_names"]:
            assert (module.WORKING_DIR / name / "SKILL.md").is_file(), \
                "route %s names %s, which is not in skills/" % (
                    route["operation_type"], name)


def test_every_category_names_a_skill_in_this_repo(clean_env):
    module = load()

    for name in module.CATEGORY_MAP:
        assert (module.WORKING_DIR / name / "SKILL.md").is_file(), name


def test_the_folder_can_be_overridden(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILLS_WORKING_DIR", str(tmp_path))
    module = load()

    assert module.WORKING_DIR == tmp_path


def test_the_live_folder_is_where_claude_loads_skills(clean_env):
    # bin/sync-skills copies the repo's skills here. The harness records
    # both paths so a drift between them can be spotted.
    module = load()

    assert module.LIVE_DIR == Path.home() / ".claude" / "skills"


def test_the_working_and_live_folders_are_not_the_same_place(clean_env):
    # With both names on one folder every comparison would be trivially
    # equal and drift could never be seen.
    module = load()

    assert module.WORKING_DIR != module.LIVE_DIR


def test_no_extra_skill_folder_is_read_unless_asked(clean_env):
    module = load()

    assert module.READONLY_SKILL_DIRS == []
    sources = module.all_skill_sources()
    assert sources and all(editable for _, editable in sources)


def test_extra_read_only_folders_come_from_the_environment(clean_env, monkeypatch, tmp_path):
    plugin = tmp_path / "plugin-skills"
    (plugin / "some-plugin-skill").mkdir(parents=True)
    (plugin / "some-plugin-skill" / "SKILL.md").write_text("---\nname: x\n---\n")
    monkeypatch.setenv("SKILLS_READONLY_DIRS", "%s, " % plugin)

    module = load()

    assert module.READONLY_SKILL_DIRS == [plugin]
    readonly = [d.name for d, editable in module.all_skill_sources() if not editable]
    assert readonly == ["some-plugin-skill"]


# ==================================================================
# It still runs
# ==================================================================

def test_the_file_parses():
    result = subprocess.run(
        [sys.executable, "-c",
         "import ast,sys; ast.parse(open(sys.argv[1]).read())", str(HARNESS)],
        capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_the_command_line_still_works():
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--help"],
        capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "audit" in result.stdout


def test_the_subcommands_the_morning_sweep_uses_are_all_present():
    # The agent's morning sweep script runs these unattended, so a renamed
    # subcommand would only show up as a failure in a log.
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--help"],
        capture_output=True, text=True)

    for needed in ("register", "sync-all", "audit", "validate"):
        assert needed in result.stdout, needed


def test_missing_credentials_are_reported_not_raised(clean_env, monkeypatch, tmp_path):
    # It runs unattended, so it must print a message that says what to do,
    # not a traceback. SPLATT_ENV_FILE pointed at nothing is the supported
    # way to load no env file at all.
    monkeypatch.setenv("HOME", "/tmp")
    monkeypatch.setenv("SPLATT_ENV_FILE", str(tmp_path / "there-is-no-env-here"))
    result = subprocess.run(
        [sys.executable, str(HARNESS), "status"],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items()
             if k not in ("PB_ADMIN_EMAIL", "PB_ADMIN_PASSWORD")},
    )

    assert "Traceback" not in result.stderr, result.stderr
    combined = result.stdout + result.stderr
    assert "PB_ADMIN_EMAIL" in combined


# ==================================================================
# Where it looks for the .env
# ==================================================================

def test_without_an_override_only_the_repo_env_is_searched(clean_env):
    module = load()

    assert module.SPLATT_ENV_CANDIDATES == [REPO / ".env"]


def test_an_explicit_override_is_the_only_candidate(clean_env, monkeypatch, tmp_path):
    # If the override merely ranked first, a caller pointing at a specific
    # file would silently fall through to another one when theirs was
    # missing, and load credentials for the wrong environment.
    target = tmp_path / "chosen.env"
    target.write_text("PB_ADMIN_EMAIL=picked\n")
    monkeypatch.setenv("SPLATT_ENV_FILE", str(target))

    module = load()

    assert module.SPLATT_ENV_CANDIDATES == [target], module.SPLATT_ENV_CANDIDATES
    assert module.PB_EMAIL == "picked"


def test_the_env_file_supplies_every_credential(clean_env, monkeypatch, tmp_path):
    target = tmp_path / "full.env"
    target.write_text(
        "# a comment\n"
        "PB_ADMIN_EMAIL=admin@example.com\n"
        'PB_ADMIN_PASSWORD="not-a-real-password"\n'
        "TODOIST_API_TOKEN='token-for-tests'\n"
        "TELEGRAM_BOT_TOKEN=bot\n"
        "TELEGRAM_CHAT_ID=123\n")
    monkeypatch.setenv("SPLATT_ENV_FILE", str(target))

    module = load()

    assert module.SPLATT_ENV == target
    assert module.PB_EMAIL == "admin@example.com"
    assert module.PB_PASSWORD == "not-a-real-password"
    assert module.TODOIST_API_TOKEN == "token-for-tests"
    assert (module.TELEGRAM_BOT_TOKEN, module.TELEGRAM_CHAT_ID) == ("bot", "123")


def test_the_environment_wins_over_the_env_file(clean_env, monkeypatch, tmp_path):
    target = tmp_path / "full.env"
    target.write_text("PB_ADMIN_EMAIL=from-file@example.com\n")
    monkeypatch.setenv("SPLATT_ENV_FILE", str(target))
    monkeypatch.setenv("PB_ADMIN_EMAIL", "from-env@example.com")

    module = load()

    assert module.PB_EMAIL == "from-env@example.com"


def test_the_failure_names_the_file_and_not_just_the_variables(clean_env, monkeypatch, tmp_path):
    # The usual cause of missing credentials is a missing or misplaced
    # .env, so the message says which file was looked for.
    monkeypatch.setenv("SPLATT_ENV_FILE", str(tmp_path / "absent.env"))
    module = load()

    with pytest.raises(module.PBError) as excinfo:
        module.PB().auth()

    message = str(excinfo.value)
    assert "looked in" in message, message
    assert "absent.env" in message, message


# ==================================================================
# What reference extraction finds
# ==================================================================

def test_only_todoist_ids_are_extracted(clean_env, tmp_path):
    # EMAIL_RE, PATH_RE and URL_RE exist but scan_refs does not use them,
    # as the module docstring says. If that changes, update the docstring.
    module = load()
    skill = tmp_path / "demo-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\n"
        "| Board | project id: 6Xproject0000001 |\n"
        "\n"
        "| Waiting | Section: 6Xsection0000004 |\n"
        "Write to operator@splatt.example.co.nz\n"
        "Files live in ~/Documents/Splatt/SS Folders\n"
        "Docs at https://docs.example.com/page\n")

    refs = module.scan_refs(skill)

    kinds = sorted((r["ref_type"], r["current_value"]) for r in refs)
    assert kinds == [("todoist_project", "6Xproject0000001"),
                     ("todoist_section", "6Xsection0000004")]
    assert all(r["status"] == "unknown" for r in refs)


class FakePB:
    """Stands in for the PocketBase client in cmd_validate."""

    def __init__(self, refs):
        self.refs = refs
        self.patches = []

    def auth(self):
        pass

    def list_refs(self, skill_id=None):
        return self.refs

    def _req(self, method, path, data=None, params=None):
        self.patches.append((method, path, data))
        return {}


def test_without_a_todoist_token_refs_are_unknown_not_broken(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_HARNESS_BUFFER", str(tmp_path / "buffer.jsonl"))
    module = load()
    module.TODOIST_API_TOKEN = ""
    pb = FakePB([
        {"id": "ref1", "ref_key": "todoist_project_6Xproject0000001",
         "ref_type": "todoist_project", "current_value": "6Xproject0000001"},
        {"id": "ref2", "ref_key": "todoist_section_6Xsection0000004",
         "ref_type": "todoist_section", "current_value": "6Xsection0000004"},
    ])

    code = module.cmd_validate(pb, argparse.Namespace(skill=None))

    assert code == 0
    assert [data["status"] for _, _, data in pb.patches] == ["unknown", "unknown"]
