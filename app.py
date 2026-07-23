import os
import re
from datetime import datetime, timedelta

from flask import Flask, render_template, jsonify
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker

from models import Base, AnguillaRevenue, DiscoveredDomain, TrancoCheck, TopAiSite, NewsItem, AnguillaBusiness

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)


def _auto_migrate():
    """
    Base.metadata.create_all() only creates missing TABLES, it never adds
    columns to tables that already exist -- so a new nullable column added
    to a model (like pct_of_govt_revenue) needs an explicit ALTER TABLE on
    a database that already has that table. Postgres supports
    "ADD COLUMN IF NOT EXISTS" natively, making this safe to run on every
    boot regardless of whether the column already exists.
    """
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE anguilla_revenue "
                "ADD COLUMN IF NOT EXISTS pct_of_govt_revenue FLOAT"
            ))
    except Exception as e:
        print(f"[warn] auto-migration failed (non-Postgres dev DB is expected to hit this): {e}")


_auto_migrate()


def _auto_seed_revenue():
    """
    Idempotently seeds published Anguilla revenue figures on startup.
    Cheap and local (no external network calls), safe to run on every
    boot -- it's a no-op after the first successful seed since
    scripts/seed_anguilla_revenue.py checks for existing rows first.
    """
    try:
        from scripts.seed_anguilla_revenue import run as seed_run
        seed_run()
    except Exception as e:
        print(f"[warn] auto-seed of revenue data failed: {e}")


_auto_seed_revenue()

app = Flask(__name__)

# --- Revenue-per-day model, derived from published annual/monthly figures ---
# Used for the "today / this week / this month / this year / projected"
# panel. Anguilla doesn't report daily figures, so days-in-period is used
# to derive an average daily rate from the most recent period we have.
#
# IMPORTANT: this prefers the CURRENT calendar year's "(trend)" projection
# scenario (if one exists) over simply carrying forward the prior actual
# year's rate. Using last year's flat rate would silently understate a
# year with real, sourced growth expectations -- e.g. right now (mid
# 2026) using 2025's $85.3M/365 rate makes the "so far this month"
# counter annualize back to exactly $85.3M, which contradicts the 2026
# outlook chart showing $96.4M-$178M scenarios one section down. Using
# the trend scenario keeps the live counters consistent with the same
# numbers shown in the projections chart.

def get_revenue_context():
    session = Session()
    years = (session.query(AnguillaRevenue)
             .filter_by(granularity="year")
             .order_by(AnguillaRevenue.period_start)
             .all())
    months = (session.query(AnguillaRevenue)
              .filter_by(granularity="month")
              .order_by(AnguillaRevenue.period_start)
              .all())
    session.close()

    actual_years = [y for y in years if not y.is_projection]
    latest_actual = actual_years[-1] if actual_years else None
    projected_years = [y for y in years if y.is_projection]

    current_year = datetime.utcnow().year
    current_year_trend = next(
        (y for y in projected_years
         if y.period_start.year == current_year and "trend" in y.period_label.lower()),
        None
    )

    rate_basis = current_year_trend or latest_actual
    rate_basis_label = None
    daily_estimate = None
    if rate_basis:
        days = (rate_basis.period_end - rate_basis.period_start).days + 1
        daily_estimate = rate_basis.revenue_usd / days
        rate_basis_label = (
            f"{rate_basis.period_label} projection"
            if rate_basis.is_projection
            else f"{rate_basis.period_label} actuals"
        )

    return {
        "years": years,
        "months": months,
        "latest_actual": latest_actual,
        "projected_years": projected_years,
        "rate_basis": rate_basis,
        "rate_basis_label": rate_basis_label,
        "daily_estimate": daily_estimate,
        "weekly_estimate": daily_estimate * 7 if daily_estimate else None,
        "monthly_estimate": daily_estimate * 30 if daily_estimate else None,
    }


# --- Civic-impact reference data ---------------------------------------
# Static reference content (updated a few times a year at most, not worth
# a DB table). Sources noted inline; see chat history / commit messages
# for the research trail.

