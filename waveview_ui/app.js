// waveview browser UI. Talks to tools/waveview.py; the view state lives on the
// server so that the command line (and an AI agent) can drive it too.
'use strict';

const ROW = 22;
const RULER = 36;
const $ = (id) => document.getElementById(id);

const S = {
  meta: null,
  all: [],             // [[name, width]] every signal in the dump
  width: new Map(),    // name -> width
  state: null,         // last state from the server
  rows: [],            // [{name, radix, w}] shown rows
  view: [0, 1],
  cursor: null,
  marker: null,
  selected: new Set(),
  anchor: null,        // last clicked row index for shift ranges
  data: null,          // last /api/wave answer
  seq: 0,
  vals: {},            // value at cursor per name
  client: Math.random().toString(36).slice(2),
  scope: null,         // tree scope shown in the signal list
  pick: new Set(),     // chosen entries in the signal list
  drag: null,
  colors: null,
};

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

async function api(path, body) {
  const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function cmd(op, args) {
  return api('/api/cmd', { op, args: args || {}, source: 'ui', client: S.client })
    .then((r) => r.result)
    .catch((e) => { toast(e.message); return null; });
}

let viewTimer = null;
function pushView() {
  clearTimeout(viewTimer);
  viewTimer = setTimeout(() => cmd('view', { t0: S.view[0], t1: S.view[1] }), 250);
}
function pushRows() {
  cmd('set_signals', { signals: S.rows.map((r) => ({ name: r.name, radix: r.radix })) });
}
function pushSelect() { cmd('select', { signals: [...S.selected] }); }

function connect() {
  const es = new EventSource('/api/events');
  es.addEventListener('state', (ev) => applyState(JSON.parse(ev.data)));
  es.addEventListener('reload', (ev) => {
    S.meta = JSON.parse(ev.data);
    loadSignals().then(() => { S.data = null; requestData(); fetchValues(); });
    toast('Dump reloaded from disk');
  });
  es.onopen = () => setPill('st-conn', 'connected', 'ok');
  es.onerror = () => setPill('st-conn', 'disconnected, retrying…', 'bad');
}

let firstState = true;
function applyState(st) {
  const own = st.last && st.last.client === S.client;
  const prevGen = S.state ? S.state.gen : st.gen;
  S.state = st;
  if (!own || firstState) {
    S.rows = st.signals.map((s) => ({ name: s.name, radix: s.radix, w: S.width.get(s.name) || 1 }));
    S.view = [st.view[0], st.view[1]];
    S.cursor = st.cursor;
    S.marker = st.marker;
    S.selected = new Set(st.selected);
    renderRows();
    fetchValues();
  }
  firstState = false;
  if (st.gen !== prevGen) S.data = null;
  if (st.last && !own && st.last.source !== 'ui') {
    const el = $('st-last');
    el.textContent = `${st.last.source}: ${st.last.op}`;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 2500);
  }
  renderNote();
  renderStatus();
  requestData();
  draw();
}

async function loadSignals() {
  S.meta = await api('/api/meta');
  S.all = await api('/api/signals');
  S.width = new Map(S.all);
  for (const r of S.rows) r.w = S.width.get(r.name) || 1;
  buildTree();
  $('st-dump').textContent = `${S.meta.dump}  ·  ${S.meta.signals} signals  ·  ${S.meta.timescale}`;
  document.title = 'waveview · ' + S.meta.dump.split('/').pop();
  $('title').textContent = S.meta.dump.split('/').pop();
}

// wave data for the visible rows, fetched with one screen of margin each side
let fetchTimer = null;
function requestData(force) {
  const names = S.rows.map((r) => r.name);
  const key = names.join('|') + '#' + (S.state ? S.state.gen : 0);
  const span = S.view[1] - S.view[0];
  const d = S.data;
  const fits = d && d.key === key && S.view[0] >= d.t0 && S.view[1] <= d.t1 &&
    Math.abs(Math.log((d.t1 - d.t0) / 3 / span)) < 0.6;
  if (fits && !force) return;
  clearTimeout(fetchTimer);
  fetchTimer = setTimeout(async () => {
    const seq = ++S.seq;
    try {
      const r = await api('/api/wave', { signals: names, t0: S.view[0] - span, t1: S.view[1] + span, px: canvasW() * 3 });
      if (seq !== S.seq) return;
      r.key = key;
      S.data = r;
      draw();
    } catch (e) { toast(e.message); }
  }, d ? 40 : 0);
}

