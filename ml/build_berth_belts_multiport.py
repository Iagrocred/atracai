#!/usr/bin/env python3
import os
import sys
import argparse
import psycopg2

def get_cols(cur, table_name: str):
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
    """, (table_name,))
    return [r[0] for r in cur.fetchall()]

def colset(cols):
    return set([c.lower() for c in cols])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", required=True, help="Comma-separated ports e.g. STS,PNG")
    ap.add_argument("--buffer-m", type=float, default=60.0, help="Buffer meters around berth polygon to create belt geom")
    ap.add_argument("--replace", action="store_true", help="Delete existing belts for port before inserting")
    args = ap.parse_args()

    db = os.getenv("DATABASE_URL")
    if not db:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)

    ports = [p.strip() for p in args.ports.split(",") if p.strip()]
    if not ports:
        print("ERROR: --ports empty", file=sys.stderr)
        sys.exit(2)

    print("=== BUILD BERTH BELTS MULTIPORT ===")
    print(f"Ports={ports} buffer_m={args.buffer_m} replace={args.replace}")

    conn = psycopg2.connect(db)
    conn.autocommit = False
    cur = conn.cursor()

    belts_cols = get_cols(cur, "berth_belts_multiport")
    berths_cols = get_cols(cur, "berth_polygons")
    if not belts_cols:
        raise SystemExit("ERROR: berth_belts_multiport not found in public schema")
    if not berths_cols:
        raise SystemExit("ERROR: berth_polygons not found in public schema")

    belts = colset(belts_cols)

    # Determine which columns we can write
    writable = []
    if "port_code" in belts: writable.append("port_code")
    if "belt_id" in belts: writable.append("belt_id")
    if "berth_id" in belts: writable.append("berth_id")
    if "radius_m" in belts: writable.append("radius_m")
    if "confidence" in belts: writable.append("confidence")
    if "geom" in belts: writable.append("geom")
    if "center_lat" in belts: writable.append("center_lat")
    if "center_lon" in belts: writable.append("center_lon")

    if "port_code" not in belts or "belt_id" not in belts or "geom" not in belts:
        raise SystemExit("ERROR: berth_belts_multiport must have at least (port_code, belt_id, geom)")

    def insert_belt(row):
        """
        row dict contains possible keys matching writable columns.
        Build INSERT dynamically.
        """
        cols = [c for c in writable if c in row]
        placeholders = [f"%({c})s" for c in cols]
        sql = f"INSERT INTO berth_belts_multiport ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
        # Try safe upsert if unique exists; otherwise just DO NOTHING
        # Most installs have UNIQUE(port_code, belt_id)
        sql += " ON CONFLICT DO NOTHING"
        cur.execute(sql, row)

    for port in ports:
        print(f"\n[BELTS] port={port}")

        # Decide source preference: refined if exists else inferred
        cur.execute("""
            SELECT COUNT(*)
            FROM berth_polygons
            WHERE port_code=%s AND source='ais_refined'
        """, (port,))
        refined_count = cur.fetchone()[0]

        if refined_count > 0:
            src = "ais_refined"
            print(f"  using source={src} (count={refined_count})")
            berth_where = "port_code=%s AND source='ais_refined'"
            params = (port,)
        else:
            src = "ais_inferred"
            print("  no refined berths found; falling back to ais_inferred with area<=0.5 km² filter")
            berth_where = "port_code=%s AND source='ais_inferred' AND ST_Area(geom::geography)/1e6 <= 0.5"
            params = (port,)

        if args.replace:
            cur.execute("DELETE FROM berth_belts_multiport WHERE port_code=%s", (port,))
            conn.commit()
            print("  cleared existing belts")

        # Fetch berth polygons + centroid for optional fields
        cur.execute(f"""
            SELECT
              berth_id,
              ST_AsText(geom) as wkt,
              ST_Y(ST_Centroid(geom)) AS cy,
              ST_X(ST_Centroid(geom)) AS cx
            FROM berth_polygons
            WHERE {berth_where}
            ORDER BY berth_id
        """, params)
        berths = cur.fetchall()

        print(f"  berth_polygons selected={len(berths)}")
        if len(berths) == 0:
            continue

        created = 0
        for berth_id, wkt, cy, cx in berths:
            belt_id = berth_id.replace("BERTH", "BELT") if "BERTH" in berth_id else f"{berth_id}_BELT"

            # Build belt geom from berth polygon buffer
            # We pass WKT to PostGIS to avoid psycopg2 geometry adapters.
            cur.execute("""
                SELECT ST_AsGeoJSON(
                    ST_Buffer(
                        ST_GeomFromText(%s, 4326)::geography,
                        %s
                    )::geometry
                )
            """, (wkt, args.buffer_m))
            belt_geojson = cur.fetchone()[0]

            conf = 0.90 if src == "ais_refined" else 0.50

            row = {
                "port_code": port,
                "belt_id": belt_id,
                "geom": belt_geojson,
            }
            # Optional columns if exist
            if "berth_id" in belts:
                row["berth_id"] = berth_id
            if "radius_m" in belts:
                row["radius_m"] = float(args.buffer_m)
            if "confidence" in belts:
                row["confidence"] = float(conf)
            if "center_lat" in belts:
                row["center_lat"] = float(cy) if cy is not None else None
            if "center_lon" in belts:
                row["center_lon"] = float(cx) if cx is not None else None

            # Insert using GeoJSON -> geometry in SQL if geom column expects geometry
            # If geom is geometry type, inserting GeoJSON string directly may fail.
            # So we convert inside SQL using ST_GeomFromGeoJSON when geom column exists.
            if "geom" in row:
                # We rewrite the insert to cast geom properly:
                cols = [c for c in writable if c in row and c != "geom"]
                cols_with_geom = cols + ["geom"]
                placeholders = [f"%({c})s" for c in cols] + ["ST_SetSRID(ST_GeomFromGeoJSON(%(geom)s),4326)"]

                sql = f"INSERT INTO berth_belts_multiport ({', '.join(cols_with_geom)}) VALUES ({', '.join(placeholders)}) ON CONFLICT DO NOTHING"
                cur.execute(sql, row)
            else:
                insert_belt(row)

            created += 1

        conn.commit()
        print(f"  [OK] created/attempted={created} belts (source={src})")

    cur.close()
    conn.close()
    print("\n=== DONE BUILD BERTH BELTS MULTIPORT ===")

if __name__ == "__main__":
    main()

