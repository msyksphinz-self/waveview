"""waveview rc (.wvrc): a debug view as a text file.

A .wvrc file says which signals to show, where the cursors are, and what
to point at: arrows between signal transitions and comments on the
waveform. Hand one to the viewer and it opens on exactly that view:

    waveview.py serve --rc bug123.wvrc          # the dump comes from the file
    waveview.py load bug123.wvrc                # into a running viewer
    waveview.py load -e 'comment flush_valid@1234c "here"'
    waveview.py save bug123.wvrc                # the current view, as a file

Format: one command per line, arguments split like a shell (quote text
with spaces), '#' starts a comment. Options are key=value anywhere on the
line.

    dump    ../sim/w.fst                 # relative to the .wvrc file
    clock   tb.clk
    note    "ROB flush one cycle late after a mispredict"

    signal  u_bru.mispredict_valid
    signal  '*u_rob.commit_*' radix=hex  # glob or unique suffix
    signal  u_rob.flush_valid  radix=bin

    cursor  1234c                        # the main cursor
    marker  1228c
    zoom    1220c 1245c                  # or: zoom fit
    cursor  A  1230c  color=red  "mispredict"     # named cursors
    cursor  B  A+4c   color=green "flush"

    arrow   u_bru.mispredict_valid@A  ->  u_rob.flush_valid@B  "4 cycles"
    comment u_rob.flush_valid@B  "expected at A+2"
    comment @A "on the time axis"

Times:
    1234c / 1234cyc   cycle 1234 (the rising edge of the clock)
    5000              time in dump units
    12ns 3.5us        time with a unit (fs ps ns us ms s)
    A  cursor marker  a named cursor, the main cursor, the marker
    A+3c  B-10ns      offset from one of those

A point (for arrow and comment) is SIGNAL@TIME, or @TIME for the time
axis. Signals named by arrows and comments are added to the signal list
when the file does not show them.

Colors: red orange yellow green cyan blue purple pink white gray, or #rrggbb.
"""
from __future__ import annotations

import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import remote  # noqa: E402

