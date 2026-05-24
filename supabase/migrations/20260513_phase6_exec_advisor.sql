-- Phase 6: Executive Command Center + Advisor Portal

-- Extend advisors table (already existed with basic columns)
ALTER TABLE advisors
  ADD COLUMN IF NOT EXISTS firm_id    UUID REFERENCES firms(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS phone      TEXT,
  ADD COLUMN IF NOT EXISTS title      TEXT,
  ADD COLUMN IF NOT EXISTS is_active  BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS api_key    TEXT UNIQUE;

CREATE INDEX IF NOT EXISTS idx_advisors_firm ON advisors(firm_id);
CREATE INDEX IF NOT EXISTS idx_advisors_email ON advisors(email);
CREATE INDEX IF NOT EXISTS idx_advisors_api_key ON advisors(api_key) WHERE api_key IS NOT NULL;

-- Link investors to advisors for scoped queries
ALTER TABLE investors
  ADD COLUMN IF NOT EXISTS advisor_id UUID REFERENCES advisors(id);

CREATE INDEX IF NOT EXISTS idx_investors_advisor_id
  ON investors(advisor_id)
  WHERE advisor_id IS NOT NULL;
