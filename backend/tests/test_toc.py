"""Deterministic TOC derivation (CNAI-1166).

Every entry must be a verbatim heading at its stated line — invention is
structurally impossible — and a document without heading structure gets
an empty toc, never a fabricated one.

Run from the backend directory: `python -m pytest tests/test_toc.py`
"""

from app.services.toc import derive_toc


def test_markdown_headings_with_ranges():
    content = "\n".join([
        "# Decision",          # 1
        "intro text",          # 2
        "## Parties",          # 3
        "a",                   # 4
        "b",                   # 5
        "## Assessment",       # 6
        "c",                   # 7
    ])
    toc = derive_toc(content)
    assert [(e["title"], e["line"], e["line_to"]) for e in toc] == [
        ("Decision", 1, 7),
        ("Parties", 3, 5),
        ("Assessment", 6, 7),
    ]
    assert toc[1]["level"] == 2


def test_numbered_sections_nest_by_depth():
    content = "\n".join([
        "1. THE PARTIES",           # 1
        "text.",                    # 2
        "",                         # 3
        "3.1. Product market",      # 4
        "text.",                    # 5
        "",                         # 6
        "3.1.1. The Notifying Party's view",  # 7
        "text.",                    # 8
        "",                         # 9
        "2. EU DIMENSION",          # 10
        "text",                     # 11
    ])
    toc = derive_toc(content)
    titles = [e["title"] for e in toc]
    assert "1. THE PARTIES" in titles
    assert "3.1. Product market" in titles[1]
    levels = {e["title"]: e["level"] for e in toc}
    assert levels["1. THE PARTIES"] == 1
    assert levels["3.1. Product market"] == 2
    # a level-1 range runs until the NEXT level-1 heading: "1. THE
    # PARTIES" spans its subsections and ends right before "2. EU
    # DIMENSION" at line 10
    parties = next(e for e in toc if e["title"] == "1. THE PARTIES")
    assert parties["line_to"] == 9
    assert next(e for e in toc if e["title"].startswith("2. EU"))["line"] == 10


def test_prose_enumerations_are_not_headings():
    content = "\n".join([
        "The parties agreed on the following points,",
        "1. that prices would be fixed quarterly and",
        "2. that customers would be allocated by region,",
        "as described further below.",
    ])
    assert derive_toc(content) == []


def test_no_headings_means_empty_never_invented():
    assert derive_toc("Just three paragraphs of prose.\n\nMore prose.\n\nEnd.") == []
    assert derive_toc("") == []


def test_code_fences_are_ignored():
    content = "\n".join([
        "# Real heading",
        "```",
        "# not a heading, code comment",
        "```",
        "text",
    ])
    toc = derive_toc(content)
    assert len(toc) == 1
    assert toc[0]["title"] == "Real heading"


def test_giant_numbered_lists_bail_to_empty():
    # 500 heading-shaped items (blank-line separated so each passes the
    # whitespace gate): far beyond any real TOC — bail to empty
    content = "\n\n".join(f"{i}. Item number {i}" for i in range(1, 500))
    assert derive_toc(content) == []


def test_wall_of_text_is_rewrapped_but_normal_content_untouched():
    from app.services.toc import normalize_line_density

    normal = "# Heading\n\nA paragraph.\nAnother line.\n"
    assert normalize_line_density(normal) == normal

    wall = ("The Court finds the agreement restrictive. " * 60).strip()
    wrapped = normalize_line_density(wall)
    assert wrapped != wall
    assert len(wrapped.split("\n")) >= 50
    # every word survives in order; only line breaks / boundary spaces move
    assert wrapped.split() == wall.split()


def test_md_structure_wins_over_numbered_heuristics():
    content = "\n".join([
        "# Decision",              # 1 — explicit structure
        "text.",                   # 2
        "## Assessment",           # 3 — explicit structure
        "text.",                   # 4
        "",                        # 5
        "5. Short List Item",      # 6 — heuristic candidate, must be ignored
        "text",                    # 7
    ])
    toc = derive_toc(content)
    assert [e["title"] for e in toc] == ["Decision", "Assessment"]


def test_multilingual_wall_of_text_wraps():
    from app.services.toc import normalize_line_density, _guess_language

    fr = ("La Cour constate une infraction dans le secteur de la livraison. "
          "Elle inflige une amende de 3,5 millions EUR aux parties. ") * 30
    assert _guess_language(fr) == "fr"
    wrapped = normalize_line_density(fr.strip())
    assert len(wrapped.split("\n")) >= 30
    assert wrapped.split() == fr.strip().split()
