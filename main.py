"""CLI entry point for the KSAE Q&A VectorDB pipeline.

Provides a unified CLI to run the full pipeline (crawl -> chunk -> embed -> upload)
or individual stages independently.
"""

from __future__ import annotations

import logging
import json
import sys
import time
from pathlib import Path

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _run_stage(name: str, func: object, **kwargs: object) -> None:
    """Run a pipeline stage with timing and error handling.

    Args:
        name: Human-readable name of the stage.
        func: Callable to invoke for the stage.
        **kwargs: Arguments forwarded to the callable.
    """
    from typing import Callable, Any

    assert callable(func)
    typed_func: Callable[..., Any] = func

    logger.info("Starting stage: %s", name)
    start = time.time()
    try:
        typed_func(**kwargs)
    except Exception as e:
        elapsed = time.time() - start
        logger.error(
            "Stage '%s' failed after %.1fs: %s", name, elapsed, e
        )
        click.echo(f"ERROR: Stage '{name}' failed after {elapsed:.1f}s: {e}", err=True)
        click.echo("Intermediate results have been preserved.", err=True)
        sys.exit(1)
    elapsed = time.time() - start
    logger.info("Stage '%s' completed in %.1fs", name, elapsed)
    click.echo(f"Stage '{name}' completed in {elapsed:.1f}s")


@click.group(invoke_without_command=True)
@click.option("--qdrant-url", default="http://localhost:6333", help="Qdrant server URL.")
@click.option("--qdrant-api-key", default=None, help="Qdrant API key.")
@click.option("--collection", default="ksae-qna", help="Qdrant collection name.")
@click.option("--batch-size", default=32, type=int, help="Embedding batch size.")
@click.option("--embed-url", default=None, help="BGE-M3 embedding API URL. If not set, uses local model.")
@click.option("--delay", default=1.5, type=float, help="Delay between requests (seconds).")
@click.option("--workers", default=5, type=int, help="Max concurrent requests for detail crawling.")
@click.option("--mode", default="incremental", type=click.Choice(["full", "incremental"]), help="Crawl mode: full or incremental (default: incremental).")
@click.pass_context
def cli(ctx: click.Context, qdrant_url: str, qdrant_api_key: str | None, collection: str, batch_size: int, embed_url: str, delay: float, workers: int, mode: str) -> None:
    """KSAE Q&A VectorDB Pipeline.

    Run the full pipeline (crawl -> chunk -> embed -> upload) or individual stages.
    """
    ctx.ensure_object(dict)
    ctx.obj["qdrant_url"] = qdrant_url
    ctx.obj["qdrant_api_key"] = qdrant_api_key
    ctx.obj["collection"] = collection
    ctx.obj["batch_size"] = batch_size
    ctx.obj["embed_url"] = embed_url
    ctx.obj["delay"] = delay
    ctx.obj["workers"] = workers
    ctx.obj["mode"] = mode

    if ctx.invoked_subcommand is None:
        # Run full pipeline
        _run_full_pipeline(qdrant_url, qdrant_api_key, collection, batch_size, embed_url, delay, workers, mode)


