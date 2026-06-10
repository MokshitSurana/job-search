"""
Job alert script.

Fetches postings from Greenhouse / Lever / Ashby public job-board APIs,
filters for target roles, dedupes against previously-seen jobs, and emails
a digest of NEW postings.

Usage:
    python job_alert.py            # normal run (sends email if new jobs)
    python job_alert.py --dry-run  # print digest to stdout, no email, no state save

Required env vars (for email):
    GMAIL_ADDRESS       your gmail address
    GMAIL_APP_PASSWORD  16-char app password (https://myaccount.google.com/apppasswords)
    TO_ADDRESS          where to send the digest (can equal GMAIL_ADDRESS)
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "seen_jobs.json"
TIMEOUT = 20
MAX_WORKERS = 10           # concurrent board fetches
SEEN_RETENTION_DAYS = 90   # drop seen IDs not re-observed within this window
HEADERS = {"User-Agent": "job-alert-script/1.0"}


def _make_session():
    """Shared session with connection pooling + automatic retry/backoff on
    transient failures (connection errors and 429/5xx responses)."""
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,                       # 0.5s, 1s, 2s between tries
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=MAX_WORKERS)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _make_session()

US_HINTS = re.compile(
    r"\b(united states|usa|u\.s\.|remote.{0,10}us|us.{0,10}remote|"
    r"new york|nyc|san francisco|sf|bay area|seattle|austin|chicago|boston|"
    r"los angeles|denver|atlanta|miami|washington|palo alto|mountain view|"
    r"menlo park|sunnyvale|san jose|cambridge|pittsburgh|portland|dallas|"
    r"houston|phoenix|salt lake|raleigh|durham|bellevue|redmond|irvine|"
    r"san diego|remote, us|us-based remote)\b",
    re.IGNORECASE,
)

NON_US_HINTS = re.compile(
    r"\b(london|toronto|vancouver|dublin|berlin|munich|paris|amsterdam|zurich|z[uü]rich|"
    r"bangalore|bengaluru|hyderabad|mumbai|pune|delhi|gurgaon|gurugram|singapore|tokyo|"
    r"sydney|tel aviv|warsaw|canada|uk|united kingdom|germany|france|india|"
    r"ireland|netherlands|israel|australia|japan|poland|brazil|mexico|"
    r"china|beijing|shanghai|shenzhen|hong kong|korea|seoul|taiwan|taipei|"
    r"reykjav[ií]k|iceland|madrid|barcelona|spain|lisbon|portugal|estonia|"
    r"slovenia|ljubljana|serbia|belgrade|hungary|budapest|sweden|stockholm|"
    r"denmark|aarhus|copenhagen|norway|oslo|finland|helsinki|switzerland|"
    r"lithuania|vilnius|uae|dubai|abu dhabi|belgium|brussels|italy|milan|rome|"
    r"austria|vienna|czech|prague|romania|bucharest|argentina|buenos aires|"
    r"uruguay|colombia|bogot[aá]|chile|santiago|peru|s[aã]o paulo|latam|emea|apac|"
    r"philippines|manila|vietnam|indonesia|jakarta|thailand|bangkok|malaysia|"
    r"egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|turkey|istanbul|"
    r"ukraine|kyiv|new zealand|auckland|costa rica|\bch\b)\b",
    re.IGNORECASE,
)

# Mid/senior level markers in titles: "Engineer II", "Engineer 3",
# "(L3)", "8+ YOE", "Distinguished", "Experienced", postdocs, etc.
LEVEL_RE = re.compile(
    r"\b(ii|iii|iv|v)\b"
    r"|\b(engineer|scientist|developer|analyst|swe)\s*-?\s*[2-9]\b"
    r"|\(?\bl[3-9]\b\)?"
    r"|\b[2-9]\s*\+?\s*(?:-\s*\d+\s*)?yoe\b"
    r"|\(\s*\d+\s*[-+]\s*\d*\s*yoe\s*\)"
    r"|\bdistinguished\b|\bexperienced\b|\bpostdoc(?:toral)?\b",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------
# Fetchers — one per ATS, all normalize to the same dict shape:
# {id, company, title, location, url, ats}
# ----------------------------------------------------------------------

def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "id": f"gh-{slug}-{j['id']}",
            "company": slug,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "ats": "greenhouse",
        })
    return jobs


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        jobs.append({
            "id": f"lv-{slug}-{j.get('id', '')}",
            "company": slug,
            "title": j.get("text", ""),
            "location": cats.get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "ats": "lever",
        })
    return jobs


def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "id": f"ab-{slug}-{j.get('id', '')}",
            "company": slug,
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "ats": "ashby",
        })
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}

SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/"
                "New-Grad-Positions/dev/.github/scripts/listings.json")


def fetch_simplifyjobs(cfg):
    """Community-maintained new-grad feed; covers Big Tech / Workday
    companies the ATS fetchers can't reach. Pre-curated for new grads,
    so we filter by category + recency instead of role keywords."""
    sj = cfg.get("simplifyjobs") or {}
    if not sj.get("enabled"):
        return []
    r = SESSION.get(SIMPLIFY_URL, timeout=60)
    r.raise_for_status()
    listings = r.json()

    categories = set(sj.get("categories") or [])
    # tolerate legacy long names in the feed
    aliases = {"Software Engineering": "Software",
               "Data Science, AI & Machine Learning": "AI/ML/Data"}
    max_age = sj.get("max_age_days", 7) * 86400
    now = datetime.now(timezone.utc).timestamp()
    skip_citizen = sj.get("skip_citizenship_required", True)

    excludes = [k.lower().strip('" ') for k in cfg["exclude_keywords"]]
    # Feed entries are sometimes miscategorized (statisticians, attendants,
    # postdocs) — require an actual tech keyword in the title.
    tech_re = re.compile(
        r"engineer|software|developer|swe|programmer|machine learning|\bml\b|"
        r"\bai\b|data scien|scientist|deployment", re.IGNORECASE)
    jobs = []
    for j in listings:
        if not (j.get("active") and j.get("is_visible")):
            continue
        cat = aliases.get(j.get("category"), j.get("category"))
        if categories and cat not in categories:
            continue
        if now - j.get("date_posted", 0) > max_age:
            continue
        if skip_citizen and j.get("sponsorship") == "U.S. Citizenship is Required":
            continue
        title = j.get("title", "")
        if not tech_re.search(title):
            continue
        if LEVEL_RE.search(title):
            continue
        if any(k in title.lower() for k in excludes):
            continue
        location = ", ".join(j.get("locations") or [])
        if cfg.get("us_only") and location:
            if NON_US_HINTS.search(location) and not US_HINTS.search(location):
                continue
        jobs.append({
            "id": f"sj-{j.get('id', '')}",
            "company": j.get("company_name", "unknown"),
            "title": title,
            "location": location,
            "url": j.get("url", ""),
            "ats": "simplifyjobs",
        })
    return jobs


# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------

def matches(job, cfg):
    title = job["title"].lower()
    location = job["location"].lower()

    if not any(k.lower() in title for k in cfg["role_keywords"]):
        return False
    if any(k.lower().strip('" ') in title for k in cfg["exclude_keywords"]):
        return False
    if LEVEL_RE.search(title):
        return False
    if cfg.get("require_new_grad_signal"):
        if not any(k.lower() in title for k in cfg["new_grad_keywords"]):
            return False
    if cfg.get("us_only") and location:
        # Drop if a non-US place is mentioned and no US place is.
        if NON_US_HINTS.search(location) and not US_HINTS.search(location):
            return False
    return True


def is_new_grad_flavored(job, cfg):
    """Used only for sorting: float likely new-grad roles to the top."""
    title = job["title"].lower()
    return any(k.lower() in title for k in cfg["new_grad_keywords"])


# ----------------------------------------------------------------------
# State (dedupe)
# ----------------------------------------------------------------------

def load_seen():
    """Return {job_id: last_seen_unix_ts}. Tolerates the legacy flat-list
    format by stamping those IDs with the current time on first read."""
    if not STATE_PATH.exists():
        return {}
    data = json.loads(STATE_PATH.read_text())
    if isinstance(data, list):  # legacy format — migrate
        now = datetime.now(timezone.utc).timestamp()
        return {jid: now for jid in data}
    return data


def save_seen(seen):
    """Persist state, pruning IDs not re-observed within the retention
    window so the file (and its git history) doesn't grow unbounded."""
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - SEEN_RETENTION_DAYS * 86400
    pruned = {jid: ts for jid, ts in seen.items() if ts >= cutoff}
    STATE_PATH.write_text(json.dumps(pruned, indent=0, sort_keys=True))


