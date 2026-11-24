// waveview browser UI. Talks to waveview.py; the view state lives on the
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
  hits: [],            // clickable debug marks drawn last frame: {kind, index, x0, y0, x1, y1}
  arrowFrom: null,     // {signal, t} while placing an arrow from the context menu
  mouse: null,         // last mouse position on the canvas, for the arrow being placed
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
    T.cache.clear(); T.inflight.clear(); S.edges = null;
    loadSignals().then(() => { requestData(); fetchValues(); watchIndex(); });
    toast('Dump reloaded from disk');
  });
  es.addEventListener('reopen', () => location.reload());
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
  if (st.gen !== prevGen) { T.cache.clear(); S.edges = null; }
  if (S.meta && st.clock !== S.meta.clock) api('/api/meta').then((m) => { S.meta = m; S.edges = null; draw(); });
  if (st.last && !own && st.last.source !== 'ui') {
    const el = $('st-last');
    el.textContent = `${st.last.source}: ${st.last.op}`;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 2500);
  }
  renderNote();
  renderStatus();
  if (typeof followSource === 'function') followSource(st);
  if (typeof followLabels === 'function') followLabels(st);
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
  const base = S.meta.dump.split(/[\\/]/).pop();
  document.title = 'waveview · ' + base;
  $('title').textContent = base;
}

// ---------------------------------------------------------------------------
// Tiles: wave data is fetched just in time, only for the rows and the time
// range on screen. A tile of level L holds TB buckets of 2^L time units; the
// level is picked so that a bucket is about one pixel. Zoomed out, a tile is
// a summary (dense stretches with a change count and, for 1-bit signals, the
// share of 1s); zoomed in, it is the exact list of changes. A tile without
// dense stretches is exact, and any deeper zoom inside it reuses it with no
// transfer. While a tile is on its way, the coarser tile above it (already
// cached, since one usually zooms in from it) stands in.
// ---------------------------------------------------------------------------

const T = {
  cache: new Map(),   // key -> decoded tile, in LRU order
  inflight: new Set(),
  bytes: 0,           // bytes received for tiles (compressed, as sent)
  reqs: 0,
  missing: 0,         // tile slots drawn from a stand-in or not at all
  LIMIT: 30000,
};
const TB = () => (S.meta && S.meta.tile_buckets) || 1024;
const gen = () => (S.state ? S.state.gen : S.meta ? S.meta.gen : 0);
const tkey = (name, L, k) => gen() + '|' + name + '|' + L + '|' + k;
const tspan = (L) => TB() * Math.pow(2, L);
function levelFor(span, W) { return Math.max(0, Math.floor(Math.log2(Math.max(1, span / W)))); }
function maxLevel() { return S.meta ? Math.max(0, Math.ceil(Math.log2(Math.max(1, (S.meta.end + 1) / TB())))) : 0; }

function cacheGet(key, touch) {
  const v = T.cache.get(key);
  if (v && touch) { T.cache.delete(key); T.cache.set(key, v); }
  return v;
}
function cachePut(key, v) {
  T.cache.set(key, v);
  while (T.cache.size > T.LIMIT) T.cache.delete(T.cache.keys().next().value);
}

function decodeTile(raw, L, k) {
  const ts = k * tspan(L);
  const t = new Float64Array(raw.t.length);
  let acc = ts;
  for (let i = 0; i < raw.t.length; i++) { acc += raw.t[i]; t[i] = acc; }
  return { L, k, ts, te: ts + tspan(L), v0: raw.v0, t, v: raw.v, d: raw.d.map(([a, b, n, h]) => [a + ts, b + ts, n, h]), exact: !!raw.x };
}

// The tile to draw slot (L, k) from: its own, or an exact ancestor (both
// "fine"), else the nearest coarser ancestor as a stand-in.
function sourceFor(name, L, k) {
  const own = cacheGet(tkey(name, L, k), true);
  if (own) return { tile: own, fine: true };
  let coarse = null;
  const top = maxLevel();
  for (let up = L + 1; up <= top; up++) {
    const t = cacheGet(tkey(name, up, Math.floor(k / Math.pow(2, up - L))));
    if (!t) continue;
    if (t.exact) return { tile: t, fine: true };
    if (!coarse) coarse = t;
  }
  return coarse ? { tile: coarse, fine: false } : null;
}

function visibleRows() {
  const H = $('wavebox').clientHeight, top = $('lists').scrollTop;
  const first = Math.max(0, Math.floor(top / ROW) - 2);
  const last = Math.min(S.rows.length - 1, Math.ceil((top + H) / ROW) + 2);
  return S.rows.slice(first, last + 1);
}

function slots() {
  const W = canvasW(), [v0, v1] = S.view;
  const L = levelFor(v1 - v0, W), TS = tspan(L);
  const kmax = S.meta ? Math.floor(S.meta.end / TS) : 0;
  return { L, TS, k0: Math.max(0, Math.floor(v0 / TS)), k1: Math.max(0, Math.min(kmax, Math.floor(v1 / TS))), kmax };
}

let ensureTimer = null;
function requestData() {
  clearTimeout(ensureTimer);
  ensureTimer = setTimeout(ensureTiles, 50);
  draw();
}

function ensureTiles() {
  if (!S.meta) return;
  const { L, k0, k1, kmax } = slots();
  const want = {};
  let n = 0;
  // the visible slots first, then one slot of margin each side for panning
  const ks = [];
  for (let k = k0; k <= k1; k++) ks.push(k);
  if (k0 > 0) ks.push(k0 - 1);
  if (k1 < kmax) ks.push(k1 + 1);
  for (const r of visibleRows()) {
    for (const k of ks) {
      const key = tkey(r.name, L, k);
      if (T.inflight.has(key)) continue;
      const src = sourceFor(r.name, L, k);
      if (src && src.fine) continue;
      (want[r.name] = want[r.name] || []).push(k);
      T.inflight.add(key);
      n++;
    }
  }
  if (!n) return;
  // a few rows per request so the first rows arrive early
  const names = Object.keys(want);
  for (let i = 0; i < names.length; i += 12) {
    const part = {};
    for (const nm of names.slice(i, i + 12)) part[nm] = want[nm];
    fetchTiles(L, part);
  }
}

async function fetchTiles(L, want) {
  const g = gen();
  T.reqs++;
  renderNet();
  try {
    const r = await fetch('/api/tiles', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ L, want }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    T.bytes += +(r.headers.get('Content-Length') || 0);
    if (j.gen === g) {
      for (const [name, tiles] of Object.entries(j.tiles)) {
        for (const [k, raw] of Object.entries(tiles)) cachePut(tkey(name, L, +k), decodeTile(raw, L, +k));
      }
    }
  } catch (e) { toast(e.message); }
  finally {
    for (const [name, ks] of Object.entries(want)) for (const k of ks) T.inflight.delete(g + '|' + name + '|' + L + '|' + k);
    renderNet();
    draw();
  }
}

function valueIn(tile, a) {
  const t = tile.t;
  let lo = 0, hi = t.length - 1, k = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (t[m] <= a) { k = m; lo = m + 1; } else hi = m - 1; }
  return k < 0 ? tile.v0 : tile.v[k];
}

// The points and dense stretches of one row over the visible slots,
// stitched from whichever tile serves each slot.
function rowData(name, sl) {
  const pts = [], dense = [];
  let stand = 0;
  const push = (t, v) => { if (!pts.length || pts[pts.length - 1][1] !== v) pts.push([t, v]); };
  for (let k = sl.k0; k <= sl.k1; k++) {
    const a = k * sl.TS, b = a + sl.TS;
    const src = sourceFor(name, sl.L, k);
    if (!src) { push(a, '?'); stand++; continue; }
    if (!src.fine) stand++;
    const tile = src.tile;
    push(a, valueIn(tile, a));
    const t = tile.t;
    let lo = 0, hi = t.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (t[m] <= a) lo = m + 1; else hi = m; }
    // tile points are real changes, or openers of dense stretches whose value
    // may repeat the one before: keep them all
    for (let i = lo; i < t.length && t[i] < b; i++) pts.push([t[i], tile.v[i]]);
    for (const d of tile.d) if (d[1] >= a && d[0] < b) dense.push([Math.max(a, d[0]), Math.min(b, d[1]), d[2], d[3]]);
  }
  return { pts, dense, t1: (sl.k1 + 1) * sl.TS, stand };
}

// cycle numbers: computed locally for a regular clock, else fetched per view
function clockInfo() { return S.meta && S.meta.clockinfo; }
let edgeReq = null;
function edgesInView() {
  const ci = clockInfo();
  if (!ci) return null;
  const [v0, v1] = S.view;
  if (ci.regular) {
    const c0 = Math.max(0, Math.ceil((v0 - ci.first) / ci.period));
    const c1 = Math.min(ci.count - 1, Math.floor((v1 - ci.first) / ci.period));
    if (c1 - c0 > 4000) return { edge0: c0, edges: null, period: ci.period };
    const edges = [];
    for (let c = c0; c <= c1; c++) edges.push(ci.first + c * ci.period);
    return { edge0: c0, edges, period: ci.period };
  }
  const e = S.edges;
  if (e && e.t0 <= v0 && e.t1 >= v1 && e.gen === gen()) return e;
  if (!edgeReq) {
    const span = v1 - v0, t0 = v0 - span, t1 = v1 + span, g = gen();
    edgeReq = api('/api/edges', { t0, t1 }).then((r) => {
      S.edges = { ...r, t0, t1, gen: g, period: r.edges && r.edges.length > 1 ? (r.edges[r.edges.length - 1] - r.edges[0]) / (r.edges.length - 1) : null };
      edgeReq = null; draw();
    }).catch(() => { edgeReq = null; });
  }
  return e && e.gen === gen() ? e : null;
}

