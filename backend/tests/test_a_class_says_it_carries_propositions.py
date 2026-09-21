"""A document class says whether its documents carry propositions, and the
store holds them per version.

A proposition is a statement a document establishes, applies or decides:
one sentence in the collection's working language with the line span it
rests on. Measured on 21 September 2026 against 14,511 documents of one
collection: hypotheses matched against propositions put every document an
expert review had asked for inside the top 15, from an English rule
against French, Dutch and Hungarian documents alike, where a summary index
had put the same documents in the hundreds. Which classes carry them is
the deployment's declaration, like `authority_label`, `naming_required`
and `standing`; the engine holds no list.

What these pin:

- the package field exists, defaults to false, and round-trips through
  import and export like the other class-level declarations;
- the store writes one document's propositions against its current
  version and replaces what that version had, so a second extraction
  leaves one copy;
- normalisation keeps order, drops empties and paragraphs, and stores a
  malformed span as none rather than as nonsense.
"""

from __future__ import annotations

import inspect

import pytest

from app.schemas.package import PackageDocumentClassEntry
from app.services import package as pkg
from app.services import propositions as props


def test_the_declaration_defaults_to_false():
    dc = PackageDocumentClassEntry(name="Ruling", slug="ruling")
    assert dc.propositions is False
    assert PackageDocumentClassEntry(name="Ruling", slug="ruling", propositions=True).propositions is True


def test_import_writes_it_and_export_hands_it_back():
    src = inspect.getsource(pkg)
    assert src.count('"propositions": dc.propositions') == 1, "the import writes the declaration"
    assert '"propositions": bool(dc.propositions)' in src, "the export round-trips it"


def test_normalisation_keeps_order_and_drops_what_is_not_a_statement():
    rows = props.normalise([
        {"holding": "  The tribunal   held the first thing. ", "lines": [12, 14]},
        {"holding": "", "lines": [1, 2]},
        {"holding": "x" * (props.MAX_CHARS + 1), "lines": [3, 3]},
        {"holding": "The second thing.", "lines": ["a", None]},
        {"holding": "The third thing.", "lines": [9, 4]},
        "not a dict",
    ])
    assert [r["text"] for r in rows] == [
        "The tribunal held the first thing.", "The second thing.", "The third thing."]
    assert [r["ordinal"] for r in rows] == [0, 1, 2]
    assert (rows[0]["line_from"], rows[0]["line_to"]) == (12, 14)
    assert rows[1]["line_from"] is None and rows[2]["line_from"] is None, "a malformed span is none"


class _Session:
    def __init__(self, version):
        self.version = version
        self.sql = []

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.sql.append((s, params))

        class _R:
            def __init__(self, v):
                self.v = v

            def first(self):
                return None if self.v is None else (self.v,)
        if "SELECT current_version_id" in s:
            return _R(self.version)
        return _R(None)

    async def commit(self):
        self.sql.append(("commit", None))


@pytest.mark.anyio
async def test_the_store_replaces_the_versions_rows():
    s = _Session(version="v-1")
    n = await props.store(s, "d-1", [{"holding": "One.", "lines": [1, 1]},
                                     {"holding": "Two.", "lines": [2, 3]}], language="en")
    assert n == 2
    kinds = [k for k, _ in s.sql]
    assert any("DELETE FROM proposition WHERE document_version_id" in k for k in kinds)
    inserts = [(k, p) for k, p in s.sql if "INSERT INTO proposition" in k]
    assert len(inserts) == 2
    assert inserts[0][1]["v"] == "v-1" and inserts[0][1]["o"] == 0 and inserts[1][1]["o"] == 1
    assert kinds.index(next(k for k in kinds if "DELETE" in k)) < kinds.index(inserts[0][0])


@pytest.mark.anyio
async def test_a_document_without_a_version_is_skipped_not_failed():
    s = _Session(version=None)
    assert await props.store(s, "d-2", [{"holding": "One.", "lines": [1, 1]}]) == 0
    assert not any("INSERT" in k for k, _ in s.sql)
