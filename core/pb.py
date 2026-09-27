"""
PocketBase client with write verification.

What it does
    A small HTTP client for the PocketBase REST API (list, get, create,
    update, delete, upsert). Every other part of the system that touches
    the database goes through it. Its job is to make sure that a write
    reported as successful really is in the database.

How a write is checked
    1. Schema check before sending. The payload is compared against
       `core/schema.py`, a generated copy of the database schema. A field
       that does not exist, a missing required field or a select value
       that is not allowed raises `SchemaViolation` locally, with the
       field named, and nothing is sent.
    2. Errors keep their reason. When PocketBase answers with a 4xx or
       5xx, the response body (which names the offending field) is kept
       on the raised `PocketBaseError` instead of being reduced to a bare
       status code.
    3. Read-back after the write. PocketBase answers a create or update
       with the record as it was stored. `_confirm` compares every field
       that was sent with the value that came back, using `values_match`.
       Any field that did not land raises `WriteNotLanded`.
    4. Re-authenticate once on 401/403. If the admin token has expired,
       the client logs in again and repeats the call one time.

Why each step exists
    - PocketBase ignores fields it does not know. A create with a
      misspelled field returns 200 and simply stores nothing for it, so
      only a local schema check (step 1) or a read-back (step 3) can
      notice.
    - A 400 without its body says only "Bad Request". The body says which
      field and why, which is what makes the error fixable (step 2).
    - Long-running processes (the daemon loop, the MCP server) outlive
      their admin token. Without step 4 every write after expiry would
      fail and look like a real permissions error. Only 401 and 403 are
      retried: a 400 is a statement about the payload and retrying it
      would only repeat the same failure.

Related pieces
    `core/ledger.py` wraps this client so every successful write is also
    appended to the write ledger, which the validator re-reads at the end
    of a run. See docs/HARNESSES.md for how these checks fit together.
"""

from __future__ import annotations

import httpx

from . import schema


class PocketBaseError(Exception):
    """A PocketBase call failed. Carries the response body so the reason
    (usually a field name and a message) is not lost."""

    def __init__(self, method, url, status, body, payload=None):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        self.payload = payload
        detail = self._readable(body)
        super().__init__("%s %s -> %s%s" % (method, url, status, detail))

    @staticmethod
    def _readable(body):
        """Flatten PocketBase's nested error dict into one line.

        >>> PocketBaseError._readable(
        ...     {"message": "Failed to create record.",
        ...      "data": {"name": {"message": "Missing required value."}}})
        ' | Failed to create record. | name: Missing required value.'
        """
        if not isinstance(body, dict):
            return " %s" % (body,)
        parts = []
        message = body.get("message")
        if message:
            parts.append(message)
        data = body.get("data") or {}
        for field, info in data.items():
            if isinstance(info, dict):
                parts.append("%s: %s" % (field, info.get("message", info)))
            else:
                parts.append("%s: %s" % (field, info))
        return " | " + " | ".join(parts) if parts else " %s" % (body,)


class SchemaViolation(Exception):
    """A write was refused locally because it does not match core/schema.py."""


class WriteNotLanded(Exception):
    """The call succeeded but a value is missing from the stored record.

    PocketBase drops fields it does not recognise without an error, so
    the HTTP call returns 200 while the value is lost. Raising here makes
    that loss stop the run instead of being logged as a save.
    """


def _as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value):
    """The YYYY-MM-DD part of a value, or None if it does not start with a date.

    Dates are sent as `2026-08-08` and come back as
    `2026-08-08 00:00:00.000Z`. Both mean the same day.
    """
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        head = text[:10]
        if head.replace("-", "").isdigit():
            return head
    return None


