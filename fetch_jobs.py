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

DEFAULT_LLM_MODEL = "gemini-4-hydrogen"
FALLBACK_LLM_MODELS = (
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
)

CFG_DEFAULTS = {
    "page_title": "Openings",
    "recipient_email": "lucasschollmatter@gmail.com",
    "email_on_baseline": True,
    "email_mode": "new_or_discovered",
    "daily_top_when_quiet": False,
    "quiet_day_mode": "note",
    "quiet_spotlight_count": 3,
    "max_email_jobs": 12,
    "llm_model": DEFAULT_LLM_MODEL,
    "min_match_score": 70,
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

LLM_JOB_MATCH_SYSTEM_PROMPT = """You are Lucy, an uncompromising, candidate-first executive career advisor and headhunter.
Your mission is to evaluate every job opening surfaced by title/location filters and decide whether it is TRULY a strong, high-interest match for THIS specific candidate—so Lucy's daily report ONLY presents openings that genuinely fit what the candidate wants next in their career and what they have proven on their CV.

Evaluate each job across ALL FOUR dimensions:
1. CAREER GOAL & DIRECTION ALIGNMENT: Does the role's function, domain, and responsibility match what the candidate is actively looking for in their career (`CANDIDATE CAREER GOAL`)?
2. CV TRACK RECORD & TRANSFERABLE PROOF: Does the candidate's `MASTER CV` demonstrate credible domain skills, leadership scope, methodologies, languages, and seniority for this opening?
3. COMPANY & INDUSTRY CONTEXT FIT: Compare the hiring company's `company_industry` and `company_description` against the industries, business models, and scale of the companies on the candidate's CV (`Company Profile` entries). Favor strong domain or operating-model parallels (e.g., global GBS, enterprise cloud/SaaS, pharma/MedTech, B2B cybersecurity/fintech commercial, supply-chain/manufacturing scale, nearshore consulting).
4. STRICT FALSE-POSITIVE REJECTION: Set `is_good_match: false` (and `match_score` below threshold) whenever a posting only matched a broad title keyword (like 'Director', 'Manager', 'Lead', 'Analyst', 'Consultant', or 'Project Manager') in an UNRELATED discipline—such as pure Accounting/Tax/Controller/Audit, Legal/Counsel, Clinical/Medical practice, Silicon/Hardware/Mechanical/Chemical lab engineering, Creative Marketing Agency production, IT Helpdesk, or Factory-Floor operator/technician supervision—or when the role is a clear seniority step-back or requires specialized licenses the candidate lacks.

Formatting & Safety Rules:
- Always refer to each employer by its exact `hiring_employer` name; NEVER confuse the employer with its ATS portal (Workday, Greenhouse, Lever, SmartRecruiters, Oracle HCM, Eightfold, Ashby, Workable, BambooHR).
- Write `match_rationale` directly to the candidate ("You...") in 2-3 crisp, concrete sentences explaining WHY this role and company are a compelling match—citing specific past employers/achievements from their CV, how the company's industry/model connects to their background, and how it advances their career goal.
- Never use < > [ ] " { } \\ inside string values.

Return ONLY valid JSON matching this exact schema:
{
  "assessments": [
    {
      "id": 0,
      "is_good_match": true,
      "match_score": 88,
      "is_career_upgrade": true,
      "match_rationale": "2-3 specific sentences to the candidate explaining why this role and company fit their career goal and CV track record.",
      "industry_synergy": "1 concise phrase linking the hiring company's industry/business model with the candidate's past employers.",
      "upgrade_reason": "1 concise sentence explaining why this role is a career step-up in scope/seniority, or empty string if lateral.",
      "matched_keywords": ["Skill 1", "Skill 2", "Skill 3", "Skill 4"]
    }
  ]
}"""

LLM_SYSTEM_PROMPT = """You are Lucy, an elite executive headhunter, ATS optimization architect, and minimalist resume strategist.
Compare the candidate's MASTER CV against the TARGET JOB POSTING (including the target company's industry & business description) and produce a CV that satisfies BOTH automated ATS/AI triage engines (top-decile keyword & semantic match) AND human hiring managers (immediate 'schedule for a call' conviction in a 6-second scan).

Strict Rules:
1. 100% TRUTHFULNESS: NEVER invent companies, job titles, dates, degrees, certifications, or fake metrics. Only select, prioritize, and re-angle real achievements and real employer profiles from the MASTER CV so they truthfully mirror the domain terminology and business priorities of the TARGET JOB POSTING.
2. GENERAL ROLE DIRECTION HEADLIGHT (`target_direction`): Craft a clear overarching career direction headline at the top of the CV that informs the reader of the general type of role and functional leadership domain the candidate is pursuing—aligned with the area of the job being applied to (e.g., "Target Role Direction: Director / Head of Process Intelligence, GBS & Enterprise Operations"), rather than awkwardly parroting an internal requisition code.
3. ATS & AI TRIAGE OPTIMIZATION: Mirror exact domain terminology, methodologies, platforms, and leadership competencies from the job posting that genuinely exist in the candidate's background. Include both full terms and standard acronyms where natural (e.g., "Global Business Services (GBS)", "Object-Centric Process Mining (OCPM)"). Weave these keywords naturally into `summary` and `experience` bullets (where ATS engines assign highest weight) as well as `core_skills` (8–10 high-signal skills).
4. HUMAN RECRUITER 'CALL CONVICTION': Keep the CV minimalist, executive, and scannable on 1 page. Structure `summary` as a 3-sentence executive pitch (Domain Identity + Signature Quantified Proof + Value for Target Company). Format every `experience` bullet as `Strong Action Verb + Scope/Context + Quantified Business Outcome` (3-4 bullets for the 2 most relevant/recent roles; 1-2 bullets for earlier roles).
5. EMPLOYER INDUSTRY & SCALE CONTEXT (`company_context`): For each role in `experience`, include a crisp 1-line `company_context` describing what that employer is, its industry, scale, and business model (grounded in the MASTER CV's Company Profile notes and angled truthfully so recruiters and ATS models immediately recognize the industry/operating-model parallel with the target employer).
6. CAREER UPGRADE MOVE (`is_career_upgrade`): Set to true if this role represents a step up in seniority, leadership scope, architectural ownership, or strategic challenge compared to the candidate's current/most recent role on their MASTER CV.
7. ATS PORTAL AUTO-FILL COMPLIANCE (CRITICAL): Enterprise job portals (Workday, Oracle HCM, Taleo, SuccessFactors, iCIMS) reject CVs whose text contains any of these 8 characters: < > [ ] " { } \\. NEVER use < > [ ] " { } \\ anywhere inside `cv` text fields. Write "under 5-minute" instead of "<5-minute", "over 90%" instead of ">90%", standard parentheses ( ) instead of [ ] or { }, "/" instead of "\\", and plain words without double quotes.
8. Write in the language of the job posting (default to English if the posting is in English or mixed).

Return ONLY valid JSON matching this exact schema:
{
  "match_score": 92,
  "is_career_upgrade": true,
  "upgrade_reason": "1 concise sentence explaining why this role is a career upgrade move or high-impact stretch challenge relative to their current role.",
  "lucy_why_it_fits": "2-3 energizing, high-conviction sentences directly to the candidate explaining the exact rationale of the match—citing their real signature wins, past employer/industry synergy, and why the hiring manager at this company will immediately want them on a call.",
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
        "company_context": "1-line industry, scale, and business-model description of this employer showing domain relevance.",
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


_COMPANY_DIR_CACHE = {}


def build_company_directory(companies=None, root=ROOT):
    """Map normalized company name -> {'name', 'industry', 'description'} for radar employers."""
    if companies is not None:
        directory = {}
        for c in companies:
            if isinstance(c, dict) and c.get("name"):
                k = norm(c["name"]).strip()
                directory[k] = {
                    "name": c["name"],
                    "industry": str(c.get("industry") or "").strip(),
                    "description": str(c.get("description") or "").strip(),
                }
        return directory

    cache_key = str(Path(root).resolve())
    if cache_key in _COMPANY_DIR_CACHE:
        return _COMPANY_DIR_CACHE[cache_key]
    directory = {}
    for base in (Path(root), ROOT):
        comp_path = base / "companies.json"
        if comp_path.exists():
            try:
                for c in json.loads(comp_path.read_text(encoding="utf-8")).get("companies") or []:
                    if isinstance(c, dict) and c.get("name"):
                        k = norm(c["name"]).strip()
                        directory.setdefault(k, {
                            "name": c["name"],
                            "industry": str(c.get("industry") or "").strip(),
                            "description": str(c.get("description") or "").strip(),
                        })
            except (OSError, json.JSONDecodeError):
                pass
    _COMPANY_DIR_CACHE[cache_key] = directory
    return directory


def get_company_metadata(company_name, companies=None, root=ROOT):
    """Return {'industry': str, 'description': str} for a radar company if known."""
    if not company_name:
        return {"industry": "", "description": ""}
    k = norm(company_name).strip()
    meta = build_company_directory(companies=companies, root=root).get(k)
    if not meta and companies is not None:
        meta = build_company_directory(companies=None, root=root).get(k)
    if meta:
        return {"industry": meta.get("industry", ""), "description": meta.get("description", "")}
    return {"industry": "", "description": ""}


def enrich_company_metadata(jobs, companies=None, root=ROOT):
    """Populate `company_industry` and `company_description` on job dicts from companies.json."""
    for j in jobs or []:
        if not isinstance(j, dict):
            continue
        if j.get("company_industry") and j.get("company_description"):
            continue
        meta = get_company_metadata(j.get("company", ""), companies=companies, root=root)
        if meta["industry"] and not j.get("company_industry"):
            j["company_industry"] = meta["industry"]
        if meta["description"] and not j.get("company_description"):
            j["company_description"] = meta["description"]
    return jobs


def load_subscriber_cv(sub_cfg, root=ROOT):
    """Load a subscriber's master CV text from `cv_text` or `cv_file` (with directory boundary check)."""
    if sub_cfg.get("cv_text"):
        return str(sub_cfg["cv_text"]).strip()
    cv_rel = sub_cfg.get("cv_file")
    if not cv_rel:
        return ""
    for base in (Path(root), ROOT):
        base_real = os.path.realpath(str(base))
        cand_real = os.path.realpath(os.path.join(base_real, str(cv_rel)))
        if not cand_real.startswith(base_real + os.sep):
            continue
        cand = Path(cand_real)
        if cand.is_file():
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
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500].strip()
                if detail:
                    e.msg = f"{e.msg} — {detail[:300]}"
            except Exception:
                pass
            if e.code < 500 and e.code not in (408, 425, 429):
                raise
            if e.headers:
                ra = e.headers.get("Retry-After")
                if ra and str(ra).strip().isdigit():
                    retry_wait = min(12.0, max(1.0, float(ra)))
            if retry_wait is None and e.code == 429 and detail:
                m_wait = re.search(r'(?:retry in|retryDelay"\s*:\s*")\s*(\d+(?:\.\d+)?)s', detail, re.I)
                if m_wait:
                    retry_wait = min(12.0, max(1.0, float(m_wait.group(1))))
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
    """Append Markdown to the GitHub Actions step summary when running in CI.
    Suppressed under unittest so offline test runs never pollute the live Run Summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and "unittest" not in sys.modules:
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
    if c.get("industry"):
        out["company_industry"] = str(c["industry"]).strip()
    if c.get("description"):
        out["company_description"] = str(c["description"]).strip()
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
    if "locations" in cfg and cfg["locations"] is not None:
        locs = cfg["locations"]
    else:
        locs = cfg.get("jalisco_locations") or []
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
        skip_tokens = (
            "tts", "image", "audio", "embedding", "live", "vision",
            "veo", "imagen", "banana", "transcribe", "robotics",
        )
        for item in d.get("models") or []:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            short = _normalize_gemini_model(item.get("name", ""))
            low = short.lower()
            if not low.startswith("gemini-") or ("flash" not in low and "hydrogen" not in low):
                continue
            if any(tok in low for tok in skip_tokens):
                continue
            found.append(short)

        def _rank(name):
            m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
            maj = int(m.group(1)) if m else 0
            min_v = int(m.group(2) or 0) if m else 0
            is_hydrogen = 1 if "hydrogen" in name else 0
            not_lite = 0 if "lite" in name else 1
            not_preview = 0 if ("preview" in name or "exp" in name) else 1
            return (maj, min_v, is_hydrogen, not_lite, not_preview)

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
    thinking_level="low",
):
    """Call Gemini generateContent across primary + free-tier fallback + auto-discovered models."""
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
        is_gemini_4 = m_name.startswith("gemini-4")
        # Gemini 4 shares maxOutputTokens across thinking + response tokens and deprecates temperature.
        eff_max_tokens = (
            max(int(max_output_tokens or 4096), 16384)
            if is_gemini_4
            else int(max_output_tokens or 4096)
        )
        gen_cfg = {
            "maxOutputTokens": eff_max_tokens,
            "responseMimeType": "application/json",
        }
        if not is_gemini_4 and temperature is not None:
            gen_cfg["temperature"] = temperature
        if is_gemini_4 or m_name.startswith("gemini-3"):
            lvl = str(thinking_level or "low").lower()
            if is_gemini_4 and lvl == "minimal":
                lvl = "low"
            gen_cfg["thinkingConfig"] = {"thinkingLevel": lvl}
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
            if isinstance(parsed, dict):
                parsed.setdefault("_llm_model", m_name)
            return parsed
        except Exception as e:
            last_err = e
            if "unittest" not in sys.modules:
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
        max_output_tokens=8192,
        timeout=60,
        retries=1,
        validator=lambda p: isinstance(p, dict) and isinstance(p.get("cv"), dict),
        thinking_level="low",
    )


def call_gemini_job_matches(prompt_text, model=DEFAULT_LLM_MODEL, api_key=None):
    """Call Gemini generateContent endpoint to evaluate candidate-job matches and rationales."""
    return _invoke_gemini_json(
        prompt_text,
        system_text=LLM_JOB_MATCH_SYSTEM_PROMPT,
        model=model,
        api_key=api_key,
        temperature=0.1,
        max_output_tokens=8192,
        timeout=55,
        retries=1,
        validator=lambda p: isinstance(p, dict) and isinstance(p.get("assessments"), list),
        thinking_level="low",
    )


def resolve_min_match_score(sub_cfg=None):
    """Resolve minimum AI match score (0-100) from env `MIN_MATCH_SCORE` or subscriber/global config."""
    env_val = (os.environ.get("MIN_MATCH_SCORE") or "").strip()
    raw = env_val if env_val != "" else (sub_cfg or {}).get("min_match_score", 70)
    try:
        val = int(round(float(raw)))
    except (ValueError, TypeError):
        val = 70
    return max(0, min(100, val))


def extract_cv_company_profiles(master_cv_text):
    """Parse `*Company Profile: ...*` employer descriptors from a subscriber's Markdown CV."""
    profiles = {}
    cur_co = ""
    for raw_ln in (master_cv_text or "").splitlines():
        ln = raw_ln.strip()
        if ln.startswith("### "):
            cur_co = re.sub(r"\*+", "", ln[4:].split("|")[0]).strip()
        elif cur_co and re.match(r"^\*(?!\*)\s*(?:Company Profile:?\s*)?(.+?)\*$", ln, re.I):
            m = re.match(r"^\*(?!\*)\s*(?:Company Profile:?\s*)?(.+?)\*$", ln, re.I)
            prof = sanitize_ats_text(m.group(1))
            if prof and norm(cur_co) not in profiles:
                profiles[norm(cur_co)] = prof
        elif ln.startswith("## ") and norm(ln[3:]) != "professional experience":
            cur_co = ""
    return profiles


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