def _run_full_pipeline(qdrant_url: str, qdrant_api_key: str | None, collection: str, batch_size: int, embed_url: str, delay: float, workers: int = 5, mode: str = "incremental") -> None:
    """Execute the full pipeline: crawl -> chunk -> embed -> upload."""
    import json

    from src.chunker import chunk_posts
    from src.crawler import crawl_all_details, crawl_list_pages, filter_new_posts, merge_posts
    from src.embedder import embed_chunks
    from src.uploader import upload_to_qdrant

    total_start = time.time()
    is_incremental = mode == "incremental"
    click.echo(f"Running full pipeline ({mode} mode): crawl -> chunk -> embed -> upload")

    _run_stage("crawl-list", crawl_list_pages, delay=delay)

    with open("data/raw/post_list.json", "r", encoding="utf-8") as f:
        post_list: list[dict[str, object]] = json.load(f)

    if is_incremental:
        new_post_list = filter_new_posts(post_list)
        if not new_post_list:
            click.echo("No new posts found.")
            return
        click.echo(f"Found {len(new_post_list)} new posts to process")
        _run_stage("crawl-detail", crawl_all_details, post_list=new_post_list, delay=delay, max_workers=workers)
        _run_stage("merge", merge_posts)
    else:
        _run_stage("crawl-detail", crawl_all_details, post_list=post_list, delay=delay, max_workers=workers)

    _run_stage("chunk", chunk_posts)
    _run_stage("embed", embed_chunks, batch_size=batch_size, embed_url=embed_url)
    _run_stage("upload", upload_to_qdrant, qdrant_url=qdrant_url, api_key=qdrant_api_key, collection_name=collection, recreate=not is_incremental)

    total_elapsed = time.time() - total_start
    click.echo(f"Full pipeline completed in {total_elapsed:.1f}s")
    logger.info("Full pipeline completed in %.1fs", total_elapsed)


@cli.command()
@click.pass_context
def crawl(ctx: click.Context) -> None:
    """Run the crawl stage (list pages + detail pages)."""
    import json

    from src.crawler import crawl_all_details, crawl_list_pages, filter_new_posts, merge_posts

    delay: float = ctx.obj["delay"]
    workers: int = ctx.obj["workers"]
    mode: str = ctx.obj["mode"]
    is_incremental = mode == "incremental"

    _run_stage("crawl-list", crawl_list_pages, delay=delay)

    with open("data/raw/post_list.json", "r", encoding="utf-8") as f:
        post_list: list[dict[str, object]] = json.load(f)

    if is_incremental:
        new_post_list = filter_new_posts(post_list)
        if not new_post_list:
            click.echo("No new posts found.")
            return
        click.echo(f"Found {len(new_post_list)} new posts to process")
        _run_stage("crawl-detail", crawl_all_details, post_list=new_post_list, delay=delay, max_workers=workers)
        _run_stage("merge", merge_posts)
    else:
        _run_stage("crawl-detail", crawl_all_details, post_list=post_list, delay=delay, max_workers=workers)


@cli.command()
@click.pass_context
def chunk(ctx: click.Context) -> None:
    """Run the chunk stage."""
    from src.chunker import chunk_posts

    _run_stage("chunk", chunk_posts)


@cli.command()
@click.pass_context
def embed(ctx: click.Context) -> None:
    """Run the embed stage."""
    from src.embedder import embed_chunks

    batch_size: int = ctx.obj["batch_size"]
    embed_url: str = ctx.obj["embed_url"]
    _run_stage("embed", embed_chunks, batch_size=batch_size, embed_url=embed_url)


@cli.command()
@click.option("--recreate", is_flag=True, default=False, help="Delete and recreate the collection before uploading.")
@click.pass_context
def upload(ctx: click.Context, recreate: bool) -> None:
    """Run the upload stage."""
    from src.uploader import upload_to_qdrant

    qdrant_url: str = ctx.obj["qdrant_url"]
    qdrant_api_key: str | None = ctx.obj["qdrant_api_key"]
    collection: str = ctx.obj["collection"]
    _run_stage("upload", upload_to_qdrant, qdrant_url=qdrant_url, api_key=qdrant_api_key, collection_name=collection, recreate=recreate)


# ---------------------------------------------------------------------------
# AARK knowledge base — a single curated Markdown document, not a crawl.
# Shares the embed/upload stages with the Q&A pipeline but uses its own
# chunker, data paths and collection.
# ---------------------------------------------------------------------------

KB_SOURCE = "data/raw/aark-kb.md"
KB_CHUNKS = "data/processed/kb_chunks.json"
KB_EMBEDDINGS = "data/processed/kb_embeddings.npy"
KB_COLLECTION = "ksae-aark-kb"
KB_PAYLOAD_FIELDS = ("source_type", "source_version", "chapter_num", "chapter",
                     "section", "topic", "confidence", "dates", "kind")
