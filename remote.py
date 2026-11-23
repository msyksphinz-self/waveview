"""Remote dumps over ssh: the viewer runs on the desktop (Windows, macOS or
Linux), the dump stays on the build server.

    waveview.py serve ssh://user@build01/work/sim/w.fst --rtl ssh://user@build01/work/rtl/filelist.f
    waveview.py serve build01:/work/sim/w.fst          (scp style works too)
    wavequery.py info build01:/work/sim/w.fst

How:
- fstsrv.c speaks a line protocol on stdin/stdout, so it runs on the
  server at the other end of an ssh session: `ssh -T -C host fstsrv w.fst`.
  Only what a view or a query asks for crosses the link, compressed
- The first connection to a host builds fstsrv there: the C source is sent
  over ssh and compiled against the server's Verilator FST library, then
  cached in ~/.cache/waveview/wavesrv/<source hash>/ on the server
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

    # -- fstsrv ---------------------------------------------------------------

    def _known_path(self):
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        return os.path.join(base, "waveview", "remote_fstsrv.json")

    def _known(self):
        try:
            with open(self._known_path()) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def forget_fstsrv(self):
        known = self._known()
        known.pop(self._key(), None)
        self._save_known(known)

    def _save_known(self, known):
        try:
            os.makedirs(os.path.dirname(self._known_path()), exist_ok=True)
            with open(self._known_path(), "w") as fh:
                json.dump(known, fh)
        except OSError:
            pass

    def _key(self):
        with open(os.path.join(HERE, "fstsrv.c"), "rb") as fh:
            digest = hashlib.sha1(fh.read()).hexdigest()[:12]
        return "%s|%s|%s" % (self.host, self.port or "", digest)

    def fstsrv(self):
        """Path of a built fstsrv on the server, built on first use. The path
        is remembered here, so later runs start without the extra round trip."""
        override = os.environ.get("WAVEVIEW_REMOTE_FSTSRV")
        if override:
            return override
        known = self._known()
        if self._key() in known:
            return known[self._key()]
        path = self._build_fstsrv()
        known[self._key()] = path
        self._save_known(known)
        return path

    def _build_fstsrv(self):
        with open(os.path.join(HERE, "fstsrv.c"), "rb") as fh:
            src = fh.read()
        digest = hashlib.sha1(src).hexdigest()[:12]
        script = r'''
d="$HOME/.cache/waveview/wavesrv/%s"
if [ -x "$d/fstsrv" ]; then echo "ok $d/fstsrv"; cat > /dev/null; exit 0; fi
mkdir -p "$d" && cat > "$d/fstsrv.c" || exit 3
# ssh runs this without a login shell, so a PATH set in ~/.profile is not here
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
''' % digest
        out = self.run("sh -c %s" % shlex.quote(script), data=src).decode(errors="replace").strip()
        if out.startswith("ok "):
            return out.split(" ", 1)[1].splitlines()[0]
        if out.startswith("no-fst-lib"):
            raise RemoteError("%s has no Verilator FST library to build fstsrv with; put verilator on PATH "
                              "there, set VERILATOR_ROOT, or point WAVEVIEW_REMOTE_FSTSRV at a built fstsrv"
                              % self.host)
        raise RemoteError("building fstsrv on %s failed:\n%s" % (self.host, out))

    def fstsrv_argv(self, path):
        return self.argv("%s %s" % (shlex.quote(self.fstsrv()), shlex.quote(path)), compress=True)

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

    def index_rtl(self, specs, root):
        """Run rtlindex.py on the server over its own sources and get
        the design JSON back. Nothing needs to be installed there but
        python3: the script is sent over ssh."""
        with open(os.path.join(HERE, "rtlindex.py"), "rb") as fh:
            src = fh.read()
        args = " ".join(shlex.quote(s) for s in specs)
        cmd = "cd %s && python3 - %s -o /dev/stdout --quiet" % (shlex.quote(root), args)
        raw = self.run(cmd, data=src, timeout=1800)
        return json.loads(raw.decode(errors="replace"))


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
    print("host %s, path %s" % (r, path))
    print("fstsrv on the server: %s" % r.fstsrv())
    return 0


if __name__ == "__main__":
    sys.exit(main())
