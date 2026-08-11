"""Monthly and immediate credit refill tests."""

from __future__ import annotations

from datetime import datetime, timezone

from src import auth


def _init_test_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth, "DB_PATH", str(tmp_path / "auth.db"))
    auth.init_db()
    auth.init_site_settings()


def _add_user(conn, google_id: str, credits: int) -> int:
    return conn.execute(
        "INSERT INTO users (google_id, email, name, credits) VALUES (?, ?, ?, ?)",
        (google_id, f"{google_id}@example.com", google_id, credits),
    ).lastrowid


def test_monthly_refill_only_runs_once_on_first_day(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    assert auth.get_monthly_refill_credits() == 20

    conn = auth._get_conn()
    low_user = _add_user(conn, "low", 3)
    exact_user = _add_user(conn, "exact", 20)
    high_user = _add_user(conn, "high", 27)
    conn.commit()
    conn.close()

    not_due = auth.apply_monthly_credit_refill(
        datetime(2026, 8, 31, 23, 59, tzinfo=auth.KST)
    )
    assert not_due["reason"] == "not_due"

    applied = auth.apply_monthly_credit_refill(
        datetime(2026, 8, 31, 15, 1, tzinfo=timezone.utc)
    )
    assert applied == {
        "applied": True,
        "reason": "applied",
        "period": "2026-09",
        "target_credits": 20,
        "affected_users": 1,
        "total_credits": 17,
    }

    duplicate = auth.apply_monthly_credit_refill(
        datetime(2026, 9, 1, 12, 0, tzinfo=auth.KST)
    )
    assert duplicate["reason"] == "already_applied"

    conn = auth._get_conn()
    balances = {
        row["id"]: row["credits"]
        for row in conn.execute("SELECT id, credits FROM users").fetchall()
    }
    transactions = conn.execute(
        "SELECT user_id, amount, type, memo FROM token_transactions ORDER BY id"
    ).fetchall()
    refill_runs = conn.execute("SELECT * FROM monthly_credit_refills").fetchall()
    conn.close()

    assert balances == {low_user: 20, exact_user: 20, high_user: 27}
    assert len(transactions) == 1
    assert dict(transactions[0]) == {
        "user_id": low_user,
        "amount": 17,
        "type": "monthly_refill",
        "memo": "월 기본 이용권 충전 (2026-09)",
    }
    assert len(refill_runs) == 1


def test_admin_can_refill_immediately_without_consuming_monthly_run(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    auth.set_site_setting("monthly_refill_credits", "24")

    conn = auth._get_conn()
    low_user = _add_user(conn, "low", 3)
    high_user = _add_user(conn, "high", 30)
    conn.commit()
    conn.close()

    result = auth.admin_refill_credits_to_floor(auth.get_monthly_refill_credits())
    assert result == {
        "target_credits": 24,
        "affected_users": 1,
        "total_credits": 21,
    }

    conn = auth._get_conn()
    balances = {
        row["id"]: row["credits"]
        for row in conn.execute("SELECT id, credits FROM users").fetchall()
    }
    transaction = conn.execute(
        "SELECT user_id, amount, type, memo FROM token_transactions"
    ).fetchone()
    refill_count = conn.execute("SELECT COUNT(*) AS count FROM monthly_credit_refills").fetchone()
    conn.close()

    assert balances == {low_user: 24, high_user: 30}
    assert dict(transaction) == {
        "user_id": low_user,
        "amount": 21,
        "type": "admin_refill",
        "memo": "관리자 즉시 기본 이용권 충전",
    }
    assert refill_count["count"] == 0

    monthly = auth.apply_monthly_credit_refill(
        datetime(2026, 9, 1, 0, 1, tzinfo=auth.KST)
    )
    assert monthly["applied"] is True
    assert monthly["target_credits"] == 24
    assert monthly["affected_users"] == 0
