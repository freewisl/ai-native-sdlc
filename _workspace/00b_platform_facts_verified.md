# 플랫폼 사실 — 공식 문서(code.claude.com/docs)로 검증한 확정본 (2026-09-06, CLI 2.1.263)
> 00_platform_facts.md 의 "NOT DOCUMENTED" 항목을 공식 문서 WebFetch 와 로컬 공식 플러그인(plugin-dev, ralph-loop, security-guidance)으로 해소. 충돌 시 이 문서가 우선.

## 훅 (docs/en/hooks)
- 이벤트: SessionStart, Setup, UserPromptSubmit, UserPromptExpansion, PreToolUse, PermissionRequest, PermissionDenied, PostToolUse, PostToolUseFailure, PostToolBatch, Notification, MessageDisplay, SubagentStart, SubagentStop, TaskCreated, TaskCompleted, Stop, StopFailure, TeammateIdle, InstructionsLoaded, ConfigChange, CwdChanged, DirectoryAdded, FileChanged, WorktreeCreate, WorktreeRemove, PreCompact, PostCompact, PreModelSwitch, PostModelSwitch, Elicitation, ElicitationResult, SessionEnd
- 공통 stdin: `session_id, prompt_id, transcript_path, cwd, permission_mode, effort, hook_event_name` (+ `agent_id/agent_type` 서브에이전트)
- PreToolUse: `tool_name, tool_input, tool_use_id` · PostToolUse: + `tool_response`(문서 표기 tool_output) · Stop: `stop_hook_active, last_assistant_message` · SessionStart: `source`/`session_start_type` (startup|resume|clear|compact|fork) · SessionEnd: `session_end_type`
- exit 0: JSON 이면 파싱; **SessionStart·UserPromptSubmit 의 평문 stdout 은 Claude 컨텍스트에 추가**, 다른 이벤트는 디버그 로그만. exit 2: 차단, stderr(또는 JSON reason)가 Claude 에 전달. 기타 exit: 비차단 오류 알림.
- PreToolUse JSON: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow|deny|ask","permissionDecisionReason":"…","updatedInput":{…},"additionalContext":"…"}}` · Stop JSON: `{"decision":"block","reason":"…"}` (+ `continue`, `suppress_output`, `systemMessage`)
- matcher: 정확 문자열 / `A|B` / `A, B` / 정규식(JS, 비앵커) · 훅 옵션: `timeout`(초, command 기본 600), `if`(권한 규칙 필터 예 `Bash(git *)`), `once`, `async`, `asyncRewake`, `shell`, `statusMessage`
- 환경변수: `CLAUDE_PROJECT_DIR`(worktree 에서도 고정), `CLAUDE_PLUGIN_ROOT`(업데이트 시 변경), `CLAUDE_PLUGIN_DATA`(영구 데이터 디렉터리), `CLAUDE_EFFORT`, (SessionStart) `CLAUDE_ENV_FILE`
- 플러그인 hooks.json: `{"description": "...", "hooks": {<event>: [{"matcher": "...", "hooks": [{"type":"command","command":"bash \"${CLAUDE_PLUGIN_ROOT}/…\"","timeout":10}]}]}}` — 기본 경로 `./hooks/hooks.json`, plugin.json `hooks` 로 변경 가능

