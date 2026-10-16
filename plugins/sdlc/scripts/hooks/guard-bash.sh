#!/bin/bash
# PreToolUse Bash — in order: (1) production deploy gate (one tier: a simple command matching protect.deploy_patterns needs the
# env var protect.deploy_approval_env), (2) no direct push to the default branch, (3) what the command writes, with the same
# two rules as guard-edit: during sdlc work (an active work doc applies) an existing test file stays untouched while it is
# locked — outside sdlc work no test is locked — and while the active work doc is ready or proposed (design first) nothing
# outside the work dir and .sdlc/ is written — rm, mv, cp, sed -i, redirects, tee, git rm/checkout/restore …; (4) secret scan
# on `git commit`: the staged diff, or — with a `git add` earlier in the command line or -a/--all — what the commit could take.
# Uninitialized project → only (1), with the built-in patterns.
# (1) matches the patterns against each simple command, not the whole text: its words with quotes resolved, heredoc bodies and
# comments left out. A quoted argument counts where it is a command (bash -c "deploy prod", ssh host 'release production', eval,
# xargs, watch …); for any other program an argument that contains white space is prose (a --body, a commit message, a -k filter) and
# is left out, and so are the words of what only reads: text and read-only tools (echo, cat, grep, ls, find without -exec, sed without
# -i, awk without system() …), `gh pr|issue|run list|api` reads, a GET-only curl, kubectl/helm/terraform read verbs, test runners,
# git commands that only read or record, and the body of a heredoc that no shell reads — a tool only while nothing on the line makes
# it run a program (rg --pre, git -c, find -exec, an env prefix …). A shell is given its raw text only when it reads its script from
# standard input (`… | bash`). A command line it cannot split is matched as plain text, as before.
# (3) and (4) are best effort: the hook reads the command line, it does not run it — a program that writes on its own (python -c,
# patch, git apply, make, a package manager, curl -o …) or a path built from a variable or brace expansion is not seen.
# (2) also holds while an sdlc item is active (the work doc branch is checked out): then a session never merges — `gh pr merge` and any
# push to the default branch are refused whatever protect.block_direct_push says: the merge belongs to the shell gate.
# Reads: protect.{deploy_patterns,deploy_approval_env,block_direct_push,default_branch,protect_tests,test_paths}, paths.work,
#        secrets.scan_commits, .sdlc/state/active-work (lib/common.sh), SDLC_ALLOW_TEST_EDIT (environment); a version-1 config's
#        environments.production.{patterns,approval_env}
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/../lib/common.sh"
. "$here/../lib/hooks-extra.sh"

input=$(cat)
guard_require_json "$input"                  # empty stdin: nothing to guard; not JSON / no jq or python3: blocked (fail closed)
root=$(project_root "$input")
SDLC_SESSION_ID=$(json_get "$input" .session_id); export SDLC_SESSION_ID
cmd=$(json_get "$input" .tool_input.command)
[ -z "$cmd" ] && exit 0
cmd_short=$(trunc "$(one_line "$cmd")" 100)
initialized=false; sdlc_initialized "$root" && initialized=true

# What can deploy. A text tool only prints, searches or lists its arguments, so words that look like a deploy there are text — unless an
# option makes it run a program (deploy_text_runs); less and more are not on the list, they run what a ! line or LESSOPEN says. find,
# sed and awk are text tools too until they run something (find -exec, sed -i or an e command, awk system() or a pipe); gh, curl,
# kubectl, helm and terraform are while they only read (deploy_gh_reads, deploy_curl_reads, deploy_infra_reads) and a test runner is
# (deploy_test_runs). Of git, the commands in SDLC_GIT_LOCAL only read or record — and only while nothing on the line makes git run a
# program (deploy_git_reads); push, anything else (`git deploy`, an alias) and the commands that run what they are given (rebase --exec,
# bisect run, submodule foreach, difftool, mergetool, config alias.x '!cmd') are checked like any command. What git may be given without
# that is listed in SDLC_GIT_C_INERT (the settings of a -c: they name the committer) and SDLC_ENV_INERT (the VAR=value in front of git:
# language, time zone, terminal and colour settings, who the author and committer are, a pager that is cat or empty, an editor that is
# true or : — git starts none) — patterns, matched against the whole word.
# Any other program is checked by its words, but a word with white space in it is prose, not a command, and is left out
# (deploy_prose_text) — except where the program takes a command: a shell, ssh, eval, xargs, watch … (SDLC_RUNNERS) keep every word;
# so does the word after -c/--command/--exec …; an interpreter (python -c, node -e, perl -e, psql -c) keeps its code word only when
# the code can start a program (deploy_launches).
# A shell runs what it is given: the words of its command line and the body of a heredoc — and, when it reads its script from standard
# input (shell_reads_stdin), whatever a pipe, a here-string or a file feeds it.
SDLC_TEXT_TOOLS="echo printf cat grep egrep fgrep rg ag ack head tail wc sort uniq cut tr diff jq ls stat file du df tree pwd readlink realpath basename dirname which whereis type test [ [[ cd pushd popd true false : date uname hostname id whoami yq xq column nl od xxd hexdump strings md5sum sha1sum sha256sum shasum cksum cmp comm join paste fold fmt rev tac seq sleep"
SDLC_SHELLS="sh bash zsh dash ksh ssh"
SDLC_RUNNERS="sh bash zsh dash ksh ssh eval watch su xargs parallel script flock"
SDLC_GIT_LOCAL="commit log show diff grep checkout switch branch tag status add rm mv merge fetch pull stash remote cherry-pick restore reset revert blame clean describe ls-files rev-parse rev-list shortlog worktree apply am archive bundle clone init notes reflog format-patch cat-file show-ref name-rev for-each-ref diff-tree range-diff cherry show-branch gc fsck prune maintenance sparse-checkout"
SDLC_GIT_C_INERT="user.name=* user.email=*"
SDLC_ENV_INERT="LANG=* LC_*=* TZ=* TERM=* NO_COLOR=* COLUMNS=* GIT_PAGER=cat GIT_PAGER= PAGER=cat PAGER= GIT_EDITOR=true GIT_EDITOR=: GIT_SEQUENCE_EDITOR=true GIT_SEQUENCE_EDITOR=: GIT_AUTHOR_NAME=* GIT_AUTHOR_EMAIL=* GIT_AUTHOR_DATE=* GIT_COMMITTER_NAME=* GIT_COMMITTER_EMAIL=* GIT_COMMITTER_DATE=*"
SDLC_GH_TEXT="pr issue search status browse gist label repo auth config alias extension completion help version codespace ruleset cache secret variable org project"
DEPLOY_MAX_REC=4000                                        # records the tokenizer prints before it stops (a word, a simple command's end, a substitution each count one)
DEPLOY_MAX_OUT=40000                                       # more tokenizer output than this is matched as plain text instead
DEPLOY_WORK_MAX=1000                                       # simple commands looked at per command line — what is left is matched as plain text
DEPLOY_NESTED_MAX=16                                       # command substitutions and shell heredoc bodies looked into per command line (one awk each)
DEPLOY_WORK=0; DEPLOY_NESTED=0; DEPLOY_CAND=""; DEPLOY_TEXT=""; TL_K=0; SDLC_DEPLOY_PATTERN=""; SDLC_DEPLOY_PART=""

