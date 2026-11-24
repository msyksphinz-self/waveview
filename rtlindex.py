#!/usr/bin/env python3
"""A light SystemVerilog source index for waveform debugging.

It answers the questions a waveform viewer needs to link a dump back to the
RTL, the way Verdi's nTrace does, without an elaborator:

- which module and source line a hierarchical signal of the dump belongs to
  (TOP.tb_scariv_top.u_dut.u_rob.r_head -> scariv_rob, core/rtl/.../rob.sv:120)
- where a signal is declared, which statements drive it and which read it
- how drivers continue across the hierarchy: through instance output ports
  into a submodule, and through input ports up to the parent's connection
- every identifier of a module body with its position, so a source view can
  put the value of each signal next to it (active annotation)

It is a tokenizer plus statement heuristics, not a parser. It handles what
RTL usually looks like: ANSI and non-ANSI ports, logic/wire/reg and user
typed declarations, assign, always blocks with = and <=, concatenated left
hand sides, instances with named, implicit (.p) and wildcard (.*) ports,
parameter overrides, instance arrays and generate blocks. Preprocessor
directives are dropped and both sides of `ifdef are indexed. Functions and
tasks are indexed as part of the module. Anything it cannot read is skipped
rather than fatal.

Usage:
    rtlindex.py core/filelist.f --module scariv_rob
    rtlindex.py core/filelist.f --trace TOP.tb_scariv_top.u_dut.w_rob_head
    rtlindex.py --selftest
"""

# no "from __future__ import annotations": this file is sent to the python3 of
# a build server, which may be 3.6 (RHEL 8)

import argparse
import json
import os
import re
import sys

KEYWORDS = set(
    """
    module endmodule interface endinterface program endprogram package endpackage
    input output inout ref logic wire reg bit byte int integer longint shortint var
    signed unsigned genvar localparam parameter typedef struct union enum packed
    assign always always_ff always_comb always_latch initial final begin end
    if else case casez casex unique unique0 priority endcase default for foreach
    while do repeat forever generate endgenerate function endfunction task endtask
    return automatic static posedge negedge or and not iff import export const
    modport void string real time realtime supply0 supply1 tri wand wor
    break continue disable fork join join_any join_none wait assert assume cover
    property endproperty sequence endsequence inside with new null this super
    """.split()
)
DECL_START = {"input", "output", "inout", "ref", "logic", "wire", "reg", "bit", "byte", "int",
              "integer", "longint", "shortint", "var", "genvar", "localparam", "parameter",
              "signed", "unsigned", "string", "real", "time", "tri"}
DIRECTIONS = {"input", "output", "inout", "ref"}
ASSIGN_OPS = {"=", "<=", "+=", "-=", "|=", "&=", "^=", "<<=", ">>=", "*=", "/=", "%="}

TOKEN_RE = re.compile(
    r"""
    (?P<ws>[ \t\r\f\v]+)
  | (?P<nl>\n)
  | (?P<lc>//[^\n]*)
  | (?P<bc>/\*.*?\*/)
  | (?P<str>"(?:\\.|[^"\\\n])*")
  | (?P<dir>`[A-Za-z_]\w*)
  | (?P<num>\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?|'[01xXzZ])
  | (?P<sys>\$[A-Za-z_]\w*)
  | (?P<id>[A-Za-z_][\w$]*|\\\S+)
  | (?P<op><<<=|>>>=|<<=|>>=|===|!==|==\?|!=\?|->|<->|::|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<=|>=|==|!=|&&|\|\||<<|>>|\*\*|\.\*|[(){}\[\];,.:#@=<>+\-*/%&|^~!?'])
  | (?P<other>.)
    """,
    re.S | re.X,
)


class Tok:
    __slots__ = ("kind", "text", "line", "col", "file")

    def __init__(self, kind, text, line, col, file=None):
        # file: None for the file being parsed, else the `include it came from
        self.kind, self.text, self.line, self.col, self.file = kind, text, line, col, file

    def __repr__(self):
        return "%s@%d:%d" % (self.text, self.line, self.col)


MAX_INCLUDE_DEPTH = 16


def find_include(name, from_file, incdirs):
    """Where `include "name" points: next to the including file, then the
    +incdir+ directories, then as given."""
    if os.path.isabs(name):
        return name if os.path.isfile(name) else None
    cands = []
    if from_file:
        cands.append(os.path.join(os.path.dirname(os.path.abspath(from_file)), name))
    cands += [os.path.join(d, name) for d in incdirs]
    cands.append(name)
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


_INCLUDE_CACHE = {}  # abs path -> (mtime, size, tokens): a header every module includes is read once


def _include_tokens(inc, incdirs, included, label, chain):
    """The tokens of an included file, from a cache keyed on the file's
    mtime and size (its own `includes expanded, their files labelled). None
    when it cannot be read."""
    try:
        st = os.stat(inc)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    hit = _INCLUDE_CACHE.get(inc)
    if hit is not None and hit[0] == key:
        for p, n in hit[2]:
            if included is not None and all(q != p for q, _ in included):
                included.append((p, n))
        return hit[1]
    try:
        with open(inc, errors="replace") as fh:
            itext = fh.read()
    except OSError:
        return None
    nested = []
    toks = tokenize(itext, inc, incdirs, nested, label, chain)
    if len(_INCLUDE_CACHE) > 512:
        _INCLUDE_CACHE.clear()
    _INCLUDE_CACHE[inc] = (key, toks, nested)
    if included is not None:
        for p, n in nested:
            if all(q != p for q, _ in included):
                included.append((p, n))
    return toks


def tokenize(text, path=None, incdirs=(), included=None, label=None, _chain=()):
    """Tokens with 1-based line and 0-based column. Comments and directive
    lines are dropped (both branches of `ifdef stay). An `include is
    expanded in place when the file is found (next to `path`, then in
    incdirs): its tokens carry the included file as `file`, so that
    positions keep pointing into it. `included` collects (abs path, label)
    of the files pulled in; `label` names them for the tokens (a function
    of the absolute path, e.g. a path relative to the index root)."""
    out = []
    line, line_start = 1, 0
    skip_line = 0
    for m in TOKEN_RE.finditer(text):
        kind = m.lastgroup
        s = m.group()
        if kind == "nl":
            line += 1
            line_start = m.end()
            continue
        if kind in ("ws",):
            continue
        if kind in ("lc", "bc"):
            n = s.count("\n")
            if n:
                line += n
                line_start = m.start() + s.rfind("\n") + 1
            continue
        if kind == "dir":
            # `define / `include / `ifdef ...: drop the rest of the line for
            # the ones that take arguments there
            if s in ("`define", "`include", "`ifdef", "`ifndef", "`elsif", "`undef", "`timescale", "`line", "`pragma"):
                skip_line = line
            if s == "`include" and len(_chain) < MAX_INCLUDE_DEPTH:
                arg = re.match(r'[ \t]*"([^"\n]+)"', text[m.end():])
                inc = find_include(arg.group(1), path, incdirs) if arg else None
                if inc and inc not in _chain and inc != (os.path.abspath(path) if path else None):
                    sub = _include_tokens(inc, incdirs, included, label,
                                          _chain + ((os.path.abspath(path),) if path else ()) + (inc,))
                    if sub is not None:
                        name = label(inc) if label else inc
                        if included is not None and all(p != inc for p, _ in included):
                            included.append((inc, name))
                        out += [Tok(t.kind, t.text, t.line, t.col, t.file or name) for t in sub]
            continue
        if skip_line == line:
            continue
        out.append(Tok(kind, s, line, m.start() - line_start))
    return out


def ident_at_file(text):
    """(line, col, name) of every identifier, for annotation."""
    return [(t.line, t.col, t.text) for t in tokenize(text) if t.kind == "id" and t.text not in KEYWORDS]


# ---------------------------------------------------------------------------
# Index data
# ---------------------------------------------------------------------------


class Loc:
    __slots__ = ("file", "line", "col", "kind", "detail")

    def __init__(self, file, line, col, kind, detail=None):
        self.file, self.line, self.col, self.kind, self.detail = file, line, col, kind, detail

    def as_dict(self):
        d = {"file": self.file, "line": self.line, "col": self.col, "kind": self.kind}
        if self.detail:
            d.update(self.detail)
        return d


class Instance:
    def __init__(self, name, mtype, file, line, col):
        self.name, self.mtype, self.file, self.line, self.col = name, mtype, file, line, col
        self.conns = []  # [(port or None, [ident toks], line, col)]
        self.wildcard = False
        self.gen = None  # enclosing generate block label


