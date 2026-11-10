"""Loads cached bulletin pages into the database."""
import re
from datetime import datetime, timezone

from db import transaction

from .parse import build_catalog, parse_page
from .section_subjects import DEPARTMENT_SUBJECTS, SECTION_SUBJECTS

CHAPTER_URL = re.compile(r"/chapter-[3-6]-")


def _set_meta(db, key, value):
    db.execute(
        "INSERT INTO catalog_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def import_catalog(db, cache, complete=False):
    """Rebuilds the catalog tables from cached pages.

    IDs stay stable across imports so tutor courses, bookings and study groups
    keep pointing at the same course. Courses are only retired after a complete
    crawl that looks healthy.
    """
    pages = [parse_page(cache.read_html(url), url) for url in cache.urls() if CHAPTER_URL.search(url)]
    catalog = build_catalog(pages, SECTION_SUBJECTS, DEPARTMENT_SUBJECTS)
    courses, majors, unresolved = catalog["courses"], catalog["majors"], catalog["unresolved"]
    if not courses:
        return {"courses": 0, "majors": 0, "retired": 0, "unresolved": unresolved}

    with transaction(db):
        before = db.execute("SELECT COUNT(*) FROM courses WHERE active = 1").fetchone()[0]
        retire = complete and len(courses) >= before * 0.5
        if retire:
            db.execute("UPDATE courses SET active = 0")

        for course in courses:
            db.execute(
                """INSERT INTO courses (subject, number, title, description, units, department, school, source_url, active)
                   VALUES (:subject, :number, :title, :description, :units, :department, :school, :sourceUrl, 1)
                   ON CONFLICT (subject, number) DO UPDATE SET title = excluded.title, description = excluded.description,
                     units = excluded.units, department = excluded.department, school = excluded.school,
                     source_url = excluded.source_url, active = 1""",
                course,
            )
        course_ids = {f"{row['subject']} {row['number']}": row["id"] for row in db.execute("SELECT id, subject, number FROM courses")}

        db.execute("DELETE FROM major_courses")
        seen = []
        for major in majors:
            db.execute(
                """INSERT INTO majors (name, school, source_url) VALUES (?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET school = excluded.school, source_url = excluded.source_url""",
                (major["name"], major["school"], major["sourceUrl"]),
            )
            major_id = db.execute("SELECT id FROM majors WHERE name = ?", (major["name"],)).fetchone()[0]
            seen.append(major_id)
            db.executemany(
                "INSERT OR IGNORE INTO major_courses (major_id, course_id) VALUES (?, ?)",
                [(major_id, course_ids[key]) for key in major["courses"]],
            )
        if retire and seen:
            db.execute(f"DELETE FROM majors WHERE id NOT IN ({','.join(map(str, seen))})")

        stamp = datetime.now(timezone.utc).isoformat()
        _set_meta(db, "last_import", stamp)
        if complete:
            _set_meta(db, "last_complete_crawl", stamp)

        retired = db.execute("SELECT COUNT(*) FROM courses WHERE active = 0").fetchone()[0]
    return {"courses": len(courses), "majors": len(majors), "retired": retired, "unresolved": unresolved}