ANGUILLA_POPULATION = 16_000  # commonly-cited round figure; sources range
# ~14,800 (UN medium-fertility estimate, Worldometer/StatisticsTimes) to
# ~17,000 (Countrymeters, which factors in recent migration). We use the
# rounder ~16,000 figure used directly in press coverage of the .ai boom
# (e.g. HLC.com: "home to around just 16,000 people").

# --- Historical archive: maps, aerials, old photos -------------------------
# Hand-curated after a research pass (see chat history for the full trail).
# Deliberately small and honest about it -- Anguilla's free/public digital
# footprint for historical material is thin compared to e.g. NYC. Each item
# below was individually verified (real image URL, real license/source)
# rather than assumed from a filename or category listing -- several
# promising-looking leads (a Wikimedia file literally named
# "Anguilla-1905.jpg", a DPLA "Anguilla Island" photo set) turned out to be
# a modern infinity-pool photo and a *different* Anguilla (a cay in the
# Bahamas) respectively, and were excluded.
# Deliberately small and honest about it -- Anguilla's free/public digital
# footprint for historical material is thin compared to e.g. NYC. Each item
# below was individually verified (real image URL, real license/source)
# rather than assumed from a filename or category listing -- several
# promising-looking leads (a Wikimedia file literally named
# "Anguilla-1905.jpg", a DPLA "Anguilla Island" photo set) turned out to be
# a modern infinity-pool photo and a *different* Anguilla (a cay in the
# Bahamas) respectively, and were excluded.
#
# NOTE ON "clear_before_launch": items flagged True are sourced from
# institutions (David Rumsey, Gallica/BnF) whose reuse terms require paid
# permission for commercial use, even though the underlying historical work
# itself is centuries out of copyright. Per Noel's decision, these are
# included for now (non-commercial testing/build phase) but MUST be
# licensed properly, replaced, or removed before any official/commercial
# launch. Do not lose track of this flag when editing this list.
ARCHIVE_ITEMS = [
    {
        "title": "Carta Universal (Caribbean detail)",
        "year": "1500", "kind": "Map",
        "image_url": "/static/archive/1500-juan-de-la-cosa-caribbean.jpg",
        "source": "Museo Naval, Madrid, via Wikimedia Commons", "license": "Public domain",
        "source_url": "https://en.wikipedia.org/wiki/Map_of_Juan_de_la_Cosa",
        "description": "Detail from Juan de la Cosa's map -- the oldest surviving map "
                        "that unambiguously shows the Caribbean. De la Cosa sailed with "
                        "Columbus as captain of the Santa María. Nothing earlier survives; "
                        "this is as close to a 15th-century Caribbean map as exists.",
        "clear_before_launch": False,
    },
    {
        "title": "Cantino Planisphere (Caribbean detail)",
        "year": "1502", "kind": "Map",
        "image_url": "/static/archive/1502-cantino-map-caribbean.jpg",
        "source": "Biblioteca Estense, Modena, via Wikimedia Commons", "license": "Public domain",
        "source_url": "https://en.wikipedia.org/wiki/Cantino_planisphere",
        "description": "Detail showing Cuba, Hispaniola, Puerto Rico and other Caribbean "
                        "islands from the Cantino Planisphere -- smuggled out of Portugal "
                        "in 1502 by an Italian spy.",
        "clear_before_launch": False,
    },
    {
        "title": "Mapa del Caribe y América Central",
        "year": "1500s", "kind": "Map",
        "image_url": "/static/archive/1500s-mapa-caribe-america-central.jpg",
        "source": "Wikimedia Commons", "license": "Public domain",
        "source_url": "https://commons.wikimedia.org/wiki/File:Mapa_del_Caribe_y_Am%C3%A9rica_Central_(Siglo_XVI).jpg",
        "description": "16th-century manuscript map showing the Florida peninsula, "
                        "Caribbean islands, and northern South America.",
        "clear_before_launch": False,
    },
    {
        "title": "Map of the 16th-century Caribbean",
        "year": "1500s", "kind": "Map",
        "image_url": "/static/archive/1500s-wellcome-caribbean.jpg",
        "source": "Wellcome Collection", "license": "CC BY 4.0",
        "source_url": "https://commons.wikimedia.org/wiki/File:Map_of_16th_century_Caribbean._Wellcome_L0001228.jpg",
        "description": "16th-century map of the Caribbean basin, Wellcome Collection.",
        "clear_before_launch": False,
    },
    {
        "title": "Central America (Theodor de Bry)",
        "year": "1594", "kind": "Map",
        "image_url": "/static/archive/1594-debry-central-america.jpg",
        "source": "Wikimedia Commons", "license": "Public domain",
        "source_url": "https://commons.wikimedia.org/wiki/File:Theodor_De_Bry_-_Central_America_1594.jpg",
        "description": "Theodor de Bry's engraved map of Central America and the "
                        "Caribbean, from his famous illustrated voyage compilations.",
        "clear_before_launch": False,
    },
    {
        "title": "Insulae Americanae in Oceano Septentrionali",
        "year": "1681", "kind": "Map",
        "image_url": "/static/archive/1681-dutch-caribbean-kb.jpg",
        "source": "Koninklijke Bibliotheek (Dutch Royal Library)", "license": "Public domain",
        "source_url": "https://commons.wikimedia.org/wiki/File:AMH-7755-KB_Map_of_Central_American_and_the_Caribbean_region.jpg",
        "description": "17th-century Dutch map of Central America and the Caribbean, "
                        "held by the Dutch Royal Library.",
        "clear_before_launch": False,
    },
    {
        "title": "Carta esférica de las yslas de Sn. Martin, Sn. Bartolome y Anguila",
        "year": "1794", "kind": "Nautical chart",
        "image_url": "/static/archive/1794-spanish-nautical-chart.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/90683965/",
        "description": "Spanish nautical chart of St. Martin, St. Barthélemy, "
                        "and Anguilla -- the oldest item found in this archive.",
        "clear_before_launch": False,
    },
    {
        "title": "Porto Rico (Sheet 69, Atlas Universel)",
        "year": "1827", "kind": "Map",
        "image_url": "/static/archive/1827-vandermaelen-porto-rico.jpg",
        "source": "David Rumsey Historical Map Collection", "license": "CC BY-NC-SA (non-commercial)",
        "source_url": "https://www.dp.la/item/?q=Vandermaelen+Porto+Rico+1827",
        "description": "Philippe Vandermaelen's Atlas Universel (Brussels, 1825-1827) -- "
                        "the first lithographed world atlas, and the first atlas with every "
                        "map on the same scale. This sheet's own catalog description confirms "
                        "it \"covers also the Virgin Islands, Anguilla and Saint Martin.\"",
        "clear_before_launch": True,
    },
    {
        "title": "Carta general de las Islas Antillas Menores, llamadas de Barlovento, y también Caribes",
        "year": "1781", "kind": "Map",
        "image_url": "/static/archive/1781-lopez-antillas-menores.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "http://www.loc.gov/item/74695785/",
        "description": "Tomás López, Geographer to the King of Spain -- explicitly "
                        "titled as covering the Lesser Antilles \"desde la isla de la "
                        "Anguila hasta la de Tobago\" (from the island of Anguilla to Tobago).",
        "clear_before_launch": False,
    },
    {
        "title": "The Virgin Islands from English and Danish Surveys",
        "year": "1775", "kind": "Map",
        "image_url": "/static/archive/1775-jefferys-virgin-islands-anguilla.jpg",
        "source": "Royal Danish Library, via Digital Commonwealth", "license": "No known copyright restrictions",
        "source_url": "https://ark.digitalcommonwealth.org/ark:/50959/gt54tg64s",
        "description": "Thomas Jefferys' map covering the Virgin Islands, Anguilla, "
                        "St. Martin, Saba, and St. Eustatius, from The West-India Atlas. "
                        "Digitized copy held by the Royal Danish Library.",
        "clear_before_launch": False,
    },
    {
        "title": "Map of The Leeward Islands",
        "year": "1858", "kind": "Map",
        "image_url": "/static/archive/1858-arrowsmith-leeward-islands.jpg",
        "source": "David Rumsey Historical Map Collection", "license": "CC BY-NC-SA (non-commercial)",
        "source_url": "https://www.davidrumsey.com/luna/servlet/detail/RUMSEY~8~1~2778~270051:Map-of-The-Leeward-Islands-",
        "description": "John Arrowsmith's map of Antigua, Montserrat, Barbuda, "
                        "St. Christopher, Nevis, Anguilla, the Virgin Islands "
                        "& Dominica, compiled from Colonial Office and Admiralty documents.",
        "clear_before_launch": True,
    },
    {
        "title": "Carte générale des îles Antilles",
        "year": "1832", "kind": "Map",
        "image_url": "/static/archive/1832-brue-antilles-general.jpg",
        "source": "Gallica / Bibliothèque nationale de France", "license": "BnF non-commercial terms",
        "source_url": "https://gallica.bnf.fr/ark:/12148/btv1b53035331p",
        "description": "A.H. Brué's general chart of the Antilles, Bahama islands "
                        "and banks, Central America, and the Gulf of Mexico.",
        "clear_before_launch": True,
    },
    {
        "title": "Leeward Islands",
        "year": "1989", "kind": "Map",
        "image_url": "/static/archive/1989-cia-leeward-islands.jpg",
        "source": "CIA / U.S. National Archives (NARA)", "license": "No known copyright restrictions",
        "source_url": "http://catalog.archives.gov/id/266783333",
        "description": "CIA-produced reference map (Records of the CIA, RG 263), "
                        "explicitly labeling Sombrero, Dog Island, and Scrub Island "
                        "as belonging to Anguilla (U.K.).",
        "clear_before_launch": False,
    },
]

