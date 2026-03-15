var CHAT_SESSIONS_KEY = 'lobster_chat_sessions';
var chatSessions = [];
var currentSessionId = null;
var chatHistory = [];
var chatPendingBySession = {};
var chatAttachmentIds = [];
var chatAttachmentInfos = [];

function getSessionById(id) {
  var sid = id != null ? String(id) : '';
  return chatSessions.find(function(s) { return String(s.id) === sid; }) || null;
}
function isSessionPending(id) {
  return !!chatPendingBySession[String(id)];
}
function setSessionPending(id, pending) {
  var sid = String(id || '');
  if (!sid) return;
  if (pending) chatPendingBySession[sid] = true;
  else delete chatPendingBySession[sid];
  var s = getSessionById(sid);
  if (s) s.pending = !!pending;
  refreshChatInputState();
  renderChatSessionList();
}
function refreshChatInputState() {
  var input = document.getElementById('chatInput');
  var btn = document.getElementById('chatSendBtn');
  if (!btn) return;
  btn.disabled = !!(currentSessionId && isSessionPending(currentSessionId));
  if (input) input.disabled = false;
}
function renderCurrentSessionMessages() {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  container.innerHTML = '';
  var sid = currentSessionId ? String(currentSessionId) : '';
  var session = getSessionById(sid);
  var messages = session && Array.isArray(session.messages) ? session.messages : [];
  chatHistory = messages.slice();
  messages.forEach(function(m) { appendChatMessage(m.role, m.content); });
  if (sid && isSessionPending(sid)) showChatTypingIndicator();
  container.scrollTop = container.scrollHeight;
  refreshChatInputState();
}

