"""Throwaway git repositories wired to the stub claude/gh, for the engine tests."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(PLUGIN, "scripts")
STUBS = os.path.join(HERE, "stubs")
SDLC = os.path.join(SCRIPTS, "sdlc.py")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

BASE_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0"}

CONFIG = {
    "version": 2,
    "mode": "solo",
    "commands": {"build": "true", "test": "test ! -f FAIL", "lint": ""},
    "loop": {"session_max_minutes": 1, "poll_minutes": 0.01, "idle_hours": 0.0005, "self_check": False},
}


def deep_update(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_update(a[k], v)
        else:
            a[k] = v
    return a


class Repo:
    def __init__(self, remote=False, config=None):
        self.base_env = dict(os.environ)
        self.tmp = tempfile.mkdtemp(prefix="sdlc-test-")
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        self.env_paths = {"STUB_CLAUDE_CALLS": os.path.join(self.tmp, "calls.jsonl"),
                          "STUB_CLAUDE_STATE": os.path.join(self.tmp, "stub-state.json"),
                          "STUB_CLAUDE_SCENARIO": os.path.join(self.tmp, "scenario.json"),
                          "STUB_GH_DB": os.path.join(self.tmp, "gh-db.json")}
        self.git("init", "-q", "-b", "main")
        cfg = deep_update(json.loads(json.dumps(CONFIG)), config or {})
        self.write(".sdlc/config.json", json.dumps(cfg, indent=2))
        self.write(".gitignore", ".sdlc/state/\n.sdlc/logs/\n.sdlc/worktrees/\n__pycache__/\n")
        self.write("tests/test_base.py", "def test_base():\n    assert 1 + 1 == 2\n")
        self.write("package.json", json.dumps({"name": "x", "dependencies": {"react": "^18.0.0"}}, indent=2))
        self.write("work/README.md", "work items\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.origin = ""
        if remote:
            self.origin = os.path.join(self.tmp, "origin.git")
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True, env=self.env())
            self.git("remote", "add", "origin", self.origin)
            self.git("push", "-q", "-u", "origin", "main")

    # ---------- environment ----------
    def env(self):
        e = dict(self.base_env)
        e.update(BASE_ENV)
        e.update(self.env_paths)
        e["PATH"] = STUBS + os.pathsep + e.get("PATH", "")
        e["SDLC_CLAUDE_BIN"] = e["CLAUDE_BIN"] = os.path.join(STUBS, "claude")     # both: no run here may reach a real claude
        e.pop("CLAUDECODE", None)
        return e

    def activate(self):
        """Point this process's environment at the stubs (the engine runs in-process)."""
        self._saved = dict(os.environ)
        env = self.env()
        os.environ.clear()
        os.environ.update(env)
        os.environ["CLAUDECODE"] = "1"          # the runner must drop it for the nested session

    def deactivate(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- files & git ----------
    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, text):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    def git(self, *args, check=True):
        r = subprocess.run(["git", "-C", self.root] + list(args), capture_output=True, text=True, env=self.env())
        if check and r.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), r.stderr))
        return r.stdout.strip()

    def show(self, ref, rel):
        r = subprocess.run(["git", "-C", self.root, "show", "%s:%s" % (ref, rel)], capture_output=True, text=True,
                           env=self.env())
        return r.stdout if r.returncode == 0 else None

    def origin_show(self, ref, rel):
        r = subprocess.run(["git", "-C", self.origin, "show", "%s:%s" % (ref, rel)], capture_output=True, text=True,
                           env=self.env())
        return r.stdout if r.returncode == 0 else None

    def doc_text(self, slug, status="ready", deps=(), title=None, goal="Do the thing.",
                 acceptance="R1. WHEN the user asks THE SYSTEM SHALL do the thing.", test_changes="none"):
        return ("---\nslug: %s\ntitle: %s\nstatus: %s\ndepends_on: [%s]\ntest_changes: %s\ncreated: 2026-10-12T0%d:00:00Z\n"
                "source: user\n---\n\n## Goal\n%s\n\n## Acceptance criteria\n%s\n" % (
                    slug, title or slug, status, ", ".join(deps), test_changes, len(slug) % 10, goal, acceptance))

    def add_items(self, *specs, push=True):
        """specs: slug or (slug, {deps, status, ...}); committed straight onto main (and pushed)."""
        for s in specs:
            slug, kw = (s, {}) if isinstance(s, str) else s
            self.write("work/%s.md" % slug, self.doc_text(slug, **kw))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "queue")
        if self.origin and push:
            self.git("push", "-q", "origin", "main")

    def status_of(self, slug, ref="main"):
        text = self.show(ref, "work/%s.md" % slug) or ""
        for line in text.splitlines():
            if line.startswith("status:"):
                return line.split(":", 1)[1].strip()
        return None

    # ---------- stubs ----------
    def scenario(self, data):
        with open(self.env_paths["STUB_CLAUDE_SCENARIO"], "w") as fh:
            json.dump(data, fh)

    def calls(self):
        try:
            with open(self.env_paths["STUB_CLAUDE_CALLS"]) as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except OSError:
            return []

    def gh_db(self):
        try:
            with open(self.env_paths["STUB_GH_DB"]) as fh:
                return json.load(fh)
        except OSError:
            return {"prs": []}

    def gh_db_write(self, db):
        with open(self.env_paths["STUB_GH_DB"], "w") as fh:
            json.dump(db, fh)

    def items_state(self):
        try:
            with open(self.path(".sdlc/state/items.json")) as fh:
                return json.load(fh)
        except OSError:
            return {}

    # ---------- engine ----------
    def engine(self, **kw):
        from sdlclib.engine import Engine
        e = Engine(self.root, claude_bin=os.path.join(STUBS, "claude"), **kw)
        e.retry_delay = 0.05
        e.limit_margin = 0.0
        return e

    def cli(self, *args, timeout=120):
        return subprocess.run([sys.executable, SDLC, "--root", self.root] + list(args), capture_output=True, text=True,
                              env=self.env(), timeout=timeout)


class RepoCase(unittest.TestCase):
    remote = False
    config = None

    def setUp(self):
        self.repo = Repo(remote=self.remote, config=self.config)
        self.repo.activate()

    def tearDown(self):
        self.repo.deactivate()
        if os.environ.get("SDLC_KEEP_TMP"):
            print("kept", self.repo.tmp)
        else:
            self.repo.cleanup()

    def keys(self):
        return [(c["key"], c["action"], c["resumed"], c["model"]) for c in self.repo.calls()]
