"""Citation-target resolver.

Promotes parked unresolved_relationship rows into resolved relationships
(high confidence) or relationship_proposal rows (medium confidence, for human
review), by matching each target_key against in-corpus identifiers (document
properties reached through a bridge relationship) and entity canonical_forms
(trigram).

The engine is domain-agnostic: it classifies each target_key with an ordered
rule list, canonicalizes both sides with named string transforms, and matches
either exactly (identifier rules) or by trigram with a top-2 margin guard
(fuzzy paths). Which relationships it sweeps, which identifier shapes exist,
which entity types the fuzzy paths search, and the kind-hint vocabularies are
all configuration; CONCURRENCES_CONFIG below carries the deployment defaults,
the same way CLASS_RULES does in ingestion_oneshot.

Read-only by default: prints the projected resolve / propose / park split and
exits without writing. Pass --execute to write. Even with --execute, only
high-confidence auto-resolves become relationships; medium matches are written
as pending proposals and are NOT auto-approved.

Matching rules (kind is a weak hint; the target_key value-shape decides):
  - identifier (ecli / celex / case_number in the default config): exact
    canonical value -> property_value -> document -> bridge relationship ->
    target entity. Auto only when exactly one entity is reached
    (ambiguous -> park).
  - fuzzy (name / legal_instrument in the default config): trigram against
    canonical_form of one entity type. Auto only when the path allows it
    (auto_resolve) and the top match clears --auto-threshold AND beats the
    runner-up by --margin (clear top-1); otherwise it drops to a proposal.
    The default config caps the legal_instrument path at propose: numbered
    instruments trigram-match on the shared prefix, not the number.

Run inside the grove container:
  docker compose exec grove python -m app.services.citation_resolver            # dry run
  docker compose exec grove python -m app.services.citation_resolver --execute  # write

Or over the API: POST /api/v1/ingestion/resolve-citations (dry run by default).
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_AUTO_THRESHOLD = 0.55
DEFAULT_REVIEW_THRESHOLD = 0.40
DEFAULT_MARGIN = 0.08

# ── configuration (pure stdlib; importable without app context) ──────────────


def _sql_quote(s: str) -> str:
    """Config value -> SQL string literal. Config is code-level, not user input;
    quoting keeps embedded quotes from breaking the statement."""
    return "'" + s.replace("'", "''") + "'"


def _sql_transform(expr: str, name: str) -> str:
    """Named, domain-free string transforms as SQL expressions. An identifier
    rule is pure data because canonicalization is limited to this vocabulary."""
    if name == "trim":
        return f"trim({expr})"
    if name == "uppercase":
        return f"upper({expr})"
    if name == "strip_spaces":
        return f"replace({expr},' ','')"
    if name.startswith("strip_prefix:"):
        prefix_re = name.split(":", 1)[1]
        return f"regexp_replace({expr},{_sql_quote(prefix_re)},'')"
    raise ValueError(f"unknown transform: {name}")


def _apply(expr: str, transforms: tuple[str, ...]) -> str:
    for t in transforms:
        expr = _sql_transform(expr, t)
    return expr


@dataclass(frozen=True)
class IdentifierRule:
    """One identifier family: (label, classifier patterns, transforms).

    `label` doubles as the document property name holding this identifier
    (matched against document_class_property.name across every class).
    `key_transforms` and `value_transforms` are separate because the parked
    key and the stored property value may need different canonicalization
    (e.g. stripping a prefix that stored values never carry).
    `malformed_pattern` is an optional looser shape reported as
    '<label>_malformed' so near-misses are visible in the park counts.
    """

    label: str
    patterns: tuple[str, ...]
    classify_transforms: tuple[str, ...]
    key_transforms: tuple[str, ...]
    value_transforms: tuple[str, ...]
    malformed_pattern: str | None = None


@dataclass(frozen=True)
class KindRule:
    """One fuzzy-classification arm, evaluated in order after the identifier
    rules: route a target_key to `path` when the raw key matches `key_regex`
    (case-insensitive), or its target_key_kind is in `kinds`, or matches
    `kind_regex`. Path 'non_resolvable' is a terminal park bucket."""

    path: str
    key_regex: str | None = None
    kinds: tuple[str, ...] = ()
    kind_regex: str | None = None


@dataclass(frozen=True)
class FuzzyPath:
    """A fuzzy path label, the entity type its candidates come from, and the
    method name written to notes/reasoning.

    auto_resolve=False caps the path at propose: even a match clearing the
    auto threshold and margin goes to human review. Use it where trigram
    similarity is known to confuse near-identical forms (numbered legal
    instruments, versioned titles)."""

    label: str
    entity_type: str
    method: str
    auto_resolve: bool = True


@dataclass(frozen=True)
class ResolverConfig:
    citation_reldefs: tuple[str, ...]
    reldef_path_overrides: dict[str, str] = field(default_factory=dict)
    bridge_reldefs: tuple[str, ...] = ()
    identifier_rules: tuple[IdentifierRule, ...] = ()
    kind_rules: tuple[KindRule, ...] = ()
    fuzzy_paths: tuple[FuzzyPath, ...] = ()
    identifier_confidence: float = 0.98
    proposing_agent: str = "citation-resolver"


# Deployment defaults for the Concurrences corpus. The identifier rules are
# EU-legal shapes (ECLI, CELEX, EC and EU-court case numbers). The kind-hint
# vocabularies in kind_rules and the case-name regex are coupled to what the
# Concurrences extraction agents emit as target_key_kind: they are deployment
# config, not engine behavior, and another corpus needs its own lists.
CONCURRENCES_CONFIG = ResolverConfig(
    citation_reldefs=("cites", "cites_legal_instrument"),
    reldef_path_overrides={"cites_legal_instrument": "legal_instrument"},
    bridge_reldefs=("is_full_text_of", "is_full_text_of_court"),
    identifier_rules=(
        IdentifierRule(
            label="ecli",
            patterns=(r"^ECLI:[A-Z]{2}:[A-Z]{1,2}:[0-9]{4}:[0-9]+$",),
            classify_transforms=("trim", "uppercase"),
            key_transforms=("trim", "uppercase"),
            value_transforms=("trim", "uppercase"),
            malformed_pattern=r"^ECLI:",
        ),
        IdentifierRule(
            label="celex",
            patterns=(r"^[0-9]{5}[A-Z][0-9A-Z]+$",),
            classify_transforms=("trim", "strip_spaces", "uppercase"),
            key_transforms=("trim", "strip_spaces", "uppercase"),
            value_transforms=("trim", "strip_spaces", "uppercase"),
        ),
        IdentifierRule(
            label="case_number",
            patterns=(
                r"^(COMP/)?M\.[0-9]+",
                r"^AT\.[0-9]+",
                r"^COMP/[0-9]",
                r"^[TC]-[0-9]+/[0-9]+",
            ),
            classify_transforms=("trim", "uppercase"),
            key_transforms=("trim", "strip_prefix:^COMP/", "uppercase"),
            value_transforms=("trim", "uppercase"),
        ),
    ),
    kind_rules=(
        KindRule(
            path="name",
            key_regex=r"( v\.? | versus |^in re|^re:)",
            kinds=("case_name", "case_citation", "merger_name"),
        ),
        KindRule(
            path="legal_instrument",
            kinds=(
                "legal_instrument", "regulation", "directive", "notice", "statute",
                "treaty", "law", "royal_decree", "guideline", "guidelines", "regulation_id",
                "directive_number", "regulation_number", "legal_provision", "national_law",
                "tfeu_article", "treaty_article", "convention", "communication", "recommendation",
                "charter", "commission_notice", "international_agreement", "international_convention",
                "spanish_law_id", "official_journal", "legal_article", "legal_instrument_name",
            ),
        ),
        KindRule(
            path="non_resolvable",
            kinds=(
                "work", "academic_work", "press_release", "eu_press_release", "game_title",
                "report", "publication", "working_paper", "us_reporter", "oecd_cartel", "antitrust_opinion",
                "scientific_authority", "scientific_committee", "expert_group", "expert_committee", "policy",
                "principle", "standard", "organization_name", "company", "company_name", "undertaking",
                "authority", "competition_authority", "court_name", "entity_name", "country", "ftc_report",
                "ftc_opinion", "legislative_report", "report_reference",
            ),
        ),
        KindRule(
            path="name",
            kind_regex=r"(case|decision|merger|competition_case|nca_case|administrative_proceeding)",
        ),
    ),
    fuzzy_paths=(
        FuzzyPath(label="name", entity_type="Competition Decision / Case", method="fuzzy_name"),
        # auto_resolve=False: instrument names differ by number alone
        # (Regulation 1218/2010 vs 1400/2002, Article 101(3) vs 102
        # Guidelines) and trigram scores the shared prefix, not the number.
        # Both false autos observed live were on this path; matches here
        # always go to review.
        FuzzyPath(label="legal_instrument", entity_type="Legal Instrument",
                  method="fuzzy_instrument", auto_resolve=False),
    ),
)


def _in_list(values: tuple[str, ...]) -> str:
    # (NULL) is valid SQL that matches nothing, so an empty optional set
    # degrades to a never-true clause instead of an IN () syntax error.
    if not values:
        return "(NULL)"
    return "(" + ",".join(_sql_quote(v) for v in values) + ")"


def build_match_sql(cfg: ResolverConfig) -> str:
    """Compose the set-based match query from the config. Single query:
    classify each parked key, canonicalize, match (exact identifier via the
    property/bridge hop, fuzzy via trigram top-2), decide auto/propose/park,
    and flag rows whose relationship or pending proposal already exists."""
    if not cfg.citation_reldefs:
        raise ValueError("ResolverConfig.citation_reldefs is empty: nothing to sweep")
    if not cfg.identifier_rules and not cfg.fuzzy_paths:
        raise ValueError(
            "ResolverConfig needs at least one identifier rule or fuzzy path"
        )
    if cfg.identifier_rules and not cfg.bridge_reldefs:
        raise ValueError(
            "ResolverConfig.bridge_reldefs is empty: the identifier path "
            "resolves through a bridge relationship"
        )

    # classification arms, in order: reldef overrides, identifier rules
    # (each optionally followed by its malformed arm), kind rules.
    arms: list[str] = []
    for reldef, path in cfg.reldef_path_overrides.items():
        arms.append(
            f"            WHEN p.reldef_name = {_sql_quote(reldef)} THEN {_sql_quote(path)}"
        )
    for rule in cfg.identifier_rules:
        expr = _apply("p.tk", rule.classify_transforms)
        tests = [f"{expr} ~ {_sql_quote(pat)}" for pat in rule.patterns]
        arms.append(
            "            WHEN " + "\n              OR ".join(tests)
            + f" THEN {_sql_quote(rule.label)}"
        )
        if rule.malformed_pattern:
            arms.append(
                f"            WHEN {expr} ~ {_sql_quote(rule.malformed_pattern)}"
                f" THEN {_sql_quote(rule.label + '_malformed')}"
            )
    for kr in cfg.kind_rules:
        tests = []
        if kr.key_regex:
            tests.append(f"p.tk ~* {_sql_quote(kr.key_regex)}")
        if kr.kinds:
            tests.append(f"p.kind IN {_in_list(kr.kinds)}")
        if kr.kind_regex:
            tests.append(f"p.kind ~ {_sql_quote(kr.kind_regex)}")
        arms.append(
            "            WHEN " + "\n              OR ".join(tests)
            + f" THEN {_sql_quote(kr.path)}"
        )
    if arms:
        path_expr = (
            "CASE\n" + "\n".join(arms) + "\n            ELSE 'unclassified'\n        END AS path"
        )
    else:
        # no overrides, no identifier rules, no kind rules: nothing routes,
        # every row parks as unclassified
        path_expr = "'unclassified' AS path"

    id_labels = _in_list(tuple(r.label for r in cfg.identifier_rules))
    fuzzy_labels = _in_list(tuple(f.label for f in cfg.fuzzy_paths))

    if cfg.identifier_rules:
        key_norm_arms = "\n".join(
            f"            WHEN {_sql_quote(r.label)} THEN {_apply('c.tk', r.key_transforms)}"
            for r in cfg.identifier_rules
        )
        id_norm_expr = (
            "CASE c.path\n" + key_norm_arms + "\n            ELSE NULL\n        END AS id_norm"
        )
        value_expr = "pv.value->>'_'"
        value_norm_arms = "\n                  ".join(
            f"WHEN {_sql_quote(r.label)} THEN {_apply(value_expr, r.value_transforms)}"
            for r in cfg.identifier_rules
        )
        value_match_clause = (
            "WHERE (CASE n.path\n                  " + value_norm_arms
            + "\n               END) = n.id_norm"
        )
    else:
        id_norm_expr = "NULL::text AS id_norm"
        value_match_clause = "WHERE FALSE"

    if cfg.fuzzy_paths:
        fuzzy_type_arms = "\n".join(
            f"        CASE WHEN c.path = {_sql_quote(f.label)} THEN {_sql_quote(f.entity_type)}"
            if i == 0
            else f"             WHEN c.path = {_sql_quote(f.label)} THEN {_sql_quote(f.entity_type)}"
            for i, f in enumerate(cfg.fuzzy_paths)
        )
        fuzzy_type_expr = fuzzy_type_arms + "\n             ELSE NULL END AS fuzzy_type"
    else:
        fuzzy_type_expr = "        NULL::text AS fuzzy_type"
    method_arms = "\n".join(
        f"             WHEN d.path = {_sql_quote(f.label)} THEN {_sql_quote(f.method)}"
        for f in cfg.fuzzy_paths
    )
    auto_fuzzy_labels = _in_list(
        tuple(f.label for f in cfg.fuzzy_paths if f.auto_resolve)
    )

    return rf"""
