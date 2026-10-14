"""git as the engine runs it: a command that lost a race for a lock is run again, a failure leaves its reason behind for the
message that reports it, and a fetch that ran out of time is told to stop before it is killed."""
import contextlib
import io
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from helpers import RepoCase

from sdlclib import gitops
from sdlclib.engine import StopRun
from sdlclib.gitops import Git, LocalForge
from sdlclib.util import CmdResult

HINT = "Another git process seems to be running in this repository, or the lock file may be stale"
INDEX_LOCK = "fatal: Unable to create '/r/.git/index.lock': File exists.\n\n" + HINT + "\n"
REF_LOCK = ("fatal: cannot lock ref 'refs/heads/foo': Unable to create '/r/.git/refs/heads/foo.lock': File exists.\n\n"
            + HINT + "\n")
FETCH_RACE = "error: cannot lock ref 'refs/remotes/origin/main': is at 2f1c but expected 9d0e\n"
REF_MOVED = ("fatal: update_ref failed for ref 'refs/heads/a': cannot lock ref 'refs/heads/a': is at 62af71a but expected "
             "5a8b8a2\n")
# cannot lock ref, and no waiting helps: another ref is in the way, or the ref is there already
REF_IN_THE_WAY = "fatal: cannot lock ref 'refs/heads/a/b': 'refs/heads/a' exists; cannot create 'refs/heads/a/b'\n"
REF_EXISTS = "fatal: cannot lock ref 'refs/heads/foo': reference already exists\n"
UPDATE_REF_EXISTS = ("fatal: update_ref failed for ref 'refs/heads/foo': cannot lock ref 'refs/heads/foo': "
                     "reference already exists\n")
INDEX_LOCK_IN_A_HOME_WITH_AN_APOSTROPHE = ("fatal: Unable to create '/Users/o'brien/r/.git/index.lock': File exists.\n\n"
                                           + HINT + "\n")


def failed(err, out="", rc=128):
    return CmdResult(rc, out, err)


def worked(out=""):
    return CmdResult(0, out, "")


class Scripted(unittest.TestCase):
    """A Git whose commands are answered from a script, one answer per run: nothing is executed, and the pauses between
    the tries are recorded instead of slept."""

    def git(self, *answers):
        patch = mock.patch.object(gitops, "run", side_effect=list(answers))
        self.runs = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(gitops, "pause")
        self.pauses = patch.start()
        self.addCleanup(patch.stop)
        return Git("/repo")

    def paused(self):
        return [c.args[0] for c in self.pauses.call_args_list]


