"""
RAG search + multi-model LLM streaming for KSAE Q&A chatbot.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
import numpy as np

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
_model_health: dict[str, dict[str, Any]] = {}

PROMPT_VERSION = "2026-08-quality-v2"
PRIMARY_MODEL_KEY = "gemini-3-pro"
FALLBACK_MODEL_KEY = "gemini-3-flash"
ROUTING_MODEL_KEYS = (PRIMARY_MODEL_KEY, FALLBACK_MODEL_KEY)
CHAT_CREDIT_COST = 1

EMBEDDING_MODEL = "BAAI/bge-m3"
# 검색 소스 레지스트리 — 컬렉션을 추가할 때 고쳐야 하는 유일한 곳.
# 프론트엔드 칩과 안내문은 /api/collections 로 이 값을 받아 렌더한다.
# 순서가 곧 UI 칩 순서다.
COLLECTION_REGISTRY: dict[str, dict] = {
    "rules": {
        "collection": "ksae-formula-rules",
        "label": "규정",
        "description": "대회 규정집 (2026 Formula)",
        "authority": "공식",
    },
    "qna": {
        "collection": "ksae-qna",
        "label": "Q&A",
        "description": "KSAE Q&A 게시판 — 운영진 질의응답",
        "authority": "공식 해석",
        "filter": "category",
    },
    "kb": {
        "collection": "ksae-aark-kb",
        "label": "AARK",
        "description": "참가팀 익명 단톡방 지식베이스 (2025-02 ~ 2026-08)",
        "authority": "경험담",
    },
}

COLLECTIONS = {key: meta["collection"] for key, meta in COLLECTION_REGISTRY.items()}

# 같은 소주제(section)에서 올라오는 청크 상한. AARK는 항목 하나가 곧 post_id라
# MAX_CHUNKS_PER_POST 가 사실상 동작하지 않아 별도 상한이 필요하다.
MAX_CHUNKS_PER_SECTION = 2
_STREAM_DONE = object()

# Search cache: key -> (timestamp, results, retrieval metadata)
_search_cache: dict[str, tuple[float, list[dict], dict[str, Any]]] = {}
_CACHE_TTL = 300  # seconds
_CACHE_MAX = 100
MAX_CHUNKS_PER_POST = 2

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
        # Google-maintained alias: avoids another outage when a dated preview
        # model is retired.  The resolved version is recorded per turn.
        "model_id": "gemini-pro-latest",
        "label": "Gemini Pro (Latest)",
        "credits": 1,
        "thinking_level": "high",
        "pricing": {"input": 2.50, "output": 15.00, "thinking": 15.00},
    },
    "claude-sonnet-4.6": {
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-6-20250514",
        "label": "Claude Sonnet 4.6",
        "credits": 1,
        "thinking_level": "high",
        "pricing": {"input": 3.00, "output": 15.00, "thinking": 15.00},
    },
    "claude-opus-4.6": {
        "provider": "anthropic",
        "model_id": "claude-opus-4-6-20250514",
        "label": "Claude Opus 4.6",
        "credits": 1,
        "thinking_level": "max",
        "pricing": {"input": 5.00, "output": 25.00, "thinking": 25.00},
    },
}

SYSTEM_PROMPT = """\
당신은 KSAE 대학생 자작자동차대회 전문 어시스턴트 PitBot입니다. 답변은 한국어로 작성합니다.

# 근거 사용 원칙
- 문서의 종류나 명목상 권위보다 **현재 질문에 직접 답하는 정도**를 우선하세요.
- 검색 문서에 실제로 적힌 내용과 일반 공학 지식을 명확히 구분하세요. 문서에 없는 규정 번호, 수치, 허용 여부를 만들어내지 마세요.
- Q&A 문서는 `[답변]`에 적힌 내용만 답변 근거로 사용하세요. 질문자의 질문이나 추측을 답변으로 취급하지 마세요.
- AARK 신뢰도는 정보의 합의 수준입니다. `미해결`은 결론으로 단정하지 말고, `단일제보`는 한 사례임을 밝히세요.
- 근거끼리 충돌하면 억지로 하나를 정답으로 고르지 말고 충돌 내용과 판단에 필요한 추가 조건을 짚으세요.
- 검색 결과가 질문과 직접 관련되지 않으면 인용하지 마세요. 규정·검차의 허용 여부를 일반 지식만으로 단정하지 마세요.

# 답변 방식
- 먼저 결론을 말하고, 이어서 근거와 적용 조건/예외를 설명하세요.
- 단순 질문은 3~6문장 정도로 간결하게 답하세요. 복잡한 절차나 비교에만 제목과 목록을 사용하세요.
- 근거 문서를 자연스럽게 지칭하고 URL이 있으면 링크하세요. 모든 문서를 억지로 인용할 필요는 없습니다.
- 일반 공학 지식을 보충할 때는 `일반적인 공학 관점에서는`처럼 출처와 구분하세요.
- 질문이 불완전하거나 대회 종목·차량 조건에 따라 답이 달라지면, 장문의 추측 대신 가장 중요한 확인 질문 하나를 먼저 하세요.
- 검색 실패와 관련 문서 부재를 구분하세요. 검색 자체가 실패했다면 근거가 없는 답을 생성하지 마세요.
- 자기소개, 인사말, 상투적인 최신성·권위 경고를 넣지 말고 바로 본론으로 들어가세요.\
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
        timeout=10,
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provider_available(model_key: str) -> bool:
    cfg = MODEL_CONFIG[model_key]
    if cfg["provider"] == "gemini":
        return _gemini is not None
    if cfg["provider"] == "anthropic":
        return _anthropic is not None
    return False


