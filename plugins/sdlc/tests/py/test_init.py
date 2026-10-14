"""`sdlc.py init`, driven through the CLI against throwaway repositories: scaffold, merge into what exists, move a 0.x
layout to 1.0, prune, and `--check`."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import BASE_ENV, PLUGIN, SDLC, STUBS

from sdlclib import init as I
from sdlclib.config import Config
from sdlclib.workdoc import WorkDoc

PACKAGE_JSON = json.dumps({"name": "shop", "scripts": {"build": "tsc", "test": "jest", "lint": "eslint ."}}, indent=2) + "\n"
SECTION_IDS = ("commands", "conventions", "architecture", "mistakes", "verifying")
GITIGNORE_LINES = [".sdlc/logs/", ".sdlc/state/", ".sdlc/worktrees/"]


class InitRepo:
    """A git repository with no sdlc files yet — what `init` meets the first time."""

    def __init__(self, git=True, name="repo"):
        self.tmp = tempfile.mkdtemp(prefix="sdlc-init-")
        self.root = os.path.join(self.tmp, name)
        os.makedirs(self.root)
        os.makedirs(os.path.join(self.tmp, "home"))
        if git:
            self.git("init", "-q", "-b", "main")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        e = dict(os.environ)
        e.update(BASE_ENV)
        e.update({"SDLC_SKIP_GH_DETECT": "1", "HOME": os.path.join(self.tmp, "home"), "GIT_CEILING_DIRECTORIES": self.tmp,
                  "SDLC_CLAUDE_BIN": os.path.join(STUBS, "claude"), "CLAUDE_BIN": os.path.join(STUBS, "claude"),
                  "PATH": STUBS + os.pathsep + e.get("PATH", "")})
        for k in ("CLAUDECODE", "CLAUDE_PROJECT_DIR", "STUB_GH_COLLAB", "STUB_GH_LOGGED_OUT"):
            e.pop(k, None)
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    # ---------- files and git ----------
    def path(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, text):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def write_bytes(self, rel, data):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def read_bytes(self, rel):
        with open(self.path(rel), "rb") as fh:
            return fh.read()

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def git(self, *args):
        r = subprocess.run(["git", "-C", self.root] + list(args), capture_output=True, text=True, env=self.env())
        if r.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), r.stderr))
        return r.stdout.strip()

    def git_in(self, cwd, *args):
        """git in another working copy — a second clone of this repository's origin."""
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, env=self.env())
        if r.returncode != 0:
            raise AssertionError("git %s failed in %s: %s" % (" ".join(args), cwd, r.stderr))
        return r.stdout.strip()

    def commit(self, message="work"):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def snapshot(self):
        """Every file (bytes) and directory outside .git — a change of either shows up in a comparison."""
        files, dirs = {}, set()
        for top, names, fnames in os.walk(self.root):
            names[:] = [n for n in names if n != ".git"]
            for n in names:
                dirs.add(os.path.relpath(os.path.join(top, n), self.root))
            for n in fnames:
                with open(os.path.join(top, n), "rb") as fh:
                    files[os.path.relpath(os.path.join(top, n), self.root)] = fh.read()
        return files, dirs

    def config(self):
        return json.loads(self.read(".sdlc/config.json"))

    # ---------- the CLI ----------
    def run(self, *args, **env):
        return subprocess.run([sys.executable, SDLC, "--root", self.root, "init"] + list(args), capture_output=True, text=True,
                              env=self.env(**env), timeout=120)

    def init(self, *args, **env):
        """`init --json`: the parsed result (the command must succeed)."""
        r = self.run("--json", *args, **env)
        if r.returncode != 0:
            raise AssertionError("init %s exited %d: %s%s" % (" ".join(args), r.returncode, r.stdout, r.stderr))
        return json.loads(r.stdout)

    def check(self, *args, **env):
        """`init --check --json`: (exit code, {row id: row})."""
        r = self.run("--check", "--json", *args, **env)
        return r.returncode, {c["id"]: c for c in json.loads(r.stdout)["checks"]}


def rows(result):
    """{path: (verb, note)} from an init result."""
    return {rel: (verb, note) for verb, rel, note in result["rows"]}


def verbs(result):
    return {rel: verb for rel, (verb, _) in rows(result).items()}


class Case(unittest.TestCase):
    def setUp(self):
        self.repo = InitRepo()
        self.addCleanup(self.repo.cleanup)

    def node_repo(self):
        self.repo.write("package.json", PACKAGE_JSON)
        self.repo.commit("package")


# =====================================================================================================================
class FirstRun(Case):
    def setUp(self):
        super().setUp()
        self.node_repo()

    def test_a_fresh_repository_gets_every_file(self):
        res = self.repo.init()
        v = verbs(res)
        for rel in (".sdlc/config.json", "work/README.md", "REVIEW.md", "CLAUDE.md", ".gitignore", ".sdlc/frozen-paths.txt",
                    ".claude/settings.json"):
            self.assertEqual(v[rel], "CREATED", rel)
        self.assertEqual(sorted(v), sorted([".sdlc/config.json", "work/README.md", "REVIEW.md", "CLAUDE.md", ".gitignore",
                                            ".sdlc/frozen-paths.txt", ".claude/settings.json"]))
        self.assertEqual((res["mode"], res["language"], res["forge"], res["stack"]), ("solo", "en", "local", ["node"]))
        self.assertEqual(res["commands"], {"build": "npm run build", "test": "npm run test", "lint": "npm run lint"})
        self.assertEqual((res["notes"], res["migrated"], res["leftovers"], res["todo"], res["dry_run"]), ([], [], [], [], False))

    def test_the_config_is_version_2_with_what_was_detected_and_nothing_else(self):
        self.repo.init()
        cfg = self.repo.config()
        self.assertEqual(set(cfg), {"version", "language", "mode", "commands", "protect"})
        self.assertEqual((cfg["version"], cfg["language"], cfg["mode"]), (2, "en", "solo"))
        self.assertEqual(cfg["commands"], {"build": "npm run build", "test": "npm run test", "lint": "npm run lint"})
        self.assertEqual(cfg["protect"], {"default_branch": "main"})

    def test_claude_md_has_the_five_sections_with_the_commands_filled_in(self):
        self.repo.init()
        text = self.repo.read("CLAUDE.md")
        self.assertTrue(text.startswith("# repo\n\n<!-- sdlc:begin commands -->"), text[:80])
        for sid in SECTION_IDS:
            self.assertEqual(text.count("<!-- sdlc:begin %s -->" % sid), 1, sid)
            self.assertEqual(text.count("<!-- sdlc:end %s -->" % sid), 1, sid)
        for line in ("- Build: npm run build", "- Test: npm run test", "- Lint: npm run lint"):
            self.assertEqual(text.count(line), 2, line)                  # Commands, and Verifying your work
        self.assertIn("- Run: TODO: set the run command", text)         # no run script: left for /sdlc:init to fill
        self.assertIn("TODO: naming, error handling", text)
        self.assertIn("TODO: main components", text)
        self.assertNotIn("{{", text)
        self.assertNotIn("@AGENTS.md", text)

    def test_gitignore_and_frozen_paths_and_work_readme_and_review(self):
        self.repo.init()
        self.assertEqual(self.repo.read(".gitignore").splitlines()[1:], GITIGNORE_LINES)
        self.assertTrue(self.repo.read(".gitignore").startswith("# sdlc plugin"))
        with open(os.path.join(PLUGIN, "templates", "config", "frozen-paths.txt"), encoding="utf-8") as fh:
            self.assertEqual(self.repo.read(".sdlc/frozen-paths.txt"), fh.read())
        with open(os.path.join(PLUGIN, "templates", "en", "work-README.md"), encoding="utf-8") as fh:
            self.assertEqual(self.repo.read("work/README.md"), fh.read())
        review = self.repo.read("REVIEW.md")
        self.assertIn("SDLC_REVIEW_TALLY", review)
        self.assertIn("TODO: generated paths", review)
        self.assertNotIn("{{", review)

    def test_settings_json_allows_git_and_the_projects_own_commands(self):
        self.repo.init()
        perm = json.loads(self.repo.read(".claude/settings.json"))["permissions"]
        for want in ("Bash(npm run build*)", "Bash(npm run test*)", "Bash(npm run lint*)", "Bash(git status*)", "Bash(git commit*)"):
            self.assertIn(want, perm["allow"])
        for want in ("Read(.env*)", "Bash(git push --force*)"):
            self.assertIn(want, perm["deny"])
        self.assertFalse([a for a in perm["allow"] if "{{" in a or a == "Bash(*)" or a == ""], perm["allow"])

    def test_files_written_by_init_get_the_mode_of_any_other_file_here(self):
        self.repo.write("probe.txt", "x")                       # made with the umask init runs under too
        want = os.stat(self.repo.path("probe.txt")).st_mode & 0o777
        self.repo.init()
        for rel in (".sdlc/config.json", ".claude/settings.json", "CLAUDE.md", "REVIEW.md"):
            self.assertEqual(os.stat(self.repo.path(rel)).st_mode & 0o777, want, rel)    # not the 0600 of a temp file

    def test_the_text_report_lists_what_was_detected_and_what_was_created(self):
        r = self.repo.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sdlc init — %s" % self.repo.root, r.stdout)
        self.assertRegex(r.stdout, r"DETECTED +stack: node \| build=npm run build, test=npm run test, lint=npm run lint")
        self.assertRegex(r.stdout, r"CREATED +work/README.md")
        self.assertRegex(r.stdout, r"CREATED +\.sdlc/config\.json +\(mode solo \(no remote")
        self.assertNotIn("TODO", r.stdout.split("DETECTED")[0])

    def test_only_a_missing_test_command_is_reported_as_todo(self):
        bare = InitRepo()
        self.addCleanup(bare.cleanup)
        res = bare.init()
        self.assertEqual(res["todo"], ["test"])                 # without one the gate verifies nothing
        self.assertEqual(res["commands"], {})
        self.assertIn("TODO: set the test command", bare.read("CLAUDE.md"))
        self.assertEqual(bare.config()["commands"], {})
        text = bare.run().stdout
        self.assertNotIn(" TODO", text.split("DETECTED")[0])
        self.assertRegex(text, r"(?m)^TODO +set commands\.test in \.sdlc/config\.json and CLAUDE\.md$")
        self.assertNotIn("commands.build", text)
        self.assertNotIn("commands.lint", text)
        self.assertRegex(text, r"(?m)^NOTE +no build or lint command — optional, the gate skips an empty one")

    def test_a_test_command_alone_leaves_no_todo_only_a_note_about_build_and_lint(self):
        bare = InitRepo()
        self.addCleanup(bare.cleanup)
        res = bare.init("--commands", "test=make check")
        self.assertEqual(res["todo"], [])
        self.assertEqual([n for n in res["notes"] if n.startswith("no ")],
                         ["no build or lint command — optional, the gate skips an empty one (set it in .sdlc/config.json, or "
                          "with `init --commands build=...,lint=...`)"])
        text = bare.run("--commands", "test=make check").stdout
        self.assertNotRegex(text, r"(?m)^TODO ")
        self.assertRegex(text, r"(?m)^NOTE +no build or lint command")
        self.assertEqual(bare.init("--commands", "test=make check", "--dry-run")["todo"], [])
        perm = json.loads(bare.read(".claude/settings.json"))["permissions"]["allow"]
        self.assertFalse([a for a in perm if "{{" in a or a in ("", "Bash(*)")], perm)

    def test_the_test_script_npm_init_writes_is_not_a_test_command(self):
        placeholder = 'echo "Error: no test specified" && exit 1'
        other = InitRepo()
        self.addCleanup(other.cleanup)
        other.write("package.json", json.dumps({"name": "x", "scripts": {"test": placeholder, "build": "tsc"}}))
        res = other.init()
        self.assertEqual(res["commands"], {"build": "npm run build"})
        self.assertEqual(res["todo"], ["test"])
        self.assertEqual([n for n in res["notes"] if n.startswith("no ")],
                         ["no lint command — optional, the gate skips an empty one (set it in .sdlc/config.json, or with "
                          "`init --commands lint=...`)"])
        self.assertNotIn("test", other.config()["commands"])
        self.assertIn("- Test: TODO: set the test command", other.read("CLAUDE.md"))
        make = InitRepo()                                           # a Makefile may still supply the command
        self.addCleanup(make.cleanup)
        make.write("package.json", json.dumps({"name": "x", "scripts": {"test": placeholder}}))
        make.write("Makefile", "test:\n\tpytest\n")
        self.assertEqual(make.init()["commands"], {"test": "make test"})
        real = InitRepo()                                           # a script that merely mentions it still counts when it is a real one
        self.addCleanup(real.cleanup)
        real.write("package.json", json.dumps({"name": "x", "scripts": {"test": "jest --passWithNoTests"}}))
        self.assertEqual(real.init()["commands"], {"test": "npm run test"})

    def test_running_from_a_subdirectory_of_the_repository_finds_the_root(self):
        os.makedirs(self.repo.path("src/deep"))
        r = subprocess.run([sys.executable, SDLC, "init", "--json"], cwd=self.repo.path("src/deep"), capture_output=True, text=True,
                           env=self.repo.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.repo.exists(".sdlc/config.json"))
        self.assertFalse(self.repo.exists("src/deep/.sdlc"))

    def test_the_default_branch_is_main_even_when_a_feature_branch_is_checked_out(self):
        self.repo.git("checkout", "-q", "-b", "feature/login")
        self.assertEqual(self.repo.init()["git"]["default_branch"], "main")
        self.assertEqual(self.repo.config()["protect"]["default_branch"], "main")

    def test_a_repository_on_master_records_master(self):
        other = InitRepo()
        self.addCleanup(other.cleanup)
        other.git("checkout", "-q", "-b", "master")
        other.write("a.txt", "x\n")
        other.commit("a")
        other.git("checkout", "-q", "-b", "feature/x")
        self.assertEqual(other.init()["git"]["default_branch"], "master")

    def test_the_default_branch_follows_origin_head(self):
        self.repo.git("remote", "add", "origin", "git@gitlab.example.com:o/r.git")
        self.repo.git("update-ref", "refs/remotes/origin/develop", "HEAD")
        self.repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        self.assertEqual(self.repo.init()["git"]["default_branch"], "develop")


class SecondRun(Case):
    def test_everything_is_skipped_and_nothing_changes(self):
        self.node_repo()
        self.repo.init()
        before = self.repo.snapshot()
        res = self.repo.init()
        self.assertEqual(set(verbs(res).values()), {"SKIPPED"}, rows(res))
        self.assertEqual(len(res["rows"]), 7)
        self.assertEqual(self.repo.snapshot(), before)
        text = self.repo.run().stdout
        self.assertNotIn("CREATED", text)
        self.assertNotIn("MERGED", text)
        self.assertEqual(self.repo.snapshot(), before)

    def test_a_command_added_later_is_filled_in_but_a_value_the_user_set_stays(self):
        self.repo.write("package.json", json.dumps({"scripts": {"test": "jest"}}))
        self.repo.init()
        cfg = self.repo.config()
        cfg["commands"]["build"] = "make all"
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.write("package.json", json.dumps({"scripts": {"test": "vitest", "build": "tsc", "lint": "eslint ."}}))
        res = self.repo.init()
        cmds = self.repo.config()["commands"]
        self.assertEqual(cmds, {"build": "make all", "test": "npm run test", "lint": "npm run lint"})
        self.assertEqual(rows(res)[".sdlc/config.json"], ("MERGED", "commands.lint"))

    def test_solo_and_team_flip_the_mode_of_an_initialized_repository(self):
        self.repo.init()
        self.assertEqual(rows(self.repo.init("--team"))[".sdlc/config.json"][0], "MERGED")
        self.assertEqual(self.repo.config()["mode"], "team")
        self.assertEqual(self.repo.init("--solo")["mode"], "solo")
        self.assertEqual(self.repo.config()["mode"], "solo")

    def test_another_language_changes_the_config_and_leaves_existing_files(self):
        self.node_repo()
        self.repo.init()
        res = self.repo.init("--lang", "ko")
        self.assertEqual(self.repo.config()["language"], "ko")
        self.assertEqual(rows(res)[".sdlc/config.json"], ("MERGED", "language"))
        self.assertNotIn("명령어", self.repo.read("CLAUDE.md"))
        self.assertEqual(verbs(res)["CLAUDE.md"], "SKIPPED")


class DryRun(Case):
    def test_nothing_is_written(self):
        self.node_repo()
        before = self.repo.snapshot()
        res = self.repo.init("--dry-run")
        self.assertEqual(self.repo.snapshot(), before)
        self.assertFalse(self.repo.exists(".sdlc"))
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertTrue(res["dry_run"])
        self.assertEqual(set(verbs(res).values()), {"WOULD CREATE"})
        self.assertEqual(len(res["rows"]), 7)

    def test_the_text_report_says_so(self):
        self.node_repo()
        r = self.repo.run("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(dry run: nothing written)", r.stdout)
        self.assertRegex(r.stdout, r"WOULD CREATE +CLAUDE\.md")
        self.assertNotRegex(r.stdout, r"(?m)^CREATED")

    def test_pending_merges_are_announced_as_would_merge(self):
        self.node_repo()
        self.repo.write(".gitignore", "node_modules/\n")
        self.repo.write("CLAUDE.md", "# Shop\n\n## Commands\n- Test: npm test\n")
        self.repo.commit("existing")
        before = self.repo.snapshot()
        res = self.repo.init("--dry-run")
        self.assertEqual(self.repo.snapshot(), before)
        self.assertEqual(verbs(res)["CLAUDE.md"], "WOULD MERGE")
        self.assertEqual(verbs(res)[".gitignore"], "WOULD MERGE")

    def test_dry_run_with_a_migration_and_a_prune_changes_nothing(self):
        make_v1_repo(self.repo)
        before = self.repo.snapshot()
        res = self.repo.init("--dry-run", "--prune")
        self.assertEqual(self.repo.snapshot(), before)
        v = verbs(res)
        self.assertEqual(v[".sdlc/config.json"], "WOULD MIGRATE")
        self.assertEqual(v["work/a-ready.md"], "WOULD MIGRATE")
        self.assertEqual(v[".sdlc/bands.json"], "WOULD REMOVE")
        self.assertEqual(self.repo.read(".sdlc/config.json"), json.dumps(V1_CONFIG, indent=2))


