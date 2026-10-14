"""PR checks: what the GitHub forge reads from `gh pr view` and `gh run view` (the stub gh plays CI), how the stub scripts
checks and a branch that is behind its base, and the local forge, which has none."""
import json
import os
import subprocess
import tempfile
import unittest

from helpers import RepoCase, STUBS

from sdlclib.gitops import CHECK_LOGS_CAP, Forge, Git, GitHubForge, LocalForge, end_of


def run_link(run_id, job=1):
    return "https://github.com/o/r/actions/runs/%s/job/%s" % (run_id, job)


def check_run(name, status="COMPLETED", conclusion="SUCCESS", run_id=100, workflow="CI"):
    """A CheckRun the way `gh pr view --json statusCheckRollup` prints it."""
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "detailsUrl": run_link(run_id), "workflowName": workflow}


def status_context(context, state="SUCCESS", url="https://ci.example.test/build/7"):
    """A StatusContext — a commit status set through the older API — in the same shape."""
    return {"__typename": "StatusContext", "context": context, "state": state, "targetUrl": url}


def failed(name, link="", workflow="CI"):
    """A failed entry as `checks` returns it."""
    return {"name": name, "bucket": "fail", "link": link, "workflow": workflow}


# (status, conclusion, bucket) of a CheckRun; a StatusContext is (state, bucket)
CHECK_RUN_BUCKETS = [
    ("COMPLETED", "SUCCESS", "pass"), ("COMPLETED", "NEUTRAL", "pass"), ("COMPLETED", "SKIPPED", "pass"),
    ("COMPLETED", "FAILURE", "fail"), ("COMPLETED", "TIMED_OUT", "fail"), ("COMPLETED", "CANCELLED", "fail"),
    ("COMPLETED", "ACTION_REQUIRED", "fail"), ("COMPLETED", "STARTUP_FAILURE", "fail"), ("COMPLETED", "STALE", "fail"),
    ("COMPLETED", "", "fail"),
    ("QUEUED", "", "pending"), ("IN_PROGRESS", "", "pending"), ("WAITING", "", "pending"), ("PENDING", "", "pending"),
    ("REQUESTED", "", "pending"), ("IN_PROGRESS", "SUCCESS", "pending"),       # not completed: the conclusion does not count yet
]
STATUS_CONTEXT_BUCKETS = [("SUCCESS", "pass"), ("FAILURE", "fail"), ("ERROR", "fail"), ("PENDING", "pending"),
                          ("EXPECTED", "pending")]


