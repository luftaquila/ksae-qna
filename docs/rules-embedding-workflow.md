# 2026 규정 전체 임베딩 워크플로우

## 1. 목적

`rules-2026` 파이프라인은 KSAE 규정 게시판(`J_rule`)의 2026년 규정 본문만 수집해
종목×규정유형 기준으로 분리 임베딩, 그리고 Qdrant 컬렉션 업로드까지 수행한다.
`formula` 전용 기존 루틴(`rules`/`rules-chunk`/`rules-embed`/`rules-upload`)은
호환성을 위해 유지한다.

현재 기준(2026-08-12):

- 수집 PDF: 9개
- 대상은 모두 “규정 본문”만 사용, `규정 제정안/개정안`은 제외
- 실제 업로드 운영 대상 컬렉션:
  - `ksae-rules-baja-event-operation-2026-v2`
  - `ksae-rules-baja-vehicle-technical-2026-v2`
  - `ksae-rules-formula-event-operation-2026-v2`
  - `ksae-rules-formula-vehicle-technical-2026-v2`
  - `ksae-rules-other-competition-rules-2026-v2`
  - `ksae-rules-other-other-2026-v2`
  - `ksae-rules-smart-e-mobility-event-operation-2026-v2`
  - `ksae-rules-smart-e-mobility-vehicle-technical-2026-v2`

### 현재 수집된 문서명(한국어)

- `2026 대학생 자작자동차대회 - Formula 차량기술규정`
- `2026 대학생 자작자동차대회 - Baja 차량기술규정`
- `2026 대학생 스마트 e모빌리티 경진대회 - EV 차량기술규정 [규정 변경]`
- `2026 대학생 자작자동차대회 - Formula 경기진행규정`
- `2026 대학생 자작자동차대회 - Baja Student Korea 경기진행규정`
- `2026 대학생 스마트 e모빌리티 경진대회 - EV 경기진행규정`
- `2026 대학생 자작자동차대회 - 기술부문규정`
- `2026 대학생 자작자동차대회 - 대회운영규정`
- `2026 대학생 자작자동차대회 - 발표대회규정`

## 2. 수집/임베딩/적재 일괄 실행

```sh
QDRANT_API_KEY=... \
python main.py \
  --qdrant-url https://vectordb.luftaquila.io:443 \
  --qdrant-api-key "$QDRANT_API_KEY" \
  rules-2026 \
  --year 2026 \
  --manifest data/raw/rules-2026/rules-2026-manifest.json \
  --raw-dir data/raw/rules-2026 \
  --chunks data/processed/rules-2026/rules-2026-all-chunks.json \
  --recreate
```

동작 순서

1. J_rule 크롤링 + PDF 다운로드
2. `manifest` 기반 청크 생성
3. `chunk`를 규정 분류(competition × document_type)별 버킷으로 분리
4. 각 버킷별 임베딩 생성
5. 각 버킷별 Qdrant 업로드

## 3. 단계별 실행

### 3-1) 2026 규정 수집/다운로드

- 제정안/개정안 표기(예: `제정(안)`, `제정안`, `개정안`, `규정안`)가 포함된 항목은 수집 대상에서 제외한다.

```sh
python main.py rules-2026-crawl \
  --year 2026 \
  --manifest data/raw/rules-2026/rules-2026-manifest.json \
  --raw-dir data/raw/rules-2026
```

### 3-2) 2026 규정 청크 생성

```sh
python main.py rules-2026-chunk \
  --manifest data/raw/rules-2026/rules-2026-manifest.json \
  --year 2026 \
  --output data/processed/rules-2026/rules-2026-all-chunks.json
```

### 3-3) 2026 규정 임베딩(전체/일부 컬렉션)

```sh
python main.py rules-2026-embed --year 2026
# 특정 컬렉션만 처리
python main.py rules-2026-embed --year 2026 --collection rules-formula-vehicle-technical-2026
```

### 3-4) 2026 규정 업로드(전체/일부 컬렉션)

```sh
python main.py --qdrant-api-key "$QDRANT_API_KEY" --qdrant-url https://vectordb.luftaquila.io:443 \
  rules-2026-upload --year 2026 --recreate
# 특정 컬렉션만 업로드
python main.py --qdrant-api-key "$QDRANT_API_KEY" --qdrant-url https://vectordb.luftaquila.io:443 \
  rules-2026-upload --year 2026 --collection rules-formula-vehicle-technical-2026 --recreate
```

## 4. 규정 컬렉션 구조

