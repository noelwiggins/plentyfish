"""
Seeds AnguillaRevenue with publicly reported figures (as of July 2026).

Sources (see source_url per row):
- Anguilla Focus reporting on 2025 monthly/annual totals
- Sherwood News / government 2026 budget address
- domaintechnik.at .ai domain report (2018-2025 series, Jan 2026 daily rate)
- PYMNTS / Semafor reporting on 2023-2024 figures

Run: python scripts/seed_anguilla_revenue.py
Requires DATABASE_URL env var (falls back to local sqlite for dev).
"""
import os
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import Base, AnguillaRevenue  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):  # Railway gives old-style URL
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

# --- Annual figures, actuals -------------------------------------------------
ANNUAL = [
    # label, revenue_usd, revenue_ecd, cumulative_registrations, source, note
    ("2018", 2_900_000, None, 48_272,
     "https://www.domaintechnik.at/wp-content/uploads/ai-domain-en.pdf",
     "Early baseline, pre-AI-boom."),
    ("2023", 32_000_000, 86_830_000, 353_928,
     "https://www.pymnts.com/artificial-intelligence-2/2026/the-ai-boom-is-funding-a-caribbean-island-two-letters-at-a-time/",
     "~22% of government revenue that year; ~354K domains registered."),
    ("2024", 39_000_000, None, None,
     "https://quasa.io/media/anguilla-s-ai-domain-boom-millions-in-revenue-but-there-s-a-catch",
     "~23% of total government revenue."),
    ("2025", 85_300_000, 230_499_740.50, 1_000_000,
     "https://anguillafocus.com/ai-domain-surge-brings-ec230m-windfall-to-anguilla-in-2025/",
     "More than double 2024; nearly triple 2023. Crossed 1M cumulative "
     "registrations around Jan 2, 2026. ~47% of government revenue."),
]

# --- Monthly 2025 figures (revenue collected in month, for prior month's sales) --
MONTHLY_2025 = [
    ("2025-01", 9_700_000),
    ("2025-04", 21_700_000),
    ("2025-08", 22_400_000),  # peak, for July sales
    ("2025-10", 19_900_000),  # low, for September sales
]

# --- Government's own (superseded) projections, kept for context ------------
PROJECTIONS = [
    ("2026", 96_400_000, None, None,
     "https://sherwood.news/tech/now-more-one-million-ai-websites-contributing-an-estimated-70-million-anguilla-government-revenue/",
     "Government 2026 budget forecast: EC$260.5M (~$96.4M). Jan 2026 daily "
     "reg. rate ~2,008/day, up from 2025 avg of 1,318/day — if sustained, "
     "government has flagged cumulative registrations reaching ~1.7M by "
     "end of 2026."),
]


def month_bounds(yyyymm: str):
    y, m = map(int, yyyymm.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start, end


def run():
    Base.metadata.create_all(engine)
    session = Session()

    for label, usd, ecd, cum, src, note in ANNUAL:
        y = int(label)
        row = session.query(AnguillaRevenue).filter_by(
            period_label=label, granularity="year"
        ).first()
        if row:
            continue
        session.add(AnguillaRevenue(
            period_start=date(y, 1, 1), period_end=date(y, 12, 31),
            period_label=label, granularity="year",
            revenue_usd=usd, revenue_ecd=ecd,
            total_registrations_cumulative=cum,
            source_url=src, source_note=note, is_projection=False,
        ))

    for label, usd in MONTHLY_2025:
        start, end = month_bounds(label)
        row = session.query(AnguillaRevenue).filter_by(
            period_label=label, granularity="month"
        ).first()
        if row:
            continue
        session.add(AnguillaRevenue(
            period_start=start, period_end=end,
            period_label=label, granularity="month",
            revenue_usd=usd, revenue_ecd=None,
            source_url="https://anguillafocus.com/ai-domain-surge-brings-ec230m-windfall-to-anguilla-in-2025/",
            source_note="Monthly collections reported by Anguilla Focus.",
            is_projection=False,
        ))

    for label, usd, ecd, cum, src, note in PROJECTIONS:
        y = int(label)
        row = session.query(AnguillaRevenue).filter_by(
            period_label=label, granularity="year"
        ).first()
        if row:
            continue
        session.add(AnguillaRevenue(
            period_start=date(y, 1, 1), period_end=date(y, 12, 31),
            period_label=label, granularity="year",
            revenue_usd=usd, revenue_ecd=ecd,
            total_registrations_cumulative=cum,
            source_url=src, source_note=note, is_projection=True,
        ))

    session.commit()
    session.close()
    print("Seeded Anguilla revenue data.")


if __name__ == "__main__":
    run()
