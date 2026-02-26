"""
FastAPI server for KSAE Q&A chatbot.
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.chat import init_resources, search_and_stream

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_resources()
    yield


app = FastAPI(lifespan=lifespan)


class ChatRequest(BaseModel):
    query: str
    limit: int = 5


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(
        search_and_stream(req.query, req.limit),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health")
async def health():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
