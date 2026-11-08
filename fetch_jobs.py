#!/usr/bin/env python3
"""
job-radar: pulls public job feeds (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Workday), keeps director-level roles in Jalisco, and builds a
static page at docs/index.html.

  python3 fetch_jobs.py            normal run (fetch, filter, write page + state)
  python3 fetch_jobs.py --check    test every company board and report problems
  python3 fetch_jobs.py --demo     build out/demo.html from fake data (no network)

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent
UA = "job-radar/1.0 (personal job board)"

CFG_DEFAULTS = {
    "page_title": "Openings",
    "include_remote_mexico": True,
    "include_country_only": True,
    "new_days": 7,
    "max_workers": 8,
    "state_retention_days": 120,
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


def fetch_workday(c):
    host = f"{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    api = f"https://{host}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    today, found = today_utc(), {}
    for term in c.get("search_terms", ["Guadalajara", "Zapopan", "Jalisco", "El Salto", "Tonala"]):
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


def fetch_one(c):
    try:
        fn = FETCHERS.get(c.get("ats"))
        if fn is None:
            return c, None, f"unknown ats '{c.get('ats')}'"
        return c, fn(c), None
    except KeyError as e:
        return c, None, f"missing field {e} in companies.json"
    except error.HTTPError as e:
        hint = " (board not found: check the token)" if e.code == 404 else ""
        return c, None, f"HTTP {e.code}{hint}"
    except Exception as e:  # network down, bad JSON, anything else: report, don't crash
        return c, None, f"{type(e).__name__}: {e}"


def fetch_all(companies, cfg):
    with ThreadPoolExecutor(max_workers=int(cfg["max_workers"])) as ex:
        return list(ex.map(fetch_one, companies))


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------

def build_rules(cfg):
    jal = "|".join(re.escape(norm(x)) for x in cfg["jalisco_locations"])
    return {
        "inc": [re.compile(p, re.I) for p in cfg["title_include"]],
        "exc": [re.compile(p, re.I) for p in cfg["title_exclude"]],
        "jal": re.compile(rf"\b({jal})\b"),
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
# state: what have we seen before?
# --------------------------------------------------------------------------

def update_state(jobs, state, today, retention_days):
    """Stamp first_seen on each job. The very first run is a 'baseline': nothing
    found then counts as newly discovered (otherwise you'd get an alert for everything)."""
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


def flag_new(j, today, new_days):
    cutoff = today - timedelta(days=new_days)
    if j.get("posted_at"):
        return date.fromisoformat(j["posted_at"]) >= cutoff
    return (not j.get("baseline")) and date.fromisoformat(j["first_seen"]) >= cutoff


# --------------------------------------------------------------------------
# output
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
const LOC = {jalisco: "Jalisco", remote_mx: "Remote \\u00b7 Mexico", mx_country: "Mexico \\u00b7 city not listed"};
const DAY = 86400000;
function daysAgo(s) {
  const d = new Date(s + "T00:00:00"), t = new Date();
  t.setHours(0, 0, 0, 0);
  return Math.max(0, Math.round((t - d) / DAY));
}
function when(n) { return n === 0 ? "today" : n === 1 ? "yesterday" : n + " days ago"; }
function safeUrl(u) { return /^https?:\\/\\//i.test(u) ? u : "#"; }
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
    m.textContent = [j.company, j.location, j.team].filter(Boolean).join(" \\u00b7 ");
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

def refresh(cfg, companies, root=ROOT, today=None):
    today = today or today_utc()
    rules = build_rules(cfg)
    results = fetch_all(companies, cfg)
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
    for p in load_json(root / "state" / "jobs.json", []):
        if p.get("company") in failed_names:
            jobs.append({**p, "stale": True, "discovered_now": False})

    for j in jobs:
        j["is_new"] = flag_new(j, today, int(cfg["new_days"]))
        j["eff_date"] = j.get("posted_at") or j["first_seen"]
    jobs.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
    jobs.sort(key=lambda j: j["eff_date"], reverse=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fresh = write_outputs(root, cfg, jobs, state, today, stamp)
    ok = len(results) - len(failed)
    print(f"Fetched {ok}/{len(results)} boards. {len(jobs)} matching roles, "
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
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.demo:
        return demo(cfg)
    companies = load_companies()
    return check(cfg, companies) if args.check else refresh(cfg, companies)


if __name__ == "__main__":
    sys.exit(main())
