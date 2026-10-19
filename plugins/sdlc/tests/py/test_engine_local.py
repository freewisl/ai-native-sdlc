"""The loop on a repository without a remote: squash merges on main, the same gate."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest

from helpers import RepoCase, SDLC, STUBS


def latest_run_log(repo):
    """The text of the newest `.sdlc/logs/run-*.log` — what the engine said."""
    d = repo.path(".sdlc/logs")
    with open(os.path.join(d, sorted(n for n in os.listdir(d) if n.startswith("run-"))[-1])) as fh:
        return fh.read()


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


class BudgetCap(RepoCase):
    """A session stopped by loop.budget_usd_per_session is continued like one stopped by the turn cap — and everything that
    names the stop (log, next prompt, event, failure) names the budget, not a turn cap."""
    config = {"loop": {"budget_usd_per_session": 2.5}}

    def sessions(self, phase):
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            return [e["detail"] for e in map(json.loads, fh) if e["event"] == "loop.session" and e["detail"]["phase"] == phase]

    def builds(self):
        return [c for c in self.repo.calls() if c["key"] == "a-first:build"]

    def test_the_budget_stop_resumes_the_same_session_and_the_log_says_budget(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_budget", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        builds = self.builds()
        self.assertEqual([b["resumed"] for b in builds], [False, True])
        self.assertEqual(builds[0]["sid"], builds[1]["sid"])
        self.assertEqual([b["budget"] for b in builds], ["2.5", "2.5"])      # the amount the reason names is the one claude got
        self.assertIn("Continue where you stopped (budget cap ($2.5))", builds[1]["prompt"])
        log = latest_run_log(self.repo)
        self.assertIn("STOPPED a-first", log)
        self.assertIn("budget cap ($2.5); continuing (warm resume)", log)
        self.assertNotIn("turn cap", log)
        self.assertEqual([s["status"] for s in self.sessions("build")], ["budget", "ok"])
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertAlmostEqual(self.repo.items_state()["a-first"]["cost"], 1.5)      # the resumed session's cost counts once

    def test_a_cold_budget_stop_starts_a_fresh_session_that_is_told_why(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_budget", "ok"]})
        e = self.repo.engine()
        e.cold_seconds = 0
        self.assertEqual(e.run(), 0)
        builds = self.builds()
        self.assertFalse(builds[1]["resumed"])
        self.assertIn("--continue", builds[1]["prompt"])
        self.assertIn("budget cap ($2.5)", builds[1]["prompt"])
        self.assertNotIn("turn cap", builds[1]["prompt"])

    def test_without_a_configured_amount_the_reason_names_none(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_budget", "ok"]})
        e = self.repo.engine()
        e.runner.budget = 0.0
        self.assertEqual(e.run(), 0)
        self.assertEqual([b["budget"] for b in self.builds()], ["", ""])
        self.assertIn("budget cap; continuing (warm resume)", latest_run_log(self.repo))

    def test_a_budget_stop_without_progress_fails_naming_the_budget(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_budget", "max_budget_noprogress"]})
        self.assertEqual(self.repo.engine().run(), 1)
        self.assertIn("no progress since the last stop (budget cap ($2.5))", self.repo.items_state()["a-first"]["last_error"])

    def test_budget_stops_are_capped_like_turn_caps(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["max_budget"] * 6})
        e = self.repo.engine()
        e.max_continuations = 2
        self.assertEqual(e.run(), 1)
        self.assertEqual(len([c for c in self.repo.calls() if c["key"] == "a-first:design"]), 3)
        self.assertIn("still unfinished after 2 continuations (budget cap ($2.5))", self.repo.items_state()["a-first"]["last_error"])


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
        self.assertNotIn("this branch's own `.sdlc/config.json`", fix["prompt"])      # the project's commands: nothing to warn about
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
        cfg["commands"]["test"] = "true"
        r.write(".sdlc/config.json", json.dumps(cfg))
        r.git("commit", "-qam", "and the config")
        self.assertIn(".sdlc/config.json (modified)", guard_files_changed(Config(r.root), Git(r.root), "main", "sdlc/x"))

    def guard(self, change):
        """What the guard says about a branch off main whose one change is `change(config dict)` — or, for a string, that
        text as the whole file; None when the file is deleted."""
        from sdlclib.config import Config
        from sdlclib.gate import guard_files_changed
        from sdlclib.gitops import Git
        r = self.repo
        r.git("checkout", "-q", "-B", "sdlc/x", "main")
        cfg_on_main = Config(r.root)                                            # the file the loop gates by: main's
        if change is None:
            r.git("rm", "-q", ".sdlc/config.json")
        elif isinstance(change, str):
            r.write(".sdlc/config.json", change)
        else:
            cfg = json.loads(r.read(".sdlc/config.json"))
            change(cfg)
            r.write(".sdlc/config.json", json.dumps(cfg, indent=4, sort_keys=True))
        r.git("add", "-A", "-f")
        r.git("commit", "-q", "--allow-empty", "-m", "config")
        out = guard_files_changed(cfg_on_main, Git(r.root), "main", "sdlc/x")
        r.git("checkout", "-q", "main")
        return out

    def new_project(self):
        """A project that has not recorded its commands yet: a config with an empty `commands` and one empty command."""
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["commands"] = {"lint": ""}
        self.repo.write(".sdlc/config.json", json.dumps(cfg, indent=2))
        self.repo.git("commit", "-qam", "a new project: no commands yet")

    def test_filling_commands_that_have_no_value_yet_is_not_a_guard_change(self):
        self.new_project()
        def fill(cfg):
            cfg["commands"].update({"build": "make", "test": "pytest", "lint": "ruff check .", "run": "python app.py"})
        self.assertIsNone(self.guard(fill))                                   # a missing key and an empty string both count as unset
        self.assertIsNone(self.guard(lambda c: c["commands"].update(test="pytest")))
        self.assertIsNone(self.guard(lambda c: c.pop("commands")))            # nothing at all: no semantic change
        self.assertIsNone(self.guard(lambda c: c["commands"].update(format="")))    # an empty command next to an unset one
        self.assertIsNone(self.guard(lambda c: c.update(commands={})))        # the same as `lint: ""`

    def test_a_fill_is_judged_against_where_the_branch_left_main_not_main_now(self):
        """Main moved on after the branch was cut — someone changed another key — which is not the branch's change."""
        from sdlclib.config import Config
        from sdlclib.gate import guard_files_changed
        from sdlclib.gitops import Git
        self.new_project()
        r = self.repo
        r.git("checkout", "-q", "-b", "sdlc/x")
        cfg = json.loads(r.read(".sdlc/config.json"))
        cfg["commands"]["lint"] = "ruff ."
        r.write(".sdlc/config.json", json.dumps(cfg, indent=2))
        r.git("commit", "-qam", "the skeleton fills lint")
        r.git("checkout", "-q", "main")
        cfg = json.loads(r.read(".sdlc/config.json"))
        cfg["loop"]["poll_minutes"] = 5
        r.write(".sdlc/config.json", json.dumps(cfg, indent=2))
        r.git("commit", "-qam", "a person changed the loop")
        self.assertIsNone(guard_files_changed(Config(r.root), Git(r.root), "main", "sdlc/x"))

    def test_a_reformatted_config_changes_nothing(self):
        self.assertIsNone(self.guard(lambda c: None))                        # re-indented and key-sorted by the helper

    def test_changing_removing_or_blanking_a_command_that_has_a_value_goes_to_a_person(self):
        guard = ".sdlc/config.json (modified)"
        for what, change in (("change", lambda c: c["commands"].update(test="true")),
                             ("remove", lambda c: c["commands"].pop("build")),
                             ("blank", lambda c: c["commands"].update(build="")),
                             ("whitespace", lambda c: c["commands"].update(build="  ")),
                             ("null", lambda c: c["commands"].update(test=None))):
            self.assertIn(guard, self.guard(change) or "", what)

    def test_a_fill_beside_any_other_change_goes_to_a_person(self):
        self.new_project()
        for what, change in (("other key", lambda c: c.update(loop={"self_check": True})),
                             ("a protection off", lambda c: c.update(protect={"protect_tests": False})),
                             ("the gate's list", lambda c: c.update(gate={"commands": []})),
                             ("a command and the loop", lambda c: (c["commands"].update(test="pytest"), c.update(mode="team"))),
                             ("an unknown key", lambda c: c.update(x=0)),
                             ("not a string", lambda c: c["commands"].update(test=["pytest"])),
                             ("a blank string", lambda c: c["commands"].update(test="  ")),
                             ("commands is not an object", lambda c: c.update(commands="pytest"))):
            self.assertIn(".sdlc/config.json (modified)", self.guard(change) or "", what)

    def test_a_config_that_is_no_longer_json_or_is_gone_goes_to_a_person(self):
        self.new_project()
        self.assertIn(".sdlc/config.json (modified)", self.guard("{not json"))
        self.assertIn(".sdlc/config.json (modified)", self.guard("[]"))
        self.assertIn(".sdlc/config.json (deleted or renamed)", self.guard(None))

    def test_the_other_guard_files_are_not_softened_by_a_commands_fill(self):
        self.new_project()
        self.repo.git("checkout", "-q", "-B", "sdlc/x", "main")
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["commands"]["lint"] = "ruff ."
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.write(".claude/settings.json", "{}\n")
        self.repo.git("add", "-A", "-f")
        self.repo.git("commit", "-q", "-m", "both")
        from sdlclib.config import Config
        from sdlclib.gate import guard_files_changed
        from sdlclib.gitops import Git
        out = guard_files_changed(Config(self.repo.root), Git(self.repo.root), "main", "sdlc/x")
        self.assertEqual(out, "guard files changed, a person merges these — .claude/settings.json (added)")

    def gate_events(self):
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            return [json.loads(l) for l in fh if '"loop.gate"' in l]

    def no_commands_yet(self):
        """A project that has recorded no commands: its first item is the skeleton that does."""
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["commands"] = {}
        self.repo.write(".sdlc/config.json", json.dumps(cfg, indent=2))
        self.repo.git("commit", "-qam", "a new project: no commands yet")

    def test_a_skeleton_that_fills_commands_is_gated_by_them_and_merges_by_itself(self):
        self.no_commands_yet()
        self.repo.add_items("a-skeleton", ("b-second", {"deps": ["a-skeleton"]}))
        fill = {"build": "true", "test": "test ! -f FAIL", "lint": "echo lint", "run": "echo run"}
        self.repo.scenario({"a-skeleton:build": ["config:" + json.dumps({"commands": fill})]})
        self.assertEqual(self.repo.engine().run(), 0)
        for slug in ("a-skeleton", "b-second"):
            self.assertEqual(self.repo.status_of(slug), "done", slug)
            self.assertEqual(self.repo.items_state()[slug].get("reasons", []), [], slug)
        self.assertEqual(json.loads(self.repo.show("main", ".sdlc/config.json"))["commands"], fill)
        gates = {e["detail"]["slug"]: e["detail"] for e in self.gate_events()}
        # the skeleton runs what it fills in (nothing runs `run`: no gate command names it), the item after it what the
        # skeleton merged — and only the skeleton's gate says its commands are the branch's own
        self.assertEqual({slug: g["ran"] for slug, g in gates.items()},
                         {"a-skeleton": ["build", "test", "lint"], "b-second": ["build", "test", "lint"]})
        self.assertEqual({slug: g["filled"] for slug, g in gates.items()},
                         {"a-skeleton": ["build", "test", "lint"], "b-second": []})

    def test_a_filled_command_that_fails_sends_the_skeleton_to_a_fix_session_not_to_a_person(self):
        self.no_commands_yet()
        self.repo.add_items("a-skeleton")
        fill = {"build": "true", "test": "test ! -f FAIL"}
        self.repo.scenario({"a-skeleton:build": ["config_fail:" + json.dumps({"commands": fill})]})
        self.assertEqual(self.repo.engine().run(), 0)
        first, second = [e["detail"] for e in self.gate_events()]
        self.assertEqual((first["passed"], first["ran"], first["fixable"], first["human"]),
                         (False, ["build", "test"], ["`test ! -f FAIL` failed (exit 1)"], []))
        self.assertEqual((second["passed"], second["ran"]), (True, ["build", "test"]))
        self.assertIn("a-skeleton:fix", [k for k, *_ in self.keys()])
        self.assertEqual(self.repo.status_of("a-skeleton"), "done")
        self.assertEqual(self.repo.items_state()["a-skeleton"].get("reasons", []), [])
        self.assertEqual(json.loads(self.repo.show("main", ".sdlc/config.json"))["commands"], fill)

    def test_the_fix_session_is_told_not_to_make_a_filled_command_that_cannot_fail(self):
        self.no_commands_yet()
        self.repo.add_items("a-skeleton")
        fill = {"build": "true", "test": "test ! -f FAIL"}
        self.repo.scenario({"a-skeleton:build": ["config_fail:" + json.dumps({"commands": fill})]})
        self.assertEqual(self.repo.engine().run(), 0)
        (fix,) = [c for c in self.repo.calls() if c["key"] == "a-skeleton:fix"]
        self.assertIn("`test ! -f FAIL` failed (exit 1)", fix["prompt"])
        self.assertIn("The gate commands `build`, `test` come from this branch's own `.sdlc/config.json` and are this project's "
                      "gate from now on.", fix["prompt"])
        self.assertIn("Never make it something that cannot fail (`true`, `exit 0`, `|| true`, an empty run).", fix["prompt"])

    def test_the_warning_names_only_the_commands_the_branch_filled_in(self):
        """The project has build and test already; the branch adds lint — which fails, and a fix session corrects."""
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["config:" + json.dumps({"commands": {"lint": "exit 4"}})],
                            "a-first:fix": ["fix_config:" + json.dumps({"commands": {"lint": "echo linted"}})]})
        self.assertEqual(self.repo.engine().run(), 0)
        first, second = [e["detail"] for e in self.gate_events()]
        self.assertEqual((first["ran"], first["filled"], first["fixable"]),
                         (["build", "test", "lint"], ["lint"], ["`exit 4` failed (exit 4)"]))
        self.assertEqual((second["passed"], second["filled"]), (True, ["lint"]))
        (fix,) = [c for c in self.repo.calls() if c["key"] == "a-first:fix"]
        self.assertIn("The gate commands `lint` come from this branch's own `.sdlc/config.json`", fix["prompt"])
        self.assertNotIn("`build`, ", fix["prompt"])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_wrong_command_a_skeleton_filled_in_is_corrected_by_a_fix_session_before_it_merges(self):
        self.no_commands_yet()
        self.repo.add_items("a-skeleton")
        self.repo.scenario({"a-skeleton:build": ["config:" + json.dumps({"commands": {"build": "true", "test": "exit 3"}})],
                            "a-skeleton:fix": ["fix_config:" + json.dumps({"commands": {"test": "echo fixed"}})]})
        self.assertEqual(self.repo.engine().run(), 0)
        first, second = [e["detail"] for e in self.gate_events()]
        self.assertEqual((first["passed"], first["fixable"], first["human"]), (False, ["`exit 3` failed (exit 3)"], []))
        self.assertEqual((second["passed"], second["ran"], second["human"]), (True, ["build", "test"], []))
        self.assertEqual(self.repo.status_of("a-skeleton"), "done")                      # still only a fill: no guard, no person
        self.assertEqual(json.loads(self.repo.show("main", ".sdlc/config.json"))["commands"],
                         {"build": "true", "test": "echo fixed"})

    def test_the_gate_runs_the_command_in_force_not_the_one_a_branch_changed_and_the_branch_still_goes_to_a_person(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["config_fail:" + json.dumps({"commands": {"test": "true"}})]})
        self.assertEqual(self.repo.engine().run(), 0)
        first = self.gate_events()[0]["detail"]
        # the branch's `test: true` would pass with FAIL there: what failed is the command the project has
        self.assertEqual((first["ran"], first["fixable"], first["filled"]),
                         (["build", "test"], ["`test ! -f FAIL` failed (exit 1)"], []))
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        self.assertEqual(rec["reasons"], ["guard files changed, a person merges these — .sdlc/config.json (modified)"])
        self.assertEqual(json.loads(self.repo.show("main", ".sdlc/config.json"))["commands"]["test"], "test ! -f FAIL")

    def test_a_fill_beside_another_change_is_not_run_and_goes_to_a_person(self):
        self.no_commands_yet()
        self.repo.add_items("a-skeleton")
        patch = {"commands": {"test": "test ! -f FAIL"}, "loop": {"poll_minutes": 5}}
        self.repo.scenario({"a-skeleton:build": ["config_fail:" + json.dumps(patch)]})
        self.assertEqual(self.repo.engine().run(), 0)
        gate = self.gate_events()[0]["detail"]
        self.assertEqual((gate["ran"], gate["fixable"], gate["filled"]), ([], [], []))
        self.assertEqual(self.repo.items_state()["a-skeleton"]["reasons"],
                         ["guard files changed, a person merges these — .sdlc/config.json (modified)"])

    def built_skeleton(self, fill, extra=()):
        """What an interactive /sdlc:go leaves on sdlc/a-skeleton: a built work doc and a config that fills in the commands."""
        r = self.repo
        r.add_items("a-skeleton")
        r.git("checkout", "-q", "-b", "sdlc/a-skeleton")
        r.write("work/a-skeleton.md", r.doc_text("a-skeleton", status="built") +
                "\n## Plan\n1. Write src/a-skeleton.txt.\n\n## Proof\n| R | check | kind |\n|---|---|---|\n| R1 | test command | auto |\n"
                "\n## Review\nSDLC_REVIEW_TALLY important=0 nits=1 validated=yes\n")
        r.write("src/a-skeleton.txt", "built\n")
        cfg = json.loads(r.read(".sdlc/config.json"))
        cfg["commands"] = fill
        r.write(".sdlc/config.json", json.dumps(cfg, indent=2))
        for path in extra:
            r.write(path, "x\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "build a-skeleton")

    def test_ship_gates_a_skeleton_by_the_commands_it_fills_in_too(self):
        self.no_commands_yet()
        self.built_skeleton({"build": "true", "test": "test ! -f FAIL", "lint": "echo lint"}, extra=["FAIL"])
        r = self.repo.cli("ship", "a-skeleton")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)                          # a command failed: fix it and ship again
        self.assertIn("`test ! -f FAIL` failed (exit 1)", r.stdout)
        self.assertIn("The gate commands `build`, `test`, `lint` come from this branch's own `.sdlc/config.json`", r.stdout)
        self.assertEqual(self.gate_events()[-1]["detail"]["ran"], ["build", "test", "lint"])
        self.repo.git("rm", "-q", "FAIL")
        self.repo.git("commit", "-q", "-m", "fix")
        r = self.repo.cli("ship", "a-skeleton")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.gate_events()[-1]["detail"]["ran"], ["build", "test", "lint"])
        self.assertEqual(self.repo.status_of("a-skeleton"), "done")
        self.assertEqual(json.loads(self.repo.show("main", ".sdlc/config.json"))["commands"]["lint"], "echo lint")

    def test_the_gate_command_runs_what_a_skeleton_fills_in_too(self):
        self.no_commands_yet()
        self.built_skeleton({"build": "true", "test": "echo tested"})
        r = self.repo.cli("gate", "a-skeleton", "--json")
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["passed"], out["ran"], out["filled"]), (0, True, ["build", "test"], ["build", "test"]))
        out = json.loads(self.repo.cli("gate", "a-skeleton", "--json", "--no-commands").stdout)       # nothing runs, nothing is filled
        self.assertEqual((out["ran"], out["filled"]), ([], []))

    def test_changing_a_command_that_has_a_value_still_waits_for_a_person(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ['config:{"commands": {"test": "true"}}']})
        self.assertEqual(self.repo.engine().run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        self.assertEqual(rec["reasons"], ["guard files changed, a person merges these — .sdlc/config.json (modified)"])
        self.assertEqual(json.loads(self.repo.show("main", ".sdlc/config.json"))["commands"]["test"], "test ! -f FAIL")

    def test_an_item_that_breaks_the_config_waits_for_a_person_too(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["config_raw:{not json"], "b-second:build": ["remove:.sdlc/config.json"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"],
                         ["guard files changed, a person merges these — .sdlc/config.json (modified)"])
        self.assertEqual(self.repo.items_state()["b-second"]["reasons"],
                         ["guard files changed, a person merges these — .sdlc/config.json (deleted or renamed)"])


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
        self.assertIn("merged", rec)
        self.assertNotIn("reasons", rec)
        self.assertEqual(self.repo.status_of("b-second"), "done")                         # its dependent was released and ran
        self.assertEqual(self.counts(), {"done": 2})
        with open(e.log_path) as fh:
            self.assertIn("MERGED  a-first — merged by a person", fh.read())
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:build"), 1)           # the loop did not build it again
        self.assertNotIn("sdlc/a-first", self.repo.git("branch", "--list"))                # the merged branch went with it

    def test_a_merged_branch_is_removed_on_a_repository_without_a_remote(self):
        self.repo.git("branch", "sdlc/a-first")                                             # at main's tip: merged by any measure
        self.repo.git("branch", "sdlc/b-second")
        self.repo.git("checkout", "-q", "sdlc/b-second")
        e = self.repo.engine()
        e.drop_merged_branch("a-first")
        e.drop_merged_branch("b-second")                                                    # checked out: `-d` refuses, no error
        e.drop_merged_branch("no-such-item")
        branches = self.repo.git("branch", "--list")
        self.assertNotIn("sdlc/a-first", branches)
        self.assertIn("sdlc/b-second", branches)

    def test_a_branch_a_person_squashed_is_left_in_place_without_an_error(self):
        self.waiting()
        self.repo.git("merge", "--squash", "sdlc/a-first")
        self.repo.git("commit", "-q", "-m", "a-first, taken by hand")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "merged")
        self.assertIn("sdlc/a-first", self.repo.git("branch", "--list"))                   # `-d` refuses a branch that is not merged
        self.assertEqual(self.repo.status_of("b-second"), "done")

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

    def test_the_uncommitted_work_of_a_stopped_item_is_continued_first_and_said_after_the_header(self):
        """The branch of the item a session stopped in, with that session's work uncommitted: the run continues it there — and
        its log still opens with the header line, as every run's does."""
        from sdlclib import state as st
        self.repo.add_items("a-first")
        self.repo.git("checkout", "-q", "-b", "sdlc/a-first")
        self.repo.write("wip.txt", "half done\n")
        st.update(self.repo.root, "a-first", stop={"sid": "", "phase": "design", "at": 0, "why": "turn cap", "fp": "", "model": ""})
        self.assertEqual(self.repo.engine().run(), 0)
        said = [line.split(" ", 1)[1] for line in latest_run_log(self.repo).splitlines()]      # past the time of day
        self.assertTrue(said[0].startswith("== sdlc run"), said[:2])
        self.assertEqual(said[1], "RESUME  a-first — the session stopped last time left uncommitted changes here; continuing it first")
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def start_and_stop(self, first):
        """Detach a loop whose build session sits in the stub action `first` until `stop` comes; what stop printed."""
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": [first, "ok"]})
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
        return r

    def resume_to_done(self):
        r = self.repo.cli("resume", "--claude-bin", STUBS + "/claude")
        self.assertEqual(r.returncode, 0, r.stdout)
        for _ in range(300):
            if self.repo.status_of("a-first") == "done":
                break
            time.sleep(0.1)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_stop_now_interrupts_the_session_and_resume_continues_it(self):
        self.start_and_stop("sleep:60")
        stop = self.repo.items_state()["a-first"]["stop"]
        self.assertEqual(stop["phase"], "build")
        self.resume_to_done()
        builds = [c for c in self.repo.calls() if c["key"] == "a-first:build"]
        self.assertTrue(builds[-1]["resumed"])

    def test_stop_is_not_a_failure_when_claude_answers_the_interrupt_with_a_success_result(self):
        """A real claude that gets SIGINT still writes a result — `success`, no text, terminal_reason aborted_streaming."""
        self.start_and_stop("sleep_graceful:60")
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["stop"]["phase"], "build")
        self.assertFalse(rec.get("failures"))
        self.assertNotIn("last_error", rec)
        self.resume_to_done()
        builds = [c for c in self.repo.calls() if c["key"] == "a-first:build"]
        self.assertEqual([c["action"] for c in builds], ["sleep_graceful:60", "ok"])
        self.assertTrue(builds[-1]["resumed"])                      # the same session continues, not a fresh one
        self.assertEqual(builds[-1]["sid"], builds[0]["sid"])
        self.assertFalse(self.repo.items_state()["a-first"].get("failures"))
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            self.assertEqual([json.loads(l)["event"] for l in fh if '"loop.item"' in l and '"fail"' in l], [])

    def test_a_session_cut_short_by_something_else_is_a_stop_too(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["aborted:aborted_tools", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["stop"]["phase"], bool(rec.get("failures"))), ("build", False))
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "planned")

    def test_work_that_was_done_before_the_cut_still_counts(self):
        """The doc says what happened, not the result event: a phase that finished is not repeated because of how it ended."""
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["aborted_after_work"], "a-first:build": ["aborted_after_work"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        rec = self.repo.items_state()["a-first"]
        self.assertFalse(rec.get("stop") or rec.get("failures"))

    def test_a_session_that_ends_silent_says_so_in_its_failure(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["silent"]})
        self.repo.engine().run()
        err = self.repo.items_state()["a-first"]["last_error"]
        self.assertIn("build session ended without finishing it: it printed no final message", err)
        self.assertIn("3 turns", err)


