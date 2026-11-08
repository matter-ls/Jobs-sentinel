#!/usr/bin/env python3
# Jobs Sentinel - Multi-Subscriber Daily Digest + LLM CV Headhunter
"""
job-radar: pulls public job feeds (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Workday, Oracle HCM, Eightfold), keeps matching roles per
subscriber profile, builds a static page at docs/index.html, optionally calls
Gemini LLM to compare top job postings against each subscriber's master CV to
generate a tailored minimalist CV, and emails personalized daily digests.

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
import gzip
import html
import http.client
import io
import json
import os
import random
import re
import smtplib
import ssl
import sys
import textwrap
import time
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parent
UA = "job-radar/1.0 (personal job board)"

DEFAULT_LLM_MODEL = "gemini-3.8-flash"
FALLBACK_LLM_MODELS = ("gemini-3.8-flash", "gemini-flash-latest", "gemini-3.8-flash-lite")

CFG_DEFAULTS = {
    "page_title": "Openings",
    "recipient_email": "lucasschollmatter@gmail.com",
    "email_on_baseline": True,
    "email_mode": "new_or_discovered",
    "daily_top_when_quiet": False,
    "quiet_spotlight_count": 3,
    "llm_model": DEFAULT_LLM_MODEL,
    "max_tailored_cvs": 3,
    "cv_attachment_format": "docx",
    "include_remote_mexico": True,
    "include_country_only": True,
    "new_days": 7,
    "max_workers": 16,
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
6. ATS PORTAL AUTO-FILL COMPLIANCE (CRITICAL): Enterprise job portals (Workday, Oracle HCM, Taleo, SuccessFactors, iCIMS) reject CVs whose text contains any of these 8 characters: < > [ ] " { } \\. NEVER use < > [ ] " { } \\ anywhere inside `cv` text fields. Write "under 5-minute" instead of "<5-minute", "over 90%" instead of ">90%", standard parentheses ( ) instead of [ ] or { }, "/" instead of "\\", and plain words without double quotes.
7. Write in the language of the job posting (default to English if the posting is in English or mixed).

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
    "target_direction": "Target Role Direction: General Role Level and Functional Domain Area Fulfilling the Target Job",
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
    """ISO string or epoch (seconds or milliseconds) -> 'YYYY-MM-DD', or None."""
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)):
            ts = v / 1000 if v > 100_000_000_000 else v
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
        m = re.match(r"\d{4}-\d{2}-\d{2}", str(v))
        return m.group(0) if m else None


def _safe_iso_date(val, fallback):
    """Parse YYYY-MM-DD safely without raising on malformed strings."""
    if not val:
        return fallback
    try:
        return date.fromisoformat(str(val)[:10])
    except (ValueError, TypeError):
        return fallback


def load_json(path, default):
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(default, dict) and not isinstance(parsed, dict):
            return copy.deepcopy(default)
        if isinstance(default, list) and not isinstance(parsed, list):
            return copy.deepcopy(default)
        return parsed
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def write_json(path, data):
    """Atomically write JSON via a sibling temp file + os.replace so state is never truncated."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


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
    default_terms = ["Guadalajara", "Zapopan", "Jalisco", "El Salto", "Tlaquepaque", "Tonala"]
    seen = {t.lower(): t for t in default_terms}
    for sub in get_subscribers(cfg):
        for term in sub.get("workday_search_terms") or []:
            if term.lower() not in seen:
                seen[term.lower()] = term
    return list(seen.values())


