"""A stored identifier is read past its citation prefix, and one value may
hold several.

Both are shapes the deployment cannot express in `identifier_pattern`, because
the pattern describes what an identifier looks like and these are about how a
value is written around one. Measured against the live corpus: 1,203 stored
values carry a citation prefix and 963 hold more than one identifier, and no
pattern reads any of them today.
"""

from __future__ import annotations

from app.services.claim_naming import _identifier_values, identifier_key

EU = r"\b([CTF])\s?[-‐-―−]\s?0*(\d{1,4})/(\d{2})\b"
US_DOCKET = r"\b(\d{2})-(\d{3,5})\b"


def test_a_bare_identifier_still_keys():
    assert identifier_key("C-606/18", EU) is not None


def test_the_anchor_still_refuses_an_identifier_buried_in_prose():
    """The guarantee `identifier_key` documents: a value has to BE an
    identifier, not merely contain something shaped like one."""
    assert identifier_key(
        "an appeal was lodged in 85-1475 last year", US_DOCKET) is None


def test_one_value_holding_two_identifiers_yields_both():
    assert _identifier_values("97-2314, 97-2315") == ["97-2314", "97-2315"]
    assert _identifier_values("18182; 18183") == ["18182", "18183"]


def test_a_single_value_is_unchanged():
    assert _identifier_values("C-606/18 P") == ["C-606/18 P"]
    assert _identifier_values("") == []


def test_a_json_list_still_works():
    assert _identifier_values('["C-792/21 P", "C-793/21 P"]') == [
        "C-792/21 P", "C-793/21 P"]


def test_splitting_reaches_the_second_identifier_of_a_value_that_already_keys():
    """The one stored value that matches today and carries a separator keys on
    its first identifier only; the second was never compared against anything.
    """
    pat = r"\b(?:COMP/)?(M|AT|SA)\.0*(\d{1,5})\b"
    raw = "SA.52162 (2019/C), SA.52617 (2019/C)"
    keys = [identifier_key(v, pat) for v in _identifier_values(raw)]
    assert all(keys) and len(set(keys)) == 2, keys
