#!/bin/bash
# PreToolUse Bash — in order: (1) production deploy gate (one tier: a simple command matching protect.deploy_patterns needs the
# env var protect.deploy_approval_env), (2) no direct push to the default branch, (3) what the command writes, with the same
# two rules as guard-edit: during sdlc work (an active work doc applies) an existing test file stays untouched while it is
# locked — outside sdlc work no test is locked — and while the active work doc is ready or proposed (design first) nothing
# outside the work dir and .sdlc/ is written — rm, mv, cp, sed -i, redirects, tee, git rm/checkout/restore …; (4) secret scan
# on `git commit`: the staged diff, or — with a `git add` earlier in the command line or -a/--all — what the commit could take.
# Uninitialized project → only (1), with the built-in patterns.
# (1) matches the patterns against each simple command, not the whole text: its words with quotes resolved, heredoc bodies and
# comments left out. A quoted argument counts (bash -c "deploy prod", ssh host 'release production'); text tools (echo, cat, grep …),
# git commands that only read or record (commit, log, diff …) and the body of a heredoc whose command is not a shell are text, not
# checked — a text tool or git only while nothing on the line makes it run a program (rg --pre, sort --compress-program, git -c,
# --exec, an env prefix …). A command line it cannot split is matched as plain text, as before.
# (3) and (4) are best effort: the hook reads the command line, it does not run it — a program that writes on its own (python -c,
# patch, git apply, make, a package manager, curl -o …) or a path built from a variable or brace expansion is not seen.
# Reads: protect.{deploy_patterns,deploy_approval_env,block_direct_push,default_branch,protect_tests,test_paths}, paths.work,
#        secrets.scan_commits, .sdlc/state/active-work (lib/common.sh), SDLC_ALLOW_TEST_EDIT (environment); a version-1 config's
#        environments.production.{patterns,approval_env}
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

