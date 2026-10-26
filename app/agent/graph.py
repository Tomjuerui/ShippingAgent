"""LangGraph 状态机编排（D7 / WP-08）。"""

from __future__ import annotations

import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.arap import determine_posting_type, document_amount
from app.agent.nodes.assemble import assemble_records
from app.agent.nodes.extract import extract_uir
from app.agent.nodes.ingest import IngestedFile, discover_inputs
from app.agent.nodes.match import decide_operation, match_charge
from app.agent.nodes.normalize import map_document_type, normalize_document
from app.agent.nodes.ocr_client import (
    OCRClient,
    OCRLowQuality,
    OCRUnavailable,
    ocr_result_to_uir,
)
from app.agent.nodes.parse_excel import parse_excel
from app.agent.nodes.parse_textpdf import parse_textpdf
from app.agent.nodes.parse_word import parse_word
from app.agent.nodes.review import finalize_review
from app.agent.nodes.unpack_eml import Payload, unpack_file
from app.agent.nodes.validate import validate_document
from app.agent.state import (
    ChargeDecision,
    DocWork,
    ExtractionWork,
    FileTask,
    PipelineState,
    UIRWork,
)
from app.config.settings import Settings, get_settings
from app.extraction.llm_client import (
    LLMClient,
    LLMUnavailable,
    SchemaRetryExceeded,
)
from app.extraction.schemas import (
    AuditTrailOut,
    BatchMeta,
    BatchOutput,
    BatchStatus,
    BatchSummary,
    BillRecord,
    DocumentInfoOut,
    FileProgress,
    FileResultOut,
    FinancialAttributesOut,
    MatchKeyCoverage,
    MatchLevel,
    OperationCounts,
    Operation,
    SourceInfo,
    SourceOut,
    UIRBlock,
    UIRDocument,
    UIRTable,
    ValidationOut,
)
from app.governance.dictionaries import load_dicts
from app.storage.bills_repo import BillsRepository, load_bills
from app.storage.outputs import (
    clone_duplicate_records,
    load_batch_output,
    load_ledger,
    make_batch_id,
    now_iso,
    save_ledger,
    write_outputs,
)
from app.storage.status import StatusStore

_PROGRESS_WEIGHT = {
    "queued": 0,
    "unpacking": 10,
    "parsing": 30,
    "ocr": 30,
    "extracting": 65,
    "deciding": 85,
    "completed": 100,
    "failed": 100,
    "skipped": 100,
}
_WEB_STATUS = {
    "queued": "queued",
    "unpacking": "ocr",
    "parsing": "ocr",
    "ocr": "ocr",
    "extracting": "extracting",
    "deciding": "deciding",
    "completed": "completed",
    "failed": "failed",
    "skipped": "skipped",
}


@dataclass
class RunContext:
    settings: Settings
    batch_id: str
    output_dir: Path
    work_dir: Path
    status_store: StatusStore
    repo: BillsRepository
    llm_client: LLMClient | None = None
    ocr_client: OCRClient | None = None
    deterministic: bool = False
    ignore_ledger: bool = False
    extraction_mode: str = "llm"
    created_at: str = ""
    total_files: int = 0
    ledger: dict[str, str] = field(default_factory=dict)


def _operation_counts(records: list[BillRecord]) -> OperationCounts:
    counts = OperationCounts()
    for record in records:
        value = record.operation.value if isinstance(record.operation, Operation) else str(record.operation)
        if hasattr(counts, value):
            setattr(counts, value, getattr(counts, value) + 1)
    return counts


def _file_progress(task: FileTask) -> FileProgress:
    return FileProgress(
        file_name=task.file_name,
        file_sha256=task.sha256,
        status=_WEB_STATUS.get(task.status, task.status),
        stage=task.status,
        record_count=task.record_count,
        review_reason=task.review_reason,
        message=task.message,
    )


def _sync_status(
    ctx: RunContext, state: PipelineState, status: str = "running", error: str | None = None
) -> None:
    files = state.get("files", []) or []
    records = state.get("records", []) or []
    total = ctx.total_files or len(files)
    processed = sum(1 for task in files if task.status in {"completed", "failed", "skipped"})
    if status == "completed":
        progress = 100
    elif total:
        progress = int(sum(_PROGRESS_WEIGHT.get(task.status, 0) for task in files) / total)
    else:
        progress = 0
    payload = BatchStatus(
        batch_id=ctx.batch_id,
        status=status,
        progress=progress,
        total_files=total,
        processed_files=processed,
        files=[_file_progress(task) for task in files],
        operation_counts=_operation_counts(records),
        created_at=ctx.created_at,
        finished_at=now_iso(ctx.deterministic) if status in {"completed", "failed"} else None,
        error=error,
    )
    try:
        ctx.status_store.write(payload)
    except Exception:
        pass


