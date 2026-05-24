-- =============================================================================
-- Altvio — Phase 3 Schema Migration
-- DocuSign pipeline upgrades: per-deal template config, 6-step signing chain,
-- pre-fill review queue, KYC field extraction, advisory fee on commitments.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- deals: per-deal DocuSign template map + advisory template + subject template
-- -----------------------------------------------------------------------------
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS docusign_templates         JSONB,
  ADD COLUMN IF NOT EXISTS docusign_advisory_template_id TEXT,
  ADD COLUMN IF NOT EXISTS email_subject_template     TEXT;

-- -----------------------------------------------------------------------------
-- investors: KYC-extracted structural fields
-- -----------------------------------------------------------------------------
ALTER TABLE investors
  ADD COLUMN IF NOT EXISTS state_of_formation        TEXT,
  ADD COLUMN IF NOT EXISTS formation_date            DATE;

-- -----------------------------------------------------------------------------
-- investor_pending_changes: source document URL for pre-fill review
-- -----------------------------------------------------------------------------
ALTER TABLE investor_pending_changes
  ADD COLUMN IF NOT EXISTS source_doc_url  TEXT,
  ADD COLUMN IF NOT EXISTS source          TEXT;  -- e.g. 'kyc_extraction', 'loi_sync'

-- -----------------------------------------------------------------------------
-- firm_settings: DocuSign signing chain configuration
-- -----------------------------------------------------------------------------
ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS docusign_reviewer_email   TEXT,
  ADD COLUMN IF NOT EXISTS ceo_email                 TEXT,
  ADD COLUMN IF NOT EXISTS docusign_role_names       JSONB,
  ADD COLUMN IF NOT EXISTS docusign_w9_template_id   TEXT,
  ADD COLUMN IF NOT EXISTS docusign_w8ben_template_id  TEXT,
  ADD COLUMN IF NOT EXISTS docusign_w8bene_template_id TEXT;

-- Default role names JSONB for firms that haven't customized
-- (can be overridden per firm via PATCH /settings)
UPDATE firm_settings
SET docusign_role_names = '{
  "reviewer": "Reviewer",
  "ops": "OpsCountersigner",
  "investor": "Investor",
  "advisor": "Advisor",
  "compliance": "Compliance",
  "ceo": "CEO"
}'::jsonb
WHERE docusign_role_names IS NULL;
