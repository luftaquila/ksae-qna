"""Chunker module for Formula / 규정 PDFs and multi-category rule documents."""

from __future__ import annotations

import unicodedata
import hashlib
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from src.chunker import MAX_TOKENS as BASE_MAX_TOKENS
    from src.chunker import _token_count as _token_count
    try:
        from src.chunker import _tokenizer as _BASE_TOKENIZER
    except Exception:  # pragma: no cover
        _BASE_TOKENIZER = None
except Exception:  # pragma: no cover
    BASE_MAX_TOKENS = 384
    _BASE_TOKENIZER = None

    def _token_count(text: str) -> int:
        return len((text or "").strip().split())

from src.rules_registry import (
    classify_rule_document,
    document_type_label,
    infer_competition,
    infer_document_type,
    is_draft_rule_document,
    normalize_competition_key,
    normalize_document_type,
)

logger = logging.getLogger(__name__)

SOURCE_TYPE = "formula-rules"
DEFAULT_OUTPUT = "data/processed/rules_chunks.json"
DEFAULT_SOURCE = "data/raw/formula-2026-2026.pdf"
DEFAULT_MANIFEST = "data/raw/rules-2026-manifest.json"
DEFAULT_RULES_YEAR = "2026"

MAX_TOKENS = BASE_MAX_TOKENS
OVERLAP_TOKENS = 64

_CHAPTER_RE = re.compile(r"^\s*제\s*([0-9]+)\s*장\s*(.*)$")
_SECTION_RE = re.compile(r"^\s*제\s*([0-9]+)\s*조\s*(?:\(([^)]*)\))?\s*(.*)$")
_SUPPLEMENT_RE = re.compile(r"^\s*부칙\s*$")
_FOOTER_RE = re.compile(r"^\s*자동차공학은 한국의 힘!.*사단법인 한국자동차공학회\s*$")
_TITLE_RE = re.compile(r"^\s*한국자동차공학회 규정\s*$")
_PAGE_NUMBER_RE = re.compile(r"^\s*\d+\s*$")
_PAGE_GUTTER_RE = re.compile(r"^\s*사단법인 한국자동차공학회\s*$")
_RULE_TERM_PATTERNS = (
    (re.compile(r"\b제\s*(\d+)\s*조\b"), r"제 \1 조"),
    (re.compile(r"\b제\s*(\d+)\s*장\b"), r"제 \1 장"),
    (re.compile(r"\b경기\s*진행규정\b"), "경기진행규정"),
    (re.compile(r"\b대회\s*운영규정\b"), "대회운영규정"),
    (re.compile(r"\b차량\s*기술규정\b"), "차량기술규정"),
    (re.compile(r"\b심사\s*규정\b"), "심사규정"),
    (re.compile(r"\b안전\s*규정\b"), "안전규정"),
)


class _RuleSection:
    """One parsed formula-style section."""

    __slots__ = ("chapter_num", "chapter", "section_num", "section_title", "lines")

    def __init__(self, chapter_num: int, chapter: str, section_num: int, section_title: str) -> None:
        self.chapter_num = chapter_num
        self.chapter = chapter
        self.section_num = section_num
        self.section_title = section_title
        self.lines: list[str] = []


def _is_noisy_line(line: str) -> bool:
    if not line.strip():
        return True
    if _FOOTER_RE.match(line):
        return True
    if _TITLE_RE.match(line):
        return True
    if _PAGE_NUMBER_RE.match(line):
        return True
    if _PAGE_GUTTER_RE.match(line):
        return True
    return False


def _pdftotext_extract(pdf_path: Path) -> str:
    """Extract text from PDF using pdftotext."""
    cmd = ["pdftotext", "-layout", "-nopgbrk", str(pdf_path), "-"]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("pdftotext not found. Install poppler to run rule chunking.") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").strip() or "pdftotext returned non-zero exit code"
        raise RuntimeError(f"pdftotext failed: {msg}") from exc
    return proc.stdout


def _load_text(input_path: Path) -> tuple[str, bytes]:
    raw = input_path.read_bytes()
    if input_path.suffix.lower() == ".pdf":
        return _pdftotext_extract(input_path), raw
    return input_path.read_text(encoding="utf-8"), raw


