---
name: wave-debug
description: Debug RTL behaviour from a simulation waveform (VCD/FST) with wavequery.py (text queries) and waveview.py (the browser waveform viewer shared with the user). Use when a test fails or hangs and you need to see what signals did cycle by cycle, find when a condition first happened, check a handshake / pipeline flow in a dump, or show the user a moment in the waveform.
---

# Waveform debugging with wavequery.py

`wavequery.py` is a text waveform viewer: every query prints a bounded
table (default cap 200 rows) or JSON. Run `wavequery.py -h` for the
full reference; this page is the workflow.

## 1. Get a dump, only around the problem

```sh
# FST from the simulator (Verilator: --trace-fst), limited to a window of cycles
# around the problem when the testbench allows it
```

Keep the window to a few thousand cycles. If you do not know where the
problem is yet, find the failing cycle from the log first (commit trace,
cosim mismatch), then dump around it.

## 2. Orient

```sh
wavequery.py info w.fst                # time range, clocks, top scopes
wavequery.py list w.fst rob commit     # signals whose name contains these
```

Signals can be named by any unique suffix (`rob.commit_valid`). An unknown
or ambiguous name prints the candidates; pick one and retry.

## 3. Locate the event, then look around it

```sh
# when did it first happen?
wavequery.py find w.fst 'rose(u_rob.flush_valid)' --first
wavequery.py find w.fst 'commit_valid && commit_pc == 0x80000040' -s commit_inst
wavequery.py find w.fst 'stall' --count

# what led up to it?
wavequery.py table w.fst -s '*u_rob.commit_*' -s '*.flush*' \
    --around 'rose(u_rob.flush_valid)' -B 8 -A 3
wavequery.py table w.fst -s '*lsu*req*' --cycles 1200:1300 --changes-only
wavequery.py changes w.fst -s '*.state' --cycles 1200:1300
wavequery.py get w.fst --cycle 1234 -s '*u_csr.mcause' -s '*u_csr.mepc'
```

Expression syntax: `== != < > && || ! & | ^ ~ << >>`, functions
`rose fell changed stable prev(s[,n]) bit(s,n) bits(s,hi,lo) isx`.
Brace a name Python cannot spell: `{u_core.pc[3]}`. Unknown (x/z) values
never compare equal to anything; use `isx(s)` to find them.

## Tips

- Cycle n is the n-th rising edge of the clock (auto-detected, or `--clock`);
  the value shown is the one just after that edge
- Narrow before widening: `find --first`, then `table --around` with small
  `-B/-A`, then add signals. Hitting `--max-rows` means the query was too wide
- Use `--format json` when you want to post-process results in a script
- The first query on a dump parses it; later ones reuse a cache, so many
  small queries are cheap
- For a picture to put in a doc or show the user, the same window can be
  drawn with `wave2img.py` (needs matplotlib)

## Working in the viewer with the user (waveview.py)

When the user has a viewer open (`waveview.py status` lists it), you
share one view with them: what you set appears in their browser at once.

```sh
waveview.py look                        # what the user is looking at now
waveview.py look --table                # plus the visible cycles as a table
waveview.py add '*u_rob.commit_*' '*u_bru.mispredict'
waveview.py goto --cycle 1234 --span 40 # cursor there, 40 cycles wide
waveview.py marker --cycle 1228         # cursor-marker delta shows in the UI
waveview.py annotate --cycle 1230 "redirect"
waveview.py note "flush at 1234 comes from the mispredict at 1230"
waveview.py find 'rose(flush_valid)'    # moves the cursor to the next hit
waveview.py inbox --wait 600            # wait for the user's "Send to AI"
```

- Start one yourself with `waveview.py serve <dump>` (run it in the
  background) and give the user the printed URL; on a remote machine they
  forward the port (`ssh -L 8765:localhost:8765 <host>`, or VS Code does it)
- Use wavequery for the digging, and waveview to show the user the result:
  put the evidence on screen (signals, cursor, marker, labels) and say the
  conclusion in a note
- `inbox` messages carry the user's view at the time they pressed the button,
  so you know which signals and which cycle they mean
- Do not `clear` the user's signal list without being asked; add to it

### Pointing at the evidence, and leaving it as a file

Named cursors, arrows and comments go through waveview rc lines
(`waverc.py` docstring has the format; times like `1230c`, `A+4c`):

```sh
waveview.py load -e 'cursor A 1230c "mispredict" color=red' \
                 -e 'cursor B A+4c "flush"' \
                 -e 'arrow u_bru.o_mispredict_valid@A -> u_rob.flush_valid@B "4 cycles, expected 2"' \
                 -e 'comment u_rob.flush_valid@A+2c "should rise here"'
waveview.py save findings.wvrc    # the whole debug view; `serve --rc findings.wvrc` reopens it
waveview.py unmark arrow 0        # indexes come from `look`
```

When asked for a debug view of a failure without a viewer running, write the
.wvrc by hand (dump line relative to the file) and check it with
`waveview.py serve --rc FILE` (warnings name lines that did not apply).

## The shape of a scope: block diagram

```sh
waveview.py diagram u_soc              # instances with their size and interfaces, and who connects to whom
waveview.py diagram u_soc --json       # the same for a program
```

Each box is an instance (signals, sub-instances, lines of source), its
interfaces are bundles of signals with the protocol told from the names (AXI4
master, APB completer, AXI4-Stream source, CHI requester...), and the edges
say which instance drives which through what bundle. The user sees the same
picture with the Diagram button (D); a connection clicked there adds its
signals to the waveform.

Bundles tagged `?` are the ones the names did not identify (`unnamed` lists
them as `box/bundle`). Name them: look at their signals (`diagram --json`,
or `wavequery.py list`) and the source (`src`), decide what the interface is
(a bus protocol under other names, a custom request/response interface, a
group of status or debug signals...), then

```sh
waveview.py label u_soc u_cpu/dbg "debug trace: 2x4-bit sample outputs"
waveview.py label u_soc /cfg "register file access from the testbench"   # the scope's own ports
```

The name shows in the diagram at once and is kept with the view. When the
user presses "Ask AI to name interfaces", the message in `inbox` lists the
bundles and their signals: answer with `label` commands, one per bundle, and
a short note on anything that looks like a known protocol under unusual
names.

## From a waveform value to the RTL that made it

```sh
waveview.py src u_rob.r_rob_head            # declaration + drivers, each source line
                                                  # followed by its signals' values at the cursor
waveview.py src u_rob.r_rob_head --loads    # who reads it
waveview.py src u_rob.r_rob_head --open     # show it in the user's source pane too
```

- Work backwards: find the cycle where the value goes wrong (wavequery find),
  put the cursor there (`goto`), then `src` the signal, pick the driver whose
  condition holds at the cursor (the printed values tell you), and `src` the
  inputs of that line. Repeat until the cause is found
- A driver of kind `port_output` means it comes out of an instance: `src`
  the port inside it (`u_rob.o_rob_head`). `parent_conn` means an input port:
  the connected signal in the parent is next
- Without a running viewer: `rtlindex.py <filelist.f> <tb dir> --trace <full name>`
- For exact tracing (generate blocks, parameterized copies, port expressions)
  use the elaborated design: run Verilator with the build's flags plus
  `--xml-only --xml-output design.xml`, then pass `design.xml` to `--rtl` or
  to rtlindex.py instead of the filelist
