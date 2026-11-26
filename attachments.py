"""Files attached to tutoring sessions and study groups, kept in a private Cloud Storage bucket.

A tutoring session holds up to two files, the tutor's (on the time slot) and the student's
(on the booking); a study group holds one, managed by the student who created it. The bucket is named by the ATTACHMENTS_BUCKET
setting; without it, file sharing is switched off and the rest of the app works as before.
"""
import os
import threading

MAX_BYTES = 5 * 1024 * 1024

# Recognized by their first bytes, never by the name or type the browser reports.
# SVG is left out on purpose: it can carry scripts.
SIGNATURES = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]
EXTENSIONS = {
    "application/pdf": (".pdf",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/gif": (".gif",),
    "image/webp": (".webp",),
}
MAX_NAME_LENGTH = 150


def detect_type(data):
    """The file's content type if it is a PDF or a supported image, otherwise None."""
    for signature, content_type in SIGNATURES:
        if data.startswith(signature):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_name(filename, content_type):
    """A display name without folders or control characters, ending in an extension that
    matches the content, so a download named "notes.html" can't open as a web page."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    stem, extension = os.path.splitext(name)
    allowed = EXTENSIONS[content_type]
    if extension.lower() not in allowed:
        stem, extension = name, allowed[0]
    stem = stem[:MAX_NAME_LENGTH].strip() or "attachment"
    return stem + extension.lower()


def slot_key(slot_id):
    return f"slots/{slot_id}"


def booking_key(booking_id):
    return f"bookings/{booking_id}"


def group_key(group_id):
    return f"groups/{group_id}"


class CloudStorage:
    """The private bucket. The client is created on first use, so the app starts without credentials."""

    def __init__(self, bucket_name):
        self.bucket_name = bucket_name
        self._bucket = None
        self._lock = threading.Lock()

    def bucket(self):
        with self._lock:
            if self._bucket is None:
                from google.cloud import storage

                self._bucket = storage.Client().bucket(self.bucket_name)
            return self._bucket

    def put(self, key, data, content_type):
        self.bucket().blob(key).upload_from_string(data, content_type=content_type)

    def get(self, key):
        """The file's bytes, or None if it isn't there."""
        from google.api_core.exceptions import NotFound

        try:
            return self.bucket().blob(key).download_as_bytes()
        except NotFound:
            return None

    def delete(self, key):
        from google.api_core.exceptions import NotFound

        try:
            self.bucket().blob(key).delete()
        except NotFound:
            pass


class MemoryStorage:
    """Keeps files in memory. For tests."""

    def __init__(self):
        self.files = {}

    def put(self, key, data, content_type):
        self.files[key] = (data, content_type)

    def get(self, key):
        stored = self.files.get(key)
        return stored[0] if stored else None

    def delete(self, key):
        self.files.pop(key, None)


def storage_from_env():
    """Cloud Storage for the ATTACHMENTS_BUCKET setting, or None when it isn't set."""
    bucket = os.environ.get("ATTACHMENTS_BUCKET", "").strip()
    return CloudStorage(bucket) if bucket else None
