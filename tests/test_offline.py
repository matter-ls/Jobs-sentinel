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

    def test_ai_partner_briefing_prompt_quality_gate_and_continuity(self):
        """Lucy's daily briefing is AI-first: the prompt is grounded in real overnight facts, the
        spotlight employer's ATS, the candidate's real craft and the story so far; every draft passes
        a quality gate (forbidden dishonest vocabulary, placeholders, near-duplicate headlines) with
        exactly one revision retry; the email renders chapter, intel and sign-off; memory persists."""
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

        # 4. The email renders chapter, Their-AI-vs-Our-AI intel and the sign-off (HTML + plain text)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "valid-key"}), patch("fetch_jobs._invoke_gemini_json", return_value=dict(good)):
            msg = fj.build_email_message([dict(top_job)], 3, sub_luis, date(2026, 11, 8), "sender@gmail.com", sub_state=sub_state, board_count=109)
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        plain_part = msg.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Chapter 7: The Doorman Switches Sides", html_part)
        self.assertIn("Their AI vs. Our AI:", html_part)
        self.assertIn("Shoulder to shoulder. Lucy", html_part)
        self.assertIn("Flex Built A Gate. We Brought The Key.", html_part)
        self.assertIn("Us vs. Their Machine · Chapter 7", plain_part)
        self.assertIn("Their AI vs. Our AI:", plain_part)
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
        self.assertIn("Lucy&rsquo;s Corner:", html1)
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
            pdf_raw = fj.render_minimalist_cv_pdf(det["cv"], sample_job).decode("latin-1", errors="replace")
            html_out = fj.render_minimalist_cv_html(det["cv"], sample_job)

            self.assertIn(sample_ctx_snippet, docx_txt)
            self.assertIn(sample_ctx_snippet[:20], pdf_raw)
            self.assertIn(fj.html.escape(sample_ctx_snippet), html_out)
            self.assertIsNone(fj.ATS_ILLEGAL_CHARS_RE.search(docx_txt))

    @patch("smtplib.SMTP_SSL")
    def test_self_service_candidate_onboarding_flow_and_immediate_matching(self, mock_smtp_ssl):
        """Verify end-to-end onboarding: web form HTML generation, Gemini CV structuring with
        company profiles, checkbox + free-text rule synthesis, config.json & cvs/<id>.md updates,
        professional confirmation email, idempotent re-onboarding, and automatic daily match delivery."""
        import onboard_subscriber as ob
        import shutil

        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        # Prepare isolated repo workspace with real config.json and cvs/
        shutil.copy2(Path(fj.ROOT) / "config.json", self.root / "config.json")
        shutil.copytree(Path(fj.ROOT) / "cvs", self.root / "cvs")

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
                    "extra_title_include": [r"\bprocess intelligence\b"],
                    "extra_title_exclude": [r"\b(tax|accounting|maintenance)\b"],
                }
            return None

        env = {
            "GMAIL_USER": "sender@gmail.com",
            "GMAIL_APP_PASSWORD": "app-secret-password",
            "GEMINI_API_KEY": "fake-gemini-key",
        }
        extracted = ob.extract_payload_from_github_event(event_file)
        validated = ob.validate_onboarding_payload(extracted)

        with patch.dict(os.environ, env), patch("fetch_jobs._invoke_gemini_json", side_effect=fake_onboard_gemini):
            res = ob.register_subscriber_in_codebase(validated, root=self.root)
            sent_conf = ob.send_onboarding_confirmation_email(res, board_count=109)

        self.assertTrue(sent_conf)
        self.assertEqual(res["subscriber_id"], "sofia")
        self.assertFalse(res["updated_existing"])

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
        updated_cfg = fj.load_json(self.root / "config.json", {})
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


if __name__ == "__main__":
    unittest.main()

