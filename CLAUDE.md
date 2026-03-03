# PitBot

KSAE(한국자동차공학회) 대학생 자작자동차대회 Q&A 게시판을 크롤링하여 RAG 파이프라인으로 벡터 DB에 저장하고, 웹 챗봇으로 질의응답하는 시스템.

## 프로젝트 구조

```
ksae-qna/
├── main.py                # CLI 파이프라인 (crawl → chunk → embed → upload)
├── mcp_server.py          # MCP 프로토콜 기반 시맨틱 검색 서버 (stdin/stdout JSON-RPC)
├── server.py              # FastAPI 웹 챗봇 서버 (SSE 스트리밍)
├── Dockerfile             # Docker 컨테이너 빌드
├── docker-compose.yml     # Docker Compose (Traefik 연동)
├── requirements.txt
├── .env                   # 환경 변수 (API 키 등)
├── .env.example           # 환경 변수 템플릿
├── src/
│   ├── auth.py            # Google OAuth, JWT, 사용자 DB, 토큰 시스템 + 거래 내역 + 관리자
│   ├── crawler.py         # KSAE Q&A 게시판 크롤링 (목록 + 상세, ThreadPoolExecutor 병렬)
│   ├── chunker.py         # 텍스트 청킹 (512 토큰, 50 오버랩)
│   ├── embedder.py        # BGE-M3 임베딩 (1024차원, 로컬/원격)
│   ├── uploader.py        # Qdrant 벡터 DB 업로드
│   └── chat.py            # RAG 멀티컬렉션 검색 + 멀티모델 LLM 스트리밍 (Gemini/Anthropic) + 카테고리 필터
├── static/
│   ├── index.html         # 채팅 UI (라이트/다크 테마, 모바일 사이드바)
│   ├── style.css          # CSS 변수 기반 테마 시스템
│   ├── script.js          # SSE 수신 + 마크다운 렌더링 + 테마 토글
│   ├── admin.html         # 관리자 페이지
│   ├── admin.css          # 관리자 페이지 스타일
│   ├── admin.js           # 관리자 페이지 로직
│   └── logo.svg           # 로고 (헤더 + favicon)
└── data/
    ├── raw/               # 크롤링 원본 (posts.json, post_list.json)
    └── processed/         # 처리 결과 (chunks.json, embeddings.npy)
```

## 핵심 기술 스택

- **임베딩**: BAAI/bge-m3 (sentence-transformers), 1024차원 dense vector
- **벡터 DB**: Qdrant (원격, HTTPS, JWT 인증), 코사인 유사도
- **LLM**: 멀티모델 — Gemini 3 Flash/Pro (google-genai SDK), Claude Sonnet/Opus 4.6 (anthropic SDK), SSE 스트리밍
- **웹 서버**: FastAPI + uvicorn, SSE 스트리밍
- **인증**: Google OAuth 2.0 + JWT 세션
- **DB**: SQLite (users, sessions, messages, token_transactions)
- **프론트엔드**: 바닐라 JS, fetch + ReadableStream, marked.js, CSS 변수 테마

## 실행 방법

```bash
# 데이터 파이프라인 (크롤링 → 벡터 DB 업로드)
python main.py                        # 전체 파이프라인 (incremental)
python main.py --mode full            # 전체 파이프라인 (full, 처음부터 재크롤링)
python main.py --workers 10           # 상세 페이지 병렬 크롤링 동시 요청 수 (기본: 5)
python main.py crawl                  # 크롤링만
python main.py chunk                  # 청킹만
python main.py embed                  # 임베딩만
python main.py upload                 # 업로드만

# 웹 챗봇 서버
python server.py                      # http://localhost:8000

# Docker
docker build -t pitbot .
docker run -p 8000:8000 --env-file .env pitbot

# Docker Compose (Traefik 리버스 프록시)
docker compose up -d

# MCP 서버 (Claude 등 AI 클라이언트용)
python mcp_server.py                  # stdin/stdout JSON-RPC
```

## 환경 변수 (.env)

`.env.example`을 복사하여 `.env` 파일 생성 후 값 입력.