class GitHub(RepoCase):
    remote = True

    def setUp(self):
        super().setUp()
        self.forge = GitHubForge(Git(self.repo.root), "main")

    def gh(self, *args):
        """The stub itself, the way a person would run it."""
        return subprocess.run([os.path.join(STUBS, "gh")] + list(args), cwd=self.repo.root, capture_output=True, text=True)

    def open_pr(self, slug="a-first", path="feature.txt"):
        """sdlc/<slug>: one commit ahead of main, pushed, with a PR open on the stub (#1 for the first). Back on main after."""
        self.repo.git("checkout", "-q", "-b", "sdlc/" + slug)
        self.repo.write(path, "feature\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "feature " + slug)
        ok, url = self.forge.publish(slug, "A title", "A body")
        self.assertTrue(ok, url)
        self.repo.git("checkout", "-q", "main")

    def db_set(self, **keys):
        db = self.repo.gh_db()
        db.update(keys)
        self.repo.gh_db_write(db)

    def script(self, slug, *snapshots):
        """The snapshots `pr view` plays for the head of sdlc/<slug>, one per call; the last one stays."""
        checks = self.repo.gh_db().get("checks", {})
        checks["sdlc/" + slug] = list(snapshots)
        self.db_set(checks=checks)

    def run_views(self):
        return [c for c in self.repo.gh_db().get("calls", []) if c[:2] == ["run", "view"]]


class Mapping(GitHub):
    """Every CheckRun status and conclusion, every StatusContext state, lands in pass, fail or pending."""

    def test_every_check_run_status_and_conclusion_lands_in_its_bucket(self):
        self.open_pr()
        self.script("a-first", [check_run("c%d" % i, s, c) for i, (s, c, _) in enumerate(CHECK_RUN_BUCKETS)])
        state, found = self.forge.checks("a-first")
        self.assertEqual(state, "fail")
        self.assertEqual(len(found), len(CHECK_RUN_BUCKETS))
        for (status, conclusion, bucket), got in zip(CHECK_RUN_BUCKETS, found):
            self.assertEqual(got["bucket"], bucket, "%s / %s" % (status, conclusion or "no conclusion"))

    def test_every_status_context_state_lands_in_its_bucket(self):
        self.open_pr()
        self.script("a-first", [status_context("ci/c%d" % i, s) for i, (s, _) in enumerate(STATUS_CONTEXT_BUCKETS)])
        state, found = self.forge.checks("a-first")
        self.assertEqual(state, "fail")
        self.assertEqual(len(found), len(STATUS_CONTEXT_BUCKETS))
        for (status, bucket), got in zip(STATUS_CONTEXT_BUCKETS, found):
            self.assertEqual(got["bucket"], bucket, status)

    def test_an_entry_carries_name_bucket_link_and_workflow(self):
        self.open_pr()
        self.script("a-first", [check_run("build", run_id=7, workflow="CI"),
                                status_context("ci/jenkins", "FAILURE", "https://ci.example.test/1"),
                                {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}])
        _, found = self.forge.checks("a-first")
        self.assertEqual(found, [{"name": "build", "bucket": "pass", "link": run_link(7), "workflow": "CI"},
                                 {"name": "ci/jenkins", "bucket": "fail", "link": "https://ci.example.test/1", "workflow": ""},
                                 {"name": "lint", "bucket": "pass", "link": "", "workflow": ""}])   # what gh left out is empty

    def test_an_entry_without_a_typename_is_told_apart_by_its_fields(self):
        self.open_pr()
        self.script("a-first", [{"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"},
                                {"context": "ci/x", "state": "SUCCESS", "targetUrl": "https://ci.example.test/x"}])
        state, found = self.forge.checks("a-first")
        self.assertEqual((state, [(c["name"], c["bucket"]) for c in found]), ("fail", [("build", "fail"), ("ci/x", "pass")]))


class Overall(GitHub):
    """The state of the whole PR: fail beats pending beats pass; no checks is none; what cannot be read is unknown."""

    def state_of(self, *entries):
        self.script("a-first", list(entries))
        return self.forge.checks("a-first")[0]

    def test_pass_when_everything_passed(self):
        self.open_pr()
        self.assertEqual(self.state_of(check_run("build"), check_run("lint", conclusion="SKIPPED"), status_context("ci/x")), "pass")

    def test_pending_while_a_check_is_still_open_and_none_failed(self):
        self.open_pr()
        self.assertEqual(self.state_of(check_run("build"), check_run("test", "IN_PROGRESS", "")), "pending")
        self.assertEqual(self.state_of(check_run("build"), status_context("ci/x", "PENDING")), "pending")

    def test_fail_as_soon_as_one_failed_even_while_others_still_run(self):
        self.open_pr()
        self.assertEqual(self.state_of(check_run("build", conclusion="FAILURE"), check_run("test", "IN_PROGRESS", ""),
                                       check_run("lint")), "fail")

    def test_none_when_the_pr_has_no_checks(self):
        self.open_pr()
        self.assertEqual(self.forge.checks("a-first"), ("none", []))                      # nothing scripted: no CI
        for snapshot in ([], None):                                                       # what gh prints with no checks: [] or null
            self.script("a-first", snapshot)
            self.assertEqual(self.forge.checks("a-first"), ("none", []), snapshot)

    def test_unknown_without_an_open_pr(self):
        self.assertEqual(self.forge.checks("a-first"), ("unknown", []))                   # never published
        self.open_pr()
        self.script("a-first", [check_run("build")])
        self.assertEqual(self.forge.checks("a-first")[0], "pass")
        ok, info = self.forge.merge("a-first", "A title")
        self.assertTrue(ok, info)
        self.assertEqual(self.forge.checks("a-first"), ("unknown", []))                   # merged: no open PR any more

    def test_unknown_when_gh_fails_and_back_to_normal_when_it_recovers(self):
        self.open_pr()
        self.script("a-first", [check_run("build")])
        os.environ["STUB_GH_VIEW_FAIL"] = "1"
        self.assertEqual(self.forge.checks("a-first"), ("unknown", []))
        self.assertEqual(self.forge.merge_state("a-first"), "")
        del os.environ["STUB_GH_VIEW_FAIL"]
        self.assertEqual(self.forge.checks("a-first")[0], "pass")

    def test_unknown_when_gh_is_not_installed(self):
        self.open_pr()
        os.environ["PATH"] = tempfile.mkdtemp(prefix="no-gh-", dir=self.repo.tmp)         # no gh anywhere, the real one included
        self.assertEqual(self.forge.checks("a-first"), ("unknown", []))
        self.assertEqual(self.forge.merge_state("a-first"), "")

    def test_unknown_when_what_gh_printed_is_not_what_was_asked_for(self):
        self.open_pr()
        for snapshot in ({"error": "boom"},                                                 # gh fails
                         {"raw": "this is not json"}, {"raw": "[]"}, {"raw": "{}"},       # not the object with the field
                         "a string", 3, ["not an entry"]):                                  # the field is not a list of entries
            self.script("a-first", snapshot)
            self.assertEqual(self.forge.checks("a-first"), ("unknown", []), snapshot)


class Scripted(GitHub):
    """The stub's scripted CI and its `pr view`."""

    def test_the_snapshots_play_out_one_per_call_and_the_last_one_stays(self):
        self.open_pr()
        self.open_pr("b-second", "other.txt")
        pending, passed = [check_run("build", "IN_PROGRESS", "")], [check_run("build")]
        self.script("a-first", pending, pending, passed)
        self.assertEqual(self.forge.merge_state("a-first"), "CLEAN")                      # asking for something else plays nothing
        self.assertEqual([self.forge.checks("a-first")[0] for _ in range(5)],
                         ["pending", "pending", "pass", "pass", "pass"])
        self.assertEqual(self.repo.gh_db()["checks"]["sdlc/a-first"], [passed])
        self.assertEqual(self.forge.checks("b-second"), ("none", []))                     # another head: no CI

    def test_a_failure_that_a_fix_turns_green(self):
        self.open_pr()
        self.script("a-first", [check_run("build", conclusion="FAILURE")], [check_run("build")])
        self.assertEqual([self.forge.checks("a-first")[0] for _ in range(3)], ["fail", "pass", "pass"])

    def test_a_scripted_error_fails_one_call_only(self):
        self.open_pr()
        self.script("a-first", {"error": "HTTP 502"}, [check_run("build")])
        self.assertEqual([self.forge.checks("a-first")[0] for _ in range(2)], ["unknown", "pass"])

    def test_pr_view_prints_the_stored_fields_asked_for(self):
        self.open_pr()
        r = self.gh("pr", "view", "1", "--json", "number,state,headRefName,isDraft,mergeStateStatus")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"number": 1, "state": "OPEN", "headRefName": "sdlc/a-first", "isDraft": False,
                                                "mergeStateStatus": "CLEAN"})
        self.assertEqual(json.loads(self.gh("pr", "view", "1", "--json", "statusCheckRollup").stdout), {"statusCheckRollup": []})
        self.assertNotEqual(self.gh("pr", "view", "9", "--json", "number").returncode, 0)  # no such PR
        self.assertNotEqual(self.gh("pr", "view", "1").returncode, 0)                      # always asked for as JSON

    def test_a_db_that_holds_only_the_script_is_enough(self):
        self.repo.gh_db_write({"checks": {"sdlc/a-first": [[check_run("build")]]}})
        self.open_pr()
        self.assertEqual(self.forge.checks("a-first")[0], "pass")