def _set_model_health(
    model_key: str,
    healthy: bool,
    *,
    resolved_model: str | None = None,
    error: str | None = None,
) -> None:
    _model_health[model_key] = {
        "healthy": healthy,
        "resolved_model": resolved_model,
        "error": error,
        "last_checked_at": _utc_now(),
    }


def _gemini_text(chunk: Any) -> str:
    """Read visible text without failing on thought/metadata-only chunks."""
    try:
        return chunk.text or ""
    except (AttributeError, ValueError):
        return ""


def check_qdrant_health() -> bool:
    try:
        if _qdrant is None:
            return False
        response = _qdrant.get_collections()
        available = {collection.name for collection in response.collections}
        return set(COLLECTIONS.values()).issubset(available)
    except Exception:
        logger.exception("Qdrant health check failed")
        return False


def get_health_status(include_errors: bool = False) -> dict[str, Any]:
    model_states = {
        key: {
            **_model_health.get(key, {"healthy": None, "resolved_model": None, "error": None, "last_checked_at": None}),
            "available": is_model_available(key),
        }
        for key in ROUTING_MODEL_KEYS
    }
    if not include_errors:
        for state in model_states.values():
            state.pop("error", None)
    return {
        "qdrant": check_qdrant_health(),
        "models": model_states,
        "any_model_available": any(state["available"] for state in model_states.values()),
        "prompt_version": PROMPT_VERSION,
        "app_version": os.environ.get("APP_VERSION", "development"),
    }


def set_model_admin_settings(model_key: str, enabled: bool) -> None:
    """Enable or disable a routing model; per-model credit pricing is retired."""
    set_model_settings(model_key, enabled, None)
    _model_enabled[model_key] = enabled


def get_effective_credits(model_key: str) -> int:
    """Return the single fixed charge retained for backward-compatible APIs."""
    if model_key not in MODEL_CONFIG:
        raise KeyError(model_key)
    return CHAT_CREDIT_COST


def is_model_available(model: str) -> bool:
    """Check provider and admin availability.

    Runtime health is diagnostic only: a transient failure must not permanently
    prevent the primary model from being retried on later questions.
    """
    cfg = MODEL_CONFIG.get(model)
    if not cfg:
        return False
    if not _model_enabled.get(model, True):
        return False
    if not _provider_available(model):
        return False
    return True


def get_all_models_admin() -> list[dict]:
    """Return the fixed Pro-primary/Flash-fallback routing configuration."""
    result = []
    for model_key in ROUTING_MODEL_KEYS:
        cfg = MODEL_CONFIG[model_key]
        provider_available = _provider_available(model_key)
        admin_enabled = _model_enabled.get(model_key, True)
        health = _model_health.get(model_key, {})
        result.append({
            "id": model_key,
            "label": cfg["label"],
            "role": "primary" if model_key == PRIMARY_MODEL_KEY else "fallback",
            "provider": cfg["provider"],
            "provider_available": provider_available,
            "admin_enabled": admin_enabled,
            "available": provider_available and admin_enabled,
            "healthy": health.get("healthy"),
            "resolved_model": health.get("resolved_model"),
            "health_error": health.get("error"),
            "last_checked_at": health.get("last_checked_at"),
        })
    return result


