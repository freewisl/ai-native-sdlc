# Review

Run in the main session: interactive after the builder returns, loop at the end of the build. The product is the `## Review` section and a tally the gate trusts, so what it counts has to be real.

1. **Scope.** The diff is `git diff <base>...HEAD`. Skip what REVIEW.md lists under "Do not report" (generated files, whatever the gate already enforces).
2. **Review pass.** Task → `sdlc:sdlc-reviewer` (review is its default mode; fresh context, read-only). Name the diff with the real ref in the prompt (`git diff origin/main...HEAD`, not `<base>`) and the work doc (`work/<slug>.md`), and say that the doc's `status` is still `planned` on purpose — you set `built` after the review, so that is no finding. It runs REVIEW.md's passes — Bugs, Security, Compliance — and returns a findings table (pass, severity, file:line, finding, fix), a separate `Pre-existing:` table, and a tally line of its own that says `validated=no` while Important findings are unvalidated. The grades:
   - **Important**: would break behavior, leak data or breach a policy, **and** it names a concrete failure — these inputs or this state → this wrong result.
   - **Nit**: anything else (style, naming, taste). At most five reported; the rest is a count.
   - **Pre-existing**: the problem was already there and this change neither introduced nor worsened it. A finding is new only if it sits on a line this change added or changed, or this change made it reachable.

   Compliance, concretely: every R is covered, partly covered or missing (missing is Important); every changed file is in the Plan or in Departures; the tests the Proof names exist and ran; no test was weakened, skipped or removed; CLAUDE.md is still true (a renamed command or a moved directory → fix that line).
3. **Validate every Important finding.** One Task per finding — several in a single message, so they run in parallel — each to `sdlc:sdlc-reviewer` in validate mode, in a fresh context. Give it that one finding (where, what is claimed, how serious) and the diff. It tries to refute it and answers `VERDICT: CONFIRMED` or `VERDICT: REFUTED`, then `Evidence:` and `Reproduce:`. It cannot run code: when it names a cheap reproduce command or test, run it yourself — a failure you saw beats one that was argued. Confirmed findings count. Refuted ones do not; note each as one line ("refuted: … because …").
4. **Act on the confirmed findings.** Fix each: a small one yourself; in an interactive run a larger one goes back to `sdlc:sdlc-builder` together with the finding. Rerun build, test and lint, commit, then review the new diff once more. Two rounds at most — if confirmed Important findings remain after the second, record them as open; the gate then sends the change to a fix session. Nits are fixed only when trivial, and never trigger another round.
5. **Pre-existing problems** the reviewer listed, the most serious three at most: write each as `work/<new-slug>.md` — `status: proposed`, `source: review:<slug>`, `depends_on: []`; Goal says where (file:line), the failure and why it matters; one criterion. Dedupe first against `work/` (`ls`, grep titles and Goals). They are committed on this branch with the review commit; do not fix them here.
6. **Repeated mistakes → CLAUDE.md.** For each confirmed finding look for the same class of mistake in CLAUDE.md "Things Claude gets wrong" and in earlier docs' `## Learned` (`grep -ril <key words> work/`). The second time, add **one line** to that CLAUDE.md section in this change:
   - a rule, not a story: imperative, at most 160 characters, naming the path or command it is about (check that it exists);
   - sharpen an existing line instead of adding a near-duplicate; replace the "(add a line here …)" stub;
   - never a rule that weakens verification ("skip flaky test X");
   - a rule that can be checked mechanically (a grep, a lint rule) says so in `## Learned` — it belongs in an executable `checks/<name>` that the gate runs;
   - CLAUDE.md already over `claude_md.max_lines` (120)? Say so in the report; do not trim it inside this change.
7. **Record** in `## Review`, replacing the template comment (or adding the section when the doc has none): the findings, one line each, confirmed first — `[Important][Bugs] src/a.py:12 — crashes on an empty list — fixed in <sha>`; the refuted lines; the slugs of the proposed docs; and, as the last line, the tally:

   `SDLC_REVIEW_TALLY important=<n> nits=<n> validated=yes`

   with real numbers for `<n>`, on a line of its own. `important` = confirmed Important findings still open (0 passes the gate). `nits` = nits found. `validated=yes` = no Important finding was left unvalidated (each one counted was CONFIRMED by a validate run, the refuted ones are dropped, or there were none). This line is yours — the reviewer's own tally, written before validation, is not recorded. The gate reads the last tally line in the section, so after a re-review write a new one below the old. Then set `status: built` and commit `sdlc(<slug>): review`.

## When the reviewer agent cannot run

A usage limit or an error — review in this session instead: REVIEW.md's three passes over the diff, then try to refute each Important finding yourself against the code. The first line of `## Review` says "Reviewed in the main session — sdlc-reviewer unavailable: <reason>", and the tally says `validated=no`. Do not claim a fresh-context validation that did not happen.
