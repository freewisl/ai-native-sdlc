---
name: release
description: Runs a deployment under the per-environment autonomy tiers (Stage 5 Deploy) — dev runs freely, staging asks the user, production requires the named release authorization and a rehearsed rollback before the command runs, logs the decision, proposes rollback on failure and hands off to monitoring. Use when the user says "deploy to dev", "release to staging", "ship to production", "roll this out", "promote the build", "run the rollback", "배포", "스테이징 배포해", "운영 배포", "롤백 실행".
argument-hint: "<dev|staging|production> [command] | rollback"
disable-model-invocation: true
---

# /sdlc:release — the agent does everything up to the production gate and nothing past it

## What changes
- **Hooks as approval gates — Traditional:** approvals live in change boards and release checklists a person follows under pressure. **AI-native:** each gate the change process must keep (release authorization, change-management sign-off, protected paths) is a hook that runs before Claude acts and can allow, ask or block — enforced every time, for everyone, logged with a timestamp.
- **Traditional:** Pipelines run deterministic scripts, and anything that needs judgment waits for a human. For example, triaging the flaky test, writing the changelog, or working out why the build broke. Deployment and rollback are runbooks a human follows under pressure.
- **AI-native:** Claude runs non-interactively inside the pipeline for the judgment steps, in a sandbox with scoped credentials. Deployment tooling is exposed to the agent through MCP, so the workflow that wrote and tested the change can also ship it and roll it back, inside gates the organization defines per environment.


## When to use
When a reviewed, merged change is ready to deploy; to run the rehearsed rollback. Prerequisites: the PR
review loop and the hooks as approval gates (the `guard-bash` hook enforces the tiers regardless of this skill).

Prerequisites: Approval gates: none. CI/CD integration: Claude in the PR review loop and hooks as approval gates — the gates must exist before automation accelerates anything through them.
Infrastructure: A written list of the approvals the change process requires (.sdlc/APPROVALS.md); a CI platform with claude-code-action or any runner that can call claude -p; model access through the API or Bedrock/Foundry/Vertex; MCP servers for the deployment targets; a sandbox profile for agent jobs with no standing production credentials.

## Inputs
- `$ARGUMENTS`: environment (`dev|staging|production`) and optionally the deploy command; or `rollback`.
- `.sdlc/config.json` → `environments.<env>` (`autonomy`: free | constrained | gated; `patterns`;
  `approval_env`, default `RELEASE_APPROVAL`), `commands.rollback`, `commands.run`, `gate.log`.
- Deploy tooling: the command given, an MCP deploy tool (`.mcp.json`, e.g. deploy/status/rollback tools scoped
  per environment), or a pipeline trigger (`gh workflow run`). Ask when none is evident — never guess a
  deploy command.

## Steps
1. **Pre-flight.** Confirm the change is merged (or the user names the artifact/tag), the last
   `/sdlc:verify` was green (run it if unknown), and `commands.rollback` is set. Show the exact command that
   will run and which environment pattern it matches.
2. **dev (`free`).** Run the command. Paste the output and the resulting version/status.
3. **staging (`constrained`).** Show the command and ask the user to confirm in the session (the hook also
   returns an `ask` decision — in headless `-p` runs this is a denial, which is intended). Run on
   confirmation. Staging is also where rollback is rehearsed: offer to run the rollback command right after
   a successful staging deploy when it has not been exercised recently.
4. **production (`gated`).**
   1. Ask for the release manager's authorization: the change ticket / release approval id and who
      approved. Do not proceed on "just do it" without an id — the approval is what the gate defines.
   2. Verify rollback readiness: `commands.rollback` non-empty **and** rehearsed. If unknown, ask "when was
      the rollback last exercised in staging?"; if never, recommend rehearsing first and continue only if the
      release manager accepts the risk (record it).
   3. Run with the approval in the environment of that command only:
      `RELEASE_APPROVAL="<approval-id>" <deploy command>` (use the configured `approval_env` name). The
      hook lets the command through only when that variable is set; never export it for the whole session
      and never set a placeholder value.
   4. Paste the output; check the post-deploy status (MCP status tool, health endpoint, or the pipeline run).
5. **On failure** at any tier: stop, show the error, and propose the rollback command from
   `commands.rollback` (always allowed by the hook — the most rehearsed path). Run it only when the user
   says so, then confirm the status is back to the previous version.
6. **`rollback`**: run `commands.rollback` after confirming the target environment with the user; log it.
7. **Hand off.** Suggest `/sdlc:monitor` once now (post-deploy metrics against the baseline) and note that
   a 3σ breach with a deployment in the window is the case where the monitor may trigger this rollback
   runbook.

## Output
The tier and matched pattern, the command run with its output, the approval id used (production), the
rollback readiness statement, the post-deploy status, and the monitor suggestion.

## Governance
- Tiered autonomy: dev free, staging asks, production requires a named human authorization. The
  `guard-bash` hook enforces the same rule for every session — this skill is the polite path, the hook is the
  guarantee. Every allow/ask/deny is written to `.sdlc/logs/gate.log`.
- The agent prepares the release and the release manager authorizes it. Never set the approval variable
  to bypass the gate, never modify `environments.*.patterns` to make a command pass, never push to main.
- Deployment through MCP tools scoped per environment is preferred over shell commands with credentials.
- Execution in CI is sandboxed with short-lived tokens; no standing production credentials.

## Measurement
```bash
mkdir -p .sdlc/logs && printf '{"ts":"%s","event":"release.deploy","session_id":"","decision":"%s","reason":"%s","detail":{"env":"%s","approval":"%s","exit_code":%s,"rollback_proposed":%s}}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<allow|deny>" "<deployed|failed|blocked: no approval>" "<env>" "<approval-id or empty>" "<n>" "<true|false>" >> .sdlc/logs/events.jsonl
```
The hook writes the gate decision line (`ts \t decision \t env \t cmd`) to `gate.log`. Feeds Stage 5 in
`/sdlc:metrics`: time waiting on each approval gate, gate blocks, deploys per environment; DORA
measures are "source needed" (CI/deploy tooling).
