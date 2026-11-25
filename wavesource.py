"""Lazy waveform sources: dumps that are never loaded as a whole.

LazyFst talks to fstsrv.c, a helper process that opens an FST, sends
the hierarchy and then answers reads for chosen signals over chosen time
windows. Nothing is converted and nothing is read until a view, a query or
a value at the cursor needs it, so the size of the dump does not matter:
opening costs the hierarchy, a view costs the blocks it overlaps.

It offers the same interface as wavequery.Wave (the in-memory reader used
for VCD), which is what wavequery and waveview program against:

    signals, by_name, timescale, start_time, end_time
    resolve() select()                    names (inherited)
    value(sig, t)  values_at(sigs, t)     value held at t (after changes at t)
    changes(sig, t0, t1)                  [(t, bits)] real changes in [t0, t1]
    next_change(sig, t, "next"|"prev")
    buckets(sigs, ts, level, n)           per bucket summaries, for tiles
    edges(clock)                          rising edges (arithmetic when regular)
    clock_candidates() change_count(sig)

A dump with tens of millions of signals takes minutes just to list. Two
ways around that: open_lazy(path, scope=[...]) has the server list only what
is below some scopes, and a local FSDB too large to list keeps its names in
an index on disk (nameindex.py): signals and by_name are then looked up
there, and the server reads only the scopes of the dump.

Values are fetched in aligned chunks and cached, so stepping through cycles
(find, table) costs one read per chunk per signal, not one per cycle.
"""

from __future__ import annotations

import bisect
import collections
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nameindex  # noqa: E402
import wave2img as w2i  # noqa: E402
import wavequery as wq  # noqa: E402

CHUNK_CACHE = 4096  # (signal, chunk) entries


class ArithEdges:
    """The rising edges of a regular clock, as a sequence: first + k*period.
    Works with len(), indexing, slicing and bisect like a list."""

    def __init__(self, first, period, count):
        self.first, self.period, self.count = first, period, count

    def __len__(self):
        return self.count

    def __getitem__(self, k):
        if isinstance(k, slice):
            return [self[i] for i in range(*k.indices(self.count))]
        if k < 0:
            k += self.count
        if not 0 <= k < self.count:
            raise IndexError(k)
        return self.first + k * self.period

    def __iter__(self):
        return (self.first + k * self.period for k in range(self.count))


class TooManySignals(wq.QueryError):
    """A dump with more signals than can be listed at once; `scopes` are
    the first levels of its hierarchy with their signal counts."""

    def __init__(self, path, built):
        self.path = path
        self.signals = built["signals"]
        self.scope_count = built["scope_count"]
        self.scopes = sorted(built["scopes"].items(), key=lambda kv: -kv[1])
        top = ", ".join("%s (%s)" % (s, _count(n)) for s, n in self.scopes[:8])
        super().__init__("%s has %s signals in %s scopes: too many to list at once. Open a part of it with "
                         "--scope A.B.C (the Scope field of Open… in the viewer; WAVE_INDEX_MIN raises the limit). "
                         "Largest first levels: %s" % (os.path.basename(path.rstrip("/")), _count(self.signals),
                                                       _count(self.scope_count), top or "none"))


def _count(n):
    return "{:,}".format(n)


