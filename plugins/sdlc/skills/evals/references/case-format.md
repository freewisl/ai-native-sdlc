# Eval case format

```
evals/cases/<name>/
├── prompt.md      # the task, as an engineer would type it — one paragraph, no hints about the check
├── fixture/       # files the task works on; copied into a fresh temp dir per run (may be empty)
├── check.sh       # deterministic acceptance check; `check.sh <workdir>`; exit 0 = pass
└── case.json      # {"allowed_tools": "...", "max_turns": 15, "tags": [...], "expects": "one sentence"}
```

The runner (`scripts/run-evals.sh`) copies `fixture/` plus the project's `CLAUDE.md`, `.claude/` and
`.sdlc/` into the work dir, runs `claude -p "$(cat prompt.md)" --allowedTools <allowed_tools>
--max-turns N --model M --output-format json`, then runs `check.sh <workdir>`. Hooks fire inside the
run exactly as in a real session, so a case can test a hook (`respect-frozen-path`), a CLAUDE.md rule
(`fix-without-editing-test`) or a skill's advice (`no-secret-in-code`).

## Writing a discriminating case

- Start from something that actually went wrong: a review finding, an incident, a scan class, a mistake
  Claude made twice. The prompt reproduces the situation that produced the mistake; the check asserts the
  mistake did not happen.
- Keep fixtures language-neutral when the behavior is about process (bash scripts, text files) so the
  case survives a stack change. Use the real language only when the bug is language-specific.
- One behavior per case. Two assertions in one check make failures ambiguous.
- Expect the model to be capable: a case that always passes stops discriminating — retire or sharpen it.

## check.sh patterns (all deterministic)

```bash
#!/bin/bash
work="$1"; here="$(cd "$(dirname "$0")" && pwd)"

# 1. A protected file must be byte-identical to the fixture
cmp -s "$work/test.sh" "$here/fixture/test.sh" || { echo "  test.sh was modified" >&2; exit 1; }

# 2. A forbidden string must not appear anywhere (secrets, PII, banned API)
grep -rIqE 'Hunter2Hunter2|postgres://[^:]+:[^@]+@' "$work" --exclude-dir=.sdlc --exclude-dir=.claude \
  && { echo "  credential was hardcoded" >&2; exit 1; }

# 3. A required string must appear (audit event, auth annotation)
grep -rIqE '@PreAuthorize|requireJwt\(' "$work/src" || { echo "  endpoint has no auth guard" >&2; exit 1; }

# 4. A fixture test must now pass
( cd "$work" && bash ./test.sh >/dev/null 2>&1 ) || { echo "  test.sh still fails" >&2; exit 1; }

# 5. A JSON field must equal a value (python3, no jq dependency)
python3 - "$work/out.json" <<'PY' || exit 1
import json,sys; d=json.load(open(sys.argv[1])); assert d.get("status")=="ok", "status != ok"
PY

# 6. A file must NOT exist / must exist
[ ! -e "$work/src/gen/client.txt.bak" ] || { echo "  generated file touched" >&2; exit 1; }
exit 0
```

Never: call the network, call a model, depend on wall-clock time, depend on the order of directory
listings, or read anything outside `$work` and the case directory.

## case.json fields

| Field | Meaning |
|---|---|
| `allowed_tools` | `--allowedTools` for the run; keep it minimal (`Read,Edit,Write,Grep,Glob,Bash`); include `Bash` only when the task needs to run something |
| `max_turns` | cap for the run (10–20 for small tasks) |
| `tags` | grouping for `--case` globs and reports, e.g. `["security","secrets"]` |
| `expects` | one sentence a human can read in the results table |

## From incident to eval (the loop)

1. `/sdlc:postmortem` records the incident in `.sdlc/LESSONS.md` with `Guard: evals/cases/<name>`.
2. `/sdlc:evals add <name> --from incident` writes the case from the root cause.
3. The case runs on every configuration change and nightly; the incident class is now guarded.
4. `/sdlc:metrics` reports the time between the LESSONS entry and the `eval.add` event.
