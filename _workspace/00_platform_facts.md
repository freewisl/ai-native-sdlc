# Claude Code Plugin Platform: Exact Facts (v2.1.263)

**Source**: Claude Code CLI v2.1.263, local help commands (`command claude <sub> --help`), local plugin examples, official marketplace metadata. **Date**: 2026-09-06. **Method**: All facts from `--help` output, `.claude-plugin/plugin.json` examples, settings.json structure, and marketplace metadata files.

---

## 1. Plugin Layout

### Directory Structure

A plugin is a directory with a required `.claude-plugin/plugin.json` manifest. The recommended structure:

```
my-plugin/
├── .claude-plugin/
│   ├── plugin.json          # Required. Plugin metadata & component pointers.
│   ├── marketplace.json     # Optional. Marketplace entry (for marketplace root).
│   └── icon.svg             # Optional. Plugin icon.
├── skills/
│   └── {skill-name}/
│       └── SKILL.md         # Skill definition (frontmatter + prompt).
├── agents/
│   └── {agent-name}.md      # Agent definition (frontmatter + prompt).
├── commands/
│   └── {command-name}.md    # Custom command (frontmatter + prompt/usage).
├── hooks/
│   ├── hooks.json           # Hook event handlers (auto-discovered).
│   └── {hook-script}.py     # Hook executable (referenced in hooks.json).
├── .mcp.json                # Optional. MCP server configuration.
├── scripts/                 # Optional. Utility scripts.
├── evals/                   # Optional. Evaluation cases (for `claude plugin eval`).
│   ├── case.yaml            # Individual case or...
│   ├── prompt.md            # Prompt + graders structure.
│   ├── graders/
│   │   └── {grader-name}.md # Evaluation grader (LLM or custom).
│   └── mocks/               # Optional. MCP server mocks for eval.
└── README.md                # Plugin documentation.
```

### `${CLAUDE_PLUGIN_ROOT}` Substitution

In hooks and other plugin code, `${CLAUDE_PLUGIN_ROOT}` expands to the plugin's root directory. Example in `hooks.json`:
```json
"command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/hook_handler.py\""
```

### `.claude-plugin/plugin.json` Schema

**Required fields:**
- `name` (string): Plugin identifier, used in `/plugin-name:skill-name` and marketplace references.
- `description` (string): One-line summary.
- `version` (string, semver): Version (e.g., "1.0.0", "0.8.0").
- `author` (object): `{name, email?}` (email optional).

**Optional fields:**
- `commands` (array of paths): `["./commands/cmd1.md", "./commands/cmd2.md"]`. Commands are discovered from `commands/` by convention; explicit list is NOT auto-discovered.
- `keywords` (array): Tag words for discoverability.
- `license` (string): License identifier (e.g., "MIT", "SEE LICENSE IN LICENSE").
- `homepage` (string): URL to plugin homepage.
- `repository` (string): URL to source repo.

**Example**:
```json
{
  "name": "claude-hud",
  "description": "Real-time statusline HUD for Claude Code",
  "version": "0.8.0",
  "author": {
    "name": "Jarrod Watts",
    "url": "https://github.com/jarrodwatts"
  },
  "commands": [
    "./commands/setup.md",
    "./commands/configure.md"
  ],
  "keywords": ["hud", "monitoring", "statusline"],
  "license": "MIT",
  "homepage": "https://github.com/jarrodwatts/claude-hud"
}
```

### Skills, Agents, Commands Discovery

- **Skills**: Auto-discovered from `skills/{name}/SKILL.md` (one skill per directory).
- **Agents**: Auto-discovered from `agents/{name}.md` (one agent per file).
- **Commands**: Explicitly listed in `plugin.json` `commands` array; NOT auto-discovered.
- **Hooks**: Auto-discovered from `hooks/hooks.json` (required at `hooks/hooks.json`, not configurable path).

### Skill Invocation Namespace

- Installed plugin skill: `/plugin-name:skill-name`
- User-scoped `~/.claude/skills/` skill: `/skill-name` (no namespace)
- Project-scoped `.claude/skills/` skill: `/skill-name` (no namespace)

### MCP Server Configuration (`.mcp.json`)

