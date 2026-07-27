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
            conn.execute(text(
                "ALTER TABLE anguilla_businesses "
                "ADD COLUMN IF NOT EXISTS layer_group VARCHAR(32)"
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
        "title": "Pascaerte van de Caribes Eylanden, van 't Eylant Granadillos, tot 't Eylant Anguilla",
        "year": "1675", "kind": "Map",
        "image_url": "/static/archive/1675-roggeveen-caribbean-chart.jpg",
        "source": "National Library of France (via Europeana)", "license": "No known copyright",
        "source_url": "https://gallica.bnf.fr/ark:/12148/btv1b8596295t",
        "description": "A Dutch nautical chart of the Lesser Antilles, from Grenada to "
                        "Anguilla, by Arent Roggeveen -- a real 17th-century Dutch "
                        "cartographer and ship's pilot (his son Jacob later became famous "
                        "for the European discovery of Easter Island).",
        "clear_before_launch": False,
    },
    {
        "title": "Insulae Americanae in Oceano Septentrionali cum terris adjacentibus",
        "year": "1634", "kind": "Map",
        "image_url": "/static/archive/loc-2003630536.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/2003630536/",
        "description": "Willem Janszoon Blaeu (Amsterdam) -- one of the most iconic "
                        "Dutch Golden Age maps of the Americas and Caribbean.",
        "history": "Willem Blaeu founded what became the most celebrated map-publishing dynasty of the 17th century; his son Joan later expanded the firm into the largest publishing house in the world. This map represents Dutch cartography at its commercial and artistic peak.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/2003630536.dzi",
    },
    {
        "title": "A chart of the Caribe Ilands",
        "year": "1680", "kind": "Map",
        "image_url": "/static/archive/loc-2007633672.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/2007633672/",
        "description": "John Thornton, London -- English maritime chart of the Caribbean.",
        "history": "John Thornton was the leading English chart-maker of the late 17th century and official hydrographer to both the East India Company and Hudson's Bay Company -- a sign of how English maritime power was catching up to the Dutch by 1680.",
        "history": "John Thornton was the leading English chart-maker of the late 17th century and official hydrographer to both the East India Company and Hudson's Bay Company -- a sign of how English maritime power was catching up to the Dutch by 1680.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/2007633672.dzi",
    },
    {
        "title": "Isole Antili, la Cuba e la Spagnuola",
        "year": "1690", "kind": "Map",
        "image_url": "/static/archive/loc-95684858.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/95684858/",
        "description": "Vincenzo Coronelli, Venice -- Italian map of the Antilles, "
                        "Cuba, and Hispaniola.",
        "history": "Vincenzo Coronelli was a Franciscan friar who became official cosmographer of the Republic of Venice and later founded one of the world's first geographical societies. He's better remembered for his enormous globes than his charts, but this map shows the same meticulous draftsmanship.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/95684858.dzi",
    },
    {
        "title": "Tabula Mexicae et Floridae",
        "year": "1710", "kind": "Map",
        "image_url": "/static/archive/loc-2004629008.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/2004629008/",
        "description": "Peter Schenk, Amsterdam -- Dutch map of Mexico, Florida, "
                        "and the surrounding American islands.",
        "history": "Peter Schenk was part of Amsterdam's prominent Schenk publishing house, which specialized in acquiring and reissuing older Dutch copperplates -- meaning much of what looks like fresh 1710 cartography is actually inherited from earlier 17th-century originals.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/2004629008.dzi",
    },
    {
        "title": "A map of the West-Indies or the islands of America in the North Sea",
        "year": "1715", "kind": "Map",
        "image_url": "/static/archive/loc-gm71005442.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/gm71005442/",
        "description": "Herman Moll and Thomas Bowles -- shows English, French, "
                        "Spanish, and Dutch territorial claims across the Caribbean, "
                        "plus galleon/flota trade routes.",
        "history": "Herman Moll was a German engraver who settled in London and became one of the most popular English mapmakers of his day, known for filling his charts with opinionated marginal notes about territorial disputes -- visible here in how explicitly it marks out English, French, Spanish, and Dutch claims.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/gm71005442.dzi",
    },
    {
        "title": "Particular draughts and plans of some of the principal towns and harbours belonging to the English, French, and Spaniards, in America and West Indies",
        "year": "1752", "kind": "Map",
        "image_url": "/static/archive/loc-74693283.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/74693283/",
        "description": "Emanuel Bowen, London -- harbor plans across the West Indies.",
        "history": 'Emanuel Bowen served as royal cartographer to both King George II and Louis XV of France simultaneously, an unusual dual appointment for the era. He was also apprentice-master to Thomas Jefferys, whose own West Indies atlas appears elsewhere in this archive.',
        "history": "Emanuel Bowen served as royal cartographer to both King George II and Louis XV of France simultaneously, an unusual dual appointment for the era. He was also apprentice-master to Thomas Jefferys, whose own West Indies atlas appears elsewhere in this archive.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/74693283.dzi",
    },
    {
        "title": "New map of the West Indies for the history of the British colonies",
        "year": "1700s", "kind": "Map",
        "image_url": "/static/archive/loc-2006629763.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/2006629763/",
        "description": "Bryan Edwards -- accompanied his landmark \"History, Civil and "
                        "Commercial, of the British Colonies in the West Indies.\"",
        "history": "Bryan Edwards was a planter and slaveholder whose \"History, Civil and Commercial, of the British West Indies\" became the standard British reference on the region for decades -- shaping metropolitan understanding of the Caribbean even as it defended the plantation system that built Edwards's own fortune.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/2006629763.dzi",
    },
    {
        "title": "Chart, containing the coasts of California... North America and the West Indies",
        "year": "1775", "kind": "Map",
        "image_url": "/static/archive/loc-74696185.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/74696185/",
        "description": "Robert Sayer and John Bennett -- a sweeping chart spanning "
                        "the Pacific to the West Indies and the coasts of Europe/Africa.",
        "history": "Robert Sayer and John Bennett ran one of London's largest map and print businesses; their firm's stock later passed to Laurie and Whittle, whose maps remained in print well into the 19th century -- a reminder that popular charts were often reprinted and updated across generations of publishers.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/74696185.dzi",
    },
    {
        "title": "Map of the Gulf of Mexico, the islands, and countries adjacent",
        "year": "1777", "kind": "Map",
        "image_url": "/static/archive/loc-2010593328.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/2010593328/",
        "description": "Thomas Kitchin -- prepared for Rev. Dr. Robertson's "
                        "\"History of America.\"",
        "history": "Thomas Kitchin was hydrographer to King George III and one of the most prolific engravers of the 18th century, reputedly producing over 1,000 maps in his lifetime. This one was commissioned as an illustration for a bestselling history book, not a standalone chart.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/2010593328.dzi",
    },
    {
        "title": "Carte du Golphe Du Mexique",
        "year": "1792", "kind": "Map",
        "image_url": "/static/archive/loc-2001622457.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/2001622457/",
        "description": "Louis Denis, Paris -- French chart of the Gulf of Mexico.",
        "history": 'Louis Denis was a French globe- and instrument-maker as well as an engraver, active in Paris during the period when French cartography was competing directly with British mapmakers for authority over Caribbean geography.',
        "history": "Louis Denis was a French globe- and instrument-maker as well as an engraver, active in Paris during a period when French cartography was competing directly with British mapmakers for authority over Caribbean geography.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/2001622457.dzi",
    },
    {
        "title": "Caribbean America. 4-61",
        "year": "1961", "kind": "Map",
        "image_url": "/static/archive/loc-75694334.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/75694334/",
        "description": "US Central Intelligence Agency reference map.",
        "history": 'Produced in 1961, the same year as the Bay of Pigs invasion -- a period when US intelligence agencies were mapping the Caribbean with unusual intensity as Cold War tensions in the region peaked.',
        "history": "Produced in 1961, the same year as the Bay of Pigs invasion -- a period when US intelligence agencies were mapping the Caribbean with unusual intensity as Cold War tensions in the region peaked.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/75694334.dzi",
    },
    {
        "title": "The West Indies. 11-58",
        "year": "1958", "kind": "Map",
        "image_url": "/static/archive/loc-75693348.jpg",
        "source": "Library of Congress", "license": "Public domain",
        "source_url": "https://www.loc.gov/item/75693348/",
        "description": "US Central Intelligence Agency reference map.",
        "history": "A 1958 CIA reference map from the final years before Caribbean decolonization accelerated -- within a decade, most of the British possessions it depicts would begin the path toward independence or, in Anguilla's case, a renegotiated relationship with Britain.",
        "history": "A 1958 CIA reference map from the final years before Caribbean decolonization accelerated -- within a decade, most of the British possessions it depicts would begin the path toward independence or, in Anguilla's case, a renegotiated relationship with Britain.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/75693348.dzi",
    },
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
        "history": "De la Cosa was a Basque navigator who owned and captained the Santa Maria before it wrecked on Columbus's first voyage. He drew this chart from firsthand experience of four transatlantic crossings, making it as much a personal record as a map.",
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
        "history": "Alberto Cantino was an agent for the Duke of Ferrara, sent to Lisbon specifically to smuggle out Portugal's closely-guarded discoveries. He paid an anonymous cartographer for this copy and shipped it home in 1502 -- an early act of industrial espionage in the map trade.",
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
        "history": "An anonymous 16th-century manuscript map from the earliest wave of Spanish charting of the Caribbean basin, made as imperial administrators and pilots worked to formalize what Columbus's voyages had only sketched.",
        "clear_before_launch": False,
    },
    {
        "title": "Map of the 16th-century Caribbean",
        "year": "1500s", "kind": "Map",
        "image_url": "/static/archive/1500s-wellcome-caribbean.jpg",
        "source": "Wellcome Collection", "license": "CC BY 4.0",
        "source_url": "https://commons.wikimedia.org/wiki/File:Map_of_16th_century_Caribbean._Wellcome_L0001228.jpg",
        "description": "16th-century map of the Caribbean basin, Wellcome Collection.",
        "history": "Held by the Wellcome Collection, whose historical map holdings trace largely back to Sir Henry Wellcome's early-20th-century collecting of medical and scientific history -- explaining why a pharmaceutical magnate's archive holds a Caribbean chart.",
        "history": "Held by the Wellcome Collection, whose historical map holdings trace largely back to Sir Henry Wellcome's early-20th-century collecting of medical and scientific history -- explaining why a pharmaceutical magnate's archive holds a Caribbean chart.",
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
        "history": "Theodor de Bry was a Flemish Protestant engraver who fled religious persecution and settled in Frankfurt, where his illustrated \"Grands Voyages\" compilations became Europe's dominant visual source for the Americas -- shaping how an entire continent imagined the New World.",
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
        "history": "Held by the Koninklijke Bibliotheek, the Dutch Royal Library, from the height of the Dutch Golden Age -- when Amsterdam briefly rivaled and then overtook the older Iberian cartographic houses as Europe's mapmaking capital.",
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
        "history": "A Spanish nautical chart from an era when Madrid still closely guarded its American survey data -- Spain's crown treated accurate charts of its colonial waters as a strategic secret for much of the 16th-18th centuries.",
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
        "history": "Philippe Vandermaelen founded the world's first geographical institute in Brussels and used the newly-perfected technique of lithography to produce this atlas -- the first ever printed with every sheet at a single, consistent scale, letting readers piece together maps of anywhere on Earth.",
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
        "history": "Tomas Lopez trained in Paris under the leading French geographers of the day before returning to Madrid, where he became the dominant Spanish cartographer of the Enlightenment -- explicitly anchoring this chart's scope on Anguilla itself.",
        "clear_before_launch": False,
        "dzi_url": "/static/dzi/1781-lopez-antillas-menores.dzi",
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
        "history": "Part of the 19th-century wave of commercial atlases, when improving lithographic printing let publishers issue far more detailed and affordable regional maps than the old engraved-copperplate era allowed.",
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
        "history": "A.H. Brue held the title of official geographer to King Louis-Philippe of France, part of a 19th-century state tradition of appointing a royal cartographer to oversee France's official mapping output.",
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
        "history": "A Cold War-era reference map produced by the CIA's own cartographic division, part of a vast mid-century US intelligence mapping effort that quietly became one of the largest sources of detailed geographic data ever produced.",
        "clear_before_launch": False,
    },
]

