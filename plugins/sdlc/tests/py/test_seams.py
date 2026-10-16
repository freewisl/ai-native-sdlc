"""Seams of the 1.2 loop found in review: what a session inherits from the person's environment, a branch cut again, a
reviewer's commit on the design PR, a `done` item that takes commits from origin, the test runner's configuration under
`test_changes: allowed`, glob settings written as one string, the claim during a usage-limit sleep, the self-check's leftovers,
a build an older version stopped, and what `init --check` counts as waiting for a person."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from helpers import RepoCase, STUBS
from sdlclib import gate as G
from sdlclib import state as st
from sdlclib.config import DEFAULTS, Config, as_list
from sdlclib.gitops import Git
from sdlclib.session import PERSON_ONLY_ENV, Runner, SessionResult
from sdlclib.util import epoch
from test_engine_more import before_sessions, run_log, spy_gate
from test_gate_more import GateCase
from test_init import InitRepo, PACKAGE_JSON


def colleague(repo, branch, change, message="PERSON: a fix"):
    """Somebody else changes origin's `branch` from a clone of their own: `change(git)` edits that clone (`git(*args)` runs git
    in it, `git.root` is its folder); the commit is pushed on top of the branch. The commit's sha."""
    clone, env = tempfile.mkdtemp(prefix="colleague-", dir=repo.tmp), repo.env()

    def git(*args):
        return subprocess.run(["git", "-C", clone] + list(args), check=True, env=env, capture_output=True, text=True).stdout.strip()
    git.root = clone
    subprocess.run(["git", "clone", "-q", repo.origin, clone], check=True, env=env, capture_output=True)
    git("checkout", "-q", "-B", branch, "origin/" + branch)
    change(git)
    git("add", "-A")
    git("commit", "-q", "-m", message)
    git("push", "-q", "origin", "HEAD:refs/heads/" + branch)
    return git("rev-parse", "HEAD")


