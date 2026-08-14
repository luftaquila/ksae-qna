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


def test_public_credit_history_hides_current_and_legacy_model_names(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        "google-history",
        "history@example.com",
        "history",
        None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    assert auth.deduct_credit(user["id"], 1, "질문 (Gemini Pro (Latest))")
    auth.refund_credit(user["id"], 1, "오류 환불 (Gemini Pro (Latest))")

    private_history = auth.get_transactions(user["id"])
    public_history = auth.get_transactions(user["id"], public_view=True)

    assert [row["memo"] for row in private_history] == [
        "오류 환불 (Gemini Pro (Latest))",
        "질문 (Gemini Pro (Latest))",
    ]
    assert [row["memo"] for row in public_history] == ["오류 환불", "질문"]


def test_user_usage_stats_returns_lifetime_totals_without_model_details(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        "google-stats",
        "stats@example.com",
        "stats",
        None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    first_session = auth.create_session(user["id"], "first")
    second_session = auth.create_session(user["id"], "second")
    auth.add_message(first_session["id"], "user", "첫 번째 질문")
    auth.add_message(
        first_session["id"],
        "assistant",
        "첫 번째 답변",
        input_tokens=1200,
        output_tokens=340,
        thinking_tokens=90,
        model="gemini-3-flash-preview",
    )
    auth.add_message(second_session["id"], "user", "두 번째 질문")
    auth.add_message(
        second_session["id"],
        "assistant",
        "두 번째 답변",
        input_tokens=800,
        output_tokens=160,
        thinking_tokens=None,
        model="gemini-3-pro-preview",
    )
    assert auth.deduct_credit(user["id"])
    assert auth.deduct_credit(user["id"])
    auth.refund_credit(user["id"])
    assert auth.delete_session(second_session["id"], user["id"])

    stats = auth.get_user_usage_stats(user["id"])

    assert stats == {
        "conversation_count": 2,
        "question_count": 2,
        "credits_used": 2,
        "credits_refunded": 1,
        "input_tokens": 2000,
        "output_tokens": 500,
        "thinking_tokens": 90,
    }
    assert not any("model" in key or "fallback" in key for key in stats)
