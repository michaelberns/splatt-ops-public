"""
Project matching: work out which project (job) a task belongs to, within one client.

Inputs
    A task title, the client id already chosen for the task (by
    core/matching.py or by hand), and a `JobIndex` built from the
    PocketBase `jobs` and `clients` collections.

Output
    `JobIndex.match()` returns `(JobMatch, None)` when one project is
    clearly the subject of the title, or `(None, Unplaced)` with a reason
    code and the candidates that were considered. Exactly one half is
    filled, so a caller cannot mistake a refusal for a pass.

How it decides
    1. Only the client's own live projects are candidates. A project that
       is completed, cancelled, paid or archived is never offered.
    2. Each project title is reduced to the words that identify it. The
       client's name and the contact's name come off the front, because
       every task for that client and contact starts with the same words.
       "Bright Fizz / Aaron Gale, Rialto April Shipment" keeps only
       "rialto april shipment".
    3. What is left becomes three kinds of evidence:
         phrases: runs of two or more adjacent words, scored by length,
                  so "cable drying system" beats "drying system";
         codes:   tokens carrying a digit, such as "150s" or a quote
                  number like "8090", worth CODE_SCORE;
         lone words: single uncommon words of MIN_LONE_WORD letters or
                  more, such as "microdoser", worth LONE_WORD_SCORE.
    4. The best project must reach MIN_SCORE and must beat the runner-up
       outright. A tie is reported as AMBIGUOUS with both names.

Refusal reasons (the values the validator reads)
    NO_CLIENT        the task has no client, so there is nothing to choose from
    NO_LIVE_PROJECT  the client has no live project; a project record is missing
    NO_EVIDENCE      nothing in the title names any of the client's projects
    AMBIGUOUS        two projects score the same

Why "the client has only one project" is not used as a rule
    It would link many more tasks, but it is often wrong: a client whose
    one live project is a freight recharge would get every task filed
    under it, including a gripper quote for an unrelated supplier. The
    same standard as client matching applies. An unfiled task is visibly
    unfiled, while a wrongly filed one looks correct and is never checked.

The module has no network access. Given the same task and the same
projects it always returns the same answer.
"""

from __future__ import annotations

import re
from collections import namedtuple

from core.matching import SEPARATORS, aliases_of, fold, lead

# Project statuses that mean the work is over. An open task filed under a
# finished project would disappear from every view of live work, so these
# projects are never offered as candidates.
CLOSED_STATUSES = frozenset({"completed", "cancelled", "paid"})

# Words that carry no identity. Many projects are a "quote" or a
# "service", so a phrase built only from these words says nothing about
# which project is meant. A phrase must contain at least one word that is
# not in this set.
GENERIC = frozenset({
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "with", "from", "by", "re", "new", "old",
    "quote", "quotes", "quoted", "quoting", "quotation",
    "job", "project", "work", "works", "order",
    "check", "enquiry", "enquiries", "inquiry", "request",
    "parts", "part", "spare", "spares", "item", "items",
    "service", "servicing", "repair", "response", "pending",
    "options", "option", "replacement", "replace", "supply",
    "client", "customer", "account", "general", "misc", "other",
})

# The shortest word that can carry a phrase. A phrase made only of words
# shorter than this (and not codes) is ignored, because such short words
# turn up inside unrelated sentences.
MIN_WORD = 3

# The score a project needs before a task is linked to it. A two-word
# phrase reaches it, and so does one code. A single ordinary word never
# does: "ropp" appearing in both a task and a project title is a hint,
# not evidence.
MIN_SCORE = 2

# The score of one shared code. Level with a two-word phrase, because a
# part or quote number in a title is at least as strong a signal as two
# adjacent words.
CODE_SCORE = 2

# An all-digit token has to be at least this long to count as a code.
# Three digits covers quote and invoice numbers and excludes the counts,
# quantities and model-number fragments that appear in many titles.
MIN_CODE_DIGITS = 3

# A single word counts as evidence on its own once it is this long and is
# not in GENERIC. Words such as "pasteuriser" or "microdoser" name the
# machine, so a task mentioning one for a client with a project named
# after it is about that project. Nine letters excludes common workshop
# words like "capper", "gripper", "filling" and "shipment", which appear
# in unrelated work for the same client. Worth the same as a code, so one
# lone word reaches MIN_SCORE by itself.
MIN_LONE_WORD = 9
LONE_WORD_SCORE = 2

# A contact name is at most this many words. A longer run before the
# comma is a description rather than a person.
MAX_NAME_WORDS = 3

