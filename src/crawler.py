"""Crawler module for KSAE Q&A board.

Crawls the KSAE Q&A board list pages and detail pages to collect
post metadata, question/answer bodies, and comments.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ksae.org"
LIST_URL = f"{BASE_URL}/jajak/bbs/"
DEFAULT_DELAY = 1.5


def _get_soup(url: str, params: dict[str, Any] | None = None) -> BeautifulSoup:
    """Fetch a URL and return a BeautifulSoup object."""
    resp = requests.get(url, params=params, timeout=30)
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