class Module:
    def __init__(self, name, file, line):
        self.name, self.file, self.line = name, file, line
        self.orig = name  # source name (an elaborated copy may be bit_cnt__W2)
        self.end_line = line
        self.ports = []  # [name] in order
        self.port_dir = {}  # name -> input/output/inout
        self.decls = {}  # name -> Loc
        self.drivers = {}  # name -> [Loc]
        self.loads = {}  # name -> [Loc]
        self.instances = {}  # name -> Instance
        self.gen_labels = {}  # label -> line
        self.params = set()
        self.kind = "module"  # module / interface / program
        self.modports = {}  # interface: modport name -> {signal: input/output/inout}
        self.port_type = {}  # port -> (type name, modport or None) for ports typed by a name (bus_if.mst m)
        self.port_iface = {}  # port -> (interface module name, modport or None), once the type is known

    def add(self, table, name, loc):
        table.setdefault(name, []).append(loc)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _match(toks, i, open_, close):
    """Index of the token closing the group opened at toks[i]."""
    depth = 0
    for j in range(i, len(toks)):
        t = toks[j].text
        if t == open_:
            depth += 1
        elif t == close:
            depth -= 1
            if depth == 0:
                return j
    return len(toks) - 1


def _ids_outside_index(toks):
    """Identifiers of an lvalue: not inside [...], not field names after '.',
    not keywords."""
    out, depth = [], 0
    for k, t in enumerate(toks):
        if t.text == "[":
            depth += 1
        elif t.text == "]":
            depth -= 1
        elif t.kind == "id" and depth == 0 and t.text not in KEYWORDS:
            if k > 0 and toks[k - 1].text in (".", "::"):
                continue
            if k + 1 < len(toks) and toks[k + 1].text == "::":
                continue
            out.append(t)
    return out


def _ids(toks):
    out = []
    for k, t in enumerate(toks):
        if t.kind != "id" or t.text in KEYWORDS:
            continue
        if k > 0 and toks[k - 1].text in (".", "::", "'"):
            continue
        if k + 1 < len(toks) and toks[k + 1].text in ("::", "("):
            # package scope or a function call
            if toks[k + 1].text == "::" or t.text.startswith("$"):
                continue
            if toks[k + 1].text == "(":
                continue
        out.append(t)
    return out


class Parser:
    def __init__(self, path, text, abspath=None, incdirs=(), label=None):
        """path: the file's name in the index (relative to its root);
        abspath: where it is, for `include; label: index name of an
        included file from its absolute path."""
        self.path = path
        self.included = []  # [(abs path, index name)]
        self.toks = tokenize(text, abspath or path, incdirs, self.included, label)
        self.modules = []

    def loc(self, tok, kind, detail=None):
        return Loc(tok.file or self.path, tok.line, tok.col, kind, detail)

    def parse(self):
        toks = self.toks
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.text in ("module", "interface", "program") and i + 1 < len(toks):
                j = i + 1
                if toks[j].text in ("automatic", "static"):
                    j += 1
                end_kw = "end" + t.text
                k = j
                while k < len(toks) and toks[k].text != end_kw:
                    k += 1
                if toks[j].kind == "id":
                    m = Module(toks[j].text, toks[j].file or self.path, toks[j].line)
                    m.kind = t.text
                    endtok = toks[min(k, len(toks) - 1)]
                    if (endtok.file or self.path) == m.file:
                        m.end_line = endtok.line
                    else:  # the module ends inside an `include: its last line in its own file
                        m.end_line = max([x.line for x in toks[j:k] if (x.file or self.path) == m.file] or [m.line])
                    self._module(m, j + 1, k)
                    self.modules.append(m)
                i = k + 1
                continue
            i += 1
        return self.modules

    # -- module header --------------------------------------------------

    def _module(self, m, i, end):
        toks = self.toks
        # imports in the header
        while i < end and toks[i].text == "import":
            while i < end and toks[i].text != ";":
                i += 1
            i += 1
        if i < end and toks[i].text == "#":
            close = _match(toks, i + 1, "(", ")")
            self._params(m, toks[i + 2:close])
            i = close + 1
        if i < end and toks[i].text == "(":
            close = _match(toks, i, "(", ")")
            self._ansi_ports(m, toks[i + 1:close])
            i = close + 1
        if i < end and toks[i].text == ";":
            i += 1
        self._body(m, i, end)

    def _params(self, m, toks):
        for item in self._split(toks, ","):
            names = [t for t in item if t.kind == "id" and t.text not in KEYWORDS]
            eq = next((k for k, t in enumerate(item) if t.text == "="), len(item))
            before = [t for t in item[:eq] if t.kind == "id" and t.text not in KEYWORDS]
            if before:
                n = before[-1]
                m.params.add(n.text)
                m.decls.setdefault(n.text, self.loc(n, "param"))
            del names

    def _ansi_ports(self, m, toks):
        direction = None
        for item in self._split(toks, ","):
            if not item:
                continue
            for t in item:
                if t.text in DIRECTIONS:
                    direction = t.text
            eq = next((k for k, t in enumerate(item) if t.text == "="), len(item))
            name = None
            depth = 0
            for t in item[:eq]:
                if t.text in "[(":
                    depth += 1
                elif t.text in "])":
                    depth -= 1
                elif t.kind == "id" and depth == 0 and t.text not in KEYWORDS:
                    name = t
            if name is None:
                continue
            if name.text in m.port_dir:
                continue
            m.ports.append(name.text)
            m.port_dir[name.text] = direction or "inout"
            m.decls[name.text] = self.loc(name, "port", {"dir": direction or "inout"})
            # a port typed by a name: `bus_if.mst m`, `bus_if m`, `interface m`
            # (an interface port, once bus_if is known to be one) or a typedef
            ids = [t for t in item[:eq] if t.kind == "id" and t.text not in KEYWORDS or t.text == "interface"]
            has_dir = any(t.text in DIRECTIONS for t in item)  # not the direction carried from the port before
            if not has_dir and len(ids) >= 2 and ids[-1] is name and ids[0].text not in DECL_START:
                tname = ids[0].text
                modport = None
                pos = item.index(ids[0])
                if pos + 2 < len(item) and item[pos + 1].text == "." and item[pos + 2].kind == "id":
                    modport = item[pos + 2].text
                m.port_type[name.text] = (tname, modport)

    def _modport(self, m, seg):
        """modport name (input a, b, output c), name2 (...);"""
        k = 1
        while k < len(seg):
            if seg[k].kind != "id" or k + 1 >= len(seg) or seg[k + 1].text != "(":
                k += 1
                continue
            close = _match(seg, k + 1, "(", ")")
            dirs = {}
            direction = "inout"
            for t in seg[k + 2:close]:
                if t.text in DIRECTIONS:
                    direction = t.text
                elif t.text in ("clocking", "import", "export"):
                    direction = None
                elif t.kind == "id" and t.text not in KEYWORDS and direction:
                    dirs[t.text] = direction
            m.modports[seg[k].text] = dirs
            k = close + 1

    @staticmethod
    def _split(toks, sep):
        """Split at sep on depth 0 of () [] {}."""
        out, cur, depth = [], [], 0
        for t in toks:
            if t.text in ("(", "[", "{"):
                depth += 1
            elif t.text in (")", "]", "}"):
                depth -= 1
            if t.text == sep and depth == 0:
                out.append(cur)
                cur = []
            else:
                cur.append(t)
        out.append(cur)
        return out

    # -- module body ----------------------------------------------------

    def _body(self, m, i, end):
        toks = self.toks
        gen_stack = []
        seg_start = i
        depth = 0
        k = i
        while k < end:
            t = toks[k]
            x = t.text
            if x in ("(", "[", "{"):
                depth += 1
            elif x in (")", "]", "}"):
                depth -= 1
            if depth == 0 and x in ("begin", "end", "generate", "endgenerate", "else", "endcase", "endfunction",
                                    "endtask", "fork", "join"):
                self._segment(m, seg_start, k, gen_stack)
                if x == "begin":
                    if k + 2 < end and toks[k + 1].text == ":" and toks[k + 2].kind == "id":
                        m.gen_labels.setdefault(toks[k + 2].text, toks[k + 2].line)
                        gen_stack.append(toks[k + 2].text)
                        k += 2
                    else:
                        gen_stack.append(None)
                elif x == "end":
                    if gen_stack:
                        gen_stack.pop()
                    if k + 2 < end and toks[k + 1].text == ":":
                        k += 2
                seg_start = k + 1
            elif depth == 0 and x == ";":
                self._segment(m, seg_start, k, gen_stack)
                seg_start = k + 1
            k += 1
        self._segment(m, seg_start, end, gen_stack)

    def _segment(self, m, a, b, gen_stack):
        if b <= a:
            return
        toks = self.toks
        seg = toks[a:b]
        # strip a leading case label "X:" / "default:" and attribute (* *)
        while len(seg) >= 2 and (seg[0].kind in ("id", "num") or seg[0].text == "default") and seg[1].text == ":" \
                and not (len(seg) > 2 and seg[2].text == ":"):
            seg = seg[2:]
        if not seg:
            return
        head = seg[0].text
        if head in ("import", "typedef", "function", "task", "return", "modport", "export"):
            if head in ("function", "task"):
                return
            if head == "typedef":
                return
            if head == "return":
                self._loads(m, seg[1:])
            if head == "modport":
                self._modport(m, seg)
            return
        if head in ("localparam", "parameter"):
            self._params(m, seg[1:])
            return
        if head in DECL_START or self._looks_like_typed_decl(seg):
            self._decl(m, seg)
            return
        inst = self._instance(m, seg, gen_stack)
        if inst:
            return
        self._statement(m, seg)

    def _looks_like_typed_decl(self, seg):
        """`type_t [dims] name ...` (not an instance: no '(' after the name)."""
        k = 0
        if seg[k].kind != "id" or seg[k].text in KEYWORDS:
            return False
        k += 1
        if k + 1 < len(seg) and seg[k].text == "::":
            k += 2
        while k < len(seg) and seg[k].text == "[":
            k = _match(seg, k, "[", "]") + 1
        if k < len(seg) and seg[k].kind == "id" and seg[k].text not in KEYWORDS:
            nxt = seg[k + 1].text if k + 1 < len(seg) else ";"
            return nxt in (",", "=", "[", ";") or k + 1 >= len(seg)
        return False

    def _decl(self, m, seg):
        direction = next((t.text for t in seg if t.text in DIRECTIONS), None)
        # skip the type: up to the first identifier at depth 0 that is
        # followed by , = [ or the end, after at least one type token
        items = self._split(seg, ",")
        first = True
        for item in items:
            eq = next((k for k, t in enumerate(item) if t.text == "=" ), len(item))
            depth = 0
            name = None
            for t in item[:eq]:
                if t.text in "[(":
                    depth += 1
                elif t.text in "])":
                    depth -= 1
                elif t.kind == "id" and depth == 0 and t.text not in KEYWORDS:
                    name = t
                    if not first:
                        break
            first = False
            if name is None:
                continue
            kind = "port" if direction else "decl"
            detail = {"dir": direction} if direction else None
            if direction:
                if name.text not in m.port_dir:
                    m.ports.append(name.text)
                m.port_dir[name.text] = direction
                m.decls[name.text] = self.loc(name, kind, detail)
            else:
                m.decls.setdefault(name.text, self.loc(name, kind, detail))
            if eq < len(item):
                m.add(m.drivers, name.text, self.loc(name, "init"))
                self._loads(m, item[eq + 1:])

    def _instance(self, m, seg, gen_stack):
        k = 0
        if seg[0].kind != "id" or seg[0].text in KEYWORDS:
            return False
        mtype = seg[0].text
        k = 1
        if k < len(seg) and seg[k].text == "#":
            if k + 1 < len(seg) and seg[k + 1].text == "(":
                close = _match(seg, k + 1, "(", ")")
                self._loads(m, seg[k + 2:close])
                k = close + 1
            else:
                k += 2
        if k >= len(seg) or seg[k].kind != "id" or seg[k].text in KEYWORDS:
            return False
        name_tok = seg[k]
        k += 1
        while k < len(seg) and seg[k].text == "[":
            close = _match(seg, k, "[", "]")
            self._loads(m, seg[k + 1:close])
            k = close + 1
        if k >= len(seg) or seg[k].text != "(":
            return False
        close = _match(seg, k, "(", ")")
        inst = Instance(name_tok.text, mtype, name_tok.file or self.path, name_tok.line, name_tok.col)
        inst.gen = next((g for g in reversed(gen_stack) if g), None)
        body = seg[k + 1:close]
        for item in self._split(body, ","):
            if not item:
                continue
            if item[0].text == ".*":
                inst.wildcard = True
                continue
            if item[0].text == "." and len(item) > 1:
                port = item[1].text
                if len(item) > 2 and item[2].text == "(":
                    pclose = _match(item, 2, "(", ")")
                    expr = item[3:pclose]
                    inst.conns.append((port, expr, item[1].line, item[1].col))
                else:  # .port  (implicit same name)
                    inst.conns.append((port, [item[1]], item[1].line, item[1].col))
            else:
                inst.conns.append((None, item, item[0].line, item[0].col))
        m.instances[inst.name] = inst
        return True

    def _statement(self, m, seg):
        """Drivers and loads of one statement."""
        depth = 0
        ops = []
        for k, t in enumerate(seg):
            if t.text in ("(", "{"):
                depth += 1
            elif t.text in (")", "}"):
                depth -= 1
            elif depth == 0 and t.text in ASSIGN_OPS:
                ops.append(k)
        if not ops:
            self._loads(m, seg)
            return
        k = ops[0]
        # left hand side: back from the operator to a statement boundary
        j = k - 1
        bdepth = 0
        while j >= 0:
            x = seg[j].text
            if x in ("]", "}"):
                bdepth += 1
            elif x in ("[", "{"):
                bdepth -= 1
            elif bdepth == 0 and (x in (")", ":", "assign", "else", "begin", "default", "@", "#", "always_comb",
                                        "always_latch", "always", "always_ff", "initial", "final", "deassign",
                                        "force", "release") or (x in KEYWORDS and x not in ("{", "}"))):
                break
            j -= 1
        lhs = seg[j + 1:k]
        kind = "assign" if any(t.text == "assign" for t in seg[:j + 1]) else (
            "always_ff" if any(t.text == "always_ff" for t in seg[:j + 1]) or seg[k].text == "<=" else "comb")
        targets = _ids_outside_index(lhs)
        for t in targets:
            m.add(m.drivers, t.text, self.loc(t, kind))
        # everything else is read
        self._loads(m, seg[:j + 1])
        idx = [t for t in lhs if t not in targets]
        self._loads(m, idx)
        self._loads(m, seg[k + 1:])

    def _loads(self, m, toks):
        for t in _ids(toks):
            m.add(m.loads, t.text, self.loc(t, "read"))


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class Sources(list):
    """The files of collect_sources, with the `include directories the
    filelists named (+incdir+a+b, -incdir a, -I a)."""

    incdirs = ()


