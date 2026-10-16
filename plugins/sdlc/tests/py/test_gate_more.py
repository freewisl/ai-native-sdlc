"""The merge gate, 1.2: what it holds a branch to (the recorded design, a review that ran, frozen and generated paths, the test
runner's configuration, skips, renames), how it reads manifests and paths, and that it leaves the tree as it found it. Real
throwaway repositories, no network."""
import json
import os
import unittest
from unittest import mock

from helpers import RepoCase

from sdlclib import gate as G
from sdlclib.config import Config
from sdlclib.gitops import Git
from sdlclib.workdoc import WorkDoc, problems, split_frontmatter

REVIEW = "\n## Review\nSDLC_REVIEW_TALLY important=0 nits=0\n"
NEW_CRITERIA = "R1. WHEN anything happens THE SYSTEM MAY do less."


class GateCase(RepoCase):
    """A branch sdlc/it the way the loop leaves one: the item queued on main, a design commit (status planned), a build
    commit (status built, with a review tally)."""

    def on_main(self, files, msg="more on main"):
        for rel, text in files.items():
            self.repo.write(rel, text)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", msg)

    def design(self, test_changes="none", acceptance=None):
        """The item queued on main, the branch cut and the design committed. The design commit's sha."""
        r = self.repo
        kw = {"test_changes": test_changes}
        if acceptance:
            kw["acceptance"] = acceptance
        r.write("work/it.md", r.doc_text("it", status="ready", **kw))
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "queue")
        r.git("checkout", "-q", "-b", "sdlc/it")
        r.write("work/it.md", r.doc_text("it", status="planned", **kw))
        r.git("add", "-A")
        r.git("commit", "-q", "-m", "design")
        return r.git("rev-parse", "HEAD")

    def build(self, files=None, deletes=(), test_changes="none", acceptance=None, status="built", tally=True, msg="build"):
        r = self.repo
        for rel, text in (files or {}).items():
            r.write(rel, text)
        for rel in deletes:
            r.git("rm", "-q", "--", rel)
        kw = {"test_changes": test_changes}
        if acceptance:
            kw["acceptance"] = acceptance
        r.write("work/it.md", r.doc_text("it", status=status, **kw) + (REVIEW if tally else ""))
        r.git("add", "-A")
        r.git("commit", "-q", "-m", msg)

    def gate(self, base="main", **kw):
        return G.run_gate(Config(self.repo.root), Git(self.repo.root), "it", base, **kw)

    def has(self, texts, needle):
        return any(needle in t for t in texts)

    def clean(self):
        """git status of the whole tree, untracked files listed one by one."""
        return self.repo.git("status", "--porcelain", "-uall")


# ---------------------------------------------------------------------------------------------------------------------
class DesignReference(GateCase):
    """Task 1: what the criteria and test_changes are held to."""

    def test_a_branch_that_follows_its_design_passes(self):
        sha = self.design()
        self.build(files={"src/feature.py": "x = 1\n"})
        res = self.gate(design_sha=sha)
        self.assertTrue(res.passed, res.summary())
        self.assertEqual(res.ran, ["build", "test"])

    def test_the_recorded_design_commit_is_the_reference_when_it_is_still_an_ancestor(self):
        first = self.design()
        r = self.repo
        r.write("work/it.md", r.doc_text("it", status="planned", acceptance=NEW_CRITERIA))      # refined while still planned
        r.git("commit", "-qam", "refine the design")
        second = r.git("rev-parse", "HEAD")
        self.build(acceptance=NEW_CRITERIA)
        self.assertTrue(self.has(self.gate().human, "acceptance criteria changed"))                  # the earliest planned doc
        self.assertTrue(self.has(self.gate(design_sha=first).human, "acceptance criteria changed"))
        res = self.gate(design_sha=second)                                                           # where design ended
        self.assertFalse(self.has(res.human, "acceptance criteria changed"), res.human)

    def test_a_design_sha_that_is_no_ancestor_of_head_counts_for_nothing(self):
        sha = self.design()
        self.build(acceptance=NEW_CRITERIA, test_changes="allowed", status="built")
        self.repo.git("reset", "-q", "--soft", "main")                                              # the branch's history rewritten
        self.repo.git("commit", "-q", "-m", "it: squashed")
        res = self.gate(design_sha=sha)
        self.assertNotIn(G.NO_DESIGN, res.human)                                                     # the doc as queued is the reference
        self.assertTrue(self.has(res.human, "acceptance criteria changed"), res.human)
        self.assertTrue(self.has(res.human, "test_changes changed after the design commit: none → allowed"), res.human)
        self.assertEqual(self.gate(design_sha="0" * 40).human, res.human)                            # a sha that is no commit either
        self.assertEqual(self.gate(design_sha="not a sha").human, res.human)

    def test_a_squashed_branch_is_held_to_the_queued_doc_and_its_own_test_changes_do_not_unlock_tests(self):
        self.design()
        self.build(acceptance=NEW_CRITERIA, test_changes="allowed",
                   files={"tests/test_base.py": "def test_base():\n    assert 1 + 1 == 2 or True\n"})
        self.repo.git("reset", "-q", "--soft", "main")
        self.repo.git("commit", "-q", "-m", "it: squashed")
        res = self.gate()
        self.assertFalse(res.passed)
        self.assertTrue(self.has(res.human, "acceptance criteria changed"), res.human)
        self.assertTrue(self.has(res.human, "existing tests changed, but the design said test_changes: none"), res.human)

    def test_without_a_planned_commit_the_queued_doc_is_the_reference(self):
        r = self.repo
        r.add_items("it")                                         # ready on main
        r.git("checkout", "-q", "-b", "sdlc/it")
        self.build()                                              # ready → built: no commit ever said planned
        res = self.gate()
        self.assertNotIn(G.NO_DESIGN, res.human)
        self.assertFalse(self.has(res.human, "acceptance criteria changed"), res.human)
        self.build(acceptance=NEW_CRITERIA, msg="criteria edited")
        self.assertTrue(self.has(self.gate().human, "acceptance criteria changed"))

    def test_a_branch_whose_doc_was_never_queued_and_lost_its_design_commit_has_no_design(self):
        r = self.repo
        r.git("checkout", "-q", "-b", "sdlc/it")                  # an interactive branch: the doc starts on it
        self.build()                                              # and the first commit already says built
        self.assertIn(G.NO_DESIGN, self.gate().human)

    def test_a_doc_that_was_planned_before_the_branch_was_cut_is_a_design(self):
        r = self.repo
        r.add_items(("it", {"status": "planned"}))
        r.git("checkout", "-q", "-b", "sdlc/it")
        self.build(files={"src/feature.py": "x = 1\n"})
        res = self.gate()
        self.assertNotIn(G.NO_DESIGN, res.human)
        self.assertTrue(res.passed, res.summary())

    def test_a_doc_that_was_built_at_the_merge_base_is_held_to_that_doc(self):
        self.repo.add_items(("it", {"status": "built"}))
        self.repo.git("checkout", "-q", "-b", "sdlc/it")
        self.build(acceptance=NEW_CRITERIA)
        res = self.gate()
        self.assertNotIn(G.NO_DESIGN, res.human)
        self.assertTrue(self.has(res.human, "acceptance criteria changed"), res.human)

    def test_the_frozen_check_off_means_nothing_to_report(self):
        self.repo.add_items("it")
        self.repo.git("checkout", "-q", "-b", "sdlc/it")
        self.build()
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["gate"] = {"acceptance_frozen": False}
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "config")
        res = self.gate()
        self.assertNotIn(G.NO_DESIGN, res.human)

    def test_without_a_design_test_changes_is_none_not_what_head_says(self):
        r = self.repo
        r.add_items("it")
        r.git("checkout", "-q", "-b", "sdlc/it")
        self.build(test_changes="allowed", files={"tests/test_base.py": "def test_base():\n    assert 1 == 1\n"})
        got = G.existing_tests_changed(Config(r.root), Git(r.root), "it", "main", "HEAD")
        self.assertIn("existing tests changed, but the design said test_changes: none", got)
        self.assertIn("none → allowed", G.test_changes_changed(Config(r.root), Git(r.root), "it", "main", "HEAD"))