# What exists but couldn't be included, for transparency in the UI:
# --- Anguilla 2026 public holidays / events -------------------------------
# Dates are the official 2026 calendar as finalized by the Anguilla
# government (confirmed via Anguilla Focus reporting, Jan 2026). Facts
# (dates, holiday names) aren't copyrightable; descriptions below are
# written fresh, not copied from any single source.
# --- Restaurants, villa areas, and activities -----------------------------
# Independently researched across multiple sources (AFAR, Eating With Ziggy,
# Tripadvisor/Viator aggregation, etc.) and written up fresh -- deliberately
# NOT scraped from any single site's curated directory, since that would be
# reproducing someone else's proprietary selection rather than building our
# own. Villas are handled as areas/neighborhoods rather than named individual
# properties, since specific villa listings are tied to particular rental
# agencies' commercial portfolios.
RESTAURANTS = [
    ("Straw Hat Restaurant", "Meads Bay", "Beachfront Caribbean-fusion, over 20 years running; known for red snapper ceviche and grilled crayfish."),
    ("Blanchard's Beach Shack", "Meads Bay", "Casual sister spot to the well-known Blanchard's; fish tacos and jerk chicken steps from the sand."),
    ("Veya Restaurant", "North Hill", "Upscale globally-inspired dining in a garden setting; a frequent pick for special-occasion meals."),
    ("Da'Vida Beach Club", "Crocus Bay", "Beachfront club blending Caribbean flavors with sweeping bay views."),
    ("Gwen's Reggae Grill", "Shoal Bay East", "Beachfront grill built around reggae music and Caribbean barbecue."),
    ("Madeariman Beach Bar & Restaurant", "Shoal Bay East", "Long-running beach bar and kitchen right on Shoal Bay's sand."),
    ("Tropical Sunset Restaurant & Bar", "Shoal Bay East", "Caribbean dining with an ocean view, geared toward sunset dinners."),
    ("Andy's Restaurant & Bar", "Shoal Bay East", "Local Caribbean cooking in a relaxed beachside setting."),
    ("Ola's Bar & Grill", "Shoal Bay East", "Beachfront grill known for Mexican-leaning dishes alongside Caribbean staples."),
    ("Falcon Nest Bar & Grill", "Island Harbour", "Caribbean bar and grill overlooking Island Harbour's fishing-village waterfront."),
    ("Ken's BBQ", "Roadside, Anguilla", "A well-known roadside barbecue stop for authentic local-style grilling."),
    ("Artisan Pizza Napoletana", "Island Harbour", "Neapolitan-style pizza in Island Harbour."),
    ("Roy's Bayside Grill", "Sandy Ground", "Long-standing bayside grill known for fresh seafood and harbor views."),
    ("Stone", "Anguilla", "Climate-controlled fine dining built around sea-to-table seafood and an award-recognized wine list."),
    ("Dune Preserve", "Rendezvous Bay", "A driftwood-and-boat-built beach bar owned by reggae musician Bankie Banx; as much a live-music venue as a restaurant."),
    ("Tasty's Point of View", "South Hill", "Known especially for its Johnny cake, a classic Anguillian staple."),
    ("Leon's at Meads Bay", "Meads Bay", "Beachfront spot known for playful takes on local classics, including a jerk-beef Johnny cake burger."),
    ("Hank's Hillside Bar & Restaurant", "Shoal Bay Village", "A well-regarded hillside spot near Shoal Bay."),
    ("Serenity Restaurant", "Anguilla", "A quieter dining setting, often noted for a more upscale price point."),
    ("Sharky's", "Sandy Ground", "Casual waterfront restaurant and bar in Sandy Ground."),
    ("The Fish Trap", "Meads Bay", "Seafood-forward restaurant in the Meads Bay dining cluster."),
    ("Jacala", "Meads Bay", "French-Caribbean fine dining directly on Meads Bay."),
    ("Julians", "Anguilla", "Caribbean restaurant with a reputation for creative plating."),
    ("SALT at Four Seasons Anguilla", "Barnes Bay", "Resort fine-dining venue at the Four Seasons property."),
    ("Sunset Lounge at Four Seasons Anguilla", "Barnes Bay", "Resort lounge geared around sunset views over Barnes Bay."),
    ("Coral Beach Bar", "Anguilla", "Casual beach bar and light-fare kitchen."),
    ("Johnno's on the Beach", "Sandy Ground", "A long-running Sandy Ground beach bar with a strong live-music tradition."),
    ("SandBar", "Anguilla", "Casual beachfront bar and grill."),
    ("Ocean Echo Restaurant", "Anguilla", "Beachfront dining focused on fresh seafood."),
    ("D Richard's", "Anguilla", "Fine-dining spot known among repeat visitors."),
    ("Garvey's Lobster House", "Anguilla", "Seafood specialist, built around fresh-caught lobster."),
    ("Mango's Seaside Grill", "Barnes Bay", "Seaside grill with a long-standing local following."),
]

