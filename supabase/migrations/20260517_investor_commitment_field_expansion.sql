-- 20260517_investor_commitment_field_expansion.sql
-- Expand investors + commitments to capture every field the ops team actually
-- needs for Orion NAImport, joint accounts, interested parties, and customer
-- contact preferences. Backfills three fields (mailing_address, tax_id,
-- date_of_birth) that were referenced throughout the codebase but never
-- defined in any prior migration.

-- ---------------------------------------------------------------------------
-- investors: identity, contact, and householding additions
-- ---------------------------------------------------------------------------

ALTER TABLE investors
  -- Identity (these were referenced everywhere but never defined)
  ADD COLUMN IF NOT EXISTS tax_id              TEXT,
  ADD COLUMN IF NOT EXISTS tax_id_type         TEXT,   -- SSN | EIN | ITIN | Foreign
  ADD COLUMN IF NOT EXISTS mailing_address     TEXT,
  ADD COLUMN IF NOT EXISTS date_of_birth       DATE,

  -- Joint / trust / entity co-clients (Client 1 + Client 2)
  ADD COLUMN IF NOT EXISTS client_one_name     TEXT,
  ADD COLUMN IF NOT EXISTS client_one_email    TEXT,
  ADD COLUMN IF NOT EXISTS client_one_phone    TEXT,
  ADD COLUMN IF NOT EXISTS client_one_dob      DATE,
  ADD COLUMN IF NOT EXISTS client_one_ssn_last4 TEXT,

  ADD COLUMN IF NOT EXISTS client_two_name     TEXT,
  ADD COLUMN IF NOT EXISTS client_two_email    TEXT,
  ADD COLUMN IF NOT EXISTS client_two_phone    TEXT,
  ADD COLUMN IF NOT EXISTS client_two_dob      DATE,
  ADD COLUMN IF NOT EXISTS client_two_ssn_last4 TEXT,

  -- Interested parties (CPA, attorney, family office, etc.)
  -- Shape: [{ "name": str, "email": str, "phone": str|null, "role": str, "receives_statements": bool }]
  ADD COLUMN IF NOT EXISTS interested_parties  JSONB NOT NULL DEFAULT '[]'::jsonb,

  -- Communication preferences
  ADD COLUMN IF NOT EXISTS preferred_contact_method TEXT,  -- email | phone | mail | advisor_only
  ADD COLUMN IF NOT EXISTS no_electronic_access     BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS do_not_contact           BOOLEAN NOT NULL DEFAULT FALSE,

  -- Compliance
  ADD COLUMN IF NOT EXISTS accredited_investor      BOOLEAN,
  ADD COLUMN IF NOT EXISTS accredited_verified_at   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS qualified_purchaser      BOOLEAN,
  ADD COLUMN IF NOT EXISTS qualified_purchaser_verified_at TIMESTAMPTZ,

  -- Ops notes (free-form, separate from sensitivity_notes which is for handle_with_care context)
  ADD COLUMN IF NOT EXISTS internal_notes      TEXT;

CREATE INDEX IF NOT EXISTS idx_investors_tax_id
  ON investors(firm_id, tax_id)
  WHERE tax_id IS NOT NULL AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_investors_do_not_contact
  ON investors(firm_id)
  WHERE do_not_contact = TRUE AND deleted_at IS NULL;

-- Guard rail: tax_id_type must be one of the standard values when set
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'chk_tax_id_type'
  ) THEN
    ALTER TABLE investors
      ADD CONSTRAINT chk_tax_id_type
      CHECK (tax_id_type IS NULL OR tax_id_type IN ('SSN','EIN','ITIN','Foreign'));
  END IF;
END $$;

-- Guard rail: preferred_contact_method must be one of the standard values when set
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'chk_preferred_contact_method'
  ) THEN
    ALTER TABLE investors
      ADD CONSTRAINT chk_preferred_contact_method
      CHECK (preferred_contact_method IS NULL OR preferred_contact_method IN ('email','phone','mail','advisor_only'));
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- commitments: deal-specific reference numbers + ops notes
-- ---------------------------------------------------------------------------

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS memorandum_number   TEXT,
  ADD COLUMN IF NOT EXISTS internal_notes      TEXT;

CREATE INDEX IF NOT EXISTS idx_commitments_memorandum
  ON commitments(firm_id, memorandum_number)
  WHERE memorandum_number IS NOT NULL AND deleted_at IS NULL;

-- Unique per firm: prevents two commitments accidentally sharing the same memo number
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'uq_commitments_firm_memorandum'
  ) THEN
    ALTER TABLE commitments
      ADD CONSTRAINT uq_commitments_firm_memorandum
      UNIQUE (firm_id, memorandum_number);
  END IF;
END $$;
