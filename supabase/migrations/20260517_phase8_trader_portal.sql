-- Phase 8: Trader / private-wealth desk portal — liquidation funding alerts per commitment

CREATE TABLE IF NOT EXISTS traders (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  display_name    TEXT NOT NULL,
  email           TEXT NOT NULL,
  api_key         TEXT UNIQUE,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  preferences     JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_traders_firm ON traders(firm_id);
CREATE INDEX IF NOT EXISTS idx_traders_api_key ON traders(api_key) WHERE api_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_traders_email ON traders(firm_id, lower(email));

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS trader_id                  UUID REFERENCES traders(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS liquidation_required       BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS liquidation_due_date     DATE,
  ADD COLUMN IF NOT EXISTS liquidation_desk_notes     TEXT,
  ADD COLUMN IF NOT EXISTS liquidation_acknowledged_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_commitments_trader_liquidation
  ON commitments(firm_id, trader_id, liquidation_required)
  WHERE liquidation_required = TRUE;

COMMENT ON COLUMN commitments.liquidation_required IS
  'When true, private-wealth desk must liquidate positions to fund this commitment; surfaced on trader portal and optional daily digest.';
COMMENT ON COLUMN commitments.liquidation_due_date IS
  'Target date by which proceeds should be available for wire (ops-set; drives alerts).';
COMMENT ON COLUMN commitments.liquidation_acknowledged_at IS
  'Set when assigned trader acknowledges the liquidation ticket via trader portal.';

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS notify_trader_liquidation_digest BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS trader_liquidation_alert_days    INTEGER NOT NULL DEFAULT 14;

COMMENT ON COLUMN firm_settings.trader_liquidation_alert_days IS
  'Include commitments in trader digest when liquidation_due_date is within this many days, today, or past; unfunded only.';