VILLA_AREAS = [
    ("Meads Bay", "The most walkable villa area -- restaurants, beach bars, and boutiques are steps from most properties. Busiest of the villa clusters, but still relatively quiet by Caribbean standards."),
    ("Shoal Bay East", "Home to one of the Caribbean's most-cited beaches -- over two miles of sand and reef. Villas here tend toward larger beachfront estates."),
    ("Barnes Bay", "Quieter, more secluded villa cluster near the Four Seasons property; popular for honeymoons."),
    ("Long Bay", "Secluded stretches of sand with some of the more exclusive, larger estates on the island."),
    ("Rendezvous Bay", "Wide, calm-water beach facing St. Martin across the channel -- a common pick for families given the gentler surf."),
    ("Little Harbour", "A quieter, more private area alongside Barnes Bay."),
    ("Crocus Bay", "North-coast bay near The Valley, Anguilla's capital."),
]

# --- Library: bibliography, NOT full text ----------------------------------
# Every title below is under active copyright (unlike the pre-1900 maps
# elsewhere on this site). This deliberately shows citation + a brief
# original description + a legitimate link to find/buy/borrow each one --
# never the full text or scanned pages, which would be reproducing
# copyrighted work regardless of how it's presented.
LIBRARY_BOOKS = [
    {
        "title": "The Search for the Giant Rodent of Anguilla", "author": "Donald A. McFarland",
        "year": "1991", "publisher": "Anguilla Archaeological & Historical Society",
        "spine_color": "#4a5a3a",
        "description": "A scientific report on Amblyrhiza inundata, an extinct giant rodent whose fossils have been found on Anguilla.",
        "find_url": "https://www.aahsanguilla.com/uploads/7/3/7/1/7371196/the_search_for_the_giant_rodent_of_anguill1.pdf",
    },
    {
        "title": "A Study of Ceramic Sherds from Hughes Estate", "author": "Desmond Nicholson",
        "year": "1985", "publisher": "The Antigua Archaeological and Historical Society",
        "spine_color": "#6a4a2a",
        "description": "Analysis of pottery fragments from the Hughes Estate ruins, dating the site's occupation to roughly 1775-1825.",
        "find_url": "https://www.aahsanguilla.com/hughes-estate-research-project-2021-2023.html",
    },
    {
        "title": "Captain Kidd and his Anguilla Connection", "author": "Nik Douglas",
        "year": "Undated", "publisher": "Anguilla Archaeological & Historical Society",
        "spine_color": "#2a2a4a",
        "description": "Traces the pirate Captain Kidd's documented visit to Anguilla and a 1706 Privy Council accusation against the island's governor for dealing in his goods.",
        "find_url": "https://www.aahsanguilla.com/uploads/7/3/7/1/7371196/captain_kidd.pdf",
    },
    {
        "title": "The History of Boat Racing in Anguilla", "author": "David Carty",
        "year": "Undated", "publisher": "Anguilla Archaeological & Historical Society",
        "spine_color": "#3a6a8a",
        "description": "An essay on Anguilla's national sport, distinct from Carty's full book \"Nuttin Bafflin\" elsewhere in this library.",
        "find_url": "https://www.aahsanguilla.com/uploads/7/3/7/1/7371196/boat_racing_.pdf",
    },
    {
        "title": "Historic Wallblake House: A Historic Past", "author": "David Carty",
        "year": "Undated", "publisher": "Anguilla Archaeological & Historical Society",
        "spine_color": "#5a4a3a",
        "description": "History of one of the few surviving plantation houses on Anguilla, built in the late 18th century by Valentine Blake.",
        "find_url": "https://www.aahsanguilla.com/uploads/7/3/7/1/7371196/wallblake_house.pdf",
    },
    {
        "title": "The Salt Industry of Anguilla: A Brief History", "author": "Sir Emile Gumbs",
        "year": "Undated", "publisher": "Anguilla Archaeological & Historical Society",
        "spine_color": "#7a7a8a",
        "description": "History of Anguilla's salt-raking industry, once producing up to 71,000 barrels a year, from a former Chief Minister of Anguilla.",
        "find_url": "https://www.aahsanguilla.com/uploads/7/3/7/1/7371196/salt.pdf",
    },
    {
        "title": "Tobacco, Cotton, Salt and Dye-Trees", "author": "Don Mitchell",
        "year": "Undated (covers 1650-1700)", "publisher": "Anguilla Archaeological & Historical Society",
        "spine_color": "#4a6a3a",
        "description": "Early Anguilla industries derived directly from colonial archives, by a noted Anguillian judge and historian.",
        "find_url": "https://www.aahsanguilla.com/uploads/7/3/7/1/7371196/other_industries.pdf",
    },
    {
        "title": "People of African Ancestry in Anguilla", "author": "Prof. Don E. Walicek",
        "year": "Undated", "publisher": "Anguilla Archaeological & Historical Society",
        "spine_color": "#6a2a2a",
        "description": "Traces African and Afro-Caribbean presence in Anguilla from the earliest colonial period through emancipation, including the 1698 arrival of the Sally Rose.",
        "find_url": "https://www.aahsanguilla.com/",
    },
    {
        "title": "Annals of Anguilla, 1650-1923", "author": "S. B. Jones",
        "year": "1936", "publisher": "Christian Journals Limited, Belfast",
        "spine_color": "#8b6f47",
        "description": "The foundational documentary history of the island, covering nearly three centuries from early settlement through the 1920s.",
        "find_url": "https://www.worldcat.org/search?q=Annals+of+Anguilla+Jones",
    },
    {
        "title": "The Dilemma of a Ministate: Anguilla", "author": "William J. Brisk",
        "year": "1969", "publisher": "Institute of International Studies, Columbia",
        "spine_color": "#4a5a6a",
        "description": "An early academic study of Anguilla's political status question, written contemporaneously with the 1967 Revolution.",
        "find_url": "https://www.worldcat.org/search?q=Dilemma+of+a+Ministate+Anguilla+Brisk",
    },
    {
        "title": "Political Fragmentation in the Caribbean", "author": "Colin G. Clarke",
        "year": "1971", "publisher": "The Canadian Geographer, 15(1), 13-29",
        "spine_color": "#5c4a6a",
        "description": "A geographer's academic analysis of the small-island secession movements across the Caribbean, using Anguilla as a central case.",
        "find_url": "https://onlinelibrary.wiley.com/journal/15410064",
    },
    {
        "title": "Myths of Caribbean Identity", "author": "Stuart Hall",
        "year": "1981", "publisher": "The Open University (Walter Rodney Memorial Lecture)",
        "spine_color": "#6a2a2a",
        "description": "A landmark lecture by the pioneering cultural theorist, on the construction of Caribbean identity after colonialism.",
        "find_url": "https://www.worldcat.org/search?q=Myths+of+Caribbean+Identity+Stuart+Hall",
    },
    {
        "title": "Caribbean Cultural Identity: The Case of Jamaica", "author": "Rex M. Nettleford",
        "year": "1978", "publisher": "Institute of Jamaica",
        "spine_color": "#2a5a4a",
        "description": "An essay in cultural dynamics from one of the Caribbean's foremost cultural theorists and founder of the National Dance Theatre Company of Jamaica.",
        "find_url": "https://www.worldcat.org/search?q=Caribbean+Cultural+Identity+Nettleford",
    },
    {
        "title": "Anguilla's Battle for Freedom, 1967", "author": "Colville L. Petty",
        "year": "1984", "publisher": "Self-published",
        "spine_color": "#7a4a2a",
        "description": "Petty's first account of the 1967 Revolution -- later expanded into the 1987/2010 co-authored edition with Nat Hodge.",
        "find_url": "https://www.amazon.com/Anguillas-battle-freedom-Colville-Petty/dp/B0006EQPMM",
    },
    {
        "title": "Anguilla: Where There is a Will There is a Way", "author": "Colville L. Petty",
        "year": "1984", "publisher": "Express Lithographics, Surrey",
        "spine_color": "#4a6a7a",
        "description": "Petty's account of Anguilla's post-Revolution development and institution-building.",
        "find_url": "https://www.worldcat.org/search?q=Anguilla+Where+There+is+a+Will+Petty",
    },
    {
        "title": "The Sea and We", "author": "Marcel Fahie",
        "year": "1981-1985", "publisher": "Anguilla Archaeological and Historical Review",
        "spine_color": "#2a4a6a",
        "description": "A serialized reflection on Anguilla's deep maritime tradition, published across several issues of the AAHS Review.",
        "find_url": "https://aahsanguilla.com/",
    },
    {
        "title": "Anguilla's Battle for Freedom, 1967-1969", "author": "Colville L. Petty & A. Nat Hodge",
        "year": "1987 / 2010", "publisher": "PETNAT Publishing, Anguilla",
        "spine_color": "#7a2a2a",
        "description": "The definitive, expanded account of the Revolution, adding a full chapter on the 1969 British invasion to Petty's earlier work.",
        "find_url": "https://books.google.com/books/about/Anguilla_s_Battle_for_Freedom_1967.html?id=61EYAAAAYAAJ",
    },
    {
        "title": "\"Nuttin Bafflin\": The Story of the Anguilla Racing Boat", "author": "David Carty",
        "year": "1997", "publisher": "Anguilla",
        "spine_color": "#3a6a8a",
        "description": "The definitive history of Anguilla's national sport, tracing hand-built racing boats back to the island's 17th-century smuggling trade. A 2011 companion documentary of the same name followed.",
        "find_url": "https://www.imdb.com/title/tt2104949/",
    },
    {
        "title": "Caribbean Life and Culture: A Citizen Reflects", "author": "Sir Fred Phillips",
        "year": "1991", "publisher": "Heinemann Publishers (Caribbean), Jamaica",
        "spine_color": "#5a3a2a",
        "description": "Memoir and reflection from a distinguished Caribbean jurist on regional life, law, and culture.",
        "find_url": "https://www.worldcat.org/search?q=Caribbean+Life+and+Culture+Fred+Phillips",
    },
    {
        "title": "Questioning Creole: Creolisation Discourses in Caribbean Culture", "author": "Verene A. Shepherd & Glen L. Richards (eds.)",
        "year": "2002", "publisher": "Ian Randle Publishers, Kingston",
        "spine_color": "#6a5a2a",
        "description": "An academic anthology examining creolisation theory and its critics across Caribbean cultural studies.",
        "find_url": "https://www.worldcat.org/search?q=Questioning+Creole+Shepherd+Richards",
    },
    {
        "title": "Preserving our Culture, Directing our Future", "author": "Colville L. Petty",
        "year": "2006", "publisher": "anguillaguide.com",
        "spine_color": "#2a6a5a",
        "description": "A web essay by Petty arguing for deliberate cultural preservation as Anguilla's tourism economy grew.",
        "find_url": "https://aahsanguilla.com/",
    },
    {
        "title": "Bless our forebears", "author": "Colville L. Petty",
        "year": "2008", "publisher": "Zenith Services Limited, Trinidad",
        "spine_color": "#7a6a3a",
        "description": "A tribute to Anguillian ancestors and the generations who shaped the island's culture and institutions.",
        "find_url": "https://www.worldcat.org/search?q=Bless+our+forebears+Petty",
    },
    {
        "title": "The Anguillian, Vol. 12, No. 41", "author": "The Anguillian Newspaper",
        "year": "Undated", "publisher": "The Anguillian",
        "spine_color": "#4a4a4a",
        "description": "A specific back issue of Anguilla's long-running weekly newspaper -- for current and archived issues, the paper's own site is the legitimate source.",
        "find_url": "https://theanguillian.com/",
    },
]



