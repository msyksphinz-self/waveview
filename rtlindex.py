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
    tools/rtlindex.py core/filelist.f --module scariv_rob
    tools/rtlindex.py core/filelist.f --trace TOP.tb_scariv_top.u_dut.w_rob_head
    tools/rtlindex.py --selftest
"""

from __future__ import annotations

import argparse
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
    __slots__ = ("kind", "text", "line", "col")

    def __init__(self, kind, text, line, col):
        self.kind, self.text, self.line, self.col = kind, text, line, col

    def __repr__(self):
        return "%s@%d:%d" % (self.text, self.line, self.col)


def tokenize(text):
    """Tokens with 1-based line and 0-based column. Comments and directive
    lines are dropped (both branches of `ifdef stay)."""
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
        self.end_line = line
        self.ports = []  # [name] in order
        self.port_dir = {}  # name -> input/output/inout
        self.decls = {}  # name -> Loc
        self.drivers = {}  # name -> [Loc]
        self.loads = {}  # name -> [Loc]
        self.instances = {}  # name -> Instance
        self.gen_labels = {}  # label -> line
        self.params = set()

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
    def __init__(self, path, text):
        self.path = path
        self.toks = tokenize(text)
        self.modules = []

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
                    m = Module(toks[j].text, self.path, toks[j].line)
                    m.end_line = toks[min(k, len(toks) - 1)].line
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
                m.decls.setdefault(n.text, Loc(self.path, n.line, n.col, "param"))
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
            m.decls[name.text] = Loc(self.path, name.line, name.col, "port", {"dir": direction or "inout"})

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
                m.decls[name.text] = Loc(self.path, name.line, name.col, kind, detail)
            else:
                m.decls.setdefault(name.text, Loc(self.path, name.line, name.col, kind, detail))
            if eq < len(item):
                m.add(m.drivers, name.text, Loc(self.path, name.line, name.col, "init"))
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
        inst = Instance(name_tok.text, mtype, self.path, name_tok.line, name_tok.col)
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
            m.add(m.drivers, t.text, Loc(self.path, t.line, t.col, kind))
        # everything else is read
        self._loads(m, seg[:j + 1])
        idx = [t for t in lhs if t not in targets]
        self._loads(m, idx)
        self._loads(m, seg[k + 1:])

    def _loads(self, m, toks):
        for t in _ids(toks):
            m.add(m.loads, t.text, Loc(self.path, t.line, t.col, "read"))


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def read_filelist(path, seen=None):
    """Source files of a .f file (nested -f supported); options are ignored."""
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
    while k < len(words):
        w = words[k]
        if w in ("-f", "-F") and k + 1 < len(words):
            nested = words[k + 1]
            cand = nested if os.path.isfile(nested) else os.path.join(base, nested)
            out += read_filelist(cand, seen)
            k += 2
            continue
        if w.startswith(("-", "+")):
            k += 1
            continue
        if w.endswith((".sv", ".v", ".svh", ".vh")):
            cand = w if os.path.isfile(w) else os.path.join(base, w)
            if os.path.isfile(cand):
                out.append(os.path.abspath(cand))
        k += 1
    return out


def collect_sources(specs):
    files = []
    for spec in specs:
        if os.path.isdir(spec):
            for root, dirs, names in os.walk(spec):
                dirs[:] = [d for d in dirs if not d.startswith((".", "obj_dir")) and d != "node_modules"]
                for n in sorted(names):
                    if n.endswith((".sv", ".v")):
                        files.append(os.path.abspath(os.path.join(root, n)))
        elif spec.endswith((".f", ".F")):
            files += read_filelist(spec)
        elif os.path.isfile(spec):
            files.append(os.path.abspath(spec))
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


class Index:
    def __init__(self, files, root=None):
        self.root = os.path.abspath(root or os.getcwd())
        self.modules = {}
        self.files = files
        self.errors = []
        for f in files:
            try:
                with open(f, errors="replace") as fh:
                    text = fh.read()
                for m in Parser(self.rel(f), text).parse():
                    self.modules.setdefault(m.name, m)
            except Exception as exc:  # noqa: BLE001
                self.errors.append("%s: %s" % (f, exc))

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
        k = 0
        while k < len(comps) and self._strip(comps[k]) not in self.modules:
            k += 1
        if k == len(comps):
            return None, None
        mod = self.modules[self._strip(comps[k])]
        k += 1
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
            "module": mod.name,
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

    def module_view(self, mod):
        """Identifiers in the module's line range, classified for a source
        view: signal / port / instance / module / param."""
        path = self.abs(mod.file)
        with open(path, errors="replace") as fh:
            text = fh.read()
        idents = []
        sigs = set(mod.decls) | set(mod.drivers) | set(mod.loads)
        for line, col, name in ident_at_file(text):
            if not mod.line <= line <= mod.end_line:
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
            "module": mod.name,
            "file": mod.file,
            "start": mod.line,
            "end": mod.end_line,
            "text": text,
            "idents": idents,
            "instances": {n: {"module": i.mtype, "line": i.line, "gen": i.gen} for n, i in mod.instances.items()},
        }


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
    print("selftest: %s" % ("PASS" if not fails else "%d FAILED" % fails))
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sources", nargs="*", help=".f filelists, source files or directories")
    ap.add_argument("--module")
    ap.add_argument("--trace", help="hierarchical signal name to trace")
    ap.add_argument("--loads", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return run_selftest()
    idx = Index(collect_sources(a.sources or ["."]))
    print("%d files, %d modules%s" % (len(idx.files), len(idx.modules),
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
