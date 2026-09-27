---
name: splatt-shipment-invoice
description: >-
  Build, fix and reconcile Splatt Engineering freight and shipment recharge invoices in Xero, where a client reimburses Splatt for a freight forwarder, a courier and the customs import duty on a shipment. Use this skill whenever the operator says "do the shipment invoice", "recharge freight", "recharge the duty", "invoice the client for freight/import", "fix the freight & duty invoice", "check what's paid on this project", "open the invoices and bills", or mentions a freight forwarder, a courier, customs or import duty in the context of billing a client. It works one project at a time, reads the real supplier bill PDFs (never a spreadsheet), opens every related invoice and bill for the operator to review, and applies the GST coding and margin rules. If unsure whether it applies, use it.
---

# Splatt shipment invoice

A shipment recharge invoice asks a client to repay Splatt for bringing their goods in: the freight forwarder, the courier, and the customs import duty. Freight carries Splatt's margin; duty is passed through at cost with no GST. The rules below are easy to get wrong in either direction, so read this skill before touching a recharge invoice.

Xero reads go through the Xero connector and Xero changes happen in the Xero web app in the browser, exactly as `splatt-ss-agent` describes. The validator rules `shipping_not_forgotten`, `supplier_bill_recharged`, `bill_not_orphaned` and `duty_recharged_with_margin` check the result.

## Operating principles

1. **One project at a time.** Work one shipment or project per session. Open every invoice and bill that belongs to it and leave them open for the operator to review.
2. **Read the source documents.** Open each supplier bill, read its line items and its own tax analysis, and view the attached PDF. A spreadsheet is never the source for an amount or a paid status.
3. **Check paid or unpaid in Xero.** A bill marked paid in a note or sheet can still be Awaiting Payment in Xero. Confirm every status in Xero and report every mismatch.
4. **Never click Approve, Update or Save** without the operator's explicit go. Stage the figures, take a screenshot, and let the operator commit. Be especially careful with an invoice that is already Awaiting Payment: do not re-save it without an instruction.
5. **Ask once about money.** The markup percentage, which shipment a bill belongs to, whether to include a small line: ask one focused multiple-choice question rather than guess.

## The recharge rules

A recharge invoice has two kinds of line, and they are treated differently:

| Line | What it is | GST | Markup |
|---|---|---|---|
| **Freight and courier** | Forwarder charges, courier charges, handling | Taxable, 15% GST | Add 10 to 30% (default 20%) |
| **Duty fee** | Customs import duty, entry fees, GST on importation, duty processing | Not taxable ("No GST") | None, recharged at cost |

Rules:

- **Freight is taxable and carries the margin.** Add 10 to 30% (default 20%, confirm with the operator) to the freight and courier line. Recharging freight at bare cost gives the margin away.
- **Duty is not taxable and is recharged at cost.** Code it No GST and add no markup.
- **Recharge the whole duty block.** The duty fee is the supplier bill's entire non-taxable block, including "GST on importation". Leaving the import GST out under-charges the client by that amount, and it is money the client still owes.
- **Do not add GST on top of a freight figure that already includes it.** Use tax inclusive amounts (below).

### Split the lines using the bill's own tax analysis

A courier's import bill shows what is taxable and what is not:

- **Code Z, "Duty and Tax", 0%:** the non-taxable duty block. It goes on the duty fee line at cost, No GST. It includes import duty, the entry fee, the GST on the entry fee, and GST on importation (usually the largest figure).
- **Code A, taxable 15%:** small processing or handling charges, for example "Duty Tax Processing". These are taxable. Add them to the freight side or as their own taxable line, and always tell the operator they exist and ask whether to include them.

### Worked example

Clearwater Bottling, a shipment of Vela filler parts, invoice INV-41359. Two supplier bills:

- **Courier import bill** (QXP Express, AKLZ00000001), total $1,046.00:
  - Code Z $1,000.00 = import duty $200.00 + entry fee $50.00 + GST on the entry fee $7.50 + GST on importation $742.50.
  - Code A $40.00 processing, plus $6.00 GST.
- **Freight forwarder bill** (Coastline Freight, job 00000138): $500.00 including GST.

The recharge invoice:

| Line | Amount | Tax |
|---|---|---|
| Freight and courier charges, Coastline Freight job 00000138: $500.00 + 20% | $600.00 | 15% GST, included |
| Duty fee (import duty, entry fees and GST on importation, not taxable), QXP bill AKLZ00000001 | $1,000.00 | No GST, no markup |
| **Total** | **$1,600.00** | |

The $40.00 Code A processing charge is left off until the operator decides. Recharging only $257.50 of the duty block (duty, entry fee and its GST, without the GST on importation) would have left $742.50 the client owes uncharged.

### Tax inclusive amounts

Set "Amounts are" to **Tax inclusive** on the invoice. A freight line typed as the gross supplier figure then carries its GST inside the line instead of adding 15% on top, and the duty line shows its exact cost, so the invoice total ties straight back to the bills. A 20% markup on the gross freight figure is the same as 20% on the net.

## Workflow

### 1. Scope the project

1. Identify the one client and the one shipment. A client can have several shipments at once; keep them apart.
2. In PocketBase, find the client and the job (`search_clients`, `search_jobs`) and note any earlier invoices.

### 2. Open everything and check what is paid

1. Confirm the Xero session is live. If it lands on the login page, stop and ask the operator to sign in.
2. **Client invoices:** read each related invoice: number, status, total, amount paid, amount due. Leave them open.
3. **Supplier bills:** open every bill for this shipment (forwarder, courier, customs). For each, read the status (Paid, Awaiting Payment, Awaiting Approval), the line items and the tax analysis, and view the PDF to confirm which shipment it covers (consignee, goods, weight, dates, master bill).
4. **Match bills to the shipment from the PDF.** The same forwarder may have bills for different clients' shipments in the same month.
5. Report every paid or unpaid mismatch between Xero and any note.

### 3. Build or fix the client invoice (staged only)

1. New invoice, the client's contact, "Amounts are: Tax inclusive", NZD.
2. **Freight and courier line:** name the forwarder and the bill number; price is the supplier freight plus the agreed markup; account 425 (Freight and courier); 15% GST.
3. **Duty fee line:** "Duty fee (import duties, entry fees and GST on importation, not taxable)" plus the courier bill reference; price is the bill's full Code Z total; No GST; no markup.
4. Optionally a taxable line for the Code A processing charge, if the operator wants it recharged.
5. Stop. Do not Approve or Update. Take a screenshot, show the line by line breakdown and the total, and let the operator commit.

### 4. A supplier bill that is not in Xero yet

Find the email with the bill in Gmail and open it for the operator, who forwards it to the Hubdoc inbox. The agent never sends it.

### 5. Record keeping

1. Update the PocketBase job (status, what is paid and outstanding, the supplier bills) and log an interaction with the final invoice structure and any mismatches found.
2. Update the project file through the playbook chain (`splatt-project-files`).
3. Send the Telegram notification described in `splatt-ss-agent`.

## The NZ invoice template

Splatt's invoice template is a Word `.docx` branding theme uploaded to Xero. Its fields are Word merge fields, and three things must be right for New Zealand:

- **Dates in NZ format, with no time.** Add a date switch to each date field, in `document.xml` and in the payment advice footer (`footer3.xml`):
  `MERGEFIELD InvoiceDate \@ "dd/MM/yyyy"` and `MERGEFIELD InvoiceDueDate \@ "dd/MM/yyyy"`.
  Without the switch Xero prints a US date with a time, such as "6/18/2026 12:00:00 AM".
- **Amounts to 2 decimal places.** Add a number switch to the line fields:
  `MERGEFIELD UnitAmount \# "0.00"` and `MERGEFIELD LineAmount \# "0.00"`.
  Without it they print 4 decimals. Totals already print 2.
- **NZ bank account only.** Remove the USD and EUR account paragraphs from the payment advice footer and keep the NZD account with its SWIFT details.

Edit the file with the `docx` skill (unpack, edit the XML, pack). Merge fields only show real data inside Xero, so the final check happens when the operator uploads the template as the branding theme.

## Checklist of mistakes to avoid

- Recharging the duty without the GST on importation. The duty line is the full Code Z block, at cost.
- Adding markup to the No GST duty line.
- Recharging freight at cost with no margin.
- Trusting a spreadsheet's paid or unpaid status instead of Xero.
- Guessing which shipment a bill belongs to instead of reading its PDF.
- A US date, 4 decimals, or foreign bank accounts on the template.
- Clicking Approve or Update without the operator's go.