def http_json(url, payload=None, retries=2, headers=None, timeout=25):
    """GET (or POST when payload is given) and parse JSON. Retries 429/5xx/network/TLS resets."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    err = None
    in_test = "unittest" in sys.modules
    for attempt in range(retries + 1):
        retry_wait = None
        try:
            req = request.Request(url, data=data, headers=hdrs)
            with request.urlopen(req, timeout=timeout) as r:
                raw_bytes = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower() if hasattr(r, "headers") and r.headers else ""
                if enc == "gzip" or raw_bytes[:2] == b"\x1f\x8b":
                    raw_bytes = gzip.decompress(raw_bytes)
                return json.loads(raw_bytes.decode("utf-8-sig", errors="replace"))
        except error.HTTPError as e:
            if e.code < 500 and e.code not in (408, 425, 429):
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300].strip()
                    if detail:
                        e.msg = f"{e.msg} — {detail}"
                except Exception:
                    pass
                raise
            if e.headers:
                ra = e.headers.get("Retry-After")
                if ra and str(ra).strip().isdigit():
                    retry_wait = min(10.0, max(1.0, float(ra)))
            err = e
        except (
            error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            http.client.HTTPException,
            ssl.SSLError,
            OSError,
        ) as e:
            err = e
        if attempt < retries:
            if not in_test:
                delay = retry_wait if retry_wait is not None else min(10.0, 1.5 * (attempt + 1) + random.uniform(0.1, 0.5))
                time.sleep(delay)
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


def _clean_workday_location(loc_str):
    """Normalize Workday ERP location codes like 'MEX.Zapopan' -> 'Zapopan, Mexico'."""
    s = re.sub(r"\s+", " ", str(loc_str or "")).strip()
    m = re.match(r"^MEX\.(.+)$", s, re.I)
    if m:
        city = m.group(1).replace(".", ", ").strip(" ,")
        return f"{city}, Mexico" if "mexico" not in city.lower() else city
    return s


def _dedupe_location_segments(raw_locs):
    """Join distinct location segments while dropping redundant bare 'Mexico' segments when
    another segment already names a specific Mexican city/state (e.g. 'Mexico City, Mexico; Mexico')."""
    if isinstance(raw_locs, str):
        items = re.split(r"[;|]", raw_locs)
    else:
        items = raw_locs or []
    cleaned = []
    for raw in items:
        s = _clean_workday_location(raw).strip(" ,;")
        if s and s not in cleaned:
            cleaned.append(s)
    if len(cleaned) > 1:
        has_specific_mx = any(
            re.search(r"\b(mexico|mx)\b", norm(seg))
            and not re.fullmatch(r"\W*(mexico|mx)\W*", norm(seg))
            and not re.fullmatch(r"\W*(remote|remoto|remota|home office|teletrabajo)\W*", norm(seg))
            for seg in cleaned
        )
        if has_specific_mx:
            cleaned = [seg for seg in cleaned if not re.fullmatch(r"\W*(mexico|mx)\W*", norm(seg))]
    return "; ".join(cleaned)


def job(c, jid, title, location, url, posted=None, team="", description="", detail_api=""):
    clean_title = re.sub(r"\s+", " ", (title or "")).replace("( ", "(").replace(" )", ")").strip()
    clean_loc = _dedupe_location_segments(location or "")
    out = {
        "key": f"{c['ats']}:{c['name']}:{jid}",
        "company": c["name"],
        "ats": c["ats"],
        "title": clean_title,
        "location": clean_loc,
        "team": re.sub(r"\s+", " ", (team or "")).strip(),
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
    items = (d.get("jobs") if isinstance(d, dict) else None) or []
    return [
        job(c, j["id"], j.get("title"), (j.get("location") or {}).get("name"),
            j.get("absolute_url"), j.get("first_published"),
            description=j.get("content") or "")
        for j in items
        if isinstance(j, dict) and j.get("id") not in (None, "")
    ]


def fetch_lever(c):
    host = "api.eu.lever.co" if c.get("region") == "eu" else "api.lever.co"
    d = http_json(f"https://{host}/v0/postings/{c['board']}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        if not isinstance(j, dict) or not j.get("id"):
            continue
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
    for j in (d.get("jobs") if isinstance(d, dict) else None) or []:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        locs = [j.get("location")]
        for s in j.get("secondaryLocations") or []:
            locs.append(s.get("location") if isinstance(s, dict) else s)
        a = (j.get("address") or {}).get("postalAddress") or {}
        locs.append(", ".join(x for x in [a.get("addressLocality"), a.get("addressRegion"),
                                          a.get("addressCountry")] if x))
        loc = _dedupe_location_segments(locs)
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
    for j in (d.get("jobs") if isinstance(d, dict) else None) or []:
        if not isinstance(j, dict) or not (j.get("shortcode") or j.get("code")):
            continue
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
        items = (d.get("content") if isinstance(d, dict) else None) or []
        for j in items:
            if not isinstance(j, dict) or not j.get("id"):
                continue
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
        if not items or offset >= ((d.get("totalFound") if isinstance(d, dict) else 0) or 0):
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


def _fetch_workday_site(c, site, today, terms):
    host = f"{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    api = f"https://{host}/wday/cxs/{c['tenant']}/{site}/jobs"
    found = {}
    for term in terms:
        offset, total = 0, None
        while offset < 600:
            d = http_json(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                                "searchText": term})
            if not isinstance(d, dict):
                break
            posts = d.get("jobPostings") or []
            if total is None:  # Workday only reports a reliable total on the first page
                total = d.get("total") or 0
            for p in posts:
                if not isinstance(p, dict):
                    continue
                loc = (p.get("locationsText") or "").strip()
                is_multi = bool(re.fullmatch(r"\d+\s+locations?", loc, re.I))
                path = p.get("externalPath", "")
                bullets = [b for b in (p.get("bulletFields") or []) if b]
                ref = str(bullets[0]) if bullets else path
                if not ref:
                    continue
                detail_api = f"https://{host}/wday/cxs/{c['tenant']}/{site}{path}" if path else ""
                if ref in found:
                    # If already seen under an ambiguous stemmer term like 'El Salto', upgrade to explicit city term
                    prev_term = found[ref].get("_wd_multi_term", "")
                    if prev_term and norm(prev_term) in ("el salto", "salto") and norm(term) not in ("el salto", "salto"):
                        found[ref]["_wd_multi_term"] = term
                    continue
                item = job(
                    c, ref, p.get("title"), loc, f"https://{host}/en-US/{site}{path}",
                    workday_posted(p.get("postedOn") or "", today),
                    detail_api=detail_api,
                )
                if is_multi:
                    item["_wd_multi_term"] = term
                found[ref] = item
            offset += 20
            if not posts or offset >= total:
                break
    return list(found.values())


def fetch_workday(c, today=None, extra_terms=None):
    today = today or today_utc()
    default_terms = extra_terms or ["Guadalajara", "Zapopan", "Jalisco", "El Salto", "Tlaquepaque", "Tonala"]
    terms = c.get("search_terms", default_terms)
    sites = [c["site"]] + list(c.get("fallback_sites") or [])
    last_err = None
    for idx, site in enumerate(sites):
        try:
            return _fetch_workday_site(c, site, today, terms)
        except error.HTTPError as e:
            last_err = e
            if e.code in (400, 404, 422) and idx + 1 < len(sites):
                continue
            raise
    raise last_err


def _fetch_oracle_hcm_site(c, site, terms):
    host = c["host"]
    found = {}
    for term in terms:
        offset = 0
        while offset < 300:
            finder = f"findReqs;siteNumber={site},keyword={parse.quote(term)},limit=25,offset={offset},sortBy=POSTING_DATES_DESC"
            url = (
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
            )
            d = http_json(url)
            items = d.get("items") or []
            reqs = items[0].get("requisitionList") or [] if items and isinstance(items[0], dict) else []
            total = (items[0].get("TotalJobsCount") if items and isinstance(items[0], dict) else 0) or 0
            for r in reqs:
                jid = str(r.get("Id") or "").strip()
                if not jid:
                    continue
                locs = [r.get("PrimaryLocation") or ""]
                for s in r.get("secondaryLocations") or []:
                    if isinstance(s, dict) and s.get("Name"):
                        locs.append(s["Name"])
                loc = _dedupe_location_segments(locs)
                wtype = f"{r.get('WorkplaceType') or ''} {r.get('WorkplaceTypeCode') or ''}".lower()
                if "remote" in wtype and "remote" not in loc.lower():
                    loc = f"{loc} (Remote)".strip()
                detail_api = (
                    f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                    f"?onlyData=true&finder=ById;Id=%22{parse.quote(jid)}%22,siteNumber={site}"
                )
                job_url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}"
                found.setdefault(jid, job(
                    c, jid, r.get("Title"), loc, job_url, r.get("PostedDate"),
                    team=r.get("Department") or r.get("JobFamily") or "",
                    description=r.get("ShortDescriptionStr") or "",
                    detail_api=detail_api,
                ))
            offset += 25
            if not reqs or (total and offset >= total):
                break
    return list(found.values())


def fetch_oracle_hcm(c):
    terms = c.get("search_terms") or ["Guadalajara", "Zapopan", "Jalisco", "Mexico"]
    sites = [c["site"]] + list(c.get("fallback_sites") or [])
    last_err = None
    for idx, site in enumerate(sites):
        try:
            return _fetch_oracle_hcm_site(c, site, terms)
        except error.HTTPError as e:
            last_err = e
            if e.code in (400, 404, 422) and idx + 1 < len(sites):
                continue
            raise
    raise last_err


def _fetch_eightfold_legacy(c, host, domain, loc_query):
    found, offset = {}, 0
    while offset < 200:
        url = (
            f"https://{host}/api/apply/v2/jobs?domain={parse.quote(domain)}"
            f"&location={parse.quote(loc_query)}&start={offset}&num=20"
        )
        d = http_json(url)
        positions = d.get("positions") or []
        total = d.get("count") or 0
        for p in positions:
            jid = str(p.get("id") or p.get("ats_job_id") or "").strip()
            if not jid:
                continue
            raw_locs = p.get("locations")
            locs = [str(x) for x in raw_locs if x] if isinstance(raw_locs, list) else ([str(p["location"])] if p.get("location") else [])
            loc = "; ".join(dict.fromkeys(locs))
            pos_url = p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{jid}"
            found.setdefault(jid, job(
                c, jid, p.get("name") or p.get("title"), loc, pos_url,
                p.get("t_create") or p.get("postedTs"),
                team=p.get("department") or "",
                description=p.get("job_description") or "",
            ))
        offset += max(len(positions), 20)
        if not positions or (total and offset >= total):
            break
    return list(found.values())


def fetch_eightfold(c):
    host = c["host"]
    domain = c["domain"]
    loc_query = c.get("location_query", "Mexico")
    found, offset = {}, 0
    try:
        while offset < 200:
            if offset > 0:
                time.sleep(0.25)
            url = (
                f"https://{host}/api/pcsx/search?domain={parse.quote(domain)}"
                f"&query=&location={parse.quote(loc_query)}&start={offset}"
            )
            d = http_json(url, retries=3)
            data = d.get("data") if isinstance(d.get("data"), dict) else d
            positions = (data.get("positions") if isinstance(data, dict) else None) or []
            total = (data.get("count") if isinstance(data, dict) else 0) or 0
            for p in positions:
                jid = str(p.get("id") or p.get("positionId") or p.get("atsJobId") or "").strip()
                if not jid:
                    continue
                raw_locs = p.get("locations")
                if isinstance(raw_locs, list):
                    locs = [str(x) for x in raw_locs if x]
                elif p.get("location"):
                    locs = [str(p["location"])]
                else:
                    locs = []
                loc = "; ".join(dict.fromkeys(locs))
                if str(p.get("workLocationOption") or "").lower() == "remote" and "remote" not in loc.lower():
                    loc = f"{loc} (Remote)".strip()
                pos_url = p.get("canonicalPositionUrl") or p.get("positionUrl") or ""
                if pos_url.startswith("/"):
                    pos_url = f"https://{host}{pos_url}"
                elif not pos_url:
                    pos_url = f"https://{host}/careers/job/{jid}"
                detail_api = (
                    f"https://{host}/api/pcsx/position_details"
                    f"?position_id={parse.quote(jid)}&domain={parse.quote(domain)}"
                )
                found.setdefault(jid, job(
                    c, jid, p.get("name") or p.get("title"), loc, pos_url,
                    p.get("postedTs") or p.get("t_create"),
                    team=p.get("department") or "",
                    detail_api=detail_api,
                ))
            page_step = max(len(positions), 10)
            offset += page_step
            if not positions or (total and offset >= total):
                break
        return list(found.values())
    except error.HTTPError as e:
        if e.code in (403, 404) and offset == 0:
            return _fetch_eightfold_legacy(c, host, domain, loc_query)
        raise


def fetch_bamboohr(c):
    board = c["board"]
    d = http_json(f"https://{board}.bamboohr.com/careers/list")
    if not isinstance(d, dict) or not isinstance(d.get("result"), list):
        raise ValueError(f"BambooHR board '{board}' did not return JSON job list")
    out = []
    for j in d.get("result") or []:
        jid = str(j.get("id") or "").strip()
        if not jid:
            continue
        loc_obj = j.get("location") if isinstance(j.get("location"), dict) else {}
        ats_loc = j.get("atsLocation") if isinstance(j.get("atsLocation"), dict) else {}
        city = loc_obj.get("city") or ats_loc.get("city")
        state = loc_obj.get("state") or ats_loc.get("state") or ats_loc.get("province")
        country = ats_loc.get("country") or loc_obj.get("country") or c.get("default_country", "")
        loc = ", ".join(dict.fromkeys(str(x).strip() for x in (city, state, country) if x and str(x).strip()))
        is_remote = bool(j.get("isRemote")) or str(j.get("locationType") or "") == "1"
        if is_remote and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip() if loc else f"Remote - {c.get('default_country', 'Mexico')}"
        out.append(job(
            c, jid, j.get("jobOpeningName") or j.get("title"), loc,
            f"https://{board}.bamboohr.com/careers/{jid}",
            j.get("datePosted"),
            team=j.get("departmentLabel") or "",
            detail_api=f"https://{board}.bamboohr.com/careers/{jid}/detail",
        ))
    return out


def enrich_job_description(j):
    """Fetch full job description on-demand for Workday / SmartRecruiters / Oracle HCM / Eightfold / BambooHR."""
    if j.get("description") and len(j["description"]) > 300:
        return j["description"]
    detail_api = j.get("detail_api")
    if not detail_api:
        return j.get("description") or ""
    try:
        d = http_json(detail_api, retries=1)
        if not isinstance(d, dict):
            return j.get("description") or ""
        if "jobPostingInfo" in d:
            desc = (d.get("jobPostingInfo") or {}).get("jobDescription") or ""
        elif "jobAd" in d:
            sections = ((d.get("jobAd") or {}).get("sections") or {}).values()
            desc = " ".join(s.get("text", "") for s in sections if isinstance(s, dict))
        elif isinstance(d.get("items"), list) and d["items"]:
            it = d["items"][0] if isinstance(d["items"][0], dict) else {}
            desc = " ".join(
                str(it.get(k) or "")
                for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")
            )
        elif isinstance(d.get("data"), dict) and "jobDescription" in d["data"]:
            desc = d["data"].get("jobDescription") or ""
        elif isinstance(d.get("result"), dict) and isinstance(d["result"].get("jobOpening"), dict):
            desc = d["result"]["jobOpening"].get("description") or ""
        else:
            desc = d.get("description") or d.get("content") or ""
        cleaned = strip_html(desc)[:12000]
        if cleaned:
            j["description"] = cleaned
        return j.get("description") or ""
    except Exception:
        return j.get("description") or ""


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "oracle_hcm": fetch_oracle_hcm,
    "eightfold": fetch_eightfold,
    "bamboohr": fetch_bamboohr,
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

def _compile_patterns(patterns, label="regex"):
    compiled = []
    for p in patterns or []:
        try:
            compiled.append(re.compile(p, re.I))
        except re.error as e:
            gha_annotate("warning", f"Skipping invalid {label} pattern {p!r}: {e}")
    return compiled


def build_rules(cfg):
    locs = cfg.get("locations") or cfg.get("jalisco_locations") or []
    jal = "|".join(re.escape(norm(x)) for x in locs if x)
    return {
        "inc": _compile_patterns(cfg.get("title_include") or [], "title_include"),
        "exc": _compile_patterns(cfg.get("title_exclude") or [], "title_exclude"),
        "jal": re.compile(rf"\b({jal})\b") if jal else re.compile(r"(?!x)x"),
    }


def is_director(title, rules):
    t = norm(title)
    return any(r.search(t) for r in rules["inc"]) and not any(r.search(t) for r in rules["exc"])


NON_MX_FOREIGN_RE = re.compile(
    r"\b(united states|usa|\bus\b|canada|brazil|brasil|argentina|colombia|chile|peru|"
    r"uruguay|costa rica|puerto rico|panama|spain|espana|india|poland|romania|germany|"
    r"united kingdom|\buk\b|ireland|france|italy|portugal|philippines|china|japan|"
    r"singapore|australia|israel|utah|salt lake|california|texas|colorado|washington|"
    r"oregon|new york|massachusetts|illinois|georgia|florida|north carolina|virginia)\b"
)

STRONG_JALISCO_RE = re.compile(
    r"\b(guadalajara|zapopan|tlaquepaque|tlajomulco|jalisco|tonala|tepatitlan|"
    r"lagos de moreno|puerto vallarta|ciudad guzman|ixtlahuacan|juanacatlan|zapotlanejo)\b"
)

OTHER_MX_CITIES_RE = re.compile(
    r"\b(mexico city|ciudad de mexico|\bcdmx\b|distrito federal|monterrey|nuevo leon|"
    r"san pedro garza|queretaro|tijuana|mexicali|baja california|puebla|ciudad juarez|"
    r"chihuahua|saltillo|coahuila|hermosillo|sonora|merida|yucatan|aguascalientes|"
    r"san luis potosi|toluca|naucalpan|ecatepec|nezahualcoyotl|tlalnepantla|"
    r"guanajuato|silao|celaya|irapuato|cancun|quintana roo|reynosa|matamoros|tampico|"
    r"torreon|culiacan|sinaloa|veracruz|oaxaca|morelia|michoacan|cuernavaca)\b"
)

REMOTE_TOKEN_RE = re.compile(r"\b(remote|remoto|remota|home office|teletrabajo)\b")
MEXICO_TOKEN_RE = re.compile(r"\b(mexico|mex|mx)\b")


def location_class(loc, rules, cfg):
    """'jalisco' | 'remote_mx' | 'mx_country' | None"""
    t = norm(loc)
    if not t:
        return None
    segments = [seg.strip() for seg in re.split(r"[;|]", t) if seg.strip()]

    # 1. Jalisco check per segment: strip 'salt lake (city)' first and guard ambiguous tokens
    #    (like 'el salto' or 'jal') against explicit foreign-country segments (e.g. Salto, Brazil; Jal, NM, USA).
    for seg in segments:
        seg_clean = re.sub(r"\bsalt\s+lake(?:\s+city)?\b", " ", seg)
        if rules["jal"].search(seg_clean):
            if STRONG_JALISCO_RE.search(seg_clean) or MEXICO_TOKEN_RE.search(seg_clean) or not NON_MX_FOREIGN_RE.search(seg_clean):
                return "jalisco"

    # 2. Remote-Mexico check: require 'remote' and 'mexico' in the same segment, OR a standalone
    #    generic 'Remote' segment paired with a 'Mexico' segment (never 'United States - Remote; Mexico City').
    if cfg["include_remote_mexico"]:
        same_seg_remote_mx = any(REMOTE_TOKEN_RE.search(seg) and re.search(r"\b(mexico|mx)\b", seg) for seg in segments)
        standalone_remote_with_mx = (
            any(re.fullmatch(r"\W*(remote|remoto|remota|home office|teletrabajo)\W*", seg) for seg in segments)
            and any(re.search(r"\b(mexico|mx)\b", seg) for seg in segments)
        )
        if same_seg_remote_mx or standalone_remote_with_mx:
            return "remote_mx"

    # 3. Country-only Mexico ('Mexico · city not listed'): only when NO non-Jalisco Mexican city is specified.
    if cfg["include_country_only"]:
        if not any(OTHER_MX_CITIES_RE.search(seg) for seg in segments):
            if any(re.fullmatch(r"\W*(mexico|mx)\W*", seg) for seg in segments):
                return "mx_country"
    return None


def resolve_workday_multi_locations(jobs, title_rule_sets, global_rules, global_cfg):
    """Verify real cities via Workday's `detail_api` (`jobPostingInfo`) for postings whose list
    feed only reports `'2 Locations'` / `'3 Locations'`, preventing Workday's fuzzy full-text
    stemmer from turning `'Salt Lake City, UT'` into `'El Salto'`."""
    candidates = [
        j for j in jobs
        if j.get("_wd_multi_term")
        and (title_rule_sets is None or any(is_director(j.get("title", ""), r) for r in title_rule_sets))
    ]
    if not candidates:
        return

    def _resolve_one(j):
        term = j.get("_wd_multi_term", "")
        detail_api = j.get("detail_api") or ""
        if detail_api:
            try:
                d = http_json(detail_api, retries=1, timeout=12)
                if isinstance(d, dict) and isinstance(d.get("jobPostingInfo"), dict):
                    jpi = d["jobPostingInfo"]
                    desc = strip_html(jpi.get("jobDescription") or "")[:12000]
                    if desc and not j.get("description"):
                        j["description"] = desc
                    raw_locs = [jpi.get("location")] + list(jpi.get("additionalLocations") or [])
                    cleaned = []
                    for rl in raw_locs:
                        cl = _clean_workday_location(rl)
                        if cl and cl not in cleaned:
                            cleaned.append(cl)
                    if cleaned:
                        matched_locs = [cl for cl in cleaned if location_class(cl, global_rules, global_cfg)]
                        if matched_locs:
                            others = len(cleaned) - len(matched_locs)
                            base_loc = "; ".join(matched_locs[:2])
                            suffix = f" (+{others} other location{'s' if others > 1 else ''})" if others > 0 else ""
                            j["location"] = f"{base_loc}{suffix}"
                        else:
                            # Real locations contain zero Jalisco/Mexico matches (e.g. Salt Lake City, UT)
                            j["location"] = "; ".join(cleaned[:5])
                        j.pop("_wd_multi_term", None)
                        return
            except Exception:
                pass

        # Fallback when detail_api is unreachable or mocked offline without jobPostingInfo:
        # fail-closed on stemmer-prone 'El Salto' or explicit US URL paths.
        url_low = (j.get("url") or "").lower()
        if norm(term) in ("el salto", "salto") or "/job/usa-" in url_low or "/job/us-" in url_low or "salt-lake" in url_low:
            j["location"] = "Multiple Locations"
        elif term:
            j["location"] = f"{j.get('location', 'Multiple Locations')} ({term})"
        j.pop("_wd_multi_term", None)

    with ThreadPoolExecutor(max_workers=min(16, max(1, len(candidates)))) as ex:
        list(ex.map(_resolve_one, candidates))


def dedupe_subscriber_jobs(jobs):
    """Collapse duplicate postings sharing (company, normalized title) for a subscriber
    (e.g. remote roles cloned across 18 Mexican cities on Lever, or multiple identical plant
    requisitions on Workday), preferring Jalisco > Remote-MX > MX-Country and newest date."""
    loc_rank = {"jalisco": 0, "remote_mx": 1, "mx_country": 2}
    grouped = {}
    for j in jobs:
        co_k = norm(j.get("company", "")).strip()
        title_k = re.sub(r"[^a-z0-9]+", " ", norm(j.get("title", ""))).strip()
        team_k = norm(j.get("team", "")).strip()
        grouped.setdefault((co_k, title_k, team_k), []).append(j)

    deduped = []
    for group in grouped.values():
        if len(group) == 1:
            deduped.append(group[0])
            continue
        group.sort(
            key=lambda x: (
                loc_rank.get(x.get("loc_class"), 9),
                "" if x.get("posted_at") else "z",
                -(int(x["posted_at"].replace("-", "")) if x.get("posted_at") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x["posted_at"]) else 0),
                x.get("key", ""),
            )
        )
        best = copy.deepcopy(group[0])
        # Only treat the collapsed group as newly discovered today if none of its clones was already known before today
        best["discovered_now"] = all(bool(g.get("discovered_now")) for g in group)
        if best.get("loc_class") == "remote_mx" and len({norm(g.get("location", "")) for g in group}) >= 3:
            best["location"] = "Remote - Mexico"
        deduped.append(best)
    return deduped


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
    if not isinstance(state.get("jobs"), dict):
        state["jobs"] = {}
    seen = state["jobs"]
    baseline = not state.get("initialized")
    for j in jobs:
        rec = seen.get(j["key"])
        if not isinstance(rec, dict):
            rec = None
        j["discovered_now"] = rec is None and not baseline
        if rec is None:
            rec = seen[j["key"]] = {"first_seen": today.isoformat(), "baseline": baseline}
        rec["last_seen"] = today.isoformat()
        j["first_seen"] = rec.get("first_seen") or today.isoformat()
        j["baseline"] = bool(rec.get("baseline", False))
    for k in list(seen):
        rec_k = seen.get(k)
        if not isinstance(rec_k, dict):
            del seen[k]
            continue
        last = _safe_iso_date(rec_k.get("last_seen") or rec_k.get("first_seen"), today)
        if (today - last).days > retention_days:
            del seen[k]
    state["initialized"] = True
    return baseline


def flag_new(j, today, new_days):
    cutoff = today - timedelta(days=new_days)
    if j.get("posted_at"):
        return _safe_iso_date(j["posted_at"], cutoff - timedelta(days=1)) >= cutoff
    return (not j.get("baseline")) and _safe_iso_date(j.get("first_seen"), today) >= cutoff


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


_DEAD_GEMINI_MODELS = set()
_DEAD_GEMINI_API_KEYS = set()
_DISCOVERED_GEMINI_MODELS = None


def _normalize_gemini_model(m):
    return re.sub(r"^models/", "", str(m or "").strip())


def _discover_gemini_flash_models(api_key):
    """Query /v1beta/models once per run if hardcoded model IDs are ever retired (HTTP 404)."""
    global _DISCOVERED_GEMINI_MODELS
    if _DISCOVERED_GEMINI_MODELS is not None:
        return _DISCOVERED_GEMINI_MODELS
    try:
        d = http_json(
            "https://generativelanguage.googleapis.com/v1beta/models",
            retries=1,
            headers={"x-goog-api-key": api_key},
            timeout=15,
        )
        found = []
        skip_tokens = ("tts", "image", "audio", "embedding", "live", "vision", "veo", "imagen")
        for item in d.get("models") or []:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            short = _normalize_gemini_model(item.get("name", ""))
            low = short.lower()
            if not low.startswith("gemini-") or "flash" not in low:
                continue
            if any(tok in low for tok in skip_tokens):
                continue
            found.append(short)

        def _rank(name):
            m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
            maj = int(m.group(1)) if m else 0
            min_v = int(m.group(2) or 0) if m else 0
            not_lite = 0 if "lite" in name else 1
            not_preview = 0 if ("preview" in name or "exp" in name) else 1
            return (maj, min_v, not_lite, not_preview)

        found.sort(key=_rank, reverse=True)
        _DISCOVERED_GEMINI_MODELS = found[:4]
    except Exception:
        _DISCOVERED_GEMINI_MODELS = []
    return _DISCOVERED_GEMINI_MODELS


def _invoke_gemini_json(
    prompt_text,
    system_text=None,
    model=DEFAULT_LLM_MODEL,
    api_key=None,
    temperature=0.2,
    max_output_tokens=4096,
    timeout=60,
    retries=2,
    validator=None,
):
    """Call Gemini generateContent across primary + fallback + auto-discovered Flash models."""
    api_key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key or api_key in _DEAD_GEMINI_API_KEYS:
        return None

    primary = _normalize_gemini_model(model) or DEFAULT_LLM_MODEL
    models_to_try = []
    for cand in (primary,) + FALLBACK_LLM_MODELS:
        c_norm = _normalize_gemini_model(cand)
        if c_norm and c_norm not in models_to_try and c_norm not in _DEAD_GEMINI_MODELS:
            models_to_try.append(c_norm)
    if not models_to_try:
        models_to_try = [DEFAULT_LLM_MODEL]

    last_err = None
    discovered_added = False
    idx = 0
    while idx < len(models_to_try):
        if api_key in _DEAD_GEMINI_API_KEYS:
            break
        m_name = models_to_try[idx]
        idx += 1
        if m_name in _DEAD_GEMINI_MODELS:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
        gen_cfg = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        }
        if m_name.startswith("gemini-3"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "low"}
        elif m_name.startswith("gemini-2.5"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": gen_cfg,
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        try:
            try:
                body = http_json(url, payload, retries=retries, headers={"x-goog-api-key": api_key}, timeout=timeout)
            except error.HTTPError as e:
                if e.code in (401, 403) or (e.code == 400 and "api_key" in str(e).lower()):
                    _DEAD_GEMINI_API_KEYS.add(api_key)
                    raise
                if e.code == 400 and "thinkingConfig" in payload["generationConfig"]:
                    payload["generationConfig"].pop("thinkingConfig", None)
                    body = http_json(url, payload, retries=1, headers={"x-goog-api-key": api_key}, timeout=timeout)
                elif e.code == 404:
                    _DEAD_GEMINI_MODELS.add(m_name)
                    if not discovered_added and idx >= len(models_to_try):
                        discovered_added = True
                        for dyn_m in _discover_gemini_flash_models(api_key):
                            if dyn_m not in models_to_try and dyn_m not in _DEAD_GEMINI_MODELS:
                                models_to_try.append(dyn_m)
                    raise
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
            if validator and not validator(parsed):
                raise ValueError(f"Gemini ({m_name}) JSON failed schema check")
            return parsed
        except Exception as e:
            last_err = e
            print(f"  [gemini] model {m_name} attempt failed: {type(e).__name__}: {e}", file=sys.stderr)
    if last_err:
        raise last_err
    return None


def call_gemini_json(prompt_text, model=DEFAULT_LLM_MODEL, api_key=None):
    """Call Gemini generateContent endpoint and return parsed CV tailoring JSON object."""
    return _invoke_gemini_json(
        prompt_text,
        system_text=LLM_SYSTEM_PROMPT,
        model=model,
        api_key=api_key,
        temperature=0.2,
        max_output_tokens=4096,
        timeout=60,
        retries=2,
        validator=lambda p: isinstance(p, dict) and isinstance(p.get("cv"), dict),
    )


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


def _fix_ats_employer_confusion(text, company_name, job_title=""):
    """Ensure LLM-generated prose never refers to the hiring employer by its ATS software
    platform name (e.g. saying 'at Workday' when the hiring employer is 'Flex')."""
    if not text or not company_name:
        return text or ""
    s = str(text)
    co = str(company_name).strip()
    ats_platforms = ("Workday", "Greenhouse", "SmartRecruiters", "Eightfold", "BambooHR")
    for plat in ats_platforms:
        if plat.lower() in co.lower() or plat.lower() in str(job_title or "").lower():
            continue
        s = re.sub(rf"\b(at|for|join|joining|into|apply to|applying to)\s+{plat}\b", rf"\1 {co}", s, flags=re.I)
        s = re.sub(rf"\b{plat}(?:'s|’s)\b", f"{co}'s", s, flags=re.I)
    return s


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

    # Single compact batch prompt using integer IDs (never pass internal `ats:Company:id` keys,
    # so Gemini never confuses 'workday:Flex:...' with employer 'Workday'!).
    batch = jobs[:25]
    items_txt = "\n".join(
        f"- id: {idx} | title: {j.get('title', '')} | hiring_employer: {j.get('company', '')} | team: {j.get('team', '')}"
        for idx, j in enumerate(batch)
    )
    career_goal = (sub_cfg.get("career_goal") or "").strip()
    goal_line = f"CANDIDATE CAREER GOAL & DIRECTION: {career_goal}\n" if career_goal else ""
    prompt = (
        "Evaluate the following job openings against the candidate's MASTER CV and career goal.\n"
        "Identify which roles represent a 'Career Upgrade Move' (an upward step in seniority, "
        "closing/revenue ownership, leadership scope, architectural ownership, or high-growth strategic "
        "challenge aligned with the candidate's career direction, rather than a lateral or junior move).\n"
        "IMPORTANT: Always refer to each employer strictly by its exact `hiring_employer` name (never by an ATS portal name like Workday).\n\n"
        f"{goal_line}"
        'Return ONLY valid JSON matching: {"upgrades": [{"id": 0, "is_career_upgrade": true, "upgrade_reason": "1 concise sentence why this role at hiring_employer is a career upgrade move"}]}\n\n'
        f"JOBS:\n{items_txt}\n\n---\nCANDIDATE MASTER CV:\n{master_cv[:4000]}"
    )
    model = (os.environ.get("GEMINI_MODEL") or "").strip() or sub_cfg.get("llm_model", DEFAULT_LLM_MODEL)
    try:
        parsed = _invoke_gemini_json(
            prompt,
            model=model,
            api_key=api_key,
            temperature=0.1,
            max_output_tokens=2048,
            timeout=35,
            retries=1,
            validator=lambda p: isinstance(p, dict) and isinstance(p.get("upgrades"), list),
        )
        if parsed:
            for u in parsed.get("upgrades") or []:
                if not isinstance(u, dict):
                    continue
                target_job = None
                raw_id = u.get("id")
                if not isinstance(raw_id, bool) and isinstance(raw_id, (int, str)) and str(raw_id).isdigit():
                    idx_val = int(raw_id)
                    if 0 <= idx_val < len(batch):
                        target_job = batch[idx_val]
                elif u.get("key"):
                    target_job = next((b for b in batch if b.get("key") == u["key"]), None)
                if target_job is not None:
                    target_job["is_career_upgrade"] = bool(u.get("is_career_upgrade", target_job.get("is_career_upgrade")))
                    if u.get("upgrade_reason"):
                        target_job["upgrade_reason"] = _fix_ats_employer_confusion(
                            str(u["upgrade_reason"]).strip(),
                            target_job.get("company", ""),
                            target_job.get("title", ""),
                        )
    except Exception:
        # Heuristic labels are already applied on `jobs`
        pass


ATS_ILLEGAL_CHARS_RE = re.compile(r'[<>\[\]"{}\\]')


def sanitize_ats_text(text):
    """Scrub characters rejected by enterprise ATS auto-fill parsers (Workday, Oracle HCM,
    Taleo, SuccessFactors, iCIMS): < > [ ] " { } \\
    Preserves human readability by translating mathematical comparisons and brackets into
    clean prose/parentheses rather than blindly deleting meaning."""
    if not text:
        return ""
    s = html.unescape(str(text))
    # Strip any embedded HTML tags (e.g. from noisy scraped job titles like 'Director <b>x</b>')
    s = re.sub(r"</?[a-zA-Z][^>]{0,80}>", " ", s)
    # Convert ASCII / Unicode arrows before stripping '>'
    s = re.sub(r"\s*(?:->|=>|→|⟶)\s*", " - ", s)
    # Convert inequality operators before digits into natural executive prose
    s = re.sub(r"<=\s*(?=\$?\d)", "up to ", s)
    s = re.sub(r">=\s*(?=\$?\d)", "at least ", s)
    s = re.sub(r"<\s*(?=\$?\d)", "under ", s)
    s = re.sub(r">\s*(?=\$?\d)", "over ", s)
    s = s.replace("≤", "up to ").replace("≥", "at least ")
    # Map brackets & braces to ATS-safe parentheses and backslashes to forward slashes
    s = s.replace("[", "(").replace("]", ")")
    s = s.replace("{", "(").replace("}", ")")
    s = s.replace("\\", "/")
    # Strip double quotes (straight & typographic), angle quotes, backticks, and any leftover < or >
    for ch in ('"', "“", "”", "„", "«", "»", "`", "<", ">"):
        s = s.replace(ch, "")
    s = re.sub(r"\*+", "", s)
    # Normalize curly single quotes/apostrophes to straight ASCII apostrophe
    s = s.replace("’", "'").replace("‘", "'")
    # Strip ASCII control characters
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    # Collapse nested/empty parentheses created by bracket conversion and normalize whitespace
    s = re.sub(r"\(\s*\(([^()]*)\)\s*\)", r"(\1)", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sanitize_cv_data_for_ats(cv_data):
    """Return a deep-sanitized copy of `cv_data` guaranteed to contain zero ATS-illegal
    characters (< > [ ] " { } \\) across all header, summary, skill, experience,
    education, and language fields."""
    if not isinstance(cv_data, dict):
        return {}
    td = sanitize_ats_text(cv_data.get("target_direction") or "")
    td = re.sub(r"^(Target Role Direction:\s*)\(([^()]+)\)$", r"\1\2", td)

    clean_exp = []
    for exp in cv_data.get("experience") or []:
        if not isinstance(exp, dict):
            continue
        bullets = [sanitize_ats_text(b) for b in (exp.get("bullets") or [])]
        clean_exp.append({
            "company": sanitize_ats_text(exp.get("company") or ""),
            "role": sanitize_ats_text(exp.get("role") or ""),
            "location": sanitize_ats_text(exp.get("location") or ""),
            "dates": sanitize_ats_text(exp.get("dates") or ""),
            "bullets": [b for b in bullets if b],
        })

    clean_edu = []
    for edu in cv_data.get("education") or []:
        if not isinstance(edu, dict):
            continue
        deg = sanitize_ats_text(edu.get("degree") or "")
        inst = sanitize_ats_text(edu.get("institution") or "")
        dts = sanitize_ats_text(edu.get("dates") or "")
        if deg or inst:
            clean_edu.append({"degree": deg, "institution": inst, "dates": dts})

    clean_skills = [sanitize_ats_text(s) for s in (cv_data.get("core_skills") or [])]
    clean_langs = [sanitize_ats_text(s) for s in (cv_data.get("languages") or [])]

    out = dict(cv_data)
    out.update({
        "full_name": sanitize_ats_text(cv_data.get("full_name") or "") or "Candidate",
        "target_direction": td,
        "target_headline": sanitize_ats_text(cv_data.get("target_headline") or ""),
        "contact_line": sanitize_ats_text(cv_data.get("contact_line") or ""),
        "summary": sanitize_ats_text(cv_data.get("summary") or ""),
        "core_skills": [s for s in clean_skills if s],
        "experience": clean_exp,
        "education": clean_edu,
        "languages": [s for s in clean_langs if s],
    })
    return out


def infer_target_direction(job_item, sub_cfg=None, core_skills=None):
    """Craft a natural 'General Role Direction' headlight that communicates the broader type
    of role and functional domain the candidate is targeting, fulfilling the area of the job
    being applied to without mechanically copying internal requisition codes."""
    title = (job_item or {}).get("title", "")
    team = (job_item or {}).get("team", "")
    goal = (sub_cfg or {}).get("career_goal", "")
    title_team_blob = norm(f"{title} {team}")
    blob = norm(f"{title} {team} {goal} {' '.join(core_skills or [])}")

    sales_goal = "sales" in norm(goal)
    is_sales_role = bool(re.search(
        r"\b(sales|account executive|account manager|\bkam\b|ejecutivo de ventas|ejecutivo comercial|"
        r"ejecutivo de cuentas|gerente comercial|gerente de ventas|gerente de cuentas|"
        r"business development|\bbdr\b|\bsdr\b|demand generation|territory|"
        r"channel sales|partner sales|channel account|partner account|renewals?|ventas|comercial)\b",
        title_team_blob,
    )) or (sales_goal and bool(re.search(r"\b(channel|partner|alliances?|customer success|client success)\b", title_team_blob)))

    if re.search(r"\b(vp|vice[- ]?president(e|a)?|svp|evp|chief|general manager|gerente general|managing director|country (manager|lead|head))\b", title_team_blob):
        level = "Executive Leadership (VP / GM / Head of Function)"
    elif re.search(r"\b(director(a)?|head of)\b", title_team_blob):
        level = "Director / Head of Function"
    elif is_sales_role and re.search(r"\b(sales manager|gerente de ventas|gerente comercial|bdr manager|inside sales manager|team lead|lider comercial|lider de ventas|regional sales manager)\b", title_team_blob):
        level = "Sales Management & Commercial Team Leadership"
    elif is_sales_role:
        level = "Enterprise & Digital Sales (Account Executive / KAM / Territory Growth)"
    elif re.search(r"\b(senior manager|sr\.? manager|gerente senior|principal|architect|arquitecto|coe lead|practice lead|global lead)\b", title_team_blob):
        level = "Senior Manager / Principal & Functional Lead"
    elif re.search(r"\b(manager|gerente|lead|leader|lider)\b", title_team_blob):
        level = "Manager & Cross-Functional Delivery Lead"
    else:
        level = "Senior Operations & Domain Specialist"

    if is_sales_role:
        domain = "B2B Enterprise Sales, Account Acquisition, Pipeline Conversion & LATAM Territory Growth"
    elif re.search(r"\b(organizational change|change management|change enablement|digital adoption|customer experience|partner experience|\bcx\b)\b", blob):
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
    elif re.search(r"\b(program|project|pmo|agile|scrum|delivery|portfolio|proyectos)\b", blob):
        domain = "Strategic Program Management (PMO), Agile Delivery & Portfolio Execution"
    else:
        top_skills = " · ".join((core_skills or ["Enterprise Operations", "Strategy", "Execution"])[:3])
        domain = top_skills

    return sanitize_ats_text(f"Target Role Direction: {level} — {domain}")


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
    cv_payload = sanitize_cv_data_for_ats({
        "full_name": full_name,
        "target_direction": target_direction,
        "target_headline": f"{job_title} | {' · '.join(core_skills[:3])}",
        "contact_line": contact_line,
        "summary": summary,
        "core_skills": core_skills,
        "experience": experience,
        "education": education[:3],
        "languages": languages[:4],
    })
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
        "cv": cv_payload,
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

    career_goal = (sub_cfg.get("career_goal") or "").strip()
    goal_section = (
        f"CANDIDATE CAREER GOAL & DIRECTION (align `target_direction`, `summary`, `core_skills`, and bullet emphasis truthfully toward this trajectory):\n{career_goal}\n\n---\n"
        if career_goal
        else ""
    )
    co_name = job_item.get("company", "")
    job_title = job_item.get("title", "")
    prompt = (
        f"TARGET JOB POSTING:\n"
        f"Title: {job_title}\n"
        f"Hiring Employer (Company Name): {co_name}\n"
        f"(IMPORTANT: The hiring employer is '{co_name}'. Never confuse '{co_name}' with its recruiting portal/ATS system such as Workday, Greenhouse, Lever, SmartRecruiters, Oracle HCM, or Eightfold.)\n"
        f"Location: {job_item.get('location', '')}\n"
        f"Team: {job_item.get('team', '')}\n"
        f"Job Description:\n{jd_text}\n\n"
        f"---\n"
        f"{goal_section}"
        f"CANDIDATE MASTER CV:\n{master_cv_text}"
    )
    model = (os.environ.get("GEMINI_MODEL") or "").strip() or sub_cfg.get("llm_model", DEFAULT_LLM_MODEL)
    try:
        res = call_gemini_json(prompt, model=model, api_key=api_key)
        if res and isinstance(res.get("cv"), dict):
            if res.get("lucy_why_it_fits"):
                res["lucy_why_it_fits"] = _fix_ats_employer_confusion(res["lucy_why_it_fits"], co_name, job_title)
            if res.get("upgrade_reason"):
                res["upgrade_reason"] = _fix_ats_employer_confusion(res["upgrade_reason"], co_name, job_title)
            if res["cv"].get("summary"):
                res["cv"]["summary"] = _fix_ats_employer_confusion(res["cv"]["summary"], co_name, job_title)
            cv_obj = sanitize_cv_data_for_ats(res["cv"])
            if not cv_obj.get("target_direction"):
                cv_obj["target_direction"] = infer_target_direction(
                    job_item, sub_cfg, cv_obj.get("core_skills") or []
                )
            res["cv"] = cv_obj
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
    cv_data = sanitize_cv_data_for_ats(cv_data or {})
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
    cv_data = sanitize_cv_data_for_ats(cv_data or {})

    def pdf_str(s):
        # Map common typography punctuation to ATS-safe Latin-1 equivalents for Type1 Helvetica
        s = (
            str(s or "")
            .replace("—", " - ")
            .replace("–", "-")
            .replace("•", "*")
            .replace("·", "|")
            .replace("→", " - ")
            .replace("“", "")
            .replace("”", "")
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
    cv_data = sanitize_cv_data_for_ats(cv_data or {})
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
        target_note = (
            f"Tailored by Lucy for: {esc(sanitize_ats_text(job_item.get('title', '')))} "
            f"at {esc(sanitize_ats_text(job_item.get('company', '')))}"
        )

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


def select_jobs_for_email(jobs, baseline, cfg, sub_state=None):
    """Choose which jobs to include in a subscriber's daily email digest.

    When `daily_top_when_quiet` is enabled and zero brand-new roles were discovered
    overnight, rotates through up to `quiet_spotlight_count` active non-stale matches
    (prioritizing Career Upgrade Moves not spotlighted in recent quiet runs) so every
    morning delivers a fresh dopamine & career-momentum briefing."""
    mode = cfg.get("email_mode", "new_or_discovered")
    if mode == "all":
        return [j for j in jobs if not j.get("stale")]
    if baseline:
        if cfg.get("email_on_baseline", True):
            return [j for j in jobs if not j.get("stale")]
        return []
    fresh = [j for j in jobs if j.get("discovered_now")]
    if fresh or mode == "discovered_only" or not cfg.get("daily_top_when_quiet", False):
        return fresh

    active = [j for j in jobs if not j.get("stale")]
    if not active:
        return []
    rel = (sub_state or {}).get("relationship") or {}
    recent_spotlights = set((rel.get("recent_spotlight_keys") or [])[-9:])
    ranked = sorted(
        active,
        key=lambda j: (
            j.get("key") in recent_spotlights,
            not is_career_upgrade_heuristic(j, cfg)[0],
            j.get("company", "").lower(),
        ),
    )
    limit = max(1, int(cfg.get("quiet_spotlight_count", 3)))
    chosen = []
    for j in ranked[:limit]:
        item = copy.deepcopy(j)
        item["quiet_spotlight"] = True
        chosen.append(item)
    return chosen


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


DAILY_MOTIVATION_SYSTEM_PROMPT = """You are Lucy, an elite, sharp-witted private executive headhunter and career co-conspirator writing a personalized 45-second morning briefing to your star client.

Your Core Philosophy ('Us Against the System' & 'Daily Dopamine + Connection'):
1. LIBERATE DOPAMINE THROUGH ASYMMETRIC MOMENTUM: Every morning must feel fresh, electric, and energizing—never repetitive. Remind them that strategic career moves are the #1 proven wealth and scope multiplier (25-50% market repricing vs. 3-4% 'loyalty tax' raises).
2. BUILD REAL HUMAN-AGENT CONNECTION: Speak like a trusted insider partner who worked overnight on their behalf—scanning over 100 live career boards, filtering out 99% of noise, and arming custom ATS-ready Word CVs so they walk into the market with an unfair advantage.
3. PERSONALIZE TO THEIR CRAFT: Ground your note naturally in their real career identity, today's day-of-week rhythm, and today's spotlight opportunity (using the exact `Spotlight Hiring Employer` name, never confusing an ATS platform like Workday with the employer)—make them feel seen, valued, and unstoppable.
4. ZERO REPETITION: NEVER reuse any phrase, metaphor, or hook listed in `RECENT_HEADLINES_TO_AVOID`. Surprise them with a fresh angle every single day.

Return ONLY valid JSON with these exact 5 keys:
{
  "subject_spark": "3-5 word curiosity/dopamine hook for the Gmail subject line (no quotes, punchy, fresh)",
  "headline": "4-8 word bold manifesto headline for today's dark card",
  "message": "2-3 sharp, energizing sentences (45-70 words) addressing the candidate by first name, connecting today's angle + their real craft + top company into an immediate confidence and dopamine boost.",
  "connection_ps": "1 warm, insider partnership sentence (18-30 words) from Lucy reinforcing your bond—referencing what you prepared overnight, your briefing streak/edition together, and why you have their back.",
  "micro_challenge": "1 concrete, zero-friction 60-second action (15-25 words) that triggers instant completion dopamine right now."
}"""

WEEKDAY_CADENCE = [
    ("Monday Launchpad", "New week, clean slate, asymmetric leverage"),
    ("Tuesday Peak Window", "Tuesday mornings hit peak recruiter inbox velocity"),
    ("Wednesday Pattern Interrupt", "Midweek check-in: invest 60 seconds in your own equity"),
    ("Thursday Market Repricing", "Know your true external clearing price before Friday"),
    ("Friday Future-Self Play", "Pay your future self one bold shot before the weekend"),
    ("Saturday Quiet Sniper", "Zero inbox noise today — land at the top of Monday's stack"),
    ("Sunday Strategic Ambush", "Position yourself ahead of 99% of candidates before Monday 8 AM"),
]

EDITION_MILESTONES = {
    1: "Edition #1 · Welcome to Your Unfair Advantage",
    3: "3-Briefing Spark · Habit Locked In",
    5: "High-Five Milestone · 5 Briefings Together",
    7: "7-Briefing Streak · Top 1% Consistency",
    10: "Double-Digit Radar · 10 Briefings Strong",
    14: "Fortnight Momentum · 14 Briefings Together",
    21: "21-Briefing Ritual · Unstoppable Velocity",
    30: "30-Briefing Club · Relentless Market Edge",
}

DAILY_MOTIVATION_THEMES = [
    "Refuse the 3% corporate loyalty tax: strategic external moves unlock 30-50% comp and scope leaps that internal review cycles stall for years.",
    "Challenge yourself before comfort becomes a cage: the moment a seat stops stretching you is the exact moment to look up.",
    "Us vs. the ATS black box: while 95% of applicants toss generic PDFs into the void, we reverse-engineered the triage keywords and pre-armed your Word CVs.",
    "Interview from power, never urgency: testing the market while you are winning lets you negotiate like an executive peer, never a supplicant.",
    "Asymmetric 60-second bet: one surgical application costs 1 minute of courage, has zero downside, and carries multi-year career upside.",
    "Outgrow the internal waiting line: when your execution outpaces your current org chart, the open market is the fastest promotion committee on earth.",
    "First-mover recruiter physics: landing in the first 15 calibrated applicants on a fresh posting 4x's hiring-manager callback odds.",
    "Be the CEO of 'Me, Inc.': treat your talent like high-growth capital—deploy it where the scope, mentorship, and upside compound fastest.",
    "Golden handcuffs rust quietly: familiarity feels safe in the short term, but audacity is what writes the best chapters of a career.",
    "Optionality changes your posture: having even two live external conversations transforms how confidently you show up in every room.",
    "Jalisco global-hub arbitrage: world-class enterprises are scaling leadership & transformation hubs right in your backyard—claim the premium seats.",
    "Compound your narrative: every stretch role you target sharpens how you articulate your highest-value wins.",
    "Beat impostor hesitation: job descriptions are wish lists, and hiring managers hire decisive problem-solvers who own 70% and learn the 30% fast.",
    "Momentum over rumination: overthinking kills more executive transitions than rejection ever could—one clean click creates real signal.",
    "Don't subsidize inertia: companies reprice new hires at market rates every quarter while keeping loyal insiders on yesterday's band.",
    "Turn rejection into calibration: you don't need twenty offers—you only need one step-up 'yes' that resets your baseline forever.",
    "Scarcity of real operators: plenty of people talk strategy; your track record of actually shipping measurable outcomes is rarer than you think.",
    "Energy follows expansion: burnout rarely comes from hard problems—it comes from solving the same solved problems without bigger stakes.",
    "Your resume is now a precision instrument: general direction at the top, hard proof in the middle, zero fluff—built for a 6-second 'yes'.",
    "Play offense while peers coast: most talented professionals wait until a reorg forces their hand; proactive movers pick their own arena.",
    "Micro-courage compounds daily: sixty seconds of boldness with morning coffee beats a whole year of 'I should update my CV someday'.",
    "Signal over noise: you don't have to scroll job boards for two hours tonight because I already hunted down the exact needles in the haystack.",
    "Step into rooms that stretch you: if you're the most experienced voice in every meeting you attend, you've already outgrown the room.",
    "Recruiters buy clarity: when your CV mirrors their exact pain points truthfully on page one, scheduling the call becomes their easiest decision all week.",
    "Protect your ambition: the people around you may settle for predictable plateaus, but your trajectory was built for steeper climbs.",
    "Speed is a status signal: crisp, early applications with tailored executive framing immediately read like high-agency leadership.",
    "Reclaim your ceiling: another team's urgent bottleneck is your exact career-upgrade runway.",
    "Zero-friction execution: the hardest 90%—sourcing, filtering, and tailoring—is already sitting in your attachments right now.",
    "Detach from outcomes, fall in love with reps: each shot on goal buys real market intelligence on what your skillset commands today.",
    "Audacity pays asymmetrical dividends: half the people in Director and VP seats simply raised their hand when equally qualified peers hesitated.",
    "Your unfair nocturnal advantage: while the city slept, your personal radar swept {board_count} boards so you wake up exclusively to curated leverage.",
    "Write your next promotion yourself: stop waiting for annual calibration committees and let competing hiring managers bid for your impact.",
]

DAILY_INSIDER_VAULT = [
    ("Stop Paying the 3% Comfort Tax.", "{first_name}, staying comfortable in one seat hands you incremental adjustments while strategic moves unlock 30%+ leaps in scope and comp. It's you and me against the hiring black box—I isolated {highlight_count} high-leverage play(s) led by {top_company} and armed your CVs. Take 60 seconds and fire one shot."),
    ("Don't Wait in Somebody Else's Queue.", "{first_name}, internal promotion cycles are engineered to keep top operators patiently producing in place. The fastest promotion committee on earth is the open market—and today's opening at {top_company} is wide open for your track record."),
    ("Comfort Is Crowded. Stretch Scope Is Wide Open.", "The moment a role stops stretching you is the moment to look up, {first_name}. While passive talent waits for permission, we scanned {board_count} boards overnight and tuned your CV to beat the triage filter—pick one target below and make them tell you no."),
    ("You + Lucy vs. The Hiring Machine.", "Most applicants lose before a human ever reads their name; we flipped the odds, {first_name}. Your wins are framed for both the ATS parser and the hiring manager's 6-second scan—grab your attached .docx for {top_company} and claim your edge."),
    ("Interview From Power, Never Urgency.", "{first_name}, elite operators test the market while they are already winning—that is how you command step-up titles and top-of-band offers. Challenge yourself with one conversation at {top_company} today; 60 seconds now can reprice your next three years."),
    ("The Market Reprices Talent Faster Than HR.", "Loyalty rarely compounds as fast as strategic mobility, {first_name}: companies pay market clearing prices to attract external leaders while capping internal bands. I surfaced {highlight_count} move(s) built for where you're going next—put your name at the top of the stack."),
    ("One Surgical Shot Beats A Year of 'Someday'.", "Getting stuck happens one 'maybe next month' at a time, {first_name}. All the friction is gone—your top targets led by {top_company} are ranked below and your Word CVs are ready in Gmail. Hit send before your first meeting."),
    ("Act Like the CEO of 'Me, Inc.' Today.", "If your career were a portfolio, {first_name}, you'd reallocate toward the highest growth ceiling immediately. {top_company} is actively investing in the exact capability you bring—deploy 60 seconds of capital into yourself this morning."),
    ("First-Mover Physics Are In Your Favor.", "Being among the earliest calibrated candidates on a fresh opening dramatically multiplies interview callbacks, {first_name}. We caught today's lineup led by {top_company} right off the wire—strike while the recruiter's queue is clean."),
    ("Outgrow the Room Before It Shrinks You.", "Burnout doesn't come from big challenges, {first_name}—it comes from solving familiar problems without bigger ownership. Step toward a steeper arena at {top_company} today; your attached CV already speaks their exact language."),
    ("Job Descriptions Are Wish Lists. Own the Room.", "Half the people sitting in bigger seats today weren't '100% ready'—they simply raised their hand while peers hesitated, {first_name}. Your proven execution puts you ahead of 90% of the field for {top_company}; take the shot."),
    ("Optionality Changes How You Walk Into Every Meeting.", "Even two live executive interviews completely change your leverage and confidence, {first_name}. Let's open a high-upside door at {top_company} this morning—zero downside, asymmetric upside."),
    ("Don't Let Familiarity Masquerade as Security.", "True career security is knowing three great companies would hire you tomorrow, {first_name}. Keep your market muscles sharp—review today's {highlight_count} curated play(s) led by {top_company} and put one live rep on the board."),
    ("Another Team's Bottleneck Is Your Promotion.", "Every open role below is a funded problem a VP is losing sleep over right now, {first_name}. At {top_company}, your real track record is the exact relief they're hunting for—introduce yourself while the seat is hot."),
    ("90% of the Work Is Already In Your Attachments.", "Most people never apply because tailoring a resume after a 10-hour workday feels exhausting, {first_name}. You don't have that problem anymore—I built your minimalist .docx CVs overnight so all you bring is 60 seconds of audacity."),
    ("Let Competing Leaders Bid on Your Impact.", "You never discover your real market value inside a single org chart, {first_name}. Test your ceiling against {top_company} today—one exploratory conversation can shift your entire compensation trajectory."),
    ("High-Agency Operators Move Before the Reorg.", "Waiting for circumstances to push you shrinks your choices; moving on your own schedule multiplies them, {first_name}. Today's radar surfaced {highlight_count} target(s) led by {top_company}—play pure offense this morning."),
    ("Clarity Wins the 6-Second Recruiter Scan.", "Hiring managers don't read walls of text—they scan for unmistakable direction and quantified proof, {first_name}. Your tailored CV for {top_company} leads with both; open it in Gmail and fire it off."),
    ("Small Morning Boldness, Massive Multi-Year ROI.", "Think about the return on sixty seconds, {first_name}: one application sent over coffee can change the logo, scope, and paycheck of your next chapter. {top_company} is on the board today—make the move."),
    ("Refuse Plateaus Dressed Up as Stability.", "Your best execution years shouldn't be spent treading water, {first_name}. Challenge yourself with a role that demands your next gear—starting with today's spotlight at {top_company}."),
    ("Scarcity Belongs to Proven Builders.", "Talkers flood job portals; leaders with real, verifiable outcomes are scarce, {first_name}. That's why we stripped away every ounce of fluff on your CV for {top_company}—let your numbers speak."),
    ("Momentum Is A Choice You Make Before 9 AM.", "Before your calendar fills up with everybody else's priorities, take one minute for your own future, {first_name}. {highlight_count} curated target(s) led by {top_company} are teed up and waiting."),
    ("Beat the Crowd While They Procrastinate.", "For every great opening at {top_company}, ten qualified people bookmark the tab and forget to apply, {first_name}. Being the one who actually hits submit with a razor-sharp CV is half the game."),
    ("Upgrade the Problems You Get Paid to Solve.", "The fastest way to grow is stepping into higher-stakes problems with bigger executive visibility, {first_name}. Today's lineup led by {top_company} offers exactly that arena—take a 60-second swing."),
    ("Your Overnight Intelligence Edge Is Live.", "While Guadalajara slept, I swept {board_count} employer boards and threw out the noise so you only see signal, {first_name}. {top_company} topped today's filter—grab your custom CV and press your advantage."),
    ("Never Self-Select Out of a Stretch Seat.", "Let the hiring committee decide if a role is a stretch—never reject yourself in advance, {first_name}. Your background gives {top_company} serious reason to book a call; put your hat in the ring."),
    ("Turn Ambition Into Pipeline Today.", "Pipeline cures career anxiety better than anything else, {first_name}. Adding {top_company} to your active conversations today costs zero effort with your pre-tailored .docx ready to attach."),
    ("Stop Discounting Skills That Feel Easy to You.", "What feels routine to you after years of mastery is often the exact capability {top_company} is paying a premium to hire right now, {first_name}. Price yourself accordingly and send one application."),
    ("Every Shot Sharpens Your Executive Story.", "Even selecting which role excites you most today clarifies where you want your career headed next, {first_name}. Look closely at {top_company} below—if it sparks curiosity, trust that signal and apply."),
    ("Play Asymmetric Offense.", "Downside of applying to {top_company} today: 60 seconds. Upside: a step-up leadership seat, fresh learning curve, and market-reset compensation. {first_name}, smart strategists take that bet every single time."),
    ("Build Escape Velocity From Routine.", "Routine quietly shrinks ambition unless you interrupt it on purpose, {first_name}. Let today's {highlight_count} curated opening(s) led by {top_company} be your pattern interrupt—one bold click and you're in motion."),
    ("Write Your Own Promotion Slip.", "Nobody cares more about your trajectory than you—except maybe me at 7 AM, {first_name}. I lined up {top_company} and armed your ATS-ready CV; go claim the conversation you deserve."),
]

LUCY_CONNECTION_NOTES = [
    "Overnight I swept {board_count} Jalisco & Remote-Mexico boards for you—Edition #{edition} together ({total_roles} roles spotted & {total_cvs} custom CVs forged so far).",
    "Briefing #{edition} in your corner: I filtered out hundreds of irrelevant postings overnight so you only spend energy on high-leverage moves.",
    "Day {streak} on active radar together—we've now surfaced {total_roles} matched openings and custom-built {total_cvs} ATS-ready CVs exclusively for your profile.",
    "Think of me as your 24/7 private scout: {board_count} company portals checked while you rested, distilled into Briefing #{edition} right on schedule.",
    "Edition #{edition} delivered: every CV attached below was tuned line-by-line to your real track record so you never start from a blank page.",
    "Your unfair advantage is compounding—{total_roles} curated roles tracked across {edition} briefings, and I'm keeping watch on {board_count} boards daily.",
    "Briefing #{edition} · {streak}-day momentum: you bring 60 seconds of courage over coffee, and I'll handle the market scanning and CV engineering every morning.",
    "Behind the scenes on Edition #{edition}: {board_count} live ATS feeds audited, noise eliminated, and {cv_today} fresh Word CV(s) armed in your inbox.",
]

DAILY_MICRO_CHALLENGES = [
    "Tap your #1 ranked role at {top_company} above, preview the attached .docx right inside Gmail, and submit before your next meeting.",
    "Set a 60-second timer right now: open {top_company}'s portal link, drop in your pre-built CV_*.docx, and log 1 fearless rep before 10 AM.",
    "Don't bookmark—execute: pick the single role below that stretches you most, attach your tailored Word CV, and hit send while your coffee is warm.",
    "Preview your first attached .docx in Gmail to feel how sharp your wins look on 1 page—then fire it straight into {top_company}'s queue.",
    "Give yourself 1 minute of pure offense: click 'Apply' on {top_company}, upload the matching .docx I built for you, and close the tab victorious.",
    "Beat 95% of procrastinating candidates today—pick 1 Career Upgrade Move above and submit your pre-armed CV before opening Slack or Teams.",
    "Run the 1-Role Rule right now: choose your favorite opening led by {top_company}, attach its tailored .docx, and earn today's momentum win.",
    "Zero overthinking allowed today: open the top card for {top_company}, verify your General Role Direction headline on the .docx, and hit submit.",
]


def extract_cv_identity_snapshot(sub_cfg, root=ROOT):
    """Extract a compact, PII-scrubbed 450-char career identity snapshot from the subscriber's CV
    so Lucy's daily motivation speaks directly to their real craft and domain."""
    raw = load_subscriber_cv(sub_cfg, root=root)
    if not raw:
        return ""
    scrubbed = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "", raw)
    scrubbed = re.sub(r"\+?\d[\d\s().-]{7,}\d", "", scrubbed)
    lines = [ln.strip(" #*-") for ln in scrubbed.splitlines() if ln.strip()]
    compact = " | ".join(lines[:8])
    return compact[:450]


