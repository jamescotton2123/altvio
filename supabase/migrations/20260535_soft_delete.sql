ALTER TABLE investors ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_investors_not_deleted ON investors(firm_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_commitments_not_deleted ON commitments(firm_id) WHERE deleted_at IS NULL;
