"""Fixture for Check 11, tab 1 of 3. Every marker says ALPHA."""

TAB_NAME = "TAB ALPHA"


def alpha_id():
    return TAB_NAME


def alpha_rows(count):
    return [f"{TAB_NAME} row {index}" for index in range(count)]
