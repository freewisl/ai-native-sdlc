# `sdlc` 플러그인 설계서 (v1) — AI-native SDLC playbook 을 모든 프로젝트에 설치하는 Claude Code 플러그인

> 원문: https://claude.com/blog/the-ai-native-sdlc-playbook (2026-08-21)
> 요구사항 매트릭스: `_workspace/01_requirements.md` (R-ID) · 플랫폼 사실: `_workspace/00_platform_facts.md`
> 이 문서는 빌더/감사 에이전트의 **계약 문서**다. 여기 적힌 파일 경로·키 이름·프로토콜을 그대로 구현한다.

## 0. 설계 원칙

1. **플러그인 = 배포 단위, 프로젝트 = 설정 단위.** 플러그인 안에는 어떤 조직·언어·빌드 도구도 하드코딩하지 않는다.
   프로젝트별 값은 전부 `<repo>/.sdlc/config.json` 에서 읽는다. 설정이 없으면 훅은 **안전한 기본값**으로 조용히 동작한다.
2. **신규·기존 프로젝트 양쪽.** `/sdlc:init` 은 항상 비파괴: 있는 파일은 절대 덮어쓰지 않고(merge 또는 skip 보고), CLAUDE.md 는
   빠진 섹션만 추가, `.claude/settings.json` 은 permissions 배열 합집합 병합.
3. **블로그의 세 층을 그대로 구현.** 스킬(권고) → 훅(강제, 결정론) → 관리형 설정(우회 불가). 훅은 빠르고(파일 1개 범위),
   차단 시 **이유와 승인 경로**를 stderr 로 설명한다(R: "A block should explain itself").
4. **모든 단계는 커밋되는 아티팩트로 끝난다.** intent → spec → plan → diff+tests → PR+review → incident(LESSONS/intent).
   스킬은 아티팩트를 쓰고 나서 git 커밋을 제안·수행한다(`auto_commit` 설정).
5. **결정론은 스크립트, 판단은 Claude.** 탐지(bands)·게이트·비밀 스캔·측정은 bash/python3 표준 라이브러리만 사용(PyYAML 등 외부 의존 금지).
   JSON 파싱은 `jq` 우선, 없으면 `python3` 폴백.
6. **로그 = 감사 추적.** 훅 결정·세션·게이트·evals·밴드 위반은 `.sdlc/logs/events.jsonl` 에 타임스탬프와 함께 기록(OTel 이 없는 조직의 로컬 대체),
   OTel 은 문서+env 템플릿으로 안내.
7. 언어: 스킬/에이전트 본문·스크립트 메시지는 영어(Claude 가 읽음). 사람이 채우는 템플릿과 README 는 `en`/`ko` 두 세트 제공, `config.language` 로 선택.

## 1. 저장소(마켓플레이스) 레이아웃

