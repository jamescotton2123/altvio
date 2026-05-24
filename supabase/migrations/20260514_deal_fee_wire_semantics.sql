-- How upfront flat amounts and implementation fees interact with investor wires

ALTER TABLE deal_fee_arrangements
  ADD COLUMN IF NOT EXISTS upfront_fee_amount_basis TEXT NOT NULL DEFAULT 'per_commitment',
  ADD COLUMN IF NOT EXISTS include_implementation_in_wire BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE deal_fee_arrangements
  ALTER COLUMN recipient_name DROP NOT NULL;

COMMENT ON COLUMN deal_fee_arrangements.upfront_fee_amount_basis IS
  'per_commitment: upfront_fee_amount added to each investor wire; pro_rata_deal_total: upfront_fee_amount is a deal-level total, allocated by commitment / sum(active commitments).';

COMMENT ON COLUMN deal_fee_arrangements.include_implementation_in_wire IS
  'When true, implementation_fee is split equally across active commitments and included in each investor wire calculation.';
