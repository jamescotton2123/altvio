-- Store per-firm Microsoft Graph webhook clientState secrets.

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS graph_subscription_client_state TEXT;

COMMENT ON COLUMN firm_settings.graph_subscription_client_state IS
  'Random Microsoft Graph subscription clientState used to validate webhook notifications for this firm.';
