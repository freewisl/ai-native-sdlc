#!/usr/bin/env python3
"""metrics.py — the playbook's leading and lagging indicators, computed from what the repository already records.

Sources (all local): git history of the artifact chain (intent/spec/plan), frontmatter, CLAUDE.md, LESSONS.md,
.sdlc/logs/events.jsonl (hooks, skills, evals, monitor), .sdlc/logs/gate.log, .sdlc/history/*.jsonl, evals/cases.
Optional: `gh` for PR-based indicators. Anything that needs a system this repo cannot see (incident tracker, CI system,
OpenTelemetry export) is reported as "source needed: …" rather than guessed.

Standard library only. Python 3.9+.  Usage: metrics.py [--since 90d] [--format md|json] [--dir ROOT]
"""
import argparse, datetime as dt, json, os, re, statistics, subprocess, sys
from collections import defaultdict

UTC = dt.timezone.utc


# ----------------------------------------------------------------------------- helpers
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


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return out


def cfg_get(cfg, path, default):
    cur = cfg
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def parse_since(s):
    m = re.fullmatch(r"(\d+)([dwmy])", s.strip())
    if not m:
        sys.exit("ERROR: --since must look like 90d, 12w, 6m, 1y")
    n, unit = int(m.group(1)), m.group(2)
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
    return dt.datetime.now(UTC) - dt.timedelta(days=days)


def parse_ts(s):
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    except Exception:
        try:
            return dt.datetime.strptime(s.strip()[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except Exception:
            return None


def frontmatter(path):
    fm = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return fm
    if not lines or lines[0].strip() != "---":
        return fm
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            fm[m.group(1)] = v
    return fm


class Git:
    def __init__(self, root):
        self.root = root
        self.ok = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode == 0

    def commit_times(self, rel):
        """Chronological list of commit datetimes touching rel."""
        if not self.ok:
            return []
        p = subprocess.run(["git", "-C", self.root, "log", "--format=%cI", "--", rel], capture_output=True, text=True)   # no --follow: rename detection wrongly links near-identical artifacts
        times = [parse_ts(x) for x in p.stdout.split() if x.strip()]
        return sorted(t for t in times if t)


def median_hours(deltas):
    vals = [d.total_seconds() / 3600 for d in deltas if d is not None]
    return round(statistics.median(vals), 1) if vals else None


def fmt(v, unit=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, 3)
    return f"{v}{unit}"


# ----------------------------------------------------------------------------- collectors
def collect_artifacts(root, cfg, git, since):
    chains = defaultdict(dict)
    for kind in ("intent", "spec", "plan"):
        d = os.path.join(root, cfg_get(cfg, f"paths.{kind}", kind))
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md") or name.upper().startswith(("TEMPLATE", "README")):
                continue
            path = os.path.join(d, name)
            fm = frontmatter(path)
            slug = fm.get("slug") or name[:-3]
            rel = os.path.relpath(path, root)
            times = git.commit_times(rel)
            chains[slug][kind] = {"path": rel, "fm": fm, "commits": times, "first": times[0] if times else None, "last": times[-1] if times else None,
                                  "created": parse_ts(fm.get("created", ""))}
        # triage queue
        if kind == "intent":
            tdir = os.path.join(root, cfg_get(cfg, "monitor.triage_dir", os.path.join(cfg_get(cfg, "paths.intent", "intent"), "triage")))
            if os.path.isdir(tdir):
                for name in sorted(os.listdir(tdir)):
                    if name.endswith(".md"):
                        fm = frontmatter(os.path.join(tdir, name))
                        chains.setdefault("__triage__", {}).setdefault("items", []).append({"path": os.path.relpath(os.path.join(tdir, name), root), "fm": fm})
    return chains


def collect_events(root, since):
    ev = read_jsonl(os.path.join(root, ".sdlc", "logs", "events.jsonl"))
    out = []
    for e in ev:
        t = parse_ts(e.get("ts", ""))
        if t and t >= since:
            e["_t"] = t
            out.append(e)
    return out


def collect_gate(root, cfg, since):
    path = cfg_get(cfg, "gate.log", ".sdlc/logs/gate.log")
    path = path if os.path.isabs(path) else os.path.join(root, path)
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 4:
                    t = parse_ts(parts[0])
                    if t and t >= since:
                        rows.append({"t": t, "decision": parts[1], "env": parts[2], "cmd": parts[3]})
    except FileNotFoundError:
        pass
    return rows


def collect_lessons(root, cfg):
    path = cfg_get(cfg, "paths.lessons", ".sdlc/LESSONS.md")
    path = path if os.path.isabs(path) else os.path.join(root, path)
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            cur = None
            for line in f:
                m = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})\s*[—-]+\s*(.+)$", line.strip())
                if m:
                    cur = {"date": parse_ts(m.group(1)), "title": m.group(2).strip(), "guard": ""}
                    entries.append(cur)
                elif cur and "Guard:" in line:
                    cur["guard"] = line.split("Guard:", 1)[1].strip()
    except FileNotFoundError:
        pass
    return entries


