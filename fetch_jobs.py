#!/usr/bin/env python3
# Jobs Sentinel - Multi-Subscriber Daily Digest
"""
job-radar: pulls public job feeds (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Workday), keeps matching roles per subscriber profile,
builds a static page at docs/index.html, and emails personalized daily
digests to one or multiple subscribers based on their individual preferences.

  python3 fetch_jobs.py                     normal run (fetch, filter, write page + state, email all active subscribers)
  python3 fetch_jobs.py --subscriber lucas  only process email for subscriber 'lucas'
  python3 fetch_jobs.py --check             test every company board and report problems
  python3 fetch_jobs.py --demo              build out/demo.html from fake data (no network)

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent
UA = "job-radar/1.0 (personal job board)"

CFG_DEFAULTS = {
    "page_title": "Openings",
    "recipient_email": "lucasschollmatter@gmail.com",
    "email_on_baseline": True,
    "email_mode": "new_or_discovered",
    "include_remote_mexico": True,
    "include_country_only": True,
    "new_days": 7,
    "max_workers": 8,
    "state_retention_days": 120,
}

LOC_LABELS = {
    "jalisco": "Target City / Region",
    "remote_mx": "Remote · Mexico",
    "mx_country": "Mexico · city not listed",
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def norm(s):
    """Lower-case and strip accents so 'Tonalá' == 'tonala'."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def today_utc():
    return datetime.now(timezone.utc).date()


def to_date(v):
    """ISO string or epoch-milliseconds -> 'YYYY-MM-DD', or None."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000, timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
        m = re.match(r"\d{4}-\d{2}-\d{2}", str(v))
        return m.group(0) if m else None


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_config(root=ROOT):
    cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
    return {**CFG_DEFAULTS, **cfg}


def load_companies(root=ROOT):
    return json.loads((Path(root) / "companies.json").read_text(encoding="utf-8"))["companies"]


def get_subscribers(cfg, only_id=None):
    """Return list of enabled subscriber configs, merging each with global defaults."""
    raw_subs = cfg.get("subscribers")
    if not raw_subs:
        # Backward-compatible single-subscriber fallback from top-level config
        raw_subs = [{
            "id": "default",
            "name": "Default",
            "recipient_email": cfg.get("recipient_email", "lucasschollmatter@gmail.com"),
            "enabled": True,
        }]
    resolved = []
    for idx, sub in enumerate(raw_subs):
        if not sub.get("enabled", True):
            continue
        sub_id = sub.get("id") or sub.get("recipient_email") or f"sub_{idx + 1}"
        if only_id and sub_id != only_id:
            continue
        merged = {**cfg, **sub}
        merged["id"] = sub_id
        merged["name"] = sub.get("name") or sub_id
        # Allow 'locations' as a friendly alias for 'jalisco_locations' per subscriber
        if "locations" in sub and "jalisco_locations" not in sub:
            merged["jalisco_locations"] = sub["locations"]
        resolved.append(merged)
    return resolved


def collect_workday_search_terms(cfg):
    """Union default Workday search terms with any extra search terms from active subscribers."""
    default_terms = ["Guadalajara", "Zapopan", "Jalisco", "El Salto", "Tonala"]
    seen = {t.lower(): t for t in default_terms}
    for sub in get_subscribers(cfg):
        for term in sub.get("workday_search_terms") or []:
            if term.lower() not in seen:
                seen[term.lower()] = term
    return list(seen.values())


def http_json(url, payload=None, retries=2):
    """GET (or POST when payload is given) and parse JSON. Retries 429/5xx/timeouts."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    err = None
    for attempt in range(retries + 1):
        try:
            req = request.Request(url, data=data, headers=headers)
            with request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            err = e
        except (error.URLError, TimeoutError, json.JSONDecodeError) as e:
            err = e
        time.sleep(2 * (attempt + 1))
    raise err


def job(c, jid, title, location, url, posted=None, team=""):
    return {
        "key": f"{c['ats']}:{c['name']}:{jid}",
        "company": c["name"],
        "ats": c["ats"],
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "team": (team or "").strip(),
        "url": url or "",
        "posted_at": to_date(posted),
    }


# --------------------------------------------------------------------------
# one fetcher per applicant-tracking system
# --------------------------------------------------------------------------

def fetch_greenhouse(c):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs")
    # Only first_published is trusted as a "posted" date: updated_at changes on every edit.
    return [
        job(c, j["id"], j.get("title"), (j.get("location") or {}).get("name"),
            j.get("absolute_url"), j.get("first_published"))
        for j in d.get("jobs") or []
    ]


