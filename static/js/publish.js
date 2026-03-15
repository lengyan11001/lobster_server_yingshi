// ── Publish Management (发布管理) ─────────────────────────────────

var _currentPubTab = 'accounts';

document.querySelectorAll('.pub-tab').forEach(function(tab) {
  tab.addEventListener('click', function() {
    var target = tab.getAttribute('data-pub-tab');
    if (!target || target === _currentPubTab) return;
    _currentPubTab = target;
    document.querySelectorAll('.pub-tab').forEach(function(t) { t.classList.remove('active'); });
    tab.classList.add('active');
    document.getElementById('pubTabAccounts').style.display = (target === 'accounts') ? '' : 'none';
    document.getElementById('pubTabAssets').style.display = (target === 'assets') ? '' : 'none';
    document.getElementById('pubTabTasks').style.display = (target === 'tasks') ? '' : 'none';
    if (target === 'accounts') loadAccounts();
    if (target === 'assets') loadAssets();
    if (target === 'tasks') loadTasks();
  });
});

var PLATFORM_NAMES = { douyin: '抖音', bilibili: 'B站', xiaohongshu: '小红书', kuaishou: '快手' };
var STATUS_LABELS = { active: '已登录', pending: '待登录', error: '异常' };
var STATUS_COLORS = { active: '#34d399', pending: '#fb923c', error: '#f87171' };

// ── Accounts ─────────────────────────────────────────────────────

var _allAccounts = [];

function _renderAccountList(accounts) {
  var el = document.getElementById('accountList');
  if (!el) return;
  if (!accounts.length) {
    el.innerHTML = '<p class="meta" style="padding:1rem;">该平台暂无发布账号。请在上方添加账号后扫码登录。</p>';
    return;
  }
  el.innerHTML = accounts.map(function(a) {
    var statusColor = STATUS_COLORS[a.status] || '#888';
    var statusLabel = STATUS_LABELS[a.status] || a.status;
    var openBtn = '<button type="button" class="btn btn-primary btn-sm" data-open-browser="' + a.id + '">打开浏览器</button>';
    var publishBtn = '<button type="button" class="btn btn-primary btn-sm" data-publish-acct="' + a.id + '" data-publish-nick="' + escapeAttr(a.nickname) + '">发布素材</button>';
    var deleteBtn = '<button type="button" class="btn btn-ghost btn-sm" data-delete-id="' + a.id + '">删除</button>';
    var lastLogin = a.last_login ? '上次登录: ' + a.last_login.substring(0, 16).replace('T', ' ') : '';
    return '<div class="skill-store-card">' +
      '<div class="card-label">' + escapeHtml(PLATFORM_NAMES[a.platform] || a.platform) +
      ' <span style="color:' + statusColor + ';font-weight:600;">' + escapeHtml(statusLabel) + '</span></div>' +
      '<div class="card-value">' + escapeHtml(a.nickname) + '</div>' +
      '<div class="card-desc" style="font-size:0.78rem;color:var(--text-muted);">' + escapeHtml(lastLogin) + '</div>' +
      '<div class="card-actions">' + openBtn + publishBtn + deleteBtn + '</div></div>';
  }).join('');
  _bindAccountButtons(el);
}

function loadAccounts() {
  var el = document.getElementById('accountList');
  if (!el) return;
  el.innerHTML = '<p class="meta">加载中…</p>';
  fetch(API_BASE + '/api/accounts', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _allAccounts = (d && Array.isArray(d.accounts)) ? d.accounts : [];
      _applyAccountListFilter();
    })
    .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
}

function _applyAccountListFilter() {
  var el = document.getElementById('accountList');
  if (!el) return;
  var platformFilter = (document.getElementById('accountListPlatform') && document.getElementById('accountListPlatform').value) || '';
  if (!_allAccounts.length) {
    el.innerHTML = '<p class="meta" style="padding:1rem;">暂无发布账号。请在上方添加账号后扫码登录。</p>';
    return;
  }
  var list = platformFilter ? _allAccounts.filter(function(a) { return a.platform === platformFilter; }) : _allAccounts;
  _renderAccountList(list);
}