class LockRetries(Scripted):
    def test_the_pauses_are_named_in_one_place_one_two_and_four_seconds(self):
        self.assertEqual(gitops.LOCK_RETRY_DELAYS, (1, 2, 4))

    def test_the_pause_sleeps(self):
        with mock.patch.object(gitops.time, "sleep") as sleep:
            gitops.pause(3)
        sleep.assert_called_once_with(3)

    def test_a_command_that_keeps_failing_on_a_lock_is_run_four_times_and_the_failure_is_the_answer(self):
        git = self.git(*[failed(INDEX_LOCK)] * 4)
        r = git("checkout", "-q", "main")
        self.assertFalse(r.ok)
        self.assertEqual((r.rc, r.err), (128, INDEX_LOCK))
        self.assertEqual(self.runs.call_count, 4)
        self.assertEqual(self.paused(), [1, 2, 4])

    def test_a_lock_that_is_gone_by_the_second_try_costs_one_pause_and_the_second_answer_is_the_answer(self):
        git = self.git(failed(INDEX_LOCK), worked("abc\n"))
        r = git("rev-parse", "HEAD")
        self.assertEqual((r.ok, r.out), (True, "abc\n"))
        self.assertEqual(self.runs.call_count, 2)
        self.assertEqual(self.paused(), [1])

    def test_a_lock_gone_by_the_third_and_by_the_fourth_try_waits_one_and_two_then_one_two_and_four(self):
        git = self.git(failed(INDEX_LOCK), failed(INDEX_LOCK), worked())
        self.assertTrue(git("checkout", "-q", "main").ok)
        self.assertEqual(self.paused(), [1, 2])
        git = self.git(failed(INDEX_LOCK), failed(INDEX_LOCK), failed(INDEX_LOCK), worked())
        self.assertTrue(git("checkout", "-q", "main").ok)
        self.assertEqual(self.paused(), [1, 2, 4])

    def test_what_counts_as_a_lock_is_git_s_own_words_for_a_lock_file_or_a_ref_that_moved_in_either_stream(self):
        for name, answer in (("index lock on stderr", failed(INDEX_LOCK)),
                             ("index lock on stdout", failed("", out=INDEX_LOCK)),
                             ("index lock in a path with an apostrophe", failed(INDEX_LOCK_IN_A_HOME_WITH_AN_APOSTROPHE)),
                             ("ref lock", failed(REF_LOCK)),
                             ("a fetch that lost the race for a ref", failed(FETCH_RACE, rc=1)),
                             ("a ref moved under update-ref", failed(REF_MOVED)),
                             ("a ref that moved, on stdout", failed("", out=FETCH_RACE, rc=1)),
                             ("a lock inside a longer message", failed("error: x\n" + INDEX_LOCK + "error: y\n"))):
            with self.subTest(name):
                git = self.git(answer, worked())
                self.assertTrue(git("fetch").ok)
                self.assertEqual(self.paused(), [1])

    def test_a_failure_that_is_not_a_lock_is_not_run_again(self):
        for name, answer in (("not a repository", failed("fatal: not a git repository (or any of the parent directories): .git")),
                             ("a pathspec", failed("error: pathspec 'x' did not match any file(s) known to git", rc=1)),
                             ("a conflict", failed("", out="CONFLICT (content): Merge conflict in yarn.lock\n", rc=1)),
                             ("a timeout", CmdResult(124, "", "timed out after 300s")),
                             ("no git", CmdResult(127, "", "[Errno 2] No such file or directory: 'git'"))):
            with self.subTest(name):
                git = self.git(*[answer] * 4)
                self.assertFalse(git("checkout", "x").ok)
                self.assertEqual(self.runs.call_count, 1)
                self.pauses.assert_not_called()

    def test_ordinary_output_is_not_a_lock_whatever_words_it_shares_with_git(self):
        """The commit the engine makes runs the repository's hooks, and a hook that runs tests says `got 1 but expected 2`."""
        for name, answer in (("a failed assertion in a hook",
                              failed("husky - pre-commit hook exited with code 1 (error)\nAssertionError: got 1 but expected 2\n", rc=1)),
                             ("the same on stdout", failed("", out="AssertionError: got 1 but expected 2\n", rc=1)),
                             ("a test runner", failed("", out="FAIL test_sum: the cursor is at 3 but expected 4\n", rc=1)),
                             ("git's sentence about something that is no commit",
                              failed("cannot lock ref 'cache': is at warm but expected cold\n", rc=1)),
                             ("a tool that names a lock file", failed("ln: failed to create symbolic link 'yarn.lock': File exists\n", rc=1)),
                             ("a directory used as a lock", failed("mkdir: cannot create directory 'build.lock': File exists\n", rc=1))):
            with self.subTest(name):
                git = self.git(*[answer] * 4)
                self.assertFalse(git("commit", "-q", "-m", "x").ok)
                self.assertEqual(self.runs.call_count, 1)
                self.pauses.assert_not_called()

    def test_a_ref_that_cannot_be_locked_for_good_fails_at_once_with_no_pause(self):
        """`cannot lock ref` opens the failures a wait cures and the ones it never does: another ref in the way (a branch
        `a` and a branch `a/b`) and a ref that is there already."""
        for name, answer in (("a ref in the way", failed(REF_IN_THE_WAY)),
                             ("a ref that exists", failed(REF_EXISTS)),
                             ("a ref that exists, as update-ref says it", failed(UPDATE_REF_EXISTS)),
                             ("the same on stdout", failed("", out=REF_IN_THE_WAY)),
                             ("a fetch that cannot update a ref for good",
                              failed("error: cannot lock ref 'refs/remotes/origin/a/b': 'refs/remotes/origin/a' exists; "
                                     "cannot create 'refs/remotes/origin/a/b'\n", rc=1))):
            with self.subTest(name):
                git = self.git(*[answer] * 4)
                r = git("branch", "a/b")
                self.assertFalse(r.ok)
                self.assertEqual(self.runs.call_count, 1)
                self.pauses.assert_not_called()
                self.assertTrue(git.last_error.startswith(("fatal: ", "error: ")) and "cannot lock ref" in git.last_error)

    def test_ok_and_out_retry_like_a_plain_call(self):
        git = self.git(failed(INDEX_LOCK), worked())
        self.assertTrue(git.ok("checkout", "-q", "main"))
        self.assertEqual(self.paused(), [1])
        git = self.git(failed(REF_LOCK), worked("abc123\n"))
        self.assertEqual(git.out("rev-parse", "HEAD"), "abc123")
        self.assertEqual(self.paused(), [1])

    def test_every_try_is_the_same_command_with_the_same_timeout_and_directory(self):
        git = self.git(failed(INDEX_LOCK), failed(INDEX_LOCK), worked())
        git("pull", "-q", "--ff-only", timeout=120, cwd="/elsewhere")
        self.assertEqual(self.runs.call_args_list, [mock.call(["git", "-C", "/elsewhere", "pull", "-q", "--ff-only"], timeout=120)] * 3)

    def test_a_command_that_works_costs_no_pause(self):
        git = self.git(worked("main\n"))
        self.assertEqual(git.out("branch", "--show-current"), "main")
        self.pauses.assert_not_called()