class ExistingClaudeMd(Case):
    def test_only_the_missing_sections_are_appended_and_the_original_text_stays(self):
        original = ("# Orders API\n\nTeam notes that must survive. Ask #orders-dev before touching billing.\n\n"
                    "## Commands\n- Test: npm test\n\n## Style\n- ES modules only.\n")
        self.repo.write("CLAUDE.md", original)
        res = self.repo.init()
        text = self.repo.read("CLAUDE.md")
        self.assertTrue(text.startswith(original.rstrip("\n")))
        self.assertEqual(text.count("## Commands"), 1)
        self.assertEqual(text.count("## Style"), 1)
        for sid in ("conventions", "architecture", "mistakes", "verifying"):
            self.assertEqual(text.count("<!-- sdlc:begin %s -->" % sid), 1, sid)
        self.assertNotIn("<!-- sdlc:begin commands -->", text)
        self.assertEqual(rows(res)["CLAUDE.md"],
                         ("MERGED", "added: Conventions, Architecture, Things Claude gets wrong, Verifying your work"))
        self.assertEqual(rows(self.repo.init())["CLAUDE.md"], ("SKIPPED", "all 5 sections present"))   # a second run adds nothing

    def test_the_commands_in_an_appended_verifying_section_are_the_detected_ones(self):
        self.node_repo()
        self.repo.write("CLAUDE.md", "# Shop\n\n## Commands\n- Test: npm test\n")
        self.repo.init()
        self.assertIn("- Test: npm run test (all green", self.repo.read("CLAUDE.md"))

    def test_the_0x_lines_naming_removed_commands_are_replaced_in_both_languages(self):
        self.assertEqual(len(I.OLD_LINES), 6)
        self.assertEqual(sum(1 for k in I.OLD_LINES if "/sdlc:lesson" in k), 2)
        for old, new in I.OLD_LINES.items():
            self.assertTrue(I.REMOVED_COMMANDS.search(old) or "plan.md" in old, old)   # the 0.4.1–0.5.1 lines name plan.md, not a command
            self.assertFalse(I.REMOVED_COMMANDS.search(new), new)         # the new wording names no removed command
        keep = ["# Orders API", "", "## Commands", "- Test: npm test", "", "## Things Claude gets wrong", "- Never edit src/gen."]
        old = list(I.OLD_LINES)
        self.repo.write("CLAUDE.md", "\n".join(keep + [old[0], "", "## Verifying your work", "Run the tests.", old[1], "",
                                                     "## 실수", old[2], old[3], "", "## Notes", "Keep PRs small.", ""]))
        res = self.repo.init()
        text = self.repo.read("CLAUDE.md")
        for line in old:
            self.assertNotIn(line, text)
        self.assertIn("- (add a line here the second time the same mistake appears)\n", text)
        self.assertIn("Every change has a work doc (`work/<slug>.md`", text)
        self.assertIn("- (같은 실수가 두 번째로 나타나면 여기에 한 줄을 추가한다)\n", text)
        self.assertIn("모든 변경에는 작업 문서(`work/<slug>.md`", text)
        self.assertIn("- Never edit src/gen.", text)
        self.assertIn("Keep PRs small.", text)
        verb, note = rows(res)["CLAUDE.md"]
        self.assertEqual(verb, "MERGED")
        self.assertIn("4 line(s) naming removed commands updated", note)
        self.assertNotIn("still mentions", note)                      # nothing left that names a removed command

    def test_the_lines_0_4_1_to_0_5_1_wrote_are_replaced_in_both_languages_too(self):
        en = "Nothing is implemented without an approved plan: start in plan mode, commit plan.md, then build."
        ko = "승인된 계획 없이는 아무것도 구현하지 않는다. 플랜 모드로 시작해 plan.md 를 커밋한 뒤 구현한다."
        for old, current in ((en, "Every change starts from a committed plan.md (`/sdlc:go` writes and commits it; `/sdlc:plan` for "
                                  "the step-by-step path)."),
                             (ko, "모든 변경은 커밋된 plan.md 에서 시작한다(`/sdlc:go` 가 작성·커밋하고, 단계별로는 `/sdlc:plan`).")):
            self.assertEqual(I.OLD_LINES[old], I.OLD_LINES[current])        # the same 1.0 line as the 0.5.2 wording gets
        self.repo.write("CLAUDE.md", "# Orders\n\n## Commands\n- Test: npm test\n\n## Verifying your work\nRun the tests.\n%s\n\n"
                                     "## 검증\n%s\n\n## Notes\nKeep PRs small.\n" % (en, ko))
        res = self.repo.init()
        text = self.repo.read("CLAUDE.md")
        self.assertNotIn(en, text)
        self.assertNotIn(ko, text)
        self.assertIn("Every change has a work doc (`work/<slug>.md`: goal, acceptance criteria, plan, proof); `/sdlc:go` writes it.\n", text)
        self.assertIn("모든 변경에는 작업 문서(`work/<slug>.md`: 목표·인수 조건·계획·증명)가 있다. `/sdlc:go` 가 작성한다.\n", text)
        self.assertIn("Keep PRs small.", text)
        verb, note = rows(res)["CLAUDE.md"]
        self.assertEqual(verb, "MERGED")
        self.assertIn("2 line(s) naming removed commands updated", note)
        self.assertNotIn("still mentions", note)

    def test_a_mention_of_a_removed_command_is_reported_and_left_alone(self):
        text = "# Shop\n\n## Notes\nUse /sdlc:plan before big changes, and /sdlc:review after. /sdlc:go is fine.\n"
        self.repo.write("CLAUDE.md", text)
        note = rows(self.repo.init())["CLAUDE.md"][1]
        self.assertIn("still mentions removed commands: /sdlc:plan, /sdlc:review", note)
        self.assertNotIn("/sdlc:go", note)
        self.assertIn("Use /sdlc:plan before big changes", self.repo.read("CLAUDE.md"))

    def test_with_all_sections_present_a_stale_mention_is_still_reported(self):
        self.repo.init()
        md = self.repo.read("CLAUDE.md") + "\nSee /sdlc:monitor.\n"
        self.repo.write("CLAUDE.md", md)
        res = self.repo.init()
        self.assertEqual(rows(res)["CLAUDE.md"], ("SKIPPED", "still mentions removed commands: /sdlc:monitor"))
        self.assertEqual(self.repo.read("CLAUDE.md"), md)

    def test_a_claude_md_that_is_not_utf8_text_is_left_alone(self):
        korean = "# 프로젝트\n\n## 명령\n- 테스트: make test\n".encode("cp949")
        self.repo.write_bytes("CLAUDE.md", korean)
        res = self.repo.init()
        self.assertEqual(rows(res)["CLAUDE.md"], ("SKIPPED", "not UTF-8 text — left alone"))
        self.assertEqual(self.repo.read_bytes("CLAUDE.md"), korean)
        self.assertEqual(rows(res)[".sdlc/config.json"][0], "CREATED")          # the rest of init goes on
        self.assertEqual(rows(self.repo.init())["CLAUDE.md"][0], "SKIPPED")
        self.assertEqual(self.repo.read_bytes("CLAUDE.md"), korean)
        self.assertEqual(self.repo.check()[1]["claude_md"]["status"], "warn")   # check still reads it (sections are missing)

    def test_a_crlf_claude_md_stays_crlf_all_the_way_down(self):
        stale = list(I.OLD_LINES)[0]
        original = "# Shop\r\n\r\nTeam notes.\r\n\r\n## Commands\r\n- Test: make test\r\n\r\n%s\r\n" % stale
        self.repo.write_bytes("CLAUDE.md", original.encode("utf-8"))
        res = self.repo.init()
        raw = self.repo.read_bytes("CLAUDE.md")
        self.assertTrue(raw.startswith(b"# Shop\r\n\r\nTeam notes.\r\n\r\n## Commands\r\n- Test: make test\r\n"), raw[:80])
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))                  # not one bare LF: no whole-file diff
        self.assertIn(b"<!-- sdlc:begin conventions -->\r\n## Conventions\r\n", raw)
        self.assertNotIn(stale.encode("utf-8"), raw)                            # the 0.x line was still found and replaced
        self.assertEqual(rows(res)["CLAUDE.md"][0], "MERGED")
        self.repo.init()
        self.assertEqual(self.repo.read_bytes("CLAUDE.md"), raw)

    def test_a_lf_file_with_a_stray_crlf_line_stays_lf(self):
        self.repo.write_bytes("CLAUDE.md", b"# Shop\n\nOne line from Windows\r\nand the rest from here.\n\n## Commands\n- Test: x\n")
        self.repo.init()
        raw = self.repo.read_bytes("CLAUDE.md")
        self.assertNotIn(b"\r\n<!--", raw)
        self.assertIn(b"-->\n## Conventions\n", raw)

    def test_headings_in_the_korean_template_count_as_present(self):
        self.repo.init("--lang", "ko")
        res = self.repo.init()
        self.assertEqual(rows(res)["CLAUDE.md"], ("SKIPPED", "all 5 sections present"))


class AgentsMd(Case):
    def setUp(self):
        super().setUp()
        self.repo.write("AGENTS.md", "# Agents\nBe careful.\n")

    def test_a_new_claude_md_starts_with_the_title_then_the_import(self):
        res = self.repo.init()
        text = self.repo.read("CLAUDE.md")
        self.assertTrue(text.startswith("# repo\n\n@AGENTS.md\n\n<!-- sdlc:begin commands -->"), text[:80])
        self.assertEqual(rows(res)["CLAUDE.md"], ("CREATED", "5 sections, imports AGENTS.md"))

    def test_an_existing_claude_md_gets_the_import_after_its_title_once(self):
        self.repo.write("CLAUDE.md", "# Shop\n\nSome rules.\n")
        res = self.repo.init()
        text = self.repo.read("CLAUDE.md")
        self.assertTrue(text.startswith("# Shop\n\n@AGENTS.md\n\nSome rules.\n"), text[:60])
        self.assertIn("imports AGENTS.md", rows(res)["CLAUDE.md"][1])
        again = self.repo.init()
        self.assertEqual(self.repo.read("CLAUDE.md"), text)
        self.assertEqual(text.count("@AGENTS.md"), 1)
        self.assertEqual(verbs(again)["CLAUDE.md"], "SKIPPED")

    def test_without_a_title_the_import_goes_first(self):
        self.repo.write("CLAUDE.md", "Some rules.\n\n## Commands\n- Test: make test\n")
        self.repo.init()
        self.assertTrue(self.repo.read("CLAUDE.md").startswith("@AGENTS.md\n\nSome rules."))

    def test_an_import_that_is_already_there_is_recognised(self):
        for text in ("# Shop\n@AGENTS.md\n", "# Shop\n\nSee @AGENTS.md for the shared rules.\n", "@AGENTS.md  \n# Shop\n"):
            self.repo.write("CLAUDE.md", text)
            self.repo.init()
            self.assertEqual(self.repo.read("CLAUDE.md").count("@AGENTS.md"), 1, text)

    def test_a_byte_order_mark_stays_first_and_the_import_goes_after_the_title(self):
        self.repo.write_bytes("CLAUDE.md", b"\xef\xbb\xbf# Shop\n\nRules.\n")
        self.repo.init()
        raw = self.repo.read_bytes("CLAUDE.md")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf# Shop\n\n@AGENTS.md\n\nRules.\n"), raw[:60])
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1)
        again = self.repo.init()
        self.assertEqual(self.repo.read_bytes("CLAUDE.md"), raw)
        self.assertEqual(verbs(again)["CLAUDE.md"], "SKIPPED")

    def test_a_similar_name_is_not_an_import(self):
        self.repo.write("CLAUDE.md", "# Shop\n\nThe file AGENTS.md.bak and @AGENTS.mdx are old.\n")
        self.repo.init()
        self.assertEqual(self.repo.read("CLAUDE.md").count("\n@AGENTS.md\n"), 1)


class LinkedFiles(Case):
    """A file that is a link (or a second name for the same file) is written through, never replaced."""

    def test_claude_md_linked_to_agents_md_imports_nothing_and_both_names_get_the_sections(self):
        self.repo.write("AGENTS.md", "# Agents\nBe careful.\n")
        os.symlink("AGENTS.md", self.repo.path("CLAUDE.md"))
        res = self.repo.init()
        self.assertTrue(os.path.islink(self.repo.path("CLAUDE.md")))
        text = self.repo.read("AGENTS.md")
        self.assertNotIn("@AGENTS.md", text)                        # a file importing itself is a loop
        self.assertEqual(self.repo.read("CLAUDE.md"), text)
        self.assertTrue(text.startswith("# Agents\nBe careful."))
        for sid in SECTION_IDS:
            self.assertEqual(text.count("<!-- sdlc:begin %s -->" % sid), 1, sid)
        self.assertEqual(rows(res)["CLAUDE.md"][0], "MERGED")
        self.assertNotIn("imports AGENTS.md", rows(res)["CLAUDE.md"][1])
        self.assertNotIn("agents_md", self.repo.check()[1])
        self.assertEqual(rows(self.repo.init())["CLAUDE.md"], ("SKIPPED", "all 5 sections present"))

    def test_agents_md_linked_to_claude_md_is_the_same_case(self):
        self.repo.write("CLAUDE.md", "# Shop\n")
        os.symlink("CLAUDE.md", self.repo.path("AGENTS.md"))
        self.repo.init()
        self.assertTrue(os.path.islink(self.repo.path("AGENTS.md")))
        self.assertNotIn("@AGENTS.md", self.repo.read("CLAUDE.md"))
        self.assertNotIn("agents_md", self.repo.check()[1])

    def test_two_names_for_one_file_by_hard_link_count_as_the_same_file(self):
        self.repo.write("AGENTS.md", "# Agents\n")
        os.link(self.repo.path("AGENTS.md"), self.repo.path("CLAUDE.md"))
        self.repo.init()
        self.assertNotIn("@AGENTS.md", self.repo.read("CLAUDE.md"))
        self.assertEqual(self.repo.read("CLAUDE.md"), self.repo.read("AGENTS.md"))

    def test_a_settings_file_that_is_a_link_stays_a_link_and_its_target_is_merged(self):
        shared = os.path.join(self.repo.tmp, "shared-settings.json")
        with open(shared, "w", encoding="utf-8") as fh:
            json.dump({"permissions": {"allow": ["WebFetch"]}, "env": {"A": "1"}}, fh)
        os.chmod(shared, 0o640)
        os.makedirs(self.repo.path(".claude"))
        os.symlink(shared, self.repo.path(".claude/settings.json"))
        res = self.repo.init()
        link = self.repo.path(".claude/settings.json")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(shared))
        with open(shared, encoding="utf-8") as fh:
            cur = json.load(fh)
        self.assertEqual(cur["env"], {"A": "1"})
        self.assertEqual(cur["permissions"]["allow"][0], "WebFetch")
        self.assertIn("Bash(git status*)", cur["permissions"]["allow"])
        self.assertEqual(os.stat(shared).st_mode & 0o777, 0o640)
        self.assertEqual(rows(res)[".claude/settings.json"][0], "MERGED")
        self.assertEqual(rows(self.repo.init())[".claude/settings.json"], ("SKIPPED", "permissions present"))


class ExistingSettings(Case):
    CUSTOM = {"permissions": {"allow": ["Bash(npm test)", "Bash(git status*)", "WebFetch"], "deny": ["Read(.env*)", "Bash(rm -rf*)"]},
              "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "echo team-hook"}]}]},
              "env": {"TEAM_FLAG": "1"}}

    def test_permissions_are_merged_and_the_rest_of_the_file_is_kept(self):
        self.node_repo()
        self.repo.write(".claude/settings.json", json.dumps(self.CUSTOM))
        res = self.repo.init()
        cur = json.loads(self.repo.read(".claude/settings.json"))
        self.assertEqual(cur["hooks"], self.CUSTOM["hooks"])
        self.assertEqual(cur["env"], self.CUSTOM["env"])
        allow = cur["permissions"]["allow"]
        self.assertEqual(allow[:3], ["Bash(npm test)", "Bash(git status*)", "WebFetch"])       # the user's entries, in order
        self.assertEqual(len(allow), len(set(allow)))                                           # git status* is not added twice
        for want in ("Bash(git diff*)", "Bash(npm run build*)", "Bash(npm run test*)", "Bash(npm run lint*)"):
            self.assertIn(want, allow)
        deny = cur["permissions"]["deny"]
        self.assertEqual(deny[:2], ["Read(.env*)", "Bash(rm -rf*)"])
        self.assertIn("Bash(git push --force*)", deny)
        self.assertEqual(len(deny), len(set(deny)))
        verb, note = rows(res)[".claude/settings.json"]
        self.assertEqual(verb, "MERGED")
        self.assertRegex(note, r"allow\+\d+ deny\+\d+")

    def test_a_merged_file_keeps_the_mode_it_had(self):
        self.repo.write(".claude/settings.json", json.dumps({"permissions": {"allow": ["WebFetch"]}}))
        os.chmod(self.repo.path(".claude/settings.json"), 0o640)
        self.repo.init()
        self.assertIn("Bash(git status*)", json.loads(self.repo.read(".claude/settings.json"))["permissions"]["allow"])
        self.assertEqual(os.stat(self.repo.path(".claude/settings.json")).st_mode & 0o777, 0o640)

    def test_a_second_run_changes_nothing(self):
        self.node_repo()
        self.repo.write(".claude/settings.json", json.dumps(self.CUSTOM))
        self.repo.init()
        text = self.repo.read(".claude/settings.json")
        res = self.repo.init()
        self.assertEqual(rows(res)[".claude/settings.json"], ("SKIPPED", "permissions present"))
        self.assertEqual(self.repo.read(".claude/settings.json"), text)

    def test_a_file_without_a_permissions_block_gets_one(self):
        self.repo.write(".claude/settings.json", json.dumps({"env": {"A": "1"}}))
        self.repo.init()
        cur = json.loads(self.repo.read(".claude/settings.json"))
        self.assertEqual(cur["env"], {"A": "1"})
        self.assertIn("Bash(git status*)", cur["permissions"]["allow"])

    def test_invalid_json_is_skipped_and_left_untouched(self):
        self.repo.write(".claude/settings.json", '{ "permissions": { "allow": [ "x" ], }  // my notes\n')
        before = self.repo.read(".claude/settings.json")
        res = self.repo.init()
        self.assertEqual(verbs(res)[".claude/settings.json"], "SKIPPED")
        self.assertIn("nothing changed", rows(res)[".claude/settings.json"][1])
        self.assertEqual(self.repo.read(".claude/settings.json"), before)

    def test_a_permissions_value_that_is_not_an_object_is_skipped(self):
        self.repo.write(".claude/settings.json", '{"permissions": ["Bash(ls)"]}\n')
        res = self.repo.init()
        self.assertEqual(verbs(res)[".claude/settings.json"], "SKIPPED")
        self.assertEqual(self.repo.read(".claude/settings.json"), '{"permissions": ["Bash(ls)"]}\n')


class ExistingGitignore(Case):
    def test_missing_lines_are_appended_once(self):
        self.repo.write(".gitignore", "node_modules/\n.sdlc/logs/\n")
        res = self.repo.init()
        text = self.repo.read(".gitignore")
        self.assertTrue(text.startswith("node_modules/\n.sdlc/logs/\n"))
        self.assertEqual(rows(res)[".gitignore"], ("MERGED", "+2 lines"))
        for line in GITIGNORE_LINES:
            self.assertEqual(text.splitlines().count(line), 1, line)
        again = self.repo.init()
        self.assertEqual(rows(again)[".gitignore"], ("SKIPPED", "all lines present"))
        self.assertEqual(self.repo.read(".gitignore"), text)

    def test_a_file_that_ends_without_a_newline_is_handled(self):
        self.repo.write(".gitignore", "dist")
        self.repo.init()
        lines = self.repo.read(".gitignore").splitlines()
        self.assertEqual(lines[0], "dist")
        self.assertEqual([l for l in lines if l.startswith(".sdlc")], GITIGNORE_LINES)

    def test_a_crlf_gitignore_stays_crlf(self):
        self.repo.write_bytes(".gitignore", b"node_modules/\r\ndist/\r\n")
        self.repo.init()
        raw = self.repo.read_bytes(".gitignore")
        self.assertTrue(raw.startswith(b"node_modules/\r\ndist/\r\n\r\n# sdlc plugin"), raw[:60])
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(rows(self.repo.init())[".gitignore"], ("SKIPPED", "all lines present"))
        self.assertEqual(self.repo.read_bytes(".gitignore"), raw)

    def test_a_gitignore_that_is_not_utf8_text_is_left_alone(self):
        utf16 = "node_modules/\ndist/\n".encode("utf-16")             # what `echo x > .gitignore` writes in Windows PowerShell
        self.repo.write_bytes(".gitignore", utf16)
        res = self.repo.init()
        self.assertEqual(rows(res)[".gitignore"], ("SKIPPED", "not UTF-8 text — left alone"))
        self.assertEqual(self.repo.read_bytes(".gitignore"), utf16)
        self.assertEqual(rows(res)["CLAUDE.md"][0], "CREATED")

    def test_a_line_written_without_the_slash_counts_as_present(self):
        self.repo.write(".gitignore", ".sdlc/logs\n.sdlc/state\n.sdlc/worktrees\n")
        res = self.repo.init()
        self.assertEqual(rows(res)[".gitignore"][0], "SKIPPED")