// 筛选平台变更时只刷新列表展示，不重新请求
var accountListPlatformEl = document.getElementById('accountListPlatform');
if (accountListPlatformEl) {
  accountListPlatformEl.addEventListener('change', function() { _applyAccountListFilter(); });
}

function _bindAccountButtons(el) {
  el.querySelectorAll('button[data-open-browser]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var id = btn.getAttribute('data-open-browser');
      btn.disabled = true; btn.textContent = '启动中…';
      fetch(API_BASE + '/api/accounts/' + id + '/open-browser', {
        method: 'POST', headers: authHeaders()
      })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          var status = d.logged_in ? '已登录' : '未登录，请在浏览器中扫码';
          btn.textContent = status;
          setTimeout(function() { loadAccounts(); }, 2000);
        })
        .catch(function() { alert('请求失败'); })
        .finally(function() { btn.disabled = false; });
    });
  });
  el.querySelectorAll('button[data-publish-acct]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var id = btn.getAttribute('data-publish-acct');
      var nick = btn.getAttribute('data-publish-nick') || '';
      var assetId = prompt('请输入要发布的素材 ID（可在「素材库」tab 查看）：');
      if (!assetId || !assetId.trim()) return;
      var title = prompt('发布标题（可留空）：', '') || '';
      btn.disabled = true; btn.textContent = '发布中…';
      fetch(API_BASE + '/api/publish', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({
          asset_id: assetId.trim(),
          account_nickname: nick,
          title: title
        })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.data && x.data.need_login) {
            alert('未登录，已打开浏览器，请扫码登录后重试');
          } else if (x.data && x.data.status === 'success') {
            alert('发布成功！' + (x.data.result_url ? '\n' + x.data.result_url : ''));
          } else {
            alert(x.data.error || x.data.detail || '发布失败');
          }
          loadAccounts();
        })
        .catch(function() { alert('请求失败'); })
        .finally(function() { btn.disabled = false; btn.textContent = '发布素材'; });
    });
  });
  el.querySelectorAll('button[data-delete-id]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var id = btn.getAttribute('data-delete-id');
      if (!confirm('确定删除此账号？')) return;
      fetch(API_BASE + '/api/accounts/' + id, {
        method: 'DELETE', headers: authHeaders()
      })
        .then(function() { loadAccounts(); })
        .catch(function() { alert('删除失败'); });
    });
  });
}

// Add account
var addAcctBtn = document.getElementById('addAcctBtn');
if (addAcctBtn) {
  addAcctBtn.addEventListener('click', function() {
    var platform = document.getElementById('addAcctPlatform').value;
    var nickname = (document.getElementById('addAcctNickname').value || '').trim();
    var msgEl = document.getElementById('addAcctMsg');
    if (!nickname) { showMsg(msgEl, '请输入账号昵称', true); return; }
    addAcctBtn.disabled = true;
    fetch(API_BASE + '/api/accounts', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({ platform: platform, nickname: nickname })
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          showMsg(msgEl, x.data.message || '添加成功', false);
          document.getElementById('addAcctNickname').value = '';
          loadAccounts();
        } else { showMsg(msgEl, x.data.detail || '添加失败', true); }
      })
      .catch(function() { showMsg(msgEl, '网络错误', true); })
      .finally(function() { addAcctBtn.disabled = false; });
  });
}

// ── Assets ───────────────────────────────────────────────────────

var _MEDIA_TYPE_LABELS = { image: '图片', video: '视频', audio: '音频' };

function _assetMsgShow(text, isErr) {
  var m = document.getElementById('assetUploadMsg');
  if (!m) return;
  m.textContent = text;
  m.className = 'msg' + (isErr ? ' err' : ' ok');
  m.style.display = 'inline';
  setTimeout(function() { m.style.display = 'none'; }, 4000);
}

