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
            # Baseline run emails initial new roles when email_on_baseline=True
            code, _ = self.run_refresh(date(2026, 11, 5), send_email=True)
            self.assertEqual(code, 0)
            self.assertTrue(mock_server.send_message.called)
            sent_msg = mock_server.send_message.call_args[0][0]
            self.assertEqual(sent_msg["To"], "lucasschollmatter@gmail.com")
            self.assertIn("Relevant Director Opening(s)", sent_msg["Subject"])

    def test_workday_dates(self):
        t = date(2026, 11, 5)
        self.assertEqual(fj.workday_posted("Posted Today", t), "2026-11-05")
        self.assertEqual(fj.workday_posted("Posted Yesterday", t), "2026-11-04")
        self.assertEqual(fj.workday_posted("Posted 3 Days Ago", t), "2026-11-02")
        self.assertIsNone(fj.workday_posted("Posted 30+ Days Ago", t))


if __name__ == "__main__":
    unittest.main()
