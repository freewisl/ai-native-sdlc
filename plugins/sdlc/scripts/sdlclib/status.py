"""What `/sdlc:status` and "어디까지 됐어?" show: is the loop running and on what, the queue by phase, what needs you,
cost, recent merges, and a few auto-merged changes nobody has looked at yet."""
from __future__ import annotations

import datetime as _dt
import os
import statistics
from typing import Any, Dict, List

from . import state as st
from .config import Config
from .events import read_events
from .gitops import Git
from .queue import Item, counts, runnable, scan
from .util import epoch, parse_iso, read_json


def _ago(iso: str) -> str:
    d = parse_iso(iso)
    if not d:
        return ""
    s = max(0, int(epoch() - d.timestamp()))
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def _first_lines(text: str, n: int = 3) -> List[str]:
    return [l.strip() for l in (text or "").splitlines() if l.strip()][:n]


def collect(root: str) -> Dict[str, Any]:
    cfg = Config(root)
    git = Git(root)
    default = cfg.default_branch
    base = git.base_ref(default)
    records = st.load_items(root)
    items = scan(cfg, git, base, records)
    loop = st.read_status(root)
    pid = st.loop_pid(root)
    pause = os.path.join(root, str(cfg.get("loop.pause_file", ".sdlc/state/pause")))
    out: Dict[str, Any] = {"root": root, "mode": "solo" if cfg.solo else "team", "default_branch": default,
                           "legacy_config": cfg.legacy}
    out["loop"] = {"running": bool(pid), "pid": pid, "paused": os.path.exists(pause), "state": loop.get("state", ""),
                   "item": loop.get("item", "") if pid else "", "phase": loop.get("phase", "") if pid else "",
                   "for": _ago(str(loop.get("phase_started", ""))) if pid else "", "log": loop.get("log", ""),
                   "session_log": loop.get("session_log", "") if pid else "", "wake_at": loop.get("wake_at", "") if pid else "",
                   "last_stop": loop.get("stop", "") if not pid else "", "run_cost_usd": loop.get("cost_usd", 0)}
    out["counts"] = counts(items)
    out["next"] = [it.slug for it in runnable(items)][:5]
    out["needs_you"] = needs_you(items)
    out["items"] = [it.as_dict() for it in sorted(items.values(), key=lambda i: (i.created or "9999", i.slug))]
    out["cost"] = costs(root)
    merges = [e for e in read_events(root, "loop.merge")]
    out["recent_merges"] = [{"slug": (e.get("detail") or {}).get("slug", ""), "title": e.get("reason", ""),
                             "pr": (e.get("detail") or {}).get("pr", ""), "ago": _ago(e.get("ts", "")),
                             "cost_usd": (e.get("detail") or {}).get("cost_usd", 0)} for e in merges[-5:]][::-1]
    out["review_sample"] = review_sample(root, cfg, merges)
    return out