def _file_result(task: FileTask) -> FileResultOut:
    status = task.status if task.status in {"completed", "failed", "skipped"} else "completed"
    if task.status == "skipped":
        status = "skipped"
    return FileResultOut(
        file_name=task.file_name,
        file_sha256=task.sha256,
        status=status,
        parse_mode=",".join(sorted(set(task.parse_modes))) or task.detected_type,
        document_count=task.document_count,
        record_count=task.record_count,
        review_reason=task.review_reason,
        message=task.message,
        duplicate_of=task.duplicate_of,
    )


# --------------------------------------------------------------------------- #
# 空提取守卫（缺陷 P1-4）：LLM 返回结构合法但零费用时不得静默通过
# --------------------------------------------------------------------------- #
_AMOUNT_HINT = re.compile(
    r"(?:USD|CNY|RMB|EUR|HKD|JPY|GBP)\s*[:：]?\s*[-+]?[\d,]+"
    r"|\$\s*[-+]?[\d,]+"
    r"|[-+]?[\d,]+\.\d{2}\b",
    re.IGNORECASE,
)
_BILL_TITLE_HINT = re.compile(
    r"debit\s*note|credit\s*note|\binvoice\b|\bstatement\b|arrival\s*notice"
    r"|total\s+amount|grand\s+total|借记|贷记|发票|对账单|账单",
    re.IGNORECASE,
)


def _looks_like_bill_text(text: str) -> bool:
    return bool(_AMOUNT_HINT.search(text or ""))


def _looks_like_bill_title(text: str) -> bool:
    return bool(_BILL_TITLE_HINT.search(text or ""))


