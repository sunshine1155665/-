'use strict';

/* ============================================================================
   1. 状态
   ========================================================================== */

const TOKEN_KEY = 'fatfish_token';
const SESSION_KEY = 'fatfish_session_id';
const GROUP_KEY = 'fatfish_is_group';
const TRANSCRIPT_PREFIX = 'fatfish_chat_';
const MAX_TRANSCRIPT = 100;
const MAX_MESSAGE = 2000;

const ROUTES = ['chat', 'overview', 'memory', 'people', 'lexicon', 'qzone', 'traces', 'services', 'about'];
const ROUTE_TITLES = {
  chat: '聊天',
  overview: '她的今天',
  memory: '记忆',
  people: '人物',
  lexicon: '她学会的话',
  qzone: 'QQ动态',
  traces: '轨迹',
  services: '服务',
  about: '关于',
};

const app = {
  token: '',
  sessionId: '',
  isGroup: false,
  route: '',
  chatPending: false,
  online: null,
  viewAbort: null,
  cleanupFns: [],
  poll: {
    fn: null,
    ms: 0,
    timer: null,
  },
  chat: {
    entries: [],
    refs: null,
  },
  overview: {
    state: null,
    health: null,
    refs: null,
    lastHealthAt: 0,
    lastUpdatedAt: 0,
    actionBusy: false,
  },
  memory: {
    query: '',
    limit: 20,
    status: 'active',
    seq: 0,
    debounce: null,
    refs: null,
  },
  people: {
    query: '',
    seq: 0,
    debounce: null,
    busy: '',
    refs: null,
  },
  lexicon: {
    status: 'all',
    kind: 'all',
    query: '',
    seq: 0,
    debounce: null,
    busy: false,
    mineResult: null,
    refs: null,
  },
  qzone: {
    data: null,
    feed: null,
    busy: '',
    refs: null,
  },
  traces: {
    data: null,
    decisions: null,
    wakeups: null,
    outbox: null,
    expanded: new Set(),
    sig: '',
    refs: null,
  },
  services: {
    data: null,
    busy: '',
    note: '',
    refs: null,
  },
  about: {
    config: null,
    health: null,
    refs: null,
  },
};

const overlayEl = document.getElementById('login-overlay');
const loginFormEl = document.getElementById('login-form');
const tokenInputEl = document.getElementById('token-input');
const loginErrorEl = document.getElementById('login-error');
const loginSubmitEl = document.getElementById('login-submit');
const toastHostEl = document.getElementById('toasts');
const viewEl = document.getElementById('view');
const liveDotEl = document.getElementById('live-dot');
const liveStateEl = document.getElementById('live-state');
const liveActivityEl = document.getElementById('live-activity');
const liveMoodEl = document.getElementById('live-mood');
const liveTimeEl = document.getElementById('live-time');

let entrySeq = 0;
let toastSeq = 0;
let lastFocusedEl = null;
const numberFmt = new Intl.NumberFormat('zh-CN');

/* ============================================================================
   2. 小工具 / DOM
   ========================================================================== */

const SVG_NS = 'http://www.w3.org/2000/svg';

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs && typeof attrs === 'object' && !(attrs instanceof Node) && !Array.isArray(attrs)) {
    applyAttrs(node, attrs);
  } else if (attrs != null) {
    children.unshift(attrs);
  }
  appendAll(node, children);
  return node;
}

function applyAttrs(node, attrs) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') {
      node.className = String(value);
    } else if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'dataset') {
      for (const [dataKey, dataValue] of Object.entries(value)) {
        if (dataValue != null) node.dataset[dataKey] = String(dataValue);
      }
    } else if (key === 'style' && typeof value === 'object') {
      for (const [styleKey, styleValue] of Object.entries(value)) {
        if (styleValue != null) node.style.setProperty(styleKey, String(styleValue));
      }
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value' || key === 'checked' || key === 'disabled' ||
               key === 'selected' || key === 'hidden' || key === 'multiple' ||
               key === 'required' || key === 'readOnly') {
      node[key] = value;
    } else if (value === true) {
      node.setAttribute(key, '');
    } else {
      node.setAttribute(key, String(value));
    }
  }
}

function appendAll(parent, children) {
  for (const child of children) {
    if (child == null || child === false) continue;
    if (Array.isArray(child)) {
      appendAll(parent, child);
    } else if (child instanceof Node) {
      parent.append(child);
    } else {
      parent.append(document.createTextNode(String(child)));
    }
  }
}

function textNode(value) {
  return document.createTextNode(value == null ? '' : String(value));
}

function setText(node, value, fallback) {
  if (!node) return;
  const empty = value == null || value === '';
  node.textContent = empty ? (fallback == null ? '—' : String(fallback)) : String(value);
}

function setBadge(node, text, tone) {
  if (!node) return;
  node.className = 'badge' + (tone ? ' ' + tone : '');
  node.textContent = text == null || text === '' ? '—' : String(text);
}

function whaleSvg(className) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 64 64');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  if (className) svg.setAttribute('class', className);
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', '#icon-whale');
  svg.append(use);
  return svg;
}

function iconSvg(name, className) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  if (className) svg.setAttribute('class', className);
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', '#' + name);
  svg.append(use);
  return svg;
}

function copyText(text, button) {
  const value = String(text == null ? '' : text);
  if (!value) return;
  const done = () => {
    if (!button || !button.isConnected) return;
    const label = button.querySelector('span');
    button.classList.add('is-done');
    if (label) label.textContent = '已复制';
    window.setTimeout(() => {
      if (!button.isConnected) return;
      button.classList.remove('is-done');
      if (label) label.textContent = '复制';
    }, 1600);
  };
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    navigator.clipboard.writeText(value).then(done).catch(() => fallbackCopy(value, done));
    return;
  }
  fallbackCopy(value, done);
}

function fallbackCopy(value, done) {
  const area = el('textarea', {
    class: 'visually-hidden',
    value,
    readOnly: true,
    'aria-hidden': 'true',
  });
  document.body.append(area);
  try {
    area.select();
    if (document.execCommand('copy')) done();
  } catch (_err) {
    /* 浏览器拒绝时保持安静，用户仍可手动选择文本 */
  } finally {
    area.remove();
  }
}

function copyBlock(label, value) {
  const valueEl = el('code', { class: 'copy-value', text: value });
  const button = el('button', {
    class: 'copy-button',
    type: 'button',
    'aria-label': '复制' + label,
    onClick: () => copyText(valueEl.textContent, button),
  },
  iconSvg('icon-copy'),
  el('span', { text: '复制' }),
  );
  const row = el('div', { class: 'copy-block' },
    el('div', { class: 'copy-label', text: label }),
    el('div', { class: 'copy-row' }, valueEl, button),
  );
  return { row, valueEl };
}

function badge(text, tone) {
  return el('span', { class: 'badge' + (tone ? ' ' + tone : ''), text });
}

function chip(text, extraClass) {
  return el('span', { class: 'chip' + (extraClass ? ' ' + extraClass : ''), text });
}

function skeletonList(count, cardClass) {
  const wrap = el('div', { class: 'skeleton-stack' });
  for (let i = 0; i < count; i += 1) {
    wrap.append(el('div', { class: 'skeleton ' + (cardClass || 'skeleton-card') }));
  }
  return wrap;
}

function emptyPanel(title, message) {
  return el(
    'div',
    { class: 'state-panel state-empty' },
    el('span', { class: 'state-icon', 'aria-hidden': 'true', text: '·' }),
    el('h3', { class: 'state-title', text: title }),
    message ? el('p', { class: 'state-text', text: message }) : null,
  );
}

function errorPanel(title, error, onRetry) {
  const panel = el(
    'div',
    { class: 'state-panel state-error' },
    el('span', { class: 'state-icon', 'aria-hidden': 'true', text: '!' }),
    el('h3', { class: 'state-title', text: title }),
    el('p', { class: 'state-text', text: friendlyError(error) }),
  );
  if (onRetry) {
    panel.append(el('button', {
      class: 'btn btn-ghost btn-sm',
      type: 'button',
      text: '重试',
      onClick: onRetry,
    }));
  }
  return panel;
}

function setButtonBusy(button, busy, busyLabel) {
  if (!button) return;
  if (busy) {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent || '';
    button.disabled = true;
    button.classList.add('is-busy');
    button.replaceChildren(
      el('span', { class: 'btn-spinner', 'aria-hidden': 'true' }),
      textNode(' ' + (busyLabel || '处理中…')),
    );
  } else {
    button.disabled = false;
    button.classList.remove('is-busy');
    button.textContent = button.dataset.idleLabel || '完成';
    delete button.dataset.idleLabel;
  }
}

function makeTable(headers, rows, caption) {
  const table = el('table', { class: 'data-table' });
  if (caption) table.append(el('caption', { class: 'visually-hidden', text: caption }));
  const thead = el('thead');
  const headRow = el('tr');
  for (const header of headers) {
    headRow.append(el('th', { scope: 'col', text: header }));
  }
  thead.append(headRow);
  const tbody = el('tbody');
  for (const row of rows) {
    const tr = el('tr');
    row.forEach((content, index) => {
      tr.append(tableCell(headers[index] || '', content));
    });
    tbody.append(tr);
  }
  table.append(thead, tbody);
  return el('div', { class: 'table-wrap' }, table);
}

function tableCell(label, content) {
  const cell = el('td', { dataset: { label } });
  if (content instanceof Node) {
    cell.append(content);
  } else {
    cell.textContent = content == null || content === '' ? '—' : String(content);
  }
  return cell;
}

function cardHead(title, sub, actions) {
  const head = el('div', { class: 'card-head' });
  const copy = el('div', { class: 'card-head-copy' });
  copy.append(el('h2', { class: 'card-title', text: title }));
  if (sub) copy.append(el('p', { class: 'card-sub', text: sub }));
  head.append(copy);
  if (actions) head.append(el('div', { class: 'card-actions' }, actions));
  return head;
}

function viewHeader(eyebrow, title, subtitle, actions) {
  const head = el('header', { class: 'view-head' });
  const copy = el('div', { class: 'view-head-text' });
  if (eyebrow) copy.append(el('p', { class: 'eyebrow', text: eyebrow }));
  copy.append(el('h1', { class: 'view-title', text: title }));
  if (subtitle) copy.append(el('p', { class: 'view-sub', text: subtitle }));
  head.append(copy);
  if (actions && actions.length) head.append(el('div', { class: 'view-actions' }, actions));
  return head;
}

function statTile(label, valueNode, hint) {
  const tile = el('div', { class: 'stat' });
  tile.append(valueNode, el('div', { class: 'stat-label', text: label }));
  if (hint) tile.append(el('div', { class: 'stat-hint', text: hint }));
  return tile;
}

/* ============================================================================
   3. 存储 / 格式化
   ========================================================================== */

function readStorage(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (_err) {
    return null;
  }
}

function writeStorage(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_err) {
    /* 隐私模式或配额不足时静默失败 */
  }
}

function removeStorage(key) {
  try {
    window.localStorage.removeItem(key);
  } catch (_err) {
    /* ignore */
  }
}

function uid(prefix) {
  entrySeq += 1;
  return (prefix || 'x') + '-' + Date.now().toString(36) + '-' + entrySeq.toString(36);
}

function newSessionId() {
  let part = '';
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      part = window.crypto.randomUUID().replace(/-/g, '').slice(0, 8);
    }
  } catch (_err) {
    part = '';
  }
  if (!part) {
    part = Math.random().toString(36).slice(2, 10);
  }
  return 'web-' + part;
}

function normalizeSessionId(value) {
  const clean = String(value || '').trim().slice(0, 64);
  return clean || newSessionId();
}

function transcriptKey(sessionId) {
  return TRANSCRIPT_PREFIX + sessionId;
}

function serializeEntry(entry) {
  if (!entry || typeof entry !== 'object') return null;
  if (entry.role === 'user') {
    return { id: entry.id, role: 'user', text: String(entry.text || ''), at: Number(entry.at) || Date.now() };
  }
  if (entry.role === 'error') {
    return {
      id: entry.id,
      role: 'error',
      text: String(entry.text || ''),
      retryText: String(entry.retryText || ''),
      userId: String(entry.userId || ''),
      at: Number(entry.at) || Date.now(),
    };
  }
  if (entry.role === 'her') {
    return {
      id: entry.id,
      role: 'her',
      messages: Array.isArray(entry.messages) ? entry.messages : [],
      meta: entry.meta || {},
      at: Number(entry.at) || Date.now(),
    };
  }
  return null;
}

function normalizeTts(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const segments = Array.isArray(raw.segments)
    ? raw.segments.slice(0, 32).map((item) => ({
      text: typeof item.text === 'string' ? item.text.slice(0, 2000) : '',
      ok: Boolean(item && item.ok),
      audio_url: item && typeof item.audio_url === 'string' ? item.audio_url : '',
      error: item && item.error ? String(item.error).slice(0, 300) : '',
    })).filter((item) => item.audio_url || item.text)
    : [];
  const urls = [];
  if (Array.isArray(raw.audio_urls)) {
    for (const url of raw.audio_urls) {
      if (typeof url === 'string' && url) urls.push(url);
      if (urls.length >= 32) break;
    }
  }
  if (!urls.length) {
    for (const item of segments) {
      if (item.audio_url) urls.push(item.audio_url);
      if (urls.length >= 32) break;
    }
  }
  if (!urls.length && typeof raw.audio_url === 'string' && raw.audio_url) {
    urls.push(raw.audio_url);
  }
  return {
    ok: Boolean(raw.ok),
    audio_url: urls[0] || '',
    audio_urls: urls,
    segments,
    format: typeof raw.format === 'string' ? raw.format : '',
    cached: Boolean(raw.cached),
    error: raw.error ? String(raw.error).slice(0, 300) : '',
  };
}

function normalizeMeta(raw) {
  if (!raw || typeof raw !== 'object') return {};
  const voice = raw.voice && typeof raw.voice === 'object'
    ? {
      mode: typeof raw.voice.mode === 'string' ? raw.voice.mode : '',
      score: Number(raw.voice.score),
      reasons: Array.isArray(raw.voice.reasons) ? raw.voice.reasons.map(String) : [],
    }
    : null;
  const security = raw.security && typeof raw.security === 'object'
    ? {
      trust: typeof raw.security.trust === 'string' ? raw.security.trust : '',
      risk: raw.security.risk == null ? null : raw.security.risk,
      blocked: Boolean(raw.security.blocked),
    }
    : null;
  const actions = Array.isArray(raw.actions)
    ? raw.actions.slice(0, 16).map((action) => ({
      tool: String((action && (action.tool || action.name)) || 'action').slice(0, 80),
      ok: !action || action.ok !== false,
      error: action && action.error ? String(action.error).slice(0, 300) : '',
    }))
    : [];
  const tts = normalizeTts(raw.tts);
  const sing = raw.sing && typeof raw.sing === 'object'
    ? {
      ok: Boolean(raw.sing.ok),
      audio_url: typeof raw.sing.audio_url === 'string' ? raw.sing.audio_url : '',
      emotion: typeof raw.sing.emotion === 'string' ? raw.sing.emotion.slice(0, 20) : '',
      error: raw.sing.error ? String(raw.sing.error).slice(0, 300) : '',
    }
    : null;
  return {
    voice,
    security,
    actions,
    tts,
    sing,
    stickerCaption: typeof raw.stickerCaption === 'string' ? raw.stickerCaption.slice(0, 300) : '',
  };
}

function normalizeEntry(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const at = Number(raw.at) || Date.now();
  const savedId = typeof raw.id === 'string' && raw.id ? raw.id : '';
  if (raw.role === 'user') {
    const text = String(raw.text || '').slice(0, MAX_MESSAGE);
    if (!text) return null;
    return { id: savedId || uid('m'), role: 'user', text, at };
  }
  if (raw.role === 'error') {
    const text = String(raw.text || '').slice(0, 500);
    if (!text) return null;
    return {
      id: savedId || uid('m'),
      role: 'error',
      text,
      retryText: String(raw.retryText || '').slice(0, MAX_MESSAGE),
      userId: String(raw.userId || ''),
      at,
    };
  }
  if (raw.role === 'her') {
    const messages = Array.isArray(raw.messages)
      ? raw.messages.filter((item) => typeof item === 'string' && item.trim()).map((item) => item.slice(0, 10000))
      : [];
    const meta = normalizeMeta(raw.meta);
    return { id: savedId || uid('m'), role: 'her', messages, meta, at };
  }
  return null;
}

function loadTranscript(sessionId) {
  const raw = readStorage(transcriptKey(sessionId));
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(normalizeEntry).filter(Boolean).slice(-MAX_TRANSCRIPT);
  } catch (_err) {
    return [];
  }
}

function saveTranscriptFor(sessionId, entries) {
  const safe = entries.slice(-MAX_TRANSCRIPT).map(serializeEntry).filter(Boolean);
  writeStorage(transcriptKey(sessionId), JSON.stringify(safe));
}

function saveCurrentTranscript() {
  saveTranscriptFor(app.sessionId, app.chat.entries);
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function asNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function pct(value) {
  const number = asNumber(value);
  if (number == null) return null;
  return clamp(number, 0, 100);
}

function fractionPct(value) {
  const number = asNumber(value);
  if (number == null) return null;
  return clamp(number <= 1 ? number * 100 : number, 0, 100);
}

function fmtNum(value, fallback) {
  const number = asNumber(value);
  if (number == null) return fallback == null ? '—' : String(fallback);
  return numberFmt.format(number);
}

function fmtScore(value) {
  const number = asNumber(value);
  if (number == null) return '—';
  if (number <= 1) return number.toFixed(2);
  return number.toFixed(2);
}

function fmtMs(value) {
  const number = asNumber(value);
  if (number == null) return '—';
  if (number >= 1000) return (number / 1000).toFixed(number >= 10000 ? 0 : 2) + ' s';
  return Math.round(number) + ' ms';
}

function fmtBytes(value) {
  const number = asNumber(value);
  if (number == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = Math.max(0, number);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return (size >= 100 || unit === 0 ? Math.round(size) : size.toFixed(1)) + ' ' + units[unit];
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

function toDate(value) {
  if (value == null || value === '') return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    const date = new Date(value < 1e12 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (/^\d+(\.\d+)?$/.test(trimmed)) return toDate(Number(trimmed));
    if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
      const date = new Date(trimmed + 'T00:00:00');
      return Number.isNaN(date.getTime()) ? null : date;
    }
    const date = new Date(trimmed.replace(' ', 'T'));
    return Number.isNaN(date.getTime()) ? null : date;
  }
  return null;
}

function fmtDate(value) {
  const date = toDate(value);
  if (!date) return '—';
  return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) +
    ' ' + pad2(date.getHours()) + ':' + pad2(date.getMinutes());
}

function fmtTime(value) {
  const date = toDate(value);
  if (!date) return '—';
  return pad2(date.getHours()) + ':' + pad2(date.getMinutes());
}

function fmtWhen(value, text) {
  if (text != null && String(text).trim()) return String(text);
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value.trim())) return value.trim();
  if (typeof value === 'string' && value.trim() && !/^\d+(\.\d+)?$/.test(value.trim())) {
    const date = toDate(value);
    return date ? fmtDate(date) : value.trim();
  }
  return fmtDate(value);
}

function relativeTime(value) {
  const date = toDate(value);
  if (!date) return '—';
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (seconds < 0) return fmtDate(date);
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' 小时前';
  return fmtDate(date);
}

function dayKey(value) {
  const date = toDate(value) || new Date();
  return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate());
}

function dayDividerLabel(value) {
  const date = toDate(value) || new Date();
  const today = new Date();
  const key = dayKey(date);
  const todayKey = dayKey(today);
  const yesterday = new Date(today.getTime() - 86400000);
  if (key === todayKey) return '今天';
  if (key === dayKey(yesterday)) return '昨天';
  const sameYear = date.getFullYear() === today.getFullYear();
  return (sameYear ? '' : date.getFullYear() + ' 年 ') + (date.getMonth() + 1) + ' 月 ' + date.getDate() + ' 日';
}

function isGroupedWith(entry, previous) {
  if (!entry || !previous) return false;
  if (entry.role !== previous.role) return false;
  if (entry.role === 'error') return false;
  if (dayKey(entry.at) !== dayKey(previous.at)) return false;
  return Math.abs(Number(entry.at) - Number(previous.at)) < 5 * 60 * 1000;
}

function previousChatEntry() {
  return app.chat.entries.length ? app.chat.entries[app.chat.entries.length - 1] : null;
}

function durationSince(value) {
  const date = toDate(value);
  if (!date) return '—';
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return '不到 1 分钟';
  if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟';
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return hours + ' 小时' + (minutes ? ' ' + minutes + ' 分钟' : '');
  }
  return Math.floor(seconds / 86400) + ' 天';
}

function truncate(text, max) {
  const value = String(text == null ? '' : text);
  if (value.length <= max) return value;
  return value.slice(0, Math.max(0, max - 1)) + '…';
}

function safeArray(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed;
    } catch (_err) {
      return value.split(',').map((item) => item.trim()).filter(Boolean);
    }
  }
  return [];
}

