"""
NicePay 결제 연동 (결제창 서버승인 모델).

이용권은 승인 API가 성립시킨 뒤에만 지급된다. 결제창이 인증만 마치고 승인 API를
부르지 않으면 결제 자체가 없던 일이 되므로, 인증 결과(returnUrl)와 승인은 분리해서
다룬다.

returnUrl과 웹훅이 같은 승인 건을 동시에 들고 들어올 수 있다. 지급·회수는 모두
`WHERE status = ?` 조건부 UPDATE의 rowcount로 단 한 번만 통과시키고, 잔액 변경을
같은 트랜잭션에 넣어 이중 지급을 구조적으로 막는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid

import httpx

from src.auth import _get_conn, get_site_setting

logger = logging.getLogger(__name__)

# 나이스페이 오류코드 3041 "금액 오류(1000원 미만 신용카드 승인 불가)".
# 카드·간편결제 결제창을 쓰는 이상 이 밑의 주문은 만들어도 승인이 거절된다.
MIN_CARD_AMOUNT = 1000

# 카드 + 간편결제(카카오페이·네이버페이·페이코 등) 통합 결제창.
PAY_METHOD = "cardAndEasyPay"

GOODS_NAME = "PitBot 이용권"

DEFAULT_API_BASE = "https://api.nicepay.co.kr"

# 승인 API는 카드사 응답을 기다리므로 read timeout을 길게 잡는다. 그래도 끊기면
# 승인 성립 여부를 알 수 없는 상태라 망취소를 던져야 한다.
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)

# 액세스 토큰 유효시간 30분. 만료 직전에 걸리지 않도록 여유를 두고 갱신한다.
# expireAt을 파싱하지 않는 이유는 시계 오차와 오프셋 표기(+0900, 콜론 없음)에
# 기대지 않기 위해서다 — 어긋나도 U103 재시도가 받아준다.
_TOKEN_TTL = 30 * 60
_TOKEN_MARGIN = 120

# 인증타입 불일치. Basic으로 결제 API를 부르면 이 코드가 온다.
_WRONG_AUTH_TYPE = "U103"

_PAID = "paid"

# 결제창을 열었다 닫기만 해도 주문 행은 pending으로 남는다. 그대로 두면
# "pending으로 오래 남은 건 = 지급 누락"이라는 운영 신호가 버려진 주문에 묻힌다.
#
# 만료는 정리용 라벨일 뿐 승인 게이트가 아니다. 만료 직후에 결제가 완료되면 승인은
# 그대로 성립하므로, 지급과 실패 기록은 expired 상태에서도 통과시켜야 한다 —
# 안 그러면 청구는 됐는데 이용권이 없는 주문이 생긴다.
_GRANTABLE = ("pending", "expired")
_EXPIRY_MINUTES = 60

# 사업자 정보 · 약관 설정 키. 값은 관리자 화면에서 채운다.
BUSINESS_SETTING_KEYS = (
    "biz_name",
    "biz_owner",
    "biz_reg_no",
    "biz_mail_order_no",
    "biz_address",
    "biz_tel",
    "biz_email",
)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def client_id() -> str:
    """결제창에 실려 브라우저로 나가는 공개값."""
    return os.environ.get("NICEPAY_CLIENT_ID", "")


def _secret_key() -> str:
    """서버 전용. 로그·응답 어디에도 실리면 안 된다."""
    return os.environ.get("NICEPAY_SECRET_KEY", "")


def api_base() -> str:
    return os.environ.get("NICEPAY_API_BASE", DEFAULT_API_BASE).rstrip("/")


def is_configured() -> bool:
    return bool(client_id() and _secret_key())


def unit_price() -> int:
    """이용권 1장 가격. 0 이하로 설정되면 결제 금액이 0이 되므로 1원으로 막는다."""
    try:
        return max(1, int(get_site_setting("credit_unit_price")))
    except (ValueError, TypeError):
        return 100


def max_quantity() -> int:
    try:
        return max(1, int(get_site_setting("credit_max_quantity")))
    except (ValueError, TypeError):
        return 1000


def min_quantity() -> int:
    """카드 최소 결제금액을 채우는 최소 수량 (올림)."""
    return max(1, -(-MIN_CARD_AMOUNT // unit_price()))


def business_info() -> dict[str, str]:
    return {key: get_site_setting(key) for key in BUSINESS_SETTING_KEYS}


def public_config() -> dict:
    """구매 UI가 필요한 값 모음. 시크릿은 포함하지 않는다."""
    price = unit_price()
    low = min_quantity()
    high = max_quantity()
    return {
        "enabled": is_configured(),
        "client_id": client_id(),
        "method": PAY_METHOD,
        "goods_name": GOODS_NAME,
        "unit_price": price,
        # 최소 수량이 상한을 넘는 설정(단가가 지나치게 낮고 상한이 작은 경우)에서도
        # UI가 빈 범위를 그리지 않도록 상한을 끌어올린다.
        "min_quantity": low,
        "max_quantity": max(low, high),
        "min_amount": MIN_CARD_AMOUNT,
    }


# ---------------------------------------------------------------------------
# 위변조 검증
# ---------------------------------------------------------------------------
def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def auth_signature(auth_token: str, amount: str | int) -> str:
    """returnUrl 검증값: hex(sha256(authToken + clientId + amount + secretKey))."""
    return _sha256_hex(f"{auth_token}{client_id()}{amount}{_secret_key()}")


def result_signature(tid: str, amount: str | int, edi_date: str) -> str:
    """승인 응답·웹훅 검증값: hex(sha256(tid + amount + ediDate + secretKey))."""
    return _sha256_hex(f"{tid}{amount}{edi_date}{_secret_key()}")


def signature_matches(expected: str, received: str | None) -> bool:
    if not received:
        return False
    return hmac.compare_digest(expected, received)


def _basic_auth_header() -> str:
    raw = f"{client_id()}:{_secret_key()}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# 나이스페이 API
#
# 결제 API(/v1/payments/*)는 Bearer 토큰만 받는다. Basic을 보내면 조회조차
# U103 "사용자 인증타입이 맞지 않습니다"로 거절된다. Basic이 통하는 곳은 토큰을
# 발급하는 /v1/access-token 하나뿐이다.
# ---------------------------------------------------------------------------
_token: dict = {"value": None, "expires_at": 0.0}


def reset_access_token() -> None:
    _token["value"] = None
    _token["expires_at"] = 0.0


async def _request(method: str, path: str, body: dict | None, authorization: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.request(
            method,
            f"{api_base()}{path}",
            json=body,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
    return response.json()


async def access_token() -> str:
    """Bearer 토큰을 캐시해서 돌려준다. 발급 실패는 예외로 올린다 — 토큰이 없으면
    승인을 시도조차 할 수 없고, 부르는 쪽이 그걸 승인 실패로 다뤄야 한다.

    락은 두지 않는다. 단일 레플리카에 저트래픽이라 경합해도 토큰이 하나 더
    발급될 뿐이고, 먼저 받은 토큰도 만료까지 그대로 유효하다.
    """
    now = time.monotonic()
    if _token["value"] and now < _token["expires_at"]:
        return _token["value"]

    body = await _request("POST", "/v1/access-token", {}, _basic_auth_header())
    if body.get("resultCode") != "0000" or not body.get("accessToken"):
        raise RuntimeError(
            f"access token 발급 실패: {body.get('resultCode')} {body.get('resultMsg')}"
        )

    _token["value"] = body["accessToken"]
    _token["expires_at"] = now + _TOKEN_TTL - _TOKEN_MARGIN
    return _token["value"]


async def _post(path: str, body: dict) -> dict:
    """토큰이 만료돼 U103이 오면 한 번 재발급해 재시도한다.

    U103은 인증 계층에서 잘린 것이므로 요청이 처리된 흔적이 없다. 그래서 승인
    요청이라도 재시도가 안전하다. 다른 오류코드는 절대 재시도하지 않는다.
    """
    result = await _request("POST", path, body, f"Bearer {await access_token()}")
    if result.get("resultCode") == _WRONG_AUTH_TYPE:
        logger.warning("Access token rejected (U103); reissuing and retrying %s", path)
        reset_access_token()
        result = await _request("POST", path, body, f"Bearer {await access_token()}")
    return result


async def approve(tid: str, amount: int) -> dict:
    """승인 API. 여기서 예외가 나면 승인 성립 여부를 알 수 없다."""
    return await _post(f"/v1/payments/{tid}", {"amount": amount})


async def cancel_payment(tid: str, *, reason: str, order_id: str) -> dict:
    """전액 취소. cancelAmt를 넘기지 않으면 전액취소로 동작한다."""
    return await _post(
        f"/v1/payments/{tid}/cancel",
        {"reason": reason, "orderId": order_id},
    )


async def net_cancel(order_id: str) -> dict:
    """망취소. 승인 요청 후 1시간 이내에만 유효하다."""
    return await _post("/v1/payments/netcancel", {"orderId": order_id})


# ---------------------------------------------------------------------------
# 주문 저장소
# ---------------------------------------------------------------------------
def create_order(user_id: int, email: str, quantity: int) -> dict:
    """수량을 검증하고 pending 주문을 만든다. 금액은 서버만 계산한다."""
    low, high = min_quantity(), max_quantity()
    if not isinstance(quantity, int) or quantity < low or quantity > max(low, high):
        raise ValueError(f"수량은 {low}~{max(low, high)}장 사이여야 합니다")

    price = unit_price()
    amount = price * quantity
    if amount < MIN_CARD_AMOUNT:
        raise ValueError(f"최소 결제금액은 {MIN_CARD_AMOUNT:,}원입니다")

    order_id = uuid.uuid4().hex
    goods_name = f"{GOODS_NAME} {quantity}장"

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO payments (
                   order_id, user_id, user_email, quantity, unit_price,
                   amount, goods_name
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_id, user_id, email, quantity, price, amount, goods_name),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "order_id": order_id,
        "quantity": quantity,
        "unit_price": price,
        "amount": amount,
        "goods_name": goods_name,
    }


def get_order(order_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM payments WHERE order_id = ?", (order_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_orders(user_id: int | None = None, limit: int = 50) -> list[dict]:
    conn = _get_conn()
    if user_id is None:
        rows = conn.execute(
            "SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def expire_stale_orders(older_than_minutes: int = _EXPIRY_MINUTES) -> int:
    """오래 방치된 pending 주문을 expired로 내린다. 정리한 건수를 돌려준다."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            """UPDATE payments
                  SET status='expired', updated_at=datetime('now')
                WHERE status='pending'
                  AND created_at < datetime('now', ?)""",
            (f"-{int(older_than_minutes)} minutes",),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def fail_order(
    order_id: str,
    reason: str,
    raw_auth: dict | None = None,
    raw_approve: dict | None = None,
) -> None:
    """아직 승인되지 않은 주문만 실패로 내린다. 이미 승인된 주문은 건드리지 않는다.

    승인이 거절된 경우 그 응답 원문까지 남긴다 — 사유 문구 하나로는 어느 단계에서
    무엇이 틀렸는지 되짚을 수 없다.
    """
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE payments
                  SET status='failed', fail_reason=?,
                      raw_auth=COALESCE(?, raw_auth),
                      raw_approve=COALESCE(?, raw_approve),
                      updated_at=datetime('now')
                WHERE order_id=? AND status IN (?, ?)""",
            (reason[:500], _dump(raw_auth), _dump(raw_approve), order_id, *_GRANTABLE),
        )
        conn.commit()
    finally:
        conn.close()


