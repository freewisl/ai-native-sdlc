#!/bin/bash
# PreToolUse Edit|Write|MultiEdit|NotebookEdit — in order: (1) frozen/generated paths, (2) test-file lock,
# (3) approved-plan requirement, (4) secret scan of the new text (design §3). Uninitialized project → (4) only.
# Reads: protect.frozen_paths_file, protect.generated_paths, protect.protect_tests, protect.test_paths, paths.spec, paths.evals,
#        plan.require_for_edits, plan.exempt_paths, secrets.scan_edits, secrets.extra_patterns (via secret-scan.sh)
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
tool=$(json_get "$input" .tool_name)
path=$(json_get "$input" .tool_input.file_path)
[ -z "$path" ] && path=$(json_get "$input" .tool_input.notebook_path)
[ -z "$path" ] && exit 0
rel=$(rel_path "$root" "$path")

deny() { # deny <rule> <message>  — audit log + exit 2
  log_event "$root" hook.guard deny "$1" "{\"tool\":$(json_escape "$tool"),\"path\":$(json_escape "$rel")}"
  block "$2"
}

# built-in test-path set (§2.1 "auto"); <paths.spec> itself is the artifact dir, not a test dir
auto_test_path() { # <rel> <spec-dir>
  local rel="$1" spec="${2%/}" base; base=$(basename "$rel")
  case "/$rel" in */test/*|*/tests/*|*/__tests__/*) return 0;; esac
  case "$rel" in "$spec"/*) ;; *) case "/$rel" in */spec/*) return 0;; esac;; esac
  case "$base" in
    *_test.go|*.test.*|*.spec.*|test_*.py|*_test.py|*Test.java|*Tests.java|*Spec.scala|*_spec.rb) return 0;;
  esac
  return 1
}
is_test_path() { # <root> <rel>
  local pat spec; spec=$(cfg "$1" .paths.spec spec)
  while IFS= read -r pat; do
    pat=$(trim "$pat"); [ -z "$pat" ] && continue
    if [ "$pat" = "auto" ]; then auto_test_path "$2" "$spec" && return 0
    else glob_match "$pat" "$2" && return 0; fi
  done <<< "$(cfg_lines "$1" .protect.test_paths)"
  return 1
}
test_edit_allowed() { # <root>
  [ "${SDLC_ALLOW_TEST_EDIT:-}" = "1" ] && return 0
  [ -f "$(state_dir "$1")/allow-test-edit" ] && return 0
  local p; p=$(active_plan_file "$1")
  [ -n "$p" ] && [ -f "$1/$p" ] && [ "$(frontmatter_get "$1/$p" test_changes)" = "allowed" ] && return 0
  return 1
}
is_exempt_path() { # <root> <rel>
  local pat
  while IFS= read -r pat; do
    pat=$(trim "$pat"); [ -z "$pat" ] && continue
    glob_match "$pat" "$2" && return 0
  done <<< "$(cfg_lines "$1" .plan.exempt_paths)"
  return 1
}

initialized=false; sdlc_initialized "$root" && initialized=true

if [ "$initialized" = true ]; then
  # (1) frozen paths file + generated paths
  frozen_cfg=$(cfg "$root" .protect.frozen_paths_file ".sdlc/frozen-paths.txt")
  frozen_file="$frozen_cfg"; case "$frozen_file" in /*) ;; *) frozen_file="$root/$frozen_file";; esac
  if [ -f "$frozen_file" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      line=$(trim "${line%%#*}"); [ -z "$line" ] && continue
      if glob_match "$line" "$rel"; then
        deny frozen_path "protected path '$rel' (matches '$line' in $frozen_cfg). Why: frozen files are not edited by hand — edit the source of truth instead. To proceed: to change this list edit $frozen_cfg (with the owner's approval)."
      fi
    done < "$frozen_file"
  fi
  while IFS= read -r pat; do
    pat=$(trim "$pat"); [ -z "$pat" ] && continue
    if glob_match "$pat" "$rel"; then
      deny generated_path "protected path '$rel' (matches '$pat' in protect.generated_paths). Why: generated files are overwritten by their generator — edit the source of truth and re-generate instead. To proceed: to change this list edit protect.generated_paths in .sdlc/config.json."
    fi
  done <<< "$(cfg_lines "$root" .protect.generated_paths)"

  # (1b) ticketed paths — migrations, infra: edits need a change ticket (playbook Stage 5 "block edits to migrations and infra without a change ticket")
  ticket_env=$(cfg "$root" .protect.ticket_env CHANGE_TICKET); case "$ticket_env" in ''|*[!A-Za-z0-9_]*) ticket_env=CHANGE_TICKET;; esac
  while IFS= read -r pat; do
    pat=$(trim "$pat"); [ -z "$pat" ] && continue
    if glob_match "$pat" "$rel" && [ -z "${!ticket_env:-}" ]; then
      deny ticketed_path "editing '$rel' needs a change ticket (matches '$pat' in protect.ticketed_paths). Why: migrations and infrastructure changes are gated by change management. To proceed: get the change ticket approved and run the session with $ticket_env=<ticket id>; the ticket id is logged with the edit."
    fi
  done <<< "$(cfg_lines "$root" .protect.ticketed_paths)"

  # (2) test-file lock
  if [ "$(cfg_bool "$root" .protect.protect_tests true)" = "true" ] && is_test_path "$root" "$rel"; then
    if ! test_edit_allowed "$root"; then
      plan_rel=$(active_plan_file "$root"); plan_hint="${plan_rel:-the approved plan}"
      deny test_lock "test file '$rel' is locked. Why: during a fix the code changes, not the test (protect.protect_tests) — fix the code, not the test. To proceed, only if the test itself is wrong or new behavior needs new tests: (1) set 'test_changes: allowed' in $plan_hint (a plan change the engineer approves), or (2) let /sdlc:verify --bugfix manage .sdlc/state/allow-test-edit for the write-the-failing-test step; as a last resort the user runs the session with SDLC_ALLOW_TEST_EDIT=1."
    fi
  fi

  # (3) approved plan required
  if [ "$(cfg_bool "$root" .plan.require_for_edits false)" = "true" ] && ! is_exempt_path "$root" "$rel"; then
    plan_rel=$(active_plan_file "$root"); status=""
    [ -n "$plan_rel" ] && [ -f "$root/$plan_rel" ] && status=$(frontmatter_get "$root/$plan_rel" status)
    if [ "$status" != "approved" ]; then
      if [ -n "$plan_rel" ]; then why="the active plan $plan_rel has status '${status:-missing}'"; else why="there is no active plan"; fi
      deny no_approved_plan "editing '$rel' without an approved plan ($why). Why: plan.require_for_edits is on — nothing is implemented without an approved plan. To proceed: run /sdlc:plan (writes $(cfg "$root" .paths.plan plan)/<slug>.md with status: approved and sets .sdlc/state/active-plan); paths listed in plan.exempt_paths are not affected."
    fi
  fi
fi

# (4) secret scan — also for projects that have not adopted sdlc yet
scan=true; [ "$initialized" = true ] && scan=$(cfg_bool "$root" .secrets.scan_edits true)
evals_dir=evals; [ "$initialized" = true ] && evals_dir=$(cfg "$root" .paths.evals evals)
case "$rel" in "${evals_dir%/}"/*) scan=false;; esac   # eval fixtures hold intentionally fake secrets
if [ "$scan" = "true" ]; then
  text=$(edit_new_text "$input")
  if [ -n "$text" ]; then
    hits=$(printf '%s\n' "$text" | bash "$here/../secret-scan.sh" --text "$root" 2>/dev/null)
    if [ -n "$hits" ]; then
      deny secret "possible secret in the new content for '$rel' ($(one_line "$hits")). Why: credentials never enter the codebase (secrets.scan_edits). To proceed: read it from the environment or a secret manager and reference it by name; a placeholder must be obvious (contain 'example' or 'REPLACE_ME')."
    fi
  fi
fi
exit 0
