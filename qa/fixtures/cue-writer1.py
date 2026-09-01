"""Writer 1's version. Large enough that char-by-char animation is watchable."""


def a():
    return 1


def collect(values):
    kept = []
    for value in values:
        if value is None:
            continue
        kept.append(value)
    return kept


def describe(values):
    kept = collect(values)
    return {"kept": len(kept), "dropped": len(values) - len(kept)}
