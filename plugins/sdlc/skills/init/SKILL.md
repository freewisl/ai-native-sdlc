---
name: init
description: Installs the AI-native SDLC playbook into the current repository — scaffolds .sdlc/config.json, the intent/spec/plan chain, REVIEW.md, evals and CLAUDE.md sections non-destructively, then fills CLAUDE.md from a repository inspection and runs the doctor. Use when the user says "set up sdlc", "initialize the playbook", "install the sdlc harness in this repo", "add CLAUDE.md and hooks", "bootstrap the sdlc plugin", "onboard this project to the sdlc plugin", "sdlc 초기화", "플레이북 설치해줘", "sdlc 셋업", "이 프로젝트에 sdlc 적용". Works the same for an empty new project and an existing codebase.
argument-hint: "[--lang en|ko] [--github|--no-github] [--solo|--no-solo] [--auth api|oauth] [--sandbox] [--managed] [--mcp] [--dry-run] [--commands build=..,test=..,lint=..]"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(gh auth status*), Bash(gh repo view*), Bash(gh secret list*), Bash(gh api repos/*/rulesets*)
---

# /sdlc:init — install the playbook into this repository

## What changes
- **Traditional:** Conventions, commands, architecture and the mistakes the team sees most often live in people's heads and on wikis; a new joiner learns them by asking around.
- **AI-native:** CLAUDE.md gives Claude the context a new joiner would need, read at the start of every session, maintained by the whole team and iterated on whenever a mistake is made twice — institutional knowledge becomes a versioned file the agent reads.


## When to use
Once per repository, at adoption time, or again after upgrading the plugin (re-running is safe: nothing
existing is overwritten). It is the single entry point for both a brand-new empty repository and an
existing project with its own CLAUDE.md, settings and CI.

Prerequisites: None (CLAUDE.md is a start-anywhere play).
Infrastructure: A repo, Claude Code installed, and one engineer who knows the codebase well.

## Inputs
- `$ARGUMENTS` flags, passed through to `init.sh`: `--lang en|ko`, `--github`, `--sandbox`, `--managed`,
  `--mcp`, `--dry-run`, `--dir <root>`, `--commands build=..,test=..,lint=..`. Anything else in
  `$ARGUMENTS` is treated as a note from the user (e.g. "we use pnpm").
- The repository itself: build files, README, CI config, existing `CLAUDE.md`, `.claude/settings.json`.
- `${CLAUDE_PLUGIN_ROOT}/skills/init/references/claude-md-guide.md` — detection heuristics and the rules
  for filling each CLAUDE.md section. Read it before step 4.

## Steps
1. **Check git.** Run `git rev-parse --show-toplevel`. If this is not a repository, stop and explain:
   the playbook's audit trail is the chain of commits (author, timestamp, approval), so the hooks,
   `status.sh` and `metrics.py` all read git. Offer to run `git init` (and an initial commit) and continue
   only after the user agrees.
2. **Inspect the repository** (read-only, keep it under a minute): package manager and build files
   (`package.json`, `pom.xml`, `build.gradle*`, `Makefile`, `go.mod`, `Cargo.toml`, `pyproject.toml`,
   `requirements.txt`, `*.csproj`, `Gemfile`), README, CI (`.github/workflows`, `.gitlab-ci.yml`,
   `Jenkinsfile`), linters/formatters configs, top-level directory layout, existing `CLAUDE.md` and
   `.claude/settings.json`. Derive `build`, `test`, `lint` (and `run` when obvious) commands using the
   table in `references/claude-md-guide.md`. A `--commands` flag from the user wins over detection.
   In an empty repository there is nothing to detect: leave the commands empty and note it.
3. **Dry run, then real run.** Execute
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/init.sh" --dry-run <flags> --commands build=..,test=..,lint=..`
   and show the user the create/merge/skip table it prints. Then run the same command without
   `--dry-run` (skip this when the user asked for `--dry-run`). `init.sh` never overwrites: existing
   files are merged (settings permissions union, CLAUDE.md missing sections only) or reported as skipped.
4. **Fill CLAUDE.md.** (If `CLAUDE.md` did not exist, Claude Code's built-in `/init` may be run first to draft it from what it finds; `/sdlc:init` then adds the five section markers on top and trims to what a new joiner needs on day one.) Open `CLAUDE.md` and edit only the text between `<!-- sdlc:begin X -->` and
   `<!-- sdlc:end X -->` markers that `init.sh` added (if `CLAUDE.md` pre-existed, sections that were
   already there have no markers — leave them untouched):
   - `commands`: one line per command with an example of healthy output. Run each detected command once
     when it is non-destructive and finishes in a couple of minutes, and quote its last healthy line
     (e.g. `Tests: 42 passed`); otherwise write `example: (run once and paste the healthy tail here)`.
   - `conventions`: language and framework versions, style rules the linter enforces, 3–6 rules found in
     README/contributing docs. No aspirational rules.
   - `architecture`: 3–6 bullets — top-level directories and what talks to what; name generated or
     frozen paths explicitly (they also go into `.sdlc/frozen-paths.txt`).
   - `mistakes`: replace the stub with exactly `- (none yet)`; `/sdlc:lesson` fills it later.
   - `verifying`: keep the template block, substitute the real commands; write `TODO: no <build|lint>
     command found` for anything missing rather than inventing one.
   Keep the whole file under a page (about 120 lines): Claude reads all of it every session.
5. **Write the commands into config.** Make sure `.sdlc/config.json` `commands.build/test/lint/run` hold
   the same commands as CLAUDE.md (edit with `python3 -c` json load/dump; do not hand-edit JSON).
6. **Optional integrations.** Zero-flag defaults fire first and are printed as NOTE lines — repeat them to the user: a GitHub
   remote (github.com or a `github.*` Enterprise host) installs the workflows without `--github` (`--no-github` refuses); a single
   collaborator reported by `gh` sets `roles.solo` (`--no-solo` refuses; an unknown count keeps team gates; an existing `roles.solo`
   is never overridden by detection, only by `--solo`); a stored subscription token selects `oauth`. The chosen auth is written to
   `.sdlc/config.json` (`ci.auth`) so `github-setup.sh` registers the matching secret. `--github`: `init.sh` copies `sdlc-*.yml`
   workflows and CODEOWNERS that do not exist yet. Ask which CI auth the user wants only when nothing decided it: `--auth api`
   (secret `ANTHROPIC_API_KEY`, API billing) or `--auth oauth`
   (secret `CLAUDE_CODE_OAUTH_TOKEN` created locally with `claude setup-token`, billed to the subscription). Then — whenever
   `.github/workflows/sdlc-*.yml` are in place, whether `init.sh` just created them, they already existed, or the GitHub remote was
   auto-detected — **do the GitHub side for them** with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/github-setup.sh"` (needs `gh` logged in with admin rights):
   it sets the secret (`--token-stdin` — ask the user to run `claude setup-token` and paste the token; never echo it) and creates
   the default-branch ruleset the playbook assumes (PR required, review-thread resolution, code-owner review where the plan
   allows, required status check `evals`, no force-push/deletion; on a solo repository approvals 0 and no code-owner requirement
   so the owner can merge their own PRs while `evals` stays required — `--bypass` would let the admin skip that too, so avoid it
   unless asked; it also enables `allow_auto_merge` for `/sdlc:go --merge`). Run `--dry-run` first and show the payload. If `gh` is missing, print the two manual steps it reports.
   **Minimize human steps**: (a) if `gh auth status --hostname <the repository's host>` fails (scoped: an expired login on another host, e.g. a company GitHub Enterprise, must not count), run `github-setup.sh --login` in the background — it prints
   `SDLC_DEVICE_CODE=XXXX-XXXX` and the URL; when a browser tool (Claude in Chrome) is available, open
   https://github.com/login/device yourself, enter the code and approve, otherwise show the code to the user (one click);
   (b) the CI token is needed once per account, not per repository: if `github-setup.sh` reports no stored token, ask the user to
   run `! claude setup-token` in their terminal and paste the result, then call `github-setup.sh --token-stdin --save-token` so it
   is kept in the keychain (or `~/.config/sdlc/ci-token`) and every later repository is fully automatic; (c) with `--solo`
   (or `roles.solo: true`) the user is product owner, tech lead and release manager — do not tell them to "wait for the product
   owner"; the approvals happen in-session. `--sandbox`: the sandbox
   settings template is written for review, not merged blindly — show it. `--managed`: writes
   `managed-settings.json` as a template to hand to IT; explain the OS-specific install paths from
   `docs/ENTERPRISE.md` and that engineers cannot self-install managed settings. `--mcp`: `.mcp.json`
   example for deploy tooling (only when absent).
7. **Doctor and next steps.** Run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.sh"` and show its table
   and its "Next steps" list exactly as printed — that list is the single source of the playbook's adoption
   order (dependency figure: layer 1 start-anywhere plays intent · CLAUDE.md · feedback loop · hook gate list ·
   plan mode → layer 2 skills · subagents · evals → layer 3 requirements & design · PR review → layer 4 CI/CD →
   layer 5 closing the loop). Remind that hooks come from the plugin and need a Claude Code restart to activate.
8. **Commit — through a pull request, like everything else.** If `auto_commit` is true: when the checkout is on the default
   branch, `git checkout -b sdlc/adopt-<yyyymmdd>` first (the push guard blocks direct pushes to the default branch, by design);
   `git add` the files the table listed as created/merged and `git commit -m "chore(sdlc): initialize AI-native SDLC playbook"`.
   With a GitHub remote and `gh` logged in: `git push -u origin <branch>` and `gh pr create --fill`; on a solo repository merge it
   once its checks are green (`gh pr checks <n> --watch`, then `gh pr merge --squash --delete-branch <n>`), otherwise hand the PR
   URL to the team. Without a remote, the local commit is the record. If `auto_commit` is false print these commands instead.

## Output
The init table (created / merged / skipped), the filled `CLAUDE.md` (show it), the doctor table, and
the "Next steps" list. Mention explicitly which pre-existing files were left alone.

## Governance
- The intent home needs an owner decision: who may write to it (teams, connector accounts) — contributors come from across the organization — expressed as repository permissions and the `intent/` line in CODEOWNERS.
- Non-destructive by contract: never overwrite `CLAUDE.md`, `REVIEW.md`, settings, workflows or
  templates that already exist; never delete anything.
- Do not write hooks into `.claude/settings.json` — the plugin provides them; managed settings are for
  the platform/IT admin to deploy, this skill only produces the template.
- The scaffold is the platform team's one-time task; the content decisions in CLAUDE.md are reviewed
  like code by code owners in the commit/PR.

## Measurement
Append `init.run` to `.sdlc/logs/events.jsonl` (design §2.3 shape, `decision: allow`,
`detail: {"flags": "<flags>", "created": n, "merged": n, "skipped": n}`) after step 3:
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"init.run","session_id":"","decision":"allow","reason":"playbook scaffolded","detail":{"flags":"%s"}}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<flags>" >> .sdlc/logs/events.jsonl
```
Feeds the adoption view in `/sdlc:metrics` (plays adopted per repository) and dates the start of every
Stage-1 "time to intent" measurement.
