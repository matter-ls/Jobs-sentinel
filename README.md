# job-radar (Jobs Sentinel)

Pulls public job feeds from **109 verified Jalisco & Remote-Mexico employer career sites** across 9 supported ATS platforms (Workday, Greenhouse, SmartRecruiters, Oracle HCM, Eightfold, Lever, Ashby, BambooHR, and Workable), enriches every posting with curated **company industry & business descriptions** (`companies.json`), validates candidate-job fit via a dedicated **Gemini AI Match Gate** against each subscriber's career north star (`career_goal`) and enhanced master CV (`cvs/*.md`), tailors ATS-safe `.docx` / `.pdf` / `.html` resumes for top matches, and emails **Lucy's Private Talent Briefing** showing **only genuine matches with clear match rationales**.

Uses standard-library Python 3.9+ with zero third-party package installs.

## 1. Try it on your computer

```bash
python3 fetch_jobs.py --demo                         # preview the page with fake data -> out/demo.html
python3 fetch_jobs.py --check                        # test every company in companies.json
python3 fetch_jobs.py --no-email                     # real run -> docs/index.html + state/, no email
python3 fetch_jobs.py                                # real run + AI match screening + CVs + email
python3 fetch_jobs.py --email-all                    # real run + re-evaluate & email all current matches
python3 fetch_jobs.py --subscriber luis              # only email one subscriber id
python3 fetch_jobs.py --model gemini-4-hydrogen      # override Gemini model
python3 fetch_jobs.py --min-match-score 75           # override minimum AI match gate score (0-100)
```

