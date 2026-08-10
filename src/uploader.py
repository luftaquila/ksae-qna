"""Uploader module for Qdrant vector database.

Uploads embedding vectors and associated metadata to a Qdrant
collection for vector similarity search in the RAG pipeline.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024
DEFAULT_BATCH_SIZE = 100
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION = "ksae_qna"


# Payload fields carried from a chunk in addition to the always-present
# id / content / chunk_index. Q&A chunks use the default; other sources
# (e.g. the AARK knowledge base) pass their own field list.
QNA_PAYLOAD_FIELDS = ("category", "title", "author", "date", "url", "has_answer")


def build_client(qdrant_url: str, api_key: str | None = None, timeout: int = 60) -> QdrantClient:
    """Build a Qdrant client from a URL, with or without an explicit port.

    ``https://host`` -> 443, ``http://host`` -> 6333, and an explicit
    ``host:port`` is honoured. The naive string-stripping this replaced could
    not read a port, so the in-cluster form ``http://qdrant:6333`` failed.
    """
    parsed = urlparse(qdrant_url if "://" in qdrant_url else f"http://{qdrant_url}")
    is_https = parsed.scheme == "https"
    host = parsed.hostname or qdrant_url
    port = parsed.port or (443 if is_https else 6333)
    return QdrantClient(
        host=host,
        port=port,
        https=is_https,
        api_key=api_key,
        prefer_grpc=False,
        timeout=timeout,
    )


def upload_to_qdrant(
    chunks_path: str | Path = "data/processed/chunks.json",
    embeddings_path: str | Path = "data/processed/embeddings.npy",
    qdrant_url: str = DEFAULT_QDRANT_URL,
    api_key: str | None = None,
    collection_name: str = DEFAULT_COLLECTION,
    batch_size: int = DEFAULT_BATCH_SIZE,
    recreate: bool = False,
    payload_fields: tuple[str, ...] | list[str] = QNA_PAYLOAD_FIELDS,
    index_fields: tuple[str, ...] | list[str] = ("category",),
    prune: bool = False,
) -> None:
    """Upload embedding vectors and metadata to Qdrant.

    Reads chunks and their embedding vectors, connects to a Qdrant
    instance, creates or reuses a collection, and uploads all points
    in batches.

    Args:
        chunks_path: Path to the chunks JSON file.
        embeddings_path: Path to the embeddings numpy file.
        qdrant_url: URL of the Qdrant server.
        collection_name: Name of the Qdrant collection.
        batch_size: Number of points to upload per batch.
        recreate: If True, delete and recreate the collection if it exists.
        payload_fields: Chunk keys copied into the payload alongside
            ``id`` / ``content`` / ``chunk_index``. Missing keys become "".
        index_fields: Payload fields to build a keyword index on for
            filtered search.
        prune: If True, delete points that this run did not write. Needed for
            document sources: upsert keys are derived from (post_id,
            chunk_index), so an item removed from the source document would
            otherwise linger in the collection and keep being retrieved.
    """
    chunks_path = Path(chunks_path)
    embeddings_path = Path(embeddings_path)

    # Load chunks
    with open(chunks_path, "r", encoding="utf-8") as f:
        chunks: list[dict[str, Any]] = json.load(f)

    # Load embeddings
    embeddings: np.ndarray[Any, np.dtype[np.float32]] = np.load(embeddings_path)

    num_chunks = len(chunks)
    assert embeddings.shape == (num_chunks, EMBEDDING_DIM), (
        f"Mismatch: {num_chunks} chunks but embeddings shape {embeddings.shape}"
    )

    logger.info("Loaded %d chunks and embeddings", num_chunks)
    print(f"Loaded {num_chunks} chunks and embeddings")

    if num_chunks == 0:
        print("No data to upload.")
        return

    client = build_client(qdrant_url, api_key)
    logger.info("Connected to Qdrant at %s", qdrant_url)

    # Create or reuse collection
    collection_exists = client.collection_exists(collection_name)

    if collection_exists and recreate:
        client.delete_collection(collection_name)
        logger.info("Deleted existing collection '%s'", collection_name)
        print(f"Deleted existing collection '{collection_name}'")
        collection_exists = False

    if not collection_exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=EMBEDDING_DIM,
                distance=Distance.COSINE,
            ),
        )
        logger.info("Created collection '%s'", collection_name)
        print(f"Created collection '{collection_name}'")
    else:
        logger.info("Collection '%s' already exists, skipping creation", collection_name)
        print(f"Collection '{collection_name}' already exists, skipping creation")

    # Upload points in batches
    written_ids: set[str] = set()
    for i in range(0, num_chunks, batch_size):
        batch_chunks = chunks[i : i + batch_size]
        batch_embeddings = embeddings[i : i + batch_size]

        points = [
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk['post_id']}_{chunk['chunk_index']}")),
                vector=embedding.tolist(),
                payload={
                    "id": chunk["post_id"],
                    "content": chunk["text"],
                    "chunk_index": chunk["chunk_index"],
                    **{k: chunk.get(k, "") for k in payload_fields},
                },
            )
            for chunk, embedding in zip(batch_chunks, batch_embeddings)
        ]

        client.upsert(collection_name=collection_name, points=points)
        written_ids.update(p.id for p in points)
        logger.info("Uploaded batch %d-%d", i, i + len(batch_chunks))

    print(f"Uploaded {num_chunks} points to collection '{collection_name}'")

    if prune:
        stale = _find_stale_points(client, collection_name, written_ids)
        if stale:
            client.delete(collection_name=collection_name, points_selector=stale)
            logger.info("Pruned %d stale points", len(stale))
            print(f"Pruned {len(stale)} stale points (source에서 사라진 항목)")
        else:
            print("No stale points to prune")

    # Create keyword payload indexes for filtered search
    for field in index_fields:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
        )
        logger.info("Created keyword index on '%s' field", field)
        print(f"Created keyword index on '{field}' field")

    # Create full-text index on content for hybrid search
    from qdrant_client.models import TextIndexParams, TokenizerType
    client.create_payload_index(
        collection_name=collection_name,
        field_name="content",
        field_schema=TextIndexParams(
            type="text",
            tokenizer=TokenizerType.MULTILINGUAL,
            min_token_len=2,
        ),
    )
    logger.info("Created full-text index on 'content' field")
    print("Created full-text index on 'content' field")

    # Print collection info
    collection_info = client.get_collection(collection_name)
    print(f"Collection info: {collection_info.points_count} points, "
          f"vector size {collection_info.config.params.vectors.size}")  # type: ignore[union-attr]


def _find_stale_points(
    client: QdrantClient,
    collection_name: str,
    written_ids: set[str],
) -> list[str]:
    """Return ids present in the collection but not written by this run."""
    stale: list[str] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection_name,
            limit=1000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        stale.extend(str(p.id) for p in points if str(p.id) not in written_ids)
        if offset is None:
            break
    return stale
