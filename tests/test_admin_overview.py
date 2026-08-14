"""Detailed admin overview aggregation tests."""

from __future__ import annotations

import pytest

from src import auth


def _init_test_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "admin-overview.db"))
    auth.init_db()
    auth.init_site_settings()


def test_admin_overview_combines_period_activity_reliability_and_cost_inputs(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        "google-admin-stats",
        "stats@example.com",
        "stats",
        None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    current_session = auth.create_session(user["id"], "current")
    old_session = auth.create_session(user["id"], "old")
    auth.add_message(current_session["id"], "user", "현재 질문")
    auth.add_message(
        current_session["id"],
        "assistant",
        "현재 답변",
        input_tokens=100,
        output_tokens=30,
        thinking_tokens=10,
        model="gemini-3-flash",
    )
    auth.add_message(old_session["id"], "user", "과거 질문")
    auth.add_message(
        old_session["id"],
        "assistant",
        "과거 답변",
        input_tokens=200,
        output_tokens=60,
        thinking_tokens=20,
        model="gemini-3-pro",
    )
    assert auth.deduct_credit(user["id"])
    assert auth.deduct_credit(user["id"])
    auth.refund_credit(user["id"])

    conn = auth._get_conn()
    conn.execute(
        "UPDATE messages SET created_at = datetime('now', '-10 days') WHERE session_id = ?",
        (old_session["id"],),
    )
    conn.execute(
        """
        INSERT INTO chat_turns (
            id, session_id, query, requested_model, resolved_model,
            retrieval_status, status, first_token_ms, total_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "current-turn",
            current_session["id"],
            "현재 질문",
            "gemini-3-flash",
            "gemini-3-pro",
            "partial",
            "success_fallback",
            850,
            4200,
        ),
    )
    conn.execute(
        """
        INSERT INTO chat_turns (
            id, session_id, query, requested_model, retrieval_status,
            status, first_token_ms, total_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-10 days'))
        """,
        (
            "old-turn",
            old_session["id"],
            "과거 질문",
            "gemini-3-flash",
            "ok",
            "error",
            None,
            900,
        ),
    )
    conn.commit()
    conn.close()

    recent = auth.get_admin_overview_stats(7, low_credit_threshold=5)
    lifetime = auth.get_admin_overview_stats(None, low_credit_threshold=20)

    assert recent["users"] == {
        "total_users": 1,
        "current_credits": 14,
        "low_credit_users": 0,
        "new_users": 1,
        "active_users": 1,
    }
    assert recent["activity"] == {
        "questions": 1,
        "answers": 1,
        "credits_used": 2,
        "credits_refunded": 1,
    }
    assert recent["tokens"] == {
        "input_tokens": 100,
        "output_tokens": 30,
        "thinking_tokens": 10,
        "total_tokens": 140,
    }
    assert recent["reliability"]["success_rate"] == 100.0
    assert recent["reliability"]["fallback_rate"] == 100.0
    assert recent["reliability"]["degraded_retrieval_turns"] == 1
    assert recent["reliability"]["avg_first_token_ms"] == 850.0
    assert recent["models"] == [
        {
            "model": "gemini-3-flash",
            "message_count": 1,
            "input_tokens": 100,
            "output_tokens": 30,
            "thinking_tokens": 10,
        }
    ]
    assert sum(day["questions"] for day in recent["daily"]) == 1
    assert len(recent["daily"]) == 7

    assert lifetime["activity"]["questions"] == 2
    assert lifetime["reliability"]["tracked_turns"] == 2
    assert lifetime["reliability"]["failed_turns"] == 1
    assert lifetime["users"]["low_credit_users"] == 1


def test_admin_overview_rejects_unsupported_period(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="days"):
        auth.get_admin_overview_stats(90)
