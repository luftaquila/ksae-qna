"""
Authentication, user DB, and credit system for KSAE Q&A chatbot.
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

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
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
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

    # Migrate: add rewritten_query column to messages
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN rewritten_query TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Stable request/response correlation.  Older rows intentionally remain
    # NULL; new chat requests always populate this field.
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT")
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

    # One row per chat request.  Message ordering alone is not a reliable
    # request/response key when a session is used from multiple tabs, and it
    # cannot preserve provider failures that produce no answer text.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_turns (
            id                  TEXT PRIMARY KEY,
            session_id          INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            user_message_id     INTEGER REFERENCES messages(id),
            assistant_message_id INTEGER REFERENCES messages(id),
            query               TEXT NOT NULL,
            requested_model     TEXT NOT NULL,
            resolved_model      TEXT,
            resolved_model_id   TEXT,
            rewritten_query     TEXT,
            collections         TEXT,
            category            TEXT,
            competition         TEXT,
            confidence          TEXT,
            source_ids          TEXT,
            retrieval_status    TEXT NOT NULL DEFAULT 'pending',
            status              TEXT NOT NULL DEFAULT 'pending',
            error_provider      TEXT,
            error_code          TEXT,
            error_message       TEXT,
            finish_reason       TEXT,
            prompt_version      TEXT,
            input_tokens        INTEGER,
            output_tokens       INTEGER,
            thinking_tokens     INTEGER,
            retrieval_ms        INTEGER,
            rerank_ms           INTEGER,
            first_token_ms      INTEGER,
            generation_ms       INTEGER,
            total_ms            INTEGER,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at        TEXT
        )
        """
    )
    try:
        conn.execute("ALTER TABLE chat_turns ADD COLUMN resolved_model_id TEXT")
    except sqlite3.OperationalError:
        pass

    # Site settings key-value table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS site_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Idempotency ledger for the automatic monthly credit refill.  One row
    # per KST calendar month prevents duplicate grants after restarts.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monthly_credit_refills (
            period          TEXT PRIMARY KEY,
            target_credits  INTEGER NOT NULL,
            affected_users INTEGER NOT NULL,
            total_credits   INTEGER NOT NULL,
            completed_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    # Migrate: add credits column to model_settings
    try:
        conn.execute("ALTER TABLE model_settings ADD COLUMN credits INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migrate: add display_order column to model_settings
    try:
        conn.execute("ALTER TABLE model_settings ADD COLUMN display_order INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Index for efficient recent messages query
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages (session_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_turn_id ON messages (turn_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_session_created ON chat_turns (session_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_status_created ON chat_turns (status, created_at)")

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Site settings (key-value, in-memory cache)
# ---------------------------------------------------------------------------
_site_settings: dict[str, str] = {}

_SITE_DEFAULTS: dict[str, str] = {
    "default_credits": "15",
    "monthly_refill_credits": "20",
    "low_credit_threshold": "5",
    "unlimited_credits": "false",
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


def get_monthly_refill_credits() -> int:
    """Return the credit floor applied on the first day of each KST month."""
    try:
        return max(0, int(get_site_setting("monthly_refill_credits")))
    except (ValueError, TypeError):
        return 20


KST = timezone(timedelta(hours=9))


def _refill_users_to_floor(
    conn: sqlite3.Connection,
    target: int,
    transaction_type: str,
    memo: str,
) -> tuple[int, int]:
    """Raise balances below *target* using the caller's open transaction."""
    rows = conn.execute(
        "SELECT id, credits FROM users WHERE credits < ? ORDER BY id",
        (target,),
    ).fetchall()
    total_credits = 0
    for row in rows:
        amount = target - row["credits"]
        conn.execute(
            "UPDATE users SET credits = ?, updated_at = datetime('now') WHERE id = ?",
            (target, row["id"]),
        )
        conn.execute(
            "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
            (row["id"], amount, transaction_type, memo),
        )
        total_credits += amount
    return len(rows), total_credits


def apply_monthly_credit_refill(now: datetime | None = None) -> dict:
    """Raise balances below the configured floor once on each KST month start.

    The monthly ledger and ``BEGIN IMMEDIATE`` make this safe across restarts
    and overlapping workers.  Calls outside the first calendar day are a
    no-op, so deploying the feature mid-month never grants credits early.
    """
    if now is None:
        kst_now = datetime.now(KST)
    elif now.tzinfo is None:
        kst_now = now.replace(tzinfo=KST)
    else:
        kst_now = now.astimezone(KST)

    period = kst_now.strftime("%Y-%m")
    target = get_monthly_refill_credits()
    if kst_now.day != 1:
        return {
            "applied": False,
            "reason": "not_due",
            "period": period,
            "target_credits": target,
            "affected_users": 0,
            "total_credits": 0,
        }

    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT target_credits, affected_users, total_credits
            FROM monthly_credit_refills
            WHERE period = ?
            """,
            (period,),
        ).fetchone()
        if existing:
            conn.rollback()
            return {
                "applied": False,
                "reason": "already_applied",
                "period": period,
                "target_credits": existing["target_credits"],
                "affected_users": existing["affected_users"],
                "total_credits": existing["total_credits"],
            }

        affected_users, total_credits = _refill_users_to_floor(
            conn,
            target,
            "monthly_refill",
            f"월 기본 이용권 충전 ({period})",
        )

        conn.execute(
            """
            INSERT INTO monthly_credit_refills
                (period, target_credits, affected_users, total_credits)
            VALUES (?, ?, ?, ?)
            """,
            (period, target, affected_users, total_credits),
        )
        conn.commit()
        return {
            "applied": True,
            "reason": "applied",
            "period": period,
            "target_credits": target,
            "affected_users": affected_users,
            "total_credits": total_credits,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def admin_refill_credits_to_floor(target: int) -> dict:
    """Immediately raise all balances below *target* and record each grant."""
    target = max(0, int(target))
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        affected_users, total_credits = _refill_users_to_floor(
            conn,
            target,
            "admin_refill",
            "관리자 즉시 기본 이용권 충전",
        )
        conn.commit()
        return {
            "target_credits": target,
            "affected_users": affected_users,
            "total_credits": total_credits,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def is_unlimited_credits() -> bool:
    """Return True if the site-wide unlimited credits mode is enabled."""
    return get_site_setting("unlimited_credits").lower() in ("1", "true")


def get_all_site_settings() -> dict[str, str]:
    """Return a copy of all current site settings."""
    return dict(_site_settings)


def get_model_settings_map() -> dict[str, dict]:
    """Return {model_key: {"enabled": bool, "credits": int|None, "display_order": int|None}} for all rows."""
    conn = _get_conn()
    rows = conn.execute("SELECT model_key, enabled, credits, display_order FROM model_settings").fetchall()
    conn.close()
    return {r["model_key"]: {"enabled": bool(r["enabled"]), "credits": r["credits"], "display_order": r["display_order"]} for r in rows}


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


def set_model_order(order: list[str]) -> None:
    """Update display_order for all models in the given order list."""
    conn = _get_conn()
    for idx, model_key in enumerate(order):
        conn.execute(
            """
            INSERT INTO model_settings (model_key, enabled, display_order, updated_at)
            VALUES (?, 1, ?, datetime('now'))
            ON CONFLICT(model_key) DO UPDATE SET
                display_order = excluded.display_order,
                updated_at = excluded.updated_at
            """,
            (model_key, idx),
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
    if is_unlimited_credits():
        return True
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
    if is_unlimited_credits():
        return
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
        (user_id, amount, "purchase", "이용권 구매"),
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
    rewritten_query: str | None = None,
    turn_id: str | None = None,
) -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, sources, input_tokens, output_tokens, thinking_tokens, model, rewritten_query, turn_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, role, content, sources, input_tokens, output_tokens, thinking_tokens, model, rewritten_query, turn_id),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


def create_chat_turn(
    turn_id: str,
    session_id: int,
    user_message_id: int,
    query: str,
    requested_model: str,
    collections: str | None = None,
    category: str | None = None,
    competition: str | None = None,
    confidence: str | None = None,
    prompt_version: str | None = None,
) -> dict:
    """Create an observable chat turn after its user message is persisted."""
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO chat_turns (
            id, session_id, user_message_id, query, requested_model,
            collections, category, competition, confidence, prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            turn_id,
            session_id,
            user_message_id,
            query,
            requested_model,
            collections,
            category,
            competition,
            confidence,
            prompt_version,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
    conn.close()
    return dict(row)


def complete_chat_turn(
    turn_id: str,
    *,
    assistant_message_id: int | None,
    resolved_model: str | None,
    resolved_model_id: str | None,
    rewritten_query: str | None,
    competition: str | None,
    source_ids: str | None,
    retrieval_status: str,
    status: str,
    error_provider: str | None,
    error_code: str | None,
    error_message: str | None,
    finish_reason: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    thinking_tokens: int | None,
    retrieval_ms: int | None,
    rerank_ms: int | None,
    first_token_ms: int | None,
    generation_ms: int | None,
    total_ms: int | None,
) -> None:
    """Finalize all observable fields for a chat turn atomically."""
    conn = _get_conn()
    conn.execute(
        """
        UPDATE chat_turns SET
            assistant_message_id = ?, resolved_model = ?, resolved_model_id = ?, rewritten_query = ?,
            competition = COALESCE(?, competition), source_ids = ?,
            retrieval_status = ?, status = ?, error_provider = ?,
            error_code = ?, error_message = ?, finish_reason = ?,
            input_tokens = ?, output_tokens = ?, thinking_tokens = ?,
            retrieval_ms = ?, rerank_ms = ?, first_token_ms = ?,
            generation_ms = ?, total_ms = ?, completed_at = datetime('now')
        WHERE id = ?
        """,
        (
            assistant_message_id,
            resolved_model,
            resolved_model_id,
            rewritten_query,
            competition,
            source_ids,
            retrieval_status,
            status,
            error_provider,
            error_code,
            error_message,
            finish_reason,
            input_tokens,
            output_tokens,
            thinking_tokens,
            retrieval_ms,
            rerank_ms,
            first_token_ms,
            generation_ms,
            total_ms,
            turn_id,
        ),
    )
    conn.commit()
    conn.close()


def check_db_health() -> bool:
    """Return whether the SQLite database accepts a simple read query."""
    try:
        conn = _get_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return True
    except sqlite3.Error:
        return False


def list_chat_turns(limit: int = 100, status: str | None = None) -> list[dict]:
    """Return recent turn diagnostics for authenticated admin tooling."""
    limit = max(1, min(limit, 500))
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM chat_turns WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chat_turns ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_messages(session_id: int) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC", (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_messages(session_id: int, limit: int = 10) -> list[dict]:
    """Return the most recent *limit* messages for a session, ordered ASC."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


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
               COALESCE(SUM(m.thinking_tokens), 0) AS total_thinking_tokens,
               MAX(m.created_at) AS last_active_at
        FROM users u
        LEFT JOIN sessions s ON s.user_id = u.id
        LEFT JOIN messages m ON m.session_id = s.id AND m.role = 'assistant'
        GROUP BY u.id
        ORDER BY u.created_at DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_users_token_usage_by_model() -> dict[int, list[dict]]:
    """Return per-model token usage for all users, keyed by user_id."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT s.user_id,
               m.model,
               COALESCE(SUM(m.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(m.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(m.thinking_tokens), 0) AS thinking_tokens,
               COUNT(*) AS message_count
        FROM sessions s
        JOIN messages m ON m.session_id = s.id AND m.role = 'assistant'
        WHERE m.input_tokens IS NOT NULL
        GROUP BY s.user_id, m.model
        ORDER BY s.user_id, m.model
        """
    ).fetchall()
    conn.close()
    result: dict[int, list[dict]] = {}
    for r in rows:
        uid = r["user_id"]
        if uid not in result:
            result[uid] = []
        result[uid].append({
            "model": r["model"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "thinking_tokens": r["thinking_tokens"],
            "message_count": r["message_count"],
        })
    return result


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

    if delta == 0:
        conn.close()
        return credits

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


def admin_bulk_set_credits(credits: int, memo: str = "관리자 일괄 조정") -> int:
    """Set all users' credits to an absolute value and record the delta for each. Returns affected count."""
    conn = _get_conn()
    rows = conn.execute("SELECT id, credits FROM users").fetchall()
    affected = 0
    for r in rows:
        delta = credits - r["credits"]
        if delta == 0:
            continue
        conn.execute(
            "UPDATE users SET credits = ?, updated_at = datetime('now') WHERE id = ?",
            (credits, r["id"]),
        )
        conn.execute(
            "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
            (r["id"], delta, "admin", memo),
        )
        affected += 1
    conn.commit()
    conn.close()
    return affected


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
