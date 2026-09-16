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

# A document row as `assemble` builds it. The identity is already resolved
# when it gets here — `identifier` from the property the class names as its
# identity, `alternate_identifier` from the property it declares as a second
# citable one, `date` from the property it declares as its date — because the
# renderer is pure and knows no property names at all. What each class CALLS
# those properties is not visible in this file, and that is the contract.
DOCS = {
    "d1": {
        "title": "Kestrel Holdings v Northmoor Authority",
        "external_ref": "T-100/20",
        "class": "Court Decision",
        "identifier": "T-100/20",
        "alternate_identifier": "ECLI:XX:YY:2021:1",
        "date": date(2021, 5, 4),
    },
    "d2": {
        "title": "An Instrument",
        "class": "Legislation",
        "identifier": "3XX0001",
        "date": date(2003, 1, 1),
    },
    "d3": {
        "title": "A Textbook Chapter",
        "class": "Commentary",
        "date": date(2024, 3, 1),
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
        # No label: the class these cite declares none, which is what says a
        # source carries a rule on its own.
        {"id": "c4", "sequence": 4, "section": "analysis", "part_index": 0,
         "position": 1, "claim_kind": "rule",
         "authority_tier": 1,
         "claim_text": "The deciding body held that the obligation reaches "
                       "an undertaking in the respondent's position."},
        {"id": "c5", "sequence": 5, "section": "analysis", "part_index": 0,
         "position": 2, "claim_kind": "rule",
         "authority_tier": 1,
         "claim_text": "It reasoned from the wording of the instrument."},
        {"id": "c6", "sequence": 6, "section": "analysis", "part_index": 0,
         "position": 3, "claim_kind": "test",
         "claim_text": "The reach test.",
         "test": {"name": "The reach test",
                  "conditions": [{"text": "the first limb", "cumulative": True},
                                 {"text": "the second limb", "cumulative": True}],
                  "source_para": "42"}},
        {"id": "c7", "sequence": 7, "section": "analysis", "part_index": 0,
         "position": 5, "claim_kind": "inference", "follows_from": ["c4"],
         "claim_text": "It follows that the respondent is within reach."},
        {"id": "c8", "sequence": 8, "section": "analysis", "part_index": 1,
         "position": 1, "claim_kind": "rule",
         "authority_tier": 2,
         "currency_note": "repealed; superseded by a later instrument",
         "claim_text": "The instrument states the duty in mandatory terms."},
        # Labelled, because the class it cites declares the label. The words
        # are the deployment's; the renderer prints them and knows nothing
        # about what they mean.
        {"id": "c9", "sequence": 9, "section": "analysis", "part_index": 1,
         "position": 2, "claim_kind": "rule",
         "authority_label": "commentary",
         "jurisdiction_note": "jurisdiction: a single state",
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


def test_a_short_label_is_the_heading_exactly_as_it_stands():
    """A heading is scanned, not read. The splitter writes one of three to
    seven words and the renderer prints it verbatim — no shortening, no
    trailing punctuation added or taken away."""
    md = render_markdown(
        _answer(question_parts=[{"index": 0, "label": "Privilege of the adviser",
                                 "text": "whether the adviser is covered, and "
                                         "from what moment"}]),
        [{"id": "x", "sequence": 1, "section": "analysis", "part_index": 0,
          "position": 1, "claim_text": "Something."}],
        [], {}).markdown
    assert "## Privilege of the adviser\n" in md


def test_a_legacy_sentence_label_falls_back_to_its_first_clause():
    """Rows written before the splitter wrote headings carry the part's first
    ten words and an ellipsis, which renders as a sentence cut off mid-phrase.
    Such a label is shortened to the clause it opens with, whole."""
    legacy = ("Whether the obligation applies to an undertaking in the "
              "respondent's position, and from what moment it binds…")
    md = render_markdown(
        _answer(question_parts=[{"index": 0, "label": legacy, "text": legacy}]),
        [{"id": "x", "sequence": 1, "section": "analysis", "part_index": 0,
          "position": 1, "claim_text": "Something."}],
        [], {}).markdown
    assert ("## Whether the obligation applies to an undertaking in the "
            "respondent's position\n") in md
    assert "…" not in md
    assert "..." not in md


def test_a_heading_is_never_a_truncated_sentence():
    """The defect, pinned on the helper the renderer and the splitter share:
    a label that is a sentence is shortened, and what comes back is a phrase
    that ends where a phrase ends rather than trailing off."""
    from app.services.answer_structure import part_heading

    assert part_heading("Privilege of the adviser") == "Privilege of the adviser"
    # Trailing punctuation and a trailing ellipsis are not part of a heading.
    assert part_heading("During an inspection.") == "During an inspection"
    assert part_heading("How the courts drew the line…") == "How the courts drew the line"
    # A sentence: the clause it opens with, whole, and nothing trails off.
    assert part_heading(
        "Whether the duty binds the respondent from the moment of "
        "notification, and what follows if it does"
    ) == "Whether the duty binds the respondent from the moment of notification"
    # One long clause and no break to stop at: cut, but never left hanging on
    # a joining word, and never with an ellipsis.
    got = part_heading("Whether the obligation applies to an undertaking that "
                       "has not yet been told of the decision")
    assert got == "Whether the obligation applies to an undertaking"
    # No label at all: the part's own text stands in, so a decomposition that
    # lost its headings still gets a phrase rather than a number.
    assert part_heading(None, "whether it is mandatory") == "whether it is mandatory"
    assert part_heading(None, None) == ""


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
            "[jurisdiction: a single state]") in _render().markdown


def test_a_stale_instrument_carries_its_currency_note():
    assert ("[repealed; superseded by a later instrument]"
            in _render().markdown)


def test_a_source_whose_class_declares_no_label_carries_none():
    """The label is the class's, so a class that declares none produces no
    bracket at all. The claims citing the decision and the instrument carry
    a tier and a currency note and still say nothing about what the source
    IS."""
    md = _render().markdown
    for c in _claims():
        if c["id"] in ("c4", "c5", "c6", "c8"):
            assert not c.get("authority_label")
    assert "[Court Decision]" not in md
    assert "[Legislation]" not in md


def test_no_label_vocabulary_survives_in_engine_code():
    """The labels the drafter used to pick from, the words they printed as
    and the headings they grouped under were a vocabulary for one collection
    living in engine code. All four tables are gone: the label a claim
    carries is the one its document's class declares, and the heading is the
    class's own name.

    Pinned by the names rather than by the words, so the check says nothing
    about any deployment's subject matter — which is the property being
    protected."""
    from app.services import answer_render, answer_structure

    for module in (answer_render, answer_structure):
        for name in ("AUTHORITY_LABELS", "SECONDARY_LABELS", "GROUP_HEADINGS",
                     "LABEL_TEXT"):
            assert not hasattr(module, name), f"{module.__name__}.{name}"
    # The one heading the engine still names, and it names the absence of a
    # class rather than any kind of source.
    assert answer_structure.UNCLASSIFIED_HEADING == "Unclassified"


# ── authorities ──────────────────────────────────────────────────────────────

def test_authorities_are_grouped_by_class_highest_authority_first():
    """The heading is the document's own class — the deployment's word for
    what the thing is — and the groups lead with whichever class holds the
    most authoritative source cited."""
    md = _render().markdown
    auth = md.split("## Authorities")[1]
    assert (auth.index("**Court Decision**") < auth.index("**Legislation**")
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
                "identifier": "T-451/20"}}).markdown
    assert "storage-name-9174" not in md
    assert "- [1] T-451/20" in md