function loadChatSessionsFromStorage() {
  try {
    var raw = localStorage.getItem(CHAT_SESSIONS_KEY);
    if (!raw) return;
    var parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      chatSessions = parsed;
      chatSessions.forEach(function(s) {
        if (s.id != null) s.id = String(s.id);
        var m = s.messages || s.history;
        s.messages = Array.isArray(m) ? m : [];
      });
    }
  } catch (e) {}
}
loadChatSessionsFromStorage();
function saveChatSessionsToStorage() {
  try {
    localStorage.setItem(CHAT_SESSIONS_KEY, JSON.stringify(chatSessions));
  } catch (e) {}
}
function getSessionTitle(session) {
  var msg = (session.messages || []).find(function(m) { return m.role === 'user' && (m.content || '').trim(); });
  if (msg) {
    var t = (msg.content || '').trim();
    return t.length > 24 ? t.slice(0, 24) + '…' : t;
  }
  return session.title || '新对话';
}
function getSessionPreview(session) {
  var messages = session.messages || [];
  for (var i = messages.length - 1; i >= 0; i--) {
    var m = messages[i];
    if (m && (m.content || '').trim()) {
      var t = (m.content || '').trim();
      return t.length > 32 ? t.slice(0, 32) + '…' : t;
    }
  }
  return '暂无消息';
}
function formatSessionTime(ts) {
  if (!ts) return '';
  var d = new Date(ts);
  var now = new Date();
  var diff = (now - d) / 60000;
  if (diff < 1) return '刚刚';
  if (diff < 60) return Math.floor(diff) + ' 分钟前';
  if (diff < 1440) return Math.floor(diff / 60) + ' 小时前';
  if (diff < 43200) return Math.floor(diff / 1440) + ' 天前';
  return d.toLocaleDateString();
}
function createNewSession() {
  var id = 's' + Date.now();
  var session = { id: id, title: '新对话', messages: [], updatedAt: Date.now(), pending: false };
  chatSessions.unshift(session);
  saveChatSessionsToStorage();
  switchChatSession(id);
  renderChatSessionList();
}
function switchChatSession(id) {
  var sid = id != null ? String(id) : '';
  if (currentSessionId === sid) return;
  saveCurrentSessionToStore();
  currentSessionId = sid;
  renderCurrentSessionMessages();
  renderChatSessionList();
}
function saveCurrentSessionToStore() {
  if (!currentSessionId) return;
  var session = chatSessions.find(function(s) { return String(s.id) === String(currentSessionId); });
  if (session) {
    session.messages = Array.isArray(chatHistory) ? chatHistory.slice() : [];
    session.updatedAt = Date.now();
    if (session.messages.length) {
      var firstUser = session.messages.find(function(m) { return m && m.role === 'user'; });
      if (firstUser && (firstUser.content || '').trim()) session.title = getSessionTitle(session);
    }
    saveChatSessionsToStorage();
  }
}
window.addEventListener('beforeunload', function() { if (typeof saveCurrentSessionToStore === 'function') saveCurrentSessionToStore(); });
function renderChatSessionList() {
  var listEl = document.getElementById('chatSessionList');
  var searchVal = (document.getElementById('chatSessionSearch') && document.getElementById('chatSessionSearch').value || '').trim().toLowerCase();
  if (!listEl) return;
  var filtered = searchVal
    ? chatSessions.filter(function(s) {
        var title = getSessionTitle(s); var preview = getSessionPreview(s);
        return title.toLowerCase().indexOf(searchVal) >= 0 || preview.toLowerCase().indexOf(searchVal) >= 0;
      })
    : chatSessions.slice();
  if (filtered.length === 0) {
    listEl.innerHTML = '<p class="meta" style="padding:0.5rem;font-size:0.8rem;color:var(--text-muted);">暂无对话</p>';
    return;
  }
  listEl.innerHTML = filtered.map(function(s) {
    var title = getSessionTitle(s);
    var preview = getSessionPreview(s);
    var time = formatSessionTime(s.updatedAt);
    var active = s.id === currentSessionId ? ' active' : '';
    return '<div class="chat-session-item' + active + '" data-session-id="' + escapeAttr(s.id) + '">' +
      '<div class="session-title">' + escapeHtml(title) + '</div>' +
      '<div class="session-preview">' + escapeHtml(preview) + '</div>' +
      '<div class="session-time">' + escapeHtml(time) + '</div></div>';
  }).join('');
  listEl.querySelectorAll('.chat-session-item').forEach(function(el) {
    el.addEventListener('click', function() { switchChatSession(el.getAttribute('data-session-id')); });
  });
}
function initChatSessions() {
  loadChatSessionsFromStorage();
  if (chatSessions.length === 0) {
    createNewSession();
    return;
  }
  var targetId = currentSessionId;
  if (!targetId || !chatSessions.find(function(s) { return s.id === targetId; })) {
    targetId = chatSessions[0].id;
  }
  currentSessionId = null;
  setTimeout(function() {
    if (document.getElementById('chatMessages')) switchChatSession(targetId);
    renderChatSessionList();
  }, 0);
}

function linkifyText(text) {
  var escaped = (text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  var result = escaped.replace(/https?:\/\/[^\s<>"]+/g, function(raw) {
    var url = raw;
    var suffix = '';
    while (/[)\]}\u3002\uff0c\uff01\uff1f,.]$/.test(url)) {
      if (url.endsWith(')')) {
        var opens = (url.match(/\(/g) || []).length;
        var closes = (url.match(/\)/g) || []).length;
        if (closes <= opens) break;
      }
      suffix = url.slice(-1) + suffix;
      url = url.slice(0, -1);
    }
    var rewritten = url.replace(/^https?:\/\/(?:localhost|127\.0\.0\.1):8000\/media\//, window.location.origin + '/media/');
    return '<a href="' + rewritten + '" target="_blank" rel="noopener noreferrer">' + rewritten + '</a>' + suffix;
  });
  result = result.replace(/(?<![a-zA-Z0-9\/">=])\/media\/[^\s<>"]+/g, function(path) {
    var full = window.location.origin + path;
    return '<a href="' + full + '" target="_blank" rel="noopener noreferrer">' + full + '</a>';
  });
  return result;
}
function appendChatMessage(role, content) {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  var div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  var text = (content || '').trim() || '（无内容）';
  var html = linkifyText(text);
  div.innerHTML = '<div class="role">' + (role === 'user' ? '我' : '龙虾') + '</div>' + html;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}
var _toolNameLabels = {
  invoke_capability: '调用能力',
  publish_content: '发布内容',
  list_assets: '查看素材',
  list_publish_accounts: '查看账号',
  check_account_login: '检查登录',
  open_account_browser: '打开浏览器'
};
function _toolLabel(name) { return _toolNameLabels[name] || name; }

