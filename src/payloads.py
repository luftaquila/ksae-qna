"""Qdrant payload schemas, one per search source.

These are the contract between the ingestion pipeline (``src/*_chunker.py`` ->
``src/uploader.py``) and retrieval (``src/chat.py``, ``mcp_server.py``).
Declaring them here keeps the schema next to the code instead of only in prose.

Discrimination order matters at retrieval time: the knowledge base payload also
carries ``chapter``, so ``source_type`` must be checked *before* falling back to
the ``title`` / ``chapter`` duck-typing used for the older two sources.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

Confidence = Literal["합의됨", "다수의견", "단일제보", "미해결"]


class BasePayload(TypedDict):
    """Fields every collection carries."""

    id: str | int          # source-level id; chunks of one item share it
    content: str           # embedded text
    chunk_index: int


class QnaPayload(BasePayload):
    """``ksae-qna`` — crawled Q&A board posts. Discriminated by ``title``."""

    category: str          # "Formula" | "Baja" | "EV" (keyword index)
    title: str
    author: str
    date: str
    url: str


class RulesPayload(BasePayload):
    """``ksae-formula-rules`` — competition rulebook. Discriminated by ``chapter``."""

    chapter: str
    chapter_num: int | str
    section: str
    section_num: NotRequired[int | str]


class KbPayload(BasePayload):
    """``ksae-aark-kb`` — AARK community knowledge base.

    Discriminated by ``source_type == "aark"``, which must be tested before
    ``chapter``. There is no ``url``: the source is an anonymous chat log, so
    ``dates`` is the only handle a reader has to cross-check a claim.
    """

    source_type: Literal["aark"]
    source_version: str    # "YYYY-MM-DD+<sha256[:12]>" of the source document
    chapter_num: int
    chapter: str
    section: str
    topic: str
    confidence: Confidence | Literal[""]   # "" for table/brief/note chunks
    dates: list[str]
    kind: Literal["topic", "table", "brief", "note"]
