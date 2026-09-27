"""
PocketBase MCP server: how the agent reads and writes the operations database.

What it is
    An MCP (Model Context Protocol) server that runs over stdio. The agent
    (Claude, running with the skills in `skills/`) calls its tools to look
    up and change clients, jobs, quotes, tasks and the rest of the business
    records stored in PocketBase. Start it with `python -m mcp_server`.

The 31 tools, by group
    Clients (5)          search_clients, get_client, create_client,
                         update_client, get_client_history
    Contacts (3)         search_contacts, create_contact, update_contact
    Jobs (3)             search_jobs, create_job, update_job
    Quotes (3)           search_quotes, create_quote, update_quote
    Suppliers (3)        search_suppliers, create_supplier, update_supplier
    Interactions (3)     search_interactions, log_interaction,
                         update_interaction
    Assignments (2)      search_assignments, update_assignment
                         (assignments are the PocketBase mirror of Todoist tasks)
    Knowledge base (2)   search_knowledge, add_knowledge
    Summary and logs (2) get_daily_summary, get_sync_log
    Task groups (4)      create_task_group, update_task_group,
                         search_task_groups, get_task_group
    Raw read (1)         query_collection, for questions no other tool answers

How a write works
    1. Every write goes through `core.pb.PocketBaseClient`, the same client
       the sync daemon and the tools use.
    2. Before sending, the client checks the payload against
       `core/schema.py`. An unknown field or a bad select value is refused
       and nothing is sent.
    3. After PocketBase answers, the client compares the stored record with
       what was sent. A field that did not save raises `WriteNotLanded`.
    4. Interactions are written with `core.interactions.save`, which keys
       each record on its `source_key` (for example `gmail:<message id>`).
       Logging the same email twice updates one record instead of making
       two.
    5. If the admin token has expired, the client signs in again and
       retries the call once.

How errors reach the agent
    The `tool` decorator below catches every exception and returns a plain
    sentence instead of a stack trace, so the agent can tell "refused,
    nothing was written" apart from "sent, but did not save".

Why the schema check happens here
    PocketBase silently drops fields it does not know. Without the check, a
    write with a misspelled field name would return success and store
    nothing, and the agent would report work that was never saved.
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from core import interactions, job_status
from core.config import ConfigError, settings
from core.pb import PocketBaseClient, PocketBaseError, SchemaViolation, WriteNotLanded

from . import output

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("pocketbase-mcp")

mcp = FastMCP("pocketbase")

_pb = None


def pb():
    """Return the shared PocketBase client, creating and signing it in on first use.

    It is created lazily so that importing this module (for example in a
    test) needs neither a running database nor a config file.
    """
    global _pb
    if _pb is None:
        conf = settings()
        client = PocketBaseClient(**conf.pocketbase())
        client.auth_admin()
        _pb = client
    return _pb


def now():
    """The current time in UTC."""
    return datetime.now(timezone.utc)


def stamp(when=None):
    """A datetime in the "YYYY-MM-DD HH:MM:SS" form PocketBase filters compare against."""
    return (when or now()).strftime("%Y-%m-%d %H:%M:%S")


def days_ago(days):
    """The stamp for `days` days before now, for "created in the last N days" filters."""
    return stamp(now() - timedelta(days=days))


def today():
    """Today's date in UTC, as YYYY-MM-DD."""
    return now().strftime("%Y-%m-%d")


def tool(func):
    """Register `func` as an MCP tool that always returns a sentence, never raises.

    An exception inside an MCP tool reaches the agent as a stack trace.
    A short sentence is something the agent can act on, so every failure
    is caught here and reported with its kind:

    - "Refused, nothing was written": the schema check (or the interaction
      rules) rejected the payload before it was sent. The database is
      unchanged.
    - "The write did not save": PocketBase accepted the call, but the
      record it returned does not hold the value that was sent.
    - "PocketBase refused this": PocketBase answered with an HTTP error.
    - "Configuration problem": settings or credentials are missing.

    The first two are kept apart on purpose: one means "fix the payload",
    the other means "the database lost data", and they need different
    responses.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SchemaViolation as exc:
            log.warning("refused: %s", exc)
            return "Refused, nothing was written.\n%s" % exc
        except WriteNotLanded as exc:
            log.error("write did not land: %s", exc)
            return "The write did not save.\n%s" % exc
        except interactions.InteractionError as exc:
            return "Refused, nothing was written.\n%s" % exc
        except PocketBaseError as exc:
            log.error("pocketbase error: %s", exc)
            return "PocketBase refused this.\n%s" % exc
        except ConfigError as exc:
            return "Configuration problem.\n%s" % exc
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected failure in %s", func.__name__)
            return "Failed: %s: %s" % (type(exc).__name__, exc)

    return mcp.tool()(wrapper)


def page_of(collection, filter_str="", sort="-created", page=1, per_page=30, expand=""):
    """One page of records from a collection, newest first by default."""
    return pb().list(collection, filter_str=filter_str, sort=sort,
                     page=page, per_page=per_page, expand=expand)


def only_given(**fields):
    """Keep only the fields the caller actually supplied.

    Every tool here uses an empty value ("", None, 0 or []) to mean "not
    supplied". Sending that value would clear what is already stored,
    which is not what leaving an argument out means.

    >>> only_given(name="Acme", phone="", value=0)
    {'name': 'Acme'}
    """
    return {k: v for k, v in fields.items() if v not in ("", None, 0, [])}


# ===================================================================
# Clients
# ===================================================================
@tool
def search_clients(query: str = "", status: str = "", page: int = 1, per_page: int = 30) -> str:
    """Search clients by name, alias, or email. Leave query empty to list all.

    Args:
        query: Search term, matches name, aliases and email_addresses.
        status: Filter by status: active, inactive, prospect.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("clients", output.and_filters([
        "name~'%s' || aliases~'%s' || email_addresses~'%s'" % (q, q, q) if query else "",
        "status='%s'" % output.escape(status) if status else "",
    ]), page=page, per_page=per_page)
    return output.as_lines(data.get("items", []),
                           ["name", "status", "aliases", "email_addresses", "phone"],
                           "client", data.get("totalItems"))