def _cite_only(doc: dict) -> str:
    """The answer built around a single document, so what its citation says
    can be read out of the prose and the Authorities list together."""
    return render_markdown(
        _answer(),
        [{"id": "x", "sequence": 1, "section": "analysis", "part_index": 0,
          "position": 1, "claim_text": "Something."}],
        [{"claim_id": "x", "document_id": "d9",
          "span": {"paragraph_ref": "44"}}],
        {"d9": doc}).markdown


def test_a_document_with_no_number_is_cited_by_title_and_date_alone():
    """The identifier slot used to fall back to `external_ref`, which is the
    file's name whenever the connector supplied no natural key — so every
    source without an identifier was cited by a storage name a reviewer
    called useless. It says less instead. A class that declares no identifier
    property at all arrives here the same way: with no identifier."""
    md = _cite_only({
        "title": "Professional duties: what the deciding body said",
        "external_ref": "stored-file-9174.md",
        "filename": "stored-file-9174.md",
        "class": "Commentary",
        "date": "2010-09-14"})
    assert ("- [1] Professional duties: what the deciding body said "
            "(2010-09-14), para. 44") in md
    assert ".md" not in md
    assert "stored-file" not in md


def test_a_document_with_a_case_number_is_cited_as_it_always_was():
    md = _cite_only(DOCS["d1"])
    assert ("- [1] Kestrel Holdings v Northmoor Authority (T-100/20, "
            "ECLI:XX:YY:2021:1, 2021-05-04), para. 44") in md