class Head(GitHub):
    """`checks(slug, head)`: what the PR shows for the commit just pushed — and nothing of the commit before it."""

    def tip(self, ref="sdlc/a-first"):
        return self.repo.git("rev-parse", ref)

    def push_commit(self, text="again\n"):
        """One more commit on sdlc/a-first, pushed (the push after a CI fix). The new tip."""
        self.repo.git("checkout", "-q", "sdlc/a-first")
        self.repo.write("feature.txt", text)
        self.repo.git("commit", "-q", "-am", "next")
        self.repo.git("push", "-q", "origin", "sdlc/a-first")
        self.repo.git("checkout", "-q", "main")
        return self.tip()

    def json_fields(self):
        """The `--json` argument of every `pr view` so far."""
        return [c[c.index("--json") + 1] for c in self.repo.gh_db().get("calls", []) if c[:2] == ["pr", "view"]]

    def test_the_checks_of_the_commit_that_was_asked_about_are_the_answer(self):
        self.open_pr()
        self.script("a-first", [check_run("build", conclusion="FAILURE"), check_run("lint")])
        state, found = self.forge.checks("a-first", self.tip())
        self.assertEqual((state, [(c["name"], c["bucket"]) for c in found]), ("fail", [("build", "fail"), ("lint", "pass")]))

    def test_a_pr_that_shows_another_commit_is_unknown(self):
        self.open_pr()
        self.script("a-first", [check_run("build")])
        self.assertEqual(self.forge.checks("a-first", "0" * 40), ("unknown", []))
        self.assertEqual(self.forge.checks("a-first", self.tip())[0], "pass")             # the right one reads as usual

    def test_the_head_and_the_checks_are_read_in_one_view(self):
        self.open_pr()
        self.script("a-first", [check_run("build")])
        self.forge.checks("a-first", self.tip())
        self.assertEqual(self.json_fields(), ["headRefOid,statusCheckRollup"])
        self.forge.checks("a-first")                                                       # without a head: the checks alone, as before
        self.assertEqual(self.json_fields(), ["headRefOid,statusCheckRollup", "statusCheckRollup"])

    def test_without_a_head_the_commit_is_not_compared(self):
        self.open_pr()
        self.script("a-first", [check_run("build")])
        self.assertEqual(self.forge.checks("a-first")[0], "pass")
        self.assertEqual(self.forge.checks("a-first", "")[0], "pass")

    def test_unknown_when_the_view_has_no_head_in_it_or_cannot_be_read(self):
        self.open_pr()
        self.script("a-first", {"raw": json.dumps({"statusCheckRollup": [check_run("build")]})}, [check_run("build")])
        self.assertEqual(self.forge.checks("a-first", self.tip()), ("unknown", []))        # asked for the head, got none
        self.assertEqual(self.forge.checks("a-first", self.tip())[0], "pass")
        self.assertEqual(self.forge.checks("no-such-item", self.tip()), ("unknown", []))   # no open PR
        os.environ["STUB_GH_VIEW_FAIL"] = "1"
        self.assertEqual(self.forge.checks("a-first", self.tip()), ("unknown", []))

    def test_the_stub_reports_the_tip_of_the_head_branch_on_origin(self):
        self.open_pr()
        first = json.loads(self.gh("pr", "view", "1", "--json", "number,headRefOid").stdout)
        self.assertEqual(first, {"number": 1, "headRefOid": self.tip("origin/sdlc/a-first")})
        new = self.push_commit()
        self.assertNotEqual(new, first["headRefOid"])
        self.assertEqual(json.loads(self.gh("pr", "view", "1", "--json", "headRefOid").stdout), {"headRefOid": new})

    def test_a_push_shows_up_at_the_next_view_unless_the_stub_is_told_to_hold_it_back(self):
        self.open_pr()
        self.script("a-first", [check_run("build")])
        self.assertEqual(self.forge.checks("a-first", self.tip())[0], "pass")
        self.assertEqual(self.forge.checks("a-first", self.push_commit())[0], "pass")

    def test_right_after_a_push_the_old_commit_and_its_old_failure_are_not_the_new_ones(self):
        self.open_pr()
        passed = [check_run("build")]
        self.script("a-first", [check_run("build", conclusion="FAILURE")], passed)
        self.db_set(head_lag={"sdlc/a-first": 2})
        self.assertEqual(self.forge.checks("a-first", self.tip())[0], "fail")              # the commit as first pushed
        new = self.push_commit()
        self.assertEqual([self.forge.checks("a-first", new)[0] for _ in range(4)], ["unknown", "unknown", "pass", "pass"])
        self.assertEqual(self.repo.gh_db()["checks"]["sdlc/a-first"], [passed])            # the held-back views played nothing

    def test_the_stub_answers_the_previous_tip_for_as_many_views_as_it_was_told_to(self):
        self.open_pr()
        self.db_set(head_lag={"sdlc/a-first": 3})
        old = json.loads(self.gh("pr", "view", "1", "--json", "headRefOid").stdout)["headRefOid"]
        new = self.push_commit()
        seen = [json.loads(self.gh("pr", "view", "1", "--json", "headRefOid").stdout)["headRefOid"] for _ in range(5)]
        self.assertEqual(seen, [old, old, old, new, new])
        self.assertEqual(json.loads(self.gh("pr", "view", "1", "--json", "number").stdout), {"number": 1})   # others are not delayed

    def test_only_a_view_that_asks_for_the_head_is_counted_and_only_the_listed_head_lags(self):
        self.open_pr()
        self.open_pr("b-second", "other.txt")
        self.db_set(head_lag={"sdlc/a-first": 1})
        self.assertEqual(self.forge.checks("a-first", self.tip())[0], "none")
        self.assertEqual(self.forge.checks("b-second", self.tip("sdlc/b-second"))[0], "none")
        self.push_commit()
        self.assertEqual(self.forge.merge_state("a-first"), "CLEAN")                       # asking for something else: not a view of the head
        self.assertEqual(self.forge.checks("a-first", self.tip())[0], "unknown")           # the one view held back
        self.assertEqual(self.forge.checks("a-first", self.tip())[0], "none")

    def test_a_forge_without_ci_ignores_the_head(self):
        for forge in (LocalForge(Git(self.repo.root), "main"), Forge(Git(self.repo.root), "main")):
            self.assertEqual(forge.checks("a-first", "abc"), ("none", []))