@tool
def get_client(client_id: str) -> str:
    """Get full details for a single client.

    Args:
        client_id: PocketBase record id.
    """
    return output.as_json(pb().get("clients", client_id))


@tool
def create_client(name: str, email_addresses: str = "", aliases: str = "",
                  phone: str = "", address: str = "", status: str = "active",
                  notes: str = "") -> str:
    """Create a client.

    Args:
        name: Canonical client name.
        email_addresses: Comma separated email addresses.
        aliases: Comma separated alternative names.
        phone: Phone number.
        address: Address.
        status: active, inactive or prospect.
        notes: Notes about this client.
    """
    record = pb().create("clients", {
        "name": name, "email_addresses": email_addresses, "aliases": aliases,
        "phone": phone, "address": address, "status": status, "notes": notes,
    })
    return "Client '%s' created, id %s" % (name, record["id"])


@tool
def update_client(client_id: str, name: str = "", add_alias: str = "", add_email: str = "",
                  phone: str = "", address: str = "", status: str = "", notes: str = "") -> str:
    """Update a client. Only the fields you pass are changed.

    Args:
        client_id: PocketBase record id.
        name: New name.
        add_alias: An alias to add to the existing list.
        add_email: An email to add to the existing list.
        phone: New phone.
        address: New address.
        status: New status.
        notes: New notes, replaces what is there.
    """
    current = pb().get("clients", client_id)
    data = only_given(name=name, phone=phone, address=address, status=status, notes=notes)

    for value, field in ((add_alias, "aliases"), (add_email, "email_addresses")):
        if not value:
            continue
        have = [p.strip() for p in (current.get(field) or "").split(",") if p.strip()]
        if value.strip() not in have:
            have.append(value.strip())
        data[field] = ", ".join(have)

    if not data:
        return "Nothing to update."
    record = pb().update("clients", client_id, data)
    return "Client updated: %s" % record.get("name", client_id)


@tool
def get_client_history(client_id: str, days_back: int = 90) -> str:
    """Everything on one client: contacts, jobs, quotes, interactions, tasks.

    Args:
        client_id: PocketBase record id.
        days_back: How far back the interactions go.
    """
    client = pb().get("clients", client_id)
    cutoff = days_ago(days_back)
    owned = "client='%s'" % output.escape(client_id)

    out = [
        "# Client: %s" % client.get("name", "?"),
        "Status: %s | Phone: %s" % (client.get("status", "?"), client.get("phone") or "none"),
        "Emails: %s" % (client.get("email_addresses") or "none"),
        "Aliases: %s" % (client.get("aliases") or "none"),
        "Notes: %s" % (client.get("notes") or "none"),
    ]

    contacts = page_of("contacts", owned, per_page=50).get("items", [])
    out.append("\n## Contacts (%d)" % len(contacts))
    for c in contacts:
        out.append("- %s, %s | %s | %s" % (c.get("name", "?"), c.get("role") or "role unknown",
                                           c.get("email") or "no email", c.get("phone") or "no phone"))

    jobs = page_of("jobs", owned, per_page=50).get("items", [])
    out.append("\n## Jobs (%d)" % len(jobs))
    for j in jobs:
        out.append("- [%s] %s, value %s, due %s" % (
            str(j.get("status", "?")).upper(), j.get("title", "?"),
            j.get("value", "?"), j.get("due_date") or "none"))

    quotes = page_of("quotes", owned, per_page=50).get("items", [])
    out.append("\n## Quotes (%d)" % len(quotes))
    for q in quotes:
        out.append("- [%s] %s, %s, sent %s" % (
            str(q.get("status", "?")).upper(), q.get("title", "?"),
            q.get("amount", "?"), q.get("sent_date") or "none"))

    recent = page_of("interactions", "%s && created>='%s'" % (owned, cutoff),
                     sort="-date", per_page=50).get("items", [])
    out.append("\n## Interactions (%d in the last %d days)" % (len(recent), days_back))
    for i in recent:
        out.append("- [%s] %s, %s" % (i.get("type", "?"), i.get("subject", "?"), i.get("date", "?")))
        if i.get("summary"):
            out.append("  %s" % i["summary"][:200])

    tasks = page_of("assignments", owned, per_page=50).get("items", [])
    out.append("\n## Tasks (%d)" % len(tasks))
    for a in tasks:
        out.append("- [%s] %s, priority %s, due %s" % (
            str(a.get("status", "?")).upper(), a.get("content", "?"),
            a.get("priority", "?"), a.get("due_date") or "none"))

    return "\n".join(out)