class LastError(Scripted):
    def test_there_is_none_before_a_command_has_failed(self):
        self.assertEqual(Git("/repo").last_error, "")

    def test_it_is_the_first_fatal_or_error_line_on_stderr(self):
        git = self.git(failed("hint: Diverging branches can't be fast-forwarded, you need to either:\nhint:\n"
                              "fatal: Not possible to fast-forward, aborting.\n"),
                       failed("warning: one\nerror: first\nfatal: second\nerror: third\nAborting\n"))
        git("pull", "-q", "--ff-only")
        self.assertEqual(git.last_error, "fatal: Not possible to fast-forward, aborting.")
        git("checkout", "x")
        self.assertEqual(git.last_error, "error: first")

    def test_a_lock_failure_names_the_lock_not_git_s_advice_after_it(self):
        git = self.git(*[failed(INDEX_LOCK)] * 4, *[failed(REF_LOCK)] * 4)
        git("checkout", "-q", "main")
        self.assertEqual(git.last_error, "fatal: Unable to create '/r/.git/index.lock': File exists.")
        self.assertNotIn("Another git process", git.last_error)
        git("branch", "foo")
        self.assertEqual(git.last_error, "fatal: cannot lock ref 'refs/heads/foo': Unable to create "
                                         "'/r/.git/refs/heads/foo.lock': File exists.")

    def test_stderr_is_read_before_stdout_for_the_line_that_says_why(self):
        git = self.git(failed("error: from stderr\n", out="fatal: from stdout\n"))
        git("merge", "x")
        self.assertEqual(git.last_error, "error: from stderr")

    def test_stdout_has_the_line_when_stderr_has_none(self):
        git = self.git(failed("stderr line\nstderr last\n", out="CONFLICT in a.txt\nfatal: from stdout\nAutomatic merge failed\n"))
        git("merge", "x")
        self.assertEqual(git.last_error, "fatal: from stdout")

    def test_with_no_such_line_it_is_the_last_one_stderr_first_and_stdout_when_stderr_said_nothing(self):
        git = self.git(failed("stderr one\nstderr last\n", out="stdout line\n"),
                       failed("", out="CONFLICT in a.txt\nAutomatic merge failed\n"),
                       failed("  one  \n  two  \n\n   \n"))
        git("merge", "x")
        self.assertEqual(git.last_error, "stderr last")
        git("merge", "y")
        self.assertEqual(git.last_error, "Automatic merge failed")
        git("merge", "z")
        self.assertEqual(git.last_error, "two")

    def test_only_a_line_that_starts_with_the_word_is_the_one(self):
        git = self.git(failed("remote: error: rejected\nthe error: is not first\nfatal-ish thing\nlast words\n"))
        git("push")
        self.assertEqual(git.last_error, "last words")

    def test_blank_lines_and_padding_do_not_count(self):
        git = self.git(failed("  fatal: no good  \n\n   \n  advice  \n\n"), failed("  one  \n\n   \n"))
        git("checkout", "x")
        self.assertEqual(git.last_error, "fatal: no good")
        git("checkout", "y")
        self.assertEqual(git.last_error, "one")

    def test_a_failure_that_said_nothing_leaves_nothing(self):
        git = self.git(failed("fatal: first\n"), failed("", rc=1))
        git("checkout", "x")
        self.assertEqual(git.last_error, "fatal: first")
        git("show-ref", "--verify", "-q", "refs/heads/x")
        self.assertEqual(git.last_error, "")

    def test_a_command_that_works_clears_it(self):
        git = self.git(failed("fatal: first\n"), worked("ok\n"))
        git("checkout", "x")
        self.assertEqual(git.last_error, "fatal: first")
        git("rev-parse", "HEAD")
        self.assertEqual(git.last_error, "")

    def test_a_lock_that_never_lets_go_leaves_what_the_last_try_said(self):
        git = self.git(*[failed("fatal: Unable to create '/r/.git/try-%d.lock': File exists.\n\n%s\n" % (n, HINT)) for n in range(4)])
        git("checkout", "-q", "main")
        self.assertEqual(git.last_error, "fatal: Unable to create '/r/.git/try-3.lock': File exists.")
        self.assertEqual(self.runs.call_count, 4)

    def test_a_timeout_and_a_missing_git_leave_their_own_words(self):
        git = self.git(CmdResult(124, "", "timed out after 120s"), CmdResult(127, "", "[Errno 2] No such file or directory: 'git'"))
        git("fetch", timeout=120)
        self.assertEqual(git.last_error, "timed out after 120s")
        git("fetch")
        self.assertEqual(git.last_error, "[Errno 2] No such file or directory: 'git'")

    def test_with_reason_appends_it_and_without_one_leaves_the_message_as_it_is(self):
        git = self.git(failed("fatal: Not possible to fast-forward, aborting.\n"), failed("", rc=1))
        self.assertEqual(git.with_reason("cannot go"), "cannot go")
        git("pull")
        self.assertEqual(git.with_reason("cannot go"), "cannot go: fatal: Not possible to fast-forward, aborting.")
        git("show-ref", "-q", "x")
        self.assertEqual(git.with_reason("cannot go"), "cannot go")


