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
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from .util import CmdResult, now, now_iso, parse_iso, run, tail

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


# A git command that fails because another git process holds a lock (the index, a ref) is run again after each of these
# pauses, in seconds: the other process, a `git fetch` or a second loop, is usually done within one.
LOCK_RETRY_DELAYS = (1, 2, 4)
# What git prints, on stdout or stderr, when it lost a race that passes. Git's own words and nothing looser: the commit the engine
# makes runs the repository's hooks, and what a hook prints is ordinary text (`got 1 but expected 2`,
# `ln: failed to create symbolic link 'yarn.lock': File exists`).
#   a lock file that exists:       Unable to create '<path>.lock': File exists       (`cannot lock ref` ends with it too)
#   a ref another process moved:   cannot lock ref '<ref>': is at <sha> but expected <sha>
# `cannot lock ref` alone is no match: it also opens failures that never pass, `'refs/heads/a' exists; cannot create
# 'refs/heads/a/b'` and `reference already exists`.
LOCK_RE = re.compile(r"Unable to create '.*\.lock': File exists"
                     r"|cannot lock ref '[^']+': is at [0-9a-f]+ but expected [0-9a-f]+")
CAUSE_PREFIXES = ("fatal:", "error:")       # how git starts the line that says what went wrong
# What a push that lost its lease says: the client's own check (`stale info`), or origin refusing to move a ref that is no
# longer where the push saw it (`failed to update ref`, `reference already exists`, `is at <sha> but expected <sha>`) —
# the second is how the loser of two pushes in the same moment hears of it.
LOST_LEASE_RE = re.compile(r"stale info|failed to update ref|reference already exists|is at [0-9a-f]+ but expected [0-9a-f]+")
TERM_GRACE = 2.0        # seconds a fetch that ran out of time has to remove its lock files after SIGTERM, before SIGKILL
STALE_PUSH = "remote branch changed"        # how the reason of a push that lost its lease begins (Forge.publish with `expect`)
CLAIM_REF = "refs/sdlc/claims/"             # a claim on an item for loops in separate clones: this ref, on origin
CLAIM_SEEN_REF = "refs/sdlc/claims-seen/"   # where a claim read from origin is looked at, then deleted
CLAIM_WORD = "sdlc-claim"                   # the one line a claim commit carries: sdlc-claim <token> <host> <root> <pid> <time>
CLAIM_TIMEOUT = 20      # seconds for each of the network calls a claim makes
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}


class Claim(NamedTuple):
    """What a claim commit says: who (token; `host`, the git user name; `root`, the checkout's folder name; pid) and when (ISO
    time, and as epoch seconds). Nothing more identifying: on a public repository anyone can read the ref."""
    token: str
    host: str
    root: str
    pid: str
    time: str
    epoch: float

    def describe(self) -> str:
        return "%s in %s since %s" % (self.host, self.root, self.time)


def lost_lease(push_output: str) -> bool:
    """A push that did not go through because the ref it was leased on is not where it was expected."""
    return bool(LOST_LEASE_RE.search(push_output))


def one_word(text: object) -> str:
    """`text` as a word of a claim line: no blanks (they become `_`), never empty."""
    return "_".join(str(text).split()) or "?"


def pause(seconds: float) -> None:
    """The wait before a command is tried again — a function of its own, so that a test does not have to sleep."""
    time.sleep(seconds)


def cause_of(r: CmdResult) -> str:
    """The line that says why a failed command failed: the first `fatal:` or `error:` line, on stderr before stdout. Git's
    advice comes after it (the hint about a stale lock file, say), so the last line is the answer only when git printed no
    such line."""
    for stream in (r.err, r.out):
        for line in stream.splitlines():
            line = line.strip()
            if line.startswith(CAUSE_PREFIXES):
                return line
    return tail(r.err, 1).strip() or tail(r.out, 1).strip()