let valTimer = null;
function fetchValues() {
  clearTimeout(valTimer);
  valTimer = setTimeout(async () => {
    if (S.cursor == null || !S.rows.length) { S.vals = {}; renderValues(); return; }
    try {
      const r = await api('/api/values', { signals: S.rows.map((x) => x.name), t: S.cursor });
      S.vals = r.values;
      S.cursorCycle = r.cycle;
    } catch (e) { S.vals = {}; }
    renderValues();
    renderReadout();
  }, 30);
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtVal(bits, w, radix) {
  if (bits == null) return '';
  if (w === 1) return bits;
  if (/[xz]/.test(bits)) {
    if (/^x+$/.test(bits)) return 'x';
    if (/^z+$/.test(bits)) return 'z';
    if (radix === 'bin') return bits;
    let out = '';
    for (let i = bits.length; i > 0; i -= 4) {
      const nib = bits.slice(Math.max(0, i - 4), i);
      out = (/x/.test(nib) ? 'x' : /z/.test(nib) ? 'z' : parseInt(nib, 2).toString(16)) + out;
    }
    return out;
  }
  const n = BigInt('0b' + bits);
  switch (radix) {
    case 'dec': return n.toString();
    case 'sdec': return (bits[0] === '1' ? n - (1n << BigInt(w)) : n).toString();
    case 'bin': return bits;
    case 'oct': return n.toString(8);
    default: return n.toString(16).padStart(Math.ceil(w / 4), '0');
  }
}

function unitOf() { return S.meta ? S.meta.timescale.replace(/^1(?=[a-z])/, '') : ''; }
function fmtT(t) {
  if (t == null) return '-';
  const v = Number.isInteger(t) ? t : Math.round(t * 10) / 10;
  return v.toLocaleString('en-US') + ' ' + unitOf();
}
function cycleAt(t) {
  const d = S.data;
  if (!d || !d.edges || t == null) return null;
  let lo = 0, hi = d.edges.length - 1, k = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (d.edges[m] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
  return k < 0 ? null : d.edge0 + k;
}

// ---------------------------------------------------------------------------
// Rows (names and values)
// ---------------------------------------------------------------------------

function splitName(n) {
  const i = n.lastIndexOf('.');
  return i < 0 ? ['', n] : [n.slice(0, i + 1), n.slice(i + 1)];
}

function renderRows() {
  const names = $('names');
  names.textContent = '';
  if (!S.rows.length) {
    const e = document.createElement('div');
    e.className = 'empty';
    e.innerHTML = 'No signals yet.<br>Pick a scope on the left and double-click signals, or drag them here.<br>From a shell: <code>tools/waveview.py add \'*valid*\'</code>';
    names.appendChild(e);
  }
  S.rows.forEach((r, i) => {
    const d = document.createElement('div');
    d.className = 'r' + (S.selected.has(r.name) ? ' on' : '');
    d.draggable = true;
    d.dataset.i = i;
    const [sc, leaf] = splitName(r.name);
    const sp1 = document.createElement('span'); sp1.className = 'sc'; sp1.textContent = '';
    const sp2 = document.createElement('span'); sp2.textContent = leaf;
    d.append(sp1, sp2);
    if (r.w > 1) { const g = document.createElement('span'); g.className = 'rg'; g.textContent = `[${r.w - 1}:0]`; d.append(g); }
    d.title = r.name + (r.w > 1 ? `[${r.w - 1}:0]` : '') + '  ·  ' + r.radix;
    names.appendChild(d);
  });
  renderValues();
  draw();
}

function renderValues() {
  const vals = $('values');
  vals.textContent = '';
  S.rows.forEach((r) => {
    const d = document.createElement('div');
    d.className = 'r' + (S.selected.has(r.name) ? ' on' : '');
    d.textContent = S.cursor == null ? '' : fmtVal(S.vals[r.name], r.w, r.radix);
    vals.appendChild(d);
  });
}

function renderReadout() {
  const cy = (t) => { const c = t === S.cursor && S.cursorCycle != null ? S.cursorCycle : cycleAt(t); return c == null ? '' : `  c${c}`; };
  $('cur').textContent = S.cursor == null ? '-' : fmtT(S.cursor) + cy(S.cursor);
  $('mrk').textContent = S.marker == null ? '-' : fmtT(S.marker) + cy(S.marker);
  if (S.cursor != null && S.marker != null) {
    const a = cycleAt(S.cursor), b = cycleAt(S.marker);
    $('dlt').textContent = fmtT(S.cursor - S.marker) + (a != null && b != null ? `  (${a - b} cyc)` : '');
  } else $('dlt').textContent = '-';
}

function renderNote() {
  const n = S.state && S.state.note;
  $('note').hidden = !n;
  if (n) { $('note-src').textContent = n.source; $('note-text').textContent = n.text; }
}

function renderStatus() {
  const st = S.state;
  if (!st) return;
  if (st.listeners > 0) setPill('st-agent', 'AI listening', 'ai');
  else if (st.inbox_pending > 0) setPill('st-agent', `${st.inbox_pending} message(s) waiting for the AI`, '');
  else setPill('st-agent', 'AI not listening', '');
}

function setPill(id, text, cls) { const e = $(id); e.textContent = text; e.className = 'pill ' + cls; }

let toastTimer = null;
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

// ---------------------------------------------------------------------------
// Hierarchy tree and signal list (GTKWave's SST)
// ---------------------------------------------------------------------------

let TREE = null;
function buildTree() {
  TREE = { name: '', path: '', kids: new Map(), sigs: [] };
  for (const [name, w] of S.all) {
    const parts = name.split('.');
    let node = TREE;
    for (let i = 0; i < parts.length - 1; i++) {
      let k = node.kids.get(parts[i]);
      if (!k) { k = { name: parts[i], path: parts.slice(0, i + 1).join('.'), kids: new Map(), sigs: [], open: false }; node.kids.set(parts[i], k); }
      node = k;
    }
    node.sigs.push([name, w]);
  }
  // open the first levels that have a single child, as GTKWave users expect
  let n = TREE;
  while (n.kids.size === 1 && n.sigs.length === 0) { n = [...n.kids.values()][0]; n.open = true; }
  if (!S.scope) S.scope = n === TREE ? null : n.path;
  renderTree();
  renderSigList();
}

function findNode(path) {
  if (!path) return TREE;
  let n = TREE;
  for (const p of path.split('.')) { n = n.kids.get(p); if (!n) return null; }
  return n;
}

function renderTree() {
  const box = $('tree');
  box.textContent = '';
  const walk = (node, depth) => {
    for (const k of [...node.kids.values()].sort((a, b) => a.name.localeCompare(b.name))) {
      const d = document.createElement('div');
      d.className = 'tnode' + (k.path === S.scope ? ' on' : '');
      d.style.paddingLeft = (6 + depth * 14) + 'px';
      const tw = document.createElement('span'); tw.className = 'tw'; tw.textContent = k.kids.size ? (k.open ? '▾' : '▸') : '';
      d.append(tw, document.createTextNode(k.name));
      tw.onclick = (e) => { e.stopPropagation(); k.open = !k.open; renderTree(); };
      d.onclick = () => { S.scope = k.path; S.pick.clear(); renderTree(); renderSigList(); };
      d.ondblclick = () => { k.open = !k.open; renderTree(); };
      box.appendChild(d);
      if (k.open) walk(k, depth + 1);
    }
  };
  walk(TREE, 0);
}

function globRe(p) {
  return new RegExp('^' + p.split('').map((c) => (c === '*' ? '.*' : c === '?' ? '.' : c.replace(/[.+^${}()|[\]\\]/g, '\\$&'))).join('') + '$', 'i');
}

let listed = [];
function renderSigList() {
  const box = $('siglist');
  box.textContent = '';
  const f = $('filter').value.trim();
  const node = findNode(S.scope) || TREE;
  if (!f) listed = node.sigs.slice();
  else {
    const prefix = S.scope ? S.scope + '.' : '';
    const re = /[*?]/.test(f) ? globRe(f) : null;
    const lf = f.toLowerCase();
    listed = S.all.filter(([n]) => n.startsWith(prefix) && (re ? re.test(n.slice(prefix.length)) || re.test(n.split('.').pop()) : n.toLowerCase().includes(lf)));
  }
  const LIMIT = 2000;
  const prefix = f ? (S.scope ? S.scope + '.' : '') : (S.scope ? S.scope + '.' : '');
  listed.slice(0, LIMIT).forEach(([n, w], i) => {
    const d = document.createElement('div');
    d.className = 'sig' + (S.pick.has(n) ? ' on' : '');
    d.draggable = true;
    const a = document.createElement('span'); a.textContent = n.startsWith(prefix) ? n.slice(prefix.length) : n;
    const b = document.createElement('span'); b.className = 'w'; b.textContent = w > 1 ? `[${w - 1}:0]` : '';
    d.append(a, b);
    d.title = n;
    d.onclick = (e) => {
      if (e.shiftKey && S.pickAnchor != null) {
        const [lo, hi] = [Math.min(S.pickAnchor, i), Math.max(S.pickAnchor, i)];
        for (let k = lo; k <= hi; k++) S.pick.add(listed[k][0]);
      } else if (e.ctrlKey || e.metaKey) { S.pick.has(n) ? S.pick.delete(n) : S.pick.add(n); S.pickAnchor = i; }
      else { S.pick = new Set([n]); S.pickAnchor = i; }
      renderSigList();
    };
    d.ondblclick = () => addRows([n]);
    d.ondragstart = (e) => {
      if (!S.pick.has(n)) { S.pick = new Set([n]); }
      e.dataTransfer.setData('text/x-wv-signals', JSON.stringify([...S.pick]));
      e.dataTransfer.effectAllowed = 'copy';
    };
    box.appendChild(d);
  });
  if (listed.length > LIMIT) {
    const m = document.createElement('div'); m.className = 'more';
    m.textContent = `… ${listed.length - LIMIT} more, narrow the filter`;
    box.appendChild(m);
  }
}

function addRows(names, index) {
  const have = new Set(S.rows.map((r) => r.name));
  const add = names.filter((n) => !have.has(n)).map((n) => ({ name: n, radix: 'hex', w: S.width.get(n) || 1 }));
  if (!add.length) return;
  if (index == null) S.rows.push(...add); else S.rows.splice(index, 0, ...add);
  renderRows(); pushRows(); requestData(true); fetchValues();
}

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------

const canvas = $('wave');
const ctx = canvas.getContext('2d');
function canvasW() { return Math.max(100, $('wavebox').clientWidth); }

function readColors() {
  const cs = getComputedStyle(document.documentElement);
  const g = (k) => cs.getPropertyValue(k).trim();
  S.colors = {
    bg: g('--wbg'), wave: g('--wave'), bus: g('--wbus'), fill: g('--wfill'), x: g('--wx'), xfill: g('--wxfill'),
    z: g('--wz'), text: g('--wtext'), grid: g('--grid'), edge: g('--edge'), cursor: g('--cursor'),
    marker: g('--marker'), ann: g('--ann'), ruler: g('--ruler'), rtext: g('--rtext'), rowsel: g('--rowsel'),
    dense: g('--dense'), line: g('--line'),
  };
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p;
  return 10 * p;
}

let drawPending = false;
function draw() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(() => { drawPending = false; drawNow(); });
}

function drawNow() {
  if (!S.colors) readColors();
  const C = S.colors;
  const box = $('wavebox');
  const W = box.clientWidth, H = box.clientHeight, dpr = window.devicePixelRatio || 1;
  if (canvas.width !== W * dpr || canvas.height !== H * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
  const [v0, v1] = S.view;
  const span = v1 - v0;
  const X = (t) => (t - v0) / span * W;
  const scrollTop = $('lists').scrollTop;
  const d = S.data;
  ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'middle';

  // clock edge gridlines
  let cycStep = 0;
  if (d && d.edges && d.edges.length > 1) {
    const per = (d.edges[d.edges.length - 1] - d.edges[0]) / (d.edges.length - 1);
    const ppx = per / span * W;
    if (ppx >= 5) {
      ctx.fillStyle = C.edge;
      for (const e of d.edges) { const x = X(e); if (x >= 0 && x <= W) ctx.fillRect(Math.round(x), RULER, 1, H - RULER); }
    }
    cycStep = niceStep(Math.max(1, 46 / Math.max(ppx, 1e-9)));
  }

  // rows
  ctx.save();
  ctx.beginPath(); ctx.rect(0, RULER, W, H - RULER); ctx.clip();
  const first = Math.max(0, Math.floor(scrollTop / ROW));
  const last = Math.min(S.rows.length - 1, Math.ceil((scrollTop + H) / ROW));
  for (let i = first; i <= last; i++) {
    const r = S.rows[i];
    const top = RULER + i * ROW - scrollTop;
    if (S.selected.has(r.name)) { ctx.fillStyle = C.rowsel; ctx.fillRect(0, top, W, ROW); }
    const sd = d && d.signals[r.name];
    if (sd) drawSignal(sd, r, top, W, X, C);
  }
  ctx.restore();

  // ruler
  ctx.fillStyle = C.ruler; ctx.fillRect(0, 0, W, RULER);
  ctx.fillStyle = C.line; ctx.fillRect(0, RULER - 1, W, 1);
  const step = niceStep(span / Math.max(1, W / 110));
  ctx.fillStyle = C.rtext; ctx.strokeStyle = C.rtext;
  ctx.textAlign = 'left';
  for (let t = Math.ceil(v0 / step) * step; t <= v1; t += step) {
    const x = Math.round(X(t)) + 0.5;
    ctx.fillRect(x - 0.5, 0, 1, 7);
    ctx.fillText(fmtT(t), x + 3, 8);
  }
  if (d && d.edges && cycStep) {
    ctx.textAlign = 'center';
    d.edges.forEach((e, k) => {
      const c = d.edge0 + k;
      if (c % cycStep) return;
      const x = X(e);
      if (x < -20 || x > W + 20) return;
      ctx.fillRect(Math.round(x), RULER - 6, 1, 6);
      ctx.fillText(String(c), x, RULER - 13);
    });
  }
  // annotations
  const st = S.state;
  if (st) {
    for (const a of st.annotations) {
      const x = Math.round(X(a.time)) + 0.5;
      if (x < 0 || x > W) continue;
      ctx.strokeStyle = C.ann; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(x, RULER); ctx.lineTo(x, H); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = C.ann; ctx.textAlign = 'left';
      const lbl = a.label || '●';
      const tw = ctx.measureText(lbl).width + 8;
      ctx.fillRect(x, RULER - 1, tw, 15);
      ctx.fillStyle = C.bg; ctx.fillText(lbl, x + 4, RULER + 6.5);
    }
  }
  // marker and cursor
  const vline = (t, col, dash) => {
    if (t == null) return;
    const x = Math.round(X(t)) + 0.5;
    if (x < 0 || x > W) return;
    ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.setLineDash(dash);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); ctx.setLineDash([]);
  };
  vline(S.marker, C.marker, [5, 4]);
  vline(S.cursor, C.cursor, []);
  if (S.cursor != null && S.marker != null) {
    const xa = X(S.marker), xb = X(S.cursor);
    if (Math.abs(xb - xa) > 30) {
      const y = H - 10, lo = Math.min(xa, xb), hi = Math.max(xa, xb);
      ctx.strokeStyle = C.marker; ctx.beginPath(); ctx.moveTo(lo, y); ctx.lineTo(hi, y); ctx.stroke();
      const a = cycleAt(S.cursor), b = cycleAt(S.marker);
      const txt = fmtT(Math.abs(S.cursor - S.marker)) + (a != null && b != null ? ` / ${Math.abs(a - b)} cyc` : '');
      ctx.textAlign = 'center';
      const tw = ctx.measureText(txt).width + 8;
      ctx.fillStyle = C.bg; ctx.fillRect((lo + hi) / 2 - tw / 2, y - 7, tw, 14);
      ctx.fillStyle = C.marker; ctx.fillText(txt, (lo + hi) / 2, y);
    }
  }
  // drag zoom box
  if (S.drag && S.drag.moved) {
    const lo = Math.min(S.drag.x0, S.drag.x1), hi = Math.max(S.drag.x0, S.drag.x1);
    ctx.fillStyle = 'rgba(78,161,255,0.15)'; ctx.fillRect(lo, 0, hi - lo, H);
    ctx.strokeStyle = 'rgba(78,161,255,0.8)'; ctx.strokeRect(lo + 0.5, 0.5, hi - lo, H - 1);
    ctx.fillStyle = C.rtext; ctx.textAlign = 'left';
    ctx.fillText(fmtT(xToT(lo)) + ' → ' + fmtT(xToT(hi)), lo + 4, RULER + 10);
  }
  renderReadout();
}

function drawSignal(sd, r, top, W, X, C) {
  const pts = [[S.data.t0, sd.v0], ...sd.ch];
  const hi = top + 4, lo = top + ROW - 5, mid = top + ROW / 2;
  // skip to the first segment that reaches the view
  let s = 0, e = pts.length - 1;
  const v0 = S.view[0];
  while (s < e) { const m = (s + e + 1) >> 1; if (pts[m][0] <= v0) s = m; else e = m - 1; }
  ctx.lineWidth = 1;
  for (let k = s; k < pts.length; k++) {
    const ta = pts[k][0], tb = k + 1 < pts.length ? pts[k + 1][0] : S.data.t1;
    let xa = X(ta), xb = X(tb);
    if (xa > W) break;
    if (xb < 0) continue;
    xa = Math.max(xa, -2); xb = Math.min(xb, W + 2);
    const v = pts[k][1];
    const isx = v.includes('x'), isz = !isx && v.includes('z');
    if (r.w === 1) {
      if (isx) { ctx.fillStyle = C.xfill; ctx.fillRect(xa, hi, xb - xa, lo - hi); ctx.strokeStyle = C.x; strokeLine(xa, hi, xb, hi); strokeLine(xa, lo, xb, lo); }
      else if (isz) { ctx.strokeStyle = C.z; strokeLine(xa, mid, xb, mid); }
      else {
        ctx.strokeStyle = C.wave;
        const y = v === '1' ? hi : lo;
        if (v === '1') { ctx.fillStyle = C.fill; ctx.fillRect(xa, hi, xb - xa, lo - hi); }
        strokeLine(xa, y, xb, y);
        if (k > s || ta > v0) {
          const pv = k > 0 ? pts[k - 1][1] : v;
          if (pv !== v) { ctx.strokeStyle = C.wave; strokeLine(Math.round(xa) + 0.5, hi, Math.round(xa) + 0.5, lo, true); }
        }
      }
    } else {
      const col = isx ? C.x : isz ? C.z : C.bus;
      const c = Math.min(3, (xb - xa) / 2);
      if (isz) { ctx.strokeStyle = col; strokeLine(xa, mid, xb, mid); }
      else {
        ctx.beginPath();
        ctx.moveTo(xa, mid); ctx.lineTo(xa + c, hi); ctx.lineTo(xb - c, hi); ctx.lineTo(xb, mid);
        ctx.lineTo(xb - c, lo); ctx.lineTo(xa + c, lo); ctx.closePath();
        ctx.fillStyle = isx ? C.xfill : C.fill; ctx.fill();
        ctx.strokeStyle = col; ctx.stroke();
      }
      const room = xb - xa - 8;
      if (room > 10) {
        let txt = fmtVal(v, r.w, r.radix);
        let tw = ctx.measureText(txt).width;
        if (tw > room) {
          const per = tw / txt.length;
          const keep = Math.floor(room / per) - 1;
          txt = keep > 0 ? txt.slice(0, keep) + '…' : '';
          tw = ctx.measureText(txt).width;
        }
        if (txt) {
          ctx.fillStyle = isx ? C.x : C.text; ctx.textAlign = 'left';
          // keep the label inside the visible part of the segment
          const left = Math.max(xa, 0) + 4, right = Math.min(xb, W) - 4 - tw;
          ctx.fillText(txt, Math.max(left, Math.min((xa + xb) / 2 - tw / 2, right)), mid + 0.5);
        }
      }
    }
  }
  // stretches with more changes than pixels
  ctx.fillStyle = C.dense;
  for (const [a, b] of sd.dense) {
    const xa = X(a), xb = Math.max(X(b), xa + 1);
    if (xb < 0 || xa > W) continue;
    ctx.fillRect(xa, hi, xb - xa, lo - hi);
  }
}

function strokeLine(x1, y1, x2, y2, exact) {
  ctx.beginPath();
  if (exact) { ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); }
  else { ctx.moveTo(x1, Math.round(y1) + 0.5); ctx.lineTo(x2, Math.round(y2) + 0.5); }
  ctx.stroke();
}

