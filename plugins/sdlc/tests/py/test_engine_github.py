"""The loop against a GitHub-style remote (a bare repo + the stub gh): PRs, merges verified, team approvals."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helpers import RepoCase, SDLC, STUBS
from sdlclib import state as st
from sdlclib.engine import CI_BRIEF_CAP, Outcome, StopRun, check_names
from sdlclib.enqueue import EnqueueError
from sdlclib.events import read_events
from test_checks import check_run, failed_job_log, run_link, status_context


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


class SelfCheck(RepoCase):
    """The self-check queues what it found through a PR the loop merges itself, and that merge is not pulled while the loop
    runs here. What the loop does next, and why a run stops, still comes from the default branch as the remote has it."""
    remote = True
    config = {"loop": {"self_check": True, "self_check_min_items": 1}}      # one merged item is enough for these tests

    def run_engine(self):
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        with open(e.log_path) as fh:
            return fh.read()

    def remote_head(self):
        return self.repo.git("ls-remote", "origin", "refs/heads/main").split()[0]

    def test_findings_that_wait_for_a_person_are_counted_when_the_run_stops_and_main_is_where_origin_is(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "old-debt", "status": "proposed"},
                                               {"slug": "more-debt", "status": "proposed"}]})
        log = self.run_engine()
        self.assertIn("stop=nothing runnable — 2 wait for you (more-debt, old-debt)", log)
        self.assertNotIn("stop=queue empty", log)
        self.assertEqual(self.repo.git("rev-parse", "main"), self.remote_head())
        self.assertEqual(self.repo.git("rev-parse", "origin/main"), self.remote_head())
        for slug in ("old-debt", "more-debt"):
            self.assertEqual(self.repo.status_of(slug), "proposed")
            self.assertEqual(self.repo.status_of(slug, "origin/main"), "proposed")
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertEqual([k for k, *_ in self.keys()].count("self-check"), 1)      # reading the queue again is not a second check

    def test_findings_that_are_partly_ready_still_build_the_ready_one_in_the_same_run(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "fix-one", "status": "ready"}, {"slug": "old-debt", "status": "proposed"}]})
        log = self.run_engine()
        self.assertEqual(self.repo.status_of("fix-one"), "done")
        self.assertEqual(self.repo.status_of("old-debt"), "proposed")
        self.assertEqual([(p["head"], p["state"]) for p in self.repo.gh_db()["prs"] if p["head"] == "sdlc/fix-one"],
                         [("sdlc/fix-one", "MERGED")])
        self.assertIn("stop=nothing runnable — 1 wait for you (old-debt)", log)
        self.assertEqual(self.repo.git("rev-parse", "main"), self.remote_head())

    def test_a_check_that_found_nothing_changes_nothing(self):
        self.repo.add_items("a-first")
        log = self.run_engine()
        self.assertIn("CHECK   clean", log)
        self.assertIn("stop=queue empty", log)
        self.assertEqual(self.repo.git("rev-parse", "main"), self.remote_head())


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

    def test_a_loop_running_here_fetches_for_itself_and_status_does_not_fetch_beside_it(self):
        """A second fetch takes the locks the loop's checkout and pull need: while a loop runs in this checkout `status` shows
        what the clone has, as with --no-fetch."""
        import sdlc
        self.merged_by_a_person()
        before = self.repo.git("rev-parse", "origin/main")
        st.write_pid(self.repo.root)                                                          # this process is the loop: it is alive
        try:
            self.assertEqual(self.view()["counts"], self.stale)
            self.assertEqual(self.repo.git("rev-parse", "origin/main"), before)               # nothing was fetched
            with mock.patch("sdlclib.gitops.Git.refresh") as refresh, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sdlc.main(["--root", self.repo.root, "status", "--json"]), 0)
            refresh.assert_not_called()
        finally:
            st.clear_pid(self.repo.root)
        self.assertEqual(self.view()["counts"], {"done": 1, "design": 1})                     # the loop is gone: fetched again

    def test_status_fetches_when_no_loop_runs_and_when_the_pid_file_is_a_dead_loops(self):
        import sdlc
        self.merged_by_a_person()
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        for pid_file in (None, "%d\n" % gone.pid):
            if pid_file is not None:
                with open(self.repo.path(st.PID_REL), "w") as fh:
                    fh.write(pid_file)
            with mock.patch("sdlclib.gitops.Git.refresh") as refresh, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sdlc.main(["--root", self.repo.root, "status", "--json"]), 0)
            refresh.assert_called_once_with(sdlc.STATUS_FETCH_SECONDS)

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
        e.merged_since_check = ["a-first", "b-second"]                            # whatever put two merges on record
        e.from_sha = "0" * 40                                                     # and main is not where this run began
        self.assertFalse(e.self_check_due())                                      # team mode: a person merges, nothing to review
        e.team = False
        self.assertTrue(e.self_check_due())                                       # the same state in solo mode is due

    def test_the_engine_does_not_wait_for_the_pr_checks_in_team_mode(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)                             # the design PR waits for approval
        self.approve(1)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "awaiting-merge")   # a person merges: nothing to wait for
        calls = [" ".join(c) for c in self.repo.gh_db()["calls"]]
        self.assertEqual([c for c in calls if c.startswith(("pr view", "run view")) or "statusCheckRollup" in c], [])
        with open(e.log_path) as fh:
            self.assertNotIn("CHECKS", fh.read())

    def test_team_mode_without_a_remote_is_refused(self):
        self.repo.git("remote", "remove", "origin")
        self.assertEqual(self.repo.engine().run(), 2)


class ChecksBase(RepoCase):
    """What the tests of the PR-checks wait share. The stub gh plays CI (see its docstring); `fast` is an engine that polls at
    once and does not wait for checks to show up unless the test says so."""
    remote = True

    def script(self, slug, *snapshots):
        """The snapshots of checks the stub plays for sdlc/<slug>, one per look; the last one stays."""
        db = self.repo.gh_db()
        db.setdefault("checks", {})["sdlc/" + slug] = list(snapshots)
        self.repo.gh_db_write(db)

    def db_set(self, **keys):
        db = self.repo.gh_db()
        db.update(keys)
        self.repo.gh_db_write(db)

    def fast(self, limit=None, **kw):
        e = self.repo.engine(**kw)
        e.checks_poll, e.checks_grace = 0.05, 0.0
        if limit is not None:
            e.cfg.data["loop"]["checks_wait_minutes"] = limit
        return e

    def log(self, e):
        try:
            with open(e.log_path) as fh:
                return fh.read()
        except OSError:
            return ""

    def gh_calls(self):
        return [" ".join(c) for c in self.repo.gh_db().get("calls", [])]

    def looks(self):
        """How often the PR's checks were read."""
        return len([c for c in self.gh_calls() if c.startswith("pr view") and "statusCheckRollup" in c])

    def person(self, *args):
        """What somebody does on GitHub meanwhile: the stub, run the way the command line would."""
        r = subprocess.run([os.path.join(STUBS, "gh")] + list(args), cwd=self.repo.root, env=self.repo.env(), capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def watch_looks(self, e, then=None):
        """Every look the engine takes at the checks, as (head, time it ended, time it began). `then` = {n: action} runs the
        action right after the n-th look — a person acting while the engine waits."""
        looks, orig = [], e.forge.checks

        def checks(slug, head=""):
            began = time.time()
            out = orig(slug, head)
            looks.append((head, time.time(), began))
            if len(looks) in (then or {}):
                then[len(looks)]()
            return out
        e.forge.checks = checks
        return looks

    def events(self, name):
        return list(read_events(self.repo.root, name))


class PrChecks(ChecksBase):
    """Solo on GitHub: after the shell gate the loop waits for the PR's checks before it merges."""
    config = {"loop": {"checks_wait_minutes": 0.3}}

    def watch_fix(self, e):
        """What the world looks like when the engine starts a fix session — recorded, then the session runs as usual."""
        seen, orig = [], e.session

        def session(slug, phase, extra="", fix_round=0):
            if phase == "fix":
                seen.append({"status": e.doc_status(slug), "subject": self.repo.git("log", "-1", "--format=%s"),
                             "ci_rounds": self.repo.items_state()[slug].get("ci_rounds"), "extra": extra, "round": fix_round,
                             "looks": self.looks()})
            return orig(slug, phase, extra, fix_round)
        e.session = session
        return seen

    def fix_prompts(self, slug="a-first"):
        return [c["prompt"] for c in self.repo.calls() if c["key"] == slug + ":fix"]

    def advance_main(self, path="other.txt", text="newer\n"):
        """origin's main moves on while the item is being shipped: a commit pushed from a clone of its own, as a colleague would."""
        clone, env = tempfile.mkdtemp(prefix="colleague-", dir=self.repo.tmp), self.repo.env()

        def git(*args):
            subprocess.run(["git", "-C", clone] + list(args), check=True, env=env, capture_output=True)
        subprocess.run(["git", "clone", "-q", self.repo.origin, clone], check=True, env=env, capture_output=True)
        os.makedirs(os.path.dirname(os.path.join(clone, path)), exist_ok=True)
        with open(os.path.join(clone, path), "w") as fh:
            fh.write(text)
        git("add", "-A")
        git("commit", "-q", "-m", "main moves on")
        git("push", "-q", "origin", "main")

    def built_branch(self, slug="a-first"):
        """What an interactive /sdlc:go leaves on sdlc/<slug>: a built work doc with its review tally, committed."""
        r = self.repo
        r.add_items(slug)
        r.git("checkout", "-q", "-b", "sdlc/" + slug)
        r.write("work/%s.md" % slug, r.doc_text(slug, status="built") +
                "\n## Plan\n1. Write src/%s.txt.\n\n## Proof\n| R | check | kind |\n|---|---|---|\n| R1 | test command | auto |\n"
                "\n## Review\nSDLC_REVIEW_TALLY important=0 nits=1 validated=yes\n" % slug)
        r.write("src/%s.txt" % slug, "built\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "build " + slug)

    # ---------- waiting, then merging ----------
    def test_pending_checks_are_waited_for_and_the_item_is_merged_when_they_pass(self):
        self.repo.add_items("a-first")
        running = [check_run("build", "IN_PROGRESS", ""), check_run("test", "QUEUED", "")]
        self.script("a-first", running, running, [check_run("build"), check_run("test")])
        e = self.fast()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        log = self.log(e)
        self.assertEqual(log.count("CHECKS  a-first — waiting for build, test\n"), 1)                  # said once, at the first look
        self.assertIn("CHECKS  a-first — passed: build, test\n", log)
        self.assertLess(log.index("waiting for build, test"), log.index("passed: build, test"))
        self.assertLess(log.index("passed: build, test"), log.index("MERGED  a-first"))
        self.assertEqual(self.looks(), 3)
        (ev,) = self.events("loop.checks")
        self.assertEqual((ev["decision"], ev["reason"]), ("pass", "build, test"))
        self.assertEqual([c["name"] for c in ev["detail"]["checks"]], ["build", "test"])
        self.assertEqual(ev["detail"]["slug"], "a-first")
        self.assertGreaterEqual(ev["detail"]["waited_s"], 0)

    def test_a_pr_with_no_checks_and_no_workflow_files_is_merged_at_once(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        e.checks_poll = 0.05                                    # the grace stays what it is by default: none without workflows
        self.assertEqual(e.run(), 0)
        log = self.log(e)
        self.assertIn("CHECKS  a-first — no checks\n", log)
        self.assertNotIn("waiting for", log)
        self.assertEqual(self.looks(), 1)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        (ev,) = self.events("loop.checks")
        self.assertEqual((ev["decision"], ev["detail"]["checks"]), ("none", []))

    def test_a_pr_with_no_checks_yet_waits_the_grace_when_the_repository_has_workflows_and_then_merges(self):
        self.repo.write(".github/workflows/ci.yml", "name: CI\non: pull_request\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "ci")
        self.repo.git("push", "-q", "origin", "main")
        self.repo.add_items("a-first")
        e = self.repo.engine()
        e.checks_poll, e.checks_grace = 0.05, 0.6
        looks = self.watch_looks(e)
        self.assertEqual(e.run(), 0)
        self.assertGreaterEqual(looks[-1][1] - looks[0][2], 0.6 - 0.02)                                # nothing showed up for the whole grace
        self.assertGreater(len(looks), 1)
        log = self.log(e)
        self.assertEqual(log.count("CHECKS  a-first — waiting for checks to start\n"), 1)
        self.assertLess(log.index("waiting for checks to start"), log.index("CHECKS  a-first — no checks"))
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")

    def test_checks_that_show_up_during_the_grace_are_waited_for_like_any_other(self):
        self.repo.add_items("a-first")
        self.script("a-first", [], [check_run("build", "IN_PROGRESS", "")], [check_run("build")])
        e = self.repo.engine()
        e.checks_poll, e.checks_grace = 0.05, 30.0
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.looks(), 3)                                                                # the grace did not make it wait on
        log = self.log(e)
        self.assertEqual(log.count("waiting for"), 1)
        self.assertIn("CHECKS  a-first — waiting for checks to start\n", log)
        self.assertIn("CHECKS  a-first — passed: build\n", log)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")

    # ---------- a failed check ----------
    def test_a_failed_check_sends_the_item_through_a_fix_session_and_the_gate_again_before_it_merges(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE", workflow="CI")], [check_run("build")])
        self.db_set(run_logs={"100": "build\tTests\t2026-10-14T10:00:02Z FAILED tests/test_pay.py::test_total - AssertionError: 3 != 4\n"
                                     "build\tTests\t2026-10-14T10:00:03Z ##[error]Process completed with exit code 1.\n"})
        e = self.fast(max_items=1)                                             # the whole cycle is one visit to the item
        seen = self.watch_fix(e)
        self.assertEqual(e.run(), 0)
        self.assertEqual(len(seen), 1)
        s = seen[0]
        self.assertEqual((s["status"], s["subject"]), ("built", "sdlc(a-first): PR checks failed — back to built"))
        self.assertEqual((s["ci_rounds"], s["round"]), (None, 1))                                      # counted when the session is over, not before
        brief = s["extra"]
        self.assertTrue(brief.startswith("The PR's checks failed on GitHub after the shell gate had passed (they run on GitHub, not here):\n"))
        self.assertIn("- `build` (workflow CI) — " + run_link(100) + "\n", brief)
        self.assertIn("test_total - AssertionError: 3 != 4", brief)
        self.assertIn("Process completed with exit code 1.", brief)
        for word in ("SDLC_FIXED", "SDLC_FIX_BLOCKED", ".github/workflows/", "never weaken, skip or delete a test"):
            self.assertIn(word, brief)
        (prompt,) = self.fix_prompts()
        self.assertTrue(prompt.startswith("/sdlc:go --item a-first --fix"))
        self.assertIn(brief, prompt)                                                                   # the session is told exactly this
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build", "a-first:fix"])
        self.assertEqual([g["decision"] for g in self.events("loop.gate")], ["pass", "pass"])          # the gate ran again after the fix
        self.assertEqual([c["decision"] for c in self.events("loop.checks")], ["fail", "pass"])
        log = self.log(e)
        self.assertIn("CHECKS  a-first — failed: build\n", log)
        self.assertEqual(log.count("GATE    a-first"), 2)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertNotIn("ci_rounds", self.repo.items_state()["a-first"])                              # a merge clears the count
        self.assertIn("\nstatus: done\n", self.repo.origin_show("main", "work/a-first.md"))
        self.assertIsNotNone(self.repo.origin_show("main", "FIXED-a-first"))                          # the fix is in what was merged

    def test_the_fix_session_is_told_the_end_of_the_failed_job_without_its_cleanup_or_the_prefix_of_each_line(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE", workflow="CI")], [check_run("build")])
        raw, kept, cleanup = failed_job_log("build")                                                   # gh prints the whole job, every line prefixed
        self.db_set(run_logs={"100": raw})
        self.assertEqual(self.fast(max_items=1).run(), 0)
        (prompt,) = self.fix_prompts()
        shown = prompt.split("The end of what they printed:\n```\n", 1)[1].split("\n```\n", 1)[0]
        self.assertEqual(shown, "== build ==\n" + "\n".join(kept))
        self.assertTrue(shown.endswith("##[error]Process completed with exit code 1."), shown[-200:])
        for line in cleanup:
            self.assertNotIn(line, shown)
        self.assertNotIn("\t", shown)
        self.assertNotRegex(shown, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertNotIn("\ufeff", shown)

    def test_a_run_still_in_progress_when_the_fix_brief_is_made_is_waited_for_with_the_loops_sleep(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE", workflow="CI")], [check_run("build")])
        raw, kept, _ = failed_job_log("build")
        self.db_set(run_logs={"100": raw}, run_in_progress={"100": 2})
        e = self.fast(max_items=1)
        naps, loops_sleep = [], e.sleep

        def sleep(seconds):
            if seconds == 10:                                                  # the wait for the log: counted, not slept
                naps.append(seconds)
                return True
            return loops_sleep(seconds)
        e.sleep = sleep
        self.assertEqual(e.run(), 0)
        self.assertEqual(naps, [10, 10])
        self.assertEqual(len([c for c in self.gh_calls() if c.startswith("run view")]), 3)               # refused twice, then read
        (prompt,) = self.fix_prompts()
        self.assertIn("\n".join(kept[-4:]), prompt)                                                      # the log is in the brief after all
        self.assertNotIn("(no log)", prompt)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")

    def stopped_while_the_log_is_waited_for(self, stop):
        """The loop's first run on an item whose PR fails in a run that is not over yet: gh refuses the log once, so the fix
        brief waits ten seconds — and `stop(engine)` happens during that wait. Checked here is what holds for either way of
        stopping: no fix session, no round used, the doc back at `built` and committed, the PR still open. Returns the engine."""
        self.repo.add_items("a-first")
        failure = [check_run("build", conclusion="FAILURE", workflow="CI")]
        self.script("a-first", failure, failure, [check_run("build")])        # each run's look sees it fail; the look after the fix, not
        self.db_set(run_logs={"100": failed_job_log("build")[0]}, run_in_progress={"100": 1})
        e = self.fast(max_items=1)
        loops_sleep = e.sleep

        def sleep(seconds):
            if seconds == 10:                                                  # the wait for the log: somebody stops the loop meanwhile
                stop(e)
            return loops_sleep(seconds)                                        # the real one, which sees it and answers False at once
        e.sleep = sleep
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])            # no fix session
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec.get("ci_rounds", 0), 0)                                                   # and no round used
        self.assertNotIn("state", rec)                                                                 # a stop: nobody has to look, nothing failed
        self.assertNotIn("failures", rec)
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "built")                      # back to built …
        self.assertEqual(self.repo.git("log", "-1", "--format=%s", "sdlc/a-first"),                    # … and committed
                         "sdlc(a-first): PR checks failed — back to built")
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        self.assertEqual(len([c for c in self.gh_calls() if c.startswith("run view")]), 1)             # asked once, never after the stop
        return e

    def test_a_pause_while_the_fix_brief_waits_for_a_log_starts_no_fix_session_and_the_next_run_goes_on_to_fix_it(self):
        e = self.stopped_while_the_log_is_waited_for(lambda e: self.repo.write(".sdlc/state/pause", ""))
        self.assertIn("stop=paused", self.log(e))
        self.assertEqual([c["decision"] for c in self.events("loop.checks")], ["fail"])
        os.remove(self.repo.path(".sdlc/state/pause"))                                                  # the person lets it go on
        e2 = self.fast(max_items=1)
        seen = self.watch_fix(e2)
        self.assertEqual(e2.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build", "a-first:fix"])
        (s,) = seen
        self.assertEqual((s["ci_rounds"], s["round"]), (None, 1))                                      # the first fix round, not the second
        self.assertIn("\n".join(failed_job_log("build")[1][-4:]), self.fix_prompts()[0])               # with the log that was not there before
        self.assertEqual([g["decision"] for g in self.events("loop.gate")], ["pass", "pass", "pass"])  # before the stop, after it, after the fix
        self.assertEqual([c["decision"] for c in self.events("loop.checks")], ["fail", "fail", "pass"])
        self.assertEqual(len(self.repo.gh_db()["prs"]), 1)                                              # the same PR all along
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertNotIn("ci_rounds", self.repo.items_state()["a-first"])                              # a merge clears the count

    def test_a_stop_request_while_the_fix_brief_waits_for_a_log_starts_no_fix_session_either(self):
        e = self.stopped_while_the_log_is_waited_for(lambda e: setattr(e, "stopping", "stop requested"))
        self.assertIn("stop=stop requested", self.log(e))

    def test_checks_that_keep_failing_end_with_a_person_after_the_fix_rounds_and_the_pr_is_flagged(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE"), check_run("lint")])
        e = self.fast()
        seen = self.watch_fix(e)
        self.assertEqual(e.run(), 0)
        self.assertEqual([s["round"] for s in seen], [1, 2])                                           # loop.fix_rounds is 2
        reason = "the PR's checks still fail after 2 fix round(s): build"
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"], rec["ci_rounds"]), ("needs-human", [reason], 2))
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["state"], "OPEN")
        self.assertFalse(pr["isDraft"])
        self.assertIn({"name": "sdlc:needs-human"}, pr["labels"])
        self.assertIn(reason, pr["comments"][0])
        self.assertIn("HUMAN   a-first — " + reason + "\n", self.log(e))
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "built")                      # not done: it is not
        self.assertEqual(self.repo.git("log", "-1", "--format=%s", "origin/sdlc/a-first"),             # and what the person gets is pushed
                         "sdlc(a-first): PR checks failed — back to built")

    def test_with_no_fix_rounds_a_failed_check_goes_straight_to_a_person(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE"), check_run("test", conclusion="TIMED_OUT")])
        e = self.fast()
        e.fix_rounds = 0
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("needs-human", ["the PR's checks failed: build, test"]))
        self.assertNotIn("ci_rounds", rec)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])            # no fix session
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "built")

    def test_a_fix_session_that_says_it_cannot_fix_it_hands_the_item_to_a_person(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:fix": ["fix_blocked:the deploy secret is missing in CI"]})
        self.script("a-first", [check_run("deploy", conclusion="FAILURE")])
        self.assertEqual(self.fast().run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("needs-human", ["the fix session could not fix it: the deploy secret is missing in CI"]))
        self.assertEqual(rec["ci_rounds"], 1)
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:fix"), 1)                         # and no second round

    def test_a_failure_waits_for_the_checks_still_running_before_the_fix_session_starts(self):
        self.repo.add_items("a-first")
        bad = check_run("build", conclusion="FAILURE")
        self.script("a-first", [bad, check_run("test", "IN_PROGRESS", "")], [bad, check_run("test", "IN_PROGRESS", "")],
                    [bad, check_run("test")], [check_run("build"), check_run("test")])
        e = self.fast()
        seen = self.watch_fix(e)
        self.assertEqual(e.run(), 0)
        self.assertEqual([s["looks"] for s in seen], [3])                    # not at the first look, which already had the failure
        self.assertIn("- `build`", seen[0]["extra"])
        self.assertNotIn("`test`", seen[0]["extra"])                          # what passed is nothing to fix
        log = self.log(e)
        self.assertEqual(log.count("CHECKS  a-first — waiting for test\n"), 1)
        self.assertIn("CHECKS  a-first — failed: build\n", log)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")

    def test_checks_read_before_the_pushed_commit_shows_up_are_not_taken_for_its_own(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE")], [check_run("build")])
        self.db_set(head_lag={"sdlc/a-first": 2})                             # after the fix is pushed GitHub shows the old commit twice
        e = self.fast()
        seen = self.watch_fix(e)
        looks = self.watch_looks(e)
        self.assertEqual(e.run(), 0)
        self.assertEqual(len(seen), 1)                                        # the old failure was not read again: no second fix round
        heads = [h for h, *_ in looks]
        self.assertEqual(len(heads), 4)                                       # the first push, then the new one three times (2 held back)
        self.assertNotEqual(heads[0], heads[1])
        self.assertEqual(heads[1], heads[2])
        self.assertEqual(heads[2], heads[3])
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")

    # ---------- the limit ----------
    def test_checks_still_running_at_the_limit_hand_the_item_to_a_person_and_the_loop_notices_the_merge(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", "IN_PROGRESS", ""), check_run("test"), check_run("lint", "QUEUED", "")])
        e = self.fast(limit=0.03)
        self.assertEqual(e.run(), 0)
        reason = ("the PR's checks were still running after 0.03 min (build, lint) — merge the PR when they pass (the loop notices), "
                  "or raise loop.checks_wait_minutes")
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("needs-human", [reason]))
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["state"], "OPEN")
        self.assertIn({"name": "sdlc:needs-human"}, pr["labels"])
        self.assertIn(reason, pr["comments"][0])
        log = self.log(e)
        self.assertIn("CHECKS  a-first — waiting for build, lint\n", log)
        self.assertNotIn("passed:", log)
        self.assertNotIn("failed:", log)                                       # the HUMAN line says it
        self.assertIn("HUMAN   a-first — " + reason + "\n", log)
        (ev,) = self.events("loop.checks")
        self.assertEqual((ev["decision"], ev["reason"]), ("pending", "build, lint"))
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "done")                       # the gate had passed: the PR is shippable
        self.person("pr", "merge", "1", "--squash", "--delete-branch")                                  # the checks pass and a person merges
        e2 = self.repo.engine()
        self.assertEqual(e2.run(), 0)
        self.assertIn("MERGED  a-first — merged by a person\n", self.log(e2))
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "merged")
        self.assertEqual(len(self.repo.gh_db()["prs"]), 1)

    # ---------- a branch that is behind ----------
    def test_a_branch_that_is_behind_gets_the_default_branch_merged_in_and_its_checks_waited_for_again(self):
        self.repo.add_items("a-first")
        self.db_set(behind=["sdlc/a-first"])
        e = self.fast()
        snap = {}

        def after_the_update():
            snap["subject"] = self.repo.git("log", "-1", "--format=%s")
            snap["parents"] = len(self.repo.git("rev-list", "--parents", "-n", "1", "HEAD").split()) - 1
            snap["pushed"] = self.repo.git("rev-parse", "origin/sdlc/a-first") == self.repo.git("rev-parse", "HEAD")
        looks = self.watch_looks(e, {1: self.advance_main, 2: after_the_update})
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertEqual(snap, {"subject": "sdlc(a-first): merge main", "parents": 2, "pushed": True})
        heads = [h for h, *_ in looks]
        self.assertEqual(len(heads), 2)
        self.assertNotEqual(heads[0], heads[1])                                # the second wait is for the commit that was pushed
        self.assertEqual(self.log(e).count("UPDATE  a-first — behind main; merged it in, waiting for the checks again\n"), 1)
        self.assertEqual(self.repo.origin_show("main", "other.txt"), "newer\n")
        self.assertEqual(self.repo.origin_show("main", "src/a-first.txt"), "built a-first\n")
        self.assertEqual(self.repo.items_state()["a-first"].get("state"), None)

    def test_a_behind_branch_whose_update_fails_its_checks_goes_through_the_fix_session_like_any_other(self):
        self.repo.add_items("a-first")
        self.db_set(behind=["sdlc/a-first"])
        self.script("a-first", [check_run("build")], [check_run("build", conclusion="FAILURE")], [check_run("build")])
        e = self.fast()
        seen = self.watch_fix(e)
        self.watch_looks(e, {1: self.advance_main})
        self.assertEqual(e.run(), 0)
        self.assertEqual(len(seen), 1)                                         # the update's own checks failed: a fix round, as before
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")

    def test_a_branch_that_is_behind_and_conflicts_with_the_default_branch_is_left_to_a_person(self):
        self.repo.add_items("a-first")
        self.db_set(behind=["sdlc/a-first"])
        e = self.fast()
        looks = self.watch_looks(e, {1: lambda: self.advance_main("src/a-first.txt", "main wrote this one too\n")})
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("needs-human", ["the branch is behind main and merging it conflicts"]))
        self.assertEqual(len(looks), 1)                                        # no second wait
        self.assertNotIn("UPDATE", self.log(e))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")           # the merge was undone
        self.assertFalse(os.path.exists(self.repo.path(".git/MERGE_HEAD")))
        self.assertEqual(self.repo.git("rev-parse", "sdlc/a-first"), self.repo.git("rev-parse", "origin/sdlc/a-first"))   # nothing was added
        self.assertEqual(self.repo.show("sdlc/a-first", "src/a-first.txt"), "built a-first\n")                         # no markers committed
        pr = self.repo.gh_db()["prs"][0]
        self.assertEqual(pr["state"], "OPEN")
        self.assertIn({"name": "sdlc:needs-human"}, pr["labels"])
        self.assertIn("the branch is behind main and merging it conflicts", pr["comments"][0])

    def test_a_merge_refused_again_after_the_update_is_told_as_it_is_and_the_update_is_made_once(self):
        os.environ["STUB_GH_MERGE_FAIL"] = "1"
        self.repo.add_items("a-first")
        self.db_set(behind=["sdlc/a-first"])
        e = self.fast()
        looks = self.watch_looks(e, {1: self.advance_main})
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        self.assertEqual(rec["reasons"], ["merge did not happen: gh pr merge: GraphQL: Pull request is not mergeable"])
        self.assertEqual(self.log(e).count("UPDATE  a-first"), 1)
        self.assertEqual(len(looks), 2)

    def test_a_merge_that_fails_for_another_reason_is_not_an_update(self):
        os.environ["STUB_GH_MERGE_FAIL"] = "1"
        self.repo.add_items("a-first")
        e = self.fast()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"], ["merge did not happen: gh pr merge: GraphQL: Pull request is not mergeable"])
        self.assertNotIn("UPDATE", self.log(e))
        self.assertEqual(self.looks(), 1)

    # ---------- no waiting ----------
    def test_a_wait_of_zero_minutes_merges_without_asking_for_the_checks(self):
        self.repo.add_items("a-first")
        e = self.fast(limit=0)
        self.assertEqual(e.run(), 0)
        self.assertEqual([c for c in self.gh_calls() if c.startswith(("pr view", "run view"))], [])
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertNotIn("CHECKS", self.log(e))
        self.assertEqual(self.events("loop.checks"), [])

    def test_the_gate_is_all_there_is_for_a_repository_without_a_remote(self):
        # the forge is local there: nothing to wait for, merged here (the engine tests of the local forge go on to show the rest)
        self.repo.git("remote", "remove", "origin")
        self.repo.add_items("a-first", push=False)
        e = self.fast()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("a-first", "main"), "done")
        self.assertEqual(self.repo.gh_db().get("calls", []), [])
        self.assertNotIn("CHECKS", self.log(e))

    # ---------- stop, pause, and what a person does meanwhile ----------
    def test_a_stop_request_while_waiting_leaves_the_item_done_and_the_next_run_ships_it(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        e = self.fast()
        looks = self.watch_looks(e, {2: lambda: setattr(e, "stopping", "stop requested")})
        self.assertEqual(e.run(), 0)
        self.assertEqual(len(looks), 2)
        self.assertIn("stop=stop requested", self.log(e))
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "done")
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        rec = self.repo.items_state()["a-first"]
        self.assertNotIn("state", rec)                                         # not waiting for a person, not merged
        self.assertNotIn("merged", rec)
        self.assertEqual(self.events("loop.checks"), [])                       # a wait that was cut short ended with no verdict
        self.script("a-first", [check_run("build")])                           # the checks have passed by the next run
        e2 = self.fast()
        self.assertEqual(e2.run(), 0)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])            # shipped again, nothing redone

    def test_a_pause_while_waiting_stops_the_run_the_same_way(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        e = self.fast()
        self.watch_looks(e, {1: lambda: self.repo.write(".sdlc/state/pause", "")})
        self.assertEqual(e.run(), 0)
        self.assertIn("stop=paused", self.log(e))
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "done")
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")

    def test_a_pr_a_person_merges_while_the_loop_waits_is_theirs_not_the_loops(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        e = self.fast()
        self.watch_looks(e, {2: lambda: self.person("pr", "merge", "1", "--squash", "--delete-branch")})
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "merged")
        self.assertIn("merged", rec)
        self.assertNotIn("reasons", rec)
        self.assertNotIn("ci_rounds", rec)
        log = self.log(e)
        self.assertIn("MERGED  a-first — merged by a person\n", log)
        self.assertNotIn("passed:", log)
        self.assertEqual((e.merged_since_check, e.done["merged"]), ([], []))                           # not a merge the loop made
        self.assertEqual([x["decision"] for x in self.events("loop.item") if x["decision"] == "merged-by-person"], ["merged-by-person"])
        self.assertEqual([x["decision"] for x in self.events("loop.checks")], ["merged"])
        self.assertEqual(self.events("loop.merge"), [])
        self.assertEqual(len(self.repo.gh_db()["prs"]), 1)
        e2 = self.repo.engine()
        self.assertEqual(e2.run(), 0)
        self.assertNotIn("ITEM    a-first", self.log(e2))                                              # it is done: the loop leaves it

    def test_a_pr_a_person_closes_while_the_loop_waits_closes_the_item(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        e = self.fast()
        self.watch_looks(e, {2: lambda: self.person("pr", "close", "1")})
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "closed")
        self.assertIn("CLOSED  a-first — PR closed by a person\n", self.log(e))
        self.assertEqual(e.merged_since_check, [])
        self.assertEqual([x["decision"] for x in self.events("loop.item") if x["decision"] == "closed-by-person"], ["closed-by-person"])
        self.assertEqual([x["decision"] for x in self.events("loop.checks")], ["closed"])

    # ---------- an interactive ship ----------
    def test_an_interactive_ship_hands_back_the_failed_checks_and_ships_after_the_fix(self):
        self.built_branch()
        self.script("a-first", [check_run("build", conclusion="FAILURE")])
        self.db_set(run_logs={"100": "build\tTests\t2026-10-14T10:00:02Z FAILED tests/test_pay.py::test_total - AssertionError: 3 != 4\n"})
        r = self.repo.cli("ship", "a-first")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("The PR's checks failed on GitHub after the shell gate had passed", r.stdout)
        self.assertIn("- `build` (workflow CI) — " + run_link(100), r.stdout)
        self.assertIn("test_total - AssertionError: 3 != 4", r.stdout)
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "built")                      # back to built, and committed
        self.assertEqual(self.repo.git("log", "-1", "--format=%s"), "sdlc(a-first): PR checks failed — back to built")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        rec = self.repo.items_state().get("a-first", {})
        self.assertNotIn("state", rec)                                         # nobody is waiting for a person
        self.assertNotIn("ci_rounds", rec)                                     # and the loop's rounds are not the caller's
        self.assertEqual(self.repo.calls(), [])                                # the caller fixes: no session of the engine's
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/loop-status.json")))                # an interactive ship is no loop
        self.repo.write("src/fix.txt", "fixed\n")                              # the caller fixes, commits, ships again
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "fix the failing check")
        self.script("a-first", [check_run("build")])
        r = self.repo.cli("ship", "a-first")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CHECKS  a-first — passed: build\n", r.stdout)                                   # what the engine said on the way
        self.assertEqual(r.stdout.strip().splitlines()[-1], "merged — https://github.test/o/r/pull/1")
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertIn("\nstatus: done\n", self.repo.origin_show("main", "work/a-first.md"))
        self.assertEqual(self.repo.origin_show("main", "src/fix.txt"), "fixed\n")

    def test_an_interactive_ship_waits_for_a_log_still_in_progress_with_a_plain_sleep(self):
        self.built_branch()
        self.script("a-first", [check_run("build", conclusion="FAILURE")])
        raw, kept, _ = failed_job_log("build")
        self.db_set(run_logs={"100": raw}, run_in_progress={"100": 1})
        e = self.fast()
        e.stopping = "stop requested"                                          # only a loop has one
        with mock.patch.object(e, "sleep", side_effect=AssertionError("the loop's sleep")):
            with mock.patch("sdlclib.gitops.time") as clock:                   # gitops' own clock: no real ten seconds
                code, brief = e.ship_now("a-first")
        self.assertEqual(code, 1)
        clock.sleep.assert_called_once_with(10)
        self.assertIn("\n".join(kept[-4:]), brief)
        self.assertNotIn("(no log)", brief)

    def test_an_interactive_ship_that_runs_out_of_time_waiting_exits_2_with_the_reason(self):
        self.built_branch()
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        e = self.fast(limit=0.03)
        code, msg = e.ship_now("a-first")
        self.assertEqual(code, 2)
        self.assertEqual(msg, "the PR's checks were still running after 0.03 min (build) — merge the PR when they pass "
                              "(the loop notices), or raise loop.checks_wait_minutes")
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")

    def test_an_interactive_ship_exits_0_when_a_person_merged_the_pr_while_it_waited(self):
        self.built_branch()
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        e = self.fast()
        self.watch_looks(e, {2: lambda: self.person("pr", "merge", "1", "--squash", "--delete-branch")})
        self.assertEqual(e.ship_now("a-first"), (0, "merged by a person — https://github.test/o/r/pull/1"))
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "merged")

    def test_an_interactive_ship_exits_2_when_a_person_closed_the_pr_while_it_waited(self):
        self.built_branch()
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        e = self.fast()
        self.watch_looks(e, {2: lambda: self.person("pr", "close", "1")})
        self.assertEqual(e.ship_now("a-first"), (2, "the PR was closed by a person"))
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "closed")

    def test_retry_clears_the_fix_rounds_the_checks_used_up(self):
        st.update(self.repo.root, "a-first", ci_rounds=2, failures=1, state="needs-human", reasons=["x"], last_error="y", stop={"sid": "s"})
        r = self.repo.cli("retry", "a-first")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual([k for k in rec if k != "updated"], [])