def _clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        line = _normalize_rule_text(line)
        if _is_noisy_line(line):
            continue
        lines.append(line.strip())
    return lines


def _normalize_rule_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", (text or "").strip())
    if not text:
        return ""
    for pattern, repl in _RULE_TERM_PATTERNS:
        text = pattern.sub(repl, text)
    text = text.replace("\u200b", " ")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_section_title(title: str | None) -> str:
    title = (title or "").strip()
    if title:
        return title
    return "조문"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tokenizer_encode(text: str) -> list[int]:
    if _BASE_TOKENIZER is None:
        return []
    return _BASE_TOKENIZER.encode(text, add_special_tokens=False)


def _tokenizer_decode(token_ids: list[int]) -> str:
    if _BASE_TOKENIZER is None:
        return ""
    return _BASE_TOKENIZER.decode(token_ids, skip_special_tokens=True)


def _fit_to_budget(text: str, budget: int) -> list[str]:
    if budget <= 0:
        budget = MAX_TOKENS
    if _token_count(text) <= budget:
        return [text]

    token_ids = _tokenizer_encode(text)
    if not token_ids:
        return [_truncate_text(text)]

    step = max(1, budget - OVERLAP_TOKENS)
    return [_tokenizer_decode(token_ids[i : i + budget]).strip() for i in range(0, len(token_ids), step)]


def _chunk_tail(lines: list[str], max_tokens: int = OVERLAP_TOKENS) -> tuple[list[str], int]:
    if not lines or max_tokens <= 0:
        return [], 0
    tail: list[str] = []
    used = 0
    for line in reversed(lines):
        count = _token_count(line)
        if used + count > max_tokens:
            break
        tail.append(line)
        used += count
    tail.reverse()
    return tail, used


def _rule_header(source_meta: dict[str, Any], section_title: str | None = None) -> str:
    title = _normalize_rule_text(source_meta.get("source_title") or source_meta.get("source_filename") or source_meta.get("source_key", "규정"))
    competition = source_meta.get("competition") or "other"
    document_type = source_meta.get("document_type") or "other"
    document_type_name = document_type_label(document_type)
    section_part = f" - {section_title}" if section_title else ""
    return (
        f"[규정문서] {title}"
        f"\n[규정분류] {competition} / {document_type_name}{section_part}"
    )


