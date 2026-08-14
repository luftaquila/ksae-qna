# PitBot

KSAE(한국자동차공학회) 대학생 자작자동차대회 Q&A 게시판을 크롤링하여 벡터 DB에 저장하고, 이를 기반으로 질문에 답변하는 RAG 챗봇입니다.

## 주요 기능

- **Q&A 게시판 크롤링** — KSAE 자작자동차대회 Q&A 게시판의 질문/답변을 자동 수집
- **벡터 검색 기반 답변** — BGE-M3 임베딩 + Qdrant 벡터 DB로 질문과 관련된 기존 Q&A, 규정집, AARK 지식베이스 검색
- **고정 모델 라우팅** — Gemini Flash의 공급자 관리 `latest` 별칭을 기본으로 사용하고 실패하면 Gemini Pro의 `latest` 별칭으로 자동 전환
- **실시간 스트리밍** — SSE 기반 토큰 단위 스트리밍 응답
- **Google OAuth 인증** — 사용자 인증 및 질문당 이용권 1장 기반 사용량 관리
- **마이페이지 통계** — 누적 대화·질문·이용권 사용/환불 및 입력·출력·추론 토큰 확인
- **관리자 페이지** — 기간별 사용자 활동·답변 안정성·응답 속도·비용 통계, 사용자 관리, 월별 이용권 자동·즉시 충전 및 대화 기록 확인
- **MCP 서버** — AI 클라이언트(Claude Desktop 등)에서 직접 벡터 검색 가능

## 요구사항

