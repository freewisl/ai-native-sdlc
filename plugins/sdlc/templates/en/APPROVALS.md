# Approval gates

The written list of human approvals the change process requires (playbook Stage 5 "Hooks as approval gates").
Each gate names what counts as approval, who gives it, what enforces it, and where the decision is logged.
Engineering leadership, change management and compliance own this list; the platform engineer expresses each row as a hook.

| Gate | What counts as approval | Approver | Enforced by | Logged in |
|---|---|---|---|---|
| Release authorization (production) | `RELEASE_APPROVAL=<ticket or sign-off id>` present in the session/pipeline environment | Release manager | `guard-bash.sh` deploy gate — `environments.production` (autonomy `gated`, `approval_env`) | `.sdlc/logs/gate.log`, `events.jsonl` (`hook.gate`) |
| Change-management sign-off (migrations, infra) | `CHANGE_TICKET=<approved ticket>` present | Change advisory / service owner | `guard-edit.sh` — `protect.ticketed_paths` (e.g. `migrations/`, `infra/`), `protect.ticket_env` | `events.jsonl` (`hook.guard`, rule `ticketed_path`) |
| Protected paths (generated, frozen packages) | Change the list itself, reviewed as code | Code owner of the path | `guard-edit.sh` — `.sdlc/frozen-paths.txt`, `protect.generated_paths` | `events.jsonl` (`hook.guard`) |
| Code approval (every PR) | Code-owner review on the PR | Code owners (CODEOWNERS) | Branch protection; agents cannot approve | PR history |
| Staging deploy | A person confirms the `ask` prompt | On-call / engineer | `guard-bash.sh` — `environments.staging` (autonomy `constrained`) | `gate.log` (`ask`) |

Team hooks live in `.claude/settings.json` (git); non-negotiable gates go to managed settings owned by the platform/IT admin.
A block always explains itself: the reason and the route to approval appear in Claude's output.
