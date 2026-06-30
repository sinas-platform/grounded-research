"""Citation-target resolver (CNAI citation-resolution Phase 3).

Promotes parked `cites` / `cites_legal_instrument` unresolved_relationship rows
into resolved relationships (high confidence) or relationship_proposal rows
(medium confidence, for human review), by matching each target_key against the
in-corpus identifiers (case_number / celex / ecli via is_full_text_of) and the
extracted entity canonical_forms (trigram).

Read-only by default: prints the projected resolve / propose / park split and
exits without writing. Pass --execute to write. Even with --execute, only
high-confidence auto-resolves become relationships; medium matches are written
as pending proposals and are NOT auto-approved.

Matching rules (kind is a weak hint; the target_key value-shape decides):
  - identifier (ecli / celex / case_number): exact value -> property_value ->
    document -> is_full_text_of -> decision entity. Auto only when exactly one
    entity is reached (ambiguous -> proposal).
  - fuzzy (name -> Competition Decision / Case, legal_instrument -> Legal
    Instrument): trigram against canonical_form. Auto only when the top match
    clears --auto-threshold AND beats the runner-up by --margin (clear top-1);
    otherwise it drops to a proposal.

Run inside the grove container:
  docker compose exec grove python -m app.services.citation_resolver            # dry run
  docker compose exec grove python -m app.services.citation_resolver --execute  # write
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.models import Relationship, RelationshipProposal, UnresolvedRelationship

PROPOSING_AGENT = "citation-resolver"

# Single set-based match query. Classification + normalization mirror
# citation_resolver_dryrun.sql; this adds the chosen target entity (argmax),
# the top-2 margin for the fuzzy paths, the identifier->entity resolution, and
# idempotency flags (does a relationship / pending proposal already exist).
MATCH_SQL = text(
    r"""
