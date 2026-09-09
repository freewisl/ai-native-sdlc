---
name: spec
description: Turns an approved intent into a requirements-and-design spec (Stage 2 Design) — loads the organization's policy skills as constraints, runs the playbook's spec prompt, writes spec/<slug>.md with a mandatory Flagged concerns table and the skill versions in force, and commits it for product-owner review. Use when the user says "write the spec for <slug>", "produce the requirements and design", "generate spec.md from the intent", "design pass on this intent", "approve the spec", "스펙 작성", "요구사항 설계서 만들어", "intent로 spec 뽑아", "스펙 승인".
argument-hint: "<slug> [--force] | approve <slug> | reject <slug> \"<reason>\""
---

# /sdlc:spec — requirements and design in one session

## What changes
- **Traditional:** Requirements and design are separate phases run by separate teams. Analysts formalize the idea into requirements and designers then parse those back into a design. The separation exists for accountability, but it is slow and lossy.
- **AI-native:** Both phases happen in a single prompted session. Claude takes intent.md and produces a requirements and design spec, constrained by the organization's skills, with areas of concern flagged.


## When to use
When an intent has `status: approved` (the merge/approval of `intent/<slug>.md` is the trigger), or when
the product owner explicitly wants a spec drafted from a draft intent (`--force`, recorded). Also for the
product owner's `approve`/`reject` decision on a spec.

Prerequisites: Write an intent.md file, with brand, security, compliance, and UX policies written as skills (dependency figure: Capture intent → Requirements & design, Skills → Requirements & design).
Infrastructure: A product owner with Claude access. No engineering skill is required.

## Inputs
- `$ARGUMENTS`: `<slug>` (required), `--force` to proceed on an unapproved intent; `approve|reject <slug>`.
- `<intent>/<slug>.md` — the approved intent (read `paths.intent` from `.sdlc/config.json`).
- `config.policies` — names of organization policy skills (`.claude/skills/<name>/`) to apply as constraints.
- Template `${CLAUDE_PLUGIN_ROOT}/templates/<lang>/spec.md` (fallback `en`), or `<spec>/TEMPLATE.md`.
- Conventions: `${CLAUDE_PLUGIN_ROOT}/skills/intent/references/artifact-conventions.md`.

## Steps
1. **Gate on approval.** Read the intent frontmatter. If `status` is not `approved` and `--force` was
   not given, stop: "intent `<slug>` is `<status>`; the product owner approves with `/sdlc:intent approve
   <slug>`". With `--force`, continue and record "Drafted from an unapproved intent at <user>'s request" in
   the spec Summary.
2. **Load the policies as constraints.** For each name in `config.policies`, invoke that skill with the
   Skill tool (`Skill(skill: "<name>")`) and keep its rules in mind as hard constraints. If a policy has a
   `scripts/check-*.sh`, run it and keep the output for the Policies applied section. Record the version in
   force: `git log -1 --format=%h -- .claude/skills/<name>` → `<name>@<hash>` (use `uncommitted` when empty).
   If `policies` is empty, note it and suggest `/sdlc:policy` — the spec still gets written.
3. **Run the Stage-2 prompt** on the intent (the playbook's wording, kept verbatim as the working instruction):
   > Read the attached intent.md and produce a requirements and design spec for integrating it into our
   > existing codebase. Apply the skills available to you so the plan conforms to our brand guidelines,
   > security policies and UX standards. Document the spec fully as spec.md, ready to hand to the
   > engineering team. Describe clearly any areas of concern, especially where you cannot satisfy
   > contradicting policies.
   Read the codebase as needed (delegate broad exploration to the `sdlc-researcher` agent to keep the
   session focused). Requirements are numbered and testable (R1, R2, …); design names interfaces, data
   flow and the modules that change; Acceptance says how the spec is proven.
4. **Flagged concerns — never empty.** For each policy and each open question in the intent, decide:
   conflict, risk, unknown, or nothing. Fill the table with concern / policy or source / owner /
   resolution (blank = unresolved). Write `none — checked: <policy list>` in the first row only after every
   policy was actually checked. Carry every open question from the intent into "Open questions carried from
   intent": answered, or carried forward with an owner.
5. **Write** `<spec>/<slug>.md` from the template: frontmatter `status: draft`, `links.intent`,
   `policies_applied: [<name>@<hash>, …]`, `{{intent_commit}}` = `git log -1 --format=%h -- <intent path>`.
   Never overwrite an existing spec (conventions §5). Korean prose when `language` is `ko`, headings unchanged.
6. **Present the product-owner review points**: (a) does the spec solve the stated problem, (b) are the
   intent's open questions answered or carried forward, (c) work through the flagged concerns first — each
   is resolved with its policy owner before engineering sees the spec, (d) anything the organization
   classes as higher risk needs a technical lead's opinion.
7. **Commit** `spec(<slug>): <title>` (when `auto_commit`), alongside the intent — the file pair records
   what was asked for and what was decided.
8. **Offer the automation.** If `.github/workflows/sdlc-spec-on-intent.yml` is absent, offer to copy it from
   `${CLAUDE_PLUGIN_ROOT}/templates/github/` so that an intent merged with `status: approved` fires this
   pass non-interactively and opens the spec as a pull request (needs `ANTHROPIC_API_KEY`).

### `approve <slug>` / `reject <slug> "<reason>"`
Only on explicit instruction from the product owner: set `status`, `approved_by`, `approved` (and `reason`),
commit `spec(<slug>): approve|reject`. Accepting the spec is what starts `/sdlc:plan <slug>`.

## Output
The spec path and content, the Flagged concerns table highlighted, the policy versions applied, the
commit hash, and the next steps: PO resolves concerns → `/sdlc:spec approve <slug>` → `/sdlc:plan <slug>`.

## Solo mode
When `roles.solo` is true the user is product owner and tech lead: present the Flagged concerns, ask for one decision per row, and on "approve" set `status: approved` and offer to continue into `/sdlc:plan <slug>` immediately. (`/sdlc:go` asks only for rows that are real policy conflicts or contradicting requirements; everything else it approves on the strength of the original request.)

## Governance
- The product owner reviews and signs off; the skill never approves. Higher-risk specs go to a tech lead.
- Policies are applied as constraints while writing, not discovered weeks later; the spec, the prompt
  above and the skill versions (`policies_applied`) are all in version control.
- Skills are advisory: a policy that must always hold needs a hook or a review pass behind it — say so
  when a Flagged concern depends on one.
- No implementation, no plan: file-level ordering belongs to `/sdlc:plan`.

## Measurement
Append (conventions §8): `spec.write` with `detail: {"slug": "...", "policies_applied": [...], "concerns": n}`;
`spec.approve`/`spec.reject` with `detail: {"slug": "...", "approved_by": "..."}`.
Feeds Stage 2 in `/sdlc:metrics`: leading — intent commit → spec commit interval (two git timestamps);
lagging — spec commits dated after the first plan commit (rework after build starts).
