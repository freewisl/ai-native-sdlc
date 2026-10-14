"""Unit tests: globs, work docs, config migration, gate heuristics, session parsing, enqueue checks, status, CLI."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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

    def test_migrate_v1_lists_as_dropped_only_what_is_not_carried_over(self):
        deploy = {"production": {"patterns": ["ship it"], "approval_env": "OK_PROD"}}
        new, notes = C.migrate_v1({"roles": {"solo": True}, "environments": deploy, "monitor": {}, "ci": {}})
        self.assertEqual(new["protect"]["deploy_patterns"], ["ship it"])
        self.assertEqual(new["mode"], "solo")
        self.assertEqual(notes, ["dropped (not used by 1.0): monitor, ci"])              # environments and roles.solo live on
        new, notes = C.migrate_v1({"roles": {"solo": False}, "environments": {"production": {"approval_env": "OK_PROD"}}})
        self.assertEqual((new["protect"], new["mode"]), ({"deploy_approval_env": "OK_PROD"}, "team"))
        self.assertEqual(notes, [])                                                       # nothing is left to be called dropped
        new, notes = C.migrate_v1({"environments": {"production": {"patterns": ["ship it"]}}})
        self.assertEqual(new["protect"], {"deploy_patterns": ["ship it"]})
        self.assertEqual(notes, [])
        new, notes = C.migrate_v1({"roles": {"autopilot": True}, "environments": {"production": {"autonomy": "gated"}},
                                   "gate": {}})
        self.assertEqual(notes, ["dropped (not used by 1.0): environments, gate, roles"])   # nothing of those was carried over

    def test_migrate_v1_does_not_carry_the_caps_0x_shipped_as_defaults(self):
        caps = "1.0's defaults apply (500 turns, 120 min per session)"
        for turns in (120, 200):
            new, notes = C.migrate_v1({"loop": {"max_turns": turns}})
            self.assertNotIn("max_turns", new["loop"])
            self.assertEqual(notes, ["loop.max_turns %d was a 0.x default — %s" % (turns, caps)])
        new, notes = C.migrate_v1({"loop": {"item_max_minutes": 45}})
        self.assertEqual((new["loop"], notes), ({}, ["loop.item_max_minutes 45 was a 0.x default — " + caps]))
        new, notes = C.migrate_v1({"loop": {"max_turns": 200, "item_max_minutes": 45}})
        self.assertEqual((new["loop"], notes), ({}, ["loop.max_turns 200, loop.item_max_minutes 45 were 0.x defaults — " + caps]))
        new, notes = C.migrate_v1({"loop": {"max_turns": 200, "item_max_minutes": 90}})              # one default, one choice
        self.assertEqual((new["loop"], notes), ({"session_max_minutes": 90}, ["loop.max_turns 200 was a 0.x default — " + caps]))

    def test_migrate_v1_carries_a_cap_somebody_chose_and_skips_one_equal_to_1_0s_default(self):
        new, notes = C.migrate_v1({"loop": {"max_turns": 300, "item_max_minutes": 90}})
        self.assertEqual((new["loop"], notes), ({"max_turns": 300, "session_max_minutes": 90}, []))
        new, notes = C.migrate_v1({"loop": {"max_turns": 500, "item_max_minutes": 120}})
        self.assertEqual((new["loop"], notes), ({}, []))                                            # nothing to carry, nothing to say
        self.assertEqual((C.DEFAULTS["loop"]["max_turns"], C.DEFAULTS["loop"]["session_max_minutes"]), (500, 120))

    def test_migrate_v1_names_the_dropped_policies_and_what_that_means(self):
        new, notes = C.migrate_v1({"policies": ["secure-api-review", {"name": "brand-voice"}, "", None, {}]})
        self.assertEqual(notes, ["policies dropped (secure-api-review, brand-voice): their skills in .claude/skills/ stay and still "
                                 "trigger on their own, but nothing applies them at review any more — name the ones the reviewer "
                                 "must apply in REVIEW.md", "dropped (not used by 1.0): policies"])      # and still in the generic list
        self.assertNotIn("policies", new)
        for none in ([], [None, ""], "secure-api-review", {"secure-api-review": True}):                 # no policy named: nothing to add
            self.assertEqual(C.migrate_v1({"policies": none})[1], ["dropped (not used by 1.0): policies"], none)
        self.assertEqual(C.migrate_v1({})[1], [])

    def test_migrate_v1_says_once_which_roles_keys_were_false_for_a_solo_repository(self):
        tail = "in 1.0 solo mode /sdlc:run and /sdlc:go merge what passes the gate — use mode team to have a person merge"
        for roles, keys in (({"solo": True, "autopilot": False}, "roles.autopilot=false"),
                            ({"solo": True, "auto_merge": False}, "roles.auto_merge=false"),
                            ({"solo": True, "autopilot": False, "auto_merge": False}, "roles.autopilot=false, roles.auto_merge=false")):
            self.assertEqual(C.migrate_v1({"roles": roles})[1], ["%s: %s" % (keys, tail)], roles)
        for roles in ({"solo": True, "autopilot": True, "auto_merge": True}, {"solo": True}, {"solo": False, "autopilot": False,
                                                                                                "auto_merge": False}):
            self.assertEqual(C.migrate_v1({"roles": roles})[1], [], roles)             # on, absent, or a team: a person merges anyway

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


class PullRequestNote(unittest.TestCase):
    """The line that says an item was allowed to change existing tests: for the PR body and the merge commit."""

    def doc(self, test_changes, **sections):
        d = W.WorkDoc({"slug": "x", "title": "X", "status": "built", "test_changes": test_changes}, "")
        d.set_section("Goal", "g")
        for name, body in sections.items():
            d.set_section(name, body)
        return d

    def note(self, test_changes="allowed", **sections):
        from sdlclib.engine import tests_allowed_note
        return tests_allowed_note(self.doc(test_changes, **sections))

    def test_only_allowed_gets_a_note(self):
        from sdlclib.engine import tests_allowed_note
        self.assertEqual(self.note("none", Plan="test_changes: allowed — because"), "")
        self.assertEqual(self.note(""), "")
        self.assertEqual(tests_allowed_note(None), "")
        self.assertEqual(self.note("allowed"), "Existing tests were allowed to change in this item (test_changes: allowed).")

    def test_the_reason_is_the_first_line_in_plan_then_departures_then_proof_that_speaks_of_it(self):
        head = "Existing tests were allowed to change in this item (test_changes: allowed) — "
        self.assertEqual(self.note(Proof="existing tests in a.py change: new rule", Departures="test_changes: from departures",
                                   Plan="1. step\n2. test_changes: allowed, a.py::t asserts the old shape"),
                         head + "a.py::t asserts the old shape")
        self.assertEqual(self.note(Proof="existing tests in a.py change: new rule", Departures="- `test_changes: allowed` — fixtures move"),
                         head + "fixtures move")
        self.assertEqual(self.note(Proof="| R | test | kind |\n|---|---|---|\n| R1 | a.py | unit |\n\n"
                                         "**test_changes:** allowed: the cart total moved"), head + "the cart total moved")
        self.assertEqual(self.note(Proof="Existing tests in tests/a.py assert the old shape."),
                         head + "Existing tests in tests/a.py assert the old shape.")

    def test_a_reason_that_starts_with_a_code_span_keeps_its_backtick(self):
        head = "Existing tests were allowed to change in this item (test_changes: allowed) — "
        reason = "`tests/test_smoke.py::test_empty_stdin_exits_zero` 는 빈 입력에서 stdout 이 `\"\"` 이길 기대한다."
        self.assertEqual(self.note(Proof="test_changes: " + reason), head + reason)
        self.assertEqual(self.note(Proof="- test_changes: allowed — " + reason), head + reason)
        self.assertEqual(self.note(Proof="1. `test_changes`: " + reason), head + reason)
        self.assertEqual(self.note(Proof="**test_changes**: " + reason), head + reason)

    def test_markup_around_the_setting_is_closed_with_it(self):
        head = "Existing tests were allowed to change in this item (test_changes: allowed) — "
        for line in ("`test_changes: allowed` — why", "**test_changes:** allowed — why", "**test_changes**: **allowed** — why",
                     "`test_changes` allowed: why", "test_changes = allowed; why", "* test_changes: allowed - why"):
            self.assertEqual(self.note(Plan=line), head + "why", line)

    def test_a_mention_that_is_not_the_setting_spelled_out_is_the_reason_as_written(self):
        head = "Existing tests were allowed to change in this item (test_changes: allowed) — "
        self.assertEqual(self.note(Plan="we need test_changes: allowed here"), head + "we need test_changes: allowed here")
        self.assertEqual(self.note(Plan="test_changes allowed because fixtures move"), head + "test_changes allowed because fixtures move")

    def test_a_line_that_only_repeats_the_setting_and_hint_comments_are_no_reason(self):
        self.assertEqual(self.note(Plan="1. step\ntest_changes: allowed"),
                         "Existing tests were allowed to change in this item (test_changes: allowed).")
        self.assertEqual(self.note(Plan="<!-- say which existing tests change and why (test_changes) -->"),
                         "Existing tests were allowed to change in this item (test_changes: allowed).")

    def test_a_long_reason_is_cut(self):
        note = self.note(Plan="test_changes: allowed — " + "x" * 400)
        self.assertEqual(len(note.split(" — ", 1)[1]), 200)


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


class CommandsFill(unittest.TestCase):
    """`only_fills_commands`: the one change to .sdlc/config.json the gate does not hand to a person."""

    def fills(self, old, new):
        return G.only_fills_commands(json.dumps(old) if not isinstance(old, str) else old,
                                     json.dumps(new) if not isinstance(new, str) else new)

    def test_unset_is_missing_null_or_an_empty_string(self):
        for before in ({}, {"test": None}, {"test": ""}):
            self.assertTrue(self.fills({"commands": before}, {"commands": {"test": "pytest"}}), before)
        self.assertTrue(self.fills({}, {"commands": {"test": "pytest"}}))               # no commands section at all

    def test_anything_else_in_the_commands_is_a_change(self):
        self.assertFalse(self.fills({"commands": {"test": "pytest"}}, {"commands": {"test": "pytest -x"}}))
        self.assertFalse(self.fills({"commands": {"test": "pytest"}}, {"commands": {}}))
        self.assertFalse(self.fills({"commands": {"test": "pytest"}}, {"commands": {"test": ""}}))
        self.assertFalse(self.fills({"commands": {}}, {"commands": {"test": " "}}))
        self.assertFalse(self.fills({"commands": {}}, {"commands": {"test": 1}}))
        self.assertFalse(self.fills({"commands": {}}, {"commands": {"test": ["pytest"]}}))
        self.assertFalse(self.fills({"commands": {}}, {"commands": "pytest"}))
        self.assertFalse(self.fills({"commands": "pytest"}, {"commands": {"test": "pytest"}}))

    def test_nothing_but_the_commands_may_differ(self):
        self.assertFalse(self.fills({"mode": "solo", "commands": {}}, {"mode": "team", "commands": {"test": "pytest"}}))
        self.assertFalse(self.fills({"commands": {}}, {"commands": {"test": "pytest"}, "protect": {}}))
        self.assertFalse(self.fills({"protect": {"protect_tests": False}}, {"protect": {"protect_tests": 0}}))   # `false` is not `0`
        self.assertFalse(self.fills({"protect": {"protect_tests": True}}, {"protect": {"protect_tests": 1}}))
        self.assertTrue(self.fills({"a": 1, "b": {"c": [1, 2]}}, {"b": {"c": [1, 2]}, "a": 1}))                  # order is not a change

    def test_a_file_that_is_not_a_json_object_is_never_a_fill(self):
        self.assertFalse(self.fills("{}", "{nope"))
        self.assertFalse(self.fills("{}", "[]"))
        self.assertFalse(self.fills("{}", "null"))
        self.assertFalse(G.only_fills_commands(None, "{}"))
        self.assertFalse(G.only_fills_commands("{}", None))

    def test_any_name_may_be_filled(self):
        """Not only the three the gate runs: a command nothing refers to does nothing, and filling `format` or `lint_file`
        adds a check."""
        for name in ("build", "test", "lint", "run", "format", "lint_file", "typecheck", "lint.js"):
            self.assertTrue(self.fills({"commands": {}}, {"commands": {name: "make " + name}}), name)
            self.assertTrue(self.fills({"commands": {name: ""}}, {"commands": {name: "make " + name}}), name)


class FixBrief(RepoCase):
    """What the fix session is told: the failures, and — when the gate ran commands the branch recorded itself — that
    those may not be made unable to fail."""

    def brief(self, **fields):
        res = G.GateResult()
        res.fixable, res.outputs = ["`make test` failed (exit 2)"], {"test": "2 failed"}
        for name, value in fields.items():
            setattr(res, name, value)
        return self.repo.engine().fix_brief(res)

    def test_a_gate_without_filled_commands_says_nothing_about_them(self):
        text = self.brief()
        self.assertIn("`make test` failed (exit 2)", text)
        self.assertNotIn("this branch's own", text)

    def test_filled_commands_are_named_with_the_rule_before_the_closing_instruction(self):
        text = self.brief(filled=["build", "test"], human=["new dependency: left-pad (package.json)"])
        line = ("The gate commands `build`, `test` come from this branch's own `.sdlc/config.json` and are this project's gate from "
                "now on. When one fails, fix the project, or the command line so that it really builds, tests or lints. Never "
                "make it something that cannot fail (`true`, `exit 0`, `|| true`, an empty run).")
        self.assertIn(line, text)
        self.assertLess(text.index("2 failed"), text.index(line))                    # after the output it is about
        self.assertLess(text.index(line), text.index("Also reported, for a person"))
        self.assertLess(text.index(line), text.index("Fix the cause"))


class ConfigFill(RepoCase):
    """`config_fill` — what a branch's config.json fills in, judged against where it left main: the comparison the guard and
    the gate share — and `Config.with_filled_commands`, which puts it where the config has no value."""

    def fill(self, change, move_main=None):
        """`config_fill` for a branch off main whose one change is `change(config dict)`; `move_main` then changes main."""
        r = self.repo
        r.git("checkout", "-q", "-B", "sdlc/x", "main")
        cfg = json.loads(r.read(".sdlc/config.json"))
        change(cfg)
        r.write(".sdlc/config.json", json.dumps(cfg, indent=2))
        r.git("commit", "-q", "--allow-empty", "-am", "config")
        r.git("checkout", "-q", "main")
        if move_main:
            cfg = json.loads(r.read(".sdlc/config.json"))
            move_main(cfg)
            r.write(".sdlc/config.json", json.dumps(cfg, indent=2))
            r.git("commit", "-qam", "main moves on")
        from sdlclib.gitops import Git
        return G.config_fill(Git(r.root), "main", "sdlc/x")

    def test_the_values_a_branch_gives_to_commands_that_had_none(self):
        # the project's config has `lint: ""` and no `run`, `format`, `typecheck`
        got = self.fill(lambda c: c["commands"].update(lint="ruff .", run="python app.py", format="black .", typecheck="tsc"))
        self.assertEqual(got, {"lint": "ruff .", "run": "python app.py", "format": "black .", "typecheck": "tsc"})

    def test_nothing_changed_is_a_fill_of_nothing_and_any_other_change_is_none(self):
        self.assertEqual(self.fill(lambda c: None), {})
        self.assertIsNone(self.fill(lambda c: c["commands"].update(test="true")))                  # a command with a value
        self.assertIsNone(self.fill(lambda c: c["commands"].update(lint="ruff .", build="make")))  # …beside a fill
        self.assertIsNone(self.fill(lambda c: (c["commands"].update(lint="ruff ."), c.update(mode="team"))))
        self.assertIsNone(self.fill(lambda c: c.pop("commands")))                                   # removing them

    def test_it_is_judged_against_where_the_branch_left_main_and_not_main_now(self):
        got = self.fill(lambda c: c["commands"].update(lint="ruff ."), move_main=lambda c: c["loop"].update(poll_minutes=5))
        self.assertEqual(got, {"lint": "ruff ."})
        # main filled the same command meanwhile: the branch still filled it, `with_filled_commands` keeps main's
        got = self.fill(lambda c: c["commands"].update(lint="ruff ."), move_main=lambda c: c["commands"].update(lint="flake8"))
        self.assertEqual(got, {"lint": "ruff ."})
        self.assertEqual(C.Config(self.repo.root).with_filled_commands(got).command("lint"), "flake8")

    def config_with(self, commands, gate=None):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["commands"] = commands
        if gate:
            cfg["gate"] = {"commands": gate}
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        return C.Config(self.repo.root)

    def test_a_filled_command_has_a_value_only_where_the_config_has_none(self):
        cfg = self.config_with({"build": "make", "test": "", "lint": None, "run": "  "})
        filled = cfg.with_filled_commands({"build": "ninja", "test": "pytest", "lint": "ruff .", "run": "python app.py",
                                           "typecheck": "tsc", "format": "black ."})
        self.assertEqual([filled.command(n) for n in ("build", "test", "lint", "run", "typecheck")],
                         ["make", "pytest", "ruff .", "python app.py", "tsc"])
        self.assertEqual(filled.command("format"), "auto")                 # the default is a value too
        self.assertEqual([cfg.command(n).strip() for n in ("build", "test", "lint", "run")], ["make", "", "", ""])

    def test_the_copy_leaves_the_original_alone(self):
        cfg = self.config_with({"build": "make"})
        copy = cfg.with_filled_commands({"test": "pytest"})
        self.assertEqual((cfg.command("test"), copy.command("test")), ("", "pytest"))
        self.assertIsNot(copy.data["commands"], cfg.data["commands"])
        self.assertEqual((copy.root, copy.get("mode"), copy.get("gate.commands")), (cfg.root, cfg.get("mode"), cfg.get("gate.commands")))

    def test_a_name_with_a_dot_is_looked_up_whole(self):
        cfg = self.config_with({"lint.js": "eslint ."})
        self.assertEqual(cfg.with_filled_commands({"lint.js": "oxlint"}).data["commands"]["lint.js"], "eslint .")

    def test_the_gate_commands_follow_the_filled_ones_and_the_list_stays_the_configs(self):
        cfg = self.config_with({"build": "make", "typecheck": ""}, gate=["build", "test", "typecheck"])
        self.assertEqual(cfg.gate_commands(), [("build", "make")])
        filled = cfg.with_filled_commands({"test": "pytest", "typecheck": "tsc --noEmit", "lint": "ruff ."})
        self.assertEqual(filled.gate_commands(), [("build", "make"), ("test", "pytest"), ("typecheck", "tsc --noEmit")])
        self.assertEqual(filled.get("gate.commands"), ["build", "test", "typecheck"])               # `lint` is no gate command


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


class ClaudeLookup(unittest.TestCase):
    """Which claude a session runs: --claude-bin, SDLC_CLAUDE_BIN, CLAUDE_BIN, then claude on PATH."""

    def lookup(self, claude_bin="", **env):
        base = {k: v for k, v in os.environ.items() if k not in ("SDLC_CLAUDE_BIN", "CLAUDE_BIN")}
        base["PATH"] = STUBS + os.pathsep + base.get("PATH", "")
        base.update(env)
        with mock.patch.dict(os.environ, base, clear=True):
            return S.Runner(tempfile.gettempdir(), claude_bin=claude_bin).claude

    def test_the_order_is_flag_then_sdlc_claude_bin_then_claude_bin_then_path(self):
        self.assertEqual(self.lookup("/flag/claude", SDLC_CLAUDE_BIN="/sdlc/claude", CLAUDE_BIN="/plain/claude"), "/flag/claude")
        self.assertEqual(self.lookup(SDLC_CLAUDE_BIN="/sdlc/claude", CLAUDE_BIN="/plain/claude"), "/sdlc/claude")
        self.assertEqual(self.lookup(CLAUDE_BIN="/plain/claude"), "/plain/claude")
        self.assertEqual(self.lookup(SDLC_CLAUDE_BIN="", CLAUDE_BIN="/plain/claude"), "/plain/claude")   # empty counts as unset
        self.assertEqual(self.lookup(), os.path.join(STUBS, "claude"))                                   # PATH: the stub comes first

    def test_a_runner_without_any_claude_is_not_available(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-dir"}, clear=True):
            rn = S.Runner(tempfile.gettempdir())
        self.assertEqual((rn.claude, rn.available()), ("", False))
        self.assertFalse(S.Runner(tempfile.gettempdir(), claude_bin="/nonexistent/claude").available())


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


class ResultStatus(RepoCase):
    """How a finished claude process becomes a status: what its result event says, and whether the engine asked it to stop
    (rc 130) or the time cap did (rc 124)."""

    def read(self, rc, result=None, **fields):
        log = self.repo.path(".sdlc/logs/classify.jsonl")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w") as fh:
            fh.write(json.dumps({"type": "system", "subtype": "init", "plugins": [{"name": "sdlc"}]}) + "\n")
            if result is not None:
                ev = {"type": "result", "subtype": "success", "is_error": False, "result": result, "num_turns": 6,
                      "terminal_reason": "completed"}
                ev.update(fields)
                fh.write(json.dumps(ev) + "\n")
        res = S.SessionResult()
        res.sid, res.log, res.rc = "sid-1", log, rc
        S.Runner(self.repo.root, claude_bin=os.path.join(STUBS, "claude"))._read(res)
        return res

    def test_an_interrupted_claude_that_reports_success_is_interrupted(self):
        r = self.read(130, "", terminal_reason="aborted_streaming")
        self.assertEqual((r.status, r.terminal, r.text), ("interrupted", "aborted_streaming", ""))

    def test_an_aborted_result_is_interrupted_whatever_the_exit_code(self):
        self.assertEqual(self.read(0, "", terminal_reason="aborted_streaming").status, "interrupted")
        self.assertEqual(self.read(0, "", terminal_reason="aborted_tools").status, "interrupted")
        self.assertEqual(self.read(130, "", terminal_reason="aborted_tools").status, "interrupted")

    def test_a_session_that_finished_itself_before_the_signal_landed_stays_ok(self):
        self.assertEqual(self.read(130, "SDLC_BUILT a-first").status, "ok")
        self.assertEqual(self.read(0, "SDLC_BUILT a-first").status, "ok")

    def test_an_interrupt_without_a_success_result_is_interrupted(self):
        self.assertEqual(self.read(130, None).status, "interrupted")
        self.assertEqual(self.read(130, "[Request interrupted by user]", subtype="error_during_execution", is_error=True,
                                   terminal_reason="aborted_streaming").status, "interrupted")

    def test_a_time_cap_is_a_timeout_even_though_its_sigint_leaves_an_aborted_result(self):
        self.assertEqual(self.read(124, "", terminal_reason="aborted_streaming").status, "timeout")

    def test_a_result_without_terminal_reason_keeps_its_old_reading(self):
        r = self.read(0, "done", terminal_reason="")
        self.assertEqual((r.status, r.terminal), ("ok", ""))
        self.assertEqual(self.read(0, "", subtype="error_max_turns", is_error=True, terminal_reason="").status, "max_turns")

    def test_a_spend_cap_is_its_own_status_and_stays_resumable_like_a_turn_cap(self):
        for subtype, status in (("error_max_turns", "max_turns"), ("error_max_budget_usd", "budget")):
            r = self.read(0, "", subtype=subtype, is_error=True)
            self.assertEqual((r.status, r.stopped), (status, True), subtype)


class CommitSubject(unittest.TestCase):
    """The subject of the commit that lands docs on the default branch says what the docs do there."""

    def doc(self, slug, status):
        return W.WorkDoc.parse("---\nslug: %s\ntitle: %s\nstatus: %s\n---\n\n## Goal\nx\n" % (slug, slug, status))

    def subject(self, statuses, **kw):
        from sdlclib.enqueue import message_for
        return message_for([self.doc("d%d" % i, s) for i, s in enumerate(statuses)], **kw).splitlines()[0]

    def test_ready_items_are_queued_proposals_proposed_and_dropped_items_dropped(self):
        self.assertEqual(self.subject(["ready"]), "sdlc: queue d0")
        self.assertEqual(self.subject(["ready", "ready", "ready"]), "sdlc: queue 3 work items")
        self.assertEqual(self.subject(["proposed"]), "sdlc: propose d0")
        self.assertEqual(self.subject(["proposed"] * 3), "sdlc: propose 3 work items")
        self.assertEqual(self.subject(["dropped", "dropped"]), "sdlc: drop 2 work items")

    def test_a_mix_is_a_queue_commit_and_a_named_verb_is_kept(self):
        self.assertEqual(self.subject(["ready", "proposed"]), "sdlc: queue 2 work items")
        self.assertEqual(self.subject(["proposed", "dropped"]), "sdlc: queue 2 work items")
        self.assertEqual(self.subject(["proposed"], verb="accept"), "sdlc: accept d0")


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


class DetachOutcome(unittest.TestCase):
    """What `run --detach` says about a loop that was gone before it could be called started."""

    @staticmethod
    def done(stop, merged=0, failed=0):
        return "== done  merged=%d awaiting=0 parked=0 needs_human=0 failed=%d stop=%s (0 min, $0.00)\n" % (merged, failed, stop)

    def outcome(self, text, rc):
        import contextlib
        import io
        import sdlc
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "loop.out")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(text)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = sdlc.ended_at_once(log, rc)
        return code, buf.getvalue()

    def test_an_empty_queue_and_a_queue_that_waits_for_people_are_nothing_to_do(self):
        code, out = self.outcome("== sdlc run /x\n" + self.done("queue empty"), 0)
        self.assertEqual(code, 0)
        self.assertIn("nothing to do: queue empty", out)
        code, out = self.outcome(self.done("nothing runnable — 3 item(s) wait for you"), 0)
        self.assertEqual(code, 0)
        self.assertIn("nothing to do: nothing runnable — 3 item(s) wait for you", out)

    def test_a_quick_queue_that_did_its_work_before_detach_looked_is_finished_not_nothing_to_do(self):
        code, out = self.outcome(self.done("queue empty", merged=2), 0)
        self.assertEqual(code, 0)
        self.assertIn("the loop has already finished — == done  merged=2", out)
        self.assertNotIn("nothing to do", out)

    def test_anything_else_shows_the_tail_and_fails(self):
        paused = "STOP    paused (.sdlc/state/pause exists) — `sdlc.py resume` removes it\n"
        code, out = self.outcome(paused, 0)
        self.assertEqual((code, out.strip()), (1, paused.strip()))              # a clean exit that is not "nothing to do"
        code, out = self.outcome("ERROR   the working tree has uncommitted changes\n", 2)
        self.assertEqual(code, 2)                                                # the loop's own code survives
        self.assertIn("uncommitted changes", out)
        code, out = self.outcome(self.done("stopped by SIGTERM"), 0)
        self.assertEqual(code, 1)
        self.assertIn("stop=stopped by SIGTERM", out)
        code, out = self.outcome(self.done("3 failures in a row", failed=3), 1)
        self.assertEqual(code, 1)
        self.assertIn("failed=3", out)
        self.assertEqual(self.outcome("", -15)[0], 1)                            # killed by a signal: not a negative exit code

    def test_the_last_done_line_decides(self):
        code, out = self.outcome(self.done("queue empty", merged=1) + self.done("queue empty"), 0)
        self.assertIn("nothing to do", out)


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

    def test_the_cost_line_says_it_covers_the_loop_only(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        out = self.repo.cli("status").stdout
        self.assertIn("loop cost: today $1.00 · 7 days $1.00 · median per merged item $1.00 (1)", out)
        self.assertNotIn("\ncost:", "\n" + out)
        cost = json.loads(self.repo.cli("status", "--json").stdout)["cost"]
        self.assertEqual((cost["covers"], cost["today_usd"], cost["week_usd"]), ("sessions the loop ran", 1.0, 1.0))

    def waiting_for_a_person(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["new_dep"]})
        self.assertEqual(self.repo.engine().run(), 0)

    def test_without_a_remote_a_needs_a_person_item_says_how_to_accept_it(self):
        self.waiting_for_a_person()
        text = self.repo.cli("status").stdout
        self.assertIn("  ! a-first — new dependency: left-pad", text)
        self.assertIn("      accept as is: git merge sdlc/a-first", text)
        entry = json.loads(self.repo.cli("status", "--json").stdout)["needs_you"][0]
        self.assertEqual((entry["kind"], entry["accept"]), ("human", "git merge sdlc/a-first"))

    def test_the_command_in_the_line_is_the_one_that_accepts_it(self):
        self.waiting_for_a_person()
        self.repo.git("merge", "-q", "--no-edit", "sdlc/a-first")
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual((s["needs_you"], s["counts"]), ([], {"done": 1}))

    def test_questions_and_proposals_name_no_merge_command(self):
        self.repo.add_items("a-first", ("c-idea", {"status": "proposed"}))
        self.repo.scenario({"a-first:design": ["needs_input"]})
        self.assertEqual(self.repo.engine().run(), 0)
        text = self.repo.cli("status").stdout
        self.assertIn("? a-first", text)
        self.assertNotIn("git merge", text)
        self.assertTrue(all("accept" not in n for n in json.loads(self.repo.cli("status", "--json").stdout)["needs_you"]))

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
