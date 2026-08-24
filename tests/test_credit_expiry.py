"""구매 이용권의 유효기간 회귀 테스트.

나이스페이 입점기준이 단건결제 상품의 제공기간을 3개월로 제한한다. 이 파일은
"만료가 실제로 일어나는가"와 "만료가 무료 충전분을 건드리지 않는가"를 본다.
후자가 깨지면 이용자는 사지도 않은 이용권을 잃는다.
"""

from __future__ import annotations

import asyncio

from src import auth, payments


def _init(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "expiry.db"))
    monkeypatch.setenv("NICEPAY_CLIENT_ID", "R2_testclient")
    monkeypatch.setenv("NICEPAY_SECRET_KEY", "testsecret")
    monkeypatch.setenv("NICEPAY_API_BASE", "https://sandbox-api.nicepay.co.kr")
    auth.init_db()
    auth.init_site_settings()
    # 단가를 카드사 최소 승인금액과 같게 두면 최소 구매수량이 1장이 되어, 테스트가
    # 만료 동작에만 집중할 수 있다.
    auth.set_site_setting("credit_unit_price", str(payments.MIN_CARD_AMOUNT))


def _add_user(credits: int = 0, google_id: str = "buyer") -> int:
    conn = auth._get_conn()
    user_id = conn.execute(
        "INSERT INTO users (google_id, email, name, credits) VALUES (?, ?, ?, ?)",
        (google_id, f"{google_id}@example.com", google_id, credits),
    ).lastrowid
    conn.commit()
    conn.close()
    return user_id


