"""Small helpers shared by the engine: subprocess, time, JSON files, globs. Standard library only (Python 3.9+)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


class CmdResult:
    __slots__ = ("rc", "out", "err")

    def __init__(self, rc: int, out: str, err: str):
        self.rc, self.out, self.err = rc, out or "", err or ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def text(self) -> str:
        return (self.out + ("\n" if self.out and self.err else "") + self.err).strip()


TERM_GRACE = 2.0        # seconds a command that ran out of time has to stop after SIGTERM, before its group is SIGKILLed


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(p: "subprocess.Popen[str]", grace: Optional[float] = None) -> None:
    """Stop a command started with `start_new_session=True` and everything it started: its whole process group is told to
    stop (SIGTERM — git and test runners clean up on it), and killed (SIGKILL) when any of it is still there `grace` seconds
    later (TERM_GRACE). The leader exiting is not the end: a worker it forked can ignore the signal and outlive it."""
    grace = TERM_GRACE if grace is None else grace
    pgid = p.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        p.terminate()
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if p.poll() is not None and not _group_alive(pgid):
            return
        time.sleep(0.02)
    if p.poll() is None or _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            p.kill()
    p.wait()


def run(cmd: Sequence[str], cwd: Optional[str] = None, timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None, input: Optional[str] = None) -> CmdResult:
    """Run a command and collect its output. The command gets a process group of its own: a command that outlives `timeout`
    is stopped together with everything it started (a test runner's workers, a dev server), not alone — rc 124."""
    try:
        p = subprocess.Popen(list(cmd), cwd=cwd, stdin=subprocess.PIPE if input is not None else None, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace", env=env, start_new_session=True)
    except FileNotFoundError as e:
        return CmdResult(127, "", str(e))
    try:
        out, err = p.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(p)
        try:
            out, _ = p.communicate(timeout=5)       # what it printed before it was stopped; a pipe a stray process holds is let go
        except subprocess.TimeoutExpired:
            out = ""
            for fh in (p.stdout, p.stderr):
                if fh:
                    fh.close()
        return CmdResult(124, out, "timed out after %ss" % timeout)
    except BaseException:                           # an interrupt of the engine itself: nothing is left running
        kill_group(p, grace=0)
        raise
    return CmdResult(p.returncode, out, err)


def run_shell(command: str, cwd: str, timeout: Optional[float] = None) -> CmdResult:
    """A project command (build/test/lint) as the user wrote it — inherits this process's environment. Like `run`, in a process
    group of its own that a timeout takes down as a whole."""
    return run(["bash", "-c", command], cwd=cwd, timeout=timeout)


def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def now_iso() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return now().strftime("%Y%m%dT%H%M%SZ")


def epoch() -> float:
    return now().timestamp()


def parse_iso(s: str) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s.strip().strip('"'), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        try:
            d = _dt.datetime.fromisoformat(s.strip().strip('"').replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return None


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: str, data: Any) -> None:
    """Atomic: a reader (status, another loop) never sees half a file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def append_line(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


_GLOB_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _glob_body(pattern: str) -> str:
    """The regular expression for one glob, without anchors: `**/` any number of directories (also none), `**` anything, `*` a
    run of characters but `/`, `?` one character but `/`; every other character stands for itself."""
    i, out = 0, ""
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3; continue
        if pattern.startswith("**", i):
            out += ".*"; i += 2; continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return out


def _rooted(pattern: str) -> Tuple[str, bool]:
    """(the pattern without its leading `/` and `./`, whether it had one): such a pattern is read from the repository root."""
    rooted = False
    while pattern.startswith("./") or pattern.startswith("/"):
        pattern, rooted = pattern[2:] if pattern.startswith("./") else pattern[1:], True
    return pattern, rooted


def _glob_re(pattern: str) -> "re.Pattern[str]":
    """The compiled glob. A pattern that ends in `/` is a directory: the directory itself or anything below it — the part
    before the slash is a glob like any other (`**/migrations/`, `src/*/secrets/`)."""
    rx = _GLOB_CACHE.get(pattern)
    if rx is None:
        if pattern.endswith("/"):
            rx = re.compile("^" + _glob_body(pattern.rstrip("/")) + r"(?:/.*)?\Z", re.S)
        else:
            rx = re.compile("^" + _glob_body(pattern) + r"\Z", re.S)
        _GLOB_CACHE[pattern] = rx
    return rx


def glob_match(pattern: str, path: str) -> bool:
    """`dir/` = the directory and everything under it, `**` any depth, `*` and `?` never cross `/`, a pattern without `/` also
    matches the file name at any depth, any other pattern is read from the repository root. A leading `/` or `./` says "from
    the root" and is not part of the path: `/infra/**` is `infra/**`, and `/*.tf` leaves out `deep/x/main.tf`."""
    pattern, rooted = _rooted(pattern.strip())
    if not pattern.strip("/"):
        return False
    if path.startswith("./"):
        path = path[2:]
    if _glob_re(pattern).match(path):
        return True
    if "/" not in pattern and not rooted:
        return bool(_glob_re(pattern).match(path.rsplit("/", 1)[-1]))
    return False


def any_glob(patterns: Union[str, List[str], None], path: str) -> Optional[str]:
    """The first of `patterns` (a list, or one pattern as a string) that matches `path`, as it was written; None when none does."""
    if isinstance(patterns, str):
        patterns = [patterns]
    for p in patterns or []:
        if isinstance(p, str) and glob_match(p, path):
            return p
    return None


def tail(text: str, lines: int = 40) -> str:
    return "\n".join((text or "").rstrip().splitlines()[-lines:])


def pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def proc_start(pid: Any) -> str:
    """When the process started, as `ps` prints it (`Thu Oct 16 10:22:33 2026`, spaces squeezed); "" when it is not running or
    `ps` cannot say. A pid number is reused after a process ends, its start time is not: the two together name one process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    r = run(["ps", "-o", "lstart=", "-p", str(pid)], env=dict(os.environ, LC_ALL="C"), timeout=10)
    return " ".join(r.out.split()) if r.ok else ""


def same_process(pid: Any, start: str) -> bool:
    """`pid` is alive and — when `start` (from `proc_start`, recorded when the process wrote its pid down) is known — it is
    still the process that started then: a different start time means the number belongs to a newer process. Without a
    recorded start, or when `ps` cannot say, being alive is all there is to go by."""
    if not pid_alive(pid):
        return False
    now_start = proc_start(pid) if start else ""
    return not (start and now_start and now_start != start)


def plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def plugin_version() -> str:
    data = read_json(os.path.join(plugin_root(), ".claude-plugin", "plugin.json"), {}) or {}
    return str(data.get("version", "?"))


def say(line: str, log_path: Optional[str] = None) -> None:
    """One progress line to stdout (and the run log)."""
    print(line, flush=True)
    if log_path:
        append_line(log_path, "%s %s" % (now().strftime("%H:%M:%S"), line))


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)
