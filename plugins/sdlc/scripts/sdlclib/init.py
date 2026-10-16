"""`sdlc.py init` — adopt a repository, or move a 0.x one to 1.0. Never overwrites what the user wrote: an existing file
is SKIPPED, or MERGED (missing CLAUDE.md sections, missing .gitignore lines, missing permissions, empty config values, the
lines of a 0.x REVIEW.md that 1.0 changed — where nobody has edited them).

  init [--lang en|ko] [--solo|--team] [--commands build=..,test=..] [--dry-run] [--prune] [--json]
  init --check [--json]          a health report instead

From 0.x (config version 1): the config is rewritten as version 2 (the old file kept as .sdlc/config.v1.json); every
intent that is not done becomes a work doc; the intent/spec/plan history stays where it is. `--prune` removes what 0.x
generated and 1.0 no longer uses.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import CONFIG_REL, DEFAULTS, OBJECT_SECTIONS, Config, config_version, migrate_v1
from .util import now_iso, plugin_root, plugin_version, read_json, read_text, run, write_json
from .workdoc import RESERVED_FILES, RESERVED_SLUGS, SLUG_RE, WorkDoc, list_docs, problems, split_frontmatter

SECTIONS = ("commands", "conventions", "architecture", "mistakes", "verifying")
SECTION_TITLES = {"commands": "Commands", "conventions": "Conventions", "architecture": "Architecture",
                  "mistakes": "Things Claude gets wrong", "verifying": "Verifying your work"}
# a heading is a section init would add when its words say so: whole-heading words, Korean ones included — "commands" in
# the middle of a longer heading ("Where the commands run") is about something else. The distinctive phrases are matched
# anywhere in the heading, the single words only in a short one (see `heading_is_section`)
SECTION_WORDS = {"commands": ("commands", "command", "명령", "명령어"),
                 "conventions": ("conventions", "convention", "규칙", "규약", "컨벤션"),
                 "architecture": ("architecture", "구조", "아키텍처"),
                 "verifying": ("verification", "verifying", "검증")}
SECTION_PHRASES = {"mistakes": ("things claude gets wrong", "claude 가 반복하는 실수"), "verifying": ("verifying your work",)}
SECTION_HEADING = re.compile(r"^#{1,3}[ \t]+(.+?)[ \t]*#*[ \t]*$")
HEADING_NOISE = frozenset(("and", "the", "a", "of", "for", "및", "와", "과"))
FENCE = re.compile(r"^\s*(```|~~~)")
# lines 0.x wrote into CLAUDE.md that name commands 1.0 removed → their 1.0 wording
OLD_LINES = {
    "- (add a line here the second time the same mistake appears; `/sdlc:lesson` does this)":
        "- (add a line here the second time the same mistake appears)",
    "Every change starts from a committed plan.md (`/sdlc:go` writes and commits it; `/sdlc:plan` for the step-by-step path).":
        "Every change has a work doc (`work/<slug>.md`: goal, acceptance criteria, plan, proof); `/sdlc:go` writes it.",
    "- (같은 실수가 두 번째로 나타나면 여기에 한 줄을 추가한다. `/sdlc:lesson` 이 이 일을 한다)":
        "- (같은 실수가 두 번째로 나타나면 여기에 한 줄을 추가한다)",
    "모든 변경은 커밋된 plan.md 에서 시작한다(`/sdlc:go` 가 작성·커밋하고, 단계별로는 `/sdlc:plan`).":
        "모든 변경에는 작업 문서(`work/<slug>.md`: 목표·인수 조건·계획·증명)가 있다. `/sdlc:go` 가 작성한다.",
    # 0.4.1–0.5.1 (before the plan-first rule named /sdlc:go)
    "Nothing is implemented without an approved plan: start in plan mode, commit plan.md, then build.":
        "Every change has a work doc (`work/<slug>.md`: goal, acceptance criteria, plan, proof); `/sdlc:go` writes it.",
    "승인된 계획 없이는 아무것도 구현하지 않는다. 플랜 모드로 시작해 plan.md 를 커밋한 뒤 구현한다.":
        "모든 변경에는 작업 문서(`work/<slug>.md`: 목표·인수 조건·계획·증명)가 있다. `/sdlc:go` 가 작성한다.",
}
REMOVED_COMMANDS = re.compile(r"/sdlc:(?:intent|spec|plan|lesson|review|verify|doctor|release|monitor|triage|"
                              r"postmortem|metrics|evals|policy|scan|ship|loop)\b")
# what 0.x wrote into REVIEW.md that 1.0 changed, by language: the Compliance line (it named spec.md and plan.md) and the
# Approval section (1.0 asks for the review tally there)
OLD_COMPLIANCE = {
    "en": "- Compliance: the change matches spec.md, plan.md and our design principles; CLAUDE.md conventions are followed",
    "ko": "- Compliance(정합성): 변경이 spec.md·plan.md·우리 설계 원칙과 일치하는지, CLAUDE.md 규약을 따르는지"}
OLD_APPROVAL = {
    "en": ("## Approval",
           "Findings do not approve or block a PR on their own. Approval comes from a",
           "code owner through branch protection. The agent that wrote the code cannot approve it."),
    "ko": ("## 승인 (Approval)",
           "지적 사항만으로 PR 이 승인되거나 차단되지 않습니다. 승인은 브랜치 보호를 통해",
           "코드 오너가 합니다. 코드를 작성한 에이전트는 그 코드를 승인할 수 없습니다.")}
REVIEW_SECTIONS = ("New or pre-existing", "Tally")               # the 1.0 sections that take the Approval section's place
HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*$")
OLD_REVIEW_FILES = re.compile(r"\b(?:spec|plan)\.md\b")           # a REVIEW.md that still sends the reviewer to files 1.0 lacks
COMMAND_KEYS = ("build", "test", "lint", "run", "format", "lint_file")
GITIGNORE = (".sdlc/logs/", ".sdlc/state/", ".sdlc/worktrees/")
GITIGNORE_HEADER = "# sdlc plugin — engine state, logs and gate worktrees (local only)"
# what 0.x wrote above its .gitignore lines (`.sdlc/logs/`, `.sdlc/state/`, `<evals>/results/`); --prune brings the block to 1.x
GITIGNORE_HEADER_0X = "# sdlc plugin — local state and logs (audit copies go to OpenTelemetry / CI artifacts)"
GITIGNORE_LEFTOVER = ".gitignore: 0.x lines"
# 0.x: files it generated, local state files, the example eval cases it copied — removed by --prune
PRUNE_FILES = (".sdlc/bands.json", ".sdlc/APPROVALS.md", ".sdlc/managed-settings.example.json",
               ".sdlc/managed-settings.README.md", ".sdlc/state/loop-failures.txt", ".sdlc/state/loop-queue.txt",
               ".sdlc/state/active-plan")
PRUNE_DIRS = (".sdlc/examples", ".sdlc/state/loop-resume")
EXAMPLE_EVALS = ("fix-without-editing-test", "no-secret-in-code", "respect-frozen-path")
TEMPLATE_MARK = re.compile(r"^slug:\s*\{\{slug\}\}", re.M)       # 0.x copied its plugin templates to <dir>/TEMPLATE.md unrendered
# a project may have none of these: init's placeholder, and what 0.x's init skill wrote when it found no command
OPTIONAL_TODO = re.compile(r"TODO: (?:set the (?:build|lint|run) command|no (?:build|lint|run) command found|"
                           r"(?:build|lint|run) 명령을 설정한다)")
AGENTS_IMPORT = re.compile(r"(?<![\w@/])@AGENTS\.md(?![\w-]|\.\w)")
FRONT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:\s?")           # a front matter line, as workdoc.split_frontmatter reads it
# the first line of the 0.x evals README — the Korean one carries no marker
EVALS_TITLES = ("# evals/ — 에이전트 설정의 회귀 테스트", "# evals/ — regression tests for the agent's configuration")
FETCH_SECONDS = 10.0                                             # the longest init waits for the remote, as `status` does
LS_REMOTE_SECONDS = 8.0                                          # …and for `git ls-remote`, which only wants to know a branch
# what in a project's own files tells a session to work the 0.x way: it merges PRs itself, keeps the queue in intent/, reads
# spec.md and plan.md. (The removed /sdlc:<command> names are REMOVED_COMMANDS' business.) Each kind has the sentence that
# follows its file and line numbers in the NOTE and the --check row.
OLD_WORKFLOW_KINDS = (
    ("merge", re.compile(r"gh\s+pr\s+merge", re.I),
     "tells sessions to merge PRs themselves — 1.x merges only through the gate; remove or reword it"),
    ("intent", None, "points sessions at %(intent)s/ — 1.x keeps the queue in %(work)s/; remove or reword it"),
    ("files", re.compile(r"(?<![\w./-])(?:spec|plan)\.md\b", re.I),
     "sends sessions to spec.md / plan.md — 1.x has one work doc per change (%(work)s/<slug>.md); remove or reword it"))
# 0.x's .github/CODEOWNERS starter: its comment block (the fourth line had the placeholder filled in) and the paths it covered
CODEOWNERS_0X = (
    "# CODEOWNERS — the human approval gate (playbook Stage 5: separation of duties).",
    '# Enable branch protection on the default branch: "Require a pull request before merging" + "Require review from',
    '# Code Owners". Agents (sdlc-review workflow, claude-code-action, Claude Code Review) can review but cannot approve.')
CODEOWNERS_0X_REPLACE = re.compile(r"^# Replace the \S+ placeholders with GitHub teams or users \(e\.g\. @acme/platform\)\.$")
CODEOWNERS_0X_PATHS = ("*", "CLAUDE.md", "REVIEW.md", ".claude/", ".sdlc/", "evals/", "intent/", "spec/", "plan/",
                       ".github/workflows/")
# subdirectories that are never a project of their own, and the ones usually named for one
SUB_SKIP = frozenset(("node_modules", "vendor", "build", "dist", "target", "out", "venv", "env", "__pycache__", "pods",
                      "deriveddata", "coverage"))
SUB_FIRST = ("android", "ios", "app", "backend", "frontend")
# the first word after `pnpm` / `yarn` that is a package-manager command and not a script name
PKG_COMMANDS = frozenset((
    "install", "i", "add", "remove", "rm", "uninstall", "update", "upgrade", "up", "exec", "dlx", "create", "init", "publish",
    "pack", "link", "unlink", "audit", "outdated", "list", "ls", "why", "workspace", "workspaces", "config", "cache", "store",
    "rebuild", "import", "patch", "dedupe", "prune", "fetch", "env", "node", "bin", "root", "setup", "deploy", "licenses",
    "info", "global", "set", "tool", "plugin", "explain", "constraints", "login", "logout", "whoami", "version", "help"))
# flags that point a package-manager command at another package or directory: its scripts are not this package.json's
PKG_REDIRECTS = ("--prefix", "--filter", "--cwd", "--dir", "--workspace", "--workspaces", "--if-present", "-C", "-F", "-w", "-r",
                 "--recursive")
# the lines of the 0.x LESSONS.md templates (0.4.1–0.8.3, en and ko) outside their HTML comment
LESSONS_LINES = frozenset((
    "# Lessons", "Version-controlled post-mortem lessons. Future investigations read this file first.",
    "Each entry is written by the team that owned the incident and links to the eval that now guards against it.",
    "# Lessons (교훈)", "버전 관리되는 포스트모템 교훈 모음입니다. 앞으로의 조사는 이 파일을 먼저 읽습니다.",
    "각 항목은 인시던트를 담당한 팀이 작성하며, 이제 그 재발을 막고 있는 eval 을 링크합니다."))


def imports_agents(text: str) -> bool:
    """CLAUDE.md pulls AGENTS.md in with `@AGENTS.md` wherever it stands in the text — Claude Code ignores it inside a
    code block or code span."""
    text = re.sub(r"```.*?```|~~~.*?~~~", "", text or "", flags=re.S)
    return bool(AGENTS_IMPORT.search(re.sub(r"`[^`\n]*`", "", text)))


def undecodable(path: str) -> bool:
    """A file that is not UTF-8 (a legacy Korean encoding, say) is not ours to rewrite: reading it with replacement
    characters and writing it back would destroy its text."""
    try:
        with open(path, "rb") as fh:
            fh.read().decode("utf-8")
    except UnicodeDecodeError:
        return True
    except OSError:
        return False
    return False


def read_style(path: str) -> Tuple[Optional[str], str, str]:
    """(text, bom, newline) of a UTF-8 file; the text is None when there is no such file. `text` is what init edits (no BOM,
    `\\n` line ends) and `styled()` writes it back the way the file had it: a CRLF file does not turn into LF."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None, "", "\n"
    bom = "\ufeff" if raw.startswith("\ufeff") else ""
    raw = raw[len(bom):]
    crlf = raw.count("\r\n")
    return raw.replace("\r\n", "\n"), bom, "\r\n" if crlf * 2 > raw.count("\n") else "\n"


