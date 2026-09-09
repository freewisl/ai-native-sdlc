# Plugin evals (for `claude plugin eval`)

These cases test that the `sdlc` skills **trigger** from natural requests (the playbook's "Test that the skill triggers" step) — not the project's own configuration evals (those live in `<repo>/evals/` and run with `scripts/run-evals.sh`).

Run from the marketplace root:

    claude plugin eval plugins/sdlc --judge-model sonnet --allow-tools "Read,Glob,Grep"

Each case is a directory with `case.yaml` (schema_version 1.0). Graders marked `arm: with-only` report whether the plugin fired.

Note: `claude plugin eval` is in early access — on accounts without it the command prints `plugin eval is currently in early access`. The case files follow the CLI's case schema (schema_version, execution.prompt/max_turns/timeout_seconds, graders of type tool_used / regex / llm) but could not be executed here; treat them as ready-to-run once the feature is enabled.
