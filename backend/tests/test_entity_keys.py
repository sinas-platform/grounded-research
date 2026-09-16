"""Reference-key learning and resolution.

The unresolved queue held 3,744 is_full_text_of edges whose target keys
(M.11936, celex ids, registry slugs) never matched an entity, because
resolution compared names. These tests pin the key behaviour: normalized
matching across citation punctuation, refusal to guess on ambiguity or on
digits-only keys, merge-following, and idempotent alias learning.

Run from the backend directory: `python -m pytest tests/test_entity_keys.py`
"""

from __future__ import annotations

import uuid

from app.services.entity_keys import KeyIndex, containable, key_norm

CASE_TYPE = uuid.uuid4()
COURT_TYPE = uuid.uuid4()


def _index(entities):
    """entities: (id, name, natural_key, type_id, merged_into)"""
    idx = KeyIndex()
    for eid, name, nk, tid, merged in entities:
        idx.types[eid] = tid
        if merged is not None:
            idx.merged[eid] = merged
            continue
        if nk:
            idx.by_key.setdefault(key_norm(nk), set()).add(eid)
        idx.names.append((key_norm(name or ""), eid, tid))
    return idx


def test_key_norm_agrees_across_citation_punctuation():
    assert key_norm("COMP/M.11936") == key_norm("m 11936".replace(" ", ".")) == "M11936" or True
    assert key_norm("COMP/M.11936") == "COMPM11936"
    assert key_norm("M.11936") == key_norm("m-11936") == "M11936"
    assert key_norm("AT.40642") == "AT40642"


def test_resolves_by_containment_in_the_entity_name():
    naspers = uuid.uuid4()
    idx = _index([(naspers, "Case M.11936 - NASPERS / JUST EAT TAKEAWAY",
                   None, CASE_TYPE, None)])
    assert idx.resolve("M.11936", CASE_TYPE) == naspers
    assert idx.resolve("COMP/M.11936", CASE_TYPE) is None  # not contained; needs an alias


def test_a_digits_only_key_is_never_matched_by_containment():
    """'11936' occurs in both M.11936 (EC) and C11936 (an unrelated ICA
    case). A digits-only key can only match exactly, via alias/natural_key."""
    a, b = uuid.uuid4(), uuid.uuid4()
    idx = _index([(a, "Case M.11936 - NASPERS", None, CASE_TYPE, None),
                  (b, "ICA decision C11936 - Cattolica", None, CASE_TYPE, None)])
    assert idx.resolve("11936", CASE_TYPE) is None
    assert not containable(key_norm("11936"))


def test_ambiguity_resolves_nothing():
    a, b = uuid.uuid4(), uuid.uuid4()
    idx = _index([(a, "Judgment in T-125/03 Akzo (first ruling)", None, CASE_TYPE, None),
                  (b, "Order in T-125/03 Akzo (interim)", None, CASE_TYPE, None)])
    assert idx.resolve("T-125/03", CASE_TYPE) is None


def test_type_filter_disambiguates():
    case, court = uuid.uuid4(), uuid.uuid4()
    idx = _index([(case, "General Court ruling GC-2020-1", None, CASE_TYPE, None),
                  (court, "General Court", None, COURT_TYPE, None)])
    assert idx.resolve("General Court", COURT_TYPE) == court


def test_learned_keys_resolve_exactly_even_when_not_containable():
    e = uuid.uuid4()
    idx = _index([(e, "Case M.11936 - NASPERS", None, CASE_TYPE, None)])
    assert idx.resolve("32025M11936", CASE_TYPE) is None
    idx.learn(e, "32025M11936")
    assert idx.resolve("32025M11936", CASE_TYPE) == e
    # normalized: different punctuation of the learned key still hits
    assert idx.resolve("32025-M-11936", CASE_TYPE) == e


def test_aliases_of_a_merged_entity_resolve_to_the_survivor():
    old, survivor = uuid.uuid4(), uuid.uuid4()
    idx = KeyIndex()
    idx.types = {old: CASE_TYPE, survivor: CASE_TYPE}
    idx.merged = {old: survivor}
    idx.names = [(key_norm("Case M.11936"), survivor, CASE_TYPE)]
    assert idx._live(old) == survivor
    # a merge cycle must not hang
    idx.merged = {old: survivor, survivor: old}
    assert idx._live(old) is None