# What exists but couldn't be included, for transparency in the UI:
ARCHIVE_KNOWN_GAPS = [
    "Anguilla Heritage Museum (Colville Petty) holds genuine early-20th-century "
    "photos -- salt industry, schooners, the 1964 Queen Elizabeth visit -- but "
    "the collection isn't digitized or online.",
    "British Library EAP596 digitized real Anguilla court records and Sombrero "
    "Lighthouse logs (1895-1916), but access is restricted to research purposes "
    "only, and the catalog is currently down after the British Library's 2023 "
    "cyberattack.",
    "Two items below (marked in their card) are sourced from David Rumsey "
    "Historical Map Collection and Gallica/BnF, whose terms require paid "
    "permission for commercial use even of public-domain originals. Included "
    "for now during this non-commercial build/testing phase -- must be "
    "properly licensed, replaced, or removed before official launch.",
]


# --- Historical accounts: old testimonials mentioning Anguilla -------------
# Anguilla never developed a profitable plantation economy (poor, dry soil),
# so it drew far less colonial administrative attention -- and far less of
# the paper trail -- than sugar-wealthy neighbors. These were found via a
# wide research pass and individually verified. Two near-misses excluded:
# an 1861 slave-smuggling account that explicitly named "Anguilla Island,
# one of the Bahamas" (a different island, same name), and Pere Labat's
# famous Antilles travel writing, whose documented itinerary doesn't
# actually include Anguilla.
HISTORICAL_ACCOUNTS = [
    {
        "year": "1667-68", "title": "Major John Scott's account",
        "quote": "left the island \u201cin good condition\u201d; noted that in "
                 "July 1668, \u201c200 or 300 people fled thither in time of war.\u201d",
        "context": "One of the earliest surviving firsthand accounts of the "
                    "English colony, from a visit less than 20 years after settlement.",
        "source": "Wikipedia, citing colonial-era sources",
        "source_url": "https://en.wikipedia.org/wiki/Anguilla",
    },
    {
        "year": "1745", "title": "The Battle of Anguilla",
        "quote": "A French force of 759 men landed at Rendezvous Bay; Governor "
                 "Arthur Hodge's ~150 defenders ambushed them from hidden "
                 "breastworks, then counterattacked -- 100 French casualties "
                 "and 50 captured, against 7 British.",
        "context": "A small, poor island with almost no military garrison "
                    "routed a force five times its size.",
        "source": "Wikipedia / Military history sources",
        "source_url": "https://en.wikipedia.org/wiki/Battle_of_Anguilla",
    },
    {
        "year": "1884", "title": "A fever-stricken vessel",
        "quote": "\u201cA St. Thomas paper tells a story of the wreck of a "
                 "Norwegian brigantine on the Anguilla reef during a gale, "
                 "and when all on board, except the Captain and a boy, were "
                 "down with the African coast fever... The Commander of the "
                 "brigantine, with the aid of the boy, kept his vessel on "
                 "her course for a month.\u201d",
        "context": "Sacramento Daily Union, Feb 29, 1884 -- syndicated from a "
                    "St. Thomas newspaper report.",
        "source": "Sacramento Daily Union, via MaritimeHeritage.org",
        "source_url": "https://www.maritimeheritage.org/ports/caribbeanAnguilla.html",
    },
    {
        "year": "1969", "title": "\u201cThe mouse that roared\u201d",
        "quote": "British paratroopers and Royal Marines landed on a "
                 "virtually undefended island in \u201cOperation Sheepskin\u201d "
                 "-- widely mocked by the British and American press at the time.",
        "context": "Followed the 1967 Anguilla Revolution, when the island "
                    "broke away from the Associated State of St. Kitts-Nevis-Anguilla.",
        "source": "The Anguillian",
        "source_url": "https://theanguillian.com/2017/03/the-last-invasion-of-anguilla/",
    },
]