def build_deterministic_match_assessment(job_item, master_cv_text, sub_cfg):
    """Build a grounded candidate-job match rationale from CV skills, past employer profiles,
    career goal, and radar company metadata when Gemini API is unconfigured or unreachable."""
    co = job_item.get("company") or "this employer"
    title = job_item.get("title") or "this opening"
    loc = job_item.get("location") or "Mexico"
    co_ind = (job_item.get("company_industry") or "").strip()
    co_desc = (job_item.get("company_description") or "").strip()
    job_blob = norm(f"{title} {co} {job_item.get('team', '')} {co_ind} {co_desc} {job_item.get('description', '')}")

    raw_skills = []
    past_cos = []
    in_comp = False
    in_exp = False
    for raw_ln in (master_cv_text or "").splitlines():
        ln = raw_ln.strip()
        if ln.startswith("## "):
            sec = ln[3:].strip().upper()
            in_comp = sec == "CORE COMPETENCIES"
            in_exp = sec == "PROFESSIONAL EXPERIENCE"
            continue
        if in_comp and (ln.startswith("- ") or ln.startswith("* ")):
            cleaned_ln = re.sub(r"\*\*[^*]+\*\*:?\s*", "", ln[2:])
            for part in re.split(r"[,;]", cleaned_ln):
                s = part.strip(" .")
                if 2 < len(s) <= 52:
                    raw_skills.append(s)
        elif in_exp and ln.startswith("### "):
            c_name = re.sub(r"\*+", "", ln[4:].split("|")[0]).strip()
            if c_name and c_name not in past_cos:
                past_cos.append(c_name)

    matched_kw = [
        s for s in dict.fromkeys(raw_skills)
        if any(w in job_blob for w in norm(s).split() if len(w) > 3)
    ][:5]
    if not matched_kw:
        matched_kw = list(dict.fromkeys(raw_skills))[:3] or ["Operations", "Leadership", "Execution"]

    is_up, up_reason = is_career_upgrade_heuristic(job_item, sub_cfg)
    score = min(95, 80 + 3 * len(matched_kw) + (2 if is_up else 0))

    co_clause = f" ({co_ind})" if co_ind else ""
    past_clause = f" across {', '.join(past_cos[:2])}" if past_cos else ""
    rationale = (
        f"Your proven track record{past_clause} in {', '.join(matched_kw[:3])} maps directly to "
        f"{co}{co_clause} for the {title} opening in {loc}."
    )
    synergy = (
        f"{co} operates in {co_ind}, aligning with your enterprise background{past_clause}."
        if co_ind
        else ""
    )
    return {
        "is_good_match": True,
        "match_score": score,
        "is_career_upgrade": is_up,
        "upgrade_reason": up_reason,
        "match_rationale": rationale,
        "industry_synergy": synergy,
        "matched_keywords": matched_kw,
        "match_source": "deterministic",
        "match_model": "deterministic-fallback",
    }


def assess_candidate_job_matches(jobs, sub_cfg, root=ROOT, companies=None, jd_cache=None):
    """Evaluate filtered job openings against the candidate's career goals, CV experience (including
    past-employer profiles), and radar company descriptions using Gemini AI.

    Returns `(kept_jobs, rejected_jobs)`:
    - Only openings that Gemini validates as genuine matches (`is_good_match=True` and
      `match_score >= min_match_score`) remain in `kept_jobs`, each enriched with
      `j['match_assessment']` explaining the rationale of the match.
    - Non-matching openings are returned in `rejected_jobs` so they are omitted from Lucy's report
      and recorded in `state/seen.json` (`rejected_matches`) to avoid daily re-evaluation.
    - If `GEMINI_API_KEY` is unset or Gemini is unreachable, falls back cleanly to deterministic
      match rationales without dropping valid regex-matched openings."""
    if not jobs:
        return [], []

    enrich_company_metadata(jobs, companies=companies, root=root)
    master_cv = load_subscriber_cv(sub_cfg, root=root)
    min_score = resolve_min_match_score(sub_cfg)

    for j in jobs:
        det = build_deterministic_match_assessment(j, master_cv, sub_cfg)
        if "is_career_upgrade" not in j:
            j["is_career_upgrade"] = det["is_career_upgrade"]
        if det["upgrade_reason"] and not j.get("upgrade_reason"):
            j["upgrade_reason"] = det["upgrade_reason"]
        j.setdefault("match_assessment", det)

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key or not master_cv or api_key in _DEAD_GEMINI_API_KEYS:
        return list(jobs), []

    # Pre-fetch full job descriptions concurrently (reusing `jd_cache` across subscribers)
    jd_cache = jd_cache if isinstance(jd_cache, dict) else {}

    def _hydrate_jd(j_item):
        k = j_item.get("key") or ""
        if k and k in jd_cache and not j_item.get("description"):
            j_item["description"] = jd_cache[k]
            return j_item["description"]
        desc = enrich_job_description(j_item)
        if k and desc:
            jd_cache[k] = desc
        return desc

    if len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            list(ex.map(_hydrate_jd, jobs))
    else:
        _hydrate_jd(jobs[0])

    career_goal = (sub_cfg.get("career_goal") or "").strip()
    goal_block = (
        f"CANDIDATE CAREER GOAL & WHAT THEY ARE LOOKING FOR:\n{career_goal}\n\n"
        if career_goal
        else "CANDIDATE CAREER GOAL & WHAT THEY ARE LOOKING FOR:\n High-impact roles matching the seniority, domain strengths, and trajectory shown in their MASTER CV.\n\n"
    )
    model = (
        (os.environ.get("GEMINI_MATCH_MODEL") or os.environ.get("GEMINI_MODEL") or "").strip()
        or sub_cfg.get("match_llm_model")
        or sub_cfg.get("llm_model", DEFAULT_LLM_MODEL)
    )

    kept_jobs = []
    rejected_jobs = []
    batch_size = 12
    for start in range(0, len(jobs), batch_size):
        chunk = jobs[start:start + batch_size]
        job_blocks = []
        for idx, j in enumerate(chunk):
            co_name = j.get("company", "")
            co_ind = j.get("company_industry") or "General Enterprise"
            co_desc = j.get("company_description") or f"{co_name} employer portal."
            jd_excerpt = (j.get("description") or "").strip()[:1100]
            if not jd_excerpt:
                jd_excerpt = "(Full job description not exposed in feed; evaluate using title, team, company industry/profile, and location.)"
            job_blocks.append(
                f"[JOB id={idx}]\n"
                f"Title: {j.get('title', '')}\n"
                f"Hiring Employer: {co_name}\n"
                f"Company Industry: {co_ind}\n"
                f"Company Profile: {co_desc}\n"
                f"Team / Department: {j.get('team', '') or 'Not specified'}\n"
                f"Location: {j.get('location', '')}\n"
                f"Job Description Excerpt: {jd_excerpt}"
            )

        prompt = (
            f"MINIMUM MATCH SCORE THRESHOLD FOR INCLUSION: {min_score} (out of 100).\n"
            f"Assess every job `[JOB id=0..{len(chunk) - 1}]` below against the candidate's career goal, "
            f"their MASTER CV (including the Company Profile context of their past employers), and each "
            f"radar company's industry & description. Set `is_good_match: false` if a job is not a strong, "
            f"genuine match for this candidate.\n\n"
            f"{goal_block}"
            f"---\nCANDIDATE MASTER CV (WITH ENHANCED PAST-COMPANY PROFILES):\n{master_cv[:6500]}\n\n"
            f"---\nCANDIDATE JOB OPENINGS TO EVALUATE ({len(chunk)}):\n\n"
            + "\n\n".join(job_blocks)
        )

        try:
            parsed = call_gemini_job_matches(prompt, model=model, api_key=api_key)
        except Exception as e:
            gha_annotate(
                "warning",
                f"Gemini job-match screening fallback for {sub_cfg.get('name', sub_cfg.get('id', 'subscriber'))} "
                f"({type(e).__name__}: {e}) — keeping deterministic matches.",
            )
            parsed = None

        if not parsed or not isinstance(parsed.get("assessments"), list):
            kept_jobs.extend(chunk)
            continue

        used_model = str(parsed.get("_llm_model") or _normalize_gemini_model(model) or DEFAULT_LLM_MODEL)
        by_id = {}
        for item in parsed.get("assessments") or []:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if not isinstance(raw_id, bool) and isinstance(raw_id, (int, str)) and str(raw_id).strip().isdigit():
                idx_val = int(str(raw_id).strip())
                if 0 <= idx_val < len(chunk):
                    by_id[idx_val] = item

        if not by_id:
            kept_jobs.extend(chunk)
            continue

        for idx, j in enumerate(chunk):
            item = by_id.get(idx)
            if item is None:
                kept_jobs.append(j)
                continue
            co_name = j.get("company", "")
            job_title = j.get("title", "")
            try:
                score_val = max(0, min(100, int(round(float(item.get("match_score", 0))))))
            except (ValueError, TypeError):
                score_val = 0
            raw_good = item.get("is_good_match")
            is_good = (bool(raw_good) if raw_good is not None else (score_val >= min_score)) and (score_val >= min_score)

            rationale = _fix_ats_employer_confusion(
                _clean_briefing_text(item.get("match_rationale") or "", limit=700),
                co_name,
                job_title,
            ) or j["match_assessment"].get("match_rationale", "")
            synergy = _fix_ats_employer_confusion(
                _clean_briefing_text(item.get("industry_synergy") or "", limit=280),
                co_name,
                job_title,
            )
            up_flag = bool(item.get("is_career_upgrade", j.get("is_career_upgrade")))
            up_reason = _fix_ats_employer_confusion(
                _clean_briefing_text(item.get("upgrade_reason") or "", limit=260),
                co_name,
                job_title,
            ) or (j.get("upgrade_reason") or "")
            kws = [
                sanitize_ats_text(k)
                for k in (item.get("matched_keywords") or [])
                if sanitize_ats_text(k)
            ][:6] or j["match_assessment"].get("matched_keywords", [])

            j["is_career_upgrade"] = up_flag
            if up_reason:
                j["upgrade_reason"] = up_reason
            j["_upgrades_classified"] = True
            j["match_assessment"] = {
                "is_good_match": is_good,
                "match_score": score_val,
                "is_career_upgrade": up_flag,
                "upgrade_reason": up_reason,
                "match_rationale": rationale,
                "industry_synergy": synergy,
                "matched_keywords": kws,
                "match_source": "gemini",
                "match_model": used_model,
            }
            if is_good:
                kept_jobs.append(j)
            else:
                rejected_jobs.append(j)

    return kept_jobs, rejected_jobs


