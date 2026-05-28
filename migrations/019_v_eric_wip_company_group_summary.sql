-- 019_v_eric_wip_company_group_summary.sql
--
-- WO-14A.1 — One row per APPROVED group with the rolled-up current WIP
-- across all APPROVED member wip_companies. ERic's group_default
-- firm_summary mode reads from this view.
--
-- Only APPROVED groups + members are visible. PROPOSED / NEEDS_REVIEW /
-- REJECTED rows are deliberately filtered out so a downstream consumer
-- never accidentally surfaces an unreviewed roll-up.
--
-- Sits alongside the existing v_eric_wip_company_summary etc. — does NOT
-- replace them. The ERic cutover to group_default mode is a separate WO
-- (WO-14A.4 per the WO-14B blueprint §6 phasing).
--
-- The GRANT for wip_read is applied out-of-band as root (the migration
-- user does not hold GRANT OPTION):
--   GRANT SELECT ON nif_wip.v_eric_wip_company_group_summary TO 'wip_read'@'%';

CREATE OR REPLACE VIEW v_eric_wip_company_group_summary AS
SELECT
    g.id                                AS group_id,
    g.group_name,
    g.group_name_normalised,
    g.group_type,
    g.primary_salesforce_account_id,
    g.primary_canonical_company_id,
    COUNT(DISTINCT m.id)                AS member_count,
    COUNT(DISTINCT m.wip_company_id)    AS member_wip_company_count,
    COUNT(DISTINCT m.canonical_company_id) AS member_canonical_count,
    SUM(CASE WHEN m.member_role = 'PRIMARY' THEN 1 ELSE 0 END) AS primary_member_count,
    COALESCE(SUM(curr.current_wip_value), 0)    AS group_current_wip_value,
    COALESCE(SUM(curr.current_wip_count), 0)    AS group_active_case_count,
    MAX(curr.latest_wip_date)                    AS group_latest_wip_date,
    g.reviewed_by,
    g.reviewed_at,
    g.updated_at
FROM wip_company_group g
JOIN wip_company_group_member m
    ON m.group_id = g.id
   AND m.review_status = 'APPROVED'
LEFT JOIN v_eric_wip_company_summary curr
    ON curr.wip_company_id = m.wip_company_id
WHERE g.review_status = 'APPROVED'
GROUP BY
    g.id, g.group_name, g.group_name_normalised, g.group_type,
    g.primary_salesforce_account_id, g.primary_canonical_company_id,
    g.reviewed_by, g.reviewed_at, g.updated_at;
