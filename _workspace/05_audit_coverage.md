# 05 — 커버리지 감사: 플레이북 349 요구사항 대비 구현 (2026-09-07)

감사 대상: `plugins/sdlc/` 구현 전체(스킬 17·에이전트 5·훅 7·스크립트 8·템플릿 6군·evals·tests), 마켓플레이스 `README.md`, `docs/ENTERPRISE.md`, `docs/eli5.html`.
방법: 매트릭스 `01_requirements.md` 의 R-ID 349개 각각에 대해 해당 파일을 열어 근거를 인용. 설계서·README 서술은 근거로 쓰지 않음(문서 요구사항 제외).
실행 검증: `claude plugin validate . --strict` 및 `plugins/sdlc --strict` 통과 · `tests/hooks.sh` 172/172 PASS(bash 3.2 로그 포함) · `tests/scripts.sh` 71/71 PASS · `monitor.py --selftest` 7/7 PASS · 모든 스크립트 `--help` 정상.
배포용 대응표: `docs/PLAYBOOK-MAPPING.md`(R-ID 전수, 근거 file:line).

---

## A. 판정 집계

| 구간 | 행 | COVERED | PARTIAL | MISSING | EXTERNAL-DOCUMENTED | N/A |
|---|---|---|---|---|---|---|
| H.INTRO | 8 | 3 | 4 | 1 | 0 | 0 |
| H.SHIFT | 7 | 1 | 6 | 0 | 0 | 0 |
| H.ART | 10 | 7 | 3 | 0 | 0 | 0 |
| H.DEP | 12 | 4 | 7 | 1 | 0 | 0 |
| H.SOT | 7 | 6 | 1 | 0 | 0 | 0 |
| H.PERM | 16 | 16 | 0 | 0 | 0 | 0 |
| H.MCP | 6 | 4 | 0 | 0 | 2 | 0 |
| H.OTEL | 6 | 6 | 0 | 0 | 0 | 0 |
| Stage 1 | 30 | 22 | 5 | 1 | 2 | 0 |
| Stage 2 | 21 | 19 | 1 | 0 | 1 | 0 |
| Stage 3 | 79 | 62 | 13 | 4 | 0 | 0 |
| Stage 4 | 35 | 30 | 5 | 0 | 0 | 0 |
| Stage 5 | 48 | 35 | 12 | 1 | 0 | 0 |
| Stage 6 | 46 | 29 | 12 | 3 | 2 | 0 |
| X | 18 | 17 | 1 | 0 | 0 | 0 |
| **합계** | **349** | **261 (74.8%)** | **70 (20.1%)** | **11 (3.2%)** | **7 (2.0%)** | **0** |

유형별 경향: `process`·`artifact`·`infra`·`governance`·`measure` 행은 거의 전부 COVERED. PARTIAL/MISSING 81건 중 약 55건이 `doc` 행(플레이별 "전통 vs AI-native" 대비, Prerequisites 줄, 채택 순서 안내, 외부 참조 링크)이고, 구현 결함으로 인한 것은 F-01~F-08 에 해당하는 십여 건이다. **"블로그 전수 충족" 을 주장하려면 F-01·F-02(P0) 와 B 절의 doc 보강이 필요하다.**

---

## B. PARTIAL / MISSING 전건과 수정안

공통 수정안(여러 행이 공유):

- **FIX-SHIFT** — 각 스킬 SKILL.md 의 `# 제목` 바로 아래에 2~3줄짜리 `## What changes` 블록을 추가한다: `Traditional: …` / `AI-native: …` 를 원문 플레이의 두 열 문장으로 적는다(R-1.1.5, R-2.1.5, R-3.1.3, R-3.6.3, R-4.1.4, R-5.1.3, R-5.3.2, R-6.1.3, R-6.2.3 의 원문 문장 사용). 동시에 `plugins/sdlc/README.md` "요약" 뒤에 원문 전환표 6행(R-H.SHIFT.1~6)을 표로 넣고 마지막에 "대부분의 조직은 두 열 사이 어딘가에 있다"(SHIFT.7) 한 줄을 붙인다. 병렬 세션·auto mode 처럼 스킬이 없는 플레이의 What changes 는 README 대응표 행 아래 각주로 적는다.
- **FIX-PREREQ** — spec·policy·monitor·scan·postmortem·triage SKILL.md 의 `## When to use` 끝에 `Prerequisites: …` 한 줄을 원문 그대로 추가한다(spec: "an intent.md, with brand, security, compliance and UX policies written as skills"; policy: "None required. Having a CLAUDE.md helps … but a skill does not depend on it"; monitor: "intent.md format; the PR review loop; hooks as an action boundary; a rehearsed rollback path"; scan: "the PR review gate and hooks as approval gates; the intent.md format"; postmortem: "LESSONS.md, the evals suite"; triage: "monitor or scan has written to the queue"). review 에는 "skills if the review passes enforce written policies, defined subagents; graph: Evals → PR review (solid), Skills ⇢ PR review (dotted)" 를 덧붙인다.
- **FIX-ADOPT** — 채택 순서의 단일 출처를 만든다. `scripts/lib/scripts-extra.sh` `sdlc_next_steps` 를 그림의 5층으로 재배열: (1) 시작 가능 5개 = intent · CLAUDE.md · feedback loop(commands) · hook gate list · **plan mode**(plan 스킬 사용 안내) → (2) skills(`/sdlc:policy`, `policies: []` 이면) · subagents(agents 안내) · evals → (3) spec(Req & design) · PR review → (4) CI/CD → (5) closed loop(monitor/scan/on-call). git init 과 `/sdlc:init` 은 "0층 준비" 로 표시. `skills/init/SKILL.md:63-66` 과 `skills/doctor/SKILL.md:29-31` 의 순서 문장을 삭제하고 "scripts-extra.sh 의 Next steps 를 그대로 보여 준다" 로 바꾼다. `plugins/sdlc/README.md` 에 "채택 순서" 절을 신설해 5층 표 + "스테이지와 채택 순서는 다르다 — 아무 1층 플레이부터 시작" + Appendix B 의 텍스트/그림 불일치 4건(B-1 Skills 전제, B-2 PR review 전제, B-3 Closing the loop 전제, B-4 clay 색)을 "원문 텍스트 vs 그림" 두 줄로 병기한다.
- **FIX-EXAMPLES** — `plugins/sdlc/templates/examples/` 디렉터리를 신설하고 원문 코드 블록을 그대로 담는다: `intent.md`(A.1 claims status self-service, J. Ortiz 전문), `plan.md`(A.3), `CLAUDE.md`(A.4 Payments service), `verifier.md`(A.6, `name: verifier`, `tools: Bash, Read`), `settings.hooks.json`(A.10), `production-gate.sh`(A.11, 실행 비트), `bands.yaml`(A.14 원문 YAML — JSON 판의 출처로). README "아티팩트 체인 운영" 과 각 템플릿 상단 주석에서 예시 경로를 가리키고, `/sdlc:init --examples` 플래그(또는 기본)로 `intent/EXAMPLE.md` 등으로 복사하게 한다. `templates/evals/cases/` 3건은 A.1 시나리오(claims status)로 fixture 를 통일해도 좋다.

