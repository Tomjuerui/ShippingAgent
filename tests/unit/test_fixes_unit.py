"""缺陷修复回归（单元层）：探测假阳性、OCR 领域适配、付款方式、警告透出。"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.nodes.ocr_client import parse_ocr_payload
from app.extraction.schemas import BatchOutput
from app.governance.dictionaries import load_dicts
from app.storage.file_exposer import ExposeResult, FileExposer
from app.storage.outputs import write_outputs


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text or payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def post(self, url, json=None, timeout=None):
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(500, {"success": False, "message": "no response queued"})


# 服务真实返回（实测抓取）：HTTP 200 + success + 空内容
_REAL_EMPTY_RESPONSE = {
    "data": {"createDate": "2026-10-25 11:54:01", "fileName": "", "list": [], "orderType": "None", "pages_split": []},
    "success": True,
    "message": "文档处理成功",
    "error_code": None,
    "task_id": None,
}

# 服务真实返回（公网 dummy.pdf）：领域字段对象
_REAL_DOMAIN_RESPONSE = {
    "data": {
        "list": [
            {
                "fileType": "PAI-COLOADER-NOTICE",
                "mblNumber": "OOLU4055331281",
                "shipper": "ACME LOGISTICS",
                "consignee": "BEAR IMPORT LLC",
                "invoiceNumber": "ORFR1618731",
                "chargeList": [
                    {"chargeName": "WHARFAGE", "amount": "21.90", "currency": "USD"},
                    {"chargeName": "WHARFAGE ADMIN FEE", "amount": "5.00", "currency": "USD"},
                ],
                "ctrList": [{"ctrNumber": "JXJL4450493"}],
                "hold": {"holdType": "H", "holdDate": "", "holdDesc": "await docs"},
                "suspicious": "N",
                "isDetention": "N",
                "isDemurrage": "N",
                "page_index": [0, 1],
            }
        ],
        "orderType": "None",
        "pages_split": [[0, 1]],
    },
    "success": True,
    "message": "文档处理成功",
}


def test_ocr_adapter_renders_domain_structure():
    result = parse_ocr_payload(_REAL_DOMAIN_RESPONSE)
    assert result.text.strip()
    joined = "\n".join(line.text for line in result.lines)
    assert "OOLU4055331281" in joined
    assert "ACME LOGISTICS" in joined
    assert "WHARFAGE" in joined  # chargeList 渲染为费用行
    assert "JXJL4450493" in joined  # ctrList 渲染为箱号行
    assert "await docs" in joined  # hold 渲染


def test_ocr_adapter_keeps_empty_response_empty():
    result = parse_ocr_payload(_REAL_EMPTY_RESPONSE)
    assert not result.text.strip()
    assert not result.lines
    assert not result.tables


def test_probe_rejects_success_but_empty_response(settings):
    """缺陷 P1-3：HTTP 200 + success + 空内容必须判探测失败。"""
    settings.ocr.exposer = "s3"
    exposer = FileExposer(settings, session=FakeSession([FakeResponse(200, _REAL_EMPTY_RESPONSE)]))
    exposer._expose_with = lambda strategy, path, content_type: ExposeResult(
        url="https://bucket.example.com/probe.png", strategy=strategy, cleanup=lambda: None
    )
    ok, reason = exposer._probe("s3")
    assert ok is False
    assert "内容为空" in reason


def test_probe_accepts_nonempty_response(settings):
    settings.ocr.exposer = "s3"
    exposer = FileExposer(
        settings, session=FakeSession([FakeResponse(200, {"success": True, "data": {"text": "probe ok"}})])
    )
    exposer._expose_with = lambda strategy, path, content_type: ExposeResult(
        url="https://bucket.example.com/probe.png", strategy=strategy, cleanup=lambda: None
    )
    ok, reason = exposer._probe("s3")
    assert ok is True


def test_payment_term_single_letters():
    dicts = load_dicts()
    assert dicts.payment.normalize("C") == "CC"
    assert dicts.payment.normalize("P") == "PP"
    assert dicts.payment.normalize("c") == "CC"
    assert dicts.payment.normalize("PREPAID") == "PP"
    assert dicts.payment.normalize("COLLECT") == "CC"


def test_batch_output_warnings_reach_summary(tmp_path: Path):
    """缺陷 P2-8：批警告必须落到 summary.json / batch.json。"""
    batch = BatchOutput(batch_id="b-warn", processed_at="2026-10-25T00:00:00", warnings=["账单 JSON 读取失败 bad.json: x"])
    write_outputs(batch, tmp_path)
    summary = json.loads((tmp_path / "b-warn" / "summary.json").read_text(encoding="utf-8"))
    assert summary["warnings"] == ["账单 JSON 读取失败 bad.json: x"]
    payload = json.loads((tmp_path / "b-warn" / "batch.json").read_text(encoding="utf-8"))
    assert payload["warnings"] == ["账单 JSON 读取失败 bad.json: x"]