```
/Users/brad/project/mma/sdlc/                 ← 마켓플레이스 루트 (git 저장소로 배포 가능)
├── .claude-plugin/marketplace.json           name: "ai-native-sdlc", plugins[0].source: "./plugins/sdlc"
├── README.md                                 마켓플레이스 소개 + 설치 3줄
├── docs/
│   ├── PLAYBOOK-MAPPING.md                   블로그 R-ID → 플러그인 구성요소 대응표 (감사 산출물)
│   ├── ENTERPRISE.md                         관리형 설정 배포(OS별 경로·admin console)·sandbox·OTel·MCP·Bedrock/Vertex/Foundry·Compliance API
│   └── eli5.html                             그림 설명
├── plugins/sdlc/                             ← 플러그인 루트 (${CLAUDE_PLUGIN_ROOT})
│   ├── .claude-plugin/plugin.json            name "sdlc", version, description, author, hooks 경로(플랫폼 사실에 따라)
│   ├── README.md                             플러그인 사용 설명서(한국어)
│   ├── CHANGELOG.md
│   ├── skills/<name>/SKILL.md                §4
│   ├── agents/<name>.md                      §5
│   ├── hooks/hooks.json                      §3
│   ├── scripts/
│   │   ├── lib/common.sh                     json_get · project_root · cfg · log_event · now · has_cmd
│   │   ├── hooks/{session-start,session-end,guard-edit,guard-bash,post-edit,post-bash,stop-verify}.sh
│   │   ├── init.sh  doctor.sh  status.sh  run-evals.sh  monitor.py  metrics.py  secret-scan.sh
│   │   └── secret-patterns.txt
│   ├── templates/
│   │   ├── en/ ko/   intent.md spec.md plan.md REVIEW.md CLAUDE.sections.md LESSONS.md intent-README.md evals-README.md
│   │   ├── config/   config.json bands.json frozen-paths.txt settings.project.json settings.sandbox.json managed-settings.json mcp.deploy.example.json gitignore.sdlc otel.env
│   │   ├── github/   sdlc-evals.yml sdlc-review.yml sdlc-spec-on-intent.yml sdlc-ci-triage.yml sdlc-monitor.yml sdlc-scan.yml CODEOWNERS
│   │   ├── evals/cases/{no-secret-in-code,respect-frozen-path,fix-without-editing-test}/  (prompt.md, check.sh, fixture/, case.json)
│   │   └── policies/secure-api-review/{SKILL.md, scripts/check-endpoints.sh}
│   ├── evals/                                `claude plugin eval` 용 플러그인 자체 평가(스킬 트리거 확인) — 플랫폼 사실의 case 형식을 따름
│   └── tests/run.sh + tests/fixtures/        훅·스크립트 단위 테스트(샘플 stdin) — 감사와 CI 가 실행
└── _workspace/                               작업 문서(요구사항·설계·감사 보고)
```

## 2. 프로젝트 측 레이아웃과 설정

`/sdlc:init` 후 프로젝트:
```
<repo>/
├── CLAUDE.md                     # 없으면 생성, 있으면 빠진 섹션만 추가: Commands · Conventions · Architecture · Things Claude gets wrong · Verifying your work
├── REVIEW.md                     # 3 passes / What Important means / Cap the nits / Do not report
├── intent/  spec/  plan/         # 아티팩트 체인 (config.paths 로 변경 가능). intent/triage/ 는 자동 생성 intent 큐
│   └── README.md, TEMPLATE.md
├── evals/                        # 설정 회귀 스위트: README.md, cases/<name>/{prompt.md,check.sh,fixture/,case.json}, results/(gitignore)
├── .sdlc/
│   ├── config.json               # 유일한 프로젝트 설정 (§2.1)
│   ├── bands.json                # 모니터링 밴드 (블로그 bands.yaml 의 JSON 판)
│   ├── frozen-paths.txt          # 편집 금지 경로 접두어/글롭, # 주석
│   ├── LESSONS.md                # 포스트모템 교훈(버전 관리) — Claude Tag 의 "lessons file"
│   ├── history/<metric>.jsonl    # 밴드 기준선용 관측치 (커밋 대상)
│   ├── logs/  (gitignore)        # events.jsonl, gate.log
│   └── state/ (gitignore)        # active-plan, session-<id>.json, allow-test-edit
├── .claude/settings.json         # permissions 병합(allow/deny). 훅은 플러그인이 제공하므로 여기 등록 안 함
├── .claude/skills/<policy>/      # /sdlc:policy 가 만드는 조직 정책 스킬
├── .github/workflows/sdlc-*.yml  # --github 또는 .github/ 존재 시, 없는 파일만 복사
├── .github/CODEOWNERS            # 템플릿(없을 때만)
└── .mcp.json                     # --mcp 시 배포 도구 예시(없을 때만)
```

