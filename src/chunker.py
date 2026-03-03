"""Chunker module for RAG text segmentation.

Splits crawled Q&A posts into optimally-sized chunks for
embedding and retrieval, preserving metadata for each chunk.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_TOKENS = 512
OVERLAP_TOKENS = 50


def _tokenize(text: str) -> list[str]:
    """Simple whitespace tokenizer for rough token counting."""
    return text.split()


def _token_count(text: str) -> int:
    """Count tokens using whitespace splitting."""
    return len(_tokenize(text))


def _force_split_by_tokens(text: str) -> list[str]:
    """Force-split text exceeding MAX_TOKENS into overlapping token windows."""
    tokens = _tokenize(text)
    step = MAX_TOKENS - OVERLAP_TOKENS
    segments: list[str] = []
    for i in range(0, len(tokens), step):
        window = tokens[i : i + MAX_TOKENS]
        segments.append(" ".join(window))
        if i + MAX_TOKENS >= len(tokens):
            break
    return segments


def _split_into_segments(text: str) -> list[str]:
    """Split text into paragraph/sentence segments for chunking.

    First splits by paragraphs (double newlines), then if a paragraph
    is still too large, splits by sentences. Segments still exceeding
    MAX_TOKENS are force-split by token windows.
    """
    # Split by double newlines (paragraphs)
    paragraphs = re.split(r"\n\s*\n", text)
    segments: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if _token_count(para) <= MAX_TOKENS:
            segments.append(para)
        else:
            # Split long paragraphs by sentence boundaries
            sentences = re.split(r"(?<=[.!?。])\s+", para)
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                if _token_count(sent) <= MAX_TOKENS:
                    segments.append(sent)
                else:
                    # Force-split sentences still exceeding MAX_TOKENS
                    segments.extend(_force_split_by_tokens(sent))
    return segments


def _build_chunks_from_segments(
    segments: list[str],
    post_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build overlapping chunks from text segments.

    Greedily accumulates segments into chunks of up to MAX_TOKENS,
    then applies OVERLAP_TOKENS overlap from the end of the previous chunk.
    """
    chunks: list[dict[str, Any]] = []
    chunk_index = 0
    current_tokens: list[str] = []

    for segment in segments:
        seg_tokens = _tokenize(segment)
        if current_tokens and len(current_tokens) + len(seg_tokens) > MAX_TOKENS:
            # Emit current chunk
            chunk_text = " ".join(current_tokens)
            chunks.append({
                "post_id": post_meta["id"],
                "category": post_meta["category"],
                "title": post_meta["title"],
                "date": post_meta["date"],
                "url": post_meta["url"],
                "chunk_index": chunk_index,
                "text": chunk_text,
            })
            chunk_index += 1
            # Start next chunk with overlap from the tail of current tokens
            overlap = current_tokens[-OVERLAP_TOKENS:] if len(current_tokens) >= OVERLAP_TOKENS else current_tokens[:]
            current_tokens = overlap + seg_tokens
        else:
            current_tokens.extend(seg_tokens)

    # Emit remaining tokens as the last chunk
    if current_tokens:
        chunk_text = " ".join(current_tokens)
        chunks.append({
            "post_id": post_meta["id"],
            "category": post_meta["category"],
            "title": post_meta["title"],
            "date": post_meta["date"],
            "url": post_meta["url"],
            "chunk_index": chunk_index,
            "text": chunk_text,
        })

    return chunks


def chunk_posts(
    input_path: str | Path = "data/raw/posts.json",
    output_path: str | Path = "data/processed/chunks.json",
) -> list[dict[str, Any]]:
    """Read crawled posts and split into RAG-optimized chunks.

    For each post, combines question title + question body + answer body.
    If the combined text fits within MAX_TOKENS, it becomes a single chunk.
    Otherwise, it is split at paragraph/sentence boundaries with overlap.

    Args:
        input_path: Path to the crawled posts JSON file.
        output_path: Path to write the output chunks JSON file.

    Returns:
        List of chunk dicts.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    with open(input_path, "r", encoding="utf-8") as f:
        posts: list[dict[str, Any]] = json.load(f)

    logger.info("Loaded %d posts from %s", len(posts), input_path)

    all_chunks: list[dict[str, Any]] = []

    for post in posts:
        # Combine title + question + answer into one document
        parts: list[str] = []
        if post.get("title"):
            parts.append(post["title"])
        if post.get("question_body"):
            parts.append(post["question_body"])
        if post.get("answer_body"):
            parts.append(post["answer_body"])

        combined = "\n\n".join(parts)

        if not combined.strip():
            logger.warning("Post %s has no text content, skipping", post.get("id"))
            continue

        post_meta = {
            "id": post["id"],
            "category": post["category"],
            "title": post["title"],
            "date": post["date"],
            "url": post["url"],
        }

        token_count = _token_count(combined)
        if token_count <= MAX_TOKENS:
            # Single chunk for the whole post
            all_chunks.append({
                "post_id": post_meta["id"],
                "category": post_meta["category"],
                "title": post_meta["title"],
                "date": post_meta["date"],
                "url": post_meta["url"],
                "chunk_index": 0,
                "text": combined,
            })
        else:
            # Split into multiple chunks
            segments = _split_into_segments(combined)
            post_chunks = _build_chunks_from_segments(segments, post_meta)
            all_chunks.extend(post_chunks)

    # Save chunks
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    # Print statistics
    chunk_token_lengths = [_token_count(c["text"]) for c in all_chunks]
    if chunk_token_lengths:
        avg_len = sum(chunk_token_lengths) / len(chunk_token_lengths)
        min_len = min(chunk_token_lengths)
        max_len = max(chunk_token_lengths)
    else:
        avg_len = min_len = max_len = 0

    logger.info("Chunking complete:")
    logger.info("  Total chunks: %d", len(all_chunks))
    logger.info("  Avg token length: %.1f", avg_len)
    logger.info("  Min token length: %d", min_len)
    logger.info("  Max token length: %d", max_len)

    print(f"Total chunks: {len(all_chunks)}")
    print(f"Avg token length: {avg_len:.1f}")
    print(f"Min token length: {min_len}")
    print(f"Max token length: {max_len}")

    return all_chunks