PEER_CCTLDS = [
    {
        "tld": ".ai", "territory": "Anguilla",
        "revenue_usd_year": 85_300_000, "revenue_year_label": "2025",
        "pct_of_govt_revenue": "~47%",
        "population": "~16,000",
        "status": "Rapid growth, riding the AI naming boom.",
        "source_url": "https://anguillafocus.com/ai-domain-surge-brings-ec230m-windfall-to-anguilla-in-2025/",
    },
    {
        "tld": ".tv", "territory": "Tuvalu",
        "revenue_usd_year": 10_000_000, "revenue_year_label": "~2024",
        "pct_of_govt_revenue": "~8-10%",
        "population": "~11,000",
        "status": "Stable but modest relative to Anguilla's .ai windfall -- "
                  "riding streaming/esports demand (Twitch.tv) rather than a boom.",
        "source_url": "https://en.wikipedia.org/wiki/.tv",
    },
    {
        "tld": ".io", "territory": "British Indian Ocean Territory",
        "revenue_usd_year": 42_400_000, "revenue_year_label": "2024",
        "pct_of_govt_revenue": "N/A (no permanent population/government budget)",
        "population": "Uninhabited except UK/US military base",
        "status": "Future genuinely uncertain -- UK ceded BIOT sovereignty to "
                  "Mauritius (treaty signed May 2025); ICANN rules could force "
                  "the ccTLD to be retired over several years if the \"IO\" "
                  "country code is removed from ISO 3166-1.",
        "source_url": "https://en.wikipedia.org/wiki/.io",
    },
    {
        "tld": ".co", "territory": "Colombia",
        "revenue_usd_year": 125_000_000, "revenue_year_label": "trailing 5yr through 2025",
        "pct_of_govt_revenue": "was ~6-7%, renegotiated to 81%, now new operator keeps only 8% (92% to Colombia)",
        "population": "~52 million",
        "status": "The clearest lesson in negotiating leverage of any ccTLD "
                  "here: Colombia's government revenue SHARE went from single "
                  "digits under the original contract to 81%+ on renewal, by "
                  "re-bidding the registry contract rather than accepting the "
                  "incumbent's terms.",
        "source_url": "https://domainincite.com/31134-godaddy-loses-co-to-team-internet",
    },
    {
        "tld": ".me", "territory": "Montenegro",
        "revenue_usd_year": 7_100_000, "revenue_year_label": "2015 (~\u20ac6.5M; most recent public figure found)",
        "pct_of_govt_revenue": "~2% of total exports (different framing than govt revenue %)",
        "population": "~620,000",
        "status": "Older, more mature boom (crossed 1M registrations in 2016) "
                  "-- shows what a ccTLD windfall looks like once growth "
                  "plateaus into a steady, smaller ongoing revenue stream, "
                  "rather than Anguilla's current rapid-growth phase.",
        "source_url": "https://techcrunch.com/2017/01/10/me-10-years-and-two-percent-of-exports/",
    },
]

