"""Front-matter parser and candidate inference.

A high-confidence prior for the suggest pipeline: when documents arrive with
YAML front-matter, the keys themselves are evidence of the schema the source
system already uses. This module is pure (no DB, no LLM) so it can run
synchronously over a sample.

Spec (kept deliberately small):

  - Recognised front-matter is a YAML block delimited by `---` lines at the
    very start of the markdown content.
  - Top-level scalars         → property candidate, cardinality "one".
  - Top-level lists of scalars → property candidate, cardinality "many".
  - Top-level lists of dicts   → entity candidate; the dict shape becomes the
                                 entity's likely properties.
  - Reserved key `sgr:`      → optional override block:
        sgr:
          class: my_class_slug
          properties:
            fine_amount: { type: money, cardinality: one }
          ignore: [content_hash, staleness_key]
"""

from __future__ import annotations

from typing import Any

import yaml

RESERVED_KEY = "sgr"
# Bookkeeping fields the SPIP exporter (and similar pipelines) emit; never
# useful as SGR properties. Users can extend this via sgr.ignore.
DEFAULT_IGNORE: frozenset[str] = frozenset(
    {"content_hash", "metadata_hash", "staleness_key"}
)
# Top-level keys that, if present as scalar strings, are treated as the
# document's declared class. `sgr.class` always wins if both are present.
CLASS_KEYS: tuple[str, ...] = (
    "document_class",
    "document_type",
    "category",
    "type",
    "kind",
    "class",
)