def _empty_extraction_placeholder(work: ExtractionWork, settings: Settings) -> BillRecord | None:
    """零费用但原文具备账单特征 → 生成占位复核记录，杜绝静默漏记。

    触发条件（二选一）：
    1. LLM 识别出单据（document_type / reference_no）但零费用，且原文含金额特征；
    2. LLM 一无所获，但原文同时具备账单标题特征与金额特征。
    """
    identifying = next(
        (
            extraction
            for extraction in work.extractions
            if extraction.document_type or extraction.reference_no
        ),
        None,
    )
    source_text = work.uir.full_text()
    if not _looks_like_bill_text(source_text):
        return None
    if identifying is None and not _looks_like_bill_title(source_text):
        return None
    sha = work.file.sha256
    src = work.uir.source
    doc_type = None
    ref = None
    if identifying is not None:
        doc_type = map_document_type(identifying.document_type) or identifying.document_type
        ref = identifying.reference_no
        suspicious_line = (
            f"LLM 识别到单据（类型 {identifying.document_type} / 编号 {ref}），但未返回任何费用行"
        )
    else:
        suspicious_line = "LLM 未返回任何单据信息，但原文具备账单标题与金额特征"
    reasoning = [
        f"来源文件：{work.file.file_name}（{work.file.detected_type}）；SHA-256 {sha[:12]}…",
        suspicious_line,
        "疑似空提取异常（模型能力或结构化输出降级问题）",
        "按 fail-safe 送人工复核（EXTRACTION_EMPTY），不自动入账、不写入幂等账本",
    ]
    return BillRecord(
        record_id=f"{sha[:8]}-E001",
        operation=Operation.PENDING_REVIEW,
        document_info=DocumentInfoOut(
            document_type=doc_type,
            reference_no=ref,
            document_date=identifying.document_date if identifying is not None else None,
        ),
        financial_attributes=FinancialAttributesOut(
            accounting_entity=settings.accounting_entity.name,
            posting_type=None,
        ),
        validation=ValidationOut(passed=True, issues=[]),
        confidence=0.0,
        low_confidence=True,
        review_reason="EXTRACTION_EMPTY",
        source=SourceOut(
            file_name=src.file_name or work.file.file_name,
            file_type=src.file_type,
            file_sha256=sha,
            email_subject=src.email_subject,
            email_from=src.email_from,
            email_message_id=src.email_message_id,
            email_date=src.email_date,
            content_node_id=work.uir.content_node_id,
        ),
        audit_trail=AuditTrailOut(
            original_text_snippet=source_text[:200],
            ai_reasoning=reasoning,
        ),
    )


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #
def _ingest_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        ingested, messages = discover_inputs(state.get("input_paths", []))
        warnings = list(state.get("warnings", [])) + messages
        tasks: list[FileTask] = []
        prebuilt: list[BillRecord] = []
        seen: dict[str, FileTask] = {}
        for item in ingested:
            task = FileTask(
                path=item.path,
                file_name=item.file_name,
                sha256=item.sha256,
                detected_type=item.detected_type,
                warnings=list(item.warnings),
            )
            if item.sha256 in seen:
                task.status = "skipped"
                task.duplicate_of = seen[item.sha256].file_name
                task.message = f"同批重复文件，已跳过（首个文件：{seen[item.sha256].file_name}）"
            else:
                seen[item.sha256] = task
                previous_batch = ctx.ledger.get(item.sha256)
                if (
                    previous_batch
                    and previous_batch != ctx.batch_id
                    and not ctx.ignore_ledger
                ):
                    previous = load_batch_output(ctx.output_dir, previous_batch)
                    if previous:
                        clones = clone_duplicate_records(previous, item.sha256, previous_batch)
                        if clones:
                            prebuilt.extend(clones)
                            task.status = "skipped"
                            task.duplicate_of = previous_batch
                            nochange = sum(
                                1 for clone in clones if clone.operation == Operation.NOCHANGE
                            )
                            pending = len(clones) - nochange
                            breakdown = f"NOCHANGE {nochange} 条"
                            if pending:
                                breakdown += f"，维持待复核 {pending} 条"
                            task.message = (
                                f"重复提交（DUPLICATE_SUBMISSION）：与批次 {previous_batch} 文件哈希相同，"
                                f"输出 {len(clones)} 条记录（{breakdown}），未重复入账"
                            )
                        else:
                            task.status = "skipped"
                            task.duplicate_of = previous_batch
                            task.message = "重复提交，历史批次中未找到对应记录"
                    else:
                        task.status = "skipped"
                        task.duplicate_of = previous_batch
                        task.message = "重复提交，但历史批次输出已不存在"
            tasks.append(task)
        ctx.total_files = len(tasks)
        result_state = {"files": tasks, "prebuilt_records": prebuilt, "warnings": warnings}
        merged_state = dict(state)
        merged_state.update(result_state)
        _sync_status(ctx, merged_state)  # type: ignore[arg-type]
        return result_state

    return node


def _unpack_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        files = state.get("files", [])
        warnings = list(state.get("warnings", []))
        for task in files:
            if task.status != "queued":
                continue
            task.status = "unpacking"
            _sync_status(ctx, state)
            try:
                ingested = IngestedFile(
                    path=task.path,
                    file_name=task.file_name,
                    sha256=task.sha256,
                    size=task.path.stat().st_size,
                    detected_type=task.detected_type,
                    extension_type=task.detected_type,
                )
                result = unpack_file(
                    ingested,
                    ctx.work_dir / task.sha256[:8],
                    ctx.settings.pipeline.max_eml_depth,
                )
                task.payloads = result.payloads
                task.content_tree = result.root
                task.warnings.extend(result.warnings)
                warnings.extend(result.warnings)
                if not result.payloads:
                    task.status = "failed"
                    task.review_reason = "OTHER"
                    task.message = "未找到可解析的正文或附件"
                else:
                    task.status = "parsing"
            except Exception as exc:
                task.status = "failed"
                task.review_reason = "OTHER"
                task.message = f"解包失败：{exc}"
                warnings.append(f"{task.file_name} 解包失败：{exc}")
            _sync_status(ctx, state)
        return {"files": files, "warnings": warnings}

    return node


