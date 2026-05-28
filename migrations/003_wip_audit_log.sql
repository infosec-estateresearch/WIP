CREATE TABLE wip_audit_log (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    entity_type     ENUM('wip_company', 'wip_case', 'wip_case_financial',
                         'wip_status_history') NOT NULL,
    entity_id       BIGINT       NOT NULL,
    action          ENUM('insert', 'update', 'soft_delete', 'reactivate') NOT NULL,

    -- Changed fields only — see WO-9B blueprint section 8.
    -- before_json/after_json contain ONLY the keys whose values changed,
    -- plus the sync_run_id / import_batch_id provenance pointer.
    changed_fields_json  JSON         NULL,
    after_json           JSON         NOT NULL,

    actor           VARCHAR(128) NOT NULL,
    sync_run_id     BIGINT       NULL,
    changed_at      DATETIME(6)  NOT NULL,

    KEY idx_wip_audit_entity (entity_type, entity_id, changed_at),
    KEY idx_wip_audit_actor_time (actor, changed_at),
    KEY idx_wip_audit_sync_run (sync_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Append-only audit log — never UPDATE or DELETE rows here'
