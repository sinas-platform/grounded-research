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
from collections import Counter
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

# A word shared by more than this many document names has stopped telling one
# from another, so writing it names nothing. Three, because one dispute
# usually yields two to four documents — a judgment, its appeal, an opinion —
# and a name should still identify its own family while failing to identify
# one spread across separate disputes. Measured on this corpus: `Hungryhouse`
# names 1, `Akcros` 3, `Nexans` 5 across two unrelated litigations, `Casino`
# 68.
MAX_NAME_SHARE = 3


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
    name: str | None = None


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


# A name is written as a proper noun, so a capital and four characters is what
# separates a party from the ordinary vocabulary of a title. It is a weaker
# signal than an identifier and deliberately so: it is only ever consulted to
# EXCUSE a claim, never to accuse one.
_NAME_TOKEN = re.compile(r"\b[A-ZÀ-Þ][\w'\-]{3,}")


def _tokens(name: str | None) -> set[str]:
    return set(_NAME_TOKEN.findall(name or ""))


def distinguishing(name: str | None, common: frozenset[str]) -> set[str]:
    """The words of a name that could tell one document from another.

    `common` is the words the corpus uses so widely that writing one says
    nothing about which document is meant. It is counted from the corpus
    rather than listed here, because the words that carry no information are
    a fact about a body of documents and not about language: this corpus
    wears out `Commission`, `relative` and `société`, and another would wear
    out something else entirely.
    """
    return {w for w in _tokens(name) if w not in common}