function parseMaybeJSON(value) {
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed || !/^[\[{]/.test(trimmed)) return value;
  try {
    return JSON.parse(trimmed);
  } catch (_err) {
    return value;
  }
}

function prettyJSON(value) {
  try {
    const parsed = parseMaybeJSON(value);
    if (typeof parsed === 'string') return parsed;
    const text = JSON.stringify(parsed == null ? null : parsed, null, 2);
    return text == null ? String(parsed) : text;
  } catch (_err) {
    return String(value == null ? '' : value);
  }
}

function shortValue(value) {
  if (value == null) return '—';
  if (Array.isArray(value)) return '[' + value.length + ' 项]';
  if (typeof value === 'object') return '{' + Object.keys(value).length + ' 项}';
  return truncate(String(value), 42);
}

function compactObject(value, max) {
  const parsed = parseMaybeJSON(value);
  if (parsed == null) return '—';
  if (typeof parsed !== 'object') return truncate(String(parsed), max || 150);
  if (Array.isArray(parsed)) {
    const head = parsed.slice(0, 3).map(shortValue).join('、');
    return truncate('[' + parsed.length + ' 项] ' + head, max || 150);
  }
  const parts = Object.entries(parsed).slice(0, 4).map(([key, item]) => key + ': ' + shortValue(item));
  return truncate(parts.join(' · '), max || 150);
}

function redactString(value) {
  return String(value)
    .replace(/([?&](?:key|api_key|token|secret|password)=)[^&#\s]+/gi, '$1••••')
    .replace(/(\/\/[^/\s:@]+:)[^@\s/]+@/g, '$1••••@');
}

function redactConfig(value, depth) {
  const level = depth || 0;
  if (level > 8) return '…';
  if (Array.isArray(value)) return value.map((item) => redactConfig(item, level + 1));
  if (value && typeof value === 'object') {
    const result = {};
    for (const [key, item] of Object.entries(value)) {
      if (/(api[_-]?key|secret|token|password|passwd|authorization|cookie)/i.test(key)) {
        result[key] = '••••••';
      } else {
        result[key] = redactConfig(item, level + 1);
      }
    }
    return result;
  }
  if (typeof value === 'string') return redactString(value);
  return value;
}

/* ------------------------------- 翻译表 -------------------------------- */

const STATE_LABELS = {
  idle: '发呆',
  chatting: '聊天中',
  browsing: '逛 B 站',
  generating: '创作中',
  sleeping: '睡觉中',
  busy: '忙着呢',
};

const MOOD_LABELS = {
  calm: '平静',
  happy: '开心',
  energetic: '活力满满',
  tired: '有点累',
  sleepy: '困困的',
  curious: '好奇',
  lonely: '有点想你',
  playful: '想逗你',
  grumpy: '闹别扭',
  shy: '害羞',
  excited: '雀跃',
  sad: '低落',
  neutral: '平常',
};

const PHASE_LABELS = {
  morning: '清晨',
  afternoon: '午后',
  evening: '傍晚',
  night: '深夜',
  sleeping: '睡眠中',
  chatting: '聊天中',
  idle: '空闲',
};

const ACTIVITY_LABELS = {
  idle: '发呆',
  sleeping: '睡觉',
  chatting: '聊天',
  browse_bilibili: '刷 B 站',
  read: '看书',
  rest: '休息',
  write_notes: '写笔记',
  chores: '做家务',
  eat: '吃饭',
  walk: '散步',
  listen_music: '听歌',
};

const TYPE_LABELS = {
  episodic: '经历',
  preference: '偏好',
  relationship: '关系',
  prospective: '约定',
  semantic: '知识',
  emotional: '心情',
  persona: '人设',
  diary: '日记',
};

const TIER_LABELS = {
  1: '核心',
  2: '重要',
  3: '普通',
  4: '归档',
};

const LEXICON_KIND_LABELS = {
  expression: '表达',
  jargon: '黑话',
};

const LEXICON_STATUS_LABELS = {
  candidate: '待审',
  approved: '已启用',
  disabled: '已停用',
};

const LEXICON_SOURCE_LABELS = {
  seed: '种子',
  miner: '对话挖掘',
  manual: '手动',
};

const LEXICON_STYLE_LABELS = {
  soft: '软软的',
  tsundere: '傲娇',
  playful: '俏皮',
  comfort: '安慰',
  serious: '认真',
};

function lexiconKindLabel(value) {
  return LEXICON_KIND_LABELS[String(value || '').toLowerCase()] || '表达';
}

function lexiconStatusLabel(value) {
  return LEXICON_STATUS_LABELS[String(value || '').toLowerCase()] || '待审';
}

function lexiconStatusTone(value) {
  const key = String(value || '').toLowerCase();
  if (key === 'approved') return 'tone-ok';
  if (key === 'candidate') return 'tone-warn';
  if (key === 'disabled') return 'tone-muted';
  return 'tone-muted';
}

function lexiconSourceLabel(value) {
  const key = String(value || '').toLowerCase();
  return LEXICON_SOURCE_LABELS[key] || '其他';
}

function lexiconStyleLabel(value) {
  const key = String(value || '').toLowerCase();
  return LEXICON_STYLE_LABELS[key] || '';
}

const TRUST_LABELS = {
  private: '私聊',
  group: '群聊',
  external: '外部',
  memory: '记忆',
  skill: '技能',
  system: '系统',
  internal: '内部',
  unknown: '未知',
};

const VOICE_LABELS = {
  text_only: '仅文字',
  voice_only: '仅语音',
  text_plus_voice: '文字 + 语音',
};

const TTS_MODE_LABELS = {
  local: '本地',
  remote: '远程',
  cloud: '云端',
  auto: '自动',
  edge: 'Edge',
  off: '关闭',
};

const NOTE_LABELS = {
  'turn complete': '刚刚聊完',
  'after chat': '刚聊完天，还在回味你刚才说的话。',
  'chat turn': '聊天之后',
  'idle drift': '发呆了一会儿',
  'energy decay': '精力慢慢降下来',
  'quiet hours': '到了安静时段',
  wake: '睡醒了',
  sleep: '去睡觉了',
  morning: '清晨',
  afternoon: '午后',
  evening: '傍晚',
  night: '深夜',
  midnight: '深夜',
};

const TIME_NOTE_LABELS = new Set(['清晨', '上午', '中午', '午后', '下午', '傍晚', '深夜', '夜里', '早上']);

const SCOPE_LABELS = {
  private: '私聊',
  group: '群聊',
  global: '全局',
  session: '会话',
  internal: '内部',
  external: '外部',
  system: '系统',
};

const PROFILE_LABELS = {
  low: '低配',
  standard: '标准',
  remote: '远程',
};

const PROVIDER_LABELS = {
  opencode_go: 'OpenCode Go',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
  ollama: 'Ollama',
  local: '本地模型',
};

const STATUS_LABELS = {
  pending: '等待中',
  processing: '处理中',
  fired: '已触发',
  done: '已完成',
  dead: '已失败',
  failed: '失败',
  cancelled: '已取消',
  expired: '已过期',
  open: '未完成',
  active: '活跃',
  archived: '已归档',
  blocked: '已拦截',
  ok: '成功',
  error: '错误',
  queued: '排队中',
  sent: '已发送',
  skipped: '已跳过',
};

const LOOP_KIND_LABELS = {
  promise: '约定',
  task: '待办',
  followup: '追问',
  reminder: '提醒',
  open_loop: '未完成',
};

const DECISION_KIND_LABELS = {
  chat_turn: '对话回合',
  recall: '记忆召回',
  wakeup: '唤醒',
  proactive_message: '主动消息',
  tick: '生活推进',
  nightly: '夜间整理',
  voice: '语音判断',
  style: '语气选择',
  memory: '记忆整理',
  sticker: '表情选择',
  tts: '语音合成',
  outbox: '发件箱',
  decision: '决策',
};

const DECISION_ACTION_LABELS = {
  reply: '回复',
  ephemeral_context: '临时上下文',
  wake: '唤醒',
  send: '送出',
  skip: '跳过',
  recall: '召回',
  compact: '压缩',
  retry: '重试',
  none: '无',
};

const RECALL_REASON_LABELS = {
  semantic: '语义',
  keyword: '关键词',
  importance: '重要性',
  recency: '时近',
  fts: '全文检索',
  vector: '向量',
  entity: '实体',
  tag: '标签',
  emotion: '情绪',
};

function humanize(value) {
  const text = String(value == null ? '' : value).trim();
  if (!text) return '—';
  return text.replace(/_/g, ' ');
}

function stateLabel(value) {
  return STATE_LABELS[value] || humanize(value);
}

function moodLabel(value) {
  return MOOD_LABELS[value] || humanize(value);
}

function phaseLabel(value) {
  return PHASE_LABELS[value] || humanize(value);
}

function activityLabel(value) {
  return ACTIVITY_LABELS[value] || humanize(value);
}

function typeLabel(value) {
  return TYPE_LABELS[value] || humanize(value);
}

function tierLabel(value) {
  return TIER_LABELS[value] || ('层级 ' + humanize(value));
}

function trustLabel(value) {
  return TRUST_LABELS[String(value || '').toLowerCase()] || humanize(value);
}

function voiceLabel(value) {
  return VOICE_LABELS[value] || humanize(value);
}

function singEmotionLabel(value) {
  return ({ soft: '撒娇', genki: '元气', sad: '委屈' })[value] || '唱歌';
}

function ttsModeLabel(value) {
  return TTS_MODE_LABELS[String(value || '').toLowerCase()] || humanize(value);
}

function statusLabel(value) {
  return STATUS_LABELS[String(value || '').toLowerCase()] || humanize(value);
}

function loopKindLabel(value) {
  return LOOP_KIND_LABELS[String(value || '').toLowerCase()] || humanize(value);
}

function decisionKindLabel(value) {
  const key = String(value || '').toLowerCase();
  return DECISION_KIND_LABELS[key] || humanize(value);
}

function decisionActionLabel(value) {
  const key = String(value || '').toLowerCase();
  return DECISION_ACTION_LABELS[key] || humanize(value);
}

function recallReasonLabel(value) {
  const text = String(value == null ? '' : value).trim();
  if (!text) return '';
  return text.split(/[+,，]/).map((part) => {
    const key = part.trim().toLowerCase();
    return RECALL_REASON_LABELS[key] || part.trim();
  }).filter(Boolean).join(' + ');
}

function noteLabel(value) {
  const text = String(value == null ? '' : value).trim();
  if (!text) return '';
  return NOTE_LABELS[text.toLowerCase()] || text;
}

function activityNote(value) {
  const text = noteLabel(value);
  return TIME_NOTE_LABELS.has(text) ? '' : text;
}

function providerLabel(value) {
  const key = String(value || '').toLowerCase();
  return PROVIDER_LABELS[key] || humanize(value);
}

function scopeLabel(value) {
  const key = String(value || '').toLowerCase();
  return SCOPE_LABELS[key] || humanize(value);
}

function profileLabel(value) {
  const key = String(value || '').toLowerCase();
  return PROFILE_LABELS[key] || humanize(value);
}

function modelLabel(value) {
  const text = String(value == null ? '' : value).trim();
  if (!text) return '—';
  const key = text.toLowerCase();
  if (['fallback', 'local', 'none', 'rule', 'rules'].includes(key)) {
    return ['rule', 'rules'].includes(key) ? '规则' : '本地回复';
  }
  return text;
}

function statusTone(value) {
  const key = String(value || '').toLowerCase();
  if (['done', 'ok', 'active', 'fired'].includes(key)) return 'tone-ok';
  if (['pending', 'processing', 'open'].includes(key)) return 'tone-info';
  if (['failed', 'dead', 'error', 'blocked', 'expired'].includes(key)) return 'tone-danger';
  if (['cancelled', 'archived'].includes(key)) return 'tone-muted';
  return 'tone-muted';
}

function trustTone(value) {
  const key = String(value || '').toLowerCase();
  if (['private', 'system', 'internal'].includes(key)) return 'tone-ok';
  if (['group', 'memory'].includes(key)) return 'tone-info';
  if (['external', 'skill'].includes(key)) return 'tone-warn';
  return 'tone-muted';
}

function moodTone(value) {
  const key = String(value || '').toLowerCase();
  if (['happy', 'energetic', 'excited', 'playful'].includes(key)) return 'tone-ok';
  if (['tired', 'sleepy', 'sad', 'grumpy', 'lonely'].includes(key)) return 'tone-warn';
  if (['shy', 'curious', 'calm', 'neutral'].includes(key)) return 'tone-info';
  return 'tone-muted';
}

function riskInfo(value) {
  if (typeof value === 'string') {
    const key = value.toLowerCase();
    if (['critical', 'high', '高风险'].includes(key)) return { label: '高', tone: 'tone-danger', detail: value };
    if (['medium', 'warn', '中风险'].includes(key)) return { label: '中', tone: 'tone-warn', detail: value };
    if (['low', '低风险'].includes(key)) return { label: '低', tone: 'tone-info', detail: value };
    if (['none', '无'].includes(key)) return { label: '无', tone: 'tone-ok', detail: value };
    return { label: value, tone: 'tone-muted', detail: value };
  }
  const number = asNumber(value);
  if (number == null) return { label: '—', tone: 'tone-muted', detail: '—' };
  if (number >= 0.85) return { label: '高', tone: 'tone-danger', detail: number.toFixed(2) };
  if (number >= 0.5) return { label: '中', tone: 'tone-warn', detail: number.toFixed(2) };
  if (number > 0) return { label: '低', tone: 'tone-info', detail: number.toFixed(2) };
  return { label: '无', tone: 'tone-ok', detail: '0' };
}

/* ============================================================================
   4. API
   ========================================================================== */

function cleanServerMessage(message) {
  const text = String(message == null ? '' : message).replace(/\s+/g, ' ').trim();
  if (!text) return '';
  if (/Traceback|File\s+"|\bat\s+[\w.]+\s+\(|Exception:|<html/i.test(text)) return '';
  return truncate(text, 180);
}

function friendlyError(error) {
  if (!error) return '发生了一个未知错误。';
  if (error.code === 'aborted') return '请求已取消。';
  if (error.code === 'timeout') return '请求超时了，请稍后再试。';
  if (error.code === 'network') return '连接不上后端。请确认服务已经启动，再重试。';
  if (error.status === 401) return '需要访问令牌，请重新输入。';
  if (error.status === 403) return '请求被本地安全策略拒绝了。';
  if (error.status === 404) return '接口不存在，前端和后端可能不是同一个版本。';
  if (error.status === 429) return '请求太频繁了，歇一小会儿再试。';
  if (error.status >= 500) {
    const serverMessage = cleanServerMessage(error.message);
    return serverMessage ? '服务器出错了：' + serverMessage : '服务器出错了，请稍后重试。';
  }
  const message = cleanServerMessage(error.message);
  if (message) return message;
  if (error.status) return '请求失败（HTTP ' + error.status + '）。';
  return '请求失败了，请稍后重试。';
}

async function fetchJSON(url, options) {
  const opts = options || {};
  const method = String(opts.method || 'GET').toUpperCase();
  const timeout = Number(opts.timeout) || 12000;
  const controller = new AbortController();
  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);

  const externalSignal = opts.signal;
  const onAbort = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener('abort', onAbort, { once: true });
  }

  const headers = { Accept: 'application/json' };
  if (app.token) headers.Authorization = 'Bearer ' + app.token;
  let payload;
  if (method === 'POST' || opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    if (method === 'POST') headers['X-Fatfish-Request'] = '1';
    payload = JSON.stringify(opts.body === undefined ? {} : opts.body);
  }

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: payload,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
    });
  } catch (error) {
    if (externalSignal && externalSignal.aborted) {
      throw { status: 0, code: 'aborted', message: '请求已取消。' };
    }
    if (timedOut) {
      throw { status: 0, code: 'timeout', message: '请求超时。' };
    }
    throw { status: 0, code: 'network', message: '无法连接后端。' };
  } finally {
    window.clearTimeout(timer);
    if (externalSignal) externalSignal.removeEventListener('abort', onAbort);
  }

  let raw = '';
  try {
    raw = await response.text();
  } catch (_err) {
    raw = '';
  }
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch (_err) {
      data = null;
    }
  }

  if (response.status === 401) {
    stopPoll();
    showLogin();
    throw { status: 401, code: 'unauthorized', message: '需要访问令牌。' };
  }

  if (!response.ok) {
    const serverMessage = data && (data.message || (data.error && data.error.message));
    const code = (data && (data.code || (data.error && data.error.code))) || ('http_' + response.status);
    throw {
      status: response.status,
      code: String(code),
      message: cleanServerMessage(serverMessage) || ('HTTP ' + response.status),
    };
  }

  if (data == null) {
    throw { status: response.status, code: 'bad_json', message: '服务器返回了无法解析的数据。' };
  }
  if (data.ok !== true) {
    const serverMessage = data.message || (data.error && data.error.message);
    const code = data.code || (data.error && data.error.code) || 'not_ok';
    throw {
      status: response.status,
      code: String(code),
      message: cleanServerMessage(serverMessage) || '服务器没有返回 ok:true。',
    };
  }
  return data;
}

/* ============================================================================
   5. 登录 / Toast / 全局在线状态
   ========================================================================== */

function showLogin(message) {
  if (!overlayEl) return;
  const wasHidden = overlayEl.hidden;
  if (wasHidden) {
    lastFocusedEl = document.activeElement;
    overlayEl.hidden = false;
    document.body.classList.add('modal-open');
    loginErrorEl.textContent = '';
  }
  if (message) loginErrorEl.textContent = message;
  if (!wasHidden) return;
  window.setTimeout(() => {
    if (tokenInputEl) {
      tokenInputEl.focus();
      tokenInputEl.select();
    }
  }, 30);
}

function hideLogin() {
  if (!overlayEl) return;
  overlayEl.hidden = true;
  document.body.classList.remove('modal-open');
  if (lastFocusedEl && lastFocusedEl.isConnected) {
    try {
      lastFocusedEl.focus({ preventScroll: true });
    } catch (_err) {
      /* ignore */
    }
  }
}

async function handleLoginSubmit(event) {
  event.preventDefault();
  const token = tokenInputEl.value.trim();
  if (!token) {
    loginErrorEl.textContent = '请输入访问令牌。';
    tokenInputEl.focus();
    return;
  }
  app.token = token;
  writeStorage(TOKEN_KEY, token);
  loginErrorEl.textContent = '';
  setButtonBusy(loginSubmitEl, true, '验证中…');
  try {
    await fetchJSON('/api/health', { timeout: 12000 });
    hideLogin();
    toast('欢迎回来，海面很安静。', 'success');
    routeView(true);
  } catch (error) {
    if (error.status === 401) {
      loginErrorEl.textContent = '令牌不正确，请检查后重试。';
    } else {
      loginErrorEl.textContent = '连接失败：' + friendlyError(error);
    }
    tokenInputEl.focus();
    tokenInputEl.select();
  } finally {
    setButtonBusy(loginSubmitEl, false);
  }
}

function toast(message, tone) {
  if (!toastHostEl) return;
  toastSeq += 1;
  const id = toastSeq;
  const kind = tone || 'info';
  const node = el('div', { class: 'toast toast-' + kind, id: 'toast-' + id });
  node.append(el('div', { class: 'toast-message', text: String(message) }));
  const close = el('button', {
    class: 'toast-close',
    type: 'button',
    'aria-label': '关闭提示',
    text: '×',
    onClick: () => removeToast(node),
  });
  node.append(close);
  toastHostEl.append(node);
  while (toastHostEl.children.length > 4) {
    toastHostEl.firstElementChild.remove();
  }
  const timeout = kind === 'error' ? 6500 : 4200;
  node._timeout = window.setTimeout(() => removeToast(node), timeout);
}

function removeToast(node) {
  if (!node || !node.isConnected) return;
  if (node._timeout) window.clearTimeout(node._timeout);
  node.classList.add('is-leaving');
  window.setTimeout(() => node.remove(), 180);
}

function setGlobalOnline(online) {
  app.online = online;
  const dot = document.querySelector('.sidebar .status-dot, .sidebar-footer .status-dot');
  if (dot) {
    dot.classList.toggle('is-off', online === false);
    dot.classList.toggle('is-wait', online === null);
  }
  if (liveDotEl) {
    liveDotEl.classList.toggle('is-off', online === false);
    liveDotEl.classList.toggle('is-wait', online === null);
  }
}

let sidebarFetchedAt = 0;

function updateSidebarLive(payload) {
  if (!liveStateEl) return;
  const state = payload && typeof payload === 'object' ? payload : {};
  const snapshot = state.state && typeof state.state === 'object' ? state.state : {};
  const life = state.life && typeof state.life === 'object' ? state.life : {};
  const activity = String(life.current_activity || '').trim();
  const passive = snapshot.state === 'idle' || snapshot.state === 'sleeping';
  const realActivity = activity !== '' && activity !== 'idle' && activity !== 'sleeping' && activity !== 'chatting';
  const useActivity = passive && realActivity;
  const stateText = useActivity ? activityLabel(activity) : stateLabel(snapshot.state);
  const note = useActivity ? activityNote(life.note) : '';
  const activityLabelText = activityLabel(activity);
  let activityText;
  if (note) {
    activityText = note;
  } else if (snapshot.state === 'sleeping') {
    activityText = '把自己裹进浪里';
  } else if (snapshot.state === 'chatting') {
    activityText = '在听你说话';
  } else if (snapshot.state === 'generating') {
    activityText = '在认真想事情';
  } else if (snapshot.state === 'browsing') {
    activityText = '在远处逛来逛去';
  } else if (activity && activity !== 'idle' && activity !== 'sleeping' && activity !== 'chatting') {
    activityText = '在' + activityLabelText + '，没走远';
  } else if (snapshot.state === 'idle') {
    activityText = '趴在珊瑚上晃尾巴';
  } else {
    activityText = '在深海里发呆';
  }
  liveStateEl.textContent = stateText === '—' ? '在深海里发呆' : stateText;
  liveActivityEl.textContent = activityText;
  liveMoodEl.textContent = '心情 · ' + moodLabel(life.mood);
  if (liveTimeEl) liveTimeEl.textContent = '刚刚';
}

function setSidebarOffline() {
  if (!liveStateEl) return;
  liveStateEl.textContent = '不在海边';
  liveActivityEl.textContent = '连接不上本地服务';
  liveMoodEl.textContent = '心情 · —';
}

async function refreshSidebarState(force) {
  if (document.hidden) return;
  if (!force && Date.now() - sidebarFetchedAt < 20000) return;
  sidebarFetchedAt = Date.now();
  try {
    const state = await fetchJSON('/api/state', { timeout: 10000 });
    updateSidebarLive(state);
    setGlobalOnline(true);
  } catch (error) {
    if (!error || error.code === 'aborted' || error.status === 401) return;
    setSidebarOffline();
    setGlobalOnline(false);
  }
}

/* ============================================================================
   6. 轮询
   ========================================================================== */

function stopPoll() {
  if (app.poll.timer) {
    window.clearTimeout(app.poll.timer);
    app.poll.timer = null;
  }
  app.poll.fn = null;
  app.poll.ms = 0;
}

function schedulePoll(delay) {
  if (!app.poll.fn || document.hidden) return;
  if (app.poll.timer) window.clearTimeout(app.poll.timer);
  app.poll.timer = window.setTimeout(async () => {
    app.poll.timer = null;
    const fn = app.poll.fn;
    if (!fn || document.hidden) return;
    if (app.chatPending) {
      schedulePoll(app.poll.ms);
      return;
    }
    try {
      await fn();
    } catch (error) {
      if (!error || error.code !== 'aborted') {
        /* 视图内部会展示自己的错误状态，这里保持安静 */
      }
    }
    if (app.poll.fn === fn) schedulePoll(app.poll.ms);
  }, delay);
}

function startPoll(fn, ms) {
  stopPoll();
  app.poll.fn = fn;
  app.poll.ms = ms;
  schedulePoll(ms);
}

/* ============================================================================
   7. 路由
   ========================================================================== */

const RENDERERS = {
  chat: renderChatView,
  overview: renderOverviewView,
  memory: renderMemoryView,
  people: renderPeopleView,
  lexicon: renderLexiconView,
  qzone: renderQzoneView,
  traces: renderTracesView,
  services: renderServicesView,
  about: renderAboutView,
};

function currentRouteName() {
  const raw = String(window.location.hash || '').replace(/^#\/?/, '');
  return ROUTES.includes(raw) ? raw : 'chat';
}

function normalizeHash() {
  if (!window.location.hash) {
    history.replaceState(null, '', '#/chat');
    return;
  }
  const route = currentRouteName();
  if (window.location.hash !== '#/' + route) {
    history.replaceState(null, '', '#/' + route);
  }
}

function setActiveNav(route) {
  document.querySelectorAll('.nav-item').forEach((link) => {
    const active = link.dataset.route === route;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

function addCleanup(fn) {
  if (typeof fn === 'function') app.cleanupFns.push(fn);
}

function teardownView() {
  stopPoll();
  for (const fn of app.cleanupFns) {
    try {
      fn();
    } catch (_err) {
      /* ignore cleanup errors */
    }
  }
  app.cleanupFns = [];
  if (app.viewAbort) {
    try {
      app.viewAbort.abort();
    } catch (_err) {
      /* ignore */
    }
  }
  app.viewAbort = null;
  app.chat.refs = null;
  app.overview.refs = null;
  app.memory.refs = null;
  app.people.refs = null;
  app.lexicon.refs = null;
  app.qzone.refs = null;
  app.traces.refs = null;
  app.services.refs = null;
  app.about.refs = null;
}

function routeView(force) {
  normalizeHash();
  const next = currentRouteName();
  if (!force && next === app.route && RENDERERS[next]) return;

  teardownView();
  app.route = next;
  setActiveNav(next);
  document.title = '蓝色大肥鱼 · ' + ROUTE_TITLES[next];

  viewEl.className = 'view view--' + next;
  viewEl.replaceChildren();
  viewEl.scrollTop = 0;
  app.viewAbort = new AbortController();

  const renderer = RENDERERS[next];
  if (renderer) renderer(viewEl);
  refreshSidebarState(false);
  try {
    viewEl.focus({ preventScroll: true });
  } catch (_err) {
    viewEl.focus();
  }
}

/* ============================================================================
   8. 聊天视图
   ========================================================================== */

function renderChatView(container) {
  const refs = {
    list: null,
    input: null,
    send: null,
    counter: null,
    form: null,
    typing: null,
    onlineWrap: null,
    onlineText: null,
    modeText: null,
    sessionInput: null,
    groupButtons: [],
    clearButton: null,
  };
  app.chat.refs = refs;
  app.chat.entries = loadTranscript(app.sessionId);

  const onlineWrap = el('span', { class: 'online-wrap is-wait' },
    el('span', { class: 'online-dot', 'aria-hidden': 'true' }),
    el('span', { text: '连接中' }),
  );
  refs.onlineWrap = onlineWrap;
  refs.onlineText = onlineWrap.lastElementChild;

  const modeText = el('span', { text: app.isGroup ? '群聊模式' : '私聊模式' });
  refs.modeText = modeText;

  const identity = el('div', { class: 'chat-identity' },
    el('span', { class: 'chat-avatar', 'aria-hidden': 'true' }, whaleSvg('')),
    el('div', { class: 'chat-title-wrap' },
      el('h1', { class: 'chat-title', text: '蓝色大肥鱼' }),
      el('p', { class: 'chat-sub' },
        onlineWrap,
        el('span', { 'aria-hidden': 'true', text: '·' }),
        modeText,
        el('span', { 'aria-hidden': 'true', text: '·' }),
        el('span', { text: '只在这台电脑里' }),
      ),
    ),
  );

  const sessionInput = el('input', {
    class: 'session-input',
    type: 'text',
    id: 'chat-session-input',
    maxlength: '64',
    spellcheck: 'false',
    autocomplete: 'off',
    value: app.sessionId,
    'aria-label': '会话编号',
  });
  refs.sessionInput = sessionInput;
  sessionInput.addEventListener('change', switchChatSession);
  sessionInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      sessionInput.blur();
    }
  });

  const sessionField = el('label', { class: 'session-field', for: 'chat-session-input' },
    el('span', { class: 'field-label', text: '会话' }),
    sessionInput,
  );

  const privateBtn = el('button', {
    class: 'seg-btn',
    type: 'button',
    text: '私聊',
    'aria-pressed': String(!app.isGroup),
    onClick: () => setChatGroup(false),
  });
  const groupBtn = el('button', {
    class: 'seg-btn',
    type: 'button',
    text: '群聊',
    'aria-pressed': String(app.isGroup),
    onClick: () => setChatGroup(true),
  });
  refs.groupButtons = [privateBtn, groupBtn];

  const clearButton = el('button', {
    class: 'btn btn-quiet btn-sm clear-chat',
    type: 'button',
    'aria-label': '清空本地视图',
    onClick: clearChatView,
  },
  iconSvg('icon-close'),
  el('span', { text: '清空本地视图' }),
  );
  refs.clearButton = clearButton;

  const tools = el('div', { class: 'chat-tools' },
    sessionField,
    el('div', { class: 'segmented', role: 'group', 'aria-label': '聊天模式' }, privateBtn, groupBtn),
    clearButton,
  );

  const head = el('header', { class: 'chat-head' }, identity, tools);
  const list = el('div', {
    class: 'chat-list',
    id: 'chat-list',
    role: 'log',
    'aria-live': 'polite',
    'aria-relevant': 'additions text',
    'aria-label': '聊天消息',
  });
  refs.list = list;

  const input = el('textarea', {
    class: 'composer-input',
    id: 'chat-input',
    rows: '1',
    maxlength: String(MAX_MESSAGE),
    placeholder: '和她说点什么…',
    'aria-label': '给蓝色大肥鱼发消息',
  });
  refs.input = input;

  const counter = el('span', { class: 'counter', text: '0 / ' + MAX_MESSAGE });
  refs.counter = counter;
  const send = el('button', {
    class: 'btn btn-primary send-btn',
    type: 'submit',
    text: '发送',
    disabled: true,
  });
  refs.send = send;

  const form = el('form', {
    class: 'composer',
    id: 'chat-form',
    onSubmit: handleChatSubmit,
  },
  el('div', { class: 'composer-row' }, input, send),
  el('div', { class: 'composer-foot' },
    el('span', { class: 'composer-hint', text: 'Enter 发送 · Shift+Enter 换行 · 她只在这台电脑里' }),
    el('div', { class: 'composer-right' }, counter),
  ),
  );
  refs.form = form;

  form.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
    if (event.shiftKey) {
      // 显式插入换行：不依赖浏览器对 Shift+Enter 的默认编辑行为。
      event.preventDefault();
      const start = input.selectionStart == null ? input.value.length : input.selectionStart;
      const end = input.selectionEnd == null ? start : input.selectionEnd;
      input.setRangeText('\n', start, end, 'end');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return;
    }
    event.preventDefault();
    form.requestSubmit();
  });

  input.addEventListener('input', () => {
    resizeComposer();
    updateComposer();
  });

  const section = el('section', { class: 'chat-view' }, head, list, form);
  container.append(section);

  renderAllChat(false);
  updateComposer();
  if (app.chatPending) showTyping();
  checkChatConnection();
}

