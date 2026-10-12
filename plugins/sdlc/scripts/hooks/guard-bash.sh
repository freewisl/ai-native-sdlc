#!/bin/bash
# PreToolUse Bash — in order: (1) production deploy gate (one tier: a command matching protect.deploy_patterns needs the
# env var protect.deploy_approval_env), (2) no direct push to the default branch, (3) existing test files stay untouched
# while they are locked (rm, mv, cp, sed -i, redirects, git rm/checkout/restore … on an existing test file),
# (4) secret scan of the diff on `git commit`. Uninitialized project → only (1), with the built-in patterns.
# Reads: protect.{deploy_patterns,deploy_approval_env,block_direct_push,default_branch,protect_tests,test_paths}, paths.work,
#        secrets.scan_commits; a version-1 config's environments.production.{patterns,approval_env}
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
cmd=$(json_get "$input" .tool_input.command)
[ -z "$cmd" ] && exit 0
cmd_short=$(trunc "$(one_line "$cmd")" 100)
initialized=false; sdlc_initialized "$root" && initialized=true

# (1) production deploy gate — one tier, no per-environment autonomy and no rollback exemption
deploy_gate_config "$root"
if matched=$(deploy_match "$cmd"); then
  approval_env="$SDLC_DEPLOY_ENV"; approval="${!approval_env:-}"
  detail="{\"pattern\":$(json_escape "$matched"),\"cmd\":$(json_escape "$cmd_short")}"
  if [ -n "$approval" ]; then
    log_event "$root" hook.gate allow "production: approved via $approval_env" "$detail"
  else
    log_event "$root" hook.gate deny "production: no $approval_env" "$detail"
    block "'$cmd_short' matches a production deploy pattern ('$matched'). Why: production deploys need a release authorization (protect.deploy_patterns). To proceed: set $approval_env=<ticket> (release manager) and retry. If this command only reads (grep/cat/log), rephrase it or narrow protect.deploy_patterns in .sdlc/config.json."
  fi
fi

# (2) no direct path to the default branch — branch protection in a hook, for repositories where GitHub rulesets are unavailable.
#     Parses each `git push` clause (split on && ; | ||): refspecs (+, refs/heads/, src:dst), --all/--mirror, --delete <default>,
#     bare `git push` while on the default branch, `git -C <dir> push`. Feature-branch pushes and PR creation are allowed.
push_hits_default() { # push_hits_default <clause> <default-branch> <current-branch> → prints a reason and returns 0 when the clause pushes to the default branch
  local def="$2" cur="$3" tok seen_git=0 seen_push=0 skipnext=0 remote="" refspecs="" all=0 delete=0 r dst
  # shellcheck disable=SC2086
  set -- $1
  for tok in "$@"; do
    if [ $seen_git = 0 ]; then case "$tok" in git|*/git) seen_git=1;; esac; continue; fi
    if [ $seen_push = 0 ]; then
      if [ $skipnext = 1 ]; then skipnext=0; continue; fi
      case "$tok" in push) seen_push=1;; -C|-c|--git-dir|--work-tree|--namespace|--exec-path) skipnext=1;; -*) ;; *) return 1;; esac
      continue
    fi
    if [ $skipnext = 1 ]; then skipnext=0; continue; fi
    case "$tok" in
      --all|--mirror|--branches) all=1;;
      --delete|-d) delete=1;;
      --repo|--receive-pack|--exec|--push-option|-o) skipnext=1;;
      -*) ;;
      *) if [ -z "$remote" ]; then remote="$tok"; else refspecs="$refspecs $tok"; fi;;
    esac
  done
  [ $seen_push = 1 ] || return 1
  [ $all = 1 ] && { printf 'pushes every branch (--all/--mirror), including %s' "$def"; return 0; }
  for r in $refspecs; do
    r="${r#+}"; case "$r" in *:*) dst="${r#*:}";; *) dst="$r";; esac
    dst="${dst#refs/heads/}"
    if [ "$dst" = "$def" ]; then
      if [ $delete = 1 ]; then printf 'deletes the default branch %s' "$def"; else printf 'refspec targets %s' "$def"; fi; return 0
    fi
  done
  if [ -z "$refspecs" ] && [ $delete = 0 ] && [ "$cur" = "$def" ]; then printf 'bare push while on %s' "$def"; return 0; fi
  return 1
}
if [ "$initialized" = true ] && [ "$(cfg_bool "$root" .protect.block_direct_push true)" = "true" ]; then
  case "$cmd" in *git*push*|*push*)
    def=$(git -C "$root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##'); [ -z "$def" ] && def=$(cfg "$root" .protect.default_branch main)
    cur=$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null)
    reason=""; old_ifs="$IFS"; IFS=$'\n'
    for clause in $(printf '%s\n' "$cmd" | sed -E 's/&&|\|\||;|\|/\n/g'); do
      IFS="$old_ifs"
      if r=$(push_hits_default "$clause" "$def" "$cur"); then reason="$r"; break; fi
      IFS=$'\n'
    done
    IFS="$old_ifs"
    if [ -n "$reason" ]; then
      log_event "$root" hook.guard deny direct_push "{\"cmd\":$(json_escape "$cmd_short"),\"branch\":\"$def\",\"reason\":$(json_escape "$reason")}"
      block "pushing to the default branch '$def' ($reason: '$cmd_short'). Why: anything the agent writes arrives as a pull request through branch protection — there is no direct path to the default branch (protect.block_direct_push). To proceed: push a feature branch and open a PR (git push -u origin <branch> && gh pr create) — /sdlc:go and /sdlc:run do this and merge through the gate; to allow direct pushes in this repository set protect.block_direct_push: false."
    fi ;;
  esac