KB_INDEX_FIELDS = ("chapter", "confidence", "kind")


# ---------------------------------------------------------------------------
# Formula 2026 규정집 — PDF 원문 기반 파이프라인.
# ---------------------------------------------------------------------------

RULES_SOURCE = "data/raw/formula-2026-2026.pdf"
RULES_CHUNKS = "data/processed/rules_chunks.json"
RULES_EMBEDDINGS = "data/processed/rules_embeddings.npy"
RULES_COLLECTION = "ksae-formula-rules-reembed"
RULES_PAYLOAD_FIELDS = ("chapter", "chapter_num", "section", "section_num")
RULES_INDEX_FIELDS = ("chapter", "chapter_num", "section")
RULES_2026_YEAR = "2026"
RULES_2026_MANIFEST = "data/raw/rules-2026/rules-2026-manifest.json"
RULES_2026_RAW_DIR = "data/raw/rules-2026"
RULES_2026_CHUNKS_ALL = "data/processed/rules-2026/rules-2026-all-chunks.json"
RULES_2026_CHUNKS_DIR = "data/processed/rules-2026/chunks"
RULES_2026_EMB_DIR = "data/processed/rules-2026/embeddings"
RULES_2026_PAYLOAD_FIELDS = (
    "source_type",
    "source_version",
    "source_post_id",
    "source_key",
    "source_file",
    "source_filename",
    "source_url",
    "source_title",
    "competition",
    "document_type",
    "year",
    "chapter",
    "chapter_num",
    "section",
    "section_num",
)
RULES_2026_INDEX_FIELDS = (
    "competition",
    "document_type",
    "year",
    "source_post_id",
    "source_filename",
    "source_key",
    "chapter",
    "section",
)


def _resolve_rules_2026_collections(year: str, requested: list[str] | tuple[str, ...] | None = None) -> list[str]:
    from src.rules_registry import rules_collection_registry

    registry = rules_collection_registry(year=year)
    available = sorted(registry)
    if not requested:
        return available

    requested_set = set(requested)
    unknown = requested_set - set(available)
    if unknown:
        raise ValueError(f"Unknown rules collections: {', '.join(sorted(unknown))}")
    return sorted(requested_set)


def _rules_2026_bucket_file(collection_key: str, folder: str = RULES_2026_CHUNKS_DIR) -> str:
    return str(Path(folder) / f"{collection_key}.json")


def _rules_2026_embedding_file(collection_key: str, folder: str = RULES_2026_EMB_DIR) -> str:
    return str(Path(folder) / f"{collection_key}.npy")


def _split_rules_2026_chunks(
    chunks_path: str = RULES_2026_CHUNKS_ALL,
    year: str = RULES_2026_YEAR,
    only_collections: set[str] | None = None,
) -> list[str]:
    """Split a combined chunk file into collection bucket chunk files."""
    from src.rules_registry import build_collection_key, normalize_competition_key, normalize_document_type

    with open(chunks_path, "r", encoding="utf-8") as f:
        chunks = json.load(f)

    buckets: dict[str, list[dict[str, object]]] = {}
    for chunk in chunks:
        if str(chunk.get("year", year)) != str(year):
            continue
        competition = normalize_competition_key(chunk.get("competition") or "")
        document_type = normalize_document_type(chunk.get("document_type") or "")
        key = build_collection_key(competition, document_type, year)
        if only_collections is not None and key not in only_collections:
            continue
        buckets.setdefault(key, []).append(chunk)

    paths: list[str] = []
    Path(RULES_2026_CHUNKS_DIR).mkdir(parents=True, exist_ok=True)
    for key in sorted(buckets):
        bucket_path = _rules_2026_bucket_file(key)
        buckets[key].sort(
            key=lambda c: (
                str(c.get("source_key", "")),
                int(c.get("chunk_index", 0) or 0),
            )
        )
        with open(bucket_path, "w", encoding="utf-8") as f:
            json.dump(buckets[key], f, ensure_ascii=False, indent=2)
        paths.append(bucket_path)

    # Remove stale bucket files for explicitly selected collections so a missing bucket
    # does not reuse an old artifact in subsequent uploads.
    if only_collections is not None:
        for key in sorted(only_collections):
            if key in buckets:
                continue
            stale_bucket_path = _rules_2026_bucket_file(key)
            if Path(stale_bucket_path).exists():
                Path(stale_bucket_path).unlink()

    return paths


