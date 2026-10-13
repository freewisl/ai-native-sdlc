"""The loop against a GitHub-style remote (a bare repo + the stub gh): PRs, merges verified, team approvals."""
import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from helpers import RepoCase


class Solo(RepoCase):
    remote = True

    def test_items_are_merged_through_pull_requests(self):
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}))
        self.assertEqual(self.repo.engine().run(), 0)
        db = self.repo.gh_db()
        self.assertEqual([(p["head"], p["state"]) for p in db["prs"]], [("sdlc/a-first", "MERGED"), ("sdlc/b-second", "MERGED")])
        self.assertIn("## Goal", db["prs"][0]["body"])
        self.assertIn("SDLC_REVIEW_TALLY important=0", db["prs"][0]["body"])
        self.assertEqual(self.repo.git("rev-parse", "main"), self.repo.git("rev-parse", "origin/main"))
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "done")
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")
        self.assertIsNone(self.repo.origin_show("sdlc/a-first", "work/a-first.md"))   # branch deleted after the merge
        self.assertEqual(self.repo.items_state()["a-first"]["pr"], "https://github.test/o/r/pull/1")

    def test_a_merge_that_does_not_happen_waits_for_a_person(self):
        self.repo.add_items("a-first")
        os.environ["STUB_GH_MERGE_FAIL"] = "1"
        self.assertEqual(self.repo.engine().run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        self.assertIn("merge did not happen", rec["reasons"][0])
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["state"], "OPEN")
        self.assertIn({"name": "sdlc:needs-human"}, pr["labels"])
        self.assertTrue(pr["comments"])

    def test_human_gate_comments_and_labels_the_pr(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["new_dep"]})
        self.repo.engine().run()
        pr = self.repo.gh_db()["prs"][0]
        self.assertFalse(pr["isDraft"])
        self.assertIn("left-pad", pr["comments"][0])
        self.assertIn({"name": "sdlc:needs-human"}, pr["labels"])

    def test_enqueue_merges_a_queue_pr(self):
        p = self.repo.write("work/new-thing.md", self.repo.doc_text("new-thing"))
        r = self.repo.cli("enqueue", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("merged", r.stdout)
        self.assertEqual(self.repo.status_of("new-thing", "origin/main"), "ready")
        # the untracked copy made way for the pull, which brought the committed doc back
        self.assertEqual(self.repo.git("ls-files", "work/new-thing.md"), "work/new-thing.md")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertEqual(self.repo.status_of("new-thing", "main"), "ready")

    def test_accept_merges_a_queue_pr_that_sets_the_proposal_ready(self):
        self.repo.add_items(("a-idea", {"status": "proposed"}))
        r = self.repo.cli("accept", "a-idea")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("accepted 1 item(s): a-idea — merged", r.stdout)
        self.assertEqual(self.repo.status_of("a-idea", "origin/main"), "ready")
        self.assertEqual(self.repo.status_of("a-idea", "main"), "ready")                     # the pull brought it here
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual((pr["title"], pr["state"]), ("sdlc: accept a-idea", "MERGED"))
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/new/a-idea.md")))
        self.assertEqual(self.repo.engine().run(), 0)                                        # and the loop takes it from there
        self.assertEqual(self.repo.status_of("a-idea", "origin/main"), "done")


class WhatAPersonDecides(RepoCase):
    """On GitHub the loop looks at the PR of an item that waits for a person: merged → done, closed → closed."""
    remote = True

    def gh(self, *args):
        import subprocess
        r = subprocess.run([os.path.join(os.path.dirname(__file__), "stubs", "gh")] + list(args), cwd=self.repo.root,
                           env=self.repo.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def counts(self):
        return json.loads(self.repo.cli("status", "--json").stdout)["counts"]

    def waiting(self):
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}))
        self.repo.scenario({"a-first:build": ["new_dep"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.assertEqual(self.counts(), {"needs-human": 1, "design": 1})

    def test_a_pr_a_person_merged_makes_the_item_done_and_releases_its_dependents(self):
        self.waiting()
        self.gh("pr", "merge", "1", "--squash", "--delete-branch")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "merged")
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")
        self.assertEqual(self.counts(), {"done": 2})
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:build"), 1)

    def test_a_pr_a_person_merged_is_done_with_no_engine_state(self):
        """The squash left the built doc on origin/main: that alone says a person merged it."""
        self.waiting()
        self.gh("pr", "merge", "1", "--squash", "--delete-branch")
        os.remove(self.repo.path(".sdlc/state/items.json"))
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual({i["slug"]: i["phase"] for i in s["items"]}, {"a-first": "done", "b-second": "design"})
        self.assertEqual(s["needs_you"], [])
        self.assertEqual(s["next"], ["b-second"])                                         # the dependent is runnable
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        with open(e.log_path) as fh:
            self.assertNotIn("ITEM    a-first", fh.read())                                # not gated, built or merged again
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:build"), 1)
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")
        self.assertEqual(self.counts(), {"done": 2})
        self.assertEqual(len(self.repo.gh_db()["prs"]), 2)                                # no second PR for a-first

    def test_a_pr_a_person_closed_closes_the_item_and_its_dependents_wait_for_a_person(self):
        self.waiting()
        self.gh("pr", "close", "1")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "closed")
        self.assertEqual(self.counts(), {"closed": 1, "design": 1})
        needs = json.loads(self.repo.cli("status", "--json").stdout)["needs_you"]
        self.assertEqual([(n["slug"], n["kind"]) for n in needs], [("b-second", "dependency")])
        self.assertIn("which was closed", needs[0]["text"][0])

    def test_an_open_pr_keeps_the_item_waiting_and_costs_no_second_pr(self):
        self.waiting()
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.assertEqual(len(self.repo.gh_db()["prs"]), 1)


class GivenUpOn(RepoCase):
    """An item the loop gave up on (failed too often) has a draft PR; a person may take it over the same way."""
    remote = True
    config = {"loop": {"max_failures_per_slug": 1}}

    def gh(self, *args):
        import subprocess
        r = subprocess.run([os.path.join(os.path.dirname(__file__), "stubs", "gh")] + list(args), cwd=self.repo.root,
                           env=self.repo.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def counts(self):
        return json.loads(self.repo.cli("status", "--json").stdout)["counts"]

    def test_a_pr_a_person_merged_makes_a_blocked_item_done(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["gave_up"]})
        self.assertEqual(self.repo.engine().run(), 1)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["failures"], 1)
        self.assertTrue(rec["pr"])
        self.assertEqual(self.counts(), {"blocked": 1})
        self.assertTrue(self.repo.gh_db()["prs"][0]["isDraft"])
        self.gh("pr", "ready", "1")
        self.gh("pr", "merge", "1", "--squash", "--delete-branch")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "merged")
        self.assertEqual(self.counts(), {"done": 1})

    def test_a_blocked_item_whose_pr_is_still_open_stays_blocked(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["gave_up"]})
        self.repo.engine().run()
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertNotIn("state", self.repo.items_state()["a-first"])
        self.assertEqual(self.counts(), {"blocked": 1})