Optional file for plugins that provide MCP servers:
```json
{
  "mcpServers": {
    "server-name": {
      "type": "http|stdio|sse|websocket",
      "url": "https://example.com/mcp",  // For HTTP/SSE/WebSocket
      "command": "python3 server.py",    // For stdio
      "env": {"API_KEY": "..."},         // Optional env vars
      "args": ["--flag"]                 // Optional args for stdio
    }
  }
}
```
Unapproved `.mcp.json` servers are shown as "⏸ Pending approval" and not connected to until user approves.

---

## 2. Marketplace

### `.claude-plugin/marketplace.json` Schema

**Required (at marketplace root)**:
- `name` (string): Marketplace identifier.
- `owner` (object): `{name, email?}`.
- `plugins` (array): Plugin entries.

**Each plugin entry must have**:
- `name` (string): Plugin name.
- `source` (object): One of three forms below.
- `description` (string): Short description.

**Source Forms** (mutually exclusive):
1. **Local relative path**: `{"source": "./my-plugin"}` (plugin dir relative to marketplace root).
2. **GitHub repo**: `{"source": "github", "repo": "owner/repo"}` (fetches from GitHub releases).
3. **Git URL with subdir**: `{"source": "git-subdir", "url": "https://...", "path": "plugins/subdir", "ref": "main", "sha": "..."}`.

**Example marketplace.json**:
```json
{
  "name": "claude-hud",
  "owner": {"name": "Jarrod Watts", "email": "..."},
  "plugins": [
    {
      "name": "claude-hud",
      "source": "./",
      "description": "Real-time statusline HUD...",
      "category": "monitoring",
      "tags": ["hud", "statusline"]
    }
  ]
}
```

### Adding a Marketplace

**Command**:
```bash
claude plugin marketplace add <source>
```

**Sources**:
- **Local path**: `claude plugin marketplace add /path/to/marketplace-dir`
- **GitHub repo**: `claude plugin marketplace add https://github.com/owner/repo`

The marketplace is written to `~/.claude/plugins/marketplaces/{name}/` with `.claude-plugin/marketplace.json` inside.

### Installing from a Marketplace

```bash
claude plugin install plugin-name@marketplace-name
```

Example:
```bash
claude plugin install eli5@claude-community
```

### Testing Locally

```bash
claude --plugin-dir /path/to/plugin [prompt]
claude --plugin-dir /path/to/plugin.zip [prompt]  # Also accepts .zip
```

Multiple plugins:
```bash
claude --plugin-dir A --plugin-dir B --plugin-dir C.zip
```

### Settings: Marketplace and Plugin Management

**In `settings.json`**:

- `extraKnownMarketplaces` (object): Additional marketplaces beyond built-in (claude-plugins-official, claude-community).
  ```json
  "extraKnownMarketplaces": {
    "my-marketplace": {
      "source": {
        "source": "github",
        "repo": "owner/repo"
      }
    }
  }
  ```

- `strictKnownMarketplaces` (boolean): If `true`, only allow plugins from official + configured marketplaces (no sidefloading via `--plugin-dir`).

- `enabledPlugins` (object): Per-plugin enable/disable.
  ```json
  "enabledPlugins": {
    "plugin-name@marketplace-name": true,
    "another@official": false
  }
  ```

---

## 3. Skills

### `skills/{name}/SKILL.md` Frontmatter

**Frontmatter format** (YAML between `---` markers):

- `name` (string, **required**): Skill identifier (used in `/skill-name` or `/plugin:skill-name`).
- `description` (string, **required**): One-line summary shown in skill picker.
- `disabled-model-invocation` (boolean, optional): If `true`, prevent Claude from invoking this skill without user request (forces explicit `/skill`).
- `allowed-tools` (array of strings, optional): List of tools the skill is allowed to use. If omitted, all tools available. Example: `["Bash", "Read", "Edit"]`.
- `user-invocable` (boolean, optional): If `false`, skill is available only to subagents/agents (not directly via `/`).
- `argument-hint` (string, optional): Brief hint for skill arguments (shown in picker). Example: `"<topic>"`.
- `context` (object, optional):
  - `fork` (boolean): If `true`, skill runs in a forked subagent (inherits full context, cached parent session).
  - `model` (string): Model override for this skill (e.g., `"sonnet"`).

