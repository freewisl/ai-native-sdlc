"""The plumbing under the engine: paths git quotes, a push that must find the branch where it was left, the item's PR among
others, an approval after a comment, a network that blinks, pid numbers that are reused, claims in worktrees and in separate
clones, commands that start processes of their own, and globs."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from helpers import RepoCase, SCRIPTS

from sdlclib import gitops
from sdlclib import state as st
from sdlclib import util as U
from sdlclib.gitops import Git, GitHubForge, LocalForge, STALE_PUSH, make_forge
from sdlclib.util import CmdResult

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def wait_gone(pid, seconds=5.0):
    """True once no process has this pid (a zombie waiting to be reaped is not a process that is running)."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        if not r.stdout.strip() or r.stdout.strip().startswith("Z"):
            return True
        time.sleep(0.05)
    return False


class WithClones(RepoCase):
    """A repository with an origin, and helpers to make other clones of it and to look at what origin holds."""
    remote = True

    def clone(self, name):
        path = os.path.join(self.repo.tmp, name)
        subprocess.run(["git", "clone", "-q", self.repo.origin, path], check=True, capture_output=True, env=self.repo.env())
        return path

    def sh(self, path, *args):
        r = subprocess.run(["git", "-C", path] + list(args), capture_output=True, text=True, env=self.repo.env())
        if r.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), r.stderr))
        return r.stdout.strip()

    def origin_git(self, *args):
        r = subprocess.run(["git", "--git-dir", self.repo.origin] + list(args), capture_output=True, text=True,
                           env=self.repo.env())
        return r.stdout.strip() if r.returncode == 0 else None

    def commit_file(self, path, rel, text, message=None):
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.sh(path, "add", "-A")
        self.sh(path, "commit", "-q", "-m", message or text)
        return self.sh(path, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------------------------------------------------
class ChangedPaths(RepoCase):
    NAMES = ["café.txt", "über/dir/naïve.py", "with space.txt", 'quote"d.txt', "tab\there.txt", "new\nline.txt", "日本語.md"]

    def branch_with(self, **files):
        self.repo.git("checkout", "-q", "-b", "sdlc/x")
        for rel, text in files.items():
            if text is None:
                self.repo.git("rm", "-q", rel)
            else:
                self.repo.write(rel, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "work")

    def test_a_path_git_would_quote_comes_back_as_it_is_for_every_kind_of_change(self):
        for name in self.NAMES:
            self.repo.write(name, "old\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "files")
        self.repo.git("checkout", "-q", "-b", "sdlc/x")
        for name in self.NAMES[:3]:
            self.repo.write(name, "changed\n")
        for name in self.NAMES[3:5]:
            self.repo.git("rm", "-q", "--", name)
        for name in ("naïve-new.txt", "a b/ü.txt"):
            self.repo.write(name, "fresh\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "work")
        got = sorted(Git(self.repo.root).changed("main", "sdlc/x"))
        want = sorted([("M", n) for n in self.NAMES[:3]] + [("D", n) for n in self.NAMES[3:5]]
                      + [("A", "naïve-new.txt"), ("A", "a b/ü.txt")])
        self.assertEqual(got, want)
        # git itself quotes them without -z: the old way of reading could not have matched any of these
        plain = self.repo.git("diff", "--name-status", "main...sdlc/x")
        self.assertIn('"caf\\303\\251.txt"', plain)

    def test_a_rename_is_the_new_path_as_added_and_the_old_one_as_deleted(self):
        self.repo.write("tests/café_old.py", "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n    assert 3 == 3\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "t")
        self.repo.git("checkout", "-q", "-b", "sdlc/x")
        self.repo.git("mv", "tests/café_old.py", "tests/über_new.py")
        self.repo.git("commit", "-q", "-m", "rename")
        self.assertEqual(sorted(Git(self.repo.root).changed("main", "sdlc/x")),
                         [("A", "tests/über_new.py"), ("D", "tests/café_old.py")])

    def test_plain_ascii_paths_are_what_they_always_were(self):
        self.branch_with(**{"src/a.py": "x = 1\n", "docs/b.md": "b\n"})
        self.assertEqual(sorted(Git(self.repo.root).changed("main", "sdlc/x")), [("A", "docs/b.md"), ("A", "src/a.py")])

    def test_it_measures_from_the_merge_base_not_from_the_tip_of_base(self):
        self.branch_with(**{"src/a.py": "x = 1\n"})
        self.repo.git("checkout", "-q", "main")
        self.repo.write("later.txt", "main moved on\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "main moves")
        self.assertEqual(Git(self.repo.root).changed("main", "sdlc/x"), [("A", "src/a.py")])

    def test_nothing_changed_is_an_empty_list(self):
        self.repo.git("branch", "sdlc/x")
        self.assertEqual(Git(self.repo.root).changed("main", "sdlc/x"), [])

    def test_a_git_failure_raises_with_git_s_reason_instead_of_saying_nothing_changed(self):
        git = Git(self.repo.root)
        with self.assertRaises(RuntimeError) as cm:
            git.changed("main", "no-such-branch")
        self.assertIn("no-such-branch", str(cm.exception))
        self.assertTrue(str(cm.exception).startswith(("fatal:", "error:")), str(cm.exception))

    def test_a_directory_that_is_no_repository_raises_too(self):
        outside = tempfile.mkdtemp(prefix="sdlc-norepo-")
        self.addCleanup(os.rmdir, outside)
        with self.assertRaises(RuntimeError):
            Git(outside).changed("main", "HEAD")

    def test_output_that_is_cut_off_raises(self):
        git = Git(self.repo.root)
        for out in ("R100\0only-one-path\0", "M\0", "\0a\0"):
            with self.subTest(out=out), mock.patch.object(gitops, "run", return_value=CmdResult(0, out, "")):
                with self.assertRaises(RuntimeError):
                    git.changed("a", "b")

    def test_untracked_lists_every_new_file_whatever_its_name(self):
        names = ["café.txt", "dir with space/ü.txt", "new\nline.txt", "plain.txt"]
        for n in names:
            self.repo.write(n, "x\n")
        self.repo.write("__pycache__/m.pyc", "x")           # ignored by the .gitignore of the test repository
        self.assertEqual(sorted(Git(self.repo.root).untracked()), sorted(names))

    def test_untracked_in_another_directory_and_when_there_is_none(self):
        git = Git(self.repo.root)
        self.assertEqual(git.untracked(), [])
        self.repo.git("worktree", "add", "-q", "-b", "other", os.path.join(self.repo.tmp, "wt"))
        with open(os.path.join(self.repo.tmp, "wt", "ü.txt"), "w") as fh:
            fh.write("x")
        self.assertEqual(git.untracked(), [])
        self.assertEqual(git.untracked(cwd=os.path.join(self.repo.tmp, "wt")), ["ü.txt"])

    def test_the_fingerprint_of_the_tree_sees_a_new_file_with_a_quoted_name_change(self):
        git = Git(self.repo.root)
        self.repo.write("café/ü.txt", "one\n")
        first = git.diff_hash()
        self.assertEqual(git.diff_hash(), first)
        self.repo.write("café/ü.txt", "two\n")
        self.assertNotEqual(git.diff_hash(), first)

    def test_a_worktree_in_a_directory_with_a_non_ascii_name_is_found(self):
        wt = os.path.join(self.repo.tmp, "wörk tree")
        self.repo.git("worktree", "add", "-q", "-b", "sdlc/x", wt)
        found = Git(self.repo.root).worktree_of("sdlc/x")
        self.assertEqual(os.path.realpath(found), os.path.realpath(wt))

    def test_a_worktree_whose_path_has_a_newline_is_found_whole(self):
        wt = os.path.join(self.repo.tmp, "wt\nline")
        self.repo.git("worktree", "add", "-q", "-b", "sdlc/x", wt)
        self.assertEqual(os.path.realpath(Git(self.repo.root).worktree_of("sdlc/x")), os.path.realpath(wt))
        self.assertIsNone(Git(self.repo.root).worktree_of("sdlc/none"))

    def test_an_older_git_without_dash_z_is_read_line_by_line(self):
        wt = os.path.join(self.repo.tmp, "wt")
        self.repo.git("worktree", "add", "-q", "-b", "sdlc/x", wt)
        real = gitops.run

        def no_dash_z(cmd, **kw):
            if cmd[-1] == "-z" and "worktree" in cmd:
                return CmdResult(129, "", "error: unknown switch `z'")
            return real(cmd, **kw)
        with mock.patch.object(gitops, "run", side_effect=no_dash_z):
            self.assertEqual(os.path.realpath(Git(self.repo.root).worktree_of("sdlc/x")), os.path.realpath(wt))


# ---------------------------------------------------------------------------------------------------------------------
class LeaseOnPublish(WithClones):
    def forge(self):
        return GitHubForge(Git(self.repo.root), "main")

    def item(self, text="one"):
        self.repo.git("checkout", "-q", "-b", "sdlc/x")
        return self.repo_commit(text)

    def repo_commit(self, text):
        self.repo.write("f.txt", text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", text)
        return self.repo.git("rev-parse", "HEAD")

    def person_pushes(self, text="person's fix"):
        """Somebody else adds a commit to sdlc/x on origin; returns the new tip."""
        other = self.clone("person")
        self.sh(other, "checkout", "-q", "-b", "sdlc/x", "origin/sdlc/x")
        tip = self.commit_file(other, "person.txt", text)
        self.sh(other, "push", "-q", "origin", "sdlc/x")
        return tip

    def push_args(self, **kw):
        """The command of the push publish makes, with `run` scripted."""
        answers = [CmdResult(0, "", ""), CmdResult(0, "[]", ""), CmdResult(0, "https://github.test/o/r/pull/1\n", "")]
        with mock.patch.object(gitops, "run", side_effect=answers) as run:
            GitHubForge(Git("/repo"), "main").publish("x", "t", "b", **kw)
        return run.call_args_list[0].args[0]

    def test_the_push_names_the_lease_the_caller_asked_for(self):
        self.assertEqual(self.push_args()[3:], ["push", "-q", "--no-verify", "-u", "origin", "sdlc/x", "--force-with-lease"])
        self.assertEqual(self.push_args(expect=None)[-1], "--force-with-lease")
        self.assertEqual(self.push_args(expect="")[-1], "--force-with-lease=refs/heads/sdlc/x:")
        self.assertEqual(self.push_args(expect="abc123")[-1], "--force-with-lease=refs/heads/sdlc/x:abc123")
        self.assertIn("--no-verify", self.push_args(expect="abc123"))
        self.assertEqual(self.push_args(draft=True, expect="abc123")[3], "push")

    def test_expect_empty_creates_the_branch_and_then_refuses_to_push_over_it(self):
        self.item("one")
        forge = self.forge()
        ok, info = forge.publish("x", "T", "body", expect="")
        self.assertTrue(ok, info)
        self.assertEqual(info, "https://github.test/o/r/pull/1")
        self.assertEqual(self.origin_git("rev-parse", "sdlc/x"), self.repo.git("rev-parse", "HEAD"))
        self.repo_commit("two")
        ok, info = forge.publish("x", "T", "body", expect="")
        self.assertFalse(ok)
        self.assertTrue(info.startswith(STALE_PUSH), info)
        self.assertIn("stale info", info)

    def test_expect_a_sha_pushes_while_origin_is_there_and_refuses_when_somebody_pushed(self):
        first = self.item("one")
        forge = self.forge()
        self.assertTrue(forge.publish("x", "T", "b", expect="")[0])
        second = self.repo_commit("two")
        ok, info = forge.publish("x", "T", "b", expect=first)
        self.assertTrue(ok, info)
        self.assertEqual(self.origin_git("rev-parse", "sdlc/x"), second)
        theirs = self.person_pushes()
        third = self.repo_commit("three")
        ok, info = forge.publish("x", "T", "b", expect=second)
        self.assertFalse(ok)
        self.assertTrue(info.startswith(STALE_PUSH + ": "), info)
        self.assertEqual(self.origin_git("rev-parse", "sdlc/x"), theirs)              # the person's work is still there
        self.assertNotEqual(third, theirs)

    def test_a_fetch_in_between_does_not_hide_the_change_from_an_exact_lease(self):
        """What the bare --force-with-lease cannot do: after any fetch the lease is origin's tip, whoever put it there."""
        first = self.item("one")
        forge = self.forge()
        self.assertTrue(forge.publish("x", "T", "b", expect="")[0])
        theirs = self.person_pushes()
        self.repo_commit("two")
        self.assertTrue(Git(self.repo.root).fetch())
        ok, info = forge.publish("x", "T", "b", expect=first)
        self.assertFalse(ok)
        self.assertTrue(info.startswith(STALE_PUSH), info)
        self.assertEqual(self.origin_git("rev-parse", "sdlc/x"), theirs)

    def test_without_expect_the_push_is_what_it_was_the_lease_is_the_last_fetched_tip(self):
        self.item("one")
        forge = self.forge()
        self.assertTrue(forge.publish("x", "T", "b")[0])
        self.repo_commit("two")
        self.assertTrue(forge.publish("x", "T", "b")[0])
        theirs = self.person_pushes()
        self.repo_commit("three")
        ok, info = forge.publish("x", "T", "b")                  # the person's push was never fetched: refused
        self.assertFalse(ok)
        self.assertTrue(info.startswith(STALE_PUSH), info)
        self.assertEqual(self.origin_git("rev-parse", "sdlc/x"), theirs)

    def test_a_push_that_fails_for_another_reason_is_a_plain_push_failure(self):
        self.item("one")
        self.repo.git("remote", "set-url", "origin", os.path.join(self.repo.tmp, "nowhere.git"))
        ok, info = self.forge().publish("x", "T", "b", expect="")
        self.assertFalse(ok)
        self.assertTrue(info.startswith("push failed: "), info)
        self.assertNotIn(STALE_PUSH, info)

    def test_the_engine_s_push_runs_no_pre_push_hook_of_the_user(self):
        marker = os.path.join(self.repo.tmp, "hook-ran")
        hook = self.repo.write(".git/hooks/pre-push", "#!/bin/sh\necho ran >> '%s'\nexit 1\n" % marker)
        os.chmod(hook, 0o755)
        self.item("one")
        ok, info = self.forge().publish("x", "T", "b", expect="")
        self.assertTrue(ok, info)
        self.assertFalse(os.path.exists(marker))
        self.repo.git("checkout", "-q", "-b", "other")                      # control: the hook is live for a push of ours
        self.assertFalse(subprocess.run(["git", "-C", self.repo.root, "push", "-q", "origin", "other"],
                                        env=self.repo.env(), capture_output=True).returncode == 0)
        self.assertTrue(os.path.exists(marker))

    def test_the_local_forge_takes_the_argument_and_ignores_it(self):
        self.repo.git("branch", "sdlc/x")
        forge = LocalForge(Git(self.repo.root), "main")
        self.assertEqual(forge.publish("x", "T", "b", expect="whatever"), (True, "sdlc/x"))
        self.assertEqual(forge.publish("nope", "T", "b", expect=None), (False, "sdlc/nope"))


class RemoteShaAndRelation(WithClones):
    def test_remote_sha_is_what_the_last_fetch_saw(self):
        git = Git(self.repo.root)
        main = git.head("main")
        self.assertEqual(git.remote_sha("main"), main)
        self.assertEqual(git.remote_sha("sdlc/none"), "")
        other = self.clone("other")
        new = self.commit_file(other, "x.txt", "x")
        self.sh(other, "push", "-q", "origin", "main")
        self.assertEqual(git.remote_sha("main"), main)                       # not fetched yet
        git.fetch()
        self.assertEqual(git.remote_sha("main"), new)

    def test_relation_between_two_refs(self):
        git = Git(self.repo.root)
        base = git.head("main")
        self.repo.git("checkout", "-q", "-b", "ahead")
        self.repo.write("a.txt", "a")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "a")
        self.repo.git("checkout", "-q", "-b", "side", base)
        self.repo.write("s.txt", "s")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "s")
        self.assertEqual(git.relation("main", "main"), "same")
        self.assertEqual(git.relation("main", "origin/main"), "same")
        self.assertEqual(git.relation("main", "ahead"), "behind")
        self.assertEqual(git.relation("ahead", "main"), "ahead")
        self.assertEqual(git.relation("ahead", "side"), "diverged")
        self.assertEqual(git.relation(base, "ahead"), "behind")
        self.assertEqual(git.relation("main", "no-such-ref"), "none")
        self.assertEqual(git.relation("no-such-ref", "main"), "none")
        self.assertEqual(git.relation("nope", "nada"), "none")


# ---------------------------------------------------------------------------------------------------------------------
class RecordedGh(GitHubForge):
    """GitHubForge with a `gh` that answers from a list of PR rows, as `gh pr list` does: newest first, state filtered, at
    most --limit of them. A merge marks the PR it was given merged; every call is kept."""

    def __init__(self, rows, raw=None, merge_fails=False):
        super().__init__(Git("/nonexistent"), "main")
        self.rows, self.raw, self.calls, self.merge_fails = rows, raw, [], merge_fails

    def _gh(self, *args, timeout=120):
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            if self.raw is not None:
                return CmdResult(0, self.raw, "")
            wanted = args[args.index("--state") + 1]
            rows = sorted([p for p in self.rows if wanted == "all" or p["state"] == wanted.upper()], key=lambda p: -p["number"])
            return CmdResult(0, json.dumps(rows[:int(args[args.index("--limit") + 1])]), "")
        if args[:2] == ("pr", "merge"):
            if self.merge_fails:
                return CmdResult(1, "", "GraphQL: Head branch was modified. Review and try the merge again.")
            for p in self.rows:
                if str(p["number"]) == args[2]:
                    p["state"] = "MERGED"
            return CmdResult(0, "", "")
        if args[:2] == ("pr", "view"):
            for p in self.rows:
                if str(p["number"]) == args[2]:
                    return CmdResult(0, json.dumps({"state": p["state"]}), "")
            return CmdResult(1, "", "no pull request")
        return CmdResult(0, "", "")

    def merges(self):
        return [c for c in self.calls if c[:2] == ("pr", "merge")]


def pr(number, state="OPEN", cross=False, **extra):
    row = {"number": number, "url": "https://github.test/o/r/pull/%d" % number, "state": state, "isDraft": False,
           "labels": [], "reviews": [], "reviewDecision": "", "author": {"login": "me"}, "isCrossRepository": cross,
           "headRefOid": "a" * 40}
    row.update(extra)
    return row


class FindingTheItemsPullRequest(unittest.TestCase):
    def test_a_pull_request_from_a_fork_is_not_the_items(self):
        f = RecordedGh([pr(10), pr(11, cross=True)])
        self.assertEqual((f.number("x"), f.state("x"), f.url("x")), ("10", "open", "https://github.test/o/r/pull/10"))
        self.assertEqual(f.close("x", "bye"), True)
        self.assertEqual(f.calls[-1][:3], ("pr", "close", "10"))

    def test_only_a_fork_s_pull_request_is_no_pull_request_at_all(self):
        f = RecordedGh([pr(11, cross=True), pr(12, "MERGED", cross=True)])
        self.assertEqual((f.number("x"), f.state("x"), f.url("x")), ("", "none", "sdlc/x"))
        self.assertEqual(f.merge("x", "t"), (False, "no open PR for sdlc/x"))
        self.assertFalse(f.merges())

    def test_an_open_pull_request_beats_a_merged_or_closed_one_of_the_same_slug_whatever_the_numbers(self):
        for others in ([pr(3, "MERGED")], [pr(30, "MERGED")], [pr(30, "CLOSED"), pr(3, "MERGED")]):
            with self.subTest(others=[(p["number"], p["state"]) for p in others]):
                f = RecordedGh([pr(10)] + others)
                self.assertEqual((f.state("x"), f.number("x"), f.url("x")), ("open", "10", "https://github.test/o/r/pull/10"))

    def test_without_an_open_one_the_newest_decides(self):
        self.assertEqual(RecordedGh([pr(3, "MERGED"), pr(5, "CLOSED")]).state("x"), "closed")
        self.assertEqual(RecordedGh([pr(3, "CLOSED"), pr(5, "MERGED")]).state("x"), "merged")
        self.assertEqual(RecordedGh([]).state("x"), "none")

    def test_number_is_the_open_one_else_the_merged_one_never_a_closed_one(self):
        self.assertEqual(RecordedGh([pr(3, "MERGED"), pr(5, "CLOSED")]).number("x"), "3")
        self.assertEqual(RecordedGh([pr(5, "CLOSED")]).number("x"), "")

    def test_the_list_asks_for_enough_rows_and_the_fields_the_code_reads(self):
        f = RecordedGh([pr(10)])
        f.state("x")
        call = f.calls[0]
        self.assertGreaterEqual(int(call[call.index("--limit") + 1]), 20)
        fields = call[call.index("--json") + 1].split(",")
        for need in ("number", "url", "state", "isCrossRepository", "headRefOid", "isDraft", "reviews", "labels", "author"):
            self.assertIn(need, fields)
        self.assertEqual(call[call.index("--head") + 1], "sdlc/x")

    def test_a_row_beyond_the_old_limit_of_one_is_seen(self):
        rows = [pr(n, "CLOSED") for n in range(20, 30)] + [pr(10)]         # ten newer closed ones in front of it
        self.assertEqual(RecordedGh(rows).state("x"), "open")
        self.assertEqual(RecordedGh(rows).number("x"), "10")

    def test_merge_acts_on_the_open_pull_request_by_its_number_and_confirms_that_one(self):
        f = RecordedGh([pr(3, "MERGED"), pr(10), pr(11, cross=True)])
        self.assertEqual(f.merge("x", "t"), (True, "10"))
        self.assertEqual(f.merges(), [("pr", "merge", "10", "--squash", "--delete-branch")])
        self.assertEqual(f.calls[-1][:2], ("pr", "list"))                     # the merge is confirmed from the list, by number
        self.assertEqual([c for c in f.calls if c[:2] == ("pr", "view")], [])

    def test_merge_with_a_head_passes_it_as_the_commit_to_match(self):
        f = RecordedGh([pr(10)])
        self.assertEqual(f.merge("x", "t", head="abc123"), (True, "10"))
        self.assertEqual(f.merges(), [("pr", "merge", "10", "--squash", "--delete-branch", "--match-head-commit", "abc123")])

    def test_a_merge_the_head_check_refuses_is_a_failure_with_gh_s_words(self):
        f = RecordedGh([pr(10)], merge_fails=True)
        ok, info = f.merge("x", "t", head="abc123")
        self.assertFalse(ok)
        self.assertEqual(info, "gh pr merge: GraphQL: Head branch was modified. Review and try the merge again.")

    def test_a_merge_that_exits_zero_but_did_not_merge_is_no_merge(self):
        f = RecordedGh([pr(10)])
        real = f._gh

        def queued(*args, timeout=120):
            if args[:2] == ("pr", "merge"):
                f.calls.append(args)
                return CmdResult(0, "", "")                       # --auto, a merge queue: accepted, not merged
            return real(*args, timeout=timeout)
        f._gh = queued
        self.assertEqual(f.merge("x", "t"), (False, "PR #10 is not merged (state: open)"))

    def test_a_pull_request_somebody_merged_already_is_a_merge_that_is_done(self):
        f = RecordedGh([pr(10, "MERGED")])
        self.assertEqual(f.merge("x", "t"), (True, "10"))
        self.assertFalse(f.merges())

    def test_the_merge_is_confirmed_by_the_number_even_when_a_newer_pull_request_took_the_branch_name(self):
        f = RecordedGh([pr(10)])
        real = f._gh

        def merged_then_reopened(*args, timeout=120):
            r = real(*args, timeout=timeout)
            if args[:2] == ("pr", "merge"):
                f.rows.append(pr(12, "CLOSED"))                  # a newer PR on the same branch name, not merged
            return r
        f._gh = merged_then_reopened
        self.assertEqual(f.merge("x", "t"), (True, "10"))

    def test_shapes_gh_does_not_normally_print_are_unknown_not_a_crash(self):
        shapes = {"an object": "{}", "null": "null", "a string": '"x"', "rows that are not objects": '["a", 1]',
                  "a row without a number": '[{"state": "OPEN", "url": "u"}]', "a number that is text": '[{"number": "7", "state": "OPEN"}]',
                  "not json": "oops", "a row that is null": "[null]"}
        for name, raw in shapes.items():
            with self.subTest(name):
                f = RecordedGh([], raw=raw)
                self.assertEqual(f.state("x"), "unknown")
                self.assertEqual(f.number("x"), "")
                self.assertEqual(f.url("x"), "sdlc/x")
                self.assertFalse(f.approved("x"))
                self.assertFalse(f.close("x", "c"))
                ok, info = f.merge("x", "t")
                self.assertFalse(ok)
                self.assertFalse(f.merges())
                self.assertEqual(f.checks("x"), ("unknown", []))
                self.assertEqual(f.merge_state("x"), "")

    def test_publish_with_a_list_gh_cannot_read_after_the_push_fails_with_the_reason(self):
        answers = [CmdResult(0, "", ""), CmdResult(0, "{}", "")]
        with mock.patch.object(gitops, "run", side_effect=answers):
            ok, info = GitHubForge(Git("/repo"), "main").publish("x", "t", "b")
        self.assertEqual((ok, info), (False, "unreadable gh output"))

    def test_a_pull_request_without_a_url_is_the_branch_name_not_a_crash(self):
        f = RecordedGh([pr(10, url=None)])
        self.assertEqual(f.url("x"), "sdlc/x")


class Approvals(unittest.TestCase):
    def approved(self, reviews, author="loop-bot", **extra):
        f = RecordedGh([pr(7, reviews=reviews, author={"login": author}, **extra)])
        return f.approved("x")

    @staticmethod
    def rv(who, state, at=None):
        row = {"author": {"login": who}, "state": state}
        if at:
            row["submittedAt"] = at
        return row

    def test_a_comment_after_an_approval_does_not_withdraw_it(self):
        self.assertTrue(self.approved([self.rv("alice", "APPROVED"), self.rv("alice", "COMMENTED")]))
        self.assertTrue(self.approved([self.rv("alice", "APPROVED"), self.rv("alice", "PENDING")]))

    def test_the_latest_of_approved_changes_requested_dismissed_decides_per_reviewer(self):
        cases = [
            ("approve only", [("alice", "APPROVED")], True),
            ("changes requested only", [("alice", "CHANGES_REQUESTED")], False),
            ("comment only", [("alice", "COMMENTED")], False),
            ("pending only", [("alice", "PENDING")], False),
            ("changes requested, then approved", [("alice", "CHANGES_REQUESTED"), ("alice", "APPROVED")], True),
            ("approved, then changes requested", [("alice", "APPROVED"), ("alice", "CHANGES_REQUESTED")], False),
            ("approved, then dismissed", [("alice", "APPROVED"), ("alice", "DISMISSED")], False),
            ("approved, dismissed, approved again", [("alice", "APPROVED"), ("alice", "DISMISSED"), ("alice", "APPROVED")], True),
            ("changes requested, then dismissed, someone else approved",
             [("alice", "CHANGES_REQUESTED"), ("alice", "DISMISSED"), ("bob", "APPROVED")], True),
            ("one approves, another still wants changes", [("alice", "APPROVED"), ("bob", "CHANGES_REQUESTED")], False),
            ("approved, a comment, changes requested, a comment",
             [("alice", "APPROVED"), ("alice", "COMMENTED"), ("alice", "CHANGES_REQUESTED"), ("alice", "COMMENTED")], False),
            ("the author's own approval", [("loop-bot", "APPROVED")], False),
            ("login case does not matter", [("Alice", "APPROVED"), ("ALICE", "COMMENTED")], True),
            ("a state in lower case", [("alice", "approved")], True),
            ("no reviews", [], False),
        ]
        for name, reviews, want in cases:
            with self.subTest(name):
                self.assertEqual(self.approved([self.rv(w, s) for w, s in reviews]), want)

    def test_reviews_are_put_in_the_order_they_were_submitted(self):
        late_comment_listed_first = [self.rv("alice", "COMMENTED", "2026-10-14T12:00:00Z"),
                                     self.rv("alice", "APPROVED", "2026-10-14T11:00:00Z")]
        self.assertTrue(self.approved(late_comment_listed_first))
        changes_after = [self.rv("alice", "CHANGES_REQUESTED", "2026-10-14T12:00:00Z"),
                         self.rv("alice", "APPROVED", "2026-10-14T11:00:00Z")]
        self.assertFalse(self.approved(changes_after))

    def test_the_decision_the_label_and_odd_review_rows(self):
        self.assertTrue(self.approved([], reviewDecision="APPROVED"))
        self.assertTrue(self.approved([self.rv("alice", "CHANGES_REQUESTED")], reviewDecision="APPROVED"))
        self.assertTrue(self.approved([], labels=[{"name": "sdlc:approved"}]))
        self.assertFalse(self.approved([], labels=[{"name": "other"}, None, "x"]))
        self.assertFalse(self.approved([None, "x", {"state": "APPROVED"}, {"author": None, "state": "APPROVED"}]))
        self.assertTrue(self.approved([None, self.rv("alice", "APPROVED")]))
        self.assertFalse(self.approved(None))
        self.assertFalse(self.approved("APPROVED"))


# ---------------------------------------------------------------------------------------------------------------------
class ToDefault(WithClones):
    def other_pushes_to_main(self, name="other", text="elsewhere"):
        other = self.clone(name)
        tip = self.commit_file(other, "%s.txt" % text, text)
        self.sh(other, "push", "-q", "origin", "main")
        return tip

    def test_a_fetch_that_works_fast_forwards_the_default_branch(self):
        tip = self.other_pushes_to_main()
        git = Git(self.repo.root)
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.head("main"), tip)
        self.assertEqual((git.last_error, git.last_warning), ("", ""))

    def test_it_leaves_a_branch_the_loop_was_on_for_the_default_one(self):
        self.repo.git("checkout", "-q", "-b", "sdlc/x")
        git = Git(self.repo.root)
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.branch(), "main")

    def test_a_network_that_is_down_is_a_warning_and_the_loop_goes_on_with_what_it_has(self):
        git = Git(self.repo.root)
        main = git.head("main")
        os.rename(self.repo.origin, self.repo.origin + ".away")
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.branch(), "main")
        self.assertEqual(git.head("main"), main)
        self.assertTrue(git.last_warning.startswith("fetch from origin failed"), git.last_warning)
        self.assertIn("fatal:", git.last_warning)                       # git's own reason is in it
        self.assertEqual(git.last_error, "")                            # …and it is no error
        os.rename(self.repo.origin + ".away", self.repo.origin)
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.last_warning, "")                          # the next call starts clean

    def test_it_fast_forwards_to_what_was_fetched_earlier_when_the_fetch_fails(self):
        tip = self.other_pushes_to_main()
        git = Git(self.repo.root)
        self.assertTrue(git.fetch())
        os.rename(self.repo.origin, self.repo.origin + ".away")
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.head("main"), tip)
        self.assertTrue(git.last_warning)

    def test_a_local_default_branch_that_is_ahead_of_origin_is_fine(self):
        self.commit_file(self.repo.root, "mine.txt", "mine")
        git = Git(self.repo.root)
        mine = git.head("main")
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.head("main"), mine)

    def test_a_diverged_default_branch_is_the_one_failure_and_says_why(self):
        self.other_pushes_to_main()
        self.commit_file(self.repo.root, "local.txt", "local")
        git = Git(self.repo.root)
        self.assertFalse(git.to_default("main"))
        self.assertEqual(git.last_error, "fatal: Not possible to fast-forward, aborting.")
        self.assertEqual(git.with_reason("cannot"), "cannot: fatal: Not possible to fast-forward, aborting.")

    def test_a_branch_that_cannot_be_checked_out_is_a_failure_with_git_s_reason(self):
        git = Git(self.repo.root)
        self.assertFalse(git.to_default("no-such-branch"))
        self.assertIn("no-such-branch", git.last_error)

    def test_a_default_branch_origin_does_not_have_is_checked_out_and_that_is_all(self):
        self.repo.git("checkout", "-q", "-b", "trunk")
        self.repo.git("checkout", "-q", "main")
        git = Git(self.repo.root)
        self.assertTrue(git.to_default("trunk"))
        self.assertEqual(git.branch(), "trunk")
        self.assertEqual(git.last_error, "")

    def test_it_needs_no_upstream_configured_for_the_default_branch(self):
        tip = self.other_pushes_to_main()
        self.repo.git("branch", "--unset-upstream", "main")
        git = Git(self.repo.root)
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.head("main"), tip)

    def test_without_a_remote_there_is_nothing_to_fetch_and_nothing_to_warn_about(self):
        self.repo.git("remote", "remove", "origin")
        git = Git(self.repo.root)
        self.assertTrue(git.to_default("main"))
        self.assertEqual((git.last_error, git.last_warning), ("", ""))

    def test_in_a_worktree_where_the_default_branch_is_elsewhere_it_detaches_at_the_tip(self):
        tip = self.other_pushes_to_main()
        wt = os.path.join(self.repo.tmp, "wt")
        self.repo.git("worktree", "add", "-q", "-b", "sdlc/x", wt)
        git = Git(wt)
        self.assertTrue(git.to_default("main"))
        self.assertEqual(git.branch(), "")
        self.assertEqual(git.head(), tip)

    def test_in_such_a_worktree_a_network_that_is_down_is_a_warning_too(self):
        wt = os.path.join(self.repo.tmp, "wt")
        self.repo.git("worktree", "add", "-q", "-b", "sdlc/x", wt)
        os.rename(self.repo.origin, self.repo.origin + ".away")
        git = Git(wt)
        self.assertTrue(git.to_default("main"))
        self.assertTrue(git.last_warning.startswith("fetch from origin failed"))


