"""
Tests for tools/geocode_clients.py.

The important behaviour is not finding an address but refusing the answers
it should refuse. A geocoder always replies: ask for a road it does not
know and it may return the middle of a region, which looks like a success.
Every accepted answer is written onto a client record, so a wrong answer
is expensive and a refusal is cheap.

The addresses are fictional; the town coordinates are real so the country
checks behave as they would in use. Nothing here touches the network.
"""

from __future__ import annotations

from tools import geocode_clients as geo

REAL = {"lat": "-37.6567", "lon": "175.5303", "addresstype": "town"}


def client(name="Dairyland Morrinsville", address="Morrinsville, Waikato, New Zealand",
           cid="c1"):
    return {"id": cid, "name": name, "address": address}


# ---------------------------------------------------------------------------
# Reading what is already there
# ---------------------------------------------------------------------------

def test_coordinates_already_on_an_address_are_read_back():
    got = geo.parse_gps("12 Example Road, Te Puke | GPS: -37.7850, 176.3260")

    assert got == (-37.785, 176.326)


def test_an_address_with_no_coordinates_reads_as_none():
    assert geo.parse_gps("Morrinsville, Waikato, New Zealand") is None
    assert geo.parse_gps("") is None
    assert geo.parse_gps(None) is None


def test_the_regex_matches_the_one_the_dashboard_uses():
    """If these two drift apart the tool writes a second set of
    coordinates onto an address that already had some, and the dashboard
    reads whichever comes first. Same pattern, same answer, both ways."""
    written = geo.strip_gps("Some Road, Town") + (geo.GPS_SUFFIX % (-36.8, 174.7))

    assert geo.parse_gps(written) == (-36.8, 174.7)


def test_stripping_leaves_the_address_and_takes_only_the_coordinates():
    assert geo.strip_gps("12 Test St, Dunedin | GPS: -45.87, 170.50") == \
        "12 Test St, Dunedin"


# ---------------------------------------------------------------------------
# Refusing bad answers
# ---------------------------------------------------------------------------

def test_a_real_town_is_accepted():
    lat, lng, why = geo.judge(REAL, "Morrinsville, Waikato, New Zealand")

    assert why is None
    assert (lat, lng) == (-37.6567, 175.5303)


def test_a_region_is_refused_because_it_is_not_a_location():
    """A pin in the middle of the Waikato region is not a location, and
    accepting it would put the client tens of kilometres from its site."""
    vague = {"lat": "-37.8", "lon": "175.3", "addresstype": "state"}

    lat, lng, why = geo.judge(vague, "Morrinsville, Waikato, New Zealand")

    assert lat is None
    assert "too vague" in why


def test_a_whole_country_is_refused():
    whole = {"lat": "-41.5", "lon": "173.0", "addresstype": "country"}

    lat, _, why = geo.judge(whole, "Somewhere, New Zealand")

    assert lat is None
    assert "too vague" in why


def test_an_exact_building_is_accepted_whatever_openstreetmap_calls_it():
    """The vague check lists what to refuse, not what to accept.

    An exact match can carry an unusual OpenStreetMap tag: a brewery
    building such as "Kea Brewery, 3 Example Street, Paraparaumu" is
    tagged "craft". A list of accepted types would always be missing some,
    so only the few vague types are refused, and all of these pass.
    """
    for kind in ("craft", "man_made", "leisure", "tourism", "commercial",
                 "farm", "isolated_dwelling", "unit", "place"):
        found = {"lat": "-40.9140", "lon": "175.0080", "addresstype": kind}

        lat, _, why = geo.judge(found, "3 Example Street, Paraparaumu, New Zealand")

        assert why is None, "refused %s, which is an exact place" % kind
        assert lat == -40.914


def test_an_answer_on_the_wrong_side_of_the_world_is_refused():
    """Street names repeat across countries. A match in Canada for a road
    in Dunedin is a plausible reply and a useless pin."""
    canada = {"lat": "43.6532", "lon": "-79.3832", "addresstype": "road"}

    lat, _, why = geo.judge(canada, "Test Street, Dunedin, New Zealand")

    assert lat is None
    assert "outside new zealand" in why


def test_fiji_is_checked_against_fiji_and_not_new_zealand():
    """A client in Fiji (such as IBF Fiji) must be checked against the Fiji
    box; the New Zealand box would refuse every correct answer."""
    suva = {"lat": "-18.1416", "lon": "178.4419", "addresstype": "city"}

    lat, _, why = geo.judge(suva, "Suva, Fiji")

    assert why is None
    assert lat == -18.1416


