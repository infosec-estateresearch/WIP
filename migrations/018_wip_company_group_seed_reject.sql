-- 018_wip_company_group_seed_reject.sql
--
-- WO-14A.1 — Sticky-reject set for the group seed proposer.
--
-- Per WO-14B §7 rule 5: a REJECTED group's group_name_normalised is
-- recorded here so the seed proposer skips any future cluster with the
-- same key. Without this, re-running propose_groups against the WO-13
-- candidates would keep re-creating PROPOSED groups the operator has
-- already said no to.
--
-- Un-rejection is manual: operator deletes the row (or sets
-- expires_at) and the next seed run re-proposes.

CREATE TABLE wip_company_group_seed_reject (
    id                          BIGINT          NOT NULL AUTO_INCREMENT,
    group_name_normalised       VARCHAR(255)    NOT NULL,
    rejected_by                 VARCHAR(255)    NULL,
    rejected_at                 DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    reason                      TEXT            NULL,
    source_group_id             BIGINT          NULL,
    source_cluster_id           VARCHAR(64)     NULL,
    expires_at                  DATETIME(6)     NULL,
    created_at                  DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (id),
    UNIQUE KEY ux_wcgsr_name (group_name_normalised),
    INDEX ix_wcgsr_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
