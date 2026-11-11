import itertools
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import db as database
from app import add_weeks, create_app

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
counter = itertools.count(1)


def at(hours):
    return (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def span(start, end=None):
    return {"startsAt": at(start), "endsAt": at(start + 1 if end is None else end)}


@pytest.fixture
def app(tmp_path):
    application = create_app(data_dir=tmp_path, clock=lambda: NOW)
    db = database.connect(application.config["DB_PATH"])
    db.executescript(
        """INSERT INTO courses (id, subject, number, title, department, school, source_url) VALUES
             (1, 'CSCI', '10', 'Intro to Computer Science', 'MCS', 'CAS', 'https://x/'),
             (2, 'MATH', '11', 'Calculus', 'MCS', 'CAS', 'https://x/'),
             (3, 'ACTG', '11', 'Accounting', 'ACTG', 'LSB', 'https://x/');
           INSERT INTO majors (id, name, school, source_url) VALUES (1, 'Computer Science', 'CAS', 'https://x/');
           INSERT INTO major_courses VALUES (1, 1), (1, 2);"""
    )
    db.close()
    return application


class User:
    def __init__(self, app, role):
        self.http = app.test_client()
        self.email = f"user{next(counter)}@scu.edu"
        res = self.http.post("/api/auth/signup", json={"email": self.email, "name": "Pat Doe", "password": "long-enough-pw", "role": role})
        assert res.status_code == 201

    def get(self, path):
        return self.http.get(f"/api{path}")

    def post(self, path, body=None):
        return self.http.post(f"/api{path}", json=body or {})

    def put(self, path, body):
        return self.http.put(f"/api{path}", json=body)

    def delete(self, path):
        return self.http.delete(f"/api{path}")


def tutor_with_slot(app, start=24, course_ids=(1,)):
    tutor = User(app, "tutor")
    tutor.put("/tutor/courses", {"courseIds": list(course_ids)})
    res = tutor.post("/tutor/slots", {**span(start), "location": "Library"})
    assert res.status_code == 201
    return tutor, res.json["ids"][0]


def group_body(start=30, **overrides):
    return {"courseId": 1, "topic": "T", "location": "L", "capacity": 3, **span(start), **overrides}


def test_index_and_static_files_are_served(app):
    client = app.test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"/static/app.js" in page.data
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert client.get("/static/app.js").status_code == 200


def test_signup_limited_to_scu_and_hides_hash(app):
    res = app.test_client().post("/api/auth/signup", json={"email": "a@gmail.com", "name": "A", "password": "long-enough-pw", "role": "student"})
    assert res.status_code == 400

    s = User(app, "student")
    me = s.get("/me")
    assert me.json["user"]["email"] == s.email
    assert "password" not in me.text
    cookie = s.post("/auth/logout").headers["Set-Cookie"]
    assert "Max-Age=0" in cookie or "expires=Thu, 01 Jan 1970" in cookie
    assert s.get("/me").json["user"] is None


def test_session_cookie_flags(app):
    client = app.test_client()
    cookie = client.post("/api/auth/signup", json={"email": "flags@scu.edu", "name": "A", "password": "long-enough-pw", "role": "student"}).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_duplicate_signup_rejected(app):
    s = User(app, "student")
    res = app.test_client().post("/api/auth/signup", json={"email": s.email.upper(), "name": "A", "password": "long-enough-pw", "role": "student"})
    assert res.status_code == 409


def signup_as_student(app, email, password="long-enough-pw", client=None):
    client = client or app.test_client()
    return client, client.post("/api/auth/signup", json={"email": email, "name": "Other Name", "password": password, "role": "student", "majorId": 1})


def dual_user(app, start=24):
    """A tutor with a slot who has added student access. Starts in the student view."""
    tutor, slot_id = tutor_with_slot(app, start)
    assert tutor.post("/me/student-account").status_code == 201
    return tutor, slot_id


def test_tutor_signs_up_as_student_with_same_email(app):
    t = User(app, "tutor")
    client, res = signup_as_student(app, t.email.upper())
    assert res.status_code == 201
    assert res.json["user"]["role"] == "student"
    assert res.json["user"]["roles"] == ["tutor", "student"]
    assert res.json["user"]["name"] == "Pat Doe"
    assert res.json["user"]["majorId"] == 1
    assert client.post("/api/groups", json=group_body()).status_code == 201
    assert client.get("/api/tutor/slots").status_code == 403

    _, again = signup_as_student(app, t.email)
    assert again.status_code == 409


def test_adding_student_access_needs_the_tutor_password(app):
    t = User(app, "tutor")
    client, res = signup_as_student(app, t.email, password="wrong-password")
    assert res.status_code == 401
    assert "Set-Cookie" not in res.headers
    assert t.get("/me").json["user"]["roles"] == ["tutor"]
    assert client.get("/api/me").json["user"] is None


def test_student_signup_guessing_shares_the_login_lockout(app):
    t = User(app, "tutor")
    for _ in range(10):
        assert signup_as_student(app, t.email, password="wrong-password")[1].status_code == 401
    assert signup_as_student(app, t.email)[1].status_code == 429
    assert app.test_client().post("/api/auth/login", json={"email": t.email, "password": "long-enough-pw"}).status_code == 429


def test_student_cannot_gain_tutor_access_through_signup(app):
    s = User(app, "student")
    res = app.test_client().post("/api/auth/signup", json={"email": s.email, "name": "A", "password": "long-enough-pw", "role": "tutor"})
    assert res.status_code == 409
    assert s.post("/me/role", {"role": "tutor"}).status_code == 403
    assert s.post("/me/student-account").status_code == 403


def test_switching_views_controls_permissions(app):
    t, _ = dual_user(app)
    assert t.get("/tutor/slots").status_code == 403
    assert t.get("/me/schedule").status_code == 200

    assert t.post("/me/role", {"role": "tutor"}).json["user"]["role"] == "tutor"
    assert t.get("/me").json["user"]["role"] == "tutor"
    assert t.get("/tutor/slots").status_code == 200
    assert t.post("/groups", group_body()).status_code == 403
    assert t.post("/me/role", {"role": "admin"}).status_code == 403


def test_tutor_only_account_cannot_switch_to_student(app):
    t = User(app, "tutor")
    assert t.post("/me/role", {"role": "student"}).status_code == 403
    assert t.post("/groups", group_body()).status_code == 403


def test_login_starts_dual_user_in_tutor_view(app):
    t, _ = dual_user(app)
    res = app.test_client().post("/api/auth/login", json={"email": t.email, "password": "long-enough-pw"})
    assert res.json["user"]["role"] == "tutor"
    assert res.json["user"]["roles"] == ["tutor", "student"]


def test_dual_user_cannot_book_own_slot(app):
    t, slot_id = dual_user(app)
    assert t.get("/availability?courses=1").json["slots"][0]["mine"] is True
    assert t.post(f"/slots/{slot_id}/book", {"courseId": 1}).status_code == 400
    assert User(app, "student").get("/availability?courses=1").json["slots"][0]["mine"] is False


def test_dual_user_schedule_spans_both_roles(app):
    t, _ = dual_user(app, 24)
    assert t.post("/groups", group_body(24)).status_code == 409
    assert t.post("/groups", group_body(30)).status_code == 201

    other_tutor, other_slot = tutor_with_slot(app, 40)
    assert t.post(f"/slots/{other_slot}/book", {"courseId": 1}).status_code == 201

    t.post("/me/role", {"role": "tutor"})
    assert t.post("/tutor/slots", {**span(30), "location": "L"}).status_code == 409
    assert t.post("/tutor/slots", {**span(40), "location": "L"}).status_code == 409
    assert t.post("/tutor/slots", {**span(50), "location": "L"}).status_code == 201


def test_existing_database_gains_new_columns(tmp_path):
    path = str(tmp_path / "old.db")
    old = database.connect(path)
    old.executescript(
        """CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
             password_hash TEXT NOT NULL, role TEXT NOT NULL, major_id INTEGER, bio TEXT NOT NULL DEFAULT '', created_at TEXT);
           CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL);
           INSERT INTO users (email, name, password_hash, role) VALUES ('old@scu.edu', 'Old', 'x', 'tutor');"""
    )
    old.close()
    database.init_database(path)
    database.init_database(path)
    db = database.connect(path)
    assert db.execute("SELECT student_enabled FROM users").fetchone()[0] == 0
    assert db.execute("SELECT active_role FROM sessions").fetchone() is None
    db.close()


def test_login_locks_out_repeated_guessing(app):
    s = User(app, "student")
    client = app.test_client()
    attempt = lambda password: client.post("/api/auth/login", json={"email": s.email, "password": password}).status_code
    assert attempt("long-enough-pw") == 200
    for _ in range(10):
        assert attempt("wrong-password") == 401
    assert attempt("long-enough-pw") == 429


def test_course_search_and_major_filter(app):
    s = User(app, "student")
    codes = lambda query: [f"{c['subject']} {c['number']}" for c in s.get(f"/courses?{query}").json["courses"]]
    assert codes("q=csci%2010") == ["CSCI 10"]
    assert codes("q=calc") == ["MATH 11"]
    assert codes("major=1") == ["CSCI 10", "MATH 11"]
    assert codes("q=%25") == []
    assert s.get("/courses?major=1").json["total"] == 2


def test_roles_enforced(app):
    s = User(app, "student")
    t = User(app, "tutor")
    assert s.post("/tutor/slots", {**span(24), "location": "L"}).status_code == 403
    assert s.put("/tutor/courses", {"courseIds": [1]}).status_code == 403
    assert t.post("/groups", group_body()).status_code == 403
    assert t.post("/slots/1/book", {"courseId": 1}).status_code == 403
    assert app.test_client().post("/api/groups", json=group_body()).status_code == 401


def test_slots_only_for_taught_courses_and_no_email_leak(app):
    _, slot_id = tutor_with_slot(app)
    s = User(app, "student")
    res = s.get("/availability?courses=1,2")
    assert [(x["id"], x["courseIds"]) for x in res.json["slots"]] == [(slot_id, [1])]
    assert "@scu.edu" not in res.text
    assert s.get("/availability?courses=2").json["slots"] == []
    assert s.get("/courses?available=1").json["total"] == 1


def test_slot_booked_once_and_only_for_offered_course(app):
    tutor, slot_id = tutor_with_slot(app)
    a = User(app, "student")
    b = User(app, "student")
    assert a.post(f"/slots/{slot_id}/book", {"courseId": 2}).status_code == 400
    assert a.post(f"/slots/{slot_id}/book", {"courseId": 1, "note": "recursion"}).status_code == 201
    assert b.post(f"/slots/{slot_id}/book", {"courseId": 1}).status_code == 409
    assert b.get("/availability?courses=1").json["slots"] == []
    view = tutor.get("/tutor/slots").json["slots"][0]
    assert view["studentEmail"] == a.email
    assert view["note"] == "recursion"


def test_concurrent_booking_has_one_winner(app):
    _, slot_id = tutor_with_slot(app)
    students = [User(app, "student") for _ in range(8)]
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(lambda s: s.post(f"/slots/{slot_id}/book", {"courseId": 1}).status_code, students))
    assert sorted(statuses) == [201] + [409] * 7


