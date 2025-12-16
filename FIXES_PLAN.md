# Critical Fixes Implementation Plan

Based on the poor model performance (MAE 83.81h, Bias -55.65h), the following issues have been identified and must be fixed immediately:

## Problem Analysis

**Current Results:**
- MAE: 83.81h (Target: <16h) - **422% worse than expected**
- Bias: -55.65h (Target: ±0.5h) - **Massive systematic underestimation**
- P90AE: 313h-324h (Target: <50h) - **Catastrophic tail performance**

**Root Causes:**
1. **Extreme outliers** in target (max 682h, P90 365h) distorting model
2. **Heavy skew** (median 1.98h vs mean 95.15h) causing imbalanced learning
3. **No outlier handling** - model trained on full range including extreme cases
4. **Sparse categorical variables** (vessel_type with 24 values, 497 missing)
5. **Suboptimal hyperparameters** (max_depth=7, learning_rate=0.05 too aggressive)

## Immediate Fixes Required

### 1. **Cap/Winsorize Target Variable**
   - Cap label_wait_hours at P95 or 500h (whichever lower)
   - Remove extreme outliers that distort model

### 2. **Handle Vessel Type Properly**
   - Group rare vessel types into broader categories
   - Impute missing vessel_type as 'Unknown'
   - Reduce from 24 to ~6-8 meaningful categories

### 3. **Add Sample Weighting**
   - Weight longer waits (>50h) more heavily
   - Force model to learn tail behavior without overfitting

### 4. **Optimize Hyperparameters**
   - Reduce max_depth: 7 → 5 (prevent overfitting to outliers)
   - Reduce learning_rate: 0.05 → 0.025 (smoother convergence)
   - Reduce max_iter: 900 → 500 (prevent overtraining)
   - Try loss='huber' for robustness to outliers

### 5. **Add Data Quality Filters**
   - Filter out samples with label_wait_hours > 500h before training
   - Add validation for feature completeness

## Implementation Steps

1. Modify `build_time_to_berth_labels.py`:
   - Add winsorization/capping at 500h
   - Improve vessel_type handling (grouping + imputation)

2. Modify `ml/train_ttb_model.py`:
   - Add sample weighting for imbalanced distribution
   - Update hyperparameters
   - Add Huber loss option
   - Add feature importance analysis
   - Add more diagnostic outputs

3. Add data quality checks and preprocessing

## Expected Improvements

After fixes:
- MAE: Should drop to 12-18h (80-85% improvement)
- Bias: Should approach ±2h (96% improvement)
- P90AE: Should drop to 30-50h (85-90% improvement)

These fixes address the "shameful" results and should bring performance in line with expectations.