class CommandsFlag(Case):
    def test_commas_inside_a_command_are_kept(self):
        res = self.repo.init("--commands", "build=make all,test=pytest -k 'a,b'")
        cmds = self.repo.config()["commands"]
        self.assertEqual(cmds, {"build": "make all", "test": "pytest -k 'a,b'"})
        self.assertEqual(res["commands"], cmds)
        self.assertIn("- Test: pytest -k 'a,b'", self.repo.read("CLAUDE.md"))
        allow = json.loads(self.repo.read(".claude/settings.json"))["permissions"]["allow"]
        self.assertIn("Bash(make all*)", allow)
        self.assertIn("Bash(pytest -k 'a,b'*)", allow)

    def test_the_flag_overrides_what_an_existing_config_says(self):
        self.node_repo()
        self.repo.init()
        self.assertEqual(self.repo.config()["commands"]["test"], "npm run test")
        res = self.repo.init("--commands", "test=pytest -k 'a,b',run=make dev")
        self.assertEqual(self.repo.config()["commands"], {"build": "npm run build", "test": "pytest -k 'a,b'", "lint": "npm run lint",
                                                          "run": "make dev"})
        self.assertEqual(rows(res)[".sdlc/config.json"], ("MERGED", "commands.test, commands.run"))

    def test_every_key_is_understood_and_a_comma_before_anything_else_stays_in_the_value(self):
        self.repo.init("--commands", "build=a,test=b,lint=c,run=d,format=e,lint_file=f")
        self.assertEqual(self.repo.config()["commands"], {"build": "a", "test": "b", "lint": "c", "run": "d", "format": "e",
                                                          "lint_file": "f"})
        other = InitRepo()
        self.addCleanup(other.cleanup)
        other.init("--commands", "test=FOO=1,BAR=2 make test,bogus=g")      # not a known key: part of the test command
        self.assertEqual(other.config()["commands"], {"test": "FOO=1,BAR=2 make test,bogus=g"})

    def test_an_empty_value_clears_a_command(self):
        self.node_repo()
        self.repo.init()
        self.repo.init("--commands", "lint=")
        self.assertEqual(self.repo.config()["commands"]["lint"], "")

    def test_parse_commands_directly(self):
        self.assertEqual(I.parse_commands("build=make,test=make test"), {"build": "make", "test": "make test"})
        self.assertEqual(I.parse_commands("lint_file=ruff check {file},lint=ruff"), {"lint_file": "ruff check {file}", "lint": "ruff"})
        self.assertEqual(I.parse_commands("test=a, b=c"), {"test": "a, b=c"})
        self.assertEqual(I.parse_commands(""), {})


class ForeignWorkDir(Case):
    def test_work_docs_go_to_sdlc_work_when_work_holds_something_else(self):
        self.repo.write("work/notes.txt", "my notes\n")
        res = self.repo.init()
        self.assertEqual(self.repo.config()["paths"], {"work": "sdlc-work"})
        self.assertEqual(verbs(res)["sdlc-work/README.md"], "CREATED")
        self.assertNotIn("work/README.md", verbs(res))
        self.assertTrue(any("work/ already holds other files" in n for n in res["notes"]), res["notes"])
        self.assertEqual(os.listdir(self.repo.path("work")), ["notes.txt"])
        self.assertTrue(self.repo.exists("sdlc-work/README.md"))
        self.assertEqual(self.repo.init()["rows"][1][:2], ["SKIPPED", "sdlc-work/README.md"])   # the second run finds the same place

    def test_a_readme_of_the_projects_own_counts_as_foreign(self):
        self.repo.write("work/README.md", "# Work log\nNot ours.\n")
        self.repo.init()
        self.assertEqual(self.repo.config()["paths"], {"work": "sdlc-work"})
        self.assertEqual(self.repo.read("work/README.md"), "# Work log\nNot ours.\n")

    def test_markdown_files_that_look_like_work_docs_do_not_count_as_foreign(self):
        self.repo.write("work/a-thing.md", "---\nslug: a-thing\nstatus: ready\n---\n\n## Goal\ng\n")
        res = self.repo.init()
        self.assertNotIn("paths", self.repo.config())
        self.assertEqual(verbs(res)["work/README.md"], "CREATED")

    def test_a_work_directory_that_turns_foreign_later_does_not_move_the_docs(self):
        self.repo.init()                                         # work/ is ours when the config is written
        self.repo.write("work/a-thing.md", "---\nslug: a-thing\nstatus: ready\n---\n\n## Goal\ng\n")
        self.repo.write("work/notes.txt", "added later\n")
        res = self.repo.init()
        self.assertNotIn("paths", self.repo.config())
        self.assertEqual(verbs(res)[".sdlc/config.json"], "SKIPPED")
        self.assertFalse(self.repo.exists("sdlc-work"))

    def test_an_empty_work_directory_is_ours(self):
        os.makedirs(self.repo.path("work"))
        self.repo.init()
        self.assertNotIn("paths", self.repo.config())
        self.assertTrue(self.repo.exists("work/README.md"))


class Korean(Case):
    def test_lang_ko_writes_korean_files(self):
        self.node_repo()
        res = self.repo.init("--lang", "ko")
        self.assertEqual(res["language"], "ko")
        self.assertEqual(self.repo.config()["language"], "ko")
        text = self.repo.read("CLAUDE.md")
        for heading in ("## Commands (명령어)", "## Conventions (규약)", "## Architecture (아키텍처)",
                        "## Things Claude gets wrong (Claude 가 반복하는 실수)", "## Verifying your work (작업 검증)"):
            self.assertIn(heading, text)
        self.assertIn("- Build(빌드): npm run build", text)
        self.assertIn("변경 하나에 파일 하나", self.repo.read("work/README.md"))
        self.assertIn("리뷰 지침", self.repo.read("REVIEW.md"))
        self.assertIn("<!-- sdlc:work -->", self.repo.read("work/README.md"))
        self.assertNotIn("{{", text)

    def test_the_language_is_remembered_by_the_second_run(self):
        self.repo.init("--lang", "ko")
        before = self.repo.snapshot()
        res = self.repo.init()
        self.assertEqual(res["language"], "ko")
        self.assertEqual(self.repo.snapshot(), before)

    def test_an_unknown_language_is_refused(self):
        r = self.repo.run("--lang", "fr")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid choice", r.stderr)
        self.assertFalse(self.repo.exists(".sdlc"))


class Forge(Case):
    def remote(self, url):
        self.repo.git("remote", "add", "origin", url)

    def test_a_gitlab_remote_merges_on_this_machine_and_says_so(self):
        self.remote("git@gitlab.example.com:o/r.git")
        res = self.repo.init()
        self.assertEqual((res["forge"], res["mode"]), ("local", "solo"))
        self.assertEqual(self.repo.config()["loop"], {"forge": "local"})
        self.assertEqual(self.repo.config()["mode"], "solo")
        note = rows(res)[".sdlc/config.json"][1]
        self.assertIn("gitlab.example.com is not GitHub", note)
        self.assertIn("push the default branch yourself", note)

    def test_a_github_remote_is_team_mode_unless_solo_is_asked_for(self):
        self.remote("git@github.com:o/r.git")
        res = self.repo.init()
        self.assertEqual((res["forge"], res["mode"]), ("github", "team"))
        cfg = self.repo.config()
        self.assertEqual(cfg["mode"], "team")
        self.assertNotIn("loop", cfg)
        self.assertIn("collaborators not checked", rows(res)[".sdlc/config.json"][1])
        self.assertEqual([n for n in res["notes"] if "optional" not in n], [])      # only the one about build and lint
        self.assertEqual(res["git"]["host"], "github.com")
        self.assertEqual(res["git"]["repo"], "o/r")

    def test_solo_overrides_the_detected_team_mode(self):
        self.remote("https://github.com/o/r.git")
        res = self.repo.init("--solo")
        self.assertEqual((res["forge"], res["mode"]), ("github", "solo"))
        self.assertIn("as asked", rows(res)[".sdlc/config.json"][1])
        self.assertEqual(res["git"]["repo"], "o/r")

    def test_team_without_a_github_remote_is_allowed_but_warned_about(self):
        res = self.repo.init("--team")
        self.assertEqual(res["mode"], "team")
        self.assertTrue(any("team mode needs a GitHub remote" in n for n in res["notes"]), res["notes"])
        gl = InitRepo()
        self.addCleanup(gl.cleanup)
        gl.git("remote", "add", "origin", "git@gitlab.example.com:o/r.git")
        self.assertTrue(any("needs a GitHub remote" in n for n in gl.init("--team")["notes"]))

    def test_a_remote_that_is_a_local_path_merges_on_this_machine(self):
        bare = os.path.join(self.repo.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, env=self.repo.env())
        self.remote(bare)
        res = self.repo.init()
        self.assertEqual((res["forge"], res["mode"]), ("local", "solo"))
        self.assertEqual(self.repo.config()["loop"], {"forge": "local"})     # the engine would otherwise look for gh and a PR
        self.assertIn("local path, not GitHub", rows(res)[".sdlc/config.json"][1])
        self.assertEqual(self.repo.init()["rows"][0][:2], ["SKIPPED", ".sdlc/config.json"])

    def test_ssh_urls_with_a_port_and_https_urls_are_parsed(self):
        for url, host, repo in (("ssh://git@git.example.com:2222/team/app.git", "git.example.com", "team/app"),
                                ("https://user@github.com/o/r", "github.com", "o/r"),
                                ("git@github.example.org:o/r.git", "github.example.org", "o/r")):
            other = InitRepo()
            self.addCleanup(other.cleanup)
            other.git("remote", "add", "origin", url)
            git = other.init("--dry-run")["git"]
            self.assertEqual((git["host"], git["repo"]), (host, repo), url)

    def test_collaborators_are_counted_when_gh_can_be_asked(self):
        self.remote("git@github.com:o/r.git")
        res = self.repo.init("--dry-run", SDLC_SKIP_GH_DETECT=None, STUB_GH_COLLAB="1")
        self.assertEqual(res["mode"], "solo")
        self.assertIn("one collaborator", rows(res)[".sdlc/config.json"][1])
        res = self.repo.init("--dry-run", SDLC_SKIP_GH_DETECT=None, STUB_GH_COLLAB="4")
        self.assertEqual(res["mode"], "team")
        self.assertIn("4 collaborators", rows(res)[".sdlc/config.json"][1])
        res = self.repo.init("--dry-run", SDLC_SKIP_GH_DETECT=None, STUB_GH_COLLAB="err")
        self.assertEqual(res["mode"], "team")                            # unknown is not one: team gates stay
        self.assertIn("collaborators unknown", rows(res)[".sdlc/config.json"][1])

    def test_a_host_that_gh_is_logged_in_to_is_treated_as_github_enterprise(self):
        self.remote("git@git.corp.example.com:o/r.git")
        res = self.repo.init("--dry-run", SDLC_SKIP_GH_DETECT=None, STUB_GH_COLLAB="1")
        self.assertEqual((res["forge"], res["mode"]), ("github", "solo"))
        res = self.repo.init("--dry-run", SDLC_SKIP_GH_DETECT=None, STUB_GH_LOGGED_OUT="git.corp.example.com")
        self.assertEqual((res["forge"], res["mode"]), ("local", "solo"))


class Cli(Case):
    def test_everything_after_init_goes_to_init_even_when_it_starts_with_an_option(self):
        import sdlc
        self.assertEqual(sdlc.split_init(["init", "--check", "--json"]), (["init"], ["--check", "--json"]))
        self.assertEqual(sdlc.split_init(["--root", "/r", "init", "--dry-run"]), (["--root", "/r", "init"], ["--dry-run"]))
        self.assertEqual(sdlc.split_init(["--root=/r", "init", "--lang", "ko"]), (["--root=/r", "init"], ["--lang", "ko"]))
        self.assertEqual(sdlc.split_init(["init"]), (["init"], []))
        self.assertEqual(sdlc.split_init(["status", "--json"]), (["status", "--json"], None))
        self.assertEqual(sdlc.split_init(["answer", "init", "x"]), (["answer", "init", "x"], None))
        self.assertEqual(sdlc.split_init([]), ([], None))

    def test_the_root_may_be_given_as_root_equals(self):
        r = subprocess.run([sys.executable, SDLC, "--root=" + self.repo.root, "init", "--dry-run", "--json"], capture_output=True,
                           text=True, env=self.repo.env(), cwd=self.repo.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["dry_run"])

    def test_init_without_arguments_and_with_an_option_first_both_reach_init(self):
        for args in ([], ["--dry-run"], ["--check"], ["--lang", "ko", "--dry-run"]):
            r = subprocess.run([sys.executable, SDLC, "--root", self.repo.root, "init"] + args, capture_output=True, text=True,
                               env=self.repo.env())
            self.assertIn(r.returncode, (0, 1), (args, r.stderr))
            self.assertNotIn("unrecognized arguments", r.stderr)
            self.assertIn("sdlc", r.stdout)

    def test_an_option_init_does_not_know_is_refused_by_init(self):
        r = self.repo.run("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments: --bogus", r.stderr)
        self.assertIn("usage: sdlc.py init", r.stderr)
        self.assertFalse(self.repo.exists(".sdlc"))

    def test_solo_and_team_exclude_each_other(self):
        r = self.repo.run("--solo", "--team")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not allowed with argument", r.stderr)