def names_in_prose(
    text: str,
    source: "Source",
    others: "tuple[Source, ...] | list[Source]",
    common: frozenset[str],
) -> bool:
    """Whether the claim names this source by name rather than by identifier.

    House style names a judgment by its parties, and a reader given `Just
    Eat/Hungryhouse` can follow it. Demanding the docket number as well is
    pedantry, and the naming check made exactly that demand.

    A word shared with another source the same answer cites does not count.
    Two Nexans judgments in one answer mean that writing `Nexans` does not say
    which, and a check that accepted it would excuse the claim on the strength
    of a word that leaves the reader where they started.

    A class declaring no name property has no name here, and the check falls
    back to the identifier alone, which is where it was before this existed.
    """
    mine = distinguishing(source.name, common)
    if not mine:
        return False
    hit = {w for w in mine if re.search(rf"\b{re.escape(w)}\b", text)}
    if not hit:
        return False
    shared: set[str] = set()
    for other in others:
        if other.key != source.key:
            shared |= hit & _tokens(other.name)
    return bool(hit - shared)


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
    common_names: frozenset[str] = frozenset(),
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

    # Every source the answer cites, so that a name shared with one of them can
    # be recognised as saying nothing. Answer scope rather than claim scope:
    # the reader following a name has the whole answer in front of them.
    cited = tuple({s.key: s for ss in sources.values() for s in ss}.values())

    def told(claim: Claim, source: Source) -> bool:
        """Whether this claim tells the reader which source it is relying on,
        by identifier or by name."""
        return carries_identifier(claim.text, source.identifiers) or names_in_prose(
            claim.text, source, cited, common_names
        )

    findings: list[Finding] = []
    for entries in attributing.values():
        named = any(told(claim, source) for claim, source in entries)
        first_claim, source = entries[0]
        if len(entries) > 1 and not named:
            findings.append(
                Finding("unanchored_chain", source, tuple(c.seq for c, _ in entries))
            )
        elif not told(first_claim, source):
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
           dc.attribution_cues, pv.value->>'_' as identifier,
           dc.identifier_pattern, nv.value->>'_' as name
      from answer_claim ac
      join claim_evidence ce on ce.claim_id = ac.id
      join document d on d.id = ce.document_id
      join document_class dc on dc.id = d.document_class_id
      join document_class_property p
        on p.document_class_id = dc.id and p.name = dc.identifier_property
      join property_value pv
        on pv.document_id = d.id and pv.property_id = p.id
      left join document_class_property np
        on np.document_class_id = dc.id and np.name = dc.name_property
      left join property_value nv
        on nv.document_id = d.id and nv.property_id = np.id
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
           pv.value->>'_' as identifier, dc.identifier_pattern,
           nv.value->>'_' as name
      from answer_claim ac
      join claim_evidence ce on ce.claim_id = ac.id
      join document d on d.id = ce.document_id
      join document_class dc on dc.id = d.document_class_id
      join document_class_property p
        on p.document_class_id = dc.id and p.name = dc.identifier_property
      join property_value pv
        on pv.document_id = d.id and pv.property_id = p.id
      left join document_class_property np
        on np.document_class_id = dc.id and np.name = dc.name_property
      left join property_value nv
        on nv.document_id = d.id and nv.property_id = np.id
     where ac.answer_id = :answer_id
       and dc.identifier_property is not null
       and dc.identifier_pattern is not null
    """
)


def _assemble(rows) -> tuple[list[Claim], dict[int, list[Source]]]:
    """Rows of (sequence, claim text, document id, filename, identifier,
    identifier pattern, name) as
    the claims of an answer and the sources each one cites.

    One row per claim, document and identifier, so a claim citing two passages
    of one document arrives twice and becomes one source.
    """
    claims: dict[int, Claim] = {}
    labels: dict[str, str] = {}
    shapes: dict[str, str | None] = {}
    names: dict[str, str | None] = {}
    identifiers: dict[tuple[int, str], list[str]] = {}
    for sequence, text, doc_id, filename, identifier, pattern, name in rows:
        claims[sequence] = Claim(sequence, text or "")
        labels[doc_id] = filename
        shapes[doc_id] = pattern
        names[doc_id] = name
        if identifier:
            identifiers.setdefault((sequence, doc_id), []).extend(
                _identifier_values(identifier)
            )
    sources: dict[int, list[Source]] = {}
    for (sequence, doc_id), values in identifiers.items():
        sources.setdefault(sequence, []).append(
            Source(doc_id, tuple(dict.fromkeys(values)), labels[doc_id],
                   shapes.get(doc_id), names.get(doc_id))
        )
    return list(claims.values()), sources


_NAMES = sa.text(
    """
    select nv.value->>'_'
      from document d
      join document_class dc on dc.id = d.document_class_id
      join document_class_property np
        on np.document_class_id = dc.id and np.name = dc.name_property
      join property_value nv
        on nv.document_id = d.id and nv.property_id = np.id
     where dc.name_property is not null
    """
)

_common_names: frozenset[str] | None = None


async def common_names(refresh: bool = False) -> frozenset[str]:
    """The words too widely shared across document names to identify one.

    Counted from the corpus rather than listed, because which words carry no
    information is a fact about a body of documents and not about language.
    This corpus wears out `Commission`, `relative` and `société`; a corpus of
    clinical trials would wear out something else, and a list written here
    would be wrong for both.

    Read once per process and held. The count moves only as documents are
    ingested, a word crossing the threshold changes one finding rather than
    the answer's shape, and the alternative is reading every name in the
    corpus on every answer. `refresh` is for a caller that has just ingested
    and wants the count to reflect it.

    Empty when no class declares a name property, which is the default. Every
    source then has no name, `names_in_prose` returns False for all of them,
    and the check is left exactly where it was.
    """
    global _common_names
    if _common_names is not None and not refresh:
        return _common_names
    counts: Counter[str] = Counter()
    async with AsyncSessionLocal() as session:
        for (name,) in (await session.execute(_NAMES)).all():
            counts.update(_tokens(name))
    _common_names = frozenset(
        word for word, seen in counts.items() if seen > MAX_NAME_SHARE
    )
    return _common_names


async def findings_for(answer_id: uuid.UUID) -> list[Finding]:
    """Findings for a published-or-drafting answer. Returns nothing at all when
    no document class opts in, which is the default."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_LOAD, {"answer_id": answer_id})).all()
    cues: set[str] = set()
    for row in rows:
        cues.update(c.lower() for c in (row[4] or []) if c)
    claims, sources = _assemble(
        [(r[0], r[1], r[2], r[3], r[5], r[6]) for r in rows]
    )
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
        # Said here, not only in the log. A check that crashed returns an
        # empty list, and an empty list is what a clean answer returns, so
        # swallowing it silently makes a broken check indistinguishable from
        # a passing one. That is the same defect as a check that was never
        # configured, and it wants the same remedy: say so in the channel
        # that carries the findings.
        log.exception("naming check failed for answer %s", answer_id)
        return [
            "The check that a claim naming a source also names it in the "
            "prose did not run on this answer: it failed. No claim was "
            "examined. This is not a finding of none."
        ]
    return [message(f) for f in found[:MAX_FINDINGS]]