def read_filelist(path, seen=None, incdirs=None):
    """Source files of a .f file (nested -f supported). +incdir+ / -incdir /
    -I go to `incdirs` when given; other options are ignored."""
    seen = seen or set()
    path = os.path.abspath(path)
    if path in seen:
        return []
    seen.add(path)
    base = os.path.dirname(path)
    out = []
    with open(path, errors="replace") as fh:
        text = re.sub(r"//[^\n]*", "", fh.read())
    words = text.split()
    k = 0

    def add_incdir(d):
        if incdirs is None:
            return
        d = os.path.expandvars(d)
        cand = d if os.path.isabs(d) else (_find_listed_dir(d, base) or os.path.join(base, d))
        cand = os.path.abspath(cand)
        if cand not in incdirs:
            incdirs.append(cand)

    while k < len(words):
        w = words[k]
        if w in ("-f", "-F") and k + 1 < len(words):
            nested = words[k + 1]
            cand = nested if os.path.isfile(nested) else os.path.join(base, nested)
            out += read_filelist(cand, seen, incdirs)
            k += 2
            continue
        if w.startswith("+incdir+"):
            for d in w[len("+incdir+"):].split("+"):
                if d:
                    add_incdir(d)
            k += 1
            continue
        if w in ("-incdir", "-I", "-i", "+incdir") and k + 1 < len(words):
            add_incdir(words[k + 1])
            k += 2
            continue
        if w.startswith("-I") and len(w) > 2:
            add_incdir(w[2:])
            k += 1
            continue
        if w.startswith(("-", "+")):
            k += 1
            continue
        if w.endswith((".sv", ".v", ".svh", ".vh")):
            cand = _find_listed(w, base)
            if cand:
                out.append(os.path.abspath(cand))
        k += 1
    return out


