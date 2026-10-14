"""`.sdlc/config.json` — version 2. Defaults live here; the file only holds what differs or what init detected.

A version-1 file (0.x) is read through `migrate_v1` until `init` rewrites it, so a repository keeps working the moment
the plugin updates.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Tuple

from .util import read_json

CONFIG_REL = os.path.join(".sdlc", "config.json")
PHASES = ("design", "build", "fix", "self_check")      # the sessions the loop starts; each has a model and an effort
LEGACY_LOOP_DEFAULTS = {"model": "opus", "fix_model": "sonnet", "effort": "high"}   # what 0.x ran when the file said nothing
LEGACY_LOOP_CAPS = {"max_turns": (120, 200), "item_max_minutes": (45,)}   # caps 0.x shipped as defaults (0.8.2's init moved them)

DEPLOY_PATTERNS = [
    "(deploy|release|rollout|promote)[^|;&]*(prod|production|live)",
    "(prod|production|live)[^|;&]*(deploy|release|rollout|promote)",
    "kubectl [^|;&]*(apply|rollout|set image)[^|;&]*(prod|production)",
    "helm (upgrade|install)[^|;&]*(prod|production)",
    "terraform apply[^|;&]*(prod|production)",
]

DEFAULTS: Dict[str, Any] = {
    "version": 2,
    "language": "en",
    "mode": "solo",                       # solo: the loop merges what passes the gate · team: a human approves design and merge
    "paths": {"work": "work", "review": "REVIEW.md", "checks": "checks"},
    "commands": {"build": "", "test": "", "lint": "", "run": "", "format": "auto", "lint_file": ""},
    "verify": {"required_before_stop": False, "commands": ["test"], "agent": "auto", "agent_min_files": 4},
    "gate": {
        "commands": ["build", "test", "lint"],   # re-run by the shell before every merge (empty commands are skipped)
        "timeout_minutes": 30,
        "review": True,                          # the work doc's review tally must say important=0
        "test_integrity": True,                  # existing tests untouched unless the work doc says test_changes: allowed;
                                                 # none deleted, no case removed, no skip/only added either way
        "acceptance_frozen": True,               # acceptance criteria and test_changes unchanged since the design commit
        "new_dependencies": "human",             # human | allow
        "checks": True,                          # every executable in paths.checks passes
    },
    "risk": {"human": []},                       # globs: a PR touching these waits for a human merge (guard files always do)
    "protect": {
        "frozen_paths_file": ".sdlc/frozen-paths.txt",
        "generated_paths": [],
        "protect_tests": True,
        "test_paths": ["auto"],
        "block_direct_push": True,
        "default_branch": "main",
        "deploy_patterns": DEPLOY_PATTERNS,
        "deploy_approval_env": "RELEASE_APPROVAL",
    },
    "secrets": {"scan_edits": True, "scan_commits": True, "extra_patterns": []},
    "hooks": {"format_on_edit": True, "log_events": True, "session_context": True},
    "auto_commit": True,
    "claude_md": {"max_lines": 120},
    "loop": {
        "enabled": True,
        # per phase: design and self-check on the strongest model, build and fix on a cheaper one (a string "" passes
        # no --model / --effort). The 0.x keys loop.model, loop.fix_model and loop.effort still count — see Config.
        "models": {"design": "opus", "build": "sonnet", "fix": "sonnet", "self_check": "opus"},
        "efforts": {"design": "high", "build": "high", "fix": "medium", "self_check": "high"},
        "escalate_model": "opus",                # a build for an item that has failed before runs on this one ("" = never)
        "session_max_minutes": 120,
        "max_turns": 500,
        "max_continuations": 3,                  # a session stopped by a cap is continued at once (warm cache), this many times
        "cold_resume_minutes": 55,               # older stops start a fresh "continue" session instead of --resume
        "max_failures_per_slug": 3,
        "max_consecutive_failures": 3,
        "fix_rounds": 2,
        "self_check": True,
        "self_check_max_items": 3,
        "limit_wait_hours": 6,                   # wait out a usage limit that resets within this many hours
        "poll_minutes": 10,                      # only waiting items left (team approvals, another loop): look again this often
        "idle_hours": 12,                        # …and give up after this long without anything to do
        "max_items": 0,                          # 0 = until the queue is empty
        "max_minutes": 0,
        "budget_usd_per_session": 0,             # 0 = no --max-budget-usd
        "allowed_tools": "Read,Edit,Write,MultiEdit,Grep,Glob,Task,Bash",
        "item_note": "",
        "pause_file": ".sdlc/state/pause",
        "forge": "auto",                         # auto | github | local (local: squash-merge on this machine, no PR)
    },
    "review": {"sample_per_week": 3},
}


OBJECT_SECTIONS = ("commands", "protect", "paths", "loop")   # a file that writes one of these as anything else is a mistake


class NotInitialized(Exception):
    pass


def section(raw: Any, key: str) -> Dict[str, Any]:
    """`raw[key]` when it is an object; `{}` for a missing, null or mistyped section (`"commands": "make"`)."""
    v = raw.get(key) if isinstance(raw, dict) else None
    return v if isinstance(v, dict) else {}


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def migrate_v1(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Map a 0.x config onto version 2. Returns (new config, notes). Keys that no longer exist are dropped."""
    notes: List[str] = []
    new: Dict[str, Any] = {"version": 2, "language": raw.get("language", "en")}
    roles = section(raw, "roles")
    new["mode"] = "solo" if roles.get("solo") else "team"
    new["commands"] = {k: v for k, v in section(raw, "commands").items() if k in DEFAULTS["commands"] and v}
    v = section(raw, "verify")
    new["verify"] = {k: v[k] for k in DEFAULTS["verify"] if k in v}
    p = section(raw, "protect")
    prot = {k: p[k] for k in ("frozen_paths_file", "generated_paths", "protect_tests", "test_paths", "block_direct_push",
                              "default_branch") if k in p}
    kept = set()                                       # 0.x keys whose content lives on in 1.0: not "dropped"
    prod = section(section(raw, "environments"), "production")
    if prod.get("patterns"):
        prot["deploy_patterns"] = prod["patterns"]
        kept.add("environments")
    if prod.get("approval_env"):
        prot["deploy_approval_env"] = prod["approval_env"]
        kept.add("environments")
    new["protect"] = prot
    if p.get("ticketed_paths"):
        new["risk"] = {"human": list(p["ticketed_paths"])}
        notes.append("protect.ticketed_paths → risk.human (PRs touching them wait for a human merge)")
    for k in ("secrets", "hooks", "claude_md"):
        if isinstance(raw.get(k), dict):
            new[k] = {kk: vv for kk, vv in raw[k].items() if kk in DEFAULTS[k]}
    if "auto_commit" in raw:
        new["auto_commit"] = raw["auto_commit"]
    lp = section(raw, "loop")
    loop = {k: lp[k] for k in ("max_failures_per_slug", "fix_rounds", "self_check", "allowed_tools", "item_note",
                              "pause_file") if k in lp}
    for k, old_default in LEGACY_LOOP_DEFAULTS.items():   # a 0.x default is not a choice: 1.0 picks the model per phase
        if k in lp and lp[k] != old_default:
            loop[k] = lp[k]
            notes.append("loop.%s %r kept (it differs from the 0.x default) — loop.models / loop.efforts set it per phase" %
                         (k, lp[k]))
    old_caps = []
    for k, key in (("max_turns", "max_turns"), ("item_max_minutes", "session_max_minutes")):
        if k not in lp:
            continue
        if lp[k] in LEGACY_LOOP_CAPS[k]:                  # a cap 0.x shipped as its default is not a choice either…
            old_caps.append("loop.%s %s" % (k, lp[k]))
        elif lp[k] != DEFAULTS["loop"][key]:              # …and a value equal to 1.0's own default needs no line
            loop[key] = lp[k]
    if old_caps:
        notes.append("%s %s — 1.0's defaults apply (%d turns, %d min per session)" % (
            ", ".join(old_caps), "were 0.x defaults" if len(old_caps) > 1 else "was a 0.x default",
            DEFAULTS["loop"]["max_turns"], DEFAULTS["loop"]["session_max_minutes"]))
    if lp.get("enabled") is False:
        if roles.get("solo"):
            loop["enabled"] = False
        else:   # 0.x ran the loop in solo repositories only; false was the team default, not a choice
            notes.append("loop.enabled=false not carried over: 1.0 runs the loop in team repositories too, with people "
                         "approving designs and merges (set it again to switch the loop off)")
    if lp.get("max_items") or lp.get("max_minutes"):
        notes.append("loop.max_items / loop.max_minutes dropped: a run now goes until the queue is empty (set them again to cap it)")
    new["loop"] = loop
    rv = section(raw, "paths").get("review")
    if rv and rv != DEFAULTS["paths"]["review"]:
        new["paths"] = {"review": rv}
    off = ["roles.%s=false" % k for k in ("autopilot", "auto_merge") if roles.get(k) is False]
    if roles.get("solo") and off:
        notes.append("%s: in 1.0 solo mode /sdlc:run and /sdlc:go merge what passes the gate — use mode team to have a "
                     "person merge" % ", ".join(off))
    if "solo" in roles:
        kept.add("roles")                              # roles.solo became mode
    names = []
    for x in raw["policies"] if isinstance(raw.get("policies"), list) else []:
        name = x.get("name") if isinstance(x, dict) else x          # 0.x listed the policy skills by name
        if name:
            names.append(str(name))
    if names:
        notes.append("policies dropped (%s): their skills in .claude/skills/ stay and still trigger on their own, but "
                     "nothing applies them at review any more — name the ones the reviewer must apply in REVIEW.md" %
                     ", ".join(names))
    dropped = [k for k in ("source_of_truth", "environments", "gate", "policies", "evals", "monitor", "ci", "plan", "roles")
               if k in raw and k not in kept]
    if dropped:
        notes.append("dropped (not used by 1.0): " + ", ".join(dropped))
    return new, notes