def styled(text: str, bom: str, newline: str) -> str:
    return bom + (text.replace("\n", newline) if newline != "\n" else text)


def same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def templates() -> str:
    return os.path.join(plugin_root(), "templates")


def tpl(lang: str, name: str) -> str:
    for l in (lang, "en"):
        text = read_text(os.path.join(templates(), l, name))
        if text is not None:
            return text
    return ""


def render(text: str, **vals: str) -> str:
    for k, v in vals.items():
        text = text.replace("{{%s}}" % k, v)
    return text


def parse_commands(spec: str) -> Dict[str, str]:
    """"build=make,test=make test" → {"build": "make", "test": "make test"}; a comma inside a command is kept."""
    keys = "|".join(COMMAND_KEYS)
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(?:^|,)\s*(%s)=(.*?)(?=,\s*(?:%s)=|$)" % (keys, keys), spec or "", re.S)}


# --------------------------------------------------------------------------------------------------------- detection
def detect_stack(base: str) -> Tuple[Dict[str, str], List[str], str]:
    """(commands, stack, package manager) of the project whose files are in `base`: the build/test/lint/run commands the files
    name. The formatter is left to the edit hook (`commands.format: auto` picks one per file type)."""
    def exists(*p: str) -> bool:
        return os.path.exists(os.path.join(base, *p))

    def read(*p: str) -> str:
        return read_text(os.path.join(base, *p)) or ""

    cmds: Dict[str, str] = {}
    stack: List[str] = []
    pm = ""

    def setc(k: str, v: str) -> None:
        if v and not cmds.get(k):
            cmds[k] = v

    if exists("package.json"):
        try:
            pkg = json.loads(read("package.json"))
        except ValueError:
            pkg = {}
        pkg = pkg if isinstance(pkg, dict) else {}
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        # a lock file says what the project installs with; without one, the `packageManager` field does
        pm = "pnpm" if exists("pnpm-lock.yaml") else "yarn" if exists("yarn.lock") else \
            "bun" if (exists("bun.lockb") or exists("bun.lock")) else \
            "npm" if (exists("package-lock.json") or exists("npm-shrinkwrap.json")) else ""
        if not pm:
            declared = str(pkg.get("packageManager") or "").split("@")[0].strip().lower()
            pm = declared if declared in ("pnpm", "yarn", "bun", "npm") else "npm"
        stack.append("node")
        for key, names in (("build", ("build",)), ("test", ("test",)), ("lint", ("lint",)), ("run", ("start", "dev", "serve"))):
            for n in names:
                if n in scripts:
                    if key == "test" and "no test specified" in str(scripts[n]):
                        break                                   # what `npm init` writes: it fails on purpose
                    setc(key, "%s run %s" % (pm, n))
                    break
    mk = read("Makefile") or read("makefile") or read("GNUmakefile")
    if mk:
        targets = set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", mk, re.M))
        stack.append("make")
        for key, names in (("build", ("build",)), ("test", ("test", "check")), ("lint", ("lint",)), ("run", ("run", "serve", "dev"))):
            for n in names:
                if n in targets:
                    setc(key, "make " + n)
                    break
    if exists("pom.xml"):
        mvn = "./mvnw" if exists("mvnw") else "mvn"
        stack.append("java-maven")
        setc("build", mvn + " -q -DskipTests package")
        setc("test", mvn + " -q test")
        if "spring-boot-maven-plugin" in read("pom.xml"):
            setc("run", mvn + " spring-boot:run")
    if exists("build.gradle") or exists("build.gradle.kts"):
        gw = "./gradlew" if exists("gradlew") else "gradle"
        stack.append("java-gradle")
        setc("build", gw + " build -x test")
        setc("test", gw + " test")
        if "org.springframework.boot" in (read("build.gradle") or read("build.gradle.kts")):
            setc("run", gw + " bootRun")
    if exists("Cargo.toml"):
        stack.append("rust")
        for k, v in (("build", "cargo build"), ("test", "cargo test"), ("lint", "cargo clippy -- -D warnings"), ("run", "cargo run")):
            setc(k, v)
    if exists("go.mod"):
        stack.append("go")
        for k, v in (("build", "go build ./..."), ("test", "go test ./..."), ("lint", "go vet ./...")):
            setc(k, v)
        if exists("main.go"):
            setc("run", "go run .")
    if exists("Package.swift"):
        stack.append("swift")
        setc("build", "swift build")
        setc("test", "swift test")
    if exists("pubspec.yaml"):
        stack.append("dart")
        flutter = "flutter:" in read("pubspec.yaml")
        setc("test", "flutter test" if flutter else "dart test")
        setc("lint", "flutter analyze" if flutter else "dart analyze")
    if any(exists(f) for f in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg")):
        stack.append("python")

        def py(tool: str) -> str:
            """How the project runs `tool`: through its environment manager, from its .venv, or from the PATH."""
            if exists("uv.lock"):
                return "uv run " + tool
            if exists("poetry.lock"):
                return "poetry run " + tool
            return ".venv/bin/" + tool if os.path.isfile(os.path.join(base, ".venv", "bin", tool)) else tool

        setc("test", py("pytest"))
        if "[tool.ruff" in read("pyproject.toml") or exists("ruff.toml") or exists(".ruff.toml"):
            setc("lint", py("ruff") + " check .")
        elif exists(".flake8") or "[flake8]" in read("setup.cfg"):
            setc("lint", py("flake8"))
    if exists("Gemfile"):
        stack.append("ruby")
        gem = read("Gemfile")
        setc("test", "bundle exec rspec" if ("rspec" in gem or exists("spec")) else "bundle exec rake test")
        if "rubocop" in gem:
            setc("lint", "bundle exec rubocop")
    if exists("composer.json"):
        stack.append("php")
        try:
            comp = json.loads(read("composer.json"))
        except ValueError:
            comp = {}
        sc = comp.get("scripts") if isinstance(comp, dict) and isinstance(comp.get("scripts"), dict) else {}
        for k in ("test", "lint", "build"):
            if k in sc:
                setc(k, "composer " + k)
    return cmds, stack, pm


def skip_remote() -> bool:
    """`SDLC_SKIP_GH_DETECT` (the tests, an offline machine) keeps init from asking the network anything."""
    return bool(os.environ.get("SDLC_SKIP_GH_DETECT"))


def ls_remote(root: str, args: List[str], timeout: float = LS_REMOTE_SECONDS) -> Optional[str]:
    """`ls-remote <args>` against the repository's origin: its output, or None when it failed or took longer than `timeout`. It
    never asks for a password, and the whole process group (ssh included) goes with it when it is late."""
    try:
        p = subprocess.Popen(["git", "-C", root, "ls-remote"] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True, universal_newlines=True, errors="replace",
                             env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except OSError:
        return None
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        p.communicate()
        return None
    return out if p.returncode == 0 else None


def remote_default_branch(root: str, ask: bool = True) -> str:
    """The branch origin calls its default ("" when it cannot be told): what the remote answers to `ls-remote --symref origin
    HEAD` (unless `ask` is off or the network is skipped), else the checkout's own `refs/remotes/origin/HEAD`."""
    if ask and not skip_remote():
        m = re.search(r"^ref:\s*refs/heads/(\S+)\s+HEAD\s*$", ls_remote(root, ["--symref", "origin", "HEAD"]) or "", re.M)
        if m:
            return m.group(1)
    r = run(["git", "-C", root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=10)
    head = r.out.strip() if r.ok else ""
    return head[len("origin/"):] if head.startswith("origin/") else ""


def xcode_project(base: str) -> str:
    """The name of the Xcode workspace or project in `base` — when there is one and no Package.swift to build it with."""
    if os.path.exists(os.path.join(base, "Package.swift")):
        return ""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ""
    for ext in (".xcworkspace", ".xcodeproj"):
        for n in names:
            if n.endswith(ext) and os.path.isdir(os.path.join(base, n)) and n != "Pods.xcodeproj":
                return n
    return ""


def sub_projects(root: str) -> List[Dict[str, Any]]:
    """The directories one level down that look like a project of their own — what a repository with an `android/` and an
    `ios/` next to each other has. [{"dir", "stack", "commands", "xcode"}], the usual names first."""
    try:
        names = sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)) and not n.startswith(".")
                       and n.lower() not in SUB_SKIP)
    except OSError:
        return []
    found = []
    for n in [x for x in SUB_FIRST if x in names] + [x for x in names if x not in SUB_FIRST]:
        cmds, stack, _ = detect_stack(os.path.join(root, n))
        xcode = xcode_project(os.path.join(root, n))
        if cmds or xcode:
            found.append({"dir": n, "stack": stack, "commands": cmds, "xcode": xcode})
    return found


def suggest_subproject(project: Dict[str, Any]) -> str:
    """One line for a project one level down: what to pass to `--commands` to use it (nothing is filled in on its own)."""
    d, cmds, xcode = project["dir"], project["commands"], project["xcode"]
    prefix = "cd %s && " % shlex.quote(d)
    if cmds:
        flag = ",".join("%s=%s%s" % (k, prefix, cmds[k]) for k in ("build", "test", "lint") if cmds.get(k))
        return ("nothing detected at the root, but %s/ looks like a project (%s) — to use it: init --commands \"%s\"" %
                (d, ", ".join(project["stack"]) or "build files", flag))
    scheme = os.path.splitext(xcode)[0]
    here = "the repository root" if d == "." else d + "/"
    flag = "test=%sxcodebuild test -scheme %s -destination 'platform=iOS Simulator,name=iPhone 16'" % \
        ("" if d == "." else prefix, shlex.quote(scheme))
    return ("nothing detected%s, but %s holds %s — to use it (adjust the scheme and destination): "
            "init --commands \"%s\"" % ("" if d == "." else " at the root", here, xcode, flag))


def detect(root: str) -> Dict[str, Any]:
    """Stack, package manager and the build/test/lint/run commands, from the files in the repository, and what the repository
    says about its remote and its default branch (as origin names it, else a guess)."""
    cmds, stack, pm = detect_stack(root)

    def g(*a: str) -> str:
        r = run(["git", "-C", root] + list(a), timeout=10)
        return r.out.strip() if r.ok else ""
    git = {"is_repo": g("rev-parse", "--is-inside-work-tree") == "true", "remote": "", "host": "", "repo": "",
           "default_branch": "main"}
    if git["is_repo"]:
        git["remote"] = g("remote", "get-url", "origin")        # what the engine looks at: no origin, no GitHub
        m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", git["remote"])
        if m:
            git["host"], git["repo"] = m.group(1), m.group(2)
        remote_head = remote_default_branch(root) if git["remote"] else ""   # the remote's own answer first: no guessing main
        local = g("for-each-ref", "--format=%(refname:short)", "refs/heads/").split()
        cur = g("symbolic-ref", "--short", "HEAD")
        if remote_head:                                         # a feature branch may be checked out: the remote says
            git["default_branch"] = remote_head
        else:
            git["default_branch"] = next((b for b in ("main", "master", "trunk", "develop") if b in local),
                                         cur if cur and not cur.startswith("sdlc/") else "main")
    return {"commands": cmds, "stack": stack, "pm": pm, "git": git}


