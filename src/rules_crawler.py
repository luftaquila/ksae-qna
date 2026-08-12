"""Crawler for 2026 rule-board (J_rule) PDFs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from src.rules_registry import classify_rule_document, is_draft_rule_document

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ksae.org"
LIST_URL = f"{BASE_URL}/jajak/bbs/"
RULES_CODE = "J_rule"
DEFAULT_DELAY = 1.5
DEFAULT_WORKERS = 5
MAX_RETRIES = 3
DEFAULT_YEAR = "2026"
PAGE_SIZE_HINT = "30"  # not used directly; for docs only


class _WeakDHAdapter(HTTPAdapter):
    """HTTPS adapter that allows weaker DH suites for ksae.org."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _new_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", _WeakDHAdapter())
    return session


def _extract_year_candidates(text: str) -> list[str]:
    return sorted(set(re.findall(r"20\d{2}", text)))


def _pick_best_year(text: str, default: str) -> str:
    years = _extract_year_candidates(text)
    return years[-1] if years else default


def _is_year_target(text: str, year: str = DEFAULT_YEAR) -> bool:
    return year in _extract_year_candidates(text)


def _safe_path_component(value: str, *, max_len: int = 120) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip().strip(".").strip()
    return (value[:max_len] or "rule").strip()


def _build_source_version(post_id: int, title: str, source_url: str, year: str) -> str:
    seed = f"{post_id}:{title}:{source_url}:{year}"
    return f"{year}-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


def _build_source_key(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def _parse_list_row(row: Tag, target_year: str = DEFAULT_YEAR) -> list[dict[str, Any]]:
    cells = row.find_all("td")
    if len(cells) < 5:
        return []

    title_cell = cells[1]
    title_link = title_cell.find("a", href=True)
    if not isinstance(title_link, Tag):
        return []

    href = str(title_link.get("href", "")).strip()
    if not href:
        return []

    params = parse_qs(urlparse(href).query)
    number_values = params.get("number", [])
    if not number_values:
        return []
    try:
        post_id = int(number_values[0])
    except ValueError:
        return []

    title = title_link.get_text(" ", strip=True)
    detail_url = urljoin(BASE_URL, href)
    date = cells[-1].get_text(" ", strip=True)

    file_cell = cells[2]
    download_links = [
        a
        for a in file_cell.find_all("a", href=True)
        if isinstance(a, Tag) and "func/download.php" in str(a["href"])
    ]
    if not download_links:
        return []

    items: list[dict[str, Any]] = []
    for link in download_links:
        source_url = urljoin(BASE_URL, str(link["href"]))
        q = parse_qs(urlparse(source_url).query)
        filename_raw = str(q.get("filename", [""])[0])

        source_filename = ""
        icon = link.find("img", src=True)
        if icon:
            alt = icon.get("alt", "")
            if alt:
                source_filename = str(alt)
        if not source_filename:
            if filename_raw:
                source_filename = filename_raw
            else:
                source_filename = os.path.basename(urlparse(source_url).path) or "rules-2026.pdf"

        if ".pdf" not in source_filename.lower():
            source_filename += ".pdf"

        classification_text = f"{title} {source_filename} {date}"
        if is_draft_rule_document(classification_text):
            logger.info(
                "Skip draft/안 document in J_rule: post=%s title=%s filename=%s",
                post_id,
                title,
                source_filename,
            )
            continue

        source_year = _pick_best_year(classification_text, target_year)
        competition, document_type = classify_rule_document(title, source_filename)
        source_key = _build_source_key(source_url)

        items.append({
            "source_post_id": post_id,
            "title": title,
            "date": date,
            "detail_url": detail_url,
            "source_url": source_url,
            "source_filename": source_filename,
            "competition": competition,
            "document_type": document_type,
            "year": source_year,
            "source_key": source_key,
            "source_version": _build_source_version(post_id, title, source_url, source_year),
        })

    return items


def _parse_list_page(soup: BeautifulSoup, target_year: str) -> list[dict[str, Any]]:
    rows = soup.find_all("tr")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Tag):
            continue
        result.extend(_parse_list_row(row, target_year=target_year))
    return result


