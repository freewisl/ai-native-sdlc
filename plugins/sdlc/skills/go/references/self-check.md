# Self-check

Runs after a loop run merged work: `--self-check <from>..<to>` on `<default>` with a clean tree, `<from>..<to>` being the commits that just merged. A run that merged fewer than `loop.self_check_min_items` items (default 2) skips it: one item's own review already read that diff. You look for what the per-item reviews missed and write it down as new work docs. You write new `work/*.md` files and nothing else: no commit, no edit to an existing file (a work doc included), no new directory. The engine puts back every other change, checks each new doc and queues the good ones.

1. **Read what merged.** `git log --oneline <from>..<to>` and `git diff --stat <from>..<to>` — the reviewer reads the diff itself, in a fresh context. The work docs that merged — `git diff --name-only <from>..<to> -- work/` — say what each change meant (Goal, Acceptance criteria). Read REVIEW.md and CLAUDE.md.
2. **Review it** the way references/review.md steps 2 and 3 do, but over the range: Task → `sdlc:sdlc-reviewer` with `git diff <from>..<to>` (not `<base>...HEAD`), and tell it that pre-existing means present at `<from>` (`git show <from>:<file>`): the default branch now already holds the merged work. Then each Important finding validated in a fresh context. Always through the agent, also for a small range — a fresh context on a stronger model is the point of a self-check; your own read of the diff is the same eyes that missed it. Only confirmed findings count. Look past the single items: how the merged changes interact (a contract one of them changed and another still assumes), what none of them tested.
3. **Sort what is confirmed.**
   - Important, on lines this range added or changed → a `ready` item.
   - Important, in code that was already there at `<from>`, or a Nit worth a person's time → a `proposed` item.
   - Everything else → nothing: nits, taste, what the gate enforces, what a later commit in the range already fixed, refuted findings.
4. **Dedupe** against every existing doc: `ls work/`, then grep slugs, titles and Goals — `done` and `dropped` ones too. A finding an existing doc already covers is not new.
5. **Write the docs**, untracked, as `work/<new-slug>.md`: at most three `ready` and at most three `proposed`. Format as in references/work-doc.md — unique kebab-case slug, `source: self-check`, `depends_on: []`, `test_changes: none`, `created` from `date -u +%Y-%m-%dT%H:%M:%SZ`, `status: ready` or `proposed`.
   - **Goal:** where (file:line), the concrete failure (input or state → wrong result), why it matters.
   - **Acceptance criteria:** the failing scenario now behaves correctly, and a regression test covers it.
   - No Plan and no Proof — the design session writes them.
6. Print `SDLC_SELFCHECK items=<n>`, n being the number of docs written. Nothing worth reporting → write nothing and print `items=0`.
