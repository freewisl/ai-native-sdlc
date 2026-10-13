#!/bin/bash
# PreToolUse Edit|Write|MultiEdit|NotebookEdit — in order: (1) frozen/generated paths, (2) test-file lock (during sdlc work
# only, i.e. while an active work doc applies: an existing test file — one already on the default branch — is locked, one
# this change adds is free; outside sdlc work no test is locked), (3) design first (only while an active work doc applies and
# is ready or proposed: then just the work dir and .sdlc/ can be edited), (4) secret scan of the new text. Uninitialized
# project → (4) only.
# Reads: protect.frozen_paths_file, protect.generated_paths, protect.protect_tests, protect.test_paths, paths.work,
#        secrets.scan_edits, secrets.extra_patterns (via secret-scan.sh), .sdlc/state/active-work (see lib/common.sh),
#        SDLC_ALLOW_TEST_EDIT (environment)
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

initialized=false; sdlc_initialized "$root" && initialized=true

if [ "$initialized" = true ]; then
  # (1) frozen paths file + generated paths (globs are read as the gate reads them: glob_match in lib/common.sh)
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

  # (2) test-file lock, during sdlc work — a test file that does not exist yet is always allowed; paths outside the project are not its tests
  case "$rel" in /*) ;; *)
    if test_file_locked "$root" "$rel" "$path"; then
      doc_hint="$(work_dir "$root")/$(active_work_slug "$root").md"
      deny test_lock "test file '$rel' is locked while $doc_hint is the active work doc. Why: existing tests (those already on the default branch) change only when the design says so — fix the code, not the test (protect.protect_tests); test files this change adds stay free to edit. To proceed, only if the test itself is wrong or the new behavior needs it changed: that is a design decision — set 'test_changes: allowed' in $doc_hint when designing the change; a build session asks a person instead of flipping it itself. As a last resort the user runs the session with SDLC_ALLOW_TEST_EDIT=1."
    fi ;;
  esac

  # (3) design first — a change is built after its design is on record: work/<slug>.md says planned
  doc=$(active_work_file "$root")
  if [ -n "$doc" ]; then
    status=$(frontmatter_get "$root/$doc" status)
    case "$status" in ready|proposed)
      wdir=$(work_dir "$root")
      case "$rel" in /*|"$wdir"/*|.sdlc/*) ;; *)
        deny design_first "design first — fill Plan and Proof, then set status planned. '$rel' cannot be edited while $doc has status '$status'. Why: a change is built only after its design is on record. To proceed: write Plan and Proof in $doc and set its status to planned (the work doc, everything under $wdir/ and .sdlc/ stay editable)." ;;
      esac ;;
    esac
  fi
fi

# (4) secret scan — also for projects that have not adopted sdlc yet
scan=true; [ "$initialized" = true ] && scan=$(cfg_bool "$root" .secrets.scan_edits true)
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
