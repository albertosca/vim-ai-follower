"""Fixture for Check 4, window A. Every marker says ZERO."""

WINDOW_NAME = "WINDOW ZERO"


def alpha():
    return "WINDOW ZERO"


def alpha_rows(count):
    rows = []
    for index in range(count):
        rows.append(f"{WINDOW_NAME} row {index}")
    return rows


def alpha_summary(count):
    rows = alpha_rows(count)
    return {
        "window": WINDOW_NAME,
        "first": rows[0] if rows else None,
        "total": len(rows),
    }
