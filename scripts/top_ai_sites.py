"""
Finds .ai domains that rank directly in Tranco's global top-sites list --
i.e. real .ai websites people actually query, not derived by guessing
{name}.ai for a popular .com. This is what powers the "most active / top
ranked .ai sites" panel.

Run: python scripts/top_ai_sites.py --scan 1000000 --top 50
Requires DATABASE_URL env var (falls back to local sqlite for dev).
"""
import argparse
import os
import sys
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import Base, TopAiSite  # noqa: E402
from scripts.tranco_check import fetch_tranco  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)


def run(scan: int, top: int):
    Base.metadata.create_all(engine)
    session = Session()

    raw = fetch_tranco(scan)
    ai_sites = [(rank, d) for rank, d in raw if d.lower().endswith(".ai")]
    ai_sites.sort(key=lambda x: x[0])
    ai_sites = ai_sites[:top]

    print(f"[info] scanned top {scan} Tranco ranks, found {len(ai_sites)} "
          f".ai domains ranked directly (requested top {top})")

    seen = set()
    for rank, domain in ai_sites:
        domain = domain.lower().strip()
        seen.add(domain)
        row = session.query(TopAiSite).filter_by(domain=domain).first()
        if row:
            row.tranco_rank = rank
            row.checked_at = datetime.utcnow()
        else:
            session.add(TopAiSite(domain=domain, tranco_rank=rank,
                                   checked_at=datetime.utcnow()))
        print(f"[ok] #{rank:>7}  {domain}")

    # Drop any previously-stored site that fell out of the current top-N
    # (Tranco rankings shift day to day)
    for row in session.query(TopAiSite).all():
        if row.domain not in seen:
            session.delete(row)

    session.commit()
    session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", type=int, default=1_000_000,
                     help="how many raw Tranco ranks to scan through")
    ap.add_argument("--top", type=int, default=50,
                     help="how many top-ranked .ai domains to keep")
    args = ap.parse_args()
    run(args.scan, args.top)
