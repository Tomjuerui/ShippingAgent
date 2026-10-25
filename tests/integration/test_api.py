"""Web API 集成测试（WP-09 DoD，同步内联执行，无外部服务）。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.extraction.llm_client import LLMClient
from app.server import create_app
from app.storage.file_exposer import PROXY_REGISTRY
from tests.conftest import make_excel
from tests.fakes import KeywordBackend, extraction_dn

EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _client(settings, backend=None):
    backend = backend or KeywordBackend([("H/D Fee", extraction_dn())])
    app = create_app(
        settings=settings,
        llm_factory=lambda: LLMClient(settings, backend=backend, use_cache=False),
        inline=True,
    )
    return TestClient(app), backend


def _upload(client, path, bills=None):
    files = [("files[]", (path.name, path.read_bytes(), EXCEL_MIME))]
    for name, payload in (bills or []):
        files.append(
            ("bills[]", (name, json.dumps(payload).encode("utf-8"), "application/json"))
        )
    return client.post("/api/batches", files=files)


def _bill(amount=35.0):
    return {
        "bill_id": "BILL-API-1",
        "document_info": {
            "document_type": "Debit Note",
            "reference_no": "DN-2026-001",
            "document_date": "2026-09-20",
        },
        "shipment_info": {
            "bl_no": "OOLU4055331281",
            "booking_no": "BK-2026-889",
            "container_no": "TIIU8975975",
        },
        "transaction_party": {"issuer": "Destination Agent", "recipient": "Origin Agent"},
        "charges": [
            {
                "charge_item": "Handling Charge",
                "original_charge_name": "H/D Fee",
                "quantity": 2,
                "unit_price": 20.0,
                "amount": amount,
                "currency": "USD",
                "payment_term": "PP",
            }
        ],
    }


def test_upload_status_records_export(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    client, _ = _client(settings)
    with client:
        response = _upload(client, excel)
        assert response.status_code == 202
        batch_id = response.json()["batch_id"]

        status = client.get(f"/api/batches/{batch_id}").json()
        assert status["status"] == "completed"
        assert status["progress"] == 100
        assert status["summary"]["records"] == 1

        records = client.get(f"/api/batches/{batch_id}/records").json()
        assert records["total"] == 1
        assert records["records"][0]["operation"] == "CREATE"

        export = client.get(f"/api/batches/{batch_id}/export")
        assert export.status_code == 200
        assert "attachment" in export.headers.get("content-disposition", "")
        payload = export.json()
        assert payload["batch_id"] == batch_id
        assert payload["summary"]["records"] == 1

        history = client.get("/api/batches").json()
        assert any(item["batch_id"] == batch_id for item in history["batches"])


def test_bills_upload_produces_update(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    client, _ = _client(settings)
    with client:
        response = _upload(client, excel, bills=[("bills.json", _bill())])
        batch_id = response.json()["batch_id"]
        records = client.get(
            f"/api/batches/{batch_id}/records?operation=UPDATE"
        ).json()
        assert records["total"] == 1
        assert records["filtered"] == 1
        record = records["records"][0]
        assert record["operation"] == "UPDATE"
        assert record["match_info"]["matched_bill_id"] == "BILL-API-1"
        assert record["charge_details"]["amount"] == 40.0


def test_records_filters(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    client, _ = _client(settings)
    with client:
        batch_id = _upload(client, excel).json()["batch_id"]
        none = client.get(f"/api/batches/{batch_id}/records?operation=PENDING_REVIEW").json()
        assert none["filtered"] == 0
        low = client.get(f"/api/batches/{batch_id}/records?low_confidence=true").json()
        assert low["filtered"] == 0
        bad = client.get(f"/api/batches/{batch_id}/records?operation=NOPE")
        assert bad.status_code == 400


def test_health(settings):
    client, _ = _client(settings)
    with client:
        data = client.get("/api/health").json()
        assert data["status"] == "ok"
        assert "ocr" in data and "llm" in data
        assert "available" in data["ocr"]


def test_rejects_unsupported_extension(settings, tmp_path):
    client, _ = _client(settings)
    with client:
        response = client.post(
            "/api/batches",
            files=[("files[]", ("evil.exe", b"MZ", "application/octet-stream"))],
        )
        assert response.status_code == 400
        assert "不支持" in response.json()["detail"]


def test_rejects_empty_upload(settings):
    client, _ = _client(settings)
    with client:
        response = client.post("/api/batches", files=[])
        assert response.status_code == 400


def test_bad_batch_id_and_missing_batch(settings):
    client, _ = _client(settings)
    with client:
        assert client.get("/api/batches/bad id").status_code == 400
        assert client.get("/api/batches/not-exist").status_code == 404
        assert client.get("/api/batches/not-exist/records").status_code == 404
        assert client.get("/api/batches/not-exist/export").status_code == 404


def test_static_pages_served(settings):
    client, _ = _client(settings)
    with client:
        index = client.get("/")
        assert index.status_code == 200
        assert "航运智能账单助手" in index.text
        css = client.get("/static/style.css")
        assert css.status_code == 200
        assert "topbar" in css.text
        js = client.get("/static/app.js")
        assert js.status_code == 200


def test_unknown_proxy_token(settings):
    client, _ = _client(settings)
    with client:
        assert client.get("/files/unknown/file.pdf").status_code == 404


def test_file_proxy_serves_registered_file(settings, tmp_path):
    path = tmp_path / "bill.pdf"
    path.write_bytes(b"%PDF-1.4")
    token = PROXY_REGISTRY.register(path, path.name, 5)
    client, _ = _client(settings)
    try:
        with client:
            response = client.get(f"/files/{token}/{path.name}")
            assert response.status_code == 200
            assert response.content == b"%PDF-1.4"
    finally:
        PROXY_REGISTRY.unregister(token)


def test_duplicate_upload_in_one_request(settings, tmp_path):
    excel = make_excel(tmp_path / "bill.xlsx")
    client, _ = _client(settings)
    with client:
        response = client.post(
            "/api/batches",
            files=[
                ("files[]", (excel.name, excel.read_bytes(), EXCEL_MIME)),
                ("files[]", (excel.name, excel.read_bytes(), EXCEL_MIME)),
            ],
        )
        batch_id = response.json()["batch_id"]
        status = client.get(f"/api/batches/{batch_id}").json()
        assert status["summary"]["input_files"] == 2
        assert status["summary"]["skipped_files"] == 1
        assert status["summary"]["records"] == 1