### B.1 횡단

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-H.INTRO.1 | PARTIAL | 역할별 핸드오프 모델 | `plugins/sdlc/README.md` 요약 첫 문단 뒤에 한 문장: "전통적으로 각 단계는 다른 역할(PM 요구사항, 아키텍트 설계, 엔지니어 빌드, QA 검증, 릴리스 팀 배포, 운영 감시)이 소유하고 문서·티켓·서명으로 이어졌다" |
| R-H.INTRO.2 | PARTIAL | 옛 통제의 존재 이유 | 같은 자리에 "PRD·추정 의식·보안 리뷰는 코드 작성이 가장 느렸을 때 수 주~분기의 빌드 동안 정렬을 강제하려 존재했다" 한 문장 |
| R-H.INTRO.3 | PARTIAL | ③ 거버넌스 비용 | 요약에 "③ 예외는 여전히 주·월 단위로 모이는 위원회를 거치므로 거버넌스 비용이 늘어난다" 추가 |
| R-H.INTRO.5 | MISSING | 보안 팀 병목 근거 | `skills/scan/SKILL.md` 헤더 아래 또는 README "Recurring codebase scans" 행에 "보안 팀은 사람 산출량에 맞춰 편성되어 있어, 에이전트가 출력을 늘리면 리뷰 큐가 쌓이거나 미검토 코드가 나간다 — 규제 조직은 둘 다 받을 수 없으므로 보안·정책 검사가 에이전트 속도를 따라야 한다" 를 넣는다 |
| R-H.INTRO.6 | PARTIAL | 동의어 | README 요약에 "(agentic SDLC, AI SDLC, agentic software development 라고도 부른다)" 괄호 추가 |
| R-H.SHIFT.1~6 | PARTIAL | 전통 열 | FIX-SHIFT |
| R-H.ART.3 | PARTIAL | .md 인 이유와 Build 이후 전환 | README "아티팩트 체인 운영" 첫 줄에 "초기 단계 아티팩트가 .md 인 이유는 프로덕트 오너와 에이전트가 같은 파일을 읽고 행동할 수 있기 때문이며, Build 부터는 코드와 그 기록(diff·테스트·PR·인시던트)이 아티팩트다" |
| R-H.ART.8 | PARTIAL | What changes·Infrastructure 절 | FIX-SHIFT 로 What changes 를, FIX-PREREQ 옆에 `Infrastructure: …` 한 줄(원문 각 플레이의 Infrastructure 문장)을 추가 |
| R-H.ART.9 | PARTIAL | Prerequisites 줄 | FIX-PREREQ |
| R-H.DEP.1 | PARTIAL | 스테이지≠채택 순서 문장 | FIX-ADOPT (README 채택 순서 절) |
| R-H.DEP.2 | PARTIAL | Plan mode 가 시작 플레이에 없음 | FIX-ADOPT (1층에 plan mode 포함, git/init 은 0층으로) |
| R-H.DEP.3 | PARTIAL | 그림/텍스트 병기 | `skills/policy/SKILL.md` When to use 에 "Prerequisites: none required (text); the dependency figure draws CLAUDE.md → Skills — having one helps, a skill does not depend on it" |
| R-H.DEP.4 | MISSING | 서브에이전트 전제 | README 대응표 "Parallel sessions and subagents" 행 또는 `agents/` 상단 주석에 "전제: CLAUDE.md(모든 세션이 읽음), 피드백 루프가 도움(그림: CLAUDE.md → Subagents 실선, Feedback loop ⇢ 점선)" |
| R-H.DEP.6 | PARTIAL | Skills → Req&Design 실선 | FIX-PREREQ(spec) |
| R-H.DEP.7 | PARTIAL | Evals/Skills 간선, 서브에이전트 | FIX-PREREQ(review) |
| R-H.DEP.9 | PARTIAL | monitor 전제 줄 | FIX-PREREQ(monitor) |
| R-H.DEP.11 | PARTIAL | 순서 불일치 3종 | FIX-ADOPT |
| R-H.SOT.2 | PARTIAL | 아티팩트별 원천 | `templates/config/config.json` `source_of_truth` 에 `"per_artifact": { "intent": "", "spec": "", "plan": "" }` (빈 값 = `mode` 상속) 추가하고 `artifact-conventions.md` §7 에 "아티팩트 종류별로 다르게 둘 수 있다(예: intent 는 Jira, plan 은 repo)" 규칙과 조회 순서를 적는다. README 설정 표에 키 추가 |

### B.2 Stage 1

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-1.1.5 | PARTIAL | 전통 열 | FIX-SHIFT(intent) |
| R-1.1.11 | PARTIAL | 전용 저장소·모노레포 안내 | `templates/en|ko/intent-README.md` 에 "단일 제품은 제품 저장소의 `intent/` 가 가장 단순하다(코드와 체인이 함께). 전용 intent 저장소는 intent 가 여러 저장소에 걸칠 때만 가치가 있고, 모노레포에서는 디렉터리 하나다" 문단 추가 |
| R-1.1.12 | PARTIAL | 쓰기 권한 결정 | `skills/init/SKILL.md` Governance 에 "intent 홈에 누가 쓸 수 있는지(팀·커넥터 계정)를 정하고 CODEOWNERS `intent/` 행과 저장소 권한으로 표현한다 — 기여자가 조직 전체에서 오기 때문" 추가; CODEOWNERS 템플릿 `intent/` 행 주석에 동일 문구 |
| R-1.1.16 | PARTIAL | 템플릿 리드 서명 | `intent-README.md` "TEMPLATE.md — 조직의 intent 템플릿. 기술 팀원이 만들고 리드가 서명한다; 변경은 PR 리뷰로" 로 문장 보강 |
| R-1.1.19 | PARTIAL | `(<team>)` | `templates/en|ko/intent.md:14` 를 `Author: {{author}} ({{team}}). Status: draft.` 로, `artifact-conventions.md` §3 에 `{{team}}` = 사용자에게 묻거나 `git config user.team` 없으면 빈 괄호 제거 규칙 추가 |
| R-1.1.25 | MISSING | A.1 예시 파일 | FIX-EXAMPLES |

### B.3 Stage 2

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-2.1.5 | PARTIAL | 전통 열 | FIX-SHIFT(spec) |

