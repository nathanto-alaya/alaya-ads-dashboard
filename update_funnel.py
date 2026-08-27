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
from collections import Counter, defaultdict
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
    Where a lead actually got to, from the stage name.

    Two of these calls were confirmed with Nathan rather than guessed, because
    both are large enough to move every rate on the page:

      "Called - No Show" (164 of 529 people, 31%) does NOT mean a booked call
      was missed. It means the setter rang and nobody picked up. So it is not a
      progression at all, it is an unreached lead. The stage NAME reads like a
      missed appointment, which is almost certainly why the sales-side review
      put 28.7% of leads at "discovery scheduled" against the 19% here.

      The "Follow up" family (138 people, 26%) is unresolved. Nobody could say
      whether a conversation happened before a lead was parked there, so they
      are held at lead level and reported separately as unplaceable. Every rate
      on this dashboard is therefore a floor, not an estimate.
    """
    name = (stage_info or {}).get("stage", "").lower()

    if any(w in name for w in ("closed won", "won closed", "signed", "engage")):
        return "won"
    if "consult" in name:                      # booked, held, or no-showed
        return "consult"
    if "discovery completed" in name:
        return "held"
    if "discovery scheduled" in name or "discoeery" in name:
        return "booked"
    if "webinar" in name:                      # webinar booked is a booked slot
        return "booked"
    if "discovery no show" in name:            # a real appointment, missed
        return "booked"
    return "lead"


def is_unreached(stage_info):
    """Rang and nobody answered. Not a stage of the funnel, a failure to start it."""
    name = (stage_info or {}).get("stage", "").lower()
    return "called" in name and ("no show" in name or "no answer" in name)


def is_unplaceable(stage_info):
    """Parked in a Follow up bucket with no record of whether a call happened."""
    name = (stage_info or {}).get("stage", "").lower()
    if is_unreached(stage_info):
        return False
    return "follow" in name or "park" in name


def is_lost(stage_info):
    name = (stage_info or {}).get("stage", "").lower()
    return "lost" in name


DEPTH_ORDER = {"lead": 0, "booked": 1, "held": 2, "consult": 3, "won": 4}


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

    def depth_of(o):
        return DEPTH_ORDER[classify(stages.get(o.get("pipelineStageId")))]

    def created_of(o):
        return parse_dt(o.get("createdAt")) or datetime.max.replace(tzinfo=timezone.utc)

    def annotate(one, rows):
        """
        Carry the facts that only exist across the group.

        Deal count is the number of records that actually reached a won stage,
        not the number of records: three records where one is won is one deal
        that moved pipeline, while three won records is three deals. That is the
        difference between "2 deals" and "1 deal" on the buyers list.
        """
        won = [o for o in rows if depth_of(o) == DEPTH_ORDER["won"]]
        one["_won_count"] = len(won)
        one["_won_value"] = round(sum(float(o.get("monetaryValue") or 0) for o in won), 2)
        # when it was actually signed, not when the record was created
        one["_won_first"] = min(((o.get("lastStageChangeAt")
                                  or o.get("lastStatusChangeAt")
                                  or o.get("createdAt") or "") for o in won),
                                default="")[:10]
        one["_sources"] = sorted({(o.get("source") or "").strip()
                                  for o in rows if (o.get("source") or "").strip()})
        one["_stages"] = sorted({(stages.get(o.get("pipelineStageId")) or {}).get("stage", "")
                                 for o in won} - {""})
        one["_pipelines"] = sorted({(stages.get(o.get("pipelineStageId")) or {}).get("pipeline", "")
                                    for o in rows} - {""})
        return one

    merged, collapsed = [], 0
    for key, rows in groups.items():
        if len(rows) == 1:
            merged.append(annotate(dict(rows[0]), rows))
            continue
        collapsed += len(rows) - 1

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
        merged.append(annotate(one, rows))

    if collapsed:
        log(f"collapsed {collapsed} duplicate opportunities into their contact, "
            f"{len(opps)} records -> {len(merged)} people")
    return merged


# --------------------------------------------------------------------------
# What a funnel actually is
# --------------------------------------------------------------------------
# An ad set is not a funnel and neither is a campaign. A funnel is the page a
# lead lands on and the mechanic that page runs. So group ad sets by the landing
# page their own leads recorded, and read the name off the path. Nothing here is
# invented: the path comes out of the attribution record.

FUNNEL_LOOKUP = {
    "/melbourne/offer":  ("VSL Funnel", "Direct booking",
                          "Video sales letter, application, call booking"),
    "/melbourne/report": ("Apartments Report", "Lead magnet",
                          "Report download, phone follow-up"),
    "/melbourne":        ("Melbourne Landing Page", "Mixed",
                          "Landing page, enquiry form, phone follow-up"),
    "/":                 ("Site Home", "Mixed", "Home page, enquiry form"),
    "_form":             ("Form Direct", "Form only",
                          "Straight into a GoHighLevel form, no landing page"),
}


def landing_path(url):
    """Normalise an attribution url down to the funnel it represents."""
    if not url:
        return None
    u = url.split("?")[0].rstrip("/")
    if "leadconnectorhq.com" in u:
        return "_form"
    for scheme in ("https://", "http://"):
        if u.startswith(scheme):
            u = u[len(scheme):]
    path = "/" + u.split("/", 1)[1] if "/" in u else "/"
    # alayaproperty.com/apply-now/melbourne/offer and
    # lp2.alayaproperty.com/melbourne/offer are the same funnel
    if path.startswith("/apply-now"):
        path = path[len("/apply-now"):] or "/"
    return path or "/"


def first_touch_path(opp):
    attrs = opp.get("attributions") or []
    if not attrs:
        return None
    first = next((a for a in attrs if a.get("isFirst")), attrs[0])
    return (landing_path(first.get("url"))
            or next((landing_path(a.get("url")) for a in attrs if a.get("url")), None))


def name_funnel(path):
    if path in FUNNEL_LOOKUP:
        return FUNNEL_LOOKUP[path]
    label = (path or "unknown").strip("/").replace("/", " ").replace("-", " ").title()
    return (label or "Unknown Page", "Mixed", "Landing page, phone follow-up")


def assign_funnels(opps):
    """
    Decide which funnel each ad set belongs to, by where its own leads landed.
    An ad set goes wholly to its dominant page so that leads and spend can never
    disagree, and the split is reported when it was not unanimous.
    """
    pages = defaultdict(Counter)
    for o in opps:
        _, asid, _, _, _ = read_attribution(o)
        if not asid:
            continue
        p = first_touch_path(o)
        if p:
            pages[asid][p] += 1
        for a in (o.get("attributions") or []):
            p2 = landing_path(a.get("url"))
            if p2:
                pages[asid][p2] += 0.001   # tie-break only

    adset_funnel, funnels = {}, {}
    for asid, counter in pages.items():
        if not counter:
            continue
        path, _ = counter.most_common(1)[0]
        total = sum(counter.values())
        share = counter[path] / total if total else 1.0
        adset_funnel[asid] = path
        name, badge, mechanic = name_funnel(path)
        f = funnels.setdefault(path, {
            "key": path, "name": name, "badge": badge, "mechanic": mechanic,
            "adsets": [], "unanimous": True,
        })
        f["adsets"].append(asid)
        if share < 0.9:
            f["unanimous"] = False
    return adset_funnel, funnels


def contact_name(opp):
    c = opp.get("contact") or {}
    n = (c.get("name") or "").strip()
    if n:
        return n
    parts = [(c.get("firstName") or "").strip(), (c.get("lastName") or "").strip()]
    return " ".join(p for p in parts if p) or "Name not recorded"


def build(opps, stages, custom_fields, users, meta, names=None, raw=None):
    """
    Emit one row per lead, not pre-baked totals.

    The page has to be able to re-cut this by week, month or quarter, and a
    pre-aggregated 120-day total cannot be cut. So the heavy lifting moves to
    the browser and this file stays a list of facts.

    One thing to be honest about: GoHighLevel keeps no stage history, so a
    lead's depth here is where it stands TODAY, not where it stood at the end
    of its own period. That makes every period a cohort read - "of the leads
    created in this period, this many have since reached X" - and the page
    says exactly that on screen.
    """
    now = datetime.now(timezone.utc)
    names = names or {}
    adset_funnel, funnels = assign_funnels(opps)

    # Meta counts form submissions. This dashboard counts people, because one
    # person filling the form twice is one lead, not two. Both are correct and
    # they will never match, so carry the submission count as well and let the
    # page show the two side by side rather than leaving a reader to wonder
    # which number is broken. Measured on Aug 2026: the VSL ad set had 34
    # submissions from 27 people, and Meta reported exactly 34.
    submissions = []
    for o in (raw if raw is not None else opps):
        created = parse_dt(o.get("createdAt"))
        if not created:
            continue
        _, asid, _, _, _ = read_attribution(o)
        if not asid:
            continue
        # Only count records created by the intake itself. When a lead is moved
        # on, a second opportunity is created in the Sales pipeline carrying the
        # same attribution, and counting that would inflate submissions: on the
        # VSL ad set in August it added 3 phantom fills and made the total look
        # like an exact match with Meta when it was not.
        pipeline = ((stages.get(o.get("pipelineStageId")) or {}).get("pipeline") or "").lower()
        if "ads" not in pipeline:
            continue
        submissions.append({"d": created.date().isoformat(),
                            "a": asid,
                            "f": adset_funnel.get(asid)})

    leads = []
    coverage = {"total": 0, "with_attr": 0, "with_adset": 0, "with_ad": 0}
    won_buyers = []

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

        stage_info = stages.get(o.get("pipelineStageId")) or {}
        depth = classify(stage_info)
        value = float(o.get("monetaryValue") or 0)

        # When this person last actually moved. GoHighLevel keeps no stage
        # history, so this is the only movement timestamp there is: it says when
        # the CURRENT stage was reached, and nothing about the stages before it.
        moved = parse_dt(o.get("lastStageChangeAt") or o.get("lastStatusChangeAt"))
        row = {
            "d": created.date().isoformat(),
            "f": adset_funnel.get(asid) if asid else None,
            "a": asid, "c": cid, "ad": adid,
            "x": DEPTH_ORDER[depth],
            "v": round(value, 2),
            "n": names.get(o.get("contactId")) or contact_name(o),
            "s": stage_info.get("stage", ""),
            "p": stage_info.get("pipeline", ""),
            "mv": moved.date().isoformat() if moved else None,
            "st": (o.get("status") or "").lower(),
            "ur": is_unreached(stage_info),     # rang, nobody answered
            "up": is_unplaceable(stage_info),   # parked in Follow up, state unknown
            "lo": is_lost(stage_info),
            "src": (o.get("source") or "").strip() or None,
            "dl": max(o.get("_won_count") or 0, 0),
        }
        if o.get("_merged_from"):
            row["m"] = o["_merged_from"]
        leads.append(row)

        if depth == "won":
            signed = o.get("_won_first") or ""
            days = None
            sd = parse_dt(signed) if signed else None
            if sd:
                days = max((sd.date() - created.date()).days, 0)
            won_buyers.append({
                "name": names.get(o.get("contactId")) or contact_name(o),
                "lead_date": created.date().isoformat(),
                "signed_date": signed,
                "days_to_sign": days,
                "deals": max(o.get("_won_count") or 1, 1),
                "value": o.get("_won_value") or round(value, 2),
                "funnel": adset_funnel.get(asid) if asid else None,
                "adset": asid,
                "sources": o.get("_sources") or [],
                "stages": o.get("_stages") or [],
                "pipelines": o.get("_pipelines") or [],
                "attributed": bool(asid),
            })

    # ---- buyers: already one entry per person, the dedupe did that ----
    buyers = sorted(won_buyers, key=lambda b: (-b["deals"], b["signed_date"]))

    out_funnels = {}
    for k, f in funnels.items():
        out_funnels[k] = {"key": k, "name": f["name"], "badge": f["badge"],
                          "mechanic": f["mechanic"], "adsets": sorted(f["adsets"]),
                          "unanimous": f["unanimous"]}

    return {
        "meta": {
            "generated": datetime.now(SYDNEY).isoformat(),
            "window_days": WINDOW_DAYS,
            "maturity_days": MATURITY_DAYS,
            "location_id": LOCATION_ID,
            "join": {"campaign": "utm_campaign", "adset": "utm_term", "ad": "utm_content"},
            "meta_data_generated": (meta or {}).get("last_updated", ""),
            "deduped_by_contact": True,
            "depth_order": ["lead", "booked", "held", "consult", "won"],
            "caveats": [
                "GoHighLevel keeps no stage history, so every figure below is "
                "where a lead stands today, not a conversion rate measured at "
                "the time. Read each period as a cohort: of the leads created "
                "in this period, this many have since got that far.",
                f"Leads younger than {MATURITY_DAYS} days have not had time to "
                f"convert, so they are counted for volume and cost per lead but "
                f"left out of every progression and cost per outcome figure.",
                "One person counts once. A lead that moved to another pipeline "
                "has a second opportunity record in GoHighLevel; those are "
                "collapsed to the deepest stage that person reached.",
                "A funnel here is the page the leads landed on. Each ad set is "
                "assigned wholly to the page most of its leads reached, so "
                "leads and spend always cover the same thing.",
                "A booked call and a held call are counted separately, because "
                "getting the call booked is what the advertising is responsible "
                "for and holding it is not.",
                "Leads parked in a Follow up stage are held at lead level, "
                "because GoHighLevel does not record whether a call happened "
                "before they were parked. Every rate here is a floor, not an "
                "estimate, and the count of unplaceable leads is shown so the "
                "size of that doubt is visible.",
            ],
        },
        "coverage": {
            "records": coverage["total"],
            "with_campaign_pct": pct(coverage["with_attr"], coverage["total"]),
            "with_adset_pct": pct(coverage["with_adset"], coverage["total"]),
            "with_ad_pct": pct(coverage["with_ad"], coverage["total"]),
        },
        "funnels": out_funnels,
        "adset_funnel": adset_funnel,
        "submissions": submissions,
        "unit_note": ("leads counts people; submissions counts intake records, "
                      "which is the closest thing in the CRM to a form fill. "
                      "Compare Meta's Results column to submissions rather than "
                      "to leads, and expect a small residual gap: Meta counts a "
                      "pixel event on the site, this counts a record that "
                      "reached the CRM, and its 7-day click window can put a "
                      "conversion in a different month."),
        "leads": leads,
        "buyers": buyers,
        "custom_fields": custom_fields,
        "users": users,
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
    per_adset = defaultdict(lambda: defaultdict(int))
    order = funnel["meta"]["depth_order"]
    for r in funnel["leads"]:
        if not r.get("a"):
            continue
        for i, nm in enumerate(order):
            if i <= r["x"]:
                per_adset[r["a"]][nm] += 1
    history["snapshots"].append({
        "date": today,
        "adsets": {k: dict(v) for k, v in per_adset.items()},
    })
    history["snapshots"] = history["snapshots"][-400:]
    path.write_text(json.dumps(history, separators=(",", ":")))
    log(f"snapshots.json now holds {len(history['snapshots'])} days")


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# weekly.json: the Monday review, small enough to be read over the wire
# --------------------------------------------------------------------------
# funnel.json is 60 KB and data.json is over a megabyte. Nothing downstream can
# reliably read either one across the network, so this writes the one week that
# matters, with the spend already joined, in a few kilobytes.

def _sum_spend(daily_adset, adset_ids, start, end):
    t = 0.0
    for aid in adset_ids or []:
        for row in daily_adset.get(aid, []):
            if start <= row.get("date", "") <= end:
                t += float(row.get("spend") or 0)
    return round(t, 2)


def _week_bounds(today, back=0):
    """The Monday-to-Sunday week that finished most recently, then earlier ones."""
    last_sunday = today - timedelta(days=(today.weekday() + 1) % 7 or 7)
    end = last_sunday - timedelta(days=7 * back)
    return (end - timedelta(days=6)).isoformat(), end.isoformat()


def spending_adsets(funnel, data):
    """
    Every ad set that spent anything, whether or not a lead can be traced to it.

    Without this, an ad set that burns money and produces no attributable lead
    never becomes a funnel and so disappears from the page entirely, and the
    funnel spends quietly stop adding up to the total. Found on the live account:
    the calculator ad set spent $73 in August with no attributed lead at all.
    """
    daily = ((data or {}).get("daily_rows") or {}).get("adset") or {}
    names = {a["id"]: a.get("name", a["id"]) for a in (data or {}).get("adsets", [])}
    claimed = {a for f in funnel["funnels"].values() for a in f["adsets"]}
    out = {}
    for aid, rows in daily.items():
        total = sum(float(r.get("spend") or 0) for r in rows)
        if total <= 0:
            continue
        out[aid] = {"name": names.get(aid, aid), "in_a_funnel": aid in claimed}
    return out


def weekly_review(funnel, data):
    daily_adset = ((data or {}).get("daily_rows") or {}).get("adset") or {}
    adset_names = {a["id"]: a.get("name", a["id"]) for a in (data or {}).get("adsets", [])}
    spend_through = ""
    for rows in daily_adset.values():
        for r in rows:
            if r.get("date", "") > spend_through:
                spend_through = r["date"]

    today = datetime.now(SYDNEY).date()
    matured_end = (today - timedelta(days=MATURITY_DAYS)).isoformat()
    order = funnel["meta"]["depth_order"]
    won_i = order.index("won")

    def cut(start, end):
        rows = [r for r in funnel["leads"] if start <= r["d"] <= end]
        mat = [r for r in rows if r["d"] <= matured_end]
        cost_end = min(end, matured_end)

        def one(key):
            defn = funnel["funnels"].get(key) if key else None
            mine = [r for r in rows if (r.get("f") or None) == key]
            mmine = [r for r in mat if (r.get("f") or None) == key]
            at = lambda n: sum(1 for r in mmine if r["x"] >= n)
            ids = defn["adsets"] if defn else []
            spend = _sum_spend(daily_adset, ids, start, end) if defn else None
            spend_cost = _sum_spend(daily_adset, ids, start, cost_end) if defn else None
            signed = sum(b["deals"] for b in funnel["buyers"]
                         if start <= (b.get("signed_date") or "") <= end
                         and (b.get("funnel") or None) == key)
            return {
                "name": defn["name"] if defn else "No attribution recorded",
                "badge": defn["badge"] if defn else "Unknown",
                "leads": len(mine), "matured": len(mmine),
                "booked": at(order.index("booked")),
                "held": at(order.index("held")),
                "consultations": at(order.index("consult")),
                "unreached": sum(1 for r in mmine if r.get("ur")),
                "unplaceable": sum(1 for r in mmine if r.get("up")),
                "signed": signed,
                "spend": spend,
                "cost_per_lead": round(spend / len(mine), 2) if spend and mine else None,
                "cost_per_booked": (round(spend_cost / at(order.index("booked")), 2)
                                    if spend_cost and at(order.index("booked")) else None),
                "cost_per_held": (round(spend_cost / at(order.index("held")), 2)
                                  if spend_cost and at(order.index("held")) else None),
                # below this many matured leads a rate is noise, so say so in the file
                "rate_is_meaningful": len(mmine) >= 10,
            }

        keys = list(funnel["funnels"].keys())
        return {
            "start": start, "end": end,
            "total_spend": _sum_spend(daily_adset, list(daily_adset.keys()), start, end),
            "total_leads": len(rows),
            "total_booked": sum(1 for r in mat if r["x"] >= order.index("booked")),
            "total_held": sum(1 for r in mat if r["x"] >= order.index("held")),
            "total_unreached": sum(1 for r in mat if r.get("ur")),
            "total_unplaceable": sum(1 for r in mat if r.get("up")),
            "deals_signed": sum(b["deals"] for b in funnel["buyers"]
                                if start <= (b.get("signed_date") or "") <= end),
            "funnels": [one(k) for k in keys],
            "unattributed": one(None),
        }

    ws, we = _week_bounds(today, 0)
    ps, pe = _week_bounds(today, 1)
    this_week, last_week = cut(ws, we), cut(ps, pe)

    # The window a rate can honestly be quoted from ENDS at the maturity cutoff,
    # not at the end of the week. A 28-day window ending last Sunday is mostly
    # leads too young to have booked anything, which is how a real-looking
    # percentage gets built out of nothing.
    m_end = datetime.strptime(matured_end, "%Y-%m-%d").date()
    rolling = cut((m_end - timedelta(days=27)).isoformat(), matured_end)
    prev_rolling = cut((m_end - timedelta(days=55)).isoformat(),
                       (m_end - timedelta(days=28)).isoformat())

    # ---------------------------------------------------------------
    # Who moved, and who is stuck
    # ---------------------------------------------------------------
    # Measured over 12 weeks of this account: a lead created in a given week is
    # only 1 to 8 days old when the Monday review runs, and the maturity rule is
    # 14 days, so a weekly COHORT can never carry a conversion rate. Movement
    # can: dated by when a stage was reached, this account produced a median of
    # 6 real progressions a week and 12 to 20 in recent weeks. So the weekly
    # message is built on movement, and rates come off a matured window instead.
    DEEP = order.index("booked")

    def movements(start, end):
        out = []
        for r in funnel["leads"]:
            mv = r.get("mv")
            if not mv or not (start <= mv <= end):
                continue
            if r["x"] < DEEP:
                continue                      # still at a first stage, not news
            out.append({
                "name": r.get("n") or "Name not recorded",
                "stage": r.get("s") or "",
                "reached": order[r["x"]],
                "moved_on": mv,
                "funnel": (funnel["funnels"].get(r["f"]) or {}).get("name") if r.get("f") else None,
                "source": r.get("src"),
                "days_from_lead": (datetime.strptime(mv, "%Y-%m-%d").date()
                                   - datetime.strptime(r["d"], "%Y-%m-%d").date()).days,
                "deals": r.get("dl") or 0,
                "value": r.get("v") or 0,
            })
        rank = {"won": 0, "consult": 1, "held": 2, "booked": 3}
        out.sort(key=lambda m: (rank.get(m["reached"], 9), m["moved_on"]))
        return out[:30]

    def _by_depth(rows):
        c = {"booked": 0, "held": 0, "consult": 0, "won": 0}
        for r in rows:
            if r["reached"] in c:
                c[r["reached"]] += 1
        c["total"] = len(rows)
        return c

    def stalled(as_of, min_days=21, limit=10):
        """
        Open, already past a first stage, and not moved in a while. This is the
        other half of a weekly review: the people the funnel already paid for
        who are sitting still.
        """
        out = []
        for r in funnel["leads"]:
            if r["x"] >= order.index("won"):
                continue
            if r.get("st") in ("lost", "abandoned") or r.get("lo"):
                continue
            if r["x"] < DEEP:
                continue
            base = r.get("mv") or r["d"]
            days = (as_of - datetime.strptime(base, "%Y-%m-%d").date()).days
            if days < min_days:
                continue
            out.append({
                "name": r.get("n") or "Name not recorded",
                "stage": r.get("s") or "",
                "days_in_stage": days,
                "funnel": (funnel["funnels"].get(r["f"]) or {}).get("name") if r.get("f") else None,
            })
        out.sort(key=lambda x: -x["days_in_stage"])
        return {"total": len(out), "aged_60": sum(1 for x in out if x["days_in_stage"] >= 60),
                "worst": out[:limit]}

    buyers = [{"name": b["name"], "deals": b["deals"], "value": b["value"],
               "days_to_sign": b.get("days_to_sign"),
               "funnel": (funnel["funnels"].get(b["funnel"]) or {}).get("name") if b.get("funnel") else None,
               "sources": b.get("sources") or []}
              for b in funnel["buyers"] if ws <= (b.get("signed_date") or "") <= we]

    return {
        "generated": datetime.now(SYDNEY).isoformat(),
        "spend_reported_through": spend_through,
        "maturity_days": MATURITY_DAYS,
        "matured_cutoff": matured_end,
        "note": ("Every rate here is where a lead stands today, not a conversion rate "
                 "measured at the time. Leads newer than the maturity window are counted "
                 "for volume and cost per lead only. rate_is_meaningful is false when "
                 "there are too few matured leads for a percentage to mean anything."),
        "this_week": this_week,
        "last_week": last_week,
        # named to make it obvious these do not end today
        "matured_28_days": rolling,
        "previous_matured_28_days": prev_rolling,
        "moved_this_week": movements(ws, we),
        "moved_last_week_counts": _by_depth(movements(ps, pe)),
        "stalled": stalled(today),
        "buyers_this_week": buyers,
        "coverage": funnel["coverage"],
    }


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

    meta, dash = {}, {}
    data_path = HERE / "data.json"
    if data_path.exists():
        try:
            dash = json.loads(data_path.read_text())
            meta = dash.get("meta", {})
        except ValueError:
            log("data.json is unreadable, continuing without spend")

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
    raw = opps
    opps = dedupe_by_contact(opps, stages)

    custom_fields = fetch_lookup(api, f"/locations/{LOCATION_ID}/customFields",
                                 None, "customFields")
    log(f"custom field labels: {len(custom_fields)}")
    users = fetch_lookup(api, "/users/", {"locationId": LOCATION_ID}, "users")
    log(f"users: {len(users)}")

    funnel = build(opps, stages, custom_fields, users, meta, raw=raw)
    (HERE / "funnel.json").write_text(json.dumps(funnel, separators=(",", ":")))

    c = funnel["coverage"]
    log(f"wrote funnel.json: {c['records']} people, {len(funnel['leads'])} lead rows, "
        f"campaign {c['with_campaign_pct']}%, ad set {c['with_adset_pct']}%, "
        f"ad {c['with_ad_pct']}%")
    for k, f in funnel["funnels"].items():
        n = sum(1 for r in funnel["leads"] if r.get("f") == k)
        log(f"  funnel {f['name']}: {n} leads from {len(f['adsets'])} ad set(s)"
            + ("" if f["unanimous"] else ", leads split across pages"))
    log(f"  buyers: {len(funnel['buyers'])}, "
        f"{sum(1 for b in funnel['buyers'] if b['attributed'])} tied to a funnel")

    try:
        weekly = weekly_review(funnel, dash)
        (HERE / "weekly.json").write_text(json.dumps(weekly, separators=(",", ":")))
        tw, lw = weekly["this_week"], weekly["last_week"]
        rl = weekly["matured_28_days"]
        log(f"wrote weekly.json: week {tw['start']} to {tw['end']}, "
            f"{tw['total_leads']} leads off ${tw['total_spend']:,.0f} "
            f"(previous week {lw['total_leads']} off ${lw['total_spend']:,.0f}); "
            f"matured 28 days {rl['total_leads']} leads, "
            f"{rl['total_appointments']} appointments; "
            f"{len(weekly['moved_this_week'])} people moved, "
            f"{weekly['stalled']['total']} stalled")
        if not dash:
            log("::warning::no data.json, so weekly.json carries no spend figures")
    except Exception as e:
        # the dashboard must not be held hostage by the weekly summary
        log(f"::warning::weekly.json could not be written: {e}")

    append_snapshot(funnel)
    log("done.")


if __name__ == "__main__":
    main()