class WaitChecks(ChecksBase):
    """`wait_checks` on its own: sdlc/a-first is checked out and pushed, its PR is open on the stub."""

    def setUp(self):
        super().setUp()
        r = self.repo
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        r.write("feature.txt", "feature\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "feature")
        self.e = self.fast()
        ok, url = self.e.forge.publish("a-first", "A title", "A body")
        self.assertTrue(ok, url)

    def wait(self, limit=0.3, interactive=False):
        self.e.cfg.data["loop"]["checks_wait_minutes"] = limit
        return self.e.wait_checks("a-first", interactive=interactive)

    def test_everything_passing_at_the_first_look_is_a_pass_with_no_waiting(self):
        self.script("a-first", [check_run("build"), check_run("test")])
        state, found = self.wait()
        self.assertEqual((state, [c["name"] for c in found]), ("pass", ["build", "test"]))
        log = self.log(self.e)
        self.assertIn("CHECKS  a-first — passed: build, test\n", log)
        self.assertNotIn("waiting", log)
        self.assertEqual(self.looks(), 1)

    def test_the_names_are_cut_at_six(self):
        many = [check_run("c%d" % i, "IN_PROGRESS", "") for i in range(8)]
        self.script("a-first", many, [check_run("c%d" % i) for i in range(8)])
        self.wait()
        log = self.log(self.e)
        self.assertIn("CHECKS  a-first — waiting for c0, c1, c2, c3, c4, c5, …\n", log)
        self.assertIn("CHECKS  a-first — passed: c0, c1, c2, c3, c4, c5, …\n", log)

    def test_a_failure_at_the_first_look_with_nothing_pending_returns_at_once(self):
        self.script("a-first", [check_run("build", conclusion="FAILURE"), check_run("lint")])
        state, found = self.wait()
        self.assertEqual((state, [(c["name"], c["bucket"]) for c in found]), ("fail", [("build", "fail"), ("lint", "pass")]))
        log = self.log(self.e)
        self.assertIn("CHECKS  a-first — failed: build\n", log)
        self.assertNotIn("waiting", log)
        self.assertEqual(self.looks(), 1)
        (ev,) = self.events("loop.checks")
        self.assertEqual((ev["decision"], ev["reason"]), ("fail", "build"))

    def test_a_failure_with_a_check_still_running_waits_for_it_and_then_returns_every_entry(self):
        bad = check_run("build", conclusion="FAILURE")
        self.script("a-first", [bad, check_run("test", "IN_PROGRESS", "")], [bad, check_run("test", "IN_PROGRESS", "")],
                    [bad, check_run("test", conclusion="CANCELLED")])
        state, found = self.wait()
        self.assertEqual((state, [(c["name"], c["bucket"]) for c in found]), ("fail", [("build", "fail"), ("test", "fail")]))
        self.assertEqual(self.looks(), 3)
        log = self.log(self.e)
        self.assertEqual(log.count("waiting for test\n"), 1)
        self.assertIn("CHECKS  a-first — failed: build, test\n", log)

    def test_a_failure_with_a_check_still_running_at_the_limit_returns_the_failure(self):
        self.script("a-first", [check_run("build", conclusion="FAILURE"), check_run("test", "IN_PROGRESS", "")])
        state, found = self.wait(limit=0.02)
        self.assertEqual((state, [c["bucket"] for c in found]), ("fail", ["fail", "pending"]))
        self.assertIn("CHECKS  a-first — failed: build\n", self.log(self.e))
        self.assertEqual([x["decision"] for x in self.events("loop.checks")], ["fail"])

    def test_pending_at_the_limit_returns_pending_with_the_entries_and_no_verdict_line(self):
        self.script("a-first", [check_run("build", "IN_PROGRESS", ""), check_run("lint")])
        state, found = self.wait(limit=0.02)
        self.assertEqual((state, [c["bucket"] for c in found]), ("pending", ["pending", "pass"]))
        self.assertEqual([l for l in self.log(self.e).splitlines() if "CHECKS" in l and "waiting for" not in l], [])
        (ev,) = self.events("loop.checks")
        self.assertEqual((ev["decision"], ev["reason"]), ("pending", "build"))

    def test_no_checks_without_workflow_files_is_a_pass_after_one_look(self):
        self.e.checks_grace = None
        self.assertEqual(self.wait(), ("pass", []))
        self.assertEqual(self.looks(), 1)
        self.assertIn("CHECKS  a-first — no checks\n", self.log(self.e))
        (ev,) = self.events("loop.checks")
        self.assertEqual(ev["decision"], "none")

    def test_no_checks_is_a_pass_only_once_the_grace_is_over(self):
        self.e.checks_grace = 0.6
        looks = self.watch_looks(self.e)
        self.assertEqual(self.wait(), ("pass", []))
        self.assertGreaterEqual(looks[-1][1] - looks[0][2], 0.6 - 0.02)
        self.assertGreater(len(looks), 1)
        log = self.log(self.e)
        self.assertEqual(log.count("CHECKS  a-first — waiting for checks to start\n"), 1)
        self.assertIn("CHECKS  a-first — no checks\n", log)

    def test_a_grace_longer_than_the_wait_is_cut_to_the_wait(self):
        self.e.checks_grace = 100
        t = time.time()
        self.assertEqual(self.wait(limit=0.02), ("pass", []))
        self.assertLess(time.time() - t, 10)

    def test_checks_that_start_during_the_grace_are_waited_for(self):
        self.e.checks_grace = 30
        self.script("a-first", [], [], [check_run("build", "IN_PROGRESS", "")], [check_run("build")])
        state, found = self.wait()
        self.assertEqual((state, [c["name"] for c in found]), ("pass", ["build"]))
        self.assertEqual(self.looks(), 4)

    def test_what_cannot_be_read_is_waited_out_not_taken_for_a_pass(self):
        self.script("a-first", {"error": "HTTP 502"}, {"error": "HTTP 502"}, [check_run("build")])
        state, found = self.wait()
        self.assertEqual((state, [c["name"] for c in found]), ("pass", ["build"]))
        self.assertEqual(self.looks(), 3)
        self.assertEqual(self.log(self.e).count("CHECKS  a-first — waiting for checks to start\n"), 1)

    def test_what_cannot_be_read_until_the_limit_is_unknown_with_nothing_to_show(self):
        self.script("a-first", {"error": "HTTP 502"})
        self.assertEqual(self.wait(limit=0.02), ("unknown", []))
        (ev,) = self.events("loop.checks")
        self.assertEqual((ev["decision"], ev["reason"], ev["detail"]["checks"]), ("unknown", "", []))

    def test_a_pr_that_shows_another_commit_is_waited_out_too(self):
        self.script("a-first", [check_run("build", conclusion="FAILURE")], [check_run("build")])
        self.wait()                                                            # look at the commit as pushed: the failure
        self.script("a-first", [check_run("build", conclusion="FAILURE")], [check_run("build")])
        self.db_set(head_lag={"sdlc/a-first": 2})
        self.repo.write("feature.txt", "feature, again\n")
        self.repo.git("commit", "-q", "-am", "fix")
        self.repo.git("push", "-q", "origin", "sdlc/a-first")
        looks = self.watch_looks(self.e)
        state, found = self.wait()
        self.assertEqual(state, "fail")                                        # (the script starts over: the failure, now for the new head)
        self.assertEqual(len(looks), 3)                                        # two views of the old commit were held back first

    def test_a_pr_a_person_merged_or_closed_ends_the_wait(self):
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        self.watch_looks(self.e, {2: lambda: self.person("pr", "merge", "1", "--squash", "--delete-branch")})
        self.assertEqual(self.wait(), ("merged", []))
        self.assertEqual([x["decision"] for x in self.events("loop.checks")], ["merged"])
        self.assertNotIn("passed", self.log(self.e))

    def test_a_pr_closed_by_a_person_ends_the_wait(self):
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        self.watch_looks(self.e, {1: lambda: self.person("pr", "close", "1")})
        self.assertEqual(self.wait(), ("closed", []))
        self.assertEqual([x["decision"] for x in self.events("loop.checks")], ["closed"])

    def test_no_wait_to_do_is_a_pass_and_not_one_gh_call(self):
        calls = len(self.repo.gh_db()["calls"])
        self.assertEqual(self.wait(limit=0), ("pass", []))
        self.assertEqual(len(self.repo.gh_db()["calls"]), calls)
        self.assertEqual(self.events("loop.checks"), [])

    def test_the_loop_says_it_is_in_the_checks_phase_and_an_interactive_ship_does_not_say_anything(self):
        self.script("a-first", [check_run("build")])
        self.wait()
        status = st.read_status(self.repo.root)
        self.assertEqual(status["phase"], "checks")
        self.assertTrue(status["phase_started"])
        os.remove(self.repo.path(".sdlc/state/loop-status.json"))
        self.wait(interactive=True)
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/loop-status.json")))

    def test_a_stop_request_ends_the_wait_with_its_reason_and_a_pause_with_paused(self):
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        self.watch_looks(self.e, {1: lambda: setattr(self.e, "stopping", "stop requested")})
        with self.assertRaises(StopRun) as cm:
            self.wait()
        self.assertEqual((cm.exception.reason, cm.exception.code), ("stop requested", 0))
        self.e.stopping = ""
        self.repo.write(".sdlc/state/pause", "")
        with self.assertRaises(StopRun) as cm:
            self.wait()
        self.assertEqual(cm.exception.reason, "paused")

    def test_an_interactive_wait_sleeps_plainly_and_is_not_stopped_by_the_loops_flags(self):
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")], [check_run("build")])
        self.e.stopping = "stop requested"                                     # only a loop has one
        self.e.checks_poll = 0.0517                                            # no other sleep in the process has this length
        with mock.patch.object(self.e, "sleep", side_effect=AssertionError("the loop's sleep")):
            with mock.patch("sdlclib.engine.time.sleep", wraps=time.sleep) as plain:
                self.assertEqual(self.wait(interactive=True)[0], "pass")
        self.assertEqual([c[0][0] for c in plain.call_args_list if c[0][0] == 0.0517], [0.0517])

    def test_ship_names_the_checks_that_were_still_running_when_it_gives_the_pr_to_a_person(self):
        self.script("a-first", [check_run("build", "IN_PROGRESS", ""), check_run("test"), check_run("lint", "QUEUED", "")])
        self.e.cfg.data["loop"]["checks_wait_minutes"] = 0.02
        self.assertEqual(self.e.ship("a-first"), Outcome.HUMAN)
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"],
                         ["the PR's checks were still running after 0.02 min (build, lint) — merge the PR when they pass "
                          "(the loop notices), or raise loop.checks_wait_minutes"])

    def test_ship_leaves_the_names_out_when_no_check_had_shown_up_by_the_last_look_it_could_read(self):
        self.script("a-first", [], {"error": "HTTP 502"})                     # no check yet, then gh fails until the limit
        self.e.checks_grace = 60.0
        self.e.cfg.data["loop"]["checks_wait_minutes"] = 0.02
        self.assertEqual(self.e.ship("a-first"), Outcome.HUMAN)
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"],
                         ["the PR's checks were still running after 0.02 min — merge the PR when they pass "
                          "(the loop notices), or raise loop.checks_wait_minutes"])

    def test_checks_that_could_never_be_read_are_not_called_running(self):
        self.script("a-first", {"error": "HTTP 502"})
        self.e.cfg.data["loop"]["checks_wait_minutes"] = 0.02
        self.assertEqual(self.wait(limit=0.02), ("unknown", []))
        self.assertEqual(self.e.ship("a-first"), Outcome.HUMAN)
        self.assertEqual(self.repo.items_state()["a-first"]["reasons"],
                         ["the PR's checks could not be read for 0.02 min (`gh pr view` failed, or the PR never showed the "
                          "pushed commit) — look at the PR, and merge it when its checks pass (the loop notices)"])

    def test_a_look_that_fails_at_the_end_keeps_what_the_last_good_look_saw(self):
        self.script("a-first", [check_run("build", "IN_PROGRESS", ""), check_run("lint")], {"error": "HTTP 502"})
        state, found = self.wait(limit=0.02)
        self.assertEqual((state, [(c["name"], c["bucket"]) for c in found]), ("pending", [("build", "pending"), ("lint", "pass")]))

    def test_the_limit_is_said_in_minutes_the_way_a_person_wrote_it(self):
        pending = [{"name": "build", "bucket": "pending", "link": "", "workflow": ""}]
        self.e.wait_checks = lambda slug, interactive=False: ("pending", pending)         # the wait itself is not what is tested
        for minutes, said in ((30, "30"), (0.5, "0.5"), (45.0, "45")):
            self.e.cfg.data["loop"]["checks_wait_minutes"] = minutes
            self.e.ship("a-first")
            self.assertIn("after %s min (build) — " % said, self.repo.items_state()["a-first"]["reasons"][0])

    def test_it_never_sleeps_past_the_limit(self):
        self.e.checks_poll = 5.0                                               # a poll interval far longer than the wait
        self.script("a-first", [check_run("build", "IN_PROGRESS", "")])
        wakes, orig = [], self.e.sleep

        def sleep(seconds):
            wakes.append(seconds)
            return orig(seconds)
        self.e.sleep = sleep
        t = time.time()
        self.assertEqual(self.wait(limit=0.01)[0], "pending")                   # 0.6 s
        self.assertLess(time.time() - t, 4)
        self.assertLessEqual(wakes[0], 0.6)
        self.assertLessEqual(sum(wakes), 0.7)