def _find_listed_dir(w, base):
    """A directory named in a filelist, found like _find_listed."""
    if os.path.isdir(w):
        return w
    d = base
    while True:
        cand = os.path.join(d, w)
        if os.path.isdir(cand):
            return cand
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def _find_listed(w, base):
    """A filelist entry: as given (relative to the current directory), else
    relative to the filelist's directory or one of its ancestors (lists that
    name files from the repository root)."""
    if os.path.isabs(w):
        return w if os.path.isfile(w) else None
    if os.path.isfile(w):
        return w
    d = base
    while True:
        cand = os.path.join(d, w)
        if os.path.isfile(cand):
            return cand
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def collect_sources(specs):
    """The source files of the specs (a .f filelist, a file, a directory),
    as a Sources list carrying the filelists' +incdir+ directories. A
    directory given directly is also an `include directory, with its
    subdirectories."""
    files = []
    incdirs = []
    for spec in specs:
        if os.path.isdir(spec):
            for root, dirs, names in os.walk(spec):
                dirs[:] = [d for d in dirs if not d.startswith((".", "obj_dir")) and d != "node_modules"]
                incdirs.append(os.path.abspath(root))
                for n in sorted(names):
                    if n.endswith((".sv", ".v")):
                        files.append(os.path.abspath(os.path.join(root, n)))
        elif spec.endswith((".f", ".F")):
            files += read_filelist(spec, incdirs=incdirs)
        elif os.path.isfile(spec):
            files.append(os.path.abspath(spec))
    seen, out = set(), Sources()
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    out.incdirs = incdirs
    return out