class StubRunView(GitHub):
    def test_a_run_with_no_scripted_log_prints_default_lines_that_name_it_and_end_with_an_error(self):
        r = self.gh("run", "view", "777", "--log-failed")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(any("run 777" in l for l in lines), lines)
        self.assertIn("##[error]", lines[-1])

    def test_a_scripted_log_is_printed_as_it_is(self):
        self.db_set(run_logs={"777": "one\ntwo"})
        r = self.gh("run", "view", "777", "--log-failed")
        self.assertEqual((r.returncode, r.stdout), (0, "one\ntwo\n"))
        self.assertIn("run 778", self.gh("run", "view", "778", "--log-failed").stdout)     # the others keep the default

    def test_an_id_in_run_view_fail_fails_and_the_others_do_not(self):
        self.db_set(run_view_fail=["777", 778])                                            # a string or a number: both are ids
        for rid in ("777", "778"):
            r = self.gh("run", "view", rid, "--log-failed")
            self.assertEqual((r.returncode, r.stdout), (1, ""), rid)
            self.assertTrue(r.stderr.strip())
        self.assertEqual(self.gh("run", "view", "779", "--log-failed").returncode, 0)

    def test_only_the_failed_log_is_supported(self):
        self.assertNotEqual(self.gh("run", "view", "777").returncode, 0)