COLORS = {
    "red": "#ff5c5c", "orange": "#ff9f43", "yellow": "#ffd93d", "green": "#4cd964",
    "cyan": "#3fd7e6", "blue": "#4ea1ff", "purple": "#b07cff", "pink": "#ff6ad5",
    "white": "#e8e8e8", "gray": "#9aa0a6", "grey": "#9aa0a6",
}
CURSOR_COLORS = ["#ff9f43", "#4cd964", "#4ea1ff", "#b07cff", "#ff5c5c", "#3fd7e6"]
RADIXES = ("hex", "dec", "sdec", "bin", "oct")
UNITS = {"fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}
RESERVED = ("cursor", "marker")

_TERM = re.compile(r"^(?P<base>[A-Za-z_]\w*)?(?P<off>[+-]?\d+(?:\.\d+)?(?P<unit>c|cyc|fs|ps|ns|us|ms|s)?)?$")


class RcError(Exception):
    pass


def split(line):
    """Shell-like words. '#' starts a comment only at the start of a word,
    so that color=#ff8800 stays one word."""
    q = None
    for i, ch in enumerate(line):
        if q:
            if ch == q and not (q == '"' and i and line[i - 1] == "\\"):
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            line = line[:i]
            break
    return shlex.split(line)


def color(text, default=None):
    if text is None:
        return default
    c = COLORS.get(text.lower())
    if c:
        return c
    if re.fullmatch(r"#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}", text):
        return text
    raise RcError("unknown color %r (use %s or #rrggbb)" % (text, " ".join(k for k in COLORS if k != "grey")))


def timescale_seconds(ts):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(fs|ps|ns|us|ms|s)\s*", ts or "")
    return float(m.group(1)) * UNITS[m.group(2)] if m else None


class Reader:
    """Applies .wvrc lines to a viewer state (a dict shaped like Hub.state)."""

    def __init__(self, wave, timeline_fn, state, base_dir="."):
        self.wave = wave
        self.timeline_fn = timeline_fn  # called again after `clock`
        self.st = state
        self.base_dir = base_dir
        self.warnings = []
        self.times = []  # every time the file names, for the default zoom
        self.zoomed = False
        self.dump = None

    # -- times --------------------------------------------------------------

    def _named(self, name):
        if name == "cursor":
            t = self.st.get("cursor")
        elif name == "marker":
            t = self.st.get("marker")
        else:
            t = next((c["time"] for c in self.st.get("cursors", []) if c["name"] == name), None)
        if t is None:
            raise RcError("%r is not set (named cursors must be defined before use)" % name)
        return t

    def time(self, tok):
        m = _TERM.match(tok)
        if not m or not (m.group("base") or m.group("off")):
            raise RcError("bad time %r (e.g. 1234c, 5000, 12ns, A, A+3c)" % tok)
        base, off, unit = m.group("base"), m.group("off"), m.group("unit")
        if base and off and off[0] not in "+-":
            raise RcError("bad time %r: write an offset with a sign (A+3c)" % tok)
        num = off[: len(off) - len(unit)] if off and unit else off
        if unit in ("c", "cyc"):
            tl = self.timeline_fn()
            if not tl.edges:
                raise RcError("%r: cycle numbers need a clock" % tok)
            n = int(float(num))
            if base:
                c0 = tl.cycle_of(self._named(base))
                n += 0 if c0 is None else c0
            t = tl.time_of(n)
        else:
            if unit:
                ts = timescale_seconds(self.wave.timescale)
                if ts is None:
                    raise RcError("%r: the dump's timescale %r has no unit" % (tok, self.wave.timescale))
                d = float(num) * UNITS[unit] / ts
            else:
                d = float(num) if num else 0.0
            t = (self._named(base) if base else 0) + d
            t = int(round(t)) if abs(t - round(t)) < 1e-6 else t
        self.times.append(t)
        return t

    def point(self, tok):
        if "@" not in tok:
            raise RcError("bad point %r: write SIGNAL@TIME or @TIME" % tok)
        sig, _, t = tok.rpartition("@")
        name = None
        if sig:
            name = self.wave.resolve(sig).name
            self._ensure_shown(name)
        return {"signal": name, "time": self.time(t)}

    def _ensure_shown(self, name):
        if all(s["name"] != name for s in self.st["signals"]):
            self.st["signals"].append({"name": name, "radix": "hex"})

    # -- lines --------------------------------------------------------------

    def run(self, text, origin="rc"):
        for no, raw in enumerate(text.splitlines(), 1):
            try:
                words = split(raw)
            except ValueError as exc:
                self.warnings.append("%s:%d: %s" % (origin, no, exc))
                continue
            if not words:
                continue
            opts, args = {}, []
            for w in words[1:]:
                k, eq, v = w.partition("=")
                if eq and re.fullmatch(r"[a-z]+", k):
                    opts[k] = v
                else:
                    args.append(w)
            fn = getattr(self, "cmd_" + words[0].lower(), None)
            try:
                if fn is None:
                    raise RcError("unknown command %r" % words[0])
                fn(args, opts)
            except Exception as exc:  # noqa: BLE001  - one bad line must not stop the file
                msg = str(exc).split("\n")[0]
                self.warnings.append("%s:%d: %s" % (origin, no, msg))
        return self

    def finish(self):
        """Default zoom: everything the file pointed at, with a margin."""
        if self.zoomed or not self.times:
            return
        lo, hi = min(self.times), max(self.times)
        tl = self.timeline_fn()
        period = (tl.edges[-1] - tl.edges[0]) / (len(tl.edges) - 1) if len(tl.edges) > 1 else 0
        pad = max((hi - lo) * 0.15, period * 4, 1)
        self.st["view"] = [lo - pad, hi + pad]

    @staticmethod
    def _need(args, n, usage):
        if len(args) < n:
            raise RcError("usage: " + usage)

    def cmd_dump(self, args, opts):
        self._need(args, 1, "dump PATH")
        p = args[0]
        if not remote.is_remote(p):
            p = os.path.normpath(os.path.join(self.base_dir, os.path.expanduser(p)))
        self.dump = p

    def cmd_clock(self, args, opts):
        self._need(args, 1, "clock SIGNAL")
        self.st["clock"] = self.wave.resolve(args[0]).name

    def cmd_note(self, args, opts):
        self.st["note"] = {"text": " ".join(args), "source": "rc", "at": 0} if args else None

    def cmd_signal(self, args, opts):
        self._need(args, 1, "signal PATTERN... [radix=hex]")
        radix = opts.get("radix", "hex")
        if radix not in RADIXES:
            raise RcError("radix must be one of %s" % " ".join(RADIXES))
        for pat in args:
            for s in self.wave.select([pat]):
                old = next((x for x in self.st["signals"] if x["name"] == s.name), None)
                if old:
                    old["radix"] = radix
                else:
                    self.st["signals"].append({"name": s.name, "radix": radix})

    cmd_add = cmd_signal

    def cmd_cursor(self, args, opts):
        self._need(args, 1, "cursor TIME | cursor NAME TIME [label] [color=..]")
        if len(args) == 1 and not opts:
            self.st["cursor"] = self.time(args[0])
            return
        name = args[0]
        if name in RESERVED or not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise RcError("bad cursor name %r" % name)
        self._need(args, 2, "cursor NAME TIME [label] [color=..]")
        t = self.time(args[1])
        cur = self.st.setdefault("cursors", [])
        cur[:] = [c for c in cur if c["name"] != name]
        cur.append({"name": name, "time": t, "label": " ".join(args[2:]),
                    "color": color(opts.get("color"), CURSOR_COLORS[len(cur) % len(CURSOR_COLORS)])})

    def cmd_marker(self, args, opts):
        self.st["marker"] = self.time(args[0]) if args else None

    def cmd_zoom(self, args, opts):
        self._need(args, 1, "zoom FROM TO | zoom fit")
        if args[0] == "fit":
            self.st["view"] = [self.wave.start_time, self.wave.end_time]
        else:
            self._need(args, 2, "zoom FROM TO | zoom fit")
            t0, t1 = self.time(args[0]), self.time(args[1])
            if t1 <= t0:
                raise RcError("zoom: empty window")
            self.st["view"] = [t0, t1]
        self.zoomed = True

    def cmd_arrow(self, args, opts):
        args = [a for a in args if a not in ("->", "=>")]
        self._need(args, 2, "arrow SIGNAL@TIME SIGNAL@TIME [label] [color=..]")
        a, b = self.point(args[0]), self.point(args[1])
        self.st.setdefault("arrows", []).append(
            {"from": a, "to": b, "label": " ".join(args[2:]), "color": color(opts.get("color"))})

    def cmd_comment(self, args, opts):
        self._need(args, 2, "comment SIGNAL@TIME TEXT [color=..]")
        p = self.point(args[0])
        self.st.setdefault("comments", []).append(
            dict(p, text=" ".join(args[1:]).replace("\\n", "\n"), color=color(opts.get("color"))))


def is_nwave(text):
    """An nWave save file rather than a .wvrc."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith((";", "#")):
            continue
        return s.split()[0] in ("Magic", "Revision", "openDirFile", "addSignal", "addGroup", "activeDirFile")
    return False


def dump_of(text, base_dir):
    """The dump a .wvrc names, or None."""
    for line in text.splitlines():
        try:
            words = split(line)
        except ValueError:
            continue
        if len(words) >= 2 and words[0] == "dump":
            r = Reader(None, None, {}, base_dir)
            r.cmd_dump(words[1:], {})
            return r.dump
    return None


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _q(text):
    return shlex.quote(text) if text else ""


def write(state, wave, timeline, dump=None):
    """The view as .wvrc text."""
    tl = timeline
    names = {}  # time -> cursor name, to write points relative to cursors
    for c in state.get("cursors", []):
        names.setdefault(c["time"], c["name"])

    def tt(t, use_names=True):
        if use_names and t in names:
            return names[t]
        if tl.edges:
            c = tl.cycle_of(t)
            if c is not None and tl.edges[c] == t:
                return "%dc" % c
        return str(int(t)) if float(t).is_integer() else repr(float(t))

    def pt(p):
        return "%s@%s" % (p["signal"] or "", tt(p["time"]))

    def col(c, default=None):
        if not c or c == default:
            return ""
        inv = {v: k for k, v in COLORS.items() if k != "grey"}
        return " color=" + inv.get(c, c)

    out = ["# waveview rc: open with  waveview.py serve --rc FILE  or  waveview.py load FILE"]
    if dump:
        out.append("dump    %s" % _q(dump))
    if state.get("clock"):
        out.append("clock   %s" % state["clock"])
    if state.get("note"):
        out.append("note    %s" % _q(state["note"]["text"]))
    out.append("")
    for s in state.get("signals", []):
        out.append("signal  %s%s" % (s["name"], "" if s.get("radix", "hex") == "hex" else " radix=" + s["radix"]))
    out.append("")
    for i, c in enumerate(state.get("cursors", [])):
        line = "cursor  %s %s" % (c["name"], tt(c["time"], use_names=False))
        if c.get("label"):
            line += " " + _q(c["label"])
        out.append(line + col(c.get("color"), CURSOR_COLORS[i % len(CURSOR_COLORS)]))
    if state.get("cursor") is not None:
        out.append("cursor  %s" % tt(state["cursor"]))
    if state.get("marker") is not None:
        out.append("marker  %s" % tt(state["marker"]))
    if state.get("view"):
        out.append("zoom    %s %s" % (tt(state["view"][0], False), tt(state["view"][1], False)))
    if state.get("arrows") or state.get("comments"):
        out.append("")
    for a in state.get("arrows", []):
        out.append("arrow   %s -> %s%s%s" % (pt(a["from"]), pt(a["to"]),
                                             " " + _q(a["label"]) if a.get("label") else "", col(a.get("color"))))
    for c in state.get("comments", []):
        out.append("comment %s %s%s" % (pt(c), _q(c["text"].replace("\n", "\\n")), col(c.get("color"))))
    for a in state.get("annotations", []):
        out.append("comment @%s %s" % (tt(a["time"]), _q(a.get("label") or "*")))
    return "\n".join(out) + "\n"
