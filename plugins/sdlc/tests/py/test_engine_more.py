"""The loop engine of release 1.2: the review that has to run, origin's branch that is never overwritten, claims on origin,
old branches brought up to date, run-end reasons that are true, a network blip, the self-check's leftovers, worktrees, the
design commit on record, small loop fixes, the fix brief, status, enqueue, NUL-separated paths and the command line."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helpers import RepoCase, SDLC, STUBS
from sdlclib import session as sess
from sdlclib import state as st
from sdlclib.config import Config
from sdlclib.engine import FIX_REVIEW_RULE, Outcome, StopRun
from sdlclib.enqueue import EnqueueError, new_docs, prepare
from sdlclib.gitops import Git
from sdlclib.queue import scan
from test_engine_github import ChecksBase
from test_checks import check_run

TALLY = "\n## Review\nSDLC_REVIEW_TALLY important=0 nits=1 validated=yes\n"
PLAN = "\n## Plan\n1. Write src/%s.txt.\n\n## Proof\n| R | check | kind |\n|---|---|---|\n| R1 | test command | auto |\n"


def run_log(e):
    with open(e.log_path, encoding="utf-8") as fh:
        return fh.read()


def built_branch(repo, slug="a-first", push=False):
    """What an interactive /sdlc:go leaves on sdlc/<slug>: a built work doc with its review tally, committed (and pushed);
    the checkout stays on the branch."""
    repo.add_items(slug)
    repo.git("checkout", "-q", "-b", "sdlc/" + slug)
    repo.write("work/%s.md" % slug, repo.doc_text(slug, status="built") + PLAN % slug + TALLY)
    repo.write("src/%s.txt" % slug, "built\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "build " + slug)
    if push:
        repo.git("push", "-q", "-u", "origin", "sdlc/" + slug)


def colleague_pushes(repo, branch, path="PERSON.txt", text="a person's commit\n"):
    """Somebody else puts a commit on origin's `branch`, from a clone of their own. The commit's sha."""
    clone, env = tempfile.mkdtemp(prefix="colleague-", dir=repo.tmp), repo.env()

    def git(*args):
        return subprocess.run(["git", "-C", clone] + list(args), check=True, env=env, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "clone", "-q", repo.origin, clone], check=True, env=env, capture_output=True)
    has = git("ls-remote", "--heads", "origin", branch) != ""
    git("checkout", "-q", *(["-B", branch, "origin/" + branch] if has else ["-b", branch]))
    with open(os.path.join(clone, path), "w", encoding="utf-8") as fh:
        fh.write(text)
    git("add", "-A")
    git("commit", "-q", "-m", "PERSON: a fix")
    git("push", "-q", "origin", "HEAD:refs/heads/" + branch)
    return git("rev-parse", "HEAD")


def spy_gate(case):
    """Every call of the merge gate by the engine, as its keyword arguments."""
    from sdlclib import engine
    calls, real = [], engine.run_gate

    def spy(*args, **kw):
        calls.append(kw)
        return real(*args, **kw)
    patcher = mock.patch("sdlclib.engine.run_gate", spy)
    patcher.start()
    case.addCleanup(patcher.stop)
    return calls


def before_sessions(e, action):
    """`action()` runs right before each session the engine starts; what it returns is collected."""
    seen, real = [], e.runner.run

    def run(*args, **kw):
        seen.append(action())
        return real(*args, **kw)
    e.runner.run = run
    return seen


# =====================================================================================================================
# 1. the review has to run
class SubagentStream(unittest.TestCase):
    """What the loop knows of a session's subagents: the `subagent_type` of every call in the stream."""

    def parse(self, *events):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for ev in events:
                    fh.write((ev if isinstance(ev, str) else json.dumps(ev)) + "\n")
            return sess.parse_stream(path)

    @staticmethod
    def use(kind, tool="Agent", **extra):
        block = {"type": "tool_use", "id": "t1", "name": tool, "input": {"description": "d", "subagent_type": kind, "prompt": "p"}}
        block.update(extra)
        return block

    def assistant(self, *blocks, **extra):
        return dict({"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}, **extra)

    def test_every_call_is_listed_in_the_order_it_was_made(self):
        info = self.parse(self.assistant(self.use("sdlc:sdlc-reviewer")), self.assistant(self.use("sdlc:sdlc-verifier"), self.use("Explore")))
        self.assertEqual(info["subagents"], ["sdlc:sdlc-reviewer", "sdlc:sdlc-verifier", "Explore"])

    def test_the_older_tool_name_counts_the_same_as_the_current_one(self):
        self.assertEqual(self.parse(self.assistant(self.use("sdlc:sdlc-reviewer", "Task")))["subagents"], ["sdlc:sdlc-reviewer"])
        self.assertEqual(self.parse(self.assistant(self.use("sdlc:sdlc-reviewer", "Agent")))["subagents"], ["sdlc:sdlc-reviewer"])

    def test_a_call_is_known_by_its_input_not_by_the_name_of_the_tool(self):
        self.assertEqual(self.parse(self.assistant(self.use("sdlc:sdlc-reviewer", "SomethingRenamed")))["subagents"], ["sdlc:sdlc-reviewer"])

    def test_a_call_a_subagent_makes_in_its_own_turn_counts_too(self):
        info = self.parse(self.assistant(self.use("sdlc:sdlc-reviewer"), parent_tool_use_id="toolu_outer"))
        self.assertEqual(info["subagents"], ["sdlc:sdlc-reviewer"])

    def test_what_is_no_call_is_ignored_and_nothing_raises(self):
        junk = [
            self.assistant({"type": "text", "text": "I will call sdlc:sdlc-reviewer"}),
            self.assistant({"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}),
            self.assistant({"type": "tool_use", "name": "Agent", "input": "a string"}),
            self.assistant({"type": "tool_use", "name": "Agent", "input": {"subagent_type": ""}}),
            self.assistant({"type": "tool_use", "name": "Agent", "input": {"subagent_type": 7}}),
            self.assistant("not a block", None, 3),
            {"type": "assistant", "message": {"content": "a string"}},
            {"type": "assistant", "message": "a string"},
            {"type": "assistant"},
            {"type": "user", "message": {"content": [self.use("sdlc:sdlc-reviewer")]}},          # a tool result echo: not a call
            [1, 2, 3], "just a string", 12, None, "{not json",
        ]
        info = self.parse(*[json.dumps(j) if not isinstance(j, str) or j == "just a string" else j for j in junk])
        self.assertEqual(info["subagents"], [])

    def test_no_calls_is_an_empty_list(self):
        self.assertEqual(self.parse({"type": "system", "subtype": "init"})["subagents"], [])
        self.assertEqual(sess.parse_stream("/nonexistent/stream.jsonl")["subagents"], [])

    def test_called_asks_by_the_end_of_the_type(self):
        r = sess.SessionResult()
        r.subagents = ["sdlc:sdlc-reviewer", "Explore"]
        self.assertTrue(r.called("sdlc-reviewer"))
        self.assertTrue(r.called("Explore"))
        self.assertFalse(r.called("sdlc-verifier"))
        self.assertFalse(sess.SessionResult().called("sdlc-reviewer"))

    def test_a_reset_time_in_milliseconds_is_taken_as_seconds(self):
        seconds = 1_760_000_000
        for given in (seconds, seconds * 1000):
            kind, reset, _ = sess.classify_limit("", {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": given})
            self.assertEqual((kind, reset), ("session", float(seconds)))
        self.assertEqual(sess.classify_limit("", {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": "soon"})[1], 0.0)


class ReviewSeen(RepoCase):
    """A loop build session that skipped the reviewer subagent is not merged on the strength of the tally it wrote."""

    def test_a_build_that_skipped_the_reviewer_goes_to_a_fix_session_that_runs_it_and_then_passes(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["no_review"]})
        gate = spy_gate(self)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build", "a-first:fix"])
        self.assertEqual([g["reviewed"] for g in gate], [False, True])
        self.assertIn("the review did not run in a fresh context", run_log(e))
        (fix,) = [c for c in self.repo.calls() if c["key"] == "a-first:fix"]
        self.assertIn("the review did not run in a fresh context", fix["prompt"])
        self.assertIn("wants that review itself", fix["prompt"])
        self.assertIs(self.repo.items_state()["a-first"]["review_seen"], True)
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_build_that_called_the_reviewer_passes_the_first_gate(self):
        self.repo.add_items("a-first")
        gate = spy_gate(self)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        self.assertEqual([g["reviewed"] for g in gate], [True])
        self.assertIs(self.repo.items_state()["a-first"]["review_seen"], True)

    def test_the_older_tool_name_is_as_good(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["subagent:sdlc:sdlc-reviewer:Task"]})
        gate = spy_gate(self)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        self.assertEqual([g["reviewed"] for g in gate], [True])

    def test_another_subagent_is_not_the_review(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["subagent:sdlc:sdlc-verifier"]})
        gate = spy_gate(self)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build", "a-first:fix"])
        self.assertEqual([g["reviewed"] for g in gate], [False, True])

    def test_an_item_built_before_the_loop_kept_the_record_is_not_held_to_it(self):
        built_branch(self.repo)
        self.repo.git("checkout", "-q", "main")
        gate = spy_gate(self)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.calls(), [])                                    # nothing was built again
        self.assertEqual([g["reviewed"] for g in gate], [None])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_record_that_says_the_reviewer_was_seen_is_kept_when_the_build_goes_on(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.scenario({"a-first:build": ["no_review"]})
        e = self.repo.engine()
        e.checkout_item("a-first")
        st.update(self.repo.root, "a-first", review_seen=True)                      # an earlier window of this item showed it
        e.session("a-first", "build")
        self.assertIs(self.repo.items_state()["a-first"]["review_seen"], True)

    def test_a_build_session_starts_with_review_seen_false_until_a_window_shows_the_reviewer(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.scenario({"a-first:build": ["no_review"]})
        e = self.repo.engine()
        e.checkout_item("a-first")
        seen = before_sessions(e, lambda: st.item(self.repo.root, "a-first").get("review_seen"))
        e.session("a-first", "build")
        self.assertEqual(seen, [False])
        self.assertIs(self.repo.items_state()["a-first"]["review_seen"], False)

    def test_an_interactive_ship_asks_the_gate_for_neither_the_review_nor_the_design(self):
        built_branch(self.repo)
        gate = spy_gate(self)
        code, text = self.repo.engine().ship_now("a-first")
        self.assertEqual(code, 0, text)
        self.assertEqual(gate, [{}])


# =====================================================================================================================
# 2. origin's branch is never overwritten
class OriginSync(RepoCase):
    remote = True

    def person_fixed_a_needs_human_pr(self):
        """A run that stops at the merge (a person has to look), then a colleague's commit on origin's branch."""
        self.repo.add_items("a-first")
        os.environ["STUB_GH_MERGE_FAIL"] = "1"
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "needs-human")
        del os.environ["STUB_GH_MERGE_FAIL"]
        return colleague_pushes(self.repo, "sdlc/a-first")

    def origin_tip(self, branch="sdlc/a-first"):
        out = self.repo.git("ls-remote", "origin", "refs/heads/" + branch)
        return out.split()[0] if out else ""

    def merge_calls(self):
        return [c for c in self.repo.gh_db()["calls"] if c[:2] == ["pr", "merge"]]

    def test_a_persons_commit_on_a_needs_human_branch_is_taken_in_and_ends_up_in_what_is_merged(self):
        person = self.person_fixed_a_needs_human_pr()
        st.clear_wait(self.repo.root, "a-first")                                   # what `retry` does
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertIn("SYNC    a-first — took 1 commit(s) from origin's sdlc/a-first (%s)" % person[:10], run_log(e))
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertEqual(self.repo.origin_show("main", "PERSON.txt"), "a person's commit\n")        # not lost
        merge = self.merge_calls()[-1]
        head = merge[merge.index("--match-head-commit") + 1]
        self.assertNotEqual(head, person)                                           # what came in is gated again, so the branch moved on
        self.repo.git("merge-base", "--is-ancestor", person, head)                  # raises when the person's commit is not under it
        self.assertEqual(self.repo.items_state()["a-first"]["remote_sha"], head)    # the PR is merged at the commit that was pushed

    def test_both_sides_having_commits_is_for_a_person_and_nothing_is_pushed_over_theirs(self):
        self.repo.add_items("a-first")
        os.environ["STUB_GH_MERGE_FAIL"] = "1"
        self.assertEqual(self.repo.engine().run(), 0)
        del os.environ["STUB_GH_MERGE_FAIL"]
        person = colleague_pushes(self.repo, "sdlc/a-first")
        self.repo.git("checkout", "-q", "sdlc/a-first")
        self.repo.write("LOCAL.txt", "mine\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "a local commit")
        self.repo.git("checkout", "-q", "main")
        st.clear_wait(self.repo.root, "a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        reason = ("sdlc/a-first on origin has commits this checkout does not have, and the other way round — merge them by hand, "
                  "then retry")
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["reasons"]), ("needs-human", [reason]))
        self.assertIn("HUMAN   a-first — " + reason, run_log(e))
        self.assertEqual(self.origin_tip(), person)                                # origin's branch is as the person left it
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "OPEN")
        self.assertTrue(any("has commits this checkout does not have" in c for c in self.repo.gh_db()["prs"][0]["comments"]))
        # and "merge them by hand, then retry" works
        self.repo.git("fetch", "-q", "origin")
        self.repo.git("checkout", "-q", "sdlc/a-first")
        self.repo.git("merge", "-q", "--no-edit", "origin/sdlc/a-first")
        self.repo.git("checkout", "-q", "main")
        st.clear_wait(self.repo.root, "a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertEqual(self.repo.origin_show("main", "PERSON.txt"), "a person's commit\n")
        self.assertEqual(self.repo.origin_show("main", "LOCAL.txt"), "mine\n")

    def test_a_push_that_loses_to_a_person_is_refused_and_the_person_gets_the_branch(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["person_push"]})                     # a commit lands on origin's branch during the build
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        (reason,) = rec["reasons"]
        self.assertTrue(reason.startswith("remote branch changed"), reason)
        self.assertTrue(reason.endswith("— merge what is on origin's sdlc/a-first into this branch, then retry"), reason)
        self.assertEqual(self.repo.origin_show("sdlc/a-first", "PERSON.txt"), "a person's commit\n")      # not overwritten
        self.assertEqual(self.repo.gh_db()["prs"], [])                             # nothing was published
        self.assertIn("HUMAN   a-first — remote branch changed", run_log(e))
        # merged in by hand, the retry goes through
        self.repo.git("fetch", "-q", "origin")
        self.repo.git("checkout", "-q", "sdlc/a-first")
        self.repo.git("merge", "-q", "--no-edit", "origin/sdlc/a-first")
        self.repo.git("checkout", "-q", "main")
        st.clear_wait(self.repo.root, "a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertEqual(self.repo.origin_show("main", "PERSON.txt"), "a person's commit\n")

    def test_the_commit_origin_has_is_recorded_and_the_merge_is_matched_to_it(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        rec = self.repo.items_state()["a-first"]
        (merge,) = self.merge_calls()
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], rec["remote_sha"])
        self.assertRegex(rec["remote_sha"], r"^[0-9a-f]{40}$")

    def test_a_branch_origin_does_not_have_is_recorded_as_none_and_the_push_must_create_it(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        e.checkout_item("a-first")
        self.assertEqual(self.repo.items_state()["a-first"]["remote_sha"], "")
        colleague_pushes(self.repo, "sdlc/a-first")                                 # somebody creates it first
        ok, why = e.publish("a-first", draft=False)
        self.assertFalse(ok)
        self.assertTrue(why.startswith("remote branch changed"), why)

    def test_a_record_from_before_the_loop_kept_the_sha_looks_before_it_pushes(self):
        person = self.person_fixed_a_needs_human_pr()
        st.update(self.repo.root, "a-first", remote_sha=None)                       # a 1.1 record
        e = self.repo.engine()
        self.repo.git("fetch", "-q", "origin")
        self.repo.git("checkout", "-q", "sdlc/a-first")
        self.repo.write("LOCAL.txt", "mine\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "a local commit")
        ok, why = e.publish("a-first", draft=False)
        self.assertFalse(ok)
        self.assertTrue(why.startswith("remote branch changed"), why)
        self.assertEqual(self.origin_tip(), person)

    # ---------- an interactive ship ----------
    def ship(self):
        return self.repo.cli("ship", "a-first")

    def test_an_interactive_ship_takes_in_what_a_person_pushed_and_merges_it_too(self):
        built_branch(self.repo, push=True)
        colleague_pushes(self.repo, "sdlc/a-first")
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SYNC    a-first", r.stdout)
        self.assertEqual(self.repo.origin_show("main", "PERSON.txt"), "a person's commit\n")
        merge = self.merge_calls()[-1]
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], self.repo.items_state()["a-first"]["remote_sha"])

    def test_an_interactive_ship_where_both_moved_exits_2_with_the_reason_and_pushes_nothing(self):
        built_branch(self.repo, push=True)
        person = colleague_pushes(self.repo, "sdlc/a-first")
        self.repo.write("LOCAL.txt", "mine\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "a local commit")
        r = self.ship()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("sdlc/a-first on origin has commits this checkout does not have, and the other way round — merge them by "
                      "hand, then retry", r.stdout + r.stderr)
        self.assertEqual(self.origin_tip(), person)
        self.assertEqual(self.repo.gh_db()["prs"], [])
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "built")      # the gate did not even run

    def test_an_interactive_ship_leaves_no_claim_behind(self):
        built_branch(self.repo)
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.repo.git("ls-remote", "origin", "refs/sdlc/claims/*"), "")

    def test_an_interactive_ship_of_an_item_another_clone_holds_exits_2_and_says_who(self):
        built_branch(self.repo)
        self.assertEqual(Git(self.repo.root).claim("a-first", "somebody-elses-token", 600), "taken")
        r = self.ship()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("another loop holds a-first on origin: ", r.stdout + r.stderr)
        self.assertIn(" in repo since ", r.stdout + r.stderr)
        self.assertEqual(self.repo.gh_db()["prs"], [])
        self.assertIn("refs/sdlc/claims/a-first", self.repo.git("ls-remote", "origin", "refs/sdlc/claims/*"))      # theirs, untouched


class TeamDesignPr(RepoCase):
    remote = True
    config = {"mode": "team"}

    def test_a_reviewers_commit_on_the_design_pr_is_built_upon_and_not_overwritten(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)                                                # design, then the draft PR waits for approval
        design = self.repo.items_state()["a-first"]["design_sha"]
        reviewer = colleague_pushes(self.repo, "sdlc/a-first", "REVIEW.txt", "reviewer's note\n")
        db = self.repo.gh_db()
        db["prs"][0]["reviews"].append({"author": {"login": "lead"}, "state": "APPROVED"})
        self.repo.gh_db_write(db)
        e2 = self.repo.engine()
        self.assertEqual(e2.run(), 0)
        self.assertIn("SYNC    a-first — took 1 commit(s)", run_log(e2))
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        self.assertEqual(self.repo.origin_show("sdlc/a-first", "REVIEW.txt"), "reviewer's note\n")
        self.assertEqual(self.repo.origin_show("sdlc/a-first", "src/a-first.txt"), "built a-first\n")
        self.repo.git("merge-base", "--is-ancestor", reviewer, "origin/sdlc/a-first")                # raises when it is not
        self.assertFalse(self.repo.gh_db()["prs"][0]["isDraft"])
        self.assertEqual(self.repo.items_state()["a-first"]["design_sha"], reviewer)  # the design as approved includes their commit
        self.assertNotEqual(reviewer, design)


# =====================================================================================================================
# 3. separate clones do not take the same item
class RemoteClaims(ChecksBase):
    config = {"loop": {"checks_wait_minutes": 0.3}}

    def claims_on_origin(self):
        return self.repo.git("ls-remote", "origin", "refs/sdlc/claims/*")

    def hold(self, slug="a-first"):
        self.assertEqual(Git(self.repo.root).claim(slug, "somebody-elses-token", 600), "taken")

    def test_an_item_held_on_origin_is_skipped_and_the_stop_reason_says_why(self):
        self.repo.add_items("a-first")
        self.hold()
        e = self.repo.engine(items=["a-first"])
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertRegex(log, r"SKIP    a-first — held by \S+ in repo since \d{4}-")
        self.assertEqual(self.repo.calls(), [])
        self.assertIn("stop=nothing runnable — 1 held by another loop (a-first)", log)
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "ready")
        self.assertNotIn("claude", os.listdir(self.repo.root))
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(e.claims.held, None)                                       # the local claim was given back

    def test_a_held_item_does_not_stop_the_others_and_is_tried_again_after_a_wait(self):
        self.repo.add_items("a-first", "b-second")
        self.hold()
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")
        self.assertEqual([k for k, *_ in self.keys()], ["b-second:design", "b-second:build"])
        self.assertGreaterEqual(log.count("SKIP    a-first"), 2)                    # looked at again after each wait
        self.assertIn("WAIT    a-first (held by another loop)", log)
        self.assertRegex(stop_of(e), r"^idle for \d+ h with only waiting items")         # looking is no work: the idle limit runs on

    def test_a_claim_is_held_while_the_item_is_worked_on_and_given_back_when_it_is_left(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        held = before_sessions(e, self.claims_on_origin)
        self.assertEqual(e.run(), 0)
        self.assertEqual(len(held), 2)
        for during in held:
            self.assertIn("refs/sdlc/claims/a-first", during)
        self.assertEqual(self.claims_on_origin(), "")

    def test_a_claim_that_cannot_be_taken_on_origin_is_said_once_and_the_run_goes_on(self):
        self.repo.add_items("a-first", "b-second")
        with mock.patch.object(Git, "claim", return_value="error:the network is down"):
            e = self.repo.engine()
            self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertEqual(log.count("WARN    could not take the claim on origin (the network is down); going on without it"), 1)
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "done")
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")

    def test_the_claim_is_renewed_when_a_session_starts_and_while_the_checks_are_waited_for(self):
        self.repo.add_items("a-first")
        running = [check_run("build", "IN_PROGRESS", "")]
        self.script("a-first", running, running, [check_run("build")])
        refreshed, real = [], Git.refresh_claim

        def spy(git, slug, token):
            refreshed.append(slug)
            return real(git, slug, token)
        e = self.fast()
        e.claim_refresh = 0                                                         # every look at the checks is a renewal
        with mock.patch.object(Git, "refresh_claim", spy):
            self.assertEqual(e.run(), 0)
        self.assertGreaterEqual(len(refreshed), self.looks() + 2)                   # + the design and the build session
        self.assertEqual(set(refreshed), {"a-first"})
        self.assertEqual(self.claims_on_origin(), "")

    def test_a_loop_in_another_clone_skips_what_this_one_is_working_on(self):
        self.repo.add_items("a-first")
        clone = os.path.join(self.repo.tmp, "second")
        subprocess.run(["git", "clone", "-q", self.repo.origin, clone], check=True, env=self.repo.env(), capture_output=True)
        from sdlclib.engine import Engine
        second = Engine(clone, items=["a-first"], claude_bin=os.path.join(STUBS, "claude"))
        out = {}

        def other_clone_looks():
            if "code" not in out:
                out["code"] = second.run()
                out["log"] = run_log(second)
            return None
        e = self.repo.engine()
        before_sessions(e, other_clone_looks)
        self.assertEqual(e.run(), 0)
        self.assertEqual(out["code"], 0)
        self.assertRegex(out["log"], r"SKIP    a-first — held by \S+ in repo since ")
        self.assertIn("stop=nothing runnable — 1 held by another loop (a-first)", out["log"])
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "done")      # the first loop finished it, once
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])


