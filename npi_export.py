#!/usr/bin/env python3
"""Export a Verdi KDB (vericom work.lib++ or a VCS simv.daidir compiled
with -kdb) as waveview design JSON (docs/waveview_design_format.md), through
Verdi's Python NPI.

    npi_export.py work.lib++ -o design.json
    npi_export.py simv.daidir -o design.json
    waveview.py serve w.fsdb --rtl design.json

Runs with Verdi's own Python ($VERDI_HOME/platform/linux64/python-3.11), its
NPI libraries and a Verdi license (SNPSLMD_LICENSE_FILE); rtlindex.py's
export_verdi() sets that up and calls this. What it takes from the KDB is
what the source heuristics cannot know for sure: the elaborated instance
tree (generate scopes unrolled, one module entry per parameter set), every
module's file and lines, ports with directions, declarations, the targets of
continuous assignments and of the statements inside processes, and each
instance's port connections. Reads (loads) are found in the source text of
the module, as the heuristic index does.
"""

import json
import os
import re
import sys

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
KEYWORDS = set("""always always_comb always_ff always_latch assign begin case casex casez default else end endcase
endfunction endmodule endtask for function if initial input logic output reg wire posedge negedge or and not
int integer unsigned signed localparam parameter return task module endgenerate generate genvar typedef enum
struct packed bit byte""".split())


def _try(obj, meth, *args):
    try:
        f = getattr(obj, meth, None)
        return f(*args) if f else None
    except Exception:  # noqa: BLE001
        return None


def _type(h):
    return _try(h, "type") or ""


def _name(h):
    return _try(h, "name") or ""


