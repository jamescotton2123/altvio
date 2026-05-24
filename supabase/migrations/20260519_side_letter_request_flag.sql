-- 20260519_side_letter_request_flag.sql
--
-- Advisors flag at intake whether the investor needs a side letter so ops
-- can route it to the Side Letter Orchestrator before sub docs go out.
-- This is the REQUEST flag — the actual generated PDF still lives on
-- side_letter_pdf_path (added in 20260513_phase4.sql).

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS side_letter_requested BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS side_letter_terms     TEXT;

CREATE INDEX IF NOT EXISTS idx_commitments_side_letter_pending
  ON commitments(deal_id)
  WHERE side_letter_requested = TRUE
    AND side_letter_pdf_path IS NULL
    AND deleted_at IS NULL;
