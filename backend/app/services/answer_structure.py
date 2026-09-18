"""The shape of an answer: parts, sections, budgets, source trust.

Pure helpers behind the structured answer. The query runner decides WHEN to
decompose, plan, draft and revise; everything here is arithmetic and string
work over what came back, so each rule is testable without a model, a
database or a run.

The rules, as the drafting contract states them:

  1. The question is decomposed into parts BEFORE planning, and claims are
     budgeted per part: at least two each, and a total that grows with the
     number of parts (12 for one part, +4 per extra part, never above 24).
  2. Sections render conclusion first — one claim per part and one overall —
     then analysis per part, then the authorities.
  3. A test a source states as two or more conditions is one claim of kind
     `test`, its conditions in the order the source states them.
  4. Every passage the drafter sees carries what its document IS: title,
     class, and the label its class declares. A source whose class declares
     a label is labelled in the answer and never carries a rule alone; the
     label, the tier, the jurisdiction note and the currency note are all
     derived here from the document, never asked of the model.
  5. Planned claims that answer no part are dropped before extraction;
     claims restating one proposition from one source are merged.
  6. A superseded instrument, or a decision with a later one in the same
     case retrieved beside it, carries a currency note.
  7. Within a part the reasoning is a chain — rule, application, inference,
     conclusion — and a step that rests on earlier claims rather than on a
     passage says which claims it follows from.

Nothing here knows a deployment's vocabulary: which annotation says the tier,
which property says the date, is read from what the deployment declared.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from app.services.declared_roles import DeclaredRoles

# ── budgets ──────────────────────────────────────────────────────────────────

#: Claims a one-part question may hold.
BASE_CLAIM_CAP = 12
#: Each part beyond the first buys this many more.
CLAIMS_PER_EXTRA_PART = 4
#: And no question buys more than this.
HARD_MAX_CLAIMS = 24
#: A part answered by fewer claims than this is thin, whatever the total.
MIN_CLAIMS_PER_PART = 2
#: And the other end: with three or more parts, the share of the answer one
#: part may take before it is crowding the rest out. Half, because a part
#: that holds more than every other part put together is no longer one of
#: several answers to one question.
MAX_PART_SHARE = 0.5
#: A decomposition longer than this is the model listing sentences.
MAX_PARTS = 8
#: Words a part's heading may run to. The splitter is asked for three to
#: seven; past this a label is a sentence rather than a heading, and is
#: shortened before it is printed under a `##`.
HEADING_MAX_WORDS = 12

# ── vocabularies ─────────────────────────────────────────────────────────────

SECTIONS = ("conclusion", "analysis", "authority")
#: Render order of the sections; a claim with no section sorts last.
_SECTION_RANK = {s: i for i, s in enumerate(SECTIONS)}
#: What a claim DOES in the answer. Six kinds say how the claim stands to
#: its source — it states a general proposition the source lays down
#: (`rule`), applies one to the matter at hand (`application`), reports
#: something that happened or is the case (`fact`), reports how a process ran
#: (`procedure`), reasons from other claims (`inference`), or answers the
#: question (`conclusion`) — plus `abstention`, which says the sources do not
#: answer it. Two more are structural rather than evidential: `test`, a rule
#: stated as ordered conditions, and `label`, which only says what a source
#: is.
#:
#: These are the ENGINE's words, not a deployment's. The first three used to
#: be `legal_principle`, `factual` and `procedural`, which made the engine's
#: stored vocabulary the property of one kind of corpus: an engine over
#: maintenance reports or clinical guidance has rules and applications and
#: facts, and nothing it would call a legal principle. A deployment that
#: wants its readers to see its own words for these renames them where it
#: renders, which is the only place the word is read by a person — see
#: `CLAIM_KIND_GLOSS`, the wording the drafter is given.
CLAIM_KINDS = ("rule", "application", "fact", "procedure", "conclusion",
               "abstention", "test", "label", "inference")
#: The default when the drafter names no kind, or names one the contract does
#: not have. A claim asserting something a source lays down is the ordinary
#: case, and the one that costs least when it is wrong: it lands in the
#: analysis section, which is where an unclassified claim belongs.
DEFAULT_CLAIM_KIND = "rule"
#: What each kind is, in one clause — the words the drafting contract uses
#: and the only place these names are explained. Kept beside the tuple so a
#: kind cannot be added without saying what it means.
CLAIM_KIND_GLOSS: dict[str, str] = {
    "rule": "a general proposition the source lays down",
    "application": "that proposition applied to the matter at hand",
    "fact": "something the source reports as having happened or being so",
    "procedure": "how a process ran — who did what, when, before whom",
    "conclusion": "the answer to the question, or to one part of it",
    "abstention": "that the sources do not answer the question",
    "test": "a rule stated as two or more ordered conditions",
    "label": "what a source is, said about the source itself",
    "inference": "a step that reasons from other claims, not from a passage",
}
#: Kinds that may stand without a span of their own, resting on the claims
#: they follow from. An abstention rests on nothing, by design.
DERIVED_KINDS = ("inference", "conclusion")
#: Kinds whose claim leads its part rather than reasons within it, and the
#: kind that only says what a source is. Everything else is the analysis.
_CONCLUDING_KINDS = ("conclusion", "abstention")
_LABELLING_KINDS = ("label",)
#: Heading for cited documents whose class is unknown. The only source name
#: in this module, and it names the absence of one.
UNCLASSIFIED_HEADING = "Unclassified"
#: A legislation `status` value in this set means the instrument is not
#: current law. Declared by the deployment on the class as a property; these
#: two words are the contract's, not a deployment's.
STALE_STATUSES = ("repealed", "superseded")

#: The shape of a tier value, whichever annotation carries it: `{"depth": n}`
#: from a `length` reducer over a standing walk, or a bare integer.
#:
#: WHICH annotation carries it is not decided here and is not a name in engine
#: code. The caller passes the annotations resolved from what the deployment
#: declared — an annotation carrying the `standing_tier` role, else one whose
#: path walks a relation declared `standing`; both are resolved together in
#: `app.services.declared_roles.resolve`. What is left below is the fallback
#: for a deployment that has declared neither: the engine-contract name, kept
#: so that such a deployment keeps the tier it has today, and documented
#: rather than hidden. A deployment that declares either may call its
#: annotation anything.
#:
#: The issuing body had a name here too and has none now: it never had a
#: documented fallback, only a literal, so a deployment that calls it anything
#: else had the field silently empty. It is read from the annotation declared
#: `issuing_body`, and where nothing declares one the absence is announced
#: where it is resolved rather than guessed at here.
TIER_ANNOTATION = "authority_tier"


def claim_cap(n_parts: int) -> int:
    """How many claims a question with `n_parts` parts may hold. Pure."""
    n = max(1, int(n_parts or 0))
    return min(HARD_MAX_CLAIMS, BASE_CLAIM_CAP + CLAIMS_PER_EXTRA_PART * (n - 1))


# ── question parts ───────────────────────────────────────────────────────────

def parse_parts(data: Any) -> list[dict]:
    """The decomposition reply as [{index, label, text}], or [] if unusable.

    Strict on shape: an element that is not an object with a non-empty
    `text` makes the whole reply unusable rather than being dropped, because
    a lost part is the defect the decomposition exists to stop — the caller
    repairs once and then falls back. The label is the heading the splitter
    wrote; a missing one is derived from the text, since a heading is a
    convenience and a part is not.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("parts")
    if not isinstance(raw, list) or not raw:
        return []
    out: list[dict] = []
    for x in raw:
        if not isinstance(x, dict):
            return []
        text = str(x.get("text") or "").strip()
        if not text:
            return []
        label = part_heading(x.get("label"), text)
        out.append({"index": len(out), "label": label[:300], "text": text[:1000]})
        if len(out) >= MAX_PARTS:
            break
    return out


