// ── Tab switching ───────────────────────────────────────────────────
var _currentStoreTab = 'popular';

document.querySelectorAll('.store-tab').forEach(function(tab) {
  tab.addEventListener('click', function() {
    var target = tab.getAttribute('data-store-tab');
    if (!target || target === _currentStoreTab) return;
    _currentStoreTab = target;
    document.querySelectorAll('.store-tab').forEach(function(t) { t.classList.remove('active'); });
    tab.classList.add('active');
    document.getElementById('storeTabPopular').style.display = (target === 'popular') ? '' : 'none';
    document.getElementById('storeTabOfficial').style.display = (target === 'official') ? '' : 'none';
    if (target === 'official' && !_officialLoaded) {
      browseOfficialPage(1);
    }
  });
});

// ── 热门 Tab: local skills ──────────────────────────────────────────

var _xskillStatus = { has_token: false, token: '', url: '' };

function _renderXSkillCard() {
  var configured = _xskillStatus.has_token;
  var statusBadge = configured
    ? '<span class="badge-installed">已配置</span>'
    : '<span class="badge-coming" style="background:rgba(251,146,60,0.15);color:#fb923c;border-color:rgba(251,146,60,0.3);">未配置</span>';
  var guide = configured ? '' :
    '<div style="margin-top:0.6rem;padding:0.6rem 0.75rem;background:rgba(251,146,60,0.06);border:1px solid rgba(251,146,60,0.18);border-radius:8px;font-size:0.8rem;color:var(--text-muted);line-height:1.6;">' +
      '<div style="font-weight:600;color:#fb923c;margin-bottom:0.3rem;">获取 Token 步骤：</div>' +
      '<div>1. 打开 <a href="https://www.51aigc.cc" target="_blank" style="color:var(--primary);">51aigc.cc</a> ，微信扫码 或 手机号登录</div>' +
      '<div>2. 登录后点击 <a href="https://www.51aigc.cc/#/userInfo" target="_blank" style="color:var(--primary);">个人中心</a> 复制 API Token</div>' +
      '<div>3. 回到这里点击「配置 Token」粘贴即可</div>' +
    '</div>';
  var configBtn = (EDITION === 'online')
    ? '<span class="btn btn-ghost btn-sm" style="cursor:default;color:var(--text-muted);">由速推登录提供</span>'
    : '<button type="button" class="btn btn-primary btn-sm" id="xskillConfigBtn">' + (configured ? '修改 Token' : '配置 Token') + '</button>';
  if (EDITION === 'online') guide = '';
  return '<div class="skill-store-card" style="border-color:rgba(6,182,212,0.25);background:linear-gradient(135deg,rgba(6,182,212,0.06),transparent);">' +
    '<div class="card-label">MCP · 内置 ' + statusBadge + '</div>' +
    '<div class="card-value">xSkill AI (速推)</div>' +
    '<div class="card-desc">图片生成、视频生成、视频解析、语音合成、音色克隆等 50+ AI 模型能力</div>' +
    '<div class="card-tags"><span class="tag">图片</span><span class="tag">视频</span><span class="tag">音频</span><span class="tag">AI创作</span></div>' +
    guide +
    '<div class="card-actions">' +
      configBtn +
      '<a href="https://xskill.ai" target="_blank" rel="noopener" class="btn btn-ghost btn-sm">官网</a>' +
    '</div></div>';
}

function _loadXSkillStatus(cb) {
  fetch(API_BASE + '/api/sutui/config', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _xskillStatus = { has_token: !!d.has_token, token: d.token || '', url: d.url || '' };
      if (cb) cb();
    })
    .catch(function() { if (cb) cb(); });
}

