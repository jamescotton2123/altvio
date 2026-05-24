-- Phase 7b: Third-party fee arrangements per deal
-- Tracks placement agent / sub-advisor fees: implementation fee, upfront fee
-- with configurable term (default 3 years), and carry. Expiry alerts surface
-- in the Ops To-Do dashboard and Exec Command Center ops_pulse.

CREATE TABLE IF NOT EXISTS deal_fee_arrangements (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id                  UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  deal_id                  UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,

  -- Who receives this fee
  recipient_name           TEXT NOT NULL,
  recipient_email          TEXT,
  arrangement_type         TEXT NOT NULL DEFAULT 'placement_agent',
    -- placement_agent | sub_advisor | referral_partner | other

  -- One-time implementation fee (flat dollar)
  implementation_fee       NUMERIC,

  -- Upfront fee: percentage of committed capital OR flat dollar
  upfront_fee_pct          NUMERIC,
  upfront_fee_amount       NUMERIC,

  -- Term tracking with expiry alert
  upfront_fee_term_years   INTEGER NOT NULL DEFAULT 3,
  upfront_fee_start_date   DATE,
  upfront_fee_expiry_date  DATE,          -- computed as start + term, overridable

  -- Carry structure
  carry_pct                NUMERIC,
  carry_hurdle_pct         NUMERIC,

  notes                    TEXT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE deal_fee_arrangements ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_fee_arrangements_deal
  ON deal_fee_arrangements(deal_id);

CREATE INDEX IF NOT EXISTS idx_fee_arrangements_firm
  ON deal_fee_arrangements(firm_id);

-- Partial index for fast expiry alert queries
CREATE INDEX IF NOT EXISTS idx_fee_arrangements_expiry
  ON deal_fee_arrangements(upfront_fee_expiry_date)
  WHERE upfront_fee_expiry_date IS NOT NULL;