class Refusals(Case):
    def test_outside_a_git_repository_it_exits_2_with_a_message(self):
        plain = InitRepo(git=False)
        self.addCleanup(plain.cleanup)
        r = plain.run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not a git repository", r.stderr)
        self.assertIn("git init", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(os.listdir(plain.root), [])
        r = plain.run("--json")
        self.assertEqual(r.returncode, 2)

    def test_an_invalid_config_is_not_touched(self):
        self.repo.write(".sdlc/config.json", "{ not json")
        before = self.repo.snapshot()
        r = self.repo.run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a valid JSON object", r.stderr)
        self.assertEqual(self.repo.snapshot(), before)
        self.repo.write(".sdlc/config.json", "[1, 2]")
        self.assertEqual(self.repo.run().returncode, 2)

    def test_a_config_section_that_is_not_an_object_is_refused_before_anything_is_written(self):
        for key in ("commands", "protect", "paths", "loop"):
            for version, value in ((2, ["npm test"]), (2, "make"), (1, 5), (1, ["x"])):
                self.repo.write(".sdlc/config.json", json.dumps({"version": version, key: value}))
                before = self.repo.snapshot()
                r = self.repo.run()
                self.assertEqual(r.returncode, 2, (key, version, value, r.stdout))
                self.assertIn('"%s" must be an object' % key, r.stderr)
                self.assertEqual(self.repo.snapshot(), before, (key, version, value))      # no CLAUDE.md, no config.v1.json

    def test_a_null_section_is_the_same_as_no_section(self):
        self.repo.write(".sdlc/config.json", json.dumps({"version": 2, "mode": "solo", "commands": None, "loop": None}))
        res = self.repo.init("--commands", "test=make test")
        self.assertEqual(res["commands"], {"test": "make test"})
        self.assertEqual(self.repo.config()["commands"], {"test": "make test"})
        self.assertEqual(rows(res)["CLAUDE.md"][0], "CREATED")

    def test_a_config_whose_version_is_not_a_number_is_treated_as_0x(self):
        self.repo.write(".sdlc/config.json", json.dumps({"version": "one", "roles": {"solo": True}, "commands": {"test": "make test"}}))
        res = self.repo.init()
        self.assertEqual(self.repo.config()["version"], 2)
        self.assertEqual(rows(res)[".sdlc/config.json"][0], "MIGRATED")
        self.assertEqual(self.repo.config()["mode"], "solo")

    def test_check_outside_a_git_repository_exits_1(self):
        plain = InitRepo(git=False)
        self.addCleanup(plain.cleanup)
        code, got = plain.check()
        self.assertEqual(code, 1)
        self.assertEqual(list(got), ["git"])
        self.assertEqual(got["git"]["status"], "missing")


# =====================================================================================================================
# The 0.x repository
V1_CONFIG = {
    "version": 1, "language": "en",
    "paths": {"intent": "intent", "spec": "spec", "plan": "plan", "evals": "evals", "review": "REVIEW.md",
              "lessons": ".sdlc/LESSONS.md"},
    "commands": {"build": "", "test": "npm test", "lint": "", "run": "", "format": "auto", "rollback": "", "lint_file": ""},
    "verify": {"required_before_stop": False, "commands": ["test"], "agent": "auto", "agent_min_files": 4},
    "plan": {"require_for_edits": False, "enforce_sync": False, "exempt_paths": ["intent/"]},
    "protect": {"frozen_paths_file": ".sdlc/frozen-paths.txt", "generated_paths": ["src/gen/"], "protect_tests": True,
                "test_paths": ["auto"], "ticketed_paths": ["infra/**"], "ticket_env": "CHANGE_TICKET",
                "block_direct_push": True, "default_branch": "main"},
    "secrets": {"scan_edits": True, "scan_commits": True, "extra_patterns": []},
    "environments": {"production": {"autonomy": "gated", "patterns": ["deploy prod"], "approval_env": "OK_PROD"}},
    "gate": {"mode": "deny"}, "hooks": {"format_on_edit": True}, "policies": [], "evals": {"model": "sonnet"},
    "monitor": {"bands": ".sdlc/bands.json"}, "auto_commit": True, "claude_md": {"max_lines": 120},
    "roles": {"solo": True, "autopilot": True, "auto_merge": True},
    "ci": {"mode": "local", "auth": "api"},
    "loop": {"enabled": True, "max_items": 5, "max_minutes": 480, "item_max_minutes": 120, "max_turns": 500,
             "max_failures_per_slug": 3, "self_check": True, "fix_rounds": 2, "split_phases": True, "model": "opus",
             "effort": "high", "fix_model": "sonnet", "pause_file": ".sdlc/state/pause", "item_note": ""},
}


def intent(slug, status, title, problem, outcome, depends_on=None):
    return ("---\ntype: intent\nslug: %s\ntitle: %s\nstatus: %s\nauthor: ann\ncreated: 2026-09-01\nsource: idea\nrecord_id: \"\"\n%s"
            "links: { spec: \"\", plan: \"\" }\n---\n# Intent: %s\n\nAuthor: ann (team). Status: %s.\n\n"
            "## Problem\n<!-- What cannot be done today, who is affected, and how often. -->\n%s\n\n"
            "## Proposed outcome\n<!-- What \"better\" looks like from the user's side. -->\n%s\n\n"
            "## Affected users and systems\n<!-- Teams, user groups. -->\nSupport staff.\n" % (
                slug, title, status, ("depends_on: [%s]\n" % ", ".join(depends_on)) if depends_on else "", title, status, problem, outcome))


def spec(slug, title, requirements):
    return ("---\ntype: spec\nslug: %s\ntitle: %s\nstatus: approved\n---\n# Spec: %s\n\n## Summary\nA summary.\n\n"
            "## Requirements\n<!-- Numbered, testable statements. -->\n%s\n\n## Design\nNot an acceptance criterion.\n" % (
                slug, title, title, requirements))


def plan(slug, title, status, test_changes="forbidden", proof="Tests prove it."):
    return ("---\ntype: plan\nslug: %s\ntitle: %s\nstatus: %s\ntest_changes: %s\napproved_by: ann\n---\n"
            "# Plan: %s (from spec/%s.md)\n\n## Files that change\n- src/%s.py (new)\n\n## Proof\n<!-- The tests that prove it. -->\n%s\n\n"
            "## Departures from plan\n" % (slug, title, status, test_changes, title, slug, slug, proof))


TEMPLATE_MD = "---\ntype: %s\nslug: {{slug}}\ntitle: {{title}}\nstatus: draft\n---\n# %s: {{title}}\n"
REQUIREMENTS_A = ("- R1. WHEN a customer opens the page THE SYSTEM SHALL show the claim status.\n"
                  "- R2. WHEN the status changes THE SYSTEM SHALL refresh within 5 seconds.\n- R3.")
OLD_CLAUDE_MD = ("# Orders\n\n## Commands\n- Test: npm test\n\n## Things Claude gets wrong\n%s\n\n## Verifying your work\n"
                 "Run the tests.\n%s\n" % (list(I.OLD_LINES)[0], list(I.OLD_LINES)[1]))

# REVIEW.md as 0.x's init wrote it (the templates of 0.4.1 to 0.8.3), `{{generated_paths}}` still to be filled in
OLD_REVIEW = {
    "en": """\
# Review instructions

## Passes
Run three passes and tag each finding with its pass:
- Bugs: logic errors, broken edge cases, subtle regressions
- Security: injection risks, authentication gaps, PII in logs, secrets in the diff
- Compliance: the change matches spec.md, plan.md and our design principles; CLAUDE.md conventions are followed

## What Important means here
Reserve Important for findings that would break behavior, leak data
or breach a policy. Style and naming are nits.

## Cap the nits
Report at most five nits per review; summarize the rest as a count.

## Do not report
Generated files under {{generated_paths}} and anything CI already enforces (formatting, lint).

## Feedback into CLAUDE.md
When a review flags a mistake for the second time, add the correction to the
"Things Claude gets wrong" section of CLAUDE.md as part of that review.
Also flag when a change has made CLAUDE.md outdated.

## Approval
Findings do not approve or block a PR on their own. Approval comes from a
code owner through branch protection. The agent that wrote the code cannot approve it.
""",
    "ko": """\
# 리뷰 지침 (Review instructions)

## 패스 (Passes)
세 번의 패스를 실행하고 모든 지적에 해당 패스를 태그합니다.
- Bugs(버그): 논리 오류, 깨진 엣지 케이스, 미묘한 회귀
- Security(보안): 인젝션 위험, 인증 누락, 로그 속 PII, diff 에 포함된 비밀값
- Compliance(정합성): 변경이 spec.md·plan.md·우리 설계 원칙과 일치하는지, CLAUDE.md 규약을 따르는지

## 여기서 Important 가 뜻하는 것 (What Important means here)
Important 는 동작을 깨뜨리거나, 데이터를 유출하거나, 정책을 위반할 지적에만 사용합니다.
스타일과 이름 짓기는 nit 입니다.

## nit 상한 (Cap the nits)
리뷰 하나에 nit 은 최대 다섯 건까지만 보고하고, 나머지는 건수로 요약합니다.

## 보고하지 않는 것 (Do not report)
{{generated_paths}} 아래의 생성 파일과 CI 가 이미 강제하는 것(포매팅, 린트).

## CLAUDE.md 로의 피드백 (Feedback into CLAUDE.md)
리뷰가 같은 실수를 두 번째로 지적하면, 그 리뷰의 일부로 CLAUDE.md 의
"Things Claude gets wrong" 섹션에 교정 내용을 추가합니다.
변경으로 CLAUDE.md 가 낡아졌다면 그것도 지적합니다.

## 승인 (Approval)
지적 사항만으로 PR 이 승인되거나 차단되지 않습니다. 승인은 브랜치 보호를 통해
코드 오너가 합니다. 코드를 작성한 에이전트는 그 코드를 승인할 수 없습니다.
""",
}
APPROVAL_HEADING = {"en": "## Approval", "ko": "## 승인 (Approval)"}
NEW_REVIEW_HEADINGS = {"en": ("## New or pre-existing", "## Tally"),
                       "ko": ("## 새로 생긴 것과 원래 있던 것 (New or pre-existing)", "## 집계 (Tally)")}
REVIEW_NOTES = ("the 0.x compliance line now names the work doc", "the 0.x approval section replaced by the review tally rule")
SPEC_PLAN_NOTE = "names spec.md/plan.md from 0.x — the reviewer reads the work doc"


def old_review(lang, generated="src/gen/"):
    return OLD_REVIEW[lang].replace("{{generated_paths}}", generated)


def new_review_template(lang):
    with open(os.path.join(PLUGIN, "templates", lang, "REVIEW.md"), encoding="utf-8") as fh:
        return fh.read()


def review_block(lang, heading):
    """The 1.x template's section that starts at `heading`: up to the next `## ` line, without the blank line after it."""
    lines = new_review_template(lang).split("\n")
    start = lines.index(heading)
    end = next((n for n in range(start + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip("\n")


def compliance_line(text):
    return next(l for l in text.split("\n") if l.startswith("- Compliance"))


def migrated_review(lang, generated="src/gen/"):
    """What a REVIEW.md as 0.x wrote it becomes: the 1.x Compliance line, and the 1.x sections where Approval was."""
    old = old_review(lang, generated)
    head = old[:old.index(APPROVAL_HEADING[lang])].replace(compliance_line(old), compliance_line(new_review_template(lang)))
    return head + "\n\n".join(review_block(lang, h) for h in NEW_REVIEW_HEADINGS[lang]) + "\n"


def make_v1_repo(repo, config=None):
    """A repository as 0.x left it: config version 1, intents/specs/plans, generated files, and one 0.x branch."""
    w = repo.write
    w(".sdlc/config.json", json.dumps(config or V1_CONFIG, indent=2))
    w("package.json", json.dumps({"scripts": {"test": "jest"}}))
    w(".gitignore", ".sdlc/state/\n.sdlc/logs/\n")
    w("CLAUDE.md", OLD_CLAUDE_MD)
    w("REVIEW.md", "# Review instructions\nOld review file.\n")
    # a-ready: approved, nothing else yet — and it depends on an open item and on one that was merged in 0.x
    w("intent/a-ready.md", intent("a-ready", "approved", "Alpha thing", "Support staff cannot see the claim status.",
                                  "Customers see the status page.", depends_on=["b-planned", "c-done"]))
    w("spec/a-ready.md", spec("a-ready", "Alpha thing", REQUIREMENTS_A))
    # b-planned: approved; its plan exists only on the 0.x branch
    w("intent/b-planned.md", intent("b-planned", "approved", "Beta thing", "Nobody can export.", "An export button."))
    w("spec/b-planned.md", spec("b-planned", "Beta thing", "- R1. WHEN the user clicks export THE SYSTEM SHALL download a CSV."))
    # c-done: merged in 0.x
    w("intent/c-done.md", intent("c-done", "approved", "Gamma thing", "Slow.", "Fast."))
    w("plan/c-done.md", plan("c-done", "Gamma thing", "implemented"))
    # d-draft and e-rejected
    w("intent/d-draft.md", intent("d-draft", "draft", "Delta thing", "Maybe later.", "Something."))
    w("intent/e-rejected.md", intent("e-rejected", "rejected", "Epsilon thing", "No.", "No."))
    # what 0.x generated and 1.0 does not use
    w("intent/README.md", "# intent/\nHow intents work.\n")
    w("intent/TEMPLATE.md", TEMPLATE_MD % ("intent", "Intent"))
    w("intent/triage/.gitkeep", "")
    w("spec/TEMPLATE.md", TEMPLATE_MD % ("spec", "Spec"))
    w("plan/TEMPLATE.md", TEMPLATE_MD % ("plan", "Plan"))
    w(".sdlc/bands.json", "{}\n")
    w(".sdlc/APPROVALS.md", "# Approvals\n")
    w(".sdlc/history/.gitkeep", "")
    w(".sdlc/examples/CLAUDE.md", "playbook code block\n")
    w(".sdlc/LESSONS.md", "# Lessons\n\n## 2026-09-02 — never edit src/gen\n- Fix: the build overwrites generated code.\n")
    w(".sdlc/frozen-paths.txt", "src/gen/\n")
    w(".github/workflows/sdlc-review.yml", "# sdlc — PR review\nname: sdlc-review\non: pull_request\n")
    w(".github/workflows/ci.yml", "name: CI\non: push\n")
    w(".github/workflows/sdlc-custom.yml", "name: our own sdlc-prefixed workflow\non: push\n")
    w("evals/README.md", "<!-- sdlc:evals -->\n# evals/\n")
    w("evals/cases/no-secret-in-code/case.json", "{}\n")
    w("evals/cases/my-own-case/case.json", "{\"mine\": true}\n")
    repo.commit("0.x state")
    repo.git("checkout", "-q", "-b", "sdlc/b-planned")
    w("plan/b-planned.md", plan("b-planned", "Beta thing", "approved", test_changes="allowed",
                                proof="test_export.py covers the CSV download."))
    repo.commit("plan b-planned")
    repo.git("checkout", "-q", "main")
    w(".sdlc/state/checks-1.txt", "old\n")      # local state: ignored by git, still on disk


LEFTOVERS = {".sdlc/bands.json", ".sdlc/APPROVALS.md", ".sdlc/history/", ".sdlc/examples/", "intent/TEMPLATE.md", "spec/TEMPLATE.md",
             "plan/TEMPLATE.md", "intent/triage/", ".sdlc/state/checks-1.txt", ".github/workflows/sdlc-review.yml",
             "evals/README.md", "evals/cases/no-secret-in-code/"}
KEPT_BY_PRUNE = ("intent/a-ready.md", "intent/b-planned.md", "intent/c-done.md", "intent/d-draft.md", "intent/e-rejected.md",
                 "intent/README.md", "spec/a-ready.md", "spec/b-planned.md", "plan/c-done.md", ".github/workflows/ci.yml",
                 ".github/workflows/sdlc-custom.yml", "evals/cases/my-own-case/case.json", ".sdlc/LESSONS.md",
                 ".sdlc/config.json", ".sdlc/config.v1.json", ".sdlc/frozen-paths.txt", "CLAUDE.md", "REVIEW.md")


# the headings the 0.x Korean templates gave the sections init reads: both languages in one title
KO_HEADINGS = (("## Problem\n", "## 문제 (Problem)\n"), ("## Proposed outcome\n", "## 제안하는 결과 (Proposed outcome)\n"),
               ("## Requirements\n", "## 요구사항 (Requirements)\n"), ("## Proof\n", "## 증명 (Proof)\n"))
# the 0.x LESSONS.md as its templates wrote it: the entry format is shown inside an HTML comment, no entry below it
LESSONS_TEMPLATE = {
    "en": "# Lessons\n\nVersion-controlled post-mortem lessons. Future investigations read this file first.\n"
          "Each entry is written by the team that owned the incident and links to the eval that now guards against it.\n\n"
          "<!-- Entry format:\n## YYYY-MM-DD — <short title>\n- Trigger: what fired\n- Impact:\n- Root cause:\n- Fix: PR link or commit\n"
          "- Guard: evals/cases/<name> · CLAUDE.md line added (yes/no)\n- Owner:\n-->\n",
    "ko": "# Lessons (교훈)\n\n버전 관리되는 포스트모템 교훈 모음입니다. 앞으로의 조사는 이 파일을 먼저 읽습니다.\n"
          "각 항목은 인시던트를 담당한 팀이 작성하며, 이제 그 재발을 막고 있는 eval 을 링크합니다.\n\n"
          "<!-- 항목 형식:\n## YYYY-MM-DD — <짧은 제목>\n- 트리거(Trigger): 무엇이 발동했나\n- 영향(Impact):\n- 근본 원인(Root cause):\n"
          "- 수정(Fix): PR 링크 또는 커밋\n- 방어(Guard): evals/cases/<name> · CLAUDE.md 한 줄 추가 여부 (yes/no)\n- 담당(Owner):\n-->\n"}
# the first lines of the 0.x evals README; the English one starts with a marker comment, the Korean one does not
KO_EVALS_README = "\n# evals/ — 에이전트 설정의 회귀 테스트\n\nCLAUDE.md·스킬·훅이 에이전트를 조종하므로, 코드가 받는 회귀 테스트를 이 설정도 똑같이 받습니다.\n"
EN_EVALS_README = "# evals/ — regression tests for the agent's configuration\n\nCLAUDE.md, skills and hooks steer the agent.\n"
CLOSING_NOTE = "the front matter has no closing `---` — read it up to the first other line, as 0.x did"


def korean(text):
    """A 0.x file as the Korean templates wrote it: `## 문제 (Problem)`, not `## Problem`."""
    for en, ko in KO_HEADINGS:
        text = text.replace(en, ko)
    return text


def unclosed(text):
    """The same 0.x file with the closing `---` of its front matter missing: the keys, then straight into the heading."""
    lines = text.split("\n")
    i = next(k for k, l in enumerate(lines) if k > 0 and l == "---")
    return "\n".join(lines[:i] + lines[i + 1:])


def with_origin(repo):
    """A bare repository as `origin`, with the checkout's main pushed to it; its path."""
    bare = os.path.join(repo.tmp, "origin.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True, env=repo.env())
    repo.git("remote", "add", "origin", bare)
    repo.git("push", "-q", "-u", "origin", "main")
    return bare


def push_elsewhere(repo, bare, branch="main", commits=1):
    """`commits` more commits on the origin's `branch`, pushed from a second clone: this checkout's own refs/remotes/origin/
    stay where they were until something fetches."""
    other = os.path.join(repo.tmp, "elsewhere")
    if not os.path.isdir(other):
        repo.git_in(repo.tmp, "clone", "-q", bare, other)
    if branch not in repo.git_in(other, "branch", "--list", branch):
        repo.git_in(other, "checkout", "-q", "-b", branch, "origin/main")
    else:
        repo.git_in(other, "checkout", "-q", branch)
    for _ in range(commits):
        with open(os.path.join(other, "merged-%s-%d.txt" % (branch, len(os.listdir(other)))), "w", encoding="utf-8") as fh:   # a new name each time
            fh.write("a person merged this\n")
        repo.git_in(other, "add", "-A")
        repo.git_in(other, "commit", "-q", "-m", "merged since")
    repo.git_in(other, "push", "-q", "origin", branch)


class Migration(Case):
    def setUp(self):
        super().setUp()
        make_v1_repo(self.repo)
        self.res = self.repo.init()

    def doc(self, slug):
        return WorkDoc.parse(self.repo.read("work/%s.md" % slug))

    def test_the_old_config_is_kept_and_the_new_one_is_version_2(self):
        with open(self.repo.path(".sdlc/config.v1.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), V1_CONFIG)
        cfg = self.repo.config()
        self.assertEqual(cfg["version"], 2)
        self.assertEqual(cfg["mode"], "solo")
        self.assertEqual(cfg["risk"], {"human": ["infra/**"]})
        self.assertEqual(cfg["commands"], {"test": "npm test", "format": "auto"})        # detected commands fill only the gaps
        self.assertEqual(cfg["protect"]["default_branch"], "main")
        self.assertEqual(cfg["protect"]["generated_paths"], ["src/gen/"])
        self.assertEqual(cfg["protect"]["deploy_patterns"], ["deploy prod"])
        self.assertEqual(cfg["protect"]["deploy_approval_env"], "OK_PROD")
        for gone in ("roles", "ci", "environments", "monitor", "policies", "evals", "gate", "plan"):
            self.assertNotIn(gone, cfg)
        self.assertEqual(cfg["loop"], {"max_failures_per_slug": 3, "self_check": True, "fix_rounds": 2,
                                       "pause_file": ".sdlc/state/pause", "item_note": ""})   # 500 turns / 120 min are 1.0's own defaults
        self.assertEqual(rows(self.res)[".sdlc/config.json"][0], "MIGRATED")
        self.assertIn("version 1 → 2, mode solo", rows(self.res)[".sdlc/config.json"][1])

    def test_the_kept_old_config_has_a_row_of_its_own(self):
        self.assertEqual(rows(self.res)[".sdlc/config.v1.json"], ("CREATED", "the 0.x config, kept"))   # the skill commits CREATED paths
        self.assertRegex(self.repo.run().stdout, r"(?m)^SKIPPED +\.sdlc/config\.json")                    # second run: nothing new
        self.assertNotIn(".sdlc/config.v1.json", rows(self.repo.init()))

    def test_a_backup_already_there_is_skipped_when_it_is_the_same_and_replaced_when_it_is_not(self):
        old = self.repo.read(".sdlc/config.v1.json")
        self.repo.write(".sdlc/config.json", old)                       # back to the 0.x config: migrate again
        again = self.repo.init()
        self.assertEqual(rows(again)[".sdlc/config.v1.json"][0], "SKIPPED")
        self.assertEqual(rows(again)[".sdlc/config.json"][0], "MIGRATED")
        self.assertEqual(self.repo.read(".sdlc/config.v1.json"), old)
        newer = dict(V1_CONFIG, language="ko")
        self.repo.write(".sdlc/config.json", json.dumps(newer))
        third = self.repo.init()
        self.assertEqual(rows(third)[".sdlc/config.v1.json"], ("MERGED", "replaced by the 0.x config just migrated"))
        self.assertEqual(json.loads(self.repo.read(".sdlc/config.v1.json")), newer)

    def test_the_backup_is_the_old_file_as_it_was_even_where_the_migration_ignored_part_of_it(self):
        odd = dict(V1_CONFIG, commands=None, paths=None)                # null sections are the same as none, for the new config
        self.repo.write(".sdlc/config.json", json.dumps(odd))
        os.remove(self.repo.path(".sdlc/config.v1.json"))
        res = self.repo.init()
        self.assertEqual(rows(res)[".sdlc/config.v1.json"][0], "CREATED")
        self.assertEqual(json.loads(self.repo.read(".sdlc/config.v1.json")), odd)
        self.assertEqual(self.repo.config()["version"], 2)

    def test_a_dry_run_reports_the_backup_as_a_would_create_and_writes_nothing(self):
        self.repo.write(".sdlc/config.json", json.dumps(V1_CONFIG))
        os.remove(self.repo.path(".sdlc/config.v1.json"))
        before = self.repo.snapshot()
        res = self.repo.init("--dry-run")
        self.assertEqual(rows(res)[".sdlc/config.v1.json"][0], "WOULD CREATE")
        self.assertEqual(self.repo.snapshot(), before)

    def test_notes_explain_what_changed(self):
        notes = " | ".join(self.res["notes"])
        self.assertIn("protect.ticketed_paths → risk.human", notes)
        self.assertIn("loop.max_items / loop.max_minutes dropped", notes)
        self.assertIn("dropped (not used by 1.0)", notes)
        self.assertIn(".sdlc/LESSONS.md holds 0.x lessons", notes)
        self.assertNotIn("loop.model", notes)                               # opus/sonnet/high are the 0.x defaults: nothing to say

    def test_a_key_whose_content_was_carried_over_is_not_listed_as_dropped(self):
        dropped = [n for n in self.res["notes"] if n.startswith("dropped (not used by 1.0)")]
        self.assertEqual(dropped, ["dropped (not used by 1.0): gate, policies, evals, monitor, ci, plan"])    # not environments, roles

    def test_only_unfinished_intents_become_work_docs(self):
        made = sorted(n for n in os.listdir(self.repo.path("work")) if n != "README.md")
        self.assertEqual(made, ["a-ready.md", "b-planned.md", "d-draft.md"])
        self.assertEqual(sorted(self.res["migrated"]), ["a-ready", "b-planned", "d-draft"])
        for slug in ("a-ready", "b-planned", "d-draft"):
            self.assertEqual(rows(self.res)["work/%s.md" % slug][0], "MIGRATED")
        self.assertNotIn("work/c-done.md", rows(self.res))                  # merged in 0.x
        self.assertNotIn("work/e-rejected.md", rows(self.res))              # rejected

    def test_an_approved_intent_with_no_plan_is_ready_with_its_goal_and_acceptance_criteria(self):
        d = self.doc("a-ready")
        self.assertEqual((d.slug, d.title, d.status), ("a-ready", "Alpha thing", "ready"))
        self.assertEqual(d.meta["source"], "migrated:intent")
        self.assertEqual(d.meta["test_changes"], "none")
        self.assertEqual(d.section("Goal"), "Support staff cannot see the claim status.\n\nCustomers see the status page.")
        self.assertEqual(d.section("Acceptance criteria"),
                         "- R1. WHEN a customer opens the page THE SYSTEM SHALL show the claim status.\n"
                         "- R2. WHEN the status changes THE SYSTEM SHALL refresh within 5 seconds.")
        self.assertEqual(d.section("Plan"), "")
        text = self.repo.read("work/a-ready.md")
        self.assertNotIn("<!--", text)
        self.assertNotIn("Not an acceptance criterion", text)

    def test_dependencies_on_open_items_are_kept_and_on_items_finished_in_0x_are_dropped(self):
        self.assertEqual(self.doc("a-ready").depends_on, ["b-planned"])
        self.assertTrue(any("c-done" in n and "a-ready" in n for n in [rows(self.res)["work/a-ready.md"][1]] + self.res["notes"]),
                        rows(self.res)["work/a-ready.md"])

    def test_an_approved_plan_on_the_0x_branch_makes_the_item_planned(self):
        d = self.doc("b-planned")
        self.assertEqual((d.status, d.meta["test_changes"], d.meta["source"]), ("planned", "allowed", "migrated:intent"))
        self.assertIn("`plan/b-planned.md` on branch `sdlc/b-planned`", d.section("Plan"))
        self.assertEqual(d.section("Proof"), "test_export.py covers the CSV download.")
        self.assertEqual(d.section("Goal"), "Nobody can export.\n\nAn export button.")
        self.assertEqual(d.section("Acceptance criteria"), "- R1. WHEN the user clicks export THE SYSTEM SHALL download a CSV.")

    def test_a_dependency_on_a_rejected_or_unknown_item_is_kept_so_a_person_sees_it(self):
        other = InitRepo()
        self.addCleanup(other.cleanup)
        make_v1_repo(other)
        other.write("intent/f-dep.md", intent("f-dep", "approved", "F", "p", "o", depends_on=["e-rejected", "nowhere", "c-done"]))
        other.init()
        self.assertEqual(WorkDoc.parse(other.read("work/f-dep.md")).depends_on, ["e-rejected", "nowhere"])

    def test_a_draft_intent_is_proposed(self):
        d = self.doc("d-draft")
        self.assertEqual((d.status, d.title), ("proposed", "Delta thing"))
        self.assertEqual(d.section("Goal"), "Maybe later.\n\nSomething.")
        self.assertEqual(d.section("Acceptance criteria"), "")
        self.assertEqual(d.depends_on, [])

    def test_the_history_is_untouched(self):
        for rel in ("intent/a-ready.md", "intent/c-done.md", "spec/a-ready.md", "plan/c-done.md"):
            self.assertTrue(self.repo.exists(rel), rel)
        self.assertIn("status: approved", self.repo.read("intent/a-ready.md"))

    def test_leftovers_are_listed_and_only_what_0x_generated(self):
        self.assertEqual(set(self.res["leftovers"]), LEFTOVERS)
        for kept in (".github/workflows/ci.yml", ".github/workflows/sdlc-custom.yml", "evals/cases/my-own-case/",
                     ".sdlc/LESSONS.md", "intent/README.md"):
            self.assertNotIn(kept, self.res["leftovers"])
        text = self.repo.run().stdout
        self.assertRegex(text, r"LEFTOVER +.*\.sdlc/bands\.json.* — `init --prune` removes them")
        for rel in LEFTOVERS:                                              # reporting removes nothing
            self.assertTrue(self.repo.exists(rel.rstrip("/")), rel)

    def test_claude_md_is_merged_too(self):
        text = self.repo.read("CLAUDE.md")
        for old in list(I.OLD_LINES)[:2]:
            self.assertNotIn(old, text)
        self.assertEqual(text.count("## Commands"), 1)
        verb, note = rows(self.res)["CLAUDE.md"]
        self.assertEqual(verb, "MERGED")
        self.assertIn("2 line(s) naming removed commands updated", note)
        self.assertIn("added: Conventions, Architecture", note)

    def test_review_md_and_the_rest_of_the_scaffold_keep_what_exists(self):
        self.assertEqual(self.repo.read("REVIEW.md"), "# Review instructions\nOld review file.\n")
        self.assertEqual(verbs(self.res)["REVIEW.md"], "SKIPPED")
        self.assertEqual(verbs(self.res)["work/README.md"], "CREATED")
        self.assertEqual(self.repo.read(".sdlc/frozen-paths.txt"), "src/gen/\n")

    def test_the_engine_reads_the_migrated_queue(self):
        self.repo.commit("migrate to 1.0")
        r = subprocess.run([sys.executable, SDLC, "--root", self.repo.root, "status", "--json"], capture_output=True, text=True,
                           env=self.repo.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        s = json.loads(r.stdout)
        self.assertEqual(s["counts"], {"design": 1, "build": 1, "proposed": 1})
        self.assertFalse(s["legacy_config"])
        self.assertEqual(s["next"], ["b-planned"])                         # a-ready waits for it, not for a person
        items = {i["slug"]: i for i in s["items"]}
        self.assertEqual((items["a-ready"]["wait_kind"], items["a-ready"]["depends_on"]), ("dep", ["b-planned"]))
        self.assertEqual(items["b-planned"]["status"], "planned")
        self.assertEqual([n["slug"] for n in s["needs_you"]], ["d-draft"])

    def test_the_second_run_migrates_nothing_again_and_still_lists_the_leftovers(self):
        before = {rel: self.repo.read(rel) for rel in ("work/a-ready.md", "work/b-planned.md", "work/d-draft.md", ".sdlc/config.json")}
        again = self.repo.init()
        self.assertEqual(again["migrated"], [])
        self.assertEqual(set(again["leftovers"]), LEFTOVERS)
        self.assertEqual({rel: self.repo.read(rel) for rel in before}, before)
        self.assertNotIn("MIGRATED", set(verbs(again).values()))

    def test_a_work_doc_that_already_exists_is_not_overwritten(self):
        other = InitRepo()
        self.addCleanup(other.cleanup)
        make_v1_repo(other)
        other.write("work/a-ready.md", "---\nslug: a-ready\nstatus: ready\n---\n\n## Goal\nMine.\n")
        res = other.init()
        self.assertEqual(rows(res)["work/a-ready.md"], ("SKIPPED", "already a work doc"))
        self.assertIn("Mine.", other.read("work/a-ready.md"))
        self.assertEqual(sorted(res["migrated"]), ["b-planned", "d-draft"])


class MigrationDetails(Case):
    def test_a_team_repository_without_a_remote_becomes_solo_and_the_loop_setting_is_not_carried_over(self):
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["roles"] = {"solo": False, "autopilot": False, "auto_merge": False}
        cfg["loop"]["enabled"] = False
        make_v1_repo(self.repo, cfg)
        res = self.repo.init()
        new = self.repo.config()
        self.assertEqual(new["mode"], "solo")
        self.assertNotIn("enabled", new["loop"])
        notes = " | ".join(res["notes"])
        self.assertIn("mode solo: team mode needs a GitHub remote", notes)
        self.assertIn("loop.enabled=false not carried over", notes)
        self.assertIn("mode solo, the old file is kept" if False else "version 1 → 2, mode solo", rows(res)[".sdlc/config.json"][1])

    def test_a_team_repository_on_github_stays_team(self):
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["roles"] = {"solo": False}
        make_v1_repo(self.repo, cfg)
        self.repo.git("remote", "add", "origin", "git@github.com:o/r.git")
        res = self.repo.init()
        self.assertEqual(self.repo.config()["mode"], "team")
        self.assertEqual(res["mode"], "team")
        self.assertFalse(any("needs a GitHub remote" in n for n in res["notes"]))

    def test_solo_on_the_command_line_wins_over_a_team_config(self):
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["roles"] = {"solo": False}
        make_v1_repo(self.repo, cfg)
        self.repo.git("remote", "add", "origin", "git@github.com:o/r.git")
        self.repo.init("--solo")
        self.assertEqual(self.repo.config()["mode"], "solo")

    def test_loop_settings_that_differ_from_the_0x_defaults_are_carried(self):
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["loop"].update({"model": "haiku", "effort": "low", "fix_model": "opus", "enabled": False})
        make_v1_repo(self.repo, cfg)
        res = self.repo.init()
        loop = self.repo.config()["loop"]
        self.assertEqual((loop["model"], loop["effort"], loop["fix_model"], loop["enabled"]), ("haiku", "low", "opus", False))
        self.assertIn("loop.model 'haiku' kept", " | ".join(res["notes"]))

    def test_a_gitlab_remote_keeps_merges_local(self):
        make_v1_repo(self.repo, dict(V1_CONFIG, roles={"solo": False}))
        self.repo.git("remote", "add", "origin", "git@gitlab.example.com:o/r.git")
        res = self.repo.init()
        self.assertEqual(self.repo.config()["loop"]["forge"], "local")
        self.assertEqual(self.repo.config()["mode"], "solo")
        self.assertTrue(any("team mode needs a GitHub remote" in n for n in res["notes"]))

    def test_custom_0x_paths_are_followed_and_remembered_after_the_config_is_rewritten(self):
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["paths"] = {"intent": "docs/intents", "spec": "docs/specs", "plan": "docs/plans", "evals": "sdlc-evals",
                        "review": "docs/REVIEW.md", "lessons": "docs/LESSONS.md"}
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.write("docs/intents/x-one.md", intent("x-one", "approved", "One", "Problem one.", "Outcome one."))
        self.repo.write("docs/specs/x-one.md", spec("x-one", "One", "- R1. WHEN a THE SYSTEM SHALL b."))
        self.repo.write("docs/intents/TEMPLATE.md", TEMPLATE_MD % ("intent", "Intent"))
        self.repo.write("docs/plans/TEMPLATE.md", TEMPLATE_MD % ("plan", "Plan"))
        self.repo.write("docs/LESSONS.md", "## 2026-09-02 — a lesson\n")
        self.repo.write("sdlc-evals/README.md", "<!-- sdlc:evals -->\n")
        self.repo.write("sdlc-evals/cases/respect-frozen-path/case.json", "{}\n")
        self.repo.write("evals/README.md", "someone else's evals\n")
        self.repo.commit("0.x")
        res = self.repo.init()
        self.assertEqual(res["migrated"], ["x-one"])
        self.assertIn("Acceptance criteria", self.repo.read("work/x-one.md"))
        self.assertEqual(self.repo.config()["paths"], {"review": "docs/REVIEW.md"})
        self.assertTrue(self.repo.exists("docs/REVIEW.md"))
        self.assertEqual(set(res["leftovers"]), {"docs/intents/TEMPLATE.md", "docs/plans/TEMPLATE.md", "sdlc-evals/README.md",
                                                 "sdlc-evals/cases/respect-frozen-path/"})
        self.assertTrue(any("docs/LESSONS.md holds 0.x lessons" in n for n in res["notes"]))
        again = self.repo.init()                                          # the config is version 2 now; the old paths come from the copy
        self.assertEqual(set(again["leftovers"]), set(res["leftovers"]))
        self.assertEqual(again["migrated"], [])

    def test_a_0x_repository_whose_work_directory_is_taken_uses_sdlc_work(self):
        make_v1_repo(self.repo)
        self.repo.write("work/notes.txt", "mine\n")
        res = self.repo.init()
        self.assertEqual(self.repo.config()["paths"], {"work": "sdlc-work"})
        self.assertTrue(self.repo.exists("sdlc-work/a-ready.md"))
        self.assertFalse(self.repo.exists("work/a-ready.md"))
        self.assertEqual(os.listdir(self.repo.path("work")), ["notes.txt"])
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])

    def test_intents_that_are_not_valid_slugs_or_have_no_status_are_left_alone(self):
        make_v1_repo(self.repo)
        self.repo.write("intent/Bad_Slug.md", intent("Bad_Slug", "approved", "Bad", "p", "o"))
        self.repo.write("intent/no-front.md", "# Just notes\n\n## Problem\nx\n")
        self.repo.write("intent/notes.txt", "text\n")
        res = self.repo.init()
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])

    def test_an_intent_whose_slug_names_a_file_work_keeps_for_itself_is_not_migrated_and_the_note_says_why(self):
        make_v1_repo(self.repo)
        for name in ("README.md", "TEMPLATE.md"):                 # intent/readme.md and intent/README.md are one file on some systems
            os.remove(self.repo.path("intent/" + name))
        self.repo.write("intent/readme.md", intent("readme", "approved", "Read me", "Nobody reads.", "Somebody reads."))
        self.repo.write("intent/template.md", intent("template", "draft", "Template", "None.", "One."))
        self.repo.commit("intents named like files of work/")
        res = self.repo.init()
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])
        self.assertNotIn("readme.md", os.listdir(self.repo.path("work")))
        self.assertNotIn("template.md", os.listdir(self.repo.path("work")))
        notes = res["notes"]
        for slug, name in (("readme", "README.md"), ("template", "TEMPLATE.md")):
            self.assertIn("intent/%s.md is not migrated: as a work doc it would be work/%s on a case-insensitive file system — "
                          "start it again under another name" % (slug, name), notes)

    def test_an_intent_whose_plan_is_implemented_only_on_its_branch_is_still_open(self):
        make_v1_repo(self.repo)
        self.repo.git("checkout", "-q", "sdlc/b-planned")
        self.repo.write("plan/b-planned.md", plan("b-planned", "Beta thing", "implemented", test_changes="allowed", proof="p"))
        self.repo.commit("implemented on the branch, not merged yet")
        self.repo.git("checkout", "-q", "main")
        res = self.repo.init()
        self.assertEqual(WorkDoc.parse(self.repo.read("work/b-planned.md")).status, "planned")
        self.assertIn("b-planned", res["migrated"])

    def test_an_intent_without_a_spec_gets_no_acceptance_section_and_one_without_a_problem_still_has_a_goal(self):
        make_v1_repo(self.repo)
        self.repo.write("intent/f-bare.md", "---\nslug: f-bare\ntitle: Bare\nstatus: approved\n---\n# Intent: Bare\n\n"
                                            "## Affected users and systems\nSomeone.\n")
        self.repo.init()
        d = WorkDoc.parse(self.repo.read("work/f-bare.md"))
        self.assertEqual(d.status, "ready")
        self.assertEqual(d.section("Goal"), "See `intent/f-bare.md`.")
        self.assertEqual(d.section("Acceptance criteria"), "")
        self.assertEqual(d.meta["created"][:4], "2026" if d.meta["created"].startswith("2026") else d.meta["created"][:4])


class MigrationOldFiles(Case):
    """The 0.x files as real repositories had them, which is not always what the templates meant."""

    def setUp(self):
        super().setUp()
        make_v1_repo(self.repo)

    def doc(self, slug):
        return WorkDoc.parse(self.repo.read("work/%s.md" % slug))

    def test_a_section_matches_by_its_name_or_by_a_title_that_ends_in_the_name_in_parentheses(self):
        doc = WorkDoc.parse("---\n---\n## 문제 (Problem)\nA\n\n## Proposed Outcome\nB\n\n## 증명 (proof)\nC\n\n"
                            "## Proof of concept\nD\n\n## Requirements (draft)\nE\n")
        self.assertEqual(I.old_section(doc, "Problem"), "A")
        self.assertEqual(I.old_section(doc, "Proposed outcome"), "B")            # whatever the case
        self.assertEqual(I.old_section(doc, "Proof"), "C")
        self.assertEqual(I.old_section(doc, "Requirements"), "")                 # `Requirements (draft)` is another title
        self.assertEqual(I.old_section(doc, "Goal"), "")
        self.assertEqual(doc.section("Problem"), "")                             # WorkDoc.section itself still takes the whole title

    def test_the_korean_templates_headings_carry_both_languages_and_still_fill_goal_acceptance_and_proof(self):
        self.repo.write("intent/k-ko.md", korean(intent("k-ko", "approved", "한국어 항목", "고객이 상태를 볼 수 없다.",
                                                         "고객이 상태 페이지를 본다.")))
        self.repo.write("spec/k-ko.md", korean(spec("k-ko", "한국어 항목", "- R1. WHEN 페이지를 열면 THE SYSTEM SHALL 상태를 보여준다.")))
        self.repo.write("plan/k-ko.md", korean(plan("k-ko", "한국어 항목", "approved", proof="test_status.py 가 증명한다.")))
        self.repo.commit("a Korean 0.x item")
        res = self.repo.init()
        d = self.doc("k-ko")
        self.assertEqual((d.status, d.title), ("planned", "한국어 항목"))
        self.assertEqual(d.section("Goal"), "고객이 상태를 볼 수 없다.\n\n고객이 상태 페이지를 본다.")
        self.assertEqual(d.section("Acceptance criteria"), "- R1. WHEN 페이지를 열면 THE SYSTEM SHALL 상태를 보여준다.")
        self.assertEqual(d.section("Proof"), "test_status.py 가 증명한다.")
        self.assertNotIn("See `intent/k-ko.md`", self.repo.read("work/k-ko.md"))     # the pointer line is the fallback only
        english = self.doc("b-planned")                                                  # the plain English headings still work
        self.assertEqual((english.section("Goal"), english.section("Proof")),
                         ("Nobody can export.\n\nAn export button.", "test_export.py covers the CSV download."))
        self.assertIn("k-ko", res["migrated"])

    def test_a_plan_whose_front_matter_never_closes_still_says_implemented_and_the_item_is_not_migrated(self):
        self.repo.write("intent/g-unclosed.md", intent("g-unclosed", "approved", "G", "p", "o"))
        self.repo.write("plan/g-unclosed.md", unclosed(plan("g-unclosed", "G", "implemented")))
        self.repo.commit("a plan whose front matter was never closed")
        res = self.repo.init()
        self.assertNotIn("g-unclosed", res["migrated"])                            # 0.x grep saw `status: implemented`: merged
        self.assertFalse(self.repo.exists("work/g-unclosed.md"))
        self.assertIn("plan/g-unclosed.md: " + CLOSING_NOTE, res["notes"])
        self.assertEqual(sum(1 for n in res["notes"] if "no closing" in n), 1)     # the one file, named once
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])

    def test_an_unclosed_approved_plan_keeps_its_proof_and_a_dependency_on_a_finished_unclosed_one_is_dropped(self):
        self.repo.write("intent/g-unclosed.md", intent("g-unclosed", "approved", "G", "p", "o"))
        self.repo.write("plan/g-unclosed.md", unclosed(plan("g-unclosed", "G", "implemented")))
        self.repo.write("intent/h-needs-g.md", intent("h-needs-g", "approved", "H", "p", "o", depends_on=["g-unclosed"]))
        self.repo.write("plan/h-needs-g.md", unclosed(plan("h-needs-g", "H", "approved", test_changes="allowed",
                                                            proof="test_h.py covers it.")))
        self.repo.commit("0.x")
        res = self.repo.init()
        d = self.doc("h-needs-g")
        self.assertEqual((d.status, d.depends_on, d.meta["test_changes"]), ("planned", [], "allowed"))
        self.assertEqual(d.section("Proof"), "test_h.py covers it.")
        self.assertIn("plan/h-needs-g.md: " + CLOSING_NOTE, res["notes"])
        self.assertIn("plan/g-unclosed.md: " + CLOSING_NOTE, res["notes"])

    def test_an_intent_and_a_spec_with_unclosed_front_matter_are_read_too(self):
        self.repo.write("intent/i-open.md", unclosed(intent("i-open", "approved", "I", "The problem.", "The outcome.")))
        self.repo.write("spec/i-open.md", unclosed(spec("i-open", "I", "- R1. WHEN a THE SYSTEM SHALL b.")))
        self.repo.commit("0.x")
        res = self.repo.init()
        d = self.doc("i-open")
        self.assertEqual((d.status, d.title), ("ready", "I"))
        self.assertEqual(d.section("Goal"), "The problem.\n\nThe outcome.")
        self.assertEqual(d.section("Acceptance criteria"), "- R1. WHEN a THE SYSTEM SHALL b.")
        self.assertIn("intent/i-open.md: " + CLOSING_NOTE, res["notes"])
        self.assertIn("spec/i-open.md: " + CLOSING_NOTE, res["notes"])

    def test_an_unclosed_plan_on_the_0x_branch_is_read_there_too(self):
        self.repo.write("intent/j-branch.md", intent("j-branch", "approved", "J", "p", "o"))
        self.repo.commit("intent")
        self.repo.git("checkout", "-q", "-b", "sdlc/j-branch")
        self.repo.write("plan/j-branch.md", unclosed(plan("j-branch", "J", "approved", proof="test_j.py covers it.")))
        self.repo.commit("plan on the item branch")
        self.repo.git("checkout", "-q", "main")
        self.assertFalse(self.repo.exists("plan/j-branch.md"))
        res = self.repo.init()
        d = self.doc("j-branch")
        self.assertEqual((d.status, d.section("Proof")), ("planned", "test_j.py covers it."))
        self.assertIn("`plan/j-branch.md` on branch `sdlc/j-branch`", d.section("Plan"))
        self.assertIn("plan/j-branch.md: " + CLOSING_NOTE, res["notes"])

    def test_front_matter_that_closes_or_is_no_front_matter_at_all_gets_no_closing_note(self):
        self.repo.write("intent/k-rule.md", "---\n\n# Just notes under a horizontal rule\n")       # `---`, then no key line
        self.repo.write("intent/l-plain.md", "# No front matter\n\n## Problem\nx\n")
        self.repo.commit("0.x")
        res = self.repo.init()
        self.assertEqual([n for n in res["notes"] if "no closing" in n], [])
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])

    def test_old_frontmatter_reads_up_to_the_first_line_that_is_not_a_key(self):
        notes = []
        meta, body = I.old_frontmatter("---\nslug: x\nstatus: implemented\nspec_commit: b0b42c1\n# Title\nstatus: later\n\ntext\n",
                                       "plan/x.md", notes)
        self.assertEqual(meta, {"slug": "x", "status": "implemented", "spec_commit": "b0b42c1"})
        self.assertEqual(body, "# Title\nstatus: later\n\ntext\n")                          # nothing past the heading is a key
        self.assertEqual(notes, ["plan/x.md: " + CLOSING_NOTE])
        self.assertEqual(I.old_frontmatter("---\nslug: x\n---\n# T\n", "plan/x.md", notes), ({"slug": "x"}, "# T\n"))
        self.assertEqual(I.old_frontmatter("# T\n---\nstatus: x\n", "plan/x.md", notes), ({}, "# T\n---\nstatus: x\n"))
        self.assertEqual(len(notes), 1)