// ---------------------------------------------------------------------------
// View changes
// ---------------------------------------------------------------------------

function xToT(x) { return S.view[0] + x / canvasW() * (S.view[1] - S.view[0]); }

function setView(t0, t1, quiet) {
  const lo = S.meta ? S.meta.start : 0, hi = S.meta ? S.meta.end : 1;
  let span = Math.max(t1 - t0, 2);
  const full = (hi - lo) * 1.05 + 10;
  if (span > full) { span = full; t0 = lo - (full - (hi - lo)) / 2; }
  t0 = Math.max(lo - span * 0.5, Math.min(t0, hi - span * 0.5));
  S.view = [t0, t0 + span];
  requestData(); draw();
  if (!quiet) pushView();
}
function zoom(f, at) {
  const [a, b] = S.view;
  const c = at != null ? at : (S.cursor != null && S.cursor >= a && S.cursor <= b ? S.cursor : (a + b) / 2);
  setView(c - (c - a) * f, c + (b - c) * f);
}
function fit() { if (S.meta) setView(S.meta.start, S.meta.end); }
function ensureVisible(t) {
  const [a, b] = S.view, span = b - a;
  if (t < a + span * 0.02 || t > b - span * 0.02) setView(t - span / 2, t + span / 2);
}
function setCursor(t) {
  S.cursor = t; draw(); fetchValues();
  cmd('cursor', { time: t });
}
function setMarker(t) { S.marker = t; draw(); cmd('marker', { time: t }); }

