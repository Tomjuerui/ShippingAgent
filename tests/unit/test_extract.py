"""LLM 提取降级链、缓存与分块合并单测（WP-04 DoD）。"""

from __future__ import annotations

import pytest

from app.agent.nodes.extract import extract_uir
from app.agent.nodes.segment import segment_uir
from app.extraction.llm_client import (
    LLMClient,
    LLMUnavailable,
    SchemaRetryExceeded,
    _extract_json,
)
from app.extraction.schemas import (
    ChargeLine,
    DocumentExtraction,
    ExtractionChunk,
    SourceInfo,
    UIRBlock,
    UIRDocument,
)
from app.extraction.llm_client import ExtractionCallResult


def _extraction(ref="DN-2026-001", amount=40.0) -> DocumentExtraction:
    return DocumentExtraction(
        document_type="DEBIT NOTE",
        reference_no=ref,
        document_date="2026-09-20",
        charges=[
            ChargeLine(
                original_charge_name="H/D Fee",
                amount=amount,
                currency="USD",
                snippet="H/D Fee 40.00 USD",
            )
        ],
    )


class FakeBackend:
    def __init__(self, structured_result=None, structured_error=None, plain_results=None):
        self.structured_result = structured_result
        self.structured_error = structured_error
        self.plain_results = list(plain_results or [])
        self.structured_calls = []
        self.plain_calls = []

    def structured(self, schema, messages, method):
        self.structured_calls.append(method)
        if self.structured_error and method == "function_calling":
            raise self.structured_error
        return self.structured_result

    def plain(self, messages):
        self.plain_calls.append(messages)
        if not self.plain_results:
            return "not json"
        return self.plain_results.pop(0)


def test_function_calling_success_and_cache(settings, tmp_path):
    settings.llm.cache_enabled = True
    settings.llm.cache_dir = str(tmp_path / "cache")
    backend = FakeBackend(structured_result=_extraction())
    client = LLMClient(settings, backend=backend)
    first = client.extract("some billing text", "a.xlsx", "chunk-1")
    assert first.mode == "function_calling"
    assert first.cached is False
    assert first.extraction.source_chunk_id == "chunk-1"
    assert first.extraction.extraction_id
    second = client.extract("some billing text", "a.xlsx", "chunk-2")
    assert second.cached is True
    assert second.mode == "cache"
    assert len(backend.structured_calls) == 1
    assert second.extraction.source_chunk_id == "chunk-2"


def test_function_calling_falls_back_to_json_mode(settings):
    backend = FakeBackend(
        structured_result=_extraction(), structured_error=RuntimeError("tools not supported")
    )
    client = LLMClient(settings, backend=backend, use_cache=False)
    result = client.extract("text", "a.xlsx")
    assert result.mode == "json_mode"
    assert backend.structured_calls == ["function_calling", "json_mode"]


def test_prompt_schema_retry_after_invalid_json(settings):
    settings.llm.max_retries = 2
    backend = FakeBackend(
        structured_result=None,
        structured_error=RuntimeError("no structured output"),
        plain_results=["这不是 JSON", '```json\n{"document_type":"DEBIT NOTE","charges":[]}\n```'],
    )
    # 让 json_mode 也失败
    class Backend(FakeBackend):
        def structured(self, schema, messages, method):
            self.structured_calls.append(method)
            raise RuntimeError("unsupported")

    backend = Backend(plain_results=[
        "这不是 JSON",
        '```json\n{"document_type":"DEBIT NOTE","charges":[]}\n```',
    ])
    client = LLMClient(settings, backend=backend, use_cache=False)
    result = client.extract("text", "a.xlsx")
    assert result.mode == "prompt_schema"
    assert len(backend.plain_calls) == 2


