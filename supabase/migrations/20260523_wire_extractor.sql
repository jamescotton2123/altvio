-- Wire instructions extraction from signed subscription PDFs (GPT-4o vision).

ALTER TABLE commitments
  ADD COLUMN IF NOT EXISTS wire_instructions_extracted JSONB;

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS wire_extraction_enabled BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN commitments.wire_instructions_extracted IS
  'Last wire extraction run from sub-doc PDF: extracted blob + action + discrepancy flag.';
COMMENT ON COLUMN firm_settings.wire_extraction_enabled IS
  'When true, envelope-completed webhook runs extract_wire_from_pdf after Email 2.';
