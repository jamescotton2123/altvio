-- Orion household fuzzy-match review queue: investor status fields + candidate store.

ALTER TABLE investors
  ADD COLUMN IF NOT EXISTS orion_id              TEXT,
  ADD COLUMN IF NOT EXISTS orion_household_name  TEXT,
  ADD COLUMN IF NOT EXISTS orion_match_status    TEXT,
  ADD COLUMN IF NOT EXISTS orion_review_notes    TEXT;

CREATE INDEX IF NOT EXISTS idx_investors_orion_match_status
  ON investors(firm_id, orion_match_status)
  WHERE orion_match_status IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS orion_match_candidates (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firm_id                  UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
  investor_id              UUID NOT NULL REFERENCES investors(id) ON DELETE CASCADE,
  candidates               JSONB NOT NULL DEFAULT '[]'::jsonb,
  status                   TEXT NOT NULL DEFAULT 'Pending',
  confirmed_household_name TEXT,
  reviewed_by              TEXT,
  reviewed_at              TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (investor_id)
);

CREATE INDEX IF NOT EXISTS idx_orion_match_candidates_firm
  ON orion_match_candidates(firm_id, status);
