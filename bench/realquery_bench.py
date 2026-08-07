"""실사용 질의 40개로 v1(운영)과 v2 변형들의 결과 차이·지연을 비교한다.

정답 라벨이 없으므로 품질 주장이 아니라 (a) 결과 중복도 (b) 지연 (c) 육안 대조용
상위 결과 차이를 낸다.
"""
from __future__ import annotations
import json, time, warnings
warnings.filterwarnings("ignore")
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from FlagEmbedding import BGEM3FlagModel
import src.chat as chat
import bench.retrieval_bench as B

client = QdrantClient(url=B.URL, api_key=B.KEY, timeout=60)
chat._qdrant = client
st = SentenceTransformer("BAAI/bge-m3")
fe = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
queries = json.load(open("/tmp/realq.json"))
K = 7

def enc(q):
    o = fe.encode([q], return_dense=True, return_sparse=True, return_colbert_vecs=False)
    w = o["lexical_weights"][0]
    return o["dense_vecs"][0].tolist(), models.SparseVector(
        indices=[int(k) for k in w], values=[float(v) for v in w.values()])

def v1(q):
    return [h.get("post_id") for h in
            chat._search_collection(st.encode(q).tolist(), "ksae-aark-kb", K, 0.0, None, q)]

def v2(q, mode):
    v, sv = enc(q)
    if mode == "sparse":
        r = client.query_points("ksae-aark-kb-v2", query=sv, using="sparse", limit=K)
    else:
        f = models.Fusion.DBSF if mode == "dbsf" else models.Fusion.RRF
        r = client.query_points("ksae-aark-kb-v2", prefetch=[
            models.Prefetch(query=v, using="dense", limit=K*2),
            models.Prefetch(query=sv, using="sparse", limit=K*2)],
            query=models.FusionQuery(fusion=f), limit=K)
    return [p.payload.get("id") for p in r.points]

modes = {"v1(운영)": v1, "v2 rrf": lambda q: v2(q,"rrf"),
         "v2 dbsf": lambda q: v2(q,"dbsf"), "v2 sparse-only": lambda q: v2(q,"sparse")}
res, lat = {}, {}
for name, fn in modes.items():
    t0 = time.time(); res[name] = [fn(q) for q in queries]
    lat[name] = (time.time()-t0)*1000/len(queries)

base = res["v1(운영)"]
print(f"실사용 질의 {len(queries)}개, top-{K}\n")
for name in modes:
    ov = sum(len(set(a) & set(b))/K for a, b in zip(base, res[name]))/len(queries)
    empty = sum(1 for r in res[name] if not r)
    print(f"  {name:16} v1과 top-{K} 중복 {ov*100:5.1f}%   결과없음 {empty:2}건   {lat[name]:5.0f}ms")

print("\n=== 결과가 가장 많이 갈린 질의 ===")
diff = sorted(range(len(queries)),
              key=lambda i: len(set(base[i]) & set(res['v2 sparse-only'][i])))[:3]
chunks = {c["post_id"]: c for c in json.load(open("data/processed/kb_chunks.json"))}
for i in diff:
    print(f"\nQ: {queries[i][:64]}")
    for name in ("v1(운영)", "v2 sparse-only"):
        top = res[name][i][:2]
        for pid in top:
            c = chunks.get(pid)
            print(f"   {name:15} {(c['topic'] or c['section'])[:56] if c else pid}")