def classify_career_upgrades(jobs, sub_cfg, root=ROOT):
    """Label roles in `jobs` with `is_career_upgrade` and `upgrade_reason`.

    First applies fast seniority/trajectory rules (`upgrade_title_include`), then—when
    `GEMINI_API_KEY` is configured and there are multiple roles—asks Gemini in a single
    compact batch call to refine which roles are true 'Career Upgrade Moves' (step-up
    challenge vs. lateral match) against the subscriber's Master CV."""
    if not jobs:
        return
    if all(j.get("_upgrades_classified") for j in jobs):
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
        for j in jobs:
            j["_upgrades_classified"] = True
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
    for j in jobs:
        j["_upgrades_classified"] = True


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
    characters (< > [ ] " { } \\) across all header, summary, skill, experience (including
    `company_context`), education, and language fields."""
    if not isinstance(cv_data, dict):
        return {}
    td = sanitize_ats_text(cv_data.get("target_direction") or "")
    td = re.sub(r"^(Target Role Direction:\s*)\(([^()]+)\)$", r"\1\2", td)

    clean_exp = []
    for exp in cv_data.get("experience") or []:
        if not isinstance(exp, dict):
            continue
        bullets = [sanitize_ats_text(b) for b in (exp.get("bullets") or [])]
        entry = {
            "company": sanitize_ats_text(exp.get("company") or ""),
            "role": sanitize_ats_text(exp.get("role") or ""),
            "location": sanitize_ats_text(exp.get("location") or ""),
            "dates": sanitize_ats_text(exp.get("dates") or ""),
            "bullets": [b for b in bullets if b],
        }
        comp_ctx = sanitize_ats_text(exp.get("company_context") or "")
        if comp_ctx:
            entry["company_context"] = comp_ctx
        clean_exp.append(entry)

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
    enrich_company_metadata([job_item])
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
    co_ind = job_item.get("company_industry", "")
    co_desc = job_item.get("company_description", "")
    job_blob = norm(
        f"{job_title} {company} {job_item.get('team', '')} {co_ind} {co_desc} {job_item.get('description', '')}"
    )

    # Extract skills from CORE COMPETENCIES
    raw_skills = []
    for ln in sections.get("CORE COMPETENCIES", []):
        if ln.startswith("- ") or ln.startswith("* "):
            cleaned_ln = re.sub(r"\*\*[^*]+\*\*:?\s*", "", ln[2:])
            for part in re.split(r"[,;]", cleaned_ln):
                s = part.strip(" .")
                if 2 < len(s) <= 52:
                    raw_skills.append(s)
    # Sort skills so those matching the target job & industry appear first for ATS keyword density
    raw_skills.sort(key=lambda s: (0 if any(w in job_blob for w in norm(s).split() if len(w) > 3) else 1))
    core_skills = list(dict.fromkeys(raw_skills))[:10] or ["Executive Leadership", "Operations", "Strategy"]
    matched_kw = [s for s in core_skills if any(w in job_blob for w in norm(s).split() if len(w) > 3)][:6] or core_skills[:4]

    # Parse PROFESSIONAL EXPERIENCE blocks (### Company | Location + *Company Profile: ...* + **Role** | Dates + bullets)
    experience = []
    cur_exp = None
    for ln in sections.get("PROFESSIONAL EXPERIENCE", []):
        if ln.startswith("### "):
            parts = [p.strip() for p in ln[4:].split("|")]
            cur_exp = {
                "company": re.sub(r"\*+", "", parts[0]),
                "company_context": "",
                "location": parts[1] if len(parts) > 1 else "",
                "role": "",
                "dates": "",
                "bullets": [],
            }
            experience.append(cur_exp)
        elif cur_exp is not None and re.match(r"^\*(?!\*)\s*(?:Company Profile:?\s*)?(.+?)\*$", ln, re.I):
            m_prof = re.match(r"^\*(?!\*)\s*(?:Company Profile:?\s*)?(.+?)\*$", ln, re.I)
            cur_exp["company_context"] = m_prof.group(1).strip()
        elif cur_exp is not None and ln.startswith("**") and ("|" in ln or "–" in ln or "-" in ln):
            parts = [p.strip() for p in ln.split("|")]
            role_txt = re.sub(r"\*+", "", parts[0]).strip()
            if cur_exp["role"] and cur_exp["bullets"]:
                cur_exp = {
                    "company": cur_exp["company"],
                    "company_context": "",
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
    pre_match = job_item.get("match_assessment") or {}
    score = int(pre_match["match_score"]) if isinstance(pre_match.get("match_score"), (int, float)) else min(96, 84 + 2 * len(matched_kw))
    is_up, up_reason = is_career_upgrade_heuristic(job_item, sub_cfg)
    if "is_career_upgrade" in pre_match:
        is_up = bool(pre_match["is_career_upgrade"])
        up_reason = pre_match.get("upgrade_reason") or up_reason
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
    ind_note = f" ({co_ind})" if co_ind else ""
    why_txt = pre_match.get("match_rationale") or (
        f"Your background in {', '.join(matched_kw[:3])} aligns directly with {company}{ind_note}'s "
        f"{job_title} opening in {job_item.get('location') or 'Mexico'}."
    )
    return {
        "tailor_source": "deterministic",
        "tailor_model": "deterministic-fallback",
        "match_score": score,
        "is_career_upgrade": is_up,
        "upgrade_reason": up_reason,
        "lucy_why_it_fits": why_txt,
        "matched_keywords": pre_match.get("matched_keywords") or matched_kw,
        "gaps_to_address": [f"Emphasize measurable impact relevant to {company}'s {job_title} scope."],
        "cv": cv_payload,
    }


def tailor_cv_with_llm(job_item, master_cv_text, sub_cfg):
    """Analyze job vs. master CV using Gemini and return structured match + ATS-ready CV."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not master_cv_text:
        return None

    enrich_company_metadata([job_item])
    co_name = job_item.get("company", "")
    job_title = job_item.get("title", "")
    co_ind = (job_item.get("company_industry") or "").strip()
    co_desc = (job_item.get("company_description") or "").strip()

    jd_text = enrich_job_description(job_item)
    if not jd_text:
        jd_text = (
            f"Role Title: {job_title}\n"
            f"Company: {co_name}\n"
            f"Company Industry: {co_ind or 'Enterprise'}\n"
            f"Company Profile: {co_desc or co_name}\n"
            f"Department/Team: {job_item.get('team', 'General')}\n"
            f"Location: {job_item.get('location', '')}\n"
            "(Full description text not exposed in feed; tailor CV to role title, company industry/profile, and level.)"
        )

    career_goal = (sub_cfg.get("career_goal") or "").strip()
    goal_section = (
        f"CANDIDATE CAREER GOAL & DIRECTION (align `target_direction`, `summary`, `core_skills`, `company_context`, and bullet emphasis truthfully toward this trajectory):\n{career_goal}\n\n---\n"
        if career_goal
        else ""
    )
    pre_match = job_item.get("match_assessment") or {}
    pre_match_section = ""
    if pre_match.get("match_rationale"):
        pre_match_section = (
            f"VALIDATED MATCH RATIONALE (reinforce this fit in `lucy_why_it_fits` and CV framing):\n"
            f"{pre_match['match_rationale']}\n"
            + (f"Industry Synergy: {pre_match['industry_synergy']}\n" if pre_match.get("industry_synergy") else "")
            + "\n---\n"
        )

    co_profile_lines = ""
    if co_ind:
        co_profile_lines += f"Company Industry: {co_ind}\n"
    if co_desc:
        co_profile_lines += f"Company Profile & Business Context: {co_desc}\n"

    prompt = (
        f"TARGET JOB POSTING:\n"
        f"Title: {job_title}\n"
        f"Hiring Employer (Company Name): {co_name}\n"
        f"{co_profile_lines}"
        f"(IMPORTANT: The hiring employer is '{co_name}'. Never confuse '{co_name}' with its recruiting portal/ATS system such as Workday, Greenhouse, Lever, SmartRecruiters, Oracle HCM, or Eightfold.)\n"
        f"Location: {job_item.get('location', '')}\n"
        f"Team: {job_item.get('team', '')}\n"
        f"Job Description:\n{jd_text}\n\n"
        f"---\n"
        f"{goal_section}"
        f"{pre_match_section}"
        f"CANDIDATE MASTER CV (WITH ENHANCED COMPANY PROFILES):\n{master_cv_text}"
    )
    model = (os.environ.get("GEMINI_MODEL") or "").strip() or sub_cfg.get("llm_model", DEFAULT_LLM_MODEL)
    cv_company_profiles = extract_cv_company_profiles(master_cv_text)
    try:
        raw_res = call_gemini_json(prompt, model=model, api_key=api_key)
        if raw_res and isinstance(raw_res.get("cv"), dict):
            res = copy.deepcopy(raw_res)
            used_model = res.pop("_llm_model", None) or _normalize_gemini_model(model) or DEFAULT_LLM_MODEL
            res["tailor_source"] = "gemini"
            res["tailor_model"] = used_model
            if res.get("lucy_why_it_fits"):
                res["lucy_why_it_fits"] = _fix_ats_employer_confusion(res["lucy_why_it_fits"], co_name, job_title)
            elif pre_match.get("match_rationale"):
                res["lucy_why_it_fits"] = pre_match["match_rationale"]
            if res.get("upgrade_reason"):
                res["upgrade_reason"] = _fix_ats_employer_confusion(res["upgrade_reason"], co_name, job_title)
            if res["cv"].get("summary"):
                res["cv"]["summary"] = _fix_ats_employer_confusion(res["cv"]["summary"], co_name, job_title)
            seen_exp_cos = set()
            for exp_item in res["cv"].get("experience") or []:
                if not isinstance(exp_item, dict):
                    continue
                c_key = norm(exp_item.get("company") or "").strip()
                if exp_item.get("company_context"):
                    exp_item["company_context"] = _fix_ats_employer_confusion(
                        exp_item["company_context"], co_name, job_title
                    )
                elif c_key and c_key not in seen_exp_cos and c_key in cv_company_profiles:
                    exp_item["company_context"] = cv_company_profiles[c_key]
                if c_key:
                    seen_exp_cos.add(c_key)
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

    # 7. Professional Experience (Reverse-Chronological, Employer Context + CAR/XYZ Impact Bullets)
    if experiences:
        paragraphs.append(section_header_xml("PROFESSIONAL EXPERIENCE"))
        for exp in experiences:
            role_str = exp.get("role") or ""
            comp_str = exp.get("company") or ""
            comp_ctx = exp.get("company_context") or ""
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
            paragraphs.append(
                p_xml(head_runs, before=85, after=15 if comp_ctx else 30, line=240, keep_next=True)
            )
            if comp_ctx:
                paragraphs.append(
                    p_xml([run_xml(comp_ctx, sz=17, italic=True, color="52525B")], before=0, after=30, line=235, keep_next=True)
                )

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

    pages_ops = [[]]
    y = 750

    def add_line(txt, font="F1", size=9.5, leading=13, x=48):
        nonlocal y
        if y < 52:
            pages_ops.append([])
            y = 750
        pages_ops[-1].append(f"BT /{font} {size} Tf 1 0 0 1 {x} {y:.1f} Tm ({pdf_str(txt)}) Tj ET")
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
    pages_ops[-1].append(f"0.7 w 48 {y + 6:.1f} m 564 {y + 6:.1f} l S")
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
            if exp.get("company_context"):
                add_wrapped(exp["company_context"], width=95, font="F1", size=8.4, leading=11.5, x=48)
            for b in exp.get("bullets") or []:
                if b:
                    add_wrapped(b, width=92, font="F1", size=9.0, leading=12, x=56, prefix="- ")
            y -= 3

    languages = [s for s in (cv_data.get("languages") or []) if s]
    if cv_data.get("education") or languages:
        add_line("EDUCATION, CERTIFICATIONS & LANGUAGES", font="F2", size=9.5, leading=13)
        for edu in cv_data.get("education") or []:
            line_edu = f"{edu.get('degree', '')} - {edu.get('institution', '')}"
            if edu.get("dates"):
                line_edu += f" ({edu.get('dates')})"
            add_wrapped(line_edu, width=95, font="F1", size=9.0, leading=12.5)
        if languages:
            add_wrapped("Languages: " + " | ".join(languages), width=95, font="F1", size=9.0, leading=12.5)

    kid_refs = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(pages_ops)))
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        f"2 0 obj\n<< /Type /Pages /Kids [{kid_refs}] /Count {len(pages_ops)} >>\nendobj\n".encode("ascii"),
        b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\nendobj\n",
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>\nendobj\n",
    ]
    for i, p_ops in enumerate(pages_ops):
        page_id = 5 + 2 * i
        content_id = 6 + 2 * i
        stream_bytes = "\n".join(p_ops).encode("latin-1", errors="replace")
        objs.append(
            f"{page_id} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>\nendobj\n".encode("ascii")
        )
        objs.append(
            f"{content_id} 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"\nendstream\nendobj\n"
        )
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
        ctx_part = (
            f"<div class='company-ctx'>{esc(exp.get('company_context', ''))}</div>"
            if exp.get("company_context")
            else ""
        )
        exp_blocks.append(f"""
        <div class="job">
          <div class="job-head">
            <div><span class="role">{esc(exp.get('role', ''))}</span> &mdash; <span class="company">{esc(exp.get('company', ''))}</span>{loc_part}</div>
            <div class="dates">{esc(exp.get('dates', ''))}</div>
          </div>
          {ctx_part}
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
        an_meta = job_item.get("llm_analysis") if isinstance(job_item, dict) else None
        src_badge = ""
        if isinstance(an_meta, dict):
            if an_meta.get("tailor_source") == "gemini":
                src_badge = f" (Gemini AI &middot; {esc(str(an_meta.get('tailor_model') or DEFAULT_LLM_MODEL))})"
            elif an_meta.get("tailor_source") == "deterministic":
                src_badge = " (Deterministic ATS Fallback)"
        target_note = (
            f"Tailored by Lucy{src_badge} for: {esc(sanitize_ats_text(job_item.get('title', '')))} "
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
  .company-ctx {{ font-size: 8.8pt; font-style: italic; color: #52525b; margin: 1px 0 3px; line-height: 1.35; }}
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
    ext_clean = re.sub(r"[^a-zA-Z0-9]+", "", str(ext or "docx").lstrip(".")) or "docx"
    return f"{clean[:60]}.{ext_clean}"


# --------------------------------------------------------------------------
# output & daily email notifications
# --------------------------------------------------------------------------

# TODO(security): Configure strict nonce-based CSP and X-Frame-Options HTTP headers at the CDN/GitHub Pages proxy layer; static HTML file uses strict DOM textContent/createElement rendering only.
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
.headbar{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.sub{color:var(--mute);font-size:.875rem;margin:0}
.joinbtn{display:inline-block;padding:7px 13px;border-radius:999px;border:1px solid var(--accent);color:var(--accent);text-decoration:none;font-size:.8125rem;font-weight:600;white-space:nowrap}
.joinbtn:hover{background:var(--accent);color:var(--card)}
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
.cd{color:var(--mute);font-size:.8125rem;margin-top:4px;line-height:1.4}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.b{font-size:.75rem;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mute)}
.b.ind{color:var(--accent);border-color:var(--accent)}
.b.new{background:var(--newbg);color:var(--new);border-color:transparent;font-weight:600}
.empty{color:var(--mute);padding:32px 0;text-align:center}
</style>
</head>
<body>
<main>
<div class="headbar">
<div>
<h1>__TITLE__</h1>
<p class="sub">Updated __UPDATED__</p>
</div>
<a class="joinbtn" href="onboard.html">&#10022; Join Radar / Onboard &rarr;</a>
</div>
<div class="controls">
<input id="q" type="search" placeholder="Search title, company, industry, team" aria-label="Search">
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
    (!q || (j.title + " " + j.company + " " + j.team + " " + j.location + " " + (j.company_industry || "") + " " + (j.company_description || "")).toLowerCase().includes(q)));
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
    if (j.company_industry) add(j.company_industry, "ind");
    add(LOC[j.loc_class] || j.loc_class);
    add((j.posted_at ? "Posted " : "First seen ") + when(daysAgo(j.posted_at || j.first_seen)));
    if (j.stale) add("last refresh failed");
    if (j.company_description) {
      const cd = document.createElement("div");
      cd.className = "cd"; cd.textContent = j.company_description;
      a.append(t, m, cd, b);
    } else {
      a.append(t, m, b);
    }
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


def job_signature(j):
    """Stable (company, normalized title, team) signature so the same role is recognized when it
    reappears under a different requisition id or as a city clone, and is never emailed twice."""
    co_k = norm((j or {}).get("company", "")).strip()
    title_k = re.sub(r"[^a-z0-9]+", " ", norm((j or {}).get("title", ""))).strip()
    team_k = norm((j or {}).get("team", "")).strip()
    return f"{co_k}|{title_k}|{team_k}"


def resolve_quiet_day_mode(cfg):
    """What to send when zero never-seen roles exist today:
    'note' (default): a short Lucy briefing with NO job list (nothing is ever re-sent),
    'spotlight': legacy behaviour re-listing up to `quiet_spotlight_count` unclaimed roles,
    'skip': no email at all on quiet days."""
    mode = str((cfg or {}).get("quiet_day_mode") or "").strip().lower()
    if mode in ("note", "spotlight", "skip"):
        return mode
    if (cfg or {}).get("daily_top_when_quiet"):
        return "spotlight"
    return "note"


def ensure_emailed_state(sub_state):
    """Return the subscriber's `emailed` ledger (key -> {date, sig}). On first run after upgrading
    from a version without the ledger, seed it with every already-known key so long-time
    subscribers are never re-sent roles they received before the ledger existed."""
    if not isinstance(sub_state, dict):
        return {}
    emailed = sub_state.get("emailed")
    if not isinstance(emailed, dict):
        emailed = {}
        if sub_state.get("initialized"):
            for k, rec in (sub_state.get("jobs") or {}).items():
                if isinstance(rec, dict):
                    emailed[k] = {"date": rec.get("first_seen") or "", "sig": "", "legacy": True}
        sub_state["emailed"] = emailed
    return emailed


def ensure_rejected_state(sub_state):
    """Return the subscriber's `rejected_matches` ledger (key -> {date, sig, score}) storing
    openings vetoed by Gemini's candidate-job match gate so they are never re-evaluated daily."""
    if not isinstance(sub_state, dict):
        return {}
    rejected = sub_state.get("rejected_matches")
    if not isinstance(rejected, dict):
        rejected = {}
        sub_state["rejected_matches"] = rejected
    return rejected


def record_rejected_jobs(sub_state, jobs_rejected, today, retention_days=120):
    """Persist AI-vetoed non-matching openings (key + signature) so subsequent daily runs skip them
    without burning Gemini API quota; prune entries after `retention_days`."""
    if not isinstance(sub_state, dict):
        return
    rejected = ensure_rejected_state(sub_state)
    for j in jobs_rejected or []:
        if j.get("key"):
            an = j.get("match_assessment") or {}
            rejected[j["key"]] = {
                "date": today.isoformat(),
                "sig": job_signature(j),
                "score": an.get("match_score", 0),
            }
    known = sub_state.get("jobs") if isinstance(sub_state.get("jobs"), dict) else {}
    for k in list(rejected):
        rec = rejected.get(k)
        if not isinstance(rec, dict):
            del rejected[k]
            continue
        if k in known:
            continue
        rej_on = _safe_iso_date(rec.get("date"), today)
        if (today - rej_on).days > retention_days:
            del rejected[k]


def record_emailed_jobs(sub_state, jobs_sent, today, retention_days=120):
    """Mark delivered roles (key + signature) so they are never emailed to this subscriber again;
    prune ledger entries for roles that disappeared from the radar longer than `retention_days` ago."""
    if not isinstance(sub_state, dict):
        return
    emailed = ensure_emailed_state(sub_state)
    for j in jobs_sent or []:
        if j.get("key"):
            emailed[j["key"]] = {"date": today.isoformat(), "sig": job_signature(j)}
    known = sub_state.get("jobs") if isinstance(sub_state.get("jobs"), dict) else {}
    for k in list(emailed):
        rec = emailed.get(k)
        if not isinstance(rec, dict):
            del emailed[k]
            continue
        if k in known:
            continue
        sent_on = _safe_iso_date(rec.get("date"), today)
        if (today - sent_on).days > retention_days:
            del emailed[k]


def select_jobs_for_email(jobs, baseline, cfg, sub_state=None):
    """Choose which roles go into a subscriber's email candidate pool: ONLY active roles that have
    never been emailed OR vetoed by AI match review (by key or by company+title signature). Newly
    discovered roles come first; overflow roles beyond `max_email_jobs` carry over until delivered.
    `email_mode: all` (manual --email-all runs) re-evaluates and sends every active match."""
    mode = cfg.get("email_mode", "new_or_discovered")
    active = [j for j in jobs if not j.get("stale")]
    if mode == "all":
        return active
    if baseline and not cfg.get("email_on_baseline", True):
        return []
    emailed = (sub_state or {}).get("emailed")
    emailed = emailed if isinstance(emailed, dict) else {}
    rejected = (sub_state or {}).get("rejected_matches")
    rejected = rejected if isinstance(rejected, dict) else {}
    skip_sigs = {
        v.get("sig")
        for ledger in (emailed, rejected)
        for v in ledger.values()
        if isinstance(v, dict) and v.get("sig")
    }
    never_sent = [
        j for j in active
        if j.get("key") not in emailed
        and j.get("key") not in rejected
        and job_signature(j) not in skip_sigs
    ]
    if mode == "discovered_only" and not baseline:
        never_sent = [j for j in never_sent if j.get("discovered_now")]
    return never_sent


def _job_match_score(j):
    an = j.get("llm_analysis") or j.get("match_assessment") or {}
    sc = an.get("match_score")
    return int(sc) if isinstance(sc, (int, float)) else 0


def cap_email_jobs(jobs, cfg, today):
    """Keep the email digestible: at most `max_email_jobs` roles (brand-new first, then Career
    Upgrade Moves, highest AI match score, hot postings, recency). Returns (shown, overflow_count);
    overflow roles carry over to the next briefing automatically because they are not recorded as emailed."""
    limit = int(cfg.get("max_email_jobs", 12) or 0)
    if limit <= 0 or len(jobs) <= limit:
        return list(jobs), 0
    ranked = sorted(
        jobs,
        key=lambda j: (
            not bool(j.get("discovered_now")),
            not bool(j.get("is_career_upgrade") or is_career_upgrade_heuristic(j, cfg)[0]),
            -_job_match_score(j),
            not job_age_info(j, today)[1],
            job_age_info(j, today)[0],
            j.get("company", "").lower(),
        ),
    )
    return ranked[:limit], len(jobs) - limit


def select_quiet_spotlight(jobs, cfg, sub_state=None):
    """Legacy quiet-day behaviour (`quiet_day_mode: spotlight`): rotate through up to
    `quiet_spotlight_count` active matches not spotlighted in recent quiet runs and never AI-rejected."""
    rejected = (sub_state or {}).get("rejected_matches")
    rejected = rejected if isinstance(rejected, dict) else {}
    rej_sigs = {v.get("sig") for v in rejected.values() if isinstance(v, dict) and v.get("sig")}
    active = [
        j for j in jobs
        if not j.get("stale") and j.get("key") not in rejected and job_signature(j) not in rej_sigs
    ]
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


def render_subscriber_board(jobs, sub_cfg, stamp):
    """Self-contained personal board (HTML) with every active match for one subscriber."""
    clean = strip_transient_fields([j for j in jobs if not j.get("stale")])
    title = f"{sub_cfg.get('name') or sub_cfg.get('id') or 'Your'} · Lucy Personal Board"
    return render_page(clean, {"page_title": title}, stamp)


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


DAILY_MOTIVATION_SYSTEM_PROMPT = """You are Lucy: a private, elite executive headhunter who chose a side. You work for exactly ONE candidate (your partner), never for the companies. Every morning you write them a personal 45-second briefing.

THE STORY WE ARE LIVING ("Us vs. Their Machine"):
- For years, large employers have pointed AI at candidates. Applicant tracking systems (Workday, Greenhouse, Oracle, Eightfold, SmartRecruiters, Lever, Ashby, Workable, BambooHR) parse, score, rank and filter CVs before any human reads a name. Generic CVs lose silently and the candidate never learns why.
- That asymmetry ended the day we teamed up. Now the candidate has an AI of their own: overnight we sweep more than 100 employer career portals across 9 ATS platforms, discard 99% of the noise, read each target posting, and rebuild the candidate's REAL track record in the exact dialect each screening system scores highest, then attach ATS-ready Word CVs so they wake up to pure leverage.
- We do not break their system; we decode it. We turned their own filter into our front door. Their robot became our doorman. That is the partner image: two of us, shoulder to shoulder, playing offense against a machine that was designed to say no.

YOUR VOICE AND PSYCHOLOGY:
1. PARTNER, NOT TOOL. Always "we / us / our". You did real work last night and you say so specifically (portals swept, roles surfaced, CVs forged). Warm, loyal, sharp-witted, a little conspiratorial. Never corporate, never generic, never a cheerleader shouting slogans.
2. DOPAMINE THROUGH TRUTHFUL MOMENTUM. Make the morning feel electric: one fresh insight, visible progress (edition, streak, cumulative wins together) and one tiny win available right now. Feed autonomy (their choice), competence (their proven wins) and relatedness (our bond).
3. SEEN, NOT FLATTERED. Anchor every note in their REAL craft from the identity snapshot, their career goal, today's spotlight employer and role, the weekday rhythm and today's chapter theme. Use the candidate's first name naturally once or twice.
4. CONTINUITY. You remember yesterday. Reference the STORY SO FAR lightly (what we targeted, the last micro-challenge) with curiosity and zero guilt. Never scold, never pressure, never shame.
5. RADICAL HONESTY. Never invent statistics, companies, product features or promises; use only the facts in the briefing data. We never lie on a CV and we never advise it: we make the truth machine-readable. FORBIDDEN vocabulary: hack, hacking, cheat, trick the system, lying, fake, fabricate, exploit, game the system, deceive. PREFERRED vocabulary: decode, translate, reverse-engineer, speak their machine's language, pass their filter truthfully, their filter is our front door.
6. EMPLOYER NAMES. Always use the exact Spotlight Hiring Employer name. Never call the employer by its ATS platform (Workday, Greenhouse, Oracle, Eightfold...). The platform is THEIR tool; the employer is the target.
7. ZERO REPETITION. Never reuse a hook, metaphor or phrase from RECENT_HEADLINES_TO_AVOID or the STORY SO FAR. Surprise them every single morning.
8. FORMAT. Write in the requested language. Plain text only: no markdown, no emojis, no quotation marks inside values, and never the characters < > [ ] { } or backslash.

Return ONLY valid JSON with exactly these keys:
{
  "subject_spark": "3-6 word curiosity hook for the Gmail subject line",
  "chapter_title": "3-6 word title of today's chapter in our story",
  "headline": "4-9 word bold manifesto headline for the dark card",
  "message": "2-4 vivid sentences (50-90 words) to the candidate by first name: today's chapter theme + their real craft + the spotlight employer + what we did overnight, ending in a confidence surge rather than a lecture",
  "their_ai_vs_our_ai": "1-2 sentences (25-45 words) of honest tactical intel: how the spotlight employer's screening tool reads a CV and exactly how today's attached CV is built to pass it truthfully",
  "connection_ps": "1 warm partner sentence (18-32 words) from Lucy: what you did last night for them, our edition or streak together, why you have their back, with light continuity to yesterday when relevant",
  "micro_challenge": "1 concrete zero-friction 60-second action (15-28 words) for right now that names the spotlight employer and the attached CV",
  "partner_signoff": "3-8 word sign-off in Lucy's voice",
  "story_beat": "12-22 word third-person memory note summarizing today's chapter for tomorrow's continuity"
}"""

# Narrative arc phases keyed by briefing edition: (min_edition, max_edition, title, guidance for Lucy)
PARTNERSHIP_ARC = [
    (1, 3, "Chapter I: First Contact",
     "We just met. Explain who you are, what you did overnight, and why the odds changed: their AI has been filtering candidates for years and now the candidate has an AI of their own."),
    (4, 10, "Chapter II: Building the Habit",
     "Reps and rhythm. Celebrate the streak, make the 60-second play feel like a ritual, and show how every application compounds into real market intelligence."),
    (11, 20, "Chapter III: Optionality and Leverage",
     "Confidence from pipeline. Talk posture, negotiating power and interviewing from strength; their filter now works for us."),
    (21, 10 ** 9, "Chapter IV: The Operator",
     "Mastery. The candidate is now the kind of leader the market bids for; keep sharpening the edge and keep the partnership alive with gratitude, precision and wit."),
]

# Honest, high-level intel on how each supported ATS reads a CV (the "their tool" half of the story).
ATS_INTEL = {
    "workday": ("Workday", "Workday parses every CV into structured fields (titles, skills, dates) and lets recruiters filter and keyword-search the candidate pool, so clean single-column text with the exact skill terms gets parsed and found"),
    "greenhouse": ("Greenhouse", "Greenhouse indexes the CV text for recruiter keyword searches and structured scorecards, so truthfully mirroring the posting's vocabulary puts a profile at the top of their own search results"),
    "lever": ("Lever", "Lever indexes resume text for recruiter searches and tags, so precise role and skill terminology is what makes a profile surface"),
    "ashby": ("Ashby", "Ashby parses CVs against structured job requirements and recruiter filters, so clarity of titles, scope and metrics drives the match"),
    "workable": ("Workable", "Workable's AI screening scores candidates against the job requirements and highlights keyword overlap, so a truthful mirror of the posting's language lifts the score"),
    "smartrecruiters": ("SmartRecruiters", "SmartRecruiters applies AI match scoring between the CV's skills and the posting, so exact verifiable skill terms raise the score before a human looks"),
    "oracle_hcm": ("Oracle Recruiting Cloud", "Oracle Recruiting Cloud auto-parses the resume into a candidate profile and ranks by requirement match, so clean parsing and exact competencies decide who ranks"),
    "eightfold": ("Eightfold AI", "Eightfold uses AI talent-matching that infers skills from career history and scores fit against the role's skill graph, so explicit quantified skills in context feed its model"),
    "bamboohr": ("BambooHR", "BambooHR's applicant tracking relies on recruiter keyword filters and status pipelines, so precise titles and skills keep a profile in the shortlist"),
}
GENERIC_ATS_INTEL = "Enterprise screening tools parse a CV into fields, score keyword and seniority overlap with the posting, and rank candidates before a recruiter reads a name"

LUCY_SIGNOFFS = [
    "Shoulder to shoulder, always. Lucy",
    "Same side of the table. Lucy",
    "Your radar never sleeps. Lucy",
    "Their filter, our front door. Lucy",
    "On watch while you win. Lucy",
    "Two of us, one machine to beat. Lucy",
    "Decoded, armed, ready. Lucy",
    "Until tomorrow's sweep. Lucy",
]

_BRIEFING_BANNED_RE = re.compile(
    r"\b(hack(?:s|ed|ing|er|ers)?|cheat(?:s|ed|ing)?|lying|fake(?:s|d)?|fabricat\w*|exploit\w*|deceiv\w*|dishonest\w*|"
    r"game the system|trick(?:s|ed|ing)? (?:the|their|an?|its|your)\b)",
    re.I,
)
_BRIEFING_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "your", "you", "we", "our", "us", "is", "are",
    "it", "its", "their", "this", "that", "with", "at", "by", "from", "into", "today", "now", "not", "no", "be",
}
BRIEFING_TEXT_KEYS = (
    "subject_spark", "chapter_title", "headline", "message", "their_ai_vs_our_ai",
    "connection_ps", "micro_challenge", "partner_signoff", "story_beat",
)