#: Where a sentence's opening clause ends. A heading derived from a sentence
#: stops here rather than at a word count, so what is printed is a phrase and
#: not a fragment.
_CLAUSE_BREAK = re.compile(r"\s*[,;:(\[–—]|\s+-\s+")


def part_heading(label: Any, text: Any = "") -> str:
    """A part's heading: a short phrase a reader scans. Pure.

    The splitter writes a heading and the part's full text separately, so the
    ordinary case is the label printed exactly as it stands. What this is for
    is the label that is really a sentence — every row written before the
    splitter wrote headings carries the part's first ten words and an
    ellipsis, which renders under a `##` as a sentence cut off mid-phrase.
    Such a label falls back to the clause it opens with, whole, and never to
    an ellipsis: a heading that trails off tells the reader nothing about
    where it was going.

    `text` is used only when there is no label at all, so a decomposition
    that lost its headings still gets a phrase rather than a number.
    """
    s = _tidy(label) or _tidy(text)
    if not s or len(s.split()) <= HEADING_MAX_WORDS:
        return s
    words = (_tidy(_CLAUSE_BREAK.split(s, maxsplit=1)[0]) or s).split()
    if len(words) > HEADING_MAX_WORDS:
        # One long clause and no break to stop at. Cutting is the last
        # resort, so the cut does not also leave the heading hanging on a
        # joining word — dropped by length rather than by a list of words, so
        # that no language's vocabulary ends up in engine code.
        words = words[:HEADING_MAX_WORDS]
        while len(words) > 3 and len(words[-1]) <= 4:
            words.pop()
    return _tidy(" ".join(words))