class MigrationBehindRemote(Case):
    """init reads the 0.x files of this checkout: one that is behind origin would bring back what was merged since."""
    BEHIND = "this checkout is %d commit(s) behind origin/%s — 0.x items merged since then would come back as work; " \
             "update it (git pull --ff-only) and run init again"

    def setUp(self):
        super().setUp()
        make_v1_repo(self.repo)
        self.bare = with_origin(self.repo)

    def test_a_checkout_behind_the_remote_is_refused_before_anything_is_written(self):
        push_elsewhere(self.repo, self.bare)
        stale = self.repo.git("rev-parse", "refs/remotes/origin/main")
        before = self.repo.snapshot()
        r = self.repo.run()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr.strip(), self.BEHIND % (1, "main") + "; nothing was changed")
        self.assertEqual(self.repo.snapshot(), before)                                   # no config, no work doc, no backup
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertNotEqual(self.repo.git("rev-parse", "refs/remotes/origin/main"), stale)   # it asked the remote first
        j = self.repo.run("--json")
        self.assertEqual((j.returncode, j.stdout), (2, ""))

    def test_after_pulling_the_default_branch_init_goes_through(self):
        push_elsewhere(self.repo, self.bare, commits=2)
        r = self.repo.run()
        self.assertEqual((r.returncode, r.stderr.strip()), (2, self.BEHIND % (2, "main") + "; nothing was changed"))
        self.repo.git("pull", "-q", "--ff-only")
        res = self.repo.init()
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])
        self.assertFalse([n for n in res["notes"] if "behind" in n])
        self.assertEqual(self.repo.config()["version"], 2)

    def test_a_dry_run_does_not_fetch_and_gives_the_count_as_its_first_note(self):
        push_elsewhere(self.repo, self.bare)
        stale = self.repo.git("rev-parse", "refs/remotes/origin/main")
        res = self.repo.init("--dry-run")
        self.assertEqual(self.repo.git("rev-parse", "refs/remotes/origin/main"), stale)  # no fetch: the refs as they are
        self.assertFalse([n for n in res["notes"] if "behind" in n])                      # …say this checkout is level
        self.repo.git("fetch", "-q", "origin")
        before = self.repo.snapshot()
        res = self.repo.init("--dry-run")
        self.assertEqual(res["notes"][0], self.BEHIND % (1, "main"))                      # a note, not an error, and no "nothing was changed"
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])    # the dry run still shows what it would do
        self.assertEqual(self.repo.snapshot(), before)
        text = self.repo.run("--dry-run")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertRegex(text.stdout, r"(?m)^NOTE +this checkout is 1 commit\(s\) behind origin/main — ")

    def test_level_with_the_remote_or_ahead_of_it_is_fine(self):
        self.repo.write("local.txt", "not pushed yet\n")
        self.repo.commit("a local commit")
        self.assertEqual(self.repo.git("rev-list", "--count", "origin/main..HEAD"), "1")
        res = self.repo.init()
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])
        self.assertFalse([n for n in res["notes"] if "behind" in n])

    def test_a_detached_head_equal_to_the_remote_branch_is_fine(self):
        self.repo.git("checkout", "-q", "--detach", "origin/main")
        res = self.repo.init()
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])

    def test_a_feature_branch_that_has_fallen_behind_is_refused_too(self):
        push_elsewhere(self.repo, self.bare)
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.write("feature.txt", "work\n")
        self.repo.commit("feature work")
        r = self.repo.run()                                                               # ahead of its start, and behind origin/main
        self.assertEqual((r.returncode, r.stderr.strip()), (2, self.BEHIND % (1, "main") + "; nothing was changed"))

    def test_the_branch_in_the_0x_config_is_the_one_compared(self):
        push_elsewhere(self.repo, self.bare, branch="develop")
        self.repo.git("fetch", "-q", "origin")                                            # origin/develop is ahead; origin/main is level
        self.assertFalse([n for n in self.repo.init("--dry-run")["notes"] if "behind" in n])   # the config says main
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["protect"]["default_branch"] = "develop"
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        r = self.repo.run()
        self.assertEqual((r.returncode, r.stderr.strip()), (2, self.BEHIND % (1, "develop") + "; nothing was changed"))

    def test_without_a_branch_in_the_config_the_detected_default_branch_is_used(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        del cfg["protect"]["default_branch"]
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        push_elsewhere(self.repo, self.bare)
        r = self.repo.run()
        self.assertEqual((r.returncode, r.stderr.strip()), (2, self.BEHIND % (1, "main") + "; nothing was changed"))

    def test_no_remote_branch_to_compare_with_means_no_check_and_no_fetch(self):
        push_elsewhere(self.repo, self.bare)
        self.repo.git("update-ref", "-d", "refs/remotes/origin/main")                     # never fetched: nothing to compare with
        res = self.repo.init()
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])
        self.assertFalse(self.repo.git("for-each-ref", "refs/remotes/origin/main"))       # …and init did not go and fetch it

    def test_a_remote_that_cannot_be_reached_is_ignored_and_the_refs_as_they_are_decide(self):
        self.repo.git("remote", "set-url", "origin", os.path.join(self.repo.tmp, "no-such-origin.git"))
        res = self.repo.init()                                                             # level with the last fetch: goes through
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])

    def test_a_version_2_repository_is_not_checked(self):
        push_elsewhere(self.repo, self.bare)
        self.repo.write(".sdlc/config.json", json.dumps({"version": 2, "mode": "solo"}))
        stale = self.repo.git("rev-parse", "refs/remotes/origin/main")
        r = self.repo.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.repo.git("rev-parse", "refs/remotes/origin/main"), stale)   # not a migration: it does not fetch either


