# The work doc

`work/<slug>.md` (dir: `paths.work`) is one file per change: the goal, what proves it, how it is built, what the review found. The engine reads the frontmatter and the `## ` headings, so those must be exact; the prose is for people. Keep `work/` for work docs only (`README.md` aside).

## Frontmatter

```
---
slug: entry-crud
title: 수입·지출 입력/수정/삭제
status: ready
depends_on: []
test_changes: none
difficulty: normal
created: 2026-10-12T09:30:00Z
source: user
---
```

- `slug`: the file name; kebab-case ascii (a-z 0-9 - . _), at most five words.
- `title`: in `language`; quote it when it contains `: ` or ` #`.
- `depends_on`: `[slug, slug]` — the item waits until those are done.
- `test_changes`: `none` or `allowed` — decided by the design and frozen with it: the gate sends a later change to a person.
- `difficulty`: `easy`, `normal` or `hard` (any case) — how hard the loop's sessions think. `easy`: mechanical — the Plan names every edit and there is no logic to get wrong (config, a rename, wording, wiring, copying a pattern that exists). `hard`: concurrency, security or auth, a data migration or a stored format, money or precision, an algorithm, a contract between modules, or a requirement that needed judgment. `normal`: everything else. `/sdlc:run` guesses it when it splits a goal; the design sets it from its Plan and revises the guess (references/design.md). Optional and not frozen. Without one, or with any other value, it is `normal` and the sessions run at their base effort.
- `created`: the current UTC time, ISO — take it from `date -u +%Y-%m-%dT%H:%M:%SZ`, never type one from memory. The queue works oldest first, so docs written together get ascending values, a second apart, in the order they should be worked. `source`: `user`, `goal:<id>`, `self-check` or `review:<slug>`.

One `key: value` per line and nothing after the value — a trailing `# comment` becomes part of the value and breaks the doc. The template is `templates/<language>/work.md` (`{{slug}}`, `{{title}}`, `{{created}}`); replace each comment you fill, and make sure Goal holds real text.

## Statuses

| status | meaning | set by |
|---|---|---|
| `proposed` | an idea a person has to accept (`sdlc.py accept <slug>` makes it `ready`) | review and self-check, for pre-existing problems |
| `ready` | queued; design is next | you when you create it (interactive); a person; the engine after an answer |
| `planned` | designed, criteria and `test_changes` frozen; build is next | the design |
| `built` | built and reviewed; the gate is next | the build / review |
| `done` | the gate passed | the engine (`ship`) — never you |
| `needs-input` | waits for an answer in `## Questions` | a loop session that cannot go on |
| `dropped` | will not be done; the first line of `## Departures` says why. The default branch carries the same (the engine records it; a person: `sdlc.py drop`) | a session, with evidence |

## Sections (in this order)

| Section | Holds | Written by |
|---|---|---|
| `## Goal` | what should be true afterwards, for whom, why — plain words | the author of the request |
| `## Acceptance criteria` | numbered, testable; frozen at `planned` | the author; design may sharpen |
| `## Plan` | numbered steps with how each is checked, files, risks (references/design.md) | design |
| `## Proof` | the table `\| R \| test or command \| kind \|`, then `Results:` lines | design, then build |
| `## Review` | findings, then the tally line (references/review.md) | build / review |
| `## Departures` | where the build left the Plan, and why; the reason when `dropped` | build |
| `## Learned` | what a later change should know — up to three lines | build |
| `## Questions` | only for `needs-input`: multiple choice, recommended first (references/questions.md) | a stuck session |

A doc written by `/sdlc:run` or by hand often has only Goal and Acceptance criteria. Whoever first writes a section adds it, with the heading exactly as above and in this order. Edit the doc with the Edit tool (an exact `old_string`, which fails loudly when it is not there), not with a regex script: a replacement that matches nothing is silent, and the doc ends up `planned` without a Plan. Check afterwards: `grep -n '^## ' work/<slug>.md` lists the section and there is text under it.

## Acceptance criteria

One observable behavior per line, numbered `R1`, `R2` … — numbers never change after `planned`.

```
R1. WHEN a signed-in user submits an entry with an amount and a category THE SYSTEM SHALL store it and show it first in the list.
R2. IF the amount is zero or negative THEN THE SYSTEM SHALL reject the entry and keep what the user typed.
R3. WHILE the list has more than 50 entries THE SYSTEM SHALL page it.
```

- The keywords (`WHEN`, `IF … THEN`, `WHILE`, `THE SYSTEM SHALL`) stay English whatever the `language`; the rest follows it.
- Say what a user or caller sees, not how it is built ("uses library X" is a Plan step, not a criterion).
- Include the edge or error case that matters (empty, invalid, unauthorized, concurrent). A change small enough for one PR rarely needs more than seven.
- Testable means a test or command can fail on it. "Fast", "clean" and "robust" are not criteria until they carry a number or an example.

## Proof

```
| R | test or command | kind |
|---|---|---|
| R1 | tests/test_entries.py::test_create_entry_shows_first | unit |
| R2 | tests/test_entries.py::test_rejects_non_positive_amount | unit |
| R3 | `curl -s localhost:3000/entries?page=2` returns the second page | e2e |
```

Every R has at least one row; a row names a concrete test or command; `kind` is `unit`, `e2e` or `manual`. After the build, `Results:` lines go under the table, one per command or test, with the literal summary: `- npm test → exit 0, "Tests: 42 passed"`.

## Questions, Departures, Learned

- **Questions** (only for `needs-input`; when to ask: references/questions.md): at most three, numbered; each is one line ending in `(recommended: <option>)`, then the options as bullets, the recommended one first. The engine appends `**Answer** (date): …` below.
- **Departures:** each time the build leaves the Plan — a file added or dropped, a step reordered, another approach — one line with the reason, in the same commit as the code. For a `dropped` doc the first line is the reason; replace the template comment, because the engine records that line as the reason of the drop.
- **Learned:** things a later change should know. The second time the same mistake shows up, the review turns it into one line in CLAUDE.md "Things Claude gets wrong" (references/review.md).

## Size

Scale the doc to the change. A one-line fix gets a one-sentence Goal, one or two criteria and a two-step Plan; a feature gets what it needs. Do not pad.
