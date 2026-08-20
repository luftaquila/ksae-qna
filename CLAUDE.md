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

1. `server.py` POST `/api/chat` — 인증 확인, 이용권 1장 차감, 세션 생성/조회
2. `src/chat.py` `search_and_stream()` — 벡터 검색 후 provider별 LLM 스트리밍
3. SSE 이벤트: `sources` → `token`(반복) → `usage` → `done`
4. `server.py`에서 asyncio.Queue로 LLM 소비/클라이언트 전달 분리 — 클라이언트 disconnect 시에도 LLM 태스크는 백그라운드에서 완료되어 응답 저장

### Model Routing

- 사용자 모델 선택은 없다. 서버가 **같은 Flash 계열을 한 세대씩 내려가며** 시도한다:
  `gemini-3.7-flash` → `gemini-3.6-flash` → `gemini-3.5-flash` (`src/chat.py`의 `MODEL_CHAIN`)
- **Pro 로 폴백하던 예전 경로가 32% 오류 환불의 원인이었다.** 이 키에서 `gemini-pro-latest`는
  `gemini-3.1-pro`로 해석되고 무료 티어 한도가 **0**이라(`429 RESOURCE_EXHAUSTED, limit: 0`)
  한 번도 성공할 수 없었다. Flash 의 일시적인 분당 한도(`Please retry in 47s`)가 확정 실패로
  바뀌어 이용권 환불만 쌓였다. 라우팅을 바꿀 때는 후보에 **쿼터가 실제로 있는지** 확인할 것 —
  `is_model_available()`은 클라이언트 존재만 보고 쿼터는 보지 않는다
- **`-latest` 별칭을 쓰지 않는다.** Google 이 별칭을 옮기면 예고 없이 다른 세대로 갈아탄다.
  버전을 박고 올릴 때 의도적으로 올린다
- 토큰이 한 번이라도 나간 뒤의 실패는 다음 세대로 내려가지 않는다(되돌릴 수 없다).
  내려갈 때 버려지는 앞 세대 오류는 `logger.warning`으로 남긴다 — 예전에는 이게 사라져서
  턴 기록에 "모델 flash / 오류 pro" 같은 모순이 남았다
- `PRIMARY_MODEL_KEY`(= `MODEL_CHAIN[0]`), `ROUTING_MODEL_KEYS`, `CHAT_CREDIT_COST`가 라우팅과
  고정 이용권 비용을 정의한다
- `MODEL_CONFIG` 딕셔너리는 모델 ID, provider, pricing, thinking level 메타데이터를 보관한다
- provider별 스트리밍 분리: `_stream_gemini()` (동기 이터레이터를 `run_in_executor`로 래핑), `_stream_anthropic()` (네이티브 async)
- **모르는 모델을 primary 로 채우지 않는다.** 예전에는 `resolved_model or PRIMARY_MODEL_KEY`
  라서 오류 턴 전부가 primary 사용 실적으로 집계됐다(오류 21건이 실제로는 Pro 의 429 인데
  flash 로 기록돼 있었다). 답한 모델이 없으면 `messages.model`·`chat_turns.resolved_model`을
  **NULL 로 남긴다**
- 체인을 내려가면 앞 후보의 모델 정보를 비운다 — 그 후보는 이 턴의 답이 아니다. 어디까지
  내려갔는지는 `chat_turns.attempted_models`(JSON 배열)에 순서대로 남는다. `resolved_model`
  하나로는 "3.7 이 실패해서 3.6 이 답했다"와 "처음부터 3.6 이었다"를 구분할 수 없다
- 체인 각 세대의 활성화 여부는 `model_settings` DB 테이블 + `_model_enabled` 인메모리 캐시에
  저장한다. `MODEL_ROUTING_VERSION`을 올리면 다음 기동에서 전 세대를 다시 켜고 마커를 갱신한다
- 클라이언트의 `/api/chat` 요청에는 모델 필드가 없고, 모델별 이용권 오버라이드나 표시 순서도 제공하지 않는다

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
| `kb` | `ksae-aark-kb` | AARK | 경험담 | — |

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