# deploy_cand_add <text> → one more line for DEPLOY_CAND, the text the deploy patterns are matched against
deploy_cand_add() { [ -z "$1" ] || DEPLOY_CAND="$DEPLOY_CAND$1"$'\n'; }
# in_globs <patterns, separated by spaces> <text> → 0 when the whole of <text> matches one of the patterns (* is any text)
in_globs() {
  local rest="$1 " p
  while [ -n "$rest" ]; do
    p="${rest%% *}"; rest="${rest#* }"
    case "$2" in $p) return 0;; esac
  done
  return 1
}
# deploy_text_runs → 0 when a word of the text tool in W[] (its name at TL_K) makes it run a program: rg --pre and --hostname-bin,
# ag and ack --pager, sort --compress-program (also with =) — then it is checked like any command
deploy_text_runs() {
  local k=$((TL_K + 1)) nw=${#W[@]}
  while [ "$k" -lt "$nw" ]; do
    case "${W[$k]}" in --pre|--pre=*|--pager|--pager=*|--compress-program|--compress-program=*|--hostname-bin|--hostname-bin=*) return 0;; esac
    k=$((k + 1))
  done
  return 1
}
# deploy_git_reads → 0 when the git command in W[] (its name at TL_K) only reads or records: its subcommand, past the global options
# (the ones tl_git skips), is in SDLC_GIT_LOCAL and nothing on the line makes git run a program —
#   - no VAR=value in front of git, also behind env or sudo (GIT_SSH_COMMAND, GIT_PAGER, GIT_CONFIG_* … configure git too) — but
#     those of SDLC_ENV_INERT,
#   - no -c / --config-env among the global options (an alias, a pager, an ssh command … set on the line) — but a -c whose setting
#     is one of SDLC_GIT_C_INERT,
#   - no --upload-pack / --receive-pack / --exec among the words (fetch, pull, clone and archive run that command),
#   - no -u, -c or --config of clone (the clone's own ssh command) and no -O / --open-files-in-pager of grep (a pager).
deploy_git_reads() {
  local k=0 nw=${#W[@]} a sub=""
  while [ "$k" -lt "$TL_K" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in *=*) if valid_ident "${a%%=*}" && ! in_globs "$SDLC_ENV_INERT" "$a"; then return 1; fi;; esac
  done
  k=$((TL_K + 1))
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      -c) if [ "$k" -ge "$nw" ] || ! in_globs "$SDLC_GIT_C_INERT" "${W[$k]}"; then return 1; fi
          k=$((k + 1));;
      --config-env|--config-env=*) return 1;;
      -C|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix) k=$((k + 1));;
      -*) ;;
      *) sub="$a"; break;;
    esac
  done
  case " $SDLC_GIT_LOCAL " in *" $sub "*) ;; *) return 1;; esac
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in --upload-pack|--upload-pack=*|--receive-pack|--receive-pack=*|--exec|--exec=*) return 1;; esac
    case "$sub" in
      clone) case "$a" in -u*|-c*|--config|--config=*) return 1;; esac;;
      grep) case "$a" in -O*|--open-files-in-pager|--open-files-in-pager=*) return 1;; esac;;
    esac
  done
  return 0
}
# deploy_find_runs → 0 when find (W[], name at TL_K) runs a program: -exec -execdir -ok -okdir
deploy_find_runs() {
  local k=$((TL_K + 1)) nw=${#W[@]}
  while [ "$k" -lt "$nw" ]; do
    case "${W[$k]}" in -exec|-execdir|-ok|-okdir) return 0;; esac
    k=$((k + 1))
  done
  return 1
}
# deploy_awk_runs → 0 when the awk program can start a program or a pipe: a word that is not an option and holds system, getline or |
deploy_awk_runs() {
  local k=$((TL_K + 1)) nw=${#W[@]} a
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in -*) ;; *system*|*getline*|*'|'*) return 0;; esac
  done
  return 1
}
# deploy_sed_runs → 0 when sed edits in place (-i, -i.bak, -ni, --in-place) or its script runs a command (an e command, the e flag of s)
deploy_sed_runs() {
  local k=$((TL_K + 1)) nw=${#W[@]} a e_cmd='(^|[;{])[[:space:]]*[0-9,$]*e([[:space:]]|$)' e_flag='(^|[;{[:space:]])s/.*/[gpimIM0-9]*e[gpimIMe0-9]*$'
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      --in-place|--in-place=*) return 0;;
      --*) ;;
      -*i*) return 0;;
      -*) ;;
      *) if [[ "$a" =~ $e_cmd ]] || [[ "$a" =~ $e_flag ]]; then return 0; fi;;
    esac
  done
  return 1
}
# deploy_gh_reads → 0 when the gh command in W[] only reads: pr/issue/repo … (text), run list|view|watch|download, workflow list|view,
# release list|view|download, and `api` without a method other than GET and without fields or input (those make it a POST)
deploy_gh_reads() {
  local k=$((TL_K + 1)) nw=${#W[@]} a sub="" sub2="" v
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      -R|--repo|--hostname) k=$((k + 1));;
      -*) ;;
      *) if [ -z "$sub" ]; then sub="$a"; else sub2="$a"; break; fi;;
    esac
  done
  case "$sub" in
    api)
      k=$((TL_K + 1))
      while [ "$k" -lt "$nw" ]; do
        a="${W[$k]}"; k=$((k + 1))
        case "$a" in
          -X|--method) v="${W[$k]:-}"; k=$((k + 1)); case "$v" in GET|get) ;; *) return 1;; esac;;
          -X?*) case "${a#-X}" in GET|get) ;; *) return 1;; esac;;
          --method=*) case "${a#--method=}" in GET|get) ;; *) return 1;; esac;;
          -f|-F|--field|--raw-field|--input|-f?*|-F?*|--field=*|--raw-field=*|--input=*) return 1;;
        esac
      done
      return 0;;
    run) case "$sub2" in list|view|watch|download) return 0;; esac; return 1;;
    workflow) case "$sub2" in list|view) return 0;; esac; return 1;;
    release) case "$sub2" in list|view|download) return 0;; esac; return 1;;
  esac
  case " $SDLC_GH_TEXT " in *" $sub "*) return 0;; esac
  return 1
}
# deploy_curl_reads → 0 when curl or wget (W[], name at TL_K) only fetches: no method but GET/HEAD, no body, form or upload
deploy_curl_reads() {
  local k=$((TL_K + 1)) nw=${#W[@]} a v
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      -X|--request) v="${W[$k]:-}"; k=$((k + 1)); case "$v" in GET|get|HEAD|head) ;; *) return 1;; esac;;
      --request=*) case "${a#--request=}" in GET|get|HEAD|head) ;; *) return 1;; esac;;
      -X?*) case "${a#-X}" in GET|get|HEAD|head) ;; *) return 1;; esac;;
      --data|--data-*|--json|--form|--form-*|--upload-file|--post-*|--body-*|--method|--method=*) return 1;;
      --*) ;;
      -?*) case "$a" in *[dFTX]*) return 1;; esac;;
    esac
  done
  return 0
}
# deploy_infra_reads <kubectl|helm|terraform|tofu> → 0 when the command in W[] only reads: kubectl get/describe/logs/… and rollout
# status/history, helm list/status/template/…, terraform plan/show/validate/…
deploy_infra_reads() {
  local k=$((TL_K + 1)) nw=${#W[@]} a sub="" sub2=""
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      -n|--namespace|--context|--cluster|--user|--kubeconfig|-s|--server|--as|--token|--kube-context|--kube-as-user) k=$((k + 1));;
      -*) ;;
      *) if [ -z "$sub" ]; then sub="$a"; else sub2="$a"; break; fi;;
    esac
  done
  case "$1" in
    kubectl) case "$sub" in
               rollout) case "$sub2" in status|history) return 0;; esac;;
               *) case " get describe logs top explain api-resources api-versions version cluster-info config auth events " in *" $sub "*) return 0;; esac;;
             esac;;
    helm) case " list ls status get history show search repo env version template lint help completion plugin dependency " in *" $sub "*) return 0;; esac;;
    terraform|tofu) case " plan show output validate fmt version providers graph console init " in *" $sub "*) return 0;; esac;;
  esac
  return 1
}
# deploy_test_runs → 0 when the command in W[] is a test runner: pytest, jest, vitest … (also behind npx), node --test, python -m pytest,
# `go|cargo|mvn|gradle|dotnet|deno test`, `npm|yarn|pnpm|bun [run] test[:x]`
deploy_test_runs() {
  local base="${W[$TL_K]##*/}" k=$((TL_K + 1)) nw=${#W[@]} a sub="" sub2="" prev=""
  case "$base" in pytest|py.test|jest|vitest|mocha|ava|tap|phpunit|rspec) return 0;; esac
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$base" in
      node|nodejs) case "$a" in --test|--test=*|--test-*) return 0;; esac;;
      python|python[0-9]*) if [ "$prev" = -m ]; then case "$a" in pytest|unittest|doctest|nose2) return 0;; esac; fi;;
    esac
    case "$a" in -*) ;; *) if [ -z "$sub" ]; then sub="$a"; elif [ -z "$sub2" ]; then sub2="$a"; fi;; esac
    prev="$a"
  done
  case "$base" in
    npx|bunx|pnpx) case "$sub" in pytest|jest|vitest|mocha|ava|tap|phpunit|rspec) return 0;; esac;;
    go|cargo|mvn|gradle|gradlew|dotnet|deno|swift|mix|dart|flutter) [ "$sub" = test ] && return 0;;
    npm|yarn|pnpm|bun)
      case "$sub" in test|t|tst|test:*) return 0;; run|run-script) case "$sub2" in test|test:*) return 0;; esac;; esac;;
  esac
  return 1
}
# deploy_runner_word → 0 when a word of the simple command in W[] makes the words after it a command: a shell, ssh, eval, xargs, watch …
deploy_runner_word() {
  local k=$TL_K nw=${#W[@]} b
  while [ "$k" -lt "$nw" ]; do
    b="${W[$k]##*/}"
    case " $SDLC_RUNNERS " in *" $b "*) return 0;; esac
    k=$((k + 1))
  done
  return 1
}
# deploy_launches <code> → 0 when the code of an interpreter's -c/-e argument can start a program (system, exec, popen, spawn,
# subprocess, child_process, a backtick, %x, a shell escape \! or COPY … PROGRAM …)
deploy_launches() {
  case "$1" in
    *system*|*exec*|*popen*|*spawn*|*subprocess*|*child_process*|*Popen*|*'`'*|*'%x'*|*check_output*|*check_call*|*shell*|*passthru*|*proc_open*|*Deno.Command*|*Deno.run*|*'Bun.'*|*PROGRAM*|*'\!'*|*'$('*) return 0;;
  esac
  return 1
}
# deploy_prose_text → DEPLOY_TEXT: the words of W[] without the prose — a word with white space in it, unless it is the command word,
# follows -c/--command/--cmd/--exec/-a/--args/--run/--eval (or carries it as --command=…), or is an interpreter's code that can start
# a program (deploy_launches)
deploy_prose_text() {
  local k=0 nw=${#W[@]} w prev="" interp=0 keep out=""
  if [ "$TL_K" -lt "$nw" ]; then
    case "${W[$TL_K]##*/}" in python|python[0-9]*|node|nodejs|ruby|perl|php|lua|deno|bun|psql|mysql|sqlite3|mongosh|mongo|redis-cli|osascript|Rscript) interp=1;; esac
  fi
  while [ "$k" -lt "$nw" ]; do
    w="${W[$k]}"; keep=1
    case "$w" in *[[:space:]]*)
      if [ "$k" -lt "$TL_K" ] && [ "$TL_K" -lt "$nw" ]; then
        # in front of a command: a VAR=value that runs something (LESSOPEN='|./deploy.sh %s' less x) is looked at; the inert ones are not
        valid_ident "${w%%=*}" && ! in_globs "$SDLC_ENV_INERT" "$w" || keep=0
      elif [ "$k" -ne "$TL_K" ]; then
        keep=0
        if [ "$interp" = 1 ]; then
          if deploy_launches "$w"; then keep=1; fi
        else
          case "$prev" in -c|--command|--cmd|--exec|-a|--args|--run|--eval) keep=1;; esac
          case "$w" in --command=*|--cmd=*|--exec=*|--args=*|--run=*|--eval=*) keep=1;; esac
        fi
      fi;;
    esac
    if [ "$keep" = 1 ]; then out="$out$w "; fi
    prev="$w"; k=$((k + 1))
  done
  DEPLOY_TEXT="${out% }"
}
# deploy_clause — the simple command in W[]: its words, joined by single spaces (and "< file" for the file its input is read from),
# join DEPLOY_CAND unless its command only reads or prints (see above); prose words are left out unless the command takes a command
# (reads cl, PIN, RIN, RF, SHELL_SER and raw of deploy_collect, which calls it)
deploy_clause() {
  local nw=${#W[@]} base="" skip=0 shell=0 mode=prose text IFS=' '
  [ "$nw" -gt 0 ] || return 0
  DEPLOY_WORK=$((DEPLOY_WORK + 1))
  tl_cmdword
  if [ "$TL_K" -lt "$nw" ]; then
    base="${W[$TL_K]##*/}"
    case " $SDLC_SHELLS " in *" $base "*) shell=1; mode=whole;; esac
    if [ "$shell" = 0 ]; then
      case " $SDLC_TEXT_TOOLS " in
        *" $base "*) if deploy_text_runs; then mode=whole; else skip=1; fi;;
        *) case "$base" in
             git) if deploy_git_reads; then skip=1; else mode=whole; fi;;
             find) deploy_find_runs || skip=1;;
             awk|gawk|mawk|nawk) if deploy_awk_runs; then mode=whole; else skip=1; fi;;
             sed|gsed) deploy_sed_runs || skip=1;;
             gh) deploy_gh_reads && skip=1;;
             curl|wget) deploy_curl_reads && skip=1;;
             kubectl|helm|terraform|tofu) deploy_infra_reads "$base" && skip=1;;
             *) deploy_test_runs && skip=1;;
           esac;;
      esac
      if [ "$skip" = 0 ] && [ "$mode" = prose ] && deploy_runner_word; then mode=whole; fi
    fi
  fi
  if [ "$shell" = 1 ] && shell_reads_stdin; then
    SHELL_SER="$SHELL_SER$cl "                                                  # a heredoc body that follows is its script
    if [ "$PIN" = 1 ] || [ "$RIN" = 1 ]; then deploy_cand_add "$raw"; fi      # what the shell reads is not in the words
  fi
  if [ "$skip" = 0 ]; then
    if [ "$mode" = prose ]; then deploy_prose_text; text="$DEPLOY_TEXT"; else text="${W[*]}"; fi
    [ -z "$RF" ] || text="$text <$RF"
    deploy_cand_add "$text"
  fi
  return 0
}
# deploy_collect <text> — adds the simple commands of <text> that could deploy to DEPLOY_CAND: through the tokenizer, and into
# what runs on its own (a heredoc body for a shell, a command substitution). Whatever it cannot split — the tokenizer fails,
# prints nothing or too much, or the command line has too many words or commands or nests too much — is added as plain text,
# which is how the gate matched before.
deploy_collect() {
  local raw="$1" out rc rec kind="" cl=0 PIN=0 POUT=0 RIN=0 RF="" SHELL_SER=" "
  local -a W=()
  if [ "$DEPLOY_WORK" -ge "$DEPLOY_WORK_MAX" ]; then deploy_cand_add "$raw"; return 0; fi
  out=$(printf '%s' "$raw" | LC_ALL=C awk -v bodies=1 -v cap="$DEPLOY_MAX_REC" -v shells="$SDLC_SHELLS" "$TL_AWK" 2>/dev/null); rc=$?
  if [ "$rc" != 0 ] || [ -z "$out" ] || [ "${#out}" -gt "$DEPLOY_MAX_OUT" ]; then deploy_cand_add "$raw"; return 0; fi
  while IFS= read -r rec; do
    if [ -n "$kind" ]; then                              # the text record after an N header
      case "$kind" in B*) case "$SHELL_SER" in *" ${kind#B} "*) ;; *) kind=""; continue;; esac;; esac
      kind=""; deploy_nested "$rec"; continue
    fi
    case "$rec" in
      W*) W[${#W[@]}]="${rec#W}";;
      P) POUT=1;;
      R*) RIN=1; [ "$rec" = R ] || RF="$RF ${rec#R}";;
      N*) kind="${rec#N}";;
      '!') deploy_cand_add "$raw"; return 0;;                # the tokenizer stopped: too many records
      C) deploy_clause; W=(); RIN=0; RF=""; PIN=$POUT; POUT=0; cl=$((cl + 1))
         if [ "$DEPLOY_WORK" -ge "$DEPLOY_WORK_MAX" ]; then deploy_cand_add "$raw"; return 0; fi;;
    esac
  done <<< "$out"
  deploy_clause
}
# deploy_nested <text, newlines as \001> — a command substitution or heredoc body, looked into as a command line of its own
# (the decoding is tr, not ${//}: bash 3.2 needs minutes for a few thousand replacements in a long string)
deploy_nested() {
  local body="$1"
  case "$body" in *$'\001'*) body=$(printf '%s' "$body" | tr '\001' '\n');; esac
  if [ "$DEPLOY_NESTED" -ge "$DEPLOY_NESTED_MAX" ]; then deploy_cand_add "$body"; return 0; fi
  DEPLOY_NESTED=$((DEPLOY_NESTED + 1))
  deploy_collect "$body"
}
# deploy_hit <command> → 0 when a simple command of it matches a deploy pattern: SDLC_DEPLOY_PATTERN is the pattern and
# SDLC_DEPLOY_PART the text it matched (that simple command, words joined by single spaces)
deploy_hit() {
  SDLC_DEPLOY_PATTERN=""; SDLC_DEPLOY_PART=""; DEPLOY_CAND=""; DEPLOY_WORK=0; DEPLOY_NESTED=0
  [ -n "$SDLC_DEPLOY_PATTERNS" ] || return 1
  deploy_collect "$1"
  [ -n "$DEPLOY_CAND" ] || return 1
  SDLC_DEPLOY_PATTERN=$(deploy_match "$DEPLOY_CAND") || return 1
  SDLC_DEPLOY_PART=$(printf '%s\n' "$DEPLOY_CAND" | grep -E -i -m 1 -e "$SDLC_DEPLOY_PATTERN" 2>/dev/null)
  return 0
}

