"""The loop against a GitHub-style remote (a bare repo + the stub gh): PRs, merges verified, team approvals."""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import RepoCase, SDLC
from sdlclib.enqueue import EnqueueError


def clone_status(case):
    """`status --json` in a fresh clone of origin: no engine state there, only what the repository itself says."""
    clone = os.path.join(case.repo.tmp, "clone")
    subprocess.run(["git", "clone", "-q", case.repo.origin, clone], check=True, env=case.repo.env(), capture_output=True)
    r = subprocess.run([sys.executable, SDLC, "--root", clone, "status", "--json", "--no-fetch"], capture_output=True, text=True,
                       env=case.repo.env())
    case.assertEqual(r.returncode, 0, r.stderr)
    return json.loads(r.stdout)


def drop_events(case, decision):
    with open(case.repo.path(".sdlc/logs/events.jsonl")) as fh:
        return [e for e in map(json.loads, fh) if e["event"] == "loop.item" and e["decision"] == decision]


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

    def clone_counts(self):
        return clone_status(self)["counts"]

    def test_drop_merges_a_queue_pr_closes_the_items_own_pr_and_a_fresh_clone_shows_it_dropped(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["new_dep"]})
        self.assertEqual(self.repo.engine().run(), 0)                            # waits for a person, its PR is open
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        r = self.repo.cli("drop", "a-first", "superseded", "by", "b")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a-first dropped — merged", r.stdout)
        prs = {p["head"]: p for p in self.repo.gh_db()["prs"]}
        self.assertEqual(prs["sdlc/a-first"]["state"], "CLOSED")
        queue = [p for p in prs.values() if p["head"].startswith("sdlc/queue-")]
        self.assertEqual([(p["title"], p["state"]) for p in queue], [("sdlc: drop a-first", "MERGED")])
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "dropped")
        self.assertIn("## Departures\nsuperseded by b\n", self.repo.origin_show("main", "work/a-first.md"))
        self.assertEqual(self.repo.status_of("a-first", "main"), "dropped")                  # the pull brought it here
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertEqual(self.clone_counts(), {"dropped": 1})

    def test_a_merge_of_the_queue_pr_that_does_not_happen_is_said_in_the_drops_words(self):
        self.repo.add_items("a-first")
        self.repo.base_env["STUB_GH_MERGE_FAIL"] = "1"                            # the CLI runs in a process of its own
        r = self.repo.cli("drop", "a-first")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("could not be merged", r.stdout)
        self.assertIn("merge it to record the drop", r.stdout)
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "ready")

    def test_a_merge_of_the_queue_pr_that_does_not_happen_is_said_in_the_proposals_words(self):
        p = self.repo.write("work/an-idea.md", self.repo.doc_text("an-idea", status="proposed"))
        self.repo.base_env["STUB_GH_MERGE_FAIL"] = "1"
        r = self.repo.cli("enqueue", p)
        self.assertIn("could not be merged", r.stdout + r.stderr)
        self.assertIn("merge it to record the proposals", r.stdout + r.stderr)
        self.assertNotIn("queue the items", r.stdout + r.stderr)

    def test_the_pr_body_says_when_the_design_allowed_existing_tests_to_change(self):
        self.repo.add_items(("a-first", {"test_changes": "allowed"}), "b-second")
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        prs = {p["head"]: p for p in self.repo.gh_db()["prs"]}
        self.assertTrue(prs["sdlc/a-first"]["body"].startswith(
            "> Existing tests were allowed to change in this item (test_changes: allowed).\n\n## Goal"), prs["sdlc/a-first"]["body"][:200])
        self.assertNotIn("Existing tests were allowed", prs["sdlc/b-second"]["body"])
        self.assertEqual([p["state"] for p in prs.values()], ["MERGED", "MERGED"])

    def test_a_pr_that_changes_a_guard_file_is_flagged_for_a_person_with_the_file_named(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["touch:.claude/settings.json"]})
        self.assertEqual(self.repo.engine().run(), 0)
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["state"], "OPEN")
        self.assertIn("guard files changed, a person merges these — .claude/settings.json (added)", pr["comments"][0])
        self.assertIn({"name": "sdlc:needs-human"}, pr["labels"])

    def test_a_pr_that_edits_an_existing_test_is_flagged_for_a_person_with_the_file_named(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["modify_test"]})
        self.assertEqual(self.repo.engine().run(), 0)
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["state"], "OPEN")
        self.assertIn("existing tests changed, but the design said test_changes: none — tests/test_base.py (modified)",
                      pr["comments"][0])


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

    def test_with_a_remote_the_line_names_the_pr_and_no_merge_command(self):
        self.waiting()
        entry = json.loads(self.repo.cli("status", "--json").stdout)["needs_you"][0]
        self.assertEqual((entry["slug"], entry["kind"]), ("a-first", "human"))
        self.assertTrue(entry["pr"])
        self.assertNotIn("accept", entry)
        self.assertNotIn("git merge", self.repo.cli("status").stdout)

    def test_a_merged_branch_is_left_to_github_to_remove(self):
        self.repo.add_items("a-first")
        self.repo.git("branch", "sdlc/a-first")                                            # at main's tip: merged by any measure
        self.repo.engine().drop_merged_branch("a-first")
        self.assertIn("sdlc/a-first", self.repo.git("branch", "--list"))

    def test_a_pr_a_person_merged_makes_the_item_done_and_releases_its_dependents(self):
        self.waiting()
        self.gh("pr", "merge", "1", "--squash", "--delete-branch")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "merged")
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")
        self.assertEqual(self.counts(), {"done": 2})
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:build"), 1)
        self.assertIn("sdlc/a-first", self.repo.git("branch", "--list"))                  # the branch of a PR is GitHub's to clean up

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