def gh_login(root: str, host: str) -> str:
    """"yes" when gh is logged in to `host` and the host answers, "offline" when gh holds a login for it and the host does not
    answer (`auth status` asks the host, `auth token` only reads the stored login), "no" otherwise."""
    if run(["gh", "auth", "status", "--hostname", host], cwd=root, timeout=30).ok:
        return "yes"
    token = run(["gh", "auth", "token", "--hostname", host], cwd=root, timeout=30)    # its output is never kept or shown
    return "offline" if token.ok and token.out.strip() else "no"


def detect_forge(root: str, git: Dict[str, Any], need_mode: bool = True) -> Tuple[str, str, str]:
    """(forge, mode, why). No remote → merges on this machine, solo. GitHub: one collaborator → solo; more, or unknown
    → team (people approve designs and merge). Another host → merges on this machine, solo. `need_mode` off (the mode comes
    from a configuration or from the command line): the collaborators are not asked."""
    host = git["host"]
    if not git["remote"]:
        return "local", "solo", "no remote — the loop merges on this machine"
    if not host:
        return "local", "solo", "the origin remote is a local path, not GitHub — the loop merges on this machine; " \
                                "push the default branch yourself"
    skip = skip_remote()
    github = "github" in host.lower() or (not skip and gh_login(root, host) != "no")
    if not github:
        return "local", "solo", "%s is not GitHub — the loop merges on this machine; push the default branch yourself" % host
    if skip:
        return "github", "team", "collaborators not checked — pass --solo for a one-person repository"
    if not need_mode:
        return "github", "team", "the mode comes from the configuration"
    r = run(["gh", "api", "--hostname", host, "repos/%s/collaborators?per_page=100" % git["repo"], "--jq", "length"],
            cwd=root, timeout=30)
    n = r.out.strip()
    if r.ok and n.isdigit():
        return ("github", "solo", "one collaborator") if int(n) <= 1 else ("github", "team", "%s collaborators" % n)
    return "github", "team", "collaborators unknown — pass --solo for a one-person repository"


# ------------------------------------------------------------------------------------------------------------ output
class Out:
    def __init__(self, dry: bool):
        self.dry = dry
        self.rows: List[Tuple[str, str, str]] = []

    def add(self, verb: str, rel: str, note: str = "") -> None:
        if self.dry and verb in ("CREATED", "MERGED", "MIGRATED", "REMOVED"):
            verb = "WOULD " + verb[:-1]
        self.rows.append((verb, rel, note))

    def write(self, root: str, rel: str, text: str) -> None:
        if not self.dry:
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as fh:      # no newline translation: LF stays LF, CRLF stays CRLF
                fh.write(text)

    def json(self, root: str, rel: str, data: Any) -> None:
        if not self.dry:
            path = os.path.join(root, rel)
            if os.path.islink(path) and os.path.exists(path):
                path = os.path.realpath(path)      # a linked file (shared settings) stays a link: write what it points at
            mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else None
            write_json(path, data)                 # …goes through a 0600 temp file: give back the mode the file had
            if mode is None:
                mask = os.umask(0)
                os.umask(mask)
                mode = 0o666 & ~mask               # a new file: what any other file written here gets
            os.chmod(path, mode)

    def text(self) -> str:
        return "\n".join(("%-17s %s  (%s)" % (v, r, n)) if n else ("%-17s %s" % (v, r)) for v, r, n in self.rows)


def put(root: str, rel: str, text: str, out: Out, note: str = "") -> None:
    if os.path.exists(os.path.join(root, rel)):
        out.add("SKIPPED", rel, "exists")
    else:
        out.write(root, rel, text)
        out.add("CREATED", rel, note)


# ------------------------------------------------------------------------------------------------------------- files
def heading_words(part: str) -> List[str]:
    """The words of a heading, as the section match reads them: lower case, without markup, a leading number, emoji or the
    little words between."""
    part = re.sub(r"[`*_~]", "", part.lower())
    part = re.sub(r"^\s*(?:\d+(?:\.\d+)*[.)]?|[ivx]+[.)])\s+", "", part)
    return [w for w in re.findall(r"[^\W_]+", part) if w not in HEADING_NOISE]


def heading_is_section(title: str, sid: str) -> bool:
    """Is a heading titled `title` the user's own section for `sid`? Whole headings count: a short one whose first or last word is
    the section's word ("Commands", "Common commands", "명령어", "코드 컨벤션"), a title and its parenthetical translation
    ("Commands (명령어)") either way round — and a distinctive phrase anywhere in it. "Where the commands run" is not a Commands
    section, and neither is a heading that merely contains "구조화"."""
    parenthetical = re.findall(r"\(([^)]*)\)", title)
    for part in [re.sub(r"\([^)]*\)", " ", title)] + parenthetical:
        words = heading_words(part)
        if not words:
            continue
        if any(phrase in " ".join(words) for phrase in SECTION_PHRASES.get(sid, ())):
            return True
        syn = SECTION_WORDS.get(sid, ())
        if len(words) <= 4 and (words[0] in syn or words[-1] in syn):
            return True
    return False


def find_section(text: str, sid: str) -> Tuple[str, str]:
    """("marker", "") when init's own section is there; ("heading", `## the line`) for the first heading of the user's that is
    that section (headings in a code block are no headings); ("", "") when the text has none."""
    if "<!-- sdlc:begin %s -->" % sid in text:
        return "marker", ""
    fenced = False
    for line in text.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            m = SECTION_HEADING.match(line)
            if m and heading_is_section(m.group(1), sid):
                return "heading", line.strip()
    return "", ""


def section_present(text: str, sid: str) -> bool:
    return bool(find_section(text, sid)[0])


def own_title(heading: str, sid: str) -> bool:
    """Is `## heading` titled as init titles that section ("Commands", or the Korean template's "Commands (명령어)")?"""
    m = SECTION_HEADING.match(heading)
    return bool(m) and " ".join(heading_words(re.sub(r"\([^)]*\)", " ", m.group(1)))) == SECTION_TITLES[sid].lower()


def sections_block(lang: str, cmds: Dict[str, str], work: str = "work") -> str:
    ko = lang == "ko"

    def todo(k: str) -> str:
        return cmds.get(k) or ("TODO: %s 명령을 설정한다" % k if ko else "TODO: set the %s command" % k)
    return render(tpl(lang, "CLAUDE.sections.md"), build_cmd=todo("build"), test_cmd=todo("test"), lint_cmd=todo("lint"),
                  run_cmd=todo("run"), work_dir=work,
                  conventions="TODO: 이름 짓기, 오류 처리, 테스트, 의존성 규약 (/sdlc:init 중에 Claude 가 채운다)" if ko else
                  "TODO: naming, error handling, testing and dependency conventions (Claude fills this in during /sdlc:init)",
                  architecture="TODO: 주요 구성요소, 경계, 데이터 흐름 (/sdlc:init 중에 Claude 가 채운다)" if ko else
                  "TODO: main components, boundaries and data flow (Claude fills this in during /sdlc:init)")


def claude_md(root: str, lang: str, cmds: Dict[str, str], out: Out, work: str = "work",
              init_notes: Optional[List[str]] = None) -> str:
    """CLAUDE.md: written when there is none, else the sections it lacks are appended. Returns the text it has now (what a
    dry run would leave), "" for a file that is not UTF-8. `init_notes` collects what init says on top of the row."""
    block = sections_block(lang, cmds, work)
    extra = init_notes if init_notes is not None else []
    path, agents_path = os.path.join(root, "CLAUDE.md"), os.path.join(root, "AGENTS.md")
    agents = os.path.exists(agents_path) and not same_file(path, agents_path)    # one file under two names imports nothing
    if undecodable(path):
        out.add("SKIPPED", "CLAUDE.md", "not UTF-8 text — left alone")
        return ""
    cur, bom, nl = read_style(path)
    if cur is None:
        created = "# %s\n\n%s%s" % (os.path.basename(os.path.abspath(root)), "@AGENTS.md\n\n" if agents else "", block)
        out.write(root, "CLAUDE.md", created)
        out.add("CREATED", "CLAUDE.md", "5 sections" + (", imports AGENTS.md" if agents else ""))
        return created
    text, notes = cur, []
    replaced = [old for old in OLD_LINES if old in text]
    for old in replaced:
        text = text.replace(old, OLD_LINES[old].replace("work/<slug>", work + "/<slug>"))
    if replaced:
        notes.append("%d line(s) naming removed commands updated" % len(replaced))
    if agents and not imports_agents(text):
        lines = text.split("\n")
        if lines and lines[0].startswith("# "):
            lines[1:1] = ["", "@AGENTS.md"]
        else:
            lines[0:0] = ["@AGENTS.md", ""]
        text = "\n".join(lines)
        notes.append("imports AGENTS.md")
    missing = []
    for s in SECTIONS:
        kind, heading = find_section(text, s)
        if not kind:
            missing.append(s)
        elif heading and not own_title(heading, s):          # the user's own section stands in: init adds no second one
            extra.append("kept your `%s`; sdlc's %s section was not added" % (heading, SECTION_TITLES[s]))
    if missing:
        chunks = []
        for s in missing:
            m = re.search(r"<!-- sdlc:begin %s -->.*?<!-- sdlc:end %s -->" % (s, s), block, re.S)
            if m:
                chunks.append(m.group(0))
        text = text.rstrip("\n") + "\n\n" + "\n\n".join(chunks) + "\n"
        notes.append("added: " + ", ".join(SECTION_TITLES[s] for s in missing))
    stale = sorted(set(REMOVED_COMMANDS.findall(text)))
    if stale:
        notes.append("still mentions removed commands: " + ", ".join(stale))
    if text != cur:
        out.write(root, "CLAUDE.md", styled(text, bom, nl))
        out.add("MERGED", "CLAUDE.md", "; ".join(notes))
    else:
        out.add("SKIPPED", "CLAUDE.md", "; ".join(notes) or "all 5 sections present")
    return text


def is_heading(line: str, key: str) -> bool:
    """`line` is a heading titled `key` — in a Korean file `<title> (key)`; case does not matter."""
    m = HEADING.match(line)
    title = m.group(1).lower() if m else ""
    return title == key.lower() or title.endswith("(%s)" % key.lower())


def section_end(lines: List[str], start: int) -> int:
    """Where the section whose heading is lines[start] ends: before the next heading, without the blank lines in front of it."""
    end = start + 1
    while end < len(lines) and not HEADING.match(lines[end]):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def migrate_review(text: str) -> Tuple[str, List[str]]:
    """A REVIEW.md as 0.x wrote it, brought to 1.0 where it is still as 0.x wrote it: the Compliance line names the work doc
    instead of spec.md and plan.md, and the Approval section gives way to the 1.0 sections that carry the review tally
    rule (each left out where the file has that heading already). The new text is the template's, read from there. A line
    or section the team has edited is no longer the 0.x one and stays as it is. Returns (the text, what was done)."""
    lines = text.split("\n")
    compliance = approval = False
    for lang in ("en", "ko"):
        new = (read_text(os.path.join(templates(), lang, "REVIEW.md")) or "").split("\n")
        if not any(new):
            continue
        line = next((l for l in new if l.startswith("- Compliance")), "")
        if line and OLD_COMPLIANCE[lang] in lines:
            lines = [line if l == OLD_COMPLIANCE[lang] else l for l in lines]
            compliance = True
        heading, body = OLD_APPROVAL[lang][0], list(OLD_APPROVAL[lang][1:])
        i = 0
        while i < len(lines):
            if lines[i] != heading or lines[i + 1:section_end(lines, i)] != body:
                i += 1
                continue
            end = section_end(lines, i)
            replacement: List[str] = []
            for key in REVIEW_SECTIONS:
                at = next((n for n, l in enumerate(new) if is_heading(l, key)), None)
                if at is not None and not any(is_heading(l, key) for l in lines[:i] + lines[end:]):
                    replacement += ([""] if replacement else []) + new[at:section_end(new, at)]
            if not replacement and i and not lines[i - 1] and (end >= len(lines) or not lines[end]):
                i -= 1                                      # nothing takes its place: the blank line before it goes too
            lines[i:end] = replacement
            i += len(replacement)
            approval = True
    notes = (["the 0.x compliance line now names the work doc"] if compliance else []) + \
        (["the 0.x approval section replaced by the review tally rule"] if approval else [])
    return "\n".join(lines), notes