async function edge(dir) {
  const name = S.rows.find((r) => S.selected.has(r.name))?.name;
  if (!name) { toast('Select a signal row first'); return; }
  const t0 = S.cursor != null ? S.cursor : S.view[0];
  const r = await api('/api/next_edge', { signal: name, t: t0, dir }).catch((e) => toast(e.message));
  if (!r || r.time == null) { toast(`No ${dir === 'next' ? 'later' : 'earlier'} change of ${name.split('.').pop()}`); return; }
  ensureVisible(r.time);
  setCursor(r.time);
}

async function search(dir) {
  const expr = $('find').value.trim();
  if (!expr) { $('find').focus(); return; }
  const res = await cmd('find', { expr, dir });
  $('find').classList.toggle('nohit', !!res && !res.found);
  if (res && !res.found) toast(`No ${dir === 'next' ? 'later' : 'earlier'} cycle where ${expr}`);
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

function rowAtY(y) {
  const i = Math.floor((y - RULER + $('lists').scrollTop) / ROW);
  return y >= RULER && i >= 0 && i < S.rows.length ? i : -1;
}

function selectRow(i, e) {
  if (i < 0) return;
  const n = S.rows[i].name;
  if (e && e.shiftKey && S.anchor != null) {
    S.selected = new Set();
    const [lo, hi] = [Math.min(S.anchor, i), Math.max(S.anchor, i)];
    for (let k = lo; k <= hi; k++) S.selected.add(S.rows[k].name);
  } else if (e && (e.ctrlKey || e.metaKey)) { S.selected.has(n) ? S.selected.delete(n) : S.selected.add(n); S.anchor = i; }
  else { S.selected = new Set([n]); S.anchor = i; }
  renderRows(); pushSelect();
}

canvas.addEventListener('mousedown', (e) => {
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  if (e.button === 1 || (e.button === 0 && e.shiftKey)) { e.preventDefault(); setMarker(Math.round(xToT(x))); return; }
  if (e.button !== 0) return;
  S.drag = { x0: x, x1: x, y, moved: false };
});
window.addEventListener('mousemove', (e) => {
  if (!S.drag) return;
  const rect = canvas.getBoundingClientRect();
  S.drag.x1 = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
  if (Math.abs(S.drag.x1 - S.drag.x0) > 5) S.drag.moved = true;
  draw();
});
window.addEventListener('mouseup', (e) => {
  const d = S.drag;
  if (!d) return;
  S.drag = null;
  if (d.moved) {
    const a = xToT(Math.min(d.x0, d.x1)), b = xToT(Math.max(d.x0, d.x1));
    setView(a, b);
  } else {
    const i = rowAtY(d.y);
    if (i >= 0 && !(e.ctrlKey || e.metaKey)) { if (!S.selected.has(S.rows[i].name) || S.selected.size > 1) selectRow(i, null); }
    else if (i >= 0) selectRow(i, e);
    setCursor(Math.round(xToT(d.x0)));
  }
  draw();
});
canvas.addEventListener('contextmenu', (e) => e.preventDefault());
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  if (e.ctrlKey || e.metaKey) {
    const f = Math.pow(1.0018, e.deltaY);
    zoom(Math.max(0.2, Math.min(5, f)), xToT(e.clientX - rect.left));
  } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    const dx = e.shiftKey ? e.deltaY || e.deltaX : e.deltaX;
    const dt = dx / canvasW() * (S.view[1] - S.view[0]);
    setView(S.view[0] + dt, S.view[1] + dt);
  } else {
    $('lists').scrollTop += e.deltaY;
  }
}, { passive: false });
$('lists').addEventListener('scroll', draw);
window.addEventListener('resize', () => { requestData(); draw(); });