function isChatActive() {
  return app.route === 'chat' && app.chat.refs && app.chat.refs.list && app.chat.refs.list.isConnected;
}

function switchChatSession() {
  const refs = app.chat.refs;
  if (!refs || app.chatPending) return;
  const next = normalizeSessionId(refs.sessionInput.value);
  if (next === app.sessionId) {
    refs.sessionInput.value = app.sessionId;
    return;
  }
  saveCurrentTranscript();
  app.sessionId = next;
  writeStorage(SESSION_KEY, next);
  app.chat.entries = loadTranscript(next);
  refs.sessionInput.value = next;
  renderAllChat(false);
  updateComposer();
  toast('已切换到会话 ' + next, 'info');
}

function setChatGroup(isGroup) {
  if (app.chatPending) return;
  app.isGroup = Boolean(isGroup);
  writeStorage(GROUP_KEY, app.isGroup ? '1' : '0');
  const refs = app.chat.refs;
  if (refs) {
    refs.groupButtons.forEach((button, index) => {
      button.setAttribute('aria-pressed', String(index === 1 ? app.isGroup : !app.isGroup));
    });
    if (refs.modeText) refs.modeText.textContent = app.isGroup ? '群聊模式' : '私聊模式';
  }
}

function clearChatView() {
  if (app.chatPending) return;
  if (!app.chat.entries.length) {
    toast('这个会话还没有聊天记录。', 'info');
    return;
  }
  app.chat.entries = [];
  saveCurrentTranscript();
  const refs = app.chat.refs;
  if (refs) {
    refs.list.replaceChildren();
    renderAllChat(false);
  }
  toast('已清空本会话的本地视图。', 'success');
}

function handleChatSubmit(event) {
  event.preventDefault();
  if (app.chatPending) return;
  const refs = app.chat.refs;
  if (!refs) return;
  const text = refs.input.value.trim();
  if (!text) return;
  if (text.length > MAX_MESSAGE) {
    toast('消息超过 2000 个字符了，先删减一点吧。', 'error');
    return;
  }
  refs.input.value = '';
  resizeComposer();
  updateComposer();
  sendChatMessage(text);
}

async function sendChatMessage(text) {
  if (app.chatPending || !text) return;
  const sessionAtSend = app.sessionId;
  const userEntry = { id: uid('m'), role: 'user', text, at: Date.now() };
  const userPrevious = previousChatEntry();
  const userGrouped = isGroupedWith(userEntry, userPrevious);
  const userDayChanged = !userPrevious || dayKey(userPrevious.at) !== dayKey(userEntry.at);
  app.chat.entries.push(userEntry);
  saveCurrentTranscript();

  app.chatPending = true;
  const refs = app.chat.refs;
  if (refs && isChatActive()) {
    removeChatEmptyState();
    if (userDayChanged) refs.list.append(buildDayDivider(userEntry.at));
    refs.list.append(buildChatEntryNode(userEntry, true, userGrouped));
    scrollChatToBottom(true);
  }
  setChatPendingUi(true);
  showTyping();
  stopChatAudio();

  try {
    const streamedUrls = [];
    const result = await fetchChatStream({
      message: text,
      session_id: sessionAtSend,
      is_group: app.isGroup,
    }, (segment) => {
      if (!segment || !segment.ok || !segment.audio_url) return;
      streamedUrls.push(segment.audio_url);
      queueChatAudio(segment.audio_url);
    });
    const messages = extractReplyMessages(result);
    const meta = normalizeMeta(metaFromResult(result));
    if (streamedUrls.length && meta.tts) {
      meta.tts.audio_urls = streamedUrls.slice(0, 32);
      meta.tts.audio_url = streamedUrls[0];
    }
    const herEntry = { id: uid('m'), role: 'her', messages, meta, at: Date.now() };
    appendChatEntryFor(sessionAtSend, herEntry, true);
  } catch (error) {
    if (error && error.code === 'aborted') return;
    if (error && (error.code === 'network' || error.code === 'timeout')) {
      setChatOnline(false);
    }
    const message = '发送失败：' + friendlyError(error);
    const errorEntry = {
      id: uid('m'),
      role: 'error',
      text: message,
      retryText: text,
      userId: userEntry.id,
      at: Date.now(),
    };
    appendChatEntryFor(sessionAtSend, errorEntry, true);
    if (isChatActive() && refs && !refs.input.value.trim()) {
      refs.input.value = text;
      resizeComposer();
      updateComposer();
    }
  } finally {
    app.chatPending = false;
    setChatPendingUi(false);
    hideTyping();
    if (isChatActive()) {
      updateComposer();
      scrollChatToBottom(false);
    }
    refreshSidebarState(true);
  }
}

function chatAudioState() {
  if (!app.chatAudio) {
    app.chatAudio = { queue: [], playing: false, audio: null };
  }
  return app.chatAudio;
}

function stopChatAudio() {
  const state = chatAudioState();
  state.queue = [];
  state.playing = false;
  if (state.audio) {
    try { state.audio.pause(); } catch (_err) { /* ignore */ }
    state.audio = null;
  }
}

function playNextChatAudio() {
  const state = chatAudioState();
  const url = state.queue.shift();
  if (!url) {
    state.playing = false;
    state.audio = null;
    return;
  }
  state.playing = true;
  const audio = new Audio(url);
  audio.preload = 'auto';
  state.audio = audio;
  audio.addEventListener('ended', () => playNextChatAudio(), { once: true });
  audio.play().catch(() => {
    // Autoplay can be blocked; the rendered players below remain available.
    state.playing = false;
  });
}

function queueChatAudio(url) {
  if (!url) return;
  const state = chatAudioState();
  state.queue.push(url);
  if (!state.playing) playNextChatAudio();
}

