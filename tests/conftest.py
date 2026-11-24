"""Database setup for the tests.

The tests read only TEST_DATABASE_URL, never DATABASE_URL. They don't write to that
database either: they create a temporary database next to it, work there, and drop
it at the end. That keeps the tests safe even when TEST_DATABASE_URL names the
same development database the app uses.
"""
import os
import uuid

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import make_conninfo

import db as database

load_dotenv()
TEST_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
# Blank, not removed, so a later load_dotenv() can't bring the real value back.
os.environ["DATABASE_URL"] = ""


def _admin():
    return psycopg.connect(TEST_URL, autocommit=True, prepare_threshold=None)


@pytest.fixture(scope="session")
def scratch_database_url():
    """A temporary database for this test run, created beside TEST_DATABASE_URL and dropped afterwards."""
    if not TEST_URL:
        pytest.skip("TEST_DATABASE_URL is not set. Add it to .env to run the database tests.")
    name = f"studyhub_test_{uuid.uuid4().hex[:12]}"
    with _admin() as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(TEST_URL, dbname=name)
    finally:
        with _admin() as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def database_url(scratch_database_url):
    """The temporary database, emptied of every table so each test starts fresh."""
    with database.connect(scratch_database_url) as db:
        db.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.SQL(", ").join(map(sql.Identifier, database.TABLES))))
    return scratch_database_url
