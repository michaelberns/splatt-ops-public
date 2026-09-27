---
name: splatt-supplier-filing
description: Two playbooks for Splatt supplier work. file-supplier-document runs whenever a PDF, brochure, datasheet or quote is saved under SS Folders/_Suppliers/<vendor>/, and makes the same operation update the vendor's RELATIONSHIP.md with list price, discount, net price (with its tax basis), filename, source email and date, plus a Change Log row. price-leak-guard runs before any client-facing email or quote that mentions money; it loads every supplier net and list price from the RELATIONSHIP.md files and scans the outbound text for exact, within 2%, and currency-converted matches, and blocks the message on a match. Use this skill when a PDF lands in a supplier folder, when writing any client-facing message that mentions money, when the operator says "file this", "save the brochure", "draft a quote for a client", "send pricing" or "what are we charging", before any splatt-email run to a client address, and before any Telegram message with figures. If in doubt, run the guard.
---

# Splatt supplier filing and the price leak guard

This skill enforces two rules through the playbook server, the same way `update-project-md` enforces the project file rules. The rules are written as playbook specs in `playbooks/`, and the playbook server refuses a step whose evidence does not pass its validators.

```
splatt-supplier-filing/
├── SKILL.md                              this file: when and how to run them
├── playbooks/
│   ├── file-supplier-document.yaml       rule 1: filing a supplier document
│   └── price-leak-guard.yaml             rule 2: no supplier price reaches a client
└── templates/
    └── RELATIONSHIP.template.md          the skeleton for a supplier's RELATIONSHIP.md
```

If a playbook is not registered on the server yet (check `playbook__specs`), follow its YAML file step by step as a checklist and report that it ran unregistered.

Paths are relative to the SS Folders root (see `splatt-project-files`). Each supplier has a folder `_Suppliers/<Vendor>/` holding its documents and a `RELATIONSHIP.md`: contacts, product range, every quote and price received, the clients whose equipment it supports, and a change log.

## Rule 1: every filed supplier document updates RELATIONSHIP.md (`file-supplier-document`)

**When:** any time a PDF, brochure, datasheet or quote is saved under `_Suppliers/<Vendor>/`.

**Why:** a price that lives only in an email or in one client's project file cannot be found later, and cannot be checked by the price leak guard.

| Step | What it checks |
|---|---|
| 1 | The supplier folder exists or is created (`supplier_folder_path`, `supplier_folder_existed`, `vendor_name`) |
| 2 | The PDF is filed under its conventional name (`pdf_byte_size`, `pdf_mtime`, `filename_matches_convention`) |
| 3 | A project copy exists if the document is for a project (`project_mirror_required`, `project_mirror_path_or_null`) |
| 4 | `RELATIONSHIP.md` exists, created from the template if not (`relationship_md_path`, `pre_edit_byte_size`, `suppliers_readme_update_queued`) |
| 5 | `RELATIONSHIP.md` is edited in this run: pricing, Documents on File, Change Log, Last Updated (`file_modified`, `lines_added`) |
| 6 | The pricing block is complete (`pricing_block_complete` must be true) |
| 7 | A handoff to `update-project-md` if a project is involved (`handoff_playbook_id`, `handoff_inputs`) |

**Inputs.** Required: `supplier_folder_path`, `canonical_pdf_path`, `document_type`, `source_email_from`, `source_email_date`. Recommended: `project_path`, `project_attachment_path`, `list_price`, `list_price_tax_basis`, `discount_pct`, `net_price`, `tax_treatment_note`, `quote_status`, `model_name`, `client_context`.

**The run fails if:** `RELATIONSHIP.md` is not updated in the same run; the price arithmetic is not written out (it must read `US$X less Y% = US$Z`); or the tax basis is unclear (it must say `ex-tax`, `incl-vat` or `incl-gst`).

**How to run it:**

1. `playbook__start` with `playbook_id: "file-supplier-document"`, the inputs, and `trigger` describing the email it came from.
2. Walk steps 1 to 7 with `playbook__step`.
3. If step 7 hands off, start `update-project-md` with the handoff inputs (then `update-project-overview-html`, see `splatt-project-files`).

## Rule 2: no supplier price reaches a client (`price-leak-guard`)

**When:** before any client-facing message that contains a figure: an email written for the operator, a quote document, a Telegram message outside the internal channels. It cannot be skipped unless the operator says "skip the guard" in the same turn.

**Why:** Splatt's margin is the difference between the supplier's net price and the client's price. A supplier figure in a client email, even converted into another currency, gives it away.

| Step | What it checks |
|---|---|
| 1 | Who the recipients are: client, internal, vendor, sensitive (`any_client_recipients`, `sensitive_account`) |
| 2 | Every supplier price from every `_Suppliers/*/RELATIONSHIP.md` (`supplier_corpus`, `corpus_size` > 0) |
| 3 | Every money-shaped figure in the outbound text (`outbound_figures`) |
| 4 | Exact matches (`exact_matches`, `exact_match_count`) |
| 5 | Matches within 2% in the same currency (`near_matches`, `near_match_count`) |
| 6 | Matches within 2% after currency conversion (`fx_matches`, `fx_match_count`, `fx_rates_used`) |
| 7 | The verdict (`verdict`, `reason`, `suggested_replacement_or_null`) |
| 8 | The verdict shown to the operator (`decision_surfaced_to_user`, `send_authorized`) |

The verdict, checked top to bottom:

```
skip_guard set by the operator this turn ............ PASS_OVERRIDDEN
no client recipients ................................ PASS_INTERNAL
exact_match_count > 0 ............................... BLOCK
near_match_count > 0 or fx_match_count > 0 .......... FLAG
sensitive client, figures present, no sign-off ...... REQUIRE_SIGN_OFF
otherwise ........................................... PASS
```

**Inputs.** Required: `outbound_text`, `recipient_addresses`, `outbound_type`. Optional: `splatt_markup_factor`, `fx_rate_overrides`, `skip_guard`, `sign_off_token`.

**Invariants:** the guard only reads `RELATIONSHIP.md` files and never edits them; it never sends anything, it only authorises or blocks; and it always runs to step 8, even on an early pass, so every check is logged.

**How to run it:**

1. `playbook__start` with `playbook_id: "price-leak-guard"` and the full draft, the recipients and the type:

   ```text
   playbook__start({
     playbook_id: "price-leak-guard",
     inputs: {
       outbound_text: "<full draft body>",
       recipient_addresses: ["alex.carter@example.com"],
       outbound_type: "email"
     },
     trigger: "pre-send check for an email to Alex Carter"
   })
   ```

2. Walk steps 1 to 8.
3. Hand the email to the operator only when step 8 reports `send_authorized: true`. On BLOCK, FLAG or REQUIRE_SIGN_OFF, show the operator the matched figure, where it came from and the sentence it is in, and wait.

## How the rules connect to other skills

| Skill or playbook | Connection |
|---|---|
| `splatt-email` | Runs `price-leak-guard` before handing over any email to a client that contains a figure |
| `splatt-project-files` | A supplier PDF filed for a project goes through `file-supplier-document`; its step 7 hands off to `update-project-md` |
| `splatt-ss-agent` | During an ops run, a supplier email with an attachment starts `file-supplier-document`; any client-facing message with figures runs the guard first |
| `splatt-suppliers` | New supplier folders are created through `file-supplier-document`; the `RELATIONSHIP.md` files are what the guard reads |
| `update-project-md` | A `new_quote` update that came from a supplier email must follow a `file-supplier-document` run for that supplier |

## Examples

All names and figures below are fictional.

### Filing a brochure with an indicative price

Christo Andersen at Aquaform emails a Cobalt 3S ozone generator brochure and an indicative price for Alex Carter's triblock project.

1. Save the brochure as `_Suppliers/Aquaform/Aquaform-Cobalt-3S-Brochure-2023.pdf`.
2. Save the project copy as `Alex Carter/Triblock and Pretreatment Line/attachments/Aquaform-Cobalt-3S-Brochure-2026-05-10.pdf`.
3. Start `file-supplier-document` with `document_type: brochure`, `source_email_from: christo@aquaform.example.co.za`, `source_email_date: 2026-05-10`, `list_price: "US$4,000"`, `list_price_tax_basis: "ex-tax"`, `discount_pct: 25`, `net_price: "US$3,000 ex-tax"`, `quote_status: indicative`, `model_name: "Cobalt 3S"`, `client_context: "Alex Carter, Triblock and Pretreatment Line"`, and the project path.
4. Walk steps 1 to 7. `RELATIONSHIP.md` now reads `US$4,000 less 25% = US$3,000 ex-tax`. Step 7 hands off to `update-project-md`.
5. Run `update-project-md` and `update-project-overview-html`.
6. Log the interaction in PocketBase.
7. Send a `project_update` Telegram that says "indicative price logged, see RELATIONSHIP.md" and contains no supplier price.

The wrong way is to drop the PDF in the folder and edit the project file by hand: the price then lives only in one client's project, and nothing checked the tax basis or the arithmetic.

### A clean pass

Draft to Alex Carter: "For ozonation we recommend the Aquaform Cobalt 3S with install support, at NZ$6,000 + GST."

- Step 3 finds one figure, NZ$6,000.
- Steps 4 and 5 find nothing: every supplier figure for this model is in US dollars.
- Step 6 converts US$3,000 at 1.65 to NZ$4,950. NZ$6,000 is about 21% above that, outside the 2% band.
- Step 7: PASS, `send_authorized: true`. The email goes to the operator in the chat.

### A caught leak

Draft: "Aquaform is quoting US$3,000 for the Cobalt 3S, plus our install..."

- Step 4 finds an exact match with the Cobalt 3S net price in `_Suppliers/Aquaform/RELATIONSHIP.md`.
- Step 7: BLOCK.
- Step 8 tells the operator: the figure US$3,000, its source (`_Suppliers/Aquaform/RELATIONSHIP.md`, Active Quotes, Cobalt 3S net), the sentence it is in, and that Splatt's markup for this line is needed. The email is not handed over.

### A caught converted leak

Draft: "Indicative for the ozone unit: NZ$4,960 + GST."

- Step 6 converts US$3,000 at 1.65 to NZ$4,950. NZ$4,960 is 0.2% away, inside the band.
- Step 7: FLAG.
- Step 8 tells the operator the figure is within 0.2% of the converted Aquaform net price and the markup looks missing.

## Operating notes

- `file-supplier-document` is the only way to file a supplier document, and `price-leak-guard` the only way to clear a client-facing message with figures.
- The guard is deliberately over-eager. Running it a hundred times and passing is better than missing one leak.
- Telegram messages about supplier filings never include supplier prices.
- A supplier with no `RELATIONSHIP.md` yet makes the guard FLAG rather than pass, so missing coverage is visible.
