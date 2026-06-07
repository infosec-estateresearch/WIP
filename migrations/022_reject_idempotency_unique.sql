-- WO-23A: make the Mastercopy reject queue idempotent.
--
-- Root cause of the 2M-row bloat: log_reject() always INSERTed, so the hourly
-- import (WO-22) re-inserted the full ~26.7k unmatched set every run. The same
-- logical Mastercopy row is uniquely identified by its source position; a
-- UNIQUE key on that lets log_reject() upsert (INSERT ... ON DUPLICATE KEY
-- UPDATE) instead of appending, holding the queue steady.
--
-- One-off data cleanup performed alongside this (kept the latest/operator-touched
-- row per source position, dropping ~1.97M duplicates; the pre-cleanup table was
-- retained out-of-band as wip_import_reject_bloat_bak and can be dropped once
-- confirmed). This migration is the durable schema guard.

ALTER TABLE wip_import_reject
  ADD UNIQUE INDEX uq_reject_source (source_file(191), source_sheet(64), source_row_number);