def values_match(sent, stored):
    """Decide whether the stored value is the value that was sent.

    Lenient about formatting, strict about meaning:

    >>> values_match(5, "5.0")                                 # True, same number
    >>> values_match("2026-08-08", "2026-08-08 00:00:00.000Z") # True, same day
    >>> values_match("", None)                                 # True, both empty
    >>> values_match("cli_1", {"id": "cli_1", "name": "..."})  # True, expanded relation
    >>> values_match(False, "")                                # False, False is a value
    >>> values_match("cli_1", {"id": "cli_2"})                 # False, different record
    """
    if sent == stored:
        return True

    blank = ("", None, [], {})
    if sent in blank and stored in blank:
        return True

    if isinstance(sent, bool) or isinstance(stored, bool):
        # Booleans are compared as booleans (with 0/1 accepted), never by
        # truthiness. Truthiness would treat False and "missing" as equal,
        # so `paid: False` sent to a misspelled field would look saved.
        if isinstance(sent, bool) and isinstance(stored, bool):
            return sent == stored
        if isinstance(sent, bool) and stored in (0, 1):
            return int(sent) == int(stored)
        if isinstance(stored, bool) and sent in (0, 1):
            return int(sent) == int(stored)
        return False

    # A relation may come back as the whole related record (when expanded)
    # instead of its id. Compare the ids, so the format is forgiven but a
    # link to the wrong record is still caught.
    if isinstance(stored, dict) and "id" in stored:
        return values_match(sent, stored["id"])
    if isinstance(sent, dict) and "id" in sent:
        return values_match(sent["id"], stored)

    if isinstance(sent, (list, tuple)) or isinstance(stored, (list, tuple)):
        sent_list = [s.get("id") if isinstance(s, dict) else s for s in (sent or [])]
        stored_list = [s.get("id") if isinstance(s, dict) else s for s in (stored or [])]
        return sent_list == stored_list

    sent_text = str(sent if sent is not None else "").strip()
    stored_text = str(stored if stored is not None else "").strip()
    if sent_text == stored_text:
        return True

    sent_number, stored_number = _as_number(sent_text), _as_number(stored_text)
    if sent_number is not None and sent_number == stored_number:
        return True

    sent_date = _as_date(sent_text)
    if sent_date and sent_date == _as_date(stored_text):
        return True

    return False