def test_no_match_at_all_is_refused_rather_than_crashing():
    lat, _, why = geo.judge(None, "Nowhere at all")

    assert lat is None
    assert why == "no match"


def test_an_answer_with_unreadable_coordinates_is_refused():
    lat, _, why = geo.judge({"lat": "north a bit", "lon": "", "addresstype": "road"},
                            "Somewhere, New Zealand")

    assert lat is None
    assert why == "unreadable coordinates"


# ---------------------------------------------------------------------------
# Which clients get touched
# ---------------------------------------------------------------------------

def test_a_client_that_already_has_coordinates_is_never_looked_up():
    """Somebody typed those in on purpose. A machine should not argue."""
    todo, skipped = geo.collect([client(address="Te Puke | GPS: -37.78, 176.33")])

    assert todo == []
    assert skipped[0].reason == "already has coordinates"


def test_a_client_with_no_address_is_reported_not_guessed():
    todo, skipped = geo.collect([client(address="")])

    assert todo == []
    assert skipped[0].reason == "no address to look up"


def test_a_client_with_an_address_and_no_coordinates_is_the_work():
    todo, skipped = geo.collect([client()])

    assert len(todo) == 1
    assert skipped == []


def test_only_narrows_the_run_to_one_client():
    rows = [client(name="Dairyland Morrinsville"), client(name="Moana Dairy", cid="c2")]

    todo, _ = geo.collect(rows, only="dairyland")

    assert [r.name for r in todo] == ["Dairyland Morrinsville"]


# ---------------------------------------------------------------------------
# The queries it tries
# ---------------------------------------------------------------------------

def test_the_full_address_is_tried_first():
    tries = geo.candidates("12 Example Road, RD 2, Te Puke 3182, New Zealand")

    assert tries[0] == "12 Example Road, RD 2, Te Puke 3182, New Zealand"


def test_the_house_number_goes_before_the_street_does():
    """OpenStreetMap knows the road far more often than the house number.

    For "45 Demo Road, RD 1, Hamilton 3281", an unknown house number can
    make the whole query fail while the road alone is found. So the number
    is the first thing given up, and the street is kept for one more try
    before falling back to "RD 1, Hamilton", a rural delivery area covering
    a lot of farmland.
    """
    tries = geo.candidates("45 Demo Road, RD 1, Hamilton 3281, New Zealand")

    assert tries[1] == "Demo Road, RD 1, Hamilton 3281, New Zealand"
    assert tries.index("Demo Road, RD 1, Hamilton 3281, New Zealand") < \
        tries.index("RD 1, Hamilton 3281, New Zealand")


def test_the_usual_ways_of_writing_a_street_number_are_all_recognised():
    for written, street in (
        ("12 Example Road, Te Puke, New Zealand", "Example Road"),
        ("12A Test Street, Dunedin, New Zealand", "Test Street"),
        ("16-18 Example Road, Auckland, New Zealand", "Example Road"),
        ("2/8 Sample Street, Auckland, New Zealand", "Sample Street"),
    ):
        assert geo.candidates(written)[1].startswith(street), \
            "did not strip the number off %r" % written


def test_a_street_with_no_number_does_not_get_a_pointless_repeat():
    """An address with no house number must not produce the same query
    twice, which would only spend a second of the rate limit."""
    tries = geo.candidates("Example Street, Morrinsville 3300, New Zealand")

    assert len(tries) == len(set(tries))
    assert tries[0] == "Example Street, Morrinsville 3300, New Zealand"


def test_the_street_is_dropped_once_the_numberless_street_has_been_tried():
    """A rural delivery address is often unknown to the geocoder while
    the town is known perfectly well. A town pin beats a region pin."""
    tries = geo.candidates("12 Example Road, RD 2, Te Puke 3182, New Zealand")

    assert "RD 2, Te Puke 3182, New Zealand" in tries


def test_the_same_query_is_never_sent_twice():
    tries = geo.candidates("Morrinsville, New Zealand")

    assert len(tries) == len(set(tries))


def test_an_address_that_is_only_coordinates_produces_no_queries():
    assert geo.candidates(" | GPS: -37.9, 175.5") == []


# ---------------------------------------------------------------------------
# The run, with the network faked out
# ---------------------------------------------------------------------------