fi

# (3) existing test files (already on the default branch) stay untouched while they are locked (same rule as guard-edit:
#     lib/hooks-extra.sh); a test this change adds is free.
#     A tokenizer (awk) turns the command line into records — W<word> (quotes and escapes resolved), O<path> (the target of an
#     output redirection) and C (end of a simple command: ; & | && || newline ( ) `) — heredoc bodies and comments skipped.
#     Each simple command is then read for what it does to an existing test file. Best effort, not a shell: no variables,
#     no brace expansion, no `find -exec`/`xargs`, nothing that runs another program that writes (python -c, patch, git apply).
#     A path that does not exist yet is allowed.
TL_AWK='
BEGIN { RS = "\001"; SQ = "\047"; DQ = "\042" }
function ws(c) { return c == " " || c == "\t" || c == "\r" }
function stop(c) {
  return c == " " || c == "\t" || c == "\r" || c == "\n" || c == ";" || c == "|" || c == "&" || c == "(" || c == ")" || c == "<" || c == ">" || c == "`"
}
function readword(   w, c, d, e, rest, j) {
  w = ""
  while (i <= n) {
    c = substr(s, i, 1)
    if (stop(c)) break
    if (c == "\\") {
      d = substr(s, i + 1, 1)
      if (d == "\n") { i += 2; continue }
      w = w d; i += 2; continue
    }
    if (c == SQ) {
      i++
      rest = substr(s, i); j = index(rest, SQ)
      if (j == 0) { w = w rest; i = n + 1 } else { w = w substr(rest, 1, j - 1); i += j }
      continue
    }
    if (c == DQ) {
      i++
      while (i <= n) {
        rest = substr(s, i, 256)                        # plain text is taken a chunk at a time
        j = match(rest, /["\\]/)
        if (j == 0) { w = w rest; i += length(rest); continue }
        w = w substr(rest, 1, j - 1); i += j - 1
        d = substr(s, i, 1)
        if (d == DQ) break
        e = substr(s, i + 1, 1)
        if (e == "\n") { i += 2; continue }
        if (e == "$" || e == "`" || e == DQ || e == "\\") { w = w e; i += 2; continue }
        w = w d; i++
      }
      i++; continue
    }
    w = w c; i++
  }
  gsub(/\n/, " ", w)
  return w
}
function skipws() { while (i <= n && ws(substr(s, i, 1))) i++ }
function skipbodies(   k, d, wrapped, j) {
  for (k = 1; k <= nh; k++) {
    if (i > n) break
    d = hd[k]
    wrapped = "\n" substr(s, i) "\n"
    if (!hdash[k]) {
      j = index(wrapped, "\n" d "\n")
      if (j > 0) { i = i + j + length(d); continue }
    } else if (d ~ /^[A-Za-z0-9_]+$/) {
      if (match(wrapped, "\n\t*" d "\n")) { i = i + RSTART + RLENGTH - 2; continue }
    } else {
      while (i <= n) {
        j = index(substr(s, i), "\n")
        if (j == 0) { wrapped = substr(s, i); i = n + 1 } else { wrapped = substr(s, i, j - 1); i += j }
        sub(/^\t+/, "", wrapped)
        if (wrapped == d) break
      }
      continue
    }
    i = n + 1
  }
  nh = 0
}
{
  s = $0; n = length(s); i = 1; nh = 0
  while (i <= n) {
    c = substr(s, i, 1)
    if (ws(c)) { i++; continue }
    if (c == "\n") { i++; print "C"; if (nh > 0) skipbodies(); continue }
    if (c == "#") { j = index(substr(s, i), "\n"); if (j == 0) i = n + 1; else i += j - 1; continue }
    if (c == ";" || c == "(" || c == ")" || c == "`") { i++; print "C"; continue }
    if (c == "|") { i++; d = substr(s, i, 1); if (d == "|" || d == "&") i++; print "C"; continue }
    if (c == "&") {
      d = substr(s, i + 1, 1)
      if (d == ">") {
        i += 2; if (substr(s, i, 1) == ">") i++
        skipws(); t = readword(); print "O" t; continue
      }
      i++; if (d == "&") i++
      print "C"; continue
    }
    if (c == ">") {
      i++; d = substr(s, i, 1)
      if (d == ">" || d == "|") i++
      else if (d == "&") {
        e = substr(s, i + 1, 1)
        if (e ~ /[0-9-]/) { i++; while (i <= n && substr(s, i, 1) ~ /[0-9-]/) i++; continue }
        i++
      }
      else if (d == "(") continue
      skipws(); t = readword(); print "O" t; continue
    }
    if (c == "<") {
      i++; d = substr(s, i, 1)
      if (d == "<") {
        i++
        if (substr(s, i, 1) == "<") { i++; skipws(); t = readword(); continue }
        dash = 0; if (substr(s, i, 1) == "-") { dash = 1; i++ }
        skipws(); t = readword()
        nh++; hd[nh] = t; hdash[nh] = dash
        continue
      }
      if (d == "(") continue
      if (d == "&") i++
      skipws(); t = readword(); continue
    }
    p = i
    w = readword()
    if (i == p) { i++; continue }
    d = substr(s, i, 1)
    if (w ~ /^[0-9]+$/ && (d == ">" || d == "<")) continue
    print "W" w
  }
}
'
TL_BASE=""; TL_ROOT=""; TL_ROOT_PHYS=""; TL_READY=""; TL_ON=0; TL_ACTION=""; TL_REL=""; TL_GLOBS=0; TL_NEW_CHECKS=0; NORM=""; ABS=""; REL=""; OPS=()
TL_MAX_GLOB_WORDS=20; TL_MAX_GLOB_FAST=2000; TL_MAX_GLOB_SLOW=300     # bounds: a command line costs the same whatever a glob expands to
TL_MAX_NEW_CHECKS=30                                                    # test files looked up in git per command line (one git call each)

# tl_interesting <command> → cheap pre-check: could this command line change a file at all?
tl_interesting() {
  case "$1" in
    *'>'*|*rm*|*mv*|*cp*|*ln*|*dd*|*sed*|*perl*|*truncate*|*tee*|*shred*|*unlink*|*checkout*|*restore*|*'-c '*|*eval*|*install*) return 0;;
  esac
  return 1
}
# norm_path <absolute path> → NORM: '.', '..' and doubled slashes collapsed textually (symlinks are not resolved)
norm_path() {
  local p="$1" out="" seg
  case "$p" in */|*/.|*/./*|*/..|*/../*|*//*) ;; *) NORM="${p:-/}"; return;; esac     # already clean: the usual case
  while [ -n "$p" ]; do
    p="${p#/}"
    seg="${p%%/*}"
    case "$p" in */*) p="${p#*/}";; *) p="";; esac
    case "$seg" in ''|.) ;; ..) out="${out%/*}";; *) out="$out/$seg";; esac
  done
  NORM="${out:-/}"
}
# tl_abs <word> → ABS: the absolute path a word names (relative words resolve against the working directory TL_BASE)
tl_abs() {
  local w="$1"
  case "$w" in /*) ;; '~') w="$HOME";; '~/'*) w="$HOME/${w#'~/'}";; *) w="$TL_BASE/$w";; esac
  norm_path "$w"; ABS="$NORM"
}
# tl_rel <abs> → REL: the path inside the project ('.' = the root itself); returns 1 for a path outside it
tl_rel() {
  case "$1" in
    "$TL_ROOT") REL=".";;
    "$TL_ROOT"/*) REL="${1#"$TL_ROOT"/}";;
    "$TL_ROOT_PHYS") REL=".";;
    "$TL_ROOT_PHYS"/*) REL="${1#"$TL_ROOT_PHYS"/}";;
    *) return 1;;
  esac
}
# tl_prepare → once per command, at the first path that could be a test file: the lock state and the test paths
tl_prepare() {
  TL_READY=1; TL_ON=0
  test_lock_on "$root" || return 0
  test_paths_load "$root"
  TL_ON=1
}
# tl_dir_test <rel dir> → prints one existing test file under the directory and returns 0. Only files git knows count — the
# ones in the base (tracked ones without a base): build output and dependency folders are not tests, and a test this change adds is free.
tl_dir_test() {
  local f listing
  git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 1
  test_base_rev "$root"
  if [ "$SDLC_TEST_BASE" != "-" ]; then
    listing=$(git -C "$root" -c core.quotepath=off ls-tree -r --name-only "$SDLC_TEST_BASE" -- "$1" 2>/dev/null | head -n 3000)
  else
    listing=$(git -C "$root" -c core.quotepath=off ls-files -- "$1" 2>/dev/null | head -n 3000)
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if is_test_path "$f" && [ -e "$root/$f" ]; then printf '%s' "$f"; return 0; fi
  done <<< "$listing"
  return 1
}
# tl_hit_at <action> <abs> <rel> <dir-aware 0|1> → 0 (and TL_ACTION/TL_REL) when <abs>, which lies at <rel> inside the
# project, is an existing locked test file (dir-aware: …or a directory holding one — for commands that delete or move whole trees)
tl_hit_at() {
  local action="$1" abs="$2" rel="$3" dirs="$4" f
  if [ -f "$abs" ] || [ -L "$abs" ]; then
    [ -n "$TL_READY" ] || tl_prepare
    [ "$TL_ON" = 1 ] || return 1
    is_test_path "$rel" || return 1
    if [ "$TL_NEW_CHECKS" -lt "$TL_MAX_NEW_CHECKS" ]; then       # one git call per test file: bounded for a glob over many tests
      TL_NEW_CHECKS=$((TL_NEW_CHECKS + 1))
      test_file_is_new "$root" "$rel" && return 1
    fi
    TL_ACTION="$action"; TL_REL="$rel"; return 0
  fi
  if [ "$dirs" = 1 ] && [ -d "$abs" ]; then
    [ -n "$TL_READY" ] || tl_prepare
    [ "$TL_ON" = 1 ] || return 1
    test_base_rev "$root"                                        # resolved here, not inside the substitution below, so it stays cached
    f=$(tl_dir_test "$rel") || return 1
    TL_ACTION="$action, the directory holds it"; TL_REL="$f"; return 0
  fi
  return 1
}
# tl_hit1 <action> <path word> <dir-aware 0|1> — one path word, resolved against the working directory
tl_hit1() {
  tl_abs "$2"; tl_rel "$ABS" || return 1
  tl_hit_at "$1" "$ABS" "$REL" "$3"
}
# tl_hit <action> <path word> <dir-aware 0|1> — tl_hit1 after glob expansion (pathname expansion only; the word is never
# evaluated). Expansions are capped: a glob that matches thousands of files is not read to the end.
tl_hit() {
  local action="$1" word="$2" dirs="$3" c exp brel max fast=0
  [ -n "$word" ] || return 1
  case "$word" in
    *[*?[]*)
      TL_GLOBS=$((TL_GLOBS + 1)); [ "$TL_GLOBS" -le "$TL_MAX_GLOB_WORDS" ] || return 1
      case "$word" in
        /*|'~'*|.|..|./*|../*|*/.|*/..|*/./*|*/../*|*//*) max=$TL_MAX_GLOB_SLOW; brel="";;      # needs path arithmetic per entry
        *) max=$TL_MAX_GLOB_FAST; fast=1                                                      # plain relative names: one prefix for all
           case "$TL_BASE" in
             "$TL_ROOT"|"$TL_ROOT_PHYS") brel="";;
             "$TL_ROOT"/*) brel="${TL_BASE#"$TL_ROOT"/}/";;
             "$TL_ROOT_PHYS"/*) brel="${TL_BASE#"$TL_ROOT_PHYS"/}/";;
             *) return 1;;                                                                    # the working directory is outside the project
           esac;;
      esac
      exp=$(cd "$TL_BASE" 2>/dev/null && IFS='' && set +f && n=0 && for c in $word; do
              printf '%s\n' "$c"; n=$((n + 1)); [ "$n" -lt "$max" ] || break
            done)
      [ -n "$exp" ] || exp="$word"
      while IFS= read -r c; do
        if [ "$fast" = 1 ]; then tl_hit_at "$action" "$TL_BASE/$c" "$brel$c" "$dirs" && return 0
        else tl_hit1 "$action" "$c" "$dirs" && return 0; fi
      done <<< "$exp"
      return 1;;
  esac
  tl_hit1 "$action" "$word" "$dirs"
}
# tl_ops <start> → OPS: the non-option words from index <start> on; after -- every word is an operand
tl_ops() {
  local k="$1" w endopts=0
  OPS=()
  while [ "$k" -lt "${#W[@]}" ]; do
    w="${W[$k]}"; k=$((k + 1))
    if [ "$endopts" = 1 ]; then OPS[${#OPS[@]}]="$w"
    elif [ "$w" = "--" ]; then endopts=1
    else case "$w" in -?*) ;; *) OPS[${#OPS[@]}]="$w";; esac
    fi
  done
}
# tl_ops_hit <action> <start> <dir-aware 0|1> → 0 when any operand is a hit
tl_ops_hit() {
  local i=0
  tl_ops "$2"
  while [ "$i" -lt "${#OPS[@]}" ]; do
    if tl_hit "$1" "${OPS[$i]}" "$3"; then return 0; fi
    i=$((i + 1))
  done
  return 1
}
# tl_inplace <sed|gsed|perl> <start> → 0 when an option word asks for in-place editing (-i, -pi, -i.bak, --in-place)
tl_inplace() {
  local tool="$1" k="$2" w rest
  while [ "$k" -lt "${#W[@]}" ]; do
    w="${W[$k]}"; k=$((k + 1))
    [ "$w" = "--" ] && return 1
    case "$w" in
      --in-place|--in-place=*) [ "$tool" = perl ] || return 0;;
      --*) ;;
      -*i*) rest="${w%%i*}"; rest="${rest#-}"
            if [ "$tool" = perl ]; then case "$rest" in *[!nplawsSTtuUvWXxcdD0-9]*) ;; *) return 0;; esac
            else case "$rest" in *[!nErszub]*) ;; *) return 0;; esac; fi;;
    esac
  done
  return 1
}
# tl_cpmv <cp|mv|ln|install> <start> → 0 when it overwrites (or, for mv, moves away) an existing locked test file
tl_cpmv() {
  local verb="$1" k="$2" w tdir="" endopts=0 nops dest i src
  OPS=()
  while [ "$k" -lt "${#W[@]}" ]; do
    w="${W[$k]}"; k=$((k + 1))
    if [ "$endopts" = 1 ]; then OPS[${#OPS[@]}]="$w"; continue; fi
    case "$w" in
      --) endopts=1;;
      -t) if [ "$k" -lt "${#W[@]}" ]; then tdir="${W[$k]}"; k=$((k + 1)); fi;;
      --target-directory=*) tdir="${w#*=}";;
      -?*) ;;
      *) OPS[${#OPS[@]}]="$w";;
    esac
  done
  nops=${#OPS[@]}
  if [ -n "$tdir" ]; then dest="$tdir"; else
    [ "$nops" -ge 2 ] || return 1
    dest="${OPS[$((nops - 1))]}"; nops=$((nops - 1))
  fi
  if [ "$verb" = mv ]; then                      # moving a test away changes it just as much as replacing it
    i=0; while [ "$i" -lt "$nops" ]; do tl_hit "mv" "${OPS[$i]}" 1 && return 0; i=$((i + 1)); done
  fi
  tl_abs "$dest"
  if [ -d "$ABS" ]; then                         # a directory receives the sources under their own names
    i=0; while [ "$i" -lt "$nops" ]; do
      src="${OPS[$i]}"; src="${src%/}"
      tl_hit "$verb onto it" "$dest/${src##*/}" 0 && return 0
      i=$((i + 1))
    done
    return 1
  fi
  tl_hit "$verb onto it" "$dest" 0
}
# tl_git <index after "git"> → 0 when the git command changes an existing locked test file (git rm/mv, checkout/restore of a file)
tl_git() {
  local k="$1" nw=${#W[@]} a sub="" saved="$TL_BASE" i staged=0 worktree=0 rc=1
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      -C) if [ "$k" -lt "$nw" ]; then tl_abs "${W[$k]}"; TL_BASE="$ABS"; k=$((k + 1)); fi;;
      -c|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix|--config-env) k=$((k + 1));;
      -*) ;;
      *) sub="$a"; break;;
    esac
  done
  case "$sub" in
    rm|mv) tl_ops_hit "git $sub" "$k" 1 && rc=0;;
    checkout|restore)
      i=$k
      while [ "$i" -lt "$nw" ]; do
        case "${W[$i]}" in --staged|-S) staged=1;; --worktree|-W) worktree=1;; --) break;; esac
        i=$((i + 1))
      done
      # `git restore --staged <file>` only touches the index, never the file
      if [ "$sub" = restore ] && [ "$staged" = 1 ] && [ "$worktree" = 0 ]; then :
      else tl_ops_hit "git $sub" "$k" 0 && rc=0; fi;;
  esac
  TL_BASE="$saved"
  return "$rc"
}
# tl_clause <depth> — one simple command (words W[], redirection targets R[]); 0 = it changes an existing locked test file
tl_clause() {
  local depth="$1" nw=${#W[@]} k=0 r=0 w base wrap=0 i str
  while [ "$r" -lt "${#R[@]}" ]; do
    if tl_hit "output redirection" "${R[$r]}" 0; then return 0; fi
    r=$((r + 1))
  done
  while [ "$k" -lt "$nw" ]; do                   # skip VAR=value prefixes, shell keywords and wrappers (sudo, env, time …)
    w="${W[$k]}"
    case "$w" in
      '!'|'{'|'}'|if|then|else|elif|fi|do|done|while|until|time) wrap=0;;
      command|builtin|exec|nohup|sudo|doas|env|nice|setsid|stdbuf) wrap=1;;
      timeout) wrap=2;;
      -*) [ "$wrap" -ge 1 ] || break;;
      *=*) valid_ident "${w%%=*}" || break;;
      *) if [ "$wrap" = 2 ]; then wrap=1; else break; fi;;
    esac
    k=$((k + 1))
  done
  [ "$k" -lt "$nw" ] || return 1
  base="${W[$k]##*/}"; k=$((k + 1))
  case "$base" in
    rm|unlink|shred) tl_ops_hit "delete ($base)" "$k" 1 && return 0;;
    cp|mv|ln|install) tl_cpmv "$base" "$k" && return 0;;
    tee) tl_ops_hit "overwrite (tee)" "$k" 0 && return 0;;
    truncate) tl_ops_hit "truncate" "$k" 0 && return 0;;
    sed|gsed|perl) if tl_inplace "$base" "$k"; then tl_ops_hit "in-place edit ($base -i)" "$k" 0 && return 0; fi;;
    dd) i=$k
        while [ "$i" -lt "$nw" ]; do
          case "${W[$i]}" in of=*) tl_hit "overwrite (dd)" "${W[$i]#of=}" 0 && return 0;; esac
          i=$((i + 1))
        done;;
    cd) tl_ops "$k"
        if [ "${#OPS[@]}" -eq 0 ]; then TL_BASE="$HOME"
        elif [ "${OPS[0]}" != "-" ]; then tl_abs "${OPS[0]}"; [ -d "$ABS" ] && TL_BASE="$ABS"; fi;;
    git) tl_git "$k" && return 0;;
    bash|sh|zsh|dash|ksh)                        # bash -c '<commands>' is read like the command line itself
      i=$k
      while [ "$i" -lt "$nw" ]; do
        case "${W[$i]}" in
          -c|-[a-z]c|-[a-z][a-z]c)
            i=$((i + 1))
            if [ "$i" -lt "$nw" ] && [ "$depth" -lt 3 ]; then tl_scan "${W[$i]}" $((depth + 1)) && return 0; fi
            break;;
        esac
        i=$((i + 1))
      done;;
    eval) str=""; i=$k
          while [ "$i" -lt "$nw" ]; do str="$str ${W[$i]}"; i=$((i + 1)); done
          if [ "$depth" -lt 3 ] && [ -n "$str" ]; then tl_scan "$str" $((depth + 1)) && return 0; fi;;
  esac
  return 1
}
# tl_scan <command> <depth> → 0 when one of its simple commands changes an existing locked test file
tl_scan() {
  local depth="$2" rec
  local -a W=() R=()
  while IFS= read -r rec; do
    case "$rec" in
      W*) W[${#W[@]}]="${rec#W}";;
      O*) R[${#R[@]}]="${rec#O}";;
      C) if tl_clause "$depth"; then return 0; fi; W=(); R=();;
    esac
  done <<< "$(printf '%s' "$1" | awk "$TL_AWK" 2>/dev/null)"
  tl_clause "$depth"
}

if [ "$initialized" = true ] && tl_interesting "$cmd"; then
  cwd=$(json_get "$input" .cwd)
  norm_path "$root"; TL_ROOT="$NORM"
  TL_ROOT_PHYS=$(cd "$root" 2>/dev/null && pwd -P); [ -n "$TL_ROOT_PHYS" ] || TL_ROOT_PHYS="$TL_ROOT"
  TL_BASE="$TL_ROOT"; if [ -n "$cwd" ] && [ -d "$cwd" ]; then norm_path "$cwd"; TL_BASE="$NORM"; fi
  if tl_scan "$cmd" 0; then
    log_event "$root" hook.guard deny test_lock_bash "{\"cmd\":$(json_escape "$cmd_short"),\"path\":$(json_escape "$TL_REL"),\"action\":$(json_escape "$TL_ACTION")}"
    doc=$(active_work_file "$root"); doc_hint="${doc:-the work doc of this change ($(work_dir "$root")/<slug>.md)}"
    block "'$cmd_short' would change the existing test file '$TL_REL' ($TL_ACTION). Why: existing tests (those already on the default branch) are locked — fix the code, not the test (protect.protect_tests) — and the lock covers shell commands as well as edits; test files this change adds stay free to edit. To proceed, only if the test itself is wrong or the new behavior needs it changed: that is a design decision — set 'test_changes: allowed' in $doc_hint when designing the change; a build session asks a person instead of flipping it itself. As a last resort the user runs the session with SDLC_ALLOW_TEST_EDIT=1."
  fi
fi

# (4) secrets in the changes being committed
if [ "$initialized" = true ] && [ "$(cfg_bool "$root" .secrets.scan_commits true)" = "true" ]; then
  if printf '%s\n' "$cmd" | grep -E -q '(^|[^[:alnum:]_./-])git([[:space:]]+-[^[:space:]]+)*[[:space:]]+commit([[:space:]]|$)'; then
    all=""
    printf '%s\n' "$cmd" | grep -E -q -e '(^|[[:space:]])--all([[:space:]]|$)' -e '(^|[[:space:]])-[A-Za-z]*a[A-Za-z]*([[:space:]]|$)' && all="--all"
    scan_root="$root"
    git -C "$scan_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || scan_root=$(json_get "$input" .cwd)
    hits=""; [ -n "$scan_root" ] && hits=$(bash "$here/../secret-scan.sh" --staged "$scan_root" $all 2>/dev/null)
    if [ -n "$hits" ]; then
      log_event "$root" hook.guard deny commit_secret "{\"cmd\":$(json_escape "$cmd_short"),\"hits\":$(json_escape "$(one_line "$hits")")}"
      block "possible secret in the changes being committed ($(one_line "$hits")). Why: credentials never enter version control (secrets.scan_commits). To proceed: take the value out of the diff (git restore --staged <file>, then edit it to read from the environment or a secret manager) and commit again."
    fi
  fi
fi
exit 0