def claude_md_stats(root, git):
    path = os.path.join(root, "CLAUDE.md")
    if not os.path.isfile(path):
        return None
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    lessons = 0
    in_sec = False
    for line in lines:
        if line.startswith("## "):
            in_sec = "things claude gets wrong" in line.lower()
        elif in_sec and line.strip().startswith("-") and "(none" not in line.lower() and "add a line here" not in line.lower() and "/sdlc:lesson" not in line and "(아직 없음" not in line:
            lessons += 1
    return {"lines": len(lines), "lessons": lessons, "commits": len(git.commit_times("CLAUDE.md"))}


def eval_history(root, cfg):
    hd = cfg_get(cfg, "monitor.history_dir", ".sdlc/history")
    hd = hd if os.path.isabs(hd) else os.path.join(root, hd)
    rows = read_jsonl(os.path.join(hd, "eval_pass_rate.jsonl"))
    vals = [float(r["value"]) for r in rows if isinstance(r.get("value"), (int, float))]
    return vals


def eval_cases(root, cfg, git):
    d = os.path.join(root, cfg_get(cfg, "paths.evals", "evals"), "cases")
    out = {}
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if os.path.isdir(os.path.join(d, name)):
                times = git.commit_times(os.path.relpath(os.path.join(d, name), root))
                out[name] = times[0] if times else None
    return out


def gh_pr_indicators(root, since):
    """Best effort via gh: median minutes to first review for merged PRs in window."""
    try:
        p = subprocess.run(["gh", "pr", "list", "--state", "merged", "--limit", "100", "--json", "number,createdAt,mergedAt,reviews,additions,deletions"],
                           capture_output=True, text=True, cwd=root, timeout=60)
        if p.returncode != 0:
            return None
        prs = json.loads(p.stdout or "[]")
    except Exception:
        return None
    first_review, cycle = [], []
    for pr in prs:
        c = parse_ts(pr.get("createdAt"))
        m = parse_ts(pr.get("mergedAt"))
        if not c or c < since:
            continue
        revs = [parse_ts(r.get("submittedAt")) for r in pr.get("reviews") or []]
        revs = [r for r in revs if r]
        if revs:
            first_review.append((min(revs) - c).total_seconds() / 60)
        if m:
            cycle.append((m - c).total_seconds() / 3600)
    return {"prs": len(prs), "first_review_min": round(statistics.median(first_review), 1) if first_review else None,
            "cycle_h": round(statistics.median(cycle), 1) if cycle else None}


