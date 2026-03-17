/**
 * 企业微信配置页：列表、添加/编辑弹窗、返回技能商店。
 * 由技能商店「企业微信自动回复」卡片点击「配置」进入（hash=wecom-config）。
 */
(function() {
  var listEl = document.getElementById('wecomConfigList');
  var backBtn = document.getElementById('wecomConfigBackBtn');
  var addBtn = document.getElementById('wecomConfigAddBtn');
  var modal = document.getElementById('wecomConfigModal');
  var modalTitle = document.getElementById('wecomConfigModalTitle');
  var nameInput = document.getElementById('wecomConfigName');
  var tokenInput = document.getElementById('wecomConfigToken');
  var aesKeyInput = document.getElementById('wecomConfigAesKey');
  var corpIdInput = document.getElementById('wecomConfigCorpId');
  var productInput = document.getElementById('wecomConfigProductKnowledge');
  var secretInput = document.getElementById('wecomConfigSecret');
  var modalMsg = document.getElementById('wecomConfigModalMsg');
  var modalCancel = document.getElementById('wecomConfigModalCancel');
  var modalSave = document.getElementById('wecomConfigModalSave');

  var _editingId = null;

  function api(method, path, body) {
    var opts = { method: method, headers: typeof authHeaders === 'function' ? authHeaders() : {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch((typeof API_BASE !== 'undefined' ? API_BASE : '') + path, opts);
  }

  function showMsg(el, text, isErr) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (isErr ? ' err' : '');
    el.style.display = text ? 'block' : 'none';
  }

  function loadWecomConfigList() {
    if (!listEl) return;
    listEl.innerHTML = '<p class="meta">加载中…</p>';
    api('GET', '/api/wecom/configs')
      .then(function(r) {
        if (r.status === 401) { if (typeof loadDashboard === 'function') loadDashboard(); return null; }
        return r.json();
      })
      .then(function(d) {
        if (!listEl) return;
        if (!d || !Array.isArray(d.configs)) {
          listEl.innerHTML = '<p class="meta">加载失败或暂无配置</p>';
          return;
        }
        var configs = d.configs;
        if (configs.length === 0) {
          listEl.innerHTML = '<p class="meta">暂无配置，点击「添加配置」创建。</p>';
          return;
        }
        var html = configs.map(function(c) {
          var name = (c.name || '未命名').trim() || '未命名';
          var corp = c.corp_id || '-';
          var url = c.callback_url || ('/api/wecom/callback/' + (c.callback_path || ''));
          var hasKnowledge = c.has_product_knowledge ? '有' : '无';
          var hasSecret = c.has_secret ? 'Secret: 已配置' : 'Secret: 未配置';
          return '<div class="skill-store-card wecom-config-card" data-config-id="' + escapeAttr(String(c.id)) + '">' +
            '<div class="card-label">应用</div>' +
            '<div class="card-value">' + escapeHtml(name) + '</div>' +
            '<div class="card-desc">CorpID: ' + escapeHtml(corp) + ' · ' + hasSecret + ' · 产品知识: ' + hasKnowledge + '</div>' +
            '<pre class="config-block-item" style="font-size:0.75rem;margin:0.5rem 0;padding:0.4rem;background:rgba(0,0,0,0.2);border-radius:4px;overflow-x:auto;">' + escapeHtml(url) + '</pre>' +
            '<div class="card-actions">' +
              '<button type="button" class="btn btn-ghost btn-sm wecom-copy-url" data-url="' + escapeAttr(url) + '">复制 URL</button>' +
              '<button type="button" class="btn btn-ghost btn-sm wecom-edit" data-id="' + escapeAttr(String(c.id)) + '">编辑</button>' +
              '<button type="button" class="btn btn-ghost btn-sm wecom-delete" data-id="' + escapeAttr(String(c.id)) + '">删除</button>' +
            '</div></div>';
        }).join('');
        listEl.innerHTML = html;
        listEl.querySelectorAll('.wecom-config-card').forEach(function(card) {
          var id = card.getAttribute('data-config-id');
          card.addEventListener('click', function(e) {
            if (e.target.closest('.card-actions')) return;
            openEdit(parseInt(id, 10));
          });
        });
        listEl.querySelectorAll('.wecom-copy-url').forEach(function(btn) {
          btn.addEventListener('click', function(e) { e.stopPropagation(); copyUrl(btn.getAttribute('data-url')); });
        });
        listEl.querySelectorAll('.wecom-edit').forEach(function(btn) {
          btn.addEventListener('click', function(e) { e.stopPropagation(); openEdit(parseInt(btn.getAttribute('data-id'), 10)); });
        });
        listEl.querySelectorAll('.wecom-delete').forEach(function(btn) {
          btn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (!confirm('确定删除该配置？')) return;
            deleteConfig(parseInt(btn.getAttribute('data-id'), 10));
          });
        });
      })
      .catch(function() {
        if (listEl) listEl.innerHTML = '<p class="msg err">加载失败</p>';
      });
  }

  function copyUrl(url) {
    if (!url) return;
    if (url.indexOf('/') === 0) url = window.location.origin + url;
    if (typeof navigator.clipboard !== 'undefined' && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function() { alert('已复制到剪贴板'); }).catch(function() { fallbackCopy(url); });
    } else { fallbackCopy(url); }
  }
  function fallbackCopy(str) {
    var ta = document.createElement('textarea');
    ta.value = str;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); alert('已复制到剪贴板'); } catch (e) {}
    document.body.removeChild(ta);
  }

  function openAdd() {
    _editingId = null;
    if (modalTitle) modalTitle.textContent = '添加企业微信配置';
    if (nameInput) nameInput.value = '默认应用';
    if (tokenInput) tokenInput.value = '';
    if (aesKeyInput) aesKeyInput.value = '';
    if (corpIdInput) corpIdInput.value = '';
    if (secretInput) { secretInput.value = ''; secretInput.placeholder = '企业微信应用 Secret'; }
    if (productInput) productInput.value = '';
    showMsg(modalMsg, '');
    if (modal) modal.classList.add('visible');
  }

  function openEdit(id) {
    _editingId = id;
    if (modalTitle) modalTitle.textContent = '编辑企业微信配置';
    api('GET', '/api/wecom/configs/' + id)
      .then(function(r) { return r.json(); })
      .then(function(c) {
        if (!c) return;
        if (nameInput) nameInput.value = c.name || '';
        if (tokenInput) { tokenInput.value = ''; tokenInput.placeholder = '不修改请留空'; }
        if (aesKeyInput) { aesKeyInput.value = ''; aesKeyInput.placeholder = '不修改请留空'; }
        if (corpIdInput) corpIdInput.value = c.corp_id || '';
        if (secretInput) { secretInput.value = c.secret || ''; secretInput.placeholder = '不修改请留空'; }
        if (productInput) productInput.value = c.product_knowledge || '';
        showMsg(modalMsg, '');
        if (modal) modal.classList.add('visible');
      })
      .catch(function() { showMsg(modalMsg, '加载配置失败', true); });
  }

  function closeModal() {
    _editingId = null;
    if (modal) modal.classList.remove('visible');
    showMsg(modalMsg, '');
  }

  function saveConfig() {
    var name = (nameInput && nameInput.value) ? nameInput.value.trim() : '默认应用';
    var token = (tokenInput && tokenInput.value) ? tokenInput.value.trim() : '';
    var aesKey = (aesKeyInput && aesKeyInput.value) ? aesKeyInput.value.trim() : '';
    var corpId = (corpIdInput && corpIdInput.value) ? corpIdInput.value.trim() : '';
    var product = (productInput && productInput.value) ? productInput.value.trim() : '';
    var secret = (secretInput && secretInput.value) ? secretInput.value.trim() : '';
    if (!token && !_editingId) { showMsg(modalMsg, '请填写 Token', true); return; }
    if (!aesKey && !_editingId) { showMsg(modalMsg, '请填写 EncodingAESKey', true); return; }
    showMsg(modalMsg, '保存中…', false);
    var body = { name: name || '默认应用', token: token || undefined, encoding_aes_key: aesKey || undefined, corp_id: corpId || undefined, secret: secret || undefined, product_knowledge: product || undefined };
    var method = _editingId ? 'PUT' : 'POST';
    var path = _editingId ? '/api/wecom/configs/' + _editingId : '/api/wecom/configs';
    if (_editingId) {
      var up = {};
      if (name) up.name = name;
      if (token) up.token = token;
      if (aesKey) up.encoding_aes_key = aesKey;
      if (corpId !== undefined) up.corp_id = corpId;
      if (secret !== undefined) up.secret = secret;
      if (product !== undefined) up.product_knowledge = product;
      body = up;
    }
    api(method, path, body)
      .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, data: d }; });
      })
      .then(function(x) {
        if (x.ok) {
          closeModal();
          loadWecomConfigList();
          showMsg(modalMsg, '');
        } else {
          showMsg(modalMsg, (x.data && (x.data.detail || x.data.message)) || '保存失败', true);
        }
      })
      .catch(function() {
        showMsg(modalMsg, '请求失败', true);
      });
  }

  function deleteConfig(id) {
    api('DELETE', '/api/wecom/configs/' + id)
      .then(function(r) {
        if (r.ok) loadWecomConfigList();
        else r.json().then(function(d) { alert(d.detail || '删除失败'); });
      })
      .catch(function() { alert('请求失败'); });
  }

  if (backBtn) {
    backBtn.addEventListener('click', function() {
      location.hash = '';
      document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
      var contentEl = document.getElementById('content-skill-store');
      if (contentEl) contentEl.classList.add('visible');
      document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
      var navEl = document.querySelector('.nav-left-item[data-view="skill-store"]');
      if (navEl) navEl.classList.add('active');
      if (typeof currentView !== 'undefined') currentView = 'skill-store';
      if (typeof loadSkillStore === 'function') loadSkillStore();
    });
  }
  if (addBtn) addBtn.addEventListener('click', openAdd);
  if (modalCancel) modalCancel.addEventListener('click', closeModal);
  if (modalSave) modalSave.addEventListener('click', saveConfig);
  if (modal) {
    modal.addEventListener('click', function(e) {
      if (e.target === modal) closeModal();
    });
  }

  window.showWecomConfigView = function() {
    location.hash = 'wecom-config';
    document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
    var contentEl = document.getElementById('content-wecom-config');
    if (contentEl) contentEl.classList.add('visible');
    document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
    var navEl = document.querySelector('.nav-left-item[data-view="skill-store"]');
    if (navEl) navEl.classList.add('active');
    if (typeof currentView !== 'undefined') currentView = 'wecom-config';
    loadWecomConfigList();
  };

  window.loadWecomConfigList = loadWecomConfigList;
})();