class Logs(GitHub):
    """check_logs: the tail of each failed Actions run's log, headed with the check's name — and no more than the cap."""

    def tag_logs(self, ids, tags="abc", lines=500):
        """A big log per run id: `a line 001 xxxx…`, 500 of them, some 45 KB each."""
        self.db_set(run_logs={str(i): "\n".join("%s line %03d %s" % (t, n, "x" * 80) for n in range(1, lines + 1))
                              for i, t in zip(ids, tags)})

    def test_the_last_lines_of_an_actions_log_under_a_header_with_the_check_name(self):
        self.db_set(run_logs={"101": "\n".join("step output %03d" % i for i in range(1, 201))})
        text = self.forge.check_logs([failed("build", run_link(101))], lines=5)
        self.assertEqual(text, "== build ==\n" + "\n".join("step output %03d" % i for i in range(196, 201)))
        self.assertEqual(self.run_views(), [["run", "view", "101", "--log-failed"]])

    def test_sixty_lines_by_default(self):
        self.db_set(run_logs={"101": "\n".join("step output %03d" % i for i in range(1, 101))})
        body = self.forge.check_logs([failed("build", run_link(101))]).splitlines()[1:]
        self.assertEqual((len(body), body[0], body[-1]), (60, "step output 041", "step output 100"))

    def test_a_check_without_an_actions_link_is_listed_by_name_and_link_and_costs_no_call(self):
        text = self.forge.check_logs([failed("ci/jenkins", "https://jenkins.example.test/job/7"), failed("ci/other", "")])
        self.assertEqual(text, "ci/jenkins — https://jenkins.example.test/job/7\nci/other")
        self.assertEqual(self.run_views(), [])

    def test_logs_come_first_and_the_checks_without_one_after_them(self):
        self.db_set(run_logs={"101": "boom"})
        text = self.forge.check_logs([failed("ci/jenkins", "https://jenkins.example.test/job/7"), failed("build", run_link(101))])
        self.assertEqual(text, "== build ==\nboom\n\nci/jenkins — https://jenkins.example.test/job/7")

    def test_at_most_three_runs_are_read_and_the_rest_are_listed(self):
        self.db_set(run_logs={str(n): "log of run %d" % n for n in range(101, 106)})
        text = self.forge.check_logs([failed("check-%d" % n, run_link(n)) for n in range(101, 106)])
        self.assertEqual([c[2] for c in self.run_views()], ["101", "102", "103"])
        for n in (101, 102, 103):
            self.assertIn("== check-%d ==\nlog of run %d" % (n, n), text)
        for n in (104, 105):
            self.assertIn("check-%d — %s" % (n, run_link(n)), text)
            self.assertNotIn("log of run %d" % n, text)

    def test_the_jobs_of_one_run_share_one_read_and_one_header(self):
        self.db_set(run_logs={str(n): "log of run %d" % n for n in range(101, 105)})
        text = self.forge.check_logs([failed("build", run_link(101, 1)), failed("lint", run_link(101, 2)),
                                      failed("t2", run_link(102)), failed("t3", run_link(103)), failed("t4", run_link(104))])
        self.assertEqual([c[2] for c in self.run_views()], ["101", "102", "103"])         # three runs, not three checks
        self.assertEqual(text.count("log of run 101"), 1)
        self.assertIn("== build, lint ==\nlog of run 101", text)
        self.assertIn("t4 — " + run_link(104), text)

    def test_the_whole_text_stays_within_the_cap_and_each_log_keeps_its_end(self):
        self.tag_logs((101, 102, 103))
        text = self.forge.check_logs([failed("t%d" % n, run_link(100 + n)) for n in (1, 2, 3)], lines=1000)
        self.assertLessEqual(len(text), CHECK_LOGS_CAP)
        self.assertGreater(len(text), CHECK_LOGS_CAP - 300)                                # the room is used, not wasted
        for n, tag in enumerate("abc", 1):
            self.assertIn("== t%d ==" % n, text)
            self.assertIn("%s line 500 " % tag, text)                                      # the end survived …
            self.assertNotIn("%s line 001 " % tag, text)                                   # … the front went
        for line in text.splitlines():                                                     # and every log starts at a line
            if line and not line.startswith("=="):
                self.assertRegex(line, r"^[abc] line \d{3} x{80}$")

    def test_a_crowd_of_checks_without_logs_does_not_crowd_the_logs_out(self):
        self.tag_logs((101, 102, 103))
        crowd = [failed("other-%03d" % n, "https://ci.example.test/job/%d" % n) for n in range(100)]
        text = self.forge.check_logs([failed("t%d" % n, run_link(100 + n)) for n in (1, 2, 3)] + crowd, lines=1000)
        self.assertLessEqual(len(text), CHECK_LOGS_CAP)
        for tag in "abc":
            self.assertIn("%s line 500 " % tag, text)
        self.assertIn("other-000 — https://ci.example.test/job/0", text)
        self.assertNotIn("other-099", text)
        self.assertRegex(text, r"… and \d+ more$")

    def test_a_run_whose_log_cannot_be_read_is_listed_with_its_link_and_the_others_still_are_read(self):
        self.db_set(run_view_fail=["101"], run_logs={"102": "second log"})
        text = self.forge.check_logs([failed("build", run_link(101)), failed("lint", run_link(102))])
        self.assertEqual(text, "== lint ==\nsecond log\n\nbuild — %s (no log)" % run_link(101))

    def test_a_run_that_cannot_be_read_is_asked_once_however_many_of_its_jobs_failed(self):
        self.db_set(run_view_fail=["101"])
        text = self.forge.check_logs([failed("build", run_link(101, 1)), failed("lint", run_link(101, 2))])
        self.assertEqual(text, "build — %s (no log)\nlint — %s (no log)" % (run_link(101, 1), run_link(101, 2)))
        self.assertEqual(len(self.run_views()), 1)

    def test_an_empty_log_is_no_log(self):
        self.db_set(run_logs={"101": ""})
        self.assertEqual(self.forge.check_logs([failed("build", run_link(101))]), "build — %s (no log)" % run_link(101))

    def test_with_gh_missing_the_checks_are_still_listed(self):
        os.environ["PATH"] = tempfile.mkdtemp(prefix="no-gh-", dir=self.repo.tmp)
        self.assertEqual(self.forge.check_logs([failed("build", run_link(101))]), "build — %s (no log)" % run_link(101))

    def test_nothing_failed_nothing_to_say(self):
        self.assertEqual(self.forge.check_logs([]), "")
        self.assertEqual(self.run_views(), [])