# ----------------------------------------------------------------------------- indicators
def build(root, cfg, since):
    git = Git(root)
    chains = collect_artifacts(root, cfg, git, since)
    events = collect_events(root, since)
    gate = collect_gate(root, cfg, since)
    lessons = collect_lessons(root, cfg)
    cm = claude_md_stats(root, git)
    ev_hist = eval_history(root, cfg)
    cases = eval_cases(root, cfg, git)
    gh = gh_pr_indicators(root, since) if subprocess.run(["bash", "-c", "command -v gh"], capture_output=True).returncode == 0 else None

    def evs(prefix):
        return [e for e in events if str(e.get("event", "")).startswith(prefix)]

    stages = []
    triage = chains.pop("__triage__", {}).get("items", [])
    real = {k: v for k, v in chains.items() if "intent" in v or "spec" in v or "plan" in v}

    # ---- Stage 1
    intents = [v["intent"] for v in real.values() if "intent" in v]
    t_to_commit = []
    for c in intents:
        start = c["created"]
        w = [e for e in evs("intent.write") if (e.get("detail") or {}).get("slug") == c["fm"].get("slug")]
        if w:
            start = min(e["_t"] for e in w)
        if start and c["first"]:
            t_to_commit.append(c["first"] - start)
    st = [c["fm"].get("status", "draft") for c in intents]
    approved, rejected = st.count("approved") + st.count("superseded"), st.count("rejected")
    edits_after_spec = 0
    for v in real.values():
        if "intent" in v and "spec" in v and v["spec"]["first"]:
            edits_after_spec += sum(1 for t in v["intent"]["commits"] if t > v["spec"]["first"])
    stages.append(("Stage 1 — Plan", [
        ("Time from first conversation to committed intent.md (median h)", median_hours(t_to_commit), "frontmatter created / intent.write event → first git commit of the intent", "playbook expects weeks → hours"),
        ("Intents in window", len(intents), "intent/*.md", ""),
        ("Survival rate (approved ÷ decided)", (round(approved / (approved + rejected), 2) if (approved + rejected) else None), "frontmatter status", f"approved={approved} rejected={rejected} draft={st.count('draft')}"),
        ("Intent edits after the first spec.md commit", edits_after_spec, "git log", "should trend to zero"),
    ]))

    # ---- Stage 2
    i2s, spec_after_plan = [], 0
    for v in real.values():
        if "intent" in v and "spec" in v and v["intent"]["first"] and v["spec"]["first"]:
            i2s.append(v["spec"]["first"] - v["intent"]["first"])
        if "spec" in v and "plan" in v and v["plan"]["first"]:
            spec_after_plan += sum(1 for t in v["spec"]["commits"] if t > v["plan"]["first"])
    stages.append(("Stage 2 — Design", [
        ("intent.md commit → spec.md commit (median h)", median_hours(i2s), "two git timestamps", ""),
        ("Requirements rework: spec.md commits after the first plan.md commit", spec_after_plan, "git log", "lagging"),
        ("Flagged concerns resolved before build", None, "source needed: spec.md 'Flagged concerns' table review by the product owner", ""),
    ]))

    # ---- Stage 3
    plans = [v["plan"] for v in real.values() if "plan" in v]
    p2impl = []
    for v in real.values():
        if "plan" in v and v["plan"]["first"] and v["plan"]["fm"].get("status") == "implemented" and v["plan"]["last"]:
            p2impl.append(v["plan"]["last"] - v["plan"]["first"])
    sessions = evs("session.start")
    per_day = defaultdict(int)
    for e in sessions:
        per_day[e["_t"].strftime("%Y-%m-%d")] += 1
    ends = {e.get("session_id"): e["_t"] for e in evs("session.end")}
    conc = 0
    starts = sorted((e["_t"], ends.get(e.get("session_id"), e["_t"] + dt.timedelta(hours=1))) for e in sessions)
    for s, _ in starts:
        conc = max(conc, sum(1 for s2, e2 in starts if s2 <= s < e2))
    stages.append(("Stage 3 — Build", [
        ("Plans approved / implemented", f"{sum(1 for p in plans if p['fm'].get('status') in ('approved', 'implemented'))} / {sum(1 for p in plans if p['fm'].get('status') == 'implemented')}", "plan/*.md frontmatter", ""),
        ("Plan approval → implemented (median h)", median_hours(p2impl), "plan.md first commit → last commit with status implemented", "leading (proxy for plan→merged PR)"),
        ("Request → PR via /sdlc:go (median min, n)", (lambda g: f"{statistics.median(g):.0f} ({len(g)})" if g else None)([float((e.get("detail") or {}).get("duration_min")) for e in evs("go.run") if isinstance((e.get("detail") or {}).get("duration_min"), (int, float))]), "go.run events (detail.duration_min)", "end-to-end leading indicator"),
        ("Autopilot loop: merged per run (mean, runs) / items that needed a human", (lambda L: f"{sum(int((e.get('detail') or {}).get('merged', 0)) for e in L) / len(L):.1f} ({len(L)}) / {sum(int((e.get('detail') or {}).get('open', 0)) + int((e.get('detail') or {}).get('failed', 0)) for e in L)}" if L else None)(evs("loop.run")), "loop.run events (/sdlc:run)", "needed a human = left open + failed"),
        ("First-pass merge share / rework cycles per change", None, "source needed: PR metadata (gh pr view … reviews, commits after first review)", ""),
        ("CLAUDE.md size (lines) / lessons / commits", (f"{cm['lines']} / {cm['lessons']} / {cm['commits']}" if cm else None), "CLAUDE.md, git log", "keep under a page; lessons grow when a mistake repeats"),
        ("Mistakes repeated that CLAUDE.md should have caught", sum(1 for e in evs("review.") if str((e.get("detail") or {}).get("repeat_finding", "")).lower() == "true"), "review.run events with repeat_finding: true", "leading"),
        ("Lessons recorded (/sdlc:lesson)", len(evs("lesson.add")), "lesson.add events", "corrections that went into CLAUDE.md"),
        ("Time to first merged PR for a new team member", None, "source needed: PR history (gh pr list --author) + join date", "lagging"),
        ("Policy skills / review findings citing a policy", f"{len(cfg_get(cfg, 'policies', []))} / {sum(int((e.get('detail') or {}).get('policy_findings', 0) or 0) for e in evs('review.'))}", "config.policies, review events", "findings should fall to zero once the skill applies the policy"),
        ("Sessions in window / max concurrent (local log)", f"{len(sessions)} / {conc}", "session.start/session.end events", "OpenTelemetry export gives the fleet-wide number"),
        ("Changes merged per engineer per week", None, "source needed: PR history (gh) + team roster", ""),
    ]))

    # ---- Stage 4
    vr = evs("verify.run")
    vpass = sum(1 for e in vr if e.get("decision") in ("pass", "allow"))   # hooks log pass/fail, skills log pass|allow / fail|deny
    stops = evs("hook.stop")
    inc_to_eval = []
    for l in lessons:
        m = re.search(r"evals?/cases/([A-Za-z0-9_.-]+)", l.get("guard", ""))
        if m and cases.get(m.group(1)) and l.get("date"):
            inc_to_eval.append(cases[m.group(1)] - l["date"])
    stages.append(("Stage 4 — Test", [
        ("Local verify runs pass rate", (round(vpass / len(vr), 2) if vr else None), "verify.run events (post-bash hook)", f"{vpass}/{len(vr)}"),
        ("Stops blocked for missing verification", sum(1 for e in stops if e.get('decision') == 'deny'), "hook.stop events", "verify-before-done gate"),
        ("First-pass CI success rate for agent-written changes", None, "source needed: CI system (workflow_run conclusions)", ""),
        ("Eval pass rate — last / mean / min (n)", (f"{ev_hist[-1]:.2f} / {statistics.fmean(ev_hist):.2f} / {min(ev_hist):.2f} ({len(ev_hist)})" if ev_hist else None), ".sdlc/history/eval_pass_rate.jsonl (run-evals.sh)", "trend"),
        ("Eval cases in suite", len(cases), "evals/cases", ""),
        ("Production incident → permanent eval (median days)", (round(statistics.median(d.total_seconds() / 86400 for d in inc_to_eval), 1) if inc_to_eval else None), "LESSONS.md entry date → first commit of the guard case", ""),
        ("Review time per PR / change failure rate", None, "source needed: PR metadata, incident tracker", ""),
        ("Regressions caught in CI vs found in production", None, "source needed: CI eval results vs incident tracker", ""),
    ]))

    # ---- Stage 5
    gates = defaultdict(int)
    for g in gate:
        gates[(g["env"], g["decision"])] += 1
    gate_summary = ", ".join(f"{env}:{dec}={n}" for (env, dec), n in sorted(gates.items())) or None
    reviews = evs("review.")
    important = sum(int((e.get("detail") or {}).get("important", 0) or 0) for e in reviews)
    nits = sum(int((e.get("detail") or {}).get("nits", 0) or 0) for e in reviews)
    stages.append(("Stage 5 — Deploy", [
        ("Time to first review (median min)", (gh or {}).get("first_review_min") if gh else None, "gh pr list (merged PRs in window)" if gh else "source needed: gh CLI / PR metadata", "should fall to minutes"),
        ("PR cycle time (median h)", (gh or {}).get("cycle_h") if gh else None, "gh pr list" if gh else "source needed: gh", ""),
        ("Review runs / Important / Nits (local)", (f"{len(reviews)} / {important} / {nits}" if reviews else None), "review.* events (/sdlc:review)", ""),
        ("Review comments resolved without a human touching the branch", None, "source needed: PR thread metadata (@claude fixes)", ""),
        ("Gate decisions (env:decision=count)", gate_summary, ".sdlc/logs/gate.log", "allow/deny/ask with timestamps"),
        ("Time waiting on each approval gate", None, "source needed: OpenTelemetry hook decision timestamps (or pair deny→allow in gate.log)", ""),
        ("Pipeline failures triaged without paging a human", None, "source needed: CI logs (sdlc-ci-triage.yml comments)", ""),
        ("Defects/vulnerabilities before merge vs escaped; DORA", None, "source needed: PR history + incident tracker; CI/CD tooling", ""),
    ]))

    # ---- Stage 6
    breaches = evs("band.breach")
    intents_written = evs("monitor.intent")
    b2i = []
    for b in breaches:
        later = [i for i in intents_written if i["_t"] >= b["_t"] and (i.get("detail") or {}).get("metric") == (b.get("detail") or {}).get("metric")]
        if later:
            b2i.append(min(later, key=lambda i: i["_t"])["_t"] - b["_t"])
    tstat = defaultdict(int)
    for it in triage:
        tstat[it["fm"].get("status", "draft")] += 1
    titles = defaultdict(int)
    for l in lessons:
        key = re.sub(r"[^a-z]+", " ", l["title"].lower()).strip()
        titles[" ".join(key.split()[:3])] += 1
    repeats = sum(n - 1 for n in titles.values() if n > 1)
    scans = evs("scan.")
    stages.append(("Stage 6 — Maintain", [
        ("Band breaches in window (by tier)", (", ".join(f"{t}={sum(1 for e in breaches if e.get('decision') == t)}" for t in ('1sigma', '2sigma', '3sigma')) if breaches else None), "band.breach events (monitor.py)", ""),
        ("Band breach → intent in triage queue (median s)", (round(statistics.median(d.total_seconds() for d in b2i), 1) if b2i else None), "band.breach → monitor.intent events", "vs. old incident→post-mortem time"),
        ("Triage queue by status", (", ".join(f"{k}={v}" for k, v in sorted(tstat.items())) or None), "intent/triage/*.md frontmatter", "fix now / schedule / dismiss"),
        ("Findings that became merged fixes", None, "source needed: PR history linked to triage slugs", ""),
        ("Repeat incidents of the same class", repeats, ".sdlc/LESSONS.md title clustering (first 3 words)", "should fall as evals accumulate"),
        ("Scans run / findings", (f"{len(scans)} / {sum(int((e.get('detail') or {}).get('findings', 0) or 0) for e in scans)}" if scans else None), "scan.* events (/sdlc:scan)", "hosted Claude Security has its own history"),
        ("Repositories on a scan schedule", None, "source needed: Claude Security project settings / sdlc-scan.yml presence per repo", ""),
    ]))
    return stages, {"root": root, "since": since.isoformat(), "git": git.ok, "gh": bool(gh), "chains": len(real), "events": len(events)}


