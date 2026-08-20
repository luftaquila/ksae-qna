"""Regression tests for retrieval and model-routing defects seen in production."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import chat


# 별칭은 Google 이 옮기면 예고 없이 다른 세대로 갈아탄다. 그래서 버전을 박는다.
def test_the_chain_pins_versions_instead_of_a_moving_alias():
    assert chat.MODEL_CHAIN == ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash")
    for key in chat.MODEL_CHAIN:
        assert chat.MODEL_CONFIG[key]["model_id"] == key
        assert "latest" not in chat.MODEL_CONFIG[key]["model_id"]

    # Pro 는 이 키에서 무료 티어 한도가 0 이라 라우팅에서 빠졌다.
    assert "gemini-3-pro" not in chat.ROUTING_MODEL_KEYS


def test_chat_routing_and_credit_cost_are_fixed():
    assert chat.PRIMARY_MODEL_KEY == "gemini-3.7-flash"
    assert chat.UTILITY_MODEL_KEY == "gemini-3.7-flash"
    assert chat.ROUTING_MODEL_KEYS == chat.MODEL_CHAIN
    assert all(chat.MODEL_CONFIG[key]["thinking_level"] == "high" for key in chat.MODEL_CHAIN)
    assert chat.CHAT_CREDIT_COST == 1
    assert all(chat.get_effective_credits(key) == 1 for key in chat.ROUTING_MODEL_KEYS)


def test_routing_migration_enables_the_whole_chain_once(monkeypatch):
    model_updates = []
    site_updates = []
    monkeypatch.setattr(
        chat,
        "get_model_settings_map",
        lambda: {
            chat.MODEL_CHAIN[0]: {"enabled": True},
            chat.MODEL_CHAIN[1]: {"enabled": False},
        },
    )
    monkeypatch.setattr(chat, "get_site_setting", lambda _key: "")
    monkeypatch.setattr(
        chat,
        "set_model_settings",
        lambda key, enabled, credits: model_updates.append((key, enabled, credits)),
    )
    monkeypatch.setattr(
        chat,
        "set_site_setting",
        lambda key, value: site_updates.append((key, value)),
    )

    chat.init_model_settings()

    assert model_updates == [(key, True, None) for key in chat.MODEL_CHAIN]
    assert site_updates == [("model_routing_version", chat.MODEL_ROUTING_VERSION)]
    assert all(chat._model_enabled[key] for key in chat.ROUTING_MODEL_KEYS)


def test_completed_routing_migration_preserves_admin_model_toggle(monkeypatch):
    monkeypatch.setattr(
        chat,
        "get_model_settings_map",
        lambda: {
            chat.MODEL_CHAIN[0]: {"enabled": True},
            chat.MODEL_CHAIN[1]: {"enabled": False},
        },
    )
    monkeypatch.setattr(
        chat,
        "get_site_setting",
        lambda _key: chat.MODEL_ROUTING_VERSION,
    )
    monkeypatch.setattr(
        chat,
        "set_model_settings",
        lambda *_args: pytest.fail("completed migration must not overwrite model settings"),
    )
    monkeypatch.setattr(
        chat,
        "set_site_setting",
        lambda *_args: pytest.fail("completed migration must not rewrite the marker"),
    )

    chat.init_model_settings()

    assert chat._model_enabled[chat.MODEL_CHAIN[0]] is True
    assert chat._model_enabled[chat.MODEL_CHAIN[1]] is False


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


def test_missing_electric_alias_collections_route_to_active_families():
    active_keys = [
        "rules-formula-vehicle-technical-2026",
        "rules-smart-e-mobility-vehicle-technical-2026",
    ]

    assert chat._effective_rules_competition("e_formula", active_keys) == "formula"
    assert chat._effective_rules_competition("ev", active_keys) == "smart_e_mobility"
    assert chat._effective_rules_competition("baja", active_keys) == "baja"
    assert chat._source_matches_competition(
        {"competition": "formula"},
        "e_formula",
        {"formula"},
    ) is True


def test_active_exact_e_formula_collection_does_not_fall_back():
    active_keys = [
        "rules-formula-vehicle-technical-2026",
        "rules-e-formula-vehicle-technical-2026",
    ]

    assert chat._effective_rules_competition("e_formula", active_keys) == "e_formula"


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


def test_public_collections_exposes_only_broad_source_groups():
    public = chat.get_public_collections()

    assert [item["key"] for item in public] == ["rules", "qna", "kb"]
    assert [item["description"] for item in public] == [
        "2026 대회 규정 전체 (Formula/Baja/EV)",
        "KSAE Q&A 게시판 질의응답",
        "AARK 익명톡방 (2025년 2월 ~ 2026년 7월)",
    ]
    assert all("filter" not in item for item in public)
    assert all("competition" not in item for item in public)
    assert all("document_type" not in item for item in public)


def test_collection_discovery_failure_exposes_stable_sources_only(monkeypatch):
    class FailingQdrant:
        def get_collections(self):
            raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(chat, "_qdrant", FailingQdrant())

    public_keys = [item["key"] for item in chat.get_public_collections()]
    assert public_keys == ["rules", "qna", "kb"]
    assert chat.expand_collection_keys(["rules"]) == ["rules"]


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


def test_master_rules_search_passes_global_competition_filter_to_other_collection(monkeypatch):
    captured_filters: dict[str, object] = {}
    formula_collection = chat.COLLECTIONS["rules-formula-event-operation-2026"]
    other_collection = chat.COLLECTIONS["rules-other-event-operation-2026"]

    class FakeEmbedding:
        def encode(self, _query):
            return SimpleNamespace(tolist=lambda: [1.0])

    def fake_search(_vector, collection_name, _limit, _min_score, query_filter, *_args):
        captured_filters[collection_name] = query_filter
        return []

    monkeypatch.setattr(chat, "_model", FakeEmbedding())
    monkeypatch.setattr(chat, "_search_collection", fake_search)
    monkeypatch.setattr(
        chat,
        "_get_available_collections",
        lambda: {formula_collection, other_collection},
    )
    chat._search_cache.clear()

    chat.search_with_metadata(
        "formula 경기진행규정",
        collections=["rules"],
        min_per_collection=0,
    )

    other_filter = captured_filters[other_collection]
    competition_condition = next(
        condition for condition in other_filter.must or [] if condition.key == "competition"
    )
    assert isinstance(competition_condition.match, chat.models.MatchAny)
    assert set(competition_condition.match.any) == {"formula", "other"}


def test_standalone_query_is_not_needlessly_rewritten():
    history = [{"role": "user", "content": "이전 질문"}]
    assert chat._should_rewrite_query("포뮬러 GLVS 장착 위치 규정을 알려줘", history) is False
    assert chat._should_rewrite_query("아니, E-Formula 기준이야", history) is True


def test_rule_query_normalizes_workshop_typos_and_spacing():
    normalized = chat._normalize_rule_query("베기 클램프 일반너트 노드 락")

    assert "배기 클램프" in normalized
    assert "일반 너트" in normalized
    assert "노드락" in normalized
    assert "Nord-Lock" in normalized
    assert "풀림 방지" in normalized
    assert chat._normalize_rule_query(normalized) == normalized


def test_nord_lock_query_adds_fastener_search_terms():
    normalized = chat._normalize_rule_query("베기 클램프 노드락 체결")

    assert normalized == "배기 클램프 노드락 체결 Nord-Lock 너트 체결 풀림 방지"
    assert chat._normalize_rule_query(normalized) == normalized


def test_rewrite_must_preserve_concrete_korean_terms():
    original = "배기 클램프 일반너트"

    assert chat._preserves_query_identifiers(original, "KSAE 자작자동차") is False
    assert chat._preserves_query_identifiers(original, "배기 클램프의 일반 너트 체결 규정") is True


def test_bad_rewrite_falls_back_to_previous_user_context(monkeypatch):
    class FakeModels:
        @staticmethod
        def generate_content(**_kwargs):
            return SimpleNamespace(text="KSAE 자작자동차")

    monkeypatch.setattr(chat, "_gemini", SimpleNamespace(models=FakeModels()))
    history = [
        {"role": "user", "content": "베기 클램프 노드락 체결"},
        {"role": "assistant", "content": "노드락 사용 관련 답변"},
    ]

    rewritten = asyncio.run(chat._rewrite_query("배기 클램프 일반너트", history))

    assert rewritten is not None
    assert "배기 클램프 노드락 체결" in rewritten
    assert "Nord-Lock" in rewritten
    assert "배기 클램프 일반 너트" in rewritten


def test_vehicle_component_routes_to_vehicle_technical_rules(monkeypatch):
    vehicle_key = "rules-formula-vehicle-technical-2026"
    vehicle_collection = chat.COLLECTIONS[vehicle_key]
    captured_filters: dict[str, object] = {}

    class FakeEmbedding:
        def encode(self, _query):
            return SimpleNamespace(tolist=lambda: [1.0])

    def fake_search(_vector, collection_name, _limit, _score, query_filter, *_args):
        captured_filters[collection_name] = query_filter
        return []

    chat._search_cache.clear()
    monkeypatch.setattr(chat, "_model", FakeEmbedding())
    monkeypatch.setattr(chat, "_search_collection", fake_search)
    monkeypatch.setattr(chat, "_get_available_collections", lambda: {vehicle_collection})

    _, metadata = chat.search_with_metadata("안전벨트 브라켓 규정", collections=["rules"])

    assert metadata["query_hints"]["document_type"] == "vehicle-technical"
    assert metadata["query_hints"]["document_type_filter"] == "vehicle-technical"
    detail_filter = captured_filters[vehicle_collection]
    document_condition = next(
        condition for condition in detail_filter.must or [] if condition.key == "document_type"
    )
    assert document_condition.match.value == "vehicle-technical"
    chat._search_cache.clear()


def test_explicit_safety_document_stays_a_soft_hint_when_collection_is_missing():
    vehicle_key = "rules-formula-vehicle-technical-2026"

    assert chat._infer_document_type_from_query("Formula 안전규정") == "safety"
    assert chat._effective_rules_document_type(
        "safety",
        [vehicle_key],
        "formula",
        "Formula 안전규정",
    ) is None


def test_vehicle_terms_do_not_override_explicit_event_document_type():
    assert chat._infer_document_type_from_query("Baja 시트 경기진행규정") == "event-operation"
    assert chat._infer_document_type_from_query("Baja 시트 등받이 지지 규정") == "vehicle-technical"


def test_existing_document_type_collection_keeps_hard_filter(monkeypatch):
    event_key = "rules-formula-event-operation-2026"
    event_collection = chat.COLLECTIONS[event_key]
    captured_filters: dict[str, object] = {}

    class FakeEmbedding:
        def encode(self, _query):
            return SimpleNamespace(tolist=lambda: [1.0])

    def fake_search(_vector, collection_name, _limit, _score, query_filter, *_args):
        captured_filters[collection_name] = query_filter
        return []

    chat._search_cache.clear()
    monkeypatch.setattr(chat, "_model", FakeEmbedding())
    monkeypatch.setattr(chat, "_search_collection", fake_search)
    monkeypatch.setattr(chat, "_get_available_collections", lambda: {event_collection})

    _, metadata = chat.search_with_metadata("포뮬러 경기진행규정", collections=["rules"])

    assert metadata["query_hints"]["document_type_filter"] == "event-operation"
    detail_filter = captured_filters[event_collection]
    document_condition = next(
        condition for condition in detail_filter.must or [] if condition.key == "document_type"
    )
    assert document_condition.match.value == "event-operation"
    chat._search_cache.clear()


def test_candidate_pool_caps_dominant_source_without_guaranteeing_equal_slots():
    sources = []
    for index in range(24):
        sources.append({"collection": "qna", "score": 1.0 - index / 1000})
    for index in range(8):
        sources.append({"collection": "rules-formula-vehicle-technical-2026", "score": 0.8 - index / 1000})
    for index in range(8):
        sources.append({"collection": "kb", "source_type": "aark", "score": 0.7 - index / 1000})

    balanced = chat._balance_candidate_pool(sources, 24)
    counts = chat._candidate_counts(balanced)

    assert counts == {"qna": 12, "rules": 8, "aark": 4, "other": 0}


def test_smart_e_document_type_filter_uses_internal_competition_key():
    event_key = "rules-smart-e-mobility-event-operation-2026"

    assert chat._effective_rules_document_type(
        "event-operation",
        [event_key],
        "smart_e_mobility",
        "스마트 e 모빌리티 경기진행규정",
    ) == "event-operation"


def test_electric_alias_uses_fallback_family_for_document_filter(monkeypatch):
    formula_key = "rules-formula-vehicle-technical-2026"
    smart_e_key = "rules-smart-e-mobility-vehicle-technical-2026"
    available = {chat.COLLECTIONS[formula_key], chat.COLLECTIONS[smart_e_key]}

    class FakeEmbedding:
        def encode(self, _query):
            return SimpleNamespace(tolist=lambda: [1.0])

    def fake_search(*_args, **_kwargs):
        return []

    chat._search_cache.clear()
    monkeypatch.setattr(chat, "_model", FakeEmbedding())
    monkeypatch.setattr(chat, "_search_collection", fake_search)
    monkeypatch.setattr(chat, "_get_available_collections", lambda: available)

    _, e_formula_meta = chat.search_with_metadata("E-Formula GLVS 장착 위치", collections=["rules"])
    _, ev_meta = chat.search_with_metadata("EV 방화벽 규정", collections=["rules"])

    assert e_formula_meta["query_hints"]["competition_filter"] == "formula"
    assert e_formula_meta["query_hints"]["document_type_filter"] == "vehicle-technical"
    assert ev_meta["query_hints"]["competition_filter"] == "smart_e_mobility"
    assert ev_meta["query_hints"]["document_type_filter"] == "vehicle-technical"
    chat._search_cache.clear()


def test_technical_division_rules_route_to_explicit_other_document():
    other_key = "rules-other-other-2026"

    assert chat._infer_document_type_from_query("기술부문규정") == "other"
    assert chat._effective_rules_document_type(
        "other",
        [other_key],
        None,
        "기술부문규정",
    ) == "other"


def test_aark_unresolved_is_searched_but_penalized():
    unresolved = {
        "collection": "kb",
        "source_type": "aark",
        "confidence": "미해결",
        "content": "결론: 답변 없음",
    }
    agreed = {
        "collection": "kb",
        "source_type": "aark",
        "confidence": "합의됨",
        "content": "결론: 사용할 수 있음",
    }

    assert chat._aark_evidence_penalty(unresolved) == 2.0
    assert chat._aark_evidence_penalty(agreed) == 0.0


def test_prompt_separates_rules_when_multiple_competitions_are_returned():
    sources = [
        {"source": "Formula 규정", "url": "", "content": "방화벽", "competition": "formula"},
        {"source": "Baja 규정", "url": "", "content": "방화벽", "competition": "baja"},
    ]

    prompt = chat._build_prompt("방화벽 규정 설명", sources)

    assert "이를 하나의 규정처럼 합치지 말고" in prompt


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


def test_failure_before_the_first_token_steps_down_one_generation(monkeypatch):
    async def no_rewrite(_query, _history):
        return None

    async def no_rerank(_query, sources, _limit):
        return sources

    async def fake_stream(_contents, model_key, fallback_from=None):
        if model_key == chat.MODEL_CHAIN[0]:
            yield f'event: model\ndata: {{"resolved_model":"{model_key}"}}\n\n'
            error = {"provider": "gemini", "code": "rate_limited", "message": "retry in 47s"}
            yield f"event: error\ndata: {json.dumps(error)}\n\n"
            return
        model = {
            "requested_model": fallback_from,
            "resolved_model": model_key,
            "resolved_model_id": model_key,
        }
        yield f"event: model\ndata: {json.dumps(model)}\n\n"
        yield 'event: token\ndata: "대체 응답"\n\n'
        yield f'event: usage\ndata: {{"resolved_model":"{model_key}"}}\n\n'

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


def test_the_next_generation_is_used_directly_when_the_newest_is_unavailable(monkeypatch):
    async def no_rewrite(_query, _history):
        return None

    async def no_rerank(_query, sources, _limit):
        return sources

    called = []

    async def fake_stream(_contents, model_key, fallback_from=None):
        called.append((model_key, fallback_from))
        yield 'event: token\ndata: "대체 응답"\n\n'
        yield f'event: usage\ndata: {{"resolved_model":"{model_key}"}}\n\n'

    monkeypatch.setattr(chat, "_rewrite_query", no_rewrite)
    monkeypatch.setattr(chat, "_rerank_results", no_rerank)
    monkeypatch.setattr(chat, "_stream_gemini", fake_stream)
    monkeypatch.setattr(
        chat,
        "search_with_metadata",
        lambda *_args, **_kwargs: ([], {"status": "ok", "failed_collections": {}}),
    )
    monkeypatch.setattr(chat, "is_model_available", lambda key: key == chat.MODEL_CHAIN[1])

    async def collect():
        return [event async for event in chat.search_and_stream("질문")]

    events = asyncio.run(collect())
    # 유일한 후보라 fallback_from 은 비어 있다 — 그 자리가 곧 체인의 시작이다.
    assert called == [(chat.MODEL_CHAIN[1], None)]
    assert any(event.startswith("event: fallback") for event in events)
    assert any("대체 응답" in event for event in events)


def _routing_harness(monkeypatch, fake_stream, available=None):
    """체인 라우팅만 남기고 검색·재작성·리랭크를 걷어낸다."""
    async def no_rewrite(_query, _history):
        return None

    async def no_rerank(_query, sources, _limit):
        return sources

    monkeypatch.setattr(chat, "_rewrite_query", no_rewrite)
    monkeypatch.setattr(chat, "_rerank_results", no_rerank)
    monkeypatch.setattr(chat, "_stream_gemini", fake_stream)
    monkeypatch.setattr(
        chat,
        "search_with_metadata",
        lambda *_args, **_kwargs: ([], {"status": "ok", "failed_collections": {}}),
    )
    monkeypatch.setattr(
        chat,
        "is_model_available",
        lambda key: key in (available if available is not None else chat.MODEL_CHAIN),
    )

    async def collect():
        return [event async for event in chat.search_and_stream("질문")]

    return asyncio.run(collect())


# Pro 로 넘어가던 예전 경로는 무료 티어 한도가 0 이라 한 번도 성공할 수 없었다.
# 같은 계열에서 한 세대씩 내려가면 앞 세대가 분당 한도에 걸려도 답변이 나간다.
def test_the_chain_walks_down_one_generation_at_a_time(monkeypatch):
    called = []

    async def fake_stream(_contents, model_key, fallback_from=None):
        called.append((model_key, fallback_from))
        if model_key != chat.MODEL_CHAIN[-1]:
            yield 'event: error\ndata: {"provider":"gemini","code":"rate_limited"}\n\n'
            return
        yield 'event: token\ndata: "마지막 세대 응답"\n\n'
        yield f'event: usage\ndata: {{"resolved_model":"{chat.MODEL_CHAIN[-1]}"}}\n\n'

    events = _routing_harness(monkeypatch, fake_stream)

    assert [key for key, _ in called] == list(chat.MODEL_CHAIN)
    # 두 번 내려갔으니 fallback 이벤트도 두 번.
    assert len([e for e in events if e.startswith("event: fallback")]) == 2
    # 중간 오류는 사용자에게 나가지 않는다 — 마지막 세대가 답을 냈다.
    assert not any(e.startswith("event: error") for e in events)
    assert any("마지막 세대 응답" in e for e in events)


def test_the_last_generations_error_reaches_the_user(monkeypatch):
    called = []

    async def fake_stream(_contents, model_key, fallback_from=None):
        called.append(model_key)
        yield 'event: error\ndata: {"provider":"gemini","code":"rate_limited"}\n\n'

    events = _routing_harness(monkeypatch, fake_stream)

    # 전부 시도하고, 마지막 오류는 그대로 전달돼 이용권이 환불된다.
    assert called == list(chat.MODEL_CHAIN)
    assert len([e for e in events if e.startswith("event: error")]) == 1


# 토큰이 한 번 나간 뒤에는 되돌릴 수 없으니 다음 세대로 내려가지 않는다.
def test_a_failure_after_the_first_token_is_not_retried(monkeypatch):
    called = []

    async def fake_stream(_contents, model_key, fallback_from=None):
        called.append(model_key)
        yield 'event: token\ndata: "앞부분"\n\n'
        yield 'event: error\ndata: {"provider":"gemini","code":"provider_error"}\n\n'

    events = _routing_harness(monkeypatch, fake_stream)

    assert called == [chat.MODEL_CHAIN[0]]
    assert not any(e.startswith("event: fallback") for e in events)
    assert any(e.startswith("event: error") for e in events)