### 2.1 `.sdlc/config.json` (v1) — 모든 스크립트·훅·스킬이 읽는 단일 설정
```json
{
  "version": 1,
  "language": "en",
  "paths": { "intent": "intent", "spec": "spec", "plan": "plan", "evals": "evals", "review": "REVIEW.md", "lessons": ".sdlc/LESSONS.md" },
  "source_of_truth": { "mode": "repo", "system": "", "record_field": "record_id" },
  "commands": { "build": "", "test": "", "lint": "", "run": "", "format": "auto", "rollback": "" },
  "verify": { "required_before_stop": false, "commands": ["test"] },
  "plan": { "require_for_edits": false, "enforce_sync": false, "exempt_paths": ["intent/", "spec/", "plan/", "CLAUDE.md", "REVIEW.md", ".sdlc/", "evals/", ".claude/", "docs/"] },
  "protect": { "frozen_paths_file": ".sdlc/frozen-paths.txt", "generated_paths": [], "protect_tests": true, "test_paths": ["auto"] },
  "secrets": { "scan_edits": true, "scan_commits": true, "extra_patterns": [] },
  "environments": {
    "dev":        { "autonomy": "free",        "patterns": [] },
    "staging":    { "autonomy": "constrained", "patterns": ["(deploy|release|rollout).*(staging|stg)"], "approval_env": "" },
    "production": { "autonomy": "gated",       "patterns": ["(deploy|release|rollout|promote).*(prod|production|live)", "kubectl .*(apply|rollout|set image).*(prod|production)", "helm (upgrade|install).*(prod|production)", "terraform apply.*(prod|production)"], "approval_env": "RELEASE_APPROVAL" }
  },
  "gate": { "mode": "deny", "log": ".sdlc/logs/gate.log" },
  "hooks": { "format_on_edit": true, "log_events": true, "session_context": true },
  "policies": [],
  "evals": { "model": "sonnet", "max_turns": 30, "threshold": 1.0, "allowed_tools": "Read,Edit,Write,Grep,Glob,Bash" },
  "monitor": { "bands": ".sdlc/bands.json", "history_dir": ".sdlc/history", "triage_dir": "intent/triage" },
  "auto_commit": true
}
```
- `mode` 의미: `free` 통과, `constrained` → 훅이 JSON `permissionDecision: "ask"` (사람 확인; -p 모드에선 거부됨), `gated` → `approval_env` 가 비어 있으면 exit 2 차단.
- `commands.rollback` 이 설정돼 있으면 그 명령은 환경 패턴과 무관하게 **항상 통과**(가장 연습된 경로).
- `test_paths: ["auto"]` = 내장 글롭 집합: `test/`, `tests/`, `__tests__/`, `spec/` (단, config.paths.spec 과 같으면 제외), `src/test/`, `*_test.go`, `*.test.*`, `*.spec.*`, `test_*.py`, `*_test.py`, `*Test.java`, `*Tests.java`, `*Spec.scala`, `*_spec.rb`.
- 테스트 편집 허용 조건(하나라도): env `SDLC_ALLOW_TEST_EDIT=1` · 파일 `.sdlc/state/allow-test-edit` 존재 · 활성 plan(`.sdlc/state/active-plan` 이 가리키는 파일) 프론트매터 `test_changes: allowed`.
  (버그 수정 프로토콜: "실패 테스트 먼저 작성·커밋" 단계에서는 `/sdlc:verify --bugfix` 가 flag 파일을 만들고, "고치기" 단계에서 지운다.)

### 2.2 아티팩트 프론트매터 (모든 intent/spec/plan 공통)
```yaml
---
type: intent | spec | plan
slug: claims-status-self-service
title: Claims status self-service
status: draft | approved | rejected | superseded      # intent/spec ; plan: draft | approved | implemented
author: name
created: 2026-09-06
source: idea | ticket | incident | monitor | scan | channel
record_id: ""            # 레거시 시스템 ID (source_of_truth linkage)
links: { intent: "", spec: "", plan: "" }
test_changes: forbidden  # plan 전용: forbidden | allowed
---
```
파일명: `<paths.intent>/<slug>.md` 등. 같은 slug 가 체인을 잇는다. 승인은 `status: approved` 커밋(=merge/review 기록).