**Example SKILL.md**:
```markdown
---
name: eli5
description: Explain like I'm 5 years old with pictures and simple words.
argument-hint: "<topic>"
disabled-model-invocation: false
user-invocable: true
context:
  model: sonnet
---

Explain this topic in the simplest possible way with a visual HTML artifact.

Topic: $ARGUMENTS
```

### Argument Substitution

- `$ARGUMENTS` (string): Full argument string passed to skill. Example: user types `/eli5 quantum computing`, `$ARGUMENTS` = `"quantum computing"`.
- `$ARGUMENTS[0]` (string): First argument (NOT DOCUMENTED; verify if supported).

### Supporting Files

Skills can reference files in their directory:
```markdown
---
name: my-skill
---

See the example config:
{{include ./example.json}}
```
(Exact syntax NOT DOCUMENTED; verify locally.)

### Size Limits

NOT DOCUMENTED in help; check official docs at https://docs.anthropic.com/en/docs/claude-code/...

### Subagent Execution

Skills can request a subagent via the Agent tool within their implementation (if they have code). **In SKILL.md alone**, NOT documented how to directly dispatch to an agent from skill definition frontmatter.

---

## 4. Agents (Subagents)

### `agents/{name}.md` Frontmatter

- `name` (string, **required**): Agent identifier.
- `description` (string, **required**): One-line summary.
- `tools` (array of strings, optional): Allowed tools (e.g., `["Read", "Glob", "Bash"]`). If omitted, all tools available.
- `model` (string, optional): Model for this agent (e.g., `"sonnet"`, `"opus"`).
- `effort` (string, optional): Default effort level (`"low"`, `"medium"`, `"high"`, `"xhigh"`, `"max"`).
- `color` (string, optional): Color name for terminal display (e.g., `"cyan"`).
- `permissionMode` (string, optional): NOT DOCUMENTED in available help.

**Example agents/explore.md** (from claude-security plugin):
```markdown
---
name: explore
description: Read-only code explorer that maps a codebase.
model: sonnet
effort: xhigh
color: cyan
tools: Read, Glob, Grep, Bash
---

# Explore Agent

You are a read-only file search specialist...
```

### Restricting Tools

List in `tools` array in agent frontmatter:
```yaml
tools: Read, Bash(git *), Edit(src/**/*.ts)
```

Syntax: `ToolName(pattern)` restricts to matching paths/args.

### Skill Requesting Specific Agent

NOT DOCUMENTED. Skill cannot directly declare "use agent X"; it must use the Agent tool within its code context (not in SKILL.md frontmatter).

---

## 5. Hooks

### Hook Events

From `command claude` help and `hooks.json` examples, the following hook events are confirmed:

- `PreToolUse`: Before a tool is executed.
- `PostToolUse`: After a tool completes (success or failure).
- `PostToolUseFailure`: After a tool fails (NOT CONFIRMED in help; check docs).
- `UserPromptSubmit`: When user submits a prompt.
- `Stop`: When a session stops.
- `SubagentStop`: When a subagent stops (NOT CONFIRMED in help).
- `SessionStart`: When a session starts.
- `SessionEnd`: When a session ends (NOT DOCUMENTED in help).
- `PreCompact`: Before context compaction.
- `Notification`: NOT DOCUMENTED.
- `PermissionRequest`: When a permission is requested (NOT CONFIRMED).

### Hook Execution

**Command in hooks.json**:
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/handler.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

**stdin JSON Format** (hook receives as JSON on stdin):

NOT FULLY DOCUMENTED. From help and examples, likely contains:
- `session_id` (UUID): Session identifier.
- `transcript_path` (string): Path to transcript.
- `cwd` (string): Current working directory.
- `hook_event_name` (string): Event type (e.g., `"PreToolUse"`).
- `tool_name` (string): For tool-related hooks (e.g., `"Bash"`, `"Edit"`).
- `tool_input` (object): Input to the tool.
- `tool_response` (object?): Response from tool (for Post* hooks).
- `source` (string?): Source of the hook trigger.

**Exit Code Semantics**:
- `0`: Hook succeeded, continue.
- `2`: Hook declined the action (deny permission, block tool).
- Other: Hook error; behavior NOT DOCUMENTED.

