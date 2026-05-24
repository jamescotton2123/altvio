-- =============================================================================
-- Billing meter tables + TOI DocuSign support + transfers_of_interest DDL
-- Aligns with BUSINESS_MODEL.md metered billing and PLATFORM_PLAN Wave A.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- firm_settings: DocuSign Transfer of Interest template (same pattern as LOI)
-- -----------------------------------------------------------------------------
ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS docusign_toi_template_id TEXT;

COMMENT ON COLUMN firm_settings.docusign_toi_template_id IS
  'DocuSign server template GUID for Transfer of Interest; template must define roles Transferor and Transferee with matching text tabs.';

-- -----------------------------------------------------------------------------
-- transfers_of_interest (ops-initiated TOI; optional DocuSign envelope)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transfers_of_interest (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id                  UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  commitment_id            UUID NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
  transferor_investor_id   UUID NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
  transferee_investor_id   UUID NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
  transfer_amount          NUMERIC(18, 2) NOT NULL,
  transfer_date            DATE,
  status                   TEXT NOT NULL DEFAULT 'Pending',
  notes                    TEXT,
  toi_envelope_id          TEXT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transfers_firm           ON transfers_of_interest(firm_id);
CREATE INDEX IF NOT EXISTS idx_transfers_commitment     ON transfers_of_interest(commitment_id);
CREATE INDEX IF NOT EXISTS idx_transfers_toi_envelope   ON transfers_of_interest(toi_envelope_id)
  WHERE toi_envelope_id IS NOT NULL;

-- If transfers_of_interest pre-existed from an ad-hoc deploy, ensure new columns exist.
ALTER TABLE transfers_of_interest ADD COLUMN IF NOT EXISTS toi_envelope_id TEXT;
ALTER TABLE transfers_of_interest ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- -----------------------------------------------------------------------------
-- billing_invoices — one row per firm per billing period (draft or finalized)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_invoices (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  billing_period  TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'draft',
  total_cents     BIGINT NOT NULL DEFAULT 0,
  currency        TEXT NOT NULL DEFAULT 'USD',
  line_items       JSONB NOT NULL DEFAULT '[]'::jsonb,
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_billing_invoice_firm_period
  ON billing_invoices (firm_id, billing_period);

-- -----------------------------------------------------------------------------
-- billing_usage — append meter lines (onboarding per commitment, AIP per period)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_usage (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  event_type      TEXT NOT NULL,
  amount_cents    BIGINT NOT NULL,
  commitment_id   UUID REFERENCES commitments(id) ON DELETE SET NULL,
  billing_period  TEXT NOT NULL,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  invoice_id      UUID REFERENCES billing_invoices(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_billing_onboarding_commitment
  ON billing_usage (firm_id, event_type, commitment_id)
  WHERE event_type = 'onboarding_complete' AND commitment_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_billing_aip_quarter_period
  ON billing_usage (firm_id, event_type, billing_period)
  WHERE event_type = 'aip_bps_quarterly';

CREATE INDEX IF NOT EXISTS idx_billing_usage_firm_period ON billing_usage(firm_id, billing_period);
CREATE INDEX IF NOT EXISTS idx_billing_usage_invoice     ON billing_usage(invoice_id);
