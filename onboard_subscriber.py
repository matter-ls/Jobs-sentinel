#!/usr/bin/env python3
"""Candidate Self-Service Onboarding Engine for Jobs Sentinel (Lucy).

Processes web-form submissions (from `docs/onboard.html` via GitHub `repository_dispatch`,
`workflow_dispatch`, CLI JSON file, or the built-in `--serve` local web server):
  1. Validates candidate email, name, uploaded plain-text CV, checkbox selections, and free-text goals.
  2. Structures & enriches the uploaded CV into `cvs/<subscriber_id>.md` using Free-Tier Gemini
     (`gemini-4-hydrogen` with automatic cascade & deterministic fallback), adding ATS-safe
     `*Company Profile: ...*` industry/scale context under every past employer.
  3. Synthesizes a calibrated `career_goal` and title rules and stores them as
     `subscribers/<subscriber_id>.json` (one file per person; `config.json` is never edited), so
     the new subscriber automatically starts receiving AI-validated daily matches.
  4. Sends a welcome and confirmation email from Lucy, in the same design as the daily briefing,
     confirming their active radar blueprint.

Standard library only (Python 3.9+, zero third-party packages).
"""
import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
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

# Each job area carries two title vocabularies (matched against the accent-folded, lower-case
# title, so they are written without accents):
#   keywords_re  words that place a title in this area ("supply chain", "ventas"). They are only
#                ever used together with a level word: "one of my levels AND one of my areas".
#   ic_titles    individual-contributor titles in this area that carry no level word, or that
#                say "manager" without managing people ("account executive", "project manager").
#                They are accepted on their own when Specialist or Senior / Lead is ticked.
DOMAIN_OPTIONS = [
    {
        "id": "operations",
        "label": "Operations & Supply Chain",
        "hint": "Operations, Logistics, Manufacturing, Procurement, Quality",
        "keywords_re": r"(operations|operaciones|supply chain|cadena de suministro|logistics|logistica|manufacturing|manufactura|production|produccion|plant|planta|quality|calidad|procurement|compras|sourcing|abastecimiento|planning|planeacion|fulfillment|warehouse|almacen|inventory|inventarios?|distribution|distribucion|materials|materiales|transportation|transporte)",
        "ic_titles": r"(buyer|comprador(a)?|planner|planeador(a)?|scheduler)",
    },
    {
        "id": "technology",
        "label": "Engineering, Data & IT",
        "hint": "Software, Data & Analytics, Cloud, IT, AI & Automation",
        "keywords_re": r"(engineering|ingenieria|software|backend|frontend|full[- ]?stack|platform|cloud|data|datos|analytics|business intelligence|\bbi\b|devops|sre|infrastructure|infraestructura|architecture|arquitectura|cybersecurity|ciberseguridad|security|seguridad|\bai\b|\bia\b|machine learning|automation|automatizacion|rpa|low[- ]code|\bit\b|\bti\b|sistemas|systems|technology|tecnologia|\bqa\b|testing|erp|sap)",
        "ic_titles": r"(data scientist|cientific[oa] de datos|programmer|programador(a)?|database administrator|\bdba\b|sysadmin|administrador(a)? de (sistemas|redes|bases de datos)|tester)",
    },
    {
        "id": "project_product",
        "label": "Project, Program & Product",
        "hint": "Project/Program Management, Product, PMO, Agile, Transformation",
        "keywords_re": r"(projects?|proyectos?|programs?|programas?|portfolio|portafolio|\bpmo\b|product|producto|agile|scrum|delivery|transformation|transformacion|implementation|implementacion)",
        "ic_titles": r"(project manager|program manager|product manager|technical program manager|product owner|scrum master|agile coach|gerente de proyectos?|lider de proyectos?|administrador(a)? de proyectos?)",
    },
    {
        "id": "sales_customer",
        "label": "Sales & Customer Success",
        "hint": "Sales, Account Management, Business Development, Customer Success / CX",
        "keywords_re": r"(sales|ventas|comercial|commercial|account executive|account manager|\bkam\b|key accounts?|cuentas clave|business development|desarrollo de negocios|\bbdr\b|\bsdr\b|customer success|client success|\bcsm\b|customer experience|\bcx\b|renewals?|retention|revenue|channel|partner|alliances?|alianzas|pre[- ]?sales|preventa)",
        "ic_titles": r"(account executive|sales executive|commercial executive|key account manager|\bkam\b|account manager|client partner|customer success manager|\bcsm\b|ejecutiv[oa] (de )?(ventas|cuentas|comercial)|business development representative|sales development representative|\bbdr\b|\bsdr\b|inside sales|sales engineer|solutions? consultant)",
    },
    {
        "id": "strategy_process",
        "label": "Strategy, Consulting & Process",
        "hint": "Business Strategy, Process Improvement, Shared Services / GBS, Change",
        "keywords_re": r"(strategy|estrategia|business operations|bizops|consulting|consultoria|process|processes|procesos?|continuous improvement|mejora continua|operational excellence|excelencia operacional|\bgbs\b|global business services|shared services|servicios compartidos|change management|gestion del cambio|enablement|transformation|transformacion|lean|six sigma)",
        "ic_titles": r"(management consultant|strategy consultant|business consultant|consultor(a)? de (negocios|procesos|estrategia)|business analyst|analista de negocios?|black belt|green belt)",
    },
    {
        "id": "finance_legal",
        "label": "Finance, Accounting & Legal",
        "hint": "Finance, FP&A, Accounting, Audit, Tax, Treasury, Legal & Compliance",
        "keywords_re": r"(finance|finanzas|financial|financier[oa]|\bfp&a\b|accounting|contabilidad|contable|controller|controllership|contraloria|contralor(a)?|audit|auditoria|tax|impuestos|fiscal|treasury|tesoreria|legal|counsel|abogad[oa]|juridic[oa]|compliance|cumplimiento|credit|credito|risk|riesgos?|payroll|nomina|billing|facturacion|accounts (payable|receivable)|cuentas por (pagar|cobrar))",
        "ic_titles": r"(accountant|contador(a)?|auditor(a)?|controller|contralor(a)?|paralegal|abogad[oa]|legal counsel|treasurer|tesorer[oa]|bookkeeper)",
    },
    {
        "id": "people_hr",
        "label": "People, HR & Talent",
        "hint": "Human Resources, Talent Acquisition, People Operations, Learning & Development",
        "keywords_re": r"(human resources|recursos humanos|\bhr\b|\brh\b|\bhrbp\b|people|talent|talento|recruiting|recruitment|reclutamiento|compensation|compensaciones|total rewards|learning|training|capacitacion|employee experience|labor relations|relaciones laborales|culture|cultura|organizational development|desarrollo organizacional)",
        "ic_titles": r"(recruiter|reclutador(a)?|hr business partner|\bhrbp\b|people partner|talent acquisition partner|hr generalist|generalista de (rh|recursos humanos)|sourcer)",
    },
    {
        "id": "marketing",
        "label": "Marketing, Comms & Design",
        "hint": "Marketing, Growth, Brand, Communications, Content, UX / Product Design",
        "keywords_re": r"(marketing|mercadotecnia|growth|brand|marca|communications|comunicacion|comunicaciones|public relations|relaciones publicas|demand generation|content|contenido|design|designer|diseno|disenador(a)?|\bux\b|\bui\b|creative|creativ[oa]|social media|redes sociales|\bseo\b|\bsem\b|advertising|publicidad|e-?commerce)",
        "ic_titles": r"(marketing manager|gerente de (marketing|mercadotecnia)|brand manager|product marketing manager|campaign manager|content manager|community manager|social media manager|copywriter|content strategist|ux researcher|ux writer|art director)",
    },
    {
        "id": "other",
        "label": "Other / Open (Guided by Free Text)",
        "hint": "Let the AI infer exact target roles from my CV and free-text description below",
        "keywords_re": r"",
        "ic_titles": r"",
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

# Short pill text for the onboarding page. The full `label` stays the source of truth for prompts,
# the confirmation email and the pill tooltip, so the page can stay quiet without losing meaning.
OPTION_SHORT_LABELS = {
    "specialist": "Specialist",
    "senior": "Senior / Lead",
    "manager": "Manager",
    "director": "Director / Head",
    "executive": "VP / C-Level",
    "operations": "Operations",
    "technology": "Engineering & Data",
    "project_product": "Project & Product",
    "sales_customer": "Sales & Customer",
    "strategy_process": "Strategy & Process",
    "finance_legal": "Finance & Legal",
    "people_hr": "People & HR",
    "marketing": "Marketing & Design",
    "other": "Other",
    "jalisco": "Jalisco, on-site or hybrid",
    "remote_mx": "Remote in Mexico",
    "mx_country": "Mexico, city not listed",
}

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
2. `target_titles`: Up to 12 job titles this candidate wants, written as plain phrases the way they appear in job postings (2 to 5 words each; add the Spanish form when the title is commonly posted in Spanish). Example: "process mining lead", "gerente de mejora continua".
3. `focus_keywords`: Up to 12 short domain keywords (1 to 3 words) that appear in the titles of jobs in this candidate's field. Example: "process mining", "automation", "supply chain". Do not use seniority words (senior, manager, director, lead) or generic words (business, global, team).
4. `step_up_titles`: Up to 6 plain phrases for ambitious step-up / promotion titles for this candidate.
5. `avoid_titles`: Up to 10 plain words or short phrases that mark job titles the candidate asked to avoid in free text, or common false-positive titles unrelated to their craft. Example: "tax", "accounts payable", "maintenance technician".

Rules for the four lists: plain lower-case words only (letters, digits, spaces and & / + . ' -). Never write regular expressions, wildcards, brackets or backslashes; anything that is not a plain phrase is discarded.

Return ONLY valid JSON:
{
  "career_goal": "2-4 sentence north-star career target and exclusions...",
  "target_titles": ["example target title"],
  "focus_keywords": ["example keyword"],
  "step_up_titles": ["example step up title"],
  "avoid_titles": ["example unwanted title"]
}"""


# --------------------------------------------------------------------------
# Validation & Slug Generation
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


# --------------------------------------------------------------------------
# Plain phrases -> title patterns
# --------------------------------------------------------------------------
#
# The model never writes regular expressions. It returns plain phrases ("process mining lead"),
# and the patterns are built here with every character escaped. A phrase can therefore only
# match its own literal words: it cannot be invalid, match everything, or make matching slow.

MAX_PHRASE_CHARS = 60
MAX_PHRASE_WORDS = 6
_PHRASE_CHARS_RE = re.compile(r"[a-z0-9 &/+.'#-]+")

# Single words that say nothing about the job area. A "focus keyword" equal to one of these
# would make the area test meaningless ("level AND manager").
GENERIC_TITLE_WORDS = frozenset("""
    senior sr junior jr lead leader lider manager gerente director directora head principal staff
    specialist especialista analyst analista associate consultant consultor consultora engineer
    ingeniero ingeniera coordinator coordinador coordinadora executive ejecutivo ejecutiva officer
    vp chief global regional local business negocio negocios team equipo mexico latam remote
    remoto hybrid professional general corporate role roles job jobs
""".split())


def clean_phrase(raw):
    """Normalise one model-supplied phrase (lower-case, accents folded, single spaces).
    Returns '' unless it is a short plain phrase containing at least one letter."""
    if not isinstance(raw, str):
        return ""
    phrase = re.sub(r"\s+", " ", fj.norm(raw)).strip(" .-'/&")
    if not 2 <= len(phrase) <= MAX_PHRASE_CHARS or len(phrase.split()) > MAX_PHRASE_WORDS:
        return ""
    if not _PHRASE_CHARS_RE.fullmatch(phrase) or not re.search(r"[a-z]", phrase):
        return ""
    return phrase


def phrase_to_pattern(phrase):
    """Pattern source matching `phrase` as whole words in a normalised job title. Words may be
    separated by spaces, '-' or '/'. Every character of the phrase is escaped."""
    return r"(?<!\w)" + r"[\s/-]+".join(re.escape(word) for word in phrase.split()) + r"(?!\w)"


def clean_phrases(raw_list, limit, drop=frozenset()):
    """Clean a model-supplied list into at most `limit` distinct phrases, leaving out `drop`."""
    phrases = []
    for raw in raw_list if isinstance(raw_list, list) else []:
        phrase = clean_phrase(raw)
        if phrase and phrase not in drop and phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= limit:
            break
    return phrases


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


# Heading used by the offline parser when it cannot separate employers (plain-text uploads).
PLACEHOLDER_EMPLOYER = "Experience listed in uploaded CV"


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
                        # Nothing is known about this employer beyond what the CV says, so the
                        # line only restates that (name and place); it never invents a description.
                        place = ", ".join(part for part in parts[1:2] if part)
                        sanitized_lines.append(
                            f"*Company Profile: {last_company_name}{', ' + place if place else ''}.*"
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
        if not cv_problems(out, name, email):
            return out
        # The Markdown did not follow the expected employer / role / bullet layout closely
        # enough to tailor from, so treat the upload as plain text instead of rejecting it.

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
        # Employers, roles and dates cannot be told apart without the model, so the whole
        # history goes under one clearly-labelled placeholder heading. Nothing is assumed about
        # the person: no seniority, no location, no languages. A sign-up stored in this form
        # gets no tailored CVs (see `register_subscriber_in_codebase`).
        "experience": [
            {
                "company": PLACEHOLDER_EMPLOYER,
                "location": "",
                "company_profile": "Employers were not separated when this CV was imported. The points below are taken from the uploaded text.",
                "role": "Experience summary",
                "dates": "see uploaded CV",
                "bullets": bullet_lines[:10] or summary_lines[:3],
            }
        ],
        "education": [],
        "languages": [],
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
                cv_md = _render_canonical_cv_markdown(parsed, name, email)
                problems = cv_problems(cv_md, name, email)
                if not problems:
                    return cv_md, str(parsed.get("_llm_model") or model)
                fj.gha_annotate(
                    "warning",
                    "Onboarding CV from Gemini was not usable (" + "; ".join(problems) + "); using the offline parser.",
                )
        except Exception as e:
            fj.gha_annotate("warning", f"Onboarding CV Gemini structuring fallback ({type(e).__name__}: {e})")
    return structure_master_cv_deterministic(raw_cv_text, name, email), "deterministic-fallback"


# --------------------------------------------------------------------------
# Smart Radar Rule & Career-Goal Synthesis
# --------------------------------------------------------------------------

# Levels whose titles are numerous on every board. A level word alone ("senior", "manager") is
# never enough for them: the title must also name one of the subscriber's job areas or a
# keyword from their own goals. Director and executive titles are rare enough to go straight
# to the AI review.
VOLUME_LEVELS = ("specialist", "senior", "manager")
# Levels that also want individual-contributor titles carrying no level word at all.
IC_LEVELS = ("specialist", "senior")

# Typical title words per level. An "avoid" phrase that matches one of these for a ticked level
# is ignored, so the model can never exclude the very level the subscriber asked for.
LEVEL_PROBE_TITLES = {
    "specialist": ("analyst", "analista", "specialist", "engineer"),
    "senior": ("senior", "lead", "principal"),
    "manager": ("manager", "gerente"),
    "director": ("director", "head of"),
    "executive": ("vp", "vice president", "general manager"),
}

NOISE_EXCLUDE_UNLESS_SALES = r"\b(call center agent|telemarketing|retail store|cashier|promotor de cambaceo)\b"
NOISE_EXCLUDE_UNLESS_SPECIALIST = r"\b(junior|jr\.?|entry[- ]level|trainee)\b"


def _add_unique(target, patterns):
    """Append each non-empty pattern that is not in `target` yet (order preserved)."""
    for pattern in patterns:
        if pattern and pattern not in target:
            target.append(pattern)


def build_title_rules(levels, domains, phrases=None):
    """Turn the form's level and job-area choices, plus plain phrases derived from the free
    text and CV, into a subscriber's title rules.

    A title is accepted when it
      (a) contains one of the target-title phrases, or
      (b) is a director / executive title and that level was ticked, or
      (c) is a level-less individual-contributor title of a ticked area (Specialist or Senior
          ticked), or
      (d) contains BOTH a word of a ticked volume level (specialist, senior, manager) AND a
          word of a ticked area or a focus keyword.
    Rule (d) is what keeps "Senior Tax Accountant" away from someone who ticked Senior and
    asked for marketing roles.

    When a volume level is ticked and nothing narrows it (no area, no phrases), the level words
    are accepted on their own and `rule_breadth` is "broad"; otherwise it is "focused".

    `phrases` holds the model's lists (`target_titles`, `focus_keywords`, `step_up_titles`,
    `avoid_titles`). Anything in them that is not a plain phrase is ignored."""
    phrases = phrases if isinstance(phrases, dict) else {}
    specific_domains = [d for d in domains if d in DOMAIN_BY_ID and d != "other"]
    target_phrases = clean_phrases(phrases.get("target_titles"), 12)
    focus_phrases = clean_phrases(phrases.get("focus_keywords"), 12, drop=GENERIC_TITLE_WORDS)
    step_up_phrases = clean_phrases(phrases.get("step_up_titles"), 6)
    avoid_phrases = clean_phrases(phrases.get("avoid_titles"), 10)

    include = [phrase_to_pattern(p) for p in target_phrases]        # rule (a)
    level_group = []                                                # rule (d), first half
    upgrade = []
    for level_id in levels:
        level = SENIORITY_BY_ID[level_id]
        _add_unique(upgrade, level["up"])
        _add_unique(level_group if level_id in VOLUME_LEVELS else include, level["inc"])  # rule (b)
    _add_unique(upgrade, [phrase_to_pattern(p) for p in step_up_phrases])

    area_group = []                                                 # rule (d), second half
    wants_ic_titles = any(level_id in IC_LEVELS for level_id in levels)
    for domain_id in specific_domains:
        domain = DOMAIN_BY_ID[domain_id]
        _add_unique(area_group, [rf"\b{domain['keywords_re']}\b"])
        if wants_ic_titles and domain.get("ic_titles"):
            _add_unique(include, [rf"\b{domain['ic_titles']}\b"])       # rule (c)
    _add_unique(area_group, [phrase_to_pattern(p) for p in focus_phrases])

    title_all_of = []
    breadth = "focused"
    if level_group and area_group:
        title_all_of = [level_group, area_group]
    elif level_group and not target_phrases:
        # Nothing narrows the level words (no area ticked, no phrases derived), so accept them
        # alone rather than drop the levels the subscriber asked for.
        _add_unique(include, level_group)
        breadth = "broad"

    exclude = list(BASE_TITLE_EXCLUDES)
    if "sales_customer" not in specific_domains:
        _add_unique(exclude, [NOISE_EXCLUDE_UNLESS_SALES])
    if "specialist" not in levels:
        _add_unique(exclude, [NOISE_EXCLUDE_UNLESS_SPECIALIST])
    wanted = target_phrases + focus_phrases + [
        probe for level_id in levels for probe in LEVEL_PROBE_TITLES.get(level_id, ())
    ]
    for phrase in avoid_phrases:
        pattern = phrase_to_pattern(phrase)
        if not any(re.search(pattern, text) for text in wanted):
            _add_unique(exclude, [pattern])

    return {
        "title_include": include,
        "title_all_of": title_all_of,
        "upgrade_title_include": upgrade,
        "title_exclude": exclude,
        "interest_phrases": target_phrases + [p for p in focus_phrases if p not in target_phrases],
        "rule_breadth": breadth,
    }


def synthesize_subscriber_profile(validated, structured_cv_md, api_key=None, model=fj.DEFAULT_LLM_MODEL):
    """Build the profile fields of a subscriber record (career goal, title rules, locations)
    from the form's broad choices, the free-text description and the structured CV.

    Gemini, when available, writes the career goal and returns plain phrases; the title
    patterns themselves are always built in code (`build_title_rules`). Returns
    `(profile_fields, rule_source)` where `rule_source` is the model name or "deterministic"."""
    levels = validated["levels"]
    domains = validated["domains"]
    locations = validated["locations"]
    free_text = validated["free_text"]

    specific_domains = [d for d in domains if d in DOMAIN_BY_ID and d != "other"]
    level_labels = [SENIORITY_BY_ID[k]["label"] for k in levels if k in SENIORITY_BY_ID]
    domain_labels = [DOMAIN_BY_ID[k]["label"] for k in specific_domains]
    loc_labels = [LOCATION_BY_ID[k]["label"] for k in locations if k in LOCATION_BY_ID]

    # Baseline career goal from the form alone; replaced by Gemini's richer version when available.
    goal_parts = [
        f"Targeting {', '.join(level_labels)} roles"
        + (f" in {', '.join(domain_labels)}" if domain_labels else "")
        + f" ({', '.join(loc_labels)})."
    ]
    if free_text:
        goal_parts.append(f"Candidate goal & preferences: {fj.sanitize_ats_text(free_text)}")
    career_goal = " ".join(goal_parts)[:1200]

    phrases = {}
    rule_source = "deterministic"
    api_key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY") or "").strip()
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
                phrases = parsed
                rule_source = str(parsed.get("_llm_model") or model)
        except Exception as e:
            fj.gha_annotate("warning", f"Onboarding Gemini profile calibration fallback ({type(e).__name__}: {e})")

    rules = build_title_rules(levels, domains, phrases)
    if rules["rule_breadth"] == "broad":
        fj.gha_annotate(
            "warning",
            f"Title rules for {validated['name']} are broad (level words only): no job area was ticked and "
            "no keywords could be derived from the free text and CV. Every matching role still goes "
            "through the AI review, but many will be off-target.",
        )

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
        **rules,
        "include_remote_mexico": "remote_mx" in locations,
        "include_country_only": "mx_country" in locations,
    }
    if "jalisco" not in locations:
        sub_entry["locations"] = []
    return sub_entry, rule_source


# --------------------------------------------------------------------------
# Codebase Registration (cvs/<id>.md + subscribers/<id>.json)
# --------------------------------------------------------------------------

_VALIDATION_SAMPLE_JOB = {
    "title": "Director, Digital Transformation & Operations",
    "company": "Flex",
    "location": "Guadalajara, Jalisco, Mexico",
    "team": "Global Operations",
}


def cv_problems(cv_md, name="Candidate", email=""):
    """Reasons a structured master CV would break CV tailoring or the checks the shared
    test-suite runs on every subscriber's CV. Returns [] when the CV is usable."""
    problems = []
    if fj.ATS_ILLEGAL_CHARS_RE.search(cv_md):
        problems.append("the CV contains characters that ATS portals reject")
    if not fj.extract_cv_company_profiles(cv_md):
        problems.append("no employer in the CV has a '*Company Profile: ...*' line")
        return problems
    try:
        sub_stub = {"name": name or "Candidate", "recipient_email": email}
        tailored_cv = fj.tailor_cv_deterministic(dict(_VALIDATION_SAMPLE_JOB), cv_md, sub_stub)["cv"]
        contexts = [exp["company_context"] for exp in tailored_cv.get("experience") or [] if exp.get("company_context")]
        if not contexts:
            problems.append("no role with bullet points was found under an employer that has a Company Profile")
            return problems
        # The employer context must survive into all three formats a tailored CV is sent in.
        snippet = contexts[0][:32]
        docx_text = fj.extract_docx_text(fj.render_minimalist_cv_docx(tailored_cv, _VALIDATION_SAMPLE_JOB))
        pdf_text = fj.extract_pdf_text(fj.render_minimalist_cv_pdf(tailored_cv, _VALIDATION_SAMPLE_JOB))
        html_text = fj.render_minimalist_cv_html(tailored_cv, _VALIDATION_SAMPLE_JOB)
        if (
            snippet not in docx_text
            or fj.pdf_safe_text(snippet[:20]) not in pdf_text
            or html.escape(snippet) not in html_text
        ):
            problems.append("an employer's Company Profile does not survive into the tailored CV formats")
        if fj.ATS_ILLEGAL_CHARS_RE.search(docx_text) or fj.ATS_ILLEGAL_CHARS_RE.search(pdf_text):
            problems.append("a CV tailored from it would contain characters that ATS portals reject")
    except Exception as e:
        problems.append(f"a tailored CV could not be built from it ({type(e).__name__}: {e})")
    return problems


def profile_problems(sub_record, cv_md):
    """Reasons a new subscriber profile must not be stored. Returns [] when it is safe.

    A stored profile is loaded by every later daily run and by the test-suite that runs before
    each of them, so one bad profile would otherwise stop the daily email for everybody."""
    problems = cv_problems(cv_md, sub_record.get("name"), sub_record.get("recipient_email") or "")
    if not str(sub_record.get("career_goal") or "").strip():
        problems.append("the profile has no career goal")
    pattern_lists = [
        (key, sub_record.get(key) or [])
        for key in ("title_include", "title_exclude", "upgrade_title_include")
    ] + [
        (f"title_all_of[{idx}]", group)
        for idx, group in enumerate(sub_record.get("title_all_of") or [])
    ]
    for label, patterns in pattern_lists:
        for pattern in patterns:
            try:
                re.compile(pattern, re.I)
            except (re.error, TypeError) as e:
                problems.append(f"{label} pattern {pattern!r} is not a valid pattern ({e})")
                continue
            if fj.has_nested_quantifier(pattern):
                problems.append(f"{label} pattern {pattern!r} could make title matching extremely slow")
    if not sub_record.get("title_include") and not sub_record.get("title_all_of"):
        problems.append("the profile has no title rule, so it could never match a role")
    return problems


def subscriber_file_path(root, sub_id):
    """Absolute path of `subscribers/<id>.json`. Raises ValueError for an id that is not a
    plain file name, which also rules out any path outside the `subscribers/` folder."""
    if not isinstance(sub_id, str) or not fj.SUBSCRIBER_ID_RE.fullmatch(sub_id):
        raise ValueError(
            f"Subscriber id {sub_id!r} cannot be used as a file name. Rename it in config.json to "
            "lower-case letters, digits and underscores (2 to 30 characters, starting with a letter)."
        )
    base = (Path(root) / fj.SUBSCRIBERS_DIR).resolve()
    path = (base / f"{sub_id}.json").resolve()
    path.relative_to(base)
    return path


def _write_text_atomic(path, text):
    """Write a text file through a sibling temp file so a crash never leaves it half-written."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def register_subscriber_in_codebase(validated, root=ROOT, api_key=None, model=fj.DEFAULT_LLM_MODEL):
    """Create or update one subscriber: `cvs/<id>.md` and `subscribers/<id>.json`.

    Only those two files are written and both belong to this subscriber alone, so two sign-ups
    can never overwrite each other and config.json is never edited. Nothing is written unless
    the profile passes `profile_problems`. Re-submitting with the same email updates the same
    id and stamps `profile_updated_at`, which makes the next daily run forget AI rejections made
    under the old profile.

    Returns a result dict for the confirmation email and the run summary."""
    root = Path(root).resolve()
    cfg = fj.load_config(root) if (root / "config.json").is_file() else dict(fj.CFG_DEFAULTS)
    sub_id, updated_existing = derive_subscriber_id(
        validated["name"], validated["email"], cfg.get("subscribers") or []
    )
    record_path = subscriber_file_path(root, sub_id)

    # Path-traversal boundary verification on cvs/<sub_id>.md
    cvs_dir = (root / "cvs").resolve()
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
        "profile_updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Without the model the offline parser may have put the whole history under a placeholder
    # heading. Matching still works from that text, but a CV tailored from it would show the
    # placeholder to employers, so tailoring stays off until the person submits again.
    tailored_cvs_enabled = f"### {PLACEHOLDER_EMPLOYER}" not in cv_md
    if not tailored_cvs_enabled:
        sub_record["max_tailored_cvs"] = 0
        fj.gha_annotate(
            "warning",
            f"The CV for {sub_record['name']} was imported without employer structure (Gemini was not "
            "available), so tailored CVs are switched off for this subscriber until they submit the form again.",
        )

    problems = profile_problems(sub_record, cv_md)
    if problems:
        raise ValueError("This sign-up cannot be stored: " + "; ".join(problems) + ".")

    cvs_dir.mkdir(parents=True, exist_ok=True)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(cv_abs_path, cv_md)
    fj.write_json(record_path, sub_record)

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
        "record_file": f"{fj.SUBSCRIBERS_DIR}/{sub_id}.json",
        "career_goal": sub_record["career_goal"],
        "levels": validated["levels"],
        "domains": validated["domains"],
        "locations": validated["locations"],
        "language": validated["language"],
        "free_text": validated["free_text"],
        "employers_indexed": employers_indexed,
        "title_include_count": len(sub_record["title_include"]) + sum(len(g) for g in sub_record["title_all_of"]),
        "title_exclude_count": len(sub_record["title_exclude"]),
        "rule_breadth": sub_record["rule_breadth"],
        "tailored_cvs_enabled": tailored_cvs_enabled,
        "cv_model": cv_model_used,
        "rule_model": rule_model_used,
        "onboarded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# --------------------------------------------------------------------------
# Professional Confirmation Email from Lucy
# --------------------------------------------------------------------------

CV_NOT_STRUCTURED_NOTE = (
    "Note: your CV could not be fully structured this time, so tailored CVs are switched off for now. "
    "Your matches are not affected. Submit the form again with the same email address in a few hours to enable them."
)


def build_onboarding_confirmation_email(result, sender_email, board_count=109):
    """Build the welcome and confirmation email (plain text and HTML) from Lucy.

    The HTML part is assembled from the same building blocks as the daily briefing
    (`fj.email_*`), so the two look alike: Lucy's dark card (what she does and what happens
    next), then one card with the subscriber's radar settings.

    When the CV could not be structured (`tailored_cvs_enabled` is False) the email says so in
    an amber note and makes no promise of tailored CVs anywhere else."""
    recipient = result["email"]
    name = result["name"]
    first_name = (name or "Partner").split()[0]
    sub_id = result["subscriber_id"]
    is_update = bool(result.get("updated_existing"))
    cvs_on = result.get("tailored_cvs_enabled") is not False

    level_labels = ", ".join(SENIORITY_BY_ID[k]["label"] for k in result.get("levels") or [] if k in SENIORITY_BY_ID) or "Leadership"
    domain_labels = ", ".join(DOMAIN_BY_ID[k]["label"] for k in result.get("domains") or [] if k in DOMAIN_BY_ID) or "Cross-Functional Leadership"
    loc_labels = " + ".join(LOCATION_BY_ID[k]["label"] for k in result.get("locations") or [] if k in LOCATION_BY_ID) or "Jalisco + Remote Mexico"
    language = result.get("language") or "English"
    career_goal = result.get("career_goal") or ""
    # The placeholder heading stands in for employers that could not be told apart; it is not one.
    employers = [e for e in (result.get("employers_indexed") or []) if e != PLACEHOLDER_EMPLOYER]
    if employers:
        emp_txt = ", ".join(employers[:6]) + (f" (+{len(employers) - 6} more)" if len(employers) > 6 else "")
    elif cvs_on:
        emp_txt = "Master CV structured & ATS-sanitized"
    else:
        emp_txt = "Not identified yet (your work history is stored as one block)"

    action_word = "Updated & Recalibrated" if is_update else "Active"
    msg = EmailMessage()
    msg["Subject"] = f"✦ Welcome to Jobs Sentinel, {first_name} — Your Private AI Talent Radar Is {action_word}"
    msg["From"] = f"Lucy · Personal Headhunter <{sender_email}>"
    msg["To"] = recipient
    msg["Reply-To"] = sender_email

    briefing_step = (
        "3. Tailored CVs & Rationale: When genuine matches land, you receive a concise morning briefing explaining why each role fits your trajectory—with ATS-ready Microsoft Word (.docx) CVs pre-built for your top picks."
        if cvs_on
        else "3. Rationale: When genuine matches land, you receive a concise morning briefing explaining why each role fits your trajectory."
    )
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
        f"- Briefing Language: {language}",
        f"- CV Employers Indexed & Enriched: {emp_txt}",
        "",
        "YOUR CAREER NORTH STAR (AI MATCH GATE)",
        career_goal,
        "",
        "WHAT HAPPENS NEXT",
        f"1. Daily Market Sweep: Every morning I scan {board_count} verified employer career portals across Workday, Greenhouse, SmartRecruiters, Oracle HCM, Eightfold, Lever, Ashby, BambooHR, and Workable.",
        "2. AI Fit Validation: Each candidate opening is reviewed by Gemini against your Career North Star, your real CV experience, and the company's industry profile. Roles that aren't a strong fit are filtered out automatically.",
        briefing_step,
    ]
    if not cvs_on:
        plain_lines += ["", CV_NOT_STRUCTURED_NOTE]
    plain_lines += [
        "",
        "Shoulder to shoulder,",
        "Lucy · Personal Headhunter (Jobs Sentinel)",
    ]
    msg.set_content("\n".join(plain_lines))

    # HTML part, from the shared building blocks. Every value from the sign-up goes through
    # `fj.email_text`, which escapes it and lets an over-long word wrap on a phone.
    first_esc = fj.email_text(first_name)
    sub_id_esc = fj.email_text(sub_id)
    bright = f'style="color:{fj.EMAIL_ON_DARK};"'

    status_html = (
        f'<span style="display:inline-block;padding:3px 10px;border:1px solid {fj.EMAIL_GREEN_LINE};border-radius:999px;'
        f"background-color:{fj.EMAIL_GREEN_SOFT};color:{fj.EMAIL_GREEN_DEEP};font-size:10px;font-weight:800;"
        f'letter-spacing:0.6px;line-height:1.5;">&#9679; '
        + fj.email_text("PROFILE RECALIBRATED" if is_update else "RADAR LIVE & ARMED")
        + "</span>"
    )
    cv_word = "structured master CV" if cvs_on else "master CV"
    dek_html = (
        f'I&rsquo;m <strong style="color:{fj.EMAIL_INK};">Lucy</strong>, your private AI headhunter. Your onboarding profile '
        f'(<code style="padding:1px 6px;border-radius:4px;background-color:{fj.EMAIL_PAGE};font-size:12px;color:{fj.EMAIL_INK};">{sub_id_esc}</code>) '
        f"and {cv_word} are now committed to the Jobs Sentinel codebase."
    )
    message_html = (
        f"Every morning I sweep <strong {bright}>{board_count} verified employer career portals</strong> across 9 ATS platforms, "
        "evaluate candidate openings with Gemini against your Career North Star and CV, "
        + (
            f"discard weak fits automatically, and attach <strong {bright}>ATS-ready Word (.docx) CVs</strong> "
            "for your strongest opportunities."
            if cvs_on
            else "and discard weak fits automatically."
        )
    )
    briefing_contents = (
        "each employer&rsquo;s industry, the reason the role fits, and tailored CVs for your top picks"
        if cvs_on
        else "each employer&rsquo;s industry and the reason the role fits"
    )
    next_html = (
        "Starting with the next scheduled sweep, you&rsquo;ll receive your daily <em>Lucy Private Talent Briefing</em> "
        'whenever genuine, never-before-sent matches pass our AI <span style="white-space:nowrap;">review&mdash;</span>'
        f"complete with {briefing_contents}. "
        "You can update your CV or preferences anytime using the onboarding form with this same email address."
    )

    def _setting_row(label, value, last=False, colour=fj.EMAIL_INK):
        """One 'label: value' line of the radar settings."""
        rule = "" if last else f"border-bottom:1px solid {fj.EMAIL_LINE};"
        return (
            f'<tr><td width="38%" valign="top" style="padding:9px 10px 9px 0;{rule}font-family:{fj.EMAIL_FONT};'
            f'font-size:12.5px;line-height:20px;font-weight:600;color:{fj.EMAIL_MUTE};">{label}</td>'
            f'<td valign="top" style="padding:9px 0;{rule}font-family:{fj.EMAIL_FONT};font-size:13.5px;line-height:20px;'
            f'color:{colour};{fj.EMAIL_WRAP}">{fj.email_text(value)}</td></tr>'
        )

    settings_html = (
        f'<table {fj.EMAIL_TABLE} style="border-collapse:collapse;margin:8px 0 0;">'
        + _setting_row("Target Seniority", level_labels)
        + _setting_row("Functional Domains", domain_labels)
        + _setting_row("Geographic Scope", loc_labels)
        + _setting_row("Briefing Language", language)
        + _setting_row("CV Employers Enriched", emp_txt, last=True, colour=fj.EMAIL_INK if cvs_on else fj.EMAIL_AMBER)
        + "</table>"
    )
    blueprint_html = fj.email_card(
        fj.email_panel("Career North Star (AI Match Gate Prompt)", fj.email_text(career_goal), margin_top=0)
        + settings_html
    )

    preheader = (
        f"Your private AI job radar ({sub_id}) is active across {board_count} employer boards — only verified matches"
        + (" + ATS-ready CVs." if cvs_on else ".")
    )
    content_html = (
        fj.email_masthead(status_html)
        + fj.email_greeting(f"Welcome to your unfair advantage, {first_esc}.", dek_html)
        + fj.email_lucy_card(
            "Us vs. Their Machine &middot; Onboarding Confirmed",
            "Zero Job-Board Noise. Only Verified Matches Built Around Your Track Record.",
            message_html,
            "What happens next",
            next_html,
        )
        + ("" if cvs_on else fj.email_note(fj.email_text(CV_NOT_STRUCTURED_NOTE), "warn"))
        + fj.email_section("Your Calibrated Radar Blueprint", blueprint_html)
        + fj.email_footer([
            f'Shoulder to shoulder, always. &mdash; <strong style="color:{fj.EMAIL_INK};">Lucy</strong> &middot; Jobs Sentinel'
        ])
    )
    msg.add_alternative(
        fj.email_document("Welcome to Jobs Sentinel", html.escape(preheader), content_html), subtype="html"
    )
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
                # Explicit context: without one smtplib does not verify the server certificate,
                # and this connection carries the mail password.
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30, context=ssl.create_default_context()) as server:
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
<title>Lucy · Set up your job radar</title>
<style>
:root{color-scheme:light dark;--bg:#ffffff;--ink:#18181b;--body:#3f3f46;--mute:#71717a;--line:#e4e4e7;--accent:#0f766e;--danger:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#111113;--ink:#fafafa;--body:#d4d4d8;--mute:#a1a1aa;--line:#2e2e33;--accent:#5eead4;--danger:#fca5a5}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--body);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:560px;margin:0 auto;padding:40px 20px 72px}
.top{display:flex;justify-content:space-between;align-items:baseline;font-size:.8125rem;color:var(--mute)}
.brand{color:var(--ink);font-weight:600}
.top a{color:inherit;text-decoration:none}
.top a:hover{color:var(--ink)}
h1{margin:48px 0 10px;font-size:1.75rem;font-weight:700;letter-spacing:-.5px;line-height:1.2;color:var(--ink)}
.lede{margin:0;color:var(--mute);font-size:.9375rem}
section{margin-top:34px}
.label{display:block;margin:0 0 8px;font-size:.875rem;font-weight:600;color:var(--ink)}
.label small{margin-left:6px;font-weight:400;font-size:.8125rem;color:var(--mute)}
.row{display:grid;gap:16px;grid-template-columns:1fr}
@media (min-width:520px){.row{grid-template-columns:1fr 1fr}}
input[type=text],input[type=email],input[type=password],select,textarea{display:block;width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--ink);font:inherit;font-size:.9375rem}
select,#inviteCode{max-width:240px}
#inviteField{display:block}
textarea{resize:vertical;line-height:1.5}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--ink)}
::placeholder{color:var(--mute);opacity:.8}
.pills{display:flex;flex-wrap:wrap;gap:8px}
.pill{position:relative;display:inline-flex;align-items:center;padding:7px 14px;border:1px solid var(--line);border-radius:999px;font-size:.875rem;line-height:1.3;color:var(--body);cursor:pointer;user-select:none;transition:background .12s,border-color .12s,color .12s}
.pill:hover{border-color:var(--ink)}
.pill input{position:absolute;opacity:0;width:1px;height:1px;margin:0;pointer-events:none}
.pill.on{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.pill:focus-within{outline:2px solid var(--accent);outline-offset:2px}
.split{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin:0 0 8px}
.split .label{margin:0}
.link{padding:0;border:0;background:none;font:inherit;font-size:.8125rem;color:var(--mute);text-decoration:underline;text-underline-offset:3px;cursor:pointer}
.link:hover{color:var(--ink)}
.count{margin:6px 0 0;font-size:.75rem;color:var(--mute);text-align:right}
.starters{display:flex;flex-wrap:wrap;gap:6px 16px;margin:0 0 8px}
.more{margin-top:14px}
.go{display:block;width:100%;margin-top:40px;padding:13px 18px;border:0;border-radius:8px;background:var(--ink);color:var(--bg);font:inherit;font-weight:600;cursor:pointer}
.go:hover{opacity:.9}
.go:disabled{opacity:.5;cursor:default}
.status{display:none;margin:16px 0 0;font-size:.875rem;line-height:1.55}
.status.ok{display:block;color:var(--accent)}
.status.err{display:block;color:var(--danger)}
.foot{margin:48px 0 0;font-size:.75rem;color:var(--mute)}
[hidden]{display:none !important}
</style>
</head>
<body>
<main>
  <div class="top">
    <span class="brand">Lucy</span>
    <a href="index.html">Job board</a>
  </div>

  <h1>Set up your job radar</h1>
  <p class="lede">Share your CV and what you want next. Each morning Lucy checks more than 100 employers in Jalisco and remote Mexico and emails you only the roles that fit, with the reason why.</p>

  <form id="onboardForm" novalidate>
    <section class="row">
      <label><span class="label">Name</span>
        <input id="fullName" type="text" required maxlength="80" autocomplete="name">
      </label>
      <label><span class="label">Email</span>
        <input id="email" type="email" required maxlength="180" autocomplete="email">
      </label>
    </section>

    <section>
      <div class="split">
        <label class="label" for="cvText">CV <small>plain text</small></label>
        <button type="button" class="link" id="uploadBtn">Upload .txt or .md</button>
      </div>
      <input id="cvFile" type="file" accept=".txt,.md,.markdown,text/plain,text/markdown" hidden>
      <textarea id="cvText" rows="7" maxlength="45000" required placeholder="Paste your CV here"></textarea>
      <p class="count" id="charCount">0 / 45,000</p>
    </section>

    <section>
      <span class="label" id="levelLabel">Role level</span>
      <div class="pills" id="levelChecks" role="group" aria-labelledby="levelLabel"></div>
    </section>

    <section>
      <span class="label" id="domainLabel">Job area <small>optional</small></span>
      <div class="pills" id="domainChecks" role="group" aria-labelledby="domainLabel"></div>
    </section>

    <section>
      <span class="label" id="locationLabel">Location</span>
      <div class="pills" id="locationChecks" role="group" aria-labelledby="locationLabel"></div>
    </section>

    <section>
      <label class="label" for="freeText">What are you looking for?</label>
      <div class="starters" id="promptChips">
        <button type="button" class="link" data-snippet="Roles I want next: ">+ Roles I want</button>
        <button type="button" class="link" data-snippet="Industries or company types I like: ">+ Industries I like</button>
        <button type="button" class="link" data-snippet="Roles or areas to avoid: ">+ Roles to avoid</button>
      </div>
      <textarea id="freeText" rows="4" maxlength="4000" placeholder="In your own words: the roles you want, the industries you like and anything to skip."></textarea>
    </section>

    <section>
      <label class="label" for="language">Email language</label>
      <select id="language">
        <option value="English" selected>English</option>
        <option value="Spanish">Espa&ntilde;ol</option>
        <option value="Portuguese">Portugu&ecirc;s</option>
      </select>
      <div class="more">
        <button type="button" class="link" id="inviteToggle">Have an invite code?</button>
        <label id="inviteField" hidden><span class="label">Invite code</span>
          <input id="inviteCode" type="password" maxlength="80" autocomplete="off">
        </label>
      </div>
    </section>

    <button type="submit" class="go" id="submitBtn">Activate my job radar</button>
    <div id="statusBanner" class="status" role="status" aria-live="polite"></div>
    <button type="button" class="link more" id="downloadFallbackBtn" hidden>Download my details (.json)</button>
  </form>

  <p class="foot">You will get a confirmation email. To update your profile later, submit again with the same email.</p>
</main>
<script>
const CONFIG = __ONBOARD_CONFIG__;
const $ = id => document.getElementById(id);
const SUBMIT_LABEL = "Activate my job radar";

// Options render as pill toggles: a short label on the pill, the full name and examples in its tooltip.
function buildPills(containerId, items) {
  const wrap = $(containerId);
  items.forEach(item => {
    const pill = document.createElement("label");
    pill.className = "pill";
    pill.title = item.hint ? item.label + ": " + item.hint : item.label;
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = item.id;
    box.checked = Boolean(item.default);
    const text = document.createElement("span");
    text.textContent = item.short || item.label;
    const sync = () => pill.classList.toggle("on", box.checked);
    box.addEventListener("change", sync);
    sync();
    pill.append(box, text);
    wrap.appendChild(pill);
  });
}
buildPills("levelChecks", CONFIG.levels);
buildPills("domainChecks", CONFIG.domains);
buildPills("locationChecks", CONFIG.locations);

// The invite code stays out of the way unless the admin requires it.
const inviteCode = $("inviteCode");
function showInvite() {
  $("inviteField").hidden = false;
  $("inviteToggle").hidden = true;
}
if (CONFIG.require_invite_code) {
  showInvite();
  inviteCode.required = true;
} else {
  $("inviteToggle").addEventListener("click", () => { showInvite(); inviteCode.focus(); });
}

document.querySelectorAll("#promptChips button").forEach(btn => {
  btn.addEventListener("click", () => {
    const box = $("freeText");
    const snippet = btn.getAttribute("data-snippet") || "";
    box.value = box.value + (box.value && !box.value.endsWith("\n") ? "\n" : "") + snippet;
    box.focus();
  });
});

function setStatus(kind, message) {
  const el = $("statusBanner");
  el.className = kind ? "status " + kind : "status";
  el.textContent = message || "";
}

const cvText = $("cvText");
const cvFile = $("cvFile");

function updateCount() {
  $("charCount").textContent = cvText.value.length.toLocaleString() + " / 45,000";
}
cvText.addEventListener("input", updateCount);

function loadTextFile(file) {
  if (!file) return;
  if (file.size > 512000) {
    setStatus("err", "That file is too large. Please use a plain-text (.txt or .md) CV under 500 KB.");
    return;
  }
  const nameLow = (file.name || "").toLowerCase();
  if (nameLow.endsWith(".pdf") || nameLow.endsWith(".docx") || nameLow.endsWith(".doc")) {
    setStatus("err", "Please use a plain-text (.txt or .md) file, or copy the text from your PDF or Word CV and paste it in the box.");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    cvText.value = String(reader.result || "").slice(0, 45000);
    $("uploadBtn").textContent = "Loaded " + file.name;
    updateCount();
    setStatus("", "");
  };
  reader.onerror = () => setStatus("err", "Could not read that file. Please paste your CV text instead.");
  reader.readAsText(file, "utf-8");
}

$("uploadBtn").addEventListener("click", () => cvFile.click());
cvFile.addEventListener("change", () => { if (cvFile.files && cvFile.files[0]) loadTextFile(cvFile.files[0]); });
cvText.addEventListener("dragover", e => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) e.preventDefault();
});
cvText.addEventListener("drop", e => {
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) { e.preventDefault(); loadTextFile(file); }
});

function checkedValues(containerId) {
  return [...$(containerId).querySelectorAll("input[type=checkbox]:checked")].map(el => el.value);
}

function collectPayload() {
  const name = $("fullName").value.trim();
  const email = $("email").value.trim().toLowerCase();
  const cv = cvText.value.trim();
  const levels = checkedValues("levelChecks");
  const domains = checkedValues("domainChecks");
  const locations = checkedValues("locationChecks");
  const freeTxt = $("freeText").value.trim().slice(0, 4000);
  if (!name) throw new Error("Please add your name.");
  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) throw new Error("Please enter a valid email address.");
  if (cv.length < 60) throw new Error("Please paste your CV or upload a .txt or .md file (at least 60 characters).");
  if (!levels.length) throw new Error("Please pick at least one role level.");
  if (!locations.length) throw new Error("Please pick at least one location.");
  if (!domains.length && freeTxt.length < 10) {
    throw new Error("Please pick a job area or describe what you are looking for.");
  }
  if (CONFIG.require_invite_code && !inviteCode.value.trim()) {
    throw new Error("Please enter your invite code.");
  }
  return {
    name: name,
    email: email,
    invite_code: inviteCode.value.trim(),
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
    setStatus("err", err.message || String(err));
  }
});

$("onboardForm").addEventListener("submit", async e => {
  e.preventDefault();
  setStatus("", "");
  let payload;
  try {
    payload = collectPayload();
    lastPacket = payload;
  } catch (err) {
    setStatus("err", err.message || String(err));
    return;
  }

  const isLocalServer = Boolean(CONFIG.local_server);
  const endpoint = isLocalServer ? "/api/onboard" : (CONFIG.webhook_url || "").trim();

  if (!endpoint) {
    $("downloadFallbackBtn").hidden = false;
    setStatus(
      "err",
      "This form is not connected yet: no webhook URL is set in config.json (see onboarding/README.md). You can download your details below and import them with `python3 onboard_subscriber.py --from-file <file.json>`."
    );
    return;
  }

  const btn = $("submitBtn");
  btn.disabled = true;
  btn.textContent = "Setting up\u2026";

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
    setStatus(
      "ok",
      data.message || ("Welcome aboard, " + payload.name + ". Your profile is being set up now and a confirmation email is on its way to " + payload.email + ".")
    );
    btn.textContent = "Done. Check your inbox.";
  } catch (err) {
    btn.disabled = false;
    btn.textContent = SUBMIT_LABEL;
    $("downloadFallbackBtn").hidden = false;
    setStatus("err", (err.message || String(err)) + " You can also download your details below.");
  }
});
</script>
</body>
</html>
"""


def _ui_options(options):
    """Project an option catalog into the fields the onboarding page needs."""
    return [
        {
            "id": o["id"],
            "label": o["label"],
            "short": OPTION_SHORT_LABELS.get(o["id"], o["label"]),
            "hint": o["hint"],
            "default": bool(o.get("default")),
        }
        for o in options
    ]


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
        "levels": _ui_options(SENIORITY_OPTIONS),
        "domains": _ui_options(DOMAIN_OPTIONS),
        "locations": _ui_options(LOCATION_OPTIONS),
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
                cfg = fj.load_config(root) if (root / "config.json").is_file() else dict(fj.CFG_DEFAULTS)
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
                    f"Welcome aboard, {res['name']}! Profile '{res['subscriber_id']}' is now saved in {res['record_file']} and {res['cv_file']}"
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
        f"{result['cv_file']} + {result['record_file']} ({result['title_include_count']} title rules, "
        f"{result['rule_breadth']}, CV={result['cv_model']}, rules={result['rule_model']})."
    )
    fj.gha_summary(
        f"### Candidate Onboarded · `{result['subscriber_id']}`\n"
        f"- **Candidate**: {result['name']} (`{result['email']}`)\n"
        f"- **Master CV**: `{result['cv_file']}` (structured via `{result['cv_model']}`)\n"
        f"- **Profile**: `{result['record_file']}` (title rules: {result['rule_breadth']})\n"
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