ACTIVITIES = [
    ("Prickly Pear Cays boat trip", "Snorkeling / Boat", "Uninhabited cays with reef protected under Anguilla's marine park rules; morning trips give the calmest water and the most cay time."),
    ("Sandy Island sunset cruise", "Boat", "A tiny sandbar cay with a beach bar, popular as an evening cocktail-cruise destination."),
    ("Little Bay snorkeling / Discover Scuba", "Snorkeling / Diving", "A marine park site with shallow (20ft) water well suited to first-time divers and snorkelers alike."),
    ("Shoal Bay East reef snorkeling", "Snorkeling", "Directly off one of Anguilla's most-cited beaches; regularly reports turtles, stingrays, and dense reef fish."),
    ("Scuba Shack Anguilla", "Diving", "A long-established 5-star PADI dive operation with access to 20+ reefs and several wreck sites."),
    ("Special D Diving & Charters", "Diving / Charters", "Locally-run dive charter operation, also offering day trips and fishing expeditions."),
    ("Tradition sailing charter", "Boat / Sailing", "A converted 1978 cargo sailboat turned leisure charter, used for sunset sails and Prickly Pear trips."),
    ("Glass-bottom boat tour, Shoal Bay to Little Bay", "Boat / Snorkeling", "A roughly 2-hour route with both snorkeling stops and glass-bottom viewing of turtles and reef fish."),
    ("St. Martin day trip by boat", "Boat", "Short charter crossings to neighboring St. Martin/St. Maarten for a day of island-hopping."),
    ("Golf at CuisinArt Golf Club", "Golf", "An 18-hole course designed by Greg Norman, one of the Caribbean's few championship-level courses."),
    ("Heritage Collection Museum", "Culture", "A locally-run museum covering Anguilla's history, from Amerindian settlement through the 1967 Revolution."),
    ("Sandy Ground beach walk", "Sightseeing", "The historic salt-trading harbor village, still home to the annual A-class boat races."),
]


