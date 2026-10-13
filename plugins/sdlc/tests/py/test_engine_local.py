"""The loop on a repository without a remote: squash merges on main, the same gate."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest

from helpers import RepoCase, SDLC, STUBS


class HappyPath(RepoCase):
    def test_two_items_with_a_dependency_are_designed_built_gated_and_merged(self):
        self.repo.add_items(("b-second", {"deps": ["a-first"]}), "a-first")
        code = self.repo.engine().run()
        self.assertEqual(code, 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build", "b-second:design", "b-second:build"])
        for slug in ("a-first", "b-second"):
            self.assertEqual(self.repo.status_of(slug), "done")
            self.assertEqual(self.repo.show("main", "src/%s.txt" % slug), "built %s\n" % slug)
            self.assertNotIn("sdlc/" + slug, self.repo.git("branch", "--list"))
        log = self.repo.git("log", "--format=%s", "main")
        self.assertIn("a-first (sdlc/a-first)", log)
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        # every session ran on its item branch, with the nested-session marker removed
        for c in self.repo.calls():
            self.assertEqual(c["branch"], "sdlc/" + c["key"].split(":")[0])
            self.assertIsNone(c["claudecode"])
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["sessions"], 2)
        self.assertAlmostEqual(rec["cost"], 1.0)
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            events = [json.loads(l) for l in fh]
        self.assertEqual([e["detail"]["slug"] for e in events if e["event"] == "loop.merge"], ["a-first", "b-second"])

    def test_dry_run_lists_the_queue_without_sessions(self):
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}))
        self.assertEqual(self.repo.engine(dry_run=True).run(), 0)
        self.assertEqual(self.repo.calls(), [])
        with open(self._latest_log()) as fh:
            log = fh.read()
        self.assertIn("WOULD   a-first", log)
        self.assertIn("WAITS   b-second", log)

    def _latest_log(self):
        d = self.repo.path(".sdlc/logs")
        return os.path.join(d, sorted(n for n in os.listdir(d) if n.startswith("run-"))[-1])


class Continuations(RepoCase):
    def test_turn_cap_resumes_the_same_session_while_warm_and_counts_cost_once(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_turns", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        calls = self.repo.calls()
        builds = [c for c in calls if c["key"] == "a-first:build"]
        self.assertEqual(len(builds), 2)
        self.assertFalse(builds[0]["resumed"])
        self.assertTrue(builds[1]["resumed"])
        self.assertEqual(builds[0]["sid"], builds[1]["sid"])
        self.assertEqual(self.repo.status_of("a-first"), "done")
        # design 0.5 + build 0.5 + resumed build +0.5 (the stub reports 1.0 cumulative: the delta is 0.5)
        self.assertAlmostEqual(self.repo.items_state()["a-first"]["cost"], 1.5)

    def test_cold_stop_starts_a_fresh_continue_session(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_turns", "ok"]})
        e = self.repo.engine()
        e.cold_seconds = 0
        self.assertEqual(e.run(), 0)
        builds = [c for c in self.repo.calls() if c["key"] == "a-first:build"]
        self.assertFalse(builds[1]["resumed"])
        self.assertNotEqual(builds[0]["sid"], builds[1]["sid"])
        self.assertIn("--continue", builds[1]["prompt"])
        self.assertIn("turn cap", builds[1]["prompt"])

    def test_a_stop_without_progress_is_a_failure(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_turns", "max_turns_noprogress", "max_turns_noprogress"]})
        self.assertEqual(self.repo.engine().run(), 1)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["failures"], 1)
        self.assertIn("no progress", rec["last_error"])
        self.assertEqual(self.repo.status_of("a-first"), "ready")
        # the stopped work was committed on the item branch, the checkout is back on main and clean
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertIsNotNone(self.repo.show("sdlc/a-first", "src/a-first-partial-0.txt"))

    def test_continuations_are_capped(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["max_turns"] * 6})
        e = self.repo.engine()
        e.max_continuations = 2
        self.assertEqual(e.run(), 1)
        self.assertEqual(len([c for c in self.repo.calls() if c["key"] == "a-first:design"]), 3)
        self.assertIn("continuations", self.repo.items_state()["a-first"]["last_error"])

    def test_session_that_ends_without_finishing_fails_and_the_item_is_not_retried_in_the_same_run(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:design": ["gave_up"]})
        self.assertEqual(self.repo.engine().run(), 1)
        keys = [k for k, *_ in self.keys()]
        self.assertEqual(keys.count("a-first:design"), 1)
        self.assertEqual(self.repo.status_of("b-second"), "done")
        # the next run takes it again
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_three_failures_block_the_item(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["gave_up"] * 3})
        for _ in range(3):
            self.repo.engine().run()
        self.assertEqual(self.repo.items_state()["a-first"]["failures"], 3)
        self.repo.engine().run()
        self.assertEqual(len(self.repo.calls()), 3)
        out = self.repo.cli("status", "--json")
        s = json.loads(out.stdout)
        self.assertEqual([n["kind"] for n in s["needs_you"]], ["blocked"])
        self.repo.cli("retry", "a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_service_errors_are_retried_and_do_not_count_against_the_item(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["infra", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertNotIn("failures", self.repo.items_state()["a-first"])


class Limits(RepoCase):
    def test_session_limit_that_resets_soon_is_waited_out(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_session:1", "ok"]})
        t = time.time()
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertGreaterEqual(time.time() - t, 0.5)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_limit_beyond_the_wait_stops_the_run_and_the_next_run_continues(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_session:7200", "ok"]})
        e = self.repo.engine()
        e.limit_wait = 60
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "ready")
        stop = self.repo.items_state()["a-first"]["stop"]
        self.assertEqual(stop["phase"], "build")
        e2 = self.repo.engine()
        self.assertEqual(e2.run(), 0)
        builds = [c for c in self.repo.calls() if c["key"] == "a-first:build"]
        self.assertTrue(builds[1]["resumed"])     # within the warm window
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_model_family_limit_switches_model(self):
        # design runs on opus and build on sonnet (the defaults): an opus limit in the design moves it to sonnet…
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["limit_opus", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        designs = [c for c in self.repo.calls() if c["key"] == "a-first:design"]
        self.assertEqual([d["model"] for d in designs], ["opus", "sonnet"])
        self.assertFalse(designs[1]["resumed"])   # another model: the cache would not help
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_sonnet_limit_in_the_build_switches_to_opus(self):
        # …and a sonnet limit in the build moves it to opus
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_sonnet", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        builds = [c for c in self.repo.calls() if c["key"] == "a-first:build"]
        self.assertEqual([b["model"] for b in builds], ["sonnet", "opus"])
        self.assertFalse(builds[1]["resumed"])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_session_without_the_plugin_stops_the_run(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["no_plugin"]})
        self.assertEqual(self.repo.engine().run(), 2)
        self.assertEqual(len(self.repo.calls()), 1)


class Questions(RepoCase):
    def test_needs_input_parks_the_item_and_an_answer_continues_it(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:design": ["needs_input", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("b-second"), "done")
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "needs-input")
        s = json.loads(self.repo.cli("status", "--json").stdout)
        q = [n for n in s["needs_you"] if n["slug"] == "a-first"][0]
        self.assertEqual(q["kind"], "question")
        self.assertIn("Which currency?", q["text"][0])
        r = self.repo.cli("answer", "a-first", "KRW", "only")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertIn("**Answer**", self.repo.show("main", "work/a-first.md"))
        self.assertIn("KRW only", self.repo.show("main", "work/a-first.md"))

    def test_answer_refuses_an_item_without_a_question(self):
        self.repo.add_items("a-first")
        r = self.repo.cli("answer", "a-first", "x")
        self.assertEqual(r.returncode, 1)


class QuestionsByPhase(RepoCase):
    """The answer goes back to the phase that asked: a design question redoes the design, a build question the build."""

    def test_a_question_asked_during_design_sends_the_answer_back_to_design(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["needs_input", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["parked_phase"], "design")
        self.assertEqual(self.repo.cli("answer", "a-first", "KRW").returncode, 0)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:design", "a-first:build"])
        self.assertNotIn("parked_phase", self.repo.items_state()["a-first"])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_question_asked_during_build_sends_the_answer_back_to_build_not_to_design(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["needs_input", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["parked_phase"], "build")
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "needs-input")
        self.assertEqual(self.repo.cli("answer", "a-first", "KRW", "please").returncode, 0)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build", "a-first:build"])   # one design only
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertIn("KRW please", self.repo.show("main", "work/a-first.md"))
        self.assertIn("src/a-first.txt", self.repo.git("ls-tree", "-r", "--name-only", "main"))


class DesignChecks(RepoCase):
    """A design session that ends "ok" with the doc flipped to planned but no Plan or no Proof has not designed anything."""

    def check(self, action):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:design": [action]})
        self.assertEqual(self.repo.engine().run(), 1)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["failures"], rec["last_error"]), (1, "design left Plan/Proof empty"))
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "ready")       # back to ready: the next try designs again
        self.assertNotIn("a-first:build", [k for k, *_ in self.keys()])                 # nothing is built on an empty plan
        self.assertEqual(self.repo.status_of("b-second"), "done")                       # the loop went on to the next item
        self.assertIn("design left Plan/Proof empty — back to ready", self.repo.git("log", "--format=%s", "sdlc/a-first"))
        self.assertEqual(self.repo.engine().run(), 0)                                   # next run: design again, this time for real
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:design"), 2)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_no_plan_and_no_proof_section_at_all(self):
        self.check("design_empty")

    def test_sections_that_hold_only_the_templates_hint_comments(self):
        self.check("design_empty:comment")

    def test_a_plan_without_a_proof(self):
        self.check("design_empty:plan")

    def test_a_question_does_not_need_a_plan(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["needs_input"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "needs-input")
        self.assertNotIn("failures", self.repo.items_state()["a-first"])

    def test_filled_ignores_comments_and_whitespace(self):
        from sdlclib.engine import filled
        self.assertFalse(filled(""))
        self.assertFalse(filled("  \n\n"))
        self.assertFalse(filled("<!-- a -->\n\n<!--\nmultiline\n-->  "))
        self.assertTrue(filled("<!-- a -->\n1. step"))
        self.assertTrue(filled("1. step <!-- trailing -->"))
        self.assertTrue(filled("| R | check | kind |"))


class Gate(RepoCase):
    def test_failing_test_command_gets_a_fix_session_on_the_fix_model(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["fail_gate"]})
        self.assertEqual(self.repo.engine().run(), 0)
        keys = self.keys()
        self.assertEqual([k for k, *_ in keys], ["a-first:design", "a-first:build", "a-first:fix"])
        self.assertEqual(keys[2][3], "sonnet")
        fix = [c for c in self.repo.calls() if c["key"] == "a-first:fix"][0]
        self.assertIn("test ! -f FAIL", fix["prompt"])
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertIsNone(self.repo.show("main", "FAIL"))

    def test_open_important_findings_are_fixable(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["important"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertIn("a-first:fix", [k for k, *_ in self.keys()])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_missing_tally_is_fixable(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["no_tally"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_fix_rounds_are_capped(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["fail_gate"], "a-first:fix": ["gave_up", "gave_up", "gave_up"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 1)
        # a fix session that changes nothing still gets a re-gate (the failure may have been flaky), up to fix_rounds
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:fix"), 2)
        self.assertIn("gate still failing after 2 fix round(s)", self.repo.items_state()["a-first"]["last_error"])

    def test_new_dependency_waits_for_a_person_and_the_loop_moves_on(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["new_dep"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("b-second"), "done")
        self.assertEqual(self.repo.status_of("a-first"), "ready")
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        self.assertIn("left-pad", " ".join(rec["reasons"]))
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual([n["kind"] for n in s["needs_you"]], ["human"])

    def test_deleted_test_and_added_skip_wait_for_a_person(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["delete_test"], "b-second:build": ["skip_test"]})
        self.repo.engine().run()
        st = self.repo.items_state()
        self.assertIn("test file deleted: tests/test_base.py", " ".join(st["a-first"]["reasons"]))
        self.assertIn("skip/only added", " ".join(st["b-second"]["reasons"]))

    def test_acceptance_criteria_changed_after_design_waits_for_a_person(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["change_acceptance"]})
        self.repo.engine().run()
        self.assertIn("acceptance criteria changed", " ".join(self.repo.items_state()["a-first"]["reasons"]))

    def test_acceptance_criteria_changed_by_the_build_of_an_item_planned_on_main_wait_for_a_person(self):
        self.repo.add_items(("a-first", {"status": "planned"}))               # designed before any branch existed
        self.repo.scenario({"a-first:build": ["change_acceptance"]})
        self.repo.engine().run()
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build"])
        self.assertIn("acceptance criteria changed", " ".join(self.repo.items_state()["a-first"]["reasons"]))

    def test_a_refinement_made_on_main_after_the_branch_was_cut_is_not_a_change_by_the_branch(self):
        from sdlclib.config import Config
        from sdlclib.gate import acceptance_changed
        from sdlclib.gitops import Git
        r = self.repo
        r.add_items(("a-first", {"status": "planned"}))
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        r.write("src/a-first.txt", "built\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "code only")
        r.git("checkout", "-q", "main")
        r.write("work/a-first.md", r.doc_text("a-first", status="planned", acceptance="R1. WHEN refined THE SYSTEM SHALL do more."))
        r.git("commit", "-qam", "a person refines the criteria on main")
        cfg, git = Config(r.root), Git(r.root)
        self.assertIsNone(acceptance_changed(cfg, git, "a-first", "main", "sdlc/a-first"))
        r.git("checkout", "-q", "sdlc/a-first")
        r.write("work/a-first.md", r.doc_text("a-first", status="built", acceptance="R1. WHEN anything THE SYSTEM SHALL do less."))
        r.git("commit", "-qam", "the build rewrites the criteria")
        self.assertIn("acceptance criteria changed", acceptance_changed(cfg, git, "a-first", "main", "sdlc/a-first"))

    def test_risk_path_waits_for_a_person(self):
        self.repo.add_items("a-first")
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["risk"] = {"human": ["src/"]}
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "risk")
        self.repo.engine().run()
        self.assertIn("risk path: src/a-first.txt", " ".join(self.repo.items_state()["a-first"]["reasons"]))

    def test_uncommitted_leftovers_are_committed_on_the_branch_before_the_gate(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["dirty_built"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertIsNotNone(self.repo.show("main", "LEFTOVER-a-first"))

    def test_conflict_with_main_gets_a_fix_session(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["conflict"]})
        self.assertEqual(self.repo.engine().run(), 0)
        fixes = [c for c in self.repo.calls() if c["key"] == "a-first:fix"]
        self.assertEqual(len(fixes), 1)
        self.assertIn("conflicts", fixes[0]["prompt"])
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertEqual(self.repo.show("main", "src/a-first.txt"), "built a-first\n")


class ExistingTests(RepoCase):
    """The build adds test files freely; an existing one stays as it was unless the design said test_changes: allowed."""

    def reasons(self, slug):
        return " | ".join(self.repo.items_state()[slug]["reasons"])

    def config_change(self, **sections):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        for name, values in sections.items():
            cfg.setdefault(name, {}).update(values)
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "config")

    def allowed_doc(self, slug, line=""):
        text = self.repo.doc_text(slug, test_changes="allowed") + ("\n## Departures\n%s\n" % line if line else "")
        self.repo.write("work/%s.md" % slug, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "queue")

    def branch_with(self, status_on_main, on_branch):
        """An item planned on main (as designed), and a branch whose build wrote `on_branch` into the doc and the test."""
        r = self.repo
        r.add_items(("a-first", {"status": "planned", "test_changes": status_on_main}))
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        r.write("tests/test_base.py", "def test_base():\n    assert True\n")
        r.write("work/a-first.md", r.doc_text("a-first", status="built", test_changes=on_branch))
        r.git("commit", "-qam", "the build")

    def test_an_existing_test_edited_in_place_waits_for_a_person_though_no_case_or_assertion_is_lost(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.assertIn("existing tests changed, but the design said test_changes: none — tests/test_base.py (modified)",
                      self.reasons("a-first"))
        self.assertNotIn("test integrity", self.reasons("a-first"))              # that check alone lets this edit through
        self.assertEqual(self.repo.status_of("a-first"), "ready")
        self.assertIn("assert 1 + 1 == 2", self.repo.show("main", "tests/test_base.py"))

    def test_a_renamed_or_deleted_existing_test_waits_for_a_person_with_the_file_list(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["rename_test"], "b-second:build": ["delete_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        for slug in ("a-first", "b-second"):
            self.assertIn("tests/test_base.py (deleted or renamed)", self.reasons(slug))
            self.assertEqual(self.repo.items_state()[slug]["state"], "needs-human")
        self.assertNotIn("tests/test_renamed.py", self.reasons("a-first"))      # the file it was renamed to is a new file

    def test_a_new_test_file_is_free(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["add_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertIsNotNone(self.repo.show("main", "tests/test_a_first.py"))
        self.assertNotIn("Existing tests were allowed", self.repo.git("log", "-1", "--format=%B", "main"))

    def test_test_changes_allowed_lets_an_existing_test_change_and_the_merge_commit_says_so_with_the_docs_reason(self):
        self.allowed_doc("a-first", "- test_changes: allowed — the base test asserted the old arithmetic")
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertIn("assert True", self.repo.show("main", "tests/test_base.py"))
        msg = self.repo.git("log", "-1", "--format=%B", "main")
        self.assertIn("a-first (sdlc/a-first)", msg)
        self.assertIn("Existing tests were allowed to change in this item (test_changes: allowed) — "
                      "the base test asserted the old arithmetic", msg)

    def test_the_commit_says_so_without_a_reason_when_the_doc_gives_none(self):
        self.repo.add_items(("a-first", {"test_changes": "allowed"}))
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertIn("\n\nExisting tests were allowed to change in this item (test_changes: allowed).",
                      self.repo.git("log", "-1", "--format=%B", "main"))

    def test_with_test_changes_allowed_a_deleted_test_and_an_added_skip_still_wait_for_a_person(self):
        self.repo.add_items(("a-first", {"test_changes": "allowed"}), ("b-second", {"test_changes": "allowed"}))
        self.repo.scenario({"a-first:build": ["delete_test"], "b-second:build": ["skip_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertIn("test file deleted: tests/test_base.py", self.reasons("a-first"))
        self.assertIn("skip/only added", self.reasons("b-second"))
        for slug in ("a-first", "b-second"):
            self.assertNotIn("existing tests changed", self.reasons(slug))

    def test_a_build_that_allows_existing_tests_for_itself_waits_for_a_person(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["unlock_tests"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.assertIn("test_changes changed after the design commit: none → allowed (work/a-first.md)", self.reasons("a-first"))
        self.assertIn("existing tests changed, but the design said test_changes: none — tests/test_base.py (modified)",
                      self.reasons("a-first"))                                   # what was frozen counts, not what the branch says
        self.assertEqual(self.repo.status_of("a-first"), "ready")

    def test_the_same_for_an_item_that_was_planned_on_main_before_any_branch_existed(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.scenario({"a-first:build": ["unlock_tests"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build"])
        self.assertIn("test_changes changed after the design commit: none → allowed", self.reasons("a-first"))
        self.assertIn("existing tests changed", self.reasons("a-first"))

    def test_an_item_planned_on_main_as_allowed_keeps_that(self):
        self.repo.add_items(("a-first", {"status": "planned", "test_changes": "allowed"}))
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_taking_the_permission_back_is_a_change_of_the_design_too_but_locks_nothing_that_was_allowed(self):
        from sdlclib.config import Config
        from sdlclib.gate import existing_tests_changed, test_changes_changed
        from sdlclib.gitops import Git
        self.branch_with("allowed", "none")
        cfg, git = Config(self.repo.root), Git(self.repo.root)
        self.assertIn("allowed → none", test_changes_changed(cfg, git, "a-first", "main", "sdlc/a-first"))
        self.assertIsNone(existing_tests_changed(cfg, git, "a-first", "main", "sdlc/a-first"))     # the design allowed it

    def test_a_value_set_back_to_what_the_design_froze_is_no_change(self):
        from sdlclib.config import Config
        from sdlclib.gate import test_changes_changed
        from sdlclib.gitops import Git
        self.branch_with("none", "allowed")
        cfg, git = Config(self.repo.root), Git(self.repo.root)
        self.assertIn("none → allowed", test_changes_changed(cfg, git, "a-first", "main", "sdlc/a-first"))
        self.repo.write("work/a-first.md", self.repo.doc_text("a-first", status="built", test_changes="none"))
        self.repo.git("commit", "-qam", "the build sets it back")
        self.assertIsNone(test_changes_changed(cfg, git, "a-first", "main", "sdlc/a-first"))

    def test_a_refinement_of_test_changes_made_on_main_after_the_branch_was_cut_is_not_a_change_by_the_branch(self):
        from sdlclib.config import Config
        from sdlclib.gate import test_changes_changed
        from sdlclib.gitops import Git
        r = self.repo
        r.add_items(("a-first", {"status": "planned"}))
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        r.write("src/a-first.txt", "built\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "code only")
        r.git("checkout", "-q", "main")
        r.write("work/a-first.md", r.doc_text("a-first", status="planned", test_changes="allowed"))
        r.git("commit", "-qam", "a person allows test changes on main")
        self.assertIsNone(test_changes_changed(Config(r.root), Git(r.root), "a-first", "main", "sdlc/a-first"))

    def test_a_project_that_does_not_lock_tests_for_the_hooks_has_none_locked_for_the_gate(self):
        self.config_change(protect={"protect_tests": False})
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_gate_test_integrity_off_turns_the_existing_test_check_off(self):
        self.config_change(gate={"test_integrity": False})
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_acceptance_frozen_off_still_holds_the_build_to_the_value_the_design_froze(self):
        self.config_change(gate={"acceptance_frozen": False})
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["unlock_tests"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertNotIn("test_changes changed", self.reasons("a-first"))
        self.assertIn("existing tests changed, but the design said test_changes: none", self.reasons("a-first"))

    def test_a_long_list_of_files_is_cut_short(self):
        from sdlclib.config import Config
        from sdlclib.gate import existing_tests_changed
        from sdlclib.gitops import Git
        r = self.repo
        for i in range(10):
            r.write("tests/test_n%d.py" % i, "def test_n%d():\n    assert 1\n" % i)
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "ten more tests")
        r.add_items("a-first")
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        for i in range(10):
            r.write("tests/test_n%d.py" % i, "def test_n%d():\n    assert True\n" % i)
        r.git("commit", "-qam", "weaken them all")
        got = existing_tests_changed(Config(r.root), Git(r.root), "a-first", "main", "sdlc/a-first")
        self.assertIn("tests/test_n0.py (modified)", got)
        self.assertIn("(+2 more)", got)
        self.assertNotIn("tests/test_n9.py", got)


class GuardFiles(RepoCase):
    """The files that keep a session in bounds are always merged by a person, whatever risk.human says."""
    PATHS = (".sdlc/config.json", ".sdlc/frozen-paths.txt", ".claude/settings.json", ".claude/settings.local.json",
             "checks/zz-stub")

    def commit_files(self, *paths):
        for path in paths:
            self.repo.write(path, "{}\n" if path.endswith(".json") else "docs/\n")
        self.repo.git("add", "-A", "-f")
        self.repo.git("commit", "-q", "-m", "guard files")

    def test_a_change_to_any_of_them_waits_for_a_person_and_the_reason_names_the_file(self):
        self.commit_files(*self.PATHS[1:])
        slugs = ["g-%d" % i for i in range(len(self.PATHS))]
        self.repo.add_items(*slugs)
        self.repo.scenario({"%s:build" % s: ["touch:" + p] for s, p in zip(slugs, self.PATHS)})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertNotIn("risk", json.loads(self.repo.read(".sdlc/config.json")))        # nothing here is a risk path
        for slug, path in zip(slugs, self.PATHS):
            rec = self.repo.items_state()[slug]
            self.assertEqual(rec["state"], "needs-human", path)
            self.assertEqual(rec["reasons"], ["guard files changed, a person merges these — %s (modified)" % path])
            self.assertEqual(self.repo.status_of(slug), "ready")

    def test_a_guard_file_that_is_added_or_deleted_counts_too_and_other_files_in_those_directories_do_not(self):
        self.commit_files("checks/zz-stub")
        self.repo.add_items("a-added", "b-deleted", "c-other")
        self.repo.scenario({"a-added:build": ["touch:.claude/settings.json"], "b-deleted:build": ["remove:checks/zz-stub"],
                            "c-other:build": ["touch:.claude/commands.md"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertIn(".claude/settings.json (added)", " ".join(self.repo.items_state()["a-added"]["reasons"]))
        self.assertIn("checks/zz-stub (deleted or renamed)", " ".join(self.repo.items_state()["b-deleted"]["reasons"]))
        self.assertEqual(self.repo.status_of("c-other"), "done")

    def test_the_frozen_paths_file_is_the_one_the_config_names(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["protect"] = {"frozen_paths_file": "meta/frozen.txt"}
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.commit_files("meta/frozen.txt")
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["touch:meta/frozen.txt"], "b-second:build": ["touch:.sdlc/frozen-paths.txt"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertIn("meta/frozen.txt (modified)", " ".join(self.repo.items_state()["a-first"]["reasons"]))
        self.assertEqual(self.repo.status_of("b-second"), "done")                 # the default path is no guard file here

    def test_a_guard_file_comes_on_top_of_a_risk_path(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["risk"] = {"human": ["src/"]}
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "risk")
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["touch:.claude/settings.json"]})
        self.assertEqual(self.repo.engine().run(), 0)
        reasons = self.repo.items_state()["a-first"]["reasons"]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(reasons[0].startswith("risk path: src/a-first.txt"))
        self.assertIn(".claude/settings.json (added)", reasons[1])

    def test_work_docs_and_engine_state_are_not_guard_files(self):
        from sdlclib.config import Config
        from sdlclib.gate import guard_files_changed
        from sdlclib.gitops import Git
        r = self.repo
        r.git("checkout", "-q", "-b", "sdlc/x")
        r.write("work/x.md", r.doc_text("x", status="built"))
        r.write(".sdlc/state/items.json", "{}\n")
        r.write(".sdlc/logs/run.log", "x\n")
        r.git("add", "-A", "-f")
        r.git("commit", "-q", "-m", "docs and state")
        self.assertIsNone(guard_files_changed(Config(r.root), Git(r.root), "main", "sdlc/x"))
        cfg = json.loads(r.read(".sdlc/config.json"))
        cfg["commands"]["lint"] = "true"
        r.write(".sdlc/config.json", json.dumps(cfg))
        r.git("commit", "-qam", "and the config")
        self.assertIn(".sdlc/config.json (modified)", guard_files_changed(Config(r.root), Git(r.root), "main", "sdlc/x"))


class WhatAPersonDecides(RepoCase):
    """An item that waits for a person is out of the loop's hands: when the person merges the branch it is done."""

    def waiting(self):
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}))
        self.repo.scenario({"a-first:build": ["new_dep"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.assertEqual(self.repo.status_of("b-second"), "ready")                       # waits for a-first, which waits for a person

    def counts(self):
        return json.loads(self.repo.cli("status", "--json").stdout)["counts"]

    def test_the_branch_a_person_merged_makes_the_item_done_and_releases_its_dependents(self):
        self.waiting()
        self.assertEqual(self.counts(), {"needs-human": 1, "design": 1})
        self.repo.git("merge", "-q", "--no-edit", "sdlc/a-first")                         # the person takes it from here
        self.assertEqual(self.repo.status_of("a-first"), "built")                         # a built doc on main: a person merged it
        self.assertEqual(self.counts(), {"done": 1, "design": 1})                         # status knows at once, no loop needed
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "merged")                                           # the loop clears the stale wait
        self.assertNotIn("reasons", rec)
        self.assertEqual(self.repo.status_of("b-second"), "done")                         # its dependent was released and ran
        self.assertEqual(self.counts(), {"done": 2})
        with open(e.log_path) as fh:
            self.assertIn("MERGED  a-first — merged by a person", fh.read())
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:build"), 1)           # the loop did not build it again

    def test_a_squash_merge_by_hand_is_done_too_with_no_engine_state_left(self):
        """The branch tip of a squash is not an ancestor of main: only the built doc on main tells that it was merged."""
        self.waiting()
        self.repo.git("merge", "--squash", "sdlc/a-first")
        self.repo.git("commit", "-q", "-m", "a-first, taken by hand")
        ancestor = subprocess.run(["git", "-C", self.repo.root, "merge-base", "--is-ancestor", "sdlc/a-first", "main"],
                                  env=self.repo.env())
        self.assertEqual(ancestor.returncode, 1)
        os.remove(self.repo.path(".sdlc/state/items.json"))                               # …and the engine remembers nothing
        self.assertEqual(self.repo.status_of("a-first"), "built")
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual({i["slug"]: i["phase"] for i in s["items"]}, {"a-first": "done", "b-second": "design"})
        self.assertEqual(s["counts"], {"done": 1, "design": 1})
        self.assertEqual(s["needs_you"], [])                                              # nothing waits for anyone
        self.assertEqual(s["next"], ["b-second"])                                         # its dependent is runnable
        text = self.repo.cli("status").stdout
        self.assertIn("1 done", text)
        self.assertNotIn("needs you", text)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        with open(e.log_path) as fh:
            log = fh.read()
        self.assertNotIn("ITEM    a-first", log)                                            # not gated, built or merged again
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:build"), 1)
        self.assertEqual(self.repo.status_of("b-second"), "done")
        self.assertEqual(self.counts(), {"done": 2})

    def test_a_built_doc_that_is_only_on_the_branch_is_not_done_even_without_engine_state(self):
        self.waiting()
        os.remove(self.repo.path(".sdlc/state/items.json"))
        self.assertEqual(self.repo.status_of("a-first"), "ready")                         # main says ready; built is on the branch
        self.assertEqual(self.counts(), {"gate": 1, "design": 1})                         # the gate is next, as for any built item

    def test_an_item_that_is_still_waiting_stays_waiting(self):
        self.waiting()
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.assertEqual(self.counts(), {"needs-human": 1, "design": 1})

    def test_retry_still_hands_the_item_back_to_the_loop(self):
        self.waiting()
        self.assertEqual(self.repo.cli("retry", "a-first").returncode, 0)
        self.assertEqual(self.counts(), {"gate": 1, "design": 1})                         # the loop would gate it again
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")      # the new dependency is still there
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:build"), 1)


class BlockedLocally(RepoCase):
    config = {"loop": {"max_failures_per_slug": 1}}

    def test_a_blocked_item_whose_branch_has_nothing_of_its_own_is_not_mistaken_for_a_merged_one(self):
        """The branch of an item whose design failed is level with main — an ancestor of it, like a merged branch is."""
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["error"]})
        self.assertEqual(self.repo.engine().run(), 1)
        self.assertEqual(self.repo.git("rev-parse", "sdlc/a-first"), self.repo.git("rev-parse", "main"))
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertNotIn("state", self.repo.items_state()["a-first"])
        self.assertEqual(json.loads(self.repo.cli("status", "--json").stdout)["counts"], {"blocked": 1})


