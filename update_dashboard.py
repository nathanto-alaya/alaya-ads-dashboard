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
    "inline_link_clicks", "inline_link_click_ctr",
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


def derive_canonical_event(adset):
    """
    Given an ad set's metadata, return (event_type, human_label).
    Maps optimization_goal + promoted_object.custom_event_str to the canonical
    event Meta Ads Manager uses for the Results column.
    """
    goal = (adset.get("optimization_goal") or "").upper()
    promoted = adset.get("promoted_object") or {}
    custom_event_str = promoted.get("custom_event_str") or ""
    custom_event_type = (promoted.get("custom_event_type") or "").upper()

    # If the ad set has a custom conversion event, that's the canonical result
    if custom_event_str:
        # The event in actions[] will look like: offsite_conversion.custom.<event_str>
        event_type = f"offsite_conversion.custom.{custom_event_str}"
        # Pretty label - replace underscores, title case
        label = custom_event_str.replace("_", " ").title()
        # Special-cased pretty names
        special = {
            "Website Submit Application": "Website Submit Application",
            "Calendly Booking": "Calendly Booking",
            "Lead": "Lead",
        }
        return (event_type, special.get(label, label))

    # Standard pixel events from custom_event_type
    if custom_event_type:
        pixel_map = {
            "LEAD": ("offsite_conversion.fb_pixel_lead", "Leads"),
            "PURCHASE": ("offsite_conversion.fb_pixel_purchase", "Purchases"),
            "COMPLETE_REGISTRATION": ("offsite_conversion.fb_pixel_complete_registration", "Registrations"),
            "ADD_TO_CART": ("offsite_conversion.fb_pixel_add_to_cart", "Adds to Cart"),
            "INITIATE_CHECKOUT": ("offsite_conversion.fb_pixel_initiate_checkout", "Checkouts Initiated"),
            "VIEW_CONTENT": ("offsite_conversion.fb_pixel_view_content", "Content Views"),
        }
        if custom_event_type in pixel_map:
            return pixel_map[custom_event_type]

    # Map optimization_goal to standard events when no promoted object
    goal_map = {
        "LEAD_GENERATION": ("onsite_conversion.lead_grouped", "Leads"),
        "QUALITY_LEAD": ("onsite_conversion.lead_grouped", "Leads"),
        "OFFSITE_CONVERSIONS": ("offsite_conversion.fb_pixel_lead", "Leads"),
        "LINK_CLICKS": ("link_click", "Link Clicks"),
        "LANDING_PAGE_VIEWS": ("landing_page_view", "Landing Page Views"),
        "REACH": ("reach", "Reach"),
        "IMPRESSIONS": ("impressions", "Impressions"),
        "PROFILE_VISIT": ("onsite_conversion.profile_visit", "Profile Visits"),
        "PROFILE_AND_PAGE_ENGAGEMENT": ("page_engagement", "Page Engagement"),
        "POST_ENGAGEMENT": ("post_engagement", "Post Engagement"),
        "VIDEO_VIEWS": ("video_view", "Video Views"),
        "MESSAGES": ("onsite_conversion.messaging_conversation_started_7d", "Conversations Started"),
        "APP_INSTALLS": ("mobile_app_install", "App Installs"),
        "PURCHASE": ("offsite_conversion.fb_pixel_purchase", "Purchases"),
        "VALUE": ("offsite_conversion.fb_pixel_purchase", "Purchases"),
    }
    if goal in goal_map:
        return goal_map[goal]

    return (None, "Results")


