"""
Client and supplier matching: work out which client or supplier a task title is about.

Inputs
    A task title (any text) and an `Index` built from the PocketBase
    `clients` and `suppliers` collections, including each record's
    comma-separated `aliases` field.

Output
    `match()` returns a `Match` naming exactly one record, or None.
    `suggest()` returns likely records for a title that did not match, so
    the unmatched report can say which record probably needs an alias.

Rules
    1. Names are compared after folding (see `fold`). Accents, punctuation
       and legal endings such as "Ltd", "PTE LTD" or "SpA" are removed, so
       the database's "Cablecorp NZ Ltd" and a title's "Cablecorp" meet.
    2. The index is built only from PocketBase, so every client, supplier
       and alias in the database can be matched and nothing else can.
    3. Ties are refused. When two records of the same kind claim a name,
       or a title fits two records equally well, the answer is None.

Why refuse instead of guessing
    A task with no client link is visibly unfiled, and someone will file
    it. A task filed under the wrong client looks correct, so nobody goes
    back to check it. Returning nothing is the safer mistake.
"""

from __future__ import annotations

import re
import unicodedata
from collections import namedtuple

# Legal endings that are part of a company's registration but not part of
# what people call it. `fold` strips them from the end repeatedly, so
# "Island Beverages (Fiji) PTE LTD" folds to "island beverages fiji".
#
# "group" is deliberately not in the list. "Splatt Engineering Group PTY"
# is a separate Australian company, while "Splatt Engineering" is the
# internal client for the operator's own work. Stripping "group" would
# fold both to "splatt engineering" and file internal admin tasks against
# the Australian company.
SUFFIXES = (
    "limited", "ltd", "pty", "pte", "inc", "incorporated", "llc",
    "corp", "corporation", "spa", "srl", "sl", "bv", "gmbh", "ag",
    "co", "company", "nz", "new zealand",
)

# Where a task title stops naming a company and starts describing the
# work. "Northstar / Raul - Send 4 quoted belts" and
# "Amberleaf: Follow up on QU-8090" both have this shape. The two escapes
# are an em dash and an en dash surrounded by spaces.
SEPARATORS = (" / ", " \u2014 ", " \u2013 ", " - ", ": ")

# The shortest key that pass 3 of `match` looks for inside a sentence.
# Short aliases such as the three-letter "MLS" are real, but three letters
# also turn up as unrelated words and abbreviations in many titles.
MIN_SCAN = 4

# The shortest front-of-title text that pass 2 of `match` will extend to
# a longer name. The front of a title is where the company name goes, so
# a short string is trustworthy there: a three-letter courier name such as
# "QXP" is not ambiguous in that position.
MIN_LEAD = 3

# kind is "client" or "supplier", key is the folded text that matched, and
# how names the pass of `match` that found it.
Match = namedtuple("Match", "kind record_id name key how")


def fold(text):
    """Reduce a name to the part that is worth comparing.

    Steps: strip accents (so "Kōwhai" and "Kowhai" agree), lower-case,
    turn "&" into "and", replace punctuation with spaces, then remove
    legal endings from the end until none are left.

    >>> fold("Island Beverages (Fiji) PTE LTD")
    'island beverages fiji'
    >>> fold("Cablecorp NZ Ltd") == fold("Cablecorp")
    True
    """
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()

    changed = True
    while changed and text:
        changed = False
        for suffix in SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].strip()
                changed = True
    return text


def lead(text):
    """The part of a task title before the first separator.

    >>> lead("Amberleaf: Follow up on QU-8090")
    'Amberleaf'

    A title with no separator is returned whole.
    """
    text = str(text or "").strip()
    cuts = [text.find(sep) for sep in SEPARATORS if text.find(sep) > 0]
    return text[: min(cuts)].strip() if cuts else text


def aliases_of(row):
    """A record's aliases as a list.

    PocketBase stores them as one comma-separated string. A list is
    accepted as well.
    """
    raw = row.get("aliases") or ""
    if isinstance(raw, str):
        return [a.strip() for a in raw.split(",") if a.strip()]
    return [str(a).strip() for a in raw if str(a).strip()]


