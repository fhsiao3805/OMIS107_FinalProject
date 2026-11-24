"""Rules for signing up and for booking 1:1 tutor time slots.

Runs through Flask's test client against the database in TEST_DATABASE_URL
(see conftest.py), with the clock fixed so "future" and "past" never drift.
"""
import itertools
from datetime import datetime, timedelta, timezone

import pytest

import auth
import db as database
from app import create_app

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
PASSWORD = "long-enough-pw"
emails = (f"person{n}@scu.edu" for n in itertools.count(1))


def at(hours):
    """A timestamp `hours` after NOW, as the browser sends it."""
    return (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def app(tmp_path, database_url):
    application = create_app(data_dir=tmp_path, clock=lambda: NOW, database_url=database_url)
    with database.connect(database_url) as db:
        db.execute(
            """INSERT INTO courses (id, subject, number, title, department, school, source_url) VALUES
                 (1, 'OMIS', '107', 'Systems Programming', 'OMIS', 'LSB', 'https://x/'),
                 (2, 'MATH', '11', 'Calculus', 'MCS', 'CAS', 'https://x/')"""
        )
    return application


class Person:
    """One browser: a test client that keeps its own login cookie."""

    def __init__(self, app, role, email=None):
        self.client = app.test_client()
        self.email = email or next(emails)
        res = self.client.post("/api/auth/signup", json={"email": self.email, "name": "Pat Doe", "password": PASSWORD, "role": role})
        assert res.status_code == 201, res.json

    def get(self, path):
        return self.client.get(f"/api{path}")

    def post(self, path, body=None):
        return self.client.post(f"/api{path}", json=body or {})

    def put(self, path, body):
        return self.client.put(f"/api{path}", json=body)

    def delete(self, path):
        return self.client.delete(f"/api{path}")

    def book(self, slot_id, course_id=1):
        return self.post(f"/slots/{slot_id}/book", {"courseId": course_id})

    def cancel(self, slot_id):
        return self.delete(f"/slots/{slot_id}/book")

    def booked_slots(self):
        return [b["slotId"] for b in self.get("/me/schedule").json["bookings"]]


def open_slot(app, starts_in_hours=24, tutor=None):
    """A tutor who teaches course 1 and one open slot `starts_in_hours` from now."""
    tutor = tutor or Person(app, "tutor")
    assert tutor.put("/tutor/courses", {"courseIds": [1]}).status_code == 200
    res = tutor.post("/tutor/slots", {"startsAt": at(starts_in_hours), "endsAt": at(starts_in_hours + 1), "location": "Library"})
    assert res.status_code == 201, res.json
    return tutor, res.json["ids"][0]


def signup(app, email, role="student"):
    return app.test_client().post("/api/auth/signup", json={"email": email, "name": "A", "password": PASSWORD, "role": role})


def login(client, email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# ---- SCU email only ----


@pytest.mark.parametrize("email", [
    "pat@gmail.com",
    "pat@scu.edu.evil.com",   # scu.edu inside a different domain
    "pat@notscu.edu",
    "pat@mail.scu.edu",       # subdomains are not accepted either
    "pat@scu.com",
    "pat@scu",
    "scu.edu",
    "@scu.edu",
    "pat @scu.edu",
    "",
])
@pytest.mark.parametrize("role", ["student", "tutor"])
def test_signup_without_scu_email_is_rejected(app, email, role):
    res = signup(app, email, role)
    assert res.status_code == 400
    assert "Set-Cookie" not in res.headers
    with database.connect(app.config["DATABASE_URL"]) as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_signup_with_scu_email_is_accepted_in_any_case(app):
    res = signup(app, "  Pat.Doe@SCU.EDU  ")
    assert res.status_code == 201
    assert res.json["user"]["email"] == "pat.doe@scu.edu"


def test_login_without_scu_email_is_rejected(app):
    client = app.test_client()
    res = login(client, "pat@gmail.com")
    assert res.status_code == 401
    assert "Set-Cookie" not in res.headers
    assert client.get("/api/me").json["user"] is None


@pytest.mark.xfail(strict=True, reason="Login doesn't check the email domain; only signup does. app.py is unchanged.")
def test_login_rejects_non_scu_account_that_reached_the_database(app):
    """An account can only get a non-SCU email if it was added outside the app (for example by hand in SQL)."""
    with database.connect(app.config["DATABASE_URL"]) as db:
        db.execute(
            "INSERT INTO users (email, name, password_hash, role) VALUES (%s, %s, %s, %s)",
            ("pat@gmail.com", "Pat", auth.hash_password(PASSWORD), "student"),
        )
    assert login(app.test_client(), "pat@gmail.com").status_code == 401


def test_login_with_scu_email_and_right_password_works(app):
    person = Person(app, "student")
    client = app.test_client()
    assert login(client, person.email, "wrong-password").status_code == 401
    assert login(client, person.email).status_code == 200
    assert client.get("/api/me").json["user"]["email"] == person.email


# ---- Booking a free slot ----


def test_logged_in_student_can_book_a_free_slot(app):
    tutor, slot_id = open_slot(app)
    student = Person(app, "student")
    assert [s["id"] for s in student.get("/availability?courses=1").json["slots"]] == [slot_id]

    res = student.book(slot_id)
    assert res.status_code == 201
    assert student.booked_slots() == [slot_id]
    assert tutor.get("/tutor/slots").json["slots"][0]["studentEmail"] == student.email
    assert student.get("/availability?courses=1").json["slots"] == []


def test_logged_out_visitor_cannot_book(app):
    _, slot_id = open_slot(app)
    student = Person(app, "student")
    student.post("/auth/logout")
    assert student.book(slot_id).status_code == 401
    assert app.test_client().post(f"/api/slots/{slot_id}/book", json={"courseId": 1}).status_code == 401


def test_tutor_view_cannot_book(app):
    _, slot_id = open_slot(app)
    other_tutor = Person(app, "tutor")
    assert other_tutor.book(slot_id).status_code == 403


def test_booking_needs_a_course_the_tutor_teaches(app):
    _, slot_id = open_slot(app)
    student = Person(app, "student")
    assert student.book(slot_id, course_id=2).status_code == 400
    assert student.post(f"/slots/{slot_id}/book", {}).status_code == 400
    assert student.booked_slots() == []


def test_slot_that_does_not_exist_cannot_be_booked(app):
    assert Person(app, "student").book(999).status_code == 404


def test_slot_in_the_past_cannot_be_booked(app):
    _, slot_id = open_slot(app)
    with database.connect(app.config["DATABASE_URL"]) as db:
        db.execute("UPDATE tutor_slots SET starts_at = %s, ends_at = %s WHERE id = %s", (at(-2), at(-1), slot_id))
    assert Person(app, "student").book(slot_id).status_code == 404


def test_slot_the_tutor_removed_cannot_be_booked(app):
    tutor, slot_id = open_slot(app)
    assert tutor.delete(f"/tutor/slots/{slot_id}").status_code == 200
    assert Person(app, "student").book(slot_id).status_code == 404


def test_student_cannot_book_two_slots_at_the_same_time(app):
    _, first = open_slot(app, 24)
    _, overlapping = open_slot(app, 24.5)
    student = Person(app, "student")
    assert student.book(first).status_code == 201
    assert student.book(overlapping).status_code == 409
    assert student.booked_slots() == [first]


# ---- A booked 1:1 slot can't be booked again ----


def test_slot_booked_by_another_student_cannot_be_booked(app):
    _, slot_id = open_slot(app)
    first, second = Person(app, "student"), Person(app, "student")
    assert first.book(slot_id).status_code == 201

    assert second.book(slot_id).status_code == 409
    assert second.booked_slots() == []
    assert first.booked_slots() == [slot_id]
    with database.connect(app.config["DATABASE_URL"]) as db:
        assert db.execute("SELECT COUNT(*) FROM bookings WHERE slot_id = %s", (slot_id,)).fetchone()[0] == 1


def test_student_cannot_book_the_same_slot_twice(app):
    _, slot_id = open_slot(app)
    student = Person(app, "student")
    assert student.book(slot_id).status_code == 201
    assert student.book(slot_id).status_code == 409
    assert student.booked_slots() == [slot_id]


def test_booked_slot_disappears_for_other_students(app):
    _, slot_id = open_slot(app)
    Person(app, "student").book(slot_id)
    other = Person(app, "student")
    assert other.get("/availability?courses=1").json["slots"] == []
    assert other.get("/courses?available=1").json["total"] == 0


# ---- Cancelling ----


def test_student_can_cancel_their_own_booking(app):
    tutor, slot_id = open_slot(app)
    student = Person(app, "student")
    student.book(slot_id)

    assert student.cancel(slot_id).status_code == 200
    assert student.booked_slots() == []
    assert tutor.get("/tutor/slots").json["slots"][0]["studentEmail"] is None
    assert student.cancel(slot_id).status_code == 404


def test_cancelled_slot_can_be_booked_again(app):
    _, slot_id = open_slot(app)
    first, second = Person(app, "student"), Person(app, "student")
    first.book(slot_id)
    first.cancel(slot_id)
    assert second.book(slot_id).status_code == 201
    assert second.booked_slots() == [slot_id]


def test_student_cannot_cancel_another_students_booking(app):
    _, slot_id = open_slot(app)
    owner, other = Person(app, "student"), Person(app, "student")
    owner.book(slot_id)

    assert other.cancel(slot_id).status_code == 404
    assert owner.booked_slots() == [slot_id]


def test_logged_out_visitor_cannot_cancel_a_booking(app):
    _, slot_id = open_slot(app)
    owner = Person(app, "student")
    owner.book(slot_id)
    assert app.test_client().delete(f"/api/slots/{slot_id}/book").status_code == 401
    assert owner.booked_slots() == [slot_id]


def test_tutor_cannot_cancel_a_students_booking_through_the_student_route(app):
    tutor, slot_id = open_slot(app)
    owner = Person(app, "student")
    owner.book(slot_id)
    assert tutor.cancel(slot_id).status_code == 403
    assert owner.booked_slots() == [slot_id]


def test_tutor_removing_a_booked_slot_keeps_it_on_the_students_schedule_as_cancelled(app):
    tutor, slot_id = open_slot(app)
    student = Person(app, "student")
    student.book(slot_id)
    assert tutor.delete(f"/tutor/slots/{slot_id}").status_code == 200
    booking = student.get("/me/schedule").json["bookings"][0]
    assert booking["slotId"] == slot_id and booking["cancelledAt"]
