# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PitBot — KSAE(한국자동차공학회) 대학생 자작자동차대회 Q&A 게시판 크롤링 → RAG 벡터 DB → 웹 챗봇 시스템.

## Commands

```bash
# 웹 챗봇 서버 (개발)
python server.py                      # http://localhost:8000

# 테스트
pytest tests/ -q                      # 청커 골든 테스트

# 데이터 파이프라인 (Q&A 크롤링)
python main.py                        # 전체 파이프라인 (incremental)
python main.py --mode full            # full 재크롤링
python main.py --workers 10           # 병렬 크롤링 워커 수 (기본 5)
python main.py crawl|chunk|embed|upload  # 개별 스테이지

# 데이터 파이프라인 (AARK 지식베이스 — 크롤링 없음, 문서 1개)
python main.py kb --source data/raw/aark-kb.md --recreate
python main.py kb-chunk|kb-embed|kb-upload   # 개별 스테이지

# Docker
docker compose up -d                  # Traefik 리버스 프록시

# MCP 서버 (AI 클라이언트용, stdin/stdout JSON-RPC)
python mcp_server.py
```

## Architecture

### Request Flow (Chat)

1. `server.py` POST `/api/chat` — 인증 확인, 모델 검증, 이용권 차감, 세션 생성/조회
2. `src/chat.py` `search_and_stream()` — 벡터 검색 후 provider별 LLM 스트리밍
3. SSE 이벤트: `sources` → `token`(반복) → `usage` → `done`
4. `server.py`에서 asyncio.Queue로 LLM 소비/클라이언트 전달 분리 — 클라이언트 disconnect 시에도 LLM 태스크는 백그라운드에서 완료되어 응답 저장

### Multi-Model Streaming

- `src/chat.py`의 `MODEL_CONFIG` 딕셔너리가 모델 레지스트리 (model_id, provider, credits, pricing, thinking_level)
- provider별 스트리밍 분리: `_stream_gemini()` (동기 이터레이터를 `run_in_executor`로 래핑), `_stream_anthropic()` (네이티브 async)
- 모델 활성화/비활성화/크레딧 오버라이드는 `model_settings` DB 테이블 + 인메모리 캐시 (`_model_enabled`, `_model_credits`, `_model_order`)
- `GET /api/models`로 클라이언트에 사용 가능한 모델 목록 제공

### Vector Search

`mcp_server.py`와 `src/chat.py`의 `search()`에서 동일 패턴:
- BGE-M3 encode → `qdrant.query_points()` → payload에서 source/content 추출
- payload 스키마는 **`src/payloads.py`에 TypedDict로 선언**돼 있다. 산문 설명이 아니라 그쪽이 계약이다
- payload 구조로 컬렉션 타입 구분: `source_type == "aark"` → 지식베이스, `title` 있으면 Q&A, `chapter` 있으면 규정집.
  **판정 순서 주의** — 지식베이스 payload도 `chapter`를 갖고 있으므로 `source_type`을 먼저 본다

### 소스 레지스트리

`src/chat.py`의 `COLLECTION_REGISTRY`가 **소스를 추가할 때 고치는 유일한 곳**이다.
`GET /api/collections`가 이 값을 내려주고 프론트엔드가 칩·필터·안내문을 렌더한다.
`COLLECTIONS`(키→컬렉션명)는 레지스트리에서 파생된다.

| 키 | 컬렉션 | 라벨 | 권위 | 필터 |
|---|---|---|---|---|
| `rules` | `ksae-formula-rules` | 규정 | 공식 | — |
| `qna` | `ksae-qna` | Q&A | 공식 해석 | `category` |
| `kb` | `ksae-aark-kb` | AARK | 경험담 | `confidence` |

지식베이스는 `url`이 없다(익명 채팅이라 링크할 원문이 없음). 대신 `dates`·`confidence`를
검색 결과에 실어 보내 UI가 발언일과 신뢰도 배지로 표시한다.
키워드 인덱스는 `chapter`/`confidence`/`kind`.

### Sparse 벡터 (`*-v2` 컬렉션) — 만들어 뒀으나 전환하지 않음