_UNSHAPED = sa.text(
    """
    select name from document_class
     where identifier_property is not null
       and identifier_pattern is null
     order by name
    """
)


async def unshaped_classes() -> list[str]:
    """Classes that opt into the correspondence check and cannot be checked.

    A class declaring `identifier_property` and no `identifier_pattern` has
    said which property identifies its documents and not what an identifier
    looks like, so the check has nothing to read prose with and returns
    nothing for it.

    This exists because returning nothing is what a clean answer also looks
    like. The same shape has cost us four times: a class pointing at a
    property it did not define, a word list excluding half the pairs it could
    reach, a size limit cutting a ranked list with no marker, and this one in
    the window between a migration and a re-import. Every one is a
    precondition failing, the check returning empty, and empty being
    indistinguishable from a pass.

    So the rule this encodes is narrow: A CHECK THAT CANNOT RUN MUST SAY SO IN
    THE SAME CHANNEL THAT CARRIES ITS FINDINGS. A telemetry key is a sibling
    channel, read only by someone who already suspects. The caller puts this
    into `issues`, which the gate record, the reviser's feedback and the run
    export all already carry.

    The package refuses this combination at import, so in steady state the
    list is empty. It is not empty between a migration adding the column and
    the re-import that fills it, which is exactly when nobody is looking.
    """
    async with AsyncSessionLocal() as session:
        return [r[0] for r in (await session.execute(_UNSHAPED)).all()]


async def unshaped_message() -> str | None:
    """One line for `issues`, or None when every opted-in class can be read."""
    try:
        names = await unshaped_classes()
    except Exception:
        log.warning("unshaped-class check failed", exc_info=True)
        return None
    if not names:
        return None
    return (
        "The check that a claim names the source it cites did not run for "
        + ", ".join(names)
        + ": the class declares which property identifies its documents but "
        "not what an identifier looks like, so no claim of these classes was "
        "examined. This is not a finding of none."
    )


async def safe_mismatches_for(
    answer_id: uuid.UUID,
) -> tuple[list[Mismatch], str | None]:
    """Every mismatch in this answer, and never an exception.

    Returns the findings and, when the check could not run, a line for
    `issues`. Never raises into the caller, for the same reason `issues_for`
    does not: this is a quality note, and an answer that is otherwise
    publishable must not be held up because a check failed. But it does not
    fail silently either — an empty list and a crash are the same bytes to
    every reader, so the crash is announced where the findings go.

    Returns the findings rather than the feedback strings, and returns all of
    them rather than `MAX_FINDINGS` of them. The cap exists to bound what the
    reviser is asked to read in one round, and applies where the strings are
    built. A count kept for measurement wants none: the question this check is
    on trial for is how often it fires and how often it is right, and a
    truncated record cannot answer it.
    """
    try:
        return await mismatches_for(answer_id), None
    except Exception:
        log.exception("naming correspondence check failed for answer %s", answer_id)
        return [], (
            "The check that a claim names the source it cites did not run on "
            "this answer: it failed. No claim was examined. This is not a "
            "finding of none."
        )