def config_version(raw: Any) -> int:
    """The `version` of a config file; 1 (0.x) when it is missing or not a number."""
    try:
        return int(float((raw or {}).get("version", 1) or 1))
    except (AttributeError, TypeError, ValueError):
        return 1


class Config:
    def __init__(self, root: str):
        self.root = root
        path = os.path.join(root, CONFIG_REL)
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise NotInitialized("no .sdlc/config.json in %s — run /sdlc:init first" % root)
        self.raw = raw
        self.legacy = config_version(raw) < 2
        self.legacy_paths = dict(section(raw, "paths")) if self.legacy else {}
        if self.legacy:
            raw, _ = migrate_v1(raw)
        self.data = deep_merge(DEFAULTS, raw)
        for k, d in DEFAULTS.items():                            # `"commands": null`, `"loop": "off"` and the like: the defaults stand
            if isinstance(d, dict) and not isinstance(self.data.get(k), dict):
                self.data[k] = copy.deepcopy(d)
        self._phase_settings(section(raw, "loop"))

    def _phase_settings(self, loop: Dict[str, Any]) -> None:
        """loop.models / loop.efforts hold one value per phase. The 0.x keys still count where the file does not name
        the phase: loop.model → design, build and self_check; loop.fix_model → fix; loop.effort → every phase."""
        for table, legacy in (("models", lambda p: "fix_model" if p == "fix" else "model"), ("efforts", lambda p: "effort")):
            own = loop.get(table)
            if isinstance(own, str):                    # one value for every phase
                own = {p: own for p in PHASES}
            own = own if isinstance(own, dict) else {}
            values = dict(DEFAULTS["loop"][table])
            for p in PHASES:
                old = loop.get(legacy(p))
                if isinstance(old, str) and (old.strip() or table == "efforts"):   # effort "" meant: pass none
                    values[p] = old.strip()
                if own.get(p) is not None:
                    values[p] = str(own[p]).strip()
            self.data["loop"][table] = values
        for k in LEGACY_LOOP_DEFAULTS:
            self.data["loop"].pop(k, None)

    def model(self, phase: str) -> str:
        """The model a session of this phase runs on ("" = pass no --model)."""
        return str((self.get("loop.models") or {}).get(phase) or "")

    def effort(self, phase: str) -> str:
        """The --effort of a session of this phase ("" = pass none)."""
        return str((self.get("loop.efforts") or {}).get(phase) or "")

    @property
    def escalate_model(self) -> str:
        return str(self.get("loop.escalate_model", "") or "")

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur if cur is not None else default

    @property
    def solo(self) -> bool:
        return self.get("mode", "solo") != "team"

    @property
    def default_branch(self) -> str:
        return self.get("protect.default_branch", "main") or "main"

    def path(self, key: str) -> str:
        return os.path.join(self.root, self.get("paths." + key, key))

    def rel(self, key: str) -> str:
        return str(self.get("paths." + key, key)).strip("/")

    def command(self, name: str) -> str:
        """A verify/gate entry: a command name (build/test/lint/...) or a literal command."""
        if name in self.data.get("commands", {}):
            return str(self.get("commands." + name, "") or "")
        return name

    def gate_commands(self) -> List[Tuple[str, str]]:
        out, seen = [], set()
        for name in self.get("gate.commands", []) or []:
            cmd = self.command(str(name)).strip()
            if cmd and cmd not in seen:
                seen.add(cmd)
                out.append((str(name), cmd))
        return out

    def with_filled_commands(self, fills: Dict[str, str]) -> "Config":
        """A copy in which the commands in `fills` have a value where this config has none — what a branch that records them
        in its own `.sdlc/config.json` adds. A command that already has a value keeps it."""
        have = self.data.get("commands") or {}
        add = {n: v for n, v in fills.items() if not str(have.get(n) or "").strip()}
        other = copy.copy(self)
        other.data = dict(self.data, commands=dict(have, **add))
        return other