ANGUILLA_EVENTS_2026 = [
    {"date": "2026-01-01", "name": "New Year's Day", "note": "Public holiday."},
    {"date": "2026-03-02", "name": "James Ronald Webster Day",
     "note": "Honors the leader of the 1967 and 1969 Anguilla Revolution, established as a holiday in 2010."},
    {"date": "2026-04-03", "name": "Good Friday", "note": "Public holiday; church services held across the island."},
    {"date": "2026-04-06", "name": "Easter Monday", "note": "Public holiday."},
    {"date": "2026-05-01", "name": "Labour Day", "note": "Public holiday, often marked with sports days between government departments and private companies."},
    {"date": "2026-05-25", "name": "Whit Monday", "note": "Public holiday, seven weeks after Easter."},
    {"date": "2026-06-01", "name": "Anguilla Day",
     "note": "The most significant date on the Anguillian calendar, marking the start of the summer festival season; traditionally features A-class boat racing at Sandy Ground."},
    {"date": "2026-06-22", "name": "Celebration of the Birthday of His Majesty The King",
     "note": "Public holiday; uniformed organizations parade, and boat races are held at Crocus Bay."},
    {"date": "2026-08-03", "name": "August Monday", "note": "Marks the start of Summer Festival/Carnival week."},
    {"date": "2026-08-06", "name": "August Thursday", "note": "Traditionally a day for picnics and family reunions, with boat racing at Meads Bay."},
    {"date": "2026-08-07", "name": "Constitution Day",
     "note": "Culmination of Summer Festival, with a costumed parade through The Valley and the Road March competition."},
    {"date": "2026-12-18", "name": "National Heroes and Heroines Day",
     "note": "Honors the figures of the 1967 Anguilla Revolution."},
    {"date": "2026-12-25", "name": "Christmas Day", "note": "Public holiday."},
    {"date": "2026-12-28", "name": "Boxing Day", "note": "Observed on the Monday since Dec 26, 2026 falls on a weekend."},
]


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
        "year": "1838", "title": "An eyewitness account of Emancipation Day in Anguilla",
        "quote": "A Black Methodist minister recounts being stationed in Anguilla when "
                 "emancipation took effect -- the newly-freed continued their work calmly "
                 "on the day itself, but broke into weeping and shouting the following "
                 "Sunday when he preached on their new status.",
        "context": "From an American Quaker delegation's tour documenting conditions "
                    "after British slave emancipation (1834) across the West Indies. "
                    "A rare direct account of how the moment itself was actually "
                    "experienced in Anguilla, rather than a description from outside.",
        "source": "James A. Thome & J. Horace Kimball, \"Emancipation in the West Indies\" (1837/38), via Library of Congress",
        "source_url": "https://www.loc.gov/item/02017739/",
    },
    {
        "year": "1826", "title": "A visiting Englishman's full chapter on Anguilla",
        "quote": "Describes the island's stark, unplanted landscape -- more like Kent or "
                 "Devon than the sugar islands -- an aging lieutenant governor "
                 "proudly recalling repelling a French attack, and a legal system "
                 "so under-resourced that a 1809 writ wasn't executed until 1818 for "
                 "want of a jail. Recounts the 1796 French raid that burned the church "
                 "and church and stripped and murdered residents, and calls Anguilla "
                 "an \u201cunjustly forgotten colony.\u201d Gives a population breakdown "
                 "(365 white, 327 free-colored, 2,388 enslaved) and describes the "
                 "salt pond as the island's one real export, calling free trade what "
                 "\u201cwould be charity to Anguilla.\u201d",
        "context": "By far the richest and most detailed pre-20th-century account of "
                    "Anguilla found in this whole project -- a full ~13-page dedicated "
                    "chapter, not a passing mention. Written by Henry Nelson Coleridge "
                    "(nephew of the poet Samuel Taylor Coleridge), who toured the West "
                    "Indies in 1825 with his cousin, the Bishop of Barbados, and "
                    "published this account anonymously the following year.",
        "source": "Henry Nelson Coleridge, \"Six Months in the West Indies in 1825\" (1826), via Library of Congress",
        "source_url": "https://www.loc.gov/item/ltf91000120/",
    },
    {
        "year": "1887", "title": "A French spy's report, and a documented Irish chapter",
        "quote": "Cites a French agent sent from St. Christopher over 200 years earlier "
                 "(so, mid-1600s) to assess Anguilla, who reported it \"not deemed worth "
                 "the trouble of keeping or cultivating\" -- independently echoing the "
                 "1678 English account elsewhere on this page. Also cites the historian "
                 "Oldmixon on a group of Irish settlers (\"Wild Irish\") who reportedly "
                 "displaced earlier fishermen and fell into internal conflict before "
                 "Britain assumed direct governance.",
        "context": "By 1887, population was estimated at ~2,500 (100 white), annual "
                    "revenue under \u00a3600, taxation about five shillings per capita -- "
                    "governed by a stipendiary magistrate and a seven-member vestry.",
        "source": "\"Down the Islands: A Voyage to the Caribbees\" (1887), via Library of Congress",
        "source_url": "https://www.loc.gov/item/02013426/",
    },
    {
        "year": "1868", "title": "Confirming the \"Eel\" etymology, and mapping the surrounding cays",
        "quote": "Explicitly attributes the name Anguilla to the island's \"long and narrow... "
                 "irregular and much twisted\" shoreline, resembling an eel. Details the "
                 "reef-lined south coast, and the surrounding rocks and cays -- Grand and "
                 "Little Scrub, Sandy Island, and the Anguilletta (also called Blowing "
                 "Rock, for a blowhole resembling a whale).",
        "context": "From official British Admiralty sailing directions -- pairs naturally "
                    "with the historical sounding charts elsewhere on this site.",
        "source": "\"Sailing Directions for the West Indies\" (1868), via Library of Congress",
        "source_url": "https://www.loc.gov/item/ltf91089143/",
    },
    {
        "year": "1912", "title": "A German geologist's classification (translated from German)",
        "quote": "Groups Anguilla with Tintamarre and Barbuda as part of the Antilles' "
                 "\"flat outer zone,\" contrasting them with the volcanic inner-zone "
                 "islands (Guadeloupe's Grande Soufri\u00e8re reaches 1,484m). Separately "
                 "notes that on Tobago, Anguilla, and Barbuda specifically, pastoral "
                 "grazing -- not crop plantation -- formed the main occupation of "
                 "inhabitants, unlike most other islands.",
        "context": "From a German geographical survey of Central America, the Lesser "
                    "Antilles, and the Dutch West and East Indies -- a genuinely "
                    "different national/scientific perspective (geological rather than "
                    "colonial-administrative) from most other sources in this archive.",
        "source": "\"Mittelamerika, Kleine Antillen, Niederl\u00e4ndisch-West- und Ostindien\" (1912), via Library of Congress -- translated from German",
        "source_url": "https://www.loc.gov/item/13014850/",
    },
    {
        "year": "1866", "title": "A different French attack, and outnumbered defenders who won",
        "quote": "Describes 600 French troops landing in 1746 against a defending force of "
                 "only 150 armed Anguillans -- who nonetheless killed 150 of the "
                 "attackers and forced the rest to retreat. Also details 1861 revenue "
                 "(\u00a3414) and expenditure (\u00a3240), and the island's governance "
                 "structure under a Stipendiary Magistrate and elected Vestry.",
        "context": "A separate incident from the 1796 French attack described in "
                    "Coleridge's 1826 account elsewhere on this page -- Anguilla was "
                    "attacked by the French more than once. Note: this source gives "
                    "the settlement date as \"1450,\" almost certainly an OCR/printing "
                    "error for 1650, the well-documented actual date.",
        "source": "\"Trinidad and the Other West India Islands and Colonies\" (1866), via Library of Congress",
        "source_url": "https://www.loc.gov/item/24031382/",
    },
    {
        "year": "1922", "title": "A thin archaeological record, even by a museum's own account",
        "quote": "A museum collections guide notes that finds from Anguilla and St. "
                 "Eustatius were \"small\" with \"little character,\" compared to richer "
                 "Carib-influenced stone tools and pottery documented from St. Kitts, "
                 "Nevis, and Montserrat.",
        "context": "Even in archaeology, Anguilla shows up as comparatively "
                    "under-documented next to its neighbors -- consistent with the "
                    "broader pattern across this whole archive.",
        "source": "\"Guide to the Collections from the West Indies\" (1922), via Library of Congress",
        "source_url": "https://www.loc.gov/item/23007166/",
    },
    {
        "year": "1917", "title": "Another nickname, and a pirate-haunt footnote",
        "quote": "Confirms the island as \"sometimes known as Eel Island and Little Snake\" -- "
                 "a second nickname corroborating the 1835 gazetteer's \"Snake Island\" -- "
                 "notes a population of about 3,000, cattle and pony export, and that it "
                 "was \"formerly a resort of freebooters.\"",
        "context": "A traveler's gazetteer appendix entry, brief but useful for "
                    "corroborating details found independently in other sources on "
                    "this page (the snake-related nicknames, and the same pirate "
                    "history documented separately via the Captain Kidd connection "
                    "in this site's Library).",
        "source": "\"The Book of the West Indies\" (1917), via Library of Congress",
        "source_url": "https://www.loc.gov/item/17029601/",
    },
    {
        "year": "1893", "title": "A vivid, literary sense of isolation",
        "quote": "Describes an Administrator's posting to Anguilla as a desolate, "
                 "vegetation-scarce islet visited only monthly by a ship bringing mail -- "
                 "the wait for it described as the one bright point in an otherwise "
                 "isolated existence for the small white administrative household.",
        "context": "A literary, atmospheric sketch of West Indian social life rather than "
                    "a strict statistical account -- captures the psychological experience "
                    "of an isolated colonial posting in a way the gazetteers don't.",
        "source": "\"Gossip of the Caribbees: Sketches of Anglo-West-Indian Life\" (1893), via Library of Congress",
        "source_url": "https://www.loc.gov/item/08028484/",
    },
    {
        "year": "1867", "title": "Anguilla in a 1627 royal land grant (translated from French)",
        "quote": "Lists Anguilla among a long chain of islands -- from Grenada up through "
                 "the Virgin Islands -- included in King Charles I's grants intended to "
                 "form a single colony to be called \u201cthe Carlisles.\u201d A footnote "
                 "identifies Anguilla as \u201cthe most northern of the Windward Isles\u201d "
                 "(this French source's period classification differs slightly from the "
                 "modern English convention of grouping it with the Leeward Islands).",
        "context": "Translated from a French-language history of early English "
                    "Caribbean colonization, tracing land grants back to Charles I's "
                    "1627 and 1628 charters -- placing Anguilla's inclusion in English "
                    "colonial planning earlier than its actual 1650 settlement.",
        "source": "\"Les colonies anglaises de 1574 \u00e0 1660\" (1867), via Library of Congress -- translated from French",
        "source_url": "https://www.loc.gov/item/13022174/",
    },
    {
        "year": "1857", "title": "A Dutch pilot mistakes Anguilla for its neighbor",
        "quote": "Sailing to New Netherland (later New York) in 1632, a Dutch ship's "
                 "helmsman mistook Anguilla for the nearby island of Sombrero as they "
                 "passed by at evening -- a small, human moment of period navigation.",
        "context": "From a Dutch sea captain's journal of transatlantic voyages "
                    "(1632-1644), published in English translation in 1857.",
        "source": "\"Voyages from Holland to America, A.D. 1632 to 1644\" (1857), via Library of Congress",
        "source_url": "https://www.loc.gov/item/11021759/",
    },
    {
        "year": "1855", "title": "A mid-century statistical snapshot",
        "quote": "Gives Anguilla's area (34 square miles), population (3,052), and economy "
                 "(cattle-breeding, salt-raking, small-scale sugar/cotton/tobacco), notes "
                 "governance by a locally-elected magistrate answerable to Antigua's "
                 "governor, and dates English settlement to 1659.",
        "context": "From a comprehensive statistical gazetteer of the West India Islands.",
        "source": "\"A Statistical Account of the West India Islands\" (1855), via Library of Congress",
        "source_url": "https://www.loc.gov/item/01000662/",
    },
    {
        "year": "1811", "title": "Anguillians as founders, not just forgotten",
        "quote": "Describes how, after the Dutch were expelled from Tortola and made "
                 "little progress developing it, credit for the colony's growth went to "
                 "English settlers from Anguilla, who emigrated with their families and "
                 "founded a nearly tax-free, self-governing community there.",
        "context": "From John Pinkerton's \"Modern Geography\" (1811) -- a rare "
                    "documented instance of Anguilla as a source of settlers who went "
                    "on to build up a neighboring colony, rather than the usual story "
                    "of being overlooked. Notes the Virgin Islands' 1756 population "
                    "(1,263 white, 6,121 Black) and that formal courts weren't "
                    "established there until 1773.",
        "source": "John Pinkerton, 1811, via Library of Congress",
        "source_url": "https://www.loc.gov/item/30033189/",
    },
    {
        "year": "1678", "title": "\"A Description of the Island of Anguilla\"",
        "quote": "A dedicated section giving the island's coordinates and dimensions, "
                 "then bluntly assessing its ~200-300 English inhabitants as poor, "
                 "concluding the isle was \u201cnot worth the keeping.\u201d",
        "context": "Part of Richard Blome's \"A Description of the Island of Jamaica: "
                    "with the other isles and territories in America to which the "
                    "English are related\" -- one of the earliest known dedicated "
                    "English descriptions of Anguilla specifically, and a blunt "
                    "confirmation of the colonial neglect that shaped the island's "
                    "whole documentary record.",
        "source": "Richard Blome, 1678, via Oxford Text Creation Partnership",
        "source_url": "https://ota.bodleian.ox.ac.uk/repository/xmlui/bitstream/handle/20.500.12024/A28392/A28392.html",
    },
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


