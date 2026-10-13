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
        self.assertEqual(len(I.OLD_LINES), 4)
        self.assertEqual(sum(1 for k in I.OLD_LINES if "/sdlc:lesson" in k), 2)
        for old, new in I.OLD_LINES.items():
            self.assertTrue(I.REMOVED_COMMANDS.search(old), old)
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
    w(".sdlc/LESSONS.md", "# Lessons\n- never edit src/gen\n")
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
        self.assertEqual(cfg["loop"], {"max_turns": 500, "max_failures_per_slug": 3, "self_check": True, "fix_rounds": 2,
                                       "pause_file": ".sdlc/state/pause", "item_note": "", "session_max_minutes": 120})
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
        self.repo.write("docs/LESSONS.md", "- a lesson\n")
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
