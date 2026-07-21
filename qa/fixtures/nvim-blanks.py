"""Fixture for the nvim des-interrupt smoke (Check 6).

Has PEP 8 double blank lines between top-level defs on purpose: interrupting
the retype around one of those gaps, then des-interrupting, exercises the
consecutive-trailing-blank-line replay fix (commit 27aa0e4).
"""

import os


def first(paths):
    collected = []
    for name in os.listdir(paths):
        collected.append(name.strip().lower())
    return sorted(set(collected))


def second(values):
    total = 0
    for value in values:
        total += len(value)
    return total


def third(items):
    return [item for item in items if item]
