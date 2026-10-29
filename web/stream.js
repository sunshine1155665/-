const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('text');
const send = document.getElementById('send');
const continuousBox = document.getElementById('continuous');

let continuous = false;
let queue = [];
let playing = false;
let firstAutoPlayed = false;
let turnStart = 0;
let firstSegmentAt = 0;

function bubble(who, text) {
  const row = document.createElement('div');
  row.className = 'row ' + who;
  row.textContent = text;
  log.appendChild(row);
  row.scrollIntoView({block: 'end'});
  return row;
}

function note(row, text) {
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = text;
  row.appendChild(meta);
  return meta;
}

function showPlayButton(item) {
  if (item.button) return;
  const btn = document.createElement('button');
  btn.className = 'play';
  btn.style.display = 'inline-block';
  btn.textContent = `播放第 ${item.index + 1} 句`;
  btn.onclick = () => {
    btn.remove();
    item.button = null;
    playItem(item);
  };
  item.button = btn;
  item.segRow.appendChild(btn);
}

function playItem(item) {
  playing = true;
  item.segRow.classList.remove('pending');
  const started = performance.now();
  item.audio.addEventListener('ended', () => {
    const seconds = (performance.now() - started) / 1000;
    const meta = item.segRow.querySelector('.meta');
    if (meta) meta.textContent = `第 ${item.index + 1} 句 · ${seconds.toFixed(1)}s`;
    playing = false;
    if (continuous) playNext();
  }, {once: true});
  item.audio.play().catch(() => {
    playing = false;
    showPlayButton(item);
  });
}

function playNext() {
  const item = queue.shift();
  if (!item) { playing = false; return; }
  playItem(item);
}

function enqueue(url, segRow, index) {
  if (!url) return;
  const audio = new Audio();
  audio.preload = 'auto';
  audio.src = url;
  const item = {audio, segRow, index, button: null};

  if (!firstAutoPlayed) {
    firstAutoPlayed = true;
    playItem(item);
    return;
  }
  if (continuous) {
    queue.push(item);
    if (!playing) playNext();
    return;
  }
  showPlayButton(item);
}

function handleEvent(evt, state) {
  if (evt.type === 'segment') {
    if (!firstSegmentAt) firstSegmentAt = performance.now();
    const seg = document.createElement('div');
    seg.className = 'seg pending';
    seg.textContent = evt.text;
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = `第 ${state.segments + 1} 句` + (evt.cached ? ' · 缓存' : '');
    seg.prepend(badge);
    state.row.appendChild(seg);
    state.row.scrollIntoView({block: 'end'});
    state.segments += 1;
    if (evt.ok) {
      enqueue(evt.audio_url, seg, state.segments - 1);
    } else {
      const err = document.createElement('div');
      err.className = 'err';
      err.textContent = '这一句没合成：' + (evt.error || 'unknown');
      seg.appendChild(err);
    }
    return;
  }
  if (evt.type === 'done') {
    const result = evt.result || {};
    const first = firstSegmentAt ? (firstSegmentAt - turnStart) / 1000 : null;
    const total = (performance.now() - turnStart) / 1000;
    note(state.row,
      `首句 ${first === null ? '-' : first.toFixed(1)}s · 总 ${total.toFixed(1)}s · ` +
      `语音 ${state.segments} 段 · ${(result.voice || {}).mode || '-'}`);
    if (result.tts && !result.tts.ok) {
      const err = document.createElement('div');
      err.className = 'err';
      err.textContent = '语音失败：' + (result.tts.error || 'unknown');
      state.row.appendChild(err);
    }
    return;
  }
  if (evt.type === 'error') {
    const err = document.createElement('div');
    err.className = 'err';
    err.textContent = '出错了：' + evt.error;
    state.row.appendChild(err);
  }
}

continuousBox.addEventListener('change', () => {
  continuous = continuousBox.checked;
  if (continuous && !playing) playNext();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  send.disabled = true;
  turnStart = performance.now();
  firstSegmentAt = 0;
  firstAutoPlayed = false;
  queue = [];

  bubble('me', message);
  const row = bubble('her', '…');
  const state = {row, segments: 0};

  try {
    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Fatfish-Request': '1'},
      body: JSON.stringify({message, session_id: 'web-stream'}),
    });
    if (!response.ok) {
      throw new Error('HTTP ' + response.status + ' ' + (await response.text()).slice(0, 120));
    }
    if (state.row.textContent === '…') state.row.textContent = '';
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let cut;
      while ((cut = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, cut).trim();
        buffer = buffer.slice(cut + 1);
        if (line) handleEvent(JSON.parse(line), state);
      }
    }
    if (buffer.trim()) handleEvent(JSON.parse(buffer), state);
  } catch (error) {
    const err = document.createElement('div');
    err.className = 'err';
    err.textContent = '连接失败：' + error.message;
    state.row.appendChild(err);
  } finally {
    send.disabled = false;
    input.focus();
  }
});
