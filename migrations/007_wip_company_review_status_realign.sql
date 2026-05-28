ALTER TABLE wip_company
    MODIFY COLUMN duplicate_review_status
        ENUM('NONE', 'NEEDS_REVIEW', 'RESOLVED', 'IGNORED_FALSE_POSITIVE')
        NOT NULL DEFAULT 'NONE'
        COMMENT 'Per-row review state for this company within any duplicate group it belongs to. Distinct from wip_company_duplicate_candidate.status which tracks the GROUP resolution.'
