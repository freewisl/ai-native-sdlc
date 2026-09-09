#!/bin/bash
# sdlc plugin — deterministic, non-destructive project scaffold (design §2, §6).
# Never overwrites a user file: existing files are reported SKIPPED (exists) or MERGED (append-only / array union).
#
# Usage: init.sh [--lang en|ko] [--github] [--sandbox] [--managed] [--mcp] [--dry-run] [--dir <root>]
#                [--commands build=..,test=..,lint=..,run=..,format=..,rollback=..] [--help]
set -o pipefail
. "$(dirname "$0")/lib/common.sh"
. "$(dirname "$0")/lib/scripts-extra.sh"

usage() {
  cat <<'EOF'
init.sh — scaffold the AI-native SDLC layout into a project (idempotent, never destructive)

  --lang en|ko      language of human-facing templates (default: en; ko falls back to en per file when missing)
  --github          also install .github/workflows/sdlc-*.yml + CODEOWNERS (automatic when .github/ exists)
  --sandbox         merge the sandbox block into .claude/settings.json
  --managed         write .sdlc/managed-settings.example.json (+ README) for the platform team
  --mcp             write .mcp.json (deployment tools example) when absent
  --dry-run         print what would happen; write nothing
  --dir <root>      project root (default: $CLAUDE_PROJECT_DIR, git toplevel, or cwd)
  --commands k=v,.. override detected commands (build,test,lint,run,format,rollback)
  --marketplace <s> marketplace source the CI workflows install the plugin from (default: $SDLC_MARKETPLACE_SOURCE or TODO-org/ai-native-sdlc)
  --ci-workflow <n> name of the CI workflow that sdlc-ci-triage.yml listens to (default: first `name:` in .github/workflows/*.yml, else CI)
  --team <@org/team> code owner handle for CODEOWNERS (default: @<git org>/TODO-team)
  --no-github       do not install workflows even when the remote is GitHub   --no-solo  never set roles.solo automatically
  --solo            one-person repository: roles.solo=true — the user is product owner, tech lead and release manager, so skills
                    take approvals in-session instead of waiting for another person (branch ruleset gets an admin bypass for PRs)
  --auth api|oauth  how CI authenticates Claude: api = secret ANTHROPIC_API_KEY (default); oauth = secret CLAUDE_CODE_OAUTH_TOKEN
                    made with `claude setup-token` (subscription account). Also via $SDLC_AUTH
  --help

Output lines start with CREATED / MERGED / SKIPPED (exists) / WOULD CREATE / WOULD MERGE / NOTE, then a summary
and the "Next steps" list (playbook dependency order).
EOF
}

LANG_OPT=en; DO_GITHUB=0; DO_SANDBOX=0; DO_MANAGED=0; DO_MCP=0; DRY=0; ROOT_ARG=""; CMD_OVERRIDE=""
MARKETPLACE_SOURCE="${SDLC_MARKETPLACE_SOURCE:-}"; CI_WORKFLOW=""; TEAM_ARG=""; AUTH_MODE="${SDLC_AUTH:-}"; SOLO=0; SOLO_EXPLICIT=0; NO_GITHUB=0; NO_SOLO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --lang) LANG_OPT="$2"; shift 2;;
    --lang=*) LANG_OPT="${1#*=}"; shift;;
    --github) DO_GITHUB=1; shift;;
    --sandbox) DO_SANDBOX=1; shift;;
    --managed) DO_MANAGED=1; shift;;
    --mcp) DO_MCP=1; shift;;
    --dry-run|-n) DRY=1; shift;;
    --dir) ROOT_ARG="$2"; shift 2;;
    --dir=*) ROOT_ARG="${1#*=}"; shift;;
    --commands) CMD_OVERRIDE="$2"; shift 2;;
    --commands=*) CMD_OVERRIDE="${1#*=}"; shift;;
    --marketplace) MARKETPLACE_SOURCE="$2"; shift 2;;
    --marketplace=*) MARKETPLACE_SOURCE="${1#*=}"; shift;;
    --ci-workflow) CI_WORKFLOW="$2"; shift 2;;
    --ci-workflow=*) CI_WORKFLOW="${1#*=}"; shift;;
    --team) TEAM_ARG="$2"; shift 2;;
    --team=*) TEAM_ARG="${1#*=}"; shift;;
    --solo) SOLO=1; SOLO_EXPLICIT=1; shift;;
    --no-github) NO_GITHUB=1; shift;;
    --no-solo) NO_SOLO=1; shift;;
    --auth) AUTH_MODE="$2"; shift 2;;
    --auth=*) AUTH_MODE="${1#*=}"; shift;;
    -h|--help) usage; exit 0;;
    *) die "unknown option: $1 (see --help)";;
  esac
done
case "$LANG_OPT" in en|ko) :;; *) die "--lang must be en or ko";; esac
case "$AUTH_MODE" in ''|api|oauth) :;; *) die "--auth must be api or oauth";; esac
has_cmd python3 || die "python3 is required by init.sh (JSON rendering and settings merge)"

ROOT=$(resolve_root "$ROOT_ARG")
TMP=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-init.XXXXXX"); trap 'rm -rf "$TMP"' EXIT
N_CREATED=0; N_MERGED=0; N_SKIPPED=0
KO_NOTE_SHOWN=0

# ---------- reporting ----------
report() { # report <verb> <rel-path> [note]
  local verb="$1" rel="$2" note="${3:-}"
  if [ -n "$note" ]; then printf '%-17s %s  (%s)\n' "$verb" "$rel" "$note"; else printf '%-17s %s\n' "$verb" "$rel"; fi
}
r_created() { if [ "$DRY" = 1 ]; then report "WOULD CREATE" "$1" "${2:-}"; else report "CREATED" "$1" "${2:-}"; fi; N_CREATED=$((N_CREATED+1)); }
r_merged()  { if [ "$DRY" = 1 ]; then report "WOULD MERGE" "$1" "${2:-}"; else report "MERGED" "$1" "${2:-}"; fi; N_MERGED=$((N_MERGED+1)); }
r_skipped() { report "SKIPPED (exists)" "$1" "${2:-}"; N_SKIPPED=$((N_SKIPPED+1)); }

# tpl <name>  → path of the template for the chosen language, falling back to en with one note
tpl() {
  local name="$1"
  if [ "$LANG_OPT" != "en" ] && [ -f "$SDLC_TEMPLATES/$LANG_OPT/$name" ]; then printf '%s' "$SDLC_TEMPLATES/$LANG_OPT/$name"; return; fi
  if [ "$LANG_OPT" != "en" ] && [ "$KO_NOTE_SHOWN" = 0 ]; then
    note "templates/$LANG_OPT/ not found (or incomplete) — falling back to templates/en/ for missing files"; KO_NOTE_SHOWN=1
  fi
  printf '%s' "$SDLC_TEMPLATES/en/$name"
}

# put_file <rel> <src-file> [note]  — create when absent (copy), else SKIPPED. Never overwrites.
put_file() {
  local rel="$1" src="$2" note="${3:-}" dest="$ROOT/$1"
  if [ -e "$dest" ]; then r_skipped "$rel"; return 1; fi
  if [ "$DRY" = 0 ]; then mkdir -p "$(dirname "$dest")" && cp "$src" "$dest"; fi
  r_created "$rel" "$note"; return 0
}
put_empty() { # put_empty <rel>  — .gitkeep style
  local dest="$ROOT/$1"
  if [ -e "$dest" ]; then r_skipped "$1"; return 1; fi
  if [ "$DRY" = 0 ]; then mkdir -p "$(dirname "$dest")" && : > "$dest"; fi
  r_created "$1"; return 0
}

# ---------- detection ----------
printf 'sdlc init — %s%s\n' "$ROOT" "$([ "$DRY" = 1 ] && printf ' (dry run)')"
DETECT=$(detect_project "$ROOT" "$CMD_OVERRIDE")
BUILD_CMD=$(json_get "$DETECT" .commands.build); TEST_CMD=$(json_get "$DETECT" .commands.test)
LINT_CMD=$(json_get "$DETECT" .commands.lint);   RUN_CMD=$(json_get "$DETECT" .commands.run)
FORMAT_CMD=$(json_get "$DETECT" .commands.format); ROLLBACK_CMD=$(json_get "$DETECT" .commands.rollback)
STACK=$(json_get "$DETECT" .stack | tr -d '[]"' | tr ',' ' ')
GIT_HOST=$(json_get "$DETECT" .git.host); GIT_ORG=$(json_get "$DETECT" .git.org); DEFAULT_BRANCH=$(json_get "$DETECT" .git.default_branch)
[ -n "$DEFAULT_BRANCH" ] || DEFAULT_BRANCH=main
IS_GIT=0; is_git_repo "$ROOT" && IS_GIT=1
detected=""
[ -n "$BUILD_CMD" ] && detected="$detected build='$BUILD_CMD'"
[ -n "$TEST_CMD" ] && detected="$detected test='$TEST_CMD'"
[ -n "$LINT_CMD" ] && detected="$detected lint='$LINT_CMD'"
[ -n "$RUN_CMD" ] && detected="$detected run='$RUN_CMD'"
[ -n "$FORMAT_CMD" ] && detected="$detected format='$FORMAT_CMD'"
printf 'DETECTED          stack: %s | commands:%s\n' "${STACK:-none}" "${detected:- none (set them in .sdlc/config.json → commands)}"
if [ "$IS_GIT" = 0 ]; then
  printf '\nNOTE  %s is NOT a git repository. The playbook audit trail (who approved which intent/spec/plan and when)\n' "$ROOT"
  printf 'NOTE  is the git log — run `git init` and commit the scaffold. init.sh does not run git for you.\n\n'
fi

# ---------- zero-flag defaults: detect what the flags would say ----------
#   GitHub remote → install workflows (unless --no-github); one collaborator → roles.solo (unless --no-solo);
#   a stored CI token (keychain / ~/.config/sdlc/ci-token) → --auth oauth, else api.  Set SDLC_SKIP_GH_DETECT=1 to skip network calls.
case "$GIT_HOST" in github.com|github.*) [ "$DO_GITHUB" = 0 ] && [ "$NO_GITHUB" = 0 ] && { DO_GITHUB=1; note "GitHub remote ($GIT_HOST) detected — installing the sdlc workflows (--no-github to skip)"; };; esac
if [ "$SOLO" = 0 ] && [ "$NO_SOLO" = 0 ] && [ "$IS_GIT" = 1 ] && [ -z "${SDLC_SKIP_GH_DETECT:-}" ] && gh_ready "$ROOT"; then
  nwo=$(cd "$ROOT" && gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)
  if [ -n "$nwo" ]; then
    n=$(gh api "repos/$nwo/collaborators?per_page=100" --jq 'length' 2>/dev/null | tr -cd '0-9')
    if [ -n "$n" ] && [ "$n" -le 1 ]; then SOLO=1; note "one collaborator on $nwo — roles.solo=true (--no-solo to keep team gates)"
    elif [ -z "$n" ]; then note "collaborator count unavailable (permissions/network) — keeping team gates; pass --solo to override"; fi
  fi
fi
if [ -z "$AUTH_MODE" ] && [ -f "$ROOT/.sdlc/config.json" ]; then AUTH_MODE=$(cfg "$ROOT" .ci.auth ""); fi
if [ -z "$AUTH_MODE" ]; then
  AUTH_MODE=api
  tf="${SDLC_CI_TOKEN_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/sdlc/ci-token}"
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -s "$tf" ] || { has_cmd security && security find-generic-password -a sdlc -s sdlc-ci-token >/dev/null 2>&1; }; then AUTH_MODE=oauth; note "stored subscription token found — CI auth = oauth (CLAUDE_CODE_OAUTH_TOKEN)"; fi
fi

# paths: respect an existing config, otherwise defaults (+ evals dir decision)
INTENT_DIR=intent; SPEC_DIR=spec; PLAN_DIR=plan; EVALS_DIR=evals; REVIEW_FILE=REVIEW.md; LESSONS_FILE=.sdlc/LESSONS.md
CONFIG_EXISTS=0
if [ -f "$ROOT/.sdlc/config.json" ]; then
  CONFIG_EXISTS=1
  INTENT_DIR=$(cfg "$ROOT" .paths.intent intent); SPEC_DIR=$(cfg "$ROOT" .paths.spec spec); PLAN_DIR=$(cfg "$ROOT" .paths.plan plan)
  EVALS_DIR=$(cfg "$ROOT" .paths.evals evals); REVIEW_FILE=$(cfg "$ROOT" .paths.review REVIEW.md); LESSONS_FILE=$(cfg "$ROOT" .paths.lessons .sdlc/LESSONS.md)
else
  if [ -d "$ROOT/evals" ] && ! grep -qF '<!-- sdlc:evals -->' "$ROOT/evals/README.md" 2>/dev/null; then
    EVALS_DIR=sdlc-evals
    note "evals/ already exists and is not ours (no <!-- sdlc:evals --> marker) — using sdlc-evals/ and setting paths.evals"
  fi
fi

# ---------- .sdlc/ ----------
if [ "$CONFIG_EXISTS" = 1 ]; then
  # merge only: an explicit --solo sets roles.solo; auto-detected solo never overrides an existing roles.solo key;
  # missing ci.auth / protect.default_branch are filled (never changed). Invalid JSON → nothing is written.
  res=$(python3 - "$ROOT/.sdlc/config.json" "$SOLO" "$SOLO_EXPLICIT" "$AUTH_MODE" "$DEFAULT_BRANCH" "$DRY" "$SDLC_TEMPLATES/config/config.json" <<'PY'
import json,sys
p,solo,explicit,auth,defb,dry,tpl=sys.argv[1:8]
try: c=json.load(open(p,encoding='utf-8'))
except Exception: print("INVALID"); sys.exit(0)
ch=[]
roles=c.setdefault('roles',{})
if solo=='1' and roles.get('solo') is not True and (explicit=='1' or 'solo' not in roles): roles['solo']=True; ch.append('roles.solo=true')
if 'auth' not in c.setdefault('ci',{}): c['ci']['auth']=auth; ch.append('ci.auth=%s'%auth)
if 'loop' not in c: c['loop']=json.load(open(tpl,encoding='utf-8')).get('loop',{}); ch.append('loop=defaults (enabled:false)')
if not c.setdefault('protect',{}).get('default_branch'): c['protect']['default_branch']=defb; ch.append('protect.default_branch=%s'%defb)
if ch and dry!='1':
    json.dump(c,open(p,'w',encoding='utf-8'),indent=2,ensure_ascii=False); open(p,'a').write('\n')
print(' '.join(ch) if ch else 'NONE')
PY
)
  case "$res" in
    INVALID) report "SKIPPED (invalid)" ".sdlc/config.json" "not valid JSON — nothing changed"; N_SKIPPED=$((N_SKIPPED+1));;
    NONE) r_skipped ".sdlc/config.json";;
    *) r_merged ".sdlc/config.json" "$res";;
  esac
else
  python3 - "$SDLC_TEMPLATES/config/config.json" "$TMP/config.json" "$LANG_OPT" "$EVALS_DIR" "$DETECT" "$SOLO" "$AUTH_MODE" "$DEFAULT_BRANCH" <<'PY'
import json, sys
src, dest, lang, evals_dir, detect, solo, auth, defb = sys.argv[1:9]
cfg = json.load(open(src, encoding='utf-8'))
d = json.loads(detect)
cfg['language'] = lang
cfg.setdefault('paths', {})['evals'] = evals_dir
if solo == '1': cfg.setdefault('roles', {})['solo'] = True
cfg.setdefault('ci', {})['auth'] = auth                       # github-setup.sh reads this — one source for the CI secret name
cfg.setdefault('protect', {})['default_branch'] = defb or 'main'   # the push guard's fallback when origin/HEAD is unknown
for k, v in d.get('commands', {}).items():
    if v: cfg.setdefault('commands', {})[k] = v
with open(dest, 'w', encoding='utf-8') as fh:
    json.dump(cfg, fh, indent=2, ensure_ascii=False); fh.write('\n')
PY
  put_file ".sdlc/config.json" "$TMP/config.json" "language=$LANG_OPT, paths.evals=$EVALS_DIR, ci.auth=$AUTH_MODE, default_branch=$DEFAULT_BRANCH$([ "$SOLO" = 1 ] && printf ', roles.solo=true')"
fi

RB="$ROLLBACK_CMD"; [ -n "$RB" ] || RB="TODO: set the rollback command (same as commands.rollback in .sdlc/config.json)"
py_render "$SDLC_TEMPLATES/config/bands.json" "$TMP/bands.json" "rollback_cmd=$RB"
put_file ".sdlc/bands.json" "$TMP/bands.json"
put_file ".sdlc/frozen-paths.txt" "$SDLC_TEMPLATES/config/frozen-paths.txt"
put_file "$LESSONS_FILE" "$(tpl LESSONS.md)"
put_file ".sdlc/APPROVALS.md" "$(tpl APPROVALS.md)" "approval-gate register — edit the approvers"
put_empty ".sdlc/history/.gitkeep"
# the playbook's original code blocks, verbatim, next to the working templates
if [ -d "$SDLC_TEMPLATES/examples" ]; then
  if [ -e "$ROOT/.sdlc/examples" ]; then r_skipped ".sdlc/examples/"
  else
    if [ "$DRY" = 0 ]; then mkdir -p "$ROOT/.sdlc" && cp -R "$SDLC_TEMPLATES/examples" "$ROOT/.sdlc/examples"; fi
    r_created ".sdlc/examples/" "playbook code blocks, verbatim"
  fi
fi

# ---------- artifact chain ----------
put_file "$INTENT_DIR/README.md" "$(tpl intent-README.md)"
put_file "$INTENT_DIR/TEMPLATE.md" "$(tpl intent.md)"
put_empty "$INTENT_DIR/triage/.gitkeep"
put_file "$SPEC_DIR/TEMPLATE.md" "$(tpl spec.md)"
put_file "$PLAN_DIR/TEMPLATE.md" "$(tpl plan.md)"

# ---------- REVIEW.md ----------
GEN_PATHS=""
[ "$CONFIG_EXISTS" = 1 ] && GEN_PATHS=$(cfg "$ROOT" .protect.generated_paths "" | tr -d '[]"' | sed 's/,/, /g')
[ -n "$GEN_PATHS" ] || GEN_PATHS="TODO: list generated paths here (and in protect.generated_paths of .sdlc/config.json)"
py_render "$(tpl REVIEW.md)" "$TMP/REVIEW.md" "generated_paths=$GEN_PATHS"
put_file "$REVIEW_FILE" "$TMP/REVIEW.md"

# ---------- evals ----------
put_file "$EVALS_DIR/README.md" "$(tpl evals-README.md)"
for case_dir in "$SDLC_TEMPLATES"/evals/cases/*/; do
  [ -d "$case_dir" ] || continue
  cname=$(basename "$case_dir"); rel="$EVALS_DIR/cases/$cname"
  if [ -e "$ROOT/$rel" ]; then r_skipped "$rel/"; continue; fi
  if [ "$DRY" = 0 ]; then mkdir -p "$ROOT/$EVALS_DIR/cases" && cp -R "$case_dir" "$ROOT/$rel"; fi
  r_created "$rel/" "example case"
done

# ---------- .gitignore (append only missing lines) ----------
sed "s#^evals/results/#$EVALS_DIR/results/#" "$SDLC_TEMPLATES/config/gitignore.sdlc" > "$TMP/gitignore.lines"
if [ ! -f "$ROOT/.gitignore" ]; then
  put_file ".gitignore" "$TMP/gitignore.lines"
else
  : > "$TMP/gitignore.add"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    case "$line" in \#*) grep -qxF -- "$line" "$ROOT/.gitignore" || printf '%s\n' "$line" >> "$TMP/gitignore.add"; continue;; esac
    grep -qxF -- "$line" "$ROOT/.gitignore" || printf '%s\n' "$line" >> "$TMP/gitignore.add"
  done < "$TMP/gitignore.lines"
  if grep -qv '^#' "$TMP/gitignore.add" 2>/dev/null; then
    nadd=$(grep -cv '^#' "$TMP/gitignore.add")
    if [ "$DRY" = 0 ]; then
      { [ -s "$ROOT/.gitignore" ] && [ "$(tail -c1 "$ROOT/.gitignore" | od -An -c | tr -d ' ')" != '\n' ] && printf '\n'; printf '\n'; cat "$TMP/gitignore.add"; } >> "$ROOT/.gitignore"
    fi
    r_merged ".gitignore" "+$nadd lines"
  else
    r_skipped ".gitignore" "all lines present"
  fi
fi

# ---------- CLAUDE.md (create, or append only missing sections) ----------
todo_or() { [ -n "$1" ] && printf '%s' "$1" || printf '%s' "$2"; }
py_render "$(tpl CLAUDE.sections.md)" "$TMP/sections.md" \
  "build_cmd=$(todo_or "$BUILD_CMD" 'TODO: set the build command')" \
  "test_cmd=$(todo_or "$TEST_CMD" 'TODO: set the test command')" \
  "lint_cmd=$(todo_or "$LINT_CMD" 'TODO: set the lint command')" \
  "run_cmd=$(todo_or "$RUN_CMD" 'TODO: set the run command')" \
  "conventions=TODO: describe naming, error handling, testing and dependency conventions (Claude fills this from the repository during /sdlc:init)" \
  "architecture=TODO: describe the main components, boundaries and data flow (Claude fills this from the repository during /sdlc:init)"
if [ ! -f "$ROOT/CLAUDE.md" ]; then
  { printf '# %s\n\n' "$(basename "$ROOT")"; cat "$TMP/sections.md"; } > "$TMP/CLAUDE.md"
  put_file "CLAUDE.md" "$TMP/CLAUDE.md" "5 sections"
else
  added=""
  : > "$TMP/claude.add"
  for s in $SDLC_CLAUDE_SECTIONS; do
    if ! claude_md_section_present "$ROOT/CLAUDE.md" "$s"; then
      awk -v s="$s" '$0=="<!-- sdlc:begin " s " -->"{p=1} p{print} $0=="<!-- sdlc:end " s " -->"{p=0; print ""}' "$TMP/sections.md" >> "$TMP/claude.add"
      added="$added$(claude_md_section_title "$s"), "
    fi
  done
  if [ -n "$added" ]; then
    if [ "$DRY" = 0 ]; then
      { [ "$(tail -c1 "$ROOT/CLAUDE.md" | od -An -c | tr -d ' ')" != '\n' ] && printf '\n'; printf '\n'; cat "$TMP/claude.add"; } >> "$ROOT/CLAUDE.md"
    fi
    r_merged "CLAUDE.md" "added: ${added%, }"
  else
    r_skipped "CLAUDE.md" "all 5 sections present"
  fi
fi

# ---------- .claude/settings.json (create or union-merge permissions) ----------
python3 - "$SDLC_TEMPLATES/config/settings.project.json" "$TMP/settings.ours.json" \
  "$(perm_for_cmd "$BUILD_CMD")" "$(perm_for_cmd "$TEST_CMD")" "$(perm_for_cmd "$LINT_CMD")" <<'PY'
import json, sys
src, dest, b, t, l = sys.argv[1:6]
s = json.load(open(src, encoding='utf-8'))
repl = {'{{build_permission}}': b, '{{test_permission}}': t, '{{lint_permission}}': l}
out = []
for item in s['permissions']['allow']:
    item = repl.get(item, item)
    if item and item not in out and '{{' not in item: out.append(item)
s['permissions']['allow'] = out
with open(dest, 'w', encoding='utf-8') as fh:
    json.dump(s, fh, indent=2, ensure_ascii=False); fh.write('\n')
PY
merge_settings() { # merge_settings <ours.json> <label>  — union of permissions.allow/deny; adds top-level keys (sandbox) only when absent
  local ours="$1" label="$2" res
  res=$(python3 - "$ROOT/.claude/settings.json" "$ours" "$DRY" <<'PY'
import json, sys
dest, ours_path, dry = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
ours = json.load(open(ours_path, encoding='utf-8'))
try:
    cur = json.load(open(dest, encoding='utf-8'))
except Exception:
    print('INVALID'); sys.exit(0)
if not isinstance(cur, dict):
    print('INVALID'); sys.exit(0)
changed = []
if isinstance(ours.get('permissions'), dict):
    perm = cur.get('permissions')
    if not isinstance(perm, dict):
        perm = {}; cur['permissions'] = perm
    for k in ('allow', 'deny'):
        want = ours['permissions'].get(k) or []
        have = perm.get(k) if isinstance(perm.get(k), list) else []
        add = [x for x in want if x not in have]
        if add:
            perm[k] = have + add; changed.append('%s+%d' % (k, len(add)))
for k, v in ours.items():
    if k == 'permissions': continue
    if k not in cur:
        cur[k] = v; changed.append('+' + k)
if changed and not dry:
    with open(dest, 'w', encoding='utf-8') as fh:
        json.dump(cur, fh, indent=2, ensure_ascii=False); fh.write('\n')
print(' '.join(changed) if changed else 'NONE')
PY
)
  case "$res" in
    INVALID) report "SKIPPED (invalid)" ".claude/settings.json" "not valid JSON — fix it by hand, nothing was changed"; N_SKIPPED=$((N_SKIPPED+1));;
    NONE) r_skipped ".claude/settings.json" "$label already present";;
    *) r_merged ".claude/settings.json" "$label: $res";;
  esac
}
if [ -f "$ROOT/.claude/settings.json" ]; then
  merge_settings "$TMP/settings.ours.json" "permissions"
