var API_BASE = '';
var token = localStorage.getItem('token');
var currentView = 'chat';
var EDITION = 'online';
/** 在线版是否允许自配模型（由 /api/edition 返回） */
var ALLOW_SELF_CONFIG_MODEL = true;
/** 在线版充值页 URL（由 /api/edition 返回） */
var RECHARGE_URL = null;

(function applyTokenFromUrl() {
  var params = new URLSearchParams(window.location.search);
  var t = params.get('token');
  if (t && t.length > 10) {
    token = t;
    localStorage.setItem('token', t);
    window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
  }
})();

function showMsg(el, text, isErr) {
  if (!el) return;
  el.textContent = text;
  el.className = 'msg ' + (isErr ? 'err' : 'ok');
  el.style.display = 'block';
}

function copyToClipboard(text, doneCb) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function() { if (doneCb) doneCb(); }).catch(function() {
      fallbackCopy(text, doneCb);
    });
  } else {
    fallbackCopy(text, doneCb);
  }
}
function fallbackCopy(text, doneCb) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed'; ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); if (doneCb) doneCb(); } catch (e) {}
  document.body.removeChild(ta);
}

function authHeaders() {
  return { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token || '') };
}

function escapeHtml(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function escapeAttr(s) { return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function truncate(s, len) { s = (s || '').trim(); return s.length <= len ? s : s.slice(0, len) + '…'; }