class ReviewRan(GateCase):
    """Task 2."""

    def test_a_review_that_did_not_run_in_a_fresh_context_is_fixable(self):
        sha = self.design()
        self.build()
        for reviewed, wanted in ((False, True), (True, False), (None, False)):
            res = self.gate(design_sha=sha, reviewed=reviewed)
            self.assertEqual(self.has(res.fixable, "the review did not run in a fresh context"), wanted, reviewed)
            self.assertEqual(res.passed, not wanted, (reviewed, res.summary()))
        text = self.gate(design_sha=sha, reviewed=False).fixable[0]
        self.assertEqual(text, "the review did not run in a fresh context (no sdlc-reviewer subagent call in the build session)"
                               " — run references/review.md steps 2–4 and record the tally")

    def test_the_last_review_section_counts(self):
        sha = self.design()
        r = self.repo
        text = r.doc_text("it", status="built") + REVIEW + "\n## Learned\nx\n\n## Review\nSDLC_REVIEW_TALLY important=2 nits=1\n"
        r.write("work/it.md", text)
        r.git("commit", "-qam", "built, reviewed twice")
        self.assertIn("review: 2 Important finding(s) open", self.gate(design_sha=sha).fixable)


class ReviewOff(GateCase):
    config = {"gate": {"review": False}}

    def test_with_the_review_check_off_reviewed_checks_nothing(self):
        sha = self.design()
        self.build(tally=False)
        res = self.gate(design_sha=sha, reviewed=False)
        self.assertEqual(res.fixable, [])
        self.assertTrue(res.passed, res.summary())


class FailClosed(GateCase):
    """Task 3: a diff the gate cannot compute is a reason for a person, never "nothing changed"."""

    def doomed(self):
        sha = self.design()
        self.build(deletes=["tests/test_base.py"])
        return sha

    def test_a_base_that_does_not_exist(self):
        sha = self.doomed()
        for base in ("origin/nope", ""):
            res = self.gate(base=base, design_sha=sha)
            self.assertFalse(res.passed, base)
            reasons = [h for h in res.human if h.startswith("the gate could not compute the diff: ")]
            self.assertEqual(len(reasons), 1, res.human)                                # said once, not per check

    def test_a_renamed_default_branch_does_not_hide_a_deleted_test(self):
        sha = self.doomed()
        self.repo.git("branch", "-m", "main", "master")                                # the config still says main
        res = self.gate(base="main", design_sha=sha)
        self.assertFalse(res.passed)
        self.assertTrue(self.has(res.human, "could not compute the diff"), res.human)

    def test_a_diff_that_fails_later_is_the_same_reason(self):
        sha = self.design()
        self.build(files={"tests/test_base.py": "def test_base():\n    assert 1 == 1\n"})
        with mock.patch.object(G, "_diff", side_effect=RuntimeError("boom")):
            res = self.gate(design_sha=sha)
        self.assertIn("the gate could not compute the diff: boom", res.human)
        self.assertFalse(res.passed)

    def test_a_check_that_blows_up_is_a_reason_not_a_crash(self):
        sha = self.design()
        self.build(files={"src/feature.py": "x = 1\n"})
        with mock.patch.object(G, "new_dependencies", side_effect=ValueError("odd manifest")):
            res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "the gate could not run the dependency check: ValueError: odd manifest"), res.human)


# ---------------------------------------------------------------------------------------------------------------------
class RestoreTree(GateCase):
    """Task 4: whatever happened, the tree is as the gate found it."""
    config = {"commands": {"build": "echo regenerated >> lock.txt; mkdir -p out/deep; touch out/deep/a.txt 'café artifact.txt' "
                                    "plain-artifact.txt; rm -f tests/test_base.py", "test": "true"}}

    def prepare(self, moved=False):
        self.on_main({"lock.txt": "v1\n"})
        sha = self.design()
        self.build(files={"src/feature.py": "x = 1\n"})
        if moved:
            self.repo.git("checkout", "-q", "main")
            self.on_main({"other.txt": "main moved\n"})
            self.repo.git("checkout", "-q", "sdlc/it")
        return sha, self.repo.git("rev-parse", "HEAD")

    def check_restored(self, head):
        self.assertEqual(self.clean(), "")
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.repo.git("branch", "--show-current"), "sdlc/it")
        self.assertEqual(self.repo.read("lock.txt"), "v1\n")
        self.assertTrue(os.path.exists(self.repo.path("tests/test_base.py")))
        self.assertFalse(os.path.exists(self.repo.path("out")))                         # a directory only its files were in
        self.assertFalse(os.path.exists(self.repo.path("café artifact.txt")))
        self.assertFalse(os.path.exists(self.repo.path(".git/MERGE_HEAD")))

    def test_a_branch_that_is_up_to_date_with_main(self):
        sha, head = self.prepare()
        self.gate(design_sha=sha)
        self.check_restored(head)

    def test_a_branch_behind_main_is_trial_merged_and_restored(self):
        sha, head = self.prepare(moved=True)
        res = self.gate(design_sha=sha)
        self.assertTrue(res.passed, res.summary())
        self.check_restored(head)

    def test_a_failing_gate_restores_too(self):
        sha, head = self.prepare()
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["commands"]["lint"] = "exit 3"
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "lint fails")
        head = self.repo.git("rev-parse", "HEAD")
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.fixable, "`exit 3` failed (exit 3)"), res.fixable)
        self.check_restored(head)

    def test_a_merge_conflict_restores(self):
        self.on_main({"lock.txt": "v1\n"})
        sha = self.design()
        self.build(files={"lock.txt": "branch\n"})
        self.repo.git("checkout", "-q", "main")
        self.on_main({"lock.txt": "main\n"})
        self.repo.git("checkout", "-q", "sdlc/it")
        head = self.repo.git("rev-parse", "HEAD")
        res = self.gate(design_sha=sha)
        self.assertTrue(res.conflict)
        self.assertEqual(self.clean(), "")
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.repo.read("lock.txt"), "branch\n")
        self.assertFalse(os.path.exists(self.repo.path(".git/MERGE_HEAD")))

    def test_files_that_were_there_before_the_gate_stay(self):
        sha, head = self.prepare()
        self.repo.write("notes/keep me.txt", "mine\n")
        self.repo.write("scratch.txt", "mine\n")
        self.gate(design_sha=sha)
        self.assertEqual(self.repo.read("notes/keep me.txt"), "mine\n")
        self.assertEqual(self.repo.read("scratch.txt"), "mine\n")
        self.assertFalse(os.path.exists(self.repo.path("plain-artifact.txt")))

    def test_an_exception_restores_too(self):
        sha, head = self.prepare()

        def boom(command, cwd, timeout=None):
            with open(os.path.join(cwd, "half-done.txt"), "w") as fh:
                fh.write("x")
            with open(os.path.join(cwd, "lock.txt"), "w") as fh:
                fh.write("changed")
            raise ValueError("the command runner died")

        with mock.patch.object(G, "run_shell", side_effect=boom):
            with self.assertRaises(ValueError):
                self.gate(design_sha=sha)
        self.assertEqual(self.clean(), "")
        self.assertEqual(self.repo.read("lock.txt"), "v1\n")