BUDGET_ALLOCATION_NOTE = {
    "text": (
        "Anguilla's Premier, Ellis Webster, has publicly stated .ai revenue "
        "is funding: airport expansion, free medical care for senior "
        "citizens, completion of a vocational technology training centre "
        "at Anguilla's high school, and hurricane-resilient infrastructure "
        "including secure domain-hosting facilities."
    ),
    "source_url": "https://www.hlc.com/en/publications/british-territories-ride-wave-of-tech-boom-ai-and-io",
}

RENEWAL_RATE = 0.90  # widely cited across sources (domaintechnik.at, pymnts, etc.)


def get_civic_context(revenue_ctx):
    """Per-resident dividend, dependency trend, renewal-base estimate."""
    latest = revenue_ctx["latest_actual"]
    per_resident_year = (latest.revenue_usd / ANGUILLA_POPULATION) if latest else None
    daily_estimate = revenue_ctx["daily_estimate"]
    per_resident_daily_rate = (daily_estimate / ANGUILLA_POPULATION) if daily_estimate else None

    dependency_years = [
        {"label": y.period_label, "pct": y.pct_of_govt_revenue}
        for y in revenue_ctx["years"]
        if y.pct_of_govt_revenue is not None
    ]

    # Renewal-base estimate: illustrative, not a precise revenue split --
    # see note rendered alongside it in the template.
    cumulative = latest.total_registrations_cumulative if latest else None
    renewal_base_estimate = int(cumulative * RENEWAL_RATE) if cumulative else None

    return {
        "population": ANGUILLA_POPULATION,
        "per_resident_year": per_resident_year,
        "per_resident_daily_rate": per_resident_daily_rate,
        "dependency_years": dependency_years,
        "renewal_rate": RENEWAL_RATE,
        "cumulative_registrations": cumulative,
        "renewal_base_estimate": renewal_base_estimate,
        "peer_cctlds": PEER_CCTLDS,
        "budget_allocation": BUDGET_ALLOCATION_NOTE,
    }