def _body_uir(payload: Payload, task: FileTask, uir_id: str) -> UIRDocument:
    blocks: list[UIRBlock] = []
    text = payload.data.decode("utf-8", errors="ignore") if payload.data else ""
    if text.strip():
        blocks.append(
            UIRBlock(
                block_id=f"{uir_id}-body",
                kind="text",
                text=text,
                locator={"source": "email_body"},
            )
        )
    for index, rows in enumerate(payload.body_tables, start=1):
        matrix = [[str(cell) for cell in row] for row in rows if row]
        if not matrix:
            continue
        header = matrix[0]
        looks_header = any(
            keyword in " ".join(header).lower()
            for keyword in ("charge", "description", "amount", "currency", "费用", "金额")
        )
        headers, body = (header, matrix[1:]) if looks_header else ([], matrix)
        blocks.append(
            UIRBlock(
                block_id=f"{uir_id}-t{index}",
                kind="table",
                table=UIRTable(
                    table_id=f"{uir_id}-t{index}",
                    headers=headers,
                    rows=body,
                    locator={"source": "email_body", "table": index},
                ),
            )
        )
    return UIRDocument(
        uir_id=uir_id,
        source=payload.source,
        content_node_id=payload.node_id,
        blocks=blocks,
        parse_mode="eml_body",
        parse_quality="high",
    )


def _parse_payload(
    payload: Payload, task: FileTask, ctx: RunContext, index: int
) -> UIRDocument | None:
    uir_id = f"uir-{task.sha256[:6]}-{index:02d}-{payload.sha256[:8]}"
    file_type = payload.file_type
    if payload.is_body or file_type in {"text", "eml_body"}:
        return _body_uir(payload, task, uir_id)
    if file_type == "xlsx" or file_type == "xls":
        return parse_excel(payload, uir_id)
    if file_type == "docx" or file_type == "doc":
        return parse_word(payload, uir_id)
    if file_type == "pdf":
        uir = parse_textpdf(payload, ctx.settings.ocr.min_text_chars, uir_id)
        if uir is not None:
            return uir
    if file_type in {"pdf", "png", "jpeg", "tiff"}:
        if payload.path is None:
            raise OCRUnavailable("待 OCR 文件缺少本地路径")
        client = ctx.ocr_client or OCRClient(ctx.settings)
        ctx.ocr_client = client
        result = client.process(payload.path)
        return ocr_result_to_uir(
            result,
            payload.source,
            payload.node_id,
            uir_id,
            ctx.settings.ocr.min_text_chars,
        )
    raise ValueError(f"不支持的文件类型：{file_type}")


def _parse_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        uirs: list[UIRWork] = []
        files = state.get("files", [])
        for task in files:
            if task.status != "parsing":
                continue
            task_uirs: list[UIRWork] = []
            errors: list[tuple[str, str]] = []
            for index, payload in enumerate(task.payloads, start=1):
                try:
                    uir = _parse_payload(payload, task, ctx, index)
                    if uir is not None and uir.blocks:
                        task_uirs.append(UIRWork(file=task, uir=uir))
                        task.parse_modes.append(uir.parse_mode)
                except OCRUnavailable as exc:
                    errors.append(("OCR_UNAVAILABLE", f"{payload.file_name}: {exc}"))
                except OCRLowQuality as exc:
                    errors.append(("OCR_LOW_QUALITY", f"{payload.file_name}: {exc}"))
                except Exception as exc:
                    errors.append(("OTHER", f"{payload.file_name}: {exc}"))
            if task_uirs:
                uirs.extend(task_uirs)
                task.status = "extracting"
                if errors:
                    task.message = "；".join(message for _, message in errors[:3])
                    task.review_reason = errors[0][0]
            else:
                task.status = "failed"
                task.review_reason = errors[0][0] if errors else "OTHER"
                task.message = "；".join(message for _, message in errors[:3]) or "未解析出内容"
            _sync_status(ctx, state)
        return {"uirs": uirs, "files": files}

    return node


