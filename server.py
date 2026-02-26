"""
FastAPI server for KSAE Q&A chatbot.
"""

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
    clear_auth_cookie,
    create_jwt,
    deduct_credit,
    get_current_user,
    get_or_create_user,
    init_db,
    init_oauth,
    oauth,
    set_auth_cookie,
)
from src.chat import init_resources, search_and_stream

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_oauth()
    init_resources()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("JWT_SECRET", "dev"))


class ChatRequest(BaseModel):
    query: str
    limit: int = 5


class TopupRequest(BaseModel):
    amount: int


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


# ---------------------------------------------------------------------------
# Chat (with auth + credit check)
# ---------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(request: Request, req: ChatRequest):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "로그인이 필요합니다"}, status_code=401)

    if not deduct_credit(user["id"]):
        return JSONResponse({"error": "크레딧이 부족합니다"}, status_code=402)

    remaining = user["credits"] - 1

    return StreamingResponse(
        search_and_stream(req.query, req.limit),
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


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
