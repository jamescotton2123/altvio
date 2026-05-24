ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS kyc_engine TEXT NOT NULL DEFAULT 'openai_vision';

CREATE TABLE IF NOT EXISTS ai_invocations (
  id            BIGSERIAL PRIMARY KEY,
  firm_id       UUID NOT NULL,
  engine        TEXT NOT NULL,
  model_version TEXT NOT NULL,
  task          TEXT NOT NULL,
  entity_id     UUID,
  input_tokens  INT,
  output_tokens INT,
  cost_usd      NUMERIC(10,6),
  latency_ms    INT,
  status        TEXT NOT NULL DEFAULT 'ok',
  audit_log_id  BIGINT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ai_invocations_firm ON ai_invocations(firm_id, task, created_at DESC);