**stdout JSON Response Schema**:
```json
{
  "decision": "allow|deny|ask",  // For permission hooks
  "reason": "string",
  "continue": true,  // Whether to proceed
  "hookSpecificOutput": {
    "permissionDecision": "allow|deny|ask",
    "permissionDecisionReason": "string"
  },
  "additionalContext": "string",
  "updatedInput": { ... }  // Modified tool input
}
```

**SessionStart stdout**:
Injected into context as system message (NOT CONFIRMED in help; check docs).

### Hook Timeout

Set `timeout` in hook definition (seconds). Example: `"timeout": 10`.

### Environment Variables Available to Hooks

- `$CLAUDE_PROJECT_DIR`: Project directory (where `.claude/` is found).
- `$CLAUDE_PLUGIN_ROOT`: Plugin root directory.
- `${CLAUDE_CONFIG_DIR}`: Claude config directory (usually `~/.claude`).
- Standard env vars (PATH, HOME, etc.).

### hooks.json Location and Auto-Discovery

- **Location**: `hooks/hooks.json` (required at this path; NOT configurable).
- **Auto-discovery**: Yes, auto-discovered inside plugin `hooks/hooks.json`.
- **In settings.json**: Hooks can also be defined directly in `settings.json` (project/user/local).

### Matcher Syntax

In `settings.json` hooks, use `matcher` to target specific tool invocations:
```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Bash",  // Match Bash tool
      "hooks": [...]
    },
    {
      "matcher": "Edit|Write|MultiEdit",  // Multiple tools
      "hooks": [...]
    }
  ]
}
```

---

## 6. Settings

### Settings File Precedence (applied bottom-up, later overrides earlier)

1. **Managed settings** (admin-console, highest precedence): Enforced, cannot be overridden.
2. **User settings** (`~/.claude/settings.json`): User's home directory.
3. **Project settings** (`.claude/settings.json`): Project root.
4. **Local settings** (`.claude/settings.local.json`): Project root, .gitignored.
5. **CLI flags** (highest runtime precedence): `--settings`, `--allowed-tools`, etc.

### Managed Settings File Locations (per OS)

NOT DOCUMENTED in help. Likely `~/.claude/settings.json` with a `managed` key or separate file (check official docs).

### Settings Keys and Value Types

**`permissions` (object)**:
- `defaultMode` (string): Default permission mode (`"auto"`, `"acceptEdits"`, `"manual"`, `"dontAsk"`, `"bypassPermissions"`, `"plan"`).
- `allow` (array): Whitelist of allowed tool operations (permissive).
  ```json
  "allow": [
    "Bash(git *)",
    "Read(.env*)",
    "Edit(src/**)"
  ]
  ```
- `deny` (array): Blacklist of denied operations (restrictive, checked first).
  ```json
  "deny": [
    "Read(**/.env*)",
    "Bash(rm -rf /*)"
  ]
  ```

**Permission Rule Syntax**:
```
ToolName(pattern[:argument-pattern])
```
Examples:
- `Bash(git *)`: All git bash commands.
- `Read(.env*)`: Read files matching `.env*`.
- `Edit(src/**)`: Edit files under src/.
- `WebFetch`: No pattern; matches all WebFetch.
- `mcp__server_name__tool`: MCP tool (e.g., `mcp__github__list_repos`).

**`permissions.disabledMcpJsonServers` (array)**: Disable specific .mcp.json servers in project.
```json
"disabledMcpjsonServers": ["server1", "server2"]
```

**`hooks` (object)**: Hook definitions (same schema as `hooks/hooks.json`).
```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Bash",
      "hooks": [
        { "type": "command", "command": "bash /path/to/hook.sh" }
      ]
    }
  ]
}
```

**`env` (object)**: Environment variables.
```json
"env": {
  "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
  "DEBUG": "false"
}
```

**`model` (string)**: Default model alias or ID.
```json
"model": "claude-fable-5-1"
```

**`allowManagedPermissionRulesOnly` (boolean)**: If true, only managed permissions apply (user/project settings ignored).

**`allowManagedHooksOnly` (boolean)**: If true, only managed hooks apply.

**`allowManagedMcpServersOnly` (boolean)**: If true, only managed MCP servers apply.

**`disableSideloadFlags` (boolean)**: If true, disallow `--plugin-dir`, `--plugin-url`, `--mcp-config`, etc.