class LazyFst(wq.Wave):
    """A dump read in place through a wave server process: fstsrv.c for FST,
    fsdbsrv.cc for FSDB (the same protocol)."""

    lazy = True

    def __init__(self, path, argv=None, tool="fstsrv", scope=None, plan=None, make_argv=None):
        """scope: list only the signals below these scopes. plan: a
        nameindex.Plan, for a dump whose names are kept on disk when there
        are too many to hold (fsdbsrv only). make_argv(extra): the command
        line with extra server options added, where argv is not a plain
        local one (over ssh the options must go inside the remote command)."""
        if argv is None:
            binary = w2i.build_fst_tool("fstsrv", verbose=True)
            if binary is None:
                raise wq.QueryError("cannot build fstsrv.c (needs a C compiler and Verilator's FST library)")
            argv = [binary, path] + scope_args(scope)
        if make_argv is None:
            make_argv = lambda extra, base=argv: base + list(extra)  # noqa: E731
        self.path = path
        self.tool = tool
        self.names = None
        meta = plan.ready() if plan is not None else None
        if meta is not None:
            argv = make_argv(["--no-tree", str(meta["scope_count"])])
        elif plan is not None:
            argv = make_argv(["--index", plan.index_arg(), "--index-min", str(nameindex.min_signals())])
        else:
            argv = make_argv([])
        try:
            self._start(path, argv, tool, scope, plan, meta)
        except BaseException:
            proc = getattr(self, "proc", None)
            if proc is not None and proc.poll() is None:  # do not leave the server behind
                proc.kill()
                proc.wait()
            if plan is not None:
                plan.discard()
                if meta is not None:
                    plan.remove()  # the index did not work: the next open writes it again
            raise

    def _start(self, path, argv, tool, scope, plan, meta):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1 << 16, encoding="ascii", errors="replace")
        self.io = threading.Lock()
        head = self.proc.stdout.readline().strip()
        if not head and self.proc.poll() is not None:
            raise wq.QueryError("the wave server for %s exited (%d)" % (path, self.proc.returncode))
        if head != "wavesrv 1":
            raise wq.QueryError("%s could not open %s: %s" % (tool, path, head))
        dump = w2i.Dump()
        exp = 0
        width_of = {}
        ended = False
        built = None
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line == "end":
                ended = True
                break
            kind, _, rest = line.partition(" ")
            if kind == "!":
                raise wq.QueryError("%s: %s" % (tool, rest.strip()))
            if kind == "timescale":
                exp = int(rest)
            elif kind == "range":
                a, b = rest.split()
                dump.start_time, dump.end_time = int(a), int(b)
            elif kind == "var":
                h, w, name = rest.split(" ", 2)
                h, w = int(h), int(w)
                width_of[h] = w
                # after a tab: the declared type and port direction ("-" when unknown)
                name, _, tk = name.partition("\t")
                vtype, _, direction = tk.partition(" ")
                dump.signals.append(w2i.Signal(name, w, h, len(dump.signals),
                                               vtype if vtype and vtype != "-" else "",
                                               direction if direction and direction != "-" else ""))
            elif kind == "vk":  # an older server: the type and direction on a line of their own
                h, vtype, direction = rest.split(" ", 2)
                if dump.signals and dump.signals[-1].code == int(h):
                    dump.signals[-1].vtype = vtype if vtype != "-" else ""
                    dump.signals[-1].direction = direction.strip() if direction.strip() != "-" else ""
            elif kind == "index":  # the names went to the plan's file
                built = {"signals": int(rest.split()[0]), "scope_count": int(rest.split()[1]), "scopes": {},
                         "clocks": []}
            elif kind == "scope" and built is not None:
                n, _, name = rest.partition(" ")
                built["scopes"][name] = built["scopes"].get(name, 0) + int(n)
            elif kind == "clock" and built is not None:
                h, o, name = rest.split(" ", 2)
                built["clocks"].append([int(h), int(o), name])
        if not ended:
            raise wq.QueryError("%s stopped while listing the signals of %s" % (tool, path))
        if built is not None and plan is None:
            # too many signals to list and nowhere to index them (the viewer,
            # or a dump on another machine): say so, with the scopes to pick from
            raise TooManySignals(path, built)
        if built is not None:
            plan.fetch()  # a no-op for a local plan; brings the file from the server for a remote one
            meta = plan.publish(built)
        elif plan is not None:
            plan.discard()
        if scope and not dump.signals:
            raise wq.QueryError("no signal below the scope %s in %s\nhint: `info` without --scope shows the first "
                                "levels of the hierarchy" % (", ".join(scope), path))
        dump.timescale = _timescale(exp)
        if dump.end_time <= dump.start_time:
            dump.end_time = dump.start_time + 1
        super().__init__(dump)
        self.width_of = width_of
        if meta is not None:
            # too many signals to hold: names are looked up in the index
            self.names = nameindex.NameIndex(plan.dir, meta)
            self.signals = nameindex.Signals(self.names)
            self.by_name = nameindex.ByName(self.names)
            self.width_of = self.names.width_of
            # scope numbers the server has read the variables of: all of them
            # when it has just listed the dump to write the index
            self.declared = None if built is not None else set()
        span = max(1, (self.end_time - self.start_time) // 65536)
        self.chunk = 1 << max(10, span.bit_length())
        self.cache = collections.OrderedDict()  # (code, k) -> (prior, times, values)

    def close(self):
        if self.names is not None:
            self.names.close()
        try:
            with self.io:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        self.proc.wait(timeout=5)

    def __del__(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def stamp(self):
        """(size, mtime) of the dump, to notice when the simulation rewrote it."""
        if not getattr(self, "remote", None):
            st = os.stat(self.path)
            return (st.st_size, st.st_mtime_ns)
        out = self._ask("stat")
        _, size, mtime = out[0].split()
        return (int(size), int(mtime))

    # -- protocol -------------------------------------------------------------

    def _ask(self, cmd):
        with self.io:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            out = []
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                if line == ".":
                    return out
                if line.startswith("!"):
                    raise wq.QueryError("%s: %s" % (self.tool, line[1:].strip()))
                out.append(line)
            raise wq.QueryError("%s stopped (dump %s)" % (self.tool, self.path))

    def _handles(self, codes):
        """The handles of a command. A server that read only the scopes (the
        names are in the index) is first told which scopes declare them."""
        if self.names is not None and self.declared is not None:
            need = sorted({self.names.scope_of.get(c, 0) for c in codes} - self.declared - {0})
            if need:
                self._ask("vars " + " ".join(str(o) for o in need))
                self.declared.update(need)
        return " ".join(str(c) for c in codes)

    def _read(self, codes, t0, t1):
        """{code: (prior, [times], [values])} for [t0, t1]."""
        out = {c: [("x" * self.width_of.get(c, 1)), [], []] for c in codes}
        if not codes:
            return out
        for line in self._ask("read %d %d %s" % (t0, t1, self._handles(codes))):
            if line[0] == "c":
                _, t, h, v = line.split(" ")
                e = out[int(h)]
                e[1].append(int(t))
                e[2].append(v)
            elif line[0] == "p":
                _, h, v = line.split(" ")
                out[int(h)][0] = v
        return out

    # -- chunk cache ----------------------------------------------------------

    def _chunks(self, sigs, k0, k1):
        """Make sure chunks k0..k1 of these signals are cached."""
        codes = sorted({s.code for s in sigs})
        missing = [c for c in codes if any((c, k) not in self.cache for k in range(k0, k1 + 1))]
        if not missing:
            for c in codes:
                for k in range(k0, k1 + 1):
                    self.cache.move_to_end((c, k))
            return
        cs = self.chunk
        data = self._read(missing, k0 * cs, (k1 + 1) * cs - 1)
        for c, (prior, times, values) in data.items():
            i = 0
            for k in range(k0, k1 + 1):
                end = (k + 1) * cs
                j = bisect.bisect_left(times, end, i)
                self.cache[(c, k)] = (prior, times[i:j], values[i:j])
                if j > i:
                    prior = values[j - 1]
                i = j
        while len(self.cache) > CHUNK_CACHE:
            self.cache.popitem(last=False)

    def _chunk(self, sig, t):
        k = max(0, int(t)) // self.chunk
        if (sig.code, k) not in self.cache:
            # read ahead: stepping through time is the common pattern
            self._chunks([sig], k, k + 3)
        else:
            self.cache.move_to_end((sig.code, k))
        return self.cache[(sig.code, k)]

    # -- the Wave interface ---------------------------------------------------

    def value(self, sig, t):
        prior, times, values = self._chunk(sig, t)
        i = bisect.bisect_right(times, t) - 1
        return w2i.normalize(values[i] if i >= 0 else prior, sig.width)

    def values_at(self, sigs, t):
        """Values of many signals at t in one round trip."""
        k = max(0, int(t)) // self.chunk
        need = [s for s in sigs if (s.code, k) not in self.cache]
        if need:
            self._chunks(need, k, k)
        return {s.name: self.value(s, t) for s in sigs}

    def changes(self, sig, t0, t1):
        t0, t1 = int(max(t0, 0)), int(t1)
        k0, k1 = t0 // self.chunk, t1 // self.chunk
        if k1 - k0 > 256:  # a long window: read it without filling the cache
            prior, times, values = self._read([sig.code], t0, t1)[sig.code]
            return [(t, w2i.normalize(v, sig.width)) for t, v in zip(times, values)]
        self._chunks([sig], k0, k1)
        out = []
        for k in range(k0, k1 + 1):
            _, times, values = self.cache[(sig.code, k)]
            for t, v in zip(times, values):
                if t0 <= t <= t1:
                    out.append((t, w2i.normalize(v, sig.width)))
        return out

    def next_change(self, sig, t, direction):
        cur = self.value(sig, t)
        if direction == "next":
            lo, span = int(t) + 1, self.chunk
            while lo <= self.end_time:
                for tt, v in self.changes(sig, lo, lo + span - 1):
                    if v != cur:
                        return tt
                lo += span
                span *= 4
            return None
        # backwards: the start of the run of values equal to the one just before t
        before = self.value(sig, t - 1) if t > self.start_time else None
        if before is None:
            return None
        hi, span = int(t) - 1, self.chunk
        while hi >= self.start_time:
            lo = max(self.start_time, hi - span + 1)
            ch = self.changes(sig, lo, hi)
            for k in range(len(ch) - 1, -1, -1):
                tt, v = ch[k]
                prev = ch[k - 1][1] if k > 0 else self.value(sig, lo - 1) if lo > self.start_time else None
                if v == before and prev != before:
                    return tt if prev is not None else None
            hi = lo - 1
            span *= 4
        return None

    def buckets(self, sigs, ts, level, n):
        codes = sorted({s.code for s in sigs})
        res = {c: ["x" * self.width_of.get(c, 1), []] for c in codes}
        if codes:
            for line in self._ask("agg %d %d %d %s" % (ts, level, n, self._handles(codes))):
                f = line.split(" ")
                if f[0] == "b":
                    res[int(f[1])][1].append((int(f[2]), int(f[3]), int(f[4]), int(f[5]), int(f[6]), f[7]))
                elif f[0] == "p":
                    res[int(f[1])][0] = f[2]
        return {s.code: (w2i.normalize(res[s.code][0], s.width),
                         [(b, a, z, cnt, one, w2i.normalize(v, s.width)) for b, a, z, cnt, one, v in res[s.code][1]])
                for s in sigs}

    def change_count(self, sig):
        return None  # unknown without reading the whole signal

    def edges(self, sig):
        if sig.code in self._edges:
            return self._edges[sig.code]
        self._edges[sig.code] = e = self._find_edges(sig)
        return e

    def _rising(self, sig, t0, t1):
        prev = self.value(sig, t0 - 1) if t0 > self.start_time else None
        out = []
        for t, v in self.changes(sig, t0, t1):
            if v == "1" and prev != "1":
                out.append(t)
            prev = v
        return out

    def _find_edges(self, sig):
        """Rising edges. A clock whose first edges are evenly spaced and whose
        last edges sit on the same grid is taken as regular and described
        arithmetically; anything else is read in full."""
        start, end = self.start_time, self.end_time
        span = self.chunk
        if getattr(self, "tool", "") == "fsdbsrv":
            # the FSDB reader loads a signal whole for any read, so a window
            # saves nothing there: one read of all the edges instead of a
            # widening series of round trips (each a load) for a slow clock
            span = end - start + 1
        while True:  # widen the window until it holds 64 edges or the dump
            first = self._rising(sig, start, min(end, start + span))
            if len(first) >= 64 or start + span >= end:
                break
            span *= 4
        if len(first) >= 3:
            p = first[1] - first[0]
            if p > 0 and all(first[i + 1] - first[i] == p for i in range(len(first) - 1)):
                count = (end - first[0]) // p + 1
                tail0 = max(first[0], first[0] + (count - 64) * p)
                tail = self._rising(sig, tail0, end)
                if tail and all((t - first[0]) % p == 0 for t in tail) and tail[-1] == first[0] + (count - 1) * p:
                    return ArithEdges(first[0], p, count)
                if tail and all((t - first[0]) % p == 0 for t in tail):
                    return ArithEdges(first[0], p, (tail[-1] - first[0]) // p + 1)
        if len(first) < 64 and start + span >= end:
            return first
        sys.stderr.write("wavesource: %s is not a regular clock, reading all of its edges\n" % sig.name)
        return self._rising(sig, start, end)

    def clock_candidates(self):
        by_code = {}
        for s in self.signals if self.names is None else self.names.clocks():
            if s.width != 1:
                continue
            leaf = s.name.split(".")[-1].lower()
            if "clk" in leaf or "clock" in leaf:
                if s.code not in by_code or len(s.name) < len(by_code[s.code].name):
                    by_code[s.code] = s
        cands = sorted(by_code.values(), key=lambda s: len(s.name))[:6]
        out = [(len(self.edges(s)), s) for s in cands]
        out.sort(key=lambda c: (-c[0], len(c[1].name)))
        return out


def _timescale(exp):
    units = {0: "s", -3: "ms", -6: "us", -9: "ns", -12: "ps", -15: "fs"}
    for base, unit in units.items():
        if base - 3 < exp <= base:
            return "%d%s" % (10 ** (exp - base), unit)
    return "1e%ds" % exp


class RemoteFst(LazyFst):
    """An FST or FSDB on another machine, read through fstsrv or fsdbsrv
    running there, over ssh (remote.py)."""

    def __init__(self, spec, tool="fstsrv", scope=None, extra=(), index=False):
        """index: a dump with too many signals to list keeps its names in
        an index written over there and brought here once (RemotePlan)."""
        import remote

        r, path = remote.parse(spec)
        self.remote = r
        plan = None
        if index and tool == "fsdbsrv" and not scope and nameindex.enabled() and not os.environ.get("WAVEVIEW_REMOTE_FSDBSRV"):
            try:
                plan = nameindex.RemotePlan(r, path)
                extra = ()
            except (remote.RemoteError, ValueError, IndexError) as exc:
                sys.stderr.write("wavesource: no name index for %s (%s)\n" % (spec, exc))
        base = scope_args(scope) + list(extra)
        for attempt in (0, 1):
            try:
                super().__init__(spec, argv=r.srv_argv(path, tool, base), tool=tool, scope=scope, plan=plan,
                                 make_argv=lambda more: r.srv_argv(path, tool, base + list(more)))
                return
            except remote.RemoteError as exc:
                raise wq.QueryError(str(exc))
            except TooManySignals:
                raise
            except wq.QueryError:
                if attempt:
                    raise
                # the remembered binary may be gone (a cleaned cache): build again
                r.forget_srv(tool)


def scope_args(scope):
    out = []
    for s in scope or ():
        out += ["--scope", s]
    return out


def open_lazy(path, scope=None, index=True):
    """A lazy source for path, or None when this format has none here.
    scope: only the signals below these scopes are listed. index: a dump
    with very many signals keeps its names on disk (nameindex.py)."""
    import remote

    ext = os.path.splitext(path)[1].lower()
    if remote.is_remote(path):
        if ext == ".fst":
            return RemoteFst(path, scope=scope)
        if ext == ".fsdb":
            # with index: a dump with too many signals to list gets a name
            # index written over there and brought here; without: it stops
            # short, naming its scopes
            return RemoteFst(path, tool="fsdbsrv", scope=scope, extra=limit_args(scope) if (scope or not index) else (),
                             index=index)
        raise wq.QueryError("remote dumps are served for FST and FSDB only (convert or dump FST on the server)")
    if os.environ.get("WAVE_EAGER") == "1":
        return None
    if ext == ".fst":
        return LazyFst(path, scope=scope)
    if ext == ".fsdb":
        binary = w2i.build_fsdb_tool(verbose=True)
        if binary is not None:
            argv = [binary, path] + scope_args(scope)
            if index and not scope and nameindex.enabled() and not os.environ.get("WAVEVIEW_FSDBSRV"):
                try:
                    return LazyFst(path, argv=argv, tool="fsdbsrv", plan=nameindex.Plan(path))
                except TooManySignals:
                    raise
                except (OSError, ValueError, wq.QueryError) as exc:
                    sys.stderr.write("wavesource: no name index for %s (%s), listing the signals\n" % (path, exc))
            return LazyFst(path, argv=argv + limit_args(scope), tool="fsdbsrv", scope=scope)
    return None


def limit_args(scope):
    """fsdbsrv options that make a dump with too many signals to list say
    so (with its scope summary) instead of listing: the names would go to
    an index file, here nowhere. With a scope, a cap on the listing: a
    scope can still hold millions."""
    if os.environ.get("WAVEVIEW_FSDBSRV") or not nameindex.enabled():
        return []
    if scope:
        return ["--list-max", str(nameindex.min_signals())]
    return ["--index", "/dev/null", "--index-min", str(nameindex.min_signals())]