def review_md(root: str, rel: str, lang: str, generated: str, out: Out) -> None:
    """REVIEW.md: written when there is none; one 0.x wrote, and nobody has edited, is brought to 1.0 (`migrate_review`)."""
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        put(root, rel, render(tpl(lang, "REVIEW.md"), generated_paths=generated), out)
        return
    if undecodable(path):
        out.add("SKIPPED", rel, "not UTF-8 text — left alone")
        return
    cur, bom, nl = read_style(path)
    cur = cur or ""
    text, notes = migrate_review(cur)
    if text != cur:
        out.write(root, rel, styled(text, bom, nl))
        out.add("MERGED", rel, "; ".join(notes))
    else:
        out.add("SKIPPED", rel, "exists")


def gitignore(root: str, out: Out) -> None:
    if undecodable(os.path.join(root, ".gitignore")):
        out.add("SKIPPED", ".gitignore", "not UTF-8 text — left alone")
        return
    cur, bom, nl = read_style(os.path.join(root, ".gitignore"))
    header = GITIGNORE_HEADER
    if cur is None:
        out.write(root, ".gitignore", "\n".join((header,) + GITIGNORE) + "\n")
        out.add("CREATED", ".gitignore")
        return
    have = set(l.strip() for l in cur.splitlines())
    add = [l for l in GITIGNORE if l not in have and l.rstrip("/") not in have]
    if not add:
        out.add("SKIPPED", ".gitignore", "all lines present")
        return
    out.write(root, ".gitignore", styled(cur.rstrip("\n") + ("\n\n" if cur.strip() else "") + "\n".join([header] + add) + "\n",
                                         bom, nl))
    out.add("MERGED", ".gitignore", "+%d lines" % len(add))


def split_compound(cmd: str) -> List[str]:
    """The simple commands of `a && b | c; d` — cut at `&&`, `||`, `|` and `;` that stand outside quotes."""
    parts: List[str] = []
    cur: List[str] = []
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                i += 1
                cur.append(cmd[i])
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            cur.append(ch)
            i += 1
            cur.append(cmd[i])
        elif ch in ";|" or (ch == "&" and cmd[i + 1:i + 2] == "&"):
            parts.append("".join(cur))
            cur = []
            i += 1 if ch == "&" else 0
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def permissions(cmd: str) -> List[str]:
    """The allow entries a command needs: one for each simple command in it — `cd android && ./gradlew test` is two, and
    a rule for `cd android` alone would still stop the second."""
    out: List[str] = []
    for part in split_compound(cmd):
        entry = "Bash(%s*)" % part
        if entry not in out:
            out.append(entry)
    return out


def settings(root: str, cmds: Dict[str, str], out: Out) -> None:
    rel = os.path.join(".claude", "settings.json")
    ours = read_json(os.path.join(templates(), "config", "settings.project.json")) or {}
    ours = ours.get("permissions") or {}
    fill = {"{{%s_permission}}" % k: permissions(cmds.get(k, "")) for k in ("build", "test", "lint")}
    allow: List[str] = []
    for x in ours.get("allow") or []:
        for y in fill.get(x, [x]):
            if y and "{{" not in y and y not in allow:
                allow.append(y)
    deny = list(ours.get("deny") or [])
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        out.json(root, rel, {"permissions": {"allow": allow, "deny": deny}})
        out.add("CREATED", rel, "permissions for git and the project's commands")
        return
    cur = read_json(path)
    if not isinstance(cur, dict) or not isinstance(cur.get("permissions", {}), dict):
        out.add("SKIPPED", rel, "not a JSON object with a permissions object — nothing changed")
        return
    perm = cur.setdefault("permissions", {})
    added = []
    for key, want in (("allow", allow), ("deny", deny)):
        have = perm.get(key) if isinstance(perm.get(key), list) else []
        new = [x for x in want if x not in have]
        if new:
            perm[key] = have + new
            added.append("%s+%d" % (key, len(new)))
    if added:
        out.json(root, rel, cur)
        out.add("MERGED", rel, " ".join(added))
    else:
        out.add("SKIPPED", rel, "permissions present")


def foreign_work_dir(root: str, rel: str = "work") -> bool:
    """work/ already used for something else: entries that are neither our README nor work docs."""
    d = os.path.join(root, rel)
    if not os.path.isdir(d):
        return False
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if n.startswith(".") or (n == "README.md" and "<!-- sdlc:work -->" in (read_text(p) or "")):
            continue
        if not (n.endswith(".md") and os.path.isfile(p)):
            return True
        meta, _ = split_frontmatter(read_text(p) or "")
        if "status" not in meta:
            return True
    return False