### 2.3 로그 형식 `.sdlc/logs/events.jsonl`
`{"ts":"2026-09-06T12:00:00Z","event":"hook.gate|hook.guard|session.start|session.end|eval.run|band.breach|triage","session_id":"...","decision":"allow|deny|ask","reason":"...","detail":{...}}`
gate.log 는 사람이 읽는 탭 구분 한 줄(`ts \t decision \t env \t cmd`).

## 3. 훅 (plugins/sdlc/hooks/hooks.json)

| 이벤트 | matcher | 스크립트 | 동작 | 블로그 근거 |
|---|---|---|---|---|
| SessionStart | * | session-start.sh | events 로그(세션 수 측정) + stdout 으로 컨텍스트 주입: 활성 plan/slug·상태, triage 큐 건수, verify 명령, 규칙 3줄(계획 없이 구현 금지·완료 전 검증·같은 실수 2번→CLAUDE.md). `.sdlc/` 없으면 한 줄 힌트(`/sdlc:init`)만 | Stage 3 plan/CLAUDE.md, 병렬 세션 측정 |
| SessionEnd | * | session-end.sh | events 로그 | 병렬 세션 측정 |
| PreToolUse | Edit\|Write\|MultiEdit\|NotebookEdit | guard-edit.sh | ① frozen/generated 경로 차단 ② 테스트 경로 보호(§2.1 조건) ③ `plan.require_for_edits` 시 승인된 활성 plan 없으면 차단(exempt_paths 제외) ④ 비밀 패턴 스캔(new_string/content) | Build 훅 3종, Test 훅, "Nothing is implemented without an accepted plan" |
| PreToolUse | Bash | guard-bash.sh | ① 환경 티어 게이트(production gated/staging ask/rollback 항상 허용) + gate.log·events 기록 ② `git commit`/`git add` 시 스테이지된 diff 비밀 스캔(`scan_commits`) | Deploy 게이트 훅, 자격증명 diff 차단, 티어별 자율도 |
| PostToolUse | Edit\|Write\|MultiEdit | post-edit.sh | 포매터 실행(`commands.format` 또는 auto 탐지: prettier/biome/ruff/black/gofmt/rustfmt/google-java-format 존재 시), 세션 state 에 `edited=true` | "Run the formatter and linter after file edits" |
| PostToolUse | Bash | post-bash.sh | 명령이 verify 명령(commands.test/build/lint)과 일치하면 성공/실패를 세션 state 에 기록 | 피드백 루프·first-pass 측정 |
| Stop | * | stop-verify.sh | `verify.required_before_stop` 이고 edited 후 성공한 verify 가 없으면 `{"decision":"block","reason":...}` (stop_hook_active 면 통과) ; `plan.enforce_sync` 이고 활성 plan 있고 edited 면 plan.md 갱신 여부 확인 요구 1회 | "Make verification part of done", "update plan.md in the same commit… hook to enforce synchronization" |

규약: 모든 훅은 `bash "${CLAUDE_PLUGIN_ROOT}/scripts/hooks/<name>.sh"` 로 호출(실행 비트 의존 금지). stdin JSON 은 한 번만 읽어 변수에 보관.
프로젝트 루트 = `$CLAUDE_PROJECT_DIR` → 없으면 stdin `cwd` → 없으면 `pwd`. 설정 파일이 없으면: guard-edit 는 비밀 스캔만, guard-bash 는 production 기본 패턴 게이트만, 나머지는 즉시 exit 0. 실행 시간 목표 < 200ms(jq 경로).
차단 메시지 형식: `[sdlc] BLOCKED: <what>. Why: <rule>. To proceed: <route to approval>` (한 줄 + 필요 시 둘째 줄).

## 4. 스킬 (plugins/sdlc/skills/<name>/SKILL.md) — 17개

