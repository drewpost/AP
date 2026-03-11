"""ATS career page scraper for Greenhouse, Lever, and Ashby job boards.

Follows the workday.py pattern: pure HTTP, no browser, no LLM.
Reads company registry from config/companies.yaml and scrapes public
board APIs in parallel.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path

import requests
import yaml

from applypilot.database import get_connection, store_jobs

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "companies.yaml"

_SESSION_TIMEOUT = 30
_USER_AGENT = "ApplyPilot/1.0 (job-search-tool)"

# Title keywords for product management roles (case-insensitive).
# A job title must contain at least one of these to be kept.
_TITLE_KEYWORDS = [
    "product manag", "product direct", "product lead", "product own",
    "head of product", "vp product", "vp of product", "vp, product",
    "director of product", "director, product", "chief product",
    "group product", "staff product", "principal product",
    "platform product", "technical product", "senior product",
]


def _title_matches(title: str) -> bool:
    """Return True if the job title looks like a product management role."""
    if not title:
        return False
    t = title.lower()
    return any(kw in t for kw in _TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# HTML stripping helper (same approach as workday.py)
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    """Strip HTML tags, keep text content."""

    def __init__(self):
        super().__init__()
        self._text = StringIO()
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4"):
            self._text.write("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("p", "div", "li"):
            self._text.write("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text.write(data)

    def get_text(self) -> str:
        return self._text.getvalue().strip()


def _strip_html(html: str | None) -> str:
    if not html:
        return ""
    parser = _HTMLStripper()
    parser.feed(html)
    return parser.get_text()


# ---------------------------------------------------------------------------
# Company registry loader
# ---------------------------------------------------------------------------

def load_companies(config_path: Path | str | None = None) -> dict:
    """Load company registry from companies.yaml."""
    path = Path(config_path) if config_path else _CONFIG_PATH
    if not path.exists():
        log.warning("Company registry not found at %s", path)
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("companies", {})


# ---------------------------------------------------------------------------
# Greenhouse API
# ---------------------------------------------------------------------------

def greenhouse_jobs(board_token: str, company_name: str, category: str) -> list[dict]:
    """Fetch all jobs from a Greenhouse board.

    API: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    """
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    try:
        resp = requests.get(
            url,
            params={"content": "true"},
            headers={"User-Agent": _USER_AGENT},
            timeout=_SESSION_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Greenhouse %s (%s) failed: %s", company_name, board_token, e)
        return []

    data = resp.json()
    raw_jobs = data.get("jobs", [])
    jobs = []

    for j in raw_jobs:
        location_name = ""
        if j.get("location"):
            location_name = j["location"].get("name", "")

        description = _strip_html(j.get("content", ""))

        job_url = j.get("absolute_url", "")
        if not job_url:
            job_url = f"https://boards.greenhouse.io/{board_token}/jobs/{j.get('id', '')}"

        jobs.append({
            "url": job_url,
            "title": j.get("title", ""),
            "description": description[:500] if description else "",
            "location": location_name,
            "salary": "",
            "company_tag": category,
        })

    log.info("Greenhouse %s: %d jobs", company_name, len(jobs))
    return jobs


# ---------------------------------------------------------------------------
# Lever API
# ---------------------------------------------------------------------------

def lever_jobs(company_slug: str, company_name: str, category: str) -> list[dict]:
    """Fetch all jobs from a Lever postings page.

    API: GET https://api.lever.co/v0/postings/{slug}?mode=json
    """
    url = f"https://api.lever.co/v0/postings/{company_slug}"
    try:
        resp = requests.get(
            url,
            params={"mode": "json"},
            headers={"User-Agent": _USER_AGENT},
            timeout=_SESSION_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Lever %s (%s) failed: %s", company_name, company_slug, e)
        return []

    raw_jobs = resp.json()
    if not isinstance(raw_jobs, list):
        log.warning("Lever %s: unexpected response type %s", company_name, type(raw_jobs))
        return []

    jobs = []
    for j in raw_jobs:
        location = j.get("categories", {}).get("location", "")
        description_plain = j.get("descriptionPlain", "")
        if not description_plain:
            description_plain = _strip_html(j.get("description", ""))

        jobs.append({
            "url": j.get("hostedUrl", ""),
            "title": j.get("text", ""),
            "description": description_plain[:500] if description_plain else "",
            "location": location,
            "salary": "",
            "company_tag": category,
        })

    log.info("Lever %s: %d jobs", company_name, len(jobs))
    return jobs


# ---------------------------------------------------------------------------
# Ashby API
# ---------------------------------------------------------------------------

def ashby_jobs(org_slug: str, company_name: str, category: str) -> list[dict]:
    """Fetch all jobs from an Ashby job board.

    API: POST https://api.ashbyhq.com/posting-api/job-board/{slug}
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org_slug}"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_SESSION_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Ashby %s (%s) failed: %s", company_name, org_slug, e)
        return []

    data = resp.json()
    raw_jobs = data.get("jobs", [])
    jobs = []

    for j in raw_jobs:
        location = j.get("location", "")
        if isinstance(location, dict):
            location = location.get("name", "")

        description = _strip_html(j.get("descriptionHtml", ""))
        job_url = j.get("jobUrl", "")
        if not job_url:
            posting_id = j.get("id", "")
            job_url = f"https://jobs.ashbyhq.com/{org_slug}/{posting_id}"

        jobs.append({
            "url": job_url,
            "title": j.get("title", ""),
            "description": description[:500] if description else "",
            "location": location if isinstance(location, str) else "",
            "salary": "",
            "company_tag": category,
        })

    log.info("Ashby %s: %d jobs", company_name, len(jobs))
    return jobs