def peek_relationship_context(sub_state, jobs_count, cvs_count, today, board_count=109, sub_id=""):
    """Compute next briefing edition, streak, cumulative wins, and non-repeating angle selection
    without mutating `sub_state` until delivery is confirmed."""
    rel = (sub_state or {}).get("relationship") or {}
    edition = int(rel.get("edition", 0)) + 1
    last_date_str = rel.get("last_briefing_date")
    prev_streak = int(rel.get("streak_days", 0))
    if last_date_str == today.isoformat():
        streak = max(1, prev_streak)
    elif last_date_str:
        try:
            gap = (today - date.fromisoformat(last_date_str)).days
            streak = (prev_streak + 1) if 1 <= gap <= 3 else 1
        except ValueError:
            streak = 1
    else:
        streak = 1

    recent_angles = list(rel.get("recent_angle_ids") or [])
    recent_headlines = list(rel.get("recent_headlines") or [])

    # Pick an angle guaranteed NOT to be in the recent window, offset per subscriber so
    # teammates comparing notes on the same morning each receive their own unique angle.
    total_themes = len(DAILY_MOTIVATION_THEMES)
    sub_offset = sum((idx + 1) * ord(ch) for idx, ch in enumerate(str(sub_id or ""))) % total_themes
    avoid_set = set(recent_angles[-(total_themes - 1):])
    base_idx = (today.toordinal() + sub_offset + edition - 1) % total_themes
    angle_id = base_idx
    for step in range(total_themes):
        cand = (base_idx + step) % total_themes
        if cand not in avoid_set:
            angle_id = cand
            break

    return {
        "edition": edition,
        "streak_days": streak,
        "total_roles_surfaced": int(rel.get("total_roles_surfaced", 0)) + int(jobs_count),
        "total_cvs_tailored": int(rel.get("total_cvs_tailored", 0)) + int(cvs_count),
        "recent_angle_ids": recent_angles,
        "recent_headlines": recent_headlines,
        "angle_id": angle_id,
        "board_count": board_count,
        "milestone_badge": EDITION_MILESTONES.get(edition, ""),
    }