WITH parked AS (
    SELECT ur.id AS ur_id, ur.relationship_definition_id AS reldef_id, rd.name AS reldef_name,
           ur.source_id, ur.evidence_document_id, ur.evidence_span,
           ur.target_key AS tk, COALESCE(ur.target_key_kind,'') AS kind
    FROM unresolved_relationship ur
    JOIN relationship_definition rd ON rd.id = ur.relationship_definition_id
    WHERE ur.status = 'unresolved'
      AND rd.name IN {_in_list(cfg.citation_reldefs)}
),
classified AS (
    SELECT p.*,
        {path_expr}
    FROM parked p
),
norm AS (
    SELECT c.*,
        {id_norm_expr},
{fuzzy_type_expr},
        lower(regexp_replace(trim(c.tk),'\s+',' ','g')) AS fuzzy_key
    FROM classified c
),
matched AS (
    SELECT n.*, idm.ents AS id_ents, f.eids AS f_eids, f.sims AS f_sims
    FROM norm n
    LEFT JOIN LATERAL (
        SELECT array_agg(DISTINCT r.target_id) AS ents
        FROM property_value pv
        JOIN document_class_property dp ON dp.id = pv.property_id AND dp.name = n.path
        JOIN relationship r ON r.source_id = pv.document_id
        JOIN relationship_definition rd2 ON rd2.id = r.relationship_definition_id
             AND rd2.name IN {_in_list(cfg.bridge_reldefs)}
        {value_match_clause}
    ) idm ON n.path IN {id_labels}
    LEFT JOIN LATERAL (
        SELECT array_agg(x.eid ORDER BY x.sim DESC) AS eids,
               array_agg(x.sim ORDER BY x.sim DESC) AS sims
        FROM (
            SELECT e.id AS eid, similarity(n.fuzzy_key, lower(e.canonical_form)) AS sim
            FROM entity e JOIN entity_type et ON et.id = e.entity_type_id
            WHERE et.name = n.fuzzy_type
            ORDER BY sim DESC
            LIMIT 2
        ) x
    ) f ON n.fuzzy_type IS NOT NULL
),
decided AS (
    SELECT m.*,
        COALESCE(array_length(m.id_ents,1),0) AS id_count,
        COALESCE((m.f_sims)[1],0) AS top1,
        COALESCE((m.f_sims)[2],0) AS top2,
        CASE
            WHEN m.path IN {id_labels} AND COALESCE(array_length(m.id_ents,1),0) = 1
                THEN (m.id_ents)[1]
            WHEN m.path IN {fuzzy_labels} AND COALESCE((m.f_sims)[1],0) >= :review
                THEN (m.f_eids)[1]
            ELSE NULL
        END AS target_entity_id
    FROM matched m
),
final AS (
    SELECT d.*,
        -- A decision other than 'park' requires a concrete target entity.
        -- Identifier matches with >1 candidate (ambiguous) have a NULL target and
        -- therefore park rather than emit a proposal with no target_id.
        CASE
            WHEN d.target_entity_id IS NULL THEN 'park'
            WHEN d.path IN {id_labels} THEN 'auto'
            WHEN d.path IN {auto_fuzzy_labels}
                 AND d.top1 >= :auto AND (d.top1 - d.top2) >= :margin THEN 'auto'
            ELSE 'propose'
        END AS decision,
        CASE WHEN d.path IN {id_labels} THEN {cfg.identifier_confidence}
             WHEN d.path IN {fuzzy_labels} THEN d.top1 ELSE NULL END AS match_conf,
        CASE WHEN d.path IN {id_labels} THEN 'identifier'
{method_arms} ELSE NULL END AS method
    FROM decided d
)
SELECT f.ur_id, f.reldef_id, f.reldef_name, f.source_id, f.evidence_document_id, f.evidence_span,
       f.path, f.target_entity_id, f.method, f.match_conf, f.decision, f.top1, f.top2, f.id_count,
       (f.target_entity_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM relationship r3
            WHERE r3.relationship_definition_id = f.reldef_id
              AND r3.source_id = f.source_id AND r3.target_id = f.target_entity_id)) AS rel_exists,
       (f.target_entity_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM relationship_proposal rp
            WHERE rp.relationship_definition_id = f.reldef_id
              AND rp.source_id = f.source_id AND rp.target_id = f.target_entity_id
              AND rp.status IN ('pending','rejected'))) AS prop_exists
