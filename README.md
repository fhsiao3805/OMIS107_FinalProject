# SCU Study Hub

A tutoring and study group platform for Santa Clara University students, built with Python and Flask.

- **Students** pick courses (searching or filtering by major), book a tutor's open time, or create and join study groups.
- **Tutors** choose the courses they teach and publish available time slots, optionally repeating weekly.
- **Tutors can also be students.** A tutor can add student access to their account and switch between the two views, using the same email and password.
- **Course and major data** is read by the server from the public [SCU Undergraduate Bulletin](https://www.scu.edu/bulletin/undergraduate/).

## Run it

Requires Python 3.11 or newer and a PostgreSQL database (such as one on [Neon](https://neon.tech)).

Put the connection strings in a `.env` file in the project folder. It is listed in `.gitignore`, so it stays on your machine:

```sh
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
TEST_DATABASE_URL=postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
```

```sh
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                    # http://localhost:8000
```

The app stops with a message if `DATABASE_URL` is not set, and creates any missing tables when it starts. On a server, set `DATABASE_URL` in the host's environment settings instead of a `.env` file.

Run the tests with `python -m pytest tests`. They read only `TEST_DATABASE_URL`, never `DATABASE_URL`. They empty every table in that database before each test, so it must be a separate database used only for testing. The tests refuse to start if both variables name the same database. Without `TEST_DATABASE_URL`, the database tests are skipped. They use Flask's test client, so no server or browser is needed.

On GitHub, [.github/workflows/tests.yml](.github/workflows/tests.yml) runs the tests on every pull request and every push to `main`. The job starts its own PostgreSQL database and points `TEST_DATABASE_URL` at it. It uses no secrets and deploys nothing, so it never reaches the Neon databases.

If the database has no course list yet, the server crawls the bulletin in the background, which takes about 25 minutes. The course list fills in as it goes.

## Project layout

| Path | Purpose |
| --- | --- |
| `app.py` | Flask application and all routes |
| `db.py` | PostgreSQL schema and connections |
| `auth.py` | Passwords, sessions, login rate limiting |
| `crawler/` | Bulletin crawler and parser |
| `templates/`, `static/` | The web pages |
| `tests/` | Test suite |

## Bulletin crawler

The rules live in [crawler/polite_fetcher.py](crawler/polite_fetcher.py):

- **robots.txt** is read first on every run and checked before each request, redirects included. If it can't be read, the crawl stops.
- **User-Agent** identifies the program as `SCUStudyHub-BulletinCrawler/1.0`. Set `CRAWLER_CONTACT` to add contact details.
- **Rate**: at most one request every 20 seconds across the whole crawl. Configuration can't go faster than one every 15 seconds, and a larger `Crawl-delay` in robots.txt is honored.
- **Backing off**: an HTTP 429 or 503 ends the run immediately.
- **Reuse**: pages are cached in `data/cache`, reused for 7 days, and revalidated with conditional requests after that. A lock file allows only one crawl at a time.

| Command | What it does |
| --- | --- |
| `flask crawl` | Crawls now, then updates the database |
| `flask import-catalog` | Rebuilds the catalog from cached pages with no network requests |

The server re-crawls every 30 days (`BULLETIN_REFRESH_DAYS`). Set `BULLETIN_AUTO_CRAWL=0` to turn that off.

Most bulletin sections don't state a subject code, so it's inferred from how each page refers to its own courses. Corrections go in [crawler/section_subjects.py](crawler/section_subjects.py).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | required | PostgreSQL connection string |
| `TEST_DATABASE_URL` | none | Separate database for the tests, emptied before each test |
| `PORT` | `8000` | HTTP port |
| `DATA_DIR` | `./data` | Bulletin page cache and crawl lock |
| `SECURE_COOKIES` | off | `1` marks session cookies `Secure` (requires HTTPS) |

## Known limits

- Signup requires an `@scu.edu` address, but no confirmation email is sent, so ownership of the address isn't verified.
- Only the undergraduate bulletin is covered. Graduate bulletins are published separately.
- There are no email notifications. Students and tutors see changes on their schedule pages.
- `python app.py` uses Flask's development server, which suits local use and demos. Use a production WSGI server for real deployment.

## Changelog

### 2026-11-11 — Login landing page

- Updated `static/app.js` so an unqualified visit to `/` shows the login page, and successful login or signup opens the main page at `/home`. Logout returns to `/`.
- Added `/home` as an alias for the Flask app page in `app.py` and updated the brand and “Find help” links to use it; the existing `#/` main-page route remains supported.
- Updated `templates/index.html` and the relevant in-app links for `/home`; added route coverage in `tests/test_api.py`.
