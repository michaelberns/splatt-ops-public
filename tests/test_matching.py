"""
Tests for the client and supplier matcher (core/matching.py).

The fixtures are fictional but keep the shapes real data has: full legal
names against short spoken names, accented names, two-word and one-word
spellings, short abbreviations, a company that is both a client and a
supplier, and two records sharing an alias. Each test pins down one rule:
which titles must match which record, and which titles must match nothing.
"""

from __future__ import annotations

import pytest

from core.matching import Index, fold, lead, match, suggest


def rec(rid, name, aliases=""):
    return {"id": rid, "name": name, "aliases": aliases}


CLIENTS = [
    rec("c_tuivalley", "Tui Valley Waters Ltd", "Tui Valley"),
    rec("c_cablecorp", "Cablecorp NZ Ltd", "Cablecorp"),
    rec("c_amberleaf", "Amberleaf", "Amberleaf Honey"),
    rec("c_hivenz", "HiveNZ", "Hive NZ"),
    rec("c_harbour", "Harbour FD Ltd", "Harbour Food Distributors, Dana Harbour"),
    rec("c_riverbend", "Riverbend Wine Co."),
    rec("c_harbourside", "Harbourside Craft Distillers"),
    rec("c_fieldmax", "Fieldmax Industries", "Fieldmax"),
    rec("c_brightfizz", "Bright Fizz"),
    rec("c_lakeside", "Lakeside Salmon", "Mt Aspiring Lakeside Salmon, MLS"),
    rec("c_northstar", "Northstar Beverage & Food New Zealand Limited",
        "Northstar, NBFNZ, Fructa Northstar"),
    rec("c_kb", "KB Breweries", "Kea Brewery, Kiwi Breweries"),
    rec("c_ibf", "Island Beverages (Fiji) PTE LTD",
        "IBF Fiji, IBF, Island Beverages Fiji"),
    rec("c_khnz", "Kowhai Health NZ Ltd", "KHNZ, Kowhai Health"),
    rec("c_orchardlane", "Orchard Lane Fruit Processors Ltd", "Orchard Lane"),
    rec("c_splatt_au", "Splatt Engineering Group PTY",
        "Splatt AU, Splatt Australia"),
    rec("c_internal", "Splatt Engineering (internal)",
        "Splatt Engineering, Splatt"),
    rec("c_taponera", "Taponera", "Taponera SL"),
]

SUPPLIERS = [
    rec("s_cyclone", "Cyclone Air Systems"),
    rec("s_courier", "QXP Express NZ"),
    rec("s_bx", "BX Packaging (Barrow-Whitley)"),
    rec("s_veneta", "Veneta Macchine SpA"),
    rec("s_continental", "Continental Equipment Co."),
    rec("s_unita", "Unita Technologies"),
    rec("s_tidewater", "Tidewater Logistics"),
    rec("s_taponera", "Taponera SL"),
]


@pytest.fixture
def index():
    return Index(CLIENTS, SUPPLIERS)


def test_a_full_legal_name_and_a_spoken_name_fold_together():
    assert fold("Cablecorp NZ Ltd") == fold("Cablecorp")
    assert fold("Tui Valley Waters Ltd") == "tui valley waters"
    assert fold("Island Beverages (Fiji) PTE LTD") == \
        "island beverages fiji"


def test_accents_do_not_stop_a_match():
    """A macron in the database name folds to the plain letter in a title."""
    assert fold("K\u014dwhai Health New Zealand") == "kowhai health"


def test_the_lead_stops_where_the_work_starts():
    assert lead("Northstar / Raul - Send 4 quoted belts") == "Northstar"
    assert lead("Amberleaf: Follow up on QU-8090 quote response") == "Amberleaf"
    assert lead("HiveNZ \u2014 Waiting for response") == "HiveNZ"
    assert lead("Fieldmax / Blake \u2014 Check for reply") == "Fieldmax"


def test_a_title_with_no_separator_is_all_lead():
    assert lead("pay club subscription") == "pay club subscription"


# Titles written the usual way: a client name (full, short or an alias)
# at the front, then the contact and the work.
@pytest.mark.parametrize("title, expected", [
    ("Amberleaf: Follow up on QU-8090 quote response", "c_amberleaf"),
    ("HiveNZ \u2014 Waiting for response on quote before invoicing", "c_hivenz"),
    ("Harbour FD / Dana \u2014 Chase suitability confirm", "c_harbour"),
    ("Riverbend Wine Co. / Stuart Hamlin \u2014 Second ROPP offer", "c_riverbend"),
    ("Harbourside Craft Distillers / Jess McLean \u2014 Chase ROPP", "c_harbourside"),
    ("Fieldmax / Blake \u2014 Check for reply on Tuesday call", "c_fieldmax"),
    ("Bright Fizz / Aaron \u2014 Reach out re chain work", "c_brightfizz"),
    ("Mt Aspiring Lakeside Salmon / Quinn \u2014 Follow up on ozone", "c_lakeside"),
    ("Northstar / Raul \u2014 Send 4 quoted Cyclone blower belts", "c_northstar"),
    ("KB Breweries / Darren Cole \u2014 Quote flash pasteuriser", "c_kb"),
])
def test_the_tasks_that_had_no_client_now_find_one(title, expected, index):
    found = match(title, index)

    assert found is not None, title
    assert found.record_id == expected
    assert found.kind == "client"