def fetch_lever(c):
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    d = http_json(f"https://{host}/v0/postings/{c['board']}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        cat = j.get("categories") or {}
        loc = "; ".join(x for x in (cat.get("allLocations") or [cat.get("location")]) if x)
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc += " (Remote)"
        out.append(job(c, j.get("id"), j.get("text"), loc, j.get("hostedUrl"),
                       j.get("createdAt"), cat.get("team") or cat.get("department")))
    return out


def fetch_ashby(c):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['board']}")
    out = []
    for j in d.get("jobs") or []:
        locs = [j.get("location")]
        for s in j.get("secondaryLocations") or []:
            locs.append(s.get("location") if isinstance(s, dict) else s)
        a = (j.get("address") or {}).get("postalAddress") or {}
        locs.append(", ".join(x for x in [a.get("addressLocality"), a.get("addressRegion"),
                                          a.get("addressCountry")] if x))
        loc = "; ".join(dict.fromkeys(x for x in locs if x))
        if j.get("isRemote") and "remote" not in loc.lower():
            loc += " (Remote)"
        out.append(job(c, j.get("id"), j.get("title"), loc, j.get("jobUrl") or j.get("applyUrl"),
                       j.get("publishedAt"), j.get("department") or j.get("team")))
    return out


def fetch_workable(c):
    d = http_json(f"https://apply.workable.com/api/v1/widget/accounts/{c['board']}?details=true")
    out = []
    for j in d.get("jobs") or []:
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc += " (Remote)"
        out.append(job(c, j.get("shortcode") or j.get("code"), j.get("title"), loc,
                       j.get("url") or j.get("shortlink"),
                       j.get("published_on") or j.get("created_at"), j.get("department")))
    return out


def fetch_smartrecruiters(c):
    out, offset = [], 0
    while offset < 1000:
        d = http_json("https://api.smartrecruiters.com/v1/companies/"
                      f"{c['board']}/postings?limit=100&offset={offset}&country=mx")
        items = d.get("content") or []
        for j in items:
            l = j.get("location") or {}
            country = (l.get("country") or "").lower()
            loc = ", ".join(x for x in [l.get("city"), l.get("region"),
                                        "Mexico" if country == "mx" else country.upper()] if x)
            if l.get("remote"):
                loc += " (Remote)"
            out.append(job(c, j["id"], j.get("name"), loc,
                           f"https://jobs.smartrecruiters.com/{c['board']}/{j['id']}",
                           j.get("releasedDate"), (j.get("department") or {}).get("label")))
        offset += 100
        if not items or offset >= (d.get("totalFound") or 0):
            break
    return out


def workday_posted(text, today):
    """'Posted 3 Days Ago' -> date. '30+ Days Ago' is too vague, so None."""
    s = norm(text)
    if "today" in s:
        return today.isoformat()
    if "yesterday" in s:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)(\+?)\s*days?", s)
    if m and not m.group(2):
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return None


def fetch_workday(c, today=None, extra_terms=None):
    host = f"{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    api = f"https://{host}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    today, found = today or today_utc(), {}
    default_terms = extra_terms or ["Guadalajara", "Zapopan", "Jalisco", "El Salto", "Tonala"]
    for term in c.get("search_terms", default_terms):
        offset, total = 0, None
        while offset < 600:
            d = http_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                                "searchText": term})
            posts = d.get("jobPostings") or []
            if total is None:  # Workday only reports a reliable total on the first page
                total = d.get("total") or 0
            for p in posts:
                loc = p.get("locationsText") or ""
                if re.fullmatch(r"\d+ locations?", loc.strip(), re.I):
                    loc = f"{loc} (matched '{term}')"
                path = p.get("externalPath", "")
                ref = (p.get("bulletFields") or [""])[0] or path
                found.setdefault(ref, job(
                    c, ref, p.get("title"), loc, f"https://{host}/en-US/{c['site']}{path}",
                    workday_posted(p.get("postedOn") or "", today)))
            offset += 20
            if not posts or offset >= total:
                break
    return list(found.values())


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def fetch_one(c, today=None, extra_terms=None):
    try:
        fn = FETCHERS.get(c.get("ats"))
        if fn is None:
            return c, None, f"unknown ats '{c.get('ats')}'"
        if fn is fetch_workday:
            return c, fn(c, today=today, extra_terms=extra_terms), None
        return c, fn(c), None
    except KeyError as e:
        return c, None, f"missing field {e} in companies.json"
    except error.HTTPError as e:
        hint = " (board not found: check the token)" if e.code == 404 else ""
        return c, None, f"HTTP {e.code}{hint}"
    except Exception as e:  # network down, bad JSON, anything else: report, don't crash
        return c, None, f"{type(e).__name__}: {e}"


def fetch_all(companies, cfg, today=None):
    extra_terms = collect_workday_search_terms(cfg)
    with ThreadPoolExecutor(max_workers=int(cfg["max_workers"])) as ex:
        return list(ex.map(lambda c: fetch_one(c, today=today, extra_terms=extra_terms), companies))


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------

