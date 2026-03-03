"""Crawler module for KSAE Q&A board.

Crawls the KSAE Q&A board list pages and detail pages to collect
post metadata, question/answer bodies, and comments.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DEFAULT_WORKERS = 5
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


# Thread-local session storage (requests.Session is not thread-safe)
_thread_local = threading.local()


def _get_session() -> requests.Session:
    """Get or create a thread-local requests session."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _make_session()
    return _thread_local.session


def _get_soup(url: str, params: dict[str, Any] | None = None) -> BeautifulSoup:
    """Fetch a URL and return a BeautifulSoup object."""
    session = _get_session()
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "lxml")


def _parse_list_page(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Parse all post rows from a list page's table.

    Returns an empty list if no data rows are found (signals last page reached).
    """
    table = soup.find("table")
    if not table or not isinstance(table, Tag):
        return []

    posts: list[dict[str, Any]] = []
    rows = table.find_all("tr")
    for row in rows:
        if not isinstance(row, Tag):
            continue
        post = _parse_list_row(row)
        if post:
            posts.append(post)
    return posts


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

    This function is resumable. It saves its progress after each page
    to `data/raw/post_list.json` and `data/raw/.crawl_progress.json`.
    If the process is interrupted, it will resume from the last saved page.

    Returns a list of post metadata dicts with keys:
    id, number, category, title, author, views, date, detail_url.

    Args:
        delay: Seconds to wait between requests to avoid server overload.
    """
    output_path = Path("data/raw/post_list.json")
    progress_file = Path("data/raw/.crawl_progress.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    start_page = 1
    all_posts: list[dict[str, Any]] = []

    if progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            last_page = progress.get("last_page", 0)
            if last_page > 0:
                start_page = last_page + 1
                logger.info("Resuming crawl from page %d", start_page)
                if output_path.exists():
                    all_posts = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            logger.warning("Could not read progress file, starting from page 1.")

    page_num = start_page
    consecutive_failures = 0
    max_consecutive_failures = 3
    while True:
        logger.info("Crawling list page %d...", page_num)

        soup = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                soup = _get_soup(LIST_URL, params={"code": "J_qna", "page": str(page_num)})
                break
            except requests.RequestException as e:
                if attempt < MAX_RETRIES:
                    wait = delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Attempt %d failed for page %d: %s. Retrying in %.1fs...",
                        attempt, page_num, e, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "All %d attempts failed for page %d: %s. Skipping.",
                        MAX_RETRIES, page_num, e,
                    )

        if soup is None:
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "%d consecutive page failures. Stopping.", consecutive_failures,
                )
                break
            page_num += 1
            time.sleep(delay)
            continue

        consecutive_failures = 0
        posts_on_page = _parse_list_page(soup)

        if not posts_on_page and page_num > start_page:
            logger.info("No more posts found on page %d. Stopping.", page_num)
            break

        existing_ids = {post["id"] for post in all_posts}
        new_posts = [post for post in posts_on_page if post["id"] not in existing_ids]
        all_posts.extend(new_posts)

        # Save progress after each page
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(all_posts, f, ensure_ascii=False, indent=2)
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump({"last_page": page_num}, f, indent=2)
        except IOError as e:
            logger.error("Failed to save progress on page %d: %s", page_num, e)
            return all_posts # Return what we have so far

        page_num += 1
        time.sleep(delay)

    logger.info("Crawl finished. Total posts collected: %d", len(all_posts))

    # Clean up progress file on successful completion
    if progress_file.exists():
        progress_file.unlink()

    return all_posts



