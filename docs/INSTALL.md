# Installation

There are two ways to set this up.

- **Part A, the demo** (about 10 minutes, no accounts needed). A local
  database filled with a fictional business, the dashboard, the files
  server with example project folders, and the validator. This is the
  quickest way to see how the system works.
- **Part B, the full setup.** Everything in Part A plus Todoist, Telegram,
  the always-on daemon and the AI agent (Claude). This is how the system
  runs for real.

Every command below is run from the root of the repository unless it says
otherwise.

## Requirements

| Tool | Version | Needed for |
|---|---|---|
| macOS or Linux | | Everything. `bin/install-autostart` is macOS only. |
| Python | 3.10 or newer (tested on 3.12) | The daemon, validator, MCP servers and tools |
| Node.js | 18 or newer (tested on 22) | The files server, and the dashboard and JS tests |
| PocketBase | 0.25.x (tested on 0.25.9) | The database. A single binary, downloaded in step A3. |
| A browser | any modern one | The dashboard |
| Todoist, Telegram, Claude | | Part B only |

---

## Part A: the demo

### A1. Get the code

```bash
git clone https://github.com/michaelberns/splatt-ops-public.git
cd splatt-ops-public
```

### A2. Create the Python environment

First check which Python you have. It must be 3.10 or newer, because the
MCP library requires it:

```bash
python3 --version
```

