"""Small helpers shared by the engine: subprocess, time, JSON files, globs. Standard library only (Python 3.9+)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence


class CmdResult:
    __slots__ = ("rc", "out", "err")

    def __init__(self, rc: int, out: str, err: str):
        self.rc, self.out, self.err = rc, out or "", err or ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def text(self) -> str:
        return (self.out + ("\n" if self.out and self.err else "") + self.err).strip()


def run(cmd: Sequence[str], cwd: Optional[str] = None, timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None, input: Optional[str] = None) -> CmdResult:
    try:
        p = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                           input=input, errors="replace")
        return CmdResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return CmdResult(124, out, "timed out after %ss" % timeout)
    except FileNotFoundError as e:
        return CmdResult(127, "", str(e))


def run_shell(command: str, cwd: str, timeout: Optional[float] = None) -> CmdResult:
    """A project command (build/test/lint) as the user wrote it — inherits this process's environment."""
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


def _glob_re(pattern: str) -> "re.Pattern[str]":
    rx = _GLOB_CACHE.get(pattern)
    if rx is None:
        i, out = 0, ""
        while i < len(pattern):
            c = pattern[i]
            if pattern.startswith("**/", i):
                out += "(?:.*/)?"; i += 3; continue
            if pattern.startswith("**", i):
                out += ".*"; i += 2; continue
            out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
            i += 1
        rx = _GLOB_CACHE[pattern] = re.compile("^" + out + "$")
    return rx


def glob_match(pattern: str, path: str) -> bool:
    """`dir/` = prefix, `**` any depth, a pattern without `/` also matches the basename."""
    pattern = pattern.strip()
    if not pattern:
        return False
    if path.startswith("./"):
        path = path[2:]
    if pattern.endswith("/"):
        return path == pattern[:-1] or path.startswith(pattern)
    if _glob_re(pattern).match(path):
        return True
    if "/" not in pattern:
        return bool(_glob_re(pattern).match(path.rsplit("/", 1)[-1]))
    return False


def any_glob(patterns: List[str], path: str) -> Optional[str]:
    for p in patterns or []:
        if glob_match(p, path):
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
