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
        for table in ("sessions", "messages", "chat_turns", "token_transactions")
    }
    retired = conn.execute("SELECT deleted_at, credits FROM users").fetchall()
    conn.close()

    assert counts == {table: 0 for table in counts}
    # 서비스 데이터는 전부 사라지고, users 행만 표시된 채 남는다.
    assert len(retired) == 1
    assert retired[0]["deleted_at"] is not None
    assert retired[0]["credits"] == 0


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


# 탈퇴는 users 행을 남기고 표시만 한다. 하드 삭제하면 같은 구글 계정으로 다시
# 들어와도 처음 온 사람과 구분되지 않아 기본 지급 이용권을 계속 새로 받는다.
def test_withdrawal_keeps_the_row_and_zeroes_the_credits(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        "google-bye", "bye@example.com", "bye", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    assert user["credits"] == 15

    assert auth.delete_user_account(user["id"]) == "deleted"

    conn = auth._get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["deleted_at"] is not None
    assert row["credits"] == 0
    assert row["paid_credits"] == 0


def test_a_withdrawn_account_cannot_be_reached_by_id(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        "google-bye", "bye@example.com", "bye", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    auth.delete_user_account(user["id"])

    # 남아 있는 JWT 로 계속 쓰지 못해야 한다.
    assert auth.get_user_by_id(user["id"]) is None


def test_re_registering_does_not_hand_out_the_default_grant_again(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    first = auth.get_or_create_user(
        "google-bye", "bye@example.com", "bye", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    auth.delete_user_account(first["id"])

    revived = auth.get_or_create_user("google-bye", "bye@example.com", "bye again", None)

    assert revived["id"] == first["id"]
    assert revived["deleted_at"] is None
    assert revived["credits"] == 0
    assert revived["name"] == "bye again"
    # 반복해도 늘어나지 않는다.
    auth.delete_user_account(revived["id"])
    again = auth.get_or_create_user("google-bye", "bye@example.com", "bye", None)
    assert again["credits"] == 0


def test_a_withdrawn_account_leaves_the_admin_user_list(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    kept = auth.get_or_create_user(
        "google-stay", "stay@example.com", "stay", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    gone = auth.get_or_create_user(
        "google-bye", "bye@example.com", "bye", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    auth.delete_user_account(gone["id"])

    ids = [u["id"] for u in auth.list_all_users()]
    assert ids == [kept["id"]]


# 되살아날 때 그 이용권을 그대로 받게 되므로, 탈퇴 계정은 충전 대상이 아니다.
# 월 충전은 매달 저절로 돌기 때문에 이걸 놓치면 구멍이 자동으로 다시 열린다.
def test_a_withdrawn_account_is_not_refilled_or_bulk_adjusted(tmp_path, monkeypatch):
    from datetime import datetime

    _init_test_db(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        "google-bye", "bye@example.com", "bye", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    auth.delete_user_account(user["id"])

    applied = auth.apply_monthly_credit_refill(
        datetime(2026, 9, 1, 0, 1, tzinfo=auth.KST)
    )
    assert applied["affected_users"] == 0

    auth.admin_bulk_set_credits(50)

    revived = auth.get_or_create_user("google-bye", "bye@example.com", "bye", None)
    assert revived["credits"] == 0
