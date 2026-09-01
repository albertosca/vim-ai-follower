"""Fixture for Check 4, window B. Every marker says ONE."""

WINDOW_NAME = "WINDOW ONE"


def beta():
    return "WINDOW ONE"


def beta_rows(count):
    rows = []
    for index in range(count):
        rows.append(f"{WINDOW_NAME} row {index}")
    return rows


def beta_summary(count):
    rows = beta_rows(count)
    return {
        "window": WINDOW_NAME,
        "first": rows[0] if rows else None,
        "total": len(rows),
    }