@app.route("/")
def dashboard():
    ctx = get_revenue_context()
    civic = get_civic_context(ctx)

    session = Session()
    unclaimed = (session.query(TrancoCheck)
                 .filter_by(ai_registered=False)
                 .order_by(TrancoCheck.tranco_rank)
                 .limit(50).all())
    claimed_count = session.query(TrancoCheck).filter_by(ai_registered=True).count()
    unclaimed_count = session.query(TrancoCheck).filter_by(ai_registered=False).count()
    last_checked = (session.query(TrancoCheck)
                     .order_by(desc(TrancoCheck.checked_at)).first())

    recent_discovered = (session.query(DiscoveredDomain)
                          .order_by(desc(DiscoveredDomain.discovered_at))
                          .limit(25).all())
    top_ai_sites = (session.query(TopAiSite)
                     .order_by(TopAiSite.tranco_rank)
                     .limit(25).all())
    session.close()

    return render_template(
        "dashboard.html",
        revenue=ctx,
        civic=civic,
        unclaimed=unclaimed,
        claimed_count=claimed_count,
        unclaimed_count=unclaimed_count,
        last_checked=last_checked,
        recent_discovered=recent_discovered,
        has_discovery_feed=len(recent_discovered) > 0,
        top_ai_sites=top_ai_sites,
        now=datetime.utcnow(),
    )


@app.route("/api/revenue.json")
def api_revenue():
    """Feeds the Chart.js revenue-over-time chart."""
    session = Session()
    years = (session.query(AnguillaRevenue)
             .filter_by(granularity="year")
             .order_by(AnguillaRevenue.period_start).all())
    session.close()
    return jsonify([
        {
            "label": y.period_label,
            "revenue_usd": y.revenue_usd,
            "is_projection": y.is_projection,
            "cumulative_registrations": y.total_registrations_cumulative,
            "pct_of_govt_revenue": y.pct_of_govt_revenue,
        }
        for y in years
    ])


