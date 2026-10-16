#!/bin/bash
# sdlc plugin — secret scanner (design §6).
#   secret-scan.sh --text [<root>]            scan text from stdin
#   secret-scan.sh --staged <root> [--all] [--untracked]
#                                             scan added lines of `git diff --cached`; every added line of every file counts, no directory is exempt
#                                             --all        …and `git diff HEAD` instead: the unstaged changes of tracked files too (what `commit -a` takes)
#                                             --untracked  …and the text files git neither tracks nor ignores (what `git add` could stage): at most
#                                                          200 files, 256 KB each, 1 MB in all; symlinks, binaries and anything .gitignore covers are skipped
# Patterns: scripts/secret-patterns.txt (+ secrets.extra_patterns from <root>/.sdlc/config.json when <root> is initialized). They are
# case-sensitive; a pattern that starts with (?i) ignores case, one that starts with (?v) is a value pattern (see the pattern file). The
# patterns of secrets.extra_patterns ignore case, as they always did — (?c) in front of one makes it case-sensitive.
# Output: one line per hit pattern, "pattern#<n>\t<label>\t<where>[\tencoded]" — where = up to three places ("line 3" for --text,
# "src/a.py:3" for --staged), encoded = the hit sits inside a long run of base64-like characters (it is probably data, not a credential).
# The matched secret itself is NEVER printed.
# Exit: 0 clean, 1 hit(s), 2 usage error. Allowlist tokens come from the pattern file header; template references (${…}, {{…}}) are also skipped.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/lib/common.sh"
. "$here/lib/hooks-extra.sh"

mode="${1:-}"; [ $# -gt 0 ] && shift
root="${1:-}"; [ $# -gt 0 ] && shift
all=false; untracked=false
case "$mode" in
  --text) ;;
  --staged) [ -n "$root" ] || { echo "usage: secret-scan.sh --staged <root> [--all] [--untracked]" >&2; exit 2; }
            for opt in "$@"; do
              case "$opt" in --all) all=true;; --untracked) untracked=true;; esac
            done ;;
  *) echo "usage: secret-scan.sh --text [<root>] | --staged <root> [--all] [--untracked]" >&2; exit 2 ;;
esac

pattern_file="$here/secret-patterns.txt"
[ -f "$pattern_file" ] || pattern_file="$(sdlc_plugin_root)/scripts/secret-patterns.txt"

# --- patterns: file (comments/blank skipped) + extra_patterns. P_RE[n] = the regular expression, P_FL[n] = its flags (i, v).
# The "# Allowlist tokens (never flagged): a, b, c" header line of the file is the allowlist; template references are skipped too.
P_RE=(); P_FL=(); allow_line=""
add_pattern() { # add_pattern <line> <default flags>
  local line="$1" fl="$2" extra=""
  case "$line" in
    '(?'*')'*) extra="${line#'(?'}"; extra="${extra%%')'*}"; line="${line#*')'}"
               case "$extra" in *c*) fl="${fl//i/}";; esac
               fl="$fl${extra//c/}";;
  esac
  [ -n "$line" ] || return 0
  P_RE[${#P_RE[@]}]="$line"; P_FL[${#P_FL[@]}]="$fl"
}
while IFS= read -r line || [ -n "$line" ]; do
  line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
  case "$line" in
    '') continue;;
    '#'*) if [ -z "$allow_line" ]; then case "$line" in *[Aa]llowlist\ [Tt]okens*) allow_line="${line#*:}";; esac; fi; continue;;
  esac
  add_pattern "$line" ""
done < "$pattern_file"
if [ -n "$root" ] && sdlc_initialized "$root"; then
  while IFS= read -r line; do
    [ -n "$line" ] && add_pattern "$line" "i"
  done <<< "$(cfg_lines "$root" .secrets.extra_patterns)"