function loadAssets(query) {
  var el = document.getElementById('assetList');
  if (!el) return;
  el.innerHTML = '<p class="meta">加载中…</p>';
  var mediaType = (document.getElementById('assetTypeFilter') || {}).value || '';
  var url = API_BASE + '/api/assets?limit=50';
  if (mediaType) url += '&media_type=' + encodeURIComponent(mediaType);
  if (query) url += '&q=' + encodeURIComponent(query);
  fetch(url, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var assets = (d && Array.isArray(d.assets)) ? d.assets : [];
      if (!assets.length) {
        el.innerHTML = '<p class="meta" style="padding:1rem;">暂无素材。可上传本地文件或保存网络URL，也可在对话中让龙虾生成。</p>';
        return;
      }
      el.innerHTML = assets.map(function(a) {
        var isImage = a.media_type === 'image';
        var isVideo = a.media_type === 'video';
        var preview = '';
        if (isImage) {
          preview = '<div class="asset-preview-wrap" data-asset-id="' + escapeAttr(a.asset_id) + '" data-media-type="image" style="margin:0.5rem 0;cursor:pointer;" title="点击在新窗口预览"><img src="/media/' + escapeAttr(a.filename) + '" style="max-width:160px;max-height:120px;border-radius:6px;object-fit:cover;pointer-events:none;"></div>';
        } else if (isVideo) {
          preview = '<div class="asset-preview-wrap" data-asset-id="' + escapeAttr(a.asset_id) + '" data-media-type="video" style="margin:0.5rem 0;cursor:pointer;" title="点击在新窗口预览"><video src="/media/' + escapeAttr(a.filename) + '" style="max-width:160px;max-height:120px;border-radius:6px;pointer-events:none;" muted preload="metadata"></video></div>';
        } else {
          preview = '<div style="margin:0.5rem 0;font-size:0.8rem;color:var(--text-muted);">[' + escapeHtml(a.media_type) + '] ' + escapeHtml(a.filename) + '</div>';
        }
        var typeLabel = _MEDIA_TYPE_LABELS[a.media_type] || a.media_type;
        var tags = a.tags ? '<div class="card-tags">' + a.tags.split(',').map(function(t) { return '<span class="tag">' + escapeHtml(t.trim()) + '</span>'; }).join('') + '</div>' : '';
        var size = a.file_size ? (a.file_size > 1048576 ? (a.file_size / 1048576).toFixed(1) + ' MB' : (a.file_size / 1024).toFixed(1) + ' KB') : '';
        var deleteBtn = '<button type="button" class="btn btn-ghost btn-sm" data-delete-asset="' + escapeAttr(a.asset_id) + '">删除</button>';
        return '<div class="skill-store-card">' +
          '<div class="card-label"><span style="background:' + (isImage ? '#6366f1' : isVideo ? '#f59e0b' : '#888') + ';color:#fff;padding:1px 6px;border-radius:3px;font-size:0.72rem;margin-right:4px;">' + escapeHtml(typeLabel) + '</span> ' + escapeHtml(size) + '</div>' +
          preview +
          '<div class="card-desc" style="font-size:0.78rem;">' + escapeHtml(a.prompt || a.filename) + '</div>' +
          tags +
          '<div class="card-desc" style="font-size:0.72rem;color:var(--text-muted);">ID: ' + escapeHtml(a.asset_id) + ' · ' + escapeHtml(a.created_at.substring(0, 16).replace('T', ' ')) + '</div>' +
          '<div class="card-actions">' + deleteBtn + '</div></div>';
      }).join('');
      el.querySelectorAll('button[data-delete-asset]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var aid = btn.getAttribute('data-delete-asset');
          if (!confirm('确定删除此素材？')) return;
          fetch(API_BASE + '/api/assets/' + aid, { method: 'DELETE', headers: authHeaders() })
            .then(function() { loadAssets(); })
            .catch(function() { alert('删除失败'); });
        });
      });
      el.querySelectorAll('.asset-preview-wrap').forEach(function(wrap) {
        wrap.addEventListener('click', function() {
          var aid = wrap.getAttribute('data-asset-id');
          var mtype = wrap.getAttribute('data-media-type');
          if (!aid) return;
          fetch(API_BASE + '/api/assets/' + aid + '/content', { headers: authHeaders() })
            .then(function(r) { if (!r.ok) throw new Error(r.status); return r.blob(); })
            .then(function(blob) {
              var u = URL.createObjectURL(blob);
              var w = window.open('', '_blank');
              if (w) {
                if (mtype === 'video') {
                  w.document.write('<video src="' + u + '" controls autoplay style="max-width:100%;max-height:100vh;"></video>');
                } else {
                  w.document.write('<img src="' + u + '" style="max-width:100%;max-height:100vh;">');
                }
                w.document.close();
              }
              setTimeout(function() { URL.revokeObjectURL(u); }, 60000);
            })
            .catch(function() { alert('预览加载失败'); });
        });
      });
    })
    .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
}

