"""어휘 과제의 진짜 병목이 검색 arm인지 융합(RRF)인지 가른다."""
from __future__ import annotations
import json, warnings, time
warnings.filterwarnings("ignore")
from qdrant_client import QdrantClient, models
from FlagEmbedding import BGEM3FlagModel
import bench.retrieval_bench as B

client = QdrantClient(url=B.URL, api_key=B.KEY, timeout=60)
fe = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
chunks = json.load(open("data/processed/kb_chunks.json"))
lexical, semantic = B.build_queries(chunks)
COL = "ksae-aark-kb-v2"

def enc(q):
    o = fe.encode([q], return_dense=True, return_sparse=True, return_colbert_vecs=False)
    w = o["lexical_weights"][0]
    return o["dense_vecs"][0].tolist(), models.SparseVector(
        indices=[int(k) for k in w], values=[float(v) for v in w.values()])

def run(mode):
    def f(q):
        v, sv = enc(q)
        if mode == "sparse-only":
            r = client.query_points(COL, query=sv, using="sparse", limit=B.K)
        elif mode == "dense-only":
            r = client.query_points(COL, query=v, using="dense", limit=B.K)
        else:
            fusion = models.Fusion.DBSF if mode == "dbsf" else models.Fusion.RRF
            r = client.query_points(COL, prefetch=[
                    models.Prefetch(query=v, using="dense", limit=B.K*2),
                    models.Prefetch(query=sv, using="sparse", limit=B.K*2)],
                query=models.FusionQuery(fusion=fusion), limit=B.K)
        return [p.payload.get("id") for p in r.points]
    return f

print(f"어휘 {len(lexical)} · 의미 {len(semantic)}\n")
for task, qs in (("어휘(부품번호)", lexical), ("의미(결론문)", semantic)):
    for mode in ("dense-only", "sparse-only", "rrf", "dbsf"):
        r = B.evaluate(mode, qs, run(mode))
        print(f"{task:14} {mode:12} R@1 {r['recall@1']:.3f}  R@5 {r['recall@5']:.3f}  MRR {r['mrr@10']:.3f}  {r['ms/query']:.0f}ms")
    print()
