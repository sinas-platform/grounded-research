"""Tests for the citation-target resolver.

Pure tests (no DB): the golden generated SQL for a neutral test config,
config validation, and the endpoint model bounds. DB tests (skipped when no
database is reachable): decision semantics and the false-auto regressions,
run against self-contained fixtures created inside a transaction that is
always rolled back. Everything here uses t877-prefixed neutral names; the
engine carries no deployment config and neither do these tests.

Run from the backend directory: `python -m pytest tests/test_citation_resolver.py`
"""

import dataclasses
import re
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.services.citation_resolver import (
    FuzzyPath,
    IdentifierRule,
    KindRule,
    ResolverConfig,
    build_match_sql,
    config_from_dict,
)

PARAMS = {"auto": 0.55, "review": 0.40, "margin": 0.08}


def _id_rule(label="t877_id", patterns=(r"^T877-[0-9]+$",)):
    return IdentifierRule(
        label=label,
        patterns=patterns,
        classify_transforms=("trim", "uppercase"),
        key_transforms=("trim", "uppercase"),
        value_transforms=("trim", "uppercase"),
    )


def _t877_config(instrument_auto: bool) -> ResolverConfig:
    return ResolverConfig(
        citation_reldefs=("t877_refs", "t877_cli"),
        reldef_path_overrides={"t877_cli": "legal_instrument"},
        bridge_reldefs=("t877_full_text_of",),
        identifier_rules=(_id_rule(),),
        kind_rules=(KindRule(path="name", kinds=("t877_name",)),),
        fuzzy_paths=(
            FuzzyPath(label="name", entity_type="T877 Type", method="fuzzy_name"),
            FuzzyPath(label="legal_instrument", entity_type="T877 Instrument",
                      method="fuzzy_instrument", auto_resolve=instrument_auto),
        ),
    )


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ── pure: golden SQL ─────────────────────────────────────────────────────────


def test_generated_sql_matches_golden():
    golden = (Path(__file__).parent / "golden_citation_resolver.sql").read_text(
        encoding="utf-8"
    )
    assert _norm(build_match_sql(_t877_config(instrument_auto=False))) == _norm(golden)


def test_config_round_trips_through_dict():
    cfg = _t877_config(instrument_auto=False)
    assert config_from_dict(dataclasses.asdict(cfg)) == cfg


# ── pure: config validation ──────────────────────────────────────────────────


def test_empty_citation_reldefs_raises():
    with pytest.raises(ValueError, match="citation_reldefs"):
        build_match_sql(ResolverConfig(citation_reldefs=()))


def test_no_rules_at_all_raises():
    with pytest.raises(ValueError, match="identifier rule or fuzzy path"):
        build_match_sql(ResolverConfig(citation_reldefs=("refs",)))


def test_identifier_rules_without_bridge_raises():
    with pytest.raises(ValueError, match="bridge_reldefs"):
        build_match_sql(
            ResolverConfig(citation_reldefs=("refs",), identifier_rules=(_id_rule(),))
        )


def test_minimal_fuzzy_only_config_builds():
    sql = build_match_sql(
        ResolverConfig(
            citation_reldefs=("refs",),
            fuzzy_paths=(FuzzyPath(label="name", entity_type="Thing", method="fuzzy_name"),),
            kind_rules=(KindRule(path="name", kinds=("thing_name",)),),
        )
    )
    assert "WITH parked" in sql and "IN ()" not in sql


def test_minimal_identifier_only_config_builds():
    sql = build_match_sql(
        ResolverConfig(
            citation_reldefs=("refs",),
            bridge_reldefs=("canonical_text_of",),
            identifier_rules=(_id_rule(),),
        )
    )
    assert "WITH parked" in sql and "IN ()" not in sql


# ── pure: endpoint model ─────────────────────────────────────────────────────


def _cfg_dict() -> dict:
    return dataclasses.asdict(_t877_config(instrument_auto=False))