class DirtyTree(RepoCase):
    """Uncommitted work is what somebody wrote — not the bytecode and the caches a test run leaves behind, which git folds
    into a bare `?? dir/` when nothing else is in the new directory."""

    def setUp(self):
        super().setUp()
        self.repo.write(".gitignore", ".sdlc/state/\n.sdlc/logs/\n.sdlc/worktrees/\n")    # this project does not ignore bytecode
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "no bytecode in .gitignore")

    def litter(self):
        self.repo.write("tally/__pycache__/x.cpython-312.pyc", "\0")
        self.repo.write("tests/__pycache__/t.cpython-312-pytest-8.0.0.pyc", "\0")
        self.repo.write(".pytest_cache/v/cache/lastfailed", "{}")
        self.repo.write("tools/old.pyc", "\0")
        self.repo.write("lib/__pycache__/mod.func-12.py312.nbi", "x")          # a cache file that does not end in .pyc

    def dirty(self):
        from sdlclib.gitops import Git
        return Git(self.repo.root).dirty()

    def run_log(self):
        return latest_run_log(self.repo)

    def test_a_new_package_holding_only_bytecode_is_clean(self):
        self.litter()
        self.assertIn("?? tally/\n", self.repo.git("status", "--porcelain") + "\n")      # what plain git says: a folded dir
        self.assertEqual(self.dirty(), [])

    def test_a_real_file_beside_the_bytecode_is_dirty_and_named(self):
        self.litter()
        self.repo.write("tally/real.py", "x = 1\n")
        self.repo.write("notes/sub/idea.md", "later\n")
        self.assertEqual(self.dirty(), ["?? notes/sub/idea.md", "?? tally/real.py"])

    def test_tracked_changes_and_renames_are_reported_once_by_their_new_path(self):
        self.repo.git("mv", "tests/test_base.py", "tests/test_moved.py")
        self.repo.write("package.json", "{}\n")
        self.assertEqual(self.dirty(), [" M package.json", "R  tests/test_moved.py"])

    def test_the_engines_own_files_are_not_dirt(self):
        self.repo.write(".sdlc/state/x.json", "{}")
        self.repo.write(".sdlc/logs/y.log", "x")
        self.assertEqual(self.dirty(), [])

    def test_the_loop_starts_on_a_tree_with_only_bytecode_and_does_not_commit_it(self):
        self.repo.add_items("a-first")
        self.litter()
        self.repo.scenario({"a-first:build": ["dirty_built"]})        # leftovers make the engine commit for the session
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertIsNotNone(self.repo.show("main", "LEFTOVER-a-first"))
        tracked = self.repo.git("ls-files").splitlines()
        self.assertEqual([p for p in tracked if p.endswith(".pyc") or ".pytest_cache" in p], [])

    def test_the_stop_fingerprint_ignores_bytecode_but_not_real_files(self):
        from sdlclib.gitops import Git
        git = Git(self.repo.root)
        clean = git.diff_hash()
        self.litter()
        self.assertEqual(git.diff_hash(), clean)
        self.repo.write("tally/real.py", "x = 1\n")
        self.assertNotEqual(git.diff_hash(), clean)

    def test_the_loop_refuses_a_tree_with_a_real_file_and_names_it(self):
        self.repo.add_items("a-first")
        self.litter()
        self.repo.write("tally/real.py", "x = 1\n")
        self.assertEqual(self.repo.engine().run(), 2)
        self.assertEqual(self.repo.calls(), [])
        log = self.run_log()
        self.assertIn("uncommitted changes (?? tally/real.py)", log)
        self.assertNotIn("pyc", log)

    def test_a_refusal_names_up_to_three_files_and_counts_the_rest(self):
        self.repo.add_items("a-first")
        for n in "abcde":
            self.repo.write("mine/%s.txt" % n, n)
        self.assertEqual(self.repo.engine().run(), 2)
        self.assertIn("(?? mine/a.txt; ?? mine/b.txt; ?? mine/c.txt … (+2 more))", self.run_log())

    def test_ship_goes_past_bytecode_and_names_what_it_refuses(self):
        self.repo.add_items("a-first")
        self.repo.git("checkout", "-qb", "sdlc/a-first")
        self.litter()
        r = self.repo.cli("ship", "a-first")
        self.assertEqual(r.returncode, 1)
        self.assertIn("work/a-first.md says 'ready'", r.stdout)                  # the dirty check let it through
        self.repo.write("tally/real.py", "x = 1\n")
        r = self.repo.cli("ship", "a-first")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "commit your changes first: ?? tally/real.py"))

    def test_the_gate_command_names_what_it_refuses_too(self):
        self.repo.add_items("a-first")
        self.repo.git("checkout", "-qb", "sdlc/a-first")
        self.repo.write("tally/real.py", "x = 1\n")
        r = self.repo.cli("gate", "a-first", "--no-commands")
        self.assertEqual(r.returncode, 1)
        self.assertIn("with a clean tree first (uncommitted: ?? tally/real.py)", r.stderr)


