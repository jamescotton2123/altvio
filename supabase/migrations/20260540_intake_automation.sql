-- Intake automation: advisor prospect stash, email review queue, Graph subscription id

CREATE TABLE IF NOT EXISTS intake_prospects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  deal_id UUID REFERENCES deals(id) ON DELETE SET NULL,
  investor_email TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'submitted',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_intake_prospects_firm_email
  ON intake_prospects(firm_id, investor_email, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_intake_prospects_firm_deal_email
  ON intake_prospects(firm_id, deal_id, investor_email, created_at DESC);

CREATE TABLE IF NOT EXISTS intake_email_review (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  message_id TEXT,
  subject TEXT,
  from_address TEXT,
  raw_body TEXT,
  parsed_payload JSONB,
  confidence TEXT,
  status TEXT NOT NULL DEFAULT 'Pending',
  matched_investor_id UUID REFERENCES investors(id) ON DELETE SET NULL,
  reviewed_by TEXT,
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intake_email_review_firm_status
  ON intake_email_review(firm_id, status, created_at DESC);

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS graph_subscription_id TEXT,
  ADD COLUMN IF NOT EXISTS graph_subscription_expires_at TIMESTAMPTZ;

COMMENT ON COLUMN firm_settings.graph_subscription_id IS
  'Microsoft Graph mail subscription id for ops_mailbox change notifications.';
COMMENT ON COLUMN firm_settings.graph_subscription_expires_at IS
  'When the Graph mail subscription expires; renewed by scheduler.';
