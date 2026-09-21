"""Fixture pair for Check 15 (catchup-blanks.py / catchup-blanks-next.py).

catchup-blanks.py is the FIRST edit -- the one that gets paused mid-animation
and whose hook is then killed, leaving a crash-fallback remainder on disk.
catchup-blanks-next.py is the SECOND edit fired afterwards; the two are
byte-identical except for report()'s returned dict, which grows a third key
whose value is the loud marker CATCHUP OK.

Long enough that a pause at `lento` always lands with real content still
untyped, and full of consecutive blank lines (PEP 8 double blanks between
top-level defs, plus one deliberate triple gap below) because those are
exactly the rows the rebuild from the persisted partial has to get right -- a
stranded or duplicated blank is the visible failure.
"""

import os


def collect(root):
    names = []
    for name in os.listdir(root):
        names.append(name.strip().lower())
    return sorted(set(names))


def widest(names):
    longest = ""
    for name in names:
        if len(name) > len(longest):
            longest = name
    return longest



def tally(names):
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return counts


def report(root):
    names = collect(root)
    return {
        "widest": widest(names),
        "tally": tally(names),
        "marker": "CATCHUP OK",
    }