class LinkedWorktree(RepoCase):
    """The default branch is checked out in the main checkout and the loop or the user works in a linked worktree: what lands
    on the default branch lands there, and what the main checkout holds uncommitted decides whether it can."""

    def setUp(self):
        super().setUp()
        self.repo.write(".gitignore", ".sdlc/state/\n.sdlc/logs/\n.sdlc/worktrees/\n")     # this project does not ignore bytecode
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "no bytecode in .gitignore")
        self.wt = os.path.join(self.repo.tmp, "linked")

    def bytecode(self):
        self.repo.write("tally/__pycache__/x.cpython-312.pyc", "\0")
        self.repo.write(".pytest_cache/v/cache/lastfailed", "{}")

    def enqueue_from_the_worktree(self):
        from sdlclib.config import Config
        from sdlclib.enqueue import enqueue, prepare
        from sdlclib.gitops import Git, make_forge
        self.repo.git("worktree", "add", "-q", "-b", "feature", self.wt)
        cfg, git = Config(self.wt), Git(self.wt)
        path = os.path.join(self.wt, "work", "x-new.md")
        with open(path, "w") as fh:
            fh.write(self.repo.doc_text("x-new"))
        return enqueue(cfg, git, make_forge(git, "main"), prepare(cfg, git, git.base_ref("main"), [path]), [path])

    def test_docs_are_queued_into_a_main_checkout_that_holds_only_bytecode(self):
        self.bytecode()
        result = self.enqueue_from_the_worktree()
        self.assertEqual(result["how"], "committed on main")
        self.assertIsNotNone(self.repo.show("main", "work/x-new.md"))

    def test_a_real_uncommitted_file_in_the_main_checkout_stops_it_and_is_named(self):
        from sdlclib.enqueue import EnqueueError
        self.bytecode()
        self.repo.write("scratch.txt", "mine\n")
        with self.assertRaises(EnqueueError) as ctx:
            self.enqueue_from_the_worktree()
        self.assertIn("with uncommitted changes (?? scratch.txt) — commit them first", str(ctx.exception))
        self.assertIsNone(self.repo.show("main", "work/x-new.md"))

    def merge_from_the_worktree(self, left_in_main=()):
        """Merge sdlc/a-first from a linked worktree that has it checked out, with `left_in_main` (path, text) files lying
        around uncommitted in the main checkout, where main is checked out."""
        from sdlclib.gitops import Git, LocalForge
        self.repo.git("checkout", "-q", "-b", "sdlc/a-first")
        self.repo.write("src/a-first.txt", "built\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "build")
        self.repo.git("checkout", "-q", "main")
        for rel, text in left_in_main:
            self.repo.write(rel, text)
        self.repo.git("worktree", "add", "-q", self.wt, "sdlc/a-first")
        return LocalForge(Git(self.wt), "main").merge("a-first", "a-first")

    def test_a_merge_from_the_worktree_goes_past_bytecode_in_the_main_checkout(self):
        ok, info = self.merge_from_the_worktree([("tally/__pycache__/x.cpython-312.pyc", "\0"),
                                                 (".pytest_cache/v/cache/lastfailed", "{}")])
        self.assertTrue(ok, info)
        self.assertEqual(self.repo.show("main", "src/a-first.txt"), "built\n")

    def test_a_merge_from_the_worktree_names_what_the_main_checkout_holds(self):
        ok, info = self.merge_from_the_worktree([("scratch.txt", "mine\n"), ("tally/__pycache__/x.cpython-312.pyc", "\0")])
        self.assertFalse(ok)
        self.assertIn("with uncommitted changes (?? scratch.txt)", info)
        self.assertIsNone(self.repo.show("main", "src/a-first.txt"))


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
        self.assertIn("nothing to do: nothing runnable — 1 wait for you (a-idea)", r.stdout)
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
    config = {"loop": {"self_check": True, "self_check_min_items": 1}}      # one merged item is enough for these tests

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

    def test_findings_that_wait_for_a_person_are_counted_when_the_run_stops(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "old-debt", "status": "proposed"},
                                               {"slug": "more-debt", "status": "proposed"}]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertIn("stop=nothing runnable — 2 wait for you (more-debt, old-debt)", latest_run_log(self.repo))
        self.assertEqual(self.repo.status_of("old-debt"), "proposed")

    def test_findings_that_are_only_proposed_are_committed_as_a_proposal(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "old-debt", "status": "proposed"},
                                               {"slug": "more-debt", "status": "proposed"}]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.git("log", "-1", "--format=%s", "--", "work/old-debt.md"), "sdlc: propose 2 work items")

    def test_findings_that_are_partly_ready_are_still_a_queue_commit(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "fix-one", "status": "ready"}, {"slug": "old-debt", "status": "proposed"}]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.git("log", "-1", "--format=%s", "--", "work/old-debt.md"), "sdlc: queue 2 work items")

    def test_a_finding_whose_slug_is_a_file_work_keeps_for_itself_is_not_queued(self):
        """work/README.md is gone first: the finding is then a new file on any file system. Where case does not count, the
        session writing `work/readme.md` would have replaced README.md, and nothing the loop checks afterwards undoes that."""
        self.repo.git("rm", "-q", "work/README.md")
        self.repo.git("commit", "-q", "-m", "no readme")
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "readme", "status": "ready"}, {"slug": "template", "status": "ready"},
                                               {"slug": "fix-one", "status": "ready"}]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("fix-one"), "done")
        for slug in ("readme", "template"):
            self.assertIsNone(self.repo.show("main", "work/%s.md" % slug))
            self.assertFalse(os.path.exists(self.repo.path("work/%s.md" % slug)))
            self.assertNotIn(slug, self.repo.items_state())

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