| 스킬 | Stage | 하는 일 | 산출물/부작용 |
|---|---|---|---|
| `init` | 전환 | 프로젝트 감지(git? 빌드도구? CLAUDE.md? CI? 언어) → `scripts/init.sh` 로 결정론적 스캐폴드(비파괴) → Claude 가 CLAUDE.md 4섹션+Verifying 블록을 저장소 조사로 채움 → settings 병합 → 옵션 `--lang ko|en --github --sandbox --managed --mcp --dry-run` → 마지막에 `doctor` 실행. 신규(빈 repo)도 기존도 동일 진입점 | 위 §2 파일들 |
| `doctor` | 전환 | `scripts/doctor.sh`: 채택 상태표(항목별 ✓/△/✗), config 검증, 훅 자가시험(샘플 stdin), 의존성 그래프 기준 "다음 할 일" | 상태 보고 |
| `intent` | 1 | 브레인스토밍(분석가 질문: 범위·사용자·제약·성공 기준) → 템플릿(문제·제안 결과·영향 사용자/시스템·제약·미해결 질문)으로 `intent/<slug>.md` 작성 → 사용자 교정 → 커밋. `--from-ticket <id>` 시 record_id 기록, `--from-incident` | intent.md, 커밋 |
| `spec` | 2 | 승인된 intent 읽기 → `config.policies` 의 정책 스킬 각각 호출(제약 적용) → 요구사항·설계·**Flagged concerns**(정책 충돌·미해결) → `spec/<slug>.md` → PO 검토 포인트 제시 → 커밋. 블로그 프롬프트 문구 내장 | spec.md |
| `plan` | 3 | 플랜 모드 강제(EnterPlanMode; 이미 plan 모드면 생략) → spec/intent 읽기 → 파일·순서·위험·증명 4섹션 → "무엇이 깨질 수 있나/가장 위험한 단계/택하지 않은 대안" 자문 → 승인 → `plan/<slug>.md`(status approved) 커밋 + `.sdlc/state/active-plan` 설정 → 구현 → 벗어나면 plan.md 같은 커밋에 갱신 | plan.md, active-plan |
| `verify` | 4 | 피드백 루프 실행: config.commands 로 build/test/lint 실행·정량 목표 확인·출력 붙이기; `--bugfix`: 실패 테스트 먼저(flag 생성)→커밋→flag 제거→테스트 수정 금지 상태로 고치기; `--ui`: 스크린샷 비교 루프 안내(브라우저/MCP); 끝에 `sdlc-verifier` 서브에이전트로 신선한 컨텍스트 검증 | 검증 출력 |
| `evals` | 4 | `add`(최근 작업/인시던트/PR/취약점 클래스 → cases/<name>/ 생성: prompt.md + check.sh + fixture) · `run`(`scripts/run-evals.sh`, 임계치, 결과 jsonl) · `list` · `report` | evals/cases, results |
| `review` | 5 | REVIEW.md 로드 → `sdlc-reviewer` 에이전트로 3 패스(bugs/security/compliance vs spec.md·plan.md) → Important/Nit 분류·nit ≤5 → 사람이 승인(에이전트 승인 금지 명시) → 같은 실수 2회 → `lesson` 호출 → CLAUDE.md 낡음 경고. `--pr <n>`: gh 로 미해결 코멘트·실패 체크 스윕→수정→푸시 반복(babysit) | 리뷰 보고, CLAUDE.md 갱신 |
| `lesson` | 3/5 | "Things Claude gets wrong" 에 1줄 추가(중복 검사), 필요 시 `evals add` 연계, 한 페이지 초과 경고 | CLAUDE.md |
| `release` | 5 | 환경 인자(dev/staging/production) → 티어 확인 → production 은 approval_env 확인·롤백 명령 존재 확인 후 진행(훅이 2중 강제) → 결정 로그 → 배포 후 `monitor` 1회 제안 | gate.log |
| `monitor` | 6 | `scripts/monitor.py` 실행(관측→기준선→WE 규칙→티어→log/diagnose/propose) `--dry-run` `--metric` | history, triage intent |
| `triage` | 6 | `intent/triage/*.md` 큐 순회: fix now(→`plan`)/schedule(status)/dismiss(reason → bands 튠 메모) 결정 기록 | intent status, events |
| `scan` | 6 | 정기 스캔: 자체 실행(보안 체크리스트로 코드베이스 스캔, 발견별 신뢰도·근거) → 한 PR 크기면 수정 제안, 크면 `intent` 생성 → 수정 배포 후 `evals add` 권고; Claude Security(호스팅) 절차 안내 | 스캔 보고, intent |
| `postmortem` | 6 | 인시던트 → `.sdlc/LESSONS.md` 항목 + `lesson` + `evals add` + 필요 시 `intent --from-incident` (Claude Tag 의 온콜 폐쇄 루프를 로컬에서 수행) | LESSONS.md 등 |
| `policy` | 2/3 | 정책 스킬 스캐폴드: `.claude/skills/<name>/SKILL.md`(owner·source of truth·트리거 문구·검사 스크립트) → config.policies 등록 → 트리거 테스트 절차(다르게 3번 물어 로드 확인) → `--example secure-api-review` | 정책 스킬 |
| `metrics` | 전체 | `scripts/metrics.py`: 선행/후행 지표표(각 스테이지) — git·events·gate.log·evals.jsonl·history 로 계산 가능한 것은 값, 외부 데이터(CI·인시던트 트래커·PR 메타) 필요한 것은 "source needed: …" 표기 | 마크다운 대시보드 |
| `status` | 전체 | `scripts/status.sh [slug]`: 체인 상태(intent/spec/plan 존재·status·타임스탬프·spec 재작업 카운트·활성 plan) | 표 |

