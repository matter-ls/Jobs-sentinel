#!/usr/bin/env python3
"""Candidate Self-Service Onboarding Engine for Jobs Sentinel (Lucy).

Processes web-form submissions (from `docs/onboard.html` via GitHub `repository_dispatch`,
`workflow_dispatch`, CLI JSON file, or the built-in `--serve` local web server):
  1. Validates candidate email, name, uploaded plain-text CV, checkbox selections, and free-text goals.
  2. Structures & enriches the uploaded CV into `cvs/<subscriber_id>.md` using Free-Tier Gemini
     (`gemini-4-hydrogen` with automatic cascade & deterministic fallback), adding ATS-safe
     `*Company Profile: ...*` industry/scale context under every past employer.
  3. Synthesizes calibrated `career_goal`, `title_include`, `upgrade_title_include`, and
     `title_exclude` rules in `config.json` so the new subscriber automatically starts receiving
     AI-validated daily matches.
  4. Sends a polished, executive Confirmation Email from Lucy confirming their active radar blueprint.

Standard library only (Python 3.9+, zero third-party packages).
"""
import argparse
import copy
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fetch_jobs as fj

ROOT = fj.ROOT

MAX_CV_CHARS = 45_000
MIN_CV_CHARS = 60
MAX_FREE_TEXT_CHARS = 4_000
STRICT_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,180}\.[A-Za-z]{2,24}$")

# --------------------------------------------------------------------------
# Agnostic, Standard Onboarding Options (Broad Signals + Free-Text Model Feedback)
# --------------------------------------------------------------------------

SENIORITY_OPTIONS = [
    {
        "id": "specialist",
        "label": "Specialist / Individual Contributor",
        "hint": "Analyst, Engineer, Representative, Associate, Coordinator",
        "inc": [
            r"\b(analyst|analista|specialist|especialista|engineer|ingenier[oa]|associate|representative|representante|coordinator|coordinador(a)?|developer|designer)\b",
        ],
        "up": [
            r"\b(senior|sr\.?|lead|lider|manager|gerente|principal|architect)\b",
        ],
    },
    {
        "id": "senior",
        "label": "Senior / Lead",
        "hint": "Senior, Lead, Principal, Staff, Consultant, Architect",
        "default": True,
        "inc": [
            r"\b(senior|sr\.?|lead|l[ií]der|principal|staff|consultant|consultor(a)?|architect|arquitect[oa]|strategist|advisor|asesor(a)?)\b",
        ],
        "up": [
            r"\b(manager|gerente|senior manager|sr\.? manager|team lead|principal|architect|director(a)?|head of)\b",
        ],
    },
    {
        "id": "manager",
        "label": "Manager",
        "hint": "Manager, Senior Manager, Team Lead, Program Manager",
        "default": True,
        "inc": [
            r"\b(manager|gerente|senior manager|sr\.? manager|gerente (senior|sr\.?)|team lead|delivery lead|supervisor(a)?)\b",
        ],
        "up": [
            r"\b(senior manager|sr\.? manager|gerente (senior|sr\.?)|director(a)?|head of|vp|vice[- ]?president(e|a)?)\b",
        ],
    },
    {
        "id": "director",
        "label": "Director / Head",
        "hint": "Director, Senior Director, Head of Area",
        "inc": [
            r"\bdirector(a)?\b",
            r"\bhead of\b",
        ],
        "up": [
            r"\b(senior director|sr\.? director|executive director|vp|vice[- ]?president(e|a)?|head of)\b",
            r"\bdirector(a)?\b",
        ],
    },
    {
        "id": "executive",
        "label": "VP / C-Level / GM",
        "hint": "VP, SVP, General Manager, Country Manager, Chief Officer",
        "inc": [
            r"\b(vp|svp|evp|vice[- ]?president(e|a)?)\b",
            r"\b(general manager|gerente general|managing director)\b",
            r"\bcountry (manager|lead|head|director)\b",
            r"\bchief\b.*\bofficer\b",
        ],
        "up": [
            r"\b(svp|evp|chief\b.*\bofficer|managing director|general manager|country (manager|head))\b",
            r"\b(vp|vice[- ]?president(e|a)?)\b",
        ],
    },
]

DOMAIN_OPTIONS = [
    {
        "id": "operations",
        "label": "Operations & Supply Chain",
        "hint": "Operations, Logistics, Manufacturing, Procurement, Quality",
        "keywords_re": r"(operations|operaciones|supply chain|cadena de suministro|logistics|logistica|manufacturing|manufactura|plant|quality|calidad|procurement|compras|sourcing|planning|fulfillment)",
        "standalone_inc": [
            r"\b(operations|operaciones|supply chain|logistics|logistica|manufacturing|quality|procurement|compras)\b.*\b(director(a)?|head|vp|manager|gerente|lead|l[ií]der|specialist|analyst|coordinator|planner)\b",
            r"\b(director(a)?|head|vp|general manager|manager|gerente|lead|l[ií]der)\b.*\b(operations|operaciones|supply chain|logistics|logistica|manufacturing|plant|quality|procurement|compras)\b",
        ],
    },
    {
        "id": "technology",
        "label": "Engineering, Data & IT",
        "hint": "Software, Data & Analytics, Cloud, IT, AI & Automation",
        "keywords_re": r"(engineering|ingenieria|software|platform|cloud|data|analytics|business intelligence|\bbi\b|devops|sre|infrastructure|architecture|cybersecurity|security|\bai\b|machine learning|automation|automatizacion|rpa|low[- ]code|\bit\b|sistemas)",
        "standalone_inc": [
            r"\b(software|backend|frontend|full[- ]stack|cloud|data|analytics|devops|sre|platform|security|cybersecurity|machine learning|\bai\b|automation|rpa|systems)\b.*\b(engineer|developer|architect|scientist|analyst|manager|lead|director(a)?|head|vp|specialist|consultant)\b",
            r"\b(director(a)?|head|vp|manager|gerente|lead|principal|staff|architect)\b.*\b(engineering|ingenieria|software|platform|cloud|data|analytics|infrastructure|security|\bit\b|technology|tecnologia|automation)\b",
        ],
    },
    {
        "id": "project_product",
        "label": "Project, Program & Product",
        "hint": "Project/Program Management, Product, PMO, Agile, Transformation",
        "keywords_re": r"(project|proyecto|proyectos|program|programa|portfolio|portafolio|\bpmo\b|product|producto|agile|scrum|delivery|transformation|transformacion)",
        "standalone_inc": [
            r"\b(project|program|portfolio|product|technical program|delivery)\b.*\b(manager|lead|leader|director(a)?|head|vp|owner|coordinator|specialist|coach)\b",
            r"\b(gerente|l[ií]der|director(a)?|coordinador(a)?)\b.*\b(proyecto|proyectos|programa|portafolio|producto|transformaci[oó]n)\b",
            r"\b(\bpmo\b|scrum master|agile coach|agile delivery|digital transformation|business transformation)\b",
        ],
    },
    {
        "id": "sales_customer",
        "label": "Sales & Customer Success",
        "hint": "Sales, Account Management, Business Development, Customer Success / CX",
        "keywords_re": r"(sales|ventas|comercial|commercial|account executive|account manager|\bkam\b|business development|\bbdr\b|\bsdr\b|customer success|\bcsm\b|customer experience|\bcx\b|renewals?|retention|revenue|channel|partner|alliances?)",
        "standalone_inc": [
            r"\b(account executive|sales executive|commercial executive|key account manager|\bkam\b|account manager|client partner|customer success manager|\bcsm\b)\b",
            r"\b(sales|business development|commercial|customer success|client success|customer experience|\bcx\b|renewals?|partner|channel)\b.*\b(representative|specialist|executive|manager|lead|director(a)?|head|vp|consultant|coordinator)\b",
            r"\b(ejecutiv[oa]|gerente|director(a)?|l[ií]der|representante|especialista)\b.*\b(ventas|comercial|cuentas|desarrollo de negocios|[eé]xito del cliente|experiencia del cliente)\b",
        ],
    },
    {
        "id": "strategy_process",
        "label": "Strategy, Consulting & Process",
        "hint": "Business Strategy, Process Improvement, Shared Services / GBS, Change",
        "keywords_re": r"(strategy|estrategia|business operations|bizops|consulting|consultoria|process|procesos|continuous improvement|mejora continua|operational excellence|\bgbs\b|shared services|change management|gestion del cambio|enablement)",
        "standalone_inc": [
            r"\b(strategy|estrategia|business operations|bizops|process improvement|process excellence|operational excellence|continuous improvement|mejora continua|process mining|change management|gesti[oó]n del cambio|\bgbs\b|shared services)\b",
            r"\b(business|process|management|strategy|transformation|operations)\b.*\b(consultant|consultor(a)?|strategist|analyst|analista|partner|manager|gerente|lead|director(a)?|head)\b",
        ],
    },
    {
        "id": "finance_legal",
        "label": "Finance, Accounting & Legal",
        "hint": "Finance, FP&A, Accounting, Audit, Tax, Treasury, Legal & Compliance",
        "keywords_re": r"(finance|finanzas|financial|\bfp&a\b|accounting|contabilidad|controller|contralor|audit|auditoria|tax|impuestos|treasury|tesoreria|legal|counsel|abogad[oa]|compliance|cumplimiento)",
        "standalone_inc": [
            r"\b(finance|finanzas|financial|\bfp&a\b|accounting|controller|audit|tax|treasury|legal|compliance)\b.*\b(analyst|specialist|manager|gerente|lead|director(a)?|head|vp|counsel|partner|officer)\b",
            r"\b(director(a)?|head|vp|manager|gerente|lead|analista|especialista|contralor)\b.*\b(finanzas|finance|contabilidad|auditor[ií]a|impuestos|tesorer[ií]a|legal|cumplimiento)\b",
        ],
    },
    {
        "id": "people_hr",
        "label": "People, HR & Talent",
        "hint": "Human Resources, Talent Acquisition, People Operations, Learning & Development",
        "keywords_re": r"(human resources|recursos humanos|\bhr\b|\bhrbp\b|people|talent|talento|recruiting|recruitment|reclutamiento|compensation|total rewards|learning|training|capacitacion)",
        "standalone_inc": [
            r"\b(human resources|recursos humanos|\bhr\b|\bhrbp\b|people|talent acquisition|talent management|recruiting|total rewards|learning)\b.*\b(specialist|partner|generalist|recruiter|manager|gerente|lead|director(a)?|head|vp|coordinator)\b",
        ],
    },
    {
        "id": "marketing",
        "label": "Marketing, Comms & Design",
        "hint": "Marketing, Growth, Brand, Communications, Content, UX / Product Design",
        "keywords_re": r"(marketing|mercadotecnia|growth|brand|marca|communications|comunicacion|public relations|demand generation|content|design|dise[nñ]o|\bux\b|\bui\b|creative)",
        "standalone_inc": [
            r"\b(marketing|mercadotecnia|growth|brand|communications|comunicaci[oó]n|content|product design|\bux\b|\bui\b|creative)\b.*\b(specialist|strategist|designer|manager|gerente|lead|director(a)?|head|vp|coordinator)\b",
        ],
    },
    {
        "id": "other",
        "label": "Other / Open (Guided by Free Text)",
        "hint": "Let the AI infer exact target roles from my CV and free-text description below",
        "keywords_re": r"",
        "standalone_inc": [],
    },
]