class SelfCheckMinItems(RepoCase):
    """One merged item is not reviewed again by the self-check — its own review covered the whole diff. It takes
    `loop.self_check_min_items` of them (2 unless the config says otherwise), counted from the run's start or the last check."""
    config = {"loop": {"self_check": True}}
    SKIPPED = "CHECK   skipped — 1 item merged since the last check; its own review covered it (loop.self_check_min_items 2)"

    def checks(self):
        return [k for k, *_ in self.keys()].count("self-check")

    def set_loop(self, **loop):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["loop"].update(loop)
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "loop config")

    def test_a_single_merged_item_is_not_self_checked_and_the_log_says_so(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertEqual(self.checks(), 0)
        self.assertEqual(e.self_checks, 0)
        self.assertEqual(e.merged_since_check, ["a-first"])
        self.assertEqual(latest_run_log(self.repo).count(self.SKIPPED), 1)

    def test_the_skip_is_said_once_per_run_however_often_the_question_is_asked(self):
        def skips(e):
            said = []
            e.say = said.append
            e.from_sha, e.merged_since_check = "0" * 40, ["a-first"]
            self.assertEqual([e.self_check_due() for _ in range(3)], [False] * 3)
            return [l for l in said if l.startswith("CHECK   skipped")]

        self.assertEqual(skips(self.repo.engine()), [self.SKIPPED])
        self.assertEqual(skips(self.repo.engine()), [self.SKIPPED])                   # the next run says it again

    def test_two_merged_items_are_self_checked_and_the_count_is_cleared(self):
        self.repo.add_items("a-first", "b-second")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.checks(), 1)
        self.assertEqual(e.merged_since_check, [])
        self.assertNotIn("CHECK   skipped", latest_run_log(self.repo))

    def test_one_item_is_checked_when_the_config_asks_for_one_and_zero_never_means_none(self):
        for value in (1, 0):
            self.set_loop(self_check_min_items=value)
            slug = "item-%d" % value
            self.repo.add_items(slug)
            before = self.checks()
            self.assertEqual(self.repo.engine().run(), 0)
            self.assertEqual(self.checks(), before + 1, value)

    def test_a_value_that_is_not_a_number_is_the_default(self):
        self.set_loop(self_check_min_items="a couple")
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.checks(), 0)
        self.assertIn(self.SKIPPED, latest_run_log(self.repo))

    def test_a_higher_minimum_skips_more_and_the_line_counts_them(self):
        self.set_loop(self_check_min_items=3)
        self.repo.add_items("a-first", "b-second")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.checks(), 0)
        self.assertIn("CHECK   skipped — 2 items merged since the last check; their own reviews covered them "
                      "(loop.self_check_min_items 3)", latest_run_log(self.repo))

    def test_the_count_starts_over_after_a_self_check(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"selfcheck_docs": [{"slug": "fix-one", "status": "ready"}]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("fix-one"), "done")                  # the check found it, the loop built it
        self.assertEqual(self.checks(), 1)                                        # and that one item is not checked again
        self.assertEqual(e.merged_since_check, ["fix-one"])
        self.assertIn(self.SKIPPED, latest_run_log(self.repo))

    def test_every_condition_still_counts(self):
        e = self.repo.engine()
        e.from_sha = "0" * 40
        self.assertFalse(e.self_check_due())                                      # nothing merged
        e.merged_since_check = ["a-first"]
        self.assertFalse(e.self_check_due())                                      # one is not enough
        e.merged_since_check = ["a-first", "b-second"]
        self.assertTrue(e.self_check_due())
        e.self_checks = 2
        self.assertFalse(e.self_check_due())                                      # two a run
        e.self_checks = 0
        e.only = ["a-first"]
        self.assertFalse(e.self_check_due())                                      # an --items run
        e.only = None
        e.team = True
        self.assertFalse(e.self_check_due())                                      # a person merges in team mode
        e.team = False
        e.cfg.data["loop"]["self_check"] = False
        self.assertFalse(e.self_check_due())
        e.cfg.data["loop"]["self_check"] = True
        e.from_sha = e.git.head()
        self.assertFalse(e.self_check_due())                                      # main has not moved since the run began
        e.from_sha = "0" * 40
        self.assertTrue(e.self_check_due())


