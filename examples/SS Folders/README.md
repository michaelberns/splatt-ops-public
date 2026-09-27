# Example project folders

A small, fictional copy of the project-folder tree the system works on
("SS Folders"). Point the files server at it to try the dashboard's
Project Files and Playbooks pages without any real data:

```bash
SS_FOLDERS_PATH="$PWD/examples/SS Folders" node server/project-files-server.js
```

## Layout

```
Clearwater Bottling/Filler Service 2026/PROJECT.md   an active project
Orchard Lane/Capper Change Parts/PROJECT.md          a project being quoted
_Suppliers/Taponera/RELATIONSHIP.md                  one supplier file
_howtotasks/playbooks/*.spec.json                    three playbook definitions
_howtotasks/runs/                                    playbook runs are saved here
```

- **`PROJECT.md`** is the written history of one project: contacts,
  quotes, orders, a communications log, decisions, open items and a
  timeline. The agent only edits it through the `update-project-md`
  playbook, which only ever appends. Every "⚪ Waiting" or "🟡 Open" row
  names the Todoist task that guards it; the validator rule
  `promises_are_guarded` checks this.
- **`RELATIONSHIP.md`** is the equivalent for a supplier: contacts,
  product range, quotes received (list price, discount, net price and tax
  basis), and which clients run their equipment.
- **Playbook specs** define multi-step procedures as a list of steps, each
  with validators the files server runs before it accepts the step (see
  [docs/HARNESSES.md](../../docs/HARNESSES.md#layer-3-playbooks-procedures-the-agent-cannot-shortcut)).

The names match the demo data created by `python -m tools.seed_demo`.