### B.4 Stage 3

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-3.1.3 | PARTIAL | 전통 열 | FIX-SHIFT(plan) |
| R-3.1.18 | MISSING | A.3 예시 파일 | FIX-EXAMPLES |
| R-3.2.2 | PARTIAL | 준비도 체크리스트 | `skills/plan/SKILL.md` Governance "Auto mode" 항목을 체크리스트로 확장: 가드레일 4(튠된 CLAUDE.md · 정책을 담은 스킬 · 불안전 행위를 막는 훅 · Claude 가 실행할 수 있는 테스트 스위트) + 작업 조건 3(좁은 spec.md · 작은 영향 범위 · 이미 테스트가 덮는 코드). `/sdlc:doctor` 에 `auto_mode_ready` 행을 추가해 네 가드레일 파일/설정 존재로 판정 |
| R-3.2.3 | PARTIAL | 행위 감시 → 아티팩트 검토 | 같은 항목에 "auto mode 에서는 편집을 지켜보는 대신 긴 자율 세션 뒤의 아티팩트(diff·verify 출력·plan Departures)를 검토한다" 한 줄 |
| R-3.2.4 | PARTIAL | worktree·닫힌 루프 연결 | README 대응표 Auto mode 행에 "worktree 와 함께 쓰면 개인·팀 병렬성이 생기고, Stage 6 의 자율 실행(monitor → triage intent → spec PR)의 전제다" |
| R-3.3.4 | PARTIAL | 내장 `/init` | `skills/init/SKILL.md` Step 4 앞에 "CLAUDE.md 가 없으면 Claude Code 내장 `/init` 을 먼저 돌려 초안을 받아도 된다; `/sdlc:init` 은 그 위에 5섹션 마커를 얹는다" |
| R-3.3.14 | MISSING | A.4 예시 파일 | FIX-EXAMPLES |
| R-3.3.17 | PARTIAL | metrics 행 없음 | `scripts/metrics.py` Stage 3 표에 `("Time to first merged PR for a new team member", None, "source needed: PR history (gh pr list --author) + join date", "lagging")` 행 추가 |
| R-3.4.3 | PARTIAL | 전제 문장 | FIX-PREREQ(policy) |
| R-3.5.3 | PARTIAL | 편집 후 린터 | `scripts/hooks/post-edit.sh` 에 `commands.lint_file`(선택, `{file}` 치환) 실행 분기를 추가하거나, `auto` 분기에서 eslint/ruff check/golangci-lint 를 변경 파일 1개에 대해 `run_capped` 로 실행(실패는 stderr 한 줄, exit 0 유지). README 훅 표 5행과 config 표에 키 추가. 최소 대안: README 에 "린트는 verify 단계에서 실행(파일 단위 린트가 필요하면 `commands.format` 에 린터를 넣는다)" 명시 |
| R-3.6.1 | PARTIAL | 병렬 세션 설명 | README 에 "병렬 세션과 서브에이전트" 절 신설(아래 3.6.6~8 포함): "병렬 세션은 자기 worktree 에서 별도 작업을 하는 또 하나의 Claude Code 인스턴스이며 서로를 모른다; 서브에이전트는 한 세션 안의 범위 한정 도우미로 자기 컨텍스트와 도구 제한을 가진다" |
| R-3.6.3 | PARTIAL | 전통 열 | FIX-SHIFT(README 절) |
| R-3.6.4 | PARTIAL | 전제 | 같은 절에 "전제: CLAUDE.md, 피드백 루프" |
| R-3.6.6 | MISSING | plan 으로 작업 분할 | 같은 절에 "plan.md 의 Files that change 로 겹치지 않는 파일 집합을 나누고, 파일을 공유하는 작업은 한 세션에서 순차로" + `/sdlc:plan` Output 에 "독립 작업 묶음 제안" 한 줄 |
| R-3.6.7 | PARTIAL | 예시 명령 | 같은 절에 `claude --worktree feature-auth` / `claude --worktree fix-rate-limit` 예시 |
| R-3.6.8 | MISSING | 2~3 세션 | 같은 절에 "2~3 세션이 출발점; 리뷰가 따라오는 동안만 늘린다" |
| R-3.6.10 | PARTIAL | verifier.md 원문 | FIX-EXAMPLES(`templates/examples/verifier.md`) + `agents/sdlc-verifier.md` 상단 주석 "원문 verifier.md 의 확장판(Grep/Glob 추가)" |

### B.5 Stage 4

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-4.1.4 | PARTIAL | 전통 열 | FIX-SHIFT(verify) |
| R-4.2.3 | PARTIAL | 주기만 실행 변형 | `templates/github/sdlc-evals.yml` 헤더 주석에 "매 변경 대신 정해진 주기로만 돌리려면 `pull_request:` 블록을 지우고 `schedule` 만 남긴다" 추가; `skills/evals/SKILL.md` When to use 에 같은 문장 |
| R-4.2.6 | PARTIAL | 20~50건 | `skills/evals/SKILL.md` `add` 1단계에 "플랫폼 엔지니어는 최근 작업에서 실제 과제 20~50건을 기대 결과와 함께 모아 시작한다" 추가 |
| R-4.2.11 | PARTIAL | 루프 본문 | `sdlc-evals.yml` 헤더에 "원문 agent-evals.yml 의 `for eval in evals/*.json … ./evals/check.sh` 루프를 `run-evals.sh`(디렉터리 케이스 형식) 로 대체했다" 명시; FIX-EXAMPLES 에 원문 yml 을 `templates/examples/agent-evals.yml` 로 보존 |
| R-4.2.12 | PARTIAL | 스위트 수준 check.sh | `templates/en|ko/evals-README.md` 에 "원문의 `evals/check.sh <eval.json> result.json` 대신 케이스별 `check.sh <workdir>`; run-evals.sh 가 결과 JSON 을 results.json 으로 모은다" 를 적고, 선택적으로 `templates/evals/check.sh`(원문 시그니처 어댑터: eval.json 의 `checks[]` 를 읽어 result.json 검사) 제공 |

