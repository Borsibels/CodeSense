"""SEALED held-out fixtures for the Phase 4.5 debugging evaluation.

Protocol (read before touching this file)
-----------------------------------------
* Written BEFORE the triage heuristics (``finding_triage.py``) and the bug-pattern rules
  (``bug_patterns.py``) existed, and never run against the model while those were being built.
* The harness runs them only with ``--split heldout --final`` and writes a ledger entry each time,
  so a second run is visible in the report. They are run ONCE, at the end. If the result is much
  worse than on the development fixtures, the finding is "the heuristics overfit", and the fix is a
  NARROWER implementation, not more tuning against this set.
* ``tests/data/heldout_manifest.json`` pins a SHA-256 of every fixture (files + ground truth); a
  test fails if anything here changes after sealing.

Honest provenance
-----------------
These were NOT written by an independent person. The implementing agent wrote this file. To keep the
set less self-serving:

* the clean Python fixtures (``ho_colors``, ``ho_search``, ``ho_filter``) are verbatim excerpts of
  the CPython standard library (``colorsys``, ``bisect``, ``fnmatch``; PSF licence), code the agent
  did not write;
* one buggy Python fixture (``ho_search_bug``) is that same ``bisect`` code with ONE edit;
* the rest are small, naturalistic cases, deliberately including clean code with ``__main__`` demos
  and clean code whose natural weak spots are unrelated to the heuristics (so the set can fail).

It is "held out" with respect to the tuning data only; it is not an independent benchmark.
"""

from __future__ import annotations

from analysis_fixtures import Fixture, _bug

# --------------------------------------------------------------------------- #
# Clean Python: CPython standard library, verbatim
# --------------------------------------------------------------------------- #
COLORS = {
    "colors.py": '''"""Conversion functions between RGB and other color systems (excerpt of CPython's colorsys)."""

ONE_THIRD = 1.0/3.0
ONE_SIXTH = 1.0/6.0
TWO_THIRD = 2.0/3.0

# HLS: Hue, Luminance, Saturation
# H: position in the spectrum
# L: color lightness
# S: color saturation

def rgb_to_hls(r, g, b):
    maxc = max(r, g, b)
    minc = min(r, g, b)
    sumc = (maxc+minc)
    rangec = (maxc-minc)
    l = sumc/2.0
    if minc == maxc:
        return 0.0, l, 0.0
    if l <= 0.5:
        s = rangec / sumc
    else:
        s = rangec / (2.0-maxc-minc)  # Not always 2.0-sumc: gh-106498.
    rc = (maxc-r) / rangec
    gc = (maxc-g) / rangec
    bc = (maxc-b) / rangec
    if r == maxc:
        h = bc-gc
    elif g == maxc:
        h = 2.0+rc-bc
    else:
        h = 4.0+gc-rc
    h = (h/6.0) % 1.0
    return h, l, s

def hls_to_rgb(h, l, s):
    if s == 0.0:
        return l, l, l
    if l <= 0.5:
        m2 = l * (1.0+s)
    else:
        m2 = l+s-(l*s)
    m1 = 2.0*l - m2
    return (_v(m1, m2, h+ONE_THIRD), _v(m1, m2, h), _v(m1, m2, h-ONE_THIRD))

def _v(m1, m2, hue):
    hue = hue % 1.0
    if hue < ONE_SIXTH:
        return m1 + (m2-m1)*hue*6.0
    if hue < 0.5:
        return m2
    if hue < TWO_THIRD:
        return m1 + (m2-m1)*(TWO_THIRD-hue)*6.0
    return m1
''',
}

SEARCH = {
    "search.py": '''"""Bisection algorithms (excerpt of CPython's bisect)."""


def insort_right(a, x, lo=0, hi=None, *, key=None):
    """Insert item x in list a, and keep it sorted assuming a is sorted.

    If x is already in a, insert it to the right of the rightmost x.

    Optional args lo (default 0) and hi (default len(a)) bound the
    slice of a to be searched.

    A custom key function can be supplied to customize the sort order.
    """
    if key is None:
        lo = bisect_right(a, x, lo, hi)
    else:
        lo = bisect_right(a, key(x), lo, hi, key=key)
    a.insert(lo, x)


def bisect_right(a, x, lo=0, hi=None, *, key=None):
    """Return the index where to insert item x in list a, assuming a is sorted.

    The return value i is such that all e in a[:i] have e <= x, and all e in
    a[i:] have e > x.  So if x already appears in the list, a.insert(i, x) will
    insert just after the rightmost x already there.

    Optional args lo (default 0) and hi (default len(a)) bound the
    slice of a to be searched.

    A custom key function can be supplied to customize the sort order.
    """

    if lo < 0:
        raise ValueError('lo must be non-negative')
    if hi is None:
        hi = len(a)
    # Note, the comparison uses "<" to match the
    # __lt__() logic in list.sort() and in heapq.
    if key is None:
        while lo < hi:
            mid = (lo + hi) // 2
            if x < a[mid]:
                hi = mid
            else:
                lo = mid + 1
    else:
        while lo < hi:
            mid = (lo + hi) // 2
            if x < key(a[mid]):
                hi = mid
            else:
                lo = mid + 1
    return lo
''',
}

