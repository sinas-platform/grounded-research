"""A claim that asserts a rule names the source it rests on.

THE REQUIREMENT. A review of published answers asked for it structurally rather than
lexically: every proposition carries a source, and where that source is one
the field treats as authority it must be named in the claim — the parties and
the reference, not a bracket at the end. An unnamed source makes the claim
unusable: a reader cannot look it up, cannot weigh it, and cannot tell a
holding from a description of one.

WHY THE OLD CHECK MISSED. What existed was an instruction to the drafter and,
around it, a check that looked for attribution WORDS. Those words are
English, and about a third of the collection is French, so a claim resting on
a French judgment passed the check by never tripping it. A check that cannot
fail on a third of the corpus is not a check.

WHAT THIS DOES INSTEAD. It looks for the document's own identifier in the
claim's text. An identifier is language-neutral by construction — a case
reference reads the same in a French judgment and an English one — so the
check behaves identically across the collection, and it is what a reader
needs in order to find the thing. The document's name is accepted too, for a
source that carries a name and no reference.

WHAT IS DOMAIN AND WHAT IS NOT. Which classes must be named is the
deployment's to declare (`DocumentClass.naming_required`): a field where an
authority must be identified says so, one where it need not says nothing, and
the engine holds no list of either. What an identifier looks like is already
declared per class. This module compares strings and knows no vocabulary.
"""

from __future__ import annotations

import re
import unicodedata

#: Claim kinds that assert something about the law rather than report or
#: reason. Only these are held to naming: an inference rests on other claims
#: and names no source of its own, a label is about the source, and an
#: abstention says there is nothing to name.
NAMED_KINDS = ("rule", "test", "application")


def unaccented(text: str) -> str:
    """`text` with its accents removed and nothing else touched. Pure.

    The half of `_fold` that is about identity rather than about punctuation,
    split out because it is not only this module's problem. A word-level
    check cannot use `_fold`, which returns one run of characters with the
    word boundaries gone, and the check next door wrote its own comparison
    without this step and so read `Générale` and `Generale` as two names.

    Shared rather than copied: two spellings of "are these the same name" is
    how one of them ends up being the English-only one.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(text or ""))
        if not unicodedata.combining(c))


def _fold(text: str) -> str:
    """Case, accents and punctuation removed, so a match is about the
    characters that identify and not about how they were typed.

    Accents matter: a French title compared against a claim that spells it
    without them is the same source, and a check that says otherwise is the
    English-only failure in a different costume.
    """
    return re.sub(r"[^a-z0-9]+", "", unaccented(str(text or "").lower()))


def _unpad(folded: str) -> str:
    """Leading zeros dropped from every run of digits. Pure.

    A reference is written with or without its separators and with or without
    padding — `T-0125/03`, `T-125/03`, `T-125/2003` — and they are one
    reference. Folding removes the separators; this removes the padding, and
    it is applied to BOTH the identifier and the claim, so neither spelling
    has to be the one that was stored.
    """
    return re.sub(r"0+(\d)", r"\1", folded)


def names_source(claim_text: str, identifier: str | None,
                 name: str | None) -> bool:
    """Whether this claim names this document. Pure.

    True when the claim carries the document's identifier in any of its
    legitimate spellings, or carries its name. A document with neither an
    identifier nor a name cannot be named and is not held against the claim —
    that is a gap in what was ingested, not something the drafter can fix by
    writing differently.
    """
    if not (identifier or name):
        return True
    body = _fold(claim_text)
    if not body:
        return False
    folded_id = _fold(identifier or "")
    if len(folded_id) >= 3 and _unpad(folded_id) in _unpad(body):
        return True
    words = [w for w in _significant_words(name or "") if w]
    if not words:
        return False
    # A source is named the way a person names it — by the words that identify
    # it, not by the whole stored title with its trailing apparatus. Two
    # significant words is enough to identify it, and enough to keep a claim
    # that merely shares one common word with a title from passing.
    hits = sum(1 for w in words if w in body)
    return hits >= min(2, len(words))


#: Folded words shorter than this identify nothing on their own.
MIN_NAME_WORD = 4


def _significant_words(name: str) -> list[str]:
    """The words of a document's name that identify it. Pure.

    Short words carry no identity once folded, and a stored title is mostly
    apparatus around the part a person would say out loud.
    """
    # Split on non-word characters with the UNICODE flag, not on a Latin-1
    # character class: a class written as A-Za-zÀ-ÿ treats every letter above
    # it as a separator, so "České" arrived as "eské" and the source could
    # never be matched by name. The corpus is not Latin-1.
    parts = re.split(r"\W+", str(name or ""), flags=re.UNICODE)
    out = []
    for part in parts:
        folded = _fold(part)
        if len(folded) >= MIN_NAME_WORD:
            out.append(folded)
    return out[:4]


def unnamed_sources(claims: list[dict], documents: dict[str, dict]) -> list[dict]:
    """Claims asserting a rule that do not name a source they must. Pure.

    `claims` carry `sequence`, `kind`, `text` and `cites` (the filenames the
    claim's evidence points at). `documents` maps a filename to
    `{"identifier", "name", "naming_required"}`, as the deployment declared
    it. Reported per claim with the sources it failed to name, so feedback can
    say which.
    """
    out: list[dict] = []
    for claim in claims:
        if str(claim.get("kind") or "") not in NAMED_KINDS:
            continue
        text = str(claim.get("text") or "")
        missed = []
        for filename in claim.get("cites") or []:
            doc = documents.get(str(filename)) or {}
            if not doc.get("naming_required"):
                continue
            if not names_source(text, doc.get("identifier"), doc.get("name")):
                missed.append({
                    "filename": str(filename),
                    "identifier": doc.get("identifier"),
                    "name": doc.get("name"),
                })
        if missed:
            out.append({"sequence": claim.get("sequence"),
                        "claim_id": claim.get("claim_id"),
                        "unnamed": missed})
    return out


def objection(entry: dict) -> str:
    """What is put to the drafter about one unnamed source. Pure.

    Names what the claim must carry, not how the sentence should read: the
    wording is the drafter's, and what a reader of this field expects belongs
    to the deployment's playbook.
    """
    first = (entry.get("unnamed") or [{}])[0]
    ident = str(first.get("identifier") or "").strip()
    name = str(first.get("name") or "").strip()
    handle = ident or name
    both = f"{name} ({ident})" if ident and name else handle
    return (
        f"Claim {entry.get('sequence')} asserts a rule on a source a reader "
        f"must be able to identify, and the claim does not name it. Name it "
        f"in the sentence — {both} — so the proposition can be looked up and "
        f"weighed, rather than left to a marker at the end."
    )
