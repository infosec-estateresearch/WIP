CREATE TABLE wip_company_duplicate_candidate (
    id                          BIGINT AUTO_INCREMENT PRIMARY KEY,
    duplicate_group_key         VARCHAR(255) NOT NULL,
    salesforce_account_ids_json JSON NOT NULL
        COMMENT 'JSON array of the SF Account IDs in this group, e.g. ["001...", "001..."]',
    account_names_json          JSON NOT NULL
        COMMENT 'JSON array of the SF Account Names in this group, parallel to ids',
    candidate_count             INT NOT NULL,

    status                      ENUM('NEW', 'NEEDS_REVIEW', 'RESOLVED_IN_SALESFORCE', 'IGNORED_FALSE_POSITIVE') NOT NULL DEFAULT 'NEW',

    first_detected_at           DATETIME(6) NOT NULL,
    last_seen_at                DATETIME(6) NOT NULL,
    resolution_note             TEXT NULL,
    resolved_at                 DATETIME(6) NULL,
    resolved_by                 VARCHAR(128) NULL,

    UNIQUE KEY uq_wip_company_dup_group_key (duplicate_group_key),
    KEY idx_wip_company_dup_status (status, last_seen_at),
    KEY idx_wip_company_dup_first_seen (first_detected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Duplicate-Salesforce-Account candidates surfaced by hourly sync. WIP NEVER merges — operator resolves in Salesforce.'
