-- =============================================================================
-- Dev seed data for Dev RIA (firm_id = 2cf19464-0fb6-460b-a283-09fab02d4ced)
-- Run in Supabase SQL Editor.  Safe to re-run — uses fixed UUIDs + ON CONFLICT DO NOTHING.
-- =============================================================================

DO $$
DECLARE
  v_firm   UUID := '2cf19464-0fb6-460b-a283-09fab02d4ced';

  -- Deals
  v_deal_active1  UUID := 'a1000000-0000-0000-0000-000000000001'; -- Meridian Growth Fund III (active)
  v_deal_active2  UUID := 'a1000000-0000-0000-0000-000000000004'; -- Lakewood Credit Opportunities (active)
  v_deal_closed1  UUID := 'a1000000-0000-0000-0000-000000000002'; -- Sequoia Opportunity Fund II (closed)
  v_deal_closed2  UUID := 'a1000000-0000-0000-0000-000000000003'; -- Harborview Real Estate LP (closed)

  -- Investors (10 total — mix of entity types, KYC states, advisors)
  v_inv1  UUID := 'b1000000-0000-0000-0000-000000000001'; -- Blackwood Family Trust       — Approved
  v_inv2  UUID := 'b1000000-0000-0000-0000-000000000002'; -- Nguyen Capital LLC           — Approved
  v_inv3  UUID := 'b1000000-0000-0000-0000-000000000003'; -- Patricia Okonkwo             — Reviewing
  v_inv4  UUID := 'b1000000-0000-0000-0000-000000000004'; -- Sterling Ridge Partners LP   — Reviewing
  v_inv5  UUID := 'b1000000-0000-0000-0000-000000000005'; -- Dr. Marcus Holt Roth IRA     — Approved
  v_inv6  UUID := 'b1000000-0000-0000-0000-000000000006'; -- Calloway Foundation          — Approved
  v_inv7  UUID := 'b1000000-0000-0000-0000-000000000007'; -- Westport Endowment Partners  — Approved
  v_inv8  UUID := 'b1000000-0000-0000-0000-000000000008'; -- Chen & Associates IRA        — Pending
  v_inv9  UUID := 'b1000000-0000-0000-0000-000000000009'; -- Mirabel Group LLC            — Escalated
  v_inv10 UUID := 'b1000000-0000-0000-0000-000000000010'; -- Aldridge Family Office       — Approved

  -- Commitments — Meridian (active1)
  v_com1  UUID := 'c1000000-0000-0000-0000-000000000001';
  v_com2  UUID := 'c1000000-0000-0000-0000-000000000002';
  v_com3  UUID := 'c1000000-0000-0000-0000-000000000003';
  v_com9  UUID := 'c1000000-0000-0000-0000-000000000009';
  v_com10 UUID := 'c1000000-0000-0000-0000-000000000010';
  v_com11 UUID := 'c1000000-0000-0000-0000-000000000011';
  v_com12 UUID := 'c1000000-0000-0000-0000-000000000012';

  -- Commitments — Lakewood (active2)
  v_com13 UUID := 'c1000000-0000-0000-0000-000000000013';
  v_com14 UUID := 'c1000000-0000-0000-0000-000000000014';
  v_com15 UUID := 'c1000000-0000-0000-0000-000000000015';

  -- Commitments — Sequoia (closed1)
  v_com4  UUID := 'c1000000-0000-0000-0000-000000000004';
  v_com5  UUID := 'c1000000-0000-0000-0000-000000000005';
  v_com6  UUID := 'c1000000-0000-0000-0000-000000000006';

  -- Commitments — Harborview (closed2)
  v_com7  UUID := 'c1000000-0000-0000-0000-000000000007';
  v_com8  UUID := 'c1000000-0000-0000-0000-000000000008';

  -- Distributions
  v_dist1 UUID := 'd1000000-0000-0000-0000-000000000001'; -- Sequoia Q1 2025
  v_dist2 UUID := 'd1000000-0000-0000-0000-000000000002'; -- Sequoia Q3 2024