@app.route("/api/unclaimed.json")
def api_unclaimed():
    session = Session()
    rows = (session.query(TrancoCheck)
            .filter_by(ai_registered=False)
            .order_by(TrancoCheck.tranco_rank)
            .limit(200).all())
    session.close()
    return jsonify([
        {"rank": r.tranco_rank, "com": r.com_domain, "ai_candidate": r.ai_candidate,
         "checked_at": r.checked_at.isoformat()}
        for r in rows
    ])


ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")


@app.route("/admin/run-tranco-check")
def admin_run_tranco():
    """Manually trigger a Tranco/RDAP unclaimed-.ai scan. Protected by
    ADMIN_TOKEN. TEMPORARY mechanism -- convert to a Railway cron service
    (see scripts/tranco_check.py) rather than relying on manual hits."""
    from flask import request
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    limit = int(request.args.get("limit", 100))
    from scripts.tranco_check import run as tranco_run
    tranco_run(limit=limit, offset=0, sleep_s=0.3)
    return jsonify({"status": "done", "limit": limit})


@app.route("/admin/run-ct-ingest")
def admin_run_ct_ingest():
    """Manually trigger CT-log (crt.sh) discovery ingestion. Protected by
    ADMIN_TOKEN. TEMPORARY mechanism -- convert to a Railway cron service
    (see scripts/ingest_ct_domains.py) for real scheduled operation."""
    from flask import request
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    since_hours = int(request.args.get("since_hours", 24))
    from scripts.ingest_ct_domains import run as ct_run
    ct_run(since_hours)
    return jsonify({"status": "done", "since_hours": since_hours})


@app.route("/api/discovered.json")
def api_discovered():
    """
    Cursor-paginated feed of CT-log-discovered .ai domains, for infinite
    scroll. Pass `before` (ISO datetime) to get the next page older than
    the last item you already have.

    NOTE: labeled "discovered", never "registered" -- see DiscoveredDomain
    docstring in models.py for why. This endpoint returns whatever the
    CT-log ingestion (scripts/ingest_ct_domains.py) has found so far; if
    that pipeline hasn't successfully run yet (e.g. crt.sh outage), this
    will legitimately return an empty list, not an error.
    """
    from flask import request
    limit = min(int(request.args.get("limit", 25)), 100)
    before_raw = request.args.get("before")

    session = Session()
    q = session.query(DiscoveredDomain).order_by(desc(DiscoveredDomain.discovered_at))
    if before_raw:
        try:
            before_dt = datetime.fromisoformat(before_raw)
            q = q.filter(DiscoveredDomain.discovered_at < before_dt)
        except ValueError:
            pass
    rows = q.limit(limit).all()
    session.close()

    return jsonify({
        "items": [
            {
                "domain": d.domain,
                "discovered_at": d.discovered_at.isoformat(),
                "vendor": d.vendor,
            }
            for d in rows
        ],
        "next_before": rows[-1].discovered_at.isoformat() if len(rows) == limit else None,
    })


@app.route("/api/top-ai-sites.json")
def api_top_ai_sites():
    from flask import request
    limit = min(int(request.args.get("limit", 25)), 100)
    offset = int(request.args.get("offset", 0))

    session = Session()
    rows = (session.query(TopAiSite)
            .order_by(TopAiSite.tranco_rank)
            .offset(offset).limit(limit).all())
    total = session.query(TopAiSite).count()
    session.close()

    next_offset = offset + limit if offset + limit < total else None
    return jsonify({
        "items": [
            {"rank": r.tranco_rank, "domain": r.domain, "checked_at": r.checked_at.isoformat()}
            for r in rows
        ],
        "total": total,
        "next_offset": next_offset,
    })


@app.route("/admin/run-top-ai-sites")
def admin_run_top_ai_sites():
    """Manually trigger a refresh of top-ranked .ai sites. Protected by
    ADMIN_TOKEN. TEMPORARY mechanism -- convert to a Railway cron service
    (see scripts/top_ai_sites.py) rather than relying on manual hits."""
    from flask import request
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    scan = int(request.args.get("scan", 1_000_000))
    top = int(request.args.get("top", 50))
    from scripts.top_ai_sites import run as top_ai_run
    top_ai_run(scan=scan, top=top)
    return jsonify({"status": "done", "scan": scan, "top": top})


