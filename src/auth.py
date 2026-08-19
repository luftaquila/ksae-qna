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
PRIVACY_CONSENT_VERSION = "2026-08-14"

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
    # 결제 주문 원장.  금액은 서버만 계산하고, 지급·회수는 status 조건부 UPDATE의
    # rowcount로 한 번만 통과시킨다 (src/payments.py).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id      TEXT    NOT NULL UNIQUE,
            -- 탈퇴해도 결제 기록 자체는 남긴다.  대금결제 기록은 보존 의무가 있고,
            -- 사람과의 연결만 끊으면 개인정보는 남지 않는다.
            user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            user_email    TEXT    NOT NULL,
            quantity      INTEGER NOT NULL,
            unit_price    INTEGER NOT NULL,
            amount        INTEGER NOT NULL,
            goods_name    TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'pending',
            method        TEXT,
            tid           TEXT,
            granted       INTEGER,
            reclaimed     INTEGER,
            fail_reason   TEXT,
            cancel_reason TEXT,
            approved_at   TEXT,
            cancelled_at  TEXT,
            raw_auth      TEXT,
            raw_approve   TEXT,
            raw_cancel    TEXT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    # 구매한 이용권은 월 충전의 바닥값에 잡히면 안 된다.  `credits` 는 계속
    # "총 잔액"이고, 그중 구매분이 얼마인지만 여기에 따로 적는다 — 잔액을 읽는
    # 코드는 전부 그대로 두고 충전 기준만 무료분으로 바꾸기 위해서다.
    # 불변식: 0 <= paid_credits <= credits.  무료분 = credits - paid_credits.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN paid_credits INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists

    # New accounts must record the privacy notice accepted after Google OAuth.
    # Existing accounts predate this signup flow and intentionally remain NULL.
    for column, definition in (
        ("privacy_consent_at", "TEXT"),
        ("privacy_consent_version", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments (user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_status_created ON payments (status, created_at)")

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
    # 결제 (src/payments.py).  단가 x 수량으로 금액을 계산한다.
    "credit_unit_price": "100",
    "credit_max_quantity": "1000",
    # 전자상거래법 제10조·제12조 표시사항.  /policy 가 이 값을 그대로 렌더한다.
    # 관리자 화면에서 덮어쓸 수 있고, 빈 값이면 "미등록"으로 표시된다.
    "biz_name": "오병준",
    "biz_owner": "오병준",
    "biz_reg_no": "486-21-02172",
    "biz_mail_order_no": "제2025-대전서구-2265호",
    "biz_address": "대전광역시 유성구 계룡로46번길 61, 204호",
    "biz_tel": "010-9479-3691",
    "biz_email": "mail@luftaquila.io",
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
    """Raise the *free* portion of each balance to *target*.

    Purchased credits are excluded from the comparison and from the result.
    Buying used to push the total past the floor and silently cancel the next
    month's grant — paying must not cost a user their free allowance.
    """
    rows = conn.execute(
        "SELECT id, credits, paid_credits FROM users WHERE credits - paid_credits < ? ORDER BY id",
        (target,),
    ).fetchall()
    total_credits = 0
    for row in rows:
        amount = target - (row["credits"] - row["paid_credits"])
        conn.execute(
            "UPDATE users SET credits = credits + ?, updated_at = datetime('now') WHERE id = ?",
            (amount, row["id"]),
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
    google_id: str,
    email: str,
    name: str,
    picture: str | None,
    privacy_consent_version: str | None = None,
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
        if not privacy_consent_version:
            conn.close()
            raise ValueError("privacy consent is required for a new account")
        default_credits = get_default_credits()
        conn.execute(
            """INSERT INTO users (
                   google_id, email, name, picture, credits,
                   privacy_consent_at, privacy_consent_version
               ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?)""",
            (google_id, email, name, picture, default_credits, privacy_consent_version),
        )
        conn.commit()
        user = dict(
            conn.execute(
                "SELECT * FROM users WHERE google_id = ?", (google_id,)
            ).fetchone()
        )

    conn.close()
    return user


def get_user_by_google_id(google_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE google_id = ?", (google_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_user_account(user_id: int) -> str:
    """Permanently delete an account and all service data owned by it.

    A recently started model response may still try to persist messages, so
    deletion is rejected while it is active. An open payment window is rejected
    for the same reason: the approval would land with no one left to credit.
    Old orphaned pending rows do not block withdrawal forever.

    Payment records themselves survive as anonymous rows — the users row goes
    away and payments.user_id falls to NULL through ON DELETE SET NULL.
    """
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            conn.rollback()
            return "not_found"
        pending = conn.execute(
            """SELECT 1
               FROM chat_turns ct
               JOIN sessions s ON s.id = ct.session_id
               WHERE s.user_id = ?
                 AND ct.status = 'pending'
                 AND ct.created_at >= datetime('now', '-15 minutes')
               LIMIT 1""",
            (user_id,),
        ).fetchone()
        if pending:
            conn.rollback()
            return "pending"

        pending_payment = conn.execute(
            """SELECT 1
               FROM payments
               WHERE user_id = ?
                 AND status = 'pending'
                 AND created_at >= datetime('now', '-15 minutes')
               LIMIT 1""",
            (user_id,),
        ).fetchone()
        if pending_payment:
            conn.rollback()
            return "payment_pending"

        session_ids = "SELECT id FROM sessions WHERE user_id = ?"
        conn.execute(
            f"DELETE FROM chat_turns WHERE session_id IN ({session_ids})",
            (user_id,),
        )
        conn.execute(
            f"DELETE FROM messages WHERE session_id IN ({session_ids})",
            (user_id,),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM token_transactions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return "deleted"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def deduct_credit(user_id: int, amount: int = 1, memo: str = "질문") -> bool:
    """Atomically deduct *amount* credits, spending the free portion first.

    Free credits come back every month and purchased ones never expire, so
    spending free first is what keeps a purchase from evaporating. SQLite
    evaluates every SET expression against the pre-update row, so the free
    portion below is the one that existed before this statement.
    """
    if is_unlimited_credits():
        return True
    conn = _get_conn()
    cur = conn.execute(
        """UPDATE users
              SET credits = credits - ?,
                  paid_credits = MAX(0, paid_credits - MAX(0, ? - (credits - paid_credits))),
                  updated_at = datetime('now')
            WHERE id = ? AND credits >= ?""",
        (amount, amount, user_id, amount),
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
    """Refund credits back to a user (e.g. on LLM error).

    The refund lands in the free portion. Which bucket the failed question
    actually drew from is not tracked per transaction, and compensation for a
    fault on our side belongs in the free bucket rather than being counted as
    something the user bought. The asymmetry favours the user, which is the
    right direction for an error we caused.
    """
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


def get_transactions(
    user_id: int,
    limit: int = 30,
    *,
    public_view: bool = False,
) -> list[dict]:
    """Return recent token transactions, optionally hiding model details."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT amount, type, memo, created_at FROM token_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    transactions = [dict(r) for r in rows]
    if public_view:
        for transaction in transactions:
            if transaction["type"] == "usage":
                transaction["memo"] = "질문"
            elif transaction["type"] == "refund" and transaction.get("memo", "").startswith(
                ("오류 환불 (", "요청 저장 실패 환불 (")
            ):
                transaction["memo"] = transaction["memo"].split(" (", 1)[0]
    return transactions


def get_user_usage_stats(user_id: int) -> dict:
    """Return privacy-safe lifetime usage totals for a user's account page."""
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*)
               FROM sessions
              WHERE user_id = ?) AS conversation_count,
            (SELECT COUNT(*)
               FROM messages m
               JOIN sessions s ON s.id = m.session_id
              WHERE s.user_id = ? AND m.role = 'user') AS question_count,
            (SELECT COALESCE(SUM(ABS(amount)), 0)
               FROM token_transactions
              WHERE user_id = ? AND type = 'usage') AS credits_used,
            (SELECT COALESCE(SUM(amount), 0)
               FROM token_transactions
              WHERE user_id = ? AND type = 'refund' AND amount > 0) AS credits_refunded,
            (SELECT COALESCE(SUM(m.input_tokens), 0)
               FROM messages m
               JOIN sessions s ON s.id = m.session_id
              WHERE s.user_id = ? AND m.role = 'assistant') AS input_tokens,
            (SELECT COALESCE(SUM(m.output_tokens), 0)
               FROM messages m
               JOIN sessions s ON s.id = m.session_id
              WHERE s.user_id = ? AND m.role = 'assistant') AS output_tokens,
            (SELECT COALESCE(SUM(m.thinking_tokens), 0)
               FROM messages m
               JOIN sessions s ON s.id = m.session_id
              WHERE s.user_id = ? AND m.role = 'assistant') AS thinking_tokens
        """,
        (user_id,) * 7,
    ).fetchone()
    conn.close()
    return dict(row)


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


def get_admin_overview_stats(
    days: int | None = 30,
    *,
    low_credit_threshold: int = 5,
) -> dict:
    """Return period-aware service metrics for the admin dashboard."""
    if days is not None and days not in (7, 30):
        raise ValueError("days must be 7, 30, or None")

    period_filter = "" if days is None else " AND {column} >= datetime('now', ?)"
    period_params: tuple[str, ...] = () if days is None else (f"-{days} days",)

    def period_sql(column: str) -> str:
        return period_filter.format(column=column)

    conn = _get_conn()
    users = conn.execute(
        """
        SELECT COUNT(*) AS total_users,
               COALESCE(SUM(credits), 0) AS current_credits,
               COALESCE(SUM(CASE WHEN credits <= ? THEN 1 ELSE 0 END), 0) AS low_credit_users
          FROM users
        """,
        (low_credit_threshold,),
    ).fetchone()
    new_users = conn.execute(
        f"SELECT COUNT(*) AS count FROM users WHERE 1=1{period_sql('created_at')}",
        period_params,
    ).fetchone()["count"]
    active_users = conn.execute(
        f"""
        SELECT COUNT(DISTINCT s.user_id) AS count
          FROM messages m
          JOIN sessions s ON s.id = m.session_id
         WHERE m.role = 'user'{period_sql('m.created_at')}
        """,
        period_params,
    ).fetchone()["count"]
    activity = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN m.role = 'user' THEN 1 ELSE 0 END), 0) AS questions,
               COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN 1 ELSE 0 END), 0) AS answers,
               COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.input_tokens ELSE 0 END), 0) AS input_tokens,
               COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.output_tokens ELSE 0 END), 0) AS output_tokens,
               COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.thinking_tokens ELSE 0 END), 0) AS thinking_tokens
          FROM messages m
          JOIN sessions s ON s.id = m.session_id
         WHERE 1=1{period_sql('m.created_at')}
        """,
        period_params,
    ).fetchone()
    credits = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN type = 'usage' THEN ABS(amount) ELSE 0 END), 0) AS used,
               COALESCE(SUM(CASE WHEN type = 'refund' AND amount > 0 THEN amount ELSE 0 END), 0) AS refunded
          FROM token_transactions
         WHERE 1=1{period_sql('created_at')}
        """,
        period_params,
    ).fetchone()
    turns = conn.execute(
        f"""
        SELECT COUNT(*) AS tracked_turns,
               COALESCE(SUM(CASE WHEN status IN ('success', 'success_fallback') THEN 1 ELSE 0 END), 0) AS successful_turns,
               COALESCE(SUM(CASE WHEN status = 'success_fallback' THEN 1 ELSE 0 END), 0) AS fallback_turns,
               COALESCE(SUM(CASE WHEN status IN ('error', 'partial_error') THEN 1 ELSE 0 END), 0) AS failed_turns,
               COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_turns,
               COALESCE(SUM(CASE WHEN retrieval_status IN ('partial', 'failed') THEN 1 ELSE 0 END), 0) AS degraded_retrieval_turns,
               ROUND(AVG(first_token_ms), 0) AS avg_first_token_ms,
               ROUND(AVG(total_ms), 0) AS avg_total_ms
          FROM chat_turns
         WHERE 1=1{period_sql('created_at')}
        """,
        period_params,
    ).fetchone()
    model_rows = conn.execute(
        f"""
        SELECT m.model,
               COUNT(*) AS message_count,
               COALESCE(SUM(m.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(m.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(m.thinking_tokens), 0) AS thinking_tokens
          FROM messages m
          JOIN sessions s ON s.id = m.session_id
         WHERE m.role = 'assistant'
           AND (m.input_tokens IS NOT NULL OR m.output_tokens IS NOT NULL OR m.thinking_tokens IS NOT NULL)
           {period_sql('m.created_at')}
         GROUP BY m.model
         ORDER BY message_count DESC, m.model
        """,
        period_params,
    ).fetchall()

    daily_days = 14 if days is None else min(days, 14)
    daily_rows = conn.execute(
        """
        SELECT date(m.created_at, '+9 hours') AS date,
               COUNT(*) AS questions,
               COUNT(DISTINCT s.user_id) AS active_users
          FROM messages m
          JOIN sessions s ON s.id = m.session_id
         WHERE m.role = 'user'
           AND m.created_at >= datetime('now', ?)
         GROUP BY date(m.created_at, '+9 hours')
         ORDER BY date
        """,
        (f"-{daily_days - 1} days",),
    ).fetchall()
    conn.close()

    completed_turns = turns["tracked_turns"] - turns["pending_turns"]
    success_rate = (
        round(turns["successful_turns"] * 100 / completed_turns, 1)
        if completed_turns > 0
        else None
    )
    fallback_rate = (
        round(turns["fallback_turns"] * 100 / turns["successful_turns"], 1)
        if turns["successful_turns"] > 0
        else None
    )
    daily_map = {row["date"]: dict(row) for row in daily_rows}
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).date()
    daily = []
    for offset in reversed(range(daily_days)):
        day = (today - timedelta(days=offset)).isoformat()
        daily.append(daily_map.get(day, {"date": day, "questions": 0, "active_users": 0}))

    return {
        "users": {
            **dict(users),
            "new_users": new_users,
            "active_users": active_users,
        },
        "activity": {
            "questions": activity["questions"],
            "answers": activity["answers"],
            "credits_used": credits["used"],
            "credits_refunded": credits["refunded"],
        },
        "reliability": {
            **dict(turns),
            "success_rate": success_rate,
            "fallback_rate": fallback_rate,
        },
        "tokens": {
            "input_tokens": activity["input_tokens"],
            "output_tokens": activity["output_tokens"],
            "thinking_tokens": activity["thinking_tokens"],
            "total_tokens": (
                activity["input_tokens"]
                + activity["output_tokens"]
                + activity["thinking_tokens"]
            ),
        },
        "models": [dict(row) for row in model_rows],
        "daily": daily,
    }


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

    # 총 잔액을 맞추되 paid_credits <= credits 를 깨지 않는다.
    conn.execute(
        """UPDATE users
              SET credits = ?, paid_credits = MIN(paid_credits, ?), updated_at = datetime('now')
            WHERE id = ?""",
        (credits, credits, user_id),
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
            """UPDATE users
                  SET credits = ?, paid_credits = MIN(paid_credits, ?), updated_at = datetime('now')
                WHERE id = ?""",
            (credits, credits, r["id"]),
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
