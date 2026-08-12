"""A/B evaluate Formula 2026 rule embedding quality.

Usage:
  python bench/rules_reembed_bench.py \
    --chunks data/processed/rules_chunks.json \
    --baseline-collection ksae-formula-rules \
    --candidate-collection ksae-formula-rules-reembed
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

DEFAULT_URL = "https://vectordb.luftaquila.io:443"
DEFAULT_LIMIT = 10
DEFAULT_BASE_COLLECTION = "ksae-formula-rules"
DEFAULT_CANDIDATE_COLLECTION = "ksae-formula-rules-reembed"
DEFAULT_CHUNKS_PATH = "data/processed/rules_chunks.json"
DEFAULT_OUTPUT = "bench/output/rules-reembed-bench.json"
DEFAULT_SAMPLES_PER_TASK = 60
DEFAULT_SEED = 20260811


@dataclass
class QueryCase:
    query: str
    task: str
    chapter_num: str
    chapter: str
    section_num: str
    section: str
    anchor: str


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _extract_body(chunk: dict[str, Any]) -> str:
    text = (chunk.get("text") or "").strip()
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0].startswith("[제") and lines[1].startswith("[제"):
        return "\n".join(lines[2:]).strip()
    return text


def _split_sentence(text: str) -> str:
    cleaned = _normalize(text)
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[\.!?])\s+|\n", cleaned)
    if parts:
        candidate = parts[0].strip()
        if len(candidate) >= 20:
            return candidate
    return cleaned[:120]


def _safe_int(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _build_queries(
    chunks: list[dict[str, Any]],
    samples_per_task: int,
    seed: int,
) -> list[QueryCase]:
    unique_sections: dict[tuple[str, str, str], dict[str, Any]] = {}
    for chunk in chunks:
        if chunk.get("chunk_index", 0) != 0:
            continue
        key = (
            _safe_int(chunk.get("chapter_num")),
            _safe_int(chunk.get("section")),
            _safe_int(chunk.get("section_num")),
        )
        unique_sections.setdefault(key, chunk)

    random.seed(seed)
    title_cases: list[QueryCase] = []
    body_cases: list[QueryCase] = []

    for chunk in unique_sections.values():
        section = _normalize(chunk.get("section", ""))
        if not section:
            continue

        chapter_num = _safe_int(chunk.get("chapter_num"))
        chapter = (chunk.get("chapter") or "").strip()
        section_num = _safe_int(chunk.get("section_num"))

        title_cases.append(
            QueryCase(
                query=f"제{chapter_num}조 {section}",
                task="title",
                chapter_num=chapter_num,
                chapter=chapter,
                section_num=section_num,
                section=section,
                anchor=section,
            )
        )

        body = _extract_body(chunk)
        sentence = _split_sentence(body)
        if sentence:
            body_cases.append(
                QueryCase(
                    query=sentence,
                    task="body",
                    chapter_num=chapter_num,
                    chapter=chapter,
                    section_num=section_num,
                    section=section,
                    anchor=sentence,
                )
            )

    random.shuffle(title_cases)
    random.shuffle(body_cases)
    if samples_per_task > 0:
        title_cases = title_cases[:samples_per_task]
        body_cases = body_cases[:samples_per_task]

    return title_cases + body_cases


def _query(collection: str, query: str, limit: int, model: SentenceTransformer, client: QdrantClient) -> list[Any]:
    vector = model.encode(query).tolist()
    return client.query_points(
        collection_name=collection,
        query=vector,
        limit=limit,
        with_payload=True,
        with_vectors=False,
    ).points


def _relevant(q: QueryCase, hit: Any) -> bool:
    payload = hit.payload or {}
    if not payload:
        return False

    if (
        _safe_int(payload.get("chapter_num")) == q.chapter_num
        and _safe_int(payload.get("section_num")) == q.section_num
        and _normalize(payload.get("section", "")) == q.section
    ):
        return True

    if q.anchor:
        return _normalize(payload.get("content", "")).find(_normalize(q.anchor)) >= 0
    return False


def _metrics(
    collection: str,
    queries: Iterable[QueryCase],
    limit: int,
    model: SentenceTransformer,
    client: QdrantClient,
) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {
        "title": {"n": 0, "r1": 0, "r5": 0, "mrr": 0.0, "latency_ms": []},
        "body": {"n": 0, "r1": 0, "r5": 0, "mrr": 0.0, "latency_ms": []},
    }

    for q in queries:
        t = totals[q.task]
        t["n"] += 1

        start = time.time()
        hits = _query(collection, q.query, limit, model, client)
        elapsed_ms = (time.time() - start) * 1000
        t["latency_ms"].append(elapsed_ms)

        rank = None
        for i, hit in enumerate(hits, 1):
            if _relevant(q, hit):
                rank = i
                break
        if rank is None:
            continue

        t["mrr"] += 1.0 / rank
        if rank <= 5:
            t["r5"] += 1
        if rank == 1:
            t["r1"] += 1

    for t in totals.values():
        n = t["n"] if t["n"] else 1
        t["recall@1"] = t["r1"] / n
        t["recall@5"] = t["r5"] / n
        t["mrr"] = t["mrr"] / n
        t["ms/query"] = sum(t["latency_ms"]) / len(t["latency_ms"]) if t["latency_ms"] else 0.0
        t["n"] = int(t["n"])

    return {"name": collection, "metrics": totals, "sample_count": sum(v["n"] for v in totals.values())}


def _find_missing(collection_name: str, client: QdrantClient) -> None:
    if not client.collection_exists(collection_name):
        raise RuntimeError(f"Collection '{collection_name}' not found on this Qdrant.")


def run_bench(
    chunks_path: str,
    baseline_collection: str,
    candidate_collection: str,
    qdrant_url: str,
    api_key: str | None,
    limit: int = DEFAULT_LIMIT,
    samples_per_task: int = DEFAULT_SAMPLES_PER_TASK,
    seed: int = DEFAULT_SEED,
    timeout: int = 60,
) -> dict[str, Any]:
    with open(chunks_path, "r", encoding="utf-8") as f:
        chunks: list[dict[str, Any]] = json.load(f)

    queries = _build_queries(chunks, samples_per_task=samples_per_task, seed=seed)
    if not queries:
        raise RuntimeError(f"No usable queries generated from {chunks_path}")

    model = SentenceTransformer("BAAI/bge-m3")
    client = QdrantClient(url=qdrant_url, api_key=api_key, timeout=timeout)

    for col in (baseline_collection, candidate_collection):
        if col:
            _find_missing(col, client)

    base = _metrics(baseline_collection, queries, limit, model, client)
    new = _metrics(candidate_collection, queries, limit, model, client)
    return {
        "baseline_collection": baseline_collection,
        "candidate_collection": candidate_collection,
        "seed": seed,
        "limit": limit,
        "samples_per_task": samples_per_task,
        "queries": len(queries),
        "baseline": base,
        "candidate": new,
        "comparison": {
            "title": {
                "d_recall@1": new["metrics"]["title"]["recall@1"] - base["metrics"]["title"]["recall@1"],
                "d_recall@5": new["metrics"]["title"]["recall@5"] - base["metrics"]["title"]["recall@5"],
                "d_mrr": new["metrics"]["title"]["mrr"] - base["metrics"]["title"]["mrr"],
                "speed_ms_delta": new["metrics"]["title"]["ms/query"] - base["metrics"]["title"]["ms/query"],
            },
            "body": {
                "d_recall@1": new["metrics"]["body"]["recall@1"] - base["metrics"]["body"]["recall@1"],
                "d_recall@5": new["metrics"]["body"]["recall@5"] - base["metrics"]["body"]["recall@5"],
                "d_mrr": new["metrics"]["body"]["mrr"] - base["metrics"]["body"]["mrr"],
                "speed_ms_delta": new["metrics"]["body"]["ms/query"] - base["metrics"]["body"]["ms/query"],
            },
        },
    }


def _print_summary(result: dict[str, Any]) -> None:
    print("\n=== 규정 임베딩 A/B 비교 결과 ===")
    print(f"컬렉션: 기존={result['baseline_collection']} / 신규={result['candidate_collection']}")
    print(f"평가 질의: {result['queries']}개 (task당 {result['samples_per_task']}개)")
    print(f"조회 깊이: top-{result['limit']}")

    for task in ("title", "body"):
        base_m = result["baseline"]["metrics"][task]
        cand_m = result["candidate"]["metrics"][task]
        print(f"\n[{task}]")
        print(f"  기존: R@1={base_m['recall@1']:.3f}, R@5={base_m['recall@5']:.3f}, MRR={base_m['mrr']:.3f}, latency={base_m['ms/query']:.1f}ms")
        print(f"  신규: R@1={cand_m['recall@1']:.3f}, R@5={cand_m['recall@5']:.3f}, MRR={cand_m['mrr']:.3f}, latency={cand_m['ms/query']:.1f}ms")
        print(f"  개선: ΔR@1={result['comparison'][task]['d_recall@1']:+.3f}, "
              f"ΔR@5={result['comparison'][task]['d_recall@5']:+.3f}, "
              f"ΔMRR={result['comparison'][task]['d_mrr']:+.3f}, "
              f"Δms={result['comparison'][task]['speed_ms_delta']:+.1f}")

    if result["comparison"]["title"]["d_recall@1"] > 0 or result["comparison"]["body"]["d_recall@1"] > 0:
        print("\n종합적으로 신규 임베딩이 기존 대비 상단 정답률에서 개선이 관찰됨을 의미합니다.")
    else:
        print("\n상단 정답률 기준으로는 기존 대비 개선이 크지 않습니다.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", default=DEFAULT_CHUNKS_PATH, help="Formula 규정 chunk JSON 경로")
    parser.add_argument("--baseline-collection", default=DEFAULT_BASE_COLLECTION, help="기존 규정 컬렉션")
    parser.add_argument("--candidate-collection", default=DEFAULT_CANDIDATE_COLLECTION, help="신규 임베딩 컬렉션")
    parser.add_argument("--qdrant-url", default=DEFAULT_URL, help="Qdrant URL")
    parser.add_argument("--api-key", default=os.environ.get("QDRANT_API_KEY"), help="Qdrant API key (or QDRANT_API_KEY)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="각 질의 상단 조회 수")
    parser.add_argument("--samples-per-task", type=int, default=DEFAULT_SAMPLES_PER_TASK, help="제목 질의/본문 질의 각각 샘플 수")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="샘플 추출 시드")
    parser.add_argument("--timeout", type=int, default=60, help="Qdrant 타임아웃 초")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="결과 JSON 저장 경로")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.api_key:
        raise RuntimeError("Qdrant API key is required. set --api-key or QDRANT_API_KEY.")

    result = run_bench(
        chunks_path=args.chunks,
        baseline_collection=args.baseline_collection,
        candidate_collection=args.candidate_collection,
        qdrant_url=args.qdrant_url,
        api_key=args.api_key,
        limit=args.limit,
        samples_per_task=args.samples_per_task,
        seed=args.seed,
        timeout=args.timeout,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    _print_summary(result)
    print(f"\n결과 저장: {output_path}")


if __name__ == "__main__":
    main()