def _briefing_tokens(text):
    return {t for t in re.findall(r"[a-z0-9']+", norm(text)) if t not in _BRIEFING_STOPWORDS and len(t) > 2}


def _briefing_pack_quality(parsed, first_name="", recent_headlines=None):
    """Quality gate for Gemini-written briefings. Returns (ok, rejection_reason)."""
    if not isinstance(parsed, dict):
        return False, "the response was not a JSON object"
    headline = str(parsed.get("headline") or "").strip()
    message = str(parsed.get("message") or "").strip()
    if not headline or not message:
        return False, "headline or message was empty"
    hw = len(headline.split())
    mw = len(message.split())
    if hw > 16:
        return False, f"the headline has {hw} words (maximum 9)"
    if mw < 25 or mw > 150:
        return False, f"the message has {mw} words (target 50-90)"
    all_text = " ".join(str(parsed.get(k) or "") for k in BRIEFING_TEXT_KEYS)
    if "{" in all_text or "}" in all_text:
        return False, "it contained unfilled template braces"
    m = _BRIEFING_BANNED_RE.search(all_text)
    if m:
        return False, (
            f"it used forbidden vocabulary '{m.group(0)}'; we decode and translate truthfully, "
            "we never hack, cheat, fake or deceive"
        )
    ht = _briefing_tokens(headline)
    for prev in (recent_headlines or [])[-30:]:
        pt = _briefing_tokens(prev)
        if norm(headline) == norm(prev):
            return False, f"the headline repeats a recent one ('{prev}')"
        if ht and pt and len(ht & pt) / len(ht | pt) >= 0.6:
            return False, f"the headline is too similar to the recent headline '{prev}'"
    return True, ""


