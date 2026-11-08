# job-radar (Jobs Sentinel)

Pulls public job feeds from company career sites (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, and Workday), keeps **director-level roles in Jalisco** (plus remote-Mexico roles), builds a static searchable page (`docs/index.html`), and emails a daily digest to **lucasschollmatter@gmail.com**.

Uses standard-library Python 3.9+ with zero third-party package installs.

## 1. Try it on your computer

```bash
python3 fetch_jobs.py --demo       # preview the page with fake data -> out/demo.html
python3 fetch_jobs.py --check      # test every company in companies.json
python3 fetch_jobs.py              # real run -> docs/index.html + email (if env vars set)
python3 fetch_jobs.py --email-all  # real run + email all current matching roles
```

## 2. Configure Daily Email Alerts (GitHub Actions)

1. Generate a 16-character **Google App Password** at `https://myaccount.google.com/apppasswords` (requires 2-Step Verification enabled).
2. In your GitHub repository, open **Settings > Secrets and variables > Actions** and add two Repository Secrets:
   - `GMAIL_USER`: `lucasschollmatter@gmail.com` (or the Gmail address sending the alert)
   - `GMAIL_APP_PASSWORD`: your 16-character Google App Password
3. Go to **Settings > Actions > General > Workflow permissions** and select **Read and write permissions**.
4. Open the **Actions** tab, select **Refresh job board & send daily email**, and click **Run workflow**.

## 3. Run Offline Unit Tests

```bash
python3 -m unittest discover -s tests -v
```