# ===================================================================
# Contacts
# ===================================================================
@tool
def search_contacts(query: str = "", client_id: str = "", page: int = 1, per_page: int = 30) -> str:
    """Search contacts by name, email or role.

    Args:
        query: Search term.
        client_id: Only contacts belonging to this client.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("contacts", output.and_filters([
        "name~'%s' || email~'%s' || role~'%s'" % (q, q, q) if query else "",
        "client='%s'" % output.escape(client_id) if client_id else "",
    ]), page=page, per_page=per_page, expand="client")
    return output.as_lines(data.get("items", []),
                           ["name", "email", "phone", "role", "client"],
                           "contact", data.get("totalItems"))


@tool
def create_contact(name: str, email: str = "", phone: str = "", role: str = "",
                   client_id: str = "", notes: str = "") -> str:
    """Create a contact, optionally attached to a client.

    Args:
        name: Full name.
        email: Email address.
        phone: Phone number.
        role: Job title or role.
        client_id: PocketBase id of the client they belong to.
        notes: Notes.
    """
    data = {"name": name, "email": email, "phone": phone, "role": role, "notes": notes}
    if client_id:
        data["client"] = client_id
    record = pb().create("contacts", data)
    return "Contact '%s' created, id %s" % (name, record["id"])


@tool
def update_contact(contact_id: str, name: str = "", email: str = "", phone: str = "",
                   role: str = "", client_id: str = "", notes: str = "",
                   detach_client: bool = False) -> str:
    """Update a contact. Only the fields you pass are changed.

    Args:
        contact_id: PocketBase record id.
        name: New name.
        email: New email address.
        phone: New phone number.
        role: New role.
        client_id: Move the contact to this client.
        notes: New notes.
        detach_client: Remove the contact from its client and leave it
            attached to nobody. Needed because an empty client_id means
            "not supplied", so there is otherwise no way to say "none".
    """
    if client_id and detach_client:
        raise ValueError("Pass client_id or detach_client, not both.")
    data = only_given(name=name, email=email, phone=phone, role=role,
                      notes=notes, client=client_id)
    if detach_client:
        data["client"] = ""
    if not data:
        return "Nothing to update."
    was = (pb().get("contacts", contact_id) or {}).get("client", "") \
        if (client_id or detach_client) else ""
    record = pb().update("contacts", contact_id, data)
    moved = moved_note(record, "client", was, "Client")
    if detach_client and was:
        moved = " Detached from client %s." % was
    return "Contact updated: %s.%s" % (record.get("name", contact_id), moved)


# ===================================================================
# Jobs
# ===================================================================
@tool
def search_jobs(query: str = "", client_id: str = "", status: str = "",
                page: int = 1, per_page: int = 30) -> str:
    """Search jobs by title, client or status.

    Args:
        query: Search term, matches title and description.
        client_id: Only jobs for this client.
        status: quoting, active, on_hold, completed or cancelled.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("jobs", output.and_filters([
        "title~'%s' || description~'%s'" % (q, q) if query else "",
        "client='%s'" % output.escape(client_id) if client_id else "",
        "status='%s'" % output.escape(status) if status else "",
    ]), page=page, per_page=per_page, expand="client")
    return output.as_lines(data.get("items", []),
                           ["title", "status", "client", "value", "due_date"],
                           "job", data.get("totalItems"))


@tool
def create_job(title: str, client_id: str = "", status: str = "", description: str = "",
               start_date: str = "", due_date: str = "", value: float = 0, notes: str = "") -> str:
    """Create a job.

    Args:
        title: Job title.
        client_id: PocketBase id of the client.
        status: One of quoting, quoted, won, invoicing, invoiced,
            commissioning, lost, on_hold, cancelled, completed, reengage.
            Leave it out for a new enquiry and it starts at quoting.
        description: Job description.
        start_date: Start date, YYYY-MM-DD.
        due_date: Due date, YYYY-MM-DD.
        value: Dollar value.
        notes: Notes.
    """
    # Checked here rather than left to PocketBase, which answers a bad
    # select value with a 400 that names no field and no allowed list.
    status = job_status.check(status or job_status.DEFAULT)
    data = {"title": title, "status": status, "description": description, "notes": notes}
    data.update(only_given(client=client_id, start_date=start_date,
                           due_date=due_date, value=value))
    record = pb().create("jobs", data)
    return "Job '%s' created, id %s" % (title, record["id"])


def archive_fields(archive_reason, unarchive):
    """Turn the two archive arguments into the fields PocketBase stores.

    Examples:
        archive_fields("Duplicate of qu2", False)
            -> {"archived": True, "archived_reason": "Duplicate of qu2",
                "archived_at": today()}
        archive_fields("", True)
            -> {"archived": False, "archived_reason": "", "archived_at": ""}
        archive_fields("", False)
            -> {}

    Booleans cannot go through `only_given`, because False equals 0 in
    Python and `only_given` drops 0. So archiving is expressed as a reason
    the caller must supply, and unarchiving as its own flag. The reason is
    required so that an archived record always says why it was archived.
    """
    if archive_reason and unarchive:
        raise ValueError("Pass archive_reason or unarchive, not both.")
    if archive_reason:
        return {"archived": True, "archived_reason": archive_reason,
                "archived_at": today()}
    if unarchive:
        return {"archived": False, "archived_reason": "", "archived_at": ""}
    return {}