def _clean_briefing_text(text, limit=600):
    """Strip ATS-unsafe / markup characters and collapse whitespace in Gemini-written prose."""
    s = re.sub(r"[<>\[\]{}\\*`]", "", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip().strip('"').strip()
    return s[:limit]


def partnership_arc_for(edition):
    """Return (title, guidance) for the narrative arc phase of this briefing edition."""
    for lo, hi, title, guidance in PARTNERSHIP_ARC:
        if lo <= int(edition or 1) <= hi:
            return title, guidance
    return PARTNERSHIP_ARC[-1][2], PARTNERSHIP_ARC[-1][3]


def ats_intel_for(job_item):
    """Return (platform display name, honest one-line intel) for the job's ATS, or a generic fallback."""
    ats_key = str((job_item or {}).get("ats") or "").lower()
    if ats_key in ATS_INTEL:
        return ATS_INTEL[ats_key]
    return "their applicant tracking system", GENERIC_ATS_INTEL


def compose_partner_briefing_prompt(
    sub_cfg, top_job, rel_ctx, today, beat, cadence, cv_snapshot,
    upgrade_count, hot_count, total_count, cvs_today_count, is_quiet_spotlight, language="English",
    quiet_day=False, active_count=None, ai_rejected_count=0,
):
    """Assemble the fully grounded 'Us vs. Their Machine' briefing prompt for Gemini."""
    first_name = ((sub_cfg or {}).get("name") or "Candidate").split()[0]
    edition = int(rel_ctx.get("edition", 1))
    b_cnt = rel_ctx.get("board_count", 109)
    arc_title, arc_guidance = partnership_arc_for(edition)
    cadence_title, cadence_subline = cadence
    beat_title, beat_insight = beat
    if top_job:
        enrich_company_metadata([top_job])
    top_co = (top_job or {}).get("company") or "a top employer"
    top_co_ind = ((top_job or {}).get("company_industry") or "").strip()
    top_co_desc = ((top_job or {}).get("company_description") or "").strip()
    top_title = (top_job or {}).get("title") or "a step-up role"
    top_loc = (top_job or {}).get("location") or "Jalisco / Remote Mexico"
    ats_name, ats_intel = ats_intel_for(top_job)
    cv_file = (top_job or {}).get("cv_filename") or ""
    career_goal = ((sub_cfg or {}).get("career_goal") or "").strip()
    milestone = rel_ctx.get("milestone_badge") or ""
    beats = [b for b in (rel_ctx.get("recent_story_beats") or []) if b][-5:]
    story_lines = "\n".join(f"- {b}" for b in reversed(beats)) if beats else "- (none yet: this is our very first briefing together)"
    avoid_list = [h for h in (rel_ctx.get("recent_headlines") or []) if h][-14:]
    avoid_txt = "; ".join(f"'{h}'" for h in avoid_list) if avoid_list else "(none yet)"
    last_chal = rel_ctx.get("last_micro_challenge") or ""
    last_co = rel_ctx.get("last_spotlight_company") or ""
    if quiet_day:
        mode_note = (
            "QUIET DAY: zero never-seen postings matched overnight, so today's email lists NO roles and re-sends nothing "
            "(we never repeat a role). Keep the connection alive briefly and honestly: acknowledge the quiet market without drama, "
            "reinforce that the radar kept sweeping and will keep sweeping, and make the micro-challenge a pipeline-hygiene move "
            "(follow up on a pending application, sharpen the LinkedIn headline, message one contact) instead of applying."
        )
    elif is_quiet_spotlight:
        mode_note = "Quiet-market momentum spotlight: zero brand-new postings dropped overnight, so you hand-picked their top unclaimed Career Upgrade Moves still live on the board to keep daily momentum unbroken."
    else:
        mode_note = "Fresh market drop: never-seen openings matched overnight (every role listed today is AI-validated for true career & CV fit and new to the candidate; we never repeat a role)."
    cv_line = (
        f"{cvs_today_count} tailored Word CV(s) attached, each rebuilt by our Gemini AI from the real master CV plus the specific posting"
        if cvs_today_count
        else "no tailored CV attached today"
    )
    if quiet_day or not top_job:
        spotlight_block = (
            "Spotlight role: none today (quiet day). Do NOT invent an employer or a role.\n"
            f"Their screening tools in general: {GENERIC_ATS_INTEL}.\n"
        )
    else:
        co_ctx_line = ""
        if top_co_ind or top_co_desc:
            co_ctx_line = f"Spotlight employer industry & profile: {top_co_ind + ' — ' if top_co_ind else ''}{top_co_desc}\n"
        spotlight_block = (
            f"Spotlight hiring employer: '{top_co}' | Spotlight role: '{top_title}' | Location: {top_loc}\n"
            f"{co_ctx_line}"
            f"Their screening tool for this role: {ats_name}. How it reads a CV: {ats_intel}.\n"
            + (f"Attached CV built for the spotlight role: {cv_file}\n" if cv_file else "")
        )
    active_line = (
        f"Roles already delivered in earlier briefings that remain open on the board: {active_count}\n"
        if isinstance(active_count, int) and active_count >= 0
        else ""
    )
    rejected_line = (
        f"Off-target openings automatically suppressed by our AI Match Gate overnight: {ai_rejected_count}\n"
        if isinstance(ai_rejected_count, int) and ai_rejected_count > 0
        else ""
    )
    return (
        "=== TODAY ===\n"
        f"Date: {today.isoformat()} ({cadence_title}: {cadence_subline})\n"
        f"Language to write in: {language}\n"
        f"Our story arc: {arc_title}. Guidance: {arc_guidance}\n"
        f"Briefing edition together: #{edition} | Active streak: {rel_ctx.get('streak_days', 1)} day(s)"
        + (f" | Milestone unlocked today: {milestone}" if milestone else "") + "\n"
        f"Cumulative wins together: {rel_ctx.get('total_roles_surfaced', total_count)} matched roles surfaced, "
        f"{rel_ctx.get('total_cvs_tailored', cvs_today_count)} custom CVs forged\n"
        f"Today's chapter theme (#{rel_ctx.get('angle_id', 0) + 1}): '{beat_title}'. {beat_insight}\n\n"
        "=== MY PARTNER (THE CANDIDATE) ===\n"
        f"First name: {first_name}\n"
        f"Career identity from their real master CV: {cv_snapshot or 'Ambitious leader targeting high-impact step-up roles in Jalisco and Remote Mexico'}\n"
        + (f"Career goal / north star: {career_goal}\n" if career_goal else "")
        + "\n=== LAST NIGHT'S OPERATION (REAL FACTS: use these, never invent others) ===\n"
        f"Employer career portals swept: {b_cnt} across 9 ATS platforms (Workday, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Oracle Recruiting Cloud, Eightfold, BambooHR)\n"
        f"Run context: {mode_note}\n"
        f"Today's briefing: {total_count} role(s) new to the candidate, {upgrade_count} Career Upgrade Move(s), {hot_count} hot opening(s) posted within 48 hours, {cv_line}\n"
        f"{rejected_line}"
        f"{active_line}"
        f"{spotlight_block}"
        + "\n=== STORY SO FAR (continuity memory: reference lightly, never scold) ===\n"
        f"{story_lines}\n"
        + (f"Yesterday's micro-challenge: '{last_chal}'\n" if last_chal else "")
        + (f"Yesterday's spotlight employer: {last_co}\n" if last_co else "")
        + f"RECENT_HEADLINES_TO_AVOID: {avoid_txt}\n\n"
        "=== TASK ===\n"
        f"Write Briefing #{edition} for {first_name} now, fully grounded in the facts above, in Lucy's partner voice. Return ONLY the JSON object."
    )


QUIET_DAY_VAULT = [
    ("A Quiet Board Is Still a Swept Board.", "{first_name}, {board_count} employer portals were swept overnight and nothing new matched your radar, so I am not going to pad your inbox with roles you have already seen. Quiet mornings are when the groundwork for the next move gets done."),
    ("No New Doors Today. The Radar Never Blinked.", "Zero never-seen postings matched overnight, {first_name}, and that is honest news, not bad news. Every role I have already sent you stays tracked, and the moment a new one drops you will be the first calibrated applicant in the queue."),
    ("Silence From Their Machine Is Not a Verdict.", "{first_name}, the filters were quiet overnight: no new openings cleared your bar across {board_count} portals. We keep the streak alive anyway, because the candidates who win are the ones still warm when the next posting lands."),
    ("Quiet Market, Sharp Partner.", "Nothing new to send you this morning, {first_name}, and I refuse to recycle old roles as if they were news. Use the breathing room to tighten one thing in your pipeline; I will keep sweeping every portal tonight."),
    ("Nothing New Dropped. Your Edge Did Not Drop Either.", "{first_name}, overnight I checked {board_count} career portals and found zero roles you have not already seen. Momentum on a quiet day looks like one small follow-up, not a frantic search, and your radar stays on watch."),
    ("Zero New Roles, One Honest Partner.", "I would rather tell you the truth than invent urgency, {first_name}: no never-seen postings matched this morning. The roles already in your hands remain live, and tomorrow's sweep starts the second this email leaves."),
]

QUIET_DAY_CHALLENGES = [
    "Open the last application you submitted and send the hiring manager or recruiter a two-line follow-up note today.",
    "Spend 60 seconds sharpening your LinkedIn headline so it mirrors the Target Role Direction on your latest tailored CV.",
    "Message one former colleague at a company on your radar and ask how their team is hiring this quarter.",
    "Re-open yesterday's attached CV, read the summary out loud once, and note one win you want to lead with in interviews.",
    "Pick one company from your earlier briefings and set a 10-minute calendar block this week to research its leadership team.",
    "Reply to this email with one role title you would love to see on your radar; I will tune the filters around it.",
]

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

PARTNER_STORY_BEATS = [
    ("The Filter Was Never Built For You", "Most large employers let an applicant tracking system rank CVs before a recruiter opens one, so generic CVs lose silently. Tonight we wrote the candidate's truth in the exact dialect that system scores highest."),
    ("Their Robot, Our Translator", "Their screening AI reads structure, keywords and seniority signals. We do not fight it; we translate real wins into its native language so the robot becomes our doorman."),
    ("Nobody Else Woke Up To This", "While the city slept we swept {board_count} employer portals, discarded 99% of the noise and left only curated leverage in the inbox. That asymmetry is the whole game."),
    ("The Loyalty Tax Is Real", "Internal raises crawl while the external market reprices leaders with every move; staying still is the most expensive decision nobody talks about."),
    ("Speed Is A Status Signal", "Fresh postings enjoy a clean recruiter queue for roughly two days; a calibrated application inside that window multiplies callback odds. We caught today's lineup off the wire."),
    ("Interview From Power, Not Urgency", "Exploring while winning turns every conversation into a peer-to-peer negotiation instead of a plea. Optionality is posture."),
    ("Job Descriptions Are Wish Lists", "Hiring managers hire people who own most of the role today and learn the rest fast. The filter cannot measure courage, so we supply it."),
    ("Six Seconds, One Decision", "After the machine says yes, a human spends a few seconds deciding whether to keep reading. The tailored CV front-loads direction and proof for exactly that moment."),
    ("Machines Reward Clarity", "ATS parsers choke on columns, tables and icons and silently drop what they cannot read. The single-column Word CV is engineered to be parsed with full fidelity."),
    ("We Did The Boring 90 Percent", "Sourcing, filtering, keyword mapping, formatting: the exhausting part of applying is already done. All that remains is 60 seconds of audacity."),
    ("Keywords Are Their Password", "Recruiters search their candidate pools by exact terms; a truthful CV that mirrors the posting's vocabulary shows up first in their own search box."),
    ("Pipeline Beats Anxiety", "Two live external conversations change how anyone walks into every internal meeting. Today we open one more door."),
    ("The CEO Of Me, Inc.", "Treat talent like growth capital and deploy it where scope, mentorship and upside compound fastest. Today's spotlight is a reallocation decision."),
    ("The First Fifteen Applicants", "Early calibrated applicants dominate shortlist slots before recruiter fatigue sets in; being first and precise beats being perfect and late."),
    ("Rejection Is Calibration", "Every no is free market data on how the candidate's skills price today, and it only takes one step-up yes to reset the baseline forever."),
    ("Comfort Is Crowded", "Stretch seats have less competition than lateral ones because most people self-reject. Let the committee decide, never yourself."),
    ("Their AI Cannot Read Ambition", "Screening models score the past; they cannot see the next gear. So we put the trajectory in the headline where humans look first."),
    ("Jalisco Is A Global Hub", "World-class enterprises keep scaling leadership and transformation teams across Guadalajara, Zapopan and Remote Mexico. The premium seats are local."),
    ("One Shot, Asymmetric Upside", "Sixty seconds of downside against a multi-year change in title, scope and compensation. Strategists take that bet every time."),
    ("The Machine Rewards Numbers", "Quantified bullets survive both the parser and the human scan; vague duties vanish. The CV leads with metrics on purpose."),
    ("Momentum Before Nine AM", "One minute invested in your own equity before the calendar fills is the highest-return meeting of the day."),
    ("Familiarity Is Not Security", "Real security is knowing three companies would hire you tomorrow. We keep that muscle warm every single morning."),
    ("Reverse-Engineered, Not Guessed", "We mapped how each ATS platform parses, ranks and searches, then built the CV to pass every gate truthfully. That is intelligence work, not luck."),
    ("The Reorg Will Not Wait", "High-agency operators move on their own schedule. Waiting for circumstances shrinks choices; moving early multiplies them."),
    ("Let Them Bid", "Nobody learns their market value inside one org chart; competing hiring managers reveal it within weeks."),
    ("Signal Over Noise", "No two hours of doom-scrolling job boards tonight; the needles are already in the inbox."),
    ("Proof Is Scarce", "Portals overflow with talkers; verifiable builders are rare. So we stripped the fluff and let the results speak."),
    ("Posture Changes Outcomes", "Candidates with options negotiate titles and bands; candidates without them accept. Today's rep buys posture."),
    ("The Compounding Narrative", "Every targeted application sharpens how the candidate articulates their highest-value wins; the story gets tighter with reps."),
    ("Their Filter, Our Front Door", "The same automation that screens out so many applicants waves through the ones who speak its language. We hold the phrasebook."),
    ("Outgrow The Room", "If you are the most experienced voice in every meeting, you have already outgrown the room. Step toward a steeper arena."),
    ("Write Your Own Promotion Slip", "Stop waiting for annual calibration committees; let competing teams compete for the candidate's impact instead."),
]

# Backward-compatible flat view of the story beats (one line per angle)
DAILY_MOTIVATION_THEMES = [f"{title}: {insight}" for title, insight in PARTNER_STORY_BEATS]

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
    """Extract a compact, PII-scrubbed career identity snapshot from the subscriber's master CV
    (identity pitch, signature strengths, current chapter, earlier chapters) so Lucy's daily
    briefing speaks to their real craft instead of generic motivation."""
    raw = load_subscriber_cv(sub_cfg, root=root)
    if not raw:
        return ""
    scrubbed = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "", raw)
    scrubbed = re.sub(r"\+?\d[\d\s().-]{7,}\d", "", scrubbed)
    scrubbed = re.sub(r"linkedin\.com/\S+", "", scrubbed, flags=re.I)

    def _clean(t):
        return re.sub(r"\s+", " ", re.sub(r"\*+", "", str(t or ""))).strip(" |-")

    sections = {}
    cur = None
    for ln in scrubbed.splitlines():
        s = ln.strip()
        if not s or s.startswith("# "):
            continue
        if s.startswith("## "):
            cur = s[3:].strip().upper()
            sections[cur] = []
            continue
        if cur is not None:
            sections[cur].append(s)

    summary = " ".join(_clean(x) for x in sections.get("PROFESSIONAL SUMMARY", []))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", summary) if s]
    identity = " ".join(sentences[:2])
    if len(identity) > 380:
        identity = identity[:380].rsplit(" ", 1)[0].rstrip(",;:") + "..."

    strengths = []
    for ln in sections.get("CORE COMPETENCIES", []):
        m = re.match(r"^[-*]\s*\*\*([^*:]+):?\*\*", ln)
        if m:
            strengths.append(_clean(m.group(1)))
        if len(strengths) >= 5:
            break

    exp_lines = sections.get("PROFESSIONAL EXPERIENCE", [])
    company = role = dates = ""
    earlier = []
    for ln in exp_lines:
        if ln.startswith("### "):
            co = _clean(ln[4:].split("|")[0])
            if not company:
                company = co
            elif co and co != company and co not in earlier:
                earlier.append(co)
        elif ln.startswith("**") and company and not role:
            parts = [_clean(p) for p in ln.split("|")]
            role = parts[0]
            dates = parts[1] if len(parts) > 1 else ""

    parts = []
    if identity:
        parts.append(f"Identity: {identity}")
    if strengths:
        parts.append("Signature strengths: " + ", ".join(strengths))
    if role or company:
        parts.append(f"Current chapter: {role or 'Leader'} at {company}" + (f" ({dates})" if dates else ""))
    if earlier:
        parts.append("Earlier chapters: " + ", ".join(earlier[:4]))
    if parts:
        return " | ".join(parts)[:900]

    lines = [ln.strip(" #*-") for ln in scrubbed.splitlines() if ln.strip()]
    return " | ".join(lines[:8])[:450]


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
        "recent_story_beats": list(rel.get("recent_story_beats") or []),
        "last_micro_challenge": str(rel.get("last_micro_challenge") or ""),
        "last_spotlight_company": str(rel.get("last_spotlight_company") or ""),
        "angle_id": angle_id,
        "board_count": board_count,
        "milestone_badge": EDITION_MILESTONES.get(edition, ""),
    }


def commit_relationship_state(sub_state, rel_ctx, pack, jobs_sent, today):
    """Persist updated relationship streak, edition, cumulative stats, continuity memory
    (story beats, last micro-challenge, last spotlight employer) and anti-repeat history."""
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

    beats = list(rel.get("recent_story_beats") or [])
    if pack.get("story_beat"):
        beats.append(f"Ed. #{rel_ctx['edition']} ({today.isoformat()}): {pack['story_beat']}")
    rel["recent_story_beats"] = beats[-12:]
    if pack.get("micro_challenge"):
        rel["last_micro_challenge"] = str(pack["micro_challenge"])[:300]
    if pack.get("spotlight_company"):
        rel["last_spotlight_company"] = str(pack["spotlight_company"])[:120]
    rel["last_briefing_source"] = pack.get("briefing_source", "vault")

    spotlights = list(rel.get("recent_spotlight_keys") or [])
    for j in jobs_sent or []:
        if j.get("quiet_spotlight") and j.get("key"):
            spotlights.append(j["key"])
    rel["recent_spotlight_keys"] = spotlights[-18:]


def call_gemini_daily_motivation(prompt_text, model=DEFAULT_LLM_MODEL, api_key=None, first_name="", recent_headlines=None):
    """Ask Gemini for today's 9-part 'Us vs. Their Machine' partner briefing.

    Every draft passes `_briefing_pack_quality` (length, forbidden dishonest vocabulary, unfilled
    placeholders, near-duplicate headlines). A rejected draft triggers exactly ONE revision round
    with the rejection reason appended, so the final text is always AI-written and on-brand or we
    fall back to the deterministic vault."""
    rejection = None
    for attempt in range(2):
        prompt = prompt_text
        if rejection:
            prompt += (
                f"\n\nREVISION NOTE: your previous draft was rejected because {rejection}. "
                "Rewrite the whole briefing from scratch, fixing this, keeping every rule above."
            )
        try:
            parsed = _invoke_gemini_json(
                prompt,
                system_text=DAILY_MOTIVATION_SYSTEM_PROMPT,
                model=model,
                api_key=api_key,
                temperature=0.95,
                max_output_tokens=2048,
                timeout=30,
                retries=1,
                validator=lambda p: isinstance(p, dict) and bool(p.get("headline")) and bool(p.get("message")),
            )
        except Exception:
            return None
        if not parsed:
            return None
        ok, reason = _briefing_pack_quality(parsed, first_name=first_name, recent_headlines=recent_headlines)
        if ok:
            out = {k: _clean_briefing_text(parsed.get(k) or "") for k in BRIEFING_TEXT_KEYS}
            out["_llm_model"] = str(parsed.get("_llm_model") or _normalize_gemini_model(model) or DEFAULT_LLM_MODEL)
            out["_revised"] = attempt > 0
            return out
        rejection = reason
    return None


