CREATE TABLE wip_import_reject (
    id                          BIGINT AUTO_INCREMENT PRIMARY KEY,
    import_batch_id             BIGINT NOT NULL,

    -- Source-row provenance
    source_file                 VARCHAR(512) NULL,
    source_sheet                VARCHAR(255) NULL,
    source_row_number           INT          NULL,

    -- The raw + normalised firm name as the operator typed/recorded it
    firm_name_raw               VARCHAR(255) NULL,
    firm_name_normalised        VARCHAR(255) NULL,
    case_reference_raw          VARCHAR(64)  NULL,

    -- Why this row didn't import
    reject_reason               ENUM(
        'NO_COMPANY_MATCH',
        'MULTIPLE_COMPANY_MATCHES',
        'POTENTIAL_DUPLICATE_COMPANY',
        'MISSING_FIRM_NAME',
        'MISSING_CASE_REFERENCE',
        'INVALID_FINANCIAL_VALUE',
        'INVALID_DATE',
        'DUPLICATE_SOURCE_ROW',
        'UNSUPPORTED_ROW_SHAPE'
    ) NOT NULL,

    -- Disambiguation aid for the operator
    candidate_company_ids_json     JSON NULL,
    candidate_company_names_json   JSON NULL,

    -- Raw source row as JSON so a human can see what they're fixing
    row_json                    JSON NOT NULL,

    -- Operator-managed lifecycle
    resolution_status           ENUM('NEW', 'NEEDS_REVIEW', 'RESOLVED', 'IGNORED', 'REJECTED')
                                NOT NULL DEFAULT 'NEW',
    resolution_note             TEXT NULL,
    reviewed_by                 VARCHAR(255) NULL,
    reviewed_at                 DATETIME(6)  NULL,

    created_at                  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),

    CONSTRAINT fk_wip_reject_batch
        FOREIGN KEY (import_batch_id) REFERENCES wip_import_batch(id),

    KEY idx_wip_reject_batch (import_batch_id),
    KEY idx_wip_reject_resolution (resolution_status, reject_reason),
    KEY idx_wip_reject_firm_case (firm_name_normalised, case_reference_raw),
    KEY idx_wip_reject_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='First-class review queue for unimportable CSV rows. GUI-ready.'
