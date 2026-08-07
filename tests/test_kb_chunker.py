"""Golden tests for the AARK knowledge base chunker.

Every case here is a defect that actually shipped during the first ingest:
a table lost its caption, a label was split away from its value, and chunks
exceeded the token budget once the repeated header ate into it.
"""

from __future__ import annotations

import json

import pytest

from src.chunker import MAX_TOKENS, _token_count
from src.kb_chunker import chunk_kb

DOC = """\
# 제목

서문은 지식이 아니므로 청크에 들어가지 않는다.

## 1. 프레임·섀시 제작

### 파이프 소재·규격 선정

- **M6 탭 기초홀 지름** `[다수의견]`
  - **결론** — 규격상 5.0이나 **5.1~5.2**로 여유를 주는 것이 안전
  - **부품·업체** — 아크(AARK) 인발 파이프, 21.2×1.8
  - **주의** — 5.5까지 확대 시 헐거워질 수 있음
  - <sub>출처: 2026-03-11, 2026-05-02</sub>

- **중량 사례표**

    | 사례 | 중량 |
    |---|---|
    | 국내 EV | 170kg |
    | TUGraz | 154kg |

**단편 정보**
- 폐타이어는 후배 팀에 인계 `[단일제보]` (2025-11-19)
- AL6013 취급 경험 문의 → 답변 없음 `[미해결]` <sub>(2026-04-08)</sub>

### 용접 지그

#### 지그 재료 비교

| 재료 | 평가 |
|---|---|
| 스틸 각관 | 무난 |
"""


@pytest.fixture()
def chunks(tmp_path):
    src = tmp_path / "kb.md"
    src.write_text(DOC, encoding="utf-8")
    out = tmp_path / "chunks.json"
    result = chunk_kb(src, out)
    assert json.loads(out.read_text(encoding="utf-8")) == result
    return result


def _by_kind(chunks, kind):
    return [c for c in chunks if c["kind"] == kind]


def test_preamble_is_not_indexed(chunks):
    """장(##) 이전의 문서 서문은 검색 대상이 아니다."""
    assert all("서문은 지식이 아니므로" not in c["text"] for c in chunks)


def test_topic_carries_context_and_metadata(chunks):
    topic = next(c for c in _by_kind(chunks, "topic") if "M6 탭" in c["topic"])
    assert topic["chapter"] == "프레임·섀시 제작"
    assert topic["chapter_num"] == 1
    assert topic["section"] == "파이프 소재·규격 선정"
    assert topic["confidence"] == "다수의견"
    assert topic["dates"] == ["2026-03-11", "2026-05-02"]
    assert topic["source_type"] == "aark"
    assert topic["source_version"]
    # 청크는 단독으로 읽혀야 하므로 분야·주제 머리말을 갖는다
    assert "[분야] 1. 프레임·섀시 제작 > 파이프 소재·규격 선정" in topic["text"]
    assert "[주제] M6 탭 기초홀 지름 (다수의견)" in topic["text"]


def test_labels_stay_attached_to_their_values(chunks):
    """`부품·업체:` 같은 라벨이 값과 분리되면 검색 결과가 의미를 잃는다."""
    topic = next(c for c in _by_kind(chunks, "topic") if "M6 탭" in c["topic"])
    assert "결론: 규격상 5.0이나 5.1~5.2로 여유를 주는 것이 안전" in topic["text"]
    assert "부품·업체: 아크(AARK) 인발 파이프, 21.2×1.8" in topic["text"]
    assert "주의: 5.5까지 확대 시 헐거워질 수 있음" in topic["text"]


def test_nested_table_keeps_its_caption(chunks):
    """리스트 안 표는 캡션이 앞 불릿에 있다. 놓치면 엉뚱한 제목이 붙는다."""
    table = next(c for c in _by_kind(chunks, "table") if "국내 EV" in c["text"])
    assert table["topic"] == "중량 사례표"
    assert table["section"] == "파이프 소재·규격 선정"
    # 원본이 4칸 들여쓰기여도 정규화되어야 한다
    assert "\n| 국내 EV | 170kg |" in table["text"]


def test_heading_table_uses_heading_caption(chunks):
    table = next(c for c in _by_kind(chunks, "table") if "스틸 각관" in c["text"])
    assert table["topic"] == "지그 재료 비교"
    assert table["section"] == "용접 지그"


def test_brief_items_are_grouped_with_dates(chunks):
    brief = next(iter(_by_kind(chunks, "brief")))
    assert "폐타이어는 후배 팀에 인계" in brief["text"]
    assert "AL6013" in brief["text"]
    assert brief["dates"] == ["2025-11-19", "2026-04-08"]


def test_confidence_markup_is_normalized(chunks):
    """`[단일제보]` 대괄호·백틱은 노이즈다. (단일제보) 형태로 통일한다."""
    joined = "\n".join(c["text"] for c in chunks)
    assert "`[" not in joined
    assert "(단일제보)" in joined
    assert "**" not in joined
    assert "<sub>" not in joined


def test_every_chunk_fits_the_token_budget(chunks):
    over = [(c["post_id"], _token_count(c["text"])) for c in chunks
            if _token_count(c["text"]) > MAX_TOKENS]
    assert over == []


def test_post_ids_are_stable_and_unique_per_chunk(chunks):
    keys = [(c["post_id"], c["chunk_index"]) for c in chunks]
    assert len(keys) == len(set(keys))
    assert all(c["post_id"].startswith("aark-") for c in chunks)


def test_source_version_changes_with_content(tmp_path):
    """계보 추적용 지문은 내용이 바뀌면 반드시 바뀌어야 한다."""
    a = tmp_path / "a.md"
    a.write_text(DOC, encoding="utf-8")
    v1 = chunk_kb(a, tmp_path / "a.json")[0]["source_version"]

    b = tmp_path / "b.md"
    b.write_text(DOC.replace("170kg", "175kg"), encoding="utf-8")
    v2 = chunk_kb(b, tmp_path / "b.json")[0]["source_version"]

    assert v1 != v2


def test_post_ids_survive_an_unrelated_edit(tmp_path):
    """id가 위치 기반이면 항목 하나만 추가해도 뒤 항목이 전부 재배정된다.

    실제로 이 때문에 재적재 한 번에 268개 포인트가 고아가 됐다.
    """
    a = tmp_path / "a.md"
    a.write_text(DOC, encoding="utf-8")
    before = {c["post_id"] for c in chunk_kb(a, tmp_path / "a.json")}

    # 문서 앞쪽에 항목을 하나 끼워 넣는다
    inserted = DOC.replace(
        "- **M6 탭 기초홀 지름**",
        "- **새로 추가된 항목** `[단일제보]`\n"
        "  - **결론** — 앞쪽에 삽입된 항목\n"
        "  - <sub>출처: 2026-08-01</sub>\n\n"
        "- **M6 탭 기초홀 지름**",
        1,
    )
    b = tmp_path / "b.md"
    b.write_text(inserted, encoding="utf-8")
    after = {c["post_id"] for c in chunk_kb(b, tmp_path / "b.json")}

    assert before <= after, "기존 항목의 id가 바뀌었다 — 재적재 시 고아 포인트가 생긴다"
    assert len(after - before) == 1