@app.route("/ai-info")
def ai_info():
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
        "ai-info.html",
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
        active_page="ai-info",
    )


@app.route("/")
def index():
    """New lean homepage: the live interactive map is the key engagement
    point, per Noel's direction -- not dominated by .ai metrics anymore."""
    return render_template("index.html", now=datetime.utcnow(), active_page="map")


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
def map_redirect():
    """Old combined map page -- now split into focused pages. Redirect to
    the new homepage so old links/bookmarks still work."""
    from flask import redirect
    return redirect("/", code=301)


@app.route("/historical-maps")
def historical_maps():
    sorted_items = sorted(ARCHIVE_ITEMS, key=_archive_sort_key)
    return render_template("historical-maps.html", now=datetime.utcnow(),
                            archive_items=sorted_items,
                            archive_gaps=ARCHIVE_KNOWN_GAPS,
                            active_page="historical-maps")


@app.route("/island-history")
def island_history():
    return render_template("island-history.html", now=datetime.utcnow(),
                            historical_accounts=HISTORICAL_ACCOUNTS,
                            events=ANGUILLA_EVENTS_2026,
                            active_page="island-history")


@app.route("/news")
def news_page():
    return render_template("news.html", now=datetime.utcnow(), active_page="news")


@app.route("/restaurants")
def restaurants_page():
    return render_template("restaurants.html", now=datetime.utcnow(),
                            restaurants=RESTAURANTS, active_page="restaurants")