def _kb_upload(ctx: click.Context, collection: str, recreate: bool) -> None:
    """Run the KB upload stage with the knowledge base payload schema."""
    from src.uploader import upload_to_qdrant

    _run_stage(
        "kb-upload", upload_to_qdrant,
        chunks_path=KB_CHUNKS,
        embeddings_path=KB_EMBEDDINGS,
        qdrant_url=ctx.obj["qdrant_url"],
        api_key=ctx.obj["qdrant_api_key"],
        collection_name=collection,
        recreate=recreate,
        payload_fields=KB_PAYLOAD_FIELDS,
        index_fields=KB_INDEX_FIELDS,
        prune=True,
    )


@cli.command()
@click.option("--source", default=KB_SOURCE, help="Knowledge base Markdown path.")
@click.option("--kb-collection", default=KB_COLLECTION, help="Qdrant collection for the knowledge base.")
@click.option("--recreate", is_flag=True, default=False, help="Delete and recreate the collection before uploading.")
@click.pass_context
def kb(ctx: click.Context, source: str, kb_collection: str, recreate: bool) -> None:
    """Run the knowledge base pipeline (chunk -> embed -> upload)."""
    from src.embedder import embed_chunks
    from src.kb_chunker import chunk_kb

    total_start = time.time()
    click.echo(f"Running KB pipeline: chunk -> embed -> upload ({source} -> {kb_collection})")

    _run_stage("kb-chunk", chunk_kb, input_path=source, output_path=KB_CHUNKS)
    _run_stage("kb-embed", embed_chunks, input_path=KB_CHUNKS, output_path=KB_EMBEDDINGS,
               batch_size=ctx.obj["batch_size"], embed_url=ctx.obj["embed_url"])
    _kb_upload(ctx, kb_collection, recreate)

    elapsed = time.time() - total_start
    click.echo(f"KB pipeline completed in {elapsed:.1f}s")


@cli.command("kb-chunk")
@click.option("--source", default=KB_SOURCE, help="Knowledge base Markdown path.")
@click.pass_context
def kb_chunk(ctx: click.Context, source: str) -> None:
    """Run the knowledge base chunk stage."""
    from src.kb_chunker import chunk_kb

    _run_stage("kb-chunk", chunk_kb, input_path=source, output_path=KB_CHUNKS)


@cli.command("kb-embed")
@click.pass_context
def kb_embed(ctx: click.Context) -> None:
    """Run the knowledge base embed stage."""
    from src.embedder import embed_chunks

    _run_stage("kb-embed", embed_chunks, input_path=KB_CHUNKS, output_path=KB_EMBEDDINGS,
               batch_size=ctx.obj["batch_size"], embed_url=ctx.obj["embed_url"])


@cli.command("kb-upload")
@click.option("--kb-collection", default=KB_COLLECTION, help="Qdrant collection for the knowledge base.")
@click.option("--recreate", is_flag=True, default=False, help="Delete and recreate the collection before uploading.")
@click.pass_context
def kb_upload(ctx: click.Context, kb_collection: str, recreate: bool) -> None:
    """Run the knowledge base upload stage."""
    _kb_upload(ctx, kb_collection, recreate)