class Index:
    """Modules and, when an elaborated source provided it, the exact
    instance tree (hier: instance path -> module name).

    Producers: Index(files) parses sources with the heuristics above;
    from_verilator_xml() reads Verilator's elaborated --xml-only output;
    load_json() reads the open design JSON that any other tool (for example
    an exporter on top of Verdi's NPI reading a KDB or simv.daidir) can
    write. save_json() writes that format."""

    def __init__(self, files=(), root=None, producer="rtlindex", incdirs=None, progress=None):
        """progress(done, path) is called after each file, for a status."""
        self.root = os.path.abspath(root or os.getcwd())
        self.modules = {}
        self.files = list(files)
        self.errors = []
        self.hier = {}
        self.producer = producer
        # `include search path: +incdir+ of the filelists (collect_sources
        # attaches them), and where listed header files live
        self.incdirs = list(incdirs if incdirs is not None else getattr(files, "incdirs", ()))
        for f in files:
            if f.endswith((".svh", ".vh")) and os.path.dirname(os.path.abspath(f)) not in self.incdirs:
                self.incdirs.append(os.path.dirname(os.path.abspath(f)))
        for k, f in enumerate(files):
            if progress is not None and (k % 20 == 0 or k == len(files) - 1):
                progress(k, f)
            try:
                with open(f, errors="replace") as fh:
                    text = fh.read()
                p = Parser(self.rel(f), text, abspath=f, incdirs=self.incdirs, label=self.rel)
                for m in p.parse():
                    self.modules.setdefault(m.name, m)
                for inc, _name in p.included:
                    if inc not in self.files:
                        self.files.append(inc)  # watched for changes, like the listed ones
            except Exception as exc:  # noqa: BLE001
                self.errors.append("%s: %s" % (f, exc))
        self.resolve_iface_ports()

    def resolve_iface_ports(self):
        """Ports typed by a name are interface ports when that name is an
        interface here (a typedef otherwise); `interface x` is one whatever
        the type."""
        for m in self.modules.values():
            for port, (tname, modport) in m.port_type.items():
                target = self.modules.get(tname)
                if tname == "interface" or (target is not None and target.kind == "interface"):
                    m.port_iface[port] = (tname, modport)

    def iface_port_dir(self, iface, modport):
        """What an instance does through an interface port: 'output' when
        its modport mostly drives (a master / source), 'input' when it
        mostly reads, None without a modport to tell."""
        im = self.modules.get(iface)
        if im is None or not modport or modport not in im.modports:
            return None
        dirs = im.modports[modport].values()
        outs = sum(1 for d in dirs if d == "output")
        ins = sum(1 for d in dirs if d == "input")
        if outs == ins:
            return None
        return "output" if outs > ins else "input"

    reader = None  # callable(rel path) -> text, for sources on another machine
    anchors = {}  # dump scope -> module, see place_roots

    def read_text(self, rel):
        if self.reader is not None:
            return self.reader(rel)
        with open(self.abs(rel), errors="replace") as fh:
            return fh.read()

    def rel(self, path):
        path = os.path.abspath(path)
        if path.startswith(self.root + os.sep):
            return path[len(self.root) + 1:]
        return path

    def abs(self, rel):
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    # -- hierarchy ----------------------------------------------------------

    @staticmethod
    def _strip(comp):
        return re.sub(r"[\[(]\d+[\])]$", "", comp)

    def resolve_scope(self, scope):
        """Module of a dump scope ('TOP.tb.u_dut.u_rob'). Returns
        (module, rest) where rest are trailing components that are not
        hierarchy (struct fields of a signal), or (None, None)."""
        comps = scope.split(".") if scope else []
        if self.hier:
            hit = self._resolve_hier(comps)
            if hit is not None:
                return hit
        k = 0
        while k < len(comps) and self._strip(comps[k]) not in self.modules:
            k += 1
        if k < len(comps):
            mod = self.modules[self._strip(comps[k])]
            k += 1
        else:
            # no scope is named after a module: start from a placed instance
            cut = next((c for c in range(len(comps), 0, -1) if ".".join(comps[:c]) in self.anchors), None)
            if cut is None:
                return None, None
            mod, k = self.modules[self.anchors[".".join(comps[:cut])]], cut
        while k < len(comps):
            c = self._strip(comps[k])
            inst = mod.instances.get(c)
            if inst is not None and inst.mtype in self.modules:
                mod = self.modules[inst.mtype]
            elif c in mod.gen_labels:
                pass  # generate scope: same module
            else:
                return mod, comps[k:]
            k += 1
        return mod, []

    def place_roots(self, names):
        """A filelist often holds the design without its testbench, so the
        scope of the DUT (tb.u_dut) is named after no module. Find it from the
        dump: the scope whose signals and child scopes are the ports and
        instances of a module that nothing in the index instantiates."""
        self.anchors = {}
        if self.hier:
            return
        kids = {}
        for n in names:
            comps = n.split(".")
            for k in range(1, len(comps)):
                kids.setdefault(".".join(comps[:k]), set()).add(self._strip(comps[k]))
        used = {i.mtype for m in self.modules.values() for i in m.instances.values()}
        roots = [m for m in self.modules.values() if m.name not in used]
        for scope in sorted(kids, key=lambda s: s.count(".")):
            if self.resolve_scope(scope)[0] is not None:
                continue
            scored = []
            for m in roots:
                parts = set(m.ports) | set(m.instances)
                hits = len(parts & kids[scope])
                if hits >= 2 and hits / len(parts) >= 0.6:
                    scored.append((hits / len(parts), m.name))
            scored.sort(reverse=True)
            if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
                self.anchors[scope] = scored[0][1]

    def _resolve_hier(self, comps):
        """Exact lookup in an elaborated instance tree. The dump may carry a
        wrapper scope the design does not (Verilator's TOP)."""
        roots = self._roots
        start = next((k for k, c in enumerate(comps) if c in roots), None)
        if start is None:
            return None
        for cut in range(len(comps), start, -1):
            key = ".".join(comps[start:cut])
            name = self.hier.get(key)
            if name is None or name not in self.modules:
                continue
            mod = self.modules[name]
            rest = comps[cut:]
            while rest and self._strip(rest[0]) in mod.gen_labels:
                rest = rest[1:]
            return mod, rest
        return None

    @property
    def _roots(self):
        r = getattr(self, "_roots_cache", None)
        if r is None or r[0] != len(self.hier):
            r = (len(self.hier), {k.split(".")[0] for k in self.hier})
            self._roots_cache = r
        return r[1]

    def parent_scope(self, scope):
        """Scope of the module instance that contains `scope`'s instance,
        skipping generate scopes. (parent_scope, instance name) or None."""
        comps = scope.split(".")
        while len(comps) > 1:
            last = comps.pop()
            parent = ".".join(comps)
            pm, rest = self.resolve_scope(parent)
            if pm is None or rest:
                return None
            if self._strip(last) in pm.instances:
                return parent, self._strip(last)
        return None

    def split_signal(self, full):
        """'scope.name' of a dump signal -> (scope, module, base name)."""
        comps = full.split(".")
        for cut in range(len(comps) - 1, 0, -1):
            scope = ".".join(comps[:cut])
            mod, rest = self.resolve_scope(scope)
            if mod is None:
                continue
            if rest:  # struct field scopes: the base signal is the first rest
                base = self._strip(rest[0])
                return ".".join(comps[:cut]), mod, base
            return scope, mod, self._strip(comps[cut])
        return None, None, None

    # -- lookups ------------------------------------------------------------

    def describe(self, mod, name):
        return {
            "module": mod.orig,
            "file": mod.file,
            "decl": mod.decls[name].as_dict() if name in mod.decls else None,
            "port_dir": mod.port_dir.get(name),
            "drivers": [loc.as_dict() for loc in mod.drivers.get(name, [])],
            "loads": [loc.as_dict() for loc in mod.loads.get(name, [])],
        }

    def trace(self, scope, name, loads=False, depth=0):
        """Drivers (or loads) of `name` in `scope`, following the hierarchy.
        Each result: {scope, signal, file, line, col, kind, via}."""
        mod, rest = self.resolve_scope(scope)
        if mod is None or depth > 32:
            return []
        out = []
        table = mod.loads if loads else mod.drivers
        for loc in table.get(name, []):
            d = loc.as_dict()
            d.update(scope=scope, signal=name)
            out.append(d)
        # through instances: an output port drives the connected signal, an
        # input port loads it
        want = "input" if loads else "output"
        for inst in mod.instances.values():
            sub = self.modules.get(inst.mtype)
            if sub is None:
                continue
            for port, expr, line, col in self._conns(inst, sub):
                if name not in [t.text for t in _ids_outside_index(expr)] and not (
                        loads and name in [t.text for t in _ids(expr)]):
                    continue
                pdir = sub.port_dir.get(port, "inout")
                if pdir not in (want, "inout"):
                    continue
                child = scope + "." + inst.name
                if inst.gen:
                    child = scope + "." + inst.gen + "." + inst.name  # best effort
                out.append({"scope": scope, "signal": name, "file": mod.file, "line": line, "col": col,
                            "kind": "port_" + pdir, "instance": inst.name, "module": inst.mtype, "port": port,
                            "child_scope": child})
        # an input port is driven by the parent's connection (loads of an
        # output port continue in the parent the same way)
        pdir = mod.port_dir.get(name)
        if pdir and ((not loads and pdir == "input") or (loads and pdir == "output")):
            up = self.parent_scope(scope)
            if up:
                pscope, iname = up
                pm, _ = self.resolve_scope(pscope)
                inst = pm.instances[iname]
                for port, expr, line, col in self._conns(inst, mod):
                    if port == name:
                        sigs = [t.text for t in _ids(expr)]
                        out.append({"scope": pscope, "signal": sigs[0] if sigs else None, "signals": sigs,
                                    "file": pm.file, "line": line, "col": col, "kind": "parent_conn",
                                    "instance": iname, "port": port})
        return out

    def _conns(self, inst, sub):
        conns = []
        named = {p for p, _, _, _ in inst.conns if p}
        for idx, (port, expr, line, col) in enumerate(inst.conns):
            if port is None:
                port = sub.ports[idx] if idx < len(sub.ports) else None
            if port:
                conns.append((port, expr, line, col))
        if inst.wildcard:
            for p in sub.ports:
                if p not in named:
                    conns.append((p, [Tok("id", p, inst.line, inst.col)], inst.line, inst.col))
        return conns

    def module_files(self, mod):
        """The files a module's code is in: its own and the `include ones
        its declarations, drivers, loads and instances point into."""
        files = [mod.file]
        for table in (mod.decls.values(), (l for v in mod.drivers.values() for l in v),
                      (l for v in mod.loads.values() for l in v), mod.instances.values()):
            for x in table:
                if x.file not in files:
                    files.append(x.file)
        return files

    def module_view(self, mod, file=None):
        """Identifiers in the module's line range, classified for a source
        view: signal / port / instance / module / param. With `file`, one of
        the `include files of the module instead, whole."""
        other = file is not None and file != mod.file
        if other and file not in self.module_files(mod):
            raise ValueError("%s is not a file of module %s" % (file, mod.orig))
        text = self.read_text(file if other else mod.file)
        idents = []
        sigs = set(mod.decls) | set(mod.drivers) | set(mod.loads)
        for line, col, name in ident_at_file(text):
            if not other and not mod.line <= line <= mod.end_line:
                continue
            if name in mod.instances:
                kind = "instance"
            elif name in self.modules:
                kind = "module"
            elif name in mod.params:
                kind = "param"
            elif name in sigs:
                kind = "signal"
            else:
                continue
            idents.append([line, col, name, kind])
        return {
            "module": mod.orig,
            "file": file if other else mod.file,
            "start": 1 if other else mod.line,
            "end": text.count("\n") + 1 if other else mod.end_line,
            "text": text,
            "idents": idents,
            "files": self.module_files(mod),
            "instances": {n: {"module": i.mtype, "line": i.line, "gen": i.gen} for n, i in mod.instances.items()},
        }


    # -- the open design JSON ---------------------------------------------------

    def to_doc(self):
        return self._doc()

    def save_json(self, path):
        doc = self._doc()
        if path in ("-", "/dev/stdout"):
            json.dump(doc, sys.stdout, separators=(",", ":"))
            sys.stdout.flush()
            return
        with open(path, "w") as fh:
            json.dump(doc, fh, separators=(",", ":"))

    def _doc(self):
        def locs(table):
            return {n: [[l.file, l.line, l.col, l.kind] for l in v] for n, v in table.items()}

        mods = {}
        for m in self.modules.values():
            mods[m.name] = {
                "orig": m.orig, "file": m.file, "line": m.line, "end": m.end_line,
                "kind": m.kind,
                "ports": [[p, m.port_dir.get(p, "inout")] for p in m.ports],
                "ifports": {p: list(v) for p, v in m.port_iface.items()},
                "modports": m.modports,
                "params": sorted(m.params),
                "decls": {n: [l.file, l.line, l.col, l.kind] for n, l in m.decls.items()},
                "drivers": locs(m.drivers), "loads": locs(m.loads),
                "gen": m.gen_labels,
                "instances": {
                    n: {"module": i.mtype, "file": i.file, "line": i.line, "col": i.col, "gen": i.gen,
                        "wildcard": i.wildcard,
                        "conns": [[p, [[t.kind, t.text, t.line, t.col] for t in toks], ln, col]
                                  for p, toks, ln, col in i.conns]}
                    for n, i in m.instances.items()},
            }
        return {"format": "waveview-design/1", "producer": self.producer, "root": self.root,
                "files": self.files, "modules": mods, "hier": self.hier}

    @classmethod
    def load_json(cls, path, root=None):
        with open(path) as fh:
            doc = json.load(fh)
        return cls.from_doc(doc, root=root, name=path)

    @classmethod
    def from_doc(cls, doc, root=None, name="design"):
        if doc.get("format") != "waveview-design/1":
            raise ValueError("%s is not a waveview design file" % name)
        idx = cls((), root=root or doc.get("root"), producer=doc.get("producer", "json"))
        idx.files = list(doc.get("files", []))
        for name, d in doc["modules"].items():
            m = Module(name, d["file"], d["line"])
            m.orig, m.end_line = d.get("orig", name), d.get("end", d["line"])
            m.kind = d.get("kind", "module")
            m.modports = {k: dict(v) for k, v in d.get("modports", {}).items()}
            m.port_iface = {p: (v[0], v[1] if len(v) > 1 else None) for p, v in d.get("ifports", {}).items()}
            for p, dr in d.get("ports", []):
                m.ports.append(p)
                m.port_dir[p] = dr
            m.params = set(d.get("params", []))
            m.decls = {n: Loc(v[0], v[1], v[2], v[3], {"dir": m.port_dir[n]} if n in m.port_dir else None)
                       for n, v in d.get("decls", {}).items()}
            m.drivers = {n: [Loc(*x) for x in v] for n, v in d.get("drivers", {}).items()}
            m.loads = {n: [Loc(*x) for x in v] for n, v in d.get("loads", {}).items()}
            m.gen_labels = dict(d.get("gen", {}))
            for n, i in d.get("instances", {}).items():
                inst = Instance(n, i["module"], i.get("file", m.file), i["line"], i.get("col", 0))
                inst.gen, inst.wildcard = i.get("gen"), i.get("wildcard", False)
                inst.conns = [(p, [Tok(*t) for t in toks], ln, col) for p, toks, ln, col in i.get("conns", [])]
                m.instances[n] = inst
            idx.modules[name] = m
        idx.hier = dict(doc.get("hier", {}))
        idx.resolve_iface_ports()
        return idx

    # -- Verilator's elaborated XML ------------------------------------------

    @classmethod
    def from_verilator_xml(cls, path, root=None):
        """Exact index from `verilator --xml-only`: every module as
        elaborated (one per parameter set), generate blocks unrolled, the
        instance tree, and each assignment's target at its exact column."""
        import xml.etree.ElementTree as ET

        idx = cls((), root=root, producer="verilator-xml")
        tree = ET.parse(path)
        files = {}
        for f in tree.iter("file"):
            fn = f.get("filename", "")
            files[f.get("id")] = idx.rel(fn) if fn and not fn.startswith("<") else None
        idx.files = sorted({v for v in files.values() if v})

        def loc(e):
            parts = (e.get("loc") or "").split(",")
            if len(parts) < 3 or files.get(parts[0]) is None:
                return None, 0, 0
            return files[parts[0]], int(parts[1]), max(0, int(parts[2]) - 1)

        for xm in tree.iter("module"):
            f, line, _ = loc(xm)
            if f is None:
                continue
            m = Module(xm.get("name"), f, line)
            m.orig = _vdecode(xm.get("origName") or xm.get("name"))
            end = line
            for e in xm.iter():
                ef, el, _ = loc(e)
                if ef == f and el > end:
                    end = el
            m.end_line = end
            _XmlModule(m, loc).walk(xm, None, [])
            idx.modules[m.name] = m

        def cells(e):
            for c in e.findall("cell"):
                idx.hier[_vdecode(c.get("hier"))] = c.get("submodname")
                cells(c)

        for cs in tree.iter("cells"):
            cells(cs)
        for m in idx.modules.values():
            for table in (m.drivers, m.loads):
                for v in table.values():
                    v.sort(key=lambda l: (l.file, l.line, l.col))
        return idx


