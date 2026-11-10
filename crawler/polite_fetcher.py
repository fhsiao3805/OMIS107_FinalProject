"""HTTP client that enforces the crawl etiquette in one place.

Every request (robots.txt and redirects included) goes through the same pacing
gate, and every URL is checked against robots.txt before it is sent.
"""
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests
from protego import Protego

CRAWLER_NAME = "SCUStudyHub-BulletinCrawler"
CRAWLER_VERSION = "1.0"

# No configuration may make the crawler faster than this (4 requests/minute).
FLOOR_INTERVAL_SECONDS = 15.0
DEFAULT_INTERVAL_SECONDS = 20.0

MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 30


def build_user_agent(contact=None):
    contact = contact if contact is not None else os.environ.get("CRAWLER_CONTACT")
    details = [
        "course catalog indexer for a student tutoring platform",
        "honors robots.txt",
        "max 4 requests/min",
    ]
    if contact:
        details.append(contact)
    return f"{CRAWLER_NAME}/{CRAWLER_VERSION} ({'; '.join(details)})"


class RobotsDisallowedError(Exception):
    pass


class RobotsUnavailableError(Exception):
    pass


class BackoffError(Exception):
    """The site asked us to slow down or is unhealthy: the whole run must stop."""


@dataclass
class PageResult:
    status: int
    url: str
    html: str | None = None
    etag: str | None = None
    last_modified: str | None = None


class PoliteFetcher:
    def __init__(
        self,
        user_agent=None,
        min_interval=DEFAULT_INTERVAL_SECONDS,
        allowed_hosts=("www.scu.edu",),
        http_get=requests.get,
        sleep=time.sleep,
        now=time.monotonic,
        log=lambda message: None,
    ):
        self.user_agent = user_agent or build_user_agent()
        self.interval = max(min_interval, FLOOR_INTERVAL_SECONDS)
        self.allowed_hosts = set(allowed_hosts)
        self.http_get = http_get
        self.sleep = sleep
        self.now = now
        self.log = log
        self.robots = {}
        self.next_request_at = 0.0
        self.request_count = 0
        self._pace_lock = threading.Lock()
        self._robots_lock = threading.Lock()

    def _paced(self, url, headers=None, allow_redirects=True):
        """Sends one request. The lock serializes callers so concurrency can't break pacing."""
        with self._pace_lock:
            wait = self.next_request_at - self.now()
            if wait > 0:
                self.sleep(wait)
            self.next_request_at = self.now() + self.interval
            self.request_count += 1
            return self.http_get(
                url,
                headers={"User-Agent": self.user_agent, **(headers or {})},
                allow_redirects=allow_redirects,
                timeout=TIMEOUT_SECONDS,
            )

    def _robots_for(self, url):
        """Returns the origin's rules, requesting robots.txt only once."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin not in self.robots:
                self.robots[origin] = self._load_robots(origin)
            return self.robots[origin]

    def _load_robots(self, origin):
        robots_url = f"{origin}/robots.txt"
        try:
            response = self._paced(robots_url)
        except requests.RequestException as err:
            raise RobotsUnavailableError(f"Could not read {robots_url}: {err}") from err

        if 200 <= response.status_code < 300:
            body = response.text
        elif 400 <= response.status_code < 500 and response.status_code != 429:
            body = ""  # A missing robots.txt means the site sets no restrictions.
        else:
            # Unknown rules: do not guess, do not crawl.
            raise RobotsUnavailableError(f"{robots_url} returned HTTP {response.status_code}")

        # Protego applies the most specific matching rule, as RFC 9309 requires.
        # urllib.robotparser applies the first match, so a leading "Allow: /"
        # (which www.scu.edu has) would silently disable every Disallow line.
        robots = Protego.parse(body)
        crawl_delay = robots.crawl_delay(CRAWLER_NAME)
        if crawl_delay:
            self.interval = max(self.interval, float(crawl_delay))
        return robots

    def _assert_allowed(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.allowed_hosts:
            raise RobotsDisallowedError(f"{url} is outside the crawl scope")
        if not self._robots_for(url).can_fetch(url, CRAWLER_NAME):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")

    def fetch_page(self, url, etag=None, last_modified=None):
        """Fetches a page, sending any cached validators so unchanged pages return 304."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self._assert_allowed(current)

            headers = {"Accept": "text/html"}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

            response = self._paced(current, headers=headers, allow_redirects=False)
            status = response.status_code
            self.log(f"{status} {current}")

            if status == 304:
                return PageResult(304, current)
            if 300 <= status < 400:
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError(f"Redirect without Location from {current}")
                current = urljoin(current, location)
                continue
            if status in (429, 503):
                retry_after = response.headers.get("Retry-After", "none")
                raise BackoffError(f"HTTP {status} from {current} (Retry-After: {retry_after})")
            if not 200 <= status < 300:
                return PageResult(status, current)

            return PageResult(
                200,
                current,
                html=response.text,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
        raise RuntimeError(f"Too many redirects from {url}")
