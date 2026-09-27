# Finding contact details

This file holds no contact details on purpose. A copied email address goes stale the day a client changes staff, and nothing would say so. Look contacts up fresh every time, in this order:

| Need | Source | Tool |
|---|---|---|
| A billing contact, an email address, a postal address | Xero (the source of truth for contact details) | The Xero connector, `get_contacts` |
| Who at a client handles what, and how they relate to a job | PocketBase | `search_contacts`, `get_client`, `get_client_history` |
| Equipment, aliases, special handling rules | The `splatt-clients` and `splatt-suppliers` skills | Read the skill |

If Xero and PocketBase disagree on an address, use Xero and tell the operator so the PocketBase record can be corrected. If a person is in neither, say so. Never invent a contact.

## Example

A thread from `kyle@tuivalley.example.com` arrives about a blower bearing.

1. `search_clients` with `tuivalley` finds Tui Valley Waters Limited.
2. `search_contacts` on that client shows Kyle Turner, operations.
3. `splatt-clients` says Tui Valley runs a Cyclone 150 blower, and that invoices go to the accounts address in Xero rather than to Kyle.
4. `splatt-suppliers` says bearing cartridges for that blower come from Cyclone Air Systems.
5. If an invoice is involved, `get_contacts` in Xero gives the accounts address to use.

## Special handling

Behavioural rules that no contact record can hold (for example "only contact on Tuesday to Thursday" or "always copy both people") live with the client or supplier in `splatt-clients` and `splatt-suppliers`. When you notice a new pattern, suggest adding it there.