`src/rules_registry.py`에서 자동 생성되는 스키마:

`rules-{competition}-{document_type}-2026`

컬렉션명은 `rules_collection_registry()`의 실제 업로드 대상명 규칙으로
`ksae-rules-{competition}-{document_type}-2026-v2` 형식이 생성된다.

카테고리는 `competition × document_type`으로 모두 생성되며, 실제 데이터가 없는 조합은
청크가 비어있어 업로드가 스킵된다.

## 5. 비교군 없이 진행하는 정확도 개선 항목

아래 항목은 비교군 없이 현재 문서 집합 자체에서 바로 적용 가능한 정밀도 개선이다.
임베딩 후에는 각 항목별로 주관적 QA 점검으로 정확도 회복/하락 여부를 판정한다.

### 5-1. 임베딩 품질 개선 (적용 완료)

1) 규정 텍스트 정규화 (`src/rules_chunker.py`)
- Unicode 정규화 및 규격 용어 정규화(예: `규정`, `경기진행`, `차량기술` 변형 정합)
- 불필요 기호/공백 정리 → 임베딩 토큰 품질 정리
- 문장/단락 분할 시 `MAX_TOKENS=384`, `OVERLAP_TOKENS=64`로 연결성 보전

2) 규정 헤더 메타 강화 (`src/rules_chunker.py`)
- chunk마다 `[규정문서]`, `[규정분류]`, `[장]` 형태로 문맥 힌트를 포함
- 같은 핵심 조항도 문서유형/종목 문맥을 잃지 않게 검색 컨텍스트 강화

3) 규정 질의 정규화 및 의도 추론 (`src/chat.py`)
- 질의에서 규정 유형, 종목명, 조/항 참조 패턴을 추출
- `경기진행규정`, `차량기술규정`, `심사규정` 등 용어 매핑을 일관화

4) 컬렉션 레벨 필터/부스팅 (`src/chat.py`)
- 규정 질의에서 추정한 `competition / document_type`을 우선 적용해 후보군 축소
- 문서 제목/헤더/항목 키워드 일치도를 기반으로 규정 결과에 가산점 부여

### 5-2. 추가로 더 밀어붙일 개선 항목

1) 문서 메타데이터 보강
- `source_filename`, `source_url`, `doc_id`, `source_sha1`, `published_at`, `version`를 일괄 수집하여
  동명 파일 충돌, 갱신 판별 정확도 향상

2) 청크 경계 규칙 강화
- 조항 경계가 분리되는 지점을 더 엄격히 검출(부록/표/목차 혼입 방지)
- 표와 본문을 별도 chunk_type으로 태깅해 재질의형 검색에서 신호를 분리

3) 정합성 강화
- 동일 조항 반복 등장 구간에서 중복 임베딩 제거 (fingerprint 기반 dedup)
- 소량 쿼리 테스트(차량기술/경기진행/대회운영/심사별)로 100개 이상 샘플을 주간 단위로 수동 검토

### 5-3. 문서별 정확도 확인 (수치 비교 없이)

- 질문 1개당 후보 `k=5` 결과를 보고 다음이 만족되는지 수동 점검
  - 상위 1~2개 중 1개 이상이 실제 대상 규정 조항과 제목이 일치
  - 조/항 참조가 있으면 그 번호가 텍스트 본문에 실제 존재
  - 동일 문서 내 서로 다른 규칙(안전 vs 심사/기술)이 섞여 상위 노출되지 않음
- 문서유형별 예시 쿼리 스위트(각 10~20개)로 주 1회 점검
  - 차량기술: `배터리`, `안전장치`, `전장`, `차량무게`
  - 경기진행: `개회`, `점수`, `타이밍`, `심사절차`
  - 대회운영: `접수`, `경기 일정`, `제재`
  - 발표/심사: `기술부문`, `심사 기준`, `사전심사`

## 6. 라이브 반영 체크리스트

1. `rules-2026`로 최신 2026 규정 전체 적재
2. 운영 Qdrant 컬렉션 존재/사이즈 확인
   - `curl -H "api-key: $QDRANT_API_KEY" https://vectordb.luftaquila.io:443/collections | jq`
3. 업로드 대상 컬렉션명(`ksae-rules-*`)과 `registry` 키(`rules-{competition}-{document_type}-2026`)이 일치하는지 확인
4. 챗봇 검색 요청에서 `rules`/`rules-*` 키 동작 검증
5. 문서유형별 수동 쿼리셋 결과가 허용 기준 충족 확인
