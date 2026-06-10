# Daily Job Alert 🎯

Every morning, this script checks the job boards of ~45 target companies,
filters for new-grad SWE / ML / FDE / Applied AI roles in the US, and emails
you only the postings you haven't seen before.

No scraping, no LinkedIn ToS issues — it uses the **public JSON APIs** that
Greenhouse, Lever, and Ashby expose for every company hosted on them, plus the
community-maintained **SimplifyJobs New-Grad feed**, which covers Big Tech and
Workday-hosted companies (Google, Meta, Nvidia, Tesla, banks, etc.) that the
ATS APIs can't reach. The SimplifyJobs source also drops roles marked
"U.S. Citizenship is Required" by default (configurable).

## How it works

```
config.yaml  ──►  job_alert.py  ──►  fetch each board's API
                                     filter titles (roles + exclusions)
                                     dedupe vs seen_jobs.json
                                     email digest via Gmail SMTP
```

## Setup (one time, ~10 minutes)

### 1. Gmail app password
1. Enable 2FA on your Google account if you haven't.
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (16 characters) — this is NOT your normal password.

### 2. Test locally
```bash
pip install -r requirements.txt
python job_alert.py --dry-run        # prints matches, no email
```

The **first real run** just seeds `seen_jobs.json` with all current postings
(so you don't get a 300-job email). From the second run onward you only get
genuinely new posts.

```bash
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
export TO_ADDRESS="you@gmail.com"
python job_alert.py                  # run 1: seeds state
python job_alert.py                  # run 2+: emails new jobs
```

### 3. Automate with GitHub Actions (free, runs even when laptop is off)
1. Create a **private** GitHub repo and push this folder.
2. Repo → Settings → Secrets and variables → Actions → add:
   - `GMAIL_ADDRESS`
   - `GMAIL_APP_PASSWORD`
   - `TO_ADDRESS`
3. Done. The workflow in `.github/workflows/daily.yml` runs at 8 AM Chicago
   time daily and commits `seen_jobs.json` back so dedupe persists.
4. You can also trigger it manually: Actions tab → Daily Job Alert → Run workflow.

## Tuning

Everything lives in `config.yaml`:

- **Add a company**: find its board slug from its careers page URL
  (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`)
  and add it under the right ATS.
- **Too many results?** Set `require_new_grad_signal: true` so only titles
  with "new grad" / "early career" / "2026" etc. get through.
- **Want internships too?** Delete the `intern` line from `exclude_keywords`.
- **SimplifyJobs feed**: tune under `simplifyjobs:` in config — categories
  (`Software`, `AI/ML/Data`, `Hardware`, `Quant`, `Product`), how many days
  back to look (`max_age_days`), and citizenship filtering. Set
  `enabled: false` to turn it off.

## Notes

- A handful of company slugs may be wrong or change over time — the script
  logs a warning per failed company and keeps going. Run `--dry-run` once to
  see which slugs error and fix them in `config.yaml`.
- GitHub Actions cron can fire up to ~15 min late; that's normal.
