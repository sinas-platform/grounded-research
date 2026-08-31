"""Whether a claim names the source it relies on.

A claim that attributes a proposition to a source without naming it states
something the reader cannot check against anything: the attribution is real,
its subject is missing. The evidence checker does not catch this, because it
asks whether a claim's stated provenance is correct, and a claim that states no
provenance has nothing to be wrong about.

Deterministic, and no model call. What a claim says and what its sources are
called are both already in the database; comparing them needs no judgement.

The rule is per source per answer, not per claim. Only the first claim that
attributes something to a source has to name it. Later claims may refer back to
it, which is ordinary writing, and flagging them would make the check fire on
every sentence of a well-written passage.

Two shapes are reported, separately:

  unnamed_first_mention   the first attributing claim does not carry the
                          source's identifier;
  unanchored_chain        several claims attribute to the same source and none
                          of them carries its identifier, so the whole passage
                          rests on a source the reader is never told.

The second is the worse defect: one unnamed attribution is a sentence to fix,
a chain of them is a passage with no visible foundation. A source is reported
under one shape or the other, never both.

Only the identifier is checked. A house style may also want the source's name
in prose, and that is a reasonable thing to want, but a name cannot be verified
mechanically: labels arrive in several shapes, some truncated, and some sources
carry no name at all. Demanding one would put the check's accuracy at the mercy
of how a corpus happens to be labelled. So the identifier is the condition, and
the source's label rides along in the finding as material for the fix.

Nothing here knows what a source is. A document class declares the property
that identifies its documents and the words that mark a claim as attributing;
a class declaring neither is not checked, which is the default.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

import sqlalchemy as sa

from app.db import AsyncSessionLocal

log = logging.getLogger(__name__)

# An identifier has to be this long, once stripped to its distinguishing part,
# before its presence in a claim means anything. Below it, matching is chance:
# a two-character remainder occurs in ordinary prose and in unrelated numbers.
MIN_CORE = 3

# How many findings are worth returning. The reviser is given a bounded amount
# of feedback per round, and a long tail of naming notes would crowd out the
# defects that block publication.
MAX_FINDINGS = 5


@dataclass(frozen=True)
class Source:
    """A document a claim cites, and what identifies it."""

    key: str
    identifiers: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class Claim:
    seq: int
    text: str


@dataclass(frozen=True)
class Finding:
    kind: str
    source: Source
    seqs: tuple[int, ...]

    @property
    def seq(self) -> int:
        return self.seqs[0]


def identifier_core(value: str) -> str:
    """The distinguishing part of an identifier, for comparison.

    Identifiers are written with a prefix that varies by who is writing: the
    same thing appears with and without its scheme, its issuing body, or its
    punctuation. Stripping the leading non-digits and keeping the first run
    leaves the part that actually distinguishes one from another, so a claim
    that writes the identifier in a different style still matches.
    """
    stripped = re.sub(r"^[^\d]+", "", value.strip())
    return stripped.split(" ")[0].strip()


def carries_identifier(text: str, identifiers: tuple[str, ...]) -> bool:
    """Whether the claim writes any of these identifiers.

    Compared with whitespace removed, so an identifier broken across a line or
    spaced differently still counts.
    """
    squashed = re.sub(r"\s+", "", text)
    for identifier in identifiers:
        core = identifier_core(identifier)
        if len(core) >= MIN_CORE and re.sub(r"\s+", "", core) in squashed:
            return True
    return False


def attributes(text: str, cues: frozenset[str]) -> bool:
    """Whether the claim attributes rather than describes.

    A cue matches as a whole word, so a cue that is a common substring does not
    fire on every claim that happens to contain it.
    """
    if not cues:
        return False
    words = set(re.findall(r"[^\W\d_]+", text.lower()))
    return bool(words & cues)


def review(
    claims: list[Claim],
    sources: dict[int, list[Source]],
    cues: frozenset[str],
) -> list[Finding]:
    """Findings for one answer. Pure: no I/O, no ordering assumptions beyond
    claim sequence, which is what "first mention" is defined against."""
    attributing: dict[str, list[tuple[Claim, Source]]] = {}
    seen: set[tuple[int, str]] = set()
    for claim in sorted(claims, key=lambda c: c.seq):
        if not attributes(claim.text, cues):
            continue
        for source in sources.get(claim.seq, ()):
            # One claim can hold several pieces of evidence from the same
            # document. That is one claim relying on one source, not a chain of
            # them, so the pair is counted once.
            if not source.identifiers or (claim.seq, source.key) in seen:
                continue
            seen.add((claim.seq, source.key))
            attributing.setdefault(source.key, []).append((claim, source))

    findings: list[Finding] = []
    for entries in attributing.values():
        named = any(
            carries_identifier(claim.text, source.identifiers)
            for claim, source in entries
        )
        first_claim, source = entries[0]
        if len(entries) > 1 and not named:
            findings.append(
                Finding("unanchored_chain", source, tuple(c.seq for c, _ in entries))
            )
        elif not carries_identifier(first_claim.text, source.identifiers):
            findings.append(
                Finding("unnamed_first_mention", source, (first_claim.seq,))
            )
    # Chains first: they are the worse defect, and the cap below is a real cut.
    findings.sort(key=lambda f: (f.kind != "unanchored_chain", f.seq))
    return findings


def message(finding: Finding) -> str:
    """The finding as feedback the reviser can act on.

    It names the identifier to write, because a reviser that is told a claim is
    unnamed and not told the name changes nothing. It says to name the source
    where it is first relied on rather than in every claim, so the fix does not
    turn into a repetition.
    """
    identifiers = ", ".join(finding.source.identifiers[:3])
    if finding.kind == "unanchored_chain":
        seqs = ", ".join(str(s) for s in finding.seqs)
        return (
            f"Claims {seqs} all attribute to {finding.source.label} and none of "
            f"them identifies it, so the passage rests on a source the reader is "
            f"never given. Identify it ({identifiers}) in claim {finding.seq}, "
            f"where it is first relied on."
        )
    return (
        f"Claim {finding.seq} attributes to {finding.source.label} without "
        f"identifying it. Give its identifier ({identifiers}) here, where the "
        f"source is first relied on."
    )


_LOAD = sa.text(
    """
    select ac.sequence, ac.claim_text, d.id::text, d.filename,
           dc.attribution_cues, pv.value->>'_' as identifier
      from answer_claim ac
      join claim_evidence ce on ce.claim_id = ac.id
      join document d on d.id = ce.document_id
      join document_class dc on dc.id = d.document_class_id
      join document_class_property p
        on p.document_class_id = dc.id and p.name = dc.identifier_property
      join property_value pv
        on pv.document_id = d.id and pv.property_id = p.id
     where ac.answer_id = :answer_id
       and dc.identifier_property is not null
       and dc.attribution_cues is not null
    """
)


async def findings_for(answer_id: uuid.UUID) -> list[Finding]:
    """Findings for a published-or-drafting answer. Returns nothing at all when
    no document class opts in, which is the default."""
    claims: dict[int, Claim] = {}
    sources: dict[int, list[Source]] = {}
    identifiers: dict[tuple[int, str], list[str]] = {}
    labels: dict[str, str] = {}
    cues: set[str] = set()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_LOAD, {"answer_id": answer_id})).all()
    for sequence, text, doc_id, filename, class_cues, identifier in rows:
        claims[sequence] = Claim(sequence, text or "")
        labels[doc_id] = filename
        cues.update(c.lower() for c in (class_cues or []) if c)
        if identifier:
            identifiers.setdefault((sequence, doc_id), []).extend(
                _identifier_values(identifier)
            )
    for (sequence, doc_id), values in identifiers.items():
        sources.setdefault(sequence, []).append(
            Source(doc_id, tuple(dict.fromkeys(values)), labels[doc_id])
        )
    return review(list(claims.values()), sources, frozenset(cues))


def _identifier_values(raw: str) -> list[str]:
    """A property value is one identifier or a list of them."""
    text = raw.strip()
    if not text.startswith("["):
        return [text] if text else []
    try:
        import json

        return [str(x) for x in json.loads(text) if str(x).strip()]
    except (ValueError, TypeError):
        return []


async def issues_for(answer_id: uuid.UUID) -> list[str]:
    """Feedback strings for the gate's `issues` list.

    Never raises into the caller: this is a quality note, and an answer that is
    otherwise publishable must not be held up because a naming check failed.
    """
    try:
        found = await findings_for(answer_id)
    except Exception:
        log.exception("naming check failed for answer %s", answer_id)
        return []
    return [message(f) for f in found[:MAX_FINDINGS]]