def first_line(text):
    """What git's stderr opens with — in the failures below that is the `fatal:` or `error:` line that says why."""
    return next(l.strip() for l in text.splitlines() if l.strip())


class RealLocks(RepoCase):
    """The same, against a real git: the lock files are the ones git itself creates."""

    def hold(self, rel):
        """A lock file somebody else holds, until `release` takes it away."""
        self.lock = self.repo.path(rel)
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        open(self.lock, "w").close()
        self.addCleanup(self.release)

    def release(self, *_):
        if os.path.exists(self.lock):
            os.remove(self.lock)

    def test_a_lock_released_during_the_first_pause_is_no_failure_at_all(self):
        self.hold(".git/index.lock")
        git = Git(self.repo.root)
        with mock.patch.object(gitops, "pause", side_effect=self.release) as pause:
            r = git("checkout", "-q", "main")
        self.assertTrue(r.ok, r.err)
        self.assertEqual(pause.call_args_list, [mock.call(1)])
        self.assertEqual(git.last_error, "")

    def test_a_lock_that_stays_fails_after_the_three_pauses_and_names_the_lock_not_git_s_advice(self):
        self.hold(".git/index.lock")
        git = Git(self.repo.root)
        with mock.patch.object(gitops, "pause") as pause:
            r = git("checkout", "-q", "main")
        self.assertFalse(r.ok)
        self.assertEqual([c.args[0] for c in pause.call_args_list], [1, 2, 4])
        self.assertIn("Another git process seems to be running", r.err)             # git says that as well, after the line below
        self.assertTrue(git.last_error.startswith("fatal: Unable to create '"), git.last_error)
        self.assertTrue(git.last_error.endswith("index.lock': File exists."), git.last_error)
        self.assertEqual(git.last_error, first_line(r.err))

    def test_a_ref_lock_is_waited_for_like_the_index_lock(self):
        self.hold(".git/refs/heads/foo.lock")
        git = Git(self.repo.root)
        with mock.patch.object(gitops, "pause", side_effect=self.release) as pause:
            self.assertTrue(git.ok("branch", "foo"))
        self.assertEqual(pause.call_count, 1)
        self.assertTrue(git.branch_exists("foo"))

    def test_a_hook_that_fails_saying_but_expected_runs_once_and_is_not_waited_for(self):
        runs = os.path.join(self.repo.tmp, "hook-runs")
        hook = self.repo.write(".git/hooks/pre-commit", "#!/bin/sh\necho run >> '%s'\n"
                               "echo 'AssertionError: got 1 but expected 2' >&2\nexit 1\n" % runs)
        os.chmod(hook, 0o755)
        git = Git(self.repo.root)
        with mock.patch.object(gitops, "pause") as pause:
            r = git("commit", "-q", "--allow-empty", "-m", "x")
        self.assertFalse(r.ok)
        pause.assert_not_called()
        with open(runs) as fh:
            self.assertEqual(fh.read().split(), ["run"])
        self.assertEqual(git.last_error, "AssertionError: got 1 but expected 2")

    def test_a_branch_that_cannot_exist_beside_another_fails_at_once_with_no_pause(self):
        self.repo.git("branch", "a")
        git = Git(self.repo.root)
        with mock.patch.object(gitops, "pause") as pause:
            r = git("branch", "a/b")
        self.assertFalse(r.ok)
        pause.assert_not_called()
        self.assertTrue(git.last_error.startswith("fatal: cannot lock ref 'refs/heads/a/b'"), git.last_error)
        self.assertIn("exists; cannot create", git.last_error)

    def test_a_ref_that_is_there_already_fails_at_once_with_no_pause(self):
        self.repo.git("branch", "foo")
        git = Git(self.repo.root)
        with mock.patch.object(gitops, "pause") as pause:
            r = git("update-ref", "refs/heads/foo", git.head(), "0" * 40)               # create it, only if there is none
        self.assertFalse(r.ok)
        pause.assert_not_called()
        self.assertIn("cannot lock ref 'refs/heads/foo': reference already exists", git.last_error)

    def test_a_ref_that_moved_under_the_command_is_waited_for_like_a_lock_file(self):
        """The words a fetch gets when another process moved a ref it was about to update. Here the ref stays where it is, so
        every try fails the same way and the last answer is the one reported."""
        self.repo.git("branch", "foo")
        self.repo.git("commit", "-q", "--allow-empty", "-m", "two")
        git = Git(self.repo.root)
        with mock.patch.object(gitops, "pause") as pause:
            r = git("update-ref", "refs/heads/foo", git.head(), git.head())              # foo is at the commit before this one
        self.assertFalse(r.ok)
        self.assertEqual([c.args[0] for c in pause.call_args_list], [1, 2, 4])
        self.assertIn("cannot lock ref 'refs/heads/foo': is at ", git.last_error)
        self.assertIn(" but expected ", git.last_error)

    def test_the_loop_gets_to_the_default_branch_when_the_lock_is_released_during_a_pause(self):
        e = self.repo.engine()
        self.hold(".git/index.lock")
        with mock.patch.object(gitops, "pause", side_effect=self.release) as pause:
            e.to_default()
        self.assertEqual(pause.call_count, 1)
        self.assertEqual(e.git.branch(), "main")

    def test_a_lock_that_stays_stops_the_run_and_says_what_git_said(self):
        e = self.repo.engine()
        self.hold(".git/index.lock")
        with mock.patch.object(gitops, "pause"):
            expect = first_line(Git(self.repo.root)("checkout", "-q", "--detach", "main").err)
            with self.assertRaises(StopRun) as cm:
                e.to_default()
        self.assertEqual(str(cm.exception), "cannot check out or fast-forward main: " + expect)
        self.assertTrue(expect.startswith("fatal: Unable to create '") and expect.endswith("index.lock': File exists."), expect)
        self.assertEqual(cm.exception.code, 2)

    def test_a_failure_git_gave_no_reason_for_keeps_the_message_it_always_had(self):
        e = self.repo.engine()
        with mock.patch.object(Git, "to_default", return_value=False):
            with self.assertRaises(StopRun) as cm:
                e.to_default()
        self.assertEqual(str(cm.exception), "cannot check out or fast-forward main")

    def test_a_branch_that_cannot_be_returned_to_says_why(self):
        e = self.repo.engine()
        expect = first_line(Git(self.repo.root)("checkout", "-q", "sdlc/nope").err)
        with self.assertRaises(StopRun) as cm:
            e.ensure_branch("nope")
        self.assertEqual(str(cm.exception), "cannot return to sdlc/nope after the session: " + expect)
        self.assertIn("sdlc/nope", expect)

    def test_a_run_that_cannot_leave_an_item_branch_says_why_on_its_last_line(self):
        self.repo.git("checkout", "-q", "-b", "sdlc/a-first")
        e = self.repo.engine()
        e.default = "gone"
        said = []
        e.say = said.append
        expect = first_line(Git(self.repo.root)("checkout", "-q", "--detach", "gone").err)
        e.return_to_default()
        self.assertEqual(said, ["LEFT    still on sdlc/a-first: could not check out gone: " + expect])

    def test_the_local_merge_that_cannot_check_out_the_default_branch_says_why(self):
        git = Git(self.repo.root)
        expect = first_line(git("checkout", "-q", "gone").err)
        ok, info = LocalForge(git, "gone").merge("a-first", "A first")
        self.assertFalse(ok)
        self.assertEqual(info, "cannot check out gone: " + expect)