# =====================================================================================================================
# 4. an old branch is brought up to date, or kept and started again
class FreshStart(RepoCase):
    """A `ready` item whose branch was cut before its work doc existed starts again from the default branch; the old branch
    is kept as sdlc-old/<slug>. A `planned` item adopts the doc as before."""

    def old_branch(self, slug="a-first"):
        self.repo.git("checkout", "-q", "-b", "sdlc/" + slug)
        self.repo.write("plan/%s.md" % slug, "old plan\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "plan " + slug)
        self.repo.git("checkout", "-q", "main")

    def test_the_old_branch_is_kept_and_the_item_is_designed_and_built_from_the_default_branch(self):
        self.old_branch()
        old = self.repo.git("rev-parse", "sdlc/a-first")
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertIn("FRESH   a-first — sdlc/a-first was cut before its work doc existed; kept as sdlc-old/a-first, "
                      "starting again from main", run_log(e))
        self.assertEqual(self.repo.git("rev-parse", "sdlc-old/a-first"), old)
        self.assertEqual(self.repo.status_of("a-first"), "done")
        self.assertIsNone(self.repo.show("main", "plan/a-first.md"))                # the old work did not come along
        self.assertEqual(self.repo.show("main", "src/a-first.txt"), "built a-first\n")
        self.assertEqual(self.repo.items_state()["a-first"]["old_branch"], "sdlc-old/a-first")
        prompts = {c["key"]: c["prompt"] for c in self.repo.calls()}
        self.assertIn("An older branch `sdlc-old/a-first` has earlier work on this: `git log main..sdlc-old/a-first`; reuse what "
                      "fits.", prompts["a-first:design"])
        self.assertNotIn("sdlc-old", prompts["a-first:build"])

    def test_an_older_branch_kept_before_gets_a_number(self):
        self.old_branch()
        self.repo.git("branch", "sdlc-old/a-first", "main")
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["old_branch"], "sdlc-old/a-first-2")
        self.assertIn("sdlc-old/a-first-2", self.repo.git("branch", "--list"))
        self.assertIn("plan/a-first.md", self.repo.git("ls-tree", "-r", "--name-only", "sdlc-old/a-first-2"))

    def test_a_planned_item_adopts_the_doc_as_before_and_nothing_is_kept_aside(self):
        self.old_branch()
        self.repo.add_items(("a-first", {"status": "planned"}))
        e = self.repo.engine()
        e.checkout_item("a-first")
        self.assertNotIn("sdlc-old", self.repo.git("branch", "--list"))
        self.assertEqual(self.repo.read("work/a-first.md"), self.repo.show("main", "work/a-first.md"))
        self.assertIn("plan/a-first.md", self.repo.git("ls-files"))
        self.assertNotIn("old_branch", self.repo.items_state().get("a-first", {}))

    def test_a_branch_that_has_its_doc_is_not_older_than_it(self):
        self.repo.add_items("a-first")
        self.repo.git("checkout", "-q", "-b", "sdlc/a-first")
        self.repo.write("src/mine.txt", "work in progress\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "wip")
        self.repo.git("checkout", "-q", "main")
        tip = self.repo.git("rev-parse", "sdlc/a-first")
        self.repo.engine().checkout_item("a-first")
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), tip)
        self.assertNotIn("sdlc-old", self.repo.git("branch", "--list"))


