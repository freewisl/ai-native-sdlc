"""The loop: take the next work item, run its sessions, gate it in the shell, merge it — until the queue is empty.

Per item (state read from its work doc on `sdlc/<slug>`):
  ready   → design session  (/sdlc:go --item <slug> --design)  → planned  (team: draft PR, a person approves)
  planned → build session   (/sdlc:go --item <slug> --build)   → built    (review tally recorded in the doc)
  built   → gate (shell)    → pass: done · fixable: fix session · human: wait with reasons
  done    → ship            → push, open the PR; on GitHub wait for its checks (`loop.checks_wait_minutes`): all pass → merge ·
                              one fails → back to `built`, a fix session gets its log, the gate runs again · still running at
                              the limit → a person. No remote: merge here. Team: a person merges, nothing is waited for.
Each session's effort is `loop.efforts` moved by the item's difficulty, an earlier failure and a second fix round (a self-check
runs at its base) — effort.py, `loop.auto_effort`.
A session stopped by a cap continues at once (warm cache, `--resume`); a stop older than `cold_resume_minutes` continues
in a fresh session; a stop without progress since the last one is a failure. Usage limits: a model-family limit switches
model, a session/weekly limit is slept out when it resets within `limit_wait_hours`, else the run stops (resumable).
"""
from __future__ import annotations

import os
import re
import signal
import time
from typing import Dict, List, Optional, Tuple

from . import state as st
from .config import PHASES, Config, NotInitialized
from .effort import LEVELS, doc_difficulty, shift
from .enqueue import EnqueueError, departure_reason, drain_inbox, enqueue, prepare, record_drop
from .events import log_event
from .gate import GateResult, run_gate
from .gitops import NOISE_EXCLUDES, Forge, Git, dirty_summary, end_of, make_forge, remote_host
from .queue import Item, TEAM_WAITS, runnable, scan, waiting
from .session import Runner, SessionResult
from .util import epoch, now, now_iso, plugin_version, read_text, run, say, stamp, tail
from .workdoc import WorkDoc, list_docs, problems

CI_BRIEF_CAP = 8000     # characters of what a fix session is told about failed PR checks
FAMILIES = ("opus", "sonnet", "haiku", "fable")
INFRA_HINTS = ("overloaded", "api error: 5", "internal server error", "service unavailable", "bad gateway",
               "gateway timeout", "econnreset", "etimedout", "socket hang up", "network error", "fetch failed")


def filled(text: str) -> bool:
    """A section with something in it: the template's HTML-comment hints and whitespace do not count."""
    return bool(re.sub(r"<!--.*?-->", "", text or "", flags=re.S).strip())


class StopRun(Exception):
    def __init__(self, reason: str, code: int = 0):
        super().__init__(reason)
        self.reason, self.code = reason, code


class AlreadyRunning(StopRun):
    """Another loop owns this checkout: its status file is the record that matters, so this refusal leaves it alone."""


class Outcome:
    MERGED, AWAITING, PARKED, FAILED, INFRA, HUMAN, ADVANCED, SKIPPED, FIX = (
        "merged", "awaiting", "parked", "failed", "infra", "needs-human", "advanced", "skipped", "fix")   # FIX: `ship_now` only


def session_note(model: str, effort: str, why: str = "") -> str:
    """What the run log says about a session: `model sonnet, effort medium — difficulty easy` (the effort, and what moved it)."""
    note = "model %s" % (model or "default")
    if effort:
        note += ", effort %s" % effort + (" — " + why if why else "")
    return note


def family(model: str) -> str:
    m = (model or "").lower()
    for f in FAMILIES:
        if f in m:
            return f
    return m


