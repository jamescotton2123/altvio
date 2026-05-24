-- Client Associate portal: API-key auth (Schwab / PW wire queue), mirrors traders/advisors.

CREATE TABLE IF NOT EXISTS client_associates (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id         UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  email           TEXT NOT NULL,
  display_name    TEXT NOT NULL DEFAULT '',
  api_key         TEXT UNIQUE,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_client_associates_firm ON client_associates(firm_id);
CREATE INDEX IF NOT EXISTS idx_client_associates_api_key
  ON client_associates(api_key) WHERE api_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_client_associates_firm_email_lower
  ON client_associates(firm_id, lower(email));

COMMENT ON TABLE client_associates IS
  'Account Support / Client Associate desks; api_key authenticates GET /deals/{id}/hub?role=client_associate. '
  'Email must match investors.client_associate_email for row-level visibility.';
