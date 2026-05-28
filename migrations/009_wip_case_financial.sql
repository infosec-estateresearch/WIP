CREATE TABLE wip_case_financial (
    id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
    wip_case_id             BIGINT       NOT NULL,

    as_of_date              DATE         NOT NULL
        COMMENT 'date the financial snapshot applies to (sheet month-end)',
    period_year             SMALLINT     NOT NULL,
    period_month            TINYINT      NOT NULL,

    -- DECIMAL throughout; floats forbidden for money
    wip_value               DECIMAL(15,2) NULL,
    fees_invoiced           DECIMAL(15,2) NULL,
    actual_yield            DECIMAL(15,2) NULL,
    estimated_yield         DECIMAL(15,2) NULL,
    currency                CHAR(3)       NOT NULL DEFAULT 'GBP',

    source                  ENUM('csv_import', 'wip_ui', 'wip_api', 'manual')
                            NOT NULL DEFAULT 'csv_import',
    import_batch_id         BIGINT       NULL,

    created_at              DATETIME(6) NOT NULL,
    updated_at              DATETIME(6) NOT NULL,

    CONSTRAINT fk_wip_cf_case
        FOREIGN KEY (wip_case_id) REFERENCES wip_case(id),

    UNIQUE KEY uq_wip_cf_case_period (wip_case_id, period_year, period_month),
    KEY idx_wip_cf_as_of (as_of_date),
    KEY idx_wip_cf_period (period_year, period_month),
    KEY idx_wip_cf_batch (import_batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Per-period WIP/fees/yield for each wip_case'
