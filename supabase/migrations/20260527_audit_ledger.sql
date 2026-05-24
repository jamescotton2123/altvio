CREATE TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY,
  firm_id UUID NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id UUID,
  before JSONB,
  after JSONB,
  metadata JSONB,
  prior_hash TEXT NOT NULL DEFAULT '',
  row_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_logs_firm_entity ON audit_logs(firm_id, entity_type, entity_id, created_at DESC);

REVOKE UPDATE, DELETE ON audit_logs FROM authenticated, anon;
