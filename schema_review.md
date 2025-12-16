to-brasil/atracai # echo "===== schema_review.json ====="
cat schema_review.json
===== schema_review.json =====
{
  "summary": "Current schema is adequate for STS v1 deployment but requires additive changes for censored data, auditability, and enhanced ML features. No breaking migrations needed.",
  "required_schema_changes": [
    {
      "table": "port_calls_multiport",
      "change_type": "ADD COLUMN",
      "statement": "ALTER TABLE port_calls_multiport ADD COLUMN censoring_flag BOOLEAN DEFAULT FALSE;",
      "breaking": false,
      "rationale": "Flag right-censored calls (berth_start_utc IS NULL) for survival analysis in label building."
    },
    {
      "table": "ml_training_samples_multiport",
      "change_type": "ADD COLUMN",
      "statement": "ALTER TABLE ml_training_samples_multiport ADD COLUMN censored BOOLEAN DEFAULT FALSE;",
      "breaking": false,
      "rationale": "Indicate censored labels for model training (e.g., vessels still waiting)."
    }
  ],
  "optional_schema_changes": [
    {
      "table": "vessel_info",
      "change_type": "ADD COLUMN",
      "statement": "ALTER TABLE vessel_info ADD COLUMN operator TEXT, ADD COLUMN flag TEXT, ADD COLUMN last_port_call_utc TIMESTAMPTZ;",
      "breaking": false,
      "rationale": "Enrich ML features with vessel operational metadata."
    },
    {
      "table": "predictions_log",
      "change_type": "NEW TABLE",
      "statement": "CREATE TABLE predictions_log (id BIGSERIAL PRIMARY KEY, port_code TEXT NOT NULL, mmsi TEXT NOT NULL, prediction_time_utc TIMESTAMPTZ NOT NULL, model_version TEXT, predicted_p50_hours FLOAT, predicted_p90_hours FLOAT, actual_hours FLOAT, coverage_90 BOOLEAN, created_at TIMESTAMPTZ DEFAULT NOW());",
      "breaking": false,
      "rationale": "Enterprise auditability for real-time predictions and demurrage risk tracking."
    }
  ],
  "indexes_recommended": [
    {
      "table": "ml_training_samples_multiport",
      "statement": "CREATE INDEX idx_ml_samples_label_ts ON ml_training_samples_multiport (label_ts_utc);",
      "rationale": "Speed up time-based splits for model training and evaluation."
    },
    {
      "table": "port_calls_multiport",
      "statement": "CREATE INDEX idx_port_calls_censoring ON port_calls_multiport (censoring_flag) WHERE censoring_flag = TRUE;",
      "rationale": "Optimize queries for censored calls in survival analysis."
    }
  ]
