"""Chat-turn persistence and migration tests."""

from __future__ import annotations

import json

from src import auth


def test_turn_correlates_messages_and_persists_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "auth.db"))
    auth.init_db()

    conn = auth._get_conn()
    user_id = conn.execute(
        "INSERT INTO users (google_id, email, name) VALUES (?, ?, ?)",
        ("g-1", "test@example.com", "tester"),
    ).lastrowid
    conn.commit()
    conn.close()

    session = auth.create_session(user_id, "test")
    user_message = auth.add_message(session["id"], "user", "질문", turn_id="turn-1")
    auth.create_chat_turn(
        "turn-1",
        session["id"],
        user_message["id"],
        "질문",
        "gemini-3-pro",
        prompt_version="test-v1",
    )
    assistant = auth.add_message(
        session["id"], "assistant", "모델 오류", model="gemini-3-pro", turn_id="turn-1"
    )
    auth.complete_chat_turn(
        "turn-1",
        assistant_message_id=assistant["id"],
        resolved_model="gemini-3-pro",
        resolved_model_id="gemini-3.1-pro-preview",
        attempted_models=None,
        rewritten_query=None,
        competition="formula",
        source_ids="[]",
        retrieval_status="ok",
        status="error",
        error_provider="gemini",
        error_code="model_not_found",
        error_message="retired model",
        finish_reason=None,
        input_tokens=10,
        output_tokens=0,
        thinking_tokens=0,
        retrieval_ms=12,
        rerank_ms=3,
        first_token_ms=None,
        generation_ms=50,
        total_ms=65,
    )

    conn = auth._get_conn()
    turn = conn.execute("SELECT * FROM chat_turns WHERE id = 'turn-1'").fetchone()
    messages = conn.execute(
        "SELECT role, turn_id FROM messages WHERE turn_id = 'turn-1' ORDER BY id"
    ).fetchall()
    conn.close()

    assert turn["user_message_id"] == user_message["id"]
    assert turn["assistant_message_id"] == assistant["id"]
    assert turn["status"] == "error"
    assert turn["error_code"] == "model_not_found"
    assert [row["role"] for row in messages] == ["user", "assistant"]

# 오류 턴을 primary 로 채우면 실패가 primary 사용 실적으로 집계된다. 모르면 NULL
# 이어야 하고, 체인을 어디까지 내려갔는지는 attempted_models 가 말해야 한다.
def test_an_unresolved_turn_records_no_model_but_keeps_the_attempt_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "auth.db"))
    auth.init_db()

    conn = auth._get_conn()
    user_id = conn.execute(
        "INSERT INTO users (google_id, email, name) VALUES (?, ?, ?)",
        ("g-2", "chain@example.com", "chain"),
    ).lastrowid
    conn.commit()
    conn.close()

    session = auth.create_session(user_id, "chain")
    auth.create_chat_turn(
        "turn-unresolved",
        session["id"],
        None,
        "질문",
        "gemini-3.7-flash",
    )
    auth.complete_chat_turn(
        "turn-unresolved",
        assistant_message_id=None,
        resolved_model=None,
        resolved_model_id=None,
        attempted_models=json.dumps(
            ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
        ),
        rewritten_query=None,
        competition=None,
        source_ids=None,
        retrieval_status="ok",
        status="error",
        error_provider="gemini",
        error_code="rate_limited",
        error_message="429 RESOURCE_EXHAUSTED",
        finish_reason=None,
        input_tokens=None,
        output_tokens=None,
        thinking_tokens=None,
        retrieval_ms=None,
        rerank_ms=None,
        first_token_ms=None,
        generation_ms=None,
        total_ms=None,
    )

    turn = next(t for t in auth.list_chat_turns() if t["id"] == "turn-unresolved")
    # 답한 모델이 없으므로 비어 있어야 한다.
    assert turn["resolved_model"] is None
    assert turn["resolved_model_id"] is None
    # 요청은 남고, 시도 순서도 남는다.
    assert turn["requested_model"] == "gemini-3.7-flash"
    assert json.loads(turn["attempted_models"]) == [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    ]
