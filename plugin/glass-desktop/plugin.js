// Glass Desktop — shows the PC screen on the glasses (frames come ready-made
// from glass_stream.py) and turns the phone into a trackpad and keyboard.
import { createGMPlugin } from './vendor/gm-plugin-web-sdk.esm.js';

const $ = (s) => document.querySelector(s);
const ZOOMS = [1, 2, 3, 4];
const DEFAULTS = { host: '', pin: '', size: '600x350', levels: 4, zoom: 3, brightness: -30, contrast: 1.2,
  style: 'text', invert: 'auto', sens: 1.5, screen: '', res: '1280x720', dark: 'on' };

let gm = null;
let atomic = false;
let pageOpen = false;
let ws = null;
let authed = false;
let paused = false;
let frameId = 1;
let settings = { ...DEFAULTS };
const stats = { frames: 0, bytes: 0, ms: 0, t: performance.now() };

const status = (msg) => { $('#status').textContent = msg; };

function bytesToBase64(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

// ---------- frames ----------

// v1: "GD" | u8 1 | u8 tiles | u16 w | u16 h | u16 x0 | u16 y0 | u32 seq
//     tile: u16 y | u16 rows | u32 decodedSize | u32 compLen | LZ4
// v2: "GD" | u8 2 | u8 tiles | 8 reserved bytes | u32 seq
//     tile: u16 x | u16 y | u16 w | u16 rows | u32 decodedSize | u32 compLen | LZ4
// Each tile is an independent screen rectangle (v2 sends only what changed).
function parseFrame(buf) {
  const view = new DataView(buf);
  if (view.getUint8(0) !== 0x47 || view.getUint8(1) !== 0x44) throw new Error('not a Glass Desktop frame');
  const version = view.getUint8(2);
  const tileCount = view.getUint8(3);
  const tiles = [];
  let off = 16;
  for (let i = 0; i < tileCount; i += 1) {
    let x; let y; let w; let rows;
    if (version >= 2) {
      x = view.getUint16(off, true);
      y = view.getUint16(off + 2, true);
      w = view.getUint16(off + 4, true);
      rows = view.getUint16(off + 6, true);
      off += 8;
    } else {
      w = view.getUint16(4, true);
      x = view.getUint16(8, true);
      y = view.getUint16(10, true) + view.getUint16(off, true);
      rows = view.getUint16(off + 2, true);
      off += 4;
    }
    const decodedSize = view.getUint32(off, true);
    const len = view.getUint32(off + 4, true);
    off += 8;
    tiles.push({ x, y, w, rows, decodedSize, data: new Uint8Array(buf, off, len) });
    off += len;
  }
  return tiles;
}

async function showFrame(tiles) {
  if (!pageOpen) {
    try { await gm.display.closePage(); } catch { /* none */ }
    await gm.display.createPage();
    pageOpen = true;
  }
  const one = (t) => ({
    x: t.x, y: t.y, width: t.w, height: t.rows, stride: t.w / 2,
    decodedSize: t.decodedSize, dataBase64: bytesToBase64(t.data)
  });
  if (tiles.length === 1) {
    await gm.display.updateImageLz4(one(tiles[0]));
  } else if (atomic) {
    const id = frameId;
    frameId = frameId >= 0xffffffff ? 1 : frameId + 1;
    await gm.display.beginFrame({ frameId: id, tileCount: tiles.length });
    for (let i = 0; i < tiles.length; i += 1) {
      await gm.display.updateFrameImageLz4({ frameId: id, tileIndex: i, ...one(tiles[i]) });
    }
  } else {
    for (const t of tiles) await gm.display.updateImageLz4(one(t));
  }
}

// ---------- connection ----------

function wsUrl(host) {
  const h = host.trim().replace(/^(https?|wss?):\/\//, '').replace(/\/+$/, '');
  if (h === 'localhost' || h.startsWith('127.0.0.1')) return `ws://${h.includes(':') ? h : `${h}:8765`}`;
  return `wss://${h}`;
}

function send(obj) {
  if (ws?.readyState === WebSocket.OPEN && (authed || obj.type === 'hello')) ws.send(JSON.stringify(obj));
}

function sendConfig() {
  send({ type: 'config', size: settings.size, levels: settings.levels, zoom: settings.zoom,
    brightness: settings.brightness, contrast: settings.contrast, style: settings.style, invert: settings.invert,
    res: settings.res, dark: settings.dark === 'on', ...(settings.screen ? { screen: settings.screen } : {}) });
}

// ---------- PC audio ----------
// The PC sends 24 kHz 16-bit PCM packets ("GA"). Instead of playing each packet
// as its own little clip (which leaves tiny gaps and resampling seams = a
// watery buzz), samples go into one ring buffer that a single audio node reads
// continuously, resampling smoothly and gently speeding up / slowing down to
// keep about 0.15 s buffered as the PC and phone clocks drift apart.

let actx = null;
let audioOn = true;
const RING = 24000 * 2;                       // 2 s per channel at 24 kHz
const ring = [new Float32Array(RING), new Float32Array(RING)];
let srcRate = 24000;
let writePos = 0;                              // total samples written
let readPos = 0;                               // fractional read position
let buffering = true;                          // waiting to fill the cushion
const TARGET = 0.15;                           // seconds of cushion
let node = null;

function ensureAudio() {   // must run inside a tap on iOS
  try {
    if (navigator.audioSession) navigator.audioSession.type = 'playback';  // ignore the silent switch
    if (!actx) {
      actx = new (window.AudioContext || window.webkitAudioContext)();
      node = actx.createScriptProcessor(2048, 0, 2);
      node.onaudioprocess = fillOutput;
      node.connect(actx.destination);
    }
    if (actx.state === 'suspended') actx.resume();
  } catch { /* no audio available */ }
}

function fillOutput(e) {
  const L = e.outputBuffer.getChannelData(0);
  const R = e.outputBuffer.getChannelData(1);
  const n = L.length;
  let fill = (writePos - readPos) / srcRate;            // seconds buffered
  if (!audioOn || buffering) {
    L.fill(0); R.fill(0);
    if (fill >= TARGET) buffering = false;
    return;
  }
  // nudge speed by up to ±1% to hold the cushion steady
  const adjust = 1 + Math.max(-0.01, Math.min(0.01, (fill - TARGET) * 0.05));
  const step = (srcRate / actx.sampleRate) * adjust;
  const [a, b] = ring;
  for (let i = 0; i < n; i += 1) {
    if (writePos - readPos < 2) {                        // ran dry: fade out, refill
      L.fill(0, i); R.fill(0, i);
      buffering = true;
      return;
    }
    const k = Math.floor(readPos);
    const f = readPos - k;
    const i0 = k % RING;
    const i1 = (k + 1) % RING;
    L[i] = a[i0] + (a[i1] - a[i0]) * f;
    R[i] = b[i0] + (b[i1] - b[i0]) * f;
    readPos += step;
  }
}

function playAudio(buf) {
  if (!actx || !audioOn) return;
  const view = new DataView(buf);
  const channels = view.getUint8(3);
  srcRate = view.getUint32(4, true);
  const samples = new Int16Array(buf, 12);
  const frames = Math.floor(samples.length / channels);
  // too far behind (e.g. after a network stall): skip ahead to the cushion
  if ((writePos - readPos) / srcRate > 0.6) readPos = writePos - TARGET * srcRate;
  for (let i = 0; i < frames; i += 1) {
    const w = (writePos + i) % RING;
    const l = samples[i * channels] / 32768;
    ring[0][w] = l;
    ring[1][w] = channels > 1 ? samples[i * channels + 1] / 32768 : l;
  }
  writePos += frames;
}

function setAudioButton() {
  const b = $('#audio');
  b.textContent = audioOn ? 'Sound on' : 'Sound off';
  b.classList.toggle('on', !audioOn);
}

// Hand the glasses display back so the last PC frame doesn't stay up.
async function clearGlasses() {
  if (!pageOpen || !gm) return;
  pageOpen = false;
  try { await gm.display.closePage(); } catch { /* already closed */ }
}

// The PC lists its screens (left to right); remember the pick by its Windows id.
function fillScreens(list, pcDefault) {
  const sel = $('#screen');
  sel.replaceChildren();
  for (const s of list) {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = s.label;
    sel.append(o);
  }
  if (!list.some((s) => s.id === settings.screen)) settings.screen = pcDefault || list[0]?.id || '';
  sel.value = settings.screen;
  sel.disabled = list.length < 2;
}

function setControls(on) { $('#control').classList.toggle('off', !on); }

function connect() {
  if (ws) { ws.close(1000); return; }
  settings.host = $('#host').value.trim();
  settings.pin = $('#pin').value.trim();
  if (!settings.host) { status('Enter your PC’s Tailscale name first.'); return; }
  save();
  ensureAudio();
  const url = wsUrl(settings.host);
  status(`Connecting to ${url}…`);
  let socket;
  try { socket = new WebSocket(url); } catch (error) { status(`Blocked before connecting: ${error.message}`); return; }
  ws = socket;
  authed = false;
  socket.binaryType = 'arraybuffer';
  $('#connect').textContent = 'Disconnect';
  let busy = false;

  socket.onopen = () => { send({ type: 'hello', pin: settings.pin }); };
  socket.onmessage = async (event) => {
    if (typeof event.data === 'string') {
      let msg = {};
      try { msg = JSON.parse(event.data); } catch { return; }
      if (msg.type === 'welcome') {
        authed = true;
        fillScreens(msg.screens || [], msg.screen);
        paused = false;
        sendConfig();
        setControls(msg.control !== false);
        status(msg.control === false ? 'Connected — streaming (mouse/keyboard only work on Windows).'
          : msg.audio ? 'Connected — picture and sound.' : 'Connected — no PC sound (see the PC window).');
        send({ type: 'audio', on: audioOn });
      } else if (msg.type === 'error') {
        status(msg.message);
      }
      return;
    }
    if (new Uint8Array(event.data, 0, 2)[1] === 0x41) { playAudio(event.data); return; }  // "GA"
    if (busy) return;
    busy = true;
    const t0 = performance.now();
    try {
      await showFrame(parseFrame(event.data));
      stats.frames += 1;
      stats.bytes += event.data.byteLength;
      stats.ms += performance.now() - t0;
    } catch (error) {
      status(`Glasses error: ${error.code || error.message}`);
      pageOpen = false;
    } finally {
      busy = false;
      send({ type: 'ack' });
    }
  };
  socket.onerror = () => status('Connection error — check the address, that glass_stream.py is running, and tailscale serve.');
  socket.onclose = (event) => {
    if (ws === socket) ws = null;
    authed = false;
    clearGlasses();
    setControls(false);
    $('#connect').textContent = 'Connect';
    if (!/PIN/.test($('#status').textContent)) {
      status(event.code === 1000 ? 'Disconnected.' : event.code === 1006
        ? 'Disconnected: could not reach the PC (or the connection dropped).' : `Disconnected (code ${event.code}).`);
    }
  };
}

setInterval(() => {
  const now = performance.now();
  const el = (now - stats.t) / 1000;
  if (el < 1) return;
  $('#fps').textContent = ws ? (stats.frames / el).toFixed(1) : '–';
  $('#kbs').textContent = ws ? (stats.bytes / 1024 / el).toFixed(0) : '–';
  $('#ms').textContent = ws && stats.frames ? (stats.ms / stats.frames).toFixed(0) : '–';
  Object.assign(stats, { frames: 0, bytes: 0, ms: 0, t: now });
}, 1000);

// ---------- trackpad ----------

const pad = $('#pad');
let pendX = 0;
let pendY = 0;
let pendScroll = 0;
let flushQueued = false;
const touch = { start: 0, maxFingers: 0, moved: 0, lastX: 0, lastY: 0, longTimer: null, dragging: false };

function flush() {
  flushQueued = false;
  const dx = Math.trunc(pendX);
  const dy = Math.trunc(pendY);
  if (dx || dy) { send({ type: 'move', dx, dy }); pendX -= dx; pendY -= dy; }
  const wheel = Math.trunc(pendScroll);
  if (wheel) { send({ type: 'scroll', dy: wheel }); pendScroll -= wheel; }
}
function queueFlush() {
  if (!flushQueued) { flushQueued = true; requestAnimationFrame(flush); }
}

function centroid(list) {
  let x = 0; let y = 0;
  for (const t of list) { x += t.clientX; y += t.clientY; }
  return { x: x / list.length, y: y / list.length };
}

pad.addEventListener('touchstart', (e) => {
  e.preventDefault();
  pad.classList.add('active');
  const c = centroid(e.touches);
  touch.lastX = c.x; touch.lastY = c.y;
  if (e.touches.length === 1 && !touch.maxFingers) {
    touch.start = performance.now();
    touch.moved = 0;
    clearTimeout(touch.longTimer);
    touch.longTimer = setTimeout(() => {
      if (touch.moved < 10 && touch.maxFingers === 1) {
        touch.dragging = true;
        send({ type: 'button', button: 'left', pressed: true });
        pad.querySelector('span').textContent = 'Dragging';
      }
    }, 450);
  }
  touch.maxFingers = Math.max(touch.maxFingers, e.touches.length);
}, { passive: false });

pad.addEventListener('touchmove', (e) => {
  e.preventDefault();
  const c = centroid(e.touches);
  const dx = c.x - touch.lastX;
  const dy = c.y - touch.lastY;
  touch.lastX = c.x; touch.lastY = c.y;
  touch.moved += Math.abs(dx) + Math.abs(dy);
  if (touch.moved > 10) clearTimeout(touch.longTimer);
  if (e.touches.length >= 2) {
    pendScroll += dy * 4;           // natural scrolling, like an iPhone
  } else {
    const speed = Math.hypot(dx, dy);
    const gain = settings.sens * (1 + Math.min(3, speed / 12));  // faster swipes travel further
    pendX += dx * gain;
    pendY += dy * gain;
  }
  queueFlush();
}, { passive: false });

function touchDone(e) {
  e.preventDefault();
  if (e.touches.length) {   // some fingers still down: continue from their centre
    const c = centroid(e.touches);
    touch.lastX = c.x; touch.lastY = c.y;
    return;
  }
  clearTimeout(touch.longTimer);
  pad.classList.remove('active');
  if (touch.dragging) {
    send({ type: 'button', button: 'left', pressed: false });
    touch.dragging = false;
    pad.querySelector('span').textContent = 'Trackpad';
  } else if (performance.now() - touch.start < 280 && touch.moved < 12) {
    send({ type: 'click', button: touch.maxFingers >= 2 ? 'right' : 'left' });
  }
  touch.maxFingers = 0;
}
pad.addEventListener('touchend', touchDone, { passive: false });
pad.addEventListener('touchcancel', touchDone, { passive: false });

// mouse buttons
for (const b of document.querySelectorAll('[data-click]')) {
  b.addEventListener('click', () => send({ type: 'click', button: b.dataset.click }));
}
$('[data-double]').addEventListener('click', () => send({ type: 'click', button: 'left', double: true }));
let holding = false;
$('#hold').addEventListener('click', () => {
  holding = !holding;
  $('#hold').classList.toggle('on', holding);
  send({ type: 'button', button: 'left', pressed: holding });
});

// ---------- keyboard ----------

const kbd = $('#kbd');
const SENTINEL = '\u200b';   // keeps Backspace working on an "empty" field
const resetKbd = () => { kbd.value = SENTINEL; kbd.setSelectionRange(1, 1); };
kbd.addEventListener('focus', resetKbd);
kbd.addEventListener('beforeinput', (e) => {
  e.preventDefault();
  if (e.inputType === 'insertText' || e.inputType === 'insertReplacementText' || e.inputType === 'insertFromPaste') {
    const text = e.data ?? e.dataTransfer?.getData('text/plain') ?? '';
    if (text) send({ type: 'text', text });
  } else if (e.inputType === 'deleteContentBackward') {
    send({ type: 'key', key: 'backspace' });
  } else if (e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') {
    send({ type: 'key', key: 'enter' });
  }
  resetKbd();
});
kbd.addEventListener('input', () => {   // fallback if beforeinput could not be cancelled
  const text = kbd.value.replaceAll(SENTINEL, '');
  if (text) send({ type: 'text', text });
  resetKbd();
});
kbd.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); send({ type: 'key', key: 'enter' }); }
});
for (const b of document.querySelectorAll('[data-key]')) {
  b.addEventListener('click', () => send({ type: 'key', key: b.dataset.key }));
}