def generate_daily_briefing_pack(
    sub_cfg, top_job, upgrade_count, hot_count, total_count, today,
    rel_ctx=None, cvs_today_count=0, is_quiet_spotlight=False, root=ROOT,
    quiet_day=False, active_count=None, ai_rejected_count=0,
):
    """Generate today's unique 'Us vs. Their Machine' partner briefing for the subscriber.

    AI-first: Gemini writes all nine parts (subject spark, chapter title, headline, message,
    their-AI-vs-our-AI intel, Lucy's connection note, micro-challenge, sign-off and a memory
    beat) from a fully grounded prompt (real overnight facts, the candidate's real craft, the
    spotlight employer's ATS, the story so far). The deterministic vault is used only when
    Gemini is unavailable or every draft fails the quality gate. On a `quiet_day` (zero
    never-seen roles) the note lists no roles and the challenge is pipeline hygiene."""
    sub_name = (sub_cfg or {}).get("name") or "Candidate"
    sub_id = (sub_cfg or {}).get("id") or sub_name.lower()
    first_name = sub_name.split()[0]
    if quiet_day:
        top_job = None
    top_co = (top_job or {}).get("company") or "a top employer"
    top_title = (top_job or {}).get("title") or "a step-up leadership role"
    hi_cnt = upgrade_count if upgrade_count > 0 else max(1, total_count)

    if rel_ctx is None:
        sub_offset = sum(ord(ch) for ch in sub_id) % len(PARTNER_STORY_BEATS)
        angle_id = (today.toordinal() + sub_offset) % len(PARTNER_STORY_BEATS)
        rel_ctx = {
            "edition": 1,
            "streak_days": 1,
            "total_roles_surfaced": max(1, total_count),
            "total_cvs_tailored": cvs_today_count,
            "recent_angle_ids": [],
            "recent_headlines": [],
            "recent_story_beats": [],
            "angle_id": angle_id,
            "board_count": 109,
            "milestone_badge": "",
        }

    b_cnt = rel_ctx.get("board_count", 109)
    angle_id = rel_ctx["angle_id"]
    edition = rel_ctx["edition"]
    cadence_title, cadence_subline = WEEKDAY_CADENCE[today.weekday()]
    beat_title, beat_insight_tmpl = PARTNER_STORY_BEATS[angle_id % len(PARTNER_STORY_BEATS)]
    beat_insight = beat_insight_tmpl.format(board_count=b_cnt)
    ats_name, ats_intel = ats_intel_for(top_job)

    # Deterministic, non-repeating vault defaults (used only when Gemini is unavailable)
    conn_tmpl = LUCY_CONNECTION_NOTES[(angle_id + edition) % len(LUCY_CONNECTION_NOTES)]
    default_conn = conn_tmpl.format(
        board_count=b_cnt,
        edition=edition,
        streak=rel_ctx.get("streak_days", 1),
        total_roles=rel_ctx.get("total_roles_surfaced", total_count),
        total_cvs=rel_ctx.get("total_cvs_tailored", cvs_today_count),
        cv_today=max(1, cvs_today_count),
    )
    if quiet_day:
        default_challenge = QUIET_DAY_CHALLENGES[(angle_id + edition) % len(QUIET_DAY_CHALLENGES)]
    else:
        chal_tmpl = DAILY_MICRO_CHALLENGES[(angle_id + edition) % len(DAILY_MICRO_CHALLENGES)]
        default_challenge = chal_tmpl.format(top_company=top_co, first_name=first_name)
    default_signoff = LUCY_SIGNOFFS[(angle_id + edition) % len(LUCY_SIGNOFFS)]
    if ats_name == "their applicant tracking system" or norm(ats_intel).startswith(norm(ats_name)):
        intel_sentence = ats_intel
    else:
        intel_sentence = f"{ats_name[0].upper() + ats_name[1:]}: {ats_intel}"
    default_intel = (
        f"{intel_sentence}. Your attached CV for {top_co} was built to pass exactly that gate, truthfully."
        if top_job and cvs_today_count
        else f"{intel_sentence}. We mirror that logic truthfully in every CV we forge together."
    )

    if quiet_day:
        h_tmpl, m_tmpl = QUIET_DAY_VAULT[(angle_id + edition) % len(QUIET_DAY_VAULT)]
        default_beat = f"Quiet day: Lucy sent no roles, kept the streak honest and asked {first_name} for one pipeline-hygiene move."
    else:
        h_tmpl, m_tmpl = DAILY_INSIDER_VAULT[angle_id % len(DAILY_INSIDER_VAULT)]
        default_beat = f"Lucy framed '{beat_title}' around {top_co} ({top_title}) and asked {first_name} for one 60-second application."
    vault_headline = h_tmpl.format(first_name=first_name, top_company=top_co, highlight_count=hi_cnt, board_count=b_cnt)
    vault_message = m_tmpl.format(first_name=first_name, top_company=top_co, highlight_count=hi_cnt, board_count=b_cnt)
    default_spark = vault_headline.rstrip(".!")

    base = {
        "cadence_title": cadence_title,
        "cadence_subline": cadence_subline,
        "angle_number": angle_id + 1,
        "chapter_title": beat_title,
        "spotlight_company": top_co if top_job else "",
        "quiet_day": bool(quiet_day),
    }

    def _fix(text):
        return _fix_ats_employer_confusion(str(text or "").strip(), top_co, top_title) if top_job else str(text or "").strip()

    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if api_key:
        cv_snapshot = extract_cv_identity_snapshot(sub_cfg, root=root)
        language = str((sub_cfg or {}).get("briefing_language") or "English").strip() or "English"
        model = (os.environ.get("GEMINI_MODEL") or "").strip() or (sub_cfg or {}).get("llm_model", DEFAULT_LLM_MODEL)
        prompt = compose_partner_briefing_prompt(
            sub_cfg, top_job, rel_ctx, today, (beat_title, beat_insight), (cadence_title, cadence_subline),
            cv_snapshot, upgrade_count, hot_count, total_count, cvs_today_count, is_quiet_spotlight, language=language,
            quiet_day=quiet_day, active_count=active_count, ai_rejected_count=ai_rejected_count,
        )
        try:
            res = call_gemini_daily_motivation(
                prompt, model=model, api_key=api_key,
                first_name=first_name, recent_headlines=rel_ctx.get("recent_headlines") or [],
            )
            if isinstance(res, tuple) and len(res) == 2:
                # Backward-compatible support if mocked as (headline, message)
                headline = _fix(res[0])
                message = _fix(res[1])
                return {
                    **base,
                    "briefing_source": "gemini",
                    "briefing_model": _normalize_gemini_model(model) or DEFAULT_LLM_MODEL,
                    "subject_spark": headline.rstrip(".!")[:42],
                    "headline": headline,
                    "message": message,
                    "their_ai_vs_our_ai": default_intel,
                    "connection_ps": default_conn,
                    "micro_challenge": default_challenge,
                    "partner_signoff": default_signoff,
                    "story_beat": default_beat,
                }
            if isinstance(res, dict) and res.get("headline") and res.get("message"):
                hl = _fix(res["headline"])
                msg_txt = _fix(res["message"])
                if norm(first_name) not in norm(msg_txt):
                    msg_txt = f"{first_name}, {msg_txt}"
                spark = _fix(res.get("subject_spark") or hl.rstrip(".!"))
                return {
                    **base,
                    "briefing_source": "gemini",
                    "briefing_model": str(res.get("_llm_model") or _normalize_gemini_model(model) or DEFAULT_LLM_MODEL),
                    "briefing_revised": bool(res.get("_revised")),
                    "subject_spark": spark[:46],
                    "chapter_title": _fix(res.get("chapter_title") or beat_title)[:60],
                    "headline": hl,
                    "message": msg_txt,
                    "their_ai_vs_our_ai": _fix(res.get("their_ai_vs_our_ai") or default_intel),
                    "connection_ps": _fix(res.get("connection_ps") or default_conn),
                    "micro_challenge": _fix(res.get("micro_challenge") or default_challenge),
                    "partner_signoff": _fix(res.get("partner_signoff") or default_signoff)[:80],
                    "story_beat": _fix(res.get("story_beat") or default_beat)[:240],
                }
        except Exception:
            pass

    return {
        **base,
        "briefing_source": "vault",
        "briefing_model": "deterministic-vault",
        "subject_spark": default_spark[:46],
        "headline": vault_headline,
        "message": vault_message,
        "their_ai_vs_our_ai": default_intel,
        "connection_ps": default_conn,
        "micro_challenge": default_challenge,
        "partner_signoff": default_signoff,
        "story_beat": default_beat,
    }


def generate_daily_insider_note(sub_cfg, top_job, upgrade_count, hot_count, total_count, today, rel_ctx=None):
    """Return `(headline, message)` tuple from `generate_daily_briefing_pack`."""
    pack = generate_daily_briefing_pack(
        sub_cfg, top_job, upgrade_count, hot_count, total_count, today, rel_ctx=rel_ctx
    )
    return pack["headline"], pack["message"]


# Minimalist email design tokens: one ink, one muted grey, one hairline and one accent (links only).
# Hierarchy comes from type size and whitespace; there are no boxes, badges, meters or pictographs.
EMAIL_INK = "#18181b"
EMAIL_BODY = "#27272a"
EMAIL_MUTE = "#71717a"
EMAIL_LINE = "#e4e4e7"
EMAIL_ACCENT = "#0f766e"
EMAIL_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
EMAIL_LABEL_STYLE = "font-size:11px;font-weight:600;letter-spacing:0.8px;text-transform:uppercase;line-height:1.5;"


def _safe_http_url(url):
    """Return `url` only when it is an absolute http(s) link; anything else becomes '#', so a
    malformed or hostile ATS feed can never place another URL scheme behind an email link."""
    u = str(url or "").strip()
    return u if re.match(r"^https?://", u, re.I) else "#"


def _render_job_card_html(j, today, show_company_profile=True):
    """Render one role as a minimalist, email-safe table row separated from its neighbours by a
    hairline: a quiet label line, the linked title, one meta line, the employer profile (pass
    `show_company_profile=False` to skip it when the same employer was already described earlier
    in the email), the match rationale and a single text 'Apply' link.

    Every dynamic value is HTML-escaped and the link target is restricted to http(s)."""
    enrich_company_metadata([j])
    _, is_hot, age_label = job_age_info(j, today)
    loc_tag = LOC_LABELS.get(j.get("loc_class"), j.get("loc_class") or "") or ""
    title_esc = html.escape(str(j.get("title") or "Untitled Role"))
    company_esc = html.escape(str(j.get("company") or ""))
    loc_esc = html.escape(str(j.get("location") or loc_tag))
    team_esc = html.escape(str(j.get("team") or ""))
    url_esc = html.escape(_safe_http_url(j.get("url")), quote=True)
    age_esc = html.escape(age_label)

    analysis = j.get("llm_analysis") or {}
    match_meta = j.get("match_assessment") or {}
    is_upgrade = bool(
        j.get("is_career_upgrade")
        or analysis.get("is_career_upgrade")
        or match_meta.get("is_career_upgrade")
    )
    upgrade_reason = str(
        j.get("upgrade_reason")
        or analysis.get("upgrade_reason")
        or match_meta.get("upgrade_reason")
        or ""
    ).strip()

    score = analysis.get("match_score")
    if not isinstance(score, (int, float)):
        score = match_meta.get("match_score")
    score_int = int(score) if isinstance(score, (int, float)) else None

    # One quiet label line instead of coloured badges (the section heading already says "upgrade")
    label_bits = []
    if is_hot:
        label_bits.append("Hot")
    elif j.get("is_new"):
        label_bits.append("New")
    if score_int is not None:
        label_bits.append(f"{score_int}% CV Match")
    label_html = (
        f'<div style="{EMAIL_LABEL_STYLE}color:{EMAIL_MUTE};margin:0 0 5px;">{" &middot; ".join(label_bits)}</div>'
        if label_bits
        else ""
    )

    meta_bits = [f'<span style="color:{EMAIL_INK};">{company_esc}</span>'] if company_esc else []
    meta_bits.extend(bit for bit in (loc_esc, team_esc, age_esc) if bit)
    meta_html = " &middot; ".join(meta_bits)

    small_style = f"font-size:13px;line-height:1.55;color:{EMAIL_MUTE};"

    co_ind = str(j.get("company_industry") or "").strip()
    co_desc = str(j.get("company_description") or "").strip()
    profile_html = ""
    if show_company_profile and (co_ind or co_desc):
        ind_html = f'<span style="color:{EMAIL_INK};">{html.escape(co_ind)}</span>' if co_ind else ""
        sep = " &mdash; " if (co_ind and co_desc) else ""
        profile_html = f'<div style="margin:8px 0 0;{small_style}">{ind_html}{sep}{html.escape(co_desc)}</div>'

    upgrade_html = (
        f'<div style="margin:8px 0 0;font-size:13px;line-height:1.55;color:{EMAIL_BODY};">&uarr; {html.escape(upgrade_reason)}</div>'
        if is_upgrade and upgrade_reason
        else ""
    )

    fit_html = ""
    if analysis or match_meta:
        why_raw = str(analysis.get("lucy_why_it_fits") or match_meta.get("match_rationale") or "").strip()
        synergy_raw = str(match_meta.get("industry_synergy") or "").strip()
        has_cv = bool(analysis and (j.get("cv_filename") or analysis.get("cv")))
        if has_cv or why_raw:
            heading = (
                "Lucy&rsquo;s Headhunter Take &amp; Tailored CV"
                if has_cv
                else "Why This Role &amp; Company Match Your Profile"
            )
            parts = [f'<div style="{EMAIL_LABEL_STYLE}color:{EMAIL_MUTE};margin:14px 0 4px;">{heading}</div>']
            if why_raw:
                parts.append(
                    f'<div style="font-size:14px;line-height:1.6;color:{EMAIL_BODY};">{html.escape(why_raw)}</div>'
                )
            if synergy_raw:
                parts.append(
                    f'<div style="margin:6px 0 0;{small_style}">Industry &amp; Company Fit: {html.escape(synergy_raw)}</div>'
                )
            if has_cv:
                att_name = html.escape(str(j.get("cv_filename") or "Tailored_CV.docx"))
                parts.append(
                    f'<div style="margin:6px 0 0;{small_style}">Attached: <span style="color:{EMAIL_INK};">{att_name}</span></div>'
                )
            fit_html = "".join(parts)

    return f"""
                <tr>
                  <td style="padding:18px 0 20px;border-top:1px solid {EMAIL_LINE};font-family:{EMAIL_FONT};">
                    {label_html}
                    <div style="font-size:16px;font-weight:600;line-height:1.4;"><a href="{url_esc}" style="color:{EMAIL_INK};text-decoration:none;">{title_esc}</a></div>
                    <div style="margin:3px 0 0;{small_style}">{meta_html}</div>
                    {profile_html}{upgrade_html}{fit_html}
                    <div style="margin:12px 0 0;font-size:13px;line-height:1.5;"><a href="{url_esc}" style="color:{EMAIL_ACCENT};font-weight:600;text-decoration:none;">Apply &rarr;</a></div>
                  </td>
                </tr>"""