**`strictKnownMarketplaces` (boolean)**: If true, only allow plugins from configured marketplaces.

**`disableBypassPermissionsMode` (boolean)**: If true, do not allow `--dangerously-skip-permissions`.

**`requiredMinimumVersion` (string)**: Require minimum Claude Code version (semver).

**`sandbox` (object)**: Sandbox configuration.
- `enabled` (boolean): Enable sandbox.
- `failIfUnavailable` (boolean): Fail if sandbox cannot be created.
- `allowUnsandboxedCommands` (array): Commands to run outside sandbox (e.g., `["nvm use"]`).
- `network` (object):
  - `allowedDomains` (array): Domains allowed for network access.
- `credentials` (object):
  - `files` (object): `{"mode": "deny|pass-through"}`. Deny or pass SSH keys, certs, etc.
  - `envVars` (object): `{"mode": "deny|pass-through"}`. Deny or pass credential env vars.
- `excludedCommands` (array): Commands to forbid in sandbox.

**`enabledPlugins` (object)**: Enable/disable plugins by name@marketplace.
```json
"enabledPlugins": {
  "claude-hud@claude-hud": true,
  "eli5@claude-community": false
}
```

**`extraKnownMarketplaces` (object)**: Additional marketplaces.
```json
"extraKnownMarketplaces": {
  "my-marketplace": {
    "source": {
      "source": "github",
      "repo": "owner/repo"
    }
  }
}
```

**`tui` (string)**: Terminal UI mode (e.g., `"fullscreen"`, `"minimal"`).

**`language` (string)**: UI language (e.g., `"en"`, `"Korean"`).

**`theme` (string)**: Terminal theme (e.g., `"dark"`, `"dark-daltonized"`, `"light"`).

**`statusLine` (object)**: Status line configuration.
```json
"statusLine": {
  "type": "command",
  "command": "bash -c '...'",
  "refreshInterval": 5
}
```

**Example settings.json**:
```json
{
  "permissions": {
    "defaultMode": "auto",
    "allow": [
      "Bash(git *)",
      "Bash(mvn *)"
    ],
    "deny": [
      "Read(.env*)"
    ]
  },
  "model": "claude-fable-5-1",
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "bash hooks/pre-tool.sh" }
        ]
      }
    ]
  },
  "enabledPlugins": {
    "eli5@claude-community": true
  }
}
```

---

## 7. Headless / CI Integration

### CLI Flags for Non-Interactive Use

**Mode**:
- `-p`, `--print`: Print response and exit (non-interactive).
- `--output-format` (choices: `text`, `json`, `stream-json`): Output format.
  - `text`: Human-readable (default).
  - `json`: Single JSON response object at end.
  - `stream-json`: Real-time streaming JSON messages.
- `--input-format` (choices: `text`, `stream-json`): Input format (only with `--print`).

**Tool Control**:
- `--allowedTools`, `--allowed-tools <tools>`: Comma or space-separated list. Example: `"Bash(git *) Edit"`.
- `--disallowedTools`, `--disallowed-tools <tools>`: Deny specific tools.
- `--tools <tools>`: Specify available built-in tools (e.g., `"Bash,Edit,Read"`). Use `""` to disable all, `"default"` for all.

**Permission & Sandbox**:
- `--permission-mode <mode>` (choices: `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`): Default permission handling.
- `--permission-prompts <target>` (choices: `host`, `none`): Who answers permission prompts. `none` denies automatically.
- `--dangerously-skip-permissions`: Bypass all permission checks (NOT recommended).

**Context & Limits**:
- `--max-turns <n>`: Maximum number of turns.
- `--max-budget-usd <amount>`: Hard cost ceiling; aborts if exceeded.
- `--json-schema <schema>`: JSON Schema for structured output validation.
- `--append-system-prompt <prompt>`: Append to default system prompt.

**Configuration**:
- `--settings <file-or-json>`: Path to settings.json or JSON string.
- `--mcp-config <configs>`: MCP server config (JSON files or strings, space-separated).
- `--plugin-dir <path>`: Load plugin from directory or .zip (repeatable).
- `--plugin-url <url>`: Fetch plugin .zip from URL (repeatable).
- `--add-dir <directories>`: Allow tool access to additional directories.