def moved_note(record, field, was, label):
    """A sentence for the reply when a relation now points somewhere else.

    >>> moved_note({"client": "rightco"}, "client", "wrongco", "Client")
    ' Client moved from wrongco to rightco.'

    Returns "" when nothing moved (no previous value, or the same value).
    Reassigning a job or quote to another client changes who the money
    belongs to, so the reply names both ids and the change is visible in
    the conversation.
    """
    now_id = record.get(field, "")
    if was and now_id and was != now_id:
        return " %s moved from %s to %s." % (label, was, now_id)
    return ""


@tool
def update_job(job_id: str, title: str = "", status: str = "", description: str = "",
               due_date: str = "", value: float = 0, notes: str = "",
               client_id: str = "", start_date: str = "",
               archive_reason: str = "", unarchive: bool = False) -> str:
    """Update a job. Only the fields you pass are changed.

    Args:
        job_id: PocketBase record id.
        title: New title.
        status: New status. One of quoting, quoted, won, invoicing,
            invoiced, commissioning, lost, on_hold, cancelled, completed,
            reengage.
        description: New description.
        due_date: New due date, YYYY-MM-DD.
        value: New value.
        notes: New notes.
        client_id: Reassign the job to this client. Use when a job was
            filed against the wrong client, for example against a
            supplier. The previous client id is named in the reply.
        start_date: New start date, YYYY-MM-DD.
        archive_reason: Archive the job and record why. A reason is
            required to archive.
        unarchive: Bring an archived job back.
    """
    if status:
        job_status.check(status)
    data = only_given(title=title, status=status, description=description,
                      due_date=due_date, value=value, notes=notes,
                      client=client_id, start_date=start_date)
    data.update(archive_fields(archive_reason, unarchive))
    if not data:
        return "Nothing to update."
    was = ""
    before = None
    if client_id or status:
        before = pb().get("jobs", job_id) or {}
        was = before.get("client", "")

    # Record when the status changed and who changed it. The seven day
    # chase rule (follow up a quote that has sat in `quoted` for a week)
    # reads status_changed_at. PocketBase's own `updated` field is no use
    # for this, because it moves whenever any field is edited.
    #
    # Only stamp a real change. Saving the same status again is an edit to
    # something else, and restamping would restart the chase clock.
    if status and str((before or {}).get("status") or "") != status:
        data["status_changed_at"] = datetime.now(timezone.utc).isoformat()
        data["status_changed_by"] = "mcp"

    record = pb().update("jobs", job_id, data)
    return "Job updated: %s.%s" % (record.get("title", job_id),
                                   moved_note(record, "client", was, "Client"))


# ===================================================================
# Quotes
# ===================================================================
@tool
def search_quotes(query: str = "", client_id: str = "", status: str = "",
                  page: int = 1, per_page: int = 30) -> str:
    """Search quotes by title, client or status.

    Args:
        query: Search term.
        client_id: Only quotes for this client.
        status: draft, sent, accepted, declined or expired.
        page: Page number.
        per_page: Results per page.
    """
    data = page_of("quotes", output.and_filters([
        "title~'%s'" % output.escape(query) if query else "",
        "client='%s'" % output.escape(client_id) if client_id else "",
        "status='%s'" % output.escape(status) if status else "",
    ]), page=page, per_page=per_page, expand="client,job")
    return output.as_lines(data.get("items", []),
                           ["title", "amount", "status", "client", "sent_date"],
                           "quote", data.get("totalItems"))


@tool
def create_quote(title: str, client_id: str = "", job_id: str = "", amount: float = 0,
                 status: str = "draft", sent_date: str = "", valid_until: str = "",
                 notes: str = "") -> str:
    """Create a quote.

    Args:
        title: Quote title or description.
        client_id: PocketBase id of the client.
        job_id: PocketBase id of the job.
        amount: Dollar amount.
        status: draft, sent, accepted, declined or expired.
        sent_date: Date sent, YYYY-MM-DD.
        valid_until: Expiry date, YYYY-MM-DD.
        notes: Notes.
    """
    data = {"title": title, "status": status, "notes": notes}
    data.update(only_given(client=client_id, job=job_id, amount=amount,
                           sent_date=sent_date, valid_until=valid_until))
    record = pb().create("quotes", data)
    return "Quote '%s' created, id %s" % (title, record["id"])


@tool
def update_quote(quote_id: str, title: str = "", client_id: str = "", job_id: str = "",
                 amount: float = 0, status: str = "", sent_date: str = "",
                 valid_until: str = "", notes: str = "",
                 archive_reason: str = "", unarchive: bool = False) -> str:
    """Update a quote. Only the fields you pass are changed.

    Typical uses: link an orphan quote (one with a client but no job) to
    its job, move a quote to the right client, or archive a duplicate
    while naming the copy that is kept.

    Args:
        quote_id: PocketBase record id.
        title: New title.
        client_id: Reassign the quote to this client.
        job_id: Link the quote to this job. Use this for an orphan
            quote that has a client but no job.
        amount: New dollar amount.
        status: draft, sent, accepted, declined or expired.
        sent_date: Date sent, YYYY-MM-DD.
        valid_until: Expiry date, YYYY-MM-DD.
        notes: New notes.
        archive_reason: Archive the quote and record why. Use this to
            retire a duplicate, and name the id of the copy being kept
            so the pair can still be read as a pair.
        unarchive: Bring an archived quote back.
    """
    data = only_given(title=title, amount=amount, status=status,
                      sent_date=sent_date, valid_until=valid_until, notes=notes,
                      client=client_id, job=job_id)
    data.update(archive_fields(archive_reason, unarchive))
    if not data:
        return "Nothing to update."
    before = pb().get("quotes", quote_id) if (client_id or job_id) else {}
    record = pb().update("quotes", quote_id, data)
    return "Quote updated: %s.%s%s" % (
        record.get("title", quote_id),
        moved_note(record, "client", (before or {}).get("client", ""), "Client"),
        moved_note(record, "job", (before or {}).get("job", ""), "Job"),
    )