# --------------------------------------------------------------------------------------------------------- migration
def old_paths(root: str, raw: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """The 0.x artifact folders: from the version-1 config, or from the copy init kept of it."""
    src = raw if isinstance(raw, dict) and config_version(raw) < 2 else read_json(os.path.join(root, ".sdlc", "config.v1.json"))
    p = src.get("paths") if isinstance(src, dict) else None
    p = p if isinstance(p, dict) else {}
    return {k: str(p.get(k) or d).strip("/") for k, d in (("intent", "intent"), ("spec", "spec"), ("plan", "plan"),
                                                           ("evals", "evals"), ("lessons", ".sdlc/LESSONS.md"))}


def old_frontmatter(text: str, rel: str, notes: List[str]) -> Tuple[Dict[str, Any], str]:
    """`split_frontmatter` for a 0.x file. 0.x read a key with a line grep (`^status:`), so a block that never closes —
    `---`, the keys, then straight into the heading — still had its status: read the `key: value` lines right after the
    first `---`, up to the first other line, and say so."""
    meta, body = split_frontmatter(text)
    if not text.startswith("---") or body != text:             # none, or one that closes
        return meta, body
    lines = text.split("\n")
    keys: List[str] = []
    for line in lines[1:]:
        if not FRONT_KEY.match(line):
            break
        keys.append(line)
    if not keys:
        return meta, body
    note = "%s: the front matter has no closing `---` — read it up to the first other line, as 0.x did" % rel
    if note not in notes:
        notes.append(note)
    return split_frontmatter("---\n" + "\n".join(keys) + "\n---\n")[0], "\n".join(lines[1 + len(keys):])


def old_section(doc: WorkDoc, name: str) -> str:
    """`doc.section(name)`, also where a 0.x Korean template carries both languages in the heading: `## 문제 (Problem)`."""
    want = name.strip().lower()
    for title, body in doc.sections:
        t = title.strip().lower()
        if t == want or t.endswith("(%s)" % want):
            return body.strip("\n")
    return ""


def artifact(root: str, rel: str, branch: str, notes: List[str]) -> Tuple[Dict[str, Any], str, str]:
    """(frontmatter, body, ref): the default branch's copy (the working tree) first, else the 0.x item branch's."""
    text = read_text(os.path.join(root, rel))
    if text is not None:
        meta, body = old_frontmatter(text, rel, notes)
        return meta, body, ""
    for ref in (branch, "origin/" + branch):
        r = run(["git", "-C", root, "show", "%s:%s" % (ref, rel)], timeout=20)
        if r.ok:
            meta, body = old_frontmatter(r.out, rel, notes)
            return meta, body, ref
    return {}, "", ""


def clean(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    return "\n".join(l for l in text.splitlines() if not re.match(r"^\s*-\s*R\d+\.\s*$", l)).strip()


def migrate_intents(root: str, paths: Dict[str, str], work_rel: str, out: Out, notes: List[str]) -> List[str]:
    """0.x intents that are not done → work docs: approved → ready, or planned when an approved plan exists (on the
    default branch or on sdlc/<slug>); draft → proposed. Rejected, superseded and merged ones stay history — and a `depends_on`
    naming one of them, or an intent that does not exist, is dropped (a NOTE each): it will have no work doc, and the queue
    would wait on it for good."""
    made: List[str] = []
    try:
        names = sorted(n for n in os.listdir(os.path.join(root, paths["intent"]))
                       if n.endswith(".md") and n not in ("README.md", "TEMPLATE.md"))
    except OSError:
        return made
    open_items, finished, statuses = [], set(), {}
    for n in names:
        slug = n[:-3]
        meta, body = old_frontmatter(read_text(os.path.join(root, paths["intent"], n)) or "",
                                     "%s/%s" % (paths["intent"], n), notes)
        status = str(meta.get("status", ""))
        statuses[slug] = status
        if status not in ("approved", "draft") or not SLUG_RE.match(slug):
            continue
        if slug in RESERVED_SLUGS:
            notes.append("%s/%s is not migrated: as a work doc it would be %s/%s on a case-insensitive file system — start it "
                         "again under another name" % (paths["intent"], n, work_rel, RESERVED_FILES[slug]))
            continue
        pmeta, pbody, pref = artifact(root, "%s/%s" % (paths["plan"], n), "sdlc/" + slug, notes)
        if str(pmeta.get("status", "")) == "implemented" and not pref:
            finished.add(slug)                                  # merged in 0.x
        else:
            open_items.append((n, slug, meta, body, status, pmeta, pbody, pref))
    will_exist = {item[1] for item in open_items} | {d.slug for d in list_docs(os.path.join(root, work_rel))}
    for n, slug, meta, body, status, pmeta, pbody, pref in open_items:
        rel = "%s/%s" % (work_rel, n)
        if os.path.exists(os.path.join(root, rel)):
            out.add("SKIPPED", rel, "already a work doc")
            continue
        _, sbody, _ = artifact(root, "%s/%s" % (paths["spec"], n), "sdlc/" + slug, notes)
        intent = WorkDoc.parse("---\n---\n" + body)
        goal = "\n\n".join(x for x in (clean(old_section(intent, sec)) for sec in ("Problem", "Proposed outcome")) if x)
        planned = status == "approved" and str(pmeta.get("status", "")) in ("approved", "implemented")
        deps = [str(d) for d in meta.get("depends_on")] if isinstance(meta.get("depends_on"), list) else []
        dropped = [d for d in deps if d in finished]
        kept = [d for d in deps if d in will_exist]
        for d in deps:                                            # neither a work doc to come nor a merged one: nothing will satisfy it
            if d not in will_exist and d not in finished:
                notes.append("%s: depends_on %s dropped — %s, so the item would wait for it for good" % (
                    rel, d, ("intent %s is %s" % (d, statuses[d] or "not a work item")) if d in statuses else
                    "no intent or work doc has that slug"))
        doc = WorkDoc({"slug": slug, "title": str(meta.get("title") or slug),
                       "status": "proposed" if status == "draft" else "planned" if planned else "ready",
                       "depends_on": kept,
                       "test_changes": "allowed" if str(pmeta.get("test_changes", "")) == "allowed" else "none",
                       "created": str(meta.get("created") or now_iso()), "source": "migrated:intent"}, "")
        doc.set_section("Goal", goal or "See `%s/%s`." % (paths["intent"], n))
        acceptance = clean(old_section(WorkDoc.parse("---\n---\n" + sbody), "Requirements"))
        if acceptance:
            doc.set_section("Acceptance criteria", acceptance)
        if planned:
            doc.set_section("Plan", "The approved 0.x plan: `%s/%s`%s." % (paths["plan"], n,
                                                                            " on branch `%s`" % pref if pref else ""))
            proof = clean(old_section(WorkDoc.parse("---\n---\n" + pbody), "Proof"))
            if proof:
                doc.set_section("Proof", proof)
        out.write(root, rel, doc.dumps())
        out.add("MIGRATED", rel, "from %s/%s, %s%s" % (paths["intent"], n, doc.status, ("; depends_on %s dropped (merged in 0.x)" %
                                                                                        ", ".join(dropped)) if dropped else ""))
        made.append(slug)
    return made


def leftovers(root: str, paths: Dict[str, str]) -> List[str]:
    """What 0.x generated that 1.0 does not use — plugin-made files only, never the intent/spec/plan history."""
    def untouched(rel: str) -> bool:                      # a directory 0.x created and nobody has put anything into
        d = os.path.join(root, rel)
        return os.path.isdir(d) and all(n == ".gitkeep" for n in os.listdir(d))

    found = [p for p in PRUNE_FILES if os.path.isfile(os.path.join(root, p))]
    found += [p + "/" for p in PRUNE_DIRS if os.path.isdir(os.path.join(root, p))]
    if untouched(".sdlc/history"):                       # with <metric>.jsonl files in it, it holds observations: keep
        found.append(".sdlc/history/")
    for k in ("intent", "spec", "plan"):                 # an organization's own template (no unrendered slug) stays
        rel = "%s/TEMPLATE.md" % paths[k]
        if TEMPLATE_MARK.search(read_text(os.path.join(root, rel)) or ""):
            found.append(rel)
    if untouched(paths["intent"] + "/triage"):
        found.append(paths["intent"] + "/triage/")
    sd = os.path.join(root, ".sdlc", "state")
    if os.path.isdir(sd):
        found += [".sdlc/state/" + n for n in sorted(os.listdir(sd)) if re.match(r"^checks-.*\.txt$", n)]
    wf = os.path.join(root, ".github", "workflows")
    if os.path.isdir(wf):
        for n in sorted(os.listdir(wf)):
            if n.startswith("sdlc-") and n.endswith((".yml", ".yaml")) and \
                    (read_text(os.path.join(wf, n)) or "").startswith("# sdlc"):
                found.append(".github/workflows/" + n)
    ev = paths["evals"]
    if evals_readme_0x(root, ev):
        found.append(ev + "/README.md")
        found += ["%s/cases/%s/" % (ev, c) for c in EXAMPLE_EVALS if os.path.isdir(os.path.join(root, ev, "cases", c))]
    lessons = read_text(os.path.join(root, paths["lessons"]))
    if lessons is not None and not has_lessons(lessons):  # the template, with no entry written under it
        found.append(paths["lessons"])
    if codeowners_0x(root):
        found.append(".github/CODEOWNERS")
    if gitignore_block_0x(root):
        found.append(GITIGNORE_LEFTOVER)
    return [f for f in found if f == GITIGNORE_LEFTOVER or inside(root, f)]


def evals_readme_0x(root: str, ev: str) -> bool:
    """Is `<ev>/README.md` the one 0.x's init wrote (its marker, or — in Korean — its title as the first line)?"""
    readme = (read_text(os.path.join(root, ev, "README.md")) or "").lstrip("\ufeff")
    title = next((l.strip() for l in readme.splitlines() if l.strip()), "")
    return "<!-- sdlc:evals -->" in readme or title in EVALS_TITLES


def codeowners_0x(root: str) -> bool:
    """Is `.github/CODEOWNERS` the starter 0.x wrote — its three comment lines and the line naming who fills the placeholders,
    then rules for the folders the starter covered (intent/, spec/ and plan/ among them) with an owner each? The owners are
    whatever 0.x's init filled in; a file with another comment or another path in it is the team's own."""
    text = read_text(os.path.join(root, ".github", "CODEOWNERS"))
    if text is None:
        return False
    lines = [l.rstrip() for l in text.lstrip("\ufeff").splitlines() if l.strip()]
    if len(lines) < 4 or tuple(lines[:3]) != CODEOWNERS_0X or not CODEOWNERS_0X_REPLACE.match(lines[3]):
        return False
    seen = set()
    for line in lines[4:]:
        parts = line.split()
        if line.startswith("#") or len(parts) < 2 or parts[0] not in CODEOWNERS_0X_PATHS or parts[0] in seen:
            return False
        seen.add(parts[0])
    return {"intent/", "spec/", "plan/"} <= seen


def gitignore_block_0x(root: str) -> Optional[Tuple[List[str], int, int]]:
    """(the lines of .gitignore, the index of the 0.x header, the end of its block) — the header 0.x wrote above its ignore
    lines and the lines under it up to the first blank or comment line; None when there is no such header."""
    text = read_style(os.path.join(root, ".gitignore"))[0]
    if text is None:
        return None
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if l.strip() == GITIGNORE_HEADER_0X), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and lines[end].strip() and not lines[end].lstrip().startswith("#"):
        end += 1
    return lines, start, end


def has_lessons(text: str) -> bool:
    """A 0.x LESSONS.md holds something of the team's once a line outside the HTML comment is not one of the template's own
    (the template shows the entry format inside the comment). Only an untouched template is a leftover."""
    text = re.sub(r"<!--.*?-->", "", text.lstrip("﻿"), flags=re.S)
    return any(l.strip() and l.strip() not in LESSONS_LINES for l in text.splitlines())


def triage_drafts(root: str, intent_rel: str) -> List[str]:
    """The 0.x triage drafts (`<intent dir>/triage/*.md`): 1.0 has no triage queue and does not read them."""
    d = os.path.join(root, intent_rel, "triage")
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return sorted(n for n in names if n.endswith(".md") and not n.startswith(".") and os.path.isfile(os.path.join(d, n)))


def project_md_files(root: str) -> List[Tuple[str, str]]:
    """[(path, text)] of the project's own skills, commands and agents (`.claude/skills`, `commands`, `agents`; the Markdown
    files in them), sorted by path."""
    found = []
    for sub in ("skills", "commands", "agents"):
        for top, _, names in os.walk(os.path.join(root, ".claude", sub)):
            for n in names:
                p = os.path.join(top, n)
                if n.endswith(".md"):
                    found.append((os.path.relpath(p, root).replace(os.sep, "/"), read_text(p) or ""))
    return sorted(found)


def stale_files(root: str) -> List[Tuple[str, List[str]]]:
    """[(path, commands)]: the project's own skills, commands and agents that name a command 1.0 removed."""
    return [(p, cmds) for p, text in project_md_files(root) for cmds in [sorted(set(REMOVED_COMMANDS.findall(text)))] if cmds]


def old_workflow_findings(root: str, claude_text: Optional[str], old: Dict[str, str], work_rel: str) -> List[str]:
    """What in CLAUDE.md (`claude_text`, as it stands) and the project's own skills, commands and agents still tells a session
    to work the 0.x way: merge PRs itself (`gh pr merge`), keep the queue in intent/, read spec.md and plan.md. One sentence
    per file and kind, with the line numbers — "CLAUDE.md:162 tells sessions to merge PRs themselves — …". Nothing is edited."""
    intent_dir = old["intent"]
    intent_rx = re.compile(r"(?<![\w./-])(?:%s)/" % "|".join(dict.fromkeys((re.escape(intent_dir), "intent"))), re.I)
    kinds = [(intent_rx if kind == "intent" else rx, say) for kind, rx, say in OLD_WORKFLOW_KINDS]
    files = [("CLAUDE.md", claude_text or "")]
    agents = os.path.join(root, "AGENTS.md")
    if os.path.isfile(agents) and not same_file(agents, os.path.join(root, "CLAUDE.md")):
        files.append(("AGENTS.md", (read_style(agents)[0] or "")))
    files += project_md_files(root)
    out = []
    for rel, text in files:
        for rx, say in kinds:
            hits = [n for n, line in enumerate(text.split("\n"), 1) if rx.search(line)]
            if hits:
                shown = ",".join(str(n) for n in hits[:5]) + (",…" if len(hits) > 5 else "")
                out.append("%s:%s %s" % (rel, shown, say % {"intent": intent_dir, "work": work_rel}))
    return out


def workflow_text(findings: List[str], cap: int = 3) -> str:
    return " | ".join(findings[:cap]) + (" | … %d more" % (len(findings) - cap) if len(findings) > cap else "")


def stale_text(files: List[Tuple[str, List[str]]]) -> str:
    return "still mentions removed commands: " + ", ".join("%s (%s)" % (p, ", ".join(c)) for p, c in files[:5]) + \
        (" …" if len(files) > 5 else "")


def commits_behind(root: str, default: str, fetch: bool) -> int:
    """How many commits origin/<default> has that HEAD lacks; 0 when there is no such branch to compare with. `fetch` asks
    the remote first — a failure is ignored, the refs as they are are all there is then."""
    from .gitops import Git
    git = Git(root)
    if not git.remote_branch_exists(default):
        return 0
    if fetch:
        git.refresh(FETCH_SECONDS)
    n = git.out("rev-list", "--count", "HEAD..origin/" + default)
    return int(n) if n.isdigit() else 0


def inside(root: str, rel: str) -> bool:
    """Is `rel` a place in the repository — not the root itself, and not somewhere a configured `../x` path leads to."""
    top = os.path.realpath(root) + os.sep
    return os.path.realpath(os.path.join(root, rel)).startswith(top)


def evals_only_0x(root: str, ev: str) -> bool:
    """Does the evals directory hold nothing of the team's — only what 0.x's init copied there (its README, its example cases)
    and what its runner wrote (`results/`)? A directory that is gone counts."""
    d = os.path.join(root, ev)
    if not os.path.isdir(d):
        return True
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if n == "results" or (n == "README.md" and evals_readme_0x(root, ev)):
            continue
        if n == "cases" and os.path.isdir(p) and all(c in EXAMPLE_EVALS for c in os.listdir(p)):
            continue
        return False
    return True


def prune_gitignore(root: str, evals: str, out: Out) -> None:
    """The 0.x block of .gitignore → the 1.x one. Its header gives way to 1.x's (merged into the 1.x block when the file has
    both, so no line stands twice), `.sdlc/logs/` and `.sdlc/state/` stay, and `<evals>/results/` goes — only when the evals
    directory is gone or holds nothing but 0.x's own files, and has no results/ directory left that the line still ignores."""
    block = gitignore_block_0x(root)
    if block is None:
        return
    lines, start, end = block
    names = {evals, "evals", "sdlc-evals"}
    keep, dropped, kept_results = [], [], []
    for line in lines[start + 1:end]:
        ev = next((e for e in sorted(names) if line.strip().rstrip("/") == e + "/results"), None)
        if ev is None:
            keep.append(line)
        elif os.path.isdir(os.path.join(root, ev, "results")) or not evals_only_0x(root, ev):
            keep.append(line)
            kept_results.append(line.strip())
        else:
            dropped.append(line.strip())
    rest = lines[:start] + lines[end:]
    h1 = next((i for i, l in enumerate(rest) if l.strip() == GITIGNORE_HEADER), None)
    if h1 is not None:                                           # 1.x's block is there (init just added to it): the lines join it
        if 0 < start < len(rest) and not rest[start - 1].strip() and not rest[start].strip():
            del rest[start - 1]                                  # the block's own blank line before it is not left behind
            h1 -= 1 if h1 >= start else 0
        have = {l.strip().rstrip("/") for l in rest if l.strip()}
        rest[h1 + 1:h1 + 1] = [l for l in keep if l.strip() and l.strip().rstrip("/") not in have]
        lines = rest
    else:                                                        # none yet: 0.x's block becomes it, complete
        have = {l.strip().rstrip("/") for l in rest + keep if l.strip()}
        lines = lines[:start] + [GITIGNORE_HEADER] + keep + [l for l in GITIGNORE if l.rstrip("/") not in have] + lines[end:]
    cur, bom, nl = read_style(os.path.join(root, ".gitignore"))
    text = "\n".join(lines)
    if text == cur:
        return
    out.write(root, ".gitignore", styled(text, bom, nl))
    out.add("MERGED", ".gitignore", "0.x header replaced by the 1.x one" + (", %s dropped" % ", ".join(dropped) if dropped else "") +
            ("; %s kept (still ignoring what is there)" % ", ".join(kept_results) if kept_results else ""))


def prune(root: str, items: List[str], out: Out, evals: str = "evals") -> None:
    top = os.path.abspath(root)
    for rel in items:
        if rel == GITIGNORE_LEFTOVER:
            continue
        p = os.path.join(top, rel.rstrip("/"))
        if not inside(top, rel):
            out.add("SKIPPED", rel, "outside the repository — left alone")
            continue
        if not out.dry:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
            parent = os.path.dirname(p)
            while parent.startswith(top + os.sep) and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
        out.add("REMOVED", rel, "0.x leftover")
    if GITIGNORE_LEFTOVER in items:
        prune_gitignore(root, evals, out)


# -------------------------------------------------------------------------------------------------------------- init
def ignored_adoption(root: str, work_rel: str, review_rel: str) -> List[str]:
    """The adoption files that git ignores: the queue and the config are read from committed files, so an ignored one is
    invisible to the loop (a clone does not even have it)."""
    from .gitops import Git
    git = Git(root)
    found = []
    for label, rel in ((CONFIG_REL, CONFIG_REL), ("%s/<slug>.md" % work_rel, work_rel + "/x.md"), (review_rel, review_rel)):
        if git.ok("check-ignore", "-q", "--no-index", "--", rel.replace(os.sep, "/")):
            found.append(label.replace(os.sep, "/"))
    return found


def ignored_text(found: List[str]) -> str:
    return "%s %s ignored by git — the queue reads committed files, so the loop cannot see ignored ones; remove the " \
           "rule that ignores them (`git check-ignore -v <path>` names it)" % (", ".join(found), "is" if len(found) == 1 else "are")


def init(root: str, lang: str = "", mode: str = "", commands: str = "", dry: bool = False,
         do_prune: bool = False) -> Dict[str, Any]:
    from .gitops import Git
    out = Out(dry)
    cfg_path = os.path.join(root, CONFIG_REL)
    raw = read_json(cfg_path) if os.path.exists(cfg_path) else None
    if os.path.exists(cfg_path) and not isinstance(raw, dict):
        return {"error": ".sdlc/config.json is not a valid JSON object — fix it by hand; nothing was changed"}
    original = copy.deepcopy(raw)                              # what the 0.x backup keeps, before the clean-up below
    for key in OBJECT_SECTIONS:
        if isinstance(raw, dict) and raw.get(key) is None:
            raw.pop(key, None)                                 # `"commands": null` is the same as no commands
        elif isinstance(raw, dict) and not isinstance(raw[key], dict):
            return {"error": ".sdlc/config.json: \"%s\" must be an object — fix it by hand; nothing was changed" % key}
    det = detect(root)
    if not det["git"]["is_repo"]:
        return {"error": "%s is not a git repository — run `git init` and commit first" % root}
    legacy = isinstance(raw, dict) and config_version(raw) < 2
    dirty = {line[3:] for line in Git(root).dirty()}           # what was uncommitted before init wrote anything
    old = old_paths(root, raw)
    notes: List[str] = []
    if legacy:                   # the 0.x files are read from this checkout: unsaved edits and work merged since come back
        pending = sorted(p for p in dirty if any(p.startswith(old[k] + "/") for k in ("intent", "spec", "plan")))
        if pending:
            msg = ("uncommitted changes under the 0.x folders (%s%s) — the migration reads the files as they are on disk, so "
                   "unsaved edits would become work; commit or stash them and run init again" %
                   (", ".join(pending[:3]), ", …" if len(pending) > 3 else ""))
            if not dry:
                return {"error": msg + "; nothing was changed"}
            notes.append(msg)
        default = str((raw.get("protect") or {}).get("default_branch") or "") or det["git"]["default_branch"]
        behind = commits_behind(root, default, fetch=not dry)
        if behind:
            msg = ("this checkout is %d commit(s) behind origin/%s — 0.x items merged since then would come back as work; "
                   "update it (git pull --ff-only) and run init again" % (behind, default))
            if not dry:
                return {"error": msg + "; nothing was changed"}
            notes.append(msg)
    explicit = parse_commands(commands)
    need_mode = raw is None and not mode                       # a config (or a flag) has the mode: nobody needs the collaborators
    forge, auto_mode, why = detect_forge(root, det["git"], need_mode=need_mode)

    def fill_commands(cm: Dict[str, Any]) -> List[str]:
        """--commands values always; detected ones only for a key the config does not have — an empty string is the user's
        choice (no such command) and stays."""
        changed = []
        for k in COMMAND_KEYS:
            if k in explicit:
                want = explicit[k]
            elif cm.get(k) is None:
                want = det["commands"].get(k, "")
            else:
                continue
            if want != cm.get(k, "") and (want or k in explicit):
                cm[k] = want
                changed.append("commands." + k)
        return changed

    def place(cfg: Dict[str, Any]) -> None:
        """Where the config says the repository's merges and work docs go — decided once, when the config is written
        (a later `work/` of someone else's must not move docs that are already there)."""
        if forge == "local" and det["git"]["remote"]:      # the engine takes any origin for GitHub unless told otherwise
            cfg.setdefault("loop", {})["forge"] = "local"
        if foreign_work_dir(root):
            cfg.setdefault("paths", {})["work"] = "sdlc-work"
            notes.append("work/ already holds other files — work docs go to sdlc-work/ (paths.work)")

    if raw is None:
        asked = bool(mode)
        lang = lang or "en"
        mode = mode or auto_mode
        cfg: Dict[str, Any] = {"version": 2, "language": lang, "mode": mode, "commands": {},
                               "protect": {"default_branch": det["git"]["default_branch"]}}
        fill_commands(cfg["commands"])
        place(cfg)
        out.json(root, CONFIG_REL, cfg)
        out.add("CREATED", CONFIG_REL, "mode %s (%s), language %s" % (mode, "as asked" if asked else why, lang))
    elif legacy:
        cfg, mnotes = migrate_v1(raw)
        notes += mnotes
        lang = lang or str(cfg.get("language") or "en")
        cfg["language"] = lang
        if mode:
            cfg["mode"] = mode
        elif cfg["mode"] == "team" and forge == "local":
            cfg["mode"] = "solo"
            notes.append("mode solo: team mode needs a GitHub remote (%s)" % why)
        fill_commands(cfg.setdefault("commands", {}))
        cfg.setdefault("protect", {}).setdefault("default_branch", det["git"]["default_branch"])
        place(cfg)
        backup = os.path.join(".sdlc", "config.v1.json")       # a row of its own: the file is new, and the adoption commit stages rows
        there = os.path.exists(os.path.join(root, backup))
        same = there and read_json(os.path.join(root, backup)) == original
        if not same:
            out.json(root, backup, original)
        out.json(root, CONFIG_REL, cfg)
        out.add("MIGRATED", CONFIG_REL, "version 1 → 2, mode %s; the old file is kept as .sdlc/config.v1.json" % cfg["mode"])
        out.add("SKIPPED" if same else "MERGED" if there else "CREATED", backup,
                "the 0.x config, already kept" if same else "replaced by the 0.x config just migrated" if there else
                "the 0.x config, kept")
    else:
        cfg = raw
        changed = []
        if lang and cfg.get("language") != lang:
            cfg["language"] = lang
            changed.append("language")
        lang = str(cfg.get("language") or "en")
        if mode and cfg.get("mode") != mode:
            cfg["mode"] = mode
            changed.append("mode")
        changed += fill_commands(cfg.setdefault("commands", {}))
        if not (cfg.get("protect") or {}).get("default_branch"):
            cfg.setdefault("protect", {})["default_branch"] = det["git"]["default_branch"]
            changed.append("protect.default_branch")
        if changed:
            out.json(root, CONFIG_REL, cfg)
        out.add("MERGED" if changed else "SKIPPED", CONFIG_REL, ", ".join(changed))
    if cfg.get("mode") == "team" and forge == "local" and str((cfg.get("loop") or {}).get("forge", "auto")) != "github":
        notes.append("team mode needs a GitHub remote — the loop refuses to run until there is one (or use --solo)")

    paths = dict(DEFAULTS["paths"], **(cfg.get("paths") or {}))
    cmds = {k: str((cfg.get("commands") or {}).get(k) or "") for k in COMMAND_KEYS}
    put(root, paths["work"] + "/README.md", render(tpl(lang, "work-README.md"), work_dir=paths["work"]), out)
    gen = ", ".join((cfg.get("protect") or {}).get("generated_paths") or []) or \
        ("TODO: 생성 파일 경로 (.sdlc/config.json 의 protect.generated_paths 에도 적는다)" if lang == "ko" else
         "TODO: generated paths (also protect.generated_paths in .sdlc/config.json)")
    review_md(root, paths["review"], lang, gen, out)
    claude_text = claude_md(root, lang, cmds, out, paths["work"], notes)
    gitignore(root, out)
    put(root, ".sdlc/frozen-paths.txt", read_text(os.path.join(templates(), "config", "frozen-paths.txt")) or "", out)
    settings(root, cmds, out)

    migrated = migrate_intents(root, old, paths["work"], out, notes) if legacy else []
    if legacy:
        lessons = read_text(os.path.join(root, old["lessons"]))
        if lessons is not None and has_lessons(lessons):
            notes.append("%s holds 0.x lessons — move the ones still true into CLAUDE.md's \"Things Claude gets wrong\"; "
                         "1.0 does not read it" % old["lessons"])
        drafts = triage_drafts(root, old["intent"])
        if drafts:
            notes.append("%s/triage/ holds %d 0.x triage draft(s) (%s%s) — 1.0 does not read them; start the ones still "
                         "wanted with /sdlc:go, or delete them" % (old["intent"], len(drafts), ", ".join(drafts[:5]),
                                                                  ", …" if len(drafts) > 5 else ""))
        pause = str((cfg.get("loop") or {}).get("pause_file") or DEFAULTS["loop"]["pause_file"])
        if os.path.exists(os.path.join(root, pause)):
            notes.append("%s exists — the loop will not start while it is there; delete it, or run `sdlc.py resume` "
                         "(which also starts the loop)" % pause)
    stale = stale_files(root)
    if stale:
        notes.append(stale_text(stale))
    notes += old_workflow_findings(root, claude_text, old, paths["work"])
    ignored = ignored_adoption(root, paths["work"], paths["review"])
    if ignored:
        notes.append(ignored_text(ignored))
    for verb, rel, _ in out.rows:                              # init's edit goes into the adoption commit with what was there
        if verb.replace("WOULD ", "").startswith(("CREATE", "MERGE", "MIGRATE")) and rel.replace(os.sep, "/") in dirty:
            notes.append("%s had uncommitted changes before init ran — the adoption commit would take them along with "
                         "init's edit; commit or stash them first if that is not what you want" % rel)
    left = leftovers(root, old)
    if ".github/CODEOWNERS" in left:
        notes.append(".github/CODEOWNERS is the 0.x starter (owners for intent/, spec/ and plan/, which 1.x does not use) — "
                     "if branch protection asks for code-owner review, write your own rules before `init --prune` removes it")
    if do_prune and left:
        prune(root, left, out, old["evals"])
        left = []
    shown = {k: v for k, v in cmds.items() if v and k in ("build", "test", "lint", "run")}
    optional = [k for k in ("build", "lint") if not cmds.get(k)]
    if optional:
        notes.append("no %s command — optional, the gate skips an empty one (set it in .sdlc/config.json, or with "
                     "`init --commands %s`)" % (" or ".join(optional), ",".join("%s=..." % k for k in optional)))
    if not cmds.get("test"):                                   # nothing at the root: a project one level down is only suggested
        found = sub_projects(root)
        if not found and xcode_project(root):
            found = [{"dir": ".", "stack": [], "commands": {}, "xcode": xcode_project(root)}]
        notes += [suggest_subproject(p) for p in found[:3]]
    return {"rows": [list(r) for r in out.rows], "text": out.text(), "notes": notes, "mode": cfg.get("mode"),
            "language": lang, "forge": forge, "commands": shown, "stack": det["stack"], "git": det["git"],
            "migrated": migrated, "leftovers": left, "dry_run": dry,
            "todo": [] if cmds.get("test") else ["test"]}          # without a test command the gate verifies nothing


# ------------------------------------------------------------------------------------------------------------- check
ENV_WORD = re.compile(r"^[A-Za-z_]\w*=")
SHELL_WORDS = frozenset(("export", "source", ".", "set", "unset", "alias", "eval", "exec", "ulimit", "umask", "wait", "exit",
                         "return", "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac",
                         "time", "{", "}", "!", "[[", "(", ")"))


def package_scripts(root: str, cwd: str) -> Optional[Dict[str, Any]]:
    """The `scripts` of the package.json that `cwd` (or the nearest folder above it, inside `root`) has; None without one or
    when it cannot be read."""
    top = os.path.abspath(root)
    cur = os.path.abspath(cwd)
    while cur == top or cur.startswith(top + os.sep):
        p = os.path.join(cur, "package.json")
        if os.path.isfile(p):
            data = read_json(p)
            if not isinstance(data, dict):
                return None
            scripts = data.get("scripts")
            return scripts if isinstance(scripts, dict) else {}
        if cur == top:
            break
        cur = os.path.dirname(cur)
    return None


def script_name(prog: str, args: List[str]) -> str:
    """The package.json script a `npm`/`pnpm`/`yarn`/`bun` command runs: "" when it runs none or the command points elsewhere
    (another package, another folder)."""
    if any(a.split("=", 1)[0] in PKG_REDIRECTS for a in args):
        return ""
    pos = [a for a in args if not a.startswith("-")]
    if not pos:
        return ""
    first, rest = pos[0], pos[1:]
    if first in ("run", "run-script") or (prog == "npm" and first in ("rum", "urn")):
        return rest[0] if rest else ""
    if prog == "npm":
        return "test" if first in ("test", "t", "tst") else ""     # `npm start` falls back to `node server.js`
    if prog == "bun":
        return ""                                                  # `bun test` is bun's own runner, `bun x` a file
    return "" if first in PKG_COMMANDS else first               # `pnpm test`, `yarn lint`: the script, else a program in .bin


def command_problem(root: str, command: str) -> str:
    """What is wrong with a configured command, judged without running it: "" when it looks runnable, else a short reason.
    A leading `cd <dir> &&` moves where the parts after it are judged; a program written with a slash must be an executable
    file; a bare program must be on PATH (or a file here); `npm run <script>` and its kin need that script in package.json."""
    cwd = root
    for part in split_compound(command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and ENV_WORD.match(words[0]):
            words = words[1:]
        if not words or words[0] in SHELL_WORDS or re.match(r"^[\d<>&$`(~!]", words[0]):
            continue
        prog, args = words[0], words[1:]
        if prog == "cd":
            target = args[0] if args else ""
            if not target or target.startswith(("-", "$", "~", "`")):
                continue
            nxt = os.path.normpath(os.path.join(cwd, target))
            if not os.path.isdir(nxt):
                return "`cd %s`: no such folder" % target
            cwd = nxt
            continue
        if "/" in prog:
            path = prog if os.path.isabs(prog) else os.path.join(cwd, prog)
            if not os.path.isfile(path):
                return "`%s` not found" % prog
            if not os.access(path, os.X_OK):
                return "`%s` is not executable (chmod +x %s)" % (prog, prog)
            continue
        if not (shutil.which(prog) or os.path.exists(os.path.join(cwd, prog))):
            return "`%s` not found" % prog
        if prog in ("npm", "pnpm", "yarn", "bun"):
            name = script_name(prog, args)
            scripts = package_scripts(root, cwd) if name else None
            if name and scripts is not None and name not in scripts and \
                    not (prog != "npm" and os.path.exists(os.path.join(root, "node_modules", ".bin", name))):
                return "package.json has no `%s` script" % name
    return ""


def dependency_findings(docs: List[Any]) -> List[str]:
    """What is wrong with the `depends_on` lines of the work docs that are still open: a dependency that is no work doc (the item
    would wait for it for good) and dependencies that wait for each other in a circle."""
    names = {os.path.splitext(os.path.basename(d.path))[0]: d for d in docs}
    live = {n: [str(x) for x in d.depends_on] for n, d in names.items() if d.status not in ("done", "dropped")}
    found = ["%s depends on %s, which is no work doc" % (n, ", ".join(sorted(m)))
             for n, deps in sorted(live.items()) for m in [[x for x in deps if x not in names]] if m]
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on: Set[str] = set()
    circles: List[List[str]] = []

    def visit(n: str) -> None:                                 # Tarjan: a group of more than one (or one that names itself) is a circle
        index[n] = low[n] = len(index)
        stack.append(n)
        on.add(n)
        for m in live[n]:
            if m not in live:
                continue
            if m not in index:
                visit(m)
                low[n] = min(low[n], low[m])
            elif m in on:
                low[n] = min(low[n], index[m])
        if low[n] == index[n]:
            group = []
            while True:
                m = stack.pop()
                on.discard(m)
                group.append(m)
                if m == n:
                    break
            if len(group) > 1 or n in live[n]:
                circles.append(sorted(group))
    for n in sorted(live):
        if n not in index:
            visit(n)
    for group in sorted(circles):
        path, cur = [group[0]], group[0]
        while True:                                            # one way round the circle, for the message
            nxt = next((m for m in sorted(live[cur]) if m in group and (m == group[0] or m not in path)), None)
            if nxt is None or nxt == group[0]:
                break
            path.append(nxt)
            cur = nxt
        found.append("dependencies wait for each other: %s" % " → ".join(path + [group[0]]))
    return found


# ---- the public site: what a static host would serve of the files sdlc puts in a repository
def site_output_configured(root: str, tracked: Set[str]) -> bool:
    """Does the repository's hosting serve a build output (not the checkout's root)? Then the files in the repository are not
    what is public."""
    pkg = read_json(os.path.join(root, "package.json"))
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if isinstance(scripts, dict) and str(scripts.get("build") or "").strip():
        return True
    vercel = read_json(os.path.join(root, "vercel.json"))
    if isinstance(vercel, dict) and (str(vercel.get("buildCommand") or "").strip() or
                                     str(vercel.get("outputDirectory") or ".").strip() not in (".", "./", "")):
        return True
    firebase = read_json(os.path.join(root, "firebase.json"))
    hosting = firebase.get("hosting") if isinstance(firebase, dict) else None
    for h in hosting if isinstance(hosting, list) else [hosting]:
        if isinstance(h, dict) and str(h.get("public") if h.get("public") is not None else ".").strip() not in (".", "./", ""):
            return True
    for name, key in (("netlify.toml", "publish"), ("wrangler.toml", "pages_build_output_dir")):
        text = read_text(os.path.join(root, name)) or ""
        for m in re.finditer(r"^\s*%s\s*=\s*[\"']([^\"']*)[\"']" % key, text, re.M):
            if m.group(1).strip() not in (".", "./", ""):
                return True
    wf = os.path.join(root, ".github", "workflows")
    for n in sorted(os.listdir(wf)) if os.path.isdir(wf) else []:
        if not n.endswith((".yml", ".yaml")):
            continue
        text = read_text(os.path.join(wf, n)) or ""
        for m in re.finditer(r"uses:\s*actions/upload-pages-artifact[^\n]*\n", text):
            step = re.split(r"\n\s*-\s+\w+:", text[m.end():m.end() + 800])[0]
            pm = re.search(r"^\s*path:\s*['\"]?([^'\"\s#]*)", step, re.M)
            if (pm.group(1) if pm else "_site") not in (".", "./", ""):
                return True
    return False


def glob_regex(pattern: str, anywhere: bool) -> "re.Pattern[str]":
    """A path glob as a regular expression (`**` crosses folders, `*` and `?` do not). `anywhere`: one without a slash matches
    at any depth, as git's patterns and Firebase's `ignore` do."""
    pat = pattern.strip()
    pat = pat[2:] if pat.startswith("./") else pat
    pat = pat.lstrip("/").rstrip("/")
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pat[i]
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile(("(?:.*/)?" if anywhere and "/" not in pat else "") + "".join(out) + "$")


def glob_excluded(patterns: List[str], path: str, anywhere: bool) -> bool:
    """Is `path` — or a folder above it — matched by one of the globs?"""
    parts = path.split("/")
    rxs = [glob_regex(p, anywhere) for p in patterns if p and p.strip()]
    return any(rx.match("/".join(parts[:i])) for rx in rxs for i in range(1, len(parts) + 1))


def jekyll_excludes(text: str) -> List[str]:
    """The `exclude:` entries of a Jekyll `_config.yml` (a block list or a flow list)."""
    out: List[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^exclude:\s*(.*?)\s*(?:#.*)?$", line)
        if not m:
            continue
        if m.group(1).startswith("["):
            out += [x.strip().strip("'\"") for x in m.group(1).strip("[]").split(",") if x.strip()]
        for nxt in lines[i + 1:]:
            lm = re.match(r"^\s+-\s*(.*?)\s*(?:#.*)?$", nxt)
            if lm:
                out.append(lm.group(1).strip("'\""))
            elif nxt.strip() and not nxt.lstrip().startswith("#"):
                break
    return out


def not_found_rules(root: str) -> List["re.Pattern[str]"]:
    """The paths a Netlify / Cloudflare Pages `_redirects` file (or netlify.toml's `[[redirects]]`) answers with 404."""
    froms: List[str] = []
    for line in (read_text(os.path.join(root, "_redirects")) or "").split("\n"):
        words = line.split("#")[0].split()
        if len(words) >= 3 and re.match(r"^404!?$", words[-1]):
            froms.append(words[0])
    toml = read_text(os.path.join(root, "netlify.toml")) or ""
    for block in re.split(r"^\[\[redirects\]\]", toml, flags=re.M)[1:]:
        block = re.split(r"^\[", block, flags=re.M)[0]
        src, status = re.search(r"^\s*from\s*=\s*[\"']([^\"']+)[\"']", block, re.M), re.search(r"^\s*status\s*=\s*404\b", block, re.M)
        if src and status:
            froms.append(src.group(1))
    rules = []
    for f in froms:
        rx = re.sub(r":[A-Za-z_]\w*", "[^/]+", re.escape(f).replace(r"\*", ".*"))
        rules.append(re.compile(rx + "$"))
    return rules


def ignored_by_file(paths: List[str], ignore_file: str) -> Set[str]:
    """Which of `paths` a gitignore-style file (`.vercelignore`) lists. Asked of an empty repository of its own, so this
    repository's `.gitignore` does not answer for it."""
    if not paths:
        return set()
    with tempfile.TemporaryDirectory() as tmp:
        if not run(["git", "init", "-q", tmp], timeout=30).ok:
            return set()
        r = run(["git", "-C", tmp, "-c", "core.excludesFile=" + ignore_file, "check-ignore", "--no-index", "--stdin", "-z"],
                timeout=60, input="\0".join(paths) + "\0")
    return {p for p in r.out.split("\0") if p} if r.rc in (0, 1) else set()


def public_site(root: str, cfg: Config, old: Dict[str, str]) -> List[str]:
    """The files sdlc puts in the repository that a static site made of this checkout would serve — `[]` when the check is
    off, when there is no `index.html` at the root (no site), when the hosting serves a build output instead, or when the
    host's own exclusion list (`.vercelignore`, Jekyll's, Firebase's `ignore`, a 404 rule) covers them."""
    if cfg.get("check.public_site") is False:
        return []
    listing = run(["git", "-C", root, "ls-files", "-z"], timeout=60)
    tracked = [p for p in listing.out.split("\0") if p] if listing.ok else []
    if "index.html" not in tracked or site_output_configured(root, set(tracked)):
        return []
    cands: List[Tuple[str, Any]] = []
    for rel in ("CLAUDE.md", "AGENTS.md", cfg.rel("review")):
        cands.append((rel, lambda p, rel=rel: p == rel))
    zero_x = (old["intent"], old["spec"], old["plan"]) if any(p.startswith(old["intent"] + "/") for p in tracked) else ()
    for rel in (cfg.rel("work"), ".sdlc", "docs/claude", cfg.rel("checks"), ".claude") + zero_x:
        rel = rel.strip("/")
        if rel and not rel.startswith(".."):
            cands.append((rel + "/", lambda p, rel=rel: p.startswith(rel + "/")))
    files = {p: [lab for lab, hit in cands if hit(p)] for p in tracked}
    files = {p: labs for p, labs in files.items() if labs}
    if not files:
        return []
    served = sorted(files)
    vi = os.path.join(root, ".vercelignore")
    if os.path.isfile(vi):
        hidden = ignored_by_file(served, vi)
        served = [p for p in served if p not in hidden]
    if os.path.isfile(os.path.join(root, "_config.yml")):                        # Jekyll (GitHub Pages)
        extra = jekyll_excludes(read_text(os.path.join(root, "_config.yml")) or "")
        if ".nojekyll" not in tracked and not os.path.exists(os.path.join(root, ".nojekyll")):    # with it, Jekyll does not run
            served = [p for p in served if not any(c.startswith((".", "_", "#")) for c in p.split("/")) and
                      not glob_excluded(extra, p, anywhere=False)]
    fb = read_json(os.path.join(root, "firebase.json"))
    hosting = fb.get("hosting") if isinstance(fb, dict) else None
    ignore = [str(x) for h in (hosting if isinstance(hosting, list) else [hosting]) if isinstance(h, dict)
              for x in (h.get("ignore") or []) if isinstance(h.get("ignore"), list)]
    if ignore:
        served = [p for p in served if not glob_excluded(ignore, p, anywhere=True)]
    rules = not_found_rules(root)
    if rules:
        served = [p for p in served if not any(rx.match("/" + p) for rx in rules)]
    exposed = []
    for lab, _ in cands:
        if lab not in exposed and any(lab in files[p] for p in served):
            exposed.append(lab)
    return exposed


def public_site_text(exposed: List[str]) -> str:
    return "%s would be served; list them in .vercelignore (or your host's equivalent)" % ", ".join(exposed)


def check(root: str) -> Dict[str, Any]:
    """What is set up, what is missing, what to do about it. Changes nothing."""
    from . import state as st
    from .gitops import Git, make_forge, remote_host
    from .session import Runner
    rows: List[Dict[str, str]] = []

    def row(status: str, cid: str, label: str, detail: str = "") -> None:
        rows.append({"status": status, "id": cid, "label": label, "detail": detail})

    def result() -> Dict[str, Any]:
        return {"root": root, "version": plugin_version(), "checks": rows,
                "ok": all(r["status"] != "missing" for r in rows)}

    git = Git(root)
    if not git.is_repo():
        row("missing", "git", "git repository", "run `git init` and commit")
        return result()
    row("ok", "git", "git repository", "on %s" % (git.branch() or "a detached HEAD"))
    raw = read_json(os.path.join(root, CONFIG_REL))
    if not isinstance(raw, dict):
        row("missing", "config", ".sdlc/config.json",
            "not a valid JSON object — fix it by hand" if os.path.exists(os.path.join(root, CONFIG_REL)) else "run /sdlc:init")
        return result()
    cfg = Config(root)
    old = old_paths(root, cfg.raw)
    odd = [k for k in OBJECT_SECTIONS if raw.get(k) is not None and not isinstance(raw[k], dict)]
    if odd:
        row("warn", "config", ".sdlc/config.json",
            "%s must be an object — the defaults stand until you fix it" % ", ".join('"%s"' % k for k in odd))
    elif cfg.legacy:
        row("warn", "config", ".sdlc/config.json", "version 1 (0.x) — run /sdlc:init once to move to 1.0")
    else:
        row("ok", "config", ".sdlc/config.json", "mode %s, language %s" % (cfg.get("mode"), cfg.get("language")))
    branch = cfg.default_branch
    if git.head():                                         # a repository without a commit has no branch to name yet
        remote = git.has_remote()
        here = git.branch_exists(branch) or git.remote_branch_exists(branch)
        if not here and remote and not skip_remote():      # a branch the remote has and this clone has not fetched
            here = bool((ls_remote(root, ["--heads", "origin", branch]) or "").strip())
        theirs = remote_default_branch(root) if remote else ""
        if not here:
            row("missing", "default_branch", "default branch", "protect.default_branch is '%s', but no such branch exists here "
                "or on origin%s — set it in .sdlc/config.json" % (branch, " (origin's default is '%s')" % theirs if theirs else ""))
        elif theirs and theirs != branch:
            row("warn", "default_branch", "default branch", "protect.default_branch is '%s', but origin's default branch is "
                "'%s' — the loop merges into '%s'; change it in .sdlc/config.json if that is not what you want" %
                (branch, theirs, branch))
    for k in ("build", "test", "lint"):
        c = cfg.command(k)
        if not c:
            if k == "test":
                row("missing", "cmd.test", "test command", "not set (commands.test)")
            else:
                row("ok", "cmd." + k, "%s command" % k, "not set — optional, the gate skips an empty command")
            continue
        problem = command_problem(root, c)
        row("warn" if problem else "ok", "cmd." + k, "%s command" % k, c + (" — " + problem if problem else ""))
    cm = read_text(os.path.join(root, "CLAUDE.md"))
    if cm is None:
        row("missing", "claude_md", "CLAUDE.md", "missing")
    else:
        n_sec = sum(1 for s in SECTIONS if section_present(cm, s))
        n = len(cm.splitlines())
        cap = int(cfg.get("claude_md.max_lines", 120) or 120)
        issues = (["%d/5 sections" % n_sec] if n_sec < 5 else []) + (["%d lines, over %d" % (n, cap)] if n > cap else []) + \
            (["TODO placeholders left"] if "TODO:" in OPTIONAL_TODO.sub("", cm) else []) + \
            (["mentions removed commands"] if REMOVED_COMMANDS.search(cm) else [])
        row("warn" if issues else "ok", "claude_md", "CLAUDE.md", "; ".join(issues) or "5 sections, %d lines" % n)
        if os.path.exists(os.path.join(root, "AGENTS.md")) and not same_file(os.path.join(root, "AGENTS.md"),
                                                                              os.path.join(root, "CLAUDE.md")) and not imports_agents(cm):
            row("warn", "agents_md", "AGENTS.md", "exists, but CLAUDE.md does not import it (@AGENTS.md)")
    stale = stale_files(root)
    if stale:
        row("warn", "skills", "project skills", stale_text(stale))
    found = old_workflow_findings(root, cm, old, cfg.rel("work"))
    if found:
        row("warn", "workflow", "0.x workflow", workflow_text(found))
    rv = read_text(cfg.path("review"))
    issues = ([] if rv is None else (["generated paths still TODO"] if "TODO:" in rv else []) +
              (["names spec.md/plan.md from 0.x — the reviewer reads the work doc"] if OLD_REVIEW_FILES.search(rv) else []))
    row("missing" if rv is None else "warn" if issues else "ok", "review", cfg.rel("review"),
        "missing" if rv is None else "; ".join(issues))
    docs = list_docs(cfg.path("work"))
    bad = []
    for d in docs:
        p = problems(d, os.path.splitext(os.path.basename(d.path))[0], cfg.rel("work"))
        if p:
            bad.append("%s: %s" % (os.path.basename(d.path), "; ".join(p)))
    row("warn" if bad else "ok", "work", cfg.rel("work") + "/",
        ("%d work docs" % len(docs)) + ("; " + " | ".join(bad[:3]) if bad else ""))
    deps = dependency_findings(docs)
    if deps:
        row("warn", "deps", "dependencies", workflow_text(deps))
    have = set(l.strip() for l in (read_text(os.path.join(root, ".gitignore")) or "").splitlines())
    miss = [l for l in GITIGNORE if l not in have and l.rstrip("/") not in have]
    row("warn" if miss else "ok", "gitignore", ".gitignore", ("missing: " + ", ".join(miss)) if miss else "")
    hidden = ignored_adoption(root, cfg.rel("work"), cfg.rel("review"))
    if hidden:
        row("warn", "ignored", "ignored files", ignored_text(hidden))
    runner = Runner(root)                                   # the loop's own lookup: SDLC_CLAUDE_BIN, CLAUDE_BIN, else PATH
    if runner.available():
        ver = run([runner.claude, "--version"], timeout=20)
        row("ok", "claude", "claude CLI", (ver.out.strip().splitlines() or [runner.claude])[0] if ver.ok else
            "%s (--version failed)" % runner.claude)
    else:
        row("missing", "claude", "claude CLI", "%s is not executable — fix SDLC_CLAUDE_BIN (or CLAUDE_BIN)" % runner.claude if runner.claude else
            "not found — put claude on PATH or set SDLC_CLAUDE_BIN (the loop runs it)")
    forge = make_forge(git, cfg.default_branch, str(cfg.get("loop.forge", "auto")))
    if forge.kind == "github":
        host = remote_host(git) or "github.com"
        login = gh_login(root, host)
        row("ok" if login == "yes" else "warn" if login == "offline" else "missing", "gh", "gh",
            ("logged in to %s" % host) if login == "yes" else
            "logged in to %s, but it did not answer (network or VPN?)" % host if login == "offline" else
            "not logged in — run `gh auth login --hostname %s`" % host)
    else:
        row("ok", "forge", "merges", "on this machine (squash merge)")
        if not cfg.solo:
            row("missing", "mode", "team mode", "needs a GitHub remote — or set mode solo")
    pid = st.loop_pid(root)
    last = st.read_status(root)
    row("ok", "loop", "loop", ("running (pid %d)" % pid) if pid else
        "not running" + ((" — last stop: %s" % last["stop"]) if last.get("stop") else ""))
    if os.path.exists(os.path.join(root, str(cfg.get("loop.pause_file") or DEFAULTS["loop"]["pause_file"]))):
        row("warn", "pause", "pause file", "paused — `sdlc.py resume` removes it")
    waiting = sorted(s for s, r in st.load_items(root).items() if r.get("state") in ("needs-human", "blocked"))
    if waiting:
        row("warn", "items", "items", "waiting for a person: " + ", ".join(waiting[:5]))
    left = leftovers(root, old)
    if left:
        row("warn", "leftovers", "0.x leftovers", ", ".join(left[:5]) + (" …" if len(left) > 5 else "") +
            " — `init --prune` removes them")
    exposed = public_site(root, cfg, old)
    if exposed:
        row("warn", "public_site", "public site", public_site_text(exposed))
    return result()


def render_check(r: Dict[str, Any]) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "missing": "FAIL"}
    lines = ["sdlc %s — %s" % (r.get("version"), r["root"])]
    lines += ["%s  %-20s %s" % (mark.get(c["status"], "?   "), c["label"], c["detail"]) for c in r["checks"]]
    return "\n".join(lines)


def main(root: str, argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="sdlc.py init", description="Adopt a repository (or move a 0.x one to 1.0).")
    ap.add_argument("--lang", choices=["en", "ko"], default="")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--solo", action="store_true", help="the loop merges what passes the gate")
    grp.add_argument("--team", action="store_true", help="people approve designs and merge")
    ap.add_argument("--commands", default="", help="build=..,test=..,lint=..,run=..")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true", help="remove files 0.x generated that 1.0 does not use")
    ap.add_argument("--check", action="store_true", help="health report; changes nothing")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        r = check(root)
        print(json.dumps(r, ensure_ascii=False, indent=2) if a.json else render_check(r))
        return 0 if r["ok"] else 1
    r = init(root, lang=a.lang, mode="solo" if a.solo else "team" if a.team else "", commands=a.commands,
             dry=a.dry_run, do_prune=a.prune)
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        return 2
    if a.json:
        r.pop("text", None)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("sdlc init — %s%s" % (root, " (dry run: nothing written)" if a.dry_run else ""))
    print("%-17s stack: %s | %s" % ("DETECTED", ", ".join(r["stack"]) or "none",
                                     ", ".join("%s=%s" % kv for kv in r["commands"].items()) or "no commands"))
    print(r["text"])
    for n in r["notes"]:
        print("%-17s %s" % ("NOTE", n))
    if r["leftovers"]:
        print("%-17s %s — `init --prune` removes them" % ("LEFTOVER", ", ".join(r["leftovers"])))
    if r["todo"]:
        print("%-17s set %s in .sdlc/config.json and CLAUDE.md" % ("TODO", ", ".join("commands." + k for k in r["todo"])))
    return 0
