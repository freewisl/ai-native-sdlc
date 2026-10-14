# Fix

The merge gate — run by the shell, not by a session's own report — failed on this branch. Interactive: `sdlc.py ship` exited 1 and printed the brief. Loop (`--item <slug> --fix`): the prompt after the flags is the brief. Either way it lists what failed, with the last lines of each command's output. On GitHub the brief can also come from the PR's checks, after the gate passed: it names each failed check with its workflow, its link and the end of its job's log, cut before the job's cleanup (step 4).

Work through it in this order:

1. **Merge conflict** with the default branch (the brief says `git merge <base>` conflicts). Run `git merge <base>` with the ref the brief names and resolve every conflict keeping both sides' intent: read what the other side changed (`git log -p <base> -- <file>`), never take one side wholesale, and make the result compile. Run the commands, then commit the merge.
2. **A command failed** (build, test, lint, or `checks/<name>`). Read the output, find the cause, fix the code, rerun that command and then the whole set the gate runs (`gate.commands`, by default build, test and lint). If it fails in the gate but passes on your branch, the cause came in with the default branch — the gate tests your branch merged with it — so `git merge <base>`, rerun, fix.
3. **No review tally, or Important findings open.** Run references/review.md: review, validate, fix, record. The gate reads the last tally line in `## Review`.
4. **A PR check failed on GitHub** (the brief says the checks ran there, after the shell gate had passed). Reproduce it locally with the command its workflow runs — read the job in `.github/workflows/` — fix the cause, rerun that command and then the gate's. A failure that is not this change's (the CI environment, a missing secret, a flaky external service) is not yours to fix: end with `SDLC_FIX_BLOCKED <why>`, as below.
5. **Commit** as a new commit: `sdlc(<slug>): fix <what>` (the engine looks for it).

Never:
- weaken, skip or delete a test to get green — fix the code;
- edit the Acceptance criteria or `test_changes`;
- loosen a lint rule, raise a timeout, disable a check or edit `.github/workflows/` to get green;
- try to get around what the brief lists "for a person" (a new dependency, a risk path, a guard file, an existing test changed, deleted or weakened, changed criteria or `test_changes`). Fix the rest; those go to a person, who sees the reasons.

If it cannot be fixed without breaking one of those, write what blocks it — the failing command, the cause, what would unblock it — in `## Departures`, commit that note, and print `SDLC_FIX_BLOCKED <one line>` instead of `SDLC_FIXED`. The item then goes to a person with your note.

Finish:
- Loop: print `SDLC_FIXED`. The engine reruns the gate — and after a PR-checks brief waits for the checks again; you do not.
- Interactive: run `sdlc.py ship <slug>` again, in the background as before. At most three rounds, then stop and show the user the last brief verbatim and what you tried.
