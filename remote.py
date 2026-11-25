"""Remote dumps over ssh: the viewer runs on the desktop (Windows, macOS or
Linux), the dump stays on the build server.

    waveview.py serve ssh://user@build01/work/sim/w.fst --rtl ssh://user@build01/work/rtl/filelist.f
    waveview.py serve build01:/work/sim/w.fst          (scp style works too)
    wavequery.py info build01:/work/sim/w.fst

How:
- fstsrv.c speaks a line protocol on stdin/stdout, so it runs on the
  server at the other end of an ssh session: `ssh -T -C host fstsrv w.fst`.
  Only what a view or a query asks for crosses the link, compressed.
  fsdbsrv.cc does the same for FSDB, on the server's Verdi
- The first connection to a host builds fstsrv there: the C source is sent
  over ssh and compiled against the server's Verilator FST library, then
  cached in ~/.cache/waveview/wavesrv/<source hash>/ on the server.
  Likewise fsdbsrv against the server's Verdi FsdbReader library
- RTL for the source pane is indexed on the server by sending
  rtlindex.py to the server's python3 (nothing has to be installed),
  and the files the pane shows are fetched over ssh when opened
- The ssh client is the system one (OpenSSH ships with Windows 10 and
  later). Keys, ~/.ssh/config, jump hosts and agents work as usual. Set
  WAVEVIEW_SSH to use another command (e.g. "plink -batch")

Spec forms: ssh://[user@]host[:port]/abs/path, or [user@]host:/abs/path.
A single letter before ':' is a Windows drive, not a host.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


class RemoteError(Exception):
    pass


class Remote:
    # no terminal to answer a prompt on (the viewer's own window): fail instead
    batch = False

    def __init__(self, host, port=None):
        self.host, self.port = host, port

    def __repr__(self):
        return "ssh://%s%s" % (self.host, ":%s" % self.port if self.port else "")

    def argv(self, command, compress=False):
        base = shlex.split(os.environ.get("WAVEVIEW_SSH", "ssh"), posix=os.name != "nt")
        command = with_remote_env(command)
        opts = ["-T", "-o", "ServerAliveInterval=30"]
        if self.batch:
            opts += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if compress:
            opts.append("-C")
        if self.port:
            opts += ["-p", str(self.port)]
        if os.path.basename(base[0]).lower().startswith("plink"):
            opts = ["-C"] if compress else []
            if self.batch:
                opts.append("-batch")
            if self.port:
                opts += ["-P", str(self.port)]
        return base + opts + [self.host, command]

    def run(self, command, data=None, timeout=600):
        """Run a shell command on the server, return stdout (bytes)."""
        done = subprocess.run(self.argv(command, compress=True), input=data, capture_output=True, timeout=timeout)
        if done.returncode != 0:
            raise RemoteError("%s: %s failed (%d): %s" % (self, command.split()[0], done.returncode,
                                                          done.stderr.decode(errors="replace").strip()[-800:]))
        return done.stdout

    # -- wave servers: fstsrv (FST) and fsdbsrv (FSDB) ------------------------

    def _known_path(self):
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        return os.path.join(base, "waveview", "remote_fstsrv.json")

    def _known(self):
        try:
            with open(self._known_path()) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def forget_srv(self, tool="fstsrv"):
        known = self._known()
        known.pop(self._key(tool), None)
        self._save_known(known)

    def _save_known(self, known):
        try:
            os.makedirs(os.path.dirname(self._known_path()), exist_ok=True)
            with open(self._known_path(), "w") as fh:
                json.dump(known, fh)
        except OSError:
            pass

    def _source(self, tool):
        with open(os.path.join(HERE, SOURCES[tool]), "rb") as fh:
            return fh.read()

    def _key(self, tool="fstsrv"):
        digest = hashlib.sha1(self._source(tool)).hexdigest()[:12]
        return "%s|%s|%s" % (self.host, self.port or "", digest)

    def srv(self, tool="fstsrv"):
        """Path of a built wave server on the server machine, built on first
        use. The path is remembered here, so later runs start without the
        extra round trip. WAVEVIEW_REMOTE_FSTSRV / WAVEVIEW_REMOTE_FSDBSRV
        name one built by hand."""
        override = os.environ.get("WAVEVIEW_REMOTE_" + tool.upper())
        if override:
            return override
        known = self._known()
        key = self._key(tool)
        if key in known:
            return known[key]
        path = self._build_srv(tool)
        known[key] = path
        self._save_known(known)
        return path

    def _build_srv(self, tool):
        src = self._source(tool)
        digest = hashlib.sha1(src).hexdigest()[:12]
        script = BUILD_SCRIPTS[tool] % {"digest": digest, "source": SOURCES[tool]}
        out = self.run("sh -c %s" % shlex.quote(script), data=src).decode(errors="replace").strip()
        if out.startswith("ok "):
            return out.split(" ", 1)[1].splitlines()[0]
        if out.startswith("no-fst-lib"):
            raise RemoteError("%s has no Verilator FST library to build fstsrv with; put verilator on PATH "
                              "there, set VERILATOR_ROOT, or point WAVEVIEW_REMOTE_FSTSRV at a built fstsrv"
                              % self.host)
        if out.startswith("no-fsdb-lib"):
            raise RemoteError("%s has no Verdi FsdbReader library (share/FsdbReader) to build fsdbsrv with; "
                              "set VERDI_HOME there or put verdi / fsdb2vcd on its PATH, or point "
                              "WAVEVIEW_REMOTE_FSDBSRV at a built fsdbsrv" % self.host)
        raise RemoteError("building %s on %s failed:\n%s" % (tool, self.host, out))

    def srv_argv(self, path, tool="fstsrv", extra=()):
        words = [self.srv(tool), path] + list(extra)
        return self.argv(" ".join(shlex.quote(w) for w in words), compress=True)

    # the old names, for fstsrv
    def fstsrv(self):
        return self.srv("fstsrv")

    def forget_fstsrv(self):
        self.forget_srv("fstsrv")

    def fstsrv_argv(self, path):
        return self.srv_argv(path, "fstsrv")

    # -- files ----------------------------------------------------------------

    def read_files(self, paths):
        """{path: text} for the files that exist, in one round trip."""
        if not paths:
            return {}
        script = 'for f in "$@"; do if [ -f "$f" ]; then printf "@@WV %s %s\\n" "$(wc -c < "$f" | tr -d " ")" "$f"; cat "$f"; fi; done'
        raw = self.run("sh -c %s sh %s" % (shlex.quote(script), " ".join(shlex.quote(p) for p in paths)))
        out = {}
        k = 0
        while k < len(raw):
            nl = raw.index(b"\n", k)
            head = raw[k:nl].decode(errors="replace")
            m = re.match(r"@@WV (\d+) (.*)$", head)
            if not m:
                break
            n = int(m.group(1))
            out[m.group(2)] = raw[nl + 1:nl + 1 + n].decode(errors="replace")
            k = nl + 1 + n
        return out

    def fetch_file(self, path, local, timeout=3600):
        """Copy a file from the server to `local`, streamed (it may be
        hundreds of MB: a name index)."""
        with open(local, "wb") as fh:
            proc = subprocess.Popen(self.argv("cat %s" % shlex.quote(path), compress=True), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.PIPE)
            _, err = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            raise RemoteError("%s: fetching %s failed (%d): %s" % (self, path, proc.returncode,
                                                                   err.decode(errors="replace").strip()[-800:]))

    def put_file(self, path, data):
        """Write data to path on the server (directories made as needed)."""
        self.run("mkdir -p %s && cat > %s" % (shlex.quote(posixpath.dirname(path)), shlex.quote(path)), data=data)

    def index_rtl(self, specs, root):
        """Run rtlindex.py on the server over its own sources and get
        the design JSON back. Nothing needs to be installed there but
        python3: the script is sent over ssh. A Verdi KDB (work.lib++,
        simv.daidir) is exported there through the server's Verdi, with
        npi_export.py sent along."""
        with open(os.path.join(HERE, "rtlindex.py"), "rb") as fh:
            src = fh.read()
        env = ""
        if any(s.rstrip("/").endswith((".lib++", ".daidir")) for s in specs):
            with open(os.path.join(HERE, "npi_export.py"), "rb") as fh:
                exporter = fh.read()
            # ssh commands start in the home directory: a relative path there
            self.put_file(".cache/waveview/npi_export.py", exporter)
            env = 'WAVEVIEW_NPI_EXPORT="$HOME/.cache/waveview/npi_export.py" '
        args = " ".join(shlex.quote(s) for s in specs)
        cmd = "cd %s && %spython3 - %s -o /dev/stdout --quiet" % (shlex.quote(root), env, args)
        raw = self.run(cmd, data=src, timeout=1800)
        return json.loads(raw.decode(errors="replace"))


def with_remote_env(command):
    """The command line for the server with the variables of
    WAVEVIEW_REMOTE_ENV set, e.g. "VERDI_HOME=/tools/verdi/Y-2026.03" when
    the server's login environment does not say where Verdi is (ssh runs
    commands without a login shell, so ~/.bashrc exports may not be there).
    The command is a shell line (it may start with cd, or chain with &&), so
    it runs under sh with the variables, whatever the login shell there."""
    text = os.environ.get("WAVEVIEW_REMOTE_ENV", "").strip()
    if not text:
        return command
    pairs = []
    for word in shlex.split(text):
        name, eq, value = word.partition("=")
        if not eq or not re.match(r"^[A-Za-z_]\w*$", name):
            raise RemoteError("WAVEVIEW_REMOTE_ENV: expected NAME=value, got %r" % word)
        pairs.append("%s=%s" % (name, shlex.quote(value)))
    return "env %s sh -c %s" % (" ".join(pairs), shlex.quote(command))


SOURCES = {"fstsrv": "fstsrv.c", "fsdbsrv": "fsdbsrv.cc"}

# Build scripts run on the server with the source on stdin. ssh runs them
# without a login shell, so a PATH set in ~/.profile is not there: the tools
# are also looked for through a login shell.
BUILD_SCRIPTS = {
    "fstsrv": r'''
d="$HOME/.cache/waveview/wavesrv/%(digest)s"
if [ -x "$d/fstsrv" ]; then echo "ok $d/fstsrv"; cat > /dev/null; exit 0; fi
mkdir -p "$d" && cat > "$d/fstsrv.c" || exit 3
R=""
for c in "$(verilator --getenv VERILATOR_ROOT 2>/dev/null)" "$VERILATOR_ROOT" \
    "$("${SHELL:-sh}" -lc 'verilator --getenv VERILATOR_ROOT' 2>/dev/null </dev/null | tail -n 1)" \
    "$HOME/.local/share/verilator" /usr/local/share/verilator /usr/share/verilator \
    "$HOME/oss-cad-suite/share/verilator"; do
  if [ -n "$c" ] && [ -f "$c/include/gtkwave/fstapi.c" ]; then R=$c; break; fi
done
I="$R/include/gtkwave"
if [ ! -f "$I/fstapi.c" ]; then echo "no-fst-lib"; exit 0; fi
CC=${CC:-cc}
X=""
for f in fastlz.c lz4.c; do [ -f "$I/$f" ] && X="$X $I/$f"; done
$CC -O2 -w '-DFST_CONFIG_INCLUDE="fst_config.h"' -I"$I" -o "$d/fstsrv.tmp" "$d/fstsrv.c" "$I/fstapi.c" $X -lz -lpthread 2>"$d/build.log" \
  || $CC -O2 -w '-DFST_CONFIG_INCLUDE="fst_config.h"' -I"$I" -o "$d/fstsrv.tmp" "$d/fstsrv.c" "$I/fstapi.c" -lz -lpthread 2>>"$d/build.log" \
  || { echo "build-failed $d/build.log"; tail -5 "$d/build.log"; exit 0; }
mv "$d/fstsrv.tmp" "$d/fstsrv" && echo "ok $d/fstsrv"
''',
    "fsdbsrv": r'''
d="$HOME/.cache/waveview/wavesrv/%(digest)s"
if [ -x "$d/fsdbsrv" ]; then echo "ok $d/fsdbsrv"; cat > /dev/null; exit 0; fi
mkdir -p "$d" && cat > "$d/fsdbsrv.cc" || exit 3
home_of() { b=$(command -v "$1" 2>/dev/null) || return 1; b=$(readlink -f "$b" 2>/dev/null || echo "$b"); dirname "$(dirname "$b")"; }
V=""
for c in "$VERDI_HOME" "$NOVAS_HOME" "$(home_of verdi)" "$(home_of fsdb2vcd)" \
    "$("${SHELL:-sh}" -lc 'echo "$VERDI_HOME"' 2>/dev/null </dev/null | tail -n 1)" \
    "$("${SHELL:-sh}" -lc 'b=$(command -v fsdb2vcd) && b=$(readlink -f "$b" 2>/dev/null || echo "$b") && dirname "$(dirname "$b")"' 2>/dev/null </dev/null | tail -n 1)"; do
  if [ -n "$c" ] && [ -f "$c/share/FsdbReader/ffrAPI.h" ]; then V=$c; break; fi
done
if [ -z "$V" ]; then echo "no-fsdb-lib"; exit 0; fi
I="$V/share/FsdbReader"
L=""
for s in LINUX64 linux64 LINUX linux .; do if ls "$I/$s"/libnffr.* >/dev/null 2>&1; then L="$I/$s"; break; fi; done
if [ -z "$L" ]; then echo "no-fsdb-lib"; exit 0; fi
: > "$d/build.log"
built=""
for x in "$CXX" c++ g++ /usr/bin/c++ /usr/bin/g++; do
  [ -n "$x" ] && command -v "$x" >/dev/null 2>&1 || continue
  if "$x" -O2 -w -I"$I" -o "$d/fsdbsrv.tmp" "$d/fsdbsrv.cc" -L"$L" -Wl,-rpath,"$L" -lnffr -lnsys -ldl -lpthread -lz 2>>"$d/build.log"; then built=1; break; fi
done
if [ -z "$built" ]; then echo "build-failed $d/build.log"; tail -5 "$d/build.log"; exit 0; fi
mv "$d/fsdbsrv.tmp" "$d/fsdbsrv" && echo "ok $d/fsdbsrv"
''',
}


_SPEC_URL = re.compile(r"^ssh://(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?(?P<path>/.*)$")
_SPEC_SCP = re.compile(r"^(?:(?P<user>[^@/:]+)@)?(?P<host>[^:/\\]{2,}):(?P<path>/.*)$")


def parse(spec):
    """(Remote, remote path) for a remote spec, or None for a local path."""
    if not isinstance(spec, str):
        return None
    m = _SPEC_URL.match(spec) or _SPEC_SCP.match(spec)
    if not m:
        return None
    host = m.group("host")
    if m.groupdict().get("user"):
        host = m.group("user") + "@" + host
    return Remote(host, m.groupdict().get("port")), posixpath.normpath(m.group("path"))


def is_remote(spec):
    return parse(spec) is not None


def main(argv=None):
    """remote.py <spec>: check that a remote dump can be served."""
    spec = (argv or sys.argv[1:] or [""])[0]
    p = parse(spec)
    if p is None:
        print("not a remote spec: %r (use ssh://host/abs/path or host:/abs/path)" % spec)
        return 2
    r, path = p
    tool = "fsdbsrv" if path.lower().endswith(".fsdb") else "fstsrv"
    print("host %s, path %s" % (r, path))
    print("%s on the server: %s" % (tool, r.srv(tool)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
