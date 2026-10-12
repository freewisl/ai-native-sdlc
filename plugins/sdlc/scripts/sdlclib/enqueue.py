"""Putting work docs on the queue = committing them to the default branch.

- No remote: commit on the default branch here (or where it is checked out). While a loop runs in this checkout the docs
  go to `.sdlc/state/inbox/` and the loop commits them between items, so the two never fight over the working tree.
- GitHub: a `sdlc/queue-<time>` branch built in a throwaway worktree, a PR, and (solo) an immediate merge. In a team
  repository that PR is the approval point: a person merging it is what puts the items on the queue.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from typing import Dict, List, Optional, Tuple

from .config import Config
from .gitops import Forge, Git
from .state import INBOX_REL, loop_pid, read_status
from .util import now_iso, read_text, stamp, write_text
from .workdoc import WorkDoc, problems

NEW_STATUSES = ("ready", "proposed", "needs-input")


class EnqueueError(Exception):
    pass


def prepare(cfg: Config, git: Git, base: str, paths: List[str], source: str = "") -> List[WorkDoc]:
    """Load and check the docs; fill in created/source; refuse what would break the queue."""
    docs: List[WorkDoc] = []
    errors: List[str] = []
    work = cfg.rel("work")
    for p in paths:
        d = WorkDoc.load(p)
        if d is None:
            errors.append("%s: cannot read" % p)
            continue
        file_slug = os.path.splitext(os.path.basename(p))[0]
        errs = problems(d, file_slug)
        if d.status not in NEW_STATUSES:
            errs.append("status '%s' — a new item is ready, proposed or needs-input" % d.status)
        committed = git.show(base, "%s/%s.md" % (work, d.slug))
        if committed is not None and committed.strip() != (read_text(p) or "").strip():
            errs.append("work/%s.md is already on %s — pick another slug" % (d.slug, base))
        if "slug" not in d.meta:
            d.meta["slug"] = d.slug
        if not d.created:
            d.meta["created"] = now_iso()
        if source and not d.meta.get("source"):
            d.meta["source"] = source
        d.meta.setdefault("source", "user")
        d.meta.setdefault("depends_on", [])
        d.meta.setdefault("test_changes", "none")
        errors += ["%s: %s" % (p, e) for e in errs]
        docs.append(d)
    known = {d.slug for d in docs}
    for d in docs:
        for dep in d.depends_on:
            if dep not in known and git.show(base, "%s/%s.md" % (work, dep)) is None:
                errors.append("%s: depends_on '%s' — no such work doc" % (d.slug, dep))
    cyc = _cycle({d.slug: d.depends_on for d in docs})
    if cyc:
        errors.append("dependency cycle: " + " → ".join(cyc))
    if len(known) != len(docs):
        errors.append("two docs share a slug")
    if errors:
        raise EnqueueError("\n".join(errors))
    return docs


def _cycle(graph: Dict[str, List[str]]) -> Optional[List[str]]:
    state: Dict[str, int] = {}
    stack: List[str] = []

    def visit(n: str) -> Optional[List[str]]:
        state[n] = 1
        stack.append(n)
        for m in graph.get(n, []):
            if m not in graph:
                continue
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if not state.get(m):
                r = visit(m)
                if r:
                    return r
        stack.pop()
        state[n] = 2
        return None

    for n in graph:
        if not state.get(n):
            r = visit(n)
            if r:
                return r
    return None


def _commit_docs(git: Git, where: str, work: str, docs: List[WorkDoc], message: str) -> Tuple[bool, str]:
    rels = []
    for d in docs:
        rel = "%s/%s.md" % (work, d.slug)
        d.save(os.path.join(where, rel))
        rels.append(rel)
    r = git("add", "--", *rels, cwd=where)
    if not r.ok:
        return False, r.text()
    r = git("commit", "-q", "--no-verify", "-m", message, "--", *rels, cwd=where)
    return r.ok, (r.text() if not r.ok else git.out("rev-parse", "HEAD", cwd=where))


def message_for(docs: List[WorkDoc]) -> str:
    head = "sdlc: queue %s" % docs[0].slug if len(docs) == 1 else "sdlc: queue %d work items" % len(docs)
    return head + "\n\n" + "\n".join("- %s — %s (%s)" % (d.slug, d.title, d.status) for d in docs)


def enqueue(cfg: Config, git: Git, forge: Forge, docs: List[WorkDoc], originals: List[str],
            from_loop: bool = False) -> Dict[str, object]:
    """Commit docs to the default branch. Returns {queued, how, pr, merged}. Originals (the files the docs came from) are
    removed from the working tree once their content is safely committed elsewhere."""
    work = cfg.rel("work")
    default = cfg.default_branch
    result: Dict[str, object] = {"queued": [d.slug for d in docs], "how": "", "pr": "", "merged": False}
    msg = message_for(docs)
    if forge.kind == "local":
        here_loop = loop_pid(git.root) and read_status(git.root).get("root") == git.root
        if here_loop and not from_loop:
            for d in docs:
                d.save(os.path.join(git.root, INBOX_REL, d.slug + ".md"))
            result["how"] = "inbox (the running loop commits them between items)"
            _drop_originals(git, originals, work)
            return result
        if git.branch() == default:
            ok, info = _commit_docs(git, git.root, work, docs, msg)
            if not ok:
                raise EnqueueError("commit failed: " + info)
            result["how"] = "committed on %s" % default
            return result
        where = git.worktree_of(default)
        if where:
            if git("status", "--porcelain", cwd=where).out.strip():
                raise EnqueueError("%s is checked out in %s with uncommitted changes — commit them first" % (default, where))
            ok, info = _commit_docs(git, where, work, docs, msg)
        else:
            tmp = tempfile.mkdtemp(prefix="sdlc-queue-")
            os.rmdir(tmp)
            if not git("worktree", "add", "-q", tmp, default).ok:
                raise EnqueueError("cannot check out %s in a temporary worktree" % default)
            try:
                ok, info = _commit_docs(git, tmp, work, docs, msg)
            finally:
                git("worktree", "remove", "--force", tmp)
                shutil.rmtree(tmp, ignore_errors=True)
        if not ok:
            raise EnqueueError("commit failed: " + info)
        result["how"] = "committed on %s" % default
        _drop_originals(git, originals, work)
        return result

    # GitHub: a queue PR from a throwaway worktree, merged at once when solo
    git.fetch()
    base = git.base_ref(default)
    qslug = "queue-" + stamp().lower()
    branch = "sdlc/" + qslug
    tmp = tempfile.mkdtemp(prefix="sdlc-queue-")
    os.rmdir(tmp)
    if not git("worktree", "add", "-q", "-b", branch, tmp, base).ok:
        raise EnqueueError("cannot create %s in a temporary worktree" % branch)
    try:
        ok, info = _commit_docs(git, tmp, work, docs, msg)
        if not ok:
            raise EnqueueError("commit failed: " + info)
    finally:
        git("worktree", "remove", "--force", tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    title = msg.split("\n", 1)[0]
    body = msg.split("\n", 1)[1].strip() + ("\n\nMerging this PR puts these items on the sdlc queue." if not cfg.solo else "")
    ok, url = forge.publish(qslug, title, body)
    if not ok:
        git("branch", "-D", branch)
        raise EnqueueError(url)
    result["pr"] = url
    _drop_originals(git, originals, work)
    if cfg.solo:
        ok, info = forge.merge(qslug, title)
        result["merged"] = ok
        result["how"] = "merged %s" % url if ok else "PR %s could not be merged (%s) — merge it to queue the items" % (url, info)
    else:
        result["how"] = "PR %s — the items join the queue when a person merges it" % url
    git("branch", "-D", branch)
    if result["merged"] and git.branch() == default and not git.dirty() and not loop_pid(git.root):
        git("pull", "-q", "--ff-only", timeout=120)
    return result


def _drop_originals(git: Git, paths: List[str], work: str) -> None:
    """Remove the source files when they are untracked copies in work/ (they come back with the next pull)."""
    for p in paths:
        ap = os.path.abspath(p)
        rel = os.path.relpath(ap, git.root)
        if not rel.startswith(work + "/"):
            if rel.startswith(os.path.join(".sdlc", "state")) and os.path.isfile(ap):
                os.remove(ap)
            continue
        if git("ls-files", "--error-unmatch", "--", rel).ok:
            continue
        if os.path.isfile(ap):
            os.remove(ap)


def drain_inbox(cfg: Config, git: Git, forge: Forge) -> List[str]:
    """Called by the loop when it is on the default branch: commit what `enqueue` parked while it ran."""
    inbox = os.path.join(git.root, INBOX_REL)
    try:
        names = sorted(n for n in os.listdir(inbox) if n.endswith(".md"))
    except OSError:
        return []
    paths = [os.path.join(inbox, n) for n in names]
    if not paths:
        return []
    base = git.base_ref(cfg.default_branch)
    docs = prepare(cfg, git, base, paths)
    enqueue(cfg, git, forge, docs, paths, from_loop=True)
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    return [d.slug for d in docs]


def new_docs(cfg: Config, git: Git) -> List[str]:
    """Untracked or modified work docs in the working tree (`enqueue --new`)."""
    work = cfg.rel("work")
    out = []
    for line in git("status", "--porcelain", "--untracked-files=all", "--", work + "/").out.splitlines():
        path = line[3:].strip().strip('"')
        if re.search(r"\.md$", path) and os.path.basename(path) not in ("README.md", "TEMPLATE.md"):
            out.append(os.path.join(git.root, path))
    return sorted(out)


def write_doc(path: str, doc: WorkDoc) -> None:
    write_text(path, doc.dumps())