## 플러그인/마켓플레이스 (plugin-dev manifest-reference, `claude plugin validate --strict` 통과 확인)
- plugin.json 필수 `name`(kebab-case), 권장 `version`(semver)·`description`·`author{name,email?,url?}`·`license`·`keywords`·`homepage`·`repository`; 경로 필드 `commands|agents|hooks|mcpServers` 는 `./` 로 시작하는 상대경로, 기본 디렉터리를 **보완**(대체 아님)
- 자동 발견: `commands/*.md`, `agents/*.md`, `skills/*/SKILL.md`, `hooks/hooks.json`, `.mcp.json`
- marketplace.json: `name`, `owner{name}`, `plugins[{name, source: "./rel" | {"source":"github","repo":"o/r"} | {"source":"url","url":…,"sha":…} | {"source":"git-subdir",…}, description, category?, tags?}]`
- 설치: `claude plugin marketplace add <path|github-url|owner/repo>` → `claude plugin install sdlc@ai-native-sdlc`; 로컬 시험 `claude --plugin-dir <plugin-root>`; `claude plugin validate <path> [--strict] [--json]`; `claude plugin eval` 은 `<plugin>/evals/**/case.yaml` 또는 `prompt.md + graders/*.md`
- 스킬 네임스페이스 `/sdlc:<skill>`; SKILL.md frontmatter: `name`, `description`(3인칭, 트리거 문구), 선택 `argument-hint`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `context: fork`, `model`; 본문에서 `$ARGUMENTS`, `${CLAUDE_PLUGIN_ROOT}` 사용 가능
- 에이전트 frontmatter: `name`, `description`, `tools`(쉼표 목록, `Bash(git *)` 식 제한 가능), `model`(inherit|sonnet|opus|haiku), `effort`, `color`