def _vdecode(name):
    """Verilator's escaped names: loop__BRA__0__KET__ -> loop[0], __05F -> _"""
    if not name:
        return name
    name = name.replace("__BRA__", "[").replace("__KET__", "]").replace("__DOT__", ".")
    return re.sub(r"__0([0-9A-F]{2})", lambda m: chr(int(m.group(1), 16)), name)


class _XmlModule:
    """Fills a Module from one <module> element of Verilator's XML."""

    ASSIGNS = {"contassign": "assign", "assignw": "assign", "assignalias": "assign", "assigndly": "always_ff"}
    INDEXED = {"sel", "arraysel", "structsel", "membersel", "wordsel"}

    def __init__(self, m, loc):
        self.m, self.loc = m, loc
        self.seen = set()

    def _add(self, table, name, e, kind):
        f, line, col = self.loc(e)
        if f is None:
            return
        key = (id(table), name, line, col, kind)
        if key in self.seen:  # unrolled generate copies share their source
            return
        self.seen.add(key)
        self.m.add(table, name, Loc(f, line, col, kind))

    def walk(self, e, proc, gen):
        tag = e.tag
        if tag == "var":
            name = _vdecode(e.get("origName") or e.get("name")).split(".")[-1]
            f, line, col = self.loc(e)
            if f is None:
                return
            d = e.get("dir")
            if e.get("param") == "true" or e.get("localparam") == "true":
                self.m.params.add(name)
                self.m.decls.setdefault(name, Loc(f, line, col, "param"))
            elif d:
                d = {"in": "input", "out": "output"}.get(d, d)
                if name not in self.m.port_dir:
                    self.m.ports.append(name)
                self.m.port_dir[name] = d
                self.m.decls[name] = Loc(f, line, col, "port", {"dir": d})
            else:
                self.m.decls.setdefault(name, Loc(f, line, col, "decl"))
            for c in e:
                self.loads(c)
            return
        if tag == "begin" and e.get("name"):
            label = _vdecode(e.get("name"))
            base = Index._strip(label)
            if not base.startswith("unnamedblk"):
                f, line, _ = self.loc(e)
                self.m.gen_labels.setdefault(base, line)
                gen = gen + [base]
        if tag in ("always", "always_ff", "always_comb", "always_latch"):
            proc = "comb"
        elif tag in ("initial", "initialstatic", "final"):
            proc = "init"
        if tag in self.ASSIGNS or tag == "assign":
            kids = list(e)
            if len(kids) >= 2:
                kind = self.ASSIGNS.get(tag) or (proc or "comb")
                self.loads(kids[0])
                self.target(kids[1], kind)
                for c in kids[2:]:
                    self.loads(c)
            return
        if tag == "instance":
            self.instance(e, gen)
            return
        if tag in ("varref",):
            self._add(self.m.loads, _vdecode(e.get("name")), e, "read")
            return
        for c in e:
            self.walk(c, proc, gen)

    def target(self, e, kind):
        if e.tag == "varref":
            self._add(self.m.drivers, _vdecode(e.get("name")), e, kind)
            return
        if e.tag in self.INDEXED:
            kids = list(e)
            if kids:
                self.target(kids[0], kind)
                for c in kids[1:]:
                    self.loads(c)
            return
        for c in e:  # concat and friends: every part is a target
            self.target(c, kind)

    def loads(self, e):
        for v in e.iter("varref"):
            self._add(self.m.loads, _vdecode(v.get("name")), v, "read")

    def instance(self, e, gen):
        f, line, col = self.loc(e)
        name = _vdecode(e.get("origName") or e.get("name"))
        inst = self.m.instances.get(name)
        if inst is None:
            inst = Instance(name, e.get("defName"), f, line, col)
            inst.gen = gen[-1] if gen else None
            self.m.instances[name] = inst
        else:
            return  # another unrolled copy of the same instance statement
        for p in e.findall("port"):
            pf, pl, pc = self.loc(p)
            toks = []
            for c in p:
                self._port_toks(c, toks)
            inst.conns.append((p.get("name"), toks, pl, pc))
            if p.get("direction") == "in":
                for c in p:
                    self.loads(c)

    def _port_toks(self, e, toks):
        """Tokens of a port expression: base signals plain, index signals
        inside [ ], so that only the base counts as driven by an output."""
        if e.tag == "varref":
            f, line, col = self.loc(e)
            toks.append(Tok("id", _vdecode(e.get("name")), line, col))
        elif e.tag in self.INDEXED:
            kids = list(e)
            if kids:
                self._port_toks(kids[0], toks)
                idx_t = []
                for c in kids[1:]:
                    if c.tag == "const" and not list(c):
                        # a constant index: kept, so that bus[0] and bus[1] are different nets
                        v = (c.get("name") or "").split("h")[-1]
                        try:
                            v = str(int(v, 16))
                        except ValueError:
                            pass
                        idx_t.append(Tok("num", v, 0, 0))
                        continue
                    for v in c.iter("varref"):
                        f, line, col = self.loc(v)
                        idx_t.append(Tok("id", _vdecode(v.get("name")), line, col))
                if idx_t:
                    toks.append(Tok("op", "[", 0, 0))
                    toks.extend(idx_t)
                    toks.append(Tok("op", "]", 0, 0))
        else:
            for c in e:
                self._port_toks(c, toks)


def is_verdi_db(spec):
    """A Verdi KDB: vericom's work.lib++ or a VCS simv.daidir (-kdb)."""
    return spec.rstrip("/").endswith((".lib++", ".daidir"))


def is_elaborated(spec):
    """Whether --rtl names an elaborated design rather than sources."""
    return spec.endswith((".json", ".xml")) or is_verdi_db(spec)


def verdi_home():
    home = os.environ.get("VERDI_HOME")
    if home and os.path.isdir(home):
        return home
    import shutil

    exe = shutil.which("verdi") or shutil.which("vericom")
    if exe:
        home = os.path.dirname(os.path.dirname(os.path.realpath(exe)))
        if os.path.isdir(os.path.join(home, "share", "NPI")):
            return home
    return None


def export_verdi(spec, quiet=True):
    """Design JSON for a Verdi KDB, written by npi_export.py under Verdi's
    own Python with its NPI libraries, and cached until the KDB changes.
    Needs VERDI_HOME (or verdi on PATH) and a Verdi license
    (SNPSLMD_LICENSE_FILE). Returns the JSON path."""
    import glob
    import hashlib
    import subprocess

    spec = os.path.abspath(spec.rstrip("/"))
    if not os.path.isdir(spec):
        raise FileNotFoundError("no such KDB: %s" % spec)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    cache_dir = os.path.join(base, "waveview", "design")
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, hashlib.sha1(spec.encode()).hexdigest()[:16] + ".json")
    newest = os.stat(spec).st_mtime
    for n in os.listdir(spec):
        try:
            newest = max(newest, os.stat(os.path.join(spec, n)).st_mtime)
        except OSError:
            pass
    if os.path.isfile(out) and os.stat(out).st_mtime >= newest:
        return out
    home = verdi_home()
    if home is None:
        raise RuntimeError("%s is a Verdi KDB: set VERDI_HOME (or put verdi on PATH) to export it" % spec)
    script = os.environ.get("WAVEVIEW_NPI_EXPORT") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                     "npi_export.py")
    if not os.path.isfile(script):
        raise RuntimeError("npi_export.py not found (looked at %s)" % script)
    pythons = sorted(glob.glob(os.path.join(home, "platform", "linux64", "python-3.*", "bin", "python3")))
    python = pythons[-1] if pythons else "python3"
    env = dict(os.environ)
    env["VERDI_HOME"] = home
    env.setdefault("NOVAS_HOME", home)
    env.setdefault("SNPSLMD_QUEUE", "true")
    libs = [os.path.join(home, "share", "NPI", "lib", "linux64"), os.path.join(home, "platform", "linux64", "bin")]
    if pythons:
        libs.append(os.path.join(os.path.dirname(os.path.dirname(python)), "lib"))
    env["LD_LIBRARY_PATH"] = ":".join(libs + [env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else libs)
    tmp = out + ".tmp"
    cmd = [python, script, spec, "-o", tmp] + (["--quiet"] if quiet else [])
    # stdout/stderr spelled out: this may run on a server's Python 3.6
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env,
                          cwd=os.path.dirname(spec))
    if done.returncode != 0 or not os.path.isfile(tmp):
        tail = (done.stderr or done.stdout).strip().split("\n")
        tail = "\n".join(l for l in tail if l.strip() and "Synopsys" not in l and "license agreement" not in l)[-1500:]
        raise RuntimeError("exporting %s with Verdi's NPI failed (%s):\n%s" % (spec, " ".join(cmd[:2]), tail))
    os.replace(tmp, out)
    return out