def _rules_upload(ctx: click.Context, rules_collection: str, recreate: bool) -> None:
    """Run the rules upload stage with the formula rules payload schema."""
    from src.uploader import upload_to_qdrant

    _run_stage(
        "rules-upload", upload_to_qdrant,
        chunks_path=RULES_CHUNKS,
        embeddings_path=RULES_EMBEDDINGS,
        qdrant_url=ctx.obj["qdrant_url"],
        api_key=ctx.obj["qdrant_api_key"],
        collection_name=rules_collection,
        recreate=recreate,
        payload_fields=RULES_PAYLOAD_FIELDS,
        index_fields=RULES_INDEX_FIELDS,
        prune=True,
    )


@cli.command()
@click.option("--source", default=RULES_SOURCE, help="Formula 규정 PDF 또는 텍스트 경로.")
@click.option("--rules-collection", default=RULES_COLLECTION, help="Qdrant collection for formula rules.")
@click.option("--recreate", is_flag=True, default=False, help="Delete and recreate the collection before uploading.")
@click.pass_context
def rules(ctx: click.Context, source: str, rules_collection: str, recreate: bool) -> None:
    """Run Formula 규정 파이프라인 (chunk -> embed -> upload)."""
    from src.embedder import embed_chunks
    from src.rules_chunker import chunk_rules

    total_start = time.time()
    click.echo(f"Running rules pipeline: chunk -> embed -> upload ({source} -> {rules_collection})")

    _run_stage("rules-chunk", chunk_rules, input_path=source, output_path=RULES_CHUNKS)
    _run_stage("rules-embed", embed_chunks, input_path=RULES_CHUNKS, output_path=RULES_EMBEDDINGS,
               batch_size=ctx.obj["batch_size"], embed_url=ctx.obj["embed_url"])
    _rules_upload(ctx, rules_collection, recreate)

    elapsed = time.time() - total_start
    click.echo(f"Rules pipeline completed in {elapsed:.1f}s")


@cli.command("rules-chunk")
@click.option("--source", default=RULES_SOURCE, help="Formula 규정 PDF 또는 텍스트 경로.")
@click.pass_context
def rules_chunk(ctx: click.Context, source: str) -> None:
    """Run the rules chunk stage."""
    from src.rules_chunker import chunk_rules

    _run_stage("rules-chunk", chunk_rules, input_path=source, output_path=RULES_CHUNKS)


@cli.command("rules-embed")
@click.pass_context
def rules_embed(ctx: click.Context) -> None:
    """Run the rules embed stage."""
    from src.embedder import embed_chunks

    _run_stage("rules-embed", embed_chunks, input_path=RULES_CHUNKS, output_path=RULES_EMBEDDINGS,
               batch_size=ctx.obj["batch_size"], embed_url=ctx.obj["embed_url"])


@cli.command("rules-upload")
@click.option("--rules-collection", default=RULES_COLLECTION, help="Qdrant collection for formula rules.")
@click.option("--recreate", is_flag=True, default=False, help="Delete and recreate the collection before uploading.")
@click.pass_context
def rules_upload(ctx: click.Context, rules_collection: str, recreate: bool) -> None:
    """Run the rules upload stage."""
    _rules_upload(ctx, rules_collection, recreate)


@cli.command("rules-2026-crawl")
@click.option("--year", default=RULES_2026_YEAR, help="년도(예: 2026).")
@click.option("--manifest", default=RULES_2026_MANIFEST, help="크롤링 메니페스트 저장 경로.")
@click.option("--raw-dir", default=RULES_2026_RAW_DIR, help="PDF 저장 디렉터리.")
@click.pass_context
def rules_2026_crawl(ctx: click.Context, year: str, manifest: str, raw_dir: str) -> None:
    """Crawl J_rule and download 2026 rule PDFs."""
    from src.rules_crawler import crawl_rules_list_pages, download_rules_pdfs, save_manifest

    delay: float = ctx.obj["delay"]
    start = time.time()

    manifest_items = crawl_rules_list_pages(delay=delay, year=year)
    manifest_items = download_rules_pdfs(manifest_items, raw_dir=raw_dir, delay=delay)
    save_manifest(manifest_items, manifest)

    elapsed = time.time() - start
    logger.info("rules-2026-crawl completed in %.1fs", elapsed)
    print(f"rules-2026-crawl completed in {elapsed:.1f}s")


