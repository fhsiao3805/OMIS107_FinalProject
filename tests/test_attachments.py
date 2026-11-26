"""Sharing a PDF or image in a tutoring session or study group.

Files go to an in-memory store instead of Cloud Storage; everything else runs through
Flask's test client against the database in TEST_DATABASE_URL.
"""
import io
import itertools
from datetime import datetime, timedelta, timezone

import pytest

import attachments
import db as database
from app import create_app

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
emails = (f"sharer{n}@scu.edu" for n in itertools.count(1))

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj << >> endobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def at(hours):
    return (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def files():
    return attachments.MemoryStorage()


@pytest.fixture
def app(tmp_path, database_url, files):
    application = create_app(data_dir=tmp_path, clock=lambda: NOW, database_url=database_url, attachment_storage=files)
    with database.connect(database_url) as db:
        db.execute(
            """INSERT INTO courses (id, subject, number, title, department, school, source_url)
               VALUES (1, 'OMIS', '107', 'Systems Programming', 'OMIS', 'LSB', 'https://x/')"""
        )
    return application


class Person:
    def __init__(self, app, role):
        self.client = app.test_client()
        self.email = next(emails)
        res = self.client.post("/api/auth/signup", json={"email": self.email, "name": "Pat Doe", "password": "long-enough-pw", "role": role})
        assert res.status_code == 201, res.json

    def get(self, path):
        return self.client.get(f"/api{path}")

    def post(self, path, body=None):
        return self.client.post(f"/api{path}", json=body or {})

    def delete(self, path):
        return self.client.delete(f"/api{path}")

    def share(self, path, data=PDF, name="notes.pdf"):
        return self.client.put(f"/api{path}", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def booked_session(app):
    """A tutor, a student and the slot the student booked."""
    tutor = Person(app, "tutor")
    tutor.client.put("/api/tutor/courses", json={"courseIds": [1]})
    slot_id = tutor.post("/tutor/slots", {"startsAt": at(24), "endsAt": at(25), "location": "Library"}).json["ids"][0]
    student = Person(app, "student")
    assert student.post(f"/slots/{slot_id}/book", {"courseId": 1}).status_code == 201
    return tutor, student, f"/slots/{slot_id}/attachment", slot_id


def study_group(app):
    """A group with its creator and one more member."""
    creator, member = Person(app, "student"), Person(app, "student")
    group_id = creator.post("/groups", {"courseId": 1, "topic": "Midterm", "location": "L", "capacity": 4, "startsAt": at(30), "endsAt": at(31)}).json["id"]
    assert member.post(f"/groups/{group_id}/join").status_code == 201
    return creator, member, f"/groups/{group_id}/attachment", group_id


# ---- Tutoring sessions ----


def test_student_and_tutor_share_a_file_in_their_session(app, files):
    tutor, student, path, slot_id = booked_session(app)
    res = student.share(path, PDF, "Practice Exam.pdf")
    assert res.status_code == 200
    assert res.json["attachmentName"] == "Practice Exam.pdf"

    for person in (student, tutor):
        opened = person.get(path)
        assert opened.status_code == 200
        assert opened.data == PDF
        assert opened.mimetype == "application/pdf"
        assert "attachment" in opened.headers["Content-Disposition"]
        assert "no-store" in opened.headers["Cache-Control"]

    assert student.get("/me/schedule").json["bookings"][0]["attachmentName"] == "Practice Exam.pdf"
    assert tutor.get("/tutor/slots").json["slots"][0]["attachmentName"] == "Practice Exam.pdf"
    assert list(files.files) == [attachments.booking_key(1)]


def test_tutor_can_share_and_replace_the_file(app):
    tutor, student, path, _ = booked_session(app)
    assert tutor.share(path, PDF, "guide.pdf").status_code == 200
    assert student.share(path, PNG, "diagram.png").json["attachmentName"] == "diagram.png"
    opened = tutor.get(path)
    assert opened.data == PNG and opened.mimetype == "image/png"


def test_people_outside_the_session_cannot_open_share_or_remove(app):
    tutor, student, path, _ = booked_session(app)
    student.share(path)
    for outsider in (Person(app, "student"), Person(app, "tutor")):
        assert outsider.get(path).status_code == 404
        assert outsider.share(path).status_code == 404
        assert outsider.delete(path).status_code == 404
    assert app.test_client().get(f"/api{path}").status_code == 401
    assert tutor.get(path).data == PDF


def test_unbooked_slot_has_no_file_to_share(app):
    tutor = Person(app, "tutor")
    tutor.client.put("/api/tutor/courses", json={"courseIds": [1]})
    slot_id = tutor.post("/tutor/slots", {"startsAt": at(24), "endsAt": at(25), "location": "L"}).json["ids"][0]
    assert tutor.share(f"/slots/{slot_id}/attachment").status_code == 404


def test_removing_the_file(app, files):
    tutor, student, path, _ = booked_session(app)
    student.share(path)
    assert tutor.delete(path).status_code == 200
    assert files.files == {}
    assert student.get(path).status_code == 404
    assert student.get("/me/schedule").json["bookings"][0]["attachmentName"] is None
    assert student.delete(path).status_code == 404


def test_cancelling_the_booking_deletes_its_file(app, files):
    tutor, student, path, slot_id = booked_session(app)
    student.share(path)
    assert student.delete(f"/slots/{slot_id}/book").status_code == 200
    assert files.files == {}
    assert tutor.get(path).status_code == 404


def test_file_stays_when_the_tutor_cancels_a_booked_session(app):
    tutor, student, path, slot_id = booked_session(app)
    student.share(path)
    assert tutor.delete(f"/tutor/slots/{slot_id}").status_code == 200
    assert student.get(path).data == PDF


# ---- Study groups ----


def test_group_members_share_a_file(app, files):
    creator, member, path, group_id = study_group(app)
    assert member.share(path, PNG, "whiteboard.png").status_code == 200
    assert creator.get(path).data == PNG
    assert creator.get("/me/schedule").json["groups"][0]["attachmentName"] == "whiteboard.png"
    assert list(files.files) == [attachments.group_key(group_id)]


def test_non_members_cannot_open_a_group_file(app):
    creator, member, path, group_id = study_group(app)
    creator.share(path)
    outsider = Person(app, "student")
    assert outsider.get(path).status_code == 404
    assert outsider.share(path).status_code == 404
    assert "attachmentName" not in outsider.get("/availability?courses=1").json["groups"][0]

    assert member.delete(f"/groups/{group_id}/join").status_code == 200
    assert member.get(path).status_code == 404


def test_cancelling_the_group_deletes_its_file(app, files):
    creator, member, path, group_id = study_group(app)
    member.share(path)
    assert creator.delete(f"/groups/{group_id}").status_code == 200
    assert files.files == {}


# ---- What can be shared ----


def test_files_over_5_mb_are_rejected(app, files):
    _, student, path, _ = booked_session(app)
    too_big = PDF + b"0" * attachments.MAX_BYTES
    res = student.share(path, too_big)
    assert res.status_code == 413
    assert res.json["error"] == "Files can be at most 5 MB."
    assert files.files == {}


def test_a_file_of_exactly_5_mb_is_accepted(app):
    _, student, path, _ = booked_session(app)
    exactly = PDF + b"0" * (attachments.MAX_BYTES - len(PDF))
    assert student.share(path, exactly).status_code == 200


@pytest.mark.parametrize("data, name", [
    (b"<html><script>alert(1)</script></html>", "notes.pdf"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "diagram.svg"),
    (b"PK\x03\x04 word document", "essay.docx"),
    (b"MZ\x90\x00 program", "study.exe"),
])
def test_only_pdfs_and_images_are_accepted_whatever_the_name(app, files, data, name):
    _, student, path, _ = booked_session(app)
    res = student.share(path, data, name)
    assert res.status_code == 400
    assert res.json["error"] == "Attach a PDF or an image (PNG, JPEG, GIF or WebP)."
    assert files.files == {}


def test_empty_or_missing_file_is_rejected(app):
    _, student, path, _ = booked_session(app)
    assert student.share(path, b"").status_code == 400
    assert student.client.put(f"/api{path}", data={}, content_type="multipart/form-data").status_code == 400


@pytest.mark.parametrize("given, data, stored", [
    ("Week 3 notes.pdf", PDF, "Week 3 notes.pdf"),
    ("scan.JPEG", b"\xff\xd8\xff\xe0" + b"\x00" * 16, "scan.jpeg"),
    ("photo.webp", WEBP, "photo.webp"),
    ("page.html", PDF, "page.html.pdf"),          # the extension always matches the content
    ("../../etc/passwd", PDF, "passwd.pdf"),       # folders are dropped
    ("", PNG, "attachment.png"),
])
def test_file_names_are_cleaned(app, given, data, stored):
    _, student, path, _ = booked_session(app)
    assert student.share(path, data, given).json["attachmentName"] == stored


def test_sharing_is_off_when_no_bucket_is_configured(tmp_path, database_url):
    app = create_app(data_dir=tmp_path, clock=lambda: NOW, database_url=database_url)
    with database.connect(database_url) as db:
        db.execute("INSERT INTO courses (id, subject, number, title, department, school, source_url) VALUES (1, 'OMIS', '107', 'T', 'D', 'S', 'https://x/')")
    _, student, path, slot_id = booked_session(app)
    assert student.share(path).status_code == 503
    assert student.get(path).status_code == 503
    assert student.delete(f"/slots/{slot_id}/book").status_code == 200


def test_attachment_name_columns_exist(database_url, app):
    with database.connect(database_url) as db:
        columns = {(r["table_name"], r["column_name"]) for r in db.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE column_name = 'attachment_name'"
        )}
    assert {("bookings", "attachment_name"), ("study_groups", "attachment_name")} <= columns