def needs_you(items: Dict[str, Item]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for it in sorted(items.values(), key=lambda i: (i.created or "9999", i.slug)):
        d = it.branch_doc or it.doc
        base = {"slug": it.slug, "title": it.title, "pr": it.rec.get("pr", "")}
        if it.phase == "needs-input":
            out.append(dict(base, kind="question", text=_first_lines(d.section("Questions") if d else "", 8)))
        elif it.phase == "needs-human":
            out.append(dict(base, kind="human", text=[str(r) for r in it.rec.get("reasons") or []]))
        elif it.phase == "approval":
            out.append(dict(base, kind="approve-design", text=["design PR waits for an approving review"]))
        elif it.phase == "awaiting-merge":
            out.append(dict(base, kind="merge", text=["gate passed — merge the PR"]))
        elif it.phase == "blocked":
            out.append(dict(base, kind="blocked", text=["failed %s times: %s" % (it.rec.get("failures"), it.rec.get("last_error", ""))]))
        elif it.phase == "proposed":
            out.append(dict(base, kind="proposed", text=_first_lines(d.section("Goal") if d else "", 2)))
        elif it.wait_kind == "human":
            out.append(dict(base, kind="dependency", text=[it.wait]))
    return out


def costs(root: str) -> Dict[str, Any]:
    today = _dt.date.today()
    day = week = 0.0
    per_slug: Dict[str, float] = {}
    for e in read_events(root, "loop.session"):
        det = e.get("detail") or {}
        c = float(det.get("cost_usd") or 0)
        d = parse_iso(e.get("ts", ""))
        if d is None:
            continue
        local = d.astimezone().date()
        if local == today:
            day += c
        if (today - local).days < 7:
            week += c
        if det.get("slug"):
            per_slug[det["slug"]] = per_slug.get(det["slug"], 0.0) + c
    merged = [(e.get("detail") or {}).get("slug") for e in read_events(root, "loop.merge")]
    vals = [per_slug[s] for s in merged if s in per_slug and per_slug[s] > 0]
    return {"today_usd": round(day, 2), "week_usd": round(week, 2),
            "median_per_merged_usd": round(statistics.median(vals), 2) if vals else None, "merged_items_counted": len(vals)}


def review_sample(root: str, cfg: Config, merges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Auto-merged changes from the last 7 days nobody rated yet — the costliest first (more turns, more risk)."""
    rated = read_json(os.path.join(root, st.RATINGS_REL), {}) or {}
    n = int(cfg.get("review.sample_per_week", 3) or 0)
    if n <= 0:
        return []
    recent = []
    for e in merges:
        det = e.get("detail") or {}
        d = parse_iso(e.get("ts", ""))
        if not d or epoch() - d.timestamp() > 7 * 86400 or det.get("mode") != "auto":
            continue
        if det.get("slug") in rated:
            continue
        recent.append({"slug": det.get("slug", ""), "title": e.get("reason", ""), "pr": det.get("pr", ""),
                       "sha": det.get("sha", ""), "ago": _ago(e.get("ts", "")), "cost_usd": det.get("cost_usd", 0)})
    recent.sort(key=lambda r: -float(r.get("cost_usd") or 0))
    return recent[:n]


def render(s: Dict[str, Any]) -> str:
    L: List[str] = []
    lp = s["loop"]
    if lp["running"]:
        what = "%s %s for %s" % (lp["phase"] or "between items", lp["item"], lp["for"]) if lp["item"] else (lp["state"] or "running")
        if lp.get("wake_at"):
            what += " (usage limit — wakes %s)" % lp["wake_at"]
        L.append("loop: running (pid %s) — %s · this run $%.2f" % (lp["pid"], what, float(lp.get("run_cost_usd") or 0)))
    else:
        L.append("loop: not running%s%s" % (" · paused" if lp["paused"] else "",
                                             " · last stop: %s" % lp["last_stop"] if lp.get("last_stop") else ""))
    if s.get("legacy_config"):
        L.append("config: version 1 — run /sdlc:init once to migrate")
    c = s["counts"]
    order = ["design", "build", "gate", "merge", "answer", "approval", "awaiting-merge", "needs-input", "needs-human",
             "blocked", "proposed", "done", "dropped", "closed"]
    names = {"design": "ready", "build": "planned", "gate": "built", "merge": "to merge", "answer": "answered",
             "approval": "design in review", "awaiting-merge": "awaiting merge", "needs-input": "question",
             "needs-human": "needs a person"}
    L.append("queue: " + (" · ".join("%d %s" % (c[k], names.get(k, k)) for k in order if c.get(k)) or "empty"))
    if s["next"]:
        L.append("next: " + ", ".join(s["next"]))
    if s["needs_you"]:
        L.append("needs you:")
        mark = {"question": "?", "human": "!", "approve-design": "✓", "merge": "⇢", "blocked": "✗", "proposed": "+",
                "dependency": "…"}
        for n in s["needs_you"]:
            text = n["text"][0] if n["text"] else ""
            L.append("  %s %s — %s%s" % (mark.get(n["kind"], "-"), n["slug"], text[:160], ("  " + n["pr"]) if n.get("pr") else ""))
            for more in n["text"][1:4]:
                L.append("      " + more[:160])
    co = s["cost"]
    L.append("cost: today $%.2f · 7 days $%.2f%s" % (co["today_usd"], co["week_usd"],
             " · median per merged item $%.2f (%d)" % (co["median_per_merged_usd"], co["merged_items_counted"])
             if co["median_per_merged_usd"] is not None else ""))
    if s["recent_merges"]:
        L.append("merged lately: " + ", ".join("%s (%s ago)" % (m["slug"], m["ago"]) for m in s["recent_merges"]))
    if s["review_sample"]:
        L.append("worth a look (auto-merged, not rated): " + ", ".join(
            "%s %s" % (r["slug"], r["pr"] or r["sha"][:10]) for r in s["review_sample"]))
    return "\n".join(L)