class RestoreAfterTimeout(GateCase):
    config = {"commands": {"build": "touch timeout-artifact.txt; echo more >> lock.txt; exec sleep 30", "test": "true"},
              "gate": {"timeout_minutes": 0.01}}

    def test_a_command_that_ran_out_of_time(self):
        self.on_main({"lock.txt": "v1\n"})
        sha = self.design()
        self.build()
        head = self.repo.git("rev-parse", "HEAD")
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.fixable, "failed (exit 124)"), res.fixable)
        self.assertEqual(self.clean(), "")
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.repo.read("lock.txt"), "v1\n")


# ---------------------------------------------------------------------------------------------------------------------
class ProtectedPaths(GateCase):
    """Task 5."""
    config = {"protect": {"generated_paths": ["gen/"]}}

    def setUp(self):
        super().setUp()
        self.on_main({".sdlc/frozen-paths.txt": "# append-only\nmigrations/   # never edited by hand\n\n*.lock\n",
                      "migrations/0001_init.sql": "create table t(id int);\n", "gen/api.ts": "// generated\n",
                      "src/app.py": "x = 1\n", "yarn.lock": "v1\n"}, "protected files")

    def test_a_frozen_and_a_generated_file_go_to_a_person(self):
        sha = self.design()
        self.build(files={"migrations/0001_init.sql": "drop table t;\n", "gen/api.ts": "// hand edited\n", "yarn.lock": "v2\n",
                          "src/app.py": "x = 2\n"})
        res = self.gate(design_sha=sha)
        self.assertEqual([h for h in res.human if "path changed" in h],
                         ["frozen path changed: migrations/0001_init.sql (migrations/), yarn.lock (*.lock)",
                          "generated path changed: gen/api.ts (gen/)"])
        self.assertFalse(res.passed)

    def test_an_ordinary_change_is_not_one(self):
        sha = self.design()
        self.build(files={"src/app.py": "x = 2\n", "migrations-notes.txt": "n\n"})
        res = self.gate(design_sha=sha)
        self.assertFalse(self.has(res.human, "path changed"), res.human)
        self.assertTrue(res.passed, res.summary())

    def test_the_lists_are_read_as_they_were_at_the_merge_base(self):
        sha = self.design()
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["protect"]["generated_paths"] = []
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.write(".sdlc/frozen-paths.txt", "# emptied\n")
        self.build(files={"migrations/0001_init.sql": "drop table t;\n", "gen/api.ts": "// hand edited\n"})
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "frozen path changed: migrations/0001_init.sql"), res.human)
        self.assertTrue(self.has(res.human, "generated path changed: gen/api.ts"), res.human)
        self.assertTrue(self.has(res.human, "guard files changed"), res.human)

    def test_a_file_renamed_away_from_a_frozen_path_counts(self):
        sha = self.design()
        os.makedirs(self.repo.path("db"))
        self.repo.git("mv", "migrations/0001_init.sql", "db/0001_init.sql")
        self.build()
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "frozen path changed: migrations/0001_init.sql (migrations/)"), res.human)

    def test_a_deleted_frozen_file_counts(self):
        sha = self.design()
        self.build(deletes=["migrations/0001_init.sql"])
        self.assertTrue(self.has(self.gate(design_sha=sha).human, "frozen path changed: migrations/0001_init.sql"))

    def test_a_non_ascii_path(self):
        self.on_main({".sdlc/frozen-paths.txt": "données/\n", "données/été.sql": "select 1;\n"})
        sha = self.design()
        self.build(files={"données/été.sql": "select 2;\n"})
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "frozen path changed: données/été.sql (données/)"), res.human)


class ProtectedPathsCustomFile(GateCase):
    config = {"protect": {"frozen_paths_file": "docs/frozen.txt"}}

    def test_the_file_named_in_the_config(self):
        self.on_main({"docs/frozen.txt": "vendor/\n", "vendor/lib.js": "1\n"})
        sha = self.design()
        self.build(files={"vendor/lib.js": "2\n"})
        self.assertTrue(self.has(self.gate(design_sha=sha).human, "frozen path changed: vendor/lib.js (vendor/)"))