function loadSkillStore() {
  var el = document.getElementById('skillStoreList');
  if (!el) return;
  el.innerHTML = '<p class="meta">加载中…</p>';

  _loadXSkillStatus(function() {
  fetch(API_BASE + '/skills/store', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var packages = (d && Array.isArray(d.packages)) ? d.packages : [];
        var html = _renderXSkillCard();
        html += packages.map(function(pkg) {
          if (pkg.id === 'sutui_mcp') return '';
        if (pkg.id === 'wecom_reply') {
          var tags = (pkg.tags || []).map(function(t) { return '<span class="tag">' + escapeHtml(t) + '</span>'; }).join('');
          var capCount = pkg.capabilities_count ? ' · ' + pkg.capabilities_count + ' 个能力' : '';
          return '<div class="skill-store-card wecom-reply-card" style="cursor:pointer;" data-platform="wecom">' +
            '<div class="card-label">' + escapeHtml(pkg.type || 'skill') + ' <span class="badge-installed">可配置</span></div>' +
            '<div class="card-value">' + escapeHtml(pkg.name || pkg.id) + '</div>' +
            '<div class="card-desc">' + escapeHtml(pkg.description || '') + capCount + '</div>' +
            '<div class="card-tags">' + tags + '</div>' +
            '<div class="card-actions"><button type="button" class="btn btn-primary btn-sm wecom-config-entry-btn">配置</button></div></div>';
        }
        var statusBadge = '';
        var actionBtn = '';
        var priceYuan = pkg.unlock_price_yuan;
        var needUnlock = priceYuan && !pkg.unlocked;
        if (pkg.status === 'installed') {
          statusBadge = '<span class="badge-installed">已安装</span>';
          actionBtn = pkg.unlock_price_yuan ? '' : '<button type="button" class="btn btn-ghost btn-sm" data-uninstall="' + escapeAttr(pkg.id) + '">卸载</button>';
        } else if (pkg.status === 'coming_soon') {
          statusBadge = '<span class="badge-coming">即将推出</span>';
        } else if (needUnlock) {
          statusBadge = '<span class="badge-muted">付费解锁</span>';
          actionBtn = '<button type="button" class="btn btn-primary btn-sm" data-unlock="' + escapeAttr(pkg.id) + '" data-amount="' + (priceYuan || 0) + '" data-name="' + escapeAttr(pkg.name || pkg.id) + '">解锁 ¥' + (priceYuan || 0) + '</button>';
        } else {
          actionBtn = '<button type="button" class="btn btn-primary btn-sm" data-install="' + escapeAttr(pkg.id) + '">安装</button>';
        }
        var tags = (pkg.tags || []).map(function(t) { return '<span class="tag">' + escapeHtml(t) + '</span>'; }).join('');
          var capCount = pkg.capabilities_count ? ' · ' + pkg.capabilities_count + ' 个能力' : '';
        return '<div class="skill-store-card">' +
          '<div class="card-label">' + escapeHtml(pkg.type || 'skill') + ' ' + statusBadge + '</div>' +
          '<div class="card-value">' + escapeHtml(pkg.name || pkg.id) + '</div>' +
            '<div class="card-desc">' + escapeHtml(pkg.description || '') + capCount + '</div>' +
          '<div class="card-tags">' + tags + '</div>' +
          '<div class="card-actions">' + actionBtn + '</div></div>';
      }).join('');
        el.innerHTML = html;
        _bindWecomConfigEntry();
        _bindInstallUninstall(el);
        _bindXSkillConfigBtn();
      })
      .catch(function() { el.innerHTML = '<p class="msg err">加载失败</p>'; });
  });
}

// ── 企业微信配置入口 ─────────────────────────────────────────────────

function _bindWecomConfigEntry() {
  document.querySelectorAll('.wecom-reply-card').forEach(function(card) {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.card-actions')) return;
      if (typeof showWecomConfigView === 'function') {
        location.hash = 'wecom-config';
        showWecomConfigView();
      }
    });
  });
  document.querySelectorAll('.wecom-config-entry-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (typeof showWecomConfigView === 'function') {
        location.hash = 'wecom-config';
        showWecomConfigView();
      }
    });
  });
}

// ── xSkill Token Modal ──────────────────────────────────────────────

function _bindXSkillConfigBtn() {
  if (EDITION === 'online') return;
  var btn = document.getElementById('xskillConfigBtn');
  if (!btn) return;
  btn.addEventListener('click', function() {
    var modal = document.getElementById('xskillModal');
    var tokenInput = document.getElementById('xskillTokenInput');
    var urlInput = document.getElementById('xskillUrlInput');
    if (!modal) return;
    if (tokenInput) { tokenInput.value = ''; tokenInput.placeholder = _xskillStatus.has_token ? '已配置 (' + _xskillStatus.token + ')' : 'sk-...'; }
    if (urlInput) urlInput.value = _xskillStatus.url || '';
    modal.classList.add('visible');
  });
}

