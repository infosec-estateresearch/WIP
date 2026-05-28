-- 020_v_eric_wip_company_group_members.sql
--
-- WO-14A.1 — One row per APPROVED member of an APPROVED group, joined
-- with current per-company WIP. This is what ERic's group_default
-- firm_summary uses to render the per-branch breakdown under the group
-- header.
--
-- Mirrors the filter rules of v_eric_wip_company_group_summary: only
-- APPROVED groups + APPROVED members. Unreviewed rows are invisible.
--
-- Out-of-band grant required (wip_app does not hold GRANT OPTION):
--   GRANT SELECT ON nif_wip.v_eric_wip_company_group_members TO 'wip_read'@'%';

CREATE OR REPLACE VIEW v_eric_wip_company_group_members AS
SELECT
    g.id                                AS group_id,
    g.group_name,
    g.group_type,
    m.id                                AS member_id,
    m.wip_company_id,
    m.salesforce_account_id,
    m.canonical_company_id,
    m.member_role,
    curr.company_name,
    curr.account_type,
    curr.industry,
    curr.is_potential_duplicate,
    curr.current_wip_value,
    curr.current_wip_count,
    curr.latest_wip_date,
    curr.has_current_wip,
    m.reviewed_by                        AS member_reviewed_by,
    m.reviewed_at                        AS member_reviewed_at
FROM wip_company_group g
JOIN wip_company_group_member m
    ON m.group_id = g.id
   AND m.review_status = 'APPROVED'
LEFT JOIN v_eric_wip_company_summary curr
    ON curr.wip_company_id = m.wip_company_id
WHERE g.review_status = 'APPROVED';
