"""The queue: every work doc on the default branch, with its progress read from `sdlc/<slug>` when that branch exists.

Docs are read from git refs, not the working tree, so the answer is the same whichever branch is checked out (the loop
moves between branches; status may run while it does).

Phases (`Item.phase`):
  runnable   design · build · gate · merge · answer (an answer is waiting to be applied)
  team waits approval (design PR not approved yet) · awaiting-merge (gate passed, a person merges)
  for a person  proposed · needs-input · needs-human · blocked (failed too often)
  finished   done · dropped · closed (PR closed by a person)

The engine merges only `done` docs, so a doc that is `built` on the default branch was merged by a person (a change that
waited for them): it counts as done, whether they merged, squashed or took the PR — and with no engine state at all.
"""
from __future__ import annotations

import subprocess
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config
from .gitops import Git
from .workdoc import WorkDoc

RUNNABLE = ("merge", "gate", "build", "answer", "design")
TEAM_WAITS = ("approval", "awaiting-merge")
HUMAN = ("proposed", "needs-input", "needs-human", "blocked")
FINISHED = ("done", "dropped", "closed")


def read_blobs(git: Git, specs: Sequence[str]) -> Dict[str, Optional[str]]:
    """`ref:path` → file text (None when missing), in one `git cat-file --batch`."""
    out: Dict[str, Optional[str]] = {}
    if not specs:
        return out
    try:
        p = subprocess.run(["git", "-C", git.root, "cat-file", "--batch"], input=("\n".join(specs) + "\n").encode("utf-8"),
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {s: None for s in specs}
    data, i = p.stdout, 0
    for spec in specs:
        nl = data.find(b"\n", i)
        if nl < 0:
            out[spec] = None
            continue
        parts = data[i:nl].decode("utf-8", "replace").split()
        i = nl + 1
        if len(parts) == 3 and parts[2].isdigit():
            size = int(parts[2])
            out[spec] = data[i:i + size].decode("utf-8", "replace") if parts[1] == "blob" else None
            i += size + 1
        else:
            out[spec] = None
    return out


class Item:
    def __init__(self, slug: str):
        self.slug = slug
        self.doc: Optional[WorkDoc] = None          # on the default branch
        self.branch_doc: Optional[WorkDoc] = None   # at the tip of sdlc/<slug>
        self.branch = ""                            # refs/heads/sdlc/<slug> or refs/remotes/origin/sdlc/<slug>
        self.rec: Dict[str, object] = {}
        self.phase = ""
        self.wait = ""                              # why it is not runnable now
        self.wait_kind = ""                         # human | poll | dep
        self.blocker = ""                           # the dependency it waits for

    @property
    def title(self) -> str:
        d = self.branch_doc or self.doc
        return d.title if d else self.slug

    @property
    def status(self) -> str:
        d = self.branch_doc or self.doc
        return d.status if d else ""

    @property
    def created(self) -> str:
        return self.doc.created if self.doc else ""

    @property
    def depends_on(self) -> List[str]:
        d = self.doc or self.branch_doc
        return d.depends_on if d else []

    def as_dict(self) -> dict:
        return {"slug": self.slug, "title": self.title, "status": self.status, "phase": self.phase, "wait": self.wait,
                "wait_kind": self.wait_kind, "branch": bool(self.branch), "depends_on": self.depends_on,
                "failures": int(self.rec.get("failures", 0) or 0), "cost_usd": round(float(self.rec.get("cost", 0) or 0), 2),
                "pr": self.rec.get("pr", ""), "reasons": self.rec.get("reasons", []) or []}


def phase_of(it: Item, team: bool, max_failures: int) -> str:
    rec = it.rec
    st = str(rec.get("state") or "")
    if st in ("dropped", "closed"):
        return st
    if st == "merged":                      # a person merged what waited for them, and the doc on the default branch says less than built
        return "done"
    d = it.doc.status if it.doc else ""
    if d in ("done", "dropped"):
        return d
    if d == "built":                        # only a person lands a built doc on the default branch (see the module note)
        return "done"
    s = (it.branch_doc.status if it.branch_doc else "") or d or "ready"
    if s in ("dropped", "proposed"):
        return s
    if s == "needs-input":
        return "answer" if rec.get("answers") else "needs-input"
    if st == "needs-human":
        return "needs-human"
    if st == "awaiting-merge" and team:
        return "awaiting-merge"
    if int(rec.get("failures", 0) or 0) >= max_failures:
        return "blocked"
    if s == "done":
        return "awaiting-merge" if team else "merge"
    if s == "built":
        return "gate"
    if s == "planned":
        return "approval" if team and not rec.get("approved") else "build"
    return "design"


def scan(cfg: Config, git: Git, base: str, records: Dict[str, Dict[str, object]]) -> Dict[str, Item]:
    work = cfg.rel("work")
    listed = git("ls-tree", "-z", "--name-only", base, "--", work + "/")        # NUL-separated: a name git would quote is the name
    names = [n for n in (listed.out.split("\0") if listed.ok else [])
             if n.endswith(".md") and n.rsplit("/", 1)[-1] not in ("README.md", "TEMPLATE.md")]
    blobs = read_blobs(git, ["%s:%s" % (base, n) for n in names])
    items: Dict[str, Item] = {}
    for n in names:
        text = blobs.get("%s:%s" % (base, n))
        if text is None:
            continue
        doc = WorkDoc.parse(text, n)
        slug = n.rsplit("/", 1)[-1][:-3]
        it = items.setdefault(slug, Item(slug))
        it.doc = doc
    # progress on sdlc/<slug> (local branch first, else the remote one)
    refs: Dict[str, str] = {}
    for ref in git.out("for-each-ref", "--format=%(refname)", "refs/remotes/origin/sdlc/", "refs/heads/sdlc/").splitlines():
        slug = ref.split("/sdlc/", 1)[-1]
        if ref.startswith("refs/heads/") or slug not in refs:
            refs[slug] = ref
    wanted = [(slug, ref) for slug, ref in refs.items() if slug in items or slug in records]
    bblobs = read_blobs(git, ["%s:%s/%s.md" % (ref, work, slug) for slug, ref in wanted])
    for slug, ref in wanted:
        text = bblobs.get("%s:%s/%s.md" % (ref, work, slug))
        if text is None:
            continue
        it = items.setdefault(slug, Item(slug))
        it.branch, it.branch_doc = ref, WorkDoc.parse(text, "%s/%s.md" % (work, slug))
    team = not cfg.solo
    maxf = int(cfg.get("loop.max_failures_per_slug", 3) or 3)
    for slug, it in items.items():
        it.rec = dict(records.get(slug) or {})
        it.phase = phase_of(it, team, maxf)
    _dependencies(items)
    return items


def _dependencies(items: Dict[str, Item]) -> None:
    for it in items.values():
        if it.phase in FINISHED:
            continue
        if it.phase in HUMAN:
            it.wait_kind, it.wait = "human", _human_reason(it)
            continue
        if it.phase in TEAM_WAITS:
            it.wait_kind = "poll"
            it.wait = "design PR waits for approval" if it.phase == "approval" else "gate passed — waits for a person to merge"
            continue
        for dep in it.depends_on:
            d = items.get(dep)
            if d is None:
                it.wait_kind, it.wait = "human", "depends on '%s', which has no work doc" % dep
            elif d.phase == "done":
                continue
            elif d.phase in ("dropped", "closed"):
                it.wait_kind, it.wait = "human", "depends on '%s', which was %s" % (dep, d.phase)
            elif d.phase in HUMAN:
                it.wait_kind, it.wait = "human", "depends on '%s', which waits for you" % dep
            else:
                it.wait_kind, it.wait = "dep", "depends on '%s' (%s)" % (dep, d.phase)
            it.blocker = dep
            break


def _human_reason(it: Item) -> str:
    if it.phase == "proposed":
        return "proposed — `sdlc.py accept %s` queues it" % it.slug
    if it.phase == "needs-input":
        return "has a question for you"
    if it.phase == "blocked":
        return "failed %s times" % it.rec.get("failures", "?")
    return "; ".join(str(r) for r in (it.rec.get("reasons") or [])[:3]) or "waits for a person"


RANK = {"merge": 0, "gate": 1, "build": 2, "answer": 3, "design": 4}


def runnable(items: Dict[str, Item], only: Optional[List[str]] = None, skip: Sequence[str] = ()) -> List[Item]:
    """Items to work on now, in order: furthest along first (finish before starting), then oldest."""
    out = [it for it in items.values() if it.phase in RUNNABLE and not it.wait and it.slug not in skip]
    if only is not None:
        pos = {s: i for i, s in enumerate(only)}
        return sorted([it for it in out if it.slug in pos], key=lambda it: pos[it.slug])
    return sorted(out, key=lambda it: (RANK.get(it.phase, 9), it.created or "9999", it.slug))


def counts(items: Dict[str, Item]) -> Dict[str, int]:
    c: Dict[str, int] = {}
    for it in items.values():
        c[it.phase] = c.get(it.phase, 0) + 1
    return c


def waiting(items: Dict[str, Item], kind: str) -> List[Tuple[str, str]]:
    return [(it.slug, it.wait) for it in sorted(items.values(), key=lambda i: i.slug) if it.wait_kind == kind]