def _dump(payload: dict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, default=str)


def settle_order(
    order_id: str,
    *,
    tid: str,
    method: str | None,
    raw_approve: dict,
    raw_auth: dict | None = None,
) -> dict | None:
    """승인된 주문을 확정하고 이용권을 지급한다.

    지급이 실제로 일어났을 때만 주문 dict를 돌려준다. 이미 처리된 주문(returnUrl과
    웹훅이 겹친 경우)이면 None이다. 만료로 표시된 주문도 지급 대상이다 — 만료는
    정리용 라벨이지 승인 게이트가 아니다.

    auth.add_credits()를 쓰지 않는 이유는 상태 전이와 잔액 증가가 같은 트랜잭션
    안에 있어야 하기 때문이다. 별도 커넥션으로 나가면 그 사이에 다른 경로가
    같은 주문을 한 번 더 지급할 수 있다.
    """
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM payments WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None

        cur = conn.execute(
            """UPDATE payments
                  SET status='paid', tid=?, method=?, raw_approve=?,
                      raw_auth=COALESCE(?, raw_auth),
                      approved_at=datetime('now'), updated_at=datetime('now')
                WHERE order_id=? AND status IN (?, ?)""",
            (tid, method, _dump(raw_approve), _dump(raw_auth), order_id, *_GRANTABLE),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None

        quantity = int(row["quantity"])
        if row["user_id"] is None:
            # 결제창이 열려 있는 사이 탈퇴한 계정. 승인은 성립했으므로 거래 기록은
            # 남기되 지급 대상이 없다. 환불은 관리자가 판단한다.
            conn.execute(
                "UPDATE payments SET granted=0 WHERE order_id=?", (order_id,)
            )
            conn.commit()
            logger.error(
                "Payment %s approved but the buyer account is gone; manual refund needed",
                order_id,
            )
            return dict(row)

        # 총 잔액과 구매분을 함께 올린다. 구매분은 월 충전 바닥값에서 제외되므로
        # 이용권을 사도 다음 달 무료 충전이 그대로 들어온다.
        conn.execute(
            """UPDATE users
                  SET credits = credits + ?, paid_credits = paid_credits + ?,
                      updated_at = datetime('now')
                WHERE id = ?""",
            (quantity, quantity, row["user_id"]),
        )
        conn.execute(
            "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
            (row["user_id"], quantity, "purchase", f"이용권 구매 ({quantity}장)"),
        )
        conn.execute(
            "UPDATE payments SET granted=? WHERE order_id=?", (quantity, order_id)
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reclaim_order(order_id: str, *, reason: str, raw_cancel: dict | None) -> dict | None:
    """취소된 주문의 이용권을 회수한다. 실제로 회수했을 때만 dict를 돌려준다.

    회수는 남아 있는 **구매분** 범위에서만 한다. 이미 써버린 몫을 무료 충전분에서
    빼오면 결제와 무관한 이용권을 뺏는 셈이고, 잔액을 음수로 만들면 질문이 막힐
    뿐 아니라 이후 충전분까지 갉아먹는다. 실제 회수량은 payments.reclaimed와
    이용 내역 메모에 남는다.
    """
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM payments WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return None

        cur = conn.execute(
            """UPDATE payments
                  SET status='cancelled', cancel_reason=?, raw_cancel=?,
                      cancelled_at=datetime('now'), updated_at=datetime('now')
                WHERE order_id=? AND status='paid'""",
            (reason[:500], _dump(raw_cancel), order_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None

        granted = int(row["granted"] or 0)
        reclaimed = 0
        if row["user_id"] is not None and granted > 0:
            balance_row = conn.execute(
                "SELECT paid_credits FROM users WHERE id = ?", (row["user_id"],)
            ).fetchone()
            paid_balance = int(balance_row["paid_credits"]) if balance_row else 0
            reclaimed = max(0, min(granted, paid_balance))
            if reclaimed:
                conn.execute(
                    """UPDATE users
                          SET credits = credits - ?, paid_credits = paid_credits - ?,
                              updated_at = datetime('now')
                        WHERE id = ?""",
                    (reclaimed, reclaimed, row["user_id"]),
                )
                conn.execute(
                    "INSERT INTO token_transactions (user_id, amount, type, memo) VALUES (?, ?, ?, ?)",
                    (
                        row["user_id"],
                        -reclaimed,
                        "cancel",
                        f"결제 취소 회수 ({reclaimed}장)",
                    ),
                )

        conn.execute(
            "UPDATE payments SET reclaimed=? WHERE order_id=?", (reclaimed, order_id)
        )
        conn.commit()
        result = dict(row)
        result["reclaimed"] = reclaimed
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 흐름 처리
# ---------------------------------------------------------------------------
async def process_return(form: dict) -> tuple[str | None, str]:
    """returnUrl POST 처리. (order_id, 결과코드)를 돌려준다.

    결과코드는 결과 페이지 쿼리스트링으로 그대로 나간다: paid | failed | invalid.

    로그인 세션에 의존하지 않는다. 나이스페이 도메인에서 넘어오는 top-level
    cross-site POST라 SameSite=Lax 쿠키가 실려 오지 않기 때문이다. 소유자는
    주문 행의 user_id로만 판단한다.
    """
    order_id = (form.get("orderId") or "").strip()
    if not order_id:
        logger.warning("Payment return without an orderId")
        return None, "invalid"

    order = get_order(order_id)
    if order is None:
        logger.warning("Payment return for an unknown order %s", order_id)
        return None, "invalid"

    auth_result_code = form.get("authResultCode")
    if auth_result_code != "0000":
        message = form.get("authResultMsg") or "결제가 취소되었거나 인증에 실패했습니다"
        fail_order(order_id, message, form)
        return order_id, "failed"

    raw_amount = form.get("amount") or ""
    try:
        amount = int(raw_amount)
    except (TypeError, ValueError):
        fail_order(order_id, "결제 금액을 해석할 수 없습니다", form)
        return order_id, "failed"

    if amount != int(order["amount"]):
        # 결제창에 넘긴 금액이 바뀐 채 돌아온 경우. 승인 API를 부르지 않으므로
        # 결제는 성립하지 않는다.
        logger.error(
            "Payment %s amount mismatch: order=%s returned=%s",
            order_id,
            order["amount"],
            amount,
        )
        fail_order(order_id, "결제 금액이 주문과 일치하지 않습니다", form)
        return order_id, "failed"

    # 서명은 나이스페이가 보낸 amount 문자열 그대로로 만든다.
    expected = auth_signature(form.get("authToken") or "", raw_amount)
    if not signature_matches(expected, form.get("signature")):
        logger.error("Payment %s signature verification failed", order_id)
        fail_order(order_id, "결제 인증 서명 검증에 실패했습니다", form)
        return order_id, "failed"

    tid = (form.get("tid") or "").strip()
    if not tid:
        fail_order(order_id, "거래키가 없습니다", form)
        return order_id, "failed"

    try:
        result = await approve(tid, int(order["amount"]))
    except Exception:
        # 승인 성립 여부를 알 수 없는 상태다. 망취소로 문제 거래를 정리한다.
        logger.exception("Payment %s approval request failed", order_id)
        try:
            await net_cancel(order_id)
        except Exception:
            logger.exception("Payment %s net cancel failed", order_id)
        fail_order(order_id, "승인 응답을 받지 못해 망취소를 요청했습니다", form)
        return order_id, "failed"

    if result.get("resultCode") != "0000" or result.get("status") != "paid":
        logger.error(
            "Payment %s approval rejected: %s %s",
            order_id,
            result.get("resultCode"),
            result.get("resultMsg"),
        )
        fail_order(
            order_id,
            result.get("resultMsg") or "결제 승인이 거절되었습니다",
            form,
            result,
        )
        return order_id, "failed"

    # 응답에 서명이 실려 오면 검증한다. 여기서 어긋나면 승인은 이미 성립한
    # 상태이므로 지급하지 않고 관리자 확인 대상으로 남긴다.
    response_signature = result.get("signature")
    if response_signature:
        expected_result = result_signature(
            tid, result.get("amount", ""), result.get("ediDate", "")
        )
        if not signature_matches(expected_result, response_signature):
            logger.error(
                "Payment %s approval response signature mismatch; not granting", order_id
            )
            fail_order(order_id, "승인 응답 서명 검증에 실패했습니다", form, result)
            return order_id, "failed"

    settle_order(
        order_id,
        tid=tid,
        method=result.get("payMethod") or form.get("payMethod"),
        raw_approve=result,
        raw_auth=form,
    )
    return order_id, "paid"


def process_webhook(payload: dict) -> None:
    """웹훅 처리. 브라우저가 returnUrl을 완주하지 못한 승인을 건져 올린다.

    서버승인 모델에서는 우리가 승인 API를 부르지 않으면 결제가 성립하지 않는다.
    따라서 여기로 들어오는 paid 웹훅의 주문이 아직 pending이라면, 승인은 났는데
    returnUrl 처리가 중간에 끊긴 경우다.
    """
    order_id = (payload.get("orderId") or "").strip()
    if not order_id:
        return

    order = get_order(order_id)
    if order is None:
        logger.warning("Webhook for an unknown order %s", order_id)
        return

    tid = (payload.get("tid") or "").strip()
    signature = payload.get("signature")
    if signature:
        expected = result_signature(
            tid, payload.get("amount", ""), payload.get("ediDate", "")
        )
        if not signature_matches(expected, signature):
            logger.error("Webhook %s signature verification failed", order_id)
            return

    try:
        amount = int(payload.get("amount"))
    except (TypeError, ValueError):
        logger.error("Webhook %s has an unreadable amount", order_id)
        return

    status = payload.get("status")

    if status == "paid":
        if amount != int(order["amount"]):
            logger.error(
                "Webhook %s amount mismatch: order=%s webhook=%s",
                order_id,
                order["amount"],
                amount,
            )
            return
        settled = settle_order(
            order_id,
            tid=tid,
            method=payload.get("payMethod"),
            raw_approve=payload,
        )
        if settled:
            logger.warning(
                "Payment %s was settled by the webhook, not by returnUrl", order_id
            )
    elif status in ("cancelled", "partialCancelled"):
        # 나이스페이 관리자 화면에서 직접 취소한 경우가 여기로 들어온다.
        reclaimed = reclaim_order(
            order_id, reason="나이스페이 취소 통보", raw_cancel=payload
        )
        if reclaimed:
            logger.warning(
                "Payment %s was cancelled outside the admin page; reclaimed %s credits",
                order_id,
                reclaimed["reclaimed"],
            )


async def admin_cancel(order_id: str, reason: str) -> dict:
    """관리자 전액 취소. 취소 API가 성립한 뒤에만 이용권을 회수한다."""
    order = get_order(order_id)
    if order is None:
        return {"ok": False, "error": "주문을 찾을 수 없습니다"}
    if order["status"] != _PAID:
        return {"ok": False, "error": "결제 완료 상태의 주문만 취소할 수 있습니다"}
    if not order["tid"]:
        return {"ok": False, "error": "거래키가 없어 취소할 수 없습니다"}

    try:
        result = await cancel_payment(
            order["tid"], reason=reason, order_id=order_id
        )
    except Exception:
        logger.exception("Payment %s cancel request failed", order_id)
        return {"ok": False, "error": "취소 요청에 실패했습니다"}

    if result.get("resultCode") != "0000":
        return {
            "ok": False,
            "error": result.get("resultMsg") or "취소가 거절되었습니다",
        }

    reclaimed = reclaim_order(order_id, reason=reason, raw_cancel=result)
    if reclaimed is None:
        # 웹훅이 한발 먼저 회수한 경우. 취소 자체는 성립했다.
        return {"ok": True, "reclaimed": 0, "already": True}
    return {"ok": True, "reclaimed": reclaimed["reclaimed"]}