def build_rules(cfg):
    locs = cfg.get("locations") or cfg.get("jalisco_locations") or []
    jal = "|".join(re.escape(norm(x)) for x in locs)
    return {
        "inc": [re.compile(p, re.I) for p in cfg["title_include"]],
        "exc": [re.compile(p, re.I) for p in cfg["title_exclude"]],
        "jal": re.compile(rf"\b({jal})\b") if jal else re.compile(r"(?!x)x"),
    }


def is_director(title, rules):
    t = norm(title)
    return any(r.search(t) for r in rules["inc"]) and not any(r.search(t) for r in rules["exc"])


def location_class(loc, rules, cfg):
    """'jalisco' | 'remote_mx' | 'mx_country' | None"""
    t = norm(loc)
    if rules["jal"].search(t):
        return "jalisco"
    remote = re.search(r"\b(remote|remoto|remota|home office|teletrabajo)\b", t)
    mexico = re.search(r"\b(mexico|mx)\b", t)
    if cfg["include_remote_mexico"] and remote and mexico:
        return "remote_mx"
    if cfg["include_country_only"] and any(
            re.fullmatch(r"\W*(mexico|mx)\W*", seg) for seg in re.split(r"[;|]", t)):
        return "mx_country"
    return None


def select(jobs, rules, cfg):
    out = []
    for j in jobs:
        if not is_director(j["title"], rules):
            continue
        lc = location_class(j["location"], rules, cfg)
        if lc:
            out.append({**j, "loc_class": lc})
    return out


# --------------------------------------------------------------------------
# state: what have we seen before? (Global + Per-Subscriber)
# --------------------------------------------------------------------------

def update_state(jobs, state, today, retention_days):
    """Stamp first_seen on each job in the given state dict. The very first run
    for a state dict is a 'baseline': nothing found then counts as newly discovered."""
    seen = state.setdefault("jobs", {})
    baseline = not state.get("initialized")
    for j in jobs:
        rec = seen.get(j["key"])
        j["discovered_now"] = rec is None and not baseline
        if rec is None:
            rec = seen[j["key"]] = {"first_seen": today.isoformat(), "baseline": baseline}
        rec["last_seen"] = today.isoformat()
        j["first_seen"], j["baseline"] = rec["first_seen"], rec["baseline"]
    for k in list(seen):
        last = date.fromisoformat(seen[k].get("last_seen") or seen[k]["first_seen"])
        if (today - last).days > retention_days:
            del seen[k]
    state["initialized"] = True
    return baseline


def flag_new(j, today, new_days):
    cutoff = today - timedelta(days=new_days)
    if j.get("posted_at"):
        return date.fromisoformat(j["posted_at"]) >= cutoff
    return (not j.get("baseline")) and date.fromisoformat(j["first_seen"]) >= cutoff


