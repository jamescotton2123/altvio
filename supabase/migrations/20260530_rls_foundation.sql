-- Application role for per-firm scoped queries
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_firm_user') THEN
    CREATE ROLE app_firm_user NOLOGIN;
  END IF;
END
$$;
GRANT USAGE ON SCHEMA public TO app_firm_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO app_firm_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO app_firm_user;

-- Reads current_firm_id from the session GUC set by the Python app per request.
CREATE OR REPLACE FUNCTION current_firm_id() RETURNS UUID
  LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.current_firm_id', true), '')::uuid $$;