def _truncate_text(text: str, max_chars: int = 4000) -> str:
    text = text.strip()
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _chunk_formula_section(section: _RuleSection, source_meta: dict[str, Any]) -> list[dict[str, Any]]:
    body = "\n".join(section.lines).strip()
    if not body:
        return []

    def section_id() -> str:
        digest = hashlib.sha1(
            f"{source_meta.get('source_key', '')}:{section.chapter_num}:{section.section_num}:{_normalize_section_title(section.section_title)}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{SOURCE_TYPE}-{digest}"

    header = (
        f"[제{section.chapter_num}장] {section.chapter}\n"
        f"[제{section.section_num}조] {section.section_title}\n"
        f"{_rule_header(source_meta, section.section_title)}"
    )
    full = f"{header}\n{body}"
    full_token_count = _token_count(full)
    if full_token_count <= MAX_TOKENS:
        return [
            {
                **_rules_payload_base(source_meta),
                "post_id": section_id(),
                "chapter": section.chapter,
                "chapter_num": section.chapter_num,
                "section": _normalize_section_title(section.section_title),
                "section_num": section.section_num,
                "chunk_index": 0,
                "text": full,
            }
        ]

    budget = MAX_TOKENS - _token_count(header) - 4
    if budget <= 0:
        budget = max(64, MAX_TOKENS // 2)

    segments: list[str] = []
    for line in section.lines:
        if not line:
            continue
        line_tokens = _token_count(line)
        if line_tokens <= budget:
            segments.append(line)
        else:
            segments.extend(_fit_to_budget(line, budget))

    chunks: list[dict[str, Any]] = []
    buf: list[str] = []
    token_count = 0
    tail: list[str] = []
    tail_tokens = 0
    chunk_index = 0
    for segment in segments:
        seg_count = _token_count(segment)
        if not segment:
            continue
        if buf and token_count + seg_count > budget:
            chunks.append(
                {
                    **_rules_payload_base(source_meta),
                    "post_id": section_id(),
                    "chapter": section.chapter,
                    "chapter_num": section.chapter_num,
                    "section": _normalize_section_title(section.section_title),
                    "section_num": section.section_num,
                    "chunk_index": chunk_index,
                    "text": f"{header}\n" + "\n".join(buf),
                }
            )
            chunk_index += 1
            tail, tail_tokens = _chunk_tail(buf)
            buf = tail
            token_count = tail_tokens

        if buf:
            buf.append(segment)
            token_count += seg_count
        else:
            buf = [segment]
            token_count = seg_count

    if buf:
        chunks.append(
            {
                **_rules_payload_base(source_meta),
                "post_id": section_id(),
                "chapter": section.chapter,
                "chapter_num": section.chapter_num,
                "section": _normalize_section_title(section.section_title),
                "section_num": section.section_num,
                "chunk_index": chunk_index,
                "text": f"{header}\n" + "\n".join(buf),
            }
        )
    return chunks


def _rules_payload_base(meta: dict[str, Any]) -> dict[str, Any]:
    title = (meta.get("title") or "").strip()
    source_version = meta.get("source_version") or _default_source_version(meta)
    competition = normalize_competition_key(meta.get("competition") or infer_competition(title))
    document_type = normalize_document_type(meta.get("document_type") or infer_document_type(title))
    return {
        "source_type": "rules",
        "source_post_id": meta.get("source_post_id", ""),
        "source_key": meta.get("source_key", ""),
        "source_version": source_version,
        "source_file": meta.get("source_file", ""),
        "source_filename": meta.get("source_filename", ""),
        "source_url": meta.get("source_url", ""),
        "source_title": title,
        "competition": competition,
        "document_type": document_type,
        "document_type_label": document_type_label(document_type),
        "year": str(meta.get("year", DEFAULT_RULES_YEAR)),
    }


def _default_source_version(meta: dict[str, Any]) -> str:
    source_key = meta.get("source_key", "")
    year = str(meta.get("year", DEFAULT_RULES_YEAR))
    return f"{year}+{source_key[:12]}"


def _segment_text(text: str) -> list[str]:
    paragraphs = re.split(r"\n\s*\n", text)
    segments: list[str] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if _token_count(para) <= MAX_TOKENS:
            segments.append(para)
            continue

        sentences = re.split(r"(?<=[.!?。])\s+|\n", para)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if _token_count(sentence) <= MAX_TOKENS:
                segments.append(sentence)
            else:
                tokens = _token_ids(sentence)
                if not tokens:
                    for i in range(0, len(sentence), 2400):
                        segments.append(sentence[i : i + 2400])
                    continue

                step = MAX_TOKENS - OVERLAP_TOKENS
                for i in range(0, len(tokens), step):
                    piece = _token_text(tokens[i : i + MAX_TOKENS])
                    if piece:
                        segments.append(piece)
                    if i + MAX_TOKENS >= len(tokens):
                        break
    return segments


def _token_ids(text: str) -> list[int]:
    ids = _tokenizer_encode(text)
    if ids:
        return ids
    # fallback deterministic path for environments where tokenizer is unavailable
    if _token_count(text) <= MAX_TOKENS:
        return []
    return []


def _token_text(token_ids: list[int]) -> str:
    if not token_ids:
        return ""
    text = _tokenizer_decode(token_ids)
    if text:
        return text
    return _truncate_text(_tokenizer_decode(token_ids[: MAX_TOKENS]))


def _build_generic_chunks(text: str, source_meta: dict[str, Any]) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []

    meta = _rules_payload_base(source_meta)
    source_key = meta["source_key"] or hashlib.sha256((meta["source_url"] + meta["source_file"]).encode("utf-8")).hexdigest()
    doc_title = meta["source_title"] or meta["source_filename"]
    competition = meta["competition"]
    document_type = meta["document_type"]
    header = _rule_header(meta)

    segments = _segment_text(text)
    chunks: list[dict[str, Any]] = []
    chunk_budget = MAX_TOKENS - _token_count(header) - 8
    if chunk_budget < 120:
        chunk_budget = 120

    current_segments: list[str] = []
    current_tokens = 0
    chunk_index = 0
    for segment in segments:
        if not segment:
            continue

        seg_tokens = _token_count(segment)
        if seg_tokens > chunk_budget:
            pieces = _fit_to_budget(segment, chunk_budget)
            if current_segments:
                chunk_text = _join_and_trim(current_segments)
                chunks.append({
                    **meta,
                    "post_id": source_key,
                    "chunk_index": chunk_index,
                    "text": f"{header}\n{chunk_text}",
                })
                chunk_index += 1
                current_segments = []
                current_tokens = 0

            if not pieces:
                pieces = [_truncate_text(segment)]
            for piece in pieces:
                chunks.append({
                    **meta,
                    "post_id": source_key,
                    "chunk_index": chunk_index,
                    "text": f"{header}\n{piece}",
                })
                chunk_index += 1
            continue

        if current_segments and current_tokens + seg_tokens > chunk_budget:
            chunks.append({
                **meta,
                "post_id": source_key,
                "chunk_index": chunk_index,
                "text": f"{header}\n{_join_and_trim(current_segments)}",
            })
            chunk_index += 1
            current_segments = []
            current_tokens = 0

        if current_segments:
            current_segments.append(segment)
            current_tokens += seg_tokens
        else:
            current_segments = [segment]
            current_tokens = seg_tokens

    if current_segments:
        chunks.append({
            **meta,
            "post_id": source_key,
            "chunk_index": chunk_index,
            "text": f"{header}\n{_join_and_trim(current_segments)}",
        })

    if not chunks:
        return []

    # Keep at least section-like fields for compatibility with downstream filters.
    for chunk in chunks:
        chunk.setdefault("chapter", "규정")
        chunk.setdefault("chapter_num", "")
        chunk.setdefault("section", "본문")
        chunk.setdefault("section_num", "")

    return chunks


def _join_and_trim(lines: list[str]) -> str:
    content = "\n".join(lines).strip()
    if not content:
        return ""
    if len(content) <= 20000:
        return content
    return content[:20000]


def _chunk_formula_text(input_path: Path, meta: dict[str, Any]) -> list[dict[str, Any]]:
    raw_text, raw_source = _load_text(input_path)
    source_version = f"{datetime.fromtimestamp(input_path.stat().st_mtime, tz=timezone.utc).strftime('%Y-%m-%d')}+{hashlib.sha256(raw_source).hexdigest()[:12]}"
    base_meta = dict(meta)
    base_meta["source_version"] = base_meta.get("source_version", source_version)

    lines = _clean_lines(raw_text)
    chapter_num = 0
    chapter = ""
    section_num = 0
    section_title = ""
    sections: list[_RuleSection] = []
    current: _RuleSection | None = None

    for line in lines:
        m = _CHAPTER_RE.match(line)
        if m:
            if current is not None:
                sections.append(current)
                current = None
            chapter_num = int(m.group(1))
            chapter = m.group(2).strip()
            section_num = 0
            section_title = ""
            continue

        m = _SECTION_RE.match(line)
        if m and chapter_num:
            if current is not None:
                sections.append(current)
            section_num = int(m.group(1))
            if m.group(2):
                section_title = f"{m.group(2).strip()}"
                if m.group(3).strip():
                    section_title = f"{section_title} - {m.group(3).strip()}"
            else:
                section_title = m.group(3).strip()
            section_title = _normalize_section_title(section_title)
            current = _RuleSection(chapter_num, chapter, section_num, section_title)
            continue

        if _SUPPLEMENT_RE.match(line):
            if current is not None:
                sections.append(current)
            section_num = 9999
            section_title = "부칙"
            current = _RuleSection(chapter_num, chapter, section_num, section_title)
            continue

        if current is not None and chapter_num:
            current.lines.append(line)

    if current is not None:
        sections.append(current)

    if not sections:
        return []

    chunks: list[dict[str, Any]] = []
    for section in sections:
        chunked = _chunk_formula_section(section, base_meta)
        chunks.extend(chunked)
    return chunks


def chunk_rules(
    input_path: str | Path = DEFAULT_SOURCE,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> list[dict[str, Any]]:
    """Parse Formula Student Korea 규정 PDF and split by 장/조."""
    input_path = Path(input_path)
    output_path = Path(output_path)

    source_meta = {
        "source_post_id": input_path.name,
        "title": input_path.name,
        "source_url": "",
        "source_file": str(input_path),
        "source_filename": input_path.name,
        "competition": "formula",
        "document_type": "vehicle-technical",
        "year": DEFAULT_RULES_YEAR,
    }

    chunks = _chunk_formula_text(input_path, source_meta)
    if not chunks:
        logger.warning("No sections matched formula parser; fallback to generic chunking")
        raw_text, _raw = _load_text(input_path)
        chunks = _build_generic_chunks(raw_text, source_meta)

    chunks.sort(
        key=lambda c: (
            _safe_int(c.get("chapter_num", 0)),
            _safe_int(c.get("section_num", 0)),
            _safe_int(c.get("chunk_index", 0)),
        )
    )
    _write_chunks(chunks, output_path)
    _print_chunk_stats(chunks)
    return chunks


def chunk_rules_manifest(
    manifest_path: str | Path = DEFAULT_MANIFEST,
    output_path: str | Path = DEFAULT_OUTPUT,
    year: str = DEFAULT_RULES_YEAR,
) -> list[dict[str, Any]]:
    """Chunk all rule PDFs listed in a J_rule manifest."""
    manifest_path = Path(manifest_path)
    output_path = Path(output_path)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    chunks: list[dict[str, Any]] = []
    for item in manifest:
        item_year = str(item.get("year", DEFAULT_RULES_YEAR))
        if item_year != str(year):
            continue

        source_file = item.get("source_file")
        if not source_file:
            continue

        path = Path(source_file)
        if not path.exists() or not path.suffix.lower().endswith(".pdf"):
            logger.warning("Missing or non-PDF source, skipped: %s", source_file)
            continue

        meta = dict(item)
        title = (item.get("title") or "").strip()
        source_filename = (item.get("source_filename") or "").strip()
        draft_text = f"{title} {source_filename} {item.get('date', '')}"
        if is_draft_rule_document(draft_text):
            logger.info("Skip draft/안 rule in chunking: %s", source_filename or title)
            continue

        meta["source_title"] = title
        meta["competition"] = normalize_competition_key(item.get("competition") or infer_competition(title))
        meta["document_type"] = normalize_document_type(item.get("document_type") or infer_document_type(title))

        if source_filename and "pdf" not in source_filename.lower():
            meta["source_filename"] = f"{source_filename}.pdf"
        else:
            meta["source_filename"] = source_filename or path.name

        formula_chunks = _chunk_formula_text(path, meta)
        if formula_chunks:
            chunks.extend(formula_chunks)
            continue

        raw_text, _ = _load_text(path)
        chunks.extend(_build_generic_chunks(raw_text, meta))

    chunks.sort(
        key=lambda c: (
            str(c.get("competition", "")),
            str(c.get("document_type", "")),
            str(c.get("source_key", "")),
            int(_safe_int(c.get("chunk_index", 0))),
        )
    )
    _write_chunks(chunks, output_path)
    _print_chunk_stats(chunks)
    return chunks


def _write_chunks(chunks: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)


def _print_chunk_stats(chunks: list[dict[str, Any]]) -> None:
    if not chunks:
        print("No chunks generated.")
        return

    lengths = [_token_count(chunk["text"]) for chunk in chunks]
    print(f"Total chunks: {len(chunks)}")
    print(f"Avg token length: {sum(lengths) / len(lengths):.1f}")
    print(f"Min token length: {min(lengths)}")
    print(f"Max token length: {max(lengths)}")