else
  put_file ".claude/settings.json" "$TMP/settings.ours.json" "permissions allow/deny; hooks come from the plugin"
fi

# ---------- --sandbox ----------
if [ "$DO_SANDBOX" = 1 ]; then
  REG=$(json_get "$DETECT" .registry | tr -d '[]"' | tr ',' '\n' | head -1)
  py_render "$SDLC_TEMPLATES/config/settings.sandbox.json" "$TMP/settings.sandbox.json" \
    "git_host=$(todo_or "$GIT_HOST" 'TODO: your git host, e.g. github.com')" \
    "package_registry=$(todo_or "$REG" 'TODO: your package registry, e.g. registry.npmjs.org')"
  if [ -f "$ROOT/.claude/settings.json" ] && [ "$DRY" = 0 ]; then
    merge_settings "$TMP/settings.sandbox.json" "sandbox"
  elif [ -f "$ROOT/.claude/settings.json" ]; then
    merge_settings "$TMP/settings.sandbox.json" "sandbox"
  else
    put_file ".claude/settings.json" "$TMP/settings.sandbox.json" "sandbox block"
  fi
fi

# ---------- --managed ----------
if [ "$DO_MANAGED" = 1 ]; then
  put_file ".sdlc/managed-settings.example.json" "$SDLC_TEMPLATES/config/managed-settings.json" "reference copy — deploy via managed settings, not from the repo"
  cat > "$TMP/managed.README.md" <<'EOF'