### B.6 Stage 5

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-5.1.3 | PARTIAL | 전통 열 | FIX-SHIFT(review) |
| R-5.1.4 | PARTIAL | 전제 | FIX-PREREQ(review) |
| R-5.1.8 | PARTIAL | 집계 게이트 스크립트 | `scripts/review-gate.sh <comment-file|stdin> [--max-important N]` 신설: `SDLC_REVIEW_TALLY {…}` 줄을 파싱해 important > N 이면 exit 1. `sdlc-review.yml` 에 주석 처리된 선택 스텝으로 연결. README CI 표에 "선택: 집계 게이트" |
| R-5.1.15 | MISSING | 참조 링크 | README "제한과 비범위" 위에 "더 읽기" 절: "How these controls compose at production scale: Securing an AI-native SDLC at Anthropic (anthropic.com engineering blog)" |
| R-5.2.2 | PARTIAL | 마이그레이션/인프라 티켓 가드 | `templates/config/config.json` `protect` 에 `"ticketed_paths": []`(예: `migrations/`, `infra/`) 와 `"ticket_env": "CHANGE_TICKET"` 추가; `guard-edit.sh` (1)~(2) 사이에 "ticketed 경로 편집은 `$CHANGE_TICKET` 이 있어야 허용, 없으면 deny + Why/To proceed" 분기; tests/hooks.sh 케이스 2건; README 훅 표 3행에 ⑤ 추가 |
| R-5.2.4 | PARTIAL | 승인 등록부 | `templates/en|ko/APPROVALS.md`(승인 게이트 등록부: 게이트 · 무엇이 승인인가 · 승인자 · 강제 수단(훅/브랜치 보호/관리형) · 로그 위치) 를 `.sdlc/APPROVALS.md` 로 init 이 생성. 기본 행 3개: 변경관리 승인(ticketed_paths/CHANGE_TICKET), 릴리스 인가(RELEASE_APPROVAL), 보호 경로(frozen-paths) |
| R-5.2.5 | PARTIAL | 변경관리 승인 게이트 명명 | 위 APPROVALS.md 기본 행 |
| R-5.2.7 | PARTIAL | settings.json 훅 예시 | FIX-EXAMPLES(`settings.hooks.json`, `production-gate.sh`) + README 훅 절에 "플러그인 훅 = 팀 공통(플러그인 버전에 고정); 프로젝트 고유 훅은 `.claude/settings.json`(예시 파일), 타협 불가 훅은 관리형 설정" 3층 설명 |
| R-5.2.9 | PARTIAL | A.10 원문 | FIX-EXAMPLES |
| R-5.3.2 | PARTIAL | 전통 열 | FIX-SHIFT(release) |
| R-5.3.5 | PARTIAL | flaky 요약·체인지로그 | `templates/github/sdlc-ci-flaky.yml`(workflow_run 실패 시 재실행 결과 비교 → "flaky/real" 3줄 코멘트) 과 `sdlc-changelog.yml`(release/tag push 시 `claude -p "Draft the changelog from git log <prev>..<tag>"` → PR) 추가, 또는 `sdlc-ci-triage.yml` 에 두 스텝을 주석 처리된 선택 잡으로 넣기. README CI 표 갱신 |
| R-5.3.6 | PARTIAL | monitor.yml 직접 푸시 | F-04 참조 |
| R-5.3.14 | PARTIAL | 에이전트 신원 문서 | README CI 템플릿 절에 "비대화형 실행은 에이전트 자기 신원(`sdlc-monitor` git 사용자, `github-actions[bot]`/claude-code-action 앱)으로 커밋·코멘트하므로 파이프라인 로그에서 사람과 구분된다" 한 문단; sdlc-spec-on-intent.yml 의 create-pull-request 에 `committer`/`author` 를 `sdlc-spec <sdlc-spec@users.noreply.github.com>` 로 명시 |

### B.7 Stage 6

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-6.1.2 | PARTIAL | 헤드리스 체인·신뢰 게이트 | README "닫힌 루프" 절에 현재 체인을 그대로 적는다: monitor(결정론) → triage intent(draft) → **사람 트리아지**(신뢰 게이트 1) → spec PR(자동) → **PR 리뷰 + 사람 승인**(게이트 2) → plan/build 는 대화형. 헤드리스 plan/build 를 원하면 `sdlc-spec-on-intent.yml` 을 본떠 `sdlc-plan-on-spec.yml`(승인된 spec 머지 → `claude -p` plan 초안 PR, 리뷰어 에이전트 통과 시 다음 단계)을 추가하는 절차와 "결정론 검사 또는 적대적 리뷰 에이전트(sdlc-reviewer)가 계속/에스컬레이션을 결정" 원칙을 문서화 |
| R-6.1.3 | PARTIAL | 전통 열 | FIX-SHIFT(monitor) |
| R-6.1.5 | PARTIAL | 전제 | FIX-PREREQ(monitor) |
| R-6.1.6 | PARTIAL | Agent SDK | `skills/monitor/SKILL.md` When to use 에 "트리거는 GitHub/GitLab 스케줄, 모니터링 스택의 웹훅, 네트워크 안 cron, 또는 웹훅을 받는 Agent SDK 서비스(샌드박스 컨테이너) — 어느 경우든 `monitor.py` 를 무상태로 호출" |
| R-6.1.7 | PARTIAL | 5xx·PR cycle time 예시 | `templates/config/bands.json` 에 주석 처리된(또는 `enabled: false`) 예시 2개 추가: `post_deploy_5xx_rate`(command 예: Prometheus `curl … | jq`) 와 `pr_cycle_time_hours`(command 예: `gh pr list --state merged --json createdAt,mergedAt --jq …`); monitor.py 가 `enabled: false` 를 건너뛰도록 3줄 추가 |
| R-6.1.10 | PARTIAL | 웹훅·Agent SDK | 위 6.1.6 문장 |
| R-6.1.18 | PARTIAL | 예시 1 | README 닫힌 루프 절에 예시 3개를 원문대로 나열(CI 실패율 3σ → flaky 격리 또는 revert PR; 5xx 3σ + 배포 → 롤백 파이프라인; PR cycle time 드리프트 → 리더십 보고) |
| R-6.1.20 | MISSING | 예시 3 | 위와 동일 + `bands.json` 의 `pr_cycle_time_hours` 3σ routes 를 `["report"]` 로 두고 monitor.py 에 `report` 라우트(intent 대신 `.sdlc/reports/<ts>-<metric>.md` 작성) 추가 |
| R-6.2.3 | PARTIAL | 전통 열 | FIX-SHIFT(scan) |
| R-6.2.4 | PARTIAL | 전제 | FIX-PREREQ(scan) |
| R-6.2.5 | PARTIAL | 호스팅 전제 6개·과금 | `skills/scan/SKILL.md` 6단계를 체크리스트로 교체: Claude Enterprise · Anthropic GitHub App 설치(cloud github.com) · Claude Code on the Web 활성 · Extra Usage 켜고 지출 한도 · 스캔 실행자 프리미엄 시트 · 관리자가 claude.ai/admin-settings/claude-code 에서 활성화 · 과금은 Mythos 5 단가로 소비 기반(한도를 저장소 수·크기에 맞춤) |
| R-6.2.6 | PARTIAL | 프로젝트 조직 | 같은 체크리스트에 "보안 리드가 저장소를 연결하고 repo/service/team 기준 프로젝트로 묶어 발견의 소유가 처음부터 분명하게" |
| R-6.2.13 | MISSING | 내보내기·웹훅 | 같은 절에 "발견은 CSV/Markdown 내보내기 또는 웹훅으로 기존 트래커·감사 시스템에 보낸다(감사인이 기대하는 곳이 system of record)"; 로컬 대체로 `/sdlc:scan --report <file>` 의 Markdown 이 그 역할임을 명시 |
| R-6.3.3 | PARTIAL | 채널 협업 | `skills/postmortem/SKILL.md` 6단계에 "채널의 누구든 대응을 지휘·실행하고 가설을 실시간으로 검증할 수 있으며, 채널 이력이 감사 가능성을 더한다" |
| R-6.3.6 | MISSING | 참조 링크 | README "더 읽기" 절에 "How Claude Tag runs on-call for CI/CD at Anthropic" |