def crawl_rules_list_pages(
    delay: float = DEFAULT_DELAY,
    year: str = DEFAULT_YEAR,
) -> list[dict[str, Any]]:
    """Crawl J_rule pages and collect target-year rule PDFs."""
    result: list[dict[str, Any]] = []
    page_num = 1
    consecutive_failures = 0
    seen_keys: set[str] = set()
    consecutive_non_target_pages = 0

    session = _new_session()
    while True:
        page_params = {
            "code": RULES_CODE,
            "page": str(page_num),
            "keyfield": "",
            "keyword": "",
            "category": "",
            "gubun": "",
        }

        try:
            resp = session.get(LIST_URL, params=page_params, timeout=30)
            resp.raise_for_status()
            response_text = resp.text
            consecutive_failures = 0
        except requests.RequestException as exc:  # pragma: no branch
            consecutive_failures += 1
            if consecutive_failures >= 3:
                logger.error("3 consecutive list-page failures. Stopping at page %s", page_num)
                break
            logger.warning("List page %s fetch failed: %s. Retry.", page_num, exc)
            import time
            time.sleep(delay * 2)
            continue

        soup = BeautifulSoup(response_text, "lxml")
        docs = _parse_list_page(soup, target_year=year)
        if not docs:
            if page_num > 1:
                break
            page_num += 1
            continue

        filtered: list[dict[str, Any]] = []
        for item in docs:
            text_for_filter = f"{item['title']} {item['source_filename']} {item['date']}"
            if not _is_year_target(text_for_filter, year):
                continue
            if item["source_key"] in seen_keys:
                continue
            seen_keys.add(item["source_key"])
            filtered.append(item)

        if not filtered:
            consecutive_non_target_pages += 1
        else:
            consecutive_non_target_pages = 0
            result.extend(filtered)

        if consecutive_non_target_pages >= 2 and page_num > 1:
            break

        page_num += 1

    logger.info("Discovered %d %s rule PDFs from J_rule", len(result), year)
    print(f"Discovered {len(result)} {year} rule PDFs from J_rule")
    return result


def _download_file(url: str, destination: Path, delay: float) -> bool:
    session = _new_session()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = session.get(url, stream=True, timeout=60)
        resp.raise_for_status()
        with open(destination, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    except requests.RequestException as exc:
        logger.warning("Download failed: %s (%s)", destination.name, exc)
        return False

    import time
    time.sleep(delay)
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_rules_pdfs(
    manifest: list[dict[str, Any]],
    raw_dir: str = "data/raw/rules-2026",
    delay: float = DEFAULT_DELAY,
) -> list[dict[str, Any]]:
    """Download all PDFs in manifest and inject local file metadata."""
    raw_dir_path = Path(raw_dir)
    raw_dir_path.mkdir(parents=True, exist_ok=True)
    updated: list[dict[str, Any]] = []

    for item in manifest:
        safe_name = _safe_path_component(f"{item['source_post_id']}_{item.get('source_filename', 'rule')}")
        if not safe_name.lower().endswith(".pdf"):
            safe_name += ".pdf"
        source_path = raw_dir_path / safe_name

        current = dict(item)
        current["source_file"] = str(source_path)
        if source_path.exists() and source_path.stat().st_size > 100:
            current["downloaded"] = True
            current["downloaded_size"] = source_path.stat().st_size
            current["source_sha256"] = _file_sha256(source_path)
            updated.append(current)
            continue

        ok = _download_file(current["source_url"], source_path, delay)
        current["downloaded"] = ok
        if ok and source_path.exists():
            current["downloaded_size"] = source_path.stat().st_size
            current["source_sha256"] = _file_sha256(source_path)
            current["source_file"] = str(source_path)
            logger.info("Downloaded %s", source_path.name)
        updated.append(current)

    return updated


def save_manifest(manifest: list[dict[str, Any]], path: str) -> None:
    """Write manifest JSON to disk."""
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with open(path_obj, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    logger.info("Saved manifest: %s (%d entries)", path, len(manifest))
    print(f"Saved manifest: {path} ({len(manifest)} entries)")


def load_manifest(path: str) -> list[dict[str, Any]]:
    """Load rule manifest JSON."""
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return manifest