function renderNet() {
  const kb = T.bytes / 1024;
  const size = kb < 1024 ? kb.toFixed(1) + ' KB' : (kb / 1024).toFixed(2) + ' MB';
  $('st-net').textContent = `↓ ${size} · ${T.reqs} req · ${T.cache.size} tiles cached` + (T.inflight.size ? ` · loading ${T.inflight.size}` : '');
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
    if (SRC.open) srcAnnot();
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

// tile values: '0' '1' 'x' 'z' for 1-bit; hex, 'x', 'z' or '~bits' for buses
function encIsX(v) { return v === 'x' || (v[0] === '~' && v.includes('x')); }
function encIsZ(v) { return v === 'z' || (v[0] === '~' && !v.includes('x') && v.includes('z')); }
function fmtEnc(v, w, radix) {
  if (w === 1 || v === 'x' || v === 'z' || v === '?') return v;
  if (v[0] === '~') return fmtVal(v.slice(1), w, radix);
  const n = BigInt('0x' + v);
  switch (radix) {
    case 'dec': return n.toString();
    case 'sdec': return ((n >> BigInt(w - 1)) & 1n ? n - (1n << BigInt(w)) : n).toString();
    case 'bin': return n.toString(2).padStart(w, '0');
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
  const ci = clockInfo();
  if (!ci || t == null || t < ci.first) return null;
  if (ci.regular) return Math.min(ci.count - 1, Math.floor((t - ci.first) / ci.period));
  const e = S.edges;
  if (!e || !e.edges || t < e.t0 || t > e.t1) return null;
  let lo = 0, hi = e.edges.length - 1, k = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (e.edges[m] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
  return k < 0 ? null : e.edge0 + k;
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
    e.innerHTML = 'No signals yet.<br>Pick a scope on the left and double-click signals, or drag them here.<br>From a shell: <code>waveview.py add \'*valid*\'</code>';
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

// the source index being built shows in the status bar until it is ready
let srcWatch = null;
function watchIndex() {
  if (srcWatch) return;
  const tick = async () => {
    let r;
    try { r = await api('/api/src/status'); } catch (e) { srcWatch = null; return; }
    const st = r.status || '';
    const pill = $('st-src');
    if (r.ready) {
      pill.hidden = true; srcWatch = null;
      if (st && !st.startsWith('not indexed')) toast('source index ready: ' + st);
      return;
    }
    if (st.startsWith('indexing')) { pill.hidden = false; setPill('st-src', 'source: ' + st, 'busy'); srcWatch = setTimeout(tick, 1500); return; }
    if (st.startsWith('index failed')) { pill.hidden = false; setPill('st-src', 'source: ' + st, 'bad'); pill.title = st; srcWatch = null; return; }
    pill.hidden = true; srcWatch = null;  // not indexed: nothing to wait for
  };
  srcWatch = setTimeout(tick, 300);
}

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
  for (const entry of S.all) {  // [name, width, kind]
    const parts = entry[0].split('.');
    let node = TREE;
    for (let i = 0; i < parts.length - 1; i++) {
      let k = node.kids.get(parts[i]);
      if (!k) { k = { name: parts[i], path: parts.slice(0, i + 1).join('.'), kids: new Map(), sigs: [], open: false }; node.kids.set(parts[i], k); }
      node = k;
    }
    node.sigs.push(entry);
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
      d.oncontextmenu = (e) => treeMenu(e, k);
      d.title = k.path + '  (right-click: open its module or its instantiation in the source)';
      box.appendChild(d);
      if (k.open) walk(k, depth + 1);
    }
  };
  walk(TREE, 0);
}

// right-click on a hierarchy node: its module in the source, or where the parent instantiates it
async function treeMenu(e, k) {
  e.preventDefault();
  S.scope = k.path; S.pick.clear(); renderTree(); renderSigList();
  let w;
  try { w = await api('/api/src/where?scope=' + encodeURIComponent(k.path)); } catch (err) {
    showMenu(e.clientX, e.clientY, [{ label: 'No source for ' + k.name + ': ' + err.message, fn: () => {} }]);
    return;
  }
  const items = [{ label: `Open module ${w.module}  (${w.file.split('/').pop()}:${w.line})`, fn: () => srcOpen(k.path, w.line, { fileOf: w.file }) }];
  if (w.parent) {
    items.push({ label: `Go to instance ${w.instance} in ${w.parent_module}  (${w.inst_file.split('/').pop()}:${w.inst_line})`,
                 fn: () => srcOpen(w.parent, w.inst_line, { sel: w.instance, fileOf: w.inst_file }) });
  }
  items.push(null, { label: 'Copy path ' + k.path, fn: () => navigator.clipboard && navigator.clipboard.writeText(k.path) });
  showMenu(e.clientX, e.clientY, items);
}

// ---------------------------------------------------------------------------
// Block diagram of a scope: boxes for instances (sized by what they hold),
// their interface bundles, and the connections between them (/api/diagram)
// ---------------------------------------------------------------------------

const DG = { scope: null, data: null, mode: 'diagram', metric: 'signals', tree: null };
const PROTO_COLOR = {
  'AXI4': '#4ea1ff', 'AXI4-Lite': '#7cc0ff', 'AXI4-Stream': '#2fbfa8', 'APB': '#4cb944', 'AHB': '#9bb43a',
  'CHI': '#a86ee8', 'TileLink': '#f0932b', 'Wishbone': '#b5651d', 'Avalon-MM': '#e06ca8',
  'valid/ready': '#7f8fa6', 'req/ack': '#8d99ae', 'clock': '#666', 'reset': '#666',
};
function protoColor(p) { return PROTO_COLOR[p] || '#8b929c'; }

async function showDiagram(scope, mode) {
  if (scope === undefined || scope === null) scope = DG.scope !== null && $('diagdlg').open ? DG.scope : (S.scope || '');
  if (mode) DG.mode = mode;
  $('dg-m-diagram').classList.toggle('primary', DG.mode === 'diagram');
  $('dg-m-treemap').classList.toggle('primary', DG.mode === 'treemap');
  $('dg-metric').hidden = DG.mode !== 'treemap';
  $('dg-ask').hidden = DG.mode === 'treemap';
  if (DG.mode === 'treemap') return showTreemap(scope);
  let d;
  try { d = await api('/api/diagram?scope=' + encodeURIComponent(scope)); } catch (e) { toast(e.message); return; }
  DG.scope = d.scope; DG.data = d;
  renderDiagram(d);
  // not modal: the context menu and the toasts live outside the dialog
  if (!$('diagdlg').open) $('diagdlg').show();
}

// -- treemap: the hierarchy below a scope, each scope as large as what it holds
async function showTreemap(scope) {
  let t;
  try { t = await api('/api/tree?scope=' + encodeURIComponent(scope) + '&depth=4&limit=60'); } catch (e) { toast(e.message); return; }
  DG.scope = scope; DG.tree = t;
  if (!$('diagdlg').open) $('diagdlg').show();  // open first: the treemap fills the pane's real size
  renderTreemap(t);
}
window.addEventListener('resize', () => { if ($('diagdlg').open && DG.mode === 'treemap' && DG.tree) renderTreemap(DG.tree); });

function dgCrumbs(scope, module) {
  const crumbs = $('dg-crumbs');
  crumbs.textContent = '';
  const comps = scope ? scope.split('.') : [];
  const top = document.createElement('a'); top.textContent = '(top)'; top.onclick = () => showDiagram('');
  crumbs.append(top);
  comps.forEach((c, i) => {
    crumbs.append(' . ');
    const a = document.createElement('a'); a.textContent = c;
    const sc = comps.slice(0, i + 1).join('.');
    a.onclick = () => showDiagram(sc);
    crumbs.append(a);
  });
  if (module) { const m = document.createElement('span'); m.className = 'mod'; m.textContent = '(' + module + ')'; crumbs.append(m); }
  $('dg-up').disabled = !scope;
}

// squarified treemap: rows of rectangles whose aspect ratio stays near 1
function squarify(items, x, y, w, h) {
  const out = [];
  const total = items.reduce((s, it) => s + it.value, 0);
  if (!total || w <= 0 || h <= 0) return out;
  let rest = items.slice();
  let cx = x, cy = y, cw = w, ch = h, remaining = total;
  while (rest.length) {
    const horizontal = cw >= ch;  // lay a row along the shorter side
    const side = horizontal ? ch : cw;
    let row = [], rowSum = 0, worst = Infinity;
    while (rest.length) {
      const it = rest[0];
      const sum = rowSum + it.value;
      const area = (cw * ch) * sum / remaining;
      const thick = area / side;
      let wst = 0;
      for (const r of row.concat([it])) { const len = (cw * ch) * r.value / remaining / thick; wst = Math.max(wst, thick / len, len / thick); }
      if (row.length && wst > worst) break;
      row.push(it); rowSum = sum; worst = wst; rest.shift();
    }
    const thick = (cw * ch) * rowSum / remaining / side;
    let off = 0;
    for (const r of row) {
      const len = (cw * ch) * r.value / remaining / thick;
      out.push(horizontal ? { it: r, x: cx, y: cy + off, w: thick, h: len } : { it: r, x: cx + off, y: cy, w: len, h: thick });
      off += len;
    }
    if (horizontal) { cx += thick; cw -= thick; } else { cy += thick; ch -= thick; }
    remaining -= rowSum;
  }
  return out;
}

function tmValue(n) {
  if (DG.metric === 'scopes') return n.scopes + 1;
  if (DG.metric === 'loc') return n.loc || (n.kids.length ? n.kids.reduce((s, k) => s + tmValue(k), 0) : 1);
  return n.signals;
}

function tmColor(name, depth) {
  let h = 0;
  for (const c of (name || '')) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const hue = h % 360;
  return `hsl(${hue} 45% ${Math.max(22, 48 - depth * 7)}%)`;
}

function renderTreemap(t) {
  const svg = $('dg-svg');
  svg.textContent = '';
  dgCrumbs(t.scope, t.module);
  const box = $('dg-box');
  const W = Math.max(600, box.clientWidth - 4), H = Math.max(400, box.clientHeight - 4);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('height', H);
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); (parent || svg).appendChild(e); return e; };
  const fmt = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
  const metricName = { signals: 'sig', scopes: 'scopes', loc: 'lines' }[DG.metric];
  $('dg-msg').textContent = `${fmt(t.signals)} signals · ${t.scopes} scopes below${t.module ? ' · ' + t.module : ''}`;
  const draw = (node, x, y, w, h, depth, parent) => {
    const items = node.kids.map((k) => ({ value: Math.max(tmValue(k), 1e-9), node: k })).filter((it) => it.value > 0);
    const pad = depth === 0 ? 0 : 3, head = depth === 0 ? 0 : (h > 34 && w > 60 ? 16 : 0);
    const inner = { x: x + pad, y: y + pad + head, w: w - 2 * pad, h: h - 2 * pad - head };
    if (!items.length || inner.w < 8 || inner.h < 8) return;
    for (const r of squarify(items, inner.x, inner.y, inner.w, inner.h)) {
      const k = r.it.node;
      const leaf = !k.kids.length || r.w < 40 || r.h < 30;
      const g = el('g', { class: 'tm ' + (leaf ? 'leaf' : 'branch') + (k.scope && k.scope === S.scope ? ' on' : '') }, parent);
      el('rect', { x: r.x, y: r.y, width: r.w, height: r.h, fill: tmColor(k.module || k.name, depth) }, g);
      const label = k.name + (k.module && k.module !== k.name ? ' · ' + k.module : '');
      const sub = `${fmt(k.signals)} sig · ${k.scopes} scopes${k.loc ? ' · ' + fmt(k.loc) + ' lines' : ''}`;
      if (r.w > 50 && r.h > 16) {
        const tx = el('text', { x: r.x + 5, y: r.y + 13 }, g); tx.textContent = label.length > r.w / 6.5 ? label.slice(0, Math.max(3, r.w / 6.5 - 1)) + '…' : label;
        if (r.h > 30 && !(!leaf && r.h > 34)) { const t2 = el('text', { x: r.x + 5, y: r.y + 26, class: 'sub' }, g); t2.textContent = sub; }
        else if (!leaf && r.w > 120) { const t2 = el('text', { x: r.x + r.w - 5, y: r.y + 13, class: 'sub', 'text-anchor': 'end' }, g); t2.textContent = sub; }
      }
      const title = el('title', {}, g); title.textContent = `${k.scope || k.name}\n${sub}${k.module ? '\nmodule ' + k.module : ''}`;
      if (k.scope) {
        g.querySelector('rect').addEventListener('click', (e) => { e.stopPropagation(); if (leaf || e.altKey) { S.scope = k.scope; S.pick.clear(); renderTree(); renderSigList(); showTreemap(k.scope); } else { S.scope = k.scope; S.pick.clear(); renderTree(); renderSigList(); showTreemap(k.scope); } });
        g.querySelector('rect').addEventListener('dblclick', (e) => { e.stopPropagation(); showDiagram(k.scope, 'diagram'); });
        g.querySelector('rect').addEventListener('contextmenu', (e) => treeMenu(e, { path: k.scope, name: k.name }));
      }
      if (!leaf) draw(k, r.x, r.y, r.w, r.h, depth + 1, g);
    }
  };
  draw(t, 0, 0, W, H, 0, svg);
}

