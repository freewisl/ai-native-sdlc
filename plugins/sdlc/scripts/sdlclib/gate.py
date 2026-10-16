"""The merge gate — decided by the shell, not by the session's own report. No tokens.

Runs in the loop's worktree on `sdlc/<slug>` with the default branch merged in (not committed — aborted afterwards), so
installed dependencies and build caches are the ones the session used. Two kinds of failure:
- fixable: a command failed, a check failed, the review tally is missing or not clean, the review did not run in a fresh
  context → a short fix session;
- human: something a person must look at — an existing test changed without the design's say-so, a test deleted or
  weakened, acceptance criteria or `test_changes` changed after design (or no design commit to compare them with), a new
  dependency, a changed test runner configuration, a frozen or generated path, a risk path, a guard file, a diff the gate
  could not compute → the PR waits with the reasons.

Whatever happens, the gate leaves the tree as it found it: at the head it started from, with no file it did not find.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple, TypeVar

from .config import DEFAULTS, Config
from .gitops import Git
from .util import any_glob, read_text, run, run_shell, tail
from .workdoc import WorkDoc

T = TypeVar("T")

TEST_DIR_RE = re.compile(r"(^|/)(test|tests|__tests__|androidTest|testFixtures)/")
TEST_FILE_RE = re.compile(r"(_test\.go|\.test\.[A-Za-z]+|\.spec\.[A-Za-z]+|(^|/)test_[^/]*\.py|_test\.py|Tests?\.(java|kt|swift)|"
                          r"Spec\.(scala|kt)|_spec\.rb)$")
# The test runner's own configuration is protected like a test file: loosening it (a `conftest.py` fixture that swallows
# failures, a `testMatch` that stops finding the tests) weakens every test without touching one. The hooks mirror this list.
TEST_CONFIG_GLOBS = ("conftest.py", "pytest.ini", "jest.config.*", "vitest.config.*", "vitest.workspace.*", "karma.conf.*",
                     ".mocharc", ".mocharc.*", "playwright.config.*", "cypress.config.*", "phpunit.xml", "phpunit.xml.dist")
TEST_CONFIG_RE = re.compile(r"(^|/)(?:%s)$" % "|".join(re.escape(g).replace(r"\*", "[^/]+") for g in TEST_CONFIG_GLOBS))
TEST_DEF_RE = re.compile(r"^\s*(?:(?:async\s+)?def\s+test_|func\s+Test[A-Z_]|func\s+test[A-Z_0-9]|func\s+test\s*\(|fn\s+test_|"
                         r"#\[(?:\w+::)*(?:test|test_case|rstest)\b|@Test\b|@ParameterizedTest\b|"
                         r"(?:it|test|describe)(?:\.each\([^)]*\))?\s*\(|should\s*\()")
RUST_TEST_RE = re.compile(r"^\s*#\[(?:\w+::)*(?:test|test_case|rstest)\b")    # in a .rs file the attribute is the test
SKIP_RE = re.compile("|".join([
    r"\.(?:skip|only|fixme)\s*\(", r"\.(?:skip|only)\.each\b",                            # jest, mocha, vitest, playwright
    r"\bx(?:it|test|describe|context|specify|example|scenario)\b\s*[(\"']",             # jasmine, rspec
    r"\bf(?:it|describe)\s*\(", r"\.todo\s*\(",
    r"\bpytest\.mark\.(?:skip|xfail)\b", r"\bpytest\.mark\.skipif\s*\(\s*(?:True|1)\s*[,)]",   # also inside `pytestmark = [...]`
    r"\bpytest\.skip\s*\(", r"@(?:unittest\.)?skip(?:If|Unless)?\b", r"@unittest\.expectedFailure",
    r"\bself\.skipTest\s*\(", r"\braise\s+(?:unittest\.)?SkipTest\b",
    r"#\[ignore\b",                                                                     # rust
    r"\b(?:t|b|tb)\.Skip(?:f|Now)?\s*\(",                                               # go
    r"^\s*(?:skip|pending)\b\s*(?:$|[(\"'])", r",\s*(?:skip|pending)\s*:\s*(?:true|[\"'])", r",\s*:(?:skip|pending)\b",  # rspec
    r"\bmarkTest(?:Skipped|Incomplete)\b",                                              # phpunit
    r"@Disabled\w*", r"@Ignore\b", r"\[Ignore\b", r"@Test\s*\([^)]*\benabled\s*=\s*false",   # junit, kotlin, nunit, testng
    r"\bXCTSkip",                                                                       # swift
]))
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
        self.filled: List[str] = []      # gate commands whose command line is the branch's own fill in .sdlc/config.json

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
                "ran": self.ran, "filled": self.filled, "notes": self.notes}


# ---------------------------------------------------------------------------------------------------------------------
# The branch's diff. Names come from `-z` output, so a path with a space, a tab, a quote or non-ASCII letters is the path
# itself — git's quoted form ("caf\303\251.py") would match no pattern. A diff git cannot compute raises RuntimeError: the
# gate turns that into a reason for a person, never into "nothing changed".
class Change(NamedTuple):
    status: str      # A M D T, or R: renamed (`path` is the new name, `old` the name at the merge base)
    path: str
    old: str = ""


_memo = threading.local()       # within one run_gate: the answers about refs that do not move while it runs


def _cached(key: Tuple[Any, ...], fn: Callable[[], T]) -> T:
    cache = getattr(_memo, "cache", None)
    if cache is None:
        return fn()
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def _fail(git: Git, what: str, r: Any) -> RuntimeError:
    why = (getattr(git, "last_error", "") or tail(r.err, 1) or tail(r.out, 1) or "exit %s" % r.rc).strip()
    return RuntimeError("%s: %s" % (what, why))


def _merge_base(git: Git, base: str, head: str) -> str:
    if not base or not head:
        raise RuntimeError("git merge-base: no %s to compare" % ("base" if not base else "head"))

    def ask() -> str:
        r = git("merge-base", base, head)
        if not r.ok or not r.out.strip():
            raise _fail(git, "git merge-base %s %s failed" % (base, head), r)
        return r.out.strip()

    return _cached(("merge-base", git.root, base, head), ask)


def changes(git: Git, base: str, head: str) -> List[Change]:
    """What the branch changed since it left `base`, a rename kept as a rename. Raises RuntimeError when git cannot say."""
    return _cached(("changes", git.root, base, head), lambda: _changes(git, base, head))


def _changes(git: Git, base: str, head: str) -> List[Change]:
    _merge_base(git, base, head)
    r = git("diff", "--name-status", "-z", "-M", "--no-color", "--no-ext-diff", base + "..." + head)
    if not r.ok:
        raise _fail(git, "git diff %s...%s failed" % (base, head), r)
    tokens = r.out.split("\0")
    out: List[Change] = []
    i = 0
    while i < len(tokens):
        st = tokens[i]
        i += 1
        if not st:
            continue
        kind = st[:1]
        n = 2 if kind in ("R", "C") else 1
        if i + n > len(tokens):
            raise RuntimeError("git diff %s...%s printed a truncated entry" % (base, head))
        if kind == "R":
            out.append(Change("R", tokens[i + 1], tokens[i]))
        elif kind == "C":
            out.append(Change("A", tokens[i + 1]))
        else:
            out.append(Change(kind, tokens[i]))
        i += n
    return out


def flat_changes(git: Git, base: str, head: str) -> List[Tuple[str, str]]:
    """`changes` as (status, path) pairs — a rename as the new path added and the old one deleted."""
    out: List[Tuple[str, str]] = []
    for c in changes(git, base, head):
        if c.status == "R":
            out += [("A", c.path), ("D", c.old)]
        else:
            out.append((c.status, c.path))
    return out


def _diff(git: Git, base: str, head: str, paths: List[str]) -> str:
    """The -U0 diff of `paths` (literally — a `[` or `*` in a name is just a character), renames detected."""
    r = git("--literal-pathspecs", "diff", "-U0", "-M", "--no-color", "--no-ext-diff", "--no-textconv", base + "..." + head,
            "--", *paths)
    if not r.ok:
        raise _fail(git, "git diff of %s failed" % ", ".join(paths), r)
    return r.out


# ---------------------------------------------------------------------------------------------------------------------
def is_test_path(cfg: Config, path: str) -> bool:
    for pat in cfg.get("protect.test_paths", ["auto"]) or []:
        if pat == "auto":
            if path.startswith(cfg.rel("work") + "/"):
                continue
            if TEST_DIR_RE.search(path) or TEST_FILE_RE.search(path) or re.search(r"(^|/)spec/", path) \
                    or TEST_CONFIG_RE.search(path):
                return True
        elif any_glob([pat], path):
            return True
    return False


def _is_comment(body: str) -> bool:
    """A line that only says something: `# assert x` is no assertion and `# self.skipTest()` no skip. (`#[test]` is a Rust
    attribute, not a comment.)"""
    s = body.strip()
    return s.startswith(("//", "/*", "*")) or (s.startswith("#") and not s.startswith("#["))


class _Counts:
    def __init__(self) -> None:
        self.removed_defs = self.added_defs = self.removed_asserts = self.added_asserts = 0
        self.skips: List[str] = []


def _count(path: str, diff: str) -> _Counts:
    """Test cases, assertions and skips on the +/- lines of a -U0 diff. Only hunks count, so a content line that begins `++`
    or `--` is not taken for a file header."""
    c = _Counts()
    rust = path.endswith(".rs")
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk or not line or line[0] not in "+-":
            continue
        body = line[1:]
        if _is_comment(body):
            continue
        is_def = int(bool((RUST_TEST_RE if rust else TEST_DEF_RE).search(body)))
        asserts = len(ASSERT_RE.findall(body))
        if line[0] == "-":
            c.removed_defs += is_def
            c.removed_asserts += asserts
        else:
            c.added_defs += is_def
            c.added_asserts += asserts
            if SKIP_RE.search(body):
                c.skips.append(body.strip()[:80])
    return c


def test_integrity(cfg: Config, git: Git, base: str, head: str) -> List[str]:
    """A test file deleted, a case or an assertion removed from one, a skip/only added to one (or to a new one). A test file
    renamed to another test file is a modification: its cases and assertions are counted like any other file's."""
    issues: List[str] = []
    todo: List[Tuple[str, bool, List[str]]] = []       # (path to report, existed at the merge base, paths for the diff)
    for c in changes(git, base, head):
        if c.status == "D" and is_test_path(cfg, c.path):
            issues.append("test file deleted: %s" % c.path)
        elif c.status == "R" and is_test_path(cfg, c.old):
            if is_test_path(cfg, c.path):
                todo.append((c.path, True, [c.old, c.path]))
            else:
                issues.append("test file deleted: %s (renamed to %s, which is not a test file)" % (c.old, c.path))
        elif c.status in ("M", "T") and is_test_path(cfg, c.path):
            todo.append((c.path, True, [c.path]))
        elif c.status in ("A", "R") and is_test_path(cfg, c.path):
            todo.append((c.path, False, [c.path]))
    for path, existing, paths in todo:
        n = _count(path, _diff(git, base, head, paths))
        if existing and n.removed_defs > n.added_defs:
            issues.append("%d test case(s) removed from %s" % (n.removed_defs - n.added_defs, path))
        if existing and n.removed_asserts > n.added_asserts:
            issues.append("%d assertion(s) removed from %s" % (n.removed_asserts - n.added_asserts, path))
        for s in n.skips[:3]:
            issues.append("skip/only added in %s: %s" % (path, s))
    return issues