def test_endpoint_thresholds_bounded():
    from app.api.v1.ingestion import ResolveCitationsIn

    assert ResolveCitationsIn(config=_cfg_dict()).dry_run is True
    with pytest.raises(ValidationError):
        ResolveCitationsIn(config=_cfg_dict(), margin=-0.1)
    with pytest.raises(ValidationError):
        ResolveCitationsIn(config=_cfg_dict(), auto_threshold=1.5)
    with pytest.raises(ValidationError):
        ResolveCitationsIn(config=_cfg_dict(), auto_threshold=0.5, review_threshold=0.9)
    with pytest.raises(ValidationError):
        ResolveCitationsIn()  # config is required


def test_endpoint_has_permission_dependency():
    from app.api.v1.ingestion import router

    route = next(r for r in router.routes if r.path.endswith("/resolve-citations"))
    assert route.dependencies


@pytest.mark.asyncio
async def test_endpoint_rejects_invalid_config():
    from fastapi import HTTPException

    import app.api.v1.ingestion as ing

    with pytest.raises(HTTPException) as exc:
        await ing.resolve_citations(ing.ResolveCitationsIn(config={"citation_reldefs": []}))
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_default_body_is_dry_run(monkeypatch):
    import app.api.v1.ingestion as ing

    captured = {}

    async def _fake_resolver(**kwargs):
        captured.update(kwargs)
        return {"executed": kwargs["execute"]}

    monkeypatch.setattr(ing, "run_citation_resolver", _fake_resolver)
    out = await ing.resolve_citations(ing.ResolveCitationsIn(config=_cfg_dict()))
    assert captured["execute"] is False and out == {"executed": False}
    assert captured["config"] == _t877_config(instrument_auto=False)


# ── DB tests: fixtures in a rolled-back transaction ──────────────────────────


async def _db_session():
    from app.db import AsyncSessionLocal

    try:
        session = AsyncSessionLocal()
        await session.execute(text("SELECT 1"))
        return session
    except Exception:
        return None


