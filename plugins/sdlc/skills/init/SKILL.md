---
name: init
description: Adopts sdlc in this repository once — detects the stack and the build/test/lint commands, creates .sdlc/config.json, work/, REVIEW.md and the CLAUDE.md sections without overwriting anything, fills in conventions and architecture, migrates a 0.x layout, commits the adoption (a pull request on GitHub) and can set up the GitHub ruleset. With --check it is the health check. Use when the user types /sdlc:init or says "set up sdlc", "adopt sdlc", "install sdlc in this repo", "migrate to sdlc 1.0", "sdlc health check", "doctor", "sdlc 붙여줘", "이 저장소에 sdlc 적용", "sdlc 초기화", "점검해줘", "설정 확인해줘".
argument-hint: "[--check] [--lang en|ko] [--solo|--team] [--commands build=..,test=..,lint=..] [--prune] [--dry-run]"
allowed-tools: Read, Write, Edit, Glob, Grep, Task, Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*"), Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/github-setup.sh" --dry-run*), Bash(git rev-parse *), Bash(git remote *), Bash(git status *), Bash(git branch *), Bash(git switch *), Bash(git add *), Bash(git commit *), Bash(git check-ignore *), Bash(git push -u origin sdlc/*), Bash(git pull --ff-only*), Bash(gh auth status*), Bash(gh repo view*), Bash(gh pr create *), Bash(gh pr merge *)
---

# /sdlc:init — adopt sdlc in this repository, once

Arguments: $ARGUMENTS

`sdlc.py` below means `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sdlc.py"`; it finds the repository from the current directory. Talk to the user in their language. `init` never overwrites: an existing file is skipped or merged (missing CLAUDE.md sections, `.gitignore` lines, permissions, empty config values), so running it again is safe.

## Health check: `--check`, "점검해줘", "doctor", "설정 확인해줘"
Run `sdlc.py init --check` (it changes nothing; exit 1 when a line says FAIL). Explain every FAIL and WARN line in one sentence with its fix, then stop.

| Line | Fix |
|---|---|
| git repository | `git init` and a first commit |
| .sdlc/config.json | missing → `/sdlc:init`; "version 1" → `/sdlc:init` migrates it; not valid JSON → fix the file by hand |
| test command (FAIL) | none set → `sdlc.py init --commands test="…"` and the matching CLAUDE.md lines |
| build / lint command (WARN) | unset: leave it empty when the project has none (the gate skips empty commands), else set it as above. "not found" on any command: install the tool or correct the command the same way |
| CLAUDE.md | "n/5 sections" → `/sdlc:init` adds them; "TODO placeholders" → fill them; "over 120 lines" → offer the trim (references/claude-md-guide.md "Trimming a long CLAUDE.md"; only on the user's yes); "mentions removed commands" → rewrite those lines |
| AGENTS.md | CLAUDE.md should import it (`@AGENTS.md`) → `/sdlc:init` |
| project skills | a file under `.claude/skills`, `commands` or `agents` names a removed 0.x command → rewrite that line for `/sdlc:go` or `/sdlc:run` (init leaves these files alone) |
| REVIEW.md, work/, .gitignore | missing or incomplete → `/sdlc:init`; a work doc with a bad slug, status or no Goal → open the named file and fix it |
| claude CLI, gh | the loop runs `claude` → put it on PATH; a GitHub remote needs `gh auth login --hostname <host>` |
| team mode | needs a GitHub remote → add one, or `sdlc.py init --solo` |
| loop, items | information: is the loop running, its last stop, items waiting for a person → `/sdlc:status` |
| 0.x leftovers | files 0.x generated → `sdlc.py init --prune`, on the user's yes |

## Adopting
1. **Git.** `git rev-parse --show-toplevel`. Not a repository: say that sdlc needs `git init` and a first commit (the engine, the merge gate and the hooks all read git) and stop. If the user asks you to do it, do it and go on. Adopt from an up-to-date checkout of the default branch (`git switch <default>`, `git pull --ff-only`; with linked worktrees, the one that has the default branch checked out), because a migration reads that checkout's 0.x files and step 5 switches back to the default branch.
2. **Run `sdlc.py init`** with the flags the user gave or implied: `--lang ko` when they write Korean and no `.sdlc/config.json` exists yet; `--solo` / `--team` only when they said so (otherwise it detects: no remote or one collaborator is solo, more is team); `--commands build=..,test=..,lint=..` when they named commands; `--dry-run` shows the rows and stops. Show the rows briefly: what was created, merged, skipped, and every NOTE line. An error (not a git repository, a config that is not JSON) → relay it and stop. Check `protect.default_branch` in `.sdlc/config.json`: it must be the branch finished work merges into (init takes it from origin/HEAD, or from the current branch without a remote); fix it when it guessed wrong.
3. **Fill what only you can.** Read `${CLAUDE_PLUGIN_ROOT}/skills/init/references/claude-md-guide.md` first.
   - CLAUDE.md **Conventions** and **Architecture**: replace the `TODO:` lines between the markers. Sweep the repository with the `sdlc-researcher` agent (layout, entry points, test layout, naming and error-handling conventions, generated code); 3–6 facts each, in the language of the rest of the file. Sections the user already had stay untouched. Keep the file under 120 lines; when it ends up over `claude_md.max_lines` anyway, offer the trim (the guide's "Trimming a long CLAUDE.md") and do it only on the user's yes.
   - **Commands:** run the detected `test` command once (ask first when it needs credentials, a database or docker) and report the result. A wrong command (script missing, wrong runner): fix it with `sdlc.py init --commands test="…"` and edit the CLAUDE.md lines to match. A right command that fails: the repository is red — say so, the merge gate will stop on it. No test command at all: say the gate verifies nothing until one is set (a missing build or lint command is fine, the gate skips empty ones).
   - **Generated paths:** when obvious (`dist/`, `build/`, generated sources, `@generated` headers) write them into REVIEW.md's TODO line and into `protect.generated_paths` in `.sdlc/config.json`; none exist: say so in that sentence; unclear: leave the TODO and tell the user.
4. **Migration** (a MIGRATED row: a 0.x layout was found). List the work docs made from the old intents (`ready`, `planned`, or `proposed` for drafts the user still has to accept) and the NOTE lines; the old intent, spec and plan history stays as it is. A NOTE about the old lessons file: offer to move the lines still true into CLAUDE.md "Things Claude gets wrong". A CLAUDE.md row that says it still mentions removed commands: rewrite those lines. The same NOTE naming a project skill, command or agent: offer to rewrite those lines. Triage drafts: ask which to start with `/sdlc:go`; the rest stay as history or go on the user's yes. Dropped policies: offer to name them in REVIEW.md so the reviewer applies them. A pause file: say the loop will not start until it is deleted, and delete it only on the user's yes. A LEFTOVER line lists files 0.x generated and 1.0 does not use: offer `sdlc.py init --prune` and run it only on the user's yes.
5. **Commit the adoption.** Stage per path, never `git add -A` (the user's other changes stay theirs): the paths of the CREATED / MERGED / MIGRATED / REMOVED rows, the files you edited in step 3, and after a migration `.sdlc/config.v1.json` (the old config). A path that git ignores or that matches nothing (a removed untracked file) is skipped. Commit as `sdlc: adopt`.
   - No remote: commit on the current branch.
   - GitHub remote: `git switch -c sdlc/init` first, push it, `gh pr create` titled `sdlc: adopt`. Solo: `gh pr merge --squash --delete-branch` at once, then `git switch <default>` and `git pull --ff-only`. Team: leave the PR to a person and say plainly that sdlc does nothing on the default branch until it is merged.
6. **GitHub ruleset** (GitHub remote only). Offer it: the default branch takes changes through pull requests only, force-push and deletion are blocked, auto-merge and delete-branch-on-merge are on (solo: no approval needed; team: one approval, code owners where the plan allows). `bash "${CLAUDE_PLUGIN_ROOT}/scripts/github-setup.sh" --dry-run` shows exactly what it would do; run it for real only on the user's yes. `gh` not logged in: give `gh auth login --hostname <host>` and stop there.
7. **Finish.** Run `sdlc.py init --check` and say in a few lines what is left (FAIL / WARN). Then the next step: `/sdlc:go "…"` for one change in this session, `/sdlc:run "goal"` for a whole backlog in the background, "어디까지 됐어?" for progress.

## Rules
- Never overwrite or delete a user's file beyond what `init` and `--prune` do. Never push to the default branch — the guard blocks it.
- The ruleset, `--prune` and anything outside the adoption files need the user's yes.
