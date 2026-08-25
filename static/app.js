/* AskData 前端逻辑：上传/示例数据 → 聊天问答 → 渲染表格与图表 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  sessionId: null,
  busy: false,
};

const els = {
  landing: $("#landing"),
  workspace: $("#workspace"),
  btnSample: $("#btn-sample"),
  fileInput: $("#file-input"),
  landingError: $("#landing-error"),
  fileName: $("#file-name"),
  stats: $("#stats"),
  schema: $("#schema"),
  previewTable: $("#preview-table"),
  messages: $("#messages"),
  suggestions: $("#suggestions"),
  chatForm: $("#chat-form"),
  chatText: $("#chat-text"),
  sendBtn: $("#send-btn"),
};

/* ---------- 工具 ---------- */

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatCell(value) {
  if (value === null || value === undefined) return '<span class="muted">-</span>';
  if (typeof value === "number") {
    return escapeHtml(Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 2 }));
  }
  return escapeHtml(value);
}

async function api(url, options = {}) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || `请求失败（${resp.status}）`);
  }
  return data;
}

function showLandingError(message) {
  els.landingError.textContent = message;
  els.landingError.classList.remove("hidden");
}

function addMessage(html, className) {
  const div = document.createElement("div");
  div.className = `msg ${className}`;
  div.innerHTML = html;
  els.messages.appendChild(div);
  els.messages.scrollTop = els.messages.scrollHeight;
  return div;
}

function setBusy(busy) {
  state.busy = busy;
  els.chatText.disabled = busy || !state.sessionId;
  els.sendBtn.disabled = busy || !state.sessionId || !els.chatText.value.trim();
}

/* ---------- 数据面板 ---------- */

function renderStats(payload) {
  els.stats.innerHTML = `
    <span class="stat-chip">${payload.row_count.toLocaleString()} 行</span>
    <span class="stat-chip">${payload.column_count} 列</span>
  `;
}

function renderSchema(schema) {
  els.schema.innerHTML = schema
    .map((item) => {
      const sample = item.sample.length
        ? item.sample.map((v) => escapeHtml(v)).join("、")
        : "空";
      return `
        <div class="schema-item">
          <span class="schema-kind kind-${escapeHtml(item.kind)}">${escapeHtml(item.kind)}</span>
          <span class="schema-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
          <span class="schema-meta" title="非空 ${item.non_null} / 去重 ${item.unique}">${item.non_null} 非空 · ${item.unique} 去重 · 示例：${sample}</span>
        </div>`;
    })
    .join("");
}

