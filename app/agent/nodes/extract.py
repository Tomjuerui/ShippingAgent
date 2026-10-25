"""LLM 提取节点（FR-06 / D6）：分块调用 + 同单据块合并。"""

from __future__ import annotations

from app.agent.nodes.segment import segment_uir
from app.extraction.llm_client import LLMClient
from app.extraction.schemas import ChargeLine, DocumentExtraction, TotalEntry, UIRDocument


def _dedupe_charges(charges: list[ChargeLine]) -> list[ChargeLine]:
    seen: set[tuple] = set()
    result: list[ChargeLine] = []
    for charge in charges:
        key = (
            (charge.original_charge_name or "").strip().lower(),
            charge.amount,
            (charge.currency or "").strip().upper(),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(charge)
    return result


def _dedupe_totals(totals: list[TotalEntry]) -> list[TotalEntry]:
    seen: set[tuple] = set()
    result: list[TotalEntry] = []
    for total in totals:
        key = (total.kind, total.amount, (total.currency or "").upper())
        if key in seen:
            continue
        seen.add(key)
        result.append(total)
    return result


def merge_extractions(extractions: list[DocumentExtraction]) -> DocumentExtraction:
    if not extractions:
        return DocumentExtraction()
    base = extractions[0].model_copy(deep=True)
    scalar_fields = ("document_type", "reference_no", "document_date", "issuer", "recipient", "direction_statement")
    shipment_fields = ("bl_no", "booking_no", "container_no", "vessel_voyage", "pol", "pod")
    charges = list(base.charges)
    totals = list(base.totals)
    confidence = dict(base.field_confidence)
    for extra in extractions[1:]:
        for field in scalar_fields:
            if not getattr(base, field) and getattr(extra, field):
                setattr(base, field, getattr(extra, field))
        for field in shipment_fields:
            if not getattr(base.shipment_info, field) and getattr(extra.shipment_info, field):
                setattr(base.shipment_info, field, getattr(extra.shipment_info, field))
        for key, value in extra.field_confidence.items():
            confidence[key] = min(confidence.get(key, 1.0), value)
        charges.extend(extra.charges)
        totals.extend(extra.totals)
    base.charges = _dedupe_charges(charges)
    base.totals = _dedupe_totals(totals)
    base.field_confidence = confidence
    return base


def _group_by_document(extractions: list[DocumentExtraction]) -> list[list[DocumentExtraction]]:
    refs = [extra.reference_no.strip() for extra in extractions if extra.reference_no and extra.reference_no.strip()]
    if len(set(refs)) <= 1:
        return [extractions]
    groups: list[list[DocumentExtraction]] = []
    last_ref: str | None = None
    for extra in extractions:
        ref = extra.reference_no.strip() if extra.reference_no else None
        if ref is None and groups:
            groups[-1].append(extra)
            continue
        if ref != last_ref or not groups:
            groups.append([extra])
            last_ref = ref
        else:
            groups[-1].append(extra)
    return groups


def extract_uir(
    uir: UIRDocument, client: LLMClient, max_chars: int = 12000
) -> list[DocumentExtraction]:
    chunks = segment_uir(uir, max_chars=max_chars)
    extractions: list[DocumentExtraction] = []
    for chunk in chunks:
        result = client.extract(
            chunk.text, source_name=chunk.source.file_name, chunk_id=chunk.chunk_id
        )
        extractions.append(result.extraction)
    if not extractions:
        return []
    return [merge_extractions(group) for group in _group_by_document(extractions)]
