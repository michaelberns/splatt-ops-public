---
name: splatt-clients
description: The Splatt Engineering client knowledge base. Use this skill whenever the operator asks about a client, a client's equipment, account status or history, or needs to know who to contact. Also use it to work out which client an email or task belongs to, when onboarding a new client, and when cross-referencing clients between Xero, PocketBase and Todoist. It holds what Xero does not: legal names and aliases, the equipment each client runs, which supplier serves which machine, account notes and special handling rules. Xero is the source of truth for contact details; PocketBase is the source of truth for relationships.
---

# Splatt clients

This file is a knowledge layer on top of the business systems. It records what no system stores well: which names a client goes by, what machines it runs, which supplier supports each machine, and how the client must be handled.

The clients below are fictional examples that show the format. A real deployment keeps its own entries in the same shape.

## Where client information lives

| Information | Source of truth | How to reach it |
|---|---|---|
| Contact names, email addresses, postal addresses, billing details | **Xero** | The Xero connector, `get_contacts` |
| Who handles what, jobs, quotes, interactions, the `critical` flag | **PocketBase** | `search_clients`, `get_client`, `search_contacts`, `get_client_history` |
| Aliases, equipment, supplier for each machine, special handling | This skill | Read it |
| Open work | Todoist | See `splatt-todoist` |

Rules:

1. Never copy an email address or phone number into this file. Look it up in Xero when it is needed, because a copy goes stale without anyone noticing.
2. If this file and Xero disagree on a name or address, Xero is right. Tell the operator.
3. If this file and PocketBase disagree on a relationship (who works where, which job belongs to whom), PocketBase is right. Tell the operator.
4. Do not change a client entry silently. When something significant changes (a rebrand, a new contact, new equipment, a change of account status), ask: "Should I update the client entry with this?"

## Entry format

```
### <Legal name, as registered>
- Xero contact name: <exactly as in Xero, if different from the legal name>
- Aliases: <every name the client appears under in email, tasks and folders>
- Email domain: <used to match incoming mail to the client>
- Location: <town>
- Currency: <NZD, ...>
- People (details in Xero): <name, role>
- Equipment: <machine, model, serial>  →  <supplier>
- Project folders: <SS Folders/<Client>/<Project>>
- Account notes: <commercial context worth knowing before writing>
- Special handling: <rules the agent must apply, or "None">
```

## Example clients

### Clearwater Bottling (NZ) Ltd
- Xero contact name: Clearwater Bottling (NZ) Ltd
- Aliases: Clearwater Bottling NZ, Clearwater Bottling, ClearwaterBottling, Clearwater
- Email domain: clearwaterbottling.example.co.nz
- Location: Hamilton
- Currency: NZD
- People (details in Xero): Nigel Grant, operations and day to day contact. Rebecca, accounts payable.
- Equipment:
  - Vela 3000-01 filler, serial 1001 → Vela Technologies, ordered through Unita Technologies
  - Vela 16-16-4 filler → Vela Technologies, through Unita Technologies
- Project folders: `SS Folders/Clearwater Bottling/Filler Service 2026`
- Account notes: sensitive to freight cost. Quote freight as its own line and say which route it takes, so the figure is never a surprise.
- Special handling: invoices go to the accounts payable contact in Xero, not to Nigel.

### Orchard Lane Fruit Processors Ltd
- Xero contact name: Orchard Lane Fruit Processes Ltd (the Xero name differs from the legal name; search Xero for "Orchard Lane")
- Aliases: Orchard Lane, Orchard Lane of Cromwell, Orchard Lane Cromwell
- Email domain: orchardlane.example.co.nz
- Location: Cromwell
- Currency: NZD
- People (details in Xero): Dan Bennett, maintenance lead.
- Equipment:
  - Taponera capper TCP-100-TR-PZ, serial 1000-23 → Taponera (change parts, grippers, chucks)
- Project folders: `SS Folders/Orchard Lane/Capper Change Parts`
- Account notes: buys 375 mL change parts in batches ahead of the season.
- Special handling: Dan works Tuesday to Thursday. Schedule follow-ups on those days, and do not count Monday or Friday as business days when timing a chase.

### Tui Valley Waters Limited
- Xero contact name: Tui Valley Waters Limited
- Aliases: Tui Valley NZ, Tui Valley
- Email domain: tuivalley.example.com
- Location: Te Puke
- Currency: NZD
- People (details in Xero): Kyle Turner, operations. Shona Vance, accounts.
- Equipment:
  - Vela 40-40-10 filler → Vela Technologies, through Unita Technologies
  - Cyclone 150 blower → Cyclone Air Systems (bearing cartridges)
- Account notes: several machines from different suppliers. Keep each enquiry as its own task and, where it is a project, its own project folder; one task per scope.
- Special handling: None.

### Kowhai Health NZ Ltd (KHNZ): sensitive
- Xero contact name: Kowhai Health Ltd
- Aliases: Kōwhai Health New Zealand, Kōwhai Health, Kowhai Health, KHNZ
- Email domain: kowhaihealth.example.co.nz
- Currency: NZD
- Equipment:
  - Taponera capper, serial 2001 → Taponera
- Special handling: **sensitive.** The PocketBase record has `critical` set to true. Report the status of this client in every run and take no action: no emails, no task moves, no completions, no job status changes. See `splatt-ss-agent`, `references/sensitive-accounts.md`.

## Which supplier serves which machine

| Client | Equipment | Supplier | Route |
|---|---|---|---|
| Clearwater Bottling | Vela 3000-01 filler (serial 1001), Vela 16-16-4 filler | Vela Technologies | Through Unita Technologies |
| Orchard Lane | Taponera TCP-100-TR-PZ capper (serial 1000-23) | Taponera | Direct |
| Tui Valley | Vela 40-40-10 filler | Vela Technologies | Through Unita Technologies |
| Tui Valley | Cyclone 150 blower | Cyclone Air Systems | Direct |
| Kowhai Health | Taponera capper (serial 2001) | Taponera | Report only (sensitive client) |

Supplier contacts, pricing terms and rules are in `splatt-suppliers`.

## Finding a client's contact details

1. `search_clients` in PocketBase with a name, an alias or an email domain. This also gives the client id for `get_client_history` and shows whether `critical` is set.
2. `search_contacts` on that client for who handles what.
3. For an email address or postal address, `get_contacts` in Xero. Use the Xero value.
4. If the person is in neither system, say so. Never invent a contact.

## Onboarding a new client

1. Check first that the client is really new: search PocketBase, search every connected mailbox, and read this file. A "new" client often has history under another name.
2. Run the `onboard-new-client` playbook. It creates the PocketBase record, the project folder, `PROJECT.md` and `PROJECT-overview.html` from the templates, the first interaction, and the first Todoist task.
3. Offer to add an entry here in the format above.

## Rules of use

1. Identify a client from an email by its domain first, then by the aliases above, then with `search_clients`.
2. When a client's machine needs parts, use the table above and then `splatt-suppliers` for the supplier's contacts and terms.
3. Apply every special handling rule before writing to the client or scheduling a follow-up.
4. A client flagged `critical` in PocketBase is surface only, whether or not this file says so.

## Changelog

| Date | Change | Source |
|---|---|---|
| YYYY-MM-DD | What changed | Where it came from (the operator's instruction, an email, Xero) |