class Index:
    """Every folded name and alias of every client and supplier.

    `entries` maps a folded key to (kind, record id, display name).
    `collisions` keeps every key that more than one record claims, so the
    duplicate can be reported and fixed in the data (for example the
    alias "Hilltop" on two Hill Top Foods records) instead of being
    settled by whichever record happened to load first.

    A client beats a supplier for the same key. A company can be both a
    client and a supplier, and every task must carry a client link while
    the supplier link is optional, so the client is the one that cannot
    be left blank. Two records of the same kind sharing a key is a real
    ambiguity, and `ambiguous()` reports it.
    """

    def __init__(self, clients=(), suppliers=()):
        self.entries = {}
        self.collisions = {}
        for row in clients:
            self._add("client", row)
        for row in suppliers:
            self._add("supplier", row)

    def _add(self, kind, row):
        name = str(row.get("name") or "").strip()
        for text in [name] + aliases_of(row):
            key = fold(text)
            if not key:
                continue
            entry = (kind, row["id"], name)
            held = self.entries.get(key)
            if held is None:
                self.entries[key] = entry
            elif held != entry:
                # Every collision is recorded. A client replaces a supplier
                # (see the class docstring). Any other tie keeps the first
                # entry, and `ambiguous()` stops it from being returned.
                self.collisions.setdefault(key, [held]).append(entry)
                if kind == "client" and held[0] == "supplier":
                    self.entries[key] = entry

    def ambiguous(self, key):
        """True when a key is claimed by two records of the same kind."""
        held = self.collisions.get(key)
        if not held:
            return False
        kinds = {e[0] for e in held}
        return kinds != {"client", "supplier"}

    def lookup(self, key):
        return self.entries.get(key)


def _resolve(index, key, how):
    if index.ambiguous(key):
        return None
    found = index.lookup(key)
    return Match(found[0], found[1], found[2], key, how) if found else None


def match(text, index):
    """Find the client or supplier a task title is about, or None.

    Three passes, strongest first. The pass that succeeds is recorded in
    `Match.how`, so a report can show weaker matches separately.

      1. "name at the front": the text before the first separator folds
         to a known key. Most titles are written "Client / Contact - work".
      2. "the front is the start of a name": the front text (at least
         MIN_LEAD characters) is the first word or words of exactly one
         record's name, as "Cyclone Air" is for "Cyclone Air Systems".
         If two records start the same way, this pass gives up.
      3. "named in the title": a known key of at least MIN_SCAN
         characters appears as whole words anywhere in the title.

    Pass 3 takes the longest key that appears and refuses if two records
    tie at that length. Because of that, and because keys are sorted
    before one is picked, the answer does not depend on the order the
    records came out of the database.
    """
    front = fold(lead(text))
    if front:
        found = _resolve(index, front, "name at the front")
        if found:
            return found

    if len(front) >= MIN_LEAD:
        starts = [k for k in index.entries if k.startswith(front + " ")]
        ids = {index.lookup(k)[1] for k in starts}
        if len(ids) == 1:
            found = _resolve(index, sorted(starts)[0], "the front is the start of a name")
            if found:
                return found

    whole = fold(text)
    if not whole:
        return None
    padded = " %s " % whole
    hits = [key for key in index.entries
            if len(key) >= MIN_SCAN and (" %s " % key) in padded]
    if not hits:
        return None

    longest = max(len(k) for k in hits)
    best = sorted(k for k in hits if len(k) == longest)
    ids = {index.lookup(k)[1] for k in best}
    if len(ids) > 1:
        return None
    return _resolve(index, best[0], "named in the title")


def suggest(text, index, limit=3):
    """Records that share a word with the title's front, for the unmatched report.

    "KB / Darren Cole - Quote flash pasteuriser" does not match because no
    record lists "KB" as an alias of "KB Breweries". The fix is to add the
    alias to the client record, where every tool can see it, so the
    report names the likely record rather than the matcher guessing.

    Returns up to `limit` strings such as
    "client KB Breweries (as kb breweries)". Keys that start with the
    front text rank first, then keys sharing a word of at least MIN_SCAN
    letters.
    """
    front = fold(lead(text))
    if not front:
        return []
    words = set(front.split())
    scored = []
    for key, (kind, rid, name) in index.entries.items():
        if key.startswith(front):
            scored.append((0, key, kind, name))
        elif any(len(w) >= MIN_SCAN for w in words & set(key.split())):
            # Short shared words do not count. "and" is in registered
            # names such as "Northstar Beverage & Food" and in many titles,
            # and a suggestion that points at the wrong client is likely
            # to be accepted by whoever reads the report.
            scored.append((1, key, kind, name))
    seen, out = set(), []
    for _, key, kind, name in sorted(scored):
        if name in seen:
            continue
        seen.add(name)
        out.append("%s %s (as %s)" % (kind, name, key))
        if len(out) == limit:
            break
    return out