# score is the total from `score()`; how is a readable note of the
# evidence ("the title says 'cable drying system'").
JobMatch = namedtuple("JobMatch", "job_id title score how")

# Why a task was not linked. The validator reads these codes, so they are
# short stable values, and UNPLACED_REASONS holds the matching sentence
# for reports.
NO_CLIENT = "no_client"
NO_LIVE_PROJECT = "no_live_project"
NO_EVIDENCE = "no_evidence"
AMBIGUOUS = "ambiguous"

UNPLACED_REASONS = {
    NO_CLIENT: "the task has no client, so there is nothing to choose between",
    NO_LIVE_PROJECT: "the client has no live project to attach this to",
    NO_EVIDENCE: "nothing in the title names any of the client's projects",
    AMBIGUOUS: "two projects match this equally well",
}

Unplaced = namedtuple("Unplaced", "reason candidates")


def is_live(job):
    """A project that is still somewhere a task can be filed."""
    if job.get("archived"):
        return False
    return str(job.get("status") or "").strip().lower() not in CLOSED_STATUSES


def _words(text):
    """Folded text as a word list, with digit runs kept separate.

    `fold` turns "Vela 12-12-1" into "vela 12 12 1", so a task written
    "Vela 12/12/1" meets a project written "Vela 12-12-1". They name the
    same machine, and only the punctuation differs.
    """
    return [w for w in fold(text).split() if w]


def _strip_client(words, client_words):
    """Drop the client's own name from a title's word list.

    Only from the front, and only while words keep matching. At the front
    the client name is a heading. Elsewhere it can be part of what makes
    a title distinctive, as in "Island Beverages line 3 cooler", so it is
    left alone there.
    """
    out = list(words)
    while out and out[0] in client_words:
        out.pop(0)
    return out


def _contact_words(text):
    """The person named in "Client / Contact, the actual work".

    Project titles usually name the contact before the job, and the
    contact is a heading in the same way the client name is.
    "Bright Fizz / Aaron Gale, Rialto April Shipment" shares "aaron gale"
    with every other task Aaron is on, so leaving the name in would file
    unrelated errands under the shipment.

    Only a short run (up to MAX_NAME_WORDS) of capitalised alphabetic
    words between the separator and a comma counts as a name. A
    title-cased description such as "Bright Fizz / Cable Drying System,
    quote" is stripped too, which loses real evidence and costs a link.
    That is the safe direction to be wrong in: an unfiled task is visible
    and a misfiled one is not.
    """
    text = str(text or "")
    head, sep, rest = text.partition(",")
    if not sep or not rest.strip():
        return set()

    cuts = [(head.find(s), s) for s in SEPARATORS if head.find(s) > 0]
    if cuts:
        at, sep_text = min(cuts)
        head = head[at + len(sep_text):]

    parts = head.split()
    if not parts or len(parts) > MAX_NAME_WORDS:
        return set()
    if not all(p.isalpha() and p[:1].isupper() for p in parts):
        return set()
    return set(_words(head))


def _is_code(word):
    """A token that is a part, model or quote number rather than a word.

    It must contain a digit, and then either contain a letter too (as
    "150s" and "2ks" do) or be at least MIN_CODE_DIGITS long (as "8090"
    and "41370" are). "Vela 12-12-1" folds to tokens including "12" and
    "1", and counting those would link any task that mentions a quantity.

    >>> _is_code("150s"), _is_code("8090"), _is_code("12")
    (True, True, False)
    """
    if not any(c.isdigit() for c in word):
        return False
    if any(c.isalpha() for c in word):
        return True
    return len(word) >= MIN_CODE_DIGITS


def signature(job, client_words=()):
    """The phrases, codes and long words that identify one project.

    Returns (phrases, codes, lone), all built after the client and
    contact heading has been removed. A phrase is a tuple of two or more
    adjacent words containing at least one word outside GENERIC. A code
    is a part or reference number (see `_is_code`). A lone word is a
    single word long enough to stand as evidence by itself.
    """
    title = job.get("title") or ""
    words = _words(title)
    # Many project titles look like "Client / Contact, what the job is".
    # Cut at the same separators a task title is cut at, so the heading
    # half goes and the identifying half stays.
    trimmed = _words(lead(title))
    if trimmed and len(trimmed) < len(words) and set(trimmed) <= set(client_words):
        words = words[len(trimmed):]
    heading = set(client_words) | _contact_words(title)
    words = _strip_client(words, heading)

    codes = {w for w in words if _is_code(w)}
    lone = {w for w in words
            if len(w) >= MIN_LONE_WORD and w not in GENERIC and not _is_code(w)}

    phrases = set()
    for size in range(2, len(words) + 1):
        for start in range(0, len(words) - size + 1):
            run = tuple(words[start:start + size])
            if all(len(w) < MIN_WORD and not _is_code(w) for w in run):
                continue
            if all(w in GENERIC for w in run):
                continue
            phrases.add(run)
    return phrases, codes, lone