@app.route("/villas")
def villas_page():
    return render_template("villas.html", now=datetime.utcnow(),
                            villa_areas=VILLA_AREAS, active_page="villas")


@app.route("/activities")
def activities_page():
    return render_template("activities.html", now=datetime.utcnow(),
                            activities=ACTIVITIES, active_page="activities")


@app.route("/library")
def library_page():
    return render_template("library.html", now=datetime.utcnow(),
                            books=LIBRARY_BOOKS, readable_books=READABLE_BOOKS,
                            active_page="library")


# Public-domain books with full reader support (confirmed PD, unlike the
# under-copyright bibliography above). Each needs a static page-data JSON
# file at static/reader-data/{slug}.json (page text + image URL per page).
READABLE_BOOKS = [
    {
        "slug": "coleridge-1826",
        "title": "Six Months in the West Indies in 1825",
        "author": "Henry Nelson Coleridge", "year": "1826",
        "spine_color": "#5a3319",  # leather brown
        "source_url": "https://www.loc.gov/item/ltf91000120/",
        "description": "The richest single account of 1820s Anguilla found in this "
                        "project -- a full dedicated chapter on the island.",
    },
    {
        "slug": "down-islands-1887",
        "title": "Down the Islands: A Voyage to the Caribbees",
        "author": "William Agnew Paton", "year": "1887",
        "spine_color": "#2a4a5a",
        "source_url": "https://www.loc.gov/item/02013426/",
        "description": "A full chapter on Anguilla, including a 200-year-old French "
                        "spy's report echoing the island's \"not worth keeping\" "
                        "reputation, and documented history of Irish settlers.",
    },
    {
        "slug": "trinidad-1866",
        "title": "Trinidad and the Other West India Islands and Colonies",
        "author": "James Hume Collens", "year": "1866",
        "spine_color": "#7a2a2a",
        "source_url": "https://www.loc.gov/item/24031382/",
        "description": "Includes a dedicated Anguilla section: a separate 1746 French "
                        "attack repelled by outnumbered defenders, revenue figures, and "
                        "governance structure.",
    },
    {
        "slug": "sailing-directions-1868",
        "title": "Sailing Directions for the West Indies",
        "author": "British Admiralty", "year": "1868",
        "spine_color": "#1a3a4a",
        "source_url": "https://www.loc.gov/item/ltf91089143/",
        "description": "Official nautical sailing directions with a full section on "
                        "Anguilla's coastline, reefs, and surrounding cays -- confirms the "
                        "island's \"Eel\" etymology directly.",
    },
    {
        "slug": "gossip-caribbees-1893",
        "title": "Gossip of the Caribbees: Sketches of Anglo-West-Indian Life",
        "author": "Anonymous", "year": "1893",
        "spine_color": "#4a3a5a",
        "description": "A literary, atmospheric account of an administrator's isolated "
                        "posting to Anguilla -- visited only monthly by a mail ship.",
        "source_url": "https://www.loc.gov/item/08028484/",
    },
    {
        "slug": "pinkerton-1811",
        "title": "Modern Geography, Vol. 2",
        "author": "John Pinkerton", "year": "1811",
        "spine_color": "#3a5a3a",
        "source_url": "https://www.loc.gov/item/30033189/",
        "description": "A massive world gazetteer with a real find buried inside: "
                        "Anguillian settlers credited with founding the Virgin Islands colony.",
    },
    {
        "slug": "west-indies-1911",
        "title": "The West Indies",
        "author": "John Fiske (posthumous)", "year": "1911",
        "spine_color": "#5a2a4a",
        "source_url": "https://www.loc.gov/item/21007149/",
        "description": "An entire chapter (XX) devoted to Anguilla, St. Martin, St. "
                        "Bartholomew, Barbuda, and Antigua -- opens by explaining the "
                        "island's \"snake\" name etymology and its bleak volcanic-versus-"
                        "coralline island geology.",
    },
]


@app.route("/library/read/<slug>")
def library_reader(slug):
    book = next((b for b in READABLE_BOOKS if b["slug"] == slug), None)
    if not book:
        from flask import abort
        abort(404)
    return render_template("reader.html", now=datetime.utcnow(),
                            slug=slug, book_title=book["title"], book_author=book["author"],
                            book_year=book["year"], book_source_url=book["source_url"],
                            data_url=f"/static/reader-data/{slug}.json")


@app.route("/api/anguilla-businesses.json")
def api_anguilla_businesses():
    session = Session()
    rows = session.query(AnguillaBusiness).all()
    session.close()
    return jsonify([
        {"name": b.name or "Unnamed", "category": b.category,
         "layer_group": b.layer_group or "Other",
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
