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
The item's branch is never overwritten: what origin has that the checkout lacks is fast-forwarded in (or, when both moved, a person's),
every push is leased on the commit the loop last saw there, and a merge is matched to the commit that was pushed. On GitHub an
item is also claimed on origin, so loops in separate clones do not take the same one.
A session stopped by a cap continues at once (warm cache, `--resume`); a stop older than `cold_resume_minutes` continues
in a fresh session; a stop without progress since the last one is a failure. Usage limits: a model-family limit switches
model, a session/weekly limit is slept out when it resets within `limit_wait_hours`, else the run stops (resumable).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import time
import traceback
import uuid
from typing import Dict, List, Optional, Tuple

from . import state as st
from .config import PHASES, Config, NotInitialized
from .effort import LEVELS, doc_difficulty, shift
from .enqueue import EnqueueError, departure_reason, drain_inbox, enqueue, prepare, record_drop
from .events import log_event
from .gate import GateResult, run_gate
from .gitops import NOISE_EXCLUDES, STALE_PUSH, Forge, Git, dirty_summary, end_of, is_noise, make_forge, remote_host
from .queue import FINISHED, HUMAN, RUNNABLE, Item, TEAM_WAITS, runnable, scan, waiting
from .session import Runner, SessionResult
from .util import epoch, now, now_iso, plugin_version, read_text, run, say, stamp, tail
from .workdoc import WorkDoc, list_docs, problems

CI_BRIEF_CAP = 8000     # characters of what a fix session is told about failed PR checks
# what references/fix.md says to a fix session about reviewing its own commits and about a failure that is not the change's
FIX_REVIEW_RULE = ("Start from `git rev-parse HEAD`, the commit before the fix. When the fix touches non-test code beyond the "
                   "failing lines or the conflict markers, nobody has reviewed that code: run the review "
                   "(references/review.md steps 2–4, through the `sdlc:sdlc-reviewer` agent) on `git diff <that commit>..HEAD` "
                   "and append its tally line to `## Review`.")
FIX_NOT_YOURS = ("A command that already fails or flakes on the default branch blocks this gate too: fix it here with a "
                 "`## Departures` line, or — when it is not this change's to fix — write why under `## Departures`, commit, and "
                 "print SDLC_FIX_BLOCKED <why> instead of SDLC_FIXED.")
CLAIM_REFRESH = 300.0   # seconds between two renewals of the claim on origin while the loop waits for PR checks
NAMES_SHOWN = 6         # item names a stop reason lists per part, before "…"
FAMILIES = ("opus", "sonnet", "haiku", "fable")
INFRA_HINTS = ("overloaded", "api error: 5", "internal server error", "service unavailable", "bad gateway",
               "gateway timeout", "econnreset", "etimedout", "socket hang up", "network error", "fetch failed")


def filled(text: str) -> bool:
    """A section with something in it: the template's HTML-comment hints and whitespace do not count."""
    return bool(re.sub(r"<!--.*?-->", "", text or "", flags=re.S).strip())


def digest(text: str) -> str:
    """A short fingerprint of a text — what tells a stopped session's brief from the one the loop has now."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


class StopRun(Exception):
    def __init__(self, reason: str, code: int = 0):
        super().__init__(reason)
        self.reason, self.code = reason, code


class AlreadyRunning(StopRun):
    """Another loop owns this checkout: its status file is the record that matters, so this refusal leaves it alone."""


class SkipItem(Exception):
    """This item cannot be worked on right now (its branch is checked out in another worktree): left for the rest of the pass."""


class NeedsPerson(Exception):
    """A person has to look before the loop touches the item's branch again (origin and this checkout both moved)."""


