"""
Add map coordinates to client addresses that do not have them yet.

Usage
    python -m tools.geocode_clients                              report only, change nothing
    python -m tools.geocode_clients --write                      save the coordinates
    python -m tools.geocode_clients --write --only "Dairyland"   one client (name contains)

What it does
    The dashboard's map places a client only from coordinates written at
    the end of its address, in the form "... | GPS: -37.78, 176.33". A
    client without them is listed as unmapped instead of getting a guessed
    pin, because a pin in the wrong place looks exactly like a right one.

    For each client whose address has no coordinates, this asks a geocoder
    where the address is, judges the answer, and (with --write) appends the
    coordinates to the address field.

    The geocoder is OpenStreetMap's Nominatim, which needs no API key. Its
    usage policy asks for at most one request per second and a User-Agent
    that identifies the caller with a contact address. Both are followed:
    calls are spaced SECONDS_BETWEEN_CALLS apart, and the contact address
    comes from the GEOCODER_CONTACT_EMAIL environment variable (default
    splatt-ops@example.com; set a real one in .env before using this).

What it will not do
    It never overwrites coordinates already on an address; somebody typed
    those on purpose.

    It refuses answers it cannot trust. A geocoder always returns
    something: ask for a road that does not exist and it may return the
    centre of the region. An answer is accepted only when it is more
    specific than a region (see TOO_VAGUE) and falls inside the country the
    address names (see COUNTRY_BOXES). Everything else is reported and left
    alone.

    It changes only the address field, and only by appending the
    coordinates to what is already there.

Read the report from a run without --write before saving anything.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

import httpx

from core.config import ConfigError, settings
from core.logging_setup import setup
from core.pb import PocketBaseClient

log = setup("geocode_clients")

#: Nominatim's usage policy requires at most one request a second and a
#: User-Agent that identifies the caller with a way to contact them. A
#: caller that ignores either can have its IP address blocked.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
SECONDS_BETWEEN_CALLS = 1.1

#: The contact address used when GEOCODER_CONTACT_EMAIL is not set.
DEFAULT_CONTACT_EMAIL = "splatt-ops@example.com"


def user_agent():
    """The User-Agent sent to Nominatim, with the contact address from
    GEOCODER_CONTACT_EMAIL. Read on every call rather than at import, so a
    value loaded from .env by settings() is picked up.

    >>> user_agent()  # with GEOCODER_CONTACT_EMAIL unset
    'splatt-ops-geocoder/1.0 (splatt-ops@example.com)'
    """
    contact = os.environ.get("GEOCODER_CONTACT_EMAIL", "").strip()
    return "splatt-ops-geocoder/1.0 (%s)" % (contact or DEFAULT_CONTACT_EMAIL)

#: Written on the end of the address, exactly as the dashboard reads it.
#: See parseGPS in dashboard/index.html.
GPS_PATTERN = re.compile(r"GPS:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")
GPS_SUFFIX = " | GPS: %.4f, %.4f"

#: Answers too vague to use, by Nominatim's addresstype.
#:
#: The set lists what to refuse rather than what to accept. OpenStreetMap
#: has hundreds of place tags, and an exact match can carry an unusual one:
#: a brewery building, for example, is tagged "craft". A list of accepted
#: types would always be missing some and would throw away correct
#: answers. There are only a few ways to be too vague (a region, a country,
#: a body of water), so those are listed, and anything more specific is
#: accepted, with the country box check below as a second guard.
TOO_VAGUE = {
    "state", "state_district", "province", "territory", "region",
    "county", "district", "country", "continent",
    "island", "archipelago", "sea", "ocean", "water",
}

#: Rough (south, north, west, east) boxes, used only to catch an answer in
#: the wrong country, which usually happens when a street name also exists
#: somewhere else. They are deliberately generous: the aim is to catch a
#: blunder, not to check precision.
COUNTRY_BOXES = {
    "new zealand": (-47.5, -34.0, 166.0, 179.5),
    "fiji": (-21.0, -12.0, 176.0, 180.0),
    "australia": (-44.0, -10.0, 112.0, 154.0),
}
DEFAULT_COUNTRY = "new zealand"


def parse_gps(address):
    """The coordinates already on an address, as (lat, lng), or None.

    GPS_PATTERN is the same regex the dashboard's parseGPS uses. If the two
    differed, the tool could append a second set of coordinates to an
    address the dashboard already reads as placed.

    >>> parse_gps("12 Example Road, Te Puke | GPS: -37.7850, 176.3260")
    (-37.785, 176.326)
    """
    hit = GPS_PATTERN.search(address or "")
    if not hit:
        return None
    return float(hit.group(1)), float(hit.group(2))


def strip_gps(address):
    """The address without the coordinates on the end."""
    return re.sub(r"\s*\|\s*GPS:.*$", "", address or "").strip()


def country_of(address):
    """Which country box to check the answer against.

    Read from the address text, because clients have no country field.
    Defaults to New Zealand, where almost all clients are.
    """
    text = (address or "").lower()
    for name in COUNTRY_BOXES:
        if name in text:
            return name
    return DEFAULT_COUNTRY


def inside_country(lat, lng, country):
    box = COUNTRY_BOXES.get(country)
    if not box:
        return True
    south, north, west, east = box
    return south <= lat <= north and west <= lng <= east


def judge(result, address):
    """Accept or refuse one geocoder answer. No network access.

    Returns (lat, lng, None) when the answer is usable, or
    (None, None, reason) when it is not. The reason goes into the report,
    so every refusal is visible to the operator.
    """
    if not result:
        return None, None, "no match"

    try:
        lat = float(result.get("lat"))
        lng = float(result.get("lon"))
    except (TypeError, ValueError):
        return None, None, "unreadable coordinates"

    kind = (result.get("addresstype") or result.get("type") or "").lower()
    if not kind or kind in TOO_VAGUE:
        # The most important refusal: for a road it does not know,
        # Nominatim can answer with the whole region, which would look like
        # a success and put the pin tens of kilometres away.
        return None, None, "too vague, matched a %s" % (kind or "unknown thing")

    country = country_of(address)
    if not inside_country(lat, lng, country):
        return None, None, "landed outside %s" % country

    return lat, lng, None


def lookup(http, query):
    """One Nominatim call. The only thing here that touches the network."""
    resp = http.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": 1,
                "addressdetails": 0},
        headers={"User-Agent": user_agent()},
    )
    resp.raise_for_status()
    found = resp.json()
    return found[0] if found else None


#: A house number at the front of a street line. Covers "12", "12A",
#: "16-18" and "2/8", which are all normal ways to write a New Zealand
#: street number.
HOUSE_NUMBER = re.compile(r"^\d+[A-Za-z]?(?:\s*[-/]\s*\d+[A-Za-z]?)*\s+(?P<street>\S.*)$")


def candidates(address):
    """The queries to try for one address, most specific first.

    Each later query gives up a little more detail, because a pin on the
    right street beats one on the right town, which beats no pin:

    1. the full address,
    2. the same without the house number (OpenStreetMap knows most roads
       but only some house numbers, and an unknown number can make the
       whole query fail),
    3. without the street line,
    4. the last three parts.

    Repeats are dropped.

    >>> candidates("45 Demo Road, RD 1, Hamilton 3281, New Zealand")[:3]
    ['45 Demo Road, RD 1, Hamilton 3281, New Zealand', 'Demo Road, RD 1, Hamilton 3281, New Zealand', 'RD 1, Hamilton 3281, New Zealand']
    """
    clean = strip_gps(address)
    if not clean:
        return []
    tries = [clean]
    parts = [p.strip() for p in clean.split(",") if p.strip()]
    numberless = HOUSE_NUMBER.match(parts[0]) if parts else None
    if numberless:
        tries.append(", ".join([numberless.group("street")] + parts[1:]))
    if len(parts) > 2:
        tries.append(", ".join(parts[1:]))
    if len(parts) > 3:
        tries.append(", ".join(parts[-3:]))
    # Keep the order, drop repeats.
    seen = set()
    return [t for t in tries if not (t in seen or seen.add(t))]


class Row:
    """One client, and what happened to it. This is the report."""

    def __init__(self, client):
        self.id = client.get("id")
        self.name = client.get("name") or "(unnamed)"
        self.address = client.get("address") or ""
        self.lat = None
        self.lng = None
        self.reason = ""
        self.query = ""
        self.state = "pending"

    @property
    def new_address(self):
        return strip_gps(self.address) + (GPS_SUFFIX % (self.lat, self.lng))

    def to_line(self):
        if self.state == "found":
            return "  %-34s %9.4f %9.4f   %s" % (
                self.name[:34], self.lat, self.lng, self.query[:44])
        return "  %-34s %s" % (self.name[:34], self.reason)


def collect(clients, only=""):
    """Which clients need a lookup, and why the rest do not."""
    todo, skipped = [], []
    for client in clients:
        row = Row(client)
        if only and only.lower() not in row.name.lower():
            continue
        if parse_gps(row.address):
            row.state, row.reason = "has_gps", "already has coordinates"
            skipped.append(row)
        elif not strip_gps(row.address):
            row.state, row.reason = "no_address", "no address to look up"
            skipped.append(row)
        else:
            todo.append(row)
    return todo, skipped


def geocode(rows, http, sleep=time.sleep):
    """Look up every row, pausing SECONDS_BETWEEN_CALLS between calls.

    The pause is per call, not per row, because one row can need two or
    three queries when the street is unknown; pausing per row would break
    the rate limit on exactly those rows. `http` is anything with an httpx
    style get(), and `sleep` is replaceable so tests run instantly.
    """
    calls = 0
    for number, row in enumerate(rows, start=1):
        print("  [%2d/%d] %s" % (number, len(rows), row.name[:50]), flush=True)
        for query in candidates(row.address):
            if calls:
                sleep(SECONDS_BETWEEN_CALLS)
            calls += 1
            try:
                result = lookup(http, query)
            except Exception as exc:
                row.state, row.reason = "failed", "lookup failed: %s" % exc
                log.error("lookup failed for %s: %s", row.name, exc)
                break
            lat, lng, why = judge(result, row.address)
            if lat is not None:
                row.lat, row.lng, row.query = lat, lng, query
                row.state = "found"
                break
            row.reason = why
        if row.state == "pending":
            row.state = "refused"
    return rows


def report(found, refused, skipped, wrote):
    lines = ["", "CLIENT COORDINATES", "==================", ""]
    if found:
        lines.append("Placed (%d):" % len(found))
        lines += [r.to_line() for r in found]
        lines.append("")
    if refused:
        lines.append("Left alone, not confident enough (%d):" % len(refused))
        lines += [r.to_line() for r in refused]
        lines.append("")
        lines.append("  These stay in the dashboard's unmapped list. Add")
        lines.append("  coordinates by hand on the client record if any of")
        lines.append("  them matter, in the form:")
        lines.append("    123 Some Road, Town | GPS: -37.7850, 176.3260")
        lines.append("")
    if skipped:
        lines.append("Untouched (%d): %d already had coordinates, %d have no "
                     "address." % (
                         len(skipped),
                         len([r for r in skipped if r.state == "has_gps"]),
                         len([r for r in skipped if r.state == "no_address"])))
        lines.append("")
    if wrote:
        lines.append("Saved %d client records." % wrote)
    else:
        lines.append("Nothing was changed. Run again with --write to save.")
    return "\n".join(lines)


def main(argv=None):
    """Read every client, geocode the ones without coordinates, report, and
    with --write save the new addresses. Returns 0, or 2 when the config
    or PocketBase is unavailable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="save the coordinates, otherwise report only")
    parser.add_argument("--only", default="",
                        help="only clients whose name contains this")
    args = parser.parse_args(argv)

    try:
        conf = settings()
    except ConfigError as exc:
        log.error("config could not be loaded: %s", exc)
        return 2

    pb = PocketBaseClient(**conf.pocketbase())
    if not pb.health():
        log.error("PocketBase is not reachable")
        return 2
    pb.auth_admin()

    clients = pb.list_all("clients")
    todo, skipped = collect(clients, args.only)
    log.info("%d clients, %d need coordinates", len(clients), len(todo))

    if todo:
        print("Looking up %d addresses, one a second." % len(todo), flush=True)
        with httpx.Client(timeout=20.0, follow_redirects=True) as http:
            geocode(todo, http)
        print("", flush=True)

    found = [r for r in todo if r.state == "found"]
    refused = [r for r in todo if r.state != "found"]

    wrote = 0
    if args.write:
        for row in found:
            try:
                pb.update("clients", row.id, {"address": row.new_address})
                wrote += 1
            except Exception as exc:
                row.state, row.reason = "failed", "save failed: %s" % exc
                log.error("could not save %s: %s", row.name, exc)
        found = [r for r in found if r.state == "found"]
        refused = [r for r in todo if r.state != "found"]

    print(report(found, refused, skipped, wrote))
    pb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