def build_email_message(
    jobs_to_send, all_jobs_count, sub_cfg, today, sender_email,
    attachments=None, sub_state=None, root=ROOT, board_count=109, return_meta=False,
    quiet_day=False, overflow_count=0, board_html=None, ai_rejected_count=0,
):
    """Construct an editorial-grade, minimalist executive daily briefing with a guaranteed-unique
    daily GenAI 'Us vs. Their Machine' partner manifesto, personal relationship streak with Lucy,
    and 60-second completion challenge. Only AI-validated matches never sent before are listed; on
    a quiet day (`quiet_day=True`) no roles are listed and nothing is re-sent."""
    recipient = sub_cfg.get("recipient_email", "lucasschollmatter@gmail.com")
    sub_name = sub_cfg.get("name") or recipient
    sub_id = sub_cfg.get("id") or (sub_name or "subscriber").lower()
    first_name = (sub_name or "there").split()[0]
    date_str = today.isoformat()
    attachments = attachments or []
    quiet_day = bool(quiet_day) or (not jobs_to_send)
    resend_all = sub_cfg.get("email_mode") == "all"

    enrich_company_metadata(jobs_to_send, root=root)
    master_cv_for_cards = load_subscriber_cv(sub_cfg, root=root) if jobs_to_send else ""
    for j in jobs_to_send:
        if "match_assessment" not in j and "llm_analysis" not in j:
            j["match_assessment"] = build_deterministic_match_assessment(j, master_cv_for_cards, sub_cfg)

    # Ensure every job has career-upgrade classification
    classify_career_upgrades(jobs_to_send, sub_cfg, root=root)

    # Sort: Career Upgrade Moves first, then highest match score, HOT (0-2d), newest, company
    enriched = []
    for j in jobs_to_send:
        days, is_hot, age_label = job_age_info(j, today)
        is_up = bool(
            j.get("is_career_upgrade")
            or (j.get("llm_analysis") or {}).get("is_career_upgrade")
            or (j.get("match_assessment") or {}).get("is_career_upgrade")
        )
        j["is_career_upgrade"] = is_up
        enriched.append((not is_up, not is_hot, days, j, is_hot, age_label, is_up))
    enriched.sort(
        key=lambda item: (
            item[0],
            -_job_match_score(item[3]),
            item[1],
            item[2],
            item[3].get("company", "").lower(),
        )
    )

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
        quiet_day=quiet_day, active_count=int(all_jobs_count or 0),
        ai_rejected_count=ai_rejected_count,
    )
    insider_headline = pack["headline"]
    insider_message = pack["message"]
    connection_ps = pack["connection_ps"]
    micro_challenge = pack["micro_challenge"]
    subject_spark = pack["subject_spark"]
    chapter_title = pack.get("chapter_title") or ""
    ai_intel = pack.get("their_ai_vs_our_ai") or ""
    partner_signoff = pack.get("partner_signoff") or ""
    edition = rel_ctx["edition"]
    streak_days = rel_ctx["streak_days"]
    milestone_badge = rel_ctx.get("milestone_badge") or ""

    def _plural(n, word):
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    n_roles = len(jobs_to_send)
    companies = []
    for item in enriched:
        co = item[3].get("company") or ""
        if co and co not in companies:
            companies.append(co)
    co_part = ", ".join(companies[:2]) + (f" +{len(companies) - 2}" if len(companies) > 2 else "")

    msg = EmailMessage()
    if quiet_day:
        spark_part = f' · "{subject_spark}"' if subject_spark else ""
        msg["Subject"] = f"⚡ Lucy #{edition}: Quiet day · 0 new roles · {streak_days}d streak{spark_part} ({date_str})"
    else:
        hot_prefix = f"🔥 {len(hot_jobs)} HOT · " if hot_jobs else "⚡ "
        what = "matching role" if resend_all else "new role"
        count_part = _plural(n_roles, what) + ("" if resend_all else " for you")
        up_part = f" · {_plural(len(upgrade_jobs), 'Career Upgrade Move')}" if upgrade_jobs else ""
        cv_part = f" · {_plural(len(attachments), 'Tailored CV')} ready" if attachments else ""
        co_txt = f" · {co_part}" if co_part else ""
        msg["Subject"] = f"{hot_prefix}Lucy #{edition}: {count_part}{co_txt}{up_part}{cv_part} ({date_str})"
    msg["From"] = f"Lucy · Personal Headhunter <{sender_email}>"
    msg["To"] = recipient
    msg["Reply-To"] = sender_email

    scope_bits = ["Jalisco"] if sub_cfg.get("locations") != [] else []
    if sub_cfg.get("include_remote_mexico", True):
        scope_bits.append("Remote Mexico")
    if sub_cfg.get("include_country_only", True):
        scope_bits.append("Mexico (city not listed)")
    radar_scope = " + ".join(scope_bits) or "Mexico"
    board_name = f"Lucy_Board_{sub_id}_{date_str}.html"
    overflow_note = (
        f"+{overflow_count} more never-sent role(s) are waiting on your personal board (attached: {board_name}); "
        "they also roll into tomorrow's briefing automatically."
        if overflow_count > 0
        else ""
    )
    quiet_note = (
        f"Quiet day on the radar: zero never-seen postings matched overnight across {board_count} employer portals, "
        f"so nothing is re-sent. {_plural(int(all_jobs_count or 0), 'role')} you already received remain open. Streak intact."
        if quiet_day
        else ""
    )
    radar_footer = (
        f"Your radar: {board_count} employer portals · {radar_scope} · AI-validated career & CV fit · only roles never sent to you before appear here. "
        "Reply to this email to pause, change cities or add target roles."
    )

    # Minimalist plain-text version (preserves '- {title} at {company}' format)
    txt_lines = [
        f"{sub_name} — Lucy Private Talent Briefing #{edition} · Day {streak_days} Streak ({date_str})",
        f"{pack['cadence_subline']} — Lucy, your personal headhunter & your daily reminder to check where to apply today.",
        "",
        f"Us vs. Their Machine · Chapter {edition}: {chapter_title}" if chapter_title else f"Us vs. Their Machine · Chapter {edition}",
        insider_headline,
        insider_message,
    ]
    if ai_intel:
        txt_lines.append(f"Their AI vs. Our AI: {ai_intel}")
    txt_lines.append(f"Lucy's Corner: {connection_ps}")
    if partner_signoff:
        txt_lines.append(f"— {partner_signoff}")
    txt_lines.append("")
    if quiet_day:
        txt_lines.extend([quiet_note, ""])
    else:
        txt_lines.extend([
            f"{_plural(len(upgrade_jobs), 'Career Upgrade Move')} · {len(hot_jobs)} Hot (0-2d) · "
            f"{n_roles} new for you ({all_jobs_count} active matches)",
            "",
        ])
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
        co_ind = (j.get("company_industry") or "").strip()
        co_desc = (j.get("company_description") or "").strip()
        if co_ind or co_desc:
            ind_lbl = f" ({co_ind})" if co_ind else ""
            txt_lines.append(f"  Company{ind_lbl}: {co_desc or j['company']}")
        if is_up and j.get("upgrade_reason"):
            txt_lines.append(f"  Upgrade: {j['upgrade_reason']}")
        an = j.get("llm_analysis") or {}
        ma = j.get("match_assessment") or {}
        if an:
            cv_obj = an.get("cv") or {}
            if cv_obj.get("target_direction"):
                txt_lines.append(f"  {cv_obj['target_direction']}")
            fit_txt = an.get("lucy_why_it_fits") or ma.get("match_rationale") or ""
            sc_txt = an.get("match_score", ma.get("match_score", "?"))
            txt_lines.append(f"  Fit ({sc_txt}% Match): {fit_txt}")
            if ma.get("industry_synergy"):
                txt_lines.append(f"  Industry Fit: {ma['industry_synergy']}")
            if j.get("cv_filename"):
                txt_lines.append(f"  Attached CV: {j['cv_filename']}")
        elif ma:
            sc_txt = ma.get("match_score", "?")
            if ma.get("match_rationale"):
                txt_lines.append(f"  Fit ({sc_txt}% Match): {ma['match_rationale']}")
            if ma.get("industry_synergy"):
                txt_lines.append(f"  Industry Fit: {ma['industry_synergy']}")
        txt_lines.append("")
    if overflow_note:
        txt_lines.extend([overflow_note, ""])
    txt_lines.append(f"Today's 60-second play: {micro_challenge}")
    txt_lines.extend(["", radar_footer])
    msg.set_content("\n".join(txt_lines))

    # Minimalist HTML: one column on white, hairline dividers and type-only hierarchy.
    profiled_companies = set()

    def _job_rows(job_list):
        """Render the rows for one section; each employer's profile is shown once per email."""
        rows = []
        for jb in job_list:
            co_key = norm(jb.get("company") or "")
            rows.append(_render_job_card_html(jb, today, show_company_profile=co_key not in profiled_companies))
            if co_key:
                profiled_companies.add(co_key)
        return "".join(rows)

    def _section_html(heading, job_list):
        return f"""
              <div style="{EMAIL_LABEL_STYLE}color:{EMAIL_INK};margin:30px 0 0;padding:0 0 9px;">{heading} ({len(job_list)})</div>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">{_job_rows(job_list)}
              </table>"""

    small_note_style = f"font-size:13px;line-height:1.6;color:{EMAIL_MUTE};"
    sections_html = []
    if quiet_day:
        sections_html.append(
            f'<div style="margin:28px 0 0;font-size:14px;line-height:1.6;color:{EMAIL_BODY};">{html.escape(quiet_note)}</div>'
        )
    else:
        # One summary line replaces the scoreboard tiles
        summary_bits = [_plural(n_roles, "matching role") if resend_all else f"{n_roles} new for you"]
        if upgrade_jobs:
            summary_bits.append(_plural(len(upgrade_jobs), "Career Upgrade Move"))
        if hot_jobs:
            summary_bits.append(f"{len(hot_jobs)} Hot Off the Press")
        if attachments:
            summary_bits.append(_plural(len(attachments), "Tailored CV") + " attached")
        if not resend_all and int(all_jobs_count or 0) > n_roles:
            summary_bits.append(f"{int(all_jobs_count)} active matches")
        sections_html.append(
            f'<div style="margin:28px 0 0;{small_note_style}">'
            + " &middot; ".join(html.escape(bit) for bit in summary_bits)
            + "</div>"
        )
        if is_quiet_spotlight:
            sections_html.append(
                f'<div style="margin:10px 0 0;{small_note_style}">'
                f'<span style="color:{EMAIL_INK};font-weight:600;">Quiet-Market Momentum Spotlight:</span> '
                "Zero brand-new postings dropped overnight, so I hand-picked your top unclaimed step-up "
                "opportunities still live on the radar to keep your daily momentum unbroken.</div>"
            )

    if upgrade_jobs:
        sections_html.append(_section_html("Career Upgrade Moves", upgrade_jobs))
    if standard_jobs:
        sections_html.append(_section_html("More Matches" if upgrade_jobs else "Matches", standard_jobs))
    if overflow_note:
        sections_html.append(
            f'<div style="margin:0;padding:16px 0 0;border-top:1px solid {EMAIL_LINE};{small_note_style}">'
            f"{html.escape(overflow_note)}</div>"
        )

    first_name_esc = html.escape(first_name)
    headline_esc = html.escape(insider_headline)
    message_esc = html.escape(insider_message)
    conn_esc = html.escape(connection_ps)
    challenge_esc = html.escape(micro_challenge)
    chapter_esc = html.escape(chapter_title)
    intel_esc = html.escape(ai_intel)
    signoff_esc = html.escape(partner_signoff)
    radar_footer_esc = html.escape(radar_footer)
    cadence_sub_esc = html.escape(pack["cadence_subline"])
    preheader_core = f"{subject_spark} — " if subject_spark else ""
    preheader_esc = html.escape(f"{preheader_core}{insider_headline}: {insider_message}")
    chapter_label = (
        f"Us vs. The System &middot; Chapter {edition}: {chapter_esc}" if chapter_title else "Us vs. The System"
    )
    streak_label = f"{streak_days}d Streak" if streak_days >= 2 else "Day 1 Live"
    lead_in_style = f"color:{EMAIL_INK};font-weight:600;"
    milestone_html = (
        f'<div style="{EMAIL_LABEL_STYLE}color:{EMAIL_ACCENT};margin:0 0 6px;">{html.escape(milestone_badge)}</div>'
        if milestone_badge
        else ""
    )
    intel_html = (
        f'<div style="margin:14px 0 0;{small_note_style}">'
        f'<span style="{lead_in_style}">Their AI vs. Our AI:</span> {intel_esc}</div>'
        if ai_intel
        else ""
    )
    signoff_html = (
        f'<div style="margin:10px 0 0;{small_note_style}">&mdash; {signoff_esc}</div>' if partner_signoff else ""
    )

    html_body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lucy &middot; Briefing #{edition}</title>
