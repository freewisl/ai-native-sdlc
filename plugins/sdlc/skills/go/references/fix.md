# Fix

The merge gate — run by the shell, not by a session's own report — failed on this branch. Interactive: `sdlc.py ship` exited 1 and printed the brief. Loop (`--item <slug> --fix`): the prompt after the flags is the brief. Either way it lists what failed, with the last lines of each command's output. On GitHub the brief can also come from the PR's checks, after the gate passed: it names each failed check with its workflow, its link and the end of its job's log, cut before the job's cleanup (step 4).

Start with `git rev-parse HEAD` and keep it: the commit before the fix. Then work through the brief in this order:

1. **Merge conflict** with the default branch (the brief says `git merge <base>` conflicts). Run `git merge <base>` with the ref the brief names and resolve every conflict keeping both sides' intent: read what the other side changed (`git log -p <base> -- <file>`), never take one side wholesale, and make the result compile. Run the commands, then commit the merge.
2. **A command failed** (build, test, lint, or `checks/<name>`). Read the output, find the cause, fix the code, rerun that command and then the whole set the gate runs (`gate.commands`, by default build, test and lint, and every executable in `checks/`). If it fails in the gate but passes on your branch, the cause came in with the default branch — the gate tests your branch merged with it — so `git merge <base>`, rerun, fix. A command that already fails or flakes on `<base>` (run it a few times; read `git log <base>` for the cause) blocks this gate as much as yours: fix it here when the cause is clear, with a `## Departures` line saying so, or — when it is not this change's to fix — write why under `## Departures`, commit, and end with `SDLC_FIX_BLOCKED <why>`.
3. **No review tally, Important findings open, or the review did not run in a fresh context** (the build session never called the reviewer agent). Run references/review.md: review through the `sdlc:sdlc-reviewer` agent, validate, fix, record. The gate reads the last tally line in `## Review`.
4. **A PR check failed on GitHub** (the brief says the checks ran there, after the shell gate had passed). Reproduce it locally with the command its workflow runs — read the job in `.github/workflows/` — fix the cause, rerun that command and then the gate's. A failure that is not this change's (the CI environment, a missing secret, a flaky external service) is not yours to fix: end with `SDLC_FIX_BLOCKED <why>`, as below.
5. **Commit** as a new commit, not an amend: `sdlc(<slug>): fix <what>`. A fix that resolves another open work doc also sets that doc to `dropped`, its first `## Departures` line `resolved by <slug> <sha>`; commit that too.
6. **Review what you changed.** When the fix touched non-test code beyond the failing lines or the conflict markers, nobody has reviewed that code (unless step 3 just did): run references/review.md steps 2–4 on `git diff <the commit from the start>..HEAD` (with a merge in it, tell the reviewer that what came in with `<base>` is not part of the fix), append the new tally line to `## Review` and commit it.

Never:
- weaken, skip or delete a test to get green — fix the code;
- edit the Acceptance criteria or `test_changes`;
- loosen a lint rule, raise a timeout, disable a check or edit `.github/workflows/` to get green;
- try to get around what the brief lists "for a person" (a new dependency, a risk path, a guard file, an existing test changed, deleted or weakened, changed criteria or `test_changes`). Fix the rest; those go to a person, who sees the reasons.

If it cannot be fixed without breaking one of those, write what blocks it — the failing command, the cause, what would unblock it — in `## Departures`, commit that note, and print `SDLC_FIX_BLOCKED <one line>` instead of `SDLC_FIXED`. The item then goes to a person with your note. The engine reacts to that word anywhere in your final message, so never write it when you are not blocked. Interactive (`/sdlc:go`): nothing reads the marker — stop and tell the user what blocks it.

Finish:
- Loop: print `SDLC_FIXED`. The engine reruns the gate — and after a PR-checks brief waits for the checks again; you do not.
- Interactive: run `sdlc.py ship <slug>` again, in the background as before. At most three rounds, then stop and show the user the last brief verbatim and what you tried.
