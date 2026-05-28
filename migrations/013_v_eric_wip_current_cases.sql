CREATE OR REPLACE VIEW v_eric_wip_current_cases AS
SELECT
    cs.id                       AS wip_case_id,
    cs.salesforce_account_id,
    cs.canonical_company_id,
    c.salesforce_account_name   AS company_name,
    cs.case_reference,
    cs.case_name,
    cs.case_type,
    cs.status,
    cs.opened_at,
    f.wip_value                 AS current_wip_value,
    f.fees_invoiced             AS current_fees_invoiced,
    f.actual_yield              AS current_actual_yield,
    f.estimated_yield           AS current_estimated_yield,
    f.as_of_date                AS financial_as_of
FROM wip_case cs
JOIN wip_company c ON c.id = cs.wip_company_id
LEFT JOIN wip_case_financial f
    ON f.wip_case_id = cs.id
    AND f.as_of_date = (
        SELECT MAX(as_of_date)
        FROM wip_case_financial f2
        WHERE f2.wip_case_id = cs.id
    )
WHERE cs.status IS NULL OR cs.status NOT IN ('closed', 'rejected', 'archived')
