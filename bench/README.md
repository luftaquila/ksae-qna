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

## 측정 결과 요약 (2026-08-07, ksae-aark-kb 4,153점)

정답이 있는 합성 과제에서는 sparse가 어휘 검색에 강했지만,
**실사용 질의에서는 sparse-only가 어휘 우연 일치로 엉뚱한 결과를 냈다.**
결론은 `CLAUDE.md`의 Vector Search 절 참조.
