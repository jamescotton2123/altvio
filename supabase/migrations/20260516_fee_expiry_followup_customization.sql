-- Firm-level playbook when placement/upfront terms approach expiry (each firm handles post-expiry differently)

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS fee_expiry_followup_style TEXT NOT NULL DEFAULT 'third_party_reminder',
  ADD COLUMN IF NOT EXISTS fee_expiry_custom_instructions TEXT;

COMMENT ON COLUMN firm_settings.fee_expiry_followup_style IS
  'third_party_reminder | management_fee_transition | capital_call_planning | management_and_capital | custom_only — controls extra ops guidance appended to fee expiry digest';
