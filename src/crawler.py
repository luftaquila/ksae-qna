"""Crawler module for KSAE Q&A board.

Crawls the KSAE Q&A board list pages and detail pages to collect
post metadata, question/answer bodies, and comments.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ksae.org"
LIST_URL = f"{BASE_URL}/jajak/bbs/"
DEFAULT_DELAY = 1.5
MAX_RETRIES = 3


class _WeakDHAdapter(HTTPAdapter):
    """HTTPS adapter that allows weaker DH keys (needed for www.ksae.org)."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _make_session() -> requests.Session:
    """Create a requests session with SSL workaround for ksae.org."""
    session = requests.Session()
    session.mount("https://", _WeakDHAdapter())
    return session


# Module-level session for reuse across calls
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Get or create the module-level requests session."""
    global _session
    if _session is None:
        _session = _make_session()
    return _session


def _get_soup(url: str, params: dict[str, Any] | None = None) -> BeautifulSoup:
    """Fetch a URL and return a BeautifulSoup object."""
    session = _get_session()
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "lxml")


def _detect_last_page(soup: BeautifulSoup) -> int:
    """Detect the last page number from the pagination '마지막' button.

    The last page button uses an img with src containing 'block_next'.
    Its parent anchor's href contains the page parameter.
    """
    block_next_img = soup.find("img", src=re.compile(r"block_next"))
    if not block_next_img:
        return 1

    parent_a = block_next_img.parent
    if not isinstance(parent_a, Tag) or parent_a.name != "a":
        return 1

    href = parent_a.get("href", "")
    if isinstance(href, list):
        href = href[0]

    parsed = urlparse(str(href))
    qs = parse_qs(parsed.query)
    page_values = qs.get("page", ["1"])
    return int(page_values[0])


def _parse_list_row(row: Tag) -> dict[str, Any] | None:
    """Parse a single table row into a post metadata dict.

    Returns None if the row is a header or non-data row.
    """
    cells = row.find_all("td")
    if len(cells) < 7:
        return None

    # Post number (번호)
    num_text = cells[0].get_text(strip=True)
    if not num_text.isdigit():
        return None
    post_number = int(num_text)

    # Category (구분)
    category = cells[1].get_text(strip=True)

    # Title and detail URL (제목)
    title_cell = cells[2]
    title_link = title_cell.find("a")
    if not title_link or not isinstance(title_link, Tag):
        return None

    # Detect reply posts by bl_reply.png icon
    reply_img = title_cell.find("img", src=lambda s: isinstance(s, str) and "bl_reply" in s)
    is_reply = reply_img is not None

    title = title_link.get_text(strip=True)
    href = title_link.get("href", "")
    if isinstance(href, list):
        href = href[0]

    # Extract the number parameter from the detail link to build a clean URL
    parsed = urlparse(str(href))
    qs = parse_qs(parsed.query)
    number_values = qs.get("number", [])
    if number_values:
        detail_number = number_values[0]
    else:
        detail_number = str(post_number)

    detail_url = f"/jajak/bbs/?number={detail_number}&mode=view&code=J_qna"

    # Author (등록자)
    author = cells[3].get_text(strip=True)

    # File column (파일) - skip index 4

    # View count (조회수)
    views_text = cells[5].get_text(strip=True).replace(",", "")
    views = int(views_text) if views_text.isdigit() else 0

    # Date (등록일)
    date = cells[6].get_text(strip=True)

    return {
        "id": post_number,
        "number": int(detail_number),
        "category": category,
        "title": title,
        "author": author,
        "views": views,
        "date": date,
        "detail_url": detail_url,
        "is_reply": is_reply,
    }


def crawl_list_pages(delay: float = DEFAULT_DELAY) -> list[dict[str, Any]]:
    """Crawl all list pages of the KSAE Q&A board.

    Returns a list of post metadata dicts with keys:
    id, number, category, title, author, views, date, detail_url.

    Args:
        delay: Seconds to wait between requests to avoid server overload.
    """
    logger.info("Detecting last page number...")
    first_page_soup = _get_soup(LIST_URL, params={"code": "J_qna", "page": "1"})
    last_page = _detect_last_page(first_page_soup)
    logger.info("Last page: %d", last_page)

    all_posts: list[dict[str, Any]] = []

    for page_num in range(1, last_page + 1):
        logger.info("Crawling list page %d/%d...", page_num, last_page)

        if page_num == 1:
            soup = first_page_soup
        else:
            time.sleep(delay)
            soup = _get_soup(LIST_URL, params={"code": "J_qna", "page": str(page_num)})

        # Find all table rows
        table = soup.find("table")
        if not table or not isinstance(table, Tag):
            logger.warning("No table found on page %d", page_num)
            continue

        rows = table.find_all("tr")
        for row in rows:
            if not isinstance(row, Tag):
                continue
            post = _parse_list_row(row)
            if post:
                all_posts.append(post)

    logger.info("Total posts collected: %d", len(all_posts))

    # Save intermediate metadata
    output_path = Path("data/raw/post_list.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_posts, f, ensure_ascii=False, indent=2)
    logger.info("Saved post list to %s", output_path)

    return all_posts


def _clean_text(text: str) -> str:
    """Strip HTML artifacts and normalize whitespace in extracted text."""
    # Normalize whitespace: collapse multiple spaces/tabs but keep newlines
    text = re.sub(r"[^\S\n]+", " ", text)
    # Collapse multiple blank lines into at most two newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def crawl_detail_page(post_number: int) -> dict[str, Any]:
    """Crawl a single post's detail page and extract its content.

    Args:
        post_number: The ``number`` parameter for the detail page URL.

    Returns:
        Dict with keys: title, author, date, views, body, attachments.
    """
    soup = _get_soup(
        LIST_URL, params={"number": str(post_number), "mode": "view", "code": "J_qna"}
    )

    table = soup.find("table", class_="tblDef")
    if not table or not isinstance(table, Tag):
        raise ValueError(f"No tblDef table found for post {post_number}")

    rows = table.find_all("tr")
    if len(rows) < 3:
        raise ValueError(f"Unexpected row count ({len(rows)}) for post {post_number}")

    # Row 0: title (class='bg')
    title = rows[0].get_text(strip=True)

    # Row 1: metadata - th/td pairs: 작성자/val, Date/val, Hits/val
    meta_tds = rows[1].find_all("td", recursive=False)
    author = meta_tds[0].get_text(strip=True) if len(meta_tds) > 0 else ""
    date = meta_tds[1].get_text(strip=True) if len(meta_tds) > 1 else ""
    views_text = meta_tds[2].get_text(strip=True).replace(",", "") if len(meta_tds) > 2 else "0"
    views = int(views_text) if views_text.isdigit() else 0

    # Row 2: body content (class='con')
    body_td = None
    for r in rows:
        td = r.find("td", class_="con")
        if td:
            body_td = td
            break

    body = ""
    if body_td:
        body = _clean_text(body_td.get_text(separator="\n", strip=True))

    # Attachment rows: th with '첨부파일' label
    attachments: list[str] = []
    for r in rows:
        th = r.find("th", class_="th")
        if th and "첨부" in th.get_text():
            td = r.find("td")
            if td:
                link = td.find("a")
                if link and isinstance(link, Tag):
                    href = link.get("href", "")
                    if isinstance(href, list):
                        href = href[0]
                    if href:
                        attachments.append(BASE_URL + str(href))

    return {
        "title": title,
        "author": author,
        "date": date,
        "views": views,
        "body": body,
        "attachments": attachments,
    }


def crawl_all_details(
    post_list: list[dict[str, Any]],
    delay: float = DEFAULT_DELAY,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Crawl detail pages for all posts and build structured output.

    Question posts and their reply posts are matched: the reply body becomes
    the ``answer_body`` of the preceding question post.  Reply-only posts
    (those with ``is_reply=True``) are not emitted as separate entries.

    Args:
        post_list: List of post metadata dicts from ``crawl_list_pages()``.
        delay: Seconds to wait between requests.

    Returns:
        Tuple of (posts list, crawl metadata dict).
    """
    failed_ids: list[int] = []

    # Crawl all detail pages with retry logic
    detail_map: dict[int, dict[str, Any]] = {}
    for idx, meta in enumerate(post_list):
        number = meta["number"]
        logger.info(
            "Crawling detail %d/%d (number=%d)...", idx + 1, len(post_list), number
        )

        detail: dict[str, Any] | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                detail = crawl_detail_page(number)
                break
            except Exception:
                if attempt < MAX_RETRIES:
                    wait = delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Attempt %d failed for number=%d, retrying in %.1fs...",
                        attempt,
                        number,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("All %d attempts failed for number=%d", MAX_RETRIES, number)
                    failed_ids.append(number)

        if detail is not None:
            detail_map[number] = detail

        if idx < len(post_list) - 1:
            time.sleep(delay)

    # Build structured posts by matching questions with reply posts.
    # In the list, reply posts (is_reply=True) appear right after the question
    # they answer and share the same title.
    posts: list[dict[str, Any]] = []
    reply_numbers: set[int] = set()

    # First pass: identify reply posts and map them to their question
    # Reply posts appear after the question with the same title.
    # We iterate in list order (descending post id) and for each reply,
    # look for the question immediately after it (next in list = earlier post).
    question_answer_map: dict[int, int] = {}  # question number -> reply number
    for i, meta in enumerate(post_list):
        if meta["is_reply"]:
            reply_numbers.add(meta["number"])
            # The question is the next entry in the list (which has a higher number
            # since the list is sorted descending by id, and the reply appears first)
            # Actually: reply posts appear AFTER the question in the list
            # (lower post id = earlier in list order which is descending).
            # Let's look at the previous entry.
            if i > 0:
                prev = post_list[i - 1]
                if not prev["is_reply"]:
                    question_answer_map[prev["number"]] = meta["number"]

    # Second pass: build output posts
    for meta in post_list:
        number = meta["number"]
        if number in reply_numbers:
            continue  # Skip reply posts as separate entries

        detail = detail_map.get(number)
        if detail is None:
            continue

        question_body = detail["body"]
        answer_body = ""
        comments: list[str] = []

        # Check if this question has an answer post
        reply_number = question_answer_map.get(number)
        if reply_number and reply_number in detail_map:
            reply_detail = detail_map[reply_number]
            raw_answer = reply_detail["body"]
            # The reply body includes the original question after a separator.
            # Extract only the answer part (before the separator).
            separator_pattern = r"={10,}\s*원\s*글\s*={10,}"
            parts = re.split(separator_pattern, raw_answer, maxsplit=1)
            answer_body = _clean_text(parts[0])

        url = f"{BASE_URL}{meta['detail_url']}"
        post_data: dict[str, Any] = {
            "id": meta["id"],
            "category": meta["category"],
            "title": meta["title"],
            "author": meta["author"],
            "date": meta["date"],
            "views": meta["views"],
            "question_body": question_body,
            "answer_body": answer_body,
            "comments": comments,
            "attachments": detail["attachments"],
            "url": url,
        }
        posts.append(post_data)

    crawl_meta: dict[str, Any] = {
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "total_count": len(posts),
        "failed_count": len(failed_ids),
        "failed_ids": failed_ids,
    }

    # Save outputs
    raw_dir = Path("data/raw")
    raw_dir.mkdir(parents=True, exist_ok=True)

    posts_path = raw_dir / "posts.json"
    with open(posts_path, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    logger.info("Saved %d posts to %s", len(posts), posts_path)

    meta_path = raw_dir / "crawl_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(crawl_meta, f, ensure_ascii=False, indent=2)
    logger.info("Saved crawl metadata to %s", meta_path)

    return posts, crawl_meta
