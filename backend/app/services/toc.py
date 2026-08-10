"""Deterministic table of contents: headings with line ranges (CNAI-1166).

The old pipeline asked a model to outline documents; audited on 30 random
documents, 52% of stored entries did not exist in their document at all,
and the shapes were inconsistent. This module replaces that with a parse
of the stored markdown: an entry exists only if its heading literally
sits at that line, so invention is structurally impossible. A document
without real headings gets an EMPTY toc — honest emptiness over
fabricated structure.

Parsing is explicit-structure-first: when a document carries real
markdown headings (two or more), those ARE the table of contents and the
plain-text heuristics are skipped entirely — explicit structure wins
outright. The numbered-heading heuristics exist as a bridge for content
whose structure was flattened by PDF conversion (most decisions, books
and legislation in the current corpus); they are language-neutral (no
word lists) and retire by themselves as source pipelines start
preserving structure (Akoma Ntoso / Formex / HTML → real headings).

Schema (one shape, always): a list of
    {"level": int, "title": str, "line": int, "line_to": int}
ordered by line. `line`/`line_to` are 1-based and inclusive, matching
read_document_content's line_from/line_to — a reading agent jumps from a
toc entry straight to the section window.
"""

from __future__ import annotations

import re

_MAX_TITLE = 120
_MAX_ENTRIES = 400  # hard sanity ceiling; beyond this it's not a TOC
_MD_STRUCTURE_MIN = 2  # this many md headings = explicit structure, trust it alone

# Markdown heading: # to ####, short line.
_MD_HEADING = re.compile(r"^(#{1,4})\s+(\S.{1,%d})$" % _MAX_TITLE)

# Numbered / lettered section heading, as court decisions and regulatory
# documents use them: "1.", "3.1.2.", "IV.", "(1)", "A." followed by a
# short title that starts with an uppercase letter or digit.
_NUMBERED = re.compile(
    r"^\s{0,3}"
    r"(?P<num>(?:\d+(?:\.\d+)*\.|[IVXLC]+\.|\(?[A-Z]\)|[A-Z]\.)(?:\d+[.)])*)"
    r"\s+"
    r"(?P<title>[A-Z0-9](?:[^\n]{2,%d}?))"
    r"\s*$" % _MAX_TITLE
)

# Language-neutral rejection: enumerated prose ends in continuation
# punctuation. (No word lists — an earlier connective list was
# English-biased; the uppercase title-start requirement and the prose
# gate below carry the rest, in any language.)
_SENTENCE_TAIL = re.compile(r"[,;:]$")


def _numbered_level(num: str) -> int:
    """'3.' → 1, '3.1.' → 2, '3.1.2.' → 3; roman/letter markers → 1."""
    parts = [p for p in re.split(r"[.)]", num) if p]
    if len(parts) == 1 and not parts[0].isdigit():
        return 1
    return len(parts)


def _close_ranges(entries: list[dict], total_lines: int) -> list[dict]:
    """An entry's range ends where the next same-or-higher level starts."""
    for idx, e in enumerate(entries):
        end = total_lines
        for later in entries[idx + 1 :]:
            if later["level"] <= e["level"]:
                end = later["line"] - 1
                break
        e["line_to"] = end
    return entries


def derive_toc(content: str) -> list[dict]:
    """Parse verbatim headings out of markdown content. Deterministic,
    no model involved. Returns [] when the document has no recognizable
    heading structure."""
    if not content or not content.strip():
        return []
    lines = content.split("\n")

    md_entries: list[dict] = []
    numbered_entries: list[dict] = []
    in_code_fence = False
    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        m = _MD_HEADING.match(line)
        if m:
            md_entries.append(
                {"level": len(m.group(1)), "title": m.group(2).strip(), "line": i}
            )
            continue
        m = _NUMBERED.match(line)
        if m:
            title = m.group("title").strip()
            if _SENTENCE_TAIL.search(title):
                continue
            # a heading is set off from prose: reject only when the
            # previous line reads like mid-sentence flow. Short leftovers
            # (page numbers, stray tokens) and lines ending in a sentence
            # plus a glued footnote digit ("...effects.11") must not
            # suppress a real heading.
            prev = lines[i - 2].strip() if i >= 2 else ""
            prev_clean = re.sub(r"[\d\s]+$", "", prev)
            if (
                len(prev) > 40
                and prev_clean
                and not prev_clean.endswith((".", ":", "?", '"', "'", ")"))
            ):
                continue
            numbered_entries.append(
                {
                    "level": _numbered_level(m.group("num")),
                    "title": f"{m.group('num')} {title}".strip(),
                    "line": i,
                }
            )

    # Explicit structure wins outright: enough markdown headings means the
    # author (or a structure-preserving converter) already declared the
    # document's outline — the heuristics would add only noise on top.
    if len(md_entries) >= _MD_STRUCTURE_MIN:
        entries = md_entries
    else:
        entries = sorted(md_entries + numbered_entries, key=lambda e: e["line"])

    if len(entries) > _MAX_ENTRIES:
        # not a heading structure — a numbered list masquerading as one
        return []
    return _close_ranges(entries, len(lines))


# ── content normalization (upload-time) ────────────────────────────────────

# A document is "wall-of-text" when its lines are this dense on average —
# seen with CourtListener plain_text (53K chars in ~1 line) and some
# EUR-Lex court documents. Line-based navigation (this module, and
# read_document_content's line_from/line_to) is meaningless there.
_DENSITY_THRESHOLD = 1000  # chars per line

# Cheap deterministic language sniff for pysbd — stopword hits over the
# document head. A wrong guess degrades gracefully: segmentation stays
# punctuation-driven either way.
_LANG_MARKERS: dict[str, tuple[str, ...]] = {
    "fr": (" le ", " la ", " les ", " des ", " une ", " être ", " dans "),
    "de": (" der ", " die ", " das ", " und ", " nicht ", " eine ", " für "),
    "nl": (" de ", " het ", " een ", " niet ", " voor ", " zijn ", " wordt "),
    "es": (" el ", " los ", " las ", " una ", " para ", " según ", " sobre "),
    "it": (" il ", " gli ", " delle ", " una ", " per ", " secondo ", " sono "),
}


def _guess_language(content: str) -> str:
    head = f" {content[:3000].lower()} "
    best, best_hits = "en", 2  # english default unless another clearly leads
    for lang, markers in _LANG_MARKERS.items():
        hits = sum(head.count(m) for m in markers)
        if hits > best_hits:
            best, best_hits = lang, hits
    return best


def normalize_line_density(content: str) -> str:
    """Re-wrap pathologically dense text into sentence-per-line form.

    Applied at UPLOAD time, before any extraction, so character spans and
    line numbers recorded later are consistent with the stored content.
    Normal documents (density under the threshold) pass through unchanged
    — this never touches content that already has line structure.

    Sentence boundaries come from pysbd (rule-based, multilingual, no
    models); the language is sniffed deterministically from the document
    head, defaulting to English rules, which stay punctuation-driven and
    degrade gracefully on a wrong guess.
    """
    if not content:
        return content
    lines = content.split("\n")
    density = len(content) / max(1, len(lines))
    if density <= _DENSITY_THRESHOLD:
        return content

    import pysbd  # local import: only wall-of-text uploads pay for it

    seg = pysbd.Segmenter(language=_guess_language(content), clean=False)
    out: list[str] = []
    for line in lines:
        if len(line) <= _DENSITY_THRESHOLD:
            out.append(line)
            continue
        out.extend(s.rstrip() for s in seg.segment(line) if s.strip())
    return "\n".join(out)