def score(task_words, phrases, codes, lone=()):
    """What one project's evidence is worth against one task.

    Returns (total, how). The total is the length of the longest phrase
    found, plus CODE_SCORE per shared code, plus LONE_WORD_SCORE per
    shared lone word.

    Only the longest phrase counts, not the sum of all phrases, because
    "cable drying system" also contains "cable drying" and adding both
    would reward a long title twice for saying one thing. For the same
    reason a code or lone word only counts when it is not already inside
    a matched phrase: "microdoser 150s" scores 2 for the phrase, not 2
    plus 2 for "150s" plus 2 for "microdoser".
    """
    hay = " " + " ".join(task_words) + " "
    best_phrase = 0
    hits = []
    for run in phrases:
        if (" " + " ".join(run) + " ") in hay:
            if len(run) > best_phrase:
                best_phrase = len(run)
            hits.append(" ".join(run))

    covered = set()
    for text in hits:
        covered.update(text.split())

    seen = set(task_words)
    found_codes = sorted(c for c in codes if c in seen and c not in covered)
    found_lone = sorted(w for w in lone if w in seen and w not in covered)
    total = (best_phrase
             + CODE_SCORE * len(found_codes)
             + LONE_WORD_SCORE * len(found_lone))

    how = []
    if hits:
        how.append("the title says '%s'" % max(hits, key=len))
    if found_codes:
        how.append("shares the code%s %s"
                   % ("" if len(found_codes) == 1 else "s", ", ".join(found_codes)))
    if found_lone:
        how.append("both name %s" % ", ".join(found_lone))
    return total, ", ".join(how)


class JobIndex:
    """Every live project, grouped by client, with its evidence prepared.

    Built once per pass. Reducing each project title to its signature is
    done here, once, rather than again for every task compared with it.
    Projects that are not live, or have no client, are left out.
    """

    def __init__(self, jobs=(), clients=()):
        self.by_client = {}
        self._titles = {}
        names = {}
        for row in clients or ():
            words = set()
            for text in [row.get("name") or ""] + aliases_of(row):
                words.update(_words(text))
            names[row.get("id")] = words

        for job in jobs or ():
            if not is_live(job):
                continue
            client_id = str(job.get("client") or "").strip()
            if not client_id:
                # A task is only ever offered its own client's projects,
                # so a project with no client can never be chosen. It is
                # left out rather than matched loosely.
                continue
            phrases, codes, lone = signature(job, names.get(client_id, set()))
            self.by_client.setdefault(client_id, []).append(
                (job["id"], job.get("title") or "", phrases, codes, lone))
            self._titles[job["id"]] = job.get("title") or ""

    def candidates(self, client_id):
        return self.by_client.get(str(client_id or "").strip(), [])

    def title(self, job_id):
        return self._titles.get(job_id, "")

    def match(self, content, client_id):
        """The project a task belongs to, or why it could not be decided.

        Returns (JobMatch, None) or (None, Unplaced), never both and never
        neither, so a caller always has either a link or a reason. Ties
        are not broken: two projects with the same best score give
        AMBIGUOUS, with both titles as candidates.
        """
        if not str(client_id or "").strip():
            return None, Unplaced(NO_CLIENT, [])

        pool = self.candidates(client_id)
        if not pool:
            return None, Unplaced(NO_LIVE_PROJECT, [])

        task_words = _words(content)
        scored = []
        for job_id, title, phrases, codes, lone in pool:
            value, how = score(task_words, phrases, codes, lone)
            if value >= MIN_SCORE:
                scored.append((value, job_id, title, how))

        if not scored:
            return None, Unplaced(NO_EVIDENCE, [row[1] for row in pool])

        scored.sort(key=lambda row: (-row[0], row[2]))
        best = scored[0]
        if len(scored) > 1 and scored[1][0] == best[0]:
            tied = [row[2] for row in scored if row[0] == best[0]]
            return None, Unplaced(AMBIGUOUS, tied)

        return JobMatch(best[1], best[2], best[0], best[3]), None


def explain(unplaced):
    """The reason in words, with the candidates that were considered."""
    text = UNPLACED_REASONS.get(unplaced.reason, unplaced.reason)
    if unplaced.candidates:
        text += " (%s)" % "; ".join(unplaced.candidates[:4])
    return text
