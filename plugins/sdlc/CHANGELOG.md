# Changelog

All notable changes to the `sdlc` plugin are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [0.4.3] - 2026-09-09

### Added
- README diagrams (Mermaid, rendered by GitHub): the two-command overview, the `/sdlc:go` flow with the human decision points highlighted, the `/sdlc:run` loop, and where each hook fires in a session.

## [0.4.2] - 2026-09-09

### Changed
- The marketplace now lives at github.com/freewisl/ai-native-sdlc: install commands in both READMEs, the team-settings example, ENTERPRISE and the ELI5 page point there; `init.sh`'s default marketplace source for the CI workflows is `freewisl/ai-native-sdlc` (override with `--marketplace` or `SDLC_MARKETPLACE_SOURCE` for a mirror); owner metadata updated.

## [0.4.1] - 2026-09-09

### Fixed
- `gh` login checks are scoped to the repository's host (`gh auth status --hostname <origin host>`) in run-loop, github-setup, init and doctor; an unscoped `gh auth status` fails whenever any other configured host (a company GitHub Enterprise) is expired or unreachable, which locked out everyone using two hosts. `GH_HOST` is exported so later `gh api`/`gh pr` calls target the same host; the device-flow login uses that host too.

## [0.4.0] - 2026-09-08

The loop that keeps running.

### Added
- `/sdlc:run` (user-typed only) + `scripts/run-loop.sh`: works through approved intents one at a time, each in a fresh headless `claude -p "/sdlc:go --slug … --autopilot --merge"` session; after at least one merge, a self-check pass (sdlc-reviewer + the scan checklist over the merged diff) files Important findings as approved intents through a PR, which the next iteration picks up; stops when the queue is empty or at a cap. Solo repositories only; `loop.enabled` must be set to true on purpose.
- Config `loop.*`: `enabled` (false), `max_items` (5), `max_minutes` (120), `item_max_minutes` (45), `max_turns` (200), `max_failures_per_slug` (3, then the slug is blocked in `.sdlc/state/loop-failures.txt`), `self_check` (true), `model`, `allowed_tools`, `pause_file` (`.sdlc/state/pause` — the stop switch). `init` adds the block (off) to existing configs.
- `templates/github/sdlc-autopilot.yml`: unattended `run-loop.sh --once` every 30 minutes; documents why `SDLC_GH_TOKEN` (fine-grained PAT) is needed for the loop's PRs to receive their checks.
- doctor row `autopilot loop`; metrics row "merged per run / items that needed a human" from `loop.run`; events `loop.run`, `loop.item`.

### Changed
- `sdlc-evals.yml` runs on every PR and finishes green immediately when no agent-configuration path changed, so the required status check `evals` is always reported (a `paths:` filter left code-only PRs blocked forever under the ruleset).
- `/sdlc:go` sets `plan/<slug>.md` `status: implemented` inside the PR (how `run` and `status` know an item is done) and, with `--slug` naming an approved intent, takes the request from the intent.

## [0.3.4] - 2026-09-07

### Fixed
- `/sdlc:go` no longer claims it will run `/sdlc:init` when the config is missing — `init` is `disable-model-invocation` (user-typed only, because it rewrites the repository and GitHub settings), so `go` now stops and asks the user to type it.

## [0.3.3] - 2026-09-07

Documentation realigned to how the plugin is actually used: `/sdlc:init` once per repository, `/sdlc:go` once per change.

### Changed
- Marketplace and plugin descriptions lead with the two-command usage; stage-by-stage skills are documented as the parts `go` calls and as the team/large-change path.
- Plugin README: quick start rewritten (two commands, what `init` decides on its own, `go` stop conditions, when to use stage skills), install commands for the company repository `<조직>/ai-native-sdlc`, CI auth described as `ci.auth`-driven, `github-setup.sh` paragraph rewritten (secret → auto-merge → ruleset, solo without bypass, Free-plan behaviour), FAQ on auto-merge and stage skills, GitHub-only note for PR automation, event names `go.run`/`verify.test_unlock`/`direct_push`, `approval_basis`.
- Root README, ENTERPRISE, PLAYBOOK-MAPPING (test counts 206/134), ELI5 pages (two-command section, direct-push lock, install section) updated; doctor/init "Next steps" first item now points at `/sdlc:go`.

## [0.3.2] - 2026-09-07

Follow-up after checking the pilot repository (private, Free plan).

### Fixed
- `github-setup.sh` sets `allow_auto_merge`/`delete_branch_on_merge` **before** the ruleset step, so repositories where rulesets are unavailable (403 on Free-plan private repos) still get them.
- `/sdlc:go --merge` falls back to `gh pr checks --watch` + direct squash merge when GitHub refuses auto-merge (unprotected base branch); never merges with failing or running checks.
- `init.sh` merges the CI auth line of existing `sdlc-*.yml` workflows when it disagrees with `ci.auth` (only that line; other workflows untouched).
- `/sdlc:init` runs `github-setup.sh` whenever sdlc workflows are in place (not only with `--github`) and makes the adoption commit through a branch + PR, since the push guard blocks the default branch.