def _search_sparse_collection(
    vector: list[float],
    col_name: str,
    limit: int,
    qf: models.Filter | None,
    sparse: models.SparseVector,
) -> Any:
    """Query a v2 collection that carries named dense + BGE-M3 sparse vectors.

    The sparse arm gives exact lexical weight to rare tokens (part numbers,
    model codes) that a dense vector alone smooths away; RRF fuses the two.
    """
    return _qdrant.query_points(
        collection_name=col_name,
        prefetch=[
            models.Prefetch(query=vector, using="dense", limit=limit * 2, filter=qf),
            models.Prefetch(query=sparse, using="sparse", limit=limit * 2, filter=qf),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=limit,
        with_vectors=["dense"],
    )


def _search_collection(
    vector: list[float],
    col_name: str,
    limit: int,
    min_score: float,
    qf: models.Filter | None,
    query_text: str | None = None,
    sparse: models.SparseVector | None = None,
    error_sink: dict[str, str] | None = None,
) -> list[dict]:
    """Search a single Qdrant collection and return formatted hits."""
    try:
        if sparse is not None:
            results = _search_sparse_collection(vector, col_name, limit, qf, sparse)
        elif query_text is not None:
            # Hybrid search: dense + dense-with-text-filter, fused with RRF
            text_conditions = [
                models.FieldCondition(key="content", match=models.MatchText(text=query_text))
            ]
            if qf is not None:
                text_conditions.extend(qf.must or [])
            text_filter = models.Filter(must=text_conditions)
            results = _qdrant.query_points(
                collection_name=col_name,
                prefetch=[
                    models.Prefetch(query=vector, limit=limit * 2, filter=qf),
                    models.Prefetch(query=vector, limit=limit * 2, filter=text_filter),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
                with_vectors=True,
            )
        else:
            results = _qdrant.query_points(
                collection_name=col_name,
                query=vector,
                limit=limit,
                query_filter=qf,
            )
    except Exception as e:
        if sparse is not None:
            logger.error("Sparse hybrid search failed for '%s': %s", col_name, e)
            if error_sink is not None:
                error_sink[col_name] = f"sparse search failed: {e}"
            return []
        if query_text is not None:
            # Fallback to dense-only search if hybrid fails (e.g., no text index)
            logger.warning("Hybrid search failed for '%s', falling back to dense: %s", col_name, e)
            try:
                results = _qdrant.query_points(
                    collection_name=col_name,
                    query=vector,
                    limit=limit,
                    query_filter=qf,
                )
            except Exception as e2:
                logger.error("Dense search also failed for '%s': %s", col_name, e2)
                if error_sink is not None:
                    error_sink[col_name] = f"hybrid and dense search failed: {e2}"
                return []
            if error_sink is not None:
                error_sink[col_name] = f"hybrid search degraded to dense: {e}"
        else:
            logger.error("Qdrant query failed for '%s': %s", col_name, e)
            if error_sink is not None:
                error_sink[col_name] = f"dense search failed: {e}"
            return []

    # For hybrid (RRF) results, compute actual cosine similarity from returned vectors.
    # RRF scores are rank-based (not similarity-based) and misleading for thresholds/display.
    is_hybrid = query_text is not None or sparse is not None
    use_cosine = False
    if is_hybrid and results.points:
        _v0 = getattr(results.points[0], 'vector', None)
        if isinstance(_v0, dict):
            _v0 = _v0.get('dense')
        if _v0 is not None:
            use_cosine = True
            q_vec = np.asarray(vector, dtype=np.float32)
            q_norm = float(np.linalg.norm(q_vec))

    hits = []
    for hit in results.points:
        hit_vec = hit.vector
        if isinstance(hit_vec, dict):          # v2 컬렉션은 named vector
            hit_vec = hit_vec.get("dense")
        if use_cosine and hit_vec is not None:
            d_vec = np.asarray(hit_vec, dtype=np.float32)
            d_norm = float(np.linalg.norm(d_vec))
            score = float(np.dot(q_vec, d_vec) / (q_norm * d_norm)) if q_norm > 0 and d_norm > 0 else 0.0
        else:
            score = hit.score

        # Skip min_score for raw RRF scores (different scale); apply for cosine/dense scores
        if is_hybrid and not use_cosine:
            pass
        elif score < min_score:
            continue

        payload = hit.payload or {}
        content = payload.get("content", "") or payload.get("chunk_text", "")

        # A sizeable part of the archived Q&A collection contains only the
        # user's question.  Treating that text as an answer was a recurring
        # source of confident hallucinations.
        if col_name == COLLECTIONS.get("qna") and "[답변]" not in content:
            continue

        if payload.get("source_type") == "aark":
            # Anonymous community knowledge base: no URL, confidence-graded.
            # Checked before "chapter" since this payload also carries one.
            label = "AARK"
            if payload.get("confidence"):
                label += f"·{payload['confidence']}"
            path = [f"{payload.get('chapter_num', '')}. {payload.get('chapter', '')}".strip(". ")]
            for key in ("section", "topic"):
                if payload.get(key):
                    path.append(payload[key])
            source = f"[{label}] " + " > ".join(p for p in path if p)
            url = ""
        elif "title" in payload:
            category = payload.get("category") or ""
            source = f"[{category}] {payload['title']}" if category else payload["title"]
            url = payload.get("url", "")
        elif "chapter" in payload:
            source = f"제{payload.get('chapter_num', '')}장 {payload.get('chapter', '')} > {payload.get('section', '')}"
            url = ""
        else:
            source = ""
            url = ""

        hit_item = {
            "score": score,
            "source": source,
            "url": url,
            "content": content,
            "collection": next((key for key, name in COLLECTIONS.items() if name == col_name), col_name),
            "source_key": str(
                payload.get("source_key")
                or payload.get("url")
                or payload.get("source_file")
                or payload.get("id")
                or source
            ),
        }
        if "id" in payload:
            hit_item["post_id"] = payload["id"]
        # 익명 채팅 출처는 URL이 없다. 발언 날짜와 신뢰도가 유일한 검증 단서이므로
        # 클라이언트까지 내려보내 출처 항목에 표시한다.
        if payload.get("confidence"):
            hit_item["confidence"] = payload["confidence"]
        if payload.get("dates"):
            hit_item["dates"] = payload["dates"]
        if payload.get("section"):
            hit_item["section"] = payload["section"]
        if payload.get("chapter"):
            hit_item["chapter"] = payload["chapter"]
        if payload.get("category"):
            hit_item["category"] = payload["category"]
        if payload.get("competition"):
            hit_item["competition"] = payload["competition"]
        hits.append(hit_item)

    hits.sort(key=lambda x: x["score"], reverse=True)
    return hits


def search_with_metadata(
    query: str,
    limit: int = 7,
    min_score: float = 0.0,
    collections: list[str] | None = None,
    category: str | None = None,
    confidence: list[str] | None = None,
    min_per_collection: int = 0,
) -> tuple[list[dict], dict[str, Any]]:
    """Search a broad candidate pool and return results plus failure metadata.

    ``min_per_collection`` remains in the public signature for older callers,
    but is intentionally not enforced.  A low-relevance collection no longer
    receives a guaranteed context slot.
    """
    # ``confidence`` is retained only for backward-compatible callers. AARK
    # retrieval always searches every confidence level.
    del min_per_collection, confidence
    if not collections:
        collections = list(COLLECTIONS.keys())
    valid_keys = [key for key in collections if key in COLLECTIONS]
    collection_names = [COLLECTIONS[key] for key in valid_keys]
    metadata: dict[str, Any] = {
        "status": "ok",
        "requested_collections": valid_keys,
        "failed_collections": {},
    }
    if not collection_names:
        metadata["status"] = "failed"
        metadata["failed_collections"] = {"request": "no valid collection selected"}
        return [], metadata

    candidate_limit = max(limit * 4, 24)
    cache_key = hashlib.sha256(
        f"{query}|{candidate_limit}|{min_score}|{','.join(sorted(valid_keys))}"
        f"|{category}".encode()
    ).hexdigest()
    now = time.monotonic()
    cached = _search_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1][:candidate_limit], dict(cached[2])

    try:
        vector = _model.encode(query).tolist()
    except Exception as exc:
        metadata["status"] = "failed"
        metadata["failed_collections"] = {"embedding": str(exc)[:500]}
        logger.exception("Query embedding failed")
        return [], metadata

    category_filter = None
    if category:
        category_filter = models.Filter(
            must=[models.FieldCondition(key="category", match=models.MatchValue(value=category))]
        )

    collection_filters = {
        COLLECTIONS.get("qna"): category_filter,
        COLLECTIONS.get("kb"): None,
    }

    per_collection: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(collection_names)) as executor:
        futures = {
            executor.submit(
                _search_collection,
                vector,
                col_name,
                candidate_limit,
                min_score,
                collection_filters.get(col_name),
                query,
                None,
                failures,
            ): col_name
            for col_name in collection_names
        }
        for future in as_completed(futures):
            col_name = futures[future]
            try:
                per_collection[col_name] = future.result()
            except Exception as exc:
                failures[col_name] = str(exc)[:500]
                per_collection[col_name] = []

    output = [item for hits in per_collection.values() for item in hits]
    output.sort(key=lambda item: item["score"], reverse=True)

    # Deduplicate over the complete candidate pool, then backfill until the
    # reranker pool is full.  This avoids returning fewer documents merely
    # because the first few candidates were duplicates.
    seen_posts: dict[object, int] = {}
    seen_sections: dict[tuple[str, str, str], int] = {}
    deduped: list[dict] = []
    for item in output:
        pid = item.get("post_id")
        if pid is not None:
            count = seen_posts.get(pid, 0)
            if count >= MAX_CHUNKS_PER_POST:
                continue
            seen_posts[pid] = count + 1
        stable_section = (
            item.get("source_key", ""),
            item.get("chapter", ""),
            item.get("section", ""),
        )
        if any(stable_section):
            count = seen_sections.get(stable_section, 0)
            if count >= MAX_CHUNKS_PER_SECTION:
                continue
            seen_sections[stable_section] = count + 1
        deduped.append(item)
        if len(deduped) >= candidate_limit:
            break

    metadata["failed_collections"] = {
        next((key for key, name in COLLECTIONS.items() if name == collection), collection): error
        for collection, error in failures.items()
    }
    if failures:
        metadata["status"] = "failed" if not output and len(failures) == len(collection_names) else "partial"
    metadata["candidate_count"] = len(deduped)

    if metadata["status"] == "ok":
        if len(_search_cache) >= _CACHE_MAX:
            oldest_key = min(_search_cache, key=lambda key: _search_cache[key][0])
            del _search_cache[oldest_key]
        _search_cache[cache_key] = (now, deduped, metadata)
    return deduped, metadata


