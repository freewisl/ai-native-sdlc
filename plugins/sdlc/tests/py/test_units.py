"""Unit tests: globs, work docs, config migration, gate heuristics, session parsing, enqueue checks, status, CLI."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import RepoCase, Repo, SCRIPTS, STUBS, BASE_ENV

from sdlclib import config as C
from sdlclib import gate as G
from sdlclib import session as S
from sdlclib import util as U
from sdlclib import workdoc as W


class Globs(unittest.TestCase):
    def test_patterns(self):
        cases = [("src/", "src/a/b.py", True), ("src/", "srcx/a.py", False), ("*.lock", "a/b/yarn.lock", True),
                 ("db/migrations/**", "db/migrations/2026/x.sql", True), ("db/migrations/**", "db/x.sql", False),
                 ("**/secrets/*", "a/secrets/k.txt", True), ("**/secrets/*", "secrets/k.txt", True),
                 ("infra/*.tf", "infra/main.tf", True), ("infra/*.tf", "infra/m/main.tf", False),
                 ("payments/**", "./payments/a.go", True), ("", "x", False)]
        for pat, path, want in cases:
            self.assertEqual(U.glob_match(pat, path), want, (pat, path))
        self.assertEqual(U.any_glob(["a/", "*.md"], "docs/x.md"), "*.md")


class WorkDocs(unittest.TestCase):
    TEXT = ("---\nslug: entry-crud\ntitle: 수입·지출 입력/수정/삭제\nstatus: ready\ndepends_on: [setup, db]\n"
            "test_changes: none\ncreated: 2026-10-12T09:30:00Z\nsource: user\n---\n\n## Goal\n가계부 항목.\n\n"
            "## Acceptance criteria\nR1. WHEN … THE SYSTEM SHALL …\n")

    def test_round_trip_and_fields(self):
        d = W.WorkDoc.parse(self.TEXT, "work/entry-crud.md")
        self.assertEqual((d.slug, d.title, d.status, d.depends_on), ("entry-crud", "수입·지출 입력/수정/삭제", "ready", ["setup", "db"]))
        self.assertEqual(W.WorkDoc.parse(d.dumps()).dumps(), d.dumps())
        self.assertEqual(d.section("acceptance criteria"), "R1. WHEN … THE SYSTEM SHALL …")
        self.assertEqual(W.problems(d, "entry-crud"), [])

    def test_sections_are_inserted_in_order_and_tally_read(self):
        d = W.WorkDoc.parse(self.TEXT)
        d.set_section("Review", "x\nSDLC_REVIEW_TALLY important=2 nits=5 validated=yes")
        d.set_section("Plan", "1. step")
        self.assertEqual([t for t, _ in d.sections], ["Goal", "Acceptance criteria", "Plan", "Review"])
        self.assertEqual(d.review_tally(), (2, 5, "yes"))
        d.set_section("Review", "SDLC_REVIEW_TALLY important=0 nits=1")
        self.assertEqual(d.review_tally(), (0, 1, ""))
        d.status = "planned"
        self.assertIn("status: planned", d.dumps())

    def test_problems(self):
        d = W.WorkDoc.parse("---\nslug: Bad Slug\nstatus: maybe\n---\n")
        p = " ".join(W.problems(d, "other"))
        for want in ("kebab-case", "does not match", "status 'maybe'", "no Goal"):
            self.assertIn(want, p)

    def test_quoted_values_survive(self):
        d = W.WorkDoc({"slug": "a", "title": "x: y # z"}, "## Goal\ng\n")
        self.assertEqual(W.WorkDoc.parse(d.dumps()).title, "x: y # z")


class Config(unittest.TestCase):
    def test_migrate_v1(self):
        raw = {"version": 1, "language": "ko", "roles": {"solo": True}, "commands": {"test": "npm test", "deploy": "x"},
               "protect": {"ticketed_paths": ["payments/"], "default_branch": "main", "protect_tests": True},
               "environments": {"production": {"patterns": ["deploy prod"], "approval_env": "OK_PROD"}},
               "loop": {"model": "opus", "item_max_minutes": 90, "max_items": 5}, "monitor": {}, "ci": {"mode": "local"}}
        new, notes = C.migrate_v1(raw)
        self.assertEqual(new["mode"], "solo")
        self.assertEqual(new["commands"], {"test": "npm test"})
        self.assertEqual(new["risk"]["human"], ["payments/"])
        self.assertEqual(new["protect"]["deploy_patterns"], ["deploy prod"])
        self.assertEqual(new["protect"]["deploy_approval_env"], "OK_PROD")
        self.assertEqual(new["loop"]["session_max_minutes"], 90)
        self.assertNotIn("max_items", new["loop"])
        self.assertTrue(any("ci" in n and "monitor" in n for n in notes))
        self.assertEqual(C.migrate_v1({"roles": {"solo": False}})[0]["mode"], "team")

    def test_config_reads_defaults_and_legacy(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".sdlc"))
        with open(os.path.join(d, ".sdlc", "config.json"), "w") as fh:
            json.dump({"version": 1, "roles": {"solo": True}, "commands": {"build": "make", "test": "make test"}}, fh)
        c = C.Config(d)
        self.assertTrue(c.legacy)
        self.assertEqual(c.gate_commands(), [("build", "make"), ("test", "make test")])
        self.assertEqual(c.get("loop.max_turns"), 500)
        with self.assertRaises(C.NotInitialized):
            C.Config(tempfile.mkdtemp())


class GateHeuristics(unittest.TestCase):
    def test_dependency_parsers(self):
        cases = {
            "package.json": ('{"dependencies": {"react": "1"}, "devDependencies": {"vitest": "1"}}', {"react", "vitest"}),
            "requirements.txt": ("flask==2.0\n# c\n-r base.txt\nRequests>=2\n", {"flask", "requests"}),
            "pyproject.toml": ('[project]\ndependencies = [\n  "httpx>=0.27",\n  "rich",\n]\n[tool.poetry.dependencies]\n'
                               'python = "^3.11"\npydantic = "^2"\n', {"httpx", "rich", "pydantic"}),
            "go.mod": ("module x\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n)\nrequire golang.org/x/net v0.1.0\n",
                       {"github.com/pkg/errors", "golang.org/x/net"}),
            "Cargo.toml": ('[dependencies]\nserde = "1"\n[dev-dependencies]\ntokio = { version = "1" }\n', {"serde", "tokio"}),
            "Gemfile": ("gem 'rails'\n  gem \"pg\"\n", {"rails", "pg"}),
            "build.gradle.kts": ('implementation("com.squareup.okhttp3:okhttp:4.12.0")\nimplementation(libs.androidx.core)\n',
                                 {"com.squareup.okhttp3:okhttp", "libs.androidx.core"}),
            "libs.versions.toml": ('[libraries]\ncore = { module = "androidx.core:core-ktx", version = "1" }\n',
                                   {"androidx.core:core-ktx"}),
            "pom.xml": ("<dependency><groupId>org.x</groupId><artifactId>y</artifactId></dependency>", {"org.x:y"}),
            "Package.swift": ('.package(url: "https://github.com/a/b.git", from: "1.0.0")', {"https://github.com/a/b.git"}),
            "Podfile": ("pod 'Alamofire'\n", {"Alamofire"}),
            "pubspec.yaml": ("name: x\ndependencies:\n  http: ^1.0.0\n  flutter:\n    sdk: flutter\ndev_dependencies:\n  test: any\n",
                             {"http", "flutter", "test"}),
            "composer.json": ('{"require": {"laravel/framework": "^11"}}', {"laravel/framework"}),
        }
        for name, (text, want) in cases.items():
            self.assertTrue(G.is_manifest("app/" + name), name)
            self.assertEqual(G.deps_of("app/" + name, text), want, name)
        self.assertFalse(G.is_manifest("src/package.ts"))

    def test_test_patterns(self):
        for line in ("def test_x():", "    async def test_y(self):", "func TestParse(t *testing.T) {", "it('works', () => {",
                     "test.each([1])('n', () => {", "@Test", "func testLogin() throws {", "fn test_add() {", "describe('a', () => {"):
            self.assertTrue(G.TEST_DEF_RE.search(line), line)
        for line in ("it.skip('x')", "describe.only(", "@pytest.mark.skip", "@pytest.mark.xfail(reason='x')", "@Disabled",
                     "t.Skip(\"later\")", "try XCTSkipIf(true)", "xit('x'", "test.todo('x')"):
            self.assertTrue(G.SKIP_RE.search(line), line)
        self.assertFalse(G.SKIP_RE.search("const skipped = true"))

    def test_test_paths(self):
        r = Repo()
        try:
            cfg = C.Config(r.root)
            for p, want in (("tests/test_a.py", True), ("src/a_test.go", True), ("web/a.test.tsx", True),
                            ("app/src/test/java/FooTest.java", True), ("spec/models/a_spec.rb", True),
                            ("src/a.py", False), ("work/test-plan.md", False), ("src/latest.py", False)):
                self.assertEqual(G.is_test_path(cfg, p), want, p)
        finally:
            r.cleanup()


class GateIntegrity(RepoCase):
    def test_removed_case_and_assertion_and_skip(self):
        r = self.repo
        r.write("tests/test_more.py", "def test_one():\n    assert f(1) == 1\n\ndef test_two():\n    assert f(2) == 2\n")
        r.git("add", "-A")
        r.git("commit", "-qm", "more tests")
        r.git("checkout", "-qb", "sdlc/x")
        r.write("tests/test_more.py", "def test_one():\n    assert f(1) == 1\n")
        r.write("tests/test_base.py", "import pytest\n\n@pytest.mark.skip\ndef test_base():\n    assert 1 + 1 == 2\n")
        r.write("tests/test_new.py", "def test_new():\n    assert True\n")
        r.git("add", "-A")
        r.git("commit", "-qm", "weaken")
        from sdlclib.gitops import Git
        issues = G.test_integrity(C.Config(r.root), Git(r.root), "main", "HEAD")
        text = " | ".join(issues)
        self.assertIn("1 test case(s) removed from tests/test_more.py", text)
        self.assertIn("1 assertion(s) removed from tests/test_more.py", text)
        self.assertIn("skip/only added in tests/test_base.py", text)
        self.assertNotIn("test_new.py", text)


class Sessions(unittest.TestCase):
    def test_classify_limit(self):
        self.assertEqual(S.classify_limit("You've hit your session limit · resets 3pm", None)[0], "session")
        self.assertEqual(S.classify_limit("You've hit your weekly limit", None)[0], "weekly")
        self.assertEqual(S.classify_limit("You've hit your Opus limit · resets 5pm", None)[0], "opus")
        self.assertEqual(S.classify_limit("You've hit your Sonnet weekly limit", None)[0], "sonnet")
        k, reset, _ = S.classify_limit("", {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": 1760000000})
        self.assertEqual((k, reset), ("weekly", 1760000000.0))
        self.assertEqual(S.classify_limit("", {"status": "rejected", "rateLimitType": "five_hour"})[0], "session")
        self.assertEqual(S.classify_limit("all fine", None), ("", 0.0, ""))

    def test_parse_stream_and_token(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.jsonl")
        with open(p, "w") as fh:
            fh.write('{"type":"sdlc_session","sid":"x"}\nnot json\n')
            fh.write(json.dumps({"type": "system", "subtype": "init", "plugins": [{"name": "sdlc"}, {"name": "other"}]}) + "\n")
            fh.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}) + "\n")
            fh.write(json.dumps({"type": "result", "subtype": "success", "result": "ok\nSDLC_PLANNED a-b\n", "num_turns": 4}) + "\n")
        info = S.parse_stream(p)
        self.assertEqual(info["plugins"], ["sdlc", "other"])
        self.assertIsNone(info["rate"])
        self.assertEqual(info["result"]["num_turns"], 4)
        r = S.SessionResult()
        r.text = info["result"]["result"]
        self.assertEqual(r.token("SDLC_PLANNED"), "a-b")
        self.assertIsNone(r.token("SDLC_BUILT"))


class RunnerCase(RepoCase):
    def runner(self):
        return S.Runner(self.repo.root, claude_bin=os.path.join(STUBS, "claude"), max_turns=7, allowed_tools="Read,Bash")

    def test_cost_is_a_delta_per_session_and_the_command_is_complete(self):
        self.repo.add_items("a-first")
        self.repo.git("checkout", "-qb", "sdlc/a-first")
        self.repo.scenario({"a-first:design": ["max_turns", "ok"]})
        rn = self.runner()
        log = self.repo.path(".sdlc/logs/s1.jsonl")
        r1 = rn.run("/sdlc:go --item a-first --design", log, 1, model="opus", effort="high")
        self.assertEqual((r1.status, r1.cost_delta, r1.plugin_loaded), ("max_turns", 0.5, True))
        r2 = rn.run("continue /sdlc:go --item a-first --design", log, 1, model="opus", resume_sid=r1.sid)
        self.assertEqual((r2.status, r2.cost_total, r2.cost_delta), ("ok", 1.0, 0.5))
        cmd = rn.command("p", "sid", False, "opus", "high")
        for flag in ("--output-format", "stream-json", "--verbose", "--max-turns", "7", "--permission-prompts", "none",
                     "--allowedTools", "Read,Bash", "--session-id", "--model", "--effort"):
            self.assertIn(flag, cmd)

    def test_time_cap_stops_the_session(self):
        self.repo.add_items("a-first")
        self.repo.git("checkout", "-qb", "sdlc/a-first")
        self.repo.scenario({"a-first:design": ["sleep:30"]})
        t = time.time()
        r = self.runner().run("/sdlc:go --item a-first --design", self.repo.path(".sdlc/logs/s.jsonl"), 0.03)
        self.assertEqual(r.status, "timeout")
        self.assertLess(time.time() - t, 15)


class EnqueueChecks(RepoCase):
    def prep(self, *paths):
        from sdlclib.enqueue import prepare
        from sdlclib.gitops import Git
        return prepare(C.Config(self.repo.root), Git(self.repo.root), "main", list(paths))

    def test_refuses_bad_docs(self):
        from sdlclib.enqueue import EnqueueError
        self.repo.add_items("old-one")
        bad = [self.repo.write("work/Bad.md", self.repo.doc_text("Bad")),
               self.repo.write("work/x-a.md", self.repo.doc_text("x-a", deps=["x-b"])),
               self.repo.write("work/x-b.md", self.repo.doc_text("x-b", deps=["x-a"])),
               self.repo.write("work/x-c.md", self.repo.doc_text("x-c", deps=["nope"])),
               self.repo.write("work/x-d.md", self.repo.doc_text("x-d", status="done"))]
        with self.assertRaises(EnqueueError) as cm:
            self.prep(*bad)
        msg = str(cm.exception)
        for want in ("kebab-case", "dependency cycle", "'nope'", "status 'done'"):
            self.assertIn(want, msg)
        self.repo.write("work/old-one.md", self.repo.doc_text("old-one", goal="changed"))
        with self.assertRaises(EnqueueError):
            self.prep(self.repo.path("work/old-one.md"))

    def test_local_enqueue_commits_on_main_and_fills_fields(self):
        p = self.repo.write("work/new-one.md", "---\nslug: new-one\ntitle: New\nstatus: ready\n---\n\n## Goal\ng\n")
        r = self.repo.cli("enqueue", "--new")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.repo.show("main", "work/new-one.md")
        for want in ("created: ", "source: user", "depends_on: []", "test_changes: none"):
            self.assertIn(want, text)
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_enqueue_while_a_loop_runs_here_goes_to_the_inbox(self):
        from sdlclib import state as st
        from sdlclib.enqueue import drain_inbox, enqueue
        from sdlclib.gitops import Git, LocalForge
        st.write_pid(self.repo.root)
        st.write_status(self.repo.root, {"root": self.repo.root})
        p = self.repo.write("work/new-one.md", self.repo.doc_text("new-one"))
        cfg, git = C.Config(self.repo.root), Git(self.repo.root)
        docs = self.prep(p)
        res = enqueue(cfg, git, LocalForge(git, "main"), docs, [p])
        self.assertIn("inbox", res["how"])
        self.assertFalse(os.path.exists(p))
        self.assertIsNone(self.repo.show("main", "work/new-one.md"))
        self.assertEqual(drain_inbox(cfg, git, LocalForge(git, "main")), ["new-one"])
        self.assertIsNotNone(self.repo.show("main", "work/new-one.md"))
        self.assertEqual(os.listdir(self.repo.path(".sdlc/state/inbox")), [])
        st.clear_pid(self.repo.root)


class StatusAndCli(RepoCase):
    def test_status_on_a_fresh_queue(self):
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}), ("c-idea", {"status": "proposed"}))
        r = self.repo.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("loop: not running", r.stdout)
        self.assertIn("2 ready", r.stdout)
        self.assertIn("next: a-first", r.stdout)
        self.assertIn("+ c-idea", r.stdout)
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual(s["counts"], {"design": 2, "proposed": 1})

    def test_uninitialized_and_legacy_repos(self):
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", d], env=dict(os.environ, **BASE_ENV))
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "sdlc.py"), "--root", d, "status"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("/sdlc:init", r.stderr)
        self.repo.write(".sdlc/config.json", json.dumps({"version": 1, "roles": {"solo": True}}))
        self.repo.git("commit", "-qam", "legacy")
        r = self.repo.cli("run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("migrate", r.stdout + r.stderr)

    def test_drop_and_rate(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.cli("drop", "a-first", "not", "needed").returncode, 0)
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual(s["counts"], {"dropped": 1})
        r = self.repo.cli("rate", "a-first", "fix", "the", "total", "is", "off", "by", "one")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.repo.show("main", "work/a-first-fix.md")
        self.assertIn("status: ready", text)
        self.assertIn("the total is off by one", text)
        self.assertIn("source: review:a-first", text)


if __name__ == "__main__":
    unittest.main()
