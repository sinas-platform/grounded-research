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

A third shape is checked separately, by `mismatches`, and is not silence but
its opposite. A claim can name a case and rest on a different one: the reader
is given an identifier, follows it, and finds a judgment that does not contain
the passage. That is the defect legal reviewers report most often, and it is
worse than an unnamed source, because a missing name asks the reader to do
work while a wrong one sends them somewhere.

It is deliberately not folded into the checks above. Those ask what a claim
fails to say, so they need to know whether it was attributing at all, which is
what the cue vocabulary is for. This one reads what the claim does say. A
written case number is itself the attribution, so no cue is required and none
is consulted, which is what lets it reach a claim that attributes with "per
paragraph 87 of" rather than with a verb the cue list happens to hold.

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
    """A document a claim cites, and what identifies it.

    `pattern` is the identifier shape its class declares, or None where the
    class declares none. It rides on the source rather than on the answer
    because one claim can cite documents of different classes, and each is
    read with its own class's shape.
    """

    key: str
    identifiers: tuple[str, ...]
    label: str
    pattern: str | None = None


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


@dataclass(frozen=True)
class Mismatch:
    """A claim that names one case and rests on another."""

    seq: int
    named: tuple[str, ...]
    cited: tuple[str, ...]
    labels: tuple[str, ...]


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


# How much separator may sit between two characters of one identifier. Enough
# for `. ` or a hyphen or a line break, and not enough to reach across a
# clause and assemble an identifier out of unrelated digits.
_GAP = r"[^0-9A-Za-z]{0,3}"


def _written_as(core: str) -> re.Pattern[str]:
    """A pattern matching this identifier however it is punctuated.

    Separators are style, not identity: the same decision is written `AT.39796`
    by the corpus and `COMP/39.796` in prose, and comparing the two literally
    said that a claim naming its source did not name it.

    The permission is given to the identifier rather than taken from the
    claim. Stripping the separators out of the claim instead would run two
    identifiers written side by side into a single number and match neither,
    which `Nos. 85-4053, 85-4068` does in the corpus today.

    A digit either side refuses the match, so a short identifier cannot be
    read out of the middle of a longer number: `111/22` is not written by
    `2011122`.
    """
    return re.compile(
        r"(?<![0-9])" + _GAP.join(re.escape(c) for c in core) + r"(?![0-9])"
    )


def carries_identifier(text: str, identifiers: tuple[str, ...]) -> bool:
    """Whether the claim writes any of these identifiers."""
    for identifier in identifiers:
        core = re.sub(r"[^0-9A-Za-z]+", "", identifier_core(identifier))
        if len(core) >= MIN_CORE and _written_as(core).search(text):
            return True
    return False


# The shape of an identifier is deployment knowledge, not platform knowledge.
# A court case number, a merger reference, an invoice number and a bug id are
# all identifiers, and nothing here can be written to know any of them without
# naming a deployment in a repo that must not name one. So the shape arrives
# from configuration, as `attribution_cues` already does: a class declares
# `identifier_pattern` beside `identifier_property`, and a class that declares
# none is not judged by this check.
#
# The pattern's CAPTURE GROUPS are the comparison key. That is the whole
# contract, and it is what lets a deployment say which differences matter
# without SGR knowing why: capture the parts that identify, leave out the
# parts that decorate. A deployment whose identifiers carry a procedural
# suffix that does not change identity simply does not capture it, and two
# values differing only by that suffix then compare equal here without this
# module knowing what a suffix is.
_PATTERNS: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str]:
    """The class's pattern, compiled once. Raises on a pattern that will not
    compile, which the package schema refuses at import so it cannot arrive
    here from a validated package."""
    got = _PATTERNS.get(pattern)
    if got is None:
        got = _PATTERNS[pattern] = re.compile(pattern)
    return got


def _key(match: re.Match[str]) -> str:
    """The comparison key of one match: its capture groups, or the whole match
    when the pattern captures nothing.

    Case-folded and stripped, because those are differences of typing rather
    than of identity in every scheme. Nothing else is normalised: a deployment
    that wants two spellings to compare equal writes a pattern that captures
    them the same way, which keeps the judgement where the knowledge is.
    """
    groups = [g for g in match.groups() if g is not None] or [match.group(0)]
    return "\u0000".join(g.strip().upper() for g in groups)


def identifiers_named(text: str, pattern: str) -> dict[str, str]:
    """The identifiers a claim writes, as key -> the text that wrote it.

    The text is kept for the message: a reader is told what the claim says,
    not the key it reduced to.
    """
    return {_key(m): m.group(0) for m in _compiled(pattern).finditer(text)}


def identifier_key(value: str, pattern: str) -> str | None:
    """The key of a stored identifier, or None if it does not match the shape.

    Anchored, so a value has to BE an identifier rather than merely contain
    something shaped like one. Being strict here can only shrink the set a
    claim is judged against, which loses a finding rather than inventing one.
    """
    m = _compiled(pattern).match(value.strip())
    return _key(m) if m else None


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


