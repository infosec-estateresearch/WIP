-- 015_v_eric_wip_import_coverage.sql
--
-- WO-12 — Expose the latest completed Mastercopy import as a read-only view
-- so ERic can co-report import coverage alongside any WIP answer. Without
-- this, ERic answers from v_eric_wip_company_summary risk reading like the
-- full picture while a backlog of rejected rows sits in wip_import_reject
-- awaiting operator review.
--
-- Selection rule: latest completed, non-dry-run batch. Failed and dry-run
-- batches are excluded so a crash or dry-run never overstates coverage.
--
-- coverage_imported_pct = (inserted + updated + unchanged) / rows_seen.
-- Rejected rows are intentionally excluded from the numerator — they are
-- the operator-review surface, not "imported".
--
-- The GRANT for wip_read is applied out-of-band as root (the migration user
-- does not hold GRANT OPTION). See: GRANT SELECT ON
-- nif_wip.v_eric_wip_import_coverage TO 'wip_read'@'%';

CREATE OR REPLACE VIEW v_eric_wip_import_coverage AS
SELECT
    b.id                          AS latest_batch_id,
    b.source_file,
    b.source_sheet,
    b.completed_at,
    b.rows_seen,
    b.rows_inserted,
    b.rows_updated,
    b.rows_unchanged,
    b.rows_rejected,
    b.status,
    CASE
        WHEN COALESCE(b.rows_seen, 0) = 0 THEN NULL
        ELSE ROUND(
            100.0 * (
                COALESCE(b.rows_inserted, 0)
              + COALESCE(b.rows_updated,  0)
              + COALESCE(b.rows_unchanged,0)
            ) / b.rows_seen,
            1
        )
    END AS coverage_imported_pct
FROM wip_import_batch b
WHERE b.id = (
    SELECT id
      FROM wip_import_batch
     WHERE status   = 'completed'
       AND dry_run  = 0
     ORDER BY completed_at DESC, id DESC
     LIMIT 1
);
