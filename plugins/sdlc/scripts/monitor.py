#!/usr/bin/env python3
"""monitor.py — closing the loop (playbook Stage 6): deterministic control-band detection, tiered response.

A deterministic script watches a metric and invokes Claude only when a control band is breached; the tier decides
what Claude may do:  1σ → log · 2σ → diagnose (read-only) · 3σ → propose (PR into the review gate, or a pre-approved
runbook).  Detection uses mean/standard deviation over a rolling window plus Western Electric rules, so slow drift is
caught as well as spikes.  No model is involved in detection.

Config: .sdlc/bands.json  (JSON form of the playbook's bands.yaml)
  { "metrics": [ { "name": "ci_test_failure_rate", "command": "<prints one number>", "history_only": false,
                   "baseline": {"window": 30, "min_points": 5}, "rules": "western_electric",
                   "tiers": { "1sigma": {"action": "log"},
                              "2sigma": {"action": "diagnose", "tools": "Read,Grep,Bash(gh run view *)"},
                              "3sigma": {"action": "propose", "routes": ["pull_request", "runbook:rollback-deploy"]} } } ],
    "runbooks": { "rollback-deploy": "<pre-approved command>" } }
History: .sdlc/history/<metric>.jsonl  lines {"ts": ISO, "value": number}   (appended by this script; committed).
Findings: <intent>/triage/<ts>-<metric>.md in the Stage 1 intent.md format (frontmatter source: monitor, status: draft).
Logs: .sdlc/logs/events.jsonl (band.check, band.breach, monitor.diagnose, monitor.propose, monitor.runbook, monitor.pr).

Standard library only (no PyYAML). Python 3.9+.
"""
import argparse, datetime as dt, json, math, os, re, shlex, statistics, subprocess, sys, tempfile

TIER_RANK = {"none": 0, "1sigma": 1, "2sigma": 2, "3sigma": 3}


# ----------------------------------------------------------------------------- deterministic core
def western_electric(values, window=30, min_points=5):
    """values: chronological list of floats (newest last). Returns (tier, rule, info).

    Baseline = mean/stddev of up to `window` points before the newest. Rules (Western Electric):
      R1: the newest point is beyond mean ± 3σ                                  → 3sigma
      R2: 2 of the last 3 points beyond 2σ on the same side                      → 2sigma
      R3: 4 of the last 5 points beyond 1σ on the same side                      → 1sigma
      R4: 8 consecutive points on the same side of the mean (drift)              → 1sigma
    A zero-variance baseline with any deviation counts as R1 (the process has never moved before).
    """
    n = len(values)
    if n < 2:
        return "none", None, {"reason": "need at least 2 points"}
    base = values[max(0, n - 1 - window):n - 1]
    if len(base) < min_points:
        return "none", None, {"reason": f"insufficient baseline ({len(base)} < {min_points})", "baseline_n": len(base)}
    mean = statistics.fmean(base)
    sigma = statistics.pstdev(base) if len(base) > 1 else 0.0
    newest = values[-1]
    info = {"mean": mean, "sigma": sigma, "value": newest, "baseline_n": len(base), "window": window}

    def side(v):
        return 1 if v > mean else (-1 if v < mean else 0)

    def z(v):
        return (abs(v - mean) / sigma) if sigma > 0 else (math.inf if v != mean else 0.0)

    info["z"] = z(newest) if sigma > 0 else (None if newest == mean else "inf")
    if sigma == 0:
        if newest != mean:
            return "3sigma", "R1 (zero-variance baseline)", info
        return "none", None, info
    if z(newest) > 3:
        return "3sigma", "R1", info
    last3 = values[-3:]
    for s in (1, -1):
        if len(last3) == 3 and sum(1 for v in last3 if side(v) == s and z(v) > 2) >= 2:
            return "2sigma", "R2", info
    last5 = values[-5:]
    for s in (1, -1):
        if len(last5) == 5 and sum(1 for v in last5 if side(v) == s and z(v) > 1) >= 4:
            return "1sigma", "R3", info
    last8 = values[-8:]
    if len(last8) == 8 and all(side(v) == 1 for v in last8) or len(last8) == 8 and all(side(v) == -1 for v in last8):
        return "1sigma", "R4", info
    return "none", None, info


