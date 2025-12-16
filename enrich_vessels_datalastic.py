#!/usr/bin/env python3
import os
import time
import argparse
import logging
import requests
import psycopg2
from psycopg2.extras import execute_batch

# ---------------- CONFIG ----------------

DATALASTIC_URL = "https://api.datalastic.com/api/v0/vessel_info"

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("vessel_enrichment")

# ---------------- ARGUMENTS ----------------

parser = argparse.ArgumentParser(description="Enrich missing vessels using Datalastic")
parser.add_argument("--ports", required=True, help="Comma-separated port codes")
parser.add_argument("--since-days", type=int, default=365)
parser.add_argument("--enrich-vessels", action="store_true")
parser.add_argument("--enrich-limit", type=int, default=0, help="0 = no limit")
parser.add_argument("--enrich-throttle-rpm", type=int, default=300)
args = parser.parse_args()

if not args.enrich_vessels:
    log.info("Nothing to do (use --enrich-vessels)")
    exit(0)

# ---------------- ENV ----------------

DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("DATALASTIC_API_KEY")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

if not API_KEY:
    raise RuntimeError("DATALASTIC_API_KEY not set")

# ---------------- DB ----------------

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = False
cur = conn.cursor()

# ---------------- SELECT MMSIs ----------------

log.info("Selecting missing numeric MMSIs for enrichment...")

cur.execute("""
    SELECT DISTINCT ap.mmsi::bigint
    FROM public.ais_positions ap
    LEFT JOIN public.vessel_info vi
      ON vi.mmsi = ap.mmsi::bigint
    WHERE
      ap.mmsi ~ '^[0-9]{7,9}$'
      AND vi.mmsi IS NULL
""")

rows = cur.fetchall()
mmsis = [r[0] for r in rows]

if args.enrich_limit > 0:
    mmsis = mmsis[:args.enrich_limit]

log.info(f"Found {len(mmsis)} MMSIs to enrich")

if not mmsis:
    log.info("Nothing to enrich. Done.")
    exit(0)

# ---------------- ENRICH ----------------

rpm = max(1, args.enrich_throttle_rpm)
sleep_s = 60.0 / rpm

UPSERT_SQL = """
INSERT INTO public.vessel_info (
    mmsi, imo, vessel_type, vessel_type_specific,
    deadweight, gross_tonnage,
    length_m, beam_m,
    draught_avg, draught_max,
    year_built, callsign,
    country_iso, source, last_updated
) VALUES (
    %(mmsi)s, %(imo)s, %(vessel_type)s, %(vessel_type_specific)s,
    %(deadweight)s, %(gross_tonnage)s,
    %(length_m)s, %(beam_m)s,
    %(draught_avg)s, %(draught_max)s,
    %(year_built)s, %(callsign)s,
    %(country_iso)s, 'datalastic', now()
)
ON CONFLICT (mmsi) DO UPDATE SET
    imo = EXCLUDED.imo,
    vessel_type = EXCLUDED.vessel_type,
    vessel_type_specific = EXCLUDED.vessel_type_specific,
    deadweight = EXCLUDED.deadweight,
    gross_tonnage = EXCLUDED.gross_tonnage,
    length_m = EXCLUDED.length_m,
    beam_m = EXCLUDED.beam_m,
    draught_avg = EXCLUDED.draught_avg,
    draught_max = EXCLUDED.draught_max,
    year_built = EXCLUDED.year_built,
    callsign = EXCLUDED.callsign,
    country_iso = EXCLUDED.country_iso,
    last_updated = now();
"""

payloads = []

for i, mmsi in enumerate(mmsis, 1):
    log.info(f"[{i}/{len(mmsis)}] Fetching MMSI {mmsi}")

    try:
        r = requests.get(
            DATALASTIC_URL,
            params={"api-key": API_KEY, "mmsi": mmsi},
            timeout=30
        )
        r.raise_for_status()
        data = r.json().get("data")

        if not data:
            log.warning(f"No data for MMSI {mmsi}")
            continue

        payloads.append({
            "mmsi": int(mmsi),
            "imo": data.get("imo"),
            "vessel_type": data.get("type"),
            "vessel_type_specific": data.get("type_specific"),
            "deadweight": data.get("deadweight"),
            "gross_tonnage": data.get("gross_tonnage"),
            "length_m": data.get("length"),
            "beam_m": data.get("breadth"),
            "draught_avg": data.get("draught_avg"),
            "draught_max": data.get("draught_max"),
            "year_built": data.get("year_built"),
            "callsign": data.get("callsign"),
            "country_iso": data.get("country_iso"),
        })

    except Exception as e:
        log.error(f"MMSI {mmsi} failed: {e}")

    time.sleep(sleep_s)

# ---------------- UPSERT ----------------

if payloads:
    log.info(f"Upserting {len(payloads)} vessels into vessel_info")
    execute_batch(cur, UPSERT_SQL, payloads, page_size=50)
    conn.commit()
else:
    log.warning("No vessels enriched")

cur.close()
conn.close()

log.info("Vessel enrichment completed successfully")
