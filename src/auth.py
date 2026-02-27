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
ADMIN_EMAILS: set[str] = set()


def init_admin_emails() -> None:
    """Parse ADMIN_EMAILS env var (comma-separated) into a lowercase set."""
    raw = os.environ.get("ADMIN_EMAILS", "")
    ADMIN_EMAILS.clear()
    for email in raw.split(","):
        email = email.strip().lower()
        if email:
            ADMIN_EMAILS.add(email)


def is_admin(request: Request) -> dict | None:
    """Return user dict if the current user is an admin, else None."""
    user = get_current_user(request)
    if not user:
        return None
    if user["email"].lower() in ADMIN_EMAILS:
        return user
    return None


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
            credits     INTEGER NOT NULL DEFAULT 15,
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_transactions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            amount     INTEGER NOT NULL,
            type       TEXT    NOT NULL,
            memo       TEXT,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    # Migrate: add token usage columns to messages
    for col in ("input_tokens", "output_tokens", "thinking_tokens"):
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migrate: add model column to messages
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN model TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migrate: add soft-delete column to sessions
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN deleted_at TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Model settings table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_settings (
            model_key   TEXT PRIMARY KEY,
            enabled     INTEGER NOT NULL DEFAULT 1,
            credits     INTEGER,
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    # Site settings key-value table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS site_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Migrate: add credits column to model_settings
    try:
        conn.execute("ALTER TABLE model_settings ADD COLUMN credits INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Site settings (key-value, in-memory cache)
# ---------------------------------------------------------------------------
_site_settings: dict[str, str] = {}

_SITE_DEFAULTS: dict[str, str] = {
    "default_credits": "15",
    "low_credit_threshold": "5",
}


def init_site_settings() -> None:
    """Load site_settings from DB into in-memory cache, filling defaults."""
    _site_settings.clear()
    _site_settings.update(_SITE_DEFAULTS)
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM site_settings").fetchall()
    conn.close()
    for r in rows:
        _site_settings[r["key"]] = r["value"]


def get_site_setting(key: str) -> str:
    return _site_settings.get(key, _SITE_DEFAULTS.get(key, ""))


def set_site_setting(key: str, value: str) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO site_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()
    _site_settings[key] = value


def get_default_credits() -> int:
    """Return the configured default credits for new users."""
    try:
        return max(0, int(get_site_setting("default_credits")))
    except (ValueError, TypeError):
        return 1


def get_all_site_settings() -> dict[str, str]:
    """Return a copy of all current site settings."""
    return dict(_site_settings)


def get_model_settings_map() -> dict[str, dict]:
    """Return {model_key: {"enabled": bool, "credits": int|None}} for all rows."""
    conn = _get_conn()
    rows = conn.execute("SELECT model_key, enabled, credits FROM model_settings").fetchall()
    conn.close()
    return {r["model_key"]: {"enabled": bool(r["enabled"]), "credits": r["credits"]} for r in rows}


def set_model_settings(model_key: str, enabled: bool, credits: int | None = None) -> None:
    """UPSERT model enabled state and optional custom credits."""
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO model_settings (model_key, enabled, credits, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(model_key) DO UPDATE SET
            enabled = excluded.enabled,
            credits = excluded.credits,
            updated_at = excluded.updated_at
        """,
        (model_key, int(enabled), credits),
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
        default_credits = get_default_credits()
        conn.execute(
            "INSERT INTO users (google_id, email, name, picture, credits) VALUES (?, ?, ?, ?, ?)",
            (google_id, email, name, picture, default_credits),
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


def deduct_credit(user_id: int, amount: int = 1, memo: str = "질문") -> bool:
    """Atomically deduct *amount* credits. Returns True if successful."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE users SET credits = credits - ?, updated_at = datetime('now') WHERE id = ? AND credits >= ?",
        (amount, user_id, amount),
    )
    if cur.rowcount > 0:
        conn.execute(
            "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
            (user_id, -amount, "usage", memo),
        )
    conn.commit()
    success = cur.rowcount > 0
    conn.close()
    return success


def refund_credit(user_id: int, amount: int = 1, memo: str = "환불") -> None:
    """Refund credits back to a user (e.g. on LLM error)."""
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?",
        (amount, user_id),
    )
    conn.execute(
        "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
        (user_id, amount, "refund", memo),
    )
    conn.commit()
    conn.close()


def add_credits(user_id: int, amount: int) -> int | None:
    """Add credits (1-1000). Returns new balance or None if invalid."""
    if not (1 <= amount <= 1000):
        return None
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?",
        (amount, user_id),
    )
    conn.execute(
        "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
        (user_id, amount, "purchase", "크레딧 구매"),
    )
    conn.commit()
    row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row["credits"] if row else None