// Search
var assetSearchBtn = document.getElementById('assetSearchBtn');
if (assetSearchBtn) {
  assetSearchBtn.addEventListener('click', function() {
    var q = (document.getElementById('assetSearchInput') || {}).value || '';
    loadAssets(q.trim());
  });
}

// Filter by media type
var assetTypeFilter = document.getElementById('assetTypeFilter');
if (assetTypeFilter) {
  assetTypeFilter.addEventListener('change', function() {
    var q = (document.getElementById('assetSearchInput') || {}).value || '';
    loadAssets(q.trim());
  });
}

// Upload local files
var assetUploadFile = document.getElementById('assetUploadFile');
if (assetUploadFile) {
  assetUploadFile.addEventListener('change', function() {
    var files = assetUploadFile.files;
    if (!files || !files.length) return;
    var total = files.length, done = 0, failed = 0;
    _assetMsgShow('正在上传 ' + total + ' 个文件…', false);
    Array.from(files).forEach(function(f) {
      var fd = new FormData();
      fd.append('file', f);
      fetch(API_BASE + '/api/assets/upload', { method: 'POST', headers: authHeaders(), body: fd })
        .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function() { done++; })
        .catch(function() { failed++; })
        .finally(function() {
          if (done + failed === total) {
            assetUploadFile.value = '';
            var msg = '上传完成: ' + done + ' 成功';
            if (failed) msg += ', ' + failed + ' 失败';
            _assetMsgShow(msg, failed > 0);
            loadAssets();
          }
        });
    });
  });
}

// Save URL asset
var assetSaveUrlBtn = document.getElementById('assetSaveUrlBtn');
if (assetSaveUrlBtn) {
  assetSaveUrlBtn.addEventListener('click', function() {
    var urlInput = document.getElementById('assetUrlInput');
    var rawUrl = (urlInput ? urlInput.value : '').trim();
    if (!rawUrl) { _assetMsgShow('请输入素材URL', true); return; }
    assetSaveUrlBtn.disabled = true;
    _assetMsgShow('正在保存…', false);
    var ext = rawUrl.split('?')[0].split('#')[0].split('.').pop().toLowerCase();
    var mtype = 'image';
    if (['mp4', 'mov', 'avi', 'mkv', 'webm', 'flv'].indexOf(ext) >= 0) mtype = 'video';
    fetch(API_BASE + '/api/assets/save-url', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ url: rawUrl, media_type: mtype })
    })
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d) {
        if (urlInput) urlInput.value = '';
        _assetMsgShow('保存成功 (ID: ' + (d.asset_id || '') + ')', false);
        loadAssets();
      })
      .catch(function(e) { _assetMsgShow('保存失败: ' + e.message, true); })
      .finally(function() { assetSaveUrlBtn.disabled = false; });
  });
}

// ── Tasks ────────────────────────────────────────────────────────

var TASK_STATUS = { pending: '排队中', publishing: '发布中', success: '成功', failed: '失败', need_login: '需登录' };
var TASK_COLORS = { pending: '#fbbf24', publishing: '#60a5fa', success: '#34d399', failed: '#f87171', need_login: '#fb923c' };