class FakeHttp:
    """Answers a fixed script, and records what it was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, params=None, headers=None):
        query = (params or {}).get("q")
        self.asked.append((query, (headers or {}).get("User-Agent")))
        return FakeResponse(self.answers.get(query))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return [self.payload] if self.payload else []


def test_a_found_client_gets_the_coordinates_appended_to_its_address():
    row = geo.collect([client()])[0][0]
    http = FakeHttp({"Morrinsville, Waikato, New Zealand": REAL})

    geo.geocode([row], http, sleep=lambda _: None)

    assert row.state == "found"
    assert row.new_address == \
        "Morrinsville, Waikato, New Zealand | GPS: -37.6567, 175.5303"


def test_the_original_address_text_is_kept_word_for_word():
    """The address is what a person reads. Only coordinates are added."""
    row = geo.collect([client()])[0][0]
    geo.geocode([row], FakeHttp({"Morrinsville, Waikato, New Zealand": REAL}),
                sleep=lambda _: None)

    assert row.new_address.startswith("Morrinsville, Waikato, New Zealand")


def test_it_falls_back_to_the_town_when_the_street_is_unknown():
    row = geo.collect([client(address="99 Nonexistent Rd, Morrinsville, New Zealand")])[0][0]
    http = FakeHttp({"Morrinsville, New Zealand": REAL})

    geo.geocode([row], http, sleep=lambda _: None)

    assert row.state == "found"
    assert row.query == "Morrinsville, New Zealand"


def test_a_client_it_cannot_place_is_left_alone_and_reported():
    row = geo.collect([client(address="Somewhere Vague, New Zealand")])[0][0]

    geo.geocode([row], FakeHttp({}), sleep=lambda _: None)

    assert row.state == "refused"
    assert row.lat is None
    assert row.reason == "no match"


def test_every_call_identifies_itself():
    """Nominatim blocks callers that do not identify themselves, and a
    block applies to everything else sharing the same IP address."""
    row = geo.collect([client()])[0][0]
    http = FakeHttp({"Morrinsville, Waikato, New Zealand": REAL})

    geo.geocode([row], http, sleep=lambda _: None)

    assert all(agent and "splatt" in agent.lower() for _, agent in http.asked)


def test_it_waits_between_calls_and_not_between_rows():
    """One row can cost three calls. Pausing per row rather than per call
    breaks the rate limit on exactly the rows that need the most work."""
    rows = geo.collect([client(address="99 Nowhere Rd, Nowhere Town, New Zealand"),
                        client(address="98 Nowhere Rd, Nowhere Town, New Zealand",
                               cid="c2")])[0]
    waits = []

    geo.geocode(rows, FakeHttp({}), sleep=waits.append)

    calls = sum(len(geo.candidates(r.address)) for r in rows)
    assert len(waits) == calls - 1
    assert all(w >= 1.0 for w in waits)


def test_a_lookup_that_throws_stops_that_client_and_not_the_run():
    class Broken:
        def get(self, *a, **kw):
            raise RuntimeError("network went away")

    rows = geo.collect([client(), client(name="Moana Dairy", cid="c2")])[0]

    geo.geocode(rows, Broken(), sleep=lambda _: None)

    assert all(r.state == "failed" for r in rows)
    assert "network went away" in rows[0].reason


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def test_the_dry_run_says_plainly_that_nothing_changed():
    text = geo.report([], [], [], wrote=0)

    assert "Nothing was changed" in text


def test_the_report_names_the_clients_it_could_not_place():
    row = geo.collect([client(name="Mystery Ltd")])[0][0]
    row.state, row.reason = "refused", "no match"

    text = geo.report([], [row], [], wrote=0)

    assert "Mystery Ltd" in text
    assert "no match" in text
    assert "GPS: -37.7850, 176.3260" in text, "the report must show how to fix it by hand"


# ---------------------------------------------------------------------------
# The contact address in the User-Agent
# ---------------------------------------------------------------------------

def test_the_user_agent_has_a_placeholder_contact_by_default(monkeypatch):
    monkeypatch.delenv("GEOCODER_CONTACT_EMAIL", raising=False)

    assert geo.user_agent() == "splatt-ops-geocoder/1.0 (splatt-ops@example.com)"


def test_the_contact_address_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEOCODER_CONTACT_EMAIL", "ops@example.co.nz")
    row = geo.collect([client()])[0][0]
    http = FakeHttp({"Morrinsville, Waikato, New Zealand": REAL})

    geo.geocode([row], http, sleep=lambda _: None)

    assert http.asked[0][1] == "splatt-ops-geocoder/1.0 (ops@example.co.nz)"