**Other Flags**:
- `--model <model>`: Override model (e.g., `"fable"`, `"claude-fable-5-1"`).
- `--session-id <uuid>`: Use specific session ID.
- `--include-hook-events`: Include all hook lifecycle events in output (stream-json only).
- `--include-partial-messages`: Include partial message chunks (stream-json only).
- `--no-session-persistence`: Do not save session to disk.

### Installation in CI

**From npm**:
```bash
npm install -g @anthropic-ai/claude-code
```

**From direct installer** (if available):
```bash
curl -sSL https://... | bash
```

Verify: `claude --version`

### API Key for CI

- **Environment variable**: `ANTHROPIC_API_KEY=<key>`
- **Alternative (OAuth)**: `claude auth login` (interactive; not suitable for CI).
- **CLI flag**: None for API key directly; use `--settings` with JSON:
  ```bash
  claude --settings '{"auth": {"apiKey": "..."}}' -p "..."
  ```

### GitHub Action

**Action**: `anthropics/claude-code-action` (current major version NOT DOCUMENTED; check GitHub Marketplace).

**Inputs**:
- `anthropic_api_key` or `claude_code_oauth_token`: Authentication (at least one required).
- `prompt` (string): Claude prompt.
- `claude_args` (string, optional): Additional CLI arguments (e.g., `--allowed-tools Bash,Read`).

**Triggers**:
- On `@claude` comments in PRs: NOT DOCUMENTED; likely via action configuration.
- On pull_request events: Configure with GitHub Actions workflow.

**PR Approval**: NOT DOCUMENTED whether action can approve PRs; likely requires manual approval or separate bot token.

**Example workflow.yml**:
```yaml
name: Claude Code Review
on: [pull_request]
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt: "Review this PR for bugs."
          claude_args: "--allowed-tools Bash(git*),Read"
```

---

## 8. OpenTelemetry / Telemetry

### Environment Variables

- `CLAUDE_CODE_ENABLE_TELEMETRY` (boolean, 0|1): Enable telemetry collection (default: 1 if not set).
- `OTEL_METRICS_EXPORTER` (string): Metrics exporter (e.g., `"otlp"` for OTLP).
- `OTEL_LOGS_EXPORTER` (string): Logs exporter.
- `OTEL_EXPORTER_OTLP_ENDPOINT` (string): OTLP collector endpoint (e.g., `"http://localhost:4318"`).
- `OTEL_EXPORTER_OTLP_HEADERS` (string): Headers for OTLP (e.g., `"key=value,key2=value2"`).

### Metrics & Events Emitted

NOT DOCUMENTED in help. Likely:
- Session count/duration.
- Tool invocations (success/failure).
- Permission decisions.
- Model usage.

### Managed Settings via Environment

Env vars can be set in `settings.json` `env` object:
```json
"env": {
  "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
  "OTEL_METRICS_EXPORTER": "otlp"
}
```

---

## 9. Plan Mode

### Activation

- **CLI flag**: `--permission-mode plan`
- **Interactive**: `Shift+Tab` (in interactive session).

### Available Tools in Plan Mode

NOT FULLY DOCUMENTED. Likely limited to Read, Bash (read-only), and planning-related tools.

### Flow

1. **EnterPlanMode**: Session enters planning state.
2. Agent proposes plan (no tools executed).
3. User can review and approve.
4. **ExitPlanMode**: Execute approved plan.

---

## 10. Related Products

### Claude Code Review (Managed)

Deep code review for PRs using multiple specialized agents (architecture, performance, security, testing). Configured and managed in dashboard, integrated via GitHub.

### Claude Security

Continuous vulnerability scanning for your codebase, run inside Claude Code at configurable effort tiers. Verifies findings before reporting; generates targeted patches you apply on demand. Plugin: `claude-security@claude-plugins-official`.

### Claude Tag / Claude in Slack

Claude integration for Slack. Use `/install-slack-app` within Claude Code to set up. Provides inline Claude responses in Slack threads.

### Compliance API

Anthropic's API for compliance/audit workflows (token counting, prompt caching, tool-use governance). Separate from Claude Code; used by enterprises for custom compliance integrations.

### Managed MCP

