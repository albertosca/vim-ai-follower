"""Fixture pair for Check 13 (rollback-before.py / rollback-after.py).

The two files are byte-identical except for total_of's body: one `return` line
BEFORE, three lines AFTER. The edit script is therefore exactly one `replace`
op of 1 line -> 3 lines -- the shape the nvim interrupt rollback defends
(commit 6bef225): the op deletes that one line instantly, then types three in,
so an interrupt landing inside it must put the deleted line back.
"""


def total_of(values):
    return sum(value for value in values if value is not None)


def describe(values):
    return {"total": total_of(values), "count": len(values)}
