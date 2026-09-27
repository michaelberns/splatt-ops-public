"""
Formatting helpers for the MCP server: filter building and record-to-text.

The tools in server.py decide what to fetch; this module decides how the
result reads in a chat window, and how search terms are quoted inside
PocketBase filter strings. Nothing here talks to the network, so it is
easy to test on its own.
"""

from __future__ import annotations

import json


def escape(value):
    """Make a value safe to put inside a single-quoted PocketBase filter string.

    >>> print(escape("O'Brien"))
    O\\'Brien

    Without escaping, the apostrophe would end the quoted string early and
    change the meaning of the filter. Every tool quotes user input through
    this one function.
    """
    return str(value or "").replace("'", "\\'")


def and_filters(parts):
    """Join filter fragments with `&&`, skipping empty ones.

    >>> and_filters(["a='1'", "b~'x' || c~'x'", "", "d='2'"])
    "(a='1') && (b~'x' || c~'x') && (d='2')"

    Each fragment is wrapped in brackets so an `||` inside one fragment
    stays inside it. Unbracketed, `a='1' && b~'x' || c~'x'` reads as
    `(a='1' && b~'x') || c~'x'` and matches every record where c contains
    x, whatever a is.
    """
    kept = ["(%s)" % p for p in parts if p]
    return " && ".join(kept)


def related_name(record, field):
    """The display name of an expanded relation, or "" if it was not expanded.

    Collections label their records differently (clients have `name`,
    jobs have `title`), so `name` is tried first and then `title`.
    """
    expanded = (record.get("expand") or {}).get(field)
    if not isinstance(expanded, dict):
        return ""
    return expanded.get("name") or expanded.get("title") or ""


def value_of(record, field):
    """A field's value; for an empty relation field, the expanded record's name instead."""
    value = record.get(field, "")
    if value in ("", None, []):
        value = related_name(record, field)
    return value


def as_lines(items, fields, label="record", total=None):
    """Format records as a compact list, one record per line.

    Example output:
        Found 42 client(s), showing 2:
        - [abc123] name: Clearwater Bottling | status: active
        - [def456] name: Hill Top Foods | status: prospect

    Empty fields are left out rather than printed as "phone: " with
    nothing after it, which keeps each line short.
    """
    if not items:
        return "No %ss found." % label
    shown = len(items)
    count = total if total is not None else shown
    header = "Found %d %s(s)" % (count, label)
    if total is not None and total > shown:
        header += ", showing %d" % shown
    lines = [header + ":"]
    for item in items:
        parts = []
        for field in fields:
            value = value_of(item, field)
            if value not in ("", None, []):
                parts.append("%s: %s" % (field, value))
        lines.append("- [%s] %s" % (item.get("id", "?"), " | ".join(parts)))
    return "\n".join(lines)


def as_json(record):
    """One record as indented JSON, without PocketBase's collectionId and collectionName."""
    noise = ("collectionId", "collectionName")
    clean = {k: v for k, v in record.items() if k not in noise}
    return json.dumps(clean, indent=2, default=str)
