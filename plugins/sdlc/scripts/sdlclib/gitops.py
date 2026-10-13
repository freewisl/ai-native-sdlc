"""git, plus the "forge" that turns a branch into a merged change: GitHub (gh) when there is an origin remote, a local
squash merge when there is none. Every state question answers `unknown` on an error instead of a guess."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from typing import List, Optional, Tuple

from .util import CmdResult, run

IGNORED_DIRTY = (".sdlc/state/", ".sdlc/logs/", ".sdlc/worktrees/")
# pathspecs that keep the same noise out of a commit the engine makes for a session
NOISE_EXCLUDES = (":(exclude).sdlc/state", ":(exclude).sdlc/logs", ":(exclude).sdlc/worktrees", ":(exclude,glob)**/__pycache__/**",
                  ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/.pytest_cache/**")


def is_noise(path: str) -> bool:
    """A path nobody wrote: the engine's own state and logs, and what a test run leaves behind — Python bytecode (anything
    under a `__pycache__/`, or a stray `.pyc`) and the pytest cache. Neither dirties the tree nor goes into a commit."""
    if any(path.startswith(p) for p in IGNORED_DIRTY):
        return True
    parts = path.rstrip("/").split("/")
    return "__pycache__" in parts or ".pytest_cache" in parts or path.endswith(".pyc")


def dirty_summary(lines: List[str], cap: int = 3) -> str:
    """The first few dirty entries for a refusal message — `?? tally/real.py; M  setup.py` — and how many more there are."""
    out = "; ".join(l.strip() for l in lines[:cap])
    return out + (" … (+%d more)" % (len(lines) - cap) if len(lines) > cap else "")


class Git:
    def __init__(self, root: str):
        self.root = root

    def __call__(self, *args: str, timeout: float = 300, cwd: Optional[str] = None) -> CmdResult:
        return run(["git", "-C", cwd or self.root] + list(args), timeout=timeout)

    def out(self, *args: str, cwd: Optional[str] = None) -> str:
        r = self(*args, cwd=cwd)
        return r.out.strip() if r.ok else ""

    def ok(self, *args: str, cwd: Optional[str] = None) -> bool:
        return self(*args, cwd=cwd).ok

    # ---------- state ----------
    def is_repo(self) -> bool:
        return self.out("rev-parse", "--is-inside-work-tree") == "true"

    def branch(self) -> str:
        return self.out("symbolic-ref", "--short", "-q", "HEAD")

    def head(self, ref: str = "HEAD") -> str:
        return self.out("rev-parse", "-q", "--verify", ref + "^{commit}")

    def dirty(self, cwd: Optional[str] = None) -> List[str]:
        """What a person would call uncommitted work, as `XY path` lines. Every untracked file is listed on its own — a
        folded `?? dir/` would hide what is inside — and the engine's state and the noise a test run leaves are left out."""
        entries = self("status", "--porcelain", "-z", "--untracked-files=all", cwd=cwd).out.split("\0")
        lines: List[str] = []
        i = 0
        while i < len(entries):
            entry, i = entries[i], i + 1
            if len(entry) < 4:
                continue
            if entry[0] in "RC" or entry[1] in "RC":
                i += 1                           # a rename or copy carries its source path as the next entry
            if not is_noise(entry[3:]):
                lines.append(entry)
        return lines

    def has_remote(self) -> bool:
        return self.ok("remote", "get-url", "origin")

    def branch_exists(self, name: str) -> bool:
        return self.ok("show-ref", "--verify", "-q", "refs/heads/" + name)

    def remote_branch_exists(self, name: str) -> bool:
        return self.ok("show-ref", "--verify", "-q", "refs/remotes/origin/" + name)

    def show(self, ref: str, path: str) -> Optional[str]:
        r = self("show", "%s:%s" % (ref, path))
        return r.out if r.ok else None

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)

    def changed(self, base: str, head: str) -> List[Tuple[str, str]]:
        """[(status, path)] between the merge base of base..head and head; renames report the new path as R."""
        r = self("diff", "--name-status", "-M", base + "..." + head)
        out = []
        for line in r.out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            kind = parts[0][:1]
            if kind in ("R", "C") and len(parts) > 2:
                out.append(("A", parts[2]))
                if kind == "R":
                    out.append(("D", parts[1]))   # a renamed file is gone from its old path
            else:
                out.append((kind, parts[1]))
        return out

    def diff(self, base: str, head: str, paths: Optional[List[str]] = None) -> str:
        args = ["diff", "-U0", base + "..." + head]
        if paths:
            args += ["--"] + paths
        return self(*args).out

    def diff_hash(self) -> str:
        """Fingerprint of HEAD plus every uncommitted change, new files' content included — equal fingerprints at two
        stops mean the session in between changed nothing."""
        h = hashlib.sha1((self.head() + "\n" + self("diff", "HEAD").out).encode("utf-8", "replace"))
        new = [p for p in self.out("ls-files", "--others", "--exclude-standard").splitlines() if not is_noise(p)]
        for p in sorted(new)[:500]:
            h.update(p.encode("utf-8", "replace"))
            try:
                with open(os.path.join(self.root, p), "rb") as fh:
                    h.update(hashlib.sha1(fh.read()).digest())
            except OSError:
                pass
        return h.hexdigest()[:16]

    def common_dir(self) -> str:
        d = self.out("rev-parse", "--git-common-dir")
        return os.path.abspath(os.path.join(self.root, d)) if d else os.path.join(self.root, ".git")

    # ---------- moves ----------
    def fetch(self) -> bool:
        return self.has_remote() and self("fetch", "-q", "--prune", "origin", timeout=120).ok

    def refresh(self, timeout: float) -> bool:
        """`fetch --prune` for a read-only view: it never asks for a password, says nothing, and does not outlive `timeout` —
        the whole process group goes (ssh and the https helper included). False on any failure: the view shows what it has."""
        if not self.has_remote():
            return False
        try:
            p = subprocess.Popen(["git", "-C", self.root, "fetch", "-q", "--prune", "origin"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                 env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        except OSError:
            return False
        try:
            return p.wait(timeout=timeout) == 0
        except subprocess.TimeoutExpired:
            return False
        finally:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                p.wait()

    def base_ref(self, default: str) -> str:
        """What a change is measured against: origin/<default> when it exists, else the local default branch."""
        return "origin/" + default if self.remote_branch_exists(default) else default

    def to_default(self, default: str) -> bool:
        """Check out the default branch and fast-forward it; in a linked worktree where it is checked out elsewhere, detach
        at its tip instead."""
        if self.ok("checkout", "-q", default):
            if self.has_remote():
                return self("pull", "-q", "--ff-only", timeout=120).ok
            return True
        self.fetch()
        return self.ok("checkout", "-q", "--detach", self.base_ref(default))

    def worktree(self, ref: str) -> Optional[str]:
        """A throwaway detached worktree at ref (for the gate). Caller removes it with drop_worktree."""
        base = os.path.join(self.root, ".sdlc", "worktrees")
        os.makedirs(base, exist_ok=True)
        path = tempfile.mkdtemp(prefix="gate-", dir=base)
        os.rmdir(path)
        r = self("worktree", "add", "-q", "--detach", path, ref, timeout=300)
        return path if r.ok else None

    def worktree_of(self, branch: str) -> Optional[str]:
        """The worktree that has `branch` checked out, if any."""
        cur = None
        for line in self.out("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                cur = line[9:]
            elif line == "branch refs/heads/" + branch:
                return cur
        return None

    def drop_worktree(self, path: str) -> None:
        self("worktree", "remove", "--force", path)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        self("worktree", "prune")


# ---------------------------------------------------------------------------------------------------------------------
class Forge:
    """How a branch becomes a merged change."""
    kind = "base"

    def __init__(self, git: Git, default: str):
        self.git, self.default = git, default

    def state(self, slug: str) -> str:            # merged | open | closed | none | unknown
        raise NotImplementedError

    def number(self, slug: str) -> str:
        return ""

    def publish(self, slug: str, title: str, body: str, draft: bool = False) -> Tuple[bool, str]:
        raise NotImplementedError

    def merge(self, slug: str, title: str, note: str = "") -> Tuple[bool, str]:
        """Squash-merge the item. `note` is one line for whoever reads the history later; the local forge writes it into
        the commit message, on GitHub it already stands at the top of the PR body."""
        raise NotImplementedError

    def flag_human(self, slug: str, reasons: List[str]) -> None:
        pass

    def close(self, slug: str, comment: str) -> bool:
        """Close the item's open PR with a comment that says why (a dropped item). False when there is none to close — a
        local branch has no PR."""
        return False

    def approved(self, slug: str) -> bool:
        return False

    def url(self, slug: str) -> str:
        return "sdlc/" + slug


class LocalForge(Forge):
    """No remote: the branch is the change; merging is a squash merge onto the default branch in this worktree."""
    kind = "local"

    def state(self, slug: str) -> str:
        b = "sdlc/" + slug
        if not self.git.branch_exists(b):
            return "none"
        tip = self.git.head(b)
        if tip and self.git.is_ancestor(tip, self.default):
            return "merged"
        return "open"

    def publish(self, slug: str, title: str, body: str, draft: bool = False) -> Tuple[bool, str]:
        return self.git.branch_exists("sdlc/" + slug), "sdlc/" + slug

    def merge(self, slug: str, title: str, note: str = "") -> Tuple[bool, str]:
        """Squash merge onto the default branch: here, or — in a linked worktree — where the default branch is checked out."""
        g, b = self.git, "sdlc/" + slug
        where = g.root
        if not g.ok("checkout", "-q", self.default):
            wt = g.worktree_of(self.default)
            if not wt:
                return False, "cannot check out " + self.default
            dirty = g.dirty(cwd=wt)
            if dirty:
                return False, "%s is checked out in %s with uncommitted changes (%s)" % (self.default, wt, dirty_summary(dirty))
            g("checkout", "-q", "--detach")
            where = wt
        r = g("merge", "--squash", b, cwd=where)
        if not r.ok:
            g("merge", "--abort", cwd=where)
            g("reset", "-q", "--hard", "HEAD", cwd=where)
            return False, "squash merge failed: " + r.text()[-300:]
        if g("diff", "--cached", "--quiet", cwd=where).ok:
            g("branch", "-D", b)
            return True, g.out("rev-parse", "HEAD", cwd=where)     # nothing left to merge: already in
        msg = "%s (%s)" % (title, b) + ("\n\n" + note if note else "")
        r = g("commit", "-q", "--no-verify", "-m", msg, cwd=where)
        if not r.ok:
            g("reset", "-q", "--hard", "HEAD", cwd=where)
            return False, "commit failed: " + r.text()[-300:]
        g("branch", "-D", b)
        return True, g.out("rev-parse", "HEAD", cwd=where)


class GitHubForge(Forge):
    kind = "github"

    def _gh(self, *args: str, timeout: float = 120) -> CmdResult:
        return run(["gh"] + list(args), cwd=self.git.root, timeout=timeout)

    def _pr(self, slug: str, state: str) -> Optional[dict]:
        r = self._gh("pr", "list", "--head", "sdlc/" + slug, "--state", state, "--json",
                     "number,url,isDraft,reviewDecision,state,labels,reviews,author", "--limit", "1")
        if not r.ok:
            raise RuntimeError(r.text())
        try:
            items = json.loads(r.out or "[]")
        except ValueError:
            raise RuntimeError("unreadable gh output")
        return items[0] if items else None

    def state(self, slug: str) -> str:
        try:
            if self._pr(slug, "merged"):
                return "merged"
            if self._pr(slug, "open"):
                return "open"
            if self._pr(slug, "closed"):
                return "closed"
            return "none"
        except RuntimeError:
            return "unknown"

    def number(self, slug: str) -> str:
        try:
            pr = self._pr(slug, "open") or self._pr(slug, "merged")
        except RuntimeError:
            return ""
        return str(pr["number"]) if pr else ""

    def url(self, slug: str) -> str:
        try:
            pr = self._pr(slug, "all")
        except RuntimeError:
            pr = None
        return pr["url"] if pr else "sdlc/" + slug

    def publish(self, slug: str, title: str, body: str, draft: bool = False) -> Tuple[bool, str]:
        b = "sdlc/" + slug
        r = self.git("push", "-q", "-u", "origin", b, "--force-with-lease", timeout=300)
        if not r.ok:
            return False, "push failed: " + r.text()[-300:]
        try:
            pr = self._pr(slug, "open")
        except RuntimeError as e:
            return False, str(e)
        if pr:
            self._gh("pr", "edit", str(pr["number"]), "--body", body)
            if not draft and pr.get("isDraft"):
                self._gh("pr", "ready", str(pr["number"]))
            return True, pr["url"]
        args = ["pr", "create", "--head", b, "--base", self.default, "--title", title, "--body", body]
        if draft:
            args.append("--draft")
        r = self._gh(*args)
        if not r.ok:
            return False, "gh pr create failed: " + r.text()[-300:]
        return True, (r.out.strip().splitlines() or [""])[-1]

    def merge(self, slug: str, title: str, note: str = "") -> Tuple[bool, str]:
        n = self.number(slug)
        if not n:
            return False, "no open PR for sdlc/" + slug
        r = self._gh("pr", "merge", n, "--squash", "--delete-branch", timeout=300)
        # verify instead of trusting the exit code: --auto or a queued merge is not a merge
        st = self.state(slug)
        if st == "merged":
            return True, n
        return False, ("gh pr merge: " + r.text()[-300:]) if not r.ok else "PR #%s is not merged (state: %s)" % (n, st)

    def flag_human(self, slug: str, reasons: List[str]) -> None:
        n = self.number(slug)
        if not n:
            return
        body = "**sdlc gate: waiting for a human**\n\n" + "\n".join("- " + r for r in reasons)
        self._gh("pr", "comment", n, "--body", body)
        self._gh("label", "create", "sdlc:needs-human", "--color", "D93F0B", "--force")
        self._gh("pr", "edit", n, "--add-label", "sdlc:needs-human")

    def close(self, slug: str, comment: str) -> bool:
        try:
            pr = self._pr(slug, "open")
        except RuntimeError:
            return False
        return bool(pr) and self._gh("pr", "close", str(pr["number"]), "--comment", comment, timeout=60).ok

    def approved(self, slug: str) -> bool:
        """A person approved the PR: reviewDecision (with required reviews), an APPROVED review by someone other than
        the author (without them), or the `sdlc:approved` label (when the approver is the one running the loop)."""
        try:
            pr = self._pr(slug, "open")
        except RuntimeError:
            return False
        if not pr:
            return False
        if pr.get("reviewDecision") == "APPROVED":
            return True
        if any((l or {}).get("name") == "sdlc:approved" for l in pr.get("labels") or []):
            return True
        author = ((pr.get("author") or {}).get("login") or "").lower()
        latest = {}
        for rv in pr.get("reviews") or []:
            who = ((rv.get("author") or {}).get("login") or "").lower()
            if who and who != author:
                latest[who] = rv.get("state")
        return any(v == "APPROVED" for v in latest.values()) and "CHANGES_REQUESTED" not in latest.values()


def make_forge(git: Git, default: str, prefer: str = "auto") -> Forge:
    """auto: GitHub when there is an origin remote, else local. `local` with a remote merges locally and does not push."""
    if prefer == "local" or (prefer == "auto" and not git.has_remote()):
        return LocalForge(git, default)
    return GitHubForge(git, default)


def remote_host(git: Git) -> str:
    url = git.out("remote", "get-url", "origin")
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)[:/]", url or "")
    return m.group(1) if m else ""
