"""
Todoist setup: create the project board this system expects, and record its ids.

What it is for
    The daemon, the overdue engine and the validator all work on one
    Todoist project that is laid out as a board with seven sections. A
    task's section is its state ("whose move is it"), so the code needs
    the exact id of every section. Those ids live in one place,
    config/settings.yaml under `todoist:`, and nothing works until they are
    filled in. This script does that setup for you.

How it works
    1. Reads the API token from .env (TODOIST_API_TOKEN) and the project
       name from config/settings.yaml (todoist.project_name).
    2. Looks for a project with that name, and for each of the seven
       sections inside it (matched by the words in the name, so a section
       you have renamed "🔴 Overdue" or just "Overdue" is still found).
    3. Without --write it only reports what exists and what is missing.
       With --write it creates whatever is missing. It never renames,
       moves or deletes anything that is already there.
    4. Prints the ids as a YAML block. With --save it also writes them
       into config/settings.yaml, changing only the id lines.

The seven sections
    today, overdue, upcoming       the operator's own work, by date
    waiting_client                 waiting for a client to reply
    waiting_supplier               waiting for a supplier to reply
    stalled                        chased twice with no reply, or 28 days old
    backlog                        not scheduled yet

Usage
    python -m tools.todoist_setup                  report only
    python -m tools.todoist_setup --write          create what is missing
    python -m tools.todoist_setup --write --save   ...and save the ids

Exit codes
    0  done (or report printed)
    1  something is missing and --write was not given
    2  could not run (no token, or Todoist unreachable)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import httpx

from core.config import ConfigError, settings

API = "https://api.todoist.com/api/v1"

# (settings key, name to create, words that identify an existing section).
# The dashboard and the overdue engine recognise a section by these words,
# so a created section always contains them.
SECTIONS = [
    ("today", "📌 Today", "today"),
    ("overdue", "🔴 Overdue", "overdue"),
    ("upcoming", "📅 Upcoming", "upcoming"),
    ("waiting_client", "⏳ Waiting on Client", "waiting on client"),
    ("waiting_supplier", "📦 Waiting on Supplier", "waiting on supplier"),
    ("stalled", "🧊 Stalled", "stalled"),
    ("backlog", "🗂 Backlog", "backlog"),
]

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


class Todoist:
    """The four Todoist calls this script needs, and nothing else."""

    def __init__(self, token, timeout=15.0):
        self.http = httpx.Client(
            base_url=API, timeout=timeout, headers={"Authorization": "Bearer %s" % token}
        )

    def _all(self, path, params=None):
        """GET a list endpoint and follow its pagination cursor to the end."""
        params = dict(params or {})
        items = []
        while True:
            resp = self.http.get(path, params=params)
            resp.raise_for_status()
            body = resp.json()
            items.extend(body.get("results", []))
            cursor = body.get("next_cursor")
            if not cursor:
                return items
            params["cursor"] = cursor

    def projects(self):
        return self._all("/projects")

    def sections(self, project_id):
        return self._all("/sections", {"project_id": project_id})

    def create_project(self, name):
        resp = self.http.post("/projects", json={"name": name})
        resp.raise_for_status()
        return resp.json()

    def create_section(self, project_id, name):
        resp = self.http.post("/sections", json={"project_id": project_id, "name": name})
        resp.raise_for_status()
        return resp.json()


def match_sections(existing):
    """Map each settings key to an existing section id, where one is found.

    `existing` is the list of section objects Todoist returned. A section
    matches when its lower-cased name contains the identifying words. The
    two waiting sections are identified by "waiting on client" and
    "waiting on supplier" rather than by "waiting" alone, so they can
    never be confused with each other.
    """
    found = {}
    for key, _, words in SECTIONS:
        for section in existing:
            if words in str(section.get("name", "")).lower():
                found[key] = str(section["id"])
                break
    return found


def yaml_block(project_id, section_ids):
    """The ids formatted exactly as they sit in config/settings.yaml."""
    lines = ["todoist:", '  project_id: "%s"' % project_id, "  sections:"]
    for key, _, _ in SECTIONS:
        lines.append('    %s: "%s"' % (key, section_ids.get(key, "")))
    return "\n".join(lines)


def save_ids(project_id, section_ids, path=SETTINGS_PATH):
    """Write the ids into settings.yaml, touching only the id lines.

    The file is edited line by line rather than parsed and re-dumped,
    because re-dumping YAML would throw away every comment in the file.
    Only lines inside the `todoist:` block are changed.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_todoist = False
    in_sections = False
    for i, line in enumerate(lines):
        if re.match(r"^todoist:\s*$", line):
            in_todoist = True
            continue
        if in_todoist and re.match(r"^\S", line):
            break  # the next top-level key: the todoist block has ended
        if not in_todoist:
            continue
        if re.match(r"^  sections:\s*$", line):
            in_sections = True
            continue
        if re.match(r"^  \S", line):
            in_sections = False
        m = re.match(r"^(  project_id:)\s*.*?(\s*#.*)?$", line.rstrip("\n"))
        if m and not in_sections:
            lines[i] = '%s "%s"%s\n' % (m.group(1), project_id, m.group(2) or "")
            continue
        m = re.match(r"^(    (\w+):)\s*.*?(\s*#.*)?$", line.rstrip("\n"))
        if m and in_sections and m.group(2) in section_ids:
            lines[i] = '%s "%s"%s\n' % (m.group(1), section_ids[m.group(2)], m.group(3) or "")
    path.write_text("".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create the Todoist board and record its ids.")
    parser.add_argument("--write", action="store_true", help="create the missing project and sections")
    parser.add_argument("--save", action="store_true", help="also write the ids into config/settings.yaml")
    args = parser.parse_args(argv)

    try:
        conf = settings()
        token = conf.todoist()["token"]
        project_name = conf.get("todoist.project_name", "Splatt Ops") or "Splatt Ops"
    except (ConfigError, KeyError) as exc:
        print("Could not read the Todoist token: %s" % exc, file=sys.stderr)
        return 2

    api = Todoist(token)
    try:
        project = next((p for p in api.projects() if p.get("name") == project_name), None)
        if project is None and args.write:
            project = api.create_project(project_name)
            print("created project  %s" % project_name)
        if project is None:
            print("missing project  %s  (run with --write to create it)" % project_name)
            return 1
        project_id = str(project["id"])
        print("project          %s  id %s" % (project_name, project_id))

        found = match_sections(api.sections(project_id))
        missing = [(key, name) for key, name, _ in SECTIONS if key not in found]
        for key, name in missing:
            if args.write:
                found[key] = str(api.create_section(project_id, name)["id"])
                print("created section  %-17s %s" % (key, name))
            else:
                print("missing section  %-17s %s" % (key, name))
    except httpx.HTTPError as exc:
        print("Todoist request failed: %s" % exc, file=sys.stderr)
        return 2

    if len(found) < len(SECTIONS):
        print("\nRun again with --write to create the missing sections.")
        return 1

    print("\nPaste this into config/settings.yaml (or run with --save):\n")
    print(yaml_block(project_id, found))
    if args.save:
        save_ids(project_id, found)
        print("\nSaved to %s" % SETTINGS_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