class Git:
    def __init__(self, root: str):
        self.root = root
        self.last_error = ""        # why the last command failed, in git's words (see cause_of); empty after one that worked
        self.last_warning = ""      # what went wrong in a step that carried on (`to_default`'s fetch); set by that step
        self.prefer_local = False   # the local default branch is what changes are measured against (a local forge)

    def __call__(self, *args: str, timeout: float = 300, cwd: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None) -> CmdResult:
        """Run git. A command that failed on a lock is run again, after each of LOCK_RETRY_DELAYS; the reason it gave the last
        time is what `last_error` keeps. `env` is added to the environment."""
        cmd = ["git", "-C", cwd or self.root] + list(args)
        extra = {"env": dict(os.environ, **env)} if env else {}
        r = run(cmd, timeout=timeout, **extra)
        for delay in LOCK_RETRY_DELAYS:
            if r.ok or not LOCK_RE.search(r.text()):
                break
            pause(delay)
            r = run(cmd, timeout=timeout, **extra)
        self.last_error = "" if r.ok else cause_of(r)
        return r

    def with_reason(self, message: str) -> str:
        """`message`, followed by the reason git gave for the command that just failed — as it stands, when git gave none."""
        return "%s: %s" % (message, self.last_error) if self.last_error else message

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

    def untracked(self, cwd: Optional[str] = None) -> List[str]:
        """Every file that is neither tracked nor ignored, as paths. NUL-separated output: a path git would quote is read as
        it is. Empty when git fails."""
        return [p for p in self("ls-files", "-z", "--others", "--exclude-standard", cwd=cwd).out.split("\0") if p]

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

    def remote_sha(self, branch: str) -> str:
        """The commit origin's `branch` was at when this repository last fetched; "" when there is no such branch there."""
        return self.head("refs/remotes/origin/" + branch)

    def relation(self, a: str, b: str) -> str:
        """How two refs stand: `same`, `behind` (a is an ancestor of b), `ahead` (b is an ancestor of a), `diverged`, or `none`
        when either does not resolve to a commit."""
        sa, sb = self.head(a), self.head(b)
        if not sa or not sb:
            return "none"
        if sa == sb:
            return "same"
        if self.is_ancestor(sa, sb):
            return "behind"
        return "ahead" if self.is_ancestor(sb, sa) else "diverged"

    def changed(self, base: str, head: str) -> List[Tuple[str, str]]:
        """[(status, path)] between the merge base of base..head and head. A renamed file is its new path as A and its old path
        as D (it is gone from there); a copy is its new path as A. The paths are read from NUL-separated output, so one that
        git would quote (non-ASCII, a space, a newline) comes back as it is. RuntimeError, with git's reason, when git fails:
        an empty answer must never stand for "nothing changed"."""
        r = self("diff", "--name-status", "-z", "-M", base + "..." + head)
        if not r.ok:
            raise RuntimeError(self.last_error or r.text() or "git diff failed")
        fields = r.out.split("\0")
        if fields and fields[-1] == "":
            fields.pop()
        out: List[Tuple[str, str]] = []
        i = 0
        while i < len(fields):
            kind = fields[i][:1]
            n = 3 if kind in ("R", "C") else 2          # a rename or copy carries two paths
            if not kind or i + n > len(fields):
                raise RuntimeError("unreadable git diff output")
            if n == 3:
                out.append(("A", fields[i + 2]))
                if kind == "R":
                    out.append(("D", fields[i + 1]))
            else:
                out.append((kind, fields[i + 1]))
            i += n
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
        new = [p for p in self.untracked() if not is_noise(p)]
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
        the whole process group goes (ssh and the https helper included), told to stop first and killed when it does not
        within TERM_GRACE: git removes its lock files on SIGTERM, a SIGKILL leaves them behind. False on any failure: the view
        shows what it has. A lock held by someone else is not waited for: the view is not worth a pause."""
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
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    p.terminate()
                try:
                    p.wait(timeout=TERM_GRACE)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except OSError:
                        p.kill()
                    p.wait()

    def base_ref(self, default: str) -> str:
        """What a change is measured against: origin/<default> when it exists, else the local default branch — and always the
        local one when `prefer_local` is set (a local forge merges there, whatever origin has)."""
        if self.prefer_local:
            return default
        return "origin/" + default if self.remote_branch_exists(default) else default

    def to_default(self, default: str) -> bool:
        """Check out the default branch and bring it up to origin's: fetch — a fetch that fails (the network is down for a
        minute) only leaves a warning in `last_warning` — and fast-forward to origin/<default> when that exists. False, with
        `last_error`, only when the checkout fails or the local branch has diverged from origin's. In a linked worktree where
        the default branch is checked out elsewhere, detach at its tip instead."""
        self.last_warning = ""
        if self.ok("checkout", "-q", default):
            if self.has_remote():
                self._fetch_or_warn()
                if self.remote_branch_exists(default) and not self(
                        "merge", "--ff-only", "-q", "refs/remotes/origin/" + default, timeout=120).ok:
                    return False
            self.last_error = ""
            return True
        if self.has_remote():
            self._fetch_or_warn()
        return self.ok("checkout", "-q", "--detach", self.base_ref(default))

    def _fetch_or_warn(self) -> None:
        if not self.fetch():
            self.last_warning = self.with_reason("fetch from origin failed, went on with what is here")

    def worktree(self, ref: str) -> Optional[str]:
        """A throwaway detached worktree at ref (for the gate). Caller removes it with drop_worktree."""
        base = os.path.join(self.root, ".sdlc", "worktrees")
        os.makedirs(base, exist_ok=True)
        path = tempfile.mkdtemp(prefix="gate-", dir=base)
        os.rmdir(path)
        r = self("worktree", "add", "-q", "--detach", path, ref, timeout=300)
        return path if r.ok else None

    def worktree_of(self, branch: str) -> Optional[str]:
        """The worktree that has `branch` checked out, if any. Read from NUL-terminated lines (git 2.36 and later), so that a
        path with a newline in it stays one line; older git has no `-z` and is read line by line."""
        r = self("worktree", "list", "--porcelain", "-z")
        lines = r.out.split("\0") if r.ok else self.out("worktree", "list", "--porcelain").splitlines()
        cur = None
        for line in lines:
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

    # ---------- claims on origin, for loops in separate clones ----------
    # A claim is the ref `refs/sdlc/claims/<slug>` on origin, pointing at a commit of the empty tree whose message is the
    # one line `sdlc-claim <token> <host> <root> <pid> <time>`. Whoever creates the ref first holds the item: the push that
    # creates it is leased on "no such ref", the ones that replace or delete it on the commit that was read. Every call here
    # is short (CLAIM_TIMEOUT), never asks for a password and runs no hook; the answers say when origin cannot be reached,
    # and a caller then goes on without a claim on origin (the claim files of `state.Claims` still keep worktrees apart).
    def _claim_read(self, slug: str) -> Tuple[str, str, Optional[Claim]]:
        """origin's claim on slug as ("absent", "", None), ("present", sha, claim) — the commit's own time stands in for a
        message that cannot be read — or ("error", reason, None)."""
        if not self.has_remote():
            return "error", "no remote named origin", None
        ref = CLAIM_REF + slug
        r = self("ls-remote", "origin", ref, timeout=CLAIM_TIMEOUT, env=NO_PROMPT)
        if not r.ok:
            return "error", self.last_error or r.text() or "ls-remote failed", None
        if not any(len(line.split()) == 2 and line.split()[1] == ref for line in r.out.splitlines()):
            return "absent", "", None
        seen = "%s%s.%d" % (CLAIM_SEEN_REF, slug, os.getpid())      # a name of our own: loops in worktrees share the refs
        try:
            if not self("fetch", "-q", "--no-tags", "origin", "+%s:%s" % (ref, seen), timeout=CLAIM_TIMEOUT, env=NO_PROMPT).ok:
                return "error", self.last_error or "fetch of the claim failed", None
            sha = self.head(seen)
            text = self.out("log", "-1", "--format=%ct%n%B", seen) if sha else ""
        finally:
            self("update-ref", "-d", seen)
        lines = text.splitlines()
        if not sha or len(lines) < 2:
            return "error", "the claim could not be read", None
        parts = lines[1].split(" ")
        when = parse_iso(parts[-1])
        if len(parts) >= 6 and parts[0] == CLAIM_WORD and when:
            return "present", sha, Claim(parts[1], parts[2], " ".join(parts[3:-2]), parts[-2], parts[-1], when.timestamp())
        made = float(lines[0]) if lines[0].isdigit() else 0.0
        return "present", sha, Claim("", "?", "?", "0", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(made)), made)

    def _claim_push(self, slug: str, token: str, expect: str, delete: bool = False) -> Tuple[bool, str]:
        """Put a claim of `token` on slug — or take it away — only while origin's is still `expect` ("" for none). (True, "")
        when it went through; (False, STALE_PUSH) when origin's changed under the lease; (False, reason) for anything else."""
        ref = CLAIM_REF + slug
        sha = ""
        if not delete:
            line = "%s %s %s %s %d %s" % (CLAIM_WORD, one_word(token), one_word(self.out("config", "user.name") or "?"),
                                           one_word(os.path.basename(self.root.rstrip(os.sep))), os.getpid(), now_iso())
            tree = self.out("hash-object", "-t", "tree", "-w", os.devnull)
            if tree:
                sha = self.out("-c", "user.name=sdlc", "-c", "user.email=sdlc@localhost", "-c", "commit.gpgsign=false",
                               "commit-tree", tree, "-m", line)
            if not sha:
                return False, self.last_error or "could not make the claim commit"
        r = self("push", "-q", "--no-verify", "--force-with-lease=%s:%s" % (ref, expect), "origin", "%s:%s" % (sha, ref),
                 timeout=CLAIM_TIMEOUT, env=NO_PROMPT)
        if r.ok:
            return True, ""
        return False, STALE_PUSH if lost_lease(r.text()) else (self.last_error or r.text()[-300:] or "push failed")

    def claim_holder(self, slug: str) -> str:
        """Who holds slug on origin, as `<git user> in <folder> since <time>`; "" when nobody does or origin cannot be asked."""
        state, _, c = self._claim_read(slug)
        return c.describe() if state == "present" and c else ""

    def claim(self, slug: str, token: str, stale_seconds: float) -> str:
        """Hold slug on origin. `taken`: nobody did, or the holder's claim is older than `stale_seconds` (a loop that is gone)
        and is replaced; `ours`: it was ours already (refreshed); `other:<host> <root> since <time>`: somebody else's and not
        stale; `error:<reason>`: origin could not be asked or would not take the push (the caller goes on without a claim
        there). A loop that wins the race for the ref in the meantime is read again, three times at most."""
        token = one_word(token)
        for _ in range(3):
            state, sha, c = self._claim_read(slug)
            if state == "error":
                return "error:" + sha
            if state == "present" and c and c.token != token and now().timestamp() - c.epoch <= stale_seconds:
                return "other:" + c.describe()
            ok, why = self._claim_push(slug, token, sha)        # absent (sha ""), ours (refresh) or stale: one push for all
            if ok or (why != STALE_PUSH and c and c.token == token):
                return "ours" if c and c.token == token else "taken"
            if why != STALE_PUSH:
                return "error:" + why
        return "error:the claim on %s kept changing" % slug

    def refresh_claim(self, slug: str, token: str) -> bool:
        """Renew our claim on slug (a new commit with the time now), only while it is still the one that was read."""
        token = one_word(token)
        state, sha, c = self._claim_read(slug)
        return state == "present" and bool(c) and c.token == token and self._claim_push(slug, token, sha)[0]

    def release_claim(self, slug: str, token: str) -> bool:
        """Give slug up: delete our claim on origin, only while it is still the one that was read. True when no claim of ours is
        left there (it was gone already); False when somebody else holds it or origin could not be asked."""
        token = one_word(token)
        state, sha, c = self._claim_read(slug)
        if state == "absent":
            return True
        if state != "present" or not c or c.token != token:
            return False
        return self._claim_push(slug, token, sha, delete=True)[0]


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

    def publish(self, slug: str, title: str, body: str, draft: bool = False,
                expect: Optional[str] = None) -> Tuple[bool, str]:
        """Push the item's branch and open (or update) its PR. `expect`: what origin's branch must still be for the push to
        go through — None leaves that to git's own lease (the last fetched tip), "" means the branch must not exist there yet,
        a commit is the exact tip. A push that loses the lease is (False, reason) with a reason that starts with STALE_PUSH."""
        raise NotImplementedError

    def merge(self, slug: str, title: str, note: str = "", head: str = "") -> Tuple[bool, str]:
        """Squash-merge the item. `note` is one line for whoever reads the history later; the local forge writes it into
        the commit message, on GitHub it already stands at the top of the PR body. `head`: the commit that was checked — on
        GitHub the merge is refused when the PR's head is any other."""
        raise NotImplementedError

    def flag_human(self, slug: str, reasons: List[str]) -> None:
        pass

    def close(self, slug: str, comment: str) -> bool:
        """Close the item's open PR with a comment that says why (a dropped item). False when there is none to close — a
        local branch has no PR."""
        return False

    def approved(self, slug: str) -> bool:
        return False

    def checks(self, slug: str, head: str = "") -> Tuple[str, List[Dict[str, str]]]:
        """The item's PR checks as (state, entries) — state pass | fail | pending | none | unknown, an entry {"name", "bucket",
        "link", "workflow"}. With `head` (the commit just pushed), what GitHub shows for another commit is `unknown`. Only a
        forge with CI to ask has any checks: here there is nothing to wait for."""
        return "none", []

    def check_logs(self, failed: List[Dict[str, str]], lines: int = 60, wait: Optional[Callable[[float], bool]] = None) -> str:
        """What the failed checks printed, for the session that has to fix them: the end of each failed job's log, cut before
        the job's cleanup, without the per-line prefix. A log that is not there yet (its run is still in progress) is asked
        for again after a pause; `wait(seconds)` makes the pause and answers False when the caller is stopping, which ends the
        waiting. None: a plain sleep."""
        return ""

    def merge_state(self, slug: str) -> str:
        """The forge's answer to "can the PR be merged now" (GitHub: CLEAN, BEHIND, BLOCKED, DIRTY, …); empty when unknown."""
        return ""

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

    def publish(self, slug: str, title: str, body: str, draft: bool = False,
                expect: Optional[str] = None) -> Tuple[bool, str]:
        return self.git.branch_exists("sdlc/" + slug), "sdlc/" + slug

    def merge(self, slug: str, title: str, note: str = "", head: str = "") -> Tuple[bool, str]:
        """Squash merge onto the default branch: here, or — in a linked worktree — where the default branch is checked out."""
        g, b = self.git, "sdlc/" + slug
        tip = g.head(b)
        if head and tip and not tip.startswith(head):
            return False, "%s is at %s, not at the %s that was checked" % (b, tip[:10], head[:10])
        where = g.root
        if not g.ok("checkout", "-q", self.default):
            why = g.with_reason("cannot check out " + self.default)
            wt = g.worktree_of(self.default)
            if not wt:
                return False, why
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


