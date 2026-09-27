# Xero reference

How the agent works with Xero. Read this when a request involves invoices, bills, payments or billing contacts.

Xero is the source of truth for money and for contact details (names, emails, postal addresses). PocketBase mirrors some of it and adds relationships; when the two disagree, Xero is right.

## Two paths, one for reading and one for writing

| Path | Used for | Needs an instruction? |
|---|---|---|
| The Xero connector (read-only reporting tools) | Finding contacts, listing invoices and bills, payments, aged receivables and payables, profit and loss | No |
| The Xero web app in the browser | Creating or editing an invoice, bill, payment, credit note or quote | Yes, an explicit one from the operator, every time |

Before relying on the connector, call `get_organisation_info` and check the organisation is Splatt's own (Splatt Engineering Ltd). A tool connected to a Xero demo company returns success for everything and changes nothing real.

## Reading invoices for a client

1. Find the contact with `get_contacts` and a search term. The Xero name can differ from everyday usage (for example the legal name "Clearwater Bottling (NZ) Ltd" rather than "Clearwater"). The client's aliases are in `splatt-clients` and in PocketBase.
2. List the contact's invoices with `get_invoices`, filtered by contact. If a full page comes back, ask the operator before fetching the next one.
3. For the invoice in question, read the line items.
4. Report for each invoice: number and reference, issue date and due date, status, line items (description, quantity, unit amount), subtotal, tax, total, amount due or amount paid and date paid, and days overdue if past due.

## Invoice types and statuses

| Type | Code | Meaning |
|---|---|---|
| Sales invoice | `ACCREC` | Accounts receivable: money owed to Splatt |
| Bill | `ACCPAY` | Accounts payable: money Splatt owes a supplier |

| Status | Meaning |
|---|---|
| `DRAFT` | Created, not approved |
| `SUBMITTED` | Waiting for approval |
| `AUTHORISED` | Approved and sent, waiting for payment ("Awaiting Payment" in the web app) |
| `PAID` | Fully paid |
| `VOIDED` | Cancelled |

## Creating an invoice (only on instruction)

1. Confirm with the operator: the contact, each line's description, quantity, unit amount, account code and tax type, the reference (for example the quote number), and whether amounts are tax inclusive.
2. Open the new invoice form in the Xero web app and fill it in.
3. Take a screenshot and show the operator. Stop before Approve or Save.
4. After the operator has committed it: add the invoice to the project file's Payments section (through the playbook chain) and log a PocketBase interaction.
5. Report the new invoice number.

Freight and import duty recharges have their own rules (GST coding, margin, which lines carry tax). Use `splatt-shipment-invoice` for those.

## Overdue invoices

During an ops cycle, check every `AUTHORISED` sales invoice against today's date:

- 14 or more days past due: make sure a Todoist follow-up task exists; create one if not. Do not touch Xero.
- 7 to 14 days past due: note it in the report.
- 1 to 7 days past due: mention only if the operator asks.

## Common account codes

The codes used most often. Check anything else with `get_chart_of_accounts`.

| Code | Description |
|---|---|
| 210 | Sales |
| 310 | Cost of goods sold |
| 400 | Advertising |
| 404 | Bank fees |
| 425 | Freight and courier |
| 429 | General expenses |
| 461 | Printing and stationery |
| 469 | Telephone and internet |
| 473 | Travel, national |

## Common NZ tax types

| Code | Description | Rate |
|---|---|---|
| `OUTPUT2` | GST on income | 15% |
| `INPUT2` | GST on expenses | 15% |
| `NONE` | No GST | 0% |
| `EXEMPTOUTPUT` | GST exempt income | 0% |
| `ZERORATED` | Zero rated | 0% |