// names pane: select, reorder, drop from the signal list, context menu
const namesEl = $('names');
namesEl.addEventListener('click', (e) => {
  const r = e.target.closest('.r'); if (!r) return;
  selectRow(+r.dataset.i, e);
});
namesEl.addEventListener('dragstart', (e) => {
  const r = e.target.closest('.r'); if (!r) return;
  const i = +r.dataset.i;
  if (!S.selected.has(S.rows[i].name)) selectRow(i, null);
  e.dataTransfer.setData('text/x-wv-rows', '1');
  e.dataTransfer.effectAllowed = 'move';
});
function dropIndex(e) {
  const r = e.target.closest('.r');
  if (!r || r.dataset.i == null) return S.rows.length;
  const box = r.getBoundingClientRect();
  return +r.dataset.i + (e.clientY > box.top + box.height / 2 ? 1 : 0);
}
function clearDropMarks() { namesEl.querySelectorAll('.dropbefore,.dropafter').forEach((x) => x.classList.remove('dropbefore', 'dropafter')); }
$('lists').addEventListener('dragover', (e) => {
  e.preventDefault();
  clearDropMarks();
  const i = dropIndex(e);
  const rows = namesEl.querySelectorAll('.r');
  if (i < rows.length) rows[i]?.classList.add('dropbefore'); else rows[rows.length - 1]?.classList.add('dropafter');
});
$('lists').addEventListener('dragleave', clearDropMarks);
$('lists').addEventListener('drop', (e) => {
  e.preventDefault(); clearDropMarks();
  const i = dropIndex(e);
  const sigs = e.dataTransfer.getData('text/x-wv-signals');
  if (sigs) { addRows(JSON.parse(sigs), i); return; }
  if (e.dataTransfer.getData('text/x-wv-rows')) {
    const moving = S.rows.filter((r) => S.selected.has(r.name));
    const before = S.rows.slice(0, i).filter((r) => !S.selected.has(r.name)).length;
    const rest = S.rows.filter((r) => !S.selected.has(r.name));
    rest.splice(before, 0, ...moving);
    S.rows = rest;
    renderRows(); pushRows();
  }
});
$('wavebox').addEventListener('dragover', (e) => e.preventDefault());
$('wavebox').addEventListener('drop', (e) => {
  e.preventDefault();
  const sigs = e.dataTransfer.getData('text/x-wv-signals');
  if (sigs) addRows(JSON.parse(sigs));
});