#: Trailing marks a heading does not carry: the sentence punctuation and the
#: ellipsis of a label that was cut short. A question mark is left alone — a
#: heading that asks something means to.
_HEADING_TAIL = re.compile(r"[\s.,;:…—–-]+$")


def _tidy(value: Any) -> str:
    return _HEADING_TAIL.sub("", str(value or "").strip())


def part_index_of(raw: Any, n_parts: int) -> int | None:
    """A model's `part` field as a 0-based index into the parts, or None.

    Accepts the 1-based number the prompts print ("part 2"), a numeric
    string, or an explicit null/"overall"/"all" for the whole question. A
    number outside the range names no part. Pure.
    """
    if n_parts <= 0 or raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "null", "none", "overall", "all", "whole"):
            return None
        m = re.search(r"\d+", s)
        if not m:
            return None
        raw = m.group(0)
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return None
    if 1 <= n <= n_parts:
        return n - 1
    return None


def parts_block(parts: list[dict]) -> str:
    """The parts as a prompt lists them: numbered from 1, label then text."""
    return "\n".join(
        f"  part {p['index'] + 1}: {p['label']} — {p['text']}" for p in parts)


# ── planned claims ───────────────────────────────────────────────────────────

def filter_plan_to_parts(plan_claims: list[dict], parts: list[dict]
                         ) -> tuple[list[dict], list[dict]]:
    """Keep the planned claims that answer a part; return (kept, dropped).

    Each kept claim gets `part` normalised to a 0-based index (None for the
    overall conclusion). A claim that names no valid part and is not marked
    as the overall conclusion answers nothing the question asks — reading
    its anchors would be extraction spent on material the answer cannot
    use. With no decomposition nothing is judged and everything is kept:
    the filter cannot be stricter than what it knows. Pure.
    """
    if not parts:
        for c in plan_claims:
            c["part"] = None
        return list(plan_claims), []
    kept, dropped = [], []
    for c in plan_claims:
        idx = part_index_of(c.get("part"), len(parts))
        overall = _is_overall(c)
        if idx is None and not overall:
            dropped.append(c)
            continue
        c["part"] = idx
        c["conclusion"] = bool(overall or c.get("conclusion"))
        kept.append(c)
    return kept, dropped


def _is_overall(c: dict) -> bool:
    raw = c.get("part")
    if isinstance(raw, str) and raw.strip().lower() in ("overall", "all", "whole"):
        return True
    return str(c.get("kind") or c.get("section") or "").strip().lower() == "conclusion"


def part_counts(claims: list[dict], n_parts: int, key: str = "part") -> list[int]:
    """How many claims each part has, by 0-based index. Pure."""
    counts = [0] * max(0, n_parts)
    for c in claims:
        idx = c.get(key)
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < n_parts:
            counts[idx] += 1
    return counts


def thin_parts(claims: list[dict], parts: list[dict], key: str = "part") -> list[dict]:
    """The parts held by fewer than MIN_CLAIMS_PER_PART claims. Pure."""
    counts = part_counts(claims, len(parts), key)
    return [p for p in parts if counts[p["index"]] < MIN_CLAIMS_PER_PART]


def crowded_parts(claims: list[dict], parts: list[dict],
                  key: str = "part") -> list[dict]:
    """The parts holding more than their share of the answer. Pure.

    A minimum per part was enforced and a maximum was not, so the answer
    could satisfy every rule and still be lopsided: across three published
    runs the claims fell 3/4/11, 6/4/5 and 5/4/10, the last part taking half
    the answer while the others were answered at the floor. A part that
    swells that far is not thoroughness, it is the drafter continuing past
    the question — and it crowds out the parts a reader asked about first.

    The share is deliberately loose: with three or more parts no one part may
    hold more than `MAX_PART_SHARE` of the claims that were filed under a
    part. Two parts are left alone — one of two can legitimately be most of
    the answer — and so is any answer too small for a share to mean anything.
    """
    n = len(parts)
    if n < 3:
        return []
    counts = part_counts(claims, n, key)
    filed = sum(counts)
    if filed < n * MIN_CLAIMS_PER_PART:
        return []
    ceiling = filed * MAX_PART_SHARE
    return [p for p in parts if counts[p["index"]] > ceiling]


# ── drafted claims ───────────────────────────────────────────────────────────