def test_concurrent_joins_respect_capacity(app):
    owner = User(app, "student")
    group_id = owner.post("/groups", group_body(capacity=3)).json["id"]
    students = [User(app, "student") for _ in range(8)]
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(lambda s: s.post(f"/groups/{group_id}/join").status_code, students))
    assert statuses.count(201) == 2


def test_cancelling_booking(app):
    tutor, slot_id = tutor_with_slot(app)
    s = User(app, "student")
    s.post(f"/slots/{slot_id}/book", {"courseId": 1})
    assert s.delete(f"/slots/{slot_id}/book").status_code == 200
    assert len(s.get("/availability?courses=1").json["slots"]) == 1

    s.post(f"/slots/{slot_id}/book", {"courseId": 1})
    assert tutor.delete(f"/tutor/slots/{slot_id}").status_code == 200
    assert s.get("/me/schedule").json["bookings"][0]["cancelledAt"]


def test_cannot_touch_other_peoples_records(app):
    _, slot_id = tutor_with_slot(app)
    assert User(app, "tutor").delete(f"/tutor/slots/{slot_id}").status_code == 404
    owner = User(app, "student")
    group_id = owner.post("/groups", group_body()).json["id"]
    assert User(app, "student").delete(f"/groups/{group_id}").status_code == 404


