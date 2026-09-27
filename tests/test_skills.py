"""
Tests for the skill files in skills/.

What is tested
    Skills are Markdown instruction files plus two scripts, so most of this
    file checks agreement: every skill folder has a SKILL.md whose declared
    name matches the folder, no skill carries a secret, a real-looking id or
    an absolute path from one machine, and no skill points at a file that
    has been deleted.

    Two things are real behaviour and are tested by running them:

    1. Where scripts/send_telegram.py reads its Telegram credentials.
       The order is: the process environment, then the file named by
       SPLATT_ENV_FILE, then the repo's own .env. The script finds "the
       repo" from its own location, so the tests copy it into a fake repo
       under tmp_path and import it from there. Nothing outside tmp_path
       is read.
    2. What scripts/morning-sweep.sh runs and logs. It is copied into a
       fake repo in the same way, next to a stub harness, and executed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"

SCRIPTS_REL = Path("skills") / "splatt-ss-agent" / "scripts"
TELEGRAM = REPO / SCRIPTS_REL / "send_telegram.py"
SWEEP = REPO / SCRIPTS_REL / "morning-sweep.sh"

CREDENTIAL_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                   "TELEGRAM_BOT_TOKEN_OPS", "TELEGRAM_CHAT_ID_OPS")


def skill_dirs():
    return sorted(p for p in SKILLS.iterdir() if p.is_dir())


# Build artifacts and OS clutter are not source. A .pyc holds the string
# literals of the file it was compiled from, so scanning it would report
# every finding twice.
JUNK = ("__pycache__", ".DS_Store", ".pyc")


def source_files():
    for path in SKILLS.rglob("*"):
        if not path.is_file():
            continue
        if any(bit in str(path) for bit in JUNK):
            continue
        yield path


def text(path: Path) -> str:
    return path.read_text()


# ==================================================================
# A fake repo to run the scripts in
# ==================================================================

def make_fake_repo(root: Path) -> Path:
    """Copy both scripts into root/skills/splatt-ss-agent/scripts/."""
    scripts = root / SCRIPTS_REL
    scripts.mkdir(parents=True)
    shutil.copy2(TELEGRAM, scripts / TELEGRAM.name)
    shutil.copy2(SWEEP, scripts / SWEEP.name)
    return root


def write_env(path: Path, token: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "TELEGRAM_BOT_TOKEN=%s\n"
        "TELEGRAM_CHAT_ID=chat-%s\n"
        "TELEGRAM_BOT_TOKEN_OPS=%s-ops\n"
        "TELEGRAM_CHAT_ID_OPS=chat-%s-ops\n" % (token, token, token, token)
    )
    return path


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """An empty repo with the scripts in it, and no credentials anywhere."""
    monkeypatch.delenv("SPLATT_ENV_FILE", raising=False)
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    return make_fake_repo(tmp_path / "repo")


def load_telegram(repo: Path):
    """Import the copy of send_telegram.py that sits inside `repo`.

    The candidate list is built at import time, so any environment change
    has to happen before this is called. Bytecode writing is turned off so
    importing the script does not leave a __pycache__ behind.
    """
    was_off = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            "send_telegram_under_test", repo / SCRIPTS_REL / "send_telegram.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = was_off
    return module


# ==================================================================
# send_telegram.py: where the credentials come from
# ==================================================================

def test_the_repo_root_is_found_from_the_script_location(fake_repo):
    module = load_telegram(fake_repo)
    assert module.REPO_ROOT == fake_repo.resolve()


def test_the_repo_env_is_used_when_nothing_overrides_it(fake_repo):
    env = write_env(fake_repo / ".env", "repo")
    module = load_telegram(fake_repo)
    assert module._find_env_file() == env.resolve()


def test_an_explicit_override_file_beats_the_repo_env(fake_repo, tmp_path,
                                                       monkeypatch):
    write_env(fake_repo / ".env", "repo")
    chosen = write_env(tmp_path / "elsewhere" / ".env", "explicit")
    monkeypatch.setenv("SPLATT_ENV_FILE", str(chosen))

    module = load_telegram(fake_repo)

    assert module._find_env_file() == chosen
    token, chat_id, _ = module.load_credentials(use_ops_channel=False)
    assert (token, chat_id) == ("explicit", "chat-explicit")


def test_an_override_that_does_not_exist_falls_through_to_the_repo(
        fake_repo, tmp_path, monkeypatch):
    env = write_env(fake_repo / ".env", "repo")
    monkeypatch.setenv("SPLATT_ENV_FILE", str(tmp_path / "missing.env"))

    module = load_telegram(fake_repo)

    assert module._find_env_file() == env.resolve()


def test_the_process_environment_beats_every_file(fake_repo, tmp_path,
                                                  monkeypatch):
    # This is how the daemon passes credentials to a child process without
    # writing them anywhere, so it must win over both files.
    write_env(fake_repo / ".env", "repo")
    chosen = write_env(tmp_path / "override.env", "explicit")
    monkeypatch.setenv("SPLATT_ENV_FILE", str(chosen))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-the-environment")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-from-the-environment")

    module = load_telegram(fake_repo)
    token, chat_id, _ = module.load_credentials(use_ops_channel=False)

    assert token == "from-the-environment"
    assert chat_id == "chat-from-the-environment"


def test_the_general_channel_reads_the_general_pair(fake_repo):
    write_env(fake_repo / ".env", "repo")
    module = load_telegram(fake_repo)
    token, chat_id, label = module.load_credentials(use_ops_channel=False)

    assert (token, chat_id, label) == ("repo", "chat-repo", "regular")


def test_the_ops_channel_reads_its_own_pair(fake_repo):
    # Reading the general pair here would post an ops summary in the wrong
    # channel and still report success.
    write_env(fake_repo / ".env", "repo")
    module = load_telegram(fake_repo)
    token, chat_id, label = module.load_credentials(use_ops_channel=True)

    assert (token, chat_id, label) == ("repo-ops", "chat-repo-ops", "ops")


def test_ops_summary_and_validation_are_the_ops_channel_types(fake_repo):
    module = load_telegram(fake_repo)
    assert module.OPS_CHANNEL_TYPES == {"ops_summary", "validation"}


def test_nothing_anywhere_returns_none_rather_than_raising(fake_repo):
    # An empty machine must produce the "not set, here is where I looked"
    # message, not a traceback from a path that does not exist.
    module = load_telegram(fake_repo)
    assert module._find_env_file() is None


def test_missing_credentials_exit_with_the_paths_that_were_checked(fake_repo):
    module = load_telegram(fake_repo)
    with pytest.raises(SystemExit) as caught:
        module.load_credentials(use_ops_channel=False)
    assert str(fake_repo / ".env") in str(caught.value)


def test_every_candidate_is_inside_the_fake_repo(fake_repo, tmp_path):
    # If any candidate pointed at a fixed location on the machine running
    # the tests, what they find would depend on whose computer it is.
    module = load_telegram(fake_repo)
    escapees = [str(p) for p in module.ENV_CANDIDATES
                if not str(p.resolve()).startswith(str(tmp_path.resolve()))]
    assert not escapees, escapees


# ==================================================================
# send_telegram.py: the outbox fallback
# ==================================================================

def test_a_failed_send_is_queued_in_the_repo_outbox(fake_repo, monkeypatch):
    write_env(fake_repo / ".env", "repo")
    module = load_telegram(fake_repo)

    def refuse(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(module.urllib.request, "urlopen", refuse)
    result = module.send_message("hello", msg_type="ops_summary")

    assert result["ok"] is True
    assert result["queued"] is True
    queued = Path(result["path"])
    assert queued.parent == (fake_repo / "state" / "telegram-outbox").resolve()
    assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}\.json", queued.name)

    entry = json.loads(queued.read_text())
    assert entry["channel"] == "ops"
    assert entry["type"] == "ops_summary"
    assert entry["text"].endswith("hello")
    assert entry["attempts"] == 0
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00",
        entry["queued_at"]), entry["queued_at"]


def test_a_general_message_is_queued_on_the_general_channel(fake_repo,
                                                           monkeypatch):
    write_env(fake_repo / ".env", "repo")
    module = load_telegram(fake_repo)
    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))

    result = module.send_message("hello", msg_type="alert")

    entry = json.loads(Path(result["path"]).read_text())
    assert entry["channel"] == "general"


def test_with_no_env_there_is_nowhere_to_queue(fake_repo):
    module = load_telegram(fake_repo)
    assert module._queue_fallback("x", "info", "regular", silent=False) is None


# ==================================================================
# morning-sweep.sh
# ==================================================================

STUB_HARNESS = (
    "import os, sys\n"
    "print('stub ran from', os.getcwd())\n"
    "print('token=' + os.environ.get('TELEGRAM_BOT_TOKEN', ''))\n"
    "sys.exit(int(os.environ.get('STUB_EXIT', '0')))\n"
)


def run_sweep(repo: Path, tmp_path: Path, exit_code: int = 0,
              harness: bool = True, **extra_env):
    """Run the copy of morning-sweep.sh inside `repo`, return (result, log)."""
    if harness:
        stub = repo / "tools" / "skill_harness.py"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(STUB_HARNESS)
    logs = tmp_path / "logs"
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "SKILL_HARNESS_LOG_DIR": str(logs),
        "STUB_EXIT": str(exit_code),
    }
    env.update(extra_env)
    result = subprocess.run(
        ["bash", str(repo / SCRIPTS_REL / "morning-sweep.sh")],
        capture_output=True, text=True, env=env)
    log_file = logs / "skill-harness-sweep.log"
    log = log_file.read_text() if log_file.exists() else ""
    return result, log


def test_the_sweep_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SWEEP)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_sweep_runs_the_harness_in_its_own_repo(tmp_path):
    repo = make_fake_repo(tmp_path / "repo")
    result, log = run_sweep(repo, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "stub ran from %s" % (repo / "tools").resolve() in log, log


def test_the_sweep_loads_the_repo_env(tmp_path):
    repo = make_fake_repo(tmp_path / "repo")
    write_env(repo / ".env", "repo")

    _, log = run_sweep(repo, tmp_path)

    assert "token=repo" in log, log


def test_an_explicit_env_override_wins_in_the_sweep(tmp_path):
    repo = make_fake_repo(tmp_path / "repo")
    write_env(repo / ".env", "repo")
    chosen = write_env(tmp_path / "somewhere-else.env", "explicit")

    _, log = run_sweep(repo, tmp_path, SPLATT_ENV_FILE=str(chosen))

    assert "token=explicit" in log, log


def test_a_missing_env_is_a_warning_that_names_the_repo_path(tmp_path):
    repo = make_fake_repo(tmp_path / "repo")

    result, log = run_sweep(repo, tmp_path)

    assert result.returncode == 0
    assert "WARN: env file not found at %s" % (repo.resolve() / ".env") in log, log


def test_a_missing_harness_fails_and_names_the_path(tmp_path):
    repo = make_fake_repo(tmp_path / "repo")

    result, log = run_sweep(repo, tmp_path, harness=False)

    assert result.returncode == 1
    wanted = repo.resolve() / "tools" / "skill_harness.py"
    assert "harness script not found at %s" % wanted in log, log


def test_an_explicit_harness_override_wins(tmp_path):
    repo = make_fake_repo(tmp_path / "repo")
    other = tmp_path / "other" / "harness.py"
    other.parent.mkdir()
    other.write_text("print('the other harness')\n")

    _, log = run_sweep(repo, tmp_path, SKILL_HARNESS_SCRIPT=str(other))

    assert "the other harness" in log, log
    assert "stub ran" not in log, log


@pytest.mark.parametrize("code", [0, 2, 3])
def test_the_sweep_logs_a_finish_line_whatever_the_harness_returns(tmp_path, code):
    # Exit 2 (PocketBase down) and 3 (stale references) are the runs
    # somebody opens the log for, so the finish line must be there too.
    repo = make_fake_repo(tmp_path / "repo")
    result, log = run_sweep(repo, tmp_path, exit_code=code)

    assert "morning sweep finished, exit %d" % code in log, log
    assert result.returncode == code, result.stderr


def test_the_sweep_has_no_second_harness_or_env_location():
    # The repo copy is the only harness and the repo .env the only default.
    body = text(SWEEP)
    assert "HARNESS_CANDIDATES" not in body
    assert "ENV_CANDIDATES" not in body
    assert "Documents/" not in body


# ==================================================================
# The skills as a set
# ==================================================================

def test_every_skill_has_a_skill_file():
    for d in skill_dirs():
        assert (d / "SKILL.md").exists(), "%s has no SKILL.md" % d.name


def test_every_skill_declares_a_name_and_a_description():
    # The description is the only thing deciding whether a skill is ever
    # loaded. A skill without one is never used and nothing reports it.
    for d in skill_dirs():
        head = (d / "SKILL.md").read_text()[:4000]
        assert head.startswith("---\n"), d.name
        assert re.search(r"^name:\s*\S", head, re.MULTILINE), d.name
        assert re.search(r"^description:\s*\S", head, re.MULTILINE), d.name


def test_the_declared_name_matches_the_folder():
    # Skills are addressed by folder name. A mismatch loads a skill under a
    # name nothing calls.
    for d in skill_dirs():
        head = (d / "SKILL.md").read_text()[:4000]
        declared = re.search(r"^name:\s*(\S+)", head, re.MULTILINE)
        assert declared, d.name
        assert declared.group(1).strip("'\"") == d.name


def test_the_python_scripts_compile():
    for path in source_files():
        if path.suffix == ".py":
            compile(path.read_text(), str(path), "exec")


def test_the_shell_scripts_are_valid_bash():
    for path in source_files():
        if path.suffix == ".sh":
            result = subprocess.run(["bash", "-n", str(path)],
                                    capture_output=True, text=True)
            assert result.returncode == 0, (path, result.stderr)


def test_no_skill_carries_a_secret():
    # These files are committed. A token in one is a token in the history.
    for path in source_files():
        body = path.read_text(errors="ignore")
        assert not re.search(r"[0-9]{9,10}:AA[\w-]{30,}", body), \
            "%s looks like it contains a Telegram bot token" % path
        assert not re.search(r"\b[a-f0-9]{40}\b", body), \
            "%s contains a 40 character hex string, which is what the " \
            "Todoist token looks like" % path


def test_no_skill_hard_codes_a_todoist_id():
    # Todoist project and section ids are 16 characters starting with 6.
    # They live only in config/settings.yaml. A copy in a skill goes stale
    # the day the board is rebuilt and nothing says so.
    todoist_id = re.compile(r"\b6[A-Za-z0-9]{15}\b")
    offenders = []
    for path in source_files():
        for match in todoist_id.finditer(path.read_text(errors="ignore")):
            offenders.append("%s: %s" % (path.relative_to(REPO), match.group()))
    assert offenders == [], offenders


# Files and skills that no longer exist. A skill telling the agent to run
# or read one of these sends it looking for something that is not there.
DELETED = [
    "docs/SS-AGENT-HOOK.md", "docs/MIGRATION-PLAN.md",
    "docs/DELETING-THE-OLD-CODE.md", "docs/RUNNING-ELSEWHERE.md", "docs/plans",
    "sandbox-bootstrap", "pb-health", "publish-mac-ip", "finish-migration",
    "retire-old-code", "migrate-database", "pb-network-setup", "Push.command",
    "start.command", "install_mcp_server", "compare_sync", "backfill_real_due",
    "migrate_job_status_values", "map_tasks", "dedupe_interactions",
    "link_interactions", "merge_gmail_duplicates", "merge_clients",
    "splatt-ops-manager", "splatt-morning-routine", "validator_path",
]


def test_no_skill_references_a_deleted_file():
    offenders = []
    for path in source_files():
        body = path.read_text(errors="ignore")
        for name in DELETED:
            if name in body:
                offenders.append("%s names %s" % (path.relative_to(REPO), name))
    assert offenders == [], offenders


def test_no_skill_names_an_absolute_path_from_one_machine():
    # Paths are repo relative, or the ~/Documents/Splatt default that comes
    # from config/settings.yaml. A home folder, a mount point or a session
    # path only exists on the machine it was copied from.
    machine_path = re.compile(r"/Users/|/home/\w|/sessions/|/mnt/")
    offenders = []
    for path in source_files():
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if machine_path.search(line):
                offenders.append("%s:%d" % (path.relative_to(REPO), n))
    assert offenders == [], offenders


def test_every_email_address_in_a_skill_is_an_example_address():
    email = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
    offenders = []
    for path in source_files():
        for match in email.finditer(path.read_text(errors="ignore")):
            if "example" not in match.group(1).split("."):
                offenders.append("%s: %s" % (path.relative_to(REPO), match.group()))
    assert offenders == [], offenders


def test_only_the_notifier_talks_to_the_telegram_api():
    # One way to send, so every message gets the outbox fallback.
    offenders = [str(p.relative_to(REPO)) for p in source_files()
                 if p != TELEGRAM
                 and "api.telegram.org" in p.read_text(errors="ignore")]
    assert offenders == [], offenders


# Repo files a skill may name before they exist, because they are part of
# the public edition and are written alongside these skills.
PROMISED = {"docs/HARNESSES.md", "docs/ARCHITECTURE.md", "docs/INSTALL.md",
            "tools/todoist_setup.py", "tools/seed_demo.py"}


def test_every_repo_path_a_skill_names_exists():
    # `bin/x`, `tools/x.py`, `core/x.py`, `config/x`, `docs/x.md` and
    # `python -m tools.x` in a skill are instructions to run or read that
    # file, so each one has to be there.
    path_ref = re.compile(
        r"(?<![\w/.-])((?:bin|tools|core|config|docs|daemon|validator)"
        r"/[A-Za-z0-9_.-]+)")
    module_ref = re.compile(r"python3? -m (tools\.[a-z_]+)")
    missing = set()
    for path in source_files():
        if path.suffix != ".md":
            continue
        body = path.read_text(errors="ignore")
        found = set(path_ref.findall(body))
        found |= {m.replace(".", "/") + ".py" for m in module_ref.findall(body)}
        for ref in found:
            ref = ref.rstrip(".")
            if ref in PROMISED or (REPO / ref).exists():
                continue
            missing.add("%s names %s" % (path.relative_to(REPO), ref))
    assert sorted(missing) == [], sorted(missing)
