<!-- sdlc:evals -->
# evals/ — regression tests for the agent's configuration

CLAUDE.md, skills and hooks steer the agent, so they get the regression testing that code gets.
Run the suite whenever they change (CI does this on pull requests and nightly) and gate the change on the result.

Each case is a directory under `cases/`:
- `prompt.md`   — the task, written as a real engineer would ask it
- `fixture/`    — files the task works on (copied to a temp directory per run)
- `check.sh`    — deterministic acceptance check; exit 0 = pass. Receives the work directory as `$1`
- `case.json`   — optional: `{"allowed_tools": "...", "max_turns": 20, "tags": ["security"]}`

Run: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/run-evals.sh"` or `/sdlc:evals run`.
Add:  `/sdlc:evals add` — turns a recent task, a review finding or a production incident into a case.
Rule: every production incident becomes an eval and stays in the suite.


Format note: the article's `agent-evals.yml` iterates `evals/*.json` and calls a suite-level `./evals/check.sh <eval.json> result.json`. This plugin uses one directory per case (`prompt.md` + `fixture/` + `check.sh <workdir>`) so fixtures stay language-agnostic and the run happens inside a temporary git repository where the plugin's hooks actually fire; `run-evals.sh` collects every verdict into `results/<ts>/results.json`. The original workflow is kept verbatim in `.sdlc/examples/agent-evals.yml`. To run only on a schedule instead of on every configuration change, keep just the `schedule` trigger in `sdlc-evals.yml`.
