ALTER TABLE wip_company_duplicate_candidate
    ADD COLUMN account_metadata_json       JSON         NULL
        COMMENT 'Owner, Type, Industry, postcode, last_modified per candidate — fed by sync for GUI review' AFTER account_names_json,
    ADD COLUMN last_salesforce_sync_run_id BIGINT       NULL
        COMMENT 'FK to wip_sync_run.id — which sync most recently confirmed this candidate' AFTER last_seen_at,
    ADD COLUMN reviewed_by                 VARCHAR(255) NULL
        COMMENT 'Operator identifier who touched the review' AFTER last_salesforce_sync_run_id,
    ADD COLUMN reviewed_at                 DATETIME(6)  NULL
        COMMENT 'Most recent human review timestamp' AFTER reviewed_by,
    ADD COLUMN created_at                  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) AFTER resolution_note,
    ADD COLUMN updated_at                  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) AFTER created_at,
    DROP COLUMN resolved_at,
    DROP COLUMN resolved_by,
    ADD KEY idx_wip_dup_cand_sync_run (last_salesforce_sync_run_id)
