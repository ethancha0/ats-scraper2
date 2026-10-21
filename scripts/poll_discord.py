from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, time as dt_time, timedelta, timezone
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
DEFAULT_HOURS_OLD = 24
DEFAULT_SITES = ["indeed", "linkedin"]
DEFAULT_RESULTS_WANTED = 20
DEFAULT_SEEN_TTL_DAYS = 30
DEFAULT_LINKEDIN_CARD_TTL_HOURS = 48
DEFAULT_LINKEDIN_MAX_PAGES = 20
DEFAULT_LINKEDIN_SWEEP_MAX_PAGES = 100
DEFAULT_LINKEDIN_SWEEP_EVERY_POLLS = 36
DEFAULT_ALERT_AFTER_POLLS = 3
# Seconds to wait between consecutive LinkedIn searches (same band as its paging).
LINKEDIN_QUERY_DELAY = (3, 7)
STATE_VERSION = 2

# Built-in per-site defaults. Each key can be overridden with an env var named
# JOBSPY_<SITE>_<KEY> (e.g. JOBSPY_LINKEDIN_QUERIES, JOBSPY_INDEED_SORT_BY).
# Queries are ";"-separated. Sites without an entry fall back to the global
# JOBSPY_SEARCH_TERM / JOBSPY_RESULTS_WANTED settings.
SITE_DEFAULTS: dict[str, dict[str, Any]] = {
    "linkedin": {
        # LinkedIn's guest search currently ignores f_JT (job type) and sortBy and
        # returns results by relevance, but it does honor the f_TPR time window.
        # So the keyword itself must say "intern", and each poll reads the whole
        # short window (hours_old) instead of relying on newest-first order;
        # a periodic sweep re-reads the full JOBSPY_HOURS_OLD window.
        "queries": ["software intern"],
        "hours_old": 2,
        # Harmless if ignored; takes effect if LinkedIn starts honoring them.
        "job_type": "internship",
        "sort_by": "date",
        # Paging is bounded by JOBSPY_LINKEDIN_(SWEEP_)MAX_PAGES instead.
        "results_wanted": 1000,
    },
    "indeed": {
        # Indeed can't combine hours_old with a job-type filter, so the query
        # itself has to say "intern".
        "queries": [
            "software intern",
            "engineer intern",
            "developer intern",
            "software co-op",
        ],
        "sort_by": "date",
        # One GraphQL page returns 100 results; asking for fewer just discards them.
        "results_wanted": 100,
    },
}

# Title filter term lists (regex fragments, matched case-insensitively on word
# boundaries). A title must hit an intern term and a role term and no exclusion.
INTERN_TERMS = [r"interns?", r"internships?", r"co[- ]?ops?"]
ROLE_TERMS = [
    r"software",
    r"swe",
    r"sde",
    r"developers?",
    r"programmers?",
    r"full[- ]?stack",
    r"back[- ]?end",
    r"front[- ]?end",
    r"mobile",
    r"ios",
    r"android",
    r"web",
    r"devops",
    r"sre",
    r"ml",
    r"ai",
    r"machine learning",
    r"artificial intelligence",
    r"computer science",
    r"forward deployed",
    r"platform",
    r"infrastructure",
    r"cloud",
    r"embedded",
    r"data engineer(?:ing)?",
]
EXCLUDED_TERMS = [
    r"sales",
    r"marketing",
    r"finance",
    r"accounting",
    r"recruit\w*",
    r"human resources",
    r"mechanical",
    r"civil",
]
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


