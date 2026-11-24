"""Interface bundles: the signals of a scope grouped into the buses they
belong to, with the protocol told from the signal names.

    bundles([("m_axi_awvalid", 1, "out"), ("m_axi_awready", 1, "in"), ...])
    -> [{"name": "m_axi", "protocol": "AXI4", "role": "master", "dir": "out",
         "width": 83, "signals": ["m_axi_awvalid", ...]}, ...]

A port is matched against the suffix vocabulary of the known protocols
(AXI4 / AXI4-Lite / AXI4-Stream, APB, AHB, CHI, TileLink, Wishbone,
Avalon-MM, generic valid/ready and req/ack, clock and reset); what is left
of the name is the bundle. A bundle's protocol is decided from the set of
suffixes it holds, and its role (master / slave, source / sink) from the
direction of the protocol's key signal when the direction is known. Names
that fit no protocol are grouped by their first word. Deterministic and
cheap: an AI pass can name what this leaves unlabelled.
"""

from __future__ import annotations

import re

# protocol: (key signals, any one of which names the bundle, as a list of
# alternatives; the signals that make the full form; the signal whose
# direction tells the role; (role when out, role when in))
_CHI_CHANNELS = ["txreq", "txrsp", "txdat", "txsnp", "rxreq", "rxrsp", "rxdat", "rxsnp"]
_CHI = ["%s%s" % (c, p) for c in _CHI_CHANNELS for p in ("flitv", "flitpend", "flit", "lcrdv")] + [
    "txlinkactivereq", "txlinkactiveack", "rxlinkactivereq", "rxlinkactiveack", "syscoreq", "syscoack", "txsactive",
    "rxsactive"]
_TL = ["%s_%s" % (c, p) for c in "abcde" for p in
       ("valid", "ready", "opcode", "param", "size", "source", "address", "mask", "data", "corrupt", "sink", "denied")]

PROTOCOLS = [
    # name, required (all of), key-dir signal, (role out, role in), vocabulary
    ("AXI4-Stream", [{"tvalid", "tdata"}], "tvalid", ("source", "sink"),
     {"tvalid", "tready", "tdata", "tstrb", "tkeep", "tlast", "tid", "tdest", "tuser"}),
    ("AXI4", [{"awvalid", "wdata"}, {"arvalid", "rdata"}, {"awvalid", "awaddr"}, {"arvalid", "araddr"}],
     "awvalid", ("master", "slave"),
     {"awvalid", "awready", "awaddr", "awlen", "awsize", "awburst", "awid", "awlock", "awcache", "awprot", "awqos",
      "awregion", "awuser", "wvalid", "wready", "wdata", "wstrb", "wlast", "wuser", "bvalid", "bready", "bresp",
      "bid", "buser", "arvalid", "arready", "araddr", "arlen", "arsize", "arburst", "arid", "arlock", "arcache",
      "arprot", "arqos", "arregion", "aruser", "rvalid", "rready", "rdata", "rresp", "rlast", "rid", "ruser",
      "awatop", "awsnoop", "arsnoop", "awdomain", "ardomain", "awbar", "arbar", "acvalid", "acready", "acaddr",
      "acsnoop", "crvalid", "crready", "crresp", "cdvalid", "cdready", "cddata", "cdlast", "wack", "rack"}),
    ("APB", [{"psel", "penable"}, {"psel", "pwrite"}], "psel", ("requester", "completer"),
     {"psel", "penable", "pwrite", "paddr", "pwdata", "prdata", "pready", "pslverr", "pprot", "pstrb", "pclk",
      "presetn", "pwakeup", "pauser", "pwuser", "pruser", "pbuser"}),
    ("AHB", [{"haddr", "htrans"}], "htrans", ("master", "slave"),
     {"haddr", "htrans", "hwrite", "hsize", "hburst", "hprot", "hwdata", "hrdata", "hready", "hreadyout", "hresp",
      "hsel", "hmastlock", "hnonsec", "hexcl", "hmaster", "hexokay", "hclk", "hresetn"}),
    ("CHI", [{"txreqflitv"}, {"rxrspflitv"}, {"txdatflitv"}, {"rxreqflitv"}, {"txlinkactivereq"}],
     "txreqflitv", ("requester", "completer"), set(_CHI)),
    ("TileLink", [{"a_valid", "d_valid"}, {"a_valid", "a_opcode"}], "a_valid", ("client", "manager"), set(_TL)),
    ("Wishbone", [{"cyc", "stb"}], "cyc", ("master", "slave"),
     {"cyc", "stb", "ack", "we", "adr", "dat_i", "dat_o", "dat", "sel", "err", "rty", "stall", "lock", "cti",
      "bte", "tga", "tgc", "tgd"}),
    ("Avalon-MM", [{"waitrequest", "read"}, {"waitrequest", "write"}, {"readdatavalid", "read"}],
     "read", ("host", "agent"),
     {"waitrequest", "read", "write", "readdata", "writedata", "readdatavalid", "byteenable", "burstcount",
      "address", "response", "chipselect", "begintransfer", "debugaccess"}),
    ("valid/ready", [{"valid", "ready"}], "valid", ("source", "sink"),
     {"valid", "ready", "data", "last", "strb", "keep", "id", "user", "addr", "len", "size", "be", "we", "wen",
      "ren", "resp", "err", "error", "op", "opcode", "cmd", "tag", "mask", "req", "ack"}),
    ("req/ack", [{"req", "ack"}, {"req", "gnt"}, {"req", "grant"}], "req", ("requester", "responder"),
     {"req", "ack", "gnt", "grant", "data", "addr", "we", "wdata", "rdata", "be", "err", "busy", "done", "valid",
      "ready", "id", "tag"}),
]

