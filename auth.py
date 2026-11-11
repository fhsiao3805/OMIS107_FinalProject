"""Password checks, login sessions and brute-force limiting."""
import hashlib
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

SESSION_COOKIE = "studyhub_session"
SESSION_TTL_SECONDS = 14 * 86_400


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def hash_password(password):
    return generate_password_hash(password, method="scrypt")


verify_password = check_password_hash

# Verified against when the email is unknown, so response time doesn't reveal which emails exist.
DUMMY_HASH = hash_password(secrets.token_hex(16))


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db, user_id, now, active_role=None):
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at, active_role) VALUES (?, ?, ?, ?)",
        (_hash_token(token), user_id, now + SESSION_TTL_SECONDS, active_role),
    )
    return token


def destroy_session(db, token):
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def set_active_role(db, token, role):
    db.execute("UPDATE sessions SET active_role = ? WHERE token_hash = ?", (role, _hash_token(token)))


def load_user(db, user_id, active_role=None):
    """The user as the API sees them: `role` is the current view, `roles` is every view they can use."""
    user = dict(db.execute(
        "SELECT id, email, name, role, major_id AS majorId, bio, student_enabled FROM users WHERE id = ?", (user_id,)
    ).fetchone())
    roles = ["tutor", "student"] if user.pop("student_enabled") else [user["role"]]
    return {**user, "roles": roles, "role": active_role or user["role"]}


def user_for_token(db, token, now):
    if not token:
        return None
    session = db.execute(
        "SELECT user_id, active_role FROM sessions WHERE token_hash = ? AND expires_at > ?",
        (_hash_token(token), now),
    ).fetchone()
    return load_user(db, session["user_id"], session["active_role"]) if session else None


class AttemptLimiter:
    """Fixed-window limiter for password guessing. In memory, which suits a single server process."""

    def __init__(self, max_attempts=10, window_seconds=15 * 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.attempts = {}
        self.lock = threading.Lock()

    def check(self, key, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self.attempts = {k: v for k, v in self.attempts.items() if v["reset_at"] > now}
            entry = self.attempts.get(key)
            if entry and entry["count"] >= self.max_attempts:
                raise HttpError(429, "Too many attempts. Try again in a few minutes.")

    def fail(self, key, now=None):
        now = time.time() if now is None else now
        with self.lock:
            entry = self.attempts.get(key)
            if entry and entry["reset_at"] > now:
                entry["count"] += 1
            else:
                self.attempts[key] = {"count": 1, "reset_at": now + self.window_seconds}

    def reset(self, key):
        with self.lock:
            self.attempts.pop(key, None)
