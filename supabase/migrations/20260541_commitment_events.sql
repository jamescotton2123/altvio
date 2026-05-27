-- Commitment event audit trail (used by PATCH /commitments and recent_activity widget)

CREATE TABLE IF NOT EXISTS commitment_events (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id        UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  commitment_id  UUID NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
  event_type     TEXT NOT NULL,
  old_value      JSONB,
  new_value      JSONB,
  changed_by     TEXT,
  changed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_commitment_events_firm_changed
  ON commitment_events (firm_id, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_commitment_events_commitment
  ON commitment_events (commitment_id, changed_at DESC);

COMMENT ON TABLE commitment_events IS
  'Audit log of commitment field changes — powers GET /exec recent_activity widget.';

-- RLS (service_role bypasses; app_firm_user scoped via current_firm_id())
ALTER TABLE commitment_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON commitment_events;
CREATE POLICY firm_isolation ON commitment_events
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());
