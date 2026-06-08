-- WO-27: manual WIP entry support.
--
-- The add/edit WIP UI lets an operator record a WIP case against an EXISTING
-- Salesforce-aligned firm (never creating a company). wip_case.source already
-- carries a 'manual' enum value; these nullable columns let a manual row also
-- carry a free-text note and record who created/last-edited it, so the future
-- (cutover-gated) write path is complete without a redesign.
--
-- All columns are nullable and unused by the Mastercopy import / Salesforce sync
-- paths, so existing writers are unaffected. Writes remain gated behind the
-- WIP_MANUAL_WRITES_ENABLED feature flag until the Mastercopy cutover (WO-27B).

ALTER TABLE wip_case
  ADD COLUMN manual_note VARCHAR(500) NULL AFTER status,
  ADD COLUMN created_by  VARCHAR(128) NULL AFTER manual_note,
  ADD COLUMN updated_by  VARCHAR(128) NULL AFTER created_by;
