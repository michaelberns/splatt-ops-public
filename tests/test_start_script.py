"""
Tests for bin/start, the script that turns a clone into a running stack.

Most of these drive check mode (`bin/start --check`). Check mode runs the
same discovery steps in the same order as a real start, but starts, stops
and writes nothing, so it can run anywhere. A real start binds ports 8090
and 8092 and launches PocketBase, which a test must not do, so the few
properties that only a real start could show are checked by reading the
script text instead ("grep tests").

Two helpers set up the environment:
    run_check()          the real repo, with an empty HOME, so the answers
                         do not depend on the machine running the tests.
    run_check_staged()   a copy of bin/start inside a bare folder, so a test
                         can decide exactly which pieces exist (a .env, a
                         pocketbase binary, pb_data) without touching the
                         real repo.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
START = REPO / "bin" / "start"


def _env(tmp_path, env_extra=None):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    # PATH is left alone on purpose: node and python3 are real requirements,
    # and faking them away would stop the tests noticing when they are gone.
    env.pop("SS_FOLDERS_PATH", None)
    if env_extra:
        env.update(env_extra)
    return env


def run_check(tmp_path, *args, env_extra=None):
    """Run the real bin/start in check mode with an empty HOME."""
    return subprocess.run(
        ["bash", str(START), "--check", *args],
        capture_output=True, text=True, env=_env(tmp_path, env_extra),
        cwd=str(REPO),
    )


def stage_repo(tmp_path, *, env_file=None, pocketbase=False, pb_data=False):
    """A bare folder holding a copy of bin/start and only the pieces asked for.

    The files server is always present (as a stub that check mode never
    runs), so the only blockers reported are the ones a test is about.
    """
    fake = tmp_path / "staged-repo"
    (fake / "bin").mkdir(parents=True, exist_ok=True)
    (fake / "server").mkdir(parents=True, exist_ok=True)
    (fake / "bin" / "start").write_bytes(START.read_bytes())
    (fake / "server" / "project-files-server.js").write_text(
        "// stand-in, never run in check mode\n", encoding="utf-8")
    if env_file is not None:
        (fake / ".env").write_text(env_file, encoding="utf-8")
    if pocketbase:
        binary = fake / "pocketbase"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    if pb_data:
        (fake / "pb_data").mkdir()
    return fake


def run_check_staged(tmp_path, fake, *args, env_extra=None):
    return subprocess.run(
        ["bash", str(fake / "bin" / "start"), "--check", *args],
        capture_output=True, text=True, env=_env(tmp_path, env_extra),
        cwd=str(fake),
    )


def code_lines(text):
    """The script's lines with comment lines removed."""
    return [ln for ln in text.splitlines() if not ln.strip().startswith("#")]


# ==================================================================
# The script itself
# ==================================================================

