# job-radar

Pulls public job feeds from company career sites, keeps **director-level roles in Jalisco** (plus remote-Mexico roles), and builds a single static page. Anything it hasn't seen before is flagged **NEW**.

Works with Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Workday. Standard-library Python 3.9+, no installs.

## 1. Try it on your computer

```bash
python3 fetch_jobs.py --demo     # preview the page with fake data -> out/demo.html
python3 fetch_jobs.py --check    # test every company in companies.json
python3 fetch_jobs.py            # real run -> docs/index.html
```

**Run `--check` first.** Entries in `companies.json` marked `"confirmed": true` had their board name seen on a live career page during research. The rest use names written from memory and were never tested against the live sites. Entries that print `FAIL` have a wrong board name; fix or delete them. `employers.md` lists the bigger picture, including employers whose custom career sites this tool can't read.

## 2. Add a company

Open the company's careers page, look at the URL, and add a line to `companies.json`:

| Careers URL looks like | Entry |
|---|---|
| `boards.greenhouse.io/acme` or `job-boards.greenhouse.io/acme` | `{"name":"Acme","ats":"greenhouse","board":"acme"}` |
| `jobs.lever.co/acme` | `{"name":"Acme","ats":"lever","board":"acme"}` |
| `jobs.ashbyhq.com/acme` | `{"name":"Acme","ats":"ashby","board":"acme"}` |
| `apply.workable.com/acme` | `{"name":"Acme","ats":"workable","board":"acme"}` |
| `jobs.smartrecruiters.com/Acme` | `{"name":"Acme","ats":"smartrecruiters","board":"Acme"}` |
| `acme.wd5.myworkdayjobs.com/External` | `{"name":"Acme","ats":"workday","tenant":"acme","wd":"wd5","site":"External"}` |

Not supported: companies with a fully custom careers site (Google, Amazon, Microsoft, Oracle, IBM, Cisco, Accenture, Deloitte). LinkedIn and Indeed block automated access, so they are left out on purpose.

## 3. Tune the filter

Everything is in `config.json`: title patterns (`title_include` / `title_exclude`), which towns count as Jalisco, whether remote-Mexico and "Mexico (no city)" roles are included, and how many days a role stays NEW (`new_days`, default 7).

## 4. Run it automatically (free, GitHub)

1. Create a GitHub repository and push this folder to it.
2. **Settings > Actions > General > Workflow permissions**: choose *Read and write*.
3. **Actions** tab > *Refresh job board* > *Run workflow*. It then repeats every 6 hours.
   - The first run is a baseline: it records what exists and sends no alert.
   - After that, each newly found role opens a GitHub **Issue** assigned to you, and GitHub emails you.
   - Every run also saves the page as a downloadable artifact (open the run, scroll to *Artifacts*).
4. Optional web page: **Settings > Pages > Source: GitHub Actions**, then **Settings > Secrets and variables > Actions > Variables** and add `ENABLE_PAGES` = `true`.

### Privacy: read this before step 4

A job search is not something to broadcast. GitHub Pages on a free account only works for **public** repositories, so the page, the repo and its Issues would be visible to anyone who finds the URL. The page carries `noindex` and the repo name is neutral, but that is obscurity, not privacy.

- **Most private:** keep the repo **private**, skip step 4, and rely on the email Issues plus the downloadable artifact. A local `python3 fetch_jobs.py` also works with no hosting at all.
- **Private page:** host `docs/` on Cloudflare Pages and put Cloudflare Access (free for small teams) in front of it so only your email can open it.

GitHub may pause scheduled workflows in a repo with no activity for 60 days. The run commits `state/` each time, which normally prevents that; if it pauses anyway, re-enable it from the Actions tab.

## How "new" works

- **Posted date** comes from the company's feed when it offers a trustworthy one (Lever, Ashby, Workable, SmartRecruiters, Greenhouse `first_published`, Workday's "Posted N days ago").
- Otherwise the role is dated by **first seen**, tracked in `state/seen.json`.
- A role is NEW when its posted date, or its first-seen date (after the baseline run), is within `new_days`.
- If one board fails during a run, its last known roles stay on the page, tagged *last refresh failed*.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

These use fake feed responses, so they need no network.
