ALTER TABLE wip_company
    ADD COLUMN account_name_normalised   VARCHAR(255) NULL
        COMMENT 'lowercased, suffix/punct-stripped form of salesforce_account_name; computed at sync time' AFTER salesforce_account_name,
    ADD COLUMN duplicate_group_key       VARCHAR(255) NULL
        COMMENT 'rows sharing this key + is_active_in_salesforce=1 are duplicate candidates' AFTER account_name_normalised,
    ADD COLUMN duplicate_review_status   ENUM('NEW', 'NEEDS_REVIEW', 'RESOLVED_IN_SALESFORCE', 'IGNORED_FALSE_POSITIVE') NOT NULL DEFAULT 'NEW'
        COMMENT 'operator-managed lifecycle for this row participating in any duplicate candidate group' AFTER duplicate_group_key,
    ADD COLUMN is_potential_duplicate    TINYINT(1) NOT NULL DEFAULT 0
        COMMENT 'set by sync when 2+ active rows share duplicate_group_key' AFTER duplicate_review_status,
    ADD KEY idx_wip_company_dup_group (duplicate_group_key, is_active_in_salesforce),
    ADD KEY idx_wip_company_dup_flag (is_potential_duplicate, duplicate_review_status)
