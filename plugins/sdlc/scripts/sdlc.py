#!/usr/bin/env python3
"""sdlc — the engine behind /sdlc:init, /sdlc:go, /sdlc:run and /sdlc:status.

Run it from inside the repository (or pass --root). Standard library only, Python 3.9+.

  run [--detach] [--items a,b] [--max-items N] [--max-minutes M] [--dry-run]   work the queue until it is empty
  status [--json]                    loop, queue, what needs you, cost
  enqueue [--new] [FILE.md ...]      put work docs on the queue (--new: every new doc in work/)
  stop [--after-item]                stop the loop now (the session is resumable) or after the current item
  resume                             clear a pause and start the loop in the background
  answer SLUG TEXT                   answer an item's question; the loop continues it
  retry SLUG                         clear an item's failures / "needs a person" and let the loop take it again
  drop SLUG [REASON]                 take an item off the queue (closes its PR)
  rate SLUG good|fix|reject [NOTE]   rate an auto-merged change; fix/reject with a note queues a follow-up
  gate SLUG [--json]                 run the merge gate on sdlc/SLUG (checked out, clean) — exit 0 pass, 1 fix, 2 person
  ship SLUG                          gate, then merge (team: hand to a person) — the end of an interactive /sdlc:go
  init [...]                         adopt the repository (see init --help)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sdlclib import state as st  # noqa: E402
from sdlclib.config import Config, NotInitialized  # noqa: E402
from sdlclib.util import eprint, now_iso, run, stamp, tail, read_text, write_json, read_json  # noqa: E402


def find_root(arg: str) -> str:
    if arg:
        return os.path.abspath(arg)
    r = run(["git", "rev-parse", "--show-toplevel"], cwd=os.getcwd())
    return r.out.strip() if r.ok and r.out.strip() else os.getcwd()


# ---------------------------------------------------------------------------------------------------------------------
def cmd_run(a: argparse.Namespace) -> int:
    from sdlclib.engine import Engine
    if a.detach:
        return detach(a)
    items = [s.strip() for s in a.items.split(",") if s.strip()] if a.items else None
    eng = Engine(a.root, items=items, max_items=a.max_items, max_minutes=a.max_minutes, claude_bin=a.claude_bin,
                 plugin_dir=a.plugin_dir, dry_run=a.dry_run)
    return eng.run()


def detach(a: argparse.Namespace) -> int:
    pid = st.loop_pid(a.root)
    if pid:
        print("the loop is already running (pid %d)" % pid)
        return 0
    log = os.path.join(a.root, ".sdlc", "logs", "loop-%s.out" % stamp())
    os.makedirs(os.path.dirname(log), exist_ok=True)
    cmd = [sys.executable, os.path.abspath(__file__), "--root", a.root, "run"]
    for flag, val in (("--items", a.items), ("--max-items", a.max_items), ("--max-minutes", a.max_minutes),
                      ("--claude-bin", a.claude_bin), ("--plugin-dir", a.plugin_dir)):
        if val not in (None, ""):
            cmd += [flag, str(val)]
    with open(log, "a", encoding="utf-8") as out:
        p = subprocess.Popen(cmd, cwd=a.root, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
    for _ in range(30):                         # preflight problems surface within a few seconds
        time.sleep(0.2)
        if p.poll() is not None:
            break
        if st.loop_pid(a.root) == p.pid:
            break
    if p.poll() is not None and p.returncode != 0:
        print(tail(read_text(log) or "", 10))
        return p.returncode
    print("loop started in the background (pid %d) — log: %s" % (p.pid, os.path.relpath(log, a.root)))
    print("check on it: sdlc.py status · stop it: sdlc.py stop")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    from sdlclib.status import collect, render
    s = collect(a.root)
    print(json.dumps(s, ensure_ascii=False, indent=2) if a.json else render(s))
    return 0


def _forge_and_git(root: str):
    from sdlclib.gitops import Git, make_forge
    cfg = Config(root)
    git = Git(root)
    return cfg, git, make_forge(git, cfg.default_branch, str(cfg.get("loop.forge", "auto")))


def cmd_enqueue(a: argparse.Namespace) -> int:
    from sdlclib.enqueue import EnqueueError, enqueue, new_docs, prepare
    cfg, git, forge = _forge_and_git(a.root)
    if cfg.legacy:
        eprint("this repository still has a version-1 sdlc layout — run /sdlc:init once to migrate it")
        return 2
    paths = [os.path.abspath(p) for p in a.files]
    if a.new:
        paths += [p for p in new_docs(cfg, git) if p not in paths]
    if not paths:
        eprint("nothing to queue: pass work doc files, or --new for every new doc in %s/" % cfg.rel("work"))
        return 2
    git.fetch()
    try:
        docs = prepare(cfg, git, git.base_ref(cfg.default_branch), paths)
        res = enqueue(cfg, git, forge, docs, paths)
    except EnqueueError as e:
        eprint("not queued:\n" + str(e))
        return 1
    print("queued %d item(s): %s — %s" % (len(docs), ", ".join("%s (%s)" % (d.slug, d.status) for d in docs), res["how"]))
    return 0


def cmd_stop(a: argparse.Namespace) -> int:
    cfg = Config(a.root)
    pause = os.path.join(a.root, str(cfg.get("loop.pause_file", ".sdlc/state/pause")))
    pid = st.loop_pid(a.root)
    if a.after_item:
        os.makedirs(os.path.dirname(pause), exist_ok=True)
        with open(pause, "w") as fh:
            fh.write(now_iso() + "\n")
        print("the loop stops after its current item%s" % ("" if pid else " (it is not running now; the next run waits for `resume`)"))
        return 0
    if not pid:
        print("the loop is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    print("stopping the loop (pid %d): the current session is asked to stop and recorded, so `resume` continues it" % pid)
    for _ in range(120):
        time.sleep(0.5)
        if not st.loop_pid(a.root):
            print("stopped")
            return 0
    print("still stopping (a session gets up to 45 s to finish its turn) — check `status`")
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    cfg = Config(a.root)
    pause = os.path.join(a.root, str(cfg.get("loop.pause_file", ".sdlc/state/pause")))
    if os.path.exists(pause):
        os.remove(pause)
    a.detach, a.items, a.max_items, a.max_minutes, a.dry_run = True, "", None, None, False
    return detach(a)


def _find_item(root: str, slug: str):
    from sdlclib.gitops import Git
    from sdlclib.queue import scan
    cfg = Config(root)
    git = Git(root)
    items = scan(cfg, git, git.base_ref(cfg.default_branch), st.load_items(root))
    return cfg, git, items.get(slug)


def cmd_answer(a: argparse.Namespace) -> int:
    cfg, git, it = _find_item(a.root, a.slug)
    if it is None:
        eprint("no work item '%s'" % a.slug)
        return 1
    if it.phase not in ("needs-input", "answer"):
        eprint("%s has no open question (phase: %s)" % (a.slug, it.phase))
        return 1
    text = " ".join(a.text).strip()
    st.mutate(a.root, a.slug, lambda r: r.setdefault("answers", []).append({"text": text, "ts": now_iso()}))
    running = bool(st.loop_pid(a.root))
    print("answer recorded for %s — %s" % (a.slug, "the running loop picks it up" if running else "start the loop to continue (`resume`)"))
    return 0


def cmd_retry(a: argparse.Namespace) -> int:
    st.update(a.root, a.slug, failures=None, state=None, reasons=None, last_error=None, stop=None)
    print("%s: failures and waits cleared — the loop takes it again" % a.slug)
    return 0


def cmd_drop(a: argparse.Namespace) -> int:
    cfg, git, forge = _forge_and_git(a.root)
    reason = " ".join(a.reason).strip() or "dropped by request"
    st.update(a.root, a.slug, state="dropped", reasons=[reason])
    if forge.kind == "github" and forge.state(a.slug) == "open":
        run(["gh", "pr", "close", forge.number(a.slug), "--comment", "sdlc: dropped — " + reason], cwd=a.root, timeout=60)
    print("%s dropped" % a.slug)
    return 0


def cmd_rate(a: argparse.Namespace) -> int:
    from sdlclib.enqueue import EnqueueError, enqueue, prepare
    from sdlclib.workdoc import WorkDoc
    path = os.path.join(a.root, st.RATINGS_REL)
    data = read_json(path, {}) or {}
    note = " ".join(a.note).strip()
    data[a.slug] = {"rating": a.rating, "note": note, "ts": now_iso()}
    write_json(path, data)
    from sdlclib.events import log_event
    log_event(a.root, "review.rating", a.rating, note, {"slug": a.slug})
    if a.rating == "good" or not note:
        print("rated %s: %s" % (a.slug, a.rating))
        return 0
    cfg, git, forge = _forge_and_git(a.root)
    base = git.base_ref(cfg.default_branch)
    n, slug = 1, "%s-fix" % a.slug[:70]
    while git.show(base, "%s/%s.md" % (cfg.rel("work"), slug)) is not None:
        n += 1
        slug = "%s-fix-%d" % (a.slug[:68], n)
    d = WorkDoc({"slug": slug, "title": "Follow-up to %s" % a.slug, "status": "ready" if a.rating == "fix" else "proposed",
                 "depends_on": [], "test_changes": "none", "created": now_iso(), "source": "review:%s" % a.slug}, "")
    d.set_section("Goal", "%s\n\n(Review of the auto-merged change `%s`, rated `%s`.)" % (note, a.slug, a.rating))
    p = os.path.join(a.root, ".sdlc", "state", "new", slug + ".md")
    d.save(p)
    try:
        docs = prepare(cfg, git, base, [p], source="review:%s" % a.slug)
        res = enqueue(cfg, git, forge, docs, [p])
    except EnqueueError as e:
        eprint("rated, but the follow-up was not queued: %s" % e)
        return 1
    print("rated %s: %s — follow-up %s queued as %s (%s)" % (a.slug, a.rating, slug, docs[0].status, res["how"]))
    return 0


def cmd_gate(a: argparse.Namespace) -> int:
    from sdlclib.gate import run_gate
    from sdlclib.gitops import Git
    cfg = Config(a.root)
    git = Git(a.root)
    if git.branch() != "sdlc/" + a.slug or git.dirty():
        eprint("check out sdlc/%s with a clean tree first" % a.slug)
        return 1
    res = run_gate(cfg, git, a.slug, git.base_ref(cfg.default_branch), run_commands=not a.no_commands)
    if a.json:
        print(json.dumps(dict(res.as_dict(), outputs=res.outputs), ensure_ascii=False, indent=2))
    else:
        print(res.summary())
        for name, out in res.outputs.items():
            print("\n--- %s ---\n%s" % (name, out))
    return 0 if res.passed else (1 if res.fixable or res.conflict else 2)


def cmd_ship(a: argparse.Namespace) -> int:
    from sdlclib.engine import Engine
    eng = Engine(a.root)
    code, msg = eng.ship_now(a.slug)
    print(msg)
    return code


def cmd_init(a: argparse.Namespace) -> int:
    from sdlclib import init as initmod
    return initmod.main(a.root, a.rest)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sdlc.py", description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 2)[-1])
    ap.add_argument("--root", default="", help="repository root (default: the git toplevel of the current directory)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("run")
    p.add_argument("--detach", action="store_true")
    p.add_argument("--items", default="")
    p.add_argument("--max-items", type=int, default=None)
    p.add_argument("--max-minutes", type=float, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--claude-bin", default="")
    p.add_argument("--plugin-dir", default="")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("enqueue")
    p.add_argument("--new", action="store_true")
    p.add_argument("files", nargs="*")
    p.set_defaults(fn=cmd_enqueue)

    p = sub.add_parser("stop")
    p.add_argument("--after-item", action="store_true")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("resume")
    p.add_argument("--claude-bin", default="")
    p.add_argument("--plugin-dir", default="")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("answer")
    p.add_argument("slug")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("retry")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_retry)

    p = sub.add_parser("drop")
    p.add_argument("slug")
    p.add_argument("reason", nargs="*")
    p.set_defaults(fn=cmd_drop)

    p = sub.add_parser("rate")
    p.add_argument("slug")
    p.add_argument("rating", choices=["good", "fix", "reject"])
    p.add_argument("note", nargs="*")
    p.set_defaults(fn=cmd_rate)

    p = sub.add_parser("gate")
    p.add_argument("slug")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-commands", action="store_true")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("ship")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_ship)

    p = sub.add_parser("init", add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_init)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    a.root = find_root(a.root)
    try:
        return a.fn(a)
    except NotInitialized as e:
        eprint(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