스킬 공통 규약: frontmatter `name`, `description`(트리거 문구 영어+한국어 키워드 병기), 필요 시 `argument-hint`. 본문은 "When to use / Inputs / Steps / Output / Governance(누가 승인) / Measurement(어떤 로그가 남는지)" 순. 스크립트 호출은 `bash "${CLAUDE_PLUGIN_ROOT}/scripts/..."` — 플랫폼 사실에서 스킬 본문에서 CLAUDE_PLUGIN_ROOT 가 치환되는지 확인하고, 안 되면 `Bash(ls ~/.claude/plugins/...)` 대신 스킬 본문에 상대 경로 규약을 기술.

## 5. 에이전트 (plugins/sdlc/agents/) — 5개

| 에이전트 | tools | 역할 |
|---|---|---|
| `sdlc-verifier` | Bash, Read, Grep, Glob | 앱/테스트 실행, 변경 동작 + 인접 플로우 2개 점검, plan.md 와 불일치 보고. **수정 금지, 보고만** (블로그 verifier.md 원형) |
| `sdlc-reviewer` | Read, Grep, Glob, Bash(git diff/log 만) | REVIEW.md 3 패스, 심각도 태깅, nit 상한, spec/plan 대조. 승인 권한 없음 명시 |
| `sdlc-diagnoser` | Read, Grep, Glob, Bash(읽기 전용 명령) | 2σ 진단: 원인 가설·증거 → intent.md 형식 초안. 파일 수정 금지 |
| `sdlc-simplifier` | Read, Edit, Grep, Glob, Bash | 메인 에이전트 후 불필요한 복잡도 제거, 테스트 재실행 (블로그 예시) |
| `sdlc-researcher` | Read, Grep, Glob | 코드베이스 탐색 후 요약 보고, 메인 컨텍스트 오염 방지 (블로그 예시) |

## 6. 스크립트 상세