WITH parked AS (
    SELECT ur.id AS ur_id, ur.relationship_definition_id AS reldef_id, rd.name AS reldef_name,
           ur.source_id, ur.evidence_document_id, ur.evidence_span,
           ur.target_key AS tk, COALESCE(ur.target_key_kind,'') AS kind
    FROM unresolved_relationship ur
    JOIN relationship_definition rd ON rd.id = ur.relationship_definition_id
    WHERE ur.status = 'unresolved'
      AND rd.name IN ('cites','cites_legal_instrument')
),
classified AS (
    SELECT p.*,
        CASE
            WHEN p.reldef_name = 'cites_legal_instrument' THEN 'legal_instrument'
            WHEN upper(trim(p.tk)) ~ '^ECLI:[A-Z]{2}:[A-Z]{1,2}:[0-9]{4}:[0-9]+$' THEN 'ecli'
            WHEN upper(trim(p.tk)) ~ '^ECLI:' THEN 'ecli_malformed'
            WHEN upper(replace(trim(p.tk),' ','')) ~ '^[0-9]{5}[A-Z][0-9A-Z]+$' THEN 'celex'
            WHEN upper(trim(p.tk)) ~ '^(COMP/)?M\.[0-9]+'
              OR upper(trim(p.tk)) ~ '^AT\.[0-9]+'
              OR upper(trim(p.tk)) ~ '^COMP/[0-9]'
              OR upper(trim(p.tk)) ~ '^[TC]-[0-9]+/[0-9]+' THEN 'case_number'
            WHEN p.tk ~* '( v\.? | versus |^in re|^re:)'
              OR p.kind IN ('case_name','case_citation','merger_name') THEN 'name'
            WHEN p.kind IN ('legal_instrument','regulation','directive','notice','statute',
                 'treaty','law','royal_decree','guideline','guidelines','regulation_id',
                 'directive_number','regulation_number','legal_provision','national_law',
                 'tfeu_article','treaty_article','convention','communication','recommendation',
                 'charter','commission_notice','international_agreement','international_convention',
                 'spanish_law_id','official_journal','legal_article','legal_instrument_name') THEN 'legal_instrument'
            WHEN p.kind IN ('work','academic_work','press_release','eu_press_release','game_title',
                 'report','publication','working_paper','us_reporter','oecd_cartel','antitrust_opinion',
                 'scientific_authority','scientific_committee','expert_group','expert_committee','policy',
                 'principle','standard','organization_name','company','company_name','undertaking',
                 'authority','competition_authority','court_name','entity_name','country','ftc_report',
                 'ftc_opinion','legislative_report','report_reference') THEN 'non_resolvable'
            WHEN p.kind ~ '(case|decision|merger|competition_case|nca_case|administrative_proceeding)' THEN 'name'
            ELSE 'unclassified'
        END AS path
    FROM parked p
),
norm AS (
    SELECT c.*,
        CASE c.path
            WHEN 'celex'       THEN upper(replace(trim(c.tk),' ',''))
            WHEN 'case_number' THEN upper(regexp_replace(trim(c.tk),'^COMP/',''))
            WHEN 'ecli'        THEN upper(trim(c.tk))
            ELSE NULL
        END AS id_norm,
        CASE WHEN c.path = 'name'             THEN 'Competition Decision / Case'
             WHEN c.path = 'legal_instrument' THEN 'Legal Instrument'
             ELSE NULL END AS fuzzy_type,
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
             AND rd2.name IN ('is_full_text_of','is_full_text_of_court')
        WHERE (CASE n.path
                  WHEN 'celex' THEN upper(replace(trim(pv.value->>'_'),' ',''))
                  ELSE upper(trim(pv.value->>'_'))
               END) = n.id_norm
    ) idm ON n.path IN ('ecli','celex','case_number')
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
            WHEN m.path IN ('ecli','celex','case_number') AND COALESCE(array_length(m.id_ents,1),0) = 1
                THEN (m.id_ents)[1]
            WHEN m.path IN ('name','legal_instrument') AND COALESCE((m.f_sims)[1],0) >= :review
                THEN (m.f_eids)[1]
            ELSE NULL
        END AS target_entity_id
    FROM matched m
),
final AS (
    SELECT d.*,
        CASE
            WHEN d.path IN ('ecli','celex','case_number') AND d.id_count = 1 THEN 'auto'
            WHEN d.path IN ('ecli','celex','case_number') AND d.id_count > 1 THEN 'propose'
            WHEN d.path IN ('name','legal_instrument') AND d.top1 >= :auto AND (d.top1 - d.top2) >= :margin THEN 'auto'
            WHEN d.path IN ('name','legal_instrument') AND d.top1 >= :auto AND (d.top1 - d.top2) <  :margin THEN 'propose'
            WHEN d.path IN ('name','legal_instrument') AND d.top1 >= :review THEN 'propose'
            ELSE 'park'
        END AS decision,
        CASE WHEN d.path IN ('ecli','celex','case_number') THEN 0.98
             WHEN d.path IN ('name','legal_instrument') THEN d.top1 ELSE NULL END AS match_conf,
        CASE WHEN d.path IN ('ecli','celex','case_number') THEN 'identifier'
             WHEN d.path = 'name' THEN 'fuzzy_name'
             WHEN d.path = 'legal_instrument' THEN 'fuzzy_instrument' ELSE NULL END AS method
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
              AND rp.status = 'pending')) AS prop_exists
FROM final f
"""
)


async def resolve(execute: bool, auto: float, review: float, margin: float,
                  write_proposals: bool) -> dict:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(MATCH_SQL, {"auto": auto, "review": review, "margin": margin})
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
                        notes=f"{PROPOSING_AGENT}:{r['method']} top1={r['top1']:.2f}",
                    )
                    session.add(rel)
                    await session.flush()
                    ur = await session.get(UnresolvedRelationship, r["ur_id"])
                    ur.status = "resolved"
                    ur.resolved_relationship_id = rel.id
                    ur.resolved_at = now
                    auto_written += 1

            elif dec == "propose" and write_proposals:
                if r["prop_exists"] or key in seen_prop:
                    skipped_dupe += 1
                    continue
                seen_prop.add(key)
                if execute:
                    session.add(RelationshipProposal(
                        relationship_definition_id=r["reldef_id"],
                        source_id=r["source_id"],
                        target_id=r["target_entity_id"],
                        proposing_agent=PROPOSING_AGENT,
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
    ap = argparse.ArgumentParser(description="Citation-target resolver (Phase 3).")
    ap.add_argument("--execute", action="store_true",
                    help="write changes (default: dry run, read-only)")
    ap.add_argument("--auto-threshold", type=float, default=0.55)
    ap.add_argument("--review-threshold", type=float, default=0.40)
    ap.add_argument("--margin", type=float, default=0.08,
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
