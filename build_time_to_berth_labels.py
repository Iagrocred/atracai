#!/usr/bin/env python3
from __future__ import annotations

import os
import argparse
from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise SystemExit("DATABASE_URL not set")

engine = create_engine(DB_URL, pool_pre_ping=True)

def parse_ports(s: str):
    return [p.strip().upper() for p in (s or "").split(",") if p.strip()]

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", required=True, help="Comma-separated port codes (e.g., STS,PNG)")
    ap.add_argument("--since-days", type=int, default=365)
    ap.add_argument("--replace-since", action="store_true")
    ap.add_argument("--window-min", type=int, default=30, help="Window (minutes) for 'now' congestion features (default 30)")
    args = ap.parse_args()

    ports = parse_ports(args.ports)
    if not ports:
        raise SystemExit("no ports")

    print("=== BUILD TTB LABELS -> ml_training_samples_multiport ===")
    print(f"ports={ports} since_days={args.since_days} replace={args.replace_since} window_min={args.window_min}")
    print("NOTE: Now includes censored data (vessels still waiting) for survival analysis")

    # Create table if missing (does not modify existing)
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS public.ml_training_samples_multiport (
          id bigserial PRIMARY KEY,
          port_code text NOT NULL,
          mmsi text NOT NULL,
          label_ts_utc timestamptz NOT NULL,
          label_type text NOT NULL,
          label_wait_hours double precision,
          features jsonb NOT NULL DEFAULT '{}'::jsonb,
          censored boolean DEFAULT FALSE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        """))

    for port in ports:
        with engine.begin() as conn:
            if args.replace_since:
                conn.execute(text("""
                    DELETE FROM public.ml_training_samples_multiport
                    WHERE port_code = :p
                      AND label_type = 'TTB'
                      AND label_ts_utc >= (now() AT TIME ZONE 'utc') - (:d || ' days')::interval
                """), {"p": port, "d": args.since_days})
                print(f"[TTB] {port}: cleared existing samples in window")

            # Leak-safe features:
            # - label_ts_utc = pc.basin_start_utc
            # - congestion uses only AIS points <= label_ts_utc (lookback windows)
            # IMPORTANT type cast: vessel_info.mmsi is BIGINT, port_calls_multiport.mmsi is TEXT
            # Censoring: include both completed and censored (berth_start_utc IS NULL) events
            # Use consistent timestamp for entire batch to ensure reproducibility
            batch_ts = conn.execute(text("SELECT now() AT TIME ZONE 'utc'")).scalar()
            conn.execute(text("""
                INSERT INTO public.ml_training_samples_multiport
                  (port_code, mmsi, label_ts_utc, label_type, label_wait_hours, censored, features)
                SELECT
                  pc.port_code,
                  pc.mmsi,
                  pc.basin_start_utc AS label_ts_utc,
                  'TTB' AS label_type,
                  -- For censored: use time since basin_start to batch_ts; for completed: actual TTB
                  -- Cap at 500h to handle extreme outliers (winsorization)
                  LEAST(
                    CASE 
                      WHEN pc.berth_start_utc IS NULL THEN 
                        EXTRACT(EPOCH FROM (:batch_ts - pc.basin_start_utc))/3600.0
                      ELSE pc.time_to_berth_hours
                    END,
                    500.0
                  ) AS label_wait_hours,
                  -- Mark as censored if berth_start_utc is NULL (vessel still waiting)
                  CASE WHEN pc.berth_start_utc IS NULL THEN TRUE ELSE FALSE END AS censored,
                  jsonb_build_object(
                    -- calendar (UTC)
                    'hour_utc',  EXTRACT(HOUR  FROM (pc.basin_start_utc AT TIME ZONE 'utc')),
                    'dow_utc',   EXTRACT(DOW   FROM (pc.basin_start_utc AT TIME ZONE 'utc')),
                    'month_utc', EXTRACT(MONTH FROM (pc.basin_start_utc AT TIME ZONE 'utc')),
                    'is_weekend', CASE WHEN EXTRACT(DOW FROM (pc.basin_start_utc AT TIME ZONE 'utc')) IN (0,6) THEN 1 ELSE 0 END,

                    -- vessel metadata (from vessel_info, leak-safe as it's static or historical)
                    'vessel_deadweight', vi.deadweight,
                    'vessel_draught_avg', vi.draught_avg,
                    'vessel_length_m', vi.length_m,
                    'vessel_beam_m', vi.beam_m,
                    -- Group vessel types into broader categories + handle missing
                    'vessel_type_grouped', CASE
                      WHEN vi.vessel_type IS NULL THEN 'Unknown'
                      WHEN vi.vessel_type ILIKE '%cargo%' THEN 'Cargo'
                      WHEN vi.vessel_type ILIKE '%tanker%' THEN 'Tanker'
                      WHEN vi.vessel_type ILIKE '%passenger%' THEN 'Passenger'
                      WHEN vi.vessel_type ILIKE '%hazard%' THEN 'Hazardous'
                      WHEN vi.vessel_type ILIKE '%container%' THEN 'Container'
                      ELSE 'Other'
                    END,

                    -- congestion now (distinct MMSI in role zones within window-min)
                    'queue_mmsi_30m', (
                      SELECT COUNT(DISTINCT ap.mmsi)
                      FROM public.ais_positions ap
                      JOIN public.port_zone_roles r
                        ON r.port_code = ap.port_code
                       AND r.zone_name = ap.zone
                       AND r.role = 'QUEUE'
                      WHERE ap.port_code = pc.port_code
                        AND ap.zone IS NOT NULL
                        AND ap.timestamp_utc >  pc.basin_start_utc - (:wmin || ' minutes')::interval
                        AND ap.timestamp_utc <= pc.basin_start_utc
                        AND ap.mmsi ~ '^[0-9]{7,9}$'
                    ),
                    'basin_mmsi_30m', (
                      SELECT COUNT(DISTINCT ap.mmsi)
                      FROM public.ais_positions ap
                      JOIN public.port_zone_roles r
                        ON r.port_code = ap.port_code
                       AND r.zone_name = ap.zone
                       AND r.role = 'BASIN'
                      WHERE ap.port_code = pc.port_code
                        AND ap.zone IS NOT NULL
                        AND ap.timestamp_utc >  pc.basin_start_utc - (:wmin || ' minutes')::interval
                        AND ap.timestamp_utc <= pc.basin_start_utc
                        AND ap.mmsi ~ '^[0-9]{7,9}$'
                    ),
                    'holding_mmsi_30m', (
                      SELECT COUNT(DISTINCT ap.mmsi)
                      FROM public.ais_positions ap
                      JOIN public.port_zone_roles r
                        ON r.port_code = ap.port_code
                       AND r.zone_name = ap.zone
                       AND r.role = 'HOLDING'
                      WHERE ap.port_code = pc.port_code
                        AND ap.zone IS NOT NULL
                        AND ap.timestamp_utc >  pc.basin_start_utc - (:wmin || ' minutes')::interval
                        AND ap.timestamp_utc <= pc.basin_start_utc
                        AND ap.mmsi ~ '^[0-9]{7,9}$'
                    ),

                    -- longer state (6h) helps heavy tail (still leak-safe)
                    'queue_mmsi_6h', (
                      SELECT COUNT(DISTINCT ap.mmsi)
                      FROM public.ais_positions ap
                      JOIN public.port_zone_roles r
                        ON r.port_code = ap.port_code
                       AND r.zone_name = ap.zone
                       AND r.role = 'QUEUE'
                      WHERE ap.port_code = pc.port_code
                        AND ap.zone IS NOT NULL
                        AND ap.timestamp_utc >  pc.basin_start_utc - interval '6 hours'
                        AND ap.timestamp_utc <= pc.basin_start_utc
                        AND ap.mmsi ~ '^[0-9]{7,9}$'
                    ),
                    'basin_mmsi_6h', (
                      SELECT COUNT(DISTINCT ap.mmsi)
                      FROM public.ais_positions ap
                      JOIN public.port_zone_roles r
                        ON r.port_code = ap.port_code
                       AND r.zone_name = ap.zone
                       AND r.role = 'BASIN'
                      WHERE ap.port_code = pc.port_code
                        AND ap.zone IS NOT NULL
                        AND ap.timestamp_utc >  pc.basin_start_utc - interval '6 hours'
                        AND ap.timestamp_utc <= pc.basin_start_utc
                        AND ap.mmsi ~ '^[0-9]{7,9}$'
                    )
                  ) AS features
                FROM public.port_calls_multiport pc
                LEFT JOIN public.vessel_info vi
                  ON vi.mmsi::text = pc.mmsi
                WHERE pc.port_code = :p
                  AND pc.basin_start_utc IS NOT NULL
                  -- Include both completed (time_to_berth_hours > 0) and censored (berth_start IS NULL) events
                  AND (
                    (pc.time_to_berth_hours IS NOT NULL AND pc.time_to_berth_hours > 0)
                    OR pc.berth_start_utc IS NULL
                  )
                  AND pc.basin_start_utc >= (now() AT TIME ZONE 'utc') - (:d || ' days')::interval
                  AND pc.mmsi ~ '^[0-9]{7,9}$'
                  -- CARGO FOCUS: Only train on cargo vessels (export/import focus)
                  AND (vi.vessel_type IS NULL OR vi.vessel_type ILIKE '%cargo%')
                  -- anti-tug filter ONLY here (ML layer)
                  AND (vi.vessel_type IS NULL OR vi.vessel_type NOT ILIKE '%tug%')
                  AND (vi.length_m IS NULL OR vi.length_m >= 70);
            """), {"p": port, "d": args.since_days, "wmin": int(args.window_min), "batch_ts": batch_ts})

            n = conn.execute(text("""
                SELECT COUNT(*)
                FROM public.ml_training_samples_multiport
                WHERE port_code=:p AND label_type='TTB'
                  AND label_ts_utc >= (now() AT TIME ZONE 'utc') - (:d || ' days')::interval
            """), {"p": port, "d": args.since_days}).scalar()

            print(f"[TTB] {port}: samples_in_window={n}")

    print("=== DONE BUILD TTB LABELS ===")

if __name__ == "__main__":
    main()

