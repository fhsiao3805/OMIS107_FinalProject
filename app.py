"""SCU Study Hub: tutoring and study groups for Santa Clara University students.

Run with `python app.py`. See README.md for crawler commands.
"""
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import auth
from auth import HttpError
from crawler.crawl import PageCache, acquire_crawl_lock, crawl_bulletin
from crawler.import_catalog import import_catalog
from crawler.polite_fetcher import PoliteFetcher, build_user_agent
import db as database
from db import transaction

CAMPUS_TIME = ZoneInfo("America/Los_Angeles")
MIN_DURATION = timedelta(minutes=15)
MAX_DURATION = timedelta(hours=4)
MAX_LEAD = timedelta(days=180)
MAX_REPEAT_WEEKS = 12
DAY_SECONDS = 86_400

OPEN_SLOT = "s.cancelled_at IS NULL AND s.starts_at > :now AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.slot_id = s.id)"
MEMBER_COUNT = "(SELECT COUNT(*) FROM study_group_members m WHERE m.group_id = g.id)"

log = logging.getLogger("studyhub")


# ---- Validation ----


def iso(moment):
    """Fixed-width UTC timestamps, so SQL string comparison matches time order."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def text_field(value, label, max_length, min_length=1):
    result = value.strip() if isinstance(value, str) else ""
    if not min_length <= len(result) <= max_length:
        raise HttpError(400, f"{label} must be {min_length}-{max_length} characters.")
    return result


def parse_int(value):
    """An integer from JSON or a form string, otherwise None. Rejects booleans and floats."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()) and value.isascii():
        return int(value)
    return None


def positive_int(value, label):
    number = parse_int(value)
    if number is None or number <= 0 or number > 2**53:
        raise HttpError(400, f"{label} is invalid.")
    return number


def id_csv(value, label):
    parts = [part for part in (value or "").split(",") if part][:50]
    return [positive_int(part, label) for part in parts]


