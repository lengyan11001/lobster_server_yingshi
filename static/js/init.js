var USE_INDEPENDENT_AUTH = false;
var USE_OWN_WECHAT_LOGIN = false;
var USE_OWN_WECHAT_PAY = false;
(function fetchEdition() {
  fetch(API_BASE + '/api/edition').then(function(r) { return r.ok ? r.json() : {}; }).then(function(d) {
    EDITION = (d && d.edition) === 'online' ? 'online' : 'online';
    USE_INDEPENDENT_AUTH = !!(d && d.use_independent_auth);
    USE_OWN_WECHAT_LOGIN = !!(d && d.use_own_wechat_login);
    USE_OWN_WECHAT_PAY = !!(d && d.use_own_wechat_pay);
    if (EDITION === 'online' && d) {
      ALLOW_SELF_CONFIG_MODEL = d.allow_self_config_model !== false;
      RECHARGE_URL = (d.recharge_url && d.recharge_url.trim()) ? d.recharge_url.trim() : null;
    }
    var sb = document.getElementById('sutuiLoginBlock');
    var form = document.getElementById('loginForm');
    var registerBlock = document.getElementById('registerBlock');
    var ownWechatBlock = document.getElementById('ownWechatLoginBlock');
    if (EDITION === 'online' && USE_INDEPENDENT_AUTH) {
      if (sb) sb.style.display = 'none';
      if (form) form.style.display = '';
      if (registerBlock) registerBlock.style.display = '';
      if (ownWechatBlock) {
        if (USE_OWN_WECHAT_LOGIN) {
          ownWechatBlock.style.display = 'block';
          startOwnWechatLogin();
        } else {
          ownWechatBlock.style.display = 'none';
        }
      }
    } else if (EDITION === 'online' && sb) {
      sb.style.display = 'block';
      if (form) form.style.display = 'none';
      if (registerBlock) registerBlock.style.display = 'none';
      startSutuiQrLogin();
    } else if (sb && form) {
      sb.style.display = 'none';
      if (form) form.style.display = '';
      if (registerBlock) registerBlock.style.display = 'none';
    }
    if (EDITION !== 'online') {
      var billingNav = document.querySelector('.nav-left-item[data-view="billing"]');
      var billingContent = document.getElementById('content-billing');
      if (billingNav) billingNav.style.display = 'none';
      if (billingContent) billingContent.style.display = 'none';
    }
  }).catch(function() {});
})();

var _sutuiQrPollTimer = null;
var _sutuiQrTimeoutTimer = null;
function startSutuiQrLogin() {
  var img = document.getElementById('sutuiQrImg');
  var status = document.getElementById('sutuiQrStatus');
  var expired = document.getElementById('sutuiQrExpired');
  var refreshBtn = document.getElementById('sutuiQrRefresh');
  if (!img || !status) return;
  if (_sutuiQrPollTimer) clearInterval(_sutuiQrPollTimer);
  if (_sutuiQrTimeoutTimer) clearTimeout(_sutuiQrTimeoutTimer);
  img.style.display = 'none';
  expired.style.display = 'none';
  status.textContent = '正在获取二维码…';
  fetch(API_BASE + '/auth/sutui-qrcode').then(function(r) { return r.json(); }).then(function(d) {
    if (!d.url || !d.scene_id) { status.textContent = '获取二维码失败'; return; }
    img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=256x256&data=' + encodeURIComponent(d.url);
    img.style.display = 'inline';
    status.textContent = '请使用微信扫描二维码';
    var sceneId = d.scene_id;
    _sutuiQrPollTimer = setInterval(function() {
      fetch(API_BASE + '/auth/sutui-qrcode-status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scene_id: sceneId, from_user_id: 0 })
      }).then(function(r) { return r.json(); }).then(function(res) {
        if (res.code === 200 && res.data && res.data.token) {
          clearInterval(_sutuiQrPollTimer);
          _sutuiQrPollTimer = null;
          if (_sutuiQrTimeoutTimer) clearTimeout(_sutuiQrTimeoutTimer);
          status.textContent = '扫码成功，正在登录…';
          fetch(API_BASE + '/auth/sutui-login-with-token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: res.data.token })
          }).then(function(r) { return r.json(); }).then(function(t) {
            if (t.access_token) {
              token = t.access_token;
              localStorage.setItem('token', token);
              loadDashboard();
            } else { status.textContent = '登录失败'; }
          }).catch(function() { status.textContent = '登录请求失败'; });
        }
      }).catch(function() {});
    }, 2000);
    _sutuiQrTimeoutTimer = setTimeout(function() {
      if (_sutuiQrPollTimer) clearInterval(_sutuiQrPollTimer);
      _sutuiQrPollTimer = null;
      status.textContent = '二维码已过期';
      expired.style.display = 'block';
    }, 5 * 60 * 1000);
  }).catch(function() { status.textContent = '获取二维码失败'; });
  if (refreshBtn) {
    refreshBtn.onclick = function(e) { e.preventDefault(); startSutuiQrLogin(); };
  }
}

