"""
Pulls Anguilla-specific news headlines from local outlets' own public RSS
feeds. Deliberately NOT using Google News' RSS (https://news.google.com/rss/...)
even though it works technically -- its own copyright notice states the
feed is "made available solely for the purpose of rendering Google News
results within a personal feed reader for personal, non-commercial use.
Any other use... is expressly prohibited." A public site with commercial
intent (affiliate links) doesn't qualify.

Using each outlet's own first-party RSS feed sidesteps that: these are
published by the outlets themselves for syndication. We store headline +
link + publish date only, never article body/excerpt text, matching
standard news-aggregator practice (this is what Google News, Apple News,
etc. all do) and staying well inside safe copyright territory.

Sources:
  - Anguilla Focus (anguillafocus.com) -- actively updated, primary source.
  - The Anguillian (theanguillian.com) -- included as a secondary source;
    its feed has been observed to update less frequently.

Run: python scripts/fetch_anguilla_news.py
Requires DATABASE_URL env var (falls back to local sqlite for dev).
"""
import os
import re
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models import Base, NewsItem  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

UA = "plentyfish.ai news aggregator (contact: noel@plentyfish.ai)"

FEEDS = [
    {"url": "https://anguillafocus.com/feed/", "source": "Anguilla Focus"},
    {"url": "https://theanguillian.com/feed/", "source": "The Anguillian"},
]

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.S)
PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)


def unescape(s: str) -> str:
    return (s.replace("&#8217;", "'").replace("&#8216;", "'")
             .replace("&#8220;", '"').replace("&#8221;", '"')
             .replace("&#8211;", "-").replace("&#8212;", "--")
             .replace("&amp;", "&").replace("&#038;", "&")
             .strip())


def fetch_feed(url: str, source: str):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[warn] {source}: fetch failed: {e}")
        return []

    items = []
    for raw_item in ITEM_RE.findall(r.text):
        title_m = TITLE_RE.search(raw_item)
        link_m = LINK_RE.search(raw_item)
        pub_m = PUBDATE_RE.search(raw_item)
        if not (title_m and link_m):
            continue
        title = unescape(title_m.group(1))
        link = link_m.group(1).strip()
        published_at = None
        if pub_m:
            try:
                published_at = parsedate_to_datetime(pub_m.group(1).strip())
                if published_at.tzinfo:
                    published_at = published_at.astimezone(tz=None).replace(tzinfo=None)
            except Exception:
                published_at = None
        items.append({"title": title, "link": link, "source": source,
                      "published_at": published_at})
    return items


def run():
    Base.metadata.create_all(engine)
    session = Session()

    total_new = 0
    for feed in FEEDS:
        items = fetch_feed(feed["url"], feed["source"])
        new_here = 0
        for item in items:
            exists = session.query(NewsItem).filter_by(link=item["link"]).first()
            if exists:
                continue
            session.add(NewsItem(
                title=item["title"], link=item["link"], source=item["source"],
                published_at=item["published_at"], fetched_at=datetime.utcnow(),
            ))
            new_here += 1
        session.commit()
        print(f"[ok] {feed['source']}: {len(items)} items seen, {new_here} new")
        total_new += new_here

    # Keep the table from growing unbounded -- retain the most recent 300 items.
    all_items = (session.query(NewsItem)
                 .order_by(NewsItem.published_at.desc().nullslast())
                 .all())
    for old in all_items[300:]:
        session.delete(old)
    session.commit()

    session.close()
    print(f"[done] {total_new} new headlines total")


if __name__ == "__main__":
    run()