Exit codes: `0` ok · `1` nothing written (every board failed) · `2` outputs written but at least one email digest could not be sent (that subscriber's state — including emailed & AI-rejected ledgers — is rolled back, so the same roles are retried next run).

## 2. Gemini Free-Tier Model Cascade (Nov 2026)

By default, `config.json` and `fetch_jobs.py` use **`gemini-4-hydrogen`** (`Gemini 4 Hydrogen`), Google's flagship **Free-Tier** workhorse model on the Gemini Developer API (`generativelanguage.googleapis.com/v1beta`), featuring >5x fewer fabricated claims, a 1M token unified context window, and `thinkingLevel: "low"` JSON generation.

If any model hits its Free-Tier RPM/RPD quota (`429`) or transient unavailability (`503`), `fetch_jobs.py` automatically cascades across distinct Free-Tier quota buckets:

1. `gemini-4-hydrogen` *(default — Free of charge on Gemini Developer API Free Tier)*
2. `gemini-3.8-flash` *(Free of charge)*
3. `gemini-3.5-flash-lite` *(Free of charge, ultra-high throughput GA Lite model)*
4. `gemini-3.1-flash-lite` *(Free of charge)*
5. `gemini-flash-latest` + live `/v1beta/models` auto-discovery

## 3. Multi-Stage AI Headhunter Pipeline

1. **Radar Company Intelligence (`companies.json`)**: Every one of the 109 tracked companies includes curated `"industry"` and `"description"` metadata (what the company builds, its business model, and its Guadalajara/Jalisco or Remote-Mexico footprint).
2. **Enhanced Candidate Employer Profiles (`cvs/*.md` & Tailored CVs)**: Each subscriber's master CV includes ATS-clean `*Company Profile: ...*` context under every employer, carried through into tailored `.docx`, `.pdf`, and `.html` resumes (`company_context`).
3. **GenAI Candidate-Job Match Gate (`assess_candidate_job_matches`)**:
   - Hydrates full job descriptions concurrently and evaluates candidate roles in batches via `LLM_JOB_MATCH_SYSTEM_PROMPT`.
   - Validates each role against **(a)** the subscriber's `career_goal`, **(b)** real experience & company domain background in `cvs/<id>.md`, and **(c)** the hiring employer's `industry` and `description`.
   - Roles where `is_good_match = false` or `match_score < min_match_score` (default `70`) are **suppressed from Lucy's email report** and recorded in `state/seen.json` (`rejected_matches`) so they are never re-evaluated on subsequent days.
4. **Match Rationale on Every Report Card**: Every role that passes the gate displays the hiring company's industry & profile plus **Lucy's Match Rationale** (`match_rationale` + `industry_synergy`) in both HTML and plain-text digests, alongside tailored `.docx` resumes for top picks.
5. **Lucy's AI Partner Briefing ("Us vs. Their Machine")**: Opens every digest with a 9-part Gemini-authored narrative grounded in real overnight telemetry, continuity memory (`state/seen.json`), spotlight employer profile + ATS parsing behavior (`ATS_INTEL`), and a strict honesty quality gate (`_briefing_pack_quality`).

## 4. Configure GitHub Actions (`.github/workflows/main.yml`)

1. **Keep the repository private.** `cvs/*.md` contain personal CVs, and the tailored CVs are uploaded as workflow artifacts.
2. Generate a 16-character **Google App Password** at `https://myaccount.google.com/apppasswords` (requires 2-Step Verification).
3. **Settings > Secrets and variables > Actions > Secrets** – add:
   - `GMAIL_USER`: the Gmail address that sends the digest
   - `GMAIL_APP_PASSWORD`: the 16-character App Password
   - `GEMINI_API_KEY`: enables the AI Match Gate, Match Rationales, Tailored CVs, and AI Partner Briefings (falls back cleanly if unset).
4. *(Optional)* **Variables** tab:
   - `GEMINI_MODEL`: override default `gemini-4-hydrogen`
   - `GEMINI_MATCH_MODEL`: optional separate model override specifically for the Candidate-Job Match Gate
   - `MIN_MATCH_SCORE`: override default `70` minimum match score threshold
   - `ONBOARD_WEBHOOK_URL`: Google Apps Script / Cloudflare Worker relay URL for `docs/onboard.html` (see [`onboarding/README.md`](./onboarding/README.md))
5. **Settings > Actions > General > Workflow permissions** – select **Read and write permissions** (the workflows commit `state/`, `docs/`, `cvs/`, and `config.json` back to the branch).

## 5. Candidate Self-Service Onboarding (`docs/onboard.html` + `onboard_subscriber.py`)

New teammates can join Jobs Sentinel via the built-in onboarding webpage (`docs/onboard.html`, linked directly from `docs/index.html`):

1. **Webpage Form**: Collects **Full Name**, **Email**, **Plain-Text CV Upload** (`.txt` / `.md` drag-and-drop or direct paste), **Target Seniority Checkboxes**, **Functional Domain Checkboxes**, **Location Checkboxes**, **Briefing Language**, and a **Free-Text Career Vision** field.
2. **Automated Codebase Inclusion (`.github/workflows/onboard.yml`)**:
   - Triggered via [`onboarding/google_apps_script.gs`](./onboarding/google_apps_script.gs) (or [`onboarding/cloudflare_worker.js`](./onboarding/cloudflare_worker.js) / manual `workflow_dispatch` / local `python3 onboard_subscriber.py --serve`).
   - Structures & enriches their CV into `cvs/<id>.md` with `*Company Profile: ...*` descriptions under every past employer, synthesizes their `career_goal` + regex rules in `config.json`, runs the full unit test suite to gate quality, commits `cvs/<id>.md` + `config.json` to the repository, and emails them a polished **Welcome & Confirmation Email** from Lucy!

```bash
python3 onboard_subscriber.py --serve --port 8080             # run live local onboarding portal at http://127.0.0.1:8080/onboard.html
python3 onboard_subscriber.py --from-file packet.json         # onboard from exported JSON packet
python3 onboard_subscriber.py --render-page docs/onboard.html # rebuild standalone onboarding HTML
```

## 6. Run Offline Unit Tests

```bash
python3 -W error::SyntaxWarning -m unittest discover -s tests -b -v
```

