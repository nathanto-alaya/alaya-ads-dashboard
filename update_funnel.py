#!/usr/bin/env python3
"""
Alaya Funnel Review - GoHighLevel side of the join
==================================================
Reads opportunities out of GoHighLevel and joins them to the Meta hierarchy
already in data.json, then writes funnel.json.

The join, verified against live records:
    utm_campaign -> Meta campaign id
    utm_term     -> Meta ad set id      (the ad set IS the funnel)
    utm_content  -> Meta ad id

Two things this script will not do:
  - It will not invent a conversion rate. GoHighLevel returns a current stage
    and no history, so every progression figure here is "leads sitting at or
    past this stage today", and it is labelled that way in the output.
  - It will not report cost per outcome on leads too young to have converted.
    Anything newer than MATURITY_DAYS is counted for volume and cost per lead
    only, and reported separately.

Environment:
    GHL_API_KEY   Private Integration token, read only scopes
Optional:
    GHL_LOCATION_ID  defaults to the Alaya location below
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://services.leadconnectorhq.com"
LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "1LnVcAPBanJSCVTFZgbG")

# The docs disagree with themselves on the Version header for this endpoint, so
# try each until one answers, and log which one worked.
API_VERSIONS = ["2021-07-28", "v3"]

WINDOW_DAYS = 120        # how far back to pull opportunities
MATURITY_DAYS = 14       # a lead younger than this cannot be judged on outcome
SYDNEY = timezone(timedelta(hours=10))

HERE = Path(__file__).parent


def log(msg):
    print(f"[funnel] {msg}", flush=True)


def die(msg):
    print(f"::error::{msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Ghl:
    def __init__(self, token):
        self.token = token
        self.version = None

    def _headers(self, version):
        return {
            "Authorization": f"Bearer {self.token}",
            "Version": version,
            "Accept": "application/json",
        }

    def get(self, path, params=None):
        """
        GET with one-time version negotiation, then backoff on rate limits.
        Fails fast on auth and scope problems: retrying those only burns CI
        minutes and buries the message that actually matters.
        """
        if self.version is None:
            self._negotiate(path, params)
        return self._call(self.version, path, params)

    def _negotiate(self, path, params):
        net_errors = []
        for round_no in range(2):
            for version in API_VERSIONS:
                try:
                    r = requests.get(f"{BASE}{path}", params=params or {},
                                     headers=self._headers(version), timeout=25)
                except requests.RequestException as e:
                    net_errors.append(f"{version}: {type(e).__name__}")
                    continue

                if r.status_code == 200:
                    self.version = version
                    log(f"API version header accepted: {version}")
                    return

                if r.status_code == 401:
                    die("GoHighLevel rejected the token (401). Either it is wrong, "
                        "it was regenerated, or it got copied with a stray space. "
                        "Create a new Private Integration token and update the "
                        "GHL_API_KEY secret.")

                if r.status_code == 403:
                    die("GoHighLevel refused the request (403). The token is valid "
                        "but a scope is missing. This needs at minimum "
                        "opportunities.readonly and contacts.readonly.")

                log(f"  {version} -> HTTP {r.status_code}, trying the next version")

            if round_no == 0 and net_errors:
                log(f"  network trouble ({', '.join(net_errors)}), one retry in 5s")
                time.sleep(5)

        die(f"could not reach {BASE}{path} with any known Version header. "
            f"Network errors: {net_errors or 'none'}. If this is not running on "
            f"GitHub Actions, the network is probably blocking leadconnectorhq.com.")

    def _call(self, version, path, params, retries=3):
        last = None
        for attempt in range(retries):
            try:
                r = requests.get(f"{BASE}{path}", params=params or {},
                                 headers=self._headers(version), timeout=45)
            except requests.RequestException as e:
                last = f"network error: {type(e).__name__}"
                time.sleep([3, 8, 20][min(attempt, 2)])
                continue

            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                die("token rejected mid-run (401). It may have been regenerated.")
            if r.status_code == 403:
                die(f"scope missing for {path} (403), stopping rather than "
                    f"writing a partial report.")
            if r.status_code in (429, 500, 502, 503, 504):
                wait = [3, 8, 20][min(attempt, 2)]
                log(f"  {r.status_code} on {path}, retry in {wait}s")
                time.sleep(wait)
                last = f"HTTP {r.status_code}"
                continue
            last = f"HTTP {r.status_code}: {r.text[:250]}"
            break
        die(f"gave up on {path}. Last: {last}")


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def fetch_pipelines(api):
    data = api.get("/opportunities/pipelines", {"locationId": LOCATION_ID})
    pipes = data.get("pipelines") or []
    stages = {}
    for p in pipes:
        for st in p.get("stages") or []:
            stages[st["id"]] = {
                "stage": st.get("name", ""),
                "position": st.get("position", 0),
                "pipeline": p.get("name", ""),
                "pipeline_id": p.get("id", ""),
            }
    log(f"pipelines: {len(pipes)}, stages: {len(stages)}")
    return pipes, stages


def fetch_opportunities(api, since):
    """Page through the search endpoint. Returns the raw opportunity dicts."""
    out, page, seen_ids = [], 1, set()
    while True:
        data = api.get("/opportunities/search", {
            "location_id": LOCATION_ID,
            "locationId": LOCATION_ID,   # the API has used both spellings
            "date": since.strftime("%Y-%m-%d"),
            "status": "all",
            "limit": 100,
            "page": page,
        })
        batch = data.get("opportunities") or []
        fresh = [o for o in batch if o.get("id") not in seen_ids]
        for o in fresh:
            seen_ids.add(o["id"])
        out.extend(fresh)
        log(f"  page {page}: {len(batch)} rows ({len(out)} total)")
        if len(batch) < 100 or not fresh or page >= 60:
            break
        page += 1
        time.sleep(0.4)
    return out


def fetch_lookup(api, path, params, list_key, id_key="id", name_key="name"):
    """
    Best effort lookup table for the 2 optional scopes. A missing scope costs
    us readable labels, not the run, so this bypasses the strict get().
    """
    try:
        r = requests.get(f"{BASE}{path}", params=params or {},
                         headers=api._headers(api.version or API_VERSIONS[0]),
                         timeout=45)
    except requests.RequestException as e:
        log(f"  {path}: {type(e).__name__}, continuing without it")
        return {}
    if r.status_code != 200:
        log(f"  {path}: HTTP {r.status_code}, continuing without it. "
            f"Ids will show instead of names.")
        return {}
    try:
        rows = r.json().get(list_key) or []
    except ValueError:
        log(f"  {path}: response was not JSON, continuing without it")
        return {}
    return {x.get(id_key): x.get(name_key, "") for x in rows if x.get(id_key)}


# --------------------------------------------------------------------------
# The attribution check, which decides whether any of this is trustworthy
# --------------------------------------------------------------------------

def read_attribution(opp):
    """
    Pull the Meta ids out of an opportunity. Prefers the first touch, falls
    back to the last. Returns (campaign_id, adset_id, ad_id, source, page).
    """
    attrs = opp.get("attributions") or []
    if not attrs:
        return (None, None, None, None, None)
    first = next((a for a in attrs if a.get("isFirst")), attrs[0])
    last = next((a for a in attrs if a.get("isLast")), attrs[-1])
    pick = first if first.get("utmCampaign") else last
    return (
        pick.get("utmCampaign") or None,
        pick.get("utmTerm") or None,
        pick.get("utmContent") or None,
        pick.get("utmSource") or None,
        pick.get("url") or None,
    )


def verify_shape(opps):
    """Fail loudly rather than quietly producing a report built on nothing."""
    if not opps:
        die("the search endpoint returned zero opportunities. Check the location id.")

    with_attr = sum(1 for o in opps if o.get("attributions"))
    pct = with_attr / len(opps) * 100
    log(f"attribution present on {with_attr} of {len(opps)} records ({pct:.0f}%)")

    if with_attr == 0:
        die("no record carries an 'attributions' array, so no lead can be tied to "
            "an ad. Either this endpoint no longer returns attribution, or the "
            "token lacks a scope. Nothing downstream would be meaningful, so "
            "stopping here rather than writing a misleading funnel.json.")

    if pct < 40:
        log(f"::warning::only {pct:.0f}% of records carry attribution. Cost per "
            f"outcome will understate paid performance. Treat the output as "
            f"indicative until the CRM stops creating opportunities by hand "
            f"without a source.")
    return pct


# --------------------------------------------------------------------------
# Join
# --------------------------------------------------------------------------

def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify(stage_info):
    """
    Depth reached, from the stage name. Deliberately crude and readable:
    the stage vocabulary differs per pipeline, so match on words, not ids.
    """
    name = (stage_info or {}).get("stage", "").lower()
    if any(w in name for w in ("closed won", "won closed", "signed", "engage")):
        return "won"
    if "consult" in name and "no show" not in name:
        return "consult"
    if "discovery" in name and "no show" not in name:
        return "discovery"
    if "webinar" in name:
        return "discovery"
    return "lead"

DEPTH_ORDER = {"lead": 0, "discovery": 1, "consult": 2, "won": 3}


# --------------------------------------------------------------------------
# One person, one lead
# --------------------------------------------------------------------------

def dedupe_by_contact(opps, stages):
    """
    GoHighLevel holds one opportunity per record, not one per person, and a lead
    that moves on gets a second opportunity in another pipeline. Counting records
    would count the same person twice AND report them stuck at New Lead, because
    the deeper stage lives on the other record.

    Measured on the live account over 120 days: 168 of 626 records shared a
    contact with at least one other record, and 26 of those pairs straddled the
    Meta Ads pipeline and the Sales pipeline.

    So collapse to one record per contact:
      - stage      the deepest stage reached across the person's records
      - createdAt  the earliest, which is when the lead actually arrived
      - attribution the earliest record that carries a utm_campaign, so a
                   hand-created follow-up opportunity cannot erase the ad
      - value      the largest, never the sum, so one deal is not counted twice
    """
    groups = defaultdict(list)
    for o in opps:
        groups[o.get("contactId") or f"_no_contact_{o.get('id')}"].append(o)

    merged, collapsed = [], 0
    for key, rows in groups.items():
        if len(rows) == 1:
            merged.append(rows[0])
            continue
        collapsed += len(rows) - 1

        def depth_of(o):
            return DEPTH_ORDER[classify(stages.get(o.get("pipelineStageId")))]

        def created_of(o):
            return parse_dt(o.get("createdAt")) or datetime.max.replace(tzinfo=timezone.utc)

        deepest = max(rows, key=lambda o: (depth_of(o), created_of(o)))
        earliest = min(rows, key=created_of)

        by_age = sorted(rows, key=created_of)
        attributed = next((o for o in by_age if read_attribution(o)[0]), None)
        source = attributed or next((o for o in by_age if o.get("attributions")), earliest)

        one = dict(deepest)
        one["createdAt"] = earliest.get("createdAt")
        one["attributions"] = source.get("attributions") or []
        one["monetaryValue"] = max(float(o.get("monetaryValue") or 0) for o in rows)
        one["_merged_from"] = len(rows)
        merged.append(one)

    if collapsed:
        log(f"collapsed {collapsed} duplicate opportunities into their contact, "
            f"{len(opps)} records -> {len(merged)} people")
    return merged


def build(opps, stages, custom_fields, users, meta):
    now = datetime.now(timezone.utc)
    matured_before = now - timedelta(days=MATURITY_DAYS)

    levels = {"campaign": defaultdict(lambda: blank()),
              "adset": defaultdict(lambda: blank()),
              "ad": defaultdict(lambda: blank())}
    coverage = {"total": 0, "with_attr": 0, "with_adset": 0, "with_ad": 0}
    unattributed = blank()

    for o in opps:
        created = parse_dt(o.get("createdAt"))
        if not created:
            continue
        coverage["total"] += 1

        cid, asid, adid, src, page = read_attribution(o)
        if cid:
            coverage["with_attr"] += 1
        if asid:
            coverage["with_adset"] += 1
        if adid:
            coverage["with_ad"] += 1

        depth = classify(stages.get(o.get("pipelineStageId")))
        matured = created <= matured_before
        value = float(o.get("monetaryValue") or 0)

        targets = []
        if cid:
            targets.append(("campaign", cid))
        if asid:
            targets.append(("adset", asid))
        if adid:
            targets.append(("ad", adid))
        if not targets:
            add(unattributed, depth, matured, value)
            continue
        for level, key in targets:
            add(levels[level][key], depth, matured, value)

    out_levels = {}
    for level, buckets in levels.items():
        out_levels[level] = {k: finish(v) for k, v in buckets.items()}

    return {
        "meta": {
            "generated": datetime.now(SYDNEY).isoformat(),
            "window_days": WINDOW_DAYS,
            "maturity_days": MATURITY_DAYS,
            "location_id": LOCATION_ID,
            "join": {"campaign": "utm_campaign", "adset": "utm_term", "ad": "utm_content"},
            "meta_data_generated": (meta or {}).get("last_updated", ""),
            "deduped_by_contact": True,
            "caveats": [
                "GoHighLevel returns a current stage and no history, so every "
                "depth figure is leads sitting at or past that stage today, not "
                "a conversion rate.",
                f"Progression and cost per outcome use only leads older than "
                f"{MATURITY_DAYS} days. Younger leads are counted for volume and "
                f"cost per lead only.",
                "Deal value is blank on almost every record, so no return figure "
                "is produced.",
                "One person counts once. A lead that moved to another pipeline "
                "has a second opportunity record in GoHighLevel; those are "
                "collapsed to the deepest stage that person reached.",
            ],
        },
        "coverage": {
            "records": coverage["total"],
            "with_campaign_pct": pct(coverage["with_attr"], coverage["total"]),
            "with_adset_pct": pct(coverage["with_adset"], coverage["total"]),
            "with_ad_pct": pct(coverage["with_ad"], coverage["total"]),
        },
        "levels": out_levels,
        "unattributed": finish(unattributed),
        "custom_fields": custom_fields,
        "users": users,
    }


def blank():
    return {"leads": 0, "matured": 0,
            "depth": defaultdict(int), "matured_depth": defaultdict(int),
            "value": 0.0, "with_value": 0}


def add(b, depth, matured, value):
    b["leads"] += 1
    d = DEPTH_ORDER[depth]
    for name, order in DEPTH_ORDER.items():
        if order <= d:
            b["depth"][name] += 1
    if matured:
        b["matured"] += 1
        for name, order in DEPTH_ORDER.items():
            if order <= d:
                b["matured_depth"][name] += 1
    if value > 0:
        b["value"] += value
        b["with_value"] += 1


def finish(b):
    return {
        "leads": b["leads"],
        "matured": b["matured"],
        "at_or_past": dict(b["depth"]),
        "matured_at_or_past": dict(b["matured_depth"]),
        "deal_value": round(b["value"], 2),
        "records_with_value": b["with_value"],
    }


def pct(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0


# --------------------------------------------------------------------------
# Snapshot history: the fix for GoHighLevel keeping no stage history
# --------------------------------------------------------------------------

def append_snapshot(funnel):
    path = HERE / "snapshots.json"
    try:
        history = json.loads(path.read_text()) if path.exists() else {"snapshots": []}
    except (ValueError, OSError):
        history = {"snapshots": []}

    today = datetime.now(SYDNEY).strftime("%Y-%m-%d")
    history["snapshots"] = [s for s in history["snapshots"] if s.get("date") != today]
    history["snapshots"].append({
        "date": today,
        "adsets": {k: v["at_or_past"] for k, v in funnel["levels"]["adset"].items()},
    })
    history["snapshots"] = history["snapshots"][-400:]
    path.write_text(json.dumps(history, separators=(",", ":")))
    log(f"snapshots.json now holds {len(history['snapshots'])} days")


# --------------------------------------------------------------------------

def probe(api):
    """Check the token and report what it can see. Writes nothing."""
    log("PROBE MODE, no files will be written")
    _, stages = fetch_pipelines(api)
    since = datetime.now(timezone.utc) - timedelta(days=14)
    data = api.get("/opportunities/search", {
        "location_id": LOCATION_ID, "locationId": LOCATION_ID,
        "date": since.strftime("%Y-%m-%d"), "status": "all", "limit": 20, "page": 1,
    })
    opps = data.get("opportunities") or []
    log(f"opportunities in the last 14 days: {len(opps)}")
    if not opps:
        log("::warning::none returned, check the location id")
        return
    log(f"fields on a record: {sorted(opps[0].keys())}")
    with_attr = [o for o in opps if o.get("attributions")]
    log(f"records carrying attributions: {len(with_attr)} of {len(opps)}")
    if with_attr:
        cid, asid, adid, src, page = read_attribution(with_attr[0])
        log(f"sample join keys -> campaign={cid} adset={asid} ad={adid} source={src}")
        log("the join works, safe to run for real")
    else:
        log("::error::no attribution on any sampled record, the join would be empty")
    cf = fetch_lookup(api, f"/locations/{LOCATION_ID}/customFields", None, "customFields")
    us = fetch_lookup(api, "/users/", {"locationId": LOCATION_ID}, "users")
    log(f"optional scopes: custom field labels {len(cf)}, users {len(us)}")


def main():
    token = os.environ.get("GHL_API_KEY")
    if not token:
        die("GHL_API_KEY is not set. Add it under Settings, Secrets and "
            "variables, Actions.")

    meta = {}
    data_path = HERE / "data.json"
    if data_path.exists():
        try:
            meta = json.loads(data_path.read_text()).get("meta", {})
        except ValueError:
            log("data.json is unreadable, continuing without its stamp")

    api = Ghl(token)
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    if "--probe" in sys.argv:
        probe(api)
        return

    log(f"location {LOCATION_ID}, window from {since:%Y-%m-%d}")
    _, stages = fetch_pipelines(api)

    log("fetching opportunities...")
    opps = fetch_opportunities(api, since)
    log(f"opportunities: {len(opps)}")

    verify_shape(opps)
    opps = dedupe_by_contact(opps, stages)

    custom_fields = fetch_lookup(api, f"/locations/{LOCATION_ID}/customFields",
                                 None, "customFields")
    log(f"custom field labels: {len(custom_fields)}")
    users = fetch_lookup(api, "/users/", {"locationId": LOCATION_ID}, "users")
    log(f"users: {len(users)}")

    funnel = build(opps, stages, custom_fields, users, meta)
    (HERE / "funnel.json").write_text(json.dumps(funnel, separators=(",", ":")))

    c = funnel["coverage"]
    log(f"wrote funnel.json: {c['records']} records, "
        f"campaign {c['with_campaign_pct']}%, ad set {c['with_adset_pct']}%, "
        f"ad {c['with_ad_pct']}%")
    for level in ("campaign", "adset", "ad"):
        log(f"  {level}s with at least one lead: {len(funnel['levels'][level])}")

    append_snapshot(funnel)
    log("done.")


if __name__ == "__main__":
    main()