# ---------------------------------------------------------------------------------------------------------------------
# (name, manifest path, before, after, the names that are new)
DEP_CASES = [
    ("pep 621 single-line array", "pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27"]\n',
     '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "rich"]\n', ["rich"]),
    ("pep 621 optional-dependencies inline", "pyproject.toml", '[project.optional-dependencies]\ndev = ["pytest"]\n',
     '[project.optional-dependencies]\ndev = ["pytest", "black>=24"]\n', ["black"]),
    ("pep 735 dependency-groups, multi-line", "pyproject.toml", '[dependency-groups]\ndev = [\n  "pytest",\n]\n',
     '[dependency-groups]\ndev = [\n  "pytest",\n  "hypothesis",\n  {include-group = "lint"},\n]\n', ["hypothesis"]),
    ("build-system requires", "pyproject.toml", '[build-system]\nrequires = ["setuptools"]\n',
     '[build-system]\nrequires = ["setuptools", "setuptools-scm[toml]>=8"]\n', ["setuptools-scm"]),
    ("pyproject name normalised", "pyproject.toml", '[project]\ndependencies = ["Foo_Bar>=1"]\n',
     '[project]\ndependencies = ["foo.bar>=2"]\n', []),
    ("poetry table", "pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.11"\nrequests = "^2"\n',
     '[tool.poetry.dependencies]\npython = "^3.12"\nrequests = "^2"\nhttpx = { version = "^0.27", extras = ["http2"] }\n', ["httpx"]),
    ("poetry group", "pyproject.toml", '[tool.poetry.group.dev.dependencies]\npytest = "^8"\n',
     '[tool.poetry.group.dev.dependencies]\npytest = "^8"\nblack = "^24"\n', ["black"]),
    ("poetry sub-table", "pyproject.toml", '[tool.poetry.dependencies]\npython = "^3.11"\n',
     '[tool.poetry.dependencies]\npython = "^3.11"\n\n[tool.poetry.dependencies.rich]\nversion = "^13"\nextras = ["jupyter"]\n', ["rich"]),
    ("poetry multi-line inline table is not a list of dependencies", "pyproject.toml",
     '[tool.poetry.dependencies]\nfoo = {\n  version = "1",\n  extras = ["a"]\n}\n',
     '[tool.poetry.dependencies]\nfoo = {\n  version = "2",\n  extras = ["a", "b"],\n  python = "<3.9"\n}\n', []),
    ("poetry bump", "pyproject.toml", '[tool.poetry.dependencies]\nrequests = "^2"\n', '[tool.poetry.dependencies]\nrequests = "^2.31"\n', []),
    ("classifiers are not dependencies", "pyproject.toml", '[project]\nclassifiers = [\n  "Programming Language :: Python",\n]\n',
     '[project]\nclassifiers = [\n  "Programming Language :: Python",\n  "License :: OSI Approved",\n]\n', []),
    ("ruff rules are not dependencies", "pyproject.toml", '[tool.ruff.lint]\nselect = [\n  "E",\n]\n',
     '[tool.ruff.lint]\nselect = [\n  "E",\n  "UP",\n]\nignore = ["E501"]\n', []),
    ("dynamic dependencies file", "pyproject.toml", '[tool.setuptools.dynamic]\ndependencies = {file = ["requirements.txt"]}\n',
     '[tool.setuptools.dynamic]\ndependencies = {file = ["requirements.txt", "requirements-extra.txt"]}\n', []),
    ("cargo table form", "Cargo.toml", '[dependencies]\nserde = "1"\n', '[dependencies]\nserde = "1"\n\n[dependencies.tokio]\nversion = "1"\n',
     ["tokio"]),
    ("cargo dev table form", "Cargo.toml", '[dev-dependencies]\nserde = "1"\n', '[dev-dependencies]\nserde = "1"\n\n[dev-dependencies.criterion]\nversion = "0.5"\n',
     ["criterion"]),
    ("cargo workspace dependencies", "Cargo.toml", '[workspace.dependencies]\nserde = "1"\n',
     '[workspace.dependencies]\nserde = "1"\nanyhow = "1"\n', ["anyhow"]),
    ("cargo target dependencies", "Cargo.toml", '[target.\'cfg(unix)\'.dependencies]\nlibc = "0.2"\n',
     '[target.\'cfg(unix)\'.dependencies]\nlibc = "0.2"\nnix = "0.29"\n', ["nix"]),
    ("cargo workspace = true is the workspace's", "Cargo.toml", '[dependencies]\nserde = "1"\n',
     '[dependencies]\nserde = "1"\nanyhow = { workspace = true }\nlog.workspace = true\n', []),
    ("cargo bump", "Cargo.toml", '[dependencies]\nserde = "1.0.1"\n', '[dependencies]\nserde = { version = "1.0.9", features = ["derive"] }\n', []),
    ("requirements in a requirements/ directory", "requirements/base.txt", "django\n", "django\nrequests\n", ["requests"]),
    ("requirements-dev.txt", "requirements-dev.txt", "pytest\n", "pytest\nblack==24.1\n", ["black"]),
    ("dev-requirements.txt", "dev-requirements.txt", "pytest\n", "pytest\nblack==24.1\n", ["black"]),
    ("requirements -e git+", "requirements.txt", "django\n", "django\n-e git+https://github.com/evil/pkg.git#egg=pkg\n", ["pkg"]),
    ("requirements -e git+ without egg", "requirements.txt", "django\n", "django\n-e git+https://github.com/evil/pkg.git@v1\n",
     ["git+https://github.com/evil/pkg.git"]),
    ("requirements -e git+ moved to another ref", "requirements.txt", "-e git+https://github.com/a/pkg.git@v1\n",
     "-e git+https://github.com/a/pkg.git@v2\n", []),
    ("requirements -e of a local path is this repository", "requirements.txt", "django\n", "django\n-e .\n-e ../sibling\n", []),
    ("requirements URL", "requirements.txt", "https://x.org/a-1.0.whl\n", "https://x.org/a-1.0.whl\nhttps://evil.org/b-1.0.whl\n", ["b"]),
    ("requirements URL bump", "requirements.txt", "https://x.org/a-1.0.whl\n", "https://x.org/a-1.1.whl\n", []),
    ("requirements name @ url", "requirements.txt", "django\n", "django\nmypkg @ https://evil.org/mypkg.tar.gz\n", ["mypkg"]),
    ("requirements bump", "requirements.txt", "flask==2.0\n", "flask==2.1 ; python_version >= '3.8'\n", []),
    ("requirements options and includes", "requirements.txt", "flask\n", "flask\n-r base.txt\n--index-url https://pypi.org/simple\n"
     "--hash=sha256:abc\n", []),
    ("requirements comments", "requirements.txt", "flask\n", "flask\n# requests\nflask  # again\n", []),
    ("requirements file that is prose", "docs/requirements.txt", "The system shall do x.\n", "The system shall do x.\nIt shall also do y.\n", []),
    ("package.json with a byte-order mark", "package.json", '\ufeff{"dependencies": {"react": "1"}}',
     '\ufeff{"dependencies": {"react": "1", "left-pad": "1"}}', ["left-pad"]),
    ("package.json bump", "package.json", '{"dependencies": {"react": "1"}}', '{"dependencies": {"react": "2"}}', []),
    ("gradle debug/release/kapt/ksp and platform", "build.gradle.kts", "dependencies {\n}\n",
     'dependencies {\n  debugImplementation("com.squareup.leakcanary:leakcanary-android:2.14")\n'
     '  releaseImplementation("com.x:release-only:1")\n  kapt("com.google.dagger:dagger-compiler:2.5")\n'
     '  ksp("androidx.room:room-compiler:2.6")\n  implementation(platform("com.google.firebase:firebase-bom:33.0.0"))\n}\n',
     ["androidx.room:room-compiler", "com.google.dagger:dagger-compiler", "com.google.firebase:firebase-bom",
      "com.squareup.leakcanary:leakcanary-android", "com.x:release-only"]),
    ("gradle groovy forms", "build.gradle", "dependencies {\n}\n",
     "dependencies {\n  implementation 'com.a:one:1'\n  testImplementation group: 'com.b', name: 'two', version: '2'\n"
     "  classpath \"com.c:three:3\"\n  implementation platform('com.d:four:4')\n}\n",
     ["com.a:one", "com.b:two", "com.c:three", "com.d:four"]),
    ("gradle bump", "build.gradle.kts", 'dependencies { implementation("com.squareup.okhttp3:okhttp:4.11.0") }\n',
     'dependencies { implementation("com.squareup.okhttp3:okhttp:4.12.0") }\n', []),
    ("gradle commented-out", "build.gradle.kts", "dependencies {\n}\n",
     '// implementation("com.x:old:1")\n/* implementation("com.y:older:1") */\ndependencies {\n}\n', []),
    ("gradle catalog alias", "build.gradle.kts", "dependencies {\n}\n", "dependencies {\n  implementation(libs.androidx.core)\n  api(platform(libs.okhttp.bom))\n}\n",
     ["libs.androidx.core", "libs.okhttp.bom"]),
    ("version catalog libraries", "gradle/libs.versions.toml", '[versions]\nkotlin = "1.9"\n[libraries]\ncore = { module = "androidx.core:core-ktx", version = "1" }\n',
     '[versions]\nkotlin = "1.9"\nroom = "2.6"\n[libraries]\ncore = { module = "androidx.core:core-ktx", version = "1" }\n'
     'room = { group = "androidx.room", name = "room-ktx", version.ref = "room" }\nshort = "com.x:short:1"\n'
     '[plugins]\nkotlin = { id = "org.jetbrains.kotlin.android", version.ref = "kotlin" }\n', ["androidx.room:room-ktx", "com.x:short"]),
    ("pubspec four-space indent", "pubspec.yaml", "dependencies:\n    http: ^1.0\n", "dependencies:\n    http: ^1.0\n    dio: ^5.0\n", ["dio"]),
    ("pubspec nested keys are not packages", "pubspec.yaml", "name: x\ndependencies:\n  flutter:\n    sdk: flutter\n",
     "name: x\ndependencies:\n  flutter:\n    sdk: flutter\n  intl:\n    git:\n      url: https://x.org/intl.git\ndev_dependencies:\n  test: any\n",
     ["intl", "test"]),
    ("setup.py install_requires", "setup.py", "setup(install_requires=['a'])\n", "setup(install_requires=['a', 'b>=2'])\n", ["b"]),
    ("setup.py multi-line, extras and a comment", "setup.py", "setup(\n    install_requires=[\n        'a',\n    ],\n)\n",
     "setup(\n    install_requires=[\n        'a',  # needed\n        'b',\n    ],\n    extras_require={'dev': ['pytest', 'black']},\n)\n",
     ["b", "black", "pytest"]),
    ("go.mod indirect lines that a bump pulled in are not new", "go.mod", "module m\nrequire github.com/a/b v1.0.0\n",
     "module m\nrequire github.com/a/b v1.1.0\nrequire github.com/c/d v0.1.0 // indirect\n", []),
    ("go.mod indirect block", "go.mod", "module m\nrequire (\n\tgithub.com/a/b v1.0.0\n)\n",
     "module m\nrequire (\n\tgithub.com/a/b v1.1.0\n)\nrequire (\n\tgithub.com/c/d v0.1.0 // indirect\n)\n", []),
    ("go.mod a module that was indirect and is now direct", "go.mod", "module m\nrequire (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/c/d v0.1.0 // indirect\n)\n",
     "module m\nrequire (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/c/d v0.1.0\n)\n", []),
    ("go.mod a module that was direct and is now indirect", "go.mod", "module m\nrequire github.com/a/b v1.0.0\n",
     "module m\nrequire github.com/a/b v1.0.0 // indirect\n", []),
    ("go.mod a new direct module", "go.mod", "module m\nrequire github.com/a/b v1.0.0\n",
     "module m\nrequire github.com/a/b v1.0.0\nrequire github.com/e/f v0.2.0\n", ["github.com/e/f"]),
    ("go.mod a new module in a block", "go.mod", "module m\nrequire (\n\tgithub.com/a/b v1.0.0\n)\n",
     "module m\nrequire (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/E/F v0.2.0\n)\n", ["github.com/E/F"]),
    ("go.mod replace to a fork", "go.mod", "module m\nrequire github.com/a/b v1.0.0\n",
     "module m\nrequire github.com/a/b v1.0.0\nreplace github.com/a/b => github.com/evil/b v1.0.1\n", ["github.com/evil/b"]),
    ("go.mod replace to a local path", "go.mod", "module m\nrequire github.com/a/b v1.0.0\n",
     "module m\nrequire github.com/a/b v1.0.0\nreplace github.com/a/b => ../b\n", []),
    ("pom.xml commented-out dependency", "pom.xml", "<dependencies></dependencies>\n",
     "<dependencies><!-- <dependency><groupId>a</groupId><artifactId>b</artifactId></dependency> --></dependencies>\n", []),
    ("Pipfile", "Pipfile", "[packages]\nrequests = \"*\"\n", "[packages]\nrequests = \"*\"\nflask = {version = \"*\"}\n[dev-packages]\npytest = \"*\"\n",
     ["flask", "pytest"]),
]


