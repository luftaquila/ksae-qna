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
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from src.auth import (
    add_credits,
    add_message,
    admin_bulk_set_credits,
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
    delete_session,
    get_all_site_settings,
    get_current_user,
    get_messages,
    get_recent_messages,
    get_site_setting,
    get_all_users_token_usage_by_model,
    get_user_token_usage_by_model,
    get_or_create_user,
    get_session,
    get_transactions,
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
    COLLECTION_REGISTRY,
    MODEL_CONFIG,
    PROMPT_VERSION,
    get_all_models_admin,
    get_effective_credits,
    get_health_status,
    get_models,
    init_model_settings,
    init_resources,
    is_model_available,
    search_and_stream,
    set_model_admin_settings,
    set_model_display_order,
)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_oauth()
    init_admin_emails()
    init_site_settings()
    init_resources()
    init_model_settings()
    yield


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
    category: str | None = None
    competition: str | None = None
    model: str = "gemini-3-flash"


class SessionPatch(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)


class TopupRequest(BaseModel):
    amount: int = Field(..., ge=1, le=1000)


class AdminCreditRequest(BaseModel):
    credits: int = Field(..., ge=0)
    memo: str = Field(default="관리자 조정", max_length=200)


class ModelToggleRequest(BaseModel):
    enabled: bool
    credits: int | None = Field(default=None, ge=0)


class SiteSettingsRequest(BaseModel):
    default_credits: int = Field(..., ge=0, le=10000)
    low_credit_threshold: int = Field(..., ge=0, le=10000)
    unlimited_credits: bool = Field(default=False)


class BulkCreditRequest(BaseModel):
    credits: int = Field(..., ge=0)
    memo: str = Field(default="관리자 일괄 조정", max_length=200)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse("static/index.html")


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
            "is_admin": is_admin(request) is not None,
        },
        "low_credit_threshold": low_threshold,
        "unlimited_credits": is_unlimited_credits(),
    }


@app.post("/api/credits/topup")
async def topup(request: Request, body: TopupRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)

    new_balance = add_credits(user["id"], body.amount)
    if new_balance is None:
        return JSONResponse({"error": "충전량은 1~1000 사이여야 합니다"}, status_code=400)

    return {"credits": new_balance}


@app.get("/api/transactions")
async def transactions(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)
    return {"transactions": get_transactions(user["id"])}


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
@app.get("/api/models")
async def models_list():
    return {"models": get_models()}


@app.get("/api/collections")
async def collections_list():
    """검색 소스 목록. 프론트엔드 칩·안내문은 이 응답으로 렌더된다."""
    return {
        "collections": [
            {"key": key, **{k: v for k, v in meta.items() if k != "collection"}}
            for key, meta in COLLECTION_REGISTRY.items()
        ]
    }


@app.post("/api/chat")
async def chat(request: Request, req: ChatRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)

    # Validate model
    model_config = MODEL_CONFIG.get(req.model)
    if not model_config:
        return JSONResponse({"error": "지원하지 않는 모델입니다"}, status_code=400)

    if not is_model_available(req.model):
        return JSONResponse({"error": f"{model_config['label']} 모델의 상태 확인에 실패해 현재 사용할 수 없습니다."}, status_code=503)

    invalid_collections = set(req.collections or []) - set(COLLECTION_REGISTRY)
    if invalid_collections:
        return JSONResponse({"error": "지원하지 않는 검색 소스가 포함되어 있습니다"}, status_code=400)
    if req.competition not in (None, "smart_e_mobility", "e_formula", "formula", "baja", "ev"):
        return JSONResponse({"error": "지원하지 않는 대회 종목입니다"}, status_code=400)

    # Validate ownership before charging.  Previously a stale/foreign session
    # returned 404 only after consuming the user's credits.
    session_id = req.session_id
    session = None
    if session_id:
        session = get_session(session_id, user["id"])
        if not session:
            return JSONResponse({"error": "세션을 찾을 수 없습니다"}, status_code=404)

    credits_needed = get_effective_credits(req.model)
    model_label = model_config["label"]

    if not deduct_credit(user["id"], credits_needed, f"질문 ({model_label})"):
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
            req.model,
            collections=json.dumps(req.collections, ensure_ascii=False) if req.collections else None,
            category=req.category,
            competition=req.competition,
            confidence=None,
            prompt_version=PROMPT_VERSION,
        )
        if req.session_id and session["title"] == "새 대화":
            update_session_title(session_id, user["id"], req.query[:50])
    except Exception:
        logger.exception("Failed to initialize chat turn")
        refund_credit(user["id"], credits_needed, f"요청 저장 실패 환불 ({model_label})")
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
        competition = req.competition
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
                category=req.category,
                competition=req.competition,
                model=req.model,
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
                elif event_type != "done":
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
                refund_credit(user["id"], credits_needed, f"오류 환불 ({model_label})")
            elif has_fallback and resolved_model:
                fallback_refund = max(0, credits_needed - get_effective_credits(resolved_model))
                if fallback_refund:
                    refund_credit(user["id"], fallback_refund, f"대체 모델 차액 환불 ({model_label})")

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
                    model=resolved_model or req.model,
                    rewritten_query=rewritten_query,
                    turn_id=turn_id,
                )
                complete_chat_turn(
                    turn_id,
                    assistant_message_id=assistant_message["id"],
                    resolved_model=resolved_model or req.model,
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
    if model_key not in MODEL_CONFIG:
        return JSONResponse({"error": "존재하지 않는 모델입니다"}, status_code=404)
    set_model_admin_settings(model_key, body.enabled, body.credits)
    return {"ok": True, "model_key": model_key, "enabled": body.enabled, "credits": get_effective_credits(model_key)}


class ModelOrderRequest(BaseModel):
    order: list[str]


@app.put("/api/admin/models/order")
async def admin_set_model_order(body: ModelOrderRequest, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    # Validate all keys exist
    for key in body.order:
        if key not in MODEL_CONFIG:
            return JSONResponse({"error": f"존재하지 않는 모델: {key}"}, status_code=400)
    set_model_display_order(body.order)
    return {"ok": True, "order": body.order}


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
    set_site_setting("low_credit_threshold", str(body.low_credit_threshold))
    set_site_setting("unlimited_credits", str(body.unlimited_credits).lower())
    return {"ok": True, "settings": get_all_site_settings()}


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