@cli.command("rules-2026-chunk")
@click.option("--manifest", default=RULES_2026_MANIFEST, help="크롤링 메니페스트 경로.")
@click.option("--year", default=RULES_2026_YEAR, help="년도(예: 2026).")
@click.option("--output", default=RULES_2026_CHUNKS_ALL, help="통합 청크 저장 경로.")
@click.pass_context
def rules_2026_chunk(ctx: click.Context, manifest: str, year: str, output: str) -> None:
    """Chunk 2026 규정 manifest to one normalized chunk file."""
    from src.rules_chunker import chunk_rules_manifest

    _run_stage("rules-2026-chunk", chunk_rules_manifest, manifest_path=manifest, output_path=output, year=year)


@cli.command("rules-2026-embed")
@click.option("--chunks", default=RULES_2026_CHUNKS_ALL, help="통합 청크 경로.")
@click.option("--year", default=RULES_2026_YEAR, help="년도(예: 2026).")
@click.option("--collection", "collections", multiple=True, help="대상 collection 키(반복 가능). 생략 시 전체 업로드.")
@click.pass_context
def rules_2026_embed(ctx: click.Context, chunks: str, year: str, collections: list[str]) -> None:
    """Embed all buckets of the selected 2026 규정 collections."""
    from src.embedder import embed_chunks

    try:
        selected_keys = _resolve_rules_2026_collections(year=year, requested=collections)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        ctx.exit(1)

    # Split/overwrite per-collection chunk files first.
    _run_stage("rules-2026-split", _split_rules_2026_chunks, chunks_path=chunks, year=year, only_collections=set(selected_keys))

    batch_size: int = ctx.obj["batch_size"]
    embed_url: str | None = ctx.obj["embed_url"]
    for collection_key in selected_keys:
        bucket_path = _rules_2026_bucket_file(collection_key)
        if not Path(bucket_path).exists():
            click.echo(f"No chunks for {collection_key}, skip embedding")
            continue

        embedding_path = _rules_2026_embedding_file(collection_key)
        _run_stage(
            f"rules-2026-embed:{collection_key}",
            embed_chunks,
            input_path=bucket_path,
            output_path=embedding_path,
            batch_size=batch_size,
            embed_url=embed_url,
        )


@cli.command("rules-2026-upload")
@click.option("--year", default=RULES_2026_YEAR, help="년도(예: 2026).")
@click.option("--chunks", default=RULES_2026_CHUNKS_ALL, help="통합 청크 경로.")
@click.option("--collection", "collections", multiple=True, help="대상 collection 키(반복 가능). 생략 시 전체 업로드.")
@click.option("--recreate", is_flag=True, default=False, help="컬렉션 재생성 후 업로드.")
@click.pass_context
def rules_2026_upload(ctx: click.Context, year: str, chunks: str, collections: list[str], recreate: bool) -> None:
    """Upload selected 2026 규정 collection buckets to Qdrant."""
    from src.uploader import upload_to_qdrant
    from src.rules_registry import rules_collection_registry

    try:
        selected_keys = _resolve_rules_2026_collections(year=year, requested=collections)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        ctx.exit(1)

    # Ensure bucketed chunk files exist for requested collections.
    _run_stage("rules-2026-split", _split_rules_2026_chunks, chunks_path=chunks, year=year, only_collections=set(selected_keys))

    registry = rules_collection_registry(year=year)
    qdrant_url: str = ctx.obj["qdrant_url"]
    qdrant_api_key: str | None = ctx.obj["qdrant_api_key"]

    for collection_key in selected_keys:
        info = registry.get(collection_key)
        if not info:
            continue

        bucket_path = _rules_2026_bucket_file(collection_key)
        embedding_path = _rules_2026_embedding_file(collection_key)
        if not Path(bucket_path).exists():
            click.echo(f"No chunks for {collection_key}, skip upload")
            continue

        _run_stage(
            f"rules-2026-upload:{collection_key}",
            upload_to_qdrant,
            chunks_path=bucket_path,
            embeddings_path=embedding_path,
            qdrant_url=qdrant_url,
            api_key=qdrant_api_key,
            collection_name=info.collection,
            recreate=recreate,
            payload_fields=RULES_2026_PAYLOAD_FIELDS,
            index_fields=RULES_2026_INDEX_FIELDS,
            prune=True,
        )