namesEl.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  const r = e.target.closest('.r'); if (!r) return;
  const i = +r.dataset.i;
  if (!S.selected.has(S.rows[i].name)) selectRow(i, null);
  const cur = S.rows[i].radix;
  const radix = (k, label) => ({ label, check: cur === k, fn: () => setRadix(k) });
  showMenu(e.clientX, e.clientY, [
    radix('hex', 'Hex'), radix('dec', 'Decimal'), radix('sdec', 'Signed decimal'), radix('bin', 'Binary'), radix('oct', 'Octal'),
    null,
    { label: 'Next edge  →', fn: () => edge('next') },
    { label: 'Previous edge  ←', fn: () => edge('prev') },
    { label: 'Search where this changes', fn: () => { $('find').value = `changed(${braced(S.rows[i].name)})`; search('next'); } },
    null,
    { label: 'Ask AI about this signal…', fn: () => openAsk(`What is ${S.rows[i].name} doing around the cursor?`) },
    { label: 'Copy full name', fn: () => navigator.clipboard?.writeText(S.rows[i].name) },
    { label: 'Remove', fn: removeSelected },
  ]);
});
function braced(n) { return /^[A-Za-z_][\w.]*$/.test(n) ? n : `{${n}}`; }

function showMenu(x, y, items) {
  const m = $('menu');
  m.textContent = '';
  for (const it of items) {
    const d = document.createElement('div');
    if (!it) d.className = 'sep';
    else { d.textContent = it.label; if (it.check) d.className = 'chk'; d.onclick = () => { m.hidden = true; it.fn(); }; }
    m.appendChild(d);
  }
  m.hidden = false;
  m.style.left = Math.min(x, innerWidth - 220) + 'px';
  m.style.top = Math.min(y, innerHeight - m.offsetHeight - 8) + 'px';
}
window.addEventListener('mousedown', (e) => { if (!e.target.closest('#menu')) $('menu').hidden = true; });

