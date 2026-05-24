-- Inbound subscription wire: which legal entity sent capital (must align with KYC subscriber or trigger alt-entity KYC).

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS funding_entity_name TEXT,
  ADD COLUMN IF NOT EXISTS funding_entity_matches_kyc BOOLEAN,
  ADD COLUMN IF NOT EXISTS funding_entity_kyc_status TEXT NOT NULL DEFAULT 'not_recorded';

COMMENT ON COLUMN commitments.funding_entity_name IS
  'Legal entity name on the inbound subscription wire (where capital was sent FROM). Distinct from investors.wire_instructions (distribution payout to investor).';
COMMENT ON COLUMN commitments.funding_entity_matches_kyc IS
  'True when funding_entity_name matches investors.entity_name (normalized). False when different entity funded. NULL when not yet recorded.';
COMMENT ON COLUMN commitments.funding_entity_kyc_status IS
  'not_recorded | not_required | required | complete — alt-entity KYC when inbound sender differs from subscriber.';

CREATE INDEX IF NOT EXISTS idx_commitments_funding_kyc_required
  ON commitments(deal_id, firm_id)
  WHERE funding_entity_kyc_status = 'required';