def section_of(kind: str | None) -> str:
    """Which section a claim of this kind belongs to. Pure.

    Derived rather than asked for. The drafter used to author `section`
    beside `kind`, which is the same decision twice and could disagree with
    itself — a claim of kind `conclusion` filed under `analysis` was a gate
    finding the reviser then had to spend a call on. One field decides, and
    the other follows from it.
    """
    k = str(kind or "").strip().lower()
    if k in _CONCLUDING_KINDS:
        return "conclusion"
    if k in _LABELLING_KINDS:
        return "authority"
    return "analysis"


def normalise_claim(c: dict, parts: list[dict]) -> dict | None:
    """One drafted claim as the columns it will be stored with, or None.

    Reads the drafter's fields leniently — `kind` or `type`, `part` as a
    number or a word — and writes them strictly: every enum value is one the
    contract names or it is dropped to its default. A test claim keeps its
    conditions only when there are at least two with text; a "test" of one
    condition is a rule, and is stored as one. Pure.

    Four columns the drafter used to author are left null here and filled by
    the caller from the cited document: the section follows from the kind,
    and the authority label, the jurisdiction note and the currency note
    follow from what the document IS, which the engine already knows and the
    model had to be told in order to repeat back. `conditions` is read off
    the claim itself as well as out of a nested `test`, because the flat
    shape is what the drafter is now asked for.
    """
    text = str(c.get("text") or "").strip()
    if not text:
        return None
    kind = str(c.get("kind") or c.get("type")
               or DEFAULT_CLAIM_KIND).strip().lower()
    if kind not in CLAIM_KINDS:
        kind = DEFAULT_CLAIM_KIND
    idx = part_index_of(c.get("part"), len(parts))
    label = parts[idx]["label"] if idx is not None else None
    test = normalise_test(raw_test(c)) if kind == "test" else None
    if kind == "test" and test is None:
        kind = DEFAULT_CLAIM_KIND
    return {
        "claim_text": text[:4000],
        "claim_type": _claim_type_of(kind),
        "claim_kind": kind,
        "section": section_of(kind),
        "part_index": idx,
        "part_label": label,
        "test": test,
        # Filled by the caller from the document the claim cites. Present and
        # null here so the column set does not depend on who filled it.
        "authority_label": None,
        "jurisdiction_note": None,
        "currency_note": None,
        "rationale": _note(c.get("rationale"), 2000),
        # The numbers the model used for the claims this one follows from;
        # the caller maps them onto ids once the rows exist.
        "follows_from_refs": ref_list(c.get("follows_from")),
    }


def raw_test(c: Any) -> dict | None:
    """A claim's test as the drafter sent it, whichever shape it used.

    The prompt asks for `conditions` (and an optional `test_name`) flat on
    the claim, one nesting level fewer than the `{"test": {...}}` object it
    used to ask for. Both are read: a model that nests it anyway is not a
    malformed reply, and older replies replayed through this code are not
    either. Pure.
    """
    if not isinstance(c, dict):
        return None
    nested = c.get("test")
    if isinstance(nested, dict):
        return nested
    conds = c.get("conditions")
    if isinstance(conds, list | tuple) and conds:
        return {"name": c.get("test_name") or c.get("name") or "",
                "conditions": list(conds)}
    return None


def ref_list(raw: Any) -> list[int]:
    """Claim numbers out of a `follows_from` field: ints only, order kept,
    duplicates dropped, bools and fractions refused. Pure."""
    if raw is None:
        return []
    if isinstance(raw, str | bytes) or not isinstance(raw, list | tuple | set):
        raw = [raw]
    out: list[int] = []
    for x in raw:
        if isinstance(x, bool):
            continue
        if isinstance(x, str):
            m = re.search(r"\d+", x)
            if not m:
                continue
            x = m.group(0)
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if not f.is_integer():
            continue
        n = int(f)
        if n not in out:
            out.append(n)
    return out


def is_derived(kind: str | None, has_evidence: bool, follows_from: Any) -> bool:
    """Does this claim rest on other claims rather than on passages? An
    inference always; a conclusion when it cites nothing and names what it
    follows from. Pure."""
    if kind == "inference":
        return True
    return kind == "conclusion" and not has_evidence and bool(follows_from)


# An identifier is a token carrying a digit and joined by a separator. The
# shape is structural, not legal: it knows nothing about courts or articles.
# Same pattern the runner's citation-loss accounting uses; kept here so the
# inference check can run without importing the runner.
_IDENTIFIER = re.compile(r"\b(?=[^\s]*\d)[A-Za-z0-9]+(?:[-/.]\w+|\(\w+\))+")


def named_identifiers(text: str) -> set[str]:
    return {m.group(0).rstrip(".,;:") for m in _IDENTIFIER.finditer(text or "")}