# --------------------------------------------------------------------------
# output & daily email notifications
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>__TITLE__</title>
<style>
:root{--bg:#fafaf9;--card:#fff;--ink:#1c1917;--mute:#78716c;--line:#e7e5e4;--accent:#0f766e;--new:#b45309;--newbg:#fef3c7}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1d1b;--ink:#f5f5f4;--mute:#a8a29e;--line:#2e2c29;--accent:#5eead4;--new:#fcd34d;--newbg:#3b2f0b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:1.35rem;margin:0 0 2px}
.sub{color:var(--mute);font-size:.875rem;margin:0 0 16px}
.controls{display:grid;gap:8px;grid-template-columns:1fr;margin-bottom:12px}
@media (min-width:640px){.controls{grid-template-columns:2fr 1fr}}
input[type=search],select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font:inherit}
.toggles{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 14px;font-size:.9rem}
.toggles label{display:flex;gap:6px;align-items:center}
.count{color:var(--mute);font-size:.85rem;margin:0 0 8px}
.job{display:block;padding:14px 16px;margin-bottom:8px;background:var(--card);border:1px solid var(--line);border-radius:12px;color:inherit;text-decoration:none}
.job:hover,.job:focus-visible{border-color:var(--accent);outline:none}
.t{font-weight:600}
.m{color:var(--mute);font-size:.875rem;margin-top:2px}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.b{font-size:.75rem;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mute)}
.b.new{background:var(--newbg);color:var(--new);border-color:transparent;font-weight:600}
.empty{color:var(--mute);padding:32px 0;text-align:center}
</style>
</head>
<body>
<main>
<h1>__TITLE__</h1>
<p class="sub">Updated __UPDATED__</p>
<div class="controls">
<input id="q" type="search" placeholder="Search title, company, team" aria-label="Search">
<select id="co" aria-label="Company"><option value="">All companies</option></select>
</div>
<div class="toggles">
<label><input id="onlyNew" type="checkbox"> New only</label>
<label><input id="onlyJal" type="checkbox"> Jalisco only</label>
</div>
<p class="count" id="count"></p>
<div id="list"></div>
</main>
<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
const LOC = {jalisco: "Jalisco", remote_mx: "Remote \u00b7 Mexico", mx_country: "Mexico \u00b7 city not listed"};
const DAY = 86400000;
function daysAgo(s) {
  const d = new Date(s + "T00:00:00"), t = new Date();
  t.setHours(0, 0, 0, 0);
  return Math.max(0, Math.round((t - d) / DAY));
}
function when(n) { return n === 0 ? "today" : n === 1 ? "yesterday" : n + " days ago"; }
function safeUrl(u) { return /^https?:\/\//i.test(u) ? u : "#"; }
const co = $("co");
[...new Set(DATA.map(j => j.company))].sort((a, b) => a.localeCompare(b)).forEach(name => {
  const o = document.createElement("option");
  o.value = name; o.textContent = name; co.appendChild(o);
});
function render() {
  const q = $("q").value.trim().toLowerCase();
  const rows = DATA.filter(j =>
    (!co.value || j.company === co.value) &&
    (!$("onlyNew").checked || j.is_new) &&
    (!$("onlyJal").checked || j.loc_class === "jalisco") &&
    (!q || (j.title + " " + j.company + " " + j.team + " " + j.location).toLowerCase().includes(q)));
  $("count").textContent = rows.length + " of " + DATA.length + " roles";
  const list = $("list");
  list.replaceChildren();
  if (!rows.length) {
    const p = document.createElement("p");
    p.className = "empty"; p.textContent = "Nothing matches."; list.appendChild(p);
    return;
  }
  rows.forEach(j => {
    const a = document.createElement("a");
    a.className = "job"; a.href = safeUrl(j.url); a.target = "_blank"; a.rel = "noopener noreferrer";
    const t = document.createElement("div"); t.className = "t"; t.textContent = j.title;
    const m = document.createElement("div"); m.className = "m";
    m.textContent = [j.company, j.location, j.team].filter(Boolean).join(" \u00b7 ");
    const b = document.createElement("div"); b.className = "badges";
    const add = (txt, cls) => {
      const s = document.createElement("span");
      s.className = "b" + (cls ? " " + cls : ""); s.textContent = txt; b.appendChild(s);
    };
    if (j.is_new) add("NEW", "new");
    add(LOC[j.loc_class] || j.loc_class);
    add((j.posted_at ? "Posted " : "First seen ") + when(daysAgo(j.posted_at || j.first_seen)));
    if (j.stale) add("last refresh failed");
    a.append(t, m, b);
    list.appendChild(a);
  });
}
["input", "change"].forEach(ev => ["q", "co", "onlyNew", "onlyJal"].forEach(id => $(id).addEventListener(ev, render)));
render();
</script>
</body>
</html>
"""


def render_page(jobs, cfg, updated):
    data = json.dumps(jobs, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    title = html.escape(cfg["page_title"])
    return (PAGE.replace("__TITLE__", title)
                .replace("__UPDATED__", html.escape(updated))
                .replace("__DATA__", data))


def alert_markdown(new_jobs, today):
    lines = [f"**{len(new_jobs)} new opening(s)**, first seen {today.isoformat()}", ""]
    for j in new_jobs:
        where = f" · {j['location']}" if j["location"] else ""
        lines.append(f"- **{j['title']}** · {j['company']}{where} · [open]({j['url']})")
    return "\n".join(lines) + "\n"


def select_jobs_for_email(jobs, baseline, cfg):
    """Choose which jobs to include in a subscriber's daily email digest."""
    mode = cfg.get("email_mode", "new_or_discovered")
    if mode == "all":
        return [j for j in jobs if not j.get("stale")]
    if baseline:
        if cfg.get("email_on_baseline", True):
            return [j for j in jobs if not j.get("stale")]
        return []
    if mode == "discovered_only":
        return [j for j in jobs if j.get("discovered_now")]
    return [j for j in jobs if j.get("discovered_now")]


def job_age_info(j, today):
    """Return (days_old, is_hot, human_label) for a job relative to `today`.
    A role is considered 🔥 HOT when posted (or first spotted) within 0–2 days."""
    ref_str = j.get("posted_at") or j.get("first_seen") or today.isoformat()
    try:
        ref_date = date.fromisoformat(ref_str)
        days = max(0, (today - ref_date).days)
    except ValueError:
        days = 0

    has_posted = bool(j.get("posted_at"))
    verb = "Posted" if has_posted else "Spotted"
    if days == 0:
        label = f"{verb} today"
    elif days == 1:
        label = f"{verb} 1 day ago"
    else:
        label = f"{verb} {days} days ago"

    is_hot = days <= 2
    return days, is_hot, label


def _render_job_card_html(j, today):
    """Render a single job card with 🔥 HOT styling for 0-2 day old roles."""
    days, is_hot, age_label = job_age_info(j, today)
    loc_tag = html.escape(LOC_LABELS.get(j.get("loc_class"), j.get("loc_class", "")))
    title_esc = html.escape(j.get("title", "Untitled Role"))
    company_esc = html.escape(j.get("company", ""))
    loc_esc = html.escape(j.get("location") or loc_tag)
    team_esc = html.escape(j.get("team", ""))
    url_esc = html.escape(j.get("url", "#"))
    age_esc = html.escape(age_label)

    meta_parts = [f"<strong>{company_esc}</strong>", loc_esc]
    if team_esc:
        meta_parts.append(team_esc)
    meta_html = " &middot; ".join(meta_parts)

    if is_hot:
        card_bg = "#fffaf5"
        card_border = "#fed7aa"
        left_border = "#f97316"
        badge_html = (
            f"<span style='display:inline-block;background:#ffedd5;color:#c2410c;"
            f"font-size:11px;font-weight:800;padding:3px 9px;border-radius:999px;"
            f"letter-spacing:0.3px;margin-right:6px;'>&#128293; HOT &middot; {age_esc.upper()}</span>"
            f"<span style='display:inline-block;background:#fef3c7;color:#92400e;"
            f"font-size:11px;font-weight:700;padding:3px 8px;border-radius:999px;"
            f"margin-right:6px;'>&#128142; Lucy&rsquo;s Top Pick</span>"
        )
        btn_bg = "#ea580c"
        btn_text = "Claim This Role &amp; Apply &#128640;"
    else:
        card_bg = "#ffffff"
        card_border = "#e7e5e4"
        left_border = "#0f766e"
        status_pill = (
            "<span style='display:inline-block;background:#ccfbf1;color:#0f766e;"
            "font-size:11px;font-weight:700;padding:3px 8px;border-radius:999px;"
            "margin-right:6px;'>&#10024; HANDPICKED</span>"
            if j.get("is_new")
            else ""
        )
        badge_html = (
            f"{status_pill}"
            f"<span style='display:inline-block;background:#f5f5f4;color:#57534e;"
            f"font-size:11px;font-weight:600;padding:3px 8px;border-radius:999px;"
            f"margin-right:6px;'>&#128197; {age_esc}</span>"
        )
        btn_bg = "#0f766e"
        btn_text = "View Role &amp; Apply &rarr;"

    loc_pill = (
        f"<span style='display:inline-block;background:#f5f5f4;color:#78716c;"
        f"font-size:11px;padding:3px 8px;border-radius:999px;'>&#128205; {loc_tag}</span>"
    )

    return f"""
    <div style="background:{card_bg};border:1px solid {card_border};border-left:5px solid {left_border};border-radius:12px;padding:16px 18px;margin-bottom:12px;">
      <div style="margin-bottom:8px;">
        {badge_html}{loc_pill}
      </div>
      <div style="font-size:17px;font-weight:700;color:#1c1917;line-height:1.35;margin-bottom:4px;">
        <a href="{url_esc}" style="color:#1c1917;text-decoration:none;">{title_esc}</a>
      </div>
      <div style="font-size:14px;color:#57534e;margin-bottom:14px;">
        {meta_html}
      </div>
      <div>
        <a href="{url_esc}" style="display:inline-block;background:{btn_bg};color:#ffffff;font-size:13px;font-weight:700;padding:9px 16px;border-radius:8px;text-decoration:none;">
          {btn_text}
        </a>
      </div>
    </div>"""


def build_email_message(jobs_to_send, all_jobs_count, sub_cfg, today, sender_email):
    """Construct a fun, VIP 'Lucy — Your Personal Headhunter' multipart email."""
    recipient = sub_cfg.get("recipient_email", "lucasschollmatter@gmail.com")
    sub_name = sub_cfg.get("name") or recipient
    date_str = today.isoformat()

    # Enrich and sort so 🔥 HOT roles (0-2 days old) appear first
    enriched = []
    for j in jobs_to_send:
        days, is_hot, age_label = job_age_info(j, today)
        enriched.append((not is_hot, days, j, is_hot, age_label))
    enriched.sort(key=lambda item: (item[0], item[1], item[2].get("company", "").lower()))

    hot_jobs = [item[2] for item in enriched if item[3]]
    other_jobs = [item[2] for item in enriched if not item[3]]

    msg = EmailMessage()
    hot_prefix = f"🔥 {len(hot_jobs)} HOT! " if hot_jobs else "🕵️‍♀️✨ "
    msg["Subject"] = (
        f"{hot_prefix}Lucy (Your Personal Headhunter): {len(jobs_to_send)} Relevant Opening(s) handpicked for {sub_name} ({date_str})"
    )
    msg["From"] = f"Lucy · Your Personal Headhunter <{sender_email}>"
    msg["To"] = recipient

    # Plain-text version (keeps '- {title} at {company}' format for compatibility)
    txt_lines = [
        f"Hey {sub_name}! 👋✨",
        "",
        "It's Lucy — your personal headhunter who ACTUALLY works for you (no commission, no ghosting, just pure hustle!).",
        "While you were doing your thing, I put on my detective hat, dug through a mountain of noisy career boards, and handpicked the coolest opportunities that match YOUR exact profile.",
        "I'm your daily reminder to check where to apply today — and honestly, you're a total catch, so these companies are lucky to have you!",
        "",
        f"Lucy's VIP Shortlist — {date_str}",
        f"🔥 {len(hot_jobs)} HOT role(s) posted in the last 1-2 days | 💎 {len(jobs_to_send)} role(s) handpicked today ({all_jobs_count} total active matches on your radar):",
        "",
    ]
    for _, _, j, is_hot, age_label in enriched:
        loc_tag = LOC_LABELS.get(j.get("loc_class"), j.get("loc_class", ""))
        hot_tag = "[🔥 HOT] " if is_hot else "[NEW] " if j.get("is_new") else ""
        txt_lines.append(f"- {hot_tag}{j['title']} at {j['company']}")
        txt_lines.append(f"  Location: {j.get('location') or loc_tag} ({loc_tag}) | {age_label} ({j.get('posted_at') or j.get('first_seen', date_str)})")
        txt_lines.append(f"  Apply: {j.get('url')}")
        txt_lines.append("")
    txt_lines.extend([
        f"Headhunter's orders, {sub_name}: I already did the heavy lifting and filtered out all the junk. Now pour yourself a coffee, pick 1 or 2 roles above, and hit apply!",
        "",
        "Cheering for you always,",
        "Lucy 🕵️‍♀️💼",
        "Your Personal Headhunter (who works exclusively for you!)",
    ])
    msg.set_content("\n".join(txt_lines))

    # HTML sections
    sections_html = []
    if hot_jobs:
        hot_cards = "".join(_render_job_card_html(j, today) for j in hot_jobs)
        sections_html.append(f"""
        <div style="margin-bottom:24px;">
          <div style="font-size:16px;font-weight:800;color:#c2410c;margin-bottom:4px;">
            &#128293; Hot Off the Press (Posted in the Last 1&ndash;2 Days)
          </div>
          <div style="font-size:13px;color:#78716c;margin-bottom:12px;">
            I spotted these right as they went live &mdash; apply now so your CV lands right at the top of the stack!
          </div>
          {hot_cards}
        </div>""")

    if other_jobs:
        other_heading = (
            "&#128142; More Handpicked Gems from My Search"
            if hot_jobs
            else "&#128142; Today&rsquo;s Handpicked Shortlist Just for You"
        )
        other_cards = "".join(_render_job_card_html(j, today) for j in other_jobs)
        sections_html.append(f"""
        <div style="margin-bottom:16px;">
          <div style="font-size:16px;font-weight:800;color:#0f766e;margin-bottom:10px;">
            {other_heading}
          </div>
          {other_cards}
        </div>""")

    sub_name_esc = html.escape(sub_name)
    html_body = f"""<!doctype html>
<html>
<body style="margin:0;padding:20px 12px;font-family:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1c1917;background:#f5f5f4;">
  <div style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e7e5e4;border-radius:16px;overflow:hidden;">
    <div style="background:#0f172a;background:linear-gradient(135deg,#0f172a 0%,#134e4a 55%,#0f766e 100%);color:#ffffff;padding:28px 24px;">
      <div style="display:inline-block;background:rgba(255,255,255,0.16);color:#5eead4;font-size:11px;font-weight:800;padding:4px 11px;border-radius:999px;letter-spacing:0.6px;margin-bottom:12px;">
        &#128373;&#65039;&zwj;&#9792;&#65039; LUCY &middot; YOUR PERSONAL HEADHUNTER &middot; VIP CLIENT DOSSIER
      </div>
      <h1 style="margin:0 0 10px;font-size:22px;line-height:1.3;color:#ffffff;">
        Hey {sub_name_esc}! &#128075;&#10024; I worked hard digging through the boards today &mdash; and I found some seriously cool opportunities for you.
      </h1>
      <p style="margin:0 0 12px;font-size:14px;line-height:1.55;color:#e2e8f0;">
        It&rsquo;s <strong>Lucy</strong>, the personal headhunter that <em>actually works for you</em> (zero commission, zero fluff, 100% on your team). I&rsquo;m your daily reminder to check where to apply today!
      </p>
      <p style="margin:0 0 18px;font-size:14px;line-height:1.55;color:#cbd5e1;">
        I just combed through dozens of career pages, tossed out all the irrelevant noise, and curated this VIP shortlist tailored to your exact background. Anything posted in the last 1&ndash;2 days is badged <strong>&#128293; HOT</strong> so you can beat the crowd!
      </p>
      <div>
        <span style="display:inline-block;background:#ea580c;color:#ffffff;font-size:12px;font-weight:700;padding:5px 12px;border-radius:999px;margin-right:6px;margin-bottom:4px;">
          &#128293; {len(hot_jobs)} Hot (1&ndash;2d)
        </span>
        <span style="display:inline-block;background:rgba(255,255,255,0.18);color:#ffffff;font-size:12px;font-weight:600;padding:5px 12px;border-radius:999px;margin-right:6px;margin-bottom:4px;">
          &#128142; {len(jobs_to_send)} Handpicked Today
        </span>
        <span style="display:inline-block;background:rgba(255,255,255,0.12);color:#cbd5e1;font-size:12px;padding:5px 12px;border-radius:999px;margin-bottom:4px;">
          &#128225; {all_jobs_count} Total Active Matches
        </span>
      </div>
    </div>

    <div style="padding:24px;">
      {''.join(sections_html)}
      <div style="margin-top:22px;padding:18px;background:#fafaf9;border:1px dashed #d6d3d1;border-radius:12px;font-size:13px;line-height:1.5;color:#44403c;">
        <div style="font-weight:800;font-size:14px;color:#0f766e;margin-bottom:4px;">
          &#128140; A quick note from Lucy, your personal headhunter:
        </div>
        I already did the heavy lifting and filtered out all the junk so you don&rsquo;t have to scroll for hours. Pour yourself a coffee &#9749;, pick 1 or 2 roles above, and send that application today, <strong>{sub_name_esc}</strong> &mdash; these companies would be lucky to have you! &#128170;&#128640;
      </div>
    </div>
  </div>
</body>
</html>"""
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_email_digest(jobs_to_send, all_jobs_count, sub_cfg, today):
    """Send a single subscriber's daily digest via Gmail/SMTP if credentials are configured."""
    sub_name = sub_cfg.get("name") or sub_cfg.get("recipient_email", "subscriber")
    if not jobs_to_send:
        print(f"  [{sub_name}] No new roles to email today.")
        return False

    smtp_user = os.environ.get("GMAIL_USER") or os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

    if not smtp_user or not smtp_password:
        print(f"  [{sub_name}] Email skipped: GMAIL_USER / GMAIL_APP_PASSWORD environment variables not set.")
        return False

    msg = build_email_message(jobs_to_send, all_jobs_count, sub_cfg, today, smtp_user)
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=25) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

    print(f"  [{sub_name}] Sent email digest ({len(jobs_to_send)} roles) to {msg['To']}.")
    return True


