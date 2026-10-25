(() => {
  "use strict";

  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));

  const ALLOWED_EXT = new Set([
    "eml", "pdf", "jpg", "jpeg", "png", "tiff", "tif", "xls", "xlsx", "doc", "docx",
  ]);
  const MAX_FILES = 20;
  const MAX_SIZE_MB = 20;

  const OP_LABEL = {
    CREATE: "新增",
    UPDATE: "更新",
    NOCHANGE: "无变化",
    PENDING_REVIEW: "待复核",
  };
  const FILE_STATUS = {
    queued: "排队中",
    ocr: "解析 / OCR",
    extracting: "语义提取",
    deciding: "匹配判定",
    completed: "完成",
    failed: "失败",
    skipped: "跳过",
  };
  const BATCH_STATUS = {
    queued: "排队中",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
  };

  const state = {
    files: [],
    bills: [],
    batchId: null,
    timer: null,
    records: [],
    filter: "",
    lowOnly: false,
    currentRecord: null,
    status: null,
    busy: false,
  };

  const el = {
    dropzone: $("#dropzone"),
    fileInput: $("#fileInput"),
    billInput: $("#billInput"),
    fileList: $("#fileList"),
    billList: $("#billList"),
    submitBtn: $("#submitBtn"),
    submitLabel: $(".btn-label", $("#submitBtn")),
    spinner: $(".spinner", $("#submitBtn")),
    clearBtn: $("#clearBtn"),
    uploadHint: $("#uploadHint"),
    uploadCard: $("#uploadCard"),
    progressCard: $("#progressCard"),
    resultsCard: $("#resultsCard"),
    batchIdText: $("#batchIdText"),
    batchStatusPill: $("#batchStatusPill"),
    progressFill: $("#progressFill"),
    progressText: $("#progressText"),
    progressFiles: $("#progressFiles"),
    countGrid: $("#countGrid"),
    progressBody: $("#progressBody"),
    recordsBody: $("#recordsBody"),
    resultsMeta: $("#resultsMeta"),
    filterGroup: $("#filterGroup"),
    lowOnly: $("#lowOnly"),
    exportBtn: $("#exportBtn"),
    historyList: $("#historyList"),
    healthChips: $("#healthChips"),
    serviceStatus: $("#serviceStatus"),
    modal: $("#detailModal"),
    detailTitle: $("#detailTitle"),
    detailSubtitle: $("#detailSubtitle"),
    detailFields: $("#detailFields"),
    jsonCode: $("#jsonCode"),
    jsonBadge: $("#jsonBadge"),
    copyJsonBtn: $("#copyJsonBtn"),
    downloadRecordBtn: $("#downloadRecordBtn"),
    closeModal: $("#closeModal"),
    toast: $("#toast"),
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function formatSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function toast(message, type = "") {
    el.toast.textContent = message;
    el.toast.className = `toast ${type}`.trim();
    el.toast.classList.remove("hidden");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.toast.classList.add("hidden"), 3600);
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : `HTTP ${response.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  // ------------------------------------------------------------------ //
  // 上传
  // ------------------------------------------------------------------ //
  function fileKey(file) {
    return `${file.name}-${file.size}-${file.lastModified}`;
  }

  function addFiles(list, kind) {
    const target = kind === "bill" ? state.bills : state.files;
    const existing = new Set(target.map(fileKey));
    const errors = [];
    for (const file of list) {
      const ext = file.name.split(".").pop().toLowerCase();
      if (kind === "bill") {
        if (ext !== "json") { errors.push(`${file.name}：已有账单必须是 JSON`); continue; }
      } else if (!ALLOWED_EXT.has(ext)) {
        errors.push(`${file.name}：不支持的格式 .${ext}`);
        continue;
      }
      if (file.size > MAX_SIZE_MB * 1024 * 1024) {
        errors.push(`${file.name}：超过 ${MAX_SIZE_MB} MB`);
        continue;
      }
      if (existing.has(fileKey(file))) continue;
      target.push(file);
      existing.add(fileKey(file));
    }
    if (target.length > MAX_FILES) {
      target.splice(MAX_FILES);
      errors.push(`单批最多 ${MAX_FILES} 个文件`);
    }
    renderFileLists();
    if (errors.length) toast(errors[0], "error");
  }

  function renderFileLists() {
    el.fileList.innerHTML = state.files
      .map(
        (file, index) => `
        <span class="file-pill">
          <span class="name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
          <span class="size">${formatSize(file.size)}</span>
          <button data-kind="file" data-index="${index}" aria-label="移除">×</button>
        </span>`
      )
      .join("");
    el.billList.innerHTML = state.bills
      .map(
        (file, index) => `
        <span class="file-pill">
          <span class="name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
          <span class="size">${formatSize(file.size)}</span>
          <button data-kind="bill" data-index="${index}" aria-label="移除">×</button>
        </span>`
      )
      .join("");
    el.uploadHint.textContent = state.files.length
      ? `已选择 ${state.files.length} 个账单文件${state.bills.length ? `、${state.bills.length} 个账单库文件` : ""}`
      : "";
  }

  function removeFile(kind, index) {
    const target = kind === "bill" ? state.bills : state.files;
    target.splice(index, 1);
    renderFileLists();
  }

  async function submitBatch() {
    if (state.busy) return;
    if (!state.files.length) {
      toast("请先选择至少一个账单文件", "error");
      return;
    }
    state.busy = true;
    el.submitBtn.disabled = true;
    el.spinner.classList.remove("hidden");
    el.submitLabel.textContent = "上传中…";
    try {
      const form = new FormData();
      state.files.forEach((file) => form.append("files[]", file, file.name));
      state.bills.forEach((file) => form.append("bills[]", file, file.name));
      const data = await fetchJson("/api/batches", { method: "POST", body: form });
      state.batchId = data.batch_id;
      state.filter = "";
      state.lowOnly = false;
      el.lowOnly.checked = false;
      el.filterGroup.querySelectorAll(".filter").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.op === "");
      });
      showProgress();
      toast(`批次 ${data.batch_id} 已创建`, "success");
      state.files = [];
      state.bills = [];
      renderFileLists();
      startPolling();
    } catch (error) {
      toast(`上传失败：${error.message}`, "error");
    } finally {
      state.busy = false;
      el.submitBtn.disabled = false;
      el.spinner.classList.add("hidden");
      el.submitLabel.textContent = "开始处理";
    }
  }

  function showProgress() {
    el.progressCard.classList.remove("hidden");
    el.resultsCard.classList.add("hidden");
    el.progressCard.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------------------ //
  // 进度轮询
  // ------------------------------------------------------------------ //
  function startPolling() {
    stopPolling();
    pollStatus();
    state.timer = setInterval(pollStatus, 2000);
  }

  function stopPolling() {
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
  }

  async function pollStatus() {
    if (!state.batchId) return;
    try {
      const data = await fetchJson(`/api/batches/${encodeURIComponent(state.batchId)}`);
      renderStatus(data);
      if (["completed", "failed"].includes(data.status)) {
        stopPolling();
        if (data.status === "completed") {
          await loadRecords();
          await loadHistory();
        } else {
          toast(`批次处理失败：${data.error || "未知错误"}`, "error");
        }
      }
    } catch (error) {
      // 保持轮询，避免偶发网络抖动打断
      console.warn("poll failed", error);
    }
  }

  function statusPillClass(status) {
    return ["queued", "running", "completed", "failed"].includes(status) ? status : "queued";
  }

  function renderStatus(data) {
    state.status = data;
    el.batchIdText.textContent = data.batch_id || "—";
    const batchStatus = data.status || "queued";
    el.batchStatusPill.textContent = BATCH_STATUS[batchStatus] || batchStatus;
    el.batchStatusPill.className = `status-pill ${statusPillClass(batchStatus)}`;
    const progress = Math.max(0, Math.min(100, Number(data.progress || 0)));
    el.progressFill.style.width = `${progress}%`;
    el.progressText.textContent = `${progress}%`;
    const total = data.total_files || (data.files || []).length;
    const processed = data.processed_files || (data.files || []).filter((f) => ["completed", "failed", "skipped"].includes(f.status)).length;
    el.progressFiles.textContent = `${processed} / ${total} 个文件`;

    const counts = data.operation_counts || {};
    el.countGrid.innerHTML = ["CREATE", "UPDATE", "NOCHANGE", "PENDING_REVIEW"]
      .map(
        (op) => `
        <div class="count-card ${op === "PENDING_REVIEW" ? "review" : op.toLowerCase()}">
          <div class="label">${OP_LABEL[op]}</div>
          <div class="value">${Number(counts[op] || 0)}</div>
        </div>`
      )
      .join("");

    const files = (data.files && data.files.length)
      ? data.files
      : (data.file_results || []).map((item) => ({
          file_name: item.file_name,
          status: item.status,
          record_count: item.record_count,
          message: item.message,
          review_reason: item.review_reason,
        }));
    if (!files.length) {
      el.progressBody.innerHTML = `<tr><td colspan="4" class="empty-cell">等待文件处理…</td></tr>`;
      return;
    }
    el.progressBody.innerHTML = files
      .map((file) => {
        const status = file.status || "queued";
        const label = FILE_STATUS[status] || status;
        const message = file.message || (file.review_reason ? `复核原因：${file.review_reason}` : "—");
        return `<tr>
          <td class="mono">${escapeHtml(file.file_name || "—")}</td>
          <td><span class="badge badge-${escapeHtml(status)}">${escapeHtml(label)}</span></td>
          <td class="mono">${Number(file.record_count || 0)}</td>
          <td class="muted">${escapeHtml(message)}</td>
        </tr>`;
      })
      .join("");
  }

  // ------------------------------------------------------------------ //
  // 记录列表与详情
  // ------------------------------------------------------------------ //
  async function loadRecords() {
    if (!state.batchId) return;
    const params = new URLSearchParams();
    if (state.filter) params.set("operation", state.filter);
    if (state.lowOnly) params.set("low_confidence", "true");
    try {
      const data = await fetchJson(
        `/api/batches/${encodeURIComponent(state.batchId)}/records?${params.toString()}`
      );
      state.records = data.records || [];
      renderRecords(data);
      el.resultsCard.classList.remove("hidden");
      el.resultsMeta.textContent = `${data.batch_id} · ${formatTime((data.summary && data.summary.processed_at) || (state.status && state.status.finished_at))}`;
      renderCounts(data.summary && data.summary.operation_counts);
    } catch (error) {
      if (!String(error.message).includes("尚未产出")) {
        toast(`读取记录失败：${error.message}`, "error");
      }
    }
  }

  function renderCounts(counts) {
    const mapping = {
      countAll: ["CREATE", "UPDATE", "NOCHANGE", "PENDING_REVIEW"],
      countCreate: ["CREATE"],
      countUpdate: ["UPDATE"],
      countNochange: ["NOCHANGE"],
      countReview: ["PENDING_REVIEW"],
    };
    const data = counts || {};
    Object.entries(mapping).forEach(([id, ops]) => {
      const node = document.getElementById(id);
      if (node) node.textContent = String(ops.reduce((sum, op) => sum + Number(data[op] || 0), 0));
    });
  }

  function confidenceCell(record) {
    const value = Math.max(0, Math.min(1, Number(record.confidence || 0)));
    const low = record.low_confidence ? `<span class="low-dot" title="低置信"></span>` : "";
    return `<div class="confidence">${low}<div class="bar"><div class="fill" style="width:${(value * 100).toFixed(0)}%"></div></div><span class="val">${value.toFixed(2)}</span></div>`;
  }

  function renderRecords(data) {
    const records = data.records || [];
    if (!records.length) {
      el.recordsBody.innerHTML = `<tr><td colspan="7" class="empty-cell">当前筛选下没有记录</td></tr>`;
      return;
    }
    el.recordsBody.innerHTML = records
      .map((record, index) => {
        const op = record.operation;
        const charge = record.charge_details || {};
        const original = charge.original_charge_name;
        const standard = charge.charge_item;
        const chargeHtml = original || standard
          ? `<div class="charge-cell"><span class="orig">${escapeHtml(original || "—")}</span><span class="std">${escapeHtml(standard || "未标准化")}</span></div>`
          : "—";
        const amount = charge.amount === null || charge.amount === undefined
          ? "—"
          : Number(charge.amount).toFixed(2);
        const review = record.review_reason
          ? `<div class="small muted">${escapeHtml(record.review_reason)}</div>`
          : "";
        return `<tr data-index="${index}">
          <td><button type="button" class="link-btn" data-index="${index}" aria-label="查看记录 ${escapeHtml(record.record_id)}">${escapeHtml(record.record_id)}</button></td>
          <td><span class="badge badge-${escapeHtml(op)}" title="${escapeHtml(review)}">${escapeHtml(OP_LABEL[op] || op)}</span></td>
          <td class="mono">${escapeHtml((record.document_info || {}).reference_no || "—")}</td>
          <td>${chargeHtml}</td>
          <td class="num mono">${escapeHtml(amount)} ${escapeHtml(charge.currency || "")}</td>
          <td>${escapeHtml((record.financial_attributes || {}).posting_type || "—")}</td>
          <td>${confidenceCell(record)}</td>
        </tr>`;
      })
      .join("");
    $$("tr[data-index]", el.recordsBody).forEach((row) => {
      row.addEventListener("click", () => {
        const record = records[Number(row.dataset.index)];
        if (record) openDetail(record);
      });
    });
  }

  function kv(rows) {
    return `<dl class="kv">${rows
      .map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value ?? "—")}</dd>`)
      .join("")}</dl>`;
  }

  function openDetail(record) {
    state.currentRecord = record;
    const doc = record.document_info || {};
    const party = record.transaction_party || {};
    const shipment = record.shipment_info || {};
    const charge = record.charge_details || {};
    const financial = record.financial_attributes || {};
    const match = record.match_info || {};
    const source = record.source || {};
    const audit = record.audit_trail || {};
    const validation = record.validation || {};
    el.detailTitle.textContent = record.record_id;
    el.detailSubtitle.textContent = `${OP_LABEL[record.operation] || record.operation} · ${doc.reference_no || "无单据号"} · ${doc.document_type || "未知类型"}`;

    const sections = [];
    sections.push(`<div class="detail-section"><h3>单据信息</h3>${kv([
      ["单据类型", doc.document_type],
      ["单据编号", doc.reference_no],
      ["单据日期", doc.document_date],
    ])}</div>`);
    sections.push(`<div class="detail-section"><h3>交易双方</h3>${kv([
      ["签发方", party.issuer],
      ["接收方", party.recipient],
    ])}</div>`);
    sections.push(`<div class="detail-section"><h3>运输信息</h3>${kv([
      ["提单号", shipment.bl_no],
      ["订舱号", shipment.booking_no],
      ["箱号", shipment.container_no],
      ["船名航次", shipment.vessel_voyage],
      ["起运港 / 目的港", [shipment.pol, shipment.pod].filter(Boolean).join(" → ") || "—"],
    ])}</div>`);
    sections.push(`<div class="detail-section"><h3>费用明细</h3>${kv([
      ["原始费用名", charge.original_charge_name],
      ["标准费用项", charge.charge_item],
      ["数量", charge.quantity],
      ["单价", charge.unit_price],
      ["金额", charge.amount === null || charge.amount === undefined ? "—" : `${Number(charge.amount).toFixed(2)} ${charge.currency || ""}`],
      ["币种", charge.currency],
      ["付款方式", charge.payment_term],
    ])}</div>`);
    sections.push(`<div class="detail-section"><h3>财务属性</h3>${kv([
      ["记账主体", financial.accounting_entity],
      ["应收 / 应付", financial.posting_type],
      ["记录置信度", Number(record.confidence || 0).toFixed(3)],
      ["低置信", record.low_confidence ? "是" : "否"],
      ["复核原因", record.review_reason],
    ])}</div>`);
    sections.push(`<div class="detail-section"><h3>匹配信息</h3>${kv([
      ["命中账单", match.matched_bill_id],
      ["匹配级别", match.match_level],
      ["匹配依据", match.match_evidence],
      ["候选账单", (match.candidates || []).join(", ") || "—"],
      ["派生键", (match.derived_keys_used || []).join(", ") || "—"],
    ])}</div>`);

    if (record.change_info && (record.change_info.changed_fields || []).length) {
      const changes = record.change_info.changed_fields
        .map(
          (item) =>
            `<li><span class="mono">${escapeHtml(item.field)}</span>：${escapeHtml(item.old_value ?? "null")} → <strong>${escapeHtml(item.new_value ?? "null")}</strong></li>`
        )
        .join("");
      sections.push(`<div class="detail-section"><h3>字段变更</h3><ul class="reason-list">${changes}</ul></div>`);
    }

    const issues = validation.issues || [];
    if (issues.length) {
      sections.push(
        `<div class="detail-section"><h3>校验与修复</h3><div class="issue-list">${issues
          .map(
            (issue) =>
              `<div class="issue ${issue.action === "review" || issue.severity === "error" ? "error" : issue.severity === "info" ? "info" : ""}">
                <span class="rule">${escapeHtml(issue.rule)}</span>${escapeHtml(issue.message)}
              </div>`
          )
          .join("")}</div></div>`
      );
    }

    sections.push(`<div class="detail-section"><h3>来源</h3>${kv([
      ["文件", source.file_name],
      ["类型", source.file_type],
      ["SHA-256", source.file_sha256],
      ["邮件主题", source.email_subject],
      ["发件人", source.email_from],
      ["Message-ID", source.email_message_id],
    ])}</div>`);
    if (audit.original_text_snippet) {
      sections.push(
        `<div class="detail-section"><h3>原文快照</h3><pre class="snippet">${escapeHtml(audit.original_text_snippet)}</pre></div>`
      );
    }
    sections.push(
      `<div class="detail-section"><h3>处理依据</h3><ul class="reason-list">${(audit.ai_reasoning || [])
        .map((line) => `<li>${escapeHtml(line)}</li>`)
        .join("")}</ul></div>`
    );

    el.detailFields.innerHTML = sections.join("");
    const json = JSON.stringify(record, null, 2);
    el.jsonCode.innerHTML = highlightJson(json);
    el.jsonBadge.textContent = `${(new Blob([json]).size / 1024).toFixed(1)} KB`;
    el.modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }

  function highlightJson(json) {
    const safe = json.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const pattern = /("(?:\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false)\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g;
    return safe.replace(pattern, (match) => {
      let cls = "n";
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? "k" : "s";
      } else if (/true|false/.test(match)) {
        cls = "b";
      } else if (/null/.test(match)) {
        cls = "z";
      }
      return `<span class="${cls}">${match}</span>`;
    });
  }

  function closeModal() {
    el.modal.classList.add("hidden");
    document.body.style.overflow = "";
    state.currentRecord = null;
  }

  async function copyJson() {
    if (!state.currentRecord) return;
    const text = JSON.stringify(state.currentRecord, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      toast("JSON 已复制", "success");
    } catch (error) {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast("JSON 已复制", "success");
    }
  }

  function downloadBlob(filename, content, type = "application/json") {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function downloadRecord() {
    if (!state.currentRecord) return;
    downloadBlob(
      `${state.currentRecord.record_id}.json`,
      JSON.stringify(state.currentRecord, null, 2)
    );
  }

  // ------------------------------------------------------------------ //
  // 历史批次
  // ------------------------------------------------------------------ //
  async function loadHistory() {
    try {
      const data = await fetchJson("/api/batches");
      renderHistory(data.batches || []);
    } catch (error) {
      el.historyList.innerHTML = `<p class="muted small">加载失败：${escapeHtml(error.message)}</p>`;
    }
  }

  function renderHistory(batches) {
    if (!batches.length) {
      el.historyList.innerHTML = `<p class="muted small">暂无历史批次</p>`;
      return;
    }
    el.historyList.innerHTML = batches
      .slice(0, 30)
      .map((batch) => {
        const summary = batch.summary || {};
        const counts = summary.operation_counts || {};
        const total = summary.records || 0;
        return `<div class="history-item" data-batch="${escapeHtml(batch.batch_id)}">
          <div class="row1">
            <span class="id" title="${escapeHtml(batch.batch_id)}">${escapeHtml(batch.batch_id)}</span>
            <span class="badge badge-${escapeHtml(batch.status || "completed")}">${escapeHtml(BATCH_STATUS[batch.status] || batch.status || "完成")}</span>
          </div>
          <div class="time">${escapeHtml(formatTime(batch.processed_at))}</div>
          <div class="counts">${total} 条记录 · 新增 ${Number(counts.CREATE || 0)} · 更新 ${Number(counts.UPDATE || 0)} · 待复核 ${Number(counts.PENDING_REVIEW || 0)}</div>
        </div>`;
      })
      .join("");
    $$(".history-item", el.historyList).forEach((item) => {
      item.addEventListener("click", () => openBatch(item.dataset.batch));
    });
  }

  async function openBatch(batchId) {
    state.batchId = batchId;
    showProgress();
    try {
      const data = await fetchJson(`/api/batches/${encodeURIComponent(batchId)}`);
      renderStatus(data);
      if (data.status === "completed") {
        await loadRecords();
      } else if (data.status === "running" || data.status === "queued") {
        startPolling();
      }
    } catch (error) {
      toast(`打开批次失败：${error.message}`, "error");
    }
  }

  // ------------------------------------------------------------------ //
  // 健康状态
  // ------------------------------------------------------------------ //
  async function loadHealth() {
    try {
      const data = await fetchJson("/api/health?probe=1");
      renderHealth(data);
    } catch (error) {
      el.healthChips.innerHTML = `<span class="chip bad"><span class="dot"></span>服务不可达</span>`;
      el.serviceStatus.innerHTML = `<div class="service-item"><span class="dot bad"></span><span>无法连接后端：${escapeHtml(error.message)}</span></div>`;
    }
  }

  function renderHealth(data) {
    const llm = data.llm || {};
    const ocr = data.ocr || {};
    const llmOk = llm.key_present && llm.reachable;
    const llmClass = !llm.key_present ? "bad" : llmOk ? "ok" : "warn";
    const llmText = !llm.key_present
      ? "LLM 未配置密钥"
      : llm.reachable === true
        ? `LLM 在线 · ${llm.model || ""}`
        : llm.reachable === false
          ? "LLM 不可达"
          : `LLM · ${llm.model || "未探测"}`;
    const ocrClass = ocr.reachable === true ? "ok" : ocr.reachable === false ? "bad" : "warn";
    const ocrText = ocr.reachable === true
      ? "OCR 在线"
      : ocr.reachable === false
        ? "OCR 不可达"
        : "OCR 未探测";
    const exposer = ocr.selected || "未配置";
    el.healthChips.innerHTML = `
      <span class="chip ${llmClass}"><span class="dot"></span>${escapeHtml(llmText)}</span>
      <span class="chip ${ocrClass}"><span class="dot"></span>${escapeHtml(ocrText)}</span>
      <span class="chip ${exposer === "未配置" ? "warn" : "ok"}"><span class="dot"></span>外发：${escapeHtml(exposer)}</span>
    `;
    const items = [];
    items.push(`<div class="service-item"><span class="dot ${llmClass}"></span><span>LLM ${escapeHtml(llm.profile || "")}</span><span class="meta">${escapeHtml(llm.model || "—")}</span></div>`);
    items.push(`<div class="service-item"><span class="dot ${llm.key_present ? "ok" : "bad"}"></span><span>API Key</span><span class="meta">${llm.key_present ? "已配置" : "缺失"}</span></div>`);
    items.push(`<div class="service-item"><span class="dot ${ocrClass}"></span><span>OCR 服务</span><span class="meta">${ocr.reachable === true ? "在线" : ocr.reachable === false ? "不可达" : "—"}</span></div>`);
    items.push(`<div class="service-item"><span class="dot ${exposer === "未配置" ? "warn" : "ok"}"></span><span>文件外发策略</span><span class="meta">${escapeHtml(exposer)}</span></div>`);
    el.serviceStatus.innerHTML = items.join("");
  }

  // ------------------------------------------------------------------ //
  // 事件绑定
  // ------------------------------------------------------------------ //
  function bind() {
    el.dropzone.addEventListener("click", () => el.fileInput.click());
    el.dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        el.fileInput.click();
      }
    });
    ["dragenter", "dragover"].forEach((type) =>
      el.dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        el.dropzone.classList.add("dragover");
      })
    );
    ["dragleave", "drop"].forEach((type) =>
      el.dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        el.dropzone.classList.remove("dragover");
      })
    );
    el.dropzone.addEventListener("drop", (event) => {
      const files = Array.from(event.dataTransfer.files || []);
      addFiles(files, "file");
    });
    el.fileInput.addEventListener("change", () => {
      addFiles(Array.from(el.fileInput.files || []), "file");
      el.fileInput.value = "";
    });
    el.billInput.addEventListener("change", () => {
      addFiles(Array.from(el.billInput.files || []), "bill");
      el.billInput.value = "";
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest(".file-pill button");
      if (button) {
        removeFile(button.dataset.kind, Number(button.dataset.index));
      }
    });
    el.submitBtn.addEventListener("click", submitBatch);
    el.clearBtn.addEventListener("click", () => {
      state.files = [];
      state.bills = [];
      renderFileLists();
    });
    el.filterGroup.addEventListener("click", (event) => {
      const button = event.target.closest(".filter");
      if (!button) return;
      state.filter = button.dataset.op || "";
      $$(".filter", el.filterGroup).forEach((item) => item.classList.toggle("active", item === button));
      loadRecords();
    });
    el.lowOnly.addEventListener("change", () => {
      state.lowOnly = el.lowOnly.checked;
      loadRecords();
    });
    el.exportBtn.addEventListener("click", () => {
      if (!state.batchId) return;
      window.location.href = `/api/batches/${encodeURIComponent(state.batchId)}/export`;
    });
    $("#refreshHistory").addEventListener("click", loadHistory);
    el.closeModal.addEventListener("click", closeModal);
    el.modal.addEventListener("click", (event) => {
      if (event.target === el.modal) closeModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !el.modal.classList.contains("hidden")) closeModal();
    });
    el.copyJsonBtn.addEventListener("click", copyJson);
    el.downloadRecordBtn.addEventListener("click", downloadRecord);
  }

  bind();
  loadHealth();
  loadHistory();
})();