def split_front_matter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Return (front_matter_dict_or_None, body_text).

    Returns (None, original_text) if no front-matter, malformed YAML, or the
    YAML root isn't a mapping.
    """
    if not text:
        return None, text
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return None, text
    block = "\n".join(lines[1:end])
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None, text
    if not isinstance(data, dict):
        return None, text
    body = "\n".join(lines[end + 1 :])
    return data, body


def infer_candidates(
    fm: dict[str, Any], extra_ignore: set[str] | None = None
) -> list[dict[str, Any]]:
    """Walk top-level keys; return candidate dicts ready to store as
    DiscoveryCandidate payloads.

    Each candidate has:
      - kind: "document_class" | "document_class_property" | "entity_type"
      - name, plus shape fields (cardinality, schema, sample_value(s) /
        object_shape, sample_canonical_form)
    """
    sgr_block = fm.get(RESERVED_KEY) if isinstance(fm.get(RESERVED_KEY), dict) else {}
    overrides: dict[str, Any] = sgr_block.get("properties") or {}
    explicit_ignore = set(sgr_block.get("ignore") or [])
    skip = (extra_ignore or set()) | DEFAULT_IGNORE | {RESERVED_KEY} | explicit_ignore

    out: list[dict[str, Any]] = []

    # Document-class hint: explicit sgr.class wins; otherwise first matching
    # well-known key. Whichever key is used is also added to skip so it's not
    # also proposed as a property.
    class_value: str | None = None
    if isinstance(sgr_block.get("class"), str) and sgr_block["class"].strip():
        class_value = sgr_block["class"].strip()
    else:
        for k in CLASS_KEYS:
            v = fm.get(k)
            if isinstance(v, str) and v.strip():
                class_value = v.strip()
                skip = skip | {k}
                break
    if class_value:
        out.append(
            {
                "kind": "document_class",
                "name": class_value,
                "source_key": "sgr.class" if isinstance(sgr_block.get("class"), str) else None,
            }
        )

    for key, value in fm.items():
        if key in skip:
            continue
        candidate = _classify(key, value)
        if candidate is None:
            continue
        if key in overrides and isinstance(overrides[key], dict):
            candidate.update(overrides[key])
        out.append(candidate)
    return out


def _classify(key: str, value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool | int | float | str):
        return {
            "kind": "document_class_property",
            "name": key,
            "cardinality": "one",
            "schema": _scalar_schema(value),
            "sample_value": value,
        }
    if isinstance(value, list):
        if not value:
            return None
        if all(isinstance(v, bool | int | float | str) for v in value):
            return {
                "kind": "document_class_property",
                "name": key,
                "cardinality": "many",
                "schema": _scalar_schema(value[0]),
                "sample_values": value[:5],
            }
        if all(isinstance(v, dict) for v in value):
            return {
                "kind": "entity_type",
                "name": _singularize(key),
                "object_shape": _merge_keys(value),
                "sample_canonical_form": _pick_canonical_form(value[0]),
                "source_key": key,
            }
        return None  # mixed-type list: skip rather than guess
    if isinstance(value, dict):
        return {
            "kind": "document_class_property",
            "name": key,
            "cardinality": "one",
            "schema": {"type": "object"},
            "sample_value": value,
        }
    return None


def _scalar_schema(v: Any) -> dict[str, str]:
    if isinstance(v, bool):
        return {"type": "boolean"}
    if isinstance(v, int):
        return {"type": "integer"}
    if isinstance(v, float):
        return {"type": "number"}
    return {"type": "string"}


def _singularize(name: str) -> str:
    if name.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss") and len(name) > 1:
        return name[:-1]
    return name


def _merge_keys(rows: list[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for r in rows:
        keys.update(r.keys())
    return sorted(keys)


def _pick_canonical_form(row: dict[str, Any]) -> str | None:
    for k in ("canonical_form", "name", "title", "label"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


# ─────────────────────── corpus-level aggregation ───────────────────────


def aggregate_candidates(
    per_doc: list[tuple[Any, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Cluster per-document candidates into one consolidated proposal per
    `(kind, name)`. Trivial dedupe — front-matter keys are canonical, so
    string equality is the right cluster key.

    Input: list of (document_id, candidates).
    Output: list of consolidated proposals, each with `supporting_doc_ids`
            and aggregated sample values.
    """
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    for doc_id, cands in per_doc:
        for c in cands:
            ck = (c["kind"], c["name"])
            cluster = clusters.setdefault(
                ck,
                {
                    "kind": c["kind"],
                    "name": c["name"],
                    "supporting_doc_ids": [],
                    "samples": [],
                    "_first": c,
                },
            )
            cluster["supporting_doc_ids"].append(doc_id)
            if c["kind"] == "document_class_property":
                if "sample_value" in c:
                    cluster["samples"].append(c["sample_value"])
                if "sample_values" in c:
                    cluster["samples"].extend(c["sample_values"])
            elif c["kind"] == "entity_type":
                form = c.get("sample_canonical_form")
                if form:
                    cluster["samples"].append(form)
            # document_class needs no per-doc sample collection; the cluster
            # name *is* the class name and supporting_doc_ids carries volume.

    out: list[dict[str, Any]] = []
    for cluster in clusters.values():
        first = cluster.pop("_first")
        n_docs = len(cluster["supporting_doc_ids"])
        proposal: dict[str, Any]
        if first["kind"] == "document_class_property":
            # Cardinality: any "many" observation wins.
            cardinality = first.get("cardinality", "one")
            proposal = {
                "kind": "document_class_property",
                "name": first["name"],
                "description": (
                    f"Inferred from front-matter `{first['name']}` field "
                    f"(seen in {n_docs} sample documents)."
                ),
                "schema": first.get("schema") or {"type": "string"},
                "cardinality": cardinality,
                "guidance": (
                    "Front-matter prior — this property was declared in the "
                    "document's YAML header. The ingestion pipeline can "
                    "populate it deterministically; LLM extraction can serve "
                    "as a fallback when the header is missing."
                ),
                "sample_values": _dedupe_samples(cluster["samples"], limit=10),
                "supporting_doc_ids": cluster["supporting_doc_ids"],
            }
        elif first["kind"] == "entity_type":
            proposal = {
                "kind": "entity_type",
                "name": first["name"],
                "description": (
                    f"Inferred from front-matter `{first.get('source_key', first['name'])}` "
                    f"list (seen in {n_docs} sample documents)."
                ),
                "guidance": (
                    "Front-matter prior — instances of this entity appear as "
                    "structured objects in the YAML header."
                ),
                "object_shape_hint": first.get("object_shape", []),
                "sample_canonical_forms": _dedupe_samples(cluster["samples"], limit=10),
                "supporting_doc_ids": cluster["supporting_doc_ids"],
            }
        else:  # document_class
            proposal = {
                "kind": "document_class",
                "name": first["name"],
                "description": (
                    f"Inferred from front-matter class declaration "
                    f"(seen in {n_docs} sample documents)."
                ),
                "classification_hints": (
                    "Front-matter prior — documents of this class declare "
                    f"`{first['name']}` as their type/category in the YAML "
                    "header. Treat that header as authoritative; for "
                    "documents without it, infer from content."
                ),
                "supporting_doc_ids": cluster["supporting_doc_ids"],
            }
        out.append(proposal)
    return out


def _dedupe_samples(values: list[Any], limit: int) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for v in values:
        # Not all values are hashable; fall back to repr equality.
        try:
            key = v
            if key in seen:
                continue
            seen.add(key)
        except TypeError:
            r = repr(v)
            if r in seen:
                continue
            seen.add(r)
        out.append(v)
        if len(out) >= limit:
            break
    return out


# Promotes "cardinality=many" if any one document declared it as a list.
def upgrade_cardinality(proposals: list[dict[str, Any]], per_doc_raw: list[tuple[Any, list[dict[str, Any]]]]) -> None:
    """Mutates `proposals` in place: any property where at least one source
    candidate had cardinality='many' becomes 'many'."""
    many_keys: set[str] = set()
    for _doc_id, cands in per_doc_raw:
        for c in cands:
            if c["kind"] == "document_class_property" and c.get("cardinality") == "many":
                many_keys.add(c["name"])
    for p in proposals:
        if p["kind"] == "document_class_property" and p["name"] in many_keys:
            p["cardinality"] = "many"
