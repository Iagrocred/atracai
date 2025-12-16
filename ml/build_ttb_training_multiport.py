#!/usr/bin/env python3
"""
Build V2 ML training samples for Time-to-Berth (TTB) into the EXISTING table:
  public.ml_training_samples_multiport

Existing schema:
- port_code, mmsi, label_ts_utc, label_wait_hours, label_type, features (jsonb), confidence, extra

V2 Features added (leakage-safe; all use data <= label_ts_utc):
- Multi-window congestion snapshots: 30m / 2h / 6h / 24h
- Queue trend proxies: last30m vs last6h average
- Arrival rate into anchorage (distinct MMSI seen in ANCHORAGE last 24h)
- Throughput proxy: berth_calls ended in last 72h (AIS-derived, past-only)
- Rolling port medians (7d/30d/90d) from past labels only
- Vessel/port history: last TTB, mean of last 3, count past visits
- Vessel motion: 1h/6h/24h sog stats + stopped share (sog<0.5)

Usage:
  export DATABASE_URL="postgresql://portuser:paranagua123@localhost:5432/paranagua_port_r50"
  python3 ml/build_ttb_training_multiport.py --days-back 5000
  python3 ml/build_ttb_training_multiport.py --days-back 365 --port-code PNG
"""

from __future__ import annotations

import os
import argparse
from sqlalchemy import create_engine, text

