"""
Tests for choosing a project within one client (core/jobmatch.py).

The first group pins down the refusal of the shortcut "the client has one
live project, so use it": a client whose only live project is a freight
recharge must not have an unrelated gripper quote filed under it. The
rest pin down each threshold in the module (MIN_SCORE, MIN_LONE_WORD,
MIN_CODE_DIGITS, the heading strip) with a title that shows why it is
there, and the four refusal reasons.
"""

from __future__ import annotations

from core import jobmatch

CLIENTS = [
    {"id": "c_brightfizz", "name": "Bright Fizz Kombucha", "aliases": "Bright Fizz"},
    {"id": "c_nectar", "name": "Nectar Doctor", "aliases": ""},
    {"id": "c_northstar", "name": "Northstar Beverage & Food New Zealand Limited",
     "aliases": "Northstar"},
]


def job(jid, title, client="c_brightfizz", **kw):
    base = {"id": jid, "title": title, "client": client, "status": "active",
            "archived": False}
    base.update(kw)
    return base


def index(*jobs):
    return jobmatch.JobIndex(list(jobs), CLIENTS)


# ---------------------------------------------------------------------------
# One live project is not a reason to file there
# ---------------------------------------------------------------------------

def test_a_client_with_one_live_project_is_not_reason_enough_to_file_there():
    """The client's only live project is a freight recharge for a Rialto
    shipment. A task about a gripper quote from another supplier shares no
    evidence with it, so it is left unplaced with NO_EVIDENCE."""
    idx = index(job("j_rialto", "Bright Fizz / Aaron Gale, Rialto April Shipment"))

    found, unplaced = idx.match(
        "Polymer Developments / Joel, chase gripper quote", "c_brightfizz")

    assert found is None
    assert unplaced.reason == jobmatch.NO_EVIDENCE


def test_the_clients_own_name_in_a_project_title_is_not_evidence():
    """Every task and project for a client starts with the client's name,
    so those words are stripped before scoring."""
    idx = index(job("j_rialto", "Bright Fizz / Rialto April Shipment"))

    found, _ = idx.match("Bright Fizz / unrelated errand", "c_brightfizz")

    assert found is None


def test_the_contact_name_is_heading_too_and_is_not_evidence():
    """"Aaron Gale" appears in every project and task Aaron is involved
    in, so a shared contact name alone must not place a task."""
    idx = index(job("j_rialto", "Bright Fizz / Aaron Gale, Rialto April Shipment"))

    found, unplaced = idx.match("Bright Fizz / Aaron Gale, unrelated errand",
                                "c_brightfizz")

    assert found is None
    assert unplaced.reason == jobmatch.NO_EVIDENCE


def test_stripping_the_contact_leaves_the_work_behind_it_intact():
    idx = index(job("j_rialto", "Bright Fizz / Aaron Gale, Rialto April Shipment"))

    found, _ = idx.match("Bright Fizz / Aaron Gale, Rialto April Shipment freight",
                         "c_brightfizz")

    assert found is not None
    assert found.job_id == "j_rialto"


# ---------------------------------------------------------------------------
# What does count as evidence
# ---------------------------------------------------------------------------

def test_a_phrase_from_the_project_title_places_the_task():
    idx = index(job("j_cable", "Bright Fizz / Aaron, cable drying system install"))

    found, unplaced = idx.match("Bright Fizz / Aaron, cable drying system quote",
                                "c_brightfizz")

    assert unplaced is None
    assert found.job_id == "j_cable"
    assert "cable drying" in found.how


def test_a_shared_part_number_is_enough_on_its_own():
    """A model number such as "150s" is worth CODE_SCORE, which reaches
    MIN_SCORE by itself."""
    idx = index(job("j_md", "Bright Fizz / Aaron, Microdoser 150s service"))

    found, _ = idx.match("Bright Fizz / order the 150s seal kit", "c_brightfizz")

    assert found is not None
    assert found.job_id == "j_md"


def test_one_long_distinctive_word_is_enough():
    idx = index(job("j_past", "Bright Fizz / Aaron, pasteuriser rebuild"))

    found, _ = idx.match("Bright Fizz / book the pasteuriser in", "c_brightfizz")

    assert found is not None


def test_one_ordinary_word_is_not_enough():
    """Words shorter than MIN_LONE_WORD, such as "capper", "gripper",
    "shipment" and "filling", appear in unrelated work for the same client,
    so one of them alone is not evidence."""
    idx = index(job("j_cap", "Bright Fizz / Aaron, capper conversion"))

    found, unplaced = idx.match("Bright Fizz / capper is making a noise", "c_brightfizz")

    assert found is None
    assert unplaced.reason == jobmatch.NO_EVIDENCE


def test_punctuation_between_a_model_number_does_not_matter():
    """"Vela 12/12/1" in a task and "Vela 12-12-1" in a project fold to
    the same tokens."""
    idx = index(job("j_vela", "Bright Fizz / Aaron, Vela 12-12-1 filler service"))

    found, _ = idx.match("Bright Fizz / Vela 12/12/1 filler valve seats", "c_brightfizz")

    assert found is not None
    assert found.job_id == "j_vela"