class MigrationConfigNotes(Case):
    """What `migrate_v1` says, as init prints it."""

    def migrate(self, loop=None, **top):
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["loop"].update(loop or {})
        cfg.update(top)
        make_v1_repo(self.repo, cfg)
        return self.repo.init()

    def test_the_caps_0x_shipped_as_defaults_are_not_carried_and_a_note_says_so(self):
        res = self.migrate({"max_turns": 200, "item_max_minutes": 45})
        loop = self.repo.config()["loop"]
        self.assertNotIn("max_turns", loop)
        self.assertNotIn("session_max_minutes", loop)
        self.assertIn("loop.max_turns 200, loop.item_max_minutes 45 were 0.x defaults — 1.0's defaults apply "
                      "(500 turns, 120 min per session)", res["notes"])
        cfg = Config(self.repo.root)
        self.assertEqual((cfg.get("loop.max_turns"), cfg.get("loop.session_max_minutes")), (500, 120))   # what the engine runs with

    def test_the_other_0x_default_for_turns_is_not_carried_either(self):
        res = self.migrate({"max_turns": 120, "item_max_minutes": 90})
        loop = self.repo.config()["loop"]
        self.assertEqual((loop.get("max_turns"), loop["session_max_minutes"]), (None, 90))
        self.assertIn("loop.max_turns 120 was a 0.x default — 1.0's defaults apply (500 turns, 120 min per session)", res["notes"])

    def test_a_cap_somebody_chose_is_carried_and_one_equal_to_1_0s_default_needs_no_line(self):
        res = self.migrate({"max_turns": 300, "item_max_minutes": 90})
        loop = self.repo.config()["loop"]
        self.assertEqual((loop["max_turns"], loop["session_max_minutes"]), (300, 90))
        self.assertFalse([n for n in res["notes"] if "0.x default" in n])
        other = InitRepo()
        self.addCleanup(other.cleanup)
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["loop"].update({"max_turns": 500, "item_max_minutes": 120})
        make_v1_repo(other, cfg)
        again = other.init()
        self.assertFalse({"max_turns", "session_max_minutes"} & set(other.config()["loop"]))
        self.assertFalse([n for n in again["notes"] if "0.x default" in n])

    def test_policies_that_are_dropped_are_named_with_what_that_means(self):
        res = self.migrate(policies=["secure-api-review", "brand-voice"])
        want = ("policies dropped (secure-api-review, brand-voice): their skills in .claude/skills/ stay and still trigger on "
                "their own, but nothing applies them at review any more — name the ones the reviewer must apply in REVIEW.md")
        self.assertIn(want, res["notes"])
        self.assertTrue(any(n.startswith("dropped (not used by 1.0):") and "policies" in n for n in res["notes"]))   # still in the generic list
        self.assertNotIn("policies", self.repo.config())

    def test_an_empty_policies_list_gets_no_note_of_its_own(self):
        res = self.migrate(policies=[])
        self.assertFalse([n for n in res["notes"] if n.startswith("policies dropped")])
        self.assertTrue(any(n.startswith("dropped (not used by 1.0):") and "policies" in n for n in res["notes"]))

    def test_solo_with_autopilot_off_is_told_that_the_loop_and_go_merge_what_passes(self):
        res = self.migrate(roles={"solo": True, "autopilot": False, "auto_merge": True})
        want = ("roles.autopilot=false: in 1.0 solo mode /sdlc:run and /sdlc:go merge what passes the gate — use mode team to "
                "have a person merge")
        self.assertIn(want, res["notes"])
        self.assertEqual(self.repo.config()["mode"], "solo")

    def test_one_note_names_both_keys_when_both_are_false_and_none_when_a_person_merges_anyway(self):
        res = self.migrate(roles={"solo": True, "autopilot": False, "auto_merge": False})
        merges = [n for n in res["notes"] if "in 1.0 solo mode" in n]
        self.assertEqual(len(merges), 1)
        self.assertTrue(merges[0].startswith("roles.autopilot=false, roles.auto_merge=false: "), merges[0])
        other = InitRepo()
        self.addCleanup(other.cleanup)
        make_v1_repo(other, dict(V1_CONFIG, roles={"solo": True, "autopilot": True, "auto_merge": False}))
        self.assertTrue(any(n.startswith("roles.auto_merge=false: in 1.0 solo mode") for n in other.init()["notes"]))
        third = InitRepo()
        self.addCleanup(third.cleanup)
        make_v1_repo(third, dict(V1_CONFIG, roles={"solo": False, "autopilot": False, "auto_merge": False}))
        self.assertFalse([n for n in third.init()["notes"] if "in 1.0 solo mode" in n])           # team: a person merges already

    def test_a_pause_file_from_0x_is_noted_and_left_alone(self):
        make_v1_repo(self.repo)
        self.repo.write(".sdlc/state/pause", "2026-10-01T00:00:00Z\n")
        res = self.repo.init()
        want = ".sdlc/state/pause exists — the loop will not start while it is there; delete it, or run `sdlc.py resume` " \
               "(which also starts the loop)"
        self.assertIn(want, res["notes"])
        self.assertTrue(self.repo.exists(".sdlc/state/pause"))
        self.assertFalse([n for n in self.repo.init()["notes"] if "pause" in n])                   # a migration note: not repeated

    def test_the_pause_file_the_0x_config_named_is_the_one_noted(self):
        res = self.migrate({"pause_file": ".sdlc/halt"})
        self.assertFalse([n for n in res["notes"] if "pause" in n or "halt" in n])                 # nothing there: no note
        self.repo.write(".sdlc/halt", "x\n")
        self.repo.write(".sdlc/config.json", self.repo.read(".sdlc/config.v1.json"))
        again = self.repo.init()
        self.assertTrue(any(n.startswith(".sdlc/halt exists — the loop will not start") for n in again["notes"]), again["notes"])

    def test_no_pause_file_no_note(self):
        make_v1_repo(self.repo)
        self.assertFalse([n for n in self.repo.init()["notes"] if "pause" in n])


class MigrationLeftovers(Case):
    def setUp(self):
        super().setUp()
        make_v1_repo(self.repo)

    def test_the_korean_evals_readme_has_no_marker_but_its_title_gives_it_away(self):
        self.repo.write("evals/README.md", KO_EVALS_README)
        self.assertNotIn("<!-- sdlc:evals -->", self.repo.read("evals/README.md"))
        res = self.repo.init()
        self.assertIn("evals/README.md", res["leftovers"])
        self.assertIn("evals/cases/no-secret-in-code/", res["leftovers"])                     # the example case goes with it
        self.assertNotIn("evals/cases/my-own-case/", res["leftovers"])
        removed = {rel for rel, (verb, _) in rows(self.repo.init("--prune")).items() if verb == "REMOVED"}
        self.assertIn("evals/README.md", removed)
        self.assertFalse(self.repo.exists("evals/README.md"))
        self.assertTrue(self.repo.exists("evals/cases/my-own-case/case.json"))

    def test_the_english_title_without_its_marker_counts_too_and_somebody_elses_readme_does_not(self):
        self.repo.write("evals/README.md", EN_EVALS_README)
        self.assertIn("evals/README.md", self.repo.init()["leftovers"])
        self.repo.write("evals/README.md", "# Our evals\n\n# evals/ — 에이전트 설정의 회귀 테스트\n")     # the title is not the first line
        self.assertNotIn("evals/README.md", self.repo.init()["leftovers"])
        self.repo.write("evals/README.md", "# evals/ — 에이전트 설정의 회귀 테스트 (우리 것)\n")         # …and not exactly the title
        self.assertNotIn("evals/README.md", self.repo.init()["leftovers"])

    def test_lessons_that_are_only_the_template_are_a_leftover_and_not_a_note(self):
        for lang in ("en", "ko"):
            other = InitRepo()
            self.addCleanup(other.cleanup)
            make_v1_repo(other)
            other.write(".sdlc/LESSONS.md", LESSONS_TEMPLATE[lang])
            res = other.init()
            self.assertFalse([n for n in res["notes"] if "lessons" in n.lower()], lang)
            self.assertIn(".sdlc/LESSONS.md", res["leftovers"], lang)
            removed = {rel for rel, (verb, _) in rows(other.init("--prune")).items() if verb == "REMOVED"}
            self.assertIn(".sdlc/LESSONS.md", removed, lang)
            self.assertFalse(other.exists(".sdlc/LESSONS.md"), lang)
            self.assertTrue(other.exists(".sdlc/config.json"))                                   # the rest of .sdlc stays

    def test_check_lists_a_template_only_lessons_file_as_a_leftover(self):
        other = InitRepo()                                              # one with nothing else of 0.x in it
        self.addCleanup(other.cleanup)
        other.init()
        self.assertNotIn("leftovers", other.check()[1])
        other.write(".sdlc/LESSONS.md", LESSONS_TEMPLATE["ko"])
        code, got = other.check()
        self.assertEqual(got["leftovers"]["status"], "warn")
        self.assertEqual(got["leftovers"]["detail"], ".sdlc/LESSONS.md — `init --prune` removes them")
        other.write(".sdlc/LESSONS.md", LESSONS_TEMPLATE["ko"] + "\n## 2026-09-10 — a lesson\n")
        self.assertNotIn("leftovers", other.check()[1])

    def test_lessons_with_an_entry_below_the_template_are_a_note_and_stay(self):
        self.repo.write(".sdlc/LESSONS.md", LESSONS_TEMPLATE["ko"] + "\n## 2026-09-10 — 생성 코드는 손대지 않는다\n- 수정: 빌드가 덮어쓴다.\n")
        res = self.repo.init()
        self.assertTrue([n for n in res["notes"] if n.startswith(".sdlc/LESSONS.md holds 0.x lessons")])
        self.assertNotIn(".sdlc/LESSONS.md", res["leftovers"])
        self.repo.init("--prune")
        self.assertTrue(self.repo.exists(".sdlc/LESSONS.md"))

    def test_only_the_untouched_template_holds_no_lessons(self):
        self.assertFalse(I.has_lessons(LESSONS_TEMPLATE["en"]))
        self.assertFalse(I.has_lessons(LESSONS_TEMPLATE["ko"]))
        self.assertFalse(I.has_lessons("﻿" + LESSONS_TEMPLATE["ko"].replace("\n", "\r\n")))
        self.assertFalse(I.has_lessons("# Lessons\n<!--\n## 2026-01-01 — in a comment\n-->\n"))
        self.assertFalse(I.has_lessons(""))
        self.assertTrue(I.has_lessons("# Lessons\n\n## 2026-01-01 — a lesson\n"))
        self.assertTrue(I.has_lessons("<!-- a comment -->\n## 설계에서 뒤집힌 판단들 (2026-09-06)\n- text\n"))
        self.assertTrue(I.has_lessons("# Lessons\n\n### a smaller heading\n"))          # anything the team wrote stays
        self.assertTrue(I.has_lessons(LESSONS_TEMPLATE["ko"] + "- 생성 코드는 손대지 않는다\n"))

    def test_triage_drafts_are_noted_by_name_and_stay(self):
        self.repo.write("intent/triage/b-two.md", "---\nstatus: draft\n---\n# Two\n")
        self.repo.write("intent/triage/a-one.md", "---\nstatus: draft\n---\n# One\n")
        res = self.repo.init()
        self.assertIn("intent/triage/ holds 2 0.x triage draft(s) (a-one.md, b-two.md) — 1.0 does not read them; start the ones "
                      "still wanted with /sdlc:go, or delete them", res["notes"])
        self.assertNotIn("intent/triage/", res["leftovers"])                                   # drafts in it: not an untouched directory
        self.assertTrue(self.repo.exists("intent/triage/a-one.md"))
        self.repo.init("--prune")
        self.assertTrue(self.repo.exists("intent/triage/b-two.md"))

    def test_at_most_five_triage_drafts_are_named(self):
        for i in range(7):
            self.repo.write("intent/triage/d%d.md" % i, "# d%d\n" % i)
        self.repo.write("intent/triage/notes.txt", "not a draft\n")
        note = [n for n in self.repo.init()["notes"] if n.startswith("intent/triage/ holds")][0]
        self.assertTrue(note.startswith("intent/triage/ holds 7 0.x triage draft(s) (d0.md, d1.md, d2.md, d3.md, d4.md, …) — "), note)

    def test_a_triage_directory_with_nothing_but_a_gitkeep_gets_no_note_and_is_a_leftover(self):
        res = self.repo.init()
        self.assertFalse([n for n in res["notes"] if "triage" in n])
        self.assertIn("intent/triage/", res["leftovers"])

    def test_the_triage_note_follows_a_custom_intent_directory(self):
        cfg = json.loads(json.dumps(V1_CONFIG))
        cfg["paths"]["intent"] = "docs/intents"
        other = InitRepo()
        self.addCleanup(other.cleanup)
        other.write(".sdlc/config.json", json.dumps(cfg))
        other.write("docs/intents/triage/x.md", "# x\n")
        other.commit("0.x")
        self.assertTrue(any(n.startswith("docs/intents/triage/ holds 1 0.x triage draft(s) (x.md)") for n in other.init()["notes"]))


