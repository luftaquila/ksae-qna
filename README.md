# PitBot

KSAE(한국자동차공학회) 대학생 자작자동차대회 Q&A 게시판을 크롤링하여 벡터 DB에 저장하고, 이를 기반으로 질문에 답변하는 RAG 챗봇입니다.

## 주요 기능

- **Q&A 게시판 크롤링** — KSAE 자작자동차대회 Q&A 게시판의 질문/답변을 자동 수집
- **벡터 검색 기반 답변** — BGE-M3 임베딩 + Qdrant 벡터 DB로 질문과 관련된 기존 Q&A 및 규정집 검색
- **멀티 모델 지원** — Gemini, Claude 등 복수의 LLM 중 선택하여 답변 생성
- **실시간 스트리밍** — SSE 기반 토큰 단위 스트리밍 응답
- **Google OAuth 인증** — 사용자 인증 및 크레딧 기반 사용량 관리
- **관리자 페이지** — 사용자 관리, 대화 기록 열람, 모델별 토큰 사용량/비용 확인
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

