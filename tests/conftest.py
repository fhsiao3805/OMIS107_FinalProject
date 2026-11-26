"""Database setup for the tests.

The tests use the database named by TEST_DATABASE_URL and empty its tables before
every test. They never connect to DATABASE_URL, and they refuse to start if both
variables name the same database, because emptying it would erase real data.
"""
import os

import pytest
from dotenv import dotenv_values, load_dotenv
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

import db as database

load_dotenv()
TEST_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
# Read only to compare against, never to connect to.
APP_URL = (os.environ.get("DATABASE_URL") or dotenv_values().get("DATABASE_URL") or "").strip()
# Blank, not removed, so a later load_dotenv() can't bring the real values back.
# Tests never reach the real database or the real file bucket.
os.environ["DATABASE_URL"] = ""
os.environ["ATTACHMENTS_BUCKET"] = ""

ALL_TABLES = sql.SQL(", ").join(map(sql.Identifier, database.TABLES))


def _same_database(a, b):
    """True when two connection strings reach the same database (Neon's pooled and direct hosts count as one)."""
    def where(url):
        parts = conninfo_to_dict(url)
        return parts.get("host", "").replace("-pooler", ""), parts.get("port") or "5432", parts.get("dbname")
    return where(a) == where(b)


@pytest.fixture(scope="session")
def test_database_url():
    if not TEST_URL:
        pytest.skip("TEST_DATABASE_URL is not set. Add it to .env to run the database tests.")
    if APP_URL and _same_database(TEST_URL, APP_URL):
        pytest.exit(
            "TEST_DATABASE_URL names the same database as DATABASE_URL. The tests empty every table, "
            "so point TEST_DATABASE_URL at a separate database.",
            returncode=2,
        )
    return TEST_URL


@pytest.fixture
def database_url(test_database_url):
    """The test database with its tables created if missing and emptied, so each test starts fresh."""
    database.init_database(test_database_url)
    with database.connect(test_database_url) as db:
        db.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(ALL_TABLES))
    return test_database_url


@pytest.fixture
def drop_tables_after(test_database_url):
    """For tests that reshape tables: drops them all afterwards so the next test recreates them normally."""
    yield
    with database.connect(test_database_url) as db:
        db.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ALL_TABLES))