# ---------- the design's decisions: acceptance criteria and test_changes ----------
def _section(text: Optional[str], name: str) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", WorkDoc.parse(text).section(name)).strip()


NO_DESIGN = "no design commit on the branch — the frozen criteria cannot be checked"


def design_text(cfg: Config, git: Git, slug: str, base: str, head: str, design_sha: str = "") -> Optional[str]:
    """The work doc as the design decided it. When the loop recorded the commit where the design phase ended (`design_sha`)
    and that commit is still an ancestor of head, it is the doc there. Otherwise the earliest point where the doc says
    `planned` — the merge base (an item planned before the branch was cut), then the branch's own commits, oldest first.
    Only `planned` counts: a doc that first says `built` is a doc somebody rewrote, not one that was designed. With no
    `planned` point at all (a design session that went straight to `built`, or a rewritten history) it is the doc as it
    was queued, at the merge base: the criteria and `test_changes` must still be the queued ones. None when the doc did
    not exist there either (an interactive branch whose history lost its design commit)."""
    return _cached(("design", git.root, slug, base, head, design_sha), lambda: _design_text(cfg, git, slug, base, head, design_sha))


def _design_text(cfg: Config, git: Git, slug: str, base: str, head: str, design_sha: str) -> Optional[str]:
    rel = "%s/%s.md" % (cfg.rel("work"), slug)
    if design_sha and git.head(design_sha) and head and git.is_ancestor(design_sha, head):
        text = git.show(design_sha, rel)
        if text is not None:
            return text
    start = git.show(git.out("merge-base", base, head) or base, rel)
    if start and WorkDoc.parse(start).status == "planned":
        return start
    for sha in git.out("log", "--format=%H", "--reverse", "%s..%s" % (base, head), "--", rel).split():
        text = git.show(sha, rel)
        if text and WorkDoc.parse(text).status == "planned":
            return text
    return start