def _extract_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        uirs = state.get("uirs", [])
        if not uirs:
            return {"extractions": []}
        client = ctx.llm_client or LLMClient(ctx.settings)
        ctx.llm_client = client
        for work in uirs:
            work.file.status = "extracting"
        _sync_status(ctx, state)

        results: list[ExtractionWork | None] = [None] * len(uirs)
        errors: list[tuple[int, str, str]] = []
        max_chars = ctx.settings.pipeline.max_text_chars_per_chunk
        workers = max(1, ctx.settings.web.concurrency)

        def _submit(indexes: set[int]) -> None:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(extract_uir, uirs[index].uir, client, max_chars): index
                    for index in indexes
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        extractions = future.result()
                        results[index] = ExtractionWork(
                            file=uirs[index].file,
                            uir=uirs[index].uir,
                            extractions=extractions,
                        )
                    except LLMUnavailable as exc:
                        errors.append((index, "LLM_UNAVAILABLE", str(exc)))
                    except SchemaRetryExceeded as exc:
                        errors.append((index, "SCHEMA_RETRY_EXCEEDED", str(exc)))
                    except Exception as exc:
                        errors.append((index, "OTHER", str(exc)))

        _submit(set(range(len(uirs))))

        # 批级重试（缺陷 P2-6）：LLM 端点瞬时不可用（超时/5xx/限流冷却）时，
        # 等待一个退避窗口后对失败 UIR 重试一次，避免高峰期整批失败。
        retry_seconds = getattr(ctx.settings.llm, "unavailable_retry_seconds", 0) or 0
        retry_indexes = {index for index, reason, _ in errors if reason == "LLM_UNAVAILABLE"}
        if retry_seconds > 0 and retry_indexes:
            time.sleep(retry_seconds)
            _submit(set(retry_indexes))
            # 重试成功的条目移除旧错误；重试仍失败的保留原错误（新错误为重复项，忽略）
            errors = [
                entry
                for entry in errors
                if not (entry[0] in retry_indexes and results[entry[0]] is not None)
            ]

        by_file: dict[str, list[ExtractionWork]] = {}
        for work in results:
            if work is not None:
                by_file.setdefault(work.file.sha256, []).append(work)
        error_by_file: dict[str, tuple[str, str]] = {}
        for index, reason, message in errors:
            task = uirs[index].file
            error_by_file.setdefault(task.sha256, (reason, message))

        extractions: list[ExtractionWork] = []
        for work in results:
            if work is None:
                continue
            with_charges = [extra for extra in work.extractions if extra.charges]
            if with_charges:
                work.extractions = with_charges
                extractions.append(work)
        placeholders: dict[str, list[BillRecord]] = {}
        for task in state.get("files", []):
            if task.status != "extracting":
                continue
            successful = by_file.get(task.sha256, [])
            has_charges = any(
                any(extra.charges for extra in work.extractions) for work in successful
            )
            if has_charges:
                task.status = "deciding"
                if task.sha256 in error_by_file:
                    reason, message = error_by_file[task.sha256]
                    task.message = f"部分内容提取失败：{message}"
                    task.review_reason = reason
            elif task.sha256 in error_by_file:
                reason, message = error_by_file[task.sha256]
                task.status = "failed"
                task.review_reason = reason
                task.message = message
            else:
                # 空提取守卫（缺陷 P1-4）：识别到单据却零费用、且原文含金额特征，
                # 生成 EXTRACTION_EMPTY 占位复核记录，不允许静默漏记
                task.status = "completed"
                task.message = "未提取到费用行（非账单内容或无可入账费用）"
                suspicious = [
                    _empty_extraction_placeholder(work, ctx.settings)
                    for work in successful
                ]
                suspicious = [record for record in suspicious if record is not None]
                if suspicious:
                    for order, record in enumerate(suspicious, start=1):
                        record.record_id = f"{task.sha256[:8]}-E{order:03d}"
                    placeholders[task.sha256] = suspicious
                    task.review_reason = "EXTRACTION_EMPTY"
                    task.message = (
                        f"识别到单据但未提取到任何费用行（原文含金额特征），"
                        f"已生成 {len(suspicious)} 条 EXTRACTION_EMPTY 待复核记录"
                    )
            _sync_status(ctx, state)
        prebuilt = list(state.get("prebuilt_records", []))
        prebuilt.extend(record for records in placeholders.values() for record in records)
        return {
            "extractions": extractions,
            "files": state.get("files", []),
            "prebuilt_records": prebuilt,
        }

    return node


def _normalize_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        documents: list[DocWork] = []
        files = state.get("files", [])
        for work in state.get("extractions", []):
            file_docs = 0
            source_confidence = min(
                [
                    block.confidence
                    for block in work.uir.blocks
                    if block.confidence is not None
                ]
                or [1.0]
            )
            for extraction in work.extractions:
                doc = normalize_document(
                    extraction,
                    ctx.settings,
                    source_text=work.uir.full_text(),
                    source=work.uir.source,
                    uir_id=work.uir.uir_id,
                    source_confidence=source_confidence,
                )
                validate_document(doc, ctx.settings)
                if not doc.charges:
                    continue
                doc.source.file_sha256 = work.file.sha256
                documents.append(
                    DocWork(
                        file=work.file,
                        uir=work.uir,
                        extraction=extraction,
                        doc=doc,
                    )
                )
                file_docs += 1
                work.file.document_count += 1
            if work.file.status == "deciding":
                if file_docs == 0:
                    work.file.status = "completed"
                    work.file.message = work.file.message or "费用行经校验后为空"
        for task in files:
            if task.status == "deciding" and task.document_count == 0:
                task.status = "completed"
                task.message = task.message or "未得到可处理的费用记录"
        _sync_status(ctx, state)
        return {"documents": documents, "files": files}

    return node


