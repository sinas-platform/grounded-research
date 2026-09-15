"""The answer is assembled, not listed.

Expert review of six generated answers found the same defects in every one:
a flat list of unconnected claims, the conclusion missing or last, legal
tests never set out as ordered conditions, every source presented at one
level, and citations rendered as filename stems. The cause was structural —
the engine emitted claim rows and each reader printed one paragraph per row.

`answer_render` writes the answer instead. These tests pin each branch of
it against one fixture answer: where the conclusion goes, how analysis
claims join into paragraphs and where they break, how a test renders, what a
citation looks like, which labels a sentence carries, and that the markers
number in first-appearance order and agree with the Authorities list. The
last test pins the other half of the contract: an answer drafted before any
of this existed still renders as it always did.

Pure functions throughout — no database, no model, no run.

Run from the backend directory:
`python -m pytest tests/test_the_answer_reads_as_prose.py`
"""

from datetime import date

from app.services.answer_render import citation, paragraph_label, render_markdown

QUESTION = "Does the obligation apply to the respondent, and is it mandatory?"

PARTS = [
    {"index": 0, "label": "Whether the obligation applies", "text": "..."},
    {"index": 1, "label": "Whether it is mandatory", "text": "..."},
]

DOCS = {
    "d1": {
        "title": "Kestrel Holdings v Northmoor Authority",
        "external_ref": "T-100/20",
        "class": "Court Decision",
        "properties": {"case_number": "T-100/20",
                       "ecli": "ECLI:XX:YY:2021:1",
                       "decision_date": "2021-05-04"},
    },
    "d2": {
        "title": "An Instrument",
        "class": "Legislation",
        "properties": {"celex": "3XX0001", "date": "2003-01-01",
                       "status": "repealed"},
    },
    "d3": {
        "title": "A Textbook Chapter",
        "class": "Commentary",
        "properties": {"date": "2024-03-01"},
    },
}


def _answer(**over):
    return {"question": QUESTION, "question_parts": PARTS,
            "law_stated_as_at": date(2024, 3, 1), **over}


def _claims():
    """One of each shape the contract names, in drafting order."""
    return [
        {"id": "c1", "sequence": 1, "section": "conclusion", "part_index": None,
         "position": 1, "claim_kind": "conclusion", "follows_from": ["c4"],
         "claim_text": "The obligation applies and is mandatory."},
        {"id": "c2", "sequence": 2, "section": "conclusion", "part_index": 0,
         "position": 1, "claim_kind": "conclusion",
         "claim_text": "The obligation applies."},
        {"id": "c3", "sequence": 3, "section": "conclusion", "part_index": 1,
         "position": 1, "claim_kind": "conclusion",
         "claim_text": "It is mandatory."},
        {"id": "c4", "sequence": 4, "section": "analysis", "part_index": 0,
         "position": 1, "claim_kind": "legal_principle",
         "authority_label": "court_judgment", "authority_tier": 1,
         "claim_text": "The deciding body held that the obligation reaches "
                       "an undertaking in the respondent's position."},
        {"id": "c5", "sequence": 5, "section": "analysis", "part_index": 0,
         "position": 2, "claim_kind": "legal_principle",
         "authority_label": "court_judgment", "authority_tier": 1,
         "claim_text": "It reasoned from the wording of the instrument."},
        {"id": "c6", "sequence": 6, "section": "analysis", "part_index": 0,
         "position": 3, "claim_kind": "test",
         "authority_label": "court_judgment",
         "claim_text": "The reach test.",
         "test": {"name": "The reach test",
                  "conditions": [{"text": "the first limb", "cumulative": True},
                                 {"text": "the second limb", "cumulative": True}],
                  "source_para": "42"}},
        {"id": "c7", "sequence": 7, "section": "analysis", "part_index": 0,
         "position": 5, "claim_kind": "inference", "follows_from": ["c4"],
         "claim_text": "It follows that the respondent is within reach."},
        {"id": "c8", "sequence": 8, "section": "analysis", "part_index": 1,
         "position": 1, "claim_kind": "legal_principle",
         "authority_label": "legislation", "authority_tier": 2,
         "currency_note": "repealed; superseded by a later instrument",
         "claim_text": "The instrument states the duty in mandatory terms."},
        {"id": "c9", "sequence": 9, "section": "analysis", "part_index": 1,
         "position": 2, "claim_kind": "legal_principle",
         "authority_label": "commentary",
         "jurisdiction_note": "national law (a single state)",
         "claim_text": "A commentator reads the duty the same way."},
    ]


def _evidence():
    return [
        {"claim_id": "c4", "document_id": "d1", "span": {"paragraph_ref": "42"}},
        {"claim_id": "c5", "document_id": "d1", "span": {"paragraph_ref": None}},
        {"claim_id": "c6", "document_id": "d1",
         "span": {"paragraph_ref": "r.o. 4.2"}},
        {"claim_id": "c8", "document_id": "d2",
         "span": {"paragraph_ref": "recital 14"}},
        {"claim_id": "c9", "document_id": "d3", "span": {}},
        # An inference's document: reached through evidence, never marked.
        {"claim_id": "c7", "document_id": "d1", "span": {}},
    ]


