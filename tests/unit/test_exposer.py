"""文件外发策略链与 OCR 客户端单测（WP-03 DoD，全部离线）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.nodes.ocr_client import (
    OCRClient,
    OCRLowQuality,
    OCRUnavailable,
    ocr_result_to_uir,
    parse_ocr_payload,
)
from app.extraction.schemas import SourceInfo
from app.storage.file_exposer import (
    ExposeError,
    ExposeResult,
    FileExposer,
    PROXY_REGISTRY,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses=None, put_url="https://tmp.example.com/file.pdf"):
        self.responses = list(responses or [])
        self.put_url = put_url
        self.posts = []
        self.puts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        if not self.responses:
            return FakeResponse(500, {"success": False, "message": "no response queued"})
        return self.responses.pop(0)

    def put(self, url, data=None, timeout=None):
        self.puts.append((url, data, timeout))
        return FakeResponse(200, text=self.put_url)


def test_no_strategy_configured(settings, tmp_path):
    settings.ocr.exposer = "auto"
    settings.ocr.proxy_base_url = ""
    settings.ocr.temp_host_enabled = False
    exposer = FileExposer(settings)
    assert exposer.validate()[0] is None
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(ExposeError):
        exposer.expose(path)


def test_proxy_strategy_registers_and_cleans_up(settings, tmp_path):
    settings.ocr.exposer = "proxy"
    settings.ocr.proxy_base_url = "https://files.example.com"
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.4")
    exposer = FileExposer(settings, session=FakeSession())
    result = exposer.expose(path, "application/pdf")
    assert result.strategy == "proxy"
    assert result.url.startswith("https://files.example.com/files/")
    token = result.url.split("/files/")[1].split("/")[0]
    assert PROXY_REGISTRY.resolve(token) is not None
    result.close()
    assert PROXY_REGISTRY.resolve(token) is None


def test_url_passthrough(settings):
    settings.ocr.exposer = "auto"
    exposer = FileExposer(settings, session=FakeSession())
    result = exposer.expose("https://example.com/bill.pdf")
    assert result.strategy == "passthrough"
    assert result.url == "https://example.com/bill.pdf"


def test_temp_host_warns(settings, tmp_path):
    settings.ocr.exposer = "temp_host"
    settings.ocr.temp_host_enabled = True
    settings.ocr.temp_host_url = "https://transfer.sh"
    session = FakeSession(put_url="https://transfer.sh/abc/bill.pdf")
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.4")
    result = FileExposer(settings, session=session).expose(path)
    assert result.strategy == "temp_host"
    assert any("外泄" in warning for warning in result.warnings)
    assert session.puts


def test_s3_strategy_uploads_private_and_deletes(monkeypatch, settings, tmp_path):
    monkeypatch.setenv("OSS_ENDPOINT", "https://oss.example.com")
    monkeypatch.setenv("OSS_BUCKET", "bucket")
    monkeypatch.setenv("OSS_AK", "ak")
    monkeypatch.setenv("OSS_SK", "sk")
    settings.ocr.exposer = "s3"

    calls = {"put": [], "delete": []}

    class FakeS3:
        def put_object(self, **kwargs):
            calls["put"].append(kwargs)

        def generate_presigned_url(self, operation, Params, ExpiresIn):
            return f"https://oss.example.com/{Params['Key']}?expires={ExpiresIn}"

        def delete_object(self, **kwargs):
            calls["delete"].append(kwargs)

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: FakeS3())
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.4")
    result = FileExposer(settings).expose(path, "application/pdf")
    assert result.strategy == "s3"
    assert result.url.startswith("https://oss.example.com/")
    assert calls["put"][0]["ACL"] == "private"
    result.close()
    assert calls["delete"]


def test_auto_prefers_proxy_over_passthrough(settings, tmp_path):
    settings.ocr.exposer = "auto"
    settings.ocr.proxy_base_url = "https://files.example.com"
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.4")
    session = FakeSession(
        responses=[FakeResponse(200, {"success": True, "data": {"text": "probe ok"}})]
    )
    result = FileExposer(settings, session=session).expose(path)
    assert result.strategy == "proxy"
    result.close()


class StubExposer:
    def __init__(self):
        self.closed = 0

    def expose(self, path, content_type=None):
        def cleanup():
            self.closed += 1

        return ExposeResult(
            url="https://files.example.com/bill.pdf", strategy="proxy", cleanup=cleanup
        )


def test_ocr_client_success_and_cleanup(settings, tmp_path):
    settings.ocr.min_text_chars = 5
    session = FakeSession(
        responses=[FakeResponse(200, {"success": True, "data": {"text": "DEBIT NOTE 40.00 USD"}})]
    )
    exposer = StubExposer()
    client = OCRClient(settings, exposer=exposer, session=session, sleep=lambda _: None)
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.4")
    result = client.process(path)
    assert "DEBIT NOTE" in result.text
    assert exposer.closed == 1


def test_ocr_retry_then_success(settings, tmp_path):
    settings.ocr.retries = 3
    settings.ocr.min_text_chars = 3
    sleeps = []
    session = FakeSession(
        responses=[
            FakeResponse(500, {"message": "err"}),
            FakeResponse(503, {"message": "err"}),
            FakeResponse(200, {"data": {"text": "OK DEBIT NOTE"}}),
        ]
    )
    client = OCRClient(
        settings,
        exposer=StubExposer(),
        session=session,
        sleep=sleeps.append,
        backoff=(0, 0, 0),
    )
    result = client.process(tmp_path / "bill.pdf")
    assert "DEBIT NOTE" in result.text
    assert len(session.posts) == 3
    assert sleeps == [0, 0]


def test_ocr_exhausted(settings, tmp_path):
    settings.ocr.retries = 2
    session = FakeSession(
        responses=[FakeResponse(500, {}), FakeResponse(500, {})]
    )
    client = OCRClient(
        settings, exposer=StubExposer(), session=session, sleep=lambda _: None, backoff=(0, 0)
    )
    with pytest.raises(OCRUnavailable):
        client.process(tmp_path / "bill.pdf")


def test_ocr_low_quality(settings, tmp_path):
    settings.ocr.min_text_chars = 40
    session = FakeSession(responses=[FakeResponse(200, {"data": {"text": "x"}})])
    client = OCRClient(settings, exposer=StubExposer(), session=session, sleep=lambda _: None)
    with pytest.raises(OCRLowQuality):
        client.process(tmp_path / "bill.pdf")


def test_ocr_success_false_is_unavailable(settings, tmp_path):
    session = FakeSession(
        responses=[FakeResponse(200, {"success": False, "message": "bad file", "error_code": "E1"})]
    )
    client = OCRClient(settings, exposer=StubExposer(), session=session, sleep=lambda _: None)
    with pytest.raises(OCRUnavailable):
        client.process(tmp_path / "bill.pdf")


def test_parse_ocr_payload_pages_lines_tables():
    payload = {
        "data": {
            "pages": [
                {
                    "page": 1,
                    "lines": [
                        {"text": "DEBIT NOTE DN-1", "confidence": 0.98},
                        {"text": "H/D Fee 40.00 USD", "confidence": 0.97},
                    ],
                    "tables": [
                        {
                            "rows": [
                                ["Charge", "Amount"],
                                ["THC", "100.00"],
                            ]
                        }
                    ],
                },
                {"page": 2, "text": "Total 140.00 USD"},
            ]
        }
    }
    result = parse_ocr_payload(payload)
    assert "DEBIT NOTE DN-1" in result.text
    assert "Total 140.00" in result.text
    assert result.tables and result.tables[0].rows[0] == ["Charge", "Amount"]
    assert result.confidence is not None and result.confidence > 0.9


def test_parse_ocr_payload_word_level_and_cells():
    result = parse_ocr_payload(
        {
            "words": [{"text": "DEBIT"}, {"text": "NOTE"}],
            "table": {
                "cells": [
                    {"row": 0, "col": 0, "text": "Charge"},
                    {"row": 0, "col": 1, "text": "Amount"},
                    {"row": 1, "col": 0, "text": "THC"},
                    {"row": 1, "col": 1, "text": "40"},
                ]
            },
        }
    )
    assert "DEBIT NOTE" in result.text
    assert result.tables[0].rows == [["Charge", "Amount"], ["THC", "40"]]


def test_ocr_result_to_uir_keeps_page_and_table_locator():
    result = parse_ocr_payload(
        {"pages": [{"page": 3, "lines": [{"text": "DEBIT NOTE", "bbox": [1, 2, 3, 4]}],
                    "tables": [{"rows": [["Charge", "Amount"], ["THC", "40"]]}]}]}
    )
    uir = ocr_result_to_uir(
        result,
        SourceInfo(file_name="scan.pdf", file_type="PDF", file_sha256="c" * 64),
        "node-1",
        "uir-ocr",
        min_chars=5,
    )
    assert uir.parse_mode == "ocr"
    text_block = next(block for block in uir.blocks if block.kind == "text")
    assert text_block.locator["page"] == 3
    table_block = next(block for block in uir.blocks if block.kind == "table")
    assert table_block.table.rows == [["THC", "40"]]
