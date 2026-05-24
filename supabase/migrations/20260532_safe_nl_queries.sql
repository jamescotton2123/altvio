-- Phase 0g: replace dynamic NL SQL execution with parameterized, invoker-mode RPCs.

CREATE OR REPLACE FUNCTION query_investor_kyc_status_counts(
  p_firm_id UUID,
  p_kyc_status TEXT DEFAULT NULL
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      i.kyc_status,
      COUNT(*)::INTEGER AS investor_count
    FROM investors i
    WHERE i.firm_id = p_firm_id
      AND (p_kyc_status IS NULL OR lower(i.kyc_status) = lower(p_kyc_status))
    GROUP BY i.kyc_status
    ORDER BY investor_count DESC, i.kyc_status
  ) q;
$$;

CREATE OR REPLACE FUNCTION query_commitments_by_status(
  p_firm_id UUID,
  p_status TEXT,
  p_limit INTEGER DEFAULT 100
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      c.id,
      c.status,
      c.committed_amount,
      c.funded_amount,
      c.commitment_date,
      i.entity_name AS investor_name,
      d.offering_name
    FROM commitments c
    LEFT JOIN investors i ON i.id = c.investor_id AND i.firm_id = c.firm_id
    LEFT JOIN deals d ON d.id = c.deal_id AND d.firm_id = c.firm_id
    WHERE c.firm_id = p_firm_id
      AND lower(c.status) = lower(p_status)
    ORDER BY c.commitment_date DESC NULLS LAST, c.id
    LIMIT greatest(1, least(coalesce(p_limit, 100), 500))
  ) q;
$$;

CREATE OR REPLACE FUNCTION query_commitment_funding_status(
  p_firm_id UUID,
  p_wire_status TEXT,
  p_limit INTEGER DEFAULT 100
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      c.id,
      c.wire_status,
      c.committed_amount,
      c.funded_amount,
      i.entity_name AS investor_name,
      d.offering_name
    FROM commitments c
    LEFT JOIN investors i ON i.id = c.investor_id AND i.firm_id = c.firm_id
    LEFT JOIN deals d ON d.id = c.deal_id AND d.firm_id = c.firm_id
    WHERE c.firm_id = p_firm_id
      AND lower(c.wire_status) = lower(p_wire_status)
    ORDER BY c.commitment_date DESC NULLS LAST, c.id
    LIMIT greatest(1, least(coalesce(p_limit, 100), 500))
  ) q;
$$;

CREATE OR REPLACE FUNCTION query_commitments_by_docusign_status(
  p_firm_id UUID,
  p_docusign_status TEXT,
  p_limit INTEGER DEFAULT 100
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      c.id,
      c.docusign_status,
      c.committed_amount,
      c.funded_amount,
      i.entity_name AS investor_name,
      d.offering_name
    FROM commitments c
    LEFT JOIN investors i ON i.id = c.investor_id AND i.firm_id = c.firm_id
    LEFT JOIN deals d ON d.id = c.deal_id AND d.firm_id = c.firm_id
    WHERE c.firm_id = p_firm_id
      AND lower(c.docusign_status) = lower(p_docusign_status)
    ORDER BY c.commitment_date DESC NULLS LAST, c.id
    LIMIT greatest(1, least(coalesce(p_limit, 100), 500))
  ) q;
$$;

CREATE OR REPLACE FUNCTION query_aum_by_advisor(
  p_firm_id UUID,
  p_advisor_email TEXT DEFAULT NULL
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      i.advisor_email,
      COUNT(DISTINCT i.id)::INTEGER AS investor_count,
      COUNT(c.id)::INTEGER AS commitment_count,
      COALESCE(SUM(c.committed_amount), 0) AS total_committed,
      COALESCE(SUM(c.funded_amount), 0) AS total_funded,
      COALESCE(SUM(c.fee_amount), 0) AS total_fees
    FROM investors i
    LEFT JOIN commitments c ON c.investor_id = i.id AND c.firm_id = i.firm_id
    WHERE i.firm_id = p_firm_id
      AND (p_advisor_email IS NULL OR lower(i.advisor_email) = lower(p_advisor_email))
    GROUP BY i.advisor_email
    ORDER BY total_committed DESC, i.advisor_email
  ) q;
