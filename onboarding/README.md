# Candidate Self-Service Onboarding Guide (`docs/onboard.html` + `onboard.yml`)

Jobs Sentinel includes a complete, zero-dependency onboarding portal that lets new teammates or candidates join the tool via a clean web form:

1. **Webpage Form (`docs/onboard.html`)**: Collects **Full Name**, **Email**, **Plain-Text CV Upload** (`.txt` / `.md` drag-and-drop or direct paste), **Target Seniority Checkboxes**, **Functional Domain Checkboxes**, **Location Checkboxes**, **Language**, and a **Free-Text Career Vision** box.
2. **Automatic Codebase Inclusion (`onboard_subscriber.py` + `.github/workflows/onboard.yml`)**:
   - Structures their raw CV into `cvs/<subscriber_id>.md` using Free-Tier **`gemini-4-hydrogen`**, automatically enriching every past employer with `*Company Profile: ...*` industry/scale descriptions and scrubbing ATS-illegal characters.
   - Calibrates their `career_goal`, `title_include`, `upgrade_title_include`, and `title_exclude` regex rules in `config.json`.
   - Runs the full offline unit test suite (`tests/test_offline.py`) to verify 100% ATS & config integrity before committing `cvs/<id>.md` and `config.json` directly to your repository.
3. **Executive Confirmation Email**: Sends a polished confirmation email from **Lucy · Personal Headhunter** summarizing their calibrated Career North Star, target roles, enriched CV employers, and daily briefing schedule—and optionally triggers their **first immediate match briefing**!

---

## Option A: Cloud Deployment (GitHub Pages + Free Google Apps Script Relay — 2 Minutes, $0 Cost)

Because static HTML pages cannot safely hold a GitHub Personal Access Token in public browser JavaScript, `onboarding/google_apps_script.gs` acts as a tiny serverless bridge using the **same Google account** you already use for Gmail:

1. **Create a Fine-Grained GitHub Token**:
   - Go to **GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**.
   - Repository access: **Only select repositories** -> select your `Jobs-sentinel` repo.
   - Repository permissions: **Contents: Read and write** (this allows `repository_dispatch`).
2. **Deploy the 1-File Google Apps Script Relay**:
   - Open `https://script.google.com` -> **New project** -> paste [`google_apps_script.gs`](./google_apps_script.gs).
   - Open **Project Settings (gear icon) > Script Properties** and add:
     - `GITHUB_REPO`: `<your-owner>/<your-repo>`
     - `GITHUB_PAT`: `<your-fine-grained-token>`
     - `INVITE_CODE`: *(optional)* e.g. `lucy2026` (if set, also set `"onboard_require_invite_code": true` in `config.json`)
   - Click **Deploy > New deployment > Web app** (*Execute as: Me*, *Who has access: Anyone*) and copy the `https://script.google.com/macros/s/.../exec` URL.
   - *(Prefer Cloudflare Workers instead? Deploy [`cloudflare_worker.js`](./cloudflare_worker.js) with the same 3 environment secrets.)*
3. **Connect the Webhook URL**:
   - In GitHub **Settings > Secrets and variables > Actions > Variables**, add `ONBOARD_WEBHOOK_URL` = your `.../exec` URL (or paste it into `"onboard_webhook_url"` in `config.json`).
   - On the next run (or `python3 onboard_subscriber.py --render-page docs/onboard.html`), `docs/onboard.html` is automatically built with your live endpoint!

---

## Option B: Run the Interactive Web Portal Locally (`--serve`)

You can also host the full onboarding webpage locally at any time with zero cloud setup:

```bash
export GEMINI_API_KEY="your-key"
export GMAIL_USER="your@gmail.com"
export GMAIL_APP_PASSWORD="your-16-char-app-password"

python3 onboard_subscriber.py --serve --port 8080
# Open http://127.0.0.1:8080/onboard.html in your browser!
```