def render_md(stages, meta):
    out = [f"# SDLC indicators — {meta['root']}", "",
           f"window since {meta['since'][:10]} · git: {'yes' if meta['git'] else 'no'} · gh: {'yes' if meta['gh'] else 'no'} · chains: {meta['chains']} · events: {meta['events']}", ""]
    for title, rows in stages:
        out += [f"## {title}", "", "| Indicator | Value | Source | Note |", "|---|---|---|---|"]
        for ind, val, src, note in rows:
            out.append(f"| {ind} | {fmt(val)} | {src} | {note} |")
        out.append("")
    out.append("Values marked — need data this repository does not hold yet; the Source column names what to connect (gh, CI, incident tracker, OpenTelemetry).")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Playbook indicators from git, events and logs.")
    ap.add_argument("--since", default="90d")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--dir")
    args = ap.parse_args()
    root = project_root(args.dir)
    cfg = load_json(os.path.join(root, ".sdlc", "config.json"), {})
    stages, meta = build(root, cfg, parse_since(args.since))
    if args.format == "json":
        print(json.dumps({"meta": meta, "stages": [{"stage": t, "indicators": [{"indicator": i, "value": v, "source": s, "note": n} for i, v, s, n in rows]} for t, rows in stages]}, indent=2, default=str))
    else:
        print(render_md(stages, meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
