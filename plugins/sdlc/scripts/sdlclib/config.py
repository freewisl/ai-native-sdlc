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
        "test_integrity": True,                  # no existing test deleted, no case removed, no skip/only added
        "acceptance_frozen": True,               # acceptance criteria unchanged since the design commit
        "new_dependencies": "human",             # human | allow
        "checks": True,                          # every executable in paths.checks passes
    },
    "risk": {"human": []},                       # globs: a PR touching these waits for a human merge
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
        "model": "opus",
        "effort": "high",
        "fix_model": "sonnet",
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


class NotInitialized(Exception):
    pass


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
    roles = raw.get("roles") or {}
    new["mode"] = "solo" if roles.get("solo") else "team"
    new["commands"] = {k: v for k, v in (raw.get("commands") or {}).items() if k in DEFAULTS["commands"] and v}
    v = raw.get("verify") or {}
    new["verify"] = {k: v[k] for k in DEFAULTS["verify"] if k in v}
    p = raw.get("protect") or {}
    prot = {k: p[k] for k in ("frozen_paths_file", "generated_paths", "protect_tests", "test_paths", "block_direct_push",
                              "default_branch") if k in p}
    prod = ((raw.get("environments") or {}).get("production") or {})
    if prod.get("patterns"):
        prot["deploy_patterns"] = prod["patterns"]
    if prod.get("approval_env"):
        prot["deploy_approval_env"] = prod["approval_env"]
    new["protect"] = prot
    if p.get("ticketed_paths"):
        new["risk"] = {"human": list(p["ticketed_paths"])}
        notes.append("protect.ticketed_paths → risk.human (PRs touching them wait for a human merge)")
    for k in ("secrets", "hooks", "claude_md"):
        if isinstance(raw.get(k), dict):
            new[k] = {kk: vv for kk, vv in raw[k].items() if kk in DEFAULTS[k]}
    if "auto_commit" in raw:
        new["auto_commit"] = raw["auto_commit"]
    lp = raw.get("loop") or {}
    loop = {k: lp[k] for k in ("model", "effort", "fix_model", "max_turns", "max_failures_per_slug", "fix_rounds",
                              "self_check", "allowed_tools", "item_note", "pause_file") if k in lp}
    if "item_max_minutes" in lp:
        loop["session_max_minutes"] = lp["item_max_minutes"]
    if lp.get("enabled") is False:
        loop["enabled"] = False
    if lp.get("max_items") or lp.get("max_minutes"):
        notes.append("loop.max_items / loop.max_minutes dropped: a run now goes until the queue is empty (set them again to cap it)")
    new["loop"] = loop
    dropped = [k for k in ("source_of_truth", "environments", "gate", "policies", "evals", "monitor", "ci", "plan", "paths")
               if k in raw]
    if dropped:
        notes.append("dropped (features removed in 1.0): " + ", ".join(dropped))
    return new, notes


class Config:
    def __init__(self, root: str):
        self.root = root
        path = os.path.join(root, CONFIG_REL)
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise NotInitialized("no .sdlc/config.json in %s — run /sdlc:init first" % root)
        self.raw = raw
        self.legacy = int(raw.get("version", 1) or 1) < 2
        self.legacy_paths = dict(raw.get("paths") or {}) if self.legacy else {}
        if self.legacy:
            raw, _ = migrate_v1(raw)
        self.data = deep_merge(DEFAULTS, raw)

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
