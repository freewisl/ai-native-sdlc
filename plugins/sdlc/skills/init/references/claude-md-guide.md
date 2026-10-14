# Filling CLAUDE.md at init time

`sdlc.py init` writes five sections between `<!-- sdlc:begin … -->` and `<!-- sdlc:end … -->` markers. Commands and Verifying your work come from detection; Conventions and Architecture start as `TODO:` lines for you to replace. The goal is what a new joiner needs on day one, under a page (`claude_md.max_lines`, 120): Claude reads the whole file every session, so a stale line costs context every time.

## Commands the engine detects — verify them, do not trust them

| Evidence | build | test | lint | run |
|---|---|---|---|---|
| `package.json` scripts (pnpm / yarn / bun / npm by lockfile) | `<pm> run build` | `<pm> run test` | `<pm> run lint` | `<pm> run start` · `dev` · `serve` |
| `Makefile` targets | `make build` | `make test` · `check` | `make lint` | `make run` · `serve` · `dev` |
| `pom.xml` (`./mvnw` when present) | `mvn -q -DskipTests package` | `mvn -q test` | — | `mvn spring-boot:run` (plugin in the pom) |
| `build.gradle(.kts)` (`./gradlew` when present) | `gradle build -x test` | `gradle test` | — | `gradle bootRun` (Spring Boot) |
| `Cargo.toml` | `cargo build` | `cargo test` | `cargo clippy -- -D warnings` | `cargo run` |
| `go.mod` | `go build ./...` | `go test ./...` | `go vet ./...` | `go run .` (with `main.go`) |
| `Package.swift` | `swift build` | `swift test` | — | — |
| `pubspec.yaml` | — | `flutter test` · `dart test` | `flutter analyze` · `dart analyze` | — |
| `pyproject.toml` · `requirements.txt` | — | `pytest` | `ruff check .` or `flake8`, only when configured | — |
| `Gemfile` | — | `bundle exec rspec` or `rake test` | `bundle exec rubocop` when present | — |
| `composer.json` | `composer build` | `composer test` | `composer lint` | — (scripts that exist) |

Not detected: `*.csproj` / `*.sln` (`dotnet build`, `dotnet test`), a Python package build, and anything project-specific.

- List a command only when its tool is really configured; a lint command without a config is noise. Prefer the wrapper the repository ships (`mvnw`, `gradlew`).
- What CI runs is the truth about how the project is built and tested: when detection and CI (`.github/workflows`, `.gitlab-ci.yml`, `Jenkinsfile`) disagree, follow CI.
- Several test commands (unit, integration): `commands.test` holds the fast one the gate re-runs before every merge; CLAUDE.md lists both, e.g. `Test: make test (unit), make itest (integration, needs docker)`.
- To change a command: `sdlc.py init --commands test="…"` rewrites the config value; the lines in CLAUDE.md "Commands" and "Verifying your work" are edited by hand. The two must say the same thing.

## Running a command once

Run the `test` command (and `build` / `lint` when cheap) once and quote the last meaningful line. Healthy tails: pytest `42 passed in 3.1s` · jest/vitest `Tests: 42 passed, 42 total` · maven `BUILD SUCCESS` · gradle `BUILD SUCCESSFUL` · go `ok  module/pkg  0.42s` · cargo `test result: ok. 42 passed` · eslint/ruff no output, exit 0.

Do not run commands that need credentials, a database or docker unless the user says so, and never a `run` / `start` command (it blocks). When one cannot be run, write the command as it is and say it is unchecked — never invent output.

## Section rules

- **Commands:** one line per command, with the healthy-output example when you have it.
- **Conventions:** facts a linter or reviewer enforces today — language and framework versions, "money is always BigDecimal", where tests live, who owns a dependency. Sources: README, CONTRIBUTING, linter configs, `.editorconfig`, existing prose. 3–6 bullets, nothing aspirational.
- **Architecture:** where things live and what talks to what: `api/ holds REST controllers, core/ holds domain logic, adapters/ talks to external systems`. Name generated directories and frozen packages. Frozen paths also go into `.sdlc/frozen-paths.txt` (the edit hook enforces it); generated paths go into REVIEW.md and `protect.generated_paths`.
- **Things Claude gets wrong:** leave the stub line. When the same mistake shows up a second time (the reviewer flags repeats, a work doc's `## Learned` collects candidates), one line goes here. Never pre-fill guesses.
- **Verifying your work:** keep the template block; its commands must match `.sdlc/config.json`. A command that does not exist stays `TODO: set the <build|test|lint> command` rather than an invented one.

## A CLAUDE.md that already existed

Sections the user already had are left alone; init appends only the missing ones (matched by heading) and adds `@AGENTS.md` when an AGENTS.md exists — do not repeat its content. If the file is already over a page, init offers the trim below and does it only on the user's yes.

## Trimming a long CLAUDE.md

Every session reads the whole file — each loop session too — so a line that only some changes need is paid for by all of them. Offer the trim when `--check` says the file is over `claude_md.max_lines`, and when the adoption's step 3 leaves it over; change nothing without the user's yes. Show the plan first: what stays, what moves where, what goes.

- **Keep** the sdlc sections between the markers and what every change needs: Commands, the conventions every change follows, the architecture map, Things Claude gets wrong.
- **Move** what only some changes need — a module's internals, runbooks, long examples, API references, history — to `docs/claude/<topic>.md`, one file per topic, and leave one pointer line in its place: `- <Topic>: read docs/claude/<topic>.md before changing <paths>.` (for example `- Billing: read docs/claude/billing.md before changing src/billing/.`). Never `@docs/claude/<topic>.md`: an import loads the file into every session again, which undoes the move.
- **Delete** what is stale (check it against the code), what README.md or AGENTS.md already say (CLAUDE.md imports AGENTS.md), and what a linter enforces.
- Never drop a rule the user wrote without listing it in the plan.
- Afterwards the file is under the cap, every pointer names a file that exists and the markers are intact.
- Commit it like the adoption: `docs: trim CLAUDE.md`, staging CLAUDE.md and the new files per path. No remote: on the current branch. GitHub remote: branch `sdlc/trim-claude-md`, push, `gh pr create`; solo merges it at once, team leaves it to a person. Inside an adoption the trim goes into the adoption commit instead (stage its new files there too).

## An empty repository

Nothing to detect. The commands stay `TODO`, Conventions name the language and framework the user states, Architecture is the intended layout in a bullet or two. `init --check` keeps failing the test command until one is set; for a new app the first work item (the skeleton, see `/sdlc:run`) sets it up and records it. Until then the merge gate has nothing to run.