$$;

CREATE OR REPLACE FUNCTION query_deal_raise_progress(
  p_firm_id UUID,
  p_deal_id UUID DEFAULT NULL,
  p_offering_name TEXT DEFAULT NULL
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      d.id,
      d.offering_name,
      d.status,
      d.close_date,
      d.fund_manager,
      d.target_raise,
      COALESCE(SUM(c.committed_amount), 0) AS total_committed,
      COALESCE(SUM(c.funded_amount), 0) AS total_funded,
      CASE
        WHEN COALESCE(d.target_raise, 0) = 0 THEN NULL
        ELSE ROUND(((COALESCE(SUM(c.committed_amount), 0) / d.target_raise) * 100)::numeric, 2)
      END AS pct_of_target
    FROM deals d
    LEFT JOIN commitments c ON c.deal_id = d.id AND c.firm_id = d.firm_id
    WHERE d.firm_id = p_firm_id
      AND (p_deal_id IS NULL OR d.id = p_deal_id)
      AND (p_offering_name IS NULL OR d.offering_name ILIKE '%' || p_offering_name || '%')
    GROUP BY d.id, d.offering_name, d.status, d.close_date, d.fund_manager, d.target_raise
    ORDER BY d.close_date DESC NULLS LAST, d.offering_name
  ) q;
$$;

CREATE OR REPLACE FUNCTION query_distribution_notices(
  p_firm_id UUID,
  p_status TEXT DEFAULT NULL,
  p_deal_id UUID DEFAULT NULL,
  p_limit INTEGER DEFAULT 100
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      dn.id,
      dn.status,
      dn.individual_amount,
      dn.sent_at,
      i.entity_name AS investor_name,
      d.distribution_date,
      d.distribution_type,
      deals.offering_name
    FROM distribution_notices dn
    JOIN distributions d ON d.id = dn.distribution_id AND d.firm_id = dn.firm_id
    LEFT JOIN deals ON deals.id = d.deal_id AND deals.firm_id = d.firm_id
    LEFT JOIN investors i ON i.id = dn.investor_id AND i.firm_id = dn.firm_id
    WHERE dn.firm_id = p_firm_id
      AND (p_status IS NULL OR lower(dn.status) = lower(p_status))
      AND (p_deal_id IS NULL OR d.deal_id = p_deal_id)
    ORDER BY d.distribution_date DESC NULLS LAST, dn.sent_at DESC NULLS LAST, dn.id
    LIMIT greatest(1, least(coalesce(p_limit, 100), 500))
  ) q;
$$;

CREATE OR REPLACE FUNCTION query_handle_with_care_investors(
  p_firm_id UUID,
  p_limit INTEGER DEFAULT 100
)
RETURNS SETOF json
LANGUAGE sql
STABLE
AS $$
  SELECT row_to_json(q)
  FROM (
    SELECT
      i.id,
      i.entity_name,
      i.entity_type,
      i.kyc_status,
      i.primary_email,
      i.advisor_email,
      i.handle_with_care
    FROM investors i
    WHERE i.firm_id = p_firm_id
      AND i.handle_with_care IS TRUE
    ORDER BY i.entity_name
    LIMIT greatest(1, least(coalesce(p_limit, 100), 500))
  ) q;
$$;

GRANT EXECUTE ON FUNCTION query_investor_kyc_status_counts(UUID, TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION query_commitments_by_status(UUID, TEXT, INTEGER) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION query_commitment_funding_status(UUID, TEXT, INTEGER) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION query_commitments_by_docusign_status(UUID, TEXT, INTEGER) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION query_aum_by_advisor(UUID, TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION query_deal_raise_progress(UUID, UUID, TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION query_distribution_notices(UUID, TEXT, UUID, INTEGER) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION query_handle_with_care_investors(UUID, INTEGER) TO authenticated, service_role;

DO $$
BEGIN
  IF to_regprocedure('public.run_safe_query(text)') IS NOT NULL THEN
    REVOKE EXECUTE ON FUNCTION run_safe_query(TEXT) FROM service_role;
    REVOKE ALL ON FUNCTION run_safe_query(TEXT) FROM PUBLIC;
    DROP FUNCTION run_safe_query(TEXT);
  END IF;
END;
$$;