### Added
- doctor rows: `CI auth` (config ↔ workflow secret lines, offline), `CI auth secret` now names the expected secret, `auto-merge` repository setting.

## [0.3.1] - 2026-09-07

Fixes from the final review of 0.3.0.

### Fixed
- CI secret name mismatch: `init.sh` now records the chosen auth in `.sdlc/config.json` (`ci.auth`) and `github-setup.sh` reads it, so workflows and the registered secret always agree.
- `/sdlc:go --merge` needs `allow_auto_merge`; `github-setup.sh` now enables it (and `delete_branch_on_merge`).
- Solo detection fails closed: an unknown collaborator count keeps team gates (init and github-setup); auto-detected solo never overrides an existing `roles.solo`; `--solo` is the explicit override.
- `protect.default_branch` is written from the detected branch, so the push guard works on `master`/`develop` repositories without `origin/HEAD`.
- Push guard parses refspecs: `+main`, `refs/heads/main`, `HEAD:refs/heads/main`, `--delete <default>`, `git -C … push`, `--all/--mirror` are blocked; `main:feature` and unrelated clauses (`… && echo main`) are not.
- Solo ruleset no longer adds an admin bypass by default (the `evals` check stays enforced); `--bypass` opts in and is documented as making evals advisory.
- `/sdlc:go` creates the branch before the first artifact commit, records `approved_by (autopilot)` + `approval_basis`, requires doctor's `auto_mode` ok for autopilot, never autopilots when model-invoked, logs test-lock unlocks, and pre-approves the tools it uses.
- `run-evals.sh` lists copied config files in `<work>.meta/copied.txt`; the secret check excludes exactly those, so a credential the agent writes into CLAUDE.md/.claude/ is still caught.
- doctor honors `SDLC_SKIP_GH_DETECT`; github-setup help text, dry-run-while-logged-out message, `--save-token-file`; invalid config JSON is reported instead of a false MERGED.

### Added
- Config keys `ci.auth`, `protect.default_branch` (detected), `roles.autopilot`; README sections "가장 짧은 사용법", "손길 최소화", "갱신(릴리스) 절차"; `/sdlc:metrics` row "Request → PR via /sdlc:go"; plugin eval `trigger-go`; tests: hooks 193→206, scripts 98→120.

## [0.3.0] - 2026-09-07

Less ceremony for one-person repositories.

### Added
- `/sdlc:go "<request>" [--autopilot] [--merge]`: the whole loop in one session — intent, spec, plan, implementation, verification (3 rounds), review, PR and check babysitting — pausing only for real decisions (policy conflict, persistent verification failure, plan unless autopilot, merge unless `--merge`, production). `roles.autopilot` sets the default.
- `init.sh` zero-flag defaults: GitHub remote → workflows installed; one collaborator (via `gh`) → `roles.solo`; stored subscription token → `--auth oauth`. `--no-github` / `--no-solo` opt out; `--solo` on an existing config now merges `roles.solo`.

## [0.2.1] - 2026-09-07

Fixes from the first real project run.

### Added
- `protect.block_direct_push` (default true): the Bash guard blocks the agent's direct `git push` to the default branch and points to a PR — branch protection as a hook for repositories where GitHub rulesets are unavailable (private repos on the Free plan).

### Fixed
- `run-evals.sh` wrote its own records (`result.json`, `stderr.txt`) inside the work directory, and the `no-secret-in-code` check scanned them plus the copied `CLAUDE.md`, so a repository whose secrets rule quotes a pattern failed the case. Records now live in `<work>.meta/`; the check judges the agent's output only (excludes `.sdlc/`, `.claude/`, `.git/`, `CLAUDE.md`, `REVIEW.md`).

## [0.2.0] - 2026-09-07

Minimal-touch GitHub setup and solo mode, after the first installed-session tests.

### Added
- `scripts/github-setup.sh`: CI auth secret + default-branch ruleset via `gh` (PR required, review-thread resolution, code-owner review where the plan allows, required check `evals`, no force-push/deletion, admin bypass for PRs on solo repos); `--login` starts the gh device flow non-interactively; token resolved from env → macOS keychain → `~/.config/sdlc/ci-token`, stored once with `--save-token`.
- `init.sh --auth api|oauth` renders the matching CI secret into all six workflows; `init.sh --solo` sets `roles.solo` and the intent/spec/review skills take approvals in-session.
- Skills that run plugin scripts pre-approve them via frontmatter `allowed-tools` with `${CLAUDE_PLUGIN_ROOT}` (verified in an installed session).
- `doctor` rows: branch ruleset and CI secret presence (when `gh` is available).

### Fixed
- `plugin.json` no longer re-declares `hooks/hooks.json` (the duplicate declaration made the installed plugin fail to load).
- Remaining second-audit items: "What changes" blocks on every play skill, plan-title note, README counts/TOC, duplicate lines.