$('dg-m-diagram').onclick = () => showDiagram(DG.scope, 'diagram');
$('dg-m-treemap').onclick = () => showDiagram(DG.scope, 'treemap');
$('dg-metric').onchange = () => { DG.metric = $('dg-metric').value; if (DG.tree) renderTreemap(DG.tree); };

function renderDiagram(d) {
  const svg = $('dg-svg');
  svg.textContent = '';
  dgCrumbs(d.scope, d.module);
  $('dg-msg').textContent = d.index ? `${d.boxes.length} instances · ${d.edges.length} connections` : `${d.boxes.length} scopes · no source index, so no connections (start with --rtl or choose the RTL in Open…)`;

  // nodes: the boxes, plus the scope's own ports as an entry and an exit node
  const nodes = new Map();
  const inB = d.ports.filter((b) => b.dir !== 'out');
  const outB = d.ports.filter((b) => b.dir === 'out');
  if (d.ports.length) {
    nodes.set('', { name: '', kind: 'port', in: [], out: inB, label: d.scope ? 'ports in' : '', rank: 0 });
    nodes.set('\u0000out', { name: '\u0000out', kind: 'port', in: outB, out: [], label: 'ports out', rank: null });
  }
  for (const b of d.boxes) {
    const left = b.bundles.filter((x) => x.dir === 'in' || x.dir === 'mixed' || !x.dir);
    const right = b.bundles.filter((x) => x.dir === 'out');
    nodes.set(b.name, { name: b.name, kind: (b.in_dump ? 'box' : 'ghost') + (b.iface ? ' iface' : ''), box: b, in: left, out: right, rank: null });
  }
  const edges = d.edges.map((e) => ({ ...e, to: e.to === '' ? '\u0000out' : e.to }));
  const RESPONSE = new Set(['slave', 'sink', 'completer', 'manager', 'agent', 'responder']);
  const isPlumbing = (e) => e.protocol === 'clock' || e.protocol === 'reset';
  // ranks: longest path from the sources along the request direction (a
  // slave's response edge and the clock / reset fan-out do not place a box)
  const preds = new Map([...nodes.keys()].map((k) => [k, []]));
  for (const e of edges) {
    if (!nodes.has(e.from) || !nodes.has(e.to) || e.from === e.to || isPlumbing(e) || RESPONSE.has(e.role)) continue;
    preds.get(e.to).push(e.from);
  }
  const state = new Map();
  const rankOf = (k) => {
    const n = nodes.get(k);
    if (n.rank !== null) return n.rank;
    if (state.get(k) === 1) return 1;  // a cycle: stop here
    state.set(k, 1);
    let r = 1;
    for (const p of preds.get(k)) r = Math.max(r, rankOf(p) + 1);
    n.rank = r; state.set(k, 2);
    return r;
  };
  for (const k of nodes.keys()) if (k !== '\u0000out') rankOf(k);
  let maxRank = 1;
  for (const n of nodes.values()) if (n.name !== '\u0000out' && n.rank > maxRank) maxRank = n.rank;
  if (nodes.has('\u0000out')) nodes.get('\u0000out').rank = maxRank + 1;
  // sizes
  for (const n of nodes.values()) {
    const rows = Math.max(1, n.in.length, n.out.length);
    const sig = n.box ? n.box.signals : 0;
    n.w = n.kind === 'port' ? 150 : 200;
    n.h = Math.max(44, 38 + 13 * rows + (n.kind === 'port' ? 0 : 20)) + (sig ? Math.min(60, 7 * Math.log2(1 + sig)) : 0);
  }
  // columns, ordered so that connected boxes sit side by side: the
  // barycentre of the neighbours, swept left to right and back a few times
  // (a pair like hif0 → ch0, hif1 → ch1 then lines up), names breaking ties
  const cols = [];
  for (const n of nodes.values()) (cols[n.rank] = cols[n.rank] || []).push(n);
  const succs = new Map([...nodes.keys()].map((k) => [k, []]));
  for (const [k, ps] of preds) for (const p of ps) succs.get(p).push(k);
  const order = new Map();  // name -> position in its column
  const place = (col) => col.forEach((n, i) => order.set(n.name, i));
  cols.forEach((col) => { col.sort((a, b) => (b.box ? b.box.signals : 0) - (a.box ? a.box.signals : 0) || a.name.localeCompare(b.name)); place(col); });
  const bary = (n, side) => { const ns = side.get(n.name).map((p) => order.get(p)).filter((v) => v !== undefined); return ns.length ? ns.reduce((s, v) => s + v, 0) / ns.length : null; };
  const sweep = (side, cs) => {
    for (const col of cs) {
      if (!col) continue;
      const keyed = col.map((n, i) => [bary(n, side), i, n]);
      keyed.sort((a, b) => ((a[0] ?? a[1]) - (b[0] ?? b[1])) || a[2].name.localeCompare(b[2].name));
      col.length = 0; col.push(...keyed.map((k) => k[2])); place(col);
    }
  };
  for (let pass = 0; pass < 3; pass++) {
    sweep(preds, cols.slice(1));
    sweep(succs, cols.slice(0, -1).reverse());
  }
  sweep(preds, cols.slice(1));
  const yOf = new Map();
  cols.forEach((col) => {
    let y = 24;
    for (const n of col) { n.y = y; yOf.set(n.name, y + n.h / 2); y += n.h + 22; }
  });
  const colX = [];
  let x = 24;
  cols.forEach((col, r) => { colX[r] = x; if (col) x += Math.max(...col.map((n) => n.w)) + 190; });
  for (const n of nodes.values()) n.x = colX[n.rank];
  const W = x + 24, H = Math.max(200, ...[...nodes.values()].map((n) => n.y + n.h + 24));
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W); svg.setAttribute('height', H);
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); (parent || svg).appendChild(e); return e; };
  const text = (parent, x, y, s, cls, anchor) => { const t = el('text', { x, y, class: cls || '', 'text-anchor': anchor || 'start' }, parent); t.textContent = s; return t; };
  const stubY = (n, side, bundle) => { const list = side === 'in' ? n.in : n.out; const i = Math.max(0, list.indexOf(bundle)); return n.y + 38 + (n.kind === 'port' ? 0 : 14) + i * 13; };
  const bundleOf = (n, side, name) => (side === 'in' ? n.in : n.out).find((b) => b.name === name);
  // edges first, under the boxes; labels of the edges between the same two
  // boxes are spread along the way so that they do not pile up, and only
  // the three widest of a pair get one (the rest say it on hover)
  const pairSeen = new Map();
  const pairCount = new Map();
  for (const e of edges) { const pk = [e.from, e.to].sort().join('\u0001'); pairCount.set(pk, (pairCount.get(pk) || 0) + 1); }
  const pairRank = new Map();
  for (const e of [...edges].sort((a, b) => b.width - a.width)) { const pk = [e.from, e.to].sort().join('\u0001'); const r = pairRank.get(pk) || 0; e._rank = r; pairRank.set(pk, r + 1); }
  for (const e of edges) {
    const a = nodes.get(e.from), b = nodes.get(e.to);
    if (!a || !b) continue;
    const fb = bundleOf(a, 'out', e.bundle) || a.out[0], tb = b.in.find((x) => e.nets.some((nt) => x.signals.includes(nt[2]))) || b.in[0];
    const x1 = a.x + a.w, y1 = fb ? stubY(a, 'out', fb) : a.y + a.h / 2;
    const x2 = b.x, y2 = tb ? stubY(b, 'in', tb) : b.y + b.h / 2;
    const g = el('g', { class: 'edge' + (isPlumbing(e) ? ' plumbing' : '') });
    const sw = isPlumbing(e) ? 1 : 1 + Math.min(5, Math.log2(1 + e.width) / 2);
    const mx = (x1 + x2) / 2;
    el('path', { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, stroke: protoColor(e.protocol), 'stroke-width': sw.toFixed(1) }, g);
    if (!isPlumbing(e) && e._rank < 3) {
      const pk = [a.name, b.name].sort().join('\u0001');
      const k = pairSeen.get(pk) || 0;
      pairSeen.set(pk, k + 1);
      const t = 0.5 + (k % 3 - 1) * 0.2;  // 0.3, 0.5, 0.7 along the edge, and a line lower each time
      const lx = x1 + (x2 - x1) * t, ly = y1 + (y2 - y1) * t - 4 + k * 13;
      const label = (e.label ? `“${e.label}” ` : '') + (e.protocol || (e.label ? '' : e.bundle)) + (e.role ? ' ' + e.role : '') + ` ${e.width}b`;
      text(g, lx, ly, label.replace(/\s+/g, ' ').trim(), '', 'middle');
    }
    g.addEventListener('click', () => {
      const pre = d.scope ? d.scope + '.' : '';
      const names = e.nets.map((nt) => (e.from ? pre + e.from + '.' + nt[0] : pre + nt[1])).filter((n) => S.width.has(n));
      if (!names.length) { toast('none of these signals is in the dump'); return; }
      addRows(names);
      toast(`added ${names.length} signals of ${e.bundle}`);
    });
    const title = el('title', {}, g);
    title.textContent = e.nets.map((nt) => `${e.from || '(ports)'}.${nt[0]}  →  ${nt[1]}  →  ${e.to === '\u0000out' ? '(ports)' : e.to}.${nt[2]}`).join('\n');
  }
  // boxes
  for (const n of nodes.values()) {
    const g = el('g', { class: 'box ' + n.kind + (n.box && n.box.scope === S.scope ? ' on' : ''), transform: `translate(${n.x},${n.y})` });
    el('rect', { width: n.w, height: n.h }, g);
    const stubCls = (b) => 'stub' + (b.protocol || b.label ? ' proto' : ' unknown');
    const stub = (x, y, b, anchor, boxName, txt) => { const t = text(g, x, y, txt, stubCls(b), anchor); t.addEventListener('contextmenu', (e) => bundleMenu(e, boxName, b)); return t; };
    if (n.kind === 'port') {
      text(g, n.w / 2, 18, n.label, 'name', 'middle');
      n.in.forEach((b, i) => stub(8, 38 + i * 13, b, 'start', '', bundleLabel(b)));
      n.out.forEach((b, i) => stub(n.w - 8, 38 + i * 13, b, 'end', '', bundleLabel(b)));
    } else {
      const b = n.box;
      text(g, 10, 18, b.name, 'name');
      text(g, 10, 32, (b.module ? b.module + (b.iface ? ' (interface)' : '') + '  ·  ' : '') + `${b.signals} sig · ${b.insts} inst` + (b.loc ? ` · ${b.loc} lines` : '') + (b.in_dump ? '' : ' · not dumped'), 'dim');
      n.in.forEach((x, i) => stub(8, 52 + i * 13, x, 'start', b.name, '▸ ' + bundleLabel(x)));
      n.out.forEach((x, i) => stub(n.w - 8, 52 + i * 13, x, 'end', b.name, bundleLabel(x) + ' ▸'));
      g.addEventListener('click', () => { S.scope = b.scope; S.pick.clear(); renderTree(); renderSigList(); for (const o of svg.querySelectorAll('.box.on')) o.classList.remove('on'); g.classList.add('on'); });
      g.addEventListener('dblclick', () => showDiagram(b.scope));
      g.addEventListener('contextmenu', (e) => treeMenu(e, { path: b.scope, name: b.name }));
      const title = el('title', {}, g);
      title.textContent = b.scope + '\n' + b.bundles.map((x) => bundleLabel(x) + ': ' + x.signals.join(', ')).join('\n');
    }
  }
}

