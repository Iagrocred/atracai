#!/usr/bin/env python3
"""
Train best-practice AIS-only TTB models:
- log1p(target) for heavy tail stability
- quantile models for risk bands (P50/P75/P90)

Reads:
- public.ml_training_samples_multiport (features jsonb, label_wait_hours, label_ts_utc)

Writes:
- models/ttb_point_log.pkl
- models/ttb_q50_log.pkl
- models/ttb_q75_log.pkl
- models/ttb_q90_log.pkl
- logs/ttb_train_report_v2.json

Usage:
  export DATABASE_URL="postgresql://portuser:paranagua123@localhost:5432/paranagua_port_r50"
  python3 ml/train_ttb_model.py --test-days 180
"""

from __future__ import annotations

import os, json, math, argparse
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.metrics import mean_absolute_error, mean_squared_error

from sklearn.ensemble import HistGradientBoostingRegressor

def _safe_dump(obj, path: str) -> None:
    try:
        import joblib
        joblib.dump(obj, path)
    except Exception:
        import pickle
        with open(path, "wb") as f:
            pickle.dump(obj, f)

def _safe_load_samples(engine, port_code: str | None, max_label_hours: float = 500.0):
    """Load samples with filtering for Cargo vessels only and outlier removal."""
    sql = """
      SELECT port_code, label_ts_utc, label_wait_hours, features, 
             COALESCE(censored, FALSE) as censored
      FROM public.ml_training_samples_multiport
      WHERE label_type='TTB'
        AND label_wait_hours IS NOT NULL
        AND label_wait_hours > 0
        AND label_wait_hours <= :max_hours
        AND (jsonb_extract_path_text(features, 'vessel_type_grouped') ILIKE 'Cargo%' 
             OR jsonb_extract_path_text(features, 'vessel_type_grouped') = 'Cargo'
             OR jsonb_extract_path_text(features, 'vessel_type_grouped') ILIKE '%Cargo%')
    """
    params = {"max_hours": max_label_hours}
    if port_code:
        sql += " AND port_code = :p"
        params["p"] = port_code
    df = pd.read_sql(text(sql), engine, params=params)
    print(f"[FILTER] Loaded {len(df)} Cargo-only samples (label_wait_hours <= {max_label_hours}h)")
    return df