def search(
    query: str,
    limit: int = 7,
    min_score: float = 0.0,
    collections: list[str] | None = None,
    category: str | None = None,
    confidence: list[str] | None = None,
    min_per_collection: int = 0,
) -> list[dict]:
    """Backward-compatible search API used by MCP and local tooling."""
    hits, _ = search_with_metadata(
        query, limit, min_score, collections, category, confidence, min_per_collection
    )
    return hits[:limit]


def _question_needs_clarification(query: str) -> bool:
    normalized = re.sub(r"\s+", " ", query.strip())
    if len(normalized) < 4:
        return True
    return bool(
        re.fullmatch(
            r"(이거|저거|그거|이것|저것|그것)?\s*(어때|뭐야|가능해|알려줘|설명해줘)[?？ ]*",
            normalized,
            re.IGNORECASE,
        )
    )


def _build_prompt(
    query: str,
    sources: list[dict],
    search_query: str | None = None,
    retrieval_status: str = "ok",
) -> str:
    """Build the user prompt with search context."""
    if sources:
        context_parts = []
        for i, s in enumerate(sources, 1):
            header = f"[문서 {i}] {s['source']}"
            if s["url"]:
                header += f"\nURL: {s['url']}"
            context_parts.append(f"{header}\n{s['content']}")

        context = "\n\n---\n\n".join(context_parts)
        prompt = f"다음은 검색된 참고 문서입니다:\n\n{context}\n\n---\n\n"
    else:
        prompt = "질문에 직접 관련된 참고 문서를 찾지 못했습니다. 문서 근거가 필요한 허용 여부·검차 판단은 단정하지 마세요.\n\n"

    if search_query and search_query != query:
        prompt += f"(검색에 사용된 쿼리: {search_query})\n"
    prompt += f"(검색 상태: {retrieval_status})\n"
    if _question_needs_clarification(query):
        prompt += "(질문이 불완전합니다. 추측해서 길게 답하지 말고 가장 중요한 확인 질문 하나만 하세요.)\n"
    prompt += f"사용자 질문: {query}"
    return prompt


