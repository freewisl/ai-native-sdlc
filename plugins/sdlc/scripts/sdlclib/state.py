"""Engine state under `.sdlc/state/` — only what the repository cannot say.

The repository stays the source of truth for an item's progress (its work doc on the default branch and on
`sdlc/<slug>`). This file keeps the rest: failure counts, why an item waits for a person, answers not yet applied, the
last stopped session (for a warm resume), cost so far. Claims live in the git common dir so loops in several worktrees
of one repository never take the same item.
"""
from __future__ import annotations

import contextlib
import os
import time
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from .util import now_iso, proc_start, read_json, read_text, same_process, write_json, write_text

ITEMS_REL = os.path.join(".sdlc", "state", "items.json")
STATUS_REL = os.path.join(".sdlc", "state", "loop-status.json")
PID_REL = os.path.join(".sdlc", "state", "loop.pid")
ACTIVE_REL = os.path.join(".sdlc", "state", "active-work")
INBOX_REL = os.path.join(".sdlc", "state", "inbox")
RATINGS_REL = os.path.join(".sdlc", "state", "ratings.json")

try:
    import fcntl
except ImportError:            # not POSIX: no lock (the engine is single-writer in practice)
    fcntl = None  # type: ignore


@contextlib.contextmanager
def locked(root: str) -> Iterator[None]:
    path = os.path.join(root, ".sdlc", "state", ".lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        if fcntl:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def load_items(root: str) -> Dict[str, Dict[str, Any]]:
    data = read_json(os.path.join(root, ITEMS_REL), {}) or {}
    return data if isinstance(data, dict) else {}


def item(root: str, slug: str) -> Dict[str, Any]:
    return dict(load_items(root).get(slug) or {})


def mutate(root: str, slug: str, fn: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """Read-modify-write one item record under the state lock. fn edits the dict in place."""
    with locked(root):
        data = load_items(root)
        rec = dict(data.get(slug) or {})
        fn(rec)
        rec["updated"] = now_iso()
        data[slug] = rec
        write_json(os.path.join(root, ITEMS_REL), data)
        return rec


def update(root: str, slug: str, **fields: Any) -> Dict[str, Any]:
    def fn(rec: Dict[str, Any]) -> None:
        for k, v in fields.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v
    return mutate(root, slug, fn)


def clear_wait(root: str, slug: str) -> Dict[str, Any]:
    return update(root, slug, state=None, reasons=None)


# ---------- the loop process ----------
def read_pid(root: str) -> Tuple[int, str]:
    """(pid, start time) from `loop.pid`: `<pid> <start>` as `write_pid` writes it, or a bare `<pid>` (an older file, the start
    time is then ""); (0, "") for a file that is not there or not a pid."""
    parts = (read_text(os.path.join(root, PID_REL)) or "").strip().split(None, 1)
    try:
        return (int(parts[0]), " ".join(parts[1].split()) if len(parts) > 1 else "") if parts else (0, "")
    except ValueError:
        return 0, ""


def loop_pid(root: str) -> int:
    """The pid of the loop running in `root`, 0 when there is none. A pid that is alive is not enough: after a loop that
    died without cleaning up (SIGKILL, a crash, a reboot) its number goes to another process in time, so the start time
    `write_pid` recorded must be the live process's too. A file without one (an older loop) is taken at its word."""
    pid, start = read_pid(root)
    return pid if pid > 0 and same_process(pid, start) else 0


def write_pid(root: str) -> None:
    start = proc_start(os.getpid())
    write_text(os.path.join(root, PID_REL), "%d%s\n" % (os.getpid(), " " + start if start else ""))


def clear_pid(root: str) -> None:
    txt = (read_text(os.path.join(root, PID_REL)) or "").strip()
    if txt.split()[:1] == [str(os.getpid())]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(root, PID_REL))


def read_status(root: str) -> Dict[str, Any]:
    return read_json(os.path.join(root, STATUS_REL), {}) or {}


def write_status(root: str, data: Dict[str, Any]) -> None:
    data = dict(data)
    data["updated"] = now_iso()
    write_json(os.path.join(root, STATUS_REL), data)


def set_active(root: str, slug: Optional[str]) -> None:
    """The work doc the hooks read (test lock, plan requirement) while a session runs."""
    path = os.path.join(root, ACTIVE_REL)
    if slug:
        write_text(path, slug + "\n")
    else:
        with contextlib.suppress(OSError):
            os.remove(path)


# ---------- claims, shared by every worktree ----------
CLAIM_FILL_GRACE = 5.0      # seconds a claim file may stay empty while its creator writes it: not a dead one's, yet


class Claims:
    """Which loop works on which item, for the loops in the worktrees of one repository (a file per item in the common git
    dir: `<pid> <root>` and, on a second line, the start time of that pid). A claim is a file created exclusively, so two
    loops cannot both take an item; one that belongs to a dead process — or to a pid since reused by another — is taken over."""

    def __init__(self, common_dir: str, root: str):
        self.dir = os.path.join(common_dir, "sdlc-claims")
        self.root = root
        self.held: Optional[str] = None
        self._start: Optional[str] = None

    def _path(self, slug: str) -> str:
        return os.path.join(self.dir, slug)

    def _read(self, slug: str) -> Optional[Tuple[int, str, str]]:
        """(pid, root, start time) of a claim file; None when it is not there or says nothing a pid could be read from."""
        text = read_text(self._path(slug))
        if not text or not text.strip():
            return None
        lines = text.split("\n")
        head = lines[0].strip().split(None, 1)
        try:
            pid = int(head[0])
        except (ValueError, IndexError):
            return None
        return pid, (head[1].strip() if len(head) > 1 else "?"), (" ".join(lines[1].split()) if len(lines) > 1 else "")

    def owner(self, slug: str) -> Optional[str]:
        """The root of another live loop holding slug, else None."""
        claim = self._read(slug)
        if claim is None:
            try:                    # a file just created and not yet written is somebody's claim, not an abandoned one
                young = time.time() - os.path.getmtime(self._path(slug)) < CLAIM_FILL_GRACE
            except OSError:
                return None
            return "?" if young else None
        pid, root, start = claim
        if pid == os.getpid() or not same_process(pid, start):
            return None
        return root

    def _content(self) -> str:
        if self._start is None:
            self._start = proc_start(os.getpid())
        return "%d %s\n%s\n" % (os.getpid(), self.root, self._start)

    def _create(self, path: str) -> bool:
        """Create the claim file, and fail when there is one: the one step two loops cannot both win."""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        try:
            os.write(fd, self._content().encode("utf-8"))
        except OSError:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.remove(path)
            raise
        os.close(fd)
        return True

    def take(self, slug: str) -> bool:
        """Claim slug: False when another live loop holds it. A claim of a dead loop (or of this process, left over) is
        replaced — under a lock, so that two loops that find the same dead claim do not both remove and take it."""
        os.makedirs(self.dir, exist_ok=True)
        path = self._path(slug)
        if not self._create(path):
            fh = open(os.path.join(self.dir, ".takeover.lock"), "a+")
            try:
                if fcntl:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                if self.owner(slug):
                    return False
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path)
                if not self._create(path):          # a loop that found the file gone took it in the same moment
                    return False
            finally:
                fh.close()                          # releases the lock
        self.held = slug
        return True

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                os.remove(self._path(self.held))
            self.held = None
