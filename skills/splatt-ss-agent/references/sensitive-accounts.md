# Sensitive clients

A sensitive client is one where a wrong word could cause real harm, so the agent reports and never acts.

## The rule

A client is sensitive when its PocketBase record has `critical` set to true. The optional `critical_note` field says why, in one line.

For a sensitive client the agent:

- reports the current status in the Sensitive Clients part of the report,
- does **not** write emails, move tasks, complete tasks, change job statuses or create Xero records.

The daemon's trigger engine applies the same rule: it refuses to move a job on a sensitive client and lists the refusal under `needs_decision`. The validator counts such jobs rather than failing them.

## Deciding whether a client should be flagged

Suggest flagging a client when its correspondence starts to include formal complaint language, a threatened escalation, or anything the operator has said must be handled personally. Ask, in one line:

"The correspondence with [client] reads like a formal complaint. Should I flag them as sensitive?"

Only the operator sets or clears `critical`. Never change it yourself.

## Example

| Client | `critical` | `critical_note` | What the agent does |
|---|---|---|---|
| Kowhai Health NZ Ltd (KHNZ) | true | "Operator handles all contact personally." | Reports the latest thread and open tasks. Writes nothing, moves nothing |
| Clearwater Bottling (NZ) Ltd | false | | Normal handling |
