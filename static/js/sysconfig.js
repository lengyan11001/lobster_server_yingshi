var ocConfigLoaded = false;
var ocProviderData = [];
var _currentSysTab = 'model';

document.querySelectorAll('.sys-tab').forEach(function(tab) {
  tab.addEventListener('click', function() {
    var target = tab.getAttribute('data-sys-tab');
    if (!target || target === _currentSysTab) return;
    _currentSysTab = target;
    document.querySelectorAll('.sys-tab').forEach(function(t) { t.classList.remove('active'); });
    tab.classList.add('active');
    document.getElementById('sysTabModel').style.display = (target === 'model') ? '' : 'none';
    document.getElementById('sysTabCustom').style.display = (target === 'custom') ? '' : 'none';
    if (target === 'custom') loadCustomConfigs();
  });
});

function loadLanInfo() {
  fetch(API_BASE + '/api/settings/lan-info', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var bar = document.getElementById('lanInfoBar');
      var link = document.getElementById('lanInfoUrl');
      var copyBtn = document.getElementById('lanInfoCopy');
      if (bar && d.url) {
        link.href = d.url;
        link.textContent = d.url;
        bar.style.display = '';
        if (copyBtn) {
          copyBtn.onclick = function() {
            try { navigator.clipboard.writeText(d.url); copyBtn.textContent = '已复制'; setTimeout(function() { copyBtn.textContent = '复制'; }, 1500); }
            catch(e) {}
          };
        }
      }
    })
    .catch(function() {});
}

function loadSutuiConfig() {
  var input = document.getElementById('sutuiTokenInput');
  if (!input) return;
  fetch(API_BASE + '/api/sutui/config', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      input.value = '';
      input.placeholder = (d.has_token ? '已配置 (' + (d.token || 'sk-***') + ')，输入新值可覆盖' : '输入速推/xSkill Token (sk-...)');
    })
    .catch(function() {
      input.placeholder = '输入速推/xSkill Token (sk-...)';
    });
}

function saveSutuiToken() {
  var input = document.getElementById('sutuiTokenInput');
  var btn = document.getElementById('saveSutuiTokenBtn');
  var msgEl = document.getElementById('sutuiTokenMsg');
  if (!input) return;
  var token = (input.value || '').trim();
  if (!token) {
    showMsg(msgEl, '请输入 Token', true);
    return;
  }
  if (btn) btn.disabled = true;
  fetch(API_BASE + '/api/sutui/config', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ token: token })
  })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(x) {
      if (x.ok) {
        showMsg(msgEl, 'Token 已保存', false);
        input.value = '';
        loadSutuiConfig();
      } else {
        showMsg(msgEl, (x.data && x.data.detail) || '保存失败', true);
      }
    })
    .catch(function() { showMsg(msgEl, '网络错误', true); })
    .finally(function() { if (btn) btn.disabled = false; });
}

function loadOpenClawConfig() {
  var modelTab = document.querySelector('.sys-tab[data-sys-tab="model"]');
  var modelPanel = document.getElementById('sysTabModel');
  var allowModel = true; // 由 ALLOW_SELF_CONFIG_MODEL 决定是否允许自配模型
  if (EDITION === 'online') {
    allowModel = typeof ALLOW_SELF_CONFIG_MODEL !== 'undefined' ? ALLOW_SELF_CONFIG_MODEL : true;
    if (modelTab) modelTab.style.display = allowModel ? '' : 'none';
    if (modelPanel) modelPanel.style.display = allowModel ? '' : 'none';
    if (!allowModel) {
      var customTab = document.querySelector('.sys-tab[data-sys-tab="custom"]');
      if (customTab) { customTab.click(); customTab.classList.add('active'); }
      if (document.getElementById('sysTabCustom')) document.getElementById('sysTabCustom').style.display = '';
    }
  } else {
    if (modelTab) modelTab.style.display = '';
    if (modelPanel) modelPanel.style.display = '';
  }
  var sutuiBlock = document.getElementById('sutuiTokenBlock');
  if (sutuiBlock) sutuiBlock.style.display = (EDITION !== 'online') ? '' : 'none';
  if (EDITION !== 'online') loadSutuiConfig();
  checkOcStatus();
  loadLanInfo();
  if (_currentSysTab === 'custom') loadCustomConfigs();
  if (ocConfigLoaded && EDITION !== 'online') return;
  if (EDITION === 'online' && !allowModel) { return; }
  fetch(API_BASE + '/api/openclaw/config', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      ocConfigLoaded = true;
      var modelSel = document.getElementById('ocPrimaryModel');
      if (modelSel && d.primary_model) {
        for (var i = 0; i < modelSel.options.length; i++) {
          if (modelSel.options[i].value === d.primary_model) {
            modelSel.selectedIndex = i;
            break;
          }
        }
      }
      ocProviderData = d.providers || [];
      renderProviderCards(ocProviderData);
    })
    .catch(function() {});
}