function startOwnWechatLogin() {
  var img = document.getElementById('ownWechatQrImg');
  var status = document.getElementById('ownWechatQrStatus');
  var link = document.getElementById('ownWechatLink');
  if (!status) return;
  status.textContent = '正在获取…';
  if (img) img.style.display = 'none';
  if (link) link.style.display = 'none';
  fetch(API_BASE + '/auth/wechat-login-url')  // 自建微信扫码登录.then(function(r) { return r.json(); }).then(function(d) {
    var url = (d && d.login_url) || '';
    if (!url) { status.textContent = '获取失败'; return; }
    status.textContent = '请使用微信扫描二维码登录';
    if (img) {
      img.src = 'https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=' + encodeURIComponent(url);
      img.style.display = 'inline';
    }
    if (link) {
      link.href = url;
      link.style.display = 'inline-block';
      link.textContent = '打开微信扫码登录';
    }
  }).catch(function() { status.textContent = '获取失败'; });
}

(function applyTokenFromUrl() {
  var m = /[?&]token=([^&]+)/.exec(window.location.search || '');
  if (!m || !m[1]) return;
  var t = decodeURIComponent(m[1]);
  token = t;
  localStorage.setItem('token', t);
  if (window.opener) {
    try { window.opener.postMessage({ type: 'sutui_login_ok', token: t }, '*'); } catch (e) {}
    window.close();
  } else {
    setTimeout(function() { loadDashboard(); }, 0);
  }
})();

window.addEventListener('message', function(e) {
  if (e.data && e.data.type === 'sutui_login_ok' && e.data.token) {
    token = e.data.token;
    localStorage.setItem('token', token);
    loadDashboard();
  }
});

document.getElementById('loginForm').addEventListener('submit', function(e) {
  e.preventDefault();
  var fd = new FormData(this);
  var body = new URLSearchParams({ username: fd.get('username'), password: fd.get('password') });
  var msgEl = document.getElementById('loginMsg');
  fetch(API_BASE + '/auth/login', { method: 'POST', body: body, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(x) {
      if (x.ok) {
        token = x.data.access_token;
        localStorage.setItem('token', token);
        showMsg(msgEl, '登录成功', false);
        loadDashboard();
      } else { showMsg(msgEl, x.data.detail || '登录失败', true); }
    })
    .catch(function() { showMsg(msgEl, '网络错误', true); });
});
var registerForm = document.getElementById('registerForm');
if (registerForm) {
  registerForm.addEventListener('submit', function(e) {
    e.preventDefault();
    var email = (document.getElementById('registerEmail') || {}).value || '';
    var password = (document.getElementById('registerPassword') || {}).value || '';
    var msgEl = document.getElementById('registerMsg');
    if (password.length < 6) { showMsg(msgEl, '密码至少 6 位', true); return; }
    fetch(API_BASE + '/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email, password: password }) })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(x) {
        if (x.ok) {
          token = x.data.access_token;
          localStorage.setItem('token', token);
          showMsg(msgEl, '注册成功', false);
          loadDashboard();
        } else { showMsg(msgEl, x.data.detail || '注册失败', true); }
      })
      .catch(function() { showMsg(msgEl, '网络错误', true); });
  });
}

function loadDashboard() {
  if (!token) {
    document.getElementById('authPanel').style.display = 'block';
    document.getElementById('dashboard').classList.remove('visible');
    document.getElementById('headerActions').style.display = 'none';
    var heroEl = document.getElementById('pageHero');
    if (heroEl) heroEl.style.display = '';
    return;
  }
  fetch(API_BASE + '/auth/me', { headers: { 'Authorization': 'Bearer ' + token } })
    .then(function(r) {
      if (r.status === 401) { token = null; localStorage.removeItem('token'); loadDashboard(); return null; }
      return r.json();
    })
    .then(function(d) {
      if (!d) return;
      document.getElementById('userEmail').textContent = d.email;
      document.getElementById('headerUserEmail').textContent = (d.email || '').split('@')[0];
      document.getElementById('headerActions').style.display = 'flex';
      document.getElementById('authPanel').style.display = 'none';
      document.getElementById('dashboard').classList.add('visible');
      var heroEl = document.getElementById('pageHero');
      if (heroEl) heroEl.style.display = 'none';
      if (typeof window._applyWecomConfigHash === 'function') window._applyWecomConfigHash();
      loadModelSelector(d.preferred_model);
      initChatSessions();
      var accNav = document.querySelector('.nav-left-item[data-view="consumption-accounts"]');
      if (accNav) accNav.style.display = (EDITION === 'online' && USE_INDEPENDENT_AUTH) ? '' : 'none';
      if (EDITION === 'online') {
        loadSutuiBalance();
        var rBtn = document.getElementById('sutuiRechargeBtn');
        if (USE_INDEPENDENT_AUTH && rBtn) {
          rBtn.onclick = function(e) { e.preventDefault(); document.querySelector('.nav-left-item[data-view="billing"]') && document.querySelector('.nav-left-item[data-view="billing"]').click(); };
        }
      } else {
        var w = document.getElementById('sutuiBalanceWrap');
        if (w) w.style.display = 'none';
      }
    });
}

