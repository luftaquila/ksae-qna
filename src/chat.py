"""
RAG search + Gemini LLM streaming for KSAE Q&A chatbot.
"""

import json
import os
from collections.abc import AsyncIterator

from google import genai
from google.genai import types
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

# Globals initialized once at server startup
_model: SentenceTransformer | None = None
_qdrant: QdrantClient | None = None
_gemini: genai.Client | None = None

EMBEDDING_MODEL = "BAAI/bge-m3"
COLLECTION = "ksae-qna"

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


def search(query: str, limit: int = 5, min_score: float = 0.0) -> list[dict]:
    """Encode query with BGE-M3 and search Qdrant for similar chunks."""
    vector = _model.encode(query).tolist()
    results = _qdrant.query_points(
        collection_name=COLLECTION,
        query=vector,
        limit=limit,
    )

    output = []
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
    return output


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
) -> AsyncIterator[str]:
    """
    Async generator that yields SSE-formatted events:
      - event: sources  (JSON array of search results)
      - event: token    (single text token from Gemini)
      - event: done     (stream finished)

    history: list of {"role": "user"|"assistant", "content": str} for multi-turn context.
    """
    # Step 1: Search
    sources = search(query, limit, min_score)

    # Yield sources event
    yield f"event: sources\ndata: {json.dumps(sources, ensure_ascii=False)}\n\n"

    # Step 2: Build contents for Gemini
    user_prompt = _build_prompt(query, sources)

    contents = []
    for msg in history or []:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_prompt)]))

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

        for chunk in response:
            if chunk.text:
                data = json.dumps(chunk.text, ensure_ascii=False)
                yield f"event: token\ndata: {data}\n\n"
    except Exception as e:
        error_msg = json.dumps(f"LLM 호출 오류: {e}", ensure_ascii=False)
        yield f"event: token\ndata: {error_msg}\n\n"

    yield "event: done\ndata: {}\n\n"
