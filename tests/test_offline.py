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

    def test_ewerson_cv_rules(self):
        subs = {s["id"]: s for s in fj.get_subscribers(CFG)}
        self.assertIn("ewerson", subs)
        cfg = subs["ewerson"]
        self.assertEqual(cfg["recipient_email"], "ewerson.lira@live.com")
        self.assertTrue(fj.load_subscriber_cv(cfg).startswith("# EWERSON LIRA"))
        rules = fj.build_rules(cfg)
        yes = [
            "Business Development Representative, LATAM",
            "Senior Sales Operations Analyst",
            "Business Operations Analyst",
            "Revenue Operations Specialist",
            "Data Analyst, Sales Reporting",
            "Power BI Analyst",
            "Account Manager - Enterprise",
            "Customer Success Manager",
            "Software Asset Management Analyst",
            "Customer Support Team Lead",
            "Sales Enablement Manager",
            "Analista de Ventas y Operaciones",
            "Ejecutivo de Desarrollo de Negocios",
            "Project Coordinator",
            "Inside Sales Representative",
            "Forecasting & Pipeline Analyst",
        ]
        no = [
            "Software Engineer",
            "Data Engineer",
            "Machine Learning Engineer",
            "QA Analyst",
            "Vice President of Sales",
            "Chief Revenue Officer",
            "Sales Intern",
            "Executive Assistant to the Sales Director",
            "Retail Store Sales Associate",
            "Tax Analyst",
            "Director, Fab Operations",
            "Becario de Analisis de Datos",
        ]
        for t in yes:
            self.assertTrue(fj.is_director(t, rules), t)
        for t in no:
            self.assertFalse(fj.is_director(t, rules), t)

    def test_locations(self):
        lc = lambda s: fj.location_class(s, RULES, CFG)
        self.assertEqual(lc("Guadalajara, Mexico"), "jalisco")
        self.assertEqual(lc("Zapopan, JAL"), "jalisco")
        self.assertEqual(lc("Tonalá, Jalisco"), "jalisco")
        self.assertEqual(lc("Remote - Mexico"), "remote_mx")
        self.assertEqual(lc("Remote (Mexico); Austin, TX"), "remote_mx")
        self.assertEqual(lc("Mexico"), "mx_country")
        self.assertIsNone(lc("Mexico City, Mexico"))
        self.assertIsNone(lc("Monterrey, Nuevo Leon"))
        self.assertIsNone(lc("Remote - United States"))
        self.assertIsNone(lc("Remote - LATAM"))


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

    def fake(url, payload=None, retries=2):
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


    @patch("fetch_jobs.call_gemini_json")
    @patch("smtplib.SMTP_SSL")
    def test_llm_cv_tailoring_and_attachment(self, mock_smtp_ssl, mock_gemini):
        mock_server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server

        mock_gemini.return_value = {
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
            att_html = attachments[0].get_content()
            self.assertIn("Lucas Scholl Matter", att_html)
            self.assertIn("Director of Engineering | Cloud &amp; AI Platform Leadership", att_html)
            saved_docs_cvs = list((self.root / "docs" / "cvs" / "lucas").glob("*.html"))
            self.assertEqual(len(saved_docs_cvs), 3)

    @patch("fetch_jobs.call_gemini_json", side_effect=RuntimeError("gemini 429 quota"))
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
            att_html = attachments[0].get_content()
            self.assertIn("Lucas Scholl Matter", att_html)
            self.assertIn("Google", att_html)
            self.assertIn("AstraZeneca", att_html)

    def test_workday_dates(self):
        t = date(2026, 11, 5)
        self.assertEqual(fj.workday_posted("Posted Today", t), "2026-11-05")
        self.assertEqual(fj.workday_posted("Posted Yesterday", t), "2026-11-04")
        self.assertEqual(fj.workday_posted("Posted 3 Days Ago", t), "2026-11-02")
        self.assertIsNone(fj.workday_posted("Posted 30+ Days Ago", t))

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


if __name__ == "__main__":
    unittest.main()
