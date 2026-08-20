"""
FastAPI server for KSAE Q&A chatbot.
"""

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from src.auth import (
    PRIVACY_CONSENT_VERSION,
    add_message,
    admin_bulk_set_credits,
    admin_refill_credits_to_floor,
    apply_monthly_credit_refill,
    get_user_by_id,
    refund_credit,
    admin_get_messages,
    admin_set_credits,
    check_db_health,
    clear_auth_cookie,
    complete_chat_turn,
    create_chat_turn,
    create_jwt,
    create_session,
    deduct_credit,
    delete_user_account,
    delete_session,
    get_all_site_settings,
    get_current_user,
    get_messages,
    get_monthly_refill_credits,
    get_recent_messages,
    get_site_setting,
    get_all_users_token_usage_by_model,
    get_admin_overview_stats,
    get_user_token_usage_by_model,
    get_or_create_user,
    get_user_by_google_id,
    get_session,
    get_transactions,
    get_user_usage_stats,
    init_admin_emails,
    init_db,
    init_oauth,
    init_site_settings,
    is_admin,
    is_unlimited_credits,
    list_all_sessions,
    list_all_users,
    list_chat_turns,
    list_sessions,
    oauth,
    set_auth_cookie,
    set_site_setting,
    update_session_title,
)
from src.chat import (
    CHAT_CREDIT_COST,
    ROUTING_MODEL_KEYS,
    MODEL_CONFIG,
    PRIMARY_MODEL_KEY,
    PROMPT_VERSION,
    get_public_collections,
    get_all_models_admin,
    get_health_status,
    init_model_settings,
    init_resources,
    is_model_available,
    search_and_stream,
    set_model_admin_settings,
)
from src import payments

load_dotenv()


def _ensure_jwt_secret() -> str:
    """Return JWT_SECRET from env, auto-generating and persisting to .env if absent."""
    secret = os.environ.get("JWT_SECRET")
    if secret:
        return secret

    secret = secrets.token_hex(32)
    os.environ["JWT_SECRET"] = secret

    env_path = os.path.join(os.path.dirname(__file__) or ".", ".env")
    with open(env_path, "a", encoding="utf-8") as f:
        f.write(f"\nJWT_SECRET={secret}\n")

    print(f"Generated new JWT_SECRET and saved to .env")
    return secret


JWT_SECRET = _ensure_jwt_secret()