class Dependencies(unittest.TestCase):
    """Task 6: the forms the gate missed, and the lines it took for dependencies that were none."""

    def test_the_table(self):
        for name, path, before, after, want in DEP_CASES:
            self.assertTrue(G.is_manifest(path), name)
            self.assertEqual(G.added_deps(path, before, after), want, name)

    def test_a_new_manifest_names_everything_as_new(self):
        self.assertEqual(G.added_deps("requirements.txt", None, "flask\nrequests\n"), ["flask", "requests"])

    def test_a_renamed_manifest_is_compared_with_where_it_came_from(self):
        self.assertEqual(G.added_deps("requirements/base.txt", "flask\n", "flask\nrequests\n", old_path="requirements.txt"),
                         ["requests"])

    def test_manifest_names(self):
        for path in ("requirements.txt", "requirements-dev.txt", "requirements.in", "requirements/dev.txt", "app/requirements/base.in",
                     "test-requirements.txt", "setup.py", "pyproject.toml", "gradle/libs.versions.toml", "a/b/package.json"):
            self.assertTrue(G.is_manifest(path), path)
        for path in ("src/package.ts", "requirements.md", "docs/requirements/overview.md", "notes.txt", "src/setup.pyc"):
            self.assertFalse(G.is_manifest(path), path)


class DependenciesOnABranch(GateCase):
    def test_new_manifests_and_non_ascii_directories(self):
        pins = "flask==3.0\ndjango==5.0\ncelery==5.3\nredis==5.0\ngunicorn==21.2\n"
        self.on_main({"frontend/über/package.json": '{"dependencies": {"react": "1"}}', "requirements.txt": pins})
        sha = self.design()
        self.repo.git("mv", "requirements.txt", "requirements-prod.txt")
        self.build(files={"frontend/über/package.json": '{"dependencies": {"react": "2", "left-pad": "1"}}',
                          "requirements-prod.txt": pins + "requests==2.32\n", "tools/Gemfile": "gem 'rake'\n"})
        found = G.new_dependencies(Git(self.repo.root), "main", "HEAD")
        self.assertEqual(found, ["left-pad (frontend/über/package.json)", "requests (requirements-prod.txt)",
                                 "rake (tools/Gemfile)"])
        res = self.gate(design_sha=sha)
        self.assertEqual([h for h in res.human if h.startswith("new dependency")],
                         ["new dependency: left-pad (frontend/über/package.json)", "new dependency: requests (requirements-prod.txt)",
                          "new dependency: rake (tools/Gemfile)"])

    def test_a_bump_is_no_new_dependency(self):
        sha = self.design()
        self.build(files={"package.json": json.dumps({"name": "x", "dependencies": {"react": "^19.0.0"}})})
        res = self.gate(design_sha=sha)
        self.assertEqual([h for h in res.human if h.startswith("new dependency")], [])


# ---------------------------------------------------------------------------------------------------------------------
TEST_CONFIGS = ["conftest.py", "tests/conftest.py", "a/b/conftest.py", "pytest.ini", "web/jest.config.js", "jest.config.ts",
                "vitest.config.mts", "vitest.workspace.ts", "karma.conf.js", ".mocharc", ".mocharc.json", "pkg/.mocharc.yml",
                "playwright.config.ts", "cypress.config.js", "phpunit.xml", "phpunit.xml.dist", "app/phpunit.xml"]
NOT_TEST_CONFIGS = ["src/main.py", "myconftest.py", "conftest.pyc", "pytest.ini.bak", "docs/jest.config", "x/.mocharcx",
                    "phpunit.xml.orig", "src/vitest.py", "work/conftest.py"]


