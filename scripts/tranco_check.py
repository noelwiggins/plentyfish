"""
Checks whether {name}.ai is registered for the top N domains in the Tranco
list (a research-grade, non-gameable top-sites ranking — tranco-list.eu).

Uses RDAP per Noel's standing rule: GET https://rdap.org/domain/{domain}
  - 404  -> available
  - 200  -> registered

This is a live, per-domain lookup, so it's slow-ish for large N (RDAP is
rate-limited and courteous crawling matters). Default N=2000, refreshed on
a rolling schedule (e.g. 200/day via cron) rather than all at once.

Run: python scripts/tranco_check.py --limit 200 --offset 0
Requires DATABASE_URL env var (falls back to local sqlite for dev).
"""
import argparse
import csv
import io
import os
import sys
import time
import zipfile
from datetime import datetime

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import Base, TrancoCheck  # noqa: E402

TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
RDAP_URL = "https://rdap.org/domain/{}"
UA = "plentyfish.ai research bot (contact: noel@plentyfish.ai)"

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

# Small offline fallback list in case the Tranco download is unreachable
# (e.g. sandboxed environments). Real deployment should always hit Tranco.
FALLBACK_TOP = [
    "google.com", "youtube.com", "facebook.com", "instagram.com", "x.com",
    "wikipedia.org", "amazon.com", "yahoo.com", "reddit.com", "tiktok.com",
    "whatsapp.com", "linkedin.com", "netflix.com", "microsoft.com",
    "apple.com", "openai.com", "chatgpt.com", "pinterest.com", "ebay.com",
    "twitch.tv", "spotify.com", "zoom.us", "salesforce.com", "adobe.com",
    "dropbox.com", "shopify.com", "stripe.com", "airbnb.com", "uber.com",
    "notion.so",
]


