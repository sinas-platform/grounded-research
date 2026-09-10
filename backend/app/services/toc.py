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

# Older Commission decisions invert the dot convention: "1 INTRODUCTION"
# (bare number, ALL-CAPS title) is a section heading, while "1. On 21
# September..." (dotted) is a numbered paragraph. The all-caps title is
# the distinguisher — footnotes ("1 OJ L 24, ..., p. 1.") carry
# lowercase. Roman variants ("IV COMPETITIVE ASSESSMENT") included.
_CAPS_NUMBERED = re.compile(
    r"^\s{0,3}(?P<num>\d{1,2}|[IVXLC]{1,6})\s+(?P<title>[A-Z][A-Z0-9 ,'&()/-]{2,%d})\s*$"
    % _MAX_TITLE
)

# Standalone bold line — the heading convention of converted bulletins
# ("**THE KEY POINTS**"). Heuristic-pool only: bold is also used for
# emphasis, so it never counts as explicit structure.
_BOLD_HEADING = re.compile(r"^\*\*(?P<title>[^*].{2,%d}?)\*\*$" % _MAX_TITLE)

# Language-neutral rejection: enumerated prose ends in continuation
# punctuation. (No word lists — an earlier connective list was
# English-biased; the uppercase title-start requirement and the prose
# gate below carry the rest, in any language.)
_SENTENCE_TAIL = re.compile(r"[,;:]$")

# Court-judgment subsection markers: "(a) Arguments of the parties",
# "(2) Findings of the Court", "(i) …". Lowercase/parenthesized, which
# _NUMBERED deliberately excludes. Uppercase title start separates real
# headings from enumerated prose ("(i) to the Product Universal … and");
# a full-sentence tail (including a closing period — regulation recitals
# are numbered "(1) This Regulation lays down ….") is rejected below.
_PAREN_HEADING = re.compile(
    r"^\s{0,3}\((?P<num>[a-z]|\d{1,2}|[ivxl]{1,4})\)"
    r"\s+(?P<title>[A-Z][^\n]{2,58})\s*$"
)

# Standalone ALL-CAPS line — the section convention of French court
# decisions ("EXPOSÉ DU LITIGE", "MOTIFS DE LA DÉCISION", "PAR CES
# MOTIFS") and many older decisions. Unicode-aware via str.isupper().
# Heuristic-pool and floodable: header blocks (party names, court
# addresses) also come in caps, so this tier is shed first on overflow.
_CAPS_LINE = re.compile(r"^\s{0,3}(?P<title>\S[^\n]{3,58}?)\s*:?\s*$")

# The same headings fused to their first paragraph by sentence-based
# normalization: "(2) Findings of the Court 150 According to settled
# case-law, …" — the heading has no terminal punctuation, so a sentence
# segmenter glues it to the numbered paragraph that follows. The title is
# cut where a bare paragraph number followed by an uppercase word starts.
# Kept deliberately narrow (short title, no sentence punctuation inside).
_FUSED_HEADING = re.compile(
    r"^\s{0,3}(?P<num>\((?:[a-z]|\d{1,2}|[ivxl]{1,4})\)|[A-Z]\.|\d{1,2}\.|[IVXLC]{1,6}\.)"
    r"\s+(?P<title>[A-Z][^.;:!?\n]{2,58}?)"
    r"\s+\d{1,4}\s+[A-Z]"
)


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
        m = _BOLD_HEADING.match(line.strip())
        if m:
            numbered_entries.append(
                {"level": 2, "title": m.group("title").strip(), "line": i}
            )
            continue
        m = _CAPS_NUMBERED.match(line)
        if m and m.group("title").strip().isupper() and len(m.group("title").strip()) >= 4:
            numbered_entries.append(
                {
                    "level": 1,
                    "title": f"{m.group('num')} {m.group('title').strip()}",
                    "line": i,
                }
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
                    "src": "dotted",
                }
            )
            continue
        m = _PAREN_HEADING.match(line)
        if m:
            title = m.group("title").strip()
            if _SENTENCE_TAIL.search(title) or title.endswith("."):
                continue
            prev = lines[i - 2].strip() if i >= 2 else ""
            prev_clean = re.sub(r"[\d\s]+$", "", prev)
            if (
                len(prev) > 40
                and prev_clean
                and not prev_clean.endswith((".", ":", "?", '"', "'", ")"))
            ):
                continue
            numbered_entries.append(
                {"level": 3, "title": f"({m.group('num')}) {title}",
                 "line": i, "src": "paren"}
            )
            continue
        m = _FUSED_HEADING.match(line)
        if m:
            num = m.group("num")
            numbered_entries.append(
                {"level": 3 if num.startswith("(") else _numbered_level(num),
                 "title": f"{num} {m.group('title').strip()}",
                 "line": i, "src": "fused"}
            )
            continue
        m = _CAPS_LINE.match(line)
        if m:
            title = m.group("title").strip()
            letters = [ch for ch in title if ch.isalpha()]
            if (
                len(letters) >= 4
                and title.isupper()
                and not any(ch.isdigit() for ch in title)
            ):
                numbered_entries.append(
                    {"level": 1, "title": title, "line": i, "src": "capsline"}
                )

    # Explicit structure wins outright: enough markdown headings means the
    # author (or a structure-preserving converter) already declared the
    # document's outline — the heuristics would add only noise on top.
    if len(md_entries) >= _MD_STRUCTURE_MIN:
        entries = md_entries
    else:
        entries = sorted(md_entries + numbered_entries, key=lambda e: e["line"])

    if len(entries) > _MAX_ENTRIES:
        # Flooded. Shed heuristic tiers progressively — noisiest first —
        # so adding a new heuristic can never cost a document the
        # structure an older, more reliable one already found.
        for drop in (("capsline",), ("capsline", "paren", "fused"),
                     ("capsline", "paren", "fused", "dotted")):
            entries = sorted(
                md_entries
                + [e for e in numbered_entries if e.get("src") not in drop],
                key=lambda e: e["line"],
            )
            if len(entries) <= _MAX_ENTRIES:
                break
        if len(entries) > _MAX_ENTRIES:
            return []
    for e in entries:
        e.pop("src", None)
    return _close_ranges(entries, len(lines))


