"""
Tests for the folders guard's scan boundaries.

The guard walks ~/Desktop, ~/Downloads and ~/Documents looking for stray
PROJECT.md files. The repository itself may be cloned inside one of those
folders, and it ships example PROJECT.md files under examples/SS Folders/.
These tests pin down that the guard reports a real stray but never treats
a file inside its own repository as one.

The guard is run in --audit mode, which reports and moves nothing, against
a throwaway HOME, so nothing on the real machine is scanned or touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _audit(tmp_path):
    """Put a copy of the guard in a fake repo under ~/Documents and run --audit.

    Returns the list of stray paths the guard reported.
    """
    home = tmp_path / "home"
    documents = home / "Documents"
    fake_repo = documents / "splatt-ops"
    (fake_repo / "guards").mkdir(parents=True)
    shutil.copy(REPO / "guards" / "ss-folders-guard.py", fake_repo / "guards" / "ss-folders-guard.py")

    # An example project file inside the repo: must be ignored.
    example = fake_repo / "examples" / "SS Folders" / "Demo Client" / "Demo Project"
    example.mkdir(parents=True)
    (example / "PROJECT.md").write_text("# Demo Client - Demo Project\n")

    # A genuine stray outside the repo: must be reported.
    stray = documents / "Some Client - Some Job"
    stray.mkdir()
    (stray / "PROJECT.md").write_text("# Some Client - Some Job\n")

    ss_folders = tmp_path / "ss"
    ss_folders.mkdir()
    env = dict(os.environ, HOME=str(home), SS_FOLDERS_PATH=str(ss_folders))
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)
    result = subprocess.run(
        [sys.executable, str(fake_repo / "guards" / "ss-folders-guard.py"), "--audit"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    start = result.stdout.index("{")
    summary = json.loads(result.stdout[start:])
    found = [p for p in summary.get("new_strays", []) if str(home) in p]
    return found, stray, example


def test_a_stray_outside_the_repo_is_reported(tmp_path):
    found, stray, _ = _audit(tmp_path)
    assert any(p.endswith(str(stray / "PROJECT.md")) or "Some Client - Some Job" in p for p in found), found


def test_example_files_inside_the_repo_are_never_reported(tmp_path):
    found, _, example = _audit(tmp_path)
    assert not any("examples" in p for p in found), found
