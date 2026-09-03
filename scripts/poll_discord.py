from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from jobspy import scrape_jobs


STATE_PATH = Path(os.getenv("JOBSPY_STATE_PATH", ".jobspy_seen_jobs.json"))
DISCORD_LIMIT = 2000
DEFAULT_SEARCH_TERM = '"software intern" OR "software engineering intern" OR "SWE intern"'
DEFAULT_LOCATION = "United States"
DEFAULT_HOURS_OLD = 4


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def env_list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def load_seen() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return set()
    return set(data.get("seen", []))


def save_seen(seen: set[str]) -> None:
    STATE_PATH.write_text(json.dumps({"seen": sorted(seen)[-5000:]}, indent=2))


def job_id(job: dict[str, Any]) -> str:
    stable_value = job.get("job_url") or "|".join(
        str(job.get(key) or "") for key in ("site", "title", "company", "location")
    )
    return hashlib.sha256(stable_value.encode("utf-8")).hexdigest()


def field(job: dict[str, Any], name: str) -> str:
    value = job.get(name)
    if pd.isna(value):
        return ""
    return str(value).strip()


def format_job(job: dict[str, Any]) -> str:
    title = field(job, "title") or "Untitled role"
    company = field(job, "company") or "Unknown company"
    location = field(job, "location") or "Unknown"
    site = field(job, "site") or "Unknown"
    date_posted = field(job, "date_posted") or "Unknown"
    url = field(job, "job_url")

    lines = [
        f"**{title}**",
        f"Company: {company}",
        f"Source: {site}",
        f"Posted: {date_posted}",
        f"Location: {location}",
    ]

    if url:
        lines.append(url)
    return "\n".join(lines)


def is_software_intern_role(job: dict[str, Any]) -> bool:
    title = field(job, "title").lower()
    if not title:
        return False

    has_intern = any(term in title for term in ("intern", "internship"))
    has_software = any(
        term in title
        for term in (
            "software",
            "swe",
            "developer",
            "programmer",
        )
    )
    excluded_terms = (
        "hardware",
        "mechanical",
        "electrical",
        "civil",
        "marketing",
        "sales",
        "finance",
        "accounting",
        "recruit",
        "human resources",
    )

    return has_intern and has_software and not any(
        term in title for term in excluded_terms
    )


def post_discord(webhook_url: str, jobs: list[dict[str, Any]]) -> None:
    header = f"Found {len(jobs)} new job{'s' if len(jobs) != 1 else ''}:"
    chunks: list[str] = []
    current = header

    for job in jobs:
        entry = "\n\n" + format_job(job)
        if len(current) + len(entry) > DISCORD_LIMIT:
            chunks.append(current)
            current = header + entry
        else:
            current += entry

    if current:
        chunks.append(current)

    for content in chunks:
        response = requests.post(webhook_url, json={"content": content}, timeout=30)
        response.raise_for_status()


def main() -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise SystemExit("DISCORD_WEBHOOK_URL is required")

    sites = env_list("JOBSPY_SITES", ["indeed", "linkedin", "zip_recruiter", "google"])
    search_term = os.getenv("JOBSPY_SEARCH_TERM", DEFAULT_SEARCH_TERM)
    location = os.getenv("JOBSPY_LOCATION", DEFAULT_LOCATION)
    hours_old = env_int("JOBSPY_HOURS_OLD", DEFAULT_HOURS_OLD)
    google_search_term = os.getenv(
        "JOBSPY_GOOGLE_SEARCH_TERM",
        f"software intern jobs in {location} posted in the past {hours_old} hours",
    )

    jobs = scrape_jobs(
        site_name=sites,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        results_wanted=env_int("JOBSPY_RESULTS_WANTED", 20),
        hours_old=hours_old,
        country_indeed=os.getenv("JOBSPY_COUNTRY_INDEED", "USA"),
        linkedin_fetch_description=env_bool("JOBSPY_LINKEDIN_FETCH_DESCRIPTION"),
        verbose=env_int("JOBSPY_VERBOSE", 1),
    )
    job_records = [job for job in jobs.to_dict("records") if is_software_intern_role(job)]

    seen = load_seen()
    current_ids = {job_id(job) for job in job_records}
    first_run = not seen
    new_jobs = [
        job
        for job in job_records
        if job_id(job) not in seen
    ]

    notify_on_first_run = env_bool("JOBSPY_NOTIFY_ON_FIRST_RUN")
    if new_jobs and (notify_on_first_run or not first_run):
        post_discord(webhook_url, new_jobs)
        print(f"Posted {len(new_jobs)} new jobs to Discord")
    else:
        print(f"Found {len(new_jobs)} new jobs; no notification sent")

    save_seen(seen | current_ids)


if __name__ == "__main__":
    main()