def inference_gaps(claims: list[dict]) -> list[str]:
    """Which inference claims rest on nothing, as findings for the reviser.

    Each claim is `{"sequence", "id", "claim_kind", "follows_from",
    "claim_text", "supported"}` — `supported` meaning it carries at least one
    evidence row. An inference must name at least one claim that is in the
    answer and supported, and may not name an authority (an identifier in
    its prose) that none of the claims it follows from carries: a reasoning
    step that introduces a source is a citation wearing the wrong kind.
    Pure.
    """
    by_id = {str(c.get("id")): c for c in claims if c.get("id") is not None}
    out = []
    for c in claims:
        if c.get("claim_kind") != "inference":
            continue
        refs = [by_id.get(str(r)) for r in (c.get("follows_from") or [])]
        refs = [r for r in refs if r is not None]
        seq = c.get("sequence")
        if not any(r.get("supported") for r in refs):
            out.append(
                f"Claim {seq} is an inference that follows from no supported claim: "
                'set "follows_from" to the claims it reasons from (each must be in '
                "the answer and carry evidence), or revise it into an evidenced "
                "claim, or drop it.")
            continue
        carried: set[str] = set()
        for r in refs:
            carried |= named_identifiers(str(r.get("claim_text") or ""))
        introduced = named_identifiers(str(c.get("claim_text") or "")) - carried
        if introduced:
            out.append(
                f"Claim {seq} is an inference but names {', '.join(sorted(introduced))}, "
                "which none of the claims it follows from carries: an inference "
                "reasons from claims already made and introduces no authority of "
                "its own. Move the citation into an evidenced claim it follows from.")
    return out


def _claim_type_of(kind: str) -> str:
    """`claim_type` is the older, coarser column several readers still have.
    The three structural kinds have no claim_type of their own and take the
    evidential kind nearest them; everything else is the kind itself. Both
    columns now carry the same vocabulary — see `CLAIM_KINDS`."""
    return {"test": "rule", "label": "fact",
            "inference": "rule"}.get(kind, kind)


def _note(raw: Any, cap: int) -> str | None:
    s = str(raw or "").strip()
    if not s or s.lower() in ("null", "none", "n/a"):
        return None
    return s[:cap]


def normalise_test(raw: Any) -> dict | None:
    """A test as the contract stores it, or None if it is not one.

    `{"name", "conditions": [{"text", "cumulative"}], "source_para"}`, the
    conditions in the order given, which the prompt says is the source's.
    Per-condition evidence the drafter attaches is not kept here: it
    becomes the claim's evidence rows, one span per condition, so the
    validator judges each condition as it judges any other span. Pure.
    """
    if not isinstance(raw, dict):
        return None
    conds = []
    for x in raw.get("conditions") or []:
        if isinstance(x, str):
            x = {"text": x}
        if not isinstance(x, dict):
            continue
        text = str(x.get("text") or "").strip()
        if not text:
            continue
        conds.append({"text": text[:1000], "cumulative": _truthy(x.get("cumulative", True))})
    if len(conds) < 2:
        return None
    para = _note(raw.get("source_para"), 50)
    return {"name": _note(raw.get("name"), 300) or "", "conditions": conds,
            "source_para": para}


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "alternative", "any", "")
    return bool(v)


def condition_spans(raw_test: Any) -> list[dict]:
    """The passages the drafter pinned to each condition, in condition order,
    as evidence dicts carrying the condition's number in `note`. Pure."""
    if not isinstance(raw_test, dict):
        return []
    out = []
    for i, x in enumerate(raw_test.get("conditions") or [], start=1):
        if not isinstance(x, dict):
            continue
        ev = x.get("evidence")
        if isinstance(ev, list):
            ev = ev[0] if ev else None
        if isinstance(ev, dict) and ev.get("filename"):
            out.append({**ev, "note": f"condition {i}"})
    return out


def order_claims(claims: list[dict]) -> list[dict]:
    """The claims in render order, each with `position` set within its
    section and part.

    Conclusion first: the overall conclusion (no part) leads, then one per
    part in part order. Then analysis, part by part, unassigned last. Then
    the authorities. Within a group the drafter's order stands. A claim with
    no section at all — an older row — sorts after the structured ones and
    keeps its relative order. Stable, so `sequence` can be assigned from the
    returned order. Pure.
    """
    def key(item: tuple[int, dict]) -> tuple:
        i, c = item
        section = c.get("section")
        rank = _SECTION_RANK.get(section, len(SECTIONS))
        part = c.get("part_index")
        if section == "conclusion":
            part_key = -1 if part is None else part
        else:
            part_key = 10_000 if part is None else part
        return (rank, part_key, i)

    ordered = [c for _, c in sorted(enumerate(claims), key=key)]
    counters: dict[tuple, int] = {}
    for c in ordered:
        group = (c.get("section"), c.get("part_index"))
        counters[group] = counters.get(group, 0) + 1
        c["position"] = counters[group]
    return ordered