def write_in(git, rel, text):
    path = os.path.join(git.root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def old_branch_on_origin(repo, slug="a-first"):
    """A 0.x `sdlc/<slug>` that only origin has — no work doc on it. Its tip."""
    repo.git("checkout", "-q", "-b", "sdlc/" + slug)
    repo.write("plan/%s.md" % slug, "old plan\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "plan " + slug)
    repo.git("push", "-q", "origin", "sdlc/" + slug)
    old = repo.git("rev-parse", "sdlc/" + slug)
    repo.git("checkout", "-q", "main")
    repo.git("branch", "-q", "-D", "sdlc/" + slug)
    return old


def open_pr(number, head, approved=False):
    """A PR of the stub gh's db, as GitHub would hold one for a branch that was worked on before."""
    reviews = [{"author": {"login": "lead"}, "state": "APPROVED"}] if approved else []
    return {"number": number, "head": head, "headRefName": head, "base": "main", "title": "old work", "body": "", "isDraft": False,
            "state": "OPEN", "url": "https://github.test/o/r/pull/%d" % number, "labels": [], "reviews": reviews,
            "reviewDecision": "APPROVED" if approved else "", "author": {"login": "me"}, "comments": []}


# =====================================================================================================================
# 1. what a session inherits
class SessionEnvironment(RepoCase):
    config = {"protect": {"deploy_approval_env": "MY_APPROVAL"}}

    def record_sessions(self):
        """The environment of every claude the engine starts."""
        envs, real = [], subprocess.Popen

        def popen(cmd, *args, **kw):
            if str(cmd[0]).endswith("claude") and "--output-format" in cmd:             # a session, not `claude --help`
                envs.append(dict(kw.get("env") or os.environ))
            return real(cmd, *args, **kw)
        patcher = mock.patch("subprocess.Popen", popen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return envs

    def test_the_persons_approval_and_test_edit_switches_never_reach_a_loop_session(self):
        os.environ.update({"SDLC_ALLOW_TEST_EDIT": "1", "RELEASE_APPROVAL": "yes", "MY_APPROVAL": "ok", "KEEP_ME": "1"})
        self.repo.add_items("a-first")
        envs = self.record_sessions()
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(len(envs), 2)                                               # the design and the build session
        for env in envs:
            for name in ("SDLC_ALLOW_TEST_EDIT", "RELEASE_APPROVAL", "MY_APPROVAL", "CLAUDECODE"):
                self.assertNotIn(name, env)
            self.assertEqual(env["KEEP_ME"], "1")                                    # everything else is inherited as before
            self.assertIn("PATH", env)

    def test_a_runner_without_a_config_drops_the_default_names(self):
        os.environ.update({"SDLC_ALLOW_TEST_EDIT": "1", "RELEASE_APPROVAL": "yes", "KEEP_ME": "1"})
        envs = self.record_sessions()
        runner = Runner(self.repo.root, claude_bin=os.path.join(STUBS, "claude"))
        runner.run("hello", os.path.join(self.repo.tmp, "s.jsonl"), 1)
        (env,) = envs
        self.assertEqual(sorted(set(PERSON_ONLY_ENV) & set(env)), [])
        self.assertEqual(env["KEEP_ME"], "1")

    def test_the_configured_name_follows_the_config_when_it_changes_on_the_default_branch(self):
        e = self.repo.engine()
        self.assertEqual(e.runner.drop_env, ("MY_APPROVAL",))
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["protect"]["deploy_approval_env"] = "OTHER_APPROVAL"
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "another approval variable")
        e.refresh_config()
        self.assertEqual(e.runner.drop_env, ("OTHER_APPROVAL",))


# =====================================================================================================================
# 2. a branch cut again goes to origin as a replacement, not as a divergence
class FreshStartBeforeFirstPush(RepoCase):
    remote = True

    def test_an_attempt_that_ends_before_the_first_push_does_not_send_the_next_one_to_a_person(self):
        r = self.repo
        old = old_branch_on_origin(r)
        r.add_items("a-first")
        r.scenario({"a-first:build": ["gave_up"]})                                  # the first build fails once, nothing pushed yet
        self.assertEqual(r.engine().run(), 1)
        rec = r.items_state()["a-first"]
        self.assertEqual((rec["replaces"], rec["remote_sha"]), (old, old))
        e = r.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertNotIn("and the other way round", log)
        self.assertNotIn("HUMAN   a-first", log)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build", "a-first:build"])
        r.git("fetch", "-q")
        self.assertEqual(r.status_of("a-first", "origin/main"), "done")
        self.assertIsNone(r.origin_show("main", "plan/a-first.md"))                  # the old work did not come along
        rec = r.items_state()["a-first"]
        self.assertNotIn("replaces", rec)
        self.assertNotEqual(rec["remote_sha"], old)

    def test_sync_remote_keeps_the_old_tip_as_the_lease_and_reports_nothing(self):
        r = self.repo
        old = old_branch_on_origin(r)
        r.add_items("a-first")
        e = r.engine()
        e.checkout_item("a-first")                                                  # the branch is cut again from main
        r.git("fetch", "-q")
        self.assertEqual(e.sync_remote("a-first"), "")
        self.assertEqual(st.item(r.root, "a-first")["remote_sha"], old)
        self.assertEqual(r.git("rev-parse", "origin/sdlc/a-first"), old)             # nothing came in, nothing moved

    def test_origin_moving_past_the_old_tip_is_somebodys_push_and_still_goes_to_a_person(self):
        r = self.repo
        old_branch_on_origin(r)
        r.add_items("a-first")
        e = r.engine()
        e.checkout_item("a-first")
        colleague(r, "sdlc/a-first", lambda git: write_in(git, "PERSON.txt", "theirs\n"))
        r.git("fetch", "-q")
        why = e.sync_remote("a-first")
        self.assertIn("on origin has commits this checkout does not have, and the other way round", why)


class ReplacedPrCase(RepoCase):
    """The older work's PR is still open on GitHub when the branch is cut again: it is closed, never reused."""
    remote = True

    def prepare(self):
        old = old_branch_on_origin(self.repo)
        self.repo.gh_db_write({"next": 2, "prs": [open_pr(1, "sdlc/a-first", approved=True)], "calls": []})
        self.repo.add_items("a-first")
        return old


class ReplacedPrTeam(ReplacedPrCase):
    config = {"mode": "team"}

    def test_the_design_pr_is_a_new_one_and_the_old_approval_does_not_count(self):
        old = self.prepare()
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        prs = self.repo.gh_db()["prs"]
        self.assertEqual([(p["number"], p["state"]) for p in prs], [(1, "CLOSED"), (2, "OPEN")])
        self.assertEqual(prs[0]["comments"], ["replaced: sdlc/a-first was cut again from main; the earlier work stays as "
                                              "sdlc-old/a-first in the checkout that ran the loop"])
        self.assertTrue(prs[1]["isDraft"])
        self.assertEqual(prs[1]["reviews"], [])
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design"])           # no build: nobody approved the new design
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["pr"]), ("awaiting-approval", prs[1]["url"]))
        self.assertNotEqual(self.repo.git("rev-parse", "origin/sdlc/a-first"), old)