(function _initXSkillModal() {
  var modal = document.getElementById('xskillModal');
  if (!modal) return;
  var cancelBtn = document.getElementById('xskillModalCancel');
  var saveBtn = document.getElementById('xskillModalSave');

  function closeModal() { modal.classList.remove('visible'); }

  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', function(e) { if (e.target === modal) closeModal(); });

  if (saveBtn) saveBtn.addEventListener('click', function() {
    var tokenInput = document.getElementById('xskillTokenInput');
    var urlInput = document.getElementById('xskillUrlInput');
    var msgEl = document.getElementById('xskillModalMsg');
    var body = {};
    if (tokenInput && tokenInput.value.trim()) body.token = tokenInput.value.trim();
    if (urlInput && urlInput.value.trim()) body.url = urlInput.value.trim();
    if (!body.token && !_xskillStatus.has_token) {
      if (msgEl) { msgEl.textContent = '请输入 Token'; msgEl.className = 'msg err'; msgEl.style.display = ''; }
      return;
    }
    saveBtn.disabled = true; saveBtn.textContent = '保存中…';
    fetch(API_BASE + '/api/sutui/config', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify(body)
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          if (msgEl) { msgEl.textContent = '保存成功'; msgEl.className = 'msg'; msgEl.style.display = ''; }
          setTimeout(function() { closeModal(); loadSkillStore(); }, 600);
        } else {
          if (msgEl) { msgEl.textContent = x.data.detail || '保存失败'; msgEl.className = 'msg err'; msgEl.style.display = ''; }
        }
      })
      .catch(function() { if (msgEl) { msgEl.textContent = '网络错误'; msgEl.className = 'msg err'; msgEl.style.display = ''; } })
      .finally(function() { saveBtn.disabled = false; saveBtn.textContent = '保存'; });
  });
})();

function _bindInstallUninstall(el) {
      el.querySelectorAll('button[data-install]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var pkgId = btn.getAttribute('data-install');
          btn.disabled = true; btn.textContent = '安装中…';
          fetch(API_BASE + '/skills/install', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_id: pkgId }) })
            .then(function(r) { return r.json().then(function(d) { return { status: r.status, ok: r.ok, data: d }; }); })
            .then(function(x) {
              if (x.ok) { alert(x.data.message || '安装成功'); loadSkillStore(); }
              else if (x.status === 402 && x.data && x.data.need_payment) {
                alert('该技能需付费解锁（¥' + (x.data.amount_yuan || 0) + '）。请点击「解锁」按钮下单，支付完成后联系管理员完成解锁。');
                loadSkillStore();
              } else { alert(x.data.detail || '安装失败'); }
              btn.disabled = false; btn.textContent = '安装';
            }).catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '安装'; });
        });
      });
      el.querySelectorAll('button[data-unlock]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var pkgId = btn.getAttribute('data-unlock');
          var amount = btn.getAttribute('data-amount');
          var name = btn.getAttribute('data-name') || pkgId;
          btn.disabled = true; btn.textContent = '下单中…';
          fetch(API_BASE + '/skills/create-unlock-order', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_id: pkgId }) })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(x) {
              if (x.ok && x.data) {
                var msg = '订单已创建：' + (x.data.out_trade_no || '') + '\n金额：¥' + (x.data.amount_yuan || amount) + '\n\n' + (x.data.payment_info || '请通过微信/支付宝转账并联系管理员完成到账，备注订单号。');
                alert(msg);
                loadSkillStore();
              } else { alert(x.data.detail || '创建订单失败'); }
              btn.disabled = false; btn.textContent = '解锁 ¥' + amount;
            }).catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '解锁 ¥' + amount; });
        });
      });
      el.querySelectorAll('button[data-uninstall]').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var pkgId = btn.getAttribute('data-uninstall');
          if (!confirm('确定卸载 ' + pkgId + '？')) return;
          btn.disabled = true; btn.textContent = '卸载中…';
          fetch(API_BASE + '/skills/uninstall', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_id: pkgId }) })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(x) {
          if (x.ok) { alert(x.data.message || '卸载成功'); loadSkillStore(); }
              else { alert(x.data.detail || '卸载失败'); btn.disabled = false; btn.textContent = '卸载'; }
            }).catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '卸载'; });
        });
      });
}