# ===================================================================
# Suppliers
# ===================================================================
@tool
def search_suppliers(query: str = "", page: int = 1, per_page: int = 30) -> str:
    """Search suppliers by name, specialty or contact.

    Args:
        query: Search term.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("suppliers", output.and_filters([
        "name~'%s' || specialty~'%s' || contact_name~'%s'" % (q, q, q) if query else "",
    ]), page=page, per_page=per_page)
    return output.as_lines(data.get("items", []),
                           ["name", "specialty", "contact_name", "email", "phone"],
                           "supplier", data.get("totalItems"))


@tool
def create_supplier(name: str, contact_name: str = "", email: str = "",
                    phone: str = "", specialty: str = "", notes: str = "") -> str:
    """Create a supplier.

    Args:
        name: Supplier or company name.
        contact_name: Primary contact person.
        email: Email.
        phone: Phone.
        specialty: What they supply.
        notes: Notes.
    """
    record = pb().create("suppliers", {
        "name": name, "contact_name": contact_name, "email": email,
        "phone": phone, "specialty": specialty, "notes": notes,
    })
    return "Supplier '%s' created, id %s" % (name, record["id"])


@tool
def update_supplier(supplier_id: str, name: str = "", contact_name: str = "",
                    email: str = "", phone: str = "", specialty: str = "",
                    notes: str = "", append_notes: str = "") -> str:
    """Update a supplier. Only the fields you pass are changed.

    The suppliers collection has no address field, so a factory address
    goes in the notes (use append_notes to add it without losing what is
    there).

    Args:
        supplier_id: PocketBase record id.
        name: New name.
        contact_name: Main contact.
        email: New email address.
        phone: New phone number.
        specialty: What they make or supply.
        notes: Replace the notes.
        append_notes: Add to the end of the notes instead of replacing
            them. Use this when merging in history from another record,
            so the existing notes are kept.
    """
    if notes and append_notes:
        raise ValueError("Pass notes or append_notes, not both.")
    data = only_given(name=name, contact_name=contact_name, email=email,
                      phone=phone, specialty=specialty, notes=notes)
    if append_notes:
        existing = (pb().get("suppliers", supplier_id) or {}).get("notes", "")
        data["notes"] = (existing + "\n\n" + append_notes).strip()
    if not data:
        return "Nothing to update."
    record = pb().update("suppliers", supplier_id, data)
    return "Supplier updated: %s." % record.get("name", supplier_id)


# ===================================================================
# Interactions
# ===================================================================
@tool
def search_interactions(query: str = "", client_id: str = "", interaction_type: str = "",
                        days_back: int = 30, page: int = 1, per_page: int = 30) -> str:
    """Search logged interactions: emails, calls, meetings and notes.

    Args:
        query: Search term, matches subject and summary.
        client_id: Only interactions for this client.
        interaction_type: email, call, meeting or note.
        days_back: How far back to look.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("interactions", output.and_filters([
        "created>='%s'" % days_ago(days_back),
        "subject~'%s' || summary~'%s'" % (q, q) if query else "",
        "client='%s'" % output.escape(client_id) if client_id else "",
        "type='%s'" % output.escape(interaction_type) if interaction_type else "",
    ]), sort="-date", page=page, per_page=per_page, expand="client,contact,supplier")

    items = data.get("items", [])
    if not items:
        return "No interactions found."
    lines = ["Found %d interaction(s):" % data.get("totalItems", len(items))]
    for i in items:
        who = output.related_name(i, "client") or output.related_name(i, "supplier") or "unlinked"
        lines.append("- [%s] %s | %s | %s" % (i.get("type", "?"), i.get("subject", "?"),
                                              i.get("date", "?"), who))
        if i.get("summary"):
            lines.append("  %s" % i["summary"][:200])
    return "\n".join(lines)