async function fetchChatStream(body, onSegment) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 240000);
  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Fatfish-Request': '1' },
      body: JSON.stringify(body),
      signal: controller.signal,
      credentials: 'same-origin',
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      const error = new Error('HTTP ' + response.status + ' ' + detail.slice(0, 120));
      error.code = response.status === 401 ? 'unauthorized' : 'network';
      throw error;
    }
    if (!response.body) {
      const error = new Error('stream unsupported');
      error.code = 'network';
      throw error;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let result = null;
    let failure = null;
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, cut).trim();
        buffer = buffer.slice(cut + 1);
        if (!line) continue;
        let event;
        try { event = JSON.parse(line); } catch (_err) { continue; }
        if (event.type === 'segment') onSegment(event);
        else if (event.type === 'done') result = event.result || {};
        else if (event.type === 'error') failure = event.error || 'stream_error';
      }
    }
    if (buffer.trim()) {
      try {
        const event = JSON.parse(buffer);
        if (event.type === 'done') result = event.result || {};
        else if (event.type === 'error') failure = event.error || 'stream_error';
      } catch (_err) { /* ignore trailing garbage */ }
    }
    if (failure) {
      const error = new Error(failure);
      error.code = 'network';
      throw error;
    }
    if (!result) {
      const error = new Error('stream_incomplete');
      error.code = 'network';
      throw error;
    }
    return result;
  } catch (error) {
    if (error && error.name === 'AbortError') {
      const timeout = new Error('timeout');
      timeout.code = 'timeout';
      throw timeout;
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function extractReplyMessages(result) {
  let messages = [];
  if (Array.isArray(result.messages)) {
    messages = result.messages.filter((item) => typeof item === 'string' && item.trim());
  }
  if (!messages.length && typeof result.reply === 'string' && result.reply.trim()) {
    messages = [result.reply];
  }
  return messages.slice(0, 12).map((item) => item.trim().slice(0, 10000));
}

function stickerCaptionOf(sticker) {
  if (!sticker) return '';
  if (typeof sticker === 'string') return sticker.trim().slice(0, 300);
  if (typeof sticker === 'object') {
    const keys = ['caption', 'description', 'name', 'text', 'alt'];
    for (const key of keys) {
      if (typeof sticker[key] === 'string' && sticker[key].trim()) {
        return sticker[key].trim().slice(0, 300);
      }
    }
  }
  return '';
}

function metaFromResult(result) {
  const voice = result.voice && typeof result.voice === 'object' ? result.voice : null;
  const security = result.security && typeof result.security === 'object' ? result.security : null;
  const tts = result.tts && typeof result.tts === 'object' ? result.tts : null;
  const sing = result.sing && typeof result.sing === 'object' ? result.sing : null;
  return {
    voice,
    security,
    actions: Array.isArray(result.actions) ? result.actions : [],
    tts,
    sing,
    stickerCaption: stickerCaptionOf(result.sticker),
  };
}

function appendChatEntryFor(sessionId, entry, animate) {
  if (sessionId === app.sessionId) {
    const previous = previousChatEntry();
    const grouped = isGroupedWith(entry, previous);
    const dayChanged = Boolean(previous) && dayKey(previous.at) !== dayKey(entry.at);
    app.chat.entries.push(entry);
    saveCurrentTranscript();
    if (isChatActive()) {
      removeChatEmptyState();
      if (dayChanged) app.chat.refs.list.append(buildDayDivider(entry.at));
      const node = buildChatEntryNode(entry, animate, grouped);
      app.chat.refs.list.append(node);
      scrollChatToBottom(entry.role === 'user');
    }
    return;
  }
  const otherEntries = loadTranscript(sessionId);
  otherEntries.push(entry);
  saveTranscriptFor(sessionId, otherEntries);
}

function renderAllChat(animate) {
  const refs = app.chat.refs;
  if (!refs) return;
  const list = refs.list;
  list.replaceChildren();
  refs.typing = null;
  if (!app.chat.entries.length) {
    list.append(buildChatEmptyState());
  } else {
    let previous = null;
    let previousDay = '';
    for (const entry of app.chat.entries) {
      const day = dayKey(entry.at);
      if (day !== previousDay) {
        list.append(buildDayDivider(entry.at));
        previousDay = day;
      }
      list.append(buildChatEntryNode(entry, animate, isGroupedWith(entry, previous)));
      previous = entry;
    }
  }
  if (app.chatPending) showTyping();
  scrollChatToBottom(true);
}

function buildDayDivider(at) {
  return el('div', { class: 'day-divider', role: 'separator', 'aria-label': dayDividerLabel(at) },
    el('span', { text: dayDividerLabel(at) }),
  );
}

function greetingKicker() {
  const hour = new Date().getHours();
  if (hour < 6) return '深夜的海面很安静';
  if (hour < 11) return '早上的海面亮起来了';
  if (hour < 14) return '午后的海水暖暖的';
  if (hour < 18) return '傍晚的浪很轻';
  return '今晚的海面很安静';
}

function buildChatEmptyState() {
  const scene = el('div', { class: 'empty-scene', 'aria-hidden': 'true' },
    el('span', { class: 'scene-ring' }),
    el('span', { class: 'empty-whale' }, whaleSvg('')),
    el('span', { class: 'scene-bubble b1' }),
    el('span', { class: 'scene-bubble b2' }),
    el('span', { class: 'scene-bubble b3' }),
  );
  const wrap = el('div', { class: 'empty-chat' },
    scene,
    el('p', { class: 'empty-kicker', text: greetingKicker() }),
    el('h2', { class: 'empty-title', text: '你终于来啦。' }),
    el('p', { class: 'empty-text', text: '我还以为你今天把我忘了呢……哼，来了就好。随便说点什么吧，我听着。' }),
  );
  const row = el('div', { class: 'suggestion-row', role: 'group', 'aria-label': '想聊的话题' });
  const suggestions = [
    { title: '说说今天', text: '开心或烦心，都讲给她听', prompt: '今天发生了一件事，想和你说。' },
    { title: '陪我坐一会儿', text: '不说话也可以', prompt: '陪我聊会儿吧，今天有点安静。' },
    { title: '你还记得吗', text: '翻翻她收好的记忆', prompt: '你还记得我上次说的事吗？' },
  ];
  suggestions.forEach((item, index) => {
    row.append(el('button', {
      class: 'suggestion-card',
      type: 'button',
      'aria-label': item.title + '：' + item.text,
      onClick: () => {
        const refs = app.chat.refs;
        if (!refs) return;
        refs.input.value = item.prompt;
        resizeComposer();
        updateComposer();
        refs.input.focus();
        refs.input.setSelectionRange(item.prompt.length, item.prompt.length);
      },
    },
    el('span', { class: 'suggestion-index', text: pad2(index + 1) }),
    el('span', { class: 'suggestion-title', text: item.title }),
    el('span', { class: 'suggestion-text', text: item.text }),
    iconSvg('icon-arrow', 'suggestion-arrow'),
    ));
  });
  wrap.append(row);
  return wrap;
}

function removeChatEmptyState() {
  const refs = app.chat.refs;
  if (!refs) return;
  const empty = refs.list.querySelector('.empty-chat');
  if (empty) empty.remove();
}

function chatTimeNode(at) {
  const date = toDate(at) || new Date();
  let iso = '';
  try {
    iso = date.toISOString();
  } catch (_err) {
    iso = '';
  }
  return el('time', {
    class: 'msg-time',
    datetime: iso,
    text: fmtTime(date.getTime()),
  });
}

function buildChatEntryNode(entry, animate, grouped) {
  const groupedClass = grouped ? ' is-grouped' : '';
  if (entry.role === 'user') {
    const row = el('div', {
      class: 'msg-row msg-user' + (animate ? '' : ' is-history') + groupedClass,
      dataset: { entryId: entry.id },
    });
    const stack = el('div', { class: 'msg-stack' });
    const bubble = el('div', { class: 'bubble bubble-user', text: entry.text });
    if (animate) bubble.classList.add('is-new');
    stack.append(bubble, chatTimeNode(entry.at));
    row.append(stack);
    return row;
  }

  if (entry.role === 'error') {
    const row = el('div', {
      class: 'msg-row msg-her' + (animate ? '' : ' is-history'),
      dataset: { entryId: entry.id },
    });
    const avatar = el('span', { class: 'msg-avatar', 'aria-hidden': 'true', text: '!' });
    const stack = el('div', { class: 'msg-stack' });
    const bubble = el('div', { class: 'bubble bubble-error' });
    const inline = el('div', { class: 'error-inline' });
    inline.append(el('span', { text: entry.text }));
    const retry = el('button', {
      class: 'retry-button',
      type: 'button',
      text: '重试',
      onClick: () => retryChatEntry(entry.id),
    });
    inline.append(retry);
    bubble.append(inline);
    stack.append(bubble, chatTimeNode(entry.at));
    row.append(avatar, stack);
    return row;
  }

  const row = el('div', {
    class: 'msg-row msg-her' + (animate ? '' : ' is-history') + groupedClass,
    dataset: { entryId: entry.id },
  });
  const avatar = el('span', { class: 'msg-avatar', 'aria-hidden': 'true' }, whaleSvg(''));
  const stack = el('div', { class: 'msg-stack' });

  entry.messages.forEach((message, index) => {
    const bubble = el('div', { class: 'bubble bubble-her', text: message });
    if (animate) {
      bubble.classList.add('is-new');
      bubble.style.setProperty('--i', String(index));
    }
    stack.append(bubble);
  });

  const meta = entry.meta || {};
  if (entry.messages.length || hasMetaContent(meta)) {
    stack.append(buildChatMeta(meta));
  }
  stack.append(chatTimeNode(entry.at));
  row.append(avatar, stack);
  return row;
}

function hasMetaContent(meta) {
  return Boolean(
    (meta.voice && meta.voice.mode) ||
    (meta.security && (meta.security.trust || meta.security.risk != null || meta.security.blocked)) ||
    (Array.isArray(meta.actions) && meta.actions.length) ||
    (meta.tts && (meta.tts.audio_url || meta.tts.error)) ||
    (meta.sing && (meta.sing.audio_url || meta.sing.error)) ||
    meta.stickerCaption
  );
}

function buildChatMeta(meta) {
  const wrap = el('div', { class: 'msg-meta' });
  const metaRow = el('div', { class: 'meta-row' });

  if (meta.voice && meta.voice.mode) {
    const score = asNumber(meta.voice.score);
    let label = '语音 · ' + voiceLabel(meta.voice.mode);
    if (score != null) label += ' · ' + Math.round(fractionPct(score)) + '%';
    const voiceNode = badge(label, 'tone-info');
    if (Array.isArray(meta.voice.reasons) && meta.voice.reasons.length) {
      voiceNode.title = meta.voice.reasons.join('；');
    }
    metaRow.append(voiceNode);
  }

  if (meta.security && meta.security.trust) {
    metaRow.append(badge('信任 · ' + trustLabel(meta.security.trust), trustTone(meta.security.trust)));
  }

  if (meta.security && meta.security.risk != null) {
    const risk = riskInfo(meta.security.risk);
    const riskNode = badge('风险 · ' + risk.label, risk.tone);
    riskNode.title = '风险值 ' + risk.detail;
    metaRow.append(riskNode);
  }

  if (meta.security && meta.security.blocked) {
    metaRow.append(badge('已拦截', 'tone-danger'));
  }

  if (meta.sing && meta.sing.audio_url) {
    metaRow.append(badge('唱歌 · ' + singEmotionLabel(meta.sing.emotion), 'tone-info'));
  } else if (meta.sing && meta.sing.error) {
    metaRow.append(badge('没唱成', 'tone-warn'));
  }

  if (meta.tts && meta.tts.error && !meta.tts.ok && meta.tts.error !== 'disabled') {
    metaRow.append(badge('语音未生成', 'tone-warn'));
  }

  if (metaRow.children.length) wrap.append(metaRow);

  if (Array.isArray(meta.actions) && meta.actions.length) {
    const actionRow = el('div', { class: 'meta-row action-row', 'aria-label': '本轮动作' });
    for (const action of meta.actions) {
      const label = action.ok ? action.tool : action.tool + ' · 拒绝';
      const node = chip(label, action.ok ? '' : 'chip-danger');
      if (!action.ok && action.error) node.title = action.error;
      actionRow.append(node);
    }
    wrap.append(actionRow);
  }

  if (meta.stickerCaption) {
    wrap.append(el('p', { class: 'sticker-caption', text: '表情：' + meta.stickerCaption }));
  }

  const audioUrls = meta.tts && Array.isArray(meta.tts.audio_urls) && meta.tts.audio_urls.length
    ? meta.tts.audio_urls.slice()
    : (meta.tts && meta.tts.audio_url ? [meta.tts.audio_url] : []);
  const singUrl = meta.sing && meta.sing.audio_url ? meta.sing.audio_url : '';
  if (singUrl) audioUrls.push(singUrl);
  if (audioUrls.length) {
    const list = el('div', { class: 'chat-audio-list' });
    audioUrls.forEach((url, index) => {
      if (url === singUrl) {
        list.append(el('span', { class: 'chat-audio-label', text: '唱给你的歌' }));
      } else if (audioUrls.length > 1) {
        list.append(el('span', { class: 'chat-audio-label', text: '第 ' + (index + 1) + ' 句' }));
      }
      list.append(el('audio', {
        class: 'chat-audio',
        controls: true,
        preload: 'none',
        src: url,
        'aria-label': url === singUrl ? '她唱给你的歌' : '她的语音回复' + (audioUrls.length > 1 ? '，第 ' + (index + 1) + ' 句' : ''),
      }));
    });
    wrap.append(list);
  }

  return wrap;
}

function retryChatEntry(entryId) {
  if (app.chatPending) return;
  const index = app.chat.entries.findIndex((entry) => entry.id === entryId);
  if (index < 0) return;
  const entry = app.chat.entries[index];
  if (!entry.retryText) return;
  app.chat.entries.splice(index, 1);
  if (entry.userId) {
    const userIndex = app.chat.entries.findIndex((item) => item.id === entry.userId);
    if (userIndex >= 0) app.chat.entries.splice(userIndex, 1);
  }
  saveCurrentTranscript();
  renderAllChat(false);
  updateComposer();
  const refs = app.chat.refs;
  if (refs && refs.input && refs.input.value.trim() === entry.retryText.trim()) {
    refs.input.value = '';
    resizeComposer();
    updateComposer();
  }
  sendChatMessage(entry.retryText);
}

function showTyping() {
  const refs = app.chat.refs;
  if (!refs || !isChatActive()) return;
  if (refs.typing && refs.typing.isConnected) return;
  const row = el('div', { class: 'typing-row', dataset: { typing: '1' } },
    el('span', { class: 'msg-avatar', 'aria-hidden': 'true' }, whaleSvg('')),
    el('div', { class: 'typing-bubble' },
      el('span', { text: '她正在想…' }),
      el('span', { class: 'typing-dots', 'aria-hidden': 'true' }, el('span'), el('span'), el('span')),
    ),
  );
  refs.typing = row;
  refs.list.append(row);
  refs.list.setAttribute('aria-busy', 'true');
  scrollChatToBottom(true);
}

function hideTyping() {
  const refs = app.chat.refs;
  if (!refs) return;
  if (refs.typing && refs.typing.isConnected) refs.typing.remove();
  refs.typing = null;
  if (refs.list) refs.list.setAttribute('aria-busy', 'false');
}

function setChatPendingUi(pending) {
  app.chatPending = pending;
  const refs = app.chat.refs;
  if (!refs) return;
  updateComposer();
  const disable = pending;
  if (refs.sessionInput) refs.sessionInput.disabled = disable;
  if (refs.clearButton) refs.clearButton.disabled = disable;
  refs.groupButtons.forEach((button) => { button.disabled = disable; });
}

function updateComposer() {
  const refs = app.chat.refs;
  if (!refs) return;
  const length = refs.input.value.length;
  refs.counter.textContent = length + ' / ' + MAX_MESSAGE;
  refs.counter.classList.toggle('is-near', length >= 1800 && length <= MAX_MESSAGE);
  refs.counter.classList.toggle('is-over', length > MAX_MESSAGE);
  const empty = !refs.input.value.trim();
  refs.send.disabled = app.chatPending || empty;
  refs.send.textContent = app.chatPending ? '发送中…' : '发送';
}

function resizeComposer() {
  const refs = app.chat.refs;
  if (!refs) return;
  const input = refs.input;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 176) + 'px';
}

function scrollChatToBottom(force) {
  const refs = app.chat.refs;
  if (!refs || !refs.list) return;
  const list = refs.list;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 150;
  if (force || nearBottom) {
    requestAnimationFrame(() => {
      if (refs.list && refs.list.isConnected) {
        refs.list.scrollTop = refs.list.scrollHeight;
      }
    });
  }
}

function checkChatConnection() {
  if (!app.viewAbort) return;
  const signal = app.viewAbort.signal;
  (async () => {
    try {
      const data = await fetchJSON('/api/health', { timeout: 10000, signal });
      if (app.route !== 'chat') return;
      setChatOnline(true, data.version);
    } catch (error) {
      if (!error || error.code === 'aborted' || app.route !== 'chat') return;
      setChatOnline(false);
    }
  })();
}

function setChatOnline(online, version) {
  setGlobalOnline(online);
  const refs = app.chat.refs;
  if (!refs) return;
  refs.onlineWrap.classList.remove('is-wait');
  refs.onlineWrap.classList.toggle('is-off', !online);
  refs.onlineText.textContent = online ? '在线' : '离线';
  if (online) {
    refs.onlineWrap.title = version ? '后端版本 ' + version : '后端已连接';
  } else {
    refs.onlineWrap.title = '没有连接到后端';
  }
}

/* ============================================================================
   9. 今日视图
   ========================================================================== */

function renderOverviewView(container) {
  const refs = {
    countValues: {},
    traceValues: {},
    healthValues: {},
    security: {},
    energyRing: null,
    energyValue: null,
    energyHint: null,
    social: {},
    wakeups: null,
    outbox: null,
    errorSlot: null,
    nightlySlot: null,
    updated: null,
    loadingEls: [],
    actionButtons: [],
  };
  app.overview.refs = refs;

  const live = (node) => {
    refs.loadingEls.push(node);
    return node;
  };

  const refreshButton = el('button', { class: 'btn btn-ghost', type: 'button', text: '刷新' });
  const tickButton = el('button', { class: 'btn btn-ghost', type: 'button', text: '推进一次日常' });
  const nightlyButton = el('button', { class: 'btn btn-ghost', type: 'button', text: '记忆压缩预演' });
  refs.actionButtons = [refreshButton, tickButton, nightlyButton];
  const updated = el('span', { class: 'updated-label', text: '尚未更新' });
  refs.updated = updated;

  const head = viewHeader(
    '今日',
    '她的今天',
    '她把今天过成什么样，心情、精力、做过的事，都写在这里。',
    [updated, refreshButton, tickButton, nightlyButton],
  );

  const errorSlot = el('div', { class: 'overview-error' });
  refs.errorSlot = errorSlot;

  const grid = el('div', { class: 'overview-grid' });
  grid.append(
    buildOverviewHero(refs, live),
    buildOverviewCounts(refs, live),
    buildOverviewTraceCard(refs, live),
    buildOverviewSecurity(refs, live),
    buildOverviewHealth(refs, live, container),
  );

  const nightlySlot = el('div', { class: 'nightly-panel' });
  refs.nightlySlot = nightlySlot;

  const page = el('div', { class: 'overview-page' }, head, errorSlot, grid, nightlySlot);
  container.append(page);

  refreshButton.addEventListener('click', () => refreshOverviewManual(refreshButton));
  tickButton.addEventListener('click', () => runTickAction(tickButton));
  nightlyButton.addEventListener('click', () => runNightlyAction(nightlyButton));

  if (app.overview.state) {
    updateOverviewUi();
    if (app.overview.health) updateOverviewHealth();
    refs.loadingEls.forEach((node) => node.classList.remove('is-loading-value'));
  } else {
    refs.loadingEls.forEach((node) => node.classList.add('is-loading-value'));
  }

  loadOverviewInitial();
}

function buildOverviewHero(refs, live) {
  const stateText = live(el('h2', { class: 'hero-state-text', text: '——' }));
  const moodBadge = live(badge('——', 'tone-info'));
  const sinceText = el('p', { class: 'hero-meta', text: '正在读取她的状态…' });
  const noteText = live(el('p', { class: 'hero-note', text: '——' }));
  refs.stateText = stateText;
  refs.moodBadge = moodBadge;
  refs.sinceText = sinceText;
  refs.noteText = noteText;

  const phaseValue = live(el('div', { class: 'life-value', text: '——' }));
  const activityValue = live(el('div', { class: 'life-value', text: '——' }));
  const planValue = live(el('div', { class: 'life-value', text: '——' }));
  refs.phaseValue = phaseValue;
  refs.activityValue = activityValue;
  refs.planValue = planValue;

  const heroMain = el('div', { class: 'hero-main' },
    el('div', { class: 'hero-top' },
      el('span', { class: 'hero-avatar', 'aria-hidden': 'true' }, whaleSvg('')),
      el('div', { class: 'hero-heading' },
        el('p', { class: 'eyebrow', text: '她现在的样子' }),
        el('div', { class: 'hero-state' }, stateText, moodBadge),
        sinceText,
      ),
    ),
    noteText,
    el('div', { class: 'life-grid' },
      el('div', { class: 'life-item' }, el('div', { class: 'life-label', text: '时段' }), phaseValue),
      el('div', { class: 'life-item' }, el('div', { class: 'life-label', text: '正在做' }), activityValue),
      el('div', { class: 'life-item' }, el('div', { class: 'life-label', text: '计划时长' }), planValue),
    ),
  );

  const energyValue = live(el('strong', { class: 'energy-value', text: '—' }));
  const energyRing = el('div', {
    class: 'energy-ring',
    role: 'progressbar',
    'aria-label': '精力',
    'aria-valuemin': '0',
    'aria-valuemax': '100',
    'aria-valuenow': '0',
  },
  el('div', { class: 'energy-ring-inner' },
    energyValue,
    el('span', { class: 'energy-label', text: '精力' }),
  ),
  );
  const energyHint = el('p', { class: 'vital-hint', text: '正在感受她的力气…' });
  refs.energyRing = energyRing;
  refs.energyValue = energyValue;
  refs.energyHint = energyHint;

  const social = buildOverviewBar('社交电量', 'social');
  social.container.classList.add('hero-social');
  refs.social = social;

  const energyBlock = el('div', { class: 'energy-block' }, energyRing, energyHint);
  const vitals = el('div', { class: 'hero-vitals' }, energyBlock, social.container);

  return el('section', { class: 'card status-hero', 'aria-labelledby': 'overview-status-title' },
    el('span', { class: 'visually-hidden', id: 'overview-status-title', text: '她的当前状态' }),
    heroMain,
    vitals,
  );
}

function buildOverviewBar(label, key) {
  const value = el('strong', { text: '—' });
  const fill = el('div', { class: 'bar-fill' });
  const track = el('div', {
    class: 'bar-track',
    role: 'progressbar',
    'aria-label': label,
    'aria-valuemin': '0',
    'aria-valuemax': '100',
    'aria-valuenow': '0',
  });
  track.append(fill);
  const container = el('div', { class: 'bar' },
    el('div', { class: 'bar-meta' }, el('span', { text: label }), value),
    track,
  );
  return { container, value, fill, track, key };
}

function buildOverviewCounts(refs, live) {
  const counts = [
    ['memory', '记忆', '条'],
    ['rag', '资料库', '块'],
    ['turns', '对话', '轮'],
    ['traces', '轨迹', '条'],
    ['stickers', '表情', '个'],
  ];
  const countGrid = el('div', { class: 'counts-grid' });
  for (const [key, label, unit] of counts) {
    const value = live(el('div', { class: 'count-value', text: '—' }));
    refs.countValues[key] = value;
    const pill = el('div', { class: 'count-pill' },
      el('span', { class: 'count-label', text: label }),
      value,
      el('span', { class: 'count-unit', text: unit }),
    );
    if (key === 'rag') pill.title = '资料库（RAG）片段';
    countGrid.append(pill);
  }

  const wakeups = el('div', { class: 'status-list', 'aria-label': '唤醒任务状态' });
  const outbox = el('div', { class: 'status-list', 'aria-label': '待发件箱状态' });
  refs.wakeups = wakeups;
  refs.outbox = outbox;

  return el('section', { class: 'card counts-card' },
    cardHead('她收好的东西', '记忆、资料库（RAG）和日常，都在这台电脑里。'),
    countGrid,
    el('div', { class: 'pill-groups' },
      el('div', { class: 'pill-group' },
        el('div', { class: 'pill-group-label', text: '唤醒任务' }),
        wakeups,
      ),
      el('div', { class: 'pill-group' },
        el('div', { class: 'pill-group-label', text: '待发件箱' }),
        outbox,
      ),
    ),
  );
}

function buildOverviewTraceCard(refs, live) {
  const entries = [
    ['turns', '回合'],
    ['blocked', '被拦截'],
    ['avg_latency_ms', '平均延迟'],
    ['prompt_tokens', '提示 Token'],
    ['completion_tokens', '回复 Token'],
    ['cache_hit_tokens', '缓存命中'],
  ];
  const stats = el('div', { class: 'trace-stats' });
  for (const [key, label] of entries) {
    const value = live(el('div', { class: 'stat-value', text: '—' }));
    refs.traceValues[key] = value;
    stats.append(el('div', { class: 'trace-stat' },
      el('div', { class: 'stat-label', text: label }),
      value,
    ));
  }
  return el('section', { class: 'card trace-card' },
    cardHead('最近轨迹', '最近 100 轮的汇总。'),
    stats,
  );
}

function buildOverviewSecurity(refs, live) {
  const status = live(badge('——', 'tone-muted'));
  const hash = live(el('span', { class: 'hash-text', text: '——' }));
  const schema = live(el('span', { class: 'text-soft', text: '——' }));
  refs.security.status = status;
  refs.security.hash = hash;
  refs.security.schema = schema;

  return el('section', { class: 'card security-card' },
    cardHead('安全内核', '人设文件的完整性。'),
    el('div', { class: 'security-list' },
      el('div', { class: 'security-row' }, el('span', { text: '内核状态' }), status),
      el('div', { class: 'security-row' }, el('span', { text: '哈希前缀' }), hash),
      el('div', { class: 'security-row' }, el('span', { text: '结构版本' }), schema),
    ),
  );
}

function buildOverviewHealth(refs, live, container) {
  const makeItem = (label, key, extraClass) => {
    const value = live(el('div', { class: 'health-value', text: '——' }));
    refs.healthValues[key] = value;
    return el('div', { class: 'health-item' + (extraClass ? ' ' + extraClass : '') },
      el('div', { class: 'health-label', text: label }),
      value,
    );
  };

  const grid = el('div', { class: 'health-grid' },
    makeItem('档案', 'profile'),
    makeItem('版本', 'version'),
    makeItem('模型', 'model'),
    makeItem('模型服务', 'llm'),
    makeItem('语音', 'tts'),
    makeItem('SQLite', 'sqlite'),
    makeItem('数据库大小', 'dbSize'),
    makeItem('数据库路径', 'dbPath', 'health-path'),
  );

  return el('section', { class: 'card health-card' },
    cardHead('运行状况', '服务、模型和本地数据库。'),
    grid,
  );
}

async function loadOverviewInitial() {
  if (!app.overview.refs) return;
  try {
    const [stateResult, healthResult] = await Promise.allSettled([
      fetchJSON('/api/state', { signal: app.viewAbort ? app.viewAbort.signal : undefined }),
      fetchJSON('/api/health', { signal: app.viewAbort ? app.viewAbort.signal : undefined }),
    ]);
    if (!app.overview.refs) return;
    if (stateResult.status === 'rejected') throw stateResult.reason;
    app.overview.state = stateResult.value;
    app.overview.lastUpdatedAt = Date.now();
    if (healthResult.status === 'fulfilled') {
      app.overview.health = healthResult.value;
      app.overview.lastHealthAt = Date.now();
      setGlobalOnline(true);
    } else if (healthResult.reason && healthResult.reason.code !== 'aborted') {
      setGlobalOnline(false);
    }
    app.overview.refs.loadingEls.forEach((node) => node.classList.remove('is-loading-value'));
    updateOverviewUi();
    updateOverviewHealth();
    clearOverviewError();
    if (app.route === 'overview') startPoll(pollOverview, 5000);
  } catch (error) {
    if (!error || error.code === 'aborted' || !app.overview.refs) return;
    app.overview.refs.loadingEls.forEach((node) => node.classList.remove('is-loading-value'));
    setGlobalOnline(false);
    showOverviewError(error);
  }
}

async function pollOverview() {
  if (!app.overview.refs || app.route !== 'overview') return;
  const signal = app.viewAbort ? app.viewAbort.signal : undefined;
  try {
    const data = await fetchJSON('/api/state', { timeout: 12000, signal });
    app.overview.state = data;
    app.overview.lastUpdatedAt = Date.now();
    updateOverviewUi();
    clearOverviewError();
    if (Date.now() - app.overview.lastHealthAt > 30000) {
      try {
        const health = await fetchJSON('/api/health', { timeout: 12000, signal });
        app.overview.health = health;
        app.overview.lastHealthAt = Date.now();
        updateOverviewHealth();
      } catch (healthError) {
        if (healthError && healthError.code === 'aborted') return;
      }
    }
  } catch (error) {
    if (!error || error.code === 'aborted' || !app.overview.refs) return;
    if (app.overview.refs.updated) {
      app.overview.refs.updated.textContent = '同步失败 · ' + fmtTime(Date.now());
    }
  }
}

function updateOverviewUi() {
  const refs = app.overview.refs;
  const state = app.overview.state;
  if (!refs || !state) return;

  const snapshot = state.state && typeof state.state === 'object' ? state.state : {};
  const life = state.life && typeof state.life === 'object' ? state.life : {};
  const counts = state.counts && typeof state.counts === 'object' ? state.counts : {};
  const traces = state.traces && typeof state.traces === 'object' ? state.traces : {};
  const security = state.security && typeof state.security === 'object' ? state.security : {};

  const activity = String(life.current_activity || '').trim();
  const passiveState = snapshot.state === 'idle' || snapshot.state === 'sleeping';
  const realActivity = activity !== '' && activity !== 'idle' && activity !== 'sleeping' && activity !== 'chatting';
  const useActivity = passiveState && realActivity;
  setText(refs.stateText, useActivity ? activityLabel(activity) : stateLabel(snapshot.state), '未知');
  const mood = moodLabel(life.mood);
  setBadge(refs.moodBadge, mood, moodTone(life.mood));
  if (refs.sinceText) {
    const since = useActivity ? life.activity_started_at : snapshot.since;
    const reasonText = activityNote(useActivity ? life.note : snapshot.reason);
    const reason = reasonText ? ' · ' + truncate(reasonText, 60) : '';
    refs.sinceText.textContent = since
      ? '自 ' + fmtDate(since) + ' · 已持续 ' + durationSince(since) + reason
      : '状态时间未知' + reason;
  }
  setText(refs.noteText, activityNote(life.note) || '她没写备注，海面替她记着。', '她没写备注，海面替她记着。');

  const awakeDuringQuiet = life.phase === 'sleeping' && activity !== '' && activity !== 'sleeping' && activity !== 'idle';
  setText(refs.phaseValue, awakeDuringQuiet ? '安静时段（还没睡）' : phaseLabel(life.phase));
  setText(refs.activityValue, activityLabel(life.current_activity));
  const planned = asNumber(life.planned_minutes);
  setText(refs.planValue, planned != null && planned > 0
    ? planned + ' 分钟 · ' + relativeTime(life.activity_started_at)
    : '未计划');

  updateEnergyRing(refs.energyRing, pct(life.energy), refs.energyHint);
  updateOverviewBar(refs.social, fractionPct(life.social));

  for (const [key, node] of Object.entries(refs.countValues)) {
    setText(node, fmtNum(counts[key], '0'));
  }

  renderStatusPills(refs.wakeups, state.wakeups);
  renderStatusPills(refs.outbox, state.outbox);

  setText(refs.traceValues.turns, fmtNum(traces.turns, '0'));
  setText(refs.traceValues.blocked, fmtNum(traces.blocked, '0'));
  setText(refs.traceValues.avg_latency_ms, fmtMs(traces.avg_latency_ms));
  setText(refs.traceValues.prompt_tokens, fmtNum(traces.prompt_tokens, '0'));
  setText(refs.traceValues.completion_tokens, fmtNum(traces.completion_tokens, '0'));
  setText(refs.traceValues.cache_hit_tokens, fmtNum(traces.cache_hit_tokens, '0'));

  const kernelOk = security.kernel_ok;
  let kernelText = '未知';
  let kernelTone = 'tone-muted';
  if (kernelOk === true || kernelOk === 1) {
    kernelText = '完好';
    kernelTone = 'tone-ok';
  } else if (kernelOk === false || kernelOk === 0) {
    kernelText = '异常';
    kernelTone = 'tone-danger';
  }
  setBadge(refs.security.status, kernelText, kernelTone);
  const hash = security.kernel_hash ? String(security.kernel_hash) : '';
  setText(refs.security.hash, hash ? truncate(hash, 17) : '—');
  if (refs.security.hash) refs.security.hash.title = hash || '';
  setText(refs.security.schema, security.schema_version == null ? '—' : String(security.schema_version));

  if (refs.updated) {
    refs.updated.textContent = '同步于 ' + fmtTime(app.overview.lastUpdatedAt || Date.now());
    refs.updated.title = fmtDate(app.overview.lastUpdatedAt || Date.now());
  }

  updateSidebarLive(state);
}

function energyHintText(value) {
  if (value == null) return '还没感觉到她的精力';
  if (value >= 75) return '精神很好，能陪你很久';
  if (value >= 50) return '还有力气，再聊一会儿';
  if (value >= 25) return '有点累了，但还在听';
  if (value > 0) return '想靠着你打个盹';
  return '困得睁不开眼了';
}

function updateEnergyRing(ring, percent, hintEl) {
  if (!ring) return;
  const valueEl = ring.querySelector('.energy-value');
  if (percent == null) {
    ring.style.setProperty('--ring-value', '0');
    ring.setAttribute('aria-valuenow', '0');
    ring.setAttribute('aria-valuetext', '精力未知');
    if (valueEl) valueEl.textContent = '—';
    if (hintEl) hintEl.textContent = energyHintText(null);
    return;
  }
  const clamped = clamp(percent, 0, 100);
  ring.style.setProperty('--ring-value', String(clamped));
  ring.setAttribute('aria-valuenow', String(Math.round(clamped)));
  ring.setAttribute('aria-valuetext', '精力 ' + Math.round(clamped) + '%');
  if (valueEl) valueEl.textContent = Math.round(clamped) + '%';
  if (hintEl) hintEl.textContent = energyHintText(clamped);
}

function updateOverviewBar(bar, percent) {
  if (!bar || !bar.fill || !bar.value || !bar.track) return;
  if (percent == null) {
    bar.value.textContent = '—';
    bar.fill.style.width = '0%';
    bar.track.setAttribute('aria-valuenow', '0');
    return;
  }
  const clamped = clamp(percent, 0, 100);
  bar.value.textContent = Math.round(clamped) + '%';
  bar.fill.style.width = clamped + '%';
  bar.track.setAttribute('aria-valuenow', String(Math.round(clamped)));
}

function renderStatusPills(container, statuses) {
  if (!container) return;
  container.replaceChildren();
  if (!statuses || typeof statuses !== 'object' || Array.isArray(statuses)) {
    container.append(chip('暂无', 'chip-muted'));
    return;
  }
  const order = ['pending', 'processing', 'fired', 'done', 'dead', 'failed', 'cancelled', 'expired'];
  const keys = Object.keys(statuses).sort((a, b) => {
    const ai = order.indexOf(a);
    const bi = order.indexOf(b);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
  });
  let shown = 0;
  for (const key of keys) {
    const value = statuses[key];
    if (value && typeof value === 'object') continue;
    const pill = el('span', { class: 'status-pill' },
      el('span', { text: statusLabel(key) }),
      el('strong', { text: fmtNum(value, '0') }),
    );
    container.append(pill);
    shown += 1;
  }
  if (!shown) container.append(chip('暂无', 'chip-muted'));
}

function updateOverviewHealth() {
  const refs = app.overview.refs;
  const health = app.overview.health;
  if (!refs || !refs.healthValues) return;
  const values = refs.healthValues;
  if (!health) return;

  const db = health.db && typeof health.db === 'object' ? health.db : {};
  const llm = health.llm && typeof health.llm === 'object' ? health.llm : {};
  const tts = health.tts && typeof health.tts === 'object' ? health.tts : {};

  setText(values.profile, health.profile ? profileLabel(health.profile) : '—');
  setText(values.version, (health.app || 'fatfish') + (health.version ? ' ' + health.version : ''));
  setText(values.model, llm.model ? modelLabel(llm.model) : '未配置');
  setBadge(values.llm, llm.available ? '可用' : '本地回复', llm.available ? 'tone-ok' : 'tone-warn');
  if (values.llm) {
    values.llm.title = [
      llm.provider ? '服务商：' + providerLabel(llm.provider) : '',
      llm.key_present ? '已配置密钥' : '未检测到密钥，当前使用本地回复',
    ].filter(Boolean).join(' · ');
  }

  let ttsText = '未启用';
  if (tts.enabled) {
    ttsText = '已启用 · ' + ttsModeLabel(tts.mode || '') + (tts.configured ? '' : ' · 未配置');
  }
  setText(values.tts, ttsText);

  const fts = db.fts5 ? '全文检索（FTS5）已启用' : '全文检索（FTS5）未启用';
  const version = db.sqlite_version ? 'v' + db.sqlite_version : '内置 SQLite';
  const schema = db.schema_version == null ? '' : ' · 结构版本 ' + db.schema_version;
  setText(values.sqlite, version + ' · ' + fts + schema);
  if (values.sqlite) {
    values.sqlite.title = '完整性：' + (db.integrity == null ? '—' : String(db.integrity)) +
      ' · 结构版本 ' + (db.schema_version == null ? '—' : String(db.schema_version));
  }
  setText(values.dbSize, fmtBytes(db.size_bytes));
  setText(values.dbPath, db.path || '—');
  if (values.dbPath) values.dbPath.title = db.path || '';
}

function showOverviewError(error) {
  const refs = app.overview.refs;
  if (!refs || !refs.errorSlot) return;
  refs.errorSlot.replaceChildren(
    el('div', { class: 'error-banner' },
      el('span', { text: '她的今天加载失败：' + friendlyError(error) }),
      el('button', {
        class: 'btn btn-ghost btn-sm',
        type: 'button',
        text: '重试',
        onClick: () => {
          refs.errorSlot.replaceChildren();
          loadOverviewInitial();
        },
      }),
    ),
  );
}

function clearOverviewError() {
  const refs = app.overview.refs;
  if (refs && refs.errorSlot) refs.errorSlot.replaceChildren();
}

async function refreshOverviewManual(button) {
  if (app.overview.actionBusy) return;
  app.overview.actionBusy = true;
  setButtonBusy(button, true, '刷新中…');
  try {
    const signal = app.viewAbort ? app.viewAbort.signal : undefined;
    const [stateResult, healthResult] = await Promise.allSettled([
      fetchJSON('/api/state', { signal }),
      fetchJSON('/api/health', { signal }),
    ]);
    if (stateResult.status === 'rejected') throw stateResult.reason;
    app.overview.state = stateResult.value;
    app.overview.lastUpdatedAt = Date.now();
    if (healthResult.status === 'fulfilled') {
      app.overview.health = healthResult.value;
      app.overview.lastHealthAt = Date.now();
      setGlobalOnline(true);
    }
    if (app.overview.refs) {
      app.overview.refs.loadingEls.forEach((node) => node.classList.remove('is-loading-value'));
      updateOverviewUi();
      updateOverviewHealth();
      clearOverviewError();
      if (app.route === 'overview') startPoll(pollOverview, 5000);
    }
    if (healthResult.status === 'rejected' && healthResult.reason && healthResult.reason.code !== 'aborted') {
      toast('状态已刷新，但健康检查失败：' + friendlyError(healthResult.reason), 'error');
    } else {
      toast('她的今天已刷新', 'success');
    }
  } catch (error) {
    if (error && error.code === 'aborted') return;
    showOverviewError(error);
    toast('刷新失败：' + friendlyError(error), 'error');
  } finally {
    app.overview.actionBusy = false;
    if (button && button.isConnected) setButtonBusy(button, false);
  }
}

async function runTickAction(button) {
  if (app.overview.actionBusy) return;
  app.overview.actionBusy = true;
  setButtonBusy(button, true, '运行中…');
  try {
    await fetchJSON('/api/tick', {
      method: 'POST',
      body: {},
      timeout: 60000,
    });
    toast('已推进一次日常', 'success');
    try {
      const signal = app.viewAbort ? app.viewAbort.signal : undefined;
      const state = await fetchJSON('/api/state', { signal });
      app.overview.state = state;
      app.overview.lastUpdatedAt = Date.now();
      if (app.overview.refs) updateOverviewUi();
    } catch (stateError) {
      if (stateError && stateError.code !== 'aborted') {
        /* tick 已成功，状态刷新失败不额外打扰 */
      }
    }
  } catch (error) {
    if (error && error.code !== 'aborted') {
      toast('推进日常失败：' + friendlyError(error), 'error');
    }
  } finally {
    app.overview.actionBusy = false;
    if (button && button.isConnected) setButtonBusy(button, false);
  }
}

async function runNightlyAction(button) {
  if (app.overview.actionBusy) return;
  app.overview.actionBusy = true;
  setButtonBusy(button, true, '预演中…');
  try {
    const data = await fetchJSON('/api/nightly', {
      method: 'POST',
      body: { apply: false },
      timeout: 120000,
    });
    toast('记忆压缩预演完成', 'success');
    const refs = app.overview.refs;
    if (!refs || !refs.nightlySlot) return;
    const stats = data.stats == null ? {} : data.stats;
    const details = el('details', { class: 'details', open: true },
      el('summary', {},
        el('span', { text: '记忆压缩预演 · 只看结果，不写入' }),
        badge(data.applied ? '已写入' : '仅预览', data.applied ? 'tone-warn' : 'tone-info'),
      ),
      el('pre', { class: 'json-block', text: prettyJSON(stats) }),
    );
    refs.nightlySlot.replaceChildren(details);
  } catch (error) {
    if (error && error.code !== 'aborted') {
      toast('预演失败：' + friendlyError(error), 'error');
    }
  } finally {
    app.overview.actionBusy = false;
    if (button && button.isConnected) setButtonBusy(button, false);
  }
}

/* ============================================================================
   10. 记忆视图
   ========================================================================== */

function renderMemoryView(container) {
  const refs = {
    search: null,
    limit: null,
    results: null,
    resultSub: null,
    resultMeta: null,
    catalog: null,
    loops: null,
    clearSearch: null,
  };
  app.memory.refs = refs;

  const searchInput = el('input', {
    class: 'input',
    type: 'search',
    id: 'memory-search',
    placeholder: '按语义或关键词搜索…',
    autocomplete: 'off',
    value: app.memory.query,
    'aria-label': '搜索记忆',
  });
  refs.search = searchInput;

  const clearSearch = el('button', {
    class: 'search-clear',
    type: 'button',
    'aria-label': '清空搜索',
    hidden: !app.memory.query,
    onClick: () => {
      searchInput.value = '';
      app.memory.query = '';
      syncSearchClear();
      if (app.memory.debounce) window.clearTimeout(app.memory.debounce);
      runMemorySearch(true);
      searchInput.focus();
    },
  }, iconSvg('icon-close'));
  refs.clearSearch = clearSearch;

  function syncSearchClear() {
    clearSearch.hidden = !searchInput.value;
  }

  const limitInput = el('input', {
    class: 'input',
    type: 'number',
    id: 'memory-limit',
    min: '1',
    max: '100',
    step: '1',
    value: String(app.memory.limit),
    inputmode: 'numeric',
    'aria-label': '最多显示条数',
  });
  refs.limit = limitInput;

  const statusSelect = el('select', {
    class: 'input',
    id: 'memory-status',
    'aria-label': '记忆状态',
  },
    el('option', { value: 'active', text: '已启用' }),
    el('option', { value: 'deleted', text: '已删除' }),
    el('option', { value: 'all', text: '全部' }),
  );
  statusSelect.value = app.memory.status;

  const form = el('form', { class: 'search-form', role: 'search', onSubmit: (event) => {
    event.preventDefault();
    app.memory.debounce && window.clearTimeout(app.memory.debounce);
    app.memory.query = searchInput.value.trim();
    syncSearchClear();
    runMemorySearch();
  } },
  el('div', { class: 'field search-field' },
    el('label', { class: 'field-label', for: 'memory-search', text: '搜索记忆' }),
    el('div', { class: 'search-box' },
      iconSvg('icon-search', 'search-icon'),
      searchInput,
      clearSearch,
    ),
  ),
  el('div', { class: 'field limit-field' },
    el('label', { class: 'field-label', for: 'memory-limit', text: '最多' }),
    limitInput,
  ),
  el('div', { class: 'field memory-status-field' },
    el('label', { class: 'field-label', for: 'memory-status', text: '状态' }),
    statusSelect,
  ),
  el('button', { class: 'btn btn-primary', type: 'submit', text: '找一找' }),
  );

  searchInput.addEventListener('input', () => {
    syncSearchClear();
    if (app.memory.debounce) window.clearTimeout(app.memory.debounce);
    app.memory.debounce = window.setTimeout(() => {
      app.memory.query = searchInput.value.trim();
      runMemorySearch();
    }, 300);
  });

  limitInput.addEventListener('change', () => {
    const parsed = asNumber(limitInput.value);
    app.memory.limit = clamp(parsed == null ? 20 : Math.round(parsed), 1, 100);
    limitInput.value = String(app.memory.limit);
    runMemorySearch();
  });

  statusSelect.addEventListener('change', () => {
    app.memory.status = statusSelect.value || 'active';
    runMemorySearch();
  });

  const head = viewHeader(
    '记忆',
    '她记住的事',
    '不是聊天记录堆砌，而是被认真收好的片段、约定和偏好。',
  );

  const toolbar = el('div', { class: 'memory-toolbar' }, form);
  refs.resultMeta = el('div', { class: 'result-summary' });
  refs.resultSub = el('p', { class: 'card-sub', text: '正在读取记忆…' });

  const results = el('div', { class: 'memory-list', 'aria-live': 'polite' });
  refs.results = results;

  const resultsCard = el('section', { class: 'card memory-results' },
    el('div', { class: 'card-head' },
      el('div', { class: 'card-head-copy' },
        el('h2', { class: 'card-title', text: '搜索结果' }),
        refs.resultSub,
      ),
      el('div', { class: 'card-actions' }, refs.resultMeta),
    ),
    results,
  );

  const catalog = el('div', { class: 'catalog-list' });
  refs.catalog = catalog;
  const catalogCard = el('section', { class: 'card' },
    cardHead('记忆目录', '常见的主题与实体，点一下就能搜索。'),
    catalog,
  );

  const loops = el('div', { class: 'loops-list' });
  refs.loops = loops;
  const loopsCard = el('section', { class: 'card' },
    cardHead('未完成的事', '说好的事，她都记着。'),
    loops,
  );

  const page = el('div', { class: 'view-page' },
    head,
    toolbar,
    el('div', { class: 'memory-layout' },
      el('div', { class: 'memory-main' }, resultsCard),
      el('div', { class: 'memory-side' }, catalogCard, loopsCard),
    ),
  );
  container.append(page);

  addCleanup(() => {
    if (app.memory.debounce) {
      window.clearTimeout(app.memory.debounce);
      app.memory.debounce = null;
    }
  });

  runMemorySearch(true);
  loadMemoryCatalog();
  loadOpenLoops();
}

async function runMemorySearch(showSkeleton) {
  const refs = app.memory.refs;
  if (!refs || app.route !== 'memory') return;
  const seq = ++app.memory.seq;
  const query = app.memory.query.trim();
  const limit = clamp(app.memory.limit, 1, 100);

  if (showSkeleton || !refs.results.children.length) {
    refs.results.replaceChildren(skeletonList(3));
  }
  refs.resultSub.textContent = '正在搜索记忆…';
  refs.resultMeta.replaceChildren(el('span', { class: 'search-spinner' },
    el('span', { class: 'spinner', 'aria-hidden': 'true' }), el('span', { text: '读取中' })));

  const params = new URLSearchParams();
  params.set('query', query);
  params.set('limit', String(limit));
  params.set('status', app.memory.status || 'active');
  const url = '/api/memory?' + params.toString();

  try {
    const data = await fetchJSON(url, { signal: app.viewAbort ? app.viewAbort.signal : undefined });
    if (seq !== app.memory.seq || app.route !== 'memory' || !app.memory.refs) return;
    setGlobalOnline(true);
    const isSearch = Boolean(query);
    const hits = isSearch && Array.isArray(data.hits) ? data.hits : null;
    const items = !isSearch && Array.isArray(data.items) ? data.items : (hits ? hits.map((hit) => hit.item) : []);
    const count = asNumber(data.count) == null ? items.length : Number(data.count);

    refs.results.replaceChildren();
    refs.resultSub.textContent = isSearch
      ? (query ? '“' + truncate(query, 40) + '” 的召回结果。' : '没有输入关键词。')
      : '最近活跃的记忆。';
    refs.resultMeta.replaceChildren(
      badge((isSearch ? '找到 ' : '共 ') + fmtNum(count, '0') + ' 条', isSearch ? 'tone-info' : 'tone-muted'),
    );

    if (!items.length) {
      refs.results.append(emptyPanel(
        isSearch ? '没有找到匹配的记忆' : '还没有记忆',
        isSearch ? '试试更短的关键词，或换一种说法。' : '和她聊几句，重要的事会慢慢留下来。',
      ));
      return;
    }

    items.forEach((item, index) => {
      const hit = hits ? hits[index] : null;
      refs.results.append(buildMemoryItem(item, hit));
    });
  } catch (error) {
    if (!error || error.code === 'aborted' || seq !== app.memory.seq || !app.memory.refs) return;
    refs.results.replaceChildren(errorPanel('记忆搜索失败', error, () => runMemorySearch(true)));
    refs.resultSub.textContent = '搜索失败。';
    refs.resultMeta.replaceChildren();
  }
}

function buildMemoryItem(item, hit) {
  const safeItem = item && typeof item === 'object' ? item : {};
  const gist = String(safeItem.gist || safeItem.summary || '未命名记忆');
  const summary = String(safeItem.summary || '');
  const showSummary = summary && summary !== gist;
  const typeKey = String(safeItem.type || '').toLowerCase().replace(/[^a-z_]/g, '');
  const card = el('article', { class: 'memory-item' + (typeKey ? ' type-' + typeKey : '') });

  const head = el('div', { class: 'memory-item-head' });
  head.append(el('h3', { class: 'memory-gist', text: gist }));
  if (hit && asNumber(hit.score) != null) {
    const score = el('div', { class: 'memory-score' },
      el('span', { text: '相关度 ' }),
      el('strong', { text: fmtScore(hit.score) }),
    );
    if (asNumber(hit.similarity) != null) score.title = '相似度 ' + fmtScore(hit.similarity);
    head.append(score);
  } else {
    head.append(el('span', { class: 'badge tone-muted', text: typeLabel(safeItem.type) }));
  }
  card.append(head);

  if (showSummary) card.append(el('p', { class: 'memory-summary', text: summary }));

  const metaRow = el('div', { class: 'chip-row memory-meta' });
  metaRow.append(chip('类型 · ' + typeLabel(safeItem.type)));
  if (safeItem.tier != null) metaRow.append(chip(tierLabel(safeItem.tier)));
  metaRow.append(badge('信任 · ' + trustLabel(safeItem.trust_level), trustTone(safeItem.trust_level)));
  if (safeItem.event_date || safeItem.observed_at) {
    const eventText = safeItem.event_date
      ? fmtWhen(safeItem.event_date)
      : fmtDate(safeItem.observed_at);
    metaRow.append(chip('事件 · ' + eventText));
  }
  if (hit && hit.reason) metaRow.append(chip('召回 · ' + truncate(recallReasonLabel(hit.reason), 36), 'chip-muted'));
  card.append(metaRow);

  const importance = clamp(asNumber(safeItem.importance) == null ? 0 : Number(safeItem.importance), 0, 1);
  const importanceWrap = el('div', { class: 'importance' },
    el('span', { text: '重要性 ' + Math.round(importance * 100) + '%' }),
    el('span', { class: 'importance-track', role: 'progressbar', 'aria-label': '重要性',
      'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(importance * 100)) },
    el('span', { class: 'importance-fill' }),
    ),
  );
  const fill = importanceWrap.querySelector('.importance-fill');
  if (fill) fill.style.width = (importance * 100) + '%';
  card.append(importanceWrap);

  const tags = safeArray(safeItem.tags).filter((tag) => typeof tag === 'string' && tag.trim());
  const entities = safeArray(safeItem.entities).map((entity) => {
    if (typeof entity === 'string') return entity;
    if (entity && typeof entity === 'object') {
      return String(entity.name || entity.text || entity.value || '');
    }
    return '';
  }).filter(Boolean);

  if (tags.length || entities.length) {
    const extra = el('div', { class: 'chip-row memory-extra' });
    tags.slice(0, 10).forEach((tag) => extra.append(chip('#' + tag, 'chip-muted')));
    entities.slice(0, 8).forEach((entity) => extra.append(chip('@' + entity, 'chip-muted')));
    card.append(extra);
  }

  const actions = el('div', { class: 'memory-actions' });
  if (String(safeItem.status || 'active') === 'deleted') {
    actions.append(el('button', {
      class: 'btn btn-ghost btn-small',
      type: 'button',
      text: '恢复',
      onClick: () => restoreMemoryItem(safeItem),
    }));
  } else {
    actions.append(
      el('button', {
        class: 'btn btn-ghost btn-small',
        type: 'button',
        text: '编辑',
        onClick: () => openMemoryEditor(safeItem),
      }),
      el('button', {
        class: 'btn btn-ghost btn-small memory-delete',
        type: 'button',
        text: '删除',
        onClick: () => deleteMemoryItem(safeItem),
      }),
    );
  }
  card.append(actions);

  return card;
}

function memoryTypeOptions() {
  return ['episodic', 'preference', 'relationship', 'prospective', 'semantic', 'emotional']
    .map((value) => el('option', { value, text: typeLabel(value) }));
}

function memoryListText(value) {
  return safeArray(value).map((item) => String(item)).filter(Boolean).join(', ');
}

function openMemoryEditor(item) {
  const safeItem = item && typeof item === 'object' ? item : {};
  const dialog = el('dialog', { class: 'memory-dialog' });
  const gistInput = el('input', { class: 'input', value: String(safeItem.gist || ''), 'aria-label': '记忆标题' });
  const summaryInput = el('textarea', { class: 'input', rows: '5', 'aria-label': '记忆内容' });
  summaryInput.value = String(safeItem.summary || '');
  const typeSelect = el('select', { class: 'input', 'aria-label': '记忆类型' }, memoryTypeOptions());
  typeSelect.value = String(safeItem.type || 'episodic');
  const importanceInput = el('input', {
    class: 'input', type: 'number', min: '0', max: '1', step: '0.05',
    value: String(clamp(asNumber(safeItem.importance) == null ? 0.5 : Number(safeItem.importance), 0, 1)),
    'aria-label': '重要性 0 到 1',
  });
  const tagsInput = el('input', { class: 'input', value: memoryListText(safeItem.tags), 'aria-label': '标签，用逗号分隔' });
  const entitiesInput = el('input', { class: 'input', value: memoryListText(safeItem.entities), 'aria-label': '实体，用逗号分隔' });
  const errorSlot = el('p', { class: 'form-error', role: 'alert' });
  const saveButton = el('button', { class: 'btn btn-primary', type: 'submit', text: '保存' });
  const cancelButton = el('button', { class: 'btn btn-ghost', type: 'button', text: '取消', onClick: () => dialog.close() });

  const form = el('form', { class: 'memory-form', onSubmit: async (event) => {
    event.preventDefault();
    errorSlot.textContent = '';
    const gist = gistInput.value.trim();
    const summary = summaryInput.value.trim();
    if (!gist && !summary) {
      errorSlot.textContent = '标题和内容至少要留一个。';
      return;
    }
    const importance = clamp(asNumber(importanceInput.value) == null ? 0.5 : Number(importanceInput.value), 0, 1);
    setButtonBusy(saveButton, true);
    try {
      await fetchJSON('/api/memory/update', { method: 'POST', body: {
        id: String(safeItem.id || ''),
        gist,
        summary,
        type: typeSelect.value,
        importance,
        tags: tagsInput.value.split(',').map((v) => v.trim()).filter(Boolean),
        entities: entitiesInput.value.split(',').map((v) => v.trim()).filter(Boolean),
      } });
      dialog.close();
      toast('记忆已更新。', 'success');
      runMemorySearch(true);
      loadMemoryCatalog();
    } catch (error) {
      errorSlot.textContent = friendlyError(error);
    } finally {
      setButtonBusy(saveButton, false);
    }
  } },
    el('div', { class: 'memory-form-grid' },
      el('label', { class: 'field' }, el('span', { class: 'field-label', text: '标题' }), gistInput),
      el('label', { class: 'field' }, el('span', { class: 'field-label', text: '类型' }), typeSelect),
      el('label', { class: 'field' }, el('span', { class: 'field-label', text: '重要性' }), importanceInput),
      el('label', { class: 'field' }, el('span', { class: 'field-label', text: '标签' }), tagsInput),
      el('label', { class: 'field memory-form-wide' }, el('span', { class: 'field-label', text: '内容' }), summaryInput),
      el('label', { class: 'field memory-form-wide' }, el('span', { class: 'field-label', text: '实体' }), entitiesInput),
    ),
    errorSlot,
    el('div', { class: 'memory-form-actions' }, cancelButton, saveButton),
  );

  dialog.append(
    el('div', { class: 'memory-dialog-head' },
      el('h2', { text: '编辑记忆' }),
      el('button', { class: 'dialog-close', type: 'button', text: '×', 'aria-label': '关闭', onClick: () => dialog.close() }),
    ),
    el('p', { class: 'card-sub', text: '只修正这条记忆，不会改动原始聊天记录。' }),
    form,
  );
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  gistInput.focus();
}

async function deleteMemoryItem(item) {
  const safeItem = item && typeof item === 'object' ? item : {};
  if (!window.confirm('只从记忆里移除，不会删除原始聊天。确定删除「' + truncate(String(safeItem.gist || '这条记忆'), 30) + '」吗？')) return;
  try {
    await fetchJSON('/api/memory/delete', { method: 'POST', body: { id: String(safeItem.id || ''), hard: false } });
    toast('已从记忆里移除，可以切到“已删除”恢复。', 'info');
    runMemorySearch(true);
    loadMemoryCatalog();
  } catch (error) {
    toast('删除失败：' + friendlyError(error), 'error');
  }
}

async function restoreMemoryItem(item) {
  const safeItem = item && typeof item === 'object' ? item : {};
  try {
    await fetchJSON('/api/memory/restore', { method: 'POST', body: { id: String(safeItem.id || '') } });
    toast('已恢复这条记忆。', 'success');
    runMemorySearch(true);
    loadMemoryCatalog();
  } catch (error) {
    toast('恢复失败：' + friendlyError(error), 'error');
  }
}

async function loadMemoryCatalog() {
  const refs = app.memory.refs;
  if (!refs) return;
  refs.catalog.replaceChildren(skeletonList(3, 'skeleton-row'));
  try {
    const data = await fetchJSON('/api/memory/meta?limit=24', {
      signal: app.viewAbort ? app.viewAbort.signal : undefined,
    });
    if (!app.memory.refs || app.route !== 'memory') return;
    const items = Array.isArray(data.items) ? data.items : [];
    refs.catalog.replaceChildren();
    if (!items.length) {
      refs.catalog.append(emptyPanel('目录还是空的', '等记忆多起来，这里会长出主题和名字。'));
      return;
    }
    for (const item of items) {
      const name = String(item && item.name || '').trim();
      if (!name) continue;
      const meta = el('span', { class: 'catalog-meta', text: fmtNum(item.count, '0') + ' 条' });
      const lastSeen = item.last_seen_text || relativeTime(item.last_seen);
      const catalogButton = el('button', {
        class: 'catalog-item',
        type: 'button',
        title: name + ' · 最近 ' + lastSeen,
        onClick: () => {
          const currentRefs = app.memory.refs;
          if (!currentRefs) return;
          currentRefs.search.value = name;
          app.memory.query = name;
          if (currentRefs.clearSearch) currentRefs.clearSearch.hidden = false;
          runMemorySearch(true);
          currentRefs.search.focus();
        },
      },
      el('span', { class: 'catalog-name', text: name }),
      meta,
      );
      refs.catalog.append(catalogButton);
    }
  } catch (error) {
    if (!error || error.code === 'aborted' || !app.memory.refs) return;
    refs.catalog.replaceChildren(errorPanel('记忆目录加载失败', error, loadMemoryCatalog));
  }
}

async function loadOpenLoops() {
  const refs = app.memory.refs;
  if (!refs) return;
  refs.loops.replaceChildren(skeletonList(2, 'skeleton-row'));
  try {
    const data = await fetchJSON('/api/memory/open-loops?limit=20', {
      signal: app.viewAbort ? app.viewAbort.signal : undefined,
    });
    if (!app.memory.refs || app.route !== 'memory') return;
    const items = Array.isArray(data.items) ? data.items : [];
    refs.loops.replaceChildren();
    if (!items.length) {
      refs.loops.append(emptyPanel('没有未完成的事', '说好的事都做完了，或者她还没舍得记下来。'));
      return;
    }
    for (const loop of items) {
      const text = String(loop && (loop.text || loop.title) || '未完成的事');
      const status = String(loop && loop.status || 'open');
      const due = loop && loop.due_at;
      const overdue = due != null && status === 'open' && toDate(due) && toDate(due).getTime() < Date.now();
      const meta = el('div', { class: 'loop-meta' },
        badge(loopKindLabel(loop && loop.kind), 'tone-muted'),
        badge(statusLabel(status), statusTone(status)),
        el('span', { text: '截止 ' + fmtWhen(due) }),
      );
      if (overdue) meta.append(badge('已逾期', 'tone-warn'));
      const item = el('div', { class: 'loop-item' },
        el('p', { class: 'loop-text', text }),
        meta,
      );
      if (loop && loop.source_ref) item.title = '来源：' + String(loop.source_ref);
      refs.loops.append(item);
    }
  } catch (error) {
    if (!error || error.code === 'aborted' || !app.memory.refs) return;
    refs.loops.replaceChildren(errorPanel('未完成事项加载失败', error, loadOpenLoops));
  }
}

/* ============================================================================
   11. 表达视图（她学会的话）
   ========================================================================== */

const LEXICON_STATUS_FILTERS = [
  { key: 'candidate', label: '待审' },
  { key: 'approved', label: '已启用' },
  { key: 'disabled', label: '已停用' },
  { key: 'all', label: '全部' },
];

const LEXICON_KIND_FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'expression', label: '表达' },
  { key: 'jargon', label: '黑话' },
];

