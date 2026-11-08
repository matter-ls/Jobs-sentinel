#!/usr/bin/env python3
# Jobs Sentinel - Multi-Subscriber Daily Digest + LLM CV Headhunter
"""
job-radar: pulls public job feeds (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Workday), keeps matching roles per subscriber profile,
builds a static page at docs/index.html, optionally calls Gemini LLM to
compare top job postings against each subscriber's master CV to generate a
tailored minimalist CV, and emails personalized daily digests.

  python3 fetch_jobs.py                     normal run (fetch, filter, write page + state, email all active subscribers)
  python3 fetch_jobs.py --subscriber lucas  only process email for subscriber 'lucas'
  python3 fetch_jobs.py --check             test every company board and report problems
  python3 fetch_jobs.py --demo              build out/demo.html from fake data (no network)

Exit codes: 0 ok · 1 nothing written (every board failed / all boards FAIL in --check)
            2 outputs written but at least one email digest failed to send

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import copy
import html
import io
import json
import os
import re
import smtplib
import sys
import textwrap
import time
import unicodedata
import zipfile
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
    "llm_model": "gemini-2.5-flash",
    "max_tailored_cvs": 3,
    "cv_attachment_format": "docx",
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

LLM_SYSTEM_PROMPT = """You are Lucy, an elite executive headhunter, ATS optimization architect, and minimalist resume strategist.
Compare the candidate's MASTER CV against the TARGET JOB POSTING and produce a CV that satisfies BOTH automated ATS/AI triage engines (top-decile keyword & semantic match) AND human hiring managers (immediate 'schedule for a call' conviction in a 6-second scan).

Strict Rules:
1. 100% TRUTHFULNESS: NEVER invent companies, job titles, dates, degrees, certifications, or fake metrics. Only select, prioritize, and re-angle real achievements from the MASTER CV so they truthfully mirror the domain terminology and business priorities of the TARGET JOB POSTING.
2. GENERAL ROLE DIRECTION HEADLIGHT (`target_direction`): Craft a clear overarching career direction headline at the top of the CV that informs the reader of the general type of role and functional leadership domain the candidate is pursuing—aligned with the area of the job being applied to (e.g., "Target Role Direction: Director / Head of Process Intelligence, GBS & Enterprise Operations"), rather than awkwardly parroting an internal requisition code.
3. ATS & AI TRIAGE OPTIMIZATION: Mirror exact domain terminology, methodologies, platforms, and leadership competencies from the job posting that genuinely exist in the candidate's background. Include both full terms and standard acronyms where natural (e.g., "Global Business Services (GBS)", "Object-Centric Process Mining (OCPM)"). Weave these keywords naturally into `summary` and `experience` bullets (where ATS engines assign highest weight) as well as `core_skills` (8–10 high-signal skills).
4. HUMAN RECRUITER 'CALL CONVICTION': Keep the CV minimalist, executive, and scannable on 1 page. Structure `summary` as a 3-sentence executive pitch (Domain Identity + Signature Quantified Proof + Value for Target Company). Format every `experience` bullet as `Strong Action Verb + Scope/Context + Quantified Business Outcome` (3-4 bullets for the 2 most relevant/recent roles; 1-2 bullets for earlier roles).
5. CAREER UPGRADE MOVE (`is_career_upgrade`): Set to true if this role represents a step up in seniority, leadership scope, architectural ownership, or strategic challenge compared to the candidate's current/most recent role on their MASTER CV.
6. Write in the language of the job posting (default to English if the posting is in English or mixed).

Return ONLY valid JSON matching this exact schema:
{
  "match_score": 92,
  "is_career_upgrade": true,
  "upgrade_reason": "1 concise sentence explaining why this role is a career upgrade move or high-impact stretch challenge relative to their current role.",
  "lucy_why_it_fits": "2-sentence energizing, high-conviction headhunter note directly to the candidate that gives them a genuine confidence boost—citing their real signature wins and why the hiring manager at this company will immediately want them on a call.",
  "matched_keywords": ["Keyword 1", "Keyword 2", "Keyword 3", "Keyword 4", "Keyword 5", "Keyword 6"],
  "gaps_to_address": ["1 brief tip or keyword to highlight in the interview"],
  "cv": {
    "full_name": "Candidate Full Name",
    "target_direction": "Target Role Direction: [General Role Level & Functional Domain Area Fulfilling the Target Job]",
    "target_headline": "Core Value Proposition Subheadline (Years of Experience | Signature Domain Pillars | Verified Impact)",
    "contact_line": "City, Country | Phone | Email | LinkedIn",
    "summary": "3-sentence ATS- and recruiter-optimized executive summary tailored specifically to this role's domain.",
    "core_skills": ["Skill 1", "Skill 2", "Skill 3", "Skill 4", "Skill 5", "Skill 6", "Skill 7", "Skill 8"],
    "experience": [
      {
        "company": "Company Name",
        "role": "Job Title",
        "location": "Location",
        "dates": "Start - End",
        "bullets": [
          "Action-verb bullet re-angled to highlight truthful, quantified impact and ATS keywords for this target role."
        ]
      }
    ],
    "education": [
      {"degree": "Degree or Certification", "institution": "Institution", "dates": "Dates"}
    ],
    "languages": ["Language 1 (Proficiency)", "Language 2 (Proficiency)"]
  }
}"""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def norm(s):
    """Lower-case and strip accents so 'Tonalá' == 'tonala'."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def strip_html(raw):
    """Convert HTML or rich text job descriptions into clean plain text."""
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", html.unescape(str(raw)))
    return re.sub(r"\s+", " ", txt).strip()


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


def load_subscriber_cv(sub_cfg, root=ROOT):
    """Load a subscriber's master CV text from `cv_text` or `cv_file`."""
    if sub_cfg.get("cv_text"):
        return str(sub_cfg["cv_text"]).strip()
    cv_rel = sub_cfg.get("cv_file")
    if not cv_rel:
        return ""
    for base in (Path(root), ROOT):
        cand = base / cv_rel
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip()
    return ""


def get_subscribers(cfg, only_id=None):
    """Return list of enabled subscriber configs, merging each with global defaults."""
    raw_subs = cfg.get("subscribers")
    if not raw_subs:
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


def http_json(url, payload=None, retries=2, headers=None, timeout=25):
    """GET (or POST when payload is given) and parse JSON. Retries 429/5xx/timeouts."""
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    err = None
    for attempt in range(retries + 1):
        try:
            req = request.Request(url, data=data, headers=hdrs)
            with request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300].strip()
                    if detail:
                        e.msg = f"{e.msg} — {detail}"
                except Exception:
                    pass
                raise
            err = e
        except (error.URLError, TimeoutError, json.JSONDecodeError) as e:
            err = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise err


def gha_annotate(level, text):
    """Print a GitHub Actions annotation (shows up in the run summary) and mirror it to stderr.
    Suppressed while running under unittest so negative tests don't pollute CI annotations."""
    in_ci = bool(os.environ.get("GITHUB_ACTIONS")) and "unittest" not in sys.modules
    print(f"::{level}::{text}" if in_ci else f"{level}: {text}", file=sys.stderr)


def gha_summary(markdown):
    """Append Markdown to the GitHub Actions step summary when running in CI (no-op locally)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def job(c, jid, title, location, url, posted=None, team="", description="", detail_api=""):
    out = {
        "key": f"{c['ats']}:{c['name']}:{jid}",
        "company": c["name"],
        "ats": c["ats"],
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "team": (team or "").strip(),
        "url": url or "",
        "posted_at": to_date(posted),
    }
    if description:
        out["description"] = strip_html(description)[:12000]
    if detail_api:
        out["detail_api"] = detail_api
    return out


# --------------------------------------------------------------------------
# one fetcher per applicant-tracking system
# --------------------------------------------------------------------------

def fetch_greenhouse(c):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs?content=true")
    return [
        job(c, j["id"], j.get("title"), (j.get("location") or {}).get("name"),
            j.get("absolute_url"), j.get("first_published"),
            description=j.get("content") or "")
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
        lists_txt = " ".join(
            f"{lst.get('text', '')}: {strip_html(lst.get('content', ''))}"
            for lst in (j.get("lists") or []) if isinstance(lst, dict)
        )
        desc = f"{j.get('descriptionPlain') or j.get('description') or ''} {lists_txt}".strip()
        out.append(job(c, j.get("id"), j.get("text"), loc, j.get("hostedUrl"),
                       j.get("createdAt"), cat.get("team") or cat.get("department"),
                       description=desc))
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
        desc = j.get("descriptionPlain") or j.get("descriptionHtml") or ""
        out.append(job(c, j.get("id"), j.get("title"), loc, j.get("jobUrl") or j.get("applyUrl"),
                       j.get("publishedAt"), j.get("department") or j.get("team"),
                       description=desc))
    return out


def fetch_workable(c):
    d = http_json(f"https://apply.workable.com/api/v1/widget/accounts/{c['board']}?details=true")
    out = []
    for j in d.get("jobs") or []:
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc += " (Remote)"
        desc = f"{j.get('description') or ''} {j.get('requirements') or ''} {j.get('benefits') or ''}".strip()
        out.append(job(c, j.get("shortcode") or j.get("code"), j.get("title"), loc,
                       j.get("url") or j.get("shortlink"),
                       j.get("published_on") or j.get("created_at"), j.get("department"),
                       description=desc))
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
            detail_api = f"https://api.smartrecruiters.com/v1/companies/{c['board']}/postings/{j['id']}"
            out.append(job(c, j["id"], j.get("name"), loc,
                           f"https://jobs.smartrecruiters.com/{c['board']}/{j['id']}",
                           j.get("releasedDate"), (j.get("department") or {}).get("label"),
                           detail_api=detail_api))
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
                detail_api = f"https://{host}/wday/cxs/{c['tenant']}/{c['site']}{path}" if path else ""
                found.setdefault(ref, job(
                    c, ref, p.get("title"), loc, f"https://{host}/en-US/{c['site']}{path}",
                    workday_posted(p.get("postedOn") or "", today),
                    detail_api=detail_api))
            offset += 20
            if not posts or offset >= total:
                break
    return list(found.values())


def enrich_job_description(j):
    """Fetch full job description on-demand for Workday / SmartRecruiters when needed."""
    if j.get("description"):
        return j["description"]
    detail_api = j.get("detail_api")
    if not detail_api:
        return ""
    try:
        d = http_json(detail_api, retries=1)
        if not isinstance(d, dict):
            return ""
        if "jobPostingInfo" in d:
            desc = (d.get("jobPostingInfo") or {}).get("jobDescription") or ""
        elif "jobAd" in d:
            sections = ((d.get("jobAd") or {}).get("sections") or {}).values()
            desc = " ".join(s.get("text", "") for s in sections if isinstance(s, dict))
        else:
            desc = d.get("description") or d.get("content") or ""
        j["description"] = strip_html(desc)[:12000]
        return j["description"]
    except Exception:
        return ""


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
    except Exception as e:
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
    """Stamp first_seen on each job in the given state dict."""
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
# LLM Headhunter Analysis & Minimalist CV Generator
# --------------------------------------------------------------------------

def parse_llm_json(raw_text):
    """Leniently extract and parse a JSON object from an LLM response string."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw_text or "").strip())
    candidates = [cleaned]
    m = re.search(r"\{.*\}", cleaned, re.S)
    if m and m.group(0) != cleaned:
        candidates.append(m.group(0))
    last_err = None
    for cand in candidates:
        for attempt_str in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                return json.loads(attempt_str, strict=False)
            except json.JSONDecodeError as e:
                last_err = e
    raise last_err or ValueError("Empty LLM JSON response")