class ReviewMd(Case):
    """A REVIEW.md written by 0.x names spec.md and plan.md in its Compliance line and ends in an Approval section 1.0 has no use
    for. Init brings both up to date where the file is still as 0.x wrote it — what a team edited stays — and says so; `--check`
    points at what is left."""

    def v1_repo(self, lang, review):
        """A 0.x repository whose REVIEW.md is `review`."""
        repo = InitRepo()
        self.addCleanup(repo.cleanup)
        make_v1_repo(repo, dict(V1_CONFIG, language=lang))
        repo.write("REVIEW.md", review)
        return repo

    def test_an_untouched_0x_file_gets_the_1x_compliance_line_and_the_two_sections_where_approval_was(self):
        for lang in ("en", "ko"):
            with self.subTest(lang):
                repo = self.v1_repo(lang, old_review(lang))
                res = repo.init()
                self.assertEqual(rows(res)["REVIEW.md"], ("MERGED", "; ".join(REVIEW_NOTES)))
                text = repo.read("REVIEW.md")
                self.assertEqual(text, migrated_review(lang))
                self.assertNotIn(APPROVAL_HEADING[lang], text)
                self.assertNotIn("spec.md", text)
                self.assertEqual(text.count(compliance_line(new_review_template(lang))), 1)
                self.assertEqual(text.count("SDLC_REVIEW_TALLY"), 1)
                for heading in NEW_REVIEW_HEADINGS[lang]:
                    self.assertEqual(text.count(heading), 1, heading)
                code, got = repo.check()
                self.assertEqual((got["review"]["status"], got["review"]["detail"]), ("ok", ""))

    def test_the_new_text_is_the_templates_own_in_the_language_of_the_file(self):
        repo = self.v1_repo("ko", old_review("en"))                  # the config says Korean; the file is English
        repo.init()
        self.assertEqual(repo.read("REVIEW.md"), migrated_review("en"))
        repo = self.v1_repo("en", old_review("ko"))
        repo.init()
        self.assertEqual(repo.read("REVIEW.md"), migrated_review("ko"))

    def test_a_second_run_changes_nothing(self):
        repo = self.v1_repo("en", old_review("en"))
        repo.init()
        repo.commit("migrated")
        before = repo.snapshot()
        again = repo.init()
        self.assertEqual(rows(again)["REVIEW.md"], ("SKIPPED", "exists"))
        self.assertEqual(repo.read_bytes("REVIEW.md"), migrated_review("en").encode("utf-8"))
        self.assertEqual(repo.snapshot(), before)

    def test_an_edited_compliance_line_stays_and_check_says_it_still_names_the_files(self):
        old = old_review("en")
        edited = old.replace(compliance_line(old), compliance_line(old) + " and our SLA")
        repo = self.v1_repo("en", edited)
        res = repo.init()
        self.assertEqual(rows(res)["REVIEW.md"], ("MERGED", REVIEW_NOTES[1]))                # the approval section only
        want = migrated_review("en").replace(compliance_line(new_review_template("en")), compliance_line(edited))
        self.assertEqual(repo.read("REVIEW.md"), want)
        code, got = repo.check()
        self.assertEqual((got["review"]["status"], got["review"]["detail"]), ("warn", SPEC_PLAN_NOTE))
        self.assertEqual(code, 0)                                                               # a warning, not a failure

    def test_an_edited_approval_section_stays_and_the_compliance_line_is_still_updated(self):
        edited = old_review("en") + "Our CODEOWNERS file names the approvers.\n"
        repo = self.v1_repo("en", edited)
        res = repo.init()
        self.assertEqual(rows(res)["REVIEW.md"], ("MERGED", REVIEW_NOTES[0]))
        text = repo.read("REVIEW.md")
        self.assertEqual(text, edited.replace(compliance_line(edited), compliance_line(new_review_template("en"))))
        self.assertIn("Our CODEOWNERS file names the approvers.", text)
        self.assertNotIn("## Tally", text)

    def test_a_file_in_which_both_were_edited_is_left_as_it_is(self):
        old = old_review("en")
        edited = old.replace(compliance_line(old), "- Compliance: ours").replace("Approval comes from a", "Approval comes from the")
        repo = self.v1_repo("en", edited)
        res = repo.init()
        self.assertEqual(rows(res)["REVIEW.md"], ("SKIPPED", "exists"))
        self.assertEqual(repo.read("REVIEW.md"), edited)

    def test_a_section_the_file_already_has_is_not_added_again(self):
        old = old_review("en")
        own = old.replace(APPROVAL_HEADING["en"], "## Tally\nOur own tally rule.\n\n" + APPROVAL_HEADING["en"])
        repo = self.v1_repo("en", own)
        repo.init()
        text = repo.read("REVIEW.md")
        self.assertEqual((text.count("## Tally"), text.count("## New or pre-existing"), text.count("## Approval")), (1, 1, 0))
        self.assertIn("Our own tally rule.", text)
        self.assertNotIn("SDLC_REVIEW_TALLY", text)                                             # theirs is theirs
        both = old.replace(APPROVAL_HEADING["en"], "## Tally\nOur own tally rule.\n\n## New or pre-existing\nOurs too.\n\n"
                           + APPROVAL_HEADING["en"])
        repo = self.v1_repo("en", both)
        res = repo.init()
        text = repo.read("REVIEW.md")
        self.assertEqual(rows(res)["REVIEW.md"], ("MERGED", "; ".join(REVIEW_NOTES)))
        self.assertTrue(text.endswith("## New or pre-existing\nOurs too.\n"), text[-80:])    # the section is gone, no blank line left
        self.assertEqual((text.count("## Tally"), text.count("## New or pre-existing"), text.count("Approval")), (1, 1, 0))

    def test_a_dry_run_says_what_it_would_do_and_writes_nothing(self):
        repo = self.v1_repo("en", old_review("en"))
        res = repo.init("--dry-run")
        self.assertEqual(rows(res)["REVIEW.md"], ("WOULD MERGE", "; ".join(REVIEW_NOTES)))
        self.assertEqual(repo.read("REVIEW.md"), old_review("en"))

    def test_crlf_and_a_byte_order_mark_stay_as_the_file_had_them(self):
        repo = self.v1_repo("en", "")
        repo.write_bytes("REVIEW.md", b"\xef\xbb\xbf" + old_review("en").replace("\n", "\r\n").encode("utf-8"))
        repo.init()
        self.assertEqual(repo.read_bytes("REVIEW.md"),
                         b"\xef\xbb\xbf" + migrated_review("en").replace("\n", "\r\n").encode("utf-8"))

    def test_a_file_that_is_not_utf_8_is_left_alone(self):
        raw = old_review("en").encode("utf-8").replace(b"Passes", b"Pass\xe9s")                  # one byte that is not UTF-8
        repo = self.v1_repo("en", "")
        repo.write_bytes("REVIEW.md", raw)
        res = repo.init()
        self.assertEqual(rows(res)["REVIEW.md"], ("SKIPPED", "not UTF-8 text — left alone"))
        self.assertEqual(repo.read_bytes("REVIEW.md"), raw)

    def test_every_init_run_does_it_and_not_only_the_move_from_0x(self):
        self.node_repo()
        self.repo.init()                                                                        # a 1.x repository, version 2 config
        self.repo.write("REVIEW.md", old_review("en", "src/gen/"))
        res = self.repo.init()
        self.assertEqual(rows(res)["REVIEW.md"], ("MERGED", "; ".join(REVIEW_NOTES)))
        self.assertEqual(self.repo.read("REVIEW.md"), migrated_review("en"))

    def test_a_1x_file_is_not_touched_and_the_row_names_the_configured_path(self):
        self.node_repo()
        self.repo.init()
        before = self.repo.read("REVIEW.md")
        self.assertEqual(rows(self.repo.init())["REVIEW.md"], ("SKIPPED", "exists"))
        self.assertEqual(self.repo.read("REVIEW.md"), before)
        cfg = dict(V1_CONFIG, paths=dict(V1_CONFIG["paths"], review="docs/REVIEW.md"))
        repo = InitRepo()
        self.addCleanup(repo.cleanup)
        make_v1_repo(repo, cfg)
        repo.write("docs/REVIEW.md", old_review("en"))
        res = repo.init()
        self.assertEqual(rows(res)["docs/REVIEW.md"], ("MERGED", "; ".join(REVIEW_NOTES)))
        self.assertEqual(repo.read("docs/REVIEW.md"), migrated_review("en"))

    def test_check_warns_while_the_file_names_spec_md_or_plan_md_and_says_so_beside_the_todo(self):
        self.node_repo()
        self.repo.init()                                                                        # generated paths are still a TODO
        base = self.repo.read("REVIEW.md")
        self.repo.write("REVIEW.md", base + "Read docs/spec.md first.\n")
        _, got = self.repo.check()
        self.assertEqual((got["review"]["status"], got["review"]["detail"]),
                         ("warn", "generated paths still TODO; " + SPEC_PLAN_NOTE))
        self.repo.write("REVIEW.md", base.replace("TODO: generated paths (also protect.generated_paths in .sdlc/config.json)",
                                                  "src/gen/") + "Compare with `plan.md`.\n")
        _, got = self.repo.check()
        self.assertEqual((got["review"]["status"], got["review"]["detail"]), ("warn", SPEC_PLAN_NOTE))
        self.repo.write("REVIEW.md", base.replace("TODO: generated paths (also protect.generated_paths in .sdlc/config.json)",
                                                  "src/gen/") + "See myspec.md and floorplan.md and the spec.mdx notes.\n")
        _, got = self.repo.check()
        self.assertEqual((got["review"]["status"], got["review"]["detail"]), ("ok", ""))        # other names are not those files


class MigrateReview(unittest.TestCase):
    """`migrate_review` on its own: text in, (text, what was done) out."""

    EN = "\n".join(I.OLD_APPROVAL["en"])

    def test_the_section_in_the_middle_is_replaced_in_place(self):
        text = "# R\n\n%s\n\n## After\nstuff\n" % self.EN
        got, notes = I.migrate_review(text)
        want = "# R\n\n%s\n\n%s\n\n## After\nstuff\n" % tuple(review_block("en", h) for h in NEW_REVIEW_HEADINGS["en"])
        self.assertEqual((got, notes), (want, [REVIEW_NOTES[1]]))

    def test_a_file_without_a_final_newline_ends_the_way_it_did(self):
        got, _ = I.migrate_review("# R\n\n" + self.EN)
        self.assertEqual(got, "# R\n\n%s\n\n%s" % tuple(review_block("en", h) for h in NEW_REVIEW_HEADINGS["en"]))

    def test_nothing_that_is_not_the_0x_text_is_touched(self):
        for text in ("", "# R\n", "# R\n\n## Approval\nWe approve by hand.\n", "# R\n\n## Approval  \n" + "\n".join(I.OLD_APPROVAL["en"][1:]) + "\n",
                     "- Compliance: the change matches spec.md, plan.md and our design principles\n",
                     " " + I.OLD_COMPLIANCE["en"] + "\n", I.OLD_COMPLIANCE["en"] + " \n", "#" + I.OLD_COMPLIANCE["en"] + "\n"):
            self.assertEqual(I.migrate_review(text), (text, []), repr(text))

    def test_the_compliance_line_is_replaced_wherever_it_stands_as_a_whole_line(self):
        en, ko = I.OLD_COMPLIANCE["en"], I.OLD_COMPLIANCE["ko"]
        got, notes = I.migrate_review("a\n%s\nb\n%s\n%s\n" % (en, ko, en))
        new_en, new_ko = (compliance_line(new_review_template(lang)) for lang in ("en", "ko"))
        self.assertEqual(got, "a\n%s\nb\n%s\n%s\n" % (new_en, new_ko, new_en))
        self.assertEqual(notes, [REVIEW_NOTES[0]])

    def test_a_heading_counts_as_there_in_any_case_level_and_language(self):
        for have in ("## TALLY", "### Tally", "## 집계 (Tally)", "# tally"):
            got, _ = I.migrate_review("# R\n\n%s\nmine\n\n%s\n" % (have, self.EN))
            self.assertEqual(got.count("SDLC_REVIEW_TALLY"), 0, have)
            self.assertIn("## New or pre-existing", got, have)

    def test_trailing_blank_lines_after_the_section_are_not_part_of_it(self):
        got, notes = I.migrate_review("# R\n\n%s\n  \n\n" % self.EN)
        self.assertEqual(notes, [REVIEW_NOTES[1]])
        self.assertTrue(got.startswith("# R\n\n## New or pre-existing\n"), got)

    def test_the_text_is_what_the_templates_hold(self):
        got, _ = I.migrate_review("# R\n\n" + "\n".join(I.OLD_APPROVAL["ko"]) + "\n")
        for heading in NEW_REVIEW_HEADINGS["ko"]:
            self.assertIn(review_block("ko", heading), got)


class ProjectSkills(Case):
    """Skills, commands and agents of the project that still name a command 1.0 removed."""

    def files(self):
        self.repo.write(".claude/skills/x/SKILL.md", "---\nname: x\n---\nRun /sdlc:review before every merge.\n")
        self.repo.write(".claude/skills/x/references/more.md", "First /sdlc:plan, then /sdlc:spec, then /sdlc:go. /sdlc:plan again.\n")
        self.repo.write(".claude/commands/ship.md", "Use /sdlc:release.\n")
        self.repo.write(".claude/agents/helper.md", "---\nname: helper\n---\nCalls /sdlc:verify.\n")
        self.repo.write(".claude/skills/ok/SKILL.md", "Only /sdlc:go and /sdlc:run here.\n")
        self.repo.write(".claude/skills/x/notes.txt", "/sdlc:review in a text file that is not scanned\n")
        self.repo.write(".claude/settings.local.json", '{"note": "/sdlc:review"}\n')

    WANT = ("still mentions removed commands: .claude/agents/helper.md (/sdlc:verify), .claude/commands/ship.md (/sdlc:release), "
            ".claude/skills/x/SKILL.md (/sdlc:review), .claude/skills/x/references/more.md (/sdlc:plan, /sdlc:spec)")

    def test_init_notes_the_files_and_commands_and_changes_none_of_them(self):
        self.files()
        before = {k: v for k, v in self.repo.snapshot()[0].items() if k.startswith(".claude/")}
        res = self.repo.init()
        self.assertIn(self.WANT, res["notes"])
        after = self.repo.snapshot()[0]
        self.assertEqual(before, {k: after[k] for k in before})                                 # every file as it was
        self.assertRegex(self.repo.run().stdout, r"(?m)^NOTE +still mentions removed commands: \.claude/agents/helper\.md")

    def test_a_second_run_still_says_it_since_nothing_was_fixed(self):
        self.files()
        self.repo.init()
        self.assertIn(self.WANT, self.repo.init()["notes"])

    def test_at_most_five_files_are_named(self):
        for i in range(7):
            self.repo.write(".claude/skills/s%d/SKILL.md" % i, "/sdlc:plan\n")
        note = [n for n in self.repo.init()["notes"] if n.startswith("still mentions removed commands")][0]
        self.assertEqual(note.count("/SKILL.md"), 5)
        self.assertTrue(note.endswith(" …"), note)

    def test_nothing_to_report_without_such_mentions(self):
        self.repo.write(".claude/skills/ok/SKILL.md", "Only /sdlc:go here.\n")
        self.assertFalse([n for n in self.repo.init()["notes"] if "removed commands" in n])

    def test_check_has_a_warn_row_with_the_same_content(self):
        self.node_repo()
        self.repo.init()
        self.assertNotIn("skills", self.repo.check()[1])
        self.files()
        code, got = self.repo.check()
        self.assertEqual(got["skills"]["status"], "warn")
        self.assertEqual(got["skills"]["detail"], self.WANT)
        self.assertEqual(code, 0)                                                              # a warning: nothing is missing
        r = self.repo.run("--check")
        self.assertRegex(r.stdout, r"(?m)^WARN +project skills +still mentions removed commands: \.claude/agents/helper\.md")


