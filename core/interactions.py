"""
Interactions: the one write path for the `interactions` collection.

What an interaction is
    A dated entry in a client's (or supplier's) history: an email, a
    Todoist task, an invoice event, a note typed by the operator, or an
    automatic change made by the trigger engine. Several sources report
    the same things over and over (the sync sees the same task on every
    pass), so the main risk is writing the same interaction many times.

How duplicates are prevented (three layers)
    1. Every record carries a `source_key` of the form `<source>:<id>`,
       for example `todoist:6Xtask0000000001` or `gmail:18f0000000000001`.
       It names the thing the interaction mirrors, so the same thing can
       be found again on the next pass.
    2. `save()` is the only function that writes interactions. It looks
       the key up first and updates the existing record if there is one,
       so running a sync ten times leaves one record.
    3. The database has a unique index on `source_key` (created by the
       initial schema migration, and by tools/migrate.py on an existing
       database). That still holds if some code bypasses `save()`, and it
       makes two concurrent `save()` calls end with one record (see the
       race handling in `save()`).

Suppliers
    An interaction can point at a client, a supplier, or both. A freight
    email, for example, is about the courier (supplier) and the customer
    (client) at once.

Every write is read back by core/pb.py and, when a ledger is passed,
recorded in the write ledger (core/ledger.py) for the validator.
"""

from __future__ import annotations

import hashlib

KNOWN_SOURCES = {
    "todoist",   # mirrors a Todoist task
    "gmail",     # mirrors an email
    "xero",      # mirrors an invoice or bill event
    "manual",    # typed by the operator or the agent
    "playbook",  # written by a playbook run
    # Written by daemon/triggers.py when completing a task moved a job's
    # status, or a chase task was created or closed. Every automatic change
    # to a job appears in that client's history as a plain sentence, so
    # nobody has to read a log to learn why a status moved. The id is the
    # rule plus the record ids, so a second pass over the same board
    # recognises its own entries and writes nothing new.
    "trigger",
}

# Fields save() is allowed to set. Anything else is a caller mistake and
# is refused by name rather than silently dropped.
WRITABLE = {
    "type", "client", "supplier", "contact", "job",
    "subject", "summary", "date", "gmail_id", "from_address",
}


class InteractionError(ValueError):
    """The interaction being written is not safe to write."""


def source_key(source, source_id):
    """Build the stable key `<source>:<id>` for an interaction.

    >>> source_key("Gmail", "18f0000000000001")   # 'gmail:18f0000000000001'
    >>> source_key("todoist", "")                 # raises InteractionError

    Both halves are required and the source must be in KNOWN_SOURCES. A
    key such as "todoist:" identifies nothing and would collide with every
    other record that lacks an id, so it is refused.
    """
    source = str(source or "").strip().lower()
    source_id = str(source_id or "").strip()
    if not source:
        raise InteractionError(
            "an interaction needs a source. One of: %s" % ", ".join(sorted(KNOWN_SOURCES))
        )
    if source not in KNOWN_SOURCES:
        raise InteractionError(
            "'%s' is not a source this system knows. One of: %s. "
            "If it is genuinely new, add it to KNOWN_SOURCES on purpose."
            % (source, ", ".join(sorted(KNOWN_SOURCES)))
        )
    if not source_id:
        raise InteractionError(
            "an interaction from %s needs the id of the thing it mirrors. "
            "Without it the record cannot be found again, and the next "
            "pass would write a duplicate." % source
        )
    return "%s:%s" % (source, source_id)


def key_for(gmail_id="", subject="", date="", client="", source="", source_id=""):
    """Choose the (source, source_id) pair an interaction should be keyed on.

    Kept here, not in each caller, so the MCP server and any future
    caller key the same interaction the same way; if two callers
    disagreed, one conversation would be stored twice.

    Rules, in order:
      1. An explicit source and/or source_id always wins; the caller knows
         more than a rule does.
      2. An email is keyed on its Gmail id, the real identity of the
         message. Logging the same email again on a later day then updates
         the record instead of adding a second one.
      3. Anything else is keyed on a short SHA-1 digest of subject, date
         (day only) and client. The summary is deliberately left out, so
         a fuller rewrite of the same conversation updates the same
         record.

    Two genuinely separate calls to the same client, on the same subject,
    on the same day would share a key under rule 3. That is rare, and the
    caller can pass an explicit source_id to separate them.
    """
    if source or source_id:
        return source or "manual", source_id

    if gmail_id:
        return "gmail", str(gmail_id).strip()

    seed = "|".join([str(subject or "").strip().lower(),
                     str(date or "")[:10],
                     str(client or "")])
    return "manual", hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _is_duplicate_key(exc):
    """True if `exc` is PocketBase's unique-index rejection of `source_key`.

    Deliberately narrow: only a 400 whose error data names `source_key`
    with a "unique" message counts. Treating other failures as "already
    exists, update instead" would hide real errors.
    """
    if getattr(exc, "status", None) != 400:
        return False
    body = getattr(exc, "body", None)
    field = (body or {}).get("data", {}).get("source_key") if isinstance(body, dict) else None
    if not field:
        return False
    message = str(field.get("message", field) if isinstance(field, dict) else field)
    return "unique" in message.lower()


def _clean(fields):
    """Reject unknown field names and drop None values."""
    unknown = sorted(set(fields or {}) - WRITABLE)
    if unknown:
        raise InteractionError(
            "these are not fields on an interaction: %s. Fields are: %s"
            % (", ".join(unknown), ", ".join(sorted(WRITABLE)))
        )
    # None means "value not known", not "clear it". Sending it would erase
    # a value set elsewhere, such as a client linked by hand.
    return {k: v for k, v in (fields or {}).items() if v is not None}


def save(pb, source, source_id, ledger=None, **fields):
    """Create or update the interaction for (source, source_id). Idempotent.

    Steps:
      1. Build the source key and validate the fields. An interaction with
         neither a summary nor a subject is refused.
      2. Look the key up. If a record exists, update it.
      3. Otherwise create it. If the create is refused by the unique index
         (another run created the same key a moment earlier), find that
         record and update it instead.
      4. Record the write in the ledger, if one was passed.

    Returns the stored record. Raises `WriteNotLanded` (from core/pb.py) if
    PocketBase did not store what was sent.
    """
    key = source_key(source, source_id)
    payload = _clean(fields)
    if not payload.get("summary") and not payload.get("subject"):
        raise InteractionError(
            "an interaction with no summary and no subject says nothing. "
            "Give it one before writing it."
        )

    filter_str = "source_key='%s'" % key.replace("'", "\\'")
    existing = pb.find_one("interactions", filter_str)
    if existing:
        record = pb.update("interactions", existing["id"], payload)
        action = "update"
    else:
        body = dict(payload)
        body["source_key"] = key
        try:
            record = pb.create("interactions", body)
            action = "create"
        except Exception as exc:
            # Two runs can both look, both find nothing and both create.
            # The unique index correctly refuses the second create, and the
            # record this call wanted now exists, so update it. Any other
            # error, or a refusal with no findable record, is re-raised.
            if not _is_duplicate_key(exc):
                raise
            landed = pb.find_one("interactions", filter_str)
            if not landed:
                raise
            record = pb.update("interactions", landed["id"], payload)
            action = "update"

    if ledger:
        ledger.record(action, "interactions", record.get("id", ""),
                      sorted(payload), "interactions.save", key)
    return record
