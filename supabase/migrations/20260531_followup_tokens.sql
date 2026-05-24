CREATE TABLE followup_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID NOT NULL,
  investor_id UUID,
  commitment_id UUID,
  type TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  used_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_followup_tokens_hash ON followup_tokens(token_hash);
