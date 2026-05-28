CREATE OR REPLACE VIEW v_eric_wip_firm_with_wip AS
SELECT
    c.id                        AS wip_company_id,
    c.salesforce_account_id,
    c.canonical_company_id,
    c.salesforce_account_name   AS company_name,
    SUM(f.wip_value)            AS total_current_wip,
    COUNT(DISTINCT cs.id)       AS active_case_count,
    MAX(f.as_of_date)           AS latest_wip_date
FROM wip_company c
JOIN wip_case cs ON cs.wip_company_id = c.id
JOIN wip_case_financial f ON f.wip_case_id = cs.id
WHERE (cs.status IS NULL OR cs.status NOT IN ('closed', 'rejected', 'archived'))
  AND f.wip_value > 0
GROUP BY c.id, c.salesforce_account_id, c.canonical_company_id, c.salesforce_account_name
HAVING SUM(f.wip_value) > 0