def mismatches(
    claims: list[Claim],
    sources: dict[int, list[Source]],
) -> list[Mismatch]:
    """Claims that name a case and cite none of the cases they name. Pure.

    Judged per claim, not per source. A claim that rests on a judgment and its
    appeal, or on joined cases filed separately, names one of them and means
    both; per source the unnamed one would fire on ordinary writing. Naming a
    second case beside the cited one is ordinary too, and common: an appeal
    relation, a case the cited judgment itself cites, a case being
    distinguished. What none of those does is leave the cited case unwritten.
    So the condition is not that the claim names something extra, but that it
    names nothing it cites.

    A claim citing no document with a readable case number is not judged. It
    has nothing to be compared against, and a book chapter or a merger
    reference may discuss whatever cases it likes.
    """
    found: list[Mismatch] = []
    for claim in sorted(claims, key=lambda c: c.seq):
        here = sources.get(claim.seq, ())
        # One claim can cite two classes with different shapes, so the prose is
        # read once per distinct shape and the results unioned.
        shapes = {s.pattern for s in here if s.pattern}
        if not shapes:
            continue
        named: dict[str, str] = {}
        for shape in shapes:
            named.update(identifiers_named(claim.text, shape))
        if not named:
            continue
        # key -> (what the source calls it, which document), so the message
        # can show a reader the identifier rather than the key it reduced to.
        cited: dict[str, tuple[str, str]] = {}
        for source in here:
            if not source.pattern:
                continue
            for identifier in source.identifiers:
                key = identifier_key(identifier, source.pattern)
                if key:
                    cited.setdefault(key, (identifier, source.label))
        if not cited or set(named) & set(cited):
            continue
        found.append(
            Mismatch(
                seq=claim.seq,
                named=tuple(sorted(named.values())),
                cited=tuple(sorted(v[0] for v in cited.values())),
                labels=tuple(dict.fromkeys(v[1] for v in cited.values())),
            )
        )
    return found


def mismatch_message(mismatch: Mismatch) -> str:
    """The mismatch as feedback the reviser can act on.

    Both halves are given because either one can be the wrong half. The claim
    may rest on the right judgment and cite it under the wrong number, or it
    may have taken a passage from one judgment and attributed it to another it
    never read. Only the reviser, holding the passage, can tell which, so both
    repairs are offered rather than one prescribed.
    """
    named = ", ".join(mismatch.named)
    cited = ", ".join(mismatch.cited)
    labels = ", ".join(mismatch.labels)
    return (
        f"Claim {mismatch.seq} names {named}, but the evidence bound to it is "
        f"{cited} ({labels}). A reader who follows {named} will not find the "
        f"passage the claim rests on. Either cite {named}, if that is the "
        f"judgment the point comes from, or attribute the point to {cited}, "
        f"which is what was actually read."
    )


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


# The same rows without the cue predicate. The correspondence check reads what
# a claim says rather than what it fails to say, so it needs no cue vocabulary
# and must not inherit one: a class that identifies its documents but declares
# no cues is out of scope for `review` and in scope here. Kept as a second
# statement rather than a relaxed `_LOAD`, so that widening this check's reach
# cannot quietly widen the other's.
_LOAD_IDENTIFIED = sa.text(
    """
    select ac.sequence, ac.claim_text, d.id::text, d.filename,
           pv.value->>'_' as identifier, dc.identifier_pattern
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
       and dc.identifier_pattern is not null
    """
)


def _assemble(rows) -> tuple[list[Claim], dict[int, list[Source]]]:
    """Rows of (sequence, claim text, document id, filename, identifier,
    identifier pattern) as
    the claims of an answer and the sources each one cites.

    One row per claim, document and identifier, so a claim citing two passages
    of one document arrives twice and becomes one source.
    """
    claims: dict[int, Claim] = {}
    labels: dict[str, str] = {}
    shapes: dict[str, str | None] = {}
    identifiers: dict[tuple[int, str], list[str]] = {}
    for sequence, text, doc_id, filename, identifier, pattern in rows:
        claims[sequence] = Claim(sequence, text or "")
        labels[doc_id] = filename
        shapes[doc_id] = pattern
        if identifier:
            identifiers.setdefault((sequence, doc_id), []).extend(
                _identifier_values(identifier)
            )
    sources: dict[int, list[Source]] = {}
    for (sequence, doc_id), values in identifiers.items():
        sources.setdefault(sequence, []).append(
            Source(doc_id, tuple(dict.fromkeys(values)), labels[doc_id],
                   shapes.get(doc_id))
        )
    return list(claims.values()), sources


async def findings_for(answer_id: uuid.UUID) -> list[Finding]:
    """Findings for a published-or-drafting answer. Returns nothing at all when
    no document class opts in, which is the default."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_LOAD, {"answer_id": answer_id})).all()
    cues: set[str] = set()
    for row in rows:
        cues.update(c.lower() for c in (row[4] or []) if c)
    claims, sources = _assemble([(r[0], r[1], r[2], r[3], r[5]) for r in rows])
    return review(claims, sources, frozenset(cues))


async def mismatches_for(answer_id: uuid.UUID) -> list[Mismatch]:
    """Claims of this answer that name a case they do not cite."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(_LOAD_IDENTIFIED, {"answer_id": answer_id})
        ).all()
    claims, sources = _assemble(rows)
    return mismatches(claims, sources)


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


async def safe_mismatches_for(answer_id: uuid.UUID) -> list[Mismatch]:
    """Every mismatch in this answer, and never an exception.

    Never raises into the caller, for the same reason `issues_for` does not:
    this is a quality note, and an answer that is otherwise publishable must
    not be held up because a check failed.

    Returns the findings rather than the feedback strings, and returns all of
    them rather than `MAX_FINDINGS` of them. The cap exists to bound what the
    reviser is asked to read in one round, and applies where the strings are
    built. A count kept for measurement wants none: the question this check is
    on trial for is how often it fires and how often it is right, and a
    truncated record cannot answer it.
    """
    try:
        return await mismatches_for(answer_id)
    except Exception:
        log.exception("naming correspondence check failed for answer %s", answer_id)
        return []
