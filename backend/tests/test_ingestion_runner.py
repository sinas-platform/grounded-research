"""Pipeline runner behavior: parts selection.

The runner executes the selected parts per document, in canonical order —
extract (with artifact wipe), ground, resolve, relationships, dossiers —
and a subset run skips the wipe and the unselected passes. The batch/
stage/polling machinery this file used to test is gone; run state is
advanced by the in-process worker alone.

Run from the backend directory:
`python -m pytest tests/test_ingestion_runner.py`
"""

import contextlib
import uuid
from types import SimpleNamespace

import pytest


class _FakeSession:
    async def get(self, model, pk):
        return SimpleNamespace(status="running", done_units=0, failed_units=0)

    async def execute(self, stmt):
        unit = SimpleNamespace(status="running", error=None, completed_at=None)
        return SimpleNamespace(scalar_one_or_none=lambda: unit)

    async def commit(self):
        pass


def _patch_world(monkeypatch, calls):
    from app.services import ingestion_runner as runner

    @contextlib.asynccontextmanager
    async def _fake_session_factory():
        yield _FakeSession()

    async def _wipe(session, did):
        calls.append("wipe")

    async def _terminal(run_id):
        calls.append("terminal")

    monkeypatch.setattr(runner, "AsyncSessionLocal", _fake_session_factory)
    monkeypatch.setattr(runner, "_wipe_extracted_artifacts", _wipe)
    monkeypatch.setattr(runner, "_mark_run_terminal_if_done", _terminal)

    import app.services.dossier_oneshot as do
    import app.services.entity_resolver as er
    import app.services.grounding_gate as gg
    import app.services.ingestion_oneshot as io_
    import app.services.relationship_oneshot as ro

    def _recorder(name, ret):
        async def _f(ids, **kw):
            calls.append(name)
            return ret

        return _f

    monkeypatch.setattr(io_, "oneshot_ingest", _recorder("extract", [{}]))
    monkeypatch.setattr(gg, "ground_documents", _recorder("ground", []))
    monkeypatch.setattr(er, "resolve_unlinked", _recorder("resolve", []))
    monkeypatch.setattr(ro, "extract_relationships", _recorder("relationships", []))
    monkeypatch.setattr(do, "assign_dossiers", _recorder("dossiers", []))
    return runner


@pytest.mark.asyncio
async def test_parts_subset_runs_only_selected_passes(monkeypatch):
    """parts=('relationships',) must run ONLY the relationship pass:
    no wipe, no extract, no grounding, no resolution, no dossiers."""
    calls: list[str] = []
    runner = _patch_world(monkeypatch, calls)
    await runner._run_pipeline_inprocess(
        uuid.uuid4(), [uuid.uuid4()], parts=("relationships",)
    )
    assert "relationships" in calls
    for name in ("wipe", "extract", "ground", "resolve", "dossiers"):
        assert name not in calls, name


@pytest.mark.asyncio
async def test_default_parts_run_everything_in_order(monkeypatch):
    calls: list[str] = []
    runner = _patch_world(monkeypatch, calls)
    await runner._run_pipeline_inprocess(uuid.uuid4(), [uuid.uuid4()])
    assert calls[:6] == [
        "wipe", "extract", "ground", "resolve", "relationships", "dossiers"
    ]


@pytest.mark.asyncio
async def test_extract_error_skips_downstream_passes(monkeypatch):
    calls: list[str] = []
    runner = _patch_world(monkeypatch, calls)

    import app.services.ingestion_oneshot as io_

    async def _failing_extract(ids, **kw):
        calls.append("extract")
        return [{"error": "boom"}]

    monkeypatch.setattr(io_, "oneshot_ingest", _failing_extract)
    await runner._run_pipeline_inprocess(uuid.uuid4(), [uuid.uuid4()])
    assert "extract" in calls
    for name in ("ground", "resolve", "relationships", "dossiers"):
        assert name not in calls, name


def test_legacy_stages_key_is_rejected_not_silently_ignored():
    """A caller still sending the retired `stages` key must get a
    validation error, not a silent full-pipeline run (the 5x-cost trap
    sgr_sink hit on 7 Aug)."""
    import pydantic
    import pytest as _pytest

    from app.schemas.ingestion import RunCreateIn

    with _pytest.raises(pydantic.ValidationError):
        RunCreateIn(stages=["oneshot"], filter={})
    # the modern shape still validates
    assert RunCreateIn(parts=["relationships"]).parts == ["relationships"]
