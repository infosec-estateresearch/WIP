CREATE OR REPLACE VIEW v_eric_wip_company_summary AS
SELECT
    c.id                              AS wip_company_id,
    c.salesforce_account_id,
    c.canonical_company_id,
    c.salesforce_account_name         AS company_name,
    c.account_type,
    c.industry,
    c.is_active_in_salesforce,
    c.is_potential_duplicate,
    COALESCE(curr.current_wip_value, 0)   AS current_wip_value,
    COALESCE(curr.current_wip_count, 0)   AS current_wip_count,
    curr.latest_wip_date,
    (COALESCE(curr.current_wip_value, 0) > 0) AS has_current_wip
FROM wip_company c
LEFT JOIN (
    SELECT
        cs.wip_company_id,
        SUM(f.wip_value)        AS current_wip_value,
        COUNT(DISTINCT cs.id)   AS current_wip_count,
        MAX(f.as_of_date)       AS latest_wip_date
    FROM wip_case cs
    JOIN wip_case_financial f ON f.wip_case_id = cs.id
    WHERE (cs.status IS NULL OR cs.status NOT IN ('closed', 'rejected', 'archived'))
    GROUP BY cs.wip_company_id
) curr ON curr.wip_company_id = c.id