def conclusion_gaps(claims: list[dict], parts: list[dict]) -> list[str]:
    """Which conclusions the answer lacks, as findings for the reviser.

    One conclusion-section claim per part and one for the whole question.
    Judged only over an answer that carries sections at all: rows from
    before the structure existed have none, and judging them would fail
    every older answer for a rule it never heard. Pure.
    """
    if not parts or not any(c.get("section") for c in claims):
        return []
    concl = [c for c in claims if c.get("section") == "conclusion"]
    out = []
    have = {c.get("part_index") for c in concl}
    for p in parts:
        if p["index"] not in have:
            out.append(
                f"Part {p['index'] + 1} ({p['label']}) has no conclusion: add one "
                f'claim of kind "conclusion" with "part": {p["index"] + 1} '
                "stating the answer to that part, carried by evidence already "
                "cited or newly bound.")
    if None not in have:
        out.append(
            'The answer has no overall conclusion: add one claim of kind '
            '"conclusion" with "part": null that states the answer to the '
            "question as a whole.")
    # A conclusion written into the analysis is the old defect wearing a new
    # label: the reader meets it last. It is named so the reviser moves it
    # rather than writes a second one.
    misplaced = [c for c in claims
                 if c.get("section") != "conclusion"
                 and str(c.get("claim_kind") or c.get("claim_type") or "") == "conclusion"]
    for c in misplaced:
        if c.get("sequence") is not None:
            out.append(
                f"Claim {c['sequence']} draws a conclusion but sits in the "
                f"{c.get('section') or 'unsectioned'} section: revise it with "
                '"kind": "conclusion" so it leads its part.')
    return out


# ── duplicates ───────────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9]{3,}")
DUPLICATE_THRESHOLD = 0.6


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def duplicate_pairs(claims: list[dict], threshold: float = DUPLICATE_THRESHOLD
                    ) -> list[tuple[int, int]]:
    """Pairs of claim sequences that restate one proposition from one source.

    Two claims are candidates when they cite a document in common; they are
    duplicates when their word sets overlap past the threshold (Jaccard).
    Word overlap is a blunt instrument, so the threshold is high and the
    finding is fed to the reviser as a merge to judge, never applied. Each
    claim is `{"sequence", "text", "docs"}`. Pure.
    """
    out = []
    items = sorted(claims, key=lambda c: c["sequence"])
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if not (set(a.get("docs") or ()) & set(b.get("docs") or ())):
                continue
            ta, tb = _tokens(a.get("text", "")), _tokens(b.get("text", ""))
            if not ta or not tb:
                continue
            if len(ta & tb) / len(ta | tb) >= threshold:
                out.append((a["sequence"], b["sequence"]))
    return out


def duplicate_feedback(pairs: list[tuple[int, int]]) -> list[str]:
    return [
        f"Claims {a} and {b} restate the same proposition from the same source: "
        f"merge them into one claim (revise {a} to carry both citations, drop "
        f"{b} with a rationale), unless they make different points."
        for a, b in pairs
    ]


# ── source context ───────────────────────────────────────────────────────────



def unwrap(value: Any) -> Any:
    """A stored property value as the scalar it wraps. `{"_": x}` is the
    one-shot's shape; `{"value": x}` and `{"name": x}` are reducer outputs."""
    if isinstance(value, dict):
        if "_" in value and len(value) == 1:
            return unwrap(value["_"])
        if "value" in value and len(value) == 1:
            return unwrap(value["value"])
        if "name" in value:
            return value["name"]
    return value


def tier_of(annotations: dict | None,
            names: Sequence[str] | None = None) -> int | None:
    """A source's tier out of its annotation values, or None. Pure.

    `names` are the annotations that derive standing, resolved from the
    deployment's declared `standing` role. The first that yields a number
    wins, so the caller's order decides; `None` means the caller resolved
    nothing and falls back to the engine-contract name, which is what a
    deployment that has declared no role still has.
    """
    if not isinstance(annotations, dict):
        return None
    for name in (names if names is not None else (TIER_ANNOTATION,)):
        v = annotations.get(name)
        if isinstance(v, dict):
            v = v.get("depth", v.get("value"))
        if isinstance(v, bool) or v is None:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            return n
    return None


