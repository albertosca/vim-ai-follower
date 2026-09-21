"""Fixture for Check 11, tab 2 of 3. Every marker says BETA."""

TAB_NAME = "TAB BETA"


def beta_id():
    return TAB_NAME


def beta_rows(count):
    return [f"{TAB_NAME} row {index}" for index in range(count)]
