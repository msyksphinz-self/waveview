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
  the hierarchy, into instances and up through ports; right-click a node of
  the hierarchy tree to open its module or its instantiation
- **Huge dumps**: FST is read in place, on demand, by a small C helper
  (`fstsrv.c`); opening reads only the hierarchy, views fetch tiles of what is
  on screen, zoomed-out tiles are aggregated in C. A 3.6 GB FST opens in ms
- **Remote**: run the viewer on your desktop (Windows included) and read an
  FST that stays on the build server, over plain ssh; nothing to install
  there
- **Text interface for agents**: `wavequery.py` answers info / list / get /
  table / changes / find with bounded output or JSON
- **Debug views as files (`.wvrc`)**: the signals to look at, named cursors
  at the moments that matter, arrows between transitions and comments on the
  waveform, in a small text file. `serve --rc bug.wvrc` opens exactly that
  view; `load` / `save` from the CLI, right-click to add marks in the browser
  (see `docs/example.wvrc`)
- **FSDB without conversion**: where Verdi is installed, `fsdbsrv.cc` reads
  an FSDB in place through Verdi's FsdbReader library, with the same
  on-demand protocol as FST, locally or over ssh. Without that library FSDB
  goes through Verdi's `fsdb2vcd`
- **Dumps with tens of millions of signals**: `--scope tb.u_dut.u_core`
  (wavequery and `serve`, FST and FSDB, also over ssh) loads only the signals
  below a scope. For a local FSDB too large to list, wavequery keeps the
  names in an index on disk (`nameindex.py`, under `~/.cache/waveview/index`,
  about 25 bytes per signal): the first query writes it, later ones read
  only the scopes of the dump and look the names up there. An 84 million
  signal emulator dump: 4 minutes per query before, 40 s for the first and
  3 to 10 s for the next ones. `WAVE_INDEX_DIR` moves the index,
  `WAVE_INDEX=0` turns it off, `WAVE_INDEX_MIN` (default 1000000 variable
  idcodes) is the size it starts at
- Verdi interop: nWave `.rc` in and out; GTKWave `.gtkw` in and out

Python 3 standard library only. FST support builds a small C helper on
demand against the FST library shipped with Verilator (`verilator` on PATH or
`VERILATOR_ROOT`; up to Verilator 5.040, later versions ship only an FST
writer). The standalone Windows and macOS builds carry the helper.

## Quick start

```sh
python3 waveview.py serve sim.fst --rtl rtl/filelist.f      # prints a URL with a token
# on a remote machine: ssh -L 8765:localhost:8765 host  (VS Code forwards it by itself)

python3 waveview.py add '*u_rob.commit_*'                   # from any shell / agent
python3 waveview.py goto --cycle 1234 --span 40
python3 waveview.py src u_rob.r_rob_head                    # drivers with values at the cursor
python3 waveview.py look                                    # what the viewer shows, as text

python3 wavequery.py find sim.fst 'rose(flush_valid)' --first

python3 waveview.py save bug123.wvrc                         # the view, with cursors, arrows, comments
python3 waveview.py serve --rc bug123.wvrc                   # someone else opens the same view
```

From Windows, with the dump on a Linux build server:

```bat
waveview.cmd serve build01:/work/sim/w.fst --rtl build01:/work/proj/rtl/filelist.f --rtl-root /work/proj --open
```

The same with an FSDB, read in place by the server's Verdi (`VERDI_HOME` or
`fsdb2vcd` on the server's PATH; the first time builds `fsdbsrv` there).
When the server's login environment does not say where Verdi is, say it
from here with `WAVEVIEW_REMOTE_ENV`, which sets variables for every command
run on the server:

```sh
export WAVEVIEW_REMOTE_ENV="VERDI_HOME=/tools/synopsys/verdi/Y-2026.03-SP2"
python3 waveview.py serve build01:/work/sim/w.fsdb --rtl build01:/work/proj/rtl/filelist.f --rtl-root /work/proj --window
```

### Standalone Windows executable

`waveview.exe` needs no Python, Verilator or C compiler. Double-click it and a
window opens (Edge or Chrome in app mode). In **Open…**, choose This PC or a host
from `~/.ssh/config`, press Connect, browse to a dump and open it; **Browse…**
next to the RTL field picks the filelist (or design file, or folder) for the
source view from the same listing. Closing the
window ends the program. You can also drop a `.fst` / `.vcd` / `.fsdb` on the exe.
A local FSDB still needs Verdi's `fsdb2vcd`; an FSDB on a server with Verdi is
read there. With arguments it is the normal command line:
`waveview.exe serve w.fst --open`.

The window has no terminal for prompts, so ssh must log in without one: use a
key (with ssh-agent if it has a passphrase) and connect once from a terminal to
accept the host key. The server needs a C compiler and Verilator the first time,
to build its FST reader.

To build it, run `./build_windows.sh` from WSL with a Windows Python on `PATH`
and Verilator installed. The result is `dist/waveview.exe`.

### Standalone macOS app

`waveview.app` is the same program for a Mac: no Python, Verilator or C
compiler needed. Double-click it and a window opens (Chrome, Edge, Chromium or
Brave in app mode; without one of those the default browser opens and a small
dialog quits the viewer). It has no Dock icon of its own: the window is the
application, closing it ends the program. Drop a `.fst` / `.vcd` / `.fsdb` on
the icon or use "Open With" to start on that dump. Next to it, `dist/waveview`
is the same binary as a plain command: `./waveview serve w.fst --open`, and
all the `add / goto / look` commands.

The app is not notarized: the first time, right-click it and choose Open, or
run `xattr -dr com.apple.quarantine waveview.app` after copying it over. It is
built for the CPU it was built on (Apple silicon or Intel).

To build it, run `./build_macos.sh` on a Mac with the Xcode command line tools
(`xcode-select --install`) and `python3`. The FST library comes from Verilator
if an installed one still ships it (up to 5.040), otherwise the script fetches
the Verilator 5.040 sources for it. The result is `dist/waveview.app` and
`dist/waveview`.

## Files

| File | What |
|---|---|
| `waveview.py` + `waveview_ui/` | viewer server, browser UI, CLI client |
| `waverc.py` | waveview rc (`.wvrc`): read and write debug views (format in its docstring) |
| `wavequery.py` | text queries over a dump |
| `wavesource.py`, `fstsrv.c`, `fsdbsrv.cc` | lazy FST / FSDB reading (local or over ssh); `fsdbsrv.cc` needs Verdi's FsdbReader |
| `nameindex.py` | the names of a very large FSDB, looked up in an index on disk |
| `remote.py` | ssh transport, remote build of `fstsrv` / `fsdbsrv`, remote RTL index |
| `opener.py` | Open dialog backend: ssh hosts, directory listings, app window |
| `rtlindex.py` | SystemVerilog index (heuristic, Verilator `--xml-only`, or design JSON) |
| `wave2img.py`, `fst2vcd_mini.c` | VCD parser; draws a timing diagram to SVG/PNG (needs matplotlib) |
| `build_windows.sh` | builds the standalone `dist/waveview.exe` |
| `build_macos.sh` | builds the standalone `dist/waveview.app` (and `dist/waveview`) |
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