class TestRunnerConfig(GateCase):
    """Task 7."""

    def test_the_patterns(self):
        cfg = Config(self.repo.root)
        for p in TEST_CONFIGS:
            self.assertTrue(G.is_test_path(cfg, p), p)
            self.assertTrue(G.TEST_CONFIG_RE.search(p), p)
        for p in NOT_TEST_CONFIGS:
            self.assertFalse(G.is_test_path(cfg, p), p)
        self.assertEqual(G.TEST_CONFIG_GLOBS, ("conftest.py", "pytest.ini", "jest.config.*", "vitest.config.*", "vitest.workspace.*",
                                               "karma.conf.*", ".mocharc", ".mocharc.*", "playwright.config.*", "cypress.config.*",
                                               "phpunit.xml", "phpunit.xml.dist"))

    def test_a_test_paths_list_without_auto_does_not_include_them(self):
        r = self.repo
        cfg = json.loads(r.read(".sdlc/config.json"))
        cfg["protect"] = {"test_paths": ["only/here/"]}
        r.write(".sdlc/config.json", json.dumps(cfg))
        cfg = Config(r.root)
        self.assertFalse(G.is_test_path(cfg, "conftest.py"))
        self.assertTrue(G.is_test_path(cfg, "only/here/a.py"))

    def test_existing_configuration_files_are_locked_like_tests(self):
        self.on_main({"conftest.py": "import pytest\n", "pytest.ini": "[pytest]\naddopts = -q\n", "web/jest.config.js": "module.exports = {}\n"})
        sha = self.design()
        self.build(files={"conftest.py": "import pytest\n\n@pytest.fixture(autouse=True)\ndef swallow():\n    pass\n",
                          "pytest.ini": "[pytest]\naddopts = --ignore=tests\n", "web/jest.config.js": "module.exports = { testMatch: [] }\n"})
        res = self.gate(design_sha=sha)
        got = [h for h in res.human if h.startswith("existing tests changed")]
        self.assertEqual(len(got), 1)
        for p in ("conftest.py (modified)", "pytest.ini (modified)", "web/jest.config.js (modified)"):
            self.assertIn(p, got[0])

    def test_a_new_conftest_is_free_and_test_changes_allowed_lifts_the_lock(self):
        self.on_main({"conftest.py": "import pytest\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed", files={"conftest.py": "import pytest\n# touched\n", "tests/conftest.py": "import pytest\n"})
        res = self.gate(design_sha=sha)
        self.assertTrue(res.passed, res.summary())

    def test_deleting_a_configuration_file_is_a_deleted_test_file(self):
        self.on_main({"pytest.ini": "[pytest]\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed", deletes=["pytest.ini"])
        self.assertIn("test integrity: test file deleted: pytest.ini", self.gate(design_sha=sha).human)

    def test_a_changed_test_script_is_a_person_s_even_when_tests_may_change(self):
        self.on_main({"package.json": json.dumps({"name": "x", "dependencies": {"react": "^18.0.0"}, "scripts": {"test": "jest"}})})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed",
                   files={"package.json": json.dumps({"name": "x", "dependencies": {"react": "^18.0.0"}, "scripts": {"test": "true"}})})
        res = self.gate(design_sha=sha)
        self.assertIn("test runner configuration changed: package.json", res.human)

    def test_pretest_and_posttest_count_other_scripts_do_not(self):
        base = {"name": "x", "scripts": {"test": "jest", "build": "tsc"}}
        self.on_main({"package.json": json.dumps(base), "lib/package.json": json.dumps(base)})
        sha = self.design()
        edited = dict(base, scripts={"test": "jest", "build": "tsc -p .", "lint": "eslint ."})
        self.build(files={"package.json": json.dumps(edited), "lib/package.json": json.dumps(dict(base, scripts=dict(base["scripts"], pretest="rm -rf tests")))})
        res = self.gate(design_sha=sha)
        self.assertEqual([h for h in res.human if h.startswith("test runner")], ["test runner configuration changed: lib/package.json"])

    def test_a_package_json_with_a_byte_order_mark(self):
        self.on_main({"package.json": "\ufeff" + json.dumps({"scripts": {"test": "jest"}})})
        sha = self.design()
        self.build(files={"package.json": "\ufeff" + json.dumps({"scripts": {"test": "true"}})})
        self.assertIn("test runner configuration changed: package.json", self.gate(design_sha=sha).human)

    def test_the_pytest_table_of_a_pyproject(self):
        self.on_main({"pyproject.toml": '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\ntestpaths = [\n  "tests",\n]\n'})
        sha = self.design()
        cases = [
            ('[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q -k \'not slow\'"\ntestpaths = ["tests"]\n', True),
            ('[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\ntestpaths = ["tests", "more"]\n', True),
            ('[project]\nname = "x"\n', True),                                                                  # the table is gone
            ('[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts   =   "-q"   # quiet\ntestpaths = ["tests"]\n', False),
            ('[project]\nname = "x"\n\n[tool.ruff]\nline-length = 120\n\n[tool.pytest.ini_options]\naddopts = "-q"\ntestpaths = [\n "tests"]\n', False),
        ]
        for text, flagged in cases:
            self.repo.write("pyproject.toml", text)
            self.repo.git("commit", "-qam", "edit pyproject")
            res = self.gate(design_sha=sha)
            self.assertEqual(self.has(res.human, "test runner configuration changed: pyproject.toml"), flagged, text)

    def test_a_new_package_json_is_free(self):
        sha = self.design()
        self.build(files={"sub/package.json": json.dumps({"scripts": {"test": "jest"}})})
        res = self.gate(design_sha=sha)
        self.assertFalse(self.has(res.human, "test runner"), res.human)


# ---------------------------------------------------------------------------------------------------------------------
SKIP_LINES = [
    'pytestmark = pytest.mark.skip(reason="flaky")', "pytestmark = [pytest.mark.skip]", "pytestmark = [pytest.mark.slow, pytest.mark.xfail]",
    '        self.skipTest("later")', '    raise unittest.SkipTest("x")', "    raise SkipTest()", "pytest.skip('no', allow_module_level=True)",
    "@pytest.mark.skipif(True, reason='x')", "@unittest.skip('x')", "@skip('x')", "@unittest.expectedFailure",
    "#[ignore]", '#[ignore = "slow"]', "    #[ignore]",
    '    t.Skip("later")', "    t.SkipNow()", '    t.Skipf("no %s", x)', "    b.Skip()",
    'skip "not now"', "  skip", "skip('x')", 'xcontext "x" do', 'xdescribe "x" do', 'xit "does it" do', "xit('x', () => {})", "xtest('x', () => {})",
    'pending "later"', 'it "x", skip: true do', 'it "x", :skip do', 'it "x", pending: "why" do',
    "        $this->markTestSkipped();", '$this->markTestIncomplete("todo");',
    "@Disabled", '@Disabled("flaky")', "@DisabledOnOs(OS.WINDOWS)", "@Ignore", '@Ignore("x")', "[Ignore]", "@Test(enabled = false)",
    "try XCTSkipIf(true)", 'throw XCTSkip("x")',
    "test.skip('x', () => {})", "it.skip('x')", "describe.only(", "it.skip.each([1])('x', () => {})", "test.fixme('x')", "test.todo('x')",
    "fit('x', () => {})", "fdescribe('x', () => {})",
]
NO_SKIP_LINES = ["const skipped = true", "skip_value = 3", "pending = []", "pending_count += 1", "foo.Skip(5)", "t.Run('x', f)",
                 "@pytest.mark.skipif(sys.platform == 'win32', reason='x')", "@pytest.mark.slow", "assert skip_count == 1",
                 "enabled = false", "describe('skipping', () => {})"]


class Skips(GateCase):
    """Task 8."""

    def test_the_skip_forms(self):
        for line in SKIP_LINES:
            self.assertTrue(G.SKIP_RE.search(line), line)
        for line in NO_SKIP_LINES:
            self.assertFalse(G.SKIP_RE.search(line), line)

    def test_the_old_forms_still_count(self):
        for line in ("it.skip('x')", "describe.only(", "@pytest.mark.skip", "@pytest.mark.xfail(reason='x')", "@Disabled", 't.Skip("later")',
                     "try XCTSkipIf(true)", "xit('x'", "test.todo('x')"):
            self.assertTrue(G.SKIP_RE.search(line), line)

    def test_test_definitions(self):
        for line in ("#[test]", "    #[tokio::test]", "#[tokio::test(flavor = \"multi_thread\")]", "#[test_case(1)]", "#[rstest]",
                     "func testLogin() throws {", "func test_login() {", "func test() {", "func test1() {", "    func testLogin() throws {",
                     "def test_x():", "func TestParse(t *testing.T) {", "@Test"):
            self.assertTrue(G.TEST_DEF_RE.search(line), line)
        for line in ("fn helper() {", "func testing() {", "func tester() {", "#[derive(Debug)]", "#[cfg(test)]"):
            self.assertFalse(G.TEST_DEF_RE.search(line), line)

    def test_comments_count_for_nothing(self):
        diff = ("--- a/t.py\n+++ b/t.py\n@@ -1,2 +1,4 @@\n-    assert x == 1\n+    # assert x == 1\n+    # self.skipTest('later')\n"
                "+    // @Disabled\n+    * @Ignore\n+    pass\n")
        n = G._count("tests/t.py", diff)
        self.assertEqual((n.removed_asserts, n.added_asserts, n.skips), (1, 0, []))

    def test_content_that_looks_like_a_file_header_is_content(self):
        diff = "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n---- assert 1 == 1\n++++ assert 2 == 2\n"
        n = G._count("t.py", diff)
        self.assertEqual((n.removed_asserts, n.added_asserts), (1, 1))

    def test_a_rust_test_is_the_attribute_not_the_function_name(self):
        diff = "@@ -1,2 +1,2 @@\n-#[test]\n-fn test_a() {\n+#[test]\n+fn b() {\n"
        n = G._count("src/lib.rs", diff)
        self.assertEqual((n.removed_defs, n.added_defs), (1, 1))
        diff = "@@ -1,4 +1,2 @@\n-#[test]\n-fn test_a() {\n-}\n-#[tokio::test]\n"
        n = G._count("src/lib.rs", diff)
        self.assertEqual((n.removed_defs, n.added_defs), (2, 0))

    def test_a_skip_in_an_existing_test_file_under_allowed(self):
        self.on_main({"tests/test_core.py": "import pytest\n\n\ndef test_a():\n    assert 1\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed",
                   files={"tests/test_core.py": 'import pytest\n\npytestmark = [pytest.mark.slow, pytest.mark.skip(reason="wip")]\n\n\ndef test_a():\n    assert 1\n'})
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "test integrity: skip/only added in tests/test_core.py"), res.human)

    def test_rust_cases_and_ignores(self):
        self.on_main({"tests/parse.rs": "#[test]\nfn parses() {\n    assert_eq!(1, 1);\n}\n\n#[test]\nfn rejects() {\n    assert!(true);\n}\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed", files={"tests/parse.rs": "#[test]\nfn parses() {\n    assert_eq!(1, 1);\n}\n",
                                                   "tests/new.rs": "#[test]\n#[ignore]\nfn later() {}\n"})
        res = self.gate(design_sha=sha)
        self.assertIn("test integrity: 1 test case(s) removed from tests/parse.rs", res.human)
        self.assertIn("test integrity: 1 assertion(s) removed from tests/parse.rs", res.human)
        self.assertIn("test integrity: skip/only added in tests/new.rs: #[ignore]", res.human)

    def test_swift_cases(self):
        swift = "Sources/AppTests/LoginTests.swift"
        self.on_main({swift: "func testLogin() {\n    XCTAssertTrue(true)\n}\nfunc testLogout() {\n    XCTAssertTrue(true)\n}\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed", files={swift: "func testLogin() {\n    XCTAssertTrue(true)\n}\n"})
        self.assertIn("test integrity: 1 test case(s) removed from " + swift, self.gate(design_sha=sha).human)


# ---------------------------------------------------------------------------------------------------------------------
class Renames(GateCase):
    """Task 9."""

    MORE = "".join("def test_%s():\n    assert f(%d) == %d\n\n\n" % (name, n, n) for n, name in enumerate(("one", "two", "three", "four"), 1))
    LESS = "".join("def test_%s():\n    assert f(%d) == %d\n\n\n" % (name, n, n) for n, name in enumerate(("one", "two", "three"), 1))

    def setUp(self):
        super().setUp()
        self.on_main({"tests/test_more.py": self.MORE})

    def test_a_renamed_test_file_with_its_cases_intact_is_not_a_deleted_one(self):
        sha = self.design(test_changes="allowed")
        self.repo.git("mv", "tests/test_more.py", "tests/test_more_renamed.py")
        self.build(test_changes="allowed")
        self.assertEqual(G.test_integrity(Config(self.repo.root), Git(self.repo.root), "main", "HEAD"), [])
        res = self.gate(design_sha=sha)
        self.assertTrue(res.passed, res.summary())

    def test_a_case_dropped_in_the_same_commit_as_the_rename_is_still_caught(self):
        sha = self.design(test_changes="allowed")
        self.repo.git("mv", "tests/test_more.py", "tests/test_more_renamed.py")
        self.build(test_changes="allowed", files={"tests/test_more_renamed.py": self.LESS + "# def test_four ... gone\n"})
        issues = G.test_integrity(Config(self.repo.root), Git(self.repo.root), "main", "HEAD")
        self.assertIn("1 test case(s) removed from tests/test_more_renamed.py", issues)
        self.assertIn("1 assertion(s) removed from tests/test_more_renamed.py", issues)
        self.assertFalse(self.has(issues, "deleted"), issues)

    def test_a_skip_added_while_renaming(self):
        sha = self.design(test_changes="allowed")
        self.repo.git("mv", "tests/test_more.py", "tests/test_more_renamed.py")
        self.build(test_changes="allowed", files={"tests/test_more_renamed.py": "import pytest\n\n\n@pytest.mark.skip\n" + self.MORE})
        issues = G.test_integrity(Config(self.repo.root), Git(self.repo.root), "main", "HEAD")
        self.assertTrue(self.has(issues, "skip/only added in tests/test_more_renamed.py"), issues)

    def test_a_test_file_renamed_to_something_that_is_no_test_is_a_deleted_one(self):
        sha = self.design(test_changes="allowed")
        os.makedirs(self.repo.path("src"), exist_ok=True)
        self.repo.git("mv", "tests/test_more.py", "src/more.py")
        self.build(test_changes="allowed")
        issues = G.test_integrity(Config(self.repo.root), Git(self.repo.root), "main", "HEAD")
        self.assertEqual(issues, ["test file deleted: tests/test_more.py (renamed to src/more.py, which is not a test file)"])

    def test_with_test_changes_none_a_rename_is_still_an_existing_test_gone(self):
        sha = self.design()
        self.repo.git("mv", "tests/test_more.py", "tests/test_more_renamed.py")
        self.build()
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "tests/test_more.py (deleted or renamed)"), res.human)
        self.assertFalse(self.has(res.human, "test file deleted"), res.human)

    def test_a_non_test_file_renamed_into_a_test_directory_is_a_new_test_file(self):
        self.on_main({"src/helper.py": "def test_like():\n    pass\n"})
        sha = self.design()
        self.repo.git("mv", "src/helper.py", "tests/test_helper.py")
        self.build()
        self.assertEqual(G.test_integrity(Config(self.repo.root), Git(self.repo.root), "main", "HEAD"), [])


