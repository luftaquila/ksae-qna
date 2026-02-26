"""
Authentication, user DB, and credit system for KSAE Q&A chatbot.
"""

import os
import sqlite3
import time

import jwt
from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = os.path.join("data", "users.db")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY = 7 * 24 * 3600  # 7 days
COOKIE_NAME = "token"

# ---------------------------------------------------------------------------
# Module-level resources (initialised once)
# ---------------------------------------------------------------------------
oauth = OAuth()


# ---------------------------------------------------------------------------
# OAuth setup
# ---------------------------------------------------------------------------
def init_oauth() -> None:
    oauth.register(
        name="google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            google_id   TEXT    UNIQUE NOT NULL,
            email       TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            picture     TEXT,
            credits     INTEGER NOT NULL DEFAULT 30,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            title      TEXT    NOT NULL DEFAULT '새 대화',
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role       TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            sources    TEXT,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    conn.close()


def get_or_create_user(
    google_id: str, email: str, name: str, picture: str | None
) -> dict:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE google_id = ?", (google_id,)
    ).fetchone()

    if row:
        conn.execute(
            "UPDATE users SET email=?, name=?, picture=?, updated_at=datetime('now') WHERE google_id=?",
            (email, name, picture, google_id),
        )
        conn.commit()
        user = dict(
            conn.execute(
                "SELECT * FROM users WHERE google_id = ?", (google_id,)
            ).fetchone()
        )
    else:
        conn.execute(
            "INSERT INTO users (google_id, email, name, picture) VALUES (?, ?, ?, ?)",
            (google_id, email, name, picture),
        )
        conn.commit()
        user = dict(
            conn.execute(
                "SELECT * FROM users WHERE google_id = ?", (google_id,)
            ).fetchone()
        )

    conn.close()
    return user


def get_user_by_id(user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def deduct_credit(user_id: int) -> bool:
    """Atomically deduct 1 credit. Returns True if successful."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET credits = credits - 1, updated_at = datetime('now') WHERE id = ? AND credits > 0",
        (user_id,),
    )
    conn.commit()
    success = cur.rowcount > 0
    conn.close()
    return success


def add_credits(user_id: int, amount: int) -> int | None:
    """Add credits (1-1000). Returns new balance or None if invalid."""
    if not (1 <= amount <= 1000):
        return None
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?",
        (amount, user_id),
    )
    conn.commit()
    row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row["credits"] if row else None


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def _jwt_secret() -> str:
    return os.environ.get("JWT_SECRET", "dev")


def create_jwt(user_id: int) -> str:
    return jwt.encode(
        {"sub": str(user_id), "exp": int(time.time()) + JWT_EXPIRY},
        _jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=JWT_EXPIRY,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind HTTPS reverse proxy
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = decode_jwt(token)
    if not payload:
        return None
    return get_user_by_id(int(payload["sub"]))


# ---------------------------------------------------------------------------
# Session / Message CRUD
# ---------------------------------------------------------------------------
def create_session(user_id: int, title: str = "새 대화") -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO sessions (user_id, title) VALUES (?, ?)", (user_id, title)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


def list_sessions(user_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session(session_id: int, user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(session_id: int, user_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def update_session_title(session_id: int, user_id: int, title: str) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE sessions SET title = ?, updated_at = datetime('now') WHERE id = ? AND user_id = ?",
        (title, session_id, user_id),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def add_message(session_id: int, role: str, content: str, sources: str | None = None) -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, sources) VALUES (?, ?, ?, ?)",
        (session_id, role, content, sources),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


def get_messages(session_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC", (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