var _modelSelectorBound = false;
function loadModelSelector(preferredModel) {
  var sel = document.getElementById('modelSelect');
  if (!sel) return;
  var row = sel.closest('.model-selector');
  if (EDITION === 'online' && row) { row.style.display = 'none'; return; }
  if (row) row.style.display = '';
  if (preferredModel) sel.setAttribute('data-preferred', preferredModel);
  var pref = preferredModel || sel.getAttribute('data-preferred') || '';
  fetch(API_BASE + '/api/settings/models', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var models = (d && Array.isArray(d.models)) ? d.models : [];
      if (!models.length) return;
      var curVal = sel.value;
      sel.innerHTML = models.map(function(m) {
        var selected = (m.id === pref || m.id === curVal) ? ' selected' : '';
        var label = m.custom ? m.name + ' (自定义)' : m.name;
        return '<option value="' + escapeAttr(m.id) + '"' + selected + '>' + escapeHtml(label) + '</option>';
      }).join('');
    })
    .catch(function() {});
  if (!_modelSelectorBound) {
    _modelSelectorBound = true;
    sel.addEventListener('change', function() {
      fetch(API_BASE + '/api/settings', {
        method: 'POST', headers: authHeaders(),
        body: JSON.stringify({ preferred_model: sel.value })
      }).catch(function() {});
    });
  }
}

function refreshModelSelector() {
  loadModelSelector();
}

document.getElementById('logout').addEventListener('click', function() {
  token = null;
  localStorage.removeItem('token');
  document.getElementById('dashboard').classList.remove('visible');
  document.getElementById('authPanel').style.display = 'block';
  document.getElementById('headerActions').style.display = 'none';
  var heroEl = document.getElementById('pageHero');
  if (heroEl) heroEl.style.display = '';
});

(function initDropdown() {
  var dropdown = document.getElementById('headerUserDropdown');
  var btn = document.getElementById('headerDropdownBtn');
  if (dropdown && btn) {
    btn.addEventListener('click', function(e) { e.stopPropagation(); dropdown.classList.toggle('open'); });
    document.addEventListener('click', function() { dropdown.classList.remove('open'); });
  }
})();

document.querySelectorAll('.nav-left-item').forEach(function(el) {
  el.addEventListener('click', function() {
    var view = el.dataset.view;
    if (!view) return;
    if (currentView === 'chat' && view !== 'chat' && typeof saveCurrentSessionToStore === 'function') saveCurrentSessionToStore();
    if (view === 'billing' && typeof EDITION !== 'undefined' && EDITION !== 'online') view = 'chat';
    document.querySelectorAll('.nav-left-item').forEach(function(b) { b.classList.remove('active'); });
    var navEl = document.querySelector('.nav-left-item[data-view="' + view + '"]');
    if (navEl) navEl.classList.add('active'); else el.classList.add('active');
    document.querySelectorAll('.content-block').forEach(function(p) { p.classList.remove('visible'); });
    var contentId = 'content-' + view;
    var contentEl = document.getElementById(contentId);
    if (contentEl) contentEl.classList.add('visible');
    currentView = view;
    if (view === 'chat') refreshModelSelector();
    if (view === 'skill-store') { loadSkillStore(); if (typeof initOnlineSkillStore === 'function') initOnlineSkillStore(); }
    if (view === 'publish') { if (typeof initPublishView === 'function') initPublishView(); }
    if (view === 'production') { if (typeof initProductionView === 'function') initProductionView(); }
    if (view === 'billing') { if (typeof loadBillingView === 'function') loadBillingView(); }
    if (view === 'consumption-accounts') { if (typeof loadConsumptionAccounts === 'function') loadConsumptionAccounts(); }
    if (view === 'sys-config') { loadOpenClawConfig(); }
    if (view === 'logs') { if (typeof ensureLogsBindings === 'function') ensureLogsBindings(); }
  });
});