// Add MCP Modal
(function() {
  var modal = document.getElementById('addMcpModal');
  var openBtn = document.getElementById('openAddMcpModal');
  var cancelBtn = document.getElementById('addMcpModalCancel');
  var addBtn = document.getElementById('addMcpBtn');
  if (!modal) return;

  function closeModal() { modal.classList.remove('visible'); }

  if (openBtn) openBtn.addEventListener('click', function() {
    var nameInput = document.getElementById('addMcpName');
    var urlInput = document.getElementById('addMcpUrl');
    var msgEl = document.getElementById('addMcpMsg');
    if (nameInput) nameInput.value = '';
    if (urlInput) urlInput.value = '';
    if (msgEl) msgEl.style.display = 'none';
    modal.classList.add('visible');
  });
  if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', function(e) { if (e.target === modal) closeModal(); });

  if (addBtn) addBtn.addEventListener('click', function() {
    var nameInput = document.getElementById('addMcpName');
    var urlInput = document.getElementById('addMcpUrl');
    var msgEl = document.getElementById('addMcpMsg');
    var name = (nameInput.value || '').trim();
    var url = (urlInput.value || '').trim();
    if (!name || !url) { showMsg(msgEl, '请填写名称和 URL', true); return; }
    addBtn.disabled = true;
    fetch(API_BASE + '/skills/add-mcp', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({ name: name, url: url })
    })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          showMsg(msgEl, x.data.message || 'MCP 已添加', false);
          setTimeout(function() { closeModal(); loadSkillStore(); }, 600);
        } else { showMsg(msgEl, x.data.detail || '添加失败', true); }
      })
      .catch(function() { showMsg(msgEl, '网络错误', true); })
      .finally(function() { addBtn.disabled = false; });
  });
})();

var refreshStoreBtn = document.getElementById('refreshStoreBtn');
if (refreshStoreBtn) {
  refreshStoreBtn.addEventListener('click', function() {
    loadSkillStore();
    if (_currentStoreTab === 'official') browseOfficialPage(_officialPage);
  });
}

// ── 官方在线 Tab: paginated browsing + cached search ───────────────

var _officialPage = 1;
var _officialHasNext = false;
var _officialLoaded = false;
var _activeCategory = null;
var _searchMode = false;

var CATEGORY_LABELS = {
  image: '图片', video: '视频', audio: '音频', database: '数据库',
  search: '搜索/爬虫', code: '代码/Git', file: '文件', ai: 'AI/LLM',
  communication: '通讯', devops: 'DevOps'
};

function renderCategoryBar(categories) {
  var bar = document.getElementById('mcpCategoryBar');
  if (!bar || !categories) return;
  var keys = Object.keys(categories);
  if (!keys.length) { bar.innerHTML = ''; return; }

  var html = '<span class="category-chip' + (!_activeCategory ? ' active' : '') + '" data-cat="">全部</span>';
  keys.forEach(function(cat) {
    var label = CATEGORY_LABELS[cat] || cat;
    var active = (_activeCategory === cat) ? ' active' : '';
    html += '<span class="category-chip' + active + '" data-cat="' + escapeAttr(cat) + '">' +
      escapeHtml(label) + '<span class="chip-count">(' + categories[cat] + ')</span></span>';
  });
  bar.innerHTML = html;
  bar.querySelectorAll('.category-chip').forEach(function(chip) {
    chip.addEventListener('click', function() {
      var cat = chip.getAttribute('data-cat') || '';
      _activeCategory = cat || null;
      searchCachedSkills(
        (document.getElementById('mcpRegistrySearch') || {}).value || '',
        cat || null, 1
      );
    });
  });
}

