"""Embedder module for BGE-M3 vector generation.

Loads the BAAI/bge-m3 model locally or calls a remote embedding API
to generate dense embedding vectors (1024 dimensions) for each text chunk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from tqdm import tqdm

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024
DEFAULT_BATCH_SIZE = 32


def _embed_local(texts: list[str], batch_size: int) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Embed texts using the local SentenceTransformer model."""
    from sentence_transformers import SentenceTransformer

    print("Loading BGE-M3 model...")
    logger.info("Loading BGE-M3 model...")
    model = SentenceTransformer("BAAI/bge-m3")
    print("Model loaded.")
    logger.info("Model loaded.")

    all_embeddings: list[np.ndarray[Any, np.dtype[np.float32]]] = []
    for i in tqdm(range(0, len(texts), batch_size), desc="Embedding"):
        batch_texts = texts[i : i + batch_size]
        # Encode one text at a time to avoid MPS memory overflow from
        # padding all texts in the batch to the longest sequence length.
        output = model.encode(batch_texts, batch_size=1)
        all_embeddings.append(output)

    return np.vstack(all_embeddings).astype(np.float32)


def _embed_remote(texts: list[str], batch_size: int, embed_url: str) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Embed texts using a remote BGE-M3 API."""
    import requests

    api_base = embed_url.rstrip("/")
    health = requests.get(f"{api_base}/health", timeout=10)
    health.raise_for_status()
    logger.info("Embedding API health: %s", health.json())
    print(f"Embedding API: {api_base} ({health.json().get('device', 'unknown')})")

    all_embeddings: list[np.ndarray[Any, np.dtype[np.float32]]] = []
    for i in tqdm(range(0, len(texts), batch_size), desc="Embedding"):
        batch_texts = texts[i : i + batch_size]
        resp = requests.post(
            f"{api_base}/embed",
            json={
                "sentences": batch_texts,
                "return_dense": True,
                "return_sparse": False,
                "return_colbert_vecs": False,
                "max_length": 8192,
                "batch_size": len(batch_texts),
            },
            timeout=120,
        )
        resp.raise_for_status()
        all_embeddings.append(np.array(resp.json()["dense_vecs"], dtype=np.float32))

    return np.vstack(all_embeddings).astype(np.float32)


def embed_chunks(
    input_path: str | Path = "data/processed/chunks.json",
    output_path: str | Path = "data/processed/embeddings.npy",
    batch_size: int = DEFAULT_BATCH_SIZE,
    embed_url: str | None = None,
) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Read text chunks and generate BGE-M3 dense embeddings.

    Uses the local FlagEmbedding model by default. If embed_url is
    provided, calls the remote API instead.

    Args:
        input_path: Path to the chunks JSON file.
        output_path: Path to write the output numpy embeddings file.
        batch_size: Number of chunks to embed per batch.
        embed_url: If set, use this remote embedding API instead of the local model.

    Returns:
        Numpy array of shape (num_chunks, 1024).
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    with open(input_path, "r", encoding="utf-8") as f:
        chunks: list[dict[str, Any]] = json.load(f)

    num_chunks = len(chunks)
    logger.info("Loaded %d chunks from %s", num_chunks, input_path)
    print(f"Loaded {num_chunks} chunks from {input_path}")

    if num_chunks == 0:
        empty = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(output_path, empty)
        logger.info("No chunks to embed. Saved empty embeddings.")
        print("No chunks to embed.")
        return empty

    texts = [chunk["text"] for chunk in chunks]

    if embed_url:
        embeddings = _embed_remote(texts, batch_size, embed_url)
    else:
        embeddings = _embed_local(texts, batch_size)

    # Verify dimensions
    assert embeddings.shape == (num_chunks, EMBEDDING_DIM), (
        f"Expected shape ({num_chunks}, {EMBEDDING_DIM}), got {embeddings.shape}"
    )

    # Save embeddings
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(output_path, embeddings)

    logger.info(
        "Saved embeddings to %s: shape %s", output_path, embeddings.shape
    )
    print(f"Saved embeddings to {output_path}: shape {embeddings.shape}")

    return embeddings