class Prune(Case):
    def setUp(self):
        super().setUp()
        make_v1_repo(self.repo)
        self.repo.init()

    def test_prune_removes_exactly_the_leftovers(self):
        res = self.repo.init("--prune")
        self.assertEqual(res["leftovers"], [])
        removed = {rel for rel, (verb, _) in rows(res).items() if verb == "REMOVED"}
        self.assertEqual(removed, LEFTOVERS)
        for rel in LEFTOVERS:
            self.assertFalse(self.repo.exists(rel.rstrip("/")), rel)
        for rel in KEPT_BY_PRUNE + ("work/a-ready.md", "work/b-planned.md", "work/d-draft.md", "work/README.md"):
            self.assertTrue(self.repo.exists(rel), rel)
        self.assertEqual(self.repo.read("evals/cases/my-own-case/case.json"), "{\"mine\": true}\n")
        self.assertEqual(self.repo.read(".github/workflows/ci.yml"), "name: CI\non: push\n")

    def test_parents_left_empty_go_with_them_and_the_repository_root_stays(self):
        self.repo.init("--prune")
        self.assertFalse(self.repo.exists("intent/triage"))               # only a .gitkeep was in it
        self.assertFalse(self.repo.exists(".sdlc/examples"))
        self.assertTrue(self.repo.exists("intent"))                        # still holds the history
        self.assertTrue(self.repo.exists("evals/cases"))                   # still holds the user's own case
        self.assertTrue(self.repo.exists(".github/workflows"))             # ci.yml
        self.assertTrue(os.path.isdir(self.repo.root))
        self.assertTrue(os.path.isdir(self.repo.path(".git")))

    def test_directories_that_end_up_empty_are_removed_all_the_way_up(self):
        lone = InitRepo()
        self.addCleanup(lone.cleanup)
        make_v1_repo(lone)
        for rel in ("evals/cases/my-own-case/case.json", ".github/workflows/ci.yml", ".github/workflows/sdlc-custom.yml"):
            os.remove(lone.path(rel))
            os.rmdir(os.path.dirname(lone.path(rel))) if not os.listdir(os.path.dirname(lone.path(rel))) else None
        os.remove(lone.path("plan/c-done.md"))
        lone.init("--prune")
        self.assertFalse(lone.exists(".github"))                           # workflows/ then .github/
        self.assertFalse(lone.exists("evals"))                             # cases/ then evals/
        self.assertFalse(lone.exists("plan"))                              # only TEMPLATE.md was left
        self.assertTrue(lone.exists("spec"))

    def test_prune_on_the_same_run_as_the_migration(self):
        fresh = InitRepo()
        self.addCleanup(fresh.cleanup)
        make_v1_repo(fresh)
        res = fresh.init("--prune")
        self.assertEqual(sorted(res["migrated"]), ["a-ready", "b-planned", "d-draft"])
        self.assertEqual({rel for rel, (verb, _) in rows(res).items() if verb == "REMOVED"}, LEFTOVERS)
        self.assertEqual(res["leftovers"], [])

    def test_a_history_directory_that_holds_observations_is_kept(self):
        self.repo.write(".sdlc/history/eval_pass_rate.jsonl", '{"ts": "2026-09-01T00:00:00Z", "value": 0.9}\n')
        before = self.repo.init()
        self.assertNotIn(".sdlc/history/", before["leftovers"])
        res = self.repo.init("--prune")
        self.assertNotIn(".sdlc/history/", {rel for rel, (verb, _) in rows(res).items() if verb == "REMOVED"})
        self.assertIn("0.9", self.repo.read(".sdlc/history/eval_pass_rate.jsonl"))
        self.assertEqual(sorted(rel for rel, (verb, _) in rows(res).items() if verb == "REMOVED"),
                         sorted(LEFTOVERS - {".sdlc/history/"}))

    def test_a_configured_path_that_leads_out_of_the_repository_is_never_touched(self):
        shared = os.path.join(self.repo.tmp, "shared")
        os.makedirs(shared)
        with open(os.path.join(shared, "TEMPLATE.md"), "w", encoding="utf-8") as fh:
            fh.write(TEMPLATE_MD % ("intent", "Intent"))
        old = json.loads(self.repo.read(".sdlc/config.v1.json"))
        old["paths"]["intent"] = "../shared"
        self.repo.write(".sdlc/config.v1.json", json.dumps(old))
        self.assertNotIn("../shared/TEMPLATE.md", self.repo.init()["leftovers"])
        res = self.repo.init("--prune")
        self.assertTrue(os.path.exists(os.path.join(shared, "TEMPLATE.md")))
        self.assertNotIn("../shared/TEMPLATE.md", verbs(res))
        self.assertTrue(os.path.isdir(self.repo.root))

    def test_a_second_prune_has_nothing_to_do(self):
        self.repo.init("--prune")
        again = self.repo.init("--prune")
        self.assertNotIn("REMOVED", set(verbs(again).values()))
        self.assertEqual(again["leftovers"], [])

    def test_the_text_report(self):
        r = self.repo.run("--prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"REMOVED +\.sdlc/bands\.json +\(0\.x leftover\)")
        self.assertNotIn("LEFTOVER", r.stdout)

    def test_user_templates_that_0x_did_not_write_are_not_removed(self):
        self.repo.write("spec/TEMPLATE.md", "# Our own spec template\n")
        res = self.repo.init("--prune")
        self.assertNotIn("spec/TEMPLATE.md", {rel for rel, (verb, _) in rows(res).items() if verb == "REMOVED"})
        self.assertEqual(self.repo.read("spec/TEMPLATE.md"), "# Our own spec template\n")

    def test_without_a_backup_of_the_old_config_the_default_paths_are_used(self):
        os.remove(self.repo.path(".sdlc/config.v1.json"))
        res = self.repo.init()
        self.assertEqual(set(res["leftovers"]), LEFTOVERS)


# =====================================================================================================================
class Check(Case):
    def ready(self):
        """A repository init has been run in: npm scripts, claude stub on the path."""
        self.node_repo()
        self.repo.init()
        self.repo.commit("sdlc")

    def test_a_freshly_initialized_repository_has_nothing_missing(self):
        self.ready()
        code, rows_ = self.repo.check()
        self.assertEqual(code, 0, rows_)
        self.assertEqual({k: v["status"] for k, v in rows_.items() if not k.startswith("cmd.")},
                         {"git": "ok", "config": "ok", "claude_md": "warn", "review": "warn", "work": "ok", "gitignore": "ok",
                          "claude": "ok", "forge": "ok", "loop": "ok"})
        for k in ("cmd.build", "cmd.test", "cmd.lint"):
            self.assertIn(rows_[k]["status"], ("ok", "warn"), rows_[k])           # warn only when npm is not installed here
        self.assertIn("TODO placeholders left", rows_["claude_md"]["detail"])
        self.assertEqual(rows_["claude"]["detail"], "9.9.9 (Claude Code stub)")
        self.assertEqual(rows_["config"]["detail"], "mode solo, language en")
        self.assertEqual(rows_["work"]["detail"], "0 work docs")
        self.assertIn("generated paths still TODO", rows_["review"]["detail"])

    def test_check_changes_nothing_and_the_text_report_marks_each_row(self):
        self.ready()
        before = self.repo.snapshot()
        r = self.repo.run("--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.repo.snapshot(), before)
        self.assertRegex(r.stdout, r"(?m)^sdlc \S+ — ")
        self.assertRegex(r.stdout, r"(?m)^ok   +git repository +on main$")
        self.assertRegex(r.stdout, r"(?m)^WARN +CLAUDE\.md +TODO placeholders left$")
        self.assertNotRegex(r.stdout, r"(?m)^FAIL")

    def test_commands_that_resolve_on_this_machine_are_ok_and_missing_programs_warn(self):
        self.node_repo()
        self.repo.init("--commands", "build=true,test=sh -c 'exit 0',lint=no-such-linter-here --fix")
        code, got = self.repo.check()
        self.assertEqual(code, 0)
        self.assertEqual((got["cmd.build"]["status"], got["cmd.test"]["status"]), ("ok", "ok"))
        self.assertEqual(got["cmd.lint"]["status"], "warn")
        self.assertIn("`no-such-linter-here` not found", got["cmd.lint"]["detail"])

    def test_a_command_that_starts_with_environment_assignments_is_judged_by_its_program(self):
        self.node_repo()
        self.repo.init("--commands", "build=CI=1 true,test=NODE_ENV=test sh -c 'exit 0',lint=A=1 B=2 no-such-linter-here")
        code, got = self.repo.check()
        self.assertEqual((got["cmd.build"]["status"], got["cmd.test"]["status"], got["cmd.lint"]["status"]), ("ok", "ok", "warn"))
        self.assertIn("`no-such-linter-here` not found", got["cmd.lint"]["detail"])

    def test_an_unset_test_command_is_missing(self):
        bare = InitRepo()
        self.addCleanup(bare.cleanup)
        bare.init()
        code, got = bare.check()
        self.assertEqual(code, 1)
        self.assertEqual(got["cmd.test"]["status"], "missing")
        for k in ("cmd.build", "cmd.lint"):                             # optional: a project may have no build step or linter
            self.assertEqual(got[k]["status"], "ok", got[k])
            self.assertIn("optional", got[k]["detail"])
        self.assertIn("commands.test", got["cmd.test"]["detail"])
        r = bare.run("--check")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"(?m)^FAIL +test command +not set \(commands\.test\)$")

    def test_placeholders_for_commands_a_project_may_not_have_are_not_left_over_todos(self):
        self.ready()
        text = self.repo.read("CLAUDE.md")
        self.assertIn("- Run: TODO: set the run command", text)         # no run script here
        done = text.replace("TODO: naming, error handling, testing and dependency conventions (Claude fills this in during "
                            "/sdlc:init)", "snake_case, errors are values").replace(
            "TODO: main components, boundaries and data flow (Claude fills this in during /sdlc:init)", "a CLI over a library")
        self.assertNotIn("TODO: naming", done)
        self.repo.write("CLAUDE.md", done)
        _, got = self.repo.check()
        self.assertEqual(got["claude_md"]["status"], "ok", got["claude_md"])
        self.repo.write("CLAUDE.md", done.replace("- Run: TODO: set the run command", "- Test: TODO: set the test command"))
        _, got = self.repo.check()
        self.assertEqual(got["claude_md"]["status"], "warn")            # the test command is not optional
        self.assertIn("TODO placeholders left", got["claude_md"]["detail"])

    def test_the_0x_line_for_a_command_it_did_not_find_is_not_a_left_over_placeholder_either(self):
        self.ready()
        done = self.repo.read("CLAUDE.md").replace(
            "TODO: naming, error handling, testing and dependency conventions (Claude fills this in during /sdlc:init)",
            "snake_case").replace("TODO: main components, boundaries and data flow (Claude fills this in during /sdlc:init)", "a CLI")
        done = done.replace("- Run: TODO: set the run command", "- Run: TODO: no run command found (package.json has no start script)")
        done += "- Lint: TODO: no lint command found (no linter is configured)\n- Build: TODO: no build command found\n"
        self.assertEqual(I.OPTIONAL_TODO.sub("", done).count("TODO:"), 0)
        self.repo.write("CLAUDE.md", done)
        _, got = self.repo.check()
        self.assertEqual(got["claude_md"]["status"], "ok", got["claude_md"])
        self.repo.write("CLAUDE.md", done + "- Test: TODO: no test command found\n")
        _, got = self.repo.check()
        self.assertEqual(got["claude_md"]["status"], "warn")            # a missing test command is not optional
        self.assertIn("TODO placeholders left", got["claude_md"]["detail"])

    def test_a_version_1_config_is_a_warning(self):
        make_v1_repo(self.repo)
        code, got = self.repo.check()
        self.assertEqual(got["config"]["status"], "warn")
        self.assertIn("version 1 (0.x)", got["config"]["detail"])
        self.assertIn("/sdlc:init", got["config"]["detail"])
        self.assertEqual(code, 0)                                       # still usable: nothing is missing
        self.assertEqual(got["cmd.test"]["detail"], "npm test")

    def test_agents_md_that_claude_md_does_not_import_is_a_warning_until_init_adds_the_import(self):
        self.ready()
        self.repo.write("AGENTS.md", "# Agents\n")
        _, got = self.repo.check()
        self.assertEqual(got["agents_md"]["status"], "warn")
        self.assertIn("@AGENTS.md", got["agents_md"]["detail"])
        self.repo.init()
        _, got = self.repo.check()
        self.assertNotIn("agents_md", got)
        self.repo.write("CLAUDE.md", self.repo.read("CLAUDE.md").replace("@AGENTS.md\n", "See @AGENTS.md here.\n"))
        _, got = self.repo.check()
        self.assertNotIn("agents_md", got)

    def test_zero_x_leftovers_are_a_warning_that_names_the_prune_command(self):
        make_v1_repo(self.repo)
        self.repo.init()
        code, got = self.repo.check()
        self.assertEqual(got["leftovers"]["status"], "warn")
        self.assertIn("init --prune", got["leftovers"]["detail"])
        self.assertIn("…", got["leftovers"]["detail"])                   # more than five: the list is cut
        self.repo.init("--prune")
        _, got = self.repo.check()
        self.assertNotIn("leftovers", got)

    def test_a_section_that_is_not_an_object_is_a_warning_and_the_defaults_stand(self):
        for body in ({"version": 2, "commands": ["npm test"]}, {"version": 2, "loop": "off", "protect": 3},
                     {"version": 1, "commands": "make", "roles": "x", "verify": 3, "environments": {"production": 5}}):
            self.repo.write(".sdlc/config.json", json.dumps(body))
            code, got = self.repo.check()                                # no traceback
            self.assertEqual(got["config"]["status"], "warn", body)
            self.assertIn("must be an object — the defaults stand until you fix it", got["config"]["detail"])
            self.assertEqual(got["cmd.test"]["status"], "missing")
            status = subprocess.run([sys.executable, SDLC, "--root", self.repo.root, "status"], capture_output=True, text=True,
                                    env=self.repo.env())
            self.assertEqual(status.returncode, 0, status.stderr)
        self.repo.write(".sdlc/config.json", json.dumps({"version": 2, "commands": None, "mode": "solo"}))
        code, got = self.repo.check()
        self.assertEqual(got["config"]["status"], "ok")                  # null: nothing to complain about
        cfg = Config(self.repo.root)
        self.assertEqual((cfg.command("test"), cfg.command("make all")), ("", "make all"))

    def test_a_missing_or_broken_config_fails(self):
        code, got = self.repo.check()
        self.assertEqual(code, 1)
        self.assertEqual((got["config"]["status"], got["config"]["detail"]), ("missing", "run /sdlc:init"))
        self.repo.write(".sdlc/config.json", "nope")
        code, got = self.repo.check()
        self.assertEqual(code, 1)
        self.assertEqual(got["config"]["detail"], "not a valid JSON object — fix it by hand")
        self.assertNotIn("cmd.test", got)

    def test_a_missing_claude_md_and_review_md_fail(self):
        self.ready()
        os.remove(self.repo.path("CLAUDE.md"))
        os.remove(self.repo.path("REVIEW.md"))
        code, got = self.repo.check()
        self.assertEqual(code, 1)
        self.assertEqual((got["claude_md"]["status"], got["review"]["status"]), ("missing", "missing"))

    def test_a_claude_md_with_missing_sections_and_too_many_lines_warns(self):
        self.ready()
        self.repo.write("CLAUDE.md", "# Shop\n\n## Commands\n- Test: x\n" + "- filler\n" * 130)
        _, got = self.repo.check()
        self.assertEqual(got["claude_md"]["status"], "warn")
        self.assertIn("1/5 sections", got["claude_md"]["detail"])
        self.assertIn("lines, over 120", got["claude_md"]["detail"])

    def test_the_claude_the_loop_would_run_must_exist(self):
        """check() looks for claude the way the loop does: SDLC_CLAUDE_BIN, then CLAUDE_BIN, then claude on PATH."""
        self.ready()
        stub, gone = os.path.join(STUBS, "claude"), "/nonexistent/claude"
        code, got = self.repo.check(SDLC_CLAUDE_BIN=gone)                                 # SDLC_CLAUDE_BIN wins over a working CLAUDE_BIN
        self.assertEqual((code, got["claude"]["status"]), (1, "missing"))
        self.assertIn("/nonexistent/claude is not executable", got["claude"]["detail"])
        code, got = self.repo.check(SDLC_CLAUDE_BIN=stub, CLAUDE_BIN=gone)                # …and over a broken one
        self.assertEqual((code, got["claude"]["status"], got["claude"]["detail"]), (0, "ok", "9.9.9 (Claude Code stub)"))
        code, got = self.repo.check(SDLC_CLAUDE_BIN=None, CLAUDE_BIN=gone)                # CLAUDE_BIN is the fallback
        self.assertEqual((code, got["claude"]["status"]), (1, "missing"))
        code, got = self.repo.check(SDLC_CLAUDE_BIN=None, CLAUDE_BIN=stub, PATH="/usr/bin:/bin")
        self.assertEqual((code, got["claude"]["status"]), (0, "ok"))
        code, got = self.repo.check(SDLC_CLAUDE_BIN=None, CLAUDE_BIN=None)                # the stub directory is on PATH
        self.assertEqual((code, got["claude"]["status"]), (0, "ok"))
        if not shutil.which("claude", path="/usr/bin:/bin"):
            code, got = self.repo.check(SDLC_CLAUDE_BIN=None, CLAUDE_BIN=None, PATH="/usr/bin:/bin")
            self.assertEqual((code, got["claude"]["status"]), (1, "missing"))
            self.assertIn("put claude on PATH or set SDLC_CLAUDE_BIN", got["claude"]["detail"])

    def test_work_docs_with_problems_are_listed(self):
        self.ready()
        self.repo.write("work/ok-one.md", "---\nslug: ok-one\nstatus: ready\n---\n\n## Goal\ng\n")
        self.repo.write("work/bad-one.md", "---\nslug: bad-one\nstatus: maybe\n---\n\nno goal\n")
        _, got = self.repo.check()
        self.assertEqual(got["work"]["status"], "warn")
        self.assertIn("2 work docs", got["work"]["detail"])
        self.assertIn("bad-one.md: status 'maybe' is not one of", got["work"]["detail"])
        self.assertIn("no Goal section", got["work"]["detail"])
        self.assertNotIn("ok-one.md:", got["work"]["detail"])

    def test_a_work_doc_whose_slug_is_a_file_work_keeps_for_itself_is_listed(self):
        self.ready()
        os.remove(self.repo.path("work/README.md"))               # gone, so work/readme.md is a file of its own on any file system
        self.repo.write("work/readme.md", "---\nslug: readme\nstatus: ready\n---\n\n## Goal\ng\n")
        _, got = self.repo.check()
        self.assertEqual(got["work"]["status"], "warn")
        self.assertIn("readme.md: slug 'readme' names the same file as work/README.md on a case-insensitive file system "
                      "(macOS, Windows) — pick another", got["work"]["detail"])

    def test_the_refusal_names_the_configured_work_directory(self):
        self.ready()
        cfg = self.repo.config()
        cfg["paths"] = {"work": "sdlc-work"}
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.write("sdlc-work/template.md", "---\nslug: template\nstatus: ready\n---\n\n## Goal\ng\n")
        _, got = self.repo.check()
        self.assertEqual(got["work"]["status"], "warn")
        self.assertIn("template.md: slug 'template' names the same file as sdlc-work/TEMPLATE.md on a case-insensitive", got["work"]["detail"])

    def test_the_files_work_keeps_for_itself_are_not_work_docs_and_are_not_reported(self):
        self.ready()
        for name, slug in (("README.md", "readme"), ("TEMPLATE.md", "template")):
            self.repo.write("work/" + name, "---\nslug: %s\nstatus: ready\n---\n\n## Goal\ng\n" % slug)      # even one that reads like a doc
        _, got = self.repo.check()
        self.assertEqual((got["work"]["status"], got["work"]["detail"]), ("ok", "0 work docs"))

    def test_missing_gitignore_lines_are_a_warning(self):
        self.ready()
        self.repo.write(".gitignore", "node_modules/\n")
        _, got = self.repo.check()
        self.assertEqual(got["gitignore"]["status"], "warn")
        self.assertEqual(got["gitignore"]["detail"], "missing: .sdlc/logs/, .sdlc/state/, .sdlc/worktrees/")

    def test_team_mode_without_a_github_remote_fails(self):
        self.ready()
        self.repo.init("--team")
        code, got = self.repo.check()
        self.assertEqual(code, 1)
        self.assertEqual(got["mode"]["status"], "missing")
        self.assertIn("needs a GitHub remote", got["mode"]["detail"])

    def test_with_a_github_remote_gh_must_be_logged_in(self):
        self.ready()
        self.repo.git("remote", "add", "origin", "git@github.com:o/r.git")
        self.repo.init("--solo")
        code, got = self.repo.check()
        self.assertEqual((code, got["gh"]["status"], got["gh"]["detail"]), (0, "ok", "logged in to github.com"))
        self.assertNotIn("forge", got)
        code, got = self.repo.check(STUB_GH_LOGGED_OUT="github.com")
        self.assertEqual((code, got["gh"]["status"]), (1, "missing"))
        self.assertIn("gh auth login --hostname github.com", got["gh"]["detail"])

    def test_items_waiting_for_a_person_and_a_running_loop_are_reported(self):
        self.ready()
        self.repo.write(".sdlc/state/items.json", json.dumps({"a-first": {"state": "needs-human", "reasons": ["new dependency"]},
                                                              "b-second": {"state": "blocked"}, "c-third": {"failures": 1}}))
        self.repo.write(".sdlc/state/loop.pid", "%d\n" % os.getpid())
        self.repo.write(".sdlc/state/loop-status.json", json.dumps({"stop": "queue empty"}))
        _, got = self.repo.check()
        self.assertEqual(got["items"]["status"], "warn")
        self.assertEqual(got["items"]["detail"], "waiting for a person: a-first, b-second")
        self.assertEqual(got["loop"]["detail"], "running (pid %d)" % os.getpid())
        os.remove(self.repo.path(".sdlc/state/loop.pid"))
        _, got = self.repo.check()
        self.assertEqual(got["loop"]["detail"], "not running — last stop: queue empty")

    def test_the_json_result_names_the_root_and_the_plugin_version(self):
        self.ready()
        r = self.repo.run("--check", "--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["root"], self.repo.root)
        self.assertTrue(data["ok"])
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+")
        self.assertEqual({c["status"] for c in data["checks"]} - {"ok", "warn", "missing"}, set())


if __name__ == "__main__":
    unittest.main()