class Export:
    def __init__(self, lang, root):
        self.lang = lang
        self.root = root
        self.modules = {}   # key -> module dict
        self.hier = {}      # instance path -> key
        self.files = set()
        self.texts = {}
        self.unknown = {}

    # -- files ---------------------------------------------------------------

    def rel(self, path):
        if not path:
            return path
        path = os.path.abspath(path)
        if self.root and (path + "/").startswith(self.root.rstrip("/") + "/"):
            return os.path.relpath(path, self.root)
        return path

    def absf(self, f):
        return f if os.path.isabs(f) else os.path.join(self.root, f)

    def text(self, path):
        if path not in self.texts:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    self.texts[path] = fh.read().split("\n")
            except OSError:
                self.texts[path] = []
        return self.texts[path]

    def col_of(self, path, line, name):
        lines = self.text(path)
        if 1 <= line <= len(lines) and name:
            lead = r"\b" if (name[0].isalnum() or name[0] == "_") else ""
            m = re.search(lead + re.escape(name) + r"\b", lines[line - 1])
            if m:
                return m.start()
        return 0

    def end_of(self, path, line):
        lines = self.text(path)
        depth = 0
        for i in range(max(0, line - 1), len(lines)):
            for w in IDENT.findall(re.sub(r"//.*", "", lines[i])):
                if w in ("module", "interface", "program", "package"):
                    depth += 1
                elif w in ("endmodule", "endinterface", "endprogram", "endpackage"):
                    depth -= 1
                    if depth <= 0:
                        return i + 1
        return len(lines) or line

    # -- expressions ---------------------------------------------------------

    def lhs_names(self, h):
        """Base identifiers assigned by an lvalue: a name, a bit/part select
        (its base), a concatenation (each member)."""
        if h is None:
            return []
        t = _type(h)
        if t in ("npiPartSelect", "npiBitSelect", "npiIndexedPartSelect", "npiVarSelect", "npiVarBit"):
            base = _try(h, "parent_handle") or _try(h, "actual_handle")
            if base is not None and _name(base):
                return [_name(base).split(".")[-1]]
            text = _try(h, "decompile") or _name(h)
            m = IDENT.match(text or "")
            return [m.group(0)] if m else []
        if t in ("npiConcatenation", "npiOperation", "npiAssignmentPattern"):
            out = []
            for o in _try(h, "operand_handles") or []:
                out += self.lhs_names(o)
            if out:
                return out
            text = _try(h, "decompile") or ""
            return [w for w in IDENT.findall(text) if w not in KEYWORDS]
        n = _name(h)
        if n:
            return [n.split(".")[-1]]
        text = _try(h, "decompile") or ""
        m = IDENT.match(text)
        return [m.group(0)] if m else []

    # -- statements ----------------------------------------------------------

    def walk_stmts(self, h, kind, out, depth=0):
        """Find the assignments inside a statement tree; out gets
        (name, line, kind)."""
        if h is None or depth > 64:
            return
        t = _type(h)
        lhs = _try(h, "lhs_handle")
        if lhs is not None and t not in ("npiContAssign",):
            k = kind
            if t in ("npiAssignment", "npiAssignStmt"):
                blocking = _try(h, "blocking")
                if kind == "always_ff" and blocking:
                    k = "comb"
                elif kind == "comb" and blocking is False:
                    k = "always_ff"
            line = _try(h, "line_no") or 0
            for n in self.lhs_names(lhs):
                out.append((n, line, k))
        for meth in ("stmt_handles", "stmt_handle", "else_stmt_handle", "case_item_handles", "for_init_stmt_handles",
                     "for_inc_stmt_handles", "alias_stmt_handles"):
            kids = _try(h, meth)
            if kids is None:
                continue
            if not isinstance(kids, (list, tuple)):
                kids = [kids]
            for k in kids:
                self.walk_stmts(k, kind, out, depth + 1)

    # -- modules -------------------------------------------------------------

    def module_key(self, scope):
        """One entry per module definition and parameter set."""
        name = _try(scope, "def_name") or _name(scope)
        vals = []
        for p in _try(scope, "parameter_handles") or []:
            v = _try(p, "value")
            if v is None:
                v = _try(p, "int_value")
            vals.append("%s=%s" % (_name(p), v))
        if not vals:
            return name, name
        sig = ",".join(vals)
        key = self.param_keys.setdefault(name, {}).get(sig)
        if key is None:
            n = len(self.param_keys[name])
            key = name if n == 0 else "%s__%d" % (name, n)
            self.param_keys[name][sig] = key
        return name, key

    param_keys = {}

    def module_of(self, scope):
        """The module dict for an instance scope, built on first sight."""
        name, key = self.module_key(scope)
        if key in self.modules:
            return key
        dfile = _try(scope, "def_file") or _try(scope, "file") or ""
        dline = _try(scope, "def_line_no") or _try(scope, "line_no") or 1
        self.files.add(dfile)
        m = {"orig": name, "file": self.rel(dfile), "line": dline, "end": self.end_of(dfile, dline),
             "ports": [], "params": [], "decls": {}, "drivers": {}, "loads": {}, "gen": {}, "instances": {}}
        self.modules[key] = m
        declared = set()

        def decl(h, kind, extra=None):
            n = _name(h).split(".")[-1]
            if not n or n in declared:
                return
            declared.add(n)
            f = _try(h, "file") or dfile
            ln = _try(h, "line_no") or dline
            self.files.add(f)
            m["decls"][n] = [self.rel(f), ln, self.col_of(f, ln, n), kind]

        m["kind"] = "interface" if _type(scope) == "npiInterface" else "module"
        m["ifports"] = {}
        m["modports"] = {}
        for p in _try(scope, "port_handles") or []:
            d = _try(p, "direction") or ""
            d = {"npiInput": "input", "npiOutput": "output", "npiInout": "inout"}.get(d, "inout")
            m["ports"].append([_name(p), d])
            decl(p, "port")
            # an interface port: what it is wired to is an interface instance
            lc = _try(p, "low_conn_handle")
            hc = _try(p, "high_conn_handle")
            for h in (lc, hc):
                if h is not None and _type(h) in ("npiInterface", "npiModport", "npiInterfaceArray"):
                    iface = _try(h, "def_name") or _try(_try(h, "parent_handle"), "def_name")
                    mp = _try(h, "modport_handle") if _type(h) != "npiModport" else h
                    if iface:
                        m["ifports"][_name(p)] = [iface, _name(mp) if mp is not None else None]
                    break
        for mp in _try(scope, "modport_handles") or []:
            dirs = {}
            for mpp in _try(mp, "mp_port_handles") or _try(mp, "port_handles") or []:
                d = _try(mpp, "direction") or ""
                dirs[_name(mpp)] = {"npiInput": "input", "npiOutput": "output", "npiInout": "inout"}.get(d, "inout")
            if dirs:
                m["modports"][_name(mp)] = dirs
        for p in _try(scope, "parameter_handles") or []:
            m["params"].append(_name(p).split(".")[-1])
            decl(p, "param")
        for meth in ("net_handles", "variables_handles", "array_net_handles", "variables_handle", "named_event_handles"):
            hs = _try(scope, meth)
            if hs is None:
                continue
            if not isinstance(hs, (list, tuple)):
                hs = [hs]
            for h in hs:
                decl(h, "decl")

        self.drivers_in(scope, m, dfile)
        # generate scopes and instances are filled by walk(); loads at the end
        return key

    def drivers_in(self, scope, m, dfile):
        """The assignments of a module body or a generate scope: continuous
        assignments and the statements of its processes."""

        def drive(n, f, ln, kind):
            self.files.add(f)
            m["drivers"].setdefault(n, []).append([self.rel(f), ln, self.col_of(f, ln, n), kind])

        for a in _try(scope, "cont_assign_handles") or []:
            f = _try(a, "file") or dfile
            ln = _try(a, "line_no") or 0
            for n in self.lhs_names(_try(a, "lhs_handle")):
                drive(n, f, ln, "assign")
        for p in _try(scope, "process_handles") or []:
            t = _type(p)
            at = str(_try(p, "always_type") or "")
            if t == "npiInitial":
                kind = "init"
            elif "FF" in at:
                kind = "always_ff"
            elif "Comb" in at or "Latch" in at:
                kind = "comb"
            else:
                kind = "always_ff" if self.has_edge(p) else "comb"
            f = _try(p, "file") or dfile
            found = []
            self.walk_stmts(_try(p, "stmt_handle") or p, kind, found)
            for n, ln, k in found:
                drive(n, f, ln or (_try(p, "line_no") or 0), k)

    def finish(self):
        """Drivers of names that are not declared are dropped (a hierarchical
        or package name), then the loads are read from the text."""
        for m in self.modules.values():
            m["drivers"] = {n: v for n, v in m["drivers"].items() if n in m["decls"]}
            self.loads_from_text(m)

    def has_edge(self, proc):
        text = _try(proc, "decompile") or ""
        return bool(re.search(r"\b(posedge|negedge)\b", text.split("\n")[0] if text else ""))

    def loads_from_text(self, m):
        """Every read of a declared name inside the module's text: the
        identifiers that are not a declaration, a driver position or a port
        name in a connection (.clk(...)). Run once the module is complete."""
        lines = self.text(self.absf(m["file"]))
        taken = {(l[1], l[2]) for l in m["decls"].values()} | {(x[1], x[2]) for v in m["drivers"].values() for x in v}
        m["loads"] = {}
        for ln in range(m["line"], min(m["end"], len(lines)) + 1):
            code = re.sub(r"//.*", "", lines[ln - 1])
            for mo in IDENT.finditer(code):
                n = mo.group(0)
                if n in m["decls"] and (ln, mo.start()) not in taken and not code[:mo.start()].rstrip().endswith("."):
                    m["loads"].setdefault(n, []).append([m["file"], ln, mo.start(), "read"])

    # -- hierarchy -----------------------------------------------------------

    def walk(self, scope, path, parent_key, gen=None):
        """scope: a module instance; registers it in the hierarchy and its
        children in the parent's module."""
        key = self.module_of(scope)
        self.hier[path] = key
        m = self.modules[key]
        self.children(scope, path, m, None)
        return key

    def children(self, scope, path, m, gen):
        for s in _try(scope, "internal_scope_handles") or []:
            t = _type(s)
            n = _name(s).split(".")[-1]
            if t == "npiModport":
                continue  # a view of the interface, not a scope of its own
            if t in ("npiModule", "npiInterface", "npiProgram"):
                base = re.sub(r"\[.*\]$", "", n)
                if base not in m["instances"]:
                    f = _try(s, "file") or ""
                    ln = _try(s, "line_no") or 0
                    self.files.add(f)
                    _, ikey = self.module_key(s)
                    m["instances"][base] = {"module": ikey, "file": self.rel(f), "line": ln,
                                            "col": self.col_of(f, ln, base), "gen": gen, "wildcard": False,
                                            "conns": self.conns(s, f)}
                self.walk(s, path + "." + n, m)
            elif t in ("npiGenScope", "npiGenScopeArray", "npiGenerateScope", "npiNamedBegin", "npiNamedFork"):
                label = re.sub(r"\[.*\]$", "", n)
                if t != "npiGenScopeArray":
                    m["gen"].setdefault(label, _try(s, "line_no") or 0)
                    self.decls_in_gen(s, m)
                    if not self.seen_gen(m, label):
                        self.drivers_in(s, m, self.absf(m["file"]))
                self.children(s, path + "." + n, m, label)
            else:
                self.unknown[t] = self.unknown.get(t, 0) + 1

    def seen_gen(self, m, label):
        """Whether this generate block's body was already taken (the
        unrolled copies of a loop all have the same text)."""
        seen = m.setdefault("_gen_seen", [])
        if label in seen:
            return True
        seen.append(label)
        return False

    def decls_in_gen(self, s, m):
        for meth in ("net_handles", "variables_handles"):
            for h in _try(s, meth) or []:
                n = _name(h).split(".")[-1]
                if n in m["decls"]:
                    continue
                f = _try(h, "file") or ""
                ln = _try(h, "line_no") or 0
                m["decls"][n] = [self.rel(f), ln, self.col_of(f, ln, n), "decl"]

    def expr_ids(self, h, depth=0):
        """The identifiers of a connected expression, an index wrapped in
        [ ]: a name, a bit/part select of a name, a concatenation or an
        operation of those."""
        if h is None or depth > 32:
            return []
        t = _type(h)
        if t in ("npiPartSelect", "npiBitSelect", "npiIndexedPartSelect", "npiVarSelect", "npiVarBit"):
            base = _try(h, "parent_handle") or _try(h, "actual_handle")
            out = [["id", _name(base).split(".")[-1]]] if base is not None and _name(base) else []
            inner = []
            for meth in ("index_handles", "index_handle", "left_range_handle", "right_range_handle", "base_expr_handle"):
                k = _try(h, meth)
                if k is None:
                    continue
                for x in (k if isinstance(k, (list, tuple)) else [k]):
                    inner += self.expr_ids(x, depth + 1)
            return out + ([["op", "["]] + inner + [["op", "]"]] if inner else [])
        ops = _try(h, "operand_handles")
        if ops:
            out = []
            for o in ops:
                out += self.expr_ids(o, depth + 1)
            return out
        if t == "npiConstant":
            v = _try(h, "value")
            if v is None:
                v = _try(h, "decompile")
            return [["num", str(v)]] if v not in (None, "") else []
        n = _name(h)
        if n and t != "npiParameter":
            return self.name_ids(n.split(".")[-1])
        return []

    @staticmethod
    def name_ids(n):
        """"arr[g]" or "bus[1]" as the KDB names a select: the base, then
        what is in the [ ]: identifiers, and numbers (so that bus[0] and
        bus[1] stay different nets)."""
        m = re.match(r"([A-Za-z_][A-Za-z0-9_$]*)(\[.*\])?$", n)
        if not m:
            return []
        out = [["id", m.group(1)]]
        if m.group(2):
            for part in re.findall(r"\[([^\]]*)\]", m.group(2)):
                inner = []
                for w in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*|\d[\d_]*(?:'[sS]?[bodhBODH][0-9a-fA-FxXzZ_]+)?|:", part):
                    if w == ":":
                        inner.append(["op", ":"])
                    elif w[0].isdigit():
                        inner.append(["num", w])
                    elif w not in KEYWORDS:
                        inner.append(["id", w])
                out += [["op", "["]] + inner + [["op", "]"]]
        return out

    def conns(self, inst, file):
        """Port connections of an instance: the identifiers of each
        expression, placed on the instantiation line (the connection's own
        line is not known to the KDB)."""
        out = []
        ln = _try(inst, "line_no") or 0
        for p in _try(inst, "port_handles") or []:
            pname = _name(p)
            toks = []
            for tok in self.expr_ids(_try(p, "high_conn_handle")):
                if tok[0] == "id":
                    toks.append(["id", tok[1], ln, self.col_of(file, ln, tok[1])])
                else:
                    toks.append([tok[0], tok[1], 0, 0])  # op or num
            out.append([pname, toks, ln, self.col_of(file, ln, "." + pname) if pname else 0])
        return out

    def doc(self):
        self.finish()
        for m in self.modules.values():
            m.pop("_gen_seen", None)
        return {"format": "waveview-design/1", "producer": "verdi-npi", "root": self.root,
                "files": sorted(self.rel(f) for f in self.files if f),
                "modules": self.modules, "hier": self.hier}


def load_args(spec):
    """Verdi design-loading options for a KDB path, and the directory to
    run in (a vericom library is named relative to the working directory)."""
    spec = os.path.abspath(spec.rstrip("/"))
    if spec.endswith(".daidir"):
        return ["-dbdir", spec], os.path.dirname(spec)
    if spec.endswith(".lib++"):
        return ["-lib", os.path.basename(spec)[:-6]], os.path.dirname(spec)
    if os.path.isdir(os.path.join(spec, "work.lib++")):
        return ["-lib", "work"], spec
    raise SystemExit("npi_export: %s is neither a work.lib++ (vericom) nor a simv.daidir (VCS -kdb)" % spec)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("kdb", help="work.lib++ from vericom, or simv.daidir from VCS -kdb")
    ap.add_argument("-o", "--output", required=True, help="design JSON to write (- for stdout)")
    ap.add_argument("--root", help="directory the file names are relative to (default: the KDB's parent)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    opts, cwd = load_args(a.kdb)
    root = os.path.abspath(a.root or cwd)
    os.chdir(cwd)
    home = os.environ.get("VERDI_HOME")
    if not home:
        raise SystemExit("npi_export: VERDI_HOME is not set")
    sys.path.append(os.path.join(home, "share", "NPI", "python"))
    sys.path.append(os.path.join(home, "share", "NPI", "lib", "linux64"))
    from pynpi import npisys, lang  # noqa: E402

    # the library writes a banner and logs to stdout: keep the JSON apart
    out_fd = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    if not npisys.init([sys.argv[0]] + opts):
        raise SystemExit("npi_export: NPI init failed (license? see npiLog/)")
    if not npisys.load_design([sys.argv[0]] + opts):
        raise SystemExit("npi_export: cannot load %s" % a.kdb)
    ex = Export(lang, root)
    tops = lang.get_top_inst_list() or []
    for top in tops:
        ex.walk(top, _name(top), None)
    npisys.end()
    doc = ex.doc()
    if not a.quiet:
        sys.stderr.write("npi_export: %d modules, %d instances, %d files from %s%s\n"
                         % (len(doc["modules"]), len(doc["hier"]), len(doc["files"]), a.kdb,
                            "; skipped scopes: %s" % ex.unknown if ex.unknown else ""))
    data = json.dumps(doc, separators=(",", ":"))
    if a.output == "-":
        os.write(out_fd, data.encode())
    else:
        with open(a.output, "w") as fh:
            fh.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