RUN_LINK = re.compile(r"/actions/runs/(\d+)")
# a line of `gh run view --log-failed`: job, step, timestamp (the first line of a log has a byte-order mark before it), text
RUN_LOG_LINE = re.compile(r"^([^\t]*)\t([^\t]*)\t\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) ?(.*)$")
POST_JOB_STEPS = "Post job cleanup."      # where a job's log turns to its post steps
CHECK_LOG_RUNS = 3          # runs whose log is read: every `gh run view` is a slow call
CHECK_LOGS_CAP = 6000       # characters of CI output a fix brief carries
CHECK_LOG_WAIT = 10         # seconds before a run that is still in progress is asked for its log again …
CHECK_LOG_RETRIES = 3       # … and how many times it is: 30 seconds in all


def _check_entry(raw: dict) -> Dict[str, str]:
    """One `statusCheckRollup` entry as {"name", "bucket", "link", "workflow"}. A CheckRun (Actions and most apps) is pending
    until it completes and then passes only on SUCCESS, NEUTRAL or SKIPPED — anything else, an empty conclusion included, is
    a failure. A StatusContext (the older commit status) carries its verdict in `state`; EXPECTED and PENDING are still open."""
    def text(key: str) -> str:
        return str(raw.get(key) or "")
    if raw.get("__typename") == "StatusContext" or ("context" in raw and "name" not in raw):
        state = text("state").upper()
        bucket = "pass" if state == "SUCCESS" else "fail" if state in ("FAILURE", "ERROR") else "pending"
        return {"name": text("context"), "bucket": bucket, "link": text("targetUrl"), "workflow": ""}
    if text("status").upper() != "COMPLETED":
        bucket = "pending"
    else:
        bucket = "pass" if text("conclusion").upper() in ("SUCCESS", "NEUTRAL", "SKIPPED") else "fail"
    return {"name": text("name"), "bucket": bucket, "link": text("detailsUrl"), "workflow": text("workflowName")}