function bundleLabel(b) {
  let tag = b.protocol && b.protocol !== 'clock' && b.protocol !== 'reset' ? `${b.protocol}${b.role ? ' ' + b.role : ''}` : '';
  if (b.label) tag = `“${b.label}”` + (tag ? ' ' + tag : '');  // what a person or the agent called it
  const name = b.protocol && b.name.toLowerCase() === b.protocol.toLowerCase() ? '' : b.name;  // "apb APB" says it once
  return [name, tag, `${b.width}b`].filter(Boolean).join(' ');
}

// right-click on a bundle: name it, ask the agent, or add its signals
function bundleMenu(e, boxName, b) {
  e.preventDefault(); e.stopPropagation();
  const key = boxName + '/' + b.name;
  const pre = (DG.scope ? DG.scope + '.' : '') + (boxName ? boxName + '.' : '');
  const names = b.signals.map((s) => pre + s).filter((n) => S.width.has(n));
  const items = [
    { label: `Name ${b.name}…`, fn: async () => {
      const text = prompt(`What is ${key}?  (${b.signals.slice(0, 6).join(', ')}${b.signals.length > 6 ? ', …' : ''})`, b.label || '');
      if (text === null) return;
      await cmd('label', { scope: DG.scope, bundle: key, text, source: 'ui' });
      showDiagram(DG.scope);
    } },
    { label: `Ask AI what ${b.name} is`, fn: () => askLabels([key]) },
  ];
  if (b.label) items.push({ label: 'Remove the name', fn: async () => { await cmd('label', { scope: DG.scope, bundle: key, clear: true }); showDiagram(DG.scope); } });
  items.push(null, { label: `Add its ${names.length} signals to the waveform`, fn: () => { if (names.length) { addRows(names); toast(`added ${names.length} signals`); } else toast('none of these signals is in the dump'); } });
  showMenu(e.clientX, e.clientY, items);
}

// the unidentified bundles of the scope, to the agent: it answers with `waveview.py label`
function askLabels(keys) {
  const d = DG.data;
  if (!d) return;
  const all = [];
  for (const b of d.boxes) for (const bd of b.bundles) all.push([b.name + '/' + bd.name, bd, b.name]);
  for (const bd of d.ports) all.push(['/' + bd.name, bd, '']);
  const want = keys ? all.filter(([k]) => keys.includes(k)) : all.filter(([k]) => d.unlabelled.includes(k));
  if (!want.length) { toast('every interface here is identified'); return; }
  const lines = want.map(([k, bd, box]) => `- ${k}: ${bd.signals.join(', ')}${bd.dir ? '  (' + bd.dir + ')' : ''}`);
  const text = `In scope ${d.scope || '(top)'}${d.module ? ' (module ' + d.module + ')' : ''} the block diagram has interface bundles that the signal names did not identify. ` +
    `Tell what each one is (a bus protocol, a custom interface, a group of controls...) from the signals and the source, and name it with:\n` +
    `  waveview.py label '${d.scope}' <box/bundle> "<what it is>"\n${lines.join('\n')}`;
  cmd('ask', { text }).then((r) => { if (r) toast(S.state && S.state.listeners > 0 ? 'Asked. The agent is listening.' : 'Asked. No agent is listening now; it will get it from `waveview.py inbox`.'); });
}

// labels given while the diagram is open (by the agent) show up at once
let lastLabelsJson = null;
function followLabels(st) {
  const j = JSON.stringify(st.iflabels || {});
  if (lastLabelsJson !== null && j !== lastLabelsJson && $('diagdlg').open && DG.scope !== null) showDiagram(DG.scope);
  lastLabelsJson = j;
}