function checkOcStatus() {
  var dot = document.getElementById('ocStatusDot');
  var text = document.getElementById('ocStatusText');
  if (!dot || !text) return;
  dot.className = 'status-dot';
  text.textContent = '检查中...';
  fetch(API_BASE + '/api/openclaw/status', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.online) {
        dot.className = 'status-dot online';
        text.textContent = 'OpenClaw Gateway 运行中';
      } else {
        dot.className = 'status-dot offline';
        text.textContent = 'OpenClaw Gateway 未运行';
      }
    })
    .catch(function() {
      dot.className = 'status-dot offline';
      text.textContent = 'OpenClaw Gateway 无法连接';
    });
}

function renderProviderCards(providers) {
  var el = document.getElementById('ocProviderCards');
  if (!el) return;
  if (!providers || !providers.length) {
    el.innerHTML = '<p class="meta">无可用模型提供商</p>';
    return;
  }
  el.innerHTML = providers.map(function(p) {
    var statusText = p.configured
      ? '<span style="color:#6ee7b7;">已配置</span> (' + escapeHtml(p.masked_key) + ')'
      : '<span style="color:#f87171;">未配置</span>';
    var placeholder = p.configured ? '留空则保持原 Key，输入新值可覆盖' : '输入 API Key...';
    return '<div class="provider-card">' +
      '<div class="provider-name">' + escapeHtml(p.name) + '</div>' +
      '<div class="provider-status">' + statusText + '</div>' +
      '<div class="key-input-row">' +
      '<input type="password" id="ocKey_' + escapeAttr(p.id) + '" placeholder="' + escapeAttr(placeholder) + '" value="" autocomplete="off" style="min-width:14rem;">' +
      '</div>' +
      '<div class="form-hint">环境变量: ' + escapeHtml(p.env_key) + '</div>' +
      '</div>';
  }).join('');
}

function saveOcConfig() {
  var btn = document.getElementById('saveOcConfigBtn');
  var msgEl = document.getElementById('ocSaveMsg');
  if (btn) btn.disabled = true;
  var modelSel = document.getElementById('ocPrimaryModel');
  var body = {};
  if (modelSel) body.primary_model = modelSel.value;

  var keyMap = {
    'anthropic': 'anthropic_api_key',
    'openai': 'openai_api_key',
    'deepseek': 'deepseek_api_key',
    'google': 'gemini_api_key'
  };
  ocProviderData.forEach(function(p) {
    var input = document.getElementById('ocKey_' + p.id);
    if (input && input.value.trim()) {
      var field = keyMap[p.id];
      if (field) body[field] = input.value.trim();
    }
  });

  fetch(API_BASE + '/api/openclaw/config', {
    method: 'POST', headers: authHeaders(),
    body: JSON.stringify(body)
  })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(x) {
      if (x.ok) {
        showMsg(msgEl, x.data.message || '保存成功', false);
        ocConfigLoaded = false;
        loadOpenClawConfig();
        if (typeof refreshModelSelector === 'function') refreshModelSelector();
        setTimeout(checkOcStatus, 3000);
      } else {
        showMsg(msgEl, x.data.detail || '保存失败', true);
      }
    })
    .catch(function() { showMsg(msgEl, '网络错误', true); })
    .finally(function() { if (btn) btn.disabled = false; });
}