def filter_new_posts(post_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter post list to only include posts not already in data/raw/posts.json.

    Compares post IDs from the crawled list against existing post IDs
    from the saved posts file. Returns only new posts (including their
    associated reply posts).

    Args:
        post_list: Full post list from ``crawl_list_pages()``.

    Returns:
        Filtered list of new post metadata dicts.
    """
    existing_path = Path("data/raw/posts.json")
    if not existing_path.exists():
        logger.info("No existing posts.json found, treating all posts as new")
        return post_list

    with open(existing_path, "r", encoding="utf-8") as f:
        existing_posts: list[dict[str, Any]] = json.load(f)

    existing_ids: set[int] = {p["id"] for p in existing_posts}
    logger.info("Found %d existing post IDs", len(existing_ids))

    # Filter to new question posts (non-reply posts not in existing IDs)
    new_question_numbers: set[int] = set()
    for meta in post_list:
        if not meta["is_reply"] and meta["id"] not in existing_ids:
            new_question_numbers.add(meta["number"])

    if not new_question_numbers:
        return []

    # Also include reply posts that follow new questions
    new_post_list: list[dict[str, Any]] = []
    for i, meta in enumerate(post_list):
        if meta["number"] in new_question_numbers:
            new_post_list.append(meta)
        elif meta["is_reply"]:
            for j in range(i - 1, -1, -1):
                if not post_list[j]["is_reply"]:
                    if post_list[j]["number"] in new_question_numbers:
                        new_post_list.append(meta)
                    break

    logger.info("Filtered to %d new posts (including replies)", len(new_post_list))
    return new_post_list


def merge_posts(
    new_posts_path: str | Path = "data/raw/posts.json",
) -> None:
    """Merge newly crawled posts into the existing posts file.

    Reads the newly crawled posts (which ``crawl_all_details`` just wrote)
    and merges them with any previously existing posts, avoiding duplicates
    by post ID.

    The merge creates a backup of the existing file before overwriting.

    Args:
        new_posts_path: Path to the posts file (used for both old and new data).
    """
    new_posts_path = Path(new_posts_path)

    # The newly crawled posts were just written to posts.json by crawl_all_details.
    # We need to merge them with the backup of the old data.
    backup_path = new_posts_path.with_suffix(".json.bak")

    if not backup_path.exists():
        # No backup means there was no previous data; nothing to merge
        logger.info("No backup file found, nothing to merge")
        return

    with open(backup_path, "r", encoding="utf-8") as f:
        old_posts: list[dict[str, Any]] = json.load(f)

    with open(new_posts_path, "r", encoding="utf-8") as f:
        new_posts: list[dict[str, Any]] = json.load(f)

    # Merge: old posts + new posts, deduplicated by id
    existing_ids: set[int] = {p["id"] for p in old_posts}
    merged = list(old_posts)
    added = 0
    for post in new_posts:
        if post["id"] not in existing_ids:
            merged.append(post)
            existing_ids.add(post["id"])
            added += 1

    with open(new_posts_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    logger.info("Merged %d new posts into %d existing (total: %d)", added, len(old_posts), len(merged))
    print(f"Merged {added} new posts into {len(old_posts)} existing (total: {len(merged)})")


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


def _crawl_detail_with_retry(
    number: int,
    delay: float = DEFAULT_DELAY,
) -> tuple[int, dict[str, Any] | None]:
    """Crawl a single detail page with retry logic (thread-safe).

    Args:
        number: The post number to crawl.
        delay: Base delay for exponential backoff on retries.

    Returns:
        Tuple of (post number, detail dict or None on failure).
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            detail = crawl_detail_page(number)
            return number, detail
        except Exception:
            if attempt < MAX_RETRIES:
                wait = delay * (2 ** (attempt - 1))
                logger.warning(
                    "Attempt %d failed for number=%d, retrying in %.1fs...",
                    attempt, number, wait,
                )
                time.sleep(wait)
            else:
                logger.error("All %d attempts failed for number=%d", MAX_RETRIES, number)
    return number, None


def crawl_all_details(
    post_list: list[dict[str, Any]],
    delay: float = DEFAULT_DELAY,
    max_workers: int = DEFAULT_WORKERS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Crawl detail pages for all posts and build structured output.

    Question posts and their reply posts are matched: the reply body becomes
    the ``answers`` list of the preceding question post.  Reply-only posts
    (those with ``is_reply=True``) are not emitted as separate entries.

    Uses a thread pool for concurrent requests to speed up crawling.

    Args:
        post_list: List of post metadata dicts from ``crawl_list_pages()``.
        delay: Seconds to wait between requests (used for retry backoff).
        max_workers: Maximum number of concurrent requests.

    Returns:
        Tuple of (posts list, crawl metadata dict).
    """
    failed_ids: list[int] = []
    detail_map: dict[int, dict[str, Any]] = {}
    total = len(post_list)

    logger.info("Crawling %d detail pages with %d workers...", total, max_workers)

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_crawl_detail_with_retry, meta["number"], delay): meta
            for meta in post_list
        }

        for future in as_completed(futures):
            number, detail = future.result()
            completed += 1

            if detail is not None:
                detail_map[number] = detail
                logger.info("Crawled detail %d/%d (number=%d)", completed, total, number)
            else:
                failed_ids.append(number)
                logger.warning("Failed detail %d/%d (number=%d)", completed, total, number)

    # Build structured posts by matching questions with reply posts.
    # In the list, reply posts (is_reply=True) appear right after the question
    # they answer and share the same title.
    posts: list[dict[str, Any]] = []
    reply_numbers: set[int] = set()

    # First pass: identify reply posts and map them to their question.
    # Reply posts appear after the question in the list. A question may
    # have multiple replies, so we look backward past other replies to
    # find the parent question post.
    question_answer_map: dict[int, list[int]] = {}  # question number -> reply numbers
    for i, meta in enumerate(post_list):
        if meta["is_reply"]:
            reply_numbers.add(meta["number"])
            # Look backward for the nearest non-reply (question) post
            for j in range(i - 1, -1, -1):
                if not post_list[j]["is_reply"]:
                    question_answer_map.setdefault(post_list[j]["number"], []).append(meta["number"])
                    break

    # Second pass: build output posts
    for meta in post_list:
        number = meta["number"]
        if number in reply_numbers:
            continue  # Skip reply posts as separate entries

        detail = detail_map.get(number)
        if detail is None:
            continue

        question_body = detail["body"]
        answers: list[dict[str, str]] = []

        # Collect answer bodies from all reply posts
        reply_nums = question_answer_map.get(number, [])
        for reply_number in reply_nums:
            if reply_number in detail_map:
                reply_detail = detail_map[reply_number]
                raw_answer = reply_detail["body"]
                # The reply body includes the original question after a separator.
                # Extract only the answer part (before the separator).
                separator_pattern = r"={10,}\s*원\s*글\s*={10,}"
                parts = re.split(separator_pattern, raw_answer, maxsplit=1)
                if len(parts) == 1:
                    logger.warning(
                        "Answer separator not found for reply number=%d (question number=%d). "
                        "Raw answer may include original question text.",
                        reply_number, number,
                    )
                cleaned = _clean_text(parts[0])
                if cleaned:
                    answers.append({
                        "body": cleaned,
                        "url": f"{BASE_URL}/jajak/bbs/?number={reply_number}&mode=view&code=J_qna",
                    })

        comments: list[str] = []

        url = f"{BASE_URL}{meta['detail_url']}"
        post_data: dict[str, Any] = {
            "id": meta["id"],
            "category": meta["category"],
            "title": meta["title"],
            "author": meta["author"],
            "date": meta["date"],
            "views": meta["views"],
            "question_body": question_body,
            "answers": answers,
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

    # Backup existing posts.json before overwriting (for incremental merge)
    if posts_path.exists():
        import shutil
        backup_path = posts_path.with_suffix(".json.bak")
        shutil.copy2(posts_path, backup_path)
        logger.info("Backed up existing posts to %s", backup_path)

    with open(posts_path, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)
    logger.info("Saved %d posts to %s", len(posts), posts_path)

    meta_path = raw_dir / "crawl_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(crawl_meta, f, ensure_ascii=False, indent=2)
    logger.info("Saved crawl metadata to %s", meta_path)

    return posts, crawl_meta
