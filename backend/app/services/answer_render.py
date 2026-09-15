"""The answer as prose, assembled from its rows.

The engine used to emit claim rows and leave every reader to print one
paragraph per row, which is how the conclusion came last and the sources all
read at one level. This is the generic assembler: given the answer, its
claims, their evidence and the documents cited, it writes the markdown the
contract describes —

    # <question>
    ## Conclusion               the overall conclusion, then one per part
    ## <part label>             analysis claims, joined into paragraphs
    ## Authorities              each document once, grouped and labelled
    Law stated as at <date>.

Citation markers are `[n]`, numbered in order of first appearance and
matching the Authorities list; the mapping n → document is returned beside
the text so a consumer can align its own bibliography. Labels in square
brackets follow a sentence whose source is secondary or whose jurisdiction
or currency is off. A document is never cited by its filename.

An answer from before the structure existed carries no sections; it renders
as it always did, one paragraph per claim, with the same markers and the
same Authorities list.

`render_markdown` is pure: dicts in, text out. `assemble` is the one
function here that touches a session — it gathers the rows an answer is
made of and hands them to `render_markdown`, so the publish path and the
read endpoint render from exactly the same material.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.services.answer_structure import (
    UNCLASSIFIED_HEADING,
    document_date,
    part_heading,
    unwrap,
)

# The eight labels this module used to name — what a judgment, an order, an
# advisory opinion, a regulator's decision, legislation, commentary and a
# party's submission are called — were a vocabulary for one collection
# sitting in engine code, printed as headings and as the words after a
# sentence. They are gone. A claim's label is the one its document's CLASS
# declares, and the Authorities are grouped under the class's own name, so a
# deployment that files its sources differently gets its own words without
# SGR learning any of them.

#: Property names a citation is built from, in order of preference.
_NUMBER_KEYS = ("case_number", "celex", "reference", "number")
_ECLI_KEYS = ("ecli",)


@dataclass
class Rendered:
    markdown: str
    #: [{"n": 1, "document_id": "..."}] in marker order.
    citations: list[dict] = field(default_factory=list)
    #: The date the closing line states, so a caller that is about to store
    #: the text can store the same date on the row rather than deriving it
    #: a second time and disagreeing with the prose.
    law_stated_as_at: date | None = None


class _Cites:
    """Marker numbers in first-appearance order."""

    def __init__(self) -> None:
        self.order: list[str] = []

    def n(self, document_id: str) -> int:
        if document_id not in self.order:
            self.order.append(document_id)
        return self.order.index(document_id) + 1


def _str(v: Any) -> str:
    return str(v) if v is not None else ""


def _props(doc: dict | None) -> dict:
    raw = (doc or {}).get("properties") or {}
    return {str(k): unwrap(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _first(props: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = props.get(k)
        if v not in (None, "", [], {}):
            return _str(v)
    return None


def citation(doc: dict | None) -> str:
    """`<title> (<case_number or celex>, <ecli if present>, <date>)` — and
    never a filename. A document with no number is cited by title and date
    alone; one with no title either is named by what it is.

    The identifier slot holds a declared property and nothing else. It used
    to fall back to `external_ref`, which is the source's natural key when a
    connector supplies one and the FILE'S NAME when it does not — so every
    document without a case number or a CELEX was cited by a storage name a
    reviewer called useless. A citation that cannot say which authority it
    points at should say less, not say a filename.
    """
    doc = doc or {}
    props = _props(doc)
    title = _str(doc.get("title")).strip()
    number = _first(props, _NUMBER_KEYS)
    ecli = _first(props, _ECLI_KEYS)
    d = document_date(props)
    if not title:
        title = number or _str(doc.get("class")).strip() or "Untitled source"
        if title == number:
            number = None
    inside = [x for x in (number, ecli, d.isoformat() if d else None) if x]
    return title + (f" ({', '.join(inside)})" if inside else "")


def paragraph_label(ref: str | None) -> str | None:
    """`para. 42` for a bare integer; anything else verbatim.

    `paragraph_ref` is a LABEL, not a number: a document numbers its
    paragraphs "42", another "r.o. 4.2", a preamble "recital 14". Only the
    bare integer is missing the word that says what it counts, so only the
    bare integer gets one added. Everything else already carries its own and
    is printed exactly as the document shows it — guessing that "4.2" is a
    paragraph rather than a section is how a citation starts asserting
    something the source never said. Pure.
    """
    s = _str(ref).strip()
    if not s:
        return None
    return f"para. {s}" if re.fullmatch(r"\d+", s) else s


def _labels(c: dict) -> str:
    """The bracketed labels a claim's sentence ends with, or "".

    The label is printed exactly as it is stored, because it is the words the
    deployment declared on the document's class. A claim whose source needs
    no label carries none, which is the same thing as the class declaring
    none.
    """
    out = []
    lab = _str(c.get("authority_label")).strip()
    if lab:
        out.append(lab)
    if c.get("jurisdiction_note"):
        out.append(_str(c["jurisdiction_note"]).strip())
    if c.get("currency_note"):
        out.append(_str(c["currency_note"]).strip())
    return "".join(f" [{x}]" for x in out if x)


def _markers(c: dict, ev_by_claim: dict, cites: _Cites) -> str:
    """`[1][3]` for the documents this claim cites, once each, in evidence
    order. An inference cites nothing and gets no marker."""
    if c.get("claim_kind") == "inference":
        return ""
    seen: list[str] = []
    for e in ev_by_claim.get(str(c.get("id")), []):
        did = _str(e.get("document_id"))
        if did and did not in seen:
            seen.append(did)
    return "".join(f"[{cites.n(d)}]" for d in seen)


def _sentence(c: dict, ev_by_claim: dict, cites: _Cites) -> str:
    return _str(c.get("claim_text")).strip() + _markers(c, ev_by_claim, cites) + _labels(c)


def _test_block(c: dict, ev_by_claim: dict, cites: _Cites) -> str:
    t = c.get("test") or {}
    conds = [x for x in (t.get("conditions") or []) if isinstance(x, dict)]
    name = _str(t.get("name")).strip() or _str(c.get("claim_text")).strip()
    cumulative = bool(conds) and all(x.get("cumulative") for x in conds)
    head = (f"**{name}** — the conditions, in order"
            + (" (cumulative)" if cumulative else "")
            + _markers(c, ev_by_claim, cites) + _labels(c) + ":")
    lines = [head]
    for i, x in enumerate(conds, start=1):
        lines.append(f"{i}. {_str(x.get('text')).strip()}")
    return "\n".join(lines)


def _paragraphs(claims: list[dict], ev_by_claim: dict, cites: _Cites) -> list[str]:
    """Claims joined into paragraphs. A new paragraph starts at a test claim
    (which renders as its own block) and at a gap in `position` past 1;
    otherwise consecutive claims read on from one another."""
    out: list[str] = []
    current: list[str] = []
    last_pos: int | None = None
    for c in claims:
        pos = c.get("position")
        gap = (isinstance(pos, int) and isinstance(last_pos, int) and pos > last_pos + 1)
        if c.get("claim_kind") == "test":
            if current:
                out.append(" ".join(current))
                current = []
            out.append(_test_block(c, ev_by_claim, cites))
        else:
            if gap and current:
                out.append(" ".join(current))
                current = []
            current.append(_sentence(c, ev_by_claim, cites))
        last_pos = pos if isinstance(pos, int) else last_pos
    if current:
        out.append(" ".join(current))
    return out


def _sort_key(c: dict) -> tuple:
    pos = c.get("position")
    seq = c.get("sequence")
    return (pos if isinstance(pos, int) else 10_000, seq if isinstance(seq, int) else 0)


def _authorities(claims: list[dict], evidence: list[dict], documents: dict,
                 cites: _Cites) -> list[str]:
    """The Authorities section: every cited document once, grouped under the
    name of its own CLASS, the groups ordered by the highest authority any
    document in them carries and then by name; each entry numbered by its
    marker and cited in full, with the paragraphs the answer pins in it.

    Grouping used to be by an eight-value label the drafter picked per claim,
    which put the same document in different groups on different runs and put
    a vocabulary for one collection in this file. The class is already stored
    on the document, is the same on every run, and is the deployment's word
    for what the thing is."""
    by_doc: dict[str, dict] = {}
    claims_by_id = {str(c.get("id")): c for c in claims}
    for e in evidence:
        did = _str(e.get("document_id"))
        if not did:
            continue
        c = claims_by_id.get(str(e.get("claim_id"))) or {}
        entry = by_doc.setdefault(did, {"tiers": [], "refs": []})
        if isinstance(c.get("authority_tier"), int):
            entry["tiers"].append(c["authority_tier"])
        ref = (e.get("span") or {}).get("paragraph_ref") or e.get("paragraph_ref")
        lab = paragraph_label(_str(ref))
        if lab and lab not in entry["refs"]:
            entry["refs"].append(lab)
    # Every document gets its marker number BEFORE the sort, in the order
    # the evidence lists it. A document whose only citing claim suppressed
    # its marker — an inference — is otherwise first numbered inside the
    # sort key, where the number it receives depends on the order the sort
    # happens to compare rows in. Numbering is first appearance, and the
    # sort is not an appearance.
    for did in by_doc:
        cites.n(did)
    rows = []
    for did, entry in by_doc.items():
        heading = (_str((documents.get(did) or {}).get("class")).strip()
                   or UNCLASSIFIED_HEADING)
        tier = min(entry["tiers"]) if entry["tiers"] else None
        d = document_date(_props(documents.get(did)))
        rows.append((heading, tier, d, did, entry["refs"]))
    # A group sorts by the highest authority it holds, so the classes a
    # deployment treats as authoritative lead without SGR being told which
    # they are. Ties go alphabetically, which is arbitrary and stable —
    # the two properties an order like this needs.
    group_tier: dict[str, int] = {}
    for heading, tier, _d, _did, _refs in rows:
        t = tier if tier is not None else 99
        group_tier[heading] = min(group_tier.get(heading, 99), t)
    rows.sort(key=lambda r: (group_tier[r[0]], r[0],
                             r[1] if r[1] is not None else 99,
                             r[2] or date.min, cites.n(r[3])))
    lines: list[str] = []
    current = None
    for heading, _tier, _d, did, refs in rows:
        if heading != current:
            # The blank line is load-bearing: without it the next group's
            # heading is a lazy continuation of the previous group's last
            # list item, and every group after the first disappears into a
            # bullet.
            if current is not None:
                lines.append("")
            lines.append(f"**{heading}**")
            lines.append("")
            current = heading
        entry = f"[{cites.n(did)}] " + citation(documents.get(did))
        if refs:
            entry += ", " + ", ".join(refs)
        lines.append(f"- {entry}")
    return lines


def render_markdown(answer: dict, claims: list[dict], evidence: list[dict],
                    documents: dict[str, dict]) -> Rendered:
    """The answer as markdown, and the marker → document mapping.

    `answer` carries `question`, `question_parts` and `law_stated_as_at`;
    `claims` are the claim rows as dicts; `evidence` the evidence rows
    (`claim_id`, `document_id`, `span`, optionally `paragraph_ref`);
    `documents` maps document id → {title, class, properties}.
    Ids may be UUIDs or strings; they are compared as strings.
    """
    cites = _Cites()
    documents = {str(k): v for k, v in (documents or {}).items()}
    ev_by_claim: dict[str, list[dict]] = {}
    for e in evidence:
        ev_by_claim.setdefault(str(e.get("claim_id")), []).append(e)
    claims = sorted(claims, key=lambda c: c.get("sequence") or 0)
    parts = [p for p in (answer.get("question_parts") or []) if isinstance(p, dict)]
    structured = any(c.get("section") for c in claims)

    out: list[str] = [f"# {_str(answer.get('question')).strip()}", ""]
    if structured:
        concl = [c for c in claims if c.get("section") == "conclusion"]
        overall = sorted([c for c in concl if c.get("part_index") is None], key=_sort_key)
        per_part = {}
        for c in concl:
            if c.get("part_index") is not None:
                per_part.setdefault(int(c["part_index"]), []).append(c)
        out += ["## Conclusion", ""]
        for c in overall:
            out += [_sentence(c, ev_by_claim, cites), ""]
        for idx in sorted(per_part):
            for c in sorted(per_part[idx], key=_sort_key):
                out += [_sentence(c, ev_by_claim, cites), ""]
        if not concl:
            out += ["(No conclusion was drawn.)", ""]

        analysis = [c for c in claims if c.get("section") == "analysis"]
        indices = sorted({int(c["part_index"]) for c in analysis
                          if c.get("part_index") is not None}
                         | {int(p["index"]) for p in parts if "index" in p})
        for idx in indices:
            part = next((p for p in parts if p.get("index") == idx), {})
            label = part.get("label") or next(
                (c.get("part_label") for c in analysis
                 if c.get("part_index") == idx and c.get("part_label")), None)
            # A heading, not the part restated: the label is printed as it
            # stands when it is already short, and a label that is really a
            # sentence — every row written before the splitter wrote
            # headings — is shortened to its opening clause.
            heading = part_heading(label, part.get("text")) or f"Part {idx + 1}"
            out += [f"## {heading}", ""]
            mine = sorted([c for c in analysis if c.get("part_index") == idx], key=_sort_key)
            paras = _paragraphs(mine, ev_by_claim, cites)
            for p in paras:
                out += [p, ""]
            if not paras:
                out += ["(No analysis for this part.)", ""]
        loose = sorted([c for c in analysis if c.get("part_index") is None], key=_sort_key)
        if loose:
            out += ["## Analysis", ""]
            for p in _paragraphs(loose, ev_by_claim, cites):
                out += [p, ""]
        # Authority-section claims are prose too: what a source is and holds,
        # kept beside the bibliography so the reader sees both.
        auth_claims = sorted([c for c in claims if c.get("section") == "authority"],
                             key=_sort_key)
        unsectioned = [c for c in claims if not c.get("section")]
        out += ["## Authorities", ""]
        for c in auth_claims + sorted(unsectioned, key=_sort_key):
            out += [_sentence(c, ev_by_claim, cites), ""]
    else:
        for c in claims:
            if c.get("claim_kind") == "test":
                out += [_test_block(c, ev_by_claim, cites), ""]
            else:
                out += [_sentence(c, ev_by_claim, cites), ""]
        out += ["## Authorities", ""]

    auth = _authorities(claims, evidence, documents, cites)
    out += auth if auth else ["(No sources cited.)"]
    out.append("")
    as_at = answer.get("law_stated_as_at")
    if as_at:
        out += [f"Law stated as at {as_at.isoformat() if isinstance(as_at, date) else as_at}.",
                ""]
    return Rendered(markdown="\n".join(out).rstrip() + "\n",
                    citations=[{"n": i + 1, "document_id": did}
                               for i, did in enumerate(cites.order)],
                    law_stated_as_at=as_at if isinstance(as_at, date) else None)


def latest_document_date(documents: dict[str, dict]) -> date | None:
    """The latest date any of these documents carries, or None. The answer
    states the law as at this date; a run whose sources carry no date at all
    falls back to the run date, which the caller supplies. Pure."""
    best = None
    for doc in (documents or {}).values():
        d = document_date(_props(doc))
        if d and (best is None or d > best):
            best = d
    return best


async def assemble(session: Any, answer_id: Any,
                   fallback_as_at: date | None = None) -> Rendered | None:
    """The answer at `answer_id` as prose, read out of the database.

    The one function here that touches a session. The publish path and
    `GET /answers/{id}/markdown` both call it, so what is stored on the row
    and what a reader asks for later are assembled from the same rows by the
    same code — a stored text that has drifted from the claims behind it is
    the defect this exists to avoid. Returns None when the answer is not
    there; an answer with no claims still renders (as its question and an
    empty Authorities list), and the caller decides what that means.

    The date the law is stated as at is the row's own if it has one, else
    the latest date any cited source carries. `fallback_as_at` is what the
    publish path passes for a corpus whose sources are all undated — the run
    date — so the answer still says how current it is. A read passes none:
    a reader must not be handed a date nothing in the answer supports.
    """
    from sqlalchemy import select

    from app.models import (
        Answer,
        AnswerClaim,
        ClaimEvidence,
        Document,
        DocumentClass,
        DocumentClassProperty,
        PropertyValue,
    )
    from app.services.document_identity import document_title_subquery

    answer = await session.get(Answer, answer_id)
    if answer is None:
        return None
    claim_rows = (await session.execute(
        select(AnswerClaim)
        .where(AnswerClaim.answer_id == answer_id)
        .order_by(AnswerClaim.sequence)
    )).scalars().all()
    ev_rows = (await session.execute(
        select(ClaimEvidence)
        .join(AnswerClaim, AnswerClaim.id == ClaimEvidence.claim_id)
        .where(AnswerClaim.answer_id == answer_id)
        .order_by(AnswerClaim.sequence, ClaimEvidence.id)
    )).scalars().all()

    doc_ids = {e.document_id for e in ev_rows}
    documents: dict[str, dict] = {}
    if doc_ids:
        # Neither the filename nor `external_ref` is read: a document is
        # cited by its declared identity — title, number, ECLI, date — and
        # `external_ref` is the file's name whenever the connector supplied
        # no natural key. What the assembler never holds, it cannot print.
        rows = (await session.execute(
            select(Document.id, DocumentClass.name, document_title_subquery())
            .outerjoin(DocumentClass, DocumentClass.id == Document.document_class_id)
            .where(Document.id.in_(doc_ids))
        )).all()
        for did, class_name, title in rows:
            documents[str(did)] = {
                "title": title, "class": class_name, "properties": {}}
        prop_rows = (await session.execute(
            select(PropertyValue.document_id, DocumentClassProperty.name,
                   PropertyValue.value)
            .join(DocumentClassProperty,
                  DocumentClassProperty.id == PropertyValue.property_id)
            .where(PropertyValue.document_id.in_(doc_ids))
        )).all()
        for did, name, value in prop_rows:
            entry = documents.get(str(did))
            if entry is not None and value is not None:
                entry["properties"][str(name)] = value

    as_at = (answer.law_stated_as_at
             or latest_document_date(documents) or fallback_as_at)
    return render_markdown(
        {"question": answer.question,
         "question_parts": answer.question_parts,
         "law_stated_as_at": as_at},
        [_claim_dict(c) for c in claim_rows],
        [{"claim_id": str(e.claim_id), "document_id": str(e.document_id),
          "span": e.span or {}, "paragraph_ref": e.paragraph_ref}
         for e in ev_rows],
        documents,
    )


_CLAIM_FIELDS = ("claim_text", "claim_type", "sequence", "section", "part_index",
                 "part_label", "position", "claim_kind", "test",
                 "authority_label", "authority_tier", "jurisdiction_note",
                 "currency_note", "follows_from")


def _claim_dict(c: Any) -> dict:
    out = {f: getattr(c, f, None) for f in _CLAIM_FIELDS}
    out["id"] = str(c.id)
    return out