def load_any(specs, root=None):
    """An Index from what the user pointed at: a design JSON, a Verilator
    XML, a Verdi KDB (work.lib++ or simv.daidir, exported through Verdi's
    NPI), or sources (.f filelists, files, directories)."""
    if len(specs) == 1 and is_verdi_db(specs[0]):
        return Index.load_json(export_verdi(specs[0]), root=root)
    if len(specs) == 1 and specs[0].endswith(".json"):
        return Index.load_json(specs[0], root=root)
    if len(specs) == 1 and specs[0].endswith(".xml"):
        return Index.from_verilator_xml(specs[0], root=root)
    return Index(collect_sources(specs), root=root)


# ---------------------------------------------------------------------------
# Self test and CLI
# ---------------------------------------------------------------------------

SELFTEST_SRC = r"""
module leaf #(parameter int W = 8) (
  input  logic         i_clk,
  input  logic [W-1:0] i_d,
  output logic [W-1:0] o_q
);
  logic [W-1:0] r_q;
  always_ff @(posedge i_clk) begin
    r_q <= i_d;          // driver of r_q
  end
  assign o_q = r_q;
endmodule

module top (input logic clk, input logic [7:0] din, output logic [7:0] dout);
  typedef struct packed { logic v; logic [6:0] d; } pkt_t;
  pkt_t   w_pkt;
  logic [7:0] w_mid, w_arr [2];
  logic   w_a, w_b;
  `ifdef FOO
  assign w_a = 1'b0;
  `else
  assign w_a = din[0];
  `endif
  always_comb begin
    w_pkt = '0;
    if (din[1] <= w_a) begin
      {w_b, w_pkt.d[0]} = 2'b10;
    end
  end
  leaf #(.W(8)) u_leaf (.i_clk(clk), .i_d(din), .o_q(w_mid));
  for (genvar g = 0; g < 2; g++) begin : gen_lane
    leaf u_l (.i_clk, .i_d(w_mid), .o_q(w_arr[g]));
  end
  assign dout = w_mid ^ w_arr[0];
endmodule
"""


SELFTEST_XML = """<?xml version="1.0" ?>
<verilator_xml>
  <files><file id="a" filename="@SRC@" language="1800-2017"/></files>
  <netlist>
    <module loc="a,2,8,2,12" name="leaf__W8" origName="leaf">
      <var loc="a,2,28,2,29" name="W" dtype_id="1" vartype="int" origName="W" param="true"/>
      <var loc="a,3,24,3,29" name="i_clk" dtype_id="2" dir="input" vartype="logic" origName="i_clk"/>
      <var loc="a,4,24,4,27" name="i_d" dtype_id="3" dir="input" vartype="logic" origName="i_d"/>
      <var loc="a,5,24,5,27" name="o_q" dtype_id="3" dir="output" vartype="logic" origName="o_q"/>
      <var loc="a,7,17,7,20" name="r_q" dtype_id="3" vartype="logic" origName="r_q"/>
      <always loc="a,8,3,8,12">
        <assigndly loc="a,9,9,9,11">
          <varref loc="a,9,12,9,15" name="i_d" dtype_id="3"/>
          <varref loc="a,9,5,9,8" name="r_q" dtype_id="3"/>
        </assigndly>
      </always>
      <contassign loc="a,11,14,11,15">
        <varref loc="a,11,16,11,19" name="r_q" dtype_id="3"/>
        <varref loc="a,11,10,11,13" name="o_q" dtype_id="3"/>
      </contassign>
    </module>
    <module loc="a,14,8,14,11" name="top" origName="top">
      <var loc="a,14,25,14,28" name="clk" dtype_id="2" dir="input" vartype="logic" origName="clk"/>
      <var loc="a,14,48,14,51" name="din" dtype_id="3" dir="input" vartype="logic" origName="din"/>
      <var loc="a,17,15,17,20" name="w_mid" dtype_id="3" vartype="logic" origName="w_mid"/>
      <var loc="a,17,22,17,27" name="w_arr" dtype_id="4" vartype="logic" origName="w_arr"/>
      <var loc="a,18,11,18,14" name="w_a" dtype_id="2" vartype="logic" origName="w_a"/>
      <contassign loc="a,22,14,22,15">
        <sel loc="a,22,19,22,20"><varref loc="a,22,16,22,19" name="din" dtype_id="3"/><const loc="a,22,20,22,21" name="1'h0"/></sel>
        <varref loc="a,22,10,22,13" name="w_a" dtype_id="2"/>
      </contassign>
      <instance loc="a,30,17,30,23" name="u_leaf" defName="leaf__W8" origName="u_leaf">
        <port loc="a,30,26,30,31" name="i_clk" direction="in"><varref loc="a,30,32,30,35" name="clk"/></port>
        <port loc="a,30,38,30,41" name="i_d" direction="in"><varref loc="a,30,42,30,45" name="din"/></port>
        <port loc="a,30,48,30,51" name="o_q" direction="out"><varref loc="a,30,52,30,57" name="w_mid"/></port>
      </instance>
      <begin loc="a,31,3,31,6" name="gen_lane[1]">
        <instance loc="a,32,10,32,13" name="u_l" defName="leaf__W8" origName="u_l">
          <port loc="a,32,16,32,21" name="i_clk" direction="in"><varref loc="a,32,16,32,21" name="clk"/></port>
          <port loc="a,32,24,32,27" name="i_d" direction="in"><varref loc="a,32,28,32,33" name="w_mid"/></port>
          <port loc="a,32,37,32,40" name="o_q" direction="out">
            <arraysel loc="a,32,46,32,47"><varref loc="a,32,41,32,46" name="w_arr"/><varref loc="a,32,47,32,48" name="g"/></arraysel>
          </port>
        </instance>
      </begin>
    </module>
  </netlist>
  <cells>
    <cell loc="a,14,8,14,11" name="top" submodname="top" hier="top">
      <cell loc="a,30,17,30,23" name="u_leaf" submodname="leaf__W8" hier="top.u_leaf"/>
      <cell loc="a,32,10,32,13" name="u_l" submodname="leaf__W8" hier="top.gen_lane__BRA__1__KET__.u_l"/>
    </cell>
  </cells>
</verilator_xml>
"""


