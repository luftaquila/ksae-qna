"""
RAG search + multi-model LLM streaming for KSAE Q&A chatbot.
"""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator

import anthropic

logger = logging.getLogger(__name__)
from google import genai
from google.genai import types
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

from src.auth import get_model_settings_map, set_model_order as _db_set_model_order, set_model_settings

# Globals initialized once at server startup
_model: SentenceTransformer | None = None
_qdrant: QdrantClient | None = None
_gemini: genai.Client | None = None
_anthropic: anthropic.AsyncAnthropic | None = None

_model_enabled: dict[str, bool] = {}
_model_credits: dict[str, int | None] = {}
_model_order: dict[str, int] = {}  # model_key -> display_order

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
당신은 KSAE(한국자동차공학회) 대학생 자작자동차대회 전문 어시스턴트 PitBot입니다.
사용자의 질문에 대해, 함께 제공되는 검색 결과 문서를 근거로 정확하게 답변합니다.
답변은 한국어로 작성합니다.

# 데이터 소스
검색 결과는 두 종류의 소스에서 올 수 있습니다:
- **규정집**: "[문서 N] 제X장 ... > ..." 형태. 대회 공식 규정이므로 가장 신뢰도가 높습니다.
- **Q&A 게시판**: "[문서 N] [카테고리] 제목" 형태. 대회 운영진의 질의응답 기록입니다.

규정집과 Q&A의 내용이 상충하는 경우, Q&A가 규정에 대한 공식 해석이므로 Q&A의 내용을 우선합니다.

# 답변 규칙
- 반드시 제공된 검색 결과에 근거하여 답변하세요. 검색 결과에 없는 내용을 추측하거나 지어내지 마세요.
- 답변에서 근거가 되는 문서를 인용하세요. 예: "규정집 제3장 3.2절에 따르면...", "Q&A 게시판의 [제목]에서..."
- URL이 있는 문서는 링크를 포함하세요.
- 검색 결과에 관련 정보가 충분하지 않으면 솔직히 알려주세요.
- 규정 관련 답변에는 "정확한 내용은 최신 규정집을 반드시 확인하세요"라는 안내를 포함하세요.
- Q&A 게시판 내용을 근거로 답변하는 경우 "Q&A 답변 내용은 현행 규정과 다를 수 있으니 유의하세요"라는 안내를 포함하세요.
- 기술적 질문에는 구체적이고 실용적인 답변을 제공하세요.
- 답변은 마크다운으로 구조화하여 가독성을 높이세요.
- 자기소개나 인삿말 등을 하지 말고 바로 본론으로 들어가세요.\
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
        if val["display_order"] is not None:
            _model_order[key] = val["display_order"]


def set_model_admin_settings(model_key: str, enabled: bool, credits: int | None = None) -> None:
    """Update both DB and in-memory cache for enabled + credits."""
    set_model_settings(model_key, enabled, credits)
    _model_enabled[model_key] = enabled
    _model_credits[model_key] = credits


def set_model_display_order(order: list[str]) -> None:
    """Update display order in both DB and in-memory cache."""
    _db_set_model_order(order)
    _model_order.clear()
    for idx, key in enumerate(order):
        _model_order[key] = idx


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


def _sort_key(model_key: str, idx: int) -> int:
    """Return display order for sorting; fall back to dict insertion index."""
    return _model_order.get(model_key, idx)


def get_models() -> list[dict]:
    """Return all models with availability status, sorted by display order."""
    result = []
    for idx, (model_key, cfg) in enumerate(MODEL_CONFIG.items()):
        admin_enabled = _model_enabled.get(model_key, True)
        provider_ok = True
        if cfg["provider"] == "gemini" and _gemini is None:
            provider_ok = False
        if cfg["provider"] == "anthropic" and _anthropic is None:
            provider_ok = False
        available = admin_enabled and provider_ok
        result.append({
            "id": model_key,
            "label": cfg["label"],
            "credits": get_effective_credits(model_key),
            "pricing": cfg["pricing"],
            "available": available,
            "_order": _sort_key(model_key, idx),
        })
    result.sort(key=lambda x: x["_order"])
    for r in result:
        del r["_order"]
    return result


def get_all_models_admin() -> list[dict]:
    """Return all models with provider_available, admin_enabled, and available status, sorted by display order."""
    result = []
    for idx, (model_key, cfg) in enumerate(MODEL_CONFIG.items()):
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
            "_order": _sort_key(model_key, idx),
        })
    result.sort(key=lambda x: x["_order"])
    for r in result:
        del r["_order"]
    return result


