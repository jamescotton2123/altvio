-- Phase 5b: Portal Expansion — KYC Upload Portal, Capital Account, Self-Service Actions

-- portal_access_tokens: distinguish KYC vs document tokens
ALTER TABLE portal_access_tokens
  ADD COLUMN IF NOT EXISTS token_type TEXT NOT NULL DEFAULT 'document';

-- firm_settings: subdomain for firm-branded portal URLs
ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS portal_subdomain TEXT;

-- commitments: track origin of LOI submissions
ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS loi_source TEXT;

-- Index for fast subdomain lookup on re-auth
CREATE INDEX IF NOT EXISTS idx_firm_settings_portal_subdomain
  ON firm_settings(portal_subdomain)
  WHERE portal_subdomain IS NOT NULL;

-- Index for KYC token lookups
CREATE INDEX IF NOT EXISTS idx_portal_tokens_type
  ON portal_access_tokens(token_type, revoked);
