"""Deterministic table of contents: headings with line ranges (CNAI-1166).

The old pipeline asked a model to outline documents; audited on 30 random
documents, 52% of stored entries did not exist in their document at all,
and the shapes were inconsistent. This module replaces that with a parse
of the stored markdown: an entry exists only if its heading literally
sits at that line, so invention is structurally impossible. A document
without real headings gets an EMPTY toc — honest emptiness over
fabricated structure.

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

# Markdown heading: # to ####, short line.
_MD_HEADING = re.compile(r"^(#{1,4})\s+(\S.{1,%d})$" % _MAX_TITLE)

# Numbered / lettered section heading, as court decisions and regulatory
# documents use them: "1.", "3.1.2.", "IV.", "(1)", "A." followed by a
# short title that starts with an uppercase letter or digit. The title
# must not read like a sentence fragment: no trailing comma/semicolon,
# bounded length.
_NUMBERED = re.compile(
    r"^\s{0,3}"
    r"(?P<num>(?:\d+(?:\.\d+)*\.|[IVXLC]+\.|\(?[A-Z]\)|[A-Z]\.)(?:\d+[.)])*)"
    r"\s+"
    r"(?P<title>[A-Z0-9](?:[^\n]{2,%d}?))"
    r"\s*$" % _MAX_TITLE
)

# Things the numbered pattern must never swallow: enumerated prose,
# citations, list items that end mid-sentence.
_SENTENCE_TAIL = re.compile(r"[,;:]$|\b(?:and|or|the|of|to|in|for)$", re.IGNORECASE)


def _numbered_level(num: str) -> int:
    """'3.' → 1, '3.1.' → 2, '3.1.2.' → 3; roman/letter markers → 1."""
    parts = [p for p in re.split(r"[.)]", num) if p]
    if len(parts) == 1 and not parts[0].isdigit():
        return 1
    return len(parts)


def derive_toc(content: str) -> list[dict]:
    """Parse verbatim headings out of markdown content. Deterministic,
    no model involved. Returns [] when the document has no recognizable
    heading structure."""
    if not content or not content.strip():
        return []
    lines = content.split("\n")
    entries: list[dict] = []
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
            entries.append(
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
            entries.append(
                {
                    "level": _numbered_level(m.group("num")),
                    "title": f"{m.group('num')} {title}".strip(),
                    "line": i,
                }
            )
    if len(entries) > _MAX_ENTRIES:
        # not a heading structure — a numbered list masquerading as one
        return []

    # close ranges: an entry ends where the next same-or-higher level starts
    total = len(lines)
    for idx, e in enumerate(entries):
        end = total
        for later in entries[idx + 1 :]:
            if later["level"] <= e["level"]:
                end = later["line"] - 1
                break
        e["line_to"] = end
    return entries

# ── content normalization (upload-time) ────────────────────────────────────

# A document is "wall-of-text" when its lines are this dense on average —
# seen with CourtListener plain_text (53K chars in ~1 line) and some
# EUR-Lex court documents. Line-based navigation (this module, and
# read_document_content's line_from/line_to) is meaningless there.
_DENSITY_THRESHOLD = 1000  # avg chars per line
_WRAP_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u00c0-\u00dc(\[])")


def normalize_line_density(content: str) -> str:
    """Re-wrap pathologically dense text into sentence-per-line form.

    Applied at UPLOAD time, before any extraction, so character spans and
    line numbers recorded later are consistent with the stored content.
    Normal documents (density under the threshold) pass through unchanged
    — this never touches content that already has line structure.
    """
    if not content:
        return content
    lines = content.split("\n")
    density = len(content) / max(1, len(lines))
    if density <= _DENSITY_THRESHOLD:
        return content
    out: list[str] = []
    for line in lines:
        if len(line) <= _DENSITY_THRESHOLD:
            out.append(line)
            continue
        out.extend(_WRAP_BOUNDARY.split(line))
    return "\n".join(out)
