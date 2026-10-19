"""What `/sdlc:status` and "어디까지 됐어?" show: is the loop running and on what, the queue by phase, what needs you,
cost, recent merges, and a few auto-merged changes nobody has looked at yet. `collect` reads local refs and state files
only; `sdlc.py status` refreshes the remote refs before it calls it."""
from __future__ import annotations

import datetime as _dt
import os
import statistics
from typing import Any, Dict, List, Optional

from . import state as st
from .config import Config
from .events import read_events
from .gitops import Git, make_forge
from .queue import Item, counts, runnable, scan
from .util import epoch, parse_iso, read_json
from .workdoc import WorkDoc

SESSION_PHASES = ("design", "build", "fix", "self-check")      # the phases a claude session runs in (the gate, the merge do not)


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


LINE_CAP = 160        # characters of a "needs you" line or a manual-proof note


def one_line(text: object, cap: int = LINE_CAP) -> str:
    """`text` on one line: every run of whitespace (a newline included) is one space, cut at `cap` characters with an ellipsis."""
    t = " ".join(str(text if text is not None else "").split())
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def manual_proof(doc: Optional[WorkDoc]) -> List[str]:
    """The rows of the doc's Proof table whose kind is `manual` — what only a person can check — as `R3 — <first 80 characters
    of how>`."""
    out: List[str] = []
    for line in (doc.section("Proof") if doc is not None else "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if len(cells) >= 3 and cells[-1].strip("`*_ ").lower() == "manual" and cells[0].lower() != "r" and not set(cells[0]) <= set("-: "):
            out.append("%s — %s" % (cells[0], one_line(cells[1], 80)))
    return out


def collect(root: str) -> Dict[str, Any]:
    cfg = Config(root)
    git = Git(root)
    default = cfg.default_branch
    forge = make_forge(git, default, str(cfg.get("loop.forge", "auto")))      # it says whether changes are measured against origin's
    base = git.base_ref(default)
    records = st.load_items(root)
    items = scan(cfg, git, base, records)
    loop = st.read_status(root)
    pid = st.loop_pid(root)
    pause = os.path.join(root, str(cfg.get("loop.pause_file", ".sdlc/state/pause")))
    out: Dict[str, Any] = {"root": root, "mode": "solo" if cfg.solo else "team", "default_branch": default,
                           "legacy_config": cfg.legacy}
    in_session = bool(pid) and loop.get("phase") in SESSION_PHASES
    out["loop"] = {"running": bool(pid), "pid": pid, "paused": os.path.exists(pause), "state": loop.get("state", ""),
                   "item": loop.get("item", "") if pid else "", "phase": loop.get("phase", "") if pid else "",
                   "for": _ago(str(loop.get("phase_started", ""))) if pid else "", "log": loop.get("log", ""),
                   "session_log": loop.get("session_log", "") if pid else "", "wake_at": loop.get("wake_at", "") if pid else "",
                   "model": loop.get("model", "") if in_session else "", "effort": loop.get("effort", "") if in_session else "",
                   "last_stop": loop.get("stop", "") if not pid else "", "run_cost_usd": loop.get("cost_usd", 0)}
    # a start that was refused after the last run ended (a run that started since has written its record without `refused`)
    refused_at, ended = parse_iso(str(loop.get("refused_at") or "")), parse_iso(str(loop.get("ended") or ""))
    refused = bool(loop.get("refused")) and not pid and refused_at is not None and (ended is None or refused_at >= ended)
    out["loop"]["refused"] = str(loop["refused"]) if refused else ""
    out["loop"]["refused_ago"] = _ago(str(loop["refused_at"])) if refused else ""
    out["counts"] = counts(items)
    out["next"] = [it.slug for it in runnable(items)][:5]
    out["needs_you"] = needs_you(items, local=forge.kind == "local")
    out["items"] = [it.as_dict() for it in sorted(items.values(), key=lambda i: (i.created or "9999", i.slug))]
    out["cost"] = costs(root)
    merges = merges_on_record(root)

    def recent(e: Dict[str, Any]) -> Dict[str, Any]:
        det = e.get("detail") or {}
        slug = det.get("slug", "")
        person = e["by"] == "person"                  # its event carries no title and no cost: the item has them
        title = e.get("reason", "") or (items[slug].title if person and slug in items else "")
        cost = records.get(slug, {}).get("cost", 0) if person else det.get("cost_usd", 0)
        return {"slug": slug, "title": title, "pr": det.get("pr", ""), "ago": _ago(e.get("ts", "")), "cost_usd": cost,
                "by": e["by"]}
    out["recent_merges"] = [recent(e) for e in merges[-5:]][::-1]
    out["review_sample"] = review_sample(root, cfg, [e for e in merges if e["by"] == "loop"], git, base)   # a person's is not auto-merged
    return out


def merges_on_record(root: str) -> List[Dict[str, Any]]:
    """The merges the events record, oldest first, each with `by`: `loop` for the loop's own (`loop.merge`), `person` for a
    person's (`loop.item` / `merged-by-person` — every merge in team mode, and the merge of an item that needed one). A slug
    the loop merged is not also counted as a person's, and a person's merge written twice stands once."""
    loop = [dict(e, by="loop") for e in read_events(root, "loop.merge")]
    taken = {(e.get("detail") or {}).get("slug") for e in loop}
    person: Dict[str, Dict[str, Any]] = {}
    for e in read_events(root, "loop.item"):
        slug = (e.get("detail") or {}).get("slug", "")
        if e.get("decision") == "merged-by-person" and slug and slug not in taken:
            person[slug] = dict(e, by="person")             # the latest event of a slug stands
    return sorted(loop + list(person.values()), key=lambda e: e.get("ts", ""))


def needs_you(items: Dict[str, Item], local: bool = False) -> List[Dict[str, Any]]:
    """What waits for the user. `local`: there is no remote, so no PR — a person accepts a `needs-human` item by merging its
    branch, and the entry's `accept` says the command."""
    out: List[Dict[str, Any]] = []
    for it in sorted(items.values(), key=lambda i: (i.created or "9999", i.slug)):
        d = it.branch_doc or it.doc
        base = {"slug": it.slug, "title": it.title, "pr": it.rec.get("pr", "")}
        if it.phase == "needs-input":
            out.append(dict(base, kind="question", text=[one_line(l) for l in _first_lines(d.section("Questions") if d else "", 8)]))
        elif it.phase == "needs-human":
            reasons = it.rec.get("reasons")
            entry = dict(base, kind="human", text=[one_line(r) for r in (reasons if isinstance(reasons, list) else [reasons] if reasons else [])])
            if local and it.branch:
                entry["accept"] = "git merge sdlc/%s" % it.slug
            out.append(entry)
        elif it.phase == "approval":
            out.append(dict(base, kind="approve-design", text=["design PR waits for an approving review"]))
        elif it.phase == "awaiting-merge":
            out.append(dict(base, kind="merge", text=["gate passed — merge the PR"]))
        elif it.phase == "blocked":
            out.append(dict(base, kind="blocked", text=[one_line("failed %s times: %s" % (it.rec.get("failures"), it.rec.get("last_error", "")))]))
        elif it.phase == "proposed":
            out.append(dict(base, kind="proposed", text=[one_line(it.title)]))       # the title says what it is; the Goal's first lines may be a template hint
        elif it.wait_kind == "human":
            out.append(dict(base, kind="dependency", text=[one_line(it.wait)]))
    return out


def costs(root: str) -> Dict[str, Any]:
    """What the loop spent: the sessions it ran, summed from its events. A session the user ran by hand (`/sdlc:go`) is
    not in it — `covers` says so in the JSON, the label `loop cost:` in the text."""
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
    merged = [(e.get("detail") or {}).get("slug") for e in merges_on_record(root)]
    vals = [per_slug[s] for s in merged if s in per_slug and per_slug[s] > 0]
    return {"covers": "sessions the loop ran", "today_usd": round(day, 2), "week_usd": round(week, 2),
            "median_per_merged_usd": round(statistics.median(vals), 2) if vals else None, "merged_items_counted": len(vals)}


def review_sample(root: str, cfg: Config, merges: List[Dict[str, Any]], git: Optional[Git] = None,
                  base: str = "") -> List[Dict[str, Any]]:
    """Auto-merged changes from the last 7 days nobody rated yet. Those with a `manual` row in their Proof — the one thing the gate
    could not run, so nobody has seen it work — come first, named; then the rest, the costliest first (more turns, more risk)."""
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
        slug = det.get("slug", "")
        text = git.show(base, "%s/%s.md" % (cfg.rel("work"), slug)) if git is not None and base and slug else None
        recent.append({"slug": slug, "title": e.get("reason", ""), "pr": det.get("pr", ""),
                       "sha": det.get("sha", ""), "ago": _ago(e.get("ts", "")), "cost_usd": det.get("cost_usd", 0),
                       "manual_proof": manual_proof(WorkDoc.parse(text)) if text is not None else []})
    recent.sort(key=lambda r: (not r["manual_proof"], -float(r.get("cost_usd") or 0)))
    return recent[:n]


def render(s: Dict[str, Any]) -> str:
    L: List[str] = []
    lp = s["loop"]
    if lp["running"]:
        if lp["item"]:
            what = "%s %s for %s" % (lp["phase"] or "between items", lp["item"], lp["for"])
        elif lp["phase"] == "self-check":
            what = "self-check for %s" % lp["for"]
        else:
            what = lp["state"] or "running"
        if lp.get("model") or lp.get("effort"):
            what += " (%s)" % " · ".join(x for x in (lp.get("model") or "default", lp.get("effort")) if x)
        if lp.get("wake_at"):
            what += " (usage limit — wakes %s)" % lp["wake_at"]
        L.append("loop: running (pid %s) — %s · this run $%.2f" % (lp["pid"], what, float(lp.get("run_cost_usd") or 0)))
    else:
        L.append("loop: not running%s%s%s" % (" · paused" if lp["paused"] else "",
                                               " · last stop: %s" % lp["last_stop"] if lp.get("last_stop") else "",
                                               " · start refused %s ago: %s" % (lp["refused_ago"], lp["refused"])
                                               if lp.get("refused") else ""))
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
            if n.get("accept"):
                L.append("      accept as is: " + n["accept"])
    co = s["cost"]
    L.append("loop cost: today $%.2f · 7 days $%.2f%s" % (co["today_usd"], co["week_usd"],
             " · median per merged item $%.2f (%d)" % (co["median_per_merged_usd"], co["merged_items_counted"])
             if co["median_per_merged_usd"] is not None else ""))
    if s["recent_merges"]:
        def merged(m: Dict[str, Any]) -> str:
            return "%s (%s ago%s)" % (m["slug"], m["ago"], ", by a person" if m.get("by") == "person" else "")    # the loop's say nothing
        L.append("merged lately: " + ", ".join(merged(m) for m in s["recent_merges"]))
    if s["review_sample"]:
        def sampled(r: Dict[str, Any]) -> str:
            line = "%s %s" % (r["slug"], r["pr"] or r["sha"][:10])
            manual = r.get("manual_proof") or []
            if manual:
                line += " (%s%s)" % ("; ".join("manual proof: " + m for m in manual[:3]),
                                     "; +%d more" % (len(manual) - 3) if len(manual) > 3 else "")
            return line
        L.append("worth a look (auto-merged, not rated): " + ", ".join(sampled(r) for r in s["review_sample"]))
    return "\n".join(L)
