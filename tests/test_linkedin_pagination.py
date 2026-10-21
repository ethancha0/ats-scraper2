import pytest

import jobspy.linkedin as linkedin_module
from jobspy.linkedin import LinkedIn
from jobspy.model import ScraperInput, Site

CARD = """
<div class="base-search-card">
  <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/software-engineer-intern-{id}?trk=x"></a>
  <span class="sr-only">Software Engineer Intern {id}</span>
  <h4 class="base-search-card__subtitle"><a href="https://www.linkedin.com/company/acme">Acme</a></h4>
  <div class="base-search-card__metadata">
    <span class="job-search-card__location">Seattle, WA</span>
    <time class="job-search-card__listdate" datetime="2026-10-21"></time>
  </div>
</div>
"""


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    """Serves pages of 10 cards keyed by the `start` param."""

    def __init__(self, pages, statuses=None):
        self.pages = pages
        self.statuses = statuses or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        page = params["start"] // 10
        ids = self.pages[page] if page < len(self.pages) else []
        html = "".join(CARD.format(id=job_id) for job_id in ids)
        return FakeResponse(html, self.statuses.get(page, 200))


def page_ids(page):
    return [str(4_000_000_000 + page * 10 + i) for i in range(10)]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(linkedin_module.time, "sleep", lambda _: None)


def run(session, **kwargs):
    scraper = LinkedIn()
    scraper.session = session
    scraper_input = ScraperInput(
        site_type=[Site.LINKEDIN],
        search_term="software",
        results_wanted=kwargs.pop("results_wanted", 1000),
        **kwargs,
    )
    return scraper.scrape(scraper_input)


def test_stops_after_first_page_of_only_known_ids():
    pages = [page_ids(p) for p in range(5)]
    known = set(pages[0]) | set(pages[1]) | set(pages[2])
    # page 0 has one new id, page 1 is entirely known -> stop before page 2
    known.discard(pages[0][0])
    session = FakeSession(pages)
    response = run(session, linkedin_known_job_ids=known, sort_by="date")

    assert [call["start"] for call in session.calls] == [0, 10]
    assert all(call["sortBy"] == "DD" for call in session.calls)
    assert len(response.jobs) == 20
    assert response.stats["stop_reason"] == "known_ids"
    assert response.stats["pages"] == 2
    assert response.stats["statuses"] == [200, 200]


def test_page_cap_limits_requests():
    pages = [page_ids(p) for p in range(20)]
    session = FakeSession(pages)
    response = run(session, linkedin_known_job_ids=set(), linkedin_max_pages=3)

    assert len(session.calls) == 3
    assert len(response.jobs) == 30
    assert response.stats["stop_reason"] == "max_pages"


def test_without_known_ids_keeps_legacy_behavior():
    pages = [page_ids(p) for p in range(3)]
    session = FakeSession(pages)
    response = run(session, results_wanted=20)

    assert len(session.calls) == 2
    assert "sortBy" not in session.calls[0]
    assert len(response.jobs) == 20


def test_empty_first_page_warns_and_reports(caplog):
    session = FakeSession([[]])
    with caplog.at_level("WARNING", logger="JobSpy:LinkedIn"):
        response = run(session, linkedin_known_job_ids=set())
    assert response.jobs == []
    assert response.stats["stop_reason"] == "no_cards"
    assert "page 0 returned 0 cards" in caplog.text


def test_http_error_is_reported_in_stats(caplog):
    session = FakeSession([page_ids(0)], statuses={0: 429})
    with caplog.at_level("WARNING", logger="JobSpy:LinkedIn"):
        response = run(session)
    assert response.jobs == []
    assert response.stats["error"] == "HTTP 429"
    assert response.stats["statuses"] == [429]
    assert "returned status 429" in caplog.text
