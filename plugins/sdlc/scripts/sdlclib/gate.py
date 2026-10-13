"""The merge gate — decided by the shell, not by the session's own report. No tokens.

Runs in the loop's worktree on `sdlc/<slug>` with the default branch merged in (not committed — aborted afterwards), so
installed dependencies and build caches are the ones the session used. Two kinds of failure:
- fixable: a command failed, a check failed, the review tally is missing or not clean → a short fix session;
- human: something a person must look at — an existing test changed without the design's say-so, a test deleted or
  weakened, acceptance criteria or `test_changes` changed after design, a new dependency, a risk path, a guard file → the
  PR waits with the reasons.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from .config import Config
from .gitops import Git
from .util import any_glob, run, run_shell, tail
from .workdoc import WorkDoc

TEST_DIR_RE = re.compile(r"(^|/)(test|tests|__tests__|androidTest|testFixtures)/")
TEST_FILE_RE = re.compile(r"(_test\.go|\.test\.[A-Za-z]+|\.spec\.[A-Za-z]+|(^|/)test_[^/]*\.py|_test\.py|Tests?\.(java|kt|swift)|"
                          r"Spec\.(scala|kt)|_spec\.rb)$")
TEST_DEF_RE = re.compile(r"^\s*(?:(?:async\s+)?def\s+test_|func\s+Test[A-Z_]|func\s+test[A-Z_]|fn\s+test_|@Test\b|"
                         r"@ParameterizedTest\b|(?:it|test|describe)(?:\.each\([^)]*\))?\s*\(|should\s*\()")
SKIP_RE = re.compile(r"(\.skip\s*\(|\.only\s*\(|\bxit\s*\(|\bxdescribe\s*\(|\bfit\s*\(|\bfdescribe\s*\(|"
                     r"@pytest\.mark\.(?:skip|xfail)|pytest\.skip\s*\(|@unittest\.skip|@Disabled\b|@Ignore\b|\bt\.Skip\s*\(|"
                     r"XCTSkip|\.todo\s*\()")
ASSERT_RE = re.compile(r"(\bassert\w*\b|\bexpect\s*\(|XCTAssert|assertThat|\bt\.(?:Error|Fatal)f?\(|\brequire\.\w+\(|"
                       r"assert_eq!|assert_ne!|assert!|\.should\b|\bverify\s*\()")


class GateResult:
    def __init__(self) -> None:
        self.fixable: List[str] = []
        self.human: List[str] = []
        self.conflict = False
        self.outputs: Dict[str, str] = {}
        self.notes: List[str] = []
        self.ran: List[str] = []

    @property
    def passed(self) -> bool:
        return not self.fixable and not self.human and not self.conflict

    def summary(self) -> str:
        if self.passed:
            return "passed (%s)" % (", ".join(self.ran) or "no commands")
        parts = []
        if self.conflict:
            parts.append("merge conflict with the default branch")
        parts += self.fixable + self.human
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "fixable": self.fixable, "human": self.human, "conflict": self.conflict,
                "ran": self.ran, "notes": self.notes}


# ---------------------------------------------------------------------------------------------------------------------
def is_test_path(cfg: Config, path: str) -> bool:
    for pat in cfg.get("protect.test_paths", ["auto"]) or []:
        if pat == "auto":
            if path.startswith(cfg.rel("work") + "/"):
                continue
            if TEST_DIR_RE.search(path) or TEST_FILE_RE.search(path) or re.search(r"(^|/)spec/", path):
                return True
        elif any_glob([pat], path):
            return True
    return False


def test_integrity(cfg: Config, git: Git, base: str, head: str) -> List[str]:
    issues: List[str] = []
    changed = git.changed(base, head)
    for st, path in changed:
        if st == "D" and is_test_path(cfg, path):
            issues.append("test file deleted: %s" % path)
    modified = [p for st, p in changed if st == "M" and is_test_path(cfg, p)]
    added_files = [p for st, p in changed if st == "A" and is_test_path(cfg, p)]
    for path in modified + added_files:
        diff = git.diff(base, head, [path])
        removed_defs = added_defs = removed_asserts = added_asserts = 0
        skips = []
        for line in diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("-"):
                body = line[1:]
                removed_defs += bool(TEST_DEF_RE.search(body))
                removed_asserts += len(ASSERT_RE.findall(body))
            elif line.startswith("+"):
                body = line[1:]
                added_defs += bool(TEST_DEF_RE.search(body))
                added_asserts += len(ASSERT_RE.findall(body))
                if SKIP_RE.search(body):
                    skips.append(body.strip()[:80])
        if path in modified and removed_defs > added_defs:
            issues.append("%d test case(s) removed from %s" % (removed_defs - added_defs, path))
        if path in modified and removed_asserts > added_asserts:
            issues.append("%d assertion(s) removed from %s" % (removed_asserts - added_asserts, path))
        for s in skips[:3]:
            issues.append("skip/only added in %s: %s" % (path, s))
    return issues


def _section(text: Optional[str], name: str) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", WorkDoc.parse(text).section(name)).strip()


def design_text(cfg: Config, git: Git, slug: str, base: str, head: str) -> Optional[str]:
    """The work doc as the design decided it. The design commit is the first commit on the branch where the doc is planned
    — or, for an item that was planned before the branch was cut (a migrated one), the doc as the branch started from it.
    None when there is no such commit."""
    rel = "%s/%s.md" % (cfg.rel("work"), slug)
    start = git.show(git.out("merge-base", base, head) or base, rel)
    if start and WorkDoc.parse(start).status in ("planned", "built", "done"):
        return start
    for sha in git.out("log", "--format=%H", "--reverse", "%s..%s" % (base, head), "--", rel).split():
        text = git.show(sha, rel)
        if text and WorkDoc.parse(text).status in ("planned", "built", "done"):
            return text
    return None


def acceptance_changed(cfg: Config, git: Git, slug: str, base: str, head: str) -> Optional[str]:
    """Acceptance criteria at the design commit vs head."""
    design = design_text(cfg, git, slug, base, head)
    if design is None:
        return None
    rel = "%s/%s.md" % (cfg.rel("work"), slug)
    before, after = _section(design, "Acceptance criteria"), _section(git.show(head, rel), "Acceptance criteria")
    if before and before != after:
        return "acceptance criteria changed after the design commit (work/%s.md)" % slug
    return None


def test_changes_of(text: Optional[str]) -> str:
    """`allowed` or `none` — anything but exactly `allowed` leaves existing tests locked, as the hooks read it too."""
    return "allowed" if text and str(WorkDoc.parse(text).meta.get("test_changes", "")).strip() == "allowed" else "none"


def test_changes_changed(cfg: Config, git: Git, slug: str, base: str, head: str) -> Optional[str]:
    """`test_changes` at the design commit vs head: like the criteria it is the design's decision, so a session cannot
    unlock existing tests for itself."""
    design = design_text(cfg, git, slug, base, head)
    if design is None:
        return None
    before = test_changes_of(design)
    after = test_changes_of(git.show(head, "%s/%s.md" % (cfg.rel("work"), slug)))
    if before != after:
        return "test_changes changed after the design commit: %s → %s (work/%s.md)" % (before, after, slug)
    return None


def existing_tests_changed(cfg: Config, git: Git, slug: str, base: str, head: str) -> Optional[str]:
    """Existing test files (present where the branch left the default branch) that the branch modified, deleted or renamed
    away, when the design did not allow that. Test files the branch adds are free."""
    design = design_text(cfg, git, slug, base, head)
    frozen = test_changes_of(design if design is not None else git.show(head, "%s/%s.md" % (cfg.rel("work"), slug)))
    if frozen == "allowed" or not cfg.get("protect.protect_tests", True):
        return None
    kinds = {"M": "modified", "T": "modified", "D": "deleted or renamed"}
    files = ["%s (%s)" % (path, kinds[st]) for st, path in git.changed(base, head) if st in kinds and is_test_path(cfg, path)]
    if not files:
        return None
    return "existing tests changed, but the design said test_changes: none — " + _list(files, 8)


def _list(items: List[str], cap: int) -> str:
    return ", ".join(items[:cap]) + (" … (+%d more)" % (len(items) - cap) if len(items) > cap else "")


# ---------- guard files ----------
def _guard_rel(cfg: Config, path: str) -> str:
    """A configured path as git names it: relative to the repository root, no `./`."""
    p = os.path.normpath(path)
    if os.path.isabs(p) and p.startswith(cfg.root.rstrip(os.sep) + os.sep):
        p = os.path.relpath(p, cfg.root)
    return p


def guard_files_changed(cfg: Config, git: Git, base: str, head: str) -> Optional[str]:
    """The files that keep a session in bounds — `.sdlc/config.json`, the frozen-paths file, the Claude Code settings and
    the gate's own checks directory. A change to any of them on the branch always waits for a person: a session must not
    loosen its own guardrails and have that merged unattended. Work docs and `.sdlc/state` are not guard files."""
    files = {".sdlc/config.json", ".claude/settings.json", ".claude/settings.local.json"}
    frozen = str(cfg.get("protect.frozen_paths_file", "") or "")
    if frozen:
        files.add(_guard_rel(cfg, frozen))
    checks = cfg.rel("checks") + "/"
    kinds = {"A": "added", "D": "deleted or renamed"}
    hits = ["%s (%s)" % (path, kinds.get(st, "modified")) for st, path in sorted(git.changed(base, head), key=lambda c: c[1])
            if path in files or path.startswith(checks)]
    if not hits:
        return None
    return "guard files changed, a person merges these — " + _list(hits, 8)


# ---------- dependencies ----------
MANIFESTS = ("package.json", "requirements", "pyproject.toml", "Pipfile", "go.mod", "Cargo.toml", "Gemfile",
             "build.gradle", "build.gradle.kts", "libs.versions.toml", "pom.xml", "Package.swift", "Podfile", "composer.json",
             "pubspec.yaml")


def is_manifest(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(base == m or (m == "requirements" and base.startswith("requirements") and base.endswith((".txt", ".in")))
               for m in MANIFESTS)


def deps_of(path: str, text: Optional[str]) -> Set[str]:
    if not text:
        return set()
    base = path.rsplit("/", 1)[-1]
    out: Set[str] = set()
    if base in ("package.json", "composer.json"):
        try:
            d = json.loads(text)
        except ValueError:
            return out
        for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "require", "require-dev"):
            if isinstance(d.get(k), dict):
                out.update(d[k].keys())
    elif base.startswith("requirements"):
        for line in text.splitlines():
            m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)", line)
            if m and not line.strip().startswith(("#", "-")):
                out.add(m.group(1).lower())
    elif base in ("pyproject.toml", "Pipfile"):
        out.update(x.lower() for x in re.findall(r'^\s*"([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(?:[<>=!~;\[@ ]|")', text, re.M))
        sect = ""
        for line in text.splitlines():
            s = re.match(r"^\s*\[([^\]]+)\]", line)
            if s:
                sect = s.group(1)
                continue
            if "dependencies" in sect or sect in ("packages", "dev-packages"):
                m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*=", line)
                if m and m.group(1).lower() != "python":
                    out.add(m.group(1).lower())
    elif base == "go.mod":
        out.update(re.findall(r"^\s*(?:require\s+)?([a-z0-9][\w.\-]*\.[a-z]{2,}/[^\s]+)\s+v", text, re.M))
    elif base == "Cargo.toml":
        sect = ""
        for line in text.splitlines():
            s = re.match(r"^\s*\[([^\]]+)\]", line)
            if s:
                sect = s.group(1)
                continue
            if sect.endswith("dependencies"):
                m = re.match(r"^\s*([A-Za-z0-9_\-]+)\s*=", line)
                if m:
                    out.add(m.group(1))
    elif base == "Gemfile":
        out.update(re.findall(r"^\s*gem\s+['\"]([^'\"]+)", text, re.M))
    elif base.startswith("build.gradle"):
        out.update(re.findall(r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|androidTestImplementation|"
                              r"kapt|ksp|annotationProcessor|classpath)\s*\(?\s*[\"']([^\"':]+:[^\"':]+)", text))
        out.update(re.findall(r"(?:implementation|api|testImplementation|ksp|kapt)\s*\(?\s*(libs\.[\w.]+)", text))
    elif base == "libs.versions.toml":
        out.update(re.findall(r"module\s*=\s*\"([^\"]+)\"", text))
        out.update("%s:%s" % m for m in re.findall(r"group\s*=\s*\"([^\"]+)\"\s*,\s*name\s*=\s*\"([^\"]+)\"", text))
    elif base == "pom.xml":
        for blk in re.findall(r"<dependency>(.*?)</dependency>", text, re.S):
            g = re.search(r"<groupId>\s*([^<\s]+)", blk)
            a = re.search(r"<artifactId>\s*([^<\s]+)", blk)
            if g and a:
                out.add("%s:%s" % (g.group(1), a.group(1)))
    elif base == "Package.swift":
        out.update(re.findall(r"\.package\(\s*(?:name:\s*\"[^\"]*\"\s*,\s*)?url:\s*\"([^\"]+)\"", text))
    elif base == "Podfile":
        out.update(re.findall(r"^\s*pod\s+['\"]([^'\"]+)", text, re.M))
    elif base == "pubspec.yaml":
        sect = ""
        for line in text.splitlines():
            s = re.match(r"^(\w[\w_]*):\s*$", line)
            if s:
                sect = s.group(1)
                continue
            if sect in ("dependencies", "dev_dependencies"):
                m = re.match(r"^  ([a-z0-9_]+):", line)
                if m:
                    out.add(m.group(1))
    return out


def new_dependencies(git: Git, base: str, head: str) -> List[str]:
    added: List[str] = []
    for st, path in git.changed(base, head):
        if st in ("A", "M") and is_manifest(path):
            before = deps_of(path, git.show(git.out("merge-base", base, head) or base, path))
            after = deps_of(path, git.show(head, path))
            for d in sorted(after - before):
                added.append("%s (%s)" % (d, path))
    return added


# ---------------------------------------------------------------------------------------------------------------------
def _untracked(git: Git) -> Set[str]:
    return set(git.out("ls-files", "--others", "--exclude-standard").splitlines())


def run_gate(cfg: Config, git: Git, slug: str, base: str, run_commands: bool = True) -> GateResult:
    """Precondition: the worktree is clean and on sdlc/<slug>. Leaves it on sdlc/<slug>, clean, as found."""
    res = GateResult()
    head = git.head()
    doc_rel = "%s/%s.md" % (cfg.rel("work"), slug)
    before_untracked = _untracked(git)

    # 0. merge the default branch in (not committed) — the tests run against what the merge will produce
    merged_in = False
    if base and git.head(base) and not git.is_ancestor(base, "HEAD"):
        r = git("merge", "--no-commit", "--no-ff", base, timeout=300)
        merged_in = True
        if not r.ok:
            res.conflict = True
            git("merge", "--abort")
            git("reset", "-q", "--hard", head)
            return res
    try:
        # 1. commands
        if run_commands:
            mins = float(cfg.get("gate.timeout_minutes", 30) or 30)
            for name, cmd in cfg.gate_commands():
                r = run_shell(cmd, cwd=git.root, timeout=mins * 60)
                res.ran.append(name)
                if not r.ok:
                    res.outputs[name] = tail(r.text(), 60)
                    res.fixable.append("`%s` failed (exit %s)" % (cmd, r.rc))
            # 7. checks/
            if cfg.get("gate.checks", True):
                cdir = os.path.join(git.root, cfg.rel("checks"))
                if os.path.isdir(cdir):
                    for n in sorted(os.listdir(cdir)):
                        p = os.path.join(cdir, n)
                        if os.path.isfile(p) and os.access(p, os.X_OK) and not n.startswith("."):
                            r = run([p], cwd=git.root, timeout=mins * 60)
                            res.ran.append("checks/" + n)
                            if not r.ok:
                                res.outputs["checks/" + n] = tail(r.text(), 40)
                                res.fixable.append("check %s/%s failed" % (cfg.rel("checks"), n))
    finally:
        if merged_in:
            git("merge", "--abort")
            git("reset", "-q", "--hard", head)
        for p in sorted(_untracked(git) - before_untracked):     # artifacts the commands left behind
            fp = os.path.join(git.root, p)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass

    # 2. review tally (recorded in the work doc by the build session)
    if cfg.get("gate.review", True):
        doc = WorkDoc.parse(git.show(head, doc_rel) or "")
        tally = doc.review_tally()
        if tally is None:
            res.fixable.append("no review tally in work/%s.md (## Review → SDLC_REVIEW_TALLY important=… nits=…)" % slug)
        elif tally[0] > 0:
            res.fixable.append("review: %d Important finding(s) open" % tally[0])
    # 3. test integrity: existing tests untouched unless the design allowed it; none deleted, weakened or skipped either way
    if cfg.get("gate.test_integrity", True):
        t = existing_tests_changed(cfg, git, slug, base, head)
        if t:
            res.human.append(t)
        res.human += ["test integrity: " + i for i in test_integrity(cfg, git, base, head)]
    # 4. the design's decisions frozen: acceptance criteria and test_changes
    if cfg.get("gate.acceptance_frozen", True):
        res.human += [x for x in (acceptance_changed(cfg, git, slug, base, head),
                                  test_changes_changed(cfg, git, slug, base, head)) if x]
    # 5. new dependencies
    if cfg.get("gate.new_dependencies", "human") != "allow":
        res.human += ["new dependency: " + d for d in new_dependencies(git, base, head)]
    # 6. risk paths — and the guard files, whatever risk.human says
    risky = cfg.get("risk.human", []) or []
    if risky:
        hits = []
        for st, path in git.changed(base, head):
            pat = any_glob(risky, path)
            if pat:
                hits.append("%s (%s)" % (path, pat))
        if hits:
            res.human.append("risk path: " + ", ".join(hits[:5]) + (" …" if len(hits) > 5 else ""))
    g = guard_files_changed(cfg, git, base, head)
    if g:
        res.human.append(g)
    return res