Qdrant는 기존 컬렉션에 sparse 벡터를 추가할 수 없다(`update_collection` → 400
"Not existing vector name"). 그래서 `src/sparse_migrate.py`가 dense를 **그대로 복사**하고
sparse만 얹은 `ksae-*-v2` 3개를 별도로 만들어 뒀다(dense는 FlagEmbedding과
SentenceTransformer가 비트 단위로 동일하므로 A/B에서 sparse 효과만 분리된다).
전환은 `COLLECTION_REGISTRY`의 컬렉션명 한 줄, 롤백은 그 반대.

측정 결과(`bench/`)는 **전환을 정당화하지 않는다**:

| 과제 | dense | sparse | RRF | DBSF |
|---|---|---|---|---|
| 어휘(부품번호 단독 질의) MRR | 0.363 | **0.636** | 0.565 | 0.554 |
| 실사용 질의 top-7 v1 대비 중복 | — | 40% | 69% | 66% |

부품번호만 던지는 합성 질의에서는 sparse가 압도적이지만, **실사용 대화형 질의에서는
어휘 우연 일치로 오답을 낸다.** 예: "전선 자체는 UL94 v-0 만족 아니여도 되냐"에
v1은 「전선 표기 요구 범위」를, sparse-only는 「3D프린터 난연 필라멘트」를 반환했다.
RRF 융합은 v1과 품질이 사실상 같다. 지연은 공개 HTTPS 경유라 실행마다 편차가 커서
유의미한 차이를 말할 수 없었다.

부품번호 검색을 정말 개선하려면 전면 전환이 아니라 **질의 유형 라우팅**
(코드 토큰 단독 질의일 때만 어휘 arm 가중)이 맞는 방향이다.

**신뢰도 필터 주의** — 표·단편 청크는 `confidence`가 빈 문자열이다. 필터를 `must`로 걸면
표가 통째로 사라지므로 `should`(OR)에 `MatchValue("")`를 함께 넣는다.

검색 시 컬렉션별 `limit`개 조회 → score 순 병합 → `min_per_collection` 보장 →
`MAX_CHUNKS_PER_POST`(post_id) + `MAX_CHUNKS_PER_SECTION`(section) 중복 제거.
`server.py`의 `min_score=0.5`로 저품질 필터링.

기본 `limit`은 7이다. `min_per_collection=1` × 3컬렉션이라 5로 두면 점수순 자유 슬롯이 2개뿐이다.
section 상한이 따로 있는 이유는 지식베이스에서 항목 하나가 곧 post_id라 post 기준 중복 제거가
동작하지 않기 때문이다(한 소주제가 결과를 독식했다).

### Auth & Credits

**UI 용어는 "이용권"이다.** 코드 식별자(`credits`, `deduct_credit`)는 그대로 두고
사용자에게 보이는 문자열만 이용권으로 쓴다.

- Google OAuth 2.0 → JWT 쿠키 → `get_current_user(request)`
- 이용권 차감: `deduct_credit(user_id, amount, memo)` — 모델별 가변 비용, `WHERE credits >= ?`로 원자적 차감
- LLM 에러 시 `refund_credit()`으로 환불
- `unlimited_credits` 모드: site_settings에서 토글, `deduct_credit`/`refund_credit`이 스킵
- 세션 삭제는 soft delete (`deleted_at` 컬럼) — 사용자에게는 숨기고 관리자는 열람 가능

### Database

SQLite (`data/users.db`), WAL 모드. 테이블:
- `users`, `sessions`, `messages`, `token_transactions`, `model_settings`, `site_settings`
- 스키마 마이그레이션은 `init_db()`에서 `ALTER TABLE ... ADD COLUMN`을 try/except로 처리

### Initialization

`server.py` lifespan에서 순서대로: `init_db()` → `init_oauth()` → `init_admin_emails()` → `init_site_settings()` → `init_resources()` (BGE-M3 로드, Qdrant/Gemini/Anthropic 클라이언트) → `init_model_settings()`

### Data Pipeline (main.py)

