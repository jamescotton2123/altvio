-- Weekly advisor performance insight reports (metrics + GPT narrative per advisor).

CREATE TABLE IF NOT EXISTS advisor_insight_reports (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id       UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  period_label  TEXT NOT NULL,
  report_data   JSONB NOT NULL,
  summary_text  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_advisor_insight_firm
  ON advisor_insight_reports(firm_id, generated_at DESC);

COMMENT ON TABLE advisor_insight_reports IS
  'Weekly advisor desk metrics + firm_executive GPT brief for CEO/partners (Executive Command Center).';
