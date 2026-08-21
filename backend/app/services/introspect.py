"""Shared introspect implementation.

The same distributions math is invoked from two places:
  - `POST /retrieval/introspect` — stateless, caller passes a SgrFilter
  - `POST /retrieval/results/{id}/introspect` — uses Result.filter
Both call `introspect_with_filter` so the math has one home.

field_filters narrow the count and (via faceted semantics) every other field's
distribution. See the stateful-filter ADR for the design discussion.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import TEXT as sql_TEXT
from sqlalchemy import and_, exists, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import literal

from app.auth import CallerIdentity
from app.models import (
    Document,
    DocumentClass,
    DocumentClassProperty,
    DocumentVersion,
    EntityMention,
    PropertyValue,
)
from app.schemas.runtime import (
    EntityFilter,
    FieldFilter,
    SgrFilter,
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


def apply_sgr_filter(
    stmt, f: SgrFilter, *, skip_field: str | None = None
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
        #
        # The regconfig MUST be 'simple', matching the trigger that builds
        # content_tsvector (0001_baseline). Relying on the session default
        # ('english' in practice) stems query terms — dominance→domin,
        # competition→competit — which can never match the unstemmed lexemes
        # in a 'simple' index, silently zeroing most matches.
        stmt = stmt.where(
            Document.id.in_(
                select(sql_text("document_version.document_id"))
                .select_from(sql_text("document_version"))
                .where(
                    sql_text("content_tsvector @@ websearch_to_tsquery('simple', :q)")
                )
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
    if f.entity_filters:
        for ef in f.entity_filters:
            if not ef.entity_ids:
                continue
            stmt = stmt.where(
                exists(
                    select(EntityMention.id).where(
                        EntityMention.document_id == Document.id,
                        EntityMention.entity_id.in_(ef.entity_ids),
                        EntityMention.status == "active",
                    )
                )
            )
    return stmt


async def count_candidates(
    session: AsyncSession, caller: CallerIdentity, f: SgrFilter
) -> int:
    read_all = await caller.has_permission("sgr.documents.read:all")
    base = select(Document.id).where(visible_clause(Document, caller, read_all=read_all))
    base = apply_sgr_filter(base, f)
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    return int(total)


# How much of the summary to inline per match. The summary is what identifies a
# document when the filename is a numeric id (most of the corpus), so it has to
# be long enough to place the document but short enough that a full page of
# matches doesn't balloon the response the caller re-reads each turn.
SUMMARY_PREVIEW_CHARS = 240


async def matching_document_summaries(
    session: AsyncSession, caller: CallerIdentity, f: SgrFilter, limit: int
):
    """Enumerate the documents matching the filter (instead of a count).

    Same selection logic as `count_candidates` — visibility scope plus
    `apply_sgr_filter` — but returns, per match, the fields a caller needs
    to identify each document without a per-id get_document call: filename,
    class (id and name), and a summary preview. The class name comes from a
    left join to `document_class` (nullable FK — an inner join would drop
    unclassified documents).

    With a text search active, results are ordered by FTS relevance so the
    top `limit` are the best matches; otherwise by id for stable truncation.
    Relevance ranks the same rows the filter matched on: every version whose
    tsvector satisfies the query, aggregated per document with MAX. Ranking
    only the current version would score documents on text the filter never
    matched, and an inner join on `current_version_id` (nullable) would drop
    matched documents outright.
    """
    read_all = await caller.has_permission("sgr.documents.read:all")
    cols = (
        Document.id,
        Document.filename,
        Document.document_class_id,
        DocumentClass.name,
        func.left(Document.summary, SUMMARY_PREVIEW_CHARS),
    )
    stmt = (
        select(*cols)
        .where(visible_clause(Document, caller, read_all=read_all))
        .outerjoin(DocumentClass, DocumentClass.id == Document.document_class_id)
    )
    stmt = apply_sgr_filter(stmt, f)
    if f.text_search:
        # 'simple' pinned to match the index build — see apply_sgr_filter.
        # literal_column keeps it an unquoted-literal regconfig rather than a
        # text bind param, which asyncpg would fail to resolve to a regconfig.
        tsquery = func.websearch_to_tsquery(
            literal_column("'simple'"), f.text_search
        )
        rank = func.max(func.ts_rank(DocumentVersion.content_tsvector, tsquery))
        stmt = (
            stmt.join(
                DocumentVersion,
                and_(
                    DocumentVersion.document_id == Document.id,
                    DocumentVersion.content_tsvector.op("@@")(tsquery),
                ),
            )
            .group_by(*cols)
            .order_by(rank.desc(), Document.id)
            .limit(limit)
        )
    else:
        stmt = stmt.distinct().order_by(Document.id).limit(limit)
    rows = (await session.execute(stmt)).all()
    return list(rows)


async def introspect_with_filter(
    session: AsyncSession,
    caller: CallerIdentity,
    f: SgrFilter,
    fields: list[str] | None,
    top_k: int,
) -> IntrospectOut:
    read_all = await caller.has_permission("sgr.documents.read:all")
    base = select(Document.id).where(visible_clause(Document, caller, read_all=read_all))
    base = apply_sgr_filter(base, f)
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

        # Anti-facet suppression: identifier-like properties (titles, slugs,
        # urls — nearly one distinct value per document) can never narrow a
        # set, yet each costs two queries and bloats the payload the agent
        # re-reads every turn. When the caller didn't name fields explicitly,
        # skip properties whose values are ~all distinct. One aggregate query
        # replaces the two-per-property cost of computing their (useless)
        # distributions. Explicitly requested fields are never skipped.
        skip_ids: set = set()
        if not fields and properties:
            cardinality = (
                await session.execute(
                    select(
                        PropertyValue.property_id,
                        func.count().label("populated"),
                        func.count(func.distinct(PropertyValue.value.cast(sql_TEXT))).label("distinct_vals"),
                    )
                    .where(
                        PropertyValue.property_id.in_([p.id for p in properties])
                    )
                    .group_by(PropertyValue.property_id)
                )
            ).all()
            for pid, populated, distinct_vals in cardinality:
                if distinct_vals > 25 and distinct_vals >= 0.9 * populated:
                    skip_ids.add(pid)

        for prop in properties:
            if fields and prop.name not in fields:
                continue
            if prop.id in skip_ids:
                continue
            # Faceted: this field's distribution narrows by ALL other
            # field_filters but NOT by its own — so the user can see other
            # candidate values to widen / pick from. This is standard facet UX
            # ("filter by Pharmaceutical, still see all sectors in the sector
            # facet so I can switch").
            field_base = select(Document.id).where(
                visible_clause(Document, caller, read_all=read_all)
            )
            field_base = apply_sgr_filter(field_base, f, skip_field=prop.name)
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
