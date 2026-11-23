#!/usr/bin/env python3
"""Query a simulation waveform dump from the command line, as text.

A waveform viewer for AI agents (and for people in a terminal). GTKWave is
built for eyes and a mouse; an agent can only run commands and read their
output. This tool answers the questions one normally answers by scrolling a
waveform, and prints the answer as a compact table or as JSON:

    info      what is in the dump: time range, timescale, likely clocks
    list      which signals exist (glob filter), with width and change count
    get       values of signals at one time or one clock cycle
    table     a cycle-by-cycle table of signals over a window
    changes   every value change of some signals inside a window
    find      the cycles at which a condition holds (first / all / count)

Design points for agent use:
- Output is bounded. Every command that can print many rows stops at
  --max-rows (default 200) and says how many rows it left out, so one command
  cannot flood the context window
- Errors say what to do next: an ambiguous or unknown signal name lists the
  closest candidates instead of failing silently
- --format json gives a stable, machine readable shape for every command
- The parsed dump is cached next to the user cache (keyed on path, size and
  mtime), so the second query on a large dump does not re-parse it
- Signals can be named by their full hierarchical name, or by any unique
  suffix ("rob.commit_valid" instead of "TOP.tb.u_dut.u_core.rob.commit_valid")

Cycles:
- Cycle n is the interval starting at the n-th rising edge (0 based) of the
  clock. The value reported for cycle n is the value held right after that
  edge, which is what a waveform viewer shows just to the right of the edge
- The clock is --clock, or the 1-bit signal whose name contains "clk" with
  the most rising edges

Expressions (find, table --around, table --where):
- Python like syntax: == != < <= > >= and or not & | ^ ~ << >> + - *;
  C style && || ! are accepted too. Numbers: 10, 0x1f, 0b101
- A signal is a bare dotted name (core.rob.commit_valid) or, when it holds
  characters Python cannot spell, a braced name ({u_core.pc[3]})
- x / z bits make a value unknown; any comparison with an unknown is false
- Functions: rose(s) fell(s) changed(s) stable(s) prev(s) prev(s, n)
  bit(s, n) bits(s, hi, lo) isx(s)

Examples:
    tools/wavequery.py info sim.fst
    tools/wavequery.py list sim.fst '*rob*valid*'
    tools/wavequery.py find sim.fst 'commit_valid && commit_pc == 0x80000040'
    tools/wavequery.py table sim.fst -s '*commit_*' --around 'rose(trap)' -B 3 -A 5
    tools/wavequery.py changes sim.fst -s '*.flush*' --cycles 100:400
    tools/wavequery.py get sim.fst --cycle 1234 -s '*.pc' --format json
    tools/wavequery.py --selftest

FST is read in place and on demand (tools/wavesource.py and tools/fstsrv.c):
opening reads only the hierarchy, and each query decompresses only the
signals and time range it needs, so dump size does not matter. VCD (and
FSDB through fsdb2vcd) is read into memory with tools/wave2img.py's parser.
A search over a long range evaluates only where its signals change.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import hashlib
import json
import os
import pickle
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wave2img as w2i  # noqa: E402

CACHE_VERSION = 1
DEFAULT_MAX_ROWS = 200


class QueryError(Exception):
    """A user facing error. The message should say what to try next."""


# ---------------------------------------------------------------------------
# Loading, with a parse cache
# ---------------------------------------------------------------------------


def _cache_path(path):
    st = os.stat(path)
    key = "%s|%d|%d|%d" % (os.path.abspath(path), st.st_size, int(st.st_mtime_ns), CACHE_VERSION)
    digest = hashlib.sha1(key.encode()).hexdigest()[:20]
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "scariv", "wavequery", digest + ".pickle")


class Wave:
    """A loaded dump plus the indexes the queries need."""

    def __init__(self, dump):
        self.timescale = dump.timescale
        self.start_time = dump.start_time
        self.end_time = dump.end_time
        self.signals = dump.signals
        self.times = dump.times
        self.values = dump.values
        self.by_name = {s.name: s for s in self.signals}
        self._edges = {}

    # -- signal lookup ------------------------------------------------------

    def resolve(self, name):
        """One signal from a full name or a unique suffix."""
        sig = self.by_name.get(name)
        if sig is not None:
            return sig
        if any(c in name for c in "*?"):
            hits = [s for s in self.signals if w2i._matches(s.name, name)]
        else:
            tail = "." + name
            hits = [s for s in self.signals if s.name.endswith(tail)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise QueryError(
                "no signal named %r. Closest names:\n%s\n"
                "hint: `list <dump> '*%s*'` shows what exists"
                % (name, self._suggest(name), name.split(".")[-1])
            )
        listing = "\n".join("  " + s.name for s in hits[:10])
        more = "\n  ... %d more" % (len(hits) - 10) if len(hits) > 10 else ""
        raise QueryError(
            "%r is ambiguous, it matches %d signals:\n%s%s\n"
            "hint: add more of the hierarchy to make it unique" % (name, len(hits), listing, more)
        )

    def _suggest(self, name):
        leaf = name.split(".")[-1].lower()
        leaf = re.sub(r"\[.*", "", leaf)
        scored = []
        for s in self.signals:
            sl = s.name.split(".")[-1].lower()
            if leaf and leaf in sl:
                scored.append((0, len(sl), s.name))
            elif leaf and sl in leaf and len(sl) > 2:
                scored.append((1, len(sl), s.name))
        if not scored:
            import difflib

            names = [s.name.split(".")[-1] for s in self.signals]
            close = set(difflib.get_close_matches(leaf, names, n=8, cutoff=0.6))
            scored = [(2, 0, s.name) for s in self.signals if s.name.split(".")[-1] in close]
        scored.sort()
        if not scored:
            return "  (none)"
        return "\n".join("  " + n for _, _, n in scored[:8])

    def select(self, patterns):
        """Signals for -s patterns: glob over full names, or unique suffix."""
        out, seen = [], set()
        for pat in patterns:
            if any(c in pat for c in "*?"):
                hits = [s for s in self.signals if w2i._matches(s.name, pat)]
                if not hits and not pat.startswith("*"):
                    hits = [s for s in self.signals if w2i._matches(s.name, "*." + pat)]
                if not hits:
                    raise QueryError(
                        "pattern %r matched no signal. Closest names:\n%s" % (pat, self._suggest(pat.replace("*", "")))
                    )
            else:
                hits = [self.resolve(pat)]
            for s in hits:
                if s.name not in seen:
                    seen.add(s.name)
                    out.append(s)
        return out

    # -- values -------------------------------------------------------------

    def value(self, sig, t):
        """Normalized bit string held at time t."""
        return w2i.value_at(self.times.get(sig.code, []), self.values.get(sig.code, []), t, sig.width)

    def changes(self, sig, t0, t1):
        """[(time, value)] for changes with t0 <= time <= t1."""
        times = self.times.get(sig.code, [])
        values = self.values.get(sig.code, [])
        i = bisect.bisect_left(times, t0)
        out = []
        while i < len(times) and times[i] <= t1:
            out.append((times[i], w2i.normalize(values[i], sig.width)))
            i += 1
        return out

    # -- clocks -------------------------------------------------------------

    def edges(self, sig):
        if sig.code not in self._edges:
            self._edges[sig.code] = w2i.rising_edges(self, sig)
        return self._edges[sig.code]

    def clock_candidates(self):
        cands = []
        for s in self.signals:
            if s.width != 1:
                continue
            leaf = s.name.split(".")[-1].lower()
            if "clk" in leaf or "clock" in leaf:
                cands.append((len(self.edges(s)), s))
        cands.sort(key=lambda c: (-c[0], len(c[1].name)))
        return cands

    def pick_clock(self, name):
        if name:
            sig = self.resolve(name)
            if not self.edges(sig):
                raise QueryError("clock %s has no rising edge in the dump" % sig.name)
            return sig
        cands = [c for c in self.clock_candidates() if c[0] > 0]
        if not cands:
            raise QueryError("no clock found automatically; pass --clock <signal>")
        return cands[0][1]

    # dump-like interface for wave2img helpers
    def by_code(self, code):
        return self.times.get(code, []), self.values.get(code, [])

    # -- the interface shared with the lazy sources (tools/wavesource.py) --

    lazy = False

    def values_at(self, sigs, t):
        return {s.name: self.value(s, t) for s in sigs}

    def change_count(self, sig):
        return len(self.times.get(sig.code, []))

    def close(self):
        pass

    def next_change(self, sig, t, direction):
        """Time of the next / previous real value change."""
        times = self.times.get(sig.code, [])
        values = self.values.get(sig.code, [])
        norm = lambda k: w2i.normalize(values[k], sig.width)  # noqa: E731
        if direction == "next":
            cur = self.value(sig, t)
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

    def buckets(self, sigs, ts, level, n):
        """Per signal: (value before ts, [(bucket, first_t, last_t, changes,
        changes_to_1, last_value)]) for n buckets of 2^level from ts. Only
        real changes count; buckets are found by bisection."""
        te = ts + (n << level)
        out = {}
        for sig in sigs:
            times = self.times.get(sig.code, [])
            values = self.values.get(sig.code, [])
            i = bisect.bisect_left(times, ts)
            end = bisect.bisect_left(times, te, i)
            prev = w2i.normalize(values[i - 1], sig.width) if i > 0 else "x" * sig.width
            v0 = prev
            res = []
            while i < end:
                b = (times[i] - ts) >> level
                j = bisect.bisect_left(times, ts + ((b + 1) << level), i, end)
                first = last = None
                cnt = ones = 0
                if j - i > 64 and len(set(values[i:j])) == 1 and w2i.normalize(values[i], sig.width) == prev:
                    i = j
                    continue
                for m in range(i, j):
                    v = w2i.normalize(values[m], sig.width)
                    if v == prev:
                        continue
                    if first is None:
                        first = times[m]
                    last = times[m]
                    cnt += 1
                    ones += v == "1"
                    prev = v
                if cnt:
                    res.append((b, first, last, cnt, ones, prev))
                i = j
            out[sig.code] = (v0, res)
        return out


def load(path):
    if not os.path.isfile(path):
        raise QueryError("no such file: %s" % path)
    # FST is read in place, on demand (tools/wavesource.py); WAVE_EAGER=1
    # falls back to converting and loading it whole
    import wavesource

    lazy = wavesource.open_lazy(path)
    if lazy is not None:
        return lazy
    cpath = _cache_path(path)
    if os.path.isfile(cpath):
        try:
            with open(cpath, "rb") as fh:
                data = pickle.load(fh)
            return _from_cache(data)
        except Exception:
            pass
    with tempfile.TemporaryDirectory(prefix="wavequery_") as tmp:
        dump = w2i.load_dump(path, tmp)
    if dump is None:
        raise QueryError("could not read %s" % path)
    try:
        os.makedirs(os.path.dirname(cpath), exist_ok=True)
        tmpname = cpath + ".%d.tmp" % os.getpid()
        with open(tmpname, "wb") as fh:
            pickle.dump(_to_cache(dump), fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmpname, cpath)
    except OSError:
        pass
    return Wave(dump)


def _to_cache(dump):
    return {
        "timescale": dump.timescale,
        "start": dump.start_time,
        "end": dump.end_time,
        "signals": [(s.name, s.width, s.code) for s in dump.signals],
        "times": dump.times,
        "values": dump.values,
    }


def _from_cache(data):
    dump = w2i.Dump()
    dump.timescale = data["timescale"]
    dump.start_time = data["start"]
    dump.end_time = data["end"]
    dump.signals = [w2i.Signal(n, w, c, i) for i, (n, w, c) in enumerate(data["signals"])]
    dump.times = data["times"]
    dump.values = data["values"]
    return Wave(dump)


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------


def to_int(bits):
    """Integer of a bit string, or None when any bit is x/z."""
    if w2i.is_unknown(bits):
        return None
    return int(bits, 2)


def fmt(bits, width, radix):
    if width == 1:
        return bits
    if w2i.is_unknown(bits):
        if all(c == "x" for c in bits):
            return "x"
        if all(c == "z" for c in bits):
            return "z"
        return "b" + bits
    n = int(bits, 2)
    if radix == "dec":
        return str(n)
    if radix == "bin":
        return "b" + bits
    return "0x%0*x" % (max(1, (width + 3) // 4), n)


def json_value(bits, width):
    n = to_int(bits)
    return n if n is not None else bits


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

_FUNCS = {"rose", "fell", "changed", "stable", "prev", "bit", "bits", "isx"}


def _preprocess(text):
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    braced = {}

    def repl(m):
        key = "__sig%d__" % len(braced)
        braced[key] = m.group(1).strip()
        return key

    text = re.sub(r"\{([^{}]+)\}", repl, text)
    return text.strip(), braced


class Expr:
    """A compiled condition over sampled signal values."""

    def __init__(self, text, wave):
        self.text = text
        src, braced = _preprocess(text)
        try:
            self.tree = ast.parse(src, mode="eval").body
        except SyntaxError as exc:
            raise QueryError("cannot parse expression %r: %s" % (text, exc.msg))
        self.wave = wave
        self.braced = braced
        self.signals = {}  # reference text -> Signal
        self.max_lag = 1
        self.temporal = False  # uses prev / rose / fell / changed / stable
        self._collect(self.tree)

    def _ref(self, node):
        """Signal reference text of a Name / Attribute / constant Subscript
        chain (core.rob.valid, arr[0].field), or None."""
        if isinstance(node, ast.Name):
            return self.braced.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._ref(node.value)
            return base and base + "." + node.attr
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, int):
            base = self._ref(node.value)
            return base and "%s[%d]" % (base, node.slice.value)
        return None

    def _collect(self, node):
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            ref = self._ref(node)
            if ref is None:
                raise QueryError("unsupported expression part in %r (a variable index? use bits(s, hi, lo))" % self.text)
            if ref not in self.signals:
                self.signals[ref] = self.wave.resolve(ref)
            return
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise QueryError(
                    "unknown function in %r; available: %s" % (self.text, ", ".join(sorted(_FUNCS)))
                )
            if node.func.id in ("prev", "rose", "fell", "changed", "stable"):
                self.temporal = True
            if node.func.id == "prev" and len(node.args) == 2 and isinstance(node.args[1], ast.Constant):
                self.max_lag = max(self.max_lag, int(node.args[1].value))
            for a in node.args:
                self._collect(a)
            return
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, int):
                raise QueryError("only integer constants are allowed in %r" % self.text)
            return
        allowed = (ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare)
        if not isinstance(node, allowed):
            raise QueryError("unsupported syntax %s in %r" % (type(node).__name__, self.text))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.operator, ast.boolop, ast.cmpop, ast.unaryop)):
                continue
            self._collect(child)

    # evaluation: history[k] maps Signal.code -> int|None for cycle (i - k)
    def evaluate(self, history):
        return bool(self._ev(self.tree, history, 0))

    def _sig_value(self, node, history, lag):
        sig = self.signals[self._ref(node)]
        if lag >= len(history):
            return None
        return history[lag].get(sig.code)

    def _ev(self, node, h, lag):
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            return self._sig_value(node, h, lag)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            vals = (self._ev(v, h, lag) for v in node.values)
            if isinstance(node.op, ast.And):
                return all(bool(v) for v in vals)
            return any(bool(v) for v in vals)
        if isinstance(node, ast.UnaryOp):
            v = self._ev(node.operand, h, lag)
            if isinstance(node.op, ast.Not):
                return v is not None and not v
            if v is None:
                return None
            if isinstance(node.op, ast.Invert):
                return ~v
            if isinstance(node.op, ast.USub):
                return -v
            return v
        if isinstance(node, ast.BinOp):
            a = self._ev(node.left, h, lag)
            b = self._ev(node.right, h, lag)
            if a is None or b is None:
                return None
            op = node.op
            if isinstance(op, ast.BitAnd):
                return a & b
            if isinstance(op, ast.BitOr):
                return a | b
            if isinstance(op, ast.BitXor):
                return a ^ b
            if isinstance(op, ast.LShift):
                return a << b
            if isinstance(op, ast.RShift):
                return a >> b
            if isinstance(op, ast.Add):
                return a + b
            if isinstance(op, ast.Sub):
                return a - b
            if isinstance(op, ast.Mult):
                return a * b
            if isinstance(op, ast.Mod):
                return a % b if b else None
            if isinstance(op, (ast.FloorDiv, ast.Div)):
                return a // b if b else None
            raise QueryError("unsupported operator in %r" % self.text)
        if isinstance(node, ast.Compare):
            left = self._ev(node.left, h, lag)
            for op, rnode in zip(node.ops, node.comparators):
                right = self._ev(rnode, h, lag)
                if left is None or right is None:
                    return False
                ok = {
                    ast.Eq: left == right,
                    ast.NotEq: left != right,
                    ast.Lt: left < right,
                    ast.LtE: left <= right,
                    ast.Gt: left > right,
                    ast.GtE: left >= right,
                }.get(type(op))
                if ok is None:
                    raise QueryError("unsupported comparison in %r" % self.text)
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            name = node.func.id
            args = node.args
            if name == "prev":
                n = int(args[1].value) if len(args) > 1 else 1
                return self._ev(args[0], h, lag + n)
            if name == "isx":
                return self._ev(args[0], h, lag) is None
            if name in ("rose", "fell", "changed", "stable"):
                now = self._ev(args[0], h, lag)
                before = self._ev(args[0], h, lag + 1)
                if now is None or before is None:
                    return False
                if name == "rose":
                    return bool(now) and not bool(before)
                if name == "fell":
                    return bool(before) and not bool(now)
                if name == "changed":
                    return now != before
                return now == before
            if name == "bit":
                v = self._ev(args[0], h, lag)
                return None if v is None else (v >> args[1].value) & 1
            if name == "bits":
                v = self._ev(args[0], h, lag)
                hi, lo = args[1].value, args[2].value
                return None if v is None else (v >> lo) & ((1 << (hi - lo + 1)) - 1)
        raise QueryError("unsupported syntax in %r" % self.text)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class Timeline:
    """Cycle <-> time mapping for one clock."""

    def __init__(self, wave, clock):
        self.wave = wave
        self.clock = clock
        self.edges = wave.edges(clock) if clock is not None else []

    def cycle_of(self, t):
        if not self.edges:
            return None
        i = bisect.bisect_right(self.edges, t) - 1
        return i if i >= 0 else None

    def time_of(self, cycle):
        if not self.edges:
            raise QueryError("no clock, cycle numbers are not available")
        if cycle < 0:
            cycle += len(self.edges)
        if not 0 <= cycle < len(self.edges):
            raise QueryError("cycle %d is outside the dump (0..%d)" % (cycle, len(self.edges) - 1))
        return self.edges[cycle]

    def cycle_range(self, t0, t1):
        lo = bisect.bisect_left(self.edges, t0)
        hi = bisect.bisect_right(self.edges, t1)
        return range(lo, hi)

    def sample(self, sigs, cycle):
        t = self.edges[cycle]
        return {s.code: to_int(self.wave.value(s, t)) for s in sigs}


def parse_cycles(text, n_edges):
    m = re.fullmatch(r"\s*(-?\d*)\s*:\s*(-?\d*)\s*", text)
    if not m:
        raise QueryError("--cycles wants START:END (either side may be empty), got %r" % text)
    a = int(m.group(1)) if m.group(1) else 0
    b = int(m.group(2)) if m.group(2) else n_edges - 1
    if a < 0:
        a += n_edges
    if b < 0:
        b += n_edges
    return max(0, a), min(n_edges - 1, b)


def parse_time(text):
    text = text.strip().lower().replace("_", "")
    return int(text, 0)


def time_window(args, wave, tl):
    """(t0, t1) from --from/--to/--cycles, defaulting to the whole dump."""
    t0, t1 = wave.start_time, wave.end_time
    if getattr(args, "cycles", None):
        if not tl.edges:
            raise QueryError("--cycles needs a clock")
        a, b = parse_cycles(args.cycles, len(tl.edges))
        t0 = tl.edges[a]
        t1 = tl.edges[b + 1] - 1 if b + 1 < len(tl.edges) else wave.end_time
    if getattr(args, "time_from", None) is not None:
        t0 = parse_time(args.time_from)
    if getattr(args, "time_to", None) is not None:
        t1 = parse_time(args.time_to)
    if t1 < t0:
        raise QueryError("empty window: end %d is before start %d" % (t1, t0))
    return t0, t1


EVENT_WINDOW = 1 << 16  # cycles per step of the event driven scan


def scan(expr, tl, cycles):
    """Yield the cycles in `cycles` where expr holds."""
    if getattr(tl.wave, "lazy", False) and isinstance(cycles, range) and len(cycles) > 4096 and expr.signals:
        yield from _scan_events(expr, tl, cycles)
        return
    yield from _scan_cycles(expr, tl, cycles)


def _scan_events(expr, tl, cycles):
    """The same answer as _scan_cycles, evaluating only where it can change.

    The expression reads its signals at the current cycle and at most
    max_lag cycles back, so its value can only differ from the previous
    cycle's at a cycle where one of its signals changed, or up to max_lag
    cycles after one. Everywhere else it repeats. Over a long dump this
    turns a per-cycle loop into one evaluation per change, which matters
    when a lazy source holds millions of cycles."""
    sigs = list({s.code: s for s in expr.signals.values()}.values())
    wave, edges = tl.wave, tl.edges
    lag = expr.max_lag + 1 if expr.temporal else 0
    c = cycles.start
    stop = cycles.stop
    while c < stop:
        c1 = min(stop, c + EVENT_WINDOW)
        t_lo = edges[max(0, c - lag)]
        t_hi = edges[c1 - 1]
        cand = {c}
        for s in sigs:
            for t, _ in wave.changes(s, t_lo, t_hi):
                k = bisect.bisect_left(edges, t)  # first cycle that sees it
                for d in range(lag + 1):
                    if c <= k + d < c1:
                        cand.add(k + d)
        order = sorted(cand)
        for i, k in enumerate(order):
            history = [tl.sample(sigs, j) for j in range(k, max(-1, k - lag - 1), -1)]
            if expr.evaluate(history):
                end = order[i + 1] if i + 1 < len(order) else c1
                for j in range(k, end):
                    yield j
        c = c1


def _scan_cycles(expr, tl, cycles):
    sigs = list({s.code: s for s in expr.signals.values()}.values())
    history = []
    first = cycles.start if isinstance(cycles, range) else None
    lag = expr.max_lag + 1
    # prime the history so rose()/prev() work on the first scanned cycle
    if first is not None:
        for c in range(max(0, first - lag), first):
            history.insert(0, tl.sample(sigs, c))
    for c in cycles:
        history.insert(0, tl.sample(sigs, c))
        del history[lag + 1 :]
        if expr.evaluate(history):
            yield c


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class Out:
    def __init__(self, args):
        self.format = args.format
        self.max_rows = args.max_rows

    def emit(self, obj, text_lines):
        if self.format == "json":
            print(json.dumps(obj, indent=None, separators=(",", ":")))
        else:
            for line in text_lines:
                print(line)

    def limit(self, rows):
        if self.max_rows and len(rows) > self.max_rows:
            return rows[: self.max_rows], len(rows) - self.max_rows
        return rows, 0


def short_names(sigs):
    scope = w2i.common_scope([s.name for s in sigs]) if len(sigs) > 1 else ""
    if not scope and len(sigs) == 1:
        scope = ".".join(sigs[0].name.split(".")[:-1])
    cut = len(scope) + 1 if scope else 0
    return scope, [s.name[cut:] for s in sigs]


def text_table(header, rows):
    if not rows:
        return ["  ".join(header)]
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(header)]
    # names read better left aligned, numbers right aligned
    left = [h in ("name", "signal") for h in header]

    def line(cells):
        return "  ".join(
            str(c).ljust(w) if lft else str(c).rjust(w) for c, w, lft in zip(cells, widths, left)
        ).rstrip()

    return [line(header)] + [line(r) for r in rows]


def truncated_line(left, what="rows"):
    return "... %d more %s not shown (raise --max-rows, narrow the window, or use --format json)" % (left, what)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_info(args, wave, out):
    cands = wave.clock_candidates()
    clocks = []
    for n, s in cands[:5]:
        e = wave.edges(s)
        period = (e[-1] - e[0]) // (len(e) - 1) if len(e) > 1 else None
        clocks.append({"name": s.name, "rising_edges": n, "period": period})
    scopes = {}
    for s in wave.signals:
        parts = s.name.split(".")
        top = ".".join(parts[: min(len(parts) - 1, 3)])
        scopes[top] = scopes.get(top, 0) + 1
    obj = {
        "file": args.dump,
        "timescale": wave.timescale,
        "start_time": wave.start_time,
        "end_time": wave.end_time,
        "signals": len(wave.signals),
        "clocks": clocks,
        "scopes": scopes,
    }
    lines = [
        "file       %s" % args.dump,
        "timescale  %s" % wave.timescale,
        "time       %d .. %d" % (wave.start_time, wave.end_time),
        "signals    %d" % len(wave.signals),
    ]
    if clocks:
        lines.append("clocks (most edges first; the first is the default --clock):")
        for c in clocks:
            lines.append("  %s  edges=%d period=%s" % (c["name"], c["rising_edges"], c["period"]))
    else:
        lines.append("clocks     none found by name; pass --clock")
    lines.append("scopes (first levels, signal count):")
    for k, v in sorted(scopes.items(), key=lambda kv: -kv[1])[:15]:
        lines.append("  %-50s %d" % (k, v))
    out.emit(obj, lines)


def cmd_list(args, wave, out):
    pats = args.patterns or ["*"]
    sigs = []
    seen = set()
    for p in pats:
        if not any(c in p for c in "*?"):
            p = "*" + p + "*"
        elif not p.startswith("*"):
            p = "*" + p  # a glob names the end of the hierarchy, like -s does
        for s in wave.signals:
            if s.name not in seen and w2i._matches(s.name, p):
                seen.add(s.name)
                sigs.append(s)
    rows = [(s.name, s.width, wave.change_count(s)) for s in sigs]
    rows = [(n, w, "-" if c is None else c) for n, w, c in rows]
    shown, left = out.limit(rows)
    obj = {
        "matches": len(rows),
        "signals": [{"name": n, "width": w, "changes": c} for n, w, c in shown],
        "truncated": left,
    }
    lines = ["%d signals match" % len(rows)] + text_table(["name", "width", "changes"], shown)
    if left:
        lines.append(truncated_line(left, "signals"))
    out.emit(obj, lines)


def cmd_get(args, wave, out):
    sigs = wave.select(args.signal)
    tl = Timeline(wave, wave.pick_clock(args.clock) if (args.cycle is not None or args.clock) else None)
    if args.cycle is not None:
        t = tl.time_of(args.cycle)
        cycle = args.cycle if args.cycle >= 0 else len(tl.edges) + args.cycle
    elif args.at is not None:
        t = parse_time(args.at)
        cycle = tl.cycle_of(t) if tl.edges else None
    else:
        raise QueryError("get needs --at <time> or --cycle <n>")
    rows = [(s.name, s.width, wave.value(s, t)) for s in sigs]
    shown, left = out.limit(rows)
    obj = {
        "time": t,
        "cycle": cycle,
        "values": {n: json_value(v, w) for n, w, v in shown},
        "truncated": left,
    }
    head = "time %d" % t + ("  cycle %d" % cycle if cycle is not None else "")
    lines = [head] + text_table(
        ["name", "value"], [(n, fmt(v, w, args.radix)) for n, w, v in shown]
    )
    if left:
        lines.append(truncated_line(left, "signals"))
    out.emit(obj, lines)


def cmd_table(args, wave, out):
    sigs = wave.select(args.signal)
    clock = wave.pick_clock(args.clock)
    tl = Timeline(wave, clock)
    note = None
    if args.around:
        expr = Expr(args.around, wave)
        hits = scan(expr, tl, range(len(tl.edges)))
        hit = None
        for k, c in enumerate(hits):
            if k == args.nth:
                hit = c
                break
        if hit is None:
            raise QueryError("--around %r never holds (match #%d)" % (args.around, args.nth))
        cycles = range(max(0, hit - args.before), min(len(tl.edges), hit + args.after + 1))
        note = "match of %r at cycle %d (time %d)" % (args.around, hit, tl.edges[hit])
        marker = hit
    else:
        t0, t1 = time_window(args, wave, tl)
        cycles = tl.cycle_range(t0, t1)
        marker = None
    where = Expr(args.where, wave) if args.where else None
    selected = list(scan(where, tl, cycles)) if where else list(cycles)

    rows = []
    prev = None
    for c in selected:
        t = tl.edges[c]
        vals = [wave.value(s, t) for s in sigs]
        if args.changes_only and prev is not None and vals == prev:
            continue
        prev = vals
        rows.append((c, t, vals))
    shown, left = out.limit(rows)
    scope, names = short_names(sigs)
    obj = {
        "clock": clock.name,
        "scope": scope,
        "signals": [s.name for s in sigs],
        "match_cycle": marker,
        "rows": [
            {"cycle": c, "time": t, "values": [json_value(v, s.width) for v, s in zip(vals, sigs)]}
            for c, t, vals in shown
        ],
        "truncated": left,
    }
    lines = ["clock %s" % clock.name]
    if scope:
        lines.append("scope %s" % scope)
    if note:
        lines.append(note)
    trows = []
    for c, t, vals in shown:
        mark = ">" if c == marker else ""
        trows.append([mark + str(c), t] + [fmt(v, s.width, args.radix) for v, s in zip(vals, sigs)])
    lines += text_table(["cycle", "time"] + names, trows)
    if left:
        lines.append(truncated_line(left))
    out.emit(obj, lines)


def cmd_changes(args, wave, out):
    sigs = wave.select(args.signal)
    try:
        clk = wave.pick_clock(args.clock)
    except QueryError:
        if args.clock or args.cycles:
            raise
        clk = None  # plain time window, no cycle column
    tl = Timeline(wave, clk)
    t0, t1 = time_window(args, wave, tl)
    events = []
    for s in sigs:
        before = wave.value(s, t0 - 1) if t0 > wave.start_time else None
        for t, v in wave.changes(s, t0, t1):
            if before is not None and v == before:
                continue
            events.append((t, s, before, v))
            before = v
    events.sort(key=lambda e: (e[0], e[1].index))
    shown, left = out.limit(events)
    scope, _ = short_names(sigs)
    cut = len(scope) + 1 if scope else 0
    obj = {
        "scope": scope,
        "events": [
            {
                "time": t,
                "cycle": tl.cycle_of(t),
                "signal": s.name,
                "from": None if b is None else json_value(b, s.width),
                "to": json_value(v, s.width),
            }
            for t, s, b, v in shown
        ],
        "total": len(events),
        "truncated": left,
    }
    lines = ["%d changes in time %d..%d" % (len(events), t0, t1)]
    if scope:
        lines.append("scope %s" % scope)
    lines += text_table(
        ["time", "cycle", "signal", "from", "to"],
        [
            (
                t,
                "" if tl.cycle_of(t) is None else tl.cycle_of(t),
                s.name[cut:],
                "" if b is None else fmt(b, s.width, args.radix),
                fmt(v, s.width, args.radix),
            )
            for t, s, b, v in shown
        ],
    )
    if left:
        lines.append(truncated_line(left, "changes"))
    out.emit(obj, lines)


def cmd_find(args, wave, out):
    clock = wave.pick_clock(args.clock)
    tl = Timeline(wave, clock)
    expr = Expr(args.expr, wave)
    t0, t1 = time_window(args, wave, tl)
    cycles = tl.cycle_range(t0, t1)
    show = wave.select(args.signal) if args.signal else []
    # by default show the signals the expression itself names
    if not show:
        show = list({s.code: s for s in expr.signals.values()}.values())
    hits = []
    total = 0
    limit = 1 if args.first else (args.max_rows or None)
    for c in scan(expr, tl, cycles):
        total += 1
        if not args.count and (limit is None or len(hits) < limit):
            hits.append(c)
        if args.first:
            break
    left = total - len(hits)
    scope, names = short_names(show)
    obj = {
        "expr": args.expr,
        "clock": clock.name,
        "count": total,
        "hits": [
            {
                "cycle": c,
                "time": tl.edges[c],
                "values": {s.name: json_value(wave.value(s, tl.edges[c]), s.width) for s in show},
            }
            for c in ([] if args.count else hits)
        ],
        "truncated": 0 if (args.count or args.first) else left,
    }
    if args.count:
        lines = ["%d cycles match %s" % (total, args.expr)]
    elif not hits:
        lines = ["no cycle matches %s (clock %s, cycles %d..%d)"
                 % (args.expr, clock.name, cycles.start, max(cycles.start, cycles.stop - 1))]
    else:
        lines = ["%s: %s" % ("first match" if args.first else "%d matches" % total, args.expr)]
        if scope:
            lines.append("scope %s" % scope)
        rows = [
            [c, tl.edges[c]] + [fmt(wave.value(s, tl.edges[c]), s.width, args.radix) for s in show]
            for c in hits
        ]
        lines += text_table(["cycle", "time"] + names, rows)
        if left and not args.first:
            lines.append(truncated_line(left, "matches"))
    out.emit(obj, lines)


# ---------------------------------------------------------------------------
# Self test
# ---------------------------------------------------------------------------


def run_selftest():
    import contextlib
    import io

    tmp = tempfile.mkdtemp(prefix="wavequery_selftest_")
    path = os.path.join(tmp, "selftest.vcd")
    w2i.write_selftest_vcd(path)
    os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cache")

    def run(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    failures = 0

    cases = []

    def check(label, argv, test, record=True):
        nonlocal failures
        if record:
            cases.append((label, argv, test))
        rc, text = run(argv)
        ok = False
        try:
            ok = rc == 0 and test(text)
        except Exception as exc:  # noqa: BLE001
            text += "\n(exception: %r)" % exc
        print("%-4s %s" % ("ok" if ok else "FAIL", label))
        if not ok:
            failures += 1
            print("     argv: %s\n     output:\n%s" % (argv, text))

    check("info finds the clock", ["info", path], lambda t: "dut.clk" in t)
    check("list filters", ["list", path, "req"], lambda t: "dut.req" in t and "1 signals" in t)
    check("get by cycle", ["get", path, "--cycle", "5", "-s", "data", "--format", "json"],
          lambda t: json.loads(t)["values"]["tb_wave.dut.data"] == 0xBEEF)
    check("find first rose(req)", ["find", path, "rose(req)", "--first", "--format", "json"],
          lambda t: json.loads(t)["hits"][0]["cycle"] == 2)
    check("find count", ["find", path, "req && data != 0", "--count", "--format", "json"],
          lambda t: json.loads(t)["count"] == 2)
    check("braced names and C ops", ["find", path, "{dut.req} == 1 || !{dut.req} && data == 0xdead",
                                     "--format", "json"],
          lambda t: [h["cycle"] for h in json.loads(t)["hits"]] == [2, 4, 7, 8, 13])
    check("unknown never compares", ["find", path, "state == 0", "--format", "json"],
          lambda t: all(h["cycle"] != 6 for h in json.loads(t)["hits"]))
    check("isx", ["find", path, "isx(state)", "--cycles", "3:", "--format", "json"],
          lambda t: [h["cycle"] for h in json.loads(t)["hits"]] == [6])
    check("table around", ["table", path, "-s", "req", "-s", "data", "--around", "fell(req)", "--nth", "1",
                           "-B", "1", "-A", "1", "--format", "json"],
          lambda t: [r["cycle"] for r in json.loads(t)["rows"]] == [8, 9, 10])
    check("table changes-only", ["table", path, "-s", "req", "--changes-only", "--format", "json"],
          lambda t: [r["values"][0] for r in json.loads(t)["rows"]] == [0, 1, 0, 1, 0])
    check("changes window", ["changes", path, "-s", "req", "--cycles", "0:10", "--format", "json"],
          lambda t: [e["cycle"] for e in json.loads(t)["events"]] == [0, 2, 3, 7, 9])
    check("max-rows truncates", ["table", path, "-s", "data", "--max-rows", "3"],
          lambda t: "more rows not shown" in t)
    check("cache is reused", ["info", path], lambda t: os.listdir(os.path.join(tmp, "cache", "scariv", "wavequery")))

    rc, text = run(["get", path, "--cycle", "1", "-s", "nosuch"])
    ok = rc == 2 and "no signal named" in text
    print("%-4s %s" % ("ok" if ok else "FAIL", "unknown name is a clear error"))
    failures += 0 if ok else 1

    # the same queries on an FST, which is read lazily (tools/wavesource.py)
    conv = w2i.build_fst_converter(verbose=False)
    if conv is None:
        print("skip FST checks: the FST tools cannot be built here (needs Verilator's FST library)")
    else:
        import subprocess

        fst = os.path.join(tmp, "selftest.fst")
        subprocess.run([conv, "--vcd2fst", path, "-o", fst], check=True, capture_output=True)
        for label, argv, test in cases:
            if "cache" in label:
                continue
            check("fst: " + label, [fst if x == path else x for x in argv], test, record=False)
        w = load(fst)
        check("fst: read lazily", ["info", fst], lambda t: getattr(w, "lazy", False), record=False)
        w.close()

    print("selftest: %s" % ("PASS" if failures == 0 else "%d FAILED" % failures))
    return 0 if failures == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    common.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                        help="cap on printed rows, 0 for no cap (default %(default)s)")
    common.add_argument("--radix", choices=("hex", "dec", "bin"), default="hex", help="bus value radix (text)")
    common.add_argument("--clock", help="clock signal (default: auto detect)")

    window = argparse.ArgumentParser(add_help=False)
    window.add_argument("--cycles", help="cycle window START:END (inclusive, negative counts from the end)")
    window.add_argument("--from", dest="time_from", help="window start time")
    window.add_argument("--to", dest="time_to", help="window end time")

    p = argparse.ArgumentParser(
        prog="wavequery",
        description=__doc__.split("\n")[0],
        epilog=__doc__.split("\n", 2)[2],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--selftest", action="store_true", help="run the built-in self test")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("info", parents=[common], help="summary of the dump")
    s.add_argument("dump")

    s = sub.add_parser("list", parents=[common], help="list signals")
    s.add_argument("dump")
    s.add_argument("patterns", nargs="*", help="glob over full names; plain text means *text*")

    s = sub.add_parser("get", parents=[common], help="values at one time or cycle")
    s.add_argument("dump")
    s.add_argument("-s", "--signal", action="append", required=True)
    s.add_argument("--at", help="time")
    s.add_argument("--cycle", type=int, help="cycle number (negative counts from the end)")

    s = sub.add_parser("table", parents=[common, window], help="cycle by cycle table")
    s.add_argument("dump")
    s.add_argument("-s", "--signal", action="append", required=True)
    s.add_argument("--around", help="center the window on the cycle where this expression holds")
    s.add_argument("--nth", type=int, default=0, help="which match of --around (0 based)")
    s.add_argument("-B", "--before", type=int, default=5, help="cycles before the --around match")
    s.add_argument("-A", "--after", type=int, default=10, help="cycles after the --around match")
    s.add_argument("--where", help="only rows where this expression holds")
    s.add_argument("--changes-only", action="store_true", help="drop rows identical to the previous row")

    s = sub.add_parser("changes", parents=[common, window], help="value changes in a window")
    s.add_argument("dump")
    s.add_argument("-s", "--signal", action="append", required=True)

    s = sub.add_parser("find", parents=[common, window], help="cycles where an expression holds")
    s.add_argument("dump")
    s.add_argument("expr")
    s.add_argument("-s", "--signal", action="append", help="extra signals to print at each match")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--first", action="store_true", help="stop at the first match")
    g.add_argument("--count", action="store_true", help="only count matches")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return run_selftest()
    if not args.cmd:
        parser.print_help()
        return 2
    try:
        wave = load(args.dump)
        out = Out(args)
        {
            "info": cmd_info,
            "list": cmd_list,
            "get": cmd_get,
            "table": cmd_table,
            "changes": cmd_changes,
            "find": cmd_find,
        }[args.cmd](args, wave, out)
    except QueryError as exc:
        if getattr(args, "format", "text") == "json":
            print(json.dumps({"error": str(exc)}))
        else:
            print("error: %s" % exc)
        return 2
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
