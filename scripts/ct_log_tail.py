"""
Direct Certificate Transparency log tailing -- reads raw CT logs
ourselves instead of depending on a third-party aggregator (crt.sh,
MerkleMap, etc). Fully free, no API keys, no rate-limited proxy.

HOW IT WORKS
------------
CT logs are append-only, publicly-readable HTTP APIs (RFC 6962). Google
publishes the canonical list of currently-operating ("usable") logs at
https://www.gstatic.com/ct/log_list/v3/log_list.json -- operators include
Google, Cloudflare, DigiCert, Sectigo, Let's Encrypt, and others. Each log
exposes:
  GET {log_url}ct/v1/get-sth        -> current tree_size
  GET {log_url}ct/v1/get-entries?start=X&end=Y  -> raw leaf entries

Each entry's `leaf_input` (base64) contains, per RFC 6962, either a full
X.509 certificate or a "precertificate" TBSCertificate, embedded as DER
bytes within a small fixed-format header (version/timestamp/entry_type/
length prefix).

EXTRACTION APPROACH (a deliberate simplification)
--------------------------------------------------
Properly parsing every certificate would mean handling two different DER
structures (full X.509 for x509_entry, bare TBSCertificate for
precert_entry) and, for the latter, either a custom ASN.1 walk to find
the SAN extension or reconstructing a fake certificate wrapper so a
library like `cryptography` can parse it. That's real engineering, but a
much cheaper and equally reliable technique works here: domain names in a
DER-encoded certificate are stored as literal ASCII bytes (IA5String).
Since we only care about names ending in ".ai", we can regex-scan the raw
decoded leaf bytes directly for domain-shaped ".ai" substrings -- no
ASN.1 parsing required, and it works identically for both entry types
since the matching bytes are physically present in the buffer either way.

Trade-off: this can't distinguish "this is definitely a SAN dNSName" from
"a domain-shaped string happened to appear somewhere in the DER" -- in
practice this is extremely rare for false positives (DER framing bytes
around real strings are non-printable, so a clean domain-shaped ASCII
run essentially only occurs in an actual string field), and we apply the
same is_apex_dot_ai() filter used for the crt.sh path afterward.

CHECKPOINTING
-------------
Each log's tree_size only grows; we track tree_size_processed per log in
CTLogCheckpoint and only fetch new entries past that point. On first run
for a given log we seed the checkpoint near (but not at) the log's
current tip -- we're tailing new activity, not backfilling a log's entire
multi-billion-entry history.

Run: python scripts/ct_log_tail.py --entry-budget 20000 --initial-lookback 3000
Requires DATABASE_URL env var (falls back to local sqlite for dev).
"""
import argparse
import base64
import os
import re
import sys
import time
from datetime import datetime

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import Base, DiscoveredDomain, CTLogCheckpoint  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"
UA = "plentyfish.ai CT log tail (contact: noel@plentyfish.ai)"
GET_ENTRIES_BATCH = 512  # per-request page size; logs may return fewer

# Matches domain-shaped ASCII ending in .ai, as it appears literally in
# DER-encoded IA5Strings. Word-bounded so we don't match ".aiwannabe" etc.
AI_DOMAIN_RE = re.compile(rb"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+ai\b")