LOCATION_OPTIONS = [
    {
        "id": "jalisco",
        "label": "On-Site / Hybrid in Jalisco",
        "hint": "Guadalajara, Zapopan, Tlaquepaque, Tlajomulco & El Salto",
        "default": True,
    },
    {
        "id": "remote_mx",
        "label": "Remote in Mexico",
        "hint": "Work remotely from anywhere in Mexico",
        "default": True,
    },
    {
        "id": "mx_country",
        "label": "Mexico-Wide (Flexible City)",
        "hint": "Country-wide Mexico roles with no city restriction",
        "default": True,
    },
]

LANGUAGE_OPTIONS = ("English", "Spanish", "Portuguese")

SENIORITY_BY_ID = {o["id"]: o for o in SENIORITY_OPTIONS}
DOMAIN_BY_ID = {o["id"]: o for o in DOMAIN_OPTIONS}
LOCATION_BY_ID = {o["id"]: o for o in LOCATION_OPTIONS}

# Backward-compatible aliases so earlier packets / workflow defaults map seamlessly
LEGACY_LEVEL_ALIASES = {
    "c_exec": "executive",
    "sr_manager": "manager",
    "senior_ic": "senior",
    "ic": "specialist",
}
LEGACY_DOMAIN_ALIASES = {
    "ops_supply_chain": "operations",
    "gbs_process": "strategy_process",
    "automation_ai": "technology",
    "engineering_cloud": "technology",
    "tpm_pmo": "project_product",
    "sales_commercial": "sales_customer",
    "change_cx": "strategy_process",
    "finance_hr_strategy": "finance_legal",
}

BASE_TITLE_EXCLUDES = [
    r"\bassistant to\b",
    r"\bexecutive assistant\b",
    r"\bdirector'?s? assistant\b",
    r"\bintern(ship)?\b",
    r"\bpracticante\b",
    r"\bbecari[oa]\b",
]

ONBOARD_CV_SYSTEM_PROMPT = """You are an elite executive resume architect for Jobs Sentinel (Lucy).
Convert the candidate's raw plain-text CV into a clean, truthful, ATS-safe structured JSON representation so we can save their canonical master CV (`cvs/<id>.md`) with enhanced employer profiles.

STRICT RULES:
1. 100% TRUTHFULNESS: Extract ONLY real roles, employers, dates, education, certifications, languages, and metrics from the candidate's uploaded CV text. NEVER invent employers, degrees, dates, or numbers.
2. ENHANCED COMPANY PROFILES (`company_profile`): For EVERY employer in `experience`, write a concise, factual 1-2 sentence description (20-38 words) of what that company is, its industry sector, scale/business model, and regional/hub context (similar to: "Global biopharmaceutical leader in oncology and rare diseases, operating a major Global Innovation and Technology Centre in Guadalajara"). If a company is a niche/local firm, describe its industry and scope truthfully based on the CV context without inventing revenue figures.
3. ATS-SAFE CHARACTERS ONLY: NEVER output any of these characters anywhere: < > [ ] " { } \\
   Use parentheses ( ) instead of brackets, 'over'/'under' instead of >/<, and standard hyphens.

Return ONLY valid JSON matching this exact schema:
{
  "full_name": "Candidate Full Name",
  "contact_line": "City, Country | email@example.com",
  "summary": "3-4 sentence executive summary grounded strictly in their real experience and target direction.",
  "core_competencies": [
    {"category": "Competency Group Title", "items": "Skill 1, Skill 2, Skill 3, Skill 4"}
  ],
  "experience": [
    {
      "company": "Employer Name",
      "location": "City, Country",
      "company_profile": "Concise 1-2 sentence description of the company, its industry, business model, and hub context.",
      "role": "Job Title",
      "dates": "Start - End",
      "bullets": [
        "Action-oriented achievement bullet grounded strictly in the uploaded CV."
      ]
    }
  ],
  "education": [
    "Degree or Certification — Institution (Year if present)"
  ],
  "languages": [
    "Language (Proficiency)"
  ]
}"""

ONBOARD_PROFILE_SYSTEM_PROMPT = """You are Lucy, an AI talent headhunter calibrating a new subscriber's personalized daily job radar across 109 employer boards in Jalisco and Remote Mexico.

IMPORTANT CONTEXT:
The onboarding form checkboxes (Role Level, General Area, Location) are intentionally broad, industry-agnostic signals.
Treat the candidate's **FREE-TEXT DESCRIPTION** (`CANDIDATE FREE-TEXT FEEDBACK`) together with their **CV** as your PRIMARY source of truth for understanding exact job titles, specialties, career goals, preferred industries, and roles they want to skip!

Produce:
1. `career_goal`: A rich, specific 2-4 sentence Career North Star paragraph synthesizing what the candidate wrote in free text + their real CV background + target seniority + explicit role types/industries to exclude. (This paragraph is fed directly into the daily Gemini Candidate-Job Match Gate prompt to evaluate every single job opening and reject weak matches!)
2. `extra_title_include`: Up to 12 Python case-insensitive regex patterns (using `\\b` word boundaries, covering both English and Spanish title variations where relevant) that capture the specific job titles, roles, and specialties described in their free-text feedback and CV. Never output wildcard-only patterns like `.*`.
3. `extra_upgrade_include`: Up to 6 Python case-insensitive regex patterns (using `\\b` word boundaries) representing ambitious step-up / promotion titles for this candidate.
4. `extra_title_exclude`: Up to 10 Python case-insensitive regex patterns (using `\\b` word boundaries) for roles the candidate asked to avoid in free text OR common false-positive titles unrelated to their craft.

Return ONLY valid JSON:
{
  "career_goal": "2-4 sentence north-star career target and exclusions...",
  "extra_title_include": ["\\\\bexample target role\\\\b"],
  "extra_upgrade_include": ["\\\\bstep up role\\\\b"],
  "extra_title_exclude": ["\\\\bunwanted role\\\\b"]
}"""


# --------------------------------------------------------------------------
# Validation, Slug Generation & Regex Safety Guards
# --------------------------------------------------------------------------

def _clean_line(val, max_len=300):
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(val or "")).strip()
    return re.sub(r"\s+", " ", s)[:max_len]


def _normalize_list_field(raw_val, allowed_map, aliases=None):
    aliases = aliases or {}
    if isinstance(raw_val, str):
        items = [x.strip() for x in re.split(r"[,;|]", raw_val) if x.strip()]
    elif isinstance(raw_val, (list, tuple, set)):
        items = [str(x).strip() for x in raw_val if str(x).strip()]
    else:
        items = []
    out = []
    for item in items:
        key = aliases.get(item.lower(), item.lower())
        if key in allowed_map and key not in out:
            out.append(key)
    return out


def validate_onboarding_payload(payload):
    """Validate and normalize untrusted onboarding form input. Raises ValueError on invalid fields."""
    if not isinstance(payload, dict):
        raise ValueError("Onboarding payload must be a JSON object.")

    raw_email = str(payload.get("email") or "").strip().lower()
    if not raw_email or any(ch in raw_email for ch in ("\r", "\n", "<", ">", '"', "'", ";", " ")):
        raise ValueError("Please provide a valid email address.")
    if not STRICT_EMAIL_RE.fullmatch(raw_email):
        raise ValueError(f"Invalid email address format: {raw_email!r}")

    raw_cv = str(payload.get("cv_text") or "").replace("\r\n", "\n").replace("\r", "\n")
    raw_cv = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_cv).strip()
    if len(raw_cv) < MIN_CV_CHARS:
        raise ValueError(
            f"Uploaded CV text is too short ({len(raw_cv)} chars). Please upload a .txt/.md resume or paste your full CV text (at least {MIN_CV_CHARS} characters)."
        )
    if len(raw_cv) > MAX_CV_CHARS:
        raw_cv = raw_cv[:MAX_CV_CHARS]

    raw_name = _clean_line(payload.get("name") or "", max_len=80)
    raw_name = fj.sanitize_ats_text(raw_name)
    if not raw_name:
        # Infer from first non-empty line of CV or email local-part
        first_cv_line = next((ln.strip(" #*\t-") for ln in raw_cv.splitlines() if ln.strip(" #*\t-")), "")
        if 2 <= len(first_cv_line) <= 60 and "@" not in first_cv_line:
            raw_name = fj.sanitize_ats_text(first_cv_line).title()
        else:
            local = raw_email.split("@", 1)[0]
            raw_name = re.sub(r"[._+-]+", " ", local).title()[:60] or "Candidate"

    levels = _normalize_list_field(payload.get("levels"), SENIORITY_BY_ID, aliases=LEGACY_LEVEL_ALIASES)
    if not levels:
        levels = ["senior", "manager", "director"]

    domains = _normalize_list_field(payload.get("domains"), DOMAIN_BY_ID, aliases=LEGACY_DOMAIN_ALIASES)
    if not domains:
        domains = ["other"]

    locations = _normalize_list_field(payload.get("locations"), LOCATION_BY_ID)
    if not locations:
        locations = ["jalisco", "remote_mx", "mx_country"]

    raw_lang = _clean_line(payload.get("language") or "English", max_len=24).title()
    language = raw_lang if raw_lang in LANGUAGE_OPTIONS else "English"

    free_text = str(payload.get("free_text") or "").replace("\r\n", "\n").replace("\r", "\n")
    free_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", free_text).strip()[:MAX_FREE_TEXT_CHARS]

    return {
        "name": raw_name,
        "email": raw_email,
        "cv_text": raw_cv,
        "levels": levels,
        "domains": domains,
        "locations": locations,
        "language": language,
        "free_text": free_text,
    }