class Engine:
    def __init__(self, root: str, items: Optional[List[str]] = None, max_items: Optional[int] = None,
                 max_minutes: Optional[float] = None, claude_bin: str = "", plugin_dir: str = "", dry_run: bool = False):
        self.root = root
        self.cfg = Config(root)
        self.git = Git(root)
        self.default = self.cfg.default_branch
        self.forge: Forge = make_forge(self.git, self.default, str(self.cfg.get("loop.forge", "auto")))
        self.team = not self.cfg.solo
        L = lambda k, d=None: self.cfg.get("loop." + k, d)
        self.session_minutes = float(L("session_max_minutes", 120) or 120)
        self.max_continuations = int(L("max_continuations", 3) or 0)
        self.cold_seconds = float(L("cold_resume_minutes", 55) or 55) * 60
        self.max_fail = int(L("max_failures_per_slug", 3) or 3)
        self.max_consec = int(L("max_consecutive_failures", 3) or 3)
        self.fix_rounds = int(L("fix_rounds", 2) or 0)
        self.limit_wait = float(L("limit_wait_hours", 6) or 0) * 3600
        self.poll = float(L("poll_minutes", 10) or 10) * 60
        self.idle_max = float(L("idle_hours", 12) or 12) * 3600
        self.max_items = int(max_items if max_items is not None else (L("max_items", 0) or 0))
        self.max_minutes = float(max_minutes if max_minutes is not None else (L("max_minutes", 0) or 0))
        self.note = str(L("item_note", "") or "")
        self.only = items
        self.dry = dry_run
        self.pause_file = os.path.join(root, str(L("pause_file", ".sdlc/state/pause")))
        self.runner = Runner(root, claude_bin=claude_bin, plugin_dir=plugin_dir, max_turns=int(L("max_turns", 500) or 500),
                             allowed_tools=str(L("allowed_tools", "") or ""), budget_usd=float(L("budget_usd_per_session", 0) or 0),
                             isolate=bool(L("isolate_sessions", False)))
        self.claims = st.Claims(self.git.common_dir(), root)
        self.log_path = os.path.join(root, ".sdlc", "logs", "run-%s.log" % stamp())
        self.stopping = ""            # set by a signal: finish nothing more, stop the session gracefully
        self.started = epoch()
        self.cost = 0.0
        self.done: Dict[str, List[str]] = {"merged": [], "awaiting": [], "parked": [], "failed": [], "needs-human": []}
        self.failed_now: List[str] = []
        self.items_run = 0
        self.consec = 0
        self.blocked_family: Dict[str, float] = {}   # family → epoch until which it is limited
        self.from_sha = ""
        self.self_checks = 0
        self.merged_since_check: List[str] = []   # slugs this loop merged since the run began or the last self-check
        self.skip_noted = False                   # the "self-check skipped" line is said once per run
        self.status: Dict[str, object] = {}
        self.retry_delay = 60.0       # after a service error
        self.limit_margin = 60.0      # past a usage-limit reset before trying again
        self.checks_poll: Optional[float] = None    # seconds between looks at a PR's checks; None: a tenth of the wait, 1-30 s
        self.checks_grace: Optional[float] = None   # how long "no checks yet" is waited out; None: 120 s with workflow files, else 0
        self.last_brief = ""          # what the last interactive ship wants fixed (the PR's failed checks), for `ship_now`

    # ------------------------------------------------------------------ small helpers
    def say(self, line: str) -> None:
        say(line, self.log_path)

    def base(self) -> str:
        return self.git.base_ref(self.default)

    def doc_path(self, slug: str) -> str:
        return os.path.join(self.root, self.cfg.rel("work"), slug + ".md")

    def doc(self, slug: str) -> Optional[WorkDoc]:
        return WorkDoc.load(self.doc_path(slug))

    def doc_status(self, slug: str) -> str:
        d = self.doc(slug)
        return d.status if d else ""

    def set_status(self, **kw: object) -> None:
        self.status.update(kw)
        self.status.update({"pid": os.getpid(), "root": self.root, "started": self.status.get("started") or now_iso(),
                            "cost_usd": round(self.cost, 2), "done": self.done, "log": os.path.relpath(self.log_path, self.root)})
        st.write_status(self.root, self.status)

    def event(self, event: str, decision: str, reason: str = "", detail: Optional[dict] = None) -> None:
        log_event(self.root, event, decision, reason, detail)

    def sleep(self, seconds: float) -> bool:
        """Interruptible sleep. False when a stop was requested meanwhile."""
        end = time.time() + seconds
        while time.time() < end:
            if self.stopping or os.path.exists(self.pause_file):
                return False
            time.sleep(min(5.0, max(0.1, end - time.time())))
        return True

    def model_for(self, phase: str, slug: str = "") -> str:
        """The model a session of this phase runs on. A build for an item that has failed before runs on
        `loop.escalate_model`; a family that hit its usage limit gives way to another configured model of a different
        family, else sonnet, else opus."""
        m = self.cfg.model(phase)
        esc = self.cfg.escalate_model
        if phase == "build" and esc and slug and int(st.item(self.root, slug).get("failures", 0) or 0) > 0:
            m = esc
        now_ = epoch()
        if self.blocked_family.get(family(m), 0) > now_:
            for alt in [self.cfg.model(p) for p in PHASES] + [esc, "sonnet", "opus"]:
                if alt and family(alt) != family(m) and self.blocked_family.get(family(alt), 0) <= now_:
                    return alt
        return m

    def effort_for(self, phase: str, slug: str = "", fix_round: int = 0) -> Tuple[str, str]:
        """(effort, why) of the next session of this phase: `loop.efforts` moved by what the loop knows. An easy item
        (`difficulty: easy` in its work doc) needs less in design and build, a hard one more in all three; an item that
        has failed before, and every fix round after the first, one more. `why` names what moved it — "" when nothing
        did. An effort a person set to "" or "max" is left alone, and so is every effort when `loop.auto_effort` is off;
        so is a self-check's, which has no item and runs at its base."""
        base = self.cfg.effort(phase)
        if not bool(self.cfg.get("loop.auto_effort", True)) or base not in LEVELS or base == "max" \
                or phase not in ("design", "build", "fix"):
            return base, ""
        steps, why = 0, []
        difficulty = doc_difficulty(self.doc(slug) if slug else None)
        if difficulty == "easy" and phase != "fix":     # a fix works on a failure: an easy item fails like a hard one
            steps -= 1
            why.append("difficulty easy")
        elif difficulty == "hard":
            steps += 1
            why.append("difficulty hard")
        if slug and int(st.item(self.root, slug).get("failures", 0) or 0) > 0:
            steps += 1
            why.append("failed before")
        if phase == "fix" and fix_round >= 2:
            steps += 1
            why.append("fix round %d" % fix_round)
        return shift(base, steps), ", ".join(why)

    # ------------------------------------------------------------------ preflight
    def preflight(self) -> None:
        if not self.git.is_repo():
            raise StopRun("%s is not a git repository" % self.root, 2)
        if self.cfg.legacy:
            raise StopRun("this repository still has a version-1 sdlc layout — run /sdlc:init once to migrate it", 2)
        if self.cfg.get("loop.enabled", True) is False:
            raise StopRun("the loop is switched off (loop.enabled=false in .sdlc/config.json)", 2)
        if self.team and self.forge.kind == "local":
            raise StopRun("team mode needs a GitHub remote: people approve the design and merge the PR there", 2)
        if os.path.exists(self.pause_file):
            raise StopRun("paused (%s exists) — `sdlc.py resume` removes it" % os.path.relpath(self.pause_file, self.root), 0)
        other = st.loop_pid(self.root)
        if other and other != os.getpid():
            raise AlreadyRunning("a loop is already running in this checkout (pid %d)" % other, 2)
        if self.dry:
            return
        if self.forge.kind == "github":
            host = remote_host(self.git)
            if not run(["gh", "auth", "status", "--hostname", host or "github.com"], cwd=self.root, timeout=30).ok:
                raise StopRun("gh is not logged in to %s — run: gh auth login --hostname %s" % (host, host), 2)
        if not self.runner.available():
            raise StopRun("claude executable not found (put claude on PATH, set SDLC_CLAUDE_BIN, or pass --claude-bin)", 2)

    # ------------------------------------------------------------------ the run
    def install_signals(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            self.stopping = "stopped by %s" % signal.Signals(signum).name
            self.runner.interrupt = True
        self._prev_handlers = {}
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                self._prev_handlers[s] = signal.signal(s, handler)
            except (ValueError, OSError):
                pass

    def restore_signals(self) -> None:
        for s, h in getattr(self, "_prev_handlers", {}).items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError, TypeError):
                pass

    def refuse(self, last: Dict[str, object], reason: str) -> None:
        """A start that is refused (a dirty tree, no claude, no gh login) is not a run: the record the last run left — why it
        stopped, when — stays as it was, and why this start did not happen is put beside it for `status`."""
        try:
            st.write_status(self.root, dict(last, state="stopped", refused=reason, refused_at=now_iso()))
        except OSError:
            pass                                    # nowhere to write it: the refusal is still said, and returned

    def run(self) -> int:
        last = st.read_status(self.root)
        resume_here = ""
        try:
            self.preflight()
            if not self.dry:
                resume_here = self.start_tree()     # before the loop leaves any trace: a refusal keeps the last run's record
        except StopRun as e:
            self.say(("ERROR   " if e.code else "STOP    ") + e.reason)
            if e.code == 2 and not self.dry and not isinstance(e, AlreadyRunning):
                self.refuse(last, e.reason)
            return e.code
        if self.dry:
            return self.dry_run()
        st.write_pid(self.root)
        self.install_signals()
        self.set_status(state="starting", started=now_iso(), version=plugin_version())
        self.say("== sdlc run  %s  default=%s  forge=%s  mode=%s  models=%s  efforts=%s%s" % (
            self.root, self.default, self.forge.kind, "team" if self.team else "solo",
            "/".join("%s:%s" % (p, self.cfg.model(p) or "default") for p in ("design", "build", "fix")),
            "/".join("%s:%s" % (p, self.cfg.effort(p) or "default") for p in ("design", "build", "fix")),
            " (auto)" if self.cfg.get("loop.auto_effort", True) else ""))
        if resume_here:
            self.say("RESUME  %s — the session stopped last time left uncommitted changes here; continuing it first" % resume_here)
        stop, code = "", 0
        try:
            self.from_sha = self.git.head(self.base()) or self.git.head()
            idle_since = 0.0
            while True:
                stop = self.check_stop()
                if stop:
                    break
                if resume_here:
                    nxt, resume_here = resume_here, ""
                    items = self.scan()
                    it = items.get(nxt)
                    if it is None:
                        continue
                else:
                    self.to_default()
                    self.take_inbox()
                    items = self.scan()
                    self.poll_team(items)
                    if self.poll_human(items):
                        items = self.scan()                  # a finished item changes what its dependents wait for
                    order = runnable(items, self.only, skip=self.failed_now + [s for s in items if self.claims.owner(s)])
                    it = order[0] if order else None
                if it is None:
                    if self.self_check_due() and self.self_check():
                        continue                          # what it queued is read again, from the default branch brought up to date
                    pollable = self.pollable(items)
                    if pollable and not self.only:
                        idle_since = idle_since or epoch()
                        if epoch() - idle_since > self.idle_max:
                            stop = "idle for %d h with only waiting items" % (self.idle_max // 3600)
                            break
                        self.set_status(state="waiting", item="", phase="", waiting=[s for s, _ in pollable])
                        self.say("WAIT    %s — checking again in %d min" % (", ".join("%s (%s)" % p for p in pollable[:4]),
                                                                          self.poll // 60))
                        if not self.sleep(self.poll):
                            continue
                        if self.forge.kind == "github":
                            self.git.fetch()
                        continue
                    human = waiting(items, "human")
                    stop = "nothing runnable — %d item(s) wait for you" % len(human) if human else "queue empty"
                    break
                idle_since = 0.0
                self.items_run += 1
                out = self.work(it)
                self.tally(it.slug, out)
                if self.consec >= self.max_consec:
                    stop = "%d failures in a row" % self.consec
                    break
        except StopRun as e:
            stop, code = e.reason, e.code
        except KeyboardInterrupt:
            stop = "interrupted"
        finally:
            self.claims.release()
            st.set_active(self.root, None)
            self.finish_run(stop or self.stopping or "stopped")
            st.clear_pid(self.root)
            self.restore_signals()
        return code if code else (1 if self.done["failed"] else 0)

    def pollable(self, items: Dict[str, Item]) -> List[Tuple[str, str]]:
        """Waits that resolve without anyone here: a person approving/merging a PR (team), another loop finishing."""
        busy = {s for s in items if self.claims.owner(s) and items[s].phase not in ("done", "dropped", "closed")}
        out = [(s, "worked on by another loop") for s in sorted(busy)]
        team = {s for s, it in items.items() if it.wait_kind == "poll"}
        out += [(s, items[s].wait) for s in sorted(team)]
        for s, it in sorted(items.items()):
            if it.wait_kind == "dep" and (it.blocker in busy or it.blocker in team):
                out.append((s, it.wait))
        return out

    def check_stop(self) -> str:
        if self.stopping:
            return self.stopping
        if os.path.exists(self.pause_file):
            return "paused"
        if self.max_items and self.items_run >= self.max_items:
            return "max_items (%d)" % self.max_items
        if self.max_minutes and (epoch() - self.started) / 60 >= self.max_minutes:
            return "max_minutes (%d)" % self.max_minutes
        return ""

    def tally(self, slug: str, out: str) -> None:
        if out in (Outcome.FAILED, Outcome.INFRA):
            self.consec += 1
            if out == Outcome.FAILED:
                self.done["failed"].append(slug)
            self.failed_now.append(slug)
        else:
            self.consec = 0
            key = {"merged": "merged", "awaiting": "awaiting", "parked": "parked", "needs-human": "needs-human"}.get(out)
            if out == Outcome.SKIPPED:
                self.failed_now.append(slug)
            if key:
                self.done[key].append(slug)
            if out == Outcome.MERGED:
                self.merged_since_check.append(slug)

    def finish_run(self, stop: str) -> None:
        mins = int((epoch() - self.started) // 60)
        d = self.done
        self.return_to_default()
        self.say("== done  merged=%d awaiting=%d parked=%d needs_human=%d failed=%d stop=%s (%d min, $%.2f)" % (
            len(d["merged"]), len(d["awaiting"]), len(d["parked"]), len(d["needs-human"]), len(d["failed"]), stop, mins, self.cost))
        self.set_status(state="stopped", stop=stop, item="", phase="", ended=now_iso(), minutes=mins)
        self.event("loop.run", "allow", stop, {"merged": d["merged"], "awaiting": d["awaiting"], "parked": d["parked"],
                                                "needs_human": d["needs-human"], "failed": d["failed"], "minutes": mins,
                                                "cost_usd": round(self.cost, 4)})

    def dry_run(self) -> int:
        items = self.scan()
        order = runnable(items, self.only)
        self.say("== dry run  %s  forge=%s  mode=%s" % (self.root, self.forge.kind, "team" if self.team else "solo"))
        for it in order:
            self.say("WOULD   %-28s %s (%s)" % (it.slug, it.phase, it.title))
        for kind in ("dep", "poll", "human"):
            for slug, why in waiting(items, kind):
                self.say("WAITS   %-28s %s" % (slug, why))
        return 0

    # ------------------------------------------------------------------ git moves
    def return_to_default(self) -> None:
        """A run that ends leaves the checkout on the default branch, not on the last item's branch — when that loses
        nothing. A branch with uncommitted changes stays as it is (the next run continues it in place)."""
        b = self.git.branch()
        if not b.startswith("sdlc/"):
            return
        dirty = self.git.dirty()
        if dirty:
            self.say("LEFT    still on %s: uncommitted changes (%s) — look at them, then `git switch %s`" % (
                b, dirty_summary(dirty), self.default))
        elif self.git.ok("checkout", "-q", self.default) or self.git.ok("checkout", "-q", "--detach", self.base()):
            self.say("BACK    on %s again" % self.default)
        else:
            self.say("LEFT    still on %s: %s" % (b, self.git.with_reason("could not check out " + self.default)))

    def start_tree(self) -> str:
        """A clean tree, or the uncommitted work of this loop's own stopped item (continued in place first)."""
        dirty = self.git.dirty()
        b = self.git.branch()
        if not dirty:
            return ""
        if b.startswith("sdlc/") and st.item(self.root, b[5:]).get("stop"):
            return b[5:]                    # `run` says so, after its header line
        raise StopRun("the working tree has uncommitted changes (%s) — commit or stash them first" % dirty_summary(dirty), 2)

    def to_default(self) -> None:
        dirty = self.git.dirty()
        if dirty:
            raise StopRun("the working tree is not clean on %s: %s" % (self.git.branch() or "HEAD", dirty_summary(dirty)), 2)
        if not self.git.to_default(self.default):
            raise StopRun(self.git.with_reason("cannot check out or fast-forward " + self.default), 2)
        self.refresh_config()

    def refresh_config(self) -> None:
        """`.sdlc/config.json` changes on the default branch while the loop runs — a skeleton item fills in `commands.*` and
        this very loop merges it — and the next item must be gated by the file as it is now, not as the run began."""
        try:
            fresh = Config(self.root)
        except NotInitialized:
            return                                   # unreadable at this moment: keep what the run has
        if fresh.raw != self.cfg.raw:
            self.cfg = fresh
            self.say("CONFIG  .sdlc/config.json changed on %s — the next items use the new one" % self.default)

    def scan(self) -> Dict[str, Item]:
        return scan(self.cfg, self.git, self.base(), st.load_items(self.root))

    def take_inbox(self) -> None:
        try:
            slugs = drain_inbox(self.cfg, self.git, self.forge)
        except EnqueueError as e:
            self.say("INBOX   could not queue the parked docs: %s" % e)
            return
        if slugs:
            self.say("QUEUED  %s (from the inbox)" % ", ".join(slugs))

    def checkout_item(self, slug: str) -> None:
        b = "sdlc/" + slug
        if self.git.branch() == b:
            return
        if self.git.branch_exists(b):
            r = self.git("checkout", "-q", b)
        elif self.git.remote_branch_exists(b):
            r = self.git("checkout", "-q", "-b", b, "--track", "origin/" + b)
        else:
            r = self.git("checkout", "-q", "-b", b, self.base())
        if not r.ok:
            raise StopRun("cannot check out %s: %s" % (b, r.text()[-200:]), 2)
        self.adopt_doc(slug)

    def adopt_doc(self, slug: str) -> None:
        """A branch made before the work doc existed (by 0.x, or by hand) gets the default branch's doc. It comes in by
        merging the default branch: a copy committed on the branch would be an add on both sides and conflict at the gate
        as soon as the session edits it. A merge that conflicts is undone and the doc alone is copied over instead."""
        if os.path.exists(self.doc_path(slug)):
            return
        rel = os.path.relpath(self.doc_path(slug), self.root)
        base = self.base()
        if self.git.show(base, rel) is None:
            return
        msg = "sdlc(%s): work doc from %s" % (slug, self.default)
        r = self.git("-c", "core.hooksPath=" + os.devnull, "merge", "--no-edit", "-m", msg, base)
        if r.ok and os.path.exists(self.doc_path(slug)):
            return
        if not r.ok:
            self.git("merge", "--abort")
        if self.git.ok("checkout", base, "--", rel):
            self.git("commit", "-q", "--no-verify", "-m", msg, "--", rel)

    def commit_all(self, slug: str, message: str) -> bool:
        """Commit whatever a session left uncommitted on the item branch (never on the default branch)."""
        if not self.git.dirty() or self.git.branch() != "sdlc/" + slug:
            return False
        self.git("add", "-A", "--", ".", *NOISE_EXCLUDES)
        r = self.git("commit", "-q", "-m", message)
        if not r.ok:
            r = self.git("commit", "-q", "--no-verify", "-m", message)
        return r.ok

    def ensure_branch(self, slug: str) -> None:
        """A session may switch branches; the engine's view of the item is sdlc/<slug>."""
        b = "sdlc/" + slug
        if self.git.branch() == b:
            return
        dirty = self.git.dirty()
        if dirty:
            raise StopRun("the session left %s with uncommitted changes (%s; expected %s) — sort it out by hand" %
                          (self.git.branch() or "a detached HEAD", dirty_summary(dirty), b), 2)
        if not self.git.ok("checkout", "-q", b):
            raise StopRun(self.git.with_reason("cannot return to %s after the session" % b), 2)

    def leave_item(self, slug: str) -> None:
        if self.git.dirty() and self.git.branch() == "sdlc/" + slug:
            self.commit_all(slug, "sdlc(%s): work in progress (session stopped)" % slug)
        st.set_active(self.root, None)
        self.claims.release()

    # ------------------------------------------------------------------ one item
    def work(self, it: Item) -> str:
        slug = it.slug
        if not self.claims.take(slug):
            return Outcome.SKIPPED
        self.say("ITEM    %s — %s  [%s]" % (slug, it.title, it.phase))
        self.set_status(state="running", item=slug, title=it.title, phase=it.phase, phase_started=now_iso(), waiting=[],
                        model="", effort="")
        try:
            if it.phase == "merge" and not it.branch:
                return self.fail(slug, "status done on no branch — nothing to merge", count=False)
            self.checkout_item(slug)
            ran = ""                      # the phase of the last session this call ran: the one that can ask a question
            for _ in range(12):
                if self.stopping:
                    raise StopRun(self.stopping)
                s = self.doc_status(slug)
                rec = st.item(self.root, slug)
                if s == "needs-input":
                    if rec.get("answers"):
                        self.apply_answers(slug, rec)
                        continue
                    return self.park(slug, ran or rec.get("parked_phase") or "design")
                if s in ("proposed", "dropped"):
                    if s == "dropped":
                        self.session_dropped(slug)
                    return Outcome.SKIPPED
                if s in ("ready", ""):
                    ran = "design"
                    out = self.session(slug, "design")
                elif s == "planned":
                    if self.team and not rec.get("approved"):
                        return self.ask_design_approval(slug)
                    ran = "build"
                    out = self.session(slug, "build")
                elif s == "built":
                    out = self.gate_phase(slug)
                    if out != Outcome.ADVANCED:
                        return out
                    continue
                elif s == "done":
                    out = self.ship(slug)
                    if out != Outcome.ADVANCED:           # a failed check sends the item back to `built`: the gate runs again
                        return out
                    continue
                else:
                    return self.fail(slug, "unexpected status '%s' on sdlc/%s" % (s, slug))
                if out != Outcome.ADVANCED:
                    return out
            return self.fail(slug, "no progress after 12 steps")
        finally:
            self.leave_item(slug)

    # ------------------------------------------------------------------ sessions
    def prompt(self, slug: str, phase: str, extra: str = "", cont: str = "") -> str:
        p = "/sdlc:go --item %s --%s" % (slug, phase)
        if cont:
            p += " --continue\n\nA previous session for this phase stopped (%s). Its work is on this branch, committed or" \
                 " not — check `git status` and `git log %s..HEAD`, then pick up where it stopped." % (cont, self.base())
        if self.note:
            p += "\n\n" + self.note
        if extra:
            p += "\n\n" + extra
        return p

    def session(self, slug: str, phase: str, extra: str = "", fix_round: int = 0) -> str:
        """Run one phase to completion: the first session plus continuations. Returns ADVANCED when the doc moved on (or,
        for fix, when the branch changed), else FAILED/INFRA; raises StopRun for limits it cannot wait out. `fix_round` is 1
        for the first fix session after a gate failure, 2 for the next — a later round gets more effort."""
        before_status = self.doc_status(slug)
        before_head = self.git.head()
        rec = st.item(self.root, slug)
        stop = rec.get("stop") or {}
        resume_sid, cont_reason, last_fp = "", "", ""
        if stop and stop.get("phase") == phase:
            last_fp = str(stop.get("fp") or "")
            cont_reason = str(stop.get("why") or "stopped")
            if epoch() - float(stop.get("at") or 0) < self.cold_seconds and stop.get("model") == self.model_for(phase, slug):
                resume_sid = str(stop.get("sid") or "")
        continuations = 0
        infra_retries = 0
        limit_hits = 0
        while True:
            model = self.model_for(phase, slug)
            effort, effort_why = self.effort_for(phase, slug, fix_round)       # per window: the doc, the failures may have changed
            if resume_sid:
                prompt = ("Continue where you stopped (%s): finish the %s phase as /sdlc:go --item %s --%s describes, "
                          "then print the marker." % (cont_reason, phase, slug, phase))
            else:
                prompt = self.prompt(slug, phase, extra, cont_reason)
            log = os.path.join(self.root, ".sdlc", "logs", "sessions", "%s-%s-%s.jsonl" % (stamp(), slug, phase))
            self.set_status(phase=phase, phase_started=now_iso(), session_log=os.path.relpath(log, self.root), model=model,
                            effort=effort)
            self.say("%-7s %s — %s%s (%s)" % (phase.upper()[:7], slug, "resuming " if resume_sid else "",
                                              "session" if not cont_reason or resume_sid else "fresh session after a stop",
                                              session_note(model, effort, effort_why)))
            st.set_active(self.root, slug)
            res = self.runner.run(prompt, log, self.session_minutes, model, effort, resume_sid)
            st.set_active(self.root, None)
            self.ensure_branch(slug)
            self.account(slug, phase, res, bool(resume_sid), effort, effort_why, bool(resume_sid or cont_reason))
            if res.status == "no_plugin":       # whatever it did ran without the guard hooks: stop before trusting it
                raise StopRun("the sdlc plugin was not loaded in the session — its hooks did not run (check `claude plugin list`)", 2)
            blocked = res.token("SDLC_FIX_BLOCKED") if phase == "fix" and res.status == "ok" else None
            if blocked is not None:             # it says it cannot: what it left in Departures goes to a person with the branch
                st.update(self.root, slug, stop=None)
                self.commit_all(slug, "sdlc(%s): %s session changes" % (slug, phase))
                return self.needs_human(slug, ["the fix session could not fix it: %s" % (blocked[:300] or "see Departures")])
            if self.advanced(slug, phase, before_status, before_head) or (phase == "fix" and res.status == "ok"):
                st.update(self.root, slug, stop=None)
                self.commit_all(slug, "sdlc(%s): %s session changes" % (slug, phase))
                return Outcome.ADVANCED
            if res.api_error and res.api_error.startswith(("401", "403")):
                raise StopRun("claude reported an authentication error (%s)" % res.api_error, 2)
            if res.status == "ok":
                if phase == "design" and self.design_unfinished(slug):
                    self.reopen_design(slug, before_status)
                    return self.fail(slug, "design left Plan/Proof empty")
                said = tail(res.text, 3)[:300].strip() or "it printed no final message (%d turns%s)" % (
                    res.turns, ", ended: " + res.terminal if res.terminal else "")
                return self.fail(slug, "the %s session ended without finishing it: %s" % (phase, said))
            if res.status == "error":
                if self.is_infra(res) and infra_retries < 2:
                    infra_retries += 1
                    self.say("RETRY   %s — %s; again in %d s" % (slug, res.api_error or "no result", self.retry_delay))
                    if not self.sleep(self.retry_delay):
                        raise StopRun(self.stopping or "paused")
                    resume_sid = res.sid if res.turns else ""
                    continue
                if self.is_infra(res):
                    self.remember_stop(slug, phase, res, "service error", model=model)
                    return Outcome.INFRA
                return self.fail(slug, "the %s session failed: %s" % (phase, res.api_error or tail(res.text, 2)[:200]))
            # stopped: max_turns · budget · timeout · limited · interrupted
            budget = self.runner.budget
            why = {"max_turns": "turn cap", "budget": "budget cap ($%g)" % budget if budget > 0 else "budget cap",
                   "timeout": "time cap (%d min)" % self.session_minutes, "limited": res.limit_text,
                   "interrupted": "stopped by request"}.get(res.status, res.status)
            fp = self.git.diff_hash()
            self.remember_stop(slug, phase, res, why, fp, model)
            if res.status == "interrupted" or self.stopping:
                raise StopRun(self.stopping or "interrupted")
            if res.status == "limited":
                limit_hits += 1
                if limit_hits > 3:
                    raise StopRun("usage limit hit %d times in a row (%s)" % (limit_hits, res.limit_text))
                self.handle_limit(res, model, phase, slug)        # waits, switches model, or raises StopRun
            else:
                if fp == last_fp:
                    st.update(self.root, slug, stop=None)
                    return self.fail(slug, "no progress since the last stop (%s)" % why)
                continuations += 1
                if continuations > self.max_continuations:
                    return self.fail(slug, "still unfinished after %d continuations (%s)" % (self.max_continuations, why))
            last_fp, cont_reason = fp, why
            fresh = (epoch() - res.ended) >= self.cold_seconds or self.model_for(phase, slug) != model
            resume_sid = "" if fresh else res.sid
            self.say("STOPPED %s — %s; continuing (%s)" % (slug, why, "fresh session" if fresh else "warm resume"))
            self.event("loop.continue", "allow", why, {"slug": slug, "phase": phase, "sid": res.sid, "fresh": fresh,
                                                       "continuations": continuations})

    def is_infra(self, res: SessionResult) -> bool:
        txt = (res.api_error + " " + (res.text or "")[-300:]).lower()
        return bool(res.api_error and res.api_error[:1] == "5") or any(h.lower() in txt for h in INFRA_HINTS) or \
            (not res.text and not res.turns)

    def design_unfinished(self, slug: str) -> bool:
        """The doc says planned (or built) but has no Plan or no Proof — a status flip is not a design."""
        d = self.doc(slug)
        return d is not None and d.status in ("planned", "built") and not (filled(d.section("Plan")) and filled(d.section("Proof")))

    def reopen_design(self, slug: str, status: str) -> None:
        """Put the doc back to where the design session found it, so the next attempt designs again instead of building
        on an empty plan."""
        d = self.doc(slug)
        if d is None:
            return
        d.status = status if status in ("ready", "proposed") else "ready"
        d.save()
        self.git("add", "--", os.path.relpath(self.doc_path(slug), self.root))
        self.git("commit", "-q", "--no-verify", "-m", "sdlc(%s): design left Plan/Proof empty — back to %s" % (slug, d.status))

    def advanced(self, slug: str, phase: str, before_status: str, before_head: str) -> bool:
        s = self.doc_status(slug)
        if phase == "design":
            if s in ("planned", "built"):
                return s != before_status and not self.design_unfinished(slug)
            return s in ("needs-input", "dropped") and s != before_status
        if phase == "build":
            return s in ("built", "needs-input", "dropped") and s != before_status
        if phase == "fix":
            return self.git.head() != before_head or bool(self.git.dirty())
        return s != before_status

    def account(self, slug: str, phase: str, res: SessionResult, resumed: bool, effort: str = "", effort_why: str = "",
                continued: bool = False) -> None:
        self.cost += res.cost_delta

        def fn(rec: dict) -> None:
            rec["cost"] = round(float(rec.get("cost", 0) or 0) + res.cost_delta, 4)
            rec["sessions"] = int(rec.get("sessions", 0) or 0) + 1
        st.mutate(self.root, slug, fn)
        self.event("loop.session", res.status, phase, {
            "slug": slug, "phase": phase, "sid": res.sid, "status": res.status, "resumed": resumed, "cost_usd": res.cost_delta,
            "turns": res.turns, "models": res.models, "minutes": round((res.ended - res.started) / 60, 1),
            "log": os.path.relpath(res.log, self.root), "limit": res.limit_kind, "api_error": res.api_error,
            "effort": effort, "effort_reason": effort_why, "continued": continued})
        self.set_status()

    def remember_stop(self, slug: str, phase: str, res: SessionResult, why: str, fp: str = "", model: str = "") -> None:
        st.update(self.root, slug, stop={"sid": res.sid, "phase": phase, "at": res.ended or epoch(), "why": why,
                                          "fp": fp or self.git.diff_hash(), "model": model or self.model_for(phase, slug)})

    def handle_limit(self, res: SessionResult, model: str, phase: str = "build", slug: str = "") -> None:
        kind = res.limit_kind
        reset = res.limit_reset or 0.0
        self.event("loop.limit", "wait", res.limit_text, {"kind": kind, "reset": reset, "model": model})
        if kind in FAMILIES:
            self.blocked_family[kind] = reset or (epoch() + 5 * 3600)
            alt = self.model_for(phase, slug)
            if family(alt) != kind:
                self.say("LIMIT   %s — switching to %s" % (res.limit_text, alt))
                return
        if kind == "spend":
            raise StopRun("spend limit reached (%s)" % res.limit_text)
        wait = max(0.0, reset - epoch() + self.limit_margin) if reset else 30 * 60
        if wait > self.limit_wait:
            until = now().astimezone().strftime("%m-%d %H:%M") if not reset else \
                time.strftime("%m-%d %H:%M", time.localtime(reset))
            raise StopRun("usage limit (%s) resets %s — beyond loop.limit_wait_hours; run again after it resets" %
                          (res.limit_text, until if reset else "at an unknown time"))
        self.set_status(state="waiting-limit", wake_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch() + wait)))
        self.say("LIMIT   %s — waiting %d min" % (res.limit_text, wait // 60))
        if not self.sleep(wait):
            raise StopRun(self.stopping or "paused")
        self.set_status(state="running", wake_at="")

    # ------------------------------------------------------------------ outcomes
    def fail(self, slug: str, reason: str, count: bool = True) -> str:
        n = 0
        if count:
            n = int(st.mutate(self.root, slug, lambda r: r.update(failures=int(r.get("failures", 0) or 0) + 1,
                                                                     last_error=reason)).get("failures", 0))
        self.say("FAILED  %s — %s%s" % (slug, reason, " (%d/%d)" % (n, self.max_fail) if count else ""))
        self.event("loop.item", "fail", reason, {"slug": slug, "failures": n})
        if count and n >= self.max_fail and self.forge.kind == "github" and self.git.branch_exists("sdlc/" + slug):
            self.publish(slug, draft=True)
            self.forge.flag_human(slug, ["the loop gave up after %d failures: %s" % (n, reason)])
        return Outcome.FAILED

    def needs_human(self, slug: str, reasons: List[str]) -> str:
        st.update(self.root, slug, state="needs-human", reasons=reasons)
        self.say("HUMAN   %s — %s" % (slug, "; ".join(reasons)[:400]))
        self.event("loop.item", "needs-human", "; ".join(reasons)[:500], {"slug": slug, "reasons": reasons})
        if self.forge.kind == "github":
            self.publish(slug, draft=False)
            self.forge.flag_human(slug, reasons)
        return Outcome.HUMAN

    def session_dropped(self, slug: str) -> None:
        """A session found the item unnecessary and set its doc to `dropped` — on the item branch, which nobody merges. The
        record `sdlc.py drop` makes goes to the default branch (team: a PR that waits for a person), where every clone
        reads it, and the item's own PR is closed. The local state is set first and stays whatever happens here: when the
        record cannot be made this checkout still leaves the item alone, the failure is announced, and the loop goes on."""
        reason = departure_reason(self.doc(slug)) or "dropped by the session"
        st.update(self.root, slug, state="dropped", reasons=[reason])
        try:
            res = record_drop(self.cfg, self.git, self.forge, slug, reason, from_loop=True)
        except (EnqueueError, OSError) as e:
            err = " ".join(str(e).replace(self.root + os.sep, "").split())[:300]
            st.update(self.root, slug, reasons=[reason, "not recorded on %s: %s" % (self.default, err)])
            self.say("DROPPED %s — %s (NOT recorded on %s: %s — `sdlc.py drop %s \"<reason>\"` records it)" % (
                slug, reason, self.default, err, slug))
            self.event("loop.item", "drop-not-recorded", reason, {"slug": slug, "error": err})
            return
        self.forge.close(slug, "sdlc: dropped — " + reason)
        self.say("DROPPED %s — %s (%s)" % (slug, reason, res["how"]))
        self.event("loop.item", "dropped", reason, {"slug": slug, "how": res["how"], "pr": res["pr"], "merged": res["merged"]})

    def park(self, slug: str, phase: str) -> str:
        self.commit_all(slug, "sdlc(%s): question for a person" % slug)
        st.update(self.root, slug, parked_phase=phase, stop=None)
        q = (self.doc(slug).section("Questions") if self.doc(slug) else "").strip()
        self.say("INPUT   %s — waits for your answer: %s" % (slug, q.splitlines()[0][:200] if q else "(see Questions)"))
        self.event("loop.item", "needs-input", q[:500], {"slug": slug, "phase": phase})
        return Outcome.PARKED

    def apply_answers(self, slug: str, rec: dict) -> None:
        d = self.doc(slug)
        if d is None:
            return
        q = d.section("Questions").rstrip()
        for a in rec.get("answers") or []:
            q += "\n\n**Answer** (%s): %s" % (str(a.get("ts", ""))[:10], a.get("text", ""))
        d.set_section("Questions", q.strip())
        d.status = "planned" if rec.get("parked_phase") == "build" else "ready"
        d.save()
        self.git("add", "--", os.path.relpath(self.doc_path(slug), self.root))
        self.git("commit", "-q", "--no-verify", "-m", "sdlc(%s): answer recorded" % slug)
        st.update(self.root, slug, answers=None, parked_phase=None)
        self.say("ANSWER  %s — applied; continuing" % slug)

    def ask_design_approval(self, slug: str) -> str:
        ok, url = self.publish(slug, draft=True)
        if not ok:
            return self.fail(slug, "could not open the design PR: %s" % url, count=False)
        if self.forge.approved(slug):
            st.update(self.root, slug, approved=True)
            return Outcome.ADVANCED
        st.update(self.root, slug, state="awaiting-approval", pr=url)
        self.say("REVIEW  %s — design PR %s waits for approval" % (slug, url))
        self.event("loop.item", "awaiting-approval", url, {"slug": slug, "pr": url})
        return Outcome.AWAITING

    def poll_team(self, items: Dict[str, Item]) -> None:
        """Team mode: notice approvals and merges done by people since the last look."""
        if self.forge.kind != "github":
            return
        for it in items.values():
            if it.phase not in TEAM_WAITS:
                continue
            if it.phase == "approval" and self.forge.approved(it.slug):
                st.update(self.root, it.slug, approved=True, state=None)
                it.phase, it.wait, it.wait_kind = "build", "", ""
                self.say("APPROVED %s — design approved; building" % it.slug)
            elif it.phase == "awaiting-merge":
                s = self.forge.state(it.slug)
                if s == "closed":
                    st.update(self.root, it.slug, state="closed")
                    it.phase, it.wait_kind = "closed", ""
                    self.say("CLOSED  %s — PR closed by a person" % it.slug)

    def poll_human(self, items: Dict[str, Item]) -> bool:
        """Items that wait for a person are out of the loop's hands. A person who merged the change left the doc `built`
        (or `done`) on the default branch, which the queue already reads as done: only the stale wait is cleared here.
        A merge that left the doc at another status (a blocked item's PR) is told by the PR (locally: the branch being
        merged); a PR a person closed closes the item. True when an item moved."""
        moved = False
        for it in items.values():
            gh = self.forge.kind == "github"
            if it.phase == "done" and it.rec.get("state") == "needs-human":
                st.update(self.root, it.slug, state="merged", reasons=None)
                self.say("MERGED  %s — merged by a person" % it.slug)
                self.event("loop.item", "merged-by-person", "", {"slug": it.slug})
                self.drop_merged_branch(it.slug)
                continue
            if it.phase == "needs-human" or (it.phase == "blocked" and gh):
                if gh and not it.rec.get("pr"):
                    continue                           # nothing was published for it, so there is no PR to have moved
                s = self.forge.state(it.slug)
                if s == "merged":
                    st.update(self.root, it.slug, state="merged", reasons=None)
                    self.say("MERGED  %s — merged by a person" % it.slug)
                    self.event("loop.item", "merged-by-person", "", {"slug": it.slug})
                    self.drop_merged_branch(it.slug)
                    moved = True
                elif s == "closed" and gh:
                    st.update(self.root, it.slug, state="closed", reasons=None)
                    self.say("CLOSED  %s — PR closed by a person" % it.slug)
                    self.event("loop.item", "closed-by-person", "", {"slug": it.slug})
                    moved = True
        return moved

    def drop_merged_branch(self, slug: str) -> None:
        """Without a remote nothing else removes the branch a person merged by hand (on GitHub the PR's merge does). Safe
        delete: a branch that is not merged into the checked-out one — a squash — stays, and a failure is left alone."""
        if self.forge.kind == "local" and self.git.branch() != "sdlc/" + slug:
            self.git("branch", "-d", "sdlc/" + slug)

    # ------------------------------------------------------------------ gate, ship
    def gate_phase(self, slug: str) -> str:
        rounds = 0
        while True:
            self.commit_all(slug, "sdlc(%s): changes left uncommitted" % slug)
            self.set_status(phase="gate", phase_started=now_iso())
            res = run_gate(self.cfg, self.git, slug, self.base())
            self.event("loop.gate", "pass" if res.passed else "fail", res.summary()[:500], dict(res.as_dict(), slug=slug))
            self.say("GATE    %s — %s" % (slug, res.summary()[:300]))
            if res.passed:
                self.mark_done(slug)
                return Outcome.ADVANCED
            if not res.fixable and not res.conflict:
                return self.needs_human(slug, res.human)
            if rounds >= self.fix_rounds:
                reasons = (["merge conflict with %s" % self.default] if res.conflict else []) + res.fixable + res.human
                if res.human:
                    return self.needs_human(slug, reasons)
                return self.fail(slug, "gate still failing after %d fix round(s): %s" % (rounds, "; ".join(reasons)[:300]))
            rounds += 1
            out = self.session(slug, "fix", extra=self.fix_brief(res), fix_round=rounds)
            if out != Outcome.ADVANCED:
                return out

    def fix_brief(self, res: GateResult) -> str:
        lines = ["The merge gate (run by the shell, not by you) failed on this branch:"]
        if res.conflict:
            lines.append("- `git merge %s` conflicts. Merge it into this branch, resolve the conflicts keeping both sides' "
                         "intent, run the tests, commit." % self.base())
        lines += ["- " + f for f in res.fixable]
        for name, out in res.outputs.items():
            lines.append("\n`%s` output (last lines):\n```\n%s\n```" % (name, out))
        if res.filled:
            lines.append("\nThe gate commands %s come from this branch's own `.sdlc/config.json` and are this project's gate from "
                         "now on. When one fails, fix the project, or the command line so that it really builds, tests or lints. "
                         "Never make it something that cannot fail (`true`, `exit 0`, `|| true`, an empty run)." %
                         ", ".join("`%s`" % n for n in res.filled))
        if res.human:
            lines.append("\nAlso reported, for a person (do not try to get around these): " + "; ".join(res.human))
        lines.append("\nFix the cause — never weaken, skip or delete a test. Update the `## Review` tally in the work doc if "
                     "you re-reviewed. Commit, then print SDLC_FIXED.")
        return "\n".join(lines)

    def mark_done(self, slug: str) -> None:
        d = self.doc(slug)
        if d is None or d.status == "done":
            return
        d.status = "done"
        d.save()
        self.git("add", "--", os.path.relpath(self.doc_path(slug), self.root))
        self.git("commit", "-q", "--no-verify", "-m", "sdlc(%s): gate passed" % slug)

    def publish(self, slug: str, draft: bool) -> Tuple[bool, str]:
        d = self.doc(slug)
        title = d.title if d else slug
        ok, url = self.forge.publish(slug, title, pr_body(self.cfg, d, slug, st.item(self.root, slug)), draft=draft)
        if ok and self.forge.kind == "github":
            st.update(self.root, slug, pr=url)
        return ok, url

    def checks_limit(self) -> float:
        """Seconds to wait for a PR's checks: `loop.checks_wait_minutes`, 0 for none. Read when needed — the config can change
        under a running loop."""
        try:
            return max(0.0, float(self.cfg.get("loop.checks_wait_minutes", 30) or 0)) * 60
        except (TypeError, ValueError):
            return 30 * 60.0

    def checks_timing(self, limit: float) -> Tuple[float, float]:
        """(seconds between looks, grace) for a wait of `limit` seconds. A PR that shows no check yet is waited on for the
        grace before it counts as having none: 2 minutes when the checkout has workflows (they are about to start), none
        when it has no CI — and never longer than the wait itself."""
        interval = self.checks_poll if self.checks_poll is not None else max(1.0, min(30.0, limit / 10))
        grace = self.checks_grace if self.checks_grace is not None else (120.0 if self.has_workflows() else 0.0)
        return interval, min(grace, limit)

    def has_workflows(self) -> bool:
        d = os.path.join(self.root, ".github", "workflows")
        try:
            return any(n.endswith((".yml", ".yaml")) and os.path.isfile(os.path.join(d, n)) for n in os.listdir(d))
        except OSError:
            return False

    def wait_checks(self, slug: str, interactive: bool = False) -> Tuple[str, List[Dict[str, str]]]:
        """GitHub, between the shell gate and the merge: wait for the PR's checks — CI runs there, not in this shell. Returns
        (state, checks): `pass` (also when no check shows up once the grace is over, and when there is nothing to wait for),
        `fail` (nothing pending any more — `gh run view --log-failed` refuses a run in progress, and a fix session should see
        every failure at once — or the limit came), `pending` (the limit came; the checks of the last look that could be
        read), `unknown` (the limit came and no look could be read: gh failing, or a PR that never showed this push),
        `merged` / `closed` (a person did it while waiting). A stop request raises StopRun: the doc stays `done`, the next
        run ships again."""
        limit = self.checks_limit()
        if self.forge.kind != "github" or limit <= 0:
            return "pass", []
        interval, grace = self.checks_timing(limit)
        if not interactive:                  # `sdlc.py ship` is no loop: the status file is a running loop's
            self.set_status(phase="checks", phase_started=now_iso())
        head = self.git.head()               # a PR that still shows another commit has not seen this push yet
        started, announced = time.time(), False
        seen: Optional[List[Dict[str, str]]] = None     # the checks of the last look that could be read

        def over(result: str, entries: List[Dict[str, str]], line: str = "", names: str = "", decision: str = ""
                 ) -> Tuple[str, List[Dict[str, str]]]:
            if line:
                self.say("CHECKS  %s — %s" % (slug, line))
            self.event("loop.checks", decision or result, names, {"slug": slug, "checks": entries,
                                                                  "waited_s": round(time.time() - started)})
            return result, entries

        while True:
            state, found = self.forge.checks(slug, head)
            waited = time.time() - started
            if state != "unknown":
                seen = found
            pending = check_names(found, "pending")
            if state == "unknown":           # no open PR to read is a PR somebody merged or closed
                gone = self.forge.state(slug)
                if gone in ("merged", "closed"):
                    return over(gone, [])
            if state == "pass":
                names = check_names(found)
                return over("pass", found, "passed: " + names, names)
            if state == "none" and waited >= grace:
                return over("pass", [], "no checks", decision="none")
            if state == "fail" and (not pending or waited >= limit):
                names = check_names(found, "fail")
                return over("fail", found, "failed: " + names, names)
            if waited >= limit:
                if seen is None:
                    return over("unknown", [])
                return over("pending", seen, names=check_names(seen, "pending"))
            if not announced:
                announced = True
                self.say("CHECKS  %s — waiting for %s" % (slug, pending or "checks to start"))
            wake = min(interval, limit - waited)
            if state == "none":
                wake = min(wake, grace - waited)
            if interactive:
                time.sleep(wake)
            elif not self.sleep(wake):
                raise StopRun(self.stopping or "paused")

    def update_branch(self, slug: str) -> bool:
        """GitHub will not merge a PR that is behind the default branch: merge the default branch into it here, hooks off like
        `adopt_doc`. False — and the merge undone — when it conflicts, which is for a person."""
        self.git("fetch", "-q", "origin", self.default, timeout=120)
        msg = "sdlc(%s): merge %s" % (slug, self.default)
        r = self.git("-c", "core.hooksPath=" + os.devnull, "merge", "--no-edit", "-m", msg, self.base())
        if not r.ok:
            self.git("merge", "--abort")
        return r.ok

    def settled_by_person(self, slug: str, state: str) -> str:
        """A person merged or closed the PR while the loop waited for its checks. Nothing is left to do for the loop — and it
        was not the loop's merge, so it counts toward no self-check."""
        if state == "merged":
            st.update(self.root, slug, state="merged", reasons=None, merged=now_iso(), ci_rounds=None)
            self.say("MERGED  %s — merged by a person" % slug)
            self.event("loop.item", "merged-by-person", "", {"slug": slug})
        else:
            st.update(self.root, slug, state="closed")
            self.say("CLOSED  %s — PR closed by a person" % slug)
            self.event("loop.item", "closed-by-person", "", {"slug": slug})
        return Outcome.SKIPPED

    def ship(self, slug: str, fix: bool = True) -> str:
        """The doc is `done`: push, open the PR, land it. Team: a person merges. Solo on GitHub the PR's checks come first —
        all pass: merge · one fails: `ci_failed` · still running at the limit: a person. A branch GitHub calls behind gets the
        default branch merged in, once. `fix` is False for an interactive ship, which hands a failure back to the session that
        called (Outcome.FIX) instead of running a fix session of its own."""
        d = self.doc(slug)
        title = d.title if d else slug
        ok, url = self.publish(slug, draft=False)
        if not ok:
            return self.fail(slug, "could not publish: %s" % url, count=False)
        if self.team:
            st.update(self.root, slug, state="awaiting-merge", reasons=None)
            self.say("READY   %s — gate passed; %s waits for a person to merge" % (slug, url))
            self.event("loop.item", "awaiting-merge", url, {"slug": slug, "pr": url})
            return Outcome.AWAITING
        updated = False
        while True:
            state, checks = self.wait_checks(slug, interactive=not fix)
            if state in ("merged", "closed"):
                return self.settled_by_person(slug, state)
            if state == "fail":
                return self.ci_failed(slug, checks, fix)
            if state == "unknown":
                return self.needs_human(slug, ["the PR's checks could not be read for %g min (`gh pr view` failed, or the PR "
                                               "never showed the pushed commit) — look at the PR, and merge it when its checks "
                                               "pass (the loop notices)" % (self.checks_limit() / 60)])
            if state == "pending":
                names = check_names(checks, "pending")
                return self.needs_human(slug, ["the PR's checks were still running after %g min%s — merge the PR when they pass "
                                               "(the loop notices), or raise loop.checks_wait_minutes" % (
                                                   self.checks_limit() / 60, " (%s)" % names if names else "")])
            ok, info = self.forge.merge(slug, title, tests_allowed_note(d))
            if ok or updated or self.forge.kind != "github" or self.forge.merge_state(slug) != "BEHIND":
                break
            updated = True
            if not self.update_branch(slug):
                return self.needs_human(slug, ["the branch is behind %s and merging it conflicts" % self.default])
            self.say("UPDATE  %s — behind %s; merged it in, waiting for the checks again" % (slug, self.default))
            ok, url = self.publish(slug, draft=False)
            if not ok:
                return self.fail(slug, "could not publish: %s" % url, count=False)
        if not ok:
            return self.needs_human(slug, ["merge did not happen: %s" % info])
        rec = st.update(self.root, slug, state=None, reasons=None, stop=None, merged=now_iso(), ci_rounds=None)
        self.say("MERGED  %s — %s ($%.2f)" % (slug, url if self.forge.kind == "github" else info[:12], float(rec.get("cost", 0) or 0)))
        self.event("loop.merge", "allow", title, {"slug": slug, "pr": url if self.forge.kind == "github" else "",
                                                   "sha": info if self.forge.kind == "local" else "", "cost_usd": rec.get("cost", 0),
                                                   "sessions": rec.get("sessions", 0), "mode": "auto"})
        return Outcome.MERGED

    def ci_failed(self, slug: str, checks: List[Dict[str, str]], fix: bool) -> str:
        """The PR's checks failed after the shell gate had passed: the change is not done. The doc goes back to `built`, so
        whatever fixes it ships through the gate again. An interactive ship hands the brief to the session that called
        (Outcome.FIX, the caller's own round limit applies); the loop runs a fix session with it — up to `loop.fix_rounds`
        of them, counted across gates in the item's `ci_rounds` — and a person gets the item after that. A stop or pause
        while the brief waits for a log raises StopRun before the round is counted."""
        failed = [c for c in checks if c.get("bucket") == "fail"]
        d = self.doc(slug)
        if d is not None and d.status != "built":
            d.status = "built"
            d.save()
            self.git("add", "--", os.path.relpath(self.doc_path(slug), self.root))
            self.git("commit", "-q", "--no-verify", "-m", "sdlc(%s): PR checks failed — back to built" % slug)
        if not fix:
            self.last_brief = self.ci_brief(failed, interactive=True)
            return Outcome.FIX
        rounds = int(st.item(self.root, slug).get("ci_rounds", 0) or 0)
        if rounds >= self.fix_rounds:
            names = check_names(failed)
            return self.needs_human(slug, ["the PR's checks still fail after %d fix round(s): %s" % (rounds, names) if self.fix_rounds
                                           else "the PR's checks failed: %s" % names])
        brief = self.ci_brief(failed)        # raises StopRun when a stop or pause came while a log was waited for
        st.update(self.root, slug, ci_rounds=rounds + 1)
        return self.session(slug, "fix", extra=brief, fix_round=rounds + 1)

    def ci_brief(self, failed: List[Dict[str, str]], interactive: bool = False) -> str:
        """What a fix session is told when the PR's checks failed: which, where to read them, the end of each failed job's log
        (cut before the job's cleanup, without the per-line prefix) — within CI_BRIEF_CAP characters, the logs giving way
        first. A log that is not there yet is waited for like the checks are: with the loop's interruptible sleep, and a stop
        or pause meanwhile stops the run (StopRun) — the doc is `built` already, so the next run gates and ships it again.
        An interactive ship waits with a plain sleep that nothing stops."""
        lines = ["The PR's checks failed on GitHub after the shell gate had passed (they run on GitHub, not here):"]
        for c in failed[:12]:
            wf = " (workflow %s)" % c["workflow"][:80] if c.get("workflow") else ""
            lines.append("- `%s`%s%s" % ((c.get("name") or "check")[:120], wf, " — " + c["link"][:240] if c.get("link") else ""))
        if len(failed) > 12:
            lines.append("- … and %d more" % (len(failed) - 12))
        foot = ("\nReproduce it locally with the command the workflow runs, then fix the cause — never weaken, skip or delete a "
                "test, and never edit `.github/workflows/` to get green. Commit, then print SDLC_FIXED. A failure that is not "
                "this change's (the CI environment, a missing secret, a flaky external service) cannot be fixed from here: print "
                "SDLC_FIX_BLOCKED <why> instead.")
        stopped = False

        def wait(seconds: float) -> bool:
            nonlocal stopped
            stopped = not self.sleep(seconds)
            return not stopped
        logs = self.forge.check_logs(failed, wait=None if interactive else wait)
        if stopped:
            raise StopRun(self.stopping or "paused")
        head = "\n".join(lines)
        if logs:
            fence = "```"
            while fence in logs:
                fence += "`"
            room = CI_BRIEF_CAP - len(head) - len(foot) - 2 * len(fence) - 60        # 60: the lead-in and the line breaks
            if room > 200:
                head += "\n\nThe end of what they printed:\n%s\n%s\n%s" % (fence, end_of(logs, room), fence)
        return head + "\n" + foot

    def ship_now(self, slug: str) -> Tuple[int, str]:
        """`sdlc.py ship <slug>` — the end of an interactive /sdlc:go: the same gate, PR-checks wait and merge the loop uses,
        without fix sessions (the session that called it fixes and calls again). 0 shipped/handed over · 1 fix needed — the
        gate's, or the PR's failed checks (the doc is back to `built`) · 2 a person."""
        b = "sdlc/" + slug
        if self.git.branch() != b:
            return 1, "check out %s first (now on %s)" % (b, self.git.branch() or "a detached HEAD")
        dirty = self.git.dirty()
        if dirty:
            return 1, "commit your changes first: " + dirty_summary(dirty)
        s = self.doc_status(slug)
        if s not in ("built", "done"):
            return 1, "work/%s.md says '%s' — set status: built (with the review tally) when the change is ready" % (slug, s)
        if not self.claims.take(slug):
            return 2, "another loop is working on %s" % slug
        try:
            if s == "built":
                res = run_gate(self.cfg, self.git, slug, self.base())
                self.event("loop.gate", "pass" if res.passed else "fail", res.summary()[:500], dict(res.as_dict(), slug=slug))
                if not res.passed:
                    if res.fixable or res.conflict:
                        return 1, self.fix_brief(res)
                    self.needs_human(slug, res.human)
                    return 2, "a person has to look at this: " + "; ".join(res.human)
                self.mark_done(slug)
            out = self.ship(slug, fix=False)
            rec = st.item(self.root, slug)
            if out == Outcome.FIX:
                return 1, self.last_brief
            if out == Outcome.MERGED:
                return 0, "merged" + (" — " + str(rec.get("pr")) if rec.get("pr") else "")
            if out == Outcome.AWAITING:
                return 0, "gate passed — %s waits for a person to merge" % (rec.get("pr") or b)
            if out == Outcome.SKIPPED and rec.get("state") == "merged":
                return 0, "merged by a person — %s" % (rec.get("pr") or b)
            if out == Outcome.SKIPPED and rec.get("state") == "closed":
                return 2, "the PR was closed by a person"
            return 2, "; ".join(rec.get("reasons") or ["not merged"])
        finally:
            self.claims.release()

    # ------------------------------------------------------------------ self-check
    def self_check_due(self) -> bool:
        """Solo only: it reviews what this loop merged itself, and in team mode a person merges. One merged item is not
        reviewed again here — its own review covered the whole diff — so it takes `loop.self_check_min_items` of them (the
        run says so once when it skips for that)."""
        if self.team or not bool(self.cfg.get("loop.self_check", True)) or self.self_checks >= 2 or self.only \
                or not self.merged_since_check or self.git.head() == self.from_sha:
            return False
        try:
            need = max(1, int(self.cfg.get("loop.self_check_min_items", 2)))
        except (TypeError, ValueError):
            need = 2
        n = len(self.merged_since_check)
        if n < need:
            if not self.skip_noted:
                self.skip_noted = True
                self.say("CHECK   skipped — %s (loop.self_check_min_items %d)" % (
                    "1 item merged since the last check; its own review covered it" if n == 1 else
                    "%d items merged since the last check; their own reviews covered them" % n, need))
            return False
        return True

    def self_check(self) -> int:
        """Review what merged in this run; validated Important findings on changed lines become new items. Returns how many were
        queued, ready or waiting for a person: on GitHub the queue PR is merged on the remote and not pulled while this loop runs,
        so until the default branch is brought up to date neither the local branch nor origin's ref shows them."""
        self.self_checks += 1
        self.merged_since_check = []
        self.to_default()
        head = self.git.head()
        rng = "%s..%s" % (self.from_sha[:12], head[:12])
        model = self.model_for("self_check")
        effort, effort_why = self.effort_for("self_check")
        self.say("CHECK   reviewing what merged (%s) (%s)" % (rng, session_note(model, effort, effort_why)))
        self.set_status(state="running", item="", phase="self-check", phase_started=now_iso(), model=model, effort=effort)
        before = set(self.git.out("ls-files", "--others", "--exclude-standard").splitlines())
        log = os.path.join(self.root, ".sdlc", "logs", "sessions", "%s-self-check.jsonl" % stamp())
        res = self.runner.run("/sdlc:go --self-check %s" % rng, log, min(self.session_minutes, 60), model, effort)
        self.cost += res.cost_delta
        self.event("loop.session", res.status, "self-check", {"slug": "", "phase": "self-check", "sid": res.sid,
                                                              "cost_usd": res.cost_delta, "turns": res.turns,
                                                              "log": os.path.relpath(log, self.root), "effort": effort,
                                                              "effort_reason": effort_why, "continued": False})
        self.from_sha = head
        work = self.cfg.rel("work")
        # the self-check writes work docs only: anything else it touched is put back
        for line in self.git("status", "--porcelain").out.splitlines():
            path = line[3:].strip().strip('"')
            if path.startswith(work + "/") or path.startswith(".sdlc/"):
                continue
            if line.startswith("??"):
                if path not in before:
                    fp = os.path.join(self.root, path)
                    if os.path.isfile(fp):
                        os.remove(fp)
            else:
                self.git("checkout", "--", path)
        paths = [os.path.join(self.root, l[3:].strip().strip('"')) for l in
                 self.git("status", "--porcelain", "--untracked-files=all", "--", work + "/").out.splitlines()
                 if l.startswith("??") and l.strip().endswith(".md")]
        if not paths:
            self.say("CHECK   clean")
            return 0
        cap = int(self.cfg.get("loop.self_check_max_items", 3) or 3)
        kept = []
        for p in paths:
            d = WorkDoc.load(p)
            if d is None or problems(d, os.path.splitext(os.path.basename(p))[0], work):
                if os.path.exists(p):
                    os.remove(p)
                continue
            d.meta["source"] = "self-check"
            if d.status == "ready" and cap > 0:
                cap -= 1
            else:
                d.status = "proposed"      # over the cap, or a pre-existing problem: a person decides
            d.save()
            kept.append(p)
        paths = kept
        if not paths:
            self.say("CHECK   nothing usable")
            return 0
        try:
            docs = prepare(self.cfg, self.git, self.base(), paths, source="self-check")
            enqueue(self.cfg, self.git, self.forge, docs, paths, from_loop=True)
        except EnqueueError as e:
            self.say("CHECK   findings could not be queued: %s" % str(e)[:300])
            for p in paths:
                if os.path.exists(p):
                    os.remove(p)
            return 0
        ready = [d.slug for d in docs if d.status == "ready"]
        self.say("CHECK   %d new item(s): %s" % (len(docs), ", ".join("%s (%s)" % (d.slug, d.status) for d in docs)))
        self.event("self_check", "allow", rng, {"items": [d.slug for d in docs], "ready": ready})
        return len(docs)


def _after_setting(line: str) -> str:
    """`test_changes: allowed — why`, `**test_changes:** allowed: why` and `` `test_changes` allowed — why`` give `why`. Markup
    the mention opened is closed with it; markup that starts the reason (a code span naming the test) is the reason's own."""
    m = re.match(r"([*_`]*)test_changes", line, re.I)
    if not m:
        return line
    opened = list(m.group(1))

    def close(text: str) -> str:
        while text and text[0] in opened:
            opened.remove(text[0])
            text = text[1:]
        return text

    after = line[m.end():]
    rest = close(after)
    sep = re.match(r"\s*[:=]", rest)
    if not sep and rest == after:
        return line                                  # not the setting spelled out: the whole line is the reason
    if sep:
        rest = close(rest[sep.end():].lstrip())
    return re.sub(r"^\s*(?:[*_`]*allowed[*_`]*)?[\s,;:—–-]*", "", rest, flags=re.I)


def check_names(entries: List[Dict[str, str]], bucket: str = "", cap: int = 6) -> str:
    """The names of the PR's checks (of one bucket), comma-separated, at most `cap` of them: `build, test, lint, …`."""
    names = [str(c.get("name") or "check") for c in entries if not bucket or c.get("bucket") == bucket]
    return ", ".join(names[:cap]) + (", …" if len(names) > cap else "")


def tests_allowed_note(d: Optional[WorkDoc]) -> str:
    """One line for the PR body and the merge commit when the design let this item change existing tests — the reason is
    the first line of the Plan, Departures or Proof that speaks of `test_changes` or existing tests. Empty otherwise."""
    if d is None or str(d.meta.get("test_changes", "")).strip() != "allowed":
        return ""
    why = ""
    for name in ("Plan", "Departures", "Proof"):
        for raw in re.sub(r"<!--.*?-->", "", d.section(name), flags=re.S).splitlines():
            line = re.sub(r"^(?:[>+-]\s*|\*\s+|\d+[.)]\s+)+", "", raw.strip()).strip()     # list bullets and numbers, not emphasis
            if re.search(r"test_changes|existing tests?", line, re.I):
                line = _after_setting(line).strip()
                if line:
                    why = line[:200]
                    break
        if why:
            break
    return "Existing tests were allowed to change in this item (test_changes: allowed)" + (" — " + why if why else ".")


def pr_body(cfg: Config, d: Optional[WorkDoc], slug: str, rec: dict) -> str:
    if d is None:
        return "sdlc work item `%s`" % slug
    parts = []
    note = tests_allowed_note(d)
    if note:
        parts.append("> " + note)
    for name in ("Goal", "Acceptance criteria", "Proof", "Review", "Departures"):
        body = d.section(name).strip()
        if body:
            parts.append("## %s\n\n%s" % (name, body))
    parts.append("---\n`%s/%s.md` · status `%s` · sdlc %s · cost so far $%.2f" % (
        cfg.rel("work"), slug, d.status, plugin_version(), float(rec.get("cost", 0) or 0)))
    return "\n\n".join(parts)


def all_docs(cfg: Config) -> List[WorkDoc]:
    return list_docs(cfg.path("work"))


def loop_log_tail(root: str, lines: int = 20) -> str:
    s = st.read_status(root)
    p = s.get("log")
    return tail(read_text(os.path.join(root, str(p))) or "", lines) if p else ""