def acceptance_changed(cfg: Config, git: Git, slug: str, base: str, head: str, *, design_sha: str = "") -> Optional[str]:
    """Acceptance criteria at the design commit vs head. With no design commit to compare with there is nothing to hold the
    criteria to — that goes to a person too."""
    design = design_text(cfg, git, slug, base, head, design_sha)
    if design is None:
        return NO_DESIGN
    rel = "%s/%s.md" % (cfg.rel("work"), slug)
    before, after = _section(design, "Acceptance criteria"), _section(git.show(head, rel), "Acceptance criteria")
    if before and before != after:
        return "acceptance criteria changed after the design commit (work/%s.md)" % slug
    return None


def test_changes_of(text: Optional[str]) -> str:
    """`allowed` or `none` — anything but exactly `allowed` leaves existing tests locked, as the hooks read it too."""
    return "allowed" if text and str(WorkDoc.parse(text).meta.get("test_changes", "")).strip() == "allowed" else "none"


def test_changes_changed(cfg: Config, git: Git, slug: str, base: str, head: str, *, design_sha: str = "") -> Optional[str]:
    """`test_changes` at the design commit vs head: like the criteria it is the design's decision, so a session cannot
    unlock existing tests for itself. (No design commit is reported by `acceptance_changed`.)"""
    design = design_text(cfg, git, slug, base, head, design_sha)
    if design is None:
        return None
    before = test_changes_of(design)
    after = test_changes_of(git.show(head, "%s/%s.md" % (cfg.rel("work"), slug)))
    if before != after:
        return "test_changes changed after the design commit: %s → %s (work/%s.md)" % (before, after, slug)
    return None


def existing_tests_changed(cfg: Config, git: Git, slug: str, base: str, head: str, *, design_sha: str = "") -> Optional[str]:
    """Existing test files (present where the branch left the default branch) that the branch modified, deleted or renamed
    away, when the design did not allow that. Test files the branch adds are free. Without a design commit the value is
    `none` — never what head says."""
    frozen = test_changes_of(design_text(cfg, git, slug, base, head, design_sha))
    if frozen == "allowed" or not cfg.get("protect.protect_tests", True):
        return None
    kinds = {"M": "modified", "T": "modified", "D": "deleted or renamed"}
    files = ["%s (%s)" % (path, kinds[st]) for st, path in flat_changes(git, base, head) if st in kinds and is_test_path(cfg, path)]
    if not files:
        return None
    return "existing tests changed, but the design said test_changes: none — " + _list(files, 8)


def _list(items: List[str], cap: int) -> str:
    return ", ".join(items[:cap]) + (" … (+%d more)" % (len(items) - cap) if len(items) > cap else "")


# ---------- the test runner's own configuration ----------
def _toml(text: str) -> List[Tuple[Tuple[str, ...], Tuple[str, ...], str]]:
    """(section, key, value) for the `key = value` pairs of a TOML document, and (section, (), "") for each `[header]`. The
    value is as written — a multi-line array, an inline table or a multi-line string joined into one, comments dropped. Not
    a validator: enough to tell a dependency array from a list of classifiers."""
    key_part = r"(?:[A-Za-z0-9_\-]+|\"[^\"]*\"|'[^']*')"
    key_re = re.compile(r"^(%s(?:\s*\.\s*%s)*)\s*=\s*(.*)$" % (key_part, key_part))
    out: List[Tuple[Tuple[str, ...], Tuple[str, ...], str]] = []
    section: Tuple[str, ...] = ()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        i += 1
        if not s or s.startswith("#"):
            continue
        if s.startswith("["):
            m = re.match(r"^\[\[?\s*(.+?)\s*\]\]?\s*(?:#.*)?$", s)
            if m:
                section = tuple(_split_key(m.group(1)))
                out.append((section, (), ""))
            continue
        m = key_re.match(s)
        if not m:
            continue
        value, i = _toml_value(m.group(2), lines, i)
        out.append((section, tuple(_split_key(m.group(1))), value))
    return out


def _split_key(s: str) -> List[str]:
    """`a."b.c".d` → ['a', 'b.c', 'd']"""
    parts: List[str] = []
    cur, quote = "", ""
    for ch in s:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return parts


def _toml_value(first: str, lines: List[str], i: int) -> Tuple[str, int]:
    """A value that starts on `first` and goes on over the following lines while a bracket, a brace or a string is open."""
    parts: List[str] = []
    depth, quote = 0, ""
    chunk = first
    while True:
        out: List[str] = []
        j = 0
        while j < len(chunk):
            ch = chunk[j]
            if quote:
                if quote in ('"""', "'''"):
                    if chunk.startswith(quote, j):
                        out.append(quote)
                        j += 3
                        quote = ""
                        continue
                elif ch == "\\" and quote == '"':
                    out.append(chunk[j:j + 2])
                    j += 2
                    continue
                elif ch == quote:
                    quote = ""
                out.append(ch)
                j += 1
                continue
            if chunk.startswith('"""', j) or chunk.startswith("'''", j):
                quote = chunk[j:j + 3]
                out.append(quote)
                j += 3
                continue
            if ch in "\"'":
                quote = ch
            elif ch == "#":
                break
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            out.append(ch)
            j += 1
        parts.append("".join(out).strip())
        if (depth <= 0 and not quote) or i >= len(lines):
            return " ".join(parts).strip(), i
        chunk = lines[i]
        i += 1


