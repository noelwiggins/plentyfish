import os
from datetime import datetime, timedelta

from flask import Flask, render_template, jsonify
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker

from models import Base, AnguillaRevenue, DiscoveredDomain, TrancoCheck, TopAiSite

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///plentyfish_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)


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

def get_revenue_context():
    session = Session()
    years = (session.query(AnguillaRevenue)
             .filter_by(granularity="year")
             .order_by(AnguillaRevenue.period_label)
             .all())
    months = (session.query(AnguillaRevenue)
              .filter_by(granularity="month")
              .order_by(AnguillaRevenue.period_label)
              .all())
    session.close()

    actual_years = [y for y in years if not y.is_projection]
    latest_actual = actual_years[-1] if actual_years else None
    projected_years = [y for y in years if y.is_projection]

    daily_estimate = None
    if latest_actual:
        days = (latest_actual.period_end - latest_actual.period_start).days + 1
        daily_estimate = latest_actual.revenue_usd / days

    return {
        "years": years,
        "months": months,
        "latest_actual": latest_actual,
        "projected_years": projected_years,
        "daily_estimate": daily_estimate,
        "weekly_estimate": daily_estimate * 7 if daily_estimate else None,
        "monthly_estimate": daily_estimate * 30 if daily_estimate else None,
    }


@app.route("/")
def dashboard():
    ctx = get_revenue_context()

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
             .order_by(AnguillaRevenue.period_label).all())
    session.close()
    return jsonify([
        {
            "label": y.period_label,
            "revenue_usd": y.revenue_usd,
            "is_projection": y.is_projection,
            "cumulative_registrations": y.total_registrations_cumulative,
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


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