def parse_instant(value):
    """Parses an ISO timestamp, returning None unless it is valid and states a timezone."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


def time_range(body, now):
    start = parse_instant(body.get("startsAt"))
    end = parse_instant(body.get("endsAt"))
    if start is None or end is None:
        raise HttpError(400, "Enter a valid start and end time.")
    if start <= now:
        raise HttpError(400, "The start time must be in the future.")
    if start > now + MAX_LEAD:
        raise HttpError(400, "Sessions can be scheduled at most 180 days ahead.")
    if not MIN_DURATION <= end - start <= MAX_DURATION:
        raise HttpError(400, "Sessions must last between 15 minutes and 4 hours.")
    return start, end


def add_weeks(moment, weeks):
    """Moves by whole weeks on the campus clock, so 5 PM stays 5 PM across daylight saving."""
    local = moment.astimezone(CAMPUS_TIME).replace(tzinfo=None) + timedelta(weeks=weeks)
    return local.replace(tzinfo=CAMPUS_TIME).astimezone(timezone.utc)


def rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


# ---- Application ----


def create_app(data_dir=None, clock=None, secure_cookies=None):
    base = Path(data_dir or os.environ.get("DATA_DIR") or Path(__file__).parent / "data")
    db_path = str(base / "app.db")
    database.init_database(db_path)
    now = clock or (lambda: datetime.now(timezone.utc))

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
    app.config["DATA_DIR"] = base
    app.config["DB_PATH"] = db_path
    app.config["CATALOG_STATE"] = {"crawling": False}
    if secure_cookies is None:
        secure_cookies = os.environ.get("SECURE_COOKIES") == "1"

    limiter = auth.AttemptLimiter()

    def get_db():
        if "db" not in g:
            g.db = database.connect(db_path)
        return g.db

    def now_iso():
        return iso(now())

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.before_request
    def load_user():
        g.user = None
        if request.path.startswith("/api"):
            token = request.cookies.get(auth.SESSION_COOKIE)
            g.user = auth.user_for_token(get_db(), token, now().timestamp())

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HttpError)
    def handle_http_error(err):
        return jsonify(error=err.message), err.status

    @app.errorhandler(HTTPException)
    def handle_framework_error(err):
        if request.path.startswith("/api"):
            messages = {404: "Not found.", 405: "Not found.", 413: "Invalid request."}
            return jsonify(error=messages.get(err.code, "Invalid request.")), err.code
        return err

    @app.errorhandler(Exception)
    def handle_unexpected(err):
        log.exception("Unhandled error", exc_info=err)
        return jsonify(error="Something went wrong."), 500

    def signed_in(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.user is None:
                raise HttpError(401, "Please log in first.")
            return view(*args, **kwargs)

        return wrapper

    def require_role(expected):
        def decorator(view):
            @wraps(view)
            @signed_in
            def wrapper(*args, **kwargs):
                if g.user["role"] != expected:
                    raise HttpError(403, f"Only {expected} accounts can do that.")
                return view(*args, **kwargs)

            return wrapper

        return decorator

    student = require_role("student")
    tutor = require_role("tutor")

    def require_course(course_id):
        if not get_db().execute("SELECT 1 FROM courses WHERE id = ? AND active = 1", (course_id,)).fetchone():
            raise HttpError(404, "Course not found.")

    def optional_major(value):
        if value is None or value == "":
            return None
        major_id = positive_int(value, "Major")
        if not get_db().execute("SELECT 1 FROM majors WHERE id = ?", (major_id,)).fetchone():
            raise HttpError(400, "Major not found.")
        return major_id

    def assert_schedule_free(user_id, starts_at, ends_at, message="That overlaps something already on your schedule."):
        """Rejects a time that overlaps the user's bookings, study groups, or own tutoring slots."""
        clash = get_db().execute(
            """SELECT 1 FROM bookings b JOIN tutor_slots s ON s.id = b.slot_id
                 WHERE b.student_id = :id AND s.cancelled_at IS NULL AND s.starts_at < :end AND s.ends_at > :start
               UNION ALL
               SELECT 1 FROM study_group_members m JOIN study_groups g ON g.id = m.group_id
                 WHERE m.user_id = :id AND g.starts_at < :end AND g.ends_at > :start
               UNION ALL
               SELECT 1 FROM tutor_slots s
                 WHERE s.tutor_id = :id AND s.cancelled_at IS NULL AND s.starts_at < :end AND s.ends_at > :start
               LIMIT 1""",
            {"id": user_id, "start": starts_at, "end": ends_at},
        ).fetchone()
        if clash:
            raise HttpError(409, message)

    def check_password(email, row, password, failure_message):
        """Verifies a password under the attempt limit. `row` is None when the email is unknown."""
        key = f"{request.remote_addr}|{email}"
        instant = now().timestamp()
        limiter.check(key, instant)
        valid = auth.verify_password(row["password_hash"] if row else auth.DUMMY_HASH, password)
        if row is None or not valid:
            limiter.fail(key, instant)
            raise HttpError(401, failure_message)
        limiter.reset(key)

    def start_session(response, user_id, active_role=None):
        token = auth.create_session(get_db(), user_id, now().timestamp(), active_role)
        response.set_cookie(
            auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL_SECONDS,
            httponly=True, samesite="Strict", secure=secure_cookies, path="/",
        )
        return response

    # ---- Pages ----

    @app.get("/")
    @app.get("/home")
    def index():
        return render_template("index.html")

    # ---- Accounts ----

    @app.post("/api/auth/signup")
    def signup():
        data = body()
        email = text_field(data.get("email"), "Email", 254).lower()
        if not re.fullmatch(r"[^@\s]+@scu\.edu", email):
            raise HttpError(400, "Use your @scu.edu email address.")
        name = text_field(data.get("name"), "Name", 80)
        password = text_field(data.get("password"), "Password", 200, min_length=8)
        role = data.get("role")
        if role not in ("student", "tutor"):
            raise HttpError(400, "Choose student or tutor.")
        major_id = optional_major(data.get("majorId"))
        db = get_db()

        # A tutor signing up as a student adds student access to the account they already have.
        existing = db.execute("SELECT id, role, password_hash, student_enabled FROM users WHERE email = ?", (email,)).fetchone()
        if existing and role == "student" and existing["role"] == "tutor" and not existing["student_enabled"]:
            check_password(email, existing, password, "That email has a tutor account. Enter its password to add student access.")
            db.execute("UPDATE users SET student_enabled = 1, major_id = COALESCE(major_id, ?) WHERE id = ?", (major_id, existing["id"]))
            response = jsonify(user=auth.load_user(db, existing["id"], "student"))
            response.status_code = 201
            return start_session(response, existing["id"], "student")

        password_hash = auth.hash_password(password)
        try:
            user_id = db.execute(
                "INSERT INTO users (email, name, password_hash, role, major_id) VALUES (?, ?, ?, ?, ?)",
                (email, name, password_hash, role, major_id),
            ).lastrowid
        except sqlite3.IntegrityError as err:
            raise HttpError(409, "An account with that email already exists.") from err
        response = jsonify(user=auth.load_user(db, user_id))
        response.status_code = 201
        return start_session(response, user_id)

    @app.post("/api/auth/login")
    def login():
        data = body()
        email = text_field(data.get("email"), "Email", 254).lower()
        password = text_field(data.get("password"), "Password", 200)
        row = get_db().execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        check_password(email, row, password, "Incorrect email or password.")
        return start_session(jsonify(user=auth.load_user(get_db(), row["id"])), row["id"])

    @app.post("/api/auth/logout")
    def logout():
        auth.destroy_session(get_db(), request.cookies.get(auth.SESSION_COOKIE))
        response = jsonify(ok=True)
        response.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, samesite="Strict")
        return response

    @app.get("/api/me")
    def me():
        return jsonify(user=g.user)

    @app.patch("/api/me")
    @signed_in
    def update_me():
        data = body()
        name = text_field(data.get("name"), "Name", 80)
        bio = text_field(data.get("bio", ""), "Bio", 500, min_length=0)
        major_id = optional_major(data.get("majorId"))
        get_db().execute("UPDATE users SET name = ?, bio = ?, major_id = ? WHERE id = ?", (name, bio, major_id, g.user["id"]))
        return jsonify(user={**g.user, "name": name, "bio": bio, "majorId": major_id})

    @app.post("/api/me/role")
    @signed_in
    def switch_role():
        role = body().get("role")
        if role not in g.user["roles"]:
            raise HttpError(403, "Your account does not have that view.")
        auth.set_active_role(get_db(), request.cookies.get(auth.SESSION_COOKIE), role)
        return jsonify(user={**g.user, "role": role})

    @app.post("/api/me/student-account")
    @tutor
    def add_student_account():
        db = get_db()
        db.execute("UPDATE users SET student_enabled = 1 WHERE id = ?", (g.user["id"],))
        auth.set_active_role(db, request.cookies.get(auth.SESSION_COOKIE), "student")
        return jsonify(user=auth.load_user(db, g.user["id"], "student")), 201

    # ---- Catalog ----

    @app.get("/api/status")
    def status():
        db = get_db()
        courses = db.execute("SELECT COUNT(*) FROM courses WHERE active = 1").fetchone()[0]
        last = db.execute("SELECT value FROM catalog_meta WHERE key = 'last_import'").fetchone()
        return jsonify(courses=courses, lastCrawl=last[0] if last else None, **app.config["CATALOG_STATE"])

    @app.get("/api/majors")
    def majors():
        return jsonify(majors=rows(get_db().execute("SELECT id, name, school FROM majors ORDER BY name")))

    @app.get("/api/courses")
    def courses():
        where = ["c.active = 1"]
        params = {"now": now_iso()}
        for i, token in enumerate(request.args.get("q", "").split()[:8]):
            params[f"t{i}"] = re.sub(r"[\\%_]", lambda m: "\\" + m[0], token)
            where.append(
                f"""(c.subject LIKE :t{i} || '%' ESCAPE '\\' OR c.number LIKE :t{i} || '%' ESCAPE '\\'
                OR c.subject || c.number LIKE :t{i} || '%' ESCAPE '\\'
                OR c.title LIKE '%' || :t{i} || '%' ESCAPE '\\' OR c.department LIKE '%' || :t{i} || '%' ESCAPE '\\')"""
            )
        if request.args.get("major"):
            params["major"] = positive_int(request.args["major"], "Major")
            where.append("EXISTS (SELECT 1 FROM major_courses mc WHERE mc.course_id = c.id AND mc.major_id = :major)")
        if request.args.get("available") == "1":
            where.append(
                f"""(EXISTS (SELECT 1 FROM tutor_courses tc JOIN tutor_slots s ON s.tutor_id = tc.tutor_id
                      WHERE tc.course_id = c.id AND {OPEN_SLOT})
                    OR EXISTS (SELECT 1 FROM study_groups g WHERE g.course_id = c.id AND g.starts_at > :now AND {MEMBER_COUNT} < g.capacity))"""
            )
        limit = min(positive_int(request.args.get("limit", "40"), "Limit"), 100)
        offset = parse_int(request.args.get("offset", "0")) or 0
        clause = " AND ".join(where)

        db = get_db()
        total = db.execute(f"SELECT COUNT(*) FROM courses c WHERE {clause}", params).fetchone()[0]
        found = rows(db.execute(
            f"""SELECT c.id, c.subject, c.number, c.title, c.units, c.department, c.school,
                  (SELECT COUNT(DISTINCT s.id) FROM tutor_courses tc JOIN tutor_slots s ON s.tutor_id = tc.tutor_id
                    WHERE tc.course_id = c.id AND {OPEN_SLOT}) AS openSlots,
                  (SELECT COUNT(*) FROM study_groups g WHERE g.course_id = c.id AND g.starts_at > :now AND {MEMBER_COUNT} < g.capacity) AS openGroups
                FROM courses c WHERE {clause}
                ORDER BY c.subject, CAST(c.number AS INTEGER), c.number
                LIMIT {limit} OFFSET {offset}""",
            params,
        ))
        return jsonify(total=total, courses=found)

    @app.get("/api/courses/by-ids")
    def courses_by_ids():
        ids = id_csv(request.args.get("ids"), "Course")
        if not ids:
            return jsonify(courses=[])
        marks = ",".join("?" * len(ids))
        return jsonify(courses=rows(get_db().execute(
            f"""SELECT id, subject, number, title, units, description, department, source_url AS sourceUrl
                FROM courses WHERE active = 1 AND id IN ({marks})""",
            ids,
        )))

    @app.get("/api/availability")
    def availability():
        """Open tutor slots and joinable study groups for the selected courses."""
        ids = id_csv(request.args.get("courses"), "Course")
        if not ids:
            return jsonify(slots=[], groups=[])
        id_list = ",".join(map(str, ids))  # positive_int guarantees plain integers
        db = get_db()
        stamp = now_iso()

        slots = rows(db.execute(
            f"""SELECT s.id, s.starts_at AS startsAt, s.ends_at AS endsAt, s.location,
                  u.name AS tutorName, u.bio AS tutorBio, GROUP_CONCAT(tc.course_id) AS courseIds,
                  s.tutor_id = :me AS mine
                FROM tutor_slots s
                JOIN users u ON u.id = s.tutor_id
                JOIN tutor_courses tc ON tc.tutor_id = s.tutor_id AND tc.course_id IN ({id_list})
                WHERE {OPEN_SLOT}
                GROUP BY s.id ORDER BY s.starts_at LIMIT 200""",
            {"now": stamp, "me": g.user["id"] if g.user else 0},
        ))
        for slot in slots:
            slot["courseIds"] = [int(v) for v in slot["courseIds"].split(",")]
            slot["mine"] = bool(slot["mine"])

        groups = rows(db.execute(
            f"""SELECT g.id, g.course_id AS courseId, g.topic, g.starts_at AS startsAt, g.ends_at AS endsAt,
                  g.location, g.capacity, {MEMBER_COUNT} AS memberCount, u.name AS creatorName,
                  EXISTS (SELECT 1 FROM study_group_members m WHERE m.group_id = g.id AND m.user_id = :me) AS joined
                FROM study_groups g JOIN users u ON u.id = g.creator_id
                WHERE g.course_id IN ({id_list}) AND g.starts_at > :now
                ORDER BY g.starts_at LIMIT 200""",
            {"now": stamp, "me": g.user["id"] if g.user else 0},
        ))
        for group in groups:
            group["joined"] = bool(group["joined"])
            group["creatorName"] = group["creatorName"].split(" ")[0]
        return jsonify(slots=slots, groups=groups)

    # ---- Tutors ----

    @app.get("/api/tutor/courses")
    @tutor
    def tutor_courses():
        return jsonify(courses=rows(get_db().execute(
            """SELECT c.id, c.subject, c.number, c.title FROM tutor_courses tc JOIN courses c ON c.id = tc.course_id
               WHERE tc.tutor_id = ? ORDER BY c.subject, CAST(c.number AS INTEGER)""",
            (g.user["id"],),
        )))

    @app.put("/api/tutor/courses")
    @tutor
    def set_tutor_courses():
        submitted = body().get("courseIds")
        if not isinstance(submitted, list) or len(submitted) > 100:
            raise HttpError(400, "Course list is invalid.")
        ids = list(dict.fromkeys(positive_int(value, "Course list") for value in submitted))
        db = get_db()
        with transaction(db):
            for course_id in ids:
                require_course(course_id)
            db.execute("DELETE FROM tutor_courses WHERE tutor_id = ?", (g.user["id"],))
            db.executemany("INSERT INTO tutor_courses (tutor_id, course_id) VALUES (?, ?)", [(g.user["id"], i) for i in ids])
        return jsonify(ok=True)

    @app.get("/api/tutor/slots")
    @tutor
    def tutor_slots():
        return jsonify(slots=rows(get_db().execute(
            """SELECT s.id, s.starts_at AS startsAt, s.ends_at AS endsAt, s.location, b.note,
                 st.name AS studentName, st.email AS studentEmail, c.subject, c.number, c.title
               FROM tutor_slots s
               LEFT JOIN bookings b ON b.slot_id = s.id
               LEFT JOIN users st ON st.id = b.student_id
               LEFT JOIN courses c ON c.id = b.course_id
               WHERE s.tutor_id = ? AND s.cancelled_at IS NULL AND s.ends_at > ?
               ORDER BY s.starts_at""",
            (g.user["id"], now_iso()),
        )))

    @app.post("/api/tutor/slots")
    @tutor
    def create_tutor_slots():
        data = body()
        start, end = time_range(data, now())
        location = text_field(data.get("location"), "Location", 120)
        repeat_weeks = 1 if data.get("repeatWeeks") is None else parse_int(data.get("repeatWeeks"))
        if repeat_weeks is None or not 1 <= repeat_weeks <= MAX_REPEAT_WEEKS:
            raise HttpError(400, f"Repeat must be between 1 and {MAX_REPEAT_WEEKS} weeks.")

        db = get_db()
        ids = []
        with transaction(db):
            for week in range(repeat_weeks):
                starts_at = iso(add_weeks(start, week))
                ends_at = iso(add_weeks(end, week))
                assert_schedule_free(g.user["id"], starts_at, ends_at, f"You already have something scheduled overlapping {starts_at[:10]}.")
                ids.append(db.execute(
                    "INSERT INTO tutor_slots (tutor_id, starts_at, ends_at, location) VALUES (?, ?, ?, ?)",
                    (g.user["id"], starts_at, ends_at, location),
                ).lastrowid)
        return jsonify(ids=ids), 201

    @app.delete("/api/tutor/slots/<int:slot_id>")
    @tutor
    def delete_tutor_slot(slot_id):
        """An unbooked slot is deleted. A booked one is kept and marked cancelled so the student can see it."""
        db = get_db()
        with transaction(db):
            slot = db.execute(
                "SELECT id FROM tutor_slots WHERE id = ? AND tutor_id = ? AND cancelled_at IS NULL", (slot_id, g.user["id"])
            ).fetchone()
            if slot is None:
                raise HttpError(404, "Slot not found.")
            if db.execute("SELECT 1 FROM bookings WHERE slot_id = ?", (slot_id,)).fetchone():
                db.execute("UPDATE tutor_slots SET cancelled_at = ? WHERE id = ?", (now_iso(), slot_id))
            else:
                db.execute("DELETE FROM tutor_slots WHERE id = ?", (slot_id,))
        return jsonify(ok=True)

    # ---- Students: booking tutors ----

    @app.post("/api/slots/<int:slot_id>/book")
    @student
    def book_slot(slot_id):
        data = body()
        course_id = positive_int(data.get("courseId"), "Course")
        note = text_field(data.get("note", ""), "Note", 500, min_length=0)
        db = get_db()
        with transaction(db):
            slot = db.execute(
                """SELECT tutor_id, starts_at, ends_at FROM tutor_slots
                   WHERE id = ? AND cancelled_at IS NULL AND starts_at > ?""",
                (slot_id, now_iso()),
            ).fetchone()
            if slot is None:
                raise HttpError(404, "That time slot is no longer available.")
            if slot["tutor_id"] == g.user["id"]:
                raise HttpError(400, "You cannot book your own time slot.")
            if not db.execute(
                "SELECT 1 FROM tutor_courses WHERE tutor_id = ? AND course_id = ?", (slot["tutor_id"], course_id)
            ).fetchone():
                raise HttpError(400, "That tutor does not offer this course.")
            if db.execute("SELECT 1 FROM bookings WHERE slot_id = ?", (slot_id,)).fetchone():
                raise HttpError(409, "Someone just booked that slot.")
            assert_schedule_free(g.user["id"], slot["starts_at"], slot["ends_at"])
            db.execute(
                "INSERT INTO bookings (slot_id, student_id, course_id, note) VALUES (?, ?, ?, ?)",
                (slot_id, g.user["id"], course_id, note),
            )
        return jsonify(ok=True), 201

    @app.delete("/api/slots/<int:slot_id>/book")
    @student
    def cancel_booking(slot_id):
        deleted = get_db().execute("DELETE FROM bookings WHERE slot_id = ? AND student_id = ?", (slot_id, g.user["id"])).rowcount
        if not deleted:
            raise HttpError(404, "Booking not found.")
        return jsonify(ok=True)

    # ---- Students: study groups ----

    @app.post("/api/groups")
    @student
    def create_group():
        data = body()
        course_id = positive_int(data.get("courseId"), "Course")
        start, end = time_range(data, now())
        starts_at, ends_at = iso(start), iso(end)
        topic = text_field(data.get("topic"), "Topic", 120)
        location = text_field(data.get("location"), "Location", 120)
        capacity = parse_int(data.get("capacity"))
        if capacity is None or not 2 <= capacity <= 30:
            raise HttpError(400, "Group size must be between 2 and 30.")

        db = get_db()
        with transaction(db):
            require_course(course_id)
            assert_schedule_free(g.user["id"], starts_at, ends_at)
            group_id = db.execute(
                """INSERT INTO study_groups (course_id, creator_id, topic, starts_at, ends_at, location, capacity)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (course_id, g.user["id"], topic, starts_at, ends_at, location, capacity),
            ).lastrowid
            db.execute("INSERT INTO study_group_members (group_id, user_id) VALUES (?, ?)", (group_id, g.user["id"]))
        return jsonify(id=group_id), 201

    @app.post("/api/groups/<int:group_id>/join")
    @student
    def join_group(group_id):
        db = get_db()
        with transaction(db):
            group = db.execute(
                f"""SELECT g.starts_at, g.ends_at, g.capacity, {MEMBER_COUNT} AS members
                    FROM study_groups g WHERE g.id = ? AND g.starts_at > ?""",
                (group_id, now_iso()),
            ).fetchone()
            if group is None:
                raise HttpError(404, "That study group is no longer available.")
            if db.execute("SELECT 1 FROM study_group_members WHERE group_id = ? AND user_id = ?", (group_id, g.user["id"])).fetchone():
                raise HttpError(409, "You already joined this group.")
            if group["members"] >= group["capacity"]:
                raise HttpError(409, "This group is full.")
            assert_schedule_free(g.user["id"], group["starts_at"], group["ends_at"])
            db.execute("INSERT INTO study_group_members (group_id, user_id) VALUES (?, ?)", (group_id, g.user["id"]))
        return jsonify(ok=True), 201

    @app.delete("/api/groups/<int:group_id>/join")
    @student
    def leave_group(group_id):
        db = get_db()
        if db.execute("SELECT 1 FROM study_groups WHERE id = ? AND creator_id = ?", (group_id, g.user["id"])).fetchone():
            raise HttpError(400, "You created this group, so cancel it instead of leaving.")
        deleted = db.execute("DELETE FROM study_group_members WHERE group_id = ? AND user_id = ?", (group_id, g.user["id"])).rowcount
        if not deleted:
            raise HttpError(404, "You are not in this group.")
        return jsonify(ok=True)

    @app.delete("/api/groups/<int:group_id>")
    @student
    def delete_group(group_id):
        deleted = get_db().execute("DELETE FROM study_groups WHERE id = ? AND creator_id = ?", (group_id, g.user["id"])).rowcount
        if not deleted:
            raise HttpError(404, "Study group not found.")
        return jsonify(ok=True)

    @app.get("/api/me/schedule")
    @student
    def schedule():
        db = get_db()
        stamp = now_iso()
        bookings = rows(db.execute(
            """SELECT s.id AS slotId, s.starts_at AS startsAt, s.ends_at AS endsAt, s.location, s.cancelled_at AS cancelledAt, b.note,
                 u.name AS tutorName, u.email AS tutorEmail, c.subject, c.number, c.title
               FROM bookings b
               JOIN tutor_slots s ON s.id = b.slot_id
               JOIN users u ON u.id = s.tutor_id
               JOIN courses c ON c.id = b.course_id
               WHERE b.student_id = ? AND s.ends_at > ? ORDER BY s.starts_at""",
            (g.user["id"], stamp),
        ))
        groups = rows(db.execute(
            """SELECT g.id, g.topic, g.starts_at AS startsAt, g.ends_at AS endsAt, g.location, g.capacity,
                 g.creator_id = :me AS isCreator, c.subject, c.number, c.title
               FROM study_group_members m
               JOIN study_groups g ON g.id = m.group_id
               JOIN courses c ON c.id = g.course_id
               WHERE m.user_id = :me AND g.ends_at > :now ORDER BY g.starts_at""",
            {"me": g.user["id"], "now": stamp},
        ))
        for group in groups:
            group["isCreator"] = bool(group["isCreator"])
            group["members"] = rows(db.execute(
                """SELECT u.name, u.email FROM study_group_members m JOIN users u ON u.id = m.user_id
                   WHERE m.group_id = ? ORDER BY u.name""",
                (group["id"],),
            ))
        return jsonify(bookings=bookings, groups=groups)

    # ---- Command line: `flask crawl` and `flask import-catalog` ----

    @app.cli.command("crawl")
    @click.option("--no-import", is_flag=True, help="Only download pages.")
    def crawl_command(no_import):
        """Crawl the SCU bulletin, then update the course catalog."""
        cache = PageCache(base / "cache")
        release = acquire_crawl_lock(cache.dir)
        if release is None:
            raise click.ClickException("Another crawl is already running against this data directory.")
        try:
            click.echo(f"User-Agent: {build_user_agent()}")
            summary = crawl_bulletin(
                cache, fetcher=PoliteFetcher(log=click.echo), log=click.echo,
                on_progress=lambda done, total: click.echo(f"Progress {done}/{total}"),
            )
            click.echo({**summary, "pages": len(summary["pages"])})
            if not no_import:
                connection = database.connect(db_path)
                click.echo(import_catalog(connection, cache, complete=summary["complete"]))
                connection.close()
        finally:
            release()

    @app.cli.command("import-catalog")
    @click.option("--complete", is_flag=True, help="Treat the cache as a full crawl, retiring courses no longer listed.")
    def import_command(complete):
        """Rebuild the catalog from downloaded pages, with no network requests."""
        connection = database.connect(db_path)
        click.echo(import_catalog(connection, PageCache(base / "cache"), complete=complete))
        connection.close()

    return app


# ---- Scheduled catalog refresh ----


def refresh_catalog(app):
    """Crawls the bulletin unless a complete crawl is recent enough."""
    refresh_days = float(os.environ.get("BULLETIN_REFRESH_DAYS", 30))
    connection = database.connect(app.config["DB_PATH"])
    state = app.config["CATALOG_STATE"]
    try:
        last = connection.execute("SELECT value FROM catalog_meta WHERE key = 'last_complete_crawl'").fetchone()
        if last and time.time() - datetime.fromisoformat(last[0]).timestamp() < refresh_days * DAY_SECONDS:
            return
        cache = PageCache(app.config["DATA_DIR"] / "cache")
        release = acquire_crawl_lock(cache.dir)
        if release is None:
            log.info("Another crawl is already running.")
            return
        state["crawling"] = True
        try:
            # Students get a growing course list during the first, slow crawl.
            def on_progress(done, _total):
                if done % 10 == 0:
                    import_catalog(connection, cache)

            summary = crawl_bulletin(cache, fetcher=PoliteFetcher(log=log.info), log=log.info, on_progress=on_progress)
            log.info("Imported: %s", import_catalog(connection, cache, complete=summary["complete"]))
        finally:
            state["crawling"] = False
            release()
    except Exception:
        log.exception("Catalog refresh failed")
    finally:
        connection.close()


def start_background_refresh(app):
    def loop():
        while True:
            refresh_catalog(app)
            time.sleep(DAY_SECONDS)

    threading.Thread(target=loop, daemon=True, name="catalog-refresh").start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    flask_app = create_app()
    if os.environ.get("BULLETIN_AUTO_CRAWL") != "0":
        start_background_refresh(flask_app)
    flask_app.run(port=int(os.environ.get("PORT", 8000)), threaded=True)