### B.8 Resources

| R-ID | 판정 | 빠진 것 | 수정 |
|---|---|---|---|
| R-X.0 | PARTIAL | 저자·감사 | `plugins/sdlc/README.md:3` 과 마켓플레이스 `README.md:3` 인용문에 "Louis Claxton, 2026-08-21; acknowledgments Jim Blackhurst, Will Steuk, Jamal Arif" 추가 |

---

## C. 특별 점검 결과 (F-xx)

심각도: **P0** 블로그 전수 충족 주장을 막음 · **P1** 잘못되었거나 오해를 부름 · **P2** 다듬기.

### P0

**F-01 · 원문 코드 블록이 파일로 재현되지 않음 (A.1·A.3·A.4·A.6·A.10·A.11)** — `example-code` 유형 행 R-1.1.25, R-3.1.18, R-3.3.14, R-3.6.10, R-5.2.9 의 수락 조건은 "Appendix 와 일치". 현 상태: intent·plan·CLAUDE.md 예시는 템플릿 구조와 가이드의 파편(`claude-md-guide.md:45-54`) 만 있고 예시 본문 파일이 없음; `agents/sdlc-verifier.md` 는 의미는 같지만 name·tools·문구가 다름; `.claude/settings.json` 훅 등록과 `production-gate.sh` 는 `hooks.json`+`guard-bash.sh` 로 동작만 구현. 원문 보존 판정을 받은 블록: A.2 프롬프트(spec:32-36), A.5(secure-api-review 동일), A.7(검증 블록, Build 줄만 일반화), A.9(REVIEW.md 4절, 두 줄에 항목 추가), A.12(관리형 설정 동일), A.13(triage 프롬프트 동일), A.14(bands, JSON 판). **수정**: FIX-EXAMPLES.

**F-02 · 설치되는 CI 파일에 치환되지 않는 플레이스홀더** — `templates/github/sdlc-evals.yml:22` 와 `sdlc-monitor.yml:22` 의 `{{marketplace_source}}` 는 `scripts/init.sh:325` 가 `marketplace_repo=` 로 넘기므로 그대로 남아 `claude plugin marketplace add {{marketplace_source}}` 로 첫 스텝이 실패한다(evals 게이트·모니터 워크플로 둘 다 무효). `sdlc-ci-triage.yml:7` 의 `{{ci_workflow_name}}` 은 어디서도 넘기지 않아 `workflow_run.workflows: ["{{ci_workflow_name}}"]` 가 어떤 워크플로와도 매치되지 않는다(triage 가 영원히 안 뜸). `CODEOWNERS` 의 `{{tech_lead}}`(4) `{{platform_team}}`(4) `{{product_owner}}`(2) 는 `init.sh:329` 가 `team=` 만 넘겨 남고, GitHub 는 해당 줄을 무효 오너로 무시한다(CLAUDE.md·REVIEW.md·.claude/·intent/·spec/·plan/ 행이 전부 죽음 — R-3.3.15 실효 없음). 헤더 주석은 "{{team}} 플레이스홀더를 바꾸라" 고만 하고 doctor 는 `TODO` 문자열만 본다. **수정**: (a) `init.sh:325` 를 `marketplace_source=${MARKETPLACE_SOURCE:-TODO-org/ai-native-sdlc}` 로 고치고 `--marketplace <src>` 옵션 추가, (b) `ci_workflow_name=${CI_WORKFLOW_NAME:-CI}` 를 넘기고 `--ci-workflow <name>` 옵션 추가(감지: `.github/workflows/*.yml` 의 `name:` 중 첫 번째), (c) CODEOWNERS 렌더에 `tech_lead=$TEAM platform_team=$TEAM product_owner=$TEAM` 추가, (d) `doctor.sh` workflows/codeowners 행에서 `grep -q '{{'` 도 경고, (e) `tests/scripts.sh` 에 "설치된 .github/ 아래 `{{` 없음" 검사 추가.

### P1

**F-03 · production 게이트 정규식이 단어 순서를 요구** — `templates/config/config.json:78`(그리고 미초기화 프로젝트 기본값)의 `(deploy|release|rollout|promote)[^|;&]*(prod|production|live)` 는 배포어가 환경어 앞에 올 때만 매치. 실측: `production deploy`, `./production-deploy.sh`, `prod-deploy.sh` 는 **통과**(원문 `production-gate.sh` 의 양쪽 부분문자열 검사라면 차단). `kubectl … production` 등 별도 패턴은 정상. **수정**: config 템플릿(그리고 README 설정 표 예시)에 역순 패턴 `(prod|production|live)[^|;&]*(deploy|release|rollout|promote)` 추가; `tests/hooks.sh` 에 세 명령 차단 케이스 추가; `doctor.sh` 훅 자가시험 probe 에 `./production-deploy.sh` 추가.

**F-04 · `sdlc-monitor.yml` 이 기본 브랜치에 직접 푸시** — `templates/github/sdlc-monitor.yml:30-34` 는 스케줄 실행에서 체크아웃된 기본 브랜치에 history 관측치와 triage intent 를 커밋하고 `git push` 한다. 원문 "에이전트가 쓰는 것은 모두 PR 로, main 직접 경로 없음"(R-5.3.6/13) 과 README.md:349 "triage intent 를 커밋하는 PR" 에 어긋나고, 브랜치 보호가 있으면 매 30분 실패한다. 3σ `pull_request` 라우트(monitor.py `open_pull_request`)는 별도 브랜치+PR 을 만들어 정상. **수정**: 마지막 스텝을 `peter-evans/create-pull-request@v6`(branch `sdlc/monitor-<run_id>`, add-paths `.sdlc/history intent/triage`) 로 교체하거나, `gh pr create` 로 브랜치 PR 을 만들도록 변경; README CI 표 문구 유지.

**F-05 · 채택 순서가 세 곳에서 다르고 그림과도 다름** — `scripts/lib/scripts-extra.sh:257-312`(git→init→CLAUDE.md→feedback→intent→hook list→spec→plan→verify→evals→PR review→CI/CD→closed loop), `skills/init/SKILL.md:63-66`(mistakes→intent→policy→spec→plan→verify→evals→review→github→monitor), `skills/doctor/SKILL.md:29-31`(CLAUDE.md+intent+hook+feedback+skills→spec→plan→parallel/subagents→evals→PR review→CI/CD→closed loop). 원문 그림: 1층 5개(Plan mode 포함) → 2층 Skills·Subagents·Evals → 3층 Req&Design·PR review → 4층 CI/CD → 5층 Closing. 세 구현 모두 Plan mode 를 시작 플레이로 두지 않고, next_steps 에는 Skills·Subagents 단계 자체가 없다. **수정**: FIX-ADOPT.