_FOLLOWUP_PATTERN = re.compile(
    r"(^|\s)(그|이|저)(것|거|규정|부분|경우)|"
    r"^(아니|아니요|그게 아니라|정정|다시)|"
    r"(위에서|방금|앞에서|내가 말한|아까)",
    re.IGNORECASE,
)


def _should_rewrite_query(query: str, history: list[dict] | None) -> bool:
    if not history:
        return False
    normalized = query.strip()
    return bool(_FOLLOWUP_PATTERN.search(normalized) or len(normalized) < 18)


def _preserves_query_identifiers(original: str, rewritten: str) -> bool:
    identifiers = set(re.findall(r"\b(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*[0-9A-Z])[A-Za-z0-9-]{2,}\b", original))
    competition_terms = re.findall(
        r"스마트\s*e\s*모빌리티|e[- ]?formula|e[- ]?포뮬러|포뮬러|formula|바하|baja|전기차|\bev\b",
        original,
        re.IGNORECASE,
    )
    return all(token.lower() in rewritten.lower() for token in identifiers | set(competition_terms))


async def _rewrite_query(query: str, history: list[dict] | None) -> str | None:
    """Rewrite a follow-up query into a standalone search query using conversation history.

    Returns the rewritten query, or None if rewriting was skipped or failed.
    """
    if not _should_rewrite_query(query, history):
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
                model=MODEL_CONFIG[FALLBACK_MODEL_KEY]["model_id"],
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
        if rewritten and rewritten != query and _preserves_query_identifiers(query, rewritten):
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


def _error_details(e: Exception, provider: str) -> dict[str, Any]:
    """Return a persistable, user-safe provider error."""
    msg = str(e).lower()
    code = "provider_error"
    retryable = True
    if "503" in msg or "unavailable" in msg or "overloaded" in msg:
        code = "unavailable"
        user_message = f"{provider} 서버가 일시적으로 과부하 상태입니다. 잠시 후 다시 시도해주세요."
    elif "429" in msg or "rate" in msg or "quota" in msg or "resource_exhausted" in msg:
        code = "rate_limited"
        user_message = f"{provider} API 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."
    elif "401" in msg or "403" in msg or "permission" in msg or "authentication" in msg:
        code = "authentication"
        retryable = False
        user_message = f"{provider} API 인증에 실패했습니다. 관리자에게 문의해주세요."
    elif "404" in msg or "not found" in msg or "no longer available" in msg:
        code = "model_not_found"
        retryable = False
        user_message = f"{provider} 모델을 사용할 수 없어 대체 모델을 확인하고 있습니다."
    elif "timeout" in msg or "deadline" in msg:
        code = "timeout"
        user_message = f"{provider} 서버 응답 시간이 초과되었습니다. 잠시 후 다시 시도해주세요."
    elif "400" in msg or "invalid" in msg:
        code = "invalid_request"
        retryable = False
        user_message = f"{provider} 요청 처리 중 오류가 발생했습니다. 질문을 수정하여 다시 시도해주세요."
    else:
        user_message = f"{provider} 응답 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    return {
        "provider": provider.lower(),
        "code": code,
        "message": str(e)[:1000],
        "user_message": user_message,
        "retryable": retryable,
    }


def _classify_error(e: Exception, provider: str) -> str:
    """Backward-compatible user-facing error classifier."""
    return _error_details(e, provider)["user_message"]


