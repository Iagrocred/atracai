#!/usr/bin/env python3
import os, sys, argparse
import psycopg2

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", required=True)
    ap.add_argument("--since-days", type=int, default=1850)
    ap.add_argument("--lookback-days", type=int, default=20)
    ap.add_argument("--session-gap-hours", type=int, default=12)
    ap.add_argument("--replace-since", action="store_true")
    ap.add_argument("--source-view", default="berth_calls_multiport_v1_ml")
    args = ap.parse_args()

    db = os.getenv("DATABASE_URL")
    if not db:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)

    ports = [p.strip().upper() for p in args.ports.split(",") if p.strip()]
    if not ports:
        print("ERROR: no ports", file=sys.stderr)
        sys.exit(2)

    conn = psycopg2.connect(db)
    conn.autocommit = False
    cur = conn.cursor()

    print("=== BUILD PORT CALLS MULTIPORT (SESSION-FIRST, NO LEAK, SAFE) ===")
    print(f"ports={ports} since_days={args.since_days} lookback_days={args.lookback_days} gap_hours={args.session_gap_hours} replace={args.replace_since} source={args.source_view}")

    for port in ports:
        print(f"\n[PORT_CALLS] port={port}")

        if args.replace_since:
            # Align delete with the unique key window: call_start_utc
            cur.execute("""
                DELETE FROM public.port_calls_multiport
                WHERE port_code=%s
                  AND call_start_utc >= (now() AT TIME ZONE 'utc') - (%s || ' days')::interval
            """, (port, args.since_days))
            conn.commit()
            print("  cleared existing port_calls by call_start_utc window")

        # Ensure roles exist
        cur.execute("""
            SELECT COUNT(*)
            FROM public.port_zone_roles
            WHERE port_code=%s AND active=true
        """, (port,))
        if cur.fetchone()[0] == 0:
            raise SystemExit(f"ERROR: no port_zone_roles for {port}. Define QUEUE/BASIN roles first.")

        # Build QUEUE & BASIN union geometries
        cur.execute("""
            WITH q AS (
              SELECT ST_Union(z.geom) AS geom
              FROM public.port_zones z
              JOIN public.port_zone_roles r
                ON r.port_code=z.port_code AND r.zone_name=z.zone_name
              WHERE z.port_code=%s AND r.active=true AND r.role='QUEUE'
            ),
            b AS (
              SELECT ST_Union(z.geom) AS geom
              FROM public.port_zones z
              JOIN public.port_zone_roles r
                ON r.port_code=z.port_code AND r.zone_name=z.zone_name
              WHERE z.port_code=%s AND r.active=true AND r.role='BASIN'
            )
            SELECT (SELECT geom FROM q), (SELECT geom FROM b);
        """, (port, port))
        qgeom, bgeom = cur.fetchone()
        if qgeom is None:
            raise SystemExit(f"ERROR: No QUEUE geometry for {port} (role='QUEUE').")
        if bgeom is None:
            raise SystemExit(f"ERROR: No BASIN geometry for {port} (role='BASIN').")

        # SESSIONIZE berth calls first. Then compute queue/basin inside session window only.
        sql = f"""
        WITH bc AS (
          SELECT
            port_code,
            mmsi::text AS mmsi_txt,
            berth_id,
            berth_start_utc,
            berth_end_utc,
            alongside_hours
          FROM public.{args.source_view}
          WHERE port_code = %s
            AND berth_start_utc >= (now() AT TIME ZONE 'utc') - (%s || ' days')::interval
            AND mmsi::text ~ '^[0-9]{{7,9}}$'
        ),
        ordered AS (
          SELECT
            *,
            LAG(berth_end_utc) OVER (PARTITION BY port_code, mmsi_txt ORDER BY berth_start_utc) AS prev_end
          FROM bc
        ),
        marked AS (
          SELECT
            *,
            CASE
              WHEN prev_end IS NULL THEN 1
              WHEN EXTRACT(EPOCH FROM (berth_start_utc - prev_end)) > (%s * 3600) THEN 1
              ELSE 0
            END AS is_new_sess
          FROM ordered
        ),
        sess AS (
          SELECT
            *,
            SUM(is_new_sess) OVER (PARTITION BY port_code, mmsi_txt ORDER BY berth_start_utc) AS sess_id
          FROM marked
        ),
        sess_agg AS (
          SELECT
            port_code,
            mmsi_txt,
            sess_id,
            MIN(berth_start_utc) AS first_berth_start_utc,
            MAX(berth_end_utc)   AS last_berth_end_utc,
            (ARRAY_AGG(berth_id ORDER BY berth_start_utc))[1] AS berth_id_first,
            SUM(COALESCE(alongside_hours,0)) AS alongside_hours_sum,
            MIN(berth_start_utc) - (%s || ' days')::interval AS win_start
          FROM sess
          GROUP BY port_code, mmsi_txt, sess_id
        ),
        pts AS (
          SELECT
            s.port_code, s.mmsi_txt, s.sess_id,
            p.timestamp_utc,
            ST_SetSRID(ST_Point(p.lon, p.lat), 4326) AS pt
          FROM sess_agg s
          JOIN public.ais_positions p
            ON p.port_code = s.port_code
           AND p.mmsi::text = s.mmsi_txt
           AND p.timestamp_utc >= s.win_start
           AND p.timestamp_utc <= s.first_berth_start_utc
        ),
        q AS (
          SELECT
            port_code, mmsi_txt, sess_id,
            MIN(timestamp_utc) AS q_start
          FROM pts
          WHERE ST_Contains(%s::geometry, pt)
          GROUP BY port_code, mmsi_txt, sess_id
        ),
        b AS (
          SELECT
            p.port_code, p.mmsi_txt, p.sess_id,
            MIN(p.timestamp_utc) AS basin_start
          FROM pts p
          LEFT JOIN q
            ON q.port_code=p.port_code AND q.mmsi_txt=p.mmsi_txt AND q.sess_id=p.sess_id
          WHERE ST_Contains(%s::geometry, p.pt)
            AND p.timestamp_utc >= COALESCE(q.q_start, (SELECT win_start FROM sess_agg s2 WHERE s2.port_code=p.port_code AND s2.mmsi_txt=p.mmsi_txt AND s2.sess_id=p.sess_id))
          GROUP BY p.port_code, p.mmsi_txt, p.sess_id
        ),
        final AS (
          SELECT
            s.port_code,
            s.mmsi_txt AS mmsi,
            COALESCE(q.q_start, b.basin_start, s.first_berth_start_utc) AS call_start_utc,
            s.last_berth_end_utc AS call_end_utc,
            q.q_start AS anchorage_queue_start_utc,
            b.basin_start AS basin_start_utc,
            s.first_berth_start_utc AS berth_start_utc,
            s.last_berth_end_utc    AS berth_end_utc,
            s.berth_id_first AS berth_id,
            CASE
              WHEN q.q_start IS NOT NULL AND b.basin_start IS NOT NULL
                THEN EXTRACT(EPOCH FROM (b.basin_start - q.q_start))/3600.0
              ELSE NULL
            END AS wait_hours,
            CASE
              WHEN b.basin_start IS NOT NULL
                THEN EXTRACT(EPOCH FROM (s.first_berth_start_utc - b.basin_start))/3600.0
              ELSE NULL
            END AS ttb_hours,
            CASE
              WHEN b.basin_start IS NOT NULL
                THEN EXTRACT(EPOCH FROM (s.first_berth_start_utc - b.basin_start))/3600.0
              ELSE NULL
            END AS time_to_berth_hours,
            s.alongside_hours_sum AS alongside_hours,
            %s::int AS lookback_days
          FROM sess_agg s
          LEFT JOIN q ON q.port_code=s.port_code AND q.mmsi_txt=s.mmsi_txt AND q.sess_id=s.sess_id
          LEFT JOIN b ON b.port_code=s.port_code AND b.mmsi_txt=s.mmsi_txt AND b.sess_id=s.sess_id
        ),
        dedup AS (
          SELECT DISTINCT ON (port_code, mmsi, call_start_utc)
            *
          FROM final
          ORDER BY port_code, mmsi, call_start_utc, berth_start_utc
        )
        INSERT INTO public.port_calls_multiport (
          port_code, mmsi,
          call_start_utc, call_end_utc,
          anchorage_queue_start_utc, basin_start_utc,
          berth_id, berth_start_utc, berth_end_utc,
          wait_hours, ttb_hours, time_to_berth_hours,
          alongside_hours, lookback_days,
          updated_at
        )
        SELECT
          port_code, mmsi,
          call_start_utc, call_end_utc,
          anchorage_queue_start_utc, basin_start_utc,
          berth_id, berth_start_utc, berth_end_utc,
          wait_hours, ttb_hours, time_to_berth_hours,
          alongside_hours, lookback_days,
          now()
        FROM dedup
        ON CONFLICT (port_code, mmsi, call_start_utc) DO UPDATE SET
          call_end_utc = GREATEST(port_calls_multiport.call_end_utc, EXCLUDED.call_end_utc),
          berth_end_utc = GREATEST(port_calls_multiport.berth_end_utc, EXCLUDED.berth_end_utc),
          berth_start_utc = LEAST(port_calls_multiport.berth_start_utc, EXCLUDED.berth_start_utc),
          berth_id = EXCLUDED.berth_id,
          anchorage_queue_start_utc = COALESCE(port_calls_multiport.anchorage_queue_start_utc, EXCLUDED.anchorage_queue_start_utc),
          basin_start_utc = COALESCE(port_calls_multiport.basin_start_utc, EXCLUDED.basin_start_utc),
          wait_hours = EXCLUDED.wait_hours,
          ttb_hours = EXCLUDED.ttb_hours,
          time_to_berth_hours = EXCLUDED.time_to_berth_hours,
          alongside_hours = EXCLUDED.alongside_hours,
          lookback_days = EXCLUDED.lookback_days,
          updated_at = now();
        """

        cur.execute(sql, (
            port,
            args.since_days,
            args.session_gap_hours,
            args.lookback_days,
            qgeom,
            bgeom,
            args.lookback_days
        ))
        conn.commit()

        cur.execute("""
            SELECT COUNT(*) FROM public.port_calls_multiport
            WHERE port_code=%s
              AND call_start_utc >= (now() AT TIME ZONE 'utc') - (%s || ' days')::interval
        """, (port, args.since_days))
        n = cur.fetchone()[0]
        print(f"  [OK] port_calls in window: {n}")

    cur.close()
    conn.close()
    print("\n=== DONE BUILD PORT CALLS MULTIPORT ===")

if __name__ == "__main__":
    main()
