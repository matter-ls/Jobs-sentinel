"""Offline tests: fake every ATS response and SMTP server, so no network is needed.
Run from the project folder:  python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import ssl
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_jobs as fj  # noqa: E402

# These tests are offline by design. Credentials and overrides that may be exported in the shell or
# in a CI step are removed for the duration of the test run, so no test can reach a real service
# or change behaviour by accident. Each test sets what it needs with patch.dict(os.environ, ...).
for _var in (
    "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_MATCH_MODEL", "MIN_MATCH_SCORE",
    "GMAIL_USER", "GMAIL_APP_PASSWORD", "SMTP_USER", "SMTP_PASSWORD", "SMTP_HOST", "SMTP_PORT",
    "IMAP_HOST", "IMAP_PORT", "LUCY_FEEDBACK_SECRET", "LUCY_FEEDBACK_MAILBOX",
    "ONBOARD_WEBHOOK_URL", "ONBOARD_INVITE_CODE",
):
    os.environ.pop(_var, None)

CFG = fj.load_config()
RULES = fj.build_rules(CFG)


class TitleAndLocation(unittest.TestCase):
    def test_titles(self):
        yes = ["Director, Engineering", "Senior Director of Sales", "Directora de Operaciones",
               "Head of Supply Chain", "VP, Operations", "Vice President Finance",
               "Vicepresidente de Tecnologia", "General Manager Mexico", "Chief Technology Officer"]
        no = ["Software Engineer", "Executive Assistant to the Director", "Directory Services Engineer",
              "Subdirector de Compras", "Program Manager", "Director Intern"]
        for t in yes:
            self.assertTrue(fj.is_director(t, RULES), t)
        for t in no:
            self.assertFalse(fj.is_director(t, RULES), t)

    def test_lucas_cv_rules(self):
        subs = {s["id"]: s for s in fj.get_subscribers(CFG)}
        self.assertIn("lucas", subs)
        lucas_cfg = subs["lucas"]
        cv_txt = fj.load_subscriber_cv(lucas_cfg)
        self.assertIn("Google", cv_txt)
        self.assertIn("AstraZeneca", cv_txt)
        self.assertIn("Wizeline", cv_txt)
        self.assertIn("Celonis", cv_txt)
        lucas_rules = fj.build_rules(lucas_cfg)
        yes = [
            "Director, Engineering",
            "VP, Global Operations",
            "Head of Process Excellence",
            "Senior Manager, Process Mining — GBS",
            "Sr. Manager, Data Analytics",
            "Senior Technical Program Manager",
            "Principal Process Mining Architect",
            "Global Lead, Operational Excellence",
        ]
        no = [
            "Software Engineer",
            "Executive Assistant to the Director",
            "Director Intern",
            "Program Manager",
        ]
        for t in yes:
            self.assertTrue(fj.is_director(t, lucas_rules), t)
        for t in no:
            self.assertFalse(fj.is_director(t, lucas_rules), t)

    def test_luis_cv_rules(self):
        subs = {s["id"]: s for s in fj.get_subscribers(CFG)}
        self.assertIn("luis", subs)
        luis_cfg = subs["luis"]
        self.assertEqual(luis_cfg["recipient_email"], "luisgallegosp@gmail.com")
        luis_rules = fj.build_rules(luis_cfg)
        yes = [
            "Senior Automation Consultant",
            "RCC Senior Process Analyst",
            "GBS Project Lead",
            "RPA Developer",
            "Operational Excellence Specialist",
            "Business Process Optimization Analyst",
            "Power Platform Solution Architect",
            "Digital Transformation Program Manager",
            "Agile Delivery Lead",
        ]
        no = [
            "QA Automation Engineer",
            "Senior Test Automation Engineer (SDET)",
            "Marketing Automation Specialist",
            "Industrial Automation Controls Engineer",
            "Junior Business Process Analyst",
            "Business Process Intern",
        ]
        for t in yes:
            self.assertTrue(fj.is_director(t, luis_rules), t)
        for t in no:
            self.assertFalse(fj.is_director(t, luis_rules), t)

        # Career Upgrade Move classification for Luis (distinguishes step-up from lateral IC roles)
        upgrades = [
            "Digital Transformation Program Manager",
            "Power Platform Solution Architect",
            "Senior Manager, Process Automation",
            "Director of Operational Excellence",
            "Global CoE Lead - Hyperautomation",
        ]
        laterals = [
            "RPA Developer",
            "Business Process Optimization Analyst",
            "Operational Excellence Specialist",
        ]
        for t in upgrades:
            is_up, reason = fj.is_career_upgrade_heuristic({"title": t, "company": "Acme"}, luis_cfg)
            self.assertTrue(is_up, t)
            self.assertTrue(reason)
        for t in laterals:
            is_up, _ = fj.is_career_upgrade_heuristic({"title": t, "company": "Acme"}, luis_cfg)
            self.assertFalse(is_up, t)

        # The email lists Career Upgrade Moves first
        sample_jobs = [
            {"key": "k1", "title": "RPA Developer", "company": "Beta", "location": "Guadalajara", "loc_class": "jalisco", "url": "https://example.com/1", "posted_at": "2026-11-05"},
            {"key": "k2", "title": "Senior Manager, Process Automation", "company": "Acme", "location": "Zapopan", "loc_class": "jalisco", "url": "https://example.com/2", "posted_at": "2026-11-01"},
        ]
        msg = fj.build_email_message(sample_jobs, 2, luis_cfg, date(2026, 11, 5), "sender@gmail.com")
        self.assertIn("1 Career Upgrade Move", msg["Subject"])
        self.assertIn("2 new roles for you", msg["Subject"])
        html_body = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Career Upgrade Move", html_body)
        # Upgrade role (even though older) appears before the lateral role
        self.assertLess(html_body.index("Senior Manager, Process Automation"), html_body.index("RPA Developer"))

    def test_ewerson_cv_rules(self):
        subs = {s["id"]: s for s in fj.get_subscribers(CFG)}
        self.assertIn("ewerson", subs)
        cfg = subs["ewerson"]
        self.assertEqual(cfg["recipient_email"], "ewerson.lira@live.com")
        self.assertIn("sales", cfg.get("career_goal", "").lower())
        cv_txt = fj.load_subscriber_cv(cfg)
        self.assertTrue(cv_txt.startswith("# EWERSON LIRA"))
        self.assertIn("B2B Enterprise & Digital Sales", cv_txt)
        rules = fj.build_rules(cfg)
        yes = [
            "Account Executive, Mid-Market",
            "Enterprise Account Executive - LATAM",
            "Key Account Manager (KAM)",
            "Account Manager - Enterprise",
            "Territory Sales Representative",
            "Inside Sales Representative",
            "Digital Sales Specialist - Brazil",
            "Channel & Partner Account Manager",
            "Business Development Representative, LATAM",
            "Sales Manager, Mexico",
            "Customer Success & Renewals Manager",
            "Sales Enablement Manager",
            "Senior Sales Operations Analyst",
            "Ejecutivo de Ventas Empresariales",
            "Ejecutivo de Desarrollo de Negocios",
            "Gerente Comercial Regional",
            "Ejecutivo de Cuentas Clave",
        ]
        no = [
            "Software Engineer",
            "Data Engineer",
            "Machine Learning Engineer",
            "Power BI Analyst",
            "Project Coordinator",
            "Software Asset Management Analyst",
            "QA Analyst",
            "Vice President of Sales",
            "Chief Revenue Officer",
            "Sales Intern",
            "Executive Assistant to the Sales Director",
            "Retail Store Sales Associate",
            "Promotor de Cambaceo",
            "Call Center Agent",
            "Tax Accountant",
            "Director, Fab Operations",
            "Becario Comercial",
        ]
        for t in yes:
            self.assertTrue(fj.is_director(t, rules), t)
        for t in no:
            self.assertFalse(fj.is_director(t, rules), t)

        # Closing & Sales Leadership roles should flag as Career Upgrade Moves for Ewerson
        for up_t in [
            "Enterprise Account Executive - LATAM",
            "Key Account Manager",
            "Inside Sales Manager",
            "Ejecutivo de Ventas Empresariales",
            "Gerente Comercial Regional",
        ]:
            is_up, _ = fj.is_career_upgrade_heuristic({"title": up_t, "company": "Stripe"}, cfg)
            self.assertTrue(is_up, up_t)

        # Lateral BDR role should match radar but not flag as upgrade move
        is_up_bdr, _ = fj.is_career_upgrade_heuristic(
            {"title": "Business Development Representative, LATAM", "company": "Stripe"}, cfg
        )
        self.assertFalse(is_up_bdr)

        # Deterministic CV tailoring & target direction should steer squarely toward B2B Sales
        tailored = fj.tailor_cv_deterministic(
            {"title": "Enterprise Account Executive", "company": "Stripe", "location": "Guadalajara, Mexico"},
            cv_txt,
            cfg,
        )
        self.assertTrue(tailored["is_career_upgrade"])
        self.assertIn("Enterprise & Digital Sales", tailored["cv"]["target_direction"])
        self.assertIn("B2B Enterprise Sales", tailored["cv"]["target_direction"])

    def test_monica_cv_rules(self):
        subs = {s["id"]: s for s in fj.get_subscribers(CFG)}
        self.assertIn("monica", subs)
        cfg = subs["monica"]
        self.assertEqual(cfg["recipient_email"], "monica.munoz.san@gmail.com")
        cv_txt = fj.load_subscriber_cv(cfg)
        self.assertIn("Hewlett Packard Enterprise", cv_txt)
        self.assertIn("President's Quality Award", cv_txt)
        rules = fj.build_rules(cfg)
        yes = [
            "Organizational Change Management Lead",
            "Senior Manager, Change Enablement & Digital Adoption",
            "Customer Experience (CX) Program Manager",
            "Business Process Improvement Specialist",
            "Digital Transformation Manager",
            "Global Partner Onboarding & Experience Lead",
            "Sales Enablement Manager",
            "CPQ Operations Team Lead",
            "Continuous Improvement Lead",
            "Gerente de Gestion del Cambio",
        ]
        no = [
            "Software Engineer",
            "Senior Backend Developer",
            "QA Automation Tester",
            "Change Management Intern",
            "Junior CX Analyst",
            "Executive Assistant to VP",
            "Call Center Agent",
            "Tax Accountant",
        ]
        for t in yes:
            self.assertTrue(fj.is_director(t, rules), t)
        for t in no:
            self.assertFalse(fj.is_director(t, rules), t)
        is_up, _ = fj.is_career_upgrade_heuristic(
            {"title": "Senior Manager, Organizational Change Management", "company": "Acme"}, cfg
        )
        self.assertTrue(is_up)

    def test_locations(self):
        lc = lambda s: fj.location_class(s, RULES, CFG)
        self.assertEqual(lc("Guadalajara, Mexico"), "jalisco")
        self.assertEqual(lc("Zapopan, JAL"), "jalisco")
        self.assertEqual(lc("Tonalá, Jalisco"), "jalisco")
        self.assertEqual(lc("El Salto, Mexico"), "jalisco")
        self.assertEqual(lc("San Pedro Tlaquepaque, MEX"), "jalisco")
        self.assertEqual(lc("Tlajomulco de Zúñiga"), "jalisco")
        self.assertEqual(lc("Amatitán, Mexico"), "jalisco")
        self.assertEqual(lc("Atotonilco el Alto"), "jalisco")
        self.assertEqual(lc("Lagos de Moreno, MX"), "jalisco")
        self.assertEqual(lc("Remote - Mexico"), "remote_mx")
        self.assertEqual(lc("Remote (Mexico); Austin, TX"), "remote_mx")
        self.assertEqual(lc("Mexico"), "mx_country")
        self.assertIsNone(lc("Mexico City, Mexico"))
        self.assertIsNone(lc("Monterrey, Nuevo Leon"))
        self.assertIsNone(lc("Remote - United States"))
        self.assertIsNone(lc("Remote - LATAM"))

    def test_companies_json_integrity(self):
        companies = fj.load_companies()
        self.assertGreaterEqual(len(companies), 109)
        seen_names = set()
        seen_targets = set()
        for c in companies:
            name = c.get("name", "").strip()
            ats = c.get("ats", "").strip()
            industry = c.get("industry", "").strip()
            description = c.get("description", "").strip()
            self.assertTrue(name)
            self.assertGreaterEqual(len(industry), 4, f"Missing/short industry for {name}")
            self.assertGreaterEqual(len(description), 40, f"Missing/short description for {name}")
            self.assertIn(ats, fj.FETCHERS, f"Unsupported ATS for {name}: {ats}")
            self.assertNotIn(name.lower(), seen_names, f"Duplicate company name: {name}")
            seen_names.add(name.lower())
            if ats in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "bamboohr"):
                self.assertTrue(c.get("board"), f"Missing board for {name}")
                target_id = (ats, c["board"].lower())
            elif ats == "workday":
                self.assertTrue(c.get("tenant") and c.get("wd") and c.get("site"), f"Missing Workday fields for {name}")
                target_id = (ats, c["tenant"].lower(), c["wd"].lower(), c["site"].lower())
            elif ats == "oracle_hcm":
                self.assertTrue(c.get("host") and c.get("site"), f"Missing Oracle HCM fields for {name}")
                target_id = (ats, c["host"].lower(), c["site"].lower())
            elif ats == "eightfold":
                self.assertTrue(c.get("host") and c.get("domain"), f"Missing Eightfold fields for {name}")
                target_id = (ats, c["host"].lower(), c["domain"].lower())
            self.assertNotIn(target_id, seen_targets, f"Duplicate board target for {name}: {target_id}")
            seen_targets.add(target_id)


GH = {"jobs": [
    {"id": 1, "title": "Director, Engineering", "location": {"name": "Guadalajara, Mexico"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "first_published": "2026-11-03T10:00:00-05:00"},
    {"id": 2, "title": "Software Engineer", "location": {"name": "Guadalajara, Mexico"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/2"},
    {"id": 3, "title": "Senior Director, Sales", "location": {"name": "Mexico City, Mexico"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/3"},
    {"id": 4, "title": "VP of Operations", "location": {"name": "Remote - Mexico"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/4", "first_published": "2026-09-01T10:00:00Z"},
    {"id": 5, "title": "Executive Assistant to the Director", "location": {"name": "Zapopan, Jalisco"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/5"},
    {"id": 6, "title": "Director </script><b>x</b>", "location": {"name": "Zapopan, Jalisco"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/6"},
]}
LEVER = [
    {"id": "l1", "text": "Head of Data", "categories": {"location": "Zapopan", "allLocations": ["Zapopan, Jalisco"], "team": "Data"},
     "hostedUrl": "https://jobs.lever.co/beta/l1", "createdAt": 1793700000000},   # 2026-11-03
    {"id": "l2", "text": "Director of Sales", "categories": {"location": "Austin, TX"},
     "hostedUrl": "https://jobs.lever.co/beta/l2", "createdAt": 1793700000000},
]
ASHBY = {"jobs": [
    {"id": "a1", "title": "Director, Platform", "location": "Remote", "isRemote": True,
     "address": {"postalAddress": {"addressCountry": "Mexico"}}, "department": "Eng",
     "jobUrl": "https://jobs.ashbyhq.com/gamma/a1", "publishedAt": "2026-11-04T15:00:00.000+00:00"},
    {"id": "a2", "title": "Director, Platform", "location": "Berlin", "secondaryLocations": [{"location": "Guadalajara"}],
     "jobUrl": "https://jobs.ashbyhq.com/gamma/a2", "publishedAt": "2026-10-01T15:00:00.000+00:00"},
]}
WORKABLE = {"jobs": [
    {"shortcode": "W1", "title": "General Manager", "city": "Guadalajara", "state": "Jalisco", "country": "Mexico",
     "url": "https://apply.workable.com/delta/j/W1/", "published_on": "2026-11-02"},
]}
SR = {"content": [
    {"id": "s1", "name": "Director, Finance", "releasedDate": "2026-11-01T08:00:00.000Z",
     "location": {"city": "Zapopan", "region": "Jalisco", "country": "mx"}, "department": {"label": "Finance"}},
], "totalFound": 1}


def workday(payload):
    if payload is None:
        return {
            "jobPostingInfo": {
                "location": "Guadalajara, Jalisco, Mexico",
                "additionalLocations": ["Austin, TX, USA"],
                "jobDescription": "<p>Head of Quality leadership in Guadalajara.</p>",
            }
        }
    posts = [
        {"title": "Director, Fab Operations", "externalPath": "/job/Guadalajara/Director_JR1",
         "locationsText": "Guadalajara, Mexico", "postedOn": "Posted 3 Days Ago", "bulletFields": ["JR1"]},
        {"title": "Head of Quality", "externalPath": "/job/Multi/Head_JR2",
         "locationsText": "2 Locations", "postedOn": "Posted 30+ Days Ago", "bulletFields": ["JR2"]},
        {"title": "Engineer", "externalPath": "/job/Guadalajara/Eng_JR3",
         "locationsText": "Guadalajara, Mexico", "postedOn": "Posted Today", "bulletFields": ["JR3"]},
    ]
    if payload["offset"] == 0:
        return {"total": 3, "jobPostings": posts}
    return {"total": 0, "jobPostings": []}


COMPANIES = [
    {"name": "Acme", "ats": "greenhouse", "board": "acme"},
    {"name": "Beta", "ats": "lever", "board": "beta"},
    {"name": "Gamma", "ats": "ashby", "board": "gamma"},
    {"name": "Delta", "ats": "workable", "board": "delta"},
    {"name": "Epsilon", "ats": "smartrecruiters", "board": "epsilon"},
    {"name": "Zeta", "ats": "workday", "tenant": "zeta", "wd": "wd1", "site": "External", "search_terms": ["Guadalajara"]},
]


def approve_all_gate(prompt_text, **kwargs):
    """Stand-in for the Gemini match gate: approves every job listed in the prompt (score 90)."""
    return {
        "_llm_model": "gemini-4-hydrogen",
        "assessments": [
            {
                "id": int(m.group(1)),
                "is_good_match": True,
                "match_score": 90,
                "is_career_upgrade": True,
                "match_rationale": "Approved by the test gate.",
                "industry_synergy": "",
                "matched_keywords": [],
                "upgrade_reason": "",
            }
            for m in re.finditer(r"\[JOB id=(\d+)\]", prompt_text)
        ],
    }


def make_subscriber(sub_id="quentin", **overrides):
    """A self-contained subscriber (inline CV, own rules) for tests that must not depend on the
    people listed in the real config.json or on the files in cvs/."""
    subscriber = {
        "id": sub_id,
        "name": f"{sub_id.title()} Tester",
        "recipient_email": f"{sub_id}@example.com",
        "enabled": True,
        "cv_text": (
            f"# {sub_id.upper()} TESTER\n{sub_id}@example.com\n\n"
            "## PROFESSIONAL SUMMARY\nProcess mining and automation leader.\n\n"
            "## CORE COMPETENCIES\n- **Core:** Process Mining, Intelligent Automation, Platform Operations\n"
        ),
        "career_goal": "Process mining and intelligent automation leadership.",
        "title_include": [r"\b(director(a)?|head of|vp|general manager)\b"],
        "title_exclude": [r"\bassistant\b"],
        "max_tailored_cvs": 0,
    }
    subscriber.update(overrides)
    return subscriber


def make_cfg(*subscribers, **overrides):
    """A complete config around the given synthetic subscriber(s), or one default subscriber."""
    return {
        **fj.CFG_DEFAULTS,
        "title_include": [r"\bdirector\b"],
        "title_exclude": [],
        "jalisco_locations": ["guadalajara", "zapopan", "jalisco", "tlaquepaque"],
        "subscribers": list(subscribers) or [make_subscriber()],
        **overrides,
    }


def fake_http(overrides=None):
    table = {"greenhouse.io": GH, "lever.co": LEVER, "ashbyhq.com": ASHBY,
             "workable.com": WORKABLE, "smartrecruiters.com": SR, "myworkdayjobs.com": workday}
    table.update(overrides or {})

    def fake(url, payload=None, retries=2, **kwargs):
        for key, val in table.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val(payload) if callable(val) else val
        raise AssertionError("unexpected url " + url)
    return fake


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.real = fj.http_json
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        fj.http_json = self.real
        self.tmp.cleanup()

    def run_refresh(self, today, overrides=None, send_email=False):
        fj.http_json = fake_http(overrides)
        code = fj.refresh(CFG, COMPANIES, root=self.root, today=today, send_email=send_email)
        jobs = fj.load_json(self.root / "docs" / "jobs.json", [])
        return code, {j["key"].split(":", 2)[2]: j for j in jobs}

    def test_flow(self):
        # --- run 1: baseline -------------------------------------------------
        code, jobs = self.run_refresh(date(2026, 11, 5))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(jobs), sorted(["1", "4", "6", "l1", "a1", "a2", "W1", "s1", "JR1", "JR2"]))
        self.assertTrue(jobs["1"]["is_new"])          # posted 2 days ago
        self.assertFalse(jobs["4"]["is_new"])         # posted in September
        self.assertFalse(jobs["6"]["is_new"])         # no posted date, baseline run
        self.assertTrue(jobs["W1"]["is_new"])         # posted 3 days ago, inside the 7-day window
        self.assertEqual(jobs["a1"]["loc_class"], "remote_mx")
        self.assertEqual(jobs["a2"]["loc_class"], "jalisco")
        self.assertEqual(jobs["JR2"]["loc_class"], "jalisco")  # multi-location, matched search term
        self.assertIsNone(jobs["JR2"]["posted_at"])
        self.assertEqual(jobs["JR1"]["posted_at"], "2026-11-02")
        self.assertFalse((self.root / "out" / "new_jobs.md").exists())   # baseline sends no markdown issue alert

        page = (self.root / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("</script><b>", page)        # titles can't break out of the script tag
        self.assertIn("noindex", page)

        # --- run 2: one brand-new job without a posted date ----------------------
        gh2 = {"jobs": GH["jobs"] + [{"id": 7, "title": "Director, Finance", "location": {"name": "Guadalajara"},
                                      "absolute_url": "https://boards.greenhouse.io/acme/jobs/7"}]}
        code, jobs = self.run_refresh(date(2026, 11, 6), {"greenhouse.io": gh2})
        self.assertTrue(jobs["7"]["is_new"])
        self.assertTrue(jobs["7"]["discovered_now"])
        self.assertFalse(jobs["6"]["discovered_now"])
        alert = (self.root / "out" / "new_jobs.md").read_text(encoding="utf-8")
        self.assertIn("Director, Finance", alert)
        self.assertNotIn("Fab Operations", alert)

        # --- run 3: one board fails, its roles are kept and marked stale -----------
        boom = error.HTTPError("https://api.lever.co", 404, "Not Found", {}, None)
        code, jobs = self.run_refresh(date(2026, 11, 7), {"lever.co": boom})
        self.assertEqual(code, 0)
        self.assertTrue(jobs["l1"]["stale"])
        self.assertFalse(jobs["1"].get("stale"))

        # --- run 4: everything fails, nothing is overwritten -------------------------
        before = (self.root / "docs" / "index.html").read_text(encoding="utf-8")
        down = error.URLError("offline")
        code, _ = self.run_refresh(date(2026, 11, 8), {k: down for k in
                                                       ["greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
                                                        "smartrecruiters.com", "myworkdayjobs.com"]})
        self.assertEqual(code, 1)
        self.assertEqual(before, (self.root / "docs" / "index.html").read_text(encoding="utf-8"))

    @patch("smtplib.SMTP_SSL")
    def test_email_digest_delivery(self, mock_smtp_ssl):
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        with patch.dict(os.environ, {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}):
            # Baseline run emails initial roles for Lucas when email_on_baseline=True
            code, _ = self.run_refresh(date(2026, 11, 5), send_email=True)
            self.assertEqual(code, 0)
            self.assertTrue(mock_server.send_message.called)
            sent_msg = mock_server.send_message.call_args_list[0][0][0]
            self.assertEqual(sent_msg["To"], "lucasschollmatter@gmail.com")
            self.assertIn("new roles for you", sent_msg["Subject"])
            self.assertIn("HOT", sent_msg["Subject"])
            self.assertLess(len(sent_msg["Subject"]), 120)
            html_part = sent_msg.get_body(preferencelist=("html",)).get_content()
            self.assertIn("daily reminder to check where to apply today", html_part)
            self.assertIn("Hot Off the Press", html_part)
            self.assertIn("only roles never sent to you before appear here", html_part)
            self.assertIn("Lucy", sent_msg["Subject"])
            self.assertIn("personal headhunter", html_part.lower())


    @patch("smtplib.SMTP_SSL")
    def test_multi_subscriber_preferences_and_state(self, mock_smtp_ssl):
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        multi_cfg = {
            **CFG,
            "subscribers": [
                {
                    "id": "lucas",
                    "name": "Lucas",
                    "recipient_email": "lucasschollmatter@gmail.com",
                    "enabled": True,
                    "email_on_baseline": True,
                    # Uses default director rules in Jalisco
                },
                {
                    "id": "sofia",
                    "name": "Sofia",
                    "recipient_email": "sofia@example.com",
                    "enabled": True,
                    "email_on_baseline": True,
                    # Sofia wants Software Engineer roles in Guadalajara, not Director roles
                    "title_include": [r"\bsoftware engineer\b", r"\bengineer\b"],
                    "title_exclude": [r"\bdirector\b"],
                    "locations": ["guadalajara"],
                },
                {
                    "id": "disabled_user",
                    "name": "Disabled",
                    "recipient_email": "nobody@example.com",
                    "enabled": False,
                },
            ],
        }

        fj.http_json = fake_http()
        with patch.dict(os.environ, {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}):
            code = fj.refresh(multi_cfg, COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True)
            self.assertEqual(code, 0)
            # Exactly 2 emails sent (Lucas and Sofia; disabled_user is skipped)
            self.assertEqual(mock_server.send_message.call_count, 2)

            msg_lucas = mock_server.send_message.call_args_list[0][0][0]
            msg_sofia = mock_server.send_message.call_args_list[1][0][0]

            self.assertEqual(msg_lucas["To"], "lucasschollmatter@gmail.com")
            self.assertIn("Director, Engineering", msg_lucas.get_body(preferencelist=("plain",)).get_content())
            self.assertNotIn("Software Engineer at Acme", msg_lucas.get_body(preferencelist=("plain",)).get_content())

            self.assertEqual(msg_sofia["To"], "sofia@example.com")
            self.assertIn("Software Engineer at Acme", msg_sofia.get_body(preferencelist=("plain",)).get_content())
            self.assertNotIn("Director, Engineering", msg_sofia.get_body(preferencelist=("plain",)).get_content())


    @patch("fetch_jobs._invoke_gemini_json", return_value=None)
    @patch("fetch_jobs.call_gemini_json")
    @patch("smtplib.SMTP_SSL")
    def test_llm_cv_tailoring_and_attachment(self, mock_smtp_ssl, mock_gemini, _mock_aux_gemini):
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        mock_gemini.return_value = {
            "_llm_model": "gemini-3.8-flash",
            "match_score": 94,
            "lucy_why_it_fits": "Your experience scaling engineering hubs in Guadalajara aligns directly with Acme's Director of Engineering role.",
            "matched_keywords": ["Engineering Leadership", "Guadalajara", "Cloud Velocity"],
            "gaps_to_address": ["Highlight semiconductor or hardware exposure if any"],
            "cv": {
                "full_name": "Lucas Scholl Matter",
                "target_headline": "Director of Engineering | Cloud & AI Platform Leadership",
                "contact_line": "Zapopan, Jalisco | lucasschollmatter@gmail.com",
                "summary": "Executive engineering leader driving high-velocity platform teams in Guadalajara.",
                "core_skills": ["Engineering Leadership", "Cloud Platforms", "Site Operations"],
                "experience": [
                    {
                        "company": "Global Tech",
                        "role": "Engineering & Operations Leader",
                        "location": "Guadalajara, Mexico",
                        "dates": "2020 - Present",
                        "bullets": ["Scaled engineering and data platform delivery across LatAm."]
                    }
                ],
                "education": [
                    {"degree": "B.S. Engineering", "institution": "University", "dates": "2015"}
                ]
            }
        }

        fj.http_json = fake_http()
        # The match gate fails closed, so a role is only emailed (and gets a tailored CV) after
        # the gate approved it. Approve everything here; this test is about CV tailoring.
        with patch.dict(os.environ, {
            "GMAIL_USER": "sender@gmail.com",
            "GMAIL_APP_PASSWORD": "app-secret-password",
            "GEMINI_API_KEY": "fake-gemini-key",
        }), patch("fetch_jobs.call_gemini_job_matches", side_effect=approve_all_gate):
            code = fj.refresh(CFG, COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True, subscriber_id="lucas")
            self.assertEqual(code, 0)
            self.assertTrue(mock_gemini.called)
            self.assertTrue(mock_server.send_message.called)

            sent_msg = mock_server.send_message.call_args_list[0][0][0]
            self.assertIn("3 Tailored CVs ready", sent_msg["Subject"])
            html_part = sent_msg.get_body(preferencelist=("html",)).get_content()
            self.assertIn("94% CV Match", html_part)
            self.assertIn("Lucy&rsquo;s Headhunter Take &amp; Tailored CV", html_part)

            attachments = list(sent_msg.iter_attachments())
            self.assertEqual(len(attachments), 3)  # max_tailored_cvs = 3
            self.assertTrue(attachments[0].get_filename().endswith(".docx"))
            self.assertEqual(
                attachments[0].get_content_type(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            att_text = fj.extract_docx_text(attachments[0].get_content())
            self.assertIn("Lucas Scholl Matter", att_text)
            self.assertIn("Target Role Direction:", att_text)
            self.assertIn("Director of Engineering | Cloud & AI Platform Leadership", att_text)
            # Tailored CVs are kept as run artifacts only; nothing is written to the published docs/ folder
            cv_dir = self.root / "out" / "cvs"
            self.assertEqual(len(list(cv_dir.glob("*.docx"))), 3)
            self.assertEqual(len(list(cv_dir.glob("*.pdf"))), 3)
            html_cvs = list(cv_dir.glob("*.html"))
            self.assertEqual(len(html_cvs), 3)
            self.assertIn("Gemini AI &middot; gemini-3.8-flash", html_cvs[0].read_text(encoding="utf-8"))
            self.assertFalse((self.root / "docs" / "cvs").exists())

    @patch("fetch_jobs._invoke_gemini_json", side_effect=RuntimeError("gemini 429 quota"))
    @patch("smtplib.SMTP_SSL")
    def test_llm_fallback_still_sends_tailored_cvs(self, mock_smtp_ssl, _mock_gemini):
        """Even if Gemini throws (429/503/JSON error) while tailoring, the deterministic fallback still
        builds & attaches 3 CVs for roles the match gate approved."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        fj.http_json = fake_http()
        # The gate itself answers here (it fails closed otherwise, see the outage test below);
        # every other Gemini call (CV tailoring, briefing) raises.
        with patch.dict(os.environ, {
            "GMAIL_USER": "sender@gmail.com",
            "GMAIL_APP_PASSWORD": "app-secret-password",
            "GEMINI_API_KEY": "fake-gemini-key",
        }), patch("fetch_jobs.call_gemini_job_matches", side_effect=approve_all_gate):
            code = fj.refresh(CFG, COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True, subscriber_id="lucas")
            self.assertEqual(code, 0)
            sent_msg = mock_server.send_message.call_args_list[0][0][0]
            attachments = list(sent_msg.iter_attachments())
            self.assertEqual(len(attachments), 3)
            self.assertTrue(attachments[0].get_filename().endswith(".docx"))
            att_text = fj.extract_docx_text(attachments[0].get_content())
            self.assertIn("Lucas Scholl Matter", att_text)
            self.assertIn("Target Role Direction:", att_text)
            self.assertIn("Google", att_text)
            self.assertIn("AstraZeneca", att_text)
            pdf_files = list((self.root / "out" / "cvs").glob("*.pdf"))
            self.assertTrue(pdf_files[0].read_bytes().startswith(b"%PDF-1.4"))
            html_cvs = list((self.root / "out" / "cvs").glob("*.html"))
            self.assertIn("Deterministic ATS Fallback", html_cvs[0].read_text(encoding="utf-8"))
            self.assertFalse((self.root / "docs" / "cvs").exists())

    def test_workday_dates(self):
        t = date(2026, 11, 5)
        self.assertEqual(fj.workday_posted("Posted Today", t), "2026-11-05")
        self.assertEqual(fj.workday_posted("Posted Yesterday", t), "2026-11-04")
        self.assertEqual(fj.workday_posted("Posted 3 Days Ago", t), "2026-11-02")
        self.assertIsNone(fj.workday_posted("Posted 30+ Days Ago", t))

    def test_oracle_hcm_eightfold_and_workday_fallbacks(self):
        def router(url, payload=None, retries=2):
            if "myworkdayjobs.com/wday/cxs/acme/OldSite/jobs" in url:
                raise error.HTTPError(url, 404, "Not Found", {}, None)
            if "myworkdayjobs.com/wday/cxs/acme/NewSite/jobs" in url:
                return {"total": 1, "jobPostings": [
                    {"title": "Director, Plant Ops", "externalPath": "/job/El-Salto/Dir_JR9",
                     "locationsText": "El Salto, Jalisco", "postedOn": "Posted Today", "bulletFields": ["JR9"]}
                ]}
            if "oraclecloud.com" in url and "siteNumber=CX_BAD" in url:
                raise error.HTTPError(url, 404, "Not Found", {}, None)
            if "oraclecloud.com" in url and "recruitingCEJobRequisitions?" in url:
                return {"items": [{"TotalJobsCount": 1, "requisitionList": [{
                    "Id": "26001",
                    "Title": "Director, Cloud Engineering",
                    "PrimaryLocation": "Zapopan, Jalisco, Mexico",
                    "secondaryLocations": [{"Name": "Guadalajara, Mexico"}],
                    "PostedDate": "2026-11-04",
                    "WorkplaceType": "Hybrid",
                    "Department": "OCI",
                }]}]}
            if "oraclecloud.com" in url and "recruitingCEJobRequisitionDetails" in url:
                return {"items": [{"ExternalDescriptionStr": "<p>Lead OCI Zapopan cloud engineering.</p>"}]}
            if "qualcomm.eightfold.ai/api/pcsx/search" in url:
                return {"data": {"count": 1, "positions": [{
                    "id": "ef101",
                    "name": "Director, Silicon Validation",
                    "locations": ["Guadalajara, Jalisco, Mexico"],
                    "department": "Hardware",
                    "postedTs": 1793750400,  # epoch seconds -> 2026-11-04
                    "workLocationOption": "hybrid",
                    "positionUrl": "/careers/job/ef101",
                }]}}
            if "qualcomm.eightfold.ai/api/pcsx/position_details" in url:
                return {"data": {"jobDescription": "<div>Silicon validation leadership in GDL.</div>"}}
            if "legacy.eightfold.ai/api/pcsx/search" in url:
                raise error.HTTPError(url, 403, "Forbidden", {}, None)
            if "legacy.eightfold.ai/api/apply/v2/jobs" in url:
                return {"count": 1, "positions": [{
                    "id": "leg1",
                    "name": "Head of Supply Chain",
                    "locations": ["Tlajomulco, Jalisco"],
                    "t_create": 1793750400,
                    "job_description": "Supply chain leadership.",
                }]}
            raise AssertionError("unexpected url " + url)

        fj.http_json = router
        wd_jobs = fj.fetch_workday(
            {"name": "AcmeWD", "ats": "workday", "tenant": "acme", "wd": "wd5",
             "site": "OldSite", "fallback_sites": ["NewSite"], "search_terms": ["El Salto"]},
            today=date(2026, 11, 5),
        )
        self.assertEqual(len(wd_jobs), 1)
        self.assertEqual(wd_jobs[0]["posted_at"], "2026-11-05")

        ora_jobs = fj.fetch_oracle_hcm({
            "name": "Oracle", "ats": "oracle_hcm", "host": "eeho.fa.us2.oraclecloud.com",
            "site": "CX_BAD", "fallback_sites": ["CX_1001"], "search_terms": ["Zapopan"],
        })
        self.assertEqual(len(ora_jobs), 1)
        self.assertIn("Zapopan", ora_jobs[0]["location"])
        self.assertIn("OCI", fj.enrich_job_description(ora_jobs[0]))

        ef_jobs = fj.fetch_eightfold({
            "name": "Qualcomm", "ats": "eightfold", "host": "qualcomm.eightfold.ai", "domain": "qualcomm.com",
        })
        self.assertEqual(len(ef_jobs), 1)
        self.assertEqual(ef_jobs[0]["posted_at"], "2026-11-04")
        self.assertIn("Silicon validation", fj.enrich_job_description(ef_jobs[0]))

        leg_jobs = fj.fetch_eightfold({
            "name": "LegacyEF", "ats": "eightfold", "host": "legacy.eightfold.ai", "domain": "legacy.com",
        })
        self.assertEqual(len(leg_jobs), 1)
        self.assertEqual(leg_jobs[0]["title"], "Head of Supply Chain")

    @patch("fetch_jobs.call_gemini_daily_motivation")
    def test_daily_insider_motivation_genai_and_vault(self, mock_daily_mot):
        """Daily 'Us vs. The System' motivation renders via Gemini when available and falls back cleanly."""
        sub = {"name": "Luis Gallegos", "recipient_email": "luisgallegosp@gmail.com"}
        sample_jobs = [
            {"key": "k1", "title": "Senior Manager, Process Automation", "company": "Acme", "location": "Zapopan", "loc_class": "jalisco", "url": "https://example.com/2", "posted_at": "2026-11-05"},
        ]
        mock_daily_mot.return_value = (
            "Refuse the 3% Loyalty Tax.",
            "Luis, staying comfortable keeps your comp flat while moving seats reprices your career—let's beat the ATS at Acme today.",
        )
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}):
            msg = fj.build_email_message(sample_jobs, 1, sub, date(2026, 11, 5), "sender@gmail.com")
        self.assertTrue(mock_daily_mot.called)
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Us vs. The System", html_part)
        self.assertIn("Refuse the 3% Loyalty Tax.", html_part)
        self.assertIn("Good morning, Luis.", html_part)

        # Fallback vault when GEMINI_API_KEY is unset
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            headline, body = fj.generate_daily_insider_note(sub, sample_jobs[0], 1, 1, 1, date(2026, 11, 5))
            self.assertTrue(headline)
            self.assertIn("Luis", body)

    def test_ai_partner_briefing_prompt_quality_gate_and_continuity(self):
        """Lucy's daily briefing is AI-first: the prompt is grounded in real overnight facts, the
        spotlight employer's ATS, the candidate's real craft and the story so far; every draft passes
        a quality gate (forbidden dishonest vocabulary, placeholders, near-duplicate headlines) with
        exactly one revision retry; the email renders the chapter, the inspiration and the 60-second
        play; memory persists."""
        sub_luis = next(s for s in fj.get_subscribers(CFG) if s["id"] == "luis")
        top_job = {
            "key": "workday:Flex:JR1", "ats": "workday", "title": "Automation Program Lead", "company": "Flex",
            "location": "Zapopan, Jalisco, Mexico", "loc_class": "jalisco", "url": "https://flex.wd1.myworkdayjobs.com/x/JR1",
            "posted_at": "2026-11-08", "cv_filename": "CV_Luis_Gallegos_Flex_Automation_Program_Lead.docx",
        }
        sub_state = {"relationship": {
            "edition": 6, "streak_days": 6, "last_briefing_date": "2026-11-07",
            "total_roles_surfaced": 40, "total_cvs_tailored": 12,
            "recent_headlines": ["Their Robot Is Now Our Doorman"],
            "recent_story_beats": ["Ed. #6 (2026-11-07): Lucy framed 'Speed Is A Status Signal' around HPE and asked Luis for one application."],
            "last_micro_challenge": "Open HPE's portal and upload the attached CV before 10 AM.",
            "last_spotlight_company": "HPE",
        }}
        rel_ctx = fj.peek_relationship_context(sub_state, 3, 2, date(2026, 11, 8), board_count=109, sub_id="luis")
        self.assertEqual(rel_ctx["edition"], 7)
        self.assertEqual(rel_ctx["last_spotlight_company"], "HPE")

        # 1. The prompt is fully grounded (facts, ATS intel, craft, continuity, avoid-list, arc, language)
        snapshot = fj.extract_cv_identity_snapshot(sub_luis)
        self.assertIn("Identity:", snapshot)
        self.assertIn("Current chapter:", snapshot)
        self.assertNotIn("@", snapshot)
        prompt = fj.compose_partner_briefing_prompt(
            sub_luis, top_job, rel_ctx, date(2026, 11, 8),
            fj.PARTNER_STORY_BEATS[rel_ctx["angle_id"]], fj.WEEKDAY_CADENCE[date(2026, 11, 8).weekday()],
            snapshot, 2, 1, 3, 2, False, language="English",
        )
        for needle in (
            "Chapter II: Building the Habit", "Briefing edition together: #7", "Spotlight hiring employer: 'Flex'",
            "Their screening tool for this role: Workday", "CV_Luis_Gallegos_Flex_Automation_Program_Lead.docx",
            "Yesterday's spotlight employer: HPE", "Yesterday's micro-challenge:", "Speed Is A Status Signal",
            "RECENT_HEADLINES_TO_AVOID: 'Their Robot Is Now Our Doorman'", "Language to write in: English",
            "109 across 9 ATS platforms", "First name: Luis",
        ):
            self.assertIn(needle, prompt)
        self.assertIn("Us vs. Their Machine", fj.DAILY_MOTIVATION_SYSTEM_PROMPT)
        self.assertIn("FORBIDDEN vocabulary", fj.DAILY_MOTIVATION_SYSTEM_PROMPT)
        self.assertEqual(len(fj.PARTNER_STORY_BEATS), len(fj.DAILY_INSIDER_VAULT))

        # 2. Quality gate: dishonest vocabulary, placeholders and near-duplicate headlines are rejected
        good = {
            "subject_spark": "Flex Just Blinked", "chapter_title": "The Doorman Switches Sides",
            "headline": "Flex Built A Gate. We Brought The Key.",
            "message": "Luis, Flex runs its screening through an automated parser that ranks structure and keywords long before a human reads a name. Last night we rebuilt your automation and process-excellence record in that exact dialect, so the machine that was designed to say no now opens the door. Your Word CV is attached; the gate is already half open.",
            "their_ai_vs_our_ai": "Workday parses your CV into fields and recruiters filter by exact skill terms; your attached CV uses a single clean column with your real RPA and Power Platform vocabulary so it parses perfectly.",
            "connection_ps": "Seventh morning in a row together, Luis: 109 portals swept, HPE yesterday, Flex today, and I will keep the radar warm.",
            "micro_challenge": "Open the Flex posting, upload CV_Luis_Gallegos_Flex_Automation_Program_Lead.docx, and hit submit before your first meeting.",
            "partner_signoff": "Shoulder to shoulder. Lucy",
            "story_beat": "Lucy framed Flex's parser as a doorman and sent Luis after the Automation Program Lead role.",
        }
        ok, _ = fj._briefing_pack_quality(good, "Luis", rel_ctx["recent_headlines"])
        self.assertTrue(ok)
        bad_vocab = dict(good, message=good["message"] + " We hack their system every night.")
        ok, reason = fj._briefing_pack_quality(bad_vocab, "Luis", rel_ctx["recent_headlines"])
        self.assertFalse(ok)
        self.assertIn("forbidden vocabulary", reason)
        ok, reason = fj._briefing_pack_quality(dict(good, headline="Their Robot Is Now Our Doorman"), "Luis", rel_ctx["recent_headlines"])
        self.assertFalse(ok)
        ok, reason = fj._briefing_pack_quality(dict(good, connection_ps="Hello {first_name}"), "Luis", [])
        self.assertFalse(ok)
        self.assertIn("template braces", reason)

        # 3. One revision round: first draft uses forbidden vocabulary -> rejected -> revised draft accepted
        calls = []

        def scripted_gemini(prompt_text, **kwargs):
            calls.append(prompt_text)
            if len(calls) == 1:
                return dict(good, headline="We Cheat Their Filter Daily")
            return dict(good)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "valid-key"}), patch("fetch_jobs._invoke_gemini_json", side_effect=scripted_gemini):
            pack = fj.generate_daily_briefing_pack(sub_luis, top_job, 2, 1, 3, date(2026, 11, 8), rel_ctx=rel_ctx, cvs_today_count=2)
        self.assertEqual(len(calls), 2)
        self.assertIn("REVISION NOTE", calls[1])
        self.assertIn("forbidden vocabulary", calls[1])
        self.assertEqual(pack["briefing_source"], "gemini")
        self.assertTrue(pack["briefing_revised"])
        self.assertEqual(pack["headline"], good["headline"])
        self.assertEqual(pack["chapter_title"], "The Doorman Switches Sides")
        self.assertEqual(pack["partner_signoff"], "Shoulder to shoulder. Lucy")
        self.assertEqual(pack["spotlight_company"], "Flex")

        # 4. The email shows two things from the pack, the inspiration (headline + message) and the
        #    60-second play, under the chapter label. The intel, Lucy's connection note and the
        #    sign-off are still written into the pack but are not shown (HTML + plain text)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "valid-key"}), patch("fetch_jobs._invoke_gemini_json", return_value=dict(good)):
            msg = fj.build_email_message([dict(top_job)], 3, sub_luis, date(2026, 11, 8), "sender@gmail.com", sub_state=sub_state, board_count=109)
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        plain_part = msg.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Chapter 7: The Doorman Switches Sides", html_part)
        self.assertIn("Flex Built A Gate. We Brought The Key.", html_part)
        self.assertEqual(html_part.count(fj.html.escape(good["message"])), 2)      # hidden preheader + Lucy's card
        self.assertIn("Today&rsquo;s 60-second play", html_part)
        self.assertIn("Open the Flex posting, upload", html_part)
        self.assertIn("Us vs. Their Machine · Chapter 7", plain_part)
        self.assertIn(good["message"], plain_part)
        self.assertIn("Today's 60-second play: " + good["micro_challenge"], plain_part)
        for part in (html_part, plain_part):
            for dropped in ("Their AI vs. Our AI", "Corner:", "Shoulder to shoulder",
                            good["their_ai_vs_our_ai"][:40], good["connection_ps"][:40]):
                self.assertNotIn(dropped, part)
        self.assertIn("Flex Just Blinked", html_part)          # subject spark lives in the preheader
        self.assertIn("1 new role for you · Flex", msg["Subject"])

        # 5. Continuity memory persists story beats, last micro-challenge and last spotlight employer
        fj.commit_relationship_state(sub_state, rel_ctx, pack, [top_job], date(2026, 11, 8))
        rel = sub_state["relationship"]
        self.assertEqual(rel["edition"], 7)
        self.assertEqual(rel["last_spotlight_company"], "Flex")
        self.assertEqual(rel["last_briefing_source"], "gemini")
        self.assertTrue(rel["recent_story_beats"][-1].startswith("Ed. #7 (2026-11-08):"))
        self.assertIn("doorman", rel["recent_story_beats"][-1])
        self.assertIn(good["headline"], rel["recent_headlines"])

        # 6. Two rejected drafts (or Gemini outage) fall back to the deterministic vault, never crash
        with patch.dict(os.environ, {"GEMINI_API_KEY": "valid-key"}), patch("fetch_jobs._invoke_gemini_json", return_value=dict(good, message="We hack and cheat " * 6)):
            vault_pack = fj.generate_daily_briefing_pack(sub_luis, top_job, 2, 1, 3, date(2026, 11, 8), rel_ctx=rel_ctx, cvs_today_count=2)
        self.assertEqual(vault_pack["briefing_source"], "vault")
        self.assertIn("Luis", vault_pack["message"])
        self.assertIn("Workday", vault_pack["their_ai_vs_our_ai"])
        self.assertTrue(vault_pack["partner_signoff"].endswith("Lucy"))

    def test_30_days_unique_dopamine_messages_and_anti_repeat(self):
        """Every single day across 30 consecutive days—and even multiple runs on the same day—
        produces a 100% distinct headline, message, subject spark, and partnership note."""
        from datetime import timedelta
        sub = {"id": "monica", "name": "Mónica Muñoz", "recipient_email": "monica.munoz.san@gmail.com", "cv_file": "cvs/monica.md"}
        sample_job = {"key": "k1", "title": "Senior Manager, Change Management", "company": "HPE", "location": "Zapopan", "loc_class": "jalisco", "url": "https://example.com/1", "posted_at": "2026-11-05"}
        sub_state = {}
        seen_headlines = set()
        seen_messages = set()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            start = date(2026, 11, 1)
            for d in range(30):
                cur_day = start + timedelta(days=d)
                rel_ctx = fj.peek_relationship_context(sub_state, 3, 2, cur_day, board_count=106)
                pack = fj.generate_daily_briefing_pack(sub, sample_job, 2, 1, 3, cur_day, rel_ctx=rel_ctx, cvs_today_count=2)
                self.assertNotIn(pack["headline"], seen_headlines, f"Repeated headline on day {d + 1}")
                self.assertNotIn(pack["message"], seen_messages, f"Repeated message on day {d + 1}")
                seen_headlines.add(pack["headline"])
                seen_messages.add(pack["message"])
                fj.commit_relationship_state(sub_state, rel_ctx, pack, [sample_job], cur_day)

        self.assertEqual(len(seen_headlines), 30)
        self.assertEqual(sub_state["relationship"]["edition"], 30)
        self.assertEqual(sub_state["relationship"]["streak_days"], 30)
        self.assertEqual(sub_state["relationship"]["total_roles_surfaced"], 90)
        self.assertEqual(sub_state["relationship"]["total_cvs_tailored"], 60)

        # Even 3 runs on the EXACT same day produce 3 completely different angles/headlines
        same_day_state = {}
        same_day_headlines = set()
        for _ in range(3):
            rel_ctx = fj.peek_relationship_context(same_day_state, 1, 1, date(2026, 11, 8))
            pack = fj.generate_daily_briefing_pack(sub, sample_job, 1, 1, 1, date(2026, 11, 8), rel_ctx=rel_ctx, cvs_today_count=1)
            same_day_headlines.add(pack["headline"])
            fj.commit_relationship_state(same_day_state, rel_ctx, pack, [sample_job], date(2026, 11, 8))
        self.assertEqual(len(same_day_headlines), 3)

    @patch("smtplib.SMTP_SSL")
    def test_relationship_streak_and_quiet_day_note(self, mock_smtp_ssl):
        """When zero never-seen postings arrive overnight, Lucy sends a short role-free quiet-day
        note (nothing is re-sent), still incrementing Edition & Streak; legacy spotlight and skip
        modes remain available via `quiet_day_mode`."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}

        def last_lucas_msg():
            return [c[0][0] for c in mock_server.send_message.call_args_list if c[0][0]["To"] == "lucasschollmatter@gmail.com"][-1]

        # Day 1 (Nov 5): Baseline -> Briefing #1 + Welcome milestone, roles listed once
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 5), send_email=True)
        self.assertEqual(code, 0)
        msg1 = last_lucas_msg()
        self.assertIn("Lucy #1:", msg1["Subject"])
        html1 = msg1.get_body(preferencelist=("html",)).get_content()
        plain1 = msg1.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Briefing #1", html1)
        self.assertIn("Today&rsquo;s 60-second play", html1)      # Lucy's card: the inspiration plus one action...
        self.assertNotIn("Corner:", html1)                         # ...and no separate "Lucy's Corner" note any more
        self.assertIn("Welcome to Your Unfair Advantage", html1)
        self.assertIn("- [CAREER UPGRADE MOVE]", plain1)

        # Day 2 (Nov 6): Zero new postings -> Quiet-day note (#2, 2d Streak, NO roles re-sent)
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 6), send_email=True)
        self.assertEqual(code, 0)
        msg2 = last_lucas_msg()
        self.assertIn("Lucy #2:", msg2["Subject"])
        self.assertIn("Quiet day", msg2["Subject"])
        self.assertIn("0 new roles", msg2["Subject"])
        html2 = msg2.get_body(preferencelist=("html",)).get_content()
        plain2 = msg2.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("2d Streak", html2)
        self.assertIn("Quiet day on the radar", html2)
        self.assertNotIn("Director, Engineering at Acme", plain2)
        self.assertNotIn("\n- ", plain2)                       # zero job bullet lines
        self.assertNotIn("Quiet-Market Momentum Spotlight", html2)

        # Day 3 (Nov 7): Another quiet day -> Briefing #3 (3d Streak + 3-Briefing Spark), still no roles
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 7), send_email=True)
        self.assertEqual(code, 0)
        msg3 = last_lucas_msg()
        self.assertIn("Lucy #3:", msg3["Subject"])
        html3 = msg3.get_body(preferencelist=("html",)).get_content()
        plain3 = msg3.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("3d Streak", html3)
        self.assertIn("3-Briefing Spark", html3)
        self.assertNotIn("\n- ", plain3)
        self.assertNotEqual(plain2, plain3)                   # Lucy's note still changes every day

        # Legacy mode: quiet_day_mode=spotlight re-lists up to 3 unclaimed roles with the spotlight banner
        spotlight_cfg = {**CFG, "quiet_day_mode": "spotlight",
                         "subscribers": [{**s, "quiet_day_mode": "spotlight"} for s in CFG["subscribers"]]}
        fj.http_json = fake_http()
        with patch.dict(os.environ, env):
            code = fj.refresh(spotlight_cfg, COMPANIES, root=self.root, today=date(2026, 11, 8), send_email=True, subscriber_id="lucas")
        self.assertEqual(code, 0)
        msg4 = mock_server.send_message.call_args_list[-1][0][0]
        html4 = msg4.get_body(preferencelist=("html",)).get_content()
        plain4 = msg4.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Quiet-Market Momentum Spotlight", html4)
        self.assertEqual(plain4.count("\n- "), 3)

        # skip mode: no email at all on a quiet day
        skip_cfg = {**CFG, "subscribers": [{**s, "quiet_day_mode": "skip"} for s in CFG["subscribers"]]}
        sent_before = mock_server.send_message.call_count
        with patch.dict(os.environ, env):
            code = fj.refresh(skip_cfg, COMPANIES, root=self.root, today=date(2026, 11, 9), send_email=True, subscriber_id="lucas")
        self.assertEqual(code, 0)
        self.assertEqual(mock_server.send_message.call_count, sent_before)

    @patch("smtplib.SMTP_SSL")
    def test_only_never_sent_roles_are_emailed_with_cap_and_carry_over(self, mock_smtp_ssl):
        """A role is emailed to a subscriber exactly once: not again the next day, not as a clone
        under a new requisition id, and manual --email-all stays the only way to re-send. Large
        drops are capped at `max_email_jobs` with carry-over + a personal board attachment."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}

        def lucas_msgs():
            return [c[0][0] for c in mock_server.send_message.call_args_list if c[0][0]["To"] == "lucasschollmatter@gmail.com"]

        # Day 1 baseline: every current match is delivered once
        with patch.dict(os.environ, env):
            self.run_refresh(date(2026, 11, 5), send_email=True)
        plain1 = lucas_msgs()[-1].get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Director, Engineering at Acme", plain1)
        seen = fj.load_json(self.root / "state" / "seen.json", {})
        ledger = seen["subscribers"]["lucas"]["emailed"]
        self.assertIn("greenhouse:Acme:1", ledger)
        self.assertEqual(ledger["greenhouse:Acme:1"]["date"], "2026-11-05")

        # Day 2: a CLONE of an already-sent role (same company + title, new requisition id) must NOT be re-sent
        gh_clone = {"jobs": GH["jobs"] + [{"id": 8, "title": "Director, Engineering", "location": {"name": "Zapopan, Jalisco"},
                                           "absolute_url": "https://boards.greenhouse.io/acme/jobs/8"}]}
        with patch.dict(os.environ, env):
            self.run_refresh(date(2026, 11, 6), {"greenhouse.io": gh_clone}, send_email=True)
        msg2 = lucas_msgs()[-1]
        self.assertIn("Quiet day", msg2["Subject"])
        self.assertNotIn("Director, Engineering", msg2.get_body(preferencelist=("plain",)).get_content())

        # Day 3: one genuinely new role -> the email lists ONLY that role
        gh_new = {"jobs": gh_clone["jobs"] + [{"id": 9, "title": "Director, Finance", "location": {"name": "Guadalajara"},
                                               "absolute_url": "https://boards.greenhouse.io/acme/jobs/9"}]}
        with patch.dict(os.environ, env):
            self.run_refresh(date(2026, 11, 7), {"greenhouse.io": gh_new}, send_email=True)
        msg3 = lucas_msgs()[-1]
        plain3 = msg3.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("1 new role for you · Acme", msg3["Subject"])
        self.assertEqual(plain3.count("\n- "), 1)
        self.assertIn("Director, Finance at Acme", plain3)
        self.assertNotIn("Director, Engineering at Acme", plain3)

        # Day 4: same feed again -> quiet day, the Finance role is NOT repeated
        with patch.dict(os.environ, env):
            self.run_refresh(date(2026, 11, 8), {"greenhouse.io": gh_new}, send_email=True)
        msg4 = lucas_msgs()[-1]
        self.assertIn("Quiet day", msg4["Subject"])
        self.assertNotIn("Director, Finance", msg4.get_body(preferencelist=("plain",)).get_content())

        # Manual --email-all (email_mode=all) remains the explicit way to re-send everything
        all_cfg = {**CFG, "email_mode": "all", "subscribers": [{**s, "email_mode": "all"} for s in CFG["subscribers"]]}
        fj.http_json = fake_http({"greenhouse.io": gh_new})
        with patch.dict(os.environ, env):
            fj.refresh(all_cfg, COMPANIES, root=self.root, today=date(2026, 11, 9), send_email=True, subscriber_id="lucas")
        msg5 = lucas_msgs()[-1]
        self.assertIn("matching roles", msg5["Subject"])
        self.assertIn("Director, Finance at Acme", msg5.get_body(preferencelist=("plain",)).get_content())

        # Cap + carry-over + personal board: max_email_jobs=3 on a fresh subscriber state
        cap_root = Path(self.tmp.name) / "cap_root"
        cap_root.mkdir()
        cap_cfg = {**CFG, "max_email_jobs": 3, "subscribers": [{**s, "max_email_jobs": 3} for s in CFG["subscribers"]]}
        fj.http_json = fake_http()
        with patch.dict(os.environ, env):
            fj.refresh(cap_cfg, COMPANIES, root=cap_root, today=date(2026, 11, 5), send_email=True, subscriber_id="lucas")
        cap_msg = lucas_msgs()[-1]
        cap_plain = cap_msg.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(cap_plain.count("\n- "), 3)
        self.assertIn("more never-sent role(s) are waiting on your personal board", cap_plain)
        board_atts = [a for a in cap_msg.iter_attachments() if a.get_filename().startswith("Lucy_Board_lucas_")]
        self.assertEqual(len(board_atts), 1)
        self.assertIn("Lucy Personal Board", board_atts[0].get_content())
        # Next day: nothing new posted, but the carried-over roles are delivered (not a quiet day)
        with patch.dict(os.environ, env):
            fj.refresh(cap_cfg, COMPANIES, root=cap_root, today=date(2026, 11, 6), send_email=True, subscriber_id="lucas")
        carry_msg = lucas_msgs()[-1]
        carry_plain = carry_msg.get_body(preferencelist=("plain",)).get_content()
        self.assertNotIn("Quiet day", carry_msg["Subject"])
        self.assertEqual(carry_plain.count("\n- "), 3)
        for line in [ln for ln in cap_plain.splitlines() if ln.startswith("- ")]:
            self.assertNotIn(line, carry_plain)                # no overlap with day 1

        # Legacy state without an 'emailed' ledger is seeded so nothing old is re-sent after upgrading
        legacy_state = {"initialized": True, "jobs": {"greenhouse:Acme:1": {"first_seen": "2026-10-01", "last_seen": "2026-11-05"}}}
        self.assertIn("greenhouse:Acme:1", fj.ensure_emailed_state(legacy_state))
        self.assertTrue(legacy_state["emailed"]["greenhouse:Acme:1"]["legacy"])

    @patch("smtplib.SMTP_SSL")
    def test_email_failure_rolls_back_subscriber_state(self, mock_smtp_ssl):
        """If SMTP blows up, exit code is 2, outputs are still written, and the
        subscriber's seen-state is NOT advanced, so the same roles are emailed next run."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}

        # run 1: baseline, delivery works -> state for 'lucas' is persisted
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 5), send_email=True)
        self.assertEqual(code, 0)
        seen1 = fj.load_json(self.root / "state" / "seen.json", {})
        self.assertIn("lucas", seen1["subscribers"])

        # run 2: a brand-new role appears but SMTP fails
        gh2 = {"jobs": GH["jobs"] + [{"id": 7, "title": "Director, Finance", "location": {"name": "Guadalajara"},
                                      "absolute_url": "https://boards.greenhouse.io/acme/jobs/7"}]}
        mock_server.send_message.side_effect = OSError("smtp down")
        with patch.dict(os.environ, env):
            code, jobs = self.run_refresh(date(2026, 11, 6), {"greenhouse.io": gh2}, send_email=True)
        self.assertEqual(code, 2)
        self.assertIn("7", jobs)                                    # main board still updated
        seen2 = fj.load_json(self.root / "state" / "seen.json", {})
        self.assertNotIn("greenhouse:Acme:7", seen2["subscribers"]["lucas"]["jobs"])   # rolled back
        self.assertIn("greenhouse:Acme:7", seen2["jobs"])                               # global state advanced

        # run 3: SMTP is back -> the role is re-discovered for lucas and finally emailed
        mock_server.send_message.side_effect = None
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 7), {"greenhouse.io": gh2}, send_email=True)
        self.assertEqual(code, 0)
        sent = [c[0][0] for c in mock_server.send_message.call_args_list if c[0][0]["To"] == "lucasschollmatter@gmail.com"]
        self.assertIn("Director, Finance", sent[-1].get_body(preferencelist=("plain",)).get_content())

    def test_bamboohr_fetcher_and_detail_enrichment(self):
        fj.http_json = fake_http({
            "bitso.bamboohr.com/careers/list": {
                "meta": {"totalCount": 1},
                "result": [{
                    "id": "1002",
                    "jobOpeningName": "Enterprise Account Executive (B2B)",
                    "departmentLabel": "Bitso Business",
                    "location": {"city": "Guadalajara", "state": "Jalisco"},
                    "atsLocation": {"country": "Mexico"},
                    "isRemote": True,
                }],
            },
            "bitso.bamboohr.com/careers/1002/detail": {
                "result": {
                    "jobOpening": {
                        "description": "<p>Drive B2B enterprise crypto payment sales across LATAM.</p>"
                    }
                }
            },
        })
        c = {"name": "Bitso", "ats": "bamboohr", "board": "bitso", "default_country": "Mexico"}
        jobs = fj.fetch_bamboohr(c)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Enterprise Account Executive (B2B)")
        self.assertIn("Guadalajara", jobs[0]["location"])
        self.assertIn("Remote", jobs[0]["location"])
        desc = fj.enrich_job_description(jobs[0])
        self.assertIn("Drive B2B enterprise crypto payment sales", desc)

    def test_gemini_4_hydrogen_free_tier_default_cascade_and_auto_discovery(self):
        self.assertEqual(CFG["llm_model"], "gemini-4-hydrogen")
        self.assertEqual(fj.DEFAULT_LLM_MODEL, "gemini-4-hydrogen")
        self.assertEqual(
            fj.FALLBACK_LLM_MODELS,
            ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest"),
        )
        self.assertNotIn("gemini-3.8-flash-lite", fj.FALLBACK_LLM_MODELS)

        fj._DEAD_GEMINI_MODELS.clear()
        fj._DISCOVERED_GEMINI_MODELS = None

        calls = []

        def mock_gemini_http(url, payload=None, retries=2, headers=None, timeout=60):
            calls.append((url, payload))
            if url.endswith("/v1beta/models"):
                return {
                    "models": [
                        {"name": "models/gemini-4.2-flash-tts", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-4.2-hydrogen", "supportedGenerationMethods": ["generateContent"]},
                    ]
                }
            if "gemini-4.2-hydrogen:generateContent" in url:
                return {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": '{"match_score": 95, "cv": {"full_name": "Lucas Matter"}}'}]
                        }
                    }]
                }
            raise error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

        fj.http_json = mock_gemini_http
        try:
            out = fj.call_gemini_json("test prompt", model="models/gemini-2.5-flash", api_key="test-key")
            self.assertEqual(out["match_score"], 95)
            # First call normalized 'models/gemini-2.5-flash' -> '/models/gemini-2.5-flash:generateContent'
            self.assertIn("/v1beta/models/gemini-2.5-flash:generateContent", calls[0][0])
            # Cascaded through gemini-4-hydrogen with Gemini 4 API compliance:
            # no temperature field, maxOutputTokens >= 16384, thinkingLevel == "low"
            c4 = [p for u, p in calls if "gemini-4-hydrogen:generateContent" in u][0]
            self.assertNotIn("temperature", c4["generationConfig"])
            self.assertGreaterEqual(c4["generationConfig"]["maxOutputTokens"], 16384)
            self.assertEqual(c4["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
            # Also tried gemini-3.8-flash and gemini-3.5-flash-lite
            c38 = [p for u, p in calls if "gemini-3.8-flash:generateContent" in u][0]
            self.assertEqual(c38["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
            self.assertTrue(any("gemini-3.5-flash-lite:generateContent" in u for u, _ in calls))
            # Auto-discovered gemini-4.2-hydrogen (skipping -tts) and succeeded
            self.assertTrue(any("gemini-4.2-hydrogen:generateContent" in u for u, _ in calls))
            self.assertIn("gemini-2.5-flash", fj._DEAD_GEMINI_MODELS)
        finally:
            fj._DEAD_GEMINI_MODELS.clear()
            fj._DISCOVERED_GEMINI_MODELS = None

    def test_ats_portal_illegal_characters_scrubbed(self):
        """Ensure generated .docx and .pdf CVs never contain ATS-rejected characters: < > [ ] " { } \\"""
        import re

        def extract_pdf_strings(pdf_bytes):
            raw = pdf_bytes.decode("latin-1", errors="replace")
            tokens = re.findall(r"Tm \((.*?)\) Tj ET", raw)
            decoded = []
            for tok in tokens:
                # Reverse PDF string literal escaping (\(, \), \\) in single pass
                s = re.sub(r"\\([()\\])", r"\1", tok)
                decoded.append(s)
            return "\n".join(decoded)

        # 1. Verify all real subscriber master CVs produce 100% clean .docx & .pdf outputs
        sample_job = {
            "title": "Director, Enterprise Operations [LATAM] </script><b>x</b>",
            "company": 'Citi "Banamex"',
            "location": "Guadalajara, Jalisco, Mexico",
            "team": "Operations {Global}",
        }
        for sub in fj.get_subscribers(CFG):
            cv_md = fj.load_subscriber_cv(sub)
            self.assertIsNone(
                fj.ATS_ILLEGAL_CHARS_RE.search(cv_md),
                f"Master CV for {sub['id']} contains ATS-illegal characters",
            )
            tailored = fj.tailor_cv_deterministic(sample_job, cv_md, sub)
            docx_text = fj.extract_docx_text(fj.render_minimalist_cv_docx(tailored["cv"], sample_job))
            pdf_text = extract_pdf_strings(fj.render_minimalist_cv_pdf(tailored["cv"], sample_job))
            self.assertIsNone(
                fj.ATS_ILLEGAL_CHARS_RE.search(docx_text),
                f"DOCX text for {sub['id']} leaked illegal chars: {docx_text}",
            )
            self.assertIsNone(
                fj.ATS_ILLEGAL_CHARS_RE.search(pdf_text),
                f"PDF text for {sub['id']} leaked illegal chars: {pdf_text}",
            )

        # 2. Verify hostile LLM / noisy ATS payload is semantically preserved while 100% scrubbed
        noisy_cv = {
            "full_name": 'Lucas "L." Matter [MBA]',
            "target_direction": "Target Role Direction: [VP / Director of Operations & AI]",
            "target_headline": 'Director <LATAM> | Cloud\\AI {Enterprise} "Transformation"',
            "contact_line": "Zapopan, Jalisco | lucas@example.com | [LinkedIn]",
            "summary": 'Delivered <5-minute streaming SLO, >95% forecast accuracy, and >=$12M savings across "As-Is" -> “To-Be” platforms.',
            "core_skills": ['CI\\CD Pipelines', 'Process Mining [Celonis]', 'Cloud {GCP}'],
            "experience": [
                {
                    "company": 'Global "Tech" Corp [MX]',
                    "role": "Sr. Manager → Director {Operations}",
                    "location": "Guadalajara [Hybrid]",
                    "dates": "2022 - Present",
                    "bullets": [
                        'Cut cycle time to <=24 hours (<5% variance) and lifted CSAT >92% across [LATAM] \\ {EMEA}.',
                        "Hardened data ingestion </script><b>x</b> with zero downtime.",
                    ],
                }
            ],
            "education": [
                {"degree": 'B.S. "Engineering"', "institution": "University [Brazil]", "dates": "2018"}
            ],
            "languages": ['English [Fluent]', 'Spanish {Professional}', 'Portuguese "Native"'],
        }
        docx_out = fj.extract_docx_text(fj.render_minimalist_cv_docx(noisy_cv, sample_job))
        pdf_out = extract_pdf_strings(fj.render_minimalist_cv_pdf(noisy_cv, sample_job))

        self.assertIsNone(fj.ATS_ILLEGAL_CHARS_RE.search(docx_out), f"Illegal char in DOCX: {docx_out}")
        self.assertIsNone(fj.ATS_ILLEGAL_CHARS_RE.search(pdf_out), f"Illegal char in PDF: {pdf_out}")
        self.assertIn("under 5-minute streaming SLO", docx_out)
        self.assertIn("over 95% forecast accuracy", docx_out)
        self.assertIn("at least $12M savings", docx_out)
        self.assertIn("up to 24 hours", docx_out)
        self.assertIn("CI/CD Pipelines", docx_out)
        self.assertIn("Target Role Direction: VP / Director of Operations & AI", docx_out)

    def test_salt_lake_city_and_location_discrepancies(self):
        """Ensure Salt Lake City / US-only multi-location postings, Salto (Brazil), Jal (NM),
        redundant CDMX '; Mexico' segments, and cross-segment US-Remote never leak as Jalisco/MX."""
        lc = lambda s: fj.location_class(s, RULES, CFG)
        # False-positive traps must all return None
        self.assertIsNone(lc("Salt Lake City, UT"))
        self.assertIsNone(lc("USA, CA, Pleasanton; USA, UT, Salt Lake City"))
        self.assertIsNone(lc("Salto, Sao Paulo, Brazil"))
        self.assertIsNone(lc("Jal, NM, USA"))
        self.assertIsNone(lc("CIUDAD DE MEXICO, Mexico; Mexico"))
        self.assertIsNone(lc("Monterrey, Nuevo Leon, Mexico; Mexico"))
        self.assertIsNone(lc("United States - Colorado - Remote; Mexico City, Mexico"))

        # True positives & Workday ERP location cleanup
        self.assertEqual(fj._clean_workday_location("MEX.Zapopan"), "Zapopan, Mexico")
        self.assertEqual(lc(fj._clean_workday_location("MEX.Zapopan")), "jalisco")
        self.assertEqual(fj._dedupe_location_segments("Mexico (remote); Mexico"), "Mexico (remote)")
        self.assertEqual(lc("Mexico (remote); Mexico"), "remote_mx")
        self.assertEqual(lc("El Salto, Jalisco, Mexico; Salt Lake City, UT"), "jalisco")

        # Workday CXS stemmer audit: 'El Salto' search returning '3 Locations' (Salt Lake City) vs real Jalisco
        def wd_multi_router(url, payload=None, retries=2, timeout=25):
            if url.endswith("/jobs"):
                return {
                    "total": 2,
                    "jobPostings": [
                        {
                            "title": "Director, Sales Operations",
                            "externalPath": "/job/USA-CA-Pleasanton/Director-Sales_JR_SLC",
                            "locationsText": "3 Locations",
                            "postedOn": "Posted Today",
                            "bulletFields": ["JR_SLC"],
                        },
                        {
                            "title": "Director, Supply Chain",
                            "externalPath": "/job/Spring-Texas/Director-SC_JR_GDL",
                            "locationsText": "2 Locations",
                            "postedOn": "Posted Today",
                            "bulletFields": ["JR_GDL"],
                        },
                    ],
                }
            if "JR_SLC" in url:
                return {
                    "jobPostingInfo": {
                        "location": "USA, CA, Pleasanton",
                        "additionalLocations": ["USA, GA, Atlanta", "USA, UT, Salt Lake City"],
                        "jobDescription": "<p>US-only role.</p>",
                    }
                }
            if "JR_GDL" in url:
                return {
                    "jobPostingInfo": {
                        "location": "Spring, Texas, United States of America",
                        "additionalLocations": ["Tlaquepaque, Jalisco, Mexico"],
                        "jobDescription": "<p>Global role with hub in Tlaquepaque.</p>",
                    }
                }
            raise AssertionError("unexpected url " + url)

        fj.http_json = wd_multi_router
        co = {"name": "Workday, Inc.", "ats": "workday", "tenant": "workday", "wd": "wd5", "site": "Workday", "search_terms": ["El Salto"]}
        fetched = fj.fetch_workday(co, today=date(2026, 11, 8))
        self.assertEqual(len(fetched), 2)
        fj.resolve_workday_multi_locations(fetched, [RULES], RULES, CFG)
        selected = fj.select(fetched, RULES, CFG)
        self.assertEqual(len(selected), 1)
        self.assertIn("JR_GDL", selected[0]["key"])
        self.assertEqual(selected[0]["location"], "Tlaquepaque, Jalisco, Mexico (+1 other location)")

    @patch("fetch_jobs._invoke_gemini_json")
    def test_ats_platform_vs_employer_name_discrepancy(self, mock_invoke):
        """Ensure Flex (and other employers hosted on Workday) never get called 'Workday' in emails/CVs."""
        companies = fj.load_companies()
        names = {c["name"] for c in companies}
        self.assertIn("Workday, Inc.", names)
        self.assertNotIn("Workday", names)

        # Direct scrubber checks
        self.assertEqual(
            fj._fix_ats_employer_confusion(
                "This step-up Director seat at Workday leverages Workday's Guadalajara footprint.",
                "Flex",
                "Director, Operations",
            ),
            "This step-up Director seat at Flex leverages Flex's Guadalajara footprint.",
        )
        # Never alter Workday, Inc. or genuine Workday HRIS role references
        self.assertIn(
            "Workday, Inc.",
            fj._fix_ats_employer_confusion("Lead enterprise sales at Workday, Inc.", "Workday, Inc.", "Account Executive"),
        )
        self.assertIn(
            "Workday",
            fj._fix_ats_employer_confusion("Own HRIS Workday configuration at Flex.", "Flex", "Workday HRIS Analyst"),
        )

        # Verify classify_career_upgrades sends integer IDs (never 'workday:Flex:...' keys) & scrubs output
        captured_prompts = []

        def fake_gemini(prompt, **kwargs):
            captured_prompts.append(prompt)
            return {
                "upgrades": [
                    {"id": 0, "is_career_upgrade": True, "upgrade_reason": "Step-up regional leadership scope at Workday."},
                ]
            }

        mock_invoke.side_effect = fake_gemini
        lucas_cfg = next(s for s in fj.get_subscribers(CFG) if s["id"] == "lucas")
        jobs = [
            {"key": "workday:Flex:WD228976", "title": "Director, Program Management", "company": "Flex", "team": "Ops"},
            {"key": "workday:Flex:WD228977", "title": "Senior Manager, Logistics", "company": "Flex", "team": "SCM"},
        ]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}):
            fj.classify_career_upgrades(jobs, lucas_cfg)
        self.assertTrue(captured_prompts)
        self.assertNotIn("workday:Flex:", captured_prompts[0])
        self.assertEqual(jobs[0]["upgrade_reason"], "Step-up regional leadership scope at Flex.")

    def test_city_spam_deduplication_and_luis_factory_exclusions(self):
        """Collapse remote multi-city clones into 1 clean card and filter hardware technicians from Luis."""
        clones = [
            {
                "key": f"lever:Bluelight:{i}",
                "company": "Bluelight Consulting",
                "title": "Mid-Level Account Executive - LATAM",
                "team": "Sales",
                "location": f"Remote - {city}, Mexico",
                "loc_class": "remote_mx",
                "posted_at": "2026-11-06",
                "discovered_now": True,
            }
            for i, city in enumerate(["Guadalajara", "Monterrey", "Mexico City", "Queretaro", "Puebla"])
        ]
        deduped = fj.dedupe_subscriber_jobs(clones)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["location"], "Remote - Mexico")
        self.assertTrue(deduped[0]["discovered_now"])

        luis_cfg = next(s for s in fj.get_subscribers(CFG) if s["id"] == "luis")
        luis_rules = fj.build_rules(luis_cfg)
        for noisy_title in (
            "Process Engineering Technician",
            "SMT Process Engineer",
            "Surface Mount Process Engineer",
            "Billing Operations Analyst 1",
        ):
            self.assertFalse(fj.is_director(noisy_title, luis_rules), noisy_title)

    @patch("smtplib.SMTP_SSL")
    def test_extreme_reliability_and_workflow_integrity(self, mock_smtp_ssl):
        """Verify corrupted state self-healing, malformed ATS items, SMTP transient retry,
        bad regex resilience, Gemini auth circuit-breaker, and workflow YAML sync."""
        import smtplib

        # 1. Corrupted state/seen.json + malformed ATS entries must self-heal without crashing
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "state" / "seen.json").write_text("[]", encoding="utf-8")  # wrong JSON type
        (self.root / "state" / "jobs.json").write_text("{truncated", encoding="utf-8")  # invalid JSON

        noisy_gh = {
            "jobs": [
                None,
                {},
                {"id": "", "title": "Director, Empty ID"},
                {
                    "id": 99,
                    "title": "Director, Platform Reliability",
                    "location": {"name": "Guadalajara, Jalisco, Mexico"},
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/99",
                    "first_published": "not-a-valid-date",
                },
            ]
        }
        code, jobs = self.run_refresh(date(2026, 11, 8), {"greenhouse.io": noisy_gh}, send_email=False)
        self.assertEqual(code, 0)
        self.assertIn("99", jobs)
        healed_seen = fj.load_json(self.root / "state" / "seen.json", {})
        self.assertTrue(healed_seen.get("initialized"))

        # 2. Bad regex pattern in config never crashes build_rules
        rules_with_typo = fj.build_rules({
            "title_include": ["[unclosed_regex", r"\bdirector\b"],
            "title_exclude": ["(unclosed_exc"],
            "jalisco_locations": ["guadalajara"],
        })
        self.assertTrue(fj.is_director("Director of Operations", rules_with_typo))

        # 3. SMTP transient disconnect retries automatically and succeeds on 2nd attempt
        mock_server = MagicMock()
        mock_server.send_message.side_effect = [
            smtplib.SMTPServerDisconnected("transient reset"),
            None,
        ]
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        with patch.dict(os.environ, {"GMAIL_USER": " sender@gmail.com ", "GMAIL_APP_PASSWORD": " app-pass "}):
            sent_ok = fj.send_email_digest(
                [jobs["99"]],
                1,
                {"id": "lucas", "name": "Lucas", "recipient_email": "lucasschollmatter@gmail.com"},
                date(2026, 11, 8),
                root=self.root,
                attachments=[],
            )
        self.assertTrue(sent_ok)
        self.assertEqual(mock_server.send_message.call_count, 2)

        # 4. SMTPAuthenticationError fails fast (1 attempt, zero retries)
        mock_auth_server = MagicMock()
        mock_auth_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        mock_smtp_ssl.return_value.__enter__.return_value = mock_auth_server
        with patch.dict(os.environ, {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "wrong"}):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                fj.send_email_digest(
                    [jobs["99"]],
                    1,
                    {"id": "lucas", "name": "Lucas", "recipient_email": "lucasschollmatter@gmail.com"},
                    date(2026, 11, 8),
                    root=self.root,
                    attachments=[],
                )
        self.assertEqual(mock_auth_server.login.call_count, 1)

        # 5. Gemini HTTP 403 trips auth circuit-breaker immediately so subsequent calls skip network
        fj._DEAD_GEMINI_API_KEYS.clear()
        gemini_hits = []

        def forbidden_gemini(url, payload=None, retries=2, headers=None, timeout=60):
            gemini_hits.append(url)
            raise error.HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

        fj.http_json = forbidden_gemini
        try:
            with self.assertRaises(error.HTTPError):
                fj.call_gemini_json("p1", api_key="revoked-key")
            self.assertEqual(len(gemini_hits), 1)
            # Second call returns None immediately with 0 HTTP hits
            self.assertIsNone(fj.call_gemini_json("p2", api_key="revoked-key"))
            self.assertEqual(len(gemini_hits), 1)
        finally:
            fj._DEAD_GEMINI_API_KEYS.clear()

        # 6. Verify gha_summary never pollutes GITHUB_STEP_SUMMARY while running under unittest
        step_sum = self.root / "gha_summary.md"
        with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(step_sum)}):
            fj.gha_summary("### Simulated Test Summary")
        self.assertFalse(step_sum.exists(), "gha_summary must not write during unittest runs")

        # 7. Verify classify_career_upgrades runs only ONCE across prepare_tailored_cvs + build_email_message
        upgrade_calls = []

        def track_upgrade_gemini(prompt_text, **kwargs):
            upgrade_calls.append(prompt_text)
            return {"upgrades": [{"id": 0, "is_career_upgrade": True, "upgrade_reason": "Executive scope at Acme."}]}

        sample_batch = [
            {"key": "k1", "title": "Director, Platform", "company": "Acme", "location": "Zapopan", "loc_class": "jalisco", "url": "https://example.com/1"},
            {"key": "k2", "title": "Head of Ops", "company": "Beta", "location": "Guadalajara", "loc_class": "jalisco", "url": "https://example.com/2"},
        ]
        sub_lucas = next(s for s in fj.get_subscribers(CFG) if s["id"] == "lucas")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "valid-key"}), patch("fetch_jobs._invoke_gemini_json", side_effect=track_upgrade_gemini):
            fj.classify_career_upgrades(sample_batch, sub_lucas)
            fj.classify_career_upgrades(sample_batch, sub_lucas)
        self.assertEqual(len(upgrade_calls), 1, "classify_career_upgrades must not fire duplicate LLM calls on already-classified jobs")

        # 8. Verify multi-page PDF pagination never truncates bottom sections (Education / Languages)
        long_cv = {
            "full_name": "Lucas Scholl Matter",
            "target_direction": "Target Role Direction: Executive Leadership - Global Operations",
            "target_headline": "Director | Global Business Services & Process Intelligence",
            "contact_line": "Zapopan, Jalisco | lucasschollmatter@gmail.com",
            "summary": "Executive leader with extensive multi-region transformation experience across global enterprise operations. " * 3,
            "core_skills": [f"Enterprise Competency {i}" for i in range(10)],
            "experience": [
                {
                    "company": f"Global Enterprise {i}",
                    "role": f"Director of Operations {i}",
                    "location": "Guadalajara, Mexico",
                    "dates": f"201{i} - 202{i}",
                    "bullets": [
                        f"Delivered enterprise transformation milestone {b} driving measurable cycle-time reduction across global hubs and cross-functional teams."
                        for b in range(6)
                    ],
                }
                for i in range(6)
            ],
            "education": [{"degree": "MBA Executive Management", "institution": "IPADE Business School", "dates": "2022"}],
            "languages": ["Portuguese (Native)", "Spanish (Fluent)", "English (Fluent)"],
        }
        long_pdf = fj.render_minimalist_cv_pdf(long_cv).decode("latin-1", errors="replace")
        self.assertIn("/Count 2", long_pdf)
        self.assertIn("IPADE Business School", long_pdf)
        self.assertIn("Portuguese", long_pdf)

        # 9. Validate .github/workflows/main.yml covers every active subscriber, uses Node-24 actions & buffered unittest
        wf_path = Path(fj.ROOT) / ".github" / "workflows" / "main.yml"
        self.assertTrue(wf_path.exists())
        wf_txt = wf_path.read_text(encoding="utf-8")
        self.assertNotIn("\t", wf_txt, "Workflow YAML must not contain tab indentation")
        self.assertIn("-m unittest discover -s tests -b -v", wf_txt)
        self.assertIn("Re-send ALL current matching roles (testing only)", wf_txt)
        email_all_block = wf_txt.split("email_all:")[1].split("subscriber:")[0]
        self.assertIn("default: false", email_all_block, "Manual runs must not re-send every role by default")
        for action_ref in ("actions/checkout@v7", "actions/setup-python@v7", "actions/upload-artifact@v7"):
            self.assertIn(action_ref, wf_txt)
        sub_block = wf_txt.split("subscriber:")[1].split("check_only:")[0]
        self.assertIn("type: string", sub_block, "subscriber input must be string so unlimited auto-onboarded IDs work")
        self.assertIn('default: "all"', sub_block)
        self.assertIn("gemini-4-hydrogen", wf_txt)
        self.assertIn("min_match_score:", wf_txt)

        onboard_wf_path = Path(fj.ROOT) / ".github" / "workflows" / "onboard.yml"
        self.assertTrue(onboard_wf_path.exists())
        onboard_wf_txt = onboard_wf_path.read_text(encoding="utf-8")
        self.assertNotIn("\t", onboard_wf_txt)
        self.assertIn("onboard_subscriber", onboard_wf_txt)
        self.assertIn("-m unittest discover -s tests -b -v", onboard_wf_txt)
        self.assertIn("--send-confirmation-from", onboard_wf_txt)

    @patch("smtplib.SMTP_SSL")
    def test_genai_candidate_job_match_gate_and_rejection_ledger(self, mock_smtp_ssl):
        """GenAI Match Gate validates every filtered opening against candidate career_goal + CV +
        radar company profile, suppresses non-matches from Lucy's report, explains match rationale
        and industry synergy on every kept card, caches rejections in state/seen.json, and rolls
        back cleanly if SMTP delivery fails."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        real_companies_by_name = {c["name"]: c for c in fj.load_companies()}
        radar_cos = [
            {
                "name": "AstraZeneca",
                "ats": "greenhouse",
                "board": "azboard",
                "industry": real_companies_by_name["AstraZeneca"]["industry"],
                "description": real_companies_by_name["AstraZeneca"]["description"],
            },
            {
                "name": "Flex",
                "ats": "lever",
                "board": "flexboard",
                "industry": real_companies_by_name["Flex"]["industry"],
                "description": real_companies_by_name["Flex"]["description"],
            },
        ]

        az_feed = {
            "jobs": [
                {
                    "id": 101,
                    "title": "Director, Global Process Excellence & Automation",
                    "location": {"name": "Guadalajara, Jalisco, Mexico"},
                    "absolute_url": "https://boards.greenhouse.io/azboard/jobs/101",
                    "first_published": "2026-11-04T10:00:00Z",
                    "content": "<p>Lead enterprise process mining, GBS transformation, and automation across AstraZeneca hubs.</p>",
                },
                {
                    "id": 102,
                    "title": "Director, Clinical Oncology Wet-Lab Research",
                    "location": {"name": "Guadalajara, Jalisco, Mexico"},
                    "absolute_url": "https://boards.greenhouse.io/azboard/jobs/102",
                    "first_published": "2026-11-04T12:00:00Z",
                    "content": "<p>Requires MD or PhD in Molecular Oncology and 10+ years of clinical trial wet-lab bench science.</p>",
                },
            ]
        }
        flex_feed = [
            {
                "id": "fx201",
                "text": "Senior Director, Supply Chain Process Intelligence",
                "categories": {"location": "Zapopan, Jalisco", "allLocations": ["Zapopan, Jalisco"], "team": "Operations"},
                "hostedUrl": "https://jobs.lever.co/flexboard/fx201",
                "createdAt": 1793750400000,
                "descriptionPlain": "Drive end-to-end operational excellence and process analytics across Jalisco manufacturing.",
            }
        ]
        fj.http_json = fake_http({"greenhouse.io": az_feed, "lever.co": flex_feed})

        match_prompts = []

        def fake_match_gate(prompt_text, **kwargs):
            import re as _re
            match_prompts.append(prompt_text)
            out_matches = []
            for m in _re.finditer(r"\[JOB id=(\d+)\]\nTitle: ([^\n]+)\nHiring Employer: ([^\n]+)", prompt_text):
                job_id = int(m.group(1))
                title = m.group(2).strip()
                employer = m.group(3).strip()
                if "Clinical Oncology" in title:
                    out_matches.append({
                        "id": job_id,
                        "is_good_match": False,
                        "match_score": 24,
                        "is_career_upgrade": False,
                        "match_rationale": "Requires an MD/PhD in wet-lab oncology research, which conflicts with Lucas's GBS, Process Mining, and Tech Operations career track.",
                        "industry_synergy": "",
                        "matched_keywords": [],
                        "upgrade_reason": "",
                    })
                elif "AstraZeneca" in employer:
                    out_matches.append({
                        "id": job_id,
                        "is_good_match": True,
                        "match_score": 95,
                        "is_career_upgrade": True,
                        "match_rationale": "Direct match with your Process Mining & Automation track record at AstraZeneca GITC Guadalajara and Google Cloud infrastructure.",
                        "industry_synergy": "Biopharmaceutical GBS & Enterprise IT synergy with your prior Global Process Mining Lead tenure at AstraZeneca.",
                        "matched_keywords": ["Process Mining", "GBS Transformation", "Automation"],
                        "upgrade_reason": "Steps up into global Director ownership across AstraZeneca's enterprise hubs.",
                    })
                else:
                    out_matches.append({
                        "id": job_id,
                        "is_good_match": True,
                        "match_score": 88,
                        "is_career_upgrade": True,
                        "match_rationale": "Strong fit between your large-scale data center and supply chain process intelligence leadership and Flex's Jalisco campus.",
                        "industry_synergy": "High-tech hardware & EMS operations scale directly mirrors your global infrastructure operations background.",
                        "matched_keywords": ["Process Intelligence", "Operational Excellence"],
                        "upgrade_reason": "Senior Director scope leading multi-site operational intelligence at Flex.",
                    })
            return {"_llm_model": "gemini-4-hydrogen", "assessments": out_matches}

        # Cap tailored CVs at 1 so we verify BOTH CV-attached and non-CV-attached cards render Match Rationale & Company Profile
        test_cfg = {
            **CFG,
            "max_tailored_cvs": 1,
            "subscribers": [
                {**s, "max_tailored_cvs": 1} if s["id"] == "lucas" else s
                for s in CFG["subscribers"]
            ],
        }

        env = {
            "GMAIL_USER": "sender@gmail.com",
            "GMAIL_APP_PASSWORD": "app-secret-password",
            "GEMINI_API_KEY": "fake-gemini-key",
        }

        # --- Day 1: 3 filtered openings -> 1 vetoed (Oncology), 2 kept & emailed with rationales ---
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=fake_match_gate) as mock_gate, \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            code = fj.refresh(test_cfg, radar_cos, root=self.root, today=date(2026, 11, 5), send_email=True, subscriber_id="lucas")

        self.assertEqual(code, 0)
        self.assertEqual(mock_gate.call_count, 1)
        # Prompt grounded in career_goal, enhanced CV company profiles, and radar company industry/description
        self.assertIn("CANDIDATE CAREER GOAL", match_prompts[0])
        self.assertIn("Biopharmaceuticals", match_prompts[0])
        self.assertIn("CANDIDATE MASTER CV", match_prompts[0])

        sent_msg = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn("2 new roles for you", sent_msg["Subject"])
        html_body = sent_msg.get_body(preferencelist=("html",)).get_content()
        plain_body = sent_msg.get_body(preferencelist=("plain",)).get_content()

        # Vetoed Oncology role MUST NOT appear anywhere in the email
        self.assertNotIn("Clinical Oncology", html_body)
        self.assertNotIn("Clinical Oncology", plain_body)
        self.assertEqual(plain_body.count("\n- "), 2)

        # Both kept roles MUST appear with company profile + industry + match rationale + industry synergy
        self.assertIn("Director, Global Process Excellence &amp; Automation", html_body)
        self.assertIn("Senior Director, Supply Chain Process Intelligence", html_body)
        self.assertIn("Lucy&rsquo;s Headhunter Take &amp; Tailored CV", html_body)      # Role 1 (with CV)
        self.assertIn("Why This Role &amp; Company Match Your Profile", html_body)      # Role 3 (without CV)
        self.assertIn("Industry &amp; Company Fit:", html_body)
        self.assertIn("95% CV Match", html_body)
        self.assertIn("88% CV Match", html_body)
        self.assertIn("Biopharmaceuticals", html_body)
        self.assertIn("EMS", html_body)
        self.assertIn("Industry Fit:", plain_body)

        # Rejection ledger persisted in state/seen.json
        seen_day1 = fj.load_json(self.root / "state" / "seen.json", {})
        rej_ledger = seen_day1["subscribers"]["lucas"]["rejected_matches"]
        self.assertIn("greenhouse:AstraZeneca:102", rej_ledger)
        self.assertEqual(rej_ledger["greenhouse:AstraZeneca:102"]["score"], 24)

        # --- Day 2: Identical feed -> rejected role is skipped from ledger (0 Gemini Match Gate calls) -> Quiet Day ---
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=fake_match_gate) as mock_gate_d2, \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            code2 = fj.refresh(test_cfg, radar_cos, root=self.root, today=date(2026, 11, 6), send_email=True, subscriber_id="lucas")
        self.assertEqual(code2, 0)
        self.assertEqual(mock_gate_d2.call_count, 0)
        msg_d2 = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn("Quiet day", msg_d2["Subject"])

        # --- Day 3: New off-target role arrives AND SMTP fails -> rejected_matches rolls back cleanly ---
        az_feed_d3 = {
            "jobs": az_feed["jobs"] + [{
                "id": 103,
                "title": "Director, Clinical Oncology Wet-Lab Research II",
                "location": {"name": "Guadalajara, Jalisco, Mexico"},
                "absolute_url": "https://boards.greenhouse.io/azboard/jobs/103",
                "first_published": "2026-11-07T08:00:00Z",
            }]
        }
        fj.http_json = fake_http({"greenhouse.io": az_feed_d3, "lever.co": flex_feed})
        mock_server.send_message.side_effect = OSError("smtp temporary outage")
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=fake_match_gate), \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            code3 = fj.refresh(test_cfg, radar_cos, root=self.root, today=date(2026, 11, 7), send_email=True, subscriber_id="lucas")
        self.assertEqual(code3, 2)
        seen_day3 = fj.load_json(self.root / "state" / "seen.json", {})
        self.assertNotIn("greenhouse:AstraZeneca:103", seen_day3["subscribers"]["lucas"]["rejected_matches"])

        # --- Day 4: SMTP recovers -> vetoed role results in honest Quiet Day (never emailed) and is now recorded ---
        mock_server.send_message.side_effect = None
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=fake_match_gate), \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            code4 = fj.refresh(test_cfg, radar_cos, root=self.root, today=date(2026, 11, 7), send_email=True, subscriber_id="lucas")
        self.assertEqual(code4, 0)
        msg_d4 = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn("Quiet day", msg_d4["Subject"])
        self.assertNotIn("Oncology", msg_d4.get_body(preferencelist=("plain",)).get_content())
        seen_day4 = fj.load_json(self.root / "state" / "seen.json", {})
        self.assertIn("greenhouse:AstraZeneca:103", seen_day4["subscribers"]["lucas"]["rejected_matches"])

    def test_enhanced_cv_company_profiles_across_subscribers_and_formats(self):
        """Every subscriber has an explicit career_goal and enhanced employer Company Profile lines
        in cvs/*.md, and tailored .docx/.pdf/.html resumes preserve company_context cleanly."""
        subs = fj.get_subscribers(fj.load_config())
        self.assertGreaterEqual(len(subs), 4)
        sample_job = {
            "title": "Director, Digital Transformation & Operations",
            "company": "Flex",
            "location": "Guadalajara, Jalisco, Mexico",
            "team": "Global Operations",
        }
        for sub in subs:
            sub_id = sub["id"]
            self.assertTrue((sub.get("career_goal") or "").strip(), f"Missing career_goal for {sub_id}")
            cv_md = fj.load_subscriber_cv(sub)
            self.assertIn("*Company Profile:", cv_md, f"Missing enhanced Company Profile lines in cvs/{sub_id}.md")
            profiles = fj.extract_cv_company_profiles(cv_md)
            self.assertGreaterEqual(len(profiles), 1, f"Expected >=1 extracted employer profile for {sub_id}")

            det = fj.tailor_cv_deterministic(sample_job, cv_md, sub)
            exp_with_ctx = [e for e in det["cv"]["experience"] if e.get("company_context")]
            self.assertTrue(exp_with_ctx, f"Deterministic CV for {sub_id} lost company_context")
            sample_ctx_snippet = exp_with_ctx[0]["company_context"][:32]

            docx_txt = fj.extract_docx_text(fj.render_minimalist_cv_docx(det["cv"], sample_job))
            pdf_txt = fj.extract_pdf_text(fj.render_minimalist_cv_pdf(det["cv"], sample_job))
            html_out = fj.render_minimalist_cv_html(det["cv"], sample_job)

            self.assertIn(sample_ctx_snippet, docx_txt)
            # Compared the way the PDF writes text (typographic punctuation mapped, string escapes
            # undone), so a profile that starts with a dash or a parenthesis is not mistaken for a
            # lost profile. The raw-bytes comparison used before failed for such sign-up CVs.
            self.assertIn(fj.pdf_safe_text(sample_ctx_snippet[:20]), pdf_txt)
            self.assertIn(fj.html.escape(sample_ctx_snippet), html_out)
            self.assertIsNone(fj.ATS_ILLEGAL_CHARS_RE.search(docx_txt))

    @patch("smtplib.SMTP_SSL")
    def test_self_service_candidate_onboarding_flow_and_immediate_matching(self, mock_smtp_ssl):
        """Verify end-to-end onboarding: web form HTML generation, Gemini CV structuring with
        company profiles, checkbox + free-text rule synthesis, subscribers/<id>.json & cvs/<id>.md
        files (config.json untouched), professional confirmation email, idempotent re-onboarding,
        and automatic daily match delivery."""
        import onboard_subscriber as ob
        import shutil

        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        # Prepare isolated repo workspace with real config.json and cvs/
        shutil.copy2(Path(fj.ROOT) / "config.json", self.root / "config.json")
        shutil.copytree(Path(fj.ROOT) / "cvs", self.root / "cvs")
        # Keep the real global settings but not the real people. The assertions below depend on
        # which ids are already taken ("sofia" free, "lucas" taken); a real sign-up named Sofia in
        # config.json used to fail this test, and with it the daily run for everybody.
        isolated_cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        isolated_cfg["subscribers"] = [{"id": "lucas", "name": "Lucas", "recipient_email": "lucas@example.com", "enabled": False}]
        (self.root / "config.json").write_text(json.dumps(isolated_cfg), encoding="utf-8")

        # 1. Verify rendered onboarding webpage contains clean, agnostic options, .txt/.md CV upload & free-text chips
        onboard_html = ob.render_onboard_page({"onboard_webhook_url": "https://script.google.com/macros/s/test/exec"})
        self.assertIn('type="email"', onboard_html)
        self.assertIn('accept=".txt,.md', onboard_html)
        self.assertIn("levelChecks", onboard_html)
        self.assertIn("domainChecks", onboard_html)
        self.assertIn("locationChecks", onboard_html)
        self.assertIn("freeText", onboard_html)
        self.assertIn("promptChips", onboard_html)
        self.assertIn("Specialist / Individual Contributor", onboard_html)
        self.assertIn("Operations & Supply Chain", onboard_html)
        self.assertIn("Other / Open (Guided by Free Text)", onboard_html)
        self.assertNotIn("Celonis", onboard_html)  # UI options stay 100% role-agnostic & jargon-free
        self.assertNotIn("</script><script>", onboard_html)

        # Verify candidate can leave domain checkboxes empty and rely purely on Role Level + Free Text
        minimal_val = ob.validate_onboarding_payload({
            "name": "Carlos Mendez",
            "email": "carlos@example.com",
            "cv_text": "Carlos Mendez | Guadalajara | 8 years leading brand marketing, product launches, and growth campaigns across LATAM.",
            "levels": ["senior", "manager"],
            "domains": [],
            "locations": ["jalisco", "remote_mx"],
            "free_text": "Looking for Marketing Manager or Growth Lead roles in consumer or tech companies; skip sales cold-calling.",
        })
        self.assertEqual(minimal_val["domains"], ["other"])
        self.assertEqual(minimal_val["levels"], ["senior", "manager"])

        # 2. Simulate candidate submitting web form via GitHub repository_dispatch event JSON
        event_file = self.root / "event.json"
        raw_uploaded_cv = (
            "Sofia Ramirez\n"
            "Guadalajara, MX | sofia.ramirez@example.com\n"
            "Senior leader with >10 years driving [GBS] & Intelligent Automation across LATAM.\n"
            "- Led 25+ RPA & Power Platform automations saving >=$3.5M at 'Global Pharma Hub'.\n"
            "- Built Celonis process mining CoE reducing order-to-cash cycle time <48 hours.\n"
            "B.S. Industrial Engineering — ITESO\n"
        )
        event_payload = {
            "action": "onboard_subscriber",
            "client_payload": {
                "name": "Sofia Ramirez",
                "email": "sofia.ramirez@example.com",
                "cv_text": raw_uploaded_cv,
                "levels": ["director", "manager"],
                "domains": ["strategy_process", "technology"],
                "locations": ["remote_mx"],  # Opted into Remote Mexico ONLY (unchecked Jalisco!)
                "language": "Spanish",
                "free_text": "Looking for Director or Senior Manager roles in Process Mining, GBS Transformation, or Intelligent Automation. Exclude pure accounting, tax, or factory maintenance roles.",
            },
        }
        event_file.write_text(json.dumps(event_payload), encoding="utf-8")

        def fake_onboard_gemini(prompt_text, system_text=None, **kwargs):
            if system_text == ob.ONBOARD_CV_SYSTEM_PROMPT:
                return {
                    "_llm_model": "gemini-4-hydrogen",
                    "full_name": 'Sofia "S." Ramirez [PMP]',
                    "contact_line": "Guadalajara, Jalisco | sofia.ramirez@example.com",
                    "summary": "Executive GBS and Intelligent Automation leader with >10 years scaling regional transformation hubs.",
                    "core_competencies": [
                        {"category": "Process & Automation", "items": "Celonis Process Mining, RPA, Microsoft Power Platform, GBS Strategy"}
                    ],
                    "experience": [
                        {
                            "company": "AstraZeneca",
                            "location": "Guadalajara, Mexico",
                            "company_profile": "Global biopharmaceutical leader operating a major Global Innovation & Technology Centre and GBS hub in Guadalajara.",
                            "role": "Senior Manager, GBS Automation & Process Mining",
                            "dates": "2021 - Present",
                            "bullets": [
                                "Delivered >=$3.5M in annualized savings across [LATAM] via 25+ RPA and Power Platform automations.",
                                "Cut order-to-cash cycle time <48 hours using Celonis process intelligence.",
                            ],
                        }
                    ],
                    "education": ["B.S. Industrial Engineering — ITESO"],
                    "languages": ["Spanish (Native)", "English (Fluent)"],
                }
            if system_text == ob.ONBOARD_PROFILE_SYSTEM_PROMPT:
                return {
                    "_llm_model": "gemini-4-hydrogen",
                    "career_goal": "Targeting Director and Senior Manager roles in GBS Transformation, Process Mining (Celonis), and Intelligent Automation in Remote Mexico—excluding accounting, tax, and factory maintenance.",
                    # The model returns plain phrases; patterns are built in code (never model-written regex)
                    "target_titles": ["process intelligence lead"],
                    "focus_keywords": ["process mining", "automation"],
                    "avoid_titles": ["tax", "accounting", "maintenance"],
                }
            return None

        env = {
            "GMAIL_USER": "sender@gmail.com",
            "GMAIL_APP_PASSWORD": "app-secret-password",
            "GEMINI_API_KEY": "fake-gemini-key",
        }
        extracted = ob.extract_payload_from_github_event(event_file)
        validated = ob.validate_onboarding_payload(extracted)
        config_before = (self.root / "config.json").read_bytes()

        with patch.dict(os.environ, env), patch("fetch_jobs._invoke_gemini_json", side_effect=fake_onboard_gemini):
            res = ob.register_subscriber_in_codebase(validated, root=self.root)
            sent_conf = ob.send_onboarding_confirmation_email(res, board_count=109)

        self.assertTrue(sent_conf)
        self.assertEqual(res["subscriber_id"], "sofia")
        self.assertFalse(res["updated_existing"])

        # The sign-up wrote only this person's two files; the shared config.json is byte-identical
        self.assertEqual((self.root / "config.json").read_bytes(), config_before)
        sofia_record = fj.load_json(self.root / "subscribers" / "sofia.json", {})
        self.assertEqual(sofia_record["id"], "sofia")
        self.assertEqual(sofia_record["cv_file"], "cvs/sofia.md")
        self.assertEqual(sofia_record["rule_breadth"], "focused")
        self.assertTrue(sofia_record["profile_updated_at"].endswith("Z"))
        self.assertIn("process mining", sofia_record["interest_phrases"])
        self.assertFalse((self.root / "docs" / "onboard.html").exists())    # rebuilt by the daily run, not by sign-ups

        # 3. Verify generated cvs/sofia.md is ATS-clean & contains *Company Profile: ...*
        sofia_cv_path = self.root / "cvs" / "sofia.md"
        self.assertTrue(sofia_cv_path.exists())
        sofia_cv_txt = sofia_cv_path.read_text(encoding="utf-8")
        self.assertIn("*Company Profile: Global biopharmaceutical leader", sofia_cv_txt)
        self.assertIsNone(fj.ATS_ILLEGAL_CHARS_RE.search(sofia_cv_txt), f"Illegal ATS chars in onboarded CV: {sofia_cv_txt}")
        self.assertIn("at least $3.5M", sofia_cv_txt)
        self.assertIn("under 48 hours", sofia_cv_txt)

        # 4. Verify Confirmation Email content
        conf_msg = mock_server.send_message.call_args_list[-1][0][0]
        self.assertEqual(conf_msg["To"], "sofia.ramirez@example.com")
        self.assertIn("Welcome to Jobs Sentinel, Sofia", conf_msg["Subject"])
        conf_html = conf_msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("RADAR LIVE &amp; ARMED", conf_html)
        self.assertIn("AstraZeneca", conf_html)
        self.assertIn("Spanish", conf_html)
        self.assertIn("Remote in Mexico", conf_html)

        # 5. Verify idempotent re-onboarding (same email keeps ID 'sofia') & name collision disambiguation ('lucas' -> 'lucas_m')
        updated_cfg = fj.load_config(self.root)      # config.json merged with subscribers/*.json
        same_id, is_upd = ob.derive_subscriber_id("Sofia R.", "sofia.ramirez@example.com", updated_cfg["subscribers"])
        self.assertEqual(same_id, "sofia")
        self.assertTrue(is_upd)
        other_lucas_id, is_upd2 = ob.derive_subscriber_id("Lucas Morales", "lucas.morales@example.com", updated_cfg["subscribers"])
        self.assertEqual(other_lucas_id, "lucas_morales")
        self.assertFalse(is_upd2)

        # 6. Verify Sofia immediately starts receiving her Remote-Mexico matches (and NOT Jalisco on-site roles!) on the next refresh
        fj.http_json = fake_http()
        mock_server.reset_mock()
        with patch.dict(os.environ, {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}):
            os.environ.pop("GEMINI_API_KEY", None)
            code = fj.refresh(updated_cfg, COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True, subscriber_id="sofia")
        self.assertEqual(code, 0)
        self.assertEqual(mock_server.send_message.call_count, 1)
        sofia_match_msg = mock_server.send_message.call_args_list[0][0][0]
        self.assertEqual(sofia_match_msg["To"], "sofia.ramirez@example.com")
        sofia_plain = sofia_match_msg.get_body(preferencelist=("plain",)).get_content()
        # Gamma a1 is 'Director, Platform' in 'Remote - Mexico' -> matches Sofia!
        # Acme 1 is 'Director, Engineering' in 'Guadalajara, Mexico' (on-site Jalisco) -> excluded because Sofia chose Remote-Mexico only!
        self.assertIn("Director, Platform at Gamma", sofia_plain)
        self.assertNotIn("Director, Engineering at Acme", sofia_plain)
        self.assertTrue((self.root / "docs" / "onboard.html").exists())

    def test_briefing_email_design_and_onboarding_ui(self):
        """The daily email is a short stack of cards. Lucy's card holds exactly two things, one
        inspiration and one 60-second play; a role card shows only what helps decide (no employer
        description, team, keyword list, file name or invented score); feed values are escaped,
        job links are http(s)-only and over-long tokens can wrap on a phone. The onboarding page
        keeps its pill toggles and collapsed invite code."""
        import onboard_subscriber as ob

        pictograph = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]|&#(?:9\d{3}|1\d{4,5});")
        sub = make_subscriber()
        employer = {"company": "Flex", "company_industry": "EMS & Hardware Manufacturing",
                    "company_description": "Flex designs and builds products for the world's largest brands."}
        jobs = [
            {"key": "k1", "title": "Director, Platform <Ops>", "location": "Zapopan, Jalisco", "loc_class": "jalisco",
             "team": "Ops Excellence Guild", "url": "https://example.com/1", "posted_at": "2026-11-07", **employer},
            {"key": "k2", "title": "Director, Supply Chain", "location": "Guadalajara, Jalisco", "loc_class": "jalisco",
             "url": "javascript:alert(1)", "posted_at": "2026-10-20", **employer},
        ]
        msg, _, pack = fj.build_email_message(jobs, 5, sub, date(2026, 11, 8), "sender@gmail.com", return_meta=True)
        body = msg.get_body(preferencelist=("html",)).get_content()
        plain = msg.get_body(preferencelist=("plain",)).get_content()

        # 1. A card-based look built from inline styles only: nothing remote, no script, no <style>
        self.assertIn("border-radius", body)
        self.assertIn(f"background-color:{fj.EMAIL_NIGHT}", body)                  # Lucy's dark card
        for unwanted in ("<style", "<script", "<img", "<link", "url(", "@import"):
            self.assertNotIn(unwanted, body)
        self.assertIsNone(pictograph.search(plain))                                   # the text part stays plain

        # 2. Lucy's card: one inspiration (headline + message) and one 60-second play, placed
        #    before the roles. Nothing else from the briefing pack is shown, in either part.
        self.assertEqual(pack["briefing_source"], "vault")
        self.assertIn(fj.html.escape(pack["headline"]), body)
        self.assertIn(fj.html.escape(pack["message"]), body)
        self.assertEqual(body.count("Today&rsquo;s 60-second play"), 1)
        self.assertIn(fj.html.escape(pack["micro_challenge"]), body)
        self.assertLess(body.index("Today&rsquo;s 60-second play"), body.index("Director, Platform &lt;Ops&gt;"))
        self.assertLess(plain.index("Today's 60-second play: "), plain.index("\n- "))
        dropped = [pack["their_ai_vs_our_ai"], pack["connection_ps"], pack["partner_signoff"], pack["cadence_subline"],
                   "Their AI vs. Our AI", "Corner:"]
        for text in dropped:
            self.assertNotIn(fj.html.escape(text), body)
            self.assertNotIn(text, plain)

        # 3. The four-number summary strip (the old "Ready CVs" and "active matches" counts are gone)
        for label in ("New for you", "Career upgrades", "Hot Off the Press", "Tailored CVs"):
            self.assertEqual(body.count(f">{label}</div>"), 1)
        for old_label in ("Ready CVs", "active matches"):
            self.assertNotIn(old_label, body)

        # 4. Role cards: the industry but not the employer description, no team, one Apply button
        #    each, and nothing invented: no score and no boilerplate step-up reason
        self.assertEqual(body.count(fj.html.escape(employer["company_industry"])), 2)
        for part in (body, plain):
            self.assertNotIn("largest brands", part)
            self.assertNotIn("Ops Excellence Guild", part)
            self.assertNotIn("Step-up leadership", part)
        self.assertEqual(body.count("Apply &rarr;"), 2)
        self.assertEqual(body.count("&uarr; Career Upgrade Move</span>"), 2)        # the badge still marks the upgrade
        self.assertEqual(body.count(">Not AI-reviewed</span>"), 2)
        self.assertNotIn("% CV Match", body)
        # ...and with no CV attached, Lucy's two items do not point at one
        self.assertIsNone(fj._TAILORED_CV_CLAIM_RE.search(" ".join(pack[k] for k in ("headline", "message", "micro_challenge"))))

        # 5. Feed values are escaped and links are http(s)-only
        self.assertIn("Director, Platform &lt;Ops&gt;", body)
        self.assertNotIn("<Ops>", body)
        self.assertIn('href="https://example.com/1"', body)
        self.assertNotIn("javascript:", body)
        self.assertEqual(fj._safe_http_url("javascript:alert(1)"), "#")
        self.assertEqual(fj._safe_http_url(" HTTPS://jobs.example.com/x "), "HTTPS://jobs.example.com/x")

        # 6. A reviewed role with a tailored CV: its score, a CV badge, the step-up reason the review
        #    wrote and one 'why' panel. Keywords, the CV headline and the file name are not shown
        #    in the card; the file name stays in the text part and in the attachment list.
        cv_name = "CV_Quentin_Tester_Flex_Senior_Director_Process_Intelligence.docx"
        why = "Your process-mining record maps onto this remit."
        reviewed = {
            "key": "k3", "title": "Senior Director, Process Intelligence", "location": "Zapopan, Jalisco",
            "loc_class": "jalisco", "url": "https://example.com/3", "posted_at": "2026-11-08", "cv_filename": cv_name,
            "is_career_upgrade": True, "_upgrades_classified": True,
            "upgrade_reason": f"{fj.HEURISTIC_UPGRADE_REASON_LEAD} (Senior Director, Process Intelligence) at Flex.",
            "match_assessment": {
                "is_good_match": True, "match_score": 91, "is_career_upgrade": True,
                "upgrade_reason": "Moves you to multi-site scope.", "match_rationale": why,
                "industry_synergy": "EMS scale mirrors your operations background.",
                "matched_keywords": ["Celonis"], "match_source": "gemini",
            },
            "llm_analysis": {"match_score": 91, "lucy_why_it_fits": why, "matched_keywords": ["Celonis"],
                             "cv": {"target_direction": "Head of Hyperautomation Programs"}},
            **employer,
        }
        self.assertEqual(fj.written_upgrade_reason(reviewed), "Moves you to multi-site scope.")
        cv_msg = fj.build_email_message([reviewed], 1, sub, date(2026, 11, 8), "sender@gmail.com",
                                        attachments=[(cv_name, b"docx bytes")])
        cv_body = cv_msg.get_body(preferencelist=("html",)).get_content()
        cv_plain = cv_msg.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("&#9733; 91% CV Match", cv_body)
        self.assertIn("&uarr; Moves you to multi-site scope.", cv_body)
        self.assertEqual(cv_body.count("Lucy&rsquo;s Headhunter Take &amp; Tailored CV"), 1)
        self.assertEqual(cv_body.count(fj.html.escape(why)), 1)
        self.assertIn("Industry &amp; Company Fit:</span> EMS scale mirrors your operations background.", cv_body)
        self.assertEqual(cv_body.count("&#128206; CV attached</span>"), 1)                # a badge on the card
        for cut in ("Celonis", "Head of Hyperautomation Programs", "Process_Intelligence.docx"):
            self.assertNotIn(cut, cv_body)
        self.assertIn(f"  Attached CV: {cv_name}", cv_plain)
        self.assertIn("  Upgrade: Moves you to multi-site scope.", cv_plain)
        self.assertNotIn("Head of Hyperautomation Programs", cv_plain)
        self.assertEqual([a.get_filename() for a in cv_msg.iter_attachments()], [cv_name])

        # 7. An over-long token (such as a CV file name in Lucy's play) gets invisible break points
        #    so a phone can wrap it instead of widening the email; ordinary text is only escaped
        zwsp = "\u200b"
        self.assertEqual(fj.email_text("Send CV_Quentin_Tester_Flex_Director.docx now"),
                         f"Send CV_{zwsp}Quentin_{zwsp}Tester_{zwsp}Flex_{zwsp}Director.{zwsp}docx now")
        self.assertEqual(fj.email_text("x" * 60), "x" * 24 + zwsp + "x" * 24 + zwsp + "x" * 12)
        self.assertEqual(fj.email_text("Director, Global Process Excellence & Automation"),
                         "Director, Global Process Excellence &amp; Automation")
        self.assertEqual(fj.email_text(None), "")
        self.assertNotIn(zwsp, body)                                                   # nothing in this email needed it

        # 8. Gmail hides the end of an email whose HTML is larger than about 102 KB. The largest
        #    email sent (max_email_jobs roles, each with long texts, a CV marker and a like link)
        #    has to stay below that.
        long_text = "Your process-mining programme across forty sites maps directly onto this multi-site remit. " * 3
        full_house = [
            dict(reviewed, key=f"greenhouse:Acme:{n}", title=f"Senior Director, Supply Chain Process Intelligence {n}",
                 url=f"https://careers.example.com/jobs/senior-director-supply-chain-process-intelligence-{n}",
                 match_assessment=dict(reviewed["match_assessment"], match_rationale=long_text[:260], industry_synergy=long_text[:200]),
                 llm_analysis=dict(reviewed["llm_analysis"], lucy_why_it_fits=long_text[:260]))
            for n in range(fj.CFG_DEFAULTS["max_email_jobs"])
        ]
        with patch.dict(os.environ, {"GMAIL_APP_PASSWORD": "app-secret-password"}):
            big = fj.build_email_message(full_house, 40, sub, date(2026, 11, 8), "sender@gmail.com",
                                         attachments=[(cv_name, b"docx bytes")], held_count=2, overflow_count=5)
        big_body = big.get_body(preferencelist=("html",)).get_content()
        self.assertEqual(big_body.count("Apply &rarr;"), len(full_house))
        self.assertEqual(big_body.count(">Like this role</a>"), len(full_house))
        self.assertLess(len(big_body.encode("utf-8")), 95_000)

        # 9. Onboarding page: pill toggles with short labels, no hero / cards / dropzone, invite code collapsed
        page = ob.render_onboard_page({})
        self.assertIsNone(pictograph.search(page))
        self.assertIn('class="pills" id="levelChecks"', page)
        self.assertIn('"short": "Specialist"', page)
        self.assertIn('"short": "Mexico, city not listed"', page)
        self.assertIn('id="inviteToggle"', page)
        self.assertIn('"require_invite_code": false', page)
        for old_chrome in ("dropzone", 'class="hero"', 'class="card'):
            self.assertNotIn(old_chrome, page)
        self.assertIn('"require_invite_code": true', ob.render_onboard_page({"onboard_require_invite_code": True}))
        for opt in ob.SENIORITY_OPTIONS + ob.DOMAIN_OPTIONS + ob.LOCATION_OPTIONS:
            self.assertIn(opt["id"], ob.OPTION_SHORT_LABELS, f"Missing short pill label for {opt['id']}")

    def test_fallback_note_and_play_never_point_at_a_cv_that_is_not_there(self):
        """When Gemini does not write Lucy's note, built-in texts are used. On a day without a
        tailored CV neither the note nor the 60-second play may tell the reader to open or send
        one; with a CV attached the note for the day's angle is used as before; and a quiet-day
        play mentions a tailored CV only once one has been built for the subscriber."""
        from datetime import timedelta

        sub = make_subscriber()
        job = {"key": "k1", "title": "Director, Platform", "company": "Gamma", "location": "Remote - Mexico",
               "url": "https://example.com/1", "posted_at": "2026-11-08"}
        claim = fj._TAILORED_CV_CLAIM_RE
        day = date(2026, 11, 8)

        def rel(angle, edition, cvs_so_far=0):
            return {"edition": edition, "streak_days": 1, "total_roles_surfaced": 3, "total_cvs_tailored": cvs_so_far,
                    "recent_angle_ids": [], "recent_headlines": [], "recent_story_beats": [], "angle_id": angle,
                    "board_count": 109, "milestone_badge": ""}

        def shown(pack):
            return " ".join(pack[k] for k in ("headline", "message", "micro_challenge"))

        # The built-in texts really do contain such claims, so the check below is not vacuous
        self.assertGreaterEqual(sum(bool(claim.search(" ".join(note))) for note in fj.DAILY_INSIDER_VAULT), 10)
        self.assertTrue(all(claim.search(play) for play in fj.DAILY_MICRO_CHALLENGES))
        self.assertFalse(any(claim.search(play) for play in fj.DAILY_MICRO_CHALLENGES_NO_CV))
        self.assertEqual(len(fj.QUIET_DAY_CHALLENGES_NO_CV), 4)

        # Every angle and every play slot on a day with roles
        for angle in range(len(fj.DAILY_INSIDER_VAULT)):
            for edition in range(1, 9):
                without_cv = fj.generate_daily_briefing_pack(sub, job, 1, 1, 2, day, rel_ctx=rel(angle, edition), cvs_today_count=0)
                self.assertIsNone(claim.search(shown(without_cv)), shown(without_cv))
                with_cv = fj.generate_daily_briefing_pack(sub, job, 1, 1, 2, day, rel_ctx=rel(angle, edition, 1), cvs_today_count=1)
                self.assertEqual(with_cv["headline"], fj.DAILY_INSIDER_VAULT[angle][0])
                self.assertIsNotNone(claim.search(with_cv["micro_challenge"]))

        # Skipping a note never makes a headline repeat while unused notes remain: 20 notes make
        # no CV claim, and 20 CV-less days in a row use each of them once
        state, headlines = {}, []
        for offset in range(20):
            today = date(2026, 11, 1) + timedelta(days=offset)
            rel_ctx = fj.peek_relationship_context(state, 2, 0, today, board_count=109)
            pack = fj.generate_daily_briefing_pack(sub, job, 1, 1, 2, today, rel_ctx=rel_ctx, cvs_today_count=0)
            headlines.append(pack["headline"])
            fj.commit_relationship_state(state, rel_ctx, pack, [job], today)
        self.assertEqual(len(set(headlines)), 20)

        # Quiet day: no tailored-CV play before the first CV exists; afterwards those plays are allowed
        before = [fj.generate_daily_briefing_pack(sub, None, 0, 0, 0, day, rel_ctx=rel(0, e), quiet_day=True)["micro_challenge"]
                  for e in range(1, 13)]
        after = [fj.generate_daily_briefing_pack(sub, None, 0, 0, 0, day, rel_ctx=rel(0, e, 3), quiet_day=True)["micro_challenge"]
                 for e in range(1, 13)]
        self.assertFalse(any(claim.search(play) for play in before))
        self.assertTrue(any(claim.search(play) for play in after))

    def test_welcome_email_shares_the_briefing_design_and_promises_only_what_is_switched_on(self):
        """The welcome email is assembled from the same parts as the daily briefing (frame,
        masthead, Lucy's card, section, card). It lists the subscriber's settings, escapes every
        submitted value, and mentions tailored CVs only when they are switched on for that person."""
        import onboard_subscriber as ob

        def welcome(**overrides):
            result = {
                "subscriber_id": "sofia", "updated_existing": False, "name": "Sofia Ramirez", "email": "sofia@example.com",
                "career_goal": "Director roles in process mining and automation, remote in Mexico.",
                "levels": ["manager", "director"], "domains": ["strategy_process"], "locations": ["remote_mx"],
                "language": "Spanish", "employers_indexed": ["AstraZeneca", "HP Inc."], "tailored_cvs_enabled": True,
            }
            result.update(overrides)
            msg = ob.build_onboarding_confirmation_email(result, "sender@gmail.com", board_count=109)
            return (msg, msg.get_body(preferencelist=("html",)).get_content(),
                    msg.get_body(preferencelist=("plain",)).get_content())

        msg, body, plain = welcome()
        daily = fj.build_email_message([], 0, make_subscriber(), date(2026, 11, 8), "sender@gmail.com")
        daily_body = daily.get_body(preferencelist=("html",)).get_content()

        # 1. Same frame and same parts as the daily briefing, built from inline styles only
        self.assertEqual(body.split("<title>")[0], daily_body.split("<title>")[0])            # identical head
        for shared in (
            fj.email_masthead("STATUS").split("STATUS")[0],                                       # Lucy's lockup
            f'bgcolor="{fj.EMAIL_NIGHT}"',                                                        # her dark card
            f"border-left:3px solid {fj.EMAIL_NIGHT_ACCENT}",                                     # ...with its inset panel
            "max-width:640px",
        ):
            self.assertIn(shared, body)
            self.assertIn(shared, daily_body)
        for unwanted in ("<style", "<script", "<img", "<link", "url(", "@import"):
            self.assertNotIn(unwanted, body)

        # 2. What it says: status, Lucy's card with "what happens next", and the radar settings
        self.assertEqual(msg["To"], "sofia@example.com")
        self.assertIn("Welcome to Jobs Sentinel, Sofia", msg["Subject"])
        self.assertIn("RADAR LIVE &amp; ARMED", body)
        self.assertIn("Welcome to your unfair advantage, Sofia.", body)
        self.assertIn(">What happens next</div>", body)
        self.assertIn(">Your Calibrated Radar Blueprint</div>", body)
        self.assertIn("Director roles in process mining and automation, remote in Mexico.", body)
        for label, value in (("Target Seniority", "Manager, Director / Head"), ("Functional Domains", "Strategy, Consulting &amp; Process"),
                             ("Geographic Scope", "Remote in Mexico"), ("Briefing Language", "Spanish"),
                             ("CV Employers Enriched", "AstraZeneca, HP Inc.")):
            self.assertRegex(body, re.escape(f">{label}</td>") + r"<td [^>]*>" + re.escape(value) + "</td>")
        self.assertIn("PROFILE RECALIBRATED", welcome(updated_existing=True)[1])
        self.assertIn("A, B, C, D, E, F (+2 more)", welcome(employers_indexed=list("ABCDEFGH"))[1])

        # 3. Tailored CVs are promised when they are on...
        self.assertIn("ATS-ready Word (.docx) CVs", body)
        self.assertIn("tailored CVs for your top picks", body)
        self.assertIn("ATS-ready Microsoft Word (.docx) CVs", plain)
        self.assertNotIn(fj.EMAIL_AMBER_SOFT, body)                                               # no amber note
        # ...and not when the CV could not be structured: one amber note says so, nothing else
        # mentions a tailored CV, and the placeholder heading is not listed as an employer
        _, off_body, off_plain = welcome(tailored_cvs_enabled=False, employers_indexed=[ob.PLACEHOLDER_EMPLOYER])
        self.assertEqual(off_body.count(fj.html.escape(ob.CV_NOT_STRUCTURED_NOTE)), 1)
        self.assertIn(f"background-color:{fj.EMAIL_AMBER_SOFT}", off_body)
        self.assertIn(ob.CV_NOT_STRUCTURED_NOTE, off_plain)
        for part in (off_body, off_plain):
            self.assertEqual(part.count("tailored CV"), 1)                                        # only inside the note
            for promise in ("ATS-ready", "top picks", "structured master CV", ob.PLACEHOLDER_EMPLOYER):
                self.assertNotIn(promise, part)
            self.assertIn("Not identified yet", part)

        # 4. Everything submitted is escaped, and an over-long word can wrap on a phone
        _, odd_body, _ = welcome(name="Zoe <script>alert(1)</script>", subscriber_id="zoe_<b>x</b>", language="<u>K</u>",
                                 career_goal="Goal <img src=x onerror=alert(1)> " + "y" * 60,
                                 employers_indexed=["Acme <i>Corp</i> & Sons"])
        for markup in ("<script", "<img", "<b>", "<i>", "<u>"):
            self.assertNotIn(markup, odd_body)
        self.assertIn("zoe_&lt;b&gt;x&lt;/b&gt;", odd_body)
        self.assertIn("Acme &lt;i&gt;Corp&lt;/i&gt; &amp; Sons", odd_body)
        self.assertIn("y" * 24 + "\u200b" + "y" * 24, odd_body)

    # ----------------------------------------------------------------------------------------
    # Match gate fails closed (item 3)
    # ----------------------------------------------------------------------------------------

    @patch("smtplib.SMTP_SSL")
    def test_gate_outage_holds_roles_and_releases_them_next_run(self, mock_smtp_ssl):
        """With a Gemini key configured, a role is only emailed after the gate approved it. When the
        review cannot run, roles are held (not sent, not recorded) and the email says so instead
        of claiming a quiet day; they are delivered once the gate answers again."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password", "GEMINI_API_KEY": "fake-gemini-key"}

        def last_msg():
            return mock_server.send_message.call_args_list[-1][0][0]

        def parts(msg):
            return (msg.get_body(preferencelist=("html",)).get_content(),
                    msg.get_body(preferencelist=("plain",)).get_content())

        cfg = make_cfg()
        sub_id = cfg["subscribers"][0]["id"]
        subscriber_cfg = fj.get_subscribers(cfg)[0]

        def sub_state(root):
            return fj.load_json(root / "state" / "seen.json", {})["subscribers"][sub_id]

        # --- Day 1: every Gemini call fails -> nothing is sent unreviewed, nothing is recorded
        fj.http_json = fake_http()
        with patch.dict(os.environ, env), \
             patch("fetch_jobs._invoke_gemini_json", side_effect=RuntimeError("gemini 503")) as gemini_calls:
            code = fj.refresh(cfg, COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True)
        self.assertEqual(code, 0)
        self.assertEqual(gemini_calls.call_count, 1)      # the failed review; no second attempt for the briefing prose
        msg1 = last_msg()
        html1, plain1 = parts(msg1)
        self.assertIn("AI review paused", msg1["Subject"])
        held = int(re.search(r"(\d+) new roles on hold", msg1["Subject"]).group(1))
        self.assertTrue(5 <= held <= fj.GATE_BATCH_SIZE, held)     # several fixture roles, all in one review batch
        self.assertNotIn("Quiet day", msg1["Subject"])
        self.assertIn(f"AI review paused: {held} new roles passed your title and location rules", html1)
        self.assertIn("nothing is sent unreviewed", plain1)
        self.assertNotIn("Quiet day on the radar", html1)
        self.assertNotIn("zero never-seen postings", html1)
        self.assertNotIn("\n- ", plain1)                          # no role is listed
        self.assertNotIn("% CV Match", html1)
        self.assertEqual(list(msg1.iter_attachments()), [])
        self.assertTrue(any(fj.html.escape(headline) in html1 for headline, _ in fj.HELD_DAY_VAULT))
        self.assertTrue(all(len(headline) <= 46 for headline, _ in fj.HELD_DAY_VAULT))   # fits the preheader spark uncut
        # Lucy's own words must not claim work that did not happen today. The email shows her
        # note and one play; on a paused first day neither may mention filtering or a tailored CV.
        self.assertTrue(any(fj.html.escape(play) in html1 for play in fj.QUIET_DAY_CHALLENGES_NO_CV))
        for claim in ("filtered out hundreds", "CV(s) armed", "custom CVs forged so far", "every CV attached below",
                      "latest tailored CV", "CV I tailored"):
            self.assertNotIn(claim, html1)
        # Her connection note is no longer shown in the email, but the pack keeps it truthful
        paused_pack = fj.generate_daily_briefing_pack(subscriber_cfg, None, 0, 0, 0, date(2026, 11, 5),
                                                      quiet_day=True, held_count=held)
        self.assertIn("the fit review did not run overnight, so I have nothing vetted to show you yet",
                      paused_pack["connection_ps"])
        state1 = sub_state(self.root)
        self.assertEqual(state1["emailed"], {})                   # held roles are not marked as sent...
        self.assertEqual(state1["rejected_matches"], {})          # ...nor as rejected
        self.assertEqual(state1["relationship"]["edition"], 1)

        # --- Day 2: the gate answers again -> the same roles are reviewed and delivered
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=approve_all_gate), \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            code = fj.refresh(cfg, COMPANIES, root=self.root, today=date(2026, 11, 6), send_email=True)
        self.assertEqual(code, 0)
        msg2 = last_msg()
        html2, plain2 = parts(msg2)
        self.assertIn(f"{held} new roles for you", msg2["Subject"])
        self.assertEqual(plain2.count("\n- "), held)
        self.assertIn("90% CV Match", html2)
        self.assertNotIn("AI review paused", html2)
        self.assertEqual(len(sub_state(self.root)["emailed"]), held)

        # --- A reply that leaves one job out: that job is held, the rest are delivered
        def gate_skipping_first_job(prompt_text, **kwargs):
            reply = approve_all_gate(prompt_text)
            reply["assessments"] = [a for a in reply["assessments"] if a["id"] != 0]
            return reply

        partial_root = Path(self.tmp.name) / "partial"
        partial_root.mkdir()
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=gate_skipping_first_job), \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            fj.refresh(cfg, COMPANIES, root=partial_root, today=date(2026, 11, 5), send_email=True)
        msg3 = last_msg()
        html3, plain3 = parts(msg3)
        self.assertIn(f"{held - 1} new roles for you", msg3["Subject"])
        self.assertEqual(plain3.count("\n- "), held - 1)
        self.assertIn("1 more new role could not be AI-reviewed today and will follow in a later briefing.", html3)
        self.assertIn("1 more new role could not be AI-reviewed today", plain3)
        self.assertEqual(len(sub_state(partial_root)["emailed"]), held - 1)
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=approve_all_gate), \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            fj.refresh(cfg, COMPANIES, root=partial_root, today=date(2026, 11, 6), send_email=True)
        self.assertIn("1 new role for you", last_msg()["Subject"])
        self.assertEqual(len(sub_state(partial_root)["emailed"]), held)

        # --- A job left out once is asked about again in the same run, so it is not delayed a day
        batch = [{"key": f"k{i}", "title": f"Director, Platform {i}", "company": "Gamma", "location": "Remote - Mexico",
                  "url": f"https://example.com/{i}"} for i in range(3)]
        prompts = []

        def forgetful_then_complete(prompt_text, **kwargs):
            prompts.append(prompt_text)
            reply = approve_all_gate(prompt_text)
            if len(prompts) == 1:
                reply["assessments"] = reply["assessments"][:-1]       # first reply omits the last job
            return reply

        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=forgetful_then_complete):
            gate = fj.assess_candidate_job_matches(batch, subscriber_cfg)
        self.assertEqual((len(gate.kept), gate.rejected, gate.held, gate.status), (3, [], [], fj.GATE_REVIEWED))
        self.assertEqual(len(prompts), 2)
        self.assertIn("Director, Platform 2", prompts[1])
        self.assertNotIn("Director, Platform 0", prompts[1])              # only the omitted job is asked again

        # --- More new roles than the review budget while Gemini is down: still no unreviewed role
        #     leaves the system, not even on an attached "personal board"
        big_feed = {"jobs": [
            {"id": 500 + i, "title": f"Director, Program {i}", "location": {"name": "Guadalajara, Mexico"},
             "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{500 + i}"}
            for i in range(30)
        ]}
        big_root = Path(self.tmp.name) / "big"
        big_root.mkdir()
        fj.http_json = fake_http({"greenhouse.io": big_feed})
        with patch.dict(os.environ, env), patch("fetch_jobs._invoke_gemini_json", side_effect=RuntimeError("gemini 503")):
            fj.refresh(cfg, [{"name": "Acme", "ats": "greenhouse", "board": "acme"}], root=big_root,
                       today=date(2026, 11, 5), send_email=True)
        msg4 = last_msg()
        html4, plain4 = parts(msg4)
        self.assertIn("AI review paused", msg4["Subject"])
        self.assertIn("30 new roles on hold", msg4["Subject"])      # 24 could not be reviewed + 6 beyond the daily budget
        self.assertEqual(list(msg4.iter_attachments()), [])
        self.assertNotIn("personal board", plain4)
        self.assertNotIn("Director, Program", html4)
        fj.http_json = fake_http()

        # --- An outage must not stretch the run: after two failed gate calls in a row the run stops
        #     calling Gemini, and every remaining subscriber's roles are held at once
        many_cfg = make_cfg(*(make_subscriber(name) for name in ("ana", "ben", "cy", "dee")))
        outage_root = Path(self.tmp.name) / "outage"
        outage_root.mkdir()
        sent_before = mock_server.send_message.call_count
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=RuntimeError("gemini 503")) as failing_gate, \
             patch("fetch_jobs._invoke_gemini_json", side_effect=RuntimeError("gemini 503")):
            self.assertEqual(fj.refresh(many_cfg, COMPANIES, root=outage_root, today=date(2026, 11, 5), send_email=True), 0)
        self.assertEqual(failing_gate.call_count, fj.GATE_MAX_CONSECUTIVE_FAILURES)      # not one call per subscriber
        outage_msgs = [c[0][0] for c in mock_server.send_message.call_args_list[sent_before:]]
        self.assertEqual(len(outage_msgs), 4)
        self.assertTrue(all("AI review paused" in m["Subject"] for m in outage_msgs))
        # The breaker belongs to one run and closes again after a good reply
        breaker = {"failures": 1}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=approve_all_gate):
            gate = fj.assess_candidate_job_matches([dict(batch[0])], subscriber_cfg, breaker=breaker)
        self.assertEqual((len(gate.kept), breaker["failures"]), (1, 0))

        # --- The gate reports what happened: nothing kept or rejected, one held, status unavailable
        job = {"key": "k1", "title": "Director, Platform", "company": "Gamma", "location": "Remote - Mexico", "url": "https://example.com/1"}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}), \
             patch("fetch_jobs._invoke_gemini_json", side_effect=RuntimeError("gemini 503")):
            gate = fj.assess_candidate_job_matches([dict(job)], subscriber_cfg)
        self.assertEqual((gate.kept, gate.rejected, len(gate.held), gate.status), ([], [], 1, fj.GATE_UNAVAILABLE))

    @patch("smtplib.SMTP_SSL")
    def test_without_gemini_key_roles_are_labelled_unreviewed_and_carry_no_score(self, mock_smtp_ssl):
        """No Gemini key at all: roles still go out (title and location rules only) but the email
        says they were not AI-reviewed and shows no percentage or rationale. No score or
        rationale is ever invented."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        cfg = make_cfg()
        fj.http_json = fake_http()
        with patch.dict(os.environ, {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}):
            code = fj.refresh(cfg, COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True)
        self.assertEqual(code, 0)
        msg = mock_server.send_message.call_args_list[0][0][0]
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        plain_part = msg.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Not AI-reviewed", html_part)
        self.assertNotIn("% CV Match", html_part)
        self.assertNotIn("% Match", plain_part)
        self.assertIn("Not AI-reviewed: matched by your title and location rules only.", plain_part)
        self.assertIn("AI review is not set up", html_part)
        self.assertNotIn("AI-validated", html_part)
        self.assertNotIn("maps directly", html_part)
        # TLS: the SMTP connection that carries the mail password verifies the server certificate
        smtp_context = mock_smtp_ssl.call_args.kwargs["context"]
        self.assertEqual(smtp_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(smtp_context.check_hostname)

        subscriber_cfg = fj.get_subscribers(cfg)[0]
        cv_txt = fj.load_subscriber_cv(subscriber_cfg)
        for off_target in ("Director of Nursing", "Director, Tax Accounting", "Director, Clinical Oncology Wet-Lab Research"):
            job = {"key": "k", "title": off_target, "company": "Example Hospital", "location": "Guadalajara, Mexico"}
            assessment = fj.build_unreviewed_match_assessment(job, cv_txt, subscriber_cfg)
            self.assertIsNone(assessment["match_score"], off_target)
            self.assertIsNone(assessment["is_good_match"], off_target)
            self.assertEqual(assessment["match_rationale"], "")
            self.assertEqual(assessment["match_source"], "unreviewed")
            self.assertEqual(assessment["matched_keywords"], [], off_target)   # nothing overlaps, so nothing is claimed

            tailored = fj.tailor_cv_deterministic(dict(job), cv_txt, subscriber_cfg)
            self.assertIsNone(tailored["match_score"], off_target)
            self.assertEqual(tailored["lucy_why_it_fits"], "")
            self.assertEqual(tailored["matched_keywords"], [], off_target)
        # Only skills that literally appear in the posting are reported, never a back-filled list
        on_target = {"key": "k", "title": "Director, Process Mining", "company": "Flex"}
        self.assertEqual(fj.build_unreviewed_match_assessment(on_target, cv_txt, subscriber_cfg)["matched_keywords"], ["Process Mining"])

        # A score reported by the gate is passed through unchanged
        reviewed_job = {"key": "k", "title": "Director, Process Mining", "company": "Flex", "location": "Zapopan",
                        "match_assessment": {"match_score": 88, "match_rationale": "Real gate rationale."}}
        tailored = fj.tailor_cv_deterministic(reviewed_job, cv_txt, subscriber_cfg)
        self.assertEqual(tailored["match_score"], 88)
        self.assertEqual(tailored["lucy_why_it_fits"], "Real gate rationale.")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            gate = fj.assess_candidate_job_matches([{"key": "k", "title": "Director of Nursing", "company": "Example Hospital"}], subscriber_cfg)
        self.assertEqual((len(gate.kept), gate.rejected, gate.held, gate.status), (1, [], [], fj.GATE_NOT_CONFIGURED))

    # ----------------------------------------------------------------------------------------
    # Pattern safety (item 7) and narrower sign-up rules (item 4)
    # ----------------------------------------------------------------------------------------

    def test_slow_patterns_are_refused_and_titles_are_capped(self):
        """A pattern with a quantifier inside a repeated group can take minutes on one long word.
        Such patterns are skipped (with a warning), titles are capped before matching, and the
        model can no longer supply patterns at all: only plain phrases, escaped in code."""
        import onboard_subscriber as ob

        slow = r"\bdata(\s*\w+)*\s*lead\b"
        for pattern in (slow, r"(a+)+", r"(a*)*b", r"((ab)*)+", r"(?:\w+\s?)+manager", r"(\w+){2,}z", r"(?:(?:a|b)+)*c"):
            re.compile(pattern)
            self.assertTrue(fj.has_nested_quantifier(pattern), pattern)
        for pattern in (r"\bdirector(a)?\b", r"\bchief\b.*\bofficer\b", r"\b(foo|bar)+\b", r"[(]a+[)]+", r"\(a+\)+",
                        r"\b(senior manager|sr\.? manager|gerente (senior|sr\.?))\b", r"\ba{2,5}\b", r"\b(ab){0,1}c\b"):
            self.assertFalse(fj.has_nested_quantifier(pattern), pattern)

        # Every pattern built into the code must pass the check (a false positive would silently
        # change matching). Patterns in config.json and subscribers/ are deliberately not asserted
        # here: they are checked when loaded (skipped with a warning), so a pattern somebody adds
        # there can never fail this suite and block the daily run.
        shipped = list(fj.DEFAULT_UPGRADE_PATTERNS) + list(ob.BASE_TITLE_EXCLUDES)
        shipped += [ob.NOISE_EXCLUDE_UNLESS_SALES, ob.NOISE_EXCLUDE_UNLESS_SPECIALIST]
        for opt in ob.SENIORITY_OPTIONS:
            shipped += opt["inc"] + opt["up"]
        for opt in ob.DOMAIN_OPTIONS:
            shipped += [rf"\b{opt[key]}\b" for key in ("keywords_re", "ic_titles") if opt[key]]
        self.assertGreater(len(shipped), 40)
        for pattern in shipped:
            re.compile(pattern, re.I)
            self.assertFalse(fj.has_nested_quantifier(pattern), pattern)

        # The slow pattern is dropped from the rules, so a hostile title is answered instantly
        rules = fj.build_rules({"title_include": [slow, r"\bdirector\b"], "title_exclude": [slow], "jalisco_locations": ["guadalajara"]})
        self.assertEqual(len(rules["inc"]), 1)
        self.assertEqual(rules["exc"], [])
        hostile = "data " + "x" * 40 + " " + "y" * 40 + " !"
        started = time.perf_counter()
        self.assertFalse(fj.is_director(hostile, rules))
        self.assertTrue(fj.is_director("Director of Data", rules))
        is_up, _ = fj.is_career_upgrade_heuristic({"title": hostile}, {"upgrade_title_include": [slow, "[unclosed", r"\bdirector\b"]})
        self.assertFalse(is_up)
        self.assertLess(time.perf_counter() - started, 1.0)

        # Titles are capped before matching
        self.assertTrue(fj.is_director("x" * 150 + " director", rules))
        self.assertFalse(fj.is_director("x" * (fj.MAX_TITLE_CHARS + 10) + " director", rules))

        # Non-text entries never raise
        self.assertEqual(len(fj.build_rules({"title_include": [None, 7, r"\bdirector\b"]})["inc"]), 1)

        # Model output is treated as plain phrases: regex syntax is discarded, never compiled
        self.assertEqual(ob.clean_phrase("  Process   Mining Lead "), "process mining lead")
        self.assertEqual(ob.clean_phrase("Gestión del Cambio"), "gestion del cambio")
        for not_a_phrase in (r"\bdata(\s*\w+)*\s*lead\b", "(a+)+", ".*", "[a-z]+", "x", "", None, 42, "a" * 80,
                             "one two three four five six seven"):
            self.assertEqual(ob.clean_phrase(not_a_phrase), "", repr(not_a_phrase))
        pattern = ob.phrase_to_pattern(ob.clean_phrase("C++ Developer"))
        self.assertTrue(re.search(pattern, fj.norm("Senior C++ Developer (Remote)")))
        self.assertFalse(re.search(pattern, fj.norm("Senior C Developer")))
        self.assertTrue(re.search(ob.phrase_to_pattern("process mining"), "head of process-mining"))
        self.assertFalse(re.search(ob.phrase_to_pattern("process mining"), "reprocess mining ops"))
        self.assertFalse(fj.has_nested_quantifier(pattern))
        self.assertNotIn("regex patterns", ob.ONBOARD_PROFILE_SYSTEM_PROMPT)
        self.assertIn("Never write regular expressions", ob.ONBOARD_PROFILE_SYSTEM_PROMPT)

    def test_form_signup_rules_need_a_level_and_an_area(self):
        """Sign-up profiles accept a title when it names one of the ticked levels AND one of the
        ticked areas or the subscriber's own keywords. A level word alone ('Senior ...') is no
        longer enough, so unrelated roles stop consuming the daily AI review."""
        import onboard_subscriber as ob

        # Rules-engine semantics: include OR (every all_of group matches); exclude always wins
        rules = fj.build_rules({
            "title_include": [r"\bgrowth lead\b"],
            "title_all_of": [[r"\b(senior|manager)\b"], [r"\b(marketing|brand)\b"]],
            "title_exclude": [r"\bintern\b"],
        })
        for title, expected in (("Growth Lead", True), ("Senior Brand Strategist", True), ("Marketing Manager", True),
                                ("Senior Tax Accountant", False), ("Marketing Coordinator", False),
                                ("Senior Marketing Intern", False)):
            self.assertEqual(fj.is_director(title, rules), expected, title)
        # Subscribers without title_all_of behave exactly as before
        self.assertEqual(fj.build_rules({"title_include": [r"\bdirector\b"]})["all_of"], [])
        # A group that lost all its patterns can never match (fails closed instead of widening)
        broken = fj.build_rules({"title_all_of": [[r"\bsenior\b"], ["[unclosed"]]})
        self.assertFalse(fj.is_director("Senior Marketing Manager", broken))

        def accepts(profile, title):
            return fj.is_director(title, fj.build_rules(profile))

        # 1. The default form (Senior + Manager ticked, no area) with phrases derived from free text
        default_signup = ob.build_title_rules(["senior", "manager"], ["other"], {
            "target_titles": ["Marketing Manager", "Growth Lead"],
            "focus_keywords": ["marketing", "growth", "brand", "manager", r"(a+)+"],
            "step_up_titles": ["head of marketing"],
            "avoid_titles": ["cold calling", "manager", "marketing", r"\bx(\w+)*\b"],
        })
        self.assertEqual(default_signup["rule_breadth"], "focused")
        self.assertEqual(len(default_signup["title_all_of"]), 2)
        self.assertEqual(default_signup["interest_phrases"], ["marketing manager", "growth lead", "marketing", "growth", "brand"])
        for title in ("Marketing Manager", "Growth Lead", "Senior Brand Strategist", "Gerente de Marketing", "Lead Growth Analyst"):
            self.assertTrue(accepts(default_signup, title), title)
        for title in ("Senior Tax Accountant", "Lead Nurse", "Warehouse Shift Manager", "Senior Software Engineer",
                      "Senior SMT Process Technician", "Marketing Intern", "Cold Calling Team Lead"):
            self.assertFalse(accepts(default_signup, title), title)
        self.assertTrue(fj.is_career_upgrade_heuristic({"title": "Head of Marketing"}, default_signup)[0])
        # An "avoid" phrase can never exclude what the subscriber asked for
        self.assertFalse(any(re.search(p, "marketing manager") for p in default_signup["title_exclude"]))

        # 2. An area ticked, no model available: level AND area keywords, plus level-less IC titles
        ops = ob.build_title_rules(["senior", "manager"], ["operations"])
        self.assertEqual(ops["rule_breadth"], "focused")
        for title in ("Operations Manager", "Senior Supply Chain Planner", "Gerente de Logística", "Plant Manager", "Buyer"):
            self.assertTrue(accepts(ops, title), title)
        for title in ("Senior Tax Accountant", "Software Engineer", "Marketing Manager", "Junior Buyer"):
            self.assertFalse(accepts(ops, title), title)
        sales_ic = ob.build_title_rules(["specialist"], ["sales_customer"])
        for title in ("Account Executive", "Key Account Manager", "Sales Operations Analyst", "Ejecutivo de Ventas"):
            self.assertTrue(accepts(sales_ic, title), title)
        self.assertFalse(accepts(sales_ic, "Data Analyst"))
        # IC titles are not added for people-manager levels
        self.assertFalse(accepts(ob.build_title_rules(["manager"], ["sales_customer"]), "Account Executive"))
        self.assertTrue(accepts(ob.build_title_rules(["manager"], ["sales_customer"]), "Regional Sales Manager"))

        # 3. Director and executive titles are rare, so they go straight to the AI review
        leader = ob.build_title_rules(["director", "executive"], ["finance_legal"])
        self.assertEqual(leader["title_all_of"], [])
        for title in ("Director, Clinical Research", "Head of Tax", "VP Finance", "Country Manager"):
            self.assertTrue(accepts(leader, title), title)
        self.assertFalse(accepts(leader, "Senior Financial Analyst"))

        # 4. Nothing to narrow with (no area, no phrases): level words alone, flagged as broad
        broad = ob.build_title_rules(["senior", "manager"], ["other"])
        self.assertEqual(broad["rule_breadth"], "broad")
        self.assertEqual(broad["title_all_of"], [])
        self.assertTrue(accepts(broad, "Senior Tax Accountant"))
        # ...and a ticked director level never swallows the other ticked levels
        mixed = ob.build_title_rules(["senior", "director"], ["other"])
        self.assertEqual(mixed["rule_breadth"], "broad")
        self.assertTrue(accepts(mixed, "Senior Buyer"))
        self.assertTrue(accepts(mixed, "Director of Sales"))

        # 5. Catalogue regression table: (levels, areas, titles that must pass, titles that must not)
        catalogue_cases = [
            (["manager"], ["people_hr"],
             ["HR Manager", "Talent Acquisition Manager", "Gerente de Recursos Humanos", "People Operations Manager"],
             ["HR Business Partner", "Recruiter", "Operations Manager"]),
            (["specialist"], ["people_hr"],
             ["Recruiter", "HR Business Partner", "HR Generalist", "Analista de Recursos Humanos"],
             ["HR Manager", "Data Analyst"]),
            (["specialist", "senior"], ["finance_legal"],
             ["Finance Analyst", "Senior Financial Analyst", "Accountant", "Contador", "Legal Counsel", "FP&A Analyst"],
             ["Finance Manager", "Software Engineer", "Senior Data Analyst"]),
            (["manager"], ["technology"],
             ["IT Manager", "Engineering Manager", "Gerente de TI", "Data Analytics Manager"],
             ["Software Engineer", "Sales Manager", "Plant Manager"]),
            (["senior"], ["technology"],
             ["Senior Software Engineer", "Staff Data Engineer", "Principal Cloud Architect", "Data Scientist"],
             ["Software Engineer", "Engineering Manager", "Senior Accountant"]),
            (["senior", "manager"], ["strategy_process"],
             ["Continuous Improvement Manager", "GBS Transformation Lead", "Gerente de Mejora Continua", "Business Analyst"],
             ["Senior Tax Accountant", "Marketing Manager", "Junior Process Analyst"]),
            (["specialist", "senior"], ["marketing"],
             ["Marketing Manager", "Senior Product Designer", "UX Designer", "Marketing Analyst", "Gerente de Mercadotecnia"],
             ["Sales Manager", "Senior Accountant", "Marketing Director"]),
            (["manager"], ["sales_customer"],
             ["Sales Manager", "Gerente Comercial", "Customer Success Manager", "Business Development Manager"],
             ["Account Executive", "Sales Representative", "Marketing Manager"]),
            (["senior", "manager"], ["project_product"],
             ["Project Manager", "Program Manager, PMO", "Product Manager", "Scrum Master", "Senior Delivery Lead"],
             ["Senior Tax Accountant", "Operations Manager"]),
            (["executive"], ["other"],
             ["VP Sales", "Vice President, Operations", "General Manager", "Country Manager", "Chief Technology Officer"],
             ["Director of Engineering", "Senior Manager", "Executive Assistant"]),
        ]
        for levels, areas, must_pass, must_not in catalogue_cases:
            profile = ob.build_title_rules(levels, areas)
            for title in must_pass:
                self.assertTrue(accepts(profile, title), f"{levels} + {areas}: {title}")
            for title in must_not:
                self.assertFalse(accepts(profile, title), f"{levels} + {areas}: {title}")

        # 6. Everything the builder produces is a safe, valid pattern
        for profile in (default_signup, ops, sales_ic, leader, broad, mixed):
            patterns = profile["title_include"] + profile["title_exclude"] + profile["upgrade_title_include"]
            for group in profile["title_all_of"]:
                patterns += group
            for pattern in patterns:
                re.compile(pattern, re.I)
                self.assertFalse(fj.has_nested_quantifier(pattern), pattern)

    @patch("smtplib.SMTP_SSL")
    def test_most_relevant_roles_are_reviewed_first_when_the_queue_exceeds_the_cap(self, mock_smtp_ssl):
        """Only 24 roles are AI-reviewed per run. The queue is ordered by overlap with the
        subscriber's goal (and liked roles) before the cap, so a relevant older role is no longer
        stuck behind newer unrelated ones; the rest roll over to the next run."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        # Unit level: stable ordering, liked roles weigh more, an empty signal changes nothing
        queue = [{"title": "Director, Tax"}, {"title": "Director, Process Mining"}, {"title": "Director, Widgets"},
                 {"title": "Senior Director, Platform Engineering", "company": "Gamma"}]
        goal = {"career_goal": "Process mining and automation leadership roles", "interest_phrases": ["celonis"]}
        ranked = [j["title"] for j in fj.rank_candidates_for_review(queue, goal)]
        self.assertEqual(ranked, ["Director, Process Mining", "Director, Tax", "Director, Widgets", "Senior Director, Platform Engineering"])
        liked = [{"title": "Director, Platform", "company": "Gamma"}]
        self.assertEqual(fj.rank_candidates_for_review(queue, goal, liked)[0]["title"], "Senior Director, Platform Engineering")
        self.assertEqual(fj.rank_candidates_for_review(queue, {}), queue)

        # End to end: 24 newer unrelated roles + 6 older relevant ones, review budget 24
        feed = {"jobs": [
            {"id": 100 + i, "title": f"Director, Widget Sales {i}", "location": {"name": "Guadalajara, Mexico"},
             "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{100 + i}", "first_published": "2026-11-04T10:00:00Z"}
            for i in range(24)
        ] + [
            {"id": 200 + i, "title": f"Director, Process Mining Program {i}", "location": {"name": "Guadalajara, Mexico"},
             "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{200 + i}", "first_published": "2026-10-20T10:00:00Z"}
            for i in range(6)
        ]}
        cfg = make_cfg(make_subscriber("ana", title_include=[r"\bdirector\b"], title_exclude=[]))
        reviewed_runs = []

        def recording_gate(prompt_text, **kwargs):
            titles = re.findall(r"\[JOB id=\d+\]\nTitle: ([^\n]+)", prompt_text)
            reviewed_runs[-1].extend(titles)
            reply = approve_all_gate(prompt_text)
            for assessment, title in zip(reply["assessments"], titles):
                if "Process Mining" not in title:
                    assessment.update(is_good_match=False, match_score=15)
            return reply

        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password", "GEMINI_API_KEY": "fake-gemini-key"}
        acme_only = [{"name": "Acme", "ats": "greenhouse", "board": "acme"}]
        fj.http_json = fake_http({"greenhouse.io": feed})
        for day in (5, 6):
            reviewed_runs.append([])
            with patch.dict(os.environ, env), \
                 patch("fetch_jobs.call_gemini_job_matches", side_effect=recording_gate), \
                 patch("fetch_jobs._invoke_gemini_json", return_value=None):
                code = fj.refresh(cfg, acme_only, root=self.root, today=date(2026, 11, day), send_email=True)
            self.assertEqual(code, 0)

        first_run, second_run = reviewed_runs
        self.assertEqual(len(first_run), 24)
        self.assertEqual(sum("Process Mining" in t for t in first_run), 6)     # all six relevant roles made the cut
        self.assertEqual(len(second_run), 6)                                   # the rest rolled over
        self.assertTrue(all("Widget Sales" in t for t in second_run))
        first_email = mock_server.send_message.call_args_list[0][0][0]
        self.assertIn("6 new roles for you", first_email["Subject"])

    # ----------------------------------------------------------------------------------------
    # Profile updates (item 5) and the per-subscriber store (item 8)
    # ----------------------------------------------------------------------------------------

    @patch("smtplib.SMTP_SSL")
    def test_profile_update_clears_old_rejections_but_never_resends(self, mock_smtp_ssl):
        """Rejections were judged against the old CV and goals. When the sign-up form stamps a new
        `profile_updated_at`, the next daily run forgets them and reviews those roles again; roles
        that were already emailed are still never sent twice."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        # Unit level
        sub_state = {"rejected_matches": {"a": {"date": "2026-11-01"}, "b": {"date": "2026-11-02"}}, "emailed": {"c": {"date": "2026-11-01"}}}
        self.assertEqual(fj.forget_rejections_after_profile_update(sub_state, {}), 0)                    # no marker: untouched
        self.assertEqual(len(sub_state["rejected_matches"]), 2)
        self.assertEqual(fj.forget_rejections_after_profile_update(sub_state, {"profile_updated_at": "2026-11-07T09:00:00Z"}), 2)
        self.assertEqual(sub_state["rejected_matches"], {})
        self.assertEqual(sub_state["emailed"], {"c": {"date": "2026-11-01"}})
        self.assertEqual(fj.forget_rejections_after_profile_update(sub_state, {"profile_updated_at": "2026-11-07T09:00:00Z"}), 0)

        # End to end
        feed = {"jobs": [
            {"id": 301, "title": "Director, Process Mining", "location": {"name": "Guadalajara, Mexico"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/301", "first_published": "2026-11-04T10:00:00Z"},
            {"id": 302, "title": "Director, Clinical Research", "location": {"name": "Guadalajara, Mexico"},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/302", "first_published": "2026-11-04T10:00:00Z"},
        ]}
        subscriber = {
            "id": "ana", "name": "Ana Torres", "recipient_email": "ana@example.com", "enabled": True,
            "cv_text": "# ANA TORRES\nana@example.com\n\n## PROFESSIONAL SUMMARY\nProcess mining leader.\n",
            "career_goal": "Process mining leadership.", "title_include": [r"\bdirector\b"], "title_exclude": [],
            "max_tailored_cvs": 0,
        }
        approved_words = ["Process Mining"]
        reviewed = []

        def gate(prompt_text, **kwargs):
            titles = re.findall(r"\[JOB id=\d+\]\nTitle: ([^\n]+)", prompt_text)
            reviewed.extend(titles)
            reply = approve_all_gate(prompt_text)
            for assessment, title in zip(reply["assessments"], titles):
                if not any(word in title for word in approved_words):
                    assessment.update(is_good_match=False, match_score=20)
            return reply

        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password", "GEMINI_API_KEY": "fake-gemini-key"}
        acme_only = [{"name": "Acme", "ats": "greenhouse", "board": "acme"}]
        fj.http_json = fake_http({"greenhouse.io": feed})

        def run(day, sub):
            reviewed.clear()
            with patch.dict(os.environ, env), \
                 patch("fetch_jobs.call_gemini_job_matches", side_effect=gate), \
                 patch("fetch_jobs._invoke_gemini_json", return_value=None):
                self.assertEqual(fj.refresh(make_cfg(sub), acme_only, root=self.root,
                                            today=date(2026, 11, day), send_email=True), 0)
            msg = mock_server.send_message.call_args_list[-1][0][0]
            state = fj.load_json(self.root / "state" / "seen.json", {})["subscribers"]["ana"]
            return msg, msg.get_body(preferencelist=("plain",)).get_content(), state

        msg, plain, state = run(5, subscriber)
        self.assertIn("1 new role for you", msg["Subject"])
        self.assertIn("Director, Process Mining at Acme", plain)
        self.assertIn("greenhouse:Acme:302", state["rejected_matches"])

        msg, plain, state = run(6, subscriber)                     # same profile: the rejection is remembered
        self.assertEqual(reviewed, [])
        self.assertIn("Quiet day", msg["Subject"])

        approved_words.append("Clinical Research")                 # the new profile wants clinical roles
        updated = {**subscriber, "career_goal": "Clinical research leadership.", "profile_updated_at": "2026-11-07T09:00:00Z"}
        msg, plain, state = run(7, updated)
        self.assertEqual(reviewed, ["Director, Clinical Research"])            # reviewed again; the sent role is not
        self.assertIn("1 new role for you", msg["Subject"])
        self.assertIn("Director, Clinical Research at Acme", plain)
        self.assertNotIn("Director, Process Mining at Acme", plain)            # already emailed: never re-sent
        self.assertEqual(state["profile_seen_at"], "2026-11-07T09:00:00Z")
        self.assertEqual(state["rejected_matches"], {})

        msg, plain, state = run(8, updated)                        # marker unchanged: nothing is reset again
        self.assertEqual(reviewed, [])
        self.assertIn("Quiet day", msg["Subject"])

    def test_subscriber_files_merge_and_signups_never_touch_shared_files(self):
        """Each sign-up is stored as its own `subscribers/<id>.json` + `cvs/<id>.md`. config.json
        is never edited, so two sign-ups cannot overwrite each other; damaged files are skipped;
        and a profile that would break the daily run is refused before anything is written."""
        import onboard_subscriber as ob

        root = self.root
        inline_ids = ["lucas", "luis"]
        (root / "config.json").write_text(json.dumps({"subscribers": [
            {"id": sub_id, "name": sub_id.title(), "recipient_email": f"{sub_id}@example.com", "title_include": [r"\bdirector\b"]}
            for sub_id in inline_ids
        ]}), encoding="utf-8")
        config_before = (root / "config.json").read_bytes()

        def payload(name, email):
            return ob.validate_onboarding_payload({
                "name": name, "email": email,
                "cv_text": (
                    f"{name}\nGuadalajara, MX | {email}\n"
                    "Operations leader with 9 years running supply chain and logistics teams across Jalisco.\n"
                    "- Led a 40-person warehouse operation and cut order cycle time by 18 percent.\n"
                    "- Managed procurement for three plants and renegotiated supplier contracts.\n"
                ),
                "levels": ["senior", "manager"], "domains": ["operations"], "locations": ["jalisco"],
            })

        # Two people with the same first name sign up; neither touches a shared file
        first = ob.register_subscriber_in_codebase(payload("Sofia Ramirez", "sofia.ramirez@example.com"), root=root, api_key="")
        second = ob.register_subscriber_in_codebase(payload("Sofia Lopez", "sofia.lopez@example.com"), root=root, api_key="")
        self.assertEqual((first["subscriber_id"], second["subscriber_id"]), ("sofia", "sofia_lopez"))
        self.assertEqual((first["record_file"], first["cv_file"]), ("subscribers/sofia.json", "cvs/sofia.md"))
        self.assertEqual((root / "config.json").read_bytes(), config_before)
        self.assertEqual(sorted(f.name for f in (root / "subscribers").iterdir()), ["sofia.json", "sofia_lopez.json"])
        self.assertFalse((root / "docs").exists())
        self.assertFalse(any(f.name.startswith(".") for f in (root / "cvs").iterdir()))     # no temp files left behind

        # The same email again updates the same id (never a duplicate) and re-stamps the profile
        again = ob.register_subscriber_in_codebase(payload("Sofia R.", "sofia.ramirez@example.com"), root=root, api_key="")
        self.assertEqual((again["subscriber_id"], again["updated_existing"]), ("sofia", True))
        self.assertEqual(len(list((root / "subscribers").iterdir())), 2)

        # load_config merges config.json with the files; order is stable
        cfg = fj.load_config(root)
        self.assertEqual([s["id"] for s in cfg["subscribers"]], inline_ids + ["sofia", "sofia_lopez"])
        sofia = next(s for s in fj.get_subscribers(cfg) if s["id"] == "sofia")
        self.assertEqual(sofia["rule_breadth"], "focused")
        self.assertTrue(sofia["profile_updated_at"])
        self.assertIn("Company Profile:", fj.load_subscriber_cv(sofia, root=root))
        sofia_rules = fj.build_rules(sofia)
        self.assertTrue(fj.is_director("Operations Manager", sofia_rules))
        self.assertFalse(fj.is_director("Senior Tax Accountant", sofia_rules))
        self.assertEqual(ob.profile_problems(sofia, fj.load_subscriber_cv(sofia, root=root)), [])

        # A file with the same id as a config.json entry replaces it in place (that person re-submitted the form)
        (root / "subscribers" / "lucas.json").write_text(json.dumps({
            "id": "lucas", "name": "Lucas Updated", "recipient_email": "lucas@example.com",
            "title_include": [r"\bdirector\b"],
        }), encoding="utf-8")
        # Damaged or mis-named files are skipped instead of stopping the run for everyone
        (root / "subscribers" / "broken.json").write_text("{not json", encoding="utf-8")
        (root / "subscribers" / "mismatch.json").write_text(json.dumps({"id": "someone_else", "name": "X"}), encoding="utf-8")
        (root / "subscribers" / "list.json").write_text("[]", encoding="utf-8")
        cfg = fj.load_config(root)
        self.assertEqual([s["id"] for s in cfg["subscribers"]], inline_ids + ["sofia", "sofia_lopez"])
        self.assertEqual(cfg["subscribers"][inline_ids.index("lucas")]["name"], "Lucas Updated")

        # Ids are file names: anything else is refused, which also rules out path traversal
        for bad_id in ("../evil", "a/b", "UPPER", "x", "", None, "with space"):
            with self.assertRaises(ValueError):
                ob.subscriber_file_path(root, bad_id)

        # A profile that would break the daily run is refused, and nothing is written for it
        cv_md = fj.load_subscriber_cv(sofia, root=root)
        self.assertTrue(ob.profile_problems({**sofia, "title_include": [r"(a+)+"]}, cv_md))
        self.assertTrue(ob.profile_problems({**sofia, "title_include": ["[unclosed"]}, cv_md))
        self.assertTrue(ob.profile_problems({**sofia, "title_include": [], "title_all_of": []}, cv_md))
        self.assertTrue(ob.profile_problems({**sofia, "career_goal": " "}, cv_md))
        self.assertTrue(ob.profile_problems(sofia, "# NAME\nno sections at all\n"))
        self.assertTrue(ob.profile_problems(sofia, cv_md + '\n- A bullet with "quotes" and [brackets]\n'))
        with patch("onboard_subscriber.profile_problems", return_value=["forced problem"]):
            with self.assertRaises(ValueError) as refused:
                ob.register_subscriber_in_codebase(payload("Carlos Diaz", "carlos.diaz@example.com"), root=root, api_key="")
        self.assertIn("forced problem", str(refused.exception))
        self.assertFalse((root / "subscribers" / "carlos.json").exists())
        self.assertFalse((root / "cvs" / "carlos.md").exists())

        # A Markdown CV that does not follow the expected layout is still accepted (parsed as plain text)
        odd_markdown = ob.validate_onboarding_payload({
            "name": "Diego Luna", "email": "diego.luna@example.com",
            "cv_text": "# Diego Luna\n\n## Experience\n**Acme Corp** 2019 to 2024\nRan the regional logistics network.\nCut freight cost by 12 percent.\n",
            "levels": ["manager"], "domains": ["operations"], "locations": ["jalisco"],
        })
        diego = ob.register_subscriber_in_codebase(odd_markdown, root=root, api_key="")
        self.assertEqual(diego["subscriber_id"], "diego")
        self.assertEqual(ob.cv_problems((root / "cvs" / "diego.md").read_text(encoding="utf-8")), [])

    @patch("smtplib.SMTP_SSL")
    def test_offline_cv_import_assumes_nothing_and_never_breaks_shared_checks(self, mock_smtp_ssl):
        """When Gemini is unavailable at sign-up the CV is imported by an offline parser. It must
        not invent facts about the person or their employers, must always produce a CV that
        passes the checks every later run applies to all subscribers, and must not lead to
        tailored CVs built on placeholder headings."""
        import onboard_subscriber as ob

        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        root = self.root
        (root / "config.json").write_text(json.dumps({"subscribers": []}), encoding="utf-8")

        def signup(name, email, cv_text):
            validated = ob.validate_onboarding_payload({
                "name": name, "email": email, "cv_text": cv_text,
                "levels": ["specialist"], "domains": ["finance_legal"], "locations": ["jalisco"],
            })
            result = ob.register_subscriber_in_codebase(validated, root=root, api_key="")
            record = fj.load_json(root / "subscribers" / f"{result['subscriber_id']}.json", {})
            return result, record, (root / result["cv_file"]).read_text(encoding="utf-8")

        # 1. Markdown with real employers but no profile lines: structure is kept, and the profile
        #    line only restates what the CV says (no invented description, plain ASCII)
        result, record, cv_md = signup("Marco Ruiz", "marco.ruiz@example.com", (
            "# Marco Ruiz\nmarco.ruiz@example.com\n\n## SUMMARY\nSupply chain manager.\n\n"
            "## SKILLS\n- Planning, Procurement, SAP\n\n## EXPERIENCE\n"
            "### Tiny Local Shop | Zapopan\n**Buyer** | 2015 - 2019\n- Negotiated 40 supplier contracts.\n"
        ))
        self.assertIn("### Tiny Local Shop | Zapopan\n*Company Profile: Tiny Local Shop, Zapopan.*", cv_md)
        self.assertNotIn("enterprise organization", cv_md)
        self.assertTrue(cv_md.isascii())
        self.assertEqual(ob.cv_problems(cv_md), [])
        self.assertTrue(result["tailored_cvs_enabled"])
        self.assertEqual(record["max_tailored_cvs"], 3)

        # 2. Plain text: employers cannot be separated, so one labelled placeholder heading is used.
        #    Nothing is assumed about the person, and no tailored CV is ever built from it.
        result, record, cv_md = signup("Paula Soto", "paula.soto@example.com", (
            "Paula Soto\nGuadalajara | paula.soto@example.com\n"
            "Finance analyst with 6 years in FP&A and treasury for consumer goods companies.\n"
            "- Built the monthly forecast model used by three business units.\n"
            "- Cut the closing calendar from 9 to 5 days.\n"
        ))
        self.assertIn(f"### {ob.PLACEHOLDER_EMPLOYER}", cv_md)
        for assumed in ("Senior Professional", "Prior Experience Portfolio", "| Mexico", "## LANGUAGES", "English", "Spanish"):
            self.assertNotIn(assumed, cv_md)
        self.assertIn("Built the monthly forecast model", cv_md)
        self.assertEqual(ob.cv_problems(cv_md), [])
        self.assertFalse(result["tailored_cvs_enabled"])
        self.assertEqual(record["max_tailored_cvs"], 0)
        with patch.dict(os.environ, {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}):
            self.assertTrue(ob.send_onboarding_confirmation_email(result, board_count=109))
        confirmation = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn("tailored CVs are switched off for now", confirmation.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("tailored CVs are switched off for now", confirmation.get_body(preferencelist=("html",)).get_content())
        # ...and the daily run honours it even with a Gemini key
        paula = next(sub for sub in fj.get_subscribers(fj.load_config(root)) if sub["id"] == "paula")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key"}):
            self.assertEqual(fj.prepare_tailored_cvs([{"key": "k", "title": "Finance Analyst", "company": "Acme"}], paula, date(2026, 11, 8), root=root), [])
        self.assertFalse((root / "out" / "cvs").exists())

        # 3. Awkward uploads are imported, not rejected, and all satisfy the shared checks
        awkward = {
            "olga": "# Olga Odd\n\n## Experience\n**Some Company** 2018 to 2024\nLed the regional sales team.\nGrew revenue 30 percent.\n",
            "sofia": ("Sofía Ñandú-O'Brien\nZapopan | sofia.nandu@example.com\n"
                      'Directora de Transformación <Digital> [LATAM] {Global} "Top" \\ performer — résumé\n'
                      "- Lideró >25 proyectos & ahorró >=$3.5M (≤48 h de ciclo).\n"),
            "min": "Min Imal\nmin.imal@example.com\nAnalyst. Data. Excel. SQL. Reporting dashboards daily.\n",
            "wendy": "Wendy Windows\r\nwendy.windows@example.com\r\nProject manager with PMP.\r\n- Delivered 12 ERP rollouts on time.\r\n",
        }
        for first_name, cv_text in awkward.items():
            result, record, cv_md = signup(f"{first_name.title()} Tester", f"{first_name}@example.com", cv_text)
            self.assertEqual(ob.cv_problems(cv_md), [], first_name)
            self.assertIsNone(fj.ATS_ILLEGAL_CHARS_RE.search(cv_md), first_name)
            self.assertEqual(ob.profile_problems(record, cv_md), [], first_name)

        # 4. Unusable CVs are reported precisely
        self.assertIn("no employer in the CV has a '*Company Profile: ...*' line", ob.cv_problems("# NAME\nno sections\n"))
        self.assertTrue(any("ATS portals reject" in problem for problem in ob.cv_problems(cv_md + "\n- [bracketed]\n")))
        no_roles = "# X\n\n## PROFESSIONAL EXPERIENCE\n### Acme | GDL\n*Company Profile: Acme, GDL.*\n"
        self.assertTrue(any("no role with bullet points" in problem for problem in ob.cv_problems(no_roles)))

        # 5. The PDF text helpers describe what the PDF writer really does, so a profile that
        #    contains a dash or parentheses is recognised as present (a raw-bytes search is not)
        cv = {"full_name": "T P", "summary": "S", "core_skills": ["A"], "experience": [{
            "company": "Acme (MX)", "role": "R", "dates": "2020", "bullets": ["b"],
            "company_context": "Acme Corp — enterprise (GBS) hub · “quoted” → next",
        }]}
        context = fj.sanitize_cv_data_for_ats(cv)["experience"][0]["company_context"]
        pdf_bytes = fj.render_minimalist_cv_pdf(cv)
        self.assertEqual(fj.pdf_safe_text("a — b · “c” → d ’e’ ✓"), "a  -  b | c  -  d 'e' ?")
        self.assertIn(fj.pdf_safe_text(context), fj.extract_pdf_text(pdf_bytes))
        self.assertNotIn(context[:20], pdf_bytes.decode("latin-1"))

    # ----------------------------------------------------------------------------------------
    # "Like this role" feedback (item 6)
    # ----------------------------------------------------------------------------------------

    @patch("smtplib.SMTP_SSL")
    def test_like_links_round_trip_and_steer_review_order_and_gate_prompt(self, mock_smtp_ssl):
        """Each role carries a signed 'Like this role' link. A like sent back is verified, stored
        once, acknowledged in the next briefing, and from then on shapes the review order and the
        gate prompt. Forged, foreign or repeated likes change nothing."""
        import copy
        from urllib.parse import parse_qs

        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password", "GEMINI_API_KEY": "fake-gemini-key"}
        gate_prompts = []
        cfg = make_cfg()
        sub_id = cfg["subscribers"][0]["id"]

        def recording_gate(prompt_text, **kwargs):
            gate_prompts.append(prompt_text)
            return approve_all_gate(prompt_text)

        def run(day, overrides=None, messages=None):
            fj.http_json = fake_http(overrides)
            with patch.dict(os.environ, env), \
                 patch("fetch_jobs.call_gemini_job_matches", side_effect=recording_gate), \
                 patch("fetch_jobs._invoke_gemini_json", return_value=None):
                code = fj.refresh(cfg, COMPANIES, root=self.root, today=date(2026, 11, day), send_email=True,
                                  feedback_fetcher=(lambda today: messages) if messages is not None else None)
            self.assertEqual(code, 0)
            msg = mock_server.send_message.call_args_list[-1][0][0]
            return (msg, msg.get_body(preferencelist=("html",)).get_content(),
                    msg.get_body(preferencelist=("plain",)).get_content())

        def sub_state():
            return fj.load_json(self.root / "state" / "seen.json", {})["subscribers"][sub_id]

        # --- Day 1: every role has its own like link, in the HTML and in the plain-text part
        msg1, html1, plain1 = run(5)
        links = [fj.html.unescape(h) for h in re.findall(r'href="(mailto:[^"]+)"', html1)]
        n_roles = plain1.count("\n- ")
        self.assertGreaterEqual(n_roles, 5)
        self.assertEqual(len(links), n_roles)
        self.assertEqual(len(set(links)), n_roles)                      # one distinct link per role
        self.assertEqual(html1.count(">Like this role</a>"), n_roles)
        self.assertEqual(plain1.count("\n  Like this role: mailto:"), n_roles)
        self.assertIn("Like a role? Its Like link opens a short email to Lucy", html1)
        self.assertNotIn("ROLES THE CANDIDATE LIKED", gate_prompts[0])

        def like_email(link, message_id, note=""):
            address, _, query = link[len("mailto:"):].partition("?")
            self.assertEqual(address, "sender@gmail.com")
            fields = parse_qs(query, keep_blank_values=True)
            body = fields["body"][0].replace(fj.FEEDBACK_NOTE_PROMPT + " ", fj.FEEDBACK_NOTE_PROMPT + " " + note)
            return {"id": message_id, "subject": fields["subject"][0], "body": body}

        liked = like_email(links[0], "<like-1@mail.example>",
                           note="I want more <b>platform leadership</b> roles\nlike this one\n> quoted old text")
        liked_role = liked["subject"][len("Like: "):liked["subject"].index(" [LUCYLIKE")]
        self.assertIn("LUCYLIKE v1-", liked["subject"])
        self.assertNotIn(sub_id, liked["subject"].lower())           # the reference exposes no subscriber id
        self.assertIn(f"I like this role: {liked_role}.", liked["body"])

        # --- Day 2: the like is ingested before the run, acknowledged in the email, then cleared
        msg2, html2, plain2 = run(6, messages=[liked])
        self.assertIn("Quiet day", msg2["Subject"])
        self.assertIn(fj.html.escape(f"Noted, thank you: you liked {liked_role}. Lucy now favours similar roles."), html2)
        self.assertIn(f"Noted, thank you: you liked {liked_role}.", plain2)
        feedback = sub_state()["feedback"]
        self.assertEqual(len(feedback["likes"]), 1)
        like = feedback["likes"][0]
        self.assertEqual(f"{like['title']} at {like['company']}", liked_role)
        self.assertEqual(like["date"], "2026-11-06")
        self.assertEqual(like["note"], "I want more platform leadership roles like this one")   # markup and the quoted reply are gone
        self.assertEqual(feedback["pending_ack"], [])
        self.assertEqual(feedback["seen_ids"], ["<like-1@mail.example>"])

        # --- Day 3: the mailbox still holds the same email -> nothing changes, no second thank-you
        msg3, html3, _ = run(7, messages=[liked])
        self.assertEqual(len(sub_state()["feedback"]["likes"]), 1)
        self.assertNotIn("Noted, thank you", html3)

        # --- Forged, foreign and unreadable likes are rejected; a second genuine like is accepted
        state = fj.load_json(self.root / "state" / "seen.json", {})
        subscribers = fj.get_subscribers(cfg)
        with patch.dict(os.environ, env):
            signing_key = fj.feedback_signing_key()
            forged = dict(like_email(links[1], "<forged@mail.example>"))
            forged["subject"] = forged["subject"][:-2] + ("0" if forged["subject"][-2] != "0" else "1") + "]"
            unknown_role = {"id": "<unknown@mail.example>", "subject": "Like: x [" + fj.build_like_reference(sub_id, "greenhouse:Acme:not-sent", signing_key) + "]", "body": ""}
            stranger = {"id": "<stranger@mail.example>", "subject": "Like: x [" + fj.build_like_reference("nobody", "greenhouse:Acme:1", signing_key) + "]", "body": ""}
            unrelated = {"id": "<hello@mail.example>", "subject": "Hello Lucy", "body": "LUCYLIKE"}
            second = like_email(links[1], "<like-2@mail.example>")
            repeat = like_email(links[1], "<like-3@mail.example>", note="added later")
            stats = fj.ingest_feedback(state, subscribers, [liked, forged, unknown_role, stranger, unrelated, second, repeat], date(2026, 11, 8))
        self.assertEqual(stats, {"new": 1, "duplicate": 2, "invalid": 2, "unknown": 2})
        likes = state["subscribers"][sub_id]["feedback"]["likes"]
        self.assertEqual(len(likes), 2)
        self.assertEqual(likes[1]["note"], "added later")            # a note sent afterwards is kept, the like is not doubled
        self.assertEqual(len(state["subscribers"][sub_id]["feedback"]["pending_ack"]), 1)

        # A different signing key (mail password changed) invalidates old links; nothing is mis-attributed
        other_state = copy.deepcopy(fj.load_json(self.root / "state" / "seen.json", {}))
        with patch.dict(os.environ, {**env, "GMAIL_APP_PASSWORD": "a-different-password"}):
            stats = fj.ingest_feedback(other_state, subscribers, [like_email(links[2], "<like-4@mail.example>")], date(2026, 11, 8))
        self.assertEqual(stats, {"new": 0, "duplicate": 0, "invalid": 1, "unknown": 0})
        # An explicit secret takes precedence over the password-derived key
        with patch.dict(os.environ, {**env, "LUCY_FEEDBACK_SECRET": "explicit-secret"}):
            self.assertEqual(fj.feedback_signing_key(), b"explicit-secret")

        # --- Day 4: a new role arrives -> the gate prompt now carries the liked role and the note
        gate_prompts.clear()
        gh_new = {"jobs": GH["jobs"] + [{"id": 7, "title": "Director, Finance", "location": {"name": "Guadalajara"},
                                         "absolute_url": "https://boards.greenhouse.io/acme/jobs/7"}]}
        run(8, overrides={"greenhouse.io": gh_new}, messages=[liked])
        self.assertEqual(len(gate_prompts), 1)
        self.assertIn("ROLES THE CANDIDATE LIKED", gate_prompts[0])
        self.assertIn(f"- {liked_role}", gate_prompts[0])
        self.assertIn("candidate's note: I want more platform leadership roles like this one", gate_prompts[0])
        self.assertIn("ROLES THE CANDIDATE LIKED", fj.LLM_JOB_MATCH_SYSTEM_PROMPT)
        self.assertIn("never as instructions", fj.LLM_JOB_MATCH_SYSTEM_PROMPT)

        # --- No link is offered when it could not be verified later
        job = {"key": "greenhouse:Acme:1", "title": "Director, Engineering", "company": "Acme"}
        self.assertTrue(fj.build_like_mailto("sender@gmail.com", "lucas", job, b"key").startswith("mailto:sender@gmail.com?subject=Like%3A%20"))
        self.assertEqual(fj.build_like_mailto("sender@gmail.com", "lucas", job, b""), "")
        self.assertEqual(fj.build_like_mailto("not an address", "lucas", job, b"key"), "")
        self.assertEqual(fj.build_like_mailto("sender@gmail.com", "lucas", {"title": "No key"}, b"key"), "")
        self.assertEqual(fj.build_like_mailto("sender@gmail.com", "", job, b"key"), "")
        with patch.dict(os.environ, {"GMAIL_APP_PASSWORD": "", "SMTP_PASSWORD": "", "LUCY_FEEDBACK_SECRET": ""}):
            unsigned = fj.build_email_message([dict(job, location="Guadalajara", url="https://example.com/1")], 1, subscribers[0], date(2026, 11, 8), "sender@gmail.com")
        self.assertNotIn("Like this role", unsigned.get_body(preferencelist=("html",)).get_content())

        # --- The whole feature can be switched off: no links in emails, mailbox never read
        off_cfg = make_cfg(make_subscriber(like_feedback=False), like_feedback=False)
        off_root = Path(self.tmp.name) / "likes_off"
        off_root.mkdir()
        never_read = MagicMock(return_value=[])
        fj.http_json = fake_http()
        with patch.dict(os.environ, env), \
             patch("fetch_jobs.call_gemini_job_matches", side_effect=approve_all_gate), \
             patch("fetch_jobs._invoke_gemini_json", return_value=None):
            self.assertEqual(fj.refresh(off_cfg, COMPANIES, root=off_root, today=date(2026, 11, 5), send_email=True,
                                        feedback_fetcher=never_read), 0)
        never_read.assert_not_called()
        off_msg = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn(f"{n_roles} new roles for you", off_msg["Subject"])
        self.assertNotIn("Like this role", off_msg.get_body(preferencelist=("html",)).get_content())
        self.assertNotIn("mailto:", off_msg.get_body(preferencelist=("plain",)).get_content())

        # --- Notes are reduced to one clean line and stop at signatures and quoted replies
        self.assertEqual(fj._feedback_note("no prompt here"), "")
        self.assertEqual(fj._feedback_note("Why (optional): great scope\n\nSent from my iPhone"), "great scope")
        self.assertEqual(fj._feedback_note("Why (optional):\n\n(Press send. Lucy reads this...)"), "")
        self.assertEqual(len(fj._feedback_note("Why (optional): " + "word " * 400)), fj.FEEDBACK_MAX_NOTE_CHARS)

    def test_like_emails_are_read_over_imap_without_changing_the_mailbox(self):
        """The daily run reads likes from Lucy's own mailbox: read-only, PEEK fetches, only
        subjects carrying the marker, with a verified TLS connection. Any failure returns no
        likes instead of raising, so the daily email is never blocked by it."""
        from email.message import EmailMessage

        def raw_email(message_id, subject, text=None, html_body=None):
            m = EmailMessage()
            m["Message-ID"] = message_id
            m["From"] = "Ana <ana@example.com>"
            m["To"] = "sender@gmail.com"
            m["Subject"] = subject
            if html_body is not None:
                m.set_content(html_body, subtype="html")
            else:
                m.set_content(text or "")
            return m.as_bytes()

        mailbox = {
            b"1": raw_email("<like-1@example.com>", "Like: Director, Platform at Gamma [LUCYLIKE v1-0123456789-0123456789ab-0123456789abcdef0123]",
                            text="I like this role: Director, Platform at Gamma.\n\nWhy (optional): Loved the scope\n\n(Press send. Lucy reads this...)\n"),
            b"2": raw_email("<huge@example.com>", "LUCYLIKE oversized", text="x"),
            b"3": raw_email("<like-3@example.com>", "Re: Like: Directora de Transformación at Example [LUCYLIKE v1-0123456789-0123456789ab-0123456789abcdef0123]",
                            html_body="<div>I like this role.</div><div>Why (optional): Because of the scope<br>(Press send. Lucy reads this...)</div>"),
        }

        class FakeIMAP:
            last = None

            def __init__(self, host, port, **kwargs):
                self.host, self.port, self.kwargs, self.calls = host, port, kwargs, []
                FakeIMAP.last = self

            def login(self, user, password):
                self.calls.append(("login", user))
                return "OK", [b"logged in"]

            def select(self, mailbox_name, readonly=False):
                self.calls.append(("select", mailbox_name, readonly))
                return "OK", [b"3"]

            def search(self, charset, *criteria):
                self.calls.append(("search",) + criteria)
                return "OK", [b"1 2 3"]

            def fetch(self, number, spec):
                self.calls.append(("fetch", number, spec))
                raw = mailbox[number]
                if spec == "(RFC822.SIZE)":
                    size = 9_999_999 if number == b"2" else len(raw)
                    return "OK", [number + b" (RFC822.SIZE " + str(size).encode() + b")"]
                return "OK", [(number + b" (BODY[] {" + str(len(raw)).encode() + b"}", raw), b")"]

            def logout(self):
                self.calls.append(("logout",))
                return "BYE", [b"bye"]

        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}
        cleared = {"SMTP_HOST": "", "IMAP_HOST": "", "IMAP_PORT": "", "LUCY_FEEDBACK_MAILBOX": ""}
        with patch.dict(os.environ, {**cleared, **env}), patch("fetch_jobs.imaplib.IMAP4_SSL", FakeIMAP):
            messages = fj.fetch_feedback_emails(date(2026, 11, 22))

        self.assertEqual([m["id"] for m in messages], ["<like-1@example.com>", "<like-3@example.com>"])   # the oversized one is skipped
        self.assertIn("LUCYLIKE v1-0123456789", messages[0]["subject"])
        self.assertEqual(fj._feedback_note(messages[0]["body"]), "Loved the scope")
        self.assertIn("Directora de Transformación", messages[1]["subject"])                                # encoded header decoded
        self.assertEqual(fj.FEEDBACK_REF_RE.search(messages[1]["subject"]).group(3), "0123456789abcdef0123") # reference survives folding
        self.assertEqual(fj._feedback_note(messages[1]["body"]), "Because of the scope")                    # HTML-only reply
        imap = FakeIMAP.last
        self.assertEqual((imap.host, imap.port), ("imap.gmail.com", 993))
        self.assertEqual(imap.kwargs["ssl_context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(imap.kwargs["ssl_context"].check_hostname)
        self.assertTrue(imap.kwargs["timeout"])
        self.assertIn(("select", '"INBOX"', True), imap.calls)                                               # opened read-only
        self.assertIn(("search", "SINCE", "08-Nov-2026", "SUBJECT", '"LUCYLIKE"'), imap.calls)
        fetch_specs = {call[2] for call in imap.calls if call[0] == "fetch"}
        self.assertEqual(fetch_specs, {"(RFC822.SIZE)", "(BODY.PEEK[])"})                                    # PEEK: never marked as read
        self.assertNotIn(("fetch", b"2", "(BODY.PEEK[])"), imap.calls)
        self.assertEqual(imap.calls[-1], ("logout",))

        # A custom mailbox (for example a Gmail label that a filter fills) can be named
        with patch.dict(os.environ, {**cleared, **env, "LUCY_FEEDBACK_MAILBOX": "Lucy Likes"}), patch("fetch_jobs.imaplib.IMAP4_SSL", FakeIMAP):
            fj.fetch_feedback_emails(date(2026, 11, 22))
        self.assertIn(("select", '"Lucy Likes"', True), FakeIMAP.last.calls)

        # Failures never raise and never block the run
        with patch.dict(os.environ, {**cleared, **env}), patch("fetch_jobs.imaplib.IMAP4_SSL", side_effect=OSError("network down")):
            self.assertEqual(fj.fetch_feedback_emails(date(2026, 11, 22)), [])
        with patch.dict(os.environ, {**cleared, **env, "IMAP_PORT": "not-a-number"}), patch("fetch_jobs.imaplib.IMAP4_SSL", FakeIMAP):
            self.assertEqual(fj.fetch_feedback_emails(date(2026, 11, 22)), [])
        # No mail credentials, or a non-Gmail SMTP host without IMAP_HOST: the mailbox is not contacted at all
        never_called = MagicMock()
        with patch.dict(os.environ, {**cleared, "GMAIL_USER": "", "GMAIL_APP_PASSWORD": "", "SMTP_USER": "", "SMTP_PASSWORD": ""}), \
             patch("fetch_jobs.imaplib.IMAP4_SSL", never_called):
            self.assertEqual(fj.fetch_feedback_emails(date(2026, 11, 22)), [])
        with patch.dict(os.environ, {**cleared, **env, "SMTP_HOST": "smtp.example.com"}), patch("fetch_jobs.imaplib.IMAP4_SSL", never_called):
            self.assertEqual(fj.fetch_feedback_emails(date(2026, 11, 22)), [])
        never_called.assert_not_called()

        # A fetcher that raises is contained by refresh(): the run still completes
        def exploding_fetcher(today):
            raise RuntimeError("mailbox exploded")

        fj.http_json = fake_http()
        self.assertEqual(fj.refresh(make_cfg(), COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True,
                                    feedback_fetcher=exploding_fetcher), 0)
        # ...and without send_email the mailbox is never read
        untouched = MagicMock(return_value=[])
        self.assertEqual(fj.refresh(make_cfg(), COMPANIES, root=self.root, today=date(2026, 11, 6), send_email=False,
                                    feedback_fetcher=untouched), 0)
        untouched.assert_not_called()

    # ----------------------------------------------------------------------------------------
    # Published CVs (item 1), invite-code reminder (item 2), workflows (item 8)
    # ----------------------------------------------------------------------------------------

    def test_tailored_cvs_are_never_published_and_old_ones_are_removed(self):
        """`docs/` is what GitHub Pages serves. Tailored CVs (email + phone) are no longer written
        there, and CVs published by earlier versions are deleted by the next run."""
        old_dir = self.root / "docs" / "cvs" / "lucas"
        old_dir.mkdir(parents=True)
        for name in ("cv_lucas_acme_director.docx", "cv_lucas_acme_director.pdf", "cv_lucas_acme_director.html"):
            (old_dir / name).write_bytes(b"old published cv")
        (self.root / "docs" / "cvs" / "luis").mkdir()
        (self.root / "docs" / "cvs" / "luis" / "cv_luis.pdf").write_bytes(b"old published cv")

        code, _ = self.run_refresh(date(2026, 11, 5))                 # an ordinary run, even without email
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "docs" / "cvs").exists())
        self.assertTrue((self.root / "docs" / "index.html").exists())  # the rest of docs/ is untouched
        self.assertEqual(fj.purge_published_cvs(self.root), 0)         # nothing left to remove

        (self.root / "docs" / "cvs").mkdir()
        (self.root / "docs" / "cvs" / "stray.pdf").write_bytes(b"x")
        self.assertEqual(fj.purge_published_cvs(self.root), 1)
        self.assertEqual(fj.purge_published_cvs(self.root / "does-not-exist"), 0)

        # README states where CVs go and that the repository must stay private
        readme = (Path(fj.ROOT) / "README.md").read_text(encoding="utf-8")
        self.assertIn("Keep the repository private", readme)
        self.assertIn("anyone with the URL can read", readme)
        self.assertNotIn("docs/cvs/<", readme)

    def test_open_signup_reminder_and_isolated_onboarding_workflow(self):
        """The daily run reminds the admin while the public sign-up page asks for no invite code.
        The onboarding workflow runs in its own concurrency group, commits only the subscriber's
        own files, and re-runs the registration (never a stale snapshot) when a push is rejected."""
        import contextlib
        import io

        def reminder(cfg, env=None):
            buffer = io.StringIO()
            with patch.dict(os.environ, {"ONBOARD_WEBHOOK_URL": "", **(env or {})}), contextlib.redirect_stderr(buffer):
                fj.warn_if_signup_is_open(cfg)
            return buffer.getvalue()

        self.assertEqual(reminder({"onboard_webhook_url": ""}), "")                                     # form not connected
        self.assertIn("does not ask for an invite code", reminder({"onboard_webhook_url": "https://script.google.com/x"}))
        self.assertIn("does not ask for an invite code", reminder({}, {"ONBOARD_WEBHOOK_URL": "https://script.google.com/x"}))
        self.assertEqual(reminder({"onboard_webhook_url": "https://script.google.com/x", "onboard_require_invite_code": True}), "")

        onboard_wf = (Path(fj.ROOT) / ".github" / "workflows" / "onboard.yml").read_text(encoding="utf-8")
        main_wf = (Path(fj.ROOT) / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")
        self.assertNotIn("\t", onboard_wf)
        # Its own concurrency group: a sign-up can no longer cancel, or be cancelled by, a pending daily run
        self.assertIn("group: lucy-onboard-", onboard_wf)
        self.assertNotIn("group: jobs-sentinel-", onboard_wf)
        self.assertIn("group: jobs-sentinel-${{ github.ref }}", main_wf)
        self.assertIn("cancel-in-progress: false", onboard_wf)
        # Only this subscriber's two files are committed; shared files are never staged or snapshotted
        self.assertIn('git add -- "subscribers/${SUB_ID}.json" "cvs/${SUB_ID}.md"', onboard_wf)
        for shared in ("config.json", "docs/onboard.html", "SNAP_DIR", "git add -A"):
            self.assertNotIn(shared, onboard_wf)
        # A rejected push re-syncs and re-runs the registration and the tests inside the retry loop
        loop = onboard_wf.split("for attempt in", 1)[1]
        self.assertIn('git reset --hard "origin/$BRANCH"', loop)
        self.assertIn("python3 onboard_subscriber.py", loop)
        self.assertIn("-m unittest discover -s tests -b -v", loop)
        self.assertLess(loop.index("python3 onboard_subscriber.py"), loop.index("git push"))
        # The confirmation email is only sent after the commit landed
        self.assertLess(onboard_wf.index("git push"), onboard_wf.index("--send-confirmation-from"))
        # The daily workflow passes the optional feedback settings and still commits state + docs
        for name in ("LUCY_FEEDBACK_SECRET", "LUCY_FEEDBACK_MAILBOX", "IMAP_HOST"):
            self.assertIn(name, main_wf)
        self.assertIn("git add -A -- state docs", main_wf)


if __name__ == "__main__":
    unittest.main()