class PreferLocal(WithClones):
    def test_base_ref_is_origin_s_branch_unless_the_local_one_is_preferred(self):
        git = Git(self.repo.root)
        self.assertFalse(git.prefer_local)
        self.assertEqual(git.base_ref("main"), "origin/main")
        git.prefer_local = True
        self.assertEqual(git.base_ref("main"), "main")
        self.assertEqual(git.base_ref("gone"), "gone")

    def test_a_forge_that_merges_locally_sets_it_whether_or_not_there_is_a_remote(self):
        git = Git(self.repo.root)
        forge = make_forge(git, "main", "local")
        self.assertIsInstance(forge, LocalForge)
        self.assertTrue(git.prefer_local)
        self.assertEqual(git.base_ref("main"), "main")
        git = Git(self.repo.root)
        self.assertIsInstance(make_forge(git, "main", "auto"), GitHubForge)
        self.assertFalse(git.prefer_local)
        self.assertEqual(git.base_ref("main"), "origin/main")
        make_forge(git, "main", "local")
        self.assertTrue(git.prefer_local)
        make_forge(git, "main", "auto")
        self.assertFalse(git.prefer_local)

    def test_with_no_remote_the_local_forge_is_chosen_and_the_result_is_the_same_as_before(self):
        self.repo.git("remote", "remove", "origin")
        git = Git(self.repo.root)
        self.assertIsInstance(make_forge(git, "main"), LocalForge)
        self.assertTrue(git.prefer_local)
        self.assertEqual(git.base_ref("main"), "main")

    def test_the_queue_of_a_local_forge_in_a_clone_is_read_from_the_local_branch(self):
        """Local main has a commit origin does not: origin/main is behind it, and is not what changes are measured against."""
        git = Git(self.repo.root)
        self.commit_file(self.repo.root, "queued.txt", "queued")
        self.assertEqual(git.relation(git.base_ref("main"), "main"), "behind")         # origin's main lacks what the queue has
        make_forge(git, "main", "local")
        self.assertEqual(git.relation(git.base_ref("main"), "main"), "same")


