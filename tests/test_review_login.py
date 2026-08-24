"""심사용 ID/PW 로그인 회귀 테스트.

나이스페이와 9개 카드사가 "2차 인증 없는 ID/PW 계정, SNS 로그인 불가"를 요구해서
Google 로그인만 있는 이 서비스에 예외 경로를 하나 뚫었다. 공개 서비스에 붙는
비밀번호 로그인이므로, 여기서 보려는 것은 "자격증명이 없으면 존재하지 않는가"와
"틀린 자격증명이 절대 통과하지 못하는가"다.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

_chat_stub = types.ModuleType("src.chat")
_chat_stub.CHAT_CREDIT_COST = 1
_chat_stub.ROUTING_MODEL_KEYS = ("primary", "fallback")
_chat_stub.MODEL_CONFIG = {}
_chat_stub.PRIMARY_MODEL_KEY = "primary"
_chat_stub.PROMPT_VERSION = "test"
for _name in (
    "get_public_collections",
    "get_all_models_admin",
    "get_health_status",
    "init_model_settings",
    "init_resources",
    "is_model_available",
    "search_and_stream",
    "set_model_admin_settings",
):
    setattr(_chat_stub, _name, lambda *args, **kwargs: None)
sys.modules.setdefault("src.chat", _chat_stub)

import httpx  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from src import auth  # noqa: E402


def _init(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "review.db"))
    auth.init_db()
    auth.init_site_settings()
    server._review_attempts.clear()


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_LOGIN_ID", "nicepay-review")
    monkeypatch.setenv("REVIEW_LOGIN_PASSWORD", "correct-horse-battery")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="https://ksae-qna.test",
    )


def _post(data: dict) -> httpx.Response:
    async def run():
        async with _client() as client:
            return await client.post("/api/review-login", data=data)

    return asyncio.run(run())


def _get(path: str) -> httpx.Response:
    async def run():
        async with _client() as client:
            return await client.get(path)

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# 비활성 상태
# ---------------------------------------------------------------------------
def test_the_route_does_not_exist_without_credentials(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    monkeypatch.delenv("REVIEW_LOGIN_ID", raising=False)
    monkeypatch.delenv("REVIEW_LOGIN_PASSWORD", raising=False)

    assert auth.review_login_enabled() is False
    assert _get("/review-login").status_code == 404
    # 404 여야 한다. 401 이면 "여기에 로그인이 있다"는 사실을 알려주는 셈이다.
    assert _post({"login_id": "x", "password": "y"}).status_code == 404


def test_half_configured_credentials_leave_it_off(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    monkeypatch.setenv("REVIEW_LOGIN_ID", "nicepay-review")
    monkeypatch.delenv("REVIEW_LOGIN_PASSWORD", raising=False)
    assert auth.review_login_enabled() is False
    assert _get("/review-login").status_code == 404

    monkeypatch.delenv("REVIEW_LOGIN_ID", raising=False)
    monkeypatch.setenv("REVIEW_LOGIN_PASSWORD", "only-password")
    assert auth.review_login_enabled() is False


# ---------------------------------------------------------------------------
# 자격증명 검증
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "login_id,password",
    [
        ("nicepay-review", "wrong"),
        ("wrong", "correct-horse-battery"),
        ("", ""),
        ("nicepay-review", ""),
        ("nicepay-review ", "correct-horse-battery"),
        ("nicepay-review", "correct-horse-batter"),
        ("NICEPAY-REVIEW", "correct-horse-battery"),
    ],
)
def test_wrong_credentials_are_refused(tmp_path, monkeypatch, login_id, password):
    _init(tmp_path, monkeypatch)
    _enable(monkeypatch)

    response = _post({"login_id": login_id, "password": password})
    assert response.status_code == 401
    assert auth.COOKIE_NAME not in response.cookies
    # 실패가 계정을 만들어서는 안 된다.
    assert auth.get_user_by_google_id(auth.REVIEW_GOOGLE_ID) is None


def test_missing_form_fields_are_refused(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _enable(monkeypatch)
    assert _post({}).status_code == 401


def test_correct_credentials_hand_back_a_session(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _enable(monkeypatch)

    response = _post({"login_id": "nicepay-review", "password": "correct-horse-battery"})
    assert response.status_code == 200
    token = response.cookies.get(auth.COOKIE_NAME)
    assert token
    payload = auth.decode_jwt(token)
    assert payload is not None

    user = auth.get_user_by_google_id(auth.REVIEW_GOOGLE_ID)
    assert user is not None
    assert str(user["id"]) == payload["sub"]
    # 심사자가 결제까지 진행할 수 있어야 하므로 기본 지급분은 들어가 있어야 한다.
    assert int(user["credits"]) == auth.get_default_credits()


def test_logging_in_twice_reuses_the_same_account(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _enable(monkeypatch)

    first = auth.get_or_create_review_user()
    second = auth.get_or_create_review_user()
    assert first["id"] == second["id"]

    conn = auth._get_conn()
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE google_id = ?", (auth.REVIEW_GOOGLE_ID,)
    ).fetchone()["n"]
    conn.close()
    assert count == 1


def test_the_review_account_is_not_an_admin(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _enable(monkeypatch)
    # 관리자 판정은 ADMIN_EMAILS 목록으로만 이뤄진다. 심사 계정의 주소가 그 목록에
    # 우연히 들어가는 일이 없어야 한다.
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    auth.init_admin_emails()
    user = auth.get_or_create_review_user()
    assert user["email"].lower() not in auth.ADMIN_EMAILS


# ---------------------------------------------------------------------------
# 시도 제한
# ---------------------------------------------------------------------------
def test_repeated_failures_are_throttled(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _enable(monkeypatch)

    codes = [
        _post({"login_id": "nicepay-review", "password": "wrong"}).status_code
        for _ in range(server._REVIEW_ATTEMPT_LIMIT + 2)
    ]
    assert codes[0] == 401
    assert 429 in codes


def test_the_throttle_also_guards_correct_credentials(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _enable(monkeypatch)

    for _ in range(server._REVIEW_ATTEMPT_LIMIT):
        _post({"login_id": "nicepay-review", "password": "wrong"})
    # 정답을 넣어도 창이 닫혀 있어야 한다. 아니면 제한을 우회할 수 있다.
    blocked = _post({"login_id": "nicepay-review", "password": "correct-horse-battery"})
    assert blocked.status_code == 429
