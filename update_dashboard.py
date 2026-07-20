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


# Meta error subcodes that mean "you're being rate-limited, back off and retry"
META_RATE_LIMIT_SUBCODES = {
    2446079,  # ad account too many API calls
    1487742,  # rate limit reached
    80004,    # too many calls for app
    4,        # application request limit
    17,       # user request limit
    32,       # page-level rate limit
}

def fetch(endpoint, params=None, retries=4):
    url = f"{GRAPH_BASE}/{endpoint}"
    params = params or {}
    params["access_token"] = ACCESS_TOKEN
    all_data = []
    while url:
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params if "?" not in url else None, timeout=60)

                # Standard HTTP rate-limit codes
                if resp.status_code in (429, 613):
                    wait = 60 * (2 ** attempt)  # 60, 120, 240, 480 seconds
                    log(f"  rate limited (HTTP {resp.status_code}), sleeping {wait}s")
                    time.sleep(wait)
                    continue

                # HTTP 403 with code 4 = application request limit reached (transient).
                # Back off hard - the whole app is throttled, not just this call.
                if resp.status_code == 403:
                    try:
                        body = resp.json()
                        code = body.get("error", {}).get("code")
                        if code == 4 or "request limit" in body.get("error", {}).get("message", "").lower():
                            wait_seconds = [120, 300, 900, 1800][min(attempt, 3)]  # 2m, 5m, 15m, 30m
                            log(f"  app request limit (HTTP 403), sleeping {wait_seconds}s (attempt {attempt + 1}/{retries})")
                            time.sleep(wait_seconds)
                            continue
                    except (ValueError, KeyError):
                        pass

                # HTTP 500 code 1 = "reduce the amount of data you're asking for".
                # This is a size problem, not a rate problem. Retrying the same request
                # won't help, but a brief pause then retry sometimes clears a transient
                # Meta-side hiccup. If it persists, it'll raise after retries exhaust.
                if resp.status_code == 500:
                    try:
                        body = resp.json()
                        msg = body.get("error", {}).get("message", "")
                        if "reduce the amount" in msg.lower():
                            wait = 30 * (attempt + 1)
                            log(f"  Meta 'reduce data' (HTTP 500), sleeping {wait}s (attempt {attempt + 1}/{retries})")
                            time.sleep(wait)
                            continue
                    except (ValueError, KeyError):
                        pass

                # Meta returns 400 with a specific subcode for ad-account rate limits.
                # Detect it from the JSON body before treating as a hard error.
                if resp.status_code == 400:
                    try:
                        body = resp.json()
                        subcode = body.get("error", {}).get("error_subcode")
                        title = body.get("error", {}).get("error_user_title", "")
                        if subcode in META_RATE_LIMIT_SUBCODES or "too many" in title.lower():
                            # Long back-off because account-level limits are sticky.
                            wait_seconds = [60, 300, 900, 1800][min(attempt, 3)]  # 1m, 5m, 15m, 30m
                            log(f"  Meta rate limit ({title or subcode}), sleeping {wait_seconds}s (attempt {attempt + 1}/{retries})")
                            time.sleep(wait_seconds)
                            continue
                    except (ValueError, KeyError):
                        pass  # Not JSON or unexpected shape; fall through to raise

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

    def pretty_label(raw):
        """SUBMIT_APPLICATION -> 'Submit Application'"""
        return raw.replace("_", " ").title()

    # If the ad set has a custom_event_str, that's the canonical result
    if custom_event_str:
        return (f"offsite_conversion.custom.{custom_event_str}", pretty_label(custom_event_str))

    # custom_event_type can be either a standard Meta event OR a custom pixel event name
    # (like SUBMIT_APPLICATION which is custom, not a Meta-defined standard event).
    if custom_event_type:
        # Standard Meta-defined pixel events
        standard_pixel_map = {
            "LEAD": ("offsite_conversion.fb_pixel_lead", "Leads"),
            "PURCHASE": ("offsite_conversion.fb_pixel_purchase", "Purchases"),
            "COMPLETE_REGISTRATION": ("offsite_conversion.fb_pixel_complete_registration", "Registrations"),
            "ADD_TO_CART": ("offsite_conversion.fb_pixel_add_to_cart", "Adds to Cart"),
            "INITIATE_CHECKOUT": ("offsite_conversion.fb_pixel_initiate_checkout", "Checkouts Initiated"),
            "VIEW_CONTENT": ("offsite_conversion.fb_pixel_view_content", "Content Views"),
            "SEARCH": ("offsite_conversion.fb_pixel_search", "Searches"),
            "ADD_TO_WISHLIST": ("offsite_conversion.fb_pixel_add_to_wishlist", "Wishlist Adds"),
            "ADD_PAYMENT_INFO": ("offsite_conversion.fb_pixel_add_payment_info", "Payment Info Added"),
            "SUBSCRIBE": ("offsite_conversion.fb_pixel_subscribe", "Subscriptions"),
            "START_TRIAL": ("offsite_conversion.fb_pixel_start_trial", "Trial Starts"),
            "CONTACT": ("offsite_conversion.fb_pixel_contact", "Contacts"),
            "DONATE": ("offsite_conversion.fb_pixel_donate", "Donations"),
            "FIND_LOCATION": ("offsite_conversion.fb_pixel_find_location", "Locations Found"),
            "SCHEDULE": ("offsite_conversion.fb_pixel_schedule", "Schedules"),
            "SUBMIT_APPLICATION_OFFICIAL_BUT_THIS_NEVER_HAPPENS": None,  # placeholder
        }
        if custom_event_type in standard_pixel_map and standard_pixel_map[custom_event_type]:
            return standard_pixel_map[custom_event_type]

        # Otherwise it's a CUSTOM pixel event (like SUBMIT_APPLICATION).
        # The action_type in insights[] will be one of these patterns - we'll try them in order:
        # - offsite_conversion.fb_pixel_custom.<EVENT_NAME>
        # - offsite_conversion.custom.<EVENT_NAME>
        # extract_results() will fall back to the family if the exact one isn't there.
        return (
            f"offsite_conversion.fb_pixel_custom.{custom_event_type}",
            pretty_label(custom_event_type)
        )

    # Map optimization_goal to standard events when no promoted object
    goal_map = {
        "LEAD_GENERATION": ("onsite_conversion.lead_grouped", "Leads"),
        "QUALITY_LEAD": ("onsite_conversion.lead_grouped", "Leads"),
        "OFFSITE_CONVERSIONS": ("offsite_conversion.fb_pixel_lead", "Leads"),
        "LINK_CLICKS": ("link_click", "Link Clicks"),
        "LANDING_PAGE_VIEWS": ("landing_page_view", "Landing Page Views"),
        "REACH": ("reach", "Reach"),
        "IMPRESSIONS": ("impressions", "Impressions"),
        "PROFILE_VISIT": ("onsite_conversion.profile_visit", "Profile Visit View"),
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
    The hint is the CANONICAL label we want (e.g. 'Submit Application').
    """
    actions = row.get("actions") or []
    cost_per = {a["action_type"]: safe_float(a["value"]) for a in (row.get("cost_per_action_type") or [])}
    action_counts = {a["action_type"]: safe_float(a["value"]) for a in actions}

    # Detect if the hint is a custom pixel event (e.g. SUBMIT_APPLICATION, etc.)
    is_custom_pixel = hint_event and "fb_pixel_custom" in hint_event

    # ---- HINT PATH ----

    # 1) Exact match
    if hint_event and hint_event in action_counts:
        count = action_counts[hint_event]
        cpr = cost_per.get(hint_event, 0)
        if not cpr and count > 0:
            cpr = safe_float(row.get("spend", 0)) / count
        return count, hint_label or "Results", cpr

    # 2) Custom pixel events: Meta groups all custom pixel events into a single
    #    bucket called `offsite_conversion.fb_pixel_custom`. This bucket can include
    #    multiple custom events (e.g. Submit Application + Calendly Booking).
    #    Meta's Ads Manager UI shows ONLY the canonical event count, deduped from
    #    the bucket. To match the UI exactly, we subtract any other custom events
    #    (offsite_conversion.custom.<numeric_id>) from the bucket — these represent
    #    the OTHER custom conversions firing alongside the canonical one.
    if is_custom_pixel:
        if "offsite_conversion.fb_pixel_custom" in action_counts:
            bucket = action_counts["offsite_conversion.fb_pixel_custom"]
            # Sum all `offsite_conversion.custom.<id>` events - these are the secondary
            # custom conversions that get bundled into the bucket. Subtract to get the
            # canonical event count alone.
            other_custom_total = 0
            for at, val in action_counts.items():
                if at.startswith("offsite_conversion.custom.") and val > 0:
                    other_custom_total += val
            count = max(0, bucket - other_custom_total)
            # CPR: prefer the cost_per_action entry tied to this canonical event.
            # Try common keys Meta uses for canonical-event cost reporting.
            cpr = 0
            event_name = hint_event.split(".")[-1].lower() if hint_event else ""
            cpa_keys_to_try = [
                f"offsite_{event_name}_add_meta_leads",  # e.g. offsite_submit_application_add_meta_leads
                "offsite_conversion.fb_pixel_custom",
            ]
            for k in cpa_keys_to_try:
                if k in cost_per and cost_per[k] > 0:
                    cpr = cost_per[k]
                    break
            if not cpr and count > 0:
                cpr = safe_float(row.get("spend", 0)) / count
            return count, hint_label or "Results", cpr
        # Fallback: look for a numeric-id custom event directly
        for at, val in action_counts.items():
            if at.startswith("offsite_conversion.custom.") and val > 0:
                cpr = cost_per.get(at, 0)
                if not cpr and val > 0:
                    cpr = safe_float(row.get("spend", 0)) / val
                return val, hint_label or "Results", cpr

    # 3) Try common variant patterns
    if hint_event:
        event_name = hint_event.split(".")[-1] if "." in hint_event else ""
        if event_name:
            variant_patterns = [
                f"offsite_conversion.fb_pixel_custom.{event_name}",
                f"offsite_conversion.custom.{event_name}",
                f"offsite_conversion.fb_pixel_custom_{event_name.lower()}",
                f"offsite_{event_name.lower()}_add_meta_leads",  # seen in cost_per_action
                event_name.lower(),
            ]
            for variant in variant_patterns:
                if variant in action_counts and action_counts[variant] > 0:
                    count = action_counts[variant]
                    cpr = cost_per.get(variant, 0)
                    if not cpr and count > 0:
                        cpr = safe_float(row.get("spend", 0)) / count
                    return count, hint_label or "Results", cpr

        # Last resort scan: any action type that contains the event name
        if event_name:
            event_name_lower = event_name.lower()
            for at in action_counts.keys():
                if event_name_lower in at.lower() and action_counts[at] > 0:
                    count = action_counts[at]
                    cpr = cost_per.get(at, 0)
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
    """
    Fetch daily insights, chunked into time windows to avoid Meta's
    'reduce the amount of data' (HTTP 500) error on large accounts.

    Ad-level requests are the biggest (many ads x many days), so they use
    the smallest window. Account/campaign levels are small and use larger windows.
    """
    # Window size in days, tuned by level. Smaller = safer against the size limit.
    window_days = {
        "ad": 30,        # ad-level is the heaviest - keep windows tight
        "campaign": 120,
        "account": 365,
    }.get(level, 60)

    all_rows = []
    chunk_start = since_date
    chunk_num = 0
    while chunk_start <= until_date:
        chunk_end = min(chunk_start + timedelta(days=window_days - 1), until_date)
        chunk_num += 1
        params = {
            "level": level,
            "fields": ",".join(INSIGHT_FIELDS),
            "time_range": json.dumps({"since": chunk_start.isoformat(), "until": chunk_end.isoformat()}),
            "time_increment": 1,
            "limit": 500,
            # CRITICAL: This makes Meta return conversion events using the ad set's attribution
            # window — without this, custom conversion events (like SUBMIT_APPLICATION) get
            # filtered out of the actions array.
            "use_unified_attribution_setting": "true",
        }
        rows = fetch(f"act_{AD_ACCOUNT_ID}/insights", params)
        all_rows.extend(rows)
        log(f"    {level} chunk {chunk_num}: {chunk_start} -> {chunk_end} ({len(rows)} rows)")
        # Small pause between chunks to stay under the app-level request rate limit
        if chunk_end < until_date:
            time.sleep(2)
        chunk_start = chunk_end + timedelta(days=1)
    return all_rows


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

    # Build adset_id -> (canonical_event_type, label) lookup
    adset_event_lookup = {}
    for aset in adsets_meta:
        adset_event_lookup[aset["id"]] = derive_canonical_event(aset)

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
        labels = [adset_event_lookup.get(aset["id"], (None, None))[1]
                  for aset in adsets_meta if aset.get("campaign_id") == cid]
        labels = [l for l in labels if l]
        if labels:
            top_label = Counter(labels).most_common(1)[0][0]
            # Find a matching event type for that label
            for aset in adsets_meta:
                if aset.get("campaign_id") == cid:
                    et, lbl = adset_event_lookup.get(aset["id"], (None, None))
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