On macOS the built-in `python3` is 3.9, which is too old. Install a newer
one with [Homebrew](https://brew.sh) (`brew install python@3.12`) and use
`python3.12` in the next command instead of `python3`.

```bash
python3 -m venv .venv            # or: python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

This installs four packages: `httpx` (HTTP client), `PyYAML` (config
files), `mcp` (the Model Context Protocol server library, kept below 2.0
because the servers are written against the 1.x API) and `pytest`.

### A3. Download PocketBase

PocketBase is one executable file. Download the build for your machine
from the [0.25.9 release](https://github.com/pocketbase/pocketbase/releases/tag/v0.25.9)
and put the `pocketbase` file in the repository root:

```bash
# Apple Silicon Mac. For an Intel Mac use darwin_amd64, for Linux linux_amd64.
curl -L -o pocketbase.zip https://github.com/pocketbase/pocketbase/releases/download/v0.25.9/pocketbase_0.25.9_darwin_arm64.zip
unzip -o pocketbase.zip pocketbase && rm pocketbase.zip
./pocketbase --version
```

The binary is gitignored. So is `pb_data/`, the folder PocketBase keeps
its data in.

### A4. Create the database and an admin account

```bash
./pocketbase superuser upsert admin@example.com 'choose-a-long-password' --dir pb_data
```

This creates `pb_data/`, applies the migration in `pb_migrations/` (which
creates all 21 tables) and adds an admin ("superuser") account. You can use
any email address; it never leaves your machine.

### A5. Create the `.env` file

```bash
cp .env.example .env
```

Open `.env` and fill in the two PocketBase lines with the account from A4:

```
PB_ADMIN_EMAIL=admin@example.com
PB_ADMIN_PASSWORD=choose-a-long-password
```

Leave everything else blank for the demo. `.env` is the only file the
system reads secrets from, and it is gitignored.

### A6. Start PocketBase

In its own terminal window:

```bash
./pocketbase serve --http=127.0.0.1:8090 --dir=pb_data
```

The admin UI is now at <http://127.0.0.1:8090/_/> (log in with the A4
account). `127.0.0.1` means only programs on this machine can reach it.

### A7. Load the demo data

```bash
.venv/bin/python -m tools.seed_demo            # shows what it will create
.venv/bin/python -m tools.seed_demo --write    # creates it
```

This creates about 60 records for a fictional business: clients, jobs at
every stage, quotes, invoices, tasks (some overdue), equipment and one
project's money ledger. Every record goes through the same checked write
path as the real system. It refuses to run against a database that
already has clients.

### A8. Start the files server

In another terminal window, pointing it at the example project folders
that come with the repo:

```bash
SS_FOLDERS_PATH="$PWD/examples/SS Folders" node server/project-files-server.js
```

This serves the project files, the example playbooks and the logs to the
dashboard on <http://127.0.0.1:8092>.

### A9. Open the dashboard

```bash
cp dashboard/config.example.js dashboard/config.local.js
open dashboard/index.html          # on Linux: xdg-open dashboard/index.html
```

The dashboard reads PocketBase and the files server directly, so there is
nothing to build. You should see the demo clients, a workload board with
overdue and waiting tasks, the equipment map, and the project files.

> **Shortcut for A6 to A9.** `bin/start` starts PocketBase, the files
> server, the daemon, the guards and the dashboard together. For the demo,
> turn off the parts that need Todoist or would touch files outside the
> repo:
>
> ```bash
> SS_FOLDERS_PATH="$PWD/examples/SS Folders" bin/start --no-sync --no-guards
> ```
>
> `bin/start --check` reports what it found and what is missing without
> starting anything. The guards are off in the demo on purpose: in normal
> use they tidy stray project files from your Desktop, Downloads and
> Documents folders into the project folders, which is not something a
> demo should do.

### A10. Run the validator

```bash
export SPLATT_ROOT="$PWD/examples"     # so the validator finds the demo project folders
.venv/bin/python -m validator doctor
.venv/bin/python -m validator run --no-todoist
echo "exit code: $?"
```

`doctor` checks the configuration and the connections. In the demo it
lists the Todoist and Telegram secrets as missing and exits with code 2;
that is expected, because Part A does not set them up.

`run --no-todoist` runs the 36 rules, skipping the ones that need
Todoist. On the demo data the verdict is **BLOCKED** (exit code 1), and
that is intended: the demo business contains the kind of problems the
validator exists to catch. For example:

- *Filler Service 2026* is marked `won`, but no quote PDF is filed in its
  project folder, so `gate_quoted` fails and `gate_won` cannot pass.
- *Spare Valve Kits* is `invoiced`, but its client has no project folder
  holding the invoice PDF (`gate_invoiced`).
- A job has sat in `quoted` with no chase task (`quoted_jobs_have_a_chase`).

The full report is written to `reports/`. Each finding names the rule,
the record and the reason, and the report lists separately the checks it
could not evaluate ("a skip is not a pass"). See
[HARNESSES.md](HARNESSES.md) for what the rules mean.

`bin/splatt-validate` runs the same thing the way the AI agent does
(including the Telegram alert, when Telegram is configured).

### A11. Run the tests

```bash
.venv/bin/python -m pytest
npm install     # only needed for the dashboard compile test and npm test
npm test
```

The Python suite also runs the JS suites through `tests/test_node_suites.py`
when Node is installed. Tests that need a live database skip themselves
when one is not available.

---

## Part B: the full setup

### B1. Todoist

1. Copy your API token from Todoist (Settings, Integrations, Developer)
   into `.env` as `TODOIST_API_TOKEN`.
2. Create the board and save its ids into `config/settings.yaml`:

   ```bash
   .venv/bin/python -m tools.todoist_setup                 # report only
   .venv/bin/python -m tools.todoist_setup --write --save  # create and save
   ```

   This creates a project called "Splatt Ops" with seven sections
   (Today, Overdue, Upcoming, Waiting on Client, Waiting on Supplier,
   Stalled, Backlog), or finds them if they already exist, and writes their
   ids into `config/settings.yaml`. The overdue engine refuses to run while
   any of these ids are blank.

### B2. Telegram

The system uses two channels: **general** for day-to-day messages and
**ops** for validator results and escalations.

1. In Telegram, talk to `@BotFather`, send `/newbot`, and copy the token
   it gives you. Do this twice if you want separate bots for the two
   channels (one bot for both also works).
2. Send any message to each bot (or add it to a group), then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   the `chat.id` value.
3. Fill in `.env`:

   ```
   TELEGRAM_BOT_TOKEN=...        TELEGRAM_CHAT_ID=...
   TELEGRAM_BOT_TOKEN_OPS=...    TELEGRAM_CHAT_ID_OPS=...
   ```

If a send fails, the message is queued in `state/telegram-outbox/` and
delivered by the daemon later.

### B3. Project folders

By default the project folders live in `~/Documents/Splatt/SS Folders`
(set `paths.root` in `config/settings.yaml`, or `SPLATT_ROOT` in the
environment, to change it). The layout is:

```
SS Folders/
  <Client name>/<Project name>/PROJECT.md     one folder per project
  _Suppliers/<Supplier>/RELATIONSHIP.md       one file per supplier
  _howtotasks/playbooks/*.spec.json           the playbook definitions
  _howtotasks/runs/                           playbook run records
```

Start with the example playbooks:

```bash
mkdir -p ~/Documents/Splatt/SS\ Folders
cp -R "examples/SS Folders/_howtotasks" ~/Documents/Splatt/SS\ Folders/
```

### B4. Start everything

```bash
bin/start --check     # see what it found; starts nothing
bin/start             # start and supervise everything
```

`bin/start` brings up PocketBase (127.0.0.1:8090), the files server
(127.0.0.1:8092), both guards, the daemon loop and the dashboard, and
restarts anything that dies. `Ctrl+C` stops it all.

Before letting the daemon write, you can see what it would do:

```bash
.venv/bin/python -m daemon sync              # Todoist -> PocketBase, dry run
.venv/bin/python -m daemon triggers          # dry run
.venv/bin/python -m daemon sweep --dry-run   # the overdue ladder, dry run
```

To start the stack automatically at login on macOS:

```bash
bin/install-autostart            # install the launchd agent
bin/install-autostart --status   # check it
bin/install-autostart --uninstall
```

### B5. Connect the AI agent

The agent is Claude with two MCP servers and the skills in `skills/`.

**MCP servers, Claude Code:**

```bash
claude mcp add pocketbase -s user -e PYTHONPATH="$PWD" -- "$PWD/.venv/bin/python" -m mcp_server
claude mcp add splatt-playbook -s user -e PYTHONPATH="$PWD" -e PLAYBOOK_SERVER_URL=http://127.0.0.1:8092 -- "$PWD/.venv/bin/python" -m playbook_mcp
```

**MCP servers, Claude Desktop:** add this to
`~/Library/Application Support/Claude/claude_desktop_config.json`,
replacing `/path/to/splatt-ops` with the real path, then restart Claude:

```json
{
  "mcpServers": {
    "pocketbase": {
      "command": "/path/to/splatt-ops/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": { "PYTHONPATH": "/path/to/splatt-ops" }
    },
    "splatt-playbook": {
      "command": "/path/to/splatt-ops/.venv/bin/python",
      "args": ["-m", "playbook_mcp"],
      "env": {
        "PYTHONPATH": "/path/to/splatt-ops",
        "PLAYBOOK_SERVER_URL": "http://127.0.0.1:8092"
      }
    }
  }
}
```

The servers read the PocketBase credentials from the repo's `.env`, so no
password goes into the Claude config.

**Skills:**

```bash
bin/sync-skills
```

copies `skills/*` into `~/.claude/skills/` and registers them in the skill
harness (PocketBase must be running). Gmail, Todoist and Xero are reached
through Claude's own connectors; enable those in Claude's settings.

**First run:** ask Claude to "run ops". The `splatt-ss-agent` skill takes
over: preflight, read mail and the board, act, then run the validator and
report from its verdict.

### B6. Optional extras

- **Hosted read-only dashboard** on Cloudflare Pages: see
  [HOSTED-DASHBOARD.md](HOSTED-DASHBOARD.md).
- **Geocoding client addresses** for the map:
  `.venv/bin/python -m tools.geocode_clients` (uses OpenStreetMap
  Nominatim; set `GEOCODER_CONTACT_EMAIL` in `.env` so they can contact
  you, as their usage policy asks).

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `missing secret 'PB_ADMIN_EMAIL'` | `.env` is missing or not filled in (step A5). |
| `refusing to write to '<collection>': field does not exist` | The database and `core/schema.py` disagree. Run `.venv/bin/python -m tools.introspect_schema --write` after a schema change. |
| The dashboard shows "config.local.js is missing" | Step A9: copy `dashboard/config.example.js` to `dashboard/config.local.js`. |
| The dashboard loads but is empty | PocketBase is not running on :8090, or the demo data was not loaded (A6, A7). |
| The Project Files or Playbooks pages are empty | The files server is not running, or `SS_FOLDERS_PATH` does not point at a project-folder tree (A8). |
| The overdue sweep says a section id is missing | Run `tools.todoist_setup --write --save` (B1). |
| The validator exits with code 2 | It could not run at all, which is not a pass. Run `.venv/bin/python -m validator doctor`. |
| `ModuleNotFoundError: mcp.server.fastmcp` | An `mcp` 2.x package was installed. Reinstall from `requirements.txt`, which pins `mcp<2`. |