class Stopping(RepoCase):
    def test_pause_file_keeps_the_loop_from_starting(self):
        self.repo.add_items("a-first")
        self.repo.cli("stop", "--after-item")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.calls(), [])

    def test_dirty_tree_refuses_to_start(self):
        self.repo.add_items("a-first")
        self.repo.write("scratch.txt", "mine\n")
        self.assertEqual(self.repo.engine().run(), 2)
        self.assertEqual(self.repo.calls(), [])

    def test_stop_now_interrupts_the_session_and_resume_continues_it(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["sleep:60", "ok"]})
        r = self.repo.cli("run", "--detach", "--claude-bin", STUBS + "/claude")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("loop started in the background", r.stdout)
        for _ in range(200):
            if any(c["key"] == "a-first:build" for c in self.repo.calls()):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        t = time.time()
        r = self.repo.cli("stop")
        self.assertIn("stopped", r.stdout)
        self.assertLess(time.time() - t, 20)
        stop = self.repo.items_state()["a-first"]["stop"]
        self.assertEqual(stop["phase"], "build")
        r = self.repo.cli("resume", "--claude-bin", STUBS + "/claude")
        self.assertEqual(r.returncode, 0, r.stdout)
        for _ in range(300):
            if self.repo.status_of("a-first") == "done":
                break
            time.sleep(0.1)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        builds = [c for c in self.repo.calls() if c["key"] == "a-first:build"]
        self.assertTrue(builds[-1]["resumed"])