def commit_relationship_state(sub_state, rel_ctx, pack, jobs_sent, today):
    """Persist updated relationship streak, edition, cumulative stats, and anti-repeat history."""
    if sub_state is None:
        return
    rel = sub_state.setdefault("relationship", {})
    rel["edition"] = rel_ctx["edition"]
    rel["streak_days"] = rel_ctx["streak_days"]
    rel["last_briefing_date"] = today.isoformat()
    rel["total_roles_surfaced"] = rel_ctx["total_roles_surfaced"]
    rel["total_cvs_tailored"] = rel_ctx["total_cvs_tailored"]

    angles = list(rel.get("recent_angle_ids") or [])
    angles.append(rel_ctx["angle_id"])
    rel["recent_angle_ids"] = angles[-30:]

    headlines = list(rel.get("recent_headlines") or [])
    if pack.get("headline"):
        headlines.append(pack["headline"])
    rel["recent_headlines"] = headlines[-30:]

    spotlights = list(rel.get("recent_spotlight_keys") or [])
    for j in jobs_sent or []:
        if j.get("quiet_spotlight") and j.get("key"):
            spotlights.append(j["key"])
    rel["recent_spotlight_keys"] = spotlights[-18:]


def call_gemini_daily_motivation(prompt_text, model=DEFAULT_LLM_MODEL, api_key=None):
    """Call Gemini for a fresh daily 5-part 'Us vs. The System' dopamine & connection briefing."""
    try:
        parsed = _invoke_gemini_json(
            prompt_text,
            system_text=DAILY_MOTIVATION_SYSTEM_PROMPT,
            model=model,
            api_key=api_key,
            temperature=0.9,
            max_output_tokens=2048,
            timeout=25,
            retries=1,
            validator=lambda p: isinstance(p, dict) and bool(p.get("headline")) and bool(p.get("message")),
        )
        if parsed:
            return {
                "subject_spark": str(parsed.get("subject_spark") or "").strip(),
                "headline": str(parsed["headline"]).strip(),
                "message": str(parsed["message"]).strip(),
                "connection_ps": str(parsed.get("connection_ps") or "").strip(),
                "micro_challenge": str(parsed.get("micro_challenge") or "").strip(),
            }
    except Exception:
        pass
    return None