def test_the_script_is_valid_bash():
    # bash only reports a syntax error when it reaches it, which would be
    # at the moment the stack is needed.
    r = subprocess.run(["bash", "-n", str(START)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_script_is_executable():
    assert os.access(START, os.X_OK), "bin/start must be runnable directly"


@pytest.mark.parametrize("banned", ["\u2014", " -- ", "---"])
def test_the_wording_avoids_dashes(banned):
    # House style for everything the operator reads in a terminal: no long
    # dashes or double dashes in prose. Flags are written as flags.
    text = START.read_text(encoding="utf-8")
    assert banned not in text, f"{banned!r} appears in bin/start"


# ==================================================================
# Flags
# ==================================================================

def test_an_unknown_option_stops_rather_than_starting_the_stack(tmp_path):
    # A typo in a flag must not be read as "start everything".
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run(["bash", str(START), "--wat"],
                       capture_output=True, text=True, env=env, cwd=str(REPO))
    assert r.returncode == 2
    assert "Unknown option" in r.stdout
    assert "Splatt stack is running" not in r.stdout


def test_help_explains_every_flag_without_starting_anything(tmp_path):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    r = subprocess.run(["bash", str(START), "--help"],
                       capture_output=True, text=True, env=env, cwd=str(REPO))
    assert r.returncode == 0
    for flag in ("--check", "--no-sync", "--no-guards", "--no-dashboard",
                 "--only-if-down"):
        assert "bin/start %s" % flag in r.stdout, flag
    assert "127.0.0.1:8090" in r.stdout
    assert "Splatt stack is running" not in r.stdout


# ==================================================================
# Check mode on the real repo
# ==================================================================

def test_check_mode_starts_nothing(tmp_path):
    r = run_check(tmp_path)
    out = r.stdout + r.stderr
    # The lines a real start prints once it has launched something.
    assert "Splatt stack is running" not in out
    assert "Stopping anything left over" not in out
    assert "PIDs:" not in out


def test_check_mode_reports_every_component(tmp_path):
    # Every component gets a "check:" line whether it was found or not, so a
    # missing piece can never simply be absent from the report.
    r = run_check(tmp_path)
    out = r.stdout
    for label in ("check: repo", "check: python", "check: env",
                  "check: pocketbase", "check: pb data", "check: files server",
                  "check: node", "check: ss folders", "check: guard",
                  "check: sync", "check: dashboard"):
        assert label in out, f"{label!r} missing from:\n{out}"


def test_the_env_line_names_the_repo_file(tmp_path):
    r = run_check(tmp_path)
    env_lines = [ln for ln in r.stdout.splitlines() if "check: env" in ln]
    assert env_lines, r.stdout
    assert str(REPO / ".env") in env_lines[0]


def test_it_finds_the_files_server_in_this_repo(tmp_path):
    r = run_check(tmp_path)
    assert "server/project-files-server.js" in r.stdout


def test_it_finds_both_guards(tmp_path):
    r = run_check(tmp_path)
    assert "guards/ss-folders-guard.py" in r.stdout
    assert "guards/ss-deliverables-guard.py" in r.stdout


def test_it_finds_the_dashboard_in_this_repo(tmp_path):
    r = run_check(tmp_path)
    assert "dashboard/index.html" in r.stdout


def test_check_mode_names_the_sync_command(tmp_path):
    r = run_check(tmp_path)
    assert "daemon loop --write" in r.stdout


def test_it_does_not_kill_anything_in_check_mode(tmp_path):
    # Asking what is wrong must never stop a running stack.
    r = run_check(tmp_path)
    assert "Stopping anything left over" not in r.stdout


# ==================================================================
# A fresh clone
# ==================================================================

def test_a_fresh_clone_is_told_what_is_missing_and_where_to_read(tmp_path):
    # No .env and no database: exactly what a new clone looks like. Check
    # mode must list both problems in one run and point at the install doc.
    fake = stage_repo(tmp_path)
    r = run_check_staged(tmp_path, fake)
    assert r.returncode == 1
    assert "No .env in the repo" in r.stdout
    assert "No usable PocketBase install" in r.stdout
    # At least these two. The staged repo has no .venv, so the python3 on
    # PATH may add a third (missing imports).
    assert "things would stop the stack from starting" in r.stdout
    assert "docs/INSTALL.md" in r.stdout


def test_a_missing_pocketbase_is_a_blocker_not_a_warning(tmp_path):
    # Starting the rest of the stack without a database would bring up a
    # dashboard that looks merely empty rather than broken.
    fake = stage_repo(tmp_path, env_file="")
    r = run_check_staged(tmp_path, fake)
    assert r.returncode == 1
    assert "No usable PocketBase install" in r.stdout


def test_it_names_every_place_it_looked_for_pocketbase(tmp_path):
    # "Not found" without saying where it looked sends you guessing.
    fake = stage_repo(tmp_path, env_file="")
    r = run_check_staged(tmp_path, fake)
    assert str(fake / "pocketbase") in r.stdout
    assert "on PATH" in r.stdout
    assert str(fake / "pb_data") in r.stdout


def test_a_binary_without_a_database_folder_is_still_a_blocker(tmp_path):
    # pb_data is required up front, so a wrong path can never start an
    # empty database for the sync daemon to fill.
    fake = stage_repo(tmp_path, env_file="", pocketbase=True)
    r = run_check_staged(tmp_path, fake)
    assert r.returncode == 1
    assert "No usable PocketBase install" in r.stdout
    assert "not found: %s" % (fake / "pb_data") in r.stdout


def test_the_repo_binary_and_database_are_found(tmp_path):
    fake = stage_repo(tmp_path, env_file="", pocketbase=True, pb_data=True)
    r = run_check_staged(tmp_path, fake)
    assert "check: pocketbase   %s" % (fake / "pocketbase") in r.stdout
    assert "check: pb data      %s" % (fake / "pb_data") in r.stdout
    assert "No usable PocketBase install" not in r.stdout


def test_a_pocketbase_on_path_is_used_with_the_repo_database(tmp_path):
    fake = stage_repo(tmp_path, env_file="", pb_data=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "pocketbase"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    r = run_check_staged(
        tmp_path, fake,
        env_extra={"PATH": "%s:%s" % (bindir, os.environ.get("PATH", ""))})
    assert "check: pocketbase   %s" % binary in r.stdout
    assert "No usable PocketBase install" not in r.stdout


def test_the_repo_binary_is_preferred_over_one_on_path(tmp_path):
    fake = stage_repo(tmp_path, env_file="", pocketbase=True, pb_data=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    other = bindir / "pocketbase"
    other.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    other.chmod(0o755)
    r = run_check_staged(
        tmp_path, fake,
        env_extra={"PATH": "%s:%s" % (bindir, os.environ.get("PATH", ""))})
    assert "check: pocketbase   %s" % (fake / "pocketbase") in r.stdout


def test_a_binary_that_is_not_executable_says_how_to_fix_it(tmp_path):
    fake = stage_repo(tmp_path, env_file="", pb_data=True)
    (fake / "pocketbase").write_text("not a program\n", encoding="utf-8")
    r = run_check_staged(tmp_path, fake)
    assert "No usable PocketBase install" in r.stdout
    assert "chmod +x" in r.stdout


def test_a_complete_setup_reports_nothing_missing(tmp_path):
    folders = tmp_path / "SS Folders"
    folders.mkdir()
    fake = stage_repo(tmp_path, env_file="", pocketbase=True, pb_data=True)
    r = run_check_staged(tmp_path, fake,
                         env_extra={"SS_FOLDERS_PATH": str(folders)})
    # The staged repo has no .venv, so whether the imports pass depends on
    # the python3 on PATH. Everything else must be clean.
    if "cannot import" not in r.stdout:
        assert r.returncode == 0, r.stdout
        assert "Nothing is missing" in r.stdout
    assert "No usable PocketBase install" not in r.stdout
    assert "No .env" not in r.stdout


# ==================================================================
# SS Folders
# ==================================================================

def test_ss_folders_can_be_pointed_at_explicitly(tmp_path):
    folder = tmp_path / "elsewhere" / "SS Folders"
    folder.mkdir(parents=True)
    r = run_check(tmp_path, env_extra={"SS_FOLDERS_PATH": str(folder)})
    assert "check: ss folders   %s" % folder in r.stdout


def test_the_default_ss_folders_is_under_documents(tmp_path):
    r = run_check(tmp_path)
    expected = tmp_path / "home" / "Documents" / "Splatt" / "SS Folders"
    assert str(expected) in r.stdout


def test_it_says_when_ss_folders_is_missing(tmp_path):
    r = run_check(tmp_path)
    assert "SS Folders was not found" in r.stdout


def test_ss_folders_in_the_env_file_is_honoured_by_check(tmp_path):
    folder = tmp_path / "from-env"
    folder.mkdir()
    fake = stage_repo(tmp_path, env_file='SS_FOLDERS_PATH="%s"\n' % folder)
    r = run_check_staged(tmp_path, fake)
    assert "check: ss folders   %s" % folder in r.stdout


def test_the_command_line_folder_wins_over_the_env_file(tmp_path):
    # The demo run sets SS_FOLDERS_PATH on the command line. A value left
    # in .env must not quietly send it back to the real folder.
    in_env = tmp_path / "from-env"
    given = tmp_path / "given"
    in_env.mkdir()
    given.mkdir()
    fake = stage_repo(tmp_path, env_file='SS_FOLDERS_PATH="%s"\n' % in_env)
    r = run_check_staged(tmp_path, fake,
                         env_extra={"SS_FOLDERS_PATH": str(given)})
    assert "check: ss folders   %s" % given in r.stdout
    assert str(in_env) not in r.stdout


def test_the_folder_is_passed_to_the_files_server_and_both_guards():
    # A grep: check mode cannot show what environment a child process got.
    # Each launch line names the folder itself rather than relying on an
    # export somewhere above it.
    text = START.read_text(encoding="utf-8")
    launches = [ln for ln in code_lines(text)
                if re.search(r'node "\$PF_SERVER"|"\$PY" "\$(DELIV_)?GUARD_SCRIPT"', ln)]
    assert len(launches) == 3, launches
    for line in launches:
        assert 'SS_FOLDERS_PATH="$SS_FOLDERS"' in line, line


# ==================================================================
# --no-sync and --no-guards
# ==================================================================

def test_no_sync_says_nothing_will_poll_todoist(tmp_path):
    # Forgetting --no-sync shows up as a board that stops updating, so the
    # report says so plainly.
    r = run_check(tmp_path, "--no-sync")
    assert "you passed --no-sync" in r.stdout
    assert "Nothing will poll Todoist" in r.stdout


def test_no_guards_is_reported_and_neither_guard_is_listed(tmp_path):
    r = run_check(tmp_path, "--no-guards")
    assert "check: guards       skipped, you passed --no-guards" in r.stdout
    assert "guards/ss-folders-guard.py" not in r.stdout
    assert "guards/ss-deliverables-guard.py" not in r.stdout


def test_guards_are_only_launched_when_asked_for():
    # A grep, because a real start cannot run in a test. Both guards are
    # launched inside start_guards(), and start_guards is only called
    # behind the --no-guards switch.
    text = START.read_text(encoding="utf-8")
    lines = text.splitlines()
    begin = lines.index("start_guards() {")
    end = lines.index("}", begin)
    for number, line in enumerate(lines):
        if line.strip().startswith("#"):
            continue
        if re.search(r'"\$PY" "\$(DELIV_)?GUARD_SCRIPT"', line):
            assert begin < number < end, "guard launched outside start_guards: %s" % line
    calls = [ln for ln in code_lines(text)
             if "start_guards" in ln and "start_guards()" not in ln]
    assert calls, "start_guards is never called"
    for number, line in enumerate(lines):
        if line.strip() == "start_guards":
            assert lines[number - 1].strip() == 'if [ "$START_GUARDS" -eq 1 ]; then', line


# ==================================================================
# Things only a real start does, checked by reading the script
# ==================================================================

def test_pocketbase_listens_on_loopback_only():
    # The database and its admin UI must not be reachable from the network.
    text = START.read_text(encoding="utf-8")
    assert "0.0.0.0" not in text
    assert 'PB_ADDR="127.0.0.1:8090"' in text
    serves = [ln for ln in code_lines(text) if " serve " in ln]
    assert serves, "bin/start never starts PocketBase"
    for line in serves:
        assert '--http="$PB_ADDR"' in line, line


def test_it_never_asks_for_root():
    text = START.read_text(encoding="utf-8")
    assert not [ln for ln in code_lines(text) if re.search(r"\bsudo\b", ln)]


def test_a_leftover_loop_is_stopped_before_a_new_one_starts():
    # The loop takes a pid lock, so a copy left over from an earlier run
    # would stop the new one from starting.
    text = START.read_text(encoding="utf-8")
    assert 'pkill -f "daemon loop"' in text


def test_only_one_line_starts_the_loop():
    # The first start and every restart go through start_sync(), so there
    # is exactly one command line to keep right, and it writes.
    text = START.read_text(encoding="utf-8")
    starts = [ln for ln in code_lines(text) if '"$PY" -m daemon loop' in ln]
    assert len(starts) == 1, starts
    assert "--write" in starts[0], starts[0]


def test_nothing_is_launched_with_a_bare_python3():
    # Check mode cannot show which interpreter a background process got.
    # Every launch goes through "$PY", which prefers the repo's .venv and is
    # checked for its imports before anything starts. A bare python3 would
    # be whatever is first on PATH, with whatever happens to be installed.
    text = START.read_text(encoding="utf-8")

    offenders = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or "python3" not in stripped:
            continue
        # Choosing the interpreter, and naming it in messages, is allowed.
        if "command -v python3" in stripped or stripped.startswith("PY="):
            continue
        if stripped.startswith(("say ", "warn ", "fail ", "echo ", "blocker ")):
            continue
        offenders.append("%d: %s" % (number, stripped))

    assert not offenders, (
        "these lines run python3 directly instead of \"$PY\": %s" % offenders)


def test_the_interpreter_is_checked_for_its_imports_before_anything_starts():
    # An empty .venv (what a half finished install leaves behind) would be
    # picked and then fail quietly in the background. So the imports are
    # checked, and a missing one blocks the launch rather than warning.
    text = START.read_text(encoding="utf-8")

    assert "for module in httpx yaml" in text, (
        "bin/start no longer checks that its interpreter can import the "
        "two things the sync daemon needs")

    after = text.split("for module in httpx yaml", 1)[1]
    assert "blocker " in after.split("# ====", 1)[0], (
        "a missing import no longer stops the launch")


def test_ctrl_c_exits_instead_of_returning_to_the_supervise_loop():
    # A trap that only ran cleanup would return into the loop, which would
    # then restart everything cleanup had just stopped.
    text = START.read_text(encoding="utf-8")
    assert "trap cleanup EXIT" in text
    assert "trap 'exit 130' INT" in text
    assert "trap 'exit 143' TERM" in text