- `QDRANT_URL` — Qdrant 서버 URL (기본: https://vectordb.luftaquila.io:443)
- `QDRANT_API_KEY` — Qdrant JWT 인증 토큰
- `GOOGLE_API_KEY` — Gemini API 키 (챗봇 서버 필수)
- `GOOGLE_CLIENT_ID` — Google OAuth 클라이언트 ID
- `GOOGLE_CLIENT_SECRET` — Google OAuth 클라이언트 시크릿
- `JWT_SECRET` — JWT 서명 시크릿 (선택, 기본값: "dev")
- `ADMIN_EMAILS` — 관리자 이메일 (쉼표 구분, `/admin` 접근 권한)
- `ANTHROPIC_API_KEY` — Anthropic API 키 (선택, Claude 모델 사용 시 필수)
- `HTTPS_ONLY` — HTTPS 리버스 프록시 뒤에서 실행 시 `true` (세션/인증 쿠키 secure 설정)

## 코딩 컨벤션

- Python 타입 힌트 사용 (`list[dict]`, `str | None` 등)
- 글로벌 리소스는 모듈 레벨 변수로 선언, 초기화 함수에서 한 번만 로드
- 검색 로직은 `mcp_server.py`와 `src/chat.py`에서 동일 패턴 사용 (encode → query_points → payload 추출)
- Qdrant 컬렉션 (`src/chat.py`의 `COLLECTIONS` 딕셔너리로 관리):
  - `ksae-qna` (키: `qna`) — Q&A 게시판. Payload: `id`, `category`, `title`, `author`, `date`, `url`, `content`, `chunk_index`
  - `ksae-formula-rules` (키: `rules`) — 규정집. Payload: `content`, `chapter`, `chapter_num`, `section`, `section_num`
- 프론트엔드에서 컬렉션 선택 칩으로 검색 대상을 선택 가능 (Q&A / 규정, 복수 선택)
- 카테고리 드롭다운으로 Q&A 검색 시 Formula/Baja/EV 필터링 (Qdrant `FieldCondition` 사용)
- 검색 시 선택된 컬렉션 각각에서 `limit`개씩 조회 후 score 순 병합, 상위 `limit`개 반환
- `server.py`의 `min_score=0.5` 로 저품질 결과 필터링
- 멀티모델 지원: `src/chat.py`의 `MODEL_CONFIG` 딕셔너리로 모델 레지스트리 관리
  - `gemini-3-flash` (1 토큰), `gemini-3-pro` (4 토큰), `claude-sonnet-4.6` (5 토큰), `claude-opus-4.6` (10 토큰)
  - provider별 스트리밍 함수 분리: `_stream_gemini()`, `_stream_anthropic()`
  - `GET /api/models` 엔드포인트로 사용 가능한 모델 목록 제공
- 크레딧 차감은 모델별 가변 (`deduct_credit(user_id, amount, memo)`)
- messages 테이블에 `model` 컬럼으로 사용된 모델 기록
- 관리자 페이지 (`/admin`): 사용자 크레딧 관리, 대화 기록 열람, API 토큰 사용량(IN/OUT/THK) 및 모델별 비용 추산
- 세션 삭제는 soft delete (`deleted_at` 컬럼) — 사용자에게는 숨기고 관리자는 열람 가능
- API 토큰 비용 추산: 모델별 차등 가격 (`admin.js`의 `MODEL_PRICING` 참조, messages에 model 미기록 시 Gemini Flash 가격으로 폴백)
- 날짜 표시는 UTC→로컬 변환, `YYYY-MM-DD HH:mm:ss` 형식

## 주의사항

- `data/` 디렉터리는 .gitignore에 포함 (크롤링 데이터 대용량)
- `.env` 파일은 .gitignore에 포함 (API 키 보안)
- BGE-M3 모델 첫 로딩 시 다운로드 필요 (~2GB)
- 크롤러는 KSAE 서버의 약한 DH 키 대응을 위해 커스텀 SSL 설정 사용
- 크롤러 상세 페이지는 `ThreadPoolExecutor`로 병렬 처리 (`--workers`, 기본 5). thread-local 세션 사용