fi
ALLOW=('${' '{{' '<%')
if [ -n "$allow_line" ]; then
  IFS=',' read -r -a toks <<< "$allow_line"
  for tok in "${toks[@]}"; do
    tok="${tok#"${tok%%[![:space:]]*}"}"; tok="${tok%"${tok##*[![:space:]]}"}"
    [ -n "$tok" ] && ALLOW[${#ALLOW[@]}]="$tok"
  done
else
  ALLOW=("${ALLOW[@]}" example REPLACE_ME changeme '<your-' xxxx dummy placeholder)
fi
allow_args=(); for tok in "${ALLOW[@]}"; do allow_args[${#allow_args[@]}]="-e"; allow_args[${#allow_args[@]}]="$tok"; done

# --- working files in a temp dir: text = the text to scan, one line per line of it; locs (for --staged) = where each run of lines came
# from, one "file<TAB>first line<TAB>count" row per hunk, in the order of the text
td=$(mktemp -d "${TMPDIR:-/tmp}/sdlc-scan.XXXXXX") || exit 0
trap 'rm -rf "$td"' EXIT
tfile="$td/text"; lfile="$td/locs"; : > "$tfile"; : > "$lfile"

# untracked_text <root> → appends to the text (and locs) every line of the text files git neither tracks nor ignores, as if they were all
# added. Bounded: the first 200 files git lists, regular files only (a symlink is not followed), at most 256 KB each, binaries skipped,
# 5000 lines a file, 1 MB of text in all.
untracked_text() {
  local root="$1" f size name sized="" listed
  local -a files=() ok=() texts=()
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$root/$f" ] && [ ! -L "$root/$f" ] && files[${#files[@]}]="./$f"
  done <<< "$(git -C "$root" -c core.quotepath=off ls-files -o --exclude-standard 2>/dev/null | head -n 200)"
  [ "${#files[@]}" -gt 0 ] || return 0
  sized=$(cd "$root" 2>/dev/null && wc -c "${files[@]}" 2>/dev/null)
  while read -r size name; do
    case "$size" in ''|*[!0-9]*) continue;; esac
    [ "$size" -le 262144 ] && [ -n "$name" ] && [ "$name" != total ] && ok[${#ok[@]}]="$name"
  done <<< "$sized"
  [ "${#ok[@]}" -gt 0 ] || return 0
  listed=$(cd "$root" 2>/dev/null && LC_ALL=C grep -I -l '' "${ok[@]}" 2>/dev/null)       # C locale: only a NUL byte makes a file binary, not a legacy encoding
  while IFS= read -r f; do [ -n "$f" ] && texts[${#texts[@]}]="$f"; done <<< "$listed"
  [ "${#texts[@]}" -gt 0 ] || return 0
  ( cd "$root" 2>/dev/null && LC_ALL=C awk -v lf="$td/u-locs" '
      function flush() { if (cnt > 0) print f "\t1\t" cnt >> lf; cnt = 0 }
      FNR == 1 { flush(); f = FILENAME; sub(/^\.\//, "", f); n = 0 }
      ++n <= 5000 { print; cnt++ }
      END { flush() }' "${texts[@]}" 2>/dev/null ) | head -c 1048576 >> "$tfile"
  [ -f "$td/u-locs" ] && cat "$td/u-locs" >> "$lfile"
  return 0
}

# --- text to scan
if [ "$mode" = "--text" ]; then
  cat > "$tfile"
else
  git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
  dargs=(diff --cached); $all && git -C "$root" rev-parse --verify HEAD >/dev/null 2>&1 && dargs=(diff HEAD)
  # every added line of every file, no path exempt: a directory of fixtures is not a reason to wave real credentials through.
  # The per-file header (diff … up to the first @@) is skipped by state, so an added line that itself starts with "++ " is still scanned;
  # the file name comes from its +++ line, the first line number of a run from the @@ line.
  # bytes, not characters: in a UTF-8 locale awk (macOS) stops at a line in a legacy encoding, and what follows is never scanned
  git -C "$root" -c core.quotepath=off "${dargs[@]}" --no-color --no-ext-diff --src-prefix=a/ --dst-prefix=b/ --unified=0 --diff-filter=ACMR 2>/dev/null \
    | LC_ALL=C awk -v lf="$lfile" '
      function flush() { if (cnt > 0) print f "\t" start "\t" cnt >> lf; cnt = 0 }
      /^diff /   { flush(); hdr = 1; f = ""; next }
      hdr && /^\+\+\+ / { f = substr($0, 5); sub(/\t.*$/, "", f); sub(/^b\//, "", f); next }
      /^@@ /     { flush(); hdr = 0; start = 1; if (match($0, /\+[0-9]+/)) start = substr($0, RSTART + 1, RLENGTH - 1) + 0; next }
      hdr        { next }
      /^\+/      { print substr($0, 2); cnt++ }
      END        { flush() }' >> "$tfile"
  if $untracked; then untracked_text "$root"; fi
fi
[ -s "$tfile" ] || exit 0

# --- candidates: the lines that match one of the patterns, found with every pattern at once (the case-sensitive ones, and the
# case-insensitive ones, two greps side by side). Only these lines are looked at pattern by pattern, which names the pattern and
# applies the allowlist — a diff of megabytes with one hit costs the two greps and a look at a handful of lines. A pattern with a
# back-reference means something else inside the combination, and a grep error proves nothing: both make every line a candidate.
# A handful (SDLC_SCAN_NATIVE_MAX, default 60) is looked at with bash's own regular expressions: no process per pattern. More than
# that goes through one grep per pattern, as before.
nmax="${SDLC_SCAN_NATIVE_MAX:-60}"; case "$nmax" in ''|*[!0-9]*) nmax=60;; esac
comb_cs=""; comb_ci=""; whole=false; n=0
while [ "$n" -lt "${#P_RE[@]}" ]; do
  pat="${P_RE[$n]}"
  case "$pat" in *'\1'*|*'\2'*|*'\3'*|*'\4'*|*'\5'*|*'\6'*|*'\7'*|*'\8'*|*'\9'*) whole=true;; esac
  case "${P_FL[$n]}" in *i*) comb_ci="${comb_ci:+$comb_ci|}($pat)";; *) comb_cs="${comb_cs:+$comb_cs|}($pat)";; esac
  n=$((n + 1))
done
cfile="$td/cand"; nfile="$td/nums"
CN=(); CL=(); native=false
if $whole; then
  cp "$tfile" "$cfile"; awk '{ print NR }' "$tfile" > "$nfile" 2>/dev/null
else
  rc_cs=1; rc_ci=1
  if [ -n "$comb_cs" ]; then ( LC_ALL=C grep -E -n -e "$comb_cs" "$tfile" > "$td/hits-cs" 2>/dev/null; echo $? > "$td/rc-cs" ) & fi
  if [ -n "$comb_ci" ]; then ( LC_ALL=C grep -E -i -n -e "$comb_ci" "$tfile" > "$td/hits-ci" 2>/dev/null; echo $? > "$td/rc-ci" ) & fi
  wait
  [ -f "$td/rc-cs" ] && read -r rc_cs < "$td/rc-cs"; [ -f "$td/rc-ci" ] && read -r rc_ci < "$td/rc-ci"
  if [ "$rc_cs" -gt 1 ] || [ "$rc_ci" -gt 1 ]; then       # a pattern grep cannot compile: look at every line, one pattern at a time below
    cp "$tfile" "$cfile"; awk '{ print NR }' "$tfile" > "$nfile" 2>/dev/null
  else
    [ "$rc_cs" = 0 ] || [ "$rc_ci" = 0 ] || exit 0         # no line matches any pattern: clean
    if [ "$rc_cs" = 0 ] && [ "$rc_ci" = 0 ]; then          # both kinds hit: one list in line order, a line once
      cat "$td/hits-cs" "$td/hits-ci" 2>/dev/null | LC_ALL=C sort -t: -k1,1n -u > "$td/hits"
    elif [ "$rc_cs" = 0 ]; then mv "$td/hits-cs" "$td/hits"
    else mv "$td/hits-ci" "$td/hits"; fi
    while IFS= read -r line; do
      CN[${#CN[@]}]="${line%%:*}"; CL[${#CL[@]}]="${line#*:}"
      [ "${#CN[@]}" -gt "$nmax" ] && break
    done < "$td/hits"
    if [ "${#CN[@]}" -le "$nmax" ]; then native=true
    else cut -d: -f1 "$td/hits" > "$nfile"; cut -d: -f2- "$td/hits" > "$cfile"; fi
  fi
fi

# --- places: the line of the text (and the file it was added to, for --staged) of a hit
LF_N=-1; LF_F=(); LF_S=(); LF_C=(); PLACE=""
place_of_num() { # place_of_num <line number in the text> → PLACE
  if [ "$mode" = "--text" ]; then PLACE="line $1"; return; fi
  if [ "$LF_N" -lt 0 ]; then
    local f s c; LF_N=0
    while IFS=$'\t' read -r f s c; do LF_F[$LF_N]="$f"; LF_S[$LF_N]="$s"; LF_C[$LF_N]="$c"; LF_N=$((LF_N + 1)); done < "$lfile"
  fi
  local i=0 acc=0
  while [ "$i" -lt "$LF_N" ]; do
    if [ "$1" -le $((acc + LF_C[i])) ]; then PLACE="${LF_F[i]}:$((LF_S[i] + $1 - acc - 1))"; return; fi
    acc=$((acc + LF_C[i])); i=$((i + 1))
  done
  PLACE="?"
}
# place_of <k> → where line <k> of the candidate file came from (grep path)
place_of() {
  local num
  num=$(sed -n "${1}p" "$nfile" 2>/dev/null)
  place_of_num "$num"; printf '%s' "$PLACE"
}

# in_run <candidate line> <match> → 0 when the match sits inside a run of 60 or more base64-like characters of that line (grep path)
enc_re='[A-Za-z0-9+/=_-]{60,}'
run_holds() { # run_holds <line text> <match>
  local rest="$1" run
  shopt -u nocasematch
  while [[ "$rest" =~ $enc_re ]]; do
    run="${BASH_REMATCH[0]}"
    case "$run" in *"$2"*) return 0;; esac
    rest="${rest#*"$run"}"
  done
  return 1
}
in_run() { run_holds "$(sed -n "${1}p" "$cfile")" "$2"; }

# label_of <pattern> → LABEL: the pattern, tabs as blanks, at most 40 characters
label_of() { LABEL="${1//$'\t'/ }"; if [ "${#LABEL}" -gt 40 ]; then LABEL="${LABEL:0:40}..."; fi; }

rc=0
if $native; then
  # --- scan, a handful of candidate lines: per pattern, every match on them (bash regular expressions, bytes not characters), minus the
  # allowlisted ones; report the pattern and where (never the value)
  scan_native() {
    local LC_ALL=C
    local n=0 total=${#P_RE[@]} nc=${#CL[@]} pat fl k rest m val tok allowed lastk nchk enc nks where kk
    local -a ks
    while [ "$n" -lt "$total" ]; do
      pat="${P_RE[$n]}"; fl="${P_FL[$n]}"; n=$((n + 1))
      ks=(); nks=0; lastk=-1; enc="encoded"; nchk=0; k=0
      while [ "$k" -lt "$nc" ]; do
        rest="${CL[$k]}"
        while true; do
          case "$fl" in *i*) shopt -s nocasematch;; *) shopt -u nocasematch;; esac
          [[ "$rest" =~ $pat ]] || break
          m="${BASH_REMATCH[0]}"; [ -n "$m" ] || break
          rest="${rest#*"$m"}"
          val="$m"
          case "$fl" in *v*)                                  # a value pattern: what follows the first : or = is the value, it needs a digit
            val="${m#*[:=]}"; val="${val#"${val%%[![:space:]]*}"}"
            case "$val" in *[0-9]*) ;; *) continue;; esac;;
          esac
          shopt -s nocasematch; allowed=false
          for tok in "${ALLOW[@]}"; do case "$val" in *"$tok"*) allowed=true; break;; esac; done
          $allowed && continue
          if [ "$k" != "$lastk" ]; then lastk=$k; if [ "$nks" -lt 3 ]; then ks[$nks]=$k; nks=$((nks + 1)); fi; fi
          case "$fl" in *v*) enc="";; *)                        # encoded only when every hit of the pattern (the first 20) sits inside such a run
            if [ -n "$enc" ]; then
              nchk=$((nchk + 1))
              if [ "$nchk" -gt 20 ]; then enc=""; else run_holds "${CL[$k]}" "$m" || enc=""; fi
            fi;;
          esac
        done
        k=$((k + 1))
      done
      [ "$nks" -gt 0 ] || continue
      where=""; for kk in "${ks[@]}"; do place_of_num "${CN[$kk]}"; where="${where:+$where, }$PLACE"; done
      label_of "$pat"
      printf 'pattern#%s\t%s\t%s%s\n' "$n" "$LABEL" "$where" "${enc:+$'\t'$enc}"
      rc=1
    done
  }
  scan_native
else
  # --- scan: for each pattern, find matches, drop allowlisted ones, report the pattern and where (never the value)
  n=0
  while [ "$n" -lt "${#P_RE[@]}" ]; do
    pat="${P_RE[$n]}"; fl="${P_FL[$n]}"; n=$((n + 1))
    ci=""; case "$fl" in *i*) ci="-i";; esac
    matches=$(LC_ALL=C grep -E $ci -n -o -e "$pat" "$cfile" 2>/dev/null) || continue      # bytes, not characters: text in a legacy encoding is not "binary"
    [ -n "$matches" ] || continue
    case "$fl" in
      *v*) kept=$(printf '%s\n' "$matches" | sed -E 's/^([0-9]+):[^:=]*[:=][[:space:]]*/\1:/' | LC_ALL=C grep -E '^[0-9]+:.*[0-9]' | grep -v -i -F "${allow_args[@]}");;
      *)   kept=$(printf '%s\n' "$matches" | grep -v -i -F "${allow_args[@]}");;
    esac
    [ -n "$kept" ] || continue
    ks=$(printf '%s\n' "$kept" | cut -d: -f1 | sort -un | head -n 3)
    where=""; for k in $ks; do where="${where:+$where, }$(place_of "$k")"; done
    enc="encoded"; nchk=0                                     # encoded only when every hit of the pattern (the first 20) sits inside such a run
    case "$fl" in *v*) enc="";; *)
      while IFS= read -r hit; do
        [ -n "$hit" ] || continue
        nchk=$((nchk + 1)); if [ "$nchk" -gt 20 ]; then enc=""; break; fi
        in_run "${hit%%:*}" "${hit#*:}" || { enc=""; break; }
      done <<< "$kept";;
    esac
    label_of "$pat"
    printf 'pattern#%s\t%s\t%s%s\n' "$n" "$LABEL" "$where" "${enc:+$'\t'$enc}"
    rc=1
  done
fi
exit $rc
