from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from jobspy import scrape_jobs


STATE_PATH = Path(os.getenv("JOBSPY_STATE_PATH", ".jobspy_seen_jobs.json"))
DISCORD_LIMIT = 2000
DISCORD_EMBEDS_PER_MESSAGE = 10
EMBED_COLOR = 0x3B82F6
DEFAULT_TZ = "America/Los_Angeles"
DEFAULT_SEARCH_TERM = '"software intern" OR "software engineering intern" OR "SWE intern"'
DEFAULT_LOCATION = "United States"
DEFAULT_HOURS_OLD = 4
SITE_LABELS = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "zip_recruiter": "ZipRecruiter",
    "google": "Google",
    "glassdoor": "Glassdoor",
}
# Leave a buffer so the cache-save step can run before GitHub kills the job.
DEFAULT_RUN_SECONDS = 6 * 60 * 60 - 10 * 60
ERROR_BACKOFF_SECONDS = 15


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


def local_now() -> datetime:
    return datetime.now(ZoneInfo(os.getenv("JOBSPY_TIMEZONE", DEFAULT_TZ)))


def parse_posted_at(value: Any) -> tuple[datetime | None, bool]:
    """Return (posted_at, has_time). Date-only values are treated as midnight with no time."""
    if value is None or (not isinstance(value, (datetime, date, str)) and pd.isna(value)):
        return None, False

    has_time = False
    posted: datetime | None = None

    if isinstance(value, datetime):
        posted = value
        has_time = not (
            value.hour == 0 and value.minute == 0 and value.second == 0 and value.microsecond == 0
        )
    elif isinstance(value, date):
        posted = datetime.combine(value, dt_time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, False
        iso = text.replace("Z", "+00:00")
        try:
            posted = datetime.fromisoformat(iso)
            has_time = "T" in iso or " " in text
            if posted.hour == 0 and posted.minute == 0 and posted.second == 0:
                has_time = bool(re.search(r"T\d{2}:\d{2}", iso))
        except ValueError:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    posted = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue

    if posted is None:
        return None, False
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=local_now().tzinfo)
    return posted, has_time


def format_posted_ago(value: Any, now: datetime) -> str:
    posted, has_time = parse_posted_at(value)
    if posted is None:
        return "Unknown"

    posted = posted.astimezone(now.tzinfo)
    if not has_time:
        days = (now.date() - posted.date()).days
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        return f"{days} days ago"

    seconds = max(0, int((now - posted).total_seconds()))
    if seconds < 60:
        return f"{seconds} sec ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


def source_label(job: dict[str, Any]) -> str:
    site = field(job, "site")
    return SITE_LABELS.get(site.lower(), site or "Unknown")


def job_embed(job: dict[str, Any], now: datetime) -> dict[str, Any]:
    title = field(job, "title") or "Untitled role"
    company = field(job, "company") or "Unknown company"
    location = field(job, "location") or "Unknown"
    url = field(job, "job_url")
    embed: dict[str, Any] = {
        "title": f"{title} — {company}"[:256],
        "description": location[:4096],
        "color": EMBED_COLOR,
        "fields": [
            {
                "name": "Posted",
                "value": format_posted_ago(job.get("date_posted"), now),
                "inline": True,
            },
            {
                "name": "Source",
                "value": source_label(job)[:1024],
                "inline": True,
            },
        ],
        "timestamp": now.isoformat(),
    }
    if url:
        embed["url"] = url
    return embed


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


def post_discord_text(webhook_url: str, content: str) -> None:
    response = requests.post(
        webhook_url, json={"content": content[:DISCORD_LIMIT]}, timeout=30
    )
    response.raise_for_status()


def post_discord(webhook_url: str, jobs: list[dict[str, Any]]) -> None:
    now = local_now()
    embeds = [job_embed(job, now) for job in jobs]
    for index in range(0, len(embeds), DISCORD_EMBEDS_PER_MESSAGE):
        chunk = embeds[index : index + DISCORD_EMBEDS_PER_MESSAGE]
        response = requests.post(webhook_url, json={"embeds": chunk}, timeout=30)
        response.raise_for_status()


def scrape_matching_jobs(
    sites: list[str],
    search_term: str,
    google_search_term: str,
    location: str,
    hours_old: int,
) -> list[dict[str, Any]]:
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
    if jobs.empty:
        return []
    return [job for job in jobs.to_dict("records") if is_software_intern_role(job)]


def process_poll(
    webhook_url: str,
    job_records: list[dict[str, Any]],
    seen: set[str],
    notify_jobs: bool,
) -> set[str]:
    current_ids = {job_id(job) for job in job_records}
    new_jobs = [job for job in job_records if job_id(job) not in seen]

    if new_jobs and notify_jobs:
        post_discord(webhook_url, new_jobs)
        print(f"Posted {len(new_jobs)} new jobs to Discord")
    else:
        print(f"Found {len(new_jobs)} new jobs; no notification sent")

    updated = seen | current_ids
    save_seen(updated)
    return updated


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
    run_seconds = env_int("JOBSPY_RUN_SECONDS", DEFAULT_RUN_SECONDS)
    interval = max(0, env_int("JOBSPY_POLL_INTERVAL_SECONDS", 0))
    notify_on_first_run = env_bool("JOBSPY_NOTIFY_ON_FIRST_RUN")

    seen = load_seen()
    first_pass = not seen
    deadline = time.monotonic() + run_seconds
    pass_num = 0

    hours = run_seconds / 3600
    post_discord_text(
        webhook_url,
        f"JobSpy is active and polling for new software intern roles for the next {hours:.1f} hours.",
    )
    print(f"Sent startup notification; looping for {run_seconds}s (interval={interval}s)")

    while time.monotonic() < deadline:
        pass_num += 1
        print(f"Poll {pass_num} starting")
        try:
            job_records = scrape_matching_jobs(
                sites, search_term, google_search_term, location, hours_old
            )
        except Exception as exc:
            print(f"Poll {pass_num} failed: {exc}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(interval, ERROR_BACKOFF_SECONDS), remaining))
            continue

        notify_jobs = notify_on_first_run or not first_pass
        seen = process_poll(webhook_url, job_records, seen, notify_jobs)
        first_pass = False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if interval:
            time.sleep(min(interval, remaining))

    print(f"Finished after {pass_num} poll(s)")


if __name__ == "__main__":
    main()