class MergeState(GitHub):
    """merge_state, and the stub's branch that is behind its base: refused until it has been brought up to date."""

    def behind_branch(self):
        """A PR whose branch lacks a commit that main got since — and GitHub says so."""
        self.open_pr()
        self.repo.write("other.txt", "newer\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "main moves on")
        self.repo.git("push", "-q", "origin", "main")
        self.db_set(behind=["sdlc/a-first"])

    def push_to_branch(self, text, merge_main=False):
        self.repo.git("checkout", "-q", "sdlc/a-first")
        if merge_main:
            self.repo.git("merge", "-q", "--no-edit", "origin/main")                      # what the engine does about BEHIND
        else:
            self.repo.write("feature.txt", text)
            self.repo.git("commit", "-q", "-am", "touch up")
        self.repo.git("push", "-q", "origin", "sdlc/a-first")
        self.repo.git("checkout", "-q", "main")

    def test_clean_for_an_open_pr_that_is_not_behind_and_empty_without_one(self):
        self.open_pr()
        self.assertEqual(self.forge.merge_state("a-first"), "CLEAN")
        self.assertEqual(self.forge.merge_state("no-such-item"), "")

    def test_a_branch_that_is_behind_is_refused_until_it_is_brought_up_to_date(self):
        self.behind_branch()
        self.assertEqual(self.forge.merge_state("a-first"), "BEHIND")
        ok, why = self.forge.merge("a-first", "A title")
        self.assertFalse(ok)
        self.assertIn("Head branch is out of date. Review and try the merge again. (mergePullRequest)", why)
        self.assertEqual(self.forge.state("a-first"), "open")
        self.assertEqual(self.forge.merge_state("a-first"), "BEHIND")
        self.assertFalse(self.forge.merge("a-first", "A title")[0])                       # nothing moved: refused again
        self.assertIsNone(self.repo.origin_show("main", "feature.txt"))
        self.push_to_branch("", merge_main=True)
        self.assertEqual(self.forge.merge_state("a-first"), "CLEAN")
        ok, info = self.forge.merge("a-first", "A title")
        self.assertTrue(ok, info)
        self.assertEqual(self.forge.state("a-first"), "merged")
        self.assertEqual(self.repo.origin_show("main", "feature.txt"), "feature\n")
        self.assertEqual(self.repo.origin_show("main", "other.txt"), "newer\n")
        self.assertEqual(self.repo.gh_db()["behind"], [])                                  # the head left the list

    def test_the_refusal_is_githubs_own_message(self):
        self.behind_branch()
        r = self.gh("pr", "merge", "1", "--squash", "--delete-branch")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.strip(), "GraphQL: Head branch is out of date. Review and try the merge again. (mergePullRequest)")

    def test_a_push_before_the_first_refused_merge_does_not_lift_it(self):
        self.behind_branch()
        self.assertEqual(self.forge.merge_state("a-first"), "BEHIND")                     # looking notes nothing
        self.push_to_branch("feature, again\n")                                            # the PR was published again
        self.assertEqual(self.forge.merge_state("a-first"), "BEHIND")
        self.assertFalse(self.forge.merge("a-first", "A title")[0])                       # this refusal notes the tip
        self.assertEqual(self.forge.merge_state("a-first"), "BEHIND")
        self.push_to_branch("feature, once more\n")                                        # a push after it does
        self.assertEqual(self.forge.merge_state("a-first"), "CLEAN")
        ok, info = self.forge.merge("a-first", "A title")
        self.assertTrue(ok, info)

    def test_only_the_listed_head_is_behind(self):
        self.behind_branch()
        self.open_pr("b-second", "second.txt")
        self.assertEqual(self.forge.merge_state("b-second"), "CLEAN")
        ok, info = self.forge.merge("b-second", "B title")
        self.assertTrue(ok, info)
        self.assertEqual(self.forge.merge_state("a-first"), "BEHIND")


