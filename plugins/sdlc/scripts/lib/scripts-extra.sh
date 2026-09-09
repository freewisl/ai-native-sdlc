#!/bin/bash
# sdlc plugin — helpers shared by scripts/*.sh (init, doctor, status, run-evals).
# Source AFTER common.sh:  . "$(dirname "$0")/lib/common.sh"; . "$(dirname "$0")/lib/scripts-extra.sh"
# bash 3.2 compatible. python3 (standard library) is used for JSON rendering/merging; jq is optional.

SDLC_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"          # <plugin>/scripts
SDLC_PLUGIN_ROOT="$(cd "$SDLC_SCRIPTS_DIR/.." && pwd)"                       # <plugin>
SDLC_TEMPLATES="$SDLC_PLUGIN_ROOT/templates"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf 'NOTE  %s\n' "$*"; }

# resolve_root [dir]  → absolute project root: --dir, else $CLAUDE_PROJECT_DIR, else git toplevel of cwd, else cwd
resolve_root() {
  local d="${1:-}"
  if [ -n "$d" ]; then
    [ -d "$d" ] || die "--dir '$d' is not a directory"
    (cd "$d" && pwd)
    return
  fi
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "$CLAUDE_PROJECT_DIR" ]; then printf '%s' "$CLAUDE_PROJECT_DIR"; return; fi
  local top; top=$(git rev-parse --show-toplevel 2>/dev/null || true)
  printf '%s' "${top:-$(pwd)}"
}

is_git_repo() { git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1; }

# git_remote_host <root> → host of the origin remote (git@host:…, ssh://…, https://…); github.com when there is none
git_remote_host() {
  local u; u=$(git -C "$1" remote get-url origin 2>/dev/null)
  case "$u" in
    git@*:*) u="${u#git@}"; printf '%s' "${u%%:*}";;
    ssh://*|https://*|http://*) u="${u#*://}"; u="${u#*@}"; u="${u%%/*}"; printf '%s' "${u%%:*}";;
    *) printf 'github.com';;
  esac
}
# gh_ready <root> → 0 when gh is installed and logged in to THIS repository's host. Scoped on purpose: `gh auth status` alone
# fails when any other configured host (a company GitHub Enterprise, say) is expired or unreachable, which is a false negative
# for people who use both. Also exports GH_HOST so every later gh api/pr call targets the same host.
gh_ready() {
  has_cmd gh || return 1
  local h; h=$(git_remote_host "$1"); export GH_HOST="$h"
  gh auth status --hostname "$h" >/dev/null 2>&1
}

# json_valid <file>  → 0 when the file parses as JSON
json_valid() {
  [ -f "$1" ] || return 1
  if has_cmd jq; then jq -e . "$1" >/dev/null 2>&1
  else python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$1" >/dev/null 2>&1; fi
}

# py_render <template-file> <dest-file|-> [key=value ...]
#   Replaces every {{key}} with value. Keys not given are left untouched (skills fill {{slug}} etc. later).
py_render() {
  local src="$1" dest="$2"; shift 2
  python3 - "$src" "$dest" "$@" <<'PY'
import sys
src, dest = sys.argv[1], sys.argv[2]
text = open(src, encoding='utf-8').read()
for kv in sys.argv[3:]:
    k, _, v = kv.partition('=')
    text = text.replace('{{' + k + '}}', v)
if dest == '-':
    sys.stdout.write(text)
else:
    with open(dest, 'w', encoding='utf-8') as fh:
        fh.write(text)
PY
}

# frontmatter_value <file> <key>  (quotes stripped)  — alias of common.sh frontmatter_get for readability
frontmatter_value() { frontmatter_get "$1" "$2"; }

