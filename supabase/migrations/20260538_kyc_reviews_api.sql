-- Parser / ops API fields on kyc_reviews (baseline only had status + ownership_structure).
ALTER TABLE kyc_reviews
  ADD COLUMN IF NOT EXISTS matched_docs       JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS nested_entities    JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS signatories        JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS flags              JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS formation_date     DATE;

-- Align privileges with other firm-scoped tables (investors, commitments, …).
-- service_role bypasses RLS but still needs table-level privileges in Supabase.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.kyc_reviews TO anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.kyc_reviews TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.kyc_reviews TO service_role;