# ---------------------------------------------------------------------------------------------------------------------
class PidFiles(RepoCase):
    def pid_path(self):
        return self.repo.path(st.PID_REL)

    def test_the_pid_file_names_the_process_and_when_it_started(self):
        st.write_pid(self.repo.root)
        with open(self.pid_path()) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("%d " % os.getpid()), text)
        self.assertEqual(text.strip().split(None, 1)[1], U.proc_start(os.getpid()))
        self.assertEqual(st.loop_pid(self.repo.root), os.getpid())

    def test_the_first_word_is_still_the_pid_for_whatever_reads_it_that_way(self):
        st.write_pid(self.repo.root)
        with open(self.pid_path()) as fh:
            self.assertEqual(fh.read().split()[0], str(os.getpid()))
        st.clear_pid(self.repo.root)
        self.assertFalse(os.path.exists(self.pid_path()))

    def test_a_pid_that_belongs_to_another_process_now_is_no_loop(self):
        victim = subprocess.Popen(["sleep", "60"])
        self.addCleanup(victim.kill)
        self.repo.write(st.PID_REL, "%d Mon Jan  1 00:00:00 2001\n" % victim.pid)          # the dead loop's pid, reused
        self.assertEqual(st.loop_pid(self.repo.root), 0)
        self.assertIsNone(victim.poll())

    def test_the_same_process_with_the_start_time_it_wrote_is_the_loop(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.kill)
        self.repo.write(st.PID_REL, "%d %s\n" % (child.pid, U.proc_start(child.pid)))
        self.assertEqual(st.loop_pid(self.repo.root), child.pid)

    def test_an_old_file_with_a_bare_pid_is_taken_at_its_word(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.kill)
        self.repo.write(st.PID_REL, "%d\n" % child.pid)
        self.assertEqual(st.loop_pid(self.repo.root), child.pid)
        self.repo.write(st.PID_REL, "%d" % child.pid)
        self.assertEqual(st.loop_pid(self.repo.root), child.pid)

    def test_a_process_that_is_gone_and_a_file_that_is_nothing_like_a_pid_are_no_loop(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.repo.write(st.PID_REL, "%d Mon Jan  1 00:00:00 2001\n" % gone.pid)
        self.assertEqual(st.loop_pid(self.repo.root), 0)
        for text in ("", "\n", "not a pid\n", "0\n", "-5 x\n", "abc 123\n"):
            with self.subTest(text=text):
                self.repo.write(st.PID_REL, text)
                self.assertEqual(st.loop_pid(self.repo.root), 0)
        os.remove(self.pid_path())
        self.assertEqual(st.loop_pid(self.repo.root), 0)

    def test_when_ps_cannot_say_what_is_alive_is_all_there_is_to_go_by(self):
        victim = subprocess.Popen(["sleep", "60"])
        self.addCleanup(victim.kill)
        self.repo.write(st.PID_REL, "%d Mon Jan  1 00:00:00 2001\n" % victim.pid)
        with mock.patch.object(U, "proc_start", return_value=""):
            self.assertEqual(st.loop_pid(self.repo.root), victim.pid)
        with mock.patch.object(st, "proc_start", return_value=""):
            st.write_pid(self.repo.root)                                           # `ps` failed when the loop started
        with open(self.pid_path()) as fh:
            self.assertEqual(fh.read(), "%d\n" % os.getpid())
        self.assertEqual(st.loop_pid(self.repo.root), os.getpid())

    def test_the_start_time_is_squeezed_and_survives_the_single_digit_day(self):
        with mock.patch.object(U, "run", return_value=CmdResult(0, "Thu Oct  2 09:05:03 2026\n", "")):
            self.assertEqual(U.proc_start(123), "Thu Oct 2 09:05:03 2026")
        with mock.patch.object(U, "run", return_value=CmdResult(1, "", "")):
            self.assertEqual(U.proc_start(123), "")
        self.assertEqual(U.proc_start("x"), "")
        self.assertEqual(U.proc_start(0), "")
        self.assertEqual(U.proc_start(None), "")
        self.assertEqual(U.proc_start(2 ** 22 + 12345678), "")                    # no such process

    def test_the_cli_does_not_signal_a_reused_pid(self):
        victim = subprocess.Popen(["sleep", "60"])
        self.addCleanup(victim.kill)
        self.repo.write(st.PID_REL, "%d Mon Jan  1 00:00:00 2001\n" % victim.pid)
        self.repo.cli("stop")
        time.sleep(0.3)
        self.assertIsNone(victim.poll())


# ---------------------------------------------------------------------------------------------------------------------
CLAIMER = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, @SCRIPTS@)
    from sdlclib.state import Claims
    common, root, slug, go, hold = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5])
    open(go + ".ready-%d" % os.getpid(), "w").close()
    while not os.path.exists(go):
        time.sleep(0.002)
    c = Claims(common, root)
    got = c.take(slug)
    print("TAKEN" if got else "REFUSED", flush=True)
    time.sleep(hold)