// ---------- settings ----------

async function save() {
  try { await gm?.storage.set('settings', settings); } catch { /* best effort */ }
}

function applyUI() {
  $('#host').value = settings.host;
  $('#pin').value = settings.pin;
  for (const id of ['size', 'zoom', 'style', 'invert', 'levels', 'res', 'dark']) $(`#${id}`).value = String(settings[id]);
  $('#brightness').value = String(settings.brightness);
  $('#brightnessOut').textContent = String(settings.brightness);
  $('#contrast').value = String(settings.contrast);
  $('#contrastOut').textContent = Number(settings.contrast).toFixed(1);
  $('#sens').value = String(settings.sens);
  $('#sensOut').textContent = Number(settings.sens).toFixed(2);
}

function readUI() {
  settings.size = $('#size').value;
  settings.zoom = Number($('#zoom').value);
  settings.style = $('#style').value;
  settings.invert = $('#invert').value;
  settings.levels = Number($('#levels').value);
  settings.brightness = Number($('#brightness').value);
  settings.contrast = Number($('#contrast').value);
  settings.sens = Number($('#sens').value);
  applyUI();
  save();
  sendConfig();
}

for (const id of ['size', 'zoom', 'style', 'invert', 'levels']) $(`#${id}`).addEventListener('change', readUI);
for (const id of ['brightness', 'contrast', 'sens']) $(`#${id}`).addEventListener('input', readUI);
$('#connect').addEventListener('click', connect);
$('#dark').addEventListener('change', () => {
  settings.dark = $('#dark').value;
  save();
  sendConfig();
  status(settings.dark === 'on' ? 'Switching the PC monitors off…' : 'Switching the PC monitors on…');
});
$('#res').addEventListener('change', () => {
  settings.res = $('#res').value;
  save();
  sendConfig();
  status('Changing the PC resolution…');
});
$('#screen').addEventListener('change', () => {
  settings.screen = $('#screen').value;
  save();
  sendConfig();
  status('Switching screen…');
});
$('#audio').addEventListener('click', () => {
  audioOn = !audioOn;
  if (audioOn) ensureAudio();
  readPos = writePos;
  buffering = true;
  setAudioButton();
  send({ type: 'audio', on: audioOn });
});