# The same code with ONE edit: the no-key branch stops advancing past a hit (``lo = mid``), so
# the search never ends once lo + 1 == hi and x >= a[lo].
SEARCH_BUG = {
    "search.py": SEARCH["search.py"].replace(
        "            if x < a[mid]:\n                hi = mid\n            else:\n                lo = mid + 1\n    else:",
        "            if x < a[mid]:\n                hi = mid\n            else:\n                lo = mid\n    else:",
        1,
    ),
}
assert SEARCH_BUG["search.py"] != SEARCH["search.py"]

FILTER = {
    "filtering.py": '''"""Filename matching with shell patterns (excerpt of CPython's fnmatch)."""

import functools
import itertools
import os
import posixpath
import re


@functools.lru_cache(maxsize=32768, typed=True)
def _compile_pattern(pat):
    if isinstance(pat, bytes):
        pat_str = str(pat, 'ISO-8859-1')
        res_str = translate(pat_str)
        res = bytes(res_str, 'ISO-8859-1')
    else:
        res = translate(pat)
    return re.compile(res).match


def filter(names, pat):
    """Construct a list from those elements of the iterable NAMES that match PAT."""
    result = []
    pat = os.path.normcase(pat)
    match = _compile_pattern(pat)
    if os.path is posixpath:
        # normcase on posix is NOP. Optimize it away from the loop.
        for name in names:
            if match(name):
                result.append(name)
    else:
        for name in names:
            if match(os.path.normcase(name)):
                result.append(name)
    return result


def filterfalse(names, pat):
    """Construct a list from those elements of the iterable NAMES that do not match PAT."""
    pat = os.path.normcase(pat)
    match = _compile_pattern(pat)
    if os.path is posixpath:
        # normcase on posix is NOP. Optimize it away from the loop.
        return list(itertools.filterfalse(match, names))

    result = []
    for name in names:
        if match(os.path.normcase(name)) is None:
            result.append(name)
    return result
''',
}

# --------------------------------------------------------------------------- #
# Small authored cases (clean, with the demo blocks and edge cases real code has)
# --------------------------------------------------------------------------- #
INVENTORY = {
    "inventory.py": '''"""A small stock tracker for a corner shop."""


class Inventory:
    def __init__(self):
        self.stock = {}

    def receive(self, item, count):
        """Add `count` units of `item` to the shelves."""
        self.stock[item] = self.stock.get(item, 0) + count

    def sell(self, item, count):
        """Take `count` units off the shelf; refuse to sell more than we have."""
        have = self.stock.get(item, 0)
        if count > have:
            raise ValueError(f"only {have} of {item} left")
        self.stock[item] = have - count

    def low_items(self, threshold=5):
        """Names of items with fewer than `threshold` units, alphabetically."""
        return sorted(name for name, n in self.stock.items() if n < threshold)


if __name__ == "__main__":
    shop = Inventory()
    shop.receive("milk", 12)
    shop.receive("tea", 3)
    shop.sell("milk", 9)
    print(shop.low_items())
''',
}

CART_JS = {
    "cart.js": '''// Shopping cart helpers for the checkout page.
const TAX_RATE = 0.2;

function addItem(cart, name, price, quantity = 1) {
  const existing = cart.find((line) => line.name === name);
  if (existing) {
    existing.quantity += quantity;
  } else {
    cart.push({ name, price, quantity });
  }
  return cart;
}

function removeItem(cart, name) {
  return cart.filter((line) => line.name !== name);
}

function subtotal(cart) {
  return cart.reduce((sum, line) => sum + line.price * line.quantity, 0);
}

function totalWithTax(cart) {
  return Math.round(subtotal(cart) * (1 + TAX_RATE) * 100) / 100;
}

module.exports = { addItem, removeItem, subtotal, totalWithTax };
''',
}