def _pkg_scripts(text: Optional[str]) -> Optional[Tuple[str, ...]]:
    """What `npm test` runs: the `test` script and the `pretest` and `posttest` that npm runs around it. None for text that
    is not JSON."""
    try:
        d = json.loads((text or "{}").lstrip("\ufeff"))
    except ValueError:
        return None
    scripts = d.get("scripts") if isinstance(d, dict) else None
    scripts = scripts if isinstance(scripts, dict) else {}
    return tuple(str(scripts.get(k) or "").strip() for k in ("test", "pretest", "posttest"))


def _compact(value: str) -> str:
    """A TOML value without the whitespace outside its strings and without a trailing comma, so that reformatting an
    array is not a change of it."""
    out: List[str] = []
    quote = ""
    for ch in value:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif not ch.isspace():
            out.append(ch)
    return re.sub(r",([\]}])", r"\1", "".join(out))


def _pytest_table(text: Optional[str]) -> Dict[Tuple[str, str], str]:
    """The pytest settings of a pyproject.toml: every entry of `[tool.pytest.ini_options]` (and `[tool.pytest]`)."""
    out: Dict[Tuple[str, str], str] = {}
    for section, key, value in _toml((text or "").lstrip("\ufeff")):
        dotted = ".".join(section)
        if dotted == "tool.pytest" or dotted.startswith("tool.pytest."):
            out[(dotted, ".".join(key))] = _compact(value)
    return out


def test_runner_config_changed(cfg: Config, git: Git, base: str, head: str) -> List[str]:
    """A change to what `npm test` runs (`scripts.test`, `pretest`, `posttest` of a package.json) or to the pytest settings
    of a pyproject.toml. Files the branch adds are free, as new test files are; `test_changes: allowed` is about the tests,
    not about switching the runner off, so it does not lift this."""
    mb = _merge_base(git, base, head)
    out: List[str] = []
    for c in changes(git, base, head):
        src = c.old if c.status == "R" else c.path
        name = src.rsplit("/", 1)[-1]
        if c.status not in ("M", "T", "D", "R") or name not in ("package.json", "pyproject.toml"):
            continue
        before = git.show(mb, src)
        after = None if c.status == "D" else git.show(head, c.path)
        if name == "package.json":
            changed = _pkg_scripts(before) != _pkg_scripts(after)
        else:
            changed = _pytest_table(before) != _pytest_table(after)
        if changed:
            out.append("test runner configuration changed: %s" % c.path)
    return out


# ---------- guard files ----------
def _guard_rel(cfg: Config, path: str) -> str:
    """A configured path as git names it: relative to the repository root, no `./`."""
    p = os.path.normpath(path)
    if os.path.isabs(p) and p.startswith(cfg.root.rstrip(os.sep) + os.sep):
        p = os.path.relpath(p, cfg.root)
    return p


