"""What the "Open" dialog of the viewer needs: the hosts of ~/.ssh/config,
directory listings here and over ssh, and a window of its own to run in.
"""

from __future__ import annotations

import glob
import os
import posixpath
import re
import shlex
import shutil
import string
import subprocess
import sys

import remote

DRIVES = "*"  # the "directory" above C:\ on Windows
LOCAL_DUMPS = (".fst", ".vcd", ".fsdb")
# what a listing shows besides folders: the dumps, or what --rtl takes (a
# filelist, a design JSON, a Verilator XML; a folder is chosen as such)
KINDS = {"dumps": LOCAL_DUMPS, "rtl": (".f", ".vc", ".flist", ".json", ".xml")}
MAX_ENTRIES = 5000
MAC = sys.platform == "darwin"

# Chromium browsers on macOS: app bundle, executable inside it
MAC_BROWSERS = (
    ("Google Chrome.app", "Google Chrome"),
    ("Microsoft Edge.app", "Microsoft Edge"),
    ("Chromium.app", "Chromium"),
    ("Brave Browser.app", "Brave Browser"),
)


def local_name():
    """What the Open dialog calls this machine."""
    return "This PC" if os.name == "nt" else "This Mac" if MAC else "This machine"

# the host goes on the ssh command line: it must never read as an option
_HOST = re.compile(r"^(?:\w[\w.-]*@)?\w[\w.-]*(?::\d+)?$")

_LIST = r'''
cd -- "$1" || exit 7
pwd
for f in *; do
  if [ -d "$f" ]; then
    case "$2:$f" in rtl:*.lib++|rtl:*.daidir) printf 'b 0 %s\n' "$f";; *) printf 'd 0 %s\n' "$f";; esac
  elif [ -f "$f" ]; then
    if [ "$2" = rtl ]; then
      case "$f" in *.f|*.F|*.vc|*.flist|*.json|*.xml) printf 'f %s %s\n' "$(wc -c < "$f" | tr -d ' ')" "$f";; esac
    else
      case "$f" in *.fst|*.FST|*.fsdb|*.FSDB) printf 'f %s %s\n' "$(wc -c < "$f" | tr -d ' ')" "$f";; esac
    fi
  fi
done
'''


def ssh_hosts(path=None):
    """Host aliases in an OpenSSH config, following Include. Patterns are
    left out: they are settings for other hosts, not places to connect to."""
    base = os.path.expanduser("~/.ssh")
    hosts, seen = [], set()

    def read(p):
        if p in seen:
            return
        seen.add(p)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return
        for line in lines:
            m = re.match(r"\s*(host|include)(?:\s*=\s*|\s+)(.*)$", line, re.I)
            if not m:
                continue
            words = [w.strip('"') for w in re.findall(r'"[^"]*"|[^\s"]+', m.group(2).split(" #")[0])]
            if m.group(1).lower() == "include":
                for w in words:
                    w = os.path.expanduser(w)
                    for inc in sorted(glob.glob(w if os.path.isabs(w) else os.path.join(base, w))):
                        read(inc)
            else:
                hosts.extend(w for w in words if not set(w) & set("*?!") and w not in hosts)

    read(path or os.path.join(base, "config"))
    return hosts


def remote_for(host):
    if not isinstance(host, str) or not _HOST.match(host):
        raise remote.RemoteError("not a host name: %r" % (host,))
    name, _, port = host.partition(":")
    return remote.Remote(name, port or None)


def _listing(path, parent, entries):
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": path, "parent": parent, "entries": entries[:MAX_ENTRIES], "more": len(entries) > MAX_ENTRIES}


def list_remote(host, path="", kind="dumps"):
    """A directory on the server: subdirectories and the dumps a server can
    serve, FST and FSDB (or, for kind "rtl", what --rtl takes). An empty
    path is the home directory, which also serves as the connection check."""
    r = remote_for(host)
    if kind not in KINDS:
        raise remote.RemoteError("no such listing kind: %r" % (kind,))
    path = path or "."
    if path == "~":
        path = "."
    elif path.startswith("~/"):
        path = path[2:] or "."
    try:
        raw = r.run("sh -c %s sh %s %s" % (shlex.quote(_LIST), shlex.quote(path), kind), timeout=30)
    except subprocess.TimeoutExpired:
        raise remote.RemoteError("%s did not answer in 30 s" % host)
    lines = raw.decode(errors="replace").split("\n")
    cwd = lines[0].strip()
    entries = []
    for line in lines[1:]:
        kind, _, rest = line.partition(" ")
        size, _, name = rest.partition(" ")
        if kind in ("d", "f", "b") and name:
            e = {"name": name, "dir": kind != "f", "size": int(size or 0), "path": posixpath.join(cwd, name)}
            if kind == "b":
                e["db"] = True  # a Verdi KDB: chosen as a whole, not entered
            entries.append(e)
    return _listing(cwd, posixpath.dirname(cwd) if cwd != "/" else None, entries)


