"""Golden tests for the Q&A board chunker."""

from __future__ import annotations

import json

import pytest

from src.chunker import MAX_TOKENS, QUESTION_CONTEXT_MAX_TOKENS, _token_count, chunk_posts

POSTS = [
    {
        "id": 101,
        "category": "Formula",
        "title": "리스트릭터 직경 문의",
        "date": "2026-03-11",
        "url": "https://example.org/101",
        "question_body": "리스트릭터를 20.0mm로 제작해도 되나요?",
        "answers": [
            {"body": "20mm 이하로 제작해야 합니다.", "url": "https://example.org/101#a1"},
            {"body": "공차를 고려해 19.9mm를 권장합니다.", "url": "https://example.org/101#a2"},
        ],
    },
    {
        "id": 102,
        "category": "EV",
        "title": "답변 없는 질문",
        "date": "2026-04-01",
        "url": "https://example.org/102",
        "question_body": "축전지 격리 요건이 궁금합니다.",
        "answers": [],
    },
    {
        "id": 103,
        "category": "Baja",
        "title": "구형 포맷 게시글",
        "date": "2025-09-01",
        "url": "https://example.org/103",
        "question_body": "구형 크롤 결과 호환 확인",
        "answer_body": "answer_body 문자열만 있는 예전 형식입니다.",
    },
]


@pytest.fixture()
def chunks(tmp_path):
    src = tmp_path / "posts.json"
    src.write_text(json.dumps(POSTS, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "chunks.json"
    return chunk_posts(src, out)


def test_each_answer_is_chunked_independently(chunks):
    """답변별로 나뉘어야 답변 단위로 검색된다."""
    p101 = [c for c in chunks if c["post_id"] == 101]
    assert len(p101) == 2
    assert {c["chunk_index"] for c in p101} == {0, 1}
    assert p101[0]["url"].endswith("#a1")
    assert p101[1]["url"].endswith("#a2")


def test_answer_chunk_carries_question_context(chunks):
    first = next(c for c in chunks if c["post_id"] == 101)
    assert "[질문] 리스트릭터 직경 문의" in first["text"]
    assert "[답변]" in first["text"]
    assert "20mm 이하로 제작해야 합니다." in first["text"]


def test_post_without_answers_is_still_indexed(chunks):
    p102 = [c for c in chunks if c["post_id"] == 102]
    assert len(p102) == 1
    assert "축전지 격리 요건" in p102[0]["text"]
    assert p102[0]["category"] == "EV"
    assert p102[0]["has_answer"] is False


def test_answer_availability_is_explicit_metadata(chunks):
    answered = [c for c in chunks if c["post_id"] in (101, 103)]
    assert answered
    assert all(c["has_answer"] is True for c in answered)


def test_legacy_answer_body_is_supported(chunks):
    """예전 크롤 결과(answer_body 문자열)도 계속 처리되어야 한다."""
    p103 = [c for c in chunks if c["post_id"] == 103]
    assert len(p103) == 1
    assert "answer_body 문자열만 있는 예전 형식입니다." in p103[0]["text"]


def test_metadata_is_preserved(chunks):
    for c in chunks:
        post = next(p for p in POSTS if p["id"] == c["post_id"])
        assert c["category"] == post["category"]
        assert c["title"] == post["title"]
        assert c["date"] == post["date"]


def test_every_chunk_fits_the_token_budget(chunks):
    assert [c["post_id"] for c in chunks if _token_count(c["text"]) > MAX_TOKENS] == []


def test_long_question_body_is_truncated_as_context(tmp_path):
    long_q = "가" * 4000
    posts = [{
        "id": 200, "category": "Formula", "title": "긴 질문",
        "date": "2026-01-01", "url": "https://example.org/200",
        "question_body": long_q,
        "answers": [{"body": "짧은 답변", "url": "https://example.org/200#a"}],
    }]
    src = tmp_path / "p.json"
    src.write_text(json.dumps(posts, ensure_ascii=False), encoding="utf-8")
    chunks = chunk_posts(src, tmp_path / "c.json")
    # 질문 본문은 맥락용이라 잘려야 하며, 답변이 밀려나면 안 된다
    assert "짧은 답변" in "\n".join(c["text"] for c in chunks)
    head = chunks[0]["text"]
    assert _token_count(head) <= MAX_TOKENS
    assert "..." in head or _token_count(long_q) <= QUESTION_CONTEXT_MAX_TOKENS