def issuing_body_of(annotations: dict | None, name: str | None) -> str | None:
    """Who issued the source, out of the annotation the deployment declared
    for it (`roles.issuing_body_annotation`). Pure.

    `None` for `name` is a deployment that declares no such annotation, and
    the answer is then None rather than a lookup under a name the engine
    picked — there is no issuing body to report, and `resolve` has already
    said so in the log.
    """
    if not name or not isinstance(annotations, dict):
        return None
    v = unwrap(annotations.get(name))
    return str(v) if v not in (None, "") else None


#: Month names as the stored values spell them, so a date a source wrote in
#: words can be read. Five languages because the corpus holds all five; a
#: month this does not know leaves the value unparsed rather than guessed.
_MONTHS = {}
for _i, _names in enumerate((
    ("january", "januari", "janvier", "enero", "januar"),
    ("february", "februari", "février", "fevrier", "febrero", "februar"),
    ("march", "maart", "mars", "marzo", "märz", "marz"),
    ("april", "avril", "abril"),
    ("may", "mei", "mai", "mayo"),
    ("june", "juni", "juin", "junio"),
    ("july", "juli", "juillet", "julio"),
    ("august", "augustus", "août", "aout", "agosto"),
    ("september", "septembre", "septiembre"),
    ("october", "oktober", "octobre", "octubre"),
    ("november", "novembre", "noviembre"),
    ("december", "dezember", "décembre", "decembre", "diciembre"),
), start=1):
    for _n in _names:
        _MONTHS[_n] = _i
        _MONTHS[_n[:3]] = _i


