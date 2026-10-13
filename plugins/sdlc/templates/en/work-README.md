<!-- sdlc:work -->
# work/

One file per change: `work/<slug>.md` — goal, acceptance criteria, plan, proof, review. The sdlc plugin reads and
writes these; you can too.

| status | meaning |
|---|---|
| `proposed` | an idea someone still has to accept: say so in /sdlc:status (it runs `sdlc.py accept <slug>`), or set it to `ready` and `sdlc.py enqueue` it |
| `ready` | on the queue; the loop designs it next |
| `planned` | designed (on branch `sdlc/<slug>`); the loop builds it next |
| `built` | built and reviewed; the merge gate runs next |
| `done` | merged |
| `needs-input` | waits for an answer — see `## Questions` |
| `dropped` | not doing it — see `## Departures` |

- Ask in plain words: "X 해줘" (one change now) or "X 만들어, 알아서 채우고 돌려" (split into items and run the loop).
- "어디까지 됐어?" shows progress and what needs you.
- `depends_on: [slug]` keeps an item waiting until that one is merged.
- `test_changes: allowed` lets the change edit existing tests (new tests are always allowed).