async def _insert_universe(session):
    """Self-contained t877 universe. Returns ids. Never committed."""
    ids = {k: uuid.uuid4() for k in (
        "et", "et_instr", "dc", "prop", "d1", "d2", "e1", "e2", "e3", "e4",
        "bridge", "refs", "cli", "ur_ambig", "ur_none", "ur_tie",
        "ur_handbook", "ur_reg", "owner",
    )}
    x = session.execute

    await x(text("INSERT INTO entity_type (id, name) VALUES (:i, 'T877 Type')"),
            {"i": ids["et"]})
    await x(text("INSERT INTO entity_type (id, name) VALUES (:i, 'T877 Instrument')"),
            {"i": ids["et_instr"]})
    await x(text("INSERT INTO document_class (id, name, slug) VALUES (:i, 'T877 Class', 't877_class')"),
            {"i": ids["dc"]})
    await x(text("INSERT INTO document_class_property (id, document_class_id, name) "
                 "VALUES (:i, :dc, 't877_id')"), {"i": ids["prop"], "dc": ids["dc"]})
    for d in ("d1", "d2"):
        await x(text("INSERT INTO document (id, filename, owner_id, document_class_id) "
                     "VALUES (:i, :f, :o, :dc)"),
                {"i": ids[d], "f": f"t877-{d}.md", "o": ids["owner"], "dc": ids["dc"]})
        await x(text("INSERT INTO property_value (id, property_id, document_id, value) "
                     "VALUES (:i, :p, :d, '{\"_\": \"T877-0001\"}'::jsonb)"),
                {"i": uuid.uuid4(), "p": ids["prop"], "d": ids[d]})
    for e, cf, et in (("e1", "T877 Case One", "et"), ("e2", "T877 Case Two", "et"),
                      ("e3", "Tie Target Alpha", "et"), ("e4", "Tie Target Alpha", "et")):
        await x(text("INSERT INTO entity (id, entity_type_id, canonical_form) "
                     "VALUES (:i, :t, :c)"), {"i": ids[e], "t": ids[et], "c": cf})
    # the false-auto shape: candidates share a long prefix with the key and
    # differ only in the number; the runner-up is distant so the margin gate
    # alone would not stop an auto
    for cf in ("Draft Section 102 Handbook",
               "Handbook on the application of Section 101(3)",
               "Common Framework Regulation 1400/2002",
               "Interim Notice 77/2001"):
        await x(text("INSERT INTO entity (id, entity_type_id, canonical_form) "
                     "VALUES (:i, :t, :c)"),
                {"i": uuid.uuid4(), "t": ids["et_instr"], "c": cf})
    for rd, name in (("bridge", "t877_full_text_of"), ("refs", "t877_refs"), ("cli", "t877_cli")):
        await x(text(
            "INSERT INTO relationship_definition "
            "(id, name, source_ref_type, source_ref_id, target_ref_type, target_ref_id) "
            "VALUES (:i, :n, 'document_class', :dc, 'entity_type', :et)"),
            {"i": ids[rd], "n": name, "dc": ids["dc"], "et": ids["et"]})
    # bridge: each document is the canonical text of a different entity, so
    # the shared identifier value reaches two entities (ambiguous)
    for d, e in (("d1", "e1"), ("d2", "e2")):
        await x(text("INSERT INTO relationship (relationship_definition_id, source_id, target_id) "
                     "VALUES (:rd, :s, :t)"), {"rd": ids["bridge"], "s": ids[d], "t": ids[e]})
    rows = (
        ("ur_ambig", "refs", "T877-0001", None),
        ("ur_none", "refs", "T877-0002", None),
        ("ur_tie", "refs", "Tie Target Alpha", "t877_name"),
        ("ur_handbook", "cli", "Section 101(3) Handbook", "handbook"),
        ("ur_reg", "cli", "Common Framework Regulation 1218/2010", "framework"),
    )
    for key, rd, tk, kind in rows:
        await x(text("INSERT INTO unresolved_relationship "
                     "(id, relationship_definition_id, source_id, target_key, target_key_kind, status) "
                     "VALUES (:i, :rd, :s, :tk, :k, 'unresolved')"),
                {"i": ids[key], "rd": ids[rd], "s": uuid.uuid4(), "tk": tk, "k": kind})
    return ids


async def _decisions(session, cfg):
    rows = (await session.execute(text(build_match_sql(cfg)), PARAMS)).mappings().all()
    return {str(r["ur_id"]): r for r in rows}


@pytest.mark.asyncio
async def test_decision_semantics_and_false_auto_regression():
    session = await _db_session()
    if session is None:
        pytest.skip("no database reachable")
    try:
        ids = await _insert_universe(session)
        by_id = await _decisions(session, _t877_config(instrument_auto=False))

        ambig = by_id[str(ids["ur_ambig"])]
        assert ambig["decision"] == "park" and ambig["id_count"] == 2
        assert ambig["target_entity_id"] is None

        none = by_id[str(ids["ur_none"])]
        assert none["decision"] == "park"

        tie = by_id[str(ids["ur_tie"])]
        assert tie["decision"] == "propose"  # top1 == top2, margin gate holds
        assert tie["target_entity_id"] is not None

        # the false-auto shape must never auto on a capped path
        for key in ("ur_handbook", "ur_reg"):
            row = by_id[str(ids[key])]
            assert row["decision"] != "auto", row["path"]

        # every propose verdict carries a concrete target
        for row in by_id.values():
            if row["decision"] == "propose":
                assert row["target_entity_id"] is not None

        # and the cap is what prevents them: with auto_resolve=True the same
        # fixtures do auto-resolve, which is exactly the observed failure
        by_id_open = await _decisions(session, _t877_config(instrument_auto=True))
        reproduced = [
            by_id_open[str(ids[k])]["decision"] == "auto"
            for k in ("ur_handbook", "ur_reg")
        ]
        assert any(reproduced)
    finally:
        await session.rollback()
        await session.close()
