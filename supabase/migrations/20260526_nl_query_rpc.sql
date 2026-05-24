-- Read-only SQL execution for NL query agent (service_role only; caller validates SELECT-only).

CREATE OR REPLACE FUNCTION run_safe_query(sql TEXT)
RETURNS SETOF json
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  -- Caller is responsible for ensuring sql is SELECT-only.
  RETURN QUERY EXECUTE sql;
END;
$$;

REVOKE ALL ON FUNCTION run_safe_query(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION run_safe_query(TEXT) TO service_role;

COMMENT ON FUNCTION run_safe_query IS
  'Executes a validated SELECT for Altvio NL query. Not exposed to anon/authenticated roles.';