const LEXICON_LIMIT = 200;

function renderLexiconView(container) {
  const refs = {
    stats: {},
    resultList: null,
    resultSub: null,
    resultMeta: null,
    statusChips: [],
    kindChips: [],
    search: null,
    clearSearch: null,
    addDetails: null,
    addForm: null,
    addKind: null,
    addContent: null,
    addMeaning: null,
    addSituation: null,
    addStyle: null,
    addTags: null,
    addSubmit: null,
    addError: null,
    mineButton: null,
    mineResult: null,
  };
  app.lexicon.refs = refs;

  const mineButton = el('button', {
    class: 'btn btn-ghost',
    type: 'button',
    text: '从最近对话里挖一挖',
    onClick: () => runLexiconMine(mineButton),
  });
  refs.mineButton = mineButton;

  const head = viewHeader(
    '表达',
    '她学会的话',
    '不是每句话都学，先放进候选，再由你决定要不要用。',
    [mineButton],
  );

  const statGrid = el('div', { class: 'stat-grid lexicon-stats' });
  for (const [key, label] of [
    ['candidate', '待审'],
    ['approved', '已启用'],
    ['disabled', '已停用'],
    ['total', '全部'],
  ]) {
    const value = el('div', { class: 'stat-value', text: '—' });
    refs.stats[key] = value;
    statGrid.append(statTile(label, value));
  }

  const statusGroup = buildLexiconFilterGroup(
    '状态',
    LEXICON_STATUS_FILTERS,
    app.lexicon.status,
    (key) => {
      if (app.lexicon.status === key) return;
      app.lexicon.status = key;
      syncLexiconChips(refs.statusChips, key);
      runLexiconLoad(true);
    },
    refs.statusChips,
  );

  const kindGroup = buildLexiconFilterGroup(
    '类型',
    LEXICON_KIND_FILTERS,
    app.lexicon.kind,
    (key) => {
      if (app.lexicon.kind === key) return;
      app.lexicon.kind = key;
      syncLexiconChips(refs.kindChips, key);
      runLexiconLoad(true);
    },
    refs.kindChips,
  );

  const searchInput = el('input', {
    class: 'input',
    type: 'search',
    id: 'lexicon-search',
    placeholder: '搜内容、意思、场景或标签…',
    autocomplete: 'off',
    value: app.lexicon.query,
    'aria-label': '搜索表达',
  });
  refs.search = searchInput;

  const clearSearch = el('button', {
    class: 'search-clear',
    type: 'button',
    'aria-label': '清空搜索',
    hidden: !app.lexicon.query,
    onClick: () => {
      searchInput.value = '';
      app.lexicon.query = '';
      syncLexiconClear();
      if (app.lexicon.debounce) {
        window.clearTimeout(app.lexicon.debounce);
        app.lexicon.debounce = null;
      }
      runLexiconLoad(true);
      searchInput.focus();
    },
  }, iconSvg('icon-close'));
  refs.clearSearch = clearSearch;

  function syncLexiconClear() {
    clearSearch.hidden = !searchInput.value;
  }

  const searchForm = el('form', {
    class: 'lexicon-search-form',
    role: 'search',
    onSubmit: (event) => {
      event.preventDefault();
      if (app.lexicon.debounce) {
        window.clearTimeout(app.lexicon.debounce);
        app.lexicon.debounce = null;
      }
      app.lexicon.query = searchInput.value.trim();
      syncLexiconClear();
      runLexiconLoad(true);
    },
  },
  el('div', { class: 'search-box' },
    iconSvg('icon-search', 'search-icon'),
    searchInput,
    clearSearch,
  ),
  );

  searchInput.addEventListener('input', () => {
    syncLexiconClear();
    if (app.lexicon.debounce) window.clearTimeout(app.lexicon.debounce);
    app.lexicon.debounce = window.setTimeout(() => {
      app.lexicon.query = searchInput.value.trim();
      runLexiconLoad(true);
    }, 320);
  });

  const mineResult = el('p', {
    class: 'lexicon-mine-result',
    hidden: !app.lexicon.mineResult,
    'aria-live': 'polite',
    text: app.lexicon.mineResult ? lexiconMineText(app.lexicon.mineResult) : '',
  });
  refs.mineResult = mineResult;

  const toolbar = el('div', { class: 'lexicon-toolbar' },
    el('div', { class: 'lexicon-filters' }, statusGroup, kindGroup),
    searchForm,
    mineResult,
  );

  refs.resultSub = el('p', { class: 'card-sub', text: '正在读取词条…' });
  refs.resultMeta = el('div', { class: 'result-summary' });
  const resultList = el('div', { class: 'lexicon-list', 'aria-live': 'polite' });
  refs.resultList = resultList;

  const resultsCard = el('section', { class: 'card lexicon-results' },
    el('div', { class: 'card-head' },
      el('div', { class: 'card-head-copy' },
        el('h2', { class: 'card-title', text: '词条库' }),
        refs.resultSub,
      ),
      el('div', { class: 'card-actions' }, refs.resultMeta),
    ),
    resultList,
  );

  const addKind = el('select', {
    class: 'select',
    id: 'lexicon-add-kind',
    'aria-label': '类型',
  },
  el('option', { value: 'expression', text: '表达' }),
  el('option', { value: 'jargon', text: '黑话' }),
  );
  refs.addKind = addKind;

  const addContent = el('input', {
    class: 'input',
    type: 'text',
    id: 'lexicon-add-content',
    maxlength: '80',
    autocomplete: 'off',
    placeholder: '比如：绷不住了',
    required: true,
  });
  refs.addContent = addContent;

  const addMeaning = el('textarea', {
    class: 'textarea',
    id: 'lexicon-add-meaning',
    rows: '2',
    maxlength: '300',
    placeholder: '它大概是什么意思',
  });
  refs.addMeaning = addMeaning;

  const addSituation = el('textarea', {
    class: 'textarea',
    id: 'lexicon-add-situation',
    rows: '2',
    maxlength: '300',
    placeholder: '什么场合适合用',
  });
  refs.addSituation = addSituation;

  const addStyle = el('select', {
    class: 'select',
    id: 'lexicon-add-style',
    'aria-label': '语气',
  },
  el('option', { value: '', text: '不限语气' }),
  el('option', { value: 'soft', text: '软软的' }),
  el('option', { value: 'tsundere', text: '傲娇' }),
  el('option', { value: 'playful', text: '俏皮' }),
  el('option', { value: 'comfort', text: '安慰' }),
  el('option', { value: 'serious', text: '认真' }),
  );
  refs.addStyle = addStyle;

  const addTags = el('input', {
    class: 'input',
    type: 'text',
    id: 'lexicon-add-tags',
    maxlength: '120',
    autocomplete: 'off',
    placeholder: '用逗号分开，最多 8 个',
  });
  refs.addTags = addTags;

  const addError = el('p', { class: 'form-error', role: 'alert' });
  refs.addError = addError;

  const addSubmit = el('button', {
    class: 'btn btn-primary btn-block',
    type: 'submit',
    text: '放进候选',
  });
  refs.addSubmit = addSubmit;

  const addForm = el('form', {
    class: 'lexicon-add-form',
    onSubmit: handleLexiconAdd,
    novalidate: true,
  },
  el('div', { class: 'lexicon-add-row' },
    lexiconField('类型', 'lexicon-add-kind', addKind),
    lexiconField('语气', 'lexicon-add-style', addStyle),
  ),
  lexiconField('内容', 'lexicon-add-content', addContent),
  lexiconField('意思', 'lexicon-add-meaning', addMeaning),
  lexiconField('适合', 'lexicon-add-situation', addSituation),
  lexiconField('标签', 'lexicon-add-tags', addTags),
  addError,
  addSubmit,
  el('p', { class: 'field-hint', text: '先放进候选，审核通过后她才会偶尔用。' }),
  );
  refs.addForm = addForm;

  const addDetails = el('details', { class: 'details lexicon-add' },
    el('summary', {},
      el('span', { text: '手动加一条' }),
      el('span', { class: 'summary-count', text: '放进候选' }),
    ),
    addForm,
  );
  refs.addDetails = addDetails;

  const addCard = el('section', { class: 'card lexicon-side-card' },
    cardHead('添加表达', '看到好玩的说法，就先收进来。'),
    addDetails,
  );

  const layout = el('div', { class: 'lexicon-layout' },
    el('div', { class: 'lexicon-main' }, resultsCard),
    el('div', { class: 'lexicon-side' }, addCard),
  );

  const page = el('div', { class: 'view-page lexicon-page' },
    head,
    statGrid,
    toolbar,
    layout,
  );
  container.append(page);

  addCleanup(() => {
    if (app.lexicon.debounce) {
      window.clearTimeout(app.lexicon.debounce);
      app.lexicon.debounce = null;
    }
  });

  runLexiconLoad(true);
}

