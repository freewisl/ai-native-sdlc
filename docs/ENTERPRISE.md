# 규제 기업을 위한 배포 가이드 — 권한 · 샌드박스 · 관리형 설정 3계층

플레이북(Stage 5 "Managed settings for a regulated enterprise", 횡단 "Permissions & sandbox")의 통제는 세 겹입니다.
`sdlc` 플러그인은 1·2계층의 템플릿과 3계층의 예시 파일을 제공하고, 배포 자체는 플랫폼 팀이 이 문서대로 수행합니다.

| 계층 | 무엇 | 누가 바꿀 수 있나 | 플러그인이 주는 것 |
|---|---|---|---|
| 1. 도구 권한 (`permissions`) | Read/Edit/Bash 등 도구별 allow · deny · ask 규칙 | 개발자·프로젝트(`.claude/settings.json`) | `templates/config/settings.project.json` (init 이 감지한 빌드·테스트 명령을 allow 에, 시크릿 읽기·force push 를 deny 에) |
| 2. OS 샌드박스 (`sandbox`) | 파일시스템·네트워크 격리, 자격증명 차단 | 프로젝트 또는 관리형 | `templates/config/settings.sandbox.json` (`init.sh --sandbox`) |
| 3. 관리형 설정 (managed settings) | 위 두 가지를 조직 정책으로 고정, 우회 불가 | 플랫폼/IT 관리자만 | `templates/config/managed-settings.json` (블로그 예시 원문) + 이 문서 |
| + 훅 | 액션마다 allow / ask / block 을 결정하는 스크립트 | 플러그인·프로젝트·관리형 | 플러그인 `hooks/hooks.json` 7종 — [플러그인 README 훅 표](../plugins/sdlc/README.md#훅-7종) |

## 1. 관리형 설정 파일 — 블로그 예시와 각 줄이 사는 것

`templates/config/managed-settings.json` 은 블로그의 JSON 을 그대로 담고 있습니다(도메인·마켓플레이스 저장소만 자리표시자).

```json
{
  "permissions": {
     "deny": [ "Read(.env*)", "Read(./secrets/**)", "WebFetch", "Bash(curl *)", "Bash(wget *)" ],
     "allow": [ "Bash(git *)", "Bash(make build)", "Bash(make test)", "Bash(make lint)" ],
     "disableBypassPermissionsMode": "disable"
  },
  "allowManagedPermissionRulesOnly": true,
  "sandbox": {
     "enabled": true, "failIfUnavailable": true, "allowUnsandboxedCommands": false,
     "network": { "allowedDomains": ["git.internal.example.com", "registry.npmjs.org"] },
     "credentials": {
        "files": [ { "path": "~/.ssh", "mode": "deny" }, { "path": "~/.aws/credentials", "mode": "deny" } ],
        "envVars": [ { "name": "GITHUB_TOKEN", "mode": "deny" } ]
     }
  },
  "allowManagedHooksOnly": true,
  "disableSideloadFlags": true,
  "allowManagedMcpServersOnly": true,
  "strictKnownMarketplaces": [ { "source": "github", "repo": "example-corp/approved-plugins" } ],
  "requiredMinimumVersion": "2.1.193"
}
```

블로그가 "각 줄이 통제 관점에서 사는 것"으로 설명한 내용(Stage 5 관리형 설정 절)입니다.

| 키 | 통제 효과 |
|---|---|
| `permissions.deny` | 시크릿을 에이전트 컨텍스트 밖에 두고, 도구를 통한 임의 네트워크 유출(WebFetch·curl·wget)을 막는다 |
| `permissions.allow` | 안전한 내부 루프(git·build·test·lint)를 사전 승인해 deny 목록이 "프롬프트 피로"로 변하지 않게 한다 |
| `permissions.disableBypassPermissionsMode: "disable"` + `allowManagedPermissionRulesOnly: true` | 어떤 엔지니어·프로젝트 파일·CLI 플래그도 규칙을 넓힐 수 없다 |
| `sandbox` | 권한이 못 막는 틈을 막는다 — WebFetch 를 deny 해도 셸 명령은 네트워크에 닿을 수 있지만, OS 수준 도메인 허용목록은 유출 자체를 차단한다 |
| `sandbox.failIfUnavailable` + `allowUnsandboxedCommands: false` | 샌드박스를 게이트로 만든다 — 초기화 실패 시 기동 거부, 샌드박스 안에서 실패한 명령을 밖에서 재시도할 수 없다 |
| `sandbox.credentials` | deny 규칙이 남긴 틈을 막는다 — 파일 도구는 `permissions.deny` 가 다스리지만 샌드박스 셸은 기본적으로 `~/.ssh`·`~/.aws/credentials` 를 읽을 수 있으므로, 그 읽기를 거부하고 지정한 시크릿 환경변수를 모든 샌드박스 명령의 환경에서 제거한다 |
| `allowManagedHooksOnly` | 이 플레이의 승인 게이트만이 실행되는 훅이다 — 로컬에서 훅을 추가·대체할 수 없다. **주의**: 이 값이 켜지면 플러그인 훅도 관리형 소스에서 배포된 것만 실행되므로, `sdlc` 플러그인은 `strictKnownMarketplaces` 의 승인 마켓플레이스로 배포해야 한다 |
| `disableSideloadFlags` + `strictKnownMarketplaces` | 엔지니어 머신의 모든 스킬·에이전트·훅·MCP 서버는 조직 승인 마켓플레이스에서 왔고, 홈 디렉터리에서 오지 않았다(`--plugin-dir`·`--plugin-url`·`--agents`·`--mcp-config` 거부, 2.1.193+) |
| `allowManagedMcpServersOnly` | 에이전트의 도구 표면을 플랫폼 팀이 소유하는 허용목록으로 만든다(`allowedMcpServers` 만 유효, `deniedMcpServers` 는 전 소스 병합) |
| `requiredMinimumVersion` | 승인 하한 미만 버전에서는 기동을 거부한다 — 조직이 실제로 평가한 빌드가 통제를 집행한다. **잘못된 값은 무시된다(fail-open)** 이므로 semver 형식을 검증할 것 |

블로그의 당부: "복사할 권고가 아니라 맞춰 쓸 출발점" — 모든 deny 는 능력과 맞바꾸며, 균형은 저장소의 데이터 분류에 따라 다릅니다.

## 2. 어디에 어떻게 배포하나

| 전달 방식 | 위치 | 특징 |
|---|---|---|
| 파일 | macOS `/Library/Application Support/ClaudeCode/managed-settings.json` · Linux/WSL `/etc/claude-code/managed-settings.json` · Windows `C:\Program Files\ClaudeCode\managed-settings.json` (구 `C:\ProgramData\ClaudeCode\…` 는 읽지 않음) | 기동 시 읽고 파일 변경 시 재로드. MDM 이 없는 머신·Linux 호스트·자체 이미지에 적합 |
| 드롭인 | 같은 디렉터리의 `managed-settings.d/*.json` (알파벳 순 병합, `10-telemetry.json`, `20-security.json` 처럼 번호 접두) + `managed-mcp.json` | 여러 팀이 정책의 일부를 각자 소유할 때 |
| MDM | macOS 구성 프로파일 `com.anthropic.claudecode` 도메인 (같은 최상위 키, 중첩은 dictionary, 배열은 plist array) | 기기 관리가 이미 MDM 인 조직 |
| 서버 관리형 | claude.ai admin console(Claude Enterprise) | **클라우드 세션에는 이것만 도달** — 로컬 파일·MDM 은 클라우드 세션에 적용되지 않는다 |

우선순위: 관리형 > `--settings` > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json`. 관리형 소스가 여럿이면 잠금 키(`allowManagedHooksOnly`, `permissions.disableBypassPermissionsMode`)는 가장 엄격한 값이, 제한 허용목록(`availableModels`, `allowedMcpServers`, `strictKnownMarketplaces`)은 최상위 소스의 목록이 통째로 적용됩니다. `requiredMinimumVersion` 은 세션 시작 시에만 읽히므로 이미 열린 세션은 종료시키지 않습니다.

이 플러그인을 조직에 배포하는 절차:
1. 이 마켓플레이스(`freewisl/ai-native-sdlc`)를 그대로 쓰거나 조직 저장소로 미러링하거나, 조직의 승인 플러그인 저장소의 `.claude-plugin/marketplace.json` 에 `sdlc` 를 등록한다.
2. 관리형 설정에 `strictKnownMarketplaces: [{"source":"github","repo":"freewisl/ai-native-sdlc"}]`(미러면 그 경로) 와 `enabledPlugins: {"sdlc@ai-native-sdlc": true}` 를 넣는다.
3. 훅을 조직 통제로 만들려면 `allowManagedHooksOnly: true` 를 켜고, 플러그인이 관리형 마켓플레이스에서 설치됐는지 `/sdlc:doctor` 의 훅 자가시험으로 확인한다.
4. 팀 단위 훅(`.claude/settings.json`)은 git 으로, 타협 불가 훅은 관리형으로 — 블로그 Stage 5 "Team hooks go in .claude/settings.json in git, and non-negotiable hooks go in managed settings".

## 3. 샌드박스

- 지원: macOS(Seatbelt, 설치 없음), Linux·WSL2(bubblewrap 등 두 패키지, `/sandbox` 패널의 Dependencies 탭이 누락을 알려줌). 네이티브 Windows 미지원 → WSL2 사용.
- 키: `sandbox.enabled`, `failIfUnavailable`, `allowUnsandboxedCommands`, `autoAllowBashIfSandboxed`(샌드박스 명령은 프롬프트 없이 실행), `excludedCommands`, `network.allowedDomains`(와일드카드 `*.npmjs.org` 가능), `network.allowUnixSockets`, `network.allowLocalBinding`, `network.tlsTerminate`, `credentials.files[{path, mode: deny|mask}]`, `credentials.envVars[{name, mode}]`, `filesystem.{allowRead, denyRead, allowWrite}`.
- 프로젝트 적용: `init.sh --sandbox` 가 `templates/config/settings.sandbox.json` 을 `.claude/settings.json` 에 병합합니다. `allowedDomains` 에 사내 git 호스트와 패키지 레지스트리를 넣지 않으면 빌드가 네트워크에서 막힙니다.
- 프록시/allowlist 원리: 샌드박스 안의 네트워크는 도메인 허용목록을 거치므로, 도구 수준 deny(`WebFetch`)와 달리 셸 명령의 유출도 차단됩니다.

## 4. 관측 — OpenTelemetry

모든 세션은 지표와 이벤트를 조직의 관측 스택으로 내보낼 수 있습니다(`templates/config/otel.env`).

```json
{ "env": { "CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "otlp",
           "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.com:4317",
           "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer example-token", "OTEL_LOG_TOOL_DETAILS": "1" } }
```

- 지표: `claude_code.session.count`(병렬 세션 측정), `lines_of_code.count`, `pull_request.count`, `commit.count`, `cost.usage`, `token.usage`, `code_edit_tool.decision`, `active_time.total` — 속성 `skill.name`, `agent.name`, `plugin.name`, `marketplace.name`.
- 이벤트: `claude_code.user_prompt`, `tool_result`(`decision_source: hook`), `tool_decision`(`source: "hook"` — **훅의 allow/block 결정이 타임스탬프와 함께 기록**), `api_request`, `api_error`, `permission_mode_changed`.
- `OTEL_LOG_TOOL_DETAILS=1` 이면 스킬 이름이 도구 이벤트에 포함되어 "스킬 호출은 세션 추적에 기록된다"(Stage 3 Skills 거버넌스)가 실제로 성립합니다.
- OTel 이 없는 조직을 위해 플러그인은 `.sdlc/logs/events.jsonl` 과 `gate.log` 에 같은 결정을 로컬로 남기고 `/sdlc:metrics` 가 읽습니다.

## 5. MCP — 도구 표면의 중앙 통제

- 배포 도구를 MCP 로 노출(`templates/config/mcp.deploy.example.json`: deploy·status·rollback 을 환경별로 스코프)하면 에이전트의 배포 능력이 자격증명이 든 셸 스크립트가 아니라 허용목록이 됩니다(Stage 5 CI/CD).
- 조직 통제: 관리형 설정의 `allowedMcpServers` / `deniedMcpServers` + `allowManagedMcpServersOnly: true`, 파일 `managed-mcp.json`. 문서: code.claude.com/docs/en/managed-mcp

## 6. 모델 접근 — Bedrock · Vertex · Foundry

CI 와 파이프라인의 `claude -p` 및 `claude-code-action` 은 API 키 대신 조직의 클라우드 계약을 탈 수 있습니다: 환경변수 `CLAUDE_CODE_USE_BEDROCK=1`(+AWS 자격증명), `CLAUDE_CODE_USE_VERTEX=1`(+GCP), Foundry 설정, 액션 입력 `use_bedrock` / `use_vertex`. 문서: code.claude.com/docs/en/third-party-integrations, amazon-bedrock, google-vertex-ai, microsoft-foundry, github-actions-cloud-providers.

## 7. 감사 — 커밋 이력 + 컴플라이언스 API

- 아티팩트 체인의 모든 결정(작성자·시각·승인)은 git 커밋이고, 훅 결정은 OTel/`events.jsonl`, 리뷰 결과·승인은 PR 이력입니다(횡단 "Audit trail = commit history").
- Claude Enterprise 의 **Compliance API** 는 활동 피드·대화 조회·삭제를 제공합니다: platform.claude.com/docs/en/manage-claude/compliance-api

## 8. 참고 문서 (블로그 Resources 절, 롤아웃 순서)

| 주제 | 문서 |
|---|---|
| 조직용 Claude Code 설정 — 관리자 결정 지도 | code.claude.com/docs/en/admin-setup |
| 설정 레퍼런스와 우선순위(관리형 전용 키 포함) | code.claude.com/docs/en/settings, code.claude.com/docs/en/settings-reference |
| 관리형 설정 배포 / 서버 관리형 설정 | code.claude.com/docs/en/managed-settings, code.claude.com/docs/en/server-managed-settings |
| 권한 | code.claude.com/docs/en/permissions |
| 샌드박스 | code.claude.com/docs/en/sandboxing |
| 훅 가이드 / 레퍼런스 | code.claude.com/docs/en/hooks-guide, code.claude.com/docs/en/hooks |
| 스킬 | code.claude.com/docs/en/skills |
| 플러그인과 비공개 마켓플레이스 | code.claude.com/docs/en/plugin-marketplaces |
| 관리형 MCP | code.claude.com/docs/en/managed-mcp |
| 기업 배포 개요(Bedrock·Vertex·Foundry) | code.claude.com/docs/en/third-party-integrations |
| 기업 네트워크 구성 | code.claude.com/docs/en/network-config |
| 모니터링(OpenTelemetry) / 분석 대시보드 | code.claude.com/docs/en/monitoring-usage, code.claude.com/docs/en/analytics |
| Compliance API | platform.claude.com/docs/en/manage-claude/compliance-api |
| 보안 모델 | code.claude.com/docs/en/security |
