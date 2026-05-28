CREATE TABLE wip_sync_run (
    id                          BIGINT AUTO_INCREMENT PRIMARY KEY,
    sync_type                   ENUM('salesforce_account_incremental',
                                     'salesforce_account_full') NOT NULL,
    started_at                  DATETIME(6) NOT NULL,
    completed_at                DATETIME(6) NULL,
    status                      ENUM('running', 'completed', 'failed', 'partial') NOT NULL,

    -- Per-run telemetry
    sf_accounts_fetched         INT NOT NULL DEFAULT 0,
    sf_accounts_inserted        INT NOT NULL DEFAULT 0,
    sf_accounts_updated         INT NOT NULL DEFAULT 0,
    sf_accounts_unchanged       INT NOT NULL DEFAULT 0,
    sf_accounts_soft_deleted    INT NOT NULL DEFAULT 0,
    canonical_resolved          INT NOT NULL DEFAULT 0,
    canonical_unresolved        INT NOT NULL DEFAULT 0,

    -- Incremental sync watermark — the LastModifiedDate this run filtered against
    incremental_since           DATETIME(6) NULL,

    error_message               TEXT NULL,

    KEY idx_wip_sync_run_type_time (sync_type, started_at),
    KEY idx_wip_sync_run_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Per-run Salesforce-to-WIP sync telemetry'