function lexiconField(labelText, id, control) {
  return el('label', { class: 'field', for: id },
    el('span', { class: 'field-label', text: labelText }),
    control,
  );
}

function buildLexiconFilterGroup(labelText, options, activeKey, onPick, chipStore) {
  const row = el('div', { class: 'filter-row' });
  for (const option of options) {
    const active = option.key === activeKey;
    const button = el('button', {
      class: 'filter-chip' + (active ? ' is-active' : ''),
      type: 'button',
      'aria-pressed': String(active),
      text: option.label,
      onClick: () => onPick(option.key),
    });
    chipStore.push({ key: option.key, node: button });
    row.append(button);
  }
  return el('div', { class: 'filter-group' },
    el('span', { class: 'filter-label', text: labelText }),
    row,
  );
}

function syncLexiconChips(chips, activeKey) {
  for (const { key, node } of chips) {
    const active = key === activeKey;
    node.classList.toggle('is-active', active);
    node.setAttribute('aria-pressed', String(active));
  }
}

function lexiconMineText(result) {
  const data = result && typeof result === 'object' ? result : {};
  return '上次挖掘：扫了 ' + fmtNum(data.scanned, '0') + ' 轮对话，候选 '
    + fmtNum(data.candidates, '0') + ' 条，新增 ' + fmtNum(data.added, '0')
    + ' 条，跳过 ' + fmtNum(data.skipped, '0') + ' 条。';
}

async function runLexiconMine(button) {
  if (!button || button.disabled) return;
  setButtonBusy(button, true, '翻对话中…');
  try {
    const data = await fetchJSON('/api/lexicon/mine', {
      method: 'POST',
      body: { days: 14, min_count: 3 },
      timeout: 60000,
    });
    const result = {
      scanned: asNumber(data.scanned) == null ? 0 : Number(data.scanned),
      candidates: asNumber(data.candidates) == null ? 0 : Number(data.candidates),
      added: asNumber(data.added) == null ? 0 : Number(data.added),
      skipped: asNumber(data.skipped) == null ? 0 : Number(data.skipped),
    };
    app.lexicon.mineResult = result;
    const refs = app.lexicon.refs;
    if (refs && refs.mineResult) {
      refs.mineResult.hidden = false;
      refs.mineResult.textContent = lexiconMineText(result);
    }
    toast(lexiconMineText(result).replace('上次挖掘：', '翻完了：'), result.added ? 'success' : 'info');
    runLexiconLoad(false);
  } catch (error) {
    toast('没能翻成：' + friendlyError(error), 'error');
  } finally {
    if (button.isConnected) setButtonBusy(button, false);
  }
}

async function runLexiconLoad(showSkeleton) {
  const refs = app.lexicon.refs;
  if (!refs || app.route !== 'lexicon') return;
  const seq = ++app.lexicon.seq;

  if (showSkeleton || !refs.resultList.children.length) {
    refs.resultList.replaceChildren(skeletonList(4));
  }
  refs.resultSub.textContent = '正在读取词条…';
  refs.resultMeta.replaceChildren(el('span', { class: 'search-spinner' },
    el('span', { class: 'spinner', 'aria-hidden': 'true' }),
    el('span', { text: '读取中' }),
  ));

  const params = new URLSearchParams();
  params.set('status', app.lexicon.status === 'all' ? 'all' : app.lexicon.status);
  params.set('limit', String(LEXICON_LIMIT));
  const query = app.lexicon.query.trim();
  if (query) params.set('query', query);

  try {
    const data = await fetchJSON('/api/lexicon?' + params.toString(), {
      signal: app.viewAbort ? app.viewAbort.signal : undefined,
    });
    if (seq !== app.lexicon.seq || app.route !== 'lexicon' || !app.lexicon.refs) return;
    setGlobalOnline(true);
    updateLexiconStats(data.stats);

    const rawItems = Array.isArray(data.items) ? data.items : [];
    const kind = app.lexicon.kind;
    const items = kind === 'all'
      ? rawItems
      : rawItems.filter((item) => String(item && item.kind || '').toLowerCase() === kind);

    refs.resultList.replaceChildren();
    refs.resultSub.textContent = lexiconResultSummary(items.length);
    refs.resultMeta.replaceChildren(
      badge('共 ' + fmtNum(items.length, '0') + ' 条', 'tone-muted'),
    );

    if (!items.length) {
      refs.resultList.append(lexiconEmptyState(query));
      return;
    }

    for (const item of items) {
      refs.resultList.append(buildLexiconItem(item));
    }
  } catch (error) {
    if (!error || error.code === 'aborted' || seq !== app.lexicon.seq || !app.lexicon.refs) return;
    refs.resultList.replaceChildren(errorPanel('表达库加载失败', error, () => runLexiconLoad(true)));
    refs.resultSub.textContent = '读取失败。';
    refs.resultMeta.replaceChildren();
  }
}

function updateLexiconStats(stats) {
  const refs = app.lexicon.refs;
  if (!refs || !stats || typeof stats !== 'object') return;
  for (const key of ['candidate', 'approved', 'disabled', 'total']) {
    if (refs.stats[key]) setText(refs.stats[key], fmtNum(stats[key], '0'));
  }
}

function lexiconResultSummary(count) {
  const parts = [];
  parts.push(app.lexicon.status === 'all' ? '全部' : lexiconStatusLabel(app.lexicon.status));
  if (app.lexicon.kind !== 'all') parts.push(lexiconKindLabel(app.lexicon.kind));
  const query = app.lexicon.query.trim();
  if (query) parts.push('搜“' + truncate(query, 20) + '”');
  return parts.join(' · ') + ' · 共 ' + fmtNum(count, '0') + ' 条。';
}

function lexiconEmptyState(query) {
  if (query) {
    return emptyPanel('没有找到匹配的表达', '换个短一点的词，或者清空搜索看看全部。');
  }
  if (app.lexicon.status === 'candidate') {
    return emptyPanel('最近没有新候选', '等她多聊几句，或者从最近对话里挖一挖。');
  }
  if (app.lexicon.status === 'approved') {
    return emptyPanel('还没有启用的表达', '通过一个候选，她才会在聊天里偶尔用。');
  }
  if (app.lexicon.status === 'disabled') {
    return emptyPanel('没有停用的表达', '不喜欢的说法点「不要」，会收在这里。');
  }
  return emptyPanel('词条库还是空的', '先手动加一条，或者从最近对话里挖一挖。');
}

function buildLexiconItem(item) {
  const safe = item && typeof item === 'object' ? item : {};
  const kind = String(safe.kind || 'expression').toLowerCase();
  const status = String(safe.status || 'candidate').toLowerCase();
  const card = el('article', { class: 'lexicon-item kind-' + kind + ' status-' + status });

  const head = el('div', { class: 'lexicon-item-head' });
  head.append(el('h3', { class: 'lexicon-content', text: String(safe.content || '未命名词条') }));
  head.append(el('span', {
    class: 'lexicon-usage',
    text: '用过 ' + fmtNum(safe.usage_count, '0') + ' 次',
  }));
  card.append(head);

  const badges = el('div', { class: 'chip-row lexicon-badges' },
    badge('类型 · ' + lexiconKindLabel(kind), kind === 'jargon' ? 'tone-info' : 'tone-ok'),
    badge(lexiconStatusLabel(status), lexiconStatusTone(status)),
    badge('来源 · ' + lexiconSourceLabel(safe.source), 'tone-muted'),
  );
  card.append(badges);

  const meaning = String(safe.meaning || '').trim();
  if (meaning) card.append(el('p', { class: 'lexicon-meaning', text: meaning }));

  const situation = String(safe.situation || '').trim();
  if (situation) card.append(el('p', { class: 'lexicon-situation', text: situation }));

  const style = lexiconStyleLabel(safe.style);
  const tags = safeArray(safe.tags).filter((tag) => typeof tag === 'string' && tag.trim());
  if (style || tags.length) {
    const extra = el('div', { class: 'chip-row lexicon-extra' });
    if (style) extra.append(chip('语气 · ' + style));
    tags.slice(0, 8).forEach((tag) => extra.append(chip('#' + tag, 'chip-muted')));
    card.append(extra);
  }

  const confidence = clamp(asNumber(safe.confidence) == null ? 0 : Number(safe.confidence), 0, 1);
  const confidencePct = Math.round(confidence * 100);
  card.append(el('div', { class: 'lexicon-confidence' },
    el('span', { text: '把握' }),
    el('span', {
      class: 'lexicon-confidence-track',
      role: 'progressbar',
      'aria-label': '把握',
      'aria-valuemin': '0',
      'aria-valuemax': '100',
      'aria-valuenow': String(confidencePct),
    },
    el('span', { class: 'lexicon-confidence-fill', style: { width: confidencePct + '%' } }),
    ),
    el('span', { class: 'lexicon-confidence-value', text: confidencePct + '%' }),
  ));

  const actions = el('div', { class: 'lexicon-actions' });
  if (status === 'candidate') {
    const approve = el('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      text: '通过',
      onClick: () => reviewLexiconItem(safe, 'approved', approve),
    });
    const skip = el('button', {
      class: 'btn btn-ghost btn-sm',
      type: 'button',
      text: '不要',
      onClick: () => reviewLexiconItem(safe, 'disabled', skip),
    });
    actions.append(approve, skip);
  } else if (status === 'approved') {
    const disable = el('button', {
      class: 'btn btn-ghost btn-sm',
      type: 'button',
      text: '停用',
      onClick: () => reviewLexiconItem(safe, 'disabled', disable),
    });
    actions.append(disable);
  } else {
    const enable = el('button', {
      class: 'btn btn-ghost btn-sm',
      type: 'button',
      text: '重新启用',
      onClick: () => reviewLexiconItem(safe, 'approved', enable),
    });
    actions.append(enable);
  }
  card.append(actions);

  return card;
}

async function reviewLexiconItem(item, status, button) {
  if (!item || !item.id) return;
  const content = truncate(String(item.content || ''), 20);
  setButtonBusy(button, true, '保存中…');
  try {
    await fetchJSON('/api/lexicon/review', {
      method: 'POST',
      body: { id: item.id, status },
      timeout: 15000,
    });
    if (status === 'approved') {
      toast('已通过「' + content + '」，她会偶尔用了。', 'success');
    } else if (status === 'disabled') {
      toast('已停用「' + content + '」，她不会再用它。', 'info');
    } else {
      toast('已重新启用「' + content + '」。', 'success');
    }
    runLexiconLoad(false);
  } catch (error) {
    toast('没能保存：' + friendlyError(error), 'error');
    if (button && button.isConnected) setButtonBusy(button, false);
  }
}

async function handleLexiconAdd(event) {
  event.preventDefault();
  const refs = app.lexicon.refs;
  if (!refs || !refs.addForm) return;
  const content = refs.addContent.value.trim();
  if (!content) {
    refs.addError.textContent = '内容不能为空，写一句想让她学会的话。';
    refs.addContent.focus();
    return;
  }
  refs.addError.textContent = '';
  const rawTags = String(refs.addTags.value || '').split(/[,，、]+/);
  const tags = Array.from(new Set(rawTags.map((tag) => tag.trim()).filter(Boolean))).slice(0, 8);
  const body = {
    kind: refs.addKind.value || 'expression',
    content,
    meaning: refs.addMeaning.value.trim(),
    situation: refs.addSituation.value.trim(),
    style: refs.addStyle.value,
    tags,
    status: 'candidate',
  };

  setButtonBusy(refs.addSubmit, true, '放入中…');
  try {
    const data = await fetchJSON('/api/lexicon', {
      method: 'POST',
      body,
      timeout: 15000,
    });
    const added = data.item && data.item.content ? String(data.item.content) : content;
    toast('已放进候选：' + truncate(added, 24), 'success');
    refs.addForm.reset();
    refs.addError.textContent = '';
    refs.addDetails.open = false;

    app.lexicon.status = 'candidate';
    app.lexicon.kind = 'all';
    app.lexicon.query = '';
    refs.search.value = '';
    syncLexiconChips(refs.statusChips, 'candidate');
    syncLexiconChips(refs.kindChips, 'all');
    refs.clearSearch.hidden = true;
    runLexiconLoad(true);
  } catch (error) {
    refs.addError.textContent = friendlyError(error);
  } finally {
    if (refs.addSubmit && refs.addSubmit.isConnected) setButtonBusy(refs.addSubmit, false);
  }
}

/* ============================================================================
   11.5 QQ动态视图（草稿审批 + 最近动态）
   ========================================================================== */

const QZONE_POST_STATUS = {
  draft: { label: '待审', tone: 'tone-warn' },
  approved: { label: '已批准', tone: 'tone-info' },
  rejected: { label: '已拒绝', tone: 'tone-muted' },
  posted: { label: '已发布', tone: 'tone-ok' },
  failed: { label: '发布失败', tone: 'tone-danger' },
  blocked: { label: '已拦截', tone: 'tone-danger' },
};

function qzoneStatusLabel(status) {
  const entry = QZONE_POST_STATUS[String(status || '').toLowerCase()];
  return entry ? entry.label : String(status || '未知');
}

function qzoneStatusTone(status) {
  const entry = QZONE_POST_STATUS[String(status || '').toLowerCase()];
  return entry ? entry.tone : 'tone-muted';
}

function renderQzoneView(container) {
  const refs = {
    drafts: null,
    draftsSub: null,
    feed: null,
    feedSub: null,
    stats: {},
    refresh: null,
  };
  app.qzone.refs = refs;

  const refresh = el('button', { class: 'btn btn-ghost', type: 'button', text: '刷新' });
  refs.refresh = refresh;
  refresh.addEventListener('click', () => runQzoneLoad(true));

  const head = viewHeader(
    'QQ动态',
    '她的小黑板',
    '她想发到空间的说说先排在这里：你点了通过才会出门，没点头就一直在草稿箱里。',
    [refresh],
  );

  const statGrid = el('div', { class: 'stat-grid lexicon-stats' });
  for (const [key, label] of [
    ['drafts', '待审草稿'],
    ['postedToday', '今日已发'],
    ['maxPerDay', '每日上限'],
    ['write', '写路径'],
  ]) {
    const value = el('div', { class: 'stat-value', text: '—' });
    refs.stats[key] = value;
    statGrid.append(statTile(label, value));
  }

  const draftsCard = el(
    'section',
    { class: 'card' },
    cardHead('草稿队列', '通过后按每日限额排队发布；拒绝就永远不发。'),
    el('p', { class: 'card-sub', text: '正在读取草稿…' }),
    el('div', { class: 'lexicon-list' }),
  );
  refs.draftsSub = draftsCard.children[1];
  refs.drafts = draftsCard.children[2];

  const feedCard = el(
    'section',
    { class: 'card' },
    cardHead('最近动态', '已经发出去的和刷到的，都只在这台电脑里。'),
    el('p', { class: 'card-sub', text: '正在读取动态…' }),
    el('div', { class: 'qzone-feed' }),
  );
  refs.feedSub = feedCard.children[1];
  refs.feed = feedCard.children[2];

  container.append(head, statGrid, draftsCard, feedCard);

  runQzoneLoad(true);
}

function qzoneWriteText(write) {
  if (!write || typeof write !== 'object') return '—';
  if (!write.require_approval) {
    return '开 · 免审';
  }
  return write.write_enabled ? '开 · 需审批' : '关';
}

function runQzoneLoad(force) {
  const refs = app.qzone.refs;
  if (!refs) return;
  if (force && refs.refresh) setButtonBusy(refs.refresh, true, '加载中…');
  Promise.all([
    fetchJSON('/api/qzone/posts?limit=50', { signal: app.viewAbort ? app.viewAbort.signal : undefined }),
    fetchJSON('/api/qzone/feed?limit=10', { signal: app.viewAbort ? app.viewAbort.signal : undefined }),
  ])
    .then(([posts, feed]) => {
      if (!app.qzone.refs) return;
      app.qzone.data = posts;
      app.qzone.feed = feed;
      updateQzoneStats(posts);
      renderQzoneDrafts(posts);
      renderQzoneFeed(feed);
      if (refs.refresh && refs.refresh.isConnected) setButtonBusy(refs.refresh, false);
    })
    .catch((error) => {
      if (!app.qzone.refs) return;
      if (error && (error.code === 'aborted' || error.name === 'AbortError')) return;
      if (refs.drafts) {
        refs.drafts.replaceChildren(errorPanel('动态加载失败', error, () => runQzoneLoad(true)));
      }
      if (refs.draftsSub) refs.draftsSub.textContent = '读取失败。';
      if (refs.refresh && refs.refresh.isConnected) setButtonBusy(refs.refresh, false);
    });
}

function updateQzoneStats(data) {
  const refs = app.qzone.refs;
  if (!refs || !data) return;
  const quota = data.quota && typeof data.quota === 'object' ? data.quota : {};
  const write = data.write && typeof data.write === 'object' ? data.write : {};
  if (refs.stats.drafts) setText(refs.stats.drafts, fmtNum(write.drafts, '0'));
  if (refs.stats.postedToday) setText(refs.stats.postedToday, fmtNum(quota.posted_today, '0'));
  if (refs.stats.maxPerDay) setText(refs.stats.maxPerDay, fmtNum(quota.max_posts_per_day, '0'));
  if (refs.stats.write) setText(refs.stats.write, qzoneWriteText(write));
}

function renderQzoneDrafts(data) {
  const refs = app.qzone.refs;
  if (!refs || !refs.drafts) return;
  const items = safeArray(data && data.drafts);
  refs.draftsSub.textContent = items.length
    ? '待你处理的草稿 ' + items.length + ' 条。'
    : '草稿箱是空的。她在聊天里说出想发的话时，会先出现在这里。';
  if (!items.length) {
    refs.drafts.replaceChildren(emptyPanel('没有待审的草稿', '她想发说说时会先写进这里，不会自己发出去。'));
    return;
  }
  refs.drafts.replaceChildren(...items.map((item) => buildQzonePostCard(item)));
}

function renderQzoneFeed(data) {
  const refs = app.qzone.refs;
  if (!refs || !refs.feed) return;
  const items = safeArray(data && data.items);
  refs.feedSub.textContent = items.length ? '最近 ' + items.length + ' 条动态。' : '还没有动态记录。';
  if (!items.length) {
    refs.feed.replaceChildren(emptyPanel('还没有动态', '她在聊天里用 post_qzone 提交草稿，你通过后会出现在这里。'));
    return;
  }
  const list = el('div', { class: 'qzone-feed' });
  for (const item of items) {
    const author = String(item.author_name || '').trim();
    const content = String(item.content || '').trim();
    const when = fmtWhen(item.published_at || item.seen_at);
    list.append(
      el('div', { class: 'qzone-feed-item' },
        el('div', { class: 'qzone-feed-head' },
          el('span', { class: 'qzone-feed-author', text: author || '主人' }),
          el('span', { class: 'qzone-feed-time', text: when }),
        ),
        el('p', { class: 'qzone-feed-content', text: content || '（无文字）' }),
      ),
    );
  }
  refs.feed.replaceChildren(list);
}function buildQzonePostCard(item) {
  const safe = item && typeof item === 'object' ? item : {};
  const status = String(safe.status || 'draft').toLowerCase();
  const card = el('article', { class: 'lexicon-item status-' + status });

  const head = el('div', { class: 'lexicon-item-head' });
  head.append(el('h3', { class: 'lexicon-content', text: String(safe.content || '（空草稿）') }));
  head.append(el('span', {
    class: 'lexicon-usage',
    text: fmtDate(safe.created_at),
  }));
  card.append(head);

  card.append(el('div', { class: 'chip-row lexicon-badges' },
    badge(qzoneStatusLabel(status), qzoneStatusTone(status)),
    badge('来源 · ' + (String(safe.created_by) === 'owner' ? '主人' : '她自己'), 'tone-muted'),
  ));

  if (safe.error) {
    card.append(el('p', { class: 'lexicon-meaning', text: '原因：' + String(safe.error) }));
  }

  const actions = el('div', { class: 'lexicon-actions' });
  if (status === 'draft') {
    const approve = el('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      text: '通过',
      onClick: () => reviewQzonePost(safe, 'approved', approve),
    });
    const reject = el('button', {
      class: 'btn btn-ghost btn-sm',
      type: 'button',
      text: '拒绝',
      onClick: () => reviewQzonePost(safe, 'rejected', reject),
    });
    actions.append(approve, reject);
  }
  card.append(actions);
  return card;
}

async function reviewQzonePost(item, decision, button) {
  if (!item || item.id == null) return;
  setButtonBusy(button, true, '保存中…');
  try {
    const data = await fetchJSON('/api/qzone/review', {
      method: 'POST',
      body: { id: item.id, decision },
      timeout: 15000,
    });
    if (decision === 'approved') {
      if (data && data.ok) {
        toast('已通过，她会按每日限额排队发布。', 'success');
      } else {
        toast('今天的名额已经用完了，草稿留在队列里，明天再点通过吧。', 'info');
      }
    } else {
      toast('已拒绝，这条说说不会发出去。', 'info');
    }
    runQzoneLoad(false);
  } catch (error) {
    toast('没能保存：' + friendlyError(error), 'error');
    if (button && button.isConnected) setButtonBusy(button, false);
  }
}

/* ============================================================================
   11.6 人物视图（人物卡：搜索 / 新增 / 改名 / 别名 / 合并）
   ========================================================================== */

function splitPeopleAliases(raw) {
  return String(raw || '')
    .split(/[,，、;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function peopleAction(url, body, successText) {
  try {
    const data = await fetchJSON(url, { method: 'POST', body });
    if (successText) {
      toast(typeof successText === 'function' ? successText(data) : successText);
    }
    runPeopleLoad();
    return true;
  } catch (error) {
    if (!(error && error.code === 'aborted')) toast(friendlyError(error), 'error');
    return false;
  }
}

function renamePerson(card) {
  const next = window.prompt('给「' + (card.name || '这个人') + '」改个名字：', card.name || '');
  if (next == null) return;
  const name = next.trim();
  if (!name) {
    toast('名字不能是空的。', 'error');
    return;
  }
  if (name === card.name) return;
  peopleAction('/api/people/update', { id: card.id, name }, '改好了，现在叫「' + name + '」。');
}

function addAliasPerson(card) {
  const next = window.prompt('给「' + (card.name || '这个人') + '」加别名（多个用逗号分开）：', '');
  if (next == null) return;
  const aliases = splitPeopleAliases(next);
  if (!aliases.length) {
    toast('别名不能是空的。', 'error');
    return;
  }
  peopleAction('/api/people/update', { id: card.id, aliases_add: aliases }, '别名记下了。');
}

function mergePerson(card) {
  const who = card.name || card.id;
  const next = window.prompt(
    '把「' + who + '」合并到哪个人？输入对方的姓名、别名或 QQ 号。' +
      '合并后这张卡的提到记录会归到对方名下，名字会变成对方的别名。',
    '',
  );
  if (next == null) return;
  const target = next.trim();
  if (!target) {
    toast('要填对方的名字才行。', 'error');
    return;
  }
  peopleAction('/api/people/merge', { source: card.id, target }, (data) => (
    data && data.merged
      ? '已经合并到「' + ((data.person && data.person.name) || target) + '」。'
      : '这本来就是同一张卡，没有动。'
  ));
}

function personCardNode(card) {
  const data = card && typeof card === 'object' ? card : {};
  const id = String(data.id || '');
  const name = String(data.name || '');
  const aliases = safeArray(data.aliases).map((item) => String(item));
  const mergedFrom = safeArray(data.merged_from).map((item) => String(item));

  const headRow = el('div', { class: 'person-head' },
    el('div', { class: 'person-id' },
      el('span', { class: 'person-name', text: name || '（未命名）' }),
      data.uin_masked ? el('span', { class: 'person-uin', text: String(data.uin_masked) }) : null,
    ),
    el('div', { class: 'person-actions' },
      el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '改名', onClick: () => renamePerson({ id, name }) }),
      el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '加别名', onClick: () => addAliasPerson({ id, name }) }),
      el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '合并到…', onClick: () => mergePerson({ id, name }) }),
    ),
  );

  const metaRow = el('div', { class: 'person-meta' },
    el('span', { class: 'person-mentions', text: '提到 ' + fmtNum(data.mentions, '0') + ' 次' }),
    data.relation ? chip('关系 · ' + String(data.relation)) : null,
    data.last_seen_at ? el('span', { class: 'person-seen', text: '最近 ' + fmtDate(data.last_seen_at) }) : null,
  );

  const cardNode = el('article', { class: 'person-card' }, headRow, metaRow);
  if (aliases.length) {
    cardNode.append(el('div', { class: 'person-aliases' },
      el('span', { class: 'person-alias-label', text: '别名' }),
      aliases.map((alias) => chip(alias)),
    ));
  }
  // 合并来源的 id 里可能带 QQ 号，界面上只报张数，不把原 id 写出来。
  if (mergedFrom.length) {
    cardNode.append(el('div', { class: 'person-merged', text: '合并自 ' + fmtNum(mergedFrom.length, '0') + ' 张旧卡' }));
  }
  return cardNode;
}

