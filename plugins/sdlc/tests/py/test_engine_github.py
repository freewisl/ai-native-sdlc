"""The loop against a GitHub-style remote (a bare repo + the stub gh): PRs, merges verified, team approvals."""
import json
import os
import unittest

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

    def test_team_mode_without_a_remote_is_refused(self):
        self.repo.git("remote", "remove", "origin")
        self.assertEqual(self.repo.engine().run(), 2)


if __name__ == "__main__":
    unittest.main()