// Custom JSON Config Import
function saveCustomConfig() {
  var nameEl = document.getElementById('customConfigName');
  var jsonEl = document.getElementById('customConfigJson');
  var msgEl = document.getElementById('customConfigMsg');
  var name = (nameEl.value || '').trim();
  var raw = (jsonEl.value || '').trim();
  if (!name) { showMsg(msgEl, '请填写配置名称', true); return; }
  if (!raw) { showMsg(msgEl, '请填写配置内容', true); return; }

  // Pre-process: strip Python variable assignment like "TOS_CONFIG = {"
  var cleaned = raw;
  var assignMatch = cleaned.match(/^\s*\w+\s*=\s*\{/);
  if (assignMatch) {
    cleaned = cleaned.replace(/^\s*\w+\s*=\s*/, '');
  }

  fetch(API_BASE + '/api/custom-configs', {
    method: 'POST', headers: authHeaders(),
    body: JSON.stringify({ name: name, config_json: cleaned })
  })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(x) {
      if (x.ok) {
        showMsg(msgEl, x.data.message || '导入成功', false);
        nameEl.value = '';
        jsonEl.value = '';
        loadCustomConfigs();
        if (typeof refreshModelSelector === 'function') refreshModelSelector();
      } else {
        showMsg(msgEl, x.data.detail || '导入失败', true);
      }
    })
    .catch(function() { showMsg(msgEl, '网络错误', true); });
}

function loadCustomConfigs() {
  var el = document.getElementById('customConfigList');
  if (!el) return;
  fetch(API_BASE + '/api/custom-configs', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var configs = (d && Array.isArray(d.configs)) ? d.configs : [];
      if (!configs.length) {
        el.innerHTML = '<p class="meta">暂无自定义配置</p>';
        return;
      }
      el.innerHTML = configs.map(function(c) {
        var preview = JSON.stringify(c.config, null, 2);
        if (preview.length > 500) preview = preview.substring(0, 500) + '\n...';
        return '<div class="config-block-item">' +
          '<div class="block-header">' +
          '<span class="block-name">' + escapeHtml(c.name) + '</span>' +
          '<button type="button" class="btn btn-ghost btn-sm" data-delete-config="' + escapeAttr(c.name) + '">删除</button>' +
          '</div>' +
          '<pre>' + escapeHtml(preview) + '</pre>' +
          '</div>';
      }).join('');
      el.querySelectorAll('button[data-delete-config]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var cfgName = btn.getAttribute('data-delete-config');
          if (!confirm('确定删除配置 ' + cfgName + '？')) return;
          fetch(API_BASE + '/api/custom-configs/' + encodeURIComponent(cfgName), {
            method: 'DELETE', headers: authHeaders()
          })
            .then(function(r) { return r.json(); })
            .then(function() { loadCustomConfigs(); })
            .catch(function() { alert('删除失败'); });
        });
      });
    })
    .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
}

var saveOcBtn = document.getElementById('saveOcConfigBtn');
if (saveOcBtn) saveOcBtn.addEventListener('click', saveOcConfig);
var saveSutuiTokenBtn = document.getElementById('saveSutuiTokenBtn');
if (saveSutuiTokenBtn) saveSutuiTokenBtn.addEventListener('click', saveSutuiToken);
var refreshOcBtn = document.getElementById('refreshOcStatusBtn');
if (refreshOcBtn) refreshOcBtn.addEventListener('click', function() {
  checkOcStatus();
  ocConfigLoaded = false;
  loadOpenClawConfig();
});
var restartOcBtn = document.getElementById('restartOcBtn');
if (restartOcBtn) {
  restartOcBtn.addEventListener('click', function() {
    var msgEl = document.getElementById('ocSaveMsg');
    restartOcBtn.disabled = true;
    restartOcBtn.textContent = '重启中…';
    fetch(API_BASE + '/api/openclaw/restart', { method: 'POST', headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        showMsg(msgEl, d.message || (d.ok ? '重启成功' : '重启失败'), !d.ok);
        setTimeout(checkOcStatus, 3000);
      })
      .catch(function() { showMsg(msgEl, '网络错误', true); })
      .finally(function() { restartOcBtn.disabled = false; restartOcBtn.textContent = '重启 Gateway'; });
  });
}
var saveCustomBtn = document.getElementById('saveCustomConfigBtn');
if (saveCustomBtn) saveCustomBtn.addEventListener('click', saveCustomConfig);

// xSkill/SuTui config moved to skill store (skill.js)
