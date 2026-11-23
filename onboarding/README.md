# Candidate Self-Service Onboarding Guide (`docs/onboard.html` + `onboard.yml`)

Jobs Sentinel includes a zero-dependency onboarding portal that lets new teammates or candidates join via a web form:

1. **Webpage Form (`docs/onboard.html`)**: Collects **Name**, **Email**, **Plain-Text CV** (`.txt` / `.md` upload or paste), **Role level**, **Job area**, **Location**, **Email language**, and a free-text **What are you looking for?** box. The free text is the main signal; the pills are deliberately broad.
2. **Automatic registration (`onboard_subscriber.py` + `.github/workflows/onboard.yml`)**:
   - Structures the CV into `cvs/<subscriber_id>.md` using Free-Tier **`gemini-4-hydrogen`**, adding a `*Company Profile: ...*` line under every past employer and scrubbing ATS-illegal characters. If Gemini is unavailable or its output cannot be used, an offline parser is used instead.
   - Writes the profile to **`subscribers/<subscriber_id>.json`**: the `career_goal`, title rules and location settings. `config.json` is never edited by a sign-up.
   - Refuses to store a profile that would break the daily run (unusable CV, invalid or slow pattern, no title rule).
   - Runs the full offline test suite, then commits only those two files.
3. **Confirmation email** from **Lucy · Personal Headhunter**, sent after the commit landed. It looks like the daily briefing (same frame, Lucy's dark card, one card with the radar settings). If the CV could not be structured, it says so in one amber note and promises no tailored CVs.

## How title rules are built

The model never writes regular expressions. It returns plain phrases (target titles, focus keywords, step-up titles, titles to avoid) and the patterns are built in code with every character escaped.

A title is accepted when it:

- contains one of the target-title phrases, or
- is a director or executive title and that level was ticked, or
- is a level-less individual-contributor title of a ticked job area (Specialist or Senior ticked), for example "Account Executive", or
- contains **both** a word of a ticked level (specialist, senior, manager) **and** a word of a ticked job area or one of the focus keywords.

If Senior or Manager is ticked but there is no job area and no phrase to narrow it, the level words are accepted alone and the profile is stored with `"rule_breadth": "broad"` (the run log warns about it). Every accepted role still goes through the AI match review.

People who signed up before this version keep their older, wider rules until they submit the form again with the same email address.

## Updating a profile

Submitting the form again with the same email address updates the same subscriber id. The new profile carries a `profile_updated_at` stamp; on the next daily run the roles that the AI rejected under the old profile are reviewed again. Roles already emailed are never sent twice.

## Concurrency

- Sign-ups run in their own concurrency group, one per email address. They no longer share a group with the daily refresh, where a new run could replace a waiting one.
- Each sign-up commits only `subscribers/<id>.json` and `cvs/<id>.md`. If the push is rejected because the branch moved, the workflow syncs with the branch and runs the registration and the tests again; nothing is copied back from the earlier attempt.
- `send_initial_matches` (manual runs only) starts the normal daily workflow for that subscriber. If a daily run is already waiting in the queue at that moment, GitHub replaces it with the new one, so use it when no daily run is pending.

These workflow changes were checked by reading and by parsing the YAML; they could not be executed outside GitHub.

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
     - `INVITE_CODE`: a passcode you share with the people you invite, e.g. `lucy2026`. **Set this.** Without it anyone who has the link can enrol any email address and trigger a workflow run. Also set `"onboard_require_invite_code": true` in `config.json` so the page asks for it.
   - Click **Deploy > New deployment > Web app** (*Execute as: Me*, *Who has access: Anyone*) and copy the `https://script.google.com/macros/s/.../exec` URL.
   - *(Prefer Cloudflare Workers instead? Deploy [`cloudflare_worker.js`](./cloudflare_worker.js) with the same 3 environment secrets.)*
3. **Connect the Webhook URL**:
   - In GitHub **Settings > Secrets and variables > Actions > Variables**, add `ONBOARD_WEBHOOK_URL` = your `.../exec` URL (or paste it into `"onboard_webhook_url"` in `config.json`).
   - On the next daily run (or `python3 onboard_subscriber.py --render-page docs/onboard.html`), `docs/onboard.html` is rebuilt with your live endpoint.

Script Properties are read on every request, so setting or changing `INVITE_CODE` should take effect without redeploying the relay. Check it once: submit the form without the code; it must answer "Invalid Team Invite Code." The relay scripts themselves are unchanged in this version.

Not included: confirming that an email address belongs to the person who typed it (double opt-in), an unsubscribe link, and a cap on sign-ups per hour. Until those exist, the invite code is what keeps the form from being misused. To remove someone, set `"enabled": false` in their `subscribers/<id>.json` (or delete the file).

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