class PocketBaseClient:
    """PocketBase REST client. See the module docstring for the write checks.

    `enforce_schema` turns the local schema check on or off, and
    `verify_writes` turns the read-back on or off. Both default to on;
    tests switch them off to exercise one behaviour at a time.
    """

    def __init__(self, base_url, admin_email="", admin_password="", timeout=15.0,
                 enforce_schema=True, verify_writes=True):
        self.base_url = base_url.rstrip("/")
        self.admin_email = admin_email
        self.admin_password = admin_password
        self.enforce_schema = enforce_schema
        self.verify_writes = verify_writes
        self.token = None
        self.http = httpx.Client(timeout=timeout)

    # transport
    def _headers(self):
        return {"Authorization": self.token} if self.token else {}

    def _request(self, method, path, payload=None, params=None, _retried=False):
        """Send one request and return the decoded JSON body.

        Raises `PocketBaseError` (with the response body attached) on any
        status of 400 or above. Returns {} for an empty or 204 response.
        """
        url = "%s%s" % (self.base_url, path)
        resp = self.http.request(
            method, url, headers=self._headers(), json=payload, params=params
        )
        if resp.status_code >= 400:
            if self._should_reauth(resp.status_code, path, _retried):
                # The admin token has probably expired: log in again and
                # repeat the call once. `_retried` stops a second retry.
                self.auth_admin()
                return self._request(method, path, payload=payload,
                                     params=params, _retried=True)
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:500]
            raise PocketBaseError(method, url, resp.status_code, body, payload)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def _should_reauth(self, status, path, already_retried):
        """True only for a first 401/403 on a client that has credentials,
        and never for the login call itself."""
        if already_retried or status not in (401, 403):
            return False
        if not self.admin_email or not self.admin_password:
            return False
        # A 401 from the login call means the credentials are wrong.
        # Retrying it would loop and hide that message.
        return "auth-with-password" not in path

    def _check(self, collection, payload, partial):
        """Step 1: refuse a write that does not match core/schema.py.

        Collections that are not in the generated schema are passed
        through unchecked.
        """
        if not self.enforce_schema or collection not in schema.SCHEMA:
            return
        problems = schema.validate_payload(collection, payload)
        if partial:
            # An update only sends the fields it changes, so a required
            # field being absent is expected. Unknown fields and bad
            # select values still count.
            problems = [p for p in problems if not p.startswith("required field")]
        if problems:
            raise SchemaViolation(
                "refusing to write to '%s':\n  - %s" % (collection, "\n  - ".join(problems))
            )

    # auth
    def auth_admin(self):
        """Log in as a PocketBase superuser and keep the token for later calls."""
        if not self.admin_email:
            raise PocketBaseError("POST", "auth", 0, {"message": "no admin email configured"})
        data = self._request(
            "POST",
            "/api/collections/_superusers/auth-with-password",
            payload={"identity": self.admin_email, "password": self.admin_password},
        )
        self.token = data["token"]
        return True

    def health(self):
        """True if the server answers its health endpoint. Never raises."""
        try:
            self._request("GET", "/api/health")
            return True
        except Exception:
            return False

    # reads
    def list(self, collection, filter_str="", sort="", page=1, per_page=200, expand=""):
        """One page of records, as PocketBase returns it (items, page,
        totalPages, totalItems)."""
        params = {"page": page, "perPage": per_page}
        if filter_str:
            params["filter"] = filter_str
        if sort:
            params["sort"] = sort
        if expand:
            params["expand"] = expand
        return self._request("GET", "/api/collections/%s/records" % collection, params=params)

    def list_all(self, collection, filter_str="", sort=""):
        """Every matching record, following pagination to the last page.

        Reading only the first page would silently truncate any collection
        larger than one page (200 records).
        """
        items = []
        page = 1
        while True:
            data = self.list(collection, filter_str=filter_str, sort=sort,
                             page=page, per_page=200)
            items.extend(data.get("items", []))
            if page >= data.get("totalPages", 1):
                break
            page += 1
        return items

    def get(self, collection, record_id):
        return self._request("GET", "/api/collections/%s/records/%s" % (collection, record_id))

    def find_one(self, collection, filter_str):
        """The first record matching a PocketBase filter, or None."""
        data = self.list(collection, filter_str=filter_str, per_page=1)
        items = data.get("items", [])
        return items[0] if items else None

    def count(self, collection, filter_str=""):
        return self.list(collection, filter_str=filter_str, per_page=1).get("totalItems", 0)

    def field_names(self, collection):
        """The set of field names a collection has, read from its schema.

        Useful before sorting or filtering on a field that may not exist.
        For example, a collection created without PocketBase's automatic
        date fields has no `created`, and sorting on `created` returns a
        400 whose message does not name the field.

        The collection definition is asked directly because reading a
        record and looking at its keys gives no answer for an empty
        collection. Newer PocketBase versions list fields under `fields`,
        older ones under `schema`; both are accepted.

        Returns an empty set if the schema cannot be read, which callers
        treat as "unknown" rather than "the field is missing".
        """
        try:
            data = self._request("GET", "/api/collections/%s" % collection)
        except Exception:
            return set()
        fields = data.get("fields") or data.get("schema") or []
        return {f.get("name") for f in fields if f.get("name")}

    def _confirm(self, collection, payload, record):
        """Step 3: compare what was sent with the record PocketBase stored.

        PocketBase returns the stored record in its reply to a create or
        update, so this needs no extra request. Every field that did not
        land is listed in the `WriteNotLanded` message with the value
        asked for and the value stored, so the fix is obvious from the
        error alone.
        """
        if not self.verify_writes or not isinstance(record, dict):
            return record
        missed = []
        for field, wanted in (payload or {}).items():
            got = record.get(field)
            if not values_match(wanted, got):
                missed.append("%s: asked for %r, stored %r" % (field, wanted, got))
        if missed:
            raise WriteNotLanded(
                "write to '%s' record %s reported success but did not save:\n  - %s"
                % (collection, record.get("id", "?"), "\n  - ".join(missed))
            )
        return record

    # writes
    def create(self, collection, payload):
        """Schema check, POST, then read-back. Returns the stored record."""
        self._check(collection, payload, partial=False)
        record = self._request(
            "POST", "/api/collections/%s/records" % collection, payload=payload
        )
        return self._confirm(collection, payload, record)

    def update(self, collection, record_id, payload):
        """Schema check (partial), PATCH, then read-back. Returns the stored record."""
        self._check(collection, payload, partial=True)
        record = self._request(
            "PATCH", "/api/collections/%s/records/%s" % (collection, record_id), payload=payload
        )
        return self._confirm(collection, payload, record)

    def delete(self, collection, record_id):
        """Delete a record and return True once it is gone.

        A 404 also returns True, since the record is absent either way.
        Any other PocketBase error returns False.
        """
        try:
            self._request("DELETE", "/api/collections/%s/records/%s" % (collection, record_id))
            return True
        except PocketBaseError as exc:
            return exc.status == 404

    def upsert(self, collection, match_field, value, payload):
        """Update the record whose `match_field` equals `value`, or create it.

        The lookup is a server-side filter, so it works at any collection
        size. `match_field` must be a real field of the collection:
        matching on a field that does not exist would never find anything
        and would create a duplicate on every call, so it raises
        `SchemaViolation` instead.
        """
        if collection in schema.SCHEMA and match_field not in schema.fields(collection):
            raise SchemaViolation(
                "cannot upsert on '%s.%s': that field does not exist. Fields are: %s"
                % (collection, match_field, sorted(schema.fields(collection)))
            )
        safe = str(value).replace("'", "\\'")
        existing = self.find_one(collection, "%s='%s'" % (match_field, safe))
        if existing:
            return self.update(collection, existing["id"], payload)
        body = dict(payload)
        body[match_field] = value
        return self.create(collection, body)

    # admin
    def collections(self):
        """Every collection definition on the server (needs admin auth)."""
        return self._request("GET", "/api/collections", params={"perPage": 200}).get("items", [])

    def close(self):
        self.http.close()