class StatusFetches(RepoCase):
    """`sdlc.py status` refreshes the remote refs first, so what a person merged on GitHub shows at once. It never asks
    GitHub, says nothing when the remote cannot be reached, gives up on a hanging one, and `--no-fetch` keeps it offline."""
    remote = True
    stale = {"needs-human": 1, "design": 1}                    # what status shows before it has heard of the merge

    def gh(self, *args):
        import subprocess
        r = subprocess.run([os.path.join(os.path.dirname(__file__), "stubs", "gh")] + list(args), cwd=self.repo.root,
                           env=self.repo.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def view(self, *extra):
        r = self.repo.cli("status", "--json", *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def merged_by_a_person(self):
        """a-first waits for a person (PR open); the person merges it in the browser — this clone has not heard of it."""
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}))
        self.repo.scenario({"a-first:build": ["new_dep"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        self.gh("pr", "merge", "1", "--squash", "--delete-branch")
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "ready")

    def test_a_merge_made_on_the_remote_shows_up_without_a_fetch(self):
        self.merged_by_a_person()
        calls = len(self.repo.gh_db()["calls"])
        s = self.view()
        self.assertEqual({i["slug"]: i["phase"] for i in s["items"]}, {"a-first": "done", "b-second": "design"})
        self.assertEqual((s["needs_you"], s["next"]), ([], ["b-second"]))
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "built")               # status did the fetch
        self.assertIn("1 done", self.repo.cli("status").stdout)
        self.assertEqual(len(self.repo.gh_db()["calls"]), calls)                              # and asked GitHub nothing

    def test_no_fetch_shows_what_this_clone_has(self):
        self.merged_by_a_person()
        before = self.repo.git("rev-parse", "origin/main")
        s = self.view("--no-fetch")
        self.assertEqual(s["counts"], self.stale)
        self.assertEqual([n["slug"] for n in s["needs_you"]], ["a-first", "b-second"])
        self.assertEqual(self.repo.git("rev-parse", "origin/main"), before)                   # nothing was fetched
        self.assertEqual(self.view()["counts"], {"done": 1, "design": 1})                     # the next plain status is fresh

    def test_an_unreachable_remote_is_ignored_quickly_and_silently(self):
        self.merged_by_a_person()
        self.repo.git("remote", "set-url", "origin", os.path.join(self.repo.tmp, "gone.git"))
        began = time.time()
        r = self.repo.cli("status", "--json")
        took = time.time() - began
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(json.loads(r.stdout)["counts"], self.stale)                          # what it has
        self.assertLess(took, 8)

    def test_a_remote_that_hangs_is_given_up_on_and_nothing_of_the_fetch_is_left_running(self):
        """The ssh of a dead connection: status waits STATUS_FETCH_SECONDS, shows what it has, and kills the whole fetch."""
        import sdlc
        self.merged_by_a_person()
        bindir = os.path.join(self.repo.tmp, "bin")
        os.makedirs(bindir)
        pids = os.path.join(self.repo.tmp, "ssh.pids")
        with open(os.path.join(bindir, "ssh"), "w") as fh:                                    # a connection that never answers
            fh.write('#!/bin/sh\necho $$ >> "%s"\nsleep 20\n' % pids)
        os.chmod(os.path.join(bindir, "ssh"), 0o755)
        self.repo.git("remote", "set-url", "origin", "ssh://example.invalid/o/r.git")
        os.environ["GIT_SSH_COMMAND"] = os.path.join(bindir, "ssh")
        out = io.StringIO()
        with mock.patch.object(sdlc, "STATUS_FETCH_SECONDS", 2.0), contextlib.redirect_stdout(out):
            began = time.time()
            code = sdlc.main(["--root", self.repo.root, "status", "--json"])
            took = time.time() - began
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["counts"], self.stale)
        self.assertGreaterEqual(took, 2.0)
        self.assertLess(took, 8)                                                              # not the 20 s the connection hangs
        with open(pids) as fh:
            started = [int(n) for n in fh.read().split()]
        self.assertTrue(started)                                                              # git did try to connect

        def alive(pid):
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            return True
        deadline = time.time() + 3
        while time.time() < deadline and any(alive(n) for n in started):
            time.sleep(0.05)
        self.assertEqual([n for n in started if alive(n)], [])


