"""
plentyfish.ai — data models

Three domains of data, kept intentionally separate because their
reliability is very different:

1. AnguillaRevenue   — real, published, periodically-updated figures
                        (government budget docs / press reporting).
2. DiscoveredDomain   — .ai names surfaced by a licensed crawl-dataset
                        vendor. Labeled "discovered", never "registered",
                        because that's what the underlying data actually is.
3. TrancoCheck        — RDAP-verified availability of {topsite}.ai for the
                        Tranco top-N list. This is ground truth (RDAP is
                        authoritative per-domain), just not a discovery feed.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Date, DateTime, Boolean,
    Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class AnguillaRevenue(Base):
    """
    Published .ai revenue figures for Anguilla, by period.
    Seeded from public reporting (see scripts/seed_anguilla_revenue.py for
    sources). This table is small and hand-curated/updated — it is NOT
    meant to be a daily-granularity feed, because Anguilla itself only
    reports monthly/annual figures.
    """
    __tablename__ = "anguilla_revenue"

    id = Column(Integer, primary_key=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    period_label = Column(String(32), nullable=False)   # e.g. "2025", "2025-08"
    granularity = Column(String(16), nullable=False)     # "year" | "month"
    revenue_usd = Column(Float, nullable=False)
    revenue_ecd = Column(Float, nullable=True)
    total_registrations_cumulative = Column(BigInteger, nullable=True)
    source_url = Column(String(512), nullable=True)
    source_note = Column(Text, nullable=True)
    is_projection = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("period_label", "granularity", name="uq_period"),
    )


class DiscoveredDomain(Base):
    """
    A .ai domain surfaced via a licensed third-party crawl-dataset feed
    (WhoisXML API / domains-monitor.com / NetAPI — vendor TBD).

    IMPORTANT: `discovered_at` is when OUR pipeline first saw this name in
    the vendor feed, not necessarily its actual registration date, since
    crawl-based vendors don't have authoritative creation dates for .ai
    (the registry doesn't publish a zone file). Never render this as
    "registered on X" in the UI — always "discovered on X".
    """
    __tablename__ = "discovered_domains"

    id = Column(Integer, primary_key=True)
    domain = Column(String(255), nullable=False, unique=True, index=True)
    discovered_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    vendor = Column(String(64), nullable=False)           # which feed surfaced it
    vendor_reported_created_date = Column(Date, nullable=True)  # if vendor supplies one
    rdap_confirmed = Column(Boolean, default=False)        # spot-verified via RDAP
    rdap_checked_at = Column(DateTime, nullable=True)


class TrancoCheck(Base):
    """
    Result of checking {name}.ai via RDAP for a domain from the Tranco
    top-sites list. This is authoritative (RDAP is a live per-domain
    lookup against the real registry), refreshed on a rolling schedule.
    """
    __tablename__ = "tranco_checks"

    id = Column(Integer, primary_key=True)
    com_domain = Column(String(255), nullable=False)       # e.g. "google.com"
    ai_candidate = Column(String(255), nullable=False)      # e.g. "google.ai"
    tranco_rank = Column(Integer, nullable=True)
    ai_registered = Column(Boolean, nullable=False)
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    rdap_raw_status = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("ai_candidate", name="uq_ai_candidate"),
    )