@tool
def log_interaction(interaction_type: str, subject: str, summary: str = "", client_id: str = "",
                    contact_id: str = "", job_id: str = "", date: str = "",
                    gmail_id: str = "", from_address: str = "", supplier_id: str = "",
                    source: str = "", source_id: str = "") -> str:
    """Log an interaction. Logging the same one twice updates it rather than
    adding a second copy.

    Every interaction has a key (its source_key) naming what it mirrors:
    - with gmail_id, the key is "gmail:<message id>", so logging the same
      email again later fills in what was missing instead of duplicating it;
    - without one, the key is a digest of subject, date and client
      ("manual:<16 hex chars>"); the summary is not part of it, so a fuller
      note about the same conversation lands on the same record.

    To log two separate conversations that share a subject, a date and a
    client, pass source and source_id yourself so they stay apart.

    Args:
        interaction_type: email, call, meeting or note.
        subject: Subject line or title.
        summary: What happened.
        client_id: Client PocketBase id.
        contact_id: Contact PocketBase id.
        job_id: Job PocketBase id.
        date: Date, YYYY-MM-DD or a full datetime. Defaults to now.
        gmail_id: Gmail message id. This is what makes an email findable again.
        from_address: Sender email address.
        supplier_id: Supplier PocketBase id, for freight and parts traffic.
        source: Where this came from: gmail, todoist, xero, manual or playbook.
        source_id: The id of the thing it mirrors, if you know it.
    """
    when = date or stamp()
    src, src_id = interactions.key_for(
        gmail_id=gmail_id, subject=subject, date=when, client=client_id,
        source=source, source_id=source_id,
    )

    fields = {"type": interaction_type, "subject": subject, "summary": summary, "date": when}
    fields.update(only_given(client=client_id, contact=contact_id, job=job_id,
                             supplier=supplier_id, gmail_id=gmail_id,
                             from_address=from_address))

    before = pb().find_one("interactions",
                           "source_key='%s'" % output.escape("%s:%s" % (src, src_id)))
    record = interactions.save(pb(), src, src_id, **fields)

    what = "updated the existing record" if before else "logged"
    return "Interaction %s, id %s, key %s:%s" % (what, record["id"], src, src_id)


@tool
def update_interaction(interaction_id: str, subject: str = "", summary: str = "",
                       interaction_type: str = "", date: str = "",
                       client_id: str = "", supplier_id: str = "",
                       contact_id: str = "", job_id: str = "",
                       detach_client: bool = False) -> str:
    """Correct an interaction that is already logged, or move it to another party.

    log_interaction only merges a repeat into a record with the same key.
    It cannot fix an interaction that was filed against the wrong party,
    for example supplier emails recorded against a client. This tool can:
    pass supplier_id with detach_client=True to move it off the client and
    onto the supplier.

    Args:
        interaction_id: PocketBase record id.
        subject: New subject line.
        summary: New summary.
        interaction_type: email, call, meeting or note.
        date: New date, YYYY-MM-DD or a full datetime.
        client_id: Move the interaction to this client.
        supplier_id: Attach the interaction to this supplier. Use with
            detach_client to move supplier traffic out of the clients
            table entirely.
        contact_id: Attach to this contact.
        job_id: Attach to this job.
        detach_client: Clear the client relation. An empty client_id
            means "not supplied", so this is the only way to say "none".
    """
    if client_id and detach_client:
        raise ValueError("Pass client_id or detach_client, not both.")
    data = only_given(subject=subject, summary=summary, type=interaction_type,
                      date=date, client=client_id, supplier=supplier_id,
                      contact=contact_id, job=job_id)
    if detach_client:
        data["client"] = ""
    if not data:
        return "Nothing to update."
    before = pb().get("interactions", interaction_id) \
        if (client_id or supplier_id or detach_client) else {}
    was = (before or {}).get("client", "")
    record = pb().update("interactions", interaction_id, data)
    notes = moved_note(record, "client", was, "Client")
    if detach_client and was:
        notes = " Detached from client %s." % was
    notes += moved_note(record, "supplier", (before or {}).get("supplier", ""), "Supplier")
    if supplier_id and not (before or {}).get("supplier", ""):
        notes += " Attached to supplier %s." % supplier_id
    return "Interaction updated: %s.%s" % (
        record.get("subject") or interaction_id, notes)


# ===================================================================
# Assignments, which mirror Todoist tasks
# ===================================================================
@tool
def search_assignments(query: str = "", client_id: str = "", status: str = "",
                       overdue_only: bool = False, page: int = 1, per_page: int = 30) -> str:
    """Search task assignments synced from Todoist.

    Args:
        query: Search term, matches content and description.
        client_id: Only tasks for this client.
        status: open or completed.
        overdue_only: Only past due tasks that are not completed.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("assignments", output.and_filters([
        "content~'%s' || description~'%s'" % (q, q) if query else "",
        "client='%s'" % output.escape(client_id) if client_id else "",
        "status='%s'" % output.escape(status) if status else "",
        "due_date<'%s' && due_date!='' && status!='completed'" % today() if overdue_only else "",
    ]), page=page, per_page=per_page, expand="client,job")
    return output.as_lines(data.get("items", []),
                           ["content", "status", "priority", "due_date", "client", "project_name"],
                           "assignment", data.get("totalItems"))


@tool
def update_assignment(assignment_id: str, client: str = "", job: str = "",
                      content: str = "", description: str = "", status: str = "",
                      priority: int = 0, due_date: str = "", labels: str = "",
                      section_name: str = "") -> str:
    """Update an assignment record. Only the fields you pass are changed.

    An assignment is the PocketBase copy of a Todoist task. This tool
    changes the PocketBase record only; it does not call Todoist.

    Args:
        assignment_id: PocketBase record id.
        client: New client PocketBase id.
        job: New job PocketBase id. Pass 'clear' to unlink.
        content: New task title.
        description: New description.
        status: open or completed.
        priority: Priority as Todoist stores it. Leave at 0 for no change.
        due_date: New due date, YYYY-MM-DD.
        labels: Comma separated labels.
        section_name: New section name.
    """
    data = only_given(client=client, content=content, description=description,
                      status=status, priority=priority, due_date=due_date,
                      labels=labels, section_name=section_name)
    if job == "clear":
        data["job"] = ""
    elif job:
        data["job"] = job

    if not data:
        return "Nothing to update. Pass at least one field."
    pb().update("assignments", assignment_id, data)
    # update() reads the record back and raises if any field did not save,
    # so reaching this line means every field in `data` is stored.
    return "Assignment %s updated and verified: %s" % (assignment_id, json.dumps(data))


# ===================================================================
# Knowledge base
# ===================================================================
@tool
def search_knowledge(query: str = "", category: str = "", client_id: str = "",
                     page: int = 1, per_page: int = 30) -> str:
    """Search the knowledge base.

    Args:
        query: Search term, matches title, content and tags.
        category: Filter by category.
        client_id: Only entries for this client.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("knowledge_base", output.and_filters([
        "title~'%s' || content~'%s' || tags~'%s'" % (q, q, q) if query else "",
        "category='%s'" % output.escape(category) if category else "",
        "client='%s'" % output.escape(client_id) if client_id else "",
    ]), page=page, per_page=per_page)

    items = data.get("items", [])
    if not items:
        return "No knowledge base entries found."
    lines = ["Found %d entry/entries:" % data.get("totalItems", len(items))]
    for item in items:
        lines.append("- [%s] %s, category %s, tags %s" % (
            item.get("id", "?"), item.get("title", "?"),
            item.get("category") or "none", item.get("tags") or "none"))
        if item.get("content"):
            lines.append("  %s" % item["content"][:300])
    return "\n".join(lines)