def selftest():
    base = [0.10] * 30
    cases = [
        ("R1 spike beyond 3σ", base[:-1] + [0.10, 0.12, 0.09, 0.11, 0.10] + [0.40], "3sigma", "R1"),
        ("R2 two of three beyond 2σ", [0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10] + [0.13, 0.10, 0.13], "2sigma", "R2"),
        ("R3 four of five beyond 1σ", [0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10] + [0.112, 0.112, 0.112, 0.10, 0.112], "1sigma", "R3"),
        ("R4 eight on one side", [0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10, 0.11, 0.09] + [0.1005] * 8, "1sigma", "R4"),
        ("none inside bands", [0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10, 0.11, 0.09, 0.10] + [0.10], "none", None),
        ("insufficient baseline", [0.1, 0.2, 0.3], "none", None),
        ("zero variance then move", [0.0] * 10 + [0.05], "3sigma", "R1 (zero-variance baseline)"),
    ]
    ok = True
    for name, series, want_tier, want_rule in cases:
        tier, rule, info = western_electric(series, window=30, min_points=5)
        good = tier == want_tier and (want_rule is None or rule == want_rule)
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'}  {name:34s} → {tier} {rule or ''}  (mean={info.get('mean')}, sigma={info.get('sigma')})")
    print("== selftest", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- io helpers
def now_iso(now=None):
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: invalid JSON in {path}: {e}")


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return out


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def project_root(explicit):
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return os.path.abspath(env)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
        if top:
            return top
    except Exception:
        pass
    return os.getcwd()


def plugin_root():
    return os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def cfg_get(cfg, path, default):
    cur = cfg
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def run_shell(cmd, timeout=60, cwd=None):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def slugify(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "metric"


class Log:
    def __init__(self, root, cfg, enabled=True, dry=False):
        self.path = os.path.join(root, ".sdlc", "logs", "events.jsonl")
        self.human = os.path.join(root, ".sdlc", "logs", "monitor.log")
        self.enabled = enabled and cfg_get(cfg, "hooks.log_events", True) is not False and os.path.isdir(os.path.join(root, ".sdlc"))
        self.dry = dry

    def event(self, event, decision, reason, detail=None, now=None):
        line = f"{now_iso(now)}\t{event}\t{decision}\t{reason}"
        print("  " + line)
        if not self.enabled or self.dry:
            return
        append_jsonl(self.path, {"ts": now_iso(now), "event": event, "session_id": "monitor", "decision": decision, "reason": reason, "detail": detail or {}})
        os.makedirs(os.path.dirname(self.human), exist_ok=True)
        with open(self.human, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ----------------------------------------------------------------------------- claude invocation
def claude_bin():
    return os.environ.get("CLAUDE_BIN") or "claude"


def invoke_claude(prompt, tools, max_turns=12, model=None, cwd=None, timeout=900, disallowed=None, plugin_dir=None):
    cmd = [claude_bin(), "-p", prompt, "--output-format", "json", "--max-turns", str(max_turns), "--no-session-persistence",
           "--permission-mode", "acceptEdits"]
    if tools:
        cmd += ["--allowedTools", tools]
    if disallowed:
        cmd += ["--disallowedTools", disallowed]
    if model:
        cmd += ["--model", model]
    if plugin_dir:
        cmd += ["--plugin-dir", plugin_dir]
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # allow running from inside a Claude Code session
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        data = {"result": p.stdout, "is_error": p.returncode != 0}
    data.setdefault("is_error", p.returncode != 0)
    data["_stderr"] = p.stderr[-2000:]
    return data


DIAGNOSE_PROMPT = """You are the on-call diagnoser for an AI-native SDLC loop. A control band was breached; detection was deterministic
and is NOT in question — your job is a read-only diagnosis.

Metric: {name} — {description}
Newest value: {value}   Baseline mean: {mean:.6g}   Baseline sigma: {sigma:.6g}   Rule: {rule}   Tier: {tier}
Recent points (oldest→newest): {recent}

Investigate with the tools you have (read-only). Then write ONLY the body of an intent.md in the Stage 1 Plan format,
using exactly these five markdown headings and nothing before the first heading:

## Problem
(the anomaly and its evidence: what changed, when, what you inspected, most likely cause with confidence)

## Proposed outcome
(what "back to baseline" and "prevented next time" look like; if a rollback or revert is the obvious first step, say so)

## Affected users and systems
(services, pipelines, teams)

## Constraints
(e.g. detection tier limits, change freeze, do not modify tests)

## Open questions
(what a human must decide or verify; include what you could not check)
"""


def write_triage_intent(root, cfg, metric, tier, rule, info, body, now, dry, log):
    intent_dir = cfg_get(cfg, "paths.intent", "intent")
    triage_rel = cfg_get(cfg, "monitor.triage_dir", os.path.join(intent_dir, "triage"))
    triage_dir = os.path.join(root, triage_rel)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    slug = f"{slugify(metric['name'])}-{stamp}"
    path = os.path.join(triage_dir, f"{stamp}-{slugify(metric['name'])}.md")
    fm = "\n".join([
        "---", "type: intent", f"slug: {slug}", f"title: \"{metric['name']} breached {tier} ({rule})\"", "status: draft",
        "author: sdlc-monitor", f"created: {now.strftime('%Y-%m-%d')}", "source: monitor", "record_id: \"\"",
        "links: { spec: \"\", plan: \"\" }",
        f"monitor: {{ metric: {metric['name']}, tier: {tier}, rule: \"{rule}\", value: {info.get('value')}, mean: {round(info.get('mean', 0), 6)}, sigma: {round(info.get('sigma', 0), 6)} }}",
        "---", f"# Intent: {metric['name']} breached {tier}", "",
        f"Author: sdlc-monitor (deterministic detection, {rule}). Status: draft — triage with /sdlc:triage.", "",
    ])
    content = fm + (body.strip() if body else "## Problem\n(diagnosis unavailable — see events.jsonl)\n") + "\n"
    if dry:
        print(f"  DRY would write {os.path.relpath(path, root)}")
        return path
    os.makedirs(triage_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log.event("monitor.intent", "allow", f"wrote {os.path.relpath(path, root)}", {"metric": metric["name"], "tier": tier}, now)
    return path


def open_pull_request(root, path, metric, tier, now, log, dry):
    if dry:
        print("  DRY would create branch + PR with the triage intent (gh)")
        return
    if subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode != 0:
        log.event("monitor.pr", "skip", "not a git repository", {"metric": metric["name"]}, now); return
    branch = f"sdlc/monitor-{slugify(metric['name'])}-{now.strftime('%Y%m%dT%H%M%S')}"
    rel = os.path.relpath(path, root)
    try:
        cur = subprocess.run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", root, "checkout", "-q", "-b", branch], check=True)
        subprocess.run(["git", "-C", root, "add", rel], check=True)
        subprocess.run(["git", "-C", root, "-c", "user.name=sdlc-monitor", "-c", "user.email=sdlc-monitor@localhost", "commit", "-qm", f"intent({tier}): {metric['name']} breached — triage"], check=True)
        if subprocess.run(["gh", "--version"], capture_output=True).returncode == 0:
            subprocess.run(["git", "-C", root, "push", "-u", "origin", branch], check=True)
            pr = subprocess.run(["gh", "pr", "create", "--title", f"[sdlc-monitor] {metric['name']} breached {tier}", "--body",
                                 f"Deterministic band breach ({tier}). Diagnosis in `{rel}`. Triage with `/sdlc:triage`; this PR only adds the intent — any fix goes through the normal review gate.",
                                 "--head", branch], capture_output=True, text=True, cwd=root)
            log.event("monitor.pr", "allow" if pr.returncode == 0 else "fail", (pr.stdout or pr.stderr).strip()[:300], {"branch": branch}, now)
        else:
            log.event("monitor.pr", "skip", f"gh not installed — intent committed on branch {branch}; push and open the PR manually", {"branch": branch}, now)
        subprocess.run(["git", "-C", root, "checkout", "-q", cur], check=False)
    except subprocess.CalledProcessError as e:
        log.event("monitor.pr", "fail", f"git/gh step failed: {e}", {"branch": branch}, now)


def run_runbook(root, bands, name, metric, now, log, dry):
    cmd = (bands.get("runbooks") or {}).get(name, "")
    if not cmd or "{{" in cmd:
        log.event("monitor.runbook", "skip", f"runbook '{name}' is not configured (bands.json runbooks)", {"metric": metric["name"]}, now); return
    if dry:
        print(f"  DRY would run runbook {name}: {cmd}"); return
    rc, out, err = run_shell(cmd, timeout=600, cwd=root)
    log.event("monitor.runbook", "allow" if rc == 0 else "fail", f"{name} exit {rc}", {"metric": metric["name"], "cmd": cmd, "stdout": out[-500:], "stderr": err[-500:]}, now)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Control-band monitoring with tiered response (Stage 6).")
    ap.add_argument("--dir", help="project root (default: $CLAUDE_PROJECT_DIR, git toplevel, cwd)")
    ap.add_argument("--config", help="bands file (default: monitor.bands in .sdlc/config.json, else .sdlc/bands.json)")
    ap.add_argument("--metric", help="only this metric")
    ap.add_argument("--dry-run", action="store_true", help="evaluate and print decisions; call nothing, write nothing")
    ap.add_argument("--now", help="ISO timestamp override (testing)")
    ap.add_argument("--selftest", action="store_true", help="run the Western Electric rule tests and exit")
    ap.add_argument("--json", action="store_true", help="print the decisions as JSON")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())

    root = project_root(args.dir)
    cfg = load_json(os.path.join(root, ".sdlc", "config.json"), {})
    bands_path = args.config or os.path.join(root, cfg_get(cfg, "monitor.bands", ".sdlc/bands.json"))
    if not os.path.isabs(bands_path):
        bands_path = os.path.join(root, bands_path)
    bands = load_json(bands_path, None)
    if bands is None:
        sys.exit(f"ERROR: no bands file at {bands_path} (run /sdlc:init, or copy templates/config/bands.json)")
    history_dir = cfg_get(cfg, "monitor.history_dir", ".sdlc/history")
    if not os.path.isabs(history_dir):
        history_dir = os.path.join(root, history_dir)
    now = dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else dt.datetime.now(dt.timezone.utc)
    log = Log(root, cfg, dry=args.dry_run)
    decisions = []
    print(f"== sdlc monitor  root={root} bands={os.path.relpath(bands_path, root)}{' (dry run)' if args.dry_run else ''}")

    for metric in bands.get("metrics", []):
        name = metric.get("name")
        if not name or (args.metric and name != args.metric):
            continue
        if metric.get("enabled") is False:
            print(f"-- {name}: disabled (example — set its command and \"enabled\": true)")
            continue
        hist_path = os.path.join(history_dir, f"{re.sub(r'[^A-Za-z0-9_.-]+', '-', name)}.jsonl")   # same file name run-evals.sh appends to
        history = read_jsonl(hist_path)
        values = [float(h["value"]) for h in history if isinstance(h.get("value"), (int, float))]
        print(f"-- {name}: {metric.get('description', '')}")
        if not metric.get("history_only"):
            cmd = metric.get("command", "")
            if not cmd:
                log.event("band.check", "skip", f"{name}: no command and not history_only", None, now); continue
            try:
                rc, out, err = run_shell(cmd, timeout=int(metric.get("timeout_seconds", 60)), cwd=root)
                value = float(out.strip().splitlines()[-1]) if out.strip() else None
            except (subprocess.TimeoutExpired, ValueError, IndexError) as e:
                rc, value, err = 1, None, str(e)
            if rc != 0 or value is None:
                log.event("band.check", "fail", f"{name}: observation command failed (rc={rc}) {err.strip()[:200]}", {"cmd": cmd}, now); continue
            values.append(value)
            if not args.dry_run:
                append_jsonl(hist_path, {"ts": now_iso(now), "value": value})
        elif not values:
            log.event("band.check", "skip", f"{name}: history_only with no history yet ({os.path.relpath(hist_path, root)})", None, now); continue

        window = int(cfg_get(metric, "baseline.window", 30)); min_points = int(cfg_get(metric, "baseline.min_points", 5))
        tier, rule, info = western_electric(values, window=window, min_points=min_points)
        tiers = metric.get("tiers", {})
        action = (tiers.get(tier) or {}).get("action", "log" if tier != "none" else "none")
        decision = {"metric": name, "value": values[-1], "tier": tier, "rule": rule, "action": action, **{k: v for k, v in info.items() if k in ("mean", "sigma", "z", "baseline_n", "reason")}}
        decisions.append(decision)
        if tier == "none":
            log.event("band.check", "ok", f"{name}={values[-1]} within bands ({info.get('reason', 'mean=%.6g sigma=%.6g' % (info.get('mean', 0), info.get('sigma', 0)))})", decision, now)
            continue
        log.event("band.breach", tier, f"{name}={values[-1]} {rule} → action {action}", decision, now)

        if action == "log":
            continue
        recent = ", ".join(f"{v:.6g}" for v in values[-10:])
        prompt = DIAGNOSE_PROMPT.format(name=name, description=metric.get("description", ""), value=values[-1], mean=info.get("mean", 0.0),
                                        sigma=info.get("sigma", 0.0), rule=rule, tier=tier, recent=recent)
        tools = (tiers.get(tier) or {}).get("tools", "Read,Grep,Glob")
        body = None
        if args.dry_run:
            print(f"  DRY would invoke claude -p (read-only diagnosis) with --allowedTools '{tools}'")
        else:
            try:
                res = invoke_claude(prompt, tools, model=cfg_get(cfg, "monitor.model", None), cwd=root,
                                    disallowed="Edit,Write,MultiEdit,NotebookEdit", plugin_dir=os.environ.get("CLAUDE_PLUGIN_ROOT"))
                body = res.get("result") if not res.get("is_error") else None
                log.event("monitor.diagnose", "allow" if body else "fail", f"{name}: claude -p diagnose ({'ok' if body else 'error'}) cost={res.get('total_cost_usd')}",
                          {"metric": name, "tier": tier, "stderr": res.get("_stderr", "")[-300:]}, now)
            except Exception as e:  # never let a diagnosis failure hide the breach
                log.event("monitor.diagnose", "fail", f"{name}: {e}", {"metric": name}, now)
        path = write_triage_intent(root, cfg, metric, tier, rule, info, body, now, args.dry_run, log)
        decision["intent"] = os.path.relpath(path, root)

        if action == "propose":
            routes = (tiers.get(tier) or {}).get("routes", [])
            for route in routes:
                if route == "pull_request":
                    open_pull_request(root, path, metric, tier, now, log, args.dry_run)
                elif route.startswith("runbook:"):
                    run_runbook(root, bands, route.split(":", 1)[1], metric, now, log, args.dry_run)
                elif route == "report":
                    rpt_dir = os.path.join(root, ".sdlc", "reports"); rpt = os.path.join(rpt_dir, f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slugify(name)}.md")
                    if args.dry_run:
                        print(f"  DRY would write {os.path.relpath(rpt, root)} (report for engineering leadership)")
                    else:
                        os.makedirs(rpt_dir, exist_ok=True)
                        with open(rpt, "w", encoding="utf-8") as f:
                            f.write(f"# {name} — {tier} ({rule})\n\nvalue {values[-1]} · baseline mean {info.get('mean', 0):.6g} · sigma {info.get('sigma', 0):.6g}\n\n{(body or '').strip()}\n")
                        log.event("monitor.report", "allow", f"wrote {os.path.relpath(rpt, root)}", {"metric": name, "tier": tier}, now)
                else:
                    log.event("monitor.propose", "skip", f"unknown route '{route}' (allowed: pull_request, runbook:<name>, report)", None, now)
            log.event("monitor.propose", tier, f"{name}: routes {routes} handled; findings enter the pipeline as {os.path.relpath(path, root)}", None, now)

    if args.json:
        print(json.dumps({"ts": now_iso(now), "root": root, "decisions": decisions}, indent=2))
    worst = max((TIER_RANK.get(d["tier"], 0) for d in decisions), default=0)
    print(f"== {len(decisions)} metric(s) evaluated; highest tier: {[k for k, v in TIER_RANK.items() if v == worst][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