def end_of(text: str, n: int) -> str:
    """The last n characters of text, from the start of a line when there is one — a log's end says why it failed."""
    if n <= 0:
        return ""
    if len(text) <= n:
        return text
    cut = text[len(text) - n:]
    if text[len(text) - n - 1] != "\n" and "\n" in cut:
        cut = cut.split("\n", 1)[1]
    return cut


def trim_run_log(text: str) -> str:
    """What `gh run view --log-failed` printed, cut down to what says why the job failed. gh prints the whole log of each
    failed job, not only the failed step — it cannot match lines to steps — as `job<TAB>step<TAB>timestamp text`. The prefix
    goes (a line without one is kept as it is, in the job before it), and so does a job's log from its first
    `Post job cleanup.` on: the post steps and "Complete job" never say why it failed, and the steps that run on failure
    come before that line. A log of more than one job has a `--- <job> ---` line before each."""
    blocks: List[Tuple[str, List[str]]] = []        # (job, its lines): consecutive lines of one job are one block
    job = ""
    for line in text.splitlines():
        m = RUN_LOG_LINE.match(line)
        if m:
            job, line = m.group(1), m.group(4)
        if not blocks or blocks[-1][0] != job:
            blocks.append((job, []))
        blocks[-1][1].append(line)
    out: List[str] = []
    for job, block in blocks:
        if len(blocks) > 1:
            out.append("--- %s ---" % job)
        out.extend(block[:block.index(POST_JOB_STEPS)] if POST_JOB_STEPS in block else block)
    return "\n".join(out)


