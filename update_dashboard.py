#!/usr/bin/env python3
"""
Alaya Ads Dashboard - Daily Data Refresh (v2)
==============================================
Pulls 365 days of Meta Ads performance at account, campaign, and ad level
plus creative metadata, then writes a single data.json for the dashboard
to filter client-side.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SYDNEY_TZ = timezone(timedelta(hours=10))
LOOKBACK_DAYS = 365

INSIGHT_FIELDS = [
    "campaign_id", "campaign_name",
    "adset_id", "adset_name",
    "ad_id", "ad_name",
    "spend", "impressions", "clicks", "ctr", "cpm", "cpc",
    "actions", "cost_per_action_type",
    "date_start"
]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def fetch(endpoint, params=None, retries=3):
    url = f"{GRAPH_BASE}/{endpoint}"
    params = params or {}
    params["access_token"] = ACCESS_TOKEN
    all_data = []
    while url:
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params if "?" not in url else None, timeout=60)
                if resp.status_code in (429, 613):
                    wait = 30 * (attempt + 1)
                    log(f"  rate limited (HTTP {resp.status_code}), sleeping {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    log(f"  ERROR {resp.status_code}: {resp.text[:300]}")
                    resp.raise_for_status()
                payload = resp.json()
                all_data.extend(payload.get("data", []))
                url = payload.get("paging", {}).get("next")
                params = None
                break
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    raise
                log(f"  retrying ({e})")
                time.sleep(5)
    return all_data


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def extract_results(row):
    actions = row.get("actions") or []
    cost_per = {a["action_type"]: safe_float(a["value"]) for a in (row.get("cost_per_action_type") or [])}
    action_counts = {a["action_type"]: safe_float(a["value"]) for a in actions}

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
                cpr = safe_float(row.get("spend", 0)) / count
            return count, label, cpr

    return 0, "Results", 0


def get_daily_insights(level, since_date, until_date):
    params = {
        "level": level,
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": json.dumps({"since": since_date.isoformat(), "until": until_date.isoformat()}),
        "time_increment": 1,
        "limit": 500,
    }
    return fetch(f"act_{AD_ACCOUNT_ID}/insights", params)


def get_ad_creatives(ad_ids):
    creatives = {}
    for ad_id in ad_ids:
        try:
            resp = requests.get(
                f"{GRAPH_BASE}/{ad_id}",
                params={
                    "fields": "creative{id,name,thumbnail_url,object_type,image_url,video_id}",
                    "access_token": ACCESS_TOKEN
                },
                timeout=30
            )
            if resp.status_code == 200:
                cr = (resp.json() or {}).get("creative") or {}
                creatives[ad_id] = {
                    "thumbnail": cr.get("thumbnail_url") or cr.get("image_url") or "",
                    "type": cr.get("object_type", "UNKNOWN"),
                    "creative_id": cr.get("id", ""),
                }
        except Exception as e:
            log(f"  creative fetch failed for {ad_id}: {e}")
            creatives[ad_id] = {"thumbnail": "", "type": "UNKNOWN", "creative_id": ""}
    return creatives


def main():
    today = datetime.now(SYDNEY_TZ).date()
    yesterday = today - timedelta(days=1)
    start_date = today - timedelta(days=LOOKBACK_DAYS)

    log(f"Pulling {LOOKBACK_DAYS} days of data for account {AD_ACCOUNT_ID}")
    log(f"Date range: {start_date} -> {yesterday}")

    log("Fetching account info...")
    acct_resp = requests.get(
        f"{GRAPH_BASE}/act_{AD_ACCOUNT_ID}",
        params={"fields": "name,currency,timezone_name", "access_token": ACCESS_TOKEN},
        timeout=30
    ).json()
    account_name = acct_resp.get("name", "Alaya Property")
    currency = acct_resp.get("currency", "AUD")
    log(f"  -> {account_name} ({currency})")

    log("Fetching campaigns...")
    campaigns_meta = fetch(f"act_{AD_ACCOUNT_ID}/campaigns", {
        "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget,start_time,stop_time",
        "limit": 200,
    })
    log(f"  -> {len(campaigns_meta)} campaigns")

    campaigns = []
    for c in campaigns_meta:
        if c.get("effective_status") == "DELETED":
            continue
        campaigns.append({
            "id": c["id"],
            "name": c["name"],
            "status": c.get("effective_status", c.get("status", "UNKNOWN")),
            "objective": c.get("objective", ""),
            "daily_budget": safe_float(c.get("daily_budget", 0)) / 100,
            "lifetime_budget": safe_float(c.get("lifetime_budget", 0)) / 100,
            "start_time": c.get("start_time", ""),
            "stop_time": c.get("stop_time", ""),
        })

    log("Fetching ads metadata...")
    ads_meta = fetch(f"act_{AD_ACCOUNT_ID}/ads", {
        "fields": "id,name,status,effective_status,campaign_id,adset_id,created_time",
        "limit": 500,
    })
    ads_meta = [a for a in ads_meta if a.get("effective_status") != "DELETED"]
    log(f"  -> {len(ads_meta)} ads")

    log(f"Fetching creative thumbnails...")
    creatives = get_ad_creatives([a["id"] for a in ads_meta])
    log(f"  -> {len(creatives)} creatives")

    ads = []
    for a in ads_meta:
        cr = creatives.get(a["id"], {})
        ads.append({
            "id": a["id"],
            "name": a["name"],
            "campaign_id": a.get("campaign_id", ""),
            "adset_id": a.get("adset_id", ""),
            "status": a.get("effective_status", a.get("status", "UNKNOWN")),
            "created_time": a.get("created_time", ""),
            "thumbnail": cr.get("thumbnail", ""),
            "creative_type": cr.get("type", "UNKNOWN"),
        })

    log("Fetching account-level daily rows (365 days)...")
    account_daily_raw = get_daily_insights("account", start_date, yesterday)
    account_daily = []
    for r in account_daily_raw:
        results, result_label, cpr = extract_results(r)
        account_daily.append({
            "date": r.get("date_start"),
            "spend": round(safe_float(r.get("spend")), 2),
            "impressions": int(safe_float(r.get("impressions"))),
            "clicks": int(safe_float(r.get("clicks"))),
            "ctr": round(safe_float(r.get("ctr")), 3),
            "cpm": round(safe_float(r.get("cpm")), 2),
            "results": int(results),
            "result_type": result_label,
            "cost_per_result": round(cpr, 2),
        })
    log(f"  -> {len(account_daily)} rows")

    log("Fetching campaign-level daily rows (365 days)...")
    campaign_daily_raw = get_daily_insights("campaign", start_date, yesterday)
    campaign_daily = {}
    for r in campaign_daily_raw:
        cid = r.get("campaign_id")
        if not cid:
            continue
        results, result_label, cpr = extract_results(r)
        row = {
            "date": r.get("date_start"),
            "spend": round(safe_float(r.get("spend")), 2),
            "impressions": int(safe_float(r.get("impressions"))),
            "clicks": int(safe_float(r.get("clicks"))),
            "ctr": round(safe_float(r.get("ctr")), 3),
            "cpm": round(safe_float(r.get("cpm")), 2),
            "results": int(results),
            "result_type": result_label,
            "cost_per_result": round(cpr, 2),
        }
        campaign_daily.setdefault(cid, []).append(row)
    log(f"  -> rows for {len(campaign_daily)} campaigns")

    log("Fetching ad-level daily rows (365 days)...")
    ad_daily_raw = get_daily_insights("ad", start_date, yesterday)
    ad_daily = {}
    for r in ad_daily_raw:
        aid = r.get("ad_id")
        if not aid:
            continue
        results, result_label, cpr = extract_results(r)
        row = {
            "date": r.get("date_start"),
            "spend": round(safe_float(r.get("spend")), 2),
            "impressions": int(safe_float(r.get("impressions"))),
            "clicks": int(safe_float(r.get("clicks"))),
            "ctr": round(safe_float(r.get("ctr")), 3),
            "cpm": round(safe_float(r.get("cpm")), 2),
            "results": int(results),
            "result_type": result_label,
            "cost_per_result": round(cpr, 2),
        }
        ad_daily.setdefault(aid, []).append(row)
    log(f"  -> rows for {len(ad_daily)} ads")

    output = {
        "meta": {
            "last_updated": datetime.now(SYDNEY_TZ).isoformat(),
            "report_for_date": yesterday.strftime("%a %d %b %Y"),
            "currency": currency,
            "account_name": account_name,
            "lookback_days": LOOKBACK_DAYS,
            "is_placeholder": False,
        },
        "campaigns": campaigns,
        "ads": ads,
        "daily_rows": {
            "account": account_daily,
            "campaign": campaign_daily,
            "ad": ad_daily,
        },
    }

    out_path = Path(__file__).parent / "data.json"
    out_path.write_text(json.dumps(output, separators=(",", ":")))
    size_kb = out_path.stat().st_size / 1024
    log(f"Wrote {out_path} ({size_kb:,.1f} KB)")
    log(f"  Campaigns: {len(campaigns)}, Ads: {len(ads)}")
    log("Done.")


if __name__ == "__main__":
    ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
    AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "1276356387745308")
    if not ACCESS_TOKEN:
        print("ERROR: Set META_ACCESS_TOKEN environment variable", file=sys.stderr)
        sys.exit(1)
    main()