## 설정 (docs/en/settings, settings-reference, managed-settings, sandboxing)
- 우선순위: 관리형 > CLI `--settings` > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json`
- **관리형 파일 경로**: macOS `/Library/Application Support/ClaudeCode/managed-settings.json`, Linux/WSL `/etc/claude-code/managed-settings.json`, Windows `C:\Program Files\ClaudeCode\managed-settings.json` (구 `C:\ProgramData\…` 는 읽지 않음). 드롭인 `managed-settings.d/*.json`(알파벳 순 병합), `managed-mcp.json`. MDM: macOS `com.anthropic.claudecode` 도메인. 서버 관리형: claude.ai admin console(클라우드 세션에는 이것만 도달)
- 키 확정: `permissions.disableBypassPermissionsMode: "disable"`(문자열), `allowManagedPermissionRulesOnly: true`, `allowManagedHooksOnly: true`, `allowManagedMcpServersOnly: true`, `disableSideloadFlags: true`(--plugin-dir/--plugin-url/--agents/--mcp-config 거부, ≥2.1.193), `strictKnownMarketplaces: [ {source, repo} … ]`(배열), `requiredMinimumVersion: "x.y.z"`(fail-open), `availableModels`, `disableCommandPluginSources`, `permissions.blockReadsOutsideWorkingDirectories`
- sandbox: `enabled, failIfUnavailable, allowUnsandboxedCommands(false|…), autoAllowBashIfSandboxed, excludedCommands[], network.{allowedDomains[], allowUnixSockets[], allowLocalBinding, tlsTerminate}, credentials.{files[{path,mode:deny|mask}], envVars[{name,mode}]}, filesystem.{allowRead, denyRead, allowWrite, disabled}`; 지원 macOS(Seatbelt)·Linux/WSL2(bubblewrap), 네이티브 Windows 미지원; `/sandbox` 패널
- OTel: `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_LOGS_EXPORT_INTERVAL`, `OTEL_LOG_TOOL_DETAILS=1`(skill_name 포함); 메트릭 `claude_code.session.count|lines_of_code.count|pull_request.count|commit.count|cost.usage|token.usage|code_edit_tool.decision|active_time.total`(속성 `skill.name, agent.name, plugin.name`); 이벤트 `claude_code.user_prompt|tool_result(decision_source: hook…)|api_request|api_error|tool_decision(source: "hook")|permission_mode_changed`
- 헤드리스: `claude -p … --output-format json|stream-json --allowedTools … --disallowedTools … --max-turns N --max-budget-usd X --permission-mode acceptEdits|auto|plan|dontAsk|bypassPermissions --permission-prompts none --append-system-prompt … --plugin-dir … --settings … --no-session-persistence --model …`; 결과 JSON `result, is_error, total_cost_usd, session_id`
- GitHub Action `anthropics/claude-code-action@v1`: inputs `anthropic_api_key | claude_code_oauth_token | use_bedrock | use_vertex`, `prompt`, `claude_args`, `github_token`, `track_progress`, `allowed_bots`; 기본 트리거 `@claude`(issue_comment, pull_request_review_comment); permissions `contents: write, pull-requests: write, issues: write, id-token: write`; **PR 승인 불가**
- 이 머신: jq 1.7.1(/usr/bin), python3 3.9.6, node 26, gh 없음, JDK 17(jenv), `timeout` 명령 없음(macOS), 중첩 `-p` 실행은 `env -u CLAUDECODE` 로 가능(비용 발생 — 시험은 `--model sonnet --max-turns` 제한)

## 스킬·에이전트 추가 확정 (docs/en/skills.md, plugins-reference.md)
- SKILL.md frontmatter: `name, description, when_to_use, argument-hint, arguments[], disable-model-invocation, user-invocable, allowed-tools, disallowed-tools, model, effort, context: fork (+agent, background), hooks, paths, shell, metadata, license, compatibility`
- 본문 치환: `$ARGUMENTS, $ARGUMENTS[N], $N, $name, ${CLAUDE_PLUGIN_ROOT}, ${CLAUDE_PLUGIN_DATA}, ${CLAUDE_SKILL_DIR}, ${CLAUDE_PROJECT_DIR}, ${CLAUDE_SESSION_ID}, ${CLAUDE_EFFORT}`
- **동적 컨텍스트 주입**: 줄 시작의 !`cmd` 는 스킬 로드 전에 실행되어 출력이 삽입됨(2분 타임아웃, `disableSkillShellExecution` 로 비활성 가능). 권장 본문 500줄 이하, 압축 시 스킬당 5,000토큰 보존
- 플러그인 agents frontmatter: `name, description, model, effort, maxTurns, tools, disallowedTools, skills, memory, background, isolation("worktree")` — `permissionMode`·`hooks` 미지원. 네임스페이스 `sdlc:sdlc-verifier`
- plugin.json 추가 필드: `$schema, displayName, metadata, defaultEnabled, skills(추가), commands(대체), agents(대체), workflows, hooks, mcpServers, outputStyles, lspServers, userConfig, channels, dependencies[], experimental.{themes,monitors}`; 훅 타입 `command | http | mcp_tool | prompt | agent`
- `${CLAUDE_PLUGIN_DATA}` = `~/.claude/plugins/data/<id>/` 업데이트에도 유지(캐시·의존성용)

## `claude plugin eval` case 스키마 (CLI 2.1.263 바이너리에서 추출한 zod 스키마)
- 케이스 = `<plugin>/evals/**/<case-dir>/case.yaml` 또는 `prompt.md` + `graders/*.md`(frontmatter `type:`)
- case.yaml: `schema_version: "1.0"`(필수) · `name`(필수) · `description?` · `tags: []` · `plugins?: []` · `context: { scaffold_script?, history_file?, add_dirs: [] }` · `execution: { prompt?, max_turns: 10(≤200), timeout_seconds: 300(≤3600), model? }` · `graders: [...]` · `runs?`
- grader 타입: `regex`(target 기본 last_message, pattern, flags, mode: contains|not_contains|count:N, weight, arm) · `file_exists`(path 글롭, exists: true, weight, arm) · `llm`(criteria, focus: last_message|trace|files|{source: file, path}, weight, arm) · `tool_used`(tool, input_match, min, max, weight, arm: with-only|both) · `tool_order`(before, after) · `baseline`(baseline_file)
- `--ablation with-without` 기본: 플러그인 유/무 두 팔로 실행, `arm: with-only` grader(예 `tool_used: Skill`)는 점수 대신 "플러그인 발동 지표". `--scaffold` 를 줘야 scaffold_script 실행. 결과 `evals/results/<ts>/aggregate-result.json`