def _json_object(text: Optional[str]) -> Optional[dict]:
    try:
        data = json.loads(text) if text is not None else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _unset(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def _same(a: Any, b: Any) -> bool:
    """Equal as JSON: Python alone would call `false` and `0`, or `true` and `1`, the same."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def only_fills_commands(old_text: Optional[str], new_text: Optional[str]) -> bool:
    """`.sdlc/config.json` changed in one way only: commands that had no value got one. Every difference is a
    `commands.<name>` whose old value is missing, null or an empty string and whose new value is a non-empty string — a new
    project's skeleton recording how to build and test it. Any name may be filled: one that nothing refers to does nothing,
    and one that is referred to (`format`, `lint_file`, a name in `gate.commands`) adds a check. Changing or removing a
    command that has a value, any other key, or a file that is no longer a JSON object is not that: those loosen a
    guardrail."""
    old, new = _json_object(old_text), _json_object(new_text)
    if old is None or new is None:
        return False
    old_cmds, new_cmds = old.get("commands"), new.get("commands")
    if any(c is not None and not isinstance(c, dict) for c in (old_cmds, new_cmds)):
        return False
    if not _same({k: v for k, v in old.items() if k != "commands"}, {k: v for k, v in new.items() if k != "commands"}):
        return False
    old_cmds, new_cmds = old_cmds or {}, new_cmds or {}
    for name in set(old_cmds) | set(new_cmds):
        before, after = old_cmds.get(name), new_cmds.get(name)
        if _same(before, after) or (_unset(before) and _unset(after)):
            continue
        if not (_unset(before) and isinstance(after, str) and after.strip()):
            return False
    return True


def config_fill(git: Git, base: str, head: str) -> Optional[Dict[str, str]]:
    """The commands a branch's `.sdlc/config.json` fills in, when filling them is all it changes — `only_fills_commands`,
    judged against where the branch left the default branch, not against the default branch now. None when the file differs
    in any other way. The guard lets such a change through and the gate runs what it fills."""
    start = git.out("merge-base", base, head) or base
    old_text, new_text = git.show(start, ".sdlc/config.json"), git.show(head, ".sdlc/config.json")
    if not only_fills_commands(old_text, new_text):
        return None
    old = (_json_object(old_text) or {}).get("commands") or {}
    new = (_json_object(new_text) or {}).get("commands") or {}
    return {name: value for name, value in new.items() if _unset(old.get(name)) and isinstance(value, str) and value.strip()}


def guard_files_changed(cfg: Config, git: Git, base: str, head: str) -> Optional[str]:
    """The files that keep a session in bounds — `.sdlc/config.json`, the frozen-paths file, the Claude Code settings and
    the gate's own checks directory. A change to any of them on the branch waits for a person: a session must not loosen
    its own guardrails and have that merged unattended. The one change let through is a new project's skeleton filling in
    `commands.*` that were still empty (`only_fills_commands`): that adds checks, it removes none. Work docs and
    `.sdlc/state` are not guard files."""
    files = {".sdlc/config.json", ".claude/settings.json", ".claude/settings.local.json"}
    frozen = str(cfg.get("protect.frozen_paths_file", "") or "")
    if frozen:
        files.add(_guard_rel(cfg, frozen))
    checks = cfg.rel("checks") + "/"
    kinds = {"A": "added", "D": "deleted or renamed"}
    hits = []
    for st, path in sorted(flat_changes(git, base, head), key=lambda c: c[1]):
        if path not in files and not path.startswith(checks):
            continue
        if path == ".sdlc/config.json" and st == "M" and config_fill(git, base, head) is not None:
            continue
        hits.append("%s (%s)" % (path, kinds.get(st, "modified")))
    if not hits:
        return None
    return "guard files changed, a person merges these — " + _list(hits, 8)


# ---------- frozen and generated paths ----------
def protected_lists(cfg: Config, git: Git, base: str, head: str) -> Tuple[List[str], List[str]]:
    """(generated globs, frozen globs) as they were where the branch left the default branch — a branch that empties a list
    and edits what it listed in the same go gains nothing. A merge base without a readable config falls back to the
    config in force."""
    mb = _merge_base(git, base, head)
    raw = _json_object(git.show(mb, ".sdlc/config.json"))
    if raw is None:
        generated = cfg.get("protect.generated_paths", []) or []
        frozen_file = str(cfg.get("protect.frozen_paths_file", "") or "")
    else:
        protect = raw.get("protect") if isinstance(raw.get("protect"), dict) else {}
        generated = protect.get("generated_paths") if isinstance(protect.get("generated_paths"), list) else []
        frozen_file = str(protect.get("frozen_paths_file") or DEFAULTS["protect"]["frozen_paths_file"])
    text = None
    if frozen_file:
        rel = _guard_rel(cfg, frozen_file)
        text = git.show(mb, rel) if not os.path.isabs(rel) else read_text(rel)
    frozen = [line.split("#", 1)[0].strip() for line in (text or "").splitlines()]
    return [str(g).strip() for g in generated if str(g).strip()], [f for f in frozen if f]


def protected_paths_changed(cfg: Config, git: Git, base: str, head: str) -> List[str]:
    """Changed files that match `protect.generated_paths` or a line of the frozen-paths file. The edit hook stops a session
    that edits them with its own tools; this is what is left when something else wrote them — a generator, `git apply`,
    `python -c`."""
    generated, frozen = protected_lists(cfg, git, base, head)
    gen_hits: List[str] = []
    frozen_hits: List[str] = []
    for c in changes(git, base, head):
        for path in dict.fromkeys([c.path] + ([c.old] if c.old else [])):
            pat = any_glob(frozen, path)
            if pat:
                frozen_hits.append("%s (%s)" % (path, pat))
            pat = any_glob(generated, path)
            if pat:
                gen_hits.append("%s (%s)" % (path, pat))
    out = []
    if frozen_hits:
        out.append("frozen path changed: " + _list(frozen_hits, 5))
    if gen_hits:
        out.append("generated path changed: " + _list(gen_hits, 5))
    return out


# ---------- dependencies ----------
MANIFESTS = ("package.json", "composer.json", "pyproject.toml", "Pipfile", "setup.py", "go.mod", "Cargo.toml", "Gemfile",
             "build.gradle", "build.gradle.kts", "libs.versions.toml", "pom.xml", "Package.swift", "Podfile", "pubspec.yaml")
REQUIREMENTS_RE = re.compile(r"requirements[^/]*\.(?:txt|in)$")


def is_manifest(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base in MANIFESTS or bool(REQUIREMENTS_RE.search(base)) \
        or (base.endswith((".txt", ".in")) and "requirements" in path.split("/")[:-1])


def _norm(name: str) -> str:
    """A Python distribution name the way pip compares it: `Foo_Bar` and `foo.bar` are `foo-bar`."""
    return re.sub(r"[-_.]+", "-", name).lower()


# A requirement: a name, extras, then a version, a marker, `@ url` or nothing — prose in a file that happens to be called
# requirements.txt is not one.
REQ_LINE_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*(?:$|[<>=!~;@(,]|--)")


def _req_name(requirement: str) -> Optional[str]:
    m = REQ_LINE_RE.match(requirement.strip())
    return _norm(m.group(1)) if m else None


def _url_dep(url: str) -> str:
    """The key of a requirement given as a URL: the `#egg=` name, else the repository, else the file's name without its
    version — so that a bump of the pinned file is no new dependency."""
    egg = re.search(r"[#&]egg=([A-Za-z0-9._\-]+)", url)
    if egg:
        return _norm(egg.group(1))
    u = re.split(r"[#?]", url)[0]
    if u.startswith("git+") or u.endswith(".git") or ".git@" in u:
        return re.sub(r"@[^/@]*$", "", u)
    m = re.match(r"^([A-Za-z0-9_.]+?)-\d", u.rsplit("/", 1)[-1])
    return _norm(m.group(1)) if m else u


def _requirements_deps(text: str) -> Set[str]:
    out: Set[str] = set()
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        m = re.match(r"^(?:-e|--editable)[=\s]\s*(.+)$", line)
        if m:
            target = m.group(1).split()[0]
            if "://" in target or target.startswith("git+"):
                out.add(_url_dep(target))
            continue                                    # `-e .` and `-e ../pkg` are this repository's own code
        if line.startswith("-"):
            continue                                    # -r, -c, --hash, --index-url …
        first = line.split()[0]
        if "://" in first or first.startswith("git+"):
            out.add(_url_dep(first))
            continue
        name = _req_name(line)
        if name:
            out.add(name)
    return out


def _strings(text: str) -> List[str]:
    return [a or b for a, b in re.findall(r"\"((?:[^\"\\]|\\.)*)\"|'([^']*)'", text)]


def _bracket_body(text: str, i: int) -> str:
    """`text[i]` opens a bracket: everything up to its partner, strings kept and `#` comments dropped."""
    depth, quote, j = 0, "", i
    out: List[str] = []
    while j < len(text):
        ch = text[j]
        if quote:
            out.append(ch)
            if ch == "\\":
                out.append(text[j + 1:j + 2])
                j += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            while j < len(text) and text[j] != "\n":
                j += 1
            continue
        elif ch in "[{(":
            depth += 1
            out.append(ch)
        elif ch in "]})":
            depth -= 1
            out.append(ch)
            if depth <= 0:
                break
        else:
            out.append(ch)
        j += 1
    return "".join(out)


def _json_deps(text: str) -> Set[str]:
    try:
        d = json.loads(text)
    except ValueError:
        return set()
    out: Set[str] = set()
    if not isinstance(d, dict):
        return out
    for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "require", "require-dev"):
        if isinstance(d.get(k), dict):
            out.update(d[k].keys())
    for k in ("bundleDependencies", "bundledDependencies"):
        if isinstance(d.get(k), list):
            out.update(str(x) for x in d[k])
    return out


PYPROJECT_ARRAY_SECTIONS = ("project.optional-dependencies", "dependency-groups", "tool.pdm.dev-dependencies")
PYPROJECT_ARRAY_KEYS = ("dependencies", "dev-dependencies", "requires", "optional-dependencies")


def _pyproject_deps(text: str) -> Set[str]:
    """Only what declares dependencies: the arrays under `dependencies`, `requires`, `optional-dependencies` and
    `dependency-groups`, and the tables of Poetry and its relatives. A list of classifiers or ruff rules is not one."""
    out: Set[str] = set()

    def add_requirements(value: str) -> None:
        for s in _strings(re.sub(r"\{[^{}]*\}", "", value)):      # `{include-group = "test"}` names a group, not a package
            name = _req_name(s)
            if name:
                out.add(name)

    for section, key, value in _toml(text):
        dotted = ".".join(section)
        if not key:                                                         # [tool.poetry.dependencies.requests]
            if len(section) >= 2 and dotted.startswith("tool.") and section[-2].endswith("dependencies") \
                    and section[-1] != "python":
                out.add(_norm(section[-1]))
        elif dotted in PYPROJECT_ARRAY_SECTIONS:                            # group = ["a", "b"]
            add_requirements(value)
        elif key[0] in PYPROJECT_ARRAY_KEYS and (value[:1] == "[" or (key[0] == "optional-dependencies" and value[:1] == "{")):
            add_requirements(value)                                         # dependencies = [...], requires = [...]
        elif dotted.startswith("tool.") and section[-1].endswith("dependencies") and key[0] != "python":
            out.add(_norm(key[0]))                                          # poetry: name = "^1" or name = { ... }
    return out


def _setup_py_deps(text: str) -> Set[str]:
    out: Set[str] = set()
    for key in ("install_requires", "setup_requires", "tests_require", "extras_require"):
        for m in re.finditer(r"\b%s\s*=\s*" % key, text):
            if m.end() >= len(text) or text[m.end()] not in "[{(":
                continue
            body = _bracket_body(text, m.end())
            parts = re.findall(r"\[([^\[\]]*)\]", body) if key == "extras_require" else [body]
            for part in parts:
                for s in _strings(part):
                    name = _req_name(s)
                    if name:
                        out.add(name)
    return out


def _cargo_deps(text: str) -> Set[str]:
    out: Set[str] = set()
    tables: Dict[Tuple[str, ...], Dict[str, Any]] = {}          # [dependencies.tokio] and the like
    for section, key, value in _toml(text):
        if not section:
            continue
        if section[-1].endswith("dependencies") and key:
            if len(key) > 1 and key[-1] == "workspace" and value == "true":
                continue
            if re.search(r"\bworkspace\s*=\s*true\b", value):       # version and source live in [workspace.dependencies]
                continue
            out.add(key[0])
            pkg = re.search(r"\bpackage\s*=\s*[\"']([^\"']+)", value)
            if pkg:
                out.add(pkg.group(1))
        elif len(section) >= 2 and section[-2].endswith("dependencies"):
            t = tables.setdefault(section, {"workspace": False, "package": ""})
            if key == ("workspace",) and value == "true":
                t["workspace"] = True
            if key == ("package",):
                t["package"] = value.strip("\"'")
    for section, t in tables.items():
        if not t["workspace"]:
            out.add(section[-1])
            if t["package"]:
                out.add(t["package"])
    return out


def _gomod_deps(text: str, indirect: bool) -> Set[str]:
    """The modules `go.mod` asks for: `require` lines and blocks, and the module a `replace` points at. `// indirect` lines
    are the toolchain's bookkeeping about what a dependency needs — left out, unless `indirect` (the "what was already
    there" side of a comparison: a module that was only indirect and is now imported directly is not new)."""
    out: Set[str] = set()
    block = ""
    for raw in text.splitlines():
        code, _, comment = raw.partition("//")
        line = code.strip()
        if not line:
            continue
        if block:
            if line == ")":
                block = ""
                continue
            verb, toks = block, line.split()
        else:
            m = re.match(r"^(require|replace|exclude|retract|module|go|toolchain|tool|godebug)\b\s*(\(?)\s*(.*)$", line)
            if not m:
                continue
            verb, opener, rest = m.groups()
            if opener:
                block = verb
                continue
            toks = rest.split()
        if verb == "require" and toks and (indirect or not comment.strip().startswith("indirect")):
            out.add(toks[0])
        elif verb == "replace" and "=>" in toks:
            target = toks[toks.index("=>") + 1:]
            if target and not target[0].startswith((".", "/")):
                out.add(target[0])
    return out


_GRADLE_CONF = (r"(?<![\w.])(?:\w*(?:[Ii]mplementation|[Aa]pi|[Cc]ompileOnly|[Rr]untimeOnly|[Cc]ompile|[Rr]untime|[Pp]rovided)|"
                r"kapt\w*|ksp\w*|\w*[Aa]nnotationProcessor|classpath|coreLibraryDesugaring|lintChecks|detektPlugins)")
_GRADLE_ARG = r"\s*\(?\s*(?:(?:enforcedPlatform|platform)\s*\(\s*)?"
GRADLE_COORD_RE = re.compile(_GRADLE_CONF + _GRADLE_ARG + r"[\"']([^\"':\s]+:[^\"':\s]+)")
GRADLE_MAP_RE = re.compile(_GRADLE_CONF + r"\s*\(?\s*group\s*[:=]\s*[\"']([^\"']+)[\"']\s*,\s*name\s*[:=]\s*[\"']([^\"']+)[\"']")
GRADLE_ALIAS_RE = re.compile(_GRADLE_CONF + _GRADLE_ARG + r"(libs\.[\w.]+)")
GRADLE_KOTLIN_RE = re.compile(_GRADLE_CONF + _GRADLE_ARG + r"kotlin\(\s*[\"']([\w\-]+)[\"']")


def _gradle_deps(text: str) -> Set[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    out: Set[str] = set(GRADLE_COORD_RE.findall(text))
    out.update("%s:%s" % m for m in GRADLE_MAP_RE.findall(text))
    out.update(GRADLE_ALIAS_RE.findall(text))
    out.update("kotlin:%s" % m for m in GRADLE_KOTLIN_RE.findall(text))
    return out


def _version_catalog_deps(text: str) -> Set[str]:
    out: Set[str] = set()
    tables: Dict[Tuple[str, ...], Dict[str, str]] = {}
    for section, key, value in _toml(text):
        if section == ("libraries",) and key:
            if value[:1] in ("\"", "'"):                              # alias = "group:name:version"
                parts = (_strings(value) or [""])[0].split(":")
                if len(parts) >= 2:
                    out.add("%s:%s" % (parts[0], parts[1]))
            else:                                                    # alias = { module = "g:n" } or { group = "g", name = "n" }
                module = re.search(r"\bmodule\s*=\s*\"([^\"]+)\"", value)
                group = re.search(r"\bgroup\s*=\s*\"([^\"]+)\"", value)
                name = re.search(r"\bname\s*=\s*\"([^\"]+)\"", value)
                if module:
                    out.add(module.group(1))
                elif group and name:
                    out.add("%s:%s" % (group.group(1), name.group(1)))
        elif len(section) == 2 and section[0] == "libraries" and key:   # [libraries.alias]
            tables.setdefault(section, {})[key[0]] = value.strip("\"'")
    for t in tables.values():
        if t.get("module"):
            out.add(t["module"])
        elif t.get("group") and t.get("name"):
            out.add("%s:%s" % (t["group"], t["name"]))
    return out


def _pubspec_deps(text: str) -> Set[str]:
    out: Set[str] = set()
    sect, indent = "", -1
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            m = re.match(r"^([A-Za-z_]\w*)\s*:", raw)
            sect, indent = (m.group(1) if m else ""), -1
            continue
        if sect in ("dependencies", "dev_dependencies", "dependency_overrides"):
            m = re.match(r"^(\s+)([A-Za-z0-9_]+)\s*:", raw)
            if not m:
                continue
            if indent < 0:
                indent = len(m.group(1))                              # however far the file indents: the first entry sets it
            if len(m.group(1)) == indent:
                out.add(m.group(2))
    return out


def deps_of(path: str, text: Optional[str], indirect: bool = False) -> Set[str]:
    """The dependencies a manifest names. `indirect` only matters to go.mod: it also lists the `// indirect` requirements."""
    if not text:
        return set()
    text = text.lstrip("\ufeff")
    base = path.rsplit("/", 1)[-1]
    out: Set[str] = set()
    if base in ("package.json", "composer.json"):
        out = _json_deps(text)
    elif REQUIREMENTS_RE.search(base) or (base.endswith((".txt", ".in")) and "requirements" in path.split("/")[:-1]):
        out = _requirements_deps(text)
    elif base == "pyproject.toml":
        out = _pyproject_deps(text)
    elif base == "Pipfile":
        for section, key, _ in _toml(text):
            if key and section in (("packages",), ("dev-packages",)):
                out.add(_norm(key[0]))
    elif base == "setup.py":
        out = _setup_py_deps(text)
    elif base == "go.mod":
        out = _gomod_deps(text, indirect)
    elif base == "Cargo.toml":
        out = _cargo_deps(text)
    elif base == "Gemfile":
        out.update(re.findall(r"^\s*gem\s+['\"]([^'\"]+)", text, re.M))
    elif base.startswith("build.gradle"):
        out = _gradle_deps(text)
    elif base == "libs.versions.toml":
        out = _version_catalog_deps(text)
    elif base == "pom.xml":
        for blk in re.findall(r"<dependency>(.*?)</dependency>", re.sub(r"<!--.*?-->", "", text, flags=re.S), re.S):
            g = re.search(r"<groupId>\s*([^<\s]+)", blk)
            a = re.search(r"<artifactId>\s*([^<\s]+)", blk)
            if g and a:
                out.add("%s:%s" % (g.group(1), a.group(1)))
    elif base == "Package.swift":
        out.update(re.findall(r"\.package\(\s*(?:name:\s*\"[^\"]*\"\s*,\s*)?url:\s*\"([^\"]+)\"", text))
        out.update(re.findall(r"\.package\(\s*id:\s*\"([^\"]+)\"", text))
    elif base == "Podfile":
        out.update(re.findall(r"^\s*pod\s+['\"]([^'\"]+)", text, re.M))
    elif base == "pubspec.yaml":
        out = _pubspec_deps(text)
    return out


def added_deps(path: str, before: Optional[str], after: Optional[str], old_path: str = "") -> List[str]:
    """What the manifest `after` (at `path`) names that `before` (at `old_path`, when it was renamed) did not. A version bump
    is not new, and neither is a go module that was already required, directly or not."""
    src = old_path or path
    known = deps_of(src, before, indirect=True) if before is not None and is_manifest(src) else set()
    return sorted(deps_of(path, after) - known)


def new_dependencies(git: Git, base: str, head: str) -> List[str]:
    """Dependencies a manifest names at head that it did not name where the branch left the default branch."""
    mb = _merge_base(git, base, head)
    added: List[str] = []
    for c in changes(git, base, head):
        if c.status not in ("A", "M", "R") or not is_manifest(c.path):
            continue
        src = c.old if c.status == "R" else c.path
        before = None if c.status == "A" else git.show(mb, src)
        for d in added_deps(c.path, before, git.show(head, c.path), c.old):
            added.append("%s (%s)" % (d, c.path))
    return added


# ---------------------------------------------------------------------------------------------------------------------
def _untracked(git: Git) -> Optional[Set[str]]:
    """The untracked files that are not ignored, as git names them under `-z`; None when git cannot say."""
    r = git("ls-files", "-z", "--others", "--exclude-standard")
    return {p for p in r.out.split("\0") if p} if r.ok else None


def _restore(git: Git, head: str, before: Optional[Set[str]]) -> None:
    """Put the tree back as the gate found it: no merge in progress, the head it started from with every tracked file as
    committed (a gate command that rewrote a lockfile or ran a `--fix` linter leaves nothing behind), and no untracked file
    that was not there — and no directory those files were the last thing in."""
    git("merge", "--abort")                                  # fails harmlessly when there is no merge to abort
    if head:
        git("reset", "-q", "--hard", head)
    if before is None:
        return
    after = _untracked(git)
    if after is None:
        return
    dirs: Set[str] = set()
    for p in sorted(after - before):
        try:
            os.remove(os.path.join(git.root, p))
        except OSError:
            continue
        d = os.path.dirname(p)
        while d:
            dirs.add(d)
            d = os.path.dirname(d)
    for d in sorted(dirs, key=lambda x: x.count("/"), reverse=True):    # deepest first; a directory with anything left stays
        try:
            os.rmdir(os.path.join(git.root, d))
        except OSError:
            pass


def _guarded(res: GateResult, label: str, fn: Callable[[], T]) -> Optional[T]:
    """Run a check that needs git's diff or the manifests. If it cannot be done the answer is "a person looks" — a check that
    did not run is not a check that passed."""
    try:
        return fn()
    except RuntimeError as e:
        msg = "the gate could not compute the diff: %s" % e
    except Exception as e:                                   # a manifest the parsers choke on is a reason, not a crash
        msg = "the gate could not run the %s check: %s: %s" % (label, type(e).__name__, e)
    if msg not in res.human:
        res.human.append(msg)
    return None


def run_gate(cfg: Config, git: Git, slug: str, base: str, run_commands: bool = True, *, design_sha: str = "",
             reviewed: Optional[bool] = None) -> GateResult:
    """Precondition: the worktree is clean and on sdlc/<slug>. Leaves it on sdlc/<slug>, clean, as found — whatever the
    commands did and however the gate ended.

    `design_sha`: the commit where the loop's design phase ended; the criteria and `test_changes` are held to the doc as it
    was there (when it is still an ancestor of head). `reviewed`: whether the build session called the reviewer subagent —
    False is a fixable failure, None (unknown: an interactive ship, an item built before the loop recorded it) checks nothing."""
    res = GateResult()
    head = git.head()
    before_untracked = _untracked(git)
    _memo.cache = {}
    try:
        _run_gate(cfg, git, slug, base, run_commands, res, head, design_sha, reviewed)
    finally:
        _memo.cache = None
        _restore(git, head, before_untracked)
    return res


def _run_gate(cfg: Config, git: Git, slug: str, base: str, run_commands: bool, res: GateResult, head: str, design_sha: str,
              reviewed: Optional[bool]) -> None:
    doc_rel = "%s/%s.md" % (cfg.rel("work"), slug)

    # 0. merge the default branch in (not committed) — the tests run against what the merge will produce
    if base and git.head(base) and not git.is_ancestor(base, "HEAD"):
        r = git("merge", "--no-commit", "--no-ff", base, timeout=300)
        if not r.ok:
            res.conflict = True
            return

    # 1. commands
    if run_commands:
        mins = float(cfg.get("gate.timeout_minutes", 30) or 30)
        # a branch that only fills in commands the project had none of — a new project's skeleton — is gated by them;
        # otherwise (and for every command that has a value) the gate runs the commands in force
        fills = config_fill(git, base, head) or {}
        commands = cfg.with_filled_commands(fills).gate_commands()
        res.filled = [name for name, cmd in commands if cmd == str(fills.get(name) or "").strip()]
        for name, cmd in commands:
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

    # 2. review tally (recorded in the work doc by the build session), and that the review ran in a fresh context
    if cfg.get("gate.review", True):
        doc = WorkDoc.parse(git.show(head, doc_rel) or "")
        tally = doc.review_tally()
        if tally is None:
            res.fixable.append("no review tally in work/%s.md (## Review → SDLC_REVIEW_TALLY important=… nits=…)" % slug)
        elif tally[0] > 0:
            res.fixable.append("review: %d Important finding(s) open" % tally[0])
        if reviewed is False:
            res.fixable.append("the review did not run in a fresh context (no sdlc-reviewer subagent call in the build "
                               "session) — run references/review.md steps 2–4 and record the tally")

    # 3. test integrity: existing tests untouched unless the design allowed it; none deleted, weakened or skipped either way;
    #    the test runner's own configuration not changed
    if cfg.get("gate.test_integrity", True):
        t = _guarded(res, "test integrity", lambda: existing_tests_changed(cfg, git, slug, base, head, design_sha=design_sha))
        if t:
            res.human.append(t)
        issues = _guarded(res, "test integrity", lambda: test_integrity(cfg, git, base, head))
        res.human += ["test integrity: " + i for i in issues or []]
        res.human += _guarded(res, "test runner", lambda: test_runner_config_changed(cfg, git, base, head)) or []
    # 4. the design's decisions frozen: acceptance criteria and test_changes
    if cfg.get("gate.acceptance_frozen", True):
        res.human += [x for x in (acceptance_changed(cfg, git, slug, base, head, design_sha=design_sha),
                                  test_changes_changed(cfg, git, slug, base, head, design_sha=design_sha)) if x]
    # 5. new dependencies
    if cfg.get("gate.new_dependencies", "human") != "allow":
        res.human += ["new dependency: " + d for d in _guarded(res, "dependency", lambda: new_dependencies(git, base, head)) or []]
    # 6. frozen and generated paths, risk paths — and the guard files, whatever risk.human says
    res.human += _guarded(res, "protected paths", lambda: protected_paths_changed(cfg, git, base, head)) or []
    risky = cfg.get("risk.human", []) or []
    if risky:
        hits = []
        for st, path in _guarded(res, "risk path", lambda: flat_changes(git, base, head)) or []:
            pat = any_glob(risky, path)
            if pat:
                hits.append("%s (%s)" % (path, pat))
        if hits:
            res.human.append("risk path: " + ", ".join(hits[:5]) + (" …" if len(hits) > 5 else ""))
    g = _guarded(res, "guard files", lambda: guard_files_changed(cfg, git, base, head))
    if g:
        res.human.append(g)