# Managed settings (reference copy)
`managed-settings.example.json` is the playbook's organization-level policy (deny lists, sandbox, managed-only hooks/MCP/marketplaces). Deploy it through the OS managed-settings path or the admin console, never from this repository — see docs/ENTERPRISE.md in the ai-native-sdlc marketplace for per-OS paths, key descriptions and rollout order.
Generated by `/sdlc:init --managed`; edit the placeholders (domains, marketplace repo, minimum version) before rollout.
EOF
  put_file ".sdlc/managed-settings.README.md" "$TMP/managed.README.md"
fi

# ---------- --mcp ----------
if [ "$DO_MCP" = 1 ]; then
  put_file ".mcp.json" "$SDLC_TEMPLATES/config/mcp.deploy.example.json" "deployment tools example — replace with your platform's MCP server"
fi

# ---------- GitHub (explicit or auto when .github/ exists) ----------
if [ "$DO_GITHUB" = 1 ] || [ -d "$ROOT/.github" ]; then
  TEAM="@TODO-org/TODO-team"; [ -n "$GIT_ORG" ] && TEAM="@$GIT_ORG/TODO-team"; [ -n "$TEAM_ARG" ] && TEAM="$TEAM_ARG"
  [ -n "$MARKETPLACE_SOURCE" ] || MARKETPLACE_SOURCE="TODO-org/ai-native-sdlc"
  if [ -z "$CI_WORKFLOW" ]; then   # the CI workflow sdlc-ci-triage.yml waits for: first `name:` of an existing non-sdlc workflow
    for f in "$ROOT"/.github/workflows/*.yml "$ROOT"/.github/workflows/*.yaml; do
      [ -f "$f" ] || continue; case "$(basename "$f")" in sdlc-*) continue;; esac
      CI_WORKFLOW=$(grep -m1 -E '^name:' "$f" | sed -E 's/^name:[[:space:]]*//; s/^["'"'"']//; s/["'"'"']$//'); [ -n "$CI_WORKFLOW" ] && break
    done
    [ -n "$CI_WORKFLOW" ] || CI_WORKFLOW="CI"
  fi
  A_OAUTH='claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}'; C_OAUTH='CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}'
  A_API='anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}'; C_API='ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}'
  if [ "$AUTH_MODE" = oauth ]; then
    ACTION_AUTH="$A_OAUTH"; CLI_AUTH="$C_OAUTH"; SECRET_NAME=CLAUDE_CODE_OAUTH_TOKEN; OTHER_SECRET=ANTHROPIC_API_KEY; OTHER_ACTION="$A_API"; OTHER_CLI="$C_API"
    note "CI auth = oauth: create the token with \`claude setup-token\` and store it as repository secret CLAUDE_CODE_OAUTH_TOKEN"
  else
    ACTION_AUTH="$A_API"; CLI_AUTH="$C_API"; SECRET_NAME=ANTHROPIC_API_KEY; OTHER_SECRET=CLAUDE_CODE_OAUTH_TOKEN; OTHER_ACTION="$A_OAUTH"; OTHER_CLI="$C_OAUTH"
  fi
  for wf in "$SDLC_TEMPLATES"/github/*.yml; do
    [ -f "$wf" ] || continue
    name=$(basename "$wf")
    py_render "$wf" "$TMP/$name" "default_branch=$DEFAULT_BRANCH" "team=$TEAM" "marketplace_source=$MARKETPLACE_SOURCE" "ci_workflow_name=$CI_WORKFLOW" \
      "evals_dir=$EVALS_DIR" "intent_dir=$INTENT_DIR" "spec_dir=$SPEC_DIR" "plan_dir=$PLAN_DIR" "action_auth_line=$ACTION_AUTH" "cli_auth_env=$CLI_AUTH"
    if grep -qE '\{\{[a-z_]+\}\}' "$TMP/$name"; then note "$name still contains {{placeholders}} after rendering: $(grep -oE '\{\{[a-z_]+\}\}' "$TMP/$name" | sort -u | tr '\n' ' ')"; fi
    dest="$ROOT/.github/workflows/$name"
    if [ -f "$dest" ] && { grep -qF "$OTHER_ACTION" "$dest" || grep -qF "$OTHER_CLI" "$dest"; }; then
      # an sdlc workflow generated with the other auth: merge only the auth line (the rest of the file is left as the user has it)
      if [ "$DRY" = 0 ]; then python3 - "$dest" "$OTHER_ACTION" "$ACTION_AUTH" "$OTHER_CLI" "$CLI_AUTH" <<'PY'
import sys
p,oa,na,oc,nc=sys.argv[1:6]; s=open(p,encoding='utf-8').read(); open(p,'w',encoding='utf-8').write(s.replace(oa,na).replace(oc,nc))
PY
      fi
      r_merged ".github/workflows/$name" "CI auth line $OTHER_SECRET → $SECRET_NAME (ci.auth=$AUTH_MODE)"; continue
    fi
    put_file ".github/workflows/$name" "$TMP/$name"
  done
  if [ -f "$SDLC_TEMPLATES/github/CODEOWNERS" ]; then
    py_render "$SDLC_TEMPLATES/github/CODEOWNERS" "$TMP/CODEOWNERS" "team=$TEAM" "tech_lead=$TEAM" "platform_team=$TEAM" "product_owner=$TEAM" \
      "intent_dir=$INTENT_DIR" "spec_dir=$SPEC_DIR" "plan_dir=$PLAN_DIR" "evals_dir=$EVALS_DIR"
    put_file ".github/CODEOWNERS" "$TMP/CODEOWNERS" "owners set to $TEAM — replace per line"
  fi
fi

# ---------- summary ----------
printf '\nSUMMARY  created=%d merged=%d skipped=%d%s\n' "$N_CREATED" "$N_MERGED" "$N_SKIPPED" "$([ "$DRY" = 1 ] && printf ' (dry run — nothing written)')"
if [ "$DRY" = 0 ] && [ "$IS_GIT" = 0 ]; then
  printf 'NOTE  not a git repository — run `git init` so approvals (status: approved commits) become the audit trail.\n'
fi
printf '\nNext steps (playbook dependency order):\n'
if [ "$DRY" = 1 ] && [ "$CONFIG_EXISTS" = 0 ]; then
  printf '  (dry run — run without --dry-run, then `bash scripts/doctor.sh` for the live list)\n'
else
  sdlc_next_steps "$ROOT" | sed 's/^/  /'
fi
exit 0