_SUFFIX_PROTOCOLS = {}
for _p in PROTOCOLS:
    for _s in _p[4]:
        _SUFFIX_PROTOCOLS.setdefault(_s, []).append(_p[0])
_SUFFIXES = sorted(_SUFFIX_PROTOCOLS, key=len, reverse=True)

_CLOCK = re.compile(r"(^|_)(clk|clock|ck)(_|$|[a-z]*$)|_clk\d*$|^clk", re.I)
_RESET = re.compile(r"(^|_)(rst|reset|arst|resetn|rstn)(_|$|n$|_n$)|rst_?n?$", re.I)


def _dir(d):
    d = (d or "").lower()
    return {"input": "in", "output": "out", "in": "in", "out": "out", "inout": "inout"}.get(d, "")


def _split(name):
    """(bundle prefix, protocol suffix or None) of a port name."""
    low = name.lower()
    for s in _SUFFIXES:
        if low == s:
            return "", s
        if low.endswith("_" + s):
            return name[:-len(s) - 1], s
        if len(s) >= 5 and low.endswith(s):  # m_axiawvalid, chi TXREQFLITV
            return name[:-len(s)], s
    return None, None


def _classify(suffixes):
    """The protocol of a set of suffixes and its key signal, or (None, None)."""
    low = {s.lower() for s in suffixes}
    for name, required, key, roles, vocab in PROTOCOLS:
        if any(req <= low for req in required):
            if name == "AXI4" and not low & {"awlen", "arlen", "wlast", "rlast", "awid", "arid", "awburst",
                                             "arburst"}:
                return "AXI4-Lite", key, roles
            return name, key, roles
    return None, None, None


def bundles(ports):
    """ports: [(name, width, direction)], direction in/out/inout/'' (or
    input/output). Returns bundles, protocol ones first, each
    {"name", "protocol", "role", "dir", "width", "signals"}."""
    groups = {}
    order = []
    loose = []
    for name, width, d in ports:
        d = _dir(d)
        if _CLOCK.search(name) and width == 1:
            groups.setdefault("\0clock", []).append((name, width, d, name))
            continue
        if _RESET.search(name) and width == 1:
            groups.setdefault("\0reset", []).append((name, width, d, name))
            continue
        prefix, suffix = _split(name)
        if suffix is None:
            loose.append((name, width, d))
            continue
        key = prefix
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append((name, width, d, suffix))
    out = []
    for key in order:
        members = groups[key]
        suffixes = {m[3] for m in members}
        protocol, keysig, roles = _classify(suffixes)
        if protocol is None and len(members) == 1:
            loose.append(members[0][:3])
            continue
        role = None
        bdir = None
        if keysig:
            for name, width, d, s in members:
                if s == keysig and d in ("in", "out"):
                    role = roles[0] if d == "out" else roles[1]
                    bdir = d
                    break
        if bdir is None:
            dirs = {m[2] for m in members if m[2] in ("in", "out")}
            bdir = dirs.pop() if len(dirs) == 1 else ("mixed" if dirs else None)
        # ports named by the protocol alone (awvalid, psel...): the bundle takes its name
        out.append({"name": key or (protocol.lower() if protocol else "(ports)"), "protocol": protocol, "role": role,
                    "dir": bdir,
                    "width": sum(m[1] for m in members), "signals": [m[0] for m in members]})
    # what matched no protocol: by first word, when that makes a group
    byword = {}
    for name, width, d in loose:
        word = re.split(r"[_.]", name, maxsplit=1)[0] if "_" in name else name
        byword.setdefault(word, []).append((name, width, d))
    for word, members in byword.items():
        dirs = {m[2] for m in members if m[2] in ("in", "out")}
        bdir = dirs.pop() if len(dirs) == 1 else ("mixed" if dirs else None)
        out.append({"name": word if len(members) > 1 else members[0][0], "protocol": None, "role": None,
                    "dir": bdir, "width": sum(m[1] for m in members), "signals": [m[0] for m in members]})
    for kind in ("clock", "reset"):
        members = groups.get("\0" + kind)
        if members:
            dirs = {m[2] for m in members if m[2] in ("in", "out")}
            out.append({"name": kind, "protocol": kind, "role": None,
                        "dir": dirs.pop() if len(dirs) == 1 else ("mixed" if dirs else None),
                        "width": len(members), "signals": [m[0] for m in members]})
    out.sort(key=lambda b: (0 if b["protocol"] not in (None, "clock", "reset") else 1 if b["protocol"] is None
                            else 2, -b["width"]))
    return out


