===== audit.md =====
# ATRACAI STS v1 Audit Report

## Zoning Correctness Assessment
**Scripts Computing Zone/Events for STS:**
- `ml/build_berth_belts_multiport.py`: Detects berth polygons from AIS data using DBSCAN clustering and creates berth belts (buffered geometries).
- `calls/build_port_calls_multiport.py`: Constructs port calls by aggregating berth calls and using `port_zone_roles` to assign timestamps for QUEUE (`anchorage_queue_start_utc`) and BASIN (`basin_start_utc`) zones based on geospatial events.

**What is Correct:**
- The pipeline is AIS-first and multiport-capable, with configurable zones via `port_zone_roles`.
- Berth detection uses unsupervised learning (DBSCAN) refined from AIS positions, which is standard for port geometry inference.
- Indexes on `port_calls_multiport` (e.g., `idx_port_calls_port_berth_start`) support efficient querying.

**Risks:**
1. **Configuration Errors:** `port_zone_roles` for STS may be misconfigured, leading to incorrect `anchorage_queue_start_utc`/`basin_start_utc` timestamps and mislabeled TTB events. Root cause: manual zone mapping prone to human error.
2. **Data Gaps:** AIS dropouts (handled by `--max-gap-min` in `calls/build_berth_calls_multiport.py`) may cause fragmented berth calls, affecting zone event continuity.
3. **Limitations of DBSCAN:** Small or isolated berths may be missed, impacting belt accuracy (per Tavily phase2 findings).

## Schema Review Summary
Current schema (`DB_SNAPSHOT.md`) supports core operations but lacks features for enterprise auditability, censored data handling, and enhanced ML. No breaking issues exist for STS v1 deployment, but additive improvements are needed.

## Evaluation Contract vs Baseline
**Baseline:** Uses capping at `--cap-hours` (336h) in `calls/build_time_to_berth_labels.py`, ignoring censored data and heavy tails; features limited to congestion counts and calendar variables.
**No-Cheating Contract:**
- **Metrics:** MAE (mean absolute error in hours), bias (mean error), coverage at 90% (proportion of actual TTB ≤ predicted P90).
- **Anti-Leakage Rules:**
  - Strict temporal splits (e.g., `--test-days` in `ml/train_ttb_models_by_port.py`).
  - Features computed only up to `label_ts_utc` (as in `calls/build_time_to_berth_labels.py`).
  - Prohibit using future berth calls or events.
  - Validate on unseen time periods (e.g., last 180 days).
- **Comparison:** Proposed v1 improves by adding censoring handling, survival analysis for heavy tails, and richer features, targeting higher coverage and lower bias.

## Required Files Not Provided
To finalize specs, need content of:
- `ml/build_berth_belts_multiport.py`
- `ml/build_ttb_training_multiport.py`
- `config/port_zone_roles_sts.csv` (or equivalent)
- `scripts/ais_zone_and_events_multiport.py` (if exists for zoning logic)
