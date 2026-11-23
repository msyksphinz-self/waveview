# waveview design JSON (`waveview-design/1`)

The elaborated-design description that `tools/waveview.py serve --rtl <file>.json`
and `tools/rtlindex.py <file>.json` read. It is what an exporter from another
design database (a Verdi KDB, a VCS `simv.daidir` through Verdi's NPI, a
commercial linter's database) writes so that waveview's source pane, active
annotation and driver tracing work from that database.

`tools/rtlindex.py <sources or design.xml> -o design.json` writes the same
format from the built-in producers, which is the easiest way to see a complete
example.

## Top level

```json
{
  "format": "waveview-design/1",
  "producer": "verdi-npi",
  "root": "/abs/path/the/file/paths/are/relative/to",
  "files": ["core/rtl/backend/scariv_rob.sv", "..."],
  "modules": { "<module key>": { ... } },
  "hier": { "tb_scariv_top.u_dut.u_rob": "scariv_rob", "...": "..." }
}
```

- `files`: every source file the modules refer to. Watched for changes
- `modules`: one entry per elaborated module. The key is unique; a module
  elaborated with different parameters may appear once per parameter set
  (`bit_cnt__W2`), with `orig` giving the source name
- `hier`: the instance tree, instance path → module key. Paths use `.` and
  write generate scopes with their index in brackets, as simulators dump them
  (`u_iq.loop_DISP[0].u_slot_encoder`). The first component is the top
  module instance. A dump may add a wrapper scope above it (Verilator's
  `TOP`); waveview skips it. When `hier` is empty, waveview walks
  `instances` from a top module instead

## Module

```json
{
  "orig": "scariv_rob",
  "file": "core/rtl/backend/scariv_rob.sv",
  "line": 14, "end": 655,
  "ports": [["i_clk", "input"], ["o_rob_head", "output"]],
  "params": ["ROB_W"],
  "decls":   { "r_rob_head": ["core/rtl/backend/scariv_rob.sv", 102, 16, "decl"] },
  "drivers": { "r_rob_head": [["core/rtl/backend/scariv_rob.sv", 490, 6, "always_ff"]] },
  "loads":   { "r_rob_head": [["core/rtl/backend/scariv_rob.sv", 653, 22, "read"]] },
  "gen": { "loop_CMT_DISP": 518 },
  "instances": { "<instance>": { ... } }
}
```

- A location is `[file, line, column, kind]`: line 1-based, column 0-based,
  pointing at the identifier
- `decls` kinds: `decl`, `port`, `param`
- `drivers` kinds: `assign` (continuous), `always_ff` (non-blocking or a
  clocked process), `comb` (blocking in a combinational process), `init`
  (initial block or declaration initializer). Only places inside this module;
  drivers through instance output ports come from `instances`
- `loads`: every read, kind `read`
- Names are the source identifiers (no hierarchy, no index). Signals declared
  inside generate blocks are listed under their own name; `gen` names the
  generate blocks (label → line) so that dump scopes like `loop_CMT_DISP[1]`
  are recognised as part of the module

## Instance

```json
{
  "module": "scariv_rob", "file": "core/rtl/top/scariv_top.sv", "line": 272, "col": 2,
  "gen": null, "wildcard": false,
  "conns": [
    ["o_rob_head", [["id", "w_rob_head", 277, 24]], 277, 4],
    ["o_out", [["id", "w_arr", 32, 41], ["op", "[", 0, 0], ["id", "g", 32, 47], ["op", "]", 0, 0]], 32, 37]
  ]
}
```

- `module`: module key of the instantiated module
- `gen`: label of the enclosing generate block, or null
- `conns`: `[port, expression tokens, line, column]`. The tokens are the
  identifiers of the connected expression; identifiers used as an index are
  wrapped in `["op","[" ...]` / `["op","]" ...]` so that an output port is
  taken to drive only the base signal. A positional connection has port null
  and is matched to `ports` by order. `wildcard` is true for `.*`

## Writing an exporter

The questions an exporter has to answer from its database, per module:
ports with direction, declared signals and parameters with locations, the
left-hand side of every assignment with its kind, every read, and every
instance with its port connections; and once for the design, the instance
tree. A database API such as Verdi's NPI exposes each of these; the
exporter only maps them to this shape.