class SessionDrops(RepoCase):
    """A session that finds an item unnecessary leaves the record `drop` would: the doc on origin/main says dropped (solo:
    merged through a queue PR at once) and the item's own open PR is closed. The PR is there because the loop gave up on the
    item once (`max_failures_per_slug: 1` makes one failure enough) and a person then asked for another try."""
    remote = True
    config = {"loop": {"max_failures_per_slug": 1}}

    def given_up_then_retried(self, *more):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["gave_up", "drop:the behavior is already there"]})
        self.assertEqual(self.repo.engine().run(), 1)                            # gave up: a draft PR, flagged for a person
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        if more:
            self.repo.add_items(*more)
        self.assertEqual(self.repo.cli("retry", "a-first").returncode, 0)

    def queue_prs(self):
        return [p for p in self.repo.gh_db()["prs"] if p["head"].startswith("sdlc/queue-")]

    def test_the_drop_is_merged_to_origin_main_the_items_pr_is_closed_and_the_loop_goes_on(self):
        self.given_up_then_retried("b-second")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()],
                         ["a-first:design", "a-first:build", "a-first:build", "b-second:design", "b-second:build"])
        prs = {p["head"]: p for p in self.repo.gh_db()["prs"]}
        self.assertEqual(prs["sdlc/a-first"]["state"], "CLOSED")
        self.assertIn("sdlc: dropped — the behavior is already there", prs["sdlc/a-first"]["comments"][-1])
        self.assertEqual([(p["title"], p["state"]) for p in self.queue_prs()], [("sdlc: drop a-first", "MERGED")])
        self.assertEqual(prs["sdlc/b-second"]["state"], "MERGED")
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "dropped")
        self.assertIn("## Departures\nthe behavior is already there\n", self.repo.origin_show("main", "work/a-first.md"))
        self.assertEqual(self.repo.status_of("a-first", "main"), "dropped")                  # the loop's next pull brought it here
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("dropped", ["the behavior is already there"]))
        self.assertEqual(clone_status(self)["counts"], {"dropped": 1, "done": 1})
        with open(e.log_path) as fh:
            self.assertIn("DROPPED a-first — the behavior is already there (merged https://github.test/o/r/pull/", fh.read())
        (ev,) = drop_events(self, "dropped")
        self.assertEqual((ev["detail"]["slug"], ev["detail"]["merged"]), ("a-first", True))
        self.assertEqual(ev["detail"]["pr"], self.queue_prs()[0]["url"])

    def test_the_record_is_made_from_the_doc_as_it_is_on_origin_now_not_as_this_clone_last_saw_it(self):
        from sdlclib.config import Config
        from sdlclib.enqueue import record_drop
        from sdlclib.gitops import Git, GitHubForge
        self.repo.add_items("a-first")
        other = os.path.join(self.repo.tmp, "other")                             # someone edits the doc while the loop works
        subprocess.run(["git", "clone", "-q", self.repo.origin, other], check=True, env=self.repo.env(), capture_output=True)
        path = os.path.join(other, "work", "a-first.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("Do the thing.", "Do the newer thing."))
        for args in (["add", "-A"], ["commit", "-q", "-m", "reword the goal"], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", "-C", other] + args, check=True, env=self.repo.env(), capture_output=True)
        git = Git(self.repo.root)
        res = record_drop(Config(self.repo.root), git, GitHubForge(git, "main"), "a-first", "gone", from_loop=True)
        self.assertTrue(res["merged"], res)
        doc = self.repo.origin_show("main", "work/a-first.md")
        self.assertIn("status: dropped", doc)
        self.assertIn("Do the newer thing.", doc)                                # their edit was not written over with a stale copy

    def test_closing_acts_on_the_open_pr_of_the_item_only(self):
        from sdlclib.gitops import Git, GitHubForge, LocalForge

        def pr(n, state):
            return {"number": n, "head": "sdlc/a-first", "headRefName": "sdlc/a-first", "base": "main", "title": "t", "body": "",
                    "isDraft": False, "state": state, "url": "https://github.test/o/r/pull/%d" % n, "labels": [], "reviews": [],
                    "reviewDecision": "", "author": {"login": "me"}, "comments": []}
        self.repo.gh_db_write({"next": 3, "prs": [pr(1, "OPEN"), pr(2, "MERGED")], "calls": []})     # a branch name used twice
        git = Git(self.repo.root)
        forge = GitHubForge(git, "main")
        self.assertTrue(forge.close("a-first", "sdlc: dropped — gone"))
        prs = {p["number"]: p for p in self.repo.gh_db()["prs"]}
        self.assertEqual((prs[1]["state"], prs[2]["state"]), ("CLOSED", "MERGED"))
        self.assertEqual((prs[1]["comments"], prs[2]["comments"]), (["sdlc: dropped — gone"], []))
        self.assertFalse(forge.close("a-first", "again"))                                          # nothing open any more
        self.assertFalse(forge.close("no-such-item", "x"))
        prs = {p["number"]: p for p in self.repo.gh_db()["prs"]}
        self.assertEqual((prs[1]["comments"], prs[2]["state"], prs[2]["comments"]), (["sdlc: dropped — gone"], "MERGED", []))
        self.assertFalse(LocalForge(git, "main").close("a-first", "x"))                            # a local branch has no PR

    def test_a_drop_that_cannot_be_recorded_leaves_the_items_pr_open_and_is_announced(self):
        self.given_up_then_retried()
        e = self.repo.engine()
        with mock.patch("sdlclib.engine.record_drop", side_effect=EnqueueError("push failed: the network is down")):
            self.assertEqual(e.run(), 0)
        prs = self.repo.gh_db()["prs"]
        self.assertEqual([(p["head"], p["state"]) for p in prs], [("sdlc/a-first", "OPEN")])     # nothing says the drop is on main
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "ready")
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"][0]), ("dropped", "the behavior is already there"))
        self.assertEqual(rec["reasons"][1], "not recorded on main: push failed: the network is down")
        with open(e.log_path) as fh:
            self.assertIn("DROPPED a-first — the behavior is already there (NOT recorded on main: push failed: the network is down",
                          fh.read())
        (ev,) = drop_events(self, "drop-not-recorded")
        self.assertEqual(ev["detail"], {"slug": "a-first", "error": "push failed: the network is down"})
        self.assertEqual(drop_events(self, "dropped"), [])

    def test_a_drop_pr_that_does_not_merge_is_told_in_the_announcement_and_the_event(self):
        self.given_up_then_retried()
        os.environ["STUB_GH_MERGE_FAIL"] = "1"
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        queue = self.queue_prs()
        self.assertEqual([(p["title"], p["state"]) for p in queue], [("sdlc: drop a-first", "OPEN")])
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "CLOSED")
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "ready")
        with open(e.log_path) as fh:
            log = fh.read()
        self.assertIn("could not be merged", log)
        self.assertIn("merge it to record the drop", log)
        (ev,) = drop_events(self, "dropped")
        self.assertEqual((ev["detail"]["merged"], ev["detail"]["pr"]), (False, queue[0]["url"]))


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

    def test_a_queue_pr_of_ready_items_says_merging_it_puts_them_on_the_queue(self):
        p = self.repo.write("work/new-thing.md", self.repo.doc_text("new-thing"))
        r = self.repo.cli("enqueue", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the items join the queue when a person merges it", r.stdout)
        self.assertTrue(self.repo.gh_db()["prs"][0]["body"].endswith("\n\nMerging this PR puts these items on the sdlc queue."))

    def test_a_queue_pr_of_proposals_says_they_wait_to_be_accepted(self):
        p = self.repo.write("work/an-idea.md", self.repo.doc_text("an-idea", status="proposed"))
        r = self.repo.cli("enqueue", p)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the items are recorded as proposals when a person merges it, and join the queue once accepted", r.stdout)
        self.assertNotIn("items join the queue", r.stdout)
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["title"], "sdlc: propose an-idea")
        self.assertIn("- an-idea — an-idea (proposed)", pr["body"])
        self.assertTrue(pr["body"].endswith("\n\nMerging this PR records these items as proposals — they join the sdlc queue only "
                                            "when a person accepts them (`sdlc.py accept <slug>`)."), pr["body"])
        self.assertNotIn("puts these items on", pr["body"])

    def test_a_queue_pr_of_proposals_and_ready_items_keeps_the_queue_wording(self):
        a = self.repo.write("work/an-idea.md", self.repo.doc_text("an-idea", status="proposed"))
        b = self.repo.write("work/new-thing.md", self.repo.doc_text("new-thing"))
        r = self.repo.cli("enqueue", a, b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["title"], "sdlc: queue 2 work items")
        self.assertTrue(pr["body"].endswith("Merging this PR puts these items on the sdlc queue."), pr["body"])
        self.assertIn("the items join the queue when a person merges it", r.stdout)

    def test_accept_leaves_the_queue_pr_for_a_person_to_merge(self):
        self.repo.add_items(("a-idea", {"status": "proposed"}))
        r = self.repo.cli("accept", "a-idea")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("when a person merges it", r.stdout)
        self.assertEqual(self.repo.status_of("a-idea", "origin/main"), "proposed")
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual((pr["title"], pr["state"]), ("sdlc: accept a-idea", "OPEN"))
        self.assertEqual(self.repo.status_of("a-idea", "main"), "proposed")

    def test_drop_leaves_the_queue_pr_for_a_person_and_closes_the_design_pr(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)                            # the design PR, a draft, waits for approval
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        r = self.repo.cli("drop", "a-first", "superseded")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the items leave the queue when a person merges it", r.stdout)
        design, queue = self.repo.gh_db()["prs"]
        self.assertEqual(design["state"], "CLOSED")
        self.assertEqual((queue["title"], queue["state"]), ("sdlc: drop a-first", "OPEN"))
        self.assertIn("Merging this PR takes these items off the sdlc queue.", queue["body"])
        self.assertIn("- a-first — a-first (dropped: superseded)", queue["body"])
        self.assertNotIn("puts these items on", queue["body"])
        self.repo.git("fetch", "-q")
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "ready")           # until a person merges it
        self.human_merge(2)
        self.repo.git("fetch", "-q")
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "dropped")
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "dropped")

    def test_an_item_a_session_found_unnecessary_leaves_the_drop_pr_for_a_person_and_the_design_pr_is_closed(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.scenario({"a-first:build": ["drop:the behavior is already there"]})
        self.assertEqual(self.repo.engine().run(), 0)                            # both designs wait for approval as draft PRs
        self.approve(1)
        self.approve(2)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "b-second:design", "a-first:build", "b-second:build"])
        prs = {p["head"]: p for p in self.repo.gh_db()["prs"]}
        self.assertEqual(prs["sdlc/a-first"]["state"], "CLOSED")
        self.assertIn("sdlc: dropped — the behavior is already there", prs["sdlc/a-first"]["comments"][-1])
        (queue,) = [p for h, p in prs.items() if h.startswith("sdlc/queue-")]
        self.assertEqual((queue["title"], queue["state"]), ("sdlc: drop a-first", "OPEN"))
        self.assertIn("Merging this PR takes these items off the sdlc queue.", queue["body"])
        self.assertIn("- a-first — a-first (dropped: the behavior is already there)", queue["body"])
        self.assertEqual(prs["sdlc/b-second"]["state"], "OPEN")                  # the loop went on: built, a person merges it
        self.repo.git("fetch", "-q")
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "ready")           # until a person merges the drop
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "dropped")
        with open(e.log_path) as fh:
            self.assertIn("DROPPED a-first — the behavior is already there (PR %s — the items leave the queue when a person "
                          "merges it)" % queue["url"], fh.read())
        self.human_merge(queue["number"])
        self.repo.git("fetch", "-q")
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "dropped")
        self.assertIn("## Departures\nthe behavior is already there\n", self.repo.origin_show("main", "work/a-first.md"))
        phases = {i["slug"]: i["phase"] for i in clone_status(self)["items"]}
        self.assertEqual(phases["a-first"], "dropped")

    def test_the_self_check_never_runs_in_team_mode_it_needs_merges_the_loop_made_itself(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["loop"]["self_check"] = True
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "self check on")
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)                             # the design PR waits for approval
        self.approve(1)
        e = self.repo.engine()
        e.idle_max = 30
        orig = e.sleep
        calls = {"n": 0}

        def sleep(seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                self.human_merge(1)                                               # a person merges while the loop waits
            return orig(0.01)
        e.sleep = sleep
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "done")
        self.assertNotIn("self-check", [k for k, *_ in self.keys()])
        self.assertEqual(e.done["merged"], [])                                    # a person's merge is not counted as the loop's
        self.assertFalse(e.self_check_due())
        self.assertNotEqual(e.git.head(), e.from_sha)                             # main did move under the loop, and still no self-check

    def test_the_self_check_is_solo_only_whatever_else_says_it_is_due(self):
        e = self.repo.engine()
        e.cfg.data["loop"]["self_check"] = True
        e.done["merged"] = ["a-first"]                                            # whatever put a merge on record
        e.from_sha = "0" * 40                                                     # and main is not where this run began
        self.assertFalse(e.self_check_due())                                      # team mode: a person merges, nothing to review
        e.team = False
        self.assertTrue(e.self_check_due())                                       # the same state in solo mode is due

    def test_team_mode_without_a_remote_is_refused(self):
        self.repo.git("remote", "remove", "origin")
        self.assertEqual(self.repo.engine().run(), 2)


if __name__ == "__main__":
    unittest.main()