**F-06 · `verify.run` 이벤트 이중 발행으로 통과율 왜곡** — `skills/verify/SKILL.md:66-69` 는 `verify.run` 을 `decision: allow|deny` 로, `scripts/hooks/post-bash.sh:55` 는 같은 이름을 `pass|fail` 로 기록한다. `scripts/metrics.py:358-359` 는 `decision == "pass"` 만 성공으로 세므로 스킬이 남긴 `allow` 는 분모에만 들어가 "Local verify runs pass rate" 가 실제보다 낮게 나온다. **수정**: 스킬 이벤트를 `verify.skill`(또는 `decision: pass|fail`)로 바꾸고 metrics 는 `pass`/`allow` 모두 성공으로 처리; `tests/scripts.sh` 에 이벤트 두 종류가 섞인 events.jsonl 로 비율 검증 추가.

**F-07 · `docs/eli5.html` 이 낡고 조직 특정 내용을 노출** — 하단 "내 주방 · /Users/brad/project/mma · 2026-09-06 실측" 절이 로컬 절대 경로, "skills 11종·agents 13종", "사내 Nexus 도메인", "JDK 1.8 게이트", "JSP 선언부 `%>` 사고", "sdlc/starter/", "_workspace" 등 다른 프로젝트 상태를 그대로 담고 있다. 마켓플레이스 README:54 가 "그림으로 보는 설명" 으로 링크하므로 외부 독자에게 그대로 보인다(요구사항 "조직 특정 하드코딩 없음" 위반). 상단 6단계 비유와 "안전장치 네 겹" 은 유효하고 R-H.INTRO/SHIFT 보강에 쓸 수 있다. **수정**: "내 주방" 절과 "오늘 할 일" 절을 삭제(또는 `/sdlc:doctor --json` 출력을 붙여 넣는 안내로 교체), 조직 특정 문구 제거, 하단 인용은 유지.

**F-08 · `status.sh` 의 `date -u -r <epoch>` 는 BSD 전용** — `scripts/status.sh:40` `fmt_date`. GNU date 는 `-r` 을 파일 참조로 해석해 실패하고 `|| printf '-'` 로 조용히 "-" 를 찍으므로 Linux/WSL 에서 모든 커밋 날짜가 사라진다(테스트는 macOS 에서만 실행되어 잡히지 않음). 다른 GNU/BSD 차이(`stat -f/-c`, `timeout` 부재, `sed -i`, `mapfile`, `declare -A`, `${var,,}`, `readlink -f`, `date -d`)는 점검 결과 없음 또는 폴백 처리됨. **수정**: `fmt_date() { date -u -r "$1" +%F 2>/dev/null || date -u -d "@$1" +%F 2>/dev/null || printf '-'; }`; `tests/scripts.sh` 에 날짜 열이 `-` 가 아닌지 확인하는 검사 추가.

**F-09 · CHANGELOG 가 존재하지 않는 `tests/run.sh` 를 언급** — `plugins/sdlc/CHANGELOG.md:17` "Tests: `tests/run.sh`". 실제는 `tests/hooks.sh` 와 `tests/scripts.sh`. **수정**: CHANGELOG 문구 교체, 또는 두 스크립트를 순서대로 호출하는 `tests/run.sh` 추가(설계서와 일치).

**F-10 · README 설정 표의 `gate.mode` 설명이 구현과 다름** — README.md:222 "v1 은 `deny`(exit 2) 만 정의" 이나 `scripts/hooks/guard-bash.sh:72` 는 `ask` 모드를 구현하고 `tests/hooks.sh:204-207` 이 검증한다. 같은 표의 events 형식(README.md:301)도 `hook.stop`·`verify.run`·`eval.suite`·`band.check`·`monitor.*`·스킬 이벤트(`intent.write`, `review.run` 등)를 빠뜨린다. **수정**: `gate.mode` 를 "`deny`(기본, exit 2) 또는 `ask`(사람 확인; `-p` 에서는 거부)" 로; 로그 표에 이벤트 목록을 스크립트에서 생성한 전체로 교체.

**F-11 · 문서 누락 묶음(각 R-ID 는 B 절 참조)** — 참조 링크 2건(R-5.1.15, R-6.3.6), Claude Security 전제·내보내기(R-6.2.5/6/13), 병렬 세션 안내(R-3.6.1/6/7/8), 읽기 전용 스텝 2종(R-5.3.5), 닫힌 루프 예시(R-6.1.7/18/20), 보안 병목 근거(R-H.INTRO.5), 출처 저자(R-X.0). 하나씩은 작지만 합치면 `doc` 행 PARTIAL/MISSING 의 대부분이다.

### P2

**F-12 · 테스트 잠금 해제 안내가 스킬과 훅 사이에서 모순** — `skills/verify/SKILL.md:60-61`, `skills/plan/SKILL.md:63-64` 는 "SDLC_ALLOW_TEST_EDIT 를 지름길로 요청하지 말라" 고 하는데 `scripts/hooks/guard-edit.sh:85` 의 차단 메시지와 `session-start.sh:50` 은 "사용자에게 SDLC_ALLOW_TEST_EDIT=1 로 실행하라고 요청" 을 첫 경로로 제시한다. 수정: 훅 메시지 순서를 "plan 의 `test_changes: allowed`(계획 변경) → 플래그 파일(`/sdlc:verify --bugfix` 가 관리) → 최후에 env" 로 바꾸고 "테스트 자체가 틀렸을 때만" 을 앞세운다.

**F-13 · "한 페이지" 임계치 불일치** — `skills/init/SKILL.md:53`·`skills/lesson/SKILL.md:30` 은 120줄, `scripts/doctor.sh:56` 은 200줄. 하나로(120) 맞추고 `templates/config/config.json` 에 `claude_md.max_lines` 로 노출.

**F-14 · 리뷰 이벤트에 `policy_findings` 가 없어 지표가 항상 0** — `agents/sdlc-reviewer.md:23` 집계에는 `policy_findings`·`repeat_findings` 가 있지만 `skills/review/SKILL.md:72` 의 printf 는 `important/nits/passes/pr/slug/repeat_finding` 만 기록; `scripts/metrics.py:352` 는 `policy_findings` 를 읽는다(R-3.4.16 실효 없음). 또 metrics.py:351 "Mistakes repeated" 는 `lesson.add` 수를 세지만 lesson 스킬 문서(56-58)는 `review.run` 의 `repeat_finding: true` 를 지표로 정의. 수정: 스킬 printf 에 `policy_findings` 추가, metrics 가 `repeat_finding` 을 집계.

