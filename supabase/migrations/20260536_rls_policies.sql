-- Phase 3b: Row-Level Security policies — firm isolation
--
-- Builds on 20260530_rls_foundation.sql which created the app_firm_user role
-- and the current_firm_id() helper that reads the app.current_firm_id GUC
-- bound per-request by the Python app.
--
-- For each tenant-scoped table we ENABLE RLS and install a firm_isolation
-- policy that constrains every SELECT/INSERT/UPDATE/DELETE issued under
-- app_firm_user to rows whose firm_id matches the session GUC.
--
-- Idempotency: policies are dropped then recreated so the migration can be
-- re-applied safely (Postgres 15 does not support CREATE POLICY IF NOT EXISTS).
-- ALTER TABLE ... ENABLE ROW LEVEL SECURITY is itself idempotent.

-- ============================================================
-- Standard firm_isolation policies
-- ============================================================

-- investors (the Rolodex)
ALTER TABLE investors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON investors;
CREATE POLICY firm_isolation ON investors
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- deals (the Hub)
ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON deals;
CREATE POLICY firm_isolation ON deals
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- commitments (the Ledger — drives billing: $75/onboarding, 1.5 bps AIP)
ALTER TABLE commitments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON commitments;
CREATE POLICY firm_isolation ON commitments
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- kyc_reviews (Agentic KYC Auditor output)
ALTER TABLE kyc_reviews ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON kyc_reviews;
CREATE POLICY firm_isolation ON kyc_reviews
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- firm_settings (per-firm config: branding, templates, integrations)
ALTER TABLE firm_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON firm_settings;
CREATE POLICY firm_isolation ON firm_settings
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- email_templates (firm-customized investor comms)
ALTER TABLE email_templates ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON email_templates;
CREATE POLICY firm_isolation ON email_templates
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- statement_mailings
ALTER TABLE statement_mailings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON statement_mailings;
CREATE POLICY firm_isolation ON statement_mailings
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- dissolution_tracker
ALTER TABLE dissolution_tracker ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON dissolution_tracker;
CREATE POLICY firm_isolation ON dissolution_tracker
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- ai_invocations (AI engine call log — billable surface)
ALTER TABLE ai_invocations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON ai_invocations;
CREATE POLICY firm_isolation ON ai_invocations
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- portal_access_tokens (RLS was enabled by 20260513_phase5_portal.sql
-- without policies, leaving the table effectively unreadable to
-- app_firm_user; this installs the missing firm_isolation policy.)
ALTER TABLE portal_access_tokens ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON portal_access_tokens;
CREATE POLICY firm_isolation ON portal_access_tokens
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- traders (Trader Portal — Private Wealth liquidation alerts)
ALTER TABLE traders ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON traders;
CREATE POLICY firm_isolation ON traders
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- client_associates (PW advisor delegate accounts)
ALTER TABLE client_associates ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON client_associates;
CREATE POLICY firm_isolation ON client_associates
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- advisor_insight_reports
ALTER TABLE advisor_insight_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON advisor_insight_reports;
CREATE POLICY firm_isolation ON advisor_insight_reports
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- billing_usage (per-firm metered events — onboarding + AIP bps)
ALTER TABLE billing_usage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON billing_usage;
CREATE POLICY firm_isolation ON billing_usage
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- billing_invoices
ALTER TABLE billing_invoices ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON billing_invoices;
CREATE POLICY firm_isolation ON billing_invoices
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- transfers_of_interest
ALTER TABLE transfers_of_interest ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON transfers_of_interest;
CREATE POLICY firm_isolation ON transfers_of_interest
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- webhook_events (DocuSign / KYC / payment callbacks)
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON webhook_events;
CREATE POLICY firm_isolation ON webhook_events
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- firm_intake_keys (firm-scoped API keys for external intake)
ALTER TABLE firm_intake_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS firm_isolation ON firm_intake_keys;
CREATE POLICY firm_isolation ON firm_intake_keys
  FOR ALL TO app_firm_user
  USING (firm_id = current_firm_id())
  WITH CHECK (firm_id = current_firm_id());

-- ============================================================
-- audit_logs — special handling
--
-- The audit subsystem must be able to record events on behalf of any firm
-- (e.g. background workers, webhook handlers, cross-firm admin actions)
-- without first binding app.current_firm_id. We grant service_role an
-- unconstrained INSERT and give app_firm_user a firm-scoped SELECT so
-- operators can only read their own firm's ledger.
--
-- Note: service_role bypasses RLS by default in Supabase, but we declare
-- the policy explicitly so the contract is auditable from the schema.
-- ============================================================
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_logs_service_insert ON audit_logs;
CREATE POLICY audit_logs_service_insert ON audit_logs
  FOR INSERT TO service_role
  WITH CHECK (true);

DROP POLICY IF EXISTS audit_logs_firm_select ON audit_logs;
CREATE POLICY audit_logs_firm_select ON audit_logs
  FOR SELECT TO app_firm_user
  USING (firm_id = current_firm_id());