def process_subscribers(fetched, state, cfg, today, send_email=True, only_id=None):
    """Filter fetched jobs for each active subscriber, update their personal seen
    state in state['subscribers'][sub_id], and send each subscriber's email."""
    subscribers = get_subscribers(cfg, only_id=only_id)
    sub_states = state.setdefault("subscribers", {})
    retention = int(cfg["state_retention_days"])

    for sub_cfg in subscribers:
        sub_id = sub_cfg["id"]
        sub_rules = build_rules(sub_cfg)
        sub_matched = list({j["key"]: copy.deepcopy(j) for j in select(fetched, sub_rules, sub_cfg)}.values())

        sub_state = sub_states.setdefault(sub_id, {})
        sub_baseline = update_state(sub_matched, sub_state, today, retention)
        new_days = int(sub_cfg.get("new_days", cfg["new_days"]))
        for j in sub_matched:
            j["is_new"] = flag_new(j, today, new_days)
            j["eff_date"] = j.get("posted_at") or j["first_seen"]
        sub_matched.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
        sub_matched.sort(key=lambda j: j["eff_date"], reverse=True)

        if send_email:
            to_email = select_jobs_for_email(sub_matched, sub_baseline, sub_cfg)
            try:
                send_email_digest(to_email, len(sub_matched), sub_cfg, today)
            except Exception as e:
                print(f"  warning: email delivery failed for {sub_id}: {type(e).__name__}: {e}", file=sys.stderr)


