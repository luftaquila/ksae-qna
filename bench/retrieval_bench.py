"""v1(dense + MatchText) vs v2(dense + BGE-M3 sparse) 검색 성능 비교.

두 시스템의 dense 벡터는 비트 단위로 동일하므로 차이는 오직 두 번째 검색 arm이다.
정답이 있는 과제 두 종류로 잰다.

  1) 어휘 과제 — 부품번호·모델코드가 든 청크를 그 토큰으로 찾는다
  2) 의미 과제 — 항목의 '결론' 문장으로 그 항목 청크를 찾는다 (제목이 질의에 없음)

지표는 recall@1 / recall@5 / MRR@10. 같은 질의·같은 인코더·같은 limit을 쓴다.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
import warnings

warnings.filterwarnings("ignore")

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

import src.chat as chat

URL = "https://vectordb.luftaquila.io:443"
KEY = open("/tmp/qk.txt").read().strip()
K = 10
SEED = 20260807


def build_queries(chunks: list[dict]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(어휘 질의, 의미 질의) — 각 항목은 (query, gold_post_id)."""
    rng = random.Random(SEED)
    topics = [c for c in chunks if c["kind"] == "topic" and c["chunk_index"] == 0]

    # 어휘: 영숫자 혼합 토큰(QS165, KLS7275H, 43100, 6910ZZ …)이 든 청크
    code = re.compile(r"\b(?=[A-Za-z0-9-]{4,})(?=[^\s]*[A-Za-z])(?=[^\s]*\d)[A-Za-z0-9-]+\b")
    lexical: list[tuple[str, str]] = []
    seen_tokens: set[str] = set()
    for c in topics:
        for tok in code.findall(c["text"]):
            if tok in seen_tokens or len(tok) < 5:
                continue
            # 코퍼스 전체에서 드문 토큰만 (흔하면 정답이 유일하지 않다)
            seen_tokens.add(tok)
            lexical.append((tok, c["post_id"]))
            break
    rng.shuffle(lexical)
    lexical = [(q, g) for q, g in lexical if sum(1 for c in chunks if q in c["text"]) == 1][:120]

    # 의미: 결론 문장을 질의로. 주제명이 질의에 없어야 하므로 제외 확인
    semantic: list[tuple[str, str]] = []
    for c in rng.sample(topics, min(len(topics), 400)):
        m = re.search(r"^결론: (.{25,110})", c["text"], re.M)
        if not m:
            continue
        q = m.group(1).strip()
        if c["topic"] and c["topic"][:8] in q:
            continue
        semantic.append((q, c["post_id"]))
        if len(semantic) >= 120:
            break

    return lexical, semantic


def evaluate(name: str, queries: list[tuple[str, str]], search) -> dict:
    r1 = r5 = 0
    mrr = 0.0
    t0 = time.time()
    for q, gold in queries:
        ids = search(q)
        rank = next((i + 1 for i, pid in enumerate(ids) if pid == gold), None)
        if rank:
            mrr += 1.0 / rank
            if rank == 1:
                r1 += 1
            if rank <= 5:
                r5 += 1
    n = len(queries)
    return {
        "name": name, "n": n,
        "recall@1": r1 / n, "recall@5": r5 / n, "mrr@10": mrr / n,
        "ms/query": (time.time() - t0) * 1000 / n,
    }


def main() -> None:
    chunks = json.load(open("data/processed/kb_chunks.json"))
    lexical, semantic = build_queries(chunks)
    print(f"어휘 질의 {len(lexical)}개 · 의미 질의 {len(semantic)}개\n")

    client = QdrantClient(url=URL, api_key=KEY, timeout=60)
    chat._qdrant = client
    st = SentenceTransformer("BAAI/bge-m3")

    from FlagEmbedding import BGEM3FlagModel
    fe = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)

    def v1(q: str) -> list[str]:
        vec = st.encode(q).tolist()
        hits = chat._search_collection(vec, "ksae-aark-kb", K, 0.0, None, q)
        return [h.get("post_id") for h in hits]

    def v2(q: str) -> list[str]:
        out = fe.encode([q], return_dense=True, return_sparse=True, return_colbert_vecs=False)
        vec = out["dense_vecs"][0].tolist()
        w = out["lexical_weights"][0]
        sv = models.SparseVector(indices=[int(k) for k in w], values=[float(v) for v in w.values()])
        hits = chat._search_collection(vec, "ksae-aark-kb-v2", K, 0.0, None, q, sparse=sv)
        return [h.get("post_id") for h in hits]

    rows = []
    for task, qs in (("어휘(부품번호)", lexical), ("의미(결론문)", semantic)):
        for label, fn in (("v1 dense+MatchText", v1), ("v2 dense+sparse", v2)):
            r = evaluate(label, qs, fn)
            r["task"] = task
            rows.append(r)
            print(f"{task:14} {label:20} R@1 {r['recall@1']:.3f}  R@5 {r['recall@5']:.3f}  "
                  f"MRR {r['mrr@10']:.3f}  {r['ms/query']:.0f}ms")
    json.dump(rows, open("/tmp/bench.json", "w"), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