function peopleDuplicatePairs(people) {
  const byName = new Map();
  const cards = new Map();
  for (const card of safeArray(people)) {
    if (!card || !card.id) continue;
    cards.set(String(card.id), card);
    const names = new Set([String(card.name || '').trim()]);
    for (const alias of safeArray(card.aliases)) names.add(String(alias).trim());
    for (const name of names) {
      const key = name.toLowerCase();
      if (!key) continue;
      if (!byName.has(key)) byName.set(key, new Set());
      byName.get(key).add(String(card.id));
    }
  }
  const pairs = [];
  const seen = new Set();
  for (const ids of byName.values()) {
    if (ids.size < 2) continue;
    const list = Array.from(ids).sort();
    for (let i = 0; i < list.length; i += 1) {
      for (let j = i + 1; j < list.length; j += 1) {
        const key = list[i] + '||' + list[j];
        if (seen.has(key)) continue;
        seen.add(key);
        const a = cards.get(list[i]);
        const b = cards.get(list[j]);
        if (a && b) pairs.push({ a, b });
      }
    }
  }
  return pairs.slice(0, 12);
}

function renderPeopleDuplicates(people) {
  const refs = app.people.refs;
  if (!refs || !refs.duplicates || !refs.duplicatesCard) return;
  const pairs = app.people.query ? [] : peopleDuplicatePairs(people);
  if (!pairs.length) {
    refs.duplicatesCard.hidden = true;
    refs.duplicates.replaceChildren();
    return;
  }
  refs.duplicatesCard.hidden = false;
  refs.duplicates.replaceChildren(...pairs.map((pair) => {
    const aName = pair.a.name || pair.a.id;
    const bName = pair.b.name || pair.b.id;
    return el('div', { class: 'people-dup-row' },
      el('span', { class: 'people-dup-text', text: '「' + aName + '」和「' + bName + '」共用同一个名字' }),
      el('button', {
        class: 'btn btn-ghost btn-sm',
        type: 'button',
        text: '合并',
        onClick: () => {
          if (!window.confirm('把「' + aName + '」合并进「' + bName + '」？提到记录会归到「' + bName + '」名下。')) return;
          peopleAction('/api/people/merge', { source: pair.a.id, target: pair.b.id }, '已经合并。');
        },
      }),
    );
  }));
}

function renderPeopleList(data) {
  const refs = app.people.refs;
  if (!refs || !refs.list) return;
  const people = safeArray(data && data.people);
  const rawCount = asNumber(data && data.count);
  const count = rawCount == null ? people.length : Number(rawCount);

  if (refs.listMeta) {
    refs.listMeta.replaceChildren(badge(
      (app.people.query ? '找到 ' : '共 ') + fmtNum(count, '0') + ' 人',
      app.people.query ? 'tone-info' : 'tone-muted',
    ));
  }
  if (refs.listSub) {
    refs.listSub.textContent = app.people.query
      ? '“' + truncate(app.people.query, 40) + '” 的匹配结果。'
      : '最近活跃的人排在前面。';
  }
  if (!people.length) {
    refs.list.replaceChildren(emptyPanel(
      app.people.query ? '没有匹配的人物' : '还没有人物卡',
      app.people.query ? '换个名字、别名或 QQ 号试试。' : '在上面记下第一个人的名字，她就开始认识他们了。',
    ));
  } else {
    refs.list.replaceChildren(...people.map(personCardNode));
  }
  renderPeopleDuplicates(people);
}

async function runPeopleLoad(showSkeleton) {
  const refs = app.people.refs;
  if (!refs || app.route !== 'people') return;
  const seq = ++app.people.seq;

  if (showSkeleton || !refs.list || !refs.list.children.length) {
    if (refs.list) refs.list.replaceChildren(skeletonList(3, 'skeleton-card'));
  }
  if (refs.listSub) refs.listSub.textContent = '正在读取人物…';
  if (refs.listMeta) {
    refs.listMeta.replaceChildren(el('span', { class: 'search-spinner' },
      el('span', { class: 'spinner', 'aria-hidden': 'true' }), el('span', { text: '读取中' })));
  }

  const params = new URLSearchParams();
  params.set('query', app.people.query);
  params.set('limit', '200');
  const url = '/api/people?' + params.toString();

  try {
    const data = await fetchJSON(url, { signal: app.viewAbort ? app.viewAbort.signal : undefined });
    if (seq !== app.people.seq || app.route !== 'people' || !app.people.refs) return;
    setGlobalOnline(true);
    renderPeopleList(data);
  } catch (error) {
    if (seq !== app.people.seq || app.route !== 'people' || !app.people.refs) return;
    if (error && (error.code === 'aborted' || error.name === 'AbortError')) return;
    setGlobalOnline(false);
    if (refs.list) refs.list.replaceChildren(errorPanel('人物列表加载失败', error, () => runPeopleLoad(true)));
    if (refs.listSub) refs.listSub.textContent = '读取失败。';
  }
}

function renderPeopleView(container) {
  const refs = {
    search: null,
    clearSearch: null,
    list: null,
    listSub: null,
    listMeta: null,
    duplicates: null,
    duplicatesCard: null,
  };
  app.people.refs = refs;

  // 搜索：回车立即查，输入防抖 300ms（与记忆视图同一套写法）。
  const searchInput = el('input', {
    class: 'input',
    type: 'search',
    id: 'people-search',
    placeholder: '按名字、别名或 QQ 号找…',
    autocomplete: 'off',
    value: app.people.query,
    'aria-label': '搜索人物',
  });
  refs.search = searchInput;

  const clearSearch = el('button', {
    class: 'search-clear',
    type: 'button',
    'aria-label': '清空搜索',
    hidden: !app.people.query,
    onClick: () => {
      searchInput.value = '';
      app.people.query = '';
      clearSearch.hidden = true;
      if (app.people.debounce) window.clearTimeout(app.people.debounce);
      runPeopleLoad(true);
      searchInput.focus();
    },
  }, iconSvg('icon-close'));
  refs.clearSearch = clearSearch;

  const searchForm = el('form', {
    class: 'search-form',
    role: 'search',
    onSubmit: (event) => {
      event.preventDefault();
      if (app.people.debounce) window.clearTimeout(app.people.debounce);
      app.people.query = searchInput.value.trim();
      clearSearch.hidden = !searchInput.value;
      runPeopleLoad(true);
    },
  },
    el('div', { class: 'field search-field' },
      el('label', { class: 'field-label', for: 'people-search', text: '搜索人物' }),
      el('div', { class: 'search-box' },
        iconSvg('icon-search', 'search-icon'),
        searchInput,
        clearSearch,
      ),
    ),
    el('button', { class: 'btn btn-primary', type: 'submit', text: '找一找' }),
  );

  searchInput.addEventListener('input', () => {
    clearSearch.hidden = !searchInput.value;
    if (app.people.debounce) window.clearTimeout(app.people.debounce);
    app.people.debounce = window.setTimeout(() => {
      app.people.query = searchInput.value.trim();
      runPeopleLoad();
    }, 300);
  });

  // 新增人物表单：QQ 号选填，落库后界面上只出现打码版本。
  const nameInput = el('input', { class: 'input', type: 'text', id: 'people-add-name', placeholder: '她怎么称呼这个人', autocomplete: 'off', maxlength: '64' });
  const uinInput = el('input', { class: 'input', type: 'text', id: 'people-add-uin', placeholder: '选填，只存在本机', autocomplete: 'off', inputmode: 'numeric', maxlength: '24' });
  const relationInput = el('input', { class: 'input', type: 'text', id: 'people-add-relation', placeholder: '选填，比如「朋友」', autocomplete: 'off', maxlength: '32' });
  const aliasInput = el('input', { class: 'input', type: 'text', id: 'people-add-alias', placeholder: '选填，多个用逗号分开', autocomplete: 'off' });
  const remarkInput = el('input', { class: 'input', type: 'text', id: 'people-add-remark', placeholder: '选填，一句话备注', autocomplete: 'off', maxlength: '120' });
  const addSubmit = el('button', { class: 'btn btn-primary', type: 'submit', text: '记下这个人' });

  const addForm = el('form', {
    class: 'people-add-form',
    novalidate: true,
    onSubmit: async (event) => {
      event.preventDefault();
      if (app.people.busy === 'add') return;
      const name = nameInput.value.trim();
      const uin = uinInput.value.trim();
      if (!name && !uin) {
        toast('姓名和 QQ 号至少填一个。', 'error');
        return;
      }
      app.people.busy = 'add';
      setButtonBusy(addSubmit, true, '记录中…');
      try {
        const data = await fetchJSON('/api/people/add', {
          method: 'POST',
          body: {
            name,
            uin,
            aliases: splitPeopleAliases(aliasInput.value),
            relation: relationInput.value.trim() || null,
            remark: remarkInput.value.trim() || null,
          },
        });
        const person = data.person || {};
        const label = person.name || name || uin;
        toast(data.created ? '已记下「' + label + '」。' : '「' + label + '」已经有卡片了，帮你补上了这次的写法。');
        nameInput.value = '';
        uinInput.value = '';
        relationInput.value = '';
        aliasInput.value = '';
        remarkInput.value = '';
        runPeopleLoad();
      } catch (error) {
        if (!(error && error.code === 'aborted')) toast(friendlyError(error), 'error');
      } finally {
        app.people.busy = '';
        setButtonBusy(addSubmit, false);
      }
    },
  },
    el('div', { class: 'people-form-grid' },
      el('div', { class: 'field' },
        el('label', { class: 'field-label', for: 'people-add-name', text: '姓名' }),
        nameInput,
      ),
      el('div', { class: 'field' },
        el('label', { class: 'field-label', for: 'people-add-uin', text: 'QQ 号' }),
        uinInput,
      ),
      el('div', { class: 'field' },
        el('label', { class: 'field-label', for: 'people-add-relation', text: '关系' }),
        relationInput,
      ),
      el('div', { class: 'field' },
        el('label', { class: 'field-label', for: 'people-add-alias', text: '别名' }),
        aliasInput,
      ),
      el('div', { class: 'field field-wide' },
        el('label', { class: 'field-label', for: 'people-add-remark', text: '备注' }),
        remarkInput,
      ),
    ),
    el('div', { class: 'people-add-actions' }, addSubmit),
  );

  const head = viewHeader(
    '人物',
    '她认识的人',
    '每张卡片只记名字、关系和被提到的次数；QQ 号永远只显示打码的样子。',
  );

  refs.listMeta = el('div', { class: 'result-summary' });
  refs.listSub = el('p', { class: 'card-sub', text: '正在读取人物…' });
  const list = el('div', { class: 'people-list', 'aria-live': 'polite' });
  refs.list = list;

  const listCard = el('section', { class: 'card people-results' },
    el('div', { class: 'card-head' },
      el('div', { class: 'card-head-copy' },
        el('h2', { class: 'card-title', text: '人物卡' }),
        refs.listSub,
      ),
      el('div', { class: 'card-actions' }, refs.listMeta),
    ),
    list,
  );

  const addCard = el('section', { class: 'card people-add' },
    cardHead('新增人物', '写下名字就好；QQ 号只留在这台电脑里，界面上永远只显示打码版本。'),
    addForm,
  );

  const duplicates = el('div', { class: 'people-dups' });
  refs.duplicates = duplicates;
  const duplicatesCard = el('section', { class: 'card people-duplicates', hidden: true },
    cardHead('疑似同一人', '两张卡共用同一个名字时提醒一下；合不合并由你决定，点了才动。'),
    duplicates,
  );
  refs.duplicatesCard = duplicatesCard;

  const page = el('div', { class: 'view-page people-page' },
    head,
    el('div', { class: 'people-toolbar' }, searchForm),
    addCard,
    listCard,
    duplicatesCard,
  );
  container.append(page);

  addCleanup(() => {
    if (app.people.debounce) {
      window.clearTimeout(app.people.debounce);
      app.people.debounce = null;
    }
  });

  runPeopleLoad(true);
}

/* ============================================================================
   12. 轨迹视图
   ========================================================================== */

function renderTracesView(container) {
  const refs = {
    summary: null,
    list: null,
    decisions: null,
    wakeups: null,
    outbox: null,
    updated: null,
    pollNote: null,
    refreshButton: null,
    loading: false,
  };
  app.traces.refs = refs;
  app.traces.expanded = new Set();
  app.traces.sig = '';

  const updated = el('span', { class: 'updated-label', text: '尚未更新' });
  refs.updated = updated;
  const pollNote = el('span', { class: 'updated-label', text: '' });
  refs.pollNote = pollNote;
  const refreshButton = el('button', { class: 'btn btn-ghost', type: 'button', text: '刷新' });
  refs.refreshButton = refreshButton;
  refreshButton.addEventListener('click', () => refreshTracesAll(refreshButton));

  const head = viewHeader(
    '轨迹',
    '她走过的痕迹',
    '每一轮对话的耗时、Token、风险和动作，都安静地留在这里。',
    [pollNote, updated, refreshButton],
  );

  const summary = el('div', { class: 'trace-summary-grid' });
  refs.summary = summary;

  const list = el('div', { class: 'trace-list' });
  refs.list = list;

  const decisions = el('div');
  refs.decisions = decisions;
  const wakeups = el('div');
  refs.wakeups = wakeups;
  const outbox = el('div');
  refs.outbox = outbox;

  const page = el('div', { class: 'view-page traces-stack' },
    head,
    el('section', { class: 'card' },
      cardHead('最近轨迹', '点开任意一条，看看那一轮的完整记录。'),
      summary,
      el('div', { class: 'traces-stack' }, list),
    ),
    el('section', { class: 'card' },
      cardHead('最近决策', '规则与模型做出的选择。'),
      decisions,
    ),
    el('div', { class: 'two-col' },
      el('section', { class: 'card' },
        cardHead('唤醒任务', '她准备主动找你的时候。'),
        wakeups,
      ),
      el('section', { class: 'card' },
        cardHead('待发件箱', '还没送出去的消息。'),
        outbox,
      ),
    ),
  );
  container.append(page);

  summary.append(skeletonList(1, 'skeleton-row'));
  list.append(skeletonList(3, 'skeleton-card'));
  decisions.append(skeletonList(3, 'skeleton-row'));
  wakeups.append(skeletonList(3, 'skeleton-row'));
  outbox.append(skeletonList(3, 'skeleton-row'));

  loadTracesInitial();
}

async function loadTracesInitial() {
  const refs = app.traces.refs;
  if (!refs) return;
  const signal = app.viewAbort ? app.viewAbort.signal : undefined;
  const results = await Promise.allSettled([
    fetchJSON('/api/traces?limit=30', { signal }),
    fetchJSON('/api/decisions?limit=30', { signal }),
    fetchJSON('/api/wakeups?status=&limit=30', { signal }),
    fetchJSON('/api/outbox?limit=30', { signal }),
  ]);
  if (!app.traces.refs || app.route !== 'traces') return;

  const [traceResult, decisionResult, wakeResult, outboxResult] = results;

  if (traceResult.status === 'fulfilled') {
    app.traces.data = traceResult.value;
    setGlobalOnline(true);
    renderTraceSummaryAndList();
    if (app.route === 'traces') startPoll(pollTraces, 8000);
  } else if (traceResult.reason && traceResult.reason.code !== 'aborted') {
    refs.list.replaceChildren(errorPanel('轨迹加载失败', traceResult.reason, loadTracesInitial));
    refs.summary.replaceChildren();
  }

  if (decisionResult.status === 'fulfilled') {
    app.traces.decisions = decisionResult.value;
    renderDecisionsSection();
  } else if (decisionResult.reason && decisionResult.reason.code !== 'aborted') {
    refs.decisions.replaceChildren(errorPanel('决策加载失败', decisionResult.reason, loadTracesInitial));
  }

  if (wakeResult.status === 'fulfilled') {
    app.traces.wakeups = wakeResult.value;
    renderWakeupsSection();
  } else if (wakeResult.reason && wakeResult.reason.code !== 'aborted') {
    refs.wakeups.replaceChildren(errorPanel('唤醒任务加载失败', wakeResult.reason, loadTracesInitial));
  }

  if (outboxResult.status === 'fulfilled') {
    app.traces.outbox = outboxResult.value;
    renderOutboxSection();
  } else if (outboxResult.reason && outboxResult.reason.code !== 'aborted') {
    refs.outbox.replaceChildren(errorPanel('发件箱加载失败', outboxResult.reason, loadTracesInitial));
  }

  if (refs.updated) {
    refs.updated.textContent = '同步于 ' + fmtTime(Date.now());
    refs.updated.title = fmtDate(Date.now());
  }
}

async function pollTraces() {
  const refs = app.traces.refs;
  if (!refs || app.route !== 'traces') return;
  try {
    const data = await fetchJSON('/api/traces?limit=30', {
      timeout: 12000,
      signal: app.viewAbort ? app.viewAbort.signal : undefined,
    });
    app.traces.data = data;
    renderTraceSummaryAndList();
    if (refs.updated) {
      refs.updated.textContent = '同步于 ' + fmtTime(Date.now());
      refs.updated.title = fmtDate(Date.now());
    }
    if (refs.pollNote) refs.pollNote.textContent = '';
  } catch (error) {
    if (!error || error.code === 'aborted' || !app.traces.refs) return;
    if (refs.pollNote) refs.pollNote.textContent = '更新失败，稍后重试';
  }
}

async function refreshTracesAll(button) {
  const refs = app.traces.refs;
  if (!refs || refs.loading) return;
  refs.loading = true;
  setButtonBusy(button, true, '刷新中…');
  try {
    const signal = app.viewAbort ? app.viewAbort.signal : undefined;
    const results = await Promise.allSettled([
      fetchJSON('/api/traces?limit=30', { signal }),
      fetchJSON('/api/decisions?limit=30', { signal }),
      fetchJSON('/api/wakeups?status=&limit=30', { signal }),
      fetchJSON('/api/outbox?limit=30', { signal }),
    ]);
    if (!app.traces.refs) return;
    const [traceResult, decisionResult, wakeResult, outboxResult] = results;
    if (traceResult.status === 'fulfilled') {
      app.traces.data = traceResult.value;
      renderTraceSummaryAndList(true);
    } else {
      refs.list.replaceChildren(errorPanel('轨迹加载失败', traceResult.reason, () => refreshTracesAll(refs.refreshButton)));
    }
    if (decisionResult.status === 'fulfilled') {
      app.traces.decisions = decisionResult.value;
      renderDecisionsSection();
    } else {
      refs.decisions.replaceChildren(errorPanel('决策加载失败', decisionResult.reason, () => refreshTracesAll(refs.refreshButton)));
    }
    if (wakeResult.status === 'fulfilled') {
      app.traces.wakeups = wakeResult.value;
      renderWakeupsSection();
    } else {
      refs.wakeups.replaceChildren(errorPanel('唤醒任务加载失败', wakeResult.reason, () => refreshTracesAll(refs.refreshButton)));
    }
    if (outboxResult.status === 'fulfilled') {
      app.traces.outbox = outboxResult.value;
      renderOutboxSection();
    } else {
      refs.outbox.replaceChildren(errorPanel('发件箱加载失败', outboxResult.reason, () => refreshTracesAll(refs.refreshButton)));
    }
    if (refs.updated) {
      refs.updated.textContent = '同步于 ' + fmtTime(Date.now());
      refs.updated.title = fmtDate(Date.now());
    }
    if (refs.pollNote) refs.pollNote.textContent = '';
    if (app.route === 'traces') startPoll(pollTraces, 8000);
    const partial = results.some((result) => result.status === 'rejected');
    toast(partial ? '轨迹已刷新，部分数据失败' : '轨迹已刷新', partial ? 'error' : 'success');
  } catch (error) {
    if (error && error.code !== 'aborted') {
      toast('刷新失败：' + friendlyError(error), 'error');
    }
  } finally {
    refs.loading = false;
    if (button && button.isConnected) setButtonBusy(button, false);
  }
}

function renderTraceSummaryAndList(force) {
  const refs = app.traces.refs;
  const data = app.traces.data;
  if (!refs || !data) return;
  renderTraceSummary(refs.summary, data.summary);
  const items = Array.isArray(data.items) ? data.items : [];
  const signature = JSON.stringify(items);
  if (!force && signature === app.traces.sig && refs.list.children.length) return;
  app.traces.sig = signature;
  renderTraceList(refs.list, items);
}

function renderTraceSummary(container, summary) {
  if (!container) return;
  container.replaceChildren();
  const data = summary && typeof summary === 'object' ? summary : {};
  const entries = [
    ['turns', '回合'],
    ['blocked', '被拦截'],
    ['avg_latency_ms', '平均延迟'],
    ['prompt_tokens', '提示 Token'],
    ['completion_tokens', '回复 Token'],
    ['cache_hit_tokens', '缓存命中'],
  ];
  for (const [key, label] of entries) {
    let value = '—';
    if (key === 'avg_latency_ms') value = fmtMs(data[key]);
    else value = fmtNum(data[key], '0');
    container.append(el('div', { class: 'summary-pill' },
      el('strong', { text: value }),
      el('span', { text: label }),
    ));
  }
}

function renderTraceList(container, items) {
  if (!container) return;
  container.replaceChildren();
  if (!items.length) {
    container.append(emptyPanel('还没有轨迹', '聊一句，或者等她主动做点什么。'));
    return;
  }
  items.forEach((trace, index) => {
    container.append(buildTraceItem(trace, index));
  });
}

function buildTraceItem(trace, index) {
  const safe = trace && typeof trace === 'object' ? trace : {};
  const id = String(safe.id || ('trace-' + index));
  const open = app.traces.expanded.has(id);
  const detailId = 'trace-detail-' + id.replace(/[^a-zA-Z0-9_-]/g, '');
  const risk = riskInfo(safe.security_risk);
  const blocked = safe.blocked === true || safe.blocked === 1 || safe.blocked === '1';
  const actions = normalizeActions(safe.actions);

  const item = el('article', { class: 'trace-item' + (open ? ' is-open' : '') });
  const button = el('button', {
    class: 'trace-row',
    type: 'button',
    'aria-expanded': String(open),
    'aria-controls': detailId,
  });

  const tokenCell = el('span', { class: 'trace-tokens' },
    el('span', {}, el('b', { text: '提示 Token ' }), textNode(fmtNum(safe.prompt_tokens, '0'))),
    el('span', {}, el('b', { text: '回复 Token ' }), textNode(fmtNum(safe.completion_tokens, '0'))),
    el('span', {}, el('b', { text: '缓存 ' }), textNode(fmtNum(safe.cache_hit_tokens, '0'))),
  );
  let actionWrap = null;
  if (actions.length) {
    actionWrap = el('span', { class: 'trace-actions' });
    actions.slice(0, 6).forEach((action) => actionWrap.append(chip(action, 'chip-muted')));
  }

  const top = el('span', { class: 'trace-top' },
    el('span', { class: 'trace-time', text: safe.at_text || fmtDate(safe.at) }),
    el('span', { class: 'trace-model', text: modelLabel(safe.model), title: safe.model ? modelLabel(safe.model) : '' }),
    el('span', { class: 'trace-latency', text: '耗时 ' + fmtMs(safe.latency_ms) }),
  );
  const bottom = el('span', { class: 'trace-bottom' }, tokenCell, actionWrap);
  const body = el('span', { class: 'trace-body' }, top, bottom);

  button.append(
    body,
    el('span', { class: 'trace-badges' },
      badge('风险 · ' + risk.label, risk.tone),
      badge(blocked ? '已拦截' : '正常', blocked ? 'tone-danger' : 'tone-ok'),
    ),
    el('span', { class: 'trace-chevron', 'aria-hidden': 'true' }),
  );

  const detail = el('div', { class: 'trace-detail', id: detailId, hidden: !open });
  const detailHead = el('div', { class: 'trace-detail-head' });
  if (safe.session_id) detailHead.append(chip('会话 ' + truncate(safe.session_id, 30)));
  if (safe.scope) detailHead.append(chip('范围 ' + scopeLabel(safe.scope)));
  if (safe.trust) detailHead.append(chip('信任 ' + trustLabel(safe.trust)));
  if (safe.user_len != null) detailHead.append(chip('输入 ' + fmtNum(safe.user_len, '0') + ' 字'));
  if (actions.length) detailHead.append(chip('动作 ' + actions.join(' / '), 'chip-muted'));
  detailHead.append(chip('编号 ' + truncate(id, 16), 'chip-muted'));
  detail.append(detailHead, el('pre', { class: 'trace-json', text: prettyJSON(safe.result) }));

  const toggle = () => {
    const isOpen = app.traces.expanded.has(id);
    if (isOpen) app.traces.expanded.delete(id);
    else app.traces.expanded.add(id);
    const nowOpen = !isOpen;
    detail.hidden = !nowOpen;
    button.setAttribute('aria-expanded', String(nowOpen));
    item.classList.toggle('is-open', nowOpen);
  };
  button.addEventListener('click', toggle);

  item.append(button, detail);
  return item;
}

