<!-- sdlc:begin commands -->
## Commands
- Build: {{build_cmd}}
- Test: {{test_cmd}}
- Lint: {{lint_cmd}}
- Run: {{run_cmd}}
<!-- sdlc:end commands -->

<!-- sdlc:begin conventions -->
## Conventions
- {{conventions}}
<!-- sdlc:end conventions -->

<!-- sdlc:begin architecture -->
## Architecture
- {{architecture}}
<!-- sdlc:end architecture -->

<!-- sdlc:begin mistakes -->
## Things Claude gets wrong
- (add a line here the second time the same mistake appears; `/sdlc:lesson` does this)
<!-- sdlc:end mistakes -->

<!-- sdlc:begin verifying -->
## Verifying your work
- Build: {{build_cmd}} (must finish without errors)
- Test: {{test_cmd}} (all green; never skip or delete a failing test)
- Lint: {{lint_cmd}} (zero warnings)

Run all three before reporting any task complete, and paste the output.
If a test fails, fix the code, not the test.
Every change starts from a committed plan.md (`/sdlc:go` writes and commits it; `/sdlc:plan` for the step-by-step path).
<!-- sdlc:end verifying -->
