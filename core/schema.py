"""
The PocketBase schema, as Python data: every collection this system uses,
with each field's type, whether it is required, the allowed values of
select fields and the target collection of relation fields.

GENERATED on 2026-09-27 by tools/introspect_schema.py from the live
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

NOT_OPS = ['courses', 'personal_projects', 'users']


SCHEMA = {
    "assignments": {
        "fields": {
            "todoist_id": {"type": "text", "required": False},
            "content": {"type": "text", "required": False},
            "description": {"type": "text", "required": False},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "job": {"type": "relation", "required": False, "collection_id": "pbc_2409499253"},
            "status": {"type": "select", "required": False, "values": ['open', 'completed']},
            "priority": {"type": "number", "required": False},
            "due_date": {"type": "date", "required": False},
            "source": {"type": "select", "required": False, "values": ['manual', 'sync', 'ai_suggested', 'dashboard']},
            "project_name": {"type": "text", "required": False},
            "section_name": {"type": "text", "required": False},
            "labels": {"type": "text", "required": False},
            "archived": {"type": "bool", "required": False},
            "archived_reason": {"type": "text", "required": False},
            "archived_at": {"type": "date", "required": False},
            "supplier": {"type": "relation", "required": False, "collection_id": "pbc_3355664324"},
            "real_due": {"type": "date", "required": False},
            "deadline": {"type": "date", "required": False},
            "on_complete_job_status": {"type": "select", "required": False, "values": ['quoted', 'won', 'invoicing', 'invoiced']},
            "auto_kind": {"type": "select", "required": False, "values": ['quoted_chase']},
        },
    },
    "bills": {
        "fields": {
            "job": {"type": "relation", "required": False, "collection_id": "pbc_2409499253"},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "description": {"type": "text", "required": True},
            "type": {"type": "select", "required": False, "values": ['invoice', 'shipping', 'parts', 'deposit', 'other']},
            "amount": {"type": "number", "required": False},
            "paid": {"type": "bool", "required": False},
            "amount_paid": {"type": "number", "required": False},
            "due_date": {"type": "date", "required": False},
            "paid_date": {"type": "date", "required": False},
            "notes": {"type": "text", "required": False},
        },
    },
    "clients": {
        "fields": {
            "name": {"type": "text", "required": True},
            "aliases": {"type": "text", "required": False},
            "email_addresses": {"type": "text", "required": False},
            "phone": {"type": "text", "required": False},
            "address": {"type": "text", "required": False},
            "status": {"type": "select", "required": False, "values": ['active', 'inactive', 'prospect']},
            "notes": {"type": "text", "required": False},
            "critical": {"type": "bool", "required": False},
            "critical_note": {"type": "text", "required": False},
        },
    },
    "contacts": {
        "fields": {
            "name": {"type": "text", "required": True},
            "email": {"type": "email", "required": False},
            "phone": {"type": "text", "required": False},
            "role": {"type": "text", "required": False},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "notes": {"type": "text", "required": False},
        },
    },
    "equipment": {
        "fields": {
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "site_name": {"type": "text", "required": True},
            "category": {"type": "select", "required": True, "values": ['capper', 'filler', 'rinser', 'conveyor', 'blower', 'dryer', 'ozone', 'pasteuriser', 'seamer', 'cip', 'doser', 'packer', 'labeller', 'other']},
            "description": {"type": "text", "required": True},
            "make": {"type": "text", "required": False},
            "model": {"type": "text", "required": False},
            "serial": {"type": "text", "required": False},
            "installed_year": {"type": "text", "required": False},
            "status": {"type": "select", "required": True, "values": ['active', 'prospect', 'history', 'decommissioned']},
            "location": {"type": "text", "required": False},
            "lat": {"type": "number", "required": False},
            "lng": {"type": "number", "required": False},
            "coords_precise": {"type": "bool", "required": False},
            "source": {"type": "text", "required": False},
            "note": {"type": "text", "required": False},
            "archived": {"type": "bool", "required": False},
            "confidence": {"type": "select", "required": False, "values": ['confirmed', 'probable', 'inferred', 'claimed']},
            "last_verified": {"type": "date", "required": False},
            "evidence_ref": {"type": "text", "required": False},
            "owner": {"type": "text", "required": False},
            "decommissioned_at": {"type": "date", "required": False},
        },
    },
    "interactions": {
        "fields": {
            "type": {"type": "select", "required": False, "values": ['email', 'call', 'meeting', 'note']},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "contact": {"type": "relation", "required": False, "collection_id": "pbc_1930317162"},
            "job": {"type": "relation", "required": False, "collection_id": "pbc_2409499253"},
            "subject": {"type": "text", "required": False},
            "summary": {"type": "text", "required": False},
            "date": {"type": "date", "required": False},
            "gmail_id": {"type": "text", "required": False},
            "from_address": {"type": "text", "required": False},
            "supplier": {"type": "relation", "required": False, "collection_id": "pbc_3355664324"},
            "source_key": {"type": "text", "required": False},
        },
    },
    "invoices": {
        "fields": {
            "invoice_number": {"type": "text", "required": False},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "job": {"type": "relation", "required": False, "collection_id": "pbc_2409499253"},
            "description": {"type": "text", "required": False},
            "amount": {"type": "number", "required": False},
            "status": {"type": "select", "required": False, "values": ['draft', 'sent', 'paid', 'overdue', 'cancelled']},
            "paid": {"type": "bool", "required": False},
            "amount_paid": {"type": "number", "required": False},
            "issue_date": {"type": "date", "required": False},
            "due_date": {"type": "date", "required": False},
            "paid_date": {"type": "date", "required": False},
            "notes": {"type": "text", "required": False},
        },
    },
    "jobs": {
        "fields": {
            "title": {"type": "text", "required": True},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "status": {"type": "select", "required": False, "values": ['quoting', 'quoted', 'won', 'invoicing', 'invoiced', 'commissioning', 'lost', 'on_hold', 'cancelled', 'completed', 'reengage']},
            "description": {"type": "text", "required": False},
            "start_date": {"type": "date", "required": False},
            "due_date": {"type": "date", "required": False},
            "value": {"type": "number", "required": False},
            "notes": {"type": "text", "required": False},
            "archived": {"type": "bool", "required": False},
            "archived_reason": {"type": "text", "required": False},
            "archived_at": {"type": "date", "required": False},
            "paid": {"type": "bool", "required": False},
            "paid_amount": {"type": "number", "required": False},
            "paid_at": {"type": "date", "required": False},
            "shipping_paid": {"type": "bool", "required": False},
            "has_shipping": {"type": "bool", "required": False},
            "supplier_paid": {"type": "bool", "required": False},
            "supplier_paid_amount": {"type": "number", "required": False},
            "supplier_paid_at": {"type": "date", "required": False},
            "site_address": {"type": "text", "required": False},
            "status_changed_at": {"type": "date", "required": False},
            "status_changed_by": {"type": "text", "required": False},
        },
    },
    "knowledge_base": {
        "fields": {
            "title": {"type": "text", "required": True},
            "content": {"type": "text", "required": False},
            "category": {"type": "text", "required": False},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "tags": {"type": "text", "required": False},
        },
    },
    "playbook_runs": {
        "fields": {
            "playbook_id": {"type": "text", "required": True},
            "status": {"type": "select", "required": True, "values": ['open', 'completed', 'failed_validation', 'abandoned']},
            "trigger": {"type": "text", "required": False},
            "inputs": {"type": "json", "required": False},
            "context": {"type": "json", "required": False},
            "steps": {"type": "json", "required": False},
            "current_step": {"type": "text", "required": False},
            "started_at": {"type": "date", "required": True},
            "completed_at": {"type": "date", "required": False},
            "validation_summary": {"type": "json", "required": False},
        },
    },
    "project_financials": {
        "fields": {
            "project_key": {"type": "text", "required": True},
            "job": {"type": "text", "required": False},
            "client": {"type": "text", "required": False},
            "direction": {"type": "select", "required": True, "values": ['in', 'out']},
            "kind": {"type": "select", "required": True, "values": ['client_invoice', 'supplier_bill', 'freight', 'duty', 'import_gst', 'custom_cost', 'ex_stock']},
            "billed_via": {"type": "select", "required": True, "values": ['splatt', 'partner']},
            "ref": {"type": "text", "required": False},
            "party": {"type": "text", "required": False},
            "description": {"type": "text", "required": False},
            "currency": {"type": "select", "required": True, "values": ['NZD', 'USD', 'EUR', 'GBP', 'AUD', 'ZAR']},
            "amount_gross": {"type": "number", "required": True},
            "gst_treatment": {"type": "select", "required": True, "values": ['incl_15', 'add_15', 'no_gst', 'zero', 'is_import_gst']},
            "amount_net": {"type": "number", "required": False},
            "fx_rate": {"type": "number", "required": False},
            "amount_net_nzd": {"type": "number", "required": False},
            "source_file": {"type": "text", "required": False},
            "parse_status": {"type": "select", "required": False, "values": ['parsed', 'confirmed', 'manual']},
            "in_hubdoc": {"type": "bool", "required": False},
            "in_xero": {"type": "bool", "required": False},
            "status": {"type": "select", "required": False, "values": ['not_raised', 'draft', 'awaiting_approval', 'awaiting_payment', 'paid']},
            "date_paid": {"type": "date", "required": False},
            "doc_date": {"type": "date", "required": False},
            "notes": {"type": "text", "required": False},
        },
    },
    "project_financials_summary": {
        "fields": {
            "project_key": {"type": "text", "required": True},
            "client": {"type": "text", "required": False},
            "revenue_nzd": {"type": "number", "required": False},
            "cost_nzd": {"type": "number", "required": False},
            "margin_nzd": {"type": "number", "required": False},
            "margin_pct": {"type": "number", "required": False},
            "partner_nzd": {"type": "number", "required": False},
            "line_count": {"type": "number", "required": False},
            "unconfirmed_count": {"type": "number", "required": False},
            "last_computed": {"type": "date", "required": False},
        },
    },
    "publish_log": {
        "fields": {
            "started": {"type": "date", "required": True},
            "finished": {"type": "date", "required": False},
            "status": {"type": "select", "required": True, "values": ['running', 'success', 'failed']},
            "trigger": {"type": "select", "required": False, "values": ['dashboard', 'cron', 'terminal', 'ops']},
            "snapshot_at": {"type": "text", "required": False},
            "records": {"type": "number", "required": False},
            "collections": {"type": "number", "required": False},
            "size_kb": {"type": "number", "required": False},
            "duration_s": {"type": "number", "required": False},
            "deploy_url": {"type": "text", "required": False},
            "error": {"type": "text", "required": False},
            "output": {"type": "text", "required": False},
            "host": {"type": "text", "required": False},
        },
    },
    "quotes": {
        "fields": {
            "title": {"type": "text", "required": True},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "job": {"type": "relation", "required": False, "collection_id": "pbc_2409499253"},
            "amount": {"type": "number", "required": False},
            "status": {"type": "select", "required": False, "values": ['draft', 'sent', 'accepted', 'declined', 'expired']},
            "sent_date": {"type": "date", "required": False},
            "valid_until": {"type": "date", "required": False},
            "notes": {"type": "text", "required": False},
            "archived": {"type": "bool", "required": False},
            "archived_reason": {"type": "text", "required": False},
            "archived_at": {"type": "date", "required": False},
        },
    },
    "skill_invocations": {
        "fields": {
            "skill": {"type": "relation", "required": False, "collection_id": "pbc_3793906494"},
            "skill_name": {"type": "text", "required": False},
            "trigger_phrase": {"type": "text", "required": False},
            "operation_type": {"type": "text", "required": False},
            "result": {"type": "select", "required": False, "values": ['success', 'partial', 'failed', 'blocked', 'pending']},
            "context_bytes_used": {"type": "number", "required": False},
            "notes": {"type": "text", "required": False},
        },
    },
    "skill_refs": {
        "fields": {
            "skill": {"type": "relation", "required": False, "collection_id": "pbc_3793906494"},
            "ref_key": {"type": "text", "required": True},
            "ref_type": {"type": "select", "required": True, "values": ['todoist_project', 'todoist_section', 'todoist_label', 'contact_email', 'file_path', 'url', 'skill_ref', 'pocketbase_collection', 'other']},
            "current_value": {"type": "text", "required": False},
            "label": {"type": "text", "required": False},
            "status": {"type": "select", "required": False, "values": ['valid', 'stale', 'broken', 'unknown']},
            "last_validated_at": {"type": "date", "required": False},
            "last_error": {"type": "text", "required": False},
        },
    },
    "skill_routes": {
        "fields": {
            "operation_type": {"type": "text", "required": True},
            "trigger_patterns": {"type": "json", "required": False},
            "skill_ids": {"type": "json", "required": False},
            "priority": {"type": "number", "required": False},
            "context_condition": {"type": "text", "required": False},
            "notes": {"type": "text", "required": False},
            "est_context_bytes": {"type": "number", "required": False},
            "enabled": {"type": "bool", "required": False},
        },
    },
    "skills": {
        "fields": {
            "name": {"type": "text", "required": True},
            "description": {"type": "text", "required": False},
            "category": {"type": "text", "required": False},
            "working_path": {"type": "text", "required": False},
            "live_path": {"type": "text", "required": False},
            "content_hash": {"type": "text", "required": False},
            "file_count": {"type": "number", "required": False},
            "size_bytes": {"type": "number", "required": False},
            "status": {"type": "select", "required": False, "values": ['healthy', 'stale', 'broken', 'unknown', 'deleted']},
            "triggers": {"type": "json", "required": False},
            "dependencies": {"type": "json", "required": False},
            "last_validated_at": {"type": "date", "required": False},
            "last_synced_at": {"type": "date", "required": False},
            "last_invoked_at": {"type": "date", "required": False},
            "last_error": {"type": "text", "required": False},
        },
    },
    "suppliers": {
        "fields": {
            "name": {"type": "text", "required": True},
            "contact_name": {"type": "text", "required": False},
            "email": {"type": "email", "required": False},
            "phone": {"type": "text", "required": False},
            "specialty": {"type": "text", "required": False},
            "notes": {"type": "text", "required": False},
            "aliases": {"type": "text", "required": False},
        },
    },
    "sync_log": {
        "fields": {
            "event_type": {"type": "text", "required": False},
            "detail": {"type": "json", "required": False},
            "occurred_at": {"type": "date", "required": False},
        },
    },
    "task_groups": {
        "fields": {
            "name": {"type": "text", "required": True},
            "client": {"type": "relation", "required": False, "collection_id": "pbc_2442875294"},
            "description": {"type": "text", "required": False},
            "todoist_task_ids": {"type": "json", "required": True},
            "status": {"type": "select", "required": False, "values": ['open', 'promoted', 'archived']},
            "job": {"type": "relation", "required": False, "collection_id": "pbc_2409499253"},
            "project_path": {"type": "text", "required": False},
            "archived": {"type": "bool", "required": False},
            "archived_reason": {"type": "text", "required": False},
            "archived_at": {"type": "date", "required": False},
        },
    },
}


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
