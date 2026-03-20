# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PitBot — KSAE(한국자동차공학회) 대학생 자작자동차대회 Q&A 게시판 크롤링 → RAG 벡터 DB → 웹 챗봇 시스템.

## Commands

```bash
# 웹 챗봇 서버 (개발)
python server.py                      # http://localhost:8000

# 데이터 파이프라인
python main.py                        # 전체 파이프라인 (incremental)
python main.py --mode full            # full 재크롤링
python main.py --workers 10           # 병렬 크롤링 워커 수 (기본 5)
python main.py crawl|chunk|embed|upload  # 개별 스테이지

# Docker
docker compose up -d                  # Traefik 리버스 프록시

# MCP 서버 (AI 클라이언트용, stdin/stdout JSON-RPC)
python mcp_server.py
```

## Architecture

### Request Flow (Chat)

1. `server.py` POST `/api/chat` — 인증 확인, 모델 검증, 크레딧 차감, 세션 생성/조회
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
- payload 구조로 컬렉션 타입 구분: `title` 있으면 Q&A, `chapter` 있으면 규정집

Qdrant 컬렉션 (`COLLECTIONS` 딕셔너리):
- `ksae-qna` (키: `qna`) — Q&A 게시판. Payload: `id`, `category`, `title`, `author`, `date`, `url`, `content`, `chunk_index`
- `ksae-formula-rules` (키: `rules`) — 규정집. Payload: `content`, `chapter`, `chapter_num`, `section`, `section_num`

검색 시 컬렉션별 `limit`개 조회 → score 순 병합 → `min_per_collection` 보장 → post_id 중복 제거.
`server.py`의 `min_score=0.5`로 저품질 필터링.

### Auth & Credits

- Google OAuth 2.0 → JWT 쿠키 → `get_current_user(request)`
- 크레딧 차감: `deduct_credit(user_id, amount, memo)` — 모델별 가변 비용, `WHERE credits >= ?`로 원자적 차감
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

### Frontend

바닐라 JS (`static/`). SSE via `fetch` + `ReadableStream`. marked.js로 마크다운 렌더링. CSS 변수 기반 라이트/다크 테마.
관리자 페이지 (`/admin`): 사용자 크레딧 관리, 대화 기록 열람, 모델별 API 토큰 사용량/비용 추산.

## Coding Conventions

- Python 타입 힌트 사용 (`list[dict]`, `str | None`)
- 글로벌 리소스는 모듈 레벨 변수 선언 + `init_*()` 함수에서 한 번만 로드
- 날짜: DB는 UTC, 프론트엔드에서 UTC→로컬 변환 (`YYYY-MM-DD HH:mm:ss`)
- 프론트엔드 카테고리 필터: Formula/Baja/EV (Qdrant `FieldCondition` 사용)
- API 에러 메시지는 한국어
- 관리자 권한은 `ADMIN_EMAILS` 환경변수 (쉼표 구분)로 제어

## Caveats

- `data/`, `.env`는 .gitignore
- BGE-M3 첫 로딩 시 ~2GB 다운로드
- 크롤러는 KSAE 서버의 약한 DH 키 대응을 위해 `_WeakDHAdapter` (커스텀 SSL `@SECLEVEL=1`) 사용
- 크롤러 `requests.Session`은 thread-safe하지 않으므로 thread-local 사용
- 환경 변수: `.env.example` 참조. `GOOGLE_API_KEY` 필수, `ANTHROPIC_API_KEY`는 선택 (없으면 Claude 모델 비활성화)
