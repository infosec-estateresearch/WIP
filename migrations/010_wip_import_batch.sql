CREATE TABLE wip_import_batch (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_type         ENUM('csv_mastercopy', 'salesforce_account_sync',
                             'manual', 'wip_ui', 'wip_api') NOT NULL,
    source_file         VARCHAR(512) NULL,
    source_sheet        VARCHAR(255) NULL,
    source_hash         VARCHAR(64)  NULL
        COMMENT 'SHA-256 of the source file at import time — for drift detection',
    dry_run             TINYINT(1)   NOT NULL DEFAULT 0,

    started_at          DATETIME(6)  NOT NULL,
    completed_at        DATETIME(6)  NULL,
    status              ENUM('running', 'completed', 'failed', 'partial') NOT NULL,

    rows_seen           INT NOT NULL DEFAULT 0,
    rows_inserted       INT NOT NULL DEFAULT 0,
    rows_updated        INT NOT NULL DEFAULT 0,
    rows_unchanged      INT NOT NULL DEFAULT 0,
    rows_rejected       INT NOT NULL DEFAULT 0,

    error_message       TEXT NULL,
    created_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),

    KEY idx_wip_import_batch_type_time (source_type, started_at),
    KEY idx_wip_import_batch_hash (source_hash),
    KEY idx_wip_import_batch_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Per-import telemetry (CSV / SF sync / manual)'