def _render(**over):
    return render_markdown(_answer(**over), _claims(), _evidence(), DOCS)


def _lines():
    return _render().markdown.splitlines()


# ── where things go ──────────────────────────────────────────────────────────

def test_the_question_is_the_title_and_the_conclusion_comes_first():
    """The reviewer's first must-have, and the one the answer never met: a
    reader who stops after the first section has the answer."""
    lines = [ln for ln in _lines() if ln.strip()]
    assert lines[0] == f"# {QUESTION}"
    assert lines[1] == "## Conclusion"
    # The overall conclusion leads, then one per part in part order.
    assert lines[2] == "The obligation applies and is mandatory."
    assert lines[3] == "The obligation applies."
    assert lines[4] == "It is mandatory."


def test_each_part_gets_its_own_section_under_its_own_label():
    md = _render().markdown
    assert "## Whether the obligation applies" in md
    assert "## Whether it is mandatory" in md
    # In part order, and after the conclusion.
    assert (md.index("## Conclusion")
            < md.index("## Whether the obligation applies")
            < md.index("## Whether it is mandatory")
            < md.index("## Authorities"))


def test_a_part_with_no_label_anywhere_is_still_a_section():
    """A decomposition that lost its labels must not lose its parts: the
    heading falls back to the part's number rather than to nothing."""
    md = render_markdown(
        _answer(question_parts=[{"index": 0, "text": "..."}]),
        [{"id": "x", "sequence": 1, "section": "analysis", "part_index": 0,
          "position": 1, "claim_text": "Something."}],
        [], {}).markdown
    assert "## Part 1" in md


# ── paragraphs ───────────────────────────────────────────────────────────────

def test_consecutive_analysis_claims_share_a_paragraph():
    """One paragraph per claim IS the flat list. Claims that follow each
    other in position read on from one another."""
    md = _render().markdown
    assert ("The deciding body held that the obligation reaches an "
            "undertaking in the respondent's position.[1] It reasoned from "
            "the wording of the instrument.[1]") in md


def test_a_test_claim_breaks_the_paragraph_and_lists_its_conditions():
    md = _render().markdown
    assert "**The reach test** — the conditions, in order (cumulative)[1]:" in md
    assert "\n1. the first limb\n2. the second limb\n" in md


def test_a_test_whose_limbs_are_alternatives_is_not_called_cumulative():
    claims = _claims()
    for c in claims:
        if c["id"] == "c6":
            for cond in c["test"]["conditions"]:
                cond["cumulative"] = False
    md = render_markdown(_answer(), claims, _evidence(), DOCS).markdown
    assert "the conditions, in order[1]:" in md
    assert "(cumulative)" not in md


def test_a_gap_in_position_starts_a_new_paragraph():
    """A gap is the drafter saying these do not run on — usually because a
    claim between them was dropped in revision."""
    claims = [c for c in _claims()
              if c["id"] in ("c4", "c7")]          # positions 1 and 5
    md = render_markdown(_answer(), claims, _evidence(), DOCS).markdown
    body = md.split("## Whether the obligation applies")[1].split("##")[0]
    assert body.strip().count("\n\n") == 1


def test_an_inference_renders_inline_and_cites_nothing():
    """An inference reasons from claims already made. A citation marker on
    one says a passage carries it, and none does."""
    md = _render().markdown
    assert "It follows that the respondent is within reach.\n" in md
    assert "It follows that the respondent is within reach.[" not in md


# ── labels ───────────────────────────────────────────────────────────────────

def test_a_secondary_source_says_so_after_the_sentence():
    assert ("A commentator reads the duty the same way.[3] [commentary] "
            "[national law (a single state)]") in _render().markdown


def test_a_stale_instrument_carries_its_currency_note():
    assert ("[repealed; superseded by a later instrument]"
            in _render().markdown)


def test_a_primary_source_carries_no_label():
    md = _render().markdown
    assert "[court_judgment]" not in md
    assert "[court judgment]" not in md


# ── authorities ──────────────────────────────────────────────────────────────

def test_authorities_are_grouped_in_the_contracts_order():
    md = _render().markdown
    auth = md.split("## Authorities")[1]
    assert (auth.index("**Court judgments**") < auth.index("**Legislation**")
            < auth.index("**Commentary**"))


def test_each_document_appears_once_with_every_paragraph_it_pins():
    auth = _render().markdown.split("## Authorities")[1]
    assert auth.count("Kestrel Holdings v Northmoor Authority") == 1
    assert ("- [1] Kestrel Holdings v Northmoor Authority (T-100/20, "
            "ECLI:XX:YY:2021:1, 2021-05-04), para. 42, r.o. 4.2") in auth


def test_a_group_heading_is_never_swallowed_by_the_list_above_it():
    """Without a blank line the next heading is a lazy continuation of the
    previous group's last bullet, and every group after the first vanishes
    into it."""
    auth = _render().markdown.split("## Authorities")[1]
    for heading in ("**Legislation**", "**Commentary**"):
        assert f"\n\n{heading}\n\n" in auth


