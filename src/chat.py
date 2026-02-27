"""
RAG search + multi-model LLM streaming for KSAE Q&A chatbot.
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator

import anthropic

logger = logging.getLogger(__name__)
from google import genai
from google.genai import types
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

from src.auth import get_model_settings_map, set_model_settings

# Globals initialized once at server startup
_model: SentenceTransformer | None = None
_qdrant: QdrantClient | None = None
_gemini: genai.Client | None = None
_anthropic: anthropic.AsyncAnthropic | None = None

_model_enabled: dict[str, bool] = {}
_model_credits: dict[str, int | None] = {}

EMBEDDING_MODEL = "BAAI/bge-m3"
COLLECTIONS = {
    "qna": "ksae-qna",
    "rules": "ksae-formula-rules",
}
_STREAM_DONE = object()

MODEL_CONFIG = {
    "gemini-3-flash": {
        "provider": "gemini",
        "model_id": "gemini-3-flash-preview",
        "label": "Gemini 3 Flash",
        "credits": 1,
        "thinking_level": "high",
        "pricing": {"input": 0.50, "output": 3.00, "thinking": 3.00},
    },
    "gemini-3-pro": {
        "provider": "gemini",
        "model_id": "gemini-3-pro-preview",
        "label": "Gemini 3 Pro",
        "credits": 4,
        "thinking_level": "high",
        "pricing": {"input": 2.50, "output": 15.00, "thinking": 15.00},
    },
    "claude-sonnet-4.6": {
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-6-20250514",
        "label": "Claude Sonnet 4.6",
        "credits": 5,
        "thinking_level": "high",
        "pricing": {"input": 3.00, "output": 15.00, "thinking": 15.00},
    },
    "claude-opus-4.6": {
        "provider": "anthropic",
        "model_id": "claude-opus-4-6-20250514",
        "label": "Claude Opus 4.6",
        "credits": 10,
        "thinking_level": "max",
        "pricing": {"input": 5.00, "output": 25.00, "thinking": 25.00},
    },
}

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
    """Initialize BGE-M3 model, Qdrant client, Gemini client, and optionally Anthropic client."""
    global _model, _qdrant, _gemini, _anthropic

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

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        _anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
        print("Anthropic client initialized.")
    else:
        print("WARNING: ANTHROPIC_API_KEY not set — Claude models will be unavailable.")


def init_model_settings() -> None:
    """Load admin model settings from DB into in-memory cache."""
    settings = get_model_settings_map()
    for key, val in settings.items():
        _model_enabled[key] = val["enabled"]
        _model_credits[key] = val["credits"]


def set_model_admin_settings(model_key: str, enabled: bool, credits: int | None = None) -> None:
    """Update both DB and in-memory cache for enabled + credits."""
    set_model_settings(model_key, enabled, credits)
    _model_enabled[model_key] = enabled
    _model_credits[model_key] = credits


def get_effective_credits(model_key: str) -> int:
    """Return admin-overridden credits or default from MODEL_CONFIG."""
    custom = _model_credits.get(model_key)
    if custom is not None:
        return custom
    return MODEL_CONFIG[model_key]["credits"]


def is_model_available(model: str) -> bool:
    """Check if a model's provider client is initialized and admin-enabled."""
    cfg = MODEL_CONFIG.get(model)
    if not cfg:
        return False
    if not _model_enabled.get(model, True):
        return False
    if cfg["provider"] == "gemini":
        return _gemini is not None
    if cfg["provider"] == "anthropic":
        return _anthropic is not None
    return False


def get_models() -> list[dict]:
    """Return list of available models (provider initialized + admin enabled)."""
    result = []
    for model_key, cfg in MODEL_CONFIG.items():
        if not _model_enabled.get(model_key, True):
            continue
        if cfg["provider"] == "gemini" and _gemini is None:
            continue
        if cfg["provider"] == "anthropic" and _anthropic is None:
            continue
        result.append({
            "id": model_key,
            "label": cfg["label"],
            "credits": get_effective_credits(model_key),
            "pricing": cfg["pricing"],
        })
    return result


def get_all_models_admin() -> list[dict]:
    """Return all models with provider_available, admin_enabled, and available status."""
    result = []
    for model_key, cfg in MODEL_CONFIG.items():
        if cfg["provider"] == "gemini":
            provider_available = _gemini is not None
        elif cfg["provider"] == "anthropic":
            provider_available = _anthropic is not None
        else:
            provider_available = False

        admin_enabled = _model_enabled.get(model_key, True)

        result.append({
            "id": model_key,
            "label": cfg["label"],
            "default_credits": cfg["credits"],
            "credits": get_effective_credits(model_key),
            "provider": cfg["provider"],
            "provider_available": provider_available,
            "admin_enabled": admin_enabled,
            "available": provider_available and admin_enabled,
        })
    return result


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


