CREATE TABLE wip_case (
    id                      BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- REQUIRED FK to the SF-mirrored company. No orphan WIP allowed.
    wip_company_id          BIGINT NOT NULL,

    -- Denormalised join keys for fast ERic-view consumption
    salesforce_account_id   VARCHAR(18)  NOT NULL,
    canonical_company_id    BIGINT       NULL,

    -- Case identity
    case_reference          VARCHAR(64)  NOT NULL
        COMMENT 'natural key — case number from CSV / future API',
    case_name               VARCHAR(255) NOT NULL,
    case_type               VARCHAR(64)  NULL,

    -- Provenance
    source                  ENUM('csv_import', 'wip_ui', 'wip_api', 'manual')
                            NOT NULL DEFAULT 'csv_import',

    -- Lifecycle
    status                  VARCHAR(64)  NULL,
    opened_at               DATE         NULL,
    closed_at               DATE         NULL,

    created_at              DATETIME(6)  NOT NULL,
    updated_at              DATETIME(6)  NOT NULL,

    CONSTRAINT fk_wip_case_company
        FOREIGN KEY (wip_company_id) REFERENCES wip_company(id),

    UNIQUE KEY uq_wip_case_reference (case_reference),
    KEY idx_wip_case_company (wip_company_id),
    KEY idx_wip_case_sf_account (salesforce_account_id),
    KEY idx_wip_case_canonical (canonical_company_id),
    KEY idx_wip_case_status_closed (status, closed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='WIP records — FK to wip_company is mandatory (no orphan WIP)'