- Python 3.12+
- [Qdrant](https://qdrant.tech/) 벡터 DB 인스턴스
- Google Cloud 프로젝트 (OAuth 클라이언트 + Gemini API 키)
- (선택) Anthropic API 키 — Claude 모델 사용 시

## 설치 및 실행

### Docker (권장)

```sh
cp .env.example .env
# .env 파일에 API 키 및 OAuth 정보 입력

docker compose up -d
```

Traefik 리버스 프록시 네트워크(`traefik`)가 사전에 구성되어 있어야 합니다. 독립 실행 시 `docker-compose.yml`에서 네트워크 설정을 수정하세요.

### 로컬 실행

```sh
pip install -r requirements.txt
cp .env.example .env
# .env 파일에 API 키 및 OAuth 정보 입력

python server.py
```

`http://localhost:8000`에서 접속 가능합니다.

> BGE-M3 임베딩 모델 첫 로딩 시 약 2GB 다운로드가 발생합니다.

관리자 설정의 `매월 기본 충전 이용권`은 기본 20입니다. 매월 1일(KST)에 잔액이
설정값보다 적은 사용자만 설정값까지 충전하며, `지금 충전`으로 같은 작업을 즉시 실행할 수 있습니다.
월 자동 충전은 월별 실행 이력으로 중복 지급을 방지합니다.

## 환경 변수

| 변수 | 필수 | 설명 |
|------|:----:|------|
| `QDRANT_URL` | O | Qdrant 서버 URL |
| `QDRANT_API_KEY` | O | Qdrant API 키 |
| `GOOGLE_API_KEY` | O | Google Gemini API 키 |
| `GOOGLE_CLIENT_ID` | O | Google OAuth 클라이언트 ID |
| `GOOGLE_CLIENT_SECRET` | O | Google OAuth 클라이언트 시크릿 |
| `ANTHROPIC_API_KEY` | | Anthropic API 키 (없으면 Claude 모델 비활성화) |
| `JWT_SECRET` | | JWT 서명 키 (미설정 시 자동 생성) |
| `ADMIN_EMAILS` | | 관리자 이메일 (쉼표 구분) |
| `HTTPS_ONLY` | | `true` 설정 시 Secure 쿠키 활성화 |
| `APP_VERSION` | | `/api/health`에 표시할 빌드 식별자 |

`/live`는 프로세스 생존 여부, `/ready`는 SQLite·Qdrant·모델 가용성을 확인합니다. `/api/health`에서는 모델이 실제로 해결된 버전과 프롬프트 버전도 확인할 수 있습니다.

## 데이터 파이프라인

Q&A 게시판 크롤링부터 벡터 DB 업로드까지의 파이프라인입니다.

```
크롤링 → 청킹 → 임베딩 → Qdrant 업로드
```

```sh
# 전체 파이프라인 (incremental — 신규 게시글만 처리)
python main.py

# 전체 재크롤링
python main.py --mode full

# 병렬 크롤링 워커 수 지정 (기본 5)
python main.py --workers 10

# 개별 스테이지 실행
python main.py crawl
python main.py chunk
python main.py embed
python main.py upload
```

크롤링 데이터는 `data/` 디렉토리에 저장됩니다.

### AARK 지식베이스

참가팀 익명 단톡방 18개월치 로그에서 추린 지식베이스(마크다운 문서 1개)를 별도 컬렉션으로 올립니다.
크롤링 단계가 없고 전용 청커(`src/kb_chunker.py`)를 쓰는 것 외에는 동일한 임베딩/업로드 스테이지를 공유합니다.

```sh
# 소스 문서를 data/raw/ 에 두고 전체 실행
python main.py kb --source data/raw/aark-kb.md --recreate

# 개별 스테이지
python main.py kb-chunk --source data/raw/aark-kb.md
python main.py kb-embed
python main.py kb-upload --recreate
```

문서 구조(장 > 절 > 항목)를 그대로 청크 단위로 삼고, 각 청크에 `[분야]`/`[주제]` 문맥 머리말과
신뢰도(합의됨/다수의견/단일제보/미해결)를 함께 저장합니다. 익명 채팅 출처라 `url`은 없습니다.

챗봇은 문서 종류의 고정 우선순위 대신 질문에 직접 답하는 정도로 문서를 재정렬합니다.
AARK는 신뢰도 필터 없이 전체 내용을 검색하며, 신뢰도는 합의 수준 표시에만 사용합니다.
Q&A는 실제 `[답변]`이 있는 청크만 생성 근거에 포함합니다.

### Formula 2026 규정(2026)

`rules` 명령은 PDF `Formula 차량기술규정(2026)`를 청킹 → 임베딩 → 업로드까지 한 번에 처리합니다.
기존 Q&A/KB와 같은 임베딩/업로드 스테이지를 재사용하지만, 청크 패스/페이로드만 다릅니다.

```sh
python main.py rules --source data/raw/formula-2026-2026.pdf --rules-collection ksae-formula-rules-reembed --recreate
python main.py rules-embed
python main.py rules-upload --rules-collection ksae-formula-rules-reembed --recreate
```

```sh
# 2026년 J_rule 전체 규정(차량기술/경기진행/심사/안전 등) 재수집 + 분류 + 적재
python main.py rules-2026 --year 2026 --recreate
```

개별 단계 실행:

```sh
python main.py rules-2026-crawl --year 2026   # J_rule 크롤링 + PDF 다운로드
python main.py rules-2026-chunk --year 2026   # 2026 규정 청크 생성
python main.py rules-2026-embed --year 2026   # 분류 컬렉션별 임베딩
python main.py rules-2026-upload --year 2026 --recreate  # 분류 컬렉션별 업로드
```

규정 임베딩 생성·비교 방법은 `docs/rules-embedding-workflow.md` 를 참고하세요.

## MCP 서버

Claude Desktop 등 MCP를 지원하는 AI 클라이언트에서 벡터 DB를 직접 검색할 수 있습니다.

```sh
python mcp_server.py
```

stdin/stdout JSON-RPC 프로토콜로 통신합니다. Claude Desktop 설정 예시:

```json
{
  "mcpServers": {
    "ksae-qna": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/ksae-qna"
    }
  }
}
```

## 프로젝트 구조

```
├── server.py           # FastAPI 웹 서버
├── main.py             # 데이터 파이프라인 CLI
├── mcp_server.py       # MCP 서버
├── src/
│   ├── auth.py         # 인증, DB, 크레딧 관리
│   ├── chat.py         # 벡터 검색 + LLM 스트리밍
│   ├── crawler.py      # KSAE 게시판 크롤러
│   ├── chunker.py      # 텍스트 청킹
│   ├── embedder.py     # BGE-M3 임베딩
│   └── uploader.py     # Qdrant 업로드
├── static/             # 프론트엔드 (바닐라 JS)
├── data/               # 런타임 데이터 (.gitignore)
├── Dockerfile
└── docker-compose.yml
```
