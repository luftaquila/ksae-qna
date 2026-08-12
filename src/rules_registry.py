"""Shared constants and helpers for multi-category rule ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass


def _slug(value: str) -> str:
    """Normalize a label to a stable id fragment."""
    value = value.lower().strip().replace(" ", "-").replace("_", "-")
    value = re.sub(r"[^a-z0-9가-힣-]", "-", value)
    return re.sub(r"-+", "-", value).strip("-")


COMPETITION_INTERNAL = {
    "formula": "Formula",
    "e_formula": "Formula",
    "smart_e_mobility": "EV",
    "baja": "Baja",
    "ev": "EV",
    "other": "기타",
}

COMPETITION_DISPLAY_BY_KEY = {
    "formula": "Formula",
    "e_formula": "Formula",
    "smart_e_mobility": "EV",
    "baja": "Baja",
    "ev": "EV",
    "other": "기타",
}

COMPETITION_KEYS = tuple(COMPETITION_INTERNAL.keys())

DOCUMENT_TYPES = (
    "vehicle-technical",
    "competition-rules",
    "judging",
    "event-operation",
    "safety",
    "other",
)

DOCUMENT_TYPE_LABEL = {
    "vehicle-technical": "차량기술규정",
    "competition-rules": "경기/대회규정",
    "judging": "심사규정",
    "event-operation": "경기진행",
    "safety": "안전규정",
    "other": "기타",
}

RULES_COLLECTION_PREFIX = "ksae-rules"
RULES_COLLECTION_SUFFIX = "v2"
RULES_COLLECTION_YEAR = "2026"

_COMPETITION_PATTERNS = (
    ("smart_e_mobility", re.compile(r"스마트\s*e[- ]?모빌리티|smart[- ]?e[- ]?mobility|smart[- ]?e", re.IGNORECASE)),
    ("e_formula", re.compile(r"e[- ]?formula|e[- ]?포뮬러|전기\s*포뮬러", re.IGNORECASE)),
    ("baja", re.compile(r"\bbaja\b|바하", re.IGNORECASE)),
    ("formula", re.compile(r"포뮬러|formula|포뮬라", re.IGNORECASE)),
    ("ev", re.compile(r"\bev\b|전기차|전기\s*모빌리티|EV", re.IGNORECASE)),
)

_DOCUMENT_TYPE_PATTERNS = (
    ("vehicle-technical", re.compile(r"차량기술규정|차량기술|차량.*규정|vehicle.*technical", re.IGNORECASE)),
    ("competition-rules", re.compile(r"대회운영규정|대회규정|발표대회규정|competition.*rule|operating.*rule|운영.*규정", re.IGNORECASE)),
    ("event-operation", re.compile(r"경기진행규정|경기진행|진행규정|event.*operation|operations?"),),
    ("judging", re.compile(r"심사규정|심사|채점|judge|심사기준", re.IGNORECASE)),
    ("safety", re.compile(r"안전규정|안전", re.IGNORECASE)),
)

_DRAFT_RULE_PATTERNS = (
    re.compile(r"제정\s*\(?\s*안\s*\)?", re.IGNORECASE),
    re.compile(r"제정안", re.IGNORECASE),
    re.compile(r"개정\s*\(?\s*안\s*\)?", re.IGNORECASE),
    re.compile(r"개정안", re.IGNORECASE),
    re.compile(r"규정안", re.IGNORECASE),
    re.compile(r"안\s*제정안", re.IGNORECASE),
)


@dataclass(frozen=True)
class RulesCollectionInfo:
    key: str
    collection: str
    competition: str
    competition_display: str
    document_type: str
    document_type_label: str
    label: str
    description: str


def normalize_competition_key(value: str | None) -> str:
    """Normalize a competition text into an internal key."""
    if not value:
        return "other"
    raw = value.strip().lower()
    raw = re.sub(r"\s+", "_", raw)
    if raw in COMPETITION_INTERNAL:
        return raw
    if raw in ("eformula", "e-formula", "e formula", "e_formula"):
        return "e_formula"
    if raw in ("smart_emobility", "smart-emobility", "smart e mobility", "ev"):
        return "smart_e_mobility"
    if "formula" in raw:
        return "formula"
    if "baja" in raw:
        return "baja"
    return "other"


def infer_competition(value: str | None) -> str:
    """Infer competition from a title/filename/body."""
    if not value:
        return "other"
    for key, pattern in _COMPETITION_PATTERNS:
        if pattern.search(value):
            return key
    return "other"


def competition_display(key: str) -> str:
    """Return UI-facing label for collection metadata."""
    return COMPETITION_DISPLAY_BY_KEY.get(key, "기타")


def normalize_document_type(value: str | None) -> str:
    """Normalize a document type text into canonical key."""
    if not value:
        return "other"
    key = value.strip().lower().replace(" ", "-")
    if key in DOCUMENT_TYPES:
        return key
    return "other"


def infer_document_type(value: str | None) -> str:
    """Infer document type from a title/filename/body."""
    if not value:
        return "other"
    for key, pattern in _DOCUMENT_TYPE_PATTERNS:
        if pattern.search(value):
            return key
    return "other"


def is_draft_rule_document(value: str | None) -> bool:
    """Return True for 규정 제정안/개정안-like documents."""
    if not value:
        return False
    return any(pattern.search(value) for pattern in _DRAFT_RULE_PATTERNS)


def document_type_label(key: str) -> str:
    """Return display label for a document type key."""
    return DOCUMENT_TYPE_LABEL.get(key, "기타")


def build_collection_name(
    competition: str,
    document_type: str,
    year: str = RULES_COLLECTION_YEAR,
    prefix: str = RULES_COLLECTION_PREFIX,
    suffix: str = RULES_COLLECTION_SUFFIX,
) -> str:
    """Build a concrete Qdrant collection for a competition × document type pair."""
    competition_key = _slug(competition)
    document_type_key = _slug(document_type)
    return f"{prefix}-{competition_key}-{document_type_key}-{year}-{suffix}"


def build_collection_key(
    competition: str,
    document_type: str,
    year: str = RULES_COLLECTION_YEAR,
) -> str:
    """Build a stable collection registry key."""
    return f"rules-{_slug(competition)}-{_slug(document_type)}-{year}"


def rules_collection_registry(
    year: str = RULES_COLLECTION_YEAR,
    prefix: str = RULES_COLLECTION_PREFIX,
    suffix: str = RULES_COLLECTION_SUFFIX,
) -> dict[str, RulesCollectionInfo]:
    """Build the full set of rule collections for one year."""
    registry: dict[str, RulesCollectionInfo] = {}
    for competition in COMPETITION_KEYS:
        competition_display_label = COMPETITION_DISPLAY_BY_KEY.get(competition, "기타")
        for document_type in DOCUMENT_TYPES:
            key = build_collection_key(competition, document_type, year)
            collection = build_collection_name(competition, document_type, year, prefix, suffix)
            dt_label = DOCUMENT_TYPE_LABEL[document_type]
            label = f"규정 {competition_display_label} - {dt_label} ({year})"
            description = (
                f"{competition_display_label} 종목 {dt_label} 문서 집합 ("
                f"{year} 규정 기준)."
            )
            registry[key] = RulesCollectionInfo(
                key=key,
                collection=collection,
                competition=competition,
                competition_display=competition_display_label,
                document_type=document_type,
                document_type_label=dt_label,
                label=label,
                description=description,
            )
    return registry


def classify_rule_document(title: str | None, filename: str | None = None) -> tuple[str, str]:
    """Infer competition + document type from title/filename context."""
    text = " ".join(filter(None, [title, filename]))
    competition = infer_competition(text)
    document_type = infer_document_type(text)
    return competition, document_type