## [0.1.0] - 2026-09-07

Initial release. Implements Anthropic's "The AI-native SDLC playbook" (claude.com/blog/the-ai-native-sdlc-playbook, 2026-08-21) as an installable Claude Code plugin.

### Added
- **Skills (17)** under the `/sdlc:` namespace: `init`, `doctor`, `intent`, `spec`, `plan`, `verify`, `evals`, `review`, `lesson`, `release`, `monitor`, `triage`, `scan`, `postmortem`, `policy`, `metrics`, `status`.
- **Agents (5)**: `sdlc-verifier`, `sdlc-reviewer`, `sdlc-diagnoser`, `sdlc-simplifier`, `sdlc-researcher`.
- **Hooks (7)** registered in `hooks/hooks.json`: SessionStart / SessionEnd context and audit logging, PreToolUse guards for edits (frozen and generated paths, test-file lock during fixes, approved-plan requirement, secret scan) and for Bash (per-environment deploy gate, staged-diff secret scan), PostToolUse formatter and verification tracking, Stop verify-before-done.
- **Scripts**: `lib/common.sh` (jq with python3 fallback), `init.sh` (non-destructive scaffold and merge), `doctor.sh`, `status.sh`, `run-evals.sh`, `monitor.py` (deterministic Western Electric band detection and tiered response), `metrics.py` (per-stage leading/lagging indicators), `secret-scan.sh` with `secret-patterns.txt`.
- **Templates**: artifact templates in English and Korean (`templates/en`, `templates/ko`); project configuration (`config.json`, `bands.json`, `frozen-paths.txt`, project/sandbox/managed settings, deploy MCP example, `.gitignore` fragment, OTel env); six GitHub Actions workflows (`sdlc-evals`, `sdlc-review`, `sdlc-spec-on-intent`, `sdlc-ci-triage`, `sdlc-monitor`, `sdlc-scan`) and a `CODEOWNERS` starter; three eval cases (`no-secret-in-code`, `respect-frozen-path`, `fix-without-editing-test`); the `secure-api-review` policy skill example.
- **Playbook examples, verbatim**: `templates/examples/` (copied to `.sdlc/examples/` by init) holds the article's 14 code blocks unchanged — intent.md, plan.md, CLAUDE.md "Payments service", verifier.md, the verification block, REVIEW.md, settings.json hook registration, production-gate.sh, agent-evals.yml, the triage pipeline step, bands.yaml, the Stage 2 prompt, secure-api-review SKILL.md, the managed-settings JSON.
- **Approval-gate register** `.sdlc/APPROVALS.md` (en/ko) and a change-ticket gate for migrations/infra (`protect.ticketed_paths`, `protect.ticket_env`, default `CHANGE_TICKET`).
- **review-gate.sh**: turns the reviewer's `SDLC_REVIEW_TALLY` line into an optional merge check (Important count); wired as a commented step in `sdlc-review.yml`.
- **Monitor**: `report` route (leadership report for process metrics), disabled example metrics `post_deploy_5xx_rate` and `pr_cycle_time_hours`, `_tuning` notes; `sdlc-monitor.yml` opens a PR instead of pushing to the default branch.
- **init.sh options** `--marketplace`, `--ci-workflow`, `--team` so installed workflows and CODEOWNERS render without placeholders; `commands.lint_file` per-file linter; `claude_md.max_lines`; `source_of_truth.per_artifact`.
- **Documentation**: plugin manual (Korean) in `README.md`, enterprise deployment guide in `docs/ENTERPRISE.md`, playbook-to-component mapping in `docs/PLAYBOOK-MAPPING.md`.
- **Tests**: `tests/run.sh` runs the hook matrix (`tests/hooks.sh`, 185 checks incl. macOS bash 3.2 and a jq-less python fallback), the script suite (`tests/scripts.sh`, 85 checks: idempotent init on an empty directory, non-destructive merge into an existing project, doctor/status/metrics/run-evals/monitor) and `claude plugin validate --strict`.
- **Plugin evals**: four `claude plugin eval` trigger cases under `evals/` (intent, plan, review, doctor) — runnable once `plugin eval` early access is enabled for the account.

### Notes
- `hooks/hooks.json` is auto-discovered; the manifest must not re-declare it (doing so fails plugin load with "Duplicate hooks file detected") — guarded by a test.
- Skills that run plugin scripts pre-approve them through frontmatter `allowed-tools` with `${CLAUDE_PLUGIN_ROOT}` (verified in an installed session).
- Hosted products referenced by the playbook (Claude Code Review, Claude Security, Claude Tag, Claude Design, Cowork) are documented, not implemented; the plugin provides the local equivalents (`/sdlc:review`, `/sdlc:scan`, `/sdlc:postmortem`).
- The plugin never approves a pull request. Approval stays with a human through branch protection.
