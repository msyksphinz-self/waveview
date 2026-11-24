"""The names of a very large dump, looked up on disk instead of held in memory.

A dump with tens of millions of signals takes minutes to list and gigabytes
to hold as Python objects, although a query names a handful of them. For
such a dump fsdbsrv writes the scope tree to a file once (the format is
described in fsdbsrv.cc) and this module answers the name questions from
that file, memory mapped:

    exact(name)            the signal with this full name
    suffix(name)           the signals whose name ends with .name
    glob(pattern, limit)   the signals matching a * ? pattern, and how many
    suggest(name)          names with a similar leaf, for error messages
    iter_all()             every signal, in dump order (slow: the whole file)

The file is searched for a literal piece of the name with mmap.find, which
runs at memory speed, and only the lines found are looked at in Python. A
pattern with no literal piece, or one that names a scope near the top, still
walks most of the file.

The index is kept under ~/.cache/waveview/index (WAVE_INDEX_DIR moves it),
keyed on the dump's path, size and modification time. About 25 bytes per
signal: 2 GB for 84 million signals.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wave2img as w2i  # noqa: E402

MAGIC = b"waveview-names 1\n"
VERSION = 1
DEFAULT_MIN = 1000000  # variable idcodes: below this a dump is listed in memory
_STRUCT = re.compile(rb"^(?:([+*])(.*)|-)$", re.M)


def enabled():
    return os.environ.get("WAVE_INDEX", "1") != "0"


def min_signals():
    try:
        return int(os.environ.get("WAVE_INDEX_MIN", DEFAULT_MIN))
    except ValueError:
        return DEFAULT_MIN


def base_dir():
    d = os.environ.get("WAVE_INDEX_DIR")
    if d:
        return d
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "waveview", "index")


class Plan:
    """Where the index of a dump is, and whether it is there already."""

    def __init__(self, path):
        real = os.path.realpath(path)
        st = os.stat(real)
        self.dump = real
        self.stamp = [st.st_size, st.st_mtime_ns]
        key = "%s|%d|%d|%d" % (real, st.st_size, st.st_mtime_ns, VERSION)
        self.dir = os.path.join(base_dir(), hashlib.sha1(key.encode()).hexdigest()[:20])
        self.tmp = "%s.%d.tmp" % (self.dir, os.getpid())

    def ready(self):
        """The meta data of a complete index, or None."""
        try:
            with open(os.path.join(self.dir, "meta.json")) as fh:
                meta = json.load(fh)
            if meta.get("version") == VERSION and os.path.getsize(os.path.join(self.dir, "names")) == meta["bytes"]:
                return meta
        except (OSError, ValueError, KeyError):
            pass
        return None

    def names_tmp(self):
        """The file the wave server writes the names to."""
        os.makedirs(self.tmp, exist_ok=True)
        return os.path.join(self.tmp, "names")

    def publish(self, meta):
        """Make the index just written the one that is found next time."""
        meta = dict(meta, version=VERSION, dump=self.dump, stamp=self.stamp,
                    bytes=os.path.getsize(os.path.join(self.tmp, "names")))
        with open(os.path.join(self.tmp, "meta.json"), "w") as fh:
            json.dump(meta, fh)
        try:
            if os.path.isdir(self.dir):  # an incomplete one, or one another process just wrote
                if self.ready() is not None:
                    self.discard()
                    return self.ready()
                shutil.rmtree(self.dir, ignore_errors=True)
            os.rename(self.tmp, self.dir)
        except OSError:
            if self.ready() is None:
                raise
            self.discard()
        return self.ready()

    def discard(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def remove(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _rx(pattern):
    """The regular expression of wave2img._matches: only * and ? are wild."""
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern), re.S)


class NameIndex:
    def __init__(self, dirpath, meta):
        self.dir = dirpath
        self.count = int(meta["signals"])
        self.scopes = dict(meta.get("scopes") or {})  # first levels -> signals
        self.scope_count = int(meta["scope_count"])
        self.clock_names = [(int(c), int(o), n) for c, o, n in meta.get("clocks") or []]
        self.fh = open(os.path.join(dirpath, "names"), "rb")
        self.m = mmap.mmap(self.fh.fileno(), 0, access=mmap.ACCESS_READ)
        if self.m[: len(MAGIC)] != MAGIC:
            raise ValueError("%s is not a waveview name index" % dirpath)
        self.width_of = {}  # code -> width, of the signals handed out
        self.scope_of = {}  # code -> number of the scope that declares it
        self._exact = {}

    def close(self):
        try:
            self.m.close()
            self.fh.close()
        except (OSError, ValueError):
            pass

    # -- reading the file -----------------------------------------------------
    #
    # +name  a scope (the n-th such line is scope number n)   *name  a struct
    # -      the end of either                                id width name
    # @n<TAB>c<TAB>names...   in scope number n, c scopes so far, the names in
    #        full (a struct's with a *): before every run of variables that
    #        does not follow a + or *, and about every 16 KB

    def _signal(self, code, width, name, at, scope):
        self.width_of[code] = width
        self.scope_of[code] = scope
        return w2i.Signal(name, width, code, at)

    def _line(self, ls):
        """(line, offset of the next line) for the line starting at ls."""
        le = self.m.find(b"\n", ls)
        if le < 0:
            le = len(self.m)
        return self.m[ls:le], le + 1

    def _place(self, at):
        """Where the line starting at `at` is: (names of the scopes and
        structs around it, their scope numbers, scopes entered so far)."""
        m = self.m
        a = m.rfind(b"\n@", 0, at + 1)
        e = m.find(b"\n", a + 1)
        f = m[a + 2:e].decode("latin-1").split("\t")
        base, count = int(f[0]), int(f[1])
        names = [n.lstrip("*") for n in f[2:]]
        # only the innermost number is known here, and no line that needs
        # another one comes before the next "@"
        ords = [base] * len(names)
        for mt in _STRUCT.finditer(m[e + 1:at]):
            kind = mt.group(1)
            if kind is None:
                if names:
                    names.pop()
                    ords.pop()
                continue
            if kind == b"+":
                count += 1
            names.append(mt.group(2).decode("latin-1"))
            ords.append(count if kind == b"+" else ords[-1] if ords else base)
        return names, ords, count

    def _parent(self, ls):
        """The innermost name around the variable line at ls: every run of
        variable lines follows a "+name", a "*name" or an "@" line."""
        m = self.m
        a = m.rfind(b"\n@", 0, ls)
        b = max(m.rfind(b"\n+", a, ls), m.rfind(b"\n*", a, ls))
        if b >= 0:
            return m[b + 2:m.find(b"\n", b + 1)]
        f = m[a + 2:m.find(b"\n", a + 1)].split(b"\t")
        return f[-1].lstrip(b"*") if len(f) > 2 else b""

    def _at(self, ls):
        """The signal of the variable line at ls."""
        f = self._line(ls)[0].split(b" ", 2)
        names, ords, _ = self._place(ls)
        return self._signal(int(f[0]), int(f[1]), ".".join(names + [f[2].decode("latin-1")]), ls,
                            ords[-1] if ords else 0)

    def _walk(self, at, whole=False):
        """(code, width, full name, offset, scope number) of each variable
        from the line at `at`: to the end of the scope that line opens, or to
        the end of the file."""
        m = self.m
        end = len(m)
        names, ords, count = self._place(at)
        prefix = ["".join(n + "." for n in names)]
        ords = [ords[-1] if ords else 0]
        depth = 0
        while at < end:
            le = m.find(b"\n", at)
            if le < 0:
                le = end
            c = m[at] if le > at else 0
            if 48 <= c <= 57:
                f = m[at:le].split(b" ", 2)
                if len(f) == 3:
                    yield int(f[0]), int(f[1]), prefix[-1] + f[2].decode("latin-1"), at, ords[-1]
            elif c == 43 or c == 42:  # + *
                prefix.append(prefix[-1] + m[at + 1:le].decode("latin-1") + ".")
                if c == 43:
                    count += 1
                    ords.append(count)
                else:
                    ords.append(ords[-1])
                depth += 1
            elif c == 45:  # -
                if len(prefix) > 1:
                    prefix.pop()
                    ords.pop()
                depth -= 1
                if depth <= 0 and not whole:
                    return
            at = le + 1

    def _skip(self, at):
        """The offset after the scope opened by the "+" line at `at`."""
        m = self.m
        depth = 0
        for mt in _STRUCT.finditer(m, at):
            depth += 1 if mt.group(1) is not None else -1
            if depth <= 0:
                return mt.end() + 1
        return len(m)

    def _leaf_lines(self, leaf):
        """Offsets of the variable lines whose name is exactly leaf."""
        m = self.m
        needle = b" " + leaf.encode("latin-1", "replace") + b"\n"
        at = len(MAGIC)
        while True:
            p = m.find(needle, at)
            if p < 0:
                return
            ls = m.rfind(b"\n", 0, p) + 1
            if 48 <= m[ls] <= 57 and m[ls:p].count(b" ") == 1:
                yield ls
            at = p + len(needle) - 1

    # -- the questions --------------------------------------------------------

    def iter_all(self):
        for v in self._walk(len(MAGIC), whole=True):
            yield self._signal(*v)

    def suffix(self, name, limit=10, exact_only=False):
        """(the signal named exactly `name` or None, the first `limit`
        signals whose name ends with .name, how many of those there are)."""
        parts = name.split(".")
        leaf = parts[-1]
        want = parts[-2].encode("latin-1", "replace") if len(parts) > 1 else None
        tail = "." + name
        exact, hits, total = None, [], 0
        for ls in self._leaf_lines(leaf):
            if want is not None and self._parent(ls) != want:
                continue
            full = ".".join(self._place(ls)[0] + [leaf])
            if full == name:
                exact = self._at(ls)
                if exact_only:
                    break
            elif not exact_only and full.endswith(tail):
                total += 1
                if len(hits) < limit:
                    hits.append(self._at(ls))
        return exact, hits, total

    def exact(self, name):
        if name not in self._exact:
            if len(self._exact) > 100000:
                self._exact.clear()
            self._exact[name] = self.suffix(name, exact_only=True)[0]
        return self._exact[name]

    def glob(self, pattern, limit=None):
        """(the first `limit` signals matching the pattern, how many match)."""
        rx = _rx(pattern)
        hits, total = [], 0

        def take(code, width, name, at, scope):
            nonlocal total
            if not rx.fullmatch(name):
                return
            total += 1
            if limit is None or len(hits) < limit:
                hits.append(self._signal(code, width, name, at, scope))

        if pattern and not pattern.strip("*"):  # everything: the count is known
            for sig in self.iter_all():
                if limit is not None and len(hits) >= limit:
                    break
                hits.append(sig)
            return hits, self.count
        piece = max(re.split(r"[*?.]", pattern), key=len)
        if not piece:
            for v in self._walk(len(MAGIC), whole=True):
                take(*v)
            return hits, total
        # the piece is inside one scope name or one variable name: a scope
        # brings all it holds, a variable itself
        m = self.m
        needle = piece.encode("latin-1", "replace")
        at = len(MAGIC)
        while True:
            p = m.find(needle, at)
            if p < 0:
                break
            ls = m.rfind(b"\n", 0, p) + 1
            c = m[ls]
            if c == 43 or c == 42:  # +scope *struct
                for v in self._walk(ls):
                    take(*v)
                at = self._skip(ls)
                continue
            line, at = self._line(ls)
            if 48 <= c <= 57:
                f = line.split(b" ", 2)
                if len(f) == 3 and needle in f[2]:  # in the name, not only in the id or the width
                    names, ords, _ = self._place(ls)
                    take(int(f[0]), int(f[1]), ".".join(names + [f[2].decode("latin-1")]), ls,
                         ords[-1] if ords else 0)
        return hits, total

    def suggest(self, name, limit=8):
        """Names whose leaf holds the leaf of `name`, shortest first."""
        leaf = re.sub(r"\[.*", "", name.split(".")[-1])
        if not leaf:
            return []
        m = self.m
        needle = leaf.encode("latin-1", "replace")
        found, at = [], len(MAGIC)
        while len(found) < 64:
            p = m.find(needle, at)
            if p < 0:
                break
            ls = m.rfind(b"\n", 0, p) + 1
            line, at = self._line(ls)
            f = line.split(b" ", 2)
            if 48 <= m[ls] <= 57 and len(f) == 3 and needle in f[2]:
                found.append(".".join(self._place(ls)[0] + [f[2].decode("latin-1")]))
        found.sort(key=lambda n: (len(n.split(".")[-1]), len(n), n))
        return found[:limit]

    def clocks(self):
        """The clock candidates the wave server noted while listing."""
        out = []
        for code, scope, name in self.clock_names:
            out.append(self._signal(code, 1, name, -1, scope))
        return out


class Signals:
    """wave.signals of an indexed dump: its length is known, walking it
    reads the whole index."""

    def __init__(self, names):
        self.names = names

    def __len__(self):
        return self.names.count

    def __iter__(self):
        return self.names.iter_all()


class ByName:
    """wave.by_name of an indexed dump: looked up, not held."""

    def __init__(self, names):
        self.names = names

    def get(self, name, default=None):
        sig = self.names.exact(name)
        return default if sig is None else sig

    def __contains__(self, name):
        return self.names.exact(name) is not None

    def __getitem__(self, name):
        sig = self.names.exact(name)
        if sig is None:
            raise KeyError(name)
        return sig


# ---------------------------------------------------------------------------
# Self test: a names file written here the way fsdbsrv.cc writes one
# ---------------------------------------------------------------------------


def _write_names(path, tree, checkpoint):
    """Write the names file of tree, a list of ("var", id, width, name),
    ("scope", name, [children]) and ("struct", name, [children]). Returns
    [(full name, id, width, scope number)] and the number of scopes."""
    out, flat = [MAGIC.decode()], []
    st = {"since": 0, "after_pop": False, "scopes": 0}
    names, kinds, ords = [], [], [0]

    def mark():
        out.append("@%d\t%d%s\n" % (ords[-1], st["scopes"],
                                    "".join("\t" + ("*" if k == "*" else "") + n for n, k in zip(names, kinds))))
        st["since"], st["after_pop"] = 0, False

    def emit(items):
        for it in items:
            if it[0] == "var":
                if st["after_pop"] or st["since"] >= checkpoint:
                    mark()
                line = "%d %d %s\n" % (it[1], it[2], it[3])
                flat.append((".".join(names + [it[3]]), it[1], it[2], ords[-1]))
            else:
                kind = "+" if it[0] == "scope" else "*"
                if st["since"] >= checkpoint or (st["after_pop"] and kind == "*"):
                    mark()
                line = kind + it[1] + "\n"
            out.append(line)
            st["since"] += len(line)
            if it[0] == "var":
                continue
            st["after_pop"] = False
            if kind == "+":
                st["scopes"] += 1
            names.append(it[1])
            kinds.append(kind)
            ords.append(st["scopes"] if kind == "+" else ords[-1])
            emit(it[2])
            names.pop()
            kinds.pop()
            ords.pop()
            out.append("-\n")
            st["since"] += 2
            st["after_pop"] = True

    mark()
    emit(tree)
    with open(path, "wb") as fh:
        fh.write("".join(out).encode("latin-1"))
    return flat, st["scopes"]


def selftest(tmp):
    """Compare every question against the plain list. Returns the failures."""
    import random

    rnd = random.Random(7)
    words = ["clk", "u_core", "rob", "valid", "data", "gen[0]", "gen[1]", "u_a", "q", "clk_en", "u_rob", "x"]
    ids = [0]

    def var():
        ids[0] += 1
        return ("var", rnd.randint(1, max(1, ids[0] // 2)), rnd.choice([1, 1, 8, 64]), rnd.choice(words))

    def build(depth):
        items = [var() for _ in range(rnd.randint(0, 4))]
        for _ in range(rnd.randint(1 if depth < 2 else 0, 3) if depth < 5 else 0):
            items.append((rnd.choice(["scope", "scope", "struct"]), rnd.choice(words), build(depth + 1)))
            items += [var() for _ in range(rnd.randint(0, 2))]
        return items

    failures = []
    for checkpoint in (1, 64, 1 << 14):
        tree = [("scope", "tb", build(0)), ("scope", "other", build(3)), var()]
        d = os.path.join(tmp, "names_%d" % checkpoint)
        os.makedirs(d, exist_ok=True)
        flat, scopes = _write_names(os.path.join(d, "names"), tree, checkpoint)
        idx = NameIndex(d, {"signals": len(flat), "scope_count": scopes})
        key = lambda s: (s.name, s.code, s.width, idx.scope_of[s.code])  # noqa: E731

        def same(label, got, want):
            if sorted(got) != sorted(want):
                failures.append("checkpoint %d: %s" % (checkpoint, label))

        got = []
        for s in idx.iter_all():
            got.append(key(s))  # scope_of is per code: read it before the next signal
        same("iter_all", got, flat)
        for full in sorted({f[0] for f in flat})[::3]:
            want = [f for f in flat if f[0] == full]
            sig = idx.exact(full)
            if sig is None or key(sig) not in want:
                failures.append("checkpoint %d: exact %s" % (checkpoint, full))
            for k in (1, 2, 3):
                name = ".".join(full.split(".")[-k:])
                exact, hits, total = idx.suffix(name, limit=1000)
                if total != sum(1 for f in flat if f[0].endswith("." + name)) or (exact is None) != (
                        not any(f[0] == name for f in flat)) or any(not h.name.endswith("." + name) for h in hits):
                    failures.append("checkpoint %d: suffix %s" % (checkpoint, name))
        if idx.exact("tb.nosuch") is not None or idx.suffix("nosuch")[2]:
            failures.append("checkpoint %d: a name that is not there" % checkpoint)
        for pat in ("*", "*1*", "*0]*", "*clk*", "tb.*", "*.u_core.*", "*rob.valid", "*gen[[]0]*", "*gen[0].c?k", "*u_?.q", "other*data",
                    "*.x", "t?.clk", "*nosuch*", "*.*.*.*.q"):
            want = [f for f in flat if w2i._matches(f[0], pat)]
            hits, total = idx.glob(pat)
            got = []
            for s in hits:
                got.append(s.name)
            if total != len(want) or sorted(got) != sorted(f[0] for f in want):
                failures.append("checkpoint %d: glob %s (%d, want %d)" % (checkpoint, pat, total, len(want)))
            few, total2 = idx.glob(pat, limit=2)
            if total2 != len(want) or len(few) != min(2, len(want)):
                failures.append("checkpoint %d: glob %s with a limit" % (checkpoint, pat))
        # the scope number of a signal, which the wave server is told before reading it
        for ls in idx._leaf_lines("clk"):
            names, ords, _ = idx._place(ls)
            want = {f[3] for f in flat if f[0] == ".".join(names + ["clk"])}
            if (ords[-1] if ords else 0) not in want:
                failures.append("checkpoint %d: scope number at %d" % (checkpoint, ls))
        if not idx.suggest("tb.cl"):
            failures.append("checkpoint %d: suggest" % checkpoint)
        idx.close()
    return failures
