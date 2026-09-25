# 10 — 프롬프트 감사 (`/claude-api prompt-audit`) · 2026-09-25

## 가정 (Step 0)
- **범위**: 저장소 전체의 프롬프트 표면 — `plugins/sdlc/` 의 스킬 19 · 에이전트 5 · `skills/init/references/*` · `templates/{en,ko}/*` · 정책 스킬 · eval `prompt.md` 3 · 워크플로 7 의 `claude -p`/`claude-code-action` 프롬프트 · `scripts/monitor.py` `DIAGNOSE_PROMPT` · `scripts/run-loop.sh` 자기 점검 프롬프트 · 훅이 모델에 보내는 텍스트(session-start 컨텍스트, `block` 메시지).
- **대상 모델**: 요청에 지정 없음. 저장소 코드가 가리키는 최신 = Claude Code 기본 모델(이 세션: Claude Fable 5.1, CI `claude-code-action@v1`/`claude -p` 기본 = Opus 5 세대). evals 는 `evals.model: "sonnet"` → Sonnet 5.
- **출처(provenance)**: git 이력은 2026-09-09~10 커밋 10건, 단일 작성자 — 전부 현세대 모델용으로 작성됐고 `git blame` 으로 구세대 완화문을 가려낼 수 없다. 관용구 기반 판정만 가능(저신뢰).
- 타 벤더 마커: 없음.

## 요약
| 그룹 | 건수 | 비고 |
|---|---|---|
| 1a 압박 어조 | 0 | 대문자 MUST/NEVER/CRITICAL 0건, 완화 어휘 0건 |
| 1b/1f 수치 상한·스캐폴드 | 2 (High) + 1 flag | 출력 줄수 상한 2곳; 사고 스캐폴드·prefill·temperature 0건 |
| 1c 과잉 명세 | 0 + 1 flag | 절차 번호는 아티팩트 순서(계획→코드)가 실제로 중요한 곳에만 |
| 1d 화석 | 0 | 구모델명·narrate 억제·포맷 금지 0건 |
| 2 스킬 파일 | 2 (High) + 2 (Medium) + 1 flag | 현재 워크플로와 어긋난 규칙, 이력 서술, 고정 모델명 |
| 3 도구 설명 | 0 | 에이전트 설명 400~500자, 계약 정확, when-not 포함 |
| 4 요청 구성 | 1 (Medium) + 1 flag | 중복 진단 프롬프트; API 파라미터 화석 없음 |

가장 영향 큰 셋: ① 세션마다 주입되는 "start with /sdlc:plan" 규칙과 채택 저장소의 CLAUDE.md 에 심는 같은 문장이 0.5.x 의 실제 진입점(`/sdlc:go`, solo 는 autopilot)과 어긋남 — 모델이 매 세션 낡은 절차를 우선하게 됨. ② 두 곳의 출력 줄수 상한(연구자 25줄, go 보고 12줄)은 현세대에서 답을 잘라먹는 클램프. ③ `monitor.py` 의 진단 프롬프트와 `sdlc-diagnoser` 에이전트가 같은 일을 두 벌의 프롬프트로 하며 이미 표현이 어긋남.

## 발견 (신뢰도 순)

