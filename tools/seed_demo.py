"""
Demo data: fill an empty PocketBase with a small, entirely fictional business.

What it is for
    A fresh install starts with an empty database, so the dashboard has
    nothing to show and the validator has nothing to check. This script
    creates a believable sample business so that every part of the system
    can be seen working a few minutes after cloning the repo:

        7 clients (one flagged as a sensitive account, one internal)
        6 contacts, 5 suppliers
        9 jobs, one or more at each stage of the pipeline
        5 quotes, 3 invoices, 3 supplier bills
        9 tasks (the PocketBase mirror of Todoist tasks), spread across
          the board sections, including overdue and waiting ones
        6 interactions (emails, calls and notes)
        5 machines on the equipment map
        2 knowledge-base notes
        1 project money ledger (the Financials panel on the dashboard)

    Every name, email address, phone number and amount is invented.

How it works
    1. It connects with the same PocketBase client the rest of the system
       uses (core/pb.py). That means every record is checked against
       core/schema.py before it is sent, and read back after it is saved.
       If the demo data and the schema ever disagree, this script fails
       loudly instead of creating half a demo.
    2. The client is wrapped in the ledger's RecordingClient, so every
       write is also appended to the write ledger (core/ledger.py),
       exactly as a daemon pass would do it.
    3. Records are created in dependency order (clients before jobs, jobs
       before quotes). Each record in the data below has a short key such
       as "clearwater", and a value written as "@client:clearwater" is
       replaced with the real PocketBase id of that record once it exists.
    4. Dates are written relative to today, so the demo always has
       something overdue, something due this week and something waiting,
       whatever day it is run.

Safety
    - Dry run by default. Without --write it only prints what it would
      create and touches nothing.
    - It refuses to write into a database that already has clients, so it
      cannot be run against real data by accident. --force overrides this
      for a database you know is a throwaway.

Usage
    python -m tools.seed_demo            show what would be created
    python -m tools.seed_demo --write    create it

Exit codes
    0  done (or dry run printed)
    1  refused (the database already has data and --force was not given)
    2  could not run (no configuration, or PocketBase unreachable)
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from core import interactions
from core.config import ConfigError, settings
from core.ledger import RecordingClient, WriteLedger, ledger_path
from core.pb import PocketBaseClient, PocketBaseError

TODAY = date.today()

# Section names as they appear on the Todoist board. The dashboard works out
# which column a task is in by looking for words such as "overdue" or
# "waiting on client" inside these names, so the emoji are decoration only.
# tools/todoist_setup.py creates the real board sections with the same names.
SECTION = {
    "today": "📌 Today",
    "overdue": "🔴 Overdue",
    "upcoming": "📅 Upcoming",
    "waiting_client": "⏳ Waiting on Client",
    "waiting_supplier": "📦 Waiting on Supplier",
    "stalled": "🧊 Stalled",
    "backlog": "🗂 Backlog",
}


def day(offset):
    """Today plus `offset` days, as the YYYY-MM-DD string PocketBase expects."""
    return (TODAY + timedelta(days=offset)).isoformat()


def pin(offset):
    """The real-due pin line that the overdue engine reads from a task.

    A task's "real due date" is the date it was first due. The daemon
    keeps it on the first lines of the Todoist description in this exact
    form, because the visible Todoist due date gets moved ("parked") every
    day and so cannot be used to measure how late something really is.
    """
    days_late = -offset
    if days_late > 0:
        age = "%d days late" % days_late
    elif days_late == 0:
        age = "due today"
    else:
        age = "due in %d days" % -days_late
    return "📌 Real due: %s | %s" % (day(offset), age)


# ── The demo business ────────────────────────────────────────────────────
#
# Client addresses end in "| GPS: lat, lng". The dashboard's maps read the
# coordinates from that tag (tools/geocode_clients.py adds it on a real
# database), so the demo clients show up on the map without geocoding.
#
# Each list is (key, payload). The key is only used inside this script, to
# let later records point at earlier ones with "@collection:key".

CLIENTS = [
    ("clearwater", {
        "name": "Clearwater Bottling (NZ) Ltd",
        "aliases": "Clearwater Bottling NZ, Clearwater Bottling",
        "email_addresses": "clearwaterbottling.example.co.nz",
        "phone": "+64 7 000 0001",
        "address": "10 Example Road, Hamilton 3200 | GPS: -37.7870, 175.2793",
        "status": "active",
        "notes": "Bottled water and juice. Runs a Vela rotary filler on the main line.",
    }),
    ("orchard", {
        "name": "Orchard Lane Fruit Processors Ltd",
        "aliases": "Orchard Lane, Orchard Lane Cromwell",
        "email_addresses": "orchardlane.example.co.nz",
        "phone": "+64 3 000 0002",
        "address": "5 Sample Lane, Cromwell 9310 | GPS: -45.0400, 169.1990",
        "status": "active",
        "notes": "Juice processor. Taponera capper; adding a 375 mL bottle.",
    }),
    ("hilltop", {
        "name": "Hill Top Foods Ltd",
        "aliases": "Hilltop, Hilltop Foods, Hill Top",
        "email_addresses": "hilltop.example.co.nz",
        "phone": "+64 7 000 0003",
        "address": "22 Demo Street, Morrinsville 3300 | GPS: -37.6567, 175.5303",
        "status": "active",
        "notes": "Sauces and dressings. Rialto rinser due for a refurbishment.",
    }),
    ("kauri", {
        "name": "Kauri Springs Ltd",
        "aliases": "Kauri",
        "email_addresses": "kaurisprings.example.com",
        "phone": "+64 7 000 0004",
        "address": "12 Example Road, RD 2, Te Puke 3182 | GPS: -37.7850, 176.3260",
        "status": "active",
        "notes": "Spring water bottler. Filler upgraded this year.",
    }),
    ("tui", {
        "name": "Tui Valley Waters Ltd",
        "aliases": "Tui Valley, Tui Valley NZ",
        "email_addresses": "tuivalley.example.com",
        "phone": "+64 7 000 0005",
        "address": "8 Sample Court, Te Awamutu 3800 | GPS: -38.0100, 175.3250",
        "status": "prospect",
        "notes": "Prospect. Interested in a cap dryer once budget is approved.",
    }),
    ("kowhai", {
        "name": "Kowhai Health NZ Ltd",
        "aliases": "Kowhai Health, KHNZ",
        "email_addresses": "kowhaihealth.example.co.nz",
        "address": "30 Example Street, Tauranga 3110 | GPS: -37.6870, 176.1650",
        "status": "active",
        # A critical client is a sensitive account: the agent only reports
        # on it and never drafts anything (see skills/splatt-ss-agent).
        "critical": True,
        "critical_note": "Sensitive account. Report only; the operator handles all replies.",
    }),
    ("internal", {
        # Splatt's own work (buying stock, updating price lists) still
        # needs a client link so it is not reported as a data gap. The
        # dashboard recognises this record by its name.
        "name": "Splatt Engineering (internal)",
        "status": "active",
        "notes": "Internal work with no customer attached.",
    }),
]

CONTACTS = [
    ("nigel", {"name": "Nigel Grant", "role": "Maintenance Manager", "client": "@clients:clearwater",
               "email": "nigel@clearwaterbottling.example.co.nz", "phone": "+64 21 000 0001"}),
    ("rebecca", {"name": "Rebecca Hall", "role": "Accounts Payable", "client": "@clients:clearwater",
                 "email": "accounts@clearwaterbottling.example.co.nz"}),
    ("dan", {"name": "Dan Bennett", "role": "Production Engineer", "client": "@clients:orchard",
             "email": "dan@orchardlane.example.co.nz", "phone": "+64 21 000 0002"}),
    ("sally", {"name": "Sally Park", "role": "Operations Manager", "client": "@clients:hilltop",
               "email": "sally@hilltop.example.co.nz"}),
    ("doug", {"name": "Doug Robson", "role": "Site Manager", "client": "@clients:kauri",
              "email": "doug@kaurisprings.example.com"}),
    ("kyle", {"name": "Kyle Turner", "role": "Plant Manager", "client": "@clients:tui",
              "email": "kyle@tuivalley.example.com"}),
]

SUPPLIERS = [
    ("vela", {"name": "Vela Technologies", "aliases": "Vela", "contact_name": "Livia Gallo",
              "email": "livia.gallo@vela-tech.example.it", "specialty": "Rotary fillers and spare parts"}),
    ("taponera", {"name": "Taponera SL", "aliases": "Taponera", "contact_name": "Eduardo Morales",
                  "email": "eduardo@taponera.example.es", "specialty": "Screw and crown cappers"}),
    ("rialto", {"name": "Rialto Filling Srl", "aliases": "Rialto", "contact_name": "Roberta Rinaldi",
                "email": "roberta@rialtofilling.example.it", "specialty": "Rinsers and triblocks"}),
    ("cyclone", {"name": "Cyclone Air Systems", "aliases": "Cyclone", "contact_name": "Paul Reed",
                 "email": "paul@cycloneair.example.com", "specialty": "Air knives and dryers"}),
    ("coastline", {"name": "Coastline Freight", "aliases": "Coastline", "contact_name": "Tom Gibbs",
                   "email": "tom@coastlinefreight.example.co.nz", "specialty": "Freight forwarding and customs"}),
]


def job(title, client, status, value, changed, **extra):
    """One job record. `changed` is how many days ago it reached its status."""
    payload = {
        "title": title,
        "client": "@clients:%s" % client,
        "status": status,
        "value": value,
        "status_changed_at": day(-changed),
        "status_changed_by": "seed_demo",
    }
    payload.update(extra)
    return payload


JOBS = [
    ("clearwater_service", job("Filler Service 2026", "clearwater", "won", 18400, 45,
                               start_date=day(-50), due_date=day(5), has_shipping=True,
                               description="Annual filler service and 24 replacement filling valves.")),
    ("orchard_parts", job("Capper Change Parts", "orchard", "quoting", 9800, 25, due_date=day(10),
                          description="Change parts for a new 375 mL bottle.")),
    ("hilltop_rinser", job("Rinser Refurbishment", "hilltop", "quoted", 24500, 12, start_date=day(-20),
                           description="Strip down and refurbish the bottle rinser.")),
    ("kauri_filler", job("Filler Upgrade", "kauri", "invoicing", 41200, 3, start_date=day(-90),
                         description="Filler upgrade, installed and commissioned.")),
    ("kauri_valves", job("Spare Valve Kits", "kauri", "invoiced", 3600, 25)),
    ("tui_dryer", job("Cap Dryer Installation", "tui", "on_hold", 15800, 30,
                      description="On hold until the client's budget is approved.")),
    ("hilltop_guards", job("Conveyor Guard Rails", "hilltop", "completed", 5200, 15,
                           paid=True, paid_amount=5200, paid_at=day(-15))),
    ("orchard_labeller", job("Labeller Service", "orchard", "lost", 2900, 40)),
    ("clearwater_rinse", job("Rinse Water Assessment", "clearwater", "commissioning", 7400, 6)),
]

QUOTES = [
    ("q1", {"title": "QU-8101 Filler service and valve kits", "client": "@clients:clearwater",
            "job": "@jobs:clearwater_service", "amount": 18400, "status": "accepted",
            "sent_date": day(-51), "valid_until": day(-21)}),
    ("q2", {"title": "QU-8102 Rinser refurbishment", "client": "@clients:hilltop",
            "job": "@jobs:hilltop_rinser", "amount": 24500, "status": "sent",
            "sent_date": day(-12), "valid_until": day(18)}),
    ("q3", {"title": "QU-8103 Filler upgrade", "client": "@clients:kauri",
            "job": "@jobs:kauri_filler", "amount": 41200, "status": "accepted",
            "sent_date": day(-120), "valid_until": day(-90)}),
    ("q4", {"title": "QU-8104 Cap dryer installation", "client": "@clients:tui",
            "job": "@jobs:tui_dryer", "amount": 15800, "status": "sent",
            "sent_date": day(-33), "valid_until": day(-3)}),
    ("q5", {"title": "QU-8105 Labeller service", "client": "@clients:orchard",
            "job": "@jobs:orchard_labeller", "amount": 2900, "status": "declined",
            "sent_date": day(-45)}),
]

INVOICES = [
    ("i1", {"invoice_number": "INV-40101", "client": "@clients:kauri", "job": "@jobs:kauri_valves",
            "description": "Spare valve kits", "amount": 4140, "status": "sent", "paid": False,
            "issue_date": day(-25), "due_date": day(-5)}),
    ("i2", {"invoice_number": "INV-40102", "client": "@clients:hilltop", "job": "@jobs:hilltop_guards",
            "description": "Conveyor guard rails", "amount": 5980, "status": "paid", "paid": True,
            "amount_paid": 5980, "issue_date": day(-40), "due_date": day(-20), "paid_date": day(-15)}),
    ("i3", {"invoice_number": "INV-40103", "client": "@clients:kauri", "job": "@jobs:kauri_filler",
            "description": "Filler upgrade", "amount": 47380, "status": "draft", "paid": False,
            "issue_date": day(0), "due_date": day(20)}),
]

BILLS = [
    ("b1", {"description": "Vela valve kits x 24", "type": "parts", "job": "@jobs:clearwater_service",
            "client": "@clients:clearwater", "amount": 10800, "paid": True, "amount_paid": 10800,
            "paid_date": day(-30)}),
    ("b2", {"description": "Coastline Freight: air freight for valve kits", "type": "shipping",
            "job": "@jobs:clearwater_service", "client": "@clients:clearwater", "amount": 1150,
            "paid": True, "amount_paid": 1150, "paid_date": day(-18)}),
    ("b3", {"description": "Rialto rinser seal kit", "type": "parts", "job": "@jobs:hilltop_rinser",
            "client": "@clients:hilltop", "amount": 8200, "paid": False, "due_date": day(14)}),
]

EQUIPMENT = [
    ("e1", {"client": "@clients:clearwater", "site_name": "Hamilton plant", "category": "filler",
            "description": "Rotary filler, main line", "make": "Vela", "model": "3000-01", "serial": "1001",
            "installed_year": "2016", "status": "active", "location": "Hamilton",
            "lat": -37.787, "lng": 175.279, "coords_precise": False, "confidence": "confirmed", "source": "demo"}),
    ("e2", {"client": "@clients:orchard", "site_name": "Cromwell packhouse", "category": "capper",
            "description": "Screw capper", "make": "Taponera", "model": "TCP-100", "serial": "1000-23",
            "installed_year": "2019", "status": "active", "location": "Cromwell",
            "lat": -45.04, "lng": 169.20, "coords_precise": False, "confidence": "confirmed", "source": "demo"}),
    ("e3", {"client": "@clients:hilltop", "site_name": "Morrinsville plant", "category": "rinser",
            "description": "Bottle rinser", "make": "Rialto", "model": "RC20/800", "installed_year": "2012",
            "status": "active", "location": "Morrinsville", "lat": -37.657, "lng": 175.530,
            "coords_precise": False, "confidence": "confirmed", "source": "demo"}),
    ("e4", {"client": "@clients:kauri", "site_name": "Te Puke plant", "category": "filler",
            "description": "Filler, upgraded this year", "make": "Vela", "model": "40-40-10",
            "installed_year": "2021", "status": "active", "location": "Te Puke",
            "lat": -37.785, "lng": 176.326, "coords_precise": False, "confidence": "confirmed", "source": "demo"}),
    ("e5", {"client": "@clients:tui", "site_name": "Te Awamutu plant", "category": "dryer",
            "description": "Cap dryer (proposed)", "make": "Cyclone", "status": "prospect",
            "location": "Te Awamutu", "lat": -38.010, "lng": 175.325, "coords_precise": False,
            "confidence": "probable", "source": "demo"}),
]

KNOWLEDGE = [
    ("k1", {"title": "Freight is recharged with a margin", "category": "billing", "tags": "freight, invoicing",
            "content": "Freight paid to a forwarder is recharged to the client as a taxable line with a "
                       "margin (default 20%). The validator's money rules check it is not forgotten."}),
    ("k2", {"title": "Import duty is recharged at cost", "category": "billing", "tags": "duty, customs",
            "content": "Customs duty is passed on at cost with no GST and no margin."}),
]


def task(tid, content, section, due=None, real_due=None, status="open", **extra):
    """One assignment: the PocketBase mirror of a Todoist task.

    On a live system the daemon writes these from Todoist. The demo writes
    them directly so the dashboard's board, workload and overdue views have
    something to show without a Todoist account.
    """
    lines = []
    if real_due is not None:
        lines.append(pin(real_due))
    lines.append("⏱ Est: 15m")
    payload = {
        "todoist_id": tid,
        "content": content,
        "description": "\n".join(lines + [extra.pop("note", "")]).strip(),
        "status": status,
        "priority": extra.pop("priority", 1),
        "source": "manual",
        "project_name": "Splatt Ops",
        "section_name": SECTION[section],
    }
    if due is not None:
        payload["due_date"] = day(due)
    if real_due is not None:
        payload["real_due"] = day(real_due)
    payload.update(extra)
    return payload


ASSIGNMENTS = [
    ("t1", task("demo-101", "Clearwater Bottling — Confirm technician site induction", "today", due=0,
                client="@clients:clearwater", job="@jobs:clearwater_service")),
    ("t2", task("demo-102", "Coastline Freight — Freight quote for Orchard Lane change parts", "waiting_supplier",
                due=2, client="@clients:orchard", supplier="@suppliers:coastline", job="@jobs:orchard_parts")),
    ("t3", task("demo-103", "Orchard Lane — Send change parts quote", "upcoming", due=3,
                client="@clients:orchard", job="@jobs:orchard_parts", on_complete_job_status="quoted",
                note="🔁 On complete: job to quoted")),
    ("t4", task("demo-104", "Hill Top — Chase rinser quote QU-8102", "overdue", due=-1, real_due=-4,
                client="@clients:hilltop", job="@jobs:hilltop_rinser", labels="escalated")),
    ("t5", task("demo-105", "Kauri Springs — Raise filler upgrade invoice", "today", due=0,
                client="@clients:kauri", job="@jobs:kauri_filler", on_complete_job_status="invoiced",
                note="🔁 On complete: job to invoiced")),
    ("t6", task("demo-106", "Tui Valley — Check whether the dryer budget is approved", "waiting_client", due=5,
                client="@clients:tui", job="@jobs:tui_dryer")),
    ("t7", task("demo-107", "Kauri Springs — Chase payment of INV-40101", "overdue", due=-1, real_due=-9,
                client="@clients:kauri", job="@jobs:kauri_valves", labels="escalated")),
    ("t8", task("demo-108", "Update the supplier price list", "backlog", client="@clients:internal")),
    ("t9", task("demo-109", "Clearwater Bottling — Book freight for valve kits", "today", due=-20,
                status="completed", client="@clients:clearwater", job="@jobs:clearwater_service")),
]

# Interactions are written through core.interactions.save, the one
# sanctioned write path for them, so each carries a source_key and running
# this twice could never create duplicates.
INTERACTIONS = [
    ("demo-1", {"type": "email", "client": "@clients:clearwater", "contact": "@contacts:nigel",
                "job": "@jobs:clearwater_service", "subject": "PO-6601 for the filler service",
                "summary": "Client accepted QU-8101 and chose the shutdown date.", "date": day(-45)}),
    ("demo-2", {"type": "email", "client": "@clients:orchard", "contact": "@contacts:dan",
                "job": "@jobs:orchard_parts", "subject": "375 mL change parts",
                "summary": "Client sent bottle drawings and asked for a price.", "date": day(-25)}),
    ("demo-3", {"type": "call", "client": "@clients:hilltop", "contact": "@contacts:sally",
                "job": "@jobs:hilltop_rinser", "subject": "Rinser quote follow-up",
                "summary": "Client is comparing the quote with a second supplier; decision next week.",
                "date": day(-5)}),
    ("demo-4", {"type": "email", "client": "@clients:tui", "contact": "@contacts:kyle",
                "job": "@jobs:tui_dryer", "subject": "Dryer project on hold",
                "summary": "Budget approval moved to next quarter.", "date": day(-30)}),
    ("demo-5", {"type": "note", "client": "@clients:kauri", "job": "@jobs:kauri_filler",
                "subject": "Commissioning complete",
                "summary": "Filler upgrade commissioned and signed off on site.", "date": day(-8)}),
    ("demo-6", {"type": "email", "client": "@clients:clearwater", "supplier": "@suppliers:coastline",
                "job": "@jobs:clearwater_service", "subject": "Valve kits delivered",
                "summary": "Forwarder confirmed delivery to the workshop.", "date": day(-19)}),
]


def financial_lines():
    """The money ledger for one project, shown in the dashboard's Financials panel.

    project_key and job hold the job's PocketBase id; the dashboard looks
    lines up by that id. These are built after the jobs exist.
    """
    return [
        ("f1", {"project_key": "@jobs:clearwater_service", "job": "@jobs:clearwater_service",
                "client": "@clients:clearwater", "direction": "in", "kind": "client_invoice",
                "billed_via": "splatt", "ref": "Planned invoice", "party": "Clearwater Bottling (NZ) Ltd",
                "description": "Filler service and valve kits", "currency": "NZD", "amount_gross": 21160,
                "gst_treatment": "incl_15", "status": "not_raised", "parse_status": "manual"}),
        ("f2", {"project_key": "@jobs:clearwater_service", "job": "@jobs:clearwater_service",
                "client": "@clients:clearwater", "direction": "out", "kind": "supplier_bill",
                "billed_via": "splatt", "ref": "SPO-2201", "party": "Vela Technologies",
                "description": "Valve kits x 24", "currency": "EUR", "amount_gross": 6000, "fx_rate": 1.8,
                "gst_treatment": "no_gst", "status": "paid", "parse_status": "confirmed",
                "date_paid": day(-30)}),
        ("f3", {"project_key": "@jobs:clearwater_service", "job": "@jobs:clearwater_service",
                "client": "@clients:clearwater", "direction": "out", "kind": "freight",
                "billed_via": "splatt", "ref": "CF-0138", "party": "Coastline Freight",
                "description": "Air freight, Italy to Auckland", "currency": "NZD", "amount_gross": 1322.50,
                "gst_treatment": "incl_15", "status": "paid", "parse_status": "confirmed",
                "date_paid": day(-18)}),
    ]


# ── Writing ──────────────────────────────────────────────────────────────

# The order records are created in. Each collection only points at
# collections earlier in this list.
PLAN = [
    ("clients", CLIENTS),
    ("contacts", CONTACTS),
    ("suppliers", SUPPLIERS),
    ("jobs", JOBS),
    ("quotes", QUOTES),
    ("invoices", INVOICES),
    ("bills", BILLS),
    ("equipment", EQUIPMENT),
    ("knowledge_base", KNOWLEDGE),
    ("assignments", ASSIGNMENTS),
]


def resolve(payload, ids):
    """Replace every "@collection:key" value with the id of that record.

    `ids` maps (collection, key) to a PocketBase id and grows as records
    are created. A reference to something not created yet is a mistake in
    the data above, so it raises rather than writing a broken link.
    """
    out = {}
    for field, value in payload.items():
        if isinstance(value, str) and value.startswith("@"):
            collection, _, key = value[1:].partition(":")
            if (collection, key) not in ids:
                raise KeyError("%s refers to %s before it exists" % (field, value))
            value = ids[(collection, key)]
        out[field] = value
    return out


def summary():
    """How many records of each kind the demo contains."""
    rows = [(name, len(items)) for name, items in PLAN]
    rows.append(("interactions", len(INTERACTIONS)))
    rows.append(("project_financials", len(financial_lines())))
    return rows


def seed(pb, ledger):
    """Create the whole demo. Returns {collection: number created}."""
    ids = {}
    created = {}
    for collection, items in PLAN:
        for key, payload in items:
            record = pb.create(collection, resolve(payload, ids))
            ids[(collection, key)] = record["id"]
        created[collection] = len(items)

    for source_id, fields in INTERACTIONS:
        interactions.save(pb, "manual", source_id, ledger=ledger, **resolve(fields, ids))
    created["interactions"] = len(INTERACTIONS)

    lines = financial_lines()
    for key, payload in lines:
        pb.create("project_financials", resolve(payload, ids))
    created["project_financials"] = len(lines)
    return created


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--write", action="store_true", help="create the records (default is a dry run)")
    parser.add_argument("--force", action="store_true",
                        help="write even though the database already has clients")
    args = parser.parse_args(argv)

    print("Demo data (all fictional), dated relative to %s:" % TODAY.isoformat())
    for name, count in summary():
        print("  %-20s %d" % (name, count))

    if not args.write:
        print("\nDry run. Nothing was written. Add --write to create these records.")
        return 0

    try:
        conf = settings()
        client = PocketBaseClient(**conf.pocketbase())
        client.auth_admin()
    except (ConfigError, PocketBaseError, OSError) as exc:
        print("\nCould not connect to PocketBase: %s" % exc, file=sys.stderr)
        print("Check .env and that PocketBase is running (see docs/INSTALL.md).", file=sys.stderr)
        return 2

    existing = client.count("clients")
    if existing and not args.force:
        print("\nRefusing: the database already has %d clients. The demo is meant for an "
              "empty database. Use --force only on a throwaway database." % existing, file=sys.stderr)
        return 1

    ledger = WriteLedger(ledger_path(conf))
    pb = RecordingClient(client, ledger, source="tools.seed_demo")
    created = seed(pb, ledger)
    print("\nCreated %d records. Open the dashboard to see them." % sum(created.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
