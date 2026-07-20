"""
Seeds AnguillaRevenue with publicly reported figures (as of July 2026).

Sources (see source_url per row):
- Anguilla Focus reporting on 2025 monthly/annual totals
- Sherwood News / government 2026 budget address
- domaintechnik.at .ai domain report (2018-2026 series, monthly run-rate)
- PYMNTS / TechFlow / Gulf News / IMF reporting across 2021-2026
- Government's own prior-cycle forecasts (2025/2026/2027), for the
  historical-undershoot evidence used in the projection scenarios below

Run: python scripts/seed_anguilla_revenue.py
Requires DATABASE_URL env var (falls back to local sqlite for dev).

NOTE ON UPSERT BEHAVIOR: this script now updates every field on existing
rows to match the values below, not just inserts missing rows. That means
correcting a figure here (e.g. if a better-sourced number turns up later)
will actually take effect on the next run/deploy, rather than silently
being ignored because a row already exists.
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
    # label, revenue_usd, revenue_ecd, cumulative_registrations, pct_of_govt_revenue, source, note
    ("2018", 2_900_000, None, 48_272, None,
     "https://www.domaintechnik.at/wp-content/uploads/ai-domain-en.pdf",
     "Early baseline, pre-AI-boom."),
    ("2021", 7_400_000, None, None, None,
     "https://gulfnews.com/business/markets/how-this-tropical-island-is-set-to-make-millions-by-selling-domain-addresses-1.1693481302426",
     "Pre-ChatGPT baseline, for scale. Registrations reached 144,000 by 2022."),
    ("2023", 32_000_000, 86_830_000, 353_928, 20.0,
     "https://www.pymnts.com/artificial-intelligence-2/2026/the-ai-boom-is-funding-a-caribbean-island-two-letters-at-a-time/",
     "~20-22% of government revenue that year (source estimates vary "
     "slightly); ~354K domains registered, up from ~144K in 2022."),
    ("2024", 39_000_000, None, None, 25.0,
     "https://quasa.io/media/anguilla-s-ai-domain-boom-millions-in-revenue-but-there-s-a-catch",
     "~23-25% of total government revenue (source estimates vary slightly)."),
    ("2025", 85_300_000, 230_499_740.50, 1_000_000, 47.0,
     "https://anguillafocus.com/ai-domain-surge-brings-ec230m-windfall-to-anguilla-in-2025/",
     "Anguilla Focus reports $85.3M (EC$230.5M); TechFlow separately "
     "reports $93M (EC$250M) for the same year -- sources don't fully "
     "agree, treat 2025 as $85-93M rather than a single precise figure. "
     "Crossed 1M cumulative registrations Dec 31, 2025 / Jan 1, 2026. "
     "~47% of government revenue. Critically: the government's OWN prior "
     "forecast for 2025 (made based on earlier trends) was only EC$132M "
     "(~$48.8M) -- actual came in at ~1.75-1.9x that forecast. This "
     "specific, well-sourced undershoot is the strongest evidence for "
     "treating official forward projections skeptically."),
]

# --- Monthly 2025 figures (revenue collected in month, for prior month's sales) --
MONTHLY_2025 = [
    ("2025-01", 9_700_000),
    ("2025-04", 21_700_000),
    ("2025-08", 22_400_000),  # peak, for July sales
    ("2025-10", 19_900_000),  # low, for September sales
]

# --- 2026 outlook: three explicit scenarios, not one point estimate ---------
# Government forecasts have a demonstrated, well-sourced history of
# undershooting substantially (see 2025 note above), so a single number
# here would be misleading. period_start dates are staggered by a day
# purely to force a stable display order for same-year rows; it has no
# calendar meaning.
PROJECTIONS_2026 = [
    ("2026 (govt.)", date(2026, 1, 1), 96_400_000, None, None,
     "https://sherwood.news/tech/now-more-one-million-ai-websites-contributing-an-estimated-70-million-anguilla-government-revenue/",
     "Government's current official 2026 budget forecast: EC$260.5M "
     "(~$96.4M) -- only ~13% growth over 2025's $85.3M figure (or as "
     "little as ~4% over the $93M TechFlow figure). Given the government's "
     "own forecasts undershot 2025 by ~1.75-1.9x, and independent "
     "reporting from June 2026 describes the .ai boom as \"not slowing "
     "down, it is accelerating,\" this is very likely conservative."),
    ("2026 (trend)", date(2026, 1, 2), 100_000_000, None, None,
     "https://www.domaintechnik.at/wp-content/uploads/ai-domain-en.pdf",
     "Estimated from a reported ~EC$20-22M/month (~$7.7M/month) revenue "
     "run-rate that domaintechnik described as a 'stabilized' level "
     "reached at some point in the 2026 cycle, annualized (~$92-102M), "
     "before the same source describes renewed acceleration on top of "
     "that. Treat as a plausible floor for 2026, not a ceiling."),
    ("2026 (upper, unverified tip)", date(2026, 1, 3), 178_000_000, None, None,
     None,
     "NOT from published reporting -- reflects a claim relayed secondhand "
     "(\"a government insider says registrations are doubling every "
     "year\"), shown here only as an upper bound, not a verified figure. "
     "If revenue simply doubled 2025->2026 (using the ~$85.3-93M range), "
     "that's ~$170.6-186M; midpoint ~$178M shown. Actual historical "
     "year-over-year revenue growth has been strong but irregular -- "
     "roughly +22% (2023->2024) then +119% to +138% (2024->2025, partly "
     "a one-time effect of 2023 registrants hitting their first 2-year "
     "renewal in 2025) -- not a clean doubling pattern, so treat this "
     "scenario as illustrative of 'if the insider is right,' not a "
     "prediction we're independently standing behind."),
]


def month_bounds(yyyymm: str):
    y, m = map(int, yyyymm.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start, end


def upsert_year_row(session, label, period_start, usd, ecd, cum, pct, src, note, is_projection):
    row = session.query(AnguillaRevenue).filter_by(
        period_label=label, granularity="year"
    ).first()
    period_end = date(period_start.year, 12, 31)
    if row:
        row.period_start = period_start
        row.period_end = period_end
        row.revenue_usd = usd
        row.revenue_ecd = ecd
        row.total_registrations_cumulative = cum
        row.pct_of_govt_revenue = pct
        row.source_url = src
        row.source_note = note
        row.is_projection = is_projection
    else:
        session.add(AnguillaRevenue(
            period_start=period_start, period_end=period_end,
            period_label=label, granularity="year",
            revenue_usd=usd, revenue_ecd=ecd,
            total_registrations_cumulative=cum, pct_of_govt_revenue=pct,
            source_url=src, source_note=note, is_projection=is_projection,
        ))


def run():
    Base.metadata.create_all(engine)
    session = Session()

    for label, usd, ecd, cum, pct, src, note in ANNUAL:
        y = int(label)
        upsert_year_row(session, label, date(y, 1, 1), usd, ecd, cum, pct,
                         src, note, is_projection=False)

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

    # Remove the old single-point 2026 projection row if it's still there
    # from before the multi-scenario model existed.
    old = session.query(AnguillaRevenue).filter_by(
        period_label="2026", granularity="year"
    ).first()
    if old:
        session.delete(old)

    for label, period_start, usd, ecd, cum, src, note in PROJECTIONS_2026:
        upsert_year_row(session, label, period_start, usd, ecd, cum, None,
                         src, note, is_projection=True)

    session.commit()
    session.close()
    print("Seeded Anguilla revenue data.")


if __name__ == "__main__":
    run()