def fetch_tranco(limit: int):
    try:
        resp = requests.get(TRANCO_URL, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        name = z.namelist()[0]
        rows = []
        with z.open(name) as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
            for rank, row in enumerate(reader, start=1):
                if rank > limit:
                    break
                rows.append((rank, row[1]))  # (rank, domain)
        return rows
    except Exception as e:
        print(f"[warn] Tranco download failed ({e}); using fallback list.")
        return [(i + 1, d) for i, d in enumerate(FALLBACK_TOP[:limit])]


def base_name(com_domain: str) -> str:
    # strip a single leading "www." and the TLD, keep second-level label only
    d = com_domain.lower().strip()
    if d.startswith("www."):
        d = d[4:]
    return d.rsplit(".", 1)[0]


# Tranco ranks by raw DNS query volume, which is dominated by invisible
# backend infrastructure (CDN edge nodes, ad-tech/analytics beacons, OS
# telemetry, DNS infra, IoT device backends) rather than sites a person
# actually visits. We filter those out so "unclaimed .ai" reads as
# recognizable consumer brands, not plumbing.
#
# This is a denylist of substrings matched against the base (second-level)
# label. It's necessarily a heuristic/curated list, not exhaustive -- new
# infra patterns will show up over time and can be added here.
INFRA_DENYLIST = [
    # CDN / edge / static-asset hosts
    "cdn", "edgekey", "edgesuite", "edgecast", "akamai", "akadns", "akam",
    "fastly", "cloudflare", "cloudfront", "stackpath", "cachefly",
    "gstatic", "ytimg", "twimg", "fbcdn", "staticflickr", "ggpht",
    "mzstatic", "googleusercontent",
    # ad-tech / analytics / tracking SDKs
    "doubleclick", "googlesyndication", "googletagmanager", "google-analytics",
    "googleadservices", "adservice", "scorecardresearch", "adnxs", "criteo",
    "taboola", "outbrain", "moatads", "adsrvr", "rubiconproject", "pubmatic",
    "appsflyersdk", "adjust", "branch.io", "segment", "mixpanel", "amplitude",
    "quantserve", "adform", "casalemedia",
    # DNS / registry infra
    "gtld-servers", "root-servers", "nsone", "ultradns", "akadns", "ripn",
    "verisign-grs", "googledomains",
    # OS / device background services (not something a user "visits")
    "windowsupdate", "msedge", "gvt1", "gvt2", "ntp", "apple-dns",
    "captive.apple", "push.apple", "myfritz",
    # IoT / device manufacturer backends
    "ezviz", "hicloudcam", "hicloud",
]


def is_consumer_facing(com_domain: str) -> bool:
    name = base_name(com_domain)
    return not any(pattern in name for pattern in INFRA_DENYLIST)
    for attempt in range(retries):
        try:
            r = requests.get(RDAP_URL.format(domain), headers={"User-Agent": UA},
                              timeout=timeout)
            if r.status_code == 404:
                return False, "404 not found (available)"
            if r.status_code == 200:
                return True, "200 (registered)"
            if r.status_code == 429:
                wait = 3 * (attempt + 1)
                print(f"[warn] 429 on {domain}, backing off {wait}s")
                time.sleep(wait)
                continue
            return None, f"unexpected status {r.status_code}"
        except Exception as e:
            return None, f"error: {e}"
    return None, "429 persisted after retries"


def cleanup_infra(session):
    """Remove any previously-stored TrancoCheck rows that fail the
    consumer-facing filter (added after the filter was introduced -- this
    cleans up rows saved before the filter existed)."""
    removed = 0
    for row in session.query(TrancoCheck).all():
        if not is_consumer_facing(row.com_domain):
            session.delete(row)
            removed += 1
    if removed:
        session.commit()
        print(f"[cleanup] removed {removed} pre-existing infra/CDN/tracking rows")


def run(limit: int, offset: int, sleep_s: float):
    Base.metadata.create_all(engine)
    session = Session()

    cleanup_infra(session)
    # Scan a generously larger window than requested, since a meaningful
    # fraction of raw Tranco entries are infra and get filtered out. Grow
    # the scan window if we don't find enough consumer-facing candidates.
    scan_size = (offset + limit) * 4
    max_scan_size = 20000
    consumer_ranked = []
    while True:
        raw = fetch_tranco(scan_size)
        consumer_ranked = [(rank, d) for rank, d in raw if is_consumer_facing(d)]
        if len(consumer_ranked) >= offset + limit or scan_size >= max_scan_size:
            break
        scan_size = min(scan_size * 2, max_scan_size)

    ranked = consumer_ranked[offset:offset + limit]
    print(f"[info] scanned {scan_size} raw Tranco ranks, found "
          f"{len(consumer_ranked)} consumer-facing candidates, "
          f"using {len(ranked)} of them (offset={offset}, limit={limit})")

    for rank, com_domain in ranked:
        candidate = f"{base_name(com_domain)}.ai"
        registered, raw_status = rdap_check(candidate)
        if registered is None:
            print(f"[skip] {candidate}: {raw_status}")
            time.sleep(sleep_s)
            continue

        row = session.query(TrancoCheck).filter_by(ai_candidate=candidate).first()
        if row:
            row.tranco_rank = rank
            row.ai_registered = registered
            row.checked_at = datetime.utcnow()
            row.rdap_raw_status = raw_status
        else:
            session.add(TrancoCheck(
                com_domain=com_domain, ai_candidate=candidate,
                tranco_rank=rank, ai_registered=registered,
                checked_at=datetime.utcnow(), rdap_raw_status=raw_status,
            ))
        session.commit()
        print(f"[ok] #{rank:>5}  {candidate:<30} {'REGISTERED' if registered else 'AVAILABLE'}")
        time.sleep(sleep_s)

    session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.2,
                     help="seconds between RDAP calls (be courteous; rdap.org "
                          "returns 429 if hit faster than ~1/sec)")
    args = ap.parse_args()
    run(args.limit, args.offset, args.sleep)
