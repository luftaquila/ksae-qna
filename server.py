"""
FastAPI server for KSAE Q&A chatbot.
"""

import json
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from src.auth import (
    add_credits,
    add_message,
    admin_get_messages,
    admin_set_credits,
    clear_auth_cookie,
    create_jwt,
    create_session,
    deduct_credit,
    delete_session,
    get_current_user,
    get_messages,
    get_or_create_user,
    get_session,
    get_transactions,
    init_admin_emails,
    init_db,
    init_oauth,
    is_admin,
    list_all_sessions,
    list_all_users,
    list_sessions,
    oauth,
    set_auth_cookie,
    update_session_title,
)
from src.chat import init_resources, search_and_stream

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_oauth()
    init_admin_emails()
    init_resources()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("JWT_SECRET", "dev"), https_only=True)


@app.middleware("http")
async def fix_request_scheme(request: Request, call_next):
    """
    Ensure request.url_for uses https if the app is behind an HTTPS reverse proxy.
    """
    if request.headers.get("x-forwarded-proto") == "https":
        request.scope["scheme"] = "https"
    return await call_next(request)


class ChatRequest(BaseModel):
    query: str
    limit: int = 5
    session_id: int | None = None
    collections: list[str] | None = None
    category: str | None = None


class SessionPatch(BaseModel):
    title: str


class TopupRequest(BaseModel):
    amount: int


class AdminCreditRequest(BaseModel):
    credits: int
    memo: str = "관리자 조정"


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
    return {
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "picture": user["picture"],
            "credits": user["credits"],
        }
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
@app.post("/api/chat")
async def chat(request: Request, req: ChatRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)

    if not deduct_credit(user["id"]):
        return JSONResponse({"error": "크레딧이 부족합니다"}, status_code=402)

    remaining = user["credits"] - 1

    # Resolve or create session
    session_id = req.session_id
    if session_id:
        session = get_session(session_id, user["id"])
        if not session:
            return JSONResponse({"error": "세션을 찾을 수 없습니다"}, status_code=404)
    else:
        title = req.query[:50]
        session = create_session(user["id"], title)
        session_id = session["id"]

    # Fetch recent history (last 3 turns = 6 messages) before persisting current user message
    history = []
    if req.session_id:
        prev_messages = get_messages(session_id)
        # Take last 6 messages (3 user + 3 assistant turns)
        for msg in prev_messages[-6:]:
            history.append({"role": msg["role"], "content": msg["content"]})

    # Persist user message
    add_message(session_id, "user", req.query)

    # If this is the first message in an existing session with default title, update it
    if req.session_id and session["title"] == "새 대화":
        update_session_title(session_id, user["id"], req.query[:50])

    async def stream_and_persist():
        full_text = ""
        sources_json = None
        input_tokens = None
        output_tokens = None

        async for event in search_and_stream(req.query, req.limit, min_score=0.5, history=history, collections=req.collections, category=req.category):
            yield event

            # Collect data for persistence
            for line in event.strip().split("\n"):
                if line.startswith("event: sources"):
                    pass  # next data line has the sources
                elif line.startswith("data: ") and sources_json is None and "sources" in event:
                    try:
                        sources_json = line[6:]
                    except Exception:
                        pass
                elif line.startswith("data: ") and "token" in event:
                    try:
                        full_text += json.loads(line[6:])
                    except Exception:
                        pass
                elif line.startswith("data: ") and "usage" in event:
                    try:
                        usage = json.loads(line[6:])
                        input_tokens = usage.get("input_tokens")
                        output_tokens = usage.get("output_tokens")
                    except Exception:
                        pass

        # Persist assistant message
        add_message(session_id, "assistant", full_text, sources_json, input_tokens, output_tokens)

        # Send session_id to client
        yield f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n"

    return StreamingResponse(
        stream_and_persist(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Credits-Remaining": str(remaining),
        },
    )


@app.get("/api/health")
async def health():
    return {"status": "ok"}


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
    return {"users": list_all_users()}


@app.patch("/api/admin/users/{user_id}/credits")
async def admin_update_credits(user_id: int, body: AdminCreditRequest, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    result = admin_set_credits(user_id, body.credits, body.memo)
    if result is None:
        return JSONResponse({"error": "사용자를 찾을 수 없습니다"}, status_code=404)
    return {"credits": result}


@app.get("/api/admin/users/{user_id}/transactions")
async def admin_user_transactions(user_id: int, request: Request):
    if not is_admin(request):
        return JSONResponse({"error": "관리자 권한이 필요합니다"}, status_code=403)
    return {"transactions": get_transactions(user_id, limit=100)}


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


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