class FreshStartOnOrigin(RepoCase):
    remote = True

    def old_branch_on_origin(self):
        r = self.repo
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        r.write("plan/a-first.md", "old plan\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "plan a-first")
        r.git("push", "-q", "origin", "sdlc/a-first")
        old = r.git("rev-parse", "sdlc/a-first")
        r.git("checkout", "-q", "main")
        r.git("branch", "-q", "-D", "sdlc/a-first")                                  # only origin has it
        return old

    def test_the_first_push_replaces_origins_older_branch_with_a_lease_and_the_log_says_so(self):
        old = self.old_branch_on_origin()
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertIn("FRESH   a-first — sdlc/a-first was cut before its work doc existed; kept as sdlc-old/a-first, starting "
                      "again from origin/main (the first push replaces origin's, which is at %s)" % old[:10], log)
        self.assertIn("REPLACE a-first — the push replaces the older sdlc/a-first on origin (%s); the earlier work stays as "
                      "sdlc-old/a-first" % old[:10], log)
        self.assertEqual(self.repo.git("rev-parse", "sdlc-old/a-first"), old)
        self.assertEqual(self.repo.gh_db()["prs"][0]["state"], "MERGED")
        self.assertIsNone(self.repo.origin_show("main", "plan/a-first.md"))
        rec = self.repo.items_state()["a-first"]
        self.assertNotIn("replaces", rec)                                            # once, for the first push
        self.assertRegex(rec["remote_sha"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(rec["remote_sha"], old)

    def test_a_person_who_pushes_to_the_old_branch_meanwhile_is_not_overwritten_either(self):
        self.old_branch_on_origin()
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["person_push"]})                       # on top of the old branch, during the build
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual(rec["state"], "needs-human")
        self.assertTrue(rec["reasons"][0].startswith("remote branch changed"), rec["reasons"])
        self.assertEqual(self.repo.origin_show("sdlc/a-first", "PERSON.txt"), "a person's commit\n")


class CatchUp(RepoCase):
    """A design or build session starts on a branch that has the default branch in it."""

    def test_a_branch_behind_the_default_branch_gets_it_merged_in_before_the_session(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.git("branch", "sdlc/a-first")                                      # cut now, with its doc …
        self.repo.write("other.txt", "landed on main since\n")                        # … and main moves on
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "other work")
        e = self.repo.engine()
        seen = before_sessions(e, lambda: (os.path.exists(self.repo.path("other.txt")), self.repo.git("status", "--porcelain")))
        self.assertEqual(e.run(), 0)
        self.assertEqual(seen, [(True, "")])                                         # the session already had it
        self.assertIn("UPDATE  a-first — merged main into the branch (it was 1 commit(s) behind)", run_log(e))
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:build"])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_merge_that_conflicts_is_undone_and_the_session_is_told_to_do_it(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.git("checkout", "-q", "-b", "sdlc/a-first")
        self.repo.write("x.txt", "the branch's version\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "x on the branch")
        self.repo.git("checkout", "-q", "main")
        self.repo.write("x.txt", "main's version\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "x on main")
        e = self.repo.engine()
        seen = before_sessions(e, lambda: (os.path.exists(self.repo.path(".git/MERGE_HEAD")), self.repo.git("status", "--porcelain")))
        self.assertEqual(e.run(), 0)
        self.assertEqual(seen[0], (False, ""))                                       # nothing half-merged was left for it
        log = run_log(e)
        self.assertIn("UPDATE  a-first — main is 1 commit(s) ahead of the branch and merging it conflicts in: x.txt; the session "
                      "is told to", log)
        build = [c["prompt"] for c in self.repo.calls() if c["key"] == "a-first:build"][0]
        self.assertIn("This branch is 1 commits behind main and `git merge main` conflicts in: x.txt. Merge it first, keep both "
                      "sides' intent, commit, then do the phase.", build)
        self.assertEqual(self.repo.status_of("a-first"), "done")                     # the gate's fix session resolved it

    def test_a_branch_that_has_the_default_branch_already_is_left_alone(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertNotIn("UPDATE", run_log(e))
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])

    def test_a_tree_with_work_in_it_and_the_other_phases_are_left_as_they_are(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.git("branch", "sdlc/a-first")
        self.repo.write("other.txt", "landed on main since\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "other work")
        e = self.repo.engine()
        e.checkout_item("a-first")
        self.assertEqual(e.catch_up_brief("a-first", "fix"), "")                             # a fix goes on the branch as it is
        self.assertFalse(os.path.exists(self.repo.path("other.txt")))
        self.repo.write("src/wip.txt", "a stopped session's work\n")
        self.assertEqual(e.catch_up_brief("a-first", "build"), "")                           # and so does a stopped session's tree
        self.assertFalse(os.path.exists(self.repo.path("other.txt")))
        self.assertEqual(e.catch_up_brief("a-first", "design"), "")
        self.assertFalse(os.path.exists(self.repo.path("other.txt")))


# =====================================================================================================================
# 5. run-end reasons that are true
def stop_of(e):
    m = re.search(r"stop=(.*?) \(\d+ min, \$[\d.]+\)\s*$", run_log(e), re.M)
    return m.group(1) if m else None


class StopReasons(RepoCase):
    def test_an_empty_queue_is_queue_empty(self):
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "queue empty")

    def test_selected_items_that_are_finished_say_so_and_count_the_others(self):
        self.repo.add_items("a-first", "b-other")
        e = self.repo.engine(items=["a-first"])
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "selected items finished · 1 other item(s) not selected")
        self.assertEqual([k for k, *_ in self.keys()], ["a-first:design", "a-first:build"])
        self.assertEqual(self.repo.status_of("b-other"), "ready")

    def test_selected_items_that_are_finished_with_nothing_else_in_the_queue(self):
        self.repo.add_items("a-first")
        e = self.repo.engine(items=["a-first"])
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "selected items finished")

    def test_a_name_that_is_no_work_doc_refuses_the_start_and_names_every_such_name(self):
        self.repo.add_items("a-first")
        e = self.repo.engine(items=["a-first", "nope", "zzz"])
        self.assertEqual(e.run(), 2)
        self.assertIn("ERROR   no work doc named nope, zzz", run_log(e))
        self.assertEqual(self.repo.calls(), [])
        self.assertEqual(self.repo.status_of("a-first"), "ready")

    def test_a_doc_parked_in_the_inbox_is_a_work_doc(self):
        self.repo.write(".sdlc/state/inbox/parked-one.md", self.repo.doc_text("parked-one"))
        e = self.repo.engine(items=["parked-one"])
        self.assertEqual(e.run(), 0)
        self.assertNotIn("no work doc named", run_log(e))
        self.assertEqual(self.repo.status_of("parked-one"), "done")

    def test_what_waits_for_a_person_and_what_waits_for_that_are_two_parts_in_that_order(self):
        self.repo.add_items(("a-idea", {"status": "proposed"}), ("b-second", {"deps": ["a-idea"]}), "c-ready")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "nothing runnable — 1 wait for you (a-idea); 1 wait on other items (b-second)")
        self.assertEqual(self.repo.status_of("c-ready"), "done")

    def test_names_are_cut_after_six(self):
        self.repo.add_items(*[("idea-%d" % i, {"status": "proposed"}) for i in range(1, 9)])
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "nothing runnable — 8 wait for you (idea-1, idea-2, idea-3, idea-4, idea-5, idea-6, …)")

    def test_an_item_that_failed_this_run_is_named_with_what_happens_next(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["error"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 1)
        self.assertEqual(stop_of(e), "nothing runnable — 1 failed this run (a-first) — the next run retries them")
        self.assertEqual(self.repo.engine().run(), 0)                                  # and it does
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_failure_and_the_item_that_waits_on_it(self):
        self.repo.add_items("a-first", ("b-second", {"deps": ["a-first"]}))
        self.repo.scenario({"a-first:design": ["error"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 1)
        self.assertEqual(stop_of(e), "nothing runnable — 1 wait on other items (b-second); 1 failed this run (a-first) — the "
                                     "next run retries them")

    def test_with_items_the_reason_is_about_the_selected_and_counts_the_rest(self):
        self.repo.add_items("a-first", "c-other")
        self.repo.scenario({"a-first:design": ["error"]})
        e = self.repo.engine(items=["a-first"])
        self.assertEqual(e.run(), 1)
        self.assertEqual(stop_of(e), "nothing runnable — 1 failed this run (a-first) — the next run retries them · 1 other "
                                     "item(s) not selected")

    def test_a_selected_proposal_waits_for_a_person(self):
        self.repo.add_items(("a-idea", {"status": "proposed"}))
        e = self.repo.engine(items=["a-idea"])
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "nothing runnable — 1 wait for you (a-idea)")

    def test_an_item_another_loops_claim_holds_is_held_not_waiting_for_a_person(self):
        self.repo.add_items("a-first")
        p = subprocess.Popen(["sleep", "60"])
        self.addCleanup(p.kill)
        from sdlclib.util import proc_start
        claims = os.path.join(self.repo.root, ".git", "sdlc-claims")
        os.makedirs(claims, exist_ok=True)
        with open(os.path.join(claims, "a-first"), "w") as fh:
            fh.write("%d /somewhere/else\n%s\n" % (p.pid, proc_start(p.pid)))
        e = self.repo.engine(items=["a-first"])
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "nothing runnable — 1 held by another loop (a-first)")
        self.assertEqual(self.repo.calls(), [])


class StopReasonsTeam(RepoCase):
    remote = True
    config = {"mode": "team"}

    def test_a_design_that_waits_for_approval_waits_for_you_and_a_built_item_waiting_for_a_merge_is_finished_for_the_loop(self):
        self.repo.add_items("a-first")
        e = self.repo.engine(items=["a-first"])
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "nothing runnable — 1 wait for you (a-first)")
        db = self.repo.gh_db()
        db["prs"][0]["reviews"].append({"author": {"login": "lead"}, "state": "APPROVED"})
        self.repo.gh_db_write(db)
        e2 = self.repo.engine(items=["a-first"])
        self.assertEqual(e2.run(), 0)
        self.assertEqual(stop_of(e2), "selected items finished")
        self.assertEqual(self.repo.items_state()["a-first"]["state"], "awaiting-merge")