# ---------------------------------------------------------------------------------------------------------------------
class OddPaths(GateCase):
    """Task 11: spaces, tabs, quotes, non-ASCII letters and glob characters in names."""
    NAMES = ["tests/test café déjà vu.py", "tests/test\tx.py", 'tests/test"q".py', "tests/test_ünï.py", "tests/test[1].py", "tests/test*.py"]

    def test_changes_come_back_as_the_names_themselves(self):
        self.on_main({n: "def test_a():\n    assert 1\n" for n in self.NAMES})
        self.design()
        self.build(deletes=self.NAMES[:2], files={self.NAMES[2]: "def test_a():\n    assert 2\n", "données/été.txt": "x\n"})
        got = {(c.status, c.path) for c in G.changes(Git(self.repo.root), "main", "HEAD")}
        self.assertIn(("D", self.NAMES[0]), got)
        self.assertIn(("D", self.NAMES[1]), got)
        self.assertIn(("M", self.NAMES[2]), got)
        self.assertIn(("A", "données/été.txt"), got)

    def test_deleted_and_weakened_tests_with_odd_names_are_caught(self):
        self.on_main({n: "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n" for n in self.NAMES})
        sha = self.design()
        self.build(deletes=self.NAMES[:2], files={n: "def test_a():\n    assert 1\n" for n in self.NAMES[2:]})
        res = self.gate(design_sha=sha)
        for n in self.NAMES[:2]:
            self.assertIn("test integrity: test file deleted: %s" % n, res.human)
        for n in self.NAMES[2:]:
            self.assertIn("test integrity: 1 test case(s) removed from %s" % n, res.human)
            self.assertIn("test integrity: 1 assertion(s) removed from %s" % n, res.human)
        got = [h for h in res.human if h.startswith("existing tests changed")][0]
        for n in self.NAMES:
            self.assertIn(n, got)

    def test_a_glob_character_in_a_name_is_just_a_character(self):
        self.on_main({"tests/test1.py": "def test_a():\n    assert 1\n", "tests/test[1].py": "def test_a():\n    assert 1\n"})
        sha = self.design(test_changes="allowed")
        self.build(test_changes="allowed", files={"tests/test[1].py": "def test_a():\n    pass\n"})
        res = self.gate(design_sha=sha)
        self.assertIn("test integrity: 1 assertion(s) removed from tests/test[1].py", res.human)

    def test_a_skip_in_a_new_file_with_a_space_in_its_name(self):
        sha = self.design()
        self.build(files={"tests/new thing.test.js": "it.skip('x', () => {})\n"})
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "skip/only added in tests/new thing.test.js"), res.human)

    def test_risk_paths_and_guard_files_with_odd_names(self):
        r = self.repo
        cfg = json.loads(r.read(".sdlc/config.json"))
        cfg["risk"] = {"human": ["données/"]}
        r.write(".sdlc/config.json", json.dumps(cfg))
        r.git("commit", "-qam", "risk")
        sha = self.design()
        self.build(files={"données/été sec.sql": "x\n", "checks/héllo check": "#!/bin/sh\n"})
        res = self.gate(design_sha=sha)
        self.assertTrue(self.has(res.human, "risk path: données/été sec.sql (données/)"), res.human)
        self.assertTrue(self.has(res.human, "guard files changed") and self.has(res.human, "checks/héllo check (added)"), res.human)


