"""One headless Claude Code session: `claude -p … --output-format stream-json`, capped in minutes and turns, stopped
gracefully (SIGINT → SIGTERM → SIGKILL), and read back from its event stream.

What the stream gives us that text never did:
- `system/init` lists the loaded plugins — a session without sdlc runs without its hooks, so the loop refuses it;
- `rate_limit_event` carries the limit status and the reset time as an epoch;
- `result` carries cost, turns and the models used. `total_cost_usd` of a resumed session already includes its earlier
  spend (Claude Code ≥ 2.1.277), so the cost recorded per call is the delta against the last total seen for that id.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from typing import Dict, List, Optional

from .util import epoch, read_json, run, write_json

LIMIT_RE = re.compile(r"(?:You've |you have )?hit your ([A-Za-z' ]+?) limit[^\n]*", re.I)
RESET_EPOCH_KEYS = ("resetsAt", "resets_at")


class SessionResult:
    def __init__(self) -> None:
        self.sid = ""
        self.status = "error"        # ok | max_turns | timeout | limited | interrupted | error | no_plugin
        self.rc = 0
        self.text = ""
        self.turns = 0
        self.cost_total = 0.0
        self.cost_delta = 0.0
        self.models: List[str] = []
        self.limit_kind = ""         # session | weekly | opus | sonnet | spend | …
        self.limit_reset = 0.0       # epoch seconds, 0 = unknown
        self.limit_text = ""
        self.api_error = ""
        self.denials = 0
        self.plugin_loaded: Optional[bool] = None
        self.log = ""
        self.started = 0.0
        self.ended = 0.0

    @property
    def stopped(self) -> bool:
        """Stopped early with its work resumable."""
        return self.status in ("max_turns", "timeout", "limited", "interrupted")

    def token(self, name: str) -> Optional[str]:
        """The value after a printed marker like `SDLC_PLANNED <slug>`, from the final result text."""
        m = None
        for m in re.finditer(r"\b%s\b[ \t]*([^\n`]*)" % re.escape(name), self.text):
            pass
        return m.group(1).strip() if m else None


def parse_stream(path: str) -> Dict[str, object]:
    """The parts of a stream-json log the loop needs."""
    info: Dict[str, object] = {"result": None, "rate": None, "plugins": None, "init": False}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return info
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "result":
                info["result"] = ev
            elif t == "rate_limit_event":
                ri = ev.get("rate_limit_info") or {}
                if ri.get("status") not in (None, "allowed"):
                    info["rate"] = ri
            elif t == "system" and ev.get("subtype") == "init":
                info["init"] = True
                info["plugins"] = [p.get("name") for p in (ev.get("plugins") or []) if isinstance(p, dict)]
    return info


def classify_limit(text: str, rate: Optional[dict]) -> tuple:
    """(kind, reset_epoch, text) for a usage-limit stop, or ('', 0, '') when the session was not stopped by one."""
    kind, reset, msg = "", 0.0, ""
    m = LIMIT_RE.search(text or "")
    if m:
        msg = m.group(0).strip()
        words = m.group(1).lower()
        # a model's own limit ("Sonnet weekly limit") is about that model: another one may still run
        for k in ("opus", "sonnet", "haiku", "fable", "weekly", "session", "daily", "spend", "usage"):
            if k in words:
                kind = k
                break
        kind = kind or "usage"
    if rate and rate.get("status") == "rejected":
        rt = str(rate.get("rateLimitType") or "")
        if not kind:
            kind = "weekly" if "seven_day" in rt else "session" if "five_hour" in rt else (rt or "usage")
            if "opus" in rt:
                kind = "opus"
            elif "sonnet" in rt:
                kind = "sonnet"
        for k in RESET_EPOCH_KEYS:
            if rate.get(k):
                try:
                    reset = float(rate[k])
                except (TypeError, ValueError):
                    pass
        msg = msg or "usage limit (%s)" % (rt or "rejected")
    return kind, reset, msg


class Runner:
    def __init__(self, root: str, claude_bin: str = "", plugin_dir: str = "", costs_path: str = "",
                 max_turns: int = 500, allowed_tools: str = "", budget_usd: float = 0.0):
        self.root = root
        # --claude-bin, then SDLC_CLAUDE_BIN, then the CLAUDE_BIN other tools read, then claude on PATH
        self.claude = claude_bin or os.environ.get("SDLC_CLAUDE_BIN") or os.environ.get("CLAUDE_BIN") or \
            shutil.which("claude") or ""
        self.plugin_dir = plugin_dir or os.environ.get("SDLC_PLUGIN_DIR", "")
        self.costs_path = costs_path or os.path.join(root, ".sdlc", "state", "session-costs.json")
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools
        self.budget = budget_usd
        self._flags: Optional[str] = None
        self.current: Optional[subprocess.Popen] = None
        self.interrupt = False      # set from a signal handler: stop the running session gracefully

    def available(self) -> bool:
        return bool(self.claude) and os.access(self.claude, os.X_OK)

    def _supports(self, flag: str) -> bool:
        if self._flags is None:
            self._flags = run([self.claude, "--help"], timeout=30).out
        return flag in self._flags

    def command(self, prompt: str, sid: str, resume: bool, model: str, effort: str) -> List[str]:
        cmd = [self.claude, "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--max-turns", str(self.max_turns), "--permission-mode", "acceptEdits"]
        if self._supports("--permission-prompts"):
            cmd += ["--permission-prompts", "none"]     # a denied call is denied, not retried by the model
        if self.allowed_tools:
            cmd += ["--allowedTools", self.allowed_tools]
        cmd += ["--resume", sid] if resume else ["--session-id", sid]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if self.budget and self.budget > 0 and self._supports("--max-budget-usd"):
            cmd += ["--max-budget-usd", str(self.budget)]
        if self.plugin_dir:
            cmd += ["--plugin-dir", self.plugin_dir]
        return cmd

    def run(self, prompt: str, log: str, minutes: float, model: str = "", effort: str = "",
            resume_sid: str = "") -> SessionResult:
        res = SessionResult()
        res.sid = resume_sid or str(uuid.uuid4())
        res.log = log
        os.makedirs(os.path.dirname(log), exist_ok=True)
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)            # a nested claude refuses to start inside another session otherwise
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        cmd = self.command(prompt, res.sid, bool(resume_sid), model, effort)
        res.started = epoch()
        with open(log, "a", encoding="utf-8") as out:
            out.write(json.dumps({"type": "sdlc_session", "sid": res.sid, "resume": bool(resume_sid), "model": model,
                                  "effort": effort, "prompt": prompt[:2000], "started": res.started}) + "\n")
            out.flush()
            try:
                p = subprocess.Popen(cmd, cwd=self.root, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
            except OSError as e:
                res.status, res.api_error = "error", "cannot start claude: %s" % e
                return res
            self.current = p
            res.rc = self._wait(p, minutes * 60)
            self.current = None
        res.ended = epoch()
        self._read(res)
        return res

    def _wait(self, p: subprocess.Popen, secs: float) -> int:
        """Wait in short slices so a stop request (self.interrupt) is noticed within a second."""
        deadline = time.time() + max(secs, 1)
        while True:
            try:
                return p.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            if self.interrupt:
                self.stop(p)
                return 130
            if time.time() >= deadline:
                self.stop(p)
                return 124

    def stop(self, p: Optional[subprocess.Popen] = None) -> None:
        """SIGINT lets claude finish the turn and write its result; then SIGTERM; then SIGKILL."""
        p = p or self.current
        if not p or p.poll() is not None:
            return
        for sig, grace in ((signal.SIGINT, 30), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                try:
                    p.send_signal(sig)
                except ProcessLookupError:
                    return
            try:
                p.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    def _read(self, res: SessionResult) -> None:
        info = parse_stream(res.log)
        r = info.get("result") or {}
        plugins = info.get("plugins")
        if isinstance(plugins, list):
            res.plugin_loaded = "sdlc" in plugins
        if isinstance(r, dict) and r:
            res.text = str(r.get("result") or "")
            res.turns = int(r.get("num_turns") or 0)
            res.cost_total = float(r.get("total_cost_usd") or 0.0)
            res.models = sorted((r.get("modelUsage") or {}).keys())
            res.denials = len(r.get("permission_denials") or [])
            res.api_error = str(r.get("api_error_status") or "")
            sub = str(r.get("subtype") or "")
            if sub == "success" and not r.get("is_error"):
                res.status = "ok"
            elif sub == "error_max_turns":
                res.status = "max_turns"
            elif sub == "error_max_budget_usd":
                res.status = "max_turns"     # resumable like a turn cap
            else:
                res.status = "error"
        kind, reset, msg = classify_limit(res.text, info.get("rate") if isinstance(info.get("rate"), dict) else None)
        if kind and res.status != "ok":
            res.status, res.limit_kind, res.limit_reset, res.limit_text = "limited", kind, reset, msg
        elif res.rc == 124:
            res.status = "timeout"
        elif res.rc == 130 and res.status != "ok":
            res.status = "interrupted"
        elif not r and res.status == "error" and not res.api_error:
            res.api_error = "no result in the session log (exit %s)" % res.rc
        if res.plugin_loaded is False:
            res.status = "no_plugin"
        self._cost(res)

    def _cost(self, res: SessionResult) -> None:
        costs = read_json(self.costs_path, {}) or {}
        last = float(costs.get(res.sid, 0.0) or 0.0)
        if res.cost_total > 0:
            res.cost_delta = round(res.cost_total - last if res.cost_total >= last else res.cost_total, 4)
            costs[res.sid] = res.cost_total
            write_json(self.costs_path, costs)


def wait_until(reset_epoch: float, should_stop=lambda: False, tick: float = 60.0) -> bool:
    """Sleep until reset_epoch (+60 s). Returns False if should_stop() turned true first."""
    while epoch() < reset_epoch + 60:
        if should_stop():
            return False
        time.sleep(min(tick, max(1.0, reset_epoch + 60 - epoch())))
    return True
