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
    pct_of_govt_revenue = Column(Float, nullable=True)
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


class TopAiSite(Base):
    """
    .ai domains that appear DIRECTLY in the Tranco top-sites list (i.e.
    ranked on their own global DNS popularity, not derived from checking
    a .com name). This is real ranking data -- Tranco ranks every domain
    it sees regardless of TLD -- so a .ai domain showing up here means it
    genuinely gets meaningful traffic/DNS query volume worldwide.

    Framed as "most active / top ranked .ai sites", not "most visited"
    (Tranco measures DNS resolution volume across contributing recursive
    resolvers, which correlates with but isn't identical to visits).
    """
    __tablename__ = "top_ai_sites"

    id = Column(Integer, primary_key=True)
    domain = Column(String(255), nullable=False, unique=True, index=True)
    tranco_rank = Column(Integer, nullable=False)
    checked_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("domain", name="uq_top_ai_domain"),
    )


class CTLogCheckpoint(Base):
    """
    Tracks how far we've read into each individual CT log (direct tailing,
    not going through crt.sh). Each usable log from Google's official CT
    log list (gstatic log_list.json) is a separate append-only stream with
    its own tree_size; we record the last index processed so each cron run
    picks up where the previous one left off instead of re-scanning from
    the start (individual logs can have billions of historical entries --
    we only care about new ones going forward).
    """
    __tablename__ = "ct_log_checkpoints"

    id = Column(Integer, primary_key=True)
    log_url = Column(String(255), nullable=False, unique=True, index=True)
    log_name = Column(String(255), nullable=True)
    tree_size_processed = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class NewsItem(Base):
    """
    Anguilla-specific news headlines, pulled directly from local outlets'
    own public RSS feeds (Anguilla Focus, The Anguillian) -- not from
    Google News' RSS, whose own terms explicitly restrict use to "a
    personal feed reader for personal, non-commercial use." Using each
    outlet's own first-party feed sidesteps that entirely: they publish it
    for syndication, and we only store headline + link + date, never
    reproducing article body text.
    """
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True)
    title = Column(String(512), nullable=False)
    link = Column(String(1024), nullable=False, unique=True)
    source = Column(String(128), nullable=False)
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("link", name="uq_news_link"),
    )


class AnguillaBusiness(Base):
    """
    Business/POI data for Anguilla, sourced from OpenStreetMap (via the
    Overpass API) -- free, no key, community-maintained. Refreshed
    periodically rather than queried live per-visitor, since Overpass is
    a shared public resource with rate limits and isn't meant for
    high-frequency client-side polling.
    """
    __tablename__ = "anguilla_businesses"

    id = Column(Integer, primary_key=True)
    osm_id = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(255), nullable=True)
    category = Column(String(64), nullable=False)  # e.g. "restaurant", "hotel"
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("osm_id", name="uq_osm_id"),
    )


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