def test_times_validated_and_overlaps_rejected(app):
    t = User(app, "tutor")
    post = lambda times: t.post("/tutor/slots", {**times, "location": "L"}).status_code
    assert post(span(-2, -1)) == 400
    assert post({"startsAt": "nope", "endsAt": "nope"}) == 400
    assert post({"startsAt": "2026-12-01T10:00:00", "endsAt": "2026-12-01T11:00:00"}) == 400
    assert post(span(24, 24.1)) == 400
    assert post(span(24, 30)) == 400
    assert post(span(24, 26)) == 201
    assert post(span(25, 27)) == 409
    assert post(span(26, 27)) == 201


def test_weekly_repeats_are_all_or_nothing(app):
    t = User(app, "tutor")
    t.post("/tutor/slots", {**span(24 * 8), "location": "L"})
    assert t.post("/tutor/slots", {**span(24), "location": "L", "repeatWeeks": 3}).status_code == 409
    assert len(t.get("/tutor/slots").json["slots"]) == 1
    assert len(t.post("/tutor/slots", {**span(48), "location": "L", "repeatWeeks": 3}).json["ids"]) == 3


def test_weekly_repeat_keeps_campus_clock_across_daylight_saving():
    # 5 PM Pacific on Oct 28, 2026 (PDT); DST ends Nov 1, so a week later is 5 PM PST.
    start = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    assert add_weeks(start, 1) == datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)


