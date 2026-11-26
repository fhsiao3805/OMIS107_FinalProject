"""Sharing a PDF or image in a tutoring session or study group.

Files go to an in-memory store instead of Cloud Storage; everything else runs through
Flask's test client against the database in TEST_DATABASE_URL.
"""
import io
import itertools
import json
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

    def post_with_file(self, path, body, data=PDF, name="notes.pdf"):
        """Creates something with a file attached, the way the forms send it."""
        form = {"data": json.dumps(body), "file": (io.BytesIO(data), name)}
        return self.client.post(f"/api{path}", data=form, content_type="multipart/form-data")

    def delete(self, path):
        return self.client.delete(f"/api{path}")

    def share(self, path, data=PDF, name="notes.pdf"):
        return self.client.put(f"/api{path}", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


SLOT = {"startsAt": at(24), "endsAt": at(25), "location": "Library"}
GROUP = {"courseId": 1, "topic": "Midterm", "location": "L", "capacity": 4, "startsAt": at(30), "endsAt": at(31)}


def new_tutor(app):
    tutor = Person(app, "tutor")
    tutor.client.put("/api/tutor/courses", json={"courseIds": [1]})
    return tutor


def booked_session(app):
    """A tutor, a student, and the paths of the tutor's file and the student's file in their session."""
    tutor = new_tutor(app)
    slot_id = tutor.post("/tutor/slots", SLOT).json["ids"][0]
    student = Person(app, "student")
    assert student.post(f"/slots/{slot_id}/book", {"courseId": 1}).status_code == 201
    return tutor, student, f"/slots/{slot_id}/attachment", f"/slots/{slot_id}/booking/attachment", slot_id


def study_group(app):
    """A group with its creator and one more member."""
    creator, member = Person(app, "student"), Person(app, "student")
    group_id = creator.post("/groups", GROUP).json["id"]
    assert member.post(f"/groups/{group_id}/join").status_code == 201
    return creator, member, f"/groups/{group_id}/attachment", group_id


def opens(person, path, data=PDF):
    res = person.get(path)
    return res.status_code == 200 and res.data == data


# ---- Attaching a file while creating a time slot, booking or group ----


def test_tutor_attaches_a_file_while_adding_time_slots(app, files):
    tutor = new_tutor(app)
    res = tutor.post_with_file("/tutor/slots", {**SLOT, "repeatWeeks": 3}, PDF, "Practice Exam.pdf")
    assert res.status_code == 201 and res.json["fileError"] is None
    ids = res.json["ids"]
    assert len(ids) == 3
    assert sorted(files.files) == sorted(attachments.slot_key(i) for i in ids)
    assert [s["tutorFileName"] for s in tutor.get("/tutor/slots").json["slots"]] == ["Practice Exam.pdf"] * 3
    assert opens(tutor, f"/slots/{ids[0]}/attachment")


def test_the_tutors_file_is_private_until_a_student_books(app):
    tutor = new_tutor(app)
    slot_id = tutor.post_with_file("/tutor/slots", SLOT).json["ids"][0]
    student = Person(app, "student")
    assert student.get(f"/slots/{slot_id}/attachment").status_code == 404
    assert "tutorFileName" not in student.get("/availability?courses=1").json["slots"][0]

    assert student.post(f"/slots/{slot_id}/book", {"courseId": 1}).status_code == 201
    assert opens(student, f"/slots/{slot_id}/attachment")
    assert student.get("/me/schedule").json["bookings"][0]["tutorFileName"] == "notes.pdf"


def test_student_attaches_a_file_while_booking(app, files):
    tutor = new_tutor(app)
    slot_id = tutor.post("/tutor/slots", SLOT).json["ids"][0]
    student = Person(app, "student")
    res = student.post_with_file(f"/slots/{slot_id}/book", {"courseId": 1, "note": "Ch. 4"}, PNG, "question.png")
    assert res.status_code == 201
    assert student.get("/me/schedule").json["bookings"][0]["studentFileName"] == "question.png"
    slot = tutor.get("/tutor/slots").json["slots"][0]
    assert slot["studentFileName"] == "question.png" and slot["note"] == "Ch. 4"
    assert opens(tutor, f"/slots/{slot_id}/booking/attachment", PNG)
    assert list(files.files) == [attachments.booking_key(1)]


def test_student_attaches_a_file_while_creating_a_study_group(app, files):
    creator = Person(app, "student")
    res = creator.post_with_file("/groups", GROUP, PDF, "Study Guide.pdf")
    assert res.status_code == 201
    group = creator.get("/me/schedule").json["groups"][0]
    assert group["attachmentName"] == "Study Guide.pdf"
    assert list(files.files) == [attachments.group_key(res.json["id"])]


def test_a_bad_file_stops_anything_from_being_created(app, files):
    tutor = new_tutor(app)
    res = tutor.post_with_file("/tutor/slots", SLOT, b"<html></html>", "notes.pdf")
    assert res.status_code == 400
    res = tutor.post_with_file("/tutor/slots", SLOT, PDF + b"0" * attachments.MAX_BYTES)
    assert res.status_code == 413
    assert tutor.get("/tutor/slots").json["slots"] == []

    creator = Person(app, "student")
    assert creator.post_with_file("/groups", GROUP, b"", "empty.pdf").status_code == 400
    assert creator.get("/me/schedule").json["groups"] == []
    assert files.files == {}


def test_creating_without_a_file_still_works_as_a_form(app):
    tutor = new_tutor(app)
    res = tutor.client.post("/api/tutor/slots", data={"data": json.dumps(SLOT)}, content_type="multipart/form-data")
    assert res.status_code == 201
    assert tutor.get("/tutor/slots").json["slots"][0]["tutorFileName"] is None


def test_a_storage_failure_keeps_what_was_created(tmp_path, database_url):
    class BrokenStorage(attachments.MemoryStorage):
        def put(self, key, data, content_type):
            raise OSError("bucket unreachable")

    app = create_app(data_dir=tmp_path, clock=lambda: NOW, database_url=database_url, attachment_storage=BrokenStorage())
    with database.connect(database_url) as db:
        db.execute("INSERT INTO courses (id, subject, number, title, department, school, source_url) VALUES (1, 'OMIS', '107', 'T', 'D', 'S', 'https://x/')")
    creator = Person(app, "student")
    res = creator.post_with_file("/groups", GROUP)
    assert res.status_code == 201
    assert "couldn't be shared" in res.json["fileError"]
    assert creator.get("/me/schedule").json["groups"][0]["attachmentName"] is None


# ---- Tutoring sessions: the tutor's file and the student's file ----


def test_each_side_manages_its_own_file_and_opens_the_others(app, files):
    tutor, student, tutor_file, student_file, _ = booked_session(app)
    assert tutor.share(tutor_file, PDF, "Practice Exam.pdf").json["attachmentName"] == "Practice Exam.pdf"
    assert student.share(student_file, PNG, "my work.png").json["attachmentName"] == "my work.png"

    for person in (tutor, student):
        opened = person.get(tutor_file)
        assert opened.status_code == 200 and opened.data == PDF and opened.mimetype == "application/pdf"
        assert "attachment" in opened.headers["Content-Disposition"]
        assert "no-store" in opened.headers["Cache-Control"]
        assert opens(person, student_file, PNG)

    booking = student.get("/me/schedule").json["bookings"][0]
    assert (booking["tutorFileName"], booking["studentFileName"]) == ("Practice Exam.pdf", "my work.png")
    slot = tutor.get("/tutor/slots").json["slots"][0]
    assert (slot["tutorFileName"], slot["studentFileName"]) == ("Practice Exam.pdf", "my work.png")


def test_neither_side_can_change_the_others_file(app):
    tutor, student, tutor_file, student_file, _ = booked_session(app)
    tutor.share(tutor_file)
    student.share(student_file, PNG)

    assert student.share(tutor_file, PNG).status_code == 403
    assert student.delete(tutor_file).status_code == 403
    assert tutor.share(student_file).status_code == 403
    assert tutor.delete(student_file).status_code == 403
    assert opens(student, tutor_file, PDF) and opens(tutor, student_file, PNG)


def test_tutor_manages_the_file_on_an_open_slot(app, files):
    tutor = new_tutor(app)
    slot_id = tutor.post("/tutor/slots", SLOT).json["ids"][0]
    path = f"/slots/{slot_id}/attachment"
    assert tutor.share(path, PDF, "first.pdf").status_code == 200
    assert tutor.share(path, PNG, "second.png").status_code == 200
    assert opens(tutor, path, PNG)
    assert tutor.delete(path).status_code == 200
    assert files.files == {}
    assert tutor.get(path).status_code == 404
    assert tutor.delete(path).status_code == 404


def test_people_outside_the_session_cannot_open_share_or_remove(app):
    tutor, student, tutor_file, student_file, _ = booked_session(app)
    tutor.share(tutor_file)
    student.share(student_file)
    for outsider in (Person(app, "student"), new_tutor(app)):
        for path in (tutor_file, student_file):
            assert outsider.get(path).status_code == 404
            assert outsider.share(path).status_code == 404
            assert outsider.delete(path).status_code == 404
    assert app.test_client().get(f"/api{tutor_file}").status_code == 401
    assert opens(tutor, student_file) and opens(student, tutor_file)


def test_there_is_no_student_file_before_a_booking(app):
    tutor = new_tutor(app)
    slot_id = tutor.post("/tutor/slots", SLOT).json["ids"][0]
    assert tutor.share(f"/slots/{slot_id}/booking/attachment").status_code == 404


def test_cancelling_a_booking_deletes_only_the_students_file(app, files):
    tutor, student, tutor_file, student_file, slot_id = booked_session(app)
    tutor.share(tutor_file)
    student.share(student_file, PNG)
    assert student.delete(f"/slots/{slot_id}/book").status_code == 200
    assert list(files.files) == [attachments.slot_key(slot_id)]
    assert opens(tutor, tutor_file)
    assert student.get(tutor_file).status_code == 404  # no longer in the session

    # The next student to book sees the tutor's file, not the last student's.
    other = Person(app, "student")
    assert other.post(f"/slots/{slot_id}/book", {"courseId": 1}).status_code == 201
    assert opens(other, tutor_file)
    assert other.get("/me/schedule").json["bookings"][0]["studentFileName"] is None


def test_removing_an_open_slot_deletes_its_file(app, files):
    tutor = new_tutor(app)
    slot_id = tutor.post_with_file("/tutor/slots", SLOT).json["ids"][0]
    assert tutor.delete(f"/tutor/slots/{slot_id}").status_code == 200
    assert files.files == {}


def test_files_stay_when_the_tutor_cancels_a_booked_session(app):
    tutor, student, tutor_file, student_file, slot_id = booked_session(app)
    tutor.share(tutor_file)
    student.share(student_file, PNG)
    assert tutor.delete(f"/tutor/slots/{slot_id}").status_code == 200
    assert opens(student, tutor_file) and opens(student, student_file, PNG)


# ---- Study groups: only the creator manages the file ----


def test_only_the_creator_adds_replaces_or_removes_the_group_file(app, files):
    creator, member, path, group_id = study_group(app)
    assert member.share(path).status_code == 403
    assert files.files == {}

    assert creator.share(path, PDF, "guide.pdf").status_code == 200
    assert member.share(path, PNG).status_code == 403
    assert member.delete(path).status_code == 403
    assert opens(member, path, PDF)
    assert member.get("/me/schedule").json["groups"][0]["attachmentName"] == "guide.pdf"

    assert creator.share(path, PNG, "whiteboard.png").status_code == 200
    assert opens(member, path, PNG)
    assert creator.delete(path).status_code == 200
    assert files.files == {}
    assert member.get(path).status_code == 404


def test_non_members_cannot_open_a_group_file(app):
    creator, member, path, group_id = study_group(app)
    creator.share(path)
    outsider = Person(app, "student")
    assert outsider.get(path).status_code == 404
    assert outsider.share(path).status_code == 404
    assert outsider.delete(path).status_code == 404
    assert "attachmentName" not in outsider.get("/availability?courses=1").json["groups"][0]

    assert member.delete(f"/groups/{group_id}/join").status_code == 200
    assert member.get(path).status_code == 404


def test_cancelling_the_group_deletes_its_file(app, files):
    creator, member, path, group_id = study_group(app)
    creator.share(path)
    assert creator.delete(f"/groups/{group_id}").status_code == 200
    assert files.files == {}


# ---- What can be shared ----


def test_files_over_5_mb_are_rejected(app, files):
    _, student, _, path, _ = booked_session(app)
    too_big = PDF + b"0" * attachments.MAX_BYTES
    res = student.share(path, too_big)
    assert res.status_code == 413
    assert res.json["error"] == "Files can be at most 5 MB."
    assert files.files == {}


def test_a_file_of_exactly_5_mb_is_accepted(app):
    _, student, _, path, _ = booked_session(app)
    exactly = PDF + b"0" * (attachments.MAX_BYTES - len(PDF))
    assert student.share(path, exactly).status_code == 200


@pytest.mark.parametrize("data, name", [
    (b"<html><script>alert(1)</script></html>", "notes.pdf"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "diagram.svg"),
    (b"PK\x03\x04 word document", "essay.docx"),
    (b"MZ\x90\x00 program", "study.exe"),
])
def test_only_pdfs_and_images_are_accepted_whatever_the_name(app, files, data, name):
    _, student, _, path, _ = booked_session(app)
    res = student.share(path, data, name)
    assert res.status_code == 400
    assert res.json["error"] == "Attach a PDF or an image (PNG, JPEG, GIF or WebP)."
    assert files.files == {}


def test_empty_or_missing_file_is_rejected(app):
    _, student, _, path, _ = booked_session(app)
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
    _, student, _, path, _ = booked_session(app)
    assert student.share(path, data, given).json["attachmentName"] == stored


def test_sharing_is_off_when_no_bucket_is_configured(tmp_path, database_url):
    app = create_app(data_dir=tmp_path, clock=lambda: NOW, database_url=database_url)
    with database.connect(database_url) as db:
        db.execute("INSERT INTO courses (id, subject, number, title, department, school, source_url) VALUES (1, 'OMIS', '107', 'T', 'D', 'S', 'https://x/')")
    tutor, student, tutor_file, student_file, slot_id = booked_session(app)
    assert student.share(student_file).status_code == 503
    assert tutor.get(tutor_file).status_code == 503
    assert tutor.post_with_file("/tutor/slots", {**SLOT, "startsAt": at(48), "endsAt": at(49)}).status_code == 503
    assert len(tutor.get("/tutor/slots").json["slots"]) == 1
    assert student.delete(f"/slots/{slot_id}/book").status_code == 200


def test_attachment_name_columns_exist(database_url, app):
    with database.connect(database_url) as db:
        columns = {(r["table_name"], r["column_name"]) for r in db.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE column_name = 'attachment_name'"
        )}
    assert {("tutor_slots", "attachment_name"), ("bookings", "attachment_name"), ("study_groups", "attachment_name")} <= columns