def extract_results(row, hint_event=None, hint_label=None):
    """
    Extract result count + cost per result from an insights row.
    The hint is the CANONICAL label we want (e.g. 'Website Submit Application').
    Strategy:
      1. Try the exact hint event - perfect match.
      2. If not found, look for ANY event of the same family (lead-like, purchase-like, etc.)
         based on hint_label, and return its count BUT keep the canonical label.
      3. If the hint type is engagement/clicks, only count those events.
      4. Fall back to broad priority list with no hint.
    Never falls through to Page Engagement when a real conversion goal exists.
    """
    actions = row.get("actions") or []
    cost_per = {a["action_type"]: safe_float(a["value"]) for a in (row.get("cost_per_action_type") or [])}
    action_counts = {a["action_type"]: safe_float(a["value"]) for a in actions}

    # ---- HINT PATH ----
    if hint_event and hint_event in action_counts:
        count = action_counts[hint_event]
        cpr = cost_per.get(hint_event, 0)
        if not cpr and count > 0:
            cpr = safe_float(row.get("spend", 0)) / count
        return count, hint_label or "Results", cpr

    # If hint exists but exact event wasn't in the row, try EQUIVALENT events
    # of the same family. We keep the canonical label either way.
    if hint_event and hint_label:
        label_lower = hint_label.lower()

        # Lead-family fallback events (any lead event counts as canonical leads/applications)
        lead_family = [
            "offsite_conversion.fb_pixel_lead",
            "onsite_conversion.lead_grouped",
            "lead",
            "offsite_conversion.fb_pixel_complete_registration",
            "complete_registration",
        ]
        # Purchase-family
        purchase_family = [
            "offsite_conversion.fb_pixel_purchase",
            "purchase",
            "onsite_web_purchase",
        ]

        # Determine which family this hint belongs to
        is_lead_like = any(k in label_lower for k in [
            "lead", "application", "submit", "registration", "signup", "booking", "enquiry"
        ])
        is_purchase_like = any(k in label_lower for k in ["purchase", "sale", "order"])
        is_traffic_like = any(k in label_lower for k in [
            "click", "visit", "page view", "landing"
        ])
        is_engagement_like = any(k in label_lower for k in [
            "engagement", "video view", "post"
        ])

        # Also include ANY custom conversion event present in the row,
        # in case Meta reports the conversion under a slightly different custom name
        custom_in_row = [k for k in action_counts.keys() if k.startswith("offsite_conversion.custom.")]

        family_events = []
        if is_lead_like:
            family_events = custom_in_row + lead_family
        elif is_purchase_like:
            family_events = custom_in_row + purchase_family
        elif is_traffic_like:
            family_events = ["link_click", "landing_page_view"]
        elif is_engagement_like:
            family_events = ["post_engagement", "page_engagement", "video_view"]
        else:
            family_events = custom_in_row + lead_family

        for ev in family_events:
            if ev in action_counts and action_counts[ev] > 0:
                count = action_counts[ev]
                cpr = cost_per.get(ev, 0)
                if not cpr and count > 0:
                    cpr = safe_float(row.get("spend", 0)) / count
                # Return with the CANONICAL label so the dashboard stays consistent
                return count, hint_label, cpr

        # No conversions of this family on this day - zero with canonical label
        return 0, hint_label, 0

    # ---- NO HINT (fallback for orphan rows) ----
    fallback_priority = [
        ("offsite_conversion.fb_pixel_lead", "Leads"),
        ("offsite_conversion.fb_pixel_purchase", "Purchases"),
        ("offsite_conversion.fb_pixel_complete_registration", "Registrations"),
        ("onsite_conversion.lead_grouped", "Leads"),
        ("lead", "Leads"),
        ("purchase", "Purchases"),
        ("complete_registration", "Registrations"),
    ]
    custom_keys = [k for k in action_counts.keys() if k.startswith("offsite_conversion.custom.")]
    for ck in custom_keys:
        pretty = ck.replace("offsite_conversion.custom.", "").replace("_", " ").title()
        fallback_priority.insert(0, (ck, pretty))

    for event_type, label in fallback_priority:
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

    # Fetch ad sets with optimization_goal + promoted_object so we know each
    # ad set's canonical result type (matches what Ads Manager shows in 'Results' col)
    log("Fetching ad set metadata for result-type mapping...")
    adsets_meta = fetch(f"act_{AD_ACCOUNT_ID}/adsets", {
        "fields": "id,name,campaign_id,optimization_goal,promoted_object,destination_type,billing_event,effective_status",
        "limit": 500,
    })
    adsets_meta = [a for a in adsets_meta if a.get("effective_status") != "DELETED"]
    log(f"  -> {len(adsets_meta)} ad sets")

    # ===== DEBUG: dump ad set config so we can see how Meta defines the canonical event =====
    log("=" * 70)
    log("DEBUG: AD SET CONFIG (optimization_goal + promoted_object per ad set)")
    log("=" * 70)
    campaign_name_lookup_pre = {c["id"]: c["name"] for c in campaigns}
    for ads in adsets_meta:
        if ads.get("effective_status") not in ("ACTIVE", "PAUSED"):
            continue
        cname = campaign_name_lookup_pre.get(ads.get("campaign_id"), "?")
        log(f"")
        log(f"AD SET: {ads.get('name')}  (campaign: {cname})")
        log(f"  optimization_goal: {ads.get('optimization_goal')}")
        log(f"  destination_type: {ads.get('destination_type')}")
        log(f"  billing_event: {ads.get('billing_event')}")
        log(f"  promoted_object: {json.dumps(ads.get('promoted_object'))}")
    log("=" * 70)
    log("END AD SET DUMP")
    log("=" * 70)
    # ===== END DEBUG =====

    # Build adset_id -> (canonical_event_type, label) lookup
    adset_event_lookup = {}
    for ads in adsets_meta:
        adset_event_lookup[ads["id"]] = derive_canonical_event(ads)

    # Build ad_id -> canonical event by going through the ad set
    ad_event_lookup = {}
    for a in ads_meta:
        ad_event_lookup[a["id"]] = adset_event_lookup.get(a.get("adset_id"), (None, None))

    log(f"Fetching creative thumbnails...")
    creatives = get_ad_creatives([a["id"] for a in ads_meta])
    log(f"  -> {len(creatives)} creatives")

    ads = []
    for a in ads_meta:
        cr = creatives.get(a["id"], {})
        canonical_event, canonical_label = ad_event_lookup.get(a["id"], (None, None))
        ads.append({
            "id": a["id"],
            "name": a["name"],
            "campaign_id": a.get("campaign_id", ""),
            "adset_id": a.get("adset_id", ""),
            "status": a.get("effective_status", a.get("status", "UNKNOWN")),
            "created_time": a.get("created_time", ""),
            "thumbnail": cr.get("thumbnail", ""),
            "creative_type": cr.get("type", "UNKNOWN"),
            "canonical_event": canonical_event or "",
            "canonical_label": canonical_label or "",
        })

    # Build campaign_id -> dominant canonical event lookup (for campaign-level rows)
    # Picks the most common label across the campaign's ad sets
    from collections import Counter
    campaign_event_lookup = {}
    for c in campaigns:
        cid = c["id"]
        labels = [adset_event_lookup.get(ads["id"], (None, None))[1]
                  for ads in adsets_meta if ads.get("campaign_id") == cid]
        labels = [l for l in labels if l]
        if labels:
            top_label = Counter(labels).most_common(1)[0][0]
            # Find a matching event type for that label
            for ads in adsets_meta:
                if ads.get("campaign_id") == cid:
                    et, lbl = adset_event_lookup.get(ads["id"], (None, None))
                    if lbl == top_label:
                        campaign_event_lookup[cid] = (et, lbl)
                        break
        c["result_type"] = campaign_event_lookup.get(cid, (None, "Results"))[1]

    # Account-level: pick the most common label across all ad sets (weighted by recency would be ideal, but volume is good enough)
    all_labels = [v[1] for v in adset_event_lookup.values() if v[1]]
    account_dominant_event = (None, "Results")
    if all_labels:
        top_label = Counter(all_labels).most_common(1)[0][0]
        for v in adset_event_lookup.values():
            if v[1] == top_label:
                account_dominant_event = v
                break

    log("Fetching account-level daily rows (365 days)...")
    account_daily_raw = get_daily_insights("account", start_date, yesterday)
    account_daily = []
    acc_hint_event, acc_hint_label = account_dominant_event
    for r in account_daily_raw:
        results, result_label, cpr = extract_results(r, hint_event=acc_hint_event, hint_label=acc_hint_label)
        account_daily.append({
            "date": r.get("date_start"),
            "spend": round(safe_float(r.get("spend")), 2),
            "impressions": int(safe_float(r.get("impressions"))),
            "clicks": int(safe_float(r.get("clicks"))),
            "link_clicks": int(safe_float(r.get("inline_link_clicks"))),
            "ctr": round(safe_float(r.get("ctr")), 3),
            "link_ctr": round(safe_float(r.get("inline_link_click_ctr")), 3),
            "cpm": round(safe_float(r.get("cpm")), 2),
            "results": int(results),
            "result_type": result_label,
            "cost_per_result": round(cpr, 2),
        })
    log(f"  -> {len(account_daily)} rows")

    log("Fetching campaign-level daily rows (365 days)...")
    campaign_daily_raw = get_daily_insights("campaign", start_date, yesterday)

    # ===== DEBUG: dump raw action data so we can see what Meta returns =====
    log("=" * 70)
    log("DEBUG: RAW ACTION DATA PER CAMPAIGN (for fixing result-type detection)")
    log("=" * 70)
    campaign_name_lookup = {c["id"]: c["name"] for c in campaigns}
    seen_campaigns = set()
    for r in campaign_daily_raw:
        cid = r.get("campaign_id")
        if cid in seen_campaigns:
            continue
        # Only dump rows that have actions (i.e. spent + had activity)
        actions = r.get("actions") or []
        if not actions:
            continue
        seen_campaigns.add(cid)
        cname = campaign_name_lookup.get(cid, "?")
        c_hint = campaign_event_lookup.get(cid, (None, None))
        log(f"")
        log(f"CAMPAIGN: {cname}")
        log(f"  Hint event: {c_hint[0]}")
        log(f"  Hint label: {c_hint[1]}")
        log(f"  Date sampled: {r.get('date_start')}, Spend: {r.get('spend')}")
        log(f"  Action types present in this row:")
        for a in actions:
            log(f"    - {a.get('action_type')}: {a.get('value')}")
        # Also dump cost_per_action_type so we see what events Meta charges against
        cpa = r.get("cost_per_action_type") or []
        if cpa:
            log(f"  Cost-per-action types:")
            for a in cpa:
                log(f"    - {a.get('action_type')}: ${a.get('value')}")
    log("")
    log("=" * 70)
    log("END DEBUG DUMP")
    log("=" * 70)
    # ===== END DEBUG =====

    campaign_daily = {}
    for r in campaign_daily_raw:
        cid = r.get("campaign_id")
        if not cid:
            continue
        c_hint_event, c_hint_label = campaign_event_lookup.get(cid, (None, None))
        results, result_label, cpr = extract_results(r, hint_event=c_hint_event, hint_label=c_hint_label)
        row = {
            "date": r.get("date_start"),
            "spend": round(safe_float(r.get("spend")), 2),
            "impressions": int(safe_float(r.get("impressions"))),
            "clicks": int(safe_float(r.get("clicks"))),
            "link_clicks": int(safe_float(r.get("inline_link_clicks"))),
            "ctr": round(safe_float(r.get("ctr")), 3),
            "link_ctr": round(safe_float(r.get("inline_link_click_ctr")), 3),
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
        a_hint_event, a_hint_label = ad_event_lookup.get(aid, (None, None))
        results, result_label, cpr = extract_results(r, hint_event=a_hint_event, hint_label=a_hint_label)
        row = {
            "date": r.get("date_start"),
            "spend": round(safe_float(r.get("spend")), 2),
            "impressions": int(safe_float(r.get("impressions"))),
            "clicks": int(safe_float(r.get("clicks"))),
            "link_clicks": int(safe_float(r.get("inline_link_clicks"))),
            "ctr": round(safe_float(r.get("ctr")), 3),
            "link_ctr": round(safe_float(r.get("inline_link_click_ctr")), 3),
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
