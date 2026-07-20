"""
Fetches business/POI data for Anguilla from OpenStreetMap via the
Overpass API -- free, no key, community-maintained map data.

Categories pulled: restaurants, bars/pubs, cafes, hotels, shops,
supermarkets, banks, pharmacies -- a reasonable general-purpose set for a
"businesses" map layer. OSM's `amenity`/`shop`/`tourism` tags are used
directly as the category label.

Run: python scripts/fetch_anguilla_businesses.py
Requires DATABASE_URL env var (falls back to local sqlite for dev).
"""
import os
import sys
from datetime import datetime

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import Base, AnguillaBusiness  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

UA = "plentyfish.ai Anguilla map (contact: noel@plentyfish.ai)"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Anguilla bounding box (south, west, north, east) -- the island plus a
# small margin, not the wider Anguilla Bank.
BBOX = "18.15,-63.20,18.30,-62.95"

AMENITY_TAGS = "restaurant|bar|cafe|pub|bank|pharmacy|fuel"
SHOP_TAGS = "supermarket|convenience|clothes|gift|jewelry"

QUERY = f"""
[out:json][timeout:60];
(
  node["amenity"~"{AMENITY_TAGS}"]({BBOX});
  node["shop"~"{SHOP_TAGS}"]({BBOX});
  node["tourism"="hotel"]({BBOX});
);
out body;
"""


def run():
    Base.metadata.create_all(engine)
    session = Session()

    try:
        r = requests.post(OVERPASS_URL, data={"data": QUERY},
                           headers={"User-Agent": UA}, timeout=90)
        r.raise_for_status()
        elements = r.json().get("elements", [])
    except Exception as e:
        print(f"[error] Overpass fetch failed: {e}")
        session.close()
        raise

    seen_osm_ids = set()
    new_count = 0
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        category = (tags.get("amenity") or tags.get("shop") or tags.get("tourism") or "other")
        osm_id = f"node/{el['id']}"
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None or lon is None:
            continue
        seen_osm_ids.add(osm_id)

        row = session.query(AnguillaBusiness).filter_by(osm_id=osm_id).first()
        if row:
            row.name = name
            row.category = category
            row.latitude = lat
            row.longitude = lon
            row.fetched_at = datetime.utcnow()
        else:
            session.add(AnguillaBusiness(
                osm_id=osm_id, name=name, category=category,
                latitude=lat, longitude=lon, fetched_at=datetime.utcnow(),
            ))
            new_count += 1

    # Remove entries no longer present in OSM (closed businesses, edits, etc.)
    removed = 0
    for row in session.query(AnguillaBusiness).all():
        if row.osm_id not in seen_osm_ids:
            session.delete(row)
            removed += 1

    session.commit()
    session.close()
    print(f"[done] {len(elements)} POIs fetched, {new_count} new, {removed} removed")


if __name__ == "__main__":
    run()