def search(
    query: str,
    limit: int = 5,
    min_score: float = 0.0,
    collections: list[str] | None = None,
    category: str | None = None,
    min_per_collection: int = 1,
) -> list[dict]:
    """Encode query with BGE-M3 and search Qdrant for similar chunks.

    *collections* is a list of short keys (``"qna"``, ``"rules"``).
    When ``None`` or empty, all collections are searched.
    *category* filters qna results by category (e.g. ``"Formula"``, ``"Baja"``, ``"EV"``).
    *min_per_collection* guarantees at least N results from each collection
    (if available), preventing one collection from dominating all results.
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

    # Collect results per collection
    per_collection: dict[str, list[dict]] = {}
    for col_name in collection_names:
        # Only apply category filter to qna collection
        qf = category_filter if (category and col_name == COLLECTIONS.get("qna")) else None
        try:
            results = _qdrant.query_points(
                collection_name=col_name,
                query=vector,
                limit=limit,
                query_filter=qf,
            )
        except Exception as e:
            logger.error("Qdrant query failed for collection '%s': %s", col_name, e)
            per_collection[col_name] = []
            continue

        hits = []
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

            hit_item = {
                "score": hit.score,
                "source": source,
                "url": url,
                "content": content,
            }
            # Track post_id for qna deduplication
            if "id" in payload:
                hit_item["post_id"] = payload["id"]
            hits.append(hit_item)

        hits.sort(key=lambda x: x["score"], reverse=True)
        per_collection[col_name] = hits

    # Guarantee min_per_collection from each, fill remainder by score
    guaranteed: list[dict] = []
    remainder: list[dict] = []
    for col_name, hits in per_collection.items():
        guaranteed.extend(hits[:min_per_collection])
        remainder.extend(hits[min_per_collection:])

    remainder.sort(key=lambda x: x["score"], reverse=True)
    remaining_slots = max(0, limit - len(guaranteed))
    output = guaranteed + remainder[:remaining_slots]
    output.sort(key=lambda x: x["score"], reverse=True)

    # Deduplicate: keep only the highest-score chunk per post
    seen_posts: set = set()
    deduped: list[dict] = []
    for item in output:
        pid = item.get("post_id")
        if pid is not None:
            if pid in seen_posts:
                continue
            seen_posts.add(pid)
        deduped.append(item)
    return deduped


def _build_prompt(query: str, sources: list[dict], search_query: str | None = None) -> str:
    """Build the user prompt with search context."""
    context_parts = []
    for i, s in enumerate(sources, 1):
        header = f"[문서 {i}] {s['source']} (유사도: {s['score']:.4f})"
        if s["url"]:
            header += f"\nURL: {s['url']}"
        context_parts.append(f"{header}\n{s['content']}")

    context = "\n\n---\n\n".join(context_parts)
    prompt = f"다음은 검색된 참고 문서입니다:\n\n{context}\n\n---\n\n"

    # Warn LLM when search results are weak
    if sources:
        max_score = max(s["score"] for s in sources)
        if max_score < 0.6:
            prompt += "⚠️ 검색 결과의 유사도가 전반적으로 낮습니다. 검색 결과가 질문과 직접적으로 관련이 없을 수 있으니, 관련 정보가 부족하다면 솔직히 알려주세요.\n"
    else:
        prompt += "⚠️ 검색 결과가 없습니다. 관련 정보를 찾지 못했다고 안내해주세요.\n"

    if search_query and search_query != query:
        prompt += f"(검색에 사용된 쿼리: {search_query})\n"
    prompt += f"사용자 질문: {query}"
    return prompt


async def _rewrite_query(query: str, history: list[dict] | None) -> str | None:
    """Rewrite a follow-up query into a standalone search query using conversation history.

    Returns the rewritten query, or None if rewriting was skipped or failed.
    """
    if not history:
        return None

    # Build condensed history (last 6 messages, assistant truncated to 500 chars)
    history_lines = []
    for msg in history[-6:]:
        role = "사용자" if msg["role"] == "user" else "어시스턴트"
        content = msg["content"]
        if msg["role"] == "assistant" and len(content) > 500:
            content = content[:500] + "..."
        history_lines.append(f"{role}: {content}")

    history_text = "\n".join(history_lines)

    prompt = f"""대화 기록과 후속 질문을 바탕으로, 벡터 검색에 사용할 독립적인 검색 쿼리를 작성하세요.

규칙:
- 대명사(그것, 이것, 그 규정 등)와 생략된 주어를 대화에서 언급된 구체적인 명사로 대체하세요.
- 대화에서 다룬 핵심 주제와 키워드를 반드시 검색 쿼리에 포함하세요.
- 후속 질문이 이미 독립적이라면 그대로 반환하세요.
- 검색 쿼리는 자연스러운 한국어 문장이나 구(phrase)로 작성하세요. 단어 1~2개로 축약하지 마세요.
- 검색 쿼리만 출력하고, 설명이나 부가 텍스트는 추가하지 마세요.

