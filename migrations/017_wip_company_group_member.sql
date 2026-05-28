-- 017_wip_company_group_member.sql
--
-- WO-14A.1 — Membership of a wip_company in a wip_company_group.
--
-- Critical invariants enforced at schema level via UNIQUE on virtual
-- generated columns (NULL when not APPROVED, so PROPOSED/REJECTED rows
-- can coexist without conflict):
--
--   (a) A wip_company belongs to AT MOST one APPROVED group across the
--       whole table (UNIQUE on wip_company_id_when_approved).
--   (b) A canonical_company_id is in AT MOST one APPROVED group
--       (UNIQUE on canonical_company_id_when_approved). This is what
--       lets ERic answer "show me X" deterministically — no ambiguity
--       about which group a canonical rolls up to.
--   (c) AT MOST one PRIMARY member per APPROVED group
--       (UNIQUE on group_id_when_primary_approved).
--
-- Without (a) and (b), an operator mistake could attach the same
-- wip_company or canonical to two APPROVED groups and silently break
-- the ERic roll-up. The DB rejects rather than silently corrupts.

CREATE TABLE wip_company_group_member (
    id                              BIGINT          NOT NULL AUTO_INCREMENT,
    group_id                        BIGINT          NOT NULL,
    wip_company_id                  BIGINT          NOT NULL,
    salesforce_account_id           VARCHAR(18)     NOT NULL,
    canonical_company_id            BIGINT          NULL,
    member_role                     ENUM(
        'PRIMARY',
        'BRANCH',
        'OFFICE',
        'ALIAS',
        'RELATED_ACCOUNT',
        'UNKNOWN'
    ) NOT NULL DEFAULT 'UNKNOWN',
    confidence_source               ENUM(
        'WO-13-CLUSTER',
        'SF_PARENT_ACCOUNT',
        'OPERATOR_MANUAL'
    ) NOT NULL,
    review_status                   ENUM(
        'PROPOSED',
        'NEEDS_REVIEW',
        'APPROVED',
        'REJECTED'
    ) NOT NULL DEFAULT 'PROPOSED',
    proposed_by                     VARCHAR(255)    NULL,
    reviewed_by                     VARCHAR(255)    NULL,
    reviewed_at                     DATETIME(6)     NULL,
    created_at                      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at                      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                                       ON UPDATE CURRENT_TIMESTAMP(6),

    -- Virtual columns for the partial-unique-index enforcement of the
    -- three invariants. Each is NULL unless the relevant rule applies.
    wip_company_id_when_approved        BIGINT
        GENERATED ALWAYS AS (
            CASE WHEN review_status = 'APPROVED' THEN wip_company_id END
        ) VIRTUAL,
    canonical_company_id_when_approved  BIGINT
        GENERATED ALWAYS AS (
            CASE WHEN review_status = 'APPROVED' THEN canonical_company_id END
        ) VIRTUAL,
    group_id_when_primary_approved      BIGINT
        GENERATED ALWAYS AS (
            CASE WHEN review_status = 'APPROVED' AND member_role = 'PRIMARY'
                 THEN group_id END
        ) VIRTUAL,

    PRIMARY KEY (id),
    UNIQUE KEY ux_wcgm_group_wip (group_id, wip_company_id),
    UNIQUE KEY ux_wcgm_wip_when_approved (wip_company_id_when_approved),
    UNIQUE KEY ux_wcgm_canonical_when_approved (canonical_company_id_when_approved),
    UNIQUE KEY ux_wcgm_primary_per_approved_group (group_id_when_primary_approved),
    INDEX ix_wcgm_group (group_id),
    INDEX ix_wcgm_wip_company (wip_company_id),
    INDEX ix_wcgm_canonical (canonical_company_id),
    INDEX ix_wcgm_review_status (review_status),

    CONSTRAINT fk_wcgm_group
        FOREIGN KEY (group_id) REFERENCES wip_company_group (id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_wcgm_wip_company
        FOREIGN KEY (wip_company_id) REFERENCES wip_company (id)
        ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