# (1) production deploy gate — one tier, no per-environment autonomy and no rollback exemption
deploy_gate_config "$root"
if deploy_hit "$cmd"; then
  matched="$SDLC_DEPLOY_PATTERN"; part_short=$(trunc "$(one_line "$SDLC_DEPLOY_PART")" 100)
  approval_env="$SDLC_DEPLOY_ENV"; approval="${!approval_env:-}"
  detail="{\"pattern\":$(json_escape "$matched"),\"part\":$(json_escape "$part_short"),\"cmd\":$(json_escape "$cmd_short")}"
  if [ -n "$approval" ]; then
    log_event "$root" hook.gate allow "production: approved via $approval_env" "$detail"
  else
    log_event "$root" hook.gate deny "production: no $approval_env" "$detail"
    where=""; [ "$part_short" = "$cmd_short" ] || where=", in '$part_short'"
    cfg_hint=""; [ "$initialized" = true ] && cfg_hint=", or narrow protect.deploy_patterns in .sdlc/config.json"
    block "'$cmd_short' matches a production deploy pattern ('$matched'$where). Why: production deploys need a release authorization. To proceed: the person who releases starts Claude Code with $approval_env=<ticket> in its environment (a prefix on the command, $approval_env=<ticket> cmd, does not count) and the command is retried. If it is not a deploy — words are checked in every command except text and read-only tools, test runners and git commands that only read or record, and a program's prose arguments are skipped — rephrase it$cfg_hint."
  fi