class DivergedMain(RepoCase):
    remote = True

    def test_a_loop_that_cannot_fast_forward_stops_with_git_s_words_in_the_log(self):
        other = os.path.join(self.repo.tmp, "other")
        subprocess.run(["git", "clone", "-q", self.repo.origin, other], check=True, env=self.repo.env(), capture_output=True)
        with open(os.path.join(other, "elsewhere.txt"), "w") as fh:
            fh.write("x\n")
        for args in (["add", "-A"], ["commit", "-q", "-m", "elsewhere"], ["push", "-q", "origin", "main"]):
            subprocess.run(["git", "-C", other] + args, check=True, env=self.repo.env(), capture_output=True)
        self.repo.write("local.txt", "y\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "local")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.repo.engine().run()
        self.assertEqual(code, 2)
        self.assertIn("stop=cannot check out or fast-forward main: fatal: Not possible to fast-forward, aborting.", out.getvalue())


class FakeProcess:
    """The fetch `refresh` starts, as far as it looks at it: it never finishes by itself, and dies of the signals it
    is given as `dies_on` — SIGKILL always."""

    def __init__(self, dies_on=()):
        self.pid = 4242
        self.dies_on = set(dies_on) | {signal.SIGKILL}
        self.returncode = None
        self.waits = []
        self.direct = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("git", timeout)
        return self.returncode

    def receive(self, sig):
        if sig in self.dies_on:
            self.returncode = -int(sig)

    def terminate(self):
        self.direct.append("terminate")
        self.receive(signal.SIGTERM)

    def kill(self):
        self.direct.append("kill")
        self.receive(signal.SIGKILL)


class RefreshStops(unittest.TestCase):
    """A fetch that outlives its time is sent SIGTERM — git removes its lock files on it — and SIGKILL only when it is
    still there TERM_GRACE seconds later."""

    def refresh(self, proc, killpg=None):
        """Git.refresh(10) with `proc` as the fetch; the signals it is sent, as (pid, signal), are returned with its result."""
        sent = []

        def send(pid, sig):
            sent.append((pid, sig))
            if killpg is not None:
                raise killpg
            proc.receive(sig)
        with mock.patch.object(Git, "has_remote", return_value=True), \
                mock.patch.object(gitops.subprocess, "Popen", return_value=proc), \
                mock.patch.object(gitops.os, "killpg", side_effect=send), \
                mock.patch.object(gitops, "pause") as pause:
            result = Git("/repo").refresh(10)
        pause.assert_not_called()
        return result, sent

    def test_a_fetch_that_stops_when_told_is_not_killed(self):
        proc = FakeProcess(dies_on=[signal.SIGTERM])
        result, sent = self.refresh(proc)
        self.assertFalse(result)
        self.assertEqual(sent, [(4242, signal.SIGTERM)])
        self.assertEqual(proc.waits, [10, gitops.TERM_GRACE])

    def test_a_fetch_that_ignores_it_is_killed_after_the_grace(self):
        proc = FakeProcess()
        result, sent = self.refresh(proc)
        self.assertFalse(result)
        self.assertEqual(sent, [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])
        self.assertEqual(proc.waits, [10, gitops.TERM_GRACE, None])

    def test_the_grace_is_two_seconds(self):
        self.assertEqual(gitops.TERM_GRACE, 2.0)

    def test_a_process_group_that_cannot_be_signalled_is_signalled_by_itself(self):
        proc = FakeProcess(dies_on=[signal.SIGTERM])
        result, sent = self.refresh(proc, killpg=ProcessLookupError())
        self.assertFalse(result)
        self.assertEqual((sent, proc.direct), ([(4242, signal.SIGTERM)], ["terminate"]))
        proc = FakeProcess()
        result, sent = self.refresh(proc, killpg=PermissionError())
        self.assertEqual((sent, proc.direct), ([(4242, signal.SIGTERM), (4242, signal.SIGKILL)], ["terminate", "kill"]))
        self.assertEqual(proc.waits, [10, gitops.TERM_GRACE, None])

    def test_a_fetch_that_finishes_in_time_is_not_signalled(self):
        for code, want in ((0, True), (1, False)):
            proc = FakeProcess()
            proc.returncode = code
            result, sent = self.refresh(proc)
            self.assertEqual((result, sent, proc.waits), (want, [], [10]))


FAKE_GIT = """#!/bin/sh
case "$*" in *"remote get-url"*) exit 0;; esac
trap '%s' TERM
: > "$FAKE_LOCK"
sleep 30 &
echo "$$ $!" >> "$FAKE_PIDS"
wait $!
"""


class RefreshStopsARealProcess(unittest.TestCase):
    """The same with a `git` that holds a lock file while it hangs: the way it is stopped decides whether the lock stays."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sdlc-refresh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lock = os.path.join(self.tmp, "index.lock")
        self.pids = os.path.join(self.tmp, "pids")

    def fetch_that(self, on_term):
        """Run Git.refresh against a fake git whose fetch hangs and answers SIGTERM with `on_term` (shell)."""
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "git"), "w") as fh:
            fh.write(FAKE_GIT % on_term)
        os.chmod(os.path.join(bindir, "git"), 0o755)
        env = {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""), "FAKE_LOCK": self.lock, "FAKE_PIDS": self.pids}
        with mock.patch.dict(os.environ, env):
            began = time.time()
            result = Git(self.tmp).refresh(1.0)
            return result, time.time() - began

    def started(self):
        with open(self.pids) as fh:
            return [int(n) for line in fh for n in line.split()]

    def assert_gone(self, pids):
        def alive(pid):
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            return True
        deadline = time.time() + 3
        while time.time() < deadline and any(alive(n) for n in pids):
            time.sleep(0.05)
        self.assertEqual([n for n in pids if alive(n)], [])

    def test_a_fetch_that_cleans_up_on_sigterm_leaves_no_lock_and_nothing_running(self):
        result, took = self.fetch_that("rm -f \"$FAKE_LOCK\"; exit 143")
        self.assertFalse(result)
        self.assertFalse(os.path.exists(self.lock))
        self.assertLess(took, 1.0 + gitops.TERM_GRACE - 0.5)                    # the grace was not waited out
        self.assertTrue(self.started())
        self.assert_gone(self.started())

    def test_a_fetch_that_ignores_sigterm_is_killed_after_the_grace_and_its_lock_is_the_price(self):
        with mock.patch.object(gitops, "TERM_GRACE", 0.3):
            result, took = self.fetch_that("")
        self.assertFalse(result)
        self.assertGreaterEqual(took, 1.25)                                      # the timeout, and then the grace
        self.assertTrue(os.path.exists(self.lock))
        self.assertTrue(self.started())
        self.assert_gone(self.started())


if __name__ == "__main__":
    unittest.main()
