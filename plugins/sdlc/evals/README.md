# Plugin evals (`claude plugin eval`)

Trigger evals: does a natural request make the model call the right sdlc skill? Each case sends one Korean request to a throwaway `claude -p` child that has only this plugin loaded (own temporary home, config and working directory, so an installed copy of the plugin or your other plugins do not leak in) and grades the child's tool calls. They test the `description` of the four skills, not what a skill does after it fires, so runs are cut off after a few turns.

| Case | Request | Must call | Must not call |
|---|---|---|---|
| `trigger-go` | 로그인 화면에 비밀번호 보기 토글 추가해줘. 끝까지 해서 PR까지 올려 | `sdlc:go` | `sdlc:run` |
| `trigger-run` | 이번 주 목표는 결제 모듈 테스트 보강이야. 일감으로 쪼개서 알아서 돌려줘 | `sdlc:run` | `sdlc:go` |
| `trigger-status` | 루프 어디까지 됐어? 내가 답해야 할 거 있어? | `sdlc:status` | `sdlc:run` |
| `trigger-init` | 이 저장소에 sdlc 도입해줘 | `sdlc:init` | `sdlc:go` |
| `no-trigger-question` | why a `for` loop that does `result = result + [x * 2]` is slow (shares "루프" and "해줘" with the skills) | no `sdlc:*` skill, and the answer mentions the cause | |

All graders are deterministic (`tool_used` on the `Skill` tool, one `regex`), so there is no judge cost. The skill name is matched with its `sdlc:` prefix because Claude Code has built-in skills named plain `run` and `init`.

## Run

From the marketplace root (the directory that contains `plugins/`):

    claude plugin eval plugins/sdlc --scaffold --runs 1 --ablation none --model sonnet --no-publish --trust-plugin --max-cost-usd 1

About $0.25 for the five cases. Add `--case trigger-run` for one case or `--runs 3` to see flakiness.

- `--ablation none`: the default also runs every case without the plugin, which can never call an sdlc skill; that doubles the cost and turns the Skill graders into indicators.
- `--scaffold`: runs `trigger-go/fixture.sh` (git init plus a minimal `.sdlc/config.json` in the run's temporary workspace). Without it `trigger-go` runs in an empty directory and the CLI warns that the scaffold was skipped.
- `--trust-plugin`: skips the first-run trust prompt; pass it only for a checkout you trust.

## Reading the results

- A case's score is the share of its graders that passed, averaged over runs; it passes at `--threshold` (default 1.0) and the command exits 1 if any case is below it.
- A positive case misses when `Skill called 0x (expected 1..∞)` or when it also called the neighbouring skill. That is a finding about the skill's `description`. To see what the model did instead, run with `--json <file> --keep-temp`: each run's `tracePath` is the child's message stream.
- The first event of that trace, `system/init`, lists the child's `plugins`, `skills` and `agents`. Expect `sdlc@inline` pointing at this checkout and `sdlc:go|init|run|status` only; anything else means another install leaked in.
- `trigger-go` runs in an adopted repository because the skill is for those. Without `.sdlc/config.json` the SessionStart hook says "run /sdlc:init" and the model does not call `sdlc:go` (0 of 8 runs, with or without code in the directory); with the file it does (7 of 7).
- A run that ends with `Reached maximum number of turns` after its graders passed is normal: the agent is stopped early on purpose.
- A pass means the skill was picked, not that it works. The engine, scripts and hooks are tested in `tests/`.