**AARK 신뢰도** — 검색 단계에서는 신뢰도로 거르지 않고 모든 청크를 대상으로 한다.
`confidence`는 검색 결과의 합의 수준 배지와 답변 표현을 위한 메타데이터로만 사용한다.

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
- 이용권 차감: 질문당 항상 1장, `deduct_credit(user_id, amount, memo)`의 `WHERE credits >= ?`로 원자적 차감
- LLM 에러 시 `refund_credit()`으로 환불
- `unlimited_credits` 모드: site_settings에서 토글, `deduct_credit`/`refund_credit`이 스킵
- **유료 구매분과 무료 충전분은 분리된다.** `users.credits`는 여전히 **총 잔액**이고,
  그중 구매분이 얼마인지만 `users.paid_credits`에 따로 적는다(불변식 `0 <= paid_credits <= credits`,
  무료분 = `credits - paid_credits`). 잔액을 읽는 코드를 전부 그대로 두고 충전 기준만
  바꾸기 위한 선택이다
- `monthly_refill_credits`: 매월 1일(KST) **무료분**이 설정값 미만인 사용자만 설정값까지 충전.
  구매분은 비교와 결과에서 모두 제외된다 — 이용권을 사서 총 잔액이 바닥값을 넘었다는 이유로
  다음 달 무료 충전이 사라지면 돈 낸 사람이 손해를 본다
- 차감은 **무료분 먼저**다. 무료분은 매월 돌아오고 구매분은 만료가 없으니, 이 순서라야 구매가
  녹아 없어지지 않는다. 환불(`refund_credit`)은 무료분으로 들어간다 — 어느 쪽에서 나갔는지
  거래별로 추적하지 않고, 우리 잘못에 대한 보상은 구매분으로 세지 않는 편이 맞다
- 월 자동 충전은 `monthly_credit_refills`의 월별 PK로 중복 실행을 막고, 관리자는 같은 floor 충전을 즉시 실행 가능
- 세션 삭제는 soft delete (`deleted_at` 컬럼) — 사용자에게는 숨기고 관리자는 열람 가능
- **회원 탈퇴도 soft delete다.** `users.deleted_at`을 찍고 이용권을 0으로 내리며, 대화·메시지·
  chat_turns·이용 내역은 실제로 삭제한다. 행을 남기는 이유는 같은 구글 계정으로 다시 들어올 때
  처음 온 사람과 구분하기 위해서다 — 하드 삭제하면 탈퇴·재가입을 반복해 기본 지급 이용권을
  계속 새로 받을 수 있다. `get_or_create_user()`가 되살릴 때 기본 지급을 하지 않고,
  `get_user_by_id()`는 `deleted_at IS NULL`로 걸러 남은 JWT를 무효화한다
- 탈퇴 계정은 월 충전(`_refill_users_to_floor`)과 일괄 조정(`admin_bulk_set_credits`)에서도
  빠진다. 채워두면 되살아나는 순간 그 이용권을 그대로 받게 되고, 월 충전은 매달 저절로 돌기
  때문에 이걸 놓치면 구멍이 자동으로 다시 열린다

### Payments (NicePay 결제창 서버승인)

`src/payments.py`가 결제의 전부다. UI 용어는 "이용권 구매"이고 상품은 **단가 x 수량** 하나뿐이다.

- 흐름: `POST /api/payments/orders`(서버가 금액 계산, pending 주문 생성) → `AUTHNICE.requestPay()`
  → `POST /api/payments/return`(브라우저 POST) → 승인 API → 지급 → `/payments/result`로 303
- **returnUrl 핸들러는 로그인 세션을 읽지 않는다.** 나이스페이 도메인에서 넘어오는 top-level
  cross-site POST라 `SameSite=Lax`인 `token` 쿠키가 실려 오지 않는다. 소유자는 주문 행의
  `user_id`로만 판단한다
- 지급·회수는 `WHERE status = ?` 조건부 UPDATE의 rowcount로 한 번만 통과시키고 잔액 변경을
  같은 트랜잭션에 넣는다. returnUrl과 웹훅이 겹쳐도 이중 지급이 없다
- 서명: returnUrl은 `sha256(authToken + clientId + amount + secretKey)`,
  승인응답·웹훅은 `sha256(tid + amount + ediDate + secretKey)`. 둘 다 `hmac.compare_digest`로 비교
- **결제 API는 Bearer 토큰만 받는다.** `/v1/payments/*`에 Basic을 보내면 조회조차 `U103`
  "사용자 인증타입이 맞지 않습니다"로 거절된다. Basic이 통하는 곳은 `/v1/access-token` 하나뿐이고,
  거기서 받은 토큰을 30분(여유 2분) 캐시해서 쓴다. `U103`이 오면 한 번만 재발급해 재시도한다 —
  인증 계층에서 잘린 것이라 요청 흔적이 없어 승인 요청이라도 재시도가 안전하다. 다른 코드는 재시도 금지
