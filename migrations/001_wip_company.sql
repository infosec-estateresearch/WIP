CREATE TABLE wip_company (
    id                          BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- Salesforce identity (PRIMARY KEY for sync; never NULL)
    salesforce_account_id       VARCHAR(18)  NOT NULL,
    salesforce_account_name     VARCHAR(255) NOT NULL,

    -- Salesforce-mirrored fields (synced hourly, may change)
    account_type                VARCHAR(64)  NULL,
    industry                    VARCHAR(128) NULL,
    owner_salesforce_id         VARCHAR(18)  NULL,
    owner_name                  VARCHAR(255) NULL,
    billing_street              VARCHAR(255) NULL,
    billing_city                VARCHAR(128) NULL,
    billing_postcode            VARCHAR(32)  NULL,
    billing_country             VARCHAR(64)  NULL,
    tlp_flag                    TINYINT(1)   NULL,

    -- ERic cross-source bridge — resolved at sync time. May remain NULL.
    canonical_company_id        BIGINT       NULL,

    -- Lifecycle state (soft-delete only — NEVER hard-delete)
    is_active_in_salesforce     TINYINT(1)   NOT NULL DEFAULT 1,
    salesforce_deleted_at       DATETIME(6)  NULL,
    last_seen_in_salesforce_at  DATETIME(6)  NOT NULL,
    last_salesforce_sync_at     DATETIME(6)  NOT NULL,

    created_at                  DATETIME(6)  NOT NULL,
    updated_at                  DATETIME(6)  NOT NULL,

    UNIQUE KEY uq_wip_company_sf_id (salesforce_account_id),
    KEY idx_wip_company_canonical (canonical_company_id),
    KEY idx_wip_company_active (is_active_in_salesforce, last_seen_in_salesforce_at),
    KEY idx_wip_company_name (salesforce_account_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Salesforce Account mirror'