class FixBlocked(RepoCase):
    """`SDLC_FIX_BLOCKED <why>` ends a fix session as references/fix.md promises: the item goes to a person with the note."""

    def blocked_run(self, action):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["fail_gate"], "a-first:fix": [action]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        return e

    def test_the_item_goes_to_a_person_with_the_reason_and_gets_no_further_fix_round(self):
        why = "the test needs a database the loop does not have"
        e = self.blocked_run("fix_blocked:" + why)
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:fix"), 1)           # fix_rounds allows two: this is not a round
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("needs-human", ["the fix session could not fix it: " + why]))
        self.assertNotIn("failures", rec)                                                 # a person's call, not a failed attempt
        self.assertEqual(self.repo.status_of("a-first"), "ready")                         # nothing was merged
        self.assertIn(why, self.repo.show("sdlc/a-first", "work/a-first.md"))             # the note in Departures went with the branch
        self.assertEqual(self.repo.status_of("b-second"), "done")                         # and the loop went on
        log = latest_run_log(self.repo)
        self.assertIn("HUMAN   a-first — the fix session could not fix it: " + why, log)
        self.assertEqual(log.count("GATE    a-first"), 1)                                 # the gate did not run again
        self.assertNotIn("FAILED  a-first", log)
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual([(n["slug"], n["kind"], n["text"]) for n in s["needs_you"]],
                         [("a-first", "human", ["the fix session could not fix it: " + why])])
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            items = [json.loads(l) for l in fh if '"loop.item"' in l and '"a-first"' in l]
        self.assertEqual([(i["decision"], i["detail"]["reasons"]) for i in items], [("needs-human", ["the fix session could not fix it: " + why])])

    def test_a_marker_without_text_sends_the_person_to_departures(self):
        self.blocked_run("fix_blocked")
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"], ["the fix session could not fix it: see Departures"])

    def test_a_note_the_session_left_uncommitted_is_committed_on_the_branch(self):
        self.blocked_run("fix_blocked_dirty:waiting on a vendor fix")
        self.assertIn("waiting on a vendor fix", self.repo.show("sdlc/a-first", "work/a-first.md"))
        self.assertEqual(self.repo.git("log", "-1", "--format=%s", "sdlc/a-first"), "sdlc(a-first): fix session changes")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_a_long_reason_is_cut_to_a_line_a_person_can_read(self):
        self.blocked_run("fix_blocked:" + "x" * 500)
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"], ["the fix session could not fix it: " + "x" * 300])

    def test_a_person_who_merges_the_branch_by_hand_finishes_it(self):
        self.blocked_run("fix_blocked:cannot run the suite here")
        self.repo.git("merge", "-q", "--no-edit", "sdlc/a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:fix"), 1)             # nothing was run for it again


