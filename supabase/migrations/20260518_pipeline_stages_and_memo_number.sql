-- 20260518_pipeline_stages_and_memo_number.sql
--
-- 1. Fix memorandum_number: change from TEXT to auto-assigned SMALLINT (1–99 per deal)
-- 2. Add stage_override to commitments for manual exception states
-- 3. Add pipeline_stages config to firm_settings (firm-customizable, with defaults)

-- ---------------------------------------------------------------------------
-- 1. Memorandum number — auto-assigned sequential integer per deal
-- ---------------------------------------------------------------------------

-- Drop the unique constraint added in the previous migration (wrong approach —
-- a trigger handles uniqueness automatically via sequential assignment)
ALTER TABLE commitments
  DROP CONSTRAINT IF EXISTS uq_commitments_firm_memorandum;

-- Re-type the column: TEXT → SMALLINT (fits 1–99 comfortably, 1–32767 max)
ALTER TABLE commitments
  DROP COLUMN IF EXISTS memorandum_number;

ALTER TABLE commitments
  ADD COLUMN memorandum_number SMALLINT;

-- Unique per deal (not just per firm): John is #1 in Fund A AND #1 in Fund B
CREATE UNIQUE INDEX IF NOT EXISTS uq_commitments_deal_memorandum
  ON commitments(deal_id, memorandum_number)
  WHERE memorandum_number IS NOT NULL;

-- Trigger function: assign the next integer for this deal at INSERT time.
-- Does nothing if memorandum_number is already set (allows manual override if
-- ops ever needs to renumber).
CREATE OR REPLACE FUNCTION assign_memorandum_number()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.memorandum_number IS NULL THEN
    SELECT COALESCE(MAX(memorandum_number), 0) + 1
      INTO NEW.memorandum_number
      FROM commitments
     WHERE deal_id = NEW.deal_id;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_assign_memorandum_number ON commitments;
CREATE TRIGGER trg_assign_memorandum_number
  BEFORE INSERT ON commitments
  FOR EACH ROW
  EXECUTE FUNCTION assign_memorandum_number();

-- ---------------------------------------------------------------------------
-- 2. Stage override — manual exception states ops can apply to a commitment
-- ---------------------------------------------------------------------------

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS stage_override TEXT;

-- Allowed override values
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'chk_stage_override'
  ) THEN
    ALTER TABLE commitments
      ADD CONSTRAINT chk_stage_override
      CHECK (stage_override IS NULL OR stage_override IN (
        'On Hold',
        'Paused',
        'Withdrawn',
        'Pending Advisor Response',
        'Pending Compliance',
        'Cancelled'
      ));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_commitments_stage_override
  ON commitments(deal_id)
  WHERE stage_override IS NOT NULL AND deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- 3. Pipeline stages — firm-customizable, stored in firm_settings
-- ---------------------------------------------------------------------------

-- Default stage list. Each stage has:
--   key        — internal identifier used by the backend compute logic
--   label      — what the ops team sees (firm can rename this)
--   color      — Tailwind color token for the badge/progress bar
--   terminal   — true if reaching this stage ends the active pipeline (funded, withdrawn)
--   order      — display position (lower = earlier in the process)
--
-- Firms can rename labels and reorder, but cannot remove the terminal stages
-- (funded, withdrawn) as they affect billing and reporting.

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS pipeline_stages JSONB NOT NULL DEFAULT '[
    {
      "key": "awaiting_subdocs",
      "label": "Awaiting Sub Docs",
      "color": "zinc",
      "terminal": false,
      "order": 1
    },
    {
      "key": "out_for_signature",
      "label": "Out for Signature",
      "color": "blue",
      "terminal": false,
      "order": 2
    },
    {
      "key": "waiting_kyc",
      "label": "Waiting on KYC",
      "color": "amber",
      "terminal": false,
      "order": 3
    },
    {
      "key": "compliance_review",
      "label": "Compliance Review",
      "color": "purple",
      "terminal": false,
      "order": 4
    },
    {
      "key": "wire_instructions_needed",
      "label": "Wire Instructions Needed",
      "color": "orange",
      "terminal": false,
      "order": 5
    },
    {
      "key": "wire_pending",
      "label": "Wire Pending",
      "color": "indigo",
      "terminal": false,
      "order": 6
    },
    {
      "key": "funded",
      "label": "Funded",
      "color": "emerald",
      "terminal": true,
      "order": 7
    }
  ]'::jsonb;
