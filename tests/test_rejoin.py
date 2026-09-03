"""탈퇴 후 재로그인은 동의 화면부터 다시 시작한다.

방침의 보유 기간이 "탈퇴 시까지"라 예전 동의는 끝났다. 콜백이 탈퇴한 행을 바로
되살리던 시절에는 동의 없이 곧바로 채팅으로 들어갔다. 여기서 보는 것은 콜백이
그 행을 건드리지 않고 동의 화면으로 보내는지, 그리고 동의 라우트가 새 동의를
적으면서 이용권 없이 되살리는지다.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from src import auth  # noqa: E402

USERINFO = {
    "sub": "google-bye",
    "email": "bye@example.com",
    "name": "Bye Again",
    "picture": "https://example.com/p.png",
}


def _init(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "rejoin.db"))
    auth.init_db()
    auth.init_site_settings()

    # Google 을 실제로 다녀오지 않는다. 콜백이 받는 토큰만 흉내낸다.
    async def authorize_access_token(request):
        return {"userinfo": dict(USERINFO)}

    monkeypatch.setattr(
        server,
        "oauth",
        types.SimpleNamespace(google=types.SimpleNamespace(authorize_access_token=authorize_access_token)),
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="https://ksae-qna.test",
    )


def _withdrawn_user() -> dict:
    user = auth.get_or_create_user(
        USERINFO["sub"], USERINFO["email"], "bye", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )
    conn = auth._get_conn()
    conn.execute(
        "UPDATE users SET privacy_consent_at = '2020-01-01 00:00:00', privacy_consent_version = 'old' WHERE id = ?",
        (user["id"],),
    )
    conn.commit()
    conn.close()
    assert auth.delete_user_account(user["id"]) == "deleted"
    return user


def test_a_withdrawn_account_is_sent_back_through_consent(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user = _withdrawn_user()

    async def run():
        async with _client() as client:
            callback = await client.get("/api/auth/callback?code=x&state=y")
            # 동의가 도착하기 전의 행 상태. 콜백은 여기서 아무것도 적지 않아야 한다.
            after_callback = auth.get_user_by_google_id(USERINFO["sub"])
            pending = await client.get("/api/auth/signup-pending")
            consent = await client.post("/api/auth/signup-consent", json={"privacy_consent": True})
            return callback, after_callback, pending, consent

    callback, after_callback, pending, consent = asyncio.run(run())

    # 콜백은 세션을 주지 않고 동의 화면으로 보내며, 행은 그대로 탈퇴 상태다.
    assert callback.status_code == 302
    assert callback.headers["location"] == "/signup/consent"
    assert auth.COOKIE_NAME not in callback.cookies
    assert after_callback["deleted_at"] is not None
    assert after_callback["privacy_consent_version"] == "old"

    assert pending.status_code == 200
    assert pending.json()["email"] == USERINFO["email"]

    # 동의가 도착하면 같은 행이 되살아난다 — 새 동의로, 이용권 없이.
    assert consent.status_code == 200
    assert auth.COOKIE_NAME in consent.cookies
    revived = auth.get_user_by_id(user["id"])
    assert revived is not None
    assert revived["deleted_at"] is None
    assert revived["name"] == "Bye Again"
    assert revived["credits"] == 0
    assert revived["privacy_consent_version"] == auth.PRIVACY_CONSENT_VERSION
    assert revived["privacy_consent_at"] != "2020-01-01 00:00:00"

    conn = auth._get_conn()
    count = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    conn.close()
    assert count == 1


def test_a_live_account_still_signs_straight_in(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user = auth.get_or_create_user(
        USERINFO["sub"], USERINFO["email"], "bye", None,
        privacy_consent_version=auth.PRIVACY_CONSENT_VERSION,
    )

    async def run():
        async with _client() as client:
            return await client.get("/api/auth/callback?code=x&state=y")

    callback = asyncio.run(run())

    assert callback.status_code == 302
    assert callback.headers["location"] == "/"
    assert auth.COOKIE_NAME in callback.cookies
    assert auth.get_user_by_id(user["id"])["name"] == "Bye Again"
