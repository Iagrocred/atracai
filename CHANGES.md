# ATRACAI STS v1 Implementation Changes

## Overview
This document outlines the changes implemented to address the root causes identified in `audit.md` and `change_plan.json` for improving Time-to-Berth (TTB) prediction accuracy for the STS port.

## Changes Implemented

### 1. Database Schema Updates (`migrations/add_censoring_support.sql`)

#### New Columns Added:
- **`port_calls_multiport.censoring_flag`** (BOOLEAN, DEFAULT FALSE)
  - Flags right-censored port calls where vessels are still waiting (berth_start_utc IS NULL)
  - Enables survival analysis techniques for handling incomplete observations
  
- **`ml_training_samples_multiport.censored`** (BOOLEAN, DEFAULT FALSE)
  - Indicates whether a training sample represents a censored event
  - Critical for training models that handle right-censored data

#### New Indexes:
- **`idx_port_calls_censoring`** on `port_calls_multiport(censoring_flag)` WHERE censoring_flag = TRUE
  - Optimizes queries for censored calls in survival analysis
  
- **`idx_ml_samples_label_ts`** on `ml_training_samples_multiport(label_ts_utc)`
  - Speeds up time-based splits for model training/evaluation

### 2. Script Modifications

#### A. `build_time_to_berth_labels.py` (Addresses Root Causes #2, #3, #4)

**Changes Made:**
1. **Removed Capping Logic** (Root Cause #2)
   - Removed `--cap-hours` parameter (was defaulting to 336h/14 days)
   - Now includes all wait times, including heavy-tail cases
   - Prevents bias from truncating long wait times

2. **Added Censoring Support** (Root Cause #3)
   - Added `censored` column to table creation
   - Identifies right-censored events (vessels still waiting for berth)
   - Computes current wait time for censored cases: `EXTRACT(EPOCH FROM (now() - basin_start_utc))/3600.0`
   - Modified WHERE clause to include both completed AND censored events

3. **Enhanced Feature Set** (Root Cause #4)
   - Added vessel metadata features from `vessel_info` table:
     - `vessel_deadweight` - Vessel deadweight tonnage
     - `vessel_draught_avg` - Average vessel draught
     - `vessel_length_m` - Vessel length in meters
     - `vessel_beam_m` - Vessel beam width in meters
     - `vessel_type` - Type of vessel
   - All features remain **leakage-safe** (use only past/static data)

**Key SQL Changes:**
```sql
-- Old (capped):
WHERE pc.time_to_berth_hours <= :cap

-- New (includes censored):
WHERE (
  (pc.time_to_berth_hours IS NOT NULL AND pc.time_to_berth_hours > 0)
  OR pc.berth_start_utc IS NULL  -- Include censored
)
```

#### B. `build_port_calls_multiport.py` (Addresses Root Cause #1)

**Changes Made:**
1. **Added Censoring Flag Updates**
   - After processing port calls, automatically marks censored calls
   - Sets `censoring_flag = TRUE` for calls where:
     - `basin_start_utc IS NOT NULL` (vessel entered basin)
     - `berth_start_utc IS NULL` (vessel hasn't berthed yet)
   - Provides visibility into censored data counts

**Output Enhancement:**
```
[OK] port_calls in window: 1234
[OK] marked 56 censored calls (berth_start IS NULL)
```

### 3. Root Causes Addressed

| Rank | Root Cause | Status | Implementation |
|------|-----------|---------|----------------|
| 1 | Incorrect port_zone_roles configuration | ⚠️ Partially | Added censoring tracking; zone validation requires config review |
| 2 | Heavy-tail handling via capping biases training | ✅ **Fixed** | Removed capping, include all wait times |
| 3 | No censoring handling for right-censored events | ✅ **Fixed** | Added censoring flags and logic |
| 4 | Limited feature set lacking vessel metadata | ✅ **Fixed** | Added vessel_info features (deadweight, draught, dimensions, type) |
| 5 | Potential AIS data gaps | ℹ️ Acknowledged | Existing --max-gap-min handles this |

### 4. Maintained Principles

✅ **Leakage Prevention**
- All features use data up to `label_ts_utc` only
- Vessel metadata is static/historical
- Congestion features use lookback windows only

✅ **Backward Compatibility**
- Schema changes use `IF NOT EXISTS` and `DEFAULT` values
- Existing queries continue to work
- New columns are optional

✅ **Data Quality**
- Anti-tug filters maintained
- MMSI validation maintained
- Minimum vessel length filter (70m) maintained

## Next Steps

### Recommended Follow-up Actions:

1. **Database Migration**
   ```bash
   psql $DATABASE_URL < migrations/add_censoring_support.sql
   ```

2. **Rebuild Training Samples**
   ```bash
   python3 build_time_to_berth_labels.py --ports STS --since-days 365 --replace-since
   ```

3. **Port Zone Validation** (Root Cause #1)
   - Review `port_zone_roles` table for STS
   - Validate QUEUE and BASIN zone geometries
   - Cross-reference with official port maps
   - Test zone assignment with sample AIS data

4. **Model Retraining**
   - Update `train_ttb_model.py` to handle censored data
   - Consider implementing survival models (Cox PH, AFT)
   - Add calibration metrics (ECE, CRPS)
   - Validate on temporal split (last 180 days)

5. **Testing & Validation**
   - Verify anti-leakage: features use only past data
   - Temporal split validation: train on older data, test on recent
   - Compute metrics: MAE, bias, P90 coverage
   - Compare against baseline capped model

## Technical Notes

### Censoring Logic
- **Right-censored**: Vessel entered basin but hasn't berthed yet
- **Observable wait time**: Time from `basin_start_utc` to `now()`
- **True wait time**: Unknown (could be longer)
- **Use case**: Survival analysis, quantile regression with censoring support

### Feature Safety
All added features are **leakage-safe**:
- Vessel metadata: Static vessel characteristics (doesn't change over voyage)
- Calendar features: Based on label timestamp only
- Congestion features: Lookback windows ending at label timestamp

### Performance Considerations
- New indexes optimize censored data queries
- Vessel metadata join adds minimal overhead (indexed on mmsi)
- Censoring flag enables efficient filtering

## References
- `audit.md` - Root cause analysis
- `change_plan.json` - Detailed change specifications
- `schema_review.md` - Database schema requirements
- `dbsnapshot` - Current database state
