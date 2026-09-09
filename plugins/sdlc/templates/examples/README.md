# Playbook examples — verbatim

The code blocks of Anthropic's "The AI-native SDLC playbook" (claude.com/blog/the-ai-native-sdlc-playbook, 2026-08-21),
kept as the article printed them. `/sdlc:init` copies this directory to `.sdlc/examples/` so every project has the
originals next to the working templates; skills point here when they generalize a block.

| file | article block | generalized counterpart in this plugin |
|---|---|---|
| `intent.md` | Stage 1 — intent.md "claims status self-service" (J. Ortiz) | `templates/<lang>/intent.md` (frontmatter added, Out of scope added) |
| `stage2-prompt.txt` | Stage 2 — the requirements & design prompt | `skills/spec/SKILL.md` (same wording) |
| `plan.md` | Stage 3 — plan.md (Files that change / Order of work / Risks / Proof) | `templates/<lang>/plan.md` (+ Departures from plan) |
| `CLAUDE.md` | Stage 3 — CLAUDE.md "Payments service" | `templates/<lang>/CLAUDE.sections.md` (same four sections, values from the repo) |
| `secure-api-review.SKILL.md` | Stage 3 — .claude/skills/secure-api-review/SKILL.md | `templates/policies/secure-api-review/` (identical text) |
| `verifier.md` | Stage 3 — .claude/agents/verifier.md | `agents/sdlc-verifier.md` (same job; adds Grep/Glob and a fixed report shape) |
| `CLAUDE.verifying.md` | Stage 4 — CLAUDE.md "Verifying your work" block | `templates/<lang>/CLAUDE.sections.md` verifying section |
| `agent-evals.yml` | Stage 4 — .github/workflows/agent-evals.yml | `templates/github/sdlc-evals.yml` (loop replaced by `run-evals.sh`; see its header) |
| `REVIEW.md` | Stage 5 — REVIEW.md | `templates/<lang>/REVIEW.md` (two passes gain one item each; Feedback/Approval sections added) |
| `settings.hooks.json` | Stage 5 — .claude/settings.json hook registration | plugin `hooks/hooks.json` (the plugin registers hooks; a project may still add its own this way) |
| `production-gate.sh` | Stage 5 — .claude/hooks/production-gate.sh | `scripts/hooks/guard-bash.sh` (same rule: deploy + production → RELEASE_APPROVAL or exit 2; patterns configurable) |
| `managed-settings.json` | Stage 5 — Managed settings for a regulated enterprise | `templates/config/managed-settings.json` (identical) |
| `triage-step.yml` | Stage 5 — "Triage failed build" pipeline step | `templates/github/sdlc-ci-triage.yml` (same `claude -p` text) |
| `bands.yaml` | Stage 6 — bands.yaml | `templates/config/bands.json` (JSON form; same tiers/actions/routes) |