def env_str(name: str, default: str | None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_queries(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return [item.strip() for item in value.split(";") if item.strip()]


@dataclass
class SeenState:
    # sha256(job_url) of jobs already posted/considered -> first-seen ISO timestamp
    jobs: dict[str, str] = dc_field(default_factory=dict)
    # raw LinkedIn card ids (incl. ones the title filter rejected) -> last-seen ISO
    # timestamp; used to stop LinkedIn paging once a page holds only known ids
    linkedin_cards: dict[str, str] = dc_field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_state(path: Path = STATE_PATH, now: datetime | None = None) -> SeenState:
    """Load seen-state, accepting the legacy {"seen": [hash, ...]} format."""
    if not path.exists():
        return SeenState()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return SeenState()
    if not isinstance(data, dict):
        return SeenState()

    stamp = (now or utc_now()).isoformat()
    seen = data.get("seen", {})
    if isinstance(seen, list):
        jobs = {str(item): stamp for item in seen}
    elif isinstance(seen, dict):
        jobs = {str(key): str(value) for key, value in seen.items()}
    else:
        jobs = {}
    cards = data.get("linkedin_cards", {})
    if not isinstance(cards, dict):
        cards = {}
    return SeenState(
        jobs=jobs, linkedin_cards={str(k): str(v) for k, v in cards.items()}
    )


def _prune(entries: dict[str, str], cutoff: datetime) -> dict[str, str]:
    kept = {}
    for key, stamp in entries.items():
        try:
            seen_at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        if seen_at >= cutoff:
            kept[key] = stamp
    return kept


def prune_state(
    state: SeenState,
    now: datetime | None = None,
    seen_ttl: timedelta = timedelta(days=DEFAULT_SEEN_TTL_DAYS),
    card_ttl: timedelta = timedelta(hours=DEFAULT_LINKEDIN_CARD_TTL_HOURS),
) -> SeenState:
    now = now or utc_now()
    return SeenState(
        jobs=_prune(state.jobs, now - seen_ttl),
        linkedin_cards=_prune(state.linkedin_cards, now - card_ttl),
    )


def save_state(state: SeenState, path: Path = STATE_PATH) -> None:
    payload = {
        "version": STATE_VERSION,
        "seen": state.jobs,
        "linkedin_cards": state.linkedin_cards,
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp_path.replace(path)


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


def _word_regex(terms: list[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)


INTERN_RE = _word_regex(INTERN_TERMS)
ROLE_RE = _word_regex(ROLE_TERMS)
EXCLUDED_RE = _word_regex(EXCLUDED_TERMS)


def is_software_intern_title(title: str) -> bool:
    return bool(
        title
        and INTERN_RE.search(title)
        and ROLE_RE.search(title)
        and not EXCLUDED_RE.search(title)
    )


def is_software_intern_role(job: dict[str, Any]) -> bool:
    return is_software_intern_title(field(job, "title"))


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


@dataclass
class SiteConfig:
    site: str
    queries: list[str]
    results_wanted: int
    hours_old: int
    job_type: str | None = None
    sort_by: str | None = None
    # LinkedIn only
    max_pages: int | None = None
    sweep_hours_old: int | None = None
    sweep_max_pages: int | None = None
    sweep_every_polls: int = 0
    stop_at_known: bool = False

    def is_sweep(self, poll_num: int) -> bool:
        """Sweep on the first poll of a run and every sweep_every_polls after."""
        return self.sweep_every_polls > 0 and (poll_num - 1) % self.sweep_every_polls == 0


def _optional(value: str | None) -> str | None:
    """Treat "none"/"off" as an explicit opt-out of a built-in default."""
    if value is None or value.lower() in {"none", "off", "false", ""}:
        return None
    return value


def site_config(site: str) -> SiteConfig:
    """Resolve settings: JOBSPY_<SITE>_<KEY> > built-in site default > global env."""
    prefix = f"JOBSPY_{site.upper()}_"
    defaults = SITE_DEFAULTS.get(site, {})
    global_query = os.getenv("JOBSPY_SEARCH_TERM", DEFAULT_SEARCH_TERM)
    global_hours_old = env_int("JOBSPY_HOURS_OLD", DEFAULT_HOURS_OLD)
    linkedin: dict[str, Any] = {}
    if site == "linkedin":
        linkedin = {
            "max_pages": env_int(prefix + "MAX_PAGES", DEFAULT_LINKEDIN_MAX_PAGES),
            "sweep_hours_old": global_hours_old,
            "sweep_max_pages": env_int(
                prefix + "SWEEP_MAX_PAGES", DEFAULT_LINKEDIN_SWEEP_MAX_PAGES
            ),
            "sweep_every_polls": env_int(
                prefix + "SWEEP_EVERY_POLLS", DEFAULT_LINKEDIN_SWEEP_EVERY_POLLS
            ),
            "stop_at_known": env_bool(prefix + "STOP_AT_KNOWN"),
        }
    return SiteConfig(
        site=site,
        queries=env_queries(
            prefix + "QUERIES", defaults.get("queries", [global_query])
        ),
        results_wanted=env_int(
            prefix + "RESULTS_WANTED",
            defaults.get(
                "results_wanted",
                env_int("JOBSPY_RESULTS_WANTED", DEFAULT_RESULTS_WANTED),
            ),
        ),
        hours_old=env_int(
            prefix + "HOURS_OLD", defaults.get("hours_old", global_hours_old)
        ),
        job_type=_optional(env_str(prefix + "JOB_TYPE", defaults.get("job_type"))),
        sort_by=_optional(env_str(prefix + "SORT_BY", defaults.get("sort_by"))),
        **linkedin,
    )


@dataclass
class SitePollResult:
    site: str
    sweep: bool = False
    raw: list[dict[str, Any]] = dc_field(default_factory=list)
    matched: list[dict[str, Any]] = dc_field(default_factory=list)
    new: list[dict[str, Any]] = dc_field(default_factory=list)
    pages: int = 0
    statuses: list[int] = dc_field(default_factory=list)
    stop_reasons: list[str] = dc_field(default_factory=list)
    errors: list[str] = dc_field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"[{self.site}{' sweep' if self.sweep else ''}] "
            f"raw={len(self.raw)} matched={len(self.matched)} "
            f"new={len(self.new)} pages={self.pages} statuses={self.statuses}"
        )
        if self.stop_reasons:
            line += f" stop={','.join(self.stop_reasons)}"
        if self.errors:
            line += f" errors={self.errors}"
        return line


def linkedin_card_id(job: dict[str, Any]) -> str | None:
    value = field(job, "id")
    if value.startswith("li-"):
        return value[3:]
    return None


def scrape_site(
    config: SiteConfig,
    state: SeenState,
    location: str,
    google_search_term: str,
    sweep: bool = False,
) -> SitePollResult:
    result = SitePollResult(site=config.site, sweep=sweep)
    is_linkedin = config.site == "linkedin"
    hours_old = config.sweep_hours_old if sweep else config.hours_old
    by_id: dict[str, dict[str, Any]] = {}

    for index, query in enumerate(config.queries):
        if is_linkedin and index:
            time.sleep(random.uniform(*LINKEDIN_QUERY_DELAY))
        kwargs: dict[str, Any] = {}
        if is_linkedin:
            kwargs["linkedin_max_pages"] = (
                config.sweep_max_pages if sweep else config.max_pages
            )
            if config.stop_at_known and not sweep:
                # Only safe while LinkedIn honors newest-first sorting.
                kwargs["linkedin_known_job_ids"] = set(state.linkedin_cards)
        try:
            jobs = scrape_jobs(
                site_name=[config.site],
                search_term=query,
                google_search_term=google_search_term,
                location=location,
                job_type=config.job_type,
                results_wanted=config.results_wanted,
                hours_old=hours_old,
                sort_by=config.sort_by,
                country_indeed=os.getenv("JOBSPY_COUNTRY_INDEED", "USA"),
                linkedin_fetch_description=env_bool("JOBSPY_LINKEDIN_FETCH_DESCRIPTION"),
                verbose=env_int("JOBSPY_VERBOSE", 1),
                **kwargs,
            )
        except Exception as exc:
            result.errors.append(f"{query!r}: {exc}"[:300])
            continue

        stats = jobs.attrs.get("site_stats", {}).get(config.site) or {}
        result.pages += stats.get("pages", 0)
        result.statuses.extend(stats.get("statuses", []))
        if stats.get("stop_reason"):
            result.stop_reasons.append(stats["stop_reason"])
        if stats.get("error"):
            result.errors.append(f"{query!r}: {stats['error']}")

        records = [] if jobs.empty else jobs.to_dict("records")
        if is_linkedin:
            stamp = utc_now().isoformat()
            for job in records:
                card_id = linkedin_card_id(job)
                if card_id:
                    state.linkedin_cards[card_id] = stamp
        for job in records:
            by_id.setdefault(job_id(job), job)

    result.raw = list(by_id.values())
    result.matched = [job for job in result.raw if is_software_intern_role(job)]
    result.new = [job for job in result.matched if job_id(job) not in state.jobs]
    return result


class ConsecutiveFailureAlerts:
    """Fires once per key (per run) after `threshold` consecutive failed polls."""

    def __init__(self, threshold: int = DEFAULT_ALERT_AFTER_POLLS) -> None:
        self.threshold = threshold
        self.streaks: dict[str, int] = {}
        self.alerted: set[str] = set()

    def record(self, key: str, failed: bool) -> bool:
        if not failed:
            self.streaks[key] = 0
            return False
        self.streaks[key] = self.streaks.get(key, 0) + 1
        if self.streaks[key] >= self.threshold and key not in self.alerted:
            self.alerted.add(key)
            return True
        return False


def health_alerts(
    results: list[SitePollResult], alerts: ConsecutiveFailureAlerts
) -> list[str]:
    messages = []
    n = alerts.threshold
    for result in results:
        if alerts.record(f"{result.site}:error", bool(result.errors)):
            messages.append(
                f"JobSpy warning: {result.site} errored on {n} polls in a row "
                f"(latest: {'; '.join(result.errors)[:500]})"
            )
        if result.site == "linkedin" and alerts.record(
            "linkedin:empty", not result.raw
        ):
            messages.append(
                f"JobSpy warning: LinkedIn returned 0 job cards on {n} polls in a row "
                f"(statuses={result.statuses}); it may be throttling this runner."
            )
    return messages


def run_poll(
    webhook_url: str,
    configs: list[SiteConfig],
    state: SeenState,
    location: str,
    google_search_term: str,
    notify_jobs: bool,
    poll_num: int = 1,
) -> list[SitePollResult]:
    results = [
        scrape_site(
            config, state, location, google_search_term, config.is_sweep(poll_num)
        )
        for config in configs
    ]
    for result in results:
        print(result.summary())

    new_jobs = [job for result in results for job in result.new]
    if new_jobs and notify_jobs:
        post_discord(webhook_url, new_jobs)
        print(f"Posted {len(new_jobs)} new jobs to Discord")
    else:
        print(f"Found {len(new_jobs)} new jobs; no notification sent")

    stamp = utc_now().isoformat()
    for result in results:
        for job in result.matched:
            state.jobs.setdefault(job_id(job), stamp)
    return results


def main() -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise SystemExit("DISCORD_WEBHOOK_URL is required")

    sites = env_list("JOBSPY_SITES", DEFAULT_SITES)
    configs = [site_config(site) for site in sites]
    location = os.getenv("JOBSPY_LOCATION", DEFAULT_LOCATION)
    hours_old = env_int("JOBSPY_HOURS_OLD", DEFAULT_HOURS_OLD)
    google_search_term = os.getenv(
        "JOBSPY_GOOGLE_SEARCH_TERM",
        f"software intern jobs in {location} posted in the past {hours_old} hours",
    )
    run_seconds = env_int("JOBSPY_RUN_SECONDS", DEFAULT_RUN_SECONDS)
    interval = max(0, env_int("JOBSPY_POLL_INTERVAL_SECONDS", 0))
    notify_on_first_run = env_bool("JOBSPY_NOTIFY_ON_FIRST_RUN")
    seen_ttl = timedelta(days=env_int("JOBSPY_SEEN_TTL_DAYS", DEFAULT_SEEN_TTL_DAYS))
    card_ttl = timedelta(
        hours=env_int("JOBSPY_LINKEDIN_CARD_TTL_HOURS", DEFAULT_LINKEDIN_CARD_TTL_HOURS)
    )
    alerts = ConsecutiveFailureAlerts(
        env_int("JOBSPY_ALERT_AFTER_POLLS", DEFAULT_ALERT_AFTER_POLLS)
    )

    state = prune_state(load_state(), seen_ttl=seen_ttl, card_ttl=card_ttl)
    first_pass = not state.jobs
    deadline = time.monotonic() + run_seconds
    pass_num = 0

    for config in configs:
        print(f"Site config: {config}")

    hours = run_seconds / 3600
    post_discord_text(
        webhook_url,
        f"JobSpy is active and polling for new software intern roles for the next {hours:.1f} hours.",
    )
    print(f"Sent startup notification; looping for {run_seconds}s (interval={interval}s)")

    while time.monotonic() < deadline:
        pass_num += 1
        print(f"Poll {pass_num} starting")
        notify_jobs = notify_on_first_run or not first_pass
        try:
            results = run_poll(
                webhook_url,
                configs,
                state,
                location,
                google_search_term,
                notify_jobs,
                pass_num,
            )
        except Exception as exc:
            print(f"Poll {pass_num} failed: {exc}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(interval, ERROR_BACKOFF_SECONDS), remaining))
            continue
        finally:
            state = prune_state(state, seen_ttl=seen_ttl, card_ttl=card_ttl)
            save_state(state)

        first_pass = False
        for message in health_alerts(results, alerts):
            print(message)
            try:
                post_discord_text(webhook_url, message)
            except Exception as exc:
                print(f"Failed to send alert: {exc}")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if interval:
            time.sleep(min(interval, remaining))

    print(f"Finished after {pass_num} poll(s)")


if __name__ == "__main__":
    main()