function browseOfficialPage(page) {
  var el = document.getElementById('mcpRegistryResults');
  var pagingEl = document.getElementById('mcpRegistryPaging');
  var totalEl = document.getElementById('mcpRegistryTotal');
  if (!el) return;
  _searchMode = false;
  _activeCategory = null;
  var searchInput = document.getElementById('mcpRegistrySearch');
  if (searchInput) searchInput.value = '';

  el.innerHTML = '<p class="meta">加载第 ' + page + ' 页…</p>';
  if (pagingEl) pagingEl.innerHTML = '';

  fetch(API_BASE + '/api/mcp-registry/browse?page=' + page, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      _officialLoaded = true;
      _officialPage = d.page || page;
      _officialHasNext = !!d.has_next;
      var servers = (d && Array.isArray(d.servers)) ? d.servers : [];
      if (d.categories) renderCategoryBar(d.categories);
      if (totalEl) totalEl.textContent = '本地已缓存 ' + (d.cached_total || 0) + ' 个技能';

      if (!servers.length) {
        el.innerHTML = '<p class="meta">该页没有更多技能了</p>';
      } else {
        _renderServerCards(el, servers);
      }
      _renderBrowsePaging(pagingEl);
    })
    .catch(function() { el.innerHTML = '<p class="msg err">网络错误，请确认可访问外网</p>'; });
}

function searchCachedSkills(query, category, page) {
  var el = document.getElementById('mcpRegistryResults');
  var pagingEl = document.getElementById('mcpRegistryPaging');
  var totalEl = document.getElementById('mcpRegistryTotal');
  if (!el) return;
  _searchMode = true;

  el.innerHTML = '<p class="meta">搜索中…</p>';
  if (pagingEl) pagingEl.innerHTML = '';

  var params = ['page=' + (page || 1), 'page_size=30'];
  if (query) params.push('q=' + encodeURIComponent(query));
  if (category) params.push('category=' + encodeURIComponent(category));
  var url = API_BASE + '/api/mcp-registry/search?' + params.join('&');

  fetch(url, { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var servers = (d && Array.isArray(d.servers)) ? d.servers : [];
      if (d.categories) renderCategoryBar(d.categories);
      var total = d.total || 0;
      var curPage = d.page || 1;
      var hasNext = !!d.has_next;
      if (totalEl) totalEl.textContent = '搜索到 ' + total + ' 个';

      if (!servers.length) {
        el.innerHTML = '<p class="meta">本地缓存中未找到匹配技能。试试先浏览几页「官方在线」让龙虾缓存更多，或换个关键词。</p>';
      } else {
        _renderServerCards(el, servers);
      }
      _renderSearchPaging(pagingEl, query, category, curPage, hasNext, total);
    })
    .catch(function() { el.innerHTML = '<p class="msg err">搜索失败</p>'; });
}

function _renderServerCards(el, servers) {
  el.innerHTML = servers.map(function(srv) {
    var hasRemote = srv.remote_url && srv.remote_url.indexOf('{') < 0;
    var addBtn = hasRemote
      ? '<button type="button" class="btn btn-primary btn-sm" data-add-registry-name="' + escapeAttr(srv.name) + '" data-add-registry-url="' + escapeAttr(srv.remote_url) + '">添加</button>'
      : '';
    var linkBtn = srv.website
      ? '<a href="' + escapeAttr(srv.website) + '" target="_blank" rel="noopener" class="btn btn-ghost btn-sm">官网</a>'
      : (srv.repo ? '<a href="' + escapeAttr(srv.repo) + '" target="_blank" rel="noopener" class="btn btn-ghost btn-sm">源码</a>' : '');
    var version = srv.version ? '<span class="tag">v' + escapeHtml(srv.version) + '</span>' : '';
    var tagHtml = (srv.tags || []).map(function(t) {
      var label = CATEGORY_LABELS[t] || t;
      return '<span class="tag">' + escapeHtml(label) + '</span>';
    }).join('');
    return '<div class="skill-store-card">' +
      '<div class="card-label">MCP ' + version + '</div>' +
      '<div class="card-value">' + escapeHtml(srv.title || srv.name) + '</div>' +
      '<div class="card-desc">' + escapeHtml(srv.description || '') + '</div>' +
      '<div class="card-tags">' + tagHtml + '</div>' +
      '<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.25rem;word-break:break-all;">' + escapeHtml(srv.name) + '</div>' +
      '<div class="card-actions">' + addBtn + linkBtn + '</div></div>';
  }).join('');
  _bindAddButtons(el);
}