function normalizeActions(actions) {
  let value = actions;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch (_err) {
      value = value.split(',').map((item) => item.trim()).filter(Boolean);
    }
  }
  if (!Array.isArray(value)) return [];
  return value.map((action) => {
    if (typeof action === 'string') return action;
    if (action && typeof action === 'object') {
      return String(action.tool || action.name || action.action || 'action');
    }
    return '';
  }).filter(Boolean);
}

function renderDecisionsSection() {
  const refs = app.traces.refs;
  const data = app.traces.decisions;
  if (!refs || !data) return;
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    refs.decisions.replaceChildren(emptyPanel('还没有决策记录', '规则和模型做出的选择会出现在这里。'));
    return;
  }
  const rows = items.map((item) => {
    const safe = item && typeof item === 'object' ? item : {};
    return [
      el('span', { class: 'table-time', text: safe.at_text || fmtDate(safe.at) }),
      decisionKindLabel(safe.kind),
      decisionActionLabel(safe.action),
      badge(statusLabel(safe.outcome), statusTone(safe.outcome)),
      fmtMs(safe.latency_ms),
      el('span', {
        class: 'decision-output',
        text: compactObject(safe.output, 160),
        title: truncate(prettyJSON(safe.output), 900),
      }),
    ];
  });
  refs.decisions.replaceChildren(makeTable(
    ['时间', '类型', '动作', '结果', '延迟', '输出'],
    rows,
    '最近决策',
  ));
}

function renderWakeupsSection() {
  const refs = app.traces.refs;
  const data = app.traces.wakeups;
  if (!refs || !data) return;
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    refs.wakeups.replaceChildren(emptyPanel('没有唤醒任务', '她暂时没有安排主动出现。'));
    return;
  }
  const rows = items.map((item) => {
    const safe = item && typeof item === 'object' ? item : {};
    return [
      badge(statusLabel(safe.status), statusTone(safe.status)),
      safe.intent || '—',
      el('span', { class: 'table-time', text: safe.fire_at_text || safe.created_at_text || fmtDate(safe.fire_at) }),
      fmtNum(safe.attempts, '0'),
      el('span', { text: truncate(safe.reason || '—', 120), title: safe.reason || '' }),
    ];
  });
  refs.wakeups.replaceChildren(makeTable(
    ['状态', '意图', '时间', '尝试', '原因'],
    rows,
    '唤醒任务',
  ));
}

function renderOutboxSection() {
  const refs = app.traces.refs;
  const data = app.traces.outbox;
  if (!refs || !data) return;
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    refs.outbox.replaceChildren(emptyPanel('发件箱是空的', '没有等待送出的消息。'));
    return;
  }
  const rows = items.map((item) => {
    const safe = item && typeof item === 'object' ? item : {};
    return [
      badge(statusLabel(safe.status), statusTone(safe.status)),
      safe.kind || '—',
      el('span', { class: 'table-time', text: fmtDate(safe.available_at) }),
      fmtNum(safe.attempts, '0') + ' / ' + fmtNum(safe.max_attempts, '—'),
      el('span', { class: 'table-error', text: truncate(safe.last_error || '—', 100), title: safe.last_error || '' }),
    ];
  });
  refs.outbox.replaceChildren(makeTable(
    ['状态', '类型', '可用时间', '尝试', '错误'],
    rows,
    '待发件箱',
  ));
}

/* ============================================================================
   12. 关于视图
   ========================================================================== */

const API_ENDPOINTS = [
  ['GET', '/api/health', '服务与依赖状态'],
  ['GET', '/api/state', '状态、生活与计数'],
  ['GET', '/api/memory', '记忆列表与搜索'],
  ['GET', '/api/memory/meta', '记忆目录'],
  ['GET', '/api/memory/open-loops', '未完成的事'],
  ['GET', '/api/traces', '对话轨迹'],
  ['GET', '/api/wakeups', '唤醒任务'],
  ['GET', '/api/outbox', '待发件箱'],
  ['GET', '/api/decisions', '决策日志'],
  ['GET', '/api/config', '脱敏后的配置'],
  ['POST', '/api/chat', '发送一条消息'],
  ['POST', '/api/tick', '推进一次日常（生命周期节拍）'],
  ['POST', '/api/nightly', '夜间记忆压缩（可预演）'],
];

/* ============================================================================
   16. 服务视图 —— 后端的开关面板

   进程不归前端管：真正拉起它们的是一键启动那个窗口（supervisor），
   这里只是替它转发命令。所以面板可能有两种「不好用」：
   control 口没开（页面上直说），或者端口已被别的东西占了（显示「非一键启动」）。
   两种都不猜，照实说。
   ========================================================================== */

const SERVICE_STATE_TEXT = {
  running: '运行中',
  starting: '启动中',
  external: '在运行（不是一键启动拉起的）',
  stopped: '未运行',
  failed: '失败',
};

function serviceStateNode(svc) {
  const state = SERVICE_STATE_TEXT[svc.state] ? svc.state : 'stopped';
  const dot = el('span', {
    class: 'svc-dot',
    'aria-hidden': 'true',
    'data-state': state,
  });
  const text = el('span', { class: 'svc-state-text', text: SERVICE_STATE_TEXT[state] });
  return el('span', { class: 'svc-state' }, dot, text);
}

function serviceRow(svc) {
  const busy = app.services.busy === svc.name;
  const actions = [];

  const canStart = svc.controllable && svc.state === 'stopped';
  const canStop =
    !busy &&
    (svc.state === 'running' || svc.state === 'external' || svc.state === 'starting');
  const canUnload = svc.name === 'tts' && svc.state === 'running' && svc.loaded === true;

  if (canStart) {
    actions.push(
      el('button', {
        class: 'btn btn-primary btn-sm',
        type: 'button',
        text: '启动',
        onClick: () => runServiceAction(svc.name, 'start'),
      }),
    );
  }
  if (canStop) {
    actions.push(
      el('button', {
        class: 'btn btn-ghost btn-sm',
        type: 'button',
        text: '停止',
        onClick: () => runServiceAction(svc.name, 'stop'),
      }),
    );
  }
  if (canUnload) {
    actions.push(
      el('button', {
        class: 'btn btn-quiet btn-sm',
        type: 'button',
        text: '释放显存',
        onClick: () => runServiceAction(svc.name, 'unload'),
      }),
    );
  }
  if (!actions.length) {
    actions.push(el('span', { class: 'svc-hint', text: busy ? '处理中…' : '—' }));
  }

  const meta = [];
  if (svc.port) meta.push('端口 ' + svc.port);
  if (svc.pid) meta.push('PID ' + svc.pid);
  if (svc.loaded === true) meta.push('模型已加载');
  if (svc.loaded === false && svc.state === 'running') meta.push('模型未加载');

  const hints = [];
  if (svc.missing && svc.missing.length) {
    hints.push(
      el('p', { class: 'svc-missing', text: '缺组件：' + svc.missing.join('、') }),
    );
  }
  if (svc.detail) hints.push(el('p', { class: 'svc-detail', text: svc.detail }));

  return el(
    'div',
    { class: 'svc-row' + (busy ? ' is-busy' : '') },
    el(
      'div',
      { class: 'svc-main' },
      el('div', { class: 'svc-top' }, el('strong', { class: 'svc-name', text: svc.label }), serviceStateNode(svc)),
      meta.length
        ? el('p', { class: 'svc-meta', text: meta.join(' · ') })
        : el('p', { class: 'svc-meta', text: '' }),
      ...hints,
    ),
    el('div', { class: 'svc-actions' }, ...actions),
  );
}

function renderServicesBody() {
  const refs = app.services.refs;
  if (!refs || !refs.body) return;
  const data = app.services.data;
  if (!data) {
    refs.body.replaceChildren(el('p', { class: 'svc-hint', text: '读取中…' }));
    return;
  }
  if (!data.available) {
    refs.body.replaceChildren(
      errorPanel(
        '没有连上一键启动',
        data.detail
          ? { message: data.detail }
          : { message: '后端控制口没开，这里没法拉起服务。' },
        loadServices,
      ),
      el(
        'p',
        { class: 'svc-hint' },
        textNode('用「启动大肥鱼.bat」启动一次，控制口会跟着开在 127.0.0.1:9882；'),
        textNode(' 如果是单独跑的 Web，加参数 --no-control 会关掉这层。'),
      ),
    );
    return;
  }
  const rows = (data.services || []).map(serviceRow);
  refs.body.replaceChildren(...rows);
}

function renderServicesSummary() {
  const refs = app.services.refs;
  if (!refs || !refs.sys) return;
  if (refs.note) {
    refs.note.textContent = app.services.note || '';
    refs.note.hidden = !app.services.note;
  }
  const data = app.services.data;
  if (!data || !data.available) {
    refs.sys.replaceChildren();
    return;
  }
  const services = data.services || [];
  const running = services.filter((s) => s.state === 'running' || s.state === 'external').length;
  const loaded = services.some((s) => s.name === 'tts' && s.loaded === true);
  const comfy = services.some((s) => s.name === 'comfyui' && s.state !== 'stopped');
  const tiles = [
    statTile('在运行', el('div', { class: 'stat-value', text: String(running) + ' / ' + String(services.length) })),
    statTile('语音显存', el('div', { class: 'stat-value', text: loaded ? '已占用' : '未占用' })),
    statTile('生图', el('div', { class: 'stat-value', text: comfy ? '在跑' : '空闲' })),
  ];
  refs.sys.replaceChildren(...tiles);
}

async function loadServices() {
  const signal = app.viewAbort ? app.viewAbort.signal : undefined;
  try {
    const data = await fetchJSON('/api/services', { signal });
    if (!app.services.refs || app.route !== 'services') return;
    app.services.data = data;
    renderServicesSummary();
    renderServicesBody();
    if (app.services.refs.updated) {
      app.services.refs.updated.textContent = '刚刚更新';
    }
  } catch (error) {
    if (error && error.code === 'aborted') return;
    if (!app.services.refs || app.route !== 'services') return;
    app.services.data = null;
    renderServicesBody();
  }
}

function serviceSettled(name, action) {
  const data = app.services.data;
  if (!data || !data.available) return true;
  const svc = (data.services || []).find((item) => item.name === name);
  if (action === 'release') {
    const comfy = (data.services || []).find((item) => item.name === 'comfyui');
    const tts = (data.services || []).find((item) => item.name === 'tts');
    return (!comfy || comfy.state === 'stopped') && (!tts || tts.loaded !== true);
  }
  if (!svc) return true;
  if (action === 'start') return svc.state === 'running' || svc.state === 'external';
  if (action === 'stop') return svc.state === 'stopped';
  if (action === 'unload') return svc.loaded !== true || svc.state === 'stopped';
  return true;
}

async function runServiceAction(name, action) {
  if (app.services.busy) return;
  app.services.busy = name || 'release';
  renderServicesBody();
  const url = name ? '/api/services/' + name + '/' + action : '/api/services/release';
  let jobId = '';
  try {
    const accepted = await fetchJSON(url, { method: 'POST', body: {}, timeout: 20000 });
    jobId = accepted && accepted.job ? String(accepted.job.id || '') : '';
  } catch (error) {
    app.services.busy = '';
    renderServicesBody();
    toast(friendlyError(error), 'error');
    return;
  }

  // 动作是异步的（冷启 ComfyUI 要 30~90 秒），轮询到状态真的变了再放手。
  // 任务本身也要看：提交成功不等于做成，比如「停一个本来就没在跑的服务」，
  // 提交时是 202，失败是在 job 里才显形的。
  const deadline = Date.now() + 90000;
  let failure = null;
  while (Date.now() < deadline && app.route === 'services') {
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
    if (app.route !== 'services') break;
    await loadServices();
    const job = jobId ? findServiceJob(jobId) : null;
    if (job && job.state === 'failed') {
      failure = job;
      break;
    }
    if (serviceSettled(name, action)) {
      if (job && job.detail) app.services.note = job.detail;
      break;
    }
  }
  if (app.route !== 'services') return;
  app.services.busy = '';
  await loadServices();
  if (failure) {
    toast(failure.error || '这个动作没做成', 'error');
  } else if (!serviceSettled(name, action)) {
    toast('还在处理中，稍后点刷新看看', 'info');
  }
}

function findServiceJob(jobId) {
  const data = app.services.data;
  if (!data || !Array.isArray(data.jobs)) return null;
  for (const job of data.jobs) {
    if (job && job.id === jobId) return job;
  }
  return null;
}

function renderServicesView(container) {
  const refs = { body: null, sys: null, note: null, updated: null, refresh: null };
  app.services.refs = refs;
  app.services.data = null;
  app.services.busy = '';
  app.services.note = '';

  const updated = el('span', { class: 'updated-label', text: '' });
  refs.updated = updated;
  const refresh = el('button', { class: 'btn btn-ghost', type: 'button', text: '刷新' });
  refs.refresh = refresh;
  refresh.addEventListener('click', () => loadServices());

  const release = el('button', {
    class: 'btn btn-primary',
    type: 'button',
    text: '一键释放显存',
    onClick: () => runServiceAction('', 'release'),
  });

  const summary = el('div', { class: 'stat-grid' });
  refs.sys = summary;

  const note = el('p', { class: 'svc-note', hidden: true });
  refs.note = note;

  const body = el('div', { class: 'svc-list' });
  refs.body = body;

  const releaseCard = el(
    'section',
    { class: 'card' },
    cardHead(
      '一键释放',
      '她自己会重开需要的那个；先把显存让出来。',
      [release],
    ),
    el(
      'ul',
      { class: 'svc-notes' },
      el('li', { text: 'ComfyUI 会被结束进程（它占的显存最大）。' }),
      el('li', { text: 'TTS 只卸载模型、保留进程，下次说话不用再等加载。' }),
      el('li', { text: '向量服务不会动：它跑在 CPU 上，停了贴纸会退化成关键词匹配。' }),
    ),
  );

  container.append(
    viewHeader(
      '服务',
      '她身后的那些进程',
      '语音、向量、生图、QQ——点一下就拉起或放掉，不用再开命令行。',
      [updated, refresh],
    ),
    summary,
    note,
    body,
    releaseCard,
  );

  addCleanup(() => {
    app.services.busy = '';
  });

  loadServices();
}

function renderAboutView(container) {
  const refs = {
    profile: null,
    version: null,
    model: null,
    llm: null,
    tts: null,
    dataDir: null,
    dbPath: null,
    configMount: null,
    errorSlot: null,
  };
  app.about.refs = refs;

  const hero = el('section', { class: 'about-hero' },
    el('div', { class: 'about-hero-copy' },
      el('p', { class: 'eyebrow', text: '关于她' }),
      el('h1', { class: 'about-title', text: '一尾住在你电脑里的鲸鱼' }),
      el('p', { class: 'about-lead', text: '我是蓝色大肥鱼。白天躲在深海里，晚上游到你的屏幕边上。我会记得你说过的话，也会在你很久不来的时候，偷偷闹一点小别扭。' }),
    ),
    el('div', { class: 'about-art', 'aria-hidden': 'true' },
      el('span', { class: 'about-whale' }, whaleSvg('')),
      el('span', { class: 'about-art-bubble a' }),
      el('span', { class: 'about-art-bubble b' }),
    ),
  );

  const errorSlot = el('div', { class: 'overview-error' });
  refs.errorSlot = errorSlot;

  const whatCard = el('section', { class: 'card' },
    cardHead('她是谁', '一个不喧哗的陪伴应用。'),
    el('div', { class: 'about-copy' },
      el('p', { text: '她有自己的生活：心情、精力、正在做的事，还有一些没做完的约定。聊天只是见面的方式，不是全部。' }),
      el('p', { text: '她住在这台电脑里。前端是原生 HTML / CSS / JavaScript，没有框架、没有构建步骤、没有 CDN；你可以直接改文字、颜色和布局。' }),
      el('p', { text: '源码可读、可改，请用于个人、非商业用途。' }),
    ),
  );

  const securityCard = el('section', { class: 'card' },
    cardHead('只在你家', '本地优先的边界。'),
    el('ul', { class: 'about-list' },
      el('li', {}, el('strong', { text: '只监听 127.0.0.1。' }), textNode(' 服务不暴露到局域网，外部设备连不进来。')),
      el('li', {}, el('strong', { text: '需要访问令牌。' }), textNode(' 用启动终端里显示的令牌进入；页面支持 HttpOnly cookie 或手动 Bearer 令牌。')),
      el('li', {}, el('strong', { text: '没有遥测。' }), textNode(' 前端不加载任何外部字体、脚本、图片或统计代码。')),
      el('li', {}, el('strong', { text: '数据留在本地。' }), textNode(' 记忆、轨迹、唤醒任务和数据库都写入本机的数据目录。')),
      el('li', {}, el('strong', { text: '模型调用是例外。' }), textNode(' 只有你配置的模型服务或语音服务会收到必要内容；如果它们也跑在本机，就完全不出门。')),
    ),
  );

  const runningCard = el('section', { class: 'card' },
    cardHead('运行信息', '服务与模型。'),
    el('dl', { class: 'kv' },
      el('dt', { text: '档案' }),
      (refs.profile = el('dd', { text: '读取中…' })),
      el('dt', { text: '版本' }),
      (refs.version = el('dd', { text: '读取中…' })),
      el('dt', { text: '模型' }),
      (refs.model = el('dd', { text: '读取中…' })),
      el('dt', { text: '模型服务' }),
      (refs.llm = el('dd', { text: '读取中…' })),
      el('dt', { text: '语音' }),
      (refs.tts = el('dd', { text: '读取中…' })),
    ),
  );

  const dataDirBlock = copyBlock('数据目录', '读取中…');
  const dbPathBlock = copyBlock('数据库文件', '读取中…');
  refs.dataDir = dataDirBlock.valueEl;
  refs.dbPath = dbPathBlock.valueEl;

  const pathsCard = el('section', { class: 'card' },
    cardHead('数据目录', '她的记忆实际存放的位置。'),
    el('div', { class: 'about-paths' }, dataDirBlock.row, dbPathBlock.row),
    el('p', { class: 'field-hint', text: '备份时直接复制这两段路径；数据库使用 SQLite。' }),
  );

  const runCard = el('section', { class: 'card' },
    cardHead('启动方式', '在项目根目录运行。'),
    el('div', { class: 'command-block' },
      el('code', { text: 'python -m fatfish serve --open' }),
    ),
    el('p', { class: 'field-hint', text: '也可以加上 --profile standard 或 --profile remote 切换配置档案。重新生成访问令牌时，按终端提示操作。' }),
  );

  const configMount = el('div');
  refs.configMount = configMount;
  const configCard = el('section', { class: 'card' },
    cardHead('配置（已脱敏）', '密钥、令牌和密码字段不会显示。'),
    configMount,
  );

  const endpointRows = API_ENDPOINTS.map(([method, path, desc]) => el('div', { class: 'endpoint-row' },
    el('span', { class: 'endpoint-method', text: method }),
    el('code', { class: 'endpoint-path', text: path }),
    el('span', { class: 'endpoint-desc', text: desc }),
  ));

  const endpointsCard = el('section', { class: 'card about-card-wide' },
    cardHead('本地 API', '所有接口都只在这个本机服务上。'),
    el('details', { class: 'details api-details' },
      el('summary', {},
        el('span', { text: '查看全部接口' }),
        el('span', { class: 'summary-count', text: API_ENDPOINTS.length + ' 个' }),
      ),
      el('div', { class: 'endpoint-list' }, endpointRows),
    ),
    el('p', { class: 'field-hint', text: 'POST 请求需要 X-Fatfish-Request: 1 与 Content-Type: application/json。' }),
  );

  const grid = el('div', { class: 'about-grid' },
    whatCard,
    securityCard,
    runningCard,
    pathsCard,
    runCard,
    configCard,
    endpointsCard,
  );

  const page = el('div', { class: 'about-page' },
    hero,
    errorSlot,
    grid,
    el('p', { class: 'about-footnote', text: '蓝色大肥鱼 · 本地优先 · 源码可读可改 · 非商业使用' }),
  );
  container.append(page);

  loadAboutData();
}

async function loadAboutData() {
  const refs = app.about.refs;
  if (!refs) return;
  const signal = app.viewAbort ? app.viewAbort.signal : undefined;
  const [configResult, healthResult] = await Promise.allSettled([
    fetchJSON('/api/config', { signal }),
    fetchJSON('/api/health', { signal }),
  ]);
  if (!app.about.refs || app.route !== 'about') return;

  const errors = [];
  if (configResult.status === 'fulfilled') {
    app.about.config = configResult.value;
    updateAboutConfig();
  } else if (configResult.reason && configResult.reason.code !== 'aborted') {
    errors.push('配置：' + friendlyError(configResult.reason));
    refs.configMount.replaceChildren(errorPanel('配置读取失败', configResult.reason, loadAboutData));
    setText(refs.dataDir, '—');
    setText(refs.dbPath, '—');
  }

  if (healthResult.status === 'fulfilled') {
    app.about.health = healthResult.value;
    setGlobalOnline(true);
    updateAboutHealth();
  } else if (healthResult.reason && healthResult.reason.code !== 'aborted') {
    errors.push('健康检查：' + friendlyError(healthResult.reason));
    setGlobalOnline(false);
  }

  if (refs.errorSlot) {
    if (errors.length) {
      refs.errorSlot.replaceChildren(el('div', { class: 'error-banner' },
        el('span', { text: errors.join('；') }),
        el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '重试', onClick: loadAboutData }),
      ));
    } else {
      refs.errorSlot.replaceChildren();
    }
  }
}

function updateAboutConfig() {
  const refs = app.about.refs;
  const config = app.about.config;
  if (!refs || !config) return;

  const paths = config.paths && typeof config.paths === 'object' ? config.paths : {};
  setText(refs.dataDir, paths.data_dir || '—');
  setText(refs.dbPath, paths.db_path || '—');

  const redacted = redactConfig(config);
  refs.configMount.replaceChildren(
    el('details', { class: 'details' },
      el('summary', { text: '展开查看当前配置' }),
      el('pre', { class: 'json-block config-json', text: prettyJSON(redacted) }),
    ),
  );
}

function updateAboutHealth() {
  const refs = app.about.refs;
  const health = app.about.health;
  if (!refs || !health) return;
  const llm = health.llm && typeof health.llm === 'object' ? health.llm : {};
  const tts = health.tts && typeof health.tts === 'object' ? health.tts : {};
  const config = app.about.config;

  setText(refs.profile, health.profile ? profileLabel(health.profile) : (config && config.profile ? profileLabel(config.profile) : '—'));
  setText(refs.version, (health.app || 'fatfish') + (health.version ? ' ' + health.version : ''));
  const model = llm.model || (config && config.llm && config.llm.model) || '';
  setText(refs.model, model ? modelLabel(model) : '未配置');

  if (refs.llm) {
    refs.llm.replaceChildren();
    refs.llm.append(badge(llm.available ? '可用' : '本地回复', llm.available ? 'tone-ok' : 'tone-warn'));
    if (llm.provider) refs.llm.append(textNode(' · ' + providerLabel(llm.provider)));
    if (llm.key_present === false) refs.llm.append(textNode(' · 未配置密钥'));
  }

  if (refs.tts) {
    refs.tts.replaceChildren();
    if (!tts.enabled) {
      refs.tts.append(textNode('未启用'));
    } else {
      refs.tts.append(
        badge('已启用', 'tone-ok'),
        textNode(' · ' + ttsModeLabel(tts.mode || '') + (tts.configured ? '' : ' · 未配置')),
      );
    }
  }
}

/* ============================================================================
   13. 初始化
   ========================================================================== */

function absorbTokenFromUrl() {
  let url;
  try {
    url = new URL(window.location.href);
  } catch (_err) {
    app.token = readStorage(TOKEN_KEY) || '';
    return;
  }
  if (url.searchParams.has('token')) {
    const token = (url.searchParams.get('token') || '').trim();
    if (token) {
      app.token = token;
      writeStorage(TOKEN_KEY, token);
    }
    url.searchParams.delete('token');
    const search = url.searchParams.toString();
    const next = url.pathname + (search ? '?' + search : '') + url.hash;
    history.replaceState(null, '', next);
  }
  if (!app.token) app.token = readStorage(TOKEN_KEY) || '';
}

function bindGlobalEvents() {
  window.addEventListener('hashchange', () => routeView(false));

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (app.poll.timer) {
        window.clearTimeout(app.poll.timer);
        app.poll.timer = null;
      }
      return;
    }
    if (app.poll.fn && !app.poll.timer) schedulePoll(250);
  });

  const tokenButton = document.getElementById('token-button');
  if (tokenButton) {
    tokenButton.addEventListener('click', () => showLogin(''));
  }

  const skipLink = document.querySelector('.skip-link');
  if (skipLink) {
    skipLink.addEventListener('click', (event) => {
      event.preventDefault();
      viewEl.focus();
      viewEl.scrollTop = 0;
    });
  }

  if (loginFormEl) loginFormEl.addEventListener('submit', handleLoginSubmit);

  const loginClose = document.getElementById('login-close');
  if (loginClose) loginClose.addEventListener('click', hideLogin);

  if (overlayEl) {
    overlayEl.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        hideLogin();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusables = Array.from(overlayEl.querySelectorAll('input, button'))
        .filter((node) => !node.disabled && node.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }
}

function init() {
  absorbTokenFromUrl();
  app.sessionId = normalizeSessionId(readStorage(SESSION_KEY));
  writeStorage(SESSION_KEY, app.sessionId);
  app.isGroup = readStorage(GROUP_KEY) === '1';

  setGlobalOnline(null);
  bindGlobalEvents();
  routeView(true);
}

init();
