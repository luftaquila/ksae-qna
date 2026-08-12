# 검색 성능 비교

`src/sparse_migrate.py`로 만든 `*-v2`(dense + BGE-M3 sparse) 컬렉션과
운영 중인 v1(dense + `MatchText` 하이브리드)을 비교한다.
두 쪽의 dense 벡터는 비트 단위로 동일하므로 차이는 두 번째 검색 arm뿐이다.

```sh
echo "<qdrant-api-key>" > /tmp/qk.txt      # 세 스크립트 공통
python bench/retrieval_bench.py            # v1 vs v2, 어휘·의미 과제 (정답 있음)
python bench/fusion_bench.py               # dense / sparse / RRF / DBSF 비교
python bench/realquery_bench.py            # 실사용 질의 40개, 결과 차이·지연
```

`realquery_bench.py`는 운영 DB에서 뽑은 질의를 `/tmp/realq.json`에서 읽는다.

규정 임베딩(2026) A/B 비교는 아래 스크립트를 사용합니다.

```sh
QDRANT_API_KEY=... \
python bench/rules_reembed_bench.py \
  --baseline-collection ksae-formula-rules \
  --candidate-collection ksae-formula-rules-reembed \
  --chunks data/processed/rules_chunks.json \
  --samples-per-task 60 \
  --limit 10 \
  --output bench/output/rules-reembed-bench.json
```

2026년 전체 규정 문서(차량기술규정 외 경기진행/심사/안전규정 포함)로 이동한 `rules-2026` 파이프라인은
`data/processed/rules-2026/rules-2026-all-chunks.json` 기준으로 비교할 때도 동일 스크립트를 사용합니다.

```sh
python bench/rules_reembed_bench.py \
  --chunks data/processed/rules-2026/rules-2026-all-chunks.json \
  --baseline-collection ksae-formula-rules \
  --candidate-collection <new-rule-collection> \
  --qdrant-url https://vectordb.luftaquila.io:443 \
  --api-key "$QDRANT_API_KEY" \
  --samples-per-task 60 \
  --limit 10 \
  --output bench/output/rules-2026-reembed-bench.json
```

## 측정 결과 요약 (2026-08-07, ksae-aark-kb 4,153점)

정답이 있는 합성 과제에서는 sparse가 어휘 검색에 강했지만,
**실사용 질의에서는 sparse-only가 어휘 우연 일치로 엉뚱한 결과를 냈다.**
결론은 `CLAUDE.md`의 Vector Search 절 참조.
