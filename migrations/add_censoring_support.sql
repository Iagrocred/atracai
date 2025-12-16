-- Migration: Add censoring support for survival analysis
-- Date: 2025-12-16
-- Purpose: Implement changes from schema_review.md for censored data handling

-- 1. Add censoring_flag to port_calls_multiport
ALTER TABLE port_calls_multiport 
ADD COLUMN IF NOT EXISTS censoring_flag BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN port_calls_multiport.censoring_flag IS 
'Flag right-censored calls (berth_start_utc IS NULL) for survival analysis in label building';

-- 2. Add censored column to ml_training_samples_multiport
ALTER TABLE ml_training_samples_multiport 
ADD COLUMN IF NOT EXISTS censored BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN ml_training_samples_multiport.censored IS 
'Indicate censored labels for model training (e.g., vessels still waiting)';

-- 3. Create index for censoring queries (recommended from schema_review.md)
CREATE INDEX IF NOT EXISTS idx_port_calls_censoring 
ON port_calls_multiport (censoring_flag) 
WHERE censoring_flag = TRUE;

COMMENT ON INDEX idx_port_calls_censoring IS 
'Optimize queries for censored calls in survival analysis';

-- 4. Create index for time-based splits (recommended from schema_review.md)
CREATE INDEX IF NOT EXISTS idx_ml_samples_label_ts 
ON ml_training_samples_multiport (label_ts_utc);

COMMENT ON INDEX idx_ml_samples_label_ts IS 
'Speed up time-based splits for model training and evaluation';

-- 5. Update existing port_calls_multiport rows to mark censored calls
-- Limit to recent data for performance (last 2 years)
UPDATE port_calls_multiport
SET censoring_flag = TRUE
WHERE berth_start_utc IS NULL
  AND basin_start_utc IS NOT NULL
  AND basin_start_utc >= (now() AT TIME ZONE 'utc') - interval '2 years';

-- Note: For older data, censoring_flag will be set by build_port_calls_multiport.py
-- when those records are processed

COMMENT ON TABLE port_calls_multiport IS 
'Port calls with session-based aggregation, zone timestamps, and censoring support for TTB prediction';

COMMENT ON TABLE ml_training_samples_multiport IS 
'ML training samples with features, labels, and censoring flags for survival analysis models';