FROM final f
"""


# ── runtime (needs the app context) ──────────────────────────────────────────

from sqlalchemy import text  # noqa: E402

from app.db import AsyncSessionLocal  # noqa: E402
from app.models import Relationship, RelationshipProposal, UnresolvedRelationship  # noqa: E402


async def resolve(execute: bool, auto: float, review: float, margin: float,
                  write_proposals: bool,
                  config: ResolverConfig = CONCURRENCES_CONFIG) -> dict:
    match_sql = text(build_match_sql(config))
    async with AsyncSessionLocal() as session:
        if execute:
            # Serialize concurrent executes: the idempotency flags are read
            # by the match query, so two simultaneous runs would both pass
            # them and double-write. Held until commit/rollback.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('grove-citation-resolver'))")
            )
        rows = (
            await session.execute(match_sql, {"auto": auto, "review": review, "margin": margin})
        ).mappings().all()

        by_decision: dict[str, int] = {"auto": 0, "propose": 0, "park": 0}
        by_path: dict[str, dict[str, int]] = {}
        auto_written = prop_written = skipped_dupe = 0
        now = datetime.now(timezone.utc)
        # Guard against duplicates created within this run: rel_exists / prop_exists
        # are evaluated by the query before any insert, so two parked rows pointing
        # at the same (definition, source, target) would otherwise both write.
        seen_rel: set = set()
        seen_prop: set = set()

        for r in rows:
            dec = r["decision"]
            by_decision[dec] = by_decision.get(dec, 0) + 1
            slot = by_path.setdefault(r["path"], {"auto": 0, "propose": 0, "park": 0})
            slot[dec] += 1
            key = (r["reldef_id"], r["source_id"], r["target_entity_id"])

            if dec == "auto":
                if r["rel_exists"] or key in seen_rel:
                    skipped_dupe += 1
                    continue
                seen_rel.add(key)
                if execute:
                    rel = Relationship(
                        relationship_definition_id=r["reldef_id"],
                        source_id=r["source_id"],
                        target_id=r["target_entity_id"],
                        evidence_document_id=r["evidence_document_id"],
                        evidence_span=r["evidence_span"],
                        confidence=r["match_conf"],
                        notes=f"{config.proposing_agent}:{r['method']} top1={r['top1']:.2f}",
                    )
                    session.add(rel)
                    await session.flush()
                    ur = await session.get(UnresolvedRelationship, r["ur_id"])
                    ur.status = "resolved"
                    ur.resolved_relationship_id = rel.id
                    ur.resolved_at = now
                    auto_written += 1

            elif dec == "propose" and write_proposals:
                # rel_exists too: a relationship already satisfying this
                # citation needs no review, and approving a redundant
                # proposal would duplicate it.
                if r["prop_exists"] or r["rel_exists"] or key in seen_prop:
                    skipped_dupe += 1
                    continue
                seen_prop.add(key)
                if execute:
                    session.add(RelationshipProposal(
                        relationship_definition_id=r["reldef_id"],
                        source_id=r["source_id"],
                        target_id=r["target_entity_id"],
                        proposing_agent=config.proposing_agent,
                        reasoning=f"{r['method']} top1={r['top1']:.2f} top2={r['top2']:.2f}",
                        evidence_document_id=r["evidence_document_id"],
                        evidence_span=r["evidence_span"],
                        confidence=r["match_conf"],
                        status="pending",
                    ))
                    prop_written += 1

        if execute:
            await session.commit()
        else:
            await session.rollback()

        return {
            "total": len(rows),
            "by_decision": by_decision,
            "by_path": by_path,
            "auto_written": auto_written,
            "prop_written": prop_written,
            "skipped_dupe": skipped_dupe,
            "executed": execute,
        }


def _print(report: dict, auto: float, review: float, margin: float) -> None:
    mode = "EXECUTE (writing)" if report["executed"] else "DRY RUN (read-only)"
    print(f"\ncitation-resolver — {mode}")
    print(f"thresholds: auto>={auto}  review>={review}  margin>={margin}\n")
    d = report["by_decision"]
    print(f"  citation edges : {report['total']}")
    print(f"  -> auto        : {d.get('auto', 0)}")
    print(f"  -> propose     : {d.get('propose', 0)}")
    print(f"  -> park        : {d.get('park', 0)}\n")
    print(f"  {'path':<18}{'auto':>7}{'propose':>9}{'park':>7}")
    for path, s in sorted(report["by_path"].items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {path:<18}{s['auto']:>7}{s['propose']:>9}{s['park']:>7}")
    if report["executed"]:
        print(f"\n  relationships written : {report['auto_written']}")
        print(f"  proposals written     : {report['prop_written']}")
        print(f"  skipped (already set) : {report['skipped_dupe']}")
    else:
        print("\n  (no writes — pass --execute to promote autos and hold proposals)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Citation-target resolver.")
    ap.add_argument("--execute", action="store_true",
                    help="write changes (default: dry run, read-only)")
    ap.add_argument("--auto-threshold", type=float, default=DEFAULT_AUTO_THRESHOLD)
    ap.add_argument("--review-threshold", type=float, default=DEFAULT_REVIEW_THRESHOLD)
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                    help="min top1 - top2 gap for a fuzzy auto-resolve")
    ap.add_argument("--no-proposals", action="store_true",
                    help="with --execute, promote autos only and do not write proposals")
    args = ap.parse_args()

    report = asyncio.run(resolve(
        execute=args.execute,
        auto=args.auto_threshold,
        review=args.review_threshold,
        margin=args.margin,
        write_proposals=not args.no_proposals,
    ))
    _print(report, args.auto_threshold, args.review_threshold, args.margin)


if __name__ == "__main__":
    main()