def _arap_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        entity = ctx.settings.accounting_entity.name
        aliases = ctx.settings.accounting_entity.aliases
        for work in state.get("documents", []):
            doc = work.doc
            amount = document_amount(
                [charge.amount for charge in doc.charges],
                [total.amount for total in doc.totals if total.kind == "total" and total.amount is not None],
            )
            work.arap = determine_posting_type(
                document_type=doc.document_type,
                issuer=doc.issuer,
                recipient=doc.recipient,
                entity=entity,
                aliases=aliases,
                amount=amount,
                policy=ctx.settings.arap.policy,
                direction_statement=doc.direction_statement,
                ar_keywords=ctx.settings.arap.direction_ar_keywords,
                ap_keywords=ctx.settings.arap.direction_ap_keywords,
            )
        return {"documents": state.get("documents", [])}

    return node


def _match_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        decisions: list[ChargeDecision] = []
        for work in state.get("documents", []):
            for charge in work.doc.charges:
                match = match_charge(work.doc, charge, ctx.repo, ctx.settings)
                operation, merged, review_reason, notes = decide_operation(
                    match, work.doc, charge, ctx.settings
                )
                decisions.append(
                    ChargeDecision(
                        work=work,
                        charge=charge,
                        arap=work.arap,
                        match=match,
                        operation=operation,
                        merged=merged,
                        operation_review_reason=review_reason,
                        operation_notes=notes,
                    )
                )

        # 主体解析补救（缺陷 P2-7）：输入单据双方无法定位记账主体（ENTITY_UNRESOLVED）、
        # 但 L1–L3 唯一命中了含完整双方信息的已有账单时，用该账单双方重算 AR/AP。
        # 弱键（L4/歧义）命中的账单双方不足以决定记账方向，保持复核。
        rescued: dict[int, object] = {}
        for decision in decisions:
            arap = decision.arap
            if arap is None or arap.review_reason != "ENTITY_UNRESOLVED":
                continue
            if id(decision.work) in rescued:
                decision.arap = rescued[id(decision.work)]  # type: ignore[assignment]
                continue
            match = decision.match
            bill = match.bill
            if (
                bill is None
                or match.level not in {MatchLevel.L1, MatchLevel.L2, MatchLevel.L3}
                or not (bill.transaction_party.issuer or bill.transaction_party.recipient)
            ):
                continue
            entity = ctx.settings.accounting_entity.name
            amount = document_amount(
                [charge.amount for charge in decision.work.doc.charges],
                [
                    total.amount
                    for total in decision.work.doc.totals
                    if total.kind == "total" and total.amount is not None
                ],
            )
            retry = determine_posting_type(
                document_type=bill.document_info.document_type or decision.work.doc.document_type,
                issuer=bill.transaction_party.issuer,
                recipient=bill.transaction_party.recipient,
                entity=entity,
                aliases=ctx.settings.accounting_entity.aliases,
                amount=amount,
                policy=ctx.settings.arap.policy,
                direction_statement=decision.work.doc.direction_statement,
                ar_keywords=ctx.settings.arap.direction_ar_keywords,
                ap_keywords=ctx.settings.arap.direction_ap_keywords,
            )
            if retry.posting_type is not None:
                retry.reasoning.insert(
                    0,
                    f"输入单据双方无法定位记账主体，但 {match.level.value} 唯一命中已有账单 "
                    f"{bill.bill_id}（签发方 {bill.transaction_party.issuer} / 接收方 "
                    f"{bill.transaction_party.recipient}），以已有账单双方重算 AR/AP",
                )
                rescued[id(decision.work)] = retry
                decision.arap = retry
            else:
                rescued[id(decision.work)] = arap

        # 同一已有账单费用行被多个输入费用命中：无法确定唯一对应关系，全部送复核
        shared: dict[int, list[ChargeDecision]] = {}
        for decision in decisions:
            if decision.match.bill is not None:
                shared.setdefault(id(decision.match.bill), []).append(decision)
        for group in shared.values():
            if len(group) <= 1:
                continue
            for decision in group:
                decision.operation = Operation.PENDING_REVIEW
                if not decision.operation_review_reason:
                    decision.operation_review_reason = "MATCH_AMBIGUOUS"
                decision.operation_notes.append(
                    f"同一已有账单费用行被 {len(group)} 条输入费用命中，无法确定唯一对应关系，转人工复核"
                )
        return {"decisions": decisions}

    return node


