"""离线测试用的确定性 LLM 后端。"""

from __future__ import annotations

import json

from app.extraction.schemas import (
    ChargeLine,
    DocumentExtraction,
    ShipmentInfo,
    TotalEntry,
)


def extraction_dn(
    reference="DN-2026-001",
    amount=40.0,
    currency="USD",
    issuer="Destination Agent",
    recipient="Origin Agent",
    doc_type="DEBIT NOTE",
    date="2026-09-20",
    total=None,
    confidence=0.99,
    bl="OOLU4055331281",
    container="TIIU8975975",
    charge_name="H/D Fee",
) -> DocumentExtraction:
    return DocumentExtraction(
        document_type=doc_type,
        reference_no=reference,
        document_date=date,
        shipment_info=ShipmentInfo(bl_no=bl, container_no=container),
        issuer=issuer,
        recipient=recipient,
        charges=[
            ChargeLine(
                original_charge_name=charge_name,
                quantity=2,
                unit_price=20.0,
                amount=amount,
                currency=currency,
                payment_term="PREPAID",
                snippet=f"{charge_name} 2 20.00 {amount} {currency} PREPAID",
                confidence={
                    "original_charge_name": confidence,
                    "amount": confidence,
                    "currency": confidence,
                },
            )
        ],
        totals=[
            TotalEntry(
                kind="total", amount=amount if total is None else total, currency=currency
            )
        ],
        field_confidence={
            "document_type": confidence,
            "reference_no": confidence,
            "document_date": confidence,
            "issuer": confidence,
            "recipient": confidence,
            "bl_no": confidence,
            "container_no": confidence,
        },
    )


class KeywordBackend:
    """按输入文本关键词返回预置提取结果；无命中返回空提取。"""

    def __init__(self, rules: list[tuple[str, DocumentExtraction]] | None = None, default=None):
        self.rules = rules or []
        self.default = default or DocumentExtraction()
        self.calls = 0

    def _pick(self, messages):
        text = messages[-1]["content"] if messages else ""
        for keyword, extraction in self.rules:
            if keyword in text:
                return extraction.model_copy(deep=True)
        return self.default.model_copy(deep=True)

    def structured(self, schema, messages, method):
        self.calls += 1
        return self._pick(messages)

    def plain(self, messages):
        self.calls += 1
        return json.dumps(self._pick(messages).model_dump(mode="json"), ensure_ascii=False)