async def _stream_gemini(
    contents: list,
    model_key: str,
    fallback_from: str | None = None,
) -> AsyncIterator[str]:
    """Stream Gemini with structured model, error, and usage metadata."""
    model_config = MODEL_CONFIG[model_key]
    input_tokens = 0
    output_tokens = 0
    thinking_tokens = 0
    resolved_model = model_config["model_id"]
    finish_reason = None
    emitted_model = False
    emitted_text = False

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
            resolved_model = getattr(chunk, "model_version", None) or resolved_model
            if not emitted_model:
                model_data = {
                    "requested_model": fallback_from or model_key,
                    "resolved_model": model_key,
                    "resolved_model_id": resolved_model,
                    "fallback_from": fallback_from,
                }
                yield f"event: model\ndata: {json.dumps(model_data, ensure_ascii=False)}\n\n"
                emitted_model = True
            chunk_text = _gemini_text(chunk)
            if chunk_text:
                emitted_text = True
                data = json.dumps(chunk_text, ensure_ascii=False)
                yield f"event: token\ndata: {data}\n\n"
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                um = chunk.usage_metadata
                if hasattr(um, "prompt_token_count") and um.prompt_token_count is not None:
                    input_tokens = um.prompt_token_count
                if hasattr(um, "candidates_token_count") and um.candidates_token_count is not None:
                    output_tokens = um.candidates_token_count
                if hasattr(um, "thoughts_token_count") and um.thoughts_token_count is not None:
                    thinking_tokens = um.thoughts_token_count
            candidates = getattr(chunk, "candidates", None) or []
            if candidates:
                reason = getattr(candidates[0], "finish_reason", None)
                if reason is not None:
                    finish_reason = getattr(reason, "name", None) or str(reason)
        if not emitted_text:
            raise RuntimeError("model returned no visible text")
        _set_model_health(model_key, True, resolved_model=resolved_model)
    except Exception as e:
        logger.exception("Gemini streaming error: %s", e)
        _set_model_health(model_key, False, error=str(e)[:500])
        error_data = json.dumps(_error_details(e, "Gemini"), ensure_ascii=False)
        yield f"event: error\ndata: {error_data}\n\n"
        return

    usage_data = json.dumps({
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "resolved_model": model_key,
        "resolved_model_id": resolved_model,
        "finish_reason": finish_reason,
    })
    yield f"event: usage\ndata: {usage_data}\n\n"


async def _stream_anthropic(
    model_key: str,
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
    finish_reason = None
    emitted_text = False

    try:
        model_data = {
            "requested_model": model_key,
            "resolved_model": model_key,
            "resolved_model_id": model_config["model_id"],
            "fallback_from": None,
        }
        yield f"event: model\ndata: {json.dumps(model_data, ensure_ascii=False)}\n\n"
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
                        text = event.delta.text
                        if text:
                            emitted_text = True
                            data = json.dumps(text, ensure_ascii=False)
                            yield f"event: token\ndata: {data}\n\n"

            # Get final message for usage
            response = await stream.get_final_message()
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            finish_reason = getattr(response, "stop_reason", None)

            # Thinking tokens are included in output_tokens for billing,
            # but may be available separately via usage metadata
            if hasattr(response.usage, "thinking_tokens"):
                thinking_tokens = response.usage.thinking_tokens
            if not emitted_text:
                raise RuntimeError("model returned no visible text")

    except Exception as e:
        logger.exception("Anthropic streaming error: %s", e)
        error_data = json.dumps(_error_details(e, "Claude"), ensure_ascii=False)
        yield f"event: error\ndata: {error_data}\n\n"
        return

    usage_data = json.dumps({
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "resolved_model": model_key,
        "resolved_model_id": model_config["model_id"],
        "finish_reason": finish_reason,
    })
    yield f"event: usage\ndata: {usage_data}\n\n"


_COMPETITION_PATTERNS = (
    ("smart_e_mobility", re.compile(r"스마트\s*e\s*모빌리티|스마트.{0,5}모빌리티", re.IGNORECASE)),
    ("e_formula", re.compile(r"e[- ]?formula|e[- ]?포뮬러|전기\s*포뮬러", re.IGNORECASE)),
    ("formula", re.compile(r"포뮬러|formula|포뮬라", re.IGNORECASE)),
    ("baja", re.compile(r"바하|baja", re.IGNORECASE)),
    ("ev", re.compile(r"\bev\b|전기차|electric\s+vehicle", re.IGNORECASE)),
)

_COMPETITION_CATEGORY = {
    "smart_e_mobility": "EV",
    "e_formula": "Formula",
    "formula": "Formula",
    "baja": "Baja",
    "ev": "EV",
}


def _detect_competition(query: str) -> str | None:
    for competition, pattern in _COMPETITION_PATTERNS:
        if pattern.search(query):
            return competition
    return None


def _detect_category(query: str) -> str | None:
    """Detect competition category from query keywords."""
    competition = _detect_competition(query)
    return _COMPETITION_CATEGORY.get(competition)


def _source_matches_competition(source: dict, competition: str | None) -> bool:
    """Reject only documents that explicitly name a different competition."""
    if not competition:
        return True
    text = f"{source.get('source', '')}\n{source.get('content', '')[:1200]}"
    source_competition = source.get("competition") or _detect_competition(text)
    return source_competition is None or source_competition == competition


def _rerank_excerpt(source: dict) -> str:
    content = source.get("content", "")
    if "[답변]" in content:
        question, answer = content.split("[답변]", 1)
        return f"{question[:300]}\n[답변]{answer[:900]}"
    return content[:1000]


async def _rerank_results(query: str, sources: list[dict], limit: int = 7) -> list[dict]:
    """Re-rank search results using LLM-based relevance scoring."""
    if not sources or len(sources) <= 1:
        return sources[:limit]

    docs = []
    for i, s in enumerate(sources):
        docs.append(f"[{i}] {s['source']}\n{_rerank_excerpt(s)}")
    docs_text = "\n\n".join(docs)

    prompt = f"""다음 검색 결과가 사용자의 질문에 직접 답하는 정도를 0-10점으로 평가하세요.
문서 종류나 명목상 권위는 점수에 반영하지 마세요. 단어만 겹치고 종목·부품·조건이 다르면 낮게 평가하세요.
JSON 배열로만 응답하세요: [{{"index": 0, "score": 8}}, ...]

질문: {query}

검색 결과:
{docs_text}"""

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: _gemini.models.generate_content(
                model=MODEL_CONFIG[FALLBACK_MODEL_KEY]["model_id"],
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=700,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                ),
            ),
        )
        text = response.text.strip()
        # Extract JSON array from response
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            return sources[:limit]
        scores = json.loads(match.group())
        score_map = {
            int(item["index"]): float(item["score"])
            for item in scores
            if isinstance(item, dict)
            and isinstance(item.get("index"), int)
            and 0 <= item["index"] < len(sources)
            and isinstance(item.get("score"), (int, float))
        }
        if not score_map:
            return sources[:limit]

        # Filter low-relevance results and re-sort
        reranked = []
        for i, s in enumerate(sources):
            relevance = score_map.get(i, 0)
            if relevance >= 3:
                item = dict(s)
                item["rerank_score"] = relevance
                reranked.append(item)
        reranked.sort(key=lambda x: (x.get("rerank_score", 0), x.get("score", 0)), reverse=True)
        return reranked[:limit]
    except Exception as e:
        logger.warning("Re-ranking failed, using original results: %s", e)
        return sources[:limit]