class Team(RepoCase):
    remote = True
    config = {"mode": "team"}

    def approve(self, number, how="review"):
        db = self.repo.gh_db()
        for pr in db["prs"]:
            if pr["number"] == number:
                if how == "review":
                    pr["reviews"].append({"author": {"login": "lead"}, "state": "APPROVED"})
                else:
                    pr["labels"].append({"name": "sdlc:approved"})
        self.repo.gh_db_write(db)

    def human_merge(self, number):
        import subprocess
        import sys
        r = subprocess.run([os.path.join(os.path.dirname(__file__), "stubs", "gh"), "pr", "merge", str(number), "--squash",
                            "--delete-branch"], cwd=self.repo.root, env=self.repo.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_design_waits_for_approval_then_build_waits_for_a_human_merge(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        db = self.repo.gh_db()
        self.assertEqual(len(db["prs"]), 1)
        self.assertTrue(db["prs"][0]["isDraft"])
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design"])
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual([n["kind"] for n in s["needs_you"]], ["approve-design"])

        self.approve(1)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        pr = self.repo.gh_db()["prs"][0]
        self.assertFalse(pr["isDraft"])
        self.assertEqual(pr["state"], "OPEN")                 # a person merges in team mode
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "awaiting-merge")

        self.human_merge(1)
        self.repo.git("pull", "-q", "--ff-only")
        s = json.loads(self.repo.cli("status", "--json").stdout)
        self.assertEqual(s["counts"], {"done": 1})

    def test_the_loop_notices_an_approval_while_it_polls(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        e.idle_max = 30
        orig = e.sleep
        calls = {"n": 0}

        def sleep(seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                self.approve(1, how="label")
            if calls["n"] > 3:
                return orig(0)
            return orig(0.01)
        e.sleep = sleep
        e.max_items = 2
        self.assertEqual(e.run(), 0)
        self.assertIn("a-first:build", [k for k, *_ in self.keys()])

    def test_enqueue_leaves_the_queue_pr_for_a_person(self):
        p = self.repo.write("work/new-thing.md", self.repo.doc_text("new-thing"))
        r = self.repo.cli("enqueue", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("when a person merges it", r.stdout)
        self.assertIsNone(self.repo.status_of("new-thing", "origin/main"))
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")

    def test_accept_leaves_the_queue_pr_for_a_person_to_merge(self):
        self.repo.add_items(("a-idea", {"status": "proposed"}))
        r = self.repo.cli("accept", "a-idea")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("when a person merges it", r.stdout)
        self.assertEqual(self.repo.status_of("a-idea", "origin/main"), "proposed")
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual((pr["title"], pr["state"]), ("sdlc: accept a-idea", "OPEN"))
        self.assertEqual(self.repo.status_of("a-idea", "main"), "proposed")

    def test_team_mode_without_a_remote_is_refused(self):
        self.repo.git("remote", "remove", "origin")
        self.assertEqual(self.repo.engine().run(), 2)


if __name__ == "__main__":
    unittest.main()