# ---------------------------------------------------------------------------------------------------------------------
class WorkDocParsing(unittest.TestCase):
    """Task 10."""

    BODY = "## Goal\ng\n"

    def test_a_byte_order_mark_in_front_of_the_front_matter(self):
        d = WorkDoc.parse("\ufeff---\nslug: a\nstatus: planned\ntest_changes: allowed\n---\n" + self.BODY, "work/a.md")
        self.assertEqual((d.slug, d.status, d.meta.get("test_changes"), d.section("Goal")), ("a", "planned", "allowed", "g"))
        meta, body = split_frontmatter("\ufeff---\nslug: a\n---\nbody\n")
        self.assertEqual((meta, body), ({"slug": "a"}, "body\n"))
        self.assertEqual(G.test_changes_of("\ufeff---\nslug: a\ntest_changes: allowed\n---\n"), "allowed")

    def test_a_block_list(self):
        d = WorkDoc.parse("---\nslug: a\nstatus: ready\ndepends_on:\n  - schema\n  - api\ntest_changes: allowed\n---\n" + self.BODY, "work/a.md")
        self.assertEqual((d.depends_on, d.meta["test_changes"], d.status), (["schema", "api"], "allowed", "ready"))
        self.assertEqual(problems(d, "a"), [])

    def test_a_block_list_with_quotes_comments_and_the_list_at_the_key_s_own_indent(self):
        d = WorkDoc.parse('---\nslug: a\ndepends_on:\n- "one"   # first\n\n  # a comment\n- \'two\'\n-\n- three\nstatus: ready\n---\n' + self.BODY)
        self.assertEqual(d.depends_on, ["one", "two", "three"])
        self.assertEqual(d.status, "ready")

    def test_a_block_list_under_crlf(self):
        d = WorkDoc.parse("---\r\nslug: a\r\ndepends_on:\r\n  - b\r\n  - c\r\nstatus: planned\r\n---\r\n## Goal\r\ng\r\n", "work/a.md")
        self.assertEqual((d.depends_on, d.status, d.section("Goal").strip()), (["b", "c"], "planned", "g"))

    def test_the_flow_list_and_the_empty_value_still_work(self):
        self.assertEqual(WorkDoc.parse("---\nslug: a\ndepends_on: [b, c]\n---\n").depends_on, ["b", "c"])
        self.assertEqual(WorkDoc.parse("---\nslug: a\ndepends_on: []\n---\n").depends_on, [])
        self.assertEqual(WorkDoc.parse("---\nslug: a\ndepends_on:\nstatus: ready\n---\n").depends_on, [])
        self.assertEqual(WorkDoc.parse("---\nslug: a\ndepends_on: b, c\n---\n").depends_on, ["b", "c"])

    def test_a_dash_line_under_a_key_that_has_a_value_is_not_a_list(self):
        meta, _ = split_frontmatter("---\nslug: a\ntitle: x\n- stray\nstatus: ready\n---\n")
        self.assertEqual((meta["title"], meta["status"]), ("x", "ready"))

    def test_a_block_list_round_trips_as_a_flow_list(self):
        d = WorkDoc.parse("---\nslug: a\nstatus: ready\ndepends_on:\n  - b\n  - c\n---\n" + self.BODY)
        self.assertIn("depends_on: [b, c]\n", d.dumps())
        self.assertEqual(WorkDoc.parse(d.dumps()).depends_on, ["b", "c"])

    def test_the_last_review_tally_counts_and_the_doc_is_reported(self):
        d = WorkDoc.parse("---\nslug: a\nstatus: built\n---\n## Goal\ng\n## Review\nSDLC_REVIEW_TALLY important=0 nits=0\n"
                          "## Plan\np\n## Review\nSDLC_REVIEW_TALLY important=2 nits=1 validated=yes\n", "work/a.md")
        self.assertEqual(d.review_tally(), (2, 1, "yes"))
        self.assertIn("more than one ## Review section", problems(d, "a"))

    def test_a_second_review_without_a_tally_leaves_the_first_one(self):
        d = WorkDoc.parse("---\nslug: a\nstatus: built\n---\n## Goal\ng\n## Review\nSDLC_REVIEW_TALLY important=1 nits=0\n## Review\nnotes\n")
        self.assertEqual(d.review_tally(), (1, 0, ""))

    def test_one_review_section_is_fine(self):
        d = WorkDoc.parse("---\nslug: a\nstatus: built\n---\n## Goal\ng\n## Review\nSDLC_REVIEW_TALLY important=0 nits=3\n")
        self.assertEqual(d.review_tally(), (0, 3, ""))
        self.assertEqual(problems(d, "a"), [])
        self.assertIsNone(WorkDoc.parse("---\nslug: a\n---\n## Goal\ng\n").review_tally())

    def test_the_last_of_several_tallies_in_one_section(self):
        d = WorkDoc.parse("---\nslug: a\n---\n## Review\nSDLC_REVIEW_TALLY important=3 nits=0\nre-reviewed\nSDLC_REVIEW_TALLY important=0 nits=0\n")
        self.assertEqual(d.review_tally(), (0, 0, ""))


if __name__ == "__main__":
    unittest.main()