def _review_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        for decision in state.get("decisions", []):
            outcome = finalize_review(
                decision.operation,
                decision.operation_review_reason,
                decision.work.doc,
                decision.charge,
                decision.arap,
                ctx.settings,
            )
            decision.operation = outcome.operation
            decision.review_reason = outcome.review_reason
            decision.review_notes = outcome.notes
            decision.low_confidence = outcome.low_confidence
            decision.confidence = outcome.confidence
            decision.posting_type = outcome.posting_type
        return {"decisions": state.get("decisions", [])}

    return node


def _assemble_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        prebuilt_by_sha: dict[str, list[BillRecord]] = {}
        for record in state.get("prebuilt_records", []):
            sha = record.source.file_sha256 or ""
            prebuilt_by_sha.setdefault(sha, []).append(record)
        records = assemble_records(
            state.get("files", []),
            state.get("decisions", []),
            prebuilt_by_sha,
            ctx.settings,
        )
        for task in state.get("files", []):
            if task.status in {"deciding", "extracting", "parsing", "unpacking", "queued"}:
                task.status = "completed"
        _sync_status(ctx, {**state, "records": records})  # type: ignore[arg-type]
        return {"records": records, "files": state.get("files", [])}

    return node


def _finalize_node(ctx: RunContext):
    def node(state: PipelineState) -> dict:
        files = state.get("files", [])
        for task in files:
            if task.status not in {"completed", "failed", "skipped"}:
                task.status = "completed"
        records = state.get("records", [])
        counts = _operation_counts(records)
        review_reasons: dict[str, int] = {}
        for record in records:
            if record.operation == Operation.PENDING_REVIEW and record.review_reason:
                review_reasons[record.review_reason] = review_reasons.get(record.review_reason, 0) + 1
        summary = BatchSummary(
            input_files=len(files),
            failed_files=sum(1 for task in files if task.status == "failed"),
            skipped_files=sum(1 for task in files if task.status == "skipped"),
            records=len(records),
            operation_counts=counts,
            review_reasons=review_reasons,
            match_key_coverage=ctx.repo.coverage(),
        )
        try:
            profile = ctx.settings.llm.active_profile()
            llm_profile = os.environ.get("LLM_PROFILE") or ctx.settings.llm.profile
            llm_model = profile.model
        except Exception:
            llm_profile, llm_model = ctx.settings.llm.profile, None
        exposer = ctx.settings.ocr.exposer
        if ctx.ocr_client is not None:
            exposer_obj = getattr(ctx.ocr_client, "exposer", None)
            if exposer_obj is not None and hasattr(exposer_obj, "health"):
                exposer = exposer_obj.health().get("selected") or exposer
        batch = BatchOutput(
            batch_id=ctx.batch_id,
            processed_at=now_iso(ctx.deterministic),
            summary=summary,
            files=[_file_result(task) for task in files],
            records=records,
            meta=BatchMeta(
                pipeline_version=ctx.settings.project.version,
                prompt_version=ctx.settings.project.prompt_version,
                dictionary_version=load_dicts().version,
                llm_profile=llm_profile,
                llm_model=llm_model,
                arap_policy=ctx.settings.arap.policy,
                ocr_exposer=exposer,
                extraction_mode=ctx.extraction_mode,
            ),
            warnings=list(dict.fromkeys(state.get("warnings", []) or [])),
        )
        write_outputs(batch, ctx.output_dir)
        for task in files:
            if task.status == "completed" and not task.review_reason:
                ctx.ledger[task.sha256] = ctx.batch_id
        # 账本合并写回（缺陷 P1-5）：--ignore-ledger 只影响读取，
        # 写回时先加载磁盘上的历史账本再合并，避免覆盖清空既有记录。
        merged_ledger = load_ledger(ctx.output_dir)
        merged_ledger.update(ctx.ledger)
        save_ledger(ctx.output_dir, merged_ledger)
        _sync_status(ctx, {**state, "records": records}, status="completed")  # type: ignore[arg-type]
        return {"batch_output": batch, "files": files, "records": records}

    return node


