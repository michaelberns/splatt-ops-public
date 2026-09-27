---
name: splatt-suppliers
description: The Splatt Engineering supplier knowledge base. Use this skill whenever the operator asks about a supplier, needs to source parts, wants to know who supplies what, or needs to know who to contact at a supplier. Also use it to find the right supplier for a client's machine (for example "who makes the Vela fillers?", "where do we get capper parts?", "who do I ask about the blower bearing?"). It holds which supplier serves which machine, the route to order through, pricing terms and special handling. Contact details come from Xero and each supplier's RELATIONSHIP.md; relationships come from PocketBase.
---

# Splatt suppliers

This file is the quick index of Splatt's suppliers: what each one makes, which client machines it supports, how to order, and the rules for dealing with it. The full record for a supplier (contacts, every price received, documents, change log) is its `RELATIONSHIP.md` under `_Suppliers/<Vendor>/` in SS Folders, kept up to date by the `file-supplier-document` playbook (see `splatt-supplier-filing`).

The suppliers below are fictional examples that show the format. A real deployment keeps its own entries in the same shape.

## Where supplier information lives

| Information | Source of truth | How to reach it |
|---|---|---|
| Billing contacts, remittance details | **Xero** | The Xero connector, `get_contacts` |
| The people Splatt deals with, every price received, documents | The supplier's `RELATIONSHIP.md` | `_Suppliers/<Vendor>/RELATIONSHIP.md` in SS Folders |
| Supplier records and links to jobs and clients | **PocketBase** | `search_suppliers`, `search_contacts` |
| What they make, which client machines they support, ordering route, rules | This skill | Read it |

Do not copy email addresses or prices into this file. Prices belong in `RELATIONSHIP.md`, where the price leak guard can see them.

## Entry format

```
### <Supplier name>: <what they make>
- Location: <city, country>
- Folder: _Suppliers/<Vendor>/
- Products: <categories and model ranges>
- Client machines supported: <client, machine, serial>
- How to order: <direct, or through which agent>
- People (details in RELATIONSHIP.md): <name, role>
- Pricing terms: <discount structure, currency, incoterm>
- Special handling: <rules the agent must apply, or "None">
```

## Example suppliers

### Taponera SL: cappers
- Location: Valencia, Spain
- Folder: `_Suppliers/Taponera/`
- Products: screw and ROPP cappers (TCP-100 series), capping heads, chucks, grippers, change parts.
- Client machines supported: Orchard Lane, TCP-100-TR-PZ capper serial 1000-23. Kowhai Health, capper serial 2001 (sensitive client, report only).
- How to order: direct.
- People (details in RELATIONSHIP.md): Eduardo Morales, engineering (change parts, drawings). Lucia Morales, export sales (prices, order confirmations).
- Pricing terms: EUR, ex-works.
- Special handling: converting fixed chucks to split chucks needs a full turret rebuild (cam and every spindle), so quote it as a rebuild, not a parts swap.

### Vela Technologies: fillers
- Location: Italy
- Folder: `_Suppliers/Vela/`
- Products: filling machines and triblocks (rinser, filler, capper), filler spare parts (valves, forks, CIP cups).
- Client machines supported: Clearwater Bottling, Vela 3000-01 serial 1001 and Vela 16-16-4. Tui Valley, Vela 40-40-10.
- How to order: **through Unita Technologies**, Vela's agent. Contact Unita first for any Vela enquiry; go to Vela's spare parts desk directly only if Unita cannot help.
- People (details in RELATIONSHIP.md): Livia Gallo, Vela spare parts. Unita's contact is in its own folder, `_Suppliers/Unita/`.
- Pricing terms: EUR, ex-works.
- Special handling: send a formal written order listing every part number; an informal email is not acted on. Vela and Rialto are different Italian manufacturers, so confirm the machine's make before asking for parts.

### Cyclone Air Systems: blowers and dryers
- Location: United States
- Folder: `_Suppliers/Cyclone/`
- Products: air knives, blowers and dryers, bearing cartridges and spares.
- Client machines supported: Tui Valley, Cyclone 150 blower.
- How to order: direct.
- People (details in RELATIONSHIP.md): Paul Reed, sales.
- Pricing terms: USD list price less 40%, ex-works USA. For example a bearing cartridge, P/N 10001: USD 1,000 list less 40% = USD 600 ex-works. Check for a price rise before quoting a client.
- Special handling: None.

### Coastline Freight: international freight forwarder
- Location: Auckland, New Zealand
- Folder: `_Suppliers/Coastline/`
- Products: international freight, customs clearance, and courier pickups booked with QXP Express.
- Client machines supported: none; it moves parts for every client.
- How to order: book through Coastline's operations desk. Do not book the courier directly.
- Pricing terms: NZD, billed per shipment. Its bills are recharged to the client with a margin; see `splatt-shipment-invoice`.
- Special handling: each bill names the shipment it covers. Match bills to shipments from the bill PDF, never from memory.

## Which supplier serves which machine

| Client | Equipment | Supplier | Route |
|---|---|---|---|
| Clearwater Bottling | Vela 3000-01 filler (serial 1001), Vela 16-16-4 filler | Vela Technologies | Through Unita Technologies |
| Orchard Lane | Taponera TCP-100-TR-PZ capper (serial 1000-23) | Taponera | Direct |
| Tui Valley | Vela 40-40-10 filler | Vela Technologies | Through Unita Technologies |
| Tui Valley | Cyclone 150 blower | Cyclone Air Systems | Direct |
| Kowhai Health | Taponera capper (serial 2001) | Taponera | Report only (sensitive client) |
| Any client | International freight and import | Coastline Freight | Direct |

Client details are in `splatt-clients`.

## Rules of use

1. When asked "who supplies parts for X?", check the table above first, then the supplier's `RELATIONSHIP.md`.
2. Confirm the make and model of the machine before contacting a supplier.
3. Before treating a supplier as a new contact, search every connected mailbox for earlier threads. First contact goes through the `engage-new-supplier` playbook.
4. International freight goes through the freight forwarder, never straight to a courier.
5. A formal supplier quote goes through the `process-incoming-quote` playbook, and any supplier document is filed with `file-supplier-document`, which records the price in `RELATIONSHIP.md`.
6. Never put a supplier's price in a message to a client. Run `price-leak-guard` before any client-facing message with figures.
