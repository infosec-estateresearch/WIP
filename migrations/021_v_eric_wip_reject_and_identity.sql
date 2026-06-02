-- WO-15: shared ERic-facing views for reject-awareness and duplicate/group
-- identity. ERic reads nif_wip ONLY through v_eric_* views (wip_read user),
-- never the base tables. These expose the reject queue and the not-yet-approved
-- group/duplicate state that the existing approved-only views hide.
--
-- Rule enforced downstream: rejected rows are potential value AWAITING REVIEW,
-- never counted as imported WIP.

-- ---------------------------------------------------------------------------
-- 1) Reject queue aggregated per normalised firm name (unresolved only).
--    Powers ERic firm-specific reject_context. estimated_yield from the raw
--    Mastercopy row is summed as "possible value" (numeric values only).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE ALGORITHM=UNDEFINED DEFINER=`wip_app`@`%` SQL SECURITY DEFINER
VIEW v_eric_wip_reject_firm_summary AS
SELECT
    r.firm_name_normalised,
    MIN(r.firm_name_raw)                                   AS firm_name_raw_sample,
    COUNT(*)                                               AS reject_count,
    SUM(
      CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(r.row_json,'$.estimated_yield')) REGEXP '^-?[0-9]+(\\.[0-9]+)?$'
           THEN CAST(JSON_UNQUOTE(JSON_EXTRACT(r.row_json,'$.estimated_yield')) AS DECIMAL(15,2))
           ELSE 0 END
    )                                                      AS possible_yield_value,
    GROUP_CONCAT(DISTINCT r.reject_reason ORDER BY r.reject_reason SEPARATOR ',') AS reject_reasons,
    MAX(r.import_batch_id)                                 AS latest_import_batch_id,
    MAX(r.created_at)                                      AS latest_reject_at
FROM wip_import_reject r
WHERE r.resolution_status IN ('NEW','NEEDS_REVIEW')
  AND r.firm_name_normalised IS NOT NULL
  AND r.firm_name_normalised <> ''
GROUP BY r.firm_name_normalised;

-- ---------------------------------------------------------------------------
-- 2) Group summary across ALL review states (approved + proposed + needs-review).
--    Mirrors v_eric_wip_company_group_summary but does NOT restrict to APPROVED,
--    so ERic can recognise proposed/needs-review groups. WIP value is a
--    provisional rollup of member WIP.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE ALGORITHM=UNDEFINED DEFINER=`wip_app`@`%` SQL SECURITY DEFINER
VIEW v_eric_wip_company_group_candidate_summary AS
SELECT
    g.id                                       AS group_id,
    g.group_name,
    g.group_name_normalised,
    g.group_type,
    g.review_status                            AS group_review_status,
    g.primary_salesforce_account_id,
    g.primary_canonical_company_id,
    COUNT(DISTINCT m.id)                       AS member_count,
    COUNT(DISTINCT m.wip_company_id)           AS member_wip_company_count,
    COUNT(DISTINCT m.canonical_company_id)     AS member_canonical_count,
    COALESCE(SUM(curr.current_wip_value),0)    AS group_current_wip_value,
    COALESCE(SUM(curr.current_wip_count),0)    AS group_active_case_count,
    MAX(curr.latest_wip_date)                  AS group_latest_wip_date
FROM wip_company_group g
JOIN wip_company_group_member m ON m.group_id = g.id
LEFT JOIN v_eric_wip_company_summary curr ON curr.wip_company_id = m.wip_company_id
WHERE g.review_status IN ('APPROVED','PROPOSED','NEEDS_REVIEW')
GROUP BY g.id, g.group_name, g.group_name_normalised, g.group_type, g.review_status,
         g.primary_salesforce_account_id, g.primary_canonical_company_id;

-- ---------------------------------------------------------------------------
-- 3) Group membership across ALL review states. Lets ERic map a canonical_id to
--    its group. NB: the group's primary_canonical_company_id may differ from any
--    member canonical (the Hugh James case), so callers should also match on the
--    summary's primary_canonical_company_id / group_name_normalised.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE ALGORITHM=UNDEFINED DEFINER=`wip_app`@`%` SQL SECURITY DEFINER
VIEW v_eric_wip_company_group_candidate_member AS
SELECT
    g.id                AS group_id,
    g.group_name,
    g.group_name_normalised,
    g.group_type,
    g.review_status     AS group_review_status,
    m.wip_company_id,
    m.salesforce_account_id,
    m.canonical_company_id,
    m.member_role,
    curr.company_name,
    COALESCE(curr.current_wip_value, 0) AS current_wip_value,
    COALESCE(curr.current_wip_count, 0) AS current_wip_count
FROM wip_company_group g
JOIN wip_company_group_member m ON m.group_id = g.id
LEFT JOIN v_eric_wip_company_summary curr ON curr.wip_company_id = m.wip_company_id
WHERE g.review_status IN ('APPROVED','PROPOSED','NEEDS_REVIEW');

-- ---------------------------------------------------------------------------
-- 4) Open duplicate candidates (multiple SF accounts that may be one firm).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE ALGORITHM=UNDEFINED DEFINER=`wip_app`@`%` SQL SECURITY DEFINER
VIEW v_eric_wip_duplicate_candidate AS
SELECT
    id                          AS duplicate_candidate_id,
    duplicate_group_key,
    candidate_count,
    status,
    salesforce_account_ids_json,
    account_names_json,
    first_detected_at,
    last_seen_at
FROM wip_company_duplicate_candidate
WHERE status IN ('NEW','NEEDS_REVIEW');

-- ---------------------------------------------------------------------------
-- Grants: ERic's read-only user
-- ---------------------------------------------------------------------------
GRANT SELECT ON nif_wip.v_eric_wip_reject_firm_summary            TO 'wip_read'@'%';
GRANT SELECT ON nif_wip.v_eric_wip_company_group_candidate_summary TO 'wip_read'@'%';
GRANT SELECT ON nif_wip.v_eric_wip_company_group_candidate_member  TO 'wip_read'@'%';
GRANT SELECT ON nif_wip.v_eric_wip_duplicate_candidate            TO 'wip_read'@'%';