class ReplacedPrSolo(ReplacedPrCase):
    def test_solo_merges_the_new_pr_and_leaves_the_old_one_closed(self):
        self.prepare()
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        prs = self.repo.gh_db()["prs"]
        self.assertEqual([(p["number"], p["state"]) for p in prs], [(1, "CLOSED"), (2, "MERGED")])
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])

    def test_nothing_is_closed_for_a_branch_that_replaces_nothing(self):
        self.repo.add_items("a-first")
        self.repo.gh_db_write({"next": 2, "prs": [open_pr(1, "sdlc/a-first")], "calls": []})     # a PR of the branch the loop works on
        e = self.repo.engine()
        e.checkout_item("a-first")
        self.assertTrue(e.publish("a-first", draft=True)[0])
        self.assertEqual([c for c in self.repo.gh_db()["calls"] if c[:2] == ["pr", "close"]], [])
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")


# =====================================================================================================================
# 3. a reviewer's commit on the design PR is part of the design they approved
class ReviewerEditsTheDesign(RepoCase):
    remote = True
    config = {"mode": "team"}

    def reviewer_tightens_r1(self):
        text = self.repo.origin_show("sdlc/a-first", "work/a-first.md")
        assert "THE SYSTEM SHALL do the thing." in text
        new = text.replace("THE SYSTEM SHALL do the thing.", "THE SYSTEM SHALL do the thing, and log it.")
        return colleague(self.repo, "sdlc/a-first", lambda git: write_in(git, "work/a-first.md", new), "review: tighten R1")

    def approve(self):
        db = self.repo.gh_db()
        db["prs"][0]["reviews"].append({"author": {"login": "lead"}, "state": "APPROVED"})
        self.repo.gh_db_write(db)

    def test_the_build_is_gated_against_the_design_as_approved_and_does_not_go_to_a_person(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)                                # the design PR waits for approval
        first = self.repo.items_state()["a-first"]["design_sha"]
        reviewer = self.reviewer_tightens_r1()
        self.approve()
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["design_sha"], reviewer)
        self.assertNotEqual(reviewer, first)
        self.assertEqual(rec["state"], "awaiting-merge", rec.get("reasons"))         # the gate passed: a person merges the PR
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        self.assertIn("and log it.", self.repo.origin_show("sdlc/a-first", "work/a-first.md"))
        self.assertNotIn("acceptance criteria changed", run_log(e))

    def test_the_design_is_frozen_at_the_approval_so_a_later_change_of_the_criteria_is_still_caught(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.reviewer_tightens_r1()
        self.approve()
        self.assertEqual(self.repo.engine().run(), 0)
        sha = self.repo.items_state()["a-first"]["design_sha"]
        self.repo.git("checkout", "-q", "sdlc/a-first")
        doc = self.repo.read("work/a-first.md")
        self.repo.write("work/a-first.md", doc.replace("and log it.", "and do less."))
        self.repo.git("commit", "-qam", "a session changes the criteria")
        res = G.run_gate(Config(self.repo.root), Git(self.repo.root), "a-first", "origin/main", run_commands=False, design_sha=sha)
        self.assertTrue(any("acceptance criteria changed" in h for h in res.human), res.human)

    def test_a_commit_that_is_not_a_planned_design_never_becomes_the_design(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        design = self.repo.items_state()["a-first"]["design_sha"]
        text = self.repo.origin_show("sdlc/a-first", "work/a-first.md")
        built = text.replace("status: planned", "status: built")                       # what a build session leaves behind
        colleague(self.repo, "sdlc/a-first", lambda git: write_in(git, "work/a-first.md", built), "build")
        self.repo.git("fetch", "-q")
        e.design_approved("a-first")
        rec = self.repo.items_state()["a-first"]
        self.assertIs(rec["approved"], True)
        self.assertEqual(rec["design_sha"], design)

    def test_a_branch_that_does_not_continue_the_design_is_not_it_either(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        st.update(self.repo.root, "a-first", design_sha="0" * 40)                      # a record that names no commit of this repository
        self.assertEqual(e.approved_tip("a-first"), "")
        st.update(self.repo.root, "a-first", design_sha=None)                          # a record from before the design was kept
        self.assertEqual(e.approved_tip("a-first"), "")


# =====================================================================================================================
# 4. a `done` item that takes commits from origin goes through the gate again
class DoneItemTakesCommits(RepoCase):
    remote = True

    def merge_fails(self):
        """The gate passes, the doc says `done`, and the merge fails: the item waits for a person."""
        self.repo.add_items("a-first")
        os.environ["STUB_GH_MERGE_FAIL"] = "1"
        self.assertEqual(self.repo.engine().run(), 0)
        del os.environ["STUB_GH_MERGE_FAIL"]
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.assertIn("status: done", self.repo.origin_show("sdlc/a-first", "work/a-first.md"))

    def delete_a_test(self):
        return colleague(self.repo, "sdlc/a-first", lambda git: git("rm", "-q", "tests/test_base.py"), "person: fix")

    def test_a_persons_fix_that_deletes_a_test_is_gated_and_goes_back_to_them(self):
        self.merge_fails()
        self.delete_a_test()
        self.assertEqual(self.repo.cli("retry", "a-first").returncode, 0)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertIn("SYNC    a-first — took 1 commit(s)", log)
        self.assertIn("REGATE  a-first", log)
        self.assertRegex(log, r"GATE    a-first — .*test file deleted: tests/test_base.py")
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        self.assertTrue(any("tests/test_base.py" in x for x in rec["reasons"]), rec["reasons"])
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")               # not merged
        self.assertIsNotNone(self.repo.origin_show("main", "tests/test_base.py"))
        self.assertIn("status: built", self.repo.origin_show("sdlc/a-first", "work/a-first.md"))

    def test_a_harmless_fix_is_gated_too_and_then_ships(self):
        self.merge_fails()
        colleague(self.repo, "sdlc/a-first", lambda git: write_in(git, "PERSON.txt", "a fix\n"))
        self.assertEqual(self.repo.cli("retry", "a-first").returncode, 0)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertIn("REGATE  a-first", log)
        self.assertIn("GATE    a-first — passed", log)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertEqual(self.repo.origin_show("main", "PERSON.txt"), "a fix\n")

    def test_commits_that_do_not_come_in_leave_a_done_doc_as_it_is(self):
        self.merge_fails()
        self.assertEqual(self.repo.cli("retry", "a-first").returncode, 0)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertNotIn("REGATE", log)
        self.assertNotIn("GATE    a-first", log)                                      # nothing new to gate: it ships as before
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")

    def test_an_interactive_ship_gates_what_came_in_on_a_done_item(self):
        self.merge_fails()
        self.delete_a_test()
        self.repo.git("checkout", "-q", "sdlc/a-first")
        r = self.repo.cli("ship", "a-first")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("a person has to look at this", r.stdout + r.stderr)
        self.assertIn("test file deleted: tests/test_base.py", r.stdout + r.stderr)
        self.assertIsNotNone(self.repo.origin_show("main", "tests/test_base.py"))
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "built")

    def test_an_interactive_ship_of_a_done_item_with_nothing_new_still_ships_without_the_gate(self):
        self.merge_fails()
        self.repo.git("checkout", "-q", "sdlc/a-first")
        gate = spy_gate(self)
        code, text = self.repo.engine().ship_now("a-first")
        self.assertEqual(code, 0, text)
        self.assertEqual(gate, [])


# =====================================================================================================================
# 6. the test runner's configuration is a person's, whatever test_changes says
class TestConfigUnderAllowed(GateCase):
    CONFIGS = {"conftest.py": "import pytest\n", "pytest.ini": "[pytest]\naddopts = -q\n", "web/jest.config.js": "module.exports = {}\n"}

    def base(self):
        self.on_main(dict(self.CONFIGS, **{"tests/test_base.py": "def test_base():\n    assert 1 + 1 == 2\n"}))
        return self.design(test_changes="allowed")

    def test_an_existing_conftest_that_changes_goes_to_a_person_even_when_tests_may_change(self):
        sha = self.base()
        self.build(test_changes="allowed", files={"conftest.py": "import pytest\n\n\ndef pytest_collection_modifyitems(items):\n"
                                                                 "    items.clear()\n"})
        res = self.gate(design_sha=sha)
        self.assertFalse(res.passed)
        self.assertEqual(res.human, ["existing test configuration changed, which test_changes: allowed does not cover — "
                                     "conftest.py (modified)"])

    def test_every_kind_of_runner_configuration_counts_and_a_deletion_too(self):
        sha = self.base()
        self.build(test_changes="allowed", files={"pytest.ini": "[pytest]\naddopts = --ignore=tests\n",
                                                  "web/jest.config.js": "module.exports = { testMatch: [] }\n"}, deletes=["conftest.py"])
        res = self.gate(design_sha=sha)
        line = [h for h in res.human if h.startswith("existing test configuration changed")]
        self.assertEqual(len(line), 1, res.human)
        for what in ("pytest.ini (modified)", "web/jest.config.js (modified)", "conftest.py (deleted or renamed)"):
            self.assertIn(what, line[0])

    def test_a_changed_test_and_a_new_configuration_file_stay_free_under_allowed(self):
        sha = self.base()
        self.build(test_changes="allowed", files={"tests/test_base.py": "def test_base():\n    assert 2 == 1 + 1\n",
                                                  "tests/conftest.py": "import pytest\n", "vitest.config.ts": "export default {}\n"})
        res = self.gate(design_sha=sha)
        self.assertTrue(res.passed, res.summary())

    def test_under_none_the_message_is_the_one_about_the_design(self):
        self.on_main({"conftest.py": "import pytest\n"})
        sha = self.design()
        self.build(files={"conftest.py": "import pytest\n# x\n"})
        res = self.gate(design_sha=sha)
        self.assertEqual([h for h in res.human if h.startswith("existing test")],
                         ["existing tests changed, but the design said test_changes: none — conftest.py (modified)"])

    def test_a_project_that_switched_the_protection_off_is_not_asked(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["protect"] = {"protect_tests": False}
        self.on_main({".sdlc/config.json": json.dumps(cfg), "conftest.py": "import pytest\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed", files={"conftest.py": "import pytest\n# x\n"})
        self.assertEqual([h for h in self.gate(design_sha=sha).human if h.startswith("existing test")], [])

    def test_a_test_paths_list_without_auto_leaves_the_configuration_out_as_before(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["protect"] = {"test_paths": ["tests/**"]}
        self.on_main({".sdlc/config.json": json.dumps(cfg), "conftest.py": "import pytest\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed", files={"conftest.py": "import pytest\n# x\n"})
        self.assertEqual([h for h in self.gate(design_sha=sha).human if h.startswith("existing test")], [])


# =====================================================================================================================
# 7. a glob setting written as one string is a list of one
GLOB_KEYS = ["verify.commands", "gate.commands", "risk.human", "protect.generated_paths", "protect.test_paths",
             "protect.deploy_patterns", "secrets.extra_patterns"]


def list_keys(tree=DEFAULTS, prefix=""):
    out = []
    for k, v in tree.items():
        if isinstance(v, dict):
            out += list_keys(v, prefix + k + ".")
        elif isinstance(v, list):
            out.append(prefix + k)
    return out


class StringGlobSettings(GateCase):
    def configure(self, **protect_and_risk):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        for key, value in protect_and_risk.items():
            section, name = key.split("__")
            cfg.setdefault(section, {})[name] = value
        self.on_main({".sdlc/config.json": json.dumps(cfg), "gen/out.js": "x\n", "docs/guide.md": "g\n", "src/app.py": "a = 1\n"})

    def test_every_list_valued_setting_reads_a_string_as_a_list_of_one_and_an_empty_string_as_none(self):
        self.assertEqual(sorted(list_keys()), sorted(GLOB_KEYS))                      # a new list setting is covered by `get` too
        self.assertEqual((as_list("dist/**"), as_list(""), as_list("  "), as_list(["a"]), as_list(None)), (["dist/**"], [], [], ["a"], None))
        for key in GLOB_KEYS:
            section, name = key.split(".")
            for written, read in (("dist/**", ["dist/**"]), ("", [])):
                with self.subTest(key=key, written=written):
                    cfg = json.loads(self.repo.read(".sdlc/config.json"))
                    cfg.setdefault(section, {})[name] = written
                    self.repo.write(".sdlc/config.json", json.dumps(cfg))
                    self.assertEqual(Config(self.repo.root).get(key), read)
                    self.assertEqual(Config(self.repo.root).get(key, ["x"]), read)

    def test_a_setting_that_is_not_a_list_by_default_stays_a_string(self):
        self.assertEqual(Config(self.repo.root).get("mode"), "solo")
        self.assertEqual(Config(self.repo.root).get("loop.allowed_tools"), DEFAULTS["loop"]["allowed_tools"])
        self.assertEqual(Config(self.repo.root).get("protect.deploy_approval_env"), "RELEASE_APPROVAL")

    def test_test_paths_as_a_string_names_tests_and_nothing_else(self):
        self.configure(protect__test_paths="tests/**")
        cfg = Config(self.repo.root)
        self.assertTrue(G.is_test_path(cfg, "tests/test_base.py"))
        self.assertFalse(G.is_test_path(cfg, "src/app.py"))
        self.assertFalse(G.is_test_path(cfg, "docs/guide.md"))

    def test_generated_paths_as_a_string_are_held_to_at_the_merge_base_too(self):
        self.configure(protect__generated_paths="gen/**")
        self.design()
        self.build(files={"gen/out.js": "y\n", "src/app.py": "a = 2\n"})
        generated, frozen = G.protected_lists(Config(self.repo.root), Git(self.repo.root), "main", "HEAD")
        self.assertEqual(generated, ["gen/**"])
        self.assertEqual(G.protected_paths_changed(Config(self.repo.root), Git(self.repo.root), "main", "HEAD"),
                         ["generated path changed: gen/out.js (gen/**)"])

    def test_risk_paths_as_a_string_flag_what_they_name_and_nothing_else(self):
        self.configure(risk__human="docs/**")
        sha = self.design()
        self.build(files={"docs/guide.md": "g2\n", "src/app.py": "a = 2\n"})
        res = self.gate(design_sha=sha)
        self.assertEqual([h for h in res.human if h.startswith("risk path")], ["risk path: docs/guide.md (docs/**)"])

    def test_the_gate_commands_as_a_string_run_that_command(self):
        self.configure(gate__commands="test")
        sha = self.design()
        self.build(files={"src/app.py": "a = 2\n"})
        self.assertEqual(self.gate(design_sha=sha).ran, ["test"])

    def test_the_review_file_init_writes_names_a_generated_path_given_as_a_string_whole(self):
        repo = InitRepo()
        self.addCleanup(repo.cleanup)
        repo.write("package.json", PACKAGE_JSON)
        repo.write(".sdlc/config.json", json.dumps({"version": 2, "language": "en", "mode": "solo", "commands": {"test": "true"},
                                                    "protect": {"generated_paths": "dist/**"}}))
        repo.commit("package")
        repo.init()
        review = repo.read("REVIEW.md")
        self.assertIn("dist/**", review)
        self.assertNotIn("d, i, s, t", review)
        self.assertNotIn("TODO: generated paths", review)


# =====================================================================================================================
# 8. the claim on origin does not lapse while the loop sleeps out a usage limit
class ClaimDuringALimit(RepoCase):
    remote = True

    def limited(self, seconds):
        res = SessionResult()
        res.status, res.limit_kind, res.limit_reset, res.limit_text = "limited", "session", epoch() + seconds, "You've hit your session limit"
        return res

    def spy(self, e):
        """Swap the engine's sleep and the claim's renewal for recorders: (the sleeps, the renewals, in order)."""
        log = []
        e.sleep = lambda seconds: log.append(("sleep", seconds)) or True
        patcher = mock.patch.object(Git, "refresh_claim", lambda git, slug, token: log.append(("refresh", slug)) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        return log

    def test_the_interval_is_a_third_of_the_stale_time_and_at_most_half_an_hour(self):
        e = self.repo.engine()
        e.session_minutes = 1
        self.assertEqual(e.claim_interval(), 40.0)                                    # stale after 2 min
        e.session_minutes = 120
        self.assertEqual(e.claim_interval(), 1800.0)

    def test_a_sleep_of_hours_is_cut_up_and_the_claim_is_renewed_between_the_pieces(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        e.session_minutes = 120
        self.assertEqual(e.take_remote_claim("a-first"), "")
        log = self.spy(e)
        self.assertTrue(e.sleep_holding("a-first", 5 * 3600 + 100))
        sleeps = [s for what, s in log if what == "sleep"]
        self.assertEqual(sleeps, [1800.0] * 10 + [100.0])
        self.assertAlmostEqual(sum(sleeps), 5 * 3600 + 100)
        self.assertEqual([what for what, _ in log], ["sleep", "refresh"] * 10 + ["sleep"])   # not after the last piece: the session follows
        self.assertEqual({s for what, s in log if what == "refresh"}, {"a-first"})

    def test_a_stop_during_the_sleep_ends_it_at_once(self):
        e = self.repo.engine()
        calls = []
        e.sleep = lambda seconds: calls.append(seconds) or False
        self.assertFalse(e.sleep_holding("a-first", 3600))
        self.assertEqual(len(calls), 1)

    def test_a_limit_wait_in_a_session_renews_the_claim_the_loop_holds(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        e.session_minutes = 120
        e.limit_margin = 0.0
        self.assertEqual(e.take_remote_claim("a-first"), "")
        log = self.spy(e)
        e.handle_limit(self.limited(2 * 3600), "sonnet", "build", "a-first")
        sleeps = [s for what, s in log if what == "sleep"]
        self.assertAlmostEqual(sum(sleeps), 2 * 3600, delta=5)
        self.assertGreaterEqual(len(sleeps), 4)
        self.assertEqual([what for what, _ in log].count("refresh"), len(sleeps) - 1)

    def test_a_limit_wait_with_no_item_renews_nothing(self):
        e = self.repo.engine()
        e.limit_margin = 0.0
        log = self.spy(e)
        e.handle_limit(self.limited(2 * 3600), "opus", "self_check", "")
        self.assertEqual([what for what, _ in log if what == "refresh"], [])

    def test_the_claim_on_origin_is_really_renewed_during_a_real_sleep(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        e.session_minutes = 0.02                                                      # stale after 2.4 s, renewed every 0.8 s
        e.limit_margin = 0.0
        self.assertEqual(e.take_remote_claim("a-first"), "")
        before = e.git.claim_holder("a-first")
        self.assertIn(" since ", before)
        time.sleep(1.1)                                                               # the time of a claim has a resolution of one second
        e.handle_limit(self.limited(2.0), "sonnet", "build", "a-first")
        after = e.git.claim_holder("a-first")
        self.assertNotEqual(after, before)                                            # renewed: a newer time, still ours
        self.assertEqual(e.git.claim("a-first", e.token, 600), "ours")
        e.release_remote_claim("a-first")


# =====================================================================================================================
# 9, 10. the self-check
class SelfCheckSeams(RepoCase):
    config = {"loop": {"self_check": True, "self_check_min_items": 1}}

    def test_a_failed_self_check_leaves_what_was_merged_to_be_checked_again(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"self-check": ["max_turns"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(e.merged_since_check, ["a-first"])
        self.assertEqual((e.self_checks, e.self_check_misses), (0, 1))
        self.assertTrue(e.self_check_due())

    def test_every_kind_of_failure_restores_it(self):
        for status in ("timeout", "max_turns", "budget", "error"):
            with self.subTest(status):
                e = self.repo.engine()
                res = SessionResult()
                res.status = status
                self.assertEqual(e.self_check_not_ok(res, "opus", ["a-first", "b-second"], []), 0)
                self.assertEqual(e.merged_since_check, ["a-first", "b-second"])

    def test_a_check_that_keeps_failing_is_not_tried_a_third_time(self):
        e = self.repo.engine()
        e.from_sha = "0" * 40                                                         # main moved since the run began
        res = SessionResult()
        res.status = "timeout"
        e.merged_since_check = ["a-first"]
        self.assertTrue(e.self_check_due())
        for n in (1, 2):
            e.self_check_not_ok(res, "opus", ["a-first"], [])
            self.assertEqual(e.self_check_misses, n)
        self.assertFalse(e.self_check_due())

    def test_a_limit_is_no_miss_it_has_its_own_count(self):
        e = self.repo.engine()
        e.limit_margin = 0.0
        res = SessionResult()
        res.status, res.limit_kind, res.limit_reset, res.limit_text = "limited", "session", epoch() + 0.2, "You've hit your session limit"
        self.assertEqual(e.self_check_not_ok(res, "opus", ["a-first"], []), 1)
        self.assertEqual((e.self_check_misses, e.merged_since_check), (0, ["a-first"]))

    def commit_what_the_session_left(self, e, message="self-check: findings"):
        """A session on the default branch (the self-check is the only one there) commits everything it did, right as it ends."""
        real, heads = e.runner.run, []

        def run(*args, **kw):
            if self.repo.git("branch", "--show-current") != "main":
                return real(*args, **kw)                                              # a design or build session on its own branch
            heads.append(self.repo.git("rev-parse", "HEAD"))
            res = real(*args, **kw)
            self.repo.git("add", "-A")
            self.repo.git("commit", "-q", "-m", message)
            return res
        e.runner.run = run
        return heads

    def test_a_commit_the_self_check_makes_on_the_default_branch_is_taken_back(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_write": {"junk.txt": "x\n"}, "selfcheck_touch": ["package.json", "tests/test_base.py"]})
        e = self.repo.engine()
        heads = self.commit_what_the_session_left(e)
        self.assertEqual(e.run(), 0)
        self.assertEqual(len(heads), 1)
        self.assertEqual(self.repo.git("rev-parse", "main"), heads[0])               # main is where the check found it
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists(self.repo.path("junk.txt")))
        self.assertNotIn("_stub_touched", self.repo.read("package.json"))
        self.assertNotIn("touched by the stub", self.repo.read("tests/test_base.py"))
        self.assertNotIn("self-check: findings", self.repo.git("log", "--format=%s", "main"))
        self.assertIn("CHECK   the session committed on main — taken back", run_log(e))

    def test_a_work_doc_the_session_committed_is_still_queued_and_nothing_else_stays(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "fix-one", "status": "proposed"}], "selfcheck_write": {"junk.txt": "x\n"}})
        e = self.repo.engine()
        self.commit_what_the_session_left(e)
        self.assertEqual(e.run(), 0)
        self.assertIn("CHECK   1 new item(s): fix-one (proposed)", run_log(e))
        self.assertEqual(self.repo.status_of("fix-one"), "proposed")
        self.assertFalse(os.path.exists(self.repo.path("junk.txt")))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertNotIn("self-check: findings", self.repo.git("log", "--format=%s", "main"))

    def test_a_failed_session_that_committed_is_taken_back_too(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"self-check": ["max_turns"], "selfcheck_write": {"junk.txt": "x\n"}})
        e = self.repo.engine()
        heads = self.commit_what_the_session_left(e)
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.git("rev-parse", "main"), heads[0])
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists(self.repo.path("junk.txt")))

    def test_a_session_that_moved_to_another_branch_does_not_get_that_branch_reset(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        head = self.repo.git("rev-parse", "HEAD")
        self.repo.git("checkout", "-q", "-b", "elsewhere")
        self.repo.write("other.txt", "x\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "work on another branch")
        tip = self.repo.git("rev-parse", "HEAD")
        e.undo_commits("main", head)                                                  # the check began on main
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), tip)
        e.undo_commits("elsewhere", tip)                                              # nothing moved: nothing is undone
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), tip)


# =====================================================================================================================
# 13. a build an older version stopped
class UpgradeMidBuild(RepoCase):
    def stopped_by_an_older_version(self, e):
        """A planned item whose build session an earlier version stopped: the record has a `stop` and no `review_seen`."""
        e.checkout_item("a-first")
        st.update(self.repo.root, "a-first", stop={"sid": "11111111-2222-3333-4444-555555555555", "phase": "build", "at": time.time(),
                                                   "why": "turn cap", "fp": "x", "model": e.model_for("build", "a-first"), "extra": ""})
        self.assertNotIn("review_seen", st.item(self.repo.root, "a-first"))

    def test_continuing_it_does_not_cost_a_fix_round(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.scenario({"a-first:build": ["no_review"]})                          # the continuation does not call the reviewer
        gate = spy_gate(self)
        e = self.repo.engine()
        self.stopped_by_an_older_version(e)
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build"])
        self.assertEqual([g["reviewed"] for g in gate], [None])                       # unknown: the gate does not demand the review
        self.assertNotIn("review_seen", self.repo.items_state()["a-first"])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_continuation_that_does_call_the_reviewer_records_it(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        e = self.repo.engine()
        self.stopped_by_an_older_version(e)
        self.assertEqual(e.run(), 0)
        self.assertIs(self.repo.items_state()["a-first"]["review_seen"], True)

    def test_a_build_this_version_starts_is_still_held_to_the_review(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.scenario({"a-first:build": ["no_review"]})
        gate = spy_gate(self)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build", "a-first:fix"])
        self.assertEqual([g["reviewed"] for g in gate], [False, True])

    def test_a_build_this_version_stopped_has_the_key_and_keeps_it(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.scenario({"a-first:build": ["max_turns", "no_review"]})              # stopped and continued in one run
        gate = spy_gate(self)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build", "a-first:build", "a-first:fix"])
        self.assertEqual([g["reviewed"] for g in gate], [False, True])


# =====================================================================================================================
# 14a. `init --check` counts the items the queue calls blocked
class ItemsWaitingForAPerson(unittest.TestCase):
    def setUp(self):
        self.repo = InitRepo()
        self.addCleanup(self.repo.cleanup)
        self.repo.write("package.json", PACKAGE_JSON)
        self.repo.commit("package")
        self.repo.init()
        self.repo.commit("sdlc")

    def doc(self, slug, status="ready"):
        self.repo.write("work/%s.md" % slug, "---\nslug: %s\ntitle: %s\nstatus: %s\ncreated: 2026-10-12T01:00:00Z\n---\n\n"
                        "## Goal\nDo it.\n" % (slug, slug, status))

    def test_failures_block_an_item_and_a_state_nothing_writes_does_not(self):
        for slug in ("a-first", "b-second", "c-third", "d-fourth", "e-fifth"):
            self.doc(slug)
        self.doc("f-sixth", status="done")
        self.repo.commit("queue")
        self.repo.write(".sdlc/state/items.json", json.dumps({
            "a-first": {"state": "needs-human", "reasons": ["new dependency"]},
            "b-second": {"failures": 3},                                              # failed as often as loop.max_failures_per_slug
            "c-third": {"state": "blocked"},                                          # no code writes this
            "d-fourth": {"failures": 2},
            "f-sixth": {"state": "needs-human"}}))                                    # merged by a person since: the queue says done
        _, got = self.repo.check()
        self.assertEqual(got["items"]["status"], "warn")
        self.assertEqual(got["items"]["detail"], "waiting for a person: a-first, b-second")

    def test_the_failure_limit_is_the_configured_one(self):
        self.doc("a-first")
        self.doc("b-second")
        cfg = self.repo.config()
        cfg.setdefault("loop", {})["max_failures_per_slug"] = 2
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.commit("queue")
        self.repo.write(".sdlc/state/items.json", json.dumps({"a-first": {"failures": 2}, "b-second": {"failures": 1}}))
        _, got = self.repo.check()
        self.assertEqual(got["items"]["detail"], "waiting for a person: a-first")

    def test_nothing_waiting_is_no_row(self):
        self.doc("a-first")
        self.repo.commit("queue")
        _, got = self.repo.check()
        self.assertNotIn("items", got)


if __name__ == "__main__":
    unittest.main()