function _renderBrowsePaging(pagingEl) {
  if (!pagingEl) return;
  var html = '';
  if (_officialPage > 1) {
    html += '<button type="button" class="btn btn-ghost btn-sm" id="pagePrev">上一页</button>';
  }
  html += '<span class="paging-info">第 ' + _officialPage + ' 页</span>';
  if (_officialHasNext) {
    html += '<button type="button" class="btn btn-primary btn-sm" id="pageNext">下一页</button>';
  }
  pagingEl.innerHTML = html;
  var prevBtn = document.getElementById('pagePrev');
  var nextBtn = document.getElementById('pageNext');
  if (prevBtn) prevBtn.addEventListener('click', function() { browseOfficialPage(_officialPage - 1); });
  if (nextBtn) nextBtn.addEventListener('click', function() { browseOfficialPage(_officialPage + 1); });
}

function _renderSearchPaging(pagingEl, query, category, curPage, hasNext, total) {
  if (!pagingEl) return;
  var html = '';
  if (curPage > 1) {
    html += '<button type="button" class="btn btn-ghost btn-sm" id="searchPrev">上一页</button>';
  }
  html += '<span class="paging-info">第 ' + curPage + ' 页 · 共 ' + total + ' 个</span>';
  if (hasNext) {
    html += '<button type="button" class="btn btn-primary btn-sm" id="searchNext">下一页</button>';
  }
  html += '<button type="button" class="btn btn-ghost btn-sm" id="backToBrowse" style="margin-left:0.5rem;">返回浏览</button>';
  pagingEl.innerHTML = html;
  var prev = document.getElementById('searchPrev');
  var next = document.getElementById('searchNext');
  var back = document.getElementById('backToBrowse');
  if (prev) prev.addEventListener('click', function() { searchCachedSkills(query, category, curPage - 1); });
  if (next) next.addEventListener('click', function() { searchCachedSkills(query, category, curPage + 1); });
  if (back) back.addEventListener('click', function() { browseOfficialPage(1); });
}

function _bindAddButtons(container) {
  container.querySelectorAll('button[data-add-registry-name]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var name = btn.getAttribute('data-add-registry-name') || '';
      var url = btn.getAttribute('data-add-registry-url') || '';
      var shortName = name.replace(/[^a-zA-Z0-9_-]/g, '_').replace(/_+/g, '_');
      btn.disabled = true; btn.textContent = '添加中…';
      fetch(API_BASE + '/skills/add-mcp', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ name: shortName, url: url })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.ok) {
            btn.textContent = '已添加'; btn.className = 'btn btn-ghost btn-sm';
            loadSkillStore();
          } else { alert(x.data.detail || '添加失败'); btn.disabled = false; btn.textContent = '添加'; }
        })
        .catch(function() { alert('网络错误'); btn.disabled = false; btn.textContent = '添加'; });
    });
  });
}

// search bar + enter key
var mcpSearchBtn = document.getElementById('mcpRegistrySearchBtn');
var mcpSearchInput = document.getElementById('mcpRegistrySearch');
if (mcpSearchBtn) {
  mcpSearchBtn.addEventListener('click', function() {
    var q = mcpSearchInput ? mcpSearchInput.value.trim() : '';
    if (!q && !_activeCategory) { browseOfficialPage(1); return; }
    searchCachedSkills(q, _activeCategory, 1);
  });
}
if (mcpSearchInput) {
  mcpSearchInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      var q = mcpSearchInput.value.trim();
      if (!q && !_activeCategory) { browseOfficialPage(1); return; }
      searchCachedSkills(q, _activeCategory, 1);
    }
  });
}

function initOnlineSkillStore() {
  if (_currentStoreTab === 'official' && !_officialLoaded) {
    browseOfficialPage(1);
  }
}
