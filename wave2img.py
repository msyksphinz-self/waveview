#!/usr/bin/env python3
"""Render a slice of a simulation waveform dump as a timing diagram image.

Takes a VCD (or FST) dump, a set of signal patterns and a time window, and
writes an SVG or PNG timing diagram. It replaces the usual routine of opening
GTKWave, zooming to the interesting moment, picking a handful of signals and
taking a screenshot, so a specific moment of RTL behaviour can be dropped into
a document or a blog post without a GUI in the loop.

How it works:
- VCD is parsed here; there is no external waveform library
- Drawing is matplotlib on the Agg backend. SVG and PNG come out of the same
  render, picked by the output file extension
- Signals are selected by glob against the full hierarchical name. Rows follow
  the order the patterns were given, and within one pattern the order the
  signals appear in the dump
- The window is either absolute (--from / --to) or found by searching for the
  first time a signal takes a value (--trigger) and sizing around it
- With --clock, a gridline is drawn at every rising edge and the x axis is
  numbered in clock cycles instead of raw time

FST input:
- .fst is converted to VCD first. The converter is tools/fst2vcd_mini.c, built
  against the GTKWave FST library that ships inside Verilator
  (<VERILATOR_ROOT>/include/gtkwave). It is compiled on demand and cached, so
  GTKWave does not have to be installed
- If that build is not possible, an external fst2vcd on PATH is used instead

Where it differs from GTKWave:
- No interaction, no signal browser, no filtering of the dump itself
- Only the VCD subset Verilator emits is understood: $date $version $timescale
  $scope $upscope $var $enddefinitions $dumpvars, scalar and vector value
  changes, and #<time> timestamps. Real values are skipped with a warning
- Bus values are printed in hex only, and only when the segment is wide enough
- No analogue traces, no enum decoding, no translate filters

Usage:
    python3 tools/wave2img.py sim.vcd -o wave.svg \
        -s 'tb.dut.clk' -s 'tb.dut.*_valid' --clock 'tb.dut.clk' \
        --trigger 'tb.dut.commit_valid=1' --before 2 --after 8

    python3 tools/wave2img.py --selftest
"""

from __future__ import annotations

import argparse
import bisect
import math
import os
import shutil
import subprocess
import re
import sys
import tempfile

# matplotlib is only needed for drawing. The parser half of this file is also
# imported by tools/wavequery.py, which must work without it.
try:
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["axes.unicode_minus"] = False
    # Keep SVG text as text so the figure stays searchable and small.
    matplotlib.rcParams["svg.fonttype"] = "none"

    import matplotlib.pyplot as plt  # noqa: E402
    from matplotlib.patches import Polygon, Rectangle  # noqa: E402
except ImportError:  # pragma: no cover - depends on the host
    matplotlib = plt = Polygon = Rectangle = None

# Palette chosen so the picture still reads after a grayscale print: meaning is
# carried by line style and fill pattern, the hues only help on screen.
COL_WAVE = "#16324f"  # waveform strokes, dark
COL_BUS_FILL = "#e8eef4"  # light fill inside a known bus value
COL_X_EDGE = "#8c1d1d"  # unknown (x) outline, dark
COL_X_FILL = "#f4e3e3"
COL_Z_EDGE = "#4a4a4a"  # high impedance (z) outline, mid
COL_Z_FILL = "#f0f0f0"
COL_GRID = "#c4ccd4"
COL_ROWBAND = "#f7f8f9"
COL_TRIGGER = "#8c1d1d"

HATCH_X = "xxxx"
HATCH_Z = "...."


class Signal:
    """One $var entry: a hierarchical name, a width and a dump identifier."""

    def __init__(self, name, width, code, index):
        self.name = name
        self.width = width
        self.code = code
        self.index = index  # declaration order in the dump

    def __repr__(self):
        return "Signal(%s, %d)" % (self.name, self.width)


class Dump:
    """A parsed waveform dump."""

    def __init__(self):
        self.timescale = "1 unit"
        self.signals = []  # list of Signal, declaration order
        self.times = {}  # code -> list of times (sorted)
        self.values = {}  # code -> list of value strings, parallel to times
        self.start_time = 0
        self.end_time = 0

    def by_code(self, code):
        return self.times.get(code, []), self.values.get(code, [])


