"""Engine state under `.sdlc/state/` — only what the repository cannot say.

The repository stays the source of truth for an item's progress (its work doc on the default branch and on
`sdlc/<slug>`). This file keeps the rest: failure counts, why an item waits for a person, answers not yet applied, the
last stopped session (for a warm resume), cost so far. Claims live in the git common dir so loops in several worktrees
of one repository never take the same item.
"""
from __future__ import annotations

import contextlib
import os
from typing import Any, Callable, Dict, Iterator, Optional

from .util import now_iso, pid_alive, read_json, read_text, write_json, write_text

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
def loop_pid(root: str) -> int:
    txt = (read_text(os.path.join(root, PID_REL)) or "").strip()
    try:
        pid = int(txt.split()[0]) if txt else 0
    except ValueError:
        return 0
    return pid if pid_alive(pid) else 0


def write_pid(root: str) -> None:
    write_text(os.path.join(root, PID_REL), "%d\n" % os.getpid())


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
class Claims:
    def __init__(self, common_dir: str, root: str):
        self.dir = os.path.join(common_dir, "sdlc-claims")
        self.root = root
        self.held: Optional[str] = None

    def owner(self, slug: str) -> Optional[str]:
        """The root of another live loop holding slug, else None."""
        txt = (read_text(os.path.join(self.dir, slug)) or "").split()
        if not txt:
            return None
        try:
            pid = int(txt[0])
        except ValueError:
            return None
        if pid == os.getpid() or not pid_alive(pid):
            return None
        return txt[1] if len(txt) > 1 else "?"

    def take(self, slug: str) -> bool:
        if self.owner(slug):
            return False
        write_text(os.path.join(self.dir, slug), "%d %s\n" % (os.getpid(), self.root))
        self.held = slug
        return True

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.dir, self.held))
            self.held = None
