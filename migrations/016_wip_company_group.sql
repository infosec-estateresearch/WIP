-- 016_wip_company_group.sql
--
-- WO-14A.1 — Company-group layer (per WO-14B blueprint §2).
--
-- A wip_company_group is the business reporting roll-up above
-- wip_company. It is NOT a replacement for Salesforce Account (which
-- stays the legal/CRM identity), wip_company (which stays the SF mirror),
-- or canonical_company in nif_elt (which stays ERic's identity nucleus).
--
-- A group is INVISIBLE to ERic until review_status = 'APPROVED'. PROPOSED
-- and NEEDS_REVIEW rows live only in the operator workflow.
--
-- The "one APPROVED group per group_name_normalised" rule is enforced by
-- a UNIQUE constraint on a virtual generated column that is NULL except
-- when review_status='APPROVED'. NULLs are allowed multiple times in a
-- UNIQUE index, so REJECTED / PROPOSED / NEEDS_REVIEW rows with the same
-- normalised name can coexist. Same trick is used in 017 for the member
-- table's canonical/wip_company uniqueness rules.

CREATE TABLE wip_company_group (
    id                              BIGINT          NOT NULL AUTO_INCREMENT,
    group_name                      VARCHAR(255)    NOT NULL,
    group_name_normalised           VARCHAR(255)    NOT NULL,
    primary_salesforce_account_id   VARCHAR(18)     NULL,
    primary_canonical_company_id    BIGINT          NULL,
    group_type                      ENUM(
        'FIRM_GROUP',
        'BRAND_GROUP',
        'BRANCH_GROUP',
        'NETWORK_GROUP',
        'UNKNOWN'
    ) NOT NULL DEFAULT 'UNKNOWN',
    review_status                   ENUM(
        'PROPOSED',
        'NEEDS_REVIEW',
        'APPROVED',
        'REJECTED'
    ) NOT NULL DEFAULT 'PROPOSED',
    source_cluster_id               VARCHAR(64)     NULL,
    proposed_by                     VARCHAR(255)    NULL,
    reviewed_by                     VARCHAR(255)    NULL,
    reviewed_at                     DATETIME(6)     NULL,
    resolution_note                 TEXT            NULL,
    seed_warning                    VARCHAR(64)     NULL,
    created_at                      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                                       ON UPDATE CURRENT_TIMESTAMP(6),

    -- Virtual column = NULL unless APPROVED. UNIQUE on it enforces the
    -- "at most one APPROVED group per normalised name" invariant while
    -- allowing PROPOSED/REJECTED duplicates to coexist for audit.
    name_when_approved              VARCHAR(255)
                                    GENERATED ALWAYS AS (
                                        CASE WHEN review_status = 'APPROVED'
                                             THEN group_name_normalised
                                        END
                                    ) VIRTUAL,

    PRIMARY KEY (id),
    UNIQUE KEY ux_wcg_name_when_approved (name_when_approved),
    INDEX ix_wcg_primary_canonical (primary_canonical_company_id),
    INDEX ix_wcg_primary_sf_account (primary_salesforce_account_id),
    INDEX ix_wcg_review_status_type (review_status, group_type),
    INDEX ix_wcg_source_cluster (source_cluster_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
