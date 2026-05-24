-- Phase 7: Dashboard & Portal Customization

-- Executive Command Center widget config
ALTER TABLE firm_settings
  ADD COLUMN IF NOT EXISTS exec_dashboard_config JSONB NOT NULL DEFAULT '{
    "widgets": [
      {"id": "aip_summary",          "enabled": true, "position": 1, "label": "AIP Summary"},
      {"id": "capital_velocity",     "enabled": true, "position": 2, "label": "Capital Velocity"},
      {"id": "fund_progress",        "enabled": true, "position": 3, "label": "Fund Progress"},
      {"id": "pipeline_health",      "enabled": true, "position": 4, "label": "Pipeline Health"},
      {"id": "investor_leaderboard", "enabled": true, "position": 5, "label": "Top Investors"},
      {"id": "ops_pulse",            "enabled": true, "position": 6, "label": "Ops Pulse"},
      {"id": "recent_activity",      "enabled": true, "position": 7, "label": "Recent Activity"}
    ],
    "thresholds": {
      "stale_subdoc_days": 7,
      "velocity_period": "month",
      "leaderboard_count": 10,
      "activity_count": 20
    },
    "show_advisory_fees": true,
    "show_fund_targets": true
  }'::jsonb;

-- Per-advisor client list preferences
ALTER TABLE advisors
  ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{
    "client_sort": "committed_desc",
    "default_filter": "all",
    "show_dollar_amounts": true,
    "show_columns": ["entity_name", "kyc_status", "total_committed", "overall_status", "wire_on_file", "fund_count"]
  }'::jsonb;