# ----------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------

def build_html(new_jobs):
    date = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    rows = []
    by_company = {}
    for j in new_jobs:
        by_company.setdefault(j["company"], []).append(j)

    for company in sorted(by_company):
        rows.append(
            f"<h3 style='margin:18px 0 6px;color:#1a1a1a;'>"
            f"{html.escape(company.title())}</h3>"
        )
        for j in by_company[company]:
            rows.append(
                "<div style='margin:4px 0 10px;'>"
                f"<a href='{html.escape(j['url'])}' "
                "style='font-size:15px;color:#1155cc;text-decoration:none;'>"
                f"{html.escape(j['title'])}</a><br>"
                f"<span style='font-size:13px;color:#666;'>"
                f"{html.escape(j['location'] or 'Location unspecified')}</span>"
                "</div>"
            )

    return f"""\
<html><body style="font-family:Arial,Helvetica,sans-serif;max-width:640px;">
<h2 style="color:#1a1a1a;">🎯 {len(new_jobs)} new role(s) — {date}</h2>
{''.join(rows)}
<hr style="border:none;border-top:1px solid #ddd;margin-top:24px;">
<p style="font-size:12px;color:#999;">Automated job alert · edit config.yaml to tune companies/keywords</p>
</body></html>"""


def send_email(new_jobs):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("TO_ADDRESS", sender)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 {len(new_jobs)} new job posting(s) for you"
    msg["From"] = sender
    msg["To"] = to_addr
    msg.attach(MIMEText(build_html(new_jobs), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, password)
        s.sendmail(sender, [to_addr], msg.as_string())


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print results, don't email or save state")
    args = parser.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    seen = load_seen()
    first_run = not STATE_PATH.exists()

    all_matched, errors = [], []

    # Fetch all boards concurrently — one slow/hanging host no longer blocks
    # the rest, and ~90 sequential requests collapse to a few seconds.
    tasks = [(ats, slug) for ats, slugs in cfg["companies"].items()
             for slug in slugs]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(FETCHERS[ats], slug): (ats, slug)
                   for ats, slug in tasks}
        for fut in as_completed(futures):
            ats, slug = futures[fut]
            try:
                jobs = fut.result()
            except Exception as e:
                errors.append(f"{ats}/{slug}: {e}")
                continue
            all_matched.extend(j for j in jobs if matches(j, cfg))

    # SimplifyJobs feed (pre-curated; does its own filtering internally)
    try:
        all_matched.extend(fetch_simplifyjobs(cfg))
    except Exception as e:
        errors.append(f"simplifyjobs: {e}")

    # Drop cross-source duplicates (same posting via ATS + SimplifyJobs)
    deduped, seen_urls = [], set()
    for j in all_matched:
        key = j["url"].rstrip("/") or j["id"]
        if key in seen_urls:
            continue
        seen_urls.add(key)
        deduped.append(j)
    all_matched = deduped

    new_jobs = [j for j in all_matched if j["id"] not in seen]
    new_jobs.sort(key=lambda j: (not is_new_grad_flavored(j, cfg),
                                 j["company"], j["title"]))

    print(f"Fetched & matched: {len(all_matched)} | new: {len(new_jobs)}")
    for e in errors:
        print(f"  [warn] {e}", file=sys.stderr)

    if args.dry_run:
        for j in new_jobs:
            print(f"  {j['company']:<22} {j['title']}  [{j['location']}]")
        return

    if new_jobs:
        if first_run:
            # First run seeds the baseline; emailing hundreds of existing
            # posts isn't useful. Comment these two lines out if you DO
            # want the initial flood.
            print("First run — seeding state, skipping email "
                  f"({len(new_jobs)} existing posts recorded).")
        else:
            send_email(new_jobs)
            print(f"Emailed {len(new_jobs)} new job(s).")
    else:
        print("No new jobs.")

    # Refresh the timestamp on every currently-matched job so still-live
    # postings never age out of the retention window.
    now = datetime.now(timezone.utc).timestamp()
    for j in all_matched:
        seen[j["id"]] = now
    save_seen(seen)


if __name__ == "__main__":
    main()