def selftest():
    fails = 0

    def check(label, ok):
        nonlocal fails
        print("%-4s %s" % ("ok" if ok else "FAIL", label))
        fails += 0 if ok else 1

    axi = [("m_axi_" + s, w, d) for s, w, d in
           (("awvalid", 1, "out"), ("awready", 1, "in"), ("awaddr", 32, "out"), ("awlen", 8, "out"),
            ("wvalid", 1, "out"), ("wdata", 64, "out"), ("wlast", 1, "out"), ("bvalid", 1, "in"),
            ("arvalid", 1, "out"), ("araddr", 32, "out"), ("rvalid", 1, "in"), ("rdata", 64, "in"))]
    apb = [("s_apb_" + s, w, d) for s, w, d in
           (("psel", 1, "in"), ("penable", 1, "in"), ("pwrite", 1, "in"), ("paddr", 12, "in"), ("prdata", 32, "out"))]
    lite = [("cfg_" + s, w, "in") for s, w in (("awvalid", 1), ("awaddr", 8), ("wdata", 32), ("wvalid", 1))]
    stream = [("in_tvalid", 1, "in"), ("in_tdata", 32, "in"), ("in_tready", 1, "out")]
    chi = [("TXREQFLITV", 1, "out"), ("TXREQFLIT", 120, "out"), ("RXRSPFLITV", 1, "in"), ("TXREQLCRDV", 1, "in")]
    other = [("clk", 1, "in"), ("rst_n", 1, "in"), ("dbg_a", 4, "out"), ("dbg_b", 4, "out"), ("irq", 1, "out")]
    out = {b["name"]: b for b in bundles(axi + apb + lite + stream + chi + other)}
    check("AXI4 master", out["m_axi"]["protocol"] == "AXI4" and out["m_axi"]["role"] == "master"
          and out["m_axi"]["width"] == 1 + 1 + 32 + 8 + 1 + 64 + 1 + 1 + 1 + 32 + 1 + 64)
    check("APB completer", out["s_apb"]["protocol"] == "APB" and out["s_apb"]["role"] == "completer")
    check("AXI4-Lite slave", out["cfg"]["protocol"] == "AXI4-Lite" and out["cfg"]["role"] == "slave")
    check("AXI4-Stream sink", out["in"]["protocol"] == "AXI4-Stream" and out["in"]["role"] == "sink")
    check("CHI requester, no separator", out["chi"]["protocol"] == "CHI" and out["chi"]["role"] == "requester")
    check("clock and reset apart", out["clock"]["signals"] == ["clk"] and out["reset"]["signals"] == ["rst_n"])
    check("loose names grouped by word", out["dbg"]["protocol"] is None and len(out["dbg"]["signals"]) == 2
          and out["irq"]["signals"] == ["irq"])
    check("protocol bundles first", [b["protocol"] for b in bundles(other + axi)][0] == "AXI4")
    check("no direction, no role", bundles([("x_valid", 1, ""), ("x_ready", 1, "")])[0]["role"] is None)
    print("selftest: %s" % ("PASS" if not fails else "%d FAILED" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys

    sys.exit(selftest())