class Outcome:
    MERGED, AWAITING, PARKED, FAILED, INFRA, HUMAN, ADVANCED, SKIPPED, FIX, HELD = (
        "merged", "awaiting", "parked", "failed", "infra", "needs-human", "advanced", "skipped", "fix", "held")   # FIX: `ship_now` only
    # HELD: another loop (or a person's worktree) has the item — no failure, skipped for the rest of the pass


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
                             isolate=bool(L("isolate_sessions", False)), drop_env=(self.cfg.deploy_approval_env,))
        self.claims = st.Claims(self.git.common_dir(), root)
        self.log_path = os.path.join(root, ".sdlc", "logs", "run-%s.log" % stamp())
        self.stopping = ""            # set by a signal: finish nothing more, stop the session gracefully
        self.started = epoch()
        self.cost = 0.0
        self.done: Dict[str, List[str]] = {"merged": [], "awaiting": [], "parked": [], "failed": [], "needs-human": []}
        self.failed_now: List[str] = []
        self.held_now: List[str] = []      # items another loop holds (a claim, a worktree): skipped until the next look
        self.token = uuid.uuid4().hex      # who this run is, on origin's claims
        self.remote_claim = ""             # the slug whose claim on origin this run holds
        self.claim_refresh = CLAIM_REFRESH
        self.claim_warned = False          # "could not take the claim on origin" is said once per run
        self.fetch_warned = False          # so is "fetch failed"
        self.self_limit_hits = 0
        self.items_run = 0
        self.consec = 0
        self.blocked_family: Dict[str, float] = {}   # family → epoch until which it is limited
        self.from_sha = ""
        self.self_checks = 0
        self.merged_since_check: List[str] = []   # slugs this loop merged since the run began or the last self-check
        self.skip_noted = False                   # the "self-check skipped" line is said once per run
        self.self_check_misses = 0                # self-checks in a row that failed for another reason than a limit: a third is not tried
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
            self.check_selected()
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
                    order = runnable(items, self.only,
                                     skip=self.failed_now + self.held_now + [s for s in items if self.claims.owner(s)])
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
                        self.held_now = []                    # whoever held them may be done by the time this look is over
                        if not self.sleep(self.poll):
                            continue
                        if self.forge.kind == "github":
                            self.git.fetch()
                        continue
                    stop = self.end_reason(items)
                    break
                self.items_run += 1
                out = self.guarded_work(it)
                if out == Outcome.HELD:
                    self.items_run -= 1                       # nothing was run for it: it does not count toward max_items
                else:
                    idle_since = 0.0                          # (a look at an item somebody holds is no work: the idle limit runs on)
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
        busy = {s for s in items if self.claims.owner(s) and items[s].phase not in FINISHED}
        out = [(s, "worked on by another loop") for s in sorted(busy)]
        out += [(s, "held by another loop") for s in sorted(set(self.held_now) - busy) if s in items and items[s].phase not in FINISHED]
        team = {s for s, it in items.items() if it.wait_kind == "poll"}
        out += [(s, items[s].wait) for s in sorted(team)]
        for s, it in sorted(items.items()):
            if it.wait_kind == "dep" and (it.blocker in busy or it.blocker in team):
                out.append((s, it.wait))
        return out

    def check_selected(self) -> None:
        """`--items` names work docs: a name that is none is a typo, and a run that took it for "nothing to do" would hide that.
        What is on origin counts (it is fetched first, tolerantly) and so do docs parked in the inbox."""
        if not self.only:
            return
        if self.forge.kind == "github":
            self.git.fetch()
        known = set(self.scan())
        try:
            known |= {n[:-3] for n in os.listdir(os.path.join(self.root, st.INBOX_REL)) if n.endswith(".md")}
        except OSError:
            pass
        unknown = [s for s in self.only if s not in known]
        if unknown:
            raise StopRun("no work doc named %s" % ", ".join(unknown), 2)

    def end_reason(self, items: Dict[str, Item]) -> str:
        """Why nothing more can run, in what is true now. Over the whole queue — or, with `--items`, over the selected items:
          queue empty                 nothing left in the queue (no `--items`);
          selected items finished     every selected item is merged, awaiting a person's merge, or dropped;
          nothing runnable — …        what is left and why: N wait for you (…); N wait on other items (…); N failed this run (…)
                                      — the next run retries them; N held by another loop (…).
        With `--items` ` · N other item(s) not selected` is added when the queue has more."""
        chosen = set(self.only) if self.only is not None else None
        pool = {s: it for s, it in items.items() if chosen is None or s in chosen}
        left = {s: it for s, it in pool.items() if it.phase not in FINISHED}
        more = [s for s, it in items.items() if chosen is not None and s not in chosen and it.phase not in FINISHED]
        tail = " · %d other item(s) not selected" % len(more) if more else ""
        if chosen is None and not left:
            return "queue empty"
        if chosen is not None and all(it.phase == "awaiting-merge" for it in left.values()):
            return "selected items finished" + tail
        you = sorted(s for s, it in left.items() if it.phase in HUMAN or it.phase in TEAM_WAITS)
        deps = sorted(s for s, it in left.items() if s not in you and it.blocker)
        held = sorted(s for s, it in left.items() if s not in you + deps and (s in self.held_now or self.claims.owner(s)))
        failed = sorted(s for s, it in left.items() if s not in you + deps + held and s in self.failed_now and it.phase in RUNNABLE)

        def names(slugs: List[str]) -> str:
            return ", ".join(slugs[:NAMES_SHOWN]) + (", …" if len(slugs) > NAMES_SHOWN else "")
        parts = []
        if you:
            parts.append("%d wait for you (%s)" % (len(you), names(you)))
        if deps:
            parts.append("%d wait on other items (%s)" % (len(deps), names(deps)))
        if failed:
            parts.append("%d failed this run (%s) — the next run retries them" % (len(failed), names(failed)))
        if held:
            parts.append("%d held by another loop (%s)" % (len(held), names(held)))
        return "nothing runnable" + (" — " + "; ".join(parts) if parts else "") + tail

    def guarded_work(self, it: Item) -> str:
        """`work`, with one net under it: an answer from a forge or a tool that is not shaped as expected (a missing key, a
        value of the wrong type) fails the item and is logged in full, it does not end the run with a traceback."""
        try:
            return self.work(it)
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            self.say("ERROR   %s — %s: %s (the traceback is in the run log)" % (it.slug, type(e).__name__, e))
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc())
            except OSError:
                pass
            return self.fail(it.slug, "unexpected %s: %s" % (type(e).__name__, e))

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
        if out == Outcome.HELD:
            if slug not in self.held_now:
                self.held_now.append(slug)
            return
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
        went = self.git.to_default(self.default)
        self.warn_fetch()
        if not went:
            raise StopRun(self.git.with_reason("cannot check out or fast-forward " + self.default), 2)
        self.refresh_config()

    def warn_fetch(self) -> None:
        """A fetch that failed (the network is down for a minute) is a warning, said once per run: the loop goes on with what is
        here and fetches again at its next look."""
        warning = self.git.last_warning
        if not warning or self.fetch_warned:
            return
        self.fetch_warned = True
        lead = "fetch from origin failed, went on with what is here"
        reason = warning[len(lead):].lstrip(": ").strip() if warning.startswith(lead) else warning
        self.say("WARN    fetch failed: %s; going on with what is here" % (reason or "no reason given"))

    def refresh_config(self) -> None:
        """`.sdlc/config.json` changes on the default branch while the loop runs — a skeleton item fills in `commands.*` and
        this very loop merges it — and the next item must be gated by the file as it is now, not as the run began."""
        try:
            fresh = Config(self.root)
        except NotInitialized:
            return                                   # unreadable at this moment: keep what the run has
        if fresh.raw != self.cfg.raw:
            self.cfg = fresh
            self.runner.drop_env = tuple(n for n in (fresh.deploy_approval_env,) if n)
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
        """Be on sdlc/<slug> with what origin has of it. SkipItem when another worktree has the branch checked out; NeedsPerson
        when origin and this checkout both hold commits the other lacks. A branch cut before the item's work doc existed, for
        an item still `ready`, is not built on: it is kept as sdlc-old/<slug> and the item starts again from the default
        branch (a `planned` item adopts the doc instead, `adopt_doc`)."""
        b = "sdlc/" + slug
        fresh = False
        if self.git.branch() != b:
            other = self.git.worktree_of(b)
            if other and os.path.realpath(other) != os.path.realpath(self.root):
                raise SkipItem("checked out in %s" % other)
            old = self.branch_before_doc(slug)
            if old:
                self.start_over(slug, old)
                fresh = True
            else:
                if self.git.branch_exists(b):
                    r = self.git("checkout", "-q", b)
                elif self.git.remote_branch_exists(b):
                    r = self.git("checkout", "-q", "-b", b, "--track", "origin/" + b)
                else:
                    r = self.git("checkout", "-q", "-b", b, self.base())
                if not r.ok:
                    raise StopRun("cannot check out %s: %s" % (b, r.text()[-200:]), 2)
        if not fresh:
            why = self.sync_remote(slug)
            if why:
                raise NeedsPerson(why)
        self.adopt_doc(slug)

    def branch_before_doc(self, slug: str) -> str:
        """The ref of an existing sdlc/<slug> (local, else origin's) that does not hold the item's work doc while the default
        branch holds it with status `ready`: a branch from before the item was queued (0.x, an earlier slug of the same name).
        "" when there is none."""
        b = "sdlc/" + slug
        if self.git.branch_exists(b):
            ref = "refs/heads/" + b
        elif self.git.remote_branch_exists(b):
            ref = "refs/remotes/origin/" + b
        else:
            return ""
        rel = os.path.relpath(self.doc_path(slug), self.root)
        if self.git.show(ref, rel) is not None:
            return ""
        queued = self.git.show(self.base(), rel)
        return ref if queued is not None and WorkDoc.parse(queued).status == "ready" else ""

    def start_over(self, slug: str, ref: str) -> None:
        """Keep the old branch as sdlc-old/<slug> and cut sdlc/<slug> again from the default branch. What origin holds of the
        old one is remembered as the commit the first push replaces (it is leased on that commit), and the design session is
        told where the earlier work is."""
        b, old = "sdlc/" + slug, "sdlc-old/" + slug
        n = 1
        while self.git.branch_exists(old):
            n += 1
            old = "sdlc-old/%s-%d" % (slug, n)
        r = self.git("branch", "-m", b, old) if self.git.branch_exists(b) else self.git("branch", old, ref)
        if not r.ok:
            raise StopRun("cannot keep the older %s as %s: %s" % (b, old, r.text()[-200:]), 2)
        r = self.git("checkout", "-q", "-b", b, self.base())
        if not r.ok:
            raise StopRun("cannot check out %s: %s" % (b, r.text()[-200:]), 2)
        remote = self.git.remote_sha(b) if self.forge.kind == "github" else ""
        st.update(self.root, slug, old_branch=old, remote_sha=remote, replaces=remote or None)
        self.say("FRESH   %s — %s was cut before its work doc existed; kept as %s, starting again from %s%s" % (
            slug, b, old, self.base(), " (the first push replaces origin's, which is at %s)" % remote[:10] if remote else ""))

    def sync_remote(self, slug: str) -> str:
        """What origin holds of sdlc/<slug> and this checkout lacks comes in before anything is pushed: a person's fix on a
        needs-human PR, a reviewer's commit on the design PR. This checkout behind origin's: fast-forward. Ahead or equal: fine.
        Both moved: "" is not returned — the reason a person has to see is. The commit origin's branch is at is recorded as
        `remote_sha` ("" when it does not exist there) — what every push of the loop is leased on. GitHub only."""
        if self.forge.kind != "github":
            return ""
        b = "sdlc/" + slug
        remote = self.git.remote_sha(b)
        if not remote:
            st.update(self.root, slug, remote_sha="")
            return ""
        if remote == st.item(self.root, slug).get("replaces"):
            # a branch that was cut again (`start_over`): origin still holds only the old work, which the first push replaces —
            # it is not a divergence to settle, and nothing of it comes in. Origin moving past it is somebody's push (below).
            st.update(self.root, slug, remote_sha=remote)
            return ""
        mine = "refs/heads/" + b
        how = self.git.relation(mine, "refs/remotes/origin/" + b)
        if how == "diverged":
            return ("%s on origin has commits this checkout does not have, and the other way round — merge them by hand, "
                    "then retry" % b)
        if how == "behind":
            if self.git.branch() != b:
                return "%s is behind origin's and is not checked out here, so it cannot be fast-forwarded" % b
            n = self.git.out("rev-list", "--count", "%s..%s" % (mine, remote))
            if not self.git("merge", "--ff-only", "-q", remote).ok:
                return self.git.with_reason("cannot fast-forward %s to origin's (%s)" % (b, remote[:10]))
            self.say("SYNC    %s — took %s commit(s) from origin's %s (%s)" % (slug, n or "?", b, remote[:10]))
            self.regate(slug)
        st.update(self.root, slug, remote_sha=remote)
        return ""

    def regate(self, slug: str) -> None:
        """Commits came in from origin onto a `done` item — a person's fix after a merge that failed, say. The gate passed
        without them, so the doc goes back to `built` and the gate runs again before anything is shipped."""
        d = self.doc(slug)
        if d is None or d.status != "done":
            return
        d.status = "built"
        d.save()
        self.git("add", "--", os.path.relpath(self.doc_path(slug), self.root))
        self.git("commit", "-q", "--no-verify", "-m", "sdlc(%s): back to built — commits came in from origin" % slug)
        self.say("REGATE  %s — what came in from origin was not gated: back to built" % slug)

    # ------------------------------------------------------------------ claims on origin
    def take_remote_claim(self, slug: str) -> str:
        """Hold slug on origin so that a loop in another clone does not take it too (GitHub only). "" when it is ours — or when
        origin could not be asked, which is said once per run and goes on without; else who holds it, as the claim says:
        `<git user> in <folder> since <time>`."""
        if self.forge.kind != "github":
            return ""
        got = self.git.claim(slug, self.token, stale_seconds=2 * self.session_minutes * 60)
        if got.startswith("other:"):
            return got[len("other:"):]
        if got.startswith("error:"):
            if not self.claim_warned:
                self.claim_warned = True
                self.say("WARN    could not take the claim on origin (%s); going on without it" % (got[len("error:"):] or "no reason"))
            return ""
        self.remote_claim = slug
        return ""

    def refresh_remote_claim(self, slug: str) -> None:
        """Renew our claim (its time is what says it is not stale). One that is gone is taken again; one somebody else has taken
        meanwhile is said, and the work goes on — it was not ours to take away, nor is it ours to give up halfway."""
        if self.remote_claim != slug or self.git.refresh_claim(slug, self.token):
            return
        holder = self.take_remote_claim(slug)
        if holder:
            self.remote_claim = ""
            self.say("WARN    %s — origin's claim is %s's now; going on" % (slug, holder))

    def release_remote_claim(self, slug: str) -> None:
        if self.remote_claim == slug:
            self.remote_claim = ""
            self.git.release_claim(slug, self.token)

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
        self.release_remote_claim(slug)
        self.claims.release()

    # ------------------------------------------------------------------ one item
    def work(self, it: Item) -> str:
        slug = it.slug
        if not self.claims.take(slug):
            self.say("SKIP    %s — held by another loop in this repository" % slug)
            return Outcome.HELD
        holder = self.take_remote_claim(slug)
        if holder:
            self.claims.release()
            self.say("SKIP    %s — held by %s" % (slug, holder))
            return Outcome.HELD
        self.say("ITEM    %s — %s  [%s]" % (slug, it.title, it.phase))
        self.set_status(state="running", item=slug, title=it.title, phase=it.phase, phase_started=now_iso(), waiting=[],
                        model="", effort="")
        try:
            if it.phase == "merge" and not it.branch:
                return self.fail(slug, "status done on no branch — nothing to merge", count=False)
            try:
                self.checkout_item(slug)
            except SkipItem as e:
                self.say("SKIP    %s — %s" % (slug, e))
                return Outcome.HELD
            except NeedsPerson as e:
                return self.needs_human(slug, [str(e)], push=False)
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
        for the first fix session after a gate failure, 2 for the next — a later round gets more effort. A design or build
        session starts on a branch that has the default branch in it (`catch_up_brief`), and a stopped session that is
        resumed with a brief other than the one it began with is told the new one."""
        before_status = self.doc_status(slug)
        before_head = self.git.head()
        rec = st.item(self.root, slug)
        stop = rec.get("stop") or {}
        extra = self.catch_up_brief(slug, phase) + extra
        if phase == "design" and rec.get("old_branch"):
            extra += ("\n\nAn older branch `%s` has earlier work on this: `git log %s..%s`; reuse what fits." % (
                rec["old_branch"], self.base(), rec["old_branch"]))
        extra = extra.strip("\n")
        extra_fp = digest(extra)
        if phase == "build" and "review_seen" not in rec and stop.get("phase") != "build":
            # a build this version starts: false until a window of this item shows the reviewer subagent being called. A build
            # that an older version stopped (`stop` in its record, no `review_seen` — it never wrote one) is continued with the
            # key left unknown, which the gate does not hold against it: the review may well have run before the stop.
            st.update(self.root, slug, review_seen=False)
        resume_sid, cont_reason, last_fp = "", "", ""
        if stop and stop.get("phase") == phase:
            last_fp = str(stop.get("fp") or "")
            cont_reason = str(stop.get("why") or "stopped")
            if epoch() - float(stop.get("at") or 0) < self.cold_seconds and stop.get("model") == self.model_for(phase, slug):
                resume_sid = str(stop.get("sid") or "")
        new_brief = bool(resume_sid and extra and str(stop.get("extra") or "") != extra_fp)   # the session never saw this one
        continuations = 0
        infra_retries = 0
        limit_hits = 0
        while True:
            model = self.model_for(phase, slug)
            effort, effort_why = self.effort_for(phase, slug, fix_round)       # per window: the doc, the failures may have changed
            if resume_sid:
                prompt = ("Continue where you stopped (%s): finish the %s phase as /sdlc:go --item %s --%s describes, "
                          "then print the marker." % (cont_reason, phase, slug, phase))
                if new_brief:
                    prompt += "\n\nWhat has to be done now is not what you started with. The brief is:\n\n" + extra
                    new_brief = False
            else:
                prompt = self.prompt(slug, phase, extra, cont_reason)
            log = os.path.join(self.root, ".sdlc", "logs", "sessions", "%s-%s-%s.jsonl" % (stamp(), slug, phase))
            self.set_status(phase=phase, phase_started=now_iso(), session_log=os.path.relpath(log, self.root), model=model,
                            effort=effort)
            self.say("%-7s %s — %s%s (%s)" % (phase.upper()[:7], slug, "resuming " if resume_sid else "",
                                              "session" if not cont_reason or resume_sid else "fresh session after a stop",
                                              session_note(model, effort, effort_why)))
            st.set_active(self.root, slug)
            self.refresh_remote_claim(slug)
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
                if phase == "design" and self.doc_status(slug) == "planned":
                    st.update(self.root, slug, design_sha=self.git.head())      # the design as decided: the gate holds the build to it
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
                    self.remember_stop(slug, phase, res, "service error", model=model, extra_fp=extra_fp)
                    return Outcome.INFRA
                return self.fail(slug, "the %s session failed: %s" % (phase, res.api_error or tail(res.text, 2)[:200]))
            # stopped: max_turns · budget · timeout · limited · interrupted
            budget = self.runner.budget
            why = {"max_turns": "turn cap", "budget": "budget cap ($%g)" % budget if budget > 0 else "budget cap",
                   "timeout": "time cap (%d min)" % self.session_minutes, "limited": res.limit_text,
                   "interrupted": "stopped by request"}.get(res.status, res.status)
            fp = self.fingerprint()
            self.remember_stop(slug, phase, res, why, fp, model, extra_fp)
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

    def catch_up_brief(self, slug: str, phase: str) -> str:
        """Before a design or build session: a branch that does not have the default branch in it gets it merged in, here, with
        the hooks off (as `adopt_doc` does) — a branch that waited days for its design to be approved is built on today's code.
        A merge that conflicts is undone, and the session is told what to do about it. A tree with work in it (a stopped
        session being continued) or a merge under way is left as it is. "" when there is nothing to tell."""
        base = self.base()
        if phase not in ("design", "build") or not self.git.head(base) or self.git.is_ancestor(base, "HEAD") \
                or self.git.dirty() or self.git.ok("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return ""
        behind = self.git.out("rev-list", "--count", "HEAD.." + base) or "some"
        msg = "sdlc(%s): merge %s" % (slug, self.default)
        r = self.git("-c", "core.hooksPath=" + os.devnull, "merge", "--no-edit", "-m", msg, base)
        if r.ok:
            self.say("UPDATE  %s — merged %s into the branch (it was %s commit(s) behind)" % (slug, base, behind))
            return ""
        why = self.git.last_error
        unmerged = [f for f in self.git("diff", "--name-only", "-z", "--diff-filter=U").out.split("\0") if f]
        self.git("merge", "--abort")
        where = "conflicts in: %s" % ", ".join(unmerged[:20]) if unmerged else "fails (%s)" % (why or "no reason given")
        self.say("UPDATE  %s — %s is %s commit(s) ahead of the branch and merging it %s; the session is told to" % (
            slug, base, behind, where))
        return ("This branch is %s commits behind %s and `git merge %s` %s. Merge it first, keep both sides' intent, commit, "
                "then do the phase.\n\n" % (behind, base, base, where))

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
        st.update(self.root, slug, design_sha=None)
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
            if phase in ("build", "fix") and res.called("sdlc-reviewer"):
                rec["review_seen"] = True               # the review ran in a fresh context: the gate may rely on its tally
        st.mutate(self.root, slug, fn)
        self.event("loop.session", res.status, phase, {
            "slug": slug, "phase": phase, "sid": res.sid, "status": res.status, "resumed": resumed, "cost_usd": res.cost_delta,
            "turns": res.turns, "models": res.models, "minutes": round((res.ended - res.started) / 60, 1),
            "log": os.path.relpath(res.log, self.root), "limit": res.limit_kind, "api_error": res.api_error,
            "effort": effort, "effort_reason": effort_why, "continued": continued})
        self.set_status()

    def remember_stop(self, slug: str, phase: str, res: SessionResult, why: str, fp: str = "", model: str = "",
                      extra_fp: str = "") -> None:
        st.update(self.root, slug, stop={"sid": res.sid, "phase": phase, "at": res.ended or epoch(), "why": why,
                                          "fp": fp or self.fingerprint(), "model": model or self.model_for(phase, slug),
                                          "extra": extra_fp})

    def fingerprint(self) -> str:
        """What this item's branch holds that the default branch does not, as content — equal fingerprints at two stops mean the
        session in between changed nothing. Every file that differs from where the branch left the default branch (committed
        or not: `leave_item` commits a stopped session's work, and that must not look like progress) and every new file, by
        path and content; the engine's own files and the noise a test run leaves are left out."""
        base = self.base()
        mb = self.git.out("merge-base", base, "HEAD") or base
        r = self.git("diff", "--name-only", "--no-renames", "-z", mb)
        paths = {p for p in r.out.split("\0") if p} if r.ok else set()
        paths |= set(self.git.untracked())
        h = hashlib.sha1()
        for p in sorted(x for x in paths if not is_noise(x)):
            h.update(p.encode("utf-8", "replace") + b"\0")
            try:
                full = os.path.join(self.root, p)
                if os.path.getsize(full) > 8 * 1024 * 1024:
                    h.update(str(os.path.getsize(full)).encode())          # a big file by its size: reading it is not worth it
                else:
                    with open(full, "rb") as fh:
                        h.update(hashlib.sha1(fh.read()).digest())
            except OSError:
                h.update(b"-")                                              # deleted, or not a plain file
        return h.hexdigest()[:16]

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
        if not self.sleep_holding(slug, wait):
            raise StopRun(self.stopping or "paused")
        self.set_status(state="running", wake_at="")

    def sleep_holding(self, slug: str, seconds: float) -> bool:
        """`sleep` for as long as a usage limit lasts — hours — without letting the claim on origin lapse: it is renewed every
        `claim_interval` seconds on the way, so that a loop in another clone does not find it stale and take the item. (The
        claim files of `state.Claims` need nothing: they go by the process being alive.) "" for no item, as in a self-check.
        False when a stop was requested meanwhile."""
        step, left = self.claim_interval(), seconds
        while left > 0:
            chunk = min(step, left)
            if not self.sleep(chunk):
                return False
            left -= chunk
            if left > 0 and slug:
                self.refresh_remote_claim(slug)
        return True

    def claim_interval(self) -> float:
        """Seconds between two renewals of the claim on origin while the loop sits on it without a session: a third of the time
        after which another clone takes a claim for abandoned (`take_remote_claim`), and never more than half an hour."""
        return max(0.1, min(30 * 60.0, 2 * self.session_minutes * 60 / 3))

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

    def needs_human(self, slug: str, reasons: List[str], push: bool = True) -> str:
        """The item waits for a person, with the branch on GitHub (pushed, its PR marked ready and commented) — `push=False`
        when the branch must not be pushed: origin has commits this checkout lacks. A push that origin refuses because its
        branch moved is no different: the person is told that too."""
        st.update(self.root, slug, state="needs-human", reasons=reasons)
        if self.forge.kind == "github" and push:
            ok, why = self.publish(slug, draft=False)
            if not ok and why.startswith(STALE_PUSH):
                reasons = reasons + ["not pushed — " + why]
                st.update(self.root, slug, reasons=reasons)
        self.say("HUMAN   %s — %s" % (slug, "; ".join(reasons)[:400]))
        self.event("loop.item", "needs-human", "; ".join(reasons)[:500], {"slug": slug, "reasons": reasons})
        if self.forge.kind == "github":
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
        st.update(self.root, slug, answers=None, parked_phase=None, **({"design_sha": None} if d.status == "ready" else {}))
        self.say("ANSWER  %s — applied; continuing" % slug)

    def ask_design_approval(self, slug: str) -> str:
        ok, url = self.publish(slug, draft=True)
        if not ok:
            if url.startswith(STALE_PUSH):
                return self.stale_push(slug, url)
            return self.fail(slug, "could not open the design PR: %s" % url, count=False)
        if self.forge.approved(slug):
            self.design_approved(slug)
            return Outcome.ADVANCED
        st.update(self.root, slug, state="awaiting-approval", pr=url)
        self.say("REVIEW  %s — design PR %s waits for approval" % (slug, url))
        self.event("loop.item", "awaiting-approval", url, {"slug": slug, "pr": url})
        return Outcome.AWAITING

    def design_approved(self, slug: str, **fields: object) -> None:
        """A person approved the design PR: what they approved is what origin's `sdlc/<slug>` holds now, so that commit becomes
        the design the gate holds the build to (`design_sha`) — a reviewer who edited the doc before approving has not changed
        it after the design. Only a commit on top of the recorded design that still says `planned`: nothing a build session made
        (it ends with `built`) and nothing on a branch that is not the design's."""
        sha = self.approved_tip(slug)
        st.update(self.root, slug, approved=True, **dict(fields, **({"design_sha": sha} if sha else {})))

    def approved_tip(self, slug: str) -> str:
        """origin's tip of `sdlc/<slug>` when it is the recorded design or a commit on top of it whose work doc still says
        `planned`; "" otherwise (nothing recorded, origin has no such branch, or the tip is something else)."""
        recorded = str(st.item(self.root, slug).get("design_sha") or "")
        tip = self.git.remote_sha("sdlc/" + slug)
        if not recorded or not tip or not self.git.head(recorded) or not self.git.is_ancestor(recorded, tip):
            return ""
        text = self.git.show(tip, os.path.relpath(self.doc_path(slug), self.root))
        return tip if text is not None and WorkDoc.parse(text).status == "planned" else ""

    def poll_team(self, items: Dict[str, Item]) -> None:
        """Team mode: notice approvals and merges done by people since the last look."""
        if self.forge.kind != "github":
            return
        for it in items.values():
            if it.phase not in TEAM_WAITS:
                continue
            if it.phase == "approval" and self.forge.approved(it.slug):
                self.design_approved(it.slug, state=None)
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
            rec = st.item(self.root, slug)
            seen = rec.get("review_seen")
            res = run_gate(self.cfg, self.git, slug, self.base(), design_sha=str(rec.get("design_sha") or ""),
                           reviewed=seen if isinstance(seen, bool) else None)
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
            if self.stopping:                      # the gate takes a while, and a stop came during it: no new session
                raise StopRun(self.stopping)
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
        lines.append("\nFix the cause — never weaken, skip or delete a test. " + FIX_REVIEW_RULE +
                     " A gate that says the review did not run, or that Important findings are open, wants that review itself."
                     " Commit as a new commit, then print SDLC_FIXED. " + FIX_NOT_YOURS)
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
        """Push the branch and open or update its PR. On GitHub the push is leased on the commit the loop last saw on origin
        (`remote_sha`, "" when there was no such branch): somebody else's commit there is never overwritten, the push is
        refused with a reason that starts with STALE_PUSH. After a push the commit pushed is the one seen."""
        d = self.doc(slug)
        title = d.title if d else slug
        b = "sdlc/" + slug
        expect: Optional[str] = None
        rec = st.item(self.root, slug)
        if self.forge.kind == "github":
            if "remote_sha" not in rec:                  # a record from before the loop kept it: look now, before any push
                why = self.sync_remote(slug)
                if why:
                    return False, "%s: %s" % (STALE_PUSH, why)
                rec = st.item(self.root, slug)
            expect = str(rec.get("remote_sha") or "")
            if rec.get("replaces"):
                old = rec.get("old_branch") or "a local branch"
                self.say("REPLACE %s — the push replaces the older %s on origin (%s); the earlier work stays as %s" % (
                    slug, b, str(rec["replaces"])[:10], old))
                # Before the push, not after: `forge.publish` reuses an open PR of the same head branch, so the older work's PR
                # — with its reviews — would take the new commits and be read as the approval of the new design. Closed first,
                # it never holds them, and a push that loses its lease leaves nothing but a closed PR of work that is replaced.
                self.forge.close(slug, "replaced: %s was cut again from %s; the earlier work stays as %s in the checkout that "
                                       "ran the loop" % (b, self.default, old))
        tip = self.git.head("refs/heads/" + b)
        ok, url = self.forge.publish(slug, title, pr_body(self.cfg, d, slug, rec), draft=draft, expect=expect)
        if self.forge.kind == "github":
            if tip and self.git.remote_sha(b) == tip:    # it went up (also when the PR step after it failed)
                st.update(self.root, slug, remote_sha=tip, replaces=None)
            if ok:
                st.update(self.root, slug, pr=url)
        return ok, url

    def stale_push(self, slug: str, why: str) -> str:
        """A push that origin refused because its branch moved since the loop looked: a person's commit is there, and the loop
        does not push over it. The branch waits for them, unpushed."""
        return self.needs_human(slug, ["%s — merge what is on origin's sdlc/%s into this branch, then retry" % (why, slug)],
                                push=False)

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
        renewed = started                    # when the claim on origin was last renewed
        seen: Optional[List[Dict[str, str]]] = None     # the checks of the last look that could be read

        def over(result: str, entries: List[Dict[str, str]], line: str = "", names: str = "", decision: str = ""
                 ) -> Tuple[str, List[Dict[str, str]]]:
            if line:
                self.say("CHECKS  %s — %s" % (slug, line))
            self.event("loop.checks", decision or result, names, {"slug": slug, "checks": entries,
                                                                  "waited_s": round(time.time() - started)})
            return result, entries

        while True:
            if time.time() - renewed >= self.claim_refresh:       # a wait of half an hour must not make the claim look abandoned
                renewed = time.time()
                self.refresh_remote_claim(slug)
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
            return self.publish_failed(slug, url)
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
            ok, info = self.forge.merge(slug, title, tests_allowed_note(d), head=self.pushed_head(slug))
            if ok or updated or self.forge.kind != "github" or self.forge.merge_state(slug) != "BEHIND":
                break
            updated = True
            if not self.update_branch(slug):
                return self.needs_human(slug, ["the branch is behind %s and merging it conflicts" % self.default])
            self.say("UPDATE  %s — behind %s; merged it in, waiting for the checks again" % (slug, self.default))
            ok, url = self.publish(slug, draft=False)
            if not ok:
                return self.publish_failed(slug, url)
        if not ok:
            return self.needs_human(slug, ["merge did not happen: %s" % info])
        rec = st.update(self.root, slug, state=None, reasons=None, stop=None, merged=now_iso(), ci_rounds=None)
        self.say("MERGED  %s — %s ($%.2f)" % (slug, url if self.forge.kind == "github" else info[:12], float(rec.get("cost", 0) or 0)))
        self.event("loop.merge", "allow", title, {"slug": slug, "pr": url if self.forge.kind == "github" else "",
                                                   "sha": info if self.forge.kind == "local" else "", "cost_usd": rec.get("cost", 0),
                                                   "sessions": rec.get("sessions", 0), "mode": "auto"})
        return Outcome.MERGED

    def pushed_head(self, slug: str) -> str:
        """The commit a merge is matched to: the one pushed last (GitHub merges the PR only if its head is that commit), or — with
        no push — the branch's own tip."""
        if self.forge.kind == "github":
            return str(st.item(self.root, slug).get("remote_sha") or "")
        return self.git.head("refs/heads/sdlc/" + slug)

    def publish_failed(self, slug: str, why: str) -> str:
        """A publish that did not go through: a person's commit on origin is theirs to merge in (the branch waits for them);
        anything else is a failure that does not count against the item."""
        if why.startswith(STALE_PUSH):
            return self.stale_push(slug, why)
        return self.fail(slug, "could not publish: %s" % why, count=False)

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
        if self.stopping:
            raise StopRun(self.stopping)
        out = self.session(slug, "fix", extra=brief, fix_round=rounds + 1)
        if out != Outcome.INFRA:             # a stop (StopRun) or a service error during the session does not use the round
            st.update(self.root, slug, ci_rounds=rounds + 1)
        return out

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
                "test, and never edit `.github/workflows/` to get green. " + FIX_REVIEW_RULE + " Commit as a new commit, then "
                "print SDLC_FIXED. A failure that is not this change's (the CI environment, a missing secret, a flaky external "
                "service, a command that already fails on the default branch) cannot be fixed from here: write why under "
                "`## Departures`, commit, and print SDLC_FIX_BLOCKED <why> instead.")
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

        def not_ready(status: str) -> str:
            return "work/%s.md says '%s' — set status: built (with the review tally) when the change is ready" % (slug, status)
        if s not in ("built", "done"):
            return 1, not_ready(s)
        if not self.claims.take(slug):
            return 2, "another loop is working on %s" % slug
        holder = self.take_remote_claim(slug)
        if holder:
            self.claims.release()
            return 2, "another loop holds %s on origin: %s" % (slug, holder)
        try:
            self.git.fetch()                  # what a person pushed meanwhile; a failure is no reason to stop
            why = self.sync_remote(slug)      # behind origin's: fast-forward; both moved: a person (nothing is pushed over theirs)
            if why:
                return 2, why
            s = self.doc_status(slug)         # what came in may have changed it: a `done` item with commits from origin is `built` again
            if s not in ("built", "done"):
                return 1, not_ready(s)
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
            self.release_remote_claim(slug)
            self.claims.release()

    # ------------------------------------------------------------------ self-check
    def self_check_due(self) -> bool:
        """Solo only: it reviews what this loop merged itself, and in team mode a person merges. One merged item is not
        reviewed again here — its own review covered the whole diff — so it takes `loop.self_check_min_items` of them (the
        run says so once when it skips for that)."""
        if self.team or not bool(self.cfg.get("loop.self_check", True)) or self.self_checks >= 2 or self.only \
                or self.self_check_misses >= 2 or not self.merged_since_check or self.git.head() == self.from_sha:
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
        so until the default branch is brought up to date neither the local branch nor origin's ref shows them. A session that
        did not end `ok` (a limit, a failure, the time) reviewed nothing: it is not one of the two self-checks, the range it
        was to review stays for the next, and a usage limit is waited out (or stops the run) like any other."""
        merged = list(self.merged_since_check)
        self.merged_since_check = []
        self.to_default()
        head = self.git.head()
        rng = "%s..%s" % (self.from_sha[:12], head[:12])
        model = self.model_for("self_check")
        effort, effort_why = self.effort_for("self_check")
        self.say("CHECK   reviewing what merged (%s) (%s)" % (rng, session_note(model, effort, effort_why)))
        self.set_status(state="running", item="", phase="self-check", phase_started=now_iso(), model=model, effort=effort)
        before = set(self.git.untracked())
        branch = self.git.branch()
        log = os.path.join(self.root, ".sdlc", "logs", "sessions", "%s-self-check.jsonl" % stamp())
        res = self.runner.run("/sdlc:go --self-check %s" % rng, log, min(self.session_minutes, 60), model, effort)
        self.cost += res.cost_delta
        self.undo_commits(branch, head)
        self.event("loop.session", res.status, "self-check", {"slug": "", "phase": "self-check", "sid": res.sid,
                                                              "cost_usd": res.cost_delta, "turns": res.turns,
                                                              "log": os.path.relpath(log, self.root), "effort": effort,
                                                              "effort_reason": effort_why, "continued": False})
        work = self.cfg.rel("work")
        new_docs = self.clean_after_self_check(before, work)
        if res.status != "ok":
            return self.self_check_not_ok(res, model, merged, new_docs)
        self.self_checks += 1
        self.self_check_misses = 0
        self.self_limit_hits = 0
        self.from_sha = head
        paths = new_docs
        if not paths:
            self.say("CHECK   clean")
            return 0
        try:
            cap = max(0, int(self.cfg.get("loop.self_check_max_items", 3)))      # 0 is none: every finding waits for a person
        except (TypeError, ValueError):
            cap = 3
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

    def undo_commits(self, branch: str, head: str) -> None:
        """The self-check writes new work docs and commits nothing — it runs on the default branch, which only a merge moves.
        A commit it made anyway is taken back, its changes kept in the index so that `clean_after_self_check` treats them like
        any other change of the session: undone, except for new work docs. A session that moved to another branch is left there."""
        if not head or self.git.branch() != branch or self.git.head() == head:
            return
        if self.git("reset", "-q", "--soft", head).ok:
            self.say("CHECK   the session committed on %s — taken back" % (branch or "the default branch"))
        else:
            self.say(self.git.with_reason("CHECK   the session committed on %s and the commit could not be taken back" % (
                branch or "the default branch")))

    def clean_after_self_check(self, before: set, work: str) -> List[str]:
        """The self-check writes new work docs and nothing else: every other change it made is undone, wherever it is and
        whatever its name — every tracked file back to what it was (staged or not, existing work docs and
        `.sdlc/config.json` too) and every new file and directory removed, except new `work/*.md`. Paths are read NUL-separated:
        a name git would quote is the name it is. The engine's own state and logs are not the session's. Returns the new docs."""
        self.git("reset", "-q")                  # a file the session staged is a plain new file from here on
        self.git("checkout", "--", ".")
        docs: List[str] = []
        for path in sorted(set(self.git.untracked()) - before):
            if is_noise(path):
                continue
            rest = path[len(work) + 1:]
            if path.startswith(work + "/") and rest.endswith(".md") and "/" not in rest:
                docs.append(os.path.join(self.root, path))
                continue
            full = os.path.join(self.root, path)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)                  # a nested repository shows up as a directory
                elif os.path.lexists(full):
                    os.remove(full)
            except OSError:
                continue
            parent = os.path.dirname(full)               # the directories the session made go with their files
            while parent != self.root and parent.startswith(self.root + os.sep):
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = os.path.dirname(parent)
        return docs

    def self_check_not_ok(self, res: SessionResult, model: str, merged: List[str], docs: List[str]) -> int:
        """A self-check that did not end `ok`: whatever it wrote is gone (docs too — a finding from a session that was cut
        short is not one to queue), it is not counted, `from_sha` stays, and a usage limit goes the way a limit goes in an item's
        session. Returns 1 when the check is to be tried again (the limit was waited out or the model switched), else 0."""
        for p in docs:
            if os.path.exists(p):
                os.remove(p)
        self.merged_since_check = merged        # whatever stopped it: the range is still to be reviewed, so the check is due again
        why = {"limited": res.limit_text, "timeout": "time cap (%d min)" % min(self.session_minutes, 60),
               "interrupted": "stopped by request", "max_turns": "turn cap", "budget": "budget cap"}.get(
                   res.status, res.api_error or res.status)
        self.say("CHECK   the session did not finish (%s) — not counted; the range is reviewed by the next check" % why)
        if res.status == "no_plugin":
            raise StopRun("the sdlc plugin was not loaded in the session — its hooks did not run (check `claude plugin list`)", 2)
        if res.status == "interrupted" or self.stopping:
            raise StopRun(self.stopping or "interrupted")
        if res.status != "limited":
            self.self_check_misses += 1         # due again, though not for ever: after two such misses this run stops trying
            return 0
        self.self_limit_hits += 1
        if self.self_limit_hits > 3:
            raise StopRun("usage limit hit %d times in a row (%s)" % (self.self_limit_hits, res.limit_text))
        self.handle_limit(res, model, "self_check")           # waits, switches model, or raises StopRun
        return 1


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
