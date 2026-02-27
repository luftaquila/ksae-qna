#!/usr/bin/env python3
"""
KSAE Rules Qdrant MCP Server
Simple MCP server for semantic search using BGE-M3 embeddings.
"""

import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

load_dotenv()

# Configuration
QDRANT_URL = os.environ.get("QDRANT_URL", "https://vectordb.luftaquila.io:443")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")
EMBEDDING_MODEL = "BAAI/bge-m3"

# Initialize clients
client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
model = SentenceTransformer(EMBEDDING_MODEL)


def get_collections() -> list[str]:
    """Get all collection names."""
    return [c.name for c in client.get_collections().collections]


def search(collection: str, query: str, limit: int = 5) -> list[dict]:
    """Search a collection with a query."""
    vector = model.encode(query).tolist()
    results = client.query_points(
        collection_name=collection,
        query=vector,
        limit=limit,
    )
    output = []
    for hit in results.points:
        payload = hit.payload or {}
        content = payload.get("content", "") or payload.get("chunk_text", "")

        # ksae-qna: id, category, title, author, date, url, content, chunk_index
        if "title" in payload:
            source = f"[{payload.get('category', '')}] {payload['title']}"
            url = payload.get("url", "")
        # ksae-formula-rules: content, chapter, chapter_num, section, section_num, ...
        elif "chapter" in payload:
            source = f"제{payload.get('chapter_num', '')}장 {payload.get('chapter', '')} > {payload.get('section', '')}"
            url = ""
        else:
            source = ""
            url = ""

        output.append({"score": hit.score, "source": source, "url": url, "content": content})
    return output


def format_results(results: list[dict]) -> str:
    """Format search results for display."""
    parts = []
    for r in results:
        header = f"**{r['source']}** (score: {r['score']:.4f})"
        if r["url"]:
            header += f"\n{r['url']}"
        parts.append(f"{header}\n```\n{r['content']}\n```")
    return "\n\n---\n\n".join(parts) or "No results found."


def handle_request(request: dict) -> dict:
    """Handle a JSON-RPC request."""
    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ksae-rules-qdrant", "version": "1.0.0"},
            },
        }

    elif method == "notifications/initialized":
        return None  # No response for notifications

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "qdrant-search",
                        "description": "Search KSAE Formula rules and documentation using semantic search",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "collection": {
                                    "type": "string",
                                    "description": f"Collection to search. Available: {', '.join(get_collections())}",
                                },
                                "query": {
                                    "type": "string",
                                    "description": "Search query",
                                },
                                "limit": {
                                    "type": "integer",
                                    "description": "Number of results (default: 5)",
                                    "default": 5,
                                },
                            },
                            "required": ["collection", "query"],
                        },
                    },
                    {
                        "name": "qdrant-collections",
                        "description": "List all available Qdrant collections",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            },
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})

        try:
            if tool_name == "qdrant-search":
                results = search(
                    args["collection"],
                    args["query"],
                    args.get("limit", 5),
                )
                text = format_results(results)
                content = [{"type": "text", "text": text}]

            elif tool_name == "qdrant-collections":
                collections = get_collections()
                content = [{"type": "text", "text": "\n".join(f"- {c}" for c in collections)}]

            else:
                content = [{"type": "text", "text": f"Unknown tool: {tool_name}"}]

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": content},
            }

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"Error: {e}"}]},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
    """Main loop: read JSON-RPC from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            response = handle_request(request)
            if response:
                print(json.dumps(response), flush=True)
        except json.JSONDecodeError:
            pass


if __name__ == "__main__":
    main()