- `lib/common.sh`: `json_get <json> <jq-path>`(jq → python3 폴백), `project_root`, `cfg <path> <default>`(config.json 캐시), `log_event <event> <decision> <reason> [detail-json]`, `now_iso`, `block <msg>`(stderr+exit 2), `ask_json <reason>`, `path_rel`, `glob_match`.
- `init.sh [--lang en|ko] [--github] [--sandbox] [--managed] [--mcp] [--dry-run] [--dir <root>]`: 결정론 부분만. 생성/병합/스킵 목록을 표로 출력. CLAUDE.md 는 `<!-- sdlc:begin <section> -->` 마커로 빠진 섹션 스텁만 추가(Claude 가 내용 채움). settings 병합은 python3 로 allow/deny 합집합. `.gitignore` 에 `.sdlc/logs/ .sdlc/state/ evals/results/` 추가. `intent/triage/.gitkeep`.
- `doctor.sh [--json]`: 상태표 + 훅 자가시험(각 훅에 샘플 payload 를 넣어 기대 결정 확인) + config 스키마 검사(필수 키·타입) + 의존 순서상 다음 단계.
- `status.sh [slug]`.
- `run-evals.sh [--case glob] [--model m] [--threshold t] [--max-turns n] [--dry-run]`: case 마다 임시 디렉터리에 fixture 복사 + 프로젝트의 CLAUDE.md/.claude/.sdlc 복사(=시험 대상 설정) → `claude -p "$(cat prompt.md)" --output-format json --max-turns N --allowedTools <case.json.allowed_tools|config> --model M` → `check.sh <workdir>` → 결과 `evals/results/<ts>/summary.md|results.json` + `.sdlc/logs/events.jsonl(eval.run)` + `.sdlc/history/eval_pass_rate.jsonl` 추가(밴드 입력으로 재사용). 임계치 미달 시 exit 1(CI 게이트).
- `monitor.py [--dry-run] [--metric name] [--config .sdlc/bands.json]`: bands.json 스키마 `{ "metrics": [ { "name", "command"(관측값 1개 출력) 또는 "history_only": true, "baseline": {"window": 30}, "rules": "western_electric", "tiers": {"1sigma": {"action":"log"}, "2sigma": {"action":"diagnose","tools":"Read,Grep,Bash(gh run view *)"}, "3sigma": {"action":"propose","routes":["pull_request","runbook:rollback"]}}, "runbooks": {"rollback": "<command>"} } ] }`. 관측치는 `history/<name>.jsonl` 에 append. WE 규칙: R1 1점>3σ, R2 3점 중 2점>2σ 동측, R3 5점 중 4점>1σ 동측, R4 8점 연속 동측 → 최고 티어 결정. 액션: log → events; diagnose → `claude -p` 읽기 전용(tools 제한, `--permission-mode` 기본) 결과를 intent 템플릿(Stage 1 형식: anomaly & evidence / proposed outcome / affected systems / open questions)으로 `intent/triage/<ts>-<metric>.md` 저장(status: draft, source: monitor); propose → `claude -p` 가 `routes` 중 하나만 허용(`pull_request`: 브랜치+PR 생성(gh 있으면), `runbook:<name>`: 사전 승인된 명령 실행) — 모두 events 로그. 모델 미개입 원칙: 탐지·티어 결정은 python 만.
- `metrics.py [--since 90d] [--format md|json]`: 지표 계산표 — Stage1: intent 첫 커밋까지 시간(frontmatter created→git 첫 커밋), 생존율(approved/rejected), spec 이후 intent 변경 수; Stage2: intent→spec 커밋 간격, plan 이후 spec 커밋 수; Stage3: plan→구현 커밋 간격, CLAUDE.md lessons 증가 추이, 세션 동시성(events); Stage4: eval pass rate 추이, verify 성공/실패(events), 인시던트→eval 시간(LESSONS↔evals 생성일); Stage5: 게이트 대기/차단 수(gate.log), 리뷰 findings 수(events 에 review 기록); Stage6: 밴드 위반→triage intent 시간, triage 결정 분포, 반복 인시던트. PR/CI/DORA 등 외부는 "source needed" 로 표기하고 gh 가 있으면 시도.
- `secret-scan.sh <text|--staged>`: 패턴 파일(AWS AKIA, GitHub ghp_/github_pat_, Slack xox, private key 블록, generic `(api[_-]?key|secret|token|password)\s*[:=]\s*['"][^'"]{12,}`, JWT, Google API key, Anthropic sk-ant-) + extra_patterns. 화이트리스트: `example`, `REPLACE_ME`, `changeme`, `<...>`, 테스트 fixture 경로 `evals/`.