Anthropic-hosted MCP server infrastructure for enterprise deployments. Centralizes MCP configuration and authentication across teams.

### Enterprise Deployment (Bedrock / Vertex / Foundry)

Claude models available on AWS Bedrock, Google Vertex AI, and Anthropic Foundry. Claude Code can be configured to use these endpoints via:
- `--settings` with custom endpoint config.
- Environment variables (Bedrock: `AWS_*`; Vertex: `GOOGLE_*`).

### Cowork

Multi-developer real-time collaboration platform (not yet documented as Claude Code feature; likely future integration).

---

## 11. Local CLI Facts

### Key Commands

```bash
claude [prompt]                # Interactive session
claude -p "prompt"             # Print mode (non-interactive)
claude --help                  # Show help
claude --version               # Show version (2.1.263)
claude -c                      # Continue last session
claude -r [session-id]         # Resume session
claude --bg                    # Run in background
claude --resume                # Pick session to resume

claude plugin init <name>      # Create new plugin
claude plugin list             # List installed plugins
claude plugin install <name>   # Install from marketplace
claude plugin eval [target]    # Run plugin evals
claude plugin validate <path>  # Validate plugin/marketplace

claude mcp add <name> <url>    # Add MCP server
claude mcp list                # List MCP servers

claude auth login              # Authenticate
claude auth status             # Check auth status

claude agents                  # List background sessions
claude attach <id>             # Attach to background session
claude logs <id>               # View background logs
```

### `claude plugin init` Scaffold

**Command**:
```bash
claude plugin init [options] <name>
```

**Options**:
- `--with <components>`: Also scaffold (space-separated, repeatable): `skills`, `agents`, `hooks`, `mcp`, `lsp`, `output-style`, `channel`.
- `--author <name>`: Author name (default: git config).
- `--author-email <email>`: Author email.
- `--description <text>`: Description.
- `-f`, `--force`: Overwrite existing.

**Scaffold Output** (default):
```
~/.claude/skills/<name>/
├── .claude-plugin/
│   └── plugin.json
├── skills/
│   └── my-skill/
│       └── SKILL.md  (if --with skills)
├── agents/           (if --with agents)
├── hooks/            (if --with hooks)
└── README.md
```

**NOT DOCUMENTED**: Exact scaffold file contents; inspect after running with flags.

---

## Documentation Links

Official Claude Code & API docs: **https://docs.anthropic.com/en/docs/claude-code/**

- Plugin building guide: `.../plugins/`
- Skills and agents: `.../skills/` and `.../agents/`
- Hooks documentation: `.../hooks/`
- Settings and permissions: `.../settings/`
- MCP integration: `.../mcp/`
- Marketplace: `.../marketplace/`
- CI/CD integration: `.../ci-cd/`

---

## Unresolved / NOT DOCUMENTED

1. **Exact stdin/stdout JSON schema for hooks**: Full field list not documented; inferred from examples.
2. **SessionStart hook injection into context**: Behavior unclear; likely injected as system message.
3. **`$ARGUMENTS[0]` syntax**: Not found in help; only `$ARGUMENTS` confirmed.
4. **Skill size limits**: NOT DOCUMENTED.
5. **PostToolUseFailure hook**: NOT CONFIRMED in help; mentioned in passing.
6. **Managed settings file location & schema**: NOT DOCUMENTED; likely `.claude/settings.json` with managed key.
7. **GitHub Action major version & PR approval**: NOT DOCUMENTED.
8. **Exact metrics/events emitted by telemetry**: NOT DOCUMENTED.
9. **Plan mode available tools**: NOT FULLY DOCUMENTED.
10. **Hook matcher regex/glob syntax**: NOT DOCUMENTED; only simple tool name examples shown.
11. **`allowed-tools` in SKILL.md frontmatter**: NOT DOCUMENTED; seems not to exist; use agent `tools` instead.
12. **PermissionRequest hook event**: NOT CONFIRMED in help.
13. **Skill supporting file inclusion syntax** (e.g., `{{include ...}}`): NOT DOCUMENTED.
14. **Claude Desktop MCP migration into Claude Code**: `add-from-claude-desktop` command exists but details NOT DOCUMENTED.

---

**This document reflects CLI v2.1.263 as of 2026-09-06. Check official docs and `--help` for the latest.**
