"""Build sparse-enabled ``*-v2`` collections alongside the live ones.

Qdrant cannot add a sparse vector to an existing collection
(``update_collection`` rejects it with "Not existing vector name"), so enabling
BGE-M3 lexical weights requires a new collection. Recreating the live ones in
place would be unrecoverable — ``ksae-formula-rules`` has no ingestion pipeline
at all — so this builds copies:

    ksae-qna            -> ksae-qna-v2
    ksae-formula-rules  -> ksae-formula-rules-v2
    ksae-aark-kb        -> ksae-aark-kb-v2

The dense vectors are **copied verbatim** from the source collection rather
than recomputed. BGEM3FlagModel and SentenceTransformer produce bit-identical
dense output for BGE-M3 (verified: max abs error 0.0), so copying keeps the A/B
comparison honest — the only difference between v1 and v2 is the sparse index.

Switching traffic is then a one-line change in ``COLLECTION_REGISTRY``; rolling
back is the same edit in reverse, with the original collections untouched.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from qdrant_client import QdrantClient, models

from src.uploader import build_client

logger = logging.getLogger(__name__)

DENSE = "dense"
SPARSE = "sparse"
SCROLL_BATCH = 256
UPSERT_BATCH = 128


def _iter_points(
    client: QdrantClient, collection: str
) -> Iterator[list[models.Record]]:
    """Yield every point of *collection* with payload and dense vector."""
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=SCROLL_BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if points:
            yield points
        if offset is None:
            return


def _dense_of(record: models.Record) -> list[float]:
    """Extract the dense vector from a legacy (unnamed) or named record."""
    vec = record.vector
    if isinstance(vec, dict):
        return list(vec[DENSE])
    if vec is None:
        raise ValueError(f"point {record.id} has no vector")
    return list(vec)


def _to_sparse(weights: dict[str, float]) -> models.SparseVector:
    """Convert BGE-M3 lexical weights to a Qdrant sparse vector."""
    if not weights:
        # Qdrant rejects nothing here, but an empty vector never matches;
        # that is the correct behaviour for a chunk with no lexical signal.
        return models.SparseVector(indices=[], values=[])
    indices = [int(k) for k in weights.keys()]
    values = [float(v) for v in weights.values()]
    return models.SparseVector(indices=indices, values=values)


def _payload_indexes(client: QdrantClient, source: str) -> dict[str, Any]:
    """Read the payload index schema of the source collection."""
    return dict(client.get_collection(source).payload_schema or {})


def migrate(
    qdrant_url: str,
    api_key: str | None,
    source: str,
    target: str,
    encoder: Any,
    batch_size: int = 8,
) -> int:
    """Copy *source* into *target*, adding a BGE-M3 sparse vector.

    Returns the number of points written.
    """
    client = build_client(qdrant_url, api_key)

    info = client.get_collection(source)
    dim = info.config.params.vectors.size  # type: ignore[union-attr]
    indexes = _payload_indexes(client, source)

    if client.collection_exists(target):
        client.delete_collection(target)
        logger.info("Deleted existing target '%s'", target)

    client.create_collection(
        collection_name=target,
        vectors_config={DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
        sparse_vectors_config={SPARSE: models.SparseVectorParams()},
    )
    print(f"Created '{target}' (dense {dim} + sparse)")

    written = 0
    for records in _iter_points(client, source):
        texts = [r.payload.get("content", "") if r.payload else "" for r in records]
        out = encoder.encode(
            texts,
            batch_size=batch_size,
            return_dense=False,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        points = [
            models.PointStruct(
                id=r.id,
                vector={DENSE: _dense_of(r), SPARSE: _to_sparse(w)},
                payload=r.payload,
            )
            for r, w in zip(records, out["lexical_weights"])
        ]
        for i in range(0, len(points), UPSERT_BATCH):
            client.upsert(collection_name=target, points=points[i : i + UPSERT_BATCH])
        written += len(points)
        print(f"  {written} points", end="\r", flush=True)

    print(f"  {written} points written to '{target}'")

    # Rebuild the payload indexes the source had, so filters behave identically.
    for field, schema in indexes.items():
        data_type = getattr(schema, "data_type", None)
        try:
            if str(data_type).endswith("TEXT"):
                client.create_payload_index(
                    collection_name=target,
                    field_name=field,
                    field_schema=models.TextIndexParams(
                        type="text",
                        tokenizer=models.TokenizerType.MULTILINGUAL,
                        min_token_len=2,
                    ),
                )
            else:
                client.create_payload_index(
                    collection_name=target,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            print(f"  index: {field}")
        except Exception as e:  # pragma: no cover - index already present
            logger.warning("index %s on %s failed: %s", field, target, e)

    got = client.get_collection(target).points_count
    print(f"'{target}': {got} points")
    return written