@cli.command("rules-2026")
@click.option("--year", default=RULES_2026_YEAR, help="년도(예: 2026).")
@click.option("--manifest", default=RULES_2026_MANIFEST, help="크롤링 메니페스트 경로.")
@click.option("--raw-dir", default=RULES_2026_RAW_DIR, help="PDF 저장 디렉터리.")
@click.option("--chunks", default=RULES_2026_CHUNKS_ALL, help="통합 청크 저장 경로.")
@click.option("--collection", "collections", multiple=True, help="업로드할 collection 키(반복 가능). 생략 시 전체 업로드.")
@click.option("--recreate", is_flag=True, default=False, help="컬렉션 재생성 후 업로드.")
@click.pass_context
def rules_2026(ctx: click.Context, year: str, manifest: str, raw_dir: str, chunks: str, collections: list[str], recreate: bool) -> None:
    """Crawl -> download -> chunk -> embed -> upload for all 2026 규정 documents."""
    from src.embedder import embed_chunks
    from src.rules_crawler import crawl_rules_list_pages, download_rules_pdfs, save_manifest
    from src.rules_chunker import chunk_rules_manifest
    from src.uploader import upload_to_qdrant
    from src.rules_registry import rules_collection_registry

    total_start = time.time()
    delay: float = ctx.obj["delay"]
    batch_size: int = ctx.obj["batch_size"]
    embed_url: str | None = ctx.obj["embed_url"]
    qdrant_url: str = ctx.obj["qdrant_url"]
    qdrant_api_key: str | None = ctx.obj["qdrant_api_key"]

    try:
        selected_keys = _resolve_rules_2026_collections(year=year, requested=collections)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        ctx.exit(1)

    manifest_items = crawl_rules_list_pages(delay=delay, year=year)
    manifest_items = download_rules_pdfs(manifest_items, raw_dir=raw_dir, delay=delay)
    save_manifest(manifest_items, manifest)
    print(f"Crawled+downloaded {len(manifest_items)} PDF entries")

    _run_stage(
        "rules-2026-chunk",
        chunk_rules_manifest,
        manifest_path=manifest,
        output_path=chunks,
        year=year,
    )

    _run_stage("rules-2026-split", _split_rules_2026_chunks, chunks_path=chunks, year=year, only_collections=set(selected_keys))

    registry = rules_collection_registry(year=year)
    for collection_key in selected_keys:
        info = registry.get(collection_key)
        if not info:
            continue

        bucket_path = _rules_2026_bucket_file(collection_key)
        if not Path(bucket_path).exists():
            click.echo(f"No chunks for {collection_key}, skip")
            continue

        embedding_path = _rules_2026_embedding_file(collection_key)
        _run_stage(
            f"rules-2026-embed:{collection_key}",
            embed_chunks,
            input_path=bucket_path,
            output_path=embedding_path,
            batch_size=batch_size,
            embed_url=embed_url,
        )
        _run_stage(
            f"rules-2026-upload:{collection_key}",
            upload_to_qdrant,
            chunks_path=bucket_path,
            embeddings_path=embedding_path,
            qdrant_url=qdrant_url,
            api_key=qdrant_api_key,
            collection_name=info.collection,
            payload_fields=RULES_2026_PAYLOAD_FIELDS,
            index_fields=RULES_2026_INDEX_FIELDS,
            recreate=recreate,
            prune=True,
        )

    elapsed = time.time() - total_start
    click.echo(f"rules-2026 pipeline completed in {elapsed:.1f}s")


if __name__ == "__main__":
    cli()
