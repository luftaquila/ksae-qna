"""CLI entry point for the KSAE Q&A VectorDB pipeline.

Provides a unified CLI to run the full pipeline (crawl -> chunk -> embed -> upload)
or individual stages independently.
"""

from __future__ import annotations

import logging
import sys
import time

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


if __name__ == "__main__":
    cli()
