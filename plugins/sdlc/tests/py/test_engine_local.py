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
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_opus", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        builds = [c for c in self.repo.calls() if c["key"] == "a-first:build"]
        self.assertEqual([b["model"] for b in builds], ["opus", "sonnet"])
        self.assertFalse(builds[1]["resumed"])   # another model: the cache would not help
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


if __name__ == "__main__":
    unittest.main()