def derive_subscriber_id(name, email, existing_subscribers=None):
    """Derive a clean, unique subscriber ID (`^[a-z][a-z0-9_]{1,29}$`).
    If `email` already matches an existing subscriber, returns their existing ID so re-submitting
    the onboarding form cleanly updates their CV and preferences without duplicating entries."""
    email_low = (email or "").strip().lower()
    taken_ids = set()
    for sub in existing_subscribers or []:
        if not isinstance(sub, dict):
            continue
        sid = str(sub.get("id") or "").strip()
        sub_email = str(sub.get("recipient_email") or "").strip().lower()
        if sid and sub_email == email_low:
            return sid, True
        if sid:
            taken_ids.add(sid.lower())

    tokens = re.findall(r"[a-z0-9]+", fj.norm(name or ""))
    if not tokens:
        tokens = re.findall(r"[a-z0-9]+", fj.norm((email or "user").split("@", 1)[0]))
    first = tokens[0] if tokens else "subscriber"
    if not first[0].isalpha():
        first = "sub_" + first

    candidates = [first[:28]]
    if len(tokens) >= 2:
        candidates.append(f"{first}_{tokens[-1]}"[:28])
        candidates.append(f"{first}_{tokens[-1][0]}"[:28])
    for n in range(2, 100):
        candidates.append(f"{first[:24]}_{n}")

    for cand in candidates:
        clean_cand = re.sub(r"[^a-z0-9_]+", "", cand).strip("_")
        if len(clean_cand) >= 2 and clean_cand not in taken_ids and clean_cand != "all":
            return clean_cand, False
    return f"sub_{int(time.time()) % 100000}", False


def _safe_validate_regex(pat):
    """Ensure a candidate regex compiles cleanly, is reasonably bounded, and does NOT match
    arbitrary unrelated strings (blocks overly broad patterns like `.*` or `\\w+`)."""
    if not isinstance(pat, str):
        return None
    s = pat.strip()
    if len(s) < 4 or len(s) > 180:
        return None
    if s in (".*", ".+", "\\w+", "[a-z]+"):
        return None
    try:
        compiled = re.compile(s, re.I)
    except re.error:
        return None
    # Guard against match-everything patterns
    for Canary in ("", "zzzzzxqjv", "12345", "unrelated random token xyz"):
        if compiled.search(Canary):
            return None
    return s


# --------------------------------------------------------------------------
# Master CV Structuring & Employer Profile Enhancement
# --------------------------------------------------------------------------

def _render_canonical_cv_markdown(cv_struct, fallback_name, fallback_email):
    """Render structured CV JSON into ATS-clean Markdown (`cvs/<id>.md`) guaranteed to contain
    zero ATS-illegal characters (`< > [ ] " { } \\`)."""
    full_name = fj.sanitize_ats_text(cv_struct.get("full_name") or fallback_name or "Candidate").upper()
    contact_line = fj.sanitize_ats_text(cv_struct.get("contact_line") or fallback_email)
    if fallback_email.lower() not in contact_line.lower():
        contact_line = f"{contact_line} | {fallback_email}" if contact_line else fallback_email

    summary = fj.sanitize_ats_text(cv_struct.get("summary") or "")

    md = [
        f"# {full_name}",
        contact_line,
        "",
        "## PROFESSIONAL SUMMARY",
        summary or f"Experienced professional targeting high-impact roles aligned with proven execution track record.",
        "",
        "## CORE COMPETENCIES",
    ]

    comps = cv_struct.get("core_competencies") or []
    wrote_comp = False
    for comp in comps:
        if isinstance(comp, dict):
            cat = fj.sanitize_ats_text(comp.get("category") or "Core Skills")
            items = fj.sanitize_ats_text(comp.get("items") or "")
            if items:
                md.append(f"- **{cat}:** {items}")
                wrote_comp = True
        elif isinstance(comp, str) and comp.strip():
            md.append(f"- {fj.sanitize_ats_text(comp)}")
            wrote_comp = True
    if not wrote_comp:
        md.append("- **Core Capabilities:** Strategic Execution, Cross-Functional Leadership, Process Improvement, Stakeholder Alignment")

    md.extend(["", "## PROFESSIONAL EXPERIENCE"])
    for exp in cv_struct.get("experience") or []:
        if not isinstance(exp, dict):
            continue
        co = fj.sanitize_ats_text(exp.get("company") or "")
        loc = fj.sanitize_ats_text(exp.get("location") or "")
        prof = fj.sanitize_ats_text(exp.get("company_profile") or "")
        role = fj.sanitize_ats_text(exp.get("role") or "")
        dates = fj.sanitize_ats_text(exp.get("dates") or "")
        bullets = [fj.sanitize_ats_text(b) for b in (exp.get("bullets") or []) if fj.sanitize_ats_text(b)]
        if not (co or role):
            continue
        header = f"### {co or 'Organization'}" + (f" | {loc}" if loc else "")
        md.append("")
        md.append(header)
        if prof:
            prof_clean = re.sub(r"^company profile:?\s*", "", prof, flags=re.I).strip()
            if prof_clean:
                md.append(f"*Company Profile: {prof_clean}*")
        role_line = f"**{role or 'Professional Role'}**" + (f" | {dates}" if dates else "")
        md.append(role_line)
        for b in bullets:
            md.append(f"- {b}")

    edu_list = [fj.sanitize_ats_text(e) for e in (cv_struct.get("education") or []) if fj.sanitize_ats_text(e)]
    if edu_list:
        md.extend(["", "## EDUCATION & CERTIFICATIONS"])
        for e in edu_list:
            md.append(f"- {e}")

    lang_list = [fj.sanitize_ats_text(l) for l in (cv_struct.get("languages") or []) if fj.sanitize_ats_text(l)]
    if lang_list:
        md.extend(["", "## LANGUAGES", "- " + " | ".join(lang_list)])

    text_out = "\n".join(md).strip() + "\n"
    # Final safety sweep: guarantee zero ATS-illegal characters anywhere in the Markdown file
    if fj.ATS_ILLEGAL_CHARS_RE.search(text_out):
        cleaned_lines = []
        for ln in text_out.splitlines():
            cleaned_lines.append(fj.ATS_ILLEGAL_CHARS_RE.sub("", ln))
        text_out = "\n".join(cleaned_lines).strip() + "\n"
    return text_out


