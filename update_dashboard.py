#!/usr/bin/env python3
"""
Alaya Ads Dashboard - Daily Data Refresh
=========================================
Pulls Meta Ads performance data via the Graph API and writes data.json
for the dashboard to consume.

Usage:
    export META_ACCESS_TOKEN="EAAB..."
    export META_AD_ACCOUNT_ID="1276356387745308"
    python3 update_dashboard.py

Outputs:
    data.json (overwrites the existing file)

Requires: requests (pip install requests)
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ===== CONFIG =====
GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SYDNEY_TZ = timezone(timedelta(hours=10))  # AEST. AEDT shifts handled by Meta - we just label.

# How many top/bottom ads to surface
TOP_N = 5

# ===== HELPERS =====
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)

def fetch(endpoint, params=None):
    """Hit the Graph API and unwrap pagination."""
    url = f"{GRAPH_BASE}/{endpoint}"
    params = params or {}
    params["access_token"] = ACCESS_TOKEN
    all_data = []
    while url:
        resp = requests.get(url, params=params if "?" not in url else None, timeout=30)
        if resp.status_code != 200:
            log(f"ERROR {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
        payload = resp.json()
        all_data.extend(payload.get("data", []))
        # Pagination: Meta returns paging.next as a fully-formed URL
        url = payload.get("paging", {}).get("next")
        params = None  # next URL already has token + params baked in
    return all_data

def get_insights(level, date_preset=None, time_range=None, fields=None, limit=500):
    """Fetch insights at account/campaign/adset/ad level."""
    fields = fields or [
        "campaign_id", "campaign_name",
        "adset_id", "adset_name",
        "ad_id", "ad_name",
        "spend", "impressions", "clicks", "ctr", "cpm", "cpc",
        "actions", "action_values", "cost_per_action_type",
        "date_start", "date_stop"
    ]
    params = {
        "level": level,
        "fields": ",".join(fields),
        "limit": limit,
    }
    if date_preset:
        params["date_preset"] = date_preset
    if time_range:
        params["time_range"] = json.dumps(time_range)
    return fetch(f"act_{AD_ACCOUNT_ID}/insights", params)

def extract_results(row):
    """
    Pull the primary result count from an insights row.
    Priority: lead → onsite_conversion.lead_grouped → purchase → link_click.
    Returns (result_count, result_type_label).
    """
    actions = row.get("actions") or []
    cost_per = {a["action_type"]: float(a["value"]) for a in (row.get("cost_per_action_type") or [])}

    # Build a lookup of action_type → count
    action_counts = {a["action_type"]: float(a["value"]) for a in actions}

    # Try common lead/conversion event types in order
    for event_type, label in [
        ("lead", "Leads"),
        ("onsite_conversion.lead_grouped", "Leads"),
        ("offsite_conversion.fb_pixel_lead", "Leads"),
        ("purchase", "Purchases"),
        ("offsite_conversion.fb_pixel_purchase", "Purchases"),
        ("complete_registration", "Registrations"),
        ("link_click", "Link Clicks"),
    ]:
        if event_type in action_counts:
            count = action_counts[event_type]
            cpr = cost_per.get(event_type, 0)
            if not cpr and count > 0:
                cpr = float(row.get("spend", 0)) / count
            return count, label, cpr

    return 0, "Results", 0


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def pct_change(new, old):
    """Return % change from old to new, or 0 if old is 0."""
    if not old or old == 0:
        return 0
    return ((new - old) / old) * 100


# ===== MAIN =====
def main():
    today = datetime.now(SYDNEY_TZ).date()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)
    seven_days_ago = today - timedelta(days=7)
    month_start = today.replace(day=1)

    log(f"Pulling ads data for ad account {AD_ACCOUNT_ID}")
    log(f"Today (Sydney): {today}")

    # ---------- Account name ----------
    log("Fetching account info...")
    acct = requests.get(
        f"{GRAPH_BASE}/act_{AD_ACCOUNT_ID}",
        params={"fields": "name,currency", "access_token": ACCESS_TOKEN},
        timeout=30
    ).json()
    account_name = acct.get("name", "Alaya Property")
    currency = acct.get("currency", "AUD")
    log(f"  → {account_name} ({currency})")

    # ---------- Yesterday ----------
    log("Fetching yesterday's snapshot...")
    yesterday_rows = get_insights(
        level="account",
        time_range={"since": yesterday.isoformat(), "until": yesterday.isoformat()}
    )
    daily_results, daily_result_label, daily_cpr = (0, "Results", 0)
    daily_spend = daily_imp = daily_clicks = daily_ctr = daily_cpm = 0
    if yesterday_rows:
        r = yesterday_rows[0]
        daily_spend = safe_float(r.get("spend"))
        daily_imp = int(safe_float(r.get("impressions")))
        daily_clicks = int(safe_float(r.get("clicks")))
        daily_ctr = safe_float(r.get("ctr"))
        daily_cpm = safe_float(r.get("cpm"))
        daily_results, daily_result_label, daily_cpr = extract_results(r)

    # ---------- Day-before-yesterday (for delta) ----------
    log("Fetching day-before for delta...")
    dby_rows = get_insights(
        level="account",
        time_range={"since": day_before.isoformat(), "until": day_before.isoformat()}
    )
    dby_spend = dby_results = dby_cpr = 0
    if dby_rows:
        r = dby_rows[0]
        dby_spend = safe_float(r.get("spend"))
        dby_results, _, dby_cpr = extract_results(r)

    # ---------- Last 7 days (per-day series) ----------
    log("Fetching 7-day timeseries...")
    weekly_series = []
    weekly_spend_total = 0
    weekly_results_total = 0
    for i in range(6, -1, -1):  # 6 days ago → today (yesterday is most recent complete)
        d = yesterday - timedelta(days=i)
        rows = get_insights(
            level="account",
            time_range={"since": d.isoformat(), "until": d.isoformat()}
        )
        s, res = 0, 0
        if rows:
            s = safe_float(rows[0].get("spend"))
            res, _, _ = extract_results(rows[0])
        weekly_series.append({
            "date": d.strftime("%a %d"),
            "spend": round(s, 2),
            "results": int(res),
        })
        weekly_spend_total += s
        weekly_results_total += res

    weekly_cpr = (weekly_spend_total / weekly_results_total) if weekly_results_total else 0

    # ---------- Month to date ----------
    log("Fetching MTD totals...")
    mtd_rows = get_insights(
        level="account",
        time_range={"since": month_start.isoformat(), "until": yesterday.isoformat()}
    )
    mtd_spend = mtd_results = mtd_cpr = 0
    if mtd_rows:
        r = mtd_rows[0]
        mtd_spend = safe_float(r.get("spend"))
        mtd_results, _, mtd_cpr = extract_results(r)

    days_elapsed = (yesterday - month_start).days + 1
    daily_pace = mtd_spend / days_elapsed if days_elapsed else 0
    pacing_note = f"Pacing ~{daily_pace:,.0f} AUD/day"

    # ---------- Top / bottom ads (last 7 days) ----------
    log("Fetching top/bottom ads (last 7 days)...")
    ad_rows = get_insights(
        level="ad",
        time_range={"since": seven_days_ago.isoformat(), "until": yesterday.isoformat()},
        limit=200
    )
    ad_perf = []
    for r in ad_rows:
        spend = safe_float(r.get("spend"))
        if spend < 1:  # skip ads with negligible spend
            continue
        results, _, cpr = extract_results(r)
        ad_perf.append({
            "name": r.get("ad_name", "Unknown"),
            "spend": round(spend, 2),
            "results": int(results),
            "cost_per_result": round(cpr, 2) if cpr else 0,
        })

    # Top: lowest CPR among ads with at least 1 result
    with_results = [a for a in ad_perf if a["results"] > 0]
    top_ads = sorted(with_results, key=lambda x: x["cost_per_result"])[:TOP_N]
    # Bottom: highest spend with zero or worst CPR
    no_results = [a for a in ad_perf if a["results"] == 0]
    if no_results:
        bottom_ads = sorted(no_results, key=lambda x: -x["spend"])[:TOP_N]
    else:
        bottom_ads = sorted(with_results, key=lambda x: -x["cost_per_result"])[:TOP_N]

    # ---------- Campaigns ----------
    log("Fetching campaign list + 7-day perf...")
    camp_meta = fetch(f"act_{AD_ACCOUNT_ID}/campaigns", {
        "fields": "id,name,status,daily_budget,effective_status",
        "limit": 100,
    })
    camp_perf_rows = get_insights(
        level="campaign",
        time_range={"since": seven_days_ago.isoformat(), "until": yesterday.isoformat()},
        limit=100
    )
    perf_lookup = {r["campaign_id"]: r for r in camp_perf_rows}
    campaigns = []
    for c in camp_meta:
        if c.get("effective_status") in ("DELETED", "ARCHIVED"):
            continue
        perf = perf_lookup.get(c["id"], {})
        spend = safe_float(perf.get("spend"))
        results, _, cpr = extract_results(perf) if perf else (0, "", 0)
        daily_budget = safe_float(c.get("daily_budget", 0)) / 100  # cents → AUD
        campaigns.append({
            "id": c["id"],
            "name": c["name"],
            "status": c.get("effective_status", c.get("status", "UNKNOWN")),
            "spend": round(spend, 2),
            "results": int(results),
            "cost_per_result": round(cpr, 2) if cpr else 0,
            "daily_budget": round(daily_budget, 2),
        })
    # Sort by spend descending
    campaigns.sort(key=lambda c: -c["spend"])

    # ---------- Build data.json ----------
    output = {
        "meta": {
            "last_updated": datetime.now(SYDNEY_TZ).isoformat(),
            "report_for_date": yesterday.strftime("%a %d %b %Y"),
            "currency": currency,
            "account_name": account_name,
            "is_placeholder": False,
            "note": ""
        },
        "daily": {
            "date": yesterday.strftime("%a %d %b"),
            "spend": round(daily_spend, 2),
            "results": int(daily_results),
            "result_type": daily_result_label,
            "cost_per_result": round(daily_cpr, 2),
            "impressions": daily_imp,
            "clicks": daily_clicks,
            "ctr": round(daily_ctr, 2),
            "cpm": round(daily_cpm, 2),
            "vs_yesterday": {
                "spend_pct": round(pct_change(daily_spend, dby_spend), 1),
                "results_pct": round(pct_change(daily_results, dby_results), 1),
                "cpr_pct": round(pct_change(daily_cpr, dby_cpr), 1),
            }
        },
        "weekly": {
            "range": f"{seven_days_ago.strftime('%d %b')} - {yesterday.strftime('%d %b')}",
            "spend": round(weekly_spend_total, 2),
            "results": int(weekly_results_total),
            "cost_per_result": round(weekly_cpr, 2),
            "daily_series": weekly_series,
        },
        "mtd": {
            "month": today.strftime("%B %Y"),
            "spend": round(mtd_spend, 2),
            "results": int(mtd_results),
            "cost_per_result": round(mtd_cpr, 2),
            "days_elapsed": days_elapsed,
            "pacing_note": pacing_note,
        },
        "top_ads": top_ads,
        "bottom_ads": bottom_ads,
        "campaigns": campaigns,
    }

    out_path = Path(__file__).parent / "data.json"
    out_path.write_text(json.dumps(output, indent=2))
    log(f"Wrote {out_path}")
    log(f"  Yesterday: {daily_spend:,.2f} AUD spend, {int(daily_results)} {daily_result_label.lower()}")
    log(f"  Last 7 days: {weekly_spend_total:,.2f} AUD spend, {int(weekly_results_total)} results")
    log(f"  MTD: {mtd_spend:,.2f} AUD across {days_elapsed} days")
    log(f"  Active campaigns: {len(campaigns)}")
    log("Done.")


if __name__ == "__main__":
    ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
    AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "1276356387745308")
    if not ACCESS_TOKEN:
        print("ERROR: Set META_ACCESS_TOKEN environment variable", file=sys.stderr)
        sys.exit(1)
    main()
