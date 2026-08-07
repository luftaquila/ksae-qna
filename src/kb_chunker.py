"""Chunker module for the AARK knowledge base document.

Splits the topic-organized AARK knowledge base Markdown (built from 18 months
of anonymous KakaoTalk logs) into RAG-optimized chunks.

Unlike the Q&A crawler output, this source is a single structured document:

    ## 1. 프레임·섀시 제작          -> chapter
    ### 파이프 소재·규격 선정        -> section
    - **주제명** `[다수의견]`        -> topic block (nested children = the answer)
      - **결론** — ...
      - <sub>출처: 2025-02-08</sub>
    #### 규격 정리                  -> table block
    | ... |
    **단편 정보**                   -> brief list block
    - 짧은 항목 `[단일제보]` (2025-05-22)

Each block becomes one chunk carrying its chapter/section path as context, so a
retrieved chunk is self-contained without needing neighbouring text.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

# Reuse the BGE-M3 tokenizer and segmentation logic from the Q&A chunker so
# both pipelines measure token budgets identically (and load the tokenizer once).
from src.chunker import MAX_TOKENS, _split_into_segments, _token_count, _tokenizer

logger = logging.getLogger(__name__)

SOURCE_TYPE = "aark"
CONFIDENCE_VALUES = ("합의됨", "다수의견", "단일제보", "미해결")

_CHAPTER_RE = re.compile(r"^##\s+(?:(\d+)\.\s*)?(.+?)\s*$")
_SECTION_RE = re.compile(r"^###\s+(.+?)\s*$")
_SUBHEAD_RE = re.compile(r"^####\s+(.+?)\s*$")
_BOLD_ONLY_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
_TOPIC_RE = re.compile(r"^-\s+(.*?)\s*$")
_NESTED_RE = re.compile(r"^\s+-\s+(.*?)\s*$")
_CONF_RE = re.compile(r"`\[(" + "|".join(CONFIDENCE_VALUES) + r")\]`")
_SUB_RE = re.compile(r"<sub>\s*(?:출처:\s*)?(.*?)\s*</sub>")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _strip_markup(text: str) -> str:
    """Remove bold markers, inline-code backticks and <sub> tags.

    Emphasis markup costs tokens without helping retrieval, and the raw
    ``<sub>`` tag would be read as content by the LLM.
    """
    text = _SUB_RE.sub(r"출처: \1", text)
    # Render confidence uniformly as (다수의견); the bracket/backtick form is
    # markup noise once the value is also lifted into the payload.
    text = _CONF_RE.sub(r"(\1)", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def _label_line(line: str) -> str:
    """Normalize a nested label line into ``라벨: 내용`` form.

    ``- **결론** — 노드락 와셔가 표준`` -> ``결론: 노드락 와셔가 표준``
    """
    body = _strip_markup(line)
    m = re.match(r"^(.{1,14}?)\s+—\s+(.*)$", body, re.S)
    if m:
        return f"{m.group(1)}: {m.group(2)}"
    return body


CAPTION_MAX_CHARS = 40


def _table_caption(lines: list[str], table_start: int) -> str:
    """Recover the caption of a table nested under a list item.

    Looks back past blank lines for a short bullet with no ``—`` body — that
    is a caption (``- **중량 사례표**``), not a knowledge item.
    """
    j = table_start - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0:
        return ""
    m = re.match(r"^\s*-\s+(.*?)\s*$", lines[j])
    if not m:
        return ""
    text = _strip_markup(_CONF_RE.sub("", m.group(1)))
    if not text or "—" in text or len(text) > CAPTION_MAX_CHARS:
        return ""
    return text


class _Block:
    """One parsed source block, before token-budget splitting."""

    __slots__ = ("kind", "chapter_num", "chapter", "section", "topic",
                 "confidence", "dates", "lines")

    def __init__(self, kind: str, chapter_num: int | None, chapter: str,
                 section: str, topic: str) -> None:
        self.kind = kind
        self.chapter_num = chapter_num
        self.chapter = chapter
        self.section = section
        self.topic = topic
        self.confidence = ""
        self.dates: list[str] = []
        self.lines: list[str] = []


def _parse_blocks(md: str) -> list[_Block]:
    """Parse the knowledge base Markdown into retrieval blocks.

    A top-level bullet with nested children is a *topic* (a full answer).
    Consecutive top-level bullets without children are grouped into one
    *brief* block. ``####`` headings and tables become *table* blocks, and
    stray prose or blockquotes under a section become *note* blocks.
    """
    lines = md.split("\n")
    blocks: list[_Block] = []
    chapter_num: int | None = None
    chapter = ""
    section = ""
    subhead = ""
    current: _Block | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and any(l.strip() for l in current.lines):
            blocks.append(current)
        current = None

    def start(kind: str, topic: str) -> _Block:
        flush()
        return _Block(kind, chapter_num, chapter, section, topic)

    i = 0
    seen_chapter = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        m = _CHAPTER_RE.match(line)
        if m and line.startswith("## ") and not line.startswith("###"):
            flush()
            chapter_num = int(m.group(1)) if m.group(1) else None
            chapter = m.group(2)
            section = ""
            subhead = ""
            seen_chapter = True
            i += 1
            continue

        if not seen_chapter:
            # Document title and preamble carry no retrievable knowledge.
            i += 1
            continue

        m = _SECTION_RE.match(line)
        if m:
            flush()
            section = m.group(1)
            subhead = ""
            i += 1
            continue

        m = _SUBHEAD_RE.match(line)
        if m:
            flush()
            subhead = m.group(1)
            i += 1
            continue

        m = _BOLD_ONLY_RE.match(line)
        if m:
            flush()
            subhead = m.group(1)
            i += 1
            continue

        if stripped.startswith("|"):
            if current is None or current.kind != "table":
                # Tables nested under a list item carry their caption in the
                # preceding bullet, not in a heading. Recover it so the table
                # is not left with a stale (or empty) title.
                current = start("table", _table_caption(lines, i) or subhead)
            current.lines.append(stripped)
            i += 1
            continue

        m = _TOPIC_RE.match(line)
        if m and not line.startswith("  "):
            has_children = i + 1 < len(lines) and bool(_NESTED_RE.match(lines[i + 1]))
            if has_children:
                head = m.group(1)
                conf = _CONF_RE.search(head)
                # Only the primary marker is lifted into the payload; a second
                # one ("→ `[미해결]` 병기") is annotation and stays in the title.
                current = start("topic", _strip_markup(_CONF_RE.sub("", head, count=1)))
                current.confidence = conf.group(1) if conf else ""
                i += 1
                while i < len(lines) and _NESTED_RE.match(lines[i]):
                    child = _NESTED_RE.match(lines[i]).group(1)  # type: ignore[union-attr]
                    current.dates.extend(_DATE_RE.findall(child))
                    if not current.confidence:
                        c = _CONF_RE.search(child)
                        if c:
                            current.confidence = c.group(1)
                    current.lines.append(_label_line(child))
                    i += 1
                flush()
                continue
            # A childless bullet immediately before a table is that table's
            # caption, not a knowledge item — emitting it alone would create a
            # junk chunk whose whole content is the caption text.
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith("|"):
                i += 1
                continue

            # Bullet with no children: part of a brief/reference list.
            if current is None or current.kind != "brief":
                current = start("brief", subhead)
            current.dates.extend(_DATE_RE.findall(stripped))
            current.lines.append(_strip_markup(m.group(1)))
            i += 1
            continue

        if stripped:
            if current is None or current.kind not in ("note", "table"):
                current = start("note", subhead)
            current.dates.extend(_DATE_RE.findall(stripped))
            current.lines.append(_strip_markup(stripped.lstrip("> ")))
            i += 1
            continue

        # Blank line ends brief/note runs but keeps tables joined to their caption.
        if current is not None and current.kind in ("brief", "note"):
            flush()
        i += 1

    flush()
    return blocks


def _fit_to_budget(text: str, budget: int) -> list[str]:
    """Force-split *text* into token windows no larger than *budget*.

    ``_split_into_segments`` caps segments at MAX_TOKENS, but a repeated
    header shrinks the per-chunk budget below that, so a final pass is needed.
    """
    if budget <= 0:
        return [text]
    if _token_count(text) <= budget:
        return [text]
    ids = _tokenizer.encode(text, add_special_tokens=False)
    return [
        _tokenizer.decode(ids[i : i + budget], skip_special_tokens=True)
        for i in range(0, len(ids), budget)
    ]


def _block_header(block: _Block) -> str:
    """Build the context prefix embedded with every chunk of a block."""
    path = f"{block.chapter_num}. {block.chapter}" if block.chapter_num else block.chapter
    if block.section:
        path += f" > {block.section}"
    header = f"[분야] {path}"
    if block.topic:
        title = block.topic
        if block.confidence:
            title += f" ({block.confidence})"
        header += f"\n[주제] {title}"
    return header


def _block_to_chunks(block: _Block, post_id: str) -> list[dict[str, Any]]:
    """Render a block to one or more chunks within the token budget."""
    header = _block_header(block)
    body = "\n".join(block.lines).strip()

    def make(text: str, index: int) -> dict[str, Any]:
        return {
            "post_id": post_id,
            "source_type": SOURCE_TYPE,
            "chapter_num": block.chapter_num or 0,
            "chapter": block.chapter,
            "section": block.section,
            "topic": block.topic,
            "confidence": block.confidence,
            "dates": sorted(set(block.dates)),
            "kind": block.kind,
            "chunk_index": index,
            "text": text,
        }

    full = f"{header}\n{body}"
    if _token_count(full) <= MAX_TOKENS:
        return [make(full, 0)]

    # Too long: split the body and repeat the header on every chunk so each
    # chunk stays self-contained after retrieval.
    prefix = header
    if block.kind == "table":
        # Carry the column header and separator rows into every chunk,
        # otherwise continuation rows lose their meaning.
        prefix = "\n".join([header] + block.lines[:2])
        raw = block.lines[2:]
    else:
        raw = block.lines

    budget = MAX_TOKENS - _token_count(prefix) - 4

    # Split on line boundaries first: every line is one labelled statement
    # (``결론: ...``) or one table row, and cutting inside one would orphan
    # its label. Only an over-long line falls back to sentence/token splitting.
    segments: list[str] = []
    for line in raw:
        if _token_count(line) <= budget:
            segments.append(line)
            continue
        for piece in _split_into_segments(line):
            segments.extend(_fit_to_budget(piece, budget))

    chunks: list[dict[str, Any]] = []
    buf: list[str] = []
    count = 0
    for segment in segments:
        seg_count = _token_count(segment)
        if buf and count + seg_count > budget:
            chunks.append(make(f"{prefix}\n" + "\n".join(buf), len(chunks)))
            buf, count = [], 0
        buf.append(segment)
        count += seg_count
    if buf:
        chunks.append(make(f"{prefix}\n" + "\n".join(buf), len(chunks)))
    return chunks


def _post_id(block: _Block, seen: dict[str, int]) -> str:
    """Build a content-derived, position-independent id for a block.

    A running block index would be simpler, but then inserting or removing one
    item anywhere in the document reshuffles every id after it — a single edit
    orphaned 268 points on the first re-ingest. Hashing the
    chapter/section/topic path keeps ids stable across unrelated edits; a
    counter disambiguates blocks that genuinely share a path.
    """
    path = f"{block.chapter}|{block.section}|{block.topic}|{block.kind}"
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
    seen[digest] = seen.get(digest, -1) + 1
    suffix = f"-{seen[digest]}" if seen[digest] else ""
    return f"{SOURCE_TYPE}-{block.chapter_num or 0:02d}-{digest}{suffix}"


def chunk_kb(
    input_path: str | Path = "data/raw/aark-kb.md",
    output_path: str | Path = "data/processed/kb_chunks.json",
) -> list[dict[str, Any]]:
    """Read the knowledge base Markdown and split it into RAG chunks.

    Args:
        input_path: Path to the knowledge base Markdown file.
        output_path: Path to write the output chunks JSON file.

    Returns:
        List of chunk dicts.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    raw = input_path.read_bytes()
    md = raw.decode("utf-8")
    # 계보 추적용 지문. 어느 문서 버전이 이 컬렉션을 만들었는지 payload에 남는다.
    mtime = datetime.datetime.fromtimestamp(
        input_path.stat().st_mtime, datetime.timezone.utc
    ).strftime("%Y-%m-%d")
    source_version = f"{mtime}+{hashlib.sha256(raw).hexdigest()[:12]}"
    logger.info("Source version: %s", source_version)

    blocks = _parse_blocks(md)
    logger.info("Parsed %d blocks from %s", len(blocks), input_path)

    all_chunks: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for block in blocks:
        post_id = _post_id(block, seen_ids)
        for chunk in _block_to_chunks(block, post_id):
            chunk["source_version"] = source_version
            all_chunks.append(chunk)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    lengths = [_token_count(c["text"]) for c in all_chunks]
    kinds: dict[str, int] = {}
    for c in all_chunks:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    avg = sum(lengths) / len(lengths) if lengths else 0

    logger.info("KB chunking complete: %d blocks -> %d chunks", len(blocks), len(all_chunks))
    print(f"Source version: {source_version}")
    print(f"Blocks: {len(blocks)}")
    print(f"Total chunks: {len(all_chunks)}  ({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})")
    print(f"Avg token length: {avg:.1f}")
    print(f"Min token length: {min(lengths) if lengths else 0}")
    print(f"Max token length: {max(lengths) if lengths else 0}")

    return all_chunks
