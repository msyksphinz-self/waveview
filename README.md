# waveview

A waveform viewer and debugger for RTL simulation dumps that a person and an
AI agent use together, built for remote development.

- **GTKWave / Verdi style UI in the browser**: hierarchy tree, signal list,
  names / values / waves, cursor and marker with cycle delta, drag zoom,
  edge jumps, expression search, radix, drag and drop, Verdi-like keys
- **Shared view for agents**: the view state lives on the server; a CLI
  (`waveview.py add / goto / marker / annotate / note / find / look / src`)
  drives the same screen the person sees, and "Send to AI" hands the agent a
  message plus the current view
- **Source pane, Verdi nTrace style**: active annotation (values at the
  cursor next to every signal), double-click to trace drivers / loads across
  the hierarchy, into instances and up through ports
- **Huge dumps**: FST is read in place, on demand, by a small C helper
  (`fstsrv.c`); opening reads only the hierarchy, views fetch tiles of what is
  on screen, zoomed-out tiles are aggregated in C. A 3.6 GB FST opens in ms
- **Remote**: run the viewer on your desktop (Windows included) and read an
  FST that stays on the build server, over plain ssh; nothing to install
  there
- **Text interface for agents**: `wavequery.py` answers info / list / get /
  table / changes / find with bounded output or JSON
- Verdi interop: FSDB via Verdi's `fsdb2vcd`, nWave `.rc` in and out; GTKWave
  `.gtkw` in and out

Python 3 standard library only. FST support builds a small C helper on
demand against the FST library shipped with Verilator (`verilator` on PATH or
`VERILATOR_ROOT`).

## Quick start

```sh
python3 waveview.py serve sim.fst --rtl rtl/filelist.f      # prints a URL with a token
# on a remote machine: ssh -L 8765:localhost:8765 host  (VS Code forwards it by itself)

python3 waveview.py add '*u_rob.commit_*'                   # from any shell / agent
python3 waveview.py goto --cycle 1234 --span 40
python3 waveview.py src u_rob.r_rob_head                    # drivers with values at the cursor
python3 waveview.py look                                    # what the viewer shows, as text

python3 wavequery.py find sim.fst 'rose(flush_valid)' --first
```

From Windows, with the dump on a Linux build server:

```bat
waveview.cmd serve build01:/work/sim/w.fst --rtl build01:/work/proj/rtl/filelist.f --rtl-root /work/proj --open
```

### Standalone Windows executable

`waveview.exe` needs no Python, Verilator or C compiler. Double-click it and
pick a dump, or drop a `.fst` / `.vcd` / `.fsdb` on it. FSDB still needs Verdi's
`fsdb2vcd`. With arguments it is the normal command line:
`waveview.exe serve w.fst --open`.

To build it, run `./build_windows.sh` from WSL with a Windows Python on `PATH`
and Verilator installed. The result is `dist/waveview.exe`.

## Files

| File | What |
|---|---|
| `waveview.py` + `waveview_ui/` | viewer server, browser UI, CLI client |
| `wavequery.py` | text queries over a dump |
| `wavesource.py`, `fstsrv.c` | lazy FST reading (local or over ssh) |
| `remote.py` | ssh transport, remote build of `fstsrv`, remote RTL index |
| `rtlindex.py` | SystemVerilog index (heuristic, Verilator `--xml-only`, or design JSON) |
| `wave2img.py`, `fst2vcd_mini.c` | VCD parser; draws a timing diagram to SVG/PNG (needs matplotlib) |
| `build_windows.sh` | builds the standalone `dist/waveview.exe` |
| `docs/guide_ja.md` | user guide (Japanese) |
| `docs/waveview_design_format.md` | design JSON format, for exporters (e.g. from a Verdi KDB) |
| `.claude/skills/wave-debug/` | Claude Code skill: how an agent debugs with these tools |

## Tests

```sh
./run_tests.sh
```

Runs the self tests of `waveview.py`, `wavequery.py` and `rtlindex.py`. The FST
and remote checks run when the FST helper can be built (Verilator installed),
and are skipped otherwise.

## Origin

Extracted from the [scariv](https://github.com/msyksphinz-self/scariv) RISC-V
core repository, history included.

## License

MIT, see [LICENSE](LICENSE).