def _balances(user_id: int) -> tuple[int, int]:
    conn = auth._get_conn()
    row = conn.execute(
        "SELECT credits, paid_credits FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return int(row["credits"]), int(row["paid_credits"])


def _lots(user_id: int) -> list[tuple[int, str]]:
    conn = auth._get_conn()
    rows = conn.execute(
        "SELECT remaining, expires_at FROM credit_lots WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    conn.close()
    return [(int(r["remaining"]), r["expires_at"]) for r in rows]


def _age_lot(user_id: int, *, days: int) -> None:
    """Move a lot's expiry into the past so the sweep can see it."""
    conn = auth._get_conn()
    conn.execute(
        "UPDATE credit_lots SET expires_at = datetime('now', ?) WHERE user_id = ?",
        (f"-{days} days", user_id),
    )
    conn.commit()
    conn.close()


def _approved(order: dict) -> dict:
    return {
        "resultCode": "0000",
        "resultMsg": "정상 처리되었습니다",
        "status": "paid",
        "tid": "tid-1",
        "orderId": order["order_id"],
        "amount": order["amount"],
        "payMethod": "card",
        "ediDate": "2026-08-19T12:00:00.000+0900",
    }


def _return_form(order: dict) -> dict:
    amount = str(order["amount"])
    return {
        "authResultCode": "0000",
        "authResultMsg": "인증 성공",
        "tid": "tid-1",
        "clientId": payments.client_id(),
        "orderId": order["order_id"],
        "amount": amount,
        "authToken": "authtoken",
        "signature": payments.auth_signature("authtoken", amount),
    }


def _settle(monkeypatch, user_id: int, quantity: int = 10) -> dict:
    order = payments.create_order(user_id, "buyer@example.com", quantity)
    response = _approved(order)

    async def fake_approve(tid, amount):
        return response

    monkeypatch.setattr(payments, "approve", fake_approve)
    asyncio.run(payments.process_return(_return_form(order)))
    return order


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def test_validity_days_is_clamped_to_the_review_ceiling(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    assert auth.get_credit_validity_days() == 90

    auth.set_site_setting("credit_validity_days", "30")
    assert auth.get_credit_validity_days() == 30

    # 90일을 넘기는 값은 심사에서 판매불가 판정을 받는 상품이 된다.
    auth.set_site_setting("credit_validity_days", "365")
    assert auth.get_credit_validity_days() == 90

    auth.set_site_setting("credit_validity_days", "0")
    assert auth.get_credit_validity_days() == 1

    auth.set_site_setting("credit_validity_days", "nonsense")
    assert auth.get_credit_validity_days() == 90


def test_public_config_publishes_the_validity_period(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    auth.set_site_setting("credit_validity_days", "45")
    assert payments.public_config()["validity_days"] == 45


# ---------------------------------------------------------------------------
# 지급
# ---------------------------------------------------------------------------
def test_a_purchase_records_a_lot_with_an_expiry(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=3)
    _settle(monkeypatch, user_id, quantity=10)

    assert _balances(user_id) == (13, 10)
    lots = _lots(user_id)
    assert len(lots) == 1
    assert lots[0][0] == 10
    assert lots[0][1] > ""  # 만료일이 비어 있으면 만료가 영원히 오지 않는다


def test_two_purchases_keep_separate_expiries(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    auth.set_site_setting("credit_validity_days", "90")
    _settle(monkeypatch, user_id, quantity=10)
    auth.set_site_setting("credit_validity_days", "30")
    _settle(monkeypatch, user_id, quantity=5)

    lots = _lots(user_id)
    assert [q for q, _ in lots] == [10, 5]
    # 두 번째 구매가 더 짧은 기간으로 팔렸으니 먼저 만료돼야 한다.
    assert lots[1][1] < lots[0][1]


# ---------------------------------------------------------------------------
# 차감
# ---------------------------------------------------------------------------
def test_spending_draws_free_first_then_the_soonest_lot(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=2)
    auth.set_site_setting("credit_validity_days", "90")
    _settle(monkeypatch, user_id, quantity=4)
    auth.set_site_setting("credit_validity_days", "10")
    _settle(monkeypatch, user_id, quantity=4)
    assert _balances(user_id) == (10, 8)

    # 무료 2장만 쓰는 동안 구매분은 그대로다.
    assert auth.deduct_credit(user_id, 2) is True
    assert _balances(user_id) == (8, 8)
    assert [q for q, _ in _lots(user_id)] == [4, 4]

    # 다음 한 장은 구매분에서 나가고, 먼저 만료되는 쪽(두 번째 구매)이 줄어야 한다.
    assert auth.deduct_credit(user_id, 1) is True
    assert _balances(user_id) == (7, 7)
    assert [q for q, _ in _lots(user_id)] == [4, 3]


def test_spending_more_than_the_balance_changes_nothing(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=1)
    _settle(monkeypatch, user_id, quantity=2)
    before_balances = _balances(user_id)
    before_lots = _lots(user_id)

    assert auth.deduct_credit(user_id, 99) is False
    assert _balances(user_id) == before_balances
    assert _lots(user_id) == before_lots


# ---------------------------------------------------------------------------
# 만료
# ---------------------------------------------------------------------------
def test_the_sweep_destroys_expired_credits_and_spares_the_free_ones(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=6)
    _settle(monkeypatch, user_id, quantity=10)
    assert _balances(user_id) == (16, 10)

    _age_lot(user_id, days=1)
    result = auth.expire_credit_lots()

    assert result == {"users": 1, "credits": 10}
    # 무료 6장은 그대로. 이게 깨지면 사지도 않은 이용권을 잃는다.
    assert _balances(user_id) == (6, 0)
    assert [q for q, _ in _lots(user_id)] == [0]


def test_the_sweep_only_takes_what_is_left_after_spending(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    _settle(monkeypatch, user_id, quantity=10)
    assert auth.deduct_credit(user_id, 4) is True
    assert _balances(user_id) == (6, 6)

    _age_lot(user_id, days=1)
    assert auth.expire_credit_lots() == {"users": 1, "credits": 6}
    assert _balances(user_id) == (0, 0)


def test_the_sweep_leaves_live_lots_alone(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=1)
    _settle(monkeypatch, user_id, quantity=7)

    assert auth.expire_credit_lots() == {"users": 0, "credits": 0}
    assert _balances(user_id) == (8, 7)


def test_the_sweep_is_idempotent(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=2)
    _settle(monkeypatch, user_id, quantity=5)
    _age_lot(user_id, days=1)

    assert auth.expire_credit_lots()["credits"] == 5
    # 두 번째 스윕이 또 빼가면 무료분까지 갉아먹는다.
    assert auth.expire_credit_lots() == {"users": 0, "credits": 0}
    assert _balances(user_id) == (2, 0)


def test_expiry_is_recorded_in_the_usage_ledger(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    _settle(monkeypatch, user_id, quantity=3)
    _age_lot(user_id, days=1)
    auth.expire_credit_lots()

    conn = auth._get_conn()
    rows = conn.execute(
        "SELECT amount, type FROM token_transactions WHERE user_id = ? AND type = 'expire'",
        (user_id,),
    ).fetchall()
    conn.close()
    assert [(int(r["amount"]), r["type"]) for r in rows] == [(-3, "expire")]


def test_expired_credits_are_not_offered_to_the_ui(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    _settle(monkeypatch, user_id, quantity=4)
    assert len(auth.live_credit_lots(user_id)) == 1

    _age_lot(user_id, days=1)
    assert auth.live_credit_lots(user_id) == []


# ---------------------------------------------------------------------------
# 환불
# ---------------------------------------------------------------------------
def test_a_cancelled_order_takes_its_credits_out_of_the_lots(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user(credits=2)
    order = _settle(monkeypatch, user_id, quantity=10)
    assert _balances(user_id) == (12, 10)

    payments.reclaim_order(order["order_id"], reason="테스트 취소", raw_cancel=None)

    assert _balances(user_id) == (2, 0)
    # 카운터만 줄고 lot 이 남으면 다음 스윕이 없는 이용권을 또 빼앗는다.
    assert [q for q, _ in _lots(user_id)] == [0]
    assert auth.expire_credit_lots() == {"users": 0, "credits": 0}
    assert _balances(user_id) == (2, 0)


def test_a_partly_spent_order_only_gives_back_what_remains(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    user_id = _add_user()
    order = _settle(monkeypatch, user_id, quantity=10)
    assert auth.deduct_credit(user_id, 4) is True

    payments.reclaim_order(order["order_id"], reason="테스트 취소", raw_cancel=None)
    assert _balances(user_id) == (0, 0)
    assert [q for q, _ in _lots(user_id)] == [0]