fi

# (2) no direct path to the default branch — branch protection in a hook, for repositories where GitHub rulesets are unavailable.
#     Parses each `git push` clause (split on && ; | ||): refspecs (+, refs/heads/, src:dst, HEAD and @ = the current branch),
#     --all/--mirror, --delete <default>, bare `git push` while on the default branch, `git -C <dir> push`. Feature-branch pushes and
#     PR creation are allowed. While an sdlc item is active the merge is the shell gate's: `gh pr merge` and every push to the default
#     branch are refused whatever protect.block_direct_push says (push_clause, read from the tokenized command line, nested bodies too).
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
    case "$dst" in HEAD|@) dst="$cur";; esac                 # `git push origin HEAD` pushes the branch that is checked out
    if [ "$dst" = "$def" ]; then
      if [ $delete = 1 ]; then printf 'deletes the default branch %s' "$def"; else printf 'refspec targets %s' "$def"; fi; return 0
    fi
  done
  if [ -z "$refspecs" ] && [ $delete = 0 ] && [ "$cur" = "$def" ]; then printf 'bare push while on %s' "$def"; return 0; fi
  return 1
}
# push_clause — walk_scan callback: the simple command in W[] is a push to the default branch (PUSH_DEF, with PUSH_CUR checked out) or a
# merge of a pull request → PUSH_REASON, 0
PUSH_DEF=""; PUSH_CUR=""; PUSH_REASON=""
push_clause() {
  local nw=${#W[@]} k base a sub="" sub2="" r
  tl_cmdword; k=$TL_K
  [ "$k" -lt "$nw" ] || return 1
  base="${W[$k]##*/}"
  case "$base" in
    git) if r=$(push_hits_default "${W[*]:$k}" "$PUSH_DEF" "$PUSH_CUR"); then PUSH_REASON="$r"; return 0; fi;;
    gh) k=$((k + 1))
        while [ "$k" -lt "$nw" ]; do
          a="${W[$k]}"; k=$((k + 1))
          case "$a" in
            -R|--repo|--hostname) k=$((k + 1));;
            -*) ;;
            *) if [ -z "$sub" ]; then sub="$a"; else sub2="$a"; break; fi;;
          esac
        done
        if [ "$sub" = pr ] && [ "$sub2" = merge ]; then PUSH_REASON="gh pr merge"; return 0; fi
        if [ "$sub" = api ]; then
          for a in "${W[@]}"; do
            case "$a" in *pulls/*/merge*|*mergePullRequest*|*enablePullRequestAutoMerge*) PUSH_REASON="a pull request merged through the API"; return 0;; esac
          done
        fi;;
  esac
  return 1
}
push_guard=false; work_active=false
if [ "$initialized" = true ]; then
  [ "$(cfg_bool "$root" .protect.block_direct_push true)" = "true" ] && push_guard=true
  sdlc_work_active "$root" && work_active=true
fi
case "$cmd" in *push*|*merge*)
  if [ "$push_guard" = true ] || [ "$work_active" = true ]; then
    def=$(git -C "$root" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##'); [ -z "$def" ] && def=$(cfg "$root" .protect.default_branch main)
    cur=$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null)
    reason=""; merge_gate=false
    if [ "$work_active" = true ]; then
      PUSH_DEF="$def"; PUSH_CUR="$cur"; PUSH_REASON=""
      if walk_scan "$cmd" 0 push_clause; then reason="$PUSH_REASON"; merge_gate=true; fi
    fi
    if [ -z "$reason" ] && [ "$push_guard" = true ]; then
      old_ifs="$IFS"; IFS=$'\n'
      for clause in $(printf '%s\n' "$cmd" | sed -E 's/&&|\|\||;|\|/\n/g'); do
        IFS="$old_ifs"
        if r=$(push_hits_default "$clause" "$def" "$cur"); then reason="$r"; break; fi
        IFS=$'\n'
      done
      IFS="$old_ifs"
    fi
    if [ -n "$reason" ]; then
      log_event "$root" hook.guard deny direct_push "{\"cmd\":$(json_escape "$cmd_short"),\"branch\":\"$def\",\"reason\":$(json_escape "$reason")}"
      if [ "$merge_gate" = true ]; then
        block "'$cmd_short' ($reason): the merge is the shell gate's (\`sdlc.py ship\`); a session never merges. Why: while an sdlc item is active ($(active_work_slug "$root")) its change reaches '$def' only through the gate, which checks it first — this holds whatever protect.block_direct_push says. To proceed: commit on the sdlc branch and stop there — \`sdlc.py ship <slug>\` (interactive) or the loop pushes, opens the PR and merges after the gate."
      fi
      block "pushing to the default branch '$def' ($reason: '$cmd_short'). Why: anything the agent writes arrives as a pull request through branch protection — there is no direct path to the default branch (protect.block_direct_push). To proceed: push a feature branch and open a PR (git push -u origin <branch> && gh pr create) — /sdlc:go and /sdlc:run do this and merge through the gate; to allow direct pushes in this repository set protect.block_direct_push: false."
    fi
  fi ;;
esac

# (3) what the command writes — the same two rules as guard-edit (lib/hooks-extra.sh): during sdlc work existing test files
#     (already on the default branch) stay untouched while they are locked, a test this change adds is free; and while the
#     active work doc is ready or proposed, a path inside the project but outside the work dir and .sdlc/ is off limits (design
#     first). The environment the hook runs in decides SDLC_ALLOW_TEST_EDIT, never an assignment inside the command line.
#     The tokenizer (lib/hooks-extra.sh, walk_scan) splits the command line into simple commands and walks into what runs inside it:
#     a command substitution in double quotes, the body of a heredoc a shell reads (`bash <<EOF`, `bash -s <<EOF`), `bash -c`, eval.
#     Each simple command is then read for its write targets: redirections, tee, the destination of cp/mv/ln/install, the
#     sources of mv, sed -i / perl -i, truncate, rm, dd of=, git rm/mv, git checkout -- <file>, git restore. Best effort, not a
#     shell: no variables (a target with $VAR or a backtick is skipped), no brace expansion, no `find -exec`/`xargs`, nothing
#     that runs another program that writes (python -c, patch, git apply, make, package managers, curl -o). A path outside the
#     project is never refused; for the test lock a path that does not exist yet is allowed too.
TL_BASE=""; TL_ROOT=""; TL_ROOT_PHYS=""; TL_READY=""; TL_ON=0; TL_ACTION=""; TL_REL=""; TL_GLOBS=0; TL_NEW_CHECKS=0; NORM=""; ABS=""; REL=""; OPS=(); OPS_DD=0
TL_DESIGN=0; TL_NODESIGN=0; TL_WDIR=""; TL_KIND=""      # design first: on while the active work doc is ready/proposed; TL_KIND = tests|design, what refused the command
TL_MAX_GLOB_WORDS=20; TL_MAX_GLOB_FAST=2000; TL_MAX_GLOB_SLOW=300     # bounds: a command line costs the same whatever a glob expands to
TL_MAX_NEW_CHECKS=30                                                    # test files looked up in git per command line (one git call each)
TL_MAX_LOADS=8                                                          # listings of the base per command line: past that one listing of the whole tree
TL_TESTS=$'\n'; TL_LOADED=$'\n'; TL_LOADS=0; TL_HINT=""; TL_FOUND=""; TL_SKIP_ROOT=0   # the base's test files, by listing (see tl_tests_load)

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
# tl_prepare → once per command, at the first path that could be a test file: the lock state, the test paths and — resolved here, in
# the main shell, not inside a substitution, so it stays cached — the base the existing tests are measured against
tl_prepare() {
  TL_READY=1; TL_ON=0
  test_lock_on "$root" || return 0
  test_paths_load "$root"
  test_base_rev "$root"
  TL_ON=1
}
# tl_tests_load <rel dir> → TL_TESTS holds the test files of the base (the tracked ones when there is no base) under <rel dir> ('.' =
# the whole tree), one per line, and TL_LOADED which directories were listed: ONE git call per directory, however many entries a glob
# over it expands to (rm -rf node_modules/* is one call, not one per package) — and no call at all for a directory a listing covers.
# The tests are the built-in set (one grep) or the configured paths (is_test_path, at most 30000 files). Past TL_MAX_LOADS listings
# the next one is the whole tree.
tl_tests_load() {
  local p="$1" q listing f tests="" w wre n=0
  [ "$p" = "." ] && p=""
  case "$TL_LOADED" in *$'\n'"/"$'\n'*) return 0;; esac               # the whole tree is loaded
  q="$p"
  while :; do
    case "$TL_LOADED" in *$'\n'"/$q"$'\n'*) return 0;; esac
    [ -n "$q" ] || break
    case "$q" in */*) q="${q%/*}";; *) q="";; esac
  done
  TL_LOADS=$((TL_LOADS + 1)); [ "$TL_LOADS" -le "$TL_MAX_LOADS" ] || p=""
  TL_LOADED="$TL_LOADED/$p"$'\n'
  git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
  if [ "$SDLC_TEST_BASE" != "-" ]; then
    listing=$(git --literal-pathspecs -C "$root" -c core.quotepath=off ls-tree -r --name-only "$SDLC_TEST_BASE" ${p:+-- "$p"} 2>/dev/null | head -n 200000)
  else
    listing=$(git --literal-pathspecs -C "$root" -c core.quotepath=off ls-files ${p:+-- "$p"} 2>/dev/null | head -n 200000)
  fi
  [ -n "$listing" ] || return 0
  if [ "${#SDLC_TEST_PATH_ARR[@]}" = 1 ] && [ "${SDLC_TEST_PATH_ARR[0]}" = auto ]; then
    tests=$(printf '%s\n' "$listing" | LC_ALL=C grep -E -e "$SDLC_TEST_DIR_RE" -e "$SDLC_TEST_FILE_RE" -e "$SDLC_TEST_CONFIG_RE" -e "$SDLC_TEST_SPEC_RE")
    w="$SDLC_TEST_WORK_DIR"
    if [ -n "$w" ] && [ -n "$tests" ]; then                         # nothing under the work dir is a test
      wre=$(printf '%s' "$w" | sed 's/[][\.*^$/]/\\&/g')
      tests=$(printf '%s\n' "$tests" | LC_ALL=C grep -v -e "^$wre/")
    fi
  else
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      n=$((n + 1)); [ "$n" -le 30000 ] || break
      if is_test_path "$f"; then tests="$tests$f"$'\n'; fi
    done <<< "$listing"
    tests="${tests%$'\n'}"
  fi
  if [ -n "$tests" ]; then TL_TESTS="$TL_TESTS$tests"$'\n'; fi
  return 0
}
# tl_tests_under <rel dir> → 0 when the base has a test file under the directory ('.' = anywhere); loads the listing it needs
tl_tests_under() {
  tl_tests_load "$1"
  if [ "$1" = "." ] || [ -z "$1" ]; then [ "$TL_TESTS" != $'\n' ]; return; fi
  case "$TL_TESTS" in *$'\n'"$1"/*) return 0;; esac
  return 1
}
# tl_dir_test <rel dir> → TL_FOUND = one existing test file under the directory, and returns 0. Only files git knows count — the
# ones in the base (tracked ones without a base): build output and dependency folders are not tests, and a test this change adds is free.
# TL_HINT = a directory above this one (the part of a glob before its wildcard) that is listed instead, once for all its entries.
tl_dir_test() {
  local rel="$1" hint="$1" f hits
  if [ -n "$TL_HINT" ]; then case "$rel" in "$TL_HINT"|"$TL_HINT"/*) hint="$TL_HINT";; esac; fi
  tl_tests_load "$hint"
  tl_tests_under "$rel" || return 1
  if [ "$rel" = "." ]; then hits="$TL_TESTS"; else hits=$(printf '%s' "$TL_TESTS" | grep -F -e "$rel/" | head -n 2000); fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$rel" in .) ;; *) case "$f" in "$rel"/*) ;; *) continue;; esac;; esac
    if [ -e "$root/$f" ]; then TL_FOUND="$f"; return 0; fi
  done <<< "$hits"
  return 1
}
# tl_test_at <action> <abs> <rel> <dir-aware 0|1> → 0 (and TL_ACTION/TL_REL) when <abs>, which lies at <rel> inside the
# project, is an existing locked test file (dir-aware: …or a directory holding one — for commands that delete or move whole trees)
tl_test_at() {
  local action="$1" abs="$2" rel="$3" dirs="$4"
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
    if [ "$TL_SKIP_ROOT" = 1 ] && [ "$rel" = "." ]; then return 1; fi    # git restore . / git checkout . : back to what the index has, not another ref
    tl_dir_test "$rel" || return 1
    TL_ACTION="$action, the directory holds it"; TL_REL="$TL_FOUND"; return 0
  fi
  return 1
}
# tl_hit_at <action> <abs> <rel> <dir-aware 0|1> <creates 0|1> → 0 when a command writing to <abs> (at <rel> inside the
# project) is refused, with TL_KIND/TL_ACTION/TL_REL set: tests = an existing locked test file; design = design first, a path
# outside the work dir and .sdlc/ while the active work doc is ready or proposed. <creates> = the command makes the path when it
# is missing (redirection, tee, cp/mv destination, dd of=); for the rest (sed -i, rm, truncate, git checkout …) a missing path
# changes nothing — and a word that is only a script or an option value (s/a/b/, 0) names no file, so it is not a target.
tl_hit_at() {
  if tl_test_at "$1" "$2" "$3" "$4"; then TL_KIND=tests; return 0; fi
  [ "$TL_DESIGN" = 1 ] && [ "$TL_NODESIGN" = 0 ] || return 1
  case "$2" in *'$'*|*'`'*) return 1;; esac                      # an unexpanded variable: the real target is unknown
  [ "${5:-0}" = 1 ] || [ -e "$2" ] || [ -L "$2" ] || return 1
  case "$3" in "$TL_WDIR"|"$TL_WDIR"/*|.sdlc|.sdlc/*) return 1;; esac
  TL_KIND=design; TL_ACTION="$1"; TL_REL="$3"; return 0
}
# tl_hit1 <action> <path word> <dir-aware 0|1> [creates 0|1] — one path word, resolved against the working directory
tl_hit1() {
  tl_abs "$2"; tl_rel "$ABS" || return 1
  tl_hit_at "$1" "$ABS" "$REL" "$3" "${4:-0}"
}
# tl_hit <action> <path word> <dir-aware 0|1> [creates 0|1] — tl_hit1 after glob expansion (pathname expansion only; the word is
# never evaluated). Expansions are capped: a glob that matches thousands of files is not read to the end. The directory a glob starts
# under is looked up in the base once (TL_HINT): when it holds no test, none of the entries can be an existing one — rm -rf
# node_modules/* with 800 packages costs one git call and no loop (while design first is on, every entry still counts).
tl_hit() {
  local action="$1" word="$2" dirs="$3" creates="${4:-0}" c exp brel max fast=0 pre hint rc
  [ -n "$word" ] || return 1
  case "$word" in
    *[*?[]*)
      TL_GLOBS=$((TL_GLOBS + 1)); [ "$TL_GLOBS" -le "$TL_MAX_GLOB_WORDS" ] || return 1
      pre="${word%%[*?[]*}"
      case "$pre" in */*) pre="${pre%/*}"; [ -n "$pre" ] || pre="/";; *) pre="";; esac
      tl_abs "$pre"; tl_rel "$ABS" || return 1                       # a glob that starts outside the project matches nothing inside it
      hint="$REL"
      if [ "$TL_DESIGN" = 0 ]; then
        [ -n "$TL_READY" ] || tl_prepare
        [ "$TL_ON" = 1 ] || return 1
        if [ "$SDLC_TEST_BASE" != "-" ]; then tl_tests_under "$hint" || return 1; fi
      fi
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
      TL_HINT="$hint"; rc=1
      while IFS= read -r c; do
        if [ "$fast" = 1 ]; then if tl_hit_at "$action" "$TL_BASE/$c" "$brel$c" "$dirs" "$creates"; then rc=0; break; fi
        else if tl_hit1 "$action" "$c" "$dirs" "$creates"; then rc=0; break; fi; fi
      done <<< "$exp"
      TL_HINT=""
      return "$rc";;
  esac
  tl_hit1 "$action" "$word" "$dirs" "$creates"
}
# tl_ops <start> → OPS: the non-option words from index <start> on; after -- every word is an operand. OPS_DD = how many
# operands came before the -- (all of them when there is none)
tl_ops() {
  local k="$1" w endopts=0
  OPS=(); OPS_DD=-1
  while [ "$k" -lt "${#W[@]}" ]; do
    w="${W[$k]}"; k=$((k + 1))
    if [ "$endopts" = 1 ]; then OPS[${#OPS[@]}]="$w"
    elif [ "$w" = "--" ]; then endopts=1; OPS_DD=${#OPS[@]}
    else case "$w" in -?*) ;; *) OPS[${#OPS[@]}]="$w";; esac
    fi
  done
  [ "$OPS_DD" -ge 0 ] || OPS_DD=${#OPS[@]}
}
# tl_ops_hit <action> <start> <dir-aware 0|1> [creates 0|1] [after-dashes 0|1] → 0 when any operand is a hit. With
# after-dashes only the operands after -- count for design first (git checkout <branch> names no file, git checkout -- <file> does).
tl_ops_hit() {
  local i=0 creates="${4:-0}" dd="${5:-0}"
  tl_ops "$2"
  while [ "$i" -lt "${#OPS[@]}" ]; do
    if [ "$dd" = 1 ] && [ "$i" -lt "$OPS_DD" ]; then TL_NODESIGN=1; else TL_NODESIGN=0; fi
    if tl_hit "$1" "${OPS[$i]}" "$3" "$creates"; then TL_NODESIGN=0; return 0; fi
    i=$((i + 1))
  done
  TL_NODESIGN=0
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
      tl_hit "$verb onto it" "$dest/${src##*/}" 0 1 && return 0
      i=$((i + 1))
    done
    return 1
  fi
  tl_hit "$verb onto it" "$dest" 0 1
}
# tl_git <index after "git"> → 0 when the git command changes an existing locked test file (git rm/mv, checkout/restore of a file)
tl_git() {
  local k="$1" nw=${#W[@]} a sub="" saved="$TL_BASE" i staged=0 worktree=0 source=0 dashes=0 ref=0 rc=1
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
        case "${W[$i]}" in --staged|-S) staged=1;; --worktree|-W) worktree=1;; --source|--source=*|-s) source=1;; --) dashes=1; break;; esac
        i=$((i + 1))
      done
      # `git restore --staged <file>` only touches the index, never the file
      if [ "$sub" = restore ] && [ "$staged" = 1 ] && [ "$worktree" = 0 ]; then :
      else
        # a directory operand counts: git checkout <ref> -- tests and git restore tests replace every file under it. Without a ref
        # (--source, or the tree-ish before the --) the project root itself is not refused: `git restore .` goes back to the index.
        tl_ops "$k"
        if [ "$sub" = restore ]; then [ "$source" = 1 ] && ref=1
        elif [ "$dashes" = 1 ]; then [ "$OPS_DD" -gt 0 ] && ref=1
        else [ "${#OPS[@]}" -ge 2 ] && ref=1; fi
        TL_SKIP_ROOT=$((1 - ref))
        tl_ops_hit "git $sub" "$k" 1 0 "$([ "$sub" = checkout ] && echo 1 || echo 0)" && rc=0
        TL_SKIP_ROOT=0
      fi;;
  esac
  TL_BASE="$saved"
  return "$rc"
}
# tl_clause <depth> — one simple command (words W[], redirection targets R[]); 0 = it changes an existing locked test file
tl_clause() {
  local depth="$1" nw=${#W[@]} k r=0 base i
  while [ "$r" -lt "${#R[@]}" ]; do
    if tl_hit "output redirection" "${R[$r]}" 0 1; then return 0; fi
    r=$((r + 1))
  done
  tl_cmdword; k=$TL_K                            # past VAR=value prefixes, shell keywords and wrappers (sudo, env, time …)
  [ "$k" -lt "$nw" ] || return 1
  base="${W[$k]##*/}"; k=$((k + 1))
  case "$base" in
    rm|unlink|shred) tl_ops_hit "delete ($base)" "$k" 1 && return 0;;
    cp|mv|ln|install) tl_cpmv "$base" "$k" && return 0;;
    tee) tl_ops_hit "overwrite (tee)" "$k" 0 1 && return 0;;
    truncate) tl_ops_hit "truncate" "$k" 0 && return 0;;
    sed|gsed|perl) if tl_inplace "$base" "$k"; then tl_ops_hit "in-place edit ($base -i)" "$k" 0 && return 0; fi;;
    dd) i=$k
        while [ "$i" -lt "$nw" ]; do
          case "${W[$i]}" in of=*) tl_hit "overwrite (dd)" "${W[$i]#of=}" 0 1 && return 0;; esac
          i=$((i + 1))
        done;;
    cd|pushd) tl_ops "$k"
        if [ "${#OPS[@]}" -eq 0 ]; then [ "$base" = pushd ] || TL_BASE="$HOME"
        elif [ "${OPS[0]}" != "-" ]; then
          tl_abs "${OPS[0]}"
          if [ -d "$ABS" ]; then
            [ "$base" = pushd ] && TL_STACK="$TL_BASE"$'\n'"$TL_STACK"
            TL_BASE="$ABS"
          fi
        fi;;
    popd) if [ -n "$TL_STACK" ]; then TL_BASE="${TL_STACK%%$'\n'*}"; TL_STACK="${TL_STACK#*$'\n'}"; fi;;
    git) tl_git "$k" && return 0;;
  esac
  return 1
}
# tl_scan <command> <depth> → 0 when one of its simple commands (walk_scan: nested bodies too) changes an existing locked test file
tl_scan() { walk_scan "$1" "$2" tl_clause; }