class DetachEnds(unittest.TestCase):
    """`run --detach` calls these ends of a loop that was gone at once "nothing to do", whatever the queue held."""

    @staticmethod
    def done(stop, merged=0, failed=0):
        return "== done  merged=%d awaiting=0 parked=0 needs_human=0 failed=%d stop=%s (0 min, $0.00)\n" % (merged, failed, stop)

    def outcome(self, text, rc):
        import sdlc
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "loop.out")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(text)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = sdlc.ended_at_once(log, rc)
        return code, buf.getvalue()

    def test_selected_items_finished_is_a_clean_end(self):
        code, out = self.outcome(self.done("selected items finished · 1 other item(s) not selected"), 0)
        self.assertEqual(code, 0)
        self.assertIn("nothing to do: selected items finished · 1 other item(s) not selected", out)
        code, out = self.outcome(self.done("selected items finished", merged=1), 0)
        self.assertEqual(code, 0)
        self.assertIn("the loop has already finished — == done  merged=1", out)

    def test_the_parts_of_nothing_runnable_are_a_clean_end(self):
        for reason in ("nothing runnable — 1 wait for you (a-idea)", "nothing runnable — 1 held by another loop (a-first)",
                       "nothing runnable — 1 wait on other items (b); 1 failed this run (a) — the next run retries them"):
            code, out = self.outcome(self.done(reason), 0)
            self.assertEqual(code, 0, reason)
            self.assertIn("nothing to do: " + reason, out)

    def test_a_nonzero_exit_is_not_nothing_to_do(self):
        code, out = self.outcome(self.done("selected items finished"), 1)
        self.assertEqual(code, 1)
        self.assertNotIn("nothing to do", out)