window.addEventListener('beforeunload', function() {
  if (typeof saveCurrentSessionToStore === 'function') saveCurrentSessionToStore();
});

function loadSutuiBalance() {
  var wrap = document.getElementById('sutuiBalanceWrap');
  var textEl = document.getElementById('sutuiBalanceText');
  var rechargeBtn = document.getElementById('sutuiRechargeBtn');
  if (!wrap || !textEl) return;
  wrap.style.display = 'flex';
  if (USE_INDEPENDENT_AUTH) {
    textEl.textContent = '积分：加载中…';
    if (rechargeBtn) { rechargeBtn.style.display = ''; rechargeBtn.href = '#'; rechargeBtn.textContent = '充值'; }
    fetch(API_BASE + '/auth/me', { headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(function(d) { textEl.textContent = '积分：' + (d && d.credits != null ? d.credits : '--'); })
      .catch(function() { textEl.textContent = '积分：--'; });
    return;
  }
  textEl.textContent = '余额：加载中…';
  if (rechargeBtn) {
    rechargeBtn.style.display = RECHARGE_URL ? '' : 'none';
    rechargeBtn.href = RECHARGE_URL || '#';
    rechargeBtn.textContent = '充值';
  }
  fetch(API_BASE + '/api/sutui/balance', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.error) {
        textEl.textContent = '余额：--';
        return;
      }
      var yuan = (d.balance_yuan != null) ? String(d.balance_yuan) : (d.balance != null ? (d.balance / 1000).toFixed(2) : '--');
      textEl.textContent = '余额：' + yuan + ' 元';
    })
    .catch(function() { textEl.textContent = '余额：--'; });
}

