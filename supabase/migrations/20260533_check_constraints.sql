-- Guarded check constraints for institutional status and amount invariants.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'chk_docusign_status'
      AND conrelid = 'public.commitments'::regclass
  ) THEN
    ALTER TABLE commitments
      ADD CONSTRAINT chk_docusign_status
      CHECK (docusign_status IN ('Pending','Sent','Signed','Completed','Pending Countersign','Voided','Declined'));
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'chk_wire_status'
      AND conrelid = 'public.commitments'::regclass
  ) THEN
    ALTER TABLE commitments
      ADD CONSTRAINT chk_wire_status
      CHECK (wire_status IN ('Awaiting Funds','Wire Sent','Funded','Partial','Refunded'));
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'chk_kyc_status'
      AND conrelid = 'public.investors'::regclass
  ) THEN
    ALTER TABLE investors
      ADD CONSTRAINT chk_kyc_status
      CHECK (kyc_status IN ('Pending','Reviewing','Escalated','Approved','Rejected','Complete'));
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'chk_committed_amount'
      AND conrelid = 'public.commitments'::regclass
  ) THEN
    ALTER TABLE commitments
      ADD CONSTRAINT chk_committed_amount
      CHECK (committed_amount >= 0);
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'chk_funded_amount'
      AND conrelid = 'public.commitments'::regclass
  ) THEN
    ALTER TABLE commitments
      ADD CONSTRAINT chk_funded_amount
      CHECK (funded_amount IS NULL OR funded_amount >= 0);
  END IF;
END;
$$;