# artifact_slugs <dir> → slugs of *.md in dir excluding README.md / TEMPLATE.md (top level only)
artifact_slugs() {
  local d="$1" f b
  [ -d "$d" ] || return 0
  for f in "$d"/*.md; do
    [ -f "$f" ] || continue
    b=$(basename "$f" .md)
    case "$b" in README|TEMPLATE) continue;; esac
    printf '%s\n' "$b"
  done
}

# claude_md_section_present <file> <section-id>  → 0 when marker or heading exists (case-insensitive)
claude_md_section_present() {
  local f="$1" s="$2" re=""
  [ -f "$f" ] || return 1
  grep -qF -- "<!-- sdlc:begin $s -->" "$f" && return 0
  case "$s" in
    commands)     re='^#{1,3}[[:space:]]+.*\bcommands?\b' ;;
    conventions)  re='^#{1,3}[[:space:]]+.*\bconventions?\b' ;;
    architecture) re='^#{1,3}[[:space:]]+.*\barchitecture\b' ;;
    mistakes)     re='^#{1,3}[[:space:]]+things claude gets wrong' ;;
    verifying)    re='^#{1,3}[[:space:]]+verifying your work' ;;
    *) return 1 ;;
  esac
  grep -qiE -- "$re" "$f"
}
claude_md_section_title() {
  case "$1" in
    commands) printf 'Commands';; conventions) printf 'Conventions';; architecture) printf 'Architecture';;
    mistakes) printf 'Things Claude gets wrong';; verifying) printf 'Verifying your work';;
  esac
}
SDLC_CLAUDE_SECTIONS="commands conventions architecture mistakes verifying"
SDLC_WORKFLOWS="sdlc-evals.yml sdlc-review.yml sdlc-spec-on-intent.yml sdlc-ci-triage.yml sdlc-monitor.yml sdlc-scan.yml"

# detect_project <root> [commands-override]  → JSON
#   {"commands":{build,test,lint,run,format,rollback},"stack":[...],"pm":"npm","registry":[...],
#    "git":{"is_repo":bool,"host":"","org":"","default_branch":"main"}}
#   commands-override: "build=..,test=..,lint=..,run=..,format=..,rollback=.." (commas inside values are kept
#   as long as the next key follows the comma).
detect_project() {
  python3 - "$1" "${2:-}" <<'PY'
import json, os, re, subprocess, sys
root, override = sys.argv[1], sys.argv[2]
def P(*p): return os.path.join(root, *p)
def exists(*p): return os.path.exists(P(*p))
def read(*p):
    try:
        with open(P(*p), encoding='utf-8', errors='replace') as fh: return fh.read()
    except Exception: return ''
cmds = {"build": "", "test": "", "lint": "", "run": "", "format": "", "rollback": ""}
info = {"stack": [], "pm": "", "registry": []}
def setc(k, v):
    if v and not cmds.get(k): cmds[k] = v
def reg(*hosts):
    for h in hosts:
        if h not in info["registry"]: info["registry"].append(h)

# 1. package.json scripts (lockfile-aware package manager)
if exists('package.json'):
    try: pkg = json.loads(read('package.json'))
    except Exception: pkg = {}
    if not isinstance(pkg, dict): pkg = {}
    scripts = pkg.get('scripts') or {}
    pm = 'pnpm' if exists('pnpm-lock.yaml') else 'yarn' if exists('yarn.lock') else 'bun' if (exists('bun.lockb') or exists('bun.lock')) else 'npm'
    info['pm'] = pm; info['stack'].append('node'); reg('registry.npmjs.org')
    def run(s): return "%s run %s" % (pm, s)
    for key, names in (('build', ('build',)), ('test', ('test',)), ('lint', ('lint',)), ('run', ('start', 'dev', 'serve')), ('format', ('format', 'fmt'))):
        for n in names:
            if n in scripts: setc(key, run(n)); break
    deps = {}
    for k in ('dependencies', 'devDependencies'):
        if isinstance(pkg.get(k), dict): deps.update(pkg[k])
    prettier_cfg = any(exists(f) for f in ('.prettierrc', '.prettierrc.json', '.prettierrc.js', '.prettierrc.cjs', '.prettierrc.yaml', '.prettierrc.yml', 'prettier.config.js', 'prettier.config.cjs', 'prettier.config.mjs'))
    if 'prettier' in deps or prettier_cfg: setc('format', 'prettier --write')
    if '@biomejs/biome' in deps or exists('biome.json') or exists('biome.jsonc'): setc('format', 'biome format --write')

# 2. Makefile targets
if exists('Makefile') or exists('makefile') or exists('GNUmakefile'):
    mk = read('Makefile') or read('makefile') or read('GNUmakefile')
    targets = set(re.findall(r'^([A-Za-z0-9_.-]+)\s*:(?!=)', mk, re.M))
    info['stack'].append('make')
    for key, names in (('build', ('build',)), ('test', ('test', 'check')), ('lint', ('lint',)), ('run', ('run', 'serve', 'dev')), ('format', ('fmt', 'format'))):
        for n in names:
            if n in targets: setc(key, 'make ' + n); break

# 3. Maven
if exists('pom.xml'):
    mvn = './mvnw' if exists('mvnw') else 'mvn'
    info['stack'].append('java-maven'); reg('repo.maven.apache.org')
    setc('build', mvn + ' -q -DskipTests package'); setc('test', mvn + ' -q test')
    pom = read('pom.xml')
    if 'spring-boot-maven-plugin' in pom: setc('run', mvn + ' spring-boot:run')
    if 'spotless-maven-plugin' in pom: setc('format', mvn + ' -q spotless:apply')

# 4. Gradle
if exists('build.gradle') or exists('build.gradle.kts'):
    gw = './gradlew' if exists('gradlew') else 'gradle'
    info['stack'].append('java-gradle'); reg('repo.maven.apache.org', 'plugins.gradle.org', 'services.gradle.org')
    setc('build', gw + ' build'); setc('test', gw + ' test')
    gb = read('build.gradle') or read('build.gradle.kts')
    if 'spotless' in gb: setc('format', gw + ' spotlessApply')
    if 'org.springframework.boot' in gb: setc('run', gw + ' bootRun')

# 5. Cargo
if exists('Cargo.toml'):
    info['stack'].append('rust'); reg('crates.io', 'static.crates.io', 'index.crates.io')
    setc('build', 'cargo build'); setc('test', 'cargo test'); setc('lint', 'cargo clippy'); setc('format', 'cargo fmt'); setc('run', 'cargo run')

# 6. Go
if exists('go.mod'):
    info['stack'].append('go'); reg('proxy.golang.org', 'sum.golang.org')
    setc('build', 'go build ./...'); setc('test', 'go test ./...'); setc('lint', 'go vet ./...'); setc('format', 'gofmt -w .')
    if exists('main.go'): setc('run', 'go run .')

# 7. Python
if exists('pyproject.toml') or exists('requirements.txt') or exists('setup.py') or exists('setup.cfg'):
    info['stack'].append('python'); reg('pypi.org', 'files.pythonhosted.org')
    py = read('pyproject.toml')
    setc('test', 'pytest')
    if '[tool.ruff' in py or exists('ruff.toml') or exists('.ruff.toml'): setc('lint', 'ruff check .'); setc('format', 'ruff format .')
    elif exists('.flake8') or '[flake8]' in read('setup.cfg'): setc('lint', 'flake8')
    else: setc('lint', 'ruff check .')
    if '[tool.black' in py: setc('format', 'black .')

# 8. Ruby
if exists('Gemfile'):
    info['stack'].append('ruby'); reg('rubygems.org', 'index.rubygems.org')
    gem = read('Gemfile')
    setc('test', 'bundle exec rspec' if ('rspec' in gem or exists('spec')) else 'bundle exec rake test')
    if 'rubocop' in gem: setc('lint', 'bundle exec rubocop'); setc('format', 'bundle exec rubocop -a')

# 9. PHP
if exists('composer.json'):
    info['stack'].append('php'); reg('packagist.org', 'repo.packagist.org')
    try: comp = json.loads(read('composer.json'))
    except Exception: comp = {}
    sc = (comp.get('scripts') or {}) if isinstance(comp, dict) else {}
    if 'test' in sc: setc('test', 'composer test')
    if 'lint' in sc: setc('lint', 'composer lint')
    if 'build' in sc: setc('build', 'composer build')

# explicit --commands override wins
if override:
    keys = 'build|test|lint|run|format|rollback'
    for m in re.finditer(r'(?:^|,)\s*(%s)=(.*?)(?=,\s*(?:%s)=|$)' % (keys, keys), override, re.S):
        cmds[m.group(1)] = m.group(2).strip()

git = {"is_repo": False, "host": "", "org": "", "default_branch": "main"}
def g(*a):
    try:
        r = subprocess.run(['git', '-C', root] + list(a), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception: return ''
if g('rev-parse', '--is-inside-work-tree') == 'true':
    git['is_repo'] = True
    url = g('remote', 'get-url', 'origin')
    m = re.match(r'^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)[:/]([^/]+)/', url or '')
    if m: git['host'], git['org'] = m.group(1), m.group(2)
    head = g('symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
    if head and '/' in head: git['default_branch'] = head.split('/', 1)[1]
    else:
        db = g('config', 'init.defaultBranch') or g('rev-parse', '--abbrev-ref', 'HEAD')
        if db and db != 'HEAD': git['default_branch'] = db
print(json.dumps({"commands": cmds, "stack": info['stack'], "pm": info['pm'], "registry": info['registry'], "git": git}))
PY
}

# perm_for_cmd <command>  → Bash permission rule for a detected command, e.g. "npm run test" → "Bash(npm run test*)"
perm_for_cmd() {
  local c="$1"
  [ -n "$c" ] || return 0
  c="${c%%|*}"; c="${c%%&&*}"; c="${c%%;*}"
  c="$(printf '%s' "$c" | sed -E 's/[[:space:]]+$//')"
  printf 'Bash(%s*)' "$c"
}

# ---------- adoption probes (shared by init & doctor) ----------
# count_files <dir> <glob>
count_md_artifacts() { artifact_slugs "$1" | grep -c . 2>/dev/null || true; }

# sdlc_next_steps <root>  → "N. text" lines in the playbook's adoption order (the dependency figure, not the stage order):
#   layer 0  preparation: git, /sdlc:init
#   layer 1  start anywhere (nothing points into them): Capture intent · CLAUDE.md · Feedback loop · Hooks (gate list) · Plan mode
#   layer 2  Skills · Subagents · Evals            layer 3  Requirements & design (spec) · PR review
#   layer 4  CI/CD                                  layer 5  Closing the loop (monitor, scans, on-call)
# This function is the single source of that order; init.sh, doctor.sh and the init/doctor skills all print it.
sdlc_next_steps() {
  local root="$1" n=0 s
  local intent_dir spec_dir plan_dir evals_dir
  intent_dir=$(cfg "$root" .paths.intent intent); spec_dir=$(cfg "$root" .paths.spec spec)
  plan_dir=$(cfg "$root" .paths.plan plan); evals_dir=$(cfg "$root" .paths.evals evals)
  local intents specs plans cases
  intents=$(count_md_artifacts "$root/$intent_dir"); specs=$(count_md_artifacts "$root/$spec_dir"); plans=$(count_md_artifacts "$root/$plan_dir")
  cases=0; [ -d "$root/$evals_dir/cases" ] && cases=$(find "$root/$evals_dir/cases" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -c . || true)
  local missing_sections="" todo_sections=""
  for s in $SDLC_CLAUDE_SECTIONS; do
    claude_md_section_present "$root/CLAUDE.md" "$s" || missing_sections="$missing_sections $(claude_md_section_title "$s"),"
  done
  [ -f "$root/CLAUDE.md" ] && grep -qE 'TODO: (set the|describe)' "$root/CLAUDE.md" 2>/dev/null && todo_sections=yes
  local test_cmd policies; test_cmd=$(cfg "$root" .commands.test "")
  policies=$(json_file_get "$root/.sdlc/config.json" .policies 2>/dev/null | tr -d '[] \n')
  local step; step() { n=$((n+1)); printf '%d. [%s] %s\n' "$n" "$1" "$2"; }

  # layer 0 — preparation
  is_git_repo "$root" || step "0 prep" "Run 'git init' and commit — the audit trail (who approved which intent/spec/plan, when) is the git log."
  sdlc_initialized "$root" || step "0 prep" "Run /sdlc:init (bash scripts/init.sh) — scaffolds .sdlc/, CLAUDE.md sections, REVIEW.md, $intent_dir/$spec_dir/$plan_dir, $evals_dir/."
  # layer 1 — start anywhere
  [ "${intents:-0}" -eq 0 ] && step "1 start" "Capture intent (Stage 1): /sdlc:intent → $intent_dir/<slug>.md; the product owner approves by committing status: approved."
  if [ ! -f "$root/CLAUDE.md" ] || [ -n "$missing_sections" ]; then
    step "1 start" "CLAUDE.md (Stage 3): add the missing sections —${missing_sections% ,} — /sdlc:init appends them and has Claude fill them from the repository."
  elif [ -n "$todo_sections" ]; then
    step "1 start" "CLAUDE.md (Stage 3): replace the TODO lines in Commands / Conventions / Architecture / Verifying your work with facts from the repository (keep it to one page)."
  fi
  [ -z "$test_cmd" ] && step "1 start" "Feedback loop (Stage 4): set commands.build/test/lint in .sdlc/config.json — the verify loop, the Stop hook and Bash permissions derive from them."
  if [ ! -f "$root/.claude/settings.json" ] || ! grep -q '"deny"' "$root/.claude/settings.json" 2>/dev/null; then
    step "1 start" "Hooks — the approval-gate list (Stage 5): review .sdlc/APPROVALS.md, .claude/settings.json deny list, .sdlc/frozen-paths.txt and environments.*.patterns in .sdlc/config.json."
  fi
  [ "${plans:-0}" -eq 0 ] && step "1 start" "Run the loop: start the next change with /sdlc:go \"<request>\" — it commits intent, spec and plan.md (Files that change / Order of work / Risks / Proof) before any code, then implements, verifies, reviews and opens the PR. Step by step instead: /sdlc:intent → /sdlc:spec → /sdlc:plan."
  # layer 2 — skills, subagents, evals
  [ -z "$policies" ] && step "2" "Skills (Stage 3): encode one inconsistently-enforced policy with /sdlc:policy (or --example secure-api-review); /sdlc:spec and /sdlc:review apply it."
  step "2" "Subagents (Stage 3): use the plugin agents (sdlc-verifier, sdlc-reviewer, sdlc-researcher, sdlc-simplifier, sdlc-diagnoser) from /sdlc:verify, /sdlc:review and /sdlc:plan; run independent tasks in parallel with 'claude --worktree <task>'."
  if [ "${cases:-0}" -eq 0 ]; then
    step "2" "Evals (Stage 4): add cases under $evals_dir/cases/ (/sdlc:evals add) — start from 20–50 real recent tasks; one case per incident, review finding or vulnerability class."
  elif ! grep -q '"event":"eval.run"' "$root/.sdlc/logs/events.jsonl" 2>/dev/null; then
    step "2" "Evals (Stage 4): run the suite once locally — /sdlc:evals run — then gate CLAUDE.md/.claude/evals changes on it in CI (sdlc-evals.yml)."
  fi
  # layer 3 — requirements & design, PR review
  if [ "${intents:-0}" -gt 0 ] && [ "${specs:-0}" -eq 0 ]; then
    step "3" "Requirements & design (Stage 2): /sdlc:spec <slug> on an approved intent → $spec_dir/<slug>.md with Flagged concerns for the product owner (needs the intent and the policy skills)."
  fi
  if [ ! -f "$root/.github/workflows/sdlc-review.yml" ] || [ ! -f "$root/.github/CODEOWNERS" ]; then
    step "3" "PR review (Stage 5): install sdlc-review.yml and CODEOWNERS (init.sh --github); require code-owner approval in branch protection — agents never approve (needs CLAUDE.md, evals, skills, subagents)."
  fi
  if [ -n "$test_cmd" ] && [ "$(cfg "$root" .verify.required_before_stop false)" != "true" ]; then
    step "3" "Verify-before-done (Stage 4): once the loop is trusted set verify.required_before_stop: true so the Stop hook enforces it."
  fi
  # layer 4 — CI/CD
  local wf_missing=""
  for s in $SDLC_WORKFLOWS; do [ -f "$root/.github/workflows/$s" ] || wf_missing="$wf_missing $s"; done
  case "$wf_missing" in *sdlc-evals.yml*|*sdlc-ci-triage.yml*|*sdlc-spec-on-intent.yml*)
    step "4" "CI/CD (Stage 5): add sdlc-evals.yml (merge check), sdlc-ci-triage.yml (read-only build triage) and sdlc-spec-on-intent.yml; set the production approval env (RELEASE_APPROVAL) and a rehearsed commands.rollback (needs the review loop and the gates)." ;;
  esac
  # layer 5 — closing the loop
  local runbook_todo=""
  if [ -f "$root/.sdlc/bands.json" ] && grep -qE 'TODO|\{\{' "$root/.sdlc/bands.json" 2>/dev/null; then runbook_todo=yes; fi
  case "$wf_missing$runbook_todo" in *sdlc-monitor.yml*|*sdlc-scan.yml*|*yes)
    step "5" "Closing the loop (Stage 6): set runbooks in .sdlc/bands.json, schedule sdlc-monitor.yml and sdlc-scan.yml, triage the queue with /sdlc:triage, close incidents with /sdlc:postmortem (needs intent.md, the review loop, the gates, the rollback path)." ;;
  esac
  if [ "$n" -eq 0 ]; then
    printf '%s\n' "All plays are adopted. Run the loop with /sdlc:go \"<one-sentence request>\" (solo: --autopilot); /sdlc:metrics for the indicators, /sdlc:evals add after each incident."
  fi
}