function loadBillingView() {
  var balanceEl = document.getElementById('billingBalance');
  var listEl = document.getElementById('billingLogList');
  var refreshBtn = document.getElementById('billingRefreshBtn');
  var pricingBlock = document.getElementById('billingPricingBlock');
  var pricingContent = document.getElementById('billingPricingContent');
  if (!listEl) return;
  listEl.innerHTML = '<p class="meta" style="padding:1rem;">加载中…</p>';
  if (pricingContent) {
    fetch(API_BASE + '/api/billing/pricing', { headers: authHeaders() })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) {
        if (!pricingContent) return;
        if (!d) { pricingContent.innerHTML = '<span class="meta">收费说明加载失败</span>'; return; }
        var skill = d.skill_unlock || {};
        var minY = skill.min_yuan != null ? skill.min_yuan : 98;
        var maxY = skill.max_yuan != null ? skill.max_yuan : 198;
        var packages = d.credit_packages || [];
        var html = '<p style="margin:0 0 0.5rem 0;"><strong>1、技能解锁</strong>：' + minY + '–' + maxY + ' 元（按技能不同）。</p>';
        if (packages.length) {
          html += '<p style="margin:0 0 0.35rem 0;"><strong>2、算力套餐（积分）</strong>：</p><ul style="margin:0;padding-left:1.25rem;">';
          packages.forEach(function(p) {
            html += '<li>' + escapeHtml(p.label || (p.price_yuan + '元 - ' + p.credits + '积分')) + '</li>';
          });
          html += '</ul>';
        } else {
          html += '<p style="margin:0;"><strong>2、算力套餐</strong>：198元/2000积分、498元/5000积分、998元/12000积分。</p>';
        }
        pricingContent.innerHTML = html;
      })
      .catch(function() { if (pricingContent) pricingContent.innerHTML = '<span class="meta">收费说明加载失败</span>'; });
  }
  if (balanceEl) {
    if (typeof EDITION !== 'undefined' && EDITION !== 'online') {
      balanceEl.textContent = '仅显示能力调用记录。';
    } else if (USE_INDEPENDENT_AUTH) {
      balanceEl.textContent = '我的积分：加载中…';
    } else {
      balanceEl.textContent = '速推余额：加载中…';
    }
  }
  function renderBalance(d) {
    if (!balanceEl || (typeof EDITION !== 'undefined' && EDITION !== 'online')) return;
    if (d && d.error) {
      balanceEl.textContent = '速推余额：' + (d.error || '--');
      return;
    }
    var yuan = (d && d.balance_yuan != null) ? String(d.balance_yuan) : (d && d.balance != null ? (d.balance / 1000).toFixed(2) : '--');
    balanceEl.textContent = '速推余额：' + yuan + ' 元' + (d && d.vip_level ? '（VIP' + d.vip_level + '）' : '');
  }
  if (USE_INDEPENDENT_AUTH && EDITION === 'online') {
    fetch(API_BASE + '/auth/me', { headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(function(d) { if (balanceEl) balanceEl.textContent = '我的积分：' + (d && d.credits != null ? d.credits : '--'); })
      .catch(function() { if (balanceEl) balanceEl.textContent = '我的积分：--'; });
    var rechargeBlock = document.getElementById('rechargeBlock');
    if (rechargeBlock) {
      rechargeBlock.style.display = '';
      var rechargeTitle = rechargeBlock.querySelector('h4');
      if (rechargeTitle) rechargeTitle.textContent = '积分充值';
      fetch(API_BASE + '/api/recharge/packages', { headers: authHeaders() })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(opts) {
          var amountSel = document.getElementById('rechargeAmount');
          if (amountSel && opts && Array.isArray(opts.packages) && opts.packages.length) {
            amountSel.innerHTML = opts.packages.map(function(p, i) {
              return '<option value="' + i + '" data-credits="' + (p.credits || 0) + '">' + escapeHtml(p.label || (p.price_yuan + '元 - ' + p.credits + '积分')) + '</option>';
            }).join('');
          }
        })
        .catch(function() {});
    }
    var rechargeSubmitBtn = document.getElementById('rechargeSubmitBtn');
    var rechargeMsg = document.getElementById('rechargeMsg');
    var rechargeResult = document.getElementById('rechargeResult');
    if (rechargeSubmitBtn && !rechargeSubmitBtn._ownRechargeBound) {
      rechargeSubmitBtn._ownRechargeBound = true;
      rechargeSubmitBtn.addEventListener('click', function() {
        var amountEl = document.getElementById('rechargeAmount');
        var idx = amountEl ? parseInt(amountEl.value, 10) : -1;
        if (!amountEl || idx < 0) { showMsg(rechargeMsg, '请选择套餐', true); return; }
        if (rechargeResult) { rechargeResult.style.display = 'none'; rechargeResult.innerHTML = ''; }
        rechargeSubmitBtn.disabled = true;
        showMsg(rechargeMsg, '正在创建订单…', false);
        var apiUrl = USE_OWN_WECHAT_PAY ? (API_BASE + '/api/recharge/wechat-create') : (API_BASE + '/api/recharge/create');
        fetch(apiUrl, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ package_index: idx }) })
          .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
          .then(function(x) {
            if (!x.ok && x.data && x.data.detail) { showMsg(rechargeMsg, x.data.detail, true); return; }
            var d = x.data || {};
            showMsg(rechargeMsg, '', false);
            if (rechargeResult) {
              if (USE_OWN_WECHAT_PAY && d.code_url) {
                rechargeResult.innerHTML = '<p><strong>订单号：' + escapeHtml(d.out_trade_no || '') + '</strong></p><p>请使用微信扫描下方二维码完成支付：</p><img src="https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=' + encodeURIComponent(d.code_url) + '" alt="支付二维码" style="max-width:220px;height:auto;margin-top:0.5rem;">';
              } else {
                rechargeResult.innerHTML = '<p><strong>订单号：' + escapeHtml(d.out_trade_no || '') + '</strong></p><p>' + escapeHtml(d.payment_info || '') + '</p>';
              }
              rechargeResult.style.display = 'block';
            }
            if (typeof loadSutuiBalance === 'function') loadSutuiBalance();
            if (balanceEl) fetch(API_BASE + '/auth/me', { headers: authHeaders() }).then(function(r) { return r.json(); }).then(function(me) { balanceEl.textContent = '我的积分：' + (me && me.credits != null ? me.credits : '--'); });
          })
          .catch(function() { showMsg(rechargeMsg, '网络错误', true); })
          .finally(function() { rechargeSubmitBtn.disabled = false; });
      });
    }
  } else if (typeof EDITION !== 'undefined' && EDITION === 'online') {
    fetch(API_BASE + '/api/sutui/balance', { headers: authHeaders() })
      .then(function(r) { return r.json(); })
      .then(renderBalance)
      .catch(function() { if (balanceEl) balanceEl.textContent = '速推余额：--'; });
    var rechargeBlock = document.getElementById('rechargeBlock');
    if (rechargeBlock) {
      rechargeBlock.style.display = '';
      fetch(API_BASE + '/api/sutui/recharge-options', { headers: authHeaders() })
        .then(function(r) { return r.json(); })
        .then(function(opts) {
          var amountSel = document.getElementById('rechargeAmount');
          var typeSel = document.getElementById('rechargePaymentType');
          if (amountSel && Array.isArray(opts.shops) && opts.shops.length) {
            amountSel.innerHTML = opts.shops.map(function(s) {
              return '<option value="' + Number(s.shop_id) + '" data-yuan="' + Number(s.money_yuan) + '">' + escapeHtml(s.title) + (s.tag ? ' ' + escapeHtml(s.tag) : '') + '</option>';
            }).join('');
          } else if (amountSel && Array.isArray(opts.amounts)) {
            amountSel.innerHTML = opts.amounts.map(function(a) { return '<option value="0" data-yuan="' + Number(a) + '">' + Number(a) + ' 元</option>'; }).join('');
          }
          if (typeSel) typeSel.style.display = 'none';
        })
        .catch(function() {});
    }
    var rechargeSubmitBtn = document.getElementById('rechargeSubmitBtn');
    var rechargeMsg = document.getElementById('rechargeMsg');
    var rechargeResult = document.getElementById('rechargeResult');
    if (rechargeSubmitBtn && !rechargeSubmitBtn._rechargeBound) {
      rechargeSubmitBtn._rechargeBound = true;
      rechargeSubmitBtn.addEventListener('click', function() {
        var amountEl = document.getElementById('rechargeAmount');
        var shopId = amountEl ? parseInt(amountEl.value, 10) : 0;
        if (!amountEl || (shopId === 0 && !amountEl.options[amountEl.selectedIndex].getAttribute('data-yuan'))) {
          showMsg(rechargeMsg, '请选择充值档位', true); return;
        }
        if (rechargeResult) { rechargeResult.style.display = 'none'; rechargeResult.innerHTML = ''; }
        rechargeSubmitBtn.disabled = true;
        showMsg(rechargeMsg, '正在创建订单…', false);
        fetch(API_BASE + '/api/sutui/recharge-create', {
          method: 'POST',
          headers: authHeaders(),
          body: JSON.stringify({ shop_id: shopId })
        })
          .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, status: r.status, data: d }; }); })
          .then(function(x) {
            if (!x.ok && x.data && x.data.detail) {
              showMsg(rechargeMsg, x.data.detail, true);
              return;
            }
            var d = x.data || {};
            showMsg(rechargeMsg, '', false);
            if (d.need_oauth && d.recharge_url) {
              window.open(d.recharge_url, '_blank', 'noopener');
              if (rechargeResult) {
                rechargeResult.innerHTML = '<p>' + (d.message || '请前往速推官网完成登录后充值') + '。已为您打开充值页，若未打开<a href="' + escapeAttr(d.recharge_url) + '" target="_blank" rel="noopener" style="color:var(--primary);">点击此处</a>。</p>';
                rechargeResult.style.display = 'block';
              }
            } else if (d.pay_url) {
              window.open(d.pay_url, '_blank', 'noopener');
              if (rechargeResult) {
                rechargeResult.innerHTML = '<p>已打开支付页面，完成支付后余额将自动到账。若未打开，<a href="' + escapeAttr(d.pay_url) + '" target="_blank" rel="noopener" style="color:var(--primary);">点击此处</a>。</p>';
                rechargeResult.style.display = 'block';
              }
            } else if (d.qr_code) {
              if (rechargeResult) {
                var qr = d.qr_code;
                if (qr.indexOf('http') === 0 || qr.indexOf('data:') === 0) {
                  rechargeResult.innerHTML = '<p>请使用支付 App 扫描下方二维码：</p><img src="' + escapeAttr(qr) + '" alt="支付二维码" style="max-width:220px;height:auto;margin-top:0.5rem;">';
                } else {
                  rechargeResult.innerHTML = '<p>支付链接：<a href="' + escapeAttr(qr) + '" target="_blank" rel="noopener" style="color:var(--primary);">' + escapeHtml(qr.slice(0, 60)) + '…</a></p>';
                }
                rechargeResult.style.display = 'block';
              }
            }
            if (typeof loadSutuiBalance === 'function') loadSutuiBalance();
          })
          .catch(function() { showMsg(rechargeMsg, '网络错误', true); })
          .finally(function() { rechargeSubmitBtn.disabled = false; });
      });
    }
  } else {
    var rechargeBlock = document.getElementById('rechargeBlock');
    if (rechargeBlock) rechargeBlock.style.display = 'none';
  }
  fetch(API_BASE + '/capabilities/my-call-logs?limit=100', { headers: authHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(logs) {
      if (!listEl) return;
      if (!Array.isArray(logs) || logs.length === 0) {
        listEl.innerHTML = '<p class="meta" style="padding:1rem;">暂无调用记录。</p>';
        return;
      }
      var html = '<table style="width:100%;border-collapse:collapse;font-size:0.82rem;">';
      html += '<thead><tr style="border-bottom:1px solid var(--border);"><th style="text-align:left;padding:0.5rem;">时间</th><th style="text-align:left;padding:0.5rem;">能力</th><th style="text-align:left;padding:0.5rem;">结果</th><th style="text-align:right;padding:0.5rem;">耗时</th></tr></thead><tbody>';
      logs.forEach(function(r) {
        var time = (r.created_at || '').replace('T', ' ').slice(0, 19);
        var cap = (r.capability_id || '-');
        var ok = r.success ? '成功' : '失败';
        var err = (r.error_message || '').slice(0, 80);
        var lat = r.latency_ms != null ? r.latency_ms + ' ms' : '-';
        html += '<tr style="border-bottom:1px solid rgba(255,255,255,0.06);"><td style="padding:0.5rem;">' + escapeHtml(time) + '</td><td style="padding:0.5rem;">' + escapeHtml(cap) + '</td><td style="padding:0.5rem;">' + escapeHtml(ok) + (err ? ' ' + escapeHtml(err) : '') + '</td><td style="padding:0.5rem;text-align:right;">' + lat + '</td></tr>';
      });
      html += '</tbody></table>';
      listEl.innerHTML = html;
    })
    .catch(function() {
      if (listEl) listEl.innerHTML = '<p class="meta" style="padding:1rem;">加载失败。</p>';
    });
  if (refreshBtn) refreshBtn.onclick = loadBillingView;
}

