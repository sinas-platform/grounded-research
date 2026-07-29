
WITH parked AS (
    SELECT ur.id AS ur_id, ur.relationship_definition_id AS reldef_id, rd.name AS reldef_name,
           ur.source_id, ur.evidence_document_id, ur.evidence_span,
           ur.target_key AS tk, COALESCE(ur.target_key_kind,'') AS kind
    FROM unresolved_relationship ur
    JOIN relationship_definition rd ON rd.id = ur.relationship_definition_id
    WHERE ur.status = 'unresolved'
      AND rd.name IN ('t877_refs','t877_cli')
),
classified AS (
    SELECT p.*,
        CASE
            WHEN p.reldef_name = 't877_cli' THEN 'legal_instrument'
            WHEN upper(trim(p.tk)) ~ '^T877-[0-9]+$' THEN 't877_id'
            WHEN p.kind IN ('t877_name') THEN 'name'
            ELSE 'unclassified'
        END AS path
    FROM parked p
),
norm AS (
    SELECT c.*,
        CASE c.path
            WHEN 't877_id' THEN upper(trim(c.tk))
            ELSE NULL
        END AS id_norm,
        CASE WHEN c.path = 'name' THEN 'T877 Type'
             WHEN c.path = 'legal_instrument' THEN 'T877 Instrument'
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
             AND rd2.name IN ('t877_full_text_of')
        WHERE (CASE n.path
                  WHEN 't877_id' THEN upper(trim(pv.value->>'_'))
               END) = n.id_norm
    ) idm ON n.path IN ('t877_id')
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
            WHEN m.path IN ('t877_id') AND COALESCE(array_length(m.id_ents,1),0) = 1
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
            WHEN d.path IN ('t877_id') THEN 'auto'
            WHEN d.path IN ('name')
                 AND d.top1 >= :auto AND (d.top1 - d.top2) >= :margin THEN 'auto'
            ELSE 'propose'
        END AS decision,
        CASE WHEN d.path IN ('t877_id') THEN 0.98
             WHEN d.path IN ('name','legal_instrument') THEN d.top1 ELSE NULL END AS match_conf,
        CASE WHEN d.path IN ('t877_id') THEN 'identifier'
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
