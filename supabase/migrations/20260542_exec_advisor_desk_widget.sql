-- Ensure advisor_desk_health widget exists in saved exec_dashboard_config for existing firms.
-- Code default includes it at position 5; phase7 migration omitted it.

UPDATE firm_settings
SET exec_dashboard_config = jsonb_set(
  exec_dashboard_config,
  '{widgets}',
  (
    SELECT COALESCE(
      jsonb_agg(
        CASE
          WHEN w->>'id' = 'investor_leaderboard' THEN jsonb_set(w, '{position}', '6')
          WHEN w->>'id' = 'ops_pulse' THEN jsonb_set(w, '{position}', '7')
          WHEN w->>'id' = 'recent_activity' THEN jsonb_set(w, '{position}', '8')
          ELSE w
        END
        ORDER BY (w->>'position')::int
      ),
      '[]'::jsonb
    )
    FROM jsonb_array_elements(exec_dashboard_config->'widgets') AS w
    WHERE w->>'id' <> 'advisor_desk_health'
  )
  || '[{"id": "advisor_desk_health", "enabled": true, "position": 5, "label": "Advisor Desk Health"}]'::jsonb
)
WHERE NOT EXISTS (
  SELECT 1
  FROM jsonb_array_elements(exec_dashboard_config->'widgets') AS w
  WHERE w->>'id' = 'advisor_desk_health'
);