def test_markers_number_in_first_appearance_order_and_match_the_list():
    r = _render()
    assert r.citations == [{"n": 1, "document_id": "d1"},
                           {"n": 2, "document_id": "d2"},
                           {"n": 3, "document_id": "d3"}]
    auth = r.markdown.split("## Authorities")[1]
    for entry in r.citations:
        assert f"- [{entry['n']}] " in auth


def test_a_document_reached_only_through_an_unmarked_claim_still_gets_a_number():
    """An inference's evidence carries no marker in the prose, so its
    document is first numbered in the Authorities pass. Numbering there must
    be first appearance and not whatever order the sort compares rows in."""
    claims = [c for c in _claims() if c["id"] == "c7"]
    r = render_markdown(_answer(), claims, _evidence(), DOCS)
    assert [c["document_id"] for c in r.citations] == ["d1", "d2", "d3"]


def test_a_document_is_never_cited_by_its_filename():
    md = render_markdown(
        _answer(),
        [{"id": "x", "sequence": 1, "section": "analysis", "part_index": 0,
          "position": 1, "claim_text": "Something."}],
        [{"claim_id": "x", "document_id": "d9", "span": {}}],
        {"d9": {"title": None, "class": "Court Decision",
                "filename": "storage-name-9174.md",
                "properties": {"case_number": "T-451/20"}}}).markdown
    assert "storage-name-9174" not in md
    assert "- [1] T-451/20" in md


def test_an_answer_that_cites_nothing_says_so_rather_than_printing_a_heading():
    md = render_markdown(_answer(), _claims(), [], {}).markdown
    assert "(No sources cited.)" in md


# ── citation form ────────────────────────────────────────────────────────────

def test_a_citation_is_name_number_ecli_and_date():
    assert citation(DOCS["d1"]) == ("Kestrel Holdings v Northmoor Authority "
                                   "(T-100/20, ECLI:XX:YY:2021:1, 2021-05-04)")


def test_a_citation_with_nothing_to_say_still_names_something():
    assert citation({}) == "Untitled source"
    assert citation(None) == "Untitled source"


def test_a_bare_integer_gets_the_word_and_every_other_label_is_verbatim():
    """`paragraph_ref` is a LABEL. "4.2" may be a section and "recital 14"
    already says what it counts; only a bare integer is missing the word."""
    assert paragraph_label("42") == "para. 42"
    assert paragraph_label(" 42 ") == "para. 42"
    assert paragraph_label("r.o. 4.2") == "r.o. 4.2"
    assert paragraph_label("recital 14") == "recital 14"
    assert paragraph_label("4.2") == "4.2"
    assert paragraph_label(None) is None
    assert paragraph_label("") is None


# ── closing ──────────────────────────────────────────────────────────────────

def test_the_answer_closes_with_the_date_the_law_is_stated_as_at():
    assert _render().markdown.rstrip().endswith("Law stated as at 2024-03-01.")


def test_an_answer_with_no_such_date_closes_without_inventing_one():
    md = _render(law_stated_as_at=None).markdown
    assert "Law stated as at" not in md


# ── the answers that came before ─────────────────────────────────────────────

def test_an_answer_with_no_structure_renders_one_paragraph_per_claim():
    """Rows from before the structure existed carry null everywhere. They
    must not acquire a Conclusion section they never had, and they must not
    stop rendering: they render as they always did."""
    claims = [{"id": "a", "sequence": 1, "claim_text": "One."},
              {"id": "b", "sequence": 2, "claim_text": "Two."}]
    ev = [{"claim_id": "a", "document_id": "d1", "span": {}},
          {"claim_id": "b", "document_id": "d3", "span": {}}]
    md = render_markdown({"question": "Q?"}, claims, ev, DOCS).markdown
    # Byte for byte. A claim with no `authority_label` classifies its source
    # as nothing, so both documents sit under "Other sources" — the old rows
    # never said what a source was, and the renderer must not decide for
    # them.
    assert md == (
        "# Q?\n"
        "\n"
        "One.[1]\n"
        "\n"
        "Two.[2]\n"
        "\n"
        "## Authorities\n"
        "\n"
        "**Other sources**\n"
        "\n"
        "- [1] Kestrel Holdings v Northmoor Authority (T-100/20, "
        "ECLI:XX:YY:2021:1, 2021-05-04)\n"
        "- [2] A Textbook Chapter (2024-03-01)\n"
    )


def test_one_structured_claim_is_enough_to_make_the_answer_structured():
    """A part-way migrated answer must not render half a list: the presence
    of any section is what switches the layout, and the unsectioned rows are
    kept rather than dropped."""
    claims = [{"id": "a", "sequence": 1, "section": "conclusion",
               "part_index": None, "position": 1, "claim_text": "The answer."},
              {"id": "b", "sequence": 2, "claim_text": "An older row."}]
    md = render_markdown(_answer(), claims, [], {}).markdown
    assert "## Conclusion" in md
    assert "An older row." in md