function loadConsumptionAccounts() {
  var listEl = document.getElementById('consumptionAccountList');
  var addBtn = document.getElementById('addConsumptionAccountBtn');
  if (!listEl) return;
  listEl.innerHTML = '<p class="meta">加载中…</p>';
  fetch(API_BASE + '/api/consumption-accounts', { headers: authHeaders() })
    .then(function(r) { return r.ok ? r.json() : []; })
    .then(function(arr) {
      if (!listEl) return;
      if (!Array.isArray(arr) || arr.length === 0) {
        listEl.innerHTML = '<p class="meta">暂无算力账号，点击上方「添加算力账号」配置。</p>';
      } else {
        listEl.innerHTML = arr.map(function(a) {
          var def = a.is_default ? ' <span class="tag" style="font-size:0.7rem;">默认</span>' : '';
          var tok = a.has_token ? ' <span class="tag" style="font-size:0.7rem;">已配置 Token</span>' : '';
          return '<div class="skill-store-card" data-account-id="' + a.id + '">' +
            '<div class="card-label">算力账号' + def + tok + '</div>' +
            '<div class="card-value">' + escapeHtml(a.name) + '</div>' +
            '<div class="card-actions" style="margin-top:0.5rem;">' +
            '<button type="button" class="btn btn-ghost btn-sm edit-consumption-account" data-id="' + a.id + '" data-name="' + escapeAttr(a.name) + '" data-default="' + (a.is_default ? '1' : '0') + '">编辑</button>' +
            '<button type="button" class="btn btn-ghost btn-sm delete-consumption-account" data-id="' + a.id + '">删除</button>' +
            '</div></div>';
        }).join('');
      }
      if (addBtn && !addBtn._bound) {
        addBtn._bound = true;
        addBtn.onclick = function() {
          document.getElementById('consumptionAccountModalTitle').textContent = '添加算力账号';
          document.getElementById('consumptionAccountName').value = '';
          document.getElementById('consumptionAccountToken').value = '';
          document.getElementById('consumptionAccountDefault').checked = false;
          document.getElementById('consumptionAccountModal').dataset.editId = '';
          document.getElementById('consumptionAccountModal').style.display = 'flex';
        };
      }
      listEl.querySelectorAll('.edit-consumption-account').forEach(function(btn) {
        btn.onclick = function() {
          var id = btn.getAttribute('data-id');
          var name = btn.getAttribute('data-name') || '';
          var isDef = btn.getAttribute('data-default') === '1';
          document.getElementById('consumptionAccountModalTitle').textContent = '编辑算力账号';
          document.getElementById('consumptionAccountName').value = name;
          document.getElementById('consumptionAccountToken').value = '';
          document.getElementById('consumptionAccountDefault').checked = isDef;
          document.getElementById('consumptionAccountModal').dataset.editId = id || '';
          document.getElementById('consumptionAccountModal').style.display = 'flex';
        };
      });
      listEl.querySelectorAll('.delete-consumption-account').forEach(function(btn) {
        btn.onclick = function() {
          var id = btn.getAttribute('data-id');
          if (!id || !confirm('确定删除该算力账号？')) return;
          fetch(API_BASE + '/api/consumption-accounts/' + id, { method: 'DELETE', headers: authHeaders() })
            .then(function(r) { if (r.ok) loadConsumptionAccounts(); });
        };
      });
    })
    .catch(function() { if (listEl) listEl.innerHTML = '<p class="meta">加载失败</p>'; });
}
(function bindConsumptionAccountModal() {
  var modal = document.getElementById('consumptionAccountModal');
  var cancelBtn = document.getElementById('consumptionAccountModalCancel');
  var saveBtn = document.getElementById('consumptionAccountModalSave');
  if (!modal || !saveBtn) return;
  if (cancelBtn) cancelBtn.onclick = function() { modal.style.display = 'none'; };
  saveBtn.onclick = function() {
    var name = (document.getElementById('consumptionAccountName') || {}).value || '';
    var token = (document.getElementById('consumptionAccountToken') || {}).value || '';
    var isDefault = (document.getElementById('consumptionAccountDefault') || {}).checked;
    var msgEl = document.getElementById('consumptionAccountMsg');
    if (!name.trim()) { showMsg(msgEl, '请输入账号名称', true); return; }
    var editId = (modal.dataset || {}).editId || '';
    if (editId) {
      fetch(API_BASE + '/api/consumption-accounts/' + editId, {
        method: 'PUT',
        headers: authHeaders(),
        body: JSON.stringify({ name: name.trim(), sutui_token: token || null, is_default: isDefault })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.ok) { modal.style.display = 'none'; loadConsumptionAccounts(); }
          else showMsg(msgEl, (x.data && x.data.detail) || '保存失败', true);
        })
        .catch(function() { showMsg(msgEl, '网络错误', true); });
    } else {
      fetch(API_BASE + '/api/consumption-accounts', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ name: name.trim(), sutui_token: token || null, is_default: isDefault })
      })
        .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
        .then(function(x) {
          if (x.ok) { modal.style.display = 'none'; loadConsumptionAccounts(); }
          else showMsg(msgEl, (x.data && x.data.detail) || '添加失败', true);
        })
        .catch(function() { showMsg(msgEl, '网络错误', true); });
    }
  };
})();

