-- Daily ops digest + configurable alert window for expiring placement/upfront terms

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS notify_ops_fee_expiry BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS fee_expiry_alert_days INTEGER NOT NULL DEFAULT 90;