async def _hourly_maintenance_worker() -> None:
    """Hourly housekeeping: the KST month-start refill and stale order cleanup.

    Both want the same cadence and neither is urgent, so they share one loop
    rather than two timers racing to wake the process.
    """
    while True:
        try:
            expired = await asyncio.to_thread(payments.expire_stale_orders)
            if expired:
                logger.info("Marked %s abandoned payment orders as expired", expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale payment order cleanup failed")

        try:
            result = await asyncio.to_thread(apply_monthly_credit_refill)
            if result["applied"]:
                logger.info(
                    "Monthly credit refill applied: period=%s target=%s users=%s credits=%s",
                    result["period"],
                    result["target_credits"],
                    result["affected_users"],
                    result["total_credits"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Monthly credit refill failed")

        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_oauth()
    init_admin_emails()
    init_site_settings()
    init_resources()
    init_model_settings()
    maintenance_task = asyncio.create_task(_hourly_maintenance_worker())
    try:
        yield
    finally:
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=JWT_SECRET,
    https_only=os.environ.get("HTTPS_ONLY", "").lower() in ("1", "true"),
)


@app.middleware("http")
async def fix_request_scheme(request: Request, call_next):
    """
    Ensure request.url_for uses https if the app is behind an HTTPS reverse proxy.
    """
    if request.headers.get("x-forwarded-proto") == "https":
        request.scope["scheme"] = "https"
    return await call_next(request)


# Prevent background LLM tasks from being garbage-collected
_background_tasks: set[asyncio.Task] = set()


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(default=7, ge=1, le=20)
    session_id: int | None = None
    collections: list[str] | None = None


class SessionPatch(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)


class PaymentOrderRequest(BaseModel):
    # 상·하한은 payments.create_order 가 설정값으로 다시 검증한다.  여기서는
    # 터무니없는 값이 DB까지 내려가지 않게만 막는다.
    quantity: int = Field(..., ge=1, le=100000)


class PaymentCancelRequest(BaseModel):
    # 나이스페이 취소사유는 100자까지다.
    reason: str = Field(default="관리자 취소", min_length=1, max_length=100)


class AdminCreditRequest(BaseModel):
    credits: int = Field(..., ge=0)
    memo: str = Field(default="관리자 조정", max_length=200)


class ModelToggleRequest(BaseModel):
    enabled: bool


class SiteSettingsRequest(BaseModel):
    default_credits: int = Field(..., ge=0, le=10000)
    monthly_refill_credits: int | None = Field(default=None, ge=0, le=10000)
    low_credit_threshold: int = Field(..., ge=0, le=10000)
    unlimited_credits: bool = Field(default=False)
    credit_unit_price: int | None = Field(default=None, ge=1, le=1000000)
    credit_max_quantity: int | None = Field(default=None, ge=1, le=100000)
    business: dict[str, str] | None = None


class BulkCreditRequest(BaseModel):
    credits: int = Field(..., ge=0)
    memo: str = Field(default="관리자 일괄 조정", max_length=200)


class PrivacyConsentRequest(BaseModel):
    privacy_consent: bool


class AccountDeleteRequest(BaseModel):
    confirmation: str = Field(..., max_length=20)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/account")
async def account_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse("static/account.html")


@app.get("/payments/result")
async def payment_result_page():
    """결제창이 돌려보낸 결과를 사람이 읽는 화면.  판정은 이미 서버에서 끝났다."""
    return FileResponse("static/payment-result.html")


@app.get("/policy")
async def policy_page():
    return FileResponse("static/policy.html")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.get("/api/auth/login")
async def auth_login(request: Request):
    redirect_uri = request.url_for("auth_callback")
    if request.headers.get("x-forwarded-proto") == "https":
        redirect_uri = str(redirect_uri).replace("http://", "https://")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/api/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")

    existing_user = get_user_by_google_id(userinfo["sub"])
    if not existing_user:
        request.session["pending_signup"] = {
            "google_id": userinfo["sub"],
            "email": userinfo["email"],
            "name": userinfo.get("name", userinfo["email"]),
            "picture": userinfo.get("picture"),
            "expires_at": int(time.time()) + 600,
        }
        return RedirectResponse(url="/signup/consent", status_code=302)

    user = get_or_create_user(
        google_id=userinfo["sub"],
        email=userinfo["email"],
        name=userinfo.get("name", userinfo["email"]),
        picture=userinfo.get("picture"),
    )

    jwt_token = create_jwt(user["id"])
    response = RedirectResponse(url="/", status_code=302)
    set_auth_cookie(response, jwt_token)
    return response


def _pending_signup(request: Request) -> dict | None:
    pending = request.session.get("pending_signup")
    if not isinstance(pending, dict) or int(pending.get("expires_at", 0)) < int(time.time()):
        request.session.pop("pending_signup", None)
        return None
    required = ("google_id", "email", "name")
    return pending if all(pending.get(key) for key in required) else None


@app.get("/signup/consent")
async def signup_consent_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=302)
    if not _pending_signup(request):
        return RedirectResponse(url="/", status_code=302)
    return FileResponse("static/signup.html")


@app.get("/api/auth/signup-pending")
async def signup_pending(request: Request):
    pending = _pending_signup(request)
    if not pending:
        return JSONResponse({"error": "가입 정보가 만료되었습니다. 다시 로그인해주세요."}, status_code=404)
    return {
        "name": pending["name"],
        "email": pending["email"],
        "picture": pending.get("picture"),
        "privacy_consent_version": PRIVACY_CONSENT_VERSION,
    }


@app.post("/api/auth/signup-consent")
async def signup_consent(body: PrivacyConsentRequest, request: Request):
    if not body.privacy_consent:
        return JSONResponse({"error": "필수 개인정보 수집·이용 동의가 필요합니다."}, status_code=400)
    pending = _pending_signup(request)
    if not pending:
        return JSONResponse({"error": "가입 정보가 만료되었습니다. 다시 로그인해주세요."}, status_code=404)

    user = get_or_create_user(
        google_id=pending["google_id"],
        email=pending["email"],
        name=pending["name"],
        picture=pending.get("picture"),
        privacy_consent_version=PRIVACY_CONSENT_VERSION,
    )
    request.session.pop("pending_signup", None)
    response = JSONResponse({"ok": True})
    set_auth_cookie(response, create_jwt(user["id"]))
    return response


@app.post("/api/auth/signup-cancel")
async def signup_cancel(request: Request):
    request.session.pop("pending_signup", None)
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"ok": True})
    clear_auth_cookie(response)
    return response