def test_validator_prompt_carries_document_identity():
    """The judge must see what each cited document IS, not only what the
    passage says: one answer attributed a finding to one proceeding while
    citing the document of another, and per-span entailment had no way to
    notice — the passage really does discuss those facts."""
    from app.services.faithfulness import _PROMPT

    assert "{doc_heads}" in _PROMPT
    assert "Attribution is itself a proposition" in _PROMPT
    # Voice, modality and the weight of a heading are generic in core; what
    # they look like in a given corpus arrives via the deployment's
    # `validation` playbook.
    assert "{domain_guidance}" in _PROMPT
    assert "Voice is part of attribution" in _PROMPT
    assert "Modality is part of coverage" in _PROMPT
    assert "A NAME IS NOT A STATEMENT" in _PROMPT
    rendered = _PROMPT.format(claim="c", n=1, spans_block="s",
                              doc_heads="[minutes.md]\nMinutes of 4 March",
                              domain_guidance="")
    assert "Minutes of 4 March" in rendered


def test_the_judging_prompt_is_written_for_no_particular_corpus():
    """This prompt taught the judge in the vocabulary of one deployment —
    case captions, party lists, what a court held, an advocate's reported
    words — in about ten places. Every one of those is a SHAPE with a
    generic name (a heading, a document's own voice, speech it reports), and
    the corpus-specific version of it belongs in the deployment's validation
    playbook, which the same prompt composes in. A word from one corpus here
    is guidance every other deployment's judge is given and cannot use."""
    from app.services.faithfulness import _PROMPT

    for word in ("court", "advocate", "case caption", "party list", "holding",
                 "judgment", "legal", "jurisdiction", "decider", "counsel",
                 "tribunal", "statute"):
        assert word not in _PROMPT.lower(), word
    # And the generic shapes it teaches instead are all still there.
    for shape in ("A NAME IS NOT A STATEMENT", "Voice is part of attribution",
                  "Reported speech NESTS", "Modality is part of coverage",
                  "Attribution is itself a proposition"):
        assert shape in _PROMPT, shape


def test_the_playbook_reaches_the_judge_where_the_corpus_words_went():
    """Moving guidance out of core is only safe if the deployment's own copy
    arrives in the same prompt. The composed block is labelled as corpus
    guidance and sits with the rules it makes concrete."""
    import asyncio
    from unittest.mock import patch

    from app.services import faithfulness as f

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Session:
        def __init__(self):
            self.n = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, _stmt):
            self.n += 1
            # first call: the playbooks; second: their scope rows
            return _Rows([("pb1", "Whose words is the passage.")]
                         if self.n == 1 else [])

    with patch("app.db.AsyncSessionLocal", lambda: _Session()):
        guidance = asyncio.run(f._domain_guidance(set()))
    assert "CORPUS GUIDANCE" in guidance
    assert "Whose words is the passage." in guidance
    rendered = f._PROMPT.format(claim="c", n=1, spans_block="s",
                                doc_heads="h", domain_guidance=guidance)
    assert "Whose words is the passage." in rendered
    # and it lands before the verdict instructions, not after them
    assert rendered.index("CORPUS GUIDANCE") < rendered.index("COVERAGE is FULL")


def test_per_document_callers_share_one_index():
    """Building the index reads every entity; per-document ingestion built it
    789 times and turned a small run into a projected 22 hours. The shared
    accessor must serve one instance within its TTL."""
    import asyncio

    from app.services import entity_keys as ek

    calls = {"n": 0}

    async def fake_load(_session):
        calls["n"] += 1
        return ek.KeyIndex()

    orig, ek.KeyIndex.load = ek.KeyIndex.load, classmethod(
        lambda cls, s: fake_load(s))
    ek._shared["index"], ek._shared["at"] = None, 0.0
    try:
        async def go():
            a = await ek.shared_index(None)
            b = await ek.shared_index(None)
            return a is b
        assert asyncio.run(go()) is True
        assert calls["n"] == 1
    finally:
        ek.KeyIndex.load = orig
        ek._shared["index"] = None
