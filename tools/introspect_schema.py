"""
Regenerate core/schema.py from the live PocketBase schema.

Usage
    python -m tools.introspect_schema            print the new file to stdout
    python -m tools.introspect_schema --write    overwrite core/schema.py

What it does
    Signs in as the PocketBase superuser (from .env), reads every
    collection definition, and writes a Python module holding, for each
    collection, its fields with their type, whether they are required, the
    allowed values of select fields and the target of relation fields.
    System collections (names starting with "_"), the built-in fields (id,
    created, updated, ...) and the collections in NOT_OPS are left out.

Why the file exists
    Code that writes to PocketBase checks its payload against
    core/schema.py first (`validate_payload`), because PocketBase silently
    drops fields it does not know. tests/test_schema.py and the validator's schema_drift rule
    compare the file with the live database, so a schema change made
    without regenerating it is caught.

After changing the schema (for example with tools/migrate.py), run this
with --write, review the diff and commit it. The diff is the history of
the database schema.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from core.config import settings
from core.pb import PocketBaseClient

# Collections that are not part of this system: the built-in users auth
# collection, and collections owned by other apps that may share the same
# PocketBase. The database created from pb_migrations/ has no courses or
# personal_projects collections; they stay listed so a shared database can
# hold them without the drift check reporting them. Anything listed here is
# also left out of core/schema.py, and therefore out of tools/backup.py.
NOT_OPS = {
    "users",
    "courses",
    "personal_projects",
}

SYSTEM_FIELDS = {"id", "created", "updated", "collectionId", "collectionName"}

HEADER = '''"""
The PocketBase schema, as Python data: every collection this system uses,
with each field's type, whether it is required, the allowed values of
select fields and the target collection of relation fields.

GENERATED on %s by tools/introspect_schema.py from the live
database. Do not edit it by hand. To regenerate it after a schema change:

    python -m tools.introspect_schema --write

then review the diff and commit it.

How it is used
    Code that writes to PocketBase calls validate_payload() first, because
    PocketBase silently drops fields it does not know and only rejects some
    bad values. tools/backup.py backs up every collection listed here.

How it is checked
    tests/test_schema.py and the validator's schema_drift rule compare this
    file with the live database and fail when they differ, so a schema
    change made without regenerating this file is caught.

NOT_OPS lists collections left out on purpose: the built-in users
collection and collections owned by other apps that share the database.
"""

NOT_OPS = %s

'''


def field_spec(field):
    """The parts of one PocketBase field definition that schema.py keeps."""
    spec = {"type": field.get("type", "text"), "required": bool(field.get("required"))}
    values = field.get("values")
    if values:
        spec["values"] = list(values)
    target = field.get("collectionId")
    if target:
        spec["collection_id"] = target
    return spec


def render(collections):
    """The full text of core/schema.py for `collections` ({name: {field: spec}})."""
    lines = [HEADER % (date.today().isoformat(), repr(sorted(NOT_OPS)))]
    lines.append("SCHEMA = {")
    for name in sorted(collections):
        lines.append('    "%s": {' % name)
        lines.append('        "fields": {')
        for field, spec in collections[name].items():
            parts = ['"type": "%s"' % spec["type"], '"required": %s' % spec["required"]]
            if "values" in spec:
                parts.append('"values": %r' % (spec["values"],))
            if "collection_id" in spec:
                parts.append('"collection_id": "%s"' % spec["collection_id"])
            lines.append('            "%s": {%s},' % (field, ", ".join(parts)))
        lines.append("        },")
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append(BODY)
    return "\n".join(lines)


BODY = '''
def collections():
    return sorted(SCHEMA)


def fields(collection):
    return set(SCHEMA[collection]["fields"])


def required_fields(collection):
    return {n for n, f in SCHEMA[collection]["fields"].items() if f["required"]}


def allowed_values(collection, field):
    return SCHEMA[collection]["fields"][field].get("values")


def relation_fields(collection):
    return {
        n for n, f in SCHEMA[collection]["fields"].items() if f["type"] == "relation"
    }


def validate_payload(collection, payload):
    """Check a dict before writing it. Returns a list of problems, empty if fine.

    Reports fields the collection does not have, required fields that are
    missing or empty, and select values that are not allowed. PocketBase
    would drop the first kind silently and reject the others with a 400.
    """
    problems = []
    if collection not in SCHEMA:
        return ["unknown collection: %s" % collection]
    known = fields(collection)
    for key in payload:
        if key not in known:
            problems.append("field does not exist in %s: %s" % (collection, key))
    for key in required_fields(collection):
        if not payload.get(key):
            problems.append("required field missing or empty in %s: %s" % (collection, key))
    for key, value in payload.items():
        if key in known and value not in (None, ""):
            values = allowed_values(collection, key)
            if values and value not in values:
                problems.append(
                    "invalid value for %s.%s: %r (allowed: %s)" % (collection, key, value, values)
                )
    return problems
'''


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    conf = settings()
    pb = PocketBaseClient(**conf.pocketbase())
    if not pb.health():
        print("PocketBase not reachable", file=sys.stderr)
        return 2
    pb.auth_admin()

    collections = {}
    for coll in pb.collections():
        name = coll.get("name", "")
        if not name or name.startswith("_") or name in NOT_OPS:
            continue
        if coll.get("type") == "view":
            # View collections are read only, so validate_payload is never
            # used on them. They are still included so the drift check
            # covers them.
            pass
        spec = {}
        for field in coll.get("fields") or coll.get("schema") or []:
            fname = field.get("name")
            if not fname or fname in SYSTEM_FIELDS:
                continue
            spec[fname] = field_spec(field)
        collections[name] = spec
    pb.close()

    text = render(collections)
    if "--write" in argv:
        path = Path(__file__).resolve().parent.parent / "core" / "schema.py"
        path.write_text(text, encoding="utf-8")
        print("wrote %s with %d collections" % (path, len(collections)))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