function ensureLogsBindings() {
  var refreshBtn = document.getElementById('logsRefreshBtn');
  var loadBtn = document.getElementById('logsLoadBtn');
  var tailEl = document.getElementById('logsTail');
  if (refreshBtn && !refreshBtn._logsBound) {
    refreshBtn._logsBound = true;
    refreshBtn.onclick = loadLogsView;
  }
  if (loadBtn && !loadBtn._logsBound) {
    loadBtn._logsBound = true;
    loadBtn.onclick = loadLogsView;
  }
  if (tailEl && !tailEl._logsBound) {
    tailEl._logsBound = true;
    tailEl.addEventListener('change', loadLogsView);
  }
}

function loadLogsView() {
  var pre = document.getElementById('logsContent');
  var tailEl = document.getElementById('logsTail');
  if (!pre) {
    if (typeof console !== 'undefined') console.warn('[日志] #logsContent 未找到');
    return;
  }
  var tail = (tailEl && tailEl.value) ? parseInt(tailEl.value, 10) : 2000;
  pre.textContent = '加载中…';
  var url = (typeof API_BASE !== 'undefined' ? API_BASE : '') + '/api/logs?tail=' + tail;
  var timeout = 20000;
  var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  var t = ctrl ? setTimeout(function() { if (ctrl) ctrl.abort(); }, timeout) : null;
  var opts = {
    method: 'GET',
    credentials: 'same-origin',
    headers: typeof authHeaders === 'function' ? authHeaders() : { 'Authorization': 'Bearer ' + (typeof token !== 'undefined' ? token : '') }
  };
  if (ctrl) opts.signal = ctrl.signal;
  fetch(url, opts)
    .then(function(r) {
      if (t) clearTimeout(t);
      if (!r.ok) return r.text().then(function(txt) { throw new Error(txt || r.status); });
      return r.text();
    })
    .then(function(text) {
      pre.textContent = text || '(空)';
      pre.scrollTop = pre.scrollHeight;
    })
    .catch(function(e) {
      if (t) clearTimeout(t);
      var msg = (e && e.name === 'AbortError') ? '加载超时，请重试' : (e && e.message ? e.message : String(e));
      pre.textContent = '加载失败: ' + msg;
    });
  ensureLogsBindings();
}

(function initWecomConfigHash() {
  function applyHash() {
    var hash = (location.hash || '').replace(/^#/, '');
    if (hash === 'wecom-config' && typeof showWecomConfigView === 'function') showWecomConfigView();
  }
  window.addEventListener('hashchange', applyHash);
  window._applyWecomConfigHash = applyHash;
})();

if (token) loadDashboard();