- **결제창 SDK는 문서보다 소스가 정확하다.** `AUTHNICE.requestPay()`는 `fnError`를 필수로 요구하는데
  문서 페이지에는 없다. 파라미터를 의심할 일이 생기면 `curl https://pay.nicepay.co.kr/v1/js/`로 직접 읽을 것
- 승인 API가 timeout/네트워크 오류로 끊기면 승인 성립 여부를 알 수 없으므로 **망취소**
  (`/v1/payments/netcancel`, 1시간 이내)를 던지고 주문을 failed로 내린다
- 웹훅은 본문에 `OK`가 없으면 나이스페이가 실패로 보고 재전송한다. 처리 중 예외는 삼키지 않는다
- **카드 최소 승인금액은 1,000원**(오류코드 3041)이다. `min_quantity()`가 단가에서 최소 구매
  수량을 역산하므로, 단가를 낮추면 최소 구매 수량이 자동으로 올라간다
- 지급은 `credits`와 `paid_credits`를 같이 올린다. 취소는 관리자 전액 취소만이고, 회수는
  **남아 있는 구매분 범위에서만** 한다 — 이미 쓴 몫을 무료 충전분에서 빼오면 결제와 무관한
  이용권을 뺏는 셈이다. 실제 회수량은 `payments.reclaimed`에 남는다
- 결제창을 열었다 닫기만 해도 주문은 `pending`으로 남는다. 시간당 유지보수 워커
  (`server.py` `_hourly_maintenance_worker`)가 1시간 지난 `pending`을 `expired`로 내린다 —
  그래야 "`pending`으로 오래 남은 건 = 지급 누락"을 운영 신호로 쓸 수 있다. **만료는 정리용
  라벨이지 승인 게이트가 아니다**: 지급·실패 기록은 `expired`에서도 통과시켜야 하고
  (`_GRANTABLE`), 아니면 만료 직후 완료된 결제가 청구만 되고 이용권이 안 들어간다
- 시각 비교는 SQLite `datetime('now', ...)` 안에서 한다. `created_at`은 `datetime('now')`
  형식("2026-08-19 11:53:00")이고 JS/Python ISO 문자열은 10번째 글자가 `T`라, 문자열 비교로
  섞으면 같은 날짜의 모든 행이 컷오프보다 작게 나온다
- 결제 기록은 탈퇴해도 남는다. `payments.user_id`가 `ON DELETE SET NULL`로 끊겨 익명화되고,
  진행 중인 결제가 있으면 `delete_user_account()`가 `"payment_pending"`으로 탈퇴를 막는다
- 단가·구매 상한·판매자 정보는 `site_settings`에 있고 `/admin` 설정 탭에서 바꾼다. `/policy`가
  그 값을 그대로 렌더한다

### Database

SQLite (`data/users.db`), WAL 모드. 테이블:
- `users`(`credits` 총 잔액 + `paid_credits` 구매분), `sessions`, `messages`, `token_transactions`,
  `model_settings`, `site_settings`, `monthly_credit_refills`, `payments`
- 스키마 마이그레이션은 `init_db()`에서 `ALTER TABLE ... ADD COLUMN`을 try/except로 처리

### Initialization

`server.py` lifespan에서 순서대로: `init_db()` → `init_oauth()` → `init_admin_emails()` → `init_site_settings()` → `init_resources()` (BGE-M3 로드, Qdrant/Gemini/Anthropic 클라이언트) → `init_model_settings()` → 월 충전 worker 시작

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

컨트롤 바에는 검색 소스만 표시한다. 소스 칩과 필터는 `/api/collections` 응답으로
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
- `NICEPAY_SECRET_KEY`는 서버 전용이다. 이 저장소는 public이므로 절대 커밋하지 말 것
- `python-multipart`는 결제 returnUrl 때문에 필요하다. starlette의 `Request.form()`이
  content-type 분기보다 먼저 `parse_options_header`를 assert 해서, urlencoded 본문에도 요구한다
- 무료 충전 라우트 `POST /api/credits/topup`은 유료화하면서 제거했다. 관리자 조정은
  `/api/admin/users/{id}/credits`와 `/api/admin/credits/bulk`가 담당한다
