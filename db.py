"""SQLite schema and connection helpers."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  number TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  units TEXT,
  department TEXT NOT NULL,
  school TEXT NOT NULL,
  source_url TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (subject, number)
);
CREATE TABLE IF NOT EXISTS majors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  school TEXT NOT NULL,
  source_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS major_courses (
  major_id INTEGER NOT NULL REFERENCES majors(id) ON DELETE CASCADE,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  PRIMARY KEY (major_id, course_id)
);
CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('student', 'tutor')),
  major_id INTEGER REFERENCES majors(id) ON DELETE SET NULL,
  bio TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tutor_courses (
  tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  PRIMARY KEY (tutor_id, course_id)
);
CREATE TABLE IF NOT EXISTS tutor_slots (
  id INTEGER PRIMARY KEY,
  tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  location TEXT NOT NULL,
  cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_slots_tutor ON tutor_slots (tutor_id, starts_at);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY,
  slot_id INTEGER NOT NULL UNIQUE REFERENCES tutor_slots(id) ON DELETE CASCADE,
  student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bookings_student ON bookings (student_id);

CREATE TABLE IF NOT EXISTS study_groups (
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES courses(id),
  creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  topic TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  location TEXT NOT NULL,
  capacity INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_groups_course ON study_groups (course_id, starts_at);
CREATE TABLE IF NOT EXISTS study_group_members (
  group_id INTEGER NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_user ON study_group_members (user_id);
"""


def connect(path):
    """Opens a connection in autocommit mode; multi-statement writes use transaction()."""
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    db.close()


@contextmanager
def transaction(db):
    """Runs a block atomically. BEGIN IMMEDIATE makes concurrent writers take turns,

    so check-then-insert logic (like "is this group full?") can't race between requests.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