function renderPreview(payload) {
  const head = payload.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const rows = payload.preview
    .map((row) => `<tr>${payload.columns.map((c) => `<td>${formatCell(row[c])}</td>`).join("")}</tr>`)
    .join("");
  els.previewTable.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${rows}</tbody>`;
}

function enterWorkspace(payload) {
  els.landing.classList.add("hidden");
  els.workspace.classList.remove("hidden");
  state.sessionId = payload.session_id;

  els.fileName.textContent = payload.filename;
  renderStats(payload);
  renderSchema(payload.schema);
  renderPreview(payload);
  els.messages.innerHTML = "";
  els.suggestions.innerHTML = "";

  addMessage(
    `👋 数据已就绪（${payload.filename}，${payload.row_count.toLocaleString()} 行 × ${payload.column_count} 列）。
试试下面的问题，或者直接输入你的分析需求：`,
    "msg-ai"
  );

  const chips = payload.suggested_questions || [];
  chips.forEach((question) => {
    const chip = document.createElement("button");
    chip.className = "suggestion-chip";
    chip.textContent = question;
    chip.addEventListener("click", () => {
      els.chatText.value = question;
      setBusy(false);
      els.chatText.focus();
      askQuestion(question);
    });
    els.suggestions.appendChild(chip);
  });

  els.chatText.disabled = false;
  els.chatText.focus();
  setBusy(false);

  // 深链演示模式：/?demo=1 自动进入示例数据并提问第一个问题（可分享的演示链接）
  if (new URLSearchParams(location.search).get("demo") === "1") {
    const firstChip = els.suggestions.querySelector(".suggestion-chip");
    if (firstChip) setTimeout(() => firstChip.click(), 350);
  }
}

/* ---------- 结果渲染 ---------- */

function renderResults(results) {
  return results
    .map((result) => {
      if (result.kind === "table") {
        const head = result.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
        const rows = result.data
          .map((row) => `<tr>${result.columns.map((c) => `<td>${formatCell(row[c])}</td>`).join("")}</tr>`)
          .join("");
        return `
          ${result.description ? `<div class="result-desc">${escapeHtml(result.description)}</div>` : ""}
          <div class="result-table-wrap"><table class="table">
            <thead><tr>${head}</tr></thead>
            <tbody>${rows}</tbody>
          </table></div>`;
      }
      const text = Array.isArray(result.data)
        ? result.data.map((v) => (typeof v === "object" ? JSON.stringify(v, null, 2) : String(v))).join("\n")
        : typeof result.data === "object"
          ? JSON.stringify(result.data, null, 2)
          : String(result.data);
      return `
        ${result.description ? `<div class="result-desc">${escapeHtml(result.description)}</div>` : ""}
        <div class="result-value">${escapeHtml(text)}</div>`;
    })
    .join("");
}

async function renderCharts(charts) {
  const blocks = [];
  for (const chart of charts) {
    const block = document.createElement("div");
    block.className = "chart-card";
    block.innerHTML = `<div class="chart-title">📈 ${escapeHtml(chart.file)}</div><div class="chart-body"></div>`;
    els.messages.appendChild(block);
    els.messages.scrollTop = els.messages.scrollHeight;
    const body = block.querySelector(".chart-body");
    if (chart.format === "plotly" && window.Plotly) {
      try {
        const fig = await fetch(chart.url).then((r) => r.json());
        await Plotly.newPlot(body, fig.data, fig.layout, { responsive: true, displaylogo: false });
      } catch (err) {
        body.innerHTML = `<div class="error-box">图表加载失败：${escapeHtml(err.message)}</div>`;
      }
    } else if (chart.format === "png") {
      body.innerHTML = `<img src="${chart.url}" alt="${escapeHtml(chart.file)}">`;
    }
    blocks.push(block);
  }
  return blocks;
}

function sandboxBadge(sandbox) {
  const cls = sandbox.ok ? "ok" : "warn";
  return `
    <div class="sandbox-line">
      <span class="${cls}">${sandbox.ok ? "🔒 沙箱执行成功" : "⛔ 沙箱拦截"}</span>
      <span>⏱ 总耗时 ${(sandbox.duration_ms / 1000).toFixed(2)}s</span>
      <span>🧮 代码运行 ${sandbox.code_time_ms}ms</span>
      <span>💾 内存上限 ${sandbox.memory_mb}MB</span>
      <span>⏲ 超时 ${sandbox.timeout_s}s</span>
    </div>`;
}

/* ---------- 问答 ---------- */

function askQuestion(question) {
  if (state.busy || !question.trim() || !state.sessionId) return;

  addMessage(escapeHtml(question), "msg-user");
  els.chatText.value = "";
  setBusy(true);

  const typing = addMessage('<span class="typing"><i></i><i></i><i></i></span> AI 正在生成代码并安全执行…', "msg-ai");

  api("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: state.sessionId, question }),
  })
    .then(async (data) => {
      typing.remove();
      const html = `
        <div class="reply-text">${escapeHtml(data.reply)}</div>
        ${data.results && data.results.length ? renderResults(data.results) : ""}
        ${sandboxBadge(data.sandbox)}
        ${data.code ? `
          <details class="code-details">
            <summary>查看 AI 生成的代码</summary>
            <pre>${escapeHtml(data.code)}</pre>
          </details>` : ""}`;
      addMessage(html, "msg-ai");
      await renderCharts(data.charts || []);
    })
    .catch((err) => {
      typing.remove();
      addMessage(`<div class="error-box">⚠️ ${escapeHtml(err.message)}</div>`, "msg-ai");
    })
    .finally(() => setBusy(false));
}

/* ---------- 事件绑定 ---------- */

els.btnSample.addEventListener("click", async () => {
  els.btnSample.disabled = true;
  els.btnSample.textContent = "加载中…";
  try {
    const payload = await api("/api/sample", { method: "POST" });
    enterWorkspace(payload);
  } catch (err) {
    showLandingError(err.message);
  } finally {
    els.btnSample.disabled = false;
    els.btnSample.textContent = "🚀 一键体验示例数据";
  }
});

els.fileInput.addEventListener("change", async () => {
  const file = els.fileInput.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const payload = await api("/api/upload", { method: "POST", body: form });
    enterWorkspace(payload);
  } catch (err) {
    showLandingError(err.message);
  } finally {
    els.fileInput.value = "";
  }
});

els.chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  askQuestion(els.chatText.value);
});

els.chatText.addEventListener("input", () => setBusy(false));

// 落地页深链：/?demo=1 自动加载示例数据
if (new URLSearchParams(location.search).get("demo") === "1") {
  window.addEventListener("load", () => setTimeout(() => els.btnSample.click(), 250));
}
