import threading
from urllib.parse import urlsplit

import pytest

from crawler.polite_fetcher import (
    FLOOR_INTERVAL_SECONDS,
    BackoffError,
    PoliteFetcher,
    RobotsDisallowedError,
    RobotsUnavailableError,
)

# Same shape as www.scu.edu's file: a broad Allow comes before the Disallow lines.
ROBOTS = "User-agent: *\nAllow: /\nDisallow: /secret\n"


class FakeResponse:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class Harness:
    def __init__(self, routes, **options):
        self.routes = routes
        self.requests = []
        self.clock = 1000.0
        self.fetcher = PoliteFetcher(http_get=self.get, sleep=self.sleep, now=lambda: self.clock, **options)

    def sleep(self, seconds):
        self.clock += seconds

    def get(self, url, headers, allow_redirects, timeout):
        self.requests.append({"url": url, "at": self.clock, "headers": headers})
        return self.routes.get(urlsplit(url).path, FakeResponse(404))

    def paths(self):
        return [urlsplit(r["url"]).path for r in self.requests]


URL = "https://www.scu.edu"


def test_reads_robots_first_and_identifies_itself():
    h = Harness({"/robots.txt": FakeResponse(text=ROBOTS), "/a": FakeResponse(text="A")})
    h.fetcher.fetch_page(f"{URL}/a")
    assert h.paths() == ["/robots.txt", "/a"]
    for r in h.requests:
        assert r["headers"]["User-Agent"].startswith("SCUStudyHub-BulletinCrawler/1.0 (")


def test_disallow_wins_over_a_leading_allow():
    h = Harness({"/robots.txt": FakeResponse(text=ROBOTS), "/secret/x": FakeResponse(text="no")})
    with pytest.raises(RobotsDisallowedError):
        h.fetcher.fetch_page(f"{URL}/secret/x")
    assert "/secret/x" not in h.paths()


def test_spaces_requests_even_when_concurrent():
    routes = {"/robots.txt": FakeResponse(text=ROBOTS), "/a": FakeResponse(), "/b": FakeResponse(), "/c": FakeResponse()}
    h = Harness(routes)
    threads = [threading.Thread(target=h.fetcher.fetch_page, args=(f"{URL}{p}",)) for p in ("/a", "/b", "/c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(h.requests) == 4
    assert h.paths().count("/robots.txt") == 1
    gaps = [b["at"] - a["at"] for a, b in zip(h.requests, h.requests[1:])]
    assert all(gap >= 20 for gap in gaps)


def test_interval_cannot_go_below_floor():
    assert Harness({}, min_interval=0.001).fetcher.interval == FLOOR_INTERVAL_SECONDS


def test_larger_crawl_delay_is_honored():
    h = Harness({"/robots.txt": FakeResponse(text="User-agent: *\nCrawl-delay: 45\n"), "/a": FakeResponse(), "/b": FakeResponse()})
    h.fetcher.fetch_page(f"{URL}/a")
    h.fetcher.fetch_page(f"{URL}/b")
    assert h.requests[2]["at"] - h.requests[1]["at"] == 45


def test_refuses_to_crawl_when_robots_is_unreadable():
    h = Harness({"/robots.txt": FakeResponse(500)})
    with pytest.raises(RobotsUnavailableError):
        h.fetcher.fetch_page(f"{URL}/a")
    assert len(h.requests) == 1


def test_missing_robots_means_no_restrictions():
    h = Harness({"/robots.txt": FakeResponse(404), "/a": FakeResponse(text="A")})
    assert h.fetcher.fetch_page(f"{URL}/a").html == "A"


@pytest.mark.parametrize("status", [429, 503])
def test_backoff_statuses_stop_the_run(status):
    h = Harness({"/robots.txt": FakeResponse(text=ROBOTS), "/a": FakeResponse(status, headers={"Retry-After": "120"})})
    with pytest.raises(BackoffError):
        h.fetcher.fetch_page(f"{URL}/a")


def test_redirects_are_rechecked():
    blocked = Harness({"/robots.txt": FakeResponse(text=ROBOTS), "/a": FakeResponse(302, headers={"Location": "/secret/y"})})
    with pytest.raises(RobotsDisallowedError):
        blocked.fetcher.fetch_page(f"{URL}/a")
    assert "/secret/y" not in blocked.paths()

    offsite = Harness({"/robots.txt": FakeResponse(text=ROBOTS), "/a": FakeResponse(302, headers={"Location": "https://example.com/"})})
    with pytest.raises(RobotsDisallowedError):
        offsite.fetcher.fetch_page(f"{URL}/a")
    assert not any("example.com" in r["url"] for r in offsite.requests)


def test_sends_cached_validators_and_reports_304():
    h = Harness({"/robots.txt": FakeResponse(text=ROBOTS), "/a": FakeResponse(304)})
    assert h.fetcher.fetch_page(f"{URL}/a", etag='"abc"').status == 304
    assert h.requests[1]["headers"]["If-None-Match"] == '"abc"'