def call_gemini_json(prompt_text, model="gemini-2.5-flash", api_key=None):
    """Call Gemini generateContent endpoint and return parsed JSON object.

    Uses `thinkingBudget: 0` on 2.5 models (so thinking tokens don't consume the
    output budget or truncate the JSON), sets `maxOutputTokens: 4096`, and falls
    back across models if a model is unavailable or returns malformed output."""
    api_key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None

    models_to_try = [model]
    for fallback_model in ("gemini-2.5-flash", "gemini-2.0-flash"):
        if fallback_model not in models_to_try:
            models_to_try.append(fallback_model)

    last_err = None
    for m_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
        gen_cfg = {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        }
        if m_name.startswith("gemini-2.5"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        payload = {
            "systemInstruction": {"parts": [{"text": LLM_SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": gen_cfg,
        }
        try:
            try:
                body = http_json(url, payload, retries=2, headers={"x-goog-api-key": api_key}, timeout=60)
            except error.HTTPError as e:
                if e.code == 400 and "thinkingConfig" in payload["generationConfig"]:
                    payload["generationConfig"].pop("thinkingConfig", None)
                    body = http_json(url, payload, retries=1, headers={"x-goog-api-key": api_key}, timeout=60)
                else:
                    raise
            cand0 = (body.get("candidates") or [{}])[0]
            parts = (cand0.get("content") or {}).get("parts") or []
            text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
            if not text_parts:
                text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
            raw_text = "\n".join(text_parts).strip()
            if not raw_text:
                reason = cand0.get("finishReason") or body.get("promptFeedback") or "empty candidates"
                raise ValueError(f"Gemini ({m_name}) returned no text ({reason})")
            parsed = parse_llm_json(raw_text)
            if isinstance(parsed, dict) and isinstance(parsed.get("cv"), dict):
                return parsed
            raise ValueError(f"Gemini ({m_name}) JSON missing 'cv' object")
        except Exception as e:
            last_err = e
            print(f"  [gemini] model {m_name} attempt failed: {type(e).__name__}: {e}", file=sys.stderr)
    if last_err:
        raise last_err
    return None


DEFAULT_UPGRADE_PATTERNS = [
    r"\b(director(a)?|head of|vp|svp|evp|vice[- ]?president(e|a)?|general manager|gerente general|country (manager|lead|head)|managing director|chief\b.*\bofficer)\b",
    r"\b(senior manager|sr\.? manager|gerente (senior|sr)|manager|gerente)\b",
    r"\b(principal|architect|arquitecto|coe lead|practice lead|global lead|regional lead|latam lead)\b",
    r"\b(head|lead|leader|lider)\b.*\b(automation|transformation|process|operations|operational excellence|continuous improvement|pmo|portfolio|gbs|data|analytics)\b",
]


def is_career_upgrade_heuristic(job_item, sub_cfg):
    """Return (is_upgrade, reason) by checking if a role represents a seniority/scope step-up."""
    title = job_item.get("title", "")
    t = norm(title)
    patterns = sub_cfg.get("upgrade_title_include") or DEFAULT_UPGRADE_PATTERNS
    if any(re.search(p, t, re.I) for p in patterns):
        co = job_item.get("company", "this employer")
        return True, f"Step-up leadership & scope opportunity ({title}) at {co}."
    return False, ""


def classify_career_upgrades(jobs, sub_cfg, root=ROOT):
    """Label roles in `jobs` with `is_career_upgrade` and `upgrade_reason`.

    First applies fast seniority/trajectory rules (`upgrade_title_include`), then—when
    `GEMINI_API_KEY` is configured and there are multiple roles—asks Gemini in a single
    compact batch call to refine which roles are true 'Career Upgrade Moves' (step-up
    challenge vs. lateral match) against the subscriber's Master CV."""
    if not jobs:
        return
    for j in jobs:
        if "is_career_upgrade" not in j:
            is_up, reason = is_career_upgrade_heuristic(j, sub_cfg)
            j["is_career_upgrade"] = is_up
            if reason:
                j["upgrade_reason"] = reason

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    master_cv = load_subscriber_cv(sub_cfg, root=root)
    if not api_key or not master_cv or len(jobs) <= 1:
        return

    # Single compact batch prompt for up to 25 roles so Luis's large list is triaged in 1 call
    batch = jobs[:25]
    items_txt = "\n".join(
        f"- key: {j['key']} | title: {j.get('title', '')} | company: {j.get('company', '')} | team: {j.get('team', '')}"
        for j in batch
    )
    prompt = (
        "Evaluate the following job openings against the candidate's MASTER CV.\n"
        "Identify which roles represent a 'Career Upgrade Move' (an upward step in seniority, "
        "leadership scope, architectural ownership, or high-growth strategic challenge compared "
        "to the candidate's current/most recent role, rather than a lateral or junior move).\n\n"
        'Return ONLY valid JSON matching: {"upgrades": [{"key": "<job key>", "is_career_upgrade": true, "upgrade_reason": "1 concise sentence why this is a career upgrade move"}]}\n\n'
        f"JOBS:\n{items_txt}\n\n---\nCANDIDATE MASTER CV:\n{master_cv[:4000]}"
    )
    model = os.environ.get("GEMINI_MODEL") or sub_cfg.get("llm_model", "gemini-2.5-flash")
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        gen_cfg = {"temperature": 0.1, "maxOutputTokens": 2048, "responseMimeType": "application/json"}
        if model.startswith("gemini-2.5"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        body = http_json(
            url,
            {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg},
            retries=1,
            headers={"x-goog-api-key": api_key},
            timeout=35,
        )
        parts = (((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
        raw = "\n".join(text_parts).strip()
        if raw:
            parsed = parse_llm_json(raw)
            by_key = {u.get("key"): u for u in (parsed.get("upgrades") or []) if isinstance(u, dict) and u.get("key")}
            if by_key:
                for j in batch:
                    if j["key"] in by_key:
                        u = by_key[j["key"]]
                        j["is_career_upgrade"] = bool(u.get("is_career_upgrade", j.get("is_career_upgrade")))
                        if u.get("upgrade_reason"):
                            j["upgrade_reason"] = str(u["upgrade_reason"]).strip()
    except Exception:
        # Heuristic labels are already applied on `jobs`
        pass


def infer_target_direction(job_item, sub_cfg=None, core_skills=None):
    """Craft a natural 'General Role Direction' headlight that communicates the broader type
    of role and functional domain the candidate is targeting, fulfilling the area of the job
    being applied to without mechanically copying internal requisition codes."""
    title = (job_item or {}).get("title", "")
    team = (job_item or {}).get("team", "")
    blob = norm(f"{title} {team} {' '.join(core_skills or [])}")

    if re.search(r"\b(vp|vice[- ]?president(e|a)?|svp|evp|chief|general manager|gerente general|managing director|country (manager|lead|head))\b", blob):
        level = "Executive Leadership (VP / GM / Head of Function)"
    elif re.search(r"\b(director(a)?|head of)\b", blob):
        level = "Director / Head of Function"
    elif re.search(r"\b(senior manager|sr\.? manager|gerente senior|principal|architect|arquitecto|coe lead|practice lead|global lead)\b", blob):
        level = "Senior Manager / Principal & Functional Lead"
    elif re.search(r"\b(manager|gerente|lead|leader|lider)\b", blob):
        level = "Manager & Cross-Functional Delivery Lead"
    else:
        level = "Senior Operations & Domain Specialist"

    if re.search(r"\b(organizational change|change management|change enablement|digital adoption|customer experience|partner experience|\bcx\b)\b", blob):
        domain = "Organizational Change Management, Customer/Partner Experience (CX) & Digital Transformation"
    elif re.search(r"\b(process mining|celonis|process intelligence|process excellence|operational excellence|continuous improvement|lean|six sigma|bpi)\b", blob):
        domain = "Process Intelligence, Operational Excellence & Continuous Improvement"
    elif re.search(r"\b(automation|rpa|power platform|power apps|power automate|low[- ]code|hyperautomation|blue prism|uipath)\b", blob):
        domain = "Intelligent Automation, Low-Code Architecture & Process Transformation"
    elif re.search(r"\b(gbs|shared services|business services|business transformation|digital transformation|transformation)\b", blob):
        domain = "Global Business Services (GBS), Process Strategy & Enterprise Transformation"
    elif re.search(r"\b(engineering|platform|software|cloud|infrastructure|technology)\b", blob):
        domain = "Engineering & Platform Leadership, Cloud Systems & Technical Operations"
    elif re.search(r"\b(data|analytics|bi\b|business intelligence|decision|forecasting)\b", blob):
        domain = "Data & Analytics Strategy, Business Intelligence & Decision Systems"
    elif re.search(r"\b(supply chain|logistics|manufacturing|fab\b|quality|procurement|operations|operaciones)\b", blob):
        domain = "Global Operations, Supply Chain & End-to-End Process Optimization"
    elif re.search(r"\b(sales|revenue|revops|bdr|sdr|business development|account|commercial|customer success|ventas)\b", blob):
        domain = "Revenue Operations, Enterprise Business Development & Commercial Analytics"
    elif re.search(r"\b(program|project|pmo|agile|scrum|delivery|portfolio|proyectos)\b", blob):
        domain = "Strategic Program Management (PMO), Agile Delivery & Portfolio Execution"
    else:
        top_skills = " · ".join((core_skills or ["Enterprise Operations", "Strategy", "Execution"])[:3])
        domain = top_skills

    return f"Target Role Direction: {level} — {domain}"


def tailor_cv_deterministic(job_item, master_cv_text, sub_cfg):
    """Build an ATS- and recruiter-optimized CV directly from the subscriber's master CV
    Markdown when Gemini API is unavailable, rate-limited, or unconfigured."""
    lines = [ln.strip() for ln in (master_cv_text or "").splitlines()]
    full_name = sub_cfg.get("name", "Candidate")
    contact_line = sub_cfg.get("recipient_email", "")
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            full_name = ln[2:].strip().title()
            if i + 1 < len(lines) and lines[i + 1] and not lines[i + 1].startswith("#"):
                contact_line = lines[i + 1].strip()
            break

    sections = {}
    cur_sec = None
    for ln in lines:
        if ln.startswith("## "):
            cur_sec = ln[3:].strip().upper()
            sections[cur_sec] = []
        elif cur_sec is not None:
            sections[cur_sec].append(ln)

    summary = " ".join(x for x in sections.get("PROFESSIONAL SUMMARY", []) if x)
    job_title = job_item.get("title", "Target Role")
    company = job_item.get("company", "Company")
    job_blob = norm(f"{job_title} {company} {job_item.get('team', '')} {job_item.get('description', '')}")

    # Extract skills from CORE COMPETENCIES
    raw_skills = []
    for ln in sections.get("CORE COMPETENCIES", []):
        if ln.startswith("- ") or ln.startswith("* "):
            cleaned_ln = re.sub(r"\*\*[^*]+\*\*:?\s*", "", ln[2:])
            for part in re.split(r"[,;]", cleaned_ln):
                s = part.strip(" .")
                if 2 < len(s) <= 52:
                    raw_skills.append(s)
    # Sort skills so those matching the target job appear first for ATS keyword density
    raw_skills.sort(key=lambda s: (0 if any(w in job_blob for w in norm(s).split() if len(w) > 3) else 1))
    core_skills = list(dict.fromkeys(raw_skills))[:10] or ["Executive Leadership", "Operations", "Strategy"]
    matched_kw = [s for s in core_skills if any(w in job_blob for w in norm(s).split() if len(w) > 3)][:6] or core_skills[:4]

    # Parse PROFESSIONAL EXPERIENCE blocks (### Company | Location + **Role** | Dates + bullets)
    experience = []
    cur_exp = None
    for ln in sections.get("PROFESSIONAL EXPERIENCE", []):
        if ln.startswith("### "):
            parts = [p.strip() for p in ln[4:].split("|")]
            cur_exp = {
                "company": re.sub(r"\*+", "", parts[0]),
                "location": parts[1] if len(parts) > 1 else "",
                "role": "",
                "dates": "",
                "bullets": [],
            }
            experience.append(cur_exp)
        elif cur_exp is not None and ln.startswith("**") and ("|" in ln or "–" in ln or "-" in ln):
            parts = [p.strip() for p in ln.split("|")]
            role_txt = re.sub(r"\*+", "", parts[0]).strip()
            if cur_exp["role"] and cur_exp["bullets"]:
                cur_exp = {
                    "company": cur_exp["company"],
                    "location": cur_exp["location"],
                    "role": role_txt,
                    "dates": parts[1] if len(parts) > 1 else "",
                    "bullets": [],
                }
                experience.append(cur_exp)
            else:
                cur_exp["role"] = role_txt
                cur_exp["dates"] = parts[1] if len(parts) > 1 else ""
        elif cur_exp is not None and (ln.startswith("- ") or ln.startswith("* ")):
            b_txt = re.sub(r"\*\*([^*]+)\*\*", r"\1", ln[2:].strip())
            if cur_exp["role"]:
                cur_exp["bullets"].append(b_txt)

    experience = [e for e in experience if e.get("role") and e.get("bullets")][:4]
    for idx, e in enumerate(experience):
        e["bullets"].sort(key=lambda b: (0 if any(w in job_blob for w in norm(b).split() if len(w) > 4) else 1))
        e["bullets"] = e["bullets"][:4 if idx < 2 else 2]

    education = []
    for sec_name in ("EDUCATION & CERTIFICATIONS", "EDUCATION"):
        for ln in sections.get(sec_name, []):
            if ln.startswith("- ") or ln.startswith("* "):
                txt = re.sub(r"\*+", "", ln[2:].strip())
                parts = [p.strip() for p in re.split(r"—|--", txt, maxsplit=1)]
                education.append({
                    "degree": parts[0],
                    "institution": parts[1] if len(parts) > 1 else "",
                    "dates": "",
                })

    languages = []
    for sec_name in ("LANGUAGES", "ADDITIONAL EDUCATION & SKILLS"):
        for ln in sections.get(sec_name, []):
            if "language" in ln.lower() or sec_name == "LANGUAGES":
                clean_l = re.sub(r"^[-*]\s*(?:\*\*Languages:?\*\*:?\s*)?", "", ln).strip()
                clean_l = re.sub(r"\*+", "", clean_l)
                for seg in re.split(r"[|,]", clean_l):
                    if seg.strip():
                        languages.append(seg.strip())

    target_direction = infer_target_direction(job_item, sub_cfg, core_skills)
    score = min(96, 84 + 2 * len(matched_kw))
    is_up, up_reason = is_career_upgrade_heuristic(job_item, sub_cfg)
    return {
        "match_score": score,
        "is_career_upgrade": is_up,
        "upgrade_reason": up_reason,
        "lucy_why_it_fits": (
            f"Your background in {', '.join(matched_kw[:3])} aligns directly with {company}'s "
            f"{job_title} opening in {job_item.get('location') or 'Mexico'}."
        ),
        "matched_keywords": matched_kw,
        "gaps_to_address": [f"Emphasize measurable impact relevant to {company}'s {job_title} scope."],
        "cv": {
            "full_name": full_name,
            "target_direction": target_direction,
            "target_headline": f"{job_title} | {' · '.join(core_skills[:3])}",
            "contact_line": contact_line,
            "summary": summary,
            "core_skills": core_skills,
            "experience": experience,
            "education": education[:3],
            "languages": languages[:4],
        },
    }


def tailor_cv_with_llm(job_item, master_cv_text, sub_cfg):
    """Analyze job vs. master CV using Gemini and return structured match + ATS-ready CV."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not master_cv_text:
        return None

    jd_text = enrich_job_description(job_item)
    if not jd_text:
        jd_text = (
            f"Role Title: {job_item.get('title', '')}\n"
            f"Company: {job_item.get('company', '')}\n"
            f"Department/Team: {job_item.get('team', 'General')}\n"
            f"Location: {job_item.get('location', '')}\n"
            "(Full description text not exposed in feed; tailor CV to role title, company industry, and level.)"
        )

    prompt = (
        f"TARGET JOB POSTING:\n"
        f"Title: {job_item.get('title', '')}\n"
        f"Company: {job_item.get('company', '')}\n"
        f"Location: {job_item.get('location', '')}\n"
        f"Team: {job_item.get('team', '')}\n"
        f"Job Description:\n{jd_text}\n\n"
        f"---\n"
        f"CANDIDATE MASTER CV:\n{master_cv_text}"
    )
    model = os.environ.get("GEMINI_MODEL") or sub_cfg.get("llm_model", "gemini-2.5-flash")
    try:
        res = call_gemini_json(prompt, model=model, api_key=api_key)
        if res and isinstance(res.get("cv"), dict):
            cv_obj = res["cv"]
            if not cv_obj.get("target_direction"):
                cv_obj["target_direction"] = infer_target_direction(
                    job_item, sub_cfg, cv_obj.get("core_skills") or []
                )
            return res
    except Exception as e:
        gha_annotate(
            "warning",
            f"Gemini API fallback triggered for '{job_item.get('title')}' ({type(e).__name__}: {e}) — using deterministic CV tailor.",
        )
    return tailor_cv_deterministic(job_item, master_cv_text, sub_cfg)


def _xml_esc(s):
    """Escape text for safe inclusion inside OpenXML (<w:t>) nodes."""
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(s or ""))
    return html.escape(clean, quote=True)


def render_minimalist_cv_docx(cv_data, job_item=None):
    """Generate a single-column, ATS-optimized Microsoft Word (.docx) resume using pure
    Python standard library (`zipfile` + OpenXML).

    Why .docx:
    1. Previews natively inside Gmail's built-in document viewer (and opens in 1 click in
       Google Docs or Microsoft Word) instead of displaying raw HTML source code.
    2. Universally parsed with 100% text fidelity by every ATS (Workday, Greenhouse, Lever,
       SmartRecruiters, Taleo, iCIMS) because each section heading and bullet is a standard
       semantic `<w:p>` paragraph in top-to-bottom linear reading order."""
    full_name = cv_data.get("full_name") or "Candidate"
    target_direction = cv_data.get("target_direction") or infer_target_direction(
        job_item or {}, None, cv_data.get("core_skills") or []
    )
    headline = cv_data.get("target_headline") or ""
    contact = cv_data.get("contact_line") or ""
    summary = cv_data.get("summary") or ""
    skills = [s for s in (cv_data.get("core_skills") or []) if s]
    experiences = cv_data.get("experience") or []
    educations = cv_data.get("education") or []
    languages = [s for s in (cv_data.get("languages") or []) if s]

    def run_xml(text, sz=20, bold=False, italic=False, color="18181B"):
        rpr = [
            '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>',
            f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>',
            f'<w:color w:val="{color}"/>',
        ]
        if bold:
            rpr.append("<w:b/>")
        if italic:
            rpr.append("<w:i/>")
        return f'<w:r><w:rPr>{"".join(rpr)}</w:rPr><w:t xml:space="preserve">{_xml_esc(text)}</w:t></w:r>'

    def p_xml(runs, style=None, before=0, after=60, line=250, keep_next=False, bottom_border=None, indent_left=0, hanging=0):
        ppr = []
        if style:
            ppr.append(f'<w:pStyle w:val="{style}"/>')
        if keep_next:
            ppr.append("<w:keepNext/>")
        if bottom_border:
            color, sz = bottom_border
            ppr.append(
                f'<w:pBdr><w:bottom w:val="single" w:sz="{sz}" w:space="4" w:color="{color}"/></w:pBdr>'
            )
        if indent_left or hanging:
            ppr.append(f'<w:ind w:left="{indent_left}" w:hanging="{hanging}"/>')
        ppr.append(f'<w:spacing w:before="{before}" w:after="{after}" w:line="{line}" w:lineRule="auto"/>')
        return f'<w:p><w:pPr>{"".join(ppr)}</w:pPr>{"".join(runs)}</w:p>'

    def section_header_xml(title_text):
        return p_xml(
            [run_xml(title_text.upper(), sz=19, bold=True, color="0F766E")],
            style="Heading2",
            before=140,
            after=65,
            line=240,
            keep_next=True,
            bottom_border=("D4D4D8", 4),
        )

    paragraphs = []
    # 1. Candidate Full Name (Heading 1)
    paragraphs.append(
        p_xml([run_xml(full_name, sz=38, bold=True, color="09090B")], style="Heading1", before=0, after=30, line=240, keep_next=True)
    )
    # 2. General Role Direction Headlight (informs ATS & recruiter of overall role trajectory)
    if target_direction:
        paragraphs.append(
            p_xml([run_xml(target_direction, sz=21, bold=True, color="0F766E")], before=0, after=25, line=240, keep_next=True)
        )
    # 3. Role-Tailored Value Proposition Subheadline
    if headline:
        paragraphs.append(
            p_xml([run_xml(headline, sz=20, bold=True, color="27272A")], before=0, after=35, line=240, keep_next=True)
        )
    # 4. Contact Information with crisp executive divider
    if contact:
        paragraphs.append(
            p_xml([run_xml(contact, sz=18, color="52525B")], before=0, after=110, line=240, bottom_border=("18181B", 12))
        )

    # 5. Executive Summary
    if summary:
        paragraphs.append(section_header_xml("EXECUTIVE SUMMARY"))
        paragraphs.append(p_xml([run_xml(summary, sz=20, color="18181B")], before=0, after=80, line=255))

    # 6. Core Competencies & ATS Keywords
    if skills:
        paragraphs.append(section_header_xml("CORE COMPETENCIES & KEY SKILLS"))
        skill_runs = []
        for idx, sk in enumerate(skills):
            if idx > 0:
                skill_runs.append(run_xml("   •   ", sz=18, bold=True, color="0F766E"))
            skill_runs.append(run_xml(sk, sz=19, bold=True, color="18181B"))
        paragraphs.append(p_xml(skill_runs, before=0, after=90, line=250))

    # 7. Professional Experience (Reverse-Chronological, CAR/XYZ Impact Bullets)
    if experiences:
        paragraphs.append(section_header_xml("PROFESSIONAL EXPERIENCE"))
        for exp in experiences:
            role_str = exp.get("role") or ""
            comp_str = exp.get("company") or ""
            loc_str = exp.get("location") or ""
            dates_str = exp.get("dates") or ""
            head_runs = [run_xml(role_str, sz=21, bold=True, color="09090B")]
            if comp_str:
                head_runs.append(run_xml("  —  ", sz=20, color="52525B"))
                head_runs.append(run_xml(comp_str, sz=21, bold=True, color="0F766E"))
            if loc_str:
                head_runs.append(run_xml(f"  ·  {loc_str}", sz=19, color="52525B"))
            if dates_str:
                head_runs.append(run_xml(f"    |    {dates_str}", sz=19, italic=True, color="52525B"))
            paragraphs.append(p_xml(head_runs, before=85, after=30, line=240, keep_next=True))

            for b in exp.get("bullets") or []:
                if not b:
                    continue
                paragraphs.append(
                    p_xml(
                        [run_xml("•  ", sz=19, bold=True, color="0F766E"), run_xml(b, sz=19, color="18181B")],
                        before=0,
                        after=40,
                        line=245,
                        indent_left=260,
                        hanging=160,
                    )
                )

    # 8. Education, Certifications & Languages
    if educations or languages:
        paragraphs.append(section_header_xml("EDUCATION, CERTIFICATIONS & LANGUAGES"))
        for edu in educations:
            deg = edu.get("degree") or ""
            inst = edu.get("institution") or ""
            dts = edu.get("dates") or ""
            edu_runs = [run_xml(deg, sz=19, bold=True, color="09090B")]
            if inst:
                edu_runs.append(run_xml(f"  —  {inst}", sz=19, color="27272A"))
            if dts:
                edu_runs.append(run_xml(f" ({dts})", sz=18, color="52525B"))
            paragraphs.append(p_xml(edu_runs, before=0, after=35, line=240))
        if languages:
            paragraphs.append(
                p_xml(
                    [run_xml("Languages: ", sz=19, bold=True, color="09090B"), run_xml(" · ".join(languages), sz=19, color="27272A")],
                    before=20,
                    after=40,
                    line=240,
                )
            )

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(paragraphs)
        + '<w:sectPr>'
        '<w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="864" w:right="936" w:bottom="864" w:left="936" w:header="432" w:footer="432" w:gutter="0"/>'
        "</w:sectPr>"
        "</w:body></w:document>"
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:docDefaults>"
        "<w:rPrDefault><w:rPr>"
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
        '<w:sz w:val="20"/><w:szCs w:val="20"/>'
        '<w:color w:val="18181B"/>'
        "</w:rPr></w:rPrDefault>"
        "</w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        "<w:rPr><w:b/><w:sz w:val=\"38\"/></w:rPr>"
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading2">'
        '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:qFormat/>'
        "<w:rPr><w:b/><w:sz w:val=\"19\"/></w:rPr>"
        "</w:style>"
        "</w:styles>"
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    doc_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("word/_rels/document.xml.rels", doc_rels_xml)
        zf.writestr("word/styles.xml", styles_xml)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def extract_docx_text(docx_bytes):
    """Extract plain text from a `.docx` byte buffer for verification and testing."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes), "r") as zf:
        xml_data = zf.read("word/document.xml").decode("utf-8", errors="replace")
    paragraphs = re.findall(r"<w:p\b[^>]*>(.*?)</w:p>", xml_data, re.S)
    out = []
    for p in paragraphs:
        texts = [html.unescape(t) for t in re.findall(r"<w:t\b[^>]*>(.*?)</w:t>", p, re.S)]
        if texts:
            out.append("".join(texts))
    return "\n".join(out)


def render_minimalist_cv_pdf(cv_data, job_item=None):
    """Generate a clean, single-column ATS-readable PDF 1.4 document in pure stdlib."""
    def pdf_str(s):
        # Map common typography punctuation to Latin-1 equivalents for Type1 Helvetica
        s = (
            str(s or "")
            .replace("—", " - ")
            .replace("–", "-")
            .replace("•", "*")
            .replace("·", "|")
            .replace("→", "->")
            .replace("“", '"')
            .replace("”", '"')
            .replace("’", "'")
        )
        encoded = s.encode("latin-1", errors="replace").decode("latin-1")
        return encoded.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    full_name = cv_data.get("full_name") or "Candidate"
    target_direction = cv_data.get("target_direction") or infer_target_direction(
        job_item or {}, None, cv_data.get("core_skills") or []
    )
    headline = cv_data.get("target_headline") or ""
    contact = cv_data.get("contact_line") or ""
    summary = cv_data.get("summary") or ""
    skills = [s for s in (cv_data.get("core_skills") or []) if s]

    ops = []
    y = 750

    def add_line(txt, font="F1", size=9.5, leading=13, x=48):
        nonlocal y
        if y < 52:
            return
        ops.append(f"BT /{font} {size} Tf 1 0 0 1 {x} {y:.1f} Tm ({pdf_str(txt)}) Tj ET")
        y -= leading

    def add_wrapped(txt, width=95, font="F1", size=9.5, leading=13, x=48, prefix=""):
        for idx, ln in enumerate(textwrap.wrap(str(txt or ""), width=width) or [""]):
            line_txt = f"{prefix}{ln}" if idx == 0 else ("   " + ln if prefix else ln)
            add_line(line_txt, font=font, size=size, leading=leading, x=x)

    add_line(full_name, font="F2", size=17, leading=19)
    if target_direction:
        add_wrapped(target_direction, width=90, font="F2", size=10, leading=13.5)
    if headline:
        add_wrapped(headline, width=92, font="F2", size=9.5, leading=13)
    if contact:
        add_line(contact, font="F1", size=8.5, leading=14)
    ops.append(f"0.7 w 48 {y + 6:.1f} m 564 {y + 6:.1f} l S")
    y -= 6

    if summary:
        add_line("EXECUTIVE SUMMARY", font="F2", size=9.5, leading=13)
        add_wrapped(summary, width=96, font="F1", size=9.2, leading=12.5)
        y -= 4

    if skills:
        add_line("CORE COMPETENCIES & KEY SKILLS", font="F2", size=9.5, leading=13)
        add_wrapped(" | ".join(skills), width=96, font="F1", size=9.0, leading=12.5)
        y -= 4

    if cv_data.get("experience"):
        add_line("PROFESSIONAL EXPERIENCE", font="F2", size=9.5, leading=13)
        for exp in cv_data.get("experience") or []:
            hdr = f"{exp.get('role', '')} - {exp.get('company', '')}"
            if exp.get("location"):
                hdr += f" ({exp.get('location')})"
            if exp.get("dates"):
                hdr += f" | {exp.get('dates')}"
            add_wrapped(hdr, width=92, font="F2", size=9.5, leading=13)
            for b in exp.get("bullets") or []:
                if b:
                    add_wrapped(b, width=92, font="F1", size=9.0, leading=12, x=56, prefix="- ")
            y -= 3

    if cv_data.get("education"):
        add_line("EDUCATION & CERTIFICATIONS", font="F2", size=9.5, leading=13)
        for edu in cv_data.get("education") or []:
            line_edu = f"{edu.get('degree', '')} - {edu.get('institution', '')}"
            if edu.get("dates"):
                line_edu += f" ({edu.get('dates')})"
            add_wrapped(line_edu, width=95, font="F1", size=9.0, leading=12.5)

    stream_bytes = "\n".join(ops).encode("latin-1", errors="replace")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>\nendobj\n",
        f"6 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"\nendstream\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for o in objs:
        offsets.append(len(out))
        out.extend(o)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def render_minimalist_cv_html(cv_data, job_item=None):
    """Render a sleek, single-page print-ready & ATS-structured minimalist CV in HTML."""
    esc = html.escape
    full_name = esc(cv_data.get("full_name", "Candidate"))
    raw_dir = cv_data.get("target_direction") or infer_target_direction(
        job_item or {}, None, cv_data.get("core_skills") or []
    )
    direction = esc(raw_dir)
    headline = esc(cv_data.get("target_headline", ""))
    contact = esc(cv_data.get("contact_line", ""))
    summary = esc(cv_data.get("summary", ""))
    skills = [esc(s) for s in (cv_data.get("core_skills") or []) if s]
    skills_html = "".join(f"<span class='skill-pill'>{s}</span>" for s in skills)

    exp_blocks = []
    for exp in cv_data.get("experience") or []:
        bullets = "".join(f"<li>{esc(b)}</li>" for b in (exp.get("bullets") or []) if b)
        loc_part = f" &middot; {esc(exp.get('location', ''))}" if exp.get("location") else ""
        exp_blocks.append(f"""
        <div class="job">
          <div class="job-head">
            <div><span class="role">{esc(exp.get('role', ''))}</span> &mdash; <span class="company">{esc(exp.get('company', ''))}</span>{loc_part}</div>
            <div class="dates">{esc(exp.get('dates', ''))}</div>
          </div>
          <ul>{bullets}</ul>
        </div>""")

    edu_blocks = []
    for edu in cv_data.get("education") or []:
        dates_part = f" <span class='dates'>({esc(edu.get('dates', ''))})</span>" if edu.get("dates") else ""
        edu_blocks.append(
            f"<div class='edu'><strong>{esc(edu.get('degree', ''))}</strong> &mdash; "
            f"{esc(edu.get('institution', ''))}{dates_part}</div>"
        )
    languages = [esc(s) for s in (cv_data.get("languages") or []) if s]
    if languages:
        edu_blocks.append(f"<div class='edu'><strong>Languages:</strong> {' &middot; '.join(languages)}</div>")

    target_note = ""
    if job_item:
        target_note = f"Tailored by Lucy for: {esc(job_item.get('title', ''))} at {esc(job_item.get('company', ''))}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_name} — Minimalist CV</title>
<style>
  @page {{ size: A4; margin: 14mm 16mm; }}
  * {{ box-sizing: border-box; }}
  body {{
    font: 10pt/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: #18181b;
    background: #f4f4f5;
    margin: 0;
    padding: 24px 12px;
  }}
  .sheet {{
    max-width: 780px;
    margin: 0 auto;
    background: #ffffff;
    padding: 36px 42px;
    border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.06);
  }}
  .toolbar {{
    max-width: 780px;
    margin: 0 auto 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 9pt;
    color: #52525b;
  }}
  .print-btn {{
    background: #0f766e;
    color: #ffffff;
    border: none;
    padding: 7px 14px;
    border-radius: 6px;
    font-weight: 600;
    cursor: pointer;
    font-size: 9pt;
  }}
  h1 {{ font-size: 21pt; font-weight: 800; margin: 0; letter-spacing: -0.4px; color: #09090b; }}
  .direction {{ font-size: 10.5pt; font-weight: 700; color: #0f766e; margin: 4px 0 2px; }}
  .headline {{ font-size: 10pt; font-weight: 600; color: #27272a; margin: 0 0 5px; }}
  .contact {{ font-size: 9pt; color: #52525b; padding-bottom: 10px; border-bottom: 1.5px solid #18181b; margin-bottom: 12px; }}
  h2 {{
    font-size: 8.5pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: #0f766e;
    margin: 14px 0 6px;
    padding-bottom: 3px;
    border-bottom: 1px solid #e4e4e7;
  }}
  .summary {{ font-size: 9.8pt; color: #27272a; margin: 0 0 8px; }}
  .skills {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }}
  .skill-pill {{
    font-size: 8.8pt;
    background: #f4f4f5;
    color: #27272a;
    padding: 2px 8px;
    border-radius: 4px;
    border: 1px solid #e4e4e7;
    font-weight: 500;
  }}
  .job {{ margin-bottom: 10px; page-break-inside: avoid; }}
  .job-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 2px; }}
  .role {{ font-weight: 700; font-size: 10pt; color: #09090b; }}
  .company {{ font-weight: 600; color: #0f766e; }}
  .dates {{ font-size: 8.8pt; color: #71717a; white-space: nowrap; }}
  ul {{ margin: 3px 0 0 16px; padding: 0; }}
  li {{ margin-bottom: 3px; font-size: 9.5pt; color: #27272a; }}
  .edu {{ font-size: 9.5pt; margin-bottom: 4px; }}
  @media print {{
    body {{ background: #ffffff; padding: 0; }}
    .sheet {{ box-shadow: none; padding: 0; max-width: 100%; border-radius: 0; }}
    .toolbar {{ display: none; }}
  }}
</style>
</head>
<body>
  <div class="toolbar">
    <span>{target_note}</span>
    <button class="print-btn" onclick="window.print()">Save as PDF / Print</button>
  </div>
  <div class="sheet">
    <h1>{full_name}</h1>
    <div class="direction">{direction}</div>
    <div class="headline">{headline}</div>
    <div class="contact">{contact}</div>

    <h2>Executive Summary</h2>
    <p class="summary">{summary}</p>

    <h2>Core Competencies &amp; Key Skills</h2>
    <div class="skills">{skills_html}</div>

    <h2>Professional Experience</h2>
    {''.join(exp_blocks)}

    <h2>Education &amp; Certifications</h2>
    {''.join(edu_blocks)}
  </div>
</body>
</html>"""


def cv_attachment_filename(sub_name, job_item, ext="docx"):
    """Build a clean, filesystem-safe filename for a tailored CV attachment."""
    raw = f"CV_{sub_name}_{job_item.get('company', '')}_{job_item.get('title', '')}"
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", norm(raw)).strip("_")
    ext_clean = str(ext or "docx").lstrip(".")
    return f"{clean[:60]}.{ext_clean}"


# --------------------------------------------------------------------------
# output & daily email notifications
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
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
const LOC = {jalisco: "Jalisco", remote_mx: "Remote · Mexico", mx_country: "Mexico · city not listed"};
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
    m.textContent = [j.company, j.location, j.team].filter(Boolean).join(" · ");
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


DAILY_MOTIVATION_SYSTEM_PROMPT = """You are Lucy, an elite, sharp-witted private executive headhunter writing a 45-second morning briefing to your star client.
Your philosophy is 'Us Against the System' & 'Relentless Career Velocity':
- Staying comfortable in one seat pays the 'loyalty tax' (3-4% raises and slow internal queues), whereas strategic job moves are the #1 proven lever to unlock 25-50% compensation jumps, bigger titles, and fresh architectural scope.
- Challenge the candidate never to get stuck where they are—move from a position of strength before comfort turns into stagnation.
- Most applicants blindly toss generic PDFs into ATS black holes; YOU and the candidate are gaming the hiring machine together—you scanned the market overnight, isolated the upgrade moves, and pre-built ATS-ready CVs so firing off a top-1% application takes 60 seconds.

Rules:
- Address the candidate naturally by first name.
- Keep the tone sharp, insider, confident, and executive—never cheesy corporate-speak.
- Write 1 punchy headline (4-8 words) and a 2-3 sentence message (40-65 words) that sparks an immediate dopamine hit and makes them want to submit 1 application right now.
- Return ONLY valid JSON: {"headline": "...", "message": "..."}"""

DAILY_MOTIVATION_THEMES = [
    "Refuse the corporate loyalty tax: strategic external moves unlock 30%+ leaps that internal review cycles stall for years.",
    "Challenge yourself before comfort becomes a cage: if you aren't stretching your scope right now, you're leaving ceiling on the table.",
    "Us vs. the hiring black box: while 95% of people spray generic resumes, we already reverse-engineered the ATS and armed your CVs.",
    "Move from strength, not urgency: interviewing while you're winning lets you negotiate like an executive peer, never a supplicant.",
    "Asymmetric daily upside: 60 seconds to send one surgical application costs nothing—and one 'yes' reprices your entire career trajectory.",
    "Outgrow the waiting line: when your execution outpaces your current title, the open market is the fastest promotion committee on earth.",
    "Momentum beats perfection: one high-conviction shot fired before 9 AM puts you leagues ahead of talented people who stay stuck.",
]

DAILY_INSIDER_VAULT = [
    (
        "Stop Paying the Comfort Tax.",
        "{first_name}, staying comfortable in one seat hands you 3% adjustments while strategic external moves unlock 30%+ leaps in scope and comp. It's you and me against the hiring black box—I already isolated {highlight_count} high-leverage play(s) including {top_company} and armed your ATS-ready CVs. Take 60 seconds and fire one shot.",
    ),
    (
        "Don't Wait in Somebody Else's Queue.",
        "{first_name}, internal promotion cycles are designed to keep great operators patiently producing in place. The fastest way to level up your title and leverage is moving seats on your own terms—and today's radar at {top_company} is wide open for your track record.",
    ),
    (
        "Comfort Is Crowded. Growth Is Open.",
        "The moment a role stops stretching you is the moment to look up, {first_name}. While 95% of passive talent waits for permission, we already scanned the market overnight and tailored your CVs to beat the triage engine—pick one role below and make them tell you no.",
    ),
    (
        "You + Lucy vs. The Hiring Machine.",
        "Most candidates lose before a human ever reads their name; we flipped the script, {first_name}. Your experience is framed for both the ATS algorithm and the hiring manager's 6-second scan—grab your pre-built .docx for {top_company} and claim your unfair advantage today.",
    ),
    (
        "Interview From Power, Never Need.",
        "{first_name}, the smartest executives test the market when they are already winning—that is how you command step-up scope and top-of-band offers. Challenge yourself with one stretch conversation at {top_company} today; one 60-second click can reprice your next three years.",
    ),
    (
        "The Market Promotes Faster Than HR.",
        "Loyalty rarely compounds as fast as mobility, {first_name}: every major jump in authority comes from stepping into a bigger arena. I surfaced {highlight_count} target move(s) built for where you're going next, not just where you've been—let's put your name at the top of the stack.",
    ),
    (
        "One Surgical Shot Beats Stagnation.",
        "Getting stuck happens one 'maybe tomorrow' at a time, {first_name}. We took all the friction out of the game—your top targets led by {top_company} are ranked below and your CVs are ready in Gmail. Hit send on one before your first meeting.",
    ),
]


def call_gemini_daily_motivation(prompt_text, model="gemini-2.5-flash", api_key=None):
    """Call Gemini for a fresh daily 'Us vs. The System' career motivation note."""
    api_key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen_cfg = {
        "temperature": 0.85,
        "maxOutputTokens": 320,
        "responseMimeType": "application/json",
    }
    if model.startswith("gemini-2.5"):
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    payload = {
        "systemInstruction": {"parts": [{"text": DAILY_MOTIVATION_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": gen_cfg,
    }
    body = http_json(url, payload, retries=1, headers={"x-goog-api-key": api_key}, timeout=25)
    parts = (((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
    raw = "\n".join(text_parts).strip()
    if not raw:
        return None
    parsed = parse_llm_json(raw)
    if isinstance(parsed, dict) and parsed.get("headline") and parsed.get("message"):
        return str(parsed["headline"]).strip(), str(parsed["message"]).strip()
    return None


def generate_daily_insider_note(sub_cfg, top_job, upgrade_count, hot_count, total_count, today):
    """Generate a daily 'Us vs. The System' career-velocity headline & message via Gemini,
    falling back seamlessly to the curated `DAILY_INSIDER_VAULT` when offline."""
    sub_name = (sub_cfg or {}).get("name") or "Candidate"
    first_name = sub_name.split()[0]
    top_co = (top_job or {}).get("company") or "a top employer"
    hi_cnt = upgrade_count if upgrade_count > 0 else max(1, total_count)
    day_idx = today.toordinal()

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if api_key:
        theme = DAILY_MOTIVATION_THEMES[day_idx % len(DAILY_MOTIVATION_THEMES)]
        model = os.environ.get("GEMINI_MODEL") or (sub_cfg or {}).get("llm_model", "gemini-2.5-flash")
        prompt = (
            f"Date: {today.isoformat()}\n"
            f"Candidate First Name: {first_name}\n"
            f"Today's Angle: {theme}\n"
            f"Radar Stats Today: {upgrade_count} Career Upgrade Moves, {hot_count} Hot (0-2d) openings, "
            f"{total_count} total curated matches. Top spotlight company: {top_co}.\n"
            "Write today's fresh 'Us Against the System' career-move briefing."
        )
        try:
            res = call_gemini_daily_motivation(prompt, model=model, api_key=api_key)
            if res:
                return res
        except Exception:
            pass

    h_tmpl, m_tmpl = DAILY_INSIDER_VAULT[day_idx % len(DAILY_INSIDER_VAULT)]
    return (
        h_tmpl.format(first_name=first_name, top_company=top_co, highlight_count=hi_cnt),
        m_tmpl.format(first_name=first_name, top_company=top_co, highlight_count=hi_cnt),
    )


def _render_job_card_html(j, today):
    """Render an email-safe, ultra-clean executive job card (using presentation tables so
    alignment stays razor-sharp in Gmail Web, iOS, Android, Apple Mail, and Outlook)."""
    days, is_hot, age_label = job_age_info(j, today)
    loc_tag = html.escape(LOC_LABELS.get(j.get("loc_class"), j.get("loc_class", "")))
    title_esc = html.escape(j.get("title", "Untitled Role"))
    company_esc = html.escape(j.get("company", ""))
    loc_esc = html.escape(j.get("location") or loc_tag)
    team_esc = html.escape(j.get("team", ""))
    url_esc = html.escape(j.get("url", "#"))
    age_esc = html.escape(age_label)

    analysis = j.get("llm_analysis") or {}
    is_upgrade = bool(j.get("is_career_upgrade") or analysis.get("is_career_upgrade"))
    upgrade_reason = (j.get("upgrade_reason") or analysis.get("upgrade_reason") or "").strip()

    meta_parts = [f"<strong style='color:#18181b;'>{company_esc}</strong>", loc_esc]
    if team_esc:
        meta_parts.append(team_esc)
    meta_parts.append(age_esc)
    meta_html = " &nbsp;&middot;&nbsp; ".join(meta_parts)

    badges = []
    if is_upgrade:
        badges.append(
            "<span style='display:inline-block;background-color:#09090b;color:#ffffff;"
            "font-size:10px;font-weight:700;padding:3px 8px;border-radius:4px;"
            "letter-spacing:0.6px;margin:0 6px 4px 0;text-transform:uppercase;'>"
            "&uarr; Career Upgrade Move</span>"
        )
    if is_hot:
        badges.append(
            f"<span style='display:inline-block;background-color:#fef3c7;color:#92400e;"
            f"border:1px solid #fde68a;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;"
            f"letter-spacing:0.4px;margin:0 6px 4px 0;'>&#128293; HOT &middot; {age_esc.upper()}</span>"
        )
    elif j.get("is_new"):
        badges.append(
            "<span style='display:inline-block;background-color:#f4f4f5;color:#27272a;"
            "font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px;"
            "margin:0 6px 4px 0;'>NEW</span>"
        )

    score = analysis.get("match_score")
    score_int = int(score) if isinstance(score, (int, float)) else None
    if score_int is not None:
        badges.append(
            f"<span style='display:inline-block;background-color:#ecfdf5;color:#065f46;"
            f"border:1px solid #a7f3d0;font-size:10px;font-weight:700;padding:2px 8px;"
            f"border-radius:4px;margin:0 6px 4px 0;'>&#9733; {score_int}% CV Match</span>"
        )

    upgrade_html = (
        f"<div style='font-size:12.5px;color:#047857;font-weight:600;margin:6px 0 4px;line-height:1.4;'>"
        f"&uarr; {html.escape(upgrade_reason)}</div>"
        if is_upgrade and upgrade_reason
        else ""
    )

    llm_box_html = ""
    if analysis:
        why_esc = html.escape(analysis.get("lucy_why_it_fits", ""))
        cv_obj = analysis.get("cv") or {}
        dir_txt = cv_obj.get("target_direction") or infer_target_direction(
            j, None, cv_obj.get("core_skills") or []
        )
        dir_esc = html.escape(dir_txt)
        kw_pills = "".join(
            f"<span style='display:inline-block;background-color:#ffffff;border:1px solid #e4e4e7;"
            f"color:#3f3f46;font-size:10.5px;padding:1px 6px;border-radius:4px;margin:2px 4px 2px 0;'>"
            f"{html.escape(k)}</span>"
            for k in (analysis.get("matched_keywords") or [])[:6]
        )
        att_name = html.escape(j.get("cv_filename", "Tailored_CV.docx"))
        bar_pct = max(50, min(100, score_int or 90))
        meter_html = (
            f"<div style='background-color:#e4e4e7;height:4px;border-radius:999px;overflow:hidden;margin:5px 0 8px;'>"
            f"<div style='background-color:#10b981;width:{bar_pct}%;height:4px;border-radius:999px;'></div>"
            f"</div>"
        )
        dir_line = (
            f"<div style='font-size:11.5px;color:#0f766e;font-weight:700;margin-bottom:4px;'>"
            f"{dir_esc}</div>"
            if dir_esc else ""
        )
        kw_line = f"<div style='margin-top:6px;'>{kw_pills}</div>" if kw_pills else ""
        llm_box_html = f"""
      <div style="background-color:#fafafa;border:1px solid #e4e4e7;border-left:3px solid #10b981;border-radius:6px;padding:10px 13px;margin-top:10px;font-size:12.5px;line-height:1.5;color:#27272a;">
        <div style="font-size:11.5px;font-weight:700;color:#09090b;letter-spacing:0.1px;">&#10024; Lucy&rsquo;s Headhunter Take &amp; Tailored CV</div>
        {meter_html}
        {dir_line}
        <div style="color:#27272a;">{why_esc}</div>
        {kw_line}
        <div style="margin-top:8px;padding-top:6px;border-top:1px solid #e4e4e7;font-size:11.5px;color:#0f766e;font-weight:700;">
          &#128206; ATS-Armed &amp; Attached: <code style="background-color:#ffffff;border:1px solid #e4e4e7;color:#09090b;padding:1px 6px;border-radius:4px;font-size:11px;">{att_name}</code>
        </div>
      </div>"""

    card_style = (
        "border:1.5px solid #18181b;border-left:4px solid #09090b;background-color:#ffffff;border-radius:10px;padding:15px 17px;margin-bottom:12px;"
        if is_upgrade
        else "border:1px solid #e4e4e7;background-color:#ffffff;border-radius:10px;padding:13px 16px;margin-bottom:9px;"
    )
    btn_style = (
        "display:inline-block;background-color:#09090b;color:#ffffff;padding:7px 13px;border-radius:6px;font-size:12px;font-weight:700;text-decoration:none;white-space:nowrap;"
        if is_upgrade
        else "display:inline-block;background-color:#ecfdf5;color:#065f46;border:1px solid #a7f3d0;padding:6px 12px;border-radius:6px;font-size:12px;font-weight:700;text-decoration:none;white-space:nowrap;"
    )
    badge_row = f"<div style='margin-bottom:5px;'>{''.join(badges)}</div>" if badges else ""

    return f"""
    <div style="{card_style}">
      {badge_row}
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
        <tr>
          <td valign="top" style="padding-right:12px;">
            <div style="font-size:15.5px;font-weight:700;color:#09090b;line-height:1.35;">
              <a href="{url_esc}" style="color:#09090b;text-decoration:none;">{title_esc}</a>
            </div>
            <div style="font-size:12.5px;color:#71717a;margin-top:3px;line-height:1.4;">
              {meta_html}
            </div>
          </td>
          <td valign="top" align="right" width="96" style="white-space:nowrap;">
            <a href="{url_esc}" style="{btn_style}">Apply &rarr;</a>
          </td>
        </tr>
      </table>
      {upgrade_html}
      {llm_box_html}
    </div>"""


def build_email_message(jobs_to_send, all_jobs_count, sub_cfg, today, sender_email, attachments=None):
    """Construct an editorial-grade, minimalist executive daily briefing with a daily GenAI
    'Us Against the System' career-move manifesto and visual dopamine cues."""
    recipient = sub_cfg.get("recipient_email", "lucasschollmatter@gmail.com")
    sub_name = sub_cfg.get("name") or recipient
    first_name = (sub_name or "there").split()[0]
    date_str = today.isoformat()
    attachments = attachments or []

    # Ensure every job has career-upgrade classification
    classify_career_upgrades(jobs_to_send, sub_cfg)

    # Sort: Career Upgrade Moves first, then HOT (0-2d), then newest, then company
    enriched = []
    for j in jobs_to_send:
        days, is_hot, age_label = job_age_info(j, today)
        is_up = bool(j.get("is_career_upgrade") or (j.get("llm_analysis") or {}).get("is_career_upgrade"))
        j["is_career_upgrade"] = is_up
        enriched.append((not is_up, not is_hot, days, j, is_hot, age_label, is_up))
    enriched.sort(key=lambda item: (item[0], item[1], item[2], item[3].get("company", "").lower()))

    upgrade_jobs = [item[3] for item in enriched if item[6]]
    hot_jobs = [item[3] for item in enriched if item[4]]
    standard_jobs = [item[3] for item in enriched if not item[6]]
    top_job = enriched[0][3] if enriched else None

    insider_headline, insider_message = generate_daily_insider_note(
        sub_cfg, top_job, len(upgrade_jobs), len(hot_jobs), len(jobs_to_send), today
    )

    msg = EmailMessage()
    hot_prefix = f"🔥 {len(hot_jobs)} HOT · " if hot_jobs else "⚡ "
    up_part = f"{len(upgrade_jobs)} Career Upgrade Move(s) · " if upgrade_jobs else ""
    cv_suffix = f" + {len(attachments)} Tailored CV(s)" if attachments else ""
    msg["Subject"] = (
        f"{hot_prefix}Lucy: {up_part}{len(jobs_to_send)} Relevant Opening(s){cv_suffix} for {sub_name} ({date_str})"
    )
    msg["From"] = f"Lucy · Personal Headhunter <{sender_email}>"
    msg["To"] = recipient

    # Minimalist plain-text version (preserves '- {title} at {company}' format)
    txt_lines = [
        f"{sub_name} — Lucy Private Talent Briefing ({date_str})",
        "Lucy, your personal headhunter — your daily reminder to check where to apply today.",
        "",
        f"⚡ {insider_headline}",
        insider_message,
        "",
        f"{len(upgrade_jobs)} Career Upgrade Move(s) · {len(hot_jobs)} Hot (0-2d) · {len(jobs_to_send)} shown ({all_jobs_count} active matches)",
        "",
    ]
    for _, _, _, j, is_hot, age_label, is_up in enriched:
        loc_tag = LOC_LABELS.get(j.get("loc_class"), j.get("loc_class", ""))
        tags = []
        if is_up:
            tags.append("[CAREER UPGRADE MOVE]")
        if is_hot:
            tags.append("[HOT]")
        elif j.get("is_new"):
            tags.append("[NEW]")
        prefix = (" ".join(tags) + " ") if tags else ""
        txt_lines.append(f"- {prefix}{j['title']} at {j['company']}")
        txt_lines.append(f"  {j.get('location') or loc_tag} · {age_label} · {j.get('url')}")
        if is_up and j.get("upgrade_reason"):
            txt_lines.append(f"  Upgrade: {j['upgrade_reason']}")
        if j.get("llm_analysis"):
            an = j["llm_analysis"]
            cv_obj = an.get("cv") or {}
            if cv_obj.get("target_direction"):
                txt_lines.append(f"  {cv_obj['target_direction']}")
            txt_lines.append(f"  Fit ({an.get('match_score', '?')}% Match): {an.get('lucy_why_it_fits', '')}")
            if j.get("cv_filename"):
                txt_lines.append(f"  Attached CV: {j['cv_filename']}")
        txt_lines.append("")
    txt_lines.append("🎯 60-Second Execution Play: Pick 1 role above, grab your pre-tailored .docx CV, and hit submit.")
    msg.set_content("\n".join(txt_lines))

    # Minimalist HTML sections
    sections_html = []
    if upgrade_jobs:
        up_cards = "".join(_render_job_card_html(j, today) for j in upgrade_jobs)
        sections_html.append(f"""
        <div style="margin-bottom:24px;">
          <div style="font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:1.1px;color:#09090b;border-bottom:2px solid #09090b;padding-bottom:6px;margin-bottom:12px;">
            &#9889; Career Upgrade Moves ({len(upgrade_jobs)}) &middot; Step-Up Targets
          </div>
          {up_cards}
        </div>""")

    if standard_jobs:
        std_heading = (
            "Hot Off the Press &amp; Additional Matches"
            if hot_jobs
            else "Additional Curated Matches"
        )
        std_cards = "".join(_render_job_card_html(j, today) for j in standard_jobs)
        sections_html.append(f"""
        <div style="margin-bottom:16px;">
          <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1.1px;color:#71717a;border-bottom:1px solid #e4e4e7;padding-bottom:6px;margin-bottom:12px;">
            {std_heading} ({len(standard_jobs)})
          </div>
          {std_cards}
        </div>""")
    elif hot_jobs:
        # Ensure "Hot Off the Press" marker is present when all roles are upgrades and hot
        sections_html.append(
            "<div style='font-size:11px;color:#71717a;margin:0 0 12px;'>Hot Off the Press: includes roles posted in the last 1&ndash;2 days.</div>"
        )

    first_name_esc = html.escape(first_name)
    headline_esc = html.escape(insider_headline)
    message_esc = html.escape(insider_message)
    preheader_esc = html.escape(f"⚡ {insider_headline} — {insider_message}")

    up_color = "#059669" if upgrade_jobs else "#18181b"
    hot_color = "#d97706" if hot_jobs else "#18181b"
    cv_color = "#0f766e" if attachments else "#71717a"

    html_body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lucy &middot; Daily Briefing</title>
</head>
<body style="margin:0;padding:28px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#18181b;background-color:#f4f4f5;-webkit-font-smoothing:antialiased;">
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{preheader_esc}</div>

  <div style="max-width:640px;margin:0 auto;background-color:#ffffff;border:1px solid #e4e4e7;border-radius:14px;padding:30px 28px 24px;">

    <!-- Editorial Masthead -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin-bottom:10px;">
      <tr>
        <td align="left" style="font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:1.3px;color:#059669;">
          &#10022; Lucy &middot; Personal Headhunter
        </td>
        <td align="right" style="font-size:11.5px;font-weight:600;color:#71717a;">
          {date_str}
        </td>
      </tr>
    </table>

    <h1 style="margin:0 0 4px;font-size:23px;font-weight:800;letter-spacing:-0.5px;color:#09090b;line-height:1.25;">
      Good morning, {first_name_esc}.
    </h1>
    <div style="font-size:13px;color:#52525b;margin-bottom:16px;line-height:1.45;">
      Your daily reminder to check where to apply today &mdash; Career Upgrade Moves ranked first.
    </div>

    <!-- Dark Obsidian 'Us Against the System' Daily Manifesto Card -->
    <div style="background-color:#09090b;color:#fafafa;border-radius:11px;padding:18px 20px;margin-bottom:16px;">
      <div style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.4px;color:#34d399;margin-bottom:6px;">
        &#9889; Today&rsquo;s Edge &middot; Us vs. The System
      </div>
      <div style="font-size:16px;font-weight:800;color:#ffffff;letter-spacing:-0.2px;margin-bottom:6px;line-height:1.3;">
        {headline_esc}
      </div>
      <div style="font-size:13px;color:#d4d4d8;line-height:1.55;">
        {message_esc}
      </div>
    </div>

    <!-- 4-Tile Minimalist Scoreboard Strip -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate;border-spacing:0;background-color:#fafafa;border:1px solid #e4e4e7;border-radius:10px;margin-bottom:22px;text-align:center;">
      <tr>
        <td width="25%" style="padding:10px 6px;border-right:1px solid #e4e4e7;">
          <div style="font-size:17px;font-weight:800;color:{up_color};line-height:1.1;">{len(upgrade_jobs)}</div>
          <div style="font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:0.6px;color:#71717a;margin-top:3px;">Upgrade Moves</div>
        </td>
        <td width="25%" style="padding:10px 6px;border-right:1px solid #e4e4e7;">
          <div style="font-size:17px;font-weight:800;color:{hot_color};line-height:1.1;">{len(hot_jobs)}</div>
          <div style="font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:0.6px;color:#71717a;margin-top:3px;">Hot (0&ndash;2d)</div>
        </td>
        <td width="25%" style="padding:10px 6px;border-right:1px solid #e4e4e7;">
          <div style="font-size:17px;font-weight:800;color:{cv_color};line-height:1.1;">{len(attachments)}</div>
          <div style="font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:0.6px;color:#71717a;margin-top:3px;">Ready CVs</div>
        </td>
        <td width="25%" style="padding:10px 6px;">
          <div style="font-size:17px;font-weight:800;color:#18181b;line-height:1.1;">{len(jobs_to_send)}</div>
          <div style="font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:0.6px;color:#71717a;margin-top:3px;">Today ({all_jobs_count} Active)</div>
        </td>
      </tr>
    </table>

    {''.join(sections_html)}

    <!-- 60-Second Execution Play Footer -->
    <div style="margin-top:18px;padding:14px 16px;background-color:#fafafa;border:1px solid #e4e4e7;border-radius:9px;font-size:12px;color:#27272a;line-height:1.5;">
      <strong style="color:#09090b;">&#127919; The 60-Second Play:</strong> Pick <strong>1 role</strong> above &rarr; preview or download your attached <code style="background-color:#ffffff;border:1px solid #e4e4e7;padding:1px 5px;border-radius:4px;font-size:11px;">CV_*.docx</code> right inside Gmail &rarr; submit before your next meeting. Comfort stays put; movers get paid.
    </div>
  </div>
</body>
</html>"""
    msg.add_alternative(html_body, subtype="html")

    for filename, file_bytes in attachments:
        lower_fn = filename.lower()
        if lower_fn.endswith(".docx"):
            maintype = "application"
            subtype = "vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif lower_fn.endswith(".pdf"):
            maintype = "application"
            subtype = "pdf"
        else:
            maintype = "text"
            subtype = "html"
        msg.add_attachment(
            file_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
    return msg


def _format_cv_markdown_preview(cv_data):
    """Render a clean Markdown preview of a tailored CV for the GitHub Actions Step Summary."""
    lines = [f"#### {cv_data.get('full_name', 'Candidate')}"]
    if cv_data.get("target_direction"):
        lines.append(f"**{cv_data.get('target_direction')}**  ")
    lines.extend([
        f"**{cv_data.get('target_headline', '')}**  ",
        f"*{cv_data.get('contact_line', '')}*",
        "",
        f"**Executive Summary:** {cv_data.get('summary', '')}",
        "",
    ])
    skills = [s for s in (cv_data.get("core_skills") or []) if s]
    if skills:
        lines.append("**Core Competencies:** " + " · ".join(f"`{s}`" for s in skills))
        lines.append("")
    for exp in cv_data.get("experience") or []:
        lines.append(f"**{exp.get('role', '')}** — *{exp.get('company', '')}* ({exp.get('dates', '')})")
        for b in exp.get("bullets") or []:
            if b:
                lines.append(f"- {b}")
        lines.append("")
    return "\n".join(lines).strip()


def prepare_tailored_cvs(jobs_to_send, sub_cfg, today, root=ROOT):
    """Run LLM analysis + ATS-ready CV generation for up to `max_tailored_cvs` top jobs.

    Saves each generated CV in `.docx` (Microsoft Word OpenXML), `.pdf`, and `.html` formats
    under both `out/cvs/` (workflow artifact) and `docs/cvs/<sub_id>/` (committed to the
    repository), attaches the ATS-ready `.docx` (or configured `cv_attachment_format`) to the
    email so it previews natively in Gmail, and appends a preview to the GitHub Actions summary."""
    sub_name = sub_cfg.get("name") or sub_cfg.get("id", "Candidate")
    sub_id = sub_cfg.get("id", "default")
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    max_cvs = int(sub_cfg.get("max_tailored_cvs", 3))
    att_fmt = str(sub_cfg.get("cv_attachment_format") or "docx").lower().lstrip(".")
    if max_cvs <= 0 or not jobs_to_send:
        return []
    if not api_key:
        print(f"  [{sub_name}] Tailored CV skipped: GEMINI_API_KEY environment variable not set.")
        return []

    master_cv = load_subscriber_cv(sub_cfg, root=root)
    if not master_cv:
        print(f"  [{sub_name}] Tailored CV skipped: no master CV found (check cv_file in config.json).")
        return []

    classify_career_upgrades(jobs_to_send, sub_cfg, root=root)

    # Prioritize Career Upgrade Moves first, then HOT (0-2d) roles
    ranked = sorted(
        jobs_to_send,
        key=lambda j: (
            not bool(j.get("is_career_upgrade")),
            not job_age_info(j, today)[1],
            job_age_info(j, today)[0],
            j.get("company", "").lower(),
        ),
    )

    attachments = []
    cv_out_dir = Path(root) / "out" / "cvs"
    cv_docs_dir = Path(root) / "docs" / "cvs" / sub_id
    cv_out_dir.mkdir(parents=True, exist_ok=True)
    cv_docs_dir.mkdir(parents=True, exist_ok=True)

    summary_blocks = []
    for j in ranked[:max_cvs]:
        try:
            analysis = tailor_cv_with_llm(j, master_cv, sub_cfg)
            if not analysis or not isinstance(analysis.get("cv"), dict):
                continue
            cv_obj = analysis["cv"]
            if not cv_obj.get("target_direction"):
                cv_obj["target_direction"] = infer_target_direction(
                    j, sub_cfg, cv_obj.get("core_skills") or []
                )
            if "is_career_upgrade" in analysis:
                j["is_career_upgrade"] = bool(analysis["is_career_upgrade"])
            if analysis.get("upgrade_reason"):
                j["upgrade_reason"] = str(analysis["upgrade_reason"]).strip()

            docx_name = cv_attachment_filename(sub_name, j, ext="docx")
            pdf_name = cv_attachment_filename(sub_name, j, ext="pdf")
            html_name = cv_attachment_filename(sub_name, j, ext="html")

            docx_bytes = render_minimalist_cv_docx(cv_obj, job_item=j)
            pdf_bytes = render_minimalist_cv_pdf(cv_obj, job_item=j)
            html_bytes = render_minimalist_cv_html(cv_obj, job_item=j).encode("utf-8")

            for fname, fbytes in ((docx_name, docx_bytes), (pdf_name, pdf_bytes), (html_name, html_bytes)):
                (cv_out_dir / fname).write_bytes(fbytes)
                (cv_docs_dir / fname).write_bytes(fbytes)

            if att_fmt == "pdf":
                att_filename, att_bytes = pdf_name, pdf_bytes
            elif att_fmt == "html":
                att_filename, att_bytes = html_name, html_bytes
            else:
                att_filename, att_bytes = docx_name, docx_bytes

            j["llm_analysis"] = analysis
            j["cv_filename"] = att_filename
            attachments.append((att_filename, att_bytes))
            score = analysis.get("match_score", "?")
            up_badge = " [CAREER UPGRADE MOVE]" if j.get("is_career_upgrade") else ""
            print(f"  [{sub_name}] Tailored CV generated for {j['title']} at {j['company']}{up_badge} ({score}% match) -> docs/cvs/{sub_id}/{att_filename}")
            kws = ", ".join(analysis.get("matched_keywords") or [])
            summary_blocks.append(
                f"- **{j.get('title')}** at **{j.get('company')}** ({j.get('location', '')}){up_badge} — "
                f"**{score}% CV Match** · saved to `docs/cvs/{sub_id}/{att_filename}`\n"
                f"  - *Target Role Direction:* {cv_obj.get('target_direction', '')}\n"
                f"  - *Lucy's take:* {analysis.get('lucy_why_it_fits', '')}\n"
                + (f"  - *Career upgrade rationale:* {j.get('upgrade_reason')}\n" if j.get("upgrade_reason") else "")
                + (f"  - *Matched keywords:* {kws}\n" if kws else "")
                + f"  <details><summary>Preview tailored CV for {j.get('company')}</summary>\n\n"
                + _format_cv_markdown_preview(cv_obj)
                + "\n\n  </details>"
            )
        except Exception as e:
            gha_annotate("warning", f"LLM CV tailoring failed for {sub_name} ({j.get('title')}): {type(e).__name__}: {e}")

    if summary_blocks:
        gha_summary(f"#### Tailored CVs for {sub_name} (`{sub_id}`)\n" + "\n".join(summary_blocks))

    return attachments


def send_email_digest(jobs_to_send, all_jobs_count, sub_cfg, today, root=ROOT, attachments=None):
    """Send a single subscriber's daily digest via Gmail/SMTP if credentials are configured."""
    sub_name = sub_cfg.get("name") or sub_cfg.get("recipient_email", "subscriber")
    if not jobs_to_send:
        print(f"  [{sub_name}] No new roles to email today.")
        return False

    if attachments is None:
        attachments = prepare_tailored_cvs(jobs_to_send, sub_cfg, today, root=root)

    smtp_user = os.environ.get("GMAIL_USER") or os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("SMTP_PASSWORD")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

    if not smtp_user or not smtp_password:
        print(f"  [{sub_name}] Email skipped: GMAIL_USER / GMAIL_APP_PASSWORD environment variables not set.")
        return False

    msg = build_email_message(jobs_to_send, all_jobs_count, sub_cfg, today, smtp_user, attachments=attachments)
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=25) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)

    print(f"  [{sub_name}] Sent email digest ({len(jobs_to_send)} roles, {len(attachments)} tailored CVs) to {msg['To']}.")
    return True


def process_subscribers(fetched, state, cfg, today, send_email=True, only_id=None, root=ROOT):
    """Filter fetched jobs for each active subscriber, update their personal seen
    state in state['subscribers'][sub_id], and send each subscriber's email.

    Returns the list of subscriber ids whose email delivery raised. For those
    subscribers the per-subscriber state is rolled back to what it was before
    this run, so the same roles are re-discovered (and re-emailed) next run
    instead of being silently lost."""
    subscribers = get_subscribers(cfg, only_id=only_id)
    sub_states = state.setdefault("subscribers", {})
    retention = int(cfg["state_retention_days"])
    failed_ids = []

    if send_email and not (os.environ.get("GEMINI_API_KEY") or "").strip():
        gha_annotate(
            "warning",
            "GEMINI_API_KEY is not set in environment/secrets — LLM tailored CVs were skipped. "
            "Add GEMINI_API_KEY in GitHub Settings > Secrets and variables > Actions.",
        )

    for sub_cfg in subscribers:
        sub_id = sub_cfg["id"]
        sub_rules = build_rules(sub_cfg)
        sub_matched = list({j["key"]: copy.deepcopy(j) for j in select(fetched, sub_rules, sub_cfg)}.values())

        previous_state = copy.deepcopy(sub_states.get(sub_id))
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
            # Generate tailored CVs for roles being emailed today (or top active matches
            # if no new roles were discovered today, so docs/cvs/<sub_id>/ stays populated).
            cv_targets = to_email if to_email else [j for j in sub_matched if not j.get("stale")]
            attachments = prepare_tailored_cvs(cv_targets, sub_cfg, today, root=root)
            try:
                send_email_digest(to_email, len(sub_matched), sub_cfg, today, root=root, attachments=attachments)
            except Exception as e:
                gha_annotate("error", f"email delivery failed for {sub_id}: {type(e).__name__}: {e}")
                failed_ids.append(sub_id)
                if previous_state is None:
                    sub_states.pop(sub_id, None)
                else:
                    sub_states[sub_id] = previous_state
    return failed_ids


def strip_transient_fields(jobs):
    """Remove heavy job description / detail_api fields before writing static JSON files."""
    clean = []
    for j in jobs:
         item = {k: v for k, v in j.items() if k not in ("description", "detail_api", "llm_analysis", "cv_filename", "is_career_upgrade", "upgrade_reason")}
         clean.append(item)
    return clean


def write_outputs(root, cfg, jobs, state, today, stamp):
    root = Path(root)
    clean_jobs = strip_transient_fields(jobs)
    write_json(root / "state" / "seen.json", state)
    write_json(root / "state" / "jobs.json", clean_jobs)
    write_json(root / "docs" / "jobs.json", clean_jobs)
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "index.html").write_text(render_page(clean_jobs, cfg, stamp), encoding="utf-8")
    (docs / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    alert = root / "out" / "new_jobs.md"
    alert.parent.mkdir(parents=True, exist_ok=True)
    fresh = [j for j in clean_jobs if j.get("discovered_now")]
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

    # Process per-subscriber filtering, LLM CV tailoring, state updates, and email digests
    email_failures = process_subscribers(fetched, state, cfg, today, send_email=send_email,
                                         only_id=subscriber_id, root=root)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fresh = write_outputs(root, cfg, jobs, state, today, stamp)
    ok = len(results) - len(failed)
    n_new = sum(1 for j in jobs if j["is_new"])
    print(f"Fetched {ok}/{len(results)} boards. {len(jobs)} matching roles on main board, "
          f"{n_new} marked NEW, {len(fresh)} first seen this run.")
    print(f"Page: {root / 'docs' / 'index.html'}")

    summary = [f"### Jobs Sentinel · {stamp}",
               f"- Boards fetched: **{ok}/{len(results)}**"
               + (f" (failed: {', '.join(c['name'] for c, _ in failed)})" if failed else ""),
               f"- Matching roles: **{len(jobs)}** · NEW: **{n_new}** · first seen this run: **{len(fresh)}**"]
    if email_failures:
        summary.append(f"- :x: Email delivery failed for: **{', '.join(email_failures)}** "
                       "(their state was rolled back; they will be retried next run)")
    gha_summary("\n".join(summary))
    if failed:
        gha_annotate("warning", f"{len(failed)} board(s) failed: "
                     + "; ".join(f"{c['name']}: {err}" for c, err in failed))
    # Exit 2 = outputs were written, but at least one digest could not be delivered.
    return 2 if email_failures else 0


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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
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
