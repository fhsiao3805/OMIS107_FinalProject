"""Walks the bulletin index and its course pages, keeping a copy of each on disk."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .polite_fetcher import (
    BackoffError,
    PoliteFetcher,
    RobotsDisallowedError,
    RobotsUnavailableError,
)

BULLETIN_INDEX = "https://www.scu.edu/bulletin/undergraduate/"

# Chapters 3-6 are the colleges and university programs, the only ones that list courses.
COURSE_PAGE = re.compile(r"^/bulletin/undergraduate/chapter-[3-6]-[^/]+/[^/]+\.html$")

DAY_MS = 86_400_000
MAX_CONSECUTIVE_FAILURES = 5


def _now_ms():
    return int(time.time() * 1000)


class PageCache:
    """On-disk copy of fetched pages, so parsing never needs another request."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}

    def get(self, url):
        return self.manifest.get(url)

    def read_html(self, url):
        entry = self.manifest.get(url)
        return (self.dir / entry["file"]).read_text(encoding="utf-8") if entry else None

    def put(self, url, result, fetched_at):
        name = f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.html"
        (self.dir / name).write_text(result.html, encoding="utf-8")
        self.manifest[url] = {
            "file": name,
            "etag": result.etag,
            "lastModified": result.last_modified,
            "fetchedAt": fetched_at,
        }
        self._save()

    def touch(self, url, fetched_at):
        self.manifest[url]["fetchedAt"] = fetched_at
        self._save()

    def urls(self):
        return list(self.manifest)

    def _save(self):
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.manifest, indent=1))
        tmp.replace(self.manifest_path)


def acquire_crawl_lock(directory):
    """Guarantees one crawler per data directory, so runs can't combine into double the rate.

    Returns a release function, or None if another live process holds the lock.
    """
    path = Path(directory) / ".crawl.lock"
    for _ in range(2):
        try:
            with open(path, "x") as handle:
                handle.write(str(os.getpid()))
            return lambda: path.unlink(missing_ok=True)
        except FileExistsError:
            try:
                os.kill(int(path.read_text() or 0), 0)
                return None
            except PermissionError:
                return None  # alive, owned by another user
            except (ProcessLookupError, ValueError):
                path.unlink(missing_ok=True)  # left behind by a process that died
    return None


def discover_course_pages(html, base_url=BULLETIN_INDEX):
    soup = BeautifulSoup(html, "html.parser")
    pages = set()
    base_host = urlsplit(base_url).hostname
    for anchor in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, anchor["href"])).url.split("?")[0]
        parts = urlsplit(url)
        if parts.hostname == base_host and COURSE_PAGE.match(parts.path):
            pages.add(url)
    return sorted(pages)


def crawl_bulletin(cache, fetcher=None, max_age_days=7, now=_now_ms, log=lambda m: None, on_progress=lambda done, total: None):
    """Fetches the bulletin through a PoliteFetcher.

    Pages fetched within max_age_days are reused without any request, which also
    lets an interrupted crawl resume where it stopped.
    """
    fetcher = fetcher or PoliteFetcher(log=log)
    summary = {
        "complete": False, "pages": [], "fetched": 0, "not_modified": 0,
        "reused": 0, "failed": [], "stopped_because": None,
    }

    def is_fresh(url):
        entry = cache.get(url)
        return entry is not None and now() - entry["fetchedAt"] < max_age_days * DAY_MS

    def retrieve(url):
        if is_fresh(url):
            summary["reused"] += 1
            return cache.read_html(url)
        prior = cache.get(url) or {}
        result = fetcher.fetch_page(url, etag=prior.get("etag"), last_modified=prior.get("lastModified"))
        if result.status == 304:
            cache.touch(url, now())
            summary["not_modified"] += 1
            return cache.read_html(url)
        if result.status != 200:
            raise RuntimeError(f"HTTP {result.status}")
        cache.put(url, result, now())
        summary["fetched"] += 1
        return result.html

    try:
        summary["pages"] = discover_course_pages(retrieve(BULLETIN_INDEX))
        if not summary["pages"]:
            raise RuntimeError("No course pages found on the bulletin index; has its layout changed?")
        total = len(summary["pages"])
        log(f"Found {total} course pages")

        consecutive_failures = 0
        for done, url in enumerate(summary["pages"], start=1):
            try:
                retrieve(url)
                consecutive_failures = 0
            except (BackoffError, RobotsUnavailableError):
                raise
            except (RobotsDisallowedError, RuntimeError, requests.RequestException) as err:
                skipped = isinstance(err, RobotsDisallowedError)
                log(f"{'Skipped' if skipped else 'Failed'} {url}: {err}")
                summary["failed"].append({"url": url, "reason": str(err)})
                if not skipped:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        raise RuntimeError(f"Stopping after {MAX_CONSECUTIVE_FAILURES} consecutive failures") from err
            on_progress(done, total)
        summary["complete"] = not summary["failed"]
    except (BackoffError, RobotsUnavailableError, RuntimeError, requests.RequestException) as err:
        summary["stopped_because"] = str(err)
        log(f"Crawl stopped: {err}")
    return summary
