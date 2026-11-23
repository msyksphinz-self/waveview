---
name: wave-debug
description: Debug RTL behaviour from a simulation waveform (VCD/FST) with tools/wavequery.py (text queries) and tools/waveview.py (the browser waveform viewer shared with the user). Use when a test fails or hangs and you need to see what signals did cycle by cycle, find when a condition first happened, check a handshake / pipeline flow in a dump, or show the user a moment in the waveform.
---

# Waveform debugging with tools/wavequery.py

`tools/wavequery.py` is a text waveform viewer: every query prints a bounded
table (default cap 200 rows) or JSON. Run `tools/wavequery.py -h` for the
full reference; this page is the workflow.

## 1. Get a dump, only around the problem

```sh
make WAVE=fst build-top                     # or WAVE=1 for VCD
./obj_dir_top/tb_scariv_top ... +wave=w.fst +wave_from=<cycle> +wave_to=<cycle>
```

Keep the window to a few thousand cycles. If you do not know where the
problem is yet, find the failing cycle from the log first (commit trace,
cosim mismatch), then dump around it.

## 2. Orient

```sh
tools/wavequery.py info w.fst                # time range, clocks, top scopes
tools/wavequery.py list w.fst rob commit     # signals whose name contains these
```

Signals can be named by any unique suffix (`rob.commit_valid`). An unknown
or ambiguous name prints the candidates; pick one and retry.

## 3. Locate the event, then look around it

```sh
# when did it first happen?
tools/wavequery.py find w.fst 'rose(u_rob.flush_valid)' --first
tools/wavequery.py find w.fst 'commit_valid && commit_pc == 0x80000040' -s commit_inst
tools/wavequery.py find w.fst 'stall' --count

# what led up to it?
tools/wavequery.py table w.fst -s '*u_rob.commit_*' -s '*.flush*' \
    --around 'rose(u_rob.flush_valid)' -B 8 -A 3
tools/wavequery.py table w.fst -s '*lsu*req*' --cycles 1200:1300 --changes-only
tools/wavequery.py changes w.fst -s '*.state' --cycles 1200:1300
tools/wavequery.py get w.fst --cycle 1234 -s '*u_csr.mcause' -s '*u_csr.mepc'
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
  drawn with `tools/wave2img.py` (needs matplotlib)

## Working in the viewer with the user (tools/waveview.py)

When the user has a viewer open (`tools/waveview.py status` lists it), you
share one view with them: what you set appears in their browser at once.

```sh
tools/waveview.py look                        # what the user is looking at now
tools/waveview.py look --table                # plus the visible cycles as a table
tools/waveview.py add '*u_rob.commit_*' '*u_bru.mispredict'
tools/waveview.py goto --cycle 1234 --span 40 # cursor there, 40 cycles wide
tools/waveview.py marker --cycle 1228         # cursor-marker delta shows in the UI
tools/waveview.py annotate --cycle 1230 "redirect"
tools/waveview.py note "flush at 1234 comes from the mispredict at 1230"
tools/waveview.py find 'rose(flush_valid)'    # moves the cursor to the next hit
tools/waveview.py inbox --wait 600            # wait for the user's "Send to AI"
```

- Start one yourself with `tools/waveview.py serve <dump>` (run it in the
  background) and give the user the printed URL; on a remote machine they
  forward the port (`ssh -L 8765:localhost:8765 <host>`, or VS Code does it)
- Use wavequery for the digging, and waveview to show the user the result:
  put the evidence on screen (signals, cursor, marker, labels) and say the
  conclusion in a note
- `inbox` messages carry the user's view at the time they pressed the button,
  so you know which signals and which cycle they mean
- Do not `clear` the user's signal list without being asked; add to it

## From a waveform value to the RTL that made it

```sh
tools/waveview.py src u_rob.r_rob_head            # declaration + drivers, each source line
                                                  # followed by its signals' values at the cursor
tools/waveview.py src u_rob.r_rob_head --loads    # who reads it
tools/waveview.py src u_rob.r_rob_head --open     # show it in the user's source pane too
```

- Work backwards: find the cycle where the value goes wrong (wavequery find),
  put the cursor there (`goto`), then `src` the signal, pick the driver whose
  condition holds at the cursor (the printed values tell you), and `src` the
  inputs of that line. Repeat until the cause is found
- A driver of kind `port_output` means it comes out of an instance: `src`
  the port inside it (`u_rob.o_rob_head`). `parent_conn` means an input port:
  the connected signal in the parent is next
- Without a running viewer: `tools/rtlindex.py <filelist.f> <tb dir> --trace <full name>`
