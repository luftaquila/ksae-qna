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


def upload_to_qdrant(
    chunks_path: str | Path = "data/processed/chunks.json",
    embeddings_path: str | Path = "data/processed/embeddings.npy",
    qdrant_url: str = DEFAULT_QDRANT_URL,
    collection_name: str = DEFAULT_COLLECTION,
    batch_size: int = DEFAULT_BATCH_SIZE,
    recreate: bool = False,
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

    # Connect to Qdrant
    client = QdrantClient(url=qdrant_url)
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
    for i in range(0, num_chunks, batch_size):
        batch_chunks = chunks[i : i + batch_size]
        batch_embeddings = embeddings[i : i + batch_size]

        points = [
            PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk['post_id']}_{chunk['chunk_index']}")),
                vector=embedding.tolist(),
                payload={
                    "id": chunk["post_id"],
                    "category": chunk["category"],
                    "title": chunk["title"],
                    "author": chunk.get("author", ""),
                    "date": chunk["date"],
                    "url": chunk["url"],
                    "chunk_text": chunk["text"],
                    "chunk_index": chunk["chunk_index"],
                },
            )
            for chunk, embedding in zip(batch_chunks, batch_embeddings)
        ]

        client.upsert(collection_name=collection_name, points=points)
        logger.info("Uploaded batch %d-%d", i, i + len(batch_chunks))

    print(f"Uploaded {num_chunks} points to collection '{collection_name}'")

    # Create payload index on category for filtered search
    client.create_payload_index(
        collection_name=collection_name,
        field_name="category",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    logger.info("Created keyword index on 'category' field")
    print("Created keyword index on 'category' field")

    # Print collection info
    collection_info = client.get_collection(collection_name)
    print(f"Collection info: {collection_info.points_count} points, "
          f"vector size {collection_info.config.params.vectors.size}")  # type: ignore[union-attr]
