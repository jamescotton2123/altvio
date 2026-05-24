-- Phase 5: Investor Document Portal
-- Creates portal_access_tokens table and adds firm-level portal configuration columns.
-- The portal gives investors a branded, magic-link page for document access and wire instructions.

-- ============================================================
-- portal_access_tokens
-- ============================================================
CREATE TABLE IF NOT EXISTS portal_access_tokens (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  investor_id     UUID NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
  commitment_id   UUID NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
  token           TEXT NOT NULL UNIQUE,
  expires_at      TIMESTAMPTZ NOT NULL,
  accessed_at     TIMESTAMPTZ,
  access_count    INTEGER NOT NULL DEFAULT 0,
  revoked         BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE portal_access_tokens ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_portal_tokens_token ON portal_access_tokens(token);
CREATE INDEX IF NOT EXISTS idx_portal_tokens_commitment ON portal_access_tokens(commitment_id);
CREATE INDEX IF NOT EXISTS idx_portal_tokens_investor ON portal_access_tokens(investor_id);

-- ============================================================
-- firm_settings additions
-- ============================================================
ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS portal_link_expiry_days  INTEGER NOT NULL DEFAULT 30,
  ADD COLUMN IF NOT EXISTS portal_brand_tagline      TEXT;

-- Update wire_delivery_mode to support 'portal' value (no enum constraint — text column)
-- Valid values: 'inline' | 'secure_link' | 'portal'
-- No migration needed for existing rows; current default of 'inline' remains valid.

-- ============================================================
-- commitments: track if portal link was sent
-- ============================================================
ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS portal_link_sent_at      TIMESTAMPTZ;