def parse_date(value: Any) -> date | None:
    """A date out of a property value, or None. ISO first, then a plain
    year-month-day with any separator, then a bare year as 1 January."""
    v = unwrap(value)
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    # A date the source wrote out in words — "9 March 2023", "14 November
    # 2012", "1st October 2020". Extraction stores what the document says, so
    # the field a class declares as its date often holds prose rather than a
    # machine date. Left unparsed, such a document is simply dateless: in one
    # published answer both judgments carried their date this way, neither
    # parsed, and the closing "law stated as at" line took the date of a
    # commentary piece eleven years older than the judgment being described.
    # Month names only, in the languages the stored values actually use; a
    # date this cannot read stays None rather than becoming a guess.
    m = re.match(
        r"(\d{1,2})(?:st|nd|rd|th)?[\s.]+([A-Za-zÀ-ÿ]+)[\s.,]+(\d{4})", s)
    if m:
        month = _MONTHS.get(m.group(2).lower().rstrip("."))
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                return None
    m = re.match(r"([A-Za-zÀ-ÿ]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if m:
        month = _MONTHS.get(m.group(1).lower().rstrip("."))
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                return None
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return date(int(m.group(1)), 1, 1)
    return None


def document_date(
    props: dict | None, class_name: str = "", by_class: dict | None = None
) -> date | None:
    """The document's own date, read from the property its class declares.

    The engine used to take the latest parsable value among properties whose
    NAME mentioned a date, which is a guess about another party's vocabulary:
    a class calling it `issued` or `fecha` had no date at all, silently. The
    class now says which of its properties is the date (`engine_role: date`),
    and a class that declares none has none.

    `by_class` is `declared_roles.property_names_by_class_name(..., DATE)`.
    With no mapping — a caller that has not been given one yet — the answer is
    None rather than a guess.
    """
    name = (by_class or {}).get(class_name)
    if not name:
        return None
    return parse_date((props or {}).get(name))


def jurisdiction_of(
    props: dict | None, class_name: str = "", by_class: dict | None = None
) -> str | None:
    """Whose law the source belongs to, from the property its class declares.

    Same fault as `document_date`: the name used to be matched against
    `jurisdiction|country|member_?state`, so any other word was invisible.
    """
    name = (by_class or {}).get(class_name)
    if not name:
        return None
    v = unwrap((props or {}).get(name))
    return str(v) if v not in (None, "") else None


def currency_notes(rows: list[dict], roles: DeclaredRoles) -> dict[str, str]:
    """Per filename, why the document is not current law — or absent.

    Two rules, both from the retrieved set alone. A document whose declared
    status property says repealed or superseded is stale, and the property it
    declares for `superseded_by` names what replaced it. A document that
    shares its class's identifier with a later-dated document in the set is a
    decision with a later ruling in the same case beside it. Rows are
    `_manifest_rows` rows carrying `props` (unwrapped property dict), `class`
    and `identifier` (the class's identifier property value, or None).

    Both properties used to be read by the literal names `status` and
    `superseded_by`, so a class calling either anything else was current law
    for ever. `roles` says what each class calls them; a class that declares
    neither gets no note, which is the truthful answer. Pure — the
    declarations are resolved once, by the caller, and handed in.
    """
    out: dict[str, str] = {}
    by_case: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        fn = r.get("filename")
        if not fn:
            continue
        props = r.get("props") or {}
        cls = str(r.get("class") or "")
        # WHICH property says how the source stands is the deployment's to
        # name; WHAT its value has to say to mean "not current law" is the
        # engine's own vocabulary — `STALE_STATUSES` is the contract a class
        # writes its status values against, not a guess at another party's
        # words, and it stays here for that reason.
        status = str(unwrap(roles.value(roles.status, props, cls))
                     or "").strip().lower()
        if status in STALE_STATUSES:
            note = f"{status}"
            by = unwrap(roles.value(roles.superseded_by, props, cls))
            if by:
                note += f"; superseded by {by}"
            out[fn] = note[:500]
        ident = r.get("identifier")
        if ident:
            by_case.setdefault((cls, str(ident)), []).append(r)
    for (cls, ident), group in by_case.items():
        dated = [(document_date(r.get("props"), cls, roles.date), r)
                 for r in group]
        dated = [(d, r) for d, r in dated if d is not None]
        if len(dated) < 2:
            continue
        latest_d, latest_r = max(dated, key=lambda x: x[0])
        for d, r in dated:
            if r is latest_r or d >= latest_d:
                continue
            fn = r["filename"]
            note = (f"a later decision in the same case ({ident}) is in the "
                    f"retrieved set: {latest_r['filename']}, {latest_d.isoformat()}")
            out[fn] = (out[fn] + "; " + note if fn in out else note)[:500]
    return out


def jurisdiction_notes(rows: list[dict], roles: DeclaredRoles) -> dict[str, str]:
    """Per filename, a note where the document is not in the jurisdiction the
    retrieved set is mostly in — or absent.

    A question states no scope, so the scope is taken from the sources it is
    answered out of: the jurisdiction most of them carry. A document outside
    that majority is the one a reader has to be told about, and the note
    names the jurisdiction it IS in rather than saying that it differs, so
    the sentence reads the same whatever the majority turned out to be.

    Silent where fewer than two documents carry a jurisdiction at all, and
    where they all carry the same one: a note every claim gets is a note that
    says nothing. Pure.
    """
    seen: dict[str, str] = {}
    for r in rows:
        fn = r.get("filename")
        j = jurisdiction_of(r.get("props"), str(r.get("class") or ""),
                            roles.jurisdiction)
        if fn and j:
            seen[str(fn)] = str(j)
    if len(seen) < 2:
        return {}
    counts: dict[str, int] = {}
    for j in seen.values():
        counts[j] = counts.get(j, 0) + 1
    if len(counts) < 2:
        return {}
    # Ties broken by name so the same set always yields the same majority.
    modal = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return {fn: f"jurisdiction: {j}"[:300]
            for fn, j in seen.items() if j != modal}


def source_context_line(r: dict, label: str | None = None) -> str:
    """One document as the drafter sees it above its passages: what it IS,
    never what it says.

    Title, class, and the label its class declares where it has one. It used
    to carry the tier, the issuing body, the date, the jurisdiction and a
    currency note as well, because the drafter was asked to write those back
    out as fields of every claim. The engine fills those fields itself now,
    from the same rows this line was built from, so what remains is what the
    model actually reads with: which document this is and what kind of thing
    it is.
    """
    bits = [f"title: {r.get('title') or r.get('filename')}",
            f"class: {r.get('class') or 'unclassified'}"]
    if label:
        bits.append(f"labelled: {label}")
    return "; ".join(bits)


def latest_source_date(rows: list[dict], roles: DeclaredRoles) -> date | None:
    """The latest date any of these documents carries, or None. Each row's
    date is the property ITS class declares, so two classes may date
    themselves differently and both count."""
    best = None
    for r in rows:
        d = document_date(r.get("props"), str(r.get("class") or ""), roles.date)
        if d and (best is None or d > best):
            best = d
    return best


# ── validation ───────────────────────────────────────────────────────────────

def judged_text(claim_text: str, test: dict | None) -> str:
    """What the validator reads for a claim: its text, and for a test claim
    its conditions numbered in order, so a span pinned to condition 2 is
    judged against condition 2 and not against the sentence around it."""
    if not isinstance(test, dict) or not test.get("conditions"):
        return claim_text
    lines = [claim_text.rstrip(), "The conditions, in the source's order"
             + (" (cumulative)" if all(c.get("cumulative") for c in test["conditions"])
                else "") + ":"]
    for i, c in enumerate(test["conditions"], start=1):
        lines.append(f"  {i}. {c.get('text', '')}")
    return "\n".join(lines)