@pytest.mark.parametrize("title, expected", [
    ("Cyclone Air Systems / Paul Reed \u2014 Send PO for 2 carts", "s_cyclone"),
    ("Cyclone Air / Paul Reed \u2014 Answer filtration questions", "s_cyclone"),
    ("QXP / Partnerco \u2014 Pay import duty on waybill 1234567890", "s_courier"),
    ("BX Packaging / Gavin Webb \u2014 Chase reply", "s_bx"),
    ("Veneta Macchine / Enzo \u2014 Dig out contact details", "s_veneta"),
    ("Continental Equipment Co. / Jason Abbott \u2014 Check if Jason replied",
     "s_continental"),
    ("Unita / Mattia Villa \u2014 chase the 2027 budget offer", "s_unita"),
])
def test_supplier_tasks_find_the_supplier(title, expected, index):
    found = match(title, index)

    assert found is not None, title
    assert found.record_id == expected
    assert found.kind == "supplier"


def test_the_operators_own_work_is_not_filed_against_the_australian_company(index):
    """"Splatt Engineering Group PTY" is a separate company in another
    country. Titles that say "Splatt Engineering" or "Splatt" belong to the
    internal client, which only works because "Group" is not a legal
    ending that `fold` strips."""
    for title in ["Splatt Engineering \u2014 Open courier business account",
                  "Splatt Engineering / Admin \u2014 Xero clean up",
                  "Splatt / Internal \u2014 Create Excel sheet",
                  "Splatt / Admin \u2014 Load all contacts into Xero"]:
        found = match(title, index)
        assert found is not None, title
        assert found.record_id == "c_internal", title


def test_the_australian_company_still_gets_its_own_tasks(index):
    for title in ["Splatt AU / Jesse \u2014 Reach out re chain for Fiji",
                  "Splatt Australia \u2014 Make payment",
                  "Splatt Australia / Accounts \u2014 Load the next payment"]:
        found = match(title, index)
        assert found.record_id == "c_splatt_au", title


def test_a_short_form_of_a_name_at_the_front_is_accepted(index):
    """The title uses the first words of a longer name, and only one
    record starts that way, so pass 2 accepts it."""
    for title, expected in [("Cyclone Air / Paul \u2014 filtration", "s_cyclone"),
                            ("QXP / Partnerco \u2014 Pay import duty", "s_courier"),
                            ("Unita / Mattia \u2014 chase the offer", "s_unita"),
                            ("BX Packaging / Gavin \u2014 chase reply", "s_bx")]:
        found = match(title, index)
        assert found.record_id == expected, title
        assert found.how == "the front is the start of a name"


def test_a_short_form_that_could_mean_two_records_is_refused():
    index = Index([rec("a", "Clearwater Bottling (NZ) Ltd"),
                   rec("b", "Clearwater One Ltd")])

    assert match("Clearwater / Sid \u2014 chase the order", index) is None


def test_a_name_buried_in_a_sentence_is_still_found(index):
    """No separator and no name at the front: pass 3 finds the name in
    the middle of the title."""
    found = match("Chase Kowhai Health about the ozone unit", index)

    assert found.record_id == "c_khnz"
    assert found.how == "named in the title"


def test_an_unknown_short_form_is_refused_and_the_report_names_the_likely_one(index):
    """"KB" is not an alias of KB Breweries, so the title does not match.
    `suggest` points at the record the alias should be added to."""
    assert match("KB / Darren Cole \u2014 come back with AU and NZ", index) is None

    hints = suggest("KB / Darren Cole \u2014 come back with AU and NZ", index)
    assert any("KB Breweries" in h for h in hints), hints


def test_a_short_alias_does_not_match_a_random_word(index):
    """A three-letter alias such as MLS is below MIN_SCAN, so it is never
    searched for inside a sentence."""
    found = match("Review the MLSomething document", index)

    assert found is None


def test_nothing_is_returned_when_nothing_is_recognised(index):
    for title in ["pay club subscription",
                  "FEEDBACK for dev team: map loses zoom state",
                  "Capper enquiries \u2014 Review overview & decide next pitches"]:
        assert match(title, index) is None, title


def test_the_longest_name_wins_so_the_answer_never_moves(index):
    """A title naming a client and a supplier resolves to the same record
    every time."""
    a = match("Northstar / Raul \u2014 Source LMS12UU bearing (non-Cyclone)", index)
    b = match("Northstar / Raul \u2014 Source LMS12UU bearing (non-Cyclone)", index)

    assert a == b
    assert a.record_id == "c_northstar"


def test_a_name_two_records_share_is_reported_not_guessed():
    """Two client records share the alias "Hilltop". The key is flagged
    as ambiguous and a title using it matches nothing, so the duplicate
    records get merged instead of one being picked at random."""
    index = Index([rec("a", "Hill Top Foods Ltd", "Hilltop"),
                   rec("b", "Hill Top Foods Ltd.", "Hilltop")])

    assert index.ambiguous(fold("Hilltop"))
    assert match("Hilltop / Sally \u2014 chase the order", index) is None


def test_a_client_beats_a_supplier_of_the_same_name(index):
    """Taponera is both a client and a supplier. Every task needs a client
    link, so the client record wins the shared key."""
    found = match("Taponera \u2014 chase the cap order", index)

    assert found.kind == "client"
    assert found.record_id == "c_taponera"


def test_the_result_does_not_depend_on_the_order_records_arrived_in():
    forward = Index(CLIENTS, SUPPLIERS)
    backward = Index(list(reversed(CLIENTS)), list(reversed(SUPPLIERS)))

    for title in ["Northstar / Raul \u2014 belts",
                  "Cyclone Air / Paul \u2014 filtration",
                  "Call this person from Island Beverages",
                  "Splatt Engineering \u2014 Open courier account"]:
        assert match(title, forward) == match(title, backward), title


def test_an_empty_title_matches_nothing(index):
    assert match("", index) is None
    assert match(None, index) is None
