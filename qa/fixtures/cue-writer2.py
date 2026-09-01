"""Writer 2's version: writer 1's file plus b() and a summary helper."""


def a():
    return 1


def b():
    return 2


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


def summarize(values):
    report = describe(values)
    report["ratio"] = report["kept"] / len(values) if values else 0.0
    return report