def structure_master_cv_deterministic(raw_cv_text, name, email):
    """Offline/fallback parser that converts plain-text or Markdown CV text into clean, ATS-safe
    canonical Markdown (`cvs/<id>.md`) while preserving existing structure or extracting bullets."""
    lines = [ln.strip() for ln in (raw_cv_text or "").splitlines()]
    # If the user already uploaded a Markdown CV with `## PROFESSIONAL EXPERIENCE`, sanitize & enrich it
    has_md_sections = any(ln.upper().startswith("## PROFESSIONAL") or ln.upper().startswith("## EXPERIENCE") for ln in lines)
    if has_md_sections:
        sanitized_lines = []
        prev_was_company = False
        last_company_name = ""
        for idx, ln in enumerate(lines):
            if not ln:
                sanitized_lines.append("")
                continue
            if ln.startswith("# "):
                sanitized_lines.append("# " + fj.sanitize_ats_text(ln[2:]).upper())
            elif ln.startswith("## "):
                sec = fj.sanitize_ats_text(ln[3:]).upper()
                if sec == "EXPERIENCE":
                    sec = "PROFESSIONAL EXPERIENCE"
                elif sec in ("SUMMARY", "EXECUTIVE SUMMARY"):
                    sec = "PROFESSIONAL SUMMARY"
                elif sec in ("SKILLS", "COMPETENCIES"):
                    sec = "CORE COMPETENCIES"
                sanitized_lines.append("## " + sec)
            elif ln.startswith("### "):
                parts = [fj.sanitize_ats_text(p) for p in ln[4:].split("|")]
                last_company_name = parts[0]
                sanitized_lines.append("### " + " | ".join(p for p in parts if p))
                next_nonempty = next((lines[k] for k in range(idx + 1, len(lines)) if lines[k]), "")
                if not next_nonempty.lower().startswith("*company profile"):
                    co_meta = fj.get_company_metadata(last_company_name)
                    if co_meta.get("description"):
                        sanitized_lines.append(f"*Company Profile: {fj.sanitize_ats_text(co_meta['description'])}*")
                    elif last_company_name:
                        sanitized_lines.append(
                            f"*Company Profile: {last_company_name} — enterprise organization where candidate delivered core operational and domain initiatives.*"
                        )
            elif ln.startswith("*") and ln.endswith("*") and not ln.startswith("**"):
                inner = fj.sanitize_ats_text(ln.strip("*"))
                if not inner.lower().startswith("company profile:"):
                    inner = "Company Profile: " + inner
                sanitized_lines.append(f"*{inner}*")
            elif ln.startswith("**"):
                parts = [fj.sanitize_ats_text(p) for p in ln.split("|")]
                role_part = f"**{parts[0]}**"
                rest = " | ".join(p for p in parts[1:] if p)
                sanitized_lines.append(f"{role_part} | {rest}" if rest else role_part)
            elif ln.startswith(("- ", "* ", "• ")):
                sanitized_lines.append("- " + fj.sanitize_ats_text(re.sub(r"^[-*•]\s*", "", ln)))
            else:
                sanitized_lines.append(fj.sanitize_ats_text(ln))
        out = "\n".join(sanitized_lines).strip() + "\n"
        return out

    # Plain-text CV fallback: segment into summary, skills, experience bullets, and education
    non_empty = [fj.sanitize_ats_text(ln) for ln in lines if fj.sanitize_ats_text(ln)]
    # Drop lines that just repeat the candidate's name or email at the top
    body_lines = [
        ln for ln in non_empty
        if fj.norm(ln) != fj.norm(name) and email.lower() not in ln.lower()
    ]
    summary_lines = body_lines[:3] or ["Experienced professional seeking high-impact roles aligned with core domain strengths."]
    bullet_lines = [re.sub(r"^[-•*]\s*", "", ln) for ln in body_lines[3:22] if len(ln) > 12]
    if not bullet_lines:
        bullet_lines = [re.sub(r"^[-•*]\s*", "", ln) for ln in body_lines if len(ln) > 12][:8]

    cv_struct = {
        "full_name": name,
        "contact_line": email,
        "summary": " ".join(summary_lines)[:650],
        "core_competencies": [
            {"category": "Core Profile & Strengths", "items": ", ".join(w[:48] for w in summary_lines[:2]) or "Leadership, Strategy, Execution"}
        ],
        "experience": [
            {
                "company": "Prior Experience Portfolio",
                "location": "Mexico",
                "company_profile": "Enterprise and domain experience extracted from uploaded candidate resume.",
                "role": "Senior Professional",
                "dates": "Career History",
                "bullets": bullet_lines[:10] or ["Delivered measurable operational and functional outcomes across prior roles."],
            }
        ],
        "education": [],
        "languages": ["English", "Spanish"],
    }
    return _render_canonical_cv_markdown(cv_struct, name, email)


def structure_master_cv_markdown(raw_cv_text, name, email, api_key=None, model=fj.DEFAULT_LLM_MODEL):
    """Structure and enrich raw CV text into canonical `cvs/<id>.md` with `*Company Profile: ...*`
    using Gemini (`gemini-4-hydrogen`), falling back cleanly to `structure_master_cv_deterministic`."""
    api_key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY") or "").strip()
    if api_key:
        prompt = (
            f"CANDIDATE NAME: {name}\n"
            f"CANDIDATE EMAIL: {email}\n\n"
            f"UPLOADED RAW CV TEXT:\n{raw_cv_text[:30000]}"
        )
        try:
            parsed = fj._invoke_gemini_json(
                prompt,
                system_text=ONBOARD_CV_SYSTEM_PROMPT,
                model=model,
                api_key=api_key,
                temperature=0.15,
                max_output_tokens=6144,
                timeout=55,
                retries=2,
                validator=lambda p: isinstance(p, dict) and isinstance(p.get("experience"), list) and len(p.get("experience")) > 0,
            )
            if parsed:
                return _render_canonical_cv_markdown(parsed, name, email), str(parsed.get("_llm_model") or model)
        except Exception as e:
            fj.gha_annotate("warning", f"Onboarding CV Gemini structuring fallback ({type(e).__name__}: {e})")
    return structure_master_cv_deterministic(raw_cv_text, name, email), "deterministic-fallback"


# --------------------------------------------------------------------------
# Smart Radar Rule & Career-Goal Synthesis
# --------------------------------------------------------------------------

def synthesize_subscriber_profile(validated, structured_cv_md, api_key=None, model=fj.DEFAULT_LLM_MODEL):
    """Build `career_goal`, `title_include`, `upgrade_title_include`, `title_exclude`, and location
    settings from broad agnostic checkbox signals + primary free-text feedback + structured CV."""
    levels = validated["levels"]
    domains = validated["domains"]
    locations = validated["locations"]
    free_text = validated["free_text"]

    title_include = []
    upgrade_include = []
    title_exclude = list(BASE_TITLE_EXCLUDES)

    def _add_unique(target_list, patterns):
        for p in patterns:
            valid = _safe_validate_regex(p)
            if valid and valid not in target_list:
                target_list.append(valid)

    specific_domains = [d for d in domains if d in DOMAIN_BY_ID and d != "other"]

    # 1. Broad deterministic guardrails from selected levels + general areas
    for lv_id in levels:
        lv = SENIORITY_BY_ID[lv_id]
        _add_unique(upgrade_include, lv["up"])
        if lv_id in ("executive", "director") or not specific_domains:
            _add_unique(title_include, lv["inc"])

    for dom_id in specific_domains:
        dom = DOMAIN_BY_ID[dom_id]
        _add_unique(title_include, dom["standalone_inc"])
        kw = dom["keywords_re"]
        if kw and any(l in levels for l in ("manager", "senior", "specialist")):
            _add_unique(title_include, [
                rf"\b(senior manager|sr\.? manager|manager|gerente|lead|l[ií]der|principal|architect|specialist|especialista|consultant|analyst|analista)\b.*\b{kw}\b",
                rf"\b{kw}\b.*\b(senior manager|sr\.? manager|manager|gerente|lead|l[ií]der|principal|architect|specialist|especialista|consultant|analyst|analista)\b",
            ])

    if not title_include:
        for lv_id in levels:
            _add_unique(title_include, SENIORITY_BY_ID[lv_id]["inc"])

    # Agnostic baseline noise exclusions
    if "sales_customer" not in specific_domains:
        _add_unique(title_exclude, [
            r"\b(call center agent|telemarketing|retail store|cashier|promotor de cambaceo)\b",
        ])
    if "specialist" not in levels:
        _add_unique(title_exclude, [r"\b(junior|jr\.?|entry[- ]level|trainee)\b"])

    # 2. Baseline career_goal combining broad selections + candidate's exact free-text feedback
    level_labels = [SENIORITY_BY_ID[k]["label"] for k in levels if k in SENIORITY_BY_ID]
    domain_labels = [DOMAIN_BY_ID[k]["label"] for k in specific_domains if k in DOMAIN_BY_ID]
    loc_labels = [LOCATION_BY_ID[k]["label"] for k in locations if k in LOCATION_BY_ID]

    goal_parts = [
        f"Targeting {', '.join(level_labels)} roles"
        + (f" in {', '.join(domain_labels)}" if domain_labels else "")
        + f" ({', '.join(loc_labels)})."
    ]
    if free_text:
        goal_parts.append(f"Candidate goal & preferences: {fj.sanitize_ats_text(free_text)}")
    career_goal = " ".join(goal_parts)[:1200]

    # 3. Primary AI Calibration: Gemini turns candidate's free-text + CV into bespoke rules & Match Gate prompt
    api_key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY") or "").strip()
    rule_source = "deterministic"
    if api_key:
        prompt = (
            f"CANDIDATE NAME: {validated['name']}\n"
            f"BROAD ROLE LEVEL SELECTION: {', '.join(level_labels)}\n"
            f"BROAD FUNCTIONAL AREA SELECTION: {', '.join(domain_labels) or 'Open / Guided by Free Text & CV'}\n"
            f"WORK LOCATION SELECTION: {', '.join(loc_labels)}\n\n"
            f"CANDIDATE FREE-TEXT FEEDBACK (PRIMARY SIGNAL — WHAT THEY WANT & WHAT TO AVOID):\n"
            f"{free_text or '(No extra text provided — infer target roles directly from CV and selected level/area)'}\n\n"
            f"---\nSTRUCTURED CANDIDATE CV:\n{structured_cv_md[:6000]}"
        )
        try:
            parsed = fj._invoke_gemini_json(
                prompt,
                system_text=ONBOARD_PROFILE_SYSTEM_PROMPT,
                model=model,
                api_key=api_key,
                temperature=0.2,
                max_output_tokens=2048,
                timeout=35,
                retries=2,
                validator=lambda p: isinstance(p, dict) and bool(p.get("career_goal")),
            )
            if parsed:
                ai_goal = fj.sanitize_ats_text(parsed.get("career_goal") or "")
                if len(ai_goal) >= 40:
                    career_goal = ai_goal[:1400]
                _add_unique(title_include, parsed.get("extra_title_include") or [])
                _add_unique(upgrade_include, parsed.get("extra_upgrade_include") or [])
                _add_unique(title_exclude, parsed.get("extra_title_exclude") or [])
                rule_source = str(parsed.get("_llm_model") or model)
        except Exception as e:
            fj.gha_annotate("warning", f"Onboarding Gemini profile calibration fallback ({type(e).__name__}: {e})")

    sub_entry = {
        "name": validated["name"],
        "recipient_email": validated["email"],
        "enabled": True,
        "email_on_baseline": True,
        "email_mode": "new_or_discovered",
        "briefing_language": validated["language"],
        "max_tailored_cvs": 3,
        "min_match_score": 70,
        "career_goal": career_goal,
        "title_include": title_include,
        "upgrade_title_include": upgrade_include,
        "title_exclude": title_exclude,
        "include_remote_mexico": "remote_mx" in locations,
        "include_country_only": "mx_country" in locations,
    }
    if "jalisco" not in locations:
        sub_entry["locations"] = []
    return sub_entry, rule_source