class EndOf(unittest.TestCase):
    """The end of a log, cut at a line start: what a fix session reads of a failed run."""

    def test_the_last_characters_from_the_start_of_a_line(self):
        text = "one\ntwo\nthree"
        self.assertEqual(end_of(text, 8), "three")                 # "wo\nthree" would start in the middle of a line
        self.assertEqual(end_of(text, 9), "two\nthree")            # this one starts exactly at a line
        self.assertEqual(end_of(text, 5), "three")
        self.assertEqual(end_of(text, 100), text)                 # short enough: whole
        self.assertEqual(end_of(text, 13), text)

    def test_a_text_without_a_line_break_is_cut_where_it_is(self):
        self.assertEqual(end_of("abcdefghij", 4), "ghij")

    def test_nothing_or_less_than_nothing_is_left_of_nothing(self):
        self.assertEqual(end_of("one\ntwo", 0), "")
        self.assertEqual(end_of("one\ntwo", -5), "")


class Local(RepoCase):
    def test_a_forge_that_has_no_ci_has_no_checks_and_no_merge_state(self):
        git = Git(self.repo.root)
        for forge in (LocalForge(git, "main"), Forge(git, "main")):
            self.assertEqual(forge.checks("a-first"), ("none", []))
            self.assertEqual(forge.check_logs([failed("build", run_link(1))]), "")
            self.assertEqual(forge.merge_state("a-first"), "")
        self.assertEqual(self.repo.gh_db().get("calls", []), [])                           # and gh was never asked

    def test_the_engine_has_nothing_to_wait_for_without_github(self):
        e = self.repo.engine()
        self.assertEqual(e.wait_checks("a-first"), ("pass", []))
        self.assertEqual(e.wait_checks("a-first", interactive=True), ("pass", []))
        self.assertEqual(self.repo.gh_db().get("calls", []), [])
        self.assertFalse(os.path.exists(self.repo.path(".sdlc/state/loop-status.json")))   # and no phase was announced


if __name__ == "__main__":
    unittest.main()