예시:
- 대화: "방화벽이 뭐야?" → 어시스턴트 답변 → 후속: "그 규정에 대해 더 알려줘" → 쿼리: "방화벽 규정 상세 내용"
- 대화: "5인치 휠 사용 가능한지" → 어시스턴트 답변 → 후속: "포뮬러 기준" → 쿼리: "포뮬러 5인치 휠 타이어 사용 규정"

대화 기록:
{history_text}

후속 질문: {query}

검색 쿼리:"""

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: _gemini.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=150,
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                ),
            ),
        )
        rewritten = response.text.strip()
        logger.warning("Query rewrite: '%s' -> '%s'", query, rewritten)
        if rewritten and rewritten != query:
            return rewritten
        return None
    except Exception as e:
        logger.warning("Query rewrite failed, using original: %s", e)
        return None


def _compress_history(history: list[dict]) -> list[dict]:
    """Compress assistant messages by removing URLs, document references, and scores, then truncating."""
    compressed = []
    for msg in history:
        if msg["role"] == "user":
            compressed.append(msg)
            continue

        content = msg["content"]
        content = re.sub(r'https?://\S+', '', content)
        content = re.sub(r'\[문서\s*\d+\]', '', content)
        content = re.sub(r'\(유사도:\s*[\d.]+%?\)', '', content)
        content = re.sub(r'\n{3,}', '\n\n', content).strip()
        if len(content) > 500:
            content = content[:500] + "..."

        compressed.append({"role": msg["role"], "content": content})

    return compressed


def _classify_error(e: Exception, provider: str) -> str:
    """Return a user-friendly error message based on the exception type."""
    msg = str(e).lower()

    if "503" in msg or "unavailable" in msg or "overloaded" in msg:
        return f"{provider} 서버가 일시적으로 과부하 상태입니다. 잠시 후 다시 시도해주세요."
    if "429" in msg or "rate" in msg or "quota" in msg or "resource_exhausted" in msg:
        return f"{provider} API 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."
    if "401" in msg or "403" in msg or "permission" in msg or "authentication" in msg:
        return f"{provider} API 인증에 실패했습니다. 관리자에게 문의해주세요."
    if "timeout" in msg:
        return f"{provider} 서버 응답 시간이 초과되었습니다. 잠시 후 다시 시도해주세요."
    if "400" in msg or "invalid" in msg:
        return f"{provider} 요청 처리 중 오류가 발생했습니다. 질문을 수정하여 다시 시도해주세요."

    return f"{provider} 응답 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."


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
        error_msg = json.dumps(_classify_error(e, "Gemini"), ensure_ascii=False)
        yield f"event: error\ndata: {error_msg}\n\n"

    usage_data = json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens, "thinking_tokens": thinking_tokens})
    yield f"event: usage\ndata: {usage_data}\n\n"


async def _stream_anthropic(
    model_config: dict,
    query: str,
    sources: list[dict],
    history: list[dict] | None = None,
    search_query: str | None = None,
) -> AsyncIterator[str]:
    """Stream from Anthropic and yield SSE events (token / usage)."""
    user_prompt = _build_prompt(query, sources, search_query)

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
        error_msg = json.dumps(_classify_error(e, "Claude"), ensure_ascii=False)
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
      - event: rewrite  (rewritten search query, if applicable)
      - event: sources  (JSON array of search results)
      - event: token    (single text token from LLM)
      - event: usage    (token usage metadata)
      - event: done     (stream finished)

    history: list of {"role": "user"|"assistant", "content": str} for multi-turn context.
    collections: list of collection keys ("qna", "rules") to search.
    model: model key from MODEL_CONFIG.
    """
    model_config = MODEL_CONFIG[model]

    # Step 1: Rewrite query for better search if we have conversation history
    search_query = query
    rewritten = await _rewrite_query(query, history)
    if rewritten:
        search_query = rewritten
        yield f"event: rewrite\ndata: {json.dumps(rewritten, ensure_ascii=False)}\n\n"

    # Step 2: Search with (possibly rewritten) query
    sources = search(search_query, limit, min_score, collections, category)

    # Yield sources event
    yield f"event: sources\ndata: {json.dumps(sources, ensure_ascii=False)}\n\n"

    # Compress history for LLM context
    compressed = _compress_history(history) if history else history

    # Step 3: Stream from the selected provider
    if model_config["provider"] == "gemini":
        # Build Gemini contents
        user_prompt = _build_prompt(query, sources, search_query if rewritten else None)
        contents = []
        for msg in compressed or []:
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
            async for event in _stream_anthropic(model_config, query, sources, compressed, search_query if rewritten else None):
                yield event

    yield "event: done\ndata: {}\n\n"