def test_a_document_with_no_title_is_named_by_what_it_is_not_by_its_file():
    md = _cite_only({"title": None, "external_ref": "storage-name-9174.md",
                     "filename": "storage-name-9174.md", "class": "Commentary",
                     "date": "2010-09-14"})
    assert "- [1] Commentary (2010-09-14), para. 44" in md
    assert "storage-name-9174" not in md


def test_the_assembler_never_reads_a_filename():
    """The display rule held only while every branch of `citation` remembered
    it. What the assembler does not hold, it cannot print: the rows it reads
    carry the declared identity and no file name at all."""
    import inspect

    from app.services import answer_render

    src = inspect.getsource(answer_render.assemble)
    assert "Document.filename" not in src
    assert "Document.external_ref" not in src


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


def test_the_identifier_slot_holds_a_declared_property_or_nothing():
    """`external_ref` is the source's natural key when a connector supplies
    one and the file's name when it does not, and the citation cannot tell
    the two apart. So it holds neither: only what the class declared, which
    reaches the renderer already resolved."""
    assert citation({"title": "A Textbook Chapter", "class": "Commentary",
                     "external_ref": "chapter-2024.md",
                     "date": "2024-03-01"}
                    ) == "A Textbook Chapter (2024-03-01)"


def test_the_renderer_holds_no_property_name_of_its_own():
    """The four names a citation's number used to be looked for under, and
    the one its ECLI came from, were five guesses at one collection's
    spelling. A row now arrives with its identity resolved, so a document
    whose properties are all there but whose class declared none of them
    is cited by title and date — never by whatever a property happens to be
    called."""
    import inspect

    from app.services import answer_render

    # Comments are excluded: the file has to be able to say what it replaced,
    # and a module that cannot name the defect it fixed teaches nobody.
    code = "\n".join(ln for ln in inspect.getsource(answer_render).splitlines()
                     if not ln.lstrip().startswith("#"))
    for guessed in ("case_number", "celex", '"reference"', '"number"',
                    '"ecli"'):
        assert guessed not in code, guessed
    assert citation({"title": "An Instrument", "class": "Legislation",
                     "properties": {"celex": "3XX0001"}}) == "An Instrument"


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
    # Byte for byte, including the marker numbers: the old rows said nothing
    # about what a source was, so the groups come from the documents' own
    # classes and neither claim acquires a label. Numbering stays first
    # appearance in the prose even though the groups sort the other way.
    assert md == (
        "# Q?\n"
        "\n"
        "One.[1]\n"
        "\n"
        "Two.[2]\n"
        "\n"
        "## Authorities\n"
        "\n"
        "**Commentary**\n"
        "\n"
        "- [2] A Textbook Chapter (2024-03-01)\n"
        "\n"
        "**Court Decision**\n"
        "\n"
        "- [1] Kestrel Holdings v Northmoor Authority (T-100/20, "
        "ECLI:XX:YY:2021:1, 2021-05-04)\n"
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