function setRadix(k) {
  for (const r of S.rows) if (S.selected.has(r.name)) r.radix = k;
  renderRows(); pushRows();
}
function removeSelected() {
  if (!S.selected.size) return;
  S.rows = S.rows.filter((r) => !S.selected.has(r.name));
  S.selected.clear();
  renderRows(); pushRows(); pushSelect(); requestData(true);
}
function moveSel(dir) {
  const idx = S.rows.map((r, i) => (S.selected.has(r.name) ? i : -1)).filter((i) => i >= 0);
  if (!idx.length) return;
  if (dir < 0 && idx[0] === 0) return;
  if (dir > 0 && idx[idx.length - 1] === S.rows.length - 1) return;
  const order = dir < 0 ? idx : idx.slice().reverse();
  for (const i of order) { const j = i + dir; [S.rows[i], S.rows[j]] = [S.rows[j], S.rows[i]]; }
  renderRows(); pushRows();
}

document.addEventListener('keydown', (e) => {
  const inField = e.target.matches('input, textarea');
  if (e.key === 'Escape') {
    $('menu').hidden = true;
    if (inField) { e.target.blur(); return; }
    S.selected.clear(); renderRows(); pushSelect(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); $('find').focus(); $('find').select(); return; }
  if (inField) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); S.selected = new Set(S.rows.map((r) => r.name)); renderRows(); pushSelect(); return; }
  if (e.ctrlKey || e.metaKey) return;
  const k = e.key;
  const sel = () => S.rows.findIndex((r) => S.selected.has(r.name));
  switch (k) {
    case 'f': case 'F': fit(); break;
    case 'z': zoom(2); break;
    case 'Z': case '+': case '=': zoom(0.5); break;
    case '-': case '_': zoom(2); break;
    case 'ArrowLeft': e.preventDefault(); S.selected.size ? edge('prev') : setView(S.view[0] - (S.view[1] - S.view[0]) * 0.1, S.view[1] - (S.view[1] - S.view[0]) * 0.1); break;
    case 'ArrowRight': e.preventDefault(); S.selected.size ? edge('next') : setView(S.view[0] + (S.view[1] - S.view[0]) * 0.1, S.view[1] + (S.view[1] - S.view[0]) * 0.1); break;
    case 'ArrowUp': e.preventDefault(); if (e.altKey) moveSel(-1); else selectRow(Math.max(0, sel() - 1), e.shiftKey ? e : null); break;
    case 'ArrowDown': e.preventDefault(); if (e.altKey) moveSel(1); else selectRow(Math.min(S.rows.length - 1, sel() < 0 ? 0 : sel() + 1), e.shiftKey ? e : null); break;
    case 'n': search('next'); break;
    case 'N': search('prev'); break;
    case 'c': if (S.cursor != null) setView(S.cursor - (S.view[1] - S.view[0]) / 2, S.cursor + (S.view[1] - S.view[0]) / 2); break;
    case 'm': if (S.cursor != null) setMarker(S.cursor); break;
    case 'Delete': case 'Backspace': removeSelected(); break;
    case 'h': setRadix('hex'); break;
    case 'd': setRadix('dec'); break;
    case 's': setRadix('sdec'); break;
    case 'b': setRadix('bin'); break;
    case 'o': setRadix('oct'); break;
    case '/': e.preventDefault(); $('find').focus(); break;
    case 'a': openAsk(); break;
    case '?': $('helpdlg').showModal(); break;
    default: return;
  }
});

