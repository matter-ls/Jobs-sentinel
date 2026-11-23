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

## 2. Privacy: what is published and what is not

**Keep the repository private.** It holds personal data: master CVs (`cvs/*.md`), email addresses and career goals (`config.json`, `subscribers/*.json`), and each run's summary and artifacts (tailored CVs).

`docs/` is the folder GitHub Pages serves. If Pages is enabled, anyone with the URL can read every file in it; `robots.txt` only asks search engines not to index.

- `docs/` contains only the job board (`index.html`, `jobs.json`), the sign-up page (`onboard.html`) and `robots.txt`.
- Tailored CVs are attached to the emails and saved under `out/cvs/` (a run artifact, never committed). They are **never** written under `docs/`. Earlier versions saved them in `docs/cvs`; the next run deletes that folder and the workflow's commit removes the files from the branch. They remain in the git history of earlier commits.
- On GitHub's free plan Pages needs a public repository. If that is your setup, a safer layout is to make this repository private and publish only the sign-up page from a small separate public repository that contains just a copy of the generated `docs/onboard.html`. The form posts to the relay, not to this repository. (This layout has not been tried end to end; the page's "Job board" link will not resolve in such a copy.)

One-minute check:

1. **Settings > General**: is the repository public or private?
2. **Settings > Pages**: is a site published?
3. In a private browser window, open the address of a CV you know was generated, `https://<user>.github.io/<repo>/cvs/<subscriber>/<file>.pdf`. After the first run with this version it must return 404.

## 3. Gemini Free-Tier Model Cascade (Nov 2026)

By default, `config.json` and `fetch_jobs.py` use **`gemini-4-hydrogen`** (`Gemini 4 Hydrogen`), Google's flagship **Free-Tier** workhorse model on the Gemini Developer API (`generativelanguage.googleapis.com/v1beta`), featuring >5x fewer fabricated claims, a 1M token unified context window, and `thinkingLevel: "low"` JSON generation.

If any model hits its Free-Tier RPM/RPD quota (`429`) or transient unavailability (`503`), `fetch_jobs.py` automatically cascades across distinct Free-Tier quota buckets:

1. `gemini-4-hydrogen` *(default — Free of charge on Gemini Developer API Free Tier)*
2. `gemini-3.8-flash` *(Free of charge)*
3. `gemini-3.5-flash-lite` *(Free of charge, ultra-high throughput GA Lite model)*
4. `gemini-3.1-flash-lite` *(Free of charge)*
5. `gemini-flash-latest` + live `/v1beta/models` auto-discovery

## 4. Multi-Stage AI Headhunter Pipeline

1. **Radar Company Intelligence (`companies.json`)**: Every one of the 109 tracked companies includes curated `"industry"` and `"description"` metadata (what the company builds, its business model, and its Guadalajara/Jalisco or Remote-Mexico footprint).
2. **Enhanced Candidate Employer Profiles (`cvs/*.md` & Tailored CVs)**: Each subscriber's master CV includes ATS-clean `*Company Profile: ...*` context under every employer, carried through into tailored `.docx`, `.pdf`, and `.html` resumes (`company_context`).
3. **Title and location rules**: A title passes when no `title_exclude` pattern matches and either a `title_include` pattern matches or every group in `title_all_of` has a match ("one of my levels AND one of my job areas", used by sign-up profiles). Patterns that could make matching extremely slow (a quantifier inside a repeated group) are skipped with a warning.
4. **GenAI Candidate-Job Match Gate (`assess_candidate_job_matches`)**:
   - Never-sent roles are ordered by overlap with the subscriber's goal and liked roles, then the first 24 are reviewed per run; the rest roll over to the next run.
   - Each role is validated against **(a)** the subscriber's `career_goal`, **(b)** real experience & company domain background in `cvs/<id>.md`, **(c)** the hiring employer's `industry` and `description`, and **(d)** roles the subscriber liked.
   - Roles where `is_good_match = false` or `match_score < min_match_score` (default `70`) are **suppressed from Lucy's email report** and recorded in `state/seen.json` (`rejected_matches`) so they are not reviewed again. When a subscriber updates their profile through the sign-up form, those rejections are cleared and reviewed against the new profile.
   - **The gate fails closed.** If a Gemini key is configured but the review cannot run (outage, quota, rejected key), the roles are **held**: not sent, not recorded, and reviewed on the next run. The subscriber gets a short "AI review paused" note instead of a list. No score or rationale is ever invented.
   - **Without a Gemini key** roles are still sent, labelled **Not AI-reviewed**, with no percentage and no rationale.
5. **Match Rationale on Every Role Card**: Every role that passes the gate is shown as a card with the hiring company's industry, **Lucy's Match Rationale** (`match_rationale` + `industry_synergy`), its match score and, for top picks, a marker that a tailored `.docx` is attached. To keep the email short, the card leaves out the longer company description (the gate still reads it), the team, keyword lists and attachment file names. The plain-text part carries the same information.
6. **Lucy's morning note**: Gemini writes one short note per briefing from a grounded prompt: the real facts of the night, the candidate's real record, the story so far (`state/seen.json`), and three things that change every day so the notes never blur together: a **lens** (the idea, 32 of them, none returns before all were used), a **shape** (form and length, 8, none within 5 days) and a **play** (the kind of one-minute action, none within 5 days). The prompt also carries a *do not repeat* section built from the last two weeks (headlines, openings, plays and words worn out lately). A quality gate (`_briefing_pack_quality`) refuses a draft that repeats a recent note or play, leans on worn words, invents an attached CV, points at roles on a quiet day, uses dishonest vocabulary or the retired "us against their software" theme; one revision is requested, then the built-in note for the day's lens is used. The built-in texts follow the same rotation, so a run without Gemini is varied too. The email shows the note (title, headline, message) and the play; nothing else is written.