def parse_vcd(path):
    """Parse the VCD subset Verilator emits. Returns a Dump."""
    dump = Dump()
    scope = []
    code_width = {}
    pending_scalar = None
    real_warned = False
    time = 0
    in_header = True
    skip_until_end = False
    header_tokens = []

    def finish_header():
        """Walk the collected header tokens and build the signal list."""
        i = 0
        nonlocal scope
        n = len(header_tokens)
        while i < n:
            tok = header_tokens[i]
            if tok == "$timescale":
                parts = []
                i += 1
                while i < n and header_tokens[i] != "$end":
                    parts.append(header_tokens[i])
                    i += 1
                dump.timescale = "".join(parts) if parts else dump.timescale
            elif tok == "$scope":
                # $scope <type> <name> $end
                j = i + 1
                fields = []
                while j < n and header_tokens[j] != "$end":
                    fields.append(header_tokens[j])
                    j += 1
                if len(fields) >= 2:
                    scope.append(fields[1])
                elif len(fields) == 1:
                    scope.append(fields[0])
                i = j
            elif tok == "$upscope":
                if scope:
                    scope.pop()
                while i < n and header_tokens[i] != "$end":
                    i += 1
            elif tok == "$var":
                # $var <type> <width> <code> <name> [<range>] $end
                j = i + 1
                fields = []
                while j < n and header_tokens[j] != "$end":
                    fields.append(header_tokens[j])
                    j += 1
                if len(fields) >= 4:
                    try:
                        width = int(fields[1])
                    except ValueError:
                        width = 1
                    code = fields[2]
                    name = fields[3]
                    full = ".".join(scope + [name])
                    dump.signals.append(Signal(full, width, code, len(dump.signals)))
                    code_width[code] = width
                i = j
            elif tok in ("$date", "$version", "$comment"):
                while i < n and header_tokens[i] != "$end":
                    i += 1
            i += 1

    def record(code, value):
        if code not in dump.times:
            dump.times[code] = []
            dump.values[code] = []
        times = dump.times[code]
        values = dump.values[code]
        if times and times[-1] == time:
            values[-1] = value
        else:
            times.append(time)
            values.append(value)

    with open(path, "r", errors="replace") as handle:
        for line in handle:
            if in_header:
                stripped = line.strip()
                if not stripped:
                    continue
                header_tokens.extend(stripped.split())
                if "$enddefinitions" in stripped:
                    # consume up to the matching $end, then switch to the body
                    while header_tokens and header_tokens[-1] != "$end":
                        break
                    if header_tokens[-1] == "$end":
                        in_header = False
                        header_tokens = [
                            t for t in header_tokens if t != "$enddefinitions"
                        ]
                        finish_header()
                continue

            toks = line.split()
            k = 0
            while k < len(toks):
                tok = toks[k]
                if skip_until_end:
                    if tok == "$end":
                        skip_until_end = False
                    k += 1
                    continue
                if pending_scalar is not None:
                    record(tok, pending_scalar)
                    pending_scalar = None
                    k += 1
                    continue
                if tok[0] == "#":
                    try:
                        time = int(tok[1:])
                    except ValueError:
                        pass
                    k += 1
                    continue
                if tok[0] == "$":
                    if tok in ("$comment", "$date", "$version"):
                        skip_until_end = True
                    # $dumpvars, $dumpall, $dumpon, $dumpoff and $end need no
                    # action: the value changes inside them are read as usual.
                    k += 1
                    continue
                if tok[0] in "bB":
                    value = tok[1:]
                    if k + 1 < len(toks):
                        record(toks[k + 1], value)
                        k += 2
                    else:
                        pending_scalar = value
                        k += 1
                    continue
                if tok[0] in "rR":
                    if not real_warned:
                        sys.stderr.write(
                            "warning: real valued signals are not drawn and "
                            "are skipped\n"
                        )
                        real_warned = True
                    k += 2 if k + 1 < len(toks) else 1
                    continue
                if tok[0] in "01xXzZ":
                    if len(tok) > 1:
                        record(tok[1:], tok[0])
                    else:
                        pending_scalar = tok[0]
                    k += 1
                    continue
                k += 1

    all_times = [t for ts in dump.times.values() if ts for t in (ts[0], ts[-1])]
    dump.start_time = min(all_times) if all_times else 0
    dump.end_time = max(all_times) if all_times else 0
    if dump.end_time == dump.start_time:
        dump.end_time = dump.start_time + 1
    return dump