$('b-diag').onclick = () => showDiagram();
$('dg-ask').onclick = () => askLabels(null);
$('dg-close').onclick = () => $('diagdlg').close();
$('diagdlg').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); $('diagdlg').close(); } });
$('dg-up').onclick = () => { if (DG.scope) showDiagram(DG.scope.split('.').slice(0, -1).join('.')); };

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
  listed.slice(0, LIMIT).forEach(([n, w, kind], i) => {
    const d = document.createElement('div');
    d.className = 'sig' + (S.pick.has(n) ? ' on' : '');
    d.draggable = true;
    const a = document.createElement('span'); a.textContent = n.startsWith(prefix) ? n.slice(prefix.length) : n;
    // the declared type and direction, as the dump says: "out logic", "wire"
    const k = document.createElement('span'); k.className = 'k';
    const dir = (kind || '').split(' ')[0];
    if (dir === 'in' || dir === 'out' || dir === 'inout') k.classList.add(dir);
    k.textContent = kind || '';
    const b = document.createElement('span'); b.className = 'w'; b.textContent = w > 1 ? `[${w - 1}:0]` : '';
    const right = document.createElement('span'); right.className = 'r'; right.append(k, b);
    d.append(a, right);
    d.title = n + (kind ? '  (' + kind + ')' : '');
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
  renderRows(); pushRows(); requestData(); fetchValues();
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
    dense: g('--dense'), line: g('--line'), loading: g('--loading'), arrow: g('--arrow'), comment: g('--comment'),
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
  const E = edgesInView();
  const sl = slots();
  ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'middle';

  // clock edge gridlines
  let cycStep = 0;
  if (E && E.edges && E.period) {
    const ppx = E.period / span * W;
    if (ppx >= 5) {
      ctx.fillStyle = C.edge;
      for (const e of E.edges) { const x = X(e); if (x >= 0 && x <= W) ctx.fillRect(Math.round(x), RULER, 1, H - RULER); }
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
    drawSignal(rowData(r.name, sl), r, top, W, X, C);
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
  if (E && E.edges && cycStep) {
    ctx.textAlign = 'center';
    E.edges.forEach((e, k) => {
      const c = E.edge0 + k;
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
  drawMarks(st, W, H, X, C, scrollTop);
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

// ---------------------------------------------------------------------------
// Debug marks from a waveview rc (.wvrc) or the context menu: named cursors,
// arrows between signal transitions, comments on the waveform
// ---------------------------------------------------------------------------

function rowMidY(name, scrollTop) {
  if (!name) return RULER + 7;
  const i = S.rows.findIndex((r) => r.name === name);
  return i < 0 ? null : RULER + i * ROW - scrollTop + ROW / 2;
}

function tag(text, x, y, col, C, align) {
  const tw = ctx.measureText(text).width + 8;
  const x0 = align === 'center' ? x - tw / 2 : x;
  ctx.fillStyle = col; ctx.fillRect(x0, y - 7, tw, 14);
  ctx.fillStyle = C.bg; ctx.textAlign = 'left'; ctx.fillText(text, x0 + 4, y + 0.5);
  return [x0, y - 7, x0 + tw, y + 7];
}

function drawMarks(st, W, H, X, C, scrollTop) {
  S.hits = [];
  if (!st) return;
  ctx.lineWidth = 1;
  // named cursors: a solid line and a tag at the bottom
  (st.cursors || []).forEach((c) => {
    const x = Math.round(X(c.time)) + 0.5;
    if (x < -200 || x > W + 200) return;
    ctx.strokeStyle = c.color || C.ann; ctx.setLineDash([]);
    ctx.globalAlpha = 0.85;
    ctx.beginPath(); ctx.moveTo(x, RULER); ctx.lineTo(x, H); ctx.stroke();
    ctx.globalAlpha = 1;
    const b = tag(c.name + (c.label ? ' ' + c.label : ''), x, H - 28, c.color || C.ann, C, 'center');
    S.hits.push({ kind: 'cursors', name: c.name, x0: Math.min(b[0], x - 4), y0: RULER, x1: Math.max(b[2], x + 4), y1: H });
  });
  // comments on the time axis: like labels, in the comment's color
  (st.comments || []).forEach((c, i) => {
    if (c.signal) return;
    const x = Math.round(X(c.time)) + 0.5, col = c.color || C.comment;
    if (x < -300 || x > W) return;
    ctx.strokeStyle = col; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(x, RULER); ctx.lineTo(x, H); ctx.stroke(); ctx.setLineDash([]);
    const b = tag(c.text.split('\n')[0], x, RULER + 6.5, col, C);
    S.hits.push({ kind: 'comments', index: i, x0: b[0] - 4, y0: b[1], x1: b[2], y1: b[3] });
  });
  ctx.save();
  ctx.beginPath(); ctx.rect(0, RULER, W, H - RULER); ctx.clip();
  // arrows: a gentle curve from one transition to another
  (st.arrows || []).forEach((a, i) => {
    const y1 = rowMidY(a.from.signal, scrollTop), y2 = rowMidY(a.to.signal, scrollTop);
    if (y1 == null || y2 == null) return;
    drawArrow(X(a.from.time), y1, X(a.to.time), y2, a.color || C.arrow, a.label, C, { kind: 'arrows', index: i });
  });
  // comments on a signal: a dot on the transition and a callout box
  (st.comments || []).forEach((c, i) => {
    if (!c.signal) return;
    const y = rowMidY(c.signal, scrollTop);
    if (y == null) return;
    const x = X(c.time), col = c.color || C.comment;
    if (x < -400 || x > W + 20) return;
    const lines = c.text.split('\n');
    const tw = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 10, th = lines.length * 14 + 6;
    let bx = x + 16, by = y - ROW / 2 - th - 4;
    if (bx + tw > W - 2) bx = x - 16 - tw;
    if (by < RULER + 2) by = y + ROW / 2 + 4;
    ctx.strokeStyle = col; ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.moveTo(x, y);
    ctx.lineTo(bx < x ? bx + tw : bx, by + (by < y ? th : 0)); ctx.stroke();
    ctx.globalAlpha = 0.9; ctx.fillStyle = C.bg; ctx.fillRect(bx, by, tw, th); ctx.globalAlpha = 1;
    ctx.strokeRect(bx + 0.5, by + 0.5, tw - 1, th - 1);
    ctx.fillStyle = col; ctx.textAlign = 'left';
    lines.forEach((l, k) => ctx.fillText(l, bx + 5, by + 10 + k * 14));
    S.hits.push({ kind: 'comments', index: i, x0: Math.min(bx, x - 5), y0: Math.min(by, y - 5), x1: Math.max(bx + tw, x + 5), y1: Math.max(by + th, y + 5) });
  });
  // the arrow being placed
  if (S.arrowFrom && S.mouse) {
    const y1 = rowMidY(S.arrowFrom.signal, scrollTop);
    if (y1 != null) drawArrow(X(S.arrowFrom.t), y1, S.mouse.x, S.mouse.y, C.arrow, null, C, null, true);
  }
  ctx.restore();
}

function drawArrow(x1, y1, x2, y2, col, label, C, hit, dashed) {
  const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy);
  if (len < 2) return;
  const bend = Math.min(40, len * 0.18);
  const cx = (x1 + x2) / 2 - dy / len * bend, cy = (y1 + y2) / 2 + dx / len * bend;
  ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1.8;
  if (dashed) ctx.setLineDash([5, 4]);
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.quadraticCurveTo(cx, cy, x2, y2); ctx.stroke();
  ctx.setLineDash([]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(x1, y1, 3, 0, Math.PI * 2); ctx.fill();
  const ang = Math.atan2(y2 - cy, x2 - cx), hl = 9;
  ctx.beginPath(); ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - hl * Math.cos(ang - 0.4), y2 - hl * Math.sin(ang - 0.4));
  ctx.lineTo(x2 - hl * Math.cos(ang + 0.4), y2 - hl * Math.sin(ang + 0.4));
  ctx.closePath(); ctx.fill();
  const mx = 0.25 * x1 + 0.5 * cx + 0.25 * x2, my = 0.25 * y1 + 0.5 * cy + 0.25 * y2;
  if (label) {
    ctx.textAlign = 'center';
    const tw = ctx.measureText(label).width + 8;
    ctx.globalAlpha = 0.9; ctx.fillStyle = C.bg; ctx.fillRect(mx - tw / 2, my - 7, tw, 14); ctx.globalAlpha = 1;
    ctx.fillStyle = col; ctx.fillText(label, mx, my + 0.5);
  }
  if (hit) {
    for (let k = 0; k <= 10; k++) {
      const u = k / 10, px = (1 - u) * (1 - u) * x1 + 2 * u * (1 - u) * cx + u * u * x2, py = (1 - u) * (1 - u) * y1 + 2 * u * (1 - u) * cy + u * u * y2;
      S.hits.push({ ...hit, x0: px - 6, y0: py - 6, x1: px + 6, y1: py + 6 });
    }
  }
}

function hitAt(x, y) {
  for (let k = S.hits.length - 1; k >= 0; k--) {
    const h = S.hits[k];
    if (x >= h.x0 && x <= h.x1 && y >= h.y0 && y <= h.y1) return h;
  }
  return null;
}

// a time from a mouse x, snapped to a clock edge within a few pixels
function snapT(x) {
  const t = xToT(x), E = edgesInView();
  if (E && E.edges) {
    const px = canvasW() / (S.view[1] - S.view[0]);
    let best = null;
    for (const e of E.edges) if (Math.abs(e - t) * px < 6 && (best == null || Math.abs(e - t) < Math.abs(best - t))) best = e;
    if (best != null) return best;
  }
  return Math.round(t);
}

// rc lines go through the server's parser, the same one a .wvrc file uses
const rcq = (text) => JSON.stringify(text);
async function rcLines(text, merge = true) {
  const r = await api('/api/cmd', { op: 'rc', args: { text, merge }, source: 'ui' }).catch((e) => { toast(e.message); return null; });
  if (r && r.result.warnings.length) toast(r.result.warnings.join('\n'));
  return r;
}

function markMenu(e) {
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  const i = rowAtY(y), sig = i >= 0 ? S.rows[i].name : '';
  const t = snapT(x), at = rcq(sig + '@' + t);
  const h = hitAt(x, y);
  const st = S.state || {};
  const items = [];
  if (h && h.kind === 'cursors') {
    items.push({ label: `Remove cursor ${h.name}`, fn: () => cmd('unmark', { kind: 'cursors', name: h.name }) });
  } else if (h && h.kind === 'arrows') {
    items.push({ label: 'Edit arrow label…', fn: () => {
      const a = st.arrows[h.index], txt = prompt('Arrow label', a.label || '');
      if (txt == null) return;
      cmd('unmark', { kind: 'arrows', index: h.index }).then(() => rcLines(`arrow ${rcq((a.from.signal || '') + '@' + a.from.time)} ${rcq((a.to.signal || '') + '@' + a.to.time)} ${rcq(txt)}${a.color ? ' color=' + a.color : ''}`));
    } });
    items.push({ label: 'Remove arrow', fn: () => cmd('unmark', { kind: 'arrows', index: h.index }) });
  } else if (h && h.kind === 'comments') {
    items.push({ label: 'Edit comment…', fn: () => {
      const c = st.comments[h.index], txt = prompt('Comment (\\n for a new line)', c.text.replace(/\n/g, '\\n'));
      if (txt == null) return;
      cmd('unmark', { kind: 'comments', index: h.index }).then(() => rcLines(`comment ${rcq((c.signal || '') + '@' + c.time)} ${rcq(txt)}${c.color ? ' color=' + c.color : ''}`));
    } });
    items.push({ label: 'Remove comment', fn: () => cmd('unmark', { kind: 'comments', index: h.index }) });
  }
  if (items.length) items.push(null);
  items.push({ label: 'Named cursor here…', fn: () => {
    const used = new Set((st.cursors || []).map((c) => c.name));
    let def = 'A'; while (used.has(def)) def = String.fromCharCode(def.charCodeAt(0) + 1);
    const txt = prompt('Name and an optional label, e.g. "A req rises"', def);
    if (!txt || !txt.trim()) return;
    const [name, ...rest] = txt.trim().split(/\s+/);
    rcLines(`cursor ${name} ${t}${rest.length ? ' ' + rcq(rest.join(' ')) : ''}`);
  } });
  items.push({ label: sig ? `Comment on ${sig.split('.').pop()} here…` : 'Comment on the time axis here…', fn: () => {
    const txt = prompt('Comment (\\n for a new line)');
    if (txt) rcLines(`comment ${at} ${rcq(txt)}`);
  } });
  if (sig) items.push({ label: `Arrow from ${sig.split('.').pop()} here → click the target`, fn: () => { S.arrowFrom = { signal: sig, t }; toast('Click where the arrow points (Esc cancels)'); } });
  items.push(null);
  items.push({ label: 'Save view as .wvrc', fn: () => { location.href = '/api/export.wvrc'; } });
  items.push({ label: 'Load .wvrc…', fn: () => $('rcfile').click() });
  showMenu(e.clientX, e.clientY, items);
}

function finishArrow(x, y) {
  const i = rowAtY(y), a = S.arrowFrom;
  S.arrowFrom = null;
  if (i < 0) { toast('Point the arrow at a signal row'); draw(); return; }
  const label = prompt('Arrow label (optional)', '');
  if (label == null) { draw(); return; }
  rcLines(`arrow ${rcq(a.signal + '@' + a.t)} ${rcq(S.rows[i].name + '@' + snapT(x))}${label ? ' ' + rcq(label) : ''}`);
}

function jumpCursor(dir) {
  const ts = ((S.state && S.state.cursors) || []).map((c) => c.time).sort((a, b) => a - b);
  const now = S.cursor == null ? (dir > 0 ? -Infinity : Infinity) : S.cursor;
  const t = dir > 0 ? ts.find((x) => x > now) : ts.reverse().find((x) => x < now);
  if (t == null) { toast(ts.length ? 'No more named cursors that way' : 'No named cursors'); return; }
  ensureVisible(t); setCursor(t);
}

$('rcfile').addEventListener('change', async () => {
  const f = $('rcfile').files[0];
  if (!f) return;
  const text = await f.text();
  $('rcfile').value = '';
  const r = await api('/api/cmd', { op: 'rc', args: { text, origin: f.name }, source: 'rc' }).catch((e) => { toast(e.message); return null; });
  if (r) toast(r.result.warnings.length ? r.result.warnings.join('\n') : `Loaded ${f.name}`);
});

function drawSignal(sd, r, top, W, X, C) {
  const pts = sd.pts;
  const hi = top + 4, lo = top + ROW - 5, mid = top + ROW / 2;
  // skip to the first segment that reaches the view
  let s = 0, e = pts.length - 1;
  const v0 = S.view[0];
  while (s < e) { const m = (s + e + 1) >> 1; if (pts[m][0] <= v0) s = m; else e = m - 1; }
  ctx.lineWidth = 1;
  const dn = sd.dense;
  let dp = 0;
  for (let k = s; k < pts.length; k++) {
    let ta = pts[k][0];
    const tb = k + 1 < pts.length ? pts[k + 1][0] : sd.t1;
    // a point that opens a dense stretch carries the value at its end; draw
    // that value only after the stretch, the stretch itself as a block
    while (dp < dn.length && dn[dp][1] < ta) dp++;
    const inDense = dp < dn.length && dn[dp][0] <= ta && dn[dp][1] > ta;
    if (inDense) { ta = dn[dp][1]; if (ta >= tb) continue; }
    let xa = X(ta), xb = X(tb);
    if (xa > W) break;
    if (xb < 0) continue;
    xa = Math.max(xa, -2); xb = Math.min(xb, W + 2);
    const v = pts[k][1];
    if (v === '?') { // not fetched yet
      ctx.fillStyle = C.loading; ctx.fillRect(xa, hi, xb - xa, lo - hi);
      continue;
    }
    const isx = encIsX(v), isz = encIsZ(v);
    if (r.w === 1) {
      if (isx) { ctx.fillStyle = C.xfill; ctx.fillRect(xa, hi, xb - xa, lo - hi); ctx.strokeStyle = C.x; strokeLine(xa, hi, xb, hi); strokeLine(xa, lo, xb, lo); }
      else if (isz) { ctx.strokeStyle = C.z; strokeLine(xa, mid, xb, mid); }
      else {
        ctx.strokeStyle = C.wave;
        const y = v === '1' ? hi : lo;
        if (v === '1') { ctx.fillStyle = C.fill; ctx.fillRect(xa, hi, xb - xa, lo - hi); }
        strokeLine(xa, y, xb, y);
        if (!inDense && (k > s || ta > v0)) {
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
        let txt = fmtEnc(v, r.w, r.radix);
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
  // stretches with more changes than pixels: for a 1-bit signal the shade
  // says how much of the time it is 1, for a bus how busy it is
  ctx.fillStyle = C.wave;
  for (const [a, b, n, h] of dn) {
    const xa = X(a), xb = Math.max(X(b), xa + 1);
    if (xb < 0 || xa > W) continue;
    // a stretch narrower than two pixels is a short event (a one cycle
    // pulse seen from far away): draw it at full strength so it is not lost
    ctx.globalAlpha = xb - xa < 2 ? 1 : r.w === 1 ? 0.25 + 0.6 * h : 0.3 + 0.55 * Math.min(1, n / Math.max(1, (xb - xa) * 2));
    ctx.fillRect(xa, hi, Math.max(1, xb - xa), lo - hi);
  }
  ctx.globalAlpha = 1;
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
  if (S.arrowFrom && e.button === 0) { e.preventDefault(); finishArrow(x, y); return; }
  if (e.button === 1 || (e.button === 0 && e.shiftKey)) { e.preventDefault(); setMarker(Math.round(xToT(x))); return; }
  if (e.button !== 0) return;
  S.drag = { x0: x, x1: x, y, moved: false };
});
canvas.addEventListener('mousemove', (e) => {
  if (!S.arrowFrom) return;
  const rect = canvas.getBoundingClientRect();
  S.mouse = { x: e.clientX - rect.left, y: e.clientY - rect.top };
  draw();
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
canvas.addEventListener('contextmenu', (e) => { e.preventDefault(); S.arrowFrom = null; markMenu(e); });
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
$('lists').addEventListener('scroll', requestData);
window.addEventListener('resize', () => { requestData(); draw(); });

// names pane: select, reorder, drop from the signal list, context menu
const namesEl = $('names');
namesEl.addEventListener('click', (e) => {
  const r = e.target.closest('.r'); if (!r) return;
  // the first click re-renders the rows, so a dblclick event would land on a
  // detached element: count clicks here instead
  if (e.detail === 2) { traceSignal(S.rows[+r.dataset.i].name, e.shiftKey); return; }
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
  renderRows(); pushRows(); pushSelect(); requestData();
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
  if ($('opendlg').open) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'o') { e.preventDefault(); showOpen(); return; }
  const inField = e.target.matches('input, textarea');
  if (e.key === 'Escape') {
    $('menu').hidden = true;
    if (S.arrowFrom) { S.arrowFrom = null; draw(); return; }
    if (inField) { e.target.blur(); return; }
    S.selected.clear(); renderRows(); pushSelect(); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') { e.preventDefault(); $('find').focus(); $('find').select(); return; }
  if (inField) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') { e.preventDefault(); S.selected = new Set(S.rows.map((r) => r.name)); renderRows(); pushSelect(); return; }
  if (e.ctrlKey || e.metaKey) return;
  if (e.altKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight') && SRC.open) { e.preventDefault(); srcHist(e.key === 'ArrowLeft' ? -1 : 1); return; }
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
    case ']': jumpCursor(1); break;
    case '[': jumpCursor(-1); break;
    case 'Delete': case 'Backspace': removeSelected(); break;
    case 'h': setRadix('hex'); break;
    case 'd': setRadix('dec'); break;
    case 's': setRadix('sdec'); break;
    case 'b': setRadix('bin'); break;
    case 'o': setRadix('oct'); break;
    case '/': e.preventDefault(); $('find').focus(); break;
    case 'a': openAsk(); break;
    case 'S': $('b-src').click(); break;
    case 'D': $('b-diag').click(); break;
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
// Source pane: the RTL of a scope with the value of every signal at the
// cursor next to it (active annotation), and driver / load tracing that
// follows instance ports down and input ports up, like Verdi's nTrace.
// ---------------------------------------------------------------------------

const SRC = { open: false, scope: null, view: null, line: null, hist: [], hpos: -1, cache: new Map(), sel: null, trace: null };
const KW_RE = /\b(module|endmodule|input|output|inout|logic|wire|reg|bit|int|integer|genvar|localparam|parameter|typedef|struct|union|enum|packed|assign|always|always_ff|always_comb|always_latch|initial|begin|end|if|else|case|casez|casex|unique|priority|endcase|default|for|foreach|generate|endgenerate|function|endfunction|task|endtask|return|automatic|posedge|negedge|or|import|signed|unsigned|interface|endinterface|package|endpackage)\b/g;
const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function showSrcPane(on) {
  SRC.open = on;
  $('srcpane').hidden = !on;
  $('hsplit').hidden = !on;
  $('b-src').classList.toggle('primary', on);
  requestData(); draw();
}

async function srcView(scope, file) {
  // file: one of the module's `include files instead of its own
  const key = scope + (file ? '\u0000' + file : '');
  let v = SRC.cache.get(key);
  if (!v || v.gen !== gen()) {
    v = await api('/api/src/view?scope=' + encodeURIComponent(scope) + (file ? '&file=' + encodeURIComponent(file) : ''));
    v.gen = gen();
    SRC.cache.set(key, v);
  }
  return v;
}

async function srcOpen(scope, line, opts) {
  opts = opts || {};
  if ($('diagdlg').open) $('diagdlg').close();  // the source pane is behind the diagram
  let v;
  try {
    v = await srcView(scope, opts.file);
    // a place in an `include file of the module: that file, whole
    if (!opts.file && v.files && v.files.length > 1 && opts.fileOf) {
      const f = opts.fileOf;
      if (f !== v.file && v.files.includes(f)) v = await srcView(scope, f);
    }
  } catch (e) { toast(e.message); return false; }
  const same = SRC.view === v;
  SRC.view = v; SRC.scope = scope; SRC.line = line || v.start;
  if (opts.sel !== undefined) SRC.sel = opts.sel;
  if (!opts.noHist) {
    SRC.hist = SRC.hist.slice(0, SRC.hpos + 1);
    const top = SRC.hist[SRC.hpos];
    if (!top || top.scope !== scope || top.line !== SRC.line || top.file !== v.file) { SRC.hist.push({ scope, line: SRC.line, sel: SRC.sel, file: v.file }); SRC.hpos = SRC.hist.length - 1; }
  }
  if (!SRC.open) showSrcPane(true);
  if (!same) renderCode(); else markLines();
  scrollToLine(SRC.line);
  renderCrumbs();
  srcAnnot();
  if (!opts.quiet) cmd('source', { scope, line: SRC.line, name: SRC.sel, kind: opts.kind || null, file: v.file });
  return true;
}

function renderCrumbs() {
  const box = $('src-crumbs');
  box.textContent = '';
  const comps = SRC.scope.split('.');
  comps.forEach((c, i) => {
    if (i) box.append('.');
    const a = document.createElement('a');
    a.textContent = c;
    const sc = comps.slice(0, i + 1).join('.');
    a.onclick = () => srcOpen(sc, null);
    a.title = 'Open ' + sc;
    box.append(a);
  });
  const m = document.createElement('span'); m.className = 'mod'; m.textContent = '(' + SRC.view.module + ')';
  box.append(m);
  $('src-file').textContent = `${SRC.view.file}:${SRC.line}`;
  $('src-back').disabled = SRC.hpos <= 0;
  $('src-fwd').disabled = SRC.hpos >= SRC.hist.length - 1;
}

function renderCode() {
  const v = SRC.view;
  const lines = v.text.split('\n');
  const byLine = new Map();
  for (const id of v.idents) { if (!byLine.has(id[0])) byLine.set(id[0], []); byLine.get(id[0]).push(id); }
  let inBlock = false;
  const html = [];
  const deco = (frag) => {
    // comments and keywords in the text between identifiers
    let out = '', rest = frag;
    while (rest) {
      if (inBlock) {
        const e = rest.indexOf('*/');
        if (e < 0) { out += `<span class="cm">${esc(rest)}</span>`; return out; }
        out += `<span class="cm">${esc(rest.slice(0, e + 2))}</span>`; rest = rest.slice(e + 2); inBlock = false; continue;
      }
      const lc = rest.indexOf('//'), bc = rest.indexOf('/*');
      const cut = [lc, bc].filter((x) => x >= 0).sort((a, b) => a - b)[0];
      const code = cut === undefined ? rest : rest.slice(0, cut);
      out += esc(code).replace(KW_RE, '<span class="kw">$1</span>').replace(/(\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+|\b\d+\b)/g, '<span class="nm">$1</span>');
      if (cut === undefined) return out;
      rest = rest.slice(cut);
      if (cut === lc && (bc < 0 || lc < bc)) { out += `<span class="cm">${esc(rest)}</span>`; return out; }
      inBlock = true;
    }
    return out;
  };
  lines.forEach((text, i) => {
    const ln = i + 1;
    const ids = (byLine.get(ln) || []).sort((a, b) => a[1] - b[1]);
    let pos = 0, body = '';
    for (const [, col, name, kind] of ids) {
      if (col < pos) continue;
      body += deco(text.slice(pos, col));
      const mapped = v.signals[name] ? ' m' : '';
      body += `<span class="id k-${kind}${mapped}" data-n="${esc(name)}" data-k="${kind}">${esc(name)}</span>`;
      if (mapped) body += `<span class="ann" data-n="${esc(name)}"></span>`;
      pos = col + name.length;
    }
    body += deco(text.slice(pos));
    const out = ln < v.start || ln > v.end ? ' out' : '';
    html.push(`<div class="ln${out}" data-l="${ln}"><span class="no">${ln}</span><span class="tx">${body || ' '}</span></div>`);
  });
  $('code').innerHTML = html.join('');
  markLines();
}

function markLines() {
  const code = $('code');
  code.querySelectorAll('.ln.cur').forEach((e) => e.classList.remove('cur'));
  const el = code.querySelector(`.ln[data-l="${SRC.line}"]`);
  if (el) el.classList.add('cur');
  code.querySelectorAll('.id.sel').forEach((e) => e.classList.remove('sel'));
  if (SRC.sel) code.querySelectorAll(`.id[data-n="${CSS.escape(SRC.sel)}"]`).forEach((e) => e.classList.add('sel'));
}

function scrollToLine(line) {
  const el = $('code').querySelector(`.ln[data-l="${line}"]`);
  if (!el) return;
  const box = $('code');
  const top = el.offsetTop - box.clientHeight / 3;
  box.scrollTop = Math.max(0, top);
}

// active annotation: values at the cursor; a value that changed at this
// clock edge is highlighted
let annTimer = null;
function srcAnnot() {
  clearTimeout(annTimer);
  annTimer = setTimeout(async () => {
    if (!SRC.open || !SRC.view) return;
    const code = $('code');
    const on = $('src-ann').checked;
    code.querySelectorAll('.ann').forEach((e) => { e.hidden = !on || S.cursor == null; });
    if (!on || S.cursor == null) return;
    const v = SRC.view;
    const want = [];
    for (const name of Object.keys(v.signals)) for (const n of v.signals[name].slice(0, 8)) want.push(n);
    if (!want.length) return;
    const ci = clockInfo();
    const tPrev = S.cursor - (ci && ci.period ? ci.period : 1);
    let now, before;
    try {
      [now, before] = await Promise.all([
        api('/api/values', { signals: want, t: S.cursor }),
        api('/api/values', { signals: want, t: tPrev }),
      ]);
    } catch (e) { return; }
    const text = {}, changed = {}, unknown = {};
    const params = new Set(v.idents.filter((i) => i[3] === 'param').map((i) => i[2]));
    for (const name of Object.keys(v.signals)) {
      const ns = v.signals[name].slice(0, 8);
      const parts = [];
      let chg = false, xx = false;
      for (const n of ns) {
        const bits = now.values[n];
        if (bits == null) continue;
        // element / field part after the identifier: [0], .valid, gen_x[1]...
        const at = n.lastIndexOf('.' + name);
        const tail = at >= 0 ? n.slice(at + 1 + name.length).replace(/^\./, '') : '';
        const label = tail ? tail + '=' : '';
        // parameters read better in decimal
        parts.push(label + fmtVal(bits, bits.length, params.has(name) ? 'dec' : 'hex'));
        if (before.values[n] !== bits) chg = true;
        if (/[xz]/.test(bits)) xx = true;
      }
      if (v.signals[name].length > 8) parts.push('…');
      let t = parts.join(' ');
      if (t.length > 48) t = t.slice(0, 47) + '…';
      text[name] = t; changed[name] = chg; unknown[name] = xx;
    }
    code.querySelectorAll('.ann').forEach((e) => {
      const n = e.dataset.n;
      e.textContent = text[n] || '';
      e.className = 'ann' + (unknown[n] ? ' x' : changed[n] ? ' chg' : '');
    });
  }, 40);
}

// tracing
function kindLabel(r) {
  return { always_ff: 'ff', comb: 'comb', assign: 'assign', init: 'init', read: 'read', port_output: 'inst out',
    port_input: 'inst in', port_inout: 'inst io', parent_conn: 'parent', decl: 'decl', port: 'port' }[r.kind] || r.kind;
}

function childScope(r) {
  // the dump scope of the instance a port connection goes into
  const kids = SRC.cache.get(r.scope)?.children?.[r.instance];
  return kids && kids.length ? kids[0] : r.child_scope;
}

async function traceFrom(scope, name, loads, hops) {
  hops = hops || [];
  let res;
  try {
    res = await api(`/api/src/trace?scope=${encodeURIComponent(scope)}&name=${encodeURIComponent(name)}&loads=${loads ? 1 : 0}`);
  } catch (e) { toast(e.message); return; }
  // follow single port hops automatically when tracing drivers: into the
  // instance that drives it, or up to the parent that feeds an input
  if (!loads && res.length === 1 && hops.length < 12) {
    const r = res[0];
    if (r.kind === 'port_output' || r.kind === 'port_inout') {
      await srcView(r.scope).catch(() => null);
      const child = childScope(r);
      if (child) { hops.push(`${r.instance}.${r.port}`); return traceFrom(child, r.port, false, hops); }
    }
    if (r.kind === 'parent_conn' && r.signal && r.signals.length === 1) {
      hops.push(`↑ ${r.scope.split('.').pop()}.${r.signal}`);
      return traceFrom(r.scope, r.signal, false, hops);
    }
  }
  SRC.trace = { scope, name, loads, res, hops };
  renderTrace();
  if (res.length) await gotoTrace(0);
  else {
    // nothing drives it in the sources (a tb input, a constant, a bad index): show the declaration
    toast(`No ${loads ? 'loads' : 'drivers'} of ${name} found in the source`);
    await srcOpen(scope, null, { sel: name });
    const decl = SRC.view && $('code').querySelector(`.id[data-n="${CSS.escape(name)}"]`);
    if (decl) { SRC.line = +decl.closest('.ln').dataset.l; markLines(); scrollToLine(SRC.line); renderCrumbs(); }
  }
}

function renderTrace() {
  const t = SRC.trace, box = $('tracelist');
  box.hidden = !t;
  if (!t) return;
  box.textContent = '';
  const h = document.createElement('div'); h.className = 'th';
  h.textContent = `${t.loads ? 'Loads' : 'Drivers'} of ${t.name} (${t.res.length})`;
  box.append(h);
  for (const hop of t.hops) { const d = document.createElement('div'); d.className = 'hop'; d.textContent = 'via ' + hop; box.append(d); }
  t.res.forEach((r, i) => {
    const d = document.createElement('div');
    d.className = 'ti' + (i === t.cur ? ' on' : '');
    const where = r.instance ? `${r.instance}.${r.port}` : r.signal;
    d.innerHTML = `<span class="k">${kindLabel(r)}</span>${esc(r.file.split('/').pop())}:${r.line}<span class="w">${esc(r.scope === t.scope ? where || '' : r.scope.split('.').slice(-2).join('.') + ' ' + (where || ''))}</span>`;
    d.title = `${r.file}:${r.line}  in ${r.scope}`;
    d.onclick = () => gotoTrace(i);
    d.ondblclick = () => {
      // go one step further from this place
      if (r.kind.startsWith('port_') && !t.loads) { const c = childScope(r); if (c) traceFrom(c, r.port, false, [...t.hops, `${r.instance}.${r.port}`]); }
      else if (r.kind === 'port_input' && t.loads) { const c = childScope(r); if (c) traceFrom(c, r.port, true); }
      else if (r.kind === 'parent_conn' && r.signal) traceFrom(r.scope, r.signal, t.loads);
    };
    box.append(d);
  });
}

async function gotoTrace(i) {
  const t = SRC.trace;
  t.cur = i;
  const r = t.res[i];
  const sel = r.kind === 'parent_conn' ? r.signal : (r.kind.startsWith('port_') ? r.signal : r.signal || t.name);
  await srcOpen(r.scope, r.line, { sel, kind: t.loads ? 'loads' : 'drivers', fileOf: r.file });
  renderTrace();
}

async function traceSignal(full, loads) {
  let info;
  try { info = await api('/api/src/signal?name=' + encodeURIComponent(full)); } catch (e) { toast(e.message); return; }
  await srcView(info.scope).catch(() => null);
  traceFrom(info.scope, info.signal, loads);
}

function srcSignalNames(name) {
  const v = SRC.view;
  return (v && v.signals[name]) || [];
}

function addFromSource(name) {
  const names = srcSignalNames(name);
  if (!names.length) { toast(`${name} is not in the dump`); return; }
  addRows(names.slice(0, 64));
  toast(`Added ${Math.min(64, names.length)} signal${names.length > 1 ? 's' : ''}`);
}

async function enterInstance(inst) {
  const v = SRC.view;
  const kids = v.children[inst];
  if (!kids || !kids.length) { toast(`${inst} has no signals in the dump`); return; }
  if (kids.length === 1) { await srcOpen(kids[0], null); return; }
  SRC.trace = { scope: SRC.scope, name: inst, loads: false, hops: [], res: [] };
  const box = $('tracelist'); box.hidden = false; box.textContent = '';
  const h = document.createElement('div'); h.className = 'th'; h.textContent = `${inst}: ${kids.length} instances`; box.append(h);
  for (const k of kids) {
    const d = document.createElement('div'); d.className = 'ti'; d.textContent = k.slice(SRC.scope.length + 1);
    d.onclick = () => srcOpen(k, null); box.append(d);
  }
}

const codeEl = $('code');
codeEl.addEventListener('click', (e) => {
  const id = e.target.closest('.id'); if (!id) return;
  const name = id.dataset.n;
  if (e.ctrlKey || e.metaKey) { if (id.dataset.k === 'signal') addFromSource(name); return; }
  SRC.sel = name;
  SRC.line = +id.closest('.ln').dataset.l;
  markLines(); renderCrumbs();
});
codeEl.addEventListener('auxclick', (e) => {
  if (e.button !== 1) return;
  const id = e.target.closest('.id'); if (id && id.dataset.k === 'signal') { e.preventDefault(); addFromSource(id.dataset.n); }
});
codeEl.addEventListener('dblclick', (e) => {
  const id = e.target.closest('.id'); if (!id) return;
  window.getSelection()?.removeAllRanges();
  const name = id.dataset.n, k = id.dataset.k;
  if (k === 'instance') enterInstance(name);
  else if (k === 'module') {
    const line = +id.closest('.ln').dataset.l;
    const inst = Object.entries(SRC.view.instances).find(([, i]) => i.line === line);
    if (inst) enterInstance(inst[0]);
  } else if (k === 'signal') traceFrom(SRC.scope, name, e.shiftKey);
});
codeEl.addEventListener('contextmenu', (e) => {
  const id = e.target.closest('.id'); if (!id) return;
  e.preventDefault();
  const name = id.dataset.n, k = id.dataset.k;
  const items = [];
  if (k === 'signal') {
    items.push({ label: 'Trace drivers', fn: () => traceFrom(SRC.scope, name, false) });
    items.push({ label: 'Trace loads', fn: () => traceFrom(SRC.scope, name, true) });
    items.push({ label: 'Add to waveform', fn: () => addFromSource(name) });
    items.push(null);
    items.push({ label: 'Ask AI about this signal…', fn: () => openAsk(`What drives ${SRC.scope}.${name} and why does it have this value at the cursor?`) });
  }
  if (k === 'instance') items.push({ label: 'Go into ' + name, fn: () => enterInstance(name) });
  items.push({ label: 'Copy hierarchical name', fn: () => navigator.clipboard?.writeText(SRC.scope + '.' + name) });
  showMenu(e.clientX, e.clientY, items);
});

$('b-src').onclick = () => {
  if (SRC.open) { showSrcPane(false); return; }
  if (SRC.view) { showSrcPane(true); srcAnnot(); return; }
  const sel = S.rows.find((r) => S.selected.has(r.name)) || S.rows[0];
  if (sel) traceSignal(sel.name, false); else showSrcPane(true);
};
$('src-close').onclick = () => showSrcPane(false);
$('src-ann').onchange = srcAnnot;
function srcHist(d) {
  const n = SRC.hpos + d;
  if (n < 0 || n >= SRC.hist.length) return;
  SRC.hpos = n;
  const h = SRC.hist[n];
  srcOpen(h.scope, h.line, { noHist: true, sel: h.sel, fileOf: h.file });
}
$('src-back').onclick = () => srcHist(-1);
$('src-fwd').onclick = () => srcHist(1);
$('hsplit').addEventListener('mousedown', (e) => {
  e.preventDefault();
  const y0 = e.clientY, h0 = $('srcpane').offsetHeight;
  const mv = (ev) => { document.documentElement.style.setProperty('--src-h', Math.max(80, Math.min(innerHeight - 200, h0 - (ev.clientY - y0))) + 'px'); draw(); };
  const up = () => { window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); requestData(); };
  window.addEventListener('mousemove', mv); window.addEventListener('mouseup', up);
});

// a double-click on a waveform row traces its drivers (Shift: its loads)
canvas.addEventListener('dblclick', (e) => {
  const rect = canvas.getBoundingClientRect();
  const i = rowAtY(e.clientY - rect.top);
  if (i >= 0) traceSignal(S.rows[i].name, e.shiftKey);
});

// the agent (or another browser) moved the source view
let lastSourceAt = 0;
function followSource(st) {
  const src = st.source;
  if (!src || !src.at || src.at === lastSourceAt) return;
  lastSourceAt = src.at;
  if (st.last && st.last.client === S.client) return;
  srcOpen(src.scope, src.line, { sel: src.signal, quiet: true, fileOf: src.file });
}

// ---------------------------------------------------------------------------

// Open dialog: this PC or a host of ~/.ssh/config, then a folder, then a dump
// ---------------------------------------------------------------------------

// mode: 'dump' lists waveform files; 'rtl' lists what --rtl takes, to fill the RTL field
const OP = { host: '', path: '', file: null, seq: 0, hosts: false, hasDump: false, busy: false, local: 'This PC', mode: 'dump', rtlDir: '' };
const OTHER_HOST = '\u0000other';

function opRemembered(key) { try { return localStorage.getItem('wv-open-' + key) || ''; } catch (e) { return ''; } }
function opRemember(key, v) { try { localStorage.setItem('wv-open-' + key, v); } catch (e) { /* ignore */ } }
function opMsg(text, bad) { $('op-msg').textContent = text; $('op-msg').classList.toggle('bad', !!bad); }
function opSize(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}
function opChosenHost() {
  const v = $('op-host').value;
  return v === OTHER_HOST ? $('op-manual').value.trim() : v;
}
function opPick(entry, row) {
  OP.file = entry;
  for (const el of $('op-list').children) el.classList.toggle('sel', el === row);
  $('op-open').disabled = OP.busy;
}

async function opList(host, path) {
  const seq = ++OP.seq;
  const rtl = OP.mode === 'rtl';
  if (!rtl) OP.file = null;
  $('op-open').disabled = true;
  $('op-list').replaceChildren();
  opMsg(host ? `Connecting to ${host}…` : 'Reading…');
  try {
    const d = await api('/api/ls', { host, path, kind: rtl ? 'rtl' : 'dumps' });
    if (seq !== OP.seq) return;
    OP.host = host; OP.parent = d.parent;
    if (rtl) { OP.rtlDir = d.path; opRemember('rtldir:' + host, d.path); } else { OP.path = d.path; opRemember('path:' + host, d.path); }
    $('op-path').value = d.path === '*' ? '' : d.path;
    $('op-up').disabled = d.parent === null || d.parent === undefined;
    opRemember('host', host);
    for (const e of d.entries) {
      const row = document.createElement('div');
      row.className = 'op-item' + (e.dir && !e.db ? ' dir' : '') + (e.db ? ' db' : '');
      const name = document.createElement('span');
      name.textContent = (e.db ? '▣ ' : e.dir ? '▸ ' : '') + e.name;
      const size = document.createElement('span');
      size.className = 'dim';
      size.textContent = e.db ? 'Verdi KDB' : e.dir ? '' : opSize(e.size);
      row.append(name, size);
      if (e.db) row.onclick = () => opRtlChoose(e.path);
      else if (e.dir) row.onclick = () => opList(host, e.path);
      else if (rtl) row.onclick = () => opRtlChoose(e.path);
      else {
        row.onclick = () => opPick(e, row);
        row.ondblclick = () => { opPick(e, row); opOpen(); };
        if (OP.file && OP.file.path === e.path) opPick(e, row);  // back from the RTL listing: still chosen
      }
      $('op-list').append(row);
    }
    const files = d.entries.filter((e) => !e.dir).length;
    if (rtl) opMsg(`${host || OP.local}: click a filelist (.f), a design .json / .xml, a Verdi KDB (work.lib++, simv.daidir), or press Use this folder` + (d.more ? ' (list cut short)' : ''));
    else opMsg(`${host || OP.local}: ${files} waveform file${files === 1 ? '' : 's'} here` + (d.more ? ' (list cut short)' : ''));
  } catch (err) {
    if (seq !== OP.seq) return;
    opMsg(err.message + (host ? '  (ssh must log in without a prompt: a key with ssh-agent, and a known host key)' : ''), true);
  }
}

// -- the RTL field from a listing: a filelist / design file, or the folder shown
function opSetMode(mode) {
  OP.mode = mode;
  const rtl = mode === 'rtl';
  $('op-rtl-browse').hidden = rtl;
  $('op-rtl-here').hidden = !rtl;
  $('op-rtl-back').hidden = !rtl;
  $('opendlg').classList.toggle('rtl-mode', rtl);
}
function opRtlBrowse() {
  const host = opChosenHost();
  if (host !== OP.host) { opMsg('Press Connect first', true); return; }
  opSetMode('rtl');
  const typed = $('op-rtl').value.trim();
  // start where the field points (its folder), else where the RTL was last chosen, else at the dump
  let start = opRemembered('rtldir:' + host) || OP.path;
  if (typed) start = /\.(f|F|vc|flist|json|xml)$/.test(typed) ? typed.replace(/[\\/][^\\/]*$/, '') || typed : typed;
  opList(OP.host, start);
}
function opRtlChoose(path) {
  $('op-rtl').value = path;
  opRtlBack();
}
function opRtlBack() {
  opSetMode('dump');
  opList(OP.host, OP.path);
}

async function opOpen() {
  if (!OP.file || OP.busy) return;
  OP.busy = true;
  OP.seq++;  // a listing still on its way must not replace this message
  $('op-open').disabled = true;
  opMsg(`Opening ${OP.file.name}…` + (OP.host ? ' The first time on a server also builds its reader.' : ''));
  try {
    await api('/api/open', { host: OP.host, path: OP.file.path, rtl: $('op-rtl').value });
    opRemember('rtl:' + OP.host, $('op-rtl').value);
    location.reload();
  } catch (err) {
    OP.busy = false;
    $('op-open').disabled = false;
    opMsg(err.message, true);
  }
}

function opHostChanged() {
  const other = $('op-host').value === OTHER_HOST;
  $('op-manual').hidden = !other;
  const host = opChosenHost();
  opSetMode('dump');
  $('op-rtl').value = opRemembered('rtl:' + host);
  // a folder belongs to one machine: never carry it over to another
  if (other && !host) { $('op-path').value = ''; $('op-list').replaceChildren(); OP.file = null; $('op-open').disabled = true; opMsg('Enter a host, then Connect'); $('op-manual').focus(); return; }
  if (host) { $('op-list').replaceChildren(); $('op-path').value = opRemembered('path:' + host); OP.file = null; $('op-open').disabled = true; opMsg('Press Connect'); return; }
  opList('', opRemembered('path:'));
}

async function showOpen() {
  $('op-cancel').hidden = !OP.hasDump;
  $('opendlg').showModal();
  if (OP.hosts) return;
  OP.hosts = true;
  let hosts = [];
  try {
    const r = await api('/api/hosts');
    hosts = r.hosts;
    if (r.local) OP.local = r.local;
  } catch (e) { /* no ssh config */ }
  const sel = $('op-host');
  sel.replaceChildren(new Option(OP.local, ''), ...hosts.map((h) => new Option(h, h)), new Option('Other host…', OTHER_HOST));
  const last = opRemembered('host');
  if (hosts.includes(last)) sel.value = last;
  opHostChanged();
}

$('b-open').onclick = showOpen;
$('op-host').onchange = opHostChanged;
$('op-connect').onclick = () => { const h = opChosenHost(); if (h || $('op-host').value === '') opList(h, h ? $('op-path').value.trim() : OP.path); };
$('op-go').onclick = () => opList(opChosenHost(), $('op-path').value.trim());
$('op-up').onclick = () => { if (OP.parent !== null && OP.parent !== undefined) opList(OP.host, OP.parent); };
$('op-open').onclick = opOpen;
$('op-rtl-browse').onclick = opRtlBrowse;
$('op-rtl-here').onclick = () => opRtlChoose(OP.rtlDir);
$('op-rtl-back').onclick = opRtlBack;
$('op-cancel').onclick = () => { opSetMode('dump'); $('opendlg').close(); };
$('op-path').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('op-go').click(); });
$('op-manual').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('op-connect').click(); });
$('op-manual').addEventListener('input', () => { OP.seq++; $('op-path').value = opRemembered('path:' + opChosenHost()); $('op-list').replaceChildren(); OP.file = null; $('op-open').disabled = true; opMsg('Press Connect'); });
// with nothing loaded there is nothing behind the dialog to go back to
$('opendlg').addEventListener('cancel', (e) => { if (!OP.hasDump) e.preventDefault(); });

// ---------------------------------------------------------------------------

(async function init() {
  try { const t = localStorage.getItem('wv-theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) { /* ignore */ }
  readColors();
  const session = await api('/api/session');
  OP.hasDump = !!session.dump;
  if (!session.dump) { setPill('st-conn', 'no waveform open', ''); showOpen(); return; }
  await loadSignals();
  connect();
  watchIndex();
  // a deep link: #diagram / #treemap, with =scope, opens it at once
  const m = /^#(diagram|treemap)(?:=(.*))?$/.exec(location.hash);
  if (m) showDiagram(m[2] ? decodeURIComponent(m[2]) : (S.scope || ''), m[1]);
})();
