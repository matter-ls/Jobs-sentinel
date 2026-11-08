# job-radar (Jobs Sentinel)

Pulls public job feeds from 109 verified Jalisco & Remote-Mexico employer career sites across 9 supported ATS platforms (Workday, Greenhouse, SmartRecruiters, Oracle HCM, Eightfold, Lever, Ashby, BambooHR, and Workable), keeps the roles that match each **subscriber profile** in `config.json` (director-level roles in Jalisco by default, plus remote-Mexico roles), builds a static searchable page (`docs/index.html`), optionally asks Gemini to tailor each subscriber's master CV (`cvs/*.md`) into an ATS-optimized `.docx` attachment for their top matches, and emails a personalised daily digest.

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
4. *(Optional)* **Variables** tab – `GEMINI_MODEL` to override `gemini-3.8-flash` (the script also auto-falls back to `gemini-flash-latest` and queries `/v1beta/models` if a model version is ever retired).
5. **Settings > Actions > General > Workflow permissions** – select **Read and write permissions** (the workflow commits `state/` and `docs/` back to the branch).
6. If the default branch has a **branch protection / ruleset** that blocks pushes, either allow `github-actions[bot]` to bypass it or turn it off; otherwise the state commit fails.
7. Open the **Actions** tab, select **Refresh job board & send daily email**, and click **Run workflow**. Tick `check_only` the first time to see which boards in `companies.json` respond before you spam anybody.

Notes:

- Scheduled workflows only run on the **default branch**, and GitHub pauses them after **60 days without repository activity** – re-enable from the Actions tab if that happens.
- **Only roles never sent before are emailed.** Each subscriber has an `emailed` ledger in `state/seen.json` (job key + company/title signature), so a role is delivered once, even if it reappears under a new requisition id or as a city clone. The ledger is seeded automatically for existing subscribers on the first run after upgrading.
- `quiet_day_mode` (global or per subscriber): `note` (default) sends a short role-free Lucy briefing when nothing new matched, `spotlight` re-lists up to `quiet_spotlight_count` unclaimed roles (legacy), `skip` sends nothing on quiet days.
- `max_email_jobs` (default 12) caps each briefing; the rest carry over to the next day and are available in the attached personal board (`Lucy_Board_<id>_<date>.html`).
- Manual runs now default to `email_all = false` (same behaviour as the daily run). Tick it only for testing: it re-sends every current match.
- To publish `docs/index.html`, enable **Settings > Pages > Deploy from a branch > `/docs`**. The page carries `noindex` + `robots.txt`, but anyone with the URL can read it.

## 3. Lucy's AI Partner Briefing ("Us vs. Their Machine")

Every digest opens with a daily briefing written by Gemini (never a canned template while `GEMINI_API_KEY` is set). The prompt in `fetch_jobs.py` (`DAILY_MOTIVATION_SYSTEM_PROMPT` + `compose_partner_briefing_prompt`) is grounded only in real facts from the run:

- **The story**: employers point AI (Workday, Greenhouse, Oracle, Eightfold, ...) at candidates to filter them out; the subscriber now has an AI of their own that decodes those filters and rebuilds their *truthful* CV in the dialect each system scores highest. Lucy is a partner ("we / us"), not a tool.
- **Grounding**: boards swept, roles surfaced, CVs forged, the spotlight employer, its ATS and how that ATS reads a CV (`ATS_INTEL`), the candidate's real craft parsed from `cvs/<id>.md`, the day-of-week cadence, the narrative arc by edition (`PARTNERSHIP_ARC`) and a rotating chapter theme (`PARTNER_STORY_BEATS`, 32 beats, never repeated within 30 days).
- **Continuity memory** in `state/seen.json`: recent headlines, story beats, yesterday's micro-challenge and spotlight employer, so Lucy remembers yesterday.
- **Quality gate** (`_briefing_pack_quality`): forbidden dishonest vocabulary (hack / cheat / fake / fabricate ...), unfilled placeholders, length limits and near-duplicate headlines are rejected; the model gets exactly one revision round with the reason, otherwise the deterministic vault is used.
- **Per-subscriber language**: add `"briefing_language": "Spanish"` (or `"Portuguese"`) to a subscriber in `config.json`; default is English.

Each briefing renders nine AI-written parts: subject spark, chapter title, headline, message, *Their AI vs. Our AI* intel, Lucy's Corner, the 60-second play, Lucy's sign-off and a memory beat for tomorrow.

## 4. Run Offline Unit Tests

```bash
python3 -m unittest discover -s tests -b -v
```