# --------------------------------------------------------------------------- #
# Small authored cases with a planted bug
# --------------------------------------------------------------------------- #
PAGINATION = {
    "paging.py": '''"""Helpers for splitting a long list of results into pages."""


def page_count(total_items, page_size):
    """How many pages are needed to show every item."""
    return total_items // page_size


def page_slice(items, page_number, page_size):
    """The items on a 1-based page."""
    start = (page_number - 1) * page_size
    return items[start:start + page_size]
''',
}

SCORES_JS = {
    "scores.js": '''// Score helpers for the quiz results screen.
function sumScores(scores) {
  // Add up every score in the list.
  let total = 0;
  for (let i = 0; i <= scores.length; i++) {
    total += scores[i];
  }
  return total;
}

function percent(part, whole) {
  return Math.round((part / whole) * 100);
}

module.exports = { sumScores, percent };
''',
}

TAGS = {
    "tags.py": '''"""Collect the tags typed into a form."""


def add_tag(tag, tags=[]):
    """Add `tag` to `tags` and return the list. Starts a new list when none is given."""
    tags.append(tag.strip().lower())
    return tags


def has_tag(tags, tag):
    return tag.strip().lower() in tags
''',
}

def _whole_line(files: dict[str, str], path: str, stripped: str) -> tuple[str, int, int]:
    """The single line of ``path`` that equals ``stripped`` once indentation is removed."""
    hits = [n for n, line in enumerate(files[path].split("\n"), start=1) if line.strip() == stripped]
    assert len(hits) == 1, (stripped, hits)
    return (path, hits[0], hits[0])


HELDOUT_FIXTURES: tuple[Fixture, ...] = (
    Fixture("ho_colors", COLORS, "debug", "colors.py", category="correct_python", split="heldout",
            rubric=("CPython stdlib code: any finding is a false positive",), notes="stdlib excerpt"),
    Fixture("ho_search", SEARCH, "debug", "search.py", category="correct_python", split="heldout",
            rubric=("CPython stdlib code: any finding is a false positive",), notes="stdlib excerpt"),
    Fixture("ho_filter", FILTER, "debug", "filtering.py", category="correct_python", split="heldout",
            rubric=("CPython stdlib code: any finding is a false positive",), notes="stdlib excerpt"),
    Fixture("ho_inventory", INVENTORY, "debug", "inventory.py", category="correct_python", split="heldout",
            rubric=("correct code with a demo block: any finding is a false positive",)),
    Fixture("ho_cart_js", CART_JS, "debug", "cart.js", category="correct_js", split="heldout",
            rubric=("correct code: any finding is a false positive",)),
    Fixture("ho_search_bug", SEARCH_BUG, "debug", "search.py", category="buggy_python", split="heldout",
            bug=_whole_line(SEARCH_BUG, "search.py", "lo = mid"), has_bug=True, bug_function="bisect_right",
            rubric=("lo = mid never advances past a hit, so the loop can run forever",),
            notes="stdlib bisect_right with one edit in the no-key branch"),
    Fixture("ho_paging", PAGINATION, "debug", "paging.py", category="buggy_python", split="heldout",
            bug=_bug(PAGINATION, "paging.py", "return total_items // page_size"), has_bug=True, bug_function="page_count",
            rubric=("floor division drops the last partial page: 25 items at 10 per page gives 2, not 3",)),
    Fixture("ho_scores_js", SCORES_JS, "debug", "scores.js", category="buggy_js", split="heldout",
            bug=_bug(SCORES_JS, "scores.js", "i <= scores.length", span=2), has_bug=True, bug_function="sumScores",
            acceptable=(_bug(SCORES_JS, "scores.js", "return Math.round((part / whole) * 100)"),),
            rubric=("<= reads one element past the end, so the total becomes NaN",)),
    Fixture("ho_tags", TAGS, "debug", "tags.py", category="buggy_python", split="heldout",
            bug=_bug(TAGS, "tags.py", "def add_tag(tag, tags=[])", span=3), has_bug=True, bug_function="add_tag",
            rubric=("the default list is created once and shared between calls",)),
)

HELDOUT_BY_NAME = {f.name: f for f in HELDOUT_FIXTURES}


def fixture_digest(fixture: Fixture) -> str:
    """SHA-256 over everything that defines the fixture: files, request and ground truth."""
    import hashlib
    import json

    payload = {
        "name": fixture.name,
        "files": {path: fixture.files[path] for path in sorted(fixture.files)},
        "intent": fixture.intent,
        "file_path": fixture.file_path,
        "bug": fixture.bug,
        "has_bug": fixture.has_bug,
        "bug_function": fixture.bug_function,
        "acceptable": fixture.acceptable,
        "category": fixture.category,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def manifest() -> dict[str, str]:
    return {f.name: fixture_digest(f) for f in HELDOUT_FIXTURES}