if [ "$initialized" = true ] && tl_interesting "$cmd"; then
  cwd=$(json_get "$input" .cwd)
  norm_path "$root"; TL_ROOT="$NORM"
  TL_ROOT_PHYS=$(cd "$root" 2>/dev/null && pwd -P); [ -n "$TL_ROOT_PHYS" ] || TL_ROOT_PHYS="$TL_ROOT"
  TL_BASE="$TL_ROOT"; if [ -n "$cwd" ] && [ -d "$cwd" ]; then norm_path "$cwd"; TL_BASE="$NORM"; fi
  doc=$(active_work_file "$root"); dstatus=""
  if [ -n "$doc" ]; then
    dstatus=$(frontmatter_get "$root/$doc" status)
    case "$dstatus" in ready|proposed) TL_DESIGN=1; TL_WDIR=$(work_dir "$root");; esac
  fi
  if tl_scan "$cmd" 0; then
    if [ "$TL_KIND" = design ]; then
      log_event "$root" hook.guard deny design_first "{\"tool\":\"Bash\",\"cmd\":$(json_escape "$cmd_short"),\"path\":$(json_escape "$TL_REL"),\"action\":$(json_escape "$TL_ACTION")}"
      block "design first — fill Plan and Proof, then set status planned. '$cmd_short' would change '$TL_REL' ($TL_ACTION) while $doc has status '$dstatus'. Why: a change is built only after its design is on record. To proceed: write Plan and Proof in $doc and set its status to planned (the work doc, everything under $TL_WDIR/ and .sdlc/ stay editable)."
    fi
    log_event "$root" hook.guard deny test_lock_bash "{\"cmd\":$(json_escape "$cmd_short"),\"path\":$(json_escape "$TL_REL"),\"action\":$(json_escape "$TL_ACTION")}"
    doc_hint="$(work_dir "$root")/$(active_work_slug "$root").md"
    block "'$cmd_short' would change the existing test file '$TL_REL' ($TL_ACTION) while $doc_hint is the active work doc. Why: existing tests (those already on the default branch) are locked — fix the code, not the test (protect.protect_tests) — and the lock covers shell commands as well as edits; test files this change adds stay free to edit. To proceed, only if the test itself is wrong or the new behavior needs it changed: that is a design decision — set 'test_changes: allowed' in $doc_hint when designing the change; a build session asks a person instead of flipping it itself. As a last resort the user runs the session with SDLC_ALLOW_TEST_EDIT=1."
  fi