# =====================================================================================================================
# 6. a network blip does not stop the loop
class NetworkBlip(RepoCase):
    remote = True

    def test_a_fetch_that_fails_is_said_once_and_the_run_goes_on(self):
        self.repo.add_items("a-first", "b-second")
        real, calls = Git.fetch, []

        def flaky(git):
            calls.append(1)
            if len(calls) <= 3:
                git.last_error = "Could not resolve host: github.test"
                return False
            return real(git)
        with mock.patch.object(Git, "fetch", flaky):
            e = self.repo.engine()
            self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertEqual(log.count("WARN    fetch failed: Could not resolve host: github.test; going on with what is here"), 1)
        self.assertEqual(log.count("WARN    fetch failed"), 1)
        self.assertNotIn("ERROR", log)
        self.assertEqual(self.repo.status_of("a-first", "origin/main"), "done")
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")
        self.assertEqual(stop_of(e), "queue empty")

    def test_a_default_branch_that_really_diverged_from_origins_stops_the_run(self):
        self.repo.add_items("a-first")
        colleague_pushes(self.repo, "main", "COLLEAGUE.txt")
        self.repo.write("MINE.txt", "mine\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "mine")
        e = self.repo.engine()
        self.assertEqual(e.run(), 2)
        self.assertIn("cannot check out or fast-forward main", run_log(e))
        self.assertEqual(self.repo.calls(), [])


class SelectedOnOrigin(RepoCase):
    remote = True

    def test_a_doc_origin_has_and_this_clone_has_not_fetched_yet_is_a_work_doc_for_items(self):
        self.repo.add_items("a-first")
        colleague_pushes(self.repo, "main", "work/fresh-doc.md", self.repo.doc_text("fresh-doc"))
        self.assertIsNone(self.repo.show("origin/main", "work/fresh-doc.md"))                  # not here yet
        e = self.repo.engine(items=["fresh-doc"])
        self.assertEqual(e.run(), 0)
        self.assertNotIn("no work doc named", run_log(e))
        self.assertEqual(self.repo.status_of("fresh-doc", "origin/main"), "done")


# =====================================================================================================================
# 7. what the self-check session leaves in the tree
class SelfCheckLeftovers(RepoCase):
    config = {"loop": {"self_check": True, "self_check_min_items": 1}}

    TRACKED = {"docs/résumé.md": "keep me\n", "docs/설정.txt": "한글\n"}

    def test_every_change_outside_new_work_docs_is_undone_wherever_it_is_and_whatever_it_is_called(self):
        for rel, text in self.TRACKED.items():
            self.repo.write(rel, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "non-ASCII files")
        self.repo.add_items("a-first")
        before = {rel: self.repo.read(rel) for rel in list(self.TRACKED) + ["package.json", ".sdlc/config.json", "work/README.md",
                                                                           "tests/test_base.py"]}
        self.repo.scenario({
            "selfcheck_touch": ["docs/résumé.md", "docs/설정.txt", "package.json", ".sdlc/config.json", "work/README.md",
                                "tests/test_base.py", "work/a-first.md"],
            "selfcheck_write": {"notes/한글 메모.txt": "x\n", "deep/nested/dir/file.bin": "y\n", "work/stray.txt": "z\n",
                                "work/sub/nested.md": "q\n", "top.txt": "t\n"},
            "selfcheck_stage": ["deep/nested/dir/file.bin", "docs/résumé.md", "top.txt"],
            "selfcheck_docs": [{"slug": "fix-one", "status": "proposed"}]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertIn("CHECK   1 new item(s): fix-one (proposed)", run_log(e))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertEqual(self.repo.git("diff", "HEAD", "--stat"), "")
        for rel, text in before.items():
            self.assertEqual(self.repo.read(rel), text, rel)
        for rel in ("notes", "deep", "top.txt", "work/stray.txt", "work/sub"):
            self.assertFalse(os.path.lexists(self.repo.path(rel)), rel)
        self.assertEqual(self.repo.git("ls-files", "work/a-first.md"), "work/a-first.md")
        self.assertIn("\nstatus: done\n", self.repo.show("main", "work/a-first.md"))          # the doc says what it said
        self.assertNotIn("touched by the stub", self.repo.show("main", "work/a-first.md"))
        self.assertEqual(self.repo.status_of("fix-one"), "proposed")                          # the new doc is the one thing kept

    def test_a_new_work_doc_that_is_not_a_doc_is_removed_and_nothing_is_queued(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_write": {"work/한글-메모.md": "no front matter here\n"}})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertIn("CHECK   nothing usable", run_log(e))
        self.assertFalse(os.path.exists(self.repo.path("work/한글-메모.md")))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_a_session_that_did_not_end_ok_is_not_a_self_check_and_what_it_wrote_is_gone(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"self-check": ["max_turns"], "selfcheck_docs": [{"slug": "old-debt", "status": "proposed"}],
                            "selfcheck_write": {"junk.txt": "x\n"}})
        start = self.repo.git("rev-parse", "main")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertIn("CHECK   the session did not finish (turn cap) — not counted; the range is reviewed by the next check",
                      run_log(e))
        self.assertEqual(e.self_checks, 0)
        self.assertEqual(e.from_sha, start)                                                    # the range is still to be reviewed
        self.assertIsNone(self.repo.status_of("old-debt"))                                     # a finding of a cut-short session
        for rel in ("junk.txt", "work/old-debt.md", "src/-partial-0.txt"):
            self.assertFalse(os.path.exists(self.repo.path(rel)), rel)
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertEqual(stop_of(e), "queue empty")

    def test_a_usage_limit_in_the_self_check_is_waited_out_and_the_check_is_made(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"self-check": ["limit_session:1", "ok"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertIn("LIMIT   You've hit your session limit", log)
        self.assertIn("CHECK   clean", log)
        self.assertEqual([k for k, *_ in self.keys()].count("self-check"), 2)
        self.assertEqual(e.self_checks, 1)

    def test_a_limit_that_keeps_coming_stops_the_run_and_says_so(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"self-check": ["limit_session:0.3"] * 6})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertRegex(stop_of(e), r"^usage limit hit 4 times in a row \(")
        self.assertEqual([k for k, *_ in self.keys()].count("self-check"), 4)
        self.assertEqual(e.self_checks, 0)

    def test_a_cap_of_zero_means_no_finding_is_made_ready(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"selfcheck_docs": [{"slug": "fix-one", "status": "ready"}]})
        e = self.repo.engine()
        e.cfg.data["loop"]["self_check_max_items"] = 0
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("fix-one"), "proposed")
        self.assertNotIn("fix-one:design", [k for k, *_ in self.keys()])


# =====================================================================================================================
# 8. an item checked out in another worktree
class Worktrees(RepoCase):
    def elsewhere(self, slug="a-first"):
        path = os.path.join(self.repo.tmp, "elsewhere-" + slug)
        self.repo.git("worktree", "add", "-q", "-b", "sdlc/" + slug, path, "main")
        return path

    def test_it_is_skipped_for_now_and_the_run_goes_on_with_the_rest(self):
        self.repo.add_items("a-first", "b-second")
        self.elsewhere()
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        log = run_log(e)
        self.assertRegex(log, r"SKIP    a-first — checked out in .*elsewhere-a-first")
        self.assertNotIn("ERROR", log)
        self.assertEqual(self.repo.status_of("b-second"), "done")
        self.assertEqual(self.repo.status_of("a-first"), "ready")
        self.assertEqual([k for k, *_ in self.keys()], ["b-second:design", "b-second:build"])
        self.assertRegex(stop_of(e), r"^idle for \d+ h with only waiting items")
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertNotIn("failures", self.repo.items_state().get("a-first", {}))               # nobody failed

    def test_with_items_the_stop_reason_says_it_is_held(self):
        self.repo.add_items("a-first")
        self.elsewhere()
        e = self.repo.engine(items=["a-first"])
        self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "nothing runnable — 1 held by another loop (a-first)")
        self.assertEqual(self.repo.calls(), [])

    def test_it_does_not_count_toward_max_items_nor_toward_failures_in_a_row(self):
        self.repo.add_items("a-first", "b-second")
        self.elsewhere()
        e = self.repo.engine(max_items=1)
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.status_of("b-second"), "done")                              # the one item that was allowed
        self.assertEqual(e.consec, 0)


