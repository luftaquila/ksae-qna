"""Signup privacy consent and account deletion regression tests."""

from __future__ import annotations

import pytest

from src import auth


def _init_test_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "account.db"))
    auth.init_db()
    auth.init_site_settings()


def test_new_user_requires_and_records_privacy_consent(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="privacy consent"):
        auth.get_or_create_user("google-new", "new@example.com", "new", None)

    user = auth.get_or_create_user(
        "google-new",
        "new@example.com",
        "new",
        None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )

    assert user["privacy_consent_at"] is not None
    assert user["privacy_consent_version"] == auth.PRIVACY_CONSENT_VERSION


def test_existing_user_can_log_in_without_retroactive_signup_consent(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conn = auth._get_conn()
    conn.execute(
        "INSERT INTO users (google_id, email, name) VALUES (?, ?, ?)",
        ("google-existing", "old@example.com", "old"),
    )
    conn.commit()
    conn.close()

    user = auth.get_or_create_user(
        "google-existing",
        "updated@example.com",
        "updated",
        None,
    )

    assert user["email"] == "updated@example.com"
    assert user["privacy_consent_at"] is None


def test_account_deletion_waits_for_pending_turn_then_removes_all_user_data(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        "google-delete",
        "delete@example.com",
        "delete",
        None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    session = auth.create_session(user["id"], "delete test")
    message = auth.add_message(session["id"], "user", "질문", turn_id="pending-turn")
    auth.create_chat_turn(
        "pending-turn",
        session["id"],
        message["id"],
        "질문",
        "gemini-3-pro",
    )
    auth.add_credits(user["id"], 1)

    assert auth.delete_user_account(user["id"]) == "pending"
    assert auth.get_user_by_id(user["id"]) is not None

    conn = auth._get_conn()
    conn.execute("UPDATE chat_turns SET status = 'success' WHERE id = 'pending-turn'")
    conn.commit()
    conn.close()

    assert auth.delete_user_account(user["id"]) == "deleted"
    assert auth.get_user_by_id(user["id"]) is None

    conn = auth._get_conn()
    counts = {
        table: conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
        for table in ("users", "sessions", "messages", "chat_turns", "token_transactions")
    }
    conn.close()
    assert counts == {table: 0 for table in counts}