def fetch_usable_logs():
    r = requests.get(LOG_LIST_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    data = r.json()
    logs = []
    for operator in data.get("operators", []):
        for log in operator.get("logs", []):
            if "usable" in log.get("state", {}):
                logs.append({
                    "url": log["url"].rstrip("/") + "/",
                    "name": f"{operator.get('name', '?')} / {log.get('description', log['url'])}",
                })
    return logs


def get_sth(log_url, timeout=15):
    r = requests.get(log_url + "ct/v1/get-sth", headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.json()["tree_size"]


def get_entries(log_url, start, end, timeout=30):
    r = requests.get(
        log_url + "ct/v1/get-entries",
        params={"start": start, "end": end},
        headers={"User-Agent": UA}, timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("entries", [])


def is_apex_dot_ai(name: str) -> bool:
    name = name.strip().lower()
    if not name.endswith(".ai") or name.startswith("*."):
        return False
    return name.count(".") == 1


def extract_ai_domains(leaf_input_b64: str):
    try:
        raw = base64.b64decode(leaf_input_b64)
    except Exception:
        return []
    matches = AI_DOMAIN_RE.finditer(raw)
    found = []
    for m in matches:
        try:
            candidate = m.group(0).decode("ascii").lower()
        except Exception:
            continue
        if is_apex_dot_ai(candidate):
            found.append(candidate)
    return found


def process_log(session, log, entry_budget):
    log_url, log_name = log["url"], log["name"]

    try:
        tree_size = get_sth(log_url)
    except Exception as e:
        print(f"[warn] {log_name}: get-sth failed: {e}")
        return 0

    checkpoint = session.query(CTLogCheckpoint).filter_by(log_url=log_url).first()
    if not checkpoint:
        # First time seeing this log: start tailing near the tip, not from
        # entry 0 (which could be billions of irrelevant historical certs).
        initial_lookback = session.info.get("initial_lookback", 3000)
        start_at = max(tree_size - initial_lookback, 0)
        checkpoint = CTLogCheckpoint(log_url=log_url, log_name=log_name,
                                      tree_size_processed=start_at)
        session.add(checkpoint)
        session.commit()
        print(f"[init] {log_name}: seeding checkpoint at {start_at} "
              f"(tree_size={tree_size})")

    if checkpoint.tree_size_processed >= tree_size - 1:
        return 0  # fully caught up

    start = checkpoint.tree_size_processed
    end_limit = min(tree_size - 1, start + entry_budget)
    new_domains = 0
    processed = 0

    while start <= end_limit:
        batch_end = min(start + GET_ENTRIES_BATCH - 1, end_limit)
        try:
            entries = get_entries(log_url, start, batch_end)
        except Exception as e:
            print(f"[warn] {log_name}: get-entries({start},{batch_end}) failed: {e}")
            break
        if not entries:
            break

        for entry in entries:
            for domain in extract_ai_domains(entry.get("leaf_input", "")):
                row = session.query(DiscoveredDomain).filter_by(domain=domain).first()
                if not row:
                    session.add(DiscoveredDomain(
                        domain=domain, discovered_at=datetime.utcnow(),
                        vendor=f"CT log tail: {log_name}",
                    ))
                    new_domains += 1

        processed += len(entries)
        start += len(entries)
        checkpoint.tree_size_processed = start - 1
        checkpoint.updated_at = datetime.utcnow()
        session.commit()

    if processed:
        print(f"[ok] {log_name}: processed {processed} entries "
              f"(now at {checkpoint.tree_size_processed}/{tree_size}), "
              f"found {new_domains} new .ai domains")
    return new_domains


def run(entry_budget: int, initial_lookback: int, max_wall_seconds: int):
    Base.metadata.create_all(engine)
    session = Session()
    session.info["initial_lookback"] = initial_lookback

    logs = fetch_usable_logs()
    print(f"[info] {len(logs)} usable CT logs found")

    start_time = time.time()
    total_new = 0
    for log in logs:
        if time.time() - start_time > max_wall_seconds:
            print(f"[info] wall-clock budget ({max_wall_seconds}s) reached, "
                  f"stopping early -- remaining logs will catch up next run")
            break
        total_new += process_log(session, log, entry_budget)

    session.close()
    print(f"[done] {total_new} new .ai domains discovered this run")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-budget", type=int, default=20000,
                     help="max entries to process per log per run")
    ap.add_argument("--initial-lookback", type=int, default=3000,
                     help="entries behind the tip to start from, for a log seen for the first time")
    ap.add_argument("--max-wall-seconds", type=int, default=600,
                     help="stop starting new logs after this long (partial progress is saved per-log)")
    args = ap.parse_args()
    run(args.entry_budget, args.initial_lookback, args.max_wall_seconds)
