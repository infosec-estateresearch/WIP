"""WIP normaliser tests — must match ERic's behaviour close enough for
canonical resolution to work across both repos.
"""

import pytest

from src.wip_app.normaliser import duplicate_group_key, normalise_company_name


@pytest.mark.parametrize("raw,expected", [
    ("Hugh James Solicitors",   "hugh james"),
    ("Knights plc",              "knights"),
    ("Knights Solicitors LLP",   "knights"),
    ("Hugh James LLP",           "hugh james"),
    ("Charles Newton & Co",      "charles newton"),
    ("WBW Solicitors Ltd",       "wbw"),
    ("Pinsent Masons LLP",       "pinsent masons"),
    ("  multiple   spaces  ",    "multiple spaces"),
    ("", ""),
    (None, ""),
])
def test_normalise_company_name(raw, expected):
    assert normalise_company_name(raw) == expected


def test_duplicate_group_key_collapses_suffix_variants():
    """The whole point of the SF duplicate detector — three SF Accounts named
    'Hugh James' / 'Hugh James Solicitors' / 'Hugh James LLP' must yield the
    same key so the detector can flag them.
    """
    keys = {
        duplicate_group_key("Hugh James"),
        duplicate_group_key("Hugh James Solicitors"),
        duplicate_group_key("Hugh James LLP"),
        duplicate_group_key("HUGH JAMES Limited"),
    }
    assert keys == {"hugh james"}