def generate_daily_briefing_pack(
    sub_cfg, top_job, upgrade_count, hot_count, total_count, today,
    rel_ctx=None, cvs_today_count=0, is_quiet_spotlight=False, root=ROOT
):
    """Generate a guaranteed-unique daily dopamine & personal connection pack for the subscriber."""
    sub_name = (sub_cfg or {}).get("name") or "Candidate"
    sub_id = (sub_cfg or {}).get("id") or sub_name.lower()
    first_name = sub_name.split()[0]
    top_co = (top_job or {}).get("company") or "a top employer"
    top_title = (top_job or {}).get("title") or "a step-up leadership role"
    hi_cnt = upgrade_count if upgrade_count > 0 else max(1, total_count)

    if rel_ctx is None:
        sub_offset = sum(ord(ch) for ch in sub_id) % len(DAILY_MOTIVATION_THEMES)
        angle_id = (today.toordinal() + sub_offset) % len(DAILY_MOTIVATION_THEMES)
        rel_ctx = {
            "edition": 1,
            "streak_days": 1,
            "total_roles_surfaced": max(1, total_count),
            "total_cvs_tailored": cvs_today_count,
            "recent_angle_ids": [],
            "recent_headlines": [],
            "angle_id": angle_id,
            "board_count": 109,
            "milestone_badge": "",
        }

    b_cnt = rel_ctx.get("board_count", 109)
    angle_id = rel_ctx["angle_id"]
    edition = rel_ctx["edition"]
    cadence_title, cadence_subline = WEEKDAY_CADENCE[today.weekday()]
    theme = DAILY_MOTIVATION_THEMES[angle_id % len(DAILY_MOTIVATION_THEMES)].format(board_count=b_cnt)

    # Deterministic non-repeating defaults for connection_ps, micro_challenge, and vault fallback
    conn_tmpl = LUCY_CONNECTION_NOTES[(angle_id + edition) % len(LUCY_CONNECTION_NOTES)]
    default_conn = conn_tmpl.format(
        board_count=b_cnt,
        edition=edition,
        streak=rel_ctx.get("streak_days", 1),
        total_roles=rel_ctx.get("total_roles_surfaced", total_count),
        total_cvs=rel_ctx.get("total_cvs_tailored", cvs_today_count),
        cv_today=max(1, cvs_today_count),
    )
    chal_tmpl = DAILY_MICRO_CHALLENGES[(angle_id + edition) % len(DAILY_MICRO_CHALLENGES)]
    default_challenge = chal_tmpl.format(top_company=top_co, first_name=first_name)

    h_tmpl, m_tmpl = DAILY_INSIDER_VAULT[angle_id % len(DAILY_INSIDER_VAULT)]
    vault_headline = h_tmpl.format(first_name=first_name, top_company=top_co, highlight_count=hi_cnt, board_count=b_cnt)
    vault_message = m_tmpl.format(first_name=first_name, top_company=top_co, highlight_count=hi_cnt, board_count=b_cnt)
    default_spark = vault_headline.rstrip(".!")

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if api_key:
        cv_snapshot = extract_cv_identity_snapshot(sub_cfg, root=root)
        career_goal = ((sub_cfg or {}).get("career_goal") or "").strip()
        goal_line = f"Candidate Career Goal & North Star: {career_goal}\n" if career_goal else ""
        avoid_list = rel_ctx.get("recent_headlines") or []
        avoid_txt = "; ".join(f'"{h}"' for h in avoid_list[-12:]) if avoid_list else "None yet (Edition #1 — make a memorable first impression!)"
        mode_note = (
            "Quiet-market momentum spotlight (zero brand-new overnight postings, so you hand-picked their top unclaimed Career Upgrade Moves on the board so they keep daily momentum)."
            if is_quiet_spotlight
            else "Fresh market drop with newly discovered openings."
        )
        model = (os.environ.get("GEMINI_MODEL") or "").strip() or (sub_cfg or {}).get("llm_model", DEFAULT_LLM_MODEL)
        prompt = (
            f"Date: {today.isoformat()} ({cadence_title} — {cadence_subline})\n"
            f"Briefing Edition Together: #{edition} | Active Streak: {rel_ctx.get('streak_days', 1)} day(s) | "
            f"Cumulative Wins Together: {rel_ctx.get('total_roles_surfaced', total_count)} matched roles surfaced across {b_cnt} boards, "
            f"{rel_ctx.get('total_cvs_tailored', cvs_today_count)} custom CVs built.\n"
            f"Candidate First Name: {first_name}\n"
            f"{goal_line}"
            f"Candidate Career Identity Snapshot: {cv_snapshot or 'Ambitious leader targeting high-impact step-up roles in Jalisco & Remote Mexico'}\n"
            f"Today's Psychological Dopamine Angle (#{angle_id + 1}): {theme}\n"
            f"Run Context: {mode_note}\n"
            f"Today's Radar Stats: {upgrade_count} Career Upgrade Moves, {hot_count} Hot (0-2d) openings, "
            f"{total_count} roles in today's briefing, {cvs_today_count} tailored Word CVs attached. "
            f"Spotlight Hiring Employer: '{top_co}' | Spotlight Role: '{top_title}' (use exact employer '{top_co}', never confuse with an ATS platform like Workday).\n"
            f"RECENT_HEADLINES_TO_AVOID (DO NOT repeat these words, metaphors, or hooks): {avoid_txt}\n"
            "Generate today's 100% unique 5-part briefing JSON."
        )
        try:
            res = call_gemini_daily_motivation(prompt, model=model, api_key=api_key)
            if isinstance(res, tuple) and len(res) == 2:
                # Backward-compatible support if mocked as (headline, message)
                headline = _fix_ats_employer_confusion(str(res[0]).strip(), top_co, top_title) if top_job else str(res[0]).strip()
                message = _fix_ats_employer_confusion(str(res[1]).strip(), top_co, top_title) if top_job else str(res[1]).strip()
                return {
                    "subject_spark": headline.rstrip(".!")[:42],
                    "headline": headline,
                    "message": message,
                    "connection_ps": default_conn,
                    "micro_challenge": default_challenge,
                    "cadence_title": cadence_title,
                    "cadence_subline": cadence_subline,
                    "angle_number": angle_id + 1,
                }
            if isinstance(res, dict) and res.get("headline") and res.get("message"):
                hl = _fix_ats_employer_confusion(res["headline"], top_co, top_title) if top_job else res["headline"]
                msg_txt = _fix_ats_employer_confusion(res["message"], top_co, top_title) if top_job else res["message"]
                spark = _fix_ats_employer_confusion(res.get("subject_spark") or hl.rstrip(".!"), top_co, top_title) if top_job else (res.get("subject_spark") or hl.rstrip(".!"))
                chal = _fix_ats_employer_confusion(res.get("micro_challenge") or default_challenge, top_co, top_title) if top_job else (res.get("micro_challenge") or default_challenge)
                return {
                    "subject_spark": spark[:46],
                    "headline": hl,
                    "message": msg_txt,
                    "connection_ps": res.get("connection_ps") or default_conn,
                    "micro_challenge": chal,
                    "cadence_title": cadence_title,
                    "cadence_subline": cadence_subline,
                    "angle_number": angle_id + 1,
                }
        except Exception:
            pass

    return {
        "subject_spark": default_spark[:46],
        "headline": vault_headline,
        "message": vault_message,
        "connection_ps": default_conn,
        "micro_challenge": default_challenge,
        "cadence_title": cadence_title,
        "cadence_subline": cadence_subline,
        "angle_number": angle_id + 1,
    }