@tool
def add_knowledge(title: str, content: str, category: str = "",
                  client_id: str = "", tags: str = "") -> str:
    """Add an entry to the knowledge base.

    Args:
        title: Entry title.
        content: The full note.
        category: Category, for example technical, process or client-info.
        client_id: Related client PocketBase id.
        tags: Comma separated tags.
    """
    data = {"title": title, "content": content, "category": category, "tags": tags}
    if client_id:
        data["client"] = client_id
    record = pb().create("knowledge_base", data)
    return "Knowledge entry '%s' created, id %s" % (title, record["id"])


# ===================================================================
# Summary and logs
# ===================================================================
@tool
def get_daily_summary() -> str:
    """Morning briefing: overdue tasks, due today, live jobs, pending quotes,
    and what was logged in the last day."""
    day = today()
    out = ["# Daily summary, %s\n" % day]

    overdue = page_of("assignments",
                      "due_date<'%s' && due_date!='' && status!='completed'" % day,
                      sort="due_date", per_page=50, expand="client").get("items", [])
    out.append("## Overdue (%d)" % len(overdue))
    for t in overdue:
        out.append("- %s, due %s, %s" % (t.get("content", "?"), t.get("due_date", "?"),
                                         output.related_name(t, "client") or "no client"))

    due = page_of("assignments", "due_date='%s' && status!='completed'" % day,
                  sort="priority", per_page=50, expand="client").get("items", [])
    out.append("\n## Due today (%d)" % len(due))
    for t in due:
        out.append("- %s, priority %s, %s" % (t.get("content", "?"), t.get("priority", "?"),
                                              output.related_name(t, "client") or "no client"))

    # Live jobs are those still moving through the pipeline: quoting and
    # quoted, plus won through commissioning (job_status.PIPELINE). The
    # list comes from core/job_status.py so this section cannot drift from
    # the statuses a job can actually hold.
    live ="(%s)" % " || ".join("status='%s'" % s for s in job_status.PIPELINE)
    jobs = page_of("jobs", live, per_page=50, expand="client").get("items", [])
    out.append("\n## Live jobs (%d)" % len(jobs))
    for j in jobs:
        out.append("- %s, %s, due %s, value %s" % (
            j.get("title", "?"), output.related_name(j, "client") or "no client",
            j.get("due_date") or "none", j.get("value", "?")))

    quotes = page_of("quotes", "(status='sent' || status='draft')",
                     per_page=50, expand="client").get("items", [])
    out.append("\n## Pending quotes (%d)" % len(quotes))
    for q in quotes:
        out.append("- [%s] %s, %s, %s" % (
            q.get("status", "?"), q.get("title", "?"),
            output.related_name(q, "client") or "no client", q.get("amount", "?")))

    recent = page_of("interactions", "created>='%s'" % days_ago(1),
                     sort="-date", per_page=20, expand="client").get("items", [])
    out.append("\n## Logged in the last 24 hours (%d)" % len(recent))
    for i in recent:
        out.append("- [%s] %s, %s" % (i.get("type", "?"), i.get("subject", "?"),
                                      output.related_name(i, "client") or "no client"))

    return "\n".join(out)


@tool
def get_sync_log(event_type: str = "", days_back: int = 7, per_page: int = 30) -> str:
    """Read the sync daemon activity log.

    Args:
        event_type: Filter by event type.
        days_back: How far back to look.
        per_page: Number of entries.
    """
    data = page_of("sync_log", output.and_filters([
        "created>='%s'" % days_ago(days_back),
        "event_type='%s'" % output.escape(event_type) if event_type else "",
    ]), per_page=per_page)
    return output.as_lines(data.get("items", []), ["event_type", "details", "source"],
                           "log entry", data.get("totalItems"))