$('find').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); search(e.shiftKey ? 'prev' : 'next'); }
});
$('find').addEventListener('input', () => $('find').classList.remove('nohit'));
$('filter').addEventListener('input', () => { S.pick.clear(); renderSigList(); });
$('filter').addEventListener('keydown', (e) => { if (e.key === 'Enter') addRows(listed.slice(0, 200).map((x) => x[0])); });

$('b-fit').onclick = fit;
$('b-zin').onclick = () => zoom(0.5);
$('b-zout').onclick = () => zoom(2);
$('b-prev').onclick = () => edge('prev');
$('b-next').onclick = () => edge('next');
$('b-append').onclick = () => addRows(pickOrder());
$('b-insert').onclick = () => {
  const i = S.rows.map((r, k) => (S.selected.has(r.name) ? k : -1)).filter((k) => k >= 0).pop();
  addRows(pickOrder(), i == null ? undefined : i + 1);
};
$('b-replace').onclick = () => {
  const i = S.rows.findIndex((r) => S.selected.has(r.name));
  const names = pickOrder();
  if (i < 0 || !names.length) { addRows(names); return; }
  S.rows = S.rows.filter((r) => !S.selected.has(r.name));
  S.selected.clear();
  addRows(names, Math.min(i, S.rows.length));
};
function pickOrder() { return listed.map((x) => x[0]).filter((n) => S.pick.has(n)); }
$('b-theme').onclick = () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('wv-theme', root.dataset.theme); } catch (e) { /* ignore */ }
  readColors(); draw();
};
$('b-help').onclick = () => $('helpdlg').showModal();
$('note-x').onclick = () => cmd('note', { text: null });

function openAsk(text) {
  $('ask-text').value = text || '';
  $('askdlg').showModal();
  $('ask-text').focus();
}
$('b-ask').onclick = () => openAsk();
$('askdlg').addEventListener('close', async () => {
  if ($('askdlg').returnValue !== 'send') return;
  const text = $('ask-text').value.trim();
  if (!text) return;
  const listening = S.state && S.state.listeners > 0;
  const r = await cmd('ask', { text });
  if (r) toast(listening ? 'Sent. The agent is listening.' : 'Sent. No agent is listening now; it will get it from `waveview.py inbox`.');
});
$('ask-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('askdlg').close('send'); }
});

// resizable hierarchy pane
$('split').addEventListener('mousedown', (e) => {
  e.preventDefault();
  const x0 = e.clientX, w0 = $('sst').offsetWidth;
  const mv = (ev) => { document.documentElement.style.setProperty('--sst-w', Math.max(120, w0 + ev.clientX - x0) + 'px'); draw(); };
  const up = () => { window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); requestData(); };
  window.addEventListener('mousemove', mv); window.addEventListener('mouseup', up);
});

// ---------------------------------------------------------------------------

(async function init() {
  try { const t = localStorage.getItem('wv-theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* ignore */ }
  readColors();
  await loadSignals();
  connect();
})();