// ---------- startup ----------

async function init() {
  applyUI();
  setControls(false);
  try {
    gm = createGMPlugin({ timeoutMs: 12000 });
    await gm.ready();
    const caps = await gm.runtime.getCapabilities();
    atomic = caps.display?.includes('atomic-framed-lz4') === true;
    try { settings = { ...DEFAULTS, ...((await gm.storage.get('settings')).value || {}) }; } catch { /* defaults */ }
    applyUI();
    $('#state').textContent = 'glasses ready';
    $('#state').classList.add('ok');
    gm.device.onConnection((e) => {
      $('#state').textContent = e.connected ? 'glasses ready' : 'glasses disconnected';
      $('#state').classList.toggle('ok', e.connected);
      if (!e.connected) pageOpen = false;
    });
    gm.device.onButton((e) => {
      if (e.action === 'single') {
        settings.zoom = ZOOMS[(ZOOMS.indexOf(settings.zoom) + 1) % ZOOMS.length];
        applyUI(); save(); sendConfig();
      } else if (e.action === 'long') {
        paused = !paused;
        send({ type: 'pause', paused });
        status(paused ? 'Paused from the glasses.' : 'Streaming.');
      }
    });
    await gm.device.subscribeEvents(['button', 'connection']);
  } catch (error) {
    $('#state').textContent = 'no bridge';
    status(`Could not connect to the MemoMind app (${error.code || error.message}).`);
  }
}

// Close cleanly when the page goes away, so the PC knows right away
// (it then turns the other screens back on).
window.addEventListener('pagehide', () => { ws?.close(1000); clearGlasses(); });
await init();