function showChatTypingIndicator() {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  var div = document.createElement('div');
  div.id = 'chatTypingIndicator';
  div.className = 'chat-msg assistant typing';
  div.innerHTML = '<div class="role">龙虾</div><div class="typing-dots"><span></span><span></span><span></span></div> <span class="typing-text">正在思考...</span><div class="typing-steps" id="chatTypingSteps"></div>';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}
function appendChatTypingStep(text) {
  var steps = document.getElementById('chatTypingSteps');
  if (!steps) return;
  var line = document.createElement('div');
  line.className = 'typing-step';
  line.style.cssText = 'font-size:0.82rem;color:var(--text-muted);margin-top:0.35rem;';
  line.textContent = text;
  steps.appendChild(line);
  var container = document.getElementById('chatMessages');
  if (container) container.scrollTop = container.scrollHeight;
}
function updateLastChatTypingStep(text) {
  var steps = document.getElementById('chatTypingSteps');
  if (!steps || !steps.lastElementChild) return;
  steps.lastElementChild.textContent = text;
  var container = document.getElementById('chatMessages');
  if (container) container.scrollTop = container.scrollHeight;
}
function setChatTypingMainText(text) {
  var el = document.querySelector('#chatTypingIndicator .typing-text');
  if (el) el.textContent = text || '正在思考...';
}
function removeChatTypingIndicator() {
  var el = document.getElementById('chatTypingIndicator');
  if (el && el.parentNode) el.parentNode.removeChild(el);
}
function appendAssistantMessageReveal(fullText) {
  var container = document.getElementById('chatMessages');
  if (!container) return;
  var text = (fullText || '').trim() || '（无内容）';
  var lines = text.split('\n');
  var div = document.createElement('div');
  div.className = 'chat-msg assistant';
  var roleDiv = document.createElement('div');
  roleDiv.className = 'role';
  roleDiv.textContent = '龙虾';
  var bodyDiv = document.createElement('div');
  bodyDiv.className = 'chat-msg-body';
  div.appendChild(roleDiv);
  div.appendChild(bodyDiv);
  container.appendChild(div);
  var lineDelay = 150;
  var i = 0;
  function showNext() {
    if (i >= lines.length) {
      container.scrollTop = container.scrollHeight;
      return;
    }
    var line = lines[i];
    var lineEl = document.createElement('div');
    lineEl.className = 'chat-msg-line';
    lineEl.innerHTML = linkifyText(line);
    bodyDiv.appendChild(lineEl);
    i++;
    container.scrollTop = container.scrollHeight;
    if (i < lines.length) setTimeout(showNext, lineDelay);
  }
  if (lines.length) setTimeout(showNext, lineDelay); else container.scrollTop = container.scrollHeight;
}
function renderChatAttachments() {
  var container = document.getElementById('chatAttachments');
  if (!container) return;
  if (chatAttachmentIds.length === 0) {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }
  container.style.display = 'flex';
  container.innerHTML = '';
  chatAttachmentInfos.forEach(function(info, idx) {
    var wrap = document.createElement('div');
    wrap.className = 'chat-attach-item';
    if (info.media_type === 'video') {
      var v = document.createElement('video');
      v.src = info.previewUrl || '';
      v.muted = true;
      v.playsInline = true;
      wrap.appendChild(v);
    } else {
      var img = document.createElement('img');
      img.src = info.previewUrl || '';
      img.alt = '附件';
      wrap.appendChild(img);
    }
    var rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'attach-remove';
    rm.textContent = '×';
    rm.setAttribute('data-idx', String(idx));
    rm.addEventListener('click', function() {
      var i = parseInt(rm.getAttribute('data-idx'), 10);
      if (chatAttachmentInfos[i] && chatAttachmentInfos[i].previewUrl) {
        try { URL.revokeObjectURL(chatAttachmentInfos[i].previewUrl); } catch (e) {}
      }
      chatAttachmentIds.splice(i, 1);
      chatAttachmentInfos.splice(i, 1);
      renderChatAttachments();
    });
    wrap.appendChild(rm);
    container.appendChild(wrap);
  });
}
function addChatAttachment(assetId, mediaType) {
  chatAttachmentIds.push(assetId);
  var info = { asset_id: assetId, media_type: mediaType || 'image', previewUrl: '' };
  chatAttachmentInfos.push(info);
  fetch(API_BASE + '/api/assets/' + assetId + '/content', { headers: authHeaders() })
    .then(function(r) { return r.blob(); })
    .then(function(blob) {
      info.previewUrl = URL.createObjectURL(blob);
      renderChatAttachments();
    })
    .catch(function() { renderChatAttachments(); });
}
function clearChatAttachments() {
  chatAttachmentInfos.forEach(function(info) {
    if (info.previewUrl) try { URL.revokeObjectURL(info.previewUrl); } catch (e) {}
  });
  chatAttachmentIds = [];
  chatAttachmentInfos = [];
  renderChatAttachments();
}
function sendChatMessage() {
  var input = document.getElementById('chatInput');
  var btn = document.getElementById('chatSendBtn');
  if (!input || !btn) return;
  var message = (input.value || '').trim();
  if (!message && chatAttachmentIds.length === 0) return;
  if (!currentSessionId) {
    if (chatSessions.length) switchChatSession(chatSessions[0].id);
    else createNewSession();
  }
  var sid = String(currentSessionId);
  var session = getSessionById(sid);
  if (!session) return;
  if (isSessionPending(sid)) return;

  input.value = '';
  var attachIds = chatAttachmentIds.slice();
  clearChatAttachments();
  session.messages = Array.isArray(session.messages) ? session.messages : [];
  session.messages.push({ role: 'user', content: message });
  session.updatedAt = Date.now();
  if (String(currentSessionId) === sid) {
    appendChatMessage('user', message);
    chatHistory = session.messages.slice();
  }
  saveCurrentSessionToStore();
  renderChatSessionList();
  setSessionPending(sid, true);
  showChatTypingIndicator();
  var historyForRequest = session.messages.slice(0, -1);
  var modelSel = document.getElementById('modelSelect');
  var model = modelSel ? (modelSel.value || '') : '';
  var body = {
    message: message,
    history: historyForRequest,
    session_id: sid,
    context_id: null,
    model: model || undefined
  };
  if (attachIds.length) body.attachment_asset_ids = attachIds;
  var bodyStr = JSON.stringify(body);
  var headers = authHeaders();
  headers['Content-Type'] = 'application/json';
  fetch(API_BASE + '/chat/stream', { method: 'POST', headers: headers, body: bodyStr })
    .then(function(r) {
      if (!r.ok) {
        return r.json().then(function(d) { throw { status: r.status, detail: (d && d.detail) || r.statusText }; });
      }
      if (!r.body) throw new Error('No body');
      var decoder = new TextDecoder();
      var buf = '';
      var reader = r.body.getReader();
      function processChunk(result) {
        if (result.done) return Promise.resolve(null);
        buf += decoder.decode(result.value, { stream: true });
        var parts = buf.split('\n\n');
        buf = parts.pop() || '';
        for (var i = 0; i < parts.length; i++) {
          var block = parts[i];
          var dataLine = block.split('\n').filter(function(l) { return l.indexOf('data:') === 0; })[0];
          if (!dataLine) continue;
          try {
            var ev = JSON.parse(dataLine.slice(5).trim());
            if (ev.type === 'tool_start' && String(currentSessionId) === sid) {
              if (ev.phase === 'video_submit') {
                appendChatTypingStep('已提交视频生成任务…');
              } else if (ev.phase === 'task_polling') {
                setChatTypingMainText('视频生成中，请稍候…');
                appendChatTypingStep('正在获取生成结果（约 1–3 分钟）… 请保持本页打开。');
              } else {
                appendChatTypingStep('正在 ' + _toolLabel(ev.name) + '…');
              }
            } else if (ev.type === 'tool_end' && String(currentSessionId) === sid) {
              if (ev.phase === 'video_submit') {
                updateLastChatTypingStep('✓ 任务已提交，正在生成视频（约 1–3 分钟）…');
              } else if (ev.phase === 'task_polling') {
                var stillInProgress = ev.in_progress === true;
                updateLastChatTypingStep(stillInProgress ? '正在查询生成结果…' : '✓ 视频已生成');
              } else {
                updateLastChatTypingStep('✓ ' + _toolLabel(ev.name) + ' 完成');
              }
            } else if (ev.type === 'task_poll' && String(currentSessionId) === sid && ev.message) {
              var line = ev.message;
              if (ev.task_id) line += ' · task_id: ' + ev.task_id;
              if (ev.result_hint) line += ' · ' + ev.result_hint;
              setChatTypingMainText(line);
            } else if (ev.type === 'status' && String(currentSessionId) === sid && ev.message) {
              appendChatTypingStep(ev.message);
            } else if (ev.type === 'done') {
              return Promise.resolve(ev);
            }
          } catch (e) {}
        }
        return reader.read().then(processChunk);
      }
      return reader.read().then(processChunk);
    })
    .then(function(doneEv) {
      var targetSession = getSessionById(sid);
      if (!targetSession) return;
      if (String(currentSessionId) === sid) removeChatTypingIndicator();
      var reply = (doneEv && doneEv.reply) ? doneEv.reply : (doneEv ? '' : '请求异常结束');
      targetSession.messages = Array.isArray(targetSession.messages) ? targetSession.messages : [];
      targetSession.messages.push({ role: 'assistant', content: reply });
      targetSession.updatedAt = Date.now();
      if (String(currentSessionId) === sid) {
        appendAssistantMessageReveal(reply);
        chatHistory = targetSession.messages.slice();
      }
      saveChatSessionsToStorage();
    })
    .catch(function(e) {
      var targetSession = getSessionById(sid);
      var msg = (e && e.detail) ? e.detail : (e && e.message ? e.message : '请稍后重试');
      if (targetSession) {
        targetSession.messages = Array.isArray(targetSession.messages) ? targetSession.messages : [];
        targetSession.messages.push({ role: 'assistant', content: '错误：' + msg });
        targetSession.updatedAt = Date.now();
      }
      if (String(currentSessionId) === sid) {
        removeChatTypingIndicator();
        appendChatMessage('assistant', '错误：' + msg);
        if (targetSession) chatHistory = targetSession.messages.slice();
      }
      saveChatSessionsToStorage();
    })
    .finally(function() {
      setSessionPending(sid, false);
      if (String(currentSessionId) === sid) removeChatTypingIndicator();
    });
}
var chatSendBtn = document.getElementById('chatSendBtn');
var chatInput = document.getElementById('chatInput');
var chatAttachBtn = document.getElementById('chatAttachBtn');
var chatFileInput = document.getElementById('chatFileInput');
if (chatSendBtn) chatSendBtn.addEventListener('click', sendChatMessage);
if (chatAttachBtn && chatFileInput) {
  chatAttachBtn.addEventListener('click', function() { chatFileInput.click(); });
  chatFileInput.addEventListener('change', function() {
    var files = chatFileInput.files;
    if (!files || !files.length) return;
    for (var i = 0; i < files.length; i++) {
      (function(file) {
        var fd = new FormData();
        fd.append('file', file);
        fetch(API_BASE + '/api/assets/upload', { method: 'POST', headers: { 'Authorization': 'Bearer ' + (typeof token !== 'undefined' ? token : '') }, body: fd })
          .then(function(r) { return r.json(); })
          .then(function(d) {
            if (d && d.asset_id) addChatAttachment(d.asset_id, d.media_type || 'image');
          })
          .catch(function() {});
      })(files[i]);
    }
    chatFileInput.value = '';
  });
}
if (chatInput) {
  var chatInputComposing = false;
  chatInput.addEventListener('compositionstart', function() { chatInputComposing = true; });
  chatInput.addEventListener('compositionend', function() { chatInputComposing = false; });
  chatInput.addEventListener('keydown', function(e) {
    if (chatInputComposing || e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
  });
}
var chatNewSessionBtn = document.getElementById('chatNewSessionBtn');
if (chatNewSessionBtn) chatNewSessionBtn.addEventListener('click', createNewSession);
var chatSessionSearch = document.getElementById('chatSessionSearch');
if (chatSessionSearch) chatSessionSearch.addEventListener('input', renderChatSessionList);
