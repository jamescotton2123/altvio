-- =============================================================================
-- Altvio — Phase 4 Schema Migration
-- Wire instructions as structured JSONB + side letter tracking on commitments.
-- =============================================================================

-- deals: migrate wire_instructions from TEXT to JSONB
-- (Applied live via MCP — this file documents the change for migration history)
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS wire_instructions JSONB;

ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS wire_instructions_legacy TEXT;

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS side_letter_pdf_path        TEXT,
  ADD COLUMN IF NOT EXISTS side_letter_generated_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS side_letter_provisions      JSONB;

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS platform_base_url           TEXT;