**F-15 · 한국어 CLAUDE.md 스텁이 lesson 1건으로 계산** — `scripts/metrics.py:226` 은 영어 스텁("(none", "add a line here") 만 제외해 `templates/ko/CLAUDE.sections.md:21` 의 스텁 줄이 lessons=1 로 잡힌다. 수정: `/sdlc:lesson` 이 이 일을 한다` 를 포함하는 줄도 제외하거나, 두 언어 스텁을 `- (none yet)` 로 통일.

**F-16 · `sdlc-spec-on-intent.yml` 이 `intent/`·`spec/` 경로를 하드코딩** — `init.sh:325` 가 `intent_dir`/`spec_dir` 를 넘기지만 템플릿(9, 24, 40-42, 54행)은 `{{intent_dir}}`/`{{spec_dir}}` 를 쓰지 않는다. `paths.*` 를 바꾼 프로젝트에서 동작하지 않음. 수정: 템플릿의 경로를 플레이스홀더로.

**F-17 · 스킬 frontmatter `allowed-tools` 의 `${CLAUDE_PLUGIN_ROOT}` 치환 미검증** — `skills/monitor|metrics|status/SKILL.md` 의 `allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*")`. 플랫폼 사실은 본문 치환만 확정. frontmatter 에서 치환되지 않으면 사전 승인이 조용히 매치되지 않아 프롬프트가 뜬다(차단은 아님). 수정: 실제 세션에서 확인하거나 `Bash(python3 *)` 로 완화. 같은 맥락에서 `!` 동적 컨텍스트 줄의 `$ARGUMENTS`·`${CLAUDE_PLUGIN_ROOT}` 치환 순서도 확인 필요(실패 시 `|| true` 로 가려지고 Step 1 이 재실행하므로 기능 손실은 없음).

**F-18 · README 스킬 표가 인자를 축약** — 실제 argument-hint 와 차이: intent(`--from-scan`, `approve|reject`), spec(`--force`, `approve|reject`), plan(`--allow-test-changes`, `--no-implement`), verify(`--ui <mock-path>`, `--no-agent`), review(`--base`, `--slug`, `--post`), release(`rollback`), monitor(`add <name>`), lesson(`"<line>" --eval --section`), scan(`<path> --since --report`), postmortem(`<title> --date --pr`), policy(`<name> --owner --source`). 수정: 표를 SKILL.md 의 argument-hint 로부터 생성(tests/scripts.sh 에 일치 검사).

**F-19 · python3 가 사실상 필수인데 README 는 폴백으로만 설명** — `scripts/init.sh:49` 는 python3 없으면 종료, doctor/status(scripts-extra 의 detect_project·py_render)·monitor.py·metrics.py 도 python3 필요. README FAQ:404 는 "jq → python3 폴백" 만. 수정: 설치 절에 "필수: bash 3.2+, git, python3 3.9+; 선택: jq(훅 속도), gh, claude" 표 추가; doctor 는 이미 python3 를 required 로 표시함.

**F-20 · 테스트의 잡음 오류** — `tests/scripts.sh:115` 가 `.sdlc/state/` 생성 전에 `active-plan` 을 쓰려 해 로그에 "No such file or directory" 가 남는다(init.sh 는 gitignore 대상인 `.sdlc/state/` 를 만들지 않음). 수정: 테스트에서 `mkdir -p "$C/.sdlc/state"`; 또는 `init.sh` 가 `.sdlc/state/.gitkeep` 없이 디렉터리만 생성.

**F-21 · `--example secure-api-review` 복사본에 플레이스홀더 잔존** — `templates/policies/secure-api-review/SKILL.md:9` 의 `{{policy_owner}}`·`{{policy_source}}`·`{{date}}` 는 `skills/policy/SKILL.md:18` "copies" 만으로는 채워지지 않는다. 수정: policy 스킬 2단계에 "복사 후 세 플레이스홀더를 --owner/--source/오늘 날짜로 치환" 명시.

**F-22 · `evals/README.md` 의 `claude plugin eval` 플래그 미검증** — `--judge-model`, `--allow-tools` 는 early access 라 실행 불가로 표기됨. 기능 개방 후 재확인 항목으로 남긴다.

**F-23 · `scripts/metrics.py:414` 의 `… if False else …` 죽은 분기** — 결과는 맞지만 읽기 어렵다. 한 표현식으로 정리.

**F-24 · CLAUDE.md 섹션 탐지 휴리스틱** — `scripts/lib/scripts-extra.sh:75-79` 는 제목이 해당 단어로 시작할 때만 인식(`## Build & test commands` 는 미인식 → `## Commands` 중복 추가). 수정: 제목 어디에든 단어가 있으면 인식(`commands?`, `convention`, `architecture`) 하되 마커가 있으면 우선.

**F-25 · PostToolUse 매처가 NotebookEdit 을 빠뜨림** — `hooks/hooks.json:11` PreToolUse 는 `Edit|Write|MultiEdit|NotebookEdit`, `:17` PostToolUse 는 `Edit|Write|MultiEdit`. post-edit.sh 는 `notebook_path` 를 처리할 준비가 되어 있으므로 매처에 추가.

**F-26 · 롤백 명령 "항상 허용" 이 부분문자열 매치** — `scripts/hooks/guard-bash.sh:23` `*"$rb"*`. `commands.rollback` 이 짧은 문자열이면 그것을 포함한 다른 명령도 게이트를 우회한다. 수정: 명령이 `$rb` 로 시작하거나(`"$rb"|"$rb "*`) 파이프/체인 없이 단독일 때만 허용.

**F-27 · plan 제목 괄호와 intent Author 줄의 원문 차이** — `templates/en/plan.md:13` `(from {{spec_path}})` vs 원문 `(from intent.md <date>)`; `templates/en/intent.md:14` 에 `(<team>)` 없음(B.2). 의도된 변경이면 템플릿 주석에 한 줄 남긴다.

**F-28 · evals 형식이 원문과 다른데 어디에도 명시되지 않음** — 디렉터리 케이스(prompt.md+check.sh+case.json) 는 원문의 `evals/*.json` + `evals/check.sh` 와 다르다. `templates/en|ko/evals-README.md` 와 `sdlc-evals.yml` 헤더에 차이와 이유(언어 무관 fixture, 훅이 실제로 발동하는 임시 저장소)를 한 문단으로.

### 특별 점검 요약표

