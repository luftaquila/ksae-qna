"""Embedder module for BGE-M3 vector generation.

Loads the BAAI/bge-m3 model and generates dense embedding
vectors (1024 dimensions) for each text chunk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from FlagEmbedding import BGEM3FlagModel  # type: ignore[import-untyped]
from tqdm import tqdm

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024
DEFAULT_BATCH_SIZE = 32


def embed_chunks(
    input_path: str | Path = "data/processed/chunks.json",
    output_path: str | Path = "data/processed/embeddings.npy",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Read text chunks and generate BGE-M3 dense embeddings.

    Loads the BAAI/bge-m3 model, processes chunks in batches,
    and saves the resulting embedding matrix as a numpy file.

    Args:
        input_path: Path to the chunks JSON file.
        output_path: Path to write the output numpy embeddings file.
        batch_size: Number of chunks to embed per batch.

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

    # Load BGE-M3 model (auto-detects GPU: CUDA > MPS > CPU)
    print("Loading BGE-M3 model...")
    logger.info("Loading BGE-M3 model...")
    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
    print("Model loaded.")
    logger.info("Model loaded.")

    texts = [chunk["text"] for chunk in chunks]

    # Process in batches with progress bar
    all_embeddings: list[np.ndarray[Any, np.dtype[np.float32]]] = []
    for i in tqdm(range(0, num_chunks, batch_size), desc="Embedding"):
        batch_texts = texts[i : i + batch_size]
        output = model.encode(
            batch_texts,
            batch_size=len(batch_texts),
            max_length=8192,
        )
        dense_vecs: np.ndarray[Any, np.dtype[np.float32]] = output["dense_vecs"]
        all_embeddings.append(dense_vecs)

    embeddings = np.vstack(all_embeddings).astype(np.float32)

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
