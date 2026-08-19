"""NicePay 결제 연동 회귀 테스트.

네트워크는 타지 않는다. 승인·취소·망취소는 전부 스텁으로 갈아끼우고, 서명·금액
검증과 지급/회수의 멱등성만 본다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime

import httpx
import pytest

from src import auth, payments


def _init(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "payments.db"))
    monkeypatch.setenv("NICEPAY_CLIENT_ID", "R2_testclient")
    monkeypatch.setenv("NICEPAY_SECRET_KEY", "testsecret")
    monkeypatch.setenv("NICEPAY_API_BASE", "https://sandbox-api.nicepay.co.kr")
    auth.init_db()
    auth.init_site_settings()


def _add_user(credits: int = 0, google_id: str = "buyer") -> int:
    conn = auth._get_conn()
    user_id = conn.execute(
        "INSERT INTO users (google_id, email, name, credits) VALUES (?, ?, ?, ?)",
        (google_id, f"{google_id}@example.com", google_id, credits),
    ).lastrowid
    conn.commit()
    conn.close()
    return user_id


def _credits(user_id: int) -> int:
    conn = auth._get_conn()
    row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row["credits"]


def _return_form(order: dict, *, auth_token: str = "authtoken", tid: str = "tid-1") -> dict:
    amount = str(order["amount"])
    return {
        "authResultCode": "0000",
        "authResultMsg": "인증 성공",
        "tid": tid,
        "clientId": payments.client_id(),
        "orderId": order["order_id"],
        "amount": amount,
        "authToken": auth_token,
        "signature": payments.auth_signature(auth_token, amount),
    }


def _approved(order: dict, tid: str = "tid-1") -> dict:
    return {
        "resultCode": "0000",
        "resultMsg": "정상 처리되었습니다",
        "status": "paid",
        "tid": tid,
        "orderId": order["order_id"],
        "amount": order["amount"],
        "payMethod": "card",
        "ediDate": "2026-08-19T12:00:00.000+0900",
    }


def _stub_approve(monkeypatch, response: dict) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_approve(tid, amount):
        calls.append((tid, amount))
        return response

    monkeypatch.setattr(payments, "approve", fake_approve)
    return calls


def _stub_net_cancel(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def fake_net_cancel(order_id):
        calls.append(order_id)
        return {"resultCode": "0000"}

    monkeypatch.setattr(payments, "net_cancel", fake_net_cancel)
    return calls


# ---------------------------------------------------------------------------
# 서명
# ---------------------------------------------------------------------------
def test_signatures_follow_the_documented_formulas(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)

    expected_auth = hashlib.sha256(
        b"authtoken" + b"R2_testclient" + b"1000" + b"testsecret"
    ).hexdigest()
    assert payments.auth_signature("authtoken", "1000") == expected_auth
    # 정수로 넘겨도 같은 문자열이 만들어져야 한다.
    assert payments.auth_signature("authtoken", 1000) == expected_auth

    expected_result = hashlib.sha256(
        b"tid-1" + b"1000" + b"2026-08-19T12:00:00.000+0900" + b"testsecret"
    ).hexdigest()
    assert (
        payments.result_signature("tid-1", 1000, "2026-08-19T12:00:00.000+0900")
        == expected_result
    )

    assert payments.signature_matches(expected_auth, expected_auth)
    assert not payments.signature_matches(expected_auth, "deadbeef")
    assert not payments.signature_matches(expected_auth, None)


# ---------------------------------------------------------------------------
# 주문 생성
# ---------------------------------------------------------------------------
def test_order_amount_comes_from_the_configured_unit_price(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()

    order = payments.create_order(user_id, "buyer@example.com", 30)
    assert order["unit_price"] == 100
    assert order["amount"] == 3000
    assert order["goods_name"] == "PitBot 이용권 30장"

    auth.set_site_setting("credit_unit_price", "250")
    assert payments.min_quantity() == 4  # ceil(1000 / 250)
    assert payments.create_order(user_id, "buyer@example.com", 4)["amount"] == 1000


def test_order_below_the_card_minimum_is_refused(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()

    # 단가 100원이면 1,000원을 채우는 최소 수량은 10장이다.
    assert payments.min_quantity() == 10
    with pytest.raises(ValueError, match="10~1000장"):
        payments.create_order(user_id, "buyer@example.com", 9)

    assert payments.create_order(user_id, "buyer@example.com", 10)["amount"] == 1000


def test_order_above_the_configured_ceiling_is_refused(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    auth.set_site_setting("credit_max_quantity", "50")

    with pytest.raises(ValueError):
        payments.create_order(user_id, "buyer@example.com", 51)


# ---------------------------------------------------------------------------
# returnUrl
# ---------------------------------------------------------------------------
def test_successful_return_approves_and_grants_credits(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=3)
    order = payments.create_order(user_id, "buyer@example.com", 10)
    calls = _stub_approve(monkeypatch, _approved(order))

    order_id, result = asyncio.run(payments.process_return(_return_form(order)))

    assert (order_id, result) == (order["order_id"], "paid")
    assert calls == [("tid-1", 1000)]
    assert _credits(user_id) == 13

    stored = payments.get_order(order["order_id"])
    assert stored["status"] == "paid"
    assert stored["granted"] == 10
    assert stored["tid"] == "tid-1"

    transactions = auth.get_transactions(user_id)
    assert transactions[0]["type"] == "purchase"
    assert transactions[0]["amount"] == 10


def test_tampered_amount_never_reaches_the_approval_api(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    calls = _stub_approve(monkeypatch, _approved(order))

    form = _return_form(order)
    form["amount"] = "100"

    order_id, result = asyncio.run(payments.process_return(form))

    assert result == "failed"
    assert calls == []
    assert _credits(user_id) == 0
    assert payments.get_order(order_id)["status"] == "failed"


def test_bad_signature_never_reaches_the_approval_api(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    calls = _stub_approve(monkeypatch, _approved(order))

    form = _return_form(order)
    form["signature"] = "0" * 64

    order_id, result = asyncio.run(payments.process_return(form))

    assert result == "failed"
    assert calls == []
    assert payments.get_order(order_id)["fail_reason"] == "결제 인증 서명 검증에 실패했습니다"


def test_cancelled_auth_marks_the_order_failed(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    calls = _stub_approve(monkeypatch, _approved(order))

    form = _return_form(order)
    form["authResultCode"] = "1001"
    form["authResultMsg"] = "사용자가 취소하였습니다"

    order_id, result = asyncio.run(payments.process_return(form))

    assert result == "failed"
    assert calls == []
    assert payments.get_order(order_id)["fail_reason"] == "사용자가 취소하였습니다"


def test_unknown_order_is_rejected_before_anything_else(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    calls = _stub_approve(monkeypatch, {})

    order_id, result = asyncio.run(
        payments.process_return({"orderId": "nope", "authResultCode": "0000"})
    )

    assert (order_id, result) == (None, "invalid")
    assert calls == []


def test_approval_timeout_triggers_a_net_cancel(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)

    async def timing_out(tid, amount):
        raise TimeoutError("read timeout")

    monkeypatch.setattr(payments, "approve", timing_out)
    net_cancels = _stub_net_cancel(monkeypatch)

    order_id, result = asyncio.run(payments.process_return(_return_form(order)))

    assert result == "failed"
    assert net_cancels == [order["order_id"]]
    assert _credits(user_id) == 0
    assert payments.get_order(order_id)["status"] == "failed"


def test_rejected_approval_does_not_grant(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    _stub_approve(
        monkeypatch,
        {"resultCode": "3041", "resultMsg": "금액 오류", "status": "failed"},
    )

    order_id, result = asyncio.run(payments.process_return(_return_form(order)))

    assert result == "failed"
    assert _credits(user_id) == 0
    assert payments.get_order(order_id)["fail_reason"] == "금액 오류"


def test_approval_response_with_a_wrong_signature_is_not_granted(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    response = _approved(order) | {"signature": "0" * 64}
    _stub_approve(monkeypatch, response)

    order_id, result = asyncio.run(payments.process_return(_return_form(order)))

    assert result == "failed"
    assert _credits(user_id) == 0


def test_approval_response_signature_is_accepted_when_correct(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    response = _approved(order)
    response["signature"] = payments.result_signature(
        response["tid"], response["amount"], response["ediDate"]
    )
    _stub_approve(monkeypatch, response)

    _, result = asyncio.run(payments.process_return(_return_form(order)))

    assert result == "paid"
    assert _credits(user_id) == 10


# ---------------------------------------------------------------------------
# 멱등성
# ---------------------------------------------------------------------------
def test_a_replayed_return_grants_only_once(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    _stub_approve(monkeypatch, _approved(order))

    asyncio.run(payments.process_return(_return_form(order)))
    asyncio.run(payments.process_return(_return_form(order)))

    assert _credits(user_id) == 10
    assert len(auth.get_transactions(user_id)) == 1


def test_webhook_after_a_settled_return_does_not_grant_twice(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    _stub_approve(monkeypatch, _approved(order))
    asyncio.run(payments.process_return(_return_form(order)))

    payments.process_webhook(
        {
            "orderId": order["order_id"],
            "tid": "tid-1",
            "amount": order["amount"],
            "status": "paid",
        }
    )

    assert _credits(user_id) == 10
    assert len(auth.get_transactions(user_id)) == 1


def test_webhook_settles_an_order_the_browser_never_came_back_from(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)

    payments.process_webhook(
        {
            "orderId": order["order_id"],
            "tid": "tid-1",
            "amount": order["amount"],
            "status": "paid",
            "payMethod": "card",
        }
    )

    assert _credits(user_id) == 10
    assert payments.get_order(order["order_id"])["status"] == "paid"


def test_webhook_with_a_mismatched_amount_is_ignored(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)

    payments.process_webhook(
        {"orderId": order["order_id"], "tid": "tid-1", "amount": 100, "status": "paid"}
    )

    assert _credits(user_id) == 0
    assert payments.get_order(order["order_id"])["status"] == "pending"


def test_webhook_with_a_bad_signature_is_ignored(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)

    payments.process_webhook(
        {
            "orderId": order["order_id"],
            "tid": "tid-1",
            "amount": order["amount"],
            "ediDate": "2026-08-19T12:00:00.000+0900",
            "status": "paid",
            "signature": "0" * 64,
        }
    )

    assert _credits(user_id) == 0
    assert payments.get_order(order["order_id"])["status"] == "pending"


# ---------------------------------------------------------------------------
# 취소
# ---------------------------------------------------------------------------
def _settle(monkeypatch, user_id: int, quantity: int = 10) -> dict:
    order = payments.create_order(user_id, "buyer@example.com", quantity)
    _stub_approve(monkeypatch, _approved(order))
    asyncio.run(payments.process_return(_return_form(order)))
    return order


def test_admin_cancel_reclaims_the_granted_credits(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=2)
    order = _settle(monkeypatch, user_id)
    assert _credits(user_id) == 12

    async def fake_cancel(tid, *, reason, order_id):
        return {"resultCode": "0000", "status": "cancelled", "tid": tid}

    monkeypatch.setattr(payments, "cancel_payment", fake_cancel)

    result = asyncio.run(payments.admin_cancel(order["order_id"], "테스트 취소"))

    assert result == {"ok": True, "reclaimed": 10}
    assert _credits(user_id) == 2
    stored = payments.get_order(order["order_id"])
    assert stored["status"] == "cancelled"
    assert stored["reclaimed"] == 10


def test_cancel_never_pushes_the_balance_below_zero(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = _settle(monkeypatch, user_id)

    # 산 이용권을 대부분 써버린 뒤 취소한 경우.
    auth.deduct_credit(user_id, 8, "질문")
    assert _credits(user_id) == 2

    async def fake_cancel(tid, *, reason, order_id):
        return {"resultCode": "0000"}

    monkeypatch.setattr(payments, "cancel_payment", fake_cancel)

    result = asyncio.run(payments.admin_cancel(order["order_id"], "테스트 취소"))

    assert result == {"ok": True, "reclaimed": 2}
    assert _credits(user_id) == 0
    assert payments.get_order(order["order_id"])["reclaimed"] == 2


def test_cancel_is_refused_when_the_gateway_rejects_it(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = _settle(monkeypatch, user_id)

    async def fake_cancel(tid, *, reason, order_id):
        return {"resultCode": "2001", "resultMsg": "취소 기간이 지났습니다"}

    monkeypatch.setattr(payments, "cancel_payment", fake_cancel)

    result = asyncio.run(payments.admin_cancel(order["order_id"], "테스트 취소"))

    assert result["ok"] is False
    assert _credits(user_id) == 10
    assert payments.get_order(order["order_id"])["status"] == "paid"


def test_only_paid_orders_can_be_cancelled(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)

    result = asyncio.run(payments.admin_cancel(order["order_id"], "테스트 취소"))

    assert result["ok"] is False


def test_gateway_side_cancel_webhook_reclaims_once(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = _settle(monkeypatch, user_id)

    cancel_hook = {
        "orderId": order["order_id"],
        "tid": "tid-1",
        "amount": order["amount"],
        "status": "cancelled",
    }
    payments.process_webhook(cancel_hook)
    payments.process_webhook(cancel_hook)

    assert _credits(user_id) == 0
    assert payments.get_order(order["order_id"])["reclaimed"] == 10


# ---------------------------------------------------------------------------
# 계정 삭제와의 상호작용
# ---------------------------------------------------------------------------
def test_withdrawal_waits_for_an_open_payment_window(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    payments.create_order(user_id, "buyer@example.com", 10)

    assert auth.delete_user_account(user_id) == "payment_pending"


def test_settled_payments_outlive_the_account(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = _settle(monkeypatch, user_id)

    assert auth.delete_user_account(user_id) == "deleted"

    # 탈퇴는 users 행을 남기므로 주문도 그 행을 계속 가리킨다 — 원장이 대사 가능한
    # 상태로 유지되는 쪽이다.
    stored = payments.get_order(order["order_id"])
    assert stored is not None
    assert stored["user_id"] == user_id
    assert stored["status"] == "paid"


def test_an_approval_for_a_departed_buyer_is_recorded_without_granting(
    tmp_path, monkeypatch
):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)

    # 결제창이 열려 있는 사이 계정 행까지 사라진 상황. 탈퇴는 행을 남기므로 실제로는
    # 관리자가 직접 지운 경우뿐이지만, 지급 대상이 없을 때 승인만 기록되는지를 본다.
    conn = auth._get_conn()
    conn.execute(
        "UPDATE payments SET user_id = NULL WHERE order_id = ?", (order["order_id"],)
    )
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    _stub_approve(monkeypatch, _approved(order))
    _, result = asyncio.run(payments.process_return(_return_form(order)))

    assert result == "paid"
    stored = payments.get_order(order["order_id"])
    assert stored["status"] == "paid"
    assert stored["granted"] == 0


# ---------------------------------------------------------------------------
# HTTP 계약
#
# server.py 를 통째로 올리되 src.chat 은 스텁으로 바꾼다. 여기서 보려는 것은
# 결제 라우트의 배선 — urlencoded 폼 파싱, 리다이렉트 코드, 웹훅 응답 본문 —
# 이지 임베딩 모델이 아니기 때문이다. lifespan 은 돌리지 않으므로(ASGITransport)
# 운영 DB 를 건드릴 일도 없다.
# ---------------------------------------------------------------------------
import os  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

_chat_stub = types.ModuleType("src.chat")
_chat_stub.CHAT_CREDIT_COST = 1
_chat_stub.FALLBACK_MODEL_KEY = "fallback"
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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app),
        base_url="https://ksae-qna.test",
    )


def _login(user_id: int) -> dict:
    return {auth.COOKIE_NAME: auth.create_jwt(user_id)}


def test_return_endpoint_parses_the_urlencoded_post_and_redirects(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    _stub_approve(monkeypatch, _approved(order))

    async def call():
        async with _client() as client:
            return await client.post("/api/payments/return", data=_return_form(order))

    response = asyncio.run(call())

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/payments/result?result=paid&order={order['order_id']}"
    )
    assert _credits(user_id) == 10


def test_webhook_endpoint_answers_with_the_literal_ok_body(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)

    async def call():
        async with _client() as client:
            return await client.post(
                "/api/payments/webhook",
                json={
                    "orderId": order["order_id"],
                    "tid": "tid-1",
                    "amount": order["amount"],
                    "status": "paid",
                },
            )

    response = asyncio.run(call())

    assert response.status_code == 200
    assert response.text == "OK"
    assert _credits(user_id) == 10


def test_order_endpoint_requires_a_session_and_prices_server_side(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()

    async def call():
        async with _client() as client:
            anonymous = await client.post("/api/payments/orders", json={"quantity": 10})
            authorised = await client.post(
                "/api/payments/orders", json={"quantity": 10}, cookies=_login(user_id)
            )
            return anonymous, authorised

    anonymous, authorised = asyncio.run(call())

    assert anonymous.status_code == 401
    assert authorised.status_code == 200

    body = authorised.json()
    assert body["amount"] == 1000
    assert body["method"] == "cardAndEasyPay"
    assert body["return_url"].endswith("/api/payments/return")
    assert "secret" not in str(body).lower()


def test_free_topup_route_is_gone(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()

    async def call():
        async with _client() as client:
            return await client.post(
                "/api/credits/topup", json={"amount": 100}, cookies=_login(user_id)
            )

    assert asyncio.run(call()).status_code == 404
    assert _credits(user_id) == 0


# ---------------------------------------------------------------------------
# API 인증 (Bearer)
#
# 이 상점은 결제 API에 Basic을 받지 않는다 — 조회조차 U103 "사용자 인증타입이
# 맞지 않습니다"로 거절한다. Basic이 통하는 곳은 /v1/access-token 하나뿐이다.
# ---------------------------------------------------------------------------
def _transport(handler):
    """httpx 요청을 가로채 (method, path, authorization) 을 기록한다."""
    seen: list[tuple[str, str, str]] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        return handler(request, len(seen))

    return seen, httpx.MockTransport(route)


def _install(monkeypatch, transport) -> None:
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(payments.httpx, "AsyncClient", factory)


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "resultCode": "0000",
            "resultMsg": "정상 처리되었습니다.",
            "accessToken": "tok-1",
            "tokenType": "Bearer",
        },
    )


def test_payment_calls_use_a_bearer_token_and_basic_only_issues_it(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    payments.reset_access_token()

    def handler(request, call):
        if request.url.path == "/v1/access-token":
            return _token_response()
        return httpx.Response(200, json={"resultCode": "0000", "status": "paid"})

    seen, transport = _transport(handler)
    _install(monkeypatch, transport)

    result = asyncio.run(payments.approve("tid-1", 1000))

    assert result["resultCode"] == "0000"
    assert seen == [
        ("POST", "/v1/access-token", payments._basic_auth_header()),
        ("POST", "/v1/payments/tid-1", "Bearer tok-1"),
    ]


def test_the_token_is_reused_across_calls(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    payments.reset_access_token()

    def handler(request, call):
        if request.url.path == "/v1/access-token":
            return _token_response()
        return httpx.Response(200, json={"resultCode": "0000"})

    seen, transport = _transport(handler)
    _install(monkeypatch, transport)

    asyncio.run(payments.approve("tid-1", 1000))
    asyncio.run(payments.cancel_payment("tid-1", reason="test", order_id="o1"))
    asyncio.run(payments.net_cancel("o1"))

    issued = [call for call in seen if call[1] == "/v1/access-token"]
    assert len(issued) == 1
    assert [call[1] for call in seen if call[1] != "/v1/access-token"] == [
        "/v1/payments/tid-1",
        "/v1/payments/tid-1/cancel",
        "/v1/payments/netcancel",
    ]


def test_an_expired_token_is_reissued_once_and_the_call_retried(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    payments.reset_access_token()

    def handler(request, call):
        if request.url.path == "/v1/access-token":
            return _token_response()
        # 첫 결제 호출은 만료된 토큰으로 잘리고, 재발급 후에는 통과한다.
        if request.headers["authorization"] == "Bearer tok-1" and call == 2:
            return httpx.Response(
                200,
                json={"resultCode": "U103", "resultMsg": "사용자 인증타입이 맞지 않습니다."},
            )
        return httpx.Response(200, json={"resultCode": "0000", "status": "paid"})

    seen, transport = _transport(handler)
    _install(monkeypatch, transport)

    result = asyncio.run(payments.approve("tid-1", 1000))

    assert result["resultCode"] == "0000"
    assert [call[1] for call in seen] == [
        "/v1/access-token",
        "/v1/payments/tid-1",
        "/v1/access-token",
        "/v1/payments/tid-1",
    ]


def test_other_error_codes_are_never_retried(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    payments.reset_access_token()

    def handler(request, call):
        if request.url.path == "/v1/access-token":
            return _token_response()
        return httpx.Response(200, json={"resultCode": "3041", "resultMsg": "금액 오류"})

    seen, transport = _transport(handler)
    _install(monkeypatch, transport)

    result = asyncio.run(payments.approve("tid-1", 1000))

    assert result["resultCode"] == "3041"
    assert [call[1] for call in seen].count("/v1/payments/tid-1") == 1


def test_a_failed_token_issue_surfaces_as_an_error(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    payments.reset_access_token()

    def handler(request, call):
        return httpx.Response(
            200, json={"resultCode": "U116", "resultMsg": "사용자 정보가 존재하지 않습니다."}
        )

    _seen, transport = _transport(handler)
    _install(monkeypatch, transport)

    with pytest.raises(RuntimeError, match="U116"):
        asyncio.run(payments.access_token())


def test_a_rejected_approval_keeps_the_response_for_diagnosis(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    rejection = {"resultCode": "U103", "resultMsg": "사용자 인증타입이 맞지 않습니다."}
    _stub_approve(monkeypatch, rejection)

    order_id, result = asyncio.run(payments.process_return(_return_form(order)))

    assert result == "failed"
    stored = payments.get_order(order_id)
    assert stored["fail_reason"] == "사용자 인증타입이 맞지 않습니다."
    # 사유 문구만 남으면 어느 단계에서 무엇이 틀렸는지 되짚을 수 없다.
    assert json.loads(stored["raw_approve"]) == rejection
    assert json.loads(stored["raw_auth"])["authResultCode"] == "0000"


# ---------------------------------------------------------------------------
# 유료 구매분과 무료 충전분의 분리
#
# `credits` 는 총 잔액이고 `paid_credits` 는 그중 구매분이다. 월 충전은 무료분
# (credits - paid_credits)만 본다 — 이용권을 사서 총 잔액이 바닥값을 넘었다는
# 이유로 다음 달 무료 충전이 사라지면 돈 낸 사람이 손해를 본다.
# ---------------------------------------------------------------------------
def _balance(user_id: int) -> tuple[int, int]:
    """(총 잔액, 구매분)."""
    conn = auth._get_conn()
    row = conn.execute(
        "SELECT credits, paid_credits FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["credits"], row["paid_credits"]


def test_buying_does_not_cancel_next_months_free_refill(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    # 무료 바닥값 20, 잔액 10에서 10장을 구매해 총 20장이 된 상태.
    user_id = _add_user(credits=10)
    _settle(monkeypatch, user_id, 10)
    assert _balance(user_id) == (20, 10)

    applied = auth.apply_monthly_credit_refill(
        datetime(2026, 9, 1, 0, 1, tzinfo=auth.KST)
    )

    assert applied["applied"] is True
    # 무료분은 10장뿐이었으니 20장까지 채워지고, 구매분 10장은 그대로 남는다.
    assert _balance(user_id) == (30, 10)


def test_the_free_portion_is_spent_before_the_purchased_one(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=3)
    _settle(monkeypatch, user_id, 10)
    assert _balance(user_id) == (13, 10)

    # 무료분 3장 안에서만 쓰면 구매분은 손대지 않는다.
    assert auth.deduct_credit(user_id, 3, "질문") is True
    assert _balance(user_id) == (10, 10)

    # 무료분이 없으면 그때부터 구매분이 줄어든다.
    assert auth.deduct_credit(user_id, 4, "질문") is True
    assert _balance(user_id) == (6, 6)


def test_a_refund_lands_in_the_free_portion(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    _settle(monkeypatch, user_id, 10)
    auth.deduct_credit(user_id, 4, "질문")
    assert _balance(user_id) == (6, 6)

    auth.refund_credit(user_id, 1, "오류 환불")

    assert _balance(user_id) == (7, 6)


def test_cancel_never_reclaims_free_credits(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=20)
    order = _settle(monkeypatch, user_id, 10)
    # 무료 20장을 다 쓰고 구매분도 6장 썼다 → 구매분 4장만 남는다.
    auth.deduct_credit(user_id, 26, "질문")
    assert _balance(user_id) == (4, 4)

    async def fake_cancel(tid, *, reason, order_id):
        return {"resultCode": "0000"}

    monkeypatch.setattr(payments, "cancel_payment", fake_cancel)
    result = asyncio.run(payments.admin_cancel(order["order_id"], "테스트 취소"))

    # 남은 구매분 4장만 회수한다. 이미 쓴 6장을 무료분에서 빼오지 않는다.
    assert result == {"ok": True, "reclaimed": 4}
    assert _balance(user_id) == (0, 0)


def test_cancel_leaves_free_credits_granted_after_the_purchase(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = _settle(monkeypatch, user_id, 10)
    # 결제 후 월 충전이 들어와 무료분이 얹힌 상태.
    auth.apply_monthly_credit_refill(datetime(2026, 9, 1, 0, 1, tzinfo=auth.KST))
    assert _balance(user_id) == (30, 10)

    async def fake_cancel(tid, *, reason, order_id):
        return {"resultCode": "0000"}

    monkeypatch.setattr(payments, "cancel_payment", fake_cancel)
    asyncio.run(payments.admin_cancel(order["order_id"], "테스트 취소"))

    # 구매분 10장만 사라지고 무료 20장은 남는다.
    assert _balance(user_id) == (20, 0)


def test_an_admin_absolute_adjustment_keeps_the_invariant(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    _settle(monkeypatch, user_id, 10)
    assert _balance(user_id) == (10, 10)

    # 총 잔액을 구매분보다 낮게 내리면 구매분도 함께 내려가야 한다.
    auth.admin_set_credits(user_id, 4)
    assert _balance(user_id) == (4, 4)

    # 올릴 때는 무료분만 늘어난다.
    auth.admin_set_credits(user_id, 9)
    assert _balance(user_id) == (9, 4)


# ---------------------------------------------------------------------------
# 버려진 주문 정리
#
# 결제창을 열었다 닫기만 해도 주문 행은 pending으로 남는다. 그대로 두면
# "pending으로 오래 남은 건 = 지급 누락"이라는 운영 신호가 버려진 주문에 묻힌다.
# ---------------------------------------------------------------------------
def _age_order(order_id: str, minutes: int) -> None:
    conn = auth._get_conn()
    conn.execute(
        "UPDATE payments SET created_at = datetime('now', ?) WHERE order_id = ?",
        (f"-{minutes} minutes", order_id),
    )
    conn.commit()
    conn.close()


def test_only_orders_past_the_window_expire(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    stale = payments.create_order(user_id, "buyer@example.com", 10)["order_id"]
    fresh = payments.create_order(user_id, "buyer@example.com", 10)["order_id"]
    _age_order(stale, 61)

    assert payments.expire_stale_orders() == 1

    assert payments.get_order(stale)["status"] == "expired"
    assert payments.get_order(fresh)["status"] == "pending"
    # 두 번 돌려도 같은 건을 다시 세지 않는다.
    assert payments.expire_stale_orders() == 0


def test_expiry_never_touches_a_settled_or_cancelled_order(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    paid = _settle(monkeypatch, user_id)["order_id"]
    _age_order(paid, 500)

    assert payments.expire_stale_orders() == 0
    assert payments.get_order(paid)["status"] == "paid"


# 만료를 승인 게이트로 만들면 청구는 됐는데 이용권이 없는 주문이 생긴다.
def test_an_expired_order_can_still_be_settled(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    _age_order(order["order_id"], 61)
    payments.expire_stale_orders()
    assert payments.get_order(order["order_id"])["status"] == "expired"

    _stub_approve(monkeypatch, _approved(order))
    _, result = asyncio.run(payments.process_return(_return_form(order)))

    assert result == "paid"
    assert _credits(user_id) == 10
    assert payments.get_order(order["order_id"])["status"] == "paid"


def test_an_expired_order_can_still_record_a_failure(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    _age_order(order["order_id"], 61)
    payments.expire_stale_orders()

    form = _return_form(order)
    form["authResultCode"] = "1001"
    form["authResultMsg"] = "사용자가 취소하였습니다"
    _, result = asyncio.run(payments.process_return(form))

    assert result == "failed"
    stored = payments.get_order(order["order_id"])
    assert stored["status"] == "failed"
    assert stored["fail_reason"] == "사용자가 취소하였습니다"


def test_settling_twice_still_grants_once_after_expiry(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = payments.create_order(user_id, "buyer@example.com", 10)
    _age_order(order["order_id"], 61)
    payments.expire_stale_orders()
    _stub_approve(monkeypatch, _approved(order))

    asyncio.run(payments.process_return(_return_form(order)))
    asyncio.run(payments.process_return(_return_form(order)))

    assert _credits(user_id) == 10
    assert len(auth.get_transactions(user_id)) == 1
