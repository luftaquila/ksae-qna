"""
RAG search + Gemini LLM streaming for KSAE Q&A chatbot.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator

from google import genai
from google.genai import types
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

# Globals initialized once at server startup
_model: SentenceTransformer | None = None
_qdrant: QdrantClient | None = None
_gemini: genai.Client | None = None

EMBEDDING_MODEL = "BAAI/bge-m3"
COLLECTIONS = {
    "qna": "ksae-qna",
    "rules": "ksae-formula-rules",
}
_STREAM_DONE = object()

SYSTEM_PROMPT = """\
당신은 KSAE(한국자동차공학회) 대학생 자작자동차대회 Q&A 전문 어시스턴트입니다.

역할:
- 검색된 Q&A 문서를 기반으로 정확하고 도움이 되는 답변을 제공합니다.
- 답변은 한국어로 작성합니다.
- 검색 결과에 근거하여 답변하고, 출처를 인용합니다.
- 검색 결과에 관련 정보가 없으면 솔직하게 "관련 정보를 찾지 못했습니다"라고 답변합니다.
- 답변은 마크다운 형식으로 구조화하여 가독성을 높입니다.

주의사항:
- 검색 결과에 없는 내용을 지어내지 마세요.
- 규정 관련 질문은 최신 규정을 확인하도록 안내하세요.
- 기술적 질문에는 구체적이고 실용적인 답변을 제공하세요.\
"""


def init_resources():
    """Initialize BGE-M3 model, Qdrant client, and Gemini client once."""
    global _model, _qdrant, _gemini

    print("Loading BGE-M3 model...")
    _model = SentenceTransformer(EMBEDDING_MODEL)
    print("BGE-M3 model loaded.")

    _qdrant = QdrantClient(
        url=os.environ.get("QDRANT_URL", "https://vectordb.luftaquila.io:443"),
        api_key=os.environ.get("QDRANT_API_KEY"),
    )
    print("Qdrant client initialized.")

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY environment variable is required")
    _gemini = genai.Client(api_key=api_key)
    print("Gemini client initialized.")


def search(
    query: str,
    limit: int = 5,
    min_score: float = 0.0,
    collections: list[str] | None = None,
    category: str | None = None,
) -> list[dict]:
    """Encode query with BGE-M3 and search Qdrant for similar chunks.

    *collections* is a list of short keys (``"qna"``, ``"rules"``).
    When ``None`` or empty, all collections are searched.
    *category* filters qna results by category (e.g. ``"Formula"``, ``"Baja"``, ``"EV"``).
    """
    if not collections:
        collections = list(COLLECTIONS.keys())
    collection_names = [COLLECTIONS[k] for k in collections if k in COLLECTIONS]

    vector = _model.encode(query).tolist()

    # Build category filter for qna collection
    category_filter = None
    if category:
        category_filter = models.Filter(
            must=[models.FieldCondition(key="category", match=models.MatchValue(value=category))]
        )

    output: list[dict] = []
    for col_name in collection_names:
        # Only apply category filter to qna collection
        qf = category_filter if (category and col_name == COLLECTIONS.get("qna")) else None
        results = _qdrant.query_points(
            collection_name=col_name,
            query=vector,
            limit=limit,
            query_filter=qf,
        )

        for hit in results.points:
            if hit.score < min_score:
                continue

            payload = hit.payload or {}
            content = payload.get("content", "") or payload.get("chunk_text", "")

            if "title" in payload:
                source = f"[{payload.get('category', '')}] {payload['title']}"
                url = payload.get("url", "")
            elif "chapter" in payload:
                source = f"제{payload.get('chapter_num', '')}장 {payload.get('chapter', '')} > {payload.get('section', '')}"
                url = ""
            else:
                source = ""
                url = ""

            output.append({
                "score": hit.score,
                "source": source,
                "url": url,
                "content": content,
            })

    output.sort(key=lambda x: x["score"], reverse=True)
    return output[:limit]


def _build_prompt(query: str, sources: list[dict]) -> str:
    """Build the user prompt with search context."""
    context_parts = []
    for i, s in enumerate(sources, 1):
        header = f"[문서 {i}] {s['source']} (유사도: {s['score']:.4f})"
        if s["url"]:
            header += f"\nURL: {s['url']}"
        context_parts.append(f"{header}\n{s['content']}")

    context = "\n\n---\n\n".join(context_parts)
    return f"다음은 검색된 참고 문서입니다:\n\n{context}\n\n---\n\n사용자 질문: {query}"


async def search_and_stream(
    query: str,
    limit: int = 5,
    min_score: float = 0.0,
    history: list[dict] | None = None,
    collections: list[str] | None = None,
    category: str | None = None,
) -> AsyncIterator[str]:
    """
    Async generator that yields SSE-formatted events:
      - event: sources  (JSON array of search results)
      - event: token    (single text token from Gemini)
      - event: done     (stream finished)

    history: list of {"role": "user"|"assistant", "content": str} for multi-turn context.
    collections: list of collection keys ("qna", "rules") to search.
    """
    # Step 1: Search
    sources = search(query, limit, min_score, collections, category)

    # Yield sources event
    yield f"event: sources\ndata: {json.dumps(sources, ensure_ascii=False)}\n\n"

    # Step 2: Build contents for Gemini
    user_prompt = _build_prompt(query, sources)

    contents = []
    for msg in history or []:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_prompt)]))

    input_tokens = 0
    output_tokens = 0

    try:
        response = _gemini.models.generate_content_stream(
            model="gemini-3-flash-preview",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=4096,
            ),
        )

        # Iterate sync Gemini stream via thread pool to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        it = iter(response)
        while True:
            chunk = await loop.run_in_executor(None, next, it, _STREAM_DONE)
            if chunk is _STREAM_DONE:
                break
            if chunk.text:
                data = json.dumps(chunk.text, ensure_ascii=False)
                yield f"event: token\ndata: {data}\n\n"
            # Capture usage metadata from the last chunk
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                um = chunk.usage_metadata
                if hasattr(um, "prompt_token_count") and um.prompt_token_count:
                    input_tokens = um.prompt_token_count
                if hasattr(um, "candidates_token_count") and um.candidates_token_count:
                    output_tokens = um.candidates_token_count
    except Exception as e:
        error_msg = json.dumps(f"LLM 호출 오류: {e}", ensure_ascii=False)
        yield f"event: token\ndata: {error_msg}\n\n"

    # Send usage metadata before done
    usage_data = json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens})
    yield f"event: usage\ndata: {usage_data}\n\n"
    yield "event: done\ndata: {}\n\n"
