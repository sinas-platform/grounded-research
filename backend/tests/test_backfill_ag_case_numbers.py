"""Unit tests for deriving a court case number from a CELEX filename.

Pure core only: the derivation takes a filename and returns a case number, so
no DB. The backfill around it is run with --dry-run against the real corpus
before it is allowed to write.

Run from the backend directory:
`python -m pytest tests/test_backfill_ag_case_numbers.py`
"""

from scripts.backfill_ag_case_numbers import case_number_from_celex


def test_an_advocate_general_opinion_gives_its_case():
    assert case_number_from_celex("62020CC0117.md") == "C-117/20"
    assert case_number_from_celex("62020CC0151.md") == "C-151/20"


def test_a_court_of_justice_judgment_gives_its_case():
    assert case_number_from_celex("62018CJ0601.md") == "C-601/18"


def test_a_general_court_judgment_takes_the_general_court_letter():
    """The court letter is the half a case number cannot be read without:
    T-449/14 and C-449/14 are different cases."""
    assert case_number_from_celex("62014TJ0449.md") == "T-449/14"


def test_an_order_is_still_the_case_it_was_made_in():
    assert case_number_from_celex("62018CO0607.md") == "C-607/18"


def test_leading_zeros_are_dropped():
    """CELEX pads the number to four digits and a case number does not."""
    assert case_number_from_celex("62018CJ0010.md") == "C-10/18"


def test_the_year_is_written_as_two_digits():
    assert case_number_from_celex("61979CJ0155.md") == "C-155/79"


def test_a_name_that_is_not_celex_yields_nothing():
    assert case_number_from_celex("103260-35-necessary-limits.md") is None
    assert case_number_from_celex("11-d-17.md") is None


def test_a_celex_outside_the_case_law_sector_yields_nothing():
    """Sector 3 is legislation. It has no case number to give."""
    assert case_number_from_celex("32003R0001.md") is None


def test_an_unknown_document_type_yields_nothing():
    """Rather than guess a court for a two-letter code the mapping does not
    hold, which would write a wrong identifier into 293 documents."""
    assert case_number_from_celex("62018ZZ0601.md") is None