class Accept(RepoCase):
    """`accept SLUG...` turns proposed work docs into ready ones on the default branch, the way enqueue lands docs."""

    def idea(self, slug, **kw):
        return (slug, dict(status="proposed", **kw))

    def head_subject(self):
        return self.repo.git("log", "-1", "--format=%s")

    def test_a_proposed_doc_becomes_ready_on_main_in_one_commit_and_the_loop_then_runs_it(self):
        self.repo.add_items(self.idea("a-idea", goal="Keep this exact goal."))
        before = self.repo.show("main", "work/a-idea.md")
        r = self.repo.cli("accept", "a-idea")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("accepted 1 item(s): a-idea — committed on main", r.stdout)
        self.assertEqual(self.repo.status_of("a-idea"), "ready")
        after = self.repo.show("main", "work/a-idea.md")
        self.assertEqual(after.replace("status: ready", "status: proposed"), before)         # only the status moved
        self.assertEqual(self.head_subject(), "sdlc: accept a-idea")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/new/a-idea.md")))       # no scratch copy left
        self.assertEqual(json.loads(self.repo.cli("status", "--json").stdout)["counts"], {"design": 1})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-idea"), "done")

    def test_several_at_once_by_slug_or_by_file_name_make_one_commit(self):
        self.repo.add_items(self.idea("a-idea"), self.idea("b-idea"), self.idea("c-idea"))
        r = self.repo.cli("accept", "work/a-idea.md", "b-idea", "a-idea")                    # a repeated slug counts once
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("accepted 2 item(s): a-idea, b-idea", r.stdout)
        self.assertEqual([self.repo.status_of(s) for s in ("a-idea", "b-idea", "c-idea")], ["ready", "ready", "proposed"])
        self.assertEqual(self.head_subject(), "sdlc: accept 2 work items")

    def test_what_cannot_be_accepted_is_refused_and_nothing_is_changed(self):
        self.repo.add_items(self.idea("a-idea"), "b-ready")
        head = self.repo.git("rev-parse", "HEAD")
        r = self.repo.cli("accept", "a-idea", "b-ready", "nope")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("b-ready: status is ready — only a proposed doc is accepted", r.stderr)
        self.assertIn("nope: no work doc on main", r.stderr)
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.repo.status_of("a-idea"), "proposed")                          # the valid one waits too
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/new/a-idea.md")))
        self.assertEqual(self.repo.cli("accept").returncode, 2)                              # a slug is required

    def test_a_doc_with_uncommitted_edits_is_not_overwritten(self):
        self.repo.add_items(self.idea("a-idea"))
        mine = self.repo.doc_text("a-idea", status="proposed", goal="My own rewrite.")
        self.repo.write("work/a-idea.md", mine)
        head = self.repo.git("rev-parse", "HEAD")
        r = self.repo.cli("accept", "a-idea")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("uncommitted changes", r.stderr)
        self.assertEqual(self.repo.read("work/a-idea.md"), mine)
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)

    def test_while_a_loop_runs_in_this_checkout_the_acceptance_waits_in_the_inbox(self):
        from sdlclib import state as st
        from sdlclib.config import Config
        from sdlclib.enqueue import drain_inbox
        from sdlclib.gitops import Git, LocalForge
        self.repo.add_items(self.idea("a-idea"))
        st.write_pid(self.repo.root)
        st.write_status(self.repo.root, {"root": self.repo.root})
        self.addCleanup(st.clear_pid, self.repo.root)
        r = self.repo.cli("accept", "a-idea")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("inbox", r.stdout)
        self.assertEqual(self.repo.status_of("a-idea"), "proposed")                          # nothing committed under the loop
        self.assertTrue(os.path.exists(self.repo.path(".sdlc/state/inbox/a-idea.md")))
        git = Git(self.repo.root)
        self.assertEqual(drain_inbox(Config(self.repo.root), git, LocalForge(git, "main")), ["a-idea"])
        self.assertEqual(self.repo.status_of("a-idea"), "ready")
        self.assertEqual(os.listdir(self.repo.path(".sdlc/state/inbox")), [])

    def test_from_another_branch_the_default_branch_still_gets_the_commit(self):
        self.repo.add_items(self.idea("a-idea"))
        self.repo.git("switch", "-q", "-c", "sdlc/some-item")
        r = self.repo.cli("accept", "a-idea")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.repo.status_of("a-idea"), "ready")
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "sdlc/some-item")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_the_wait_line_of_a_proposed_item_names_the_command(self):
        self.repo.add_items(self.idea("a-idea"))
        r = self.repo.cli("run", "--dry-run")
        self.assertRegex(r.stdout, r"WAITS +a-idea +proposed — `sdlc\.py accept a-idea` queues it")

    def test_a_proposal_edited_to_ready_can_also_be_enqueued_over_its_committed_self(self):
        self.repo.add_items(self.idea("a-idea"))
        path = self.repo.write("work/a-idea.md", self.repo.doc_text("a-idea", goal="Refined by a person."))
        r = self.repo.cli("enqueue", path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.repo.status_of("a-idea"), "ready")
        self.assertIn("Refined by a person.", self.repo.show("main", "work/a-idea.md"))

    def test_only_the_step_from_proposed_to_ready_may_replace_a_committed_doc(self):
        self.repo.add_items(self.idea("a-idea"), "b-ready")
        path = self.repo.write("work/a-idea.md", self.repo.doc_text("a-idea", status="proposed", goal="Still only an idea."))
        r = self.repo.cli("enqueue", path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already on main", r.stderr)
        path = self.repo.write("work/b-ready.md", self.repo.doc_text("b-ready", goal="Changed under the loop's feet."))
        r = self.repo.cli("enqueue", path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already on main", r.stderr)
        self.assertNotIn("Changed under", self.repo.show("main", "work/b-ready.md"))
        self.assertNotIn("Still only", self.repo.show("main", "work/a-idea.md"))


class Drop(RepoCase):
    """`drop SLUG [REASON]` is recorded where the queue lives: the doc on the default branch says dropped, so every clone knows.
    So is an item a session found unnecessary — it leaves the record `drop` would, not only a note in this checkout's state."""

    def head(self):
        return self.repo.git("rev-parse", "HEAD")

    def clone_status(self):
        clone = os.path.join(self.repo.tmp, "clone")
        subprocess.run(["git", "clone", "-q", self.repo.root, clone], check=True, env=self.repo.env(), capture_output=True)
        r = subprocess.run([sys.executable, SDLC, "--root", clone, "status", "--json", "--no-fetch"], capture_output=True,
                           text=True, env=self.repo.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_doc_on_main_says_dropped_with_the_reason_and_a_fresh_clone_shows_it(self):
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}), "c-third")
        before = self.repo.show("main", "work/a-first.md")
        r = self.repo.cli("drop", "a-first", "not", "needed", "any", "more")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a-first dropped — committed on main", r.stdout)
        doc = self.repo.show("main", "work/a-first.md")
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertIn("## Departures\nnot needed any more\n", doc)
        for kept in ("## Goal\nDo the thing.", "## Acceptance criteria\nR1. WHEN the user asks THE SYSTEM SHALL do the thing."):
            self.assertIn(kept, doc)
            self.assertIn(kept, before)
        self.assertEqual(self.repo.git("log", "-1", "--format=%s"), "sdlc: drop a-first")
        self.assertIn("- a-first — a-first (dropped: not needed any more)", self.repo.git("log", "-1", "--format=%b"))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/new/a-first.md")))        # no scratch copy left
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "dropped")
        s = self.clone_status()                                     # no engine state there: only the repository speaks
        self.assertEqual(s["counts"], {"design": 2, "dropped": 1})
        waits = {n["slug"]: " ".join(n["text"]) for n in s["needs_you"]}
        self.assertIn("depends on 'a-first', which was dropped", waits["b-second"])

    def test_the_loop_leaves_a_dropped_item_alone(self):
        self.repo.add_items("a-first", "b-second")
        self.assertEqual(self.repo.cli("drop", "a-first").returncode, 0)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["b-second:design", "b-second:build"])
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertEqual(self.repo.status_of("b-second"), "done")

    def test_without_a_reason_it_says_so_and_a_file_name_works_as_well_as_a_slug(self):
        self.repo.add_items("a-first")
        r = self.repo.cli("drop", "work/a-first.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("## Departures\ndropped by request\n", self.repo.show("main", "work/a-first.md"))

    def test_what_the_doc_already_said_under_departures_stays_below_the_reason(self):
        self.repo.write("work/a-first.md", self.repo.doc_text("a-first") + "\n## Departures\n<!-- hint -->\nEarlier note.\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "queue")
        self.assertEqual(self.repo.cli("drop", "a-first", "superseded").returncode, 0)
        doc = self.repo.show("main", "work/a-first.md")
        self.assertIn("## Departures\nsuperseded\n\nEarlier note.\n", doc)
        self.assertNotIn("<!-- hint -->", doc)

    def test_every_state_that_is_not_finished_can_be_dropped(self):
        self.repo.add_items(("a-planned", {"status": "planned"}), ("b-idea", {"status": "proposed"}),
                            ("c-asking", {"status": "needs-input"}))
        for slug in ("a-planned", "b-idea", "c-asking"):
            r = self.repo.cli("drop", slug, "gone")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.repo.status_of(slug), "dropped")

    def test_what_cannot_be_dropped_is_refused_and_nothing_is_changed(self):
        self.repo.add_items(("a-done", {"status": "done"}), ("b-built", {"status": "built"}))
        head = self.head()
        r = self.repo.cli("drop", "nope", "why")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("nope: no work doc on main", r.stderr)
        for slug, status in (("a-done", "done"), ("b-built", "built")):
            r = self.repo.cli("drop", slug)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("%s: status is %s — a finished item is not dropped" % (slug, status), r.stderr)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.repo.items_state(), {})                           # no engine record was made up either
        self.assertEqual(self.repo.cli("drop").returncode, 2)                   # a slug is required

    def test_dropping_twice_records_it_once(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.cli("drop", "a-first", "first reason").returncode, 0)
        head = self.head()
        r = self.repo.cli("drop", "a-first", "second reason")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a-first dropped — already dropped on main", r.stdout)
        self.assertEqual(self.head(), head)
        self.assertIn("## Departures\nfirst reason\n", self.repo.show("main", "work/a-first.md"))

    def test_a_doc_with_uncommitted_edits_is_not_overwritten(self):
        self.repo.add_items("a-first")
        mine = self.repo.doc_text("a-first", goal="My own rewrite.")
        self.repo.write("work/a-first.md", mine)
        head = self.head()
        r = self.repo.cli("drop", "a-first")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("uncommitted changes", r.stderr)
        self.assertEqual(self.repo.read("work/a-first.md"), mine)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.repo.items_state(), {})

    def test_an_item_that_waits_for_something_missing_can_still_be_dropped(self):
        self.repo.add_items(("a-first", {"deps": ["long-gone"]}))
        r = self.repo.cli("drop", "a-first")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.repo.status_of("a-first"), "dropped")

    def test_enqueue_does_not_start_a_new_item_as_dropped(self):
        p = self.repo.write("work/new-thing.md", self.repo.doc_text("new-thing", status="dropped"))
        r = self.repo.cli("enqueue", p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("status 'dropped' — a new item is ready, proposed or needs-input", r.stderr)

    def test_a_version_1_layout_is_sent_to_init(self):
        self.repo.write(".sdlc/config.json", json.dumps({"version": 1, "roles": {"solo": True}}))
        self.repo.git("commit", "-qam", "legacy")
        r = self.repo.cli("drop", "a-first")
        self.assertEqual(r.returncode, 2)
        self.assertIn("migrate", r.stderr)

    def test_while_a_loop_runs_in_this_checkout_the_drop_waits_in_the_inbox_and_the_loop_commits_it(self):
        from sdlclib import state as st
        from sdlclib.config import Config
        from sdlclib.enqueue import drain_inbox
        from sdlclib.gitops import Git, LocalForge
        self.repo.add_items("a-first")
        st.write_pid(self.repo.root)
        st.write_status(self.repo.root, {"root": self.repo.root})
        self.addCleanup(st.clear_pid, self.repo.root)
        r = self.repo.cli("drop", "a-first", "not", "needed")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("inbox", r.stdout)
        self.assertEqual(self.repo.status_of("a-first"), "ready")                              # nothing committed under the loop
        self.assertTrue(os.path.exists(self.repo.path(".sdlc/state/inbox/a-first.md")))
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "dropped")               # but this checkout stops at once
        git = Git(self.repo.root)
        self.assertEqual(drain_inbox(Config(self.repo.root), git, LocalForge(git, "main")), ["a-first"])
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertEqual(self.repo.git("log", "-1", "--format=%s"), "sdlc: drop a-first")
        self.assertEqual(os.listdir(self.repo.path(".sdlc/state/inbox")), [])

    def test_from_another_branch_the_default_branch_still_gets_the_commit(self):
        self.repo.add_items("a-first")
        self.repo.git("switch", "-q", "-c", "sdlc/some-item")
        r = self.repo.cli("drop", "a-first", "gone")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "sdlc/some-item")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def events(self, decision):
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            return [e for e in map(json.loads, fh) if e["event"] == "loop.item" and e["decision"] == decision]

    def run_log(self, engine):
        with open(engine.log_path) as fh:
            return fh.read()

    def test_an_item_a_session_found_unnecessary_is_recorded_on_main_like_a_drop_and_the_loop_goes_on(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:design": ["drop:already there - src/x.py line 12"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "b-second:design", "b-second:build"])
        doc = self.repo.show("main", "work/a-first.md")
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertIn("## Departures\nalready there - src/x.py line 12\n", doc)
        self.assertIn("## Goal\nDo the thing.", doc)
        self.assertIn("sdlc: drop a-first", self.repo.git("log", "--format=%s", "main"))
        self.assertEqual(self.repo.status_of("b-second"), "done")
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/new/a-first.md")))        # no scratch copy left
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("dropped", ["already there - src/x.py line 12"]))
        self.assertIn("DROPPED a-first — already there - src/x.py line 12 (committed on main)", self.run_log(e))
        self.assertEqual([(ev["detail"]["slug"], ev["reason"]) for ev in self.events("dropped")],
                         [("a-first", "already there - src/x.py line 12")])
        self.assertEqual(self.clone_status()["counts"], {"dropped": 1, "done": 1})            # a clone reads it from the repository

    def test_a_build_session_that_drops_the_item_leaves_main_the_doc_it_had_not_the_branchs_plan(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["drop:the behavior is already there"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        doc = self.repo.show("main", "work/a-first.md")
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertIn("## Departures\nthe behavior is already there\n", doc)
        self.assertNotIn("## Plan", doc)                                                      # the design commit stays on the branch
        self.assertIn("## Plan", self.repo.show("sdlc/a-first", "work/a-first.md"))
        self.assertEqual(self.clone_status()["counts"], {"dropped": 1})

    def test_a_session_that_leaves_only_the_template_hint_still_leaves_a_reason(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["drop"]})
        self.assertEqual(self.repo.engine().run(), 0)
        doc = self.repo.show("main", "work/a-first.md")
        self.assertIn("## Departures\ndropped by the session\n", doc)
        self.assertNotIn("<!--", doc)
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"], ["dropped by the session"])

    def test_a_drop_that_cannot_be_recorded_is_announced_this_checkout_leaves_the_item_alone_and_the_loop_goes_on(self):
        self.repo.add_items(("a-first", {"goal": ""}), "b-second")                           # the queue would not take this doc as it is
        self.repo.scenario({"a-first:design": ["drop:not needed"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "b-second:design", "b-second:build"])
        self.assertEqual(self.repo.status_of("a-first"), "ready")                            # nothing was recorded on main
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "dropped")                                            # but this checkout does not take it again
        self.assertEqual(rec["reasons"][0], "not needed")
        self.assertIn("not recorded on main: ", rec["reasons"][1])
        self.assertIn("no Goal section", rec["reasons"][1])
        log = self.run_log(e)
        self.assertIn("DROPPED a-first — not needed (NOT recorded on main: ", log)
        self.assertIn('`sdlc.py drop a-first "<reason>"` records it', log)
        failed = self.events("drop-not-recorded")
        self.assertEqual([(ev["detail"]["slug"], ev["reason"]) for ev in failed], [("a-first", "not needed")])
        self.assertIn("no Goal section", failed[0]["detail"]["error"])
        self.assertEqual(self.events("dropped"), [])
        self.assertEqual(self.repo.status_of("b-second"), "done")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/new/a-first.md")))

    def test_the_loop_records_a_drop_while_the_doc_here_has_edits_a_person_s_own_are_not_overwritten(self):
        from sdlclib.config import Config
        from sdlclib.enqueue import EnqueueError, record_drop
        from sdlclib.gitops import Git, LocalForge
        self.repo.add_items("a-first")
        self.repo.git("switch", "-q", "-c", "sdlc/a-first")
        mine = self.repo.doc_text("a-first", goal="Half-edited.")
        self.repo.write("work/a-first.md", mine)
        cfg, git = Config(self.repo.root), Git(self.repo.root)
        with self.assertRaises(EnqueueError) as cm:
            record_drop(cfg, git, LocalForge(git, "main"), "a-first", "gone")
        self.assertIn("uncommitted changes", str(cm.exception))
        self.assertEqual(self.repo.status_of("a-first"), "ready")
        res = record_drop(cfg, git, LocalForge(git, "main"), "a-first", "gone", from_loop=True)
        self.assertEqual(res["how"], "committed on main")
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertEqual(self.repo.read("work/a-first.md"), mine)                            # the working tree was left as it was
        self.assertEqual(record_drop(cfg, git, LocalForge(git, "main"), "a-first", "again", from_loop=True)["how"],
                         "already dropped on main")


class Detach(RepoCase):
    """`run --detach` says what became of the loop it started: a loop that is gone at once is not "started"."""

    def detach(self, *extra):
        return self.repo.cli("run", "--detach", "--claude-bin", STUBS + "/claude", *extra)

    def test_an_empty_queue_is_nothing_to_do_not_a_started_loop(self):
        r = self.detach()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing to do: queue empty", r.stdout)
        self.assertNotIn("loop started", r.stdout)
        self.assertEqual(self.repo.calls(), [])
        self.assertIn("loop: not running", self.repo.cli("status").stdout)

    def test_a_queue_that_only_waits_for_a_person_is_nothing_to_do_too(self):
        self.repo.add_items(("a-idea", {"status": "proposed"}))
        r = self.detach()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing to do: nothing runnable — 1 item(s) wait for you", r.stdout)
        self.assertNotIn("loop started", r.stdout)

    def test_a_pause_shows_the_loops_own_line_and_fails(self):
        self.repo.add_items("a-first")
        self.repo.cli("stop", "--after-item")
        r = self.detach()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("paused", r.stdout)
        self.assertIn("sdlc.py resume", r.stdout)
        self.assertNotIn("loop started", r.stdout)
        self.assertEqual(self.repo.calls(), [])

    def test_a_refusal_shows_the_tail_of_the_log_and_the_loops_exit_code(self):
        self.repo.add_items("a-first")
        self.repo.write("scratch.txt", "mine\n")
        r = self.detach()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("uncommitted changes", r.stdout)
        self.assertNotIn("loop started", r.stdout)

    def test_a_loop_that_has_work_is_started_and_a_second_detach_finds_it_running(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["sleep:60", "ok"]})
        r = self.detach()
        self.addCleanup(self.repo.cli, "stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("loop started in the background", r.stdout)
        self.assertRegex(self.repo.cli("status").stdout, r"loop: running")
        again = self.detach()
        self.assertEqual(again.returncode, 0)
        self.assertIn("already running", again.stdout)


class FinishRun(RepoCase):
    """A run that ends puts the checkout back on the default branch, unless that would lose uncommitted work."""

    def head(self):
        return self.repo.git("rev-parse", "--abbrev-ref", "HEAD")

    def log(self, e):
        with open(e.log_path) as fh:
            return fh.read()

    def test_a_run_that_stops_on_max_items_is_back_on_main(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["new_dep"]})                    # a-first waits for a person; its branch stays
        e = self.repo.engine(max_items=1)
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.head(), "main")
        self.assertIn("BACK    on main again", self.log(e))
        self.assertIn("sdlc/a-first", self.repo.git("branch", "--list"))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_a_run_stopped_by_a_usage_limit_is_back_on_main_and_the_next_run_continues_the_item(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_session:100000", "ok"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)                                           # waiting is beyond limit_wait_hours: the run stops
        self.assertEqual(self.head(), "main")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_branch_with_uncommitted_changes_is_left_alone_and_the_loop_says_so(self):
        e = self.repo.engine()
        self.repo.git("checkout", "-qb", "sdlc/a-first")
        self.repo.write("tests/test_base.py", "def test_base():\n    assert 2 + 2 == 4\n")
        e.return_to_default()
        self.assertEqual(self.head(), "sdlc/a-first")
        self.assertIn("LEFT    still on sdlc/a-first: uncommitted changes", self.log(e))

    def test_a_clean_item_branch_goes_back_and_other_branches_are_not_touched(self):
        e = self.repo.engine()
        self.repo.git("checkout", "-qb", "sdlc/a-first")
        e.return_to_default()
        self.assertEqual(self.head(), "main")
        self.repo.git("checkout", "-qb", "feature/mine")
        e.return_to_default()
        self.assertEqual(self.head(), "feature/mine")                          # not the loop's branch: not its business


class SelfCheck(RepoCase):
    config = {"loop": {"self_check": True}}

    def test_findings_become_items_capped_and_built_in_the_same_run(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "fix-one", "status": "ready"}, {"slug": "fix-two", "status": "ready"},
                                               {"slug": "old-debt", "status": "proposed"}]})
        e = self.repo.engine()
        e.cfg.data["loop"]["self_check_max_items"] = 1
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("fix-one"), "done")
        self.assertEqual(self.repo.status_of("fix-two"), "proposed")
        self.assertEqual(self.repo.status_of("old-debt"), "proposed")
        self.assertIn("source: self-check", self.repo.show("main", "work/fix-one.md"))
        self.assertEqual([k for k, *_ in self.keys()].count("self-check"), 2)

    def test_a_drop_the_loop_recorded_moves_main_but_is_not_a_merge_so_nothing_is_self_checked(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["drop:already there"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "dropped")
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design"])                   # no self-check session
        self.assertEqual(e.done["merged"], [])
        self.assertNotEqual(e.git.head(), e.from_sha)                                        # the drop's commit moved main
        self.assertFalse(e.self_check_due())


class Adoption(RepoCase):
    """A branch sdlc/<slug> that 0.x (or a person) made before the item had a work doc gets the doc when the engine checks
    the item out — committed on the branch — and the item then goes through design, build, gate and merge like any other."""

    def zero_x_branch(self, slug="a-first", files=None):
        """sdlc/<slug> cut from main as it is now, with what 0.x left on it, then back to main."""
        self.repo.git("checkout", "-q", "-b", "sdlc/" + slug)
        for rel, text in (files or {"plan/%s.md" % slug: "---\ntype: plan\nstatus: approved\n---\n# Plan\n"}).items():
            self.repo.write(rel, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "plan %s" % slug)
        self.repo.git("checkout", "-q", "main")

    def test_the_checkout_commits_the_default_branchs_doc_on_the_branch(self):
        self.zero_x_branch()
        self.repo.add_items("a-first")
        self.assertIsNone(self.repo.show("sdlc/a-first", "work/a-first.md"))
        e = self.repo.engine()
        e.checkout_item("a-first")
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "sdlc/a-first")
        self.assertEqual(self.repo.read("work/a-first.md"), self.repo.show("main", "work/a-first.md"))
        self.assertEqual(self.repo.show("sdlc/a-first", "work/a-first.md"), self.repo.show("main", "work/a-first.md"))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertIn("plan/a-first.md", self.repo.git("ls-files"))             # what 0.x left on the branch stays
        self.assertIn("a-first", self.repo.git("log", "-1", "--format=%s"))
        again = self.repo.git("rev-parse", "HEAD")
        e.adopt_doc("a-first")                                                  # the doc is there now: nothing more to do
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), again)

    def test_the_item_then_completes_without_a_fix_session(self):
        self.zero_x_branch()
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])      # no conflict, no fix round
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertEqual(self.repo.show("main", "src/a-first.txt"), "built a-first\n")
        self.assertIn("## Plan", self.repo.show("main", "work/a-first.md"))
        self.assertIsNotNone(self.repo.show("main", "plan/a-first.md"))          # the 0.x plan came along with the branch
        self.assertNotIn("sdlc/a-first", self.repo.git("branch", "--list"))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_the_doc_is_taken_even_when_the_default_branch_moved_on_after_the_branch_was_cut(self):
        self.zero_x_branch()
        self.repo.add_items("a-first")
        self.repo.write("src/other.txt", "work that landed on main meanwhile\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "other work")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertEqual(self.repo.show("main", "src/other.txt"), "work that landed on main meanwhile\n")

    def test_a_branch_that_has_its_doc_already_is_left_alone(self):
        self.repo.add_items("a-first")
        self.repo.git("checkout", "-q", "-b", "sdlc/a-first")
        self.repo.write("work/a-first.md", self.repo.doc_text("a-first", status="planned") + "\n## Plan\nmine\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "plan")
        self.repo.git("checkout", "-q", "main")
        head = self.repo.git("rev-parse", "sdlc/a-first")
        self.repo.engine().checkout_item("a-first")
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)
        self.assertIn("mine", self.repo.read("work/a-first.md"))

    def test_a_merge_that_conflicts_is_undone_and_only_the_doc_is_copied(self):
        self.zero_x_branch(files={"src/x.txt": "the branch's version\n"})
        self.repo.write("src/x.txt", "main's version\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "main changes the same file")
        self.repo.add_items("a-first")
        self.repo.engine().checkout_item("a-first")
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "sdlc/a-first")
        self.assertFalse(os.path.exists(self.repo.path(".git/MERGE_HEAD")))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertEqual(self.repo.read("src/x.txt"), "the branch's version\n")        # the conflicting change was not merged in
        self.assertEqual(self.repo.read("work/a-first.md"), self.repo.show("main", "work/a-first.md"))
        self.assertEqual(self.repo.git("show", "--format=", "--name-only", "HEAD").split(), ["work/a-first.md"])
        self.assertIn("work doc from main", self.repo.git("log", "-1", "--format=%s"))

    def test_acceptance_criteria_changed_by_the_build_of_an_item_designed_before_the_branch_still_wait_for_a_person(self):
        self.zero_x_branch()
        self.repo.add_items(("a-first", {"status": "planned"}))                 # a migrated item: designed on main already
        self.repo.scenario({"a-first:build": ["change_acceptance"]})
        self.repo.engine().run()
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build"])
        self.assertIn("acceptance criteria changed", " ".join(self.repo.items_state()["a-first"]["reasons"]))
        self.assertEqual(self.repo.status_of("a-first"), "planned")             # nothing was merged

    def test_nothing_is_adopted_when_the_default_branch_has_no_doc_either(self):
        self.zero_x_branch()
        e = self.repo.engine()
        e.checkout_item("a-first")
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "sdlc/a-first")
        self.assertFalse(os.path.exists(self.repo.path("work/a-first.md")))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")


class AdoptionOnRemote(RepoCase):
    remote = True

    def test_a_branch_that_only_exists_on_origin_gets_the_doc_too(self):
        r = self.repo
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        r.write("plan/a-first.md", "old plan\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "plan a-first")
        r.git("push", "-q", "origin", "sdlc/a-first")
        r.git("checkout", "-q", "main")
        r.git("branch", "-q", "-D", "sdlc/a-first")
        r.add_items("a-first")                                                   # committed on main and pushed
        self.assertIsNone(r.origin_show("sdlc/a-first", "work/a-first.md"))
        r.engine().checkout_item("a-first")
        self.assertEqual(r.git("rev-parse", "--abbrev-ref", "HEAD"), "sdlc/a-first")
        self.assertEqual(r.read("work/a-first.md"), r.show("main", "work/a-first.md"))
        self.assertEqual(r.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
