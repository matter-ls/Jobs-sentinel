"""Offline tests: fake every ATS response and SMTP server, so no network is needed.
Run from the project folder:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_jobs as fj  # noqa: E402

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

        # Minimalist email prioritizes Career Upgrade Moves at the top
        sample_jobs = [
            {"key": "k1", "title": "RPA Developer", "company": "Beta", "location": "Guadalajara", "loc_class": "jalisco", "url": "https://example.com/1", "posted_at": "2026-11-05"},
            {"key": "k2", "title": "Senior Manager, Process Automation", "company": "Acme", "location": "Zapopan", "loc_class": "jalisco", "url": "https://example.com/2", "posted_at": "2026-11-01"},
        ]
        msg = fj.build_email_message(sample_jobs, 2, luis_cfg, date(2026, 11, 5), "sender@gmail.com")
        self.assertIn("1 Career Upgrade Move(s)", msg["Subject"])
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
        self.assertGreaterEqual(len(companies), 105)
        seen_names = set()
        seen_targets = set()
        for c in companies:
            name = c.get("name", "").strip()
            ats = c.get("ats", "").strip()
            self.assertTrue(name)
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
            self.assertIn("Relevant Opening(s)", sent_msg["Subject"])
            self.assertIn("HOT", sent_msg["Subject"])
            html_part = sent_msg.get_body(preferencelist=("html",)).get_content()
            self.assertIn("daily reminder to check where to apply today", html_part)
            self.assertIn("Hot Off the Press", html_part)
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
        with patch.dict(os.environ, {
            "GMAIL_USER": "sender@gmail.com",
            "GMAIL_APP_PASSWORD": "app-secret-password",
            "GEMINI_API_KEY": "fake-gemini-key",
        }):
            code = fj.refresh(CFG, COMPANIES, root=self.root, today=date(2026, 11, 5), send_email=True, subscriber_id="lucas")
            self.assertEqual(code, 0)
            self.assertTrue(mock_gemini.called)
            self.assertTrue(mock_server.send_message.called)

            sent_msg = mock_server.send_message.call_args_list[0][0][0]
            self.assertIn("Tailored CV(s)", sent_msg["Subject"])
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
            self.assertEqual(len(list((self.root / "docs" / "cvs" / "lucas").glob("*.docx"))), 3)
            self.assertEqual(len(list((self.root / "docs" / "cvs" / "lucas").glob("*.pdf"))), 3)
            html_cvs = list((self.root / "docs" / "cvs" / "lucas").glob("*.html"))
            self.assertEqual(len(html_cvs), 3)
            self.assertIn("Gemini AI &middot; gemini-3.8-flash", html_cvs[0].read_text(encoding="utf-8"))

    @patch("fetch_jobs._invoke_gemini_json", side_effect=RuntimeError("gemini 429 quota"))
    @patch("smtplib.SMTP_SSL")
    def test_llm_fallback_still_sends_tailored_cvs(self, mock_smtp_ssl, _mock_gemini):
        """Even if Gemini throws (429/503/JSON error), deterministic fallback still builds & attaches 3 CVs."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        fj.http_json = fake_http()
        with patch.dict(os.environ, {
            "GMAIL_USER": "sender@gmail.com",
            "GMAIL_APP_PASSWORD": "app-secret-password",
            "GEMINI_API_KEY": "fake-gemini-key",
        }):
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
            pdf_files = list((self.root / "docs" / "cvs" / "lucas").glob("*.pdf"))
            self.assertTrue(pdf_files[0].read_bytes().startswith(b"%PDF-1.4"))
            html_cvs = list((self.root / "docs" / "cvs" / "lucas").glob("*.html"))
            self.assertIn("Deterministic ATS Fallback", html_cvs[0].read_text(encoding="utf-8"))

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
    def test_relationship_streak_and_quiet_day_spotlight(self, mock_smtp_ssl):
        """When zero brand-new postings arrive overnight, Lucy keeps daily connection alive by
        spotlighting top unclaimed roles (rotating them each quiet day) and incrementing Edition & Streak."""
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        env = {"GMAIL_USER": "sender@gmail.com", "GMAIL_APP_PASSWORD": "app-secret-password"}

        # Day 1 (Nov 5): Baseline -> Briefing #1 + Welcome milestone
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 5), send_email=True)
        self.assertEqual(code, 0)
        msg1 = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn("Lucy #1:", msg1["Subject"])
        html1 = msg1.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Briefing #1", html1)
        self.assertIn("Lucy&rsquo;s Corner:", html1)
        self.assertIn("Welcome to Your Unfair Advantage", html1)

        # Day 2 (Nov 6): Zero new postings -> Quiet-Market Momentum Spotlight (#2, 2d Streak, 3 roles)
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 6), send_email=True)
        self.assertEqual(code, 0)
        msg2 = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn("Lucy #2:", msg2["Subject"])
        self.assertNotEqual(msg1["Subject"], msg2["Subject"])
        html2 = msg2.get_body(preferencelist=("html",)).get_content()
        self.assertIn("2d Streak", html2)
        self.assertIn("Quiet-Market Momentum Spotlight", html2)
        plain2 = msg2.get_body(preferencelist=("plain",)).get_content()

        # Day 3 (Nov 7): Another quiet day -> Briefing #3 (3d Streak + 3-Briefing Spark milestone + rotated spotlight roles)
        with patch.dict(os.environ, env):
            code, _ = self.run_refresh(date(2026, 11, 7), send_email=True)
        self.assertEqual(code, 0)
        msg3 = mock_server.send_message.call_args_list[-1][0][0]
        self.assertIn("Lucy #3:", msg3["Subject"])
        html3 = msg3.get_body(preferencelist=("html",)).get_content()
        self.assertIn("3d Streak", html3)
        self.assertIn("3-Briefing Spark", html3)
        plain3 = msg3.get_body(preferencelist=("plain",)).get_content()
        # Spotlighted roles rotate rather than repeating the exact same 3 roles
        self.assertNotEqual(plain2, plain3)

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

    def test_gemini_3_8_flash_default_and_404_auto_discovery(self):
        self.assertEqual(CFG["llm_model"], "gemini-3.8-flash")
        self.assertEqual(fj.DEFAULT_LLM_MODEL, "gemini-3.8-flash")

        fj._DEAD_GEMINI_MODELS.clear()
        fj._DISCOVERED_GEMINI_MODELS = None

        calls = []

        def mock_gemini_http(url, payload=None, retries=2, headers=None, timeout=60):
            calls.append((url, payload))
            if url.endswith("/v1beta/models"):
                return {
                    "models": [
                        {"name": "models/gemini-4.2-flash-tts", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-4.2-flash", "supportedGenerationMethods": ["generateContent"]},
                    ]
                }
            if "gemini-4.2-flash:generateContent" in url:
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
            # Also tried gemini-3.8-flash with thinkingLevel: low
            c38 = [p for u, p in calls if "gemini-3.8-flash:generateContent" in u][0]
            self.assertEqual(c38["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
            # Auto-discovered gemini-4.2-flash (skipping -tts) and succeeded
            self.assertTrue(any("gemini-4.2-flash:generateContent" in u for u, _ in calls))
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
        for action_ref in ("actions/checkout@v7", "actions/setup-python@v7", "actions/upload-artifact@v7"):
            self.assertIn(action_ref, wf_txt)
        for sub in fj.get_subscribers(CFG):
            self.assertIn(f"- {sub['id']}", wf_txt, f"Subscriber {sub['id']} missing from workflow_dispatch options")


if __name__ == "__main__":
    unittest.main()