# ── content normalization (upload-time) ────────────────────────────────────

# A document is "wall-of-text" when its lines are this dense on average.
# Seen with plain-text exports that carry a whole document on one line, 53K
# characters of it in the worst observed case. Line-based navigation (this
# module, and read_document_content's line_from/line_to) is meaningless
# there: a span selects the whole document, so a passage cannot be taken out
# of one.
#
# 200 and not a rounder number. Measured over a 35,407-document corpus: at
# 1000 the rule fires on 46 documents, at 200 on 8,891, and the difference is
# where the damage is. Of documents actually retrieved to answer a question,
# those above 200 chars per line are cited by a claim at 3.9% against 8.1%
# for the rest, and that gap holds inside every rank band and inside every
# document class but one. The exception is instructive: short documents whose
# whole body is one line are cited at the normal rate, because there a
# whole-line span is still a usable passage. The cost is grip, not density.
_DENSITY_THRESHOLD = 200  # chars per line

# A document can be well structured on average and still carry one paragraph
# nothing can grip. Measured on the same corpus, by citation rate among
# documents actually retrieved for a question: dense-average documents are
# cited at 3.9%, and documents whose average is fine but whose longest line
# runs past 2,000 chars at 3.7%, which is the same injury. Between 200 and
# 2,000 there is none: that group is cited at 9.8%, the healthiest of all,
# so re-wrapping it would be churn. Hence a second, much higher bar for the
# single-long-line case rather than reusing the one above.
_LONG_LINE_THRESHOLD = 2000  # chars in any one line

# Segmentation is context-dependent: the boundaries pysbd finds inside a
# 5,000-character line are not the ones it finds in a 500-character line taken
# out of it, so one pass can leave behind a line a second pass would split
# again. The wrap therefore runs to a fixed point, which is what lets the
# function be applied to its own output, and lets the backfill be re-run
# without writing a fresh version every time. Measured over 200 dense
# documents: 33 need no pass, 159 settle after one, 8 need two, none needs
# three. The bound is a guard against a document that oscillates, not a
# budget: reaching it means the content is pathological and worth leaving.
_MAX_WRAP_PASSES = 4

# A segment that cannot be the start of a sentence is not one: the segmenter
# broke inside an abbreviation or a citation. Two shapes, both common in
# reference-dense text: "77 Cong., 1st Sess.," splits after "Cong." and leaves
# a line opening on a comma, and "Fed.R.Civ.P. 12(b)(6)" splits after the
# final period and leaves one opening on a digit. Opening punctuation, a
# digit, a bracket or a lower-case letter all mean the same thing, and none
# of them needs a list of known abbreviations to recognise, which would be
# both language-specific and subject-specific. Merge such a segment back into
# the one before.
_CONTINUATION = re.compile(
    r"^\s*(?:[,;:.\u2026\u00bb\u201d')\]]|[0-9(\[]|[a-z\u00df-\u00ff])")

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


def _rewrap_once(content: str) -> str:
    """One pass of the re-wrap. Returns content unchanged when nothing in it
    is dense enough to touch, which is what ends the loop above."""

    if not content:
        return content
    lines = content.split("\n")
    density = len(content) / max(1, len(lines))
    longest = max((len(line) for line in lines), default=0)
    # Average catches the wall of text; longest catches the otherwise
    # structured document carrying one unbreakable paragraph. Both are the
    # same defect for anything that addresses content by line.
    if density <= _DENSITY_THRESHOLD and longest <= _LONG_LINE_THRESHOLD:
        return content

    import pysbd  # local import: only wall-of-text uploads pay for it

    seg = pysbd.Segmenter(language=_guess_language(content), clean=False)
    out: list[str] = []
    for line in lines:
        if len(line) <= _DENSITY_THRESHOLD:
            out.append(line)
            continue
        # Segments are merged as returned, not stripped and rejoined, so the
        # characters of the line survive the round trip exactly.
        merged: list[str] = []
        for piece in seg.segment(line):
            # The merge is capped at the same threshold it exists to enforce.
            # Without that, a document the segmenter reads badly, one whose
            # sentences mostly open lower-case, merges back into the single
            # line this function was called to break up.
            if (merged and _CONTINUATION.match(piece)
                    and len(merged[-1]) + len(piece) <= _DENSITY_THRESHOLD):
                merged[-1] += piece
            else:
                merged.append(piece)
        out.extend(piece.rstrip() for piece in merged if piece.strip())
    return "\n".join(out)


def normalize_line_density(content: str) -> str:
    """Re-wrap pathologically dense text into sentence-per-line form.

    Applied at UPLOAD time, before any extraction, so character spans and
    line numbers recorded later are consistent with the stored content.
    Normal documents (density under the threshold) pass through unchanged:
    this never touches content that already has line structure.

    Sentence boundaries come from pysbd (rule-based, multilingual, no
    models); the language is sniffed deterministically from the document
    head, defaulting to English rules, which stay punctuation-driven and
    degrade gracefully on a wrong guess.

    Idempotent: run to a fixed point, so applying it to its own output
    returns that output. See _MAX_WRAP_PASSES for why one pass is not enough.
    """
    for _ in range(_MAX_WRAP_PASSES):
        out = _rewrap_once(content)
        if out == content:
            return content
        content = out
    return content
