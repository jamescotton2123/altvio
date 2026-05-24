-- Private-wealth segmentation: public-markets / Schwab-managed clients only (liquidation + CA wire flow)

ALTER TABLE investors
  ADD COLUMN IF NOT EXISTS private_wealth BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS client_associate_email TEXT;

COMMENT ON COLUMN investors.private_wealth IS
  'True when firm manages this client public-markets assets (Schwab, etc.). Liquidation/trader portal + CA alerts apply only when this is true.';

COMMENT ON COLUMN investors.client_associate_email IS
  'Account support / Client Associate who initiates Schwab wires for this private-wealth client; copied on liquidation digests.';

CREATE INDEX IF NOT EXISTS idx_investors_private_wealth_firm
  ON investors(firm_id, private_wealth)
  WHERE private_wealth = TRUE;
