-- Phase 5c: Ops To-Do Dashboard + Portal Visibility Controls

-- investors: add phone for ops verbal verification workflow
ALTER TABLE investors
  ADD COLUMN IF NOT EXISTS phone TEXT;

-- firm_settings: per-section portal visibility toggles
-- All default to true so existing firms without this setting see everything
ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS portal_visibility JSONB NOT NULL DEFAULT '{
    "show_documents": true,
    "show_capital_account": true,
    "show_distributions": true,
    "show_distribution_history": true,
    "show_loi_opportunities": true,
    "show_wire_change_request": true
  }'::jsonb;

-- Index for fast ops to-do queries
CREATE INDEX IF NOT EXISTS idx_pending_changes_wire
  ON investor_pending_changes(firm_id, field_name, status)
  WHERE status = 'Pending';

CREATE INDEX IF NOT EXISTS idx_commitments_stale_subdocs
  ON commitments(firm_id, docusign_status, created_at)
  WHERE docusign_status = 'Sent';
