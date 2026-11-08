# job-radar (Jobs Sentinel)

Pulls public job feeds from 106 verified Jalisco & Remote-Mexico employer career sites across 8 supported ATS platforms (Workday, Greenhouse, SmartRecruiters, Lever, Ashby, Oracle HCM, Eightfold, and Workable), keeps the roles that match each **subscriber profile** in `config.json` (director-level roles in Jalisco by default, plus remote-Mexico roles), builds a static searchable page (`docs/index.html`), optionally asks Gemini to tailor each subscriber's master CV (`cvs/*.md`) into an ATS-optimized `.docx` attachment for their top matches, and emails a personalised daily digest.

Uses standard-library Python 3.9+ with zero third-party package installs.

## 1. Try it on your computer

```bash
python3 fetch_jobs.py --demo       # preview the page with fake data -> out/demo.html
python3 fetch_jobs.py --check      # test every company in companies.json
python3 fetch_jobs.py --no-email   # real run -> docs/index.html + state/, no email
python3 fetch_jobs.py              # real run + email (if env vars set)
python3 fetch_jobs.py --email-all  # real run + email all current matching roles
python3 fetch_jobs.py --subscriber luis   # only email one subscriber id
```

Exit codes: `0` ok · `1` nothing written (every board failed) · `2` outputs written but at least one email digest could not be sent (that subscriber's state is rolled back, so the same roles are retried next run).

## 2. Configure GitHub Actions (daily at 07:00 Guadalajara)

1. **Keep the repository private.** `cvs/*.md` contain personal CVs, and the tailored CVs are uploaded as workflow artifacts.
2. Generate a 16-character **Google App Password** at `https://myaccount.google.com/apppasswords` (requires 2-Step Verification).
3. **Settings > Secrets and variables > Actions > Secrets** – add:
   - `GMAIL_USER`: the Gmail address that sends the digest
   - `GMAIL_APP_PASSWORD`: the 16-character App Password
   - `GEMINI_API_KEY` *(optional)*: enables the tailored-CV feature. Without it the digest is still sent, just without CVs.
4. *(Optional)* **Variables** tab – `GEMINI_MODEL` to override `gemini-2.5-flash`.
5. **Settings > Actions > General > Workflow permissions** – select **Read and write permissions** (the workflow commits `state/` and `docs/` back to the branch).
6. If the default branch has a **branch protection / ruleset** that blocks pushes, either allow `github-actions[bot]` to bypass it or turn it off; otherwise the state commit fails.
7. Open the **Actions** tab, select **Refresh job board & send daily email**, and click **Run workflow**. Tick `check_only` the first time to see which boards in `companies.json` respond before you spam anybody.

Notes:

- Scheduled workflows only run on the **default branch**, and GitHub pauses them after **60 days without repository activity** – re-enable from the Actions tab if that happens.
- Manual runs default to `email_all = true` (useful for testing). Scheduled runs only email newly discovered roles.
- To publish `docs/index.html`, enable **Settings > Pages > Deploy from a branch > `/docs`**. The page carries `noindex` + `robots.txt`, but anyone with the URL can read it.

## 3. Run Offline Unit Tests

```bash
python3 -m unittest discover -s tests -v
```