# =====================================================================================================================
# 9. the commit where the design ended
class DesignOnRecord(RepoCase):
    def test_the_design_commit_is_recorded_and_the_gate_is_held_to_it(self):
        self.repo.add_items("a-first")
        gate = spy_gate(self)
        self.assertEqual(self.repo.engine().run(), 0)
        sha = self.repo.items_state()["a-first"]["design_sha"]
        self.assertEqual(self.repo.git("log", "-1", "--format=%s", sha), "design a-first")
        self.assertEqual([g["design_sha"] for g in gate], [sha])

    def test_a_design_that_asks_a_question_records_nothing(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["needs_input"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertNotIn("design_sha", self.repo.items_state()["a-first"])

    def test_a_design_that_is_reopened_loses_it(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["design_empty"]})
        st.update(self.repo.root, "a-first", design_sha="deadbeef")
        self.assertEqual(self.repo.engine().run(), 1)
        self.assertNotIn("design_sha", self.repo.items_state()["a-first"])
        self.assertEqual(self.repo.engine().run(), 0)                                          # designed again, recorded again
        self.assertRegex(self.repo.items_state()["a-first"]["design_sha"], r"^[0-9a-f]{40}$")

    def test_an_answer_that_sends_the_item_back_to_design_loses_it(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["needs_input", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        st.update(self.repo.root, "a-first", design_sha="deadbeef")
        self.assertEqual(self.repo.cli("answer", "a-first", "KRW").returncode, 0)
        e = self.repo.engine()
        seen = before_sessions(e, lambda: st.item(self.repo.root, "a-first").get("design_sha"))
        self.assertEqual(e.run(), 0)
        self.assertEqual(seen[0], None)                                                        # cleared before the design is redone
        self.assertRegex(seen[1], r"^[0-9a-f]{40}$")                                           # and the new design recorded


# =====================================================================================================================
# 10. small loop fixes
class SmallLoopFixes(RepoCase):
    def test_the_fingerprint_does_not_change_because_leave_item_committed_the_work(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        e = self.repo.engine()
        e.checkout_item("a-first")
        self.repo.write("src/new.txt", "x\n")
        self.repo.write("src/한글.txt", "y\n")
        self.repo.write("work/a-first.md", self.repo.read("work/a-first.md") + "\nsome note\n")
        self.repo.write(".pytest_cache/v/cache/lastfailed", "{}")                              # noise: nobody wrote it
        dirty = e.fingerprint()
        self.repo.write(".pytest_cache/v/cache/lastfailed", "{\"other\": 1}")
        self.assertEqual(e.fingerprint(), dirty)
        e.leave_item("a-first")                                                                # commits the work in progress
        self.assertEqual(self.repo.git("status", "--porcelain", "--", ".", ":(exclude).pytest_cache"), "")      # noise stays out of it
        self.assertIn("work in progress", self.repo.git("log", "-1", "--format=%s"))
        self.assertEqual(e.fingerprint(), dirty)
        self.repo.write("src/new.txt", "changed\n")
        self.assertNotEqual(e.fingerprint(), dirty)

    def test_a_stop_that_changed_nothing_is_still_no_progress_after_the_work_was_committed(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["max_turns", "max_turns_noprogress", "max_turns_noprogress"]})
        self.assertEqual(self.repo.engine().run(), 1)
        self.assertIn("no progress", self.repo.items_state()["a-first"]["last_error"])

    def fix_calls(self):
        return [c for c in self.repo.calls() if c["key"] == "a-first:fix"]

    def test_a_resumed_fix_session_is_told_a_brief_other_than_the_one_it_started_with(self):
        self.repo.add_items(("a-first", {"status": "planned"}))
        self.repo.scenario({"a-first:fix": ["limit_session:7200", "ok", "limit_session:7200", "ok"]})
        e = self.repo.engine()
        e.limit_wait = 60                                                                      # the wait is beyond it: a stop
        e.checkout_item("a-first")
        with self.assertRaises(StopRun):
            e.session("a-first", "fix", extra="Brief ONE: the gate failed on x", fix_round=1)
        e.limit_wait = 3600 * 6
        self.assertEqual(e.session("a-first", "fix", extra="Brief TWO: now it fails on y", fix_round=1), Outcome.ADVANCED)
        first, second = self.fix_calls()
        self.assertIn("Brief ONE", first["prompt"])
        self.assertTrue(second["resumed"])
        self.assertTrue(second["prompt"].startswith("Continue where you stopped"))
        self.assertIn("What has to be done now is not what you started with. The brief is:\n\nBrief TWO: now it fails on y",
                      second["prompt"])
        # the same brief again is not told twice
        e.limit_wait = 60
        with self.assertRaises(StopRun):
            e.session("a-first", "fix", extra="Brief TWO: now it fails on y", fix_round=1)
        e.limit_wait = 3600 * 6
        e.session("a-first", "fix", extra="Brief TWO: now it fails on y", fix_round=1)
        fourth = self.fix_calls()[3]
        self.assertTrue(fourth["resumed"])
        self.assertNotIn("not what you started with", fourth["prompt"])

    def test_no_fix_session_is_started_when_a_stop_came_during_the_gate(self):
        from sdlclib import engine
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["fail_gate"]})
        e = self.repo.engine()
        real = engine.run_gate

        def gate(*args, **kw):
            res = real(*args, **kw)
            e.stopping = "stop requested"                                                      # it came while the gate ran
            return res
        with mock.patch("sdlclib.engine.run_gate", gate):
            self.assertEqual(e.run(), 0)
        self.assertEqual(stop_of(e), "stop requested")
        self.assertNotIn("a-first:fix", [k for k, *_ in self.keys()])
        self.assertEqual(self.repo.status_of("a-first", "sdlc/a-first"), "built")

    def test_an_answer_shaped_wrongly_fails_the_item_and_is_logged_in_full_not_a_traceback_that_ends_the_run(self):
        self.repo.add_items("a-first", "b-second")
        e = self.repo.engine()
        real = e.gate_phase

        def gate_phase(slug):
            if slug == "a-first":
                raise TypeError("bad operand type for unary -: 'str'")                         # what unexpected gh JSON does
            return real(slug)
        e.gate_phase = gate_phase
        self.assertEqual(e.run(), 1)
        log = run_log(e)
        self.assertIn("ERROR   a-first — TypeError: bad operand type for unary -: 'str' (the traceback is in the run log)", log)
        self.assertIn("FAILED  a-first — unexpected TypeError", log)
        self.assertIn("Traceback (most recent call last)", log)
        self.assertEqual(self.repo.status_of("b-second"), "done")                              # the run went on
        self.assertIn("unexpected TypeError", self.repo.items_state()["a-first"]["last_error"])


class CiRounds(ChecksBase):
    config = {"loop": {"checks_wait_minutes": 0.3}}

    def test_a_service_error_in_the_fix_session_does_not_use_the_round(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE")])
        self.repo.scenario({"a-first:fix": ["infra", "infra", "infra"]})
        e = self.fast(max_items=1)
        self.assertEqual(e.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertNotIn("ci_rounds", rec)
        self.assertEqual([k for k, *_ in self.keys()].count("a-first:fix"), 3)
        e2 = self.fast()                                                                       # the checks keep failing: both rounds are still there
        self.assertEqual(e2.run(), 0)
        rec = self.repo.items_state()["a-first"]
        self.assertEqual((rec["state"], rec["ci_rounds"]), ("needs-human", 2))
        self.assertEqual(rec["reasons"], ["the PR's checks still fail after 2 fix round(s): build"])

    def test_a_stop_during_the_fix_session_does_not_use_the_round(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE")])
        e = self.fast(max_items=1)
        real = e.session

        def session(slug, phase, extra="", fix_round=0):
            if phase == "fix":
                raise StopRun("stop requested")
            return real(slug, phase, extra, fix_round)
        e.session = session
        self.assertEqual(e.run(), 0)
        self.assertIn("stop=stop requested", self.log(e))
        self.assertNotIn("ci_rounds", self.repo.items_state()["a-first"])

    def test_a_stop_that_came_while_the_brief_was_made_starts_no_fix_session_and_uses_no_round(self):
        self.repo.add_items("a-first")
        self.script("a-first", [check_run("build", conclusion="FAILURE")])
        e = self.fast(max_items=1)
        real = e.ci_brief

        def brief(failed, interactive=False):
            text = real(failed, interactive)
            e.stopping = "stop requested"
            return text
        e.ci_brief = brief
        self.assertEqual(e.run(), 0)
        self.assertIn("stop=stop requested", self.log(e))
        self.assertNotIn("a-first:fix", [k for k, *_ in self.keys()])
        self.assertNotIn("ci_rounds", self.repo.items_state()["a-first"])


# =====================================================================================================================
# 11. the fix brief says what references/fix.md says
class FixBriefs(RepoCase):
    def test_the_gates_fix_brief_has_the_review_rule_and_the_way_out(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["fail_gate"]})
        self.assertEqual(self.repo.engine().run(), 0)
        (fix,) = [c["prompt"] for c in self.repo.calls() if c["key"] == "a-first:fix"]
        self.assertIn(FIX_REVIEW_RULE, fix)
        for word in ("never weaken, skip or delete a test", "Commit as a new commit, then print SDLC_FIXED",
                     "SDLC_FIX_BLOCKED <why>", "A command that already fails or flakes on the default branch"):
            self.assertIn(word, fix)
        self.assertNotIn("Update the `## Review` tally", fix)

    def test_the_checks_fix_brief_has_the_same_rule(self):
        e = self.repo.engine()
        e.forge.check_logs = lambda f, lines=60, wait=None: ""
        text = e.ci_brief([{"name": "build", "bucket": "fail", "link": "", "workflow": ""}])
        self.assertIn(FIX_REVIEW_RULE, text)
        self.assertIn("Commit as a new commit, then print SDLC_FIXED", text)
        self.assertIn("a command that already fails on the default branch", text)
        self.assertNotIn("Update the `## Review` tally", text)


# =====================================================================================================================
# 12. status
class StatusLines(RepoCase):
    def status(self, *args):
        r = self.repo.cli("status", "--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_needs_you_line_is_one_line_the_title_for_a_proposal_and_collapsed_text_for_the_rest(self):
        self.repo.add_items(("a-idea", {"status": "proposed", "title": "Offer a refund button",
                                        "goal": "<!-- what and why -->\nSecond line of the goal."}), "b-stuck")
        st.update(self.repo.root, "b-stuck", state="needs-human", reasons=["first line\nsecond   line " + "z" * 300])
        s = self.status()
        lines = {n["slug"]: n for n in s["needs_you"]}
        self.assertEqual(lines["a-idea"]["text"], ["Offer a refund button"])
        (text,) = lines["b-stuck"]["text"]
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith("first line second line zzz"), text)
        self.assertLessEqual(len(text), 160)
        self.assertTrue(text.endswith("…"), text)
        rendered = self.repo.cli("status").stdout
        self.assertIn("Offer a refund button", rendered)
        self.assertNotIn("what and why", rendered)

    def with_proof(self, slug, rows, status="planned"):
        text = self.repo.doc_text(slug, status=status) + "\n## Plan\n1. Do it.\n\n## Proof\n| R | check | kind |\n|---|---|---|\n" + rows
        self.repo.write("work/%s.md" % slug, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "queue " + slug)

    def test_worth_a_look_names_the_manual_proof_and_puts_those_items_first(self):
        self.with_proof("a-first", "| R1 | test command | auto |\n| R3 | open the page and look at the colors | manual |\n")
        self.repo.add_items("b-second")                                                        # costs more, nothing to check by hand
        self.assertEqual(self.repo.engine().run(), 0)
        s = self.status()
        self.assertEqual([r["slug"] for r in s["review_sample"]], ["a-first", "b-second"])
        self.assertEqual(s["review_sample"][0]["manual_proof"], ["R3 — open the page and look at the colors"])
        self.assertEqual(s["review_sample"][1]["manual_proof"], [])
        rendered = self.repo.cli("status").stdout
        line = [l for l in rendered.splitlines() if l.startswith("worth a look")][0]
        self.assertIn("a-first", line)
        self.assertIn("(manual proof: R3 — open the page and look at the colors)", line)
        self.assertLess(line.index("a-first"), line.index("b-second"))

    def test_many_manual_rows_are_cut_after_three(self):
        import sdlclib.status as status
        s = status.collect(self.repo.root)
        s["review_sample"] = [{"slug": "a-first", "pr": "", "sha": "0123456789abcdef", "manual_proof": ["R%d — x" % i for i in range(1, 6)]}]
        line = [l for l in status.render(s).splitlines() if l.startswith("worth a look")][0]
        self.assertIn("(manual proof: R1 — x; manual proof: R2 — x; manual proof: R3 — x; +2 more)", line)

    def test_manual_rows_are_read_from_the_proof_table_only(self):
        from sdlclib.status import manual_proof
        from sdlclib.workdoc import WorkDoc
        doc = WorkDoc.parse(self.repo.doc_text("a-first") + "\n## Proof\n| R | check | kind |\n|---|---|---|\n"
                            "| R1 | `pytest -q` | auto |\n| R2 | look at it | **manual** |\n| R3 | " + "x" * 200 + " | manual |\n")
        rows = manual_proof(doc)
        self.assertEqual(rows[0], "R2 — look at it")
        self.assertEqual(len(rows), 2)
        self.assertLessEqual(len(rows[1]), len("R3 — ") + 80)
        self.assertEqual(manual_proof(None), [])


class StatusSeesLocalWork(RepoCase):
    remote = True
    config = {"loop": {"forge": "local"}}

    def test_the_forge_is_made_before_the_base_is_asked_for_so_local_work_is_what_is_shown(self):
        self.repo.add_items("a-first", push=False)                      # on main here, not on origin yet
        r = self.repo.cli("status", "--json", "--no-fetch")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([i["slug"] for i in json.loads(r.stdout)["items"]], ["a-first"])

    def test_the_commands_that_look_an_item_up_do_too(self):
        import sdlc
        self.repo.add_items("a-first", push=False)
        cfg, git, item = sdlc._find_item(self.repo.root, "a-first")
        self.assertIsNotNone(item)


# =====================================================================================================================
# 13. enqueue and the queue
class EnqueueRules(RepoCase):
    def prepare(self, slug, base="main", **doc):
        path = self.repo.write("work/%s.md" % slug, self.repo.doc_text(slug, **doc))
        return prepare(Config(self.repo.root), Git(self.repo.root), base, [path])

    def test_a_new_doc_whose_slug_has_a_branch_is_refused(self):
        self.repo.git("branch", "sdlc/a-first", "main")
        with self.assertRaises(EnqueueError) as ctx:
            self.prepare("a-first")
        self.assertIn("a branch sdlc/a-first already exists — pick another slug or delete the branch", str(ctx.exception))
        r = self.repo.cli("enqueue", self.repo.path("work/a-first.md"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a branch sdlc/a-first already exists", r.stdout + r.stderr)

    def test_a_doc_already_on_the_default_branch_may_have_its_branch(self):
        self.repo.add_items("a-first")
        self.repo.git("branch", "sdlc/a-first", "main")
        (doc,) = self.prepare("a-first")                           # the same doc, as committed
        self.assertEqual(doc.slug, "a-first")

    def test_a_doc_whose_slug_is_not_used_by_any_branch_is_fine(self):
        self.repo.git("branch", "sdlc/other", "main")
        (doc,) = self.prepare("a-first")
        self.assertEqual(doc.slug, "a-first")

    def test_a_doc_parked_in_the_inbox_counts_as_queued(self):
        self.repo.write(".sdlc/state/inbox/parked-one.md", self.repo.doc_text("parked-one"))
        (doc,) = self.prepare("later-one")
        self.assertEqual(doc.slug, "later-one")
        path = self.repo.write("work/later-two.md", self.repo.doc_text("later-two", deps=["parked-one"]))
        (doc,) = prepare(Config(self.repo.root), Git(self.repo.root), "main", [path])
        self.assertEqual(doc.depends_on, ["parked-one"])                  # a dependency on a parked doc is accepted
        with self.assertRaises(EnqueueError) as ctx:
            self.prepare("parked-one")                              # the same slug twice is not
        self.assertIn("work/parked-one.md is already waiting in the inbox", str(ctx.exception))

    def test_the_parked_doc_itself_is_not_refused_for_being_parked(self):
        path = self.repo.write(".sdlc/state/inbox/parked-one.md", self.repo.doc_text("parked-one"))
        (doc,) = prepare(Config(self.repo.root), Git(self.repo.root), "main", [path])
        self.assertEqual(doc.slug, "parked-one")

    def test_a_dependency_that_is_nowhere_is_still_refused(self):
        path = self.repo.write("work/later-two.md", self.repo.doc_text("later-two", deps=["nowhere"]))
        with self.assertRaises(EnqueueError) as ctx:
            prepare(Config(self.repo.root), Git(self.repo.root), "main", [path])
        self.assertIn("depends_on 'nowhere' — no such work doc", str(ctx.exception))

    def test_new_ignores_a_doc_that_was_deleted_in_the_working_tree(self):
        self.repo.add_items("old-done")
        os.remove(self.repo.path("work/old-done.md"))
        self.repo.write("work/new-one.md", self.repo.doc_text("new-one"))
        found = new_docs(Config(self.repo.root), Git(self.repo.root))
        self.assertEqual([os.path.basename(p) for p in found], ["new-one.md"])
        self.repo.git("rm", "-q", "--cached", "work/old-done.md")                      # staged as a deletion too
        found = new_docs(Config(self.repo.root), Git(self.repo.root))
        self.assertEqual([os.path.basename(p) for p in found], ["new-one.md"])

    def test_new_lists_docs_whose_names_git_would_quote_and_follows_a_rename_to_its_new_name(self):
        self.repo.add_items("renamed-from")
        self.repo.git("mv", "work/renamed-from.md", "work/renamed-to.md")
        self.repo.write("work/café-notes.md", self.repo.doc_text("cafe-notes"))
        self.repo.write("work/한글.md", self.repo.doc_text("hangul"))
        found = sorted(os.path.basename(p) for p in new_docs(Config(self.repo.root), Git(self.repo.root)))
        self.assertEqual(found, ["café-notes.md", "renamed-to.md", "한글.md"])


class EnqueueRulesOnOrigin(RepoCase):
    remote = True

    def test_a_branch_only_origin_has_is_refused_too(self):
        r = self.repo
        r.git("checkout", "-q", "-b", "sdlc/a-first")
        r.write("x.txt", "x\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "x")
        r.git("push", "-q", "origin", "sdlc/a-first")
        r.git("checkout", "-q", "main")
        r.git("branch", "-q", "-D", "sdlc/a-first")
        path = r.write("work/a-first.md", r.doc_text("a-first"))
        with self.assertRaises(EnqueueError) as ctx:
            prepare(Config(r.root), Git(r.root), "origin/main", [path])
        self.assertIn("a branch sdlc/a-first already exists — pick another slug or delete the branch", str(ctx.exception))


# =====================================================================================================================
# 14. paths git would quote
class NulPaths(RepoCase):
    def test_a_work_doc_with_a_non_ascii_name_is_in_the_queue(self):
        self.repo.add_items("café-item", "plain-item")
        items = scan(Config(self.repo.root), Git(self.repo.root), "main", {})
        self.assertEqual(sorted(items), ["café-item", "plain-item"])

    def test_the_loop_works_such_an_item(self):
        self.repo.add_items("café-item")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual(self.repo.status_of("café-item"), "done")


# =====================================================================================================================
# 15. the command line
class StopCommand(RepoCase):
    def stop(self, error=None):
        import argparse
        import sdlc
        st.write_pid(self.repo.root)                                      # this process: a loop that is alive
        out, err = io.StringIO(), io.StringIO()
        real = os.kill

        def kill(pid, sig):
            if sig == 0:                                                  # "is it alive?" is asked the usual way
                return real(pid, sig)
            if error:
                raise error                                               # only the signal that stops it fails
        with mock.patch("os.kill", kill), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sdlc.cmd_stop(argparse.Namespace(root=self.repo.root, after_item=False))
        st.clear_pid(self.repo.root)
        return code, out.getvalue(), err.getvalue()

    def test_a_loop_that_is_gone_already_is_not_an_error(self):
        code, out, err = self.stop(ProcessLookupError())
        self.assertEqual(code, 0)
        self.assertIn("the loop (pid %d) is gone already" % os.getpid(), out)

    def test_a_loop_of_another_user_cannot_be_signalled_and_nothing_raises(self):
        code, out, err = self.stop(PermissionError())
        self.assertEqual(code, 1)
        self.assertIn("cannot signal the loop (pid %d): it belongs to another user" % os.getpid(), err)

    def test_any_other_failure_to_signal_is_said_too(self):
        code, out, err = self.stop(OSError(22, "Invalid argument"))
        self.assertEqual(code, 1)
        self.assertIn("cannot signal the loop (pid %d)" % os.getpid(), err)

    def test_a_pid_file_of_a_process_that_is_not_the_loop_is_no_loop(self):
        self.repo.write(".sdlc/state/loop.pid", "%d some-other-start-time\n" % os.getpid())      # alive, but not what wrote the file
        r = self.repo.cli("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the loop is not running", r.stdout)


class UnexpectedForgeAnswers(RepoCase):
    remote = True

    def test_a_pull_request_listing_that_is_not_what_gh_prints_fails_the_item_quietly_and_the_run_goes_on(self):
        self.repo.add_items("a-first", "b-second")
        self.repo.gh_db_write({"prs": [{"number": "seven", "head": "sdlc/a-first", "headRefName": "sdlc/a-first", "base": "main",
                                        "title": "t", "body": "", "isDraft": False, "state": "OPEN", "url": "u", "labels": [],
                                        "reviews": [], "reviewDecision": "", "author": {"login": "me"}, "comments": []}]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 1)
        log = run_log(e)
        self.assertNotIn("Traceback", log)
        self.assertIn("FAILED  a-first — could not publish: unreadable gh output", log)
        self.assertEqual(self.repo.status_of("b-second", "origin/main"), "done")


if __name__ == "__main__":
    unittest.main()
