# 플레이북 요구사항 → `sdlc` 플러그인 구성요소 대응표

원문: Anthropic, *The AI-native SDLC playbook* (2026-08-21, https://claude.com/blog/the-ai-native-sdlc-playbook).
원문을 검증 가능한 문장 단위로 쪼갠 349개 요구사항(R-ID)에 대해, **구현 파일을 직접 열어** 근거를 확인한 결과입니다.
동작을 요구하는 행은 그 동작을 담은 파일만 근거로 인정합니다 — 훅·스크립트는 코드가, 스킬은 SKILL.md 의 단계가, 템플릿은 템플릿 본문이, CI 는 워크플로 파일이 실제로 담고 있어야 COVERED 입니다.
원문이 설명·안내를 요구하는 행(전환표의 두 열, 플레이별 전제, 채택 순서, 외부 참조)은 그 서술이 사용자에게 배포되는 문서에 있어야 COVERED 이므로 `README.md`·SKILL.md 본문을 근거로 적었습니다.
판정은 2차 감사(수정 반영 후) 기준이며, 이후 최종 검토(`_workspace/08_final_review.md`)의 반영분은 근거란의 괄호 주석으로 덧붙였습니다. 실행 검증(0.4.5): `bash plugins/sdlc/tests/run.sh` — 훅 206건 · 스크립트 163건 · `monitor.py --selftest` 7건 전부 통과, `claude plugin validate --strict` 는 플러그인·마켓플레이스 양쪽 통과.
사용 형태는 `/sdlc:init` 한 번 + 변경마다 `/sdlc:go` 한 줄이며, `go` 는 아래 표의 단계 스킬(intent·spec·plan·verify·review)을 같은 순서로 부르므로 각 행의 근거는 `go` 경로에도 그대로 적용됩니다.

## 판정 기준

| 판정 | 의미 |
|---|---|
| COVERED | 구현 파일이 요구를 충족한다 |
| PARTIAL | 일부만 충족. 근거란에 빠진 부분을 적었다 |
| MISSING | 구현·문서 어디에도 없다 |
| EXTERNAL-DOCUMENTED | Anthropic 호스팅 제품이나 조직 절차라 플러그인이 구현할 수 없고, 문서로 안내하며 로컬 대체를 제공한다 |
| NOT-APPLICABLE | 해당 없음 |

경로 표기: 접두어가 없는 경로는 `plugins/sdlc/` 기준입니다. `docs/…` 와 마켓플레이스 `README.md` 는 저장소 루트 기준이고, 플러그인 사용 설명서는 `README.md`(plugins/sdlc) 로 적었습니다. `file:line` 은 확인 당시의 줄 번호입니다.

## ID 체계

`R-<stage>.<play>.<seq>` 는 스테이지별 플레이, `R-H.*` 는 횡단 주제(INTRO 서론 · SHIFT 전환표 · ART 아티팩트 체인 · DEP 의존성 그래프 · SOT 기록 원천 · PERM 권한/샌드박스 · MCP · OTEL), `R-X.*` 는 원문 말미의 Resources 목록입니다.

---

## 1. 횡단 — 서론과 정의 (R-H.INTRO)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.INTRO.1 | 6단계와 전통적 역할별 핸드오프 모델 | COVERED | README.md:16-17 이 역할별 소유(프로덕트 매니저 요구사항 · 아키텍트 설계 · 엔지니어 빌드 · QA 검증 · 릴리스 팀 배포 · 운영 감시)와 문서·티켓·서명 핸드오프를 명시 |
| R-H.INTRO.2 | 옛 통제가 존재했던 이유 | COVERED | README.md:18 "PRD·추정 의식·제품 보안 리뷰는 코드 작성이 가장 느리고 비쌌던 시절 … 정렬을 강제하기 위해 존재했습니다" |
| R-H.INTRO.3 | 세 가지 결과(병목 이동·통제 불일치·거버넌스 비용) | COVERED | README.md:19-20 에 ①병목 이동 ②한 줄씩 읽는 통제의 불일치 ③위원회 주기에 묶인 거버넌스 비용 세 가지가 모두 있음 |
| R-H.INTRO.4 | "빌드는 붕괴하고 사람 속도 단계는 그대로" | COVERED | README.md:8 "빌드 단계를 몇 시간으로 줄이면 병목은 그 앞뒤의 계획·설계·리뷰·배포로", docs/eli5.html 도입부 |
| R-H.INTRO.5 | 보안 팀 병목 → 자동화된 보안·정책 검사 필요 | COVERED | skills/scan/SKILL.md:16 "Security teams are sized for human output … a regulated organization can accept neither", README.md:193 대응표 Recurring scans 행에 같은 근거 |
| R-H.INTRO.6 | 정의: 옛 통제 목표 + 새 집행, 루프, 자동 핸드오버, 동의어 | COVERED | README.md:14 세 층 + README.md:21 "agentic SDLC, AI SDLC, agentic software development 라고도 부릅니다" |
| R-H.INTRO.7 | 선 vs 루프, "humans above the loop" | COVERED | README.md:10 "선형 절차가 아닌 루프", README.md:13 "사람은 판단이 필요한 게이트에만" |
| R-H.INTRO.8 | 사람 판단이 중심, 규제 기업 고려 | COVERED | README.md:13, docs/ENTERPRISE.md 전체, docs/eli5.html 마지막 줄 인용 |

## 2. 횡단 — 전환표 (R-H.SHIFT)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.SHIFT.1 | Plan 행 + intent.md 템플릿 | COVERED | README.md:25-27 전환표 Plan 행 — 전통 열 "요구사항을 위원회가 모아 워크숍과 서명을 거쳐 손으로 정리", AI-native 열 intent.md. skills/intent/SKILL.md:9-11 What changes |
| R-H.SHIFT.2 | Design 행 | COVERED | README.md:28 Design 행 양쪽 열. skills/spec/SKILL.md:9-11 What changes |
| R-H.SHIFT.3 | Build 행 | COVERED | README.md:29 Build 행 양쪽 열(손으로 쓴 테스트·사후 문서 ↔ AI 생성 + 버전 관리되는 CLAUDE.md·스킬) |
| R-H.SHIFT.4 | Test 행 | COVERED | README.md:30 Test 행 양쪽 열(단계 경계 QA 게이트 ↔ 구현 사이에 짜여 들어간 연속 evals) |
| R-H.SHIFT.5 | Deploy 행 | COVERED | README.md:31 Deploy 행 양쪽 열. skills/review/SKILL.md:9-11 · skills/release/SKILL.md:10-12 What changes |
| R-H.SHIFT.6 | Maintain 행 | COVERED | README.md:32 Maintain 행 양쪽 열. skills/monitor/SKILL.md:10-12 What changes |
| R-H.SHIFT.7 | 채택은 스펙트럼 | COVERED | README.md:321 "처음에는 손으로 호출하고, 익숙해지면 CI 템플릿이 트리거", doctor 의 ✓/△/✗ 채택 상태표(scripts/doctor.sh) |

## 3. 횡단 — 아티팩트 체인·플레이 구조·트리거·감사 추적 (R-H.ART)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.ART.1 | 각 단계는 커밋으로 끝나고 다음 단계는 그 파일을 읽으며 시작 | COVERED | skills/intent/SKILL.md:46-50, skills/spec/SKILL.md:22,52, skills/plan/SKILL.md:23,44, skills/intent/references/artifact-conventions.md §6 |
| R-H.ART.2 | intent·spec·plan·diff+tests·PR+findings·incident record | COVERED | templates/en|ko/{intent,spec,plan}.md, templates/github/sdlc-review.yml, templates/en|ko/LESSONS.md |
| R-H.ART.3 | 초기 단계는 .md, Build 이후는 코드와 기록 | COVERED | README.md:34-35 "초기 단계의 아티팩트가 .md 인 이유는 프로덕트 오너와 에이전트가 같은 파일을 읽고 행동할 수 있기 때문이고, Build 부터는 코드와 그 기록(diff·테스트·PR·인시던트)이 아티팩트" |
| R-H.ART.4 | 커밋 체인 = 감사 추적 | COVERED | README.md:323-327 "승인 = 커밋", artifact-conventions.md §6, docs/ENTERPRISE.md §7 |
| R-H.ART.5 | 게이트마다 사람 승인자 | COVERED | README.md:144-162 스킬 표의 "승인자" 열 |
| R-H.ART.6 | 트리거 맵(intent 수락→spec, spec 승인→plan, PR 머지→파이프라인, 밴드 위반→intent) | COVERED | README.md:313-321 상태 전이, templates/github/sdlc-spec-on-intent.yml, scripts/monitor.py `write_triage_intent` |
| R-H.ART.7 | 수동 명령 + 자동화 템플릿 | COVERED | 스킬 19종(수동; `go` 는 한 세션에서 체인 전체를 이어 실행, `run` 은 큐를 반복), CI 템플릿 7종(자동). spec→plan 전이는 플랜 모드가 대화형이라 수동만 제공 |
| R-H.ART.8 | 플레이 문서 구조(What changes·Getting started·Steps·Governance·Measure) | COVERED | 스킬 19종 전부 `## What changes`(Traditional/AI-native)·`Prerequisites:`·`Infrastructure:` 보유(hooks 플레이는 `skills/release` What changes + `README.md` 훅 7종 절 첫 단락) |
| R-H.ART.9 | 플레이마다 Prerequisites 줄 | COVERED | `Prerequisites:` 줄이 스킬 14종에 있음 — intent:17 spec:19 plan:18 verify:18 evals:14 review:19 release:19 monitor:21 scan:21 postmortem:18 triage:17 policy:12 init:15. 원문 각 플레이의 문장을 그대로 인용 |
| R-H.ART.10 | 6단계로 조직 | COVERED | README.md:115-136 플레이 대응표, README.md:144-162 스킬 표의 단계 열 |

## 4. 횡단 — 의존성 그래프와 채택 순서 (R-H.DEP)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.DEP.1 | 스테이지 소속 ≠ 채택 순서, "clay 플레이부터" | COVERED | README.md:146-150 "채택 순서" 절 — "플레이는 스테이지별로 나열되지만 화살표가 채택 순서를 준다 — 둘은 같지 않다" + 아무 1층 플레이부터 시작 가능, scripts/lib/scripts-extra.sh:239-244 가 단일 출처임을 명시 |
| R-H.DEP.2 | 시작 플레이 5개(Capture intent·CLAUDE.md·Feedback loop·Hooks·Plan mode) | COVERED | scripts-extra.sh:265-276 1층 = Capture intent · CLAUDE.md · Feedback loop · Hooks(승인 게이트 목록) · **Plan mode**; git init 과 /sdlc:init 은 262-264 의 0층. README.md:154-155 표에 동일 |
| R-H.DEP.3 | Skills ← CLAUDE.md(그림) vs 텍스트 "의존 없음" 병기 | COVERED | skills/policy/SKILL.md:12 "None required. Having a CLAUDE.md helps … but a skill does not depend on it (text); the dependency figure draws CLAUDE.md → Skills" — 텍스트와 그림을 병기. README.md:165 불일치 표 |
| R-H.DEP.4 | Subagents ← CLAUDE.md, ⇠ Feedback loop | COVERED | README.md:231 "전제: CLAUDE.md(모든 세션이 읽음). 피드백 루프(Stage 4)가 있으면 … (그림: CLAUDE.md → Subagents 실선, Feedback loop ⇢ 점선)" |
| R-H.DEP.5 | Evals ← CLAUDE.md, ← Feedback loop | COVERED | skills/evals/SKILL.md:11-12 "Prerequisites: CLAUDE.md and the feedback loop" |
| R-H.DEP.6 | Req&Design ← Capture intent, ← Skills | COVERED | skills/spec/SKILL.md:19 "Write an intent.md file, with brand, security, compliance, and UX policies written as skills (dependency figure: Capture intent → Requirements & design, Skills → Requirements & design)" |
| R-H.DEP.7 | PR review ← Evals, ⇠ Skills + 텍스트 전제(CLAUDE.md·스킬·서브에이전트) | COVERED | skills/review/SKILL.md:19 "An updated CLAUDE.md from Stage 3; skills if the review passes enforce written policies; defined subagents (text). Dependency figure: Evals → PR review (solid), Skills ⇢ PR review (dotted)" |
| R-H.DEP.8 | CI/CD ← PR review, ← Hooks | COVERED | skills/release/SKILL.md:11-12, scripts/lib/scripts-extra.sh:304-305 |
| R-H.DEP.9 | Closing the loop ← CI/CD, ← Capture intent | COVERED | skills/monitor/SKILL.md:21 "intent.md … Claude-accelerated PR reviews; hooks as an action boundary; and a rollback path for CI/CD" |
| R-H.DEP.10 | 그림에 없는 플레이의 위치를 꾸미지 않음 | COVERED | 스캔·온콜은 자체 텍스트 전제(리뷰 게이트·훅·intent)에 따라 닫힌 루프 단계와 함께(scripts-extra.sh:311); auto mode·관리형 설정은 순서에 넣지 않음 |
| R-H.DEP.11 | 권장 순서 (1)시작 5 → (2)Skills·Subagents·Evals → (3)Req&Design·PR review → (4)CI/CD → (5)Closing | COVERED | scripts-extra.sh:239-306 이 5층(0 준비 → 1 시작 5개 → 2 Skills·Subagents·Evals → 3 Req&Design·PR review → 4 CI/CD → 5 Closing)을 유일한 출처로 구현하고, skills/init/SKILL.md:66 과 skills/doctor/SKILL.md:29 는 자체 순서를 버리고 "그대로 보여 준다"로 위임. README.md:152-159 표가 같은 5층, 161-168 이 원문 텍스트↔그림 불일치 4건을 병기 |
| R-H.DEP.12 | Resources 롤아웃 순서 | COVERED | docs/ENTERPRISE.md §8 표가 원문 순서(admin → settings → managed → permissions → sandbox → hooks → skills → plugins → MCP → enterprise → network → monitoring/analytics → compliance → security) |

## 5. 횡단 — 레거시 시스템과 기록 원천 (R-H.SOT)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.SOT.1 | 기존 시스템(Jira·요구사항 도구·Figma·변경위원회)에 맞춰야 함 | COVERED | README.md:331 |
| R-H.SOT.2 | 아티팩트마다 하나의 기록 원천 선언, 아티팩트별로 다를 수 있음 | COVERED | templates/config/config.json `source_of_truth.per_artifact` = `{intent:"",spec:"",plan:""}`(빈 값은 `mode` 상속), skills/intent/references/artifact-conventions.md:110 이 조회 순서(`per_artifact.<kind>` → `mode`)와 "intent 는 Jira, plan 은 repo" 예를 규정, README.md:282 설정 표 |
| R-H.SOT.3 | 옵션 A: 저장소가 원천 | COVERED | README.md:335 `repo` 모드 |
| R-H.SOT.4 | 옵션 B: 레거시가 원천, 세션 시작에 읽고 같은 세션에서 MCP 로 되쓰기 | COVERED | README.md:336, artifact-conventions.md §7 (`legacy`: 시작 시 읽기·커밋 후 SHA 되쓰기) |
| R-H.SOT.5 | 옵션 C: record ID + 커밋 SHA 연계 | COVERED | 세 템플릿 모두 `record_id: ""`, artifact-conventions.md §7 `linkage` |
| R-H.SOT.6 | 공존 조건 | COVERED | README.md:331-337 |
| R-H.SOT.7 | Stage 1 문서가 사이드바를 참조 | COVERED | README.md:117 Stage 1 행의 `source`/`record_id`, README.md:122 사이드바 행 |

## 6. 횡단 — 권한과 샌드박스 (R-H.PERM)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.PERM.1 | MDM/관리 콘솔로 배포, 엔지니어 수정 불가 | COVERED | scripts/init.sh:303-312 는 `.sdlc/managed-settings.example.json` 참조본만 생성, docs/ENTERPRISE.md §2 OS 별 경로·MDM·서버 관리형 |
| R-H.PERM.2 | 관리형 설정 JSON 원문 | COVERED | templates/config/managed-settings.json 이 원문과 동일 |
| R-H.PERM.3 | permissions.deny 와 설명 | COVERED | managed-settings.json:3-6, docs/ENTERPRISE.md:45 |
| R-H.PERM.4 | permissions.allow 와 설명 | COVERED | managed-settings.json:7-10, ENTERPRISE.md:46 |
| R-H.PERM.5 | disableBypassPermissionsMode + allowManagedPermissionRulesOnly | COVERED | managed-settings.json:11-13, ENTERPRISE.md:47 |
| R-H.PERM.6 | sandbox.network.allowedDomains | COVERED | managed-settings.json:18, ENTERPRISE.md:48, templates/config/settings.sandbox.json |
| R-H.PERM.7 | failIfUnavailable + allowUnsandboxedCommands | COVERED | managed-settings.json:16-17, ENTERPRISE.md:49 |
| R-H.PERM.8 | sandbox.credentials | COVERED | managed-settings.json:19-25, ENTERPRISE.md:50 |
| R-H.PERM.9 | allowManagedHooksOnly | COVERED | managed-settings.json:27, ENTERPRISE.md:51 |
| R-H.PERM.10 | disableSideloadFlags + strictKnownMarketplaces, 마켓플레이스 배포 설명 | COVERED | managed-settings.json:28,30-32, ENTERPRISE.md:52,69-73, README.md:67-71 |
| R-H.PERM.11 | allowManagedMcpServersOnly | COVERED | managed-settings.json:29, ENTERPRISE.md:53 |
| R-H.PERM.12 | requiredMinimumVersion | COVERED | managed-settings.json:33, ENTERPRISE.md:54 (fail-open 주의 포함) |
| R-H.PERM.13 | "복사 말고 맞춰 쓰라" 당부 | COVERED | ENTERPRISE.md:56 |
| R-H.PERM.14 | settings 레퍼런스 링크 | COVERED | ENTERPRISE.md:116 |
| R-H.PERM.15 | allowManagedHooksOnly 환경에서 플러그인 훅 배포 고려 | COVERED | ENTERPRISE.md:51 주의, README.md:420 |
| R-H.PERM.16 | 병렬 세션용 permissions.allow 튜닝 | COVERED | templates/config/settings.project.json, scripts/init.sh:229-242 (감지한 build/test/lint 를 allow 로) |

## 7. 횡단 — MCP (R-H.MCP)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.MCP.1 | 비엔지니어가 claude.ai/Cowork + GitHub 커넥터로 커밋 | EXTERNAL-DOCUMENTED | templates/en/intent-README.md:11-12, skills/intent/SKILL.md:65-66 |
| R-H.MCP.2 | 레거시 원천일 때 MCP 로 결과 되쓰기 | COVERED | artifact-conventions.md §7 |
| R-H.MCP.3 | UI 작업용 브라우저·스크린샷 MCP | COVERED | skills/verify/SKILL.md:39-44 (Chrome MCP·Playwright MCP·스크립트), README.md:127 |
| R-H.MCP.4 | 배포를 환경별 MCP 도구로 | COVERED | templates/config/mcp.deploy.example.json (production 은 status·rollback 만), docs/ENTERPRISE.md §5, skills/release/SKILL.md:18-19,60 |
| R-H.MCP.5 | allowManagedMcpServersOnly + Managed MCP 문서 | COVERED | managed-settings.json:29, ENTERPRISE.md:53,100 |
| R-H.MCP.6 | MCP 로 지표 복귀 확인, 티켓 태그 트리아지 | EXTERNAL-DOCUMENTED | skills/postmortem/SKILL.md:23, skills/intent/SKILL.md:25-27 (`--from-ticket` 이 커넥터로 레코드 읽기) |

## 8. 횡단 — OpenTelemetry (R-H.OTEL)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-H.OTEL.1 | 세션 전사본 OTel 내보내기 안내 | COVERED | docs/ENTERPRISE.md §4, templates/config/otel.env, README.md:375 |
| R-H.OTEL.2 | 동시 세션 수는 OTel 에서 | COVERED | README.md:366, scripts/metrics.py:353 (로컬 대리값 + OTel 안내) |
| R-H.OTEL.3 | 훅 결정을 타임스탬프·allow/block 으로 기록 | COVERED | scripts/lib/hooks-extra.sh:181-188 gate_log, scripts/hooks/guard-bash.sh:59-70, ENTERPRISE.md:93 |
| R-H.OTEL.4 | 스킬 호출은 세션 추적에 기록 | COVERED | ENTERPRISE.md:94 (`OTEL_LOG_TOOL_DETAILS`), skills/policy/SKILL.md:28 |
| R-H.OTEL.5 | 세션 행위는 엔지니어에게 귀속 | COVERED | ENTERPRISE.md §4·§7, README.md:366 |
| R-H.OTEL.6 | 호출·발견·트리아지 결정을 타임스탬프로 기록 | COVERED | scripts/monitor.py:174-189 `Log.event`, skills/triage/SKILL.md:37 |

## 9. Stage 1 — Plan: Capture as intent.md (R-1.1)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-1.1.1 | 단계 요약 | COVERED | skills/intent/SKILL.md:3,7, README.md:117 |
| R-1.1.2 | 진입 경로 3종(아이디어·티켓·인시던트) | COVERED | skills/intent/SKILL.md:10-11, 플래그 `--from-ticket`/`--from-incident`/`--from-scan` |
| R-1.1.3 | 브레인스토밍 → proto-spec → intent.md | COVERED | skills/intent/SKILL.md:30-43 |
| R-1.1.4 | PO 가 커밋 전 검토·교정(에이전트 발원 포함) | COVERED | skills/intent/SKILL.md:44-45,61-64, scripts/monitor.py 는 `status: draft` 로만 작성 |
| R-1.1.5 | 전통 vs AI-native | COVERED | skills/intent/SKILL.md:9-11 What changes — 전통 열 "백로그 항목·유저 스토리·스토리 포인트·리파인먼트 회의를 거치며 핸드오프마다 소유가 옮겨간다" |
| R-1.1.6 | 무엇·왜·제약 | COVERED | templates/en/intent.md:16-32 |
| R-1.1.7 | Prerequisites: None | COVERED | skills/intent/SKILL.md:12 |
| R-1.1.8 | 비엔지니어용 Claude 접근 | EXTERNAL-DOCUMENTED | intent-README.md:11-12 |
| R-1.1.9 | 합의된 intent 템플릿 | COVERED | templates/en|ko/intent.md → `intent/TEMPLATE.md` (scripts/init.sh:153) |
| R-1.1.10 | PO 가 지켜보는 버전 관리 홈 | COVERED | scripts/init.sh:152-154 (`intent/`, README, `triage/`) |
| R-1.1.11 | 기본 `intent/`, 대안(전용 저장소·모노레포) | COVERED | templates/en/intent-README.md:11(ko:19) "단일 제품은 제품 저장소의 intent/ 가 가장 단순 … 전용 intent 저장소는 intent 가 여러 저장소에 걸칠 때만 … 모노레포에서는 디렉터리 하나" |
| R-1.1.12 | 일회성 설정, 쓰기 권한 결정 | COVERED | skills/init/SKILL.md:78 Governance "who may write to it (teams, connector accounts) … expressed as repository permissions and the `intent/` line in CODEOWNERS", templates/en|ko/intent-README.md 같은 문단 |
| R-1.1.13 | 커넥터로 대리 커밋 | EXTERNAL-DOCUMENTED | intent-README.md:11-12 |
| R-1.1.14 | 발안자 자유 서술(못 하는 것·영향·더 나은 상태·범위 밖) | COVERED | skills/intent/SKILL.md:30-38 |
| R-1.1.15 | 분석가 질문(범위·사용자·제약·성공) | COVERED | skills/intent/SKILL.md:30-37 |
| R-1.1.16 | 템플릿 절 작성, 템플릿 소유·승인 | COVERED | templates/en/intent-README.md:7 "a technical team member writes it, a lead signs it off, and changes go through PR review"(ko 동일) |
| R-1.1.17 | 발안자 교정 | COVERED | skills/intent/SKILL.md:44-45 |
| R-1.1.18 | 커밋(작성자·시각) | COVERED | skills/intent/SKILL.md:46-48, artifact-conventions §6 |
| R-1.1.19 | `# Intent: <title>` / `Author: <name> (<team>). Status: draft.` | COVERED | templates/en|ko/intent.md:14 `Author: {{author}} ({{team}}). Status: draft.`, artifact-conventions.md:109 가 `{{team}}` 확정 규칙(팀을 묻거나 `git config user.team`, 모르면 괄호 삭제)을 규정 |
| R-1.1.20 | `## Problem` | COVERED | intent.md:16 |
| R-1.1.21 | `## Proposed outcome` | COVERED | intent.md:19 |
| R-1.1.22 | `## Affected users and systems` | COVERED | intent.md:22 |
| R-1.1.23 | `## Constraints` | COVERED | intent.md:25 |
| R-1.1.24 | `## Open questions` | COVERED | intent.md:31 |
| R-1.1.25 | 원문 예시(claims status self-service) 파일 | COVERED | templates/examples/intent.md 이 원문 블록과 문자 단위로 일치(J. Ortiz claims status self-service 전문). `/sdlc:init` 이 `.sdlc/examples/` 로 복사(scripts/init.sh:162-166), templates/examples/README.md 가 일반화한 템플릿과 대응 |
| R-1.1.26 | 증거 = 커밋된 intent.md 의 git 이력 | COVERED | artifact-conventions §6, skills/intent/SKILL.md:62-64 |
| R-1.1.27 | 수락/기각 = 머지 또는 종결 리뷰 | COVERED | skills/intent/SKILL.md:49-55 (`approve`/`reject` 커밋 또는 PR 머지) |
| R-1.1.28 | 선행: 첫 대화 → 커밋 시간 | COVERED | scripts/metrics.py:312 |
| R-1.1.29 | 후행: 생존율 | COVERED | scripts/metrics.py:314 |
| R-1.1.30 | 후행: 첫 spec 커밋 이후 intent 변경 수 | COVERED | scripts/metrics.py:308-315 |

## 10. Stage 2 — Design: Requirements and design (R-2.1)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-2.1.1 | 단계 요약 | COVERED | skills/spec/SKILL.md:7,68-69 |
| R-2.1.2 | 승인된 intent → 정책 스킬 제약 하에 spec | COVERED | skills/spec/SKILL.md:22-30 |
| R-2.1.3 | PO 는 검토만, 우려 사항 플래그 | COVERED | templates/en/spec.md:30-34 Flagged concerns, spec:40-44 |
| R-2.1.4 | Claude Design 목업 | EXTERNAL-DOCUMENTED | README.md:118 |
| R-2.1.5 | 전통 vs AI-native | COVERED | skills/spec/SKILL.md:9-11 What changes — 전통 열 "분석가가 요구사항으로 형식화하고 디자이너가 다시 해석 … 느리고 손실이 있다" |
| R-2.1.6 | 전제: intent + 정책 스킬, 스킬 사전 점검 | COVERED | spec:22-30 (승인 확인, 정책 로드 보고, 비어 있으면 `/sdlc:policy` 제안) |
| R-2.1.7 | PO 만으로 가능, 엔지니어링 기술 불필요 | COVERED | README.md:149 승인자, templates/github/sdlc-spec-on-intent.yml:4 |
| R-2.1.8 | intent 파일을 입력으로 | COVERED | spec:15-16 |
| R-2.1.9 | 슬래시 명령으로 코드화 | COVERED | `/sdlc:spec` 자체, 프롬프트 spec:31-36 |
| R-2.1.10 | intent 머지 트리거 → 비대화형 → spec PR | COVERED | templates/github/sdlc-spec-on-intent.yml (기본 브랜치 push, `status: approved` 전이 탐지, `claude -p`, PR) |
| R-2.1.11 | 검토 질문 2개 | COVERED | spec:48-49 |
| R-2.1.12 | 우려 사항 → 정책 오너 | COVERED | templates/en/spec.md:33-34 Owner 열, spec:40-44,50 |
| R-2.1.13 | spec 을 intent 옆에 커밋 | COVERED | spec:52-53 |
| R-2.1.14 | 사람이 진행 결정, 고위험은 기술 리드 | COVERED | spec:51,58-60,67 |
| R-2.1.15 | 원문 프롬프트 | COVERED | spec:32-36 원문 그대로, sdlc-spec-on-intent.yml:41 은 경로 삽입 변형 |
| R-2.1.16 | 정책을 작성 중 제약으로 | COVERED | spec:26-29 |
| R-2.1.17 | spec·프롬프트·스킬 버전 로그 | COVERED | spec:29,46 `policies_applied: [<name>@<hash>]`, 프롬프트는 버전 관리되는 스킬 본문 |
| R-2.1.18 | PO 서명, 우려 사항 라우팅 | COVERED | spec:50,67 |
| R-2.1.19 | 선행: intent 커밋 → spec 커밋 | COVERED | scripts/metrics.py:326 |
| R-2.1.20 | 후행: plan 이후 spec 커밋 수 | COVERED | scripts/metrics.py:327, scripts/status.sh rework 열 |
| R-2.1.21 | spec.md 필수 절 | COVERED | templates/en/spec.md (Requirements·Design·Policies applied·Flagged concerns·Open questions carried·Acceptance) |

## 11. Stage 3 — Build

### 11.1 Plan mode (R-3.1)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-3.1.1 | 단계 요약 | COVERED | skills/plan/SKILL.md:7, templates/en/CLAUDE.sections.md:32, scripts/hooks/session-start.sh:64 |
| R-3.1.2 | 플랜 모드에서 spec 을 주고 인터뷰 반복 | COVERED | plan:21-38 |
| R-3.1.3 | 전통 vs AI-native | COVERED | skills/plan/SKILL.md:9-11 What changes — 전통 열 "어떻게 바꿀지는 엔지니어 머릿속이나 티켓 코멘트에 남아 아무도 리뷰할 수 없고, 리뷰어가 처음 보는 것은 완성된 diff" |
| R-3.1.4 | 전제 | COVERED | plan:11 |
| R-3.1.5 | 저장소 접근 가능한 Claude Code | COVERED | README.md 설치·빠른 시작 |
| R-3.1.6 | 플랜 모드 진입 | COVERED | plan:21-22 `EnterPlanMode` |
| R-3.1.7 | 파일·순서·테스트 | COVERED | plan:26-32 |
| R-3.1.8 | 세 가지 자문 | COVERED | plan:33-34 |
| R-3.1.9 | 완료 기준 | COVERED | plan:35-36 |
| R-3.1.10 | plan.md 커밋 | COVERED | plan:39-44 |
| R-3.1.11 | 승인 후 구현 | COVERED | plan:46 (solo 기본 autopilot 편차는 §읽는 법 첫 항목; 승인 주체는 `approved_by`·`approval_basis` 로 기록) |
| R-3.1.12 | 벗어나면 같은 커밋에 갱신, 훅 | COVERED | plan:47-49, scripts/hooks/stop-verify.sh:51-61 (`plan.enforce_sync`), post-edit.sh:20-24 |
| R-3.1.13 | `# Plan: <title> (from …)` | COVERED | templates/en/plan.md:13 (`from {{spec_path}}` — 원문은 intent 날짜) |
| R-3.1.14 | `## Files that change` | COVERED | plan.md:15 |
| R-3.1.15 | `## Order of work` | COVERED | plan.md:18 |
| R-3.1.16 | `## Risks` | COVERED | plan.md:23 |
| R-3.1.17 | `## Proof` | COVERED | plan.md:26 |
| R-3.1.18 | 원문 plan 예시 파일 | COVERED | templates/examples/plan.md 이 원문 블록과 일치(`# Plan: claims status self-service (from intent.md 2026-06-02)`, Files that change / Order of work / Risks / Proof) |
| R-3.1.19 | 코드 생성 전 설계 리뷰, 플랜 모드가 강제 | COVERED | plan:21-22,59-61 |
| R-3.1.20 | 승인자 기록, 고위험 라우팅 | COVERED | plan:59-61, plan.md `approved_by` |
| R-3.1.21 | 선행 지표 | COVERED | scripts/metrics.py:348-349 (PR 메타는 source needed) |
| R-3.1.22 | 후행 지표 | COVERED | scripts/metrics.py:349, plan 의 Departures 카운트(plan:70) |

### 11.2 Auto mode (R-3.2)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-3.2.1 | 자동 모드 설명 | COVERED | skills/plan/SKILL.md:65-66, README.md:121 |
| R-3.2.2 | 준비도 체크리스트(가드레일 4 + 작업 조건 3) | COVERED | skills/plan/SKILL.md:73-76 이 가드레일 4(튠된 CLAUDE.md · 정책 스킬 · 불안전 행위를 막는 훅 · Claude 가 돌릴 수 있는 테스트 스위트)와 작업 조건 3(좁은 spec.md · 작은 영향 범위 · 테스트가 덮는 코드)을 체크리스트로 기술. scripts/doctor.sh:197-204 가 `auto mode readiness` 행으로 네 가드레일 존재를 판정 (`/sdlc:go` 는 doctor 의 `auto_mode` 판정이 ok 일 때만 autopilot) |
| R-3.2.3 | 행위 감시 → 아티팩트 사후 검토로 이동 | COVERED | skills/plan/SKILL.md:77 "In auto mode the engineer reviews artifacts after the session (diff, verify output, plan Departures) instead of watching edits" |
| R-3.2.4 | worktree 병렬성, 닫힌 루프의 기반 | COVERED | skills/plan/SKILL.md:78 "with worktrees this is what makes individual and team parallelism — and the autonomous Stage 6 loop — possible", README.md:180 대응표 Auto mode 행 |

### 11.3 CLAUDE.md (R-3.3)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-3.3.1 | 신규 합류자 컨텍스트 4영역 | COVERED | skills/init/references/claude-md-guide.md:3-4,43-57, templates/en/CLAUDE.sections.md |
| R-3.3.2 | Prerequisites: None | COVERED | scripts/lib/scripts-extra.sh:257 1층(의존 없음) |
| R-3.3.3 | 저장소·Claude Code·코드베이스를 아는 엔지니어 | COVERED | skills/init/SKILL.md:24-27 |
| R-3.3.4 | `/init` 실행 | COVERED | skills/init/SKILL.md:43 "If CLAUDE.md did not exist, Claude Code's built-in `/init` may be run first to draft it … `/sdlc:init` then adds the five section markers on top" |
| R-3.3.5 | 하루치로 줄이기 | COVERED | claude-md-guide.md:68-72, init:53 |
| R-3.3.6 | 루트에 커밋, 코드처럼 리뷰 | COVERED | init:67-68,79-80 |
| R-3.3.7 | 두 번 틀리면 CLAUDE.md | COVERED | skills/lesson/SKILL.md, session-start.sh:66, templates/en/REVIEW.md:19-22 |
| R-3.3.8 | 한 페이지 이하 | COVERED | init:53, lesson:30-32, scripts/doctor.sh:56 |
| R-3.3.9 | 제목 줄 | COVERED | scripts/init.sh:207 |
| R-3.3.10 | `## Commands` | COVERED | CLAUDE.sections.md:2-6, claude-md-guide.md:45-46 |
| R-3.3.11 | `## Conventions` | COVERED | CLAUDE.sections.md:10-12, guide:48-50 |
| R-3.3.12 | `## Architecture` | COVERED | CLAUDE.sections.md:15-17, guide:52-54 |
| R-3.3.13 | `## Things Claude gets wrong` | COVERED | CLAUDE.sections.md:20-22, guide:56-57 |
| R-3.3.14 | 원문 "Payments service" 예시 파일 | COVERED | templates/examples/CLAUDE.md 이 원문 "Payments service" 블록과 문자 단위로 일치(Commands·Conventions·Architecture·Things Claude gets wrong 4절 전문) |
| R-3.3.15 | 코드 오너가 변경 승인 | COVERED | templates/github/CODEOWNERS:6 |
| R-3.3.16 | 선행: 반복 실수 빈도 | COVERED | scripts/metrics.py:350-351 |
| R-3.3.17 | 후행: 신규 팀원 첫 PR 머지 시간 | COVERED | scripts/metrics.py:353 `("Time to first merged PR for a new team member", None, "source needed: PR history (gh pr list --author) + join date", "lagging")` |

### 11.4 Skills (R-3.4)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-3.4.1 | 기관 지식의 운영화 | COVERED | skills/policy/SKILL.md:7 |
| R-3.4.2 | 스킬 vs CLAUDE.md 경험칙 | COVERED | policy:10 |
| R-3.4.3 | 전제 없음, CLAUDE.md 는 도움 | COVERED | skills/policy/SKILL.md:12 Prerequisites 줄이 원문 문장 그대로 |
| R-3.4.4 | 오너·기록 원천 | COVERED | policy:17-18, templates/policies/secure-api-review/SKILL.md:9 |
| R-3.4.5 | 일관되지 않은 지식 하나 선택 | COVERED | policy:10,17 |
| R-3.4.6 | frontmatter 트리거 + 본문 | COVERED | policy:18 |
| R-3.4.7 | `.claude/skills/<name>/` 또는 플러그인 | COVERED | policy:18, README.md 설치·기업 배포 |
| R-3.4.8 | 트리거 테스트 | COVERED | policy:20, evals/trigger-*/case.yaml |
| R-3.4.9 | 정책 변경 시 오너 승인 | COVERED | policy:21,27, CODEOWNERS:8 |
| R-3.4.10 | 다음 세션에 자동 반영 | COVERED | policy:21 |
| R-3.4.11 | secure-api-review frontmatter 원문 | COVERED | templates/policies/secure-api-review/SKILL.md:1-6 |
| R-3.4.12 | secure-api-review 본문 원문 | COVERED | 같은 파일 7-21 (오너 주석 한 줄 추가) |
| R-3.4.13 | 권고 통제, 결정론적 뒷받침 | COVERED | policy:22, spec:70-71, guard-edit·secret-scan 훅 |
| R-3.4.14 | 호출 로그, 오너 리뷰 | COVERED | policy:27-28 |
| R-3.4.15 | 선행: 정책 승인 → 스킬 머지 | COVERED | policy:31, README.md:365 |
| R-3.4.16 | 후행: 정책 인용 리뷰 지적 | COVERED | scripts/metrics.py:352, policy:31 |

### 11.5 Hooks as build-time guardrails (R-3.5)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-3.5.1 | 스킬은 권고, 훅은 결정론 | COVERED | policy:22, README.md:14, docs/ENTERPRISE.md 표 |
| R-3.5.2 | 보호 경로 편집 차단 | COVERED | scripts/hooks/guard-edit.sh:63-79, templates/config/frozen-paths.txt, `protect.generated_paths` |
| R-3.5.3 | 편집 후 포매터·린터 | COVERED | scripts/hooks/post-edit.sh:63 이 `commands.lint_file`(선택, `{file}` 치환)을 편집 직후 변경 파일 1개에 실행. README.md:286 설정 표에 키와 "비어 있으면 린트는 verify 단계에서만" |
| R-3.5.4 | 자격증명 diff 차단 | COVERED | guard-edit.sh:100-112, guard-bash.sh:78-91, scripts/secret-scan.sh |
| R-3.5.5 | 예외 없는 정책은 훅으로 | COVERED | policy:22, README.md:125 |
| R-3.5.6 | 빠르고 변경 파일 범위 | COVERED | post-edit.sh 단일 파일 + 시간 상한(30-41), guard-edit 는 새 텍스트만, tests/hooks.sh:449 1초 미만 |
| R-3.5.7 | 빌드 훅에 ask 없음 | COVERED | guard-edit 는 deny 만, ask 는 배포 게이트(guard-bash.sh:60-62)에만 |

### 11.6 Parallel sessions and subagents (R-3.6)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-3.6.1 | 병렬 세션 = 별도 worktree 인스턴스 | COVERED | README.md:225-227 "병렬 세션과 서브에이전트" 절 — 병렬 세션 = 자기 worktree 의 또 하나의 인스턴스로 서로를 모름, 서브에이전트 = 한 세션 안의 범위 한정 도우미 |
| R-3.6.2 | 서브에이전트 정의 | COVERED | agents/*.md, README.md:126, skills/verify/SKILL.md:45-48 |
| R-3.6.3 | 전통 vs AI-native | COVERED | README.md:229-230 전통/AI-native 두 줄(한 번에 한 작업·대기 ↔ 여러 worktree 세션·조율로 이동) |
| R-3.6.4 | 전제(CLAUDE.md, 피드백 루프) | COVERED | README.md:231 전제 줄(CLAUDE.md 실선, Feedback loop 점선) |
| R-3.6.5 | git + 권한 튜닝 | COVERED | templates/config/settings.project.json (`Bash(git worktree*)` 포함), README.md:121 |
| R-3.6.6 | plan 의 파일 목록으로 작업 분할 | COVERED | skills/plan/SKILL.md:62-66 "Parallel work" 절 — Files that change 가 겹치지 않는 집합으로 나뉘면 Output 에 그렇게 적고, 파일을 공유하는 작업은 한 세션에서 순차로. README.md:232 동일 |
| R-3.6.7 | `claude --worktree <name>` | COVERED | README.md:233 `claude --worktree feature-auth` / `claude --worktree fix-rate-limit` 예시, skills/plan/SKILL.md:64 |
| R-3.6.8 | 2~3 세션부터 | COVERED | README.md:233 "**2~3 세션이 출발점**이며 … 리뷰가 따라오는 동안만 세션을 늘린다", skills/plan/SKILL.md:65-66 |
| R-3.6.9 | verifier·simplifier·researcher 정의 | COVERED | agents/sdlc-verifier.md, sdlc-simplifier.md, sdlc-researcher.md (+reviewer, diagnoser), name·description·tools |
| R-3.6.10 | verifier.md 원문 | COVERED | templates/examples/verifier.md 이 원문과 일치(`name: verifier`, `tools: Bash, Read`, 본문 3문장). agents/sdlc-verifier.md:9 가 "원문 verifier.md 의 확장판(Grep/Glob 추가, 고정 보고 형식)"임을 명시 |
| R-3.6.11 | 통제는 저장소 설정에서, 로그 귀속 | COVERED | README.md:121, ENTERPRISE.md §4·§7 |
| R-3.6.12 | 선행: 동시 세션 수 | COVERED | scripts/metrics.py:353, README.md:366 |
| R-3.6.13 | 후행: 주당 머지 + 재작업률 | COVERED | scripts/metrics.py:354 (source needed) |

## 12. Stage 4 — Test

### 12.1 Feedback loop (R-4.1)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-4.1.1 | 단계 요약 | COVERED | skills/verify/SKILL.md:7, skills/evals/SKILL.md:7 |
| R-4.1.2 | 항상 검증 수단 | COVERED | templates/en/CLAUDE.sections.md:25-32, verify 스킬 |
| R-4.1.3 | 피드백 루프 ≠ verifier 서브에이전트 | COVERED | verify:45-48, agents/sdlc-verifier.md:7 |
| R-4.1.4 | 전통 vs AI-native | COVERED | skills/verify/SKILL.md:9-11 What changes — 전통 열 "신호가 늦게 온다: CI 는 몇 분 뒤, 테스터는 며칠 뒤, 프로덕션은 몇 주 뒤" |
| R-4.1.5 | Prerequisites: None | COVERED | verify:11 |
| R-4.1.6 | 한 명령 테스트·빌드, UI 는 MCP | COVERED | verify:11,39-42 |
| R-4.1.7 | 한 타깃으로 묶기 | COVERED | verify:14-17, claude-md-guide.md |
| R-4.1.8 | 정상 출력 예시 | COVERED | init:43-45, claude-md-guide.md:25-41 |
| R-4.1.9 | 정량 목표 | COVERED | verify:21-24 |
| R-4.1.10 | 버그: 실패 테스트 먼저, 잠금 훅 | COVERED | verify:30-38, guard-edit.sh:81-87 |
| R-4.1.11 | UI 시각 검증 루프 | COVERED | verify:39-44 |
| R-4.1.12 | 검증이 done 의 일부 | COVERED | CLAUDE.sections.md:30-31, stop-verify.sh |
| R-4.1.13 | 테스트 파일 잠금 + 리뷰 대안 | COVERED | guard-edit.sh:44-50 (플래그 파일·env·plan `test_changes`), review-rubric.md:35 |
| R-4.1.14 | 검증 블록 원문 | COVERED | CLAUDE.sections.md:25-31 (Build 줄은 프로젝트 무관하게 "must finish without errors") |
| R-4.1.15 | 두 훅 선택 제공 | COVERED | stop-verify.sh (`verify.required_before_stop`), guard-edit.sh 테스트 잠금 |
| R-4.1.16 | 증거는 도구 출력 | COVERED | verify:26-27,49-50,57-58 |
| R-4.1.17 | 전사본·OTel·PR 체크 | COVERED | ENTERPRISE.md §4, verify:57 |
| R-4.1.18 | 코드 오너 승인 | COVERED | verify:57-58 |
| R-4.1.19 | 선행: 첫 패스 CI 성공률 | COVERED | scripts/metrics.py:367-369 |
| R-4.1.20 | 후행: PR 리뷰 시간, 변경 실패율 | COVERED | scripts/metrics.py:373 |

### 12.2 Continuous evals (R-4.2)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-4.2.1 | 설정 변경 시 회귀 스위트 | COVERED | evals:7-12, templates/en/evals-README.md |
| R-4.2.2 | 살아있는 스위트 | COVERED | evals:51-54, skills/evals/references/case-format.md:25 |
| R-4.2.3 | 정해진 주기로만 실행하는 선택지 | COVERED | templates/github/sdlc-evals.yml:6 "Cadence: to run only on a schedule instead of on every configuration change, delete the `pull_request:` block and keep `schedule`", skills/evals/SKILL.md:15 같은 문장 |
| R-4.2.4 | 전제 | COVERED | evals:11-12 |
| R-4.2.5 | 비대화형 CI + API 예산 | COVERED | templates/github/sdlc-evals.yml, README.md:410 |
| R-4.2.6 | 실제 과제 20~50건 수집 | COVERED | skills/evals/SKILL.md:25 0단계 "The platform engineer collects 20 to 50 real tasks from recent work together with their expected/accepted outcome", scripts-extra.sh:281 채택 안내에도 동일 |
| R-4.2.7 | 프롬프트 + 검사 형식과 예시 | COVERED | case-format.md, templates/evals/cases/ 3건 (형식은 원문의 `evals/*.json` 대신 디렉터리) |
| R-4.2.8 | 스케줄 + CLAUDE.md/.claude 변경 트리거 | COVERED | sdlc-evals.yml:8-11 |
| R-4.2.9 | 결과로 설정 변경 게이트 | COVERED | sdlc-evals.yml:5, scripts/run-evals.sh:168 (임계치 미달 exit 1), evals:48-49 |
| R-4.2.10 | 인시던트마다 eval | COVERED | evals `add --from incident`, case-format.md:68-74, postmortem:21 |
| R-4.2.11 | agent-evals.yml 원문 | COVERED | templates/examples/agent-evals.yml 이 원문 워크플로 전문(루프 본문 `for eval in evals/*.json … ./evals/check.sh` 포함)과 일치. templates/github/sdlc-evals.yml:7-9 헤더가 루프를 `run-evals.sh` 로 대체한 사실과 이유를 명시 |
| R-4.2.12 | `evals/check.sh <eval.json> result.json` | PARTIAL | 차이가 명시됨 — templates/en|ko/evals-README.md:18 이 원문의 스위트 수준 `evals/check.sh <eval.json> result.json` 과 플러그인의 케이스별 `check.sh <workdir>` 차이·이유(언어 무관 fixture, 훅이 실제로 발동하는 임시 저장소)를 적고 원문은 `.sdlc/examples/agent-evals.yml` 에 보존. 남은 것: 원문 시그니처를 받는 스위트 수준 어댑터 자체는 제공하지 않음(의도된 편차) |
| R-4.2.13 | 임계치 머지 체크, 실행 로그, 승인자 | COVERED | sdlc-evals.yml:5, run-evals.sh results.json·summary.md·events, evals:61-62 |
| R-4.2.14 | 선행: 통과율, 인시던트 → eval 시간 | COVERED | run-evals.sh, scripts/metrics.py:370,372 |
| R-4.2.15 | 후행: CI 회귀 vs 운영 회귀 | COVERED | scripts/metrics.py:374 (source needed) |

## 13. Stage 5 — Deploy

### 13.1 PR review loop (R-5.1)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-5.1.1 | 단계 요약 | COVERED | skills/release/SKILL.md:8, README.md:130 |
| R-5.1.2 | 리뷰를 주고 받음 | COVERED | skills/review/SKILL.md, sdlc-review.yml `address-comment` 잡, review `--pr` |
| R-5.1.3 | 전통 vs AI-native | COVERED | skills/review/SKILL.md:9-11 What changes — 전통 열 "리뷰 용량은 사람 산출량에 맞춰 계획됐다 … 저자가 쫓아다니는 동안 백로그가 쌓인다" |
| R-5.1.4 | 전제 | COVERED | skills/review/SKILL.md:19-20 Prerequisites·Infrastructure — 갱신된 CLAUDE.md·정책 스킬·정의된 서브에이전트 + 그림 간선 |
| R-5.1.5 | 관리형 Code Review 또는 claude-code-action, 브랜치 보호 | COVERED | sdlc-review.yml:5-6, CODEOWNERS:1-4, README.md:353 |
| R-5.1.6 | 관리형이 가장 빠른 시작 | COVERED | sdlc-review.yml:6, README.md:129,418 |
| R-5.1.7 | REVIEW.md 루트 배치, 패스·Important/Nit·제외 | COVERED | templates/en|ko/REVIEW.md, scripts/init.sh:158-163 |
| R-5.1.8 | 사람 임계치, 기계가 읽는 집계 | COVERED | scripts/review-gate.sh 가 `SDLC_REVIEW_TALLY {…}` 줄을 파싱해 important > `--max-important`(기본 0) 이면 exit 1, 집계 줄이 없으면 exit 2(미리뷰 취급). templates/github/sdlc-review.yml:42-47 에 주석 처리된 선택 머지 게이트 스텝, README.md:188 대응표 |
| R-5.1.9 | @claude 수정 루프 | COVERED | sdlc-review.yml:42-53, review:66-67 |
| R-5.1.10 | PR babysit 명령 | COVERED | review:45-49, review-rubric.md:47-65 |
| R-5.1.11 | 지적 → CLAUDE.md, CLAUDE.md 낡음 경고 | COVERED | REVIEW.md:19-22, review:39-42, sdlc-reviewer.md:19 |
| R-5.1.12 | 월간 튠 | COVERED | review-rubric.md:67-71, review:64-65 |
| R-5.1.13 | REVIEW.md 원문 4절 | COVERED | templates/en/REVIEW.md:1-17 (Security·Compliance 줄에 항목 추가, 생성 경로는 플레이스홀더) |
| R-5.1.14 | 직무 분리, PR = 감사 기록 | COVERED | review:59-63, REVIEW.md:24-26, README.md:419 |
| R-5.1.15 | "securing an AI-native SDLC at Anthropic" 참조 | COVERED | README.md:546 "더 읽기" 절 — *Securing an AI-native SDLC at Anthropic* (원문 Stage 5 참조) |
| R-5.1.16 | 선행 지표 | COVERED | scripts/metrics.py:386-389 |
| R-5.1.17 | 후행 지표 | COVERED | scripts/metrics.py:393 |

### 13.2 Hooks as approval gates (R-5.2)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-5.2.1 | ask 가능한 훅 | COVERED | guard-bash.sh:60-62, release:28-29, README.md:178 |
| R-5.2.2 | 마이그레이션/인프라 변경 티켓 가드, 테스트 잠금 | COVERED | scripts/hooks/guard-edit.sh:81-88 이 `protect.ticketed_paths` 경로 편집을 `protect.ticket_env`(기본 `CHANGE_TICKET`) 없이는 exit 2 로 차단하고 Why/To proceed 를 함께 출력(실측: 티켓 없으면 rc=2, `CHANGE_TICKET=…` 이면 rc=0). templates/config/config.json `protect.ticketed_paths`·`ticket_env`, tests/hooks.sh:125-140 5건 |
| R-5.2.3 | Prerequisites: None | COVERED | scripts-extra.sh:277-279 1층 hook gate list |
| R-5.2.4 | 필요한 승인 목록(등록부) | COVERED | templates/en|ko/APPROVALS.md — 게이트 · 무엇이 승인인가 · 승인자 · 강제 수단 · 로그 위치 5열 등록부(기본 5행). scripts/init.sh:159 이 `.sdlc/APPROVALS.md` 로 생성, README.md:243 이 훅 절에서 참조 |
| R-5.2.5 | 변경관리 승인·릴리스 인가·보호 경로 | COVERED | templates/en|ko/APPROVALS.md 기본 행이 변경관리 승인(CHANGE_TICKET) · 릴리스 인가(RELEASE_APPROVAL) · 보호 경로(frozen/generated) · PR 코드 승인 · staging 확인을 이름 붙여 나열 |
| R-5.2.6 | 훅이 allow/ask/block | COVERED | guard-bash.sh:57-73 |
| R-5.2.7 | 팀 훅은 settings.json, 필수 훅은 관리형 | COVERED | README.md:242-243 3층 설명 — 플러그인 훅 = 팀 공통(플러그인 버전에 고정) · 프로젝트 고유 훅은 `.claude/settings.json`(원문 예시 `.sdlc/examples/settings.hooks.json`·`production-gate.sh`) · 타협 불가 훅은 관리형 설정 |
| R-5.2.8 | 차단은 이유·승인 경로 설명 | COVERED | guard-edit.sh:70,77,85,95,109, guard-bash.sh:71,88 (`Why: … To proceed: …`) |
| R-5.2.9 | settings.json 훅 등록 원문 | COVERED | templates/examples/settings.hooks.json 이 원문 블록과 일치(PreToolUse Bash matcher → `${CLAUDE_PROJECT_DIR}/.claude/hooks/production-gate.sh`), templates/examples/production-gate.sh 도 원문 전문 + 실행 비트 |
| R-5.2.10 | production-gate.sh 의미 | COVERED | guard-bash.sh:14,45-73 — `tool_input.command` 읽기, production 패턴, 승인 env 비어 있으면 stderr "Production deploys need a release authorization." + exit 2, 아니면 exit 0 |
| R-5.2.11 | 게이트 조건 매번 강제, 타임스탬프 로그, 승인 정의 | COVERED | hooks-extra.sh gate_log, guard-bash.sh 59-70, release:31-40 |
| R-5.2.12 | 관리형 설정 예시 상호 참조 | COVERED | README.md:131 → docs/ENTERPRISE.md |
| R-5.2.13 | 선행: 게이트 대기 시간 | COVERED | README.md:370, scripts/metrics.py:390-391 |
| R-5.2.14 | 후행: 운영 도달 위반 | COVERED | scripts/metrics.py:393 |

### 13.3 CI/CD integration and deployment (R-5.3)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-5.3.1 | 비대화형·샌드박스·MCP 배포·롤백 리허설 | COVERED | README.md:132,353, release:60-61, 템플릿 7종 |
| R-5.3.2 | 전통 vs AI-native | COVERED | skills/release/SKILL.md:10-12 What changes — 전통 열 "파이프라인은 결정론적 스크립트를 돌리고 판단이 필요한 것은 사람을 기다린다 … 배포와 롤백은 사람이 압박 속에 따르는 런북" |
| R-5.3.3 | 전제 | COVERED | release:11-12 |
| R-5.3.4 | 인프라 4항목 | COVERED | README.md:341-353, mcp.deploy.example.json, release:61 |
| R-5.3.5 | 읽기 전용 판단 스텝 3종 | COVERED | templates/github/sdlc-ci-triage.yml — 실패 빌드 triage(30-42, 원문 프롬프트) + 주석 처리된 선택 잡 2종: `flaky-check`(44-52, 실패 잡 1회 재실행 후 비교) 와 `changelog`(53-63, 태그 푸시 시 `git log` 로 초안 → PR). 헤더 6-7 이 두 잡을 안내 |
| R-5.3.6 | 쓰기 스텝은 PR 로, main 직접 푸시 없음 | COVERED | templates/github/sdlc-monitor.yml:31-44 가 `peter-evans/create-pull-request@v6`(branch `sdlc/monitor-<run_id>`, add-paths `.sdlc/history/**` `{{intent_dir}}/triage/**`)로 교체 — 기본 브랜치 직접 푸시 없음(14행에 명시). sdlc-review.yml·sdlc-spec-on-intent.yml 도 PR 경로 |
| R-5.3.7 | 컨테이너·단기 토큰·운영 자격증명 없음 | COVERED | release:61, 워크플로는 `GITHUB_TOKEN` 만, ENTERPRISE.md §3 |
| R-5.3.8 | MCP 배포 도구 | COVERED | mcp.deploy.example.json, ENTERPRISE.md §5 |
| R-5.3.9 | 환경별 자율도 티어 + 훅 | COVERED | config.json:60-85, guard-bash.sh, release 스킬 |
| R-5.3.10 | 단일 명령 롤백, 스테이징 리허설 | COVERED | `commands.rollback`, guard-bash.sh:19-29 항상 허용, release:28-30, bands.json runbook |
| R-5.3.11 | triage 스텝 원문 | COVERED | sdlc-ci-triage.yml:26-32 (스텝 이름·프롬프트 원문, `if: failure()` 는 workflow_run 결론 조건으로 표현) |
| R-5.3.12 | 운영 게이트까지만 | COVERED | release:8,58-59 |
| R-5.3.13 | 브랜치 보호 | COVERED | CODEOWNERS:1-4, README.md:353,419 |
| R-5.3.14 | 운영 훅 + 에이전트 자기 신원 | COVERED | README.md:454 "비대화형 실행은 에이전트 자기 신원으로 행동합니다 — 코멘트는 github-actions[bot]/claude-code-action 앱, 커밋은 sdlc-spec·sdlc-monitor 커미터". sdlc-spec-on-intent.yml:50-51 · sdlc-monitor.yml:37-38 이 `committer`/`author` 를 명시 |
| R-5.3.15 | 환경별 권한 티어 | COVERED | config.json environments, bands tools, 워크플로별 `--allowedTools` |
| R-5.3.16 | 선행: 사람 호출 없는 triage 비율 | COVERED | scripts/metrics.py:392 |
| R-5.3.17 | 후행: DORA | COVERED | scripts/metrics.py:393, README.md:371 |

## 14. Stage 6 — Maintain

### 14.1 Closing the loop (R-6.1)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-6.1.1 | 단계 요약 | COVERED | skills/monitor/SKILL.md:8, README.md:133 |
| R-6.1.2 | 헤드리스 체인과 단계 간 신뢰 게이트 | COVERED | README.md:458-471 "닫힌 루프" 절이 현재 체인과 신뢰 게이트 2곳(사람 트리아지 · PR 리뷰 + 코드 오너 승인)을 명시하고, plan/build 를 헤드리스로 이으려면 `sdlc-spec-on-intent.yml` 을 본떠 워크플로를 추가하고 단계 사이에 결정론 검사 또는 적대적 리뷰 에이전트(`sdlc-reviewer`)를 두라는 절차를 기술 |
| R-6.1.3 | 전통 vs AI-native | COVERED | skills/monitor/SKILL.md:10-12 What changes — 전통 열 "유지보수는 반응적 단계다. 3시에 알람이 울리고 놓칠 수 있으며, 티켓은 누군가 집기까지 백로그에 앉아 있다" |
| R-6.1.4 | 결정론 탐지 스크립트 | COVERED | scripts/monitor.py |
| R-6.1.5 | 전제 | COVERED | skills/monitor/SKILL.md:21 Prerequisites 줄이 원문 4항 그대로 |
| R-6.1.6 | 메트릭 스토어·비대화형 실행·Agent SDK | COVERED | skills/monitor/SKILL.md:22 Infrastructure — 메트릭 스토어(Prometheus·CI API) · 저장소 읽기 · CI 의 비대화형 실행 "or the Agent SDK for a service that receives webhooks" · 트리거 층 4종(GitHub/GitLab 스케줄, 모니터링 스택 웹훅, 네트워크 안 cron, 샌드박스 컨테이너의 Agent SDK 서비스) "in every case monitor.py runs stateless" |
| R-6.1.7 | 지표 예시 3종 | COVERED | templates/config/bands.json 에 `post_deploy_5xx_rate`(Prometheus curl 관측 명령, 3σ routes `runbook:rollback-deploy`)와 `pr_cycle_time_hours`(`gh pr list … --jq`, 3σ routes `report`)가 `"enabled": false` 예시로 포함. scripts/monitor.py:343-344 가 `enabled: false` 를 건너뛰고 그 사실을 출력 |
| R-6.1.8 | 평균·σ·WE 규칙, 단위 테스트, 모델 없음 | COVERED | monitor.py:28-73, `--selftest`, tests/scripts.sh:133-142 |
| R-6.1.9 | 1σ log / 2σ 읽기 전용 / 3σ PR·런북만 | COVERED | bands.json tiers, monitor.py:375-405 (`--disallowedTools Edit,Write…`, routes 는 pull_request·runbook 만) |
| R-6.1.10 | 스케줄 워크플로 + 웹훅·cron·Agent SDK 문서 | COVERED | templates/github/sdlc-monitor.yml(스케줄) + skills/monitor/SKILL.md:22 가 웹훅·네트워크 내 cron·Agent SDK 서비스를 트리거 층으로 명시 |
| R-6.1.11 | Stage 1 형식의 intent | COVERED | monitor.py:220-244 DIAGNOSE_PROMPT 5개 제목, `write_triage_intent` |
| R-6.1.12 | 트리아지: fix/schedule/dismiss, 기각이 밴드 튠 | COVERED | skills/triage/SKILL.md:22-26 |
| R-6.1.13 | 수정 후 eval 추가 | COVERED | triage:28, postmortem:21 |
| R-6.1.14 | bands.yaml 원문 | COVERED | templates/config/bands.json:4-15 (JSON 판, 2σ tools 는 상위집합) |
| R-6.1.15 | 티어 강제, 로그, 사전 승인 런북 | COVERED | monitor.py:299-306 (`runbooks` 에 있는 것만 실행), Log 타임스탬프, monitor:30 |
| R-6.1.16 | 선행: 위반 → 트리아지 intent 시간, 로그에 위반 시각·티어 | COVERED | monitor.py:373 `band.breach` decision=tier, scripts/metrics.py:415 |
| R-6.1.17 | 후행 지표 | COVERED | scripts/metrics.py:417-418 |
| R-6.1.18 | 예시 1: flaky 격리·revert PR | COVERED | README.md:473-476 이 원문 예시 3개를 그대로 나열하고 `.sdlc/bands.json` 의 어느 항목이 그것인지 짚음(CI 실패율 3σ → flaky 격리 또는 revert PR, 리뷰 게이트가 결정) |
| R-6.1.19 | 예시 2: 5xx + 배포 → 롤백 | COVERED | release:46-48, bands.json `runbook:rollback-deploy` |
| R-6.1.20 | 예시 3: PR cycle time → 리더십 보고 | COVERED | bands.json `pr_cycle_time_hours` 의 3σ `routes: ["report"]` + scripts/monitor.py:406-414 `report` 라우트가 `.sdlc/reports/<ts>-<metric>.md`(값·기준선 평균·시그마·진단 본문)를 쓰고 `monitor.report` 이벤트를 남김. README.md:474-476 이 "엔지니어링 리더십용 보고서"로 설명 |
| R-6.1.21 | 탐지는 결정론, 티어가 권한 결정 | COVERED | monitor.py 구조, monitor:21 |

### 14.2 Recurring codebase scans (R-6.2)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-6.2.1 | 스캔은 시점 진술, 스케줄 실행 | COVERED | skills/scan/SKILL.md:8,23, sdlc-scan.yml:1-4 |
| R-6.2.2 | Claude Security 제품 | EXTERNAL-DOCUMENTED | scan:23 |
| R-6.2.3 | 전통 vs AI-native | COVERED | skills/scan/SKILL.md:10-12 What changes — 전통 열 "보안 스캔은 릴리스나 감사 전에 실행하는 이벤트다 … 그 사이에 쓰인 코드는 PR 리뷰가 잡은 것으로만 덮인다" |
| R-6.2.4 | 전제 | COVERED | skills/scan/SKILL.md:21 Prerequisites — PR 리뷰 게이트와 승인 게이트 훅(Stage 5), 한 PR 보다 큰 발견을 위한 intent.md 형식(Stage 1) |
| R-6.2.5 | 호스팅 전제 6개 + 과금 | COVERED | skills/scan/SKILL.md:22 와 34-41 6단계 체크리스트 — Anthropic GitHub App(cloud github.com) · Claude Code on the Web 활성 · Extra Usage + 지출 한도 · 스캔 실행자 프리미엄 시트 · claude.ai/admin-settings/claude-code 관리자 토글 · Mythos 5 단가 소비 기반 과금(저장소 수·크기에 맞춘 한도) |
| R-6.2.6 | 저장소 연결·프로젝트 조직 | COVERED | skills/scan/SKILL.md:36 "connect the repositories and organize them into projects by repo, service or team so ownership of findings is clear from the start" |
| R-6.2.7 | 첫 스캔 = 기준선 | COVERED | scan:23, sdlc-scan.yml:3-4 |
| R-6.2.8 | 주간 스케줄, 범위 지정 | COVERED | scan:11,14, sdlc-scan.yml:7-8 |
| R-6.2.9 | 신뢰도로 트리아지, 사유 있는 기각 | COVERED | scan:19,21 |
| R-6.2.10 | 패치는 PR 게이트, 제안자는 승인 불가 | COVERED | scan:21,23,29 |
| R-6.2.11 | 큰 발견은 intent | COVERED | scan:21, skills/intent `--from-scan` |
| R-6.2.12 | 취약점 클래스 eval | COVERED | scan:22 |
| R-6.2.13 | CSV/Markdown/웹훅으로 기존 트래커 유지 | COVERED | skills/scan/SKILL.md:40 "export findings as CSV/Markdown or use webhooks so the existing tracker and audit systems stay the system of record (locally, the `--report` Markdown plays that role)" |
| R-6.2.14 | 중앙 관리, 검증·신뢰도·기각 사유 | COVERED | scan:19,21,23,29 |
| R-6.2.15 | 결정론 검사는 CI, 모델 스캔은 문맥 취약점 | COVERED | scan:23,29 |
| R-6.2.16 | 선행 지표 | COVERED | scripts/metrics.py:419-420 |
| R-6.2.17 | 후행 지표 | COVERED | scripts/metrics.py:419, scan:32 |

### 14.3 Claude on call with Claude Tag (R-6.3)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-6.3.1 | Slack/Teams 로 들어오는 인시던트 | COVERED | skills/postmortem/SKILL.md:18, README.md:135 |
| R-6.3.2 | Claude Tag 제품 | EXTERNAL-DOCUMENTED | README.md:135,418, postmortem:23 ("자기 신원의 첫 대응자" 설명은 없음) |
| R-6.3.3 | 채널에서 누구나 지휘·가설 검증 | COVERED | skills/postmortem/SKILL.md:31 "anyone in the channel can guide and action the response and test hypotheses in real time, and the channel history adds to the auditability" |
| R-6.3.4 | MCP 로 기준선 복귀 확인, 버전 관리 lessons 파일 | COVERED | postmortem:23, `.sdlc/LESSONS.md` (`paths.lessons`) |
| R-6.3.5 | 티켓 태그도 같은 방식, 작은 수정은 PR, 큰 것은 intent | COVERED | postmortem:22, scan:21, intent `--from-ticket`/`--from-incident` |
| R-6.3.6 | "Claude Tag on-call for CI/CD" 참조 | COVERED | README.md:547 "더 읽기" 절 — *How Claude Tag runs on-call for CI/CD at Anthropic* (원문 Stage 6 참조) |
| R-6.3.7 | 채널 = 감사 추적 | COVERED | postmortem:23 |
| R-6.3.8 | 사람 인가 후 운영 행위 | COVERED | release:31-44 (인가 없이는 진행 금지, 롤백은 사용자 지시로), postmortem:23 |

## 15. Resources (R-X)

| R-ID | 요구 | 판정 | 구성요소 / 근거 |
|---|---|---|---|
| R-X.0 | 출처 표기 | COVERED | README.md:3 "Louis Claxton, 2026-08-21 (acknowledgments: Jim Blackhurst, Will Steuk, Jamal Arif)" — 제목·URL·날짜·저자·감사. 마켓플레이스 README.md:3 은 제목·URL·날짜까지 |
| R-X.1 | 롤아웃 순서 유지 | COVERED | docs/ENTERPRISE.md §8 |
| R-X.2 | admin-setup | COVERED | ENTERPRISE.md:115 |
| R-X.3 | settings | COVERED | ENTERPRISE.md:116 |
| R-X.4 | server-managed-settings | COVERED | ENTERPRISE.md:117 |
| R-X.5 | permissions | COVERED | ENTERPRISE.md:118 |
| R-X.6 | sandboxing | COVERED | ENTERPRISE.md:119 |
| R-X.7 | hooks-guide | COVERED | ENTERPRISE.md:120 |
| R-X.8 | hooks | COVERED | ENTERPRISE.md:120 |
| R-X.9 | skills | COVERED | ENTERPRISE.md:121 |
| R-X.10 | plugin-marketplaces | COVERED | ENTERPRISE.md:122 |
| R-X.11 | managed-mcp | COVERED | ENTERPRISE.md:123 |
| R-X.12 | third-party-integrations | COVERED | ENTERPRISE.md:124 |
| R-X.13 | network-config | COVERED | ENTERPRISE.md:125 |
| R-X.14 | monitoring-usage | COVERED | ENTERPRISE.md:126 |
| R-X.15 | analytics | COVERED | ENTERPRISE.md:126 |
| R-X.16 | compliance-api | COVERED | ENTERPRISE.md:127 |
| R-X.17 | security | COVERED | ENTERPRISE.md:128 |

---

## 판정 집계

| 구간 | 행 수 | COVERED | PARTIAL | MISSING | EXTERNAL-DOCUMENTED | NOT-APPLICABLE |
|---|---|---|---|---|---|---|
| 횡단 H.INTRO | 8 | 8 | 0 | 0 | 0 | 0 |
| 횡단 H.SHIFT | 7 | 7 | 0 | 0 | 0 | 0 |
| 횡단 H.ART | 10 | 10 | 0 | 0 | 0 | 0 |
| 횡단 H.DEP | 12 | 12 | 0 | 0 | 0 | 0 |
| 횡단 H.SOT | 7 | 7 | 0 | 0 | 0 | 0 |
| 횡단 H.PERM | 16 | 16 | 0 | 0 | 0 | 0 |
| 횡단 H.MCP | 6 | 4 | 0 | 0 | 2 | 0 |
| 횡단 H.OTEL | 6 | 6 | 0 | 0 | 0 | 0 |
| **횡단 소계** | **72** | **70** | **0** | **0** | **2** | **0** |
| Stage 1 Plan | 30 | 28 | 0 | 0 | 2 | 0 |
| Stage 2 Design | 21 | 20 | 0 | 0 | 1 | 0 |
| Stage 3 Build | 79 | 79 | 0 | 0 | 0 | 0 |
| Stage 4 Test | 35 | 34 | 1 | 0 | 0 | 0 |
| Stage 5 Deploy | 48 | 48 | 0 | 0 | 0 | 0 |
| Stage 6 Maintain | 46 | 44 | 0 | 0 | 2 | 0 |
| Resources X | 18 | 18 | 0 | 0 | 0 | 0 |
| **합계** | **349** | **341** | **1** | **0** | **7** | **0** |

MISSING 0건. 남은 PARTIAL 1건은 아래 "읽는 법" 의 마지막 항목에 사유를 적었습니다.

## 읽는 법

- **1인 저장소의 autopilot·auto-merge(`roles.autopilot`·`roles.auto_merge`, `init` 이 켬)는 기본값이고 팀 저장소에서는 꺼져 있는, 1인 저장소 한정 편차입니다. 사용자는 해당 키를 false 로 두거나 `--no-autopilot`/`--no-merge` 로 원문의 정지 지점을 되살릴 수 있습니다.** 원문 "Nothing is implemented without an accepted plan" 의 '수락' 을 solo 모드에서는 사용자의 요청 자체로 보고 plan.md 를 커밋한 뒤 구현합니다. plan 은 여전히 커밋되어 리뷰어가 diff 를 대조하고, 훅·리뷰·브랜치 보호·production 게이트는 그대로 적용됩니다. 팀 저장소(`roles.solo: false`)에서는 intent·plan 승인 지점이 유지됩니다.

- **원문 코드 블록 14종은 전부 파일로 보존됩니다.** `templates/examples/` 에 원문이 인쇄된 그대로 들어 있고(`/sdlc:init` 이 `.sdlc/examples/` 로 복사), 같은 디렉터리의 `README.md` 가 각 블록과 이 플러그인의 일반화한 대응물을 1:1 로 짚습니다 — intent.md(A.1) · Stage 2 프롬프트(A.2) · plan.md(A.3) · CLAUDE.md "Payments service"(A.4) · secure-api-review 스킬(A.5) · verifier.md(A.6) · 검증 블록(A.7) · agent-evals.yml(A.8) · REVIEW.md(A.9) · settings.json 훅 등록(A.10) · production-gate.sh(A.11) · 관리형 설정 JSON(A.12) · triage 스텝(A.13) · bands.yaml(A.14).
- **호스팅 제품**(Claude Code Review · Claude Security · Claude Tag · Claude Design · Cowork)은 플러그인이 구현하지 않으며, 절차 안내와 로컬 대체(`/sdlc:review` · `/sdlc:scan` · `/sdlc:postmortem`)를 제공합니다. EXTERNAL-DOCUMENTED 7건이 이에 해당합니다.
- **채택 순서의 단일 출처는 `scripts/lib/scripts-extra.sh` 의 `sdlc_next_steps`** 입니다. `init.sh`·`doctor.sh`·`/sdlc:init`·`/sdlc:doctor` 는 그 출력을 그대로 보여 주며, 원문 텍스트와 의존성 그림이 어긋나는 네 곳은 README 의 "채택 순서" 절이 양쪽을 병기합니다.
- **남은 PARTIAL 1건은 의도된 편차입니다.** R-4.2.12 — eval 체커 계약이 원문의 스위트 수준 `check.sh <eval.json> result.json` 대신 케이스별 `check.sh <workdir>` 이며, 그 차이와 이유는 `evals-README.md` 가 명시하고 원문 워크플로는 예시로 보존합니다.
