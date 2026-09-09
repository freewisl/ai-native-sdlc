# intent/ — where ideas enter the loop

Every change starts as one file here: `intent/<slug>.md`, written in the originator's own words
with Claude's help (`/sdlc:intent`). Author and timestamp come from the git commit; approval is
`status: approved` committed (or the merged PR) by the product owner.

- `TEMPLATE.md` — the organization's intent template: a technical team member writes it, a lead signs it off, and changes go through PR review. `/sdlc:intent` reads it.
- `triage/` — intents written by automation (monitoring, scans, on-call). The service owner
  triages them with `/sdlc:triage`: fix now, schedule, or dismiss with a reason.

Where intent lives: for a single product the simplest home is this `intent/` folder in the product repo — the artifact chain stays next to the code derived from it. A dedicated intent repository is only worth the overhead when intent spans many repositories; in a monorepo it is a directory. Setting the home up is a one-time task for the platform or engineering team, who also decide who can write to it (contributors come from across the organization) — expressed as repository permissions and the `intent/` line in CODEOWNERS.

Non-engineers do not need git: connect claude.ai or Cowork to the repository (GitHub connector)
and ask Claude to commit the file.

Next stage: an approved intent triggers `/sdlc:spec`, which writes `spec/<slug>.md`.