""").replace("@SCRIPTS@", repr(SCRIPTS))


class ClaimFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sdlc-claims-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.common = os.path.join(self.tmp, "common")
        os.makedirs(self.common)
        self.claims = st.Claims(self.common, "/work/a")

    def path(self, slug):
        return os.path.join(self.common, "sdlc-claims", slug)

    def write_claim(self, slug, text):
        os.makedirs(os.path.dirname(self.path(slug)), exist_ok=True)
        with open(self.path(slug), "w") as fh:
            fh.write(text)

    def claimers(self, slug, n, hold):
        go = os.path.join(self.tmp, "go-%s" % slug)
        procs = [subprocess.Popen([sys.executable, "-c", CLAIMER, self.common, "/work/p%d" % i, slug, go, str(hold)],
                                  stdout=subprocess.PIPE, text=True) for i in range(n)]
        self.addCleanup(lambda: [p.kill() for p in procs])
        end = time.time() + 60
        while time.time() < end and len([f for f in os.listdir(self.tmp) if f.startswith(os.path.basename(go) + ".ready-")]) < n:
            time.sleep(0.02)                             # every one of them is up and waiting for the file
        open(go, "w").close()
        return [p.communicate()[0].strip() for p in procs]

    def test_take_creates_the_file_and_release_removes_it(self):
        self.assertTrue(self.claims.take("a-item"))
        self.assertEqual(self.claims.held, "a-item")
        with open(self.path("a-item")) as fh:
            lines = fh.read().split("\n")
        self.assertEqual(lines[0], "%d /work/a" % os.getpid())
        self.assertEqual(lines[1], U.proc_start(os.getpid()))
        self.claims.release()
        self.assertFalse(os.path.exists(self.path("a-item")))
        self.assertIsNone(self.claims.held)

    def test_the_file_is_created_exclusively(self):
        with mock.patch.object(st.os, "open", wraps=os.open) as op:
            self.assertTrue(self.claims.take("a-item"))
        flags = op.call_args_list[0].args[1]
        self.assertTrue(flags & os.O_CREAT and flags & os.O_EXCL)

    def test_a_live_claim_of_another_process_refuses_the_take_and_names_its_root(self):
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.kill)
        self.write_claim("a-item", "%d /work/other\n%s\n" % (holder.pid, U.proc_start(holder.pid)))
        self.assertEqual(self.claims.owner("a-item"), "/work/other")
        self.assertFalse(self.claims.take("a-item"))
        self.assertIsNone(self.claims.held)
        with open(self.path("a-item")) as fh:
            self.assertTrue(fh.read().startswith("%d /work/other" % holder.pid))

    def test_a_claim_in_the_old_format_of_a_live_process_still_holds(self):
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.kill)
        self.write_claim("a-item", "%d /work/old\n" % holder.pid)
        self.assertEqual(self.claims.owner("a-item"), "/work/old")
        self.assertFalse(self.claims.take("a-item"))

    def test_a_root_with_spaces_is_given_whole(self):
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.kill)
        self.write_claim("a-item", "%d /work/my repo/x\n%s\n" % (holder.pid, U.proc_start(holder.pid)))
        self.assertEqual(self.claims.owner("a-item"), "/work/my repo/x")

    def test_a_claim_of_a_dead_process_is_taken_over(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.write_claim("a-item", "%d /work/dead\n%s\n" % (gone.pid, "Mon Jan  1 00:00:00 2001"))
        self.assertIsNone(self.claims.owner("a-item"))
        self.assertTrue(self.claims.take("a-item"))
        with open(self.path("a-item")) as fh:
            self.assertTrue(fh.read().startswith("%d /work/a" % os.getpid()))

    def test_a_claim_whose_pid_was_reused_by_another_process_is_taken_over(self):
        stranger = subprocess.Popen(["sleep", "60"])
        self.addCleanup(stranger.kill)
        self.write_claim("a-item", "%d /work/ghost\nMon Jan  1 00:00:00 2001\n" % stranger.pid)
        self.assertIsNone(self.claims.owner("a-item"))
        self.assertTrue(self.claims.take("a-item"))
        self.assertIsNone(stranger.poll())

    def test_a_claim_left_by_this_process_is_not_another_loop_s(self):
        self.write_claim("a-item", "%d /work/a\n%s\n" % (os.getpid(), U.proc_start(os.getpid())))
        self.assertIsNone(self.claims.owner("a-item"))
        self.assertTrue(self.claims.take("a-item"))

    def test_a_file_just_created_and_not_yet_written_is_a_live_claim_an_old_empty_one_is_not(self):
        self.write_claim("a-item", "")
        self.assertEqual(self.claims.owner("a-item"), "?")
        self.assertFalse(self.claims.take("a-item"))
        old = time.time() - 60
        os.utime(self.path("a-item"), (old, old))
        self.assertIsNone(self.claims.owner("a-item"))
        self.assertTrue(self.claims.take("a-item"))
        self.write_claim("b-item", "garbage\n")
        os.utime(self.path("b-item"), (old, old))
        self.assertIsNone(self.claims.owner("b-item"))
        self.assertTrue(self.claims.take("b-item"))

    def test_nothing_there_is_nobody(self):
        self.assertIsNone(self.claims.owner("a-item"))
        self.assertFalse(os.path.exists(os.path.join(self.common, "sdlc-claims")))

    def test_eight_loops_taking_one_item_at_the_same_moment_one_of_them_gets_it(self):
        answers = self.claimers("race", 8, hold=1.5)
        self.assertEqual(sorted(answers), ["REFUSED"] * 7 + ["TAKEN"], answers)

    def test_eight_loops_finding_the_same_dead_claim_one_of_them_takes_it_over(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.write_claim("race", "%d /work/dead\nMon Jan  1 00:00:00 2001\n" % gone.pid)
        answers = self.claimers("race", 8, hold=1.5)
        self.assertEqual(sorted(answers), ["REFUSED"] * 7 + ["TAKEN"], answers)

    def test_once_the_holder_has_exited_the_item_can_be_taken(self):
        answers = self.claimers("done", 1, hold=0)             # `claimers` waits for it to exit, without releasing the claim
        self.assertEqual(answers, ["TAKEN"])
        self.assertTrue(os.path.exists(self.path("done")))
        self.assertIsNone(self.claims.owner("done"))
        self.assertTrue(self.claims.take("done"))


# ---------------------------------------------------------------------------------------------------------------------
class ClaimsOnOrigin(WithClones):
    def setUp(self):
        super().setUp()
        self.a = Git(self.repo.root)
        self.second = self.clone("second clone")             # a space in the folder name: it becomes one word
        self.b = Git(self.second)

    def who(self, root):
        """How a claim from `root` names its holder: the git user name and the checkout's folder name, nothing more."""
        return "%s in %s" % (gitops.one_word(Git(root).out("config", "user.name") or "?"), gitops.one_word(os.path.basename(root)))

    def ref(self, slug="x"):
        return self.origin_git("rev-parse", "refs/sdlc/claims/" + slug)

    def message(self, slug="x"):
        return self.origin_git("log", "-1", "--format=%B", "refs/sdlc/claims/" + slug)

    def test_the_first_claim_is_taken_and_is_a_commit_of_the_empty_tree_with_one_line(self):
        self.assertEqual(self.a.claim("x", "tok-a", 3600), "taken")
        sha = self.ref()
        self.assertTrue(sha)
        self.assertIn("tree " + EMPTY_TREE, self.origin_git("cat-file", "-p", sha))
        words = self.message().split(" ")
        self.assertEqual(self.message().count("\n"), 0)
        self.assertEqual(words[:3], ["sdlc-claim", "tok-a", gitops.one_word(self.a.out("config", "user.name") or "?")])
        self.assertEqual(words[3:-2], [os.path.basename(self.repo.root)])                 # the folder, not the whole path
        self.assertNotIn(os.path.dirname(self.repo.root), self.message())
        self.assertEqual(int(words[-2]), os.getpid())
        self.assertTrue(U.parse_iso(words[-1]))

    def test_the_claim_is_no_branch_and_leaves_no_ref_in_this_repository(self):
        self.a.claim("x", "tok-a", 3600)
        self.assertNotIn("claims", self.origin_git("branch", "-a"))
        self.assertEqual(self.b.claim("x", "tok-b", 3600)[:6], "other:")
        for g in (self.repo.root, self.second):
            self.assertEqual(self.sh(g, "for-each-ref", "refs/sdlc"), "")

    def test_the_same_token_again_is_ours_and_renews_the_claim(self):
        with mock.patch.object(gitops, "now_iso", return_value="2026-10-14T10:00:00Z"):
            self.assertEqual(self.a.claim("x", "tok-a", 3600), "taken")
        first = self.ref()
        with mock.patch.object(gitops, "now_iso", return_value="2026-10-14T10:05:00Z"):
            self.assertEqual(self.a.claim("x", "tok-a", 3600), "ours")
        self.assertNotEqual(self.ref(), first)
        self.assertTrue(self.message().endswith("2026-10-14T10:05:00Z"), self.message())

    def test_another_clone_finds_it_held_and_is_told_by_whom_since_when(self):
        with mock.patch.object(gitops, "now_iso", return_value="2026-10-14T10:00:00Z"):
            self.assertEqual(self.a.claim("x", "tok-a", 10 ** 9), "taken")
        got = self.b.claim("x", "tok-b", 10 ** 9)
        self.assertEqual(got, "other:%s since 2026-10-14T10:00:00Z" % self.who(self.repo.root))
        self.assertEqual(self.a.claim_holder("x"), "%s since 2026-10-14T10:00:00Z" % self.who(self.repo.root))
        self.assertEqual(self.b.claim_holder("x"), self.a.claim_holder("x"))
        self.assertTrue(self.message().split(" ")[1] == "tok-a")                          # the claim was not touched

    def test_the_holder_of_a_folder_with_spaces_is_named_in_one_word(self):
        self.assertEqual(self.b.claim("x", "tok-b", 3600), "taken")
        self.assertEqual(self.a.claim("x", "tok-a", 3600).split(" since ")[0], "other:" + self.who(self.second))
        self.assertIn(" in second_clone", self.who(self.second))

    def test_nobody_is_the_empty_holder(self):
        self.assertEqual(self.a.claim_holder("x"), "")

    def test_the_race_the_other_clone_wins_between_our_look_and_our_push(self):
        """A looked and saw nothing; B then took it. A's push is leased on "no such ref" and loses, A looks again."""
        real = self.a._claim_read
        views = []

        def stale_first(slug):
            views.append(1)
            return ("absent", "", None) if len(views) == 1 else real(slug)
        self.assertEqual(self.b.claim("x", "tok-b", 3600), "taken")
        with mock.patch.object(self.a, "_claim_read", side_effect=stale_first):
            got = self.a.claim("x", "tok-a", 3600)
        self.assertTrue(got.startswith("other:"), got)
        self.assertEqual(len(views), 2)
        self.assertEqual(self.message().split(" ")[1], "tok-b")

    def test_two_clones_claiming_together_one_gets_it(self):
        import threading
        results, gate = {}, threading.Barrier(2)

        def go(name, git):
            gate.wait()
            results[name] = git.claim("x", "tok-" + name, 3600)
        threads = [threading.Thread(target=go, args=(n, g)) for n, g in (("a", self.a), ("b", self.b))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(r.split(":")[0] for r in results.values()), ["other", "taken"], results)
        winner = "a" if results["a"] == "taken" else "b"
        self.assertEqual(self.message().split(" ")[1], "tok-" + winner)

    def test_a_stale_claim_is_taken_over_and_the_old_holder_can_no_longer_refresh_or_release(self):
        with mock.patch.object(gitops, "now_iso", return_value="2020-01-01T00:00:00Z"):
            self.assertEqual(self.a.claim("x", "tok-a", 3600), "taken")
        self.assertEqual(self.b.claim("x", "tok-b", 3600), "taken")
        self.assertEqual(self.message().split(" ")[1], "tok-b")
        self.assertFalse(self.a.refresh_claim("x", "tok-a"))
        self.assertFalse(self.a.release_claim("x", "tok-a"))
        self.assertEqual(self.message().split(" ")[1], "tok-b")
        self.assertTrue(self.a.claim("x", "tok-a", 3600).startswith("other:"))

    def test_a_claim_is_stale_only_when_older_than_the_limit(self):
        with mock.patch.object(gitops, "now_iso", return_value=U.now_iso()):
            self.a.claim("x", "tok-a", 3600)
        self.assertTrue(self.b.claim("x", "tok-b", 3600).startswith("other:"))
        self.assertEqual(self.b.claim("x", "tok-b", -1), "taken")

    def test_the_takeover_is_leased_on_the_claim_that_was_read(self):
        """B reads the stale claim; before B pushes, A renews it. B's push must lose and B reads again."""
        with mock.patch.object(gitops, "now_iso", return_value="2020-01-01T00:00:00Z"):
            self.a.claim("x", "tok-a", 3600)
        real = self.b._claim_read
        views = []

        def read_then_a_renews(slug):
            views.append(1)
            seen = real(slug)
            if len(views) == 1:
                self.assertTrue(self.a.refresh_claim("x", "tok-a"))
            return seen
        with mock.patch.object(self.b, "_claim_read", side_effect=read_then_a_renews):
            got = self.b.claim("x", "tok-b", 3600)
        self.assertTrue(got.startswith("other:"), got)
        self.assertEqual(self.message().split(" ")[1], "tok-a")

    def test_refresh_renews_our_claim_and_nobody_else_s(self):
        with mock.patch.object(gitops, "now_iso", return_value="2026-10-14T10:00:00Z"):
            self.a.claim("x", "tok-a", 3600)
        first = self.ref()
        with mock.patch.object(gitops, "now_iso", return_value="2026-10-14T11:00:00Z"):
            self.assertTrue(self.a.refresh_claim("x", "tok-a"))
            self.assertFalse(self.b.refresh_claim("x", "tok-b"))
            self.assertFalse(self.a.refresh_claim("nothing", "tok-a"))
        self.assertNotEqual(self.ref(), first)
        self.assertTrue(self.message().endswith("2026-10-14T11:00:00Z"))
        self.assertEqual(self.message().split(" ")[1], "tok-a")

    def test_release_deletes_our_claim_only_and_a_second_release_finds_nothing_to_do(self):
        self.a.claim("x", "tok-a", 3600)
        self.assertFalse(self.b.release_claim("x", "tok-b"))
        self.assertTrue(self.ref())
        self.assertTrue(self.a.release_claim("x", "tok-a"))
        self.assertIsNone(self.ref())
        self.assertTrue(self.a.release_claim("x", "tok-a"))
        self.assertEqual(self.b.claim("x", "tok-b", 3600), "taken")

    def test_release_with_a_lease_does_not_delete_a_claim_that_changed_after_it_was_read(self):
        self.a.claim("x", "tok-a", 3600)
        real = self.a._claim_read

        def read_then_it_is_renewed(slug):
            seen = real(slug)
            with mock.patch.object(gitops, "now_iso", return_value="2026-10-14T11:00:00Z"):
                self.assertTrue(Git(self.repo.root).refresh_claim("x", "tok-a"))         # same token, a new commit
            return seen
        with mock.patch.object(self.a, "_claim_read", side_effect=read_then_it_is_renewed):
            self.assertFalse(self.a.release_claim("x", "tok-a"))
        self.assertTrue(self.ref())

    def test_claims_on_different_items_do_not_meet(self):
        self.assertEqual(self.a.claim("x", "tok-a", 3600), "taken")
        self.assertEqual(self.b.claim("y", "tok-b", 3600), "taken")
        self.assertEqual(self.b.claim("x-more", "tok-b", 3600), "taken")
        self.assertIn(self.who(self.second), self.a.claim_holder("y"))

    def test_an_origin_that_cannot_be_reached_is_an_error_for_every_call(self):
        os.rename(self.repo.origin, self.repo.origin + ".away")
        got = self.a.claim("x", "tok-a", 3600)
        self.assertTrue(got.startswith("error:") and len(got) > len("error:"), got)
        self.assertEqual(self.a.claim_holder("x"), "")
        self.assertFalse(self.a.refresh_claim("x", "tok-a"))
        self.assertFalse(self.a.release_claim("x", "tok-a"))
        os.rename(self.repo.origin + ".away", self.repo.origin)
        self.assertEqual(self.a.claim("x", "tok-a", 3600), "taken")

    def test_origin_refusing_the_push_is_an_error(self):
        hook = os.path.join(self.repo.origin, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\necho 'no claims here' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        got = self.a.claim("x", "tok-a", 3600)
        self.assertTrue(got.startswith("error:"), got)
        self.assertIsNone(self.ref())

    def test_no_remote_is_an_error_and_so_is_a_remote_that_is_not_a_repository(self):
        lonely = Git(self.repo.root)
        self.repo.git("remote", "remove", "origin")
        self.assertEqual(lonely.claim("x", "tok", 3600), "error:no remote named origin")
        self.assertEqual(lonely.claim_holder("x"), "")
        self.assertFalse(lonely.refresh_claim("x", "tok"))
        self.assertFalse(lonely.release_claim("x", "tok"))
        self.repo.git("remote", "add", "origin", os.path.join(self.repo.tmp, "not-there.git"))
        self.assertTrue(lonely.claim("x", "tok", 3600).startswith("error:"))

    def test_a_claim_nobody_can_read_holds_until_it_is_stale_by_the_time_of_its_commit(self):
        tree = self.sh(self.repo.root, "hash-object", "-t", "tree", "-w", os.devnull)
        junk = self.sh(self.repo.root, "commit-tree", tree, "-m", "somebody was here")
        self.sh(self.repo.root, "push", "-q", "origin", "%s:refs/sdlc/claims/x" % junk)
        got = self.b.claim("x", "tok-b", 3600)
        self.assertTrue(got.startswith("other:? in ? since "), got)
        self.assertEqual(self.b.claim("x", "tok-b", -1), "taken")

    def test_the_claims_and_the_work_never_run_a_pre_push_hook_of_the_user(self):
        marker = os.path.join(self.repo.tmp, "hook-ran")
        hook = self.repo.write(".git/hooks/pre-push", "#!/bin/sh\necho ran >> '%s'\nexit 1\n" % marker)
        os.chmod(hook, 0o755)
        self.assertEqual(self.a.claim("x", "tok-a", 3600), "taken")
        self.assertEqual(self.a.claim("x", "tok-a", 3600), "ours")
        self.assertTrue(self.a.refresh_claim("x", "tok-a"))
        self.assertTrue(self.a.release_claim("x", "tok-a"))
        self.assertFalse(os.path.exists(marker))

    def test_a_clone_with_no_git_identity_can_still_claim(self):
        cfg = os.path.join(self.repo.tmp, "strict-config")
        with open(cfg, "w") as fh:
            fh.write("[user]\n\tuseConfigOnly = true\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER"))}
        env["GIT_CONFIG_GLOBAL"] = cfg
        with mock.patch.dict(os.environ, env, clear=True):
            control = subprocess.run(["git", "-C", self.repo.root, "commit", "-q", "--allow-empty", "-m", "x"],
                                     capture_output=True, text=True)
            self.assertNotEqual(control.returncode, 0)                                  # the identity really is missing
            self.assertEqual(Git(self.repo.root).claim("x", "tok-a", 3600), "taken")

    def test_a_token_with_blanks_cannot_break_the_one_line_and_is_still_recognised_as_ours(self):
        token = "tok with blanks\nand more"
        self.assertEqual(self.a.claim("x", token, 3600), "taken")
        self.assertEqual(self.message().count("\n"), 0)
        self.assertEqual(self.message().split(" ")[1], "tok_with_blanks_and_more")
        self.assertEqual(self.a.claim("x", token, 3600), "ours")
        self.assertTrue(self.a.refresh_claim("x", token))
        self.assertTrue(self.a.release_claim("x", token))
        self.assertIsNone(self.ref())

    def test_the_loser_of_two_pushes_in_the_same_moment_reads_again_instead_of_failing(self):
        """origin takes the ref from somebody else between the advertisement a push is made against and its update: git says
        `failed to update ref`, which is a lost lease, not an error."""
        hook = os.path.join(self.repo.origin, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\nif [ ! -f \"$0.done\" ]; then touch \"$0.done\"; unset GIT_QUARANTINE_PATH; "
                     "git update-ref refs/sdlc/claims/x \"$(git rev-parse refs/heads/main)\"; fi\n")
        os.chmod(hook, 0o755)
        got = self.a.claim("x", "tok-a", 3600)
        self.assertTrue(got.startswith("other:"), got)
        self.assertEqual(self.ref(), self.origin_git("rev-parse", "refs/heads/main"))

    def test_the_network_calls_are_short_and_never_ask_for_a_password(self):
        seen = []
        real = gitops.run

        def spy(cmd, **kw):
            seen.append((cmd, kw))
            return real(cmd, **kw)
        with mock.patch.object(gitops, "run", side_effect=spy):
            self.assertEqual(self.a.claim("x", "tok-a", 3600), "taken")
        with mock.patch.object(gitops, "run", side_effect=spy):
            self.assertEqual(self.a.claim("x", "tok-a", 3600), "ours")
        net = [(c, kw) for c, kw in seen if c[3] in ("ls-remote", "fetch", "push")]
        self.assertEqual({c[3] for c, _ in net}, {"ls-remote", "fetch", "push"})
        for cmd, kw in net:
            self.assertLessEqual(kw["timeout"], 20)
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
            if cmd[3] == "push":
                self.assertIn("--no-verify", cmd)


# ---------------------------------------------------------------------------------------------------------------------
class ProcessGroups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sdlc-run-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def pids(self, name="pids"):
        with open(os.path.join(self.tmp, name)) as fh:
            return [int(n) for n in fh.read().split()]

    def test_a_command_that_runs_out_of_time_takes_what_it_started_with_it(self):
        r = U.run_shell("(sleep 2; touch LATE_ARTIFACT) & (sleep 2; touch LATE_TOO); true; wait", cwd=self.tmp, timeout=0.5)
        self.assertEqual(r.rc, 124)
        self.assertEqual(r.err, "timed out after 0.5s")
        time.sleep(2.5)
        self.assertEqual(sorted(os.listdir(self.tmp)), [])

    def test_the_same_for_run_with_a_command_that_starts_a_child(self):
        script = ("import subprocess, time; p = subprocess.Popen(['sleep', '30']); "
                  "open('pids', 'w').write(str(p.pid)); time.sleep(30)")
        r = U.run([sys.executable, "-c", script], cwd=self.tmp, timeout=1.0)
        self.assertEqual(r.rc, 124)
        self.assertTrue(wait_gone(self.pids()[0]))

    def test_one_that_ignores_sigterm_is_killed_after_the_grace_and_so_is_its_whole_group(self):
        cmd = "trap '' TERM; (trap '' TERM; exec sh -c 'echo $$ >> pids; exec sleep 30') & echo $$ >> pids; wait"
        with mock.patch.object(U, "TERM_GRACE", 0.4):
            began = time.time()
            r = U.run_shell(cmd, cwd=self.tmp, timeout=1.0)
            took = time.time() - began
        self.assertEqual(r.rc, 124)
        self.assertGreaterEqual(took, 1.3)                          # the timeout, then the grace
        self.assertLess(took, 5)
        pids = self.pids()
        self.assertEqual(len(pids), 2)
        for pid in pids:
            self.assertTrue(wait_gone(pid), "process %d is still running" % pid)

    def test_one_that_stops_when_told_is_not_killed_and_does_not_wait_out_the_grace(self):
        with mock.patch.object(U, "TERM_GRACE", 5.0):
            began = time.time()
            r = U.run_shell("echo $$ >> pids; sleep 30", cwd=self.tmp, timeout=0.5)
            took = time.time() - began
        self.assertEqual(r.rc, 124)
        self.assertLess(took, 4.0)                                  # not the 5 seconds of grace
        self.assertTrue(wait_gone(self.pids()[0]))

    def test_a_leader_that_exits_on_sigterm_does_not_save_a_worker_that_ignores_it(self):
        cmd = "(trap '' TERM; exec sh -c 'echo $$ >> pids; exec sleep 30') & wait"
        with mock.patch.object(U, "TERM_GRACE", 0.4):
            r = U.run_shell(cmd, cwd=self.tmp, timeout=1.0)
        self.assertEqual(r.rc, 124)
        self.assertTrue(wait_gone(self.pids()[0]), "the worker outlived the group")

    def test_what_a_command_printed_before_it_ran_out_of_time_is_kept(self):
        r = U.run_shell("echo before; sleep 5", cwd=self.tmp, timeout=0.5)
        self.assertEqual((r.rc, r.out), (124, "before\n"))

    def test_a_command_that_finishes_is_what_it_was(self):
        r = U.run_shell("echo out; echo err >&2; exit 3", cwd=self.tmp, timeout=10)
        self.assertEqual((r.rc, r.out, r.err), (3, "out\n", "err\n"))
        r = U.run(["cat"], input="fed\n", timeout=10)
        self.assertEqual((r.rc, r.out), (0, "fed\n"))
        self.assertEqual(U.run(["pwd"], cwd=self.tmp).out.strip(), os.path.realpath(self.tmp))
        self.assertEqual(U.run(["sh", "-c", "echo $FOO"], env=dict(os.environ, FOO="bar")).out, "bar\n")
        self.assertTrue(U.run(["true"]).ok)
        self.assertEqual(U.run(["false"]).rc, 1)

    def test_a_command_that_does_not_exist_is_127(self):
        r = U.run(["no-such-command-xyz"])
        self.assertEqual(r.rc, 127)
        self.assertIn("no-such-command-xyz", r.err)

    def test_the_command_has_a_process_group_of_its_own_and_no_timeout_means_none(self):
        r = U.run_shell("ps -o pgid= -p $$", cwd=self.tmp)
        self.assertNotEqual(int(r.out.strip()), os.getpgrp())
        self.assertEqual(U.run_shell("sleep 0.3; echo done", cwd=self.tmp, timeout=None).out, "done\n")

    def test_an_interrupt_of_the_engine_itself_leaves_nothing_running(self):
        def interrupted(self_, *a, **kw):
            time.sleep(0.4)                                    # long enough for the command to have started
            raise KeyboardInterrupt
        with mock.patch.object(subprocess.Popen, "communicate", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                U.run_shell("echo $$ >> pids; sleep 30", cwd=self.tmp, timeout=10)
        time.sleep(0.3)
        for pid in self.pids():
            self.assertTrue(wait_gone(pid))


# ---------------------------------------------------------------------------------------------------------------------
class Globs(unittest.TestCase):
    CASES = [
        # a directory: itself and everything below, from the root
        ("src/", "src/a/b.py", True), ("src/", "src", True), ("src/", "srcx/a.py", False), ("src/", "x/src/a.py", False),
        ("src/", "src.py", False),
        # a directory with a glob in front of the slash
        ("**/migrations/", "migrations/0001.py", True), ("**/migrations/", "app/migrations/0001.py", True),
        ("**/migrations/", "app/db/migrations/x/y.sql", True), ("**/migrations/", "app/migrations", True),
        ("**/migrations/", "app/migrationsx/0001.py", False), ("**/migrations/", "app/my_migrations/0001.py", False),
        ("src/*/secrets/", "src/a/secrets/k.pem", True), ("src/*/secrets/", "src/a/b/secrets/k.pem", False),
        ("src/*/secrets/", "src/secrets/k.pem", False), ("src/*/secrets/", "lib/a/secrets/k.pem", False),
        ("db/**/", "db/a/b.sql", True), ("*.d/", "conf.d/x", True), ("*.d/", "a/conf.d/x", False),
        ("src//", "src/a.py", True),
        # from the repository root
        ("/infra/**", "infra/main.tf", True), ("./infra/**", "infra/main.tf", True), ("/infra/**", "x/infra/main.tf", False),
        ("/infra/", "infra/main.tf", True), ("./infra/", "x/infra/main.tf", False), ("/**/migrations/", "a/migrations/x", True),
        ("/*.tf", "main.tf", True), ("/*.tf", "deep/x/main.tf", False), ("./Makefile", "Makefile", True),
        ("./Makefile", "sub/Makefile", False), ("/src/*.py", "src/a.py", True), ("/src/*.py", "src/a/b.py", False),
        ("//infra/**", "infra/a", True), ("././infra/**", "infra/a", True),
        # a pattern that is nothing but the root matches nothing
        ("/", "a", False), ("./", "a", False), ("", "x", False), ("   ", "x", False), ("//", "a", False),
        # no slash: the file name at any depth
        ("*.tf", "main.tf", True), ("*.tf", "deep/x/main.tf", True), ("*.lock", "a/b/yarn.lock", True), ("Makefile", "a/b/Makefile", True),
        ("infra", "infra/main.tf", False), ("infra", "a/infra", True), ("*.tf", "a/main.tfx", False),
        # a slash anywhere: rooted
        ("infra/*.tf", "infra/main.tf", True), ("infra/*.tf", "infra/m/main.tf", False), ("infra/*.tf", "x/infra/main.tf", False),
        ("src/**", "src/a/b.py", True), ("src/**", "src", False), ("db/migrations/**", "db/migrations/2026/x.sql", True),
        ("db/migrations/**", "db/x.sql", False), ("**/secrets/*", "a/secrets/k.txt", True), ("**/secrets/*", "secrets/k.txt", True),
        ("**/secrets/*", "a/secrets/b/k.txt", False), ("**/*.sql", "x.sql", True), ("**/*.sql", "a/b/x.sql", True),
        ("db/**/*.sql", "db/x.sql", True), ("db/**/*.sql", "db/a/b/x.sql", True), ("db/**/*.sql", "db/x.txt", False),
        ("src/*.py", "src/a/b.py", False), ("payments/**", "./payments/a.go", True), ("a?c", "abc", True), ("a?c", "a/c", False),
        ("a*c/d", "abbc/d", True), ("a*c/d", "a/c/d", False), ("a**b", "a/x/b", True),
        # characters that mean something in a regular expression mean nothing here
        ("a.b", "axb", False), ("a.b", "a.b", True), ("(x)+[y]{z}$^|", "(x)+[y]{z}$^|", True), ("file (1).txt", "d/file (1).txt", True),
        # a path that starts with ./
        ("src/", "./src/a.py", True), ("*.py", "./a.py", True),
        # newlines and a trailing one
        ("*.py", "a.py\n", False), ("src/", "src/a\nb.py", True), ("**", "a\nb", True),
    ]

    def test_the_table(self):
        for pattern, path, want in self.CASES:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(U.glob_match(pattern, path), want)

    def test_the_pattern_is_trimmed(self):
        self.assertTrue(U.glob_match("  src/  ", "src/a.py"))
        self.assertTrue(U.glob_match("\t/infra/**\n", "infra/a"))

    def test_any_glob_takes_a_list_or_one_string_and_answers_the_pattern_as_written(self):
        self.assertEqual(U.any_glob(["a/", "*.md"], "docs/x.md"), "*.md")
        self.assertEqual(U.any_glob("*.md", "docs/x.md"), "*.md")
        self.assertEqual(U.any_glob("/infra/**", "infra/main.tf"), "/infra/**")
        self.assertEqual(U.any_glob(["**/migrations/", "x"], "a/migrations/1.py"), "**/migrations/")
        self.assertIsNone(U.any_glob("*.md", "docs/x.py"))
        self.assertIsNone(U.any_glob([], "docs/x.py"))
        self.assertIsNone(U.any_glob(None, "docs/x.py"))
        self.assertIsNone(U.any_glob("", "docs/x.py"))
        self.assertEqual(U.any_glob(["a", 5, None, "*.py"], "docs/x.py"), "*.py")

    def test_the_documented_rules(self):
        """The README's rules, one case each: `dir/` is everything under it, `*` and `?` stop at `/`, `**` any depth, a pattern
        without `/` is a name at any depth, any other is rooted."""
        readme = [("dir/", "dir/a/b", True), ("a*", "a/b", False), ("a?", "a/b", False), ("a/*", "a/b/c", False),
                  ("**/x", "a/b/x", True), ("x", "a/b/x", True), ("a/x", "b/a/x", False), ("a/**", "a/b/c", True)]
        for pattern, path, want in readme:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(U.glob_match(pattern, path), want)


if __name__ == "__main__":
    unittest.main()
