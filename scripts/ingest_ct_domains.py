"""
FREE alternative to paid newly-registered-domains feeds: Certificate
Transparency (CT) logs via crt.sh.

HOW THIS WORKS
---------------
Every publicly-trusted TLS certificate issued to a domain gets logged to
public, append-only CT logs (mandated since ~2018 for all major browsers
to trust a cert). crt.sh (run by Sectigo) is the standard free aggregator.
It exposes a public read-only Postgres interface:

    host=crt.sh  port=5432  dbname=certwatch  user=guest  (no password)

We query for certificate identities ending in ".ai" with an entry_timestamp
newer than the last time we ran, and record each first-seen hostname as a
"discovered" domain.

WHY THIS IS A GOOD PROXY (not the same as "all registrations")
-----------------------------------------------------------------
- A huge share of .ai domains get a cert almost immediately after DNS is
  pointed anywhere (Cloudflare, Vercel, Netlify, GitHub Pages, and most
  registrar parking pages all auto-issue a cert), so first-CT-sighting
  tends to land within hours to a few days of the domain going live.
- It's genuinely free and requires no account, unlike WhoisXML API /
  domains-monitor.com / NetAPI.

WHAT IT MISSES (be upfront about this in the UI, always)
-----------------------------------------------------------
- Pure speculative/investment registrations that are never pointed at a
  live site never get a cert, so they never show up here. .ai in
  particular has a large speculative-registration segment (average
  renewal rate ~90%, high resale prices), so this feed will meaningfully
  UNDERCOUNT true daily registration volume. Treat the Anguilla-published
  aggregate registration/revenue numbers as the "total volume" source of
  truth, and this feed as "confirmed live sightings" only.
- "First seen in CT logs" is not the same as "registration date" — a
  domain could have been registered earlier and only recently gotten a
  certificate (e.g. moved from parked to active). We label it
  `discovered_at`, never `registered_at`, in the schema and UI for
  exactly this reason.
- Wildcard certs (*.example.ai) are excluded below since they don't
  confirm the base apex domain is in use.
- crt.sh is a free community resource with NO uptime SLA. It is known to
  be periodically overloaded/unreachable (confirmed unreachable during
  testing on 2026-07-02). This script must be run on a schedule with
  retries and must fail gracefully — do not treat an empty run as "no new
  domains today", treat it as "couldn't reach crt.sh, try again later"
  and alert accordingly.

USAGE
-----
Run on a cron schedule (e.g. every 15-60 min). Requires DATABASE_URL.
Requires outbound TCP on port 5432 to crt.sh — most standard hosts
(Railway included) allow this, but sandboxed/proxied dev environments
that only permit HTTP(S) egress will NOT be able to reach it (this is
what happened when this script was developed/tested).

    python scripts/ingest_ct_domains.py --since-hours 2
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

import psycopg2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import Base, DiscoveredDomain  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

CRTSH_QUERY = """
    SELECT min(sub.CERTIFICATE_ID) ID,
           array_to_string(array_agg(DISTINCT sub.NAME_VALUE), chr(10)) NAME_VALUE,
           min(sub.ENTRY_TIMESTAMP) ENTRY_TIMESTAMP
        FROM (SELECT cai.CERTIFICATE_ID, cai.NAME_VALUE, cai.ENTRY_TIMESTAMP
                  FROM certificate_and_identities cai
                  WHERE cai.NAME_VALUE ILIKE %s
                    AND cai.ENTRY_TIMESTAMP > %s
                  LIMIT 50000
             ) sub
        GROUP BY sub.CERTIFICATE_ID
        ORDER BY min(sub.ENTRY_TIMESTAMP) ASC;
"""


def connect_crtsh(retries=3, backoff=5):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(
                host="crt.sh", port=5432, dbname="certwatch", user="guest",
                connect_timeout=15,
            )
        except Exception as e:
            last_err = e
            print(f"[warn] crt.sh connect attempt {attempt}/{retries} failed: {e}")
            time.sleep(backoff * attempt)
    raise RuntimeError(f"crt.sh unreachable after {retries} attempts: {last_err}")


def is_apex_dot_ai(name: str) -> bool:
    """Filter out wildcards, subdomains, and non-.ai noise picked up by ILIKE."""
    name = name.strip().lower()
    if not name.endswith(".ai") or name.startswith("*."):
        return False
    # apex-only: exactly one dot before the .ai (e.g. "foo.ai", not "www.foo.ai")
    return name.count(".") == 1


def run(since_hours: int):
    Base.metadata.create_all(engine)
    session = Session()

    since = datetime.utcnow() - timedelta(hours=since_hours)

    try:
        conn = connect_crtsh()
    except RuntimeError as e:
        session.close()
        print(f"[error] {e}")
        print("[error] Skipping this run — crt.sh unreachable. "
              "Do not interpret as zero new domains.")
        # Raise rather than sys.exit() so this is safe to call as a library
        # (e.g. from a Flask admin route) as well as from the CLI below.
        raise

    rows = None
    last_err = None
    for attempt in range(1, 4):
        try:
            cur = conn.cursor()
            cur.execute(CRTSH_QUERY, ("%.ai", since))
            rows = cur.fetchall()
            cur.close()
            break
        except Exception as e:
            last_err = e
            print(f"[warn] crt.sh query attempt {attempt}/3 failed: {e}")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(5 * attempt)
            if attempt < 3:
                conn = connect_crtsh()
    conn.close()

    if rows is None:
        session.close()
        print(f"[error] crt.sh query failed after retries: {last_err}")
        raise RuntimeError(f"crt.sh query failed after retries: {last_err}")

    new_count = 0
    for cert_id, name_value_block, entry_ts in rows:
        for candidate in name_value_block.split("\n"):
            candidate = candidate.strip().lower()
            if not is_apex_dot_ai(candidate):
                continue
            exists = session.query(DiscoveredDomain).filter_by(domain=candidate).first()
            if exists:
                continue
            session.add(DiscoveredDomain(
                domain=candidate,
                discovered_at=entry_ts or datetime.utcnow(),
                vendor="crt.sh (Certificate Transparency)",
                vendor_reported_created_date=None,  # CT logs don't give this
            ))
            new_count += 1

    session.commit()
    session.close()
    print(f"[ok] Scanned {len(rows)} certificates since {since.isoformat()}Z, "
          f"added {new_count} newly-discovered .ai domains.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=int, default=24,
                     help="look back this many hours for new CT log entries")
    args = ap.parse_args()
    try:
        run(args.since_hours)
    except RuntimeError:
        sys.exit(1)
