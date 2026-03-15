/* production.js — 生产记录：仅速推能力调用 + 模型对话，可刷新看进度 */
(function () {
  const PAGE_SIZE = 20;
  let _offset = 0;
  let _total = 0;
  let _initialized = false;

  function _formatTime(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function _esc(s) {
    if (s == null) return "";
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  function _renderCard(item) {
    if (item.type === "capability") {
      const ok = item.success;
      const statusText = item.status ? String(item.status) : (ok ? "成功" : "失败");
      const latency = item.latency_ms != null ? `${item.latency_ms}ms` : "";
      const err = item.error_message ? `<div style="font-size:0.78rem;color:var(--error);margin-top:0.25rem;">${_esc(item.error_message)}</div>` : "";
      return `
        <div class="card prod-card" style="margin-bottom:0.5rem;padding:0.75rem 1rem;">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.3rem;">
            <span style="font-size:0.75rem;padding:2px 8px;background:rgba(6,182,212,0.15);color:var(--accent);border-radius:10px;">速推能力</span>
            <span style="font-size:0.78rem;color:var(--text-muted);">${_esc(item.capability_id)}</span>
            <span style="font-size:0.75rem;padding:2px 8px;background:${ok ? "rgba(16,185,129,0.15)" : "rgba(239,68,68,0.15)"};color:${ok ? "var(--success)" : "var(--error)"};border-radius:10px;">${_esc(statusText)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:0.75rem;margin-top:0.4rem;font-size:0.72rem;color:var(--text-muted);">
            <span>${_formatTime(item.created_at)}</span>
            ${latency ? `<span>${latency}</span>` : ""}
          </div>
          ${err}
        </div>`;
    }
    if (item.type === "model") {
      const userPreview = (item.user_message || "").trim().slice(0, 120);
      const replyPreview = (item.assistant_reply || "").trim().slice(0, 150);
      return `
        <div class="card prod-card" style="margin-bottom:0.5rem;padding:0.75rem 1rem;">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.3rem;">
            <span style="font-size:0.75rem;padding:2px 8px;background:rgba(255,255,255,0.08);color:var(--text-muted);border-radius:10px;">模型对话</span>
            <span style="font-size:0.72rem;color:var(--text-muted);">${_formatTime(item.created_at)}</span>
          </div>
          <div style="font-size:0.82rem;color:var(--text);margin-top:0.4rem;">${_esc(userPreview)}${userPreview.length >= 120 ? "…" : ""}</div>
          <div style="font-size:0.78rem;color:var(--text-muted);margin-top:0.25rem;">${_esc(replyPreview)}${replyPreview.length >= 150 ? "…" : ""}</div>
        </div>`;
    }
    return "";
  }

  async function _loadLogs() {
    const list = document.getElementById("prodList");
    if (!list) return;
    const qs = new URLSearchParams({ limit: PAGE_SIZE, offset: _offset });
    list.innerHTML = '<p style="text-align:center;color:var(--text-muted);padding:2rem;">加载中…</p>';
    try {
      const base = typeof API_BASE !== "undefined" ? API_BASE : "";
      const res = await fetch(base + "/api/production/records?" + qs.toString(), {
        headers: typeof authHeaders !== "undefined" ? authHeaders() : { Authorization: "Bearer " + (localStorage.getItem("token") || "") },
      });
      if (!res.ok) throw new Error("加载失败");
      const data = await res.json();
      _total = data.total || 0;
      const items = data.items || [];
      if (!items.length) {
        list.innerHTML = '<p style="text-align:center;color:var(--text-muted);padding:2rem;">暂无记录。在对话中使用速推生成或发送消息后，点击刷新查看。</p>';
      } else {
        list.innerHTML = items.map(_renderCard).join("");
      }
      _renderPagination();
    } catch (e) {
      list.innerHTML = '<p style="text-align:center;color:var(--error);padding:2rem;">' + _esc(e.message) + '</p>';
    }
  }

  function _renderPagination() {
    const el = document.getElementById("prodPagination");
    if (!el) return;
    const totalPages = Math.max(1, Math.ceil(_total / PAGE_SIZE));
    const curPage = Math.floor(_offset / PAGE_SIZE) + 1;
    if (_total <= 0) {
      el.innerHTML = "";
      return;
    }
    if (totalPages <= 1) {
      el.innerHTML = '<span style="font-size:0.78rem;color:var(--text-muted);">共 ' + _total + ' 条</span>';
      return;
    }
    el.innerHTML =
      '<button class="btn btn-ghost btn-sm" ' + (curPage <= 1 ? "disabled" : "") + ' id="prodPrev">上一页</button>' +
      '<span style="font-size:0.82rem;color:var(--text-muted);">' + curPage + ' / ' + totalPages + '（共 ' + _total + ' 条）</span>' +
      '<button class="btn btn-ghost btn-sm" ' + (curPage >= totalPages ? "disabled" : "") + ' id="prodNext">下一页</button>';
    const prev = document.getElementById("prodPrev");
    const next = document.getElementById("prodNext");
    if (prev) prev.onclick = function () { _offset = Math.max(0, _offset - PAGE_SIZE); _loadLogs(); };
    if (next) next.onclick = function () { _offset += PAGE_SIZE; _loadLogs(); };
  }

  function _bind() {
    const btn = document.getElementById("prodRefreshBtn");
    if (btn) btn.onclick = function () { _offset = 0; _loadLogs(); };
  }

  window.initProductionView = function () {
    if (!_initialized) {
      _bind();
      _initialized = true;
    }
    _offset = 0;
    _loadLogs();
  };
})();