BEGIN

  -- Ensure closed_at column exists (not in baseline migration)
  ALTER TABLE deals ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;

  -- -------------------------------------------------------------------------
  -- DEALS
  -- -------------------------------------------------------------------------
  INSERT INTO deals (id, firm_id, offering_name, status, target_raise, fund_manager, fund_manager_title, close_date, closed_at, created_at)
  VALUES
    (v_deal_active1, v_firm, 'Meridian Growth Fund III',       'Active', 25000000, 'Jane Porter',   'Managing Partner', '2026-09-30', NULL,                     now() - interval '30 days'),
    (v_deal_active2, v_firm, 'Lakewood Credit Opportunities I', 'Active', 15000000, 'Daniel Cruz',   'Fund Manager',     '2026-12-15', NULL,                     now() - interval '14 days'),
    (v_deal_closed1, v_firm, 'Sequoia Opportunity Fund II',    'Closed', 50000000, 'Robert Huang',  'General Partner',  '2024-12-31', '2024-12-31 17:00:00+00', now() - interval '500 days'),
    (v_deal_closed2, v_firm, 'Harborview Real Estate LP',      'Closed', 18000000, 'Sarah Coleman', 'Fund Manager',     '2023-06-30', '2023-06-30 17:00:00+00', now() - interval '900 days')
  ON CONFLICT (id) DO NOTHING;

  -- -------------------------------------------------------------------------
  -- INVESTORS
  -- -------------------------------------------------------------------------
  INSERT INTO investors (id, firm_id, entity_name, entity_type, primary_email, advisor_email, kyc_status,
                         wire_instructions, handle_with_care, prefers_physical_mail,
                         state_of_formation, country_of_formation, created_at)
  VALUES
    (v_inv1,  v_firm, 'Blackwood Family Trust',       'Trust',               'bft@example.com',        'advisor1@devria.com', 'Approved',   '{"bank":"Chase","account":"****4821","routing":"021000021"}',        false, false, 'DE', 'US', now() - interval '600 days'),
    (v_inv2,  v_firm, 'Nguyen Capital LLC',            'LLC',                 'nguyen@example.com',     'advisor1@devria.com', 'Approved',   '{"bank":"Wells Fargo","account":"****7732","routing":"121000248"}',  false, false, 'CA', 'US', now() - interval '600 days'),
    (v_inv3,  v_firm, 'Patricia Okonkwo',              'Individual',          'p.okonkwo@example.com',  'advisor2@devria.com', 'Reviewing',  '{"bank":"Citi","account":"****3301","routing":"031100209"}',         true,  false, NULL, 'US', now() - interval '12 days'),
    (v_inv4,  v_firm, 'Sterling Ridge Partners LP',    'Limited Partnership', 'srp@example.com',        'advisor2@devria.com', 'Reviewing',  NULL,                                                                false, false, 'NY', 'US', now() - interval '18 days'),
    (v_inv5,  v_firm, 'Dr. Marcus Holt Roth IRA',      'IRA',                 'mholt@example.com',      'advisor1@devria.com', 'Approved',   '{"bank":"Schwab","account":"****9901","routing":"121202211"}',       false, true,  NULL, 'US', now() - interval '550 days'),
    (v_inv6,  v_firm, 'Calloway Foundation',           'Non-Profit',          'giving@calloway.org',    'advisor1@devria.com', 'Approved',   '{"bank":"Bank of America","account":"****2241","routing":"026009593"}', false, false, 'TX', 'US', now() - interval '400 days'),
    (v_inv7,  v_firm, 'Westport Endowment Partners',   'LLC',                 'endowment@westport.com', 'advisor2@devria.com', 'Approved',   '{"bank":"JPMorgan","account":"****8810","routing":"021000021"}',     false, false, 'CT', 'US', now() - interval '350 days'),
    (v_inv8,  v_firm, 'Chen & Associates IRA',          'IRA',                 'chen@example.com',       'advisor2@devria.com', 'Pending',    NULL,                                                                false, false, NULL, 'US', now() - interval '3 days'),
    (v_inv9,  v_firm, 'Mirabel Group LLC',              'LLC',                 'info@mirabelgroup.com',  'advisor1@devria.com', 'Escalated',  NULL,                                                                false, false, 'FL', 'US', now() - interval '8 days'),
    (v_inv10, v_firm, 'Aldridge Family Office',         'Trust',               'afo@aldridge.com',       'advisor1@devria.com', 'Approved',   '{"bank":"Fidelity","account":"****5503","routing":"101205681"}',     false, false, 'IL', 'US', now() - interval '200 days')
  ON CONFLICT (id) DO NOTHING;

  -- -------------------------------------------------------------------------
  -- COMMITMENTS — Meridian Growth Fund III (active)
  -- Target $25M · showing ~$11.25M committed (45%) at various stages
  -- -------------------------------------------------------------------------
  INSERT INTO commitments (id, firm_id, deal_id, investor_id, status,
                           committed_amount, funded_amount, fee_amount, advisory_fee_pct,
                           docusign_status, wire_status, kyc_verified, verbal_confirmed,
                           commitment_date, created_at)
  VALUES
    -- Fully in: wire funded, docs signed
    (v_com1,  v_firm, v_deal_active1, v_inv1,  'Active', 2000000, 2000000, 20000, 1.00, 'Completed', 'Funded',         true,  true,  now() - interval '25 days', now() - interval '25 days'),
    (v_com3,  v_firm, v_deal_active1, v_inv4,  'Active', 1000000, 1000000,     0, 0.00, 'Completed', 'Funded',         false, true,  now() - interval '20 days', now() - interval '20 days'),
    (v_com9,  v_firm, v_deal_active1, v_inv6,  'Active', 3000000, 3000000, 30000, 1.00, 'Completed', 'Funded',         true,  true,  now() - interval '22 days', now() - interval '22 days'),
    (v_com10, v_firm, v_deal_active1, v_inv7,  'Active', 2500000, 2500000, 25000, 1.00, 'Completed', 'Funded',         true,  true,  now() - interval '18 days', now() - interval '18 days'),
    (v_com11, v_firm, v_deal_active1, v_inv10, 'Active', 1500000, 1500000, 15000, 1.00, 'Completed', 'Funded',         true,  true,  now() - interval '10 days', now() - interval '10 days'),
    -- DocuSign sent, wire pending
    (v_com2,  v_firm, v_deal_active1, v_inv3,  'Active',  250000,       0,  2500, 1.00, 'Sent',      'Awaiting Funds', false, false, now() - interval '8 days',  now() - interval '8 days'),
    (v_com12, v_firm, v_deal_active1, v_inv5,  'Active', 1000000,       0, 10000, 1.00, 'Sent',      'Awaiting Funds', true,  true,  now() - interval '5 days',  now() - interval '5 days')
  ON CONFLICT (id) DO NOTHING;

  -- -------------------------------------------------------------------------
  -- COMMITMENTS — Lakewood Credit Opportunities I (active, newer raise)
  -- Target $15M · showing ~$3.5M early commitments
  -- -------------------------------------------------------------------------
  INSERT INTO commitments (id, firm_id, deal_id, investor_id, status,
                           committed_amount, funded_amount, fee_amount, advisory_fee_pct,
                           docusign_status, wire_status, kyc_verified, verbal_confirmed,
                           commitment_date, created_at)
  VALUES
    (v_com13, v_firm, v_deal_active2, v_inv2,  'Active', 1000000, 1000000, 10000, 1.00, 'Completed', 'Funded',         true,  true,  now() - interval '10 days', now() - interval '10 days'),
    (v_com14, v_firm, v_deal_active2, v_inv10, 'Active', 2000000, 2000000, 20000, 1.00, 'Completed', 'Funded',         true,  true,  now() - interval '8 days',  now() - interval '8 days'),
    (v_com15, v_firm, v_deal_active2, v_inv6,  'Active',  500000,       0,  5000, 1.00, 'Sent',      'Awaiting Funds', true,  true,  now() - interval '3 days',  now() - interval '3 days')
  ON CONFLICT (id) DO NOTHING;

  -- -------------------------------------------------------------------------
  -- COMMITMENTS — Sequoia Opportunity Fund II (closed)
  -- -------------------------------------------------------------------------
  INSERT INTO commitments (id, firm_id, deal_id, investor_id, status,
                           committed_amount, funded_amount, fee_amount, advisory_fee_pct,
                           docusign_status, wire_status, kyc_verified, verbal_confirmed,
                           verbal_confirmed_at, commitment_date, created_at)
  VALUES
    (v_com4, v_firm, v_deal_closed1, v_inv1, 'Active', 750000,  750000, 7500, 1.00, 'Completed', 'Funded', true,  true,  now() - interval '490 days', now() - interval '495 days', now()),
    (v_com5, v_firm, v_deal_closed1, v_inv2, 'Active', 300000,  300000, 3000, 1.00, 'Completed', 'Funded', true,  true,  now() - interval '488 days', now() - interval '495 days', now()),
    (v_com6, v_firm, v_deal_closed1, v_inv5, 'Active', 100000,  100000, 1000, 1.00, 'Completed', 'Funded', true,  false, NULL,                         now() - interval '492 days', now())
  ON CONFLICT (id) DO NOTHING;

  -- -------------------------------------------------------------------------
  -- COMMITMENTS — Harborview Real Estate LP (closed)
  -- -------------------------------------------------------------------------
  INSERT INTO commitments (id, firm_id, deal_id, investor_id, status,
                           committed_amount, funded_amount, fee_amount, advisory_fee_pct,
                           docusign_status, wire_status, kyc_verified, verbal_confirmed,
                           verbal_confirmed_at, commitment_date, created_at)
  VALUES
    (v_com7, v_firm, v_deal_closed2, v_inv1, 'Active', 200000, 200000, 2000, 1.00, 'Completed', 'Funded', true,  true, now() - interval '890 days', now() - interval '895 days', now()),
    (v_com8, v_firm, v_deal_closed2, v_inv2, 'Active', 150000, 150000, 1500, 1.00, 'Completed', 'Funded', true,  true, now() - interval '888 days', now() - interval '895 days', now())
  ON CONFLICT (id) DO NOTHING;

  -- -------------------------------------------------------------------------
  -- DISTRIBUTIONS — Sequoia: two quarters
  -- -------------------------------------------------------------------------
  INSERT INTO distributions (id, firm_id, deal_id, distribution_date, distribution_type, total_amount, status, created_at)
  VALUES
    (v_dist1, v_firm, v_deal_closed1, '2025-03-15', 'Quarterly',         85000, 'Sent', now() - interval '60 days'),
    (v_dist2, v_firm, v_deal_closed1, '2024-09-30', 'Return of Capital', 50000, 'Sent', now() - interval '240 days')
  ON CONFLICT (id) DO NOTHING;

  -- Distribution notices — Q1 2025
  INSERT INTO distribution_notices (firm_id, distribution_id, investor_id, status, individual_amount, sent_at, kyc_verified, verbal_confirmed)
  SELECT v_firm, v_dist1, v_inv1, 'Sent', 55769.23, now() - interval '59 days', true, true
  WHERE NOT EXISTS (SELECT 1 FROM distribution_notices WHERE distribution_id = v_dist1 AND investor_id = v_inv1);

  INSERT INTO distribution_notices (firm_id, distribution_id, investor_id, status, individual_amount, sent_at, kyc_verified, verbal_confirmed)
  SELECT v_firm, v_dist1, v_inv2, 'Sent', 22307.69, now() - interval '59 days', true, true
  WHERE NOT EXISTS (SELECT 1 FROM distribution_notices WHERE distribution_id = v_dist1 AND investor_id = v_inv2);

  INSERT INTO distribution_notices (firm_id, distribution_id, investor_id, status, individual_amount, sent_at, kyc_verified, verbal_confirmed)
  SELECT v_firm, v_dist1, v_inv5, 'Sent',  6923.08, now() - interval '59 days', true, false
  WHERE NOT EXISTS (SELECT 1 FROM distribution_notices WHERE distribution_id = v_dist1 AND investor_id = v_inv5);

  -- Distribution notices — Q3 2024 (Return of Capital)
  INSERT INTO distribution_notices (firm_id, distribution_id, investor_id, status, individual_amount, sent_at, kyc_verified, verbal_confirmed)
  SELECT v_firm, v_dist2, v_inv1, 'Sent', 32692.31, now() - interval '239 days', true, true
  WHERE NOT EXISTS (SELECT 1 FROM distribution_notices WHERE distribution_id = v_dist2 AND investor_id = v_inv1);

  INSERT INTO distribution_notices (firm_id, distribution_id, investor_id, status, individual_amount, sent_at, kyc_verified, verbal_confirmed)
  SELECT v_firm, v_dist2, v_inv2, 'Sent', 13076.92, now() - interval '239 days', true, true
  WHERE NOT EXISTS (SELECT 1 FROM distribution_notices WHERE distribution_id = v_dist2 AND investor_id = v_inv2);

  INSERT INTO distribution_notices (firm_id, distribution_id, investor_id, status, individual_amount, sent_at, kyc_verified, verbal_confirmed)
  SELECT v_firm, v_dist2, v_inv5, 'Sent',  4230.77, now() - interval '239 days', true, false
  WHERE NOT EXISTS (SELECT 1 FROM distribution_notices WHERE distribution_id = v_dist2 AND investor_id = v_inv5);

  -- -------------------------------------------------------------------------
  -- KYC REVIEWS
  -- -------------------------------------------------------------------------
  INSERT INTO kyc_reviews (firm_id, investor_id, status, matched_docs, nested_entities, signatories, flags, ownership_structure, escalated_to_compliance)
  SELECT v_firm, v_inv3, 'Reviewing',
    '["Trust Certificate","Passport Copy"]'::jsonb,
    '[{"name":"Nguyen Family Trust","type":"Trust","role":"Beneficiary","requires_kyc":true}]'::jsonb,
    '[{"name":"Patricia Okonkwo","title":"Trustee","qualified_to_sign":true}]'::jsonb,
    '["Missing EIN documentation","Signatory title unclear"]'::jsonb,
    '{"type":"Individual","is_joint_tenancy":false}'::jsonb,
    false
  WHERE NOT EXISTS (SELECT 1 FROM kyc_reviews WHERE firm_id = v_firm AND investor_id = v_inv3);

  INSERT INTO kyc_reviews (firm_id, investor_id, status, matched_docs, nested_entities, signatories, flags, ownership_structure, escalated_to_compliance)
  SELECT v_firm, v_inv4, 'Pending',
    '["Limited Partnership Agreement","Certificate of Limited Partnership"]'::jsonb,
    '[{"name":"Sterling Ridge GP LLC","type":"LLC","role":"General Partner","requires_kyc":true}]'::jsonb,
    '[{"name":"David Sterling","title":"Managing Member","qualified_to_sign":true},{"name":"Anne Ridge","title":"CFO","qualified_to_sign":false}]'::jsonb,
    '["Ownership structure exceeds 20% threshold — beneficial owner forms required"]'::jsonb,
    '{"type":"Limited Partnership","is_joint_tenancy":false}'::jsonb,
    false
  WHERE NOT EXISTS (SELECT 1 FROM kyc_reviews WHERE firm_id = v_firm AND investor_id = v_inv4);

  INSERT INTO kyc_reviews (firm_id, investor_id, status, matched_docs, nested_entities, signatories, flags, ownership_structure, escalated_to_compliance)
  SELECT v_firm, v_inv9, 'Reviewing',
    '["Articles of Organization"]'::jsonb,
    '[{"name":"Offshore Holdings Ltd","type":"Corporation","role":"Majority Member","requires_kyc":true}]'::jsonb,
    '[{"name":"Rafael Mirabel","title":"Manager","qualified_to_sign":true}]'::jsonb,
    '["Foreign beneficial owner — FATCA documentation required","UBO identification incomplete"]'::jsonb,
    '{"type":"LLC","is_joint_tenancy":false}'::jsonb,
    true
  WHERE NOT EXISTS (SELECT 1 FROM kyc_reviews WHERE firm_id = v_firm AND investor_id = v_inv9);

END $$;