def _route_after_parse(state: PipelineState) -> str:
    if state.get("uirs"):
        return "extract"
    return "assemble" if state.get("prebuilt_records") else "finalize"


def _route_after_extract(state: PipelineState) -> str:
    if state.get("extractions"):
        return "normalize"
    return "assemble" if state.get("prebuilt_records") else "finalize"


def _route_after_normalize(state: PipelineState) -> str:
    if state.get("documents"):
        return "arap"
    return "assemble" if state.get("prebuilt_records") else "finalize"


def _route_after_review(state: PipelineState) -> str:
    if state.get("decisions") or state.get("prebuilt_records"):
        return "assemble"
    return "finalize"


def build_graph(ctx: RunContext):
    builder = StateGraph(PipelineState)
    builder.add_node("ingest", _ingest_node(ctx))
    builder.add_node("unpack", _unpack_node(ctx))
    builder.add_node("parse", _parse_node(ctx))
    builder.add_node("extract", _extract_node(ctx))
    builder.add_node("normalize", _normalize_node(ctx))
    builder.add_node("arap", _arap_node(ctx))
    builder.add_node("match", _match_node(ctx))
    builder.add_node("review", _review_node(ctx))
    builder.add_node("assemble", _assemble_node(ctx))
    builder.add_node("finalize", _finalize_node(ctx))

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "unpack")
    builder.add_edge("unpack", "parse")
    builder.add_conditional_edges(
        "parse",
        _route_after_parse,
        {"extract": "extract", "assemble": "assemble", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "extract",
        _route_after_extract,
        {"normalize": "normalize", "assemble": "assemble", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "normalize",
        _route_after_normalize,
        {"arap": "arap", "assemble": "assemble", "finalize": "finalize"},
    )
    builder.add_edge("arap", "match")
    builder.add_edge("match", "review")
    builder.add_conditional_edges(
        "review", _route_after_review, {"assemble": "assemble", "finalize": "finalize"}
    )
    builder.add_edge("assemble", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_batch(
    input_paths: list[str | Path],
    bill_paths: list[str | Path] | None = None,
    batch_id: str | None = None,
    settings: Settings | None = None,
    output_dir: str | Path | None = None,
    llm_client: LLMClient | None = None,
    ocr_client: OCRClient | None = None,
    repo: BillsRepository | None = None,
    deterministic: bool = False,
    ignore_ledger: bool = False,
) -> BatchOutput:
    settings = settings or get_settings()
    settings.ensure_dirs()
    batch_id = batch_id or make_batch_id()
    output_root = Path(output_dir) if output_dir else settings.output_dir
    output_root.mkdir(parents=True, exist_ok=True)
    work_dir = settings.data_dir / "tmp" / batch_id
    work_dir.mkdir(parents=True, exist_ok=True)

    ledger = {} if ignore_ledger else load_ledger(output_root)
    status_store = StatusStore(output_root, batch_id)
    created_at = now_iso(deterministic)
    status_store.write(
        BatchStatus(batch_id=batch_id, status="queued", progress=0, created_at=created_at)
    )

    repo = repo if repo is not None else load_bills(list(bill_paths or []), settings)
    ctx = RunContext(
        settings=settings,
        batch_id=batch_id,
        output_dir=output_root,
        work_dir=work_dir,
        status_store=status_store,
        repo=repo,
        llm_client=llm_client,
        ocr_client=ocr_client,
        deterministic=deterministic,
        ignore_ledger=ignore_ledger,
        extraction_mode="fake" if llm_client is not None else "llm",
        created_at=created_at,
        ledger=ledger,
    )
    initial: PipelineState = {
        "batch_id": batch_id,
        "input_paths": [str(path) for path in input_paths],
        "bill_paths": [str(path) for path in (bill_paths or [])],
        "work_dir": str(work_dir),
        "output_dir": str(output_root),
        "files": [],
        "uirs": [],
        "extractions": [],
        "documents": [],
        "decisions": [],
        "records": [],
        "prebuilt_records": [],
        "warnings": repo.warnings.copy(),
        "skipped_inputs": [],
        "error": None,
    }
    try:
        result = build_graph(ctx).invoke(initial, {"recursion_limit": 30})
    except Exception as exc:
        _sync_status(
            ctx,
            initial,
            status="failed",
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}",
        )
        raise
    batch = result.get("batch_output")
    if batch is None:
        raise RuntimeError("流水线未产出批结果")
    return batch