# ---------------------------------------------------------------------------
# User / Credits
# ---------------------------------------------------------------------------
@app.get("/api/me")
async def me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"user": None}, status_code=200)
    low_threshold = 5
    try:
        low_threshold = max(0, int(get_site_setting("low_credit_threshold")))
    except (ValueError, TypeError):
        pass
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "picture": user["picture"],
            "credits": user["credits"],
            # 총 잔액 중 구매분. 무료 충전분(credits - paid_credits)과 나눠 보여준다.
            "paid_credits": user["paid_credits"],
            "is_admin": is_admin(request) is not None,
        },
        "low_credit_threshold": low_threshold,
        "unlimited_credits": is_unlimited_credits(),
    }


@app.delete("/api/account")
async def account_delete(body: AccountDeleteRequest, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    if body.confirmation != "회원탈퇴":
        return JSONResponse({"error": "확인 문구를 정확히 입력해주세요."}, status_code=400)

    result = delete_user_account(user["id"])
    if result == "pending":
        return JSONResponse(
            {"error": "답변 생성이 끝난 뒤 다시 탈퇴해주세요."},
            status_code=409,
        )
    if result == "payment_pending":
        return JSONResponse(
            {"error": "진행 중인 결제가 끝난 뒤 다시 탈퇴해주세요."},
            status_code=409,
        )
    if result != "deleted":
        return JSONResponse({"error": "계정을 찾을 수 없습니다."}, status_code=404)
    response = JSONResponse({"ok": True})
    clear_auth_cookie(response)
    request.session.clear()
    return response


@app.get("/api/account/stats")
async def account_stats(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    return {"stats": get_user_usage_stats(user["id"])}


# ---------------------------------------------------------------------------
# Payments (NicePay 결제창 서버승인 — src/payments.py)
# ---------------------------------------------------------------------------
def _site_base(request: Request) -> str:
    """returnUrl 은 절대 경로여야 한다.  SITE_URL 이 없으면 요청 기준으로 만든다."""
    configured = os.environ.get("SITE_URL", "").strip()
    return configured.rstrip("/") if configured else str(request.base_url).rstrip("/")


def _public_payment(row: dict) -> dict:
    """사용자에게 보이는 결제 내역.  거래키와 원문은 내보내지 않는다."""
    return {
        "order_id": row["order_id"],
        "goods_name": row["goods_name"],
        "quantity": row["quantity"],
        "amount": row["amount"],
        "status": row["status"],
        "method": row["method"],
        "created_at": row["created_at"],
        "approved_at": row["approved_at"],
        "cancelled_at": row["cancelled_at"],
        # 실패 사유는 본인 주문에 대한 안내문이라 그대로 보여준다.
        "fail_reason": row["fail_reason"],
    }


def _admin_payment(row: dict) -> dict:
    """관리자 화면용.  취소에 필요한 값까지 싣되 원문 JSON 은 뺀다."""
    return _public_payment(row) | {
        "user_id": row["user_id"],
        "user_email": row["user_email"],
        "unit_price": row["unit_price"],
        "tid": row["tid"],
        "granted": row["granted"],
        "reclaimed": row["reclaimed"],
        "cancel_reason": row["cancel_reason"],
    }


@app.get("/api/payments/config")
async def payments_config():
    return {"payment": payments.public_config()}


@app.post("/api/payments/orders")
async def payments_create_order(body: PaymentOrderRequest, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    if not payments.is_configured():
        return JSONResponse({"error": "결제가 준비되지 않았습니다"}, status_code=503)

    try:
        order = payments.create_order(user["id"], user["email"], body.quantity)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return {
        "order_id": order["order_id"],
        "amount": order["amount"],
        "quantity": order["quantity"],
        "goods_name": order["goods_name"],
        "client_id": payments.client_id(),
        "method": payments.PAY_METHOD,
        "return_url": f"{_site_base(request)}/api/payments/return",
        "buyer_name": user["name"],
        "buyer_email": user["email"],
    }


@app.post("/api/payments/return")
async def payments_return(request: Request):
    """결제창 인증 결과.  나이스페이 도메인에서 넘어오는 cross-site POST 라
    SameSite=Lax 인 로그인 쿠키가 실려 오지 않는다.  소유자 판단은 주문 행이 한다."""
    form = await request.form()
    fields = {key: value for key, value in form.items() if isinstance(value, str)}
    order_id, result = await payments.process_return(fields)

    query = f"?result={result}"
    if order_id:
        query += f"&order={order_id}"
    return RedirectResponse(url=f"/payments/result{query}", status_code=303)


@app.post("/api/payments/webhook")
async def payments_webhook(request: Request):
    """승인·취소 비동기 통보.  본문에 "OK" 가 없으면 나이스페이가 실패로 보고
    재전송하므로, 처리 중 예외는 삼키지 않고 그대로 5xx 로 흘려 재전송을 받는다."""
    payload = await request.json()
    if isinstance(payload, dict):
        await asyncio.to_thread(payments.process_webhook, payload)
    return PlainTextResponse("OK", media_type="text/html; charset=utf-8")


@app.get("/api/payments")
async def payments_list(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    return {"payments": [_public_payment(row) for row in payments.list_orders(user["id"])]}


@app.get("/api/policy")
async def policy_info():
    return {"business": payments.business_info(), "payment": payments.public_config()}


@app.get("/api/transactions")
async def transactions(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    return {"transactions": get_transactions(user["id"], public_view=True)}


# ---------------------------------------------------------------------------
# Session routes
# ---------------------------------------------------------------------------
@app.get("/api/sessions")
async def sessions_list(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    return {"sessions": list_sessions(user["id"])}


@app.post("/api/sessions")
async def sessions_create(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    session = create_session(user["id"])
    return {"session": session}


@app.get("/api/sessions/{session_id}/messages")
async def sessions_messages(session_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    session = get_session(session_id, user["id"])
    if not session:
        return JSONResponse({"error": "세션을 찾을 수 없습니다"}, status_code=404)
    return {"messages": get_messages(session_id)}


@app.delete("/api/sessions/{session_id}")
async def sessions_delete(session_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    if not delete_session(session_id, user["id"]):
        return JSONResponse({"error": "세션을 찾을 수 없습니다"}, status_code=404)
    return {"ok": True}


@app.patch("/api/sessions/{session_id}")
async def sessions_update(session_id: int, body: SessionPatch, request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    if not update_session_title(session_id, user["id"], body.title):
        return JSONResponse({"error": "세션을 찾을 수 없습니다"}, status_code=404)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat (with auth + credit check + session persistence)
# ---------------------------------------------------------------------------
@app.get("/api/collections")
async def collections_list():
    """검색 소스 목록. 프론트엔드 칩·안내문은 이 응답으로 렌더된다."""
    return {"collections": get_public_collections()}


@app.post("/api/chat")
async def chat(request: Request, req: ChatRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)

    # Model routing is server-owned: Flash is always attempted first and Pro
    # is the only fallback. Client payloads cannot select or price a model.
    if not any(is_model_available(key) for key in ROUTING_MODEL_KEYS):
        return JSONResponse({"error": "답변 모델을 현재 사용할 수 없습니다."}, status_code=503)

    invalid_collections = set()
    if req.collections:
        public_collections = {entry["key"] for entry in get_public_collections()}
        invalid_collections = set(req.collections) - public_collections
    if invalid_collections:
        return JSONResponse({"error": "지원하지 않는 검색 소스가 포함되어 있습니다"}, status_code=400)
    # Validate ownership before charging.  Previously a stale/foreign session
    # returned 404 only after consuming the user's credits.
    session_id = req.session_id
    session = None
    if session_id:
        session = get_session(session_id, user["id"])
        if not session:
            return JSONResponse({"error": "세션을 찾을 수 없습니다"}, status_code=404)

    credits_needed = CHAT_CREDIT_COST
    if not deduct_credit(user["id"], credits_needed, "질문"):
        return JSONResponse({"error": "이용권이 부족합니다"}, status_code=402)

    try:
        if session is None:
            session = create_session(user["id"], req.query[:50])
            session_id = session["id"]

        history = []
        if req.session_id:
            for msg in get_recent_messages(session_id)[-10:]:
                history.append({"role": msg["role"], "content": msg["content"]})

        turn_id = uuid.uuid4().hex
        user_message = add_message(session_id, "user", req.query, turn_id=turn_id)
        create_chat_turn(
            turn_id,
            session_id,
            user_message["id"],
            req.query,
            PRIMARY_MODEL_KEY,
            collections=json.dumps(req.collections, ensure_ascii=False) if req.collections else None,
            category=None,
            competition=None,
            confidence=None,
            prompt_version=PROMPT_VERSION,
        )
        if req.session_id and session["title"] == "새 대화":
            update_session_title(session_id, user["id"], req.query[:50])
    except Exception:
        logger.exception("Failed to initialize chat turn")
        refund_credit(user["id"], credits_needed, "요청 저장 실패 환불")
        return JSONResponse({"error": "질문을 저장하지 못했습니다. 다시 시도해주세요."}, status_code=500)

    updated_user = get_user_by_id(user["id"])
    remaining = updated_user["credits"] if updated_user else 0

    # Decouple LLM consumption from client delivery via a queue.
    # If the client disconnects, the LLM task keeps running in the background
    # so the full response is persisted and visible when the user returns.
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def consume_llm():
        full_text = ""
        sources_json = None
        source_ids_json = None
        input_tokens = None
        output_tokens = None
        thinking_tokens = None
        has_error = False
        has_fallback = False
        model_text_emitted = False
        rewritten_query = None
        resolved_model = None
        resolved_model_id = None
        finish_reason = None
        competition = None
        retrieval_status = "pending"
        retrieval_ms = None
        rerank_ms = None
        first_token_ms = None
        error_provider = None
        error_code = None
        error_message = None
        started_at = time.monotonic()
        generation_started_at = None

        def parse_event(event: str) -> tuple[str | None, object | None]:
            event_type = None
            data_parts = []
            for line in event.splitlines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_parts.append(line[6:])
            if not data_parts:
                return event_type, None
            raw = "\n".join(data_parts)
            try:
                return event_type, json.loads(raw)
            except json.JSONDecodeError:
                return event_type, raw

        try:
            async for event in search_and_stream(
                req.query,
                req.limit,
                min_score=0.5,
                history=history,
                collections=req.collections,
                category=None,
                competition=None,
            ):
                event_type, payload = parse_event(event)

                if event_type == "error":
                    has_error = True
                    details = payload if isinstance(payload, dict) else {}
                    error_provider = str(details.get("provider") or "application")
                    error_code = str(details.get("code") or "stream_error")
                    error_message = str(details.get("message") or payload or "unknown error")[:1000]
                    user_message = str(details.get("user_message") or "답변 생성 중 오류가 발생했습니다.")
                    display = user_message if not full_text else f"\n\n---\n*{user_message}*"
                    full_text += display
                    await queue.put(f"event: token\ndata: {json.dumps(display, ensure_ascii=False)}\n\n")
                elif event_type in {"rewrite", "sources", "retrieval", "token"}:
                    await queue.put(event)

                if event_type == "sources" and isinstance(payload, list):
                    sources_json = json.dumps(payload, ensure_ascii=False)
                    source_ids_json = json.dumps(
                        [source.get("source_key") for source in payload if source.get("source_key")],
                        ensure_ascii=False,
                    )
                elif event_type == "token" and isinstance(payload, str):
                    model_text_emitted = True
                    if first_token_ms is None:
                        first_token_ms = round((time.monotonic() - started_at) * 1000)
                    full_text += payload
                elif event_type == "usage" and isinstance(payload, dict):
                    input_tokens = payload.get("input_tokens")
                    output_tokens = payload.get("output_tokens")
                    thinking_tokens = payload.get("thinking_tokens")
                    resolved_model = payload.get("resolved_model") or resolved_model
                    resolved_model_id = payload.get("resolved_model_id") or resolved_model_id
                    finish_reason = payload.get("finish_reason")
                elif event_type == "rewrite" and isinstance(payload, str):
                    rewritten_query = payload
                elif event_type == "retrieval" and isinstance(payload, dict):
                    retrieval_status = str(payload.get("status") or "unknown")
                    retrieval_ms = payload.get("retrieval_ms")
                    rerank_ms = payload.get("rerank_ms")
                    competition = payload.get("competition") or competition
                    generation_started_at = time.monotonic()
                elif event_type == "model" and isinstance(payload, dict):
                    resolved_model = payload.get("resolved_model") or resolved_model
                    resolved_model_id = payload.get("resolved_model_id") or resolved_model_id
                elif event_type == "fallback" and isinstance(payload, dict):
                    has_fallback = True
                    reason = payload.get("reason") if isinstance(payload.get("reason"), dict) else {}
                    error_provider = str(reason.get("provider") or "model")
                    error_code = str(reason.get("code") or "fallback")
                    error_message = str(reason.get("message") or "primary model failed; fallback used")[:1000]
        except Exception as exc:
            logger.exception("LLM streaming error in background task")
            has_error = True
            error_provider = "application"
            error_code = "internal_error"
            error_message = str(exc)[:1000]
            if not full_text:
                full_text = "답변 생성 중 내부 오류가 발생했습니다. 이용권은 환불되었습니다."
                await queue.put(f"event: token\ndata: {json.dumps(full_text, ensure_ascii=False)}\n\n")
        finally:
            if has_error:
                refund_credit(user["id"], credits_needed, "오류 환불")

            total_ms = round((time.monotonic() - started_at) * 1000)
            generation_ms = (
                round((time.monotonic() - generation_started_at) * 1000)
                if generation_started_at is not None
                else None
            )
            status = "partial_error" if has_error and model_text_emitted else "error" if has_error else "success_fallback" if has_fallback else "success"
            assistant_message = None
            try:
                assistant_message = add_message(
                    session_id,
                    "assistant",
                    full_text,
                    sources_json,
                    input_tokens,
                    output_tokens,
                    thinking_tokens,
                    model=resolved_model or PRIMARY_MODEL_KEY,
                    rewritten_query=rewritten_query,
                    turn_id=turn_id,
                )
                complete_chat_turn(
                    turn_id,
                    assistant_message_id=assistant_message["id"],
                    resolved_model=resolved_model or PRIMARY_MODEL_KEY,
                    resolved_model_id=resolved_model_id,
                    rewritten_query=rewritten_query,
                    competition=competition,
                    source_ids=source_ids_json,
                    retrieval_status=retrieval_status,
                    status=status,
                    error_provider=error_provider,
                    error_code=error_code,
                    error_message=error_message,
                    finish_reason=finish_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    thinking_tokens=thinking_tokens,
                    retrieval_ms=retrieval_ms,
                    rerank_ms=rerank_ms,
                    first_token_ms=first_token_ms,
                    generation_ms=generation_ms,
                    total_ms=total_ms,
                )
            except Exception:
                logger.exception("Failed to persist completed chat turn %s", turn_id)

            balance = get_user_by_id(user["id"])
            if balance:
                await queue.put(f"event: credits\ndata: {json.dumps({'remaining': balance['credits']})}\n\n")
            await queue.put(f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n")
            await queue.put("event: done\ndata: {}\n\n")
            await queue.put(None)  # sentinel: stream finished

    task = asyncio.create_task(consume_llm())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    async def sse_generator():
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected — LLM task continues in background
            pass

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Credits-Remaining": str(remaining),
        },
    )


@app.get("/live")
async def live():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    health = await asyncio.to_thread(get_health_status)
    health["database"] = await asyncio.to_thread(check_db_health)
    ready_status = health["database"] and health["qdrant"] and health["any_model_available"]
    return JSONResponse({"status": "ready" if ready_status else "not_ready", **health}, status_code=200 if ready_status else 503)


@app.get("/api/health")
async def health():
    health_status = await asyncio.to_thread(get_health_status)
    database = await asyncio.to_thread(check_db_health)
    healthy = database and health_status["qdrant"] and health_status["any_model_available"]
    return {"status": "ok" if healthy else "degraded", "database": database, **health_status}


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.get("/admin")
async def admin_page(request: Request):
    user = is_admin(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)
    return FileResponse("static/admin.html")


@app.get("/api/admin/check")
async def admin_check(request: Request):
    user = is_admin(request)
    if not user:
        return JSONResponse({"admin": False}, status_code=403)
    return {"admin": True, "email": user["email"]}


@app.get("/api/admin/users")
async def admin_users(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    users = list_all_users()
    usage_map = get_all_users_token_usage_by_model()
    for u in users:
        u["model_usage"] = usage_map.get(u["id"], [])
    return {"users": users}


@app.get("/api/admin/overview")
async def admin_overview(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    period = request.query_params.get("period", "30d")
    period_days = {"7d": 7, "30d": 30, "all": None}
    if period not in period_days:
        return JSONResponse({"error": "조회 기간이 올바르지 않습니다"}, status_code=400)
    try:
        low_threshold = max(0, int(get_site_setting("low_credit_threshold")))
    except (TypeError, ValueError):
        low_threshold = 5
    overview = get_admin_overview_stats(
        period_days[period],
        low_credit_threshold=low_threshold,
    )
    total_cost = 0.0
    for usage in overview["models"]:
        config = MODEL_CONFIG.get(usage["model"], MODEL_CONFIG[PRIMARY_MODEL_KEY])
        pricing = config["pricing"]
        cost = (
            usage["input_tokens"] * pricing["input"]
            + usage["output_tokens"] * pricing["output"]
            + usage["thinking_tokens"] * pricing["thinking"]
        ) / 1_000_000
        usage["label"] = config["label"] if usage["model"] in MODEL_CONFIG else "미기록 모델"
        usage["estimated_cost_usd"] = round(cost, 6)
        total_cost += cost
    overview["tokens"]["estimated_cost_usd"] = round(total_cost, 6)
    overview["period"] = period
    return {"overview": overview}


@app.patch("/api/admin/users/{user_id}/credits")
async def admin_update_credits(user_id: int, body: AdminCreditRequest, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    result = admin_set_credits(user_id, body.credits, body.memo)
    if result is None:
        return JSONResponse({"error": "사용자를 찾을 수 없습니다"}, status_code=404)
    return {"credits": result}


@app.post("/api/admin/credits/bulk")
async def admin_bulk_credits(body: BulkCreditRequest, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    affected = admin_bulk_set_credits(body.credits, body.memo)
    return {"ok": True, "affected": affected}


@app.post("/api/admin/credits/monthly-refill")
async def admin_monthly_refill_now(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    result = admin_refill_credits_to_floor(get_monthly_refill_credits())
    return {"ok": True, **result}


@app.get("/api/admin/users/{user_id}/token-usage")
async def admin_user_token_usage(user_id: int, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"usage": get_user_token_usage_by_model(user_id)}


@app.get("/api/admin/users/{user_id}/transactions")
async def admin_user_transactions(user_id: int, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"transactions": get_transactions(user_id, limit=100)}


@app.get("/api/admin/sessions")
async def admin_all_sessions(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"sessions": list_all_sessions()}


@app.get("/api/admin/users/{user_id}/sessions")
async def admin_user_sessions(user_id: int, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"sessions": list_all_sessions(user_id)}


@app.get("/api/admin/sessions/{session_id}/messages")
async def admin_session_messages(session_id: int, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"messages": admin_get_messages(session_id)}


@app.get("/api/admin/models")
async def admin_models_list(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"models": get_all_models_admin()}


@app.get("/api/admin/turns")
async def admin_turns_list(request: Request, limit: int = 100, status: str | None = None):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"turns": list_chat_turns(limit=limit, status=status)}


@app.patch("/api/admin/models/{model_key}")
async def admin_toggle_model(model_key: str, body: ModelToggleRequest, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    if model_key not in ROUTING_MODEL_KEYS:
        return JSONResponse({"error": "존재하지 않는 모델입니다"}, status_code=404)
    set_model_admin_settings(model_key, body.enabled)
    return {"ok": True, "model_key": model_key, "enabled": body.enabled}


@app.get("/api/admin/settings")
async def admin_get_settings(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"settings": get_all_site_settings()}


@app.patch("/api/admin/settings")
async def admin_update_settings(body: SiteSettingsRequest, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    set_site_setting("default_credits", str(body.default_credits))
    if body.monthly_refill_credits is not None:
        set_site_setting("monthly_refill_credits", str(body.monthly_refill_credits))
    set_site_setting("low_credit_threshold", str(body.low_credit_threshold))
    set_site_setting("unlimited_credits", str(body.unlimited_credits).lower())
    if body.credit_unit_price is not None:
        set_site_setting("credit_unit_price", str(body.credit_unit_price))
    if body.credit_max_quantity is not None:
        set_site_setting("credit_max_quantity", str(body.credit_max_quantity))
    if body.business is not None:
        for key, value in body.business.items():
            if key in payments.BUSINESS_SETTING_KEYS:
                set_site_setting(key, str(value)[:200])
    return {"ok": True, "settings": get_all_site_settings()}


@app.get("/api/admin/payments")
async def admin_payments(request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"payments": [_admin_payment(row) for row in payments.list_orders(limit=200)]}


@app.post("/api/admin/payments/{order_id}/cancel")
async def admin_payment_cancel(
    order_id: str, body: PaymentCancelRequest, request: Request
):
    """전액 취소.  나이스페이 취소가 성립한 뒤에만 이용권을 회수한다."""
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    result = await payments.admin_cancel(order_id, body.reason)
    if not result["ok"]:
        return JSONResponse({"error": result["error"]}, status_code=400)
    return result


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