function _renderSteps(steps) {
  if (!steps || !steps.length) return '';
  var html = '<div style="margin-top:0.5rem;padding:0.5rem;background:rgba(255,255,255,0.03);border-radius:6px;font-size:0.75rem;">';
  html += '<div style="color:var(--text-muted);margin-bottom:0.25rem;font-weight:600;">执行步骤：</div>';
  for (var i = 0; i < steps.length; i++) {
    var s = steps[i];
    var icon = s.ok ? '✓' : '✗';
    var color = s.ok ? '#34d399' : '#f87171';
    var action = s.action || s.note || '';
    var detail = '';
    if (s.error) detail = ' — ' + s.error;
    else if (s.selector) detail = '';
    else if (s.url) detail = '';
    else if (s.tried && !s.ok) detail = ' (未匹配)';
    html += '<div style="color:' + color + ';padding:1px 0;">' +
      '<span style="display:inline-block;width:1.2em;text-align:center;">' + icon + '</span> ' +
      escapeHtml(action) + escapeHtml(detail) + '</div>';
  }
  html += '</div>';
  return html;
}

function loadTasks() {
  var el = document.getElementById('taskList');
  if (!el) return;
  el.innerHTML = '<p class="meta">加载中…</p>';
  fetch(API_BASE + '/api/publish/tasks?limit=50', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var tasks = (d && Array.isArray(d.tasks)) ? d.tasks : [];
      if (!tasks.length) {
        el.innerHTML = '<p class="meta" style="padding:1rem;">暂无发布记录。在对话中让龙虾「生成图片并发到抖音」试试。</p>';
        return;
      }
      el.innerHTML = '<div class="card">' + tasks.map(function(t) {
        var statusColor = TASK_COLORS[t.status] || '#888';
        var statusLabel = TASK_STATUS[t.status] || t.status;
        var resultLink = t.result_url ? ' <a href="' + escapeAttr(t.result_url) + '" target="_blank" style="color:var(--primary);">查看</a>' : '';
        var errorText = t.error ? '<div style="color:#f87171;font-size:0.78rem;margin-top:0.25rem;">' + escapeHtml(t.error) + '</div>' : '';
        var acctInfo = (t.platform ? (PLATFORM_NAMES[t.platform] || t.platform) : '') +
          (t.account_nickname ? ' · ' + t.account_nickname : '');
        var stepsHtml = _renderSteps(t.steps || []);
        return '<div style="padding:0.75rem 0;border-bottom:1px solid rgba(255,255,255,0.06);">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;">' +
            '<div><span style="font-weight:600;">' + escapeHtml(t.title || '无标题') + '</span>' +
            ' <span style="font-size:0.78rem;color:var(--text-muted);">素材:' + escapeHtml(t.asset_id) +
            (acctInfo ? ' · ' + escapeHtml(acctInfo) : '') + '</span></div>' +
            '<span style="color:' + statusColor + ';font-weight:600;font-size:0.85rem;">' + statusLabel + resultLink + '</span>' +
          '</div>' +
          errorText +
          stepsHtml +
          '<div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem;">' +
            escapeHtml(t.created_at.substring(0, 16).replace('T', ' ')) +
            (t.finished_at ? ' → ' + escapeHtml(t.finished_at.substring(0, 16).replace('T', ' ')) : '') +
          '</div>' +
        '</div>';
      }).join('') + '</div>';
    })
    .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
}

// ── Refresh button ───────────────────────────────────────────────

var refreshPubBtn = document.getElementById('refreshPublishBtn');
if (refreshPubBtn) {
  refreshPubBtn.addEventListener('click', function() {
    if (_currentPubTab === 'accounts') loadAccounts();
    if (_currentPubTab === 'assets') loadAssets();
    if (_currentPubTab === 'tasks') loadTasks();
  });
}

function initPublishView() {
  loadAccounts();
}