def run_selftest():
    import tempfile

    d = tempfile.mkdtemp(prefix="rtlindex_selftest_")
    p = os.path.join(d, "t.sv")
    with open(p, "w") as fh:
        fh.write(SELFTEST_SRC)
    idx = Index([p], root=d)
    fails = 0

    def check(label, ok):
        nonlocal fails
        print("%-4s %s" % ("ok" if ok else "FAIL", label))
        fails += 0 if ok else 1

    top, leaf = idx.modules["top"], idx.modules["leaf"]
    check("modules", set(idx.modules) == {"top", "leaf"} and not idx.errors)
    check("ansi ports", leaf.ports == ["i_clk", "i_d", "o_q"] and leaf.port_dir["o_q"] == "output")
    check("param", "W" in leaf.params)
    check("always_ff driver", [loc.kind for loc in leaf.drivers["r_q"]] == ["always_ff"])
    check("assign driver line", leaf.drivers["o_q"][0].line == 11)
    check("both ifdef branches", len(top.drivers["w_a"]) == 2)
    check("<= inside if() is a compare", "din" in top.loads and "din" not in top.drivers)
    check("concat lhs", "w_b" in top.drivers and any(loc.line == 27 for loc in top.drivers["w_pkt"]))
    check("typed decl", "w_pkt" in top.decls and "w_arr" in top.decls and "w_mid" in top.decls)
    check("instances", set(top.instances) == {"u_leaf", "u_l"} and top.instances["u_l"].gen == "gen_lane")
    check("scope resolve", idx.resolve_scope("TOP.top.u_leaf")[0] is leaf
          and idx.resolve_scope("TOP.top.gen_lane[1].u_l")[0] is leaf)
    check("struct field scope", idx.split_signal("TOP.top.w_pkt.v")[2] == "w_pkt")
    tr = idx.trace("TOP.top", "w_mid")
    check("trace into instance output", any(t["kind"] == "port_output" and t["port"] == "o_q" for t in tr))
    tr = idx.trace("TOP.top.u_leaf", "i_d")
    check("trace input up to parent", any(t["kind"] == "parent_conn" and t["signal"] == "din" for t in tr))
    tr = idx.trace("TOP.top.gen_lane[0].u_l", "i_clk")
    check("implicit .port up through generate", any(t["kind"] == "parent_conn" and t["signal"] == "i_clk" for t in tr))
    loads = idx.trace("TOP.top", "w_mid", loads=True)
    check("loads include instance inputs", any(t["kind"] == "port_input" for t in loads))
    v = idx.module_view(leaf)
    check("module view idents", any(i[2] == "r_q" and i[3] == "signal" for i in v["idents"]))
    # `include: code pulled in from another file keeps its own positions
    os.makedirs(os.path.join(d, "inc"))
    with open(os.path.join(d, "inc", "body.svh"), "w") as fh:
        fh.write("logic [7:0] w_inc;\nassign w_inc = din ^ 8'h5a;\nleaf u_inc (.i_clk(clk), .i_d(w_inc), .o_q());\n")
    with open(os.path.join(d, "top2.sv"), "w") as fh:
        fh.write('module top2 (input logic clk, input logic [7:0] din);\n`include "body.svh"\nendmodule\n')
    with open(os.path.join(d, "list.f"), "w") as fh:
        fh.write("+incdir+inc\nt.sv\ntop2.sv\n")
    srcs = collect_sources([os.path.join(d, "list.f")])
    idx2 = Index(srcs, root=d)
    t2 = idx2.modules.get("top2")
    check("include: +incdir+ from the filelist", os.path.join(d, "inc") in srcs.incdirs)
    check("include: declarations, drivers and instances placed in the included file",
          t2 is not None and t2.decls["w_inc"].file == "inc/body.svh" and t2.decls["w_inc"].line == 1
          and t2.drivers["w_inc"][0].file == "inc/body.svh" and t2.drivers["w_inc"][0].line == 2
          and "u_inc" in t2.instances and t2.instances["u_inc"].file == "inc/body.svh")
    check("include: the module keeps its own file and end line", t2 is not None and t2.file == "top2.sv" and t2.end_line == 3)
    check("include: the included file is watched and viewable",
          os.path.join(d, "inc", "body.svh") in idx2.files
          and idx2.module_view(t2, "inc/body.svh")["text"].startswith("logic [7:0] w_inc")
          and any(i[2] == "u_inc" and i[3] == "instance" for i in idx2.module_view(t2, "inc/body.svh")["idents"]))
    # SystemVerilog interfaces: modports, interface-typed ports, instances
    with open(os.path.join(d, "bus.sv"), "w") as fh:
        fh.write("interface bus_if (input logic clk);\n  logic valid, ready; logic [7:0] data;\n"
                 "  modport mst (output valid, data, input ready);\n  modport slv (input valid, data, output ready);\n"
                 "endinterface\n"
                 "module src_m (input logic clk, bus_if.mst m, output logic busy);\n  assign m.valid = 1'b1;\nendmodule\n"
                 "module dst_m (input logic clk, bus_if.slv s, pkt_t cfg);\nendmodule\n"
                 "module top3 (input logic clk);\n  bus_if bus_i (.clk(clk));\n  src_m u_src (.clk(clk), .m(bus_i), .busy());\n"
                 "  dst_m u_dst (.clk(clk), .s(bus_i), .cfg());\nendmodule\n")
    idx3 = Index([os.path.join(d, "bus.sv")], root=d)
    bus, src, dst, top3 = (idx3.modules.get(k) for k in ("bus_if", "src_m", "dst_m", "top3"))
    check("interface: parsed with its modports", bus is not None and bus.kind == "interface"
          and bus.modports.get("mst") == {"valid": "output", "data": "output", "ready": "input"})
    check("interface: typed ports resolved, a typedef port is not one",
          src is not None and src.port_iface.get("m") == ("bus_if", "mst") and dst.port_iface.get("s") == ("bus_if", "slv")
          and "cfg" not in dst.port_iface and dst.port_type.get("cfg") == ("pkt_t", None))
    check("interface: modport tells the way", idx3.iface_port_dir("bus_if", "mst") == "output"
          and idx3.iface_port_dir("bus_if", "slv") == "input" and idx3.iface_port_dir("bus_if", None) is None)
    check("interface: instance and connections", top3 is not None and top3.instances["bus_i"].mtype == "bus_if"
          and idx3.resolve_scope("top3.bus_i")[0] is bus)
    back3 = Index.from_doc(idx3.to_doc())
    check("interface: design JSON round trip", back3.modules["src_m"].port_iface.get("m") == ("bus_if", "mst")
          and back3.modules["bus_if"].modports == bus.modports and back3.modules["bus_if"].kind == "interface")
    check("verdi KDB names", is_verdi_db("/x/work.lib++") and is_verdi_db("sim/simv.daidir/")
          and is_elaborated("d.json") and not is_verdi_db("rtl/x.f"))
    js = os.path.join(d, "design.json")
    idx.save_json(js)
    back = Index.load_json(js)
    check("design JSON round trip", back.trace("TOP.top", "w_mid") == idx.trace("TOP.top", "w_mid")
          and back.trace("TOP.top.u_leaf", "i_d") == idx.trace("TOP.top.u_leaf", "i_d"))
    xml = os.path.join(d, "t.xml")
    with open(xml, "w") as fh:
        fh.write(SELFTEST_XML.replace("@SRC@", p))
    vx = Index.from_verilator_xml(xml, root=d)
    check("verilator xml: modules and exact tree", set(vx.modules) == {"top", "leaf__W8"}
          and vx.hier["top.gen_lane[1].u_l"] == "leaf__W8" and vx.modules["leaf__W8"].orig == "leaf")
    check("verilator xml: drivers", [(l.line, l.kind) for l in vx.modules["leaf__W8"].drivers["r_q"]] == [(9, "always_ff")]
          and [l.kind for l in vx.modules["top"].drivers["w_a"]] == ["assign"])
    tr = vx.trace("TOP.top", "w_mid")
    check("verilator xml: trace into instance", any(t["kind"] == "port_output" and t["port"] == "o_q" for t in tr))
    tr = vx.trace("TOP.top.gen_lane[1].u_l", "i_d")
    check("verilator xml: input up through generate", any(t["kind"] == "parent_conn" and t["signal"] == "w_mid" for t in tr))
    check("verilator xml: index is not driven", "g" not in [t.text for t in _ids_outside_index(
        next(c for c in vx.modules["top"].instances["u_l"].conns if c[0] == "o_q")[1])])
    print("selftest: %s" % ("PASS" if not fails else "%d FAILED" % fails))
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sources", nargs="*", help=".f filelists, source files or directories")
    ap.add_argument("--module")
    ap.add_argument("--trace", help="hierarchical signal name to trace")
    ap.add_argument("--loads", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("-o", "--output", help="write the index as design JSON (waveview serve --rtl <it>); - for stdout")
    ap.add_argument("--quiet", action="store_true", help="no summary (with -o -)")
    a = ap.parse_args(argv)
    if a.selftest:
        return run_selftest()
    idx = load_any(a.sources or ["."])
    if a.output:
        idx.save_json(a.output)
        if a.quiet:
            return 0
        print("wrote %s (%s)" % (a.output, idx.producer))
    print("%s: %d files, %d modules%s" % (idx.producer, len(idx.files), len(idx.modules),
                                       ", %d errors" % len(idx.errors) if idx.errors else ""))
    for e in idx.errors[:5]:
        print("  " + e)
    if a.module:
        m = idx.modules[a.module]
        print("%s  %s:%d-%d" % (m.name, m.file, m.line, m.end_line))
        print("ports: %s" % ", ".join("%s %s" % (m.port_dir[p], p) for p in m.ports))
        print("instances: %s" % ", ".join("%s:%s" % (n, i.mtype) for n, i in m.instances.items()))
        print("signals: %d declared, %d driven" % (len(m.decls), len(m.drivers)))
    if a.trace:
        scope, mod, name = idx.split_signal(a.trace)
        if mod is None:
            print("cannot place %s in the source hierarchy" % a.trace)
            return 1
        print("%s in %s (%s)" % (name, scope, mod.name))
        for t in idx.trace(scope, name, loads=a.loads):
            print("  %-12s %s:%d  %s" % (t["kind"], t["file"], t["line"], t.get("instance") or t.get("signal") or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
