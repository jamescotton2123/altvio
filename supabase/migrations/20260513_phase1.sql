-- =============================================================================
-- Altvio — Phase 1 Schema Migration
-- Adds columns for: Fund Hub dashboard, KYC completeness tracking,
-- wire instructions on investors, Orion householding declarations,
-- sensitive client flags, commitment lifecycle fields, and new tables
-- for email templates, statement mailings, and dissolution tracking.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- investors: new operational + KYC + Orion fields
-- -----------------------------------------------------------------------------
ALTER TABLE investors
  ADD COLUMN IF NOT EXISTS wire_instructions         JSONB,
  ADD COLUMN IF NOT EXISTS handle_with_care          BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS sensitivity_notes         TEXT,
  ADD COLUMN IF NOT EXISTS orion_is_new_household    BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS orion_linked_household_name TEXT,
  ADD COLUMN IF NOT EXISTS prefers_physical_mail     BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS existing_orion_fee_pct    NUMERIC(6,4),
  ADD COLUMN IF NOT EXISTS country_of_formation      TEXT;

-- -----------------------------------------------------------------------------
-- deals: fund manager info + DocuSign template mapping
-- -----------------------------------------------------------------------------
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS fund_manager           TEXT,
  ADD COLUMN IF NOT EXISTS fund_manager_title     TEXT,
  ADD COLUMN IF NOT EXISTS docusign_template_id   TEXT;

-- -----------------------------------------------------------------------------
-- commitments: lifecycle dates, fee tracking, verbal confirm, wire early send
-- -----------------------------------------------------------------------------
ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS commitment_date         TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS advisory_fee_pct        NUMERIC(6,4),
  ADD COLUMN IF NOT EXISTS wire_sent_at            TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS wire_sent_adv_pending   BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS last_followup_at        TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS kyc_verified            BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS verbal_confirmed        BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS verbal_confirmed_at     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS verbal_confirmed_by     TEXT;

-- -----------------------------------------------------------------------------
-- kyc_reviews: zip source tracking, compliance escalation, ownership tree
-- -----------------------------------------------------------------------------
ALTER TABLE kyc_reviews
  ADD COLUMN IF NOT EXISTS source_archive            TEXT,
  ADD COLUMN IF NOT EXISTS escalated_to_compliance   BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS ownership_structure       JSONB;

-- -----------------------------------------------------------------------------
-- distribution_notices: distribution readiness checklist per investor
-- -----------------------------------------------------------------------------
ALTER TABLE distribution_notices
  ADD COLUMN IF NOT EXISTS kyc_verified              BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS verbal_confirmed          BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS verbal_confirmed_at       TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS verbal_confirmed_by       TEXT,
  ADD COLUMN IF NOT EXISTS negative_consent_sent_at  TIMESTAMPTZ;

-- distributions: negative consent tracking at distribution level
ALTER TABLE distributions
  ADD COLUMN IF NOT EXISTS negative_consent_sent_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS status                    TEXT;

-- -----------------------------------------------------------------------------
-- firm_settings: new per-firm configuration fields
-- -----------------------------------------------------------------------------
ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS compliance_email              TEXT,
  ADD COLUMN IF NOT EXISTS file_naming_template          TEXT DEFAULT 'Sub Docs - {entity_name}',
  ADD COLUMN IF NOT EXISTS docusign_subject_template     TEXT,
  ADD COLUMN IF NOT EXISTS default_advisory_fee_pct      NUMERIC(6,4) DEFAULT 1.0,
  ADD COLUMN IF NOT EXISTS side_letter_template          TEXT,
  ADD COLUMN IF NOT EXISTS distribution_notice_template  TEXT,
  ADD COLUMN IF NOT EXISTS adv_brochure_pdf_path         TEXT;

-- -----------------------------------------------------------------------------
-- email_templates: firm-level overrides for any system email
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_templates (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  template_key    TEXT NOT NULL,  -- e.g. 'email_1', 'funding_received', 'wire_instructions'
  subject         TEXT NOT NULL,
  body_html       TEXT NOT NULL,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (firm_id, template_key)
);

-- -----------------------------------------------------------------------------
-- statement_mailings: physical mail tracker for investors who refuse the portal
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS statement_mailings (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id             UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  investor_id         UUID NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
  document_type       TEXT NOT NULL,  -- 'Statement' | 'K-1' | 'Distribution Notice'
  period              TEXT NOT NULL,  -- e.g. 'Q1 2026', '2025 Annual'
  mailed_date         DATE,
  tracking_number     TEXT,
  confirmed_received  BOOLEAN NOT NULL DEFAULT FALSE,
  confirmed_at        TIMESTAMPTZ,
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- dissolution_tracker: per-investor collection status during fund dissolution
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dissolution_tracker (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id             UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  deal_id             UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
  investor_id         UUID NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
  wire_received       BOOLEAN NOT NULL DEFAULT FALSE,
  kyc_received        BOOLEAN NOT NULL DEFAULT FALSE,
  ops_verbal_cleared  BOOLEAN NOT NULL DEFAULT FALSE,
  last_followup_at    TIMESTAMPTZ,
  response_status     TEXT DEFAULT 'Pending',  -- 'Pending' | 'Responded' | 'Unreachable'
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (deal_id, investor_id)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_dissolution_tracker_deal   ON dissolution_tracker(deal_id);
CREATE INDEX IF NOT EXISTS idx_statement_mailings_investor ON statement_mailings(investor_id);
CREATE INDEX IF NOT EXISTS idx_email_templates_firm        ON email_templates(firm_id);
CREATE INDEX IF NOT EXISTS idx_investors_handle_with_care  ON investors(handle_with_care) WHERE handle_with_care = TRUE;
CREATE INDEX IF NOT EXISTS idx_investors_wire_missing      ON investors(id) WHERE wire_instructions IS NULL;