def generate_daily_insider_note(sub_cfg, top_job, upgrade_count, hot_count, total_count, today, rel_ctx=None):
    """Return `(headline, message)` tuple from `generate_daily_briefing_pack`."""
    pack = generate_daily_briefing_pack(
        sub_cfg, top_job, upgrade_count, hot_count, total_count, today, rel_ctx=rel_ctx
    )
    return pack["headline"], pack["message"]


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


def build_email_message(
    jobs_to_send, all_jobs_count, sub_cfg, today, sender_email,
    attachments=None, sub_state=None, root=ROOT, board_count=109, return_meta=False
):
    """Construct an editorial-grade, minimalist executive daily briefing with a guaranteed-unique
    daily GenAI 'Us Against the System' dopamine manifesto, personal relationship streak with Lucy,
    and 60-second completion challenge."""
    recipient = sub_cfg.get("recipient_email", "lucasschollmatter@gmail.com")
    sub_name = sub_cfg.get("name") or recipient
    first_name = (sub_name or "there").split()[0]
    date_str = today.isoformat()
    attachments = attachments or []

    # Ensure every job has career-upgrade classification
    classify_career_upgrades(jobs_to_send, sub_cfg, root=root)

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
    is_quiet_spotlight = bool(jobs_to_send) and all(j.get("quiet_spotlight") for j in jobs_to_send)

    rel_ctx = peek_relationship_context(
        sub_state, len(jobs_to_send), len(attachments), today,
        board_count=board_count, sub_id=sub_cfg.get("id") or recipient,
    )
    pack = generate_daily_briefing_pack(
        sub_cfg, top_job, len(upgrade_jobs), len(hot_jobs), len(jobs_to_send), today,
        rel_ctx=rel_ctx, cvs_today_count=len(attachments),
        is_quiet_spotlight=is_quiet_spotlight, root=root,
    )
    insider_headline = pack["headline"]
    insider_message = pack["message"]
    connection_ps = pack["connection_ps"]
    micro_challenge = pack["micro_challenge"]
    subject_spark = pack["subject_spark"]
    edition = rel_ctx["edition"]
    streak_days = rel_ctx["streak_days"]
    milestone_badge = rel_ctx.get("milestone_badge") or ""

    msg = EmailMessage()
    hot_prefix = f"🔥 {len(hot_jobs)} HOT · " if hot_jobs else "⚡ "
    up_part = f"{len(upgrade_jobs)} Career Upgrade Move(s) · " if upgrade_jobs else ""
    cv_suffix = f" + {len(attachments)} Tailored CV(s)" if attachments else ""
    spark_part = f'"{subject_spark}" — ' if subject_spark else ""
    msg["Subject"] = (
        f"{hot_prefix}Lucy #{edition}: {spark_part}{up_part}{len(jobs_to_send)} Relevant Opening(s){cv_suffix} ({date_str})"
    )
    msg["From"] = f"Lucy · Personal Headhunter <{sender_email}>"
    msg["To"] = recipient

    # Minimalist plain-text version (preserves '- {title} at {company}' format)
    txt_lines = [
        f"{sub_name} — Lucy Private Talent Briefing #{edition} · Day {streak_days} Streak ({date_str})",
        f"{pack['cadence_subline']} — Lucy, your personal headhunter & your daily reminder to check where to apply today.",
        "",
        f"⚡ {insider_headline}",
        insider_message,
        f"🤝 Lucy's Corner: {connection_ps}",
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
    txt_lines.append(f"🎯 Today's 60-Second Play: {micro_challenge}")
    msg.set_content("\n".join(txt_lines))

    # Minimalist HTML sections
    sections_html = []
    if is_quiet_spotlight:
        sections_html.append(
            "<div style='background-color:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;border-radius:8px;"
            "padding:9px 12px;margin-bottom:16px;font-size:12px;line-height:1.45;'>"
            "<strong>&#10024; Quiet-Market Momentum Spotlight:</strong> Zero brand-new postings dropped overnight, "
            "so I hand-picked your top unclaimed step-up opportunities still live on the radar to keep your daily momentum unbroken."
            "</div>"
        )

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
        sections_html.append(
            "<div style='font-size:11px;color:#71717a;margin:0 0 12px;'>Hot Off the Press: includes roles posted in the last 1&ndash;2 days.</div>"
        )

    first_name_esc = html.escape(first_name)
    headline_esc = html.escape(insider_headline)
    message_esc = html.escape(insider_message)
    conn_esc = html.escape(connection_ps)
    challenge_esc = html.escape(micro_challenge)
    cadence_title_esc = html.escape(pack["cadence_title"])
    cadence_sub_esc = html.escape(pack["cadence_subline"])
    preheader_esc = html.escape(f"⚡ Briefing #{edition}: {insider_headline} — {insider_message}")

    streak_label = f"&#128293; {streak_days}d Streak" if streak_days >= 2 else "Day 1 Live"
    milestone_html = (
        f"<div style='display:inline-block;background-color:#064e3b;border:1px solid #059669;color:#6ee7b7;"
        f"font-size:10px;font-weight:700;padding:3px 9px;border-radius:999px;letter-spacing:0.5px;"
        f"margin-bottom:10px;text-transform:uppercase;'>&#127942; {html.escape(milestone_badge)}</div>"
        if milestone_badge
        else ""
    )

    up_color = "#059669" if upgrade_jobs else "#18181b"
    hot_color = "#d97706" if hot_jobs else "#18181b"
    cv_color = "#0f766e" if attachments else "#71717a"

    html_body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lucy &middot; Briefing #{edition}</title>
</head>
<body style="margin:0;padding:28px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#18181b;background-color:#f4f4f5;-webkit-font-smoothing:antialiased;">
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{preheader_esc}</div>

  <div style="max-width:640px;margin:0 auto;background-color:#ffffff;border:1px solid #e4e4e7;border-radius:14px;padding:30px 28px 24px;">

    <!-- Editorial Masthead + Briefing Edition & Streak -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin-bottom:10px;">
      <tr>
        <td align="left" style="font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:1.3px;color:#059669;">
          &#10022; Lucy &middot; Personal Headhunter
        </td>
        <td align="right" style="font-size:11px;font-weight:700;color:#52525b;">
          Briefing #{edition} &middot; {streak_label} &middot; {date_str}
        </td>
      </tr>
    </table>

    <h1 style="margin:0 0 4px;font-size:23px;font-weight:800;letter-spacing:-0.5px;color:#09090b;line-height:1.25;">
      Good morning, {first_name_esc}.
    </h1>
    <div style="font-size:13px;color:#52525b;margin-bottom:16px;line-height:1.45;">
      <strong style="color:#18181b;">{cadence_sub_esc}</strong> &mdash; your daily reminder to check where to apply today.
    </div>

    <!-- Dark Obsidian 'Us Against the System' Daily Manifesto + Lucy Partnership Corner -->
    <div style="background-color:#09090b;color:#fafafa;border-radius:11px;padding:18px 20px;margin-bottom:16px;">
      {milestone_html}
      <div style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.4px;color:#34d399;margin-bottom:6px;">
        &#9889; Today&rsquo;s Edge &middot; Us vs. The System &middot; {cadence_title_esc}
      </div>
      <div style="font-size:16.5px;font-weight:800;color:#ffffff;letter-spacing:-0.2px;margin-bottom:7px;line-height:1.3;">
        {headline_esc}
      </div>
      <div style="font-size:13px;color:#e4e4e7;line-height:1.58;">
        {message_esc}
      </div>
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid #27272a;font-size:12px;color:#a1a1aa;line-height:1.5;">
        <span style="color:#34d399;font-weight:700;">&#129309; Lucy&rsquo;s Corner:</span> {conn_esc}
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

    <!-- Dynamic Daily 60-Second Dopamine Challenge Footer -->
    <div style="margin-top:18px;padding:14px 16px;background-color:#fafafa;border:1px solid #e4e4e7;border-left:3px solid #059669;border-radius:9px;font-size:12px;color:#27272a;line-height:1.5;">
      <strong style="color:#09090b;">&#127919; Today&rsquo;s 60-Second Dopamine Play:</strong> {challenge_esc}
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
    if return_meta:
        return msg, rel_ctx, pack
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


def send_email_digest(
    jobs_to_send, all_jobs_count, sub_cfg, today, root=ROOT,
    attachments=None, sub_state=None, board_count=109
):
    """Send a single subscriber's daily digest via Gmail/SMTP if credentials are configured."""
    sub_name = sub_cfg.get("name") or sub_cfg.get("recipient_email", "subscriber")
    if not jobs_to_send:
        print(f"  [{sub_name}] No new roles to email today.")
        return False

    if attachments is None:
        attachments = prepare_tailored_cvs(jobs_to_send, sub_cfg, today, root=root)

    smtp_user = (os.environ.get("GMAIL_USER") or os.environ.get("SMTP_USER") or "").strip()
    smtp_password = (os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("SMTP_PASSWORD") or "").strip()
    smtp_host = (os.environ.get("SMTP_HOST") or "smtp.gmail.com").strip()
    smtp_port = int((os.environ.get("SMTP_PORT") or "465").strip())

    if not smtp_user or not smtp_password:
        print(f"  [{sub_name}] Email skipped: GMAIL_USER / GMAIL_APP_PASSWORD environment variables not set.")
        return False

    recipient = (sub_cfg.get("recipient_email") or "").strip()
    if not recipient:
        print(f"  [{sub_name}] Email skipped: recipient_email is empty.")
        return False

    msg, rel_ctx, pack = build_email_message(
        jobs_to_send, all_jobs_count, sub_cfg, today, smtp_user,
        attachments=attachments, sub_state=sub_state, root=root,
        board_count=board_count, return_meta=True,
    )
    in_test = "unittest" in sys.modules
    last_smtp_err = None
    for attempt in range(3):
        try:
            if smtp_port == 587:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                    server.ehlo()
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
            last_smtp_err = None
            break
        except smtplib.SMTPAuthenticationError:
            raise
        except Exception as e:
            last_smtp_err = e
            if attempt < 2 and not in_test:
                time.sleep(2.5 * (attempt + 1))
    if last_smtp_err is not None:
        raise last_smtp_err

    commit_relationship_state(sub_state, rel_ctx, pack, jobs_to_send, today)
    print(
        f"  [{sub_name}] Sent Briefing #{rel_ctx['edition']} ({len(jobs_to_send)} roles, "
        f"{len(attachments)} tailored CVs, angle #{pack['angle_number']}) to {msg['To']}."
    )
    return True


def process_subscribers(fetched, state, cfg, today, send_email=True, only_id=None, root=ROOT, board_count=109):
    """Filter fetched jobs for each active subscriber, update their personal seen
    state in state['subscribers'][sub_id], and send each subscriber's email.

    Returns the list of subscriber ids whose email delivery raised. For those
    subscribers the per-subscriber state is rolled back to what it was before
    this run, so the same roles are re-discovered (and re-emailed) next run
    instead of being silently lost."""
    subscribers = get_subscribers(cfg, only_id=only_id)
    if not isinstance(state.get("subscribers"), dict):
        state["subscribers"] = {}
    sub_states = state["subscribers"]
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
        previous_state = copy.deepcopy(sub_states.get(sub_id))
        try:
            sub_rules = build_rules(sub_cfg)
            sub_matched = list({j["key"]: copy.deepcopy(j) for j in select(fetched, sub_rules, sub_cfg)}.values())

            sub_state = sub_states.setdefault(sub_id, {})
            if not isinstance(sub_state, dict):
                sub_state = sub_states[sub_id] = {}
            sub_baseline = update_state(sub_matched, sub_state, today, retention)
            sub_matched = dedupe_subscriber_jobs(sub_matched)
            new_days = int(sub_cfg.get("new_days", cfg["new_days"]))
            for j in sub_matched:
                j["is_new"] = flag_new(j, today, new_days)
                j["eff_date"] = j.get("posted_at") or j["first_seen"]
            sub_matched.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
            sub_matched.sort(key=lambda j: j["eff_date"], reverse=True)

            if send_email:
                to_email = select_jobs_for_email(sub_matched, sub_baseline, sub_cfg, sub_state=sub_state)
                attachments = prepare_tailored_cvs(to_email, sub_cfg, today, root=root) if to_email else []
                send_email_digest(
                    to_email, len(sub_matched), sub_cfg, today, root=root,
                    attachments=attachments, sub_state=sub_state, board_count=board_count,
                )
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
         item = {k: v for k, v in j.items() if k not in ("description", "detail_api", "llm_analysis", "cv_filename", "is_career_upgrade", "upgrade_reason", "quiet_spotlight", "_wd_multi_term")}
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
    all_rule_sets = [rules] + [build_rules(s) for s in get_subscribers(cfg)]
    resolve_workday_multi_locations(fetched, all_rule_sets, rules, cfg)
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
                                         only_id=subscriber_id, root=root, board_count=len(companies))

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
    all_rule_sets = [rules] + [build_rules(s) for s in get_subscribers(cfg)]
    bad = 0
    failed_list = []
    for c, jobs, err in fetch_all(companies, cfg):
        if jobs is None:
            bad += 1
            failed_list.append((c["name"], err))
            print(f"FAIL  {c['name']:<26} {c.get('ats', '?'):<16} {err}")
            continue
        resolve_workday_multi_locations(jobs, all_rule_sets, rules, cfg)
        in_area = sum(1 for j in jobs if location_class(j["location"], rules, cfg))
        director = len(select(jobs, rules, cfg))
        print(f"ok    {c['name']:<26} {c['ats']:<16} {len(jobs):>5} jobs, "
              f"{in_area:>3} in Jalisco/remote-MX, {director:>3} director-level")
    ok = len(companies) - bad
    print(f"\n{bad} of {len(companies)} boards failed." if bad else f"\nAll {len(companies)} boards responded.")
    gha_summary(
        f"### Jobs Sentinel Board Health Check\n"
        f"- Healthy boards: **{ok}/{len(companies)}**"
        + (f"\n- Unreachable boards ({bad}): " + ", ".join(f"`{n}` ({e})" for n, e in failed_list) if failed_list else "")
    )
    if failed_list:
        gha_annotate("warning", f"{bad}/{len(companies)} board(s) unreachable in --check: " + "; ".join(f"{n}: {e}" for n, e in failed_list))
    return 1 if bad == len(companies) else 0


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
