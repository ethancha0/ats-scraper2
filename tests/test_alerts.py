import pandas as pd

import poll_discord
from poll_discord import (
    ConsecutiveFailureAlerts,
    SeenState,
    SiteConfig,
    SitePollResult,
    health_alerts,
    scrape_site,
)


def test_fires_once_after_threshold_consecutive_failures():
    alerts = ConsecutiveFailureAlerts(threshold=3)
    assert [alerts.record("k", True) for _ in range(5)] == [
        False,
        False,
        True,
        False,
        False,
    ]


def test_success_resets_streak():
    alerts = ConsecutiveFailureAlerts(threshold=3)
    outcomes = [True, True, False, True, True, True]
    assert [alerts.record("k", failed) for failed in outcomes] == [
        False,
        False,
        False,
        False,
        False,
        True,
    ]


def test_linkedin_empty_and_site_error_alerts():
    alerts = ConsecutiveFailureAlerts(threshold=3)
    empty_linkedin = SitePollResult(site="linkedin", statuses=[200])
    healthy_indeed = SitePollResult(site="indeed", raw=[{"title": "x"}])
    broken_indeed = SitePollResult(site="indeed", errors=["'q': HTTP 403"])

    assert health_alerts([empty_linkedin, healthy_indeed], alerts) == []
    assert health_alerts([empty_linkedin, broken_indeed], alerts) == []
    messages = health_alerts([empty_linkedin, broken_indeed], alerts)
    assert len(messages) == 1 and "LinkedIn returned 0 job cards" in messages[0]
    messages = health_alerts([empty_linkedin, broken_indeed], alerts)
    assert len(messages) == 1 and "indeed errored on 3 polls" in messages[0]
    # each condition alerts at most once per run
    assert health_alerts([empty_linkedin, broken_indeed], alerts) == []


def test_scrape_site_tracks_raw_linkedin_ids_and_filters(monkeypatch):
    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        df = pd.DataFrame(
            [
                {"id": "li-1", "site": "linkedin", "title": "Software Engineer Intern",
                 "job_url": "https://www.linkedin.com/jobs/view/1"},
                {"id": "li-2", "site": "linkedin", "title": "Marketing Intern",
                 "job_url": "https://www.linkedin.com/jobs/view/2"},
            ]
        )
        df.attrs["site_stats"] = {
            "linkedin": {"pages": 1, "statuses": [200], "stop_reason": "known_ids"}
        }
        return df

    monkeypatch.setattr(poll_discord, "scrape_jobs", fake_scrape_jobs)
    state = SeenState(linkedin_cards={"99": "2026-10-21T00:00:00+00:00"})
    config = SiteConfig(
        site="linkedin", queries=["software"], results_wanted=1000, hours_old=24,
        job_type="internship", sort_by="date",
    )
    result = scrape_site(config, state, "United States", "")

    assert calls[0]["linkedin_known_job_ids"] == {"99"}
    assert calls[0]["site_name"] == ["linkedin"]
    assert calls[0]["sort_by"] == "date"
    # rejected cards are still remembered so paging can stop on them
    assert set(state.linkedin_cards) == {"1", "2", "99"}
    assert len(result.raw) == 2
    assert [job["title"] for job in result.matched] == ["Software Engineer Intern"]
    assert len(result.new) == 1
    assert "pages=1" in result.summary() and "stop=known_ids" in result.summary()


def test_site_config_precedence(monkeypatch):
    monkeypatch.setenv("JOBSPY_RESULTS_WANTED", "20")
    monkeypatch.setenv("JOBSPY_HOURS_OLD", "6")
    monkeypatch.setenv("JOBSPY_LINKEDIN_QUERIES", "software; developer ;")
    monkeypatch.setenv("JOBSPY_INDEED_SORT_BY", "none")
    linkedin = poll_discord.site_config("linkedin")
    indeed = poll_discord.site_config("indeed")
    zip_recruiter = poll_discord.site_config("zip_recruiter")

    assert linkedin.queries == ["software", "developer"]
    assert linkedin.job_type == "internship" and linkedin.hours_old == 6
    assert indeed.results_wanted == 100 and indeed.sort_by is None
    assert zip_recruiter.results_wanted == 20
