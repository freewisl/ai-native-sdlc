# Filling CLAUDE.md at init time

`init.sh` writes marker-delimited stubs; the skill fills them from a repository inspection. The goal is
the blog's "Payments service" example: what a new joiner needs on day one, under a page.

## Command detection heuristics

| Evidence | build | test | lint | run |
|---|---|---|---|---|
| `Makefile` with targets | `make build` | `make test` | `make lint` | `make run` |
| `package.json` scripts (`npm`/`pnpm`/`yarn`/`bun` by lockfile) | `<pm> run build` | `<pm> test` | `<pm> run lint` | `<pm> start` or `<pm> run dev` |
| `pom.xml` (`mvnw` if present) | `./mvnw -q -DskipTests package` | `./mvnw -q test` | `./mvnw -q checkstyle:check` (only if plugin configured) | `./mvnw spring-boot:run` (Spring Boot) |
| `build.gradle(.kts)` | `./gradlew build -x test` | `./gradlew test` | `./gradlew check -x test` | `./gradlew bootRun` (Spring) |
| `go.mod` | `go build ./...` | `go test ./...` | `golangci-lint run` if config exists, else `go vet ./...` | `go run .` |
| `Cargo.toml` | `cargo build` | `cargo test` | `cargo clippy -- -D warnings` | `cargo run` |
| `pyproject.toml` / `requirements.txt` | `python -m build` only if packaging config exists, else empty | `pytest -q` | `ruff check .` if ruff config, else `flake8` if config | from README |
| `*.csproj` / `*.sln` | `dotnet build` | `dotnet test` | `dotnet format --verify-no-changes` | `dotnet run` |
| `Gemfile` | empty | `bundle exec rspec` or `bundle exec rake test` | `bundle exec rubocop` | `bundle exec rails s` |

Rules: only list a command whose tool is actually configured (a lint command with no config produces
noise). Prefer the wrapper the repo ships (`mvnw`, `gradlew`). Read CI workflow steps: what CI runs is
the truth about how the project is built and tested. If several test commands exist (unit/integration),
list them the way the blog does: `make test (unit), make itest (integration, needs docker)`.

## Healthy-output examples

Run each non-destructive command once and quote its last meaningful line. Typical healthy tails:

| Tool | Healthy tail |
|---|---|
| pytest | `42 passed in 3.1s` |
| jest/vitest | `Tests: 42 passed, 42 total` |
| maven | `BUILD SUCCESS` |
| gradle | `BUILD SUCCESSFUL in 12s` |
| go test | `ok  	module/pkg	0.42s` (one `ok` per package, no `FAIL`) |
| cargo test | `test result: ok. 42 passed; 0 failed` |
| eslint/ruff | no output, exit 0 |

Do not run commands that need credentials, a database or docker unless the user confirms. Do not run
`run`/`start` commands (they block). When a command cannot be run, write the placeholder
`example: (run once and paste the healthy tail here)` — never invent output.

## Section rules

**Commands** — one line per command, as in the blog: `- Test: make test (unit), make itest (integration, needs docker)`.
Add the healthy-output example in parentheses or on the same line.

**Conventions** — facts a linter or a reviewer enforces today: language/framework versions,
"Money is always BigDecimal, never double", test location rules, dependency ownership. Source them from
README, CONTRIBUTING, linter configs, `.editorconfig`, existing CLAUDE.md prose. 3–6 bullets.

**Architecture** — where things live and what talks to what: `api/ holds REST controllers, core/ holds
domain logic, adapters/ talks to external systems`. Name generated directories and frozen packages
("never edit generated classes") — and add those paths to `.sdlc/frozen-paths.txt` so the hook enforces it.

**Things Claude gets wrong** — start with `- (none yet)`. The working rule (blog): when Claude makes a
mistake twice, the correction goes here (`/sdlc:lesson`). Do not pre-fill guesses.

**Verifying your work** — the blog's verification block with real commands:
```
- Build: <build> (must finish without errors)
- Test: <test> (all green; never skip or delete a failing test)
- Lint: <lint> (zero warnings)
Run all three before reporting any task complete, and paste the output.
If a test fails, fix the code, not the test.
```

## Existing CLAUDE.md

If the file existed before init, `init.sh` appended only the sections that were missing (matched by
heading). Fill only those. If the pre-existing file is already over a page, suggest (do not perform)
cutting it down to what a new joiner needs on day one; stale lines cost context every session.

## Empty repository

Nothing to detect: write the commands as `TODO: add <build|test|lint> command`, conventions as the
language/framework the user names, architecture as the intended layout (one or two bullets), and say
in the summary that `/sdlc:doctor` will keep flagging the empty test command until it is set.
