"""Shared introspect implementation.

The same distributions math is invoked from two places:
  - `POST /retrieval/introspect` — stateless, caller passes a GroveFilter
  - `POST /retrieval/results/{id}/introspect` — uses Result.filter
Both call `introspect_with_filter` so the math has one home.

field_filters narrow the count and (via faceted semantics) every other field's
distribution. See the stateful-filter ADR for the design discussion.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import literal

from app.auth import CallerIdentity
from app.models import Document, DocumentClassProperty, PropertyValue
from app.schemas.runtime import (
    FieldFilter,
    GroveFilter,
    IntrospectFieldDistribution,
    IntrospectOut,
)
from app.services.visibility import visible_clause


def _unwrap_property_value(v: Any) -> Any:
    if isinstance(v, dict) and set(v.keys()) == {"_"}:
        return v["_"]
    return v


def _field_filter_exists_clause(ff: FieldFilter, document_class_id):
    """Build an EXISTS clause: there is a PropertyValue for this document
    whose property (by name, scoped to the given document class) matches the
    field filter's operator and value(s).

    PropertyValue.value is stored as JSONB of shape `{"_": X}` where X is a
    scalar OR a list (when the property is cardinality="many"). Comparisons
    happen on `value->'_'`. Equality / membership accepts either scalar match
    or list containment, so it works on both shapes transparently.
    """
    inner = PropertyValue.value["_"]
    inner_text = inner.astext

    op = ff.op
    values_in = ff.values if ff.values else ([ff.value] if ff.value is not None else [])

    if op == "eq":
        # eq accepts a single value; if `values` was passed treat first.
        v = values_in[0] if values_in else None
        op_clause = or_(
            inner_text == str(v),
            inner.op("@>")(func.to_jsonb(literal(v))),
        )
    elif op == "in":
        if not values_in:
            return None
        scalar_clause = inner_text.in_([str(v) for v in values_in])
        # JSONB array containment for any one of the candidate values.
        contains_clauses = [
            inner.op("@>")(func.to_jsonb(literal(v))) for v in values_in
        ]
        op_clause = or_(scalar_clause, *contains_clauses)
    elif op == "neq":
        v = values_in[0] if values_in else None
        # neq is "no value equals" — strict scalar/list non-match. Note this
        # still requires a row to exist; documents that have NO value for the
        # property won't be selected. Matches "neq" semantics in most facet
        # libraries; revisit if we want NULL inclusion.
        op_clause = and_(
            inner_text != str(v),
            ~inner.op("@>")(func.to_jsonb(literal(v))),
        )
    elif op in ("gte", "lte", "gt", "lt"):
        v = values_in[0] if values_in else None
        sval = str(v)
        # Text-cast comparison works for ISO dates and zero-padded strings;
        # numeric ranges that need true numeric ordering can be added with a
        # cast variant if a real case appears (revisit when extending FieldFilter
        # with a typed shape).
        ops = {
            "gte": lambda lhs, rhs: lhs >= rhs,
            "lte": lambda lhs, rhs: lhs <= rhs,
            "gt": lambda lhs, rhs: lhs > rhs,
            "lt": lambda lhs, rhs: lhs < rhs,
        }
        op_clause = ops[op](inner_text, sval)
    else:
        return None

    prop_subq = (
        select(DocumentClassProperty.id)
        .where(DocumentClassProperty.document_class_id == document_class_id)
        .where(DocumentClassProperty.name == ff.field)
        .scalar_subquery()
    )
    return exists(
        select(PropertyValue.id).where(
            PropertyValue.document_id == Document.id,
            PropertyValue.property_id == prop_subq,
            op_clause,
        )
    )


def apply_grove_filter(
    stmt, f: GroveFilter, *, skip_field: str | None = None
):
    """Narrow a Document-selecting statement by the supported clauses.

    Narrows on: staged state, class, explicit excludes, FTS, and field_filters
    (when document_class_id is set, since field_filters resolve by name within
    a class).

    `skip_field` lets the faceted-distribution code request "all filters except
    this one" so each field's distribution shows values it could narrow to next.

    regex_filters and dossier scoping remain TODO — wire in when a use case
    needs them in the count.
    """
    stmt = stmt.where(Document.staged.is_(False))
    if f.document_class_id is not None:
        stmt = stmt.where(Document.document_class_id == f.document_class_id)
    if f.explicit_excludes:
        stmt = stmt.where(~Document.id.in_(f.explicit_excludes))
    if f.text_search:
        from sqlalchemy import text as sql_text

        # websearch_to_tsquery gives callers web-search syntax: terms are
        # AND-ed by default, `OR` unions, `-term` excludes, and "quoted
        # phrases" match adjacent words. The query is passed through raw so
        # the caller decides the semantics; malformed input never raises.
        # 'simple' matches the index: content_tsvector is built with
        # to_tsvector('simple', ...) (0001_baseline trigger), so the query
        # must tokenize the same way. The default (english) stems query terms
        # the simple index never contains ("delivery" -> "deliveri"), which
        # silently drops them from matching.
        stmt = stmt.where(
            Document.id.in_(
                select(sql_text("document_version.document_id"))
                .select_from(sql_text("document_version"))
                .where(sql_text(
                    "content_tsvector @@ websearch_to_tsquery('simple', :q)"))
                .params(q=f.text_search)
            )
        )
    if f.document_class_id is not None and f.field_filters:
        for ff in f.field_filters:
            if skip_field is not None and ff.field == skip_field:
                continue
            clause = _field_filter_exists_clause(ff, f.document_class_id)
            if clause is not None:
                stmt = stmt.where(clause)
    return stmt


async def count_candidates(
    session: AsyncSession, caller: CallerIdentity, f: GroveFilter
) -> int:
    read_all = await caller.has_permission("grove.documents.read:all")
    base = select(Document.id).where(visible_clause(Document, caller, read_all=read_all))
    base = apply_grove_filter(base, f)
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    return int(total)


async def matching_document_ids(
    session: AsyncSession, caller: CallerIdentity, f: GroveFilter, limit: int
) -> list[uuid.UUID]:
    """Return the document ids matching the filter (instead of a count).

    Same selection logic as `count_candidates` — visibility scope plus
    `apply_grove_filter` — but enumerates the matches so the caller can pull
    them into a Result. `DISTINCT` guards against row fan-out if a future
    clause joins; ordered by id for stable truncation under `limit`.
    """
    read_all = await caller.has_permission("grove.documents.read:all")
    stmt = select(Document.id).where(
        visible_clause(Document, caller, read_all=read_all)
    )
    stmt = apply_grove_filter(stmt, f)
    stmt = stmt.distinct().order_by(Document.id).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def introspect_with_filter(
    session: AsyncSession,
    caller: CallerIdentity,
    f: GroveFilter,
    fields: list[str] | None,
    top_k: int,
) -> IntrospectOut:
    read_all = await caller.has_permission("grove.documents.read:all")
    base = select(Document.id).where(visible_clause(Document, caller, read_all=read_all))
    base = apply_grove_filter(base, f)
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    distributions: list[IntrospectFieldDistribution] = []
    if f.document_class_id is not None:
        properties = (
            await session.execute(
                select(DocumentClassProperty).where(
                    DocumentClassProperty.document_class_id == f.document_class_id
                )
            )
        ).scalars().all()
        for prop in properties:
            if fields and prop.name not in fields:
                continue
            # Faceted: this field's distribution narrows by ALL other
            # field_filters but NOT by its own — so the user can see other
            # candidate values to widen / pick from. This is standard facet UX
            # ("filter by Pharmaceutical, still see all sectors in the sector
            # facet so I can switch").
            field_base = select(Document.id).where(
                visible_clause(Document, caller, read_all=read_all)
            )
            field_base = apply_grove_filter(field_base, f, skip_field=prop.name)
            field_total = (
                await session.execute(
                    select(func.count()).select_from(field_base.subquery())
                )
            ).scalar_one()
            counts = (
                await session.execute(
                    select(PropertyValue.value, func.count().label("n"))
                    .where(PropertyValue.property_id == prop.id)
                    .where(PropertyValue.document_id.in_(field_base))
                    .group_by(PropertyValue.value)
                    .order_by(func.count().desc())
                    .limit(top_k)
                )
            ).all()
            distributions.append(
                IntrospectFieldDistribution(
                    field=prop.name,
                    values=[
                        {"value": _unwrap_property_value(c[0]), "count": int(c[1])}
                        for c in counts
                    ],
                    total_documents=int(field_total),
                )
            )
    return IntrospectOut(candidate_count=total, distributions=distributions)
