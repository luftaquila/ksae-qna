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
@click.option("--collection", default="ksae_qna", help="Qdrant collection name.")
@click.option("--batch-size", default=32, type=int, help="Embedding batch size.")
@click.option("--delay", default=1.5, type=float, help="Delay between requests (seconds).")
@click.pass_context
def cli(ctx: click.Context, qdrant_url: str, collection: str, batch_size: int, delay: float) -> None:
    """KSAE Q&A VectorDB Pipeline.

    Run the full pipeline (crawl -> chunk -> embed -> upload) or individual stages.
    """
    ctx.ensure_object(dict)
    ctx.obj["qdrant_url"] = qdrant_url
    ctx.obj["collection"] = collection
    ctx.obj["batch_size"] = batch_size
    ctx.obj["delay"] = delay

    if ctx.invoked_subcommand is None:
        # Run full pipeline
        _run_full_pipeline(qdrant_url, collection, batch_size, delay)


def _run_full_pipeline(qdrant_url: str, collection: str, batch_size: int, delay: float) -> None:
    """Execute the full pipeline: crawl -> chunk -> embed -> upload."""
    from src.chunker import chunk_posts
    from src.crawler import crawl_all_details, crawl_list_pages
    from src.embedder import embed_chunks
    from src.uploader import upload_to_qdrant

    total_start = time.time()
    click.echo("Running full pipeline: crawl -> chunk -> embed -> upload")

    _run_stage("crawl-list", crawl_list_pages, delay=delay)
    from src.crawler import crawl_list_pages as _crawl_list

    import json
    with open("data/raw/post_list.json", "r", encoding="utf-8") as f:
        post_list = json.load(f)
    _run_stage("crawl-detail", crawl_all_details, post_list=post_list, delay=delay)

    _run_stage("chunk", chunk_posts)
    _run_stage("embed", embed_chunks, batch_size=batch_size)
    _run_stage("upload", upload_to_qdrant, qdrant_url=qdrant_url, collection_name=collection)

    total_elapsed = time.time() - total_start
    click.echo(f"Full pipeline completed in {total_elapsed:.1f}s")
    logger.info("Full pipeline completed in %.1fs", total_elapsed)


@cli.command()
@click.pass_context
def crawl(ctx: click.Context) -> None:
    """Run the crawl stage (list pages + detail pages)."""
    import json

    from src.crawler import crawl_all_details, crawl_list_pages

    delay: float = ctx.obj["delay"]

    _run_stage("crawl-list", crawl_list_pages, delay=delay)

    with open("data/raw/post_list.json", "r", encoding="utf-8") as f:
        post_list = json.load(f)
    _run_stage("crawl-detail", crawl_all_details, post_list=post_list, delay=delay)


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
    _run_stage("embed", embed_chunks, batch_size=batch_size)


@cli.command()
@click.pass_context
def upload(ctx: click.Context) -> None:
    """Run the upload stage."""
    from src.uploader import upload_to_qdrant

    qdrant_url: str = ctx.obj["qdrant_url"]
    collection: str = ctx.obj["collection"]
    _run_stage("upload", upload_to_qdrant, qdrant_url=qdrant_url, collection_name=collection)


if __name__ == "__main__":
    cli()
