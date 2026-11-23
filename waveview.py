#!/usr/bin/env python3
"""waveview: a GTKWave / Verdi style waveform viewer in the browser, shared
between a person and an AI agent, built for remote development.

    tools/waveview.py serve w.fst              start a viewer on the dump
    tools/waveview.py add '*u_rob.commit_*'    (from any shell) add signals
    tools/waveview.py goto --cycle 1234        move the cursor and the view
    tools/waveview.py note "flush comes from the BRU mispredict here"
    tools/waveview.py look                     what the viewer shows, as text
    tools/waveview.py inbox --wait             wait for "Send to AI" from the UI

Why it is built this way:
- The server runs where the dump is (the remote build machine) and needs
  nothing beyond Python 3. The browser is the display: forward the port with
  `ssh -L 8765:localhost:8765 host` or let VS Code Remote forward it. No X11,
  no copying multi-GB dumps to a laptop
- The view is one shared state on the server: signal list, cursor, marker,
  zoom, notes. The browser and the command line both edit it, and every
  change is pushed to every open browser at once. So an agent can say "look
  here" by moving the view, and read back what the person is looking at
- The command line half finds the running server by itself (a registry under
  ~/.cache/scariv/waveview), so an agent does not need to know ports or tokens
- When the simulation rewrites the dump, the viewer reloads it in place and
  keeps the signal list, cursor and zoom
- The view is saved next to the dump (<dump>.waveview.json) and restored on
  the next start. --gtkw loads a GTKWave save file, and /api/export.gtkw
  writes one, so the same signal list opens in GTKWave too

Security: the server binds to 127.0.0.1 and every request needs a random
token (in the printed URL, then a cookie). On a shared server other users
cannot read the dump through it.

Data is read with tools/wave2img.py's parser via tools/wavequery.py, so VCD
and FST both work, and the condition language is the one wavequery uses.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import http.server
import json
import os
import queue
import re
import secrets
import signal as signal_mod
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wave2img as w2i  # noqa: E402
import wavequery as wq  # noqa: E402

UI_DIR = os.path.join(HERE, "waveview_ui")
DEFAULT_PORT = 8765

# Tiles: a tile of level L covers TILE_BUCKETS buckets of 2**L time units.
# The browser asks for the tiles that cover the visible rows and time range,
# at the level where one bucket is about one pixel, and caches them.
TILE_BUCKETS = 1024
TILE_CACHE = 40000


def registry_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "scariv", "waveview")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Shared view state
# ---------------------------------------------------------------------------


class Hub:
    """The loaded dump, the shared view state, and the push channel."""

    def __init__(self, dump_path, gtkw=None):
        self.dump_path = os.path.abspath(dump_path)
        self.state_path = self.dump_path + ".waveview.json"
        self.lock = threading.RLock()
        self.wave = wq.load(self.dump_path)
        self.gen = 1
        self.mtime = os.stat(self.dump_path).st_mtime_ns
        self.subscribers = []
        self.inbox = []
        self.inbox_cv = threading.Condition(self.lock)
        self.listeners = 0  # agents currently waiting on the inbox
        self.version = 0
        self.state = {
            "signals": [],  # [{name, radix}]
            "cursor": None,
            "marker": None,
            "view": None,  # [t0, t1]
            "selected": [],
            "clock": None,
            "note": None,  # {text, source, at}
            "annotations": [],  # [{time, label, source}]
            "last": None,  # {op, source, client, at}
        }
        self.tiles = collections.OrderedDict()
        self.clockinfo = None
        self._restore()
        if gtkw:
            self._load_gtkw(gtkw)
        self._fix_state()
        self._save_timer = None

    # -- persistence --------------------------------------------------------

    def _restore(self):
        try:
            with open(self.state_path) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        for k in ("signals", "cursor", "marker", "view", "clock", "annotations", "note"):
            if k in saved:
                self.state[k] = saved[k]

    def _save_later(self):
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(1.0, self._save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _save(self):
        with self.lock:
            data = {k: v for k, v in self.state.items() if k not in ("last", "selected")}
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _load_gtkw(self, path):
        flags_radix = {0x2: "hex", 0x4: "dec", 0x8: "bin", 0x10: "oct"}
        radix = "hex"
        sigs = []
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line[0] in "[*-":
                    continue
                if line[0] == "@":
                    try:
                        flags = int(line[1:], 16)
                    except ValueError:
                        continue
                    radix = "sdec" if flags & 0x400 and flags & 0x4 else next(
                        (r for bit, r in flags_radix.items() if flags & bit), "hex"
                    )
                    continue
                name = re.sub(r"\[\d+:\d+\]$", "", line)
                if name in self.wave.by_name:
                    sigs.append({"name": name, "radix": radix})
        if sigs:
            self.state["signals"] = sigs

    def _fix_state(self):
        """Drop what the (possibly rebuilt) dump no longer has."""
        w = self.wave
        st = self.state
        st["signals"] = [s for s in st["signals"] if s.get("name") in w.by_name]
        if st["clock"] not in w.by_name:
            st["clock"] = None
        if st["clock"] is None:
            cands = [c for c in w.clock_candidates() if c[0] > 0]
            st["clock"] = cands[0][1].name if cands else None
        lo, hi = w.start_time, w.end_time
        if not st["view"] or st["view"][1] <= lo or st["view"][0] >= hi:
            st["view"] = [lo, hi]
        for k in ("cursor", "marker"):
            if st[k] is not None and not lo <= st[k] <= hi:
                st[k] = None

    # -- helpers ------------------------------------------------------------

    def timeline(self):
        clk = self.state["clock"]
        return wq.Timeline(self.wave, self.wave.by_name[clk] if clk else None)

    def to_time(self, args, key="time"):
        """A time from args[key] or args['cycle']."""
        if args.get("cycle") is not None:
            return self.timeline().time_of(int(args["cycle"]))
        if args.get(key) is not None:
            return int(args[key]) if not isinstance(args[key], str) else wq.parse_time(args[key])
        return None

    def public_state(self):
        st = dict(self.state)
        st["version"] = self.version
        st["gen"] = self.gen
        st["listeners"] = self.listeners
        st["inbox_pending"] = sum(1 for m in self.inbox if not m.get("read"))
        return st

    def meta(self):
        w = self.wave
        tl = self.timeline()
        return {
            "dump": self.dump_path,
            "timescale": w.timescale,
            "start": w.start_time,
            "end": w.end_time,
            "signals": len(w.signals),
            "clock": self.state["clock"],
            "clocks": [s.name for n, s in w.clock_candidates() if n > 0][:8],
            "cycles": len(tl.edges),
            "gen": self.gen,
            "clockinfo": self.clock_info(),
            "tile_buckets": TILE_BUCKETS,
        }

    def clock_info(self):
        """First edge and period of the clock, and whether every period is
        the same. A regular clock lets the browser number cycles by itself,
        with no edge list on the wire."""
        clk = self.state["clock"]
        key = (self.gen, clk)
        if self.clockinfo and self.clockinfo[0] == key:
            return self.clockinfo[1]
        info = None
        if clk:
            edges = self.wave.edges(self.wave.by_name[clk])
            if len(edges) > 1:
                period = edges[1] - edges[0]
                regular = all(edges[i + 1] - edges[i] == period for i in range(len(edges) - 1))
                info = {"first": edges[0], "period": period, "count": len(edges), "regular": regular}
            elif edges:
                info = {"first": edges[0], "period": 0, "count": 1, "regular": False}
        self.clockinfo = (key, info)
        return info

    def edges_in(self, t0, t1, limit=4000):
        tl = self.timeline()
        if not tl.edges:
            return {"edges": None, "edge0": None}
        lo = bisect.bisect_left(tl.edges, t0)
        hi = bisect.bisect_right(tl.edges, t1)
        if hi - lo > limit:
            return {"edges": None, "edge0": lo, "count": hi - lo}
        return {"edges": tl.edges[lo:hi], "edge0": lo}

    # -- tiles --------------------------------------------------------------

    @staticmethod
    def enc(v, width):
        """Compact value text: hex for known values, x / z when uniform,
        ~bits when mixed."""
        if width == 1:
            return v
        if "x" in v or "z" in v:
            if v.count(v[0]) == len(v):
                return v[0]
            return "~" + v
        return "%x" % int(v, 2)

    def tile(self, sig, level, k):
        key = (self.gen, sig.code, sig.width, level, k)
        hit = self.tiles.get(key)
        if hit is not None:
            self.tiles.move_to_end(key)
            return hit
        t = self._make_tile(sig, level, k)
        self.tiles[key] = t
        if len(self.tiles) > TILE_CACHE:
            self.tiles.popitem(last=False)
        return t

    def _make_tile(self, sig, level, k):
        """One tile: the value before it, then at most one change per
        bucket. A bucket holding several changes becomes one change to its
        final value plus a dense entry [start, end, records, high share].
        A tile with no dense entry is exact: no finer level has more to show.
        Buckets are found by bisection, so a coarse tile costs about one
        step per non empty bucket, not one per change."""
        w = self.wave
        width = sig.width
        times = w.times.get(sig.code, [])
        values = w.values.get(sig.code, [])
        norm = w2i.normalize
        ts = k * (TILE_BUCKETS << level)
        te = ts + (TILE_BUCKETS << level)
        i = bisect.bisect_left(times, ts)
        end = bisect.bisect_left(times, te, i)
        prev = norm(values[i - 1], width) if i > 0 else "x" * width
        v0 = prev
        out_t, out_v, dense = [], [], []
        last_db, last_q = None, None  # bucket and shade of the last dense entry
        while i < end:
            b = (times[i] - ts) >> level
            be = ts + ((b + 1) << level)
            j = bisect.bisect_left(times, be, i, end)
            n = j - i
            if n <= 64:
                trans = []
                for m in range(i, j):
                    v = norm(values[m], width)
                    if v != prev:
                        trans.append((times[m], v))
                        prev = v
                if not trans:
                    i = j
                    continue
                first, last_t, last_v, cnt = trans[0][0], trans[-1][0], trans[-1][1], len(trans)
                hi = sum(1 for _, v in trans if v == "1") / cnt if width == 1 else 0
                is_dense = cnt > 1
            else:
                seg = values[i:j]
                last_v = norm(values[j - 1], width)
                if len(set(seg)) == 1 and last_v == prev:
                    i = j
                    continue
                first = next((times[m] for m in range(i, j) if norm(values[m], width) != prev), times[i])
                last_t, cnt = times[j - 1], n
                hi = seg.count("1") / n if width == 1 else 0
                is_dense = True
                prev = last_v
            if not is_dense:
                out_t.append(first)
                out_v.append(last_v)
                last_db = None
            else:
                # neighbouring dense buckets with the same shade become one
                # entry: zoomed out this is what keeps tiles small
                q = round(hi * 8) if width == 1 else cnt.bit_length()
                if last_db == b - 1 and last_q == q:
                    d = dense[-1]
                    tot = d[2] + cnt
                    d[3] = (d[3] * d[2] + hi * cnt) / tot
                    d[2] = tot
                    d[1] = last_t
                    out_v[-1] = last_v
                else:
                    out_t.append(first)
                    out_v.append(last_v)
                    dense.append([first, last_t, cnt, hi])
                last_db, last_q = b, q
            i = j
        # wire form: times as deltas from the tile start, values compact
        dt, last_t = [], ts
        for t in out_t:
            dt.append(t - last_t)
            last_t = t
        return {
            "v0": self.enc(v0, width),
            "t": dt,
            "v": [self.enc(v, width) for v in out_v],
            "d": [[a - ts, b - ts, n, round(h, 2)] for a, b, n, h in dense],
            "x": 1 if not dense else 0,
        }

    def tiles_for(self, level, want):
        out = {}
        for name, ks in want.items():
            sig = self.wave.by_name.get(name)
            if sig is None:
                continue
            out[name] = {str(k): self.tile(sig, int(level), int(k)) for k in ks[:64]}
        return {"L": level, "gen": self.gen, "tiles": out}

    # -- push ---------------------------------------------------------------

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, kind, payload):
        with self.lock:
            for q in list(self.subscribers):
                q.put((kind, payload))

    # -- commands -----------------------------------------------------------

    def apply(self, op, args, source="ui", client=None):
        """Run one view command. Returns a result dict."""
        with self.lock:
            fn = getattr(self, "op_" + op, None)
            if fn is None:
                raise wq.QueryError("unknown op %r" % op)
            result = fn(args or {}) or {}
            if op not in ("ask",):
                self.version += 1
                self.state["last"] = {"op": op, "source": source, "client": client, "at": time.time()}
                self._save_later()
                self.broadcast("state", self.public_state())
            return result

    def _names(self, pats):
        if isinstance(pats, str):
            pats = [pats]
        return [s.name for s in self.wave.select(pats)]

    def op_add(self, a):
        have = {s["name"] for s in self.state["signals"]}
        names = self._names(a.get("signals", []))
        radix = a.get("radix", "hex")
        new = [{"name": n, "radix": radix} for n in names if n not in have]
        pos = a.get("index")
        sigs = self.state["signals"]
        if pos is None:
            sigs.extend(new)
        else:
            sigs[pos:pos] = new
        return {"added": [s["name"] for s in new]}

    def op_remove(self, a):
        pats = a.get("signals", [])
        drop = set()
        for p in pats if isinstance(pats, list) else [pats]:
            if p in self.wave.by_name:
                drop.add(p)
            else:
                drop.update(self._names([p]))
        before = len(self.state["signals"])
        self.state["signals"] = [s for s in self.state["signals"] if s["name"] not in drop]
        return {"removed": before - len(self.state["signals"])}

    def op_clear(self, a):
        self.state["signals"] = []
        self.state["annotations"] = []
        self.state["note"] = None

    def op_set_signals(self, a):
        sigs = [s for s in a.get("signals", []) if s.get("name") in self.wave.by_name]
        self.state["signals"] = [{"name": s["name"], "radix": s.get("radix", "hex")} for s in sigs]

    def op_radix(self, a):
        names = set()
        for p in a.get("signals") or self.state["selected"]:
            names.update([p] if p in self.wave.by_name else self._names([p]))
        for s in self.state["signals"]:
            if s["name"] in names:
                s["radix"] = a.get("radix", "hex")

    def op_select(self, a):
        self.state["selected"] = [n for n in a.get("signals", []) if n in self.wave.by_name]

    def op_cursor(self, a):
        self.state["cursor"] = self.to_time(a)
        return {"cursor": self.state["cursor"]}

    def op_marker(self, a):
        self.state["marker"] = self.to_time(a)
        return {"marker": self.state["marker"]}

    def op_view(self, a):
        tl = self.timeline()
        if a.get("cycles"):
            c0, c1 = wq.parse_cycles(a["cycles"], len(tl.edges))
            t0 = tl.edges[c0]
            t1 = tl.edges[c1 + 1] if c1 + 1 < len(tl.edges) else self.wave.end_time
        else:
            t0, t1 = float(a["t0"]), float(a["t1"])
        if t1 <= t0:
            raise wq.QueryError("empty view")
        self.state["view"] = [t0, t1]
        return {"view": self.state["view"]}

    def op_fit(self, a):
        self.state["view"] = [self.wave.start_time, self.wave.end_time]

    def op_goto(self, a):
        t = self.to_time(a)
        if t is None:
            raise wq.QueryError("goto needs time or cycle")
        self.state["cursor"] = t
        t0, t1 = self.state["view"]
        span = t1 - t0
        if a.get("span_cycles"):
            tl = self.timeline()
            if len(tl.edges) > 1:
                period = (tl.edges[-1] - tl.edges[0]) / (len(tl.edges) - 1)
                span = period * int(a["span_cycles"])
        self.state["view"] = [t - span / 2, t + span / 2]
        return {"cursor": t}

    def op_clock(self, a):
        name = a.get("name")
        self.state["clock"] = self.wave.resolve(name).name if name else None

    def op_note(self, a):
        text = a.get("text")
        self.state["note"] = {"text": text, "source": a.get("source", "agent"), "at": time.time()} if text else None

    def op_annotate(self, a):
        if a.get("clear"):
            self.state["annotations"] = []
            return
        t = self.to_time(a)
        if t is None:
            t = self.state["cursor"]
        self.state["annotations"].append({"time": t, "label": a.get("label", ""), "source": a.get("source", "agent")})

    def op_find(self, a):
        """Move the cursor to the next / previous cycle where expr holds."""
        expr = wq.Expr(a["expr"], self.wave)
        tl = self.timeline()
        if not tl.edges:
            raise wq.QueryError("find needs a clock")
        start = a.get("from")
        if start is None:
            start = self.state["cursor"] if self.state["cursor"] is not None else self.wave.start_time - 1
        c = tl.cycle_of(start)
        c = -1 if c is None else c
        hit = None
        if a.get("dir", "next") == "next":
            for h in wq.scan(expr, tl, range(c + 1, len(tl.edges))):
                hit = h
                break
        else:
            hi = c
            while hi > 0 and hit is None:
                lo = max(0, hi - 4096)
                hits = list(wq.scan(expr, tl, range(lo, hi)))
                if hits:
                    hit = hits[-1]
                hi = lo
        if hit is None:
            return {"found": False}
        t = tl.edges[hit]
        self.state["cursor"] = t
        t0, t1 = self.state["view"]
        if not t0 <= t <= t1:
            span = t1 - t0
            self.state["view"] = [t - span / 2, t + span / 2]
        return {"found": True, "time": t, "cycle": hit}

    def op_ask(self, a):
        """A message from the person in the browser to the agent."""
        msg = {
            "id": len(self.inbox) + 1,
            "text": a.get("text", ""),
            "at": time.time(),
            "context": self.snapshot(),
        }
        self.inbox.append(msg)
        self.inbox_cv.notify_all()
        self.broadcast("state", self.public_state())
        return {"id": msg["id"]}

    # -- reads --------------------------------------------------------------

    def wave_data(self, names, t0, t1, px):
        """Value changes of names in [t0, t1], merged so that at most about
        one change per pixel is sent. Merged stretches come back as dense."""
        w = self.wave
        px = max(16, min(int(px), 20000))
        bw = max((t1 - t0) / px, 1e-9)
        out = {}
        for n in names:
            sig = w.by_name.get(n)
            if sig is None:
                continue
            times = w.times.get(sig.code, [])
            values = w.values.get(sig.code, [])
            i = bisect.bisect_right(times, t0)
            j = bisect.bisect_right(times, t1)
            v0 = w.value(sig, t0)
            ch, dense = [], []
            last_b = None
            prev = v0
            for k in range(i, j):
                v = w2i.normalize(values[k], sig.width)
                if v == prev:  # a rewrite of the same value is not an edge
                    continue
                prev = v
                t = times[k]
                b = int((t - t0) / bw)
                if b == last_b and ch:
                    ch[-1][1] = v
                    if dense and dense[-1][1] >= ch[-1][0]:
                        dense[-1][1] = t
                    else:
                        dense.append([ch[-1][0], t])
                else:
                    ch.append([t, v])
                    last_b = b
            out[n] = {"w": sig.width, "v0": v0, "ch": ch, "dense": dense}
        tl = self.timeline()
        edges = None
        first = None
        if tl.edges:
            lo = bisect.bisect_left(tl.edges, t0)
            hi = bisect.bisect_right(tl.edges, t1)
            if hi - lo <= 4000:
                edges = tl.edges[lo:hi]
                first = lo
        return {"t0": t0, "t1": t1, "signals": out, "edges": edges, "edge0": first, "gen": self.gen}

    def values_at(self, names, t):
        w = self.wave
        tl = self.timeline()
        return {
            "time": t,
            "cycle": tl.cycle_of(t) if tl.edges else None,
            "values": {n: w.value(w.by_name[n], t) for n in names if n in w.by_name},
        }

    def next_edge(self, name, t, direction):
        """Time of the next / previous real value change of a signal."""
        w = self.wave
        sig = w.resolve(name)
        times = w.times.get(sig.code, [])
        values = w.values.get(sig.code, [])
        norm = lambda k: w2i.normalize(values[k], sig.width)  # noqa: E731
        if direction == "next":
            cur = w.value(sig, t)
            for k in range(bisect.bisect_right(times, t), len(times)):
                if norm(k) != cur:
                    return times[k]
            return None
        # the start of the run of equal values that ends just before t; the
        # very first record is the initial value, not an edge
        k = bisect.bisect_left(times, t) - 1
        if k < 0:
            return None
        v = norm(k)
        while k > 0 and norm(k - 1) == v:
            k -= 1
        return times[k] if k > 0 else None

    def snapshot(self, table=False, radix="hex", max_rows=60):
        st = self.state
        w = self.wave
        tl = self.timeline()
        names = [s["name"] for s in st["signals"]]

        def cyc(t):
            if t is None or not tl.edges:
                return None
            return tl.cycle_of(t)

        v0, v1 = st["view"]
        snap = {
            "dump": self.dump_path,
            "gen": self.gen,
            "clock": st["clock"],
            "view": {"t0": v0, "t1": v1, "c0": cyc(v0), "c1": cyc(v1)},
            "cursor": {"time": st["cursor"], "cycle": cyc(st["cursor"])},
            "marker": {"time": st["marker"], "cycle": cyc(st["marker"])},
            "selected": st["selected"],
            "note": st["note"],
            "annotations": st["annotations"],
            "signals": [],
        }
        for s in st["signals"]:
            sig = w.by_name[s["name"]]
            row = {"name": s["name"], "width": sig.width, "radix": s["radix"]}
            for key in ("cursor", "marker"):
                t = st[key]
                row[key] = None if t is None else wq.json_value(w.value(sig, t), sig.width)
            snap["signals"].append(row)
        if table and tl.edges and names:
            cycles = tl.cycle_range(v0, v1)
            rows = []
            for c in list(cycles)[:max_rows]:
                t = tl.edges[c]
                rows.append([c, t] + [wq.fmt(w.value(w.by_name[n], t), w.by_name[n].width, radix) for n in names])
            snap["table"] = {"rows": rows, "total": len(cycles)}
        return snap

    # -- reload -------------------------------------------------------------

    def watch(self):
        """Reload the dump when the simulation rewrites it."""
        last_seen = None
        while True:
            time.sleep(1.0)
            try:
                st = os.stat(self.dump_path)
            except OSError:
                continue
            sig = (st.st_mtime_ns, st.st_size)
            if st.st_mtime_ns == self.mtime:
                last_seen = None
                continue
            if sig != last_seen:  # still being written; wait until it settles
                last_seen = sig
                continue
            try:
                wave = wq.load(self.dump_path)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("waveview: reload failed, will retry: %s\n" % exc)
                last_seen = None
                continue
            with self.lock:
                self.wave = wave
                self.mtime = st.st_mtime_ns
                self.gen += 1
                self._fix_state()
                self.version += 1
                self.state["last"] = {"op": "reload", "source": "file", "client": None, "at": time.time()}
                self.broadcast("reload", self.meta())
                self.broadcast("state", self.public_state())
            sys.stderr.write("waveview: reloaded %s (gen %d)\n" % (self.dump_path, self.gen))

    def export_gtkw(self):
        lines = [
            "[*] written by scariv tools/waveview.py",
            '[dumpfile] "%s"' % self.dump_path,
            "[timestart] %d" % int(self.state["view"][0]),
        ]
        flags = {"hex": "22", "dec": "24", "sdec": "424", "bin": "28", "oct": "30"}
        current = None
        for s in self.state["signals"]:
            sig = self.wave.by_name[s["name"]]
            f = flags.get(s["radix"], "22") if sig.width > 1 else "28"
            if f != current:
                lines.append("@" + f)
                current = f
            lines.append(s["name"] + ("[%d:0]" % (sig.width - 1) if sig.width > 1 else ""))
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

STATIC_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}


class Handler(http.server.BaseHTTPRequestHandler):
    hub: Hub = None
    token: str = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        if os.environ.get("WAVEVIEW_DEBUG"):
            sys.stderr.write("waveview: " + fmt % args + "\n")

    # -- plumbing -----------------------------------------------------------

    def _authed(self, query):
        if query.get("token", [None])[0] == self.token:
            return True
        if self.headers.get("X-Waveview-Token") == self.token:
            return True
        cookie = self.headers.get("Cookie", "")
        return ("wvtoken=" + self.token) in cookie

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":"))
        if isinstance(body, str):
            body = body.encode()
        extra = dict(extra or {})
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=5)
            extra["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.endswith("json") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode() or "{}")

    def _route(self, method):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        if not self._authed(query):
            return self._send(403, {"error": "missing or wrong token; open the URL waveview printed"})
        if method == "GET" and "token" in query and not path.startswith("/api/"):
            # move the token into a cookie and drop it from the address bar
            return self._send(
                302, b"", "text/plain",
                {"Location": path, "Set-Cookie": "wvtoken=%s; Path=/; HttpOnly; SameSite=Strict" % self.token},
            )
        hub = self.hub
        try:
            if path == "/" or path.startswith("/ui/"):
                return self._static(path)
            if path == "/api/events":
                return self._events()
            if path == "/api/meta":
                return self._send(200, hub.meta())
            if path == "/api/state":
                with hub.lock:
                    return self._send(200, hub.public_state())
            if path == "/api/signals":
                with hub.lock:
                    return self._send(200, [[s.name, s.width] for s in hub.wave.signals])
            if path == "/api/snapshot":
                table = query.get("table", ["0"])[0] == "1"
                with hub.lock:
                    return self._send(200, hub.snapshot(table=table, radix=query.get("radix", ["hex"])[0]))
            if path == "/api/export.gtkw":
                with hub.lock:
                    return self._send(200, hub.export_gtkw(), "text/plain",
                                      {"Content-Disposition": 'attachment; filename="waveview.gtkw"'})
            if path == "/api/inbox":
                return self._inbox(query)
            if method == "POST":
                body = self._body()
                if path == "/api/cmd":
                    res = hub.apply(body["op"], body.get("args"), body.get("source", "ui"), body.get("client"))
                    return self._send(200, {"ok": True, "result": res, "version": hub.version})
                if path == "/api/wave":
                    with hub.lock:
                        return self._send(200, hub.wave_data(body["signals"], float(body["t0"]), float(body["t1"]),
                                                             body.get("px", 1000)))
                if path == "/api/tiles":
                    with hub.lock:
                        return self._send(200, hub.tiles_for(int(body["L"]), body["want"]))
                if path == "/api/edges":
                    with hub.lock:
                        return self._send(200, hub.edges_in(float(body["t0"]), float(body["t1"])))
                if path == "/api/values":
                    with hub.lock:
                        return self._send(200, hub.values_at(body["signals"], float(body["t"])))
                if path == "/api/next_edge":
                    with hub.lock:
                        t = hub.next_edge(body["signal"], float(body["t"]), body.get("dir", "next"))
                        return self._send(200, {"time": t})
            return self._send(404, {"error": "no such endpoint %s" % path})
        except wq.QueryError as exc:
            return self._send(400, {"error": str(exc)})
        except (KeyError, ValueError, TypeError) as exc:
            return self._send(400, {"error": "bad request: %r" % exc})
        except (BrokenPipeError, ConnectionResetError):
            return None

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _static(self, path):
        rel = "index.html" if path == "/" else path[len("/ui/"):]
        full = os.path.normpath(os.path.join(UI_DIR, rel))
        if not full.startswith(UI_DIR + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as fh:
            data = fh.read()
        return self._send(200, data, STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    def _events(self):
        hub = self.hub
        q = hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True
        try:
            with hub.lock:
                first = hub.public_state()
            self._sse("state", first)
            while True:
                try:
                    kind, payload = q.get(timeout=15)
                    self._sse(kind, payload)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(q)

    def _sse(self, kind, payload):
        data = json.dumps(payload, separators=(",", ":"))
        self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, data)).encode())
        self.wfile.flush()

    def _inbox(self, query):
        hub = self.hub
        after = int(query.get("after", ["0"])[0])
        wait = min(float(query.get("wait", ["0"])[0]), 3600)
        deadline = time.time() + wait
        with hub.lock:
            hub.listeners += 1
            hub.broadcast("state", hub.public_state())
            try:
                while True:
                    msgs = [m for m in hub.inbox if m["id"] > after]
                    left = deadline - time.time()
                    if msgs or left <= 0:
                        break
                    hub.inbox_cv.wait(timeout=min(left, 5))
                for m in msgs:
                    m["read"] = True
            finally:
                hub.listeners -= 1
                hub.broadcast("state", hub.public_state())
        return self._send(200, {"messages": msgs})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def cmd_serve(args):
    hub = Hub(args.dump, gtkw=args.gtkw)
    token = args.token or secrets.token_urlsafe(16)
    Handler.hub = hub
    Handler.token = token
    port = args.port
    httpd = None
    for p in ([port] if port else range(DEFAULT_PORT, DEFAULT_PORT + 50)):
        try:
            httpd = Server((args.host, p), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        sys.stderr.write("waveview: no free port\n")
        return 1
    port = httpd.server_address[1]
    url = "http://localhost:%d/" % port
    reg = os.path.join(registry_dir(), "%d.json" % port)
    with open(os.open(reg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump({"url": url, "token": token, "dump": hub.dump_path, "pid": os.getpid(),
                   "started": time.time(), "host": args.host, "port": port}, fh)

    def cleanup(*_):
        try:
            os.remove(reg)
        except OSError:
            pass
        os._exit(0)

    signal_mod.signal(signal_mod.SIGTERM, cleanup)
    signal_mod.signal(signal_mod.SIGINT, cleanup)
    threading.Thread(target=hub.watch, daemon=True).start()
    hostname = os.uname().nodename
    print("waveview: serving %s" % hub.dump_path)
    print("  open      %s?token=%s" % (url, token))
    print("  remote    ssh -L %d:localhost:%d %s   (VS Code Remote forwards it by itself)" % (port, port, hostname))
    print("  control   tools/waveview.py add '<pattern>' | goto --cycle N | look | note TEXT")
    sys.stdout.flush()
    if args.open:
        import webbrowser

        webbrowser.open(url + "?token=" + token)
    httpd.serve_forever()
    return 0


# ---------------------------------------------------------------------------
# Command line client
# ---------------------------------------------------------------------------


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def sessions():
    out = []
    d = registry_dir()
    for f in os.listdir(d):
        if not f.endswith(".json"):
            continue
        p = os.path.join(d, f)
        try:
            with open(p) as fh:
                s = json.load(fh)
        except (OSError, ValueError):
            continue
        if not _alive(s.get("pid", -1)):
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        out.append(s)
    out.sort(key=lambda s: -s["started"])
    return out


class Client:
    def __init__(self, dump=None, url=None, token=None):
        url = url or os.environ.get("WAVEVIEW_URL")
        token = token or os.environ.get("WAVEVIEW_TOKEN")
        if not url:
            ss = sessions()
            if dump:
                want = os.path.abspath(dump)
                ss = [s for s in ss if s["dump"] == want]
            if not ss:
                raise wq.QueryError(
                    "no running waveview found%s. Start one with: tools/waveview.py serve <dump>"
                    % (" for %s" % dump if dump else "")
                )
            url, token = ss[0]["url"], ss[0]["token"]
        self.url = url.rstrip("/")
        self.token = token
        # localhost must not go through HTTP(S)_PROXY
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET")
        req.add_header("X-Waveview-Token", self.token or "")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                is_json = "json" in resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                raise wq.QueryError(json.loads(raw).get("error", raw))
            except ValueError:
                raise wq.QueryError(raw)
        except urllib.error.URLError as exc:
            raise wq.QueryError("cannot reach waveview at %s: %s" % (self.url, exc.reason))
        return json.loads(raw) if is_json else raw

    def cmd(self, op, **args):
        args = {k: v for k, v in args.items() if v is not None}
        return self.call("/api/cmd", {"op": op, "args": args, "source": "agent"})


def print_snapshot(snap, radix="hex"):
    def tc(d):
        if d["time"] is None:
            return "-"
        return "%s%s" % (_t(d["time"]), "" if d["cycle"] is None else " (cycle %d)" % d["cycle"])

    v = snap["view"]
    print("dump    %s  (load #%d)" % (snap["dump"], snap["gen"]))
    print("clock   %s" % snap["clock"])
    print("view    time %s .. %s%s" % (_t(v["t0"]), _t(v["t1"]),
                                        "" if v["c0"] is None else "  cycles %s .. %s" % (v["c0"], v["c1"])))
    print("cursor  %s" % tc(snap["cursor"]))
    print("marker  %s" % tc(snap["marker"]))
    if snap["cursor"]["time"] is not None and snap["marker"]["time"] is not None:
        d = snap["cursor"]["time"] - snap["marker"]["time"]
        dc = ""
        if snap["cursor"]["cycle"] is not None and snap["marker"]["cycle"] is not None:
            dc = " (%d cycles)" % (snap["cursor"]["cycle"] - snap["marker"]["cycle"])
        print("delta   %s%s" % (_t(d), dc))
    if snap["note"]:
        print("note    [%s] %s" % (snap["note"]["source"], snap["note"]["text"]))
    for a in snap["annotations"]:
        print("label   %s  %s" % (_t(a["time"]), a["label"]))
    if snap["selected"]:
        print("selected %s" % ", ".join(snap["selected"]))
    sigs = snap["signals"]
    if not sigs:
        print("signals (none shown)")
        return
    scope, short = wq.short_names([w2i.Signal(s["name"], s["width"], "", 0) for s in sigs])
    print("signals%s  (value at cursor | at marker)" % (" in " + scope if scope else ""))
    rows = []
    for s, n in zip(sigs, short):
        r = s["radix"] if s["radix"] in ("hex", "dec", "bin") else radix

        def f(v, s=s, r=r):
            if v is None:
                return "-"
            bits = format(v, "0%db" % s["width"]) if isinstance(v, int) else v
            return wq.fmt(bits, s["width"], r)

        mark = "*" if s["name"] in snap["selected"] else " "
        rows.append((mark + n, f(s["cursor"]), f(s["marker"])))
    for line in wq.text_table(["signal", "cursor", "marker"], rows):
        print("  " + line)
    if "table" in snap:
        tb = snap["table"]
        print("visible cycles (%d%s):" % (tb["total"], ", first %d shown" % len(tb["rows"]) if tb["total"] > len(tb["rows"]) else ""))
        for line in wq.text_table(["cycle", "time"] + short, tb["rows"]):
            print("  " + line)


def _t(t):
    if t is None:
        return "-"
    return str(int(t)) if float(t).is_integer() else "%.1f" % t


def run_client(args):
    if args.cmd == "status":
        ss = sessions()
        if not ss:
            print("no waveview running")
        for s in ss:
            print("%s  pid %d  %s" % (s["url"], s["pid"], s["dump"]))
        return 0
    c = Client(args.dump, args.url)
    j = getattr(args, "json", False)

    def show(res):
        if j:
            print(json.dumps(res))
        return res

    if args.cmd == "add":
        r = c.cmd("add", signals=args.signals, radix=args.radix)
        show(r) if j else print("added %d: %s" % (len(r["result"]["added"]), " ".join(r["result"]["added"]) or "(already shown)"))
    elif args.cmd == "remove":
        r = c.cmd("remove", signals=args.signals)
        show(r) if j else print("removed %d" % r["result"]["removed"])
    elif args.cmd == "clear":
        show(c.cmd("clear"))
    elif args.cmd == "goto":
        r = c.cmd("goto", time=args.time, cycle=args.cycle, span_cycles=args.span)
        show(r) if j else print("cursor at %s" % _t(r["result"]["cursor"]))
    elif args.cmd == "cursor":
        show(c.cmd("cursor", time=args.time, cycle=args.cycle))
    elif args.cmd == "marker":
        show(c.cmd("marker", time=args.time, cycle=args.cycle))
    elif args.cmd == "zoom":
        if args.fit:
            show(c.cmd("fit"))
        elif args.cycles:
            show(c.cmd("view", cycles=args.cycles))
        else:
            show(c.cmd("view", t0=wq.parse_time(args.time_from), t1=wq.parse_time(args.time_to)))
    elif args.cmd == "radix":
        show(c.cmd("radix", signals=args.signals, radix=args.radix))
    elif args.cmd == "note":
        show(c.cmd("note", text=" ".join(args.text) if args.text else None))
    elif args.cmd == "annotate":
        show(c.cmd("annotate", time=args.time, cycle=args.cycle, label=" ".join(args.label), clear=args.clear or None))
    elif args.cmd == "clock":
        show(c.cmd("clock", name=args.name))
    elif args.cmd == "find":
        r = c.cmd("find", expr=args.expr, dir="prev" if args.prev else "next")
        res = r["result"]
        if j:
            show(r)
        elif res["found"]:
            print("cursor moved to cycle %d (time %s)" % (res["cycle"], _t(res["time"])))
        else:
            print("no match %s the cursor" % ("before" if args.prev else "after"))
            return 1
    elif args.cmd == "look":
        snap = c.call("/api/snapshot?table=%d" % (1 if args.table else 0))
        print(json.dumps(snap)) if j else print_snapshot(snap, args.radix)
    elif args.cmd == "inbox":
        after = args.after
        r = c.call("/api/inbox?after=%d&wait=%d" % (after, args.wait if args.wait else 0),
                   timeout=(args.wait or 0) + 30)
        msgs = r["messages"]
        if j:
            print(json.dumps(msgs))
        elif not msgs:
            print("no message")
            return 1
        for m in msgs:
            print("message #%d from the viewer: %s" % (m["id"], m["text"]))
            print("view when it was sent:")
            print_snapshot(m["context"])
    elif args.cmd == "gtkw":
        sys.stdout.write(c.call("/api/export.gtkw"))
    return 0


def run_selftest():
    """Start a server on the wave2img synthetic dump and drive it over HTTP."""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="waveview_selftest_")
    os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cache")
    path = os.path.join(tmp, "t.vcd")
    w2i.write_selftest_vcd(path)
    hub = Hub(path)
    Handler.hub, Handler.token = hub, "tok"
    httpd = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(url="http://127.0.0.1:%d" % httpd.server_address[1], token="tok")
    fails = 0

    def check(label, ok):
        nonlocal fails
        print("%-4s %s" % ("ok" if ok else "FAIL", label))
        fails += 0 if ok else 1

    check("meta finds the clock", c.call("/api/meta")["clock"] == "tb_wave.dut.clk")
    check("add by glob and suffix", c.cmd("add", signals=["*req", "data", "state"])["result"]["added"]
          == ["tb_wave.dut.req", "tb_wave.dut.data", "tb_wave.dut.state"])
    check("goto cycle", c.cmd("goto", cycle=5)["result"]["cursor"] == 50)
    r = c.call("/api/values", {"signals": ["tb_wave.dut.data"], "t": 50})
    check("value at cursor", int(r["values"]["tb_wave.dut.data"], 2) == 0xBEEF and r["cycle"] == 5)
    check("find next", c.cmd("find", expr="rose(req)")["result"].get("cycle") == 7)
    check("find prev", c.cmd("find", expr="rose(req)", dir="prev")["result"].get("cycle") == 2)
    check("next edge skips nothing", c.call("/api/next_edge", {"signal": "req", "t": 20, "dir": "next"})["time"] == 30)
    d = c.call("/api/wave", {"signals": ["tb_wave.dut.req"], "t0": 0, "t1": 195, "px": 1000})
    check("wave data", [t for t, _ in d["signals"]["tb_wave.dut.req"]["ch"]] == [20, 30, 70, 90] and len(d["edges"]) == 20)
    d = c.call("/api/wave", {"signals": ["tb_wave.dut.data"], "t0": 0, "t1": 195, "px": 16})
    check("dense merge", len(d["signals"]["tb_wave.dut.data"]["dense"]) > 0)
    t = c.call("/api/tiles", {"L": 0, "want": {"tb_wave.dut.req": [0]}})["tiles"]["tb_wave.dut.req"]["0"]
    abs_t, acc = [], 0
    for d in t["t"]:
        acc += d
        abs_t.append(acc)
    check("level 0 tile is exact", t["x"] == 1 and abs_t == [0, 20, 30, 70, 90] and t["v"] == ["0", "1", "0", "1", "0"])
    t = c.call("/api/tiles", {"L": 0, "want": {"tb_wave.dut.data": [0]}})["tiles"]["tb_wave.dut.data"]["0"]
    check("tile values are compact hex", "beef" in t["v"] and t["v0"] == "x")
    coarse = c.call("/api/tiles", {"L": 0, "want": {"tb_wave.dut.clk": [0]}})
    check("clock tile exact at level 0", coarse["tiles"]["tb_wave.dut.clk"]["0"]["x"] == 1)
    TB = TILE_BUCKETS
    # a level where one bucket spans two clock periods must summarise
    lvl = 5  # 32 time units per bucket, the clock toggles every 5
    t = c.call("/api/tiles", {"L": lvl, "want": {"tb_wave.dut.clk": [0]}})["tiles"]["tb_wave.dut.clk"]["0"]
    check("coarse tile summarises with dense stretches", t["x"] == 0 and len(t["d"]) > 0
          and all(0 <= d[3] <= 1 for d in t["d"]) and TB == 1024)
    check("regular clock info", c.call("/api/meta")["clockinfo"] == {"first": 0, "period": 10, "count": 20, "regular": True})
    c.cmd("note", text="hello")
    snap = c.call("/api/snapshot?table=1")
    check("snapshot", snap["note"]["text"] == "hello" and snap["cursor"]["cycle"] == 2 and snap["table"]["total"] > 0)
    c.cmd("ask", text="why?")
    check("inbox", c.call("/api/inbox?after=0")["messages"][0]["text"] == "why?")
    check("gtkw export", "tb_wave.dut.data[15:0]" in c.call("/api/export.gtkw"))
    bad = Client(url=c.url, token="wrong")
    try:
        bad.call("/api/meta")
        check("token required", False)
    except wq.QueryError:
        check("token required", True)
    httpd.shutdown()
    hub._save()
    check("state saved next to the dump", os.path.isfile(path + ".waveview.json"))
    print("selftest: %s" % ("PASS" if not fails else "%d FAILED" % fails))
    return 1 if fails else 0


def build_parser():
    p = argparse.ArgumentParser(prog="waveview", description=__doc__.split("\n")[0],
                                epilog=__doc__.split("\n", 2)[2], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the viewer on a dump")
    s.add_argument("dump")
    s.add_argument("--port", type=int, default=0, help="port (default: first free from %d)" % DEFAULT_PORT)
    s.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    s.add_argument("--token", help="fixed access token (default: random)")
    s.add_argument("--gtkw", help="load the signal list from a GTKWave save file")
    s.add_argument("--open", action="store_true", help="open a local browser")

    client = argparse.ArgumentParser(add_help=False)
    client.add_argument("--dump", help="which running viewer (default: the most recent)")
    client.add_argument("--url", help="viewer URL (default: found automatically)")
    client.add_argument("--json", action="store_true", help="JSON output")

    def tc(sp):
        sp.add_argument("--time", type=wq.parse_time)
        sp.add_argument("--cycle", type=int)

    sub.add_parser("status", help="list running viewers")
    sub.add_parser("selftest", help="run the built-in self test")
    s = sub.add_parser("add", parents=[client], help="add signals (glob or unique suffix)")
    s.add_argument("signals", nargs="+")
    s.add_argument("--radix", default="hex", choices=("hex", "dec", "sdec", "bin", "oct"))
    s = sub.add_parser("remove", parents=[client], help="remove signals")
    s.add_argument("signals", nargs="+")
    sub.add_parser("clear", parents=[client], help="remove all signals, notes and labels")
    s = sub.add_parser("goto", parents=[client], help="put the cursor there and center the view")
    tc(s)
    s.add_argument("--span", type=int, help="also zoom to this many cycles")
    s = sub.add_parser("cursor", parents=[client], help="set the cursor")
    tc(s)
    s = sub.add_parser("marker", parents=[client], help="set the marker (no argument clears it)")
    tc(s)
    s = sub.add_parser("zoom", parents=[client], help="set the visible window")
    s.add_argument("--cycles")
    s.add_argument("--from", dest="time_from")
    s.add_argument("--to", dest="time_to")
    s.add_argument("--fit", action="store_true")
    s = sub.add_parser("radix", parents=[client], help="set the radix of shown signals")
    s.add_argument("radix", choices=("hex", "dec", "sdec", "bin", "oct"))
    s.add_argument("signals", nargs="+")
    s = sub.add_parser("note", parents=[client], help="show a note banner in the viewer (no text clears it)")
    s.add_argument("text", nargs="*")
    s = sub.add_parser("annotate", parents=[client], help="put a labelled flag on the time axis")
    tc(s)
    s.add_argument("label", nargs="*")
    s.add_argument("--clear", action="store_true")
    s = sub.add_parser("clock", parents=[client], help="set the clock used for cycle numbers")
    s.add_argument("name")
    s = sub.add_parser("find", parents=[client], help="move the cursor to the next cycle where EXPR holds")
    s.add_argument("expr")
    s.add_argument("--prev", action="store_true")
    s = sub.add_parser("look", parents=[client], help="print what the viewer shows")
    s.add_argument("--table", action="store_true", help="also print the visible cycles")
    s.add_argument("--radix", default="hex", choices=("hex", "dec", "bin"))
    s = sub.add_parser("inbox", parents=[client], help="read messages sent with 'Send to AI'")
    s.add_argument("--wait", type=int, default=0, help="block up to this many seconds for one")
    s.add_argument("--after", type=int, default=0, help="only messages with a larger id")
    sub.add_parser("gtkw", parents=[client], help="print the view as a GTKWave save file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "serve":
            return cmd_serve(args)
        if args.cmd == "selftest":
            return run_selftest()
        return run_client(args)
    except wq.QueryError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