| # | 위치 | 근거(원문) | 패턴 | 왜 낡았나 | 신뢰 | 조치 |
|---|---|---|---|---|---|---|
| H1 | `scripts/hooks/session-start.sh:64` (+`:26` `plan_line`) | `- Nothing is implemented without an approved plan — start with /sdlc:plan` | G2 휘발성 명세(현 코드와 불일치) | 0.5.x 진입점은 `/sdlc:go`(plan.md 를 스스로 커밋, solo 는 정지 없음). 세션마다 주입되는 규칙이 문서·doctor 의 "다음 할 일"과 반대를 가리켜 모델이 낡은 절차로 회귀 | High | rewrite |
| H2 | `templates/en/CLAUDE.sections.md:32`, `templates/ko/CLAUDE.sections.md:32` | `Nothing is implemented without an approved plan: start in plan mode, commit plan.md, then build.` | G2 동일 | 채택 저장소의 CLAUDE.md 에 영구히 심기는 문장. "plan mode 로 시작"은 autopilot 경로에서 거짓 | High | rewrite (기존 채택 저장소는 init 이 덮어쓰지 않으므로 `/sdlc:lesson`·수동 수정 안내 필요) |
| H3 | `agents/sdlc-researcher.md:18` | `## Output (≤ 25 lines unless asked otherwise)` | 1b/1f 수치 출력 상한 | 현세대는 질문 크기에 맞춰 답함; 줄수 클램프는 큰 코드베이스 질문에서 위치 목록을 잘라 낸다 | High | rewrite |
| H4 | `skills/go/SKILL.md:37` | `8. **Report** in ≤ 12 lines:` | 1b/1f | 동일. 보고 항목 7개를 12줄에 맞추려 정지 사유 설명이 잘림 | High | rewrite |
| M1 | `agents/sdlc-verifier.md:9` | `(This agent is the plugin's version of the playbook's .claude/agents/verifier.md — kept verbatim in templates/examples/verifier.md — with Grep/Glob added and a fixed report shape.)` | G2 이력 서술 | 모델에게 행동 정보가 없는 출처 각주; README 대응표에 이미 기록됨 | Medium | remove |
| M2 | `skills/scan/SKILL.md:22`, `:35` | `billed on consumption at Mythos 5 rates` | G2 고정 모델명·검증일 없는 요금 주장 | 다음 모델 출시 때 조용히 틀려짐 | Medium | rewrite |
| M3 | `scripts/monitor.py:220-245` ↔ `agents/sdlc-diagnoser.md` | 두 프롬프트가 같은 5헤딩 intent 본문을 요구; 이미 표현이 다름(에이전트: 가설 2~4개 순위+차점, 프롬프트: 최유력 원인 1개) | G4 중복 전문 에이전트 | 같은 작업·같은 도구·거의 같은 프롬프트 두 벌 → 진단 품질이 경로마다 달라짐 | Medium | rewrite (monitor.py 는 사실만 넘기고 에이전트를 호출; `bands.json` 2σ tools 에 `Task` 추가) |
| F1 | 스킬 19종 `description` | 영·한 트리거 구문 6~8개 나열 | G2 트리거 열거 | 라우팅 텍스트는 보정된 강조 허용, `evals/trigger-*` 로 측정 중 | Low | flag |
| F2 | `skills/plan/SKILL.md:13-16`, `skills/verify/SKILL.md:13-16` | "Prerequisites:" 문장이 두 번 | 1c 근접 중복 | 내용이 일치 → keep-list 8(작동 중인 중복) | Low | flag |
| F3 | `templates/github/sdlc-ci-triage.yml:36` | `write a three-line summary for the PR thread` | 1f 수치 상한 | 플레이북 원문 그대로(`templates/examples/triage-step.yml` 보존이 목적), PR 코멘트 형식 민감 | Low | flag |
| F4 | `skills/review/SKILL.md` Solo mode | `ask once whether to enable auto-merge` | 범위 밖(제품 일관성) | 0.5.0 "solo 기본 자동" 원칙과 단계별 스킬의 정지 유지가 의도된 구분인지 확인 필요 | Low | flag |
| F5 | `scripts/run-evals.sh:64` | evals 기본 모델 `sonnet` | G4 측정 | 설정 회귀 evals 가 `go` 가 도는 모델과 다른 모델로 판정됨 — 지표 해석 시 유의 | Low | flag |

## 점검 후 유지(삭제 아님)
- `REVIEW.md` "at most five nits", `sdlc-review.yml` 동일 — 플레이북 정책, 기술 리드가 월간 튜닝(keep-list 3·7).
- intent "at most six" · go "≤ 2" · postmortem "≤ 5" 질문 상한 — 상호작용 의식 예산이며 이유가 붙음(제품 컨텍스트).
- lesson "≤ 160 characters" — CLAUDE.md 한 줄 형식 계약.
- `print exactly: SDLC_SELFCHECK …`, `SDLC_REVIEW_TALLY …`, monitor "write ONLY the body … exactly these five headings" — 파서가 읽는 출력 계약.
- 에이전트 Rules 의 금지문(verifier "Do not fix", simplifier "Never edit test files") — 역할 계약이며 훅/도구 목록이 강제.
- 훅 `block` 메시지 — 무엇을·왜·어떻게 풀지 형식, 계약.
- spec 스킬·`sdlc-spec-on-intent.yml` 의 플레이북 원문 프롬프트 — 원문 충실이 프로젝트 목표.
- Group 4: `thinking`·`budget_tokens`·`temperature`·prefill·`tool_choice` 없음; 모델 지정은 `claude -p` 기본에 위임; 결정론(감지·게이트)과 모델 호출(진단·리뷰)의 분리가 적절.

## 제안 diff — 본문 참조(대화 응답)
적용은 하지 않았다. H1 은 `tests/hooks.sh:443` 단언을 함께 바꿔야 한다.