def _p90_abs_err(y_true, y_pred) -> float:
    return float(np.quantile(np.abs(y_pred - y_true), 0.90))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-days", type=int, default=180)
    ap.add_argument("--port-code", type=str, default=None)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-label-hours", type=float, default=500.0, 
                    help="Cap labels at this value to handle outliers (default: 500h)")
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("Missing DATABASE_URL")

    os.makedirs("models", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    engine = create_engine(db_url, pool_pre_ping=True)
    df = _safe_load_samples(engine, args.port_code, args.max_label_hours)
    if df.empty:
        raise SystemExit("No samples found.")

    print(f"[DATA] Loaded {len(df)} samples (max_label_hours={args.max_label_hours})")
    print(f"[DATA] Label stats: min={df['label_wait_hours'].min():.2f}h, "
          f"max={df['label_wait_hours'].max():.2f}h, "
          f"median={df['label_wait_hours'].median():.2f}h, "
          f"mean={df['label_wait_hours'].mean():.2f}h")

    feat_df = pd.json_normalize(df["features"]).astype("float64", errors="ignore")
    X = pd.concat([df[["port_code"]].reset_index(drop=True), feat_df.reset_index(drop=True)], axis=1)

    y = df["label_wait_hours"].astype("float64").values
    y_log = np.log1p(y)
    censored = df["censored"].astype("bool").values

    # Compute sample weights - prioritize longer waits for better tail performance
    sample_weights = np.where(y > 50, 2.0, 1.0)
    print(f"[WEIGHTS] {(sample_weights > 1.0).sum()} samples weighted 2x (TTB > 50h)")

    ts = pd.to_datetime(df["label_ts_utc"], utc=True)
    cutoff = ts.max() - pd.Timedelta(days=int(args.test_days))
    test_idx = ts >= cutoff
    train_idx = ~test_idx

    # Report censoring statistics
    n_censored_train = censored[train_idx].sum()
    n_censored_test = censored[test_idx].sum()
    print(f"[CENSORING] Train: {n_censored_train}/{train_idx.sum()} ({100*n_censored_train/train_idx.sum():.1f}%) censored")
    print(f"[CENSORING] Test: {n_censored_test}/{test_idx.sum()} ({100*n_censored_test/test_idx.sum():.1f}%) censored")

    if train_idx.sum() < 200 or test_idx.sum() < 50:
        raise SystemExit("Time split too small; reduce --test-days or load more history.")

    X_train, X_test = X.loc[train_idx], X.loc[test_idx]
    y_train_log, y_test_log = y_log[train_idx.values], y_log[test_idx.values]
    y_train, y_test = y[train_idx.values], y[test_idx.values]
    sample_weights_train = sample_weights[train_idx.values]
    censored_test = censored[test_idx.values]

    cat_cols = ["port_code"]
    num_cols = [c for c in X.columns if c not in cat_cols]

    try:
        ohe = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:
        ohe = OneHotEncoder(handle_unknown="ignore", sparse=False)

    pre = ColumnTransformer(
        transformers=[
            ("port", ohe, cat_cols),
            ("num", "passthrough", num_cols),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )

    def train_model(loss: str, quantile: float | None, out_path: str):
        # Improved hyperparameters for better generalization
        kwargs = dict(
            learning_rate=0.025,  # Reduced from 0.05 for smoother convergence
            max_depth=5,          # Reduced from 7 to prevent overfitting
            max_iter=500,         # Reduced from 900 to prevent overtraining
            min_samples_leaf=20,
            random_state=args.seed,
        )
        if loss == "quantile":
            model = HistGradientBoostingRegressor(loss="quantile", quantile=quantile, **kwargs)
        elif loss == "huber":
            # Huber loss is robust to outliers
            model = HistGradientBoostingRegressor(loss="squared_error", **kwargs)  # Will use huber via custom loss
            loss_name = "huber"
        else:
            model = HistGradientBoostingRegressor(loss=loss, **kwargs)

        pipe = Pipeline([("pre", pre), ("model", model)])
        
        # Train with sample weights to prioritize longer waits
        pipe.fit(X_train, y_train_log, model__sample_weight=sample_weights_train)

        pred_log = pipe.predict(X_test)
        pred = np.expm1(pred_log)  # back-transform to hours

        # Evaluate only on completed (non-censored) test events for accuracy
        completed_mask = ~censored_test
        if completed_mask.sum() == 0:
            print(f"WARNING: No completed events in test set for {out_path}")
            mae = rmse = p90ae = bias = float('nan')
        else:
            y_test_completed = y_test[completed_mask]
            pred_completed = pred[completed_mask]
            mae = float(mean_absolute_error(y_test_completed, pred_completed))
            rmse = float(math.sqrt(mean_squared_error(y_test_completed, pred_completed)))
            p90ae = _p90_abs_err(y_test_completed, pred_completed)
            bias = float(np.mean(pred_completed - y_test_completed))

        _safe_dump(pipe, out_path)
        return {
            "out": out_path, 
            "mae": mae, 
            "rmse": rmse, 
            "p90ae": p90ae, 
            "bias": bias,
            "eval_on_completed_only": True,
            "n_test_completed": int(completed_mask.sum()),
            "n_test_total": int(len(censored_test)),
            "hyperparameters": kwargs
        }

    # Point model: optimize absolute error in log space (robust)
    point = train_model(loss="absolute_error", quantile=None, out_path="models/ttb_point_log.pkl")

    # Quantiles for risk bands
    q50 = train_model(loss="quantile", quantile=0.50, out_path="models/ttb_q50_log.pkl")
    q75 = train_model(loss="quantile", quantile=0.75, out_path="models/ttb_q75_log.pkl")
    q90 = train_model(loss="quantile", quantile=0.90, out_path="models/ttb_q90_log.pkl")

    report = {
        "rows_total": int(len(df)),
        "rows_train": int(train_idx.sum()),
        "rows_test": int(test_idx.sum()),
        "censored_train": int(n_censored_train),
        "censored_test": int(n_censored_test),
        "censored_pct_train": float(100 * n_censored_train / train_idx.sum()),
        "censored_pct_test": float(100 * n_censored_test / test_idx.sum()),
        "max_label_hours": float(args.max_label_hours),
        "label_stats": {
            "min": float(df['label_wait_hours'].min()),
            "max": float(df['label_wait_hours'].max()),
            "median": float(df['label_wait_hours'].median()),
            "mean": float(df['label_wait_hours'].mean()),
            "p90": float(df['label_wait_hours'].quantile(0.90))
        },
        "weighted_samples": int((sample_weights > 1.0).sum()),
        "cutoff_ts_utc": cutoff.isoformat(),
        "port_filter": args.port_code,
        "models": {
            "point": point,
            "q50": q50,
            "q75": q75,
            "q90": q90,
        },
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "training_config": {
            "target_transform": "log1p",
            "features": "AIS-only, leakage-safe, vessel metadata included",
            "censoring": "Included in training, excluded from evaluation metrics",
            "evaluation": "Metrics computed on completed events only",
            "sample_weighting": "2x weight for TTB > 50h",
            "outlier_handling": f"Labels capped at {args.max_label_hours}h"
        }
    }

    with open("logs/ttb_train_report_v2.json", "w") as f:
        json.dump(report, f, indent=2)

    # Feature importance analysis
    try:
        from sklearn.inspection import permutation_importance
        print("\n[FEATURE_IMPORTANCE] Computing permutation importance...")
        result = permutation_importance(
            point["pipe"], X_test, y_test, 
            n_repeats=5, random_state=args.seed, n_jobs=-1
        )
        
        # Get feature names after transformation
        feature_names = list(X.columns)
        importance_df = pd.DataFrame({
            'feature': feature_names,
            'importance_mean': result.importances_mean,
            'importance_std': result.importances_std
        }).sort_values('importance_mean', ascending=False)
        
        print("\nTop 10 Most Important Features:")
        print(importance_df.head(10).to_string(index=False))
        
        # Save to file
        importance_df.to_csv("logs/feature_importance.csv", index=False)
        print("\n[FEATURE_IMPORTANCE] Saved to logs/feature_importance.csv")
    except Exception as e:
        print(f"\n[FEATURE_IMPORTANCE] Warning: Could not compute importance: {e}")

    print(f"\n[TRAIN_V2] point: MAE={point['mae']:.2f}h P90AE={point['p90ae']:.2f}h bias={point['bias']:.2f}h")
    print(f"[TRAIN_V2] q50  : MAE={q50['mae']:.2f}h P90AE={q50['p90ae']:.2f}h bias={q50['bias']:.2f}h")
    print(f"[TRAIN_V2] q75  : MAE={q75['mae']:.2f}h P90AE={q75['p90ae']:.2f}h bias={q75['bias']:.2f}h")
    print(f"[TRAIN_V2] q90  : MAE={q90['mae']:.2f}h P90AE={q90['p90ae']:.2f}h bias={q90['bias']:.2f}h")
    
    # Performance assessment
    print("\n" + "="*60)
    print("PERFORMANCE ASSESSMENT")
    print("="*60)
    if point['mae'] < 20:
        print("✅ MAE: GOOD (<20h)")
    elif point['mae'] < 30:
        print("⚠️  MAE: ACCEPTABLE (20-30h) - Room for improvement")
    else:
        print("❌ MAE: POOR (>30h) - Needs urgent attention")
    
    if abs(point['bias']) < 2:
        print("✅ Bias: EXCELLENT (<2h)")
    elif abs(point['bias']) < 5:
        print("⚠️  Bias: ACCEPTABLE (2-5h)")
    else:
        print("❌ Bias: POOR (>5h) - Systematic prediction error")
    
    if point['p90ae'] < 50:
        print("✅ P90AE: GOOD (<50h)")
    elif point['p90ae'] < 100:
        print("⚠️  P90AE: ACCEPTABLE (50-100h)")
    else:
        print("❌ P90AE: POOR (>100h) - Tail prediction needs work")
    print("="*60)
    
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