class Continued(RepoCase):
    """A window that continues a stopped one says so in its `loop.session` event, and the engine logs the continuation."""

    def events(self, name):
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            return [e for e in map(json.loads, fh) if e["event"] == name]

    def builds(self):
        return [e["detail"] for e in self.events("loop.session") if e["detail"]["phase"] == "build"]

    def test_a_warm_resume_after_a_cap_is_continued_and_the_continuation_is_an_event(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_turns", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        builds = self.builds()
        self.assertEqual([(b["status"], b["resumed"], b["continued"]) for b in builds], [("max_turns", False, False), ("ok", True, True)])
        cont = self.events("loop.continue")
        self.assertEqual(len(cont), 1)
        self.assertEqual((cont[0]["decision"], cont[0]["reason"]), ("allow", "turn cap"))
        self.assertEqual(cont[0]["detail"], {"slug": "a-first", "phase": "build", "sid": builds[0]["sid"], "fresh": False,
                                             "continuations": 1})
        self.assertEqual([e["detail"]["continued"] for e in self.events("loop.session") if e["detail"]["phase"] == "design"], [False])
        self.assertIn("STOPPED a-first — turn cap; continuing (warm resume)", latest_run_log(self.repo))

    def test_a_cold_continuation_is_a_fresh_session_and_still_continued(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_turns", "ok"]})
        e = self.repo.engine()
        e.cold_seconds = 0
        self.assertEqual(e.run(), 0)
        self.assertEqual([(b["resumed"], b["continued"]) for b in self.builds()], [(False, False), (False, True)])
        self.assertIs(self.events("loop.continue")[0]["detail"]["fresh"], True)

    def test_a_usage_limit_that_is_waited_out_is_a_continuation_too(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_session:1", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        cont = self.events("loop.continue")
        self.assertEqual(len(cont), 1)
        self.assertIn("session limit", cont[0]["reason"])
        self.assertEqual((cont[0]["detail"]["slug"], cont[0]["detail"]["phase"], cont[0]["detail"]["continuations"]), ("a-first", "build", 0))
        self.assertEqual([(b["status"], b["continued"]) for b in self.builds()], [("limited", False), ("ok", True)])

    def test_every_continuation_of_a_session_is_an_event(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["max_turns", "max_turns", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([e["detail"]["continuations"] for e in self.events("loop.continue")], [1, 2])

    def test_a_stop_that_a_later_run_continues_marks_that_runs_first_window_though_nothing_continued_in_between(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_session:7200", "ok"]})
        e = self.repo.engine()
        e.limit_wait = 60
        self.assertEqual(e.run(), 0)                                                     # the wait is beyond the limit: the run stops
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([(b["status"], b["resumed"], b["continued"]) for b in self.builds()],
                         [("limited", False, False), ("ok", True, True)])
        self.assertEqual(self.events("loop.continue"), [])

    def test_the_first_session_of_an_item_is_not_a_continuation(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual({e["detail"]["continued"] for e in self.events("loop.session")}, {False})
        self.assertEqual(self.events("loop.continue"), [])


class RefusedStart(RepoCase):
    """A start that is refused is not a run: loop-status.json keeps the last run's `stop` and `ended` and gains `refused` and
    `refused_at`; `status` says both; the next run that does start writes a record without it."""

    def record(self):
        with open(self.repo.path(".sdlc/state/loop-status.json")) as fh:
            return json.load(fh)

    def a_finished_run(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        before = self.record()
        self.assertEqual(before["stop"], "queue empty")
        return before

    def test_a_dirty_tree_keeps_the_last_stop_and_adds_why_it_was_refused(self):
        before = self.a_finished_run()
        self.repo.write("scratch.txt", "mine\n")
        self.assertEqual(self.repo.engine().run(), 2)
        after = self.record()
        self.assertEqual((after["state"], after["stop"], after["ended"], after["done"]),
                         ("stopped", "queue empty", before["ended"], before["done"]))
        self.assertEqual(after["refused"], "the working tree has uncommitted changes (?? scratch.txt) — commit or stash them first")
        self.assertRegex(after["refused_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/loop.pid")))
        out = self.repo.cli("status").stdout
        self.assertRegex(out.splitlines()[0], r"^loop: not running · last stop: queue empty · start refused \d+s ago: "
                                              r"the working tree has uncommitted changes \(\?\? scratch\.txt\)")
        loop = json.loads(self.repo.cli("status", "--json").stdout)["loop"]
        self.assertEqual((loop["running"], loop["last_stop"], loop["refused"]), (False, "queue empty", after["refused"]))
        self.assertRegex(loop["refused_ago"], r"^\d+s$")

    def test_a_run_that_starts_afterwards_writes_a_record_without_it(self):
        self.a_finished_run()
        self.repo.write("scratch.txt", "mine\n")
        self.assertEqual(self.repo.engine().run(), 2)
        os.remove(self.repo.path("scratch.txt"))
        self.repo.add_items("b-second")
        self.assertEqual(self.repo.engine().run(), 0)
        after = self.record()
        self.assertNotIn("refused", after)
        self.assertNotIn("refused_at", after)
        self.assertEqual(after["stop"], "queue empty")
        out = self.repo.cli("status").stdout
        self.assertIn("loop: not running · last stop: queue empty\n", out + "\n")
        self.assertNotIn("refused", out)
        self.assertEqual(json.loads(self.repo.cli("status", "--json").stdout)["loop"]["refused"], "")

    def test_a_checkout_that_never_ran_has_no_last_stop_to_keep(self):
        self.repo.add_items("a-first")
        self.repo.write("scratch.txt", "mine\n")
        self.assertEqual(self.repo.engine().run(), 2)
        rec = self.record()
        self.assertEqual((rec["state"], "stop" in rec, "ended" in rec), ("stopped", False, False))
        self.assertRegex(self.repo.cli("status").stdout.splitlines()[0],
                         r"^loop: not running · start refused \d+s ago: the working tree has uncommitted changes")

    def test_a_refusal_before_the_tree_is_looked_at_is_kept_the_same_way(self):
        from sdlclib.engine import Engine
        before = self.a_finished_run()
        e = Engine(self.repo.root, claude_bin="/nonexistent/claude")
        self.assertEqual(e.run(), 2)
        after = self.record()
        self.assertEqual((after["stop"], after["ended"]), ("queue empty", before["ended"]))
        self.assertIn("claude executable not found", after["refused"])
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["loop"]["enabled"] = False
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "loop off")
        self.assertEqual(self.repo.engine().run(), 2)
        self.assertIn("the loop is switched off", self.record()["refused"])
        self.assertEqual(self.record()["stop"], "queue empty")

    def test_a_loop_that_is_already_running_is_not_a_refusal_and_its_record_is_left_alone(self):
        self.a_finished_run()
        path = self.repo.path(".sdlc/state/loop-status.json")
        with open(path) as fh:
            before = fh.read()
        self.repo.write(".sdlc/state/loop.pid", "%d\n" % os.getppid())               # a live process that is not this one
        self.assertEqual(self.repo.engine().run(), 2)
        with open(path) as fh:
            self.assertEqual(fh.read(), before)
        self.assertIn("a loop is already running in this checkout", latest_run_log(self.repo))

    def test_a_pause_is_no_refusal(self):
        self.a_finished_run()
        self.repo.cli("stop", "--after-item")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertNotIn("refused", self.record())

    def test_a_dry_run_on_a_dirty_tree_still_works_and_writes_no_record(self):
        self.repo.add_items("a-first")
        self.repo.write("scratch.txt", "mine\n")
        self.assertEqual(self.repo.engine(dry_run=True).run(), 0)
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/loop-status.json")))
        self.assertEqual(self.repo.calls(), [])

    def test_status_shows_a_refusal_only_when_it_is_the_latest_word_and_no_loop_runs(self):
        from sdlclib.status import collect, render
        root = self.repo.root

        def loop(**rec):
            self.repo.write(".sdlc/state/loop-status.json", json.dumps(dict({"state": "stopped", "stop": "queue empty"}, **rec)))
            return collect(root)["loop"]

        refused = dict(refused="no claude", refused_at="2026-10-14T10:00:00Z")
        self.assertEqual(loop(ended="2026-10-14T09:00:00Z", **refused)["refused"], "no claude")        # after the last run
        self.assertEqual(loop(ended="2026-10-14T10:00:00Z", **refused)["refused"], "no claude")        # the same second
        shown = loop(ended="2026-10-14T11:00:00Z", **refused)                                          # a run ended after it
        self.assertEqual((shown["refused"], shown["refused_ago"]), ("", ""))
        self.assertEqual(loop(**refused)["refused"], "no claude")                                      # no run ever ended
        self.assertEqual(loop(refused="no claude")["refused"], "")                                     # undated: not shown
        self.assertEqual(loop(ended="2026-10-14T09:00:00Z")["refused"], "")
        self.repo.write(".sdlc/state/loop.pid", "%d\n" % os.getpid())                                  # a loop is running now
        self.assertEqual(loop(ended="2026-10-14T09:00:00Z", **refused)["refused"], "")
        self.assertNotIn("refused", render(collect(root)))


class Adoption(RepoCase):
    """A branch sdlc/<slug> that 0.x (or a person) made before the item had a work doc gets the doc when the engine checks
    the item out — committed on the branch — for an item that is already `planned`, and the item then goes through build, gate
    and merge like any other."""

    def zero_x_branch(self, slug="a-first", files=None):
        """sdlc/<slug> cut from main as it is now, with what 0.x left on it, then back to main."""
        self.repo.git("checkout", "-q", "-b", "sdlc/" + slug)
        for rel, text in (files or {"plan/%s.md" % slug: "---\ntype: plan\nstatus: approved\n---\n# Plan\n"}).items():
            self.repo.write(rel, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "plan %s" % slug)
        self.repo.git("checkout", "-q", "main")

    def planned(self):
        """The item as a design session leaves it on main: planned, with its Plan and Proof. (A `ready` item with such a branch
        starts again from main instead: `FreshStart` in test_engine_more.)"""
        self.repo.write("work/a-first.md", self.repo.doc_text("a-first", status="planned") +
                        "\n## Plan\n1. Write src/a-first.txt.\n\n## Proof\n| R | check | kind |\n|---|---|---|\n| R1 | test command | auto |\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "queue")

    def test_the_checkout_commits_the_default_branchs_doc_on_the_branch(self):
        self.zero_x_branch()
        self.planned()
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
        self.planned()
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build"])      # no conflict, no fix round
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertEqual(self.repo.show("main", "src/a-first.txt"), "built a-first\n")
        self.assertIn("## Plan", self.repo.show("main", "work/a-first.md"))
        self.assertIsNotNone(self.repo.show("main", "plan/a-first.md"))          # the 0.x plan came along with the branch
        self.assertNotIn("sdlc/a-first", self.repo.git("branch", "--list"))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_the_doc_is_taken_even_when_the_default_branch_moved_on_after_the_branch_was_cut(self):
        self.zero_x_branch()
        self.planned()
        self.repo.write("src/other.txt", "work that landed on main meanwhile\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "other work")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build"])
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
        self.planned()
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
        r.write("work/a-first.md", r.doc_text("a-first", status="planned"))      # committed on main and pushed
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "queue")
        r.git("push", "-q", "origin", "main")
        self.assertIsNone(r.origin_show("sdlc/a-first", "work/a-first.md"))
        r.engine().checkout_item("a-first")
        self.assertEqual(r.git("rev-parse", "--abbrev-ref", "HEAD"), "sdlc/a-first")
        self.assertEqual(r.read("work/a-first.md"), r.show("main", "work/a-first.md"))
        self.assertEqual(r.git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
