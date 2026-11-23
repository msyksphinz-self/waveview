---
name: wave-debug
description: Debug RTL behaviour from a simulation waveform (VCD/FST) without a GUI, using tools/wavequery.py. Use when a test fails or hangs and you need to see what signals did cycle by cycle, find when a condition first happened, or check a handshake / pipeline flow in a dump.
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
