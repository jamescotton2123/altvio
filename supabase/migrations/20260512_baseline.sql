-- Baseline core tables so a fresh database can be initialized from migrations alone.
-- Later migrations continue to add feature-specific columns and indexes idempotently.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS firms (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID,
  name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS advisors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  email TEXT,
  display_name TEXT,
  phone TEXT,
  title TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  api_key TEXT UNIQUE,
  api_key_hash TEXT,
  api_key_last8 TEXT,
  preferences JSONB NOT NULL DEFAULT '{
    "client_sort": "committed_desc",
    "default_filter": "all",
    "show_dollar_amounts": true,
    "show_columns": ["entity_name", "kyc_status", "total_committed", "overall_status", "wire_on_file", "fund_count"]
  }'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS investors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  advisor_id UUID REFERENCES advisors(id),
  entity_name TEXT,
  entity_type TEXT,
  primary_email TEXT,
  advisor_email TEXT,
  kyc_status TEXT NOT NULL DEFAULT 'Pending',
  wire_instructions JSONB,
  handle_with_care BOOLEAN NOT NULL DEFAULT FALSE,
  sensitivity_notes TEXT,
  orion_is_new_household BOOLEAN NOT NULL DEFAULT FALSE,
  orion_linked_household_name TEXT,
  prefers_physical_mail BOOLEAN NOT NULL DEFAULT FALSE,
  existing_orion_fee_pct NUMERIC(6,4),
  country_of_formation TEXT,
  state_of_formation TEXT,
  formation_date DATE,
  phone TEXT,
  private_wealth BOOLEAN NOT NULL DEFAULT FALSE,
  client_associate_email TEXT,
  schwab_estimated_liquid_cash NUMERIC(18, 2),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  offering_name TEXT,
  status TEXT,
  close_date DATE,
  target_raise NUMERIC(18, 2),
  fund_manager TEXT,
  fund_manager_title TEXT,
  docusign_template_id TEXT,
  docusign_templates JSONB,
  docusign_advisory_template_id TEXT,
  email_subject_template TEXT,
  wire_instructions JSONB,
  wire_instructions_legacy TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS commitments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  investor_id UUID REFERENCES investors(id) ON DELETE CASCADE,
  deal_id UUID REFERENCES deals(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'Pending',
  committed_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
  funded_amount NUMERIC(18, 2),
  fee_amount NUMERIC(18, 2),
  docusign_status TEXT NOT NULL DEFAULT 'Pending',
  wire_status TEXT NOT NULL DEFAULT 'Awaiting Funds',
  commitment_date TIMESTAMPTZ,
  advisory_fee_pct NUMERIC(6,4),
  wire_sent_at TIMESTAMPTZ,
  wire_sent_adv_pending BOOLEAN NOT NULL DEFAULT FALSE,
  last_followup_at TIMESTAMPTZ,
  kyc_verified BOOLEAN NOT NULL DEFAULT FALSE,
  verbal_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  verbal_confirmed_at TIMESTAMPTZ,
  verbal_confirmed_by TEXT,
  side_letter_pdf_path TEXT,
  side_letter_generated_at TIMESTAMPTZ,
  side_letter_provisions JSONB,
  portal_link_sent_at TIMESTAMPTZ,
  loi_source TEXT,
  trader_id UUID,
  liquidation_required BOOLEAN NOT NULL DEFAULT FALSE,
  liquidation_due_date DATE,
  liquidation_desk_notes TEXT,
  liquidation_acknowledged_at TIMESTAMPTZ,
  liquidation_needed BOOLEAN,
  cash_shortfall NUMERIC(18, 2),
  wire_instructions_extracted JSONB,
  funding_entity_name TEXT,
  funding_entity_matches_kyc BOOLEAN,
  funding_entity_kyc_status TEXT NOT NULL DEFAULT 'not_recorded',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kyc_reviews (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  investor_id UUID REFERENCES investors(id) ON DELETE CASCADE,
  commitment_id UUID REFERENCES commitments(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'Pending',
  source_archive TEXT,
  escalated_to_compliance BOOLEAN NOT NULL DEFAULT FALSE,
  ownership_structure JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS firm_settings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  compliance_email TEXT,
  file_naming_template TEXT DEFAULT 'Sub Docs - {entity_name}',
  docusign_subject_template TEXT,
  default_advisory_fee_pct NUMERIC(6,4) DEFAULT 1.0,
  side_letter_template TEXT,
  distribution_notice_template TEXT,
  adv_brochure_pdf_path TEXT,
  docusign_reviewer_email TEXT,
  ceo_email TEXT,
  docusign_role_names JSONB,
  docusign_w9_template_id TEXT,
  docusign_w8ben_template_id TEXT,
  docusign_w8bene_template_id TEXT,
  platform_base_url TEXT,
  portal_link_expiry_days INTEGER NOT NULL DEFAULT 30,
  portal_brand_tagline TEXT,
  portal_subdomain TEXT,
  portal_visibility JSONB NOT NULL DEFAULT '{
    "show_documents": true,
    "show_capital_account": true,
    "show_distributions": true,
    "show_distribution_history": true,
    "show_loi_opportunities": true,
    "show_wire_change_request": true
  }'::jsonb,
  exec_dashboard_config JSONB NOT NULL DEFAULT '{
    "widgets": [
      {"id": "aip_summary", "enabled": true, "position": 1, "label": "AIP Summary"},
      {"id": "capital_velocity", "enabled": true, "position": 2, "label": "Capital Velocity"},
      {"id": "fund_progress", "enabled": true, "position": 3, "label": "Fund Progress"},
      {"id": "pipeline_health", "enabled": true, "position": 4, "label": "Pipeline Health"},
      {"id": "investor_leaderboard", "enabled": true, "position": 5, "label": "Top Investors"},
      {"id": "ops_pulse", "enabled": true, "position": 6, "label": "Ops Pulse"},
      {"id": "recent_activity", "enabled": true, "position": 7, "label": "Recent Activity"}
    ],
    "thresholds": {
      "stale_subdoc_days": 7,
      "velocity_period": "month",
      "leaderboard_count": 10,
      "activity_count": 20
    },
    "show_advisory_fees": true,
    "show_fund_targets": true
  }'::jsonb,
  notify_statement_mailing_unconfirmed BOOLEAN NOT NULL DEFAULT FALSE,
  notify_ops_fee_expiry BOOLEAN NOT NULL DEFAULT TRUE,
  fee_expiry_alert_days INTEGER NOT NULL DEFAULT 90,
  fee_expiry_followup_style TEXT NOT NULL DEFAULT 'third_party_reminder',
  fee_expiry_custom_instructions TEXT,
  notify_trader_liquidation_digest BOOLEAN NOT NULL DEFAULT FALSE,
  trader_liquidation_alert_days INTEGER NOT NULL DEFAULT 14,
  docusign_toi_template_id TEXT,
  wire_extraction_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  graph_subscription_client_state TEXT,
  kyc_engine TEXT NOT NULL DEFAULT 'openai_vision',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS investor_pending_changes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  investor_id UUID REFERENCES investors(id) ON DELETE CASCADE,
  field_name TEXT,
  old_value JSONB,
  new_value JSONB,
  status TEXT NOT NULL DEFAULT 'Pending',
  source_doc_url TEXT,
  source TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS distributions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  deal_id UUID REFERENCES deals(id) ON DELETE CASCADE,
  distribution_date DATE,
  distribution_type TEXT,
  total_amount NUMERIC(18, 2),
  negative_consent_sent_at TIMESTAMPTZ,
  status TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS distribution_notices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id UUID REFERENCES firms(id) ON DELETE CASCADE,
  distribution_id UUID REFERENCES distributions(id) ON DELETE CASCADE,
  investor_id UUID REFERENCES investors(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'Pending',
  individual_amount NUMERIC(18, 2),
  sent_at TIMESTAMPTZ,
  kyc_verified BOOLEAN NOT NULL DEFAULT FALSE,
  verbal_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  verbal_confirmed_at TIMESTAMPTZ,
  verbal_confirmed_by TEXT,
  negative_consent_sent_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