Click CLI. `crawl_list_pages()` → `filter_new_posts()` → `crawl_all_details()` (ThreadPoolExecutor 병렬) → `merge_posts()` → `chunk_posts()` → `embed_chunks()` → `upload_to_qdrant()`.
Incremental 모드는 기존 posts.json과 비교하여 신규만 처리.

지식베이스는 크롤링 대상이 아니라 **큐레이션된 마크다운 문서 1개**라 별도 경로를 쓴다:
`chunk_kb()` → `embed_chunks()` → `upload_to_qdrant()` (`kb-*` 커맨드). embed/upload 스테이지는 공용이며,
`upload_to_qdrant(payload_fields=..., index_fields=...)` 로 소스별 payload 스키마를 넘긴다.

`src/kb_chunker.py`는 문서 구조를 청크 단위로 삼는다:
- `- **주제** [신뢰도]` + 중첩 자식 → `topic` 청크 (자식 유무가 항목/단편 구분자)
- `#### 캡션` 또는 리스트 안 캡션 불릿 + 표 → `table` 청크. 분할 시 헤더 행을 매 청크에 반복
- 자식 없는 최상위 불릿 연속 → `brief` 청크, 그 외 산문·인용 → `note` 청크
- 모든 청크에 `[분야] 장 > 절` / `[주제] ...` 문맥 머리말을 붙여 단독으로 읽히게 한다
- 분할은 **줄 경계 우선**. 줄 내부를 자르면 `결론:` 같은 라벨이 값과 분리된다

### Frontend

바닐라 JS (`static/`). SSE via `fetch` + `ReadableStream`. marked.js로 마크다운 렌더링. CSS 변수 기반 라이트/다크 테마.
관리자 페이지 (`/admin`): 사용자 이용권 관리, 대화 기록 열람, 모델별 API 토큰 사용량/비용 추산.

컨트롤 바는 두 줄(모델 / 검색 소스)로 나뉜다. 소스 칩과 필터는 `/api/collections` 응답으로
렌더되므로 소스를 추가해도 프론트엔드를 고칠 필요가 없다.
검색 결과 출처는 신뢰도 배지(`.conf-badge`)와 발언일을 함께 표시한다 — 지식베이스는 URL이 없어
발언일이 유일한 대조 단서다.

## Coding Conventions

- Python 타입 힌트 사용 (`list[dict]`, `str | None`)
- 글로벌 리소스는 모듈 레벨 변수 선언 + `init_*()` 함수에서 한 번만 로드
- 날짜: DB는 UTC, 프론트엔드에서 UTC→로컬 변환 (`YYYY-MM-DD HH:mm:ss`)
- 프론트엔드 카테고리 필터: Formula/Baja/EV (Qdrant `FieldCondition` 사용)
- API 에러 메시지는 한국어
- 관리자 권한은 `ADMIN_EMAILS` 환경변수 (쉼표 구분)로 제어

## Caveats

- `data/`, `.env`는 .gitignore
- **`ksae-formula-rules` 적재 코드가 레포에 없다.** 규정집 161점은 재현 불가 상태이므로
  갱신·복구가 필요하면 `src/kb_chunker.py`와 같은 규격으로 파이프라인을 먼저 복원할 것
- `requirements.txt`에 버전이 고정돼 있지 않다. `src/chunker.py`가 `transformers`를 직접
  import하는데 선언은 sentence-transformers 경유 전이 의존이다
- Python은 3.12~3.13을 쓸 것. 3.14는 torch 휠이 없다
- 문서 소스 재적재는 `prune=True`로 돌린다. upsert 키가 (post_id, chunk_index)라
  원본에서 사라진 항목이 컬렉션에 남는다
- BGE-M3 첫 로딩 시 ~2GB 다운로드
- 크롤러는 KSAE 서버의 약한 DH 키 대응을 위해 `_WeakDHAdapter` (커스텀 SSL `@SECLEVEL=1`) 사용
- 크롤러 `requests.Session`은 thread-safe하지 않으므로 thread-local 사용
- 환경 변수: `.env.example` 참조. `GOOGLE_API_KEY` 필수, `ANTHROPIC_API_KEY`는 선택 (없으면 Claude 모델 비활성화)