## 7. 템플릿 요점
- `CLAUDE.sections.md`: 블로그 Payments 예시 구조 그대로(Commands/Conventions/Architecture/Things Claude gets wrong) + "Verifying your work" 블록(Build/Test/Lint + "Run all three before reporting… If a test fails, fix the code, not the test.") — 값은 init 이 감지한 명령으로 치환, 없으면 TODO 표기.
- `REVIEW.md`: 블로그 원문 4섹션 그대로 + 프로젝트 generated 경로 치환.
- `managed-settings.json`: 블로그 JSON **그대로**(도메인·저장소만 플레이스홀더) + 각 키 설명 주석은 ENTERPRISE.md 에.
- `settings.project.json`: permissions allow(git·감지된 build/test/lint) / deny(Read(.env*), Read(**/secrets/**), Bash(git push --force*)) — 훅은 넣지 않음(플러그인 제공).
- `settings.sandbox.json`: sandbox 블록(enabled, failIfUnavailable, allowUnsandboxedCommands false, network.allowedDomains 플레이스홀더, credentials files/envVars).
- `otel.env`: CLAUDE_CODE_ENABLE_TELEMETRY 등(플랫폼 사실의 정확한 이름).
- GitHub 워크플로 6종: `sdlc-evals.yml`(블로그 원형: PR paths CLAUDE.md/.claude/**/evals/** + cron 02:00 → run-evals.sh --threshold) · `sdlc-review.yml`(claude-code-action: PR opened/synchronize 시 REVIEW.md 기반 리뷰, issue_comment `@claude` 대응; 승인 없음) · `sdlc-spec-on-intent.yml`(intent/**.md 가 status approved 로 main 에 머지되면 비대화형으로 spec 생성 PR — Stage 2 자동 트리거) · `sdlc-ci-triage.yml`(workflow_run failure → `claude -p` 3줄 triage 코멘트, 블로그 pipeline step 원문) · `sdlc-monitor.yml`(cron → monitor.py → triage intent 커밋 PR) · `sdlc-scan.yml`(주간 cron → scan → 이슈/PR). 모두 `ANTHROPIC_API_KEY` 시크릿 + Bedrock/Vertex/Foundry env 주석.
- evals 예시 3건: 비밀 하드코딩 금지 / frozen 경로 존중 / 테스트 수정 없이 버그 고치기(fixture 는 bash 스크립트 기반으로 언어 무관).

## 8. 채택 경로(블로그 의존성 그래프) — doctor 의 "다음 할 일" 순서
1. CLAUDE.md(Stage 3, 의존 없음) + intent.md(Stage 1, 의존 없음) + 훅 게이트 목록(Stage 5 hooks, 의존 없음) + 피드백 루프(Stage 4, 의존 없음) + 스킬(의존 없음)
2. spec(intent 필요) → 3. plan(spec 필요, CLAUDE.md 도움) → 4. 병렬 세션/서브에이전트(CLAUDE.md·피드백 루프) → 5. evals(CLAUDE.md·피드백 루프) → 6. PR 리뷰(CLAUDE.md·스킬·서브에이전트) → 7. CI/CD(리뷰·게이트) → 8. 닫힌 루프/스캔/온콜(intent·리뷰·게이트·롤백)

## 9. 검증 계획(감사 에이전트가 실행)
- `command claude plugin validate plugins/sdlc --strict` 및 마켓플레이스 validate 통과.
- `tests/run.sh` 전건 PASS(훅 7종 × 시나리오, init 비파괴 시나리오: 빈 디렉터리 / 기존 CLAUDE.md+settings+CI 있는 디렉터리 / jq 없음 시뮬레이션(PATH 조작) / git 아님).
- 실제 실행: 임시 신규 프로젝트에서 `claude --plugin-dir plugins/sdlc -p "/sdlc:doctor"` 와 기존 프로젝트 복제본에서 동일 실행 → 스킬 로드·스크립트 실행 확인.
- 커버리지: `01_requirements.md` 의 모든 R-ID 가 `docs/PLAYBOOK-MAPPING.md` 에 구성요소 또는 "external product – documented" 로 대응.