</head>
<body style="margin:0;padding:0;background-color:#ffffff;color:{EMAIL_INK};font-family:{EMAIL_FONT};-webkit-font-smoothing:antialiased;">
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{preheader_esc}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background-color:#ffffff;">
    <tr>
      <td align="center" style="padding:32px 20px 40px;">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;max-width:600px;">
          <tr>
            <td align="left" style="text-align:left;font-family:{EMAIL_FONT};color:{EMAIL_INK};">

              <!-- Masthead: one quiet line -->
              <div style="{EMAIL_LABEL_STYLE}color:{EMAIL_MUTE};">
                <span style="color:{EMAIL_INK};">Lucy &middot; Personal Headhunter</span> &nbsp;&middot;&nbsp; Briefing #{edition} &middot; {streak_label} &middot; {date_str}
              </div>

              <h1 style="margin:22px 0 6px;font-size:24px;font-weight:700;letter-spacing:-0.3px;line-height:1.25;color:{EMAIL_INK};">Good morning, {first_name_esc}.</h1>
              <div style="font-size:14px;line-height:1.55;color:{EMAIL_MUTE};">{cadence_sub_esc} &mdash; your daily reminder to check where to apply today.</div>

              <!-- Lucy's daily note: plain type, no card -->
              <div style="margin:30px 0 0;">
                {milestone_html}
                <div style="{EMAIL_LABEL_STYLE}color:{EMAIL_MUTE};">{chapter_label}</div>
                <div style="margin:6px 0 8px;font-size:18px;font-weight:700;letter-spacing:-0.2px;line-height:1.35;color:{EMAIL_INK};">{headline_esc}</div>
                <div style="font-size:15px;line-height:1.65;color:{EMAIL_BODY};">{message_esc}</div>
                {intel_html}
                <div style="margin:10px 0 0;{small_note_style}"><span style="{lead_in_style}">Lucy&rsquo;s Corner:</span> {conn_esc}</div>
                {signoff_html}
              </div>

              {''.join(sections_html)}

              <!-- Today's 60-second play -->
              <div style="margin:30px 0 0;padding:18px 0 0;border-top:1px solid {EMAIL_LINE};">
                <div style="{EMAIL_LABEL_STYLE}color:{EMAIL_MUTE};">Today&rsquo;s 60-second play</div>
                <div style="margin:6px 0 0;font-size:14px;line-height:1.6;color:{EMAIL_BODY};">{challenge_esc}</div>
              </div>

              <!-- Radar transparency footer -->
              <div style="margin:26px 0 0;padding:16px 0 0;border-top:1px solid {EMAIL_LINE};font-size:12px;line-height:1.6;color:{EMAIL_MUTE};">{radar_footer_esc}</div>

            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    msg.add_alternative(html_body, subtype="html")

    if board_html:
        msg.add_attachment(
            board_html.encode("utf-8") if isinstance(board_html, str) else bytes(board_html),
            maintype="text",
            subtype="html",
            filename=board_name,
        )

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
        if exp.get("company_context"):
            lines.append(f"*{exp.get('company_context')}*")
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
    sub_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(sub_cfg.get("id", "default"))).strip("_") or "default"
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

    # Prioritize Career Upgrade Moves first, then highest AI match score, then HOT (0-2d) roles
    ranked = sorted(
        jobs_to_send,
        key=lambda j: (
            not bool(j.get("is_career_upgrade")),
            -_job_match_score(j),
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

    target_jobs = ranked[:max_cvs]

    def _tailor_one(j_item):
        return tailor_cv_with_llm(j_item, master_cv, sub_cfg)

    # Tailor up to 3 CVs concurrently per subscriber (cuts 12 sequential JD + LLM calls by ~3x)
    if len(target_jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(3, len(target_jobs))) as ex:
            futures = [ex.submit(_tailor_one, j_item) for j_item in target_jobs]
            tailored_results = []
            for j_item, fut in zip(target_jobs, futures):
                try:
                    tailored_results.append((j_item, fut.result(), None))
                except Exception as exc:
                    tailored_results.append((j_item, None, exc))
    else:
        tailored_results = []
        for j_item in target_jobs:
            try:
                tailored_results.append((j_item, _tailor_one(j_item), None))
            except Exception as exc:
                tailored_results.append((j_item, None, exc))

    summary_blocks = []
    for j, analysis, err in tailored_results:
        if err is not None:
            gha_annotate("warning", f"LLM CV tailoring failed for {sub_name} ({j.get('title')}): {type(err).__name__}: {err}")
            continue
        try:
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

            j["llm_analysis"] = analysis
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

            j["cv_filename"] = att_filename
            attachments.append((att_filename, att_bytes))
            score = analysis.get("match_score", "?")
            up_badge = " [CAREER UPGRADE MOVE]" if j.get("is_career_upgrade") else ""
            t_src = analysis.get("tailor_source", "gemini")
            t_mod = analysis.get("tailor_model") or DEFAULT_LLM_MODEL
            engine_tag = f"AI: {t_mod}" if t_src == "gemini" else "FALLBACK: deterministic"
            print(
                f"  [{sub_name}] Tailored CV [{engine_tag}] for {j['title']} at {j['company']}"
                f"{up_badge} ({score}% match) -> docs/cvs/{sub_id}/{att_filename}"
            )
            kws = ", ".join(analysis.get("matched_keywords") or [])
            summary_blocks.append(
                f"- **{j.get('title')}** at **{j.get('company')}** ({j.get('location', '')}){up_badge} — "
                f"**{score}% CV Match** · `{engine_tag}` · saved to `docs/cvs/{sub_id}/{att_filename}`\n"
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
    attachments=None, sub_state=None, board_count=109, ai_stats=None,
    quiet_day=False, overflow_count=0, board_html=None, ai_rejected_count=0,
):
    """Send a single subscriber's daily digest via Gmail/SMTP if credentials are configured.
    With `quiet_day=True` a role-free briefing is sent (nothing is ever re-sent)."""
    sub_name = sub_cfg.get("name") or sub_cfg.get("recipient_email", "subscriber")
    sub_id = sub_cfg.get("id") or sub_name
    if not jobs_to_send and not quiet_day:
        print(f"  [{sub_name}] No new roles to email today.")
        return False

    if attachments is None:
        attachments = prepare_tailored_cvs(jobs_to_send, sub_cfg, today, root=root) if jobs_to_send else []

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
        quiet_day=quiet_day, overflow_count=overflow_count, board_html=board_html,
        ai_rejected_count=ai_rejected_count,
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
    ai_cvs = sum(1 for j in jobs_to_send if (j.get("llm_analysis") or {}).get("tailor_source") == "gemini")
    fb_cvs = sum(1 for j in jobs_to_send if (j.get("llm_analysis") or {}).get("tailor_source") == "deterministic")
    b_src = pack.get("briefing_source", "vault")
    b_mod = pack.get("briefing_model", "deterministic-vault")
    if isinstance(ai_stats, dict):
        ai_stats["gemini_cvs"] = ai_stats.get("gemini_cvs", 0) + ai_cvs
        ai_stats["fallback_cvs"] = ai_stats.get("fallback_cvs", 0) + fb_cvs
        if b_src == "gemini":
            ai_stats["gemini_briefings"] = ai_stats.get("gemini_briefings", 0) + 1
            ai_stats.setdefault("models_used", set()).add(b_mod)
        else:
            ai_stats["vault_briefings"] = ai_stats.get("vault_briefings", 0) + 1
        for j in jobs_to_send:
            an = j.get("llm_analysis") or {}
            if an.get("tailor_source") == "gemini" and an.get("tailor_model"):
                ai_stats.setdefault("models_used", set()).add(an["tailor_model"])
            ma = j.get("match_assessment") or {}
            if ma.get("match_source") == "gemini" and ma.get("match_model"):
                ai_stats.setdefault("models_used", set()).add(ma["match_model"])
    cv_detail = (
        f"{len(attachments)} tailored CVs [{ai_cvs} Gemini AI, {fb_cvs} fallback]"
        if attachments
        else "0 tailored CVs"
    )
    b_tag = f"AI:{b_mod}" if b_src == "gemini" else "vault"
    kind = "Quiet-Day Briefing" if quiet_day else "Briefing"
    extra = f", +{overflow_count} carried over" if overflow_count else ""
    print(
        f"  [{sub_name}] Sent {kind} #{rel_ctx['edition']} ({len(jobs_to_send)} new roles{extra}, "
        f"{cv_detail}, briefing={b_tag}, angle #{pack['angle_number']}) -> subscriber '{sub_id}'."
    )
    return True


def process_subscribers(
    fetched, state, cfg, today, send_email=True, only_id=None, root=ROOT,
    board_count=109, ai_stats=None, companies=None,
):
    """Filter fetched jobs for each active subscriber, validate candidate-job fit via Gemini AI,
    update their personal seen/emailed/rejected state in state['subscribers'][sub_id], and send
    each subscriber's email containing ONLY strong, verified matches with clear rationales.

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
    jd_cache = {}

    if send_email and not (os.environ.get("GEMINI_API_KEY") or "").strip():
        gha_annotate(
            "warning",
            "GEMINI_API_KEY is not set in environment/secrets — LLM match validation & tailored CVs used deterministic fallback. "
            "Add GEMINI_API_KEY in GitHub Settings > Secrets and variables > Actions.",
        )

    for sub_cfg in subscribers:
        sub_id = sub_cfg["id"]
        sub_label = sub_cfg.get("name") or sub_id
        previous_state = copy.deepcopy(sub_states.get(sub_id))
        try:
            sub_rules = build_rules(sub_cfg)
            sub_matched = list({j["key"]: copy.deepcopy(j) for j in select(fetched, sub_rules, sub_cfg)}.values())
            enrich_company_metadata(sub_matched, companies=companies, root=root)

            sub_state = sub_states.setdefault(sub_id, {})
            if not isinstance(sub_state, dict):
                sub_state = sub_states[sub_id] = {}
            # Seed the 'emailed' ledger BEFORE today's keys are stamped so legacy subscribers are
            # never re-sent old roles, while brand-new subscribers start with an empty ledger.
            ensure_emailed_state(sub_state)
            ensure_rejected_state(sub_state)
            sub_baseline = update_state(sub_matched, sub_state, today, retention)
            sub_matched = dedupe_subscriber_jobs(sub_matched)
            new_days = int(sub_cfg.get("new_days", cfg["new_days"]))
            for j in sub_matched:
                j["is_new"] = flag_new(j, today, new_days)
                j["eff_date"] = j.get("posted_at") or j["first_seen"]
            sub_matched.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
            sub_matched.sort(key=lambda j: j["eff_date"], reverse=True)

            if send_email:
                candidates = select_jobs_for_email(sub_matched, sub_baseline, sub_cfg, sub_state=sub_state)
                max_em = int(sub_cfg.get("max_email_jobs", 12) or 12)
                eval_limit = max(24, max_em * 2) if max_em > 0 else len(candidates)
                eval_pool = candidates[:eval_limit]
                unevaluated_overflow = max(0, len(candidates) - len(eval_pool))

                matched_jobs, rejected_jobs = assess_candidate_job_matches(
                    eval_pool, sub_cfg, root=root, companies=companies, jd_cache=jd_cache
                )
                ai_eval_cnt = sum(
                    1 for j in (matched_jobs + rejected_jobs)
                    if (j.get("match_assessment") or {}).get("match_source") == "gemini"
                )
                if isinstance(ai_stats, dict):
                    ai_stats["ai_screened"] = ai_stats.get("ai_screened", 0) + ai_eval_cnt
                    ai_stats["ai_rejected"] = ai_stats.get("ai_rejected", 0) + len(rejected_jobs)
                    for j in matched_jobs + rejected_jobs:
                        ma = j.get("match_assessment") or {}
                        if ma.get("match_source") == "gemini" and ma.get("match_model"):
                            ai_stats.setdefault("models_used", set()).add(ma["match_model"])
                if rejected_jobs:
                    print(
                        f"  [{sub_label}] AI Match Gate filtered out {len(rejected_jobs)}/{len(eval_pool)} "
                        f"non-matching role(s); {len(matched_jobs)} verified match(es) kept."
                    )

                to_email, cap_overflow = cap_email_jobs(matched_jobs, sub_cfg, today)
                overflow = cap_overflow + unevaluated_overflow
                quiet_day = False
                skip_today = False
                if sub_baseline and not sub_cfg.get("email_on_baseline", True):
                    skip_today = True
                elif not to_email and (not sub_baseline or rejected_jobs):
                    q_mode = resolve_quiet_day_mode(sub_cfg)
                    if q_mode == "skip":
                        skip_today = True
                    elif q_mode == "spotlight" and not sub_baseline:
                        # Record today's rejected roles before selecting spotlight so they are excluded
                        if rejected_jobs and sub_cfg.get("email_mode") != "all":
                            record_rejected_jobs(sub_state, rejected_jobs, today, retention)
                            rejected_jobs = []
                        spot_pool = select_quiet_spotlight(sub_matched, sub_cfg, sub_state=sub_state)
                        to_email, spot_rejected = assess_candidate_job_matches(
                            spot_pool, sub_cfg, root=root, companies=companies, jd_cache=jd_cache
                        )
                        rejected_jobs.extend(spot_rejected)
                        quiet_day = not to_email
                    else:
                        quiet_day = True

                if skip_today:
                    if rejected_jobs and sub_cfg.get("email_mode") != "all":
                        record_rejected_jobs(sub_state, rejected_jobs, today, retention)
                    print(f"  [{sub_label}] Quiet day: no never-sent matching roles and quiet_day_mode=skip, no email.")
                else:
                    rejected_keys_now = {j.get("key") for j in rejected_jobs} | {
                        k for k in (sub_state.get("rejected_matches") or {})
                    }
                    board_jobs = [j for j in sub_matched if j.get("key") not in rejected_keys_now]
                    stamp_txt = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    board_html = render_subscriber_board(board_jobs, sub_cfg, stamp_txt) if overflow > 0 else None
                    attachments = prepare_tailored_cvs(to_email, sub_cfg, today, root=root) if to_email else []
                    sent = send_email_digest(
                        to_email, len(board_jobs), sub_cfg, today, root=root,
                        attachments=attachments, sub_state=sub_state, board_count=board_count,
                        ai_stats=ai_stats, quiet_day=quiet_day, overflow_count=overflow, board_html=board_html,
                        ai_rejected_count=len(rejected_jobs),
                    )
                    if sent:
                        if to_email:
                            record_emailed_jobs(sub_state, to_email, today, retention)
                        if rejected_jobs and sub_cfg.get("email_mode") != "all":
                            record_rejected_jobs(sub_state, rejected_jobs, today, retention)
        except Exception as e:
            gha_annotate("error", f"email delivery failed for {sub_id}: {type(e).__name__}: {e}")
            failed_ids.append(sub_id)
            if previous_state is None:
                sub_states.pop(sub_id, None)
            else:
                sub_states[sub_id] = previous_state
    return failed_ids


def strip_transient_fields(jobs):
    """Remove heavy job description / detail_api / transient LLM fields before writing static JSON files."""
    clean = []
    for j in jobs:
        item = {
            k: v for k, v in j.items()
            if k not in (
                "description", "detail_api", "llm_analysis", "match_assessment",
                "cv_filename", "is_career_upgrade", "upgrade_reason",
                "quiet_spotlight", "_wd_multi_term", "_upgrades_classified",
            )
        }
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
    try:
        import onboard_subscriber as _ob
        (docs / "onboard.html").write_text(_ob.render_onboard_page(cfg), encoding="utf-8")
    except Exception:
        pass
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
    enrich_company_metadata(fetched, companies=companies, root=root)
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
    enrich_company_metadata(jobs, companies=companies, root=root)

    for j in jobs:
        j["is_new"] = flag_new(j, today, int(cfg["new_days"]))
        j["eff_date"] = j.get("posted_at") or j["first_seen"]
    jobs.sort(key=lambda j: (j["company"].lower(), j["title"].lower()))
    jobs.sort(key=lambda j: j["eff_date"], reverse=True)

    # Process per-subscriber filtering, AI match screening, LLM CV tailoring, state updates, and email digests
    ai_stats = {
        "ai_screened": 0,
        "ai_rejected": 0,
        "gemini_cvs": 0,
        "fallback_cvs": 0,
        "gemini_briefings": 0,
        "vault_briefings": 0,
        "models_used": set(),
    }
    email_failures = process_subscribers(
        fetched, state, cfg, today, send_email=send_email,
        only_id=subscriber_id, root=root, board_count=len(companies),
        ai_stats=ai_stats, companies=companies,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fresh = write_outputs(root, cfg, jobs, state, today, stamp)
    ok = len(results) - len(failed)
    n_new = sum(1 for j in jobs if j["is_new"])
    total_cvs = ai_stats["gemini_cvs"] + ai_stats["fallback_cvs"]
    models_str = ", ".join(sorted(ai_stats["models_used"])) or "none"
    print(f"Fetched {ok}/{len(results)} boards. {len(jobs)} matching roles on main board, "
          f"{n_new} marked NEW, {len(fresh)} first seen this run.")
    if send_email:
        print(
            f"AI Telemetry: {ai_stats['ai_screened']} roles AI-screened ({ai_stats['ai_rejected']} non-matches filtered out) | "
            f"{ai_stats['gemini_cvs']}/{total_cvs} CVs generated by Gemini AI "
            f"({ai_stats['fallback_cvs']} fallback) | {ai_stats['gemini_briefings']} AI briefings "
            f"({ai_stats['vault_briefings']} vault) | models: {models_str}"
        )
    print(f"Page: {root / 'docs' / 'index.html'}")

    summary = [f"### Jobs Sentinel · {stamp}",
               f"- Boards fetched: **{ok}/{len(results)}**"
               + (f" (failed: {', '.join(c['name'] for c, _ in failed)})" if failed else ""),
               f"- Matching roles: **{len(jobs)}** · NEW: **{n_new}** · first seen this run: **{len(fresh)}**"]
    if send_email:
        summary.append(
            f"- Gemini AI Telemetry: **{ai_stats['ai_screened']} candidate roles AI-screened** "
            f"({ai_stats['ai_rejected']} weak/unrelated matches suppressed) · "
            f"**{ai_stats['gemini_cvs']}/{total_cvs} CVs tailored by Gemini AI** "
            f"({ai_stats['fallback_cvs']} deterministic fallback) · **{ai_stats['gemini_briefings']} AI daily briefings** "
            f"({ai_stats['vault_briefings']} vault) · active model(s): `{models_str}`"
        )
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
        ("Senior Director, Site Operations", "Example Semiconductors", "Semiconductors & AI Computing",
         "Global silicon design and validation hub in Guadalajara.", "Guadalajara, Jalisco, Mexico", "jalisco", "Operations", iso(1), True),
        ("Director of Engineering", "Example Cloud", "Enterprise Cloud & AI Infrastructure",
         "Global cloud platform engineering center in Zapopan.", "Zapopan, Jalisco", "jalisco", "Engineering", iso(2), True),
        ("VP, Latin America Operations", "Example Payments", "Fintech & Digital Payments",
         "Cross-border B2B and consumer digital payments platform.", "Remote - Mexico", "remote_mx", "Operations", iso(9), False),
        ("Head of Supply Chain", "Example Devices", "EMS & Smart Manufacturing",
         "High-complexity electronics manufacturing campus in Jalisco.", "Tlaquepaque, Jalisco", "jalisco", "Supply Chain", None, False),
        ("Directora de Transformación Digital", "Example Biopharma", "Biopharmaceuticals & GBS",
         "Global business services and digital innovation center in Mexico.", "Mexico", "mx_country", "Transformation", iso(20), False),
    ]
    jobs = [{"key": f"demo:{i}", "company": co, "company_industry": ind, "company_description": desc,
             "ats": "demo", "title": t, "location": loc,
             "team": team, "url": "https://example.com/jobs/" + str(i), "posted_at": posted,
             "loc_class": lc, "first_seen": iso(3 if posted is None else 0), "baseline": False,
             "is_new": new, "eff_date": posted or iso(3)}
            for i, (t, co, ind, desc, loc, lc, team, posted, new) in enumerate(rows)]
    out = Path(root) / "out" / "demo.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(jobs, cfg, "demo data"), encoding="utf-8")
    try:
        import onboard_subscriber as _ob
        (out.parent / "onboard.html").write_text(_ob.render_onboard_page(cfg), encoding="utf-8")
    except Exception:
        pass
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
    ap.add_argument("--model", help="override Gemini model (default: free-tier gemini-4-hydrogen)")
    ap.add_argument("--min-match-score", type=int, help="override minimum AI match score (0-100, default: 70)")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.model:
        os.environ["GEMINI_MODEL"] = args.model.strip()
        cfg["llm_model"] = args.model.strip()
    if args.min_match_score is not None:
        score_override = max(0, min(100, int(args.min_match_score)))
        os.environ["MIN_MATCH_SCORE"] = str(score_override)
        cfg["min_match_score"] = score_override
        for sub in cfg.get("subscribers") or []:
            sub["min_match_score"] = score_override
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