# ===================================================================
# Task groups
# ===================================================================
def parse_task_ids(raw):
    """Read the JSON array of Todoist ids, or say why it could not be read."""
    try:
        ids = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError('todoist_task_ids must be valid JSON, for example ["123", "456"]')
    if not isinstance(ids, list):
        raise ValueError("todoist_task_ids must be a JSON array of strings")
    return ids


@tool
def create_task_group(name: str, todoist_task_ids: str, client_id: str = "",
                      description: str = "") -> str:
    """Create a task group that clusters related Todoist tasks.

    Groups exist only in PocketBase; Todoist has no equivalent. A group
    can later be promoted to a full project with a job and a project
    folder (see update_task_group).

    Args:
        name: Short scope name, for example "Orchard Lane Grippers Supply".
        todoist_task_ids: JSON array of Todoist task id strings.
        client_id: PocketBase client id.
        description: What ties these tasks together.
    """
    ids = parse_task_ids(todoist_task_ids)
    data = {"name": name, "todoist_task_ids": ids, "status": "open", "description": description}
    if client_id:
        data["client"] = client_id
    record = pb().create("task_groups", data)
    return "Task group '%s' created, id %s, %d task(s)" % (name, record["id"], len(ids))


@tool
def update_task_group(group_id: str, name: str = "", description: str = "",
                      todoist_task_ids: str = "", status: str = "",
                      client_id: str = "", job: str = "", project_path: str = "") -> str:
    """Update a task group. Only the fields you pass are changed.

    Args:
        group_id: PocketBase record id.
        name: New name.
        description: New description.
        todoist_task_ids: The complete updated JSON array. This replaces the list.
        status: open, promoted or archived.
        client_id: PocketBase client id.
        job: PocketBase job id, set when promoting to a project.
        project_path: Path to the project folder, set when promoting.
    """
    data = only_given(name=name, description=description, status=status,
                      client=client_id, job=job, project_path=project_path)
    if todoist_task_ids:
        data["todoist_task_ids"] = parse_task_ids(todoist_task_ids)
    if not data:
        return "Nothing to update."
    record = pb().update("task_groups", group_id, data)
    return "Task group updated: %s, status %s" % (record.get("name", group_id),
                                                  record.get("status", "?"))


@tool
def search_task_groups(query: str = "", client_id: str = "", status: str = "",
                       page: int = 1, per_page: int = 30) -> str:
    """Search task groups by name, client or status.

    Args:
        query: Search term, matches name and description.
        client_id: Only groups for this client.
        status: open, promoted or archived.
        page: Page number.
        per_page: Results per page.
    """
    q = output.escape(query)
    data = page_of("task_groups", output.and_filters([
        "name~'%s' || description~'%s'" % (q, q) if query else "",
        "client='%s'" % output.escape(client_id) if client_id else "",
        "status='%s'" % output.escape(status) if status else "",
    ]), page=page, per_page=per_page, expand="client,job")

    items = data.get("items", [])
    if not items:
        return "No task groups found."
    lines = ["Found %d task group(s):" % data.get("totalItems", len(items))]
    for g in items:
        ids = g.get("todoist_task_ids") or []
        block = ["- %s [%s], %d task(s)" % (g.get("name", "?"), g.get("status", "?"), len(ids)),
                 "  id: %s" % g.get("id", "?")]
        if output.related_name(g, "client"):
            block.append("  client: %s" % output.related_name(g, "client"))
        if g.get("description"):
            block.append("  description: %s" % g["description"][:120])
        if ids:
            more = " and %d more" % (len(ids) - 8) if len(ids) > 8 else ""
            block.append("  todoist ids: %s%s" % (", ".join(str(t) for t in ids[:8]), more))
        if output.related_name(g, "job"):
            block.append("  promoted to job: %s" % output.related_name(g, "job"))
        if g.get("project_path"):
            block.append("  project path: %s" % g["project_path"])
        lines.append("\n".join(block))
    return "\n".join(lines)


@tool
def get_task_group(group_id: str) -> str:
    """Get one task group in full.

    Args:
        group_id: PocketBase record id.
    """
    return output.as_json(pb().get("task_groups", group_id))


# ===================================================================
# Raw read access to any collection
# ===================================================================
@tool
def query_collection(collection: str, filter_expr: str = "", sort: str = "-created",
                     expand: str = "", page: int = 1, per_page: int = 30) -> str:
    """Read any collection directly, for the questions the tools above do not answer.

    Args:
        collection: Collection name.
        filter_expr: A PocketBase filter, for example "name~'smith' && status='active'".
        sort: Sort expression, for example "-created" or "name".
        expand: Relations to expand, for example "client,job".
        page: Page number.
        per_page: Results per page.
    """
    data = page_of(collection, filter_str=filter_expr, sort=sort,
                   page=page, per_page=per_page, expand=expand)
    items = data.get("items", [])
    if not items:
        return "No records in '%s' matching: %s" % (collection, filter_expr or "everything")
    lines = ["'%s': %d total, showing %d" % (collection, data.get("totalItems", len(items)),
                                             len(items))]
    lines.extend(output.as_json(item) for item in items)
    return "\n".join(lines)


def main():
    """Serve the tools over stdio, which is how MCP clients launch this server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