def test_study_group_lifecycle(app):
    owner = User(app, "student")
    b = User(app, "student")
    c = User(app, "student")
    group_id = owner.post("/groups", group_body(capacity=2, topic="Midterm")).json["id"]

    listed = c.get("/availability?courses=1")
    assert listed.json["groups"][0]["memberCount"] == 1
    assert "@scu.edu" not in listed.text

    assert b.post(f"/groups/{group_id}/join").status_code == 201
    assert b.post(f"/groups/{group_id}/join").status_code == 409
    assert c.post(f"/groups/{group_id}/join").status_code == 409

    roster = sorted(m["email"] for m in b.get("/me/schedule").json["groups"][0]["members"])
    assert roster == sorted([owner.email, b.email])

    assert b.post("/groups", group_body(30.5, courseId=2)).status_code == 409
    assert owner.delete(f"/groups/{group_id}/join").status_code == 400
    assert b.delete(f"/groups/{group_id}/join").status_code == 200
    assert c.post(f"/groups/{group_id}/join").status_code == 201
    assert owner.delete(f"/groups/{group_id}").status_code == 200
    assert c.get("/me/schedule").json["groups"] == []


def test_booking_blocks_overlapping_group(app):
    _, slot_id = tutor_with_slot(app, 24)
    s = User(app, "student")
    s.post(f"/slots/{slot_id}/book", {"courseId": 1})
    assert s.post("/groups", group_body(24)).status_code == 409


def test_hostile_input_is_data_and_bad_input_is_400(app):
    s = User(app, "student")
    topic = "<img src=x onerror=alert(1)> '; DROP TABLE users; --"
    s.post("/groups", group_body(topic=topic))
    assert s.get("/availability?courses=1").json["groups"][0]["topic"] == topic
    assert s.get("/availability?courses=1;DROP").status_code == 400
    assert s.get("/courses/by-ids?ids=abc").status_code == 400
    assert s.get("/no-such-route").json["error"] == "Not found."