def write_outputs(root, cfg, jobs, state, today, stamp):
    root = Path(root)
    write_json(root / "state" / "seen.json", state)
    write_json(root / "state" / "jobs.json", jobs)
    write_json(root / "docs" / "jobs.json", jobs)
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "index.html").write_text(render_page(jobs, cfg, stamp), encoding="utf-8")
    (docs / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    alert = root / "out" / "new_jobs.md"
    alert.parent.mkdir(parents=True, exist_ok=True)
    fresh = [j for j in jobs if j.get("discovered_now")]
    if fresh:
        alert.write_text(alert_markdown(fresh, today), encoding="utf-8")
    elif alert.exists():
        alert.unlink()
    return fresh


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def refresh(cfg, companies, root=ROOT, today=None, send_email=True, subscriber_id=None):
    today = today or today_utc()
    rules = build_rules(cfg)
    results = fetch_all(companies, cfg, today=today)
    failed = [(c, err) for c, jobs, err in results if jobs is None]
    for c, err in failed:
        print(f"  skipped {c['name']}: {err}")
    if len(failed) == len(results):
        print("Every board failed, so nothing was written. Run with --check to see why.")
        return 1

    fetched = [j for _, jobs, _ in results if jobs for j in jobs]
    matched = {j["key"]: j for j in select(fetched, rules, cfg)}.values()
    jobs = list(matched)

    root = Path(root)
    state = load_json(root / "state" / "seen.json", {})
    update_state(jobs, state, today, int(cfg["state_retention_days"]))

    # A board that failed this time keeps its last known roles, marked stale.
    failed_names = {c["name"] for c, _ in failed}
    existing_keys = {j["key"] for j in jobs}
    for p in load_json(root / "state" / "jobs.json", []):
        if p.get("company") in failed_names and p.get("key") not in existing_keys:
            jobs.append({**p, "stale": True, "discovered_now": False})
            existing_keys.add(p.get("key"))

    for j in jobs:
        j["is_new"] = flag_new(j, today, int(cfg["new_days"]))
        j["eff_date"] = j.get("posted_at") or j["first_seen"]
    jobs.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
    jobs.sort(key=lambda j: j["eff_date"], reverse=True)

    # Process per-subscriber filtering, state updates, and email digests
    process_subscribers(fetched, state, cfg, today, send_email=send_email, only_id=subscriber_id)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fresh = write_outputs(root, cfg, jobs, state, today, stamp)
    ok = len(results) - len(failed)
    print(f"Fetched {ok}/{len(results)} boards. {len(jobs)} matching roles on main board, "
          f"{sum(1 for j in jobs if j['is_new'])} marked NEW, {len(fresh)} first seen this run.")
    print(f"Page: {root / 'docs' / 'index.html'}")
    return 0


def check(cfg, companies):
    rules = build_rules(cfg)
    bad = 0
    for c, jobs, err in fetch_all(companies, cfg):
        if jobs is None:
            bad += 1
            print(f"FAIL  {c['name']:<22} {c.get('ats', '?'):<16} {err}")
            continue
        in_area = sum(1 for j in jobs if location_class(j["location"], rules, cfg))
        director = len(select(jobs, rules, cfg))
        print(f"ok    {c['name']:<22} {c['ats']:<16} {len(jobs):>5} jobs, "
              f"{in_area:>3} in Jalisco/remote-MX, {director:>3} director-level")
    print(f"\n{bad} of {len(companies)} boards failed." if bad else "\nAll boards responded.")
    return 1 if bad else 0


def demo(cfg, root=ROOT):
    today = today_utc()
    iso = lambda n: (today - timedelta(days=n)).isoformat()
    rows = [
        ("Senior Director, Site Operations", "Example Semiconductors", "Guadalajara, Jalisco, Mexico", "jalisco", "Operations", iso(1), True),
        ("Director of Engineering", "Example Cloud", "Zapopan, Jalisco", "jalisco", "Engineering", iso(2), True),
        ("VP, Latin America Operations", "Example Payments", "Remote - Mexico", "remote_mx", "Operations", iso(9), False),
        ("Head of Supply Chain", "Example Devices", "Tlaquepaque, Jalisco", "jalisco", "Supply Chain", None, False),
        ("Directora de Finanzas", "Example Retail", "Mexico", "mx_country", "Finance", iso(20), False),
    ]
    jobs = [{"key": f"demo:{i}", "company": co, "ats": "demo", "title": t, "location": loc,
             "team": team, "url": "https://example.com/jobs/" + str(i), "posted_at": posted,
             "loc_class": lc, "first_seen": iso(3 if posted is None else 0), "baseline": False,
             "is_new": new, "eff_date": posted or iso(3)}
            for i, (t, co, loc, lc, team, posted, new) in enumerate(rows)]
    out = Path(root) / "out" / "demo.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(jobs, cfg, "demo data"), encoding="utf-8")
    print(f"Wrote {out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="test every company board")
    ap.add_argument("--demo", action="store_true", help="build out/demo.html from fake data")
    ap.add_argument("--no-email", action="store_true", help="skip sending email digest")
    ap.add_argument("--email-all", action="store_true", help="email all current matching roles")
    ap.add_argument("--subscriber", help="only send email for a specific subscriber id")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.email_all:
        cfg["email_mode"] = "all"
        for sub in cfg.get("subscribers") or []:
            sub["email_mode"] = "all"
    if args.demo:
        return demo(cfg)
    companies = load_companies()
    if args.check:
        return check(cfg, companies)
    return refresh(cfg, companies, send_email=not args.no_email, subscriber_id=args.subscriber)


if __name__ == "__main__":
    sys.exit(main())
