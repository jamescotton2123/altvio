-- Optional weekly ops alerts for physical mailings not confirmed received (30+ days after mailed_date).
-- Default false: firms that only log mailings and do not chase confirmations see no automated emails.

ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS notify_statement_mailing_unconfirmed BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN firm_settings.notify_statement_mailing_unconfirmed IS
  'When true, Monday 08:00 job emails ops_mailbox for each statement_mailings row still unconfirmed 30+ days after mailed_date.';