| 점검 | 결과 |
|---|---|
| 원문 코드 블록 재현 | A.2·A.5·A.7·A.9·A.12·A.13·A.14 보존(A.7 Build 줄·A.9 두 줄 소폭 변형, A.14 는 JSON). A.1·A.3·A.4 예시 파일 없음, A.6 의미만, A.8 루프 본문 대체, A.10·A.11 동작만 → **F-01** |
| 의존성 그래프 / 채택 순서 | 세 곳 불일치, 그림과 다름, Appendix B 불일치 4건은 어느 쪽도 문서화하지 않음 → **F-05** |
| 신규 vs 기존 프로젝트 | `init.sh` 는 어떤 파일도 덮어쓰지 않음(`put_file` 존재 시 SKIPPED), CLAUDE.md 는 빠진 섹션만 append(199-226), settings 는 allow/deny 합집합 + 최상위 키 없을 때만 추가(243-281, 팀 훅·env 보존), .gitignore 는 빠진 줄만(175-195), 남의 `evals/` 는 `sdlc-evals/` 로 우회(119-122), `.github/` 는 없는 파일만(319-332). `tests/scripts.sh:41-97` 이 전부 검증. **양호** |
| 이식성 | bash 3.2 실측 통과(`test_hooks_bash32.log` 172/172); 금지 구문 없음; `stat` 폴백 있음; `timeout` 대체(`run_capped`/`wait_capped`); jq 없는 PATH 테스트 10건 통과; Windows/WSL 안내(README:408); en/ko 템플릿 8/8 완비, 한국어 제목이 영어 키워드를 괄호로 유지해 스크립트 매칭 유지. 예외: `date -r`(**F-08**), eli5 조직 특정 내용(**F-07**), 매니페스트 author "mma platform team"(배포 시 조직명으로 교체 — 허용) |
| 일관성(플래그) | SKILL.md 가 언급한 스크립트 플래그 전부 `--help` 에 존재(init 8종, doctor `--json`, run-evals 5종, metrics 2종, status slug, monitor `--dry-run --metric`). `monitor add`·`evals add|list|report`·`intent approve` 등은 스킬 수준 서브커맨드 |
| 일관성(이벤트) | 훅·스크립트가 쓰는 이벤트(hook.gate/guard/stop, session.*, verify.run, eval.run/suite, band.*, monitor.*)와 스킬이 쓰는 이벤트(init.run, doctor.run, intent.*, spec.*, plan.*, eval.add, review.run, lesson.add, release.deploy, triage, scan.run, postmortem.write, policy.add) 모두 `.sdlc/logs/events.jsonl` 의 `{ts,event,session_id,decision,reason,detail}` 형태. metrics.py 는 그중 10개 접두어만 읽음. 결함: verify.run 이중 발행(**F-06**), policy_findings 키 누락(**F-14**) |
| README 표 ↔ 파일 | 훅 표 7행·타임아웃 일치; 설정 표 키 전부 템플릿에 존재(`gate.mode` 설명 오류 **F-10**); 워크플로 표 6행 일치(monitor "PR" 문구는 구현과 불일치 **F-04**); 스킬 표 인자 축약(**F-18**) |
| frontmatter 유효성 | 스킬 17: `name`(디렉터리명 일치)·`description`·`argument-hint`·`disable-model-invocation`(init·release·triage·postmortem)·`allowed-tools`(monitor·metrics·status·triage·scan) — 모두 지원 필드. 에이전트 5: `name`·`description`·`model: inherit`·`tools`(`Bash(git diff *)` 식 제한 포함) — 지원 필드만, `color`/`permissionMode` 없음. `claude plugin validate --strict` 마켓플레이스·플러그인 모두 통과 |

---

## D. README / 문서 불일치 목록

| # | 위치 | 문서가 말하는 것 | 실제 | 조치 |
|---|---|---|---|---|
| D-1 | plugins/sdlc/README.md:222 | `gate.mode` 는 v1 에서 `deny` 만 | `ask` 도 구현·테스트됨 | F-10 |
| D-2 | plugins/sdlc/README.md:301 | events 종류 7개 | 30여 개 | F-10 |
| D-3 | plugins/sdlc/README.md:349 | sdlc-monitor.yml 은 "triage intent 를 커밋하는 PR" | 기본 브랜치에 직접 푸시 | F-04 |
| D-4 | plugins/sdlc/README.md:144-162 | 스킬 인자 | 축약·누락 11건 | F-18 |
| D-5 | plugins/sdlc/README.md:404 | python3 는 jq 폴백 | init/doctor/status/monitor/metrics 필수 | F-19 |
| D-6 | plugins/sdlc/README.md:246-258 | bands 예시 2σ tools 3개 | 템플릿은 6개 | 예시를 템플릿과 동일하게 |
| D-7 | plugins/sdlc/CHANGELOG.md:17 | `tests/run.sh` | `tests/hooks.sh`, `tests/scripts.sh` | F-09 |
| D-8 | plugins/sdlc/CHANGELOG.md:16, README.md:138, 마켓플레이스 README.md:53 | `docs/PLAYBOOK-MAPPING.md` 존재 | 이번 감사로 생성됨 | 없음 |
| D-9 | docs/eli5.html 하단 | "내 주방" 현황·오늘 할 일 | 다른 프로젝트의 과거 상태, 로컬 경로 | F-07 |
| D-10 | templates/github/CODEOWNERS:4 | "{{team}} 플레이스홀더를 바꾸라" | 4종 플레이스홀더 중 3종은 렌더되지 않음 | F-02 |
| D-11 | skills/init/SKILL.md:63-66 vs skills/doctor/SKILL.md:29-31 vs scripts-extra.sh | "Next steps" 순서 | 세 가지 다른 순서 | F-05 |
| D-12 | skills/lesson/SKILL.md:56-58 | 반복 실수 지표 = `review.run.repeat_finding` | metrics.py 는 `lesson.add` 수 | F-14 |
| D-13 | skills/init/SKILL.md:53 / lesson:30 vs doctor.sh:56 | 한 페이지 = 120줄 / 200줄 | 불일치 | F-13 |
| D-14 | templates/en/evals-README.md | (침묵) | 원문과 다른 eval 형식 | F-28 |
| D-15 | docs/ENTERPRISE.md:11 | 훅 7종 "`docs`: 플러그인 README 훅 표" | 링크 아닌 문구 | README 앵커 링크로 |
| D-16 | 마켓플레이스 README.md:13 | "17개 스킬·5개 에이전트·7개 훅" | 일치 | 없음 |

---

## E. 결론

- 프로세스·아티팩트·훅·게이트·측정 요구는 구현이 원문을 충실히 따르고, 테스트(243건)와 매니페스트 검증이 이를 뒷받침한다. `governance` 행 전부와 `measure` 행 전부가 COVERED 다.
- "블로그 전수 충족" 을 주장하기 전에 반드시: **F-01**(원문 예시 파일 7개 추가), **F-02**(CI 플레이스홀더 렌더 수정 — 현재 설치되는 evals·monitor·triage 워크플로와 CODEOWNERS 가 동작하지 않음).
- 그 다음 우선순위: F-03(게이트 역순 패턴), F-04(monitor PR), F-05(채택 순서 단일화), F-06(verify.run), F-07(eli5 정리), F-08(date -r).
- 문서 PARTIAL 55건은 FIX-SHIFT·FIX-PREREQ·FIX-ADOPT 세 묶음으로 대부분 해소된다.