# The command line as simple commands — (1) and (3) both read it through one tokenizer (awk) that turns it into records: W<word>
# (quotes and escapes resolved), O<path> (the target of an output redirection) and C (end of a simple command: ; & | && || newline
# ( ) `) — heredoc bodies and comments skipped. Only (1) asks for more (awk -v bodies=1 -v cap=<n>): P (the command's output is
# piped on), R<file> (its input is a here-string — no file — or the file), N<kind> followed by one record of text, newlines as
# \001 — kind X: a command substitution inside double quotes or inside an unquoted heredoc body (a command of its own), kind B<n>:
# the body of a heredoc whose command, simple command number n, names a shell — and ! (more than <n> records: stopped there).
# Plain text, white space and quoted text are read a chunk at a time: substr in macOS awk measures the whole command line on every
# call, so a character-by-character scan is quadratic in its length. awk runs as LC_ALL=C — it reads bytes, whatever the locale: in a
# UTF-8 locale macOS awk stops dead on a regular expression over half a character (a chunk that ends inside one, the text after a
# backslash that escapes its first byte), and everything after that point is never seen.
TL_AWK='
BEGIN {
  RS = "\001"; SQ = "\047"; DQ = "\042"; BQ = "\140"; DOL = "\044"; cl = 0
  PLAINRE = "^[^ \t\r\n;|&()<>" BQ DQ SQ "\\\\]+"                 # a run of characters that need no attention (not stop, quote or backslash)
  DQRE = bodies ? "[\"\\\\$`]" : "[\"\\\\]"
  if (bodies) { m = split(shells, a, " "); for (k = 1; k <= m; k++) SH[a[k]] = 1 }
}
function stop(c) {
  return c == " " || c == "\t" || c == "\r" || c == "\n" || c == ";" || c == "|" || c == "&" || c == "(" || c == ")" || c == "<" || c == ">" || c == "`"
}
function tick() { if (cap && ++nrec > cap) { print "!"; exit } }                  # (bodies only) too many records: the caller matches the text instead
function cend() { tick(); print "C"; cl++ }
function nest(kind, x) { tick(); gsub(/\n/, "\001", x); print "N" kind; print x }
function pclose(t, p,   nt, dep, rest, j, c) {              # the ) that closes a $( — p is just past the $(
  nt = length(t); dep = 1
  while (p <= nt) {
    rest = substr(t, p, 256)
    j = match(rest, /[()\\]/)
    if (j == 0) { p += length(rest); continue }
    p += j - 1
    c = substr(t, p, 1)
    if (c == "\\") { p += 2; continue }
    if (c == "(") dep++
    else if (--dep == 0) return p
    p++
  }
  return nt + 1
}
function bclose(t, p,   nt, rest, j) {                       # the backtick that closes one — p is just past the opening one
  nt = length(t)
  while (p <= nt) {
    rest = substr(t, p, 256)
    j = match(rest, /[`\\]/)
    if (j == 0) { p += length(rest); continue }
    p += j - 1
    if (substr(t, p, 1) == "\\") { p += 2; continue }
    return p
  }
  return nt + 1
}
function substs(t,   p, nt, rest, j, c, q) {                 # the command substitutions an unquoted heredoc body expands
  if (index(t, "$(") == 0 && index(t, BQ) == 0) return
  nt = length(t); p = 1
  while (p <= nt) {
    rest = substr(t, p, 256)
    j = match(rest, /[\\$`]/)
    if (j == 0) { p += length(rest); continue }
    p += j - 1
    c = substr(t, p, 1)
    if (c == "\\") { p += 2; continue }
    if (c == DOL) {
      if (substr(t, p + 1, 1) != "(") { p++; continue }
      q = pclose(t, p + 2); nest("X", substr(t, p + 2, q - p - 2)); p = q + 1
    } else {
      q = bclose(t, p + 1); nest("X", substr(t, p + 1, q - p - 1)); p = q + 1
    }
  }
}
function readword(   w, c, d, e, rest, j, q, k0) {
  w = ""
  while (i <= n) {
    rest = substr(s, i, 256)
    if (match(rest, PLAINRE)) { w = w substr(rest, 1, RLENGTH); i += RLENGTH; continue }
    c = substr(rest, 1, 1)
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
        j = match(rest, DQRE)
        if (j == 0) { w = w rest; i += length(rest); continue }
        w = w substr(rest, 1, j - 1); i += j - 1
        d = substr(s, i, 1)
        if (d == DQ) break
        if (d == DOL || d == BQ) {                      # (bodies only) a command substitution inside the quotes is a command of its own
          if (d == BQ) { k0 = i + 1; q = bclose(s, k0) }
          else if (substr(s, i + 1, 1) == "(") { k0 = i + 2; q = pclose(s, k0) }
          else { w = w d; i++; continue }
          nest("X", substr(s, k0, q - k0))
          w = w substr(s, i, q - i + 1); i = q + 1; continue
        }
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
function skipws(   rest) {                                  # a run of white space, a chunk at a time
  while (i <= n) {
    rest = substr(s, i, 256)
    if (!match(rest, /^[ \t\r]+/)) return
    i += RLENGTH
  }
}
function emit(k, b) {                                       # (bodies only) a heredoc body: a script when its command is a shell, plus the $( ) of an unquoted one
  if (!bodies) return
  if (shw[hown[k]]) nest("B" hown[k], b)
  if (!hq[k]) substs(b)
}
function skipbodies(   k, d, wrapped, j, b, from, rs, rl) {
  for (k = 1; k <= nh; k++) {
    if (i > n) break
    d = hd[k]; from = i
    wrapped = "\n" substr(s, i) "\n"
    if (!hdash[k]) {
      j = index(wrapped, "\n" d "\n")
      if (j > 0) { emit(k, substr(s, from, j - 1)); i = i + j + length(d); continue }
    } else if (d ~ /^[A-Za-z0-9_]+$/) {
      if (match(wrapped, "\n\t*" d "\n")) { rs = RSTART; rl = RLENGTH; emit(k, substr(s, from, rs - 1)); i = i + rs + rl - 2; continue }
    } else {
      b = ""
      while (i <= n) {
        j = index(substr(s, i), "\n")
        if (j == 0) { wrapped = substr(s, i); i = n + 1 } else { wrapped = substr(s, i, j - 1); i += j }
        sub(/^\t+/, "", wrapped)
        if (wrapped == d) break
        b = b wrapped "\n"
      }
      emit(k, b)
      continue
    }
    emit(k, substr(s, from)); i = n + 1
  }
  nh = 0
}
{
  s = $0; n = length(s); i = 1; nh = 0
  while (i <= n) {
    rest = substr(s, i, 256)
    if (match(rest, /^[ \t\r]+/)) { i += RLENGTH; continue }
    c = substr(rest, 1, 1)
    if (c == "\n") { i++; cend(); if (nh > 0) skipbodies(); continue }
    if (c == "#") { j = index(substr(s, i), "\n"); if (j == 0) i = n + 1; else i += j - 1; continue }
    if (c == ";" || c == "(" || c == ")" || c == "`") { i++; cend(); continue }
    if (c == "|") {
      i++; d = substr(s, i, 1)
      if (d == "|") i++
      else { if (d == "&") i++; if (bodies) print "P" }
      cend(); continue
    }
    if (c == "&") {
      d = substr(s, i + 1, 1)
      if (d == ">") {
        i += 2; if (substr(s, i, 1) == ">") i++
        skipws(); t = readword(); print "O" t; continue
      }
      i++; if (d == "&") i++
      cend(); continue
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
        if (substr(s, i, 1) == "<") { i++; skipws(); t = readword(); if (bodies) print "R"; continue }
        dash = 0; if (substr(s, i, 1) == "-") { dash = 1; i++ }
        skipws(); hp = i; t = readword()
        nh++; hd[nh] = t; hdash[nh] = dash; hown[nh] = cl
        q = substr(s, hp, i - hp); hq[nh] = (index(q, DQ) > 0 || index(q, SQ) > 0 || index(q, "\\") > 0)
        continue
      }
      if (d == "(") continue
      if (d == "&") i++
      skipws(); t = readword(); if (bodies && d != "&") print "R" t; continue
    }
    p = i
    w = readword()
    if (i == p) { i++; continue }
    d = substr(s, i, 1)
    if (w ~ /^[0-9]+$/ && (d == ">" || d == "<")) continue
    if (bodies) { b = w; sub(/^.*\//, "", b); if (b in SH) shw[cl] = 1 }
    tick(); print "W" w
  }
}
'
# What can deploy. A text tool only prints or searches its arguments, so words that look like a deploy there are text — unless an option
# makes it run a program (deploy_text_runs); less and more are not on the list, they run what a ! line or LESSOPEN says. Of git, the
# commands in SDLC_GIT_LOCAL only read or record — and only while nothing on the line makes git run a program (deploy_git_reads);
# push, anything else (`git deploy`, an alias) and the commands that run what they are given (rebase --exec, bisect run, submodule
# foreach, difftool, mergetool, config alias.x '!cmd') are checked like any command. What git may be given without that is listed in
# SDLC_GIT_C_INERT (the settings of a -c: they name the committer) and SDLC_ENV_INERT (the VAR=value in front of git: language, time
# zone, terminal and colour settings, who the author and committer are, a pager that is cat or empty — git starts none) — patterns,
# matched against the whole word.
# A shell runs what it is given: the words of its command line, the body of a heredoc, and — where the tokenizer cannot follow
# it — whatever a pipe, a here-string or a file feeds it.
SDLC_TEXT_TOOLS="echo printf cat grep egrep fgrep rg ag ack head tail wc sort uniq cut tr diff jq"
SDLC_SHELLS="sh bash zsh dash ksh ssh"
SDLC_GIT_LOCAL="commit log show diff grep checkout switch branch tag status add rm mv merge fetch pull stash remote cherry-pick restore reset revert blame clean describe ls-files rev-parse rev-list shortlog worktree apply am archive bundle clone init notes reflog format-patch cat-file show-ref name-rev for-each-ref diff-tree range-diff cherry show-branch gc fsck prune maintenance sparse-checkout"
SDLC_GIT_C_INERT="user.name=* user.email=*"
SDLC_ENV_INERT="LANG=* LC_*=* TZ=* TERM=* NO_COLOR=* COLUMNS=* GIT_PAGER=cat GIT_PAGER= PAGER=cat PAGER= GIT_AUTHOR_NAME=* GIT_AUTHOR_EMAIL=* GIT_AUTHOR_DATE=* GIT_COMMITTER_NAME=* GIT_COMMITTER_EMAIL=* GIT_COMMITTER_DATE=*"
DEPLOY_MAX_REC=4000                                        # records the tokenizer prints before it stops (a word, a simple command's end, a substitution each count one)
DEPLOY_MAX_OUT=40000                                       # more tokenizer output than this is matched as plain text instead
DEPLOY_WORK_MAX=1000                                       # simple commands looked at per command line — what is left is matched as plain text
DEPLOY_NESTED_MAX=16                                       # command substitutions and shell heredoc bodies looked into per command line (one awk each)
DEPLOY_WORK=0; DEPLOY_NESTED=0; DEPLOY_CAND=""; TL_K=0; SDLC_DEPLOY_PATTERN=""; SDLC_DEPLOY_PART=""

# tl_cmdword → TL_K: the index in W[] of the command word — past VAR=value prefixes, shell keywords and wrappers (sudo, env, time,
# nice … — also with a path) and their options; ${#W[@]} when the simple command has none
tl_cmdword() {
  local nw=${#W[@]} w b wrap=0 tool=""
  TL_K=0
  while [ "$TL_K" -lt "$nw" ]; do
    w="${W[$TL_K]}"
    case "$w" in
      -*) [ "$wrap" -ge 1 ] || break
          case "$tool" in                        # an option that takes the next word as its argument (-u root, -n 5 …)
            sudo) case "$w" in -[ughpCrtTUDR]|--user|--group|--host|--prompt|--close-from|--role|--type|--command-timeout|--other-user|--chdir|--chroot) TL_K=$((TL_K + 1));; esac;;
            doas) case "$w" in -[uC]) TL_K=$((TL_K + 1));; esac;;
            env) case "$w" in -[uCS]|--unset|--chdir|--split-string) TL_K=$((TL_K + 1));; esac;;
            nice) case "$w" in -n|--adjustment) TL_K=$((TL_K + 1));; esac;;
            stdbuf) case "$w" in -[ioe]|--input|--output|--error) TL_K=$((TL_K + 1));; esac;;
            timeout) case "$w" in -[sk]|--signal|--kill-after) TL_K=$((TL_K + 1));; esac;;
            time) case "$w" in -[fo]|--format|--output) TL_K=$((TL_K + 1));; esac;;
            exec) case "$w" in -a) TL_K=$((TL_K + 1));; esac;;
          esac;;
      *=*) valid_ident "${w%%=*}" || break;;
      '!'|'{'|'}'|if|then|else|elif|fi|do|done|while|until) wrap=0;;
      *) b="${w##*/}"
         case "$b" in
           time|command|builtin|exec|nohup|sudo|doas|env|nice|setsid|stdbuf) wrap=1; tool="$b";;
           timeout) wrap=2; tool="$b";;
           *) if [ "$wrap" = 2 ]; then wrap=1; else break; fi;;
         esac;;
    esac
    TL_K=$((TL_K + 1))
  done
}
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
# deploy_clause — the simple command in W[]: its words, joined by single spaces (and "< file" for the file its input is read from),
# join DEPLOY_CAND unless its command is a text tool or a git command that only reads or records and runs no program (deploy_text_runs,
# deploy_git_reads) (reads cl, PIN, RIN, RF, SHELL_SER and raw of deploy_collect, which calls it)
deploy_clause() {
  local nw=${#W[@]} base="" skip=0 shell=0 text IFS=' '
  [ "$nw" -gt 0 ] || return 0
  DEPLOY_WORK=$((DEPLOY_WORK + 1))
  tl_cmdword
  if [ "$TL_K" -lt "$nw" ]; then
    base="${W[$TL_K]##*/}"
    case " $SDLC_TEXT_TOOLS " in *" $base "*) deploy_text_runs || skip=1;; esac
    case " $SDLC_SHELLS " in *" $base "*) shell=1;; esac
    if [ "$base" = git ] && deploy_git_reads; then skip=1; fi
  fi
  if [ "$shell" = 1 ]; then
    SHELL_SER="$SHELL_SER$cl "
    if [ "$PIN" = 1 ] || [ "$RIN" = 1 ]; then deploy_cand_add "$raw"; fi      # what the shell reads is not in the words
  fi
  if [ "$skip" = 0 ]; then text="${W[*]}"; [ -z "$RF" ] || text="$text <$RF"; deploy_cand_add "$text"; fi
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
    block "'$cmd_short' matches a production deploy pattern ('$matched'$where). Why: production deploys need a release authorization (protect.deploy_patterns). To proceed: set $approval_env=<ticket> (release manager) and retry. If this is not a deploy, the words only look like one — they are checked in every command except text tools (echo, cat, grep …), git commands that only read or record (commit, log, diff …) and heredoc text — and a text tool or git is checked too when it runs a program (--pre, --pager, git -c, --exec, an env prefix …): rephrase it, or narrow protect.deploy_patterns in .sdlc/config.json."
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