# --------------------------------------------------------------------------
# Codebase Registration (cvs/<id>.md + config.json + docs/onboard.html)
# --------------------------------------------------------------------------

def register_subscriber_in_codebase(validated, root=ROOT, api_key=None, model=fj.DEFAULT_LLM_MODEL):
    """Create/update `cvs/<id>.md` and `config.json` for the onboarded subscriber.
    Returns a result dict suitable for confirmation emailing and GitHub Actions telemetry."""
    root = Path(root).resolve()
    cfg_path = root / "config.json"
    cfg = fj.load_json(cfg_path, None)
    if not isinstance(cfg, dict):
        cfg = copy.deepcopy(fj.DEFAULTS)

    subs = cfg.setdefault("subscribers", [])
    sub_id, updated_existing = derive_subscriber_id(validated["name"], validated["email"], subs)

    # Path-traversal boundary verification on cvs/<sub_id>.md
    cvs_dir = (root / "cvs").resolve()
    cvs_dir.mkdir(parents=True, exist_ok=True)
    cv_rel_path = f"cvs/{sub_id}.md"
    cv_abs_path = (root / cv_rel_path).resolve()
    cv_abs_path.relative_to(cvs_dir)

    eff_model = (os.environ.get("GEMINI_MODEL") or "").strip() or cfg.get("llm_model") or model
    cv_md, cv_model_used = structure_master_cv_markdown(
        validated["cv_text"], validated["name"], validated["email"], api_key=api_key, model=eff_model
    )
    sub_profile, rule_model_used = synthesize_subscriber_profile(
        validated, cv_md, api_key=api_key, model=eff_model
    )
    sub_record = {
        "id": sub_id,
        **sub_profile,
        "cv_file": cv_rel_path,
    }

    # Write CV file atomically
    tmp_cv = cv_abs_path.with_suffix(".md.tmp")
    tmp_cv.write_text(cv_md, encoding="utf-8")
    tmp_cv.replace(cv_abs_path)

    # Insert or update subscriber in config.json
    replaced = False
    for idx, existing in enumerate(subs):
        if isinstance(existing, dict) and existing.get("id") == sub_id:
            subs[idx] = sub_record
            replaced = True
            break
    if not replaced:
        subs.append(sub_record)

    fj.write_json(cfg_path, cfg)

    # Refresh docs/onboard.html if docs/ exists
    docs_dir = root / "docs"
    if docs_dir.exists():
        (docs_dir / "onboard.html").write_text(render_onboard_page(cfg), encoding="utf-8")

    employers_indexed = []
    for ln in cv_md.splitlines():
        if ln.startswith("### "):
            co = ln[4:].split("|", 1)[0].strip()
            if co and co not in employers_indexed:
                employers_indexed.append(co)

    return {
        "subscriber_id": sub_id,
        "updated_existing": updated_existing,
        "name": sub_record["name"],
        "email": sub_record["recipient_email"],
        "cv_file": cv_rel_path,
        "career_goal": sub_record["career_goal"],
        "levels": validated["levels"],
        "domains": validated["domains"],
        "locations": validated["locations"],
        "language": validated["language"],
        "free_text": validated["free_text"],
        "employers_indexed": employers_indexed,
        "title_include_count": len(sub_record["title_include"]),
        "title_exclude_count": len(sub_record["title_exclude"]),
        "cv_model": cv_model_used,
        "rule_model": rule_model_used,
        "onboarded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# --------------------------------------------------------------------------
# Professional Confirmation Email from Lucy
# --------------------------------------------------------------------------

def build_onboarding_confirmation_email(result, sender_email, board_count=109):
    """Build a polished executive welcome & confirmation email (HTML + plain text) from Lucy."""
    recipient = result["email"]
    name = result["name"]
    first_name = (name or "Partner").split()[0]
    sub_id = result["subscriber_id"]
    is_update = bool(result.get("updated_existing"))

    level_labels = ", ".join(SENIORITY_BY_ID[k]["label"] for k in result.get("levels") or [] if k in SENIORITY_BY_ID) or "Leadership"
    domain_labels = ", ".join(DOMAIN_BY_ID[k]["label"] for k in result.get("domains") or [] if k in DOMAIN_BY_ID) or "Cross-Functional Leadership"
    loc_labels = " + ".join(LOCATION_BY_ID[k]["label"] for k in result.get("locations") or [] if k in LOCATION_BY_ID) or "Jalisco + Remote Mexico"
    employers = result.get("employers_indexed") or []
    emp_txt = ", ".join(employers[:6]) + (f" (+{len(employers) - 6} more)" if len(employers) > 6 else "")
    if not emp_txt:
        emp_txt = "Master CV structured & ATS-sanitized"

    action_word = "Updated & Recalibrated" if is_update else "Active"
    msg = EmailMessage()
    msg["Subject"] = f"✦ Welcome to Jobs Sentinel, {first_name} — Your Private AI Talent Radar Is {action_word}"
    msg["From"] = f"Lucy · Personal Headhunter <{sender_email}>"
    msg["To"] = recipient
    msg["Reply-To"] = sender_email

    plain_lines = [
        f"Welcome aboard, {first_name}.",
        "",
        f"I'm Lucy, your private AI headhunter on Jobs Sentinel. Your profile ({sub_id}) and master CV have been onboarded into our codebase, and your daily radar is now {action_word.lower()}.",
        "",
        "YOUR CALIBRATED RADAR BLUEPRINT",
        f"- Subscriber ID: {sub_id}",
        f"- Target Seniority: {level_labels}",
        f"- Functional Domains: {domain_labels}",
        f"- Geographic Scope: {loc_labels}",
        f"- Briefing Language: {result.get('language', 'English')}",
        f"- CV Employers Indexed & Enriched: {emp_txt}",
        "",
        "YOUR CAREER NORTH STAR (AI MATCH GATE)",
        result.get("career_goal", ""),
        "",
        "WHAT HAPPENS NEXT",
        f"1. Daily Market Sweep: Every morning I scan {board_count} verified employer career portals across Workday, Greenhouse, SmartRecruiters, Oracle HCM, Eightfold, Lever, Ashby, BambooHR, and Workable.",
        "2. AI Fit Validation: Each candidate opening is reviewed by Gemini against your Career North Star, your real CV experience, and the company's industry profile. Roles that aren't a strong fit are filtered out automatically.",
        "3. Tailored CVs & Rationale: When genuine matches land, you receive a concise morning briefing explaining why each role fits your trajectory—with ATS-ready Microsoft Word (.docx) CVs pre-built for your top picks.",
        "",
        "Shoulder to shoulder,",
        "Lucy · Personal Headhunter (Jobs Sentinel)",
    ]
    msg.set_content("\n".join(plain_lines))

    first_esc = html.escape(first_name)
    sub_id_esc = html.escape(sub_id)
    levels_esc = html.escape(level_labels)
    domains_esc = html.escape(domain_labels)
    locs_esc = html.escape(loc_labels)
    lang_esc = html.escape(result.get("language") or "English")
    emp_esc = html.escape(emp_txt)
    goal_esc = html.escape(result.get("career_goal") or "")
    status_badge = html.escape("PROFILE RECALIBRATED" if is_update else "RADAR LIVE & ARMED")

    html_body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Welcome to Jobs Sentinel</title>
</head>
<body style="margin:0;padding:28px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#18181b;background-color:#f4f4f5;-webkit-font-smoothing:antialiased;">
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">Your private AI job radar ({sub_id_esc}) is active across {board_count} employer boards — only verified matches + ATS-ready CVs.</div>

  <div style="max-width:640px;margin:0 auto;background-color:#ffffff;border:1px solid #e4e4e7;border-radius:14px;padding:30px 28px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin-bottom:12px;">
      <tr>
        <td align="left" style="font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:1.3px;color:#059669;">
          &#10022; Lucy &middot; Personal Headhunter
        </td>
        <td align="right">
          <span style="display:inline-block;background-color:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;font-size:10px;font-weight:800;padding:3px 9px;border-radius:999px;letter-spacing:0.6px;">&#9679; {status_badge}</span>
        </td>
      </tr>
    </table>

    <h1 style="margin:0 0 6px;font-size:24px;font-weight:800;letter-spacing:-0.5px;color:#09090b;line-height:1.25;">
      Welcome to your unfair advantage, {first_esc}.
    </h1>
    <div style="font-size:13.5px;color:#52525b;margin-bottom:18px;line-height:1.55;">
      I&rsquo;m <strong style="color:#18181b;">Lucy</strong>, your private AI headhunter. Your onboarding profile (<code style="background-color:#f4f4f5;padding:1px 6px;border-radius:4px;font-size:12px;color:#09090b;">{sub_id_esc}</code>) and structured master CV are now committed to the Jobs Sentinel codebase.
    </div>

    <!-- Dark Obsidian Partner Welcome Card -->
    <div style="background-color:#09090b;color:#fafafa;border-radius:11px;padding:18px 20px;margin-bottom:20px;">
      <div style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.4px;color:#34d399;margin-bottom:6px;">
        &#9889; Us vs. Their Machine &middot; Onboarding Confirmed
      </div>
      <div style="font-size:16px;font-weight:800;color:#ffffff;margin-bottom:7px;line-height:1.3;">
        Zero Job-Board Noise. Only Verified Matches Built Around Your Track Record.
      </div>
      <div style="font-size:13px;color:#d4d4d8;line-height:1.58;">
        Every morning I sweep <strong style="color:#ffffff;">{board_count} verified employer career portals</strong> across 9 ATS platforms, evaluate candidate openings with Gemini against your Career North Star and CV, discard weak fits automatically, and attach <strong style="color:#ffffff;">ATS-ready Word (.docx) CVs</strong> for your strongest opportunities.
      </div>
    </div>

    <!-- Calibrated Blueprint Card -->
    <div style="font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:1.1px;color:#09090b;border-bottom:2px solid #09090b;padding-bottom:6px;margin-bottom:12px;">
      &#127919; Your Calibrated Radar Blueprint
    </div>

    <div style="background-color:#fafafa;border:1px solid #e4e4e7;border-left:3px solid #059669;border-radius:8px;padding:12px 15px;margin-bottom:14px;">
      <div style="font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:0.6px;color:#059669;margin-bottom:4px;">Career North Star (AI Match Gate Prompt)</div>
      <div style="font-size:12.8px;color:#18181b;line-height:1.55;">{goal_esc}</div>
    </div>

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-size:12.8px;margin-bottom:20px;">
      <tr>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#71717a;width:36%;font-weight:600;">Target Seniority</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#18181b;font-weight:600;">{levels_esc}</td>
      </tr>
      <tr>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#71717a;font-weight:600;">Functional Domains</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#18181b;">{domains_esc}</td>
      </tr>
      <tr>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#71717a;font-weight:600;">Geographic Scope</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#18181b;">{locs_esc}</td>
      </tr>
      <tr>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#71717a;font-weight:600;">Briefing Language</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e4e4e7;color:#18181b;">{lang_esc}</td>
      </tr>
      <tr>
        <td style="padding:8px 10px;color:#71717a;font-weight:600;">CV Employers Enriched</td>
        <td style="padding:8px 10px;color:#0f766e;font-weight:600;">{emp_esc}</td>
      </tr>
    </table>

    <!-- What Happens Next -->
    <div style="padding:14px 16px;background-color:#fafafa;border:1px solid #e4e4e7;border-radius:9px;font-size:12.5px;color:#27272a;line-height:1.55;">
      <strong style="color:#09090b;">What happens next:</strong> Starting with the next scheduled sweep, you&rsquo;ll receive your daily <em>Lucy Private Talent Briefing</em> whenever genuine, never-before-sent matches pass our AI review—complete with company industry profiles, match rationales, and tailored CVs. You can update your CV or preferences anytime using the onboarding form with this same email address.
    </div>

    <div style="margin-top:16px;padding-top:12px;border-top:1px solid #e4e4e7;font-size:11.5px;color:#71717a;text-align:center;">
      Shoulder to shoulder, always. &mdash; <strong style="color:#18181b;">Lucy</strong> &middot; Jobs Sentinel
    </div>
  </div>
</body>
</html>
"""
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_onboarding_confirmation_email(result, board_count=109):
    """Deliver the onboarding confirmation email via Gmail/SMTP with transient retry."""
    smtp_user = (os.environ.get("GMAIL_USER") or os.environ.get("SMTP_USER") or "").strip()
    smtp_password = (os.environ.get("GMAIL_APP_PASSWORD") or os.environ.get("SMTP_PASSWORD") or "").strip()
    smtp_host = (os.environ.get("SMTP_HOST") or "smtp.gmail.com").strip()
    smtp_port = int((os.environ.get("SMTP_PORT") or "465").strip())

    if not smtp_user or not smtp_password:
        print(f"  [Onboarding:{result['subscriber_id']}] Confirmation email skipped: GMAIL_USER / GMAIL_APP_PASSWORD not set.")
        return False

    msg = build_onboarding_confirmation_email(result, smtp_user, board_count=board_count)
    in_test = "unittest" in sys.modules
    last_err = None
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
            print(f"  [Onboarding:{result['subscriber_id']}] Sent confirmation email -> {result['email']}")
            return True
        except smtplib.SMTPAuthenticationError:
            raise
        except Exception as e:
            last_err = e
            if attempt < 2 and not in_test:
                time.sleep(2.5 * (attempt + 1))
    if last_err is not None:
        raise last_err
    return False


# --------------------------------------------------------------------------
# Self-Contained Onboarding Webpage (`docs/onboard.html`)
# --------------------------------------------------------------------------

# TODO(security): Enforce strict CSP & X-Frame-Options headers on hosting proxy; client JS uses strict textContent/createElement DOM rendering only.
ONBOARD_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Join Jobs Sentinel · Lucy Private AI Talent Radar</title>
<style>
:root{--bg:#fafaf9;--card:#ffffff;--ink:#09090b;--body:#27272a;--mute:#71717a;--line:#e4e4e7;--accent:#059669;--accent-ink:#065f46;--accent-bg:#ecfdf5;--obsidian:#09090b;--danger:#dc2626;--danger-bg:#fef2f2}
@media (prefers-color-scheme:dark){:root{--bg:#0c0c0d;--card:#18181b;--ink:#fafafa;--body:#e4e4e7;--mute:#a1a1aa;--line:#27272a;--accent:#10b981;--accent-ink:#6ee7b7;--accent-bg:#064e3b;--obsidian:#18181b;--danger:#f87171;--danger-bg:#450a0a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--body);font:15.5px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:760px;margin:0 auto;padding:26px 16px 64px}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;font-size:.82rem}
.brand{font-weight:800;text-transform:uppercase;letter-spacing:1.2px;color:var(--accent)}
.back{color:var(--mute);text-decoration:none;font-weight:600}
.back:hover{color:var(--accent)}
.hero{background:#09090b;color:#fafafa;border-radius:16px;padding:24px 24px 22px;margin-bottom:20px;border:1px solid #27272a}
.hero .eyebrow{font-size:.72rem;font-weight:800;text-transform:uppercase;letter-spacing:1.4px;color:#34d399;margin-bottom:6px}
.hero h1{margin:0 0 8px;font-size:1.6rem;font-weight:800;letter-spacing:-.4px;color:#fff;line-height:1.22}
.hero p{margin:0;color:#d4d4d8;font-size:.92rem;line-height:1.55}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin-bottom:14px}
.card.highlight{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent-bg)}
.sec-title{font-size:.78rem;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:var(--ink);margin:0 0 3px}
.sec-sub{font-size:.85rem;color:var(--mute);margin:0 0 13px;line-height:1.45}
.grid2{display:grid;gap:12px;grid-template-columns:1fr}
@media (min-width:600px){.grid2{grid-template-columns:1fr 1fr}}
label.field{display:block;font-size:.85rem;font-weight:700;color:var(--ink);margin-bottom:10px}
label.field span.req{color:var(--accent)}
input[type=text],input[type=email],input[type=password],select,textarea{width:100%;margin-top:5px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit;transition:border-color .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
.dropzone{border:1.5px dashed var(--line);border-radius:12px;padding:16px;text-align:center;background:var(--bg);cursor:pointer;transition:border-color .15s,background .15s;margin-bottom:10px}
.dropzone:hover,.dropzone.drag{border-color:var(--accent);background:var(--accent-bg)}
.dropzone strong{color:var(--ink);display:block;font-size:.93rem;margin-bottom:2px}
.dropzone small{color:var(--mute);font-size:.8rem}
.cv-meta{display:flex;justify-content:space-between;align-items:center;font-size:.79rem;color:var(--mute);margin:4px 0}
.checks{display:grid;gap:8px;grid-template-columns:1fr}
@media (min-width:580px){.checks.two{grid-template-columns:1fr 1fr}.checks.three{grid-template-columns:1fr 1fr 1fr}}
.chk{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);cursor:pointer;transition:border-color .15s,background .15s}
.chk:hover{border-color:var(--accent)}
.chk.active{border-color:var(--accent);background:var(--accent-bg)}
.chk input{margin-top:2px;accent-color:var(--accent);width:16px;height:16px;flex-shrink:0}
.chk .clabel{font-weight:700;font-size:.87rem;color:var(--ink);display:block;line-height:1.28}
.chk .chint{font-size:.76rem;color:var(--mute);display:block;margin-top:2px;line-height:1.32}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:9px}
.chip{background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:999px;padding:4px 10px;font:600 .76rem/1.3 inherit;cursor:pointer;transition:border-color .15s,color .15s}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.submit-bar{display:flex;flex-direction:column;gap:10px;margin-top:16px}
button.primary{background:var(--accent);color:#fff;border:none;border-radius:11px;padding:14px 22px;font:700 1rem/1.2 inherit;cursor:pointer;transition:opacity .15s}
button.primary:hover{opacity:.92}
button.primary:disabled{opacity:.55;cursor:not-allowed}
button.secondary{background:transparent;color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:10px 16px;font:600 .88rem/1.2 inherit;cursor:pointer}
.banner{border-radius:12px;padding:16px 18px;margin-top:14px;font-size:.9rem;line-height:1.5;display:none}
.banner.ok{display:block;background:var(--accent-bg);color:var(--accent-ink);border:1px solid var(--accent)}
.banner.err{display:block;background:var(--danger-bg);color:var(--danger);border:1px solid var(--danger)}
.foot{text-align:center;font-size:.78rem;color:var(--mute);margin-top:22px}
</style>
</head>
<body>
<main>
  <div class="topbar">
    <span class="brand">&#10022; Lucy &middot; Jobs Sentinel</span>
    <a class="back" href="index.html">&larr; View Live Job Board</a>
  </div>

  <section class="hero">
    <div class="eyebrow">&#9889; AI Job Radar Onboarding</div>
    <h1>Set Up Your Personal AI Job Radar</h1>
    <p>Share your CV, pick a few quick filters, and tell the AI in plain words what kind of job you want next. Every morning Lucy scans <strong>109 verified companies</strong>, filters out weak matches with Gemini AI, explains why each match fits you, and attaches <strong>tailored Word (.docx) CVs</strong>.</p>
  </section>

  <form id="onboardForm" novalidate>
    <section class="card">
      <h2 class="sec-title">1. Your Details</h2>
      <p class="sec-sub">Where Lucy will send your confirmation email and daily job matches.</p>
      <div class="grid2">
        <label class="field">Full Name <span class="req">*</span>
          <input id="fullName" type="text" required maxlength="80" placeholder="e.g. Sofia Ramirez" autocomplete="name">
        </label>
        <label class="field">Email Address <span class="req">*</span>
          <input id="email" type="email" required maxlength="180" placeholder="sofia@example.com" autocomplete="email">
        </label>
      </div>
      <div class="grid2">
        <label class="field">Daily Briefing Language
          <select id="language">
            <option value="English" selected>English</option>
            <option value="Spanish">Spanish (Espa&ntilde;ol)</option>
            <option value="Portuguese">Portuguese (Portugu&ecirc;s)</option>
          </select>
        </label>
        <label class="field" id="inviteWrap">Team Invite Code <span id="inviteHint" style="font-weight:400;color:var(--mute);">(optional)</span>
          <input id="inviteCode" type="password" maxlength="80" placeholder="Enter invite code if provided" autocomplete="off">
        </label>
      </div>
    </section>

    <section class="card">
      <h2 class="sec-title">2. Your CV in Text Format <span style="color:var(--accent);">*</span></h2>
      <p class="sec-sub">Upload a <code>.txt</code> or <code>.md</code> file, or simply copy and paste the text from your resume below.</p>
      <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Upload plain-text CV file">
        <strong id="dropLabel">&#128196; Upload .txt / .md CV file (or drag &amp; drop here)</strong>
        <small>Plain text &middot; up to 45,000 characters</small>
        <input id="cvFile" type="file" accept=".txt,.md,.markdown,text/plain,text/markdown" style="display:none">
      </div>
      <div class="cv-meta">
        <span id="fileStatus">Or paste your resume text directly:</span>
        <span id="charCount">0 / 45,000 chars</span>
      </div>
      <textarea id="cvText" rows="8" maxlength="45000" required placeholder="Paste your resume text here (experience, companies, skills, education, languages)..."></textarea>
    </section>

    <section class="card">
      <h2 class="sec-title">3. Role Level <span style="color:var(--accent);">*</span></h2>
      <p class="sec-sub">Pick one or more seniority levels you are open to.</p>
      <div class="checks two" id="levelChecks"></div>
    </section>

    <section class="card">
      <h2 class="sec-title">4. Broad Job Area <span style="font-weight:400;color:var(--mute);text-transform:none;letter-spacing:0;">(pick all that fit, or leave open)</span></h2>
      <p class="sec-sub">High-level category to help orient the AI — you&rsquo;ll specify your exact role wishes in Step 6.</p>
      <div class="checks two" id="domainChecks"></div>
    </section>

    <section class="card">
      <h2 class="sec-title">5. Work Location <span style="color:var(--accent);">*</span></h2>
      <p class="sec-sub">Where are you open to working?</p>
      <div class="checks three" id="locationChecks"></div>
    </section>

    <section class="card highlight">
      <h2 class="sec-title">6. Tell the AI What You Are Looking For <span style="color:var(--accent);">*</span></h2>
      <p class="sec-sub">Write freely in your own words — Gemini uses this directly alongside your CV to decide which jobs truly match you and which ones to skip.</p>
      <div class="chips" id="promptChips">
        <button type="button" class="chip" data-snippet="Roles I want next: ">+ Roles I want</button>
        <button type="button" class="chip" data-snippet="Industries / company types I like: ">+ Industries I like</button>
        <button type="button" class="chip" data-snippet="Roles or areas to avoid: ">+ Roles to avoid</button>
      </div>
      <textarea id="freeText" rows="5" maxlength="4000" placeholder="Example: I want to move into a Senior or Manager role in Project Management, Operations, or Data Analytics at a tech or international company. I want roles where I lead cross-functional initiatives. Please skip sales, accounting, or night-shift support roles."></textarea>
    </section>

    <div class="submit-bar">
      <button type="submit" class="primary" id="submitBtn">&#10022; Activate My Job Radar</button>
      <button type="button" class="secondary" id="downloadFallbackBtn" style="display:none;">Download Onboarding Packet (.json) for CLI Import</button>
    </div>

    <div id="statusBanner" class="banner" role="status" aria-live="polite"></div>
  </form>

  <p class="foot">Powered by Jobs Sentinel &middot; Gemini 4 Hydrogen Match Gate &middot; Only genuine matches delivered</p>
</main>
<script>
const CONFIG = __ONBOARD_CONFIG__;
const $ = id => document.getElementById(id);

function syncCardState(lbl, inp) {
  if (inp.checked) lbl.classList.add("active");
  else lbl.classList.remove("active");
}

function buildCheckboxes(containerId, items, defaultIds) {
  const wrap = $(containerId);
  items.forEach(item => {
    const lbl = document.createElement("label");
    lbl.className = "chk";
    const inp = document.createElement("input");
    inp.type = "checkbox";
    inp.value = item.id;
    inp.checked = Boolean(item.default || (defaultIds && defaultIds.includes(item.id)));
    syncCardState(lbl, inp);
    inp.addEventListener("change", () => syncCardState(lbl, inp));
    const textWrap = document.createElement("div");
    const title = document.createElement("span");
    title.className = "clabel";
    title.textContent = item.label;
    const hint = document.createElement("span");
    hint.className = "chint";
    hint.textContent = item.hint;
    textWrap.append(title, hint);
    lbl.append(inp, textWrap);
    wrap.appendChild(lbl);
  });
}

buildCheckboxes("levelChecks", CONFIG.levels, ["senior", "manager"]);
buildCheckboxes("domainChecks", CONFIG.domains, []);
buildCheckboxes("locationChecks", CONFIG.locations, ["jalisco", "remote_mx", "mx_country"]);

if (CONFIG.require_invite_code) {
  $("inviteHint").textContent = "* required";
  $("inviteHint").style.color = "var(--accent)";
  $("inviteCode").required = true;
}

document.querySelectorAll("#promptChips .chip").forEach(btn => {
  btn.addEventListener("click", () => {
    const box = $("freeText");
    const snippet = btn.getAttribute("data-snippet") || "";
    const prefix = box.value && !box.value.endsWith("\n") ? "\n" : "";
    box.value = box.value + prefix + snippet;
    box.focus();
  });
});

const cvText = $("cvText");
const cvFile = $("cvFile");
const dropzone = $("dropzone");

function updateCounter() {
  $("charCount").textContent = cvText.value.length.toLocaleString() + " / 45,000 chars";
}
cvText.addEventListener("input", updateCounter);

function loadTextFile(file) {
  if (!file) return;
  if (file.size > 512000) {
    showStatus("err", "File is too large. Please upload a plain-text (.txt or .md) resume under 500 KB.");
    return;
  }
  const nameLow = (file.name || "").toLowerCase();
  if (nameLow.endsWith(".pdf") || nameLow.endsWith(".docx") || nameLow.endsWith(".doc")) {
    showStatus("err", "Please upload your CV in plain-text (.txt or .md) format, or copy and paste the text from your PDF/Word resume into the box below.");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const raw = String(reader.result || "").slice(0, 45000);
    cvText.value = raw;
    $("fileStatus").textContent = "Loaded file: " + file.name;
    updateCounter();
    hideStatus();
  };
  reader.onerror = () => showStatus("err", "Could not read file. Please paste your CV text directly.");
  reader.readAsText(file, "utf-8");
}

dropzone.addEventListener("click", () => cvFile.click());
dropzone.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); cvFile.click(); } });
cvFile.addEventListener("change", () => { if (cvFile.files && cvFile.files[0]) loadTextFile(cvFile.files[0]); });
["dragenter", "dragover"].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add("drag"); }));
["dragleave", "drop"].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove("drag"); }));
dropzone.addEventListener("drop", e => {
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) loadTextFile(e.dataTransfer.files[0]);
});

function checkedValues(containerId) {
  return [...$(containerId).querySelectorAll("input[type=checkbox]:checked")].map(el => el.value);
}

function showStatus(kind, message) {
  const b = $("statusBanner");
  b.className = "banner " + kind;
  b.textContent = message;
}
function hideStatus() {
  const b = $("statusBanner");
  b.className = "banner";
  b.textContent = "";
}

function collectPayload() {
  const name = $("fullName").value.trim();
  const email = $("email").value.trim().toLowerCase();
  const cv = cvText.value.trim();
  const levels = checkedValues("levelChecks");
  const domains = checkedValues("domainChecks");
  const locations = checkedValues("locationChecks");
  const freeTxt = $("freeText").value.trim().slice(0, 4000);
  if (!name) throw new Error("Please enter your full name.");
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) throw new Error("Please enter a valid email address.");
  if (cv.length < 60) throw new Error("Please upload a .txt/.md CV file or paste your resume text (at least 60 characters).");
  if (!levels.length) throw new Error("Please select at least one Role Level.");
  if (!locations.length) throw new Error("Please select at least one Work Location.");
  if (!domains.length && freeTxt.length < 10) {
    throw new Error("Please either select a Broad Job Area or describe what you are looking for in Step 6.");
  }
  if (CONFIG.require_invite_code && !$("inviteCode").value.trim()) {
    throw new Error("Please enter your Team Invite Code.");
  }
  return {
    name: name,
    email: email,
    invite_code: $("inviteCode").value.trim(),
    cv_text: cv.slice(0, 45000),
    levels: levels,
    domains: domains.length ? domains : ["other"],
    locations: locations,
    language: $("language").value,
    free_text: freeTxt
  };
}

let lastPacket = null;
$("downloadFallbackBtn").addEventListener("click", () => {
  try {
    const pkt = lastPacket || collectPayload();
    const copyPkt = Object.assign({}, pkt);
    delete copyPkt.invite_code;
    const blob = new Blob([JSON.stringify(copyPkt, null, 2)], {type: "application/json"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "onboard_" + pkt.email.split("@")[0].replace(/[^a-z0-9]+/gi, "_") + ".json";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (err) {
    showStatus("err", err.message || String(err));
  }
});

$("onboardForm").addEventListener("submit", async e => {
  e.preventDefault();
  hideStatus();
  let payload;
  try {
    payload = collectPayload();
    lastPacket = payload;
  } catch (err) {
    showStatus("err", err.message || String(err));
    return;
  }

  const isLocalServer = Boolean(CONFIG.local_server);
  const endpoint = isLocalServer ? "/api/onboard" : (CONFIG.webhook_url || "").trim();

  if (!endpoint) {
    $("downloadFallbackBtn").style.display = "inline-block";
    showStatus(
      "err",
      "Cloud webhook URL is not configured in config.json yet (see onboarding/README.md for 2-minute Google Apps Script setup, or run `python3 onboard_subscriber.py --serve` locally). Click 'Download Onboarding Packet (.json)' above to import right away via `python3 onboard_subscriber.py --from-file <packet.json>`."
    );
    return;
  }

  const btn = $("submitBtn");
  btn.disabled = true;
  btn.textContent = "Calibrating your radar with Lucy...";

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: {"Content-Type": isLocalServer ? "application/json;charset=utf-8" : "text/plain;charset=utf-8"},
      body: JSON.stringify(payload)
    });
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || data.message || ("Submission failed with HTTP " + res.status));
    }
    showStatus(
      "ok",
      data.message || ("Welcome aboard, " + payload.name + "! Your profile is being added to the Jobs Sentinel codebase right now, and Lucy is sending a confirmation email to " + payload.email + ".")
    );
    btn.textContent = "Radar Activated — Check Your Inbox!";
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "\u2726 Activate My Job Radar";
    $("downloadFallbackBtn").style.display = "inline-block";
    showStatus("err", (err.message || String(err)) + " (You can also download your onboarding packet above if offline.)");
  }
});
</script>
</body>
</html>
"""


def render_onboard_page(cfg=None, local_server=False):
    """Render `docs/onboard.html` with current checkbox options and configured webhook URL."""
    cfg = cfg or {}
    webhook_url = (
        (os.environ.get("ONBOARD_WEBHOOK_URL") or "").strip()
        or str(cfg.get("onboard_webhook_url") or "").strip()
    )
    require_invite = bool(cfg.get("onboard_require_invite_code", False))
    ui_cfg = {
        "webhook_url": webhook_url,
        "require_invite_code": require_invite,
        "local_server": bool(local_server),
        "levels": [{"id": o["id"], "label": o["label"], "hint": o["hint"], "default": bool(o.get("default"))} for o in SENIORITY_OPTIONS],
        "domains": [{"id": o["id"], "label": o["label"], "hint": o["hint"], "default": bool(o.get("default"))} for o in DOMAIN_OPTIONS],
        "locations": [{"id": o["id"], "label": o["label"], "hint": o["hint"], "default": bool(o.get("default"))} for o in LOCATION_OPTIONS],
    }
    safe_json = json.dumps(ui_cfg, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    return ONBOARD_PAGE_TEMPLATE.replace("__ONBOARD_CONFIG__", safe_json)


# --------------------------------------------------------------------------
# GitHub Event Parser & Local Self-Service HTTP Server (`--serve`)
# --------------------------------------------------------------------------

def extract_payload_from_github_event(event_path):
    """Extract onboarding payload from GitHub Actions `$GITHUB_EVENT_PATH`
    (supports both `repository_dispatch.client_payload` and `workflow_dispatch.inputs`)."""
    raw = json.loads(Path(event_path).read_text(encoding="utf-8"))
    if isinstance(raw.get("client_payload"), dict) and raw["client_payload"]:
        return raw["client_payload"]
    if isinstance(raw.get("inputs"), dict) and raw["inputs"]:
        inp = raw["inputs"]
        return {
            "name": inp.get("name", ""),
            "email": inp.get("email", ""),
            "cv_text": inp.get("cv_text", ""),
            "levels": inp.get("levels", "senior,manager,director"),
            "domains": inp.get("domains", "other"),
            "locations": inp.get("locations", "jalisco,remote_mx,mx_country"),
            "language": inp.get("language", "English"),
            "free_text": inp.get("free_text", ""),
        }
    return raw


def run_local_onboarding_server(host="127.0.0.1", port=8080, root=ROOT, send_email=True):
    """Run a zero-dependency local onboarding web server at http://127.0.0.1:<port>/onboard.html."""
    root = Path(root).resolve()

    class OnboardHandler(BaseHTTPRequestHandler):
        def _send(self, status, body_bytes, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body_bytes)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/onboard", "/onboard.html"):
                cfg = fj.load_config() if (root / "config.json").exists() else fj.DEFAULTS
                html_bytes = render_onboard_page(cfg, local_server=True).encode("utf-8")
                self._send(200, html_bytes, "text/html; charset=utf-8")
                return
            if path == "/index.html" and (root / "docs" / "index.html").exists():
                self._send(200, (root / "docs" / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            self._send(404, b'{"ok":false,"error":"Not found"}')

        def do_POST(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/onboard":
                self._send(404, b'{"ok":false,"error":"Not found"}')
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0 or length > 520_000:
                    self._send(413, b'{"ok":false,"error":"Payload too large (max 500 KB)."}')
                    return
                raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
                payload = json.loads(raw_body)
                expected_code = (os.environ.get("ONBOARD_INVITE_CODE") or "").strip()
                if expected_code and str(payload.get("invite_code") or "").strip() != expected_code:
                    self._send(403, b'{"ok":false,"error":"Invalid Team Invite Code."}')
                    return
                validated = validate_onboarding_payload(payload)
                companies = fj.load_companies() if (root / "companies.json").exists() else []
                res = register_subscriber_in_codebase(validated, root=root)
                emailed = False
                if send_email:
                    emailed = send_onboarding_confirmation_email(res, board_count=len(companies) or 109)
                msg_txt = (
                    f"Welcome aboard, {res['name']}! Profile '{res['subscriber_id']}' is now saved in config.json and {res['cv_file']}"
                    + (f", and Lucy sent a confirmation email to {res['email']}." if emailed else " (set GMAIL_USER & GMAIL_APP_PASSWORD to send confirmation emails).")
                )
                out = json.dumps({"ok": True, "subscriber_id": res["subscriber_id"], "emailed": emailed, "message": msg_txt}).encode("utf-8")
                self._send(200, out)
            except ValueError as ve:
                err = json.dumps({"ok": False, "error": str(ve)}).encode("utf-8")
                self._send(400, err)
            except Exception as exc:
                err = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}).encode("utf-8")
                self._send(500, err)

    server = ThreadingHTTPServer((host, int(port)), OnboardHandler)
    print(f"Lucy Onboarding Portal live at: http://{host}:{port}/onboard.html")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped onboarding server.")
    finally:
        server.server_close()
    return 0


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-event", help="path to GitHub Actions event JSON ($GITHUB_EVENT_PATH)")
    ap.add_argument("--from-file", help="path to an onboarding payload JSON file")
    ap.add_argument("--send-confirmation-from", help="send confirmation email from a saved onboard_result.json")
    ap.add_argument("--result-out", help="write onboarding result metadata JSON to this path")
    ap.add_argument("--render-page", help="write standalone docs/onboard.html to this path and exit")
    ap.add_argument("--no-email", action="store_true", help="register in codebase without sending confirmation email yet")
    ap.add_argument("--serve", action="store_true", help="start local interactive onboarding web server")
    ap.add_argument("--host", default="127.0.0.1", help="bind host for --serve (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8080, help="bind port for --serve (default: 8080)")
    args = ap.parse_args(argv)

    if args.render_page:
        cfg = fj.load_config()
        out_p = Path(args.render_page)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(render_onboard_page(cfg), encoding="utf-8")
        print(f"Wrote onboarding webpage -> {out_p}")
        return 0

    if args.serve:
        return run_local_onboarding_server(host=args.host, port=args.port, root=ROOT, send_email=not args.no_email)

    companies = fj.load_companies()
    board_count = len(companies) or 109

    if args.send_confirmation_from:
        res = json.loads(Path(args.send_confirmation_from).read_text(encoding="utf-8"))
        send_onboarding_confirmation_email(res, board_count=board_count)
        return 0

    if args.from_event:
        raw_payload = extract_payload_from_github_event(args.from_event)
    elif args.from_file:
        raw_payload = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    else:
        ap.error("Provide --from-event, --from-file, --send-confirmation-from, --render-page, or --serve.")
        return 2

    validated = validate_onboarding_payload(raw_payload)
    result = register_subscriber_in_codebase(validated, root=ROOT)

    print(
        f"Onboarded subscriber '{result['subscriber_id']}' ({result['name']} <{result['email']}>) -> "
        f"{result['cv_file']} + config.json ({result['title_include_count']} include rules, "
        f"CV={result['cv_model']}, rules={result['rule_model']})."
    )
    fj.gha_summary(
        f"### Candidate Onboarded · `{result['subscriber_id']}`\n"
        f"- **Candidate**: {result['name']} (`{result['email']}`)\n"
        f"- **Master CV**: `{result['cv_file']}` (structured via `{result['cv_model']}`)\n"
        f"- **Career North Star**: {result['career_goal']}\n"
        f"- **Employers Enriched**: {', '.join(result['employers_indexed']) or 'N/A'}"
    )

    if args.result_out:
        out_file = Path(args.result_out)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if not args.no_email:
        send_onboarding_confirmation_email(result, board_count=board_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