def test_a_stray_digit_from_a_model_number_is_not_a_code():
    """"Vela 12-12-1" folds to tokens including "12" and "1". They are
    shorter than MIN_CODE_DIGITS, so a task that mentions a quantity of 12
    does not match."""
    idx = index(job("j_vela", "Bright Fizz / Aaron, Vela 12-12-1 filler service"))

    found, unplaced = idx.match("Bright Fizz / order 12 gaskets", "c_brightfizz")

    assert found is None
    assert unplaced.reason == jobmatch.NO_EVIDENCE


def test_a_word_inside_a_matched_phrase_is_not_counted_twice():
    """"microdoser 150s" scores 2 for the phrase, not extra for the code
    and the lone word inside it."""
    idx = index(job("j_md", "Bright Fizz / Aaron, Microdoser 150s service"))

    found, _ = idx.match("Bright Fizz / Microdoser 150s seals", "c_brightfizz")

    assert found.score == 2


# ---------------------------------------------------------------------------
# When it refuses, and why it says so
# ---------------------------------------------------------------------------

def test_two_projects_matching_equally_well_are_named_and_neither_is_linked():
    idx = index(job("j_a", "Bright Fizz / Aaron, cable drying system north line"),
                job("j_b", "Bright Fizz / Aaron, cable drying system south line"))

    found, unplaced = idx.match("Bright Fizz / cable drying system spares", "c_brightfizz")

    assert found is None
    assert unplaced.reason == jobmatch.AMBIGUOUS
    assert len(unplaced.candidates) == 2


def test_a_task_with_no_client_has_nothing_to_choose_between():
    idx = index(job("j_rialto", "Bright Fizz / Aaron, Rialto April Shipment"))

    found, unplaced = idx.match("Rialto April Shipment freight", "")

    assert found is None
    assert unplaced.reason == jobmatch.NO_CLIENT


def test_a_client_with_no_live_project_says_so_rather_than_no_evidence():
    """The two reasons need different fixes: NO_LIVE_PROJECT means a
    project record is missing, NO_EVIDENCE means the title needs to name
    the project."""
    idx = index(job("j_done", "Bright Fizz / Aaron, Rialto April Shipment",
                    status="paid"))

    found, unplaced = idx.match("Bright Fizz / Rialto April Shipment freight",
                                "c_brightfizz")

    assert found is None
    assert unplaced.reason == jobmatch.NO_LIVE_PROJECT


def test_a_finished_project_is_never_offered():
    for status in ("completed", "cancelled", "paid"):
        assert not jobmatch.is_live({"status": status, "archived": False})
    assert not jobmatch.is_live({"status": "active", "archived": True})
    assert jobmatch.is_live({"status": "active", "archived": False})


def test_another_clients_project_is_never_offered():
    idx = index(job("j_rialto", "Bright Fizz / Aaron, Rialto April Shipment"))

    found, unplaced = idx.match("Nectar Doctor / Rialto April Shipment", "c_nectar")

    assert found is None
    assert unplaced.reason == jobmatch.NO_LIVE_PROJECT


def test_a_project_with_no_client_is_left_out_entirely():
    idx = index(job("j_loose", "Cable drying system", client=""))

    assert idx.candidates("c_brightfizz") == []


def test_generic_words_alone_never_place_a_task():
    """A phrase made only of GENERIC words, such as "service quote", is
    not evidence."""
    idx = index(job("j_q", "Bright Fizz / Aaron, service quote"))

    found, _ = idx.match("Bright Fizz / send the service quote", "c_brightfizz")

    assert found is None


# ---------------------------------------------------------------------------
# Deterministic answers
# ---------------------------------------------------------------------------

def test_the_same_task_gets_the_same_answer_whatever_order_projects_arrive_in():
    jobs = [job("j_a", "Bright Fizz / Aaron, cable drying system install"),
            job("j_b", "Bright Fizz / Aaron, Microdoser 150s service"),
            job("j_c", "Bright Fizz / Aaron, Rialto April Shipment")]
    content = "Bright Fizz / cable drying system spares"

    first, _ = jobmatch.JobIndex(jobs, CLIENTS).match(content, "c_brightfizz")
    second, _ = jobmatch.JobIndex(list(reversed(jobs)), CLIENTS).match(
        content, "c_brightfizz")

    assert first.job_id == second.job_id == "j_a"


def test_a_longer_phrase_beats_a_shorter_one():
    idx = index(job("j_short", "Bright Fizz / Aaron, drying system check"),
                job("j_long", "Bright Fizz / Aaron, cable drying system install"))

    found, _ = idx.match("Bright Fizz / cable drying system spares", "c_brightfizz")

    assert found.job_id == "j_long"


def test_the_reason_reads_as_a_sentence_with_the_candidates_named():
    idx = index(job("j_rialto", "Bright Fizz / Aaron, Rialto April Shipment"))

    _, unplaced = idx.match("Bright Fizz / unrelated errand", "c_brightfizz")
    text = jobmatch.explain(unplaced)

    assert "names any of the client's projects" in text
    assert "Rialto April Shipment" in text