async def _stream_gemini(
    contents: list,
    model_config: dict,
) -> AsyncIterator[str]:
    """Stream from Gemini and yield SSE events (token / usage)."""
    input_tokens = 0
    output_tokens = 0
    thinking_tokens = 0

    try:
        config_kwargs: dict = {
            "system_instruction": SYSTEM_PROMPT,
            "temperature": 0.3,
            "max_output_tokens": 4096,
        }
        if model_config["thinking_level"]:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=model_config["thinking_level"]
            )

        response = _gemini.models.generate_content_stream(
            model=model_config["model_id"],
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        loop = asyncio.get_event_loop()
        it = iter(response)
        while True:
            chunk = await loop.run_in_executor(None, next, it, _STREAM_DONE)
            if chunk is _STREAM_DONE:
                break
            if chunk.text:
                data = json.dumps(chunk.text, ensure_ascii=False)
                yield f"event: token\ndata: {data}\n\n"
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                um = chunk.usage_metadata
                if hasattr(um, "prompt_token_count") and um.prompt_token_count is not None:
                    input_tokens = um.prompt_token_count
                if hasattr(um, "candidates_token_count") and um.candidates_token_count is not None:
                    output_tokens = um.candidates_token_count
                if hasattr(um, "thoughts_token_count") and um.thoughts_token_count is not None:
                    thinking_tokens = um.thoughts_token_count
    except Exception as e:
        logger.exception("Gemini streaming error: %s", e)
        error_msg = json.dumps("LLM 응답 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ensure_ascii=False)
        yield f"event: error\ndata: {error_msg}\n\n"

    usage_data = json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens, "thinking_tokens": thinking_tokens})
    yield f"event: usage\ndata: {usage_data}\n\n"


async def _stream_anthropic(
    model_config: dict,
    query: str,
    sources: list[dict],
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    """Stream from Anthropic and yield SSE events (token / usage)."""
    user_prompt = _build_prompt(query, sources)

    # Build messages in Anthropic format
    messages = []
    for msg in history or []:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_prompt})

    input_tokens = 0
    output_tokens = 0
    thinking_tokens = 0

    try:
        kwargs: dict = {
            "model": model_config["model_id"],
            "max_tokens": 128000,
            "system": SYSTEM_PROMPT,
            "messages": messages,
        }

        if model_config["thinking_level"]:
            # Use adaptive thinking (recommended for Opus 4.6 / Sonnet 4.6)
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": model_config["thinking_level"]}
            kwargs["temperature"] = 1  # required for extended thinking

        async with _anthropic.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        data = json.dumps(event.delta.text, ensure_ascii=False)
                        yield f"event: token\ndata: {data}\n\n"

            # Get final message for usage
            response = await stream.get_final_message()
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

            # Thinking tokens are included in output_tokens for billing,
            # but may be available separately via usage metadata
            if hasattr(response.usage, "thinking_tokens"):
                thinking_tokens = response.usage.thinking_tokens

    except Exception as e:
        logger.exception("Anthropic streaming error: %s", e)
        error_msg = json.dumps("LLM 응답 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", ensure_ascii=False)
        yield f"event: error\ndata: {error_msg}\n\n"

    usage_data = json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens, "thinking_tokens": thinking_tokens})
    yield f"event: usage\ndata: {usage_data}\n\n"


async def search_and_stream(
    query: str,
    limit: int = 5,
    min_score: float = 0.0,
    history: list[dict] | None = None,
    collections: list[str] | None = None,
    category: str | None = None,
    model: str = "gemini-3-flash",
) -> AsyncIterator[str]:
    """
    Async generator that yields SSE-formatted events:
      - event: sources  (JSON array of search results)
      - event: token    (single text token from LLM)
      - event: usage    (token usage metadata)
      - event: done     (stream finished)

    history: list of {"role": "user"|"assistant", "content": str} for multi-turn context.
    collections: list of collection keys ("qna", "rules") to search.
    model: model key from MODEL_CONFIG.
    """
    model_config = MODEL_CONFIG[model]

    # Step 1: Search
    sources = search(query, limit, min_score, collections, category)

    # Yield sources event
    yield f"event: sources\ndata: {json.dumps(sources, ensure_ascii=False)}\n\n"

    # Step 2: Stream from the selected provider
    if model_config["provider"] == "gemini":
        # Build Gemini contents
        user_prompt = _build_prompt(query, sources)
        contents = []
        for msg in history or []:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
        contents.append(types.Content(role="user", parts=[types.Part(text=user_prompt)]))

        async for event in _stream_gemini(contents, model_config):
            yield event

    elif model_config["provider"] == "anthropic":
        if _anthropic is None:
            error_msg = json.dumps("Anthropic API 키가 설정되지 않았습니다. Claude 모델을 사용할 수 없습니다.", ensure_ascii=False)
            yield f"event: error\ndata: {error_msg}\n\n"
            usage_data = json.dumps({"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0})
            yield f"event: usage\ndata: {usage_data}\n\n"
        else:
            async for event in _stream_anthropic(model_config, query, sources, history):
                yield event

    yield "event: done\ndata: {}\n\n"
