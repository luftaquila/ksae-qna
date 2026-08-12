"""Regression tests for rule crawler/chunker draft filtering."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup

from src import rules_crawler, rules_chunker


def test_crawler_skips_draft_documents_in_list_row():
    html = """
    <tr>
      <td>1</td>
      <td><a href="/jajak/bbs/view.php?number=123&listnum=1">제정안 차량기술규정</a></td>
      <td>
        <a href="func/download.php?filename=rule-draft.pdf"><img alt="rule-draft" src="i.png" /></a>
      </td>
      <td>foo</td>
      <td>2026-06-01</td>
    </tr>
    """

    row = BeautifulSoup(html, "lxml").find("tr")
    assert row is not None
    assert rules_crawler._parse_list_row(row) == []


def test_chunker_manifest_skips_draft_records_and_normalizes_metadata(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    source_pdf = tmp_path / "rules.pdf"
    source_pdf.write_bytes(b"fake")

    manifest_path.write_text(
        json.dumps(
            [
                {
                    "source_post_id": 1,
                    "title": "차량기술 규정집",
                    "date": "2026-01-01",
                    "source_url": "https://example.org/rules.pdf",
                    "source_file": str(source_pdf),
                    "source_filename": "vehicle-tech.pdf",
                    "competition": "formula",
                    "document_type": "vehicle-technical",
                    "year": "2026",
                    "source_key": "normal",
                    "source_version": "v1",
                },
                {
                    "source_post_id": 2,
                    "title": "제정안 규정집",
                    "date": "2026-01-02",
                    "source_url": "https://example.org/draft.pdf",
                    "source_file": str(source_pdf),
                    "source_filename": "draft.pdf",
                    "competition": "formula",
                    "document_type": "vehicle-technical",
                    "year": "2026",
                    "source_key": "draft",
                    "source_version": "v2",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_chunk_formula_text(_input_path, _meta):
        return []

    def fake_build_generic_chunks(text, meta):
        return [
            {
                "text": text,
                "chunk_index": 0,
                "source_key": meta["source_key"],
                "competition": meta["competition"],
                "document_type": meta["document_type"],
                "source_filename": meta["source_filename"],
                "source_title": meta["source_title"],
                "year": meta["year"],
            },
        ]

    def fake_load_text(_path):
        return "rule body", None

    monkeypatch.setattr(rules_chunker, "_chunk_formula_text", fake_chunk_formula_text)
    monkeypatch.setattr(rules_chunker, "_build_generic_chunks", fake_build_generic_chunks)
    monkeypatch.setattr(rules_chunker, "_load_text", fake_load_text)

    chunks = rules_chunker.chunk_rules_manifest(
        manifest_path=manifest_path,
        output_path=tmp_path / "chunks.json",
    )

    assert len(chunks) == 1
    assert chunks[0]["source_key"] == "normal"
    assert chunks[0]["competition"] == "formula"
    assert chunks[0]["document_type"] == "vehicle-technical"
    assert chunks[0]["source_title"] == "차량기술 규정집"
