# PRD: KSAE Q&A 게시판 크롤링 및 벡터 DB 구축

## Introduction

한국자동차공학회(KSAE) Q&A 게시판(https://www.ksae.org/jajak/bbs/?code=J_qna)의 전체 게시글과 답변을 크롤링하여, BGE-M3 모델로 임베딩한 뒤 Qdrant 벡터 DB에 저장하는 파이프라인을 구축한다. 이 데이터는 RAG 기반 챗봇의 지식 베이스로 활용되어, 대회 규정 및 기술 관련 질문에 자동으로 답변할 수 있는 시스템의 기반이 된다.

## Goals

- KSAE Q&A 게시판의 전체 게시글(약 150건 이상, 11페이지)과 답변을 빠짐없이 크롤링
- 질문-답변 쌍을 의미 단위로 적절히 청킹하여 RAG 검색 품질 극대화
- BGE-M3 모델을 사용하여 한국어/영어 혼합 텍스트를 고품질 벡터로 임베딩
- Qdrant에 메타데이터와 함께 업로드하여 필터링 가능한 벡터 검색 지원
- 초기 전체 크롤링 이후 증분 크롤링으로 새 게시글만 추가 가능한 구조

## User Stories

### US-001: 게시판 목록 페이지 크롤링
**Description:** 개발자로서, 게시판의 모든 페이지를 순회하여 전체 게시글의 메타데이터(번호, 구분, 제목, 작성자, 조회수, 등록일)를 수집한다.

**Acceptance Criteria:**
- [ ] 1페이지부터 마지막 페이지(현재 11페이지)까지 자동 순회
- [ ] 페이지당 게시글 목록에서 번호, 구분(카테고리), 제목, 작성자, 조회수, 등록일 추출
- [ ] 각 게시글의 상세 페이지 URL 생성 (`/jajak/bbs/?number={id}&mode=view&code=J_qna`)
- [ ] 마지막 페이지 번호를 자동 감지 (하드코딩 아닌 동적 파싱)
- [ ] 수집된 메타데이터를 중간 결과로 JSON 파일에 저장
- [ ] 서버 부하 방지를 위한 요청 간 딜레이(1~2초) 적용

### US-002: 게시글 상세 페이지 크롤링
**Description:** 개발자로서, 각 게시글의 상세 페이지에 접속하여 질문 본문과 답변 본문을 추출한다.

**Acceptance Criteria:**
- [ ] 게시글 상세 페이지에서 질문 제목, 질문 본문 텍스트 추출
- [ ] 답변이 존재하는 경우 답변 본문 텍스트 추출
- [ ] 댓글이 존재하는 경우 댓글 내용도 추출
- [ ] 첨부파일 URL이 있으면 메타데이터에 기록 (다운로드는 하지 않음)
- [ ] HTML 태그 제거 후 순수 텍스트로 정리 (줄바꿈, 공백 정규화)
- [ ] 크롤링 실패 시 재시도 로직 (최대 3회) 및 실패 로그 기록
- [ ] 서버 부하 방지를 위한 요청 간 딜레이(1~2초) 적용

### US-003: 크롤링 데이터 저장
**Description:** 개발자로서, 크롤링한 전체 데이터를 구조화된 형태로 로컬에 저장하여 이후 파이프라인 단계에서 재사용할 수 있게 한다.

**Acceptance Criteria:**
- [ ] 각 게시글을 아래 스키마의 JSON 객체로 저장:
  ```json
  {
    "id": 12958,
    "category": "Baja",
    "title": "제목",
    "author": "작성자",
    "date": "2024-01-01",
    "views": 100,
    "question_body": "질문 본문",
    "answer_body": "답변 본문",
    "comments": ["댓글1", "댓글2"],
    "attachments": ["url1"],
    "url": "원본 URL"
  }
  ```
- [ ] 전체 데이터를 `data/raw/posts.json`에 저장
- [ ] 크롤링 메타정보(실행 시각, 총 건수, 실패 건수)를 `data/raw/crawl_meta.json`에 저장

### US-004: 텍스트 청킹
**Description:** 개발자로서, 크롤링한 Q&A 데이터를 RAG 검색에 최적화된 청크로 분할한다.

**Acceptance Criteria:**
- [ ] 기본 청킹 전략: 질문+답변을 하나의 문서 단위로 유지
- [ ] 질문+답변 합산 길이가 512토큰 이하이면 하나의 청크로 유지
- [ ] 512토큰 초과 시 문단/문장 단위로 분할하되, 청크 간 50토큰 오버랩 적용
- [ ] 각 청크에 원본 게시글의 메타데이터(id, category, title, date, url) 보존
- [ ] 청크 결과를 `data/processed/chunks.json`에 저장
- [ ] 청킹 통계(총 청크 수, 평균 청크 길이, 최대/최소 길이) 출력

### US-005: BGE-M3 임베딩
**Description:** 개발자로서, 청킹된 텍스트를 BGE-M3 모델로 임베딩하여 벡터를 생성한다.

**Acceptance Criteria:**
- [ ] `BAAI/bge-m3` 모델을 FlagEmbedding 또는 sentence-transformers 라이브러리로 로드
- [ ] Dense embedding 벡터 생성 (1024차원)
- [ ] 배치 처리로 임베딩 (배치 사이즈 32)
- [ ] GPU 사용 가능 시 자동으로 GPU 활용, 없으면 CPU fallback
- [ ] 임베딩 결과를 `data/processed/embeddings.npy`에 저장 (청크 인덱스와 1:1 매핑)
- [ ] 진행률 표시 (tqdm 등)

### US-006: Qdrant 업로드
**Description:** 개발자로서, 임베딩 벡터와 메타데이터를 Qdrant 벡터 DB에 업로드한다.

**Acceptance Criteria:**
- [ ] Qdrant 컬렉션 `ksae_qna` 생성 (존재하면 스킵 또는 recreate 옵션)
- [ ] 벡터 설정: 1024차원, Cosine distance
- [ ] 각 포인트에 payload로 메타데이터 저장: `id`, `category`, `title`, `author`, `date`, `url`, `content`, `chunk_index`
- [ ] 배치 업로드 (배치 사이즈 100)
- [ ] 업로드 완료 후 컬렉션 정보 출력 (총 포인트 수, 벡터 차원)
- [ ] `category` 필드에 keyword 인덱스 생성 (필터링용)

### US-007: 증분 크롤링 지원
**Description:** 개발자로서, 이미 크롤링한 게시글을 건너뛰고 새 게시글만 추가 처리할 수 있게 한다.

**Acceptance Criteria:**
- [ ] 기존 `posts.json`에서 크롤링 완료된 게시글 ID 목록 로드
- [ ] 게시판 목록 크롤링 시 기존 ID와 비교하여 새 게시글만 필터링
- [ ] 새 게시글이 없으면 "No new posts found" 메시지 출력 후 종료
- [ ] 새 게시글만 크롤링 → 청킹 → 임베딩 → Qdrant 업로드 파이프라인 실행
- [ ] 기존 `posts.json`에 새 게시글 데이터 병합 저장
- [ ] CLI 옵션 `--mode full|incremental`로 전체/증분 모드 선택

### US-008: CLI 파이프라인 통합
**Description:** 개발자로서, 전체 파이프라인을 단일 CLI 명령으로 실행할 수 있게 한다.

**Acceptance Criteria:**
- [ ] `python main.py` 실행 시 크롤링 → 청킹 → 임베딩 → 업로드 전체 파이프라인 순차 실행
- [ ] CLI 옵션:
  - `--mode full|incremental` (기본: incremental)
  - `--qdrant-url` (기본: http://localhost:6333)
  - `--collection` (기본: ksae_qna)
  - `--batch-size` (기본: 32)
  - `--delay` (기본: 1.5초)
- [ ] 각 단계별 로그 출력 (시작/완료/소요시간)
- [ ] 특정 단계만 실행할 수 있는 서브커맨드: `crawl`, `chunk`, `embed`, `upload`
- [ ] 에러 발생 시 중간 결과는 보존되고 해당 단계부터 재실행 가능

## Functional Requirements

- FR-1: HTTP 클라이언트로 KSAE 게시판 페이지를 요청하고 HTML을 파싱하여 게시글 목록 및 상세 내용을 추출한다
- FR-2: 목록 페이지의 페이지네이션을 자동 감지하여 1페이지부터 마지막 페이지까지 순회한다
- FR-3: 상세 페이지에서 질문 본문, 답변 본문, 댓글을 분리 추출하고 HTML을 정리된 텍스트로 변환한다
- FR-4: 요청 간 1~2초 딜레이를 두어 서버에 과도한 부하를 주지 않는다
- FR-5: 크롤링 결과를 구조화된 JSON 형태로 로컬 파일시스템에 저장한다
- FR-6: Q&A 텍스트를 512토큰 기준으로 청킹하되, 질문+답변 쌍은 가능한 한 하나의 청크로 유지한다
- FR-7: 청크 간 50토큰 오버랩을 적용하여 문맥 손실을 방지한다
- FR-8: BGE-M3 모델로 각 청크의 dense embedding 벡터(1024차원)를 생성한다
- FR-9: Qdrant에 `ksae_qna` 컬렉션을 생성하고 벡터 + 메타데이터를 업로드한다
- FR-10: `category` 필드에 payload 인덱스를 생성하여 카테고리별 필터 검색을 지원한다
- FR-11: 증분 크롤링 모드에서 기존 데이터와 비교하여 새 게시글만 처리한다
- FR-12: CLI 인터페이스로 전체/부분 파이프라인 실행을 제어한다

## Non-Goals

- 게시판 로그인이 필요한 비공개 게시글 크롤링은 지원하지 않음
- 첨부파일(이미지, PDF 등)의 다운로드 및 내용 추출은 범위 밖
- RAG 챗봇 자체의 구현 (이 PRD는 데이터 파이프라인만 다룸)
- 실시간 크롤링 또는 웹훅 기반 자동 업데이트
- Qdrant 외 다른 벡터 DB 지원
- Sparse embedding 또는 ColBERT 등 multi-vector 임베딩 (dense만 사용)
- 웹 UI 또는 API 서버 구현

## Technical Considerations

### 게시판 구조
- 목록 URL: `https://www.ksae.org/jajak/bbs/?code=J_qna&page={n}`
- 상세 URL: `https://www.ksae.org/jajak/bbs/?number={id}&mode=view&code=J_qna`
- 페이지네이션: 1~11페이지, "마지막" 버튼의 page 파라미터로 마지막 페이지 감지
- 카테고리: Baja, Formula, EV, 기술, 기타
- 답변: 게시글당 단일 답변 형태 + 댓글 존재 가능

### 기술 스택
- **언어:** Python 3.10+
- **크롤링:** `requests` + `BeautifulSoup4`
- **임베딩:** `FlagEmbedding` 또는 `sentence-transformers` (BAAI/bge-m3)
- **청킹:** `langchain-text-splitters` 또는 직접 구현
- **벡터 DB:** `qdrant-client`
- **CLI:** `argparse` 또는 `click`

### 디렉토리 구조
```
ksae-qna/
├── main.py                 # CLI 진입점
├── requirements.txt
├── src/
│   ├── crawler.py          # 크롤링 로직
│   ├── chunker.py          # 청킹 로직
│   ├── embedder.py         # 임베딩 로직
│   └── uploader.py         # Qdrant 업로드 로직
├── data/
│   ├── raw/                # 크롤링 원본 데이터
│   │   ├── posts.json
│   │   └── crawl_meta.json
│   └── processed/          # 가공된 데이터
│       ├── chunks.json
│       └── embeddings.npy
└── tasks/
    └── prd-ksae-qna-vectordb.md
```

### 성능 고려사항
- 전체 크롤링 예상 소요: 약 150건 x 1.5초 딜레이 = ~4분
- BGE-M3 임베딩: GPU 사용 시 수초, CPU는 수분 예상
- Qdrant 업로드: 수백 건 수준이므로 성능 이슈 없음

## Success Metrics

- 게시판의 전체 게시글을 100% 크롤링 (누락 0건)
- 크롤링 데이터가 올바른 JSON 스키마로 저장되고 파싱 가능
- 모든 청크가 Qdrant에 업로드되고 벡터 검색으로 유사 질문 검색 가능
- 증분 모드에서 기존 게시글을 중복 처리하지 않음
- `python main.py --mode full` 한 번으로 전체 파이프라인 완료

## Open Questions

- KSAE 게시판에 robots.txt 또는 크롤링 제한 정책이 있는지 확인 필요
- 게시판 세션/쿠키 관리가 필요한지 (일부 페이지 접근 시 로그인 필요 여부)
- BGE-M3의 max_length(8192토큰)를 고려한 청크 최대 크기 조정 필요 여부
- Qdrant를 로컬 Docker로 운영할지, 클라우드(Qdrant Cloud)를 사용할지