class Timing(RepoCase):
    """How long the engine waits for checks and how often it looks."""

    def test_the_interval_is_a_tenth_of_the_wait_kept_between_one_and_thirty_seconds(self):
        e = self.repo.engine()
        self.assertEqual([e.checks_timing(s)[0] for s in (2, 5, 60, 300, 1800, 7200)], [1.0, 1.0, 6.0, 30.0, 30.0, 30.0])

    def test_the_interval_can_be_set(self):
        e = self.repo.engine()
        e.checks_poll = 0.1
        self.assertEqual(e.checks_timing(1800)[0], 0.1)

    def test_the_grace_is_two_minutes_with_workflow_files_and_none_without(self):
        e = self.repo.engine()
        self.assertEqual(e.checks_timing(1800)[1], 0.0)
        self.repo.write(".github/workflows/notes.md", "not a workflow\n")
        self.assertFalse(e.has_workflows())
        self.repo.write(".github/workflows/ci.yaml", "name: CI\n")
        self.assertTrue(e.has_workflows())
        self.assertEqual(e.checks_timing(1800)[1], 120.0)
        os.remove(self.repo.path(".github/workflows/ci.yaml"))
        self.repo.write(".github/workflows/ci.yml", "name: CI\n")
        self.assertEqual(e.checks_timing(1800)[1], 120.0)

    def test_a_directory_named_like_a_workflow_is_none(self):
        os.makedirs(self.repo.path(".github/workflows/odd.yml"))
        self.assertFalse(self.repo.engine().has_workflows())

    def test_the_grace_is_never_longer_than_the_wait_and_can_be_set(self):
        e = self.repo.engine()
        self.repo.write(".github/workflows/ci.yml", "name: CI\n")
        self.assertEqual(e.checks_timing(60)[1], 60.0)
        e.checks_grace = 5
        self.assertEqual(e.checks_timing(1800)[1], 5.0)
        self.assertEqual(e.checks_timing(2)[1], 2.0)
        e.checks_grace = 0
        self.assertEqual(e.checks_timing(1800)[1], 0.0)

    def test_a_config_file_that_says_zero_turns_the_wait_off(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["loop"]["checks_wait_minutes"] = 0
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.assertEqual(self.repo.engine().checks_limit(), 0.0)

    def test_the_wait_is_read_from_the_config_as_minutes(self):
        e = self.repo.engine()
        self.assertEqual(e.checks_limit(), 1800.0)                             # the default is 30 minutes
        for value, seconds in ((0.5, 30.0), ("0.5", 30.0), (0, 0.0), (-3, 0.0), ("soon", 1800.0), (None, 1800.0)):
            e.cfg.data["loop"]["checks_wait_minutes"] = value
            self.assertEqual(e.checks_limit(), seconds, repr(value))


class BriefWaits(ChecksBase):
    """The wait for a log that is not there yet (its run still in progress), inside the brief a fix session gets."""
    FAILED = [{"name": "build", "bucket": "fail", "link": run_link(100), "workflow": "CI"}]

    def setUp(self):
        super().setUp()
        self.db_set(run_in_progress={"100": 1}, run_logs={"100": "boom"})
        self.e = self.fast()

    def run_views(self):
        return [c for c in self.gh_calls() if c.startswith("run view")]

    def test_the_loop_waits_with_its_own_sleep_and_the_log_is_in_the_brief(self):
        naps = []
        self.e.sleep = lambda seconds: naps.append(seconds) or True
        brief = self.e.ci_brief(self.FAILED)
        self.assertEqual(naps, [10])
        self.assertIn("\n```\n== build ==\nboom\n```\n", brief)
        self.assertEqual(len(self.run_views()), 2)

    def test_a_stop_request_while_the_log_is_waited_for_stops_the_run_and_nothing_is_asked_after_it(self):
        self.e.stopping = "stop requested"                                     # the loop's sleep answers False at once
        with self.assertRaises(StopRun) as cm:
            self.e.ci_brief(self.FAILED)
        self.assertEqual((cm.exception.reason, cm.exception.code), ("stop requested", 0))
        self.assertEqual(len(self.run_views()), 1)                              # asked once and refused, never again

    def test_a_pause_while_the_log_is_waited_for_stops_the_run_the_same_way(self):
        self.repo.write(".sdlc/state/pause", "")
        with self.assertRaises(StopRun) as cm:
            self.e.ci_brief(self.FAILED)
        self.assertEqual((cm.exception.reason, cm.exception.code), ("paused", 0))
        self.assertEqual(len(self.run_views()), 1)

    def test_a_stop_request_is_not_looked_at_when_no_log_has_to_be_waited_for(self):
        self.db_set(run_in_progress={})                                         # the log is there at the first ask
        self.e.stopping = "stop requested"
        self.assertIn("\n```\n== build ==\nboom\n```\n", self.e.ci_brief(self.FAILED))
        self.repo.write(".sdlc/state/pause", "")
        self.assertIn("\n```\n== build ==\nboom\n```\n", self.e.ci_brief(self.FAILED))

    def test_an_interactive_brief_waits_with_a_plain_sleep_whatever_the_loops_flags_say(self):
        self.e.stopping = "stop requested"
        with mock.patch.object(self.e, "sleep", side_effect=AssertionError("the loop's sleep")):
            with mock.patch("sdlclib.gitops.time") as clock:
                brief = self.e.ci_brief(self.FAILED, interactive=True)
        clock.sleep.assert_called_once_with(10)
        self.assertIn("\n```\n== build ==\nboom\n```\n", brief)

    def test_the_forge_is_handed_the_loops_sleep_and_for_an_interactive_ship_none(self):
        waits = []
        self.e.forge.check_logs = lambda failed, lines=60, wait=None: waits.append(wait) or ""
        self.e.ci_brief(self.FAILED)
        self.e.ci_brief(self.FAILED, interactive=True)
        self.assertIsNone(waits[1])                                             # the forge's own plain sleep, which nothing stops
        asked = []
        self.e.sleep = lambda seconds: asked.append(seconds) or len(asked) < 2
        self.assertEqual([waits[0](10), waits[0](5)], [True, False])           # it sleeps the loop's way and answers as that sleep does
        self.assertEqual(asked, [10, 5])


class Brief(RepoCase):
    """What a fix session is told about the failed checks."""

    def brief(self, failed, logs):
        e = self.repo.engine()
        e.forge.check_logs = lambda f, lines=60, wait=None: logs
        return e.ci_brief(failed)

    def test_the_sentence_the_checks_and_the_log_in_a_fenced_block_then_what_to_do(self):
        failed = [{"name": "build", "bucket": "fail", "link": run_link(7), "workflow": "CI"},
                  {"name": "ci/jenkins", "bucket": "fail", "link": "https://ci.example.test/1", "workflow": ""},
                  {"name": "lint", "bucket": "fail", "link": "", "workflow": ""}]
        text = self.brief(failed, "== build ==\nFAILED test_x\n")
        lines = text.splitlines()
        self.assertEqual(lines[0], "The PR's checks failed on GitHub after the shell gate had passed (they run on GitHub, not here):")
        self.assertEqual(lines[1:4], ["- `build` (workflow CI) — " + run_link(7), "- `ci/jenkins` — https://ci.example.test/1", "- `lint`"])
        self.assertIn("\n```\n== build ==\nFAILED test_x\n\n```\n", text)                      # in a fence of its own
        self.assertLess(text.index("FAILED test_x"), text.index("Reproduce it locally"))
        for word in ("never weaken, skip or delete a test", "never edit `.github/workflows/` to get green", "print SDLC_FIXED",
                     "SDLC_FIX_BLOCKED <why>", "the CI environment, a missing secret, a flaky external service"):
            self.assertIn(word, text)

    def test_no_log_no_fence(self):
        text = self.brief([{"name": "build", "bucket": "fail", "link": "", "workflow": ""}], "")
        self.assertNotIn("```", text)
        self.assertIn("Reproduce it locally", text)

    def test_a_log_with_a_fence_in_it_gets_a_longer_one(self):
        text = self.brief([{"name": "build", "bucket": "fail", "link": "", "workflow": ""}], "output\n```\nmore\n```\n")
        self.assertIn("\n````\noutput\n```\nmore\n```\n\n````\n", text)

    def test_the_whole_brief_stays_within_the_cap_and_each_log_keeps_its_end(self):
        failed = [{"name": "n" * 150 + str(i), "bucket": "fail", "link": "https://ci.example.test/" + "x" * 300, "workflow": "w" * 120}
                  for i in range(20)]
        logs = "\n".join("line %04d of a long log" % i for i in range(600))                       # about 14 000 characters
        text = self.brief(failed, logs)
        self.assertLessEqual(len(text), CI_BRIEF_CAP)
        self.assertEqual(text.count("\n- `"), 12)                                                # at most twelve are listed
        self.assertIn("- … and 8 more", text)
        self.assertIn("line 0599 of a long log", text)                                           # the end of the log is kept
        self.assertNotIn("line 0000 of a long log", text)
        kept = [l for l in text.splitlines() if l.startswith("line ")]
        self.assertTrue(kept[0].startswith("line "))                                             # the cut is at a line boundary
        self.assertEqual(kept, ["line %04d of a long log" % i for i in range(600 - len(kept), 600)])

    def test_a_log_that_fits_is_kept_whole(self):
        logs = "\n".join("line %d" % i for i in range(50))
        self.assertIn(logs, self.brief([{"name": "build", "bucket": "fail", "link": "", "workflow": ""}], logs))

    def test_names_are_listed_the_way_the_log_says_them(self):
        entries = [{"name": "a", "bucket": "pass"}, {"name": "b", "bucket": "fail"}, {"bucket": "fail"}] + \
            [{"name": "c%d" % i, "bucket": "pending"} for i in range(7)]
        self.assertEqual(check_names(entries, "fail"), "b, check")
        self.assertEqual(check_names(entries, "pending"), "c0, c1, c2, c3, c4, c5, …")
        self.assertEqual(check_names(entries[:3]), "a, b, check")
        self.assertEqual(check_names([]), "")
        self.assertEqual(check_names(entries, "pending", cap=7), "c0, c1, c2, c3, c4, c5, c6")


if __name__ == "__main__":
    unittest.main()
