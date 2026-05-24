-- Private-wealth liquidity: manual Schwab cash estimate vs commitment; desk + CA coordination

ALTER TABLE investors
  ADD COLUMN IF NOT EXISTS schwab_estimated_liquid_cash NUMERIC(18, 2);

COMMENT ON COLUMN investors.schwab_estimated_liquid_cash IS
  'Ops/CA-maintained estimate of investable cash in Schwab (or other custody). NULL = unknown — conservative path assumes liquidation review needed.';

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS liquidation_needed BOOLEAN,
  ADD COLUMN IF NOT EXISTS cash_shortfall NUMERIC(18, 2);

COMMENT ON COLUMN commitments.liquidation_needed IS
  'True when private-wealth commitment likely needs trades or funding review: cash shortfall > 0 or Schwab balance unknown.';
COMMENT ON COLUMN commitments.cash_shortfall IS
  'max(0, total investor wire due - schwab_estimated_liquid_cash) when balance known; NULL when unknown. Wire = commitment + fees in wire (deal_fee_arrangements).';