def _plain_sleep(seconds: float) -> bool:
    """The pause of a caller that nothing can stop: sleep, then go on."""
    time.sleep(seconds)
    return True


PR_FIELDS = "number,url,isDraft,reviewDecision,state,labels,reviews,author,isCrossRepository,headRefOid"
PR_LIST_LIMIT = 20          # rows of `gh pr list` to choose from: a reused slug has several PRs, and forks may add more
REVIEW_DECISIONS = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")     # the reviews that count; a comment decides nothing
PR_STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def pr_state(pr: object) -> str:
    """The state gh reports for a PR, upper case; "" when it says none."""
    return str(pr.get("state") or "").upper() if isinstance(pr, dict) else ""


class GitHubForge(Forge):
    kind = "github"

    def _gh(self, *args: str, timeout: float = 120) -> CmdResult:
        return run(["gh"] + list(args), cwd=self.git.root, timeout=timeout)

    def _prs(self, slug: str, state: str) -> List[dict]:
        """The PRs whose head branch is sdlc/<slug>, best first: an open one before any other, then the newest. A PR from a
        fork is none of ours — `gh pr list --head` goes by the branch name alone, and anyone's fork can have an `sdlc/<slug>`.
        RuntimeError when gh fails or prints anything but a list of objects that have a number."""
        r = self._gh("pr", "list", "--head", "sdlc/" + slug, "--state", state, "--json", PR_FIELDS, "--limit", str(PR_LIST_LIMIT))
        if not r.ok:
            raise RuntimeError(r.text())
        try:
            items = json.loads(r.out or "[]")
        except ValueError:
            raise RuntimeError("unreadable gh output")
        if not isinstance(items, list) or not all(isinstance(p, dict) for p in items):
            raise RuntimeError("unreadable gh output")
        mine = [p for p in items if p.get("isCrossRepository") is not True]
        if not all(isinstance(p.get("number"), int) for p in mine):
            raise RuntimeError("unreadable gh output")
        return sorted(mine, key=lambda p: (pr_state(p) != "OPEN", -p["number"]))

    def _pr(self, slug: str, state: str) -> Optional[dict]:
        prs = self._prs(slug, state)
        return prs[0] if prs else None

    def _open_or_merged(self, slug: str) -> Optional[dict]:
        """The item's open PR, else its newest merged one — what a merge has to act on or find done. RuntimeError as `_prs`."""
        prs = self._prs(slug, "all")
        return next((p for p in prs if pr_state(p) == "OPEN"), None) or next((p for p in prs if pr_state(p) == "MERGED"), None)

    def state(self, slug: str) -> str:
        try:
            pr = self._pr(slug, "all")
        except RuntimeError:
            return "unknown"
        return PR_STATES.get(pr_state(pr), "unknown") if pr else "none"

    def number(self, slug: str) -> str:
        try:
            pr = self._open_or_merged(slug)
        except RuntimeError:
            return ""
        return str(pr["number"]) if pr else ""

    def url(self, slug: str) -> str:
        try:
            pr = self._pr(slug, "all")
        except RuntimeError:
            pr = None
        return str((pr or {}).get("url") or "") or "sdlc/" + slug

    def publish(self, slug: str, title: str, body: str, draft: bool = False,
                expect: Optional[str] = None) -> Tuple[bool, str]:
        b = "sdlc/" + slug
        lease = "--force-with-lease" if expect is None else "--force-with-lease=refs/heads/%s:%s" % (b, expect)
        # --no-verify: a pre-push hook is the user's, not part of what the engine does
        r = self.git("push", "-q", "--no-verify", "-u", "origin", b, lease, timeout=300)
        if not r.ok:
            text = r.text()
            if lost_lease(text):                # the lease no longer holds: somebody else pushed the branch
                rejected = next((" ".join(l.split()) for l in text.splitlines() if "rejected]" in l), "")
                return False, "%s: %s" % (STALE_PUSH, rejected or self.git.last_error)
            return False, "push failed: " + text[-300:]
        try:
            pr = self._pr(slug, "open")
        except RuntimeError as e:
            return False, str(e)
        if pr:
            self._gh("pr", "edit", str(pr["number"]), "--body", body)
            if not draft and pr.get("isDraft"):
                self._gh("pr", "ready", str(pr["number"]))
            return True, str(pr.get("url") or "")
        args = ["pr", "create", "--head", b, "--base", self.default, "--title", title, "--body", body]
        if draft:
            args.append("--draft")
        r = self._gh(*args)
        if not r.ok:
            return False, "gh pr create failed: " + r.text()[-300:]
        return True, (r.out.strip().splitlines() or [""])[-1]

    def _state_of_number(self, slug: str, n: str) -> str:
        """merged | open | closed | unknown for the PR with this number among the PRs of sdlc/<slug> — that one, not whichever
        PR the branch name leads to now."""
        try:
            mine = [p for p in self._prs(slug, "all") if str(p["number"]) == n]
        except RuntimeError:
            return "unknown"
        return PR_STATES.get(pr_state(mine[0]), "unknown") if mine else "unknown"

    def merge(self, slug: str, title: str, note: str = "", head: str = "") -> Tuple[bool, str]:
        try:
            pr = self._open_or_merged(slug)
        except RuntimeError as e:
            return False, "cannot read the PR of sdlc/%s: %s" % (slug, e)
        if not pr:
            return False, "no open PR for sdlc/" + slug
        n = str(pr["number"])
        if pr_state(pr) == "MERGED":
            return True, n                      # somebody did it already
        args = ["pr", "merge", n, "--squash", "--delete-branch"]
        if head:
            args += ["--match-head-commit", head]       # refused when a commit landed on the branch after the one checked
        r = self._gh(*args, timeout=300)
        # verify instead of trusting the exit code: --auto or a queued merge is not a merge
        st = self._state_of_number(slug, n)
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
        the author (without them), or the `sdlc:approved` label (when the approver is the one running the loop). Of each
        reviewer's reviews the latest APPROVED, CHANGES_REQUESTED or DISMISSED one decides — a comment after an approval is
        no withdrawal of it — and one request for changes that stands keeps the PR from being approved."""
        try:
            pr = self._pr(slug, "open")
        except RuntimeError:
            return False
        if not pr:
            return False
        if pr.get("reviewDecision") == "APPROVED":
            return True
        labels = pr.get("labels")
        if any(isinstance(l, dict) and l.get("name") == "sdlc:approved" for l in (labels if isinstance(labels, list) else [])):
            return True

        def login(entry: object) -> str:
            who = entry.get("author") if isinstance(entry, dict) else None
            return str((who.get("login") if isinstance(who, dict) else "") or "").lower()
        author = login(pr)
        reviews = pr.get("reviews")
        decided: Dict[str, str] = {}
        # oldest first, so that a later review replaces an earlier one (gh lists them in that order already)
        for rv in sorted([r for r in (reviews if isinstance(reviews, list) else []) if isinstance(r, dict)],
                         key=lambda r: str(r.get("submittedAt") or "")):
            who, verdict = login(rv), str(rv.get("state") or "").upper()
            if who and who != author and verdict in REVIEW_DECISIONS:
                decided[who] = verdict
        return "APPROVED" in decided.values() and "CHANGES_REQUESTED" not in decided.values()

    def _view(self, slug: str, fields: str) -> Dict[str, object]:
        """The comma-separated `fields` of `gh pr view` for the item's open PR. RuntimeError — as `_pr` raises it — when there
        is no open PR, gh fails or what it printed is not the JSON object asked for: the callers turn that into `unknown`."""
        pr = self._pr(slug, "open")
        if not pr:
            raise RuntimeError("no open PR for sdlc/" + slug)
        r = self._gh("pr", "view", str(pr["number"]), "--json", fields, timeout=60)
        if not r.ok:
            raise RuntimeError(r.text())
        try:
            data = json.loads(r.out)
        except ValueError:
            raise RuntimeError("unreadable gh output")
        if not isinstance(data, dict) or any(f not in data for f in fields.split(",")):
            raise RuntimeError("unreadable gh output")
        return data

    def checks(self, slug: str, head: str = "") -> Tuple[str, List[Dict[str, str]]]:
        """State of the open PR's checks: fail if any failed, else pending if any is still open, else pass; `none` when the
        PR has none (yet) — an empty or null rollup is how GitHub says so. Whatever cannot be read is `unknown`, never a
        guess a merge could go through on. With `head`, a PR that still shows another commit is `unknown` too: right after
        a push GitHub keeps the previous commit and its checks for a while, and its old failure is not this push's."""
        try:
            data = self._view(slug, "headRefOid,statusCheckRollup" if head else "statusCheckRollup")
        except RuntimeError:
            return "unknown", []
        if head and data["headRefOid"] != head:
            return "unknown", []
        rollup = data["statusCheckRollup"]
        if rollup is None:
            rollup = []
        if not isinstance(rollup, list) or not all(isinstance(e, dict) for e in rollup):
            return "unknown", []
        found = [_check_entry(e) for e in rollup]
        if not found:
            return "none", []
        buckets = {c["bucket"] for c in found}
        return ("fail" if "fail" in buckets else "pending" if "pending" in buckets else "pass"), found

    def check_logs(self, failed: List[Dict[str, str]], lines: int = 60, wait: Optional[Callable[[float], bool]] = None) -> str:
        """For each failed Actions run — its jobs share one log, and every `gh run view` is a slow call, so at most
        CHECK_LOG_RUNS of them — the end of each failed job's log, cut before the job's cleanup and without the per-line
        prefix (`trim_run_log`), headed with the failed checks' names; any other check by name and link. gh refuses the log
        of a run that is still in progress, which a run can be for a few seconds after its last check finished: such runs are
        asked again together after CHECK_LOG_WAIT seconds, CHECK_LOG_RETRIES times at most (`wait`: see Forge.check_logs),
        and one that stays refused is listed as having no log, like any log that cannot be read. Within CHECK_LOGS_CAP
        characters: a log's end says why it failed, so what goes is the front of each."""
        if wait is None:
            wait = _plain_sleep
        runs: List[str] = []                 # the runs whose log is read, in the order their checks come
        for c in failed:
            m = RUN_LINK.search(c.get("link") or "")
            if m and m.group(1) not in runs and len(runs) < CHECK_LOG_RUNS:
                runs.append(m.group(1))
        logs: Dict[str, str] = {rid: "" for rid in runs}     # run id -> the end of its log ("" when it could not be read)
        asking = runs
        for attempt in range(CHECK_LOG_RETRIES + 1):
            if attempt and not wait(CHECK_LOG_WAIT):
                break
            in_progress: List[str] = []
            for rid in asking:
                r = self._gh("run", "view", rid, "--log-failed", timeout=60)
                if r.ok:
                    logs[rid] = tail(trim_run_log(r.out), lines)
                elif "still in progress" in r.text():
                    in_progress.append(rid)
            asking = in_progress
            if not asking:
                break
        names: Dict[str, List[str]] = {}     # run id -> the failed checks that ran in it
        rest: List[str] = []
        for c in failed:
            name, link = (c.get("name") or "check")[:120], c.get("link") or ""
            m = RUN_LINK.search(link)
            rid = m.group(1) if m else ""
            if logs.get(rid):
                names.setdefault(rid, []).append(name)
            else:
                rest.append(name + (" — " + link[:240] if link else "") + (" (no log)" if rid in logs else ""))
        rest_text = "\n".join(rest)
        if len(rest_text) > CHECK_LOGS_CAP // 4:
            kept = rest_text[:CHECK_LOGS_CAP // 4].rsplit("\n", 1)[0].count("\n") + 1
            rest_text = "\n".join(rest[:kept]) + "\n… and %d more" % (len(rest) - kept)
        heads = ["== %s ==" % ", ".join(n) for n in names.values()]
        heads = [h if len(h) <= 300 else h[:299] + "…" for h in heads]
        room = CHECK_LOGS_CAP - len(rest_text) - sum(len(h) + 3 for h in heads)     # a head, its newline, the gap after it
        per = max(room, 0) // max(len(heads), 1)
        parts = ["%s\n%s" % (h, end_of(logs[rid], per)) for h, rid in zip(heads, names)]
        return "\n\n".join(parts + ([rest_text] if rest_text else []))

    def merge_state(self, slug: str) -> str:
        """GitHub's answer to "can the open PR be merged now": CLEAN, BEHIND (the branch has to be brought up to date first),
        BLOCKED, DIRTY, … — empty when it cannot be read."""
        try:
            return str(self._view(slug, "mergeStateStatus")["mergeStateStatus"] or "")
        except RuntimeError:
            return ""


def make_forge(git: Git, default: str, prefer: str = "auto") -> Forge:
    """auto: GitHub when there is an origin remote, else local. `local` with a remote merges locally and does not push."""
    if prefer == "local" or (prefer == "auto" and not git.has_remote()):
        git.prefer_local = True                 # changes are measured against the local default branch, not origin's
        return LocalForge(git, default)
    git.prefer_local = False
    return GitHubForge(git, default)


def remote_host(git: Git) -> str:
    url = git.out("remote", "get-url", "origin")
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)[:/]", url or "")
    return m.group(1) if m else ""
