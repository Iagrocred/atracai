to-brasil/atracai/ml # cat train_ttb_model.py
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

def _safe_load_samples(engine, port_code: str | None):
    sql = """
      SELECT port_code, label_ts_utc, label_wait_hours, features
      FROM public.ml_training_samples_multiport
      WHERE label_type='TTB'
        AND label_wait_hours IS NOT NULL
        AND label_wait_hours > 0
    """
    params = {}
    if port_code:
        sql += " AND port_code = :p"
        params["p"] = port_code
    df = pd.read_sql(text(sql), engine, params=params)
    return df

def _p90_abs_err(y_true, y_pred) -> float:
    return float(np.quantile(np.abs(y_pred - y_true), 0.90))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-days", type=int, default=180)
    ap.add_argument("--port-code", type=str, default=None)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("Missing DATABASE_URL")

    os.makedirs("models", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    engine = create_engine(db_url, pool_pre_ping=True)
    df = _safe_load_samples(engine, args.port_code)
    if df.empty:
        raise SystemExit("No samples found.")

    feat_df = pd.json_normalize(df["features"]).astype("float64", errors="ignore")
    X = pd.concat([df[["port_code"]].reset_index(drop=True), feat_df.reset_index(drop=True)], axis=1)

    y = df["label_wait_hours"].astype("float64").values
    y_log = np.log1p(y)

    ts = pd.to_datetime(df["label_ts_utc"], utc=True)
    cutoff = ts.max() - pd.Timedelta(days=int(args.test_days))
    test_idx = ts >= cutoff
    train_idx = ~test_idx

    if train_idx.sum() < 200 or test_idx.sum() < 50:
        raise SystemExit("Time split too small; reduce --test-days or load more history.")

    X_train, X_test = X.loc[train_idx], X.loc[test_idx]
    y_train_log, y_test_log = y_log[train_idx.values], y_log[test_idx.values]
    y_test = y[test_idx.values]

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
        kwargs = dict(
            learning_rate=0.05,
            max_depth=7,
            max_iter=900,
            min_samples_leaf=20,
            random_state=args.seed,
        )
        if loss == "quantile":
            model = HistGradientBoostingRegressor(loss="quantile", quantile=quantile, **kwargs)
        else:
            model = HistGradientBoostingRegressor(loss=loss, **kwargs)

        pipe = Pipeline([("pre", pre), ("model", model)])
        pipe.fit(X_train, y_train_log)

        pred_log = pipe.predict(X_test)
        pred = np.expm1(pred_log)  # back-transform to hours

        mae = float(mean_absolute_error(y_test, pred))
        rmse = float(math.sqrt(mean_squared_error(y_test, pred)))
        p90ae = _p90_abs_err(y_test, pred)
        bias = float(np.mean(pred - y_test))

        _safe_dump(pipe, out_path)
        return {"out": out_path, "mae": mae, "rmse": rmse, "p90ae": p90ae, "bias": bias}

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
        "cutoff_ts_utc": cutoff.isoformat(),
        "port_filter": args.port_code,
        "models": {
            "point": point,
            "q50": q50,
            "q75": q75,
            "q90": q90,
        },
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "All models trained on log1p(y) and back-transformed; features are AIS-only and leakage-safe."
    }

    with open("logs/ttb_train_report_v2.json", "w") as f:
        json.dump(report, f, indent=2)

    print(f"[TRAIN_V2] point: MAE={point['mae']:.2f}h P90AE={point['p90ae']:.2f}h bias={point['bias']:.2f}h")
    print(f"[TRAIN_V2] q50  : MAE={q50['mae']:.2f}h P90AE={q50['p90ae']:.2f}h bias={q50['bias']:.2f}h")
    print(f"[TRAIN_V2] q75  : MAE={q75['mae']:.2f}h P90AE={q75['p90ae']:.2f}h bias={q75['bias']:.2f}h")
    print(f"[TRAIN_V2] q90  : MAE={q90['mae']:.2f}h P90AE={q90['p90ae']:.2f}h bias={q90['bias']:.2f}h")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
(base) root@Ubuntu-2404-noble-amd64-base ~/custo-brasil/atracai/ml #