fi

# (4) secrets in the changes being committed. The hook runs before the command, so what a `git add` earlier in the same command
#     line stages is not in the index yet: with one, and with -a/--all of the `git commit` itself (not of another command on the line:
#     `git commit -m x && ls -la`), the scan covers what the commit could take (secret-scan.sh --all, plus --untracked for the add); a
#     plain `git commit` scans the staged diff only. `git -C <dir> add|commit` adds the
#     repository named there to the scan. Best effort: a pathspec commit (`git commit <file>`) and `git add -f` of an ignored
#     file are not widened.
# commit_all_clause — walk_scan callback: the simple command in W[] is a `git commit` with -a/--all (also in a bundle: -am). The value
# of -m -F -C -c -t and of --message --file … is skipped: `-m -a` is a message, not a flag.
commit_all_clause() {
  local nw=${#W[@]} k a sub="" i ch
  tl_cmdword; k=$TL_K
  [ "$k" -lt "$nw" ] && [ "${W[$k]##*/}" = git ] || return 1
  k=$((k + 1))
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix|--config-env) k=$((k + 1));;
      -*) ;;
      *) sub="$a"; break;;
    esac
  done
  [ "$sub" = commit ] || return 1
  while [ "$k" -lt "$nw" ]; do
    a="${W[$k]}"; k=$((k + 1))
    case "$a" in
      --) break;;
      --all|--al) return 0;;
      --message|--file|--author|--date|--reuse-message|--reedit-message|--template|--cleanup|--fixup|--squash|--trailer|--pathspec-from-file) k=$((k + 1));;
      --*) ;;
      -?*) i=1
           while [ "$i" -lt "${#a}" ]; do
             ch="${a:$i:1}"
             case "$ch" in
               a) return 0;;
               m|F|C|c|t) [ $((i + 1)) -lt "${#a}" ] || k=$((k + 1)); break;;         # the rest of the word, or the next word, is its value
             esac
             i=$((i + 1))
           done;;
    esac
  done
  return 1
}
if [ "$initialized" = true ] && [ "$(cfg_bool "$root" .secrets.scan_commits true)" = "true" ]; then
  GARG='((\\.|"[^"]*"|'"'"'[^'"'"']*'"'"'|[^[:space:]\\"'"'"'])+)'      # one shell word: pieces that are \-escaped, "quoted", 'quoted' or plain
  GIT_CMD='(^|[^[:alnum:]_./-])git([[:space:]]+(-[Cc][[:space:]]+'"$GARG"'|--(git-dir|work-tree|namespace)[[:space:]]+'"$GARG"'|-[^[:space:]]+))*[[:space:]]+'
  if printf '%s\n' "$cmd" | grep -E -q -e "${GIT_CMD}commit([[:space:]]|\$)"; then
    scope=staged; scan_flags=""
    if walk_scan "$cmd" 0 commit_all_clause; then scope=all; scan_flags="--all"; fi
    if printf '%s' "$cmd" | tr '\n' ' ' | grep -E -q -e "${GIT_CMD}add([[:space:]]|\$).*${GIT_CMD}commit([[:space:]]|\$)"; then
      scope=all+untracked; scan_flags="--all --untracked"
    fi
    scan_root="$root"
    git -C "$scan_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || scan_root=$(json_get "$input" .cwd)
    # The project's own repository is always scanned. `git -C <dir> add|commit` commits to the repository named there, so that one
    # is scanned as well (the first two, each repository once — a scan costs about 0.2 s) — a command can mix both, and a named
    # directory that is not a repository (built from a variable or `~`) is left to the scan of the project.
    scan_dirs="$scan_root"; seen=$'\n'"$(git -C "$scan_root" rev-parse --show-toplevel 2>/dev/null)"$'\n'
    c_re='(^|[^[:alnum:]_./-])git[[:space:]]+-C[[:space:]]+'"$GARG"'[[:space:]]+(commit|add)([[:space:]]|$)'
    c_rest="$cmd"; c_n=0; c_k=0
    while [ "$c_n" -lt 8 ] && [ "$c_k" -lt 2 ] && [[ "$c_rest" =~ $c_re ]]; do
      c_dir="${BASH_REMATCH[2]}"; c_rest="${c_rest#*"${BASH_REMATCH[0]}"}"; c_n=$((c_n + 1))
      c_dir="${c_dir//[\"\'\\]/}"                       # the word as the shell would pass it: quotes and escapes resolved
      case "$c_dir" in /*) ;; *) c_cwd=$(json_get "$input" .cwd); c_dir="${c_cwd:-$root}/$c_dir";; esac
      c_top=$(git -C "$c_dir" rev-parse --show-toplevel 2>/dev/null)
      [ -n "$c_top" ] || continue
      case "$seen" in *$'\n'"$c_top"$'\n'*) continue;; esac
      seen="$seen$c_top"$'\n'; scan_dirs="$scan_dirs"$'\n'"$c_dir"; c_k=$((c_k + 1))
    done
    hits=""; hit_dir=""
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      hits=$(bash "$here/../secret-scan.sh" --staged "$d" $scan_flags 2>/dev/null)
      [ -n "$hits" ] && { hit_dir="$d"; break; }
    done <<< "$scan_dirs"
    if [ -n "$hits" ]; then
      log_event "$root" hook.guard deny commit_secret "{\"cmd\":$(json_escape "$cmd_short"),\"scope\":\"$scope\",\"hits\":$(json_escape "$(one_line "$hits")")}"
      scope_note=""; where=""
      [ "$scope" = "all+untracked" ] && scope_note=" The command stages before it commits, so the scan covered everything git add could take: tracked changes and untracked files that are not ignored (git status lists them)."
      [ "$hit_dir" = "$scan_root" ] || where=" in $hit_dir"
      # file:line of every hit, and whether any of them is more than a pattern inside long encoded (base64-like) data
      descr=$(printf '%s\n' "$hits" | awk -F'\t' '{ printf "%s%s %s: %s", (NR > 1 ? "; " : ""), $1, $2, $3 }')
      real=$(printf '%s\n' "$hits" | awk -F'\t' '$4 != "encoded"' | head -c 1)
      if [ -n "$real" ]; then
        block "possible secret in the changes being committed$where ($descr). Why: credentials never enter version control (secrets.scan_commits). To proceed: take the value out of the diff (git restore --staged <file>, then edit it to read from the environment or a secret manager) and commit again; if it is not a credential (test data), set secrets.scan_commits: false in .sdlc/config.json.$scope_note"
      fi
      block "the changes being committed$where match a secret pattern ($descr), but only inside long runs of base64-like characters — it looks like encoded data, not a credential. Why: a token pattern can occur by chance in such data (secrets.scan_commits). To proceed: keep the data out of the diff (a file that is copied or fetched at build time), or set secrets.scan_commits: false in .sdlc/config.json.$scope_note"
    fi
  fi
fi
exit 0