def get_transactions(user_id: int, limit: int = 30) -> list[dict]:
    """Return recent token transactions for a user."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT amount, type, memo, created_at FROM token_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET environment variable is not set")
    return secret


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
        secure=os.environ.get("HTTPS_ONLY", "").lower() in ("1", "true"),
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
        "SELECT * FROM sessions WHERE user_id = ? AND deleted_at IS NULL ORDER BY updated_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session(session_id: int, user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (session_id, user_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(session_id: int, user_id: int) -> bool:
    """Soft-delete: set deleted_at so the session is hidden from the user but preserved for admin."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE sessions SET deleted_at = datetime('now') WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (session_id, user_id),
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


def add_message(
    session_id: int,
    role: str,
    content: str,
    sources: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    thinking_tokens: int | None = None,
    model: str | None = None,
) -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, sources, input_tokens, output_tokens, thinking_tokens, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, role, content, sources, input_tokens, output_tokens, thinking_tokens, model),
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


# ---------------------------------------------------------------------------
# Admin queries
# ---------------------------------------------------------------------------
def list_all_users() -> list[dict]:
    """Return all users with aggregate API token usage, ordered by created_at DESC."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT u.id, u.google_id, u.email, u.name, u.picture, u.credits,
               u.created_at, u.updated_at,
               COALESCE(SUM(m.input_tokens), 0) AS total_input_tokens,
               COALESCE(SUM(m.output_tokens), 0) AS total_output_tokens,
               COALESCE(SUM(m.thinking_tokens), 0) AS total_thinking_tokens
        FROM users u
        LEFT JOIN sessions s ON s.user_id = u.id
        LEFT JOIN messages m ON m.session_id = s.id AND m.role = 'assistant'
        GROUP BY u.id
        ORDER BY u.created_at DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_token_usage_by_model(user_id: int) -> list[dict]:
    """Return per-model token usage breakdown for a user."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT m.model,
               COALESCE(SUM(m.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(m.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(m.thinking_tokens), 0) AS thinking_tokens,
               COUNT(*) AS message_count
        FROM sessions s
        JOIN messages m ON m.session_id = s.id AND m.role = 'assistant'
        WHERE s.user_id = ? AND m.input_tokens IS NOT NULL
        GROUP BY m.model
        ORDER BY m.model
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def admin_set_credits(user_id: int, credits: int, memo: str = "관리자 조정") -> int | None:
    """Set a user's credits to an absolute value and record the delta as an admin transaction."""
    conn = _get_conn()
    row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return None

    old_credits = row["credits"]
    delta = credits - old_credits

    conn.execute(
        "UPDATE users SET credits = ?, updated_at = datetime('now') WHERE id = ?",
        (credits, user_id),
    )
    conn.execute(
        "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
        (user_id, delta, "admin", memo),
    )
    conn.commit()
    conn.close()
    return credits


def list_all_sessions(user_id: int | None = None) -> list[dict]:
    """Return sessions with user info. Optionally filter by user_id."""
    conn = _get_conn()
    if user_id:
        rows = conn.execute(
            """SELECT s.*, u.email, u.name AS user_name
               FROM sessions s JOIN users u ON s.user_id = u.id
               WHERE s.user_id = ?
               ORDER BY s.updated_at DESC""",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.*, u.email, u.name AS user_name
               FROM sessions s JOIN users u ON s.user_id = u.id
               ORDER BY s.updated_at DESC"""
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def admin_get_messages(session_id: int) -> list[dict]:
    """Get messages for a session without ownership check."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC", (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
