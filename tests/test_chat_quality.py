"""Regression tests for retrieval and model-routing defects seen in production."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src import chat


def test_pro_uses_provider_maintained_latest_alias():
    assert chat.MODEL_CONFIG["gemini-3-pro"]["model_id"] == "gemini-pro-latest"


def test_chat_routing_and_credit_cost_are_fixed():
    assert chat.PRIMARY_MODEL_KEY == "gemini-3-pro"
    assert chat.FALLBACK_MODEL_KEY == "gemini-3-flash"
    assert chat.MODEL_CONFIG[chat.FALLBACK_MODEL_KEY]["model_id"] == "gemini-3.6-flash"
    assert chat.CHAT_CREDIT_COST == 1
    assert all(chat.get_effective_credits(key) == 1 for key in chat.ROUTING_MODEL_KEYS)


def test_competition_router_distinguishes_similar_electric_classes():
    assert chat._detect_competition("스마트 e 모빌리티 제동 규정") == "smart_e_mobility"
    assert chat._detect_competition("E-Formula GLVS 장착 위치") == "e_formula"
    assert chat._detect_competition("포뮬러 검차") == "formula"
    assert chat._detect_competition("전기 배선 굵기는?") is None
    assert chat._detect_category("전기차 축전지") == "EV"


def test_explicitly_conflicting_competition_source_is_rejected():
    formula_source = {"source": "Formula 규정", "content": "포뮬러 내연기관 차량"}
    generic_source = {"source": "배선 지식", "content": "커넥터 체결 방법"}
    assert chat._source_matches_competition(formula_source, "smart_e_mobility") is False
    assert chat._source_matches_competition(generic_source, "smart_e_mobility") is True


def test_other_competition_is_treated_as_global_rule_source():
    assert chat._source_matches_competition({"competition": "other"}, "formula") is True
    assert chat._source_matches_competition({"competition": "formula"}, "formula") is True
    assert chat._source_matches_competition({"competition": "baja"}, "formula") is False


def test_build_rules_filter_includes_other_competition():
    filters = chat._build_rules_filter("formula", "event-operation")
    assert filters is not None
    competition_condition = next(condition for condition in filters.must or [] if condition.key == "competition")
    document_type_condition = next(condition for condition in filters.must or [] if condition.key == "document_type")
    assert isinstance(competition_condition.match, chat.models.MatchAny)
    assert set(competition_condition.match.any) == {"formula", "other"}
    assert isinstance(document_type_condition.match, chat.models.MatchValue)
    assert document_type_condition.match.value == "event-operation"


def test_build_rules_filter_keeps_other_as_only_competition_value():
    filters = chat._build_rules_filter("other", "vehicle-technical")
    assert filters is not None
    competition_condition = filters.must[0]
    assert isinstance(competition_condition.match, chat.models.MatchValue)
    assert competition_condition.match.value == "other"


def test_master_rules_chip_expands_only_to_available_detail_collections(monkeypatch):
    available_detail = "ksae-rules-formula-event-operation-2026-v2"
    expected_formula_detail = "rules-formula-event-operation-2026"
    expected_hidden_detail = "rules-formula-vehicle-technical-2026"

    class FakeQdrant:
        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name=available_detail)])

    original_qdrant = chat._qdrant
    chat._qdrant = FakeQdrant()
    try:
        expanded = chat.expand_collection_keys(["rules"])
    finally:
        chat._qdrant = original_qdrant

    assert "rules" in expanded
    assert expected_formula_detail in expanded
    assert expected_hidden_detail not in expanded


def test_public_collections_hides_unpopulated_rules_detail_collections(monkeypatch):
    available_detail = "ksae-rules-formula-event-operation-2026-v2"

    class FakeQdrant:
        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name=available_detail)])

    original_qdrant = chat._qdrant
    chat._qdrant = FakeQdrant()
    try:
        public = chat.get_public_collections()
    finally:
        chat._qdrant = original_qdrant

    keys = {item["key"] for item in public}
    assert "rules" in keys
    assert "qna" in keys
    assert "kb" in keys
    assert "rules-formula-event-operation-2026" in keys
    assert "rules-formula-vehicle-technical-2026" not in keys


def test_legacy_rules_collection_is_not_filtered(monkeypatch):
    captured_filters: dict[str, object] = {}
    detail_collection = chat.COLLECTIONS["rules-formula-event-operation-2026"]

    class FakeEmbedding:
        def encode(self, _query):
            return SimpleNamespace(tolist=lambda: [1.0])

    def fake_search(_vector, collection_name, _limit, _min_score, query_filter, *_args):
        captured_filters[collection_name] = query_filter
        return []

    monkeypatch.setattr(chat, "_model", FakeEmbedding())
    monkeypatch.setattr(chat, "_search_collection", fake_search)
    monkeypatch.setattr(chat, "_get_available_collections", lambda: {detail_collection})
    chat._search_cache.clear()

    chat.search_with_metadata(
        "formula 차량기술규정 제1조",
        collections=["rules"],
        category=None,
        confidence=None,
        min_per_collection=0,
    )

    assert captured_filters[chat.COLLECTIONS["rules"]] is None
    assert captured_filters[detail_collection] is not None


def test_standalone_query_is_not_needlessly_rewritten():
    history = [{"role": "user", "content": "이전 질문"}]
    assert chat._should_rewrite_query("포뮬러 GLVS 장착 위치 규정을 알려줘", history) is False
    assert chat._should_rewrite_query("아니, E-Formula 기준이야", history) is True


def test_reranker_excerpt_contains_the_answer_not_only_question():
    content = "[질문] 긴 질문\n조건을 묻습니다.\n\n[답변]\n실제 답변 핵심"
    excerpt = chat._rerank_excerpt({"content": content})
    assert "[답변]" in excerpt
    assert "실제 답변 핵심" in excerpt


def test_question_only_qna_points_are_excluded(monkeypatch):
    points = [
        SimpleNamespace(
            vector=None,
            score=0.95,
            payload={"id": 1, "title": "미답변", "content": "[질문] 허용되나요?"},
        ),
        SimpleNamespace(
            vector=None,
            score=0.90,
            payload={"id": 2, "title": "답변 있음", "content": "[질문] 허용되나요?\n[답변]\n가능합니다."},
        ),
    ]

    class FakeQdrant:
        def query_points(self, **_kwargs):
            return SimpleNamespace(points=points)

    monkeypatch.setattr(chat, "_qdrant", FakeQdrant())
    hits = chat._search_collection(
        [1.0], chat.COLLECTIONS["qna"], 10, 0.0, None
    )
    assert [hit["post_id"] for hit in hits] == [2]


def test_prompt_forbids_unsupported_permission_claims():
    assert "문서에 없는 규정 번호, 수치, 허용 여부를 만들어내지 마세요" in chat.SYSTEM_PROMPT
    prompt = chat._build_prompt("검차에 사용 가능한가요?", [], retrieval_status="ok")
    assert "허용 여부·검차 판단은 단정하지 마세요" in prompt


def test_aark_search_ignores_legacy_confidence_filter(monkeypatch):
    captured_filters = []

    class FakeEmbedding:
        def encode(self, _query):
            return SimpleNamespace(tolist=lambda: [1.0])

    def fake_search(_vector, _collection, _limit, _score, query_filter, *_args):
        captured_filters.append(query_filter)
        return []

    chat._search_cache.clear()
    monkeypatch.setattr(chat, "_model", FakeEmbedding())
    monkeypatch.setattr(chat, "_search_collection", fake_search)

    chat.search_with_metadata(
        "질문",
        collections=["kb"],
        confidence=["합의됨"],
    )

    assert "filter" not in chat.COLLECTION_REGISTRY["kb"]
    assert captured_filters == [None]
    chat._search_cache.clear()


def test_pro_failure_before_first_token_falls_back_to_flash(monkeypatch):
    async def no_rewrite(_query, _history):
        return None

    async def no_rerank(_query, sources, _limit):
        return sources

    async def fake_stream(_contents, model_key, fallback_from=None):
        if model_key == "gemini-3-pro":
            yield 'event: model\ndata: {"resolved_model":"gemini-3-pro"}\n\n'
            error = {"provider": "gemini", "code": "model_not_found", "message": "retired"}
            yield f"event: error\ndata: {json.dumps(error)}\n\n"
            return
        model = {
            "requested_model": fallback_from,
            "resolved_model": "gemini-3-flash",
            "resolved_model_id": "gemini-3.6-flash",
        }
        yield f"event: model\ndata: {json.dumps(model)}\n\n"
        yield 'event: token\ndata: "대체 응답"\n\n'
        yield 'event: usage\ndata: {"resolved_model":"gemini-3-flash"}\n\n'

    monkeypatch.setattr(chat, "_rewrite_query", no_rewrite)
    monkeypatch.setattr(chat, "_rerank_results", no_rerank)
    monkeypatch.setattr(chat, "_stream_gemini", fake_stream)
    monkeypatch.setattr(
        chat,
        "search_with_metadata",
        lambda *_args, **_kwargs: ([], {"status": "ok", "failed_collections": {}}),
    )
    monkeypatch.setattr(chat, "is_model_available", lambda key: key in chat.ROUTING_MODEL_KEYS)

    async def collect():
        return [event async for event in chat.search_and_stream("질문")]

    events = asyncio.run(collect())
    assert any(event.startswith("event: fallback") for event in events)
    assert any("대체 응답" in event for event in events)
    assert not any(event.startswith("event: error") for event in events)


def test_flash_is_used_directly_when_pro_is_unavailable(monkeypatch):
    async def no_rewrite(_query, _history):
        return None

    async def no_rerank(_query, sources, _limit):
        return sources

    called = []

    async def fake_stream(_contents, model_key, fallback_from=None):
        called.append((model_key, fallback_from))
        yield 'event: token\ndata: "대체 응답"\n\n'
        yield 'event: usage\ndata: {"resolved_model":"gemini-3-flash"}\n\n'

    monkeypatch.setattr(chat, "_rewrite_query", no_rewrite)
    monkeypatch.setattr(chat, "_rerank_results", no_rerank)
    monkeypatch.setattr(chat, "_stream_gemini", fake_stream)
    monkeypatch.setattr(
        chat,
        "search_with_metadata",
        lambda *_args, **_kwargs: ([], {"status": "ok", "failed_collections": {}}),
    )
    monkeypatch.setattr(chat, "is_model_available", lambda key: key == chat.FALLBACK_MODEL_KEY)

    async def collect():
        return [event async for event in chat.search_and_stream("질문")]

    events = asyncio.run(collect())
    assert called == [(chat.FALLBACK_MODEL_KEY, chat.PRIMARY_MODEL_KEY)]
    assert any(event.startswith("event: fallback") for event in events)
    assert any("대체 응답" in event for event in events)
