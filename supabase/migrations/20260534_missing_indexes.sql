CREATE INDEX IF NOT EXISTS idx_kyc_reviews_firm_status ON kyc_reviews(firm_id, status);
CREATE INDEX IF NOT EXISTS idx_deals_firm_status ON deals(firm_id, status);
CREATE INDEX IF NOT EXISTS idx_investors_firm_email ON investors(firm_id, lower(primary_email));
CREATE INDEX IF NOT EXISTS idx_commitments_firm_investor ON commitments(firm_id, investor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_firm_action ON audit_logs(firm_id, action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_invocations_firm_engine ON ai_invocations(firm_id, engine, created_at DESC);