def test_schema_retry_exceeded(settings):
    settings.llm.max_retries = 1

    class Backend(FakeBackend):
        def structured(self, schema, messages, method):
            raise RuntimeError("unsupported")

    backend = Backend(plain_results=["bad", "still bad"])
    client = LLMClient(settings, backend=backend, use_cache=False)
    with pytest.raises(SchemaRetryExceeded):
        client.extract("text", "a.xlsx")


def test_connection_error_raises_llm_unavailable(settings):
    class Backend(FakeBackend):
        def structured(self, schema, messages, method):
            raise ConnectionError("Connection refused")

    client = LLMClient(settings, backend=Backend(), use_cache=False)
    with pytest.raises(LLMUnavailable):
        client.extract("text", "a.xlsx")


def test_empty_text_skips_backend(settings):
    backend = FakeBackend(structured_result=_extraction())
    client = LLMClient(settings, backend=backend, use_cache=False)
    result = client.extract("   ", "a.xlsx", "chunk-empty")
    assert result.mode == "empty"
    assert result.extraction.charges == []
    assert backend.structured_calls == []


def test_cache_key_changes_with_text(settings):
    client = LLMClient(settings, backend=FakeBackend(), use_cache=False)
    assert client.cache_key("a") != client.cache_key("b")


def test_extract_json_handles_fences_and_extra_text():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('前缀 {"a": 2} 后缀') == {"a": 2}


def _uir_with_pages(pages: list[str]) -> UIRDocument:
    return UIRDocument(
        uir_id="uir-test",
        source=SourceInfo(file_name="multi.pdf", file_type="PDF", file_sha256="b" * 64),
        blocks=[
            UIRBlock(block_id=f"p{i}", kind="text", text=text, locator={"page": i})
            for i, text in enumerate(pages, start=1)
        ],
        parse_mode="pdf-text",
    )


def test_segment_splits_multi_document_pdf():
    uir = _uir_with_pages(
        [
            "DEBIT NOTE DN-1\nH/D Fee 40.00 USD\nmore text " * 5,
            "CREDIT NOTE CN-1\nDoc Fee -35.00 USD\nmore text " * 5,
        ]
    )
    chunks = segment_uir(uir, max_chars=12000)
    assert len(chunks) == 2
    assert "DN-1" in chunks[0].text
    assert "CN-1" in chunks[1].text


def test_segment_splits_long_text():
    uir = _uir_with_pages(["X" * 5000])
    chunks = segment_uir(uir, max_chars=1200)
    assert len(chunks) >= 4
    assert all(len(chunk.text) <= 1200 for chunk in chunks)


class StubClient:
    def __init__(self, by_keyword: dict[str, DocumentExtraction]):
        self.by_keyword = by_keyword
        self.calls = []

    def extract(self, text, source_name="", chunk_id=None):
        self.calls.append(chunk_id)
        for keyword, extraction in self.by_keyword.items():
            if keyword in text:
                return ExtractionCallResult(extraction, "function_calling", False, "stub", "v1")
        return ExtractionCallResult(DocumentExtraction(), "function_calling", False, "stub", "v1")


def test_extract_uir_keeps_distinct_documents_separate():
    uir = _uir_with_pages(["DEBIT NOTE DN-1 H/D Fee 40", "CREDIT NOTE CN-1 Doc Fee -35"])
    client = StubClient({"DN-1": _extraction("DN-1"), "CN-1": _extraction("CN-1", -35)})
    results = extract_uir(uir, client, max_chars=12000)
    assert len(results) == 2
    assert {result.reference_no for result in results} == {"DN-1", "CN-1"}


def test_extract_uir_merges_chunks_of_same_document():
    uir = _uir_with_pages(
        ["DEBIT NOTE DN-1 " + "A" * 2000, "continued " + "B" * 2000]
    )
    extraction = _extraction("DN-1")
    chunks = segment_uir(uir, max_chars=1500)
    assert len(chunks) >= 2
    client = StubClient({"DN-1": extraction, "continued": extraction})
    results = extract_uir(uir, client, max_chars=1500)
    assert len(results) == 1