# ---------------------------------------------------------------------------
# Single company scraper (dispatches by ATS type)
# ---------------------------------------------------------------------------

def _scrape_company(key: str, info: dict) -> list[dict]:
    """Scrape a single company's job board based on ATS type."""
    ats = info.get("ats", "")
    name = info.get("name", key)
    category = info.get("category", "")

    log.info("Scraping %s (%s)...", name, ats)

    if ats == "greenhouse":
        token = info.get("board_token", key)
        return greenhouse_jobs(token, name, category)
    elif ats == "lever":
        slug = info.get("company_slug", key)
        return lever_jobs(slug, name, category)
    elif ats == "ashby":
        slug = info.get("org_slug", key)
        return ashby_jobs(slug, name, category)
    elif ats == "workday":
        # Delegate to existing workday scraper — skip here to avoid duplication
        log.debug("Skipping %s (workday) — handled by workday.py", name)
        return []
    else:
        log.warning("Unknown ATS type '%s' for %s", ats, name)
        return []


# ---------------------------------------------------------------------------
# Parallel ATS discovery runner
# ---------------------------------------------------------------------------

def run_ats_discovery(
    workers: int = 4,
    config_path: Path | str | None = None,
    stop_event: "threading.Event | None" = None,
    progress_callback: "callable | None" = None,
    skip_companies: "set | None" = None,
) -> dict:
    """Scrape all company career pages in parallel and store results.

    Args:
        workers: Number of parallel threads.
        config_path: Override path to companies.yaml.
        stop_event: If set, stop gracefully between companies.
        progress_callback: Called with (current, total, company_name) after each company.
        skip_companies: Set of company keys to skip (for resume support).

    Returns:
        Dict with stats: companies_scraped, total_found, new, existing, errors, completed_keys.
    """
    import threading as _threading

    companies = load_companies(config_path)
    if not companies:
        log.warning("No companies loaded from registry")
        return {"companies_scraped": 0, "total_found": 0, "new": 0, "existing": 0, "completed_keys": []}

    # Filter out workday companies and already-completed ones
    active = {
        k: v for k, v in companies.items()
        if v.get("ats") != "workday" and (skip_companies is None or k not in skip_companies)
    }

    conn = get_connection()
    total_found = 0
    total_new = 0
    total_existing = 0
    errors = 0
    companies_scraped = 0
    completed_keys: list[str] = []
    total_companies = len(active)
    t0 = time.time()

    log.info("ATS discovery: %d companies, %d workers", total_companies, workers)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_scrape_company, key, info): (key, info)
            for key, info in active.items()
        }

        for future in as_completed(futures):
            # Check stop event
            if stop_event and stop_event.is_set():
                log.info("ATS discovery: stop requested, cancelling remaining")
                for f in futures:
                    f.cancel()
                break

            key, info = futures[future]
            name = info.get("name", key)
            try:
                jobs = future.result()
                if jobs:
                    pm_jobs = [j for j in jobs if _title_matches(j.get("title", ""))]
                    skipped = len(jobs) - len(pm_jobs)
                    if pm_jobs:
                        new, existing = store_jobs(conn, pm_jobs, site=f"ats:{name}", strategy="ats_api")
                        total_found += len(pm_jobs)
                        total_new += new
                        total_existing += existing
                        log.info("  %s: %d PM roles (%d skipped), %d new, %d existing",
                                 name, len(pm_jobs), skipped, new, existing)
                    else:
                        log.info("  %s: %d jobs, none matched PM filter", name, len(jobs))
                    companies_scraped += 1
                else:
                    companies_scraped += 1
                completed_keys.append(key)
            except Exception as e:
                log.error("  %s: error — %s", name, e)
                errors += 1
                completed_keys.append(key)

            if progress_callback:
                progress_callback(len(completed_keys), total_companies, name)

    elapsed = time.time() - t0
    log.info(
        "ATS discovery complete: %d companies, %d jobs found, %d new, %d existing, %d errors (%.1fs)",
        companies_scraped, total_found, total_new, total_existing, errors, elapsed,
    )

    return {
        "companies_scraped": companies_scraped,
        "total_found": total_found,
        "new": total_new,
        "existing": total_existing,
        "errors": errors,
        "elapsed": elapsed,
        "completed_keys": completed_keys,
    }