async def search_and_stream(
    query: str,
    limit: int = 7,
    min_score: float = 0.0,
    history: list[dict] | None = None,
    collections: list[str] | None = None,
    category: str | None = None,
    competition: str | None = None,
    confidence: list[str] | None = None,
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
    Model routing is fixed: Gemini Pro first, then Gemini Flash on failure.
    """
    # Legacy callers may still pass ``confidence``; it is intentionally
    # ignored so AARK retrieval always covers the complete collection.
    del confidence
    primary_available = is_model_available(PRIMARY_MODEL_KEY)
    fallback_available = is_model_available(FALLBACK_MODEL_KEY)
    if not primary_available and not fallback_available:
        error = {
            "provider": "gemini",
            "code": "model_unavailable",
            "message": "both primary and fallback models are unavailable",
            "user_message": "답변 모델을 현재 사용할 수 없습니다. 잠시 후 다시 시도해주세요.",
            "retryable": True,
        }
        yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
        yield "event: done\ndata: {}\n\n"
        return

    model = PRIMARY_MODEL_KEY if primary_available else FALLBACK_MODEL_KEY
    model_config = MODEL_CONFIG[model]

    if model == FALLBACK_MODEL_KEY:
        reason = {
            "provider": "gemini",
            "code": "primary_unavailable",
            "message": "primary model is disabled or its provider is unavailable",
        }
        fallback_event = {
            "from": PRIMARY_MODEL_KEY,
            "to": FALLBACK_MODEL_KEY,
            "reason": reason,
        }
        yield f"event: fallback\ndata: {json.dumps(fallback_event, ensure_ascii=False)}\n\n"

    # Step 1: Rewrite query for better search if we have conversation history
    search_query = query
    rewritten = await _rewrite_query(query, history)
    if rewritten:
        search_query = rewritten
        yield f"event: rewrite\ndata: {json.dumps(rewritten, ensure_ascii=False)}\n\n"

    # Auto-detect the exact competition first; generic "전기" no longer
    # routes unrelated electrical questions into EV.
    competition = competition or _detect_competition(search_query) or _detect_competition(query)
    if not category:
        category = _COMPETITION_CATEGORY.get(competition)

    # Step 2: Search with (possibly rewritten) query without blocking the event loop.
    retrieval_started = time.monotonic()
    loop = asyncio.get_running_loop()
    sources, retrieval_meta = await loop.run_in_executor(
        None,
        lambda: search_with_metadata(
            search_query, limit, min_score, collections, category
        ),
    )

    # A rewrite can overfit a previous bad answer.  Preserve candidates found
    # directly from the user's correction as a second retrieval path.
    if rewritten:
        original_sources, original_meta = await loop.run_in_executor(
            None,
            lambda: search_with_metadata(
                query, limit, min_score, collections, category
            ),
        )
        merged: dict[tuple[str, str], dict] = {}
        for source in sources + original_sources:
            key = (source.get("source_key", ""), source.get("content", ""))
            previous = merged.get(key)
            if previous is None or source.get("score", 0) > previous.get("score", 0):
                merged[key] = source
        sources = sorted(merged.values(), key=lambda item: item.get("score", 0), reverse=True)
        combined_failures = {
            **retrieval_meta.get("failed_collections", {}),
            **{f"original:{key}": value for key, value in original_meta.get("failed_collections", {}).items()},
        }
        retrieval_meta["failed_collections"] = combined_failures
        statuses = {retrieval_meta.get("status"), original_meta.get("status")}
        if "ok" in statuses and len(statuses) > 1:
            retrieval_meta["status"] = "partial"
        elif statuses == {"failed"}:
            retrieval_meta["status"] = "failed"

    if competition:
        sources = [source for source in sources if _source_matches_competition(source, competition)]

    # Re-rank results for relevance
    retrieval_ms = round((time.monotonic() - retrieval_started) * 1000)
    rerank_started = time.monotonic()
    sources = await _rerank_results(query, sources, limit)
    rerank_ms = round((time.monotonic() - rerank_started) * 1000)

    retrieval_event = {
        **retrieval_meta,
        "competition": competition,
        "category": category,
        "retrieval_ms": retrieval_ms,
        "rerank_ms": rerank_ms,
        "result_count": len(sources),
    }
    yield f"event: retrieval\ndata: {json.dumps(retrieval_event, ensure_ascii=False)}\n\n"

    # Yield sources event
    yield f"event: sources\ndata: {json.dumps(sources, ensure_ascii=False)}\n\n"

    if retrieval_meta.get("status") == "failed":
        error = {
            "provider": "retrieval",
            "code": "retrieval_failed",
            "message": json.dumps(retrieval_meta.get("failed_collections", {}), ensure_ascii=False)[:1000],
            "user_message": "참고 문서 검색에 실패해 근거 있는 답변을 생성하지 못했습니다. 잠시 후 다시 시도해주세요.",
            "retryable": True,
        }
        yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
        yield "event: done\ndata: {}\n\n"
        return

    # Compress history for LLM context
    compressed = _compress_history(history) if history else history

    # Step 3: Stream from the selected provider
    if model_config["provider"] == "gemini":
        # Build Gemini contents
        user_prompt = _build_prompt(
            query,
            sources,
            search_query if rewritten else None,
            retrieval_meta.get("status", "ok"),
        )
        contents = []
        for msg in compressed or []:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
        contents.append(types.Content(role="user", parts=[types.Part(text=user_prompt)]))

        emitted_text = False
        should_fallback = False
        stream_fallback_from = PRIMARY_MODEL_KEY if model == FALLBACK_MODEL_KEY else None
        async for event in _stream_gemini(contents, model, fallback_from=stream_fallback_from):
            if event.startswith("event: token"):
                emitted_text = True
            if event.startswith("event: error") and not emitted_text:
                should_fallback = (
                    model != FALLBACK_MODEL_KEY and is_model_available(FALLBACK_MODEL_KEY)
                )
                if should_fallback:
                    try:
                        reason = json.loads(event.split("data: ", 1)[1])
                    except Exception:
                        reason = {"provider": "gemini", "code": "provider_error"}
                    fallback_event = {
                        "from": model,
                        "to": FALLBACK_MODEL_KEY,
                        "reason": reason,
                    }
                    yield f"event: fallback\ndata: {json.dumps(fallback_event, ensure_ascii=False)}\n\n"
                    break
            yield event

        if should_fallback:
            logger.warning("Falling back from %s to %s", model, FALLBACK_MODEL_KEY)
            async for event in _stream_gemini(contents, FALLBACK_MODEL_KEY, fallback_from=model):
                yield event

    elif model_config["provider"] == "anthropic":
        if _anthropic is None:
            error_data = {
                "provider": "anthropic",
                "code": "authentication",
                "message": "Anthropic client unavailable",
                "user_message": "Claude 모델을 사용할 수 없습니다. 관리자에게 문의해주세요.",
                "retryable": False,
            }
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        else:
            emitted_text = False
            should_fallback = False
            async for event in _stream_anthropic(model, model_config, query, sources, compressed, search_query if rewritten else None):
                if event.startswith("event: token"):
                    emitted_text = True
                if event.startswith("event: error") and not emitted_text and is_model_available(FALLBACK_MODEL_KEY):
                    should_fallback = True
                    try:
                        reason = json.loads(event.split("data: ", 1)[1])
                    except Exception:
                        reason = {"provider": "anthropic", "code": "provider_error"}
                    fallback_event = {"from": model, "to": FALLBACK_MODEL_KEY, "reason": reason}
                    yield f"event: fallback\ndata: {json.dumps(fallback_event, ensure_ascii=False)}\n\n"
                    break
                yield event

            if should_fallback:
                user_prompt = _build_prompt(
                    query,
                    sources,
                    search_query if rewritten else None,
                    retrieval_meta.get("status", "ok"),
                )
                contents = []
                for msg in compressed or []:
                    role = "model" if msg["role"] == "assistant" else "user"
                    contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
                contents.append(types.Content(role="user", parts=[types.Part(text=user_prompt)]))
                async for event in _stream_gemini(contents, FALLBACK_MODEL_KEY, fallback_from=model):
                    yield event

    yield "event: done\ndata: {}\n\n"