def list_local(path="", kind="dumps"):
    """A directory on this machine: subdirectories and dumps (or, for kind
    "rtl", what --rtl takes)."""
    if kind not in KINDS:
        raise OSError("no such listing kind: %r" % (kind,))
    if path == DRIVES and os.name == "nt":
        roots = [d + ":\\" for d in string.ascii_uppercase if os.path.exists(d + ":\\")]
        return _listing(DRIVES, None, [{"name": d, "dir": True, "size": 0, "path": d} for d in roots])
    path = os.path.abspath(os.path.expanduser(path or "~"))
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir():
                    d = {"name": e.name, "dir": True, "size": 0, "path": e.path}
                    if kind == "rtl" and e.name.endswith((".lib++", ".daidir")):
                        d["db"] = True  # a Verdi KDB: chosen as a whole, not entered
                    entries.append(d)
                elif e.name.lower().endswith(KINDS[kind]):
                    entries.append({"name": e.name, "dir": False, "size": e.stat().st_size, "path": e.path})
            except OSError:
                continue
    parent = os.path.dirname(path)
    if parent == path:
        parent = DRIVES if os.name == "nt" else None
    return _listing(path, parent, entries)


def find_browser():
    """A Chromium browser, which can show a page as a plain application window."""
    if os.name == "nt":
        roots = [os.environ.get(v) for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for root in filter(None, roots):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                if os.path.isfile(os.path.join(root, rel)):
                    return os.path.join(root, rel)
        return None
    if MAC:
        for root in ("/Applications", os.path.expanduser("~/Applications")):
            for app, exe in MAC_BROWSERS:
                path = os.path.join(root, app, "Contents", "MacOS", exe)
                if os.path.isfile(path):
                    return path
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def open_window(url, profile):
    """Show url in its own window. With a profile of its own the browser
    process lives exactly as long as the window. None without a browser."""
    browser = find_browser()
    if browser is None:
        return None
    os.makedirs(profile, exist_ok=True)
    return subprocess.Popen([browser, "--app=" + url, "--user-data-dir=" + profile, "--no-first-run",
                             "--no-default-browser-check", "--disable-extensions", "--window-size=1400,860"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hide_console_if_ours():
    """Hide the console when nothing else uses it, i.e. started from Explorer.
    A onefile exe is two processes; a shell would be a third."""
    if os.name != "nt":
        return
    import ctypes

    k32 = ctypes.windll.kernel32
    if k32.GetConsoleProcessList(ctypes.byref(ctypes.c_ulong()), 1) <= 2:
        ctypes.windll.user32.ShowWindow(k32.GetConsoleWindow(), 0)


def show_console():
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 5)


def wait_for_quit(url):
    """The macOS app without a Chromium browser: the viewer is in the default
    browser, which gives no sign when its tab closes, so a dialog is what
    the user quits. Returns when they do (or the dialog is gone)."""
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display dialog "waveview is running in your browser at " & (item 1 of argv) & '
                          '"\\n\\nNo Chrome or Edge was found for a window of its own. Quit stops the viewer." '
                          'with title "waveview" buttons {"Quit"} default button 1',
                    "-e", "end run", url],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def report_error(text):
    """An error from the packaged app started by a double click. On macOS the
    app bundle has no console, so it goes in a dialog; elsewhere the console
    stays open until Enter."""
    print("error: %s" % text, file=sys.stderr)
    if MAC and (sys.stdin is None or not sys.stdin.isatty()):
        subprocess.run(["osascript", "-e", "on run argv",
                        "-e", 'display dialog (item 1 of argv) with title "waveview" buttons {"OK"} '
                              "default button 1 with icon stop",
                        "-e", "end run", text],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        input("Press Enter to close...")
    except EOFError:
        pass
