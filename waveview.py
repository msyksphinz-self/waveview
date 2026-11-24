#!/usr/bin/env python3
"""waveview: a GTKWave / Verdi style waveform viewer in the browser, shared
between a person and an AI agent, built for remote development.

    waveview.py serve w.fst              start a viewer on the dump
    waveview.py add '*u_rob.commit_*'    (from any shell) add signals
    waveview.py goto --cycle 1234        move the cursor and the view
    waveview.py note "flush comes from the BRU mispredict here"
    waveview.py look                     what the viewer shows, as text
    waveview.py inbox --wait             wait for "Send to AI" from the UI
    waveview.py serve --rc bug.wvrc      open a saved debug view: signals,
                                         named cursors, arrows, comments
    waveview.py load bug.wvrc | save bug.wvrc   (see waverc.py for the format)

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
  ~/.cache/waveview/server), so an agent does not need to know ports or tokens
- When the simulation rewrites the dump, the viewer reloads it in place and
  keeps the signal list, cursor and zoom
- The view is saved next to the dump (<dump>.waveview.json) and restored on
  the next start. --gtkw loads a GTKWave save file, and /api/export.gtkw
  writes one, so the same signal list opens in GTKWave too

From a desktop (Windows and macOS included) the dump can stay on the build
server: `serve build01:/work/sim/w.fst` reads it through fstsrv.c running
there over ssh (remote.py); only what the view asks for crosses the link.
An FSDB there is read the same way through fsdbsrv.cc on the server's Verdi.

Security: the server binds to 127.0.0.1 and every request needs a random
token (in the printed URL, then a cookie). On a shared server other users
cannot read the dump through it.

FST is read in place, on demand, through wavesource.py: nothing is
converted or loaded up front, so a dump of any size opens at once. VCD is
parsed into memory. The condition language is the one wavequery uses.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import hashlib
import http.server
import json
import os
import platform
import queue
import re
import secrets
import signal as signal_mod
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wave2img as w2i  # noqa: E402
import wavequery as wq  # noqa: E402
import remote  # noqa: E402
import rtlindex  # noqa: E402
import waverc  # noqa: E402
import opener  # noqa: E402
import ifaces  # noqa: E402

UI_DIR = os.path.join(HERE, "waveview_ui")
DEFAULT_PORT = 8765

# Tiles: a tile of level L covers TILE_BUCKETS buckets of 2**L time units.
# The browser asks for the tiles that cover the visible rows and time range,
# at the level where one bucket is about one pixel, and caches them.
TILE_BUCKETS = 1024
TILE_CACHE = 40000


def registry_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "waveview", "server")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Shared view state
# ---------------------------------------------------------------------------


class Hub:
    """The loaded dump, the shared view state, and the push channel."""

    def __init__(self, dump_path, gtkw=None, rtl=None, rc=None, scope=None):
        self.scope = scope  # only the signals below these scopes are loaded
        if remote.is_remote(dump_path):
            # a dump on another machine: the view state is kept here
            self.dump_path = dump_path
            key = hashlib.sha1(dump_path.encode()).hexdigest()[:16]
            self.state_path = os.path.join(registry_dir(), "state", key + ".json")
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        else:
            self.dump_path = os.path.abspath(dump_path)
            self.state_path = self.dump_path + ".waveview.json"
        self.lock = threading.RLock()
        self.retired = False
        # the viewer walks the signal list (hierarchy, search): no name index
        self.wave = wq.load(self.dump_path, scope=self.scope, index=False)
        self.gen = 1
        self.mtime = self._stamp()
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
            "cursors": [],  # named cursors [{name, time, label, color}]
            "arrows": [],  # [{from: {signal, time}, to: {signal, time}, label, color}]
            "comments": [],  # [{signal, time, text, color}]; signal None: on the time axis
            "source": None,  # {scope, line, signal, kind} shown in the source pane
            "iflabels": {},  # names given to interface bundles of the diagram: {scope: {"box/bundle": {text, source}}}
            "last": None,  # {op, source, client, at}
        }
        self.tiles = collections.OrderedDict()
        self.rtl_specs = rtl
        self.rtl_root = os.environ.get("WAVEVIEW_RTL_ROOT")  # remote: base of the shown file names
        self.src = None
        self.src_status = "not indexed"
        self.src_mtimes = {}
        self.src_checked = 0.0
        if rtl is not None:
            threading.Thread(target=self.build_index, daemon=True).start()
        self.clockinfo = None
        self._restore()
        self.rc_warnings = []
        if gtkw:
            self._load_gtkw(gtkw)
        if rc:
            self._fix_state()  # a clock, so that the file can count cycles
            with open(rc, errors="replace") as fh:
                text = fh.read()
            if waverc.is_nwave(text):
                self._load_rc(rc)
            else:
                self.rc_warnings = self.load_wvrc(text, base_dir=os.path.dirname(os.path.abspath(rc)),
                                                  origin=os.path.basename(rc))
                for w in self.rc_warnings:
                    sys.stderr.write("waveview: %s\n" % w)
        self._fix_state()
        self._save_timer = None

    # -- persistence --------------------------------------------------------

    def _restore(self):
        try:
            with open(self.state_path) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        for k in ("signals", "cursor", "marker", "view", "clock", "annotations", "note",
                  "cursors", "arrows", "comments", "iflabels"):
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

    # -- Verdi nWave signal files (.rc) ---------------------------------------

    def _rc_lookup(self, path):
        """A dump signal for an nWave path (/tb/u_dut/x[31:0]). FSDB from a
        commercial simulator has no TOP prefix and Verilator dumps do, so
        the name is matched as a unique suffix."""
        name = re.sub(r"\[\d+:\d+\]$", "", path.strip().strip('"')).strip("/").replace("/", ".")
        if name in self.wave.by_name:
            return name
        tail = "." + name
        hits = [s.name for s in self.wave.signals if s.name.endswith(tail)]
        return hits[0] if len(hits) == 1 else None

    def _load_rc(self, path):
        """Signal list, groups (as comment-less order), cursor, marker and
        zoom of an nWave save file."""
        sigs, missing = [], []
        with open(path, errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith(";"):
                    continue
                words = line.split()
                cmd = words[0]
                try:
                    if cmd == "addSignal":
                        radix = "hex"
                        flags = {w.upper() for w in words[1:] if w.startswith("-")}
                        if "-DEC" in flags:
                            radix = "sdec" if flags & {"-2COMP", "-SIGNED"} else "dec"
                        elif "-BIN" in flags:
                            radix = "bin"
                        elif "-OCT" in flags:
                            radix = "oct"
                        args = [w for k, w in enumerate(words[1:], 1)
                                if not w.startswith("-") and words[k - 1] not in ("-h", "-w", "-c", "-color", "-ls")]
                        if not args:
                            continue
                        name = self._rc_lookup(args[-1])
                        if name:
                            sigs.append({"name": name, "radix": radix})
                        else:
                            missing.append(args[-1])
                    elif cmd == "cursor":
                        self.state["cursor"] = float(words[1])
                    elif cmd == "marker" and float(words[1]) > 0:
                        self.state["marker"] = float(words[1])
                    elif cmd == "zoom" and len(words) >= 3:
                        self.state["view"] = [float(words[1]), float(words[2])]
                except (ValueError, IndexError):
                    continue
        if sigs:
            self.state["signals"] = sigs
        if missing:
            sys.stderr.write("waveview: %d signals of %s are not in the dump, e.g. %s\n"
                             % (len(missing), path, ", ".join(missing[:3])))

    def export_rc(self):
        """The view as an nWave save file, to open the same signals in Verdi."""
        st = self.state
        unit = re.sub(r"^1(?=[a-z])", "", self.wave.timescale) or "ps"
        lines = [
            "Magic 271485",
            "Revision waveview",
            "; written by waveview",
            "; File list:",
            'openDirFile -d / "" "%s"' % self.dump_path,
            "windowTimeUnit %s" % ("1" + unit if unit[0].isalpha() else unit),
            "zoom %f %f" % (st["view"][0], st["view"][1]),
        ]
        if st["cursor"] is not None:
            lines.append("cursor %f" % st["cursor"])
        if st["marker"] is not None:
            lines.append("marker %f" % st["marker"])
        lines += ["; signal list", 'addGroup "G1"', 'activeDirFile "" "%s"' % self.dump_path]
        flags = {"hex": "-HEX", "dec": "-UNSIGNED -DEC", "sdec": "-2COMP -DEC", "bin": "-BIN", "oct": "-OCT"}
        for s in st["signals"]:
            sig = self.wave.by_name[s["name"]]
            comps = s["name"].split(".")
            if comps[0] == "TOP" and len(comps) > 2:  # Verilator's wrapper scope
                comps = comps[1:]
            path = "/" + "/".join(comps) + ("[%d:0]" % (sig.width - 1) if sig.width > 1 else "")
            f = flags.get(s["radix"], "-HEX") + " " if sig.width > 1 else ""
            lines.append("addSignal -h 15 %s%s" % (f, path))
        lines += ['addGroup "G2"', "", "; getSignalForm Scope Hierarchy Status", "; active file of getSignalForm"]
        return "\n".join(lines) + "\n"

    # -- waveview rc (.wvrc) ----------------------------------------------------

    def load_wvrc(self, text, merge=False, base_dir=".", origin="rc"):
        """Apply a .wvrc. Without merge the file replaces the view: named
        cursors, arrows, comments, labels and the note, and the signal list
        when the file has signal lines. Returns the warnings, one per line
        that could not be applied."""
        import copy

        st = copy.deepcopy({k: v for k, v in self.state.items() if k != "last"})
        if not merge:
            st.update(cursors=[], arrows=[], comments=[], annotations=[], note=None)
            if re.search(r"^\s*(signal|add)\s", text, re.M):
                st["signals"] = []
        w = self.wave
        rd = waverc.Reader(w, lambda: wq.Timeline(w, w.by_name[st["clock"]] if st.get("clock") else None),
                           st, base_dir)
        rd.run(text, origin)
        if not merge:
            rd.finish()
        for k in ("signals", "cursor", "marker", "view", "clock", "note", "cursors", "arrows", "comments",
                  "annotations"):
            self.state[k] = st[k]
        return rd.warnings

    def export_wvrc(self):
        return waverc.write(self.state, self.wave, self.timeline(), self.dump_path)

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
            if hasattr(edges, "period"):  # a regular clock of a lazy source
                info = {"first": edges.first, "period": edges.period, "count": len(edges), "regular": True}
            elif len(edges) > 1:
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

    def tile_key(self, sig, level, k):
        return (self.gen, sig.code, sig.width, level, k)

    def make_tiles(self, sigs, level, k):
        """Tiles of slot (level, k) for several signals in one pass over the
        source. From the source's bucket summaries: a bucket with one change
        is a point; with more it is a dense stretch [start, end, changes,
        share of 1s], and neighbouring dense buckets of the same shade merge
        (that is what keeps zoomed out tiles small). A tile without dense
        stretches is exact: no finer level has more to show."""
        ts = k * (TILE_BUCKETS << level)
        summ = self.wave.buckets(sigs, ts, level, TILE_BUCKETS)
        out = {}
        for sig in sigs:
            v0, bks = summ[sig.code]
            out_t, out_v, dense = [], [], []
            last_db = last_q = None
            for b, first, last_t, cnt, ones, last_v in bks:
                if cnt == 1:
                    out_t.append(first)
                    out_v.append(last_v)
                    last_db = None
                    continue
                hi = ones / cnt if sig.width == 1 else 0
                q = round(hi * 8) if sig.width == 1 else cnt.bit_length()
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
            dt, prev_t = [], ts
            for t in out_t:
                dt.append(t - prev_t)
                prev_t = t
            out[sig.code] = {
                "v0": self.enc(v0, sig.width),
                "t": dt,
                "v": [self.enc(v, sig.width) for v in out_v],
                "d": [[a - ts, b - ts, n, round(h, 2)] for a, b, n, h in dense],
                "x": 1 if not dense else 0,
            }
        return out

    def tiles_for(self, level, want):
        """Requested tiles, from the cache or made in one source pass per
        slot for all the signals that miss."""
        by_k = {}
        out = {}
        for name, ks in want.items():
            sig = self.wave.by_name.get(name)
            if sig is None:
                continue
            out[name] = {}
            for k in ks[:64]:
                key = self.tile_key(sig, level, int(k))
                hit = self.tiles.get(key)
                if hit is not None:
                    self.tiles.move_to_end(key)
                    out[name][str(k)] = hit
                else:
                    by_k.setdefault(int(k), []).append(sig)
        for k, sigs in by_k.items():
            uniq = list({s.code: s for s in sigs}.values())
            made = self.make_tiles(uniq, level, k)
            for sig in sigs:
                t = made[sig.code]
                self.tiles[self.tile_key(sig, level, k)] = t
                out[sig.name][str(k)] = t
        while len(self.tiles) > TILE_CACHE:
            self.tiles.popitem(last=False)
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
        self.state["cursors"] = []
        self.state["arrows"] = []
        self.state["comments"] = []
        self.state["note"] = None

    def op_rc(self, a):
        """Apply .wvrc text: a whole file, or single lines with merge."""
        warnings = self.load_wvrc(a.get("text", ""), merge=bool(a.get("merge")),
                                  base_dir=a.get("base_dir") or ".", origin=a.get("origin") or "rc")
        self._fix_state()
        return {"warnings": warnings}

    def op_unmark(self, a):
        """Remove one named cursor (name), arrow or comment (kind, index)."""
        kind = a.get("kind", "cursors")
        if kind not in ("cursors", "arrows", "comments", "annotations"):
            raise wq.QueryError("unknown kind %r" % kind)
        lst = self.state[kind]
        if a.get("all"):
            lst.clear()
        elif kind == "cursors" and a.get("name") is not None:
            lst[:] = [c for c in lst if c["name"] != a["name"]]
        elif a.get("index") is not None and 0 <= int(a["index"]) < len(lst):
            del lst[int(a["index"])]

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
                lo = max(0, hi - (1 << 18 if getattr(self.wave, "lazy", False) else 4096))
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

    # -- source (RTL) ---------------------------------------------------------

    def build_index(self):
        specs = self.rtl_specs or ["."]
        self.src_status = "indexing"
        t0 = time.time()
        try:
            if specs and remote.is_remote(specs[0]):
                self._build_remote_index(specs, t0)
                return
            if len(specs) == 1 and rtlindex.is_elaborated(specs[0]):
                # an elaborated design: Verilator --xml-only, design JSON from
                # any exporter, or a Verdi KDB / daidir (exported through NPI)
                idx = rtlindex.load_any(specs)
                files = [specs[0]] + [idx.abs(f) for f in idx.files]
            else:
                files = rtlindex.collect_sources(specs)
                if specs == ["."]:
                    kept = rtlindex.Sources(f for f in files if "/vendor/" not in f and "/third_party/" not in f
                                            and "/obj_dir" not in f)
                    kept.incdirs = files.incdirs
                    files = kept
                total = len(files)

                def progress(done, path):
                    self.src_status = "indexing %d/%d files, %.0f s" % (done, total, time.time() - t0)

                idx = rtlindex.Index(files, progress=progress)
                files = list(files) + [f for f in idx.files if f not in files]  # `include files are watched too
        except Exception as exc:  # noqa: BLE001
            self.src_status = "index failed: %s" % exc
            sys.stderr.write("waveview: %s\n" % self.src_status)
            return
        mt = {}
        for f in files:
            try:
                mt[f] = os.stat(f).st_mtime_ns
            except OSError:
                pass
        with self.lock:
            idx.place_roots([s.name for s in self.wave.signals])
            self.src = idx
            self.src_mtimes = mt
            self.src_status = "%s: %d modules from %d files (%.1fs)" % (
                idx.producer, len(idx.modules), len(files), time.time() - t0)
        sys.stderr.write("waveview: source index: %s\n" % self.src_status)

    def _build_remote_index(self, specs, t0):
        """Index RTL on the server (rtlindex.py runs there over ssh) and fetch
        the files the source pane opens, once each."""
        parsed = [remote.parse(s) for s in specs]
        if any(p is None or p[0].host != parsed[0][0].host for p in parsed):
            raise wq.QueryError("--rtl over ssh: all entries must be on the same host")
        r = parsed[0][0]
        paths = [p for _, p in parsed]
        root = self.rtl_root or __import__("posixpath").dirname(paths[0])
        self.src_status = "indexing on %s" % r.host
        doc = r.index_rtl(paths, root)
        idx = rtlindex.Index.from_doc(doc)
        rroot = doc.get("root") or root
        cache = {}

        def reader(rel, _cache=cache):
            full = rel if rel.startswith("/") else rroot.rstrip("/") + "/" + rel
            if full not in _cache:
                got = r.read_files([full])
                if full not in got:
                    raise wq.QueryError("cannot read %s on %s" % (full, r.host))
                _cache[full] = got[full]
            return _cache[full]

        idx.reader = reader
        with self.lock:
            idx.place_roots([s.name for s in self.wave.signals])
            self.src = idx
            self.src_mtimes = {}
            self.src_status = "%s on %s: %d modules (%.1fs)" % (idx.producer, r.host, len(idx.modules),
                                                               time.time() - t0)
        sys.stderr.write("waveview: source index: %s\n" % self.src_status)

    def _stamp(self):
        stamp = getattr(self.wave, "stamp", None)
        if stamp is not None:
            return stamp()
        st = os.stat(self.dump_path)
        return (st.st_size, st.st_mtime_ns)

    def index(self):
        """The source index, rebuilt when a source file changed."""
        if self.src is None:
            st = self.src_status
            if st.startswith("indexing"):
                raise wq.QueryError("the source index is still being built (%s); try again in a moment" % st)
            if st.startswith("index failed"):
                raise wq.QueryError("the source index could not be built: %s" % st[len("index failed"):].strip(": "))
            raise wq.QueryError("no source index (%s). Start the viewer with --rtl <filelist.f or dir>, "
                                "or choose the RTL in Open…" % st)
        now = time.time()
        if now - self.src_checked > 2:
            self.src_checked = now
            for f, m in self.src_mtimes.items():
                try:
                    if os.stat(f).st_mtime_ns != m:
                        self.build_index()
                        break
                except OSError:
                    continue
        return self.src

    def dump_names(self, scope, mod=None):
        """Dump signals directly in `scope`, grouped by source identifier:
        'w_x' -> ['scope.w_x'], arrays 'w_a' -> ['scope.w_a[0]', ...],
        structs 'w_s' -> ['scope.w_s.f1', ...]."""
        pre = scope + "."
        out = {}
        for s in self.wave.signals:
            n = s.name
            if not n.startswith(pre):
                continue
            rest = n[len(pre):]
            comps = rest.split(".")
            # signals declared inside generate blocks: gen_x[0].w_y -> w_y
            if mod is not None and len(comps) > 1 and rtlindex.Index._strip(comps[0]) in mod.gen_labels:
                base = re.split(r"[.\[(]", comps[1], maxsplit=1)[0]
                if base not in mod.instances:
                    out.setdefault(base, []).append(n)
                continue
            base = re.split(r"[.\[(]", rest, maxsplit=1)[0]
            out.setdefault(base, []).append(n)
        return out

    def child_scopes(self, scope, mod):
        """instance name -> dump scopes of it (through generate scopes)."""
        pre = scope + "."
        found = {}
        for s in self.wave.signals:
            if not s.name.startswith(pre):
                continue
            comps = s.name[len(pre):].split(".")[:-1]
            path = scope
            for c in comps:
                path += "." + c
                base = rtlindex.Index._strip(c)
                if base in mod.instances:
                    found.setdefault(base, [])
                    if path not in found[base]:
                        found[base].append(path)
                    break
                if base not in mod.gen_labels:
                    break
        return found

    def src_view(self, scope, file=None):
        """The source of a scope's module; `file` picks one of its `include
        files instead of the module's own."""
        idx = self.index()
        mod, rest = idx.resolve_scope(scope)
        if mod is None:
            raise wq.QueryError("scope %s is not in the source index" % scope)
        try:
            v = idx.module_view(mod, file or None)
        except ValueError as exc:
            raise wq.QueryError(str(exc))
        v["scope"] = scope
        names = self.dump_names(scope, mod)
        # a generate scope (gen_x[0]) also sees the signals of its module
        comps = scope.split(".")
        while len(comps) > 1 and rtlindex.Index._strip(comps[-1]) in mod.gen_labels:
            comps.pop()
        if len(comps) < len(scope.split(".")):
            for k, ns in self.dump_names(".".join(comps), mod).items():
                names.setdefault(k, ns)
        v["signals"] = {k: names[k][:64] for k in {i[2] for i in v["idents"] if i[3] in ("signal", "param")} if k in names}
        v["children"] = self.child_scopes(scope, mod)
        up = idx.parent_scope(scope)
        v["parent"] = up[0] if up else None
        return v

    def diagram(self, scope):
        """A block diagram of a scope: its child instances as boxes with
        their size (signals, instances, lines) and interface bundles, the
        scope's own ports as bundles, and, with a source index, the
        connections between the boxes grouped by bundle with the protocol
        told from the names (ifaces.py)."""
        scope = (scope or "").strip(".")
        pre = scope + "." if scope else ""
        kids = {}  # child -> {"signals", "sub", "own"}
        own = []   # (leaf, width, direction) of the signals directly in scope
        widths = {}
        for s in self.wave.signals:
            if not s.name.startswith(pre):
                continue
            rest = s.name[len(pre):]
            comps = rest.split(".")
            widths[rest] = s.width
            if len(comps) == 1:
                own.append((comps[0], s.width, s.direction))
                continue
            d = kids.setdefault(comps[0], {"signals": 0, "sub": set(), "own": []})
            d["signals"] += 1
            if len(comps) > 2:
                d["sub"].add(comps[1])
            else:
                d["own"].append((comps[1], s.width, s.direction))
        idx = self.src
        mod = None
        if idx is not None and scope:
            mod, rest = idx.resolve_scope(scope)
            if rest:
                mod = None
        boxes = {}

        def box(name):
            if name in boxes:
                return boxes[name]
            d = kids.get(name)
            b = {"name": name, "scope": pre + name, "module": None, "signals": d["signals"] if d else 0,
                 "insts": len(d["sub"]) if d else 0, "loc": None, "in_dump": d is not None, "bundles": [],
                 "ports": {}, "iface": False}
            if idx is not None:
                cm, crest = idx.resolve_scope(pre + name)
                if cm is not None and not crest:
                    b["module"] = cm.orig
                    b["loc"] = cm.end_line - cm.line + 1
                    b["insts"] = max(b["insts"], len(cm.instances))
                    if cm.kind == "interface":
                        # an interface instance: what it carries, not ports
                        b["iface"] = True
                        b["ports"] = {n: "" for n in cm.decls if n not in cm.params}
                    else:
                        b["ports"] = {p: cm.port_dir.get(p, "inout") for p in cm.ports if p not in cm.port_iface}
                        for p, (iface, modport) in cm.port_iface.items():
                            b["ports"][p] = idx.iface_port_dir(iface, modport) or "inout"
            boxes[name] = b
            return b

        for name in kids:
            box(name)
        if mod is not None:
            for name in mod.instances:
                box(name)
        # each box's interfaces: its ports (index) or its port-like dump signals
        for b in boxes.values():
            d = kids.get(b["name"])
            extra = []
            if b["ports"]:
                cm = idx.resolve_scope(b["scope"])[0] if idx is not None else None
                ifp = cm.port_iface if cm is not None else {}
                plist = [(p, widths.get(b["name"] + "." + p, 1), dr) for p, dr in b["ports"].items() if p not in ifp]
                for p, (iface, modport) in ifp.items():
                    # the interface instance it is wired to, if the dump has one, gives the widths
                    ibox = next((n for n, x in boxes.items() if x["iface"] and x["module"] == iface), None)
                    bd = self._iface_bundle(idx, iface, modport, p, widths, ibox or "")
                    if bd:
                        extra.append(bd)
            else:
                plist = [(n, w, dr) for n, w, dr in (d["own"] if d else []) if dr] or (d["own"] if d else [])
            b["bundles"] = extra + ifaces.bundles(plist)
            b.pop("ports")
        # the scope's own ports; an interface port is one bundle, with the
        # protocol of the interface's signals and the modport's view of it
        if mod is not None:
            plist = [(p, widths.get(p, 1), mod.port_dir.get(p, "inout")) for p in mod.ports if p not in mod.port_iface]
        else:
            plist = [(n, w, dr) for n, w, dr in own if dr]
        ports = ifaces.bundles(plist)
        if mod is not None:
            for p, (iface, modport) in mod.port_iface.items():
                bd = self._iface_bundle(idx, iface, modport, p, widths, p)
                if bd:
                    ports.insert(0, bd)
        # connections: nets of the module, who drives and who reads them
        edges = {}
        if mod is not None:
            bundle_of = {}  # (box, port) -> bundle
            for b in boxes.values():
                for bd in b["bundles"]:
                    for sname in bd["signals"]:
                        bundle_of[(b["name"], sname)] = bd
            for bd in ports:
                for sname in bd["signals"]:
                    bundle_of[("", sname)] = bd
            nets = {}
            iface_links = []  # (instance, port, interface instance or "" for the scope's port, iface, modport)
            for p in mod.ports:
                if p in mod.port_iface:
                    continue
                # inside the module an input port is a source, an output a sink
                flipped = {"input": "output", "output": "input"}.get(mod.port_dir.get(p, "inout"), "inout")
                nets.setdefault(p, []).append(("", p, flipped))
            for iname, inst in mod.instances.items():
                im = idx.modules.get(inst.mtype)
                for k, (port, toks, _ln, _col) in enumerate(inst.conns):
                    pname = port if port is not None else (im.ports[k] if im and k < len(im.ports) else None)
                    if pname is None:
                        continue
                    net = net_key(toks)
                    if net is None:
                        continue
                    if im is not None and pname in im.port_iface:
                        iface, modport = im.port_iface[pname]
                        if net in boxes and boxes[net]["iface"]:
                            iface_links.append((iname, pname, net, iface, modport))
                        elif net in mod.port_iface:
                            iface_links.append((iname, pname, "", iface, modport))
                        continue
                    pdir = im.port_dir.get(pname, "inout") if im else "inout"
                    nets.setdefault(net, []).append((iname, pname, pdir))
            for iname, pname, ibox, iface, modport in iface_links:
                # the whole interface as one edge, the modport telling which way
                d = idx.iface_port_dir(iface, modport)
                bd = self._iface_bundle(idx, iface, modport, pname, widths, ibox or ibox) or {}
                src, dst = (ibox, iname) if d == "input" else (iname, ibox)
                key = (src, dst, pname)
                edges[key] = {"from": src, "to": dst, "bundle": pname, "protocol": bd.get("protocol"),
                              "role": bd.get("role"), "width": bd.get("width", 0), "iface": iface,
                              "modport": modport, "nets": [[pname, ibox or pname, modport or ""]]}
            # an instance on the whole array (bus) also meets the ones on
            # its elements (bus[0], bus[1]); the elements stay apart
            whole = {k: v for k, v in nets.items() if "[" not in k}
            for net, ends in nets.items():
                base = net.split("[", 1)[0]
                if "[" in net and base in whole:
                    ends = ends + whole[base]
                drivers = [e for e in ends if e[2] == "output"]
                sinks = [e for e in ends if e[2] == "input"]
                if not drivers:
                    # inout or unknown direction: a sibling-to-sibling line
                    # would be a guess, so only the scope's port (if the net
                    # is one) is tied to the instances on it
                    port_end = [e for e in ends if e[0] == ""]
                    if not port_end:
                        continue
                    drivers, sinks = port_end[:1], [e for e in ends if e[0] != ""]
                for di, dp, _ in drivers:
                    for si, sp, _ in sinks:
                        if di == si:
                            continue
                        bd = bundle_of.get((di, dp))
                        key = (di, si, bd["name"] if bd else dp)
                        e = edges.setdefault(key, {"from": di, "to": si, "bundle": bd["name"] if bd else dp,
                                                   "protocol": bd["protocol"] if bd else None,
                                                   "role": bd["role"] if bd else None, "width": 0, "nets": []})
                        e["width"] += widths.get(net, widths.get(di + "." + dp, 1))
                        e["nets"].append([dp, net, sp])
        # names given by a person or an agent (op_label), on bundles and their edges
        labels = self.state.get("iflabels", {}).get(scope, {})
        for b in boxes.values():
            for bd in b["bundles"]:
                bd["label"] = (labels.get(b["name"] + "/" + bd["name"]) or {}).get("text")
        for bd in ports:
            bd["label"] = (labels.get("/" + bd["name"]) or {}).get("text")
        for e in edges.values():
            e["label"] = (labels.get(e["from"] + "/" + e["bundle"]) or {}).get("text")
        return {"scope": scope, "module": mod.orig if mod else None, "index": idx is not None,
                "boxes": sorted(boxes.values(), key=lambda b: (-b["signals"], b["name"])),
                "ports": ports, "edges": list(edges.values()),
                "unlabelled": [(b["name"] + "/" + bd["name"]) for b in boxes.values() for bd in b["bundles"]
                               if bd["protocol"] is None and not bd["label"]]
                + ["/" + bd["name"] for bd in ports if bd["protocol"] is None and not bd["label"]]}

    # -- the whole hierarchy by size (treemap) -------------------------------

    def scope_tree(self):
        """Every scope of the dump with the number of signals below it,
        built once per dump generation. A dump whose names live on disk
        (nameindex) uses the per-scope counts the server summarised."""
        cached = getattr(self, "_scope_tree", None)
        if cached is not None and cached[0] == self.gen:
            return cached[1]
        root = {"name": "", "scope": "", "signals": 0, "kids": {}}

        def node_for(comps):
            node = root
            node["signals"] += 1
            path = ""
            for c in comps:
                path = path + "." + c if path else c
                node = node["kids"].setdefault(c, {"name": c, "scope": path, "signals": 0, "kids": {}})
                node["signals"] += 1
            return node

        names = getattr(self.wave, "names", None)
        counts = getattr(names, "meta", {}).get("scopes") if names is not None else None
        if counts:
            for scope, n in counts.items():
                node = node_for(scope.split("."))
                node["signals"] += n - 1
                for anc in self._ancestors(root, scope.split(".")):
                    anc["signals"] += n - 1
        else:
            for s in self.wave.signals:
                node_for(s.name.split(".")[:-1])
        self._scope_tree = (self.gen, root)
        return root

    @staticmethod
    def _ancestors(root, comps):
        out = [root]
        node = root
        for c in comps[:-1]:
            node = node["kids"].get(c)
            if node is None:
                break
            out.append(node)
        return out

    def tree(self, scope, depth=3, limit=40):
        """The subtree below `scope` for a treemap: each node with its
        signals, descendant scopes, module and lines (from the index), its
        children cut to the `limit` largest plus a remainder."""
        root = self.scope_tree()
        node = root
        for c in (scope or "").strip(".").split("."):
            if not c:
                continue
            node = node["kids"].get(c)
            if node is None:
                raise wq.QueryError("no scope %s in the dump" % scope)
        idx = self.src

        def count_scopes(n):
            return sum(1 + count_scopes(k) for k in n["kids"].values())

        def out(n, d):
            o = {"name": n["name"], "scope": n["scope"], "signals": n["signals"], "scopes": count_scopes(n),
                 "module": None, "loc": None, "kids": []}
            if idx is not None and n["scope"]:
                m, rest = idx.resolve_scope(n["scope"])
                if m is not None and not rest:
                    o["module"] = m.orig
                    o["loc"] = m.end_line - m.line + 1
                    o["iface"] = m.kind == "interface"
            if d > 0 and n["kids"]:
                kids = sorted(n["kids"].values(), key=lambda k: -k["signals"])
                o["kids"] = [out(k, d - 1) for k in kids[:limit]]
                rest = kids[limit:]
                if rest:
                    o["kids"].append({"name": "+%d more" % len(rest), "scope": None, "signals": sum(k["signals"] for k in rest),
                                      "scopes": sum(1 + count_scopes(k) for k in rest), "module": None, "loc": None,
                                      "kids": []})
            return o

        return out(node, depth)

    def _iface_bundle(self, idx, iface, modport, port, widths, ibox):
        """An interface port as one bundle: the protocol from the interface's
        signal names, the role from the modport's directions, the width from
        the dump's copy of the interface instance (ibox) when it has one."""
        im = idx.modules.get(iface)
        if im is None:
            return None
        sigs = [n for n in im.decls if n not in im.params]
        dirs = im.modports.get(modport or "", {})
        plist = [(n, widths.get((ibox + "." if ibox else "") + n, 1), dirs.get(n, "")) for n in sigs]
        found = [b for b in ifaces.bundles(plist) if b["protocol"] not in (None, "clock", "reset")]
        main = found[0] if found else None
        return {"name": port, "protocol": main["protocol"] if main else None, "role": main["role"] if main else None,
                "dir": {"output": "out", "input": "in"}.get(idx.iface_port_dir(iface, modport) or "", None),
                "width": sum(w for _, w, _ in plist), "signals": [port], "iface": iface, "modport": modport,
                "carries": sigs}

    def src_where(self, scope):
        """For a node of the hierarchy: the module that implements it (file
        and line) and, when it is an instance, where the parent module
        instantiates it."""
        idx = self.index()
        mod, rest = idx.resolve_scope(scope)
        if mod is None or rest:
            raise wq.QueryError("scope %s is not in the source index" % scope)
        out = {"scope": scope, "module": mod.orig, "file": mod.file, "line": mod.line}
        up = idx.parent_scope(scope)
        if up:
            parent, inst = up
            pm, _ = idx.resolve_scope(parent)
            i = pm.instances.get(inst) if pm is not None else None
            if i is not None:
                out.update(parent=parent, parent_module=pm.orig, instance=inst, inst_file=i.file, inst_line=i.line)
        return out

    def src_signal(self, full):
        """Where a dump signal comes from: scope, module, declaration and the
        drivers, following the hierarchy."""
        idx = self.index()
        scope, mod, name = idx.split_signal(full)
        if mod is None:
            raise wq.QueryError("%s is outside the RTL given to the viewer (%d modules). Add the file that "
                                "defines its module, such as the testbench, or give the RTL folder"
                                % (full, len(idx.modules)))
        d = idx.describe(mod, name)
        d.update(scope=scope, signal=name, drivers=idx.trace(scope, name), loads=idx.trace(scope, name, loads=True))
        return d

    def src_trace(self, scope, name, loads):
        return self.index().trace(scope, name, loads=loads)

    def op_source(self, a):
        """Show a place in the source pane: a signal's driver, or scope+line."""
        if a.get("signal"):
            full = a["signal"]
            if full not in self.wave.by_name:
                full = self.wave.resolve(full).name
            info = self.src_signal(full)
            target = (info["loads"] if a.get("loads") else info["drivers"]) or ([info["decl"]] if info["decl"] else [])
            first = target[0] if target else {"line": None}
            self.state["source"] = {"scope": first.get("scope", info["scope"]), "line": first.get("line"),
                                    "file": first.get("file"),
                                    "signal": info["signal"], "kind": "loads" if a.get("loads") else "drivers",
                                    "full": full, "at": time.time()}
        else:
            self.state["source"] = {"scope": a["scope"], "line": a.get("line"), "signal": a.get("name"),
                                    "file": a.get("file"), "kind": a.get("kind"), "at": time.time()}
        return self.state["source"]

    def op_label(self, a):
        """Name an interface bundle of the diagram: scope, bundle as
        "box/bundle" ("/bundle" for the scope's own ports), text (none or
        clear: remove). What the names did not tell, a person or an agent
        says here; kept with the view."""
        scope = (a.get("scope") or "").strip(".")
        key = a.get("bundle") or ""
        if "/" not in key:
            raise wq.QueryError("bundle must be box/bundle, e.g. u_cpu/dbg or /apb for the scope's ports")
        labels = self.state.setdefault("iflabels", {})
        text = (a.get("text") or "").strip()
        if a.get("clear") or not text:
            if scope in labels:
                labels[scope].pop(key, None)
                if not labels[scope]:
                    del labels[scope]
            return {"cleared": key}
        labels.setdefault(scope, {})[key] = {"text": text, "source": a.get("source", "agent"), "at": time.time()}
        return {"labelled": key}

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
        one change per pixel is sent. Merged stretches come back as dense.
        (The UI uses tiles; this stays for scripts.)"""
        w = self.wave
        px = max(16, min(int(px), 20000))
        bw = max((t1 - t0) / px, 1e-9)
        out = {}
        for n in names:
            sig = w.by_name.get(n)
            if sig is None:
                continue
            v0 = w.value(sig, t0)
            ch, dense = [], []
            last_b = None
            prev = v0
            for t, v in w.changes(sig, t0, t1):
                if t <= t0 or v == prev:
                    continue
                prev = v
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
        e = self.edges_in(t0, t1)
        return {"t0": t0, "t1": t1, "signals": out, "edges": e["edges"], "edge0": e["edge0"], "gen": self.gen}

    def values_at(self, names, t):
        w = self.wave
        tl = self.timeline()
        return {
            "time": t,
            "cycle": tl.cycle_of(t) if tl.edges else None,
            "values": w.values_at([w.by_name[n] for n in names if n in w.by_name], t),
        }

    def next_edge(self, name, t, direction):
        """Time of the next / previous real value change of a signal."""
        return self.wave.next_change(self.wave.resolve(name), t, direction)

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
            "cursors": [dict(c, cycle=cyc(c["time"])) for c in st.get("cursors", [])],
            "arrows": [dict(a, **{"from": dict(a["from"], cycle=cyc(a["from"]["time"])),
                                  "to": dict(a["to"], cycle=cyc(a["to"]["time"]))}) for a in st.get("arrows", [])],
            "comments": [dict(c, cycle=cyc(c["time"])) for c in st.get("comments", [])],
            "source": st.get("source"),
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
        while not self.retired:
            time.sleep(1.0)
            if self.retired:
                return
            try:
                with self.lock:
                    sig = self._stamp()
            except (OSError, wq.QueryError):
                continue
            if sig == self.mtime:
                last_seen = None
                continue
            if sig != last_seen:  # still being written; wait until it settles
                last_seen = sig
                continue
            try:
                wave = wq.load(self.dump_path, scope=self.scope, index=False)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write("waveview: reload failed, will retry: %s\n" % exc)
                last_seen = None
                continue
            with self.lock:
                if self.retired:
                    wave.close()
                    return
                old, self.wave = self.wave, wave
                old.close()
                if self.src is not None:
                    self.src.place_roots([s.name for s in wave.signals])
                self.mtime = sig
                self.gen += 1
                self._fix_state()
                self.version += 1
                self.state["last"] = {"op": "reload", "source": "file", "client": None, "at": time.time()}
                self.broadcast("reload", self.meta())
                self.broadcast("state", self.public_state())
            sys.stderr.write("waveview: reloaded %s (gen %d)\n" % (self.dump_path, self.gen))

    def retire(self):
        """Another dump was opened in this server: save, tell the browsers
        to load the new one, and let go of this one."""
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save()
        with self.lock:
            self.retired = True
            self.broadcast("reopen", {})
            try:
                self.wave.close()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass

    def export_gtkw(self):
        lines = [
            "[*] written by waveview",
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
    hub: Hub = None  # None until a dump is opened from the window
    token: str = None
    on_open = None  # called after another dump became the current one
    opening = threading.Lock()
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
            if path == "/api/session":
                return self._send(200, {"dump": hub.dump_path if hub else None})
            if path == "/api/hosts":
                return self._send(200, {"hosts": opener.ssh_hosts(), "local": opener.local_name()})
            if method == "POST" and path == "/api/ls":
                return self._list_dir(self._body())
            if method == "POST" and path == "/api/open":
                return self._open_dump(self._body())
            if hub is None:
                return self._send(409, {"error": "no dump is open yet"})
            if path == "/api/events":
                return self._events()
            if path == "/api/meta":
                return self._send(200, hub.meta())
            if path == "/api/state":
                with hub.lock:
                    return self._send(200, hub.public_state())
            if path == "/api/signals":
                with hub.lock:
                    return self._send(200, [[s.name, s.width, s.kind] for s in hub.wave.signals])
            if path == "/api/snapshot":
                table = query.get("table", ["0"])[0] == "1"
                with hub.lock:
                    return self._send(200, hub.snapshot(table=table, radix=query.get("radix", ["hex"])[0]))
            if path == "/api/export.rc":
                with hub.lock:
                    return self._send(200, hub.export_rc(), "text/plain",
                                      {"Content-Disposition": 'attachment; filename="waveview.rc"'})
            if path == "/api/export.wvrc":
                with hub.lock:
                    return self._send(200, hub.export_wvrc(), "text/plain",
                                      {"Content-Disposition": 'attachment; filename="waveview.wvrc"'})
            if path == "/api/export.gtkw":
                with hub.lock:
                    return self._send(200, hub.export_gtkw(), "text/plain",
                                      {"Content-Disposition": 'attachment; filename="waveview.gtkw"'})
            if path == "/api/src/view":
                with hub.lock:
                    return self._send(200, hub.src_view(query["scope"][0], query.get("file", [None])[0]))
            if path == "/api/src/signal":
                with hub.lock:
                    return self._send(200, hub.src_signal(query["name"][0]))
            if path == "/api/src/where":
                with hub.lock:
                    return self._send(200, hub.src_where(query["scope"][0]))
            if path == "/api/diagram":
                with hub.lock:
                    return self._send(200, hub.diagram(query.get("scope", [""])[0]))
            if path == "/api/tree":
                with hub.lock:
                    return self._send(200, hub.tree(query.get("scope", [""])[0],
                                                    int(query.get("depth", ["3"])[0]),
                                                    int(query.get("limit", ["40"])[0])))
            if path == "/api/src/status":
                return self._send(200, {"status": hub.src_status, "ready": hub.src is not None})
            if path == "/api/src/trace":
                with hub.lock:
                    return self._send(200, hub.src_trace(query["scope"][0], query["name"][0],
                                                         query.get("loads", ["0"])[0] == "1"))
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
        except (wq.QueryError, remote.RemoteError) as exc:
            return self._send(400, {"error": str(exc)})
        except (KeyError, ValueError, TypeError) as exc:
            return self._send(400, {"error": "bad request: %r" % exc})
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _list_dir(self, body):
        host, path, kind = body.get("host") or "", body.get("path") or "", body.get("kind") or "dumps"
        if kind not in opener.KINDS:
            raise wq.QueryError("no such listing kind: %r" % (kind,))
        try:
            return self._send(200, opener.list_remote(host, path, kind) if host else opener.list_local(path, kind))
        except OSError as exc:
            raise wq.QueryError("cannot list %s: %s" % (path or "the folder", exc.strerror or exc))

    def _open_dump(self, body):
        """Load the dump first: if that fails, the current view stays."""
        host, path, rtl = body.get("host") or "", body["path"], (body.get("rtl") or "").strip()
        if host:
            r = opener.remote_for(host)
            if not path.startswith("/") or (rtl and not rtl.startswith("/")):
                raise wq.QueryError("paths on the server must be absolute")
            base = "ssh://%s%s" % (r.host, ":%s" % r.port if r.port else "")
            path, rtl = base + path, base + rtl if rtl else ""
        if not Handler.opening.acquire(blocking=False):
            return self._send(409, {"error": "another dump is being opened"})
        try:
            try:
                new = Hub(path, rtl=[rtl] if rtl else None)
            except (OSError, subprocess.SubprocessError) as exc:
                raise wq.QueryError("cannot open %s: %s" % (path, exc))
            old, Handler.hub = Handler.hub, new
            threading.Thread(target=new.watch, daemon=True).start()
            if Handler.on_open:
                Handler.on_open()
            if old is not None:
                old.retire()
        finally:
            Handler.opening.release()
        return self._send(200, {"dump": new.dump_path})

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

    def handle_error(self, request, client_address):
        # a browser that went away is normal (on Windows it shows as a reset)
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def cmd_serve(args):
    if args.rtl_root:
        os.environ["WAVEVIEW_RTL_ROOT"] = args.rtl_root
    if args.dump is None and args.rc:
        with open(args.rc, errors="replace") as fh:
            args.dump = waverc.dump_of(fh.read(), os.path.dirname(os.path.abspath(args.rc)))
        if args.dump is None:
            raise wq.QueryError("%s has no 'dump' line; give the dump too: serve DUMP --rc %s" % (args.rc, args.rc))
    if args.window or not args.dump:
        remote.Remote.batch = True  # ssh must not wait for a prompt nobody sees
    hub = None
    if args.dump:
        hub = Hub(args.dump, gtkw=args.gtkw, rc=args.rc, rtl=args.rtl or (["."] if not args.no_rtl else None),
                  scope=args.scope)
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
    started = time.time()

    def register():
        current = Handler.hub
        with open(os.open(reg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump({"url": url, "token": token, "dump": current.dump_path if current else "", "pid": os.getpid(),
                       "started": started, "host": args.host, "port": port}, fh)

    register()
    Handler.on_open = register

    def cleanup(*_):
        if Handler.hub is not None:
            Handler.hub._save()
        try:
            os.remove(reg)
        except OSError:
            pass
        os._exit(0)

    signal_mod.signal(signal_mod.SIGTERM, cleanup)
    signal_mod.signal(signal_mod.SIGINT, cleanup)
    if hub is not None:
        threading.Thread(target=hub.watch, daemon=True).start()
    hostname = platform.node() or "this-host"
    print("waveview: %s" % ("serving " + hub.dump_path if hub else "no dump yet, choose one in the window"))
    print("  open      %s?token=%s" % (url, token))
    if hub is not None and remote.is_remote(hub.dump_path):
        print("  data      read over ssh from %s, only what the view asks for" % remote.parse(hub.dump_path)[0].host)
    else:
        print("  remote    ssh -L %d:localhost:%d %s   (VS Code Remote forwards it by itself)" % (port, port, hostname))
    print("  control   waveview.py add '<pattern>' | goto --cycle N | look | note TEXT")
    sys.stdout.flush()
    link = url + "?token=" + token
    if args.window:
        threading.Thread(target=run_window, args=(link, port, cleanup), daemon=True).start()
    elif args.open:
        import webbrowser

        webbrowser.open(link)
    httpd.serve_forever()
    return 0


def run_window(link, port, on_close):
    """The viewer in its own window; closing the window ends the server. If
    there is no such browser, use the default one and keep the console; the
    macOS app has no console, so a dialog stands in for the window there."""
    import webbrowser

    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    proc = opener.open_window(link, os.path.join(base, "waveview", "window", str(port)))
    if proc is None:
        webbrowser.open(link)
        if opener.MAC and getattr(sys, "frozen", False) and (sys.stdin is None or not sys.stdin.isatty()):
            opener.wait_for_quit(link)
            on_close()
        return
    opened = time.time()
    opener.hide_console_if_ours()
    proc.wait()
    if time.time() - opened < 3:  # handed over to another process: not ours to follow
        opener.show_console()
        return
    on_close()


# ---------------------------------------------------------------------------
# Command line client
# ---------------------------------------------------------------------------


def _alive(pid):
    if os.name == "nt":
        # os.kill(pid, 0) would terminate the process on Windows
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
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
                want = dump if remote.is_remote(dump) else os.path.abspath(dump)
                ss = [s for s in ss if s["dump"] == want]
            if not ss:
                raise wq.QueryError(
                    "no running waveview found%s. Start one with: waveview.py serve <dump>"
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

    def at(p):
        return "%s%s" % (_t(p["time"]), "" if p.get("cycle") is None else " (cycle %d)" % p["cycle"])

    def on(p):
        return (p["signal"] or "time axis") + " @ " + at(p)

    for c in snap.get("cursors", []):
        print("cursor %s  %s%s" % (c["name"], at(c), "  " + c["label"] if c.get("label") else ""))
    for i, a in enumerate(snap.get("arrows", [])):
        print("arrow #%d  %s -> %s%s" % (i, on(a["from"]), on(a["to"]), "  \"%s\"" % a["label"] if a.get("label") else ""))
    for i, c in enumerate(snap.get("comments", [])):
        print("comment #%d  %s  \"%s\"" % (i, on(c), c["text"].replace("\n", " / ")))
    if snap["selected"]:
        print("selected %s" % ", ".join(snap["selected"]))
    src = snap.get("source")
    if src and src.get("scope"):
        print("source  %s line %s%s" % (src["scope"], src.get("line"), "  (%s)" % src["signal"] if src.get("signal") else ""))
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


def net_key(toks):
    """The net a port connection names: the first identifier, with a
    constant index or range kept (bus[1], bus[7:4]) so that the elements
    of an array are different nets; a variable index (bus[i]) is the whole
    array. None when the expression names no net (a constant)."""
    base = None
    k = 0
    while k < len(toks):
        t = toks[k]
        if t.kind == "id":
            base = t.text
            k += 1
            break
        k += 1
    if base is None:
        return None
    key = base
    while k < len(toks) and toks[k].kind == "op" and toks[k].text == "[":
        depth, j, inner = 1, k + 1, []
        while j < len(toks) and depth:
            if toks[j].kind == "op" and toks[j].text in "[]":
                depth += 1 if toks[j].text == "[" else -1
                if depth == 0:
                    break
            inner.append(toks[j])
            j += 1
        if inner and all(t.kind in ("num", "op") for t in inner) and any(t.kind == "num" for t in inner):
            key += "[" + "".join(t.text for t in inner) + "]"
        else:
            break
        k = j + 1
    return key


def print_tree(t):
    """The hierarchy by size as an indented list: signals below each scope,
    scopes below it, the module and its lines."""
    total = max(1, t["signals"])

    def line(n, depth):
        size = "%7d sig %5.1f%%  %4d scopes" % (n["signals"], 100.0 * n["signals"] / total, n["scopes"])
        mod = ("  (%s%s%s)" % (n["module"], ", %d lines" % n["loc"] if n["loc"] else "", ", interface" if n.get("iface") else "")
               if n["module"] else "")
        print("%s %s%s%s" % (size, "  " * depth, n["name"] or "(top)", mod))
        for k in n["kids"]:
            line(k, depth + 1)

    line(t, 0)


def print_diagram(d):
    """The block diagram as text, for a terminal or an agent."""

    def bundle_text(b):
        tag = b["protocol"] or "?"
        if b["role"]:
            tag += " " + b["role"]
        if b.get("label"):
            tag = '"%s"' % b["label"] + (", " + tag if b["protocol"] else "")
        name = b["name"] if not (b["protocol"] and b["name"].lower() == b["protocol"].lower()) else ""
        return "%s[%s, %d bits, %d signals%s]" % (name + " " if name else "", tag, b["width"], len(b["signals"]),
                                                  ", " + b["dir"] if b["dir"] else "")

    print("scope   %s%s" % (d["scope"] or "(top)", "  (%s)" % d["module"] if d["module"] else ""))
    if not d["index"]:
        print("        no source index: boxes from the dump only, no connections")
    if d["ports"]:
        print("ports   " + "; ".join(bundle_text(b) for b in d["ports"]))
    for b in d["boxes"]:
        size = "%d signals, %d insts" % (b["signals"], b["insts"]) + (", %d lines" % b["loc"] if b["loc"] else "")
        print("box     %-24s %s%s  %s" % (b["name"], "(%s) " % b["module"] if b["module"] else "", size,
                                         "" if b["in_dump"] else "[not in the dump]"))
        for bd in b["bundles"]:
            if bd["protocol"] not in (None, "clock", "reset") or len(bd["signals"]) > 2:
                print("          %s" % bundle_text(bd))
    for e in sorted(d["edges"], key=lambda e: -e["width"]):
        tag = e["protocol"] or "?"
        if e["role"]:
            tag += " " + e["role"]
        if e.get("label"):
            tag = '"%s"' % e["label"] + (", " + tag if e["protocol"] else "")
        print("edge    %s -> %s  %s [%s, %d bits, %d nets]" % (e["from"] or "(ports)", e["to"] or "(ports)",
                                                            e["bundle"], tag, e["width"], len(e["nets"])))
    if d.get("unlabelled"):
        print("unnamed %s" % ", ".join(d["unlabelled"]))
        print("        (name one: waveview.py label %s <box/bundle> \"what it is\"; its signals: diagram --json)"
              % (d["scope"] or "''"))


def _full_name(c, name):
    names = [row[0] for row in c.call("/api/signals")]
    if name in names:
        return name
    hits = [n for n in names if n.endswith("." + name)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise wq.QueryError("no signal named %r in the dump" % name)
    raise wq.QueryError("%r is ambiguous: %s" % (name, ", ".join(hits[:6])))


def print_source_info(c, info, loads=False, context=0):
    """Drivers (or loads) of a signal as source lines, each followed by the
    values at the cursor of the signals on it: active annotation as text."""
    st = c.call("/api/state")
    cur = st.get("cursor")
    print("%s.%s  (module %s)" % (info["scope"], info["signal"], info["module"]))
    if info.get("decl"):
        d = info["decl"]
        print("  declared  %s:%d%s" % (d["file"], d["line"], "  (%s port)" % d["dir"] if d.get("dir") else ""))
    places = info["loads"] if loads else info["drivers"]
    print("  %s (%d)%s:" % ("loads" if loads else "drivers", len(places),
                            "  values at cursor %s" % _t(cur) if cur is not None else ""))
    views = {}
    files = {}
    for p in places[:40]:
        scope = p["scope"]
        if scope not in views:
            try:
                views[scope] = c.call("/api/src/view?scope=" + urllib.parse.quote(scope))
            except wq.QueryError:
                views[scope] = None
        v = views[scope]
        if v is None:
            continue
        lines = files.setdefault(v["file"], v["text"].split("\n"))
        what = p["kind"]
        if p.get("instance"):
            what += " %s.%s" % (p["instance"], p.get("port") or "")
        print("  %s:%d  [%s]%s" % (p["file"], p["line"], what, "" if scope == info["scope"] else "  in " + scope))
        lo = max(1, p["line"] - context)
        hi = min(len(lines), p["line"] + context)
        for ln in range(lo, hi + 1):
            print("    %5d%s %s" % (ln, ">" if ln == p["line"] else " ", lines[ln - 1].rstrip()))
        if cur is None:
            continue
        on_line = sorted({i[2] for i in v["idents"] if i[0] == p["line"] and i[2] in v["signals"]})
        names = [n for k in on_line for n in v["signals"][k][:4]]
        if names:
            vals = c.call("/api/values", {"signals": names, "t": cur})["values"]
            shown = []
            for n in names:
                sig_w = len(vals.get(n, ""))
                shown.append("%s=%s" % (n[len(scope) + 1:], wq.fmt(vals[n], sig_w, "hex")))
            print("           = " + "  ".join(shown))


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
            print("%s  pid %d  %s" % (s["url"], s["pid"], s["dump"] or "(no dump open)"))
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
    elif args.cmd == "diagram":
        d = c.call("/api/diagram?scope=" + urllib.parse.quote(args.scope))
        if j:
            print(json.dumps(d))
            return 0
        print_diagram(d)
    elif args.cmd == "label":
        show(c.cmd("label", scope=args.scope, bundle=args.bundle, text=" ".join(args.text), clear=args.clear or None))
    elif args.cmd == "tree":
        t = c.call("/api/tree?scope=%s&depth=%d&limit=%d" % (urllib.parse.quote(args.scope), args.depth, args.limit))
        if j:
            print(json.dumps(t))
            return 0
        print_tree(t)
    elif args.cmd == "src":
        name = args.signal
        info = c.call("/api/src/signal?name=" + urllib.parse.quote(_full_name(c, name)))
        if j:
            print(json.dumps(info))
            return 0
        print_source_info(c, info, loads=args.loads, context=args.context)
        if args.open:
            c.cmd("source", signal=info["scope"] + "." + info["signal"], loads=args.loads or None)
    elif args.cmd == "gtkw":
        sys.stdout.write(c.call("/api/export.gtkw"))
    elif args.cmd == "rc":
        sys.stdout.write(c.call("/api/export.rc"))
    elif args.cmd == "load":
        if not args.file and not args.lines:
            raise wq.QueryError("load needs a file or -e LINE")
        warnings = []
        if args.file:
            text = sys.stdin.read() if args.file == "-" else open(args.file, errors="replace").read()
            base = "." if args.file == "-" else os.path.dirname(os.path.abspath(args.file))
            r = c.cmd("rc", text=text, merge=args.merge or None, base_dir=os.path.abspath(base),
                      origin=os.path.basename(args.file) if args.file != "-" else "stdin")
            warnings += r["result"]["warnings"]
        if args.lines:
            r = c.cmd("rc", text="\n".join(args.lines), merge=True, base_dir=os.getcwd(), origin="-e")
            warnings += r["result"]["warnings"]
        if j:
            print(json.dumps({"warnings": warnings}))
        else:
            for w in warnings:
                print("warning: %s" % w, file=sys.stderr)
            print("loaded%s" % (" with %d warnings" % len(warnings) if warnings else ""))
        return 1 if warnings else 0
    elif args.cmd == "save":
        text = c.call("/api/export.wvrc")
        if args.file:
            with open(args.file, "w") as fh:
                fh.write(text)
            print("saved %s" % args.file)
        else:
            sys.stdout.write(text)
    elif args.cmd == "unmark":
        kind = {"cursor": "cursors", "arrow": "arrows", "comment": "comments", "label": "annotations"}[args.kind]
        a = {"kind": kind}
        if args.which is None:
            a["all"] = True
        elif args.which.isdigit():
            a["index"] = int(args.which)
        else:
            a["name"] = args.which
        show(c.cmd("unmark", **a))
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
    check("signal list carries the declared kind", any(row[0] == "tb_wave.dut.req" and row[2] == "wire"
                                                       for row in c.call("/api/signals")))
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
    # source view: a dump whose hierarchy matches rtlindex's self test RTL
    src_dir = os.path.join(tmp, "rtl")
    os.makedirs(src_dir)
    with open(os.path.join(src_dir, "t.sv"), "w") as fh:
        fh.write(rtlindex.SELFTEST_SRC)
    vcd2 = os.path.join(tmp, "src.vcd")
    with open(vcd2, "w") as fh:
        fh.write("$timescale 1ns $end\n$scope module TOP $end\n$scope module top $end\n"
                 "$var wire 1 ! clk $end\n$var wire 8 \" w_mid [7:0] $end\n$var wire 8 # din [7:0] $end\n"
                 "$scope module u_leaf $end\n$var wire 8 $ r_q [7:0] $end\n$var wire 8 % i_d [7:0] $end\n"
                 "$upscope $end\n$upscope $end\n$upscope $end\n$enddefinitions $end\n"
                 "#0\n0!\nb0 \"\nb101 #\nb0 $\nb101 %\n#5\n1!\nb101 $\nb101 \"\n#10\n0!\n")
    hub2 = Hub(vcd2, rtl=None)
    hub2.rtl_specs = [src_dir]
    hub2.build_index()
    info = hub2.src_signal("TOP.top.w_mid")
    check("source: signal placed in module", info["module"] == "top" and info["scope"] == "TOP.top")
    check("source: driver through instance port", any(d["kind"] == "port_output" and d["instance"] == "u_leaf"
                                                       for d in info["drivers"]))
    view = hub2.src_view("TOP.top")
    check("source: view maps idents to dump names", view["signals"].get("w_mid") == ["TOP.top.w_mid"]
          and view["children"].get("u_leaf") == ["TOP.top.u_leaf"])
    tr = hub2.src_trace("TOP.top.u_leaf", "i_d", False)
    check("source: input traced to parent", any(t["kind"] == "parent_conn" and t["signal"] == "din" for t in tr))
    hub2.op_source({"signal": "u_leaf.r_q"})
    check("source: op_source opens the driver", hub2.state["source"]["line"] == 9)
    where = hub2.src_where("TOP.top.u_leaf")
    check("source: where a hierarchy node is defined and instantiated",
          where["module"] == "leaf" and where["line"] >= 1 and where["parent"] == "TOP.top"
          and where["instance"] == "u_leaf" and where["inst_line"] > where["line"])
    check("source: where of the top", "parent" not in hub2.src_where("TOP.top"))
    dg = hub2.diagram("TOP.top")
    names = {b["name"]: b for b in dg["boxes"]}
    check("diagram: instances as boxes with their size",
          dg["module"] == "top" and "u_leaf" in names and names["u_leaf"]["module"] == "leaf"
          and names["u_leaf"]["loc"] > 5 and names["u_leaf"]["signals"] > 0)
    T = rtlindex.Tok
    check("diagram: array elements are nets of their own, a variable index the whole array",
          net_key([T("id", "bus", 1, 0), T("op", "[", 1, 3), T("num", "1", 1, 4), T("op", "]", 1, 5)]) == "bus[1]"
          and net_key([T("id", "bus", 1, 0), T("op", "[", 1, 3), T("id", "i", 1, 4), T("op", "]", 1, 5)]) == "bus"
          and net_key([T("id", "bus", 1, 0), T("op", "[", 1, 3), T("num", "7", 1, 4), T("op", ":", 1, 5), T("num", "0", 1, 6),
                       T("op", "]", 1, 7)]) == "bus[7:0]"
          and net_key([T("num", "1'b0", 1, 0)]) is None)
    check("diagram: the scope's ports and connections through nets",
          any(b["signals"] == ["clk"] for b in dg["ports"])
          and any(e["from"] == "" and e["to"] == "u_leaf" for e in dg["edges"]))
    check("diagram: the top without an index", hub2.diagram("")["boxes"][0]["name"] == "TOP")
    tr = hub2.tree("", depth=3)
    top_node = tr["kids"][0]["kids"][0] if tr["kids"] and tr["kids"][0]["kids"] else {}
    check("tree: signals and scopes counted below each scope",
          tr["signals"] == 5 and tr["scopes"] == 3 and top_node.get("name") == "top" and top_node.get("signals") == 5
          and top_node.get("module") == "top" and top_node["kids"][0]["name"] == "u_leaf" and top_node["kids"][0]["signals"] == 2)
    check("tree: a subtree and its limit", hub2.tree("TOP.top", depth=1, limit=0)["kids"][0]["name"] == "+1 more")
    unnamed = dg["unlabelled"]
    hub2.apply("label", {"scope": "TOP.top", "bundle": unnamed[0], "text": "test bus"}, source="agent")
    dg2 = hub2.diagram("TOP.top")
    labelled = [bd for b in dg2["boxes"] for bd in b["bundles"] if bd.get("label")] + [bd for bd in dg2["ports"] if bd.get("label")]
    check("diagram: a label names an unidentified bundle", unnamed and labelled and labelled[0]["label"] == "test bus"
          and unnamed[0] not in dg2["unlabelled"])
    hub2.apply("label", {"scope": "TOP.top", "bundle": unnamed[0], "clear": True})
    check("diagram: label cleared", unnamed[0] in hub2.diagram("TOP.top")["unlabelled"])
    # a SystemVerilog interface between two instances: one edge each way through it
    with open(os.path.join(src_dir, "bus.sv"), "w") as fh:
        fh.write("interface bus_if (input logic clk);\n  logic valid, ready; logic [7:0] data;\n"
                 "  modport mst (output valid, data, input ready);\n  modport slv (input valid, data, output ready);\n"
                 "endinterface\nmodule src_m (input logic clk, bus_if.mst m);\nendmodule\n"
                 "module dst_m (input logic clk, bus_if.slv s);\nendmodule\n"
                 "module top3 (input logic clk);\n  bus_if bus_i (.clk(clk));\n  src_m u_src (.clk(clk), .m(bus_i));\n"
                 "  dst_m u_dst (.clk(clk), .s(bus_i));\nendmodule\n")
    vcd3 = os.path.join(tmp, "iface.vcd")
    with open(vcd3, "w") as fh:
        fh.write("$timescale 1ns $end\n$scope module top3 $end\n$var wire 1 ! clk $end\n"
                 "$scope module bus_i $end\n$var wire 1 \" valid $end\n$var wire 1 # ready $end\n$var wire 8 $ data [7:0] $end\n$upscope $end\n"
                 "$scope module u_src $end\n$var wire 1 % clk $end\n$upscope $end\n"
                 "$scope module u_dst $end\n$var wire 1 & clk $end\n$upscope $end\n$upscope $end\n"
                 "$enddefinitions $end\n#0\n0!\n#10\n1!\n")
    hub3 = Hub(vcd3, rtl=None)
    hub3.rtl_specs = [os.path.join(src_dir, "bus.sv")]
    hub3.build_index()
    dg3 = hub3.diagram("top3")
    boxes3 = {b["name"]: b for b in dg3["boxes"]}
    e3 = {(e["from"], e["to"]): e for e in dg3["edges"]}
    check("diagram: interface instance is a box carrying a valid/ready bus",
          boxes3.get("bus_i", {}).get("iface") and any(bd["protocol"] == "valid/ready" for bd in boxes3["bus_i"]["bundles"]))
    check("diagram: modports give the edges their way and role",
          ("u_src", "bus_i") in e3 and e3[("u_src", "bus_i")]["role"] == "source" and e3[("u_src", "bus_i")]["width"] == 11
          and ("bus_i", "u_dst") in e3 and e3[("bus_i", "u_dst")]["role"] == "sink")
    hub3.retire()
    # a filelist without the testbench: the DUT's scope is named after no module
    vcd_tb = os.path.join(tmp, "tb.vcd")
    with open(vcd_tb, "w") as fh:
        fh.write("$timescale 1ns $end\n$scope module TOP $end\n$scope module tb_x $end\n$var wire 1 ! tb_only $end\n"
                 "$scope module u_dut $end\n$var wire 1 \" clk $end\n$var wire 8 # din [7:0] $end\n"
                 "$var wire 8 $ w_mid [7:0] $end\n$var wire 8 ( dout [7:0] $end\n"
                 "$scope module u_leaf $end\n$var wire 8 % r_q [7:0] $end\n$var wire 8 & i_d [7:0] $end\n"
                 "$upscope $end\n$upscope $end\n$upscope $end\n$upscope $end\n$enddefinitions $end\n"
                 "#0\n0!\n0\"\nb0 #\nb0 $\nb0 (\nb0 %\nb0 &\n#5\n1\"\n")
    hub_tb = Hub(vcd_tb, rtl=None)
    hub_tb.rtl_specs = [src_dir]
    hub_tb.build_index()
    placed = hub_tb.src_signal("TOP.tb_x.u_dut.w_mid")
    check("source: DUT placed without its testbench", placed["module"] == "top" and placed["scope"] == "TOP.tb_x.u_dut")
    check("source: instance below the placed DUT", hub_tb.src_signal("TOP.tb_x.u_dut.u_leaf.r_q")["module"] == "leaf")
    try:
        hub_tb.src_signal("TOP.tb_x.tb_only")
        check("source: testbench signal is a clear error", False)
    except wq.QueryError as exc:
        check("source: testbench signal is a clear error", "outside the RTL" in str(exc))
    # what is sent to a server to be built there must be next to this file,
    # also inside the packaged app (build_macos.sh / build_windows.sh --add-data)
    check("remote: the sources a server builds are here",
          all(os.path.isfile(os.path.join(remote.HERE, f)) for f in list(remote.SOURCES.values()) + ["rtlindex.py"]))
    # serve --scope: only the signals below the scope are loaded
    hub_sc = Hub(path, rtl=None, scope=["tb_wave.dut.req"])
    check("scope: only what is below is loaded", [s.name for s in hub_sc.wave.signals] == ["tb_wave.dut.req"])
    # Verdi interop: an nWave .rc in, an .rc out, and FSDB through fsdb2vcd
    rc = os.path.join(tmp, "v.rc")
    with open(rc, "w") as fh:
        fh.write('Magic 271485\nzoom 10.000000 90.000000\ncursor 50.000000\naddGroup "G1"\n'
                 "addSignal -h 15 /tb_wave/dut/clk\naddSignal -h 15 -2COMP -DEC /tb_wave/dut/data[15:0]\n"
                 "addSignal -h 15 /tb_wave/dut/gone\n")
    hub3 = Hub(path, rc=rc)
    check("nWave .rc import", [s["name"] for s in hub3.state["signals"]] == ["tb_wave.dut.clk", "tb_wave.dut.data"]
          and hub3.state["signals"][1]["radix"] == "sdec" and hub3.state["cursor"] == 50 and hub3.state["view"] == [10, 90])
    out_rc = hub3.export_rc()
    check("nWave .rc export", "addSignal -h 15 -2COMP -DEC /tb_wave/dut/data[15:0]" in out_rc and "cursor 50.000000" in out_rc)
    # waveview rc: a debug view with named cursors, arrows and comments
    wvrc = os.path.join(tmp, "dbg.wvrc")
    with open(wvrc, "w") as fh:
        fh.write("# debug view\ndump t.vcd\nnote 'req answered late'\nsignal clk radix=bin\nsignal req\n"
                 "cursor A 7c color=red 'req rises'\ncursor B A+2c\ncursor A+1c\nmarker A\n"
                 "arrow req@A -> data@B '2 cycles' color=#123456\ncomment data@B \"BEEF #here\"  # tail\n"
                 "comment @25ns axis\ncomment nosuch@B bad\n")
    check("wvrc: dump line relative to the file", waverc.dump_of(open(wvrc).read(), tmp) == path)
    hub4 = Hub(path, rc=wvrc)
    st4 = hub4.state
    check("wvrc: signals, arrow targets added", [s["name"] for s in st4["signals"]]
          == ["tb_wave.dut.clk", "tb_wave.dut.req", "tb_wave.dut.data"] and st4["signals"][0]["radix"] == "bin")
    check("wvrc: named and relative cursors", [(c["name"], c["time"]) for c in st4["cursors"]] == [("A", 70), ("B", 90)]
          and st4["cursor"] == 80 and st4["marker"] == 70 and st4["cursors"][0]["color"] == waverc.COLORS["red"])
    check("wvrc: arrow", st4["arrows"] == [{"from": {"signal": "tb_wave.dut.req", "time": 70},
                                            "to": {"signal": "tb_wave.dut.data", "time": 90},
                                            "label": "2 cycles", "color": "#123456"}])
    check("wvrc: comments, units, # inside quotes", [(c["signal"], c["time"], c["text"]) for c in st4["comments"]]
          == [("tb_wave.dut.data", 90, "BEEF #here"), (None, 25, "axis")])
    check("wvrc: bad line is a warning", len(hub4.rc_warnings) == 1 and ":13:" in hub4.rc_warnings[0])
    check("wvrc: default zoom covers the marks", st4["view"][0] < 25 and st4["view"][1] > 90)
    hub5 = Hub(path)
    hub5.load_wvrc(hub4.export_wvrc())
    check("wvrc: export round trip", all(hub5.state[k] == st4[k] for k in
                                         ("signals", "cursors", "arrows", "comments", "cursor", "marker", "view")))
    hub4.load_wvrc("comment req@B+1c more", merge=True)
    check("wvrc: merge adds", len(hub4.state["comments"]) == 3 and hub4.state["comments"][-1]["time"] == 100
          and len(hub4.state["arrows"]) == 1)
    hub4.apply("unmark", {"kind": "cursors", "name": "A"})
    check("wvrc: unmark", [c["name"] for c in hub4.state["cursors"]] == ["B"])
    # stand-in for Verdi's fsdb2vcd: <in> -o <out>
    if os.name == "nt":
        fake = os.path.join(tmp, "fsdb2vcd.cmd")
        with open(fake, "w") as fh:
            fh.write('@copy /y "%s" "%%~3" >nul\n' % path)
    else:
        fake = os.path.join(tmp, "fsdb2vcd")
        with open(fake, "w") as fh:
            fh.write('#!/bin/sh\ncp "%s" "$3"\n' % path)
        os.chmod(fake, 0o755)
    fsdb = os.path.join(tmp, "t.fsdb")
    with open(fsdb, "w") as fh:
        fh.write("stand-in")
    # WAVE_EAGER: where Verdi is installed fsdbsrv would open it instead
    old = {k: os.environ.get(k) for k in ("FSDB2VCD", "WAVE_EAGER")}
    os.environ["FSDB2VCD"] = fake
    os.environ["WAVE_EAGER"] = "1"
    try:
        w = wq.load(fsdb)
        check("FSDB through fsdb2vcd", "tb_wave.dut.data" in w.by_name)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    # a dump and RTL on "another machine": ssh replaced by a local stand-in,
    # which builds fstsrv from source like a real server would
    conv = w2i.build_fst_converter(verbose=False)
    if conv is None or os.name == "nt" or w2i.fst_include() is None:
        print("skip remote checks: needs sh and the FST tools")
    else:
        import subprocess

        fst = os.path.join(tmp, "r.fst")
        subprocess.run([conv, "--vcd2fst", path, "-o", fst], check=True, capture_output=True)
        rhome = os.path.join(tmp, "remote_home")
        os.makedirs(rhome)
        fake = os.path.join(tmp, "fake_ssh")
        with open(fake, "w") as fh:
            fh.write('#!/bin/sh\n# ssh stand-in: drop options and the host, run the command here\n'
                     'while [ $# -gt 0 ]; do case "$1" in -T|-C) shift;; -o|-p) shift 2;; *) break;; esac; done\n'
                     'shift\ncd "%s" && HOME="%s" exec sh -c "$*"\n' % (rhome, rhome))
        os.chmod(fake, 0o755)
        old = os.environ.get("WAVEVIEW_SSH")
        os.environ["WAVEVIEW_SSH"] = fake
        try:
            spec = "buildhost:" + fst
            rw = wq.load(spec)
            lw = wq.load(fst)
            same = all(rw.value(rw.by_name[n], t) == lw.value(lw.by_name[n], t)
                       for n in ("tb_wave.dut.data", "tb_wave.dut.req") for t in (0, 25, 50, 199))
            check("remote: FST read over ssh", type(rw).__name__ == "RemoteFst" and same)
            check("remote: fstsrv built and cached on the server",
                  any(f == "fstsrv" for _, _, fs in os.walk(os.path.join(rhome, ".cache")) for f in fs))
            check("remote: dump stamp", rw.stamp()[0] == os.path.getsize(fst))
            rw.close()
            rw = wq.load(spec, scope=["tb_wave.dut.req"])
            check("remote: --scope reaches the server", [s.name for s in rw.signals] == ["tb_wave.dut.req"]
                  and rw.value(rw.by_name["tb_wave.dut.req"], 25) == lw.value(lw.by_name["tb_wave.dut.req"], 25))
            rw.close()
            lw.close()
            r, _ = remote.parse(spec)
            doc = r.index_rtl([src_dir], src_dir)
            got = r.read_files([os.path.join(src_dir, "t.sv")])
            check("remote: RTL indexed on the server and read over ssh",
                  "leaf" in doc["modules"] and "module leaf" in got[os.path.join(src_dir, "t.sv")])
            with open(os.path.join(tmp, "r2.fsdb"), "w") as fh:
                fh.write("stand-in")
            seen = opener.list_remote("buildhost", tmp)
            # pwd on the "server" may spell the directory through a symlink (macOS: /var -> /private/var)
            check("remote: directory listing", os.path.realpath(seen["path"]) == os.path.realpath(tmp)
                  and any(e["name"] == "r.fst" and e["size"] == os.path.getsize(fst) for e in seen["entries"])
                  and any(e["name"] == "rtl" and e["dir"] for e in seen["entries"])
                  and any(e["name"] == "r2.fsdb" for e in seen["entries"])
                  and not any(e["name"] == "t.vcd" for e in seen["entries"]))
            with open(os.path.join(src_dir, "list.f"), "w") as fh:
                fh.write("t.sv\n")
            seen = opener.list_remote("buildhost", src_dir, "rtl")
            check("remote: RTL listing shows filelists, not sources",
                  [e["name"] for e in seen["entries"] if not e["dir"]] == ["list.f"])
        finally:
            if old is None:
                os.environ.pop("WAVEVIEW_SSH", None)
            else:
                os.environ["WAVEVIEW_SSH"] = old
    # the Open dialog: start with no dump, list, open, and refuse a bad host
    Handler.hub = None
    check("no dump: session is empty", c.call("/api/session")["dump"] is None)
    try:
        c.call("/api/meta")
        check("no dump: wave calls refused", False)
    except wq.QueryError:
        check("no dump: wave calls refused", True)
    listed = c.call("/api/ls", {"path": tmp})
    check("open: local listing", any(e["name"] == "t.vcd" and not e["dir"] for e in listed["entries"])
          and any(e["name"] == "rtl" and e["dir"] for e in listed["entries"]))
    with open(os.path.join(tmp, "rtl", "files.f"), "w") as fh:
        fh.write("t.sv\n")
    listed = c.call("/api/ls", {"path": os.path.join(tmp, "rtl"), "kind": "rtl"})
    names = [e["name"] for e in listed["entries"] if not e["dir"]]
    check("open: RTL listing shows filelists, not sources", "files.f" in names and "t.sv" not in names)
    os.makedirs(os.path.join(tmp, "rtl", "work.lib++"))
    listed = c.call("/api/ls", {"path": os.path.join(tmp, "rtl"), "kind": "rtl"})
    plain = c.call("/api/ls", {"path": os.path.join(tmp, "rtl")})
    check("open: a Verdi KDB is listed as one to choose",
          any(e["name"] == "work.lib++" and e.get("db") for e in listed["entries"])
          and not any(e.get("db") for e in plain["entries"]))
    try:
        c.call("/api/ls", {"path": tmp, "kind": "everything"})
        check("open: unknown listing kind refused", False)
    except wq.QueryError:
        check("open: unknown listing kind refused", True)
    try:
        c.call("/api/ls", {"host": "-oProxyCommand=x", "path": "/"})
        check("open: option-like host refused", False)
    except wq.QueryError:
        check("open: option-like host refused", True)
    cfg = os.path.join(tmp, "ssh_config")
    with open(cfg, "w") as fh:
        fh.write("Host build01 build02\n  User me\nHost *.lab !x\nhost = gw # jump\nMatch all\n")
    check("open: hosts from ssh config", opener.ssh_hosts(cfg) == ["build01", "build02", "gw"])
    check("open: batch ssh", "BatchMode=yes" in type("R", (remote.Remote,), {"batch": True})("h").argv("true"))
    try:
        c.call("/api/open", {"path": os.path.join(tmp, "missing.vcd")})
        check("open: a bad file is an error", False)
    except wq.QueryError:
        check("open: a bad file is an error", Handler.hub is None)
    c.call("/api/open", {"path": path})
    check("open: dump loaded", c.call("/api/session")["dump"] == path and c.call("/api/meta")["signals"] > 0)
    first = Handler.hub
    c.call("/api/open", {"path": vcd2})
    check("open: switching retires the old dump", first.retired and c.call("/api/session")["dump"] == vcd2)
    Handler.hub.retire()
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
    s.add_argument("dump", nargs="?",
                   help="the dump (default: the 'dump' line of --rc, else choose one in the Open dialog)")
    s.add_argument("--port", type=int, default=0, help="port (default: first free from %d)" % DEFAULT_PORT)
    s.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    s.add_argument("--token", help="fixed access token (default: random)")
    s.add_argument("--gtkw", help="load the signal list from a GTKWave save file")
    s.add_argument("--rc", help="open on a saved view: a waveview rc (.wvrc: signals, cursors, arrows, "
                                "comments; see waverc.py) or a Verdi nWave save file (.rc)")
    s.add_argument("--rtl", action="append",
                   help="RTL sources for the source view: a .f filelist, a file or a directory "
                        "(repeatable; default: the current directory, without vendor/)")
    s.add_argument("--no-rtl", action="store_true", help="no source view")
    s.add_argument("--scope", action="append", metavar="A.B.C",
                   help="load only the signals below this scope (repeatable): a dump with tens of millions of "
                        "signals opens in seconds instead of minutes")
    s.add_argument("--rtl-root", help="for --rtl on another machine: the directory there that source paths are "
                                       "shown relative to (default: the first entry's directory)")
    s.add_argument("--open", action="store_true", help="open a local browser")
    s.add_argument("--window", action="store_true",
                   help="open an application window (Chrome or Edge); closing it stops the server")

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
    sub.add_parser("rc", parents=[client], help="print the view as a Verdi nWave save file (.rc)")
    s = sub.add_parser("load", parents=[client],
                       help="apply a waveview rc file (.wvrc): signals, cursors, arrows, comments")
    s.add_argument("file", nargs="?", help="the .wvrc file, or - for stdin")
    s.add_argument("-e", dest="lines", action="append", default=[],
                   help="apply this rc line on top of the view (repeatable), e.g. "
                        "-e 'arrow req@10c ack@12c \"2 cycles\"'")
    s.add_argument("--merge", action="store_true", help="add to the view instead of replacing it")
    s = sub.add_parser("save", parents=[client], help="write the view as a waveview rc file (.wvrc)")
    s.add_argument("file", nargs="?", help="output file (default: stdout)")
    s = sub.add_parser("unmark", parents=[client], help="remove named cursors, arrows or comments")
    s.add_argument("kind", choices=("cursor", "arrow", "comment", "label"))
    s.add_argument("which", nargs="?", help="cursor name, or index (from `look`); none: all of that kind")
    s = sub.add_parser("src", parents=[client],
                       help="where a signal is declared and driven, with values at the cursor")
    s.add_argument("signal", help="dump signal (full name or unique suffix)")
    s.add_argument("--loads", action="store_true", help="show the readers instead of the drivers")
    s.add_argument("--context", type=int, default=0, help="lines of source around each place")
    s.add_argument("--open", action="store_true", help="also show it in the viewer's source pane")
    s = sub.add_parser("diagram", parents=[client],
                       help="block diagram of a scope as text: instances with their size and interfaces "
                            "(AXI, APB, CHI... told from the names), and the connections between them")
    s.add_argument("scope", nargs="?", default="", help="dump scope (default: the top)")
    s = sub.add_parser("label", parents=[client],
                       help="name an interface bundle of the diagram that the signal names did not identify")
    s.add_argument("scope", help="dump scope of the diagram ('' for the top)")
    s.add_argument("bundle", help="box/bundle as `diagram` lists it (u_cpu/dbg), or /bundle for the scope's ports")
    s.add_argument("text", nargs="*", help="what it is, e.g. 'debug trace port'; none with --clear removes it")
    s.add_argument("--clear", action="store_true")
    s = sub.add_parser("tree", parents=[client],
                       help="the hierarchy by size: signals and scopes below each scope, module and lines")
    s.add_argument("scope", nargs="?", default="", help="dump scope to start from (default: the top)")
    s.add_argument("--depth", type=int, default=3, help="levels to show (default 3)")
    s.add_argument("--limit", type=int, default=40, help="largest children shown per scope (default 40)")
    return p


def desktop_argv(argv):
    """The packaged exe started from Explorer or the Finder: no arguments
    (double click) or one dump (dropped on the exe, or "Open with"). Anything
    else is the normal command line. The source view stays off until asked
    for in the Open dialog: the working directory of a double click is not a
    project. The Finder may add a process serial number (-psn_...): not ours."""
    argv = [a for a in argv if not a.startswith("-psn_")]
    if len(argv) > 1 or (argv and not argv[0].lower().endswith((".fst", ".vcd", ".fsdb"))):
        return None
    return ["serve", "--no-rtl", "--window"] + argv


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    launched = desktop_argv(argv) if getattr(sys, "frozen", False) else None
    if launched is None:
        return run_command(argv)
    # started by a double click: an error must stay readable
    try:
        rc = run_command(launched)
    except Exception as exc:
        rc = 1
        opener.report_error(str(exc))
    else:
        if rc:
            opener.report_error("waveview exited with status %d" % rc)
    return rc


def run_command(argv):
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
    except BrokenPipeError:  # output piped into head and the like
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