def verilator_root():
    """Locate VERILATOR_ROOT, preferring what the installed verilator says."""
    try:
        out = subprocess.run(
            ["verilator", "--getenv", "VERILATOR_ROOT"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        root = out.stdout.strip()
        if out.returncode == 0 and root and os.path.isdir(root):
            return root
    except (OSError, subprocess.SubprocessError):
        pass
    root = os.environ.get("VERILATOR_ROOT", "")
    if root and os.path.isdir(root):
        return root
    return None


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "scariv", "wave2img")
    os.makedirs(path, exist_ok=True)
    return path


def build_fst_converter(verbose=True):
    """Compile tools/fst2vcd_mini.c on demand. Returns the binary path or None.

    The binary is cached and only rebuilt when the C source is newer.
    """
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fst2vcd_mini.c")
    if not os.path.isfile(source):
        return None
    binary = os.path.join(cache_dir(), "fst2vcd_mini")
    if os.path.isfile(binary) and os.path.getmtime(binary) >= os.path.getmtime(source):
        return binary

    root = verilator_root()
    if root is None:
        if verbose:
            sys.stderr.write(
                "warning: VERILATOR_ROOT not found, cannot build the FST reader\n"
            )
        return None
    include = os.path.join(root, "include", "gtkwave")
    fstapi = os.path.join(include, "fstapi.c")
    if not os.path.isfile(fstapi):
        if verbose:
            sys.stderr.write("warning: %s not found\n" % fstapi)
        return None

    compiler = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        if verbose:
            sys.stderr.write("warning: no C compiler found for the FST reader\n")
        return None

    base = [
        compiler,
        "-O2",
        "-w",
        '-DFST_CONFIG_INCLUDE="fst_config.h"',
        "-I" + include,
        "-o",
        binary,
        source,
        fstapi,
    ]
    # Depending on the version, fstapi.c either includes fastlz.c and lz4.c or
    # expects them to be compiled alongside. Try with them first, then without.
    extra = [
        os.path.join(include, name)
        for name in ("fastlz.c", "lz4.c")
        if os.path.isfile(os.path.join(include, name))
    ]
    attempts = [base + extra + ["-lz", "-lpthread"], base + ["-lz", "-lpthread"]]
    last = None
    for cmd in attempts:
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode == 0 and os.path.isfile(binary):
            return binary
        last = done
    if verbose and last is not None:
        sys.stderr.write("warning: building the FST reader failed:\n")
        sys.stderr.write(last.stderr.strip()[-2000:] + "\n")
    return None


def fst_to_vcd(fst_path, out_path):
    """Convert an FST dump to VCD. Returns True on success."""
    binary = build_fst_converter()
    if binary is not None:
        done = subprocess.run(
            [binary, fst_path, "-o", out_path], capture_output=True, text=True
        )
        if done.returncode == 0 and os.path.isfile(out_path):
            return True
        sys.stderr.write(
            "warning: the built-in FST reader failed: %s\n" % done.stderr.strip()
        )
    external = shutil.which("fst2vcd")
    if external is not None:
        done = subprocess.run(
            [external, fst_path, "-o", out_path], capture_output=True, text=True
        )
        if done.returncode == 0 and os.path.isfile(out_path):
            return True
        sys.stderr.write("error: fst2vcd failed: %s\n" % done.stderr.strip())
        return False
    sys.stderr.write(
        "error: cannot read FST input.\n"
        "  The built-in reader needs a C compiler plus the GTKWave sources\n"
        "  that ship with Verilator (<VERILATOR_ROOT>/include/gtkwave).\n"
        "  Set VERILATOR_ROOT or put verilator on PATH, or install GTKWave\n"
        "  so that fst2vcd is on PATH. VCD input needs none of this.\n"
    )
    return False


def load_dump(path, tmpdir):
    """Load a dump, converting FST to VCD first when needed."""
    if path.lower().endswith(".fst"):
        converted = os.path.join(tmpdir, "converted.vcd")
        if not fst_to_vcd(path, converted):
            return None
        return parse_vcd(converted)
    return parse_vcd(path)


def _matches(name, pattern):
    """Glob match, except that [] is literal.

    Signal names carry a bit index (foo[2]), and fnmatch reads [2] as a
    character class, so the obvious pattern silently matches nothing. Only
    * and ? are treated as wildcards here.
    """
    rx = []
    for ch in pattern:
        if ch == "*":
            rx.append(".*")
        elif ch == "?":
            rx.append(".")
        else:
            rx.append(re.escape(ch))
    return re.fullmatch("".join(rx), name) is not None


def select_signals(dump, patterns):
    """Match patterns against full hierarchical names, keeping pattern order."""
    selected = []
    seen = set()
    for pattern in patterns:
        hits = [s for s in dump.signals if _matches(s.name, pattern)]
        if not hits:
            sys.stderr.write("warning: pattern matched no signal: %s\n" % pattern)
            continue
        for sig in hits:
            if sig.name in seen:
                continue
            seen.add(sig.name)
            selected.append(sig)
    return selected


def find_one(dump, pattern):
    """First signal matching a pattern, or None."""
    for sig in dump.signals:
        if _matches(sig.name, pattern):
            return sig
    return None


def common_scope(names):
    """Longest shared leading hierarchy, never the whole name. Dot joined."""
    if not names:
        return ""
    parts = [n.split(".") for n in names]
    limit = min(len(p) for p in parts) - 1  # always keep the leaf
    shared = []
    for i in range(max(0, limit)):
        token = parts[0][i]
        if all(p[i] == token for p in parts):
            shared.append(token)
        else:
            break
    return ".".join(shared)


def normalize(value, width):
    """Extend a VCD payload to the declared width, per the VCD extension rule."""
    value = value.lower()
    if len(value) >= width:
        return value[len(value) - width :]
    fill = value[0] if value and value[0] in "xz" else "0"
    return fill * (width - len(value)) + value


def is_unknown(value):
    return any(c in "xz" for c in value)


def value_at(times, values, t, width):
    """Value held at time t, or all x before the first change."""
    if not times:
        return "x" * width
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return "x" * width
    return normalize(values[i], width)


def segments(dump, sig, t0, t1):
    """Value segments inside [t0, t1] as (start, end, value, clip0, clip1)."""
    times, values = dump.by_code(sig.code)
    out = []
    start = t0
    current = value_at(times, values, t0, sig.width)
    clipped_start = True
    if times:
        i = bisect.bisect_right(times, t0)
        while i < len(times) and times[i] <= t1:
            if times[i] > start:
                out.append((start, times[i], current, clipped_start, False))
                clipped_start = False
            start = times[i]
            current = normalize(values[i], sig.width)
            i += 1
    out.append((start, t1, current, clipped_start, True))
    return [s for s in out if s[1] > s[0]]


def rising_edges(dump, sig):
    times, values = dump.by_code(sig.code)
    edges = []
    previous = None
    for t, v in zip(times, values):
        norm = normalize(v, sig.width)
        if norm == "1" and previous != "1":
            edges.append(t)
        previous = norm
    return edges


def parse_match_value(text):
    """Parse a --trigger right hand side. Returns ('int', n) or ('sym', c)."""
    text = text.strip().lower()
    if text in ("x", "z"):
        return ("sym", text)
    if text.startswith("0x"):
        return ("int", int(text[2:], 16))
    if text.startswith("0b"):
        return ("int", int(text[2:], 2))
    if len(text) > 1 and all(c in "01" for c in text):
        return ("int", int(text, 2))
    return ("int", int(text, 0))


def value_matches(value, target):
    kind, wanted = target
    if kind == "sym":
        return all(c == wanted for c in value)
    if is_unknown(value):
        return False
    return int(value, 2) == wanted


def find_trigger_time(dump, sig, target):
    """First time the signal takes the wanted value, or None."""
    times, values = dump.by_code(sig.code)
    for t, v in zip(times, values):
        if value_matches(normalize(v, sig.width), target):
            return t
    return None


def hex_text(value, width):
    """Hex rendering of a known bus value. Unknown values get a short label."""
    if is_unknown(value):
        if all(c == "x" for c in value):
            return "x"
        if all(c == "z" for c in value):
            return "z"
        return value
    digits = max(1, (width + 3) // 4)
    return "%0*x" % (digits, int(value, 2))


def render(dump, sigs, t0, t1, args, clock_sig, trigger_time, output):
    """Draw one timing diagram and write it to output."""
    if plt is None:
        raise SystemExit("error: drawing needs matplotlib (pip install matplotlib)")
    scope = common_scope([s.name for s in sigs])
    labels = []
    for sig in sigs:
        name = sig.name
        if scope and name.startswith(scope + "."):
            name = name[len(scope) + 1 :]
        if sig.width > 1:
            name = "%s[%d:0]" % (name, sig.width - 1)
        labels.append(name)

    rows = len(sigs)
    label_chars = max(len(x) for x in labels)
    label_fs = 9.0
    fig_w = float(args.width)
    fig_h = rows * float(args.height_per_signal) + 1.15
    if args.title:
        fig_h += 0.25

    left = min(0.45, 0.012 + label_chars * label_fs * 0.62 / 72.0 / fig_w)
    right = 0.985
    bottom = 0.72 / fig_h
    top = 1.0 - (0.55 if args.title else 0.22) / fig_h

    fig = plt.figure(figsize=(fig_w, fig_h))
    ax = fig.add_axes([left, bottom, right - left, top - bottom])
    ax.set_xlim(t0, t1)
    ax.set_ylim(0, rows)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#888888")
    ax.tick_params(axis="y", length=0)
    ax.set_yticks([rows - i - 0.5 for i in range(rows)])
    ax.set_yticklabels(labels, family="monospace", fontsize=label_fs)

    # faint alternating row bands so the eye can follow a long row
    for i in range(rows):
        if i % 2 == 1:
            ax.add_patch(
                Rectangle(
                    (t0, rows - i - 1),
                    t1 - t0,
                    1.0,
                    facecolor=COL_ROWBAND,
                    edgecolor="none",
                    zorder=0,
                )
            )

    edges = []
    if clock_sig is not None:
        all_edges = rising_edges(dump, clock_sig)
        edges = [t for t in all_edges if t0 <= t <= t1]
        for t in edges:
            ax.axvline(t, color=COL_GRID, linewidth=0.7, linestyle=(0, (3, 3)), zorder=1)

    # geometry of one row
    pad = 0.22
    axes_w_in = fig_w * (right - left)
    data_per_in = (t1 - t0) / axes_w_in if axes_w_in > 0 else 1.0
    skew = min(0.006 * (t1 - t0), 0.4 * data_per_in * 0.08)
    if skew <= 0:
        skew = 0.004 * (t1 - t0)
    value_fs = 8.0
    char_data = value_fs * 0.6 / 72.0 * data_per_in

    for i, sig in enumerate(sigs):
        base = rows - i - 1
        ylo = base + pad
        yhi = base + 1 - pad
        ymid = (ylo + yhi) / 2.0
        segs = segments(dump, sig, t0, t1)

        if sig.width == 1:
            previous_y = None
            for (xa, xb, value, clip0, clip1) in segs:
                if is_unknown(value):
                    unknown_box(ax, xa, xb, ylo, yhi, value)
                    previous_y = None
                    continue
                y = yhi if value == "1" else ylo
                ax.plot(
                    [xa, xb],
                    [y, y],
                    color=COL_WAVE,
                    linewidth=1.6,
                    solid_capstyle="butt",
                    zorder=3,
                )
                if previous_y is not None and previous_y != y:
                    ax.plot(
                        [xa, xa],
                        [previous_y, y],
                        color=COL_WAVE,
                        linewidth=1.6,
                        zorder=3,
                    )
                elif previous_y is None and not clip0:
                    ax.plot([xa, xa], [ylo, yhi], color=COL_WAVE, linewidth=1.6, zorder=3)
                previous_y = y
        else:
            for (xa, xb, value, clip0, clip1) in segs:
                if is_unknown(value):
                    unknown_box(ax, xa, xb, ylo, yhi, value)
                else:
                    sa = 0.0 if clip0 else min(skew, (xb - xa) / 2.0)
                    sb = 0.0 if clip1 else min(skew, (xb - xa) / 2.0)
                    points = [
                        (xa, ymid if sa else yhi),
                        (xa + sa, yhi),
                        (xb - sb, yhi),
                        (xb, ymid if sb else yhi),
                        (xb, ymid if sb else ylo),
                        (xb - sb, ylo),
                        (xa + sa, ylo),
                        (xa, ymid if sa else ylo),
                    ]
                    ax.add_patch(
                        Polygon(
                            points,
                            closed=True,
                            facecolor=COL_BUS_FILL,
                            edgecolor=COL_WAVE,
                            linewidth=1.3,
                            zorder=3,
                        )
                    )
                text = hex_text(value, sig.width)
                if (len(text) + 1.0) * char_data <= (xb - xa):
                    # the hatch of an unknown segment would swallow bare text
                    box = None
                    if is_unknown(value):
                        box = dict(
                            facecolor="white",
                            edgecolor="none",
                            alpha=0.9,
                            pad=1.5,
                        )
                    ax.text(
                        (xa + xb) / 2.0,
                        ymid,
                        text,
                        ha="center",
                        va="center",
                        family="monospace",
                        fontsize=value_fs,
                        color="#101010",
                        bbox=box,
                        zorder=4,
                    )

    if trigger_time is not None and t0 <= trigger_time <= t1:
        ax.axvline(
            trigger_time,
            color=COL_TRIGGER,
            linewidth=1.2,
            linestyle=(0, (6, 3)),
            zorder=5,
        )

    if clock_sig is not None and edges:
        index = {t: n for n, t in enumerate(rising_edges(dump, clock_sig))}
        step = max(1, int(math.ceil(len(edges) / 16.0)))
        ticks = edges[::step]
        ax.set_xticks(ticks)
        ax.set_xticklabels([str(index[t]) for t in ticks], fontsize=8)
        ax.set_xlabel(
            "clock cycle (rising edge of %s)" % clock_sig.name.split(".")[-1],
            fontsize=9,
        )
    else:
        ax.tick_params(axis="x", labelsize=8)
        ax.set_xlabel("time [%s]" % dump.timescale, fontsize=9)

    if args.title:
        fig.suptitle(args.title, fontsize=11, y=1.0 - 0.12 / fig_h, va="top")

    note = []
    if scope:
        note.append("scope %s" % scope)
    if clock_sig is None:
        note.append("timescale %s" % dump.timescale)
    if trigger_time is not None:
        note.append("trigger at %d" % trigger_time)
    if note:
        fig.text(
            right,
            0.012,
            "   ".join(note),
            ha="right",
            va="bottom",
            fontsize=7.5,
            color="#555555",
            family="monospace",
        )

    fig.savefig(output, facecolor="white")
    plt.close(fig)


def unknown_box(ax, xa, xb, ylo, yhi, value):
    """Draw an x or z segment so it can never be mistaken for a zero."""
    if all(c == "z" for c in value):
        face, edge, hatch, style = COL_Z_FILL, COL_Z_EDGE, HATCH_Z, (0, (4, 2))
    else:
        face, edge, hatch, style = COL_X_FILL, COL_X_EDGE, HATCH_X, "-"
    ax.add_patch(
        Rectangle(
            (xa, ylo),
            xb - xa,
            yhi - ylo,
            facecolor=face,
            edgecolor=edge,
            hatch=hatch,
            linewidth=1.2,
            linestyle=style,
            zorder=3,
        )
    )


SELFTEST_VCD = """$date
\tnot a real run
$end
$version
\twave2img.py selftest
$end
$timescale
\t1ns
$end
$scope module tb_wave $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 1 " req $end
$var wire 16 # data [15:0] $end
$var wire 4 $ state [3:0] $end
$var wire 8 % cfg_const [7:0] $end
$var wire 1 & tri_sig $end
$upscope $end
$upscope $end
$enddefinitions $end
"""


def write_selftest_vcd(path):
    """Write a synthetic dump that exercises every drawing path."""
    lines = [SELFTEST_VCD.rstrip("\n")]
    lines.append("#0")
    lines.append("$dumpvars")
    lines.append("0!")
    lines.append("0\"")
    lines.append("bxxxxxxxxxxxxxxxx #")
    lines.append("bxxxx $")
    lines.append("b10100101 %")
    lines.append("z&")
    lines.append("$end")

    bus_values = [
        0x0000,
        0x00FF,
        0xDEAD,
        0xBEEF,
        0x1234,
        0xFFFF,
        0x0001,
        0xABCD,
        0x5A5A,
    ]
    state_values = [0x0, 0x1, 0x3, 0x7, 0xF, 0x8, 0x2]

    t = 0
    for cycle in range(20):
        # rising edge
        t = cycle * 10
        lines.append("#%d" % t)
        lines.append("1!")
        if cycle == 2:
            lines.append("1\"")
            lines.append("1&")
        if cycle == 3:
            lines.append("0\"")
        if cycle == 7:
            lines.append("1\"")
        if cycle == 9:
            lines.append("0\"")
        if cycle == 14:
            lines.append("z&")
        if cycle >= 2:
            value = bus_values[(cycle - 2) % len(bus_values)]
            lines.append("b%s #" % format(value, "b"))
        if cycle == 6:
            # a stretch of x in the middle of a known bus
            lines.append("bxxxx $")
        elif cycle >= 3:
            lines.append("b%s $" % format(state_values[cycle % len(state_values)], "b"))
        # falling edge
        lines.append("#%d" % (t + 5))
        lines.append("0!")
    lines.append("#%d" % (20 * 10))
    lines.append("")
    with open(path, "w") as handle:
        handle.write("\n".join(lines))


def dump_fingerprint(dump):
    """A comparable summary of a dump: names, widths and value changes."""
    out = []
    for sig in sorted(dump.signals, key=lambda s: s.name):
        times, values = dump.by_code(sig.code)
        changes = [
            (t, normalize(v, sig.width)) for t, v in zip(times, values)
        ]
        out.append((sig.name, sig.width, changes))
    return out


def run_selftest(args):
    """Write a synthetic dump, render it several ways, and report the files."""
    outdir = tempfile.mkdtemp(prefix="wave2img_selftest_")
    vcd_path = os.path.join(outdir, "selftest.vcd")
    write_selftest_vcd(vcd_path)
    print("synthetic dump: %s" % vcd_path)

    dump = parse_vcd(vcd_path)
    print(
        "parsed %d signals, time %d..%d, timescale %s"
        % (len(dump.signals), dump.start_time, dump.end_time, dump.timescale)
    )

    class Options:
        pass

    base = Options()
    base.width = args.width
    base.height_per_signal = args.height_per_signal

    sigs = select_signals(dump, ["*"])
    clock = find_one(dump, "*.clk")

    # 1. the whole dump, plain time axis
    base.title = "wave2img selftest: whole dump, time axis"
    out1 = os.path.join(outdir, "selftest_full.svg")
    render(dump, sigs, dump.start_time, dump.end_time, base, None, None, out1)
    print("wrote %s" % out1)

    # 2. a trigger window with a clock axis
    trigger_sig = find_one(dump, "*.req")
    t_trig = find_trigger_time(dump, trigger_sig, ("int", 1))
    edges = rising_edges(dump, clock)
    i = max(0, bisect.bisect_right(edges, t_trig) - 1)
    t0 = edges[max(0, i - 2)]
    t1 = edges[min(len(edges) - 1, i + 8)]
    base.title = "wave2img selftest: trigger req=1, clock axis"
    out2 = os.path.join(outdir, "selftest_trigger.png")
    render(dump, sigs, t0, t1, base, clock, t_trig, out2)
    print("wrote %s (trigger at %d, window %d..%d)" % (out2, t_trig, t0, t1))

    # 3. the FST path, when the reader can be built
    binary = build_fst_converter(verbose=False)
    if binary is None:
        print(
            "FST path: skipped, the FST reader could not be built "
            "(needs a C compiler and VERILATOR_ROOT)"
        )
        return 0
    fst_path = os.path.join(outdir, "selftest.fst")
    done = subprocess.run(
        [binary, "--vcd2fst", vcd_path, "-o", fst_path],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0 or not os.path.isfile(fst_path):
        print("FST path: could not write a test FST: %s" % done.stderr.strip())
        return 1
    fst_dump = load_dump(fst_path, outdir)
    if fst_dump is None:
        print("FST path: reading the FST back failed")
        return 1
    if dump_fingerprint(fst_dump) == dump_fingerprint(dump):
        print("FST path: round trip matches the VCD exactly")
    else:
        print("FST path: round trip differs from the VCD")
        return 1
    fst_sigs = select_signals(fst_dump, ["*"])
    base.title = "wave2img selftest: same dump read back from FST"
    out3 = os.path.join(outdir, "selftest_fst.svg")
    render(fst_dump, fst_sigs, t0, t1, base, find_one(fst_dump, "*.clk"), t_trig, out3)
    print("wrote %s" % out3)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="input waveform dump (.vcd or .fst)")
    parser.add_argument(
        "-s",
        "--signal",
        action="append",
        default=[],
        metavar="PATTERN",
        help="glob matched against the full hierarchical signal name; repeat "
        "for more rows, the row order follows the pattern order",
    )
    parser.add_argument(
        "-o", "--output", metavar="PATH", help="output image, .svg or .png"
    )
    parser.add_argument(
        "--from",
        dest="from_time",
        type=int,
        metavar="TIME",
        help="window start, in dump time units (default: start of the dump)",
    )
    parser.add_argument(
        "--to",
        dest="to_time",
        type=int,
        metavar="TIME",
        help="window end, in dump time units (default: end of the dump)",
    )
    parser.add_argument(
        "--trigger",
        metavar="SIGNAL=VALUE",
        help="instead of an absolute window, centre on the first time SIGNAL "
        "takes VALUE. VALUE may be binary, decimal, 0b... or 0x...",
    )
    parser.add_argument(
        "--before",
        type=int,
        metavar="N",
        help="window size before the trigger, in clock cycles with --clock "
        "and in time units otherwise (default: 2 cycles, or a twentieth of "
        "the dump without a clock)",
    )
    parser.add_argument(
        "--after",
        type=int,
        metavar="N",
        help="window size after the trigger, same units as --before "
        "(default: 8 cycles, or a tenth of the dump without a clock)",
    )
    parser.add_argument(
        "--clock",
        metavar="PATTERN",
        help="clock signal; draws a gridline at every rising edge and numbers "
        "the x axis in cycles. The clock gets a row of its own only if it "
        "also matches a --signal pattern",
    )
    parser.add_argument("--title", metavar="TEXT", help="title above the diagram")
    parser.add_argument(
        "--height-per-signal",
        type=float,
        default=0.52,
        metavar="INCHES",
        help="row height (default: 0.52)",
    )
    parser.add_argument(
        "--width",
        type=float,
        default=11.0,
        metavar="INCHES",
        help="figure width (default: 11.0)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the signal names in the dump and exit",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="render a synthetic dump into a temporary directory and report "
        "the paths; needs no input file",
    )
    args = parser.parse_args()

    if args.selftest:
        return run_selftest(args)

    if not args.input:
        parser.error("an input dump is required (or use --selftest)")

    tmpdir = tempfile.mkdtemp(prefix="wave2img_")
    try:
        dump = load_dump(args.input, tmpdir)
        if dump is None:
            return 1

        if args.list:
            for sig in dump.signals:
                print("%-6d %s" % (sig.width, sig.name))
            return 0

        if not args.signal:
            parser.error("at least one -s/--signal pattern is required")
        if not args.output:
            parser.error("-o/--output is required")
        extension = os.path.splitext(args.output)[1].lower()
        if extension not in (".svg", ".png"):
            sys.stderr.write("error: output must end in .svg or .png\n")
            return 1

        sigs = select_signals(dump, args.signal)
        if not sigs:
            sys.stderr.write("error: no signal matched any pattern\n")
            return 1

        clock_sig = None
        if args.clock:
            clock_sig = find_one(dump, args.clock)
            if clock_sig is None:
                sys.stderr.write(
                    "warning: clock pattern matched no signal: %s\n" % args.clock
                )
            elif clock_sig.width != 1:
                sys.stderr.write(
                    "warning: clock %s is %d bits wide, rising edges may be "
                    "meaningless\n" % (clock_sig.name, clock_sig.width)
                )

        edges = rising_edges(dump, clock_sig) if clock_sig is not None else []
        span = dump.end_time - dump.start_time
        trigger_time = None

        if args.trigger:
            if "=" not in args.trigger:
                sys.stderr.write("error: --trigger wants SIGNAL=VALUE\n")
                return 1
            pattern, _, raw = args.trigger.partition("=")
            trigger_sig = find_one(dump, pattern.strip())
            if trigger_sig is None:
                sys.stderr.write(
                    "error: trigger pattern matched no signal: %s\n" % pattern.strip()
                )
                return 1
            try:
                target = parse_match_value(raw)
            except ValueError:
                sys.stderr.write("error: cannot read trigger value: %s\n" % raw)
                return 1
            trigger_time = find_trigger_time(dump, trigger_sig, target)
            if trigger_time is None:
                sys.stderr.write(
                    "error: %s never takes the value %s in this dump\n"
                    % (trigger_sig.name, raw.strip())
                )
                return 1
            if edges:
                before = 2 if args.before is None else args.before
                after = 8 if args.after is None else args.after
                i = max(0, bisect.bisect_right(edges, trigger_time) - 1)
                t0 = edges[max(0, i - before)]
                t1 = edges[min(len(edges) - 1, i + after)]
            else:
                before = max(1, span // 20) if args.before is None else args.before
                after = max(1, span // 10) if args.after is None else args.after
                t0 = trigger_time - before
                t1 = trigger_time + after
        else:
            t0 = dump.start_time if args.from_time is None else args.from_time
            t1 = dump.end_time if args.to_time is None else args.to_time

        if args.from_time is not None:
            t0 = args.from_time
        if args.to_time is not None:
            t1 = args.to_time
        if t1 <= t0:
            sys.stderr.write("error: empty window: %d..%d\n" % (t0, t1))
            return 1

        directory = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(directory, exist_ok=True)
        render(dump, sigs, t0, t1, args, clock_sig, trigger_time, args.output)
        print(
            "wrote %s (%d signals, time %d..%d)" % (args.output, len(sigs), t0, t1)
        )
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