# (3) what the command writes — the same two rules as guard-edit (lib/hooks-extra.sh): during sdlc work existing test files
#     (already on the default branch) stay untouched while they are locked, a test this change adds is free; and while the
#     active work doc is ready or proposed, a path inside the project but outside the work dir and .sdlc/ is off limits (design
#     first). The environment the hook runs in decides SDLC_ALLOW_TEST_EDIT, never an assignment inside the command line.
#     The tokenizer above splits the command line into simple commands.
#     Each simple command is then read for its write targets: redirections, tee, the destination of cp/mv/ln/install, the
#     sources of mv, sed -i / perl -i, truncate, rm, dd of=, git rm/mv, git checkout -- <file>, git restore. Best effort, not a
#     shell: no variables (a target with $VAR or a backtick is skipped), no brace expansion, no `find -exec`/`xargs`, nothing
#     that runs another program that writes (python -c, patch, git apply, make, package managers, curl -o). A path outside the
#     project is never refused; for the test lock a path that does not exist yet is allowed too.
TL_BASE=""; TL_ROOT=""; TL_ROOT_PHYS=""; TL_READY=""; TL_ON=0; TL_ACTION=""; TL_REL=""; TL_GLOBS=0; TL_NEW_CHECKS=0; NORM=""; ABS=""; REL=""; OPS=(); OPS_DD=0
TL_DESIGN=0; TL_NODESIGN=0; TL_WDIR=""; TL_KIND=""      # design first: on while the active work doc is ready/proposed; TL_KIND = tests|design, what refused the command
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
# tl_test_at <action> <abs> <rel> <dir-aware 0|1> → 0 (and TL_ACTION/TL_REL) when <abs>, which lies at <rel> inside the
# project, is an existing locked test file (dir-aware: …or a directory holding one — for commands that delete or move whole trees)
tl_test_at() {
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
# never evaluated). Expansions are capped: a glob that matches thousands of files is not read to the end.
tl_hit() {
  local action="$1" word="$2" dirs="$3" creates="${4:-0}" c exp brel max fast=0
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
        if [ "$fast" = 1 ]; then tl_hit_at "$action" "$TL_BASE/$c" "$brel$c" "$dirs" "$creates" && return 0
        else tl_hit1 "$action" "$c" "$dirs" "$creates" && return 0; fi
      done <<< "$exp"
      return 1;;
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
      else tl_ops_hit "git $sub" "$k" 0 0 "$([ "$sub" = checkout ] && echo 1 || echo 0)" && rc=0; fi;;
  esac
  TL_BASE="$saved"
  return "$rc"
}
# tl_clause <depth> — one simple command (words W[], redirection targets R[]); 0 = it changes an existing locked test file
tl_clause() {
  local depth="$1" nw=${#W[@]} k r=0 base i str
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
  done <<< "$(printf '%s' "$1" | LC_ALL=C awk "$TL_AWK" 2>/dev/null)"
  tl_clause "$depth"
}

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
#     line stages is not in the index yet: with one, and with -a/--all, the scan covers what the commit could take (secret-scan.sh
#     --all, plus --untracked for the add); a plain `git commit` scans the staged diff only. `git -C <dir> add|commit` adds the
#     repository named there to the scan. Best effort: a pathspec commit (`git commit <file>`) and `git add -f` of an ignored
#     file are not widened.
if [ "$initialized" = true ] && [ "$(cfg_bool "$root" .secrets.scan_commits true)" = "true" ]; then
  GARG='((\\.|"[^"]*"|'"'"'[^'"'"']*'"'"'|[^[:space:]\\"'"'"'])+)'      # one shell word: pieces that are \-escaped, "quoted", 'quoted' or plain
  GIT_CMD='(^|[^[:alnum:]_./-])git([[:space:]]+(-[Cc][[:space:]]+'"$GARG"'|--(git-dir|work-tree|namespace)[[:space:]]+'"$GARG"'|-[^[:space:]]+))*[[:space:]]+'
  if printf '%s\n' "$cmd" | grep -E -q -e "${GIT_CMD}commit([[:space:]]|\$)"; then
    scope=staged; scan_flags=""
    if printf '%s\n' "$cmd" | grep -E -q -e '(^|[[:space:]])--all([[:space:]]|$)' -e '(^|[[:space:]])-[A-Za-z]*a[A-Za-z]*([[:space:]]|$)'; then
      scope=all; scan_flags="--all"
    fi
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
      block "possible secret in the changes being committed$where ($(one_line "$hits")). Why: credentials never enter version control (secrets.scan_commits). To proceed: take the value out of the diff (git restore --staged <file>, then edit it to read from the environment or a secret manager) and commit again.$scope_note"
    fi
  fi
fi
exit 0
