"""Fixture for Check 11, tab 3 of 3. Every marker says GAMMA."""

TAB_NAME = "TAB GAMMA"


def gamma_id():
    return TAB_NAME


def gamma_rows(count):
    return [f"{TAB_NAME} row {index}" for index in range(count)]