## 5. Feedback: "Like this role"

Every role in a briefing has a **Like this role** link. It opens a pre-filled email to Lucy's own address; the subscriber presses send (and may add a line after "Why (optional):").

- On the next daily run Lucy reads those emails, checks the signed reference in the subject, and stores the like in `state/seen.json`. The following briefing confirms it ("Noted, thank you: you liked ...").
- Liked roles move similar new roles to the front of the review queue and are shown to the match gate as a preference signal. A like never overrides a clear mismatch with the CV.
- To read the likes the workflow logs in to the **sending mailbox over IMAP** with the same app password. It opens the mailbox read-only, never marks anything as read, and only looks at messages whose subject contains `LUCYLIKE`. Like emails must stay in the inbox until the next run (or set `LUCY_FEEDBACK_MAILBOX` to the label a Gmail filter moves them to).
- Links are signed with a key derived from the mail password. If you change the password, likes from emails sent before the change are ignored; set the `LUCY_FEEDBACK_SECRET` secret to keep links valid across password changes.
- Non-Gmail senders: set the `IMAP_HOST` variable, otherwise likes are not read.
- To switch the feature off completely (no Like links, mailbox never read), set `"like_feedback": false` in `config.json`.

## 6. Configure GitHub Actions (`.github/workflows/main.yml`)

1. **Keep the repository private** (see section 2).
2. Generate a 16-character **Google App Password** at `https://myaccount.google.com/apppasswords` (requires 2-Step Verification).
3. **Settings > Secrets and variables > Actions > Secrets** – add:
   - `GMAIL_USER`: the Gmail address that sends the digest
   - `GMAIL_APP_PASSWORD`: the 16-character App Password
   - `GEMINI_API_KEY`: enables the AI Match Gate, Match Rationales, Tailored CVs, and AI Partner Briefings. Without it, roles are sent labelled "Not AI-reviewed" and no CVs are built.
   - `LUCY_FEEDBACK_SECRET` *(optional)*: fixed signing secret for like links
4. *(Optional)* **Variables** tab:
   - `GEMINI_MODEL`: override default `gemini-4-hydrogen`
   - `GEMINI_MATCH_MODEL`: optional separate model override specifically for the Candidate-Job Match Gate
   - `MIN_MATCH_SCORE`: override default `70` minimum match score threshold
   - `ONBOARD_WEBHOOK_URL`: Google Apps Script / Cloudflare Worker relay URL for `docs/onboard.html` (see [`onboarding/README.md`](./onboarding/README.md))
   - `LUCY_FEEDBACK_MAILBOX`, `IMAP_HOST`: see section 5
5. **Settings > Actions > General > Workflow permissions** – select **Read and write permissions** (the daily workflow commits `state/` and `docs/`; the onboarding workflow commits `subscribers/<id>.json` and `cvs/<id>.md`).

## 7. Candidate Self-Service Onboarding (`docs/onboard.html` + `onboard_subscriber.py`)

New teammates can join Jobs Sentinel via the built-in onboarding webpage (`docs/onboard.html`, linked directly from `docs/index.html`):

1. **Webpage Form**: Collects **Full Name**, **Email**, **Plain-Text CV Upload** (`.txt` / `.md` drag-and-drop or direct paste), **Target Seniority Checkboxes**, **Functional Domain Checkboxes**, **Location Checkboxes**, **Briefing Language**, and a **Free-Text Career Vision** field.
2. **Automated Codebase Inclusion (`.github/workflows/onboard.yml`)**:
   - Triggered via [`onboarding/google_apps_script.gs`](./onboarding/google_apps_script.gs) (or [`onboarding/cloudflare_worker.js`](./onboarding/cloudflare_worker.js) / manual `workflow_dispatch` / local `python3 onboard_subscriber.py --serve`).
   - Structures & enriches the CV into `cvs/<id>.md` with `*Company Profile: ...*` descriptions under every past employer, synthesizes a `career_goal` and title rules, and stores the profile as **`subscribers/<id>.json`** (one file per person; `config.json` is never edited by sign-ups). It then runs the full unit test suite, commits those two files, and emails a **Welcome & Confirmation Email** from Lucy. That email is built from the same parts as the daily briefing, lists the settings Lucy understood, and mentions tailored CVs only when they are switched on for that person.
   - Subscribers listed in `config.json` and those in `subscribers/` are merged at load time. If both define the same id, the file wins.
3. **Require an invite code.** Without one, anyone who has the link can enrol any email address. Set `INVITE_CODE` in the relay's Script Properties (enforced there) and `"onboard_require_invite_code": true` in `config.json` (shows the field on the page). The daily run prints a reminder while the flag is off.

```bash
python3 onboard_subscriber.py --serve --port 8080             # run live local onboarding portal at http://127.0.0.1:8080/onboard.html
python3 onboard_subscriber.py --from-file packet.json         # onboard from exported JSON packet
python3 onboard_subscriber.py --render-page docs/onboard.html # rebuild standalone onboarding HTML
```

## 8. Run Offline Unit Tests

```bash
python3 -W error::SyntaxWarning -m unittest discover -s tests -b -v
```
