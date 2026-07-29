
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
            WHEN p.kind IN ('legal_instrument','regulation','directive','notice','statute','treaty','law','royal_decree','guideline','guidelines','regulation_id','directive_number','regulation_number','legal_provision','national_law','tfeu_article','treaty_article','convention','communication','recommendation','charter','commission_notice','international_agreement','international_convention','spanish_law_id','official_journal','legal_article','legal_instrument_name') THEN 'legal_instrument'
            WHEN p.kind IN ('work','academic_work','press_release','eu_press_release','game_title','report','publication','working_paper','us_reporter','oecd_cartel','antitrust_opinion','scientific_authority','scientific_committee','expert_group','expert_committee','policy','principle','standard','organization_name','company','company_name','undertaking','authority','competition_authority','court_name','entity_name','country','ftc_report','ftc_opinion','legislative_report','report_reference') THEN 'non_resolvable'
            WHEN p.kind ~ '(case|decision|merger|competition_case|nca_case|administrative_proceeding)' THEN 'name'
            ELSE 'unclassified'
        END AS path
    FROM parked p
),
norm AS (
    SELECT c.*,
        CASE c.path
            WHEN 'ecli' THEN upper(trim(c.tk))
            WHEN 'celex' THEN upper(replace(trim(c.tk),' ',''))
            WHEN 'case_number' THEN upper(regexp_replace(trim(c.tk),'^COMP/',''))
            ELSE NULL
        END AS id_norm,
        CASE WHEN c.path = 'name' THEN 'Competition Decision / Case'
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
                  WHEN 'ecli' THEN upper(trim(pv.value->>'_'))
                  WHEN 'celex' THEN upper(replace(trim(pv.value->>'_'),' ',''))
                  WHEN 'case_number' THEN upper(trim(pv.value->>'_'))
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
        -- A decision other than 'park' requires a concrete target entity.
        -- Identifier matches with >1 candidate (ambiguous) have a NULL target and
        -- therefore park rather than emit a proposal with no target_id.
        CASE
            WHEN d.target_entity_id IS NULL THEN 'park'
            WHEN d.path IN ('ecli','celex','case_number') THEN 'auto'
            WHEN d.path IN ('name')
                 AND d.top1 >= :auto AND (d.top1 - d.top2) >= :margin THEN 'auto'
            ELSE 'propose'
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
              AND rp.status IN ('pending','rejected'))) AS prop_exists
FROM final f