UPSERT_SQL = """
WITH lbl AS (
  SELECT
    port_call_id,
    port_code,
    mmsi,
    anchorage_start_utc,
    berth_start_utc,
    time_to_berth_hours,
    confidence
  FROM public.time_to_berth_labels
  WHERE anchorage_start_utc >= :since_ts
    AND (:port_code IS NULL OR port_code = :port_code)
),

feat AS (
  SELECT
    l.port_code,
    l.mmsi,
    l.anchorage_start_utc AS label_ts_utc,
    'TTB'::text AS label_type,
    l.time_to_berth_hours::double precision AS label_wait_hours,
    l.confidence::text AS confidence,

    jsonb_strip_nulls(
      jsonb_build_object(
        /* ----------------- calendar ----------------- */
        'hour_of_day', EXTRACT(HOUR FROM l.anchorage_start_utc AT TIME ZONE 'utc')::int,
        'day_of_week', EXTRACT(DOW  FROM l.anchorage_start_utc AT TIME ZONE 'utc')::int,
        'month',      EXTRACT(MONTH FROM l.anchorage_start_utc AT TIME ZONE 'utc')::int,

        /* -------- congestion snapshots (distinct MMSI) -------- */
        'q_anch_30m', COALESCE((
          SELECT COUNT(DISTINCT ap.mmsi)::bigint
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '30 minutes'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.zone = 'ANCHORAGE'
        ),0),

        'q_anch_2h', COALESCE((
          SELECT COUNT(DISTINCT ap.mmsi)::bigint
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '2 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.zone = 'ANCHORAGE'
        ),0),

        'q_anch_6h', COALESCE((
          SELECT COUNT(DISTINCT ap.mmsi)::bigint
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '6 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.zone = 'ANCHORAGE'
        ),0),

        'q_anch_24h', COALESCE((
          SELECT COUNT(DISTINCT ap.mmsi)::bigint
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '24 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.zone = 'ANCHORAGE'
        ),0),

        'q_app_6h', COALESCE((
          SELECT COUNT(DISTINCT ap.mmsi)::bigint
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '6 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.zone = 'APPROACH'
        ),0),

        'q_portarea_6h', COALESCE((
          SELECT COUNT(DISTINCT ap.mmsi)::bigint
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '6 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.zone = 'PORT_AREA'
        ),0),

        /* -------- queue trend proxy: last30m vs avg per-hour last6h -------- */
        'q_trend_30m_minus_6h_rate', (
          COALESCE((
            SELECT COUNT(DISTINCT ap.mmsi)::double precision
            FROM public.ais_positions ap
            WHERE ap.port_code = l.port_code
              AND ap.timestamp_utc >  l.anchorage_start_utc - interval '30 minutes'
              AND ap.timestamp_utc <= l.anchorage_start_utc
              AND ap.zone = 'ANCHORAGE'
          ),0)
          -
          COALESCE((
            SELECT (COUNT(DISTINCT ap.mmsi)::double precision / 6.0)
            FROM public.ais_positions ap
            WHERE ap.port_code = l.port_code
              AND ap.timestamp_utc >  l.anchorage_start_utc - interval '6 hours'
              AND ap.timestamp_utc <= l.anchorage_start_utc
              AND ap.zone = 'ANCHORAGE'
          ),0)
        ),

        /* -------- arrival rate proxy into anchorage (distinct MMSI seen in anchorage last 24h) -------- */
        'anch_arrivals_24h_distinct', COALESCE((
          SELECT COUNT(DISTINCT ap.mmsi)::bigint
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '24 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.zone = 'ANCHORAGE'
        ),0),

        /* -------- throughput proxy: AIS berth_calls ended in last 72h (PAST ONLY) -------- */
        'berth_throughput_72h', COALESCE((
          SELECT COUNT(*)::bigint
          FROM public.berth_calls_multiport bc
          WHERE bc.port_code = l.port_code
            AND bc.berth_end_utc IS NOT NULL
            AND bc.berth_end_utc >  l.anchorage_start_utc - interval '72 hours'
            AND bc.berth_end_utc <= l.anchorage_start_utc
        ),0),

        /* -------- rolling port medians from PAST labels only -------- */
        'port_median_ttb_7d', (
          SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t.time_to_berth_hours)::double precision
          FROM public.time_to_berth_labels t
          WHERE t.port_code = l.port_code
            AND t.anchorage_start_utc <  l.anchorage_start_utc
            AND t.anchorage_start_utc >= l.anchorage_start_utc - interval '7 days'
        ),
        'port_median_ttb_30d', (
          SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t.time_to_berth_hours)::double precision
          FROM public.time_to_berth_labels t
          WHERE t.port_code = l.port_code
            AND t.anchorage_start_utc <  l.anchorage_start_utc
            AND t.anchorage_start_utc >= l.anchorage_start_utc - interval '30 days'
        ),
        'port_median_ttb_90d', (
          SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t.time_to_berth_hours)::double precision
          FROM public.time_to_berth_labels t
          WHERE t.port_code = l.port_code
            AND t.anchorage_start_utc <  l.anchorage_start_utc
            AND t.anchorage_start_utc >= l.anchorage_start_utc - interval '90 days'
        ),

        /* -------- vessel+port history (PAST ONLY) -------- */
        'vessel_port_prev_count_365d', COALESCE((
          SELECT COUNT(*)::bigint
          FROM public.time_to_berth_labels t
          WHERE t.port_code = l.port_code
            AND t.mmsi = l.mmsi
            AND t.anchorage_start_utc <  l.anchorage_start_utc
            AND t.anchorage_start_utc >= l.anchorage_start_utc - interval '365 days'
        ),0),

        'vessel_port_last_ttb', (
          SELECT t.time_to_berth_hours::double precision
          FROM public.time_to_berth_labels t
          WHERE t.port_code = l.port_code
            AND t.mmsi = l.mmsi
            AND t.anchorage_start_utc < l.anchorage_start_utc
          ORDER BY t.anchorage_start_utc DESC
          LIMIT 1
        ),

        'vessel_port_mean_last3_ttb', (
          SELECT AVG(x.time_to_berth_hours)::double precision
          FROM (
            SELECT t.time_to_berth_hours
            FROM public.time_to_berth_labels t
            WHERE t.port_code = l.port_code
              AND t.mmsi = l.mmsi
              AND t.anchorage_start_utc < l.anchorage_start_utc
            ORDER BY t.anchorage_start_utc DESC
            LIMIT 3
          ) x
        ),

        /* -------- vessel motion windows (PAST ONLY) -------- */
        'sog_mean_1h', (
          SELECT AVG(ap.sog)::double precision
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.mmsi = l.mmsi
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '1 hour'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.sog IS NOT NULL
        ),
        'sog_mean_6h', (
          SELECT AVG(ap.sog)::double precision
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.mmsi = l.mmsi
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '6 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.sog IS NOT NULL
        ),
        'sog_mean_24h', (
          SELECT AVG(ap.sog)::double precision
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.mmsi = l.mmsi
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '24 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.sog IS NOT NULL
        ),

        'stopped_share_6h', (
          SELECT CASE WHEN COUNT(*) = 0 THEN NULL
                      ELSE (SUM(CASE WHEN ap.sog < 0.5 THEN 1 ELSE 0 END)::double precision / COUNT(*)::double precision)
                 END
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.mmsi = l.mmsi
            AND ap.timestamp_utc >  l.anchorage_start_utc - interval '6 hours'
            AND ap.timestamp_utc <= l.anchorage_start_utc
            AND ap.sog IS NOT NULL
        ),

        'distance_nm_last', (
          SELECT ap.distance_nm::double precision
          FROM public.ais_positions ap
          WHERE ap.port_code = l.port_code
            AND ap.mmsi = l.mmsi
            AND ap.timestamp_utc <= l.anchorage_start_utc
          ORDER BY ap.timestamp_utc DESC
          LIMIT 1
        )
      )
    ) AS features,

    jsonb_build_object(
      'port_call_id', l.port_call_id,
      'anchorage_start_utc', l.anchorage_start_utc,
      'berth_start_utc', l.berth_start_utc
    ) AS extra

  FROM lbl l
),

do_update AS (
  UPDATE public.ml_training_samples_multiport s
  SET
    label_wait_hours = f.label_wait_hours,
    features = f.features,
    confidence = f.confidence,
    extra = f.extra
  FROM feat f
  WHERE s.port_code = f.port_code
    AND s.mmsi = f.mmsi
    AND s.label_ts_utc = f.label_ts_utc
    AND s.label_type = f.label_type
  RETURNING 1
),

do_insert AS (
  INSERT INTO public.ml_training_samples_multiport (
    port_code, mmsi, label_ts_utc, label_wait_hours, label_type,
    features, confidence, extra
  )
  SELECT
    f.port_code, f.mmsi, f.label_ts_utc, f.label_wait_hours, f.label_type,
    f.features, f.confidence, f.extra
  FROM feat f
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.ml_training_samples_multiport s
    WHERE s.port_code = f.port_code
      AND s.mmsi = f.mmsi
      AND s.label_ts_utc = f.label_ts_utc
      AND s.label_type = f.label_type
  )
  RETURNING 1
)

SELECT
  (SELECT COUNT(*) FROM do_update) AS updated_rows,
  (SELECT COUNT(*) FROM do_insert) AS inserted_rows;
"""

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=5000)
    ap.add_argument("--port-code", type=str, default=None)
    args = ap.parse_args()

    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("Missing DATABASE_URL")

    engine = create_engine(url, pool_pre_ping=True)

    with engine.begin() as conn:
        since_ts = conn.execute(
            text("SELECT (now() AT TIME ZONE 'utc') - (:d || ' days')::interval;"),
            {"d": args.days_back},
        ).scalar()

        updated, inserted = conn.execute(
            text(UPSERT_SQL),
            {"since_ts": since_ts, "port_code": args.port_code},
        ).one()

        total = conn.execute(
            text("SELECT COUNT(*)::bigint FROM public.ml_training_samples_multiport WHERE label_type='TTB';")
        ).scalar()

    print(f"[TTB_TRAIN_V2] OK inserted={inserted} updated={updated} total={total}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