@app.route("/admin/run-ct-tail")
def admin_run_ct_tail():
    """Manually trigger the direct CT-log tailer (bypasses crt.sh entirely).
    Protected by ADMIN_TOKEN. TEMPORARY mechanism -- the real operation is
    via the ct-tail-cron Railway service on a schedule."""
    from flask import request
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    entry_budget = int(request.args.get("entry_budget", 20000))
    initial_lookback = int(request.args.get("initial_lookback", 3000))
    max_wall_seconds = int(request.args.get("max_wall_seconds", 200))
    from scripts.ct_log_tail import run as ct_tail_run
    ct_tail_run(entry_budget, initial_lookback, max_wall_seconds)
    return jsonify({"status": "done"})


LOOKUP_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


@app.route("/api/check-domain.json")
def api_check_domain():
    """
    Domain availability lookup for the search box on the homepage --
    checks both {name}.ai and {name}.com. Proxied through our backend
    (rather than the browser hitting RDAP directly) to avoid CORS issues
    and to reuse the existing retry/backoff-aware rdap_check() helper.
    """
    from flask import request
    from scripts.tranco_check import rdap_check

    raw = (request.args.get("name") or "").strip().lower()
    # Be forgiving: accept "example", "example.ai", "example.com", etc.
    for suffix in (".ai", ".com"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    raw = raw.rstrip(".")

    if not raw or not LOOKUP_NAME_RE.match(raw):
        return jsonify({
            "error": "Enter a valid domain label (letters, numbers, "
                     "hyphens only, not starting/ending with a hyphen)."
        }), 400

    results = {}
    for tld in ("ai", "com"):
        candidate = f"{raw}.{tld}"
        registered, raw_status = rdap_check(candidate)
        if registered is None:
            results[tld] = {"domain": candidate, "error": raw_status}
        else:
            results[tld] = {"domain": candidate, "registered": registered}

    return jsonify({"query": raw, "results": results})


@app.route("/api/news.json")
def api_news():
    from flask import request
    limit = min(int(request.args.get("limit", 20)), 100)
    offset = int(request.args.get("offset", 0))

    session = Session()
    rows = (session.query(NewsItem)
            .order_by(NewsItem.published_at.desc().nullslast())
            .offset(offset).limit(limit).all())
    total = session.query(NewsItem).count()
    session.close()

    next_offset = offset + limit if offset + limit < total else None
    return jsonify({
        "items": [
            {"title": n.title, "link": n.link, "source": n.source,
             "published_at": n.published_at.isoformat() if n.published_at else None}
            for n in rows
        ],
        "total": total,
        "next_offset": next_offset,
    })


@app.route("/admin/run-news-fetch")
def admin_run_news_fetch():
    """Manually trigger the Anguilla news fetch. Protected by ADMIN_TOKEN."""
    from flask import request
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    from scripts.fetch_anguilla_news import run as news_run
    news_run()
    return jsonify({"status": "done"})


def _archive_sort_key(item):
    import re
    m = re.match(r"^\d{3,4}", item["year"])
    return int(m.group()) if m else 9999  # undated items sort last


@app.route("/map")
def anguilla_map():
    sorted_items = sorted(ARCHIVE_ITEMS, key=_archive_sort_key)
    return render_template("map.html", now=datetime.utcnow(),
                            archive_items=sorted_items,
                            archive_gaps=ARCHIVE_KNOWN_GAPS,
                            historical_accounts=HISTORICAL_ACCOUNTS)


@app.route("/api/anguilla-businesses.json")
def api_anguilla_businesses():
    session = Session()
    rows = session.query(AnguillaBusiness).all()
    session.close()
    return jsonify([
        {"name": b.name or "Unnamed", "category": b.category,
         "lat": b.latitude, "lon": b.longitude}
        for b in rows
    ])


@app.route("/admin/run-businesses-fetch")
def admin_run_businesses_fetch():
    """Manually trigger the Anguilla business/POI fetch. Protected by ADMIN_TOKEN."""
    from flask import request
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 403
    from scripts.fetch_anguilla_businesses import run as biz_run
    biz_run()
    return jsonify({"status": "done"})


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
