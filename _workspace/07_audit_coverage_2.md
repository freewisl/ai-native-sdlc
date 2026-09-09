# 07 — 2차 커버리지 감사 (수정 반영 재판정, 2026-09-07)

대상: 1차 감사(`05_audit_coverage.md`)가 표시한 **PARTIAL 70 + MISSING 11 = 81행**과 **F-01~F-28**.
방법: 1차 감사의 줄 번호를 신뢰하지 않고 현재 파일을 다시 열어 판정. 훅·게이트·init 렌더는 실제로 실행해 확인.
배포용 대응표 `docs/PLAYBOOK-MAPPING.md` 는 이 판정으로 갱신했다(81행 판정·근거 교체 + 집계표 재생성 + 꼬리말 서술 갱신).

실행 검증(전부 이번 세션에서 재실행):

| 항목 | 결과 |
|---|---|
| `bash plugins/sdlc/tests/run.sh` | 훅 185 passed / 0 failed · 스크립트 85 passed / 0 failed · monitor selftest 7/7 PASS · ALL SUITES PASSED |
| `claude plugin validate plugins/sdlc --strict` | Validation passed |
| `claude plugin validate . --strict` (마켓플레이스) | Validation passed |
| `init.sh --dir <tmp> --github` (`.github/workflows/ci.yml` 에 `name: CI` 있는 저장소) | created=27, `.github/` 아래 `{{…}}` 잔여 0, `.sdlc/examples/` 15파일, `.sdlc/APPROVALS.md` 생성 |
| `init.sh --marketplace acme/sdlc-mkt --ci-workflow "Build and test" --team @acme/platform` | 세 값 모두 렌더 반영 |
| `guard-bash.sh` 역순 배포어 3종(미초기화 프로젝트 기본값) | `production deploy` · `./production-deploy.sh` · `prod-deploy.sh` 전부 exit 2 |
| `guard-edit.sh` ticketed 경로 | `CHANGE_TICKET` 없으면 exit 2, 있으면 exit 0 |
| `doctor.sh` (신규 init 프로젝트) | ✓19 △10 ✗0, 훅 자가시험 "6/6 probes behaved (frozen path, secret, production gate both word orders)", `auto mode readiness` 행 출력 |
| `status.sh` · `metrics.py` (동일 프로젝트) | 정상 종료, 회귀 없음 |

---

## A. 새 판정 집계

| 구간 | 행 | COVERED | PARTIAL | MISSING | EXTERNAL-DOCUMENTED | N/A |
|---|---|---|---|---|---|---|
| H.INTRO | 8 | 8 | 0 | 0 | 0 | 0 |
| H.SHIFT | 7 | 7 | 0 | 0 | 0 | 0 |
| H.ART | 10 | 9 | 1 | 0 | 0 | 0 |
| H.DEP | 12 | 12 | 0 | 0 | 0 | 0 |
| H.SOT | 7 | 7 | 0 | 0 | 0 | 0 |
| H.PERM | 16 | 16 | 0 | 0 | 0 | 0 |
| H.MCP | 6 | 4 | 0 | 0 | 2 | 0 |
| H.OTEL | 6 | 6 | 0 | 0 | 0 | 0 |
| **횡단 소계** | **72** | **69** | **1** | **0** | **2** | **0** |
| Stage 1 | 30 | 28 | 0 | 0 | 2 | 0 |
| Stage 2 | 21 | 20 | 0 | 0 | 1 | 0 |
| Stage 3 | 79 | 79 | 0 | 0 | 0 | 0 |
| Stage 4 | 35 | 34 | 1 | 0 | 0 | 0 |
| Stage 5 | 48 | 48 | 0 | 0 | 0 | 0 |
| Stage 6 | 46 | 44 | 0 | 0 | 2 | 0 |
| X | 18 | 18 | 0 | 0 | 0 | 0 |
| **합계** | **349** | **340 (97.4%)** | **2 (0.6%)** | **0** | **7 (2.0%)** | **0** |

1차 대비 이동: COVERED 261 → 340 (+79), PARTIAL 70 → 2 (−68), MISSING 11 → 0 (−11).
재판정한 81행 중 **79행이 COVERED 로 올라갔고 2행이 PARTIAL 로 남았다**. MISSING 11건은 전건 해소되었다.

---

## B. 아직 PARTIAL 인 행 (2건)

| R-ID | 요구 | 판정 | 정확히 남은 것 | 수정 |
|---|---|---|---|---|
| R-H.ART.8 | 플레이 문서 구조(What changes · Getting started · Steps · Governance · Measure) | PARTIAL | `## What changes`(Traditional/AI-native 원문 문장) 블록이 플레이 스킬 **9종**에만 있다 — intent:9 · spec:9 · plan:9 · verify:9 · review:9 · release:10 · monitor:10 · scan:10 · postmortem:10. 원문에 What changes 가 있는 플레이 중 **CLAUDE.md**(스킬 `init`·`lesson`) · **Skills as institutional knowledge**(`policy`) · **Continuous evals in CI**(`evals`) · **Hooks as build-time guardrails** · **Hooks as approval gates** 는 블록이 없다. README 전환표(25-32)는 스테이지 6행 단위이므로 이 다섯 플레이의 두 열 문장을 대체하지 못한다. `Prerequisites:`/`Infrastructure:` 는 스킬 14종에 있어 이 부분은 충족 | `skills/init/SKILL.md`(또는 `lesson`) · `policy` · `evals` 세 SKILL.md 의 제목 아래에 원문 해당 플레이의 What changes 두 줄을 추가하고, 스킬이 없는 훅 두 플레이의 두 줄은 README "훅 7종" 절 서두에 넣는다. 형식은 이미 있는 9종과 동일(`- **Traditional:** …` / `- **AI-native:** …`) |
| R-4.2.12 | `evals/check.sh <eval.json> result.json` | PARTIAL | 플러그인의 eval 체커 계약은 케이스별 `check.sh <workdir>` 이고 원문의 스위트 수준 시그니처를 받는 어댑터는 없다. 차이와 이유는 `templates/en\|ko/evals-README.md:18` 과 `templates/github/sdlc-evals.yml:7-9` 가 명시하고 원문 워크플로는 `templates/examples/agent-evals.yml` 에 그대로 보존되어 있으므로 **의도된·문서화된 편차**다 | 그대로 두어도 무해하다. 원문 계약까지 충족하려면 `templates/evals/check.sh` 어댑터(인자 `<eval.json> <result.json>`, `eval.json` 의 `checks[]` 를 읽어 `result.json` 을 검사하고 exit 0/1)를 추가하고 `evals-README.md` 에 "원문 형식으로 쓰인 기존 스위트를 그대로 돌리려면 이 어댑터를 쓴다" 한 줄을 붙인다 |

---

## C. F-01 ~ F-28 재판정

심각도는 1차 감사 기준(P0 = 전수 충족 주장을 막음).

| # | 심각도 | 결과 | 근거 |
|---|---|---|---|
| F-01 원문 코드 블록이 파일로 재현되지 않음 | P0 | **FIXED** | `templates/examples/` 에 14블록 + `README.md` 대응표. 원문 텍스트와 직접 diff 하여 **전부 문자 단위 일치** 확인(intent.md · plan.md · CLAUDE.md "Payments service" · secure-api-review.SKILL.md · verifier.md · CLAUDE.verifying.md · settings.hooks.json · production-gate.sh(실행 비트 O) · managed-settings.json · triage-step.yml · agent-evals.yml · REVIEW.md · bands.yaml · stage2-prompt.txt). `scripts/init.sh:162-166` 이 `.sdlc/examples/` 로 복사하고 `tests/scripts.sh:105` 가 이를 검증 |
| F-02 설치되는 CI 파일에 치환되지 않는 플레이스홀더 | P0 | **FIXED** | `init.sh --help` 에 `--marketplace`/`--ci-workflow`/`--team` 신설. 실측: `--github` 만으로도 `marketplace add TODO-org/ai-native-sdlc`, `workflows: ["CI"]`(저장소의 첫 `name:` 자동 감지), CODEOWNERS 10행 전부 `@TODO-org/TODO-team` 로 렌더 — `.github/` 아래 미치환 `{{…}}` 0건. 값을 주면 그대로 반영. `doctor.sh:210,215` 가 워크플로·CODEOWNERS 의 `{{…}}`/`TODO` 를 경고, `tests/scripts.sh:95` 가 회귀 방지 |
| F-03 production 게이트 정규식이 단어 순서를 요구 | P1 | **FIXED** | `templates/config/config.json` production `patterns` 에 역순 `(prod\|production\|live)[^\|;&]*(deploy\|release\|rollout\|promote)` 추가. 미초기화 프로젝트(내장 기본값 경로)에서도 `production deploy`·`./production-deploy.sh`·`prod-deploy.sh` 3종 실측 차단. `tests/hooks.sh:207` 4케이스, `doctor.sh` 자가시험이 "both word orders" 로 표기 |
| F-04 `sdlc-monitor.yml` 이 기본 브랜치에 직접 푸시 | P1 | **FIXED** | `templates/github/sdlc-monitor.yml:31-44` 가 `peter-evans/create-pull-request@v6`(branch `sdlc/monitor-${{ github.run_id }}`, add-paths `.sdlc/history/**` + `{{intent_dir}}/triage/**`, committer/author `sdlc-monitor`). 14행에 "Nothing is pushed to the default branch" 명시. `git push` 문장 0건 |
| F-05 채택 순서가 세 곳에서 다르고 그림과도 다름 | P1 | **FIXED** | `scripts/lib/scripts-extra.sh:239-306` 이 0~5층으로 재작성되고 244행이 "This function is the single source of that order" 를 선언. 1층에 **Plan mode** 포함(276), 2층에 **Skills·Subagents·Evals** 신설(278-284), git/init 은 0층(262-264). `skills/init/SKILL.md:66` 과 `skills/doctor/SKILL.md:29` 는 자체 순서를 삭제하고 "single source" 로 위임. README "채택 순서" 절(146-168)이 5층 표 + 원문 텍스트↔그림 불일치 4건 병기 |
| F-06 `verify.run` 이벤트 이중 발행으로 통과율 왜곡 | P1 | **FIXED** | `scripts/metrics.py:361` 이 `decision in ("pass","allow")` 를 성공으로 집계하고 주석으로 두 어휘를 설명. `skills/verify/SKILL.md:79` 가 `pass`/`fail` 을 쓰며 "the same vocabulary the post-bash hook uses, so metrics count both" 를 명시. `tests/scripts.sh:141` 이 두 종류 섞인 events 로 0.67 을 검증 |
| F-07 `docs/eli5.html` 이 낡고 조직 특정 내용을 노출 | P1 | **FIXED** | 하단 절(284-293)이 `/sdlc:doctor`·`/sdlc:init` 안내로 재작성되어 "어느 저장소에서든" 이 되었다. 297줄 전체에서 `/Users/`·사내 Nexus 도메인·`jdk`·`jsp`·`_workspace`·`starter/`·다른 프로젝트 경로 **0건**. "오늘 할 일" 절과 "skills 11종·agents 13종" 류 현황 수치도 사라졌다(비유 제목 "내 주방" 은 조직 특정 정보가 아니어서 유지) |
| F-08 `status.sh` 의 `date -u -r` 는 BSD 전용 | P1 | **FIXED** | `scripts/status.sh:40-43` `fmt_date` 가 `date -u -r` → `date -u -d "@$1"` → `-` 순 폴백. `tests/scripts.sh:133` 이 날짜 열이 `2026-02-0…` 임을 확인 |
| F-09 CHANGELOG 가 존재하지 않는 `tests/run.sh` 를 언급 | P1 | **FIXED** | `plugins/sdlc/tests/run.sh` 신설(hooks → scripts → validate 순차 실행, 실측 ALL SUITES PASSED). `CHANGELOG.md:22` 가 185/85 로 정확 |
| F-10 README 설정 표의 `gate.mode` 설명이 구현과 다름 | P1 | **FIXED** | `README.md:305` "`deny`(exit 2, 기본) 또는 `ask`(사람 확인 프롬프트; 비대화형 `-p` 에서는 거부됨)". `README.md:402` 이벤트 목록을 전수로 교체 — 스크립트가 실제로 내보내는 16종(`session.start/end` `hook.gate/guard/stop` `verify.run` `eval.run/suite` `band.check/breach` `monitor.diagnose/intent/propose/pr/runbook/report`)이 전부 포함되고 스킬 이벤트 14종도 나열 |
| F-11 문서 누락 묶음 | P1 | **FIXED** | 참조 링크 2건 → README "더 읽기"(546-547). Claude Security 전제·프로젝트 조직·내보내기 → `skills/scan/SKILL.md:22,34-41`. 병렬 세션 → README "병렬 세션과 서브에이전트"(223-238). 읽기 전용 스텝 2종 → `sdlc-ci-triage.yml:44-63`. 닫힌 루프 예시 3개 → README:473-476 + `bands.json` 2예시. 보안 병목 근거 → `skills/scan/SKILL.md:16`. 출처 저자 → `README.md:3` |
| F-12 테스트 잠금 해제 안내가 스킬과 훅 사이에서 모순 | P2 | **FIXED** | `guard-edit.sh:94` 메시지가 "only if the test itself is wrong or new behavior needs new tests" 를 앞세우고 (1) plan 의 `test_changes: allowed` → (2) `/sdlc:verify --bugfix` 의 `.sdlc/state/allow-test-edit` → (3) "as a last resort … `SDLC_ALLOW_TEST_EDIT=1`" 순. `session-start.sh:50` 과 README:521 도 같은 순서 |
| F-13 "한 페이지" 임계치 불일치 | P2 | **FIXED** | `scripts/doctor.sh:56` 이 `claude_md.max_lines` 를 읽고 기본 120. `templates/config/config.json` `claude_md.max_lines: 120`, README:318 |
| F-14 리뷰 이벤트에 `policy_findings` 가 없어 지표가 항상 0 | P2 | **FIXED** | `skills/review/SKILL.md:80` printf 에 `policy_findings` 추가. `metrics.py:351` 이 "Mistakes repeated" 를 `review.*` 의 `repeat_finding: true` 로 집계(lesson.add 카운트 아님), `:354` 가 `policy_findings` 를 합산. 잔여 나이트: `agents/sdlc-reviewer.md:23` 의 집계 키는 `repeat_findings`(복수)로 스킬·metrics 의 `repeat_finding` 과 이름이 다르다 — 이벤트를 쓰는 주체가 스킬이므로 기능 영향은 없다 |
| F-15 한국어 CLAUDE.md 스텁이 lesson 1건으로 계산 | P2 | **FIXED** | `metrics.py:226` 이 `/sdlc:lesson` 과 `(아직 없음` 을 포함한 줄도 제외 |
| F-16 `sdlc-spec-on-intent.yml` 이 경로를 하드코딩 | P2 | **FIXED** | 템플릿이 `{{default_branch}}`·`{{intent_dir}}`·`{{spec_dir}}` 를 8곳에서 사용(8,9,24,40,41,42,56). `sdlc-monitor.yml:41,44` 도 `{{intent_dir}}` 사용 |
| F-17 스킬 frontmatter `allowed-tools` 의 `${CLAUDE_PLUGIN_ROOT}` 치환 미검증 | P2 | **OPEN(미검증)** | `skills/monitor`·`metrics`·`status/SKILL.md` 가 여전히 `Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*")` 를 쓴다. 실제 세션의 사전 승인 매칭 여부는 이 감사에서 확인할 수 없다(정적 검사·`plugin validate` 로는 드러나지 않음). 실패해도 권한 프롬프트가 한 번 뜨는 정도이므로 차단은 아니다. 조치: 실세션에서 한 번 확인하거나 `Bash(python3 *)`/`Bash(bash *)` 로 완화 |
| F-18 README 스킬 표가 인자를 축약 | P2 | **FIXED** | README:205-221 의 17행을 각 SKILL.md 의 `argument-hint` 와 1:1 대조 — 전 항목 일치(intent 의 `--from-scan`/`approve`/`reject`, plan 의 `--allow-test-changes`/`--no-implement`, verify 의 `--ui`/`--no-agent`, review 의 `--base`/`--slug`/`--post`, release 의 `rollback`, monitor 의 `add`, lesson·scan·postmortem·policy·evals 포함). 1차가 제안한 자동 일치 테스트는 없으나 현 상태는 정합 |
| F-19 python3 가 사실상 필수인데 README 는 폴백으로만 설명 | P2 | **FIXED** | README:58-65 요구사항 표가 python3 3.9+ 를 **필수**로, jq·gh·claude 를 선택으로 구분. FAQ:528 이 "python3 는 폴백이 아니라 **필수**" 로 정정 |
| F-20 테스트의 잡음 오류 | P2 | **FIXED** | `tests/scripts.sh:120` 이 `mkdir -p "$C/.sdlc/state"` 를 선행. 실행 로그에 "No such file or directory" 없음 |
| F-21 `--example secure-api-review` 복사본에 플레이스홀더 잔존 | P2 | **FIXED** | `skills/policy/SKILL.md:21` "`--example` copies the secure-api-review skill and its check script, then fills its three header placeholders (`{{policy_owner}}` from `--owner`, `{{policy_source}}` from `--source`, `{{date}}` = today) — ask for owner/source when not given" |
| F-22 `evals/README.md` 의 `claude plugin eval` 플래그 미검증 | P2 | **OPEN(연기, 의도적)** | `plugins/sdlc/evals/README.md:11` 이 early access 로 실행 불가임을 명시하고 "ready-to-run once the feature is enabled" 로 표기. 1차 감사의 처분("기능 개방 후 재확인 항목으로 남긴다")대로이며 이 환경에서 해소 불가 |
| F-23 `metrics.py` 의 `… if False else …` 죽은 분기 | P2 | **FIXED** | `if False` 문자열 0건 |
| F-24 CLAUDE.md 섹션 탐지 휴리스틱 | P2 | **FIXED** | `scripts-extra.sh:70-84` 가 `^#{1,3}[[:space:]]+.*\bcommands?\b` 식으로 제목 어디에든 매치하고 `<!-- sdlc:begin … -->` 마커를 우선 검사 |
| F-25 PostToolUse 매처가 NotebookEdit 을 빠뜨림 | P2 | **FIXED** | `hooks/hooks.json:11,17` 이 PreToolUse·PostToolUse 모두 `Edit\|Write\|MultiEdit\|NotebookEdit`. `tests/hooks.sh:60,173` 이 NotebookEdit 페이로드를 검증 |
| F-26 롤백 명령 "항상 허용" 이 부분문자열 매치 | P2 | **FIXED** | `guard-bash.sh:23` `case "$cmd" in "$rb"\|"$rb "*)` 로 앵커. `tests/hooks.sh:225-228` 이 "롤백 명령 자체는 통과 / 롤백 경로를 언급만 한 체인 명령은 여전히 게이트" 두 케이스를 고정 |
| F-27 plan 제목 괄호와 intent Author 줄의 원문 차이 | P2 | **PARTIALLY FIXED** | intent 쪽은 해소 — `templates/en\|ko/intent.md:14` 가 `Author: {{author}} ({{team}})`, `artifact-conventions.md:109` 가 미지정 시 괄호 삭제 규칙까지 규정. plan 쪽은 미해소 — `templates/en/plan.md:13` 이 여전히 `(from {{spec_path}})`(원문은 `(from intent.md <date>)`)이고 그 편차를 적은 주석·문서가 없다(`templates/examples/README.md` 의 plan 행은 "+ Departures from plan" 만 언급). 조치: 템플릿 13행 위에 한 줄 주석 또는 examples/README 의 plan 행에 "제목 괄호는 파생 아티팩트 경로로 대체" 추가 |
| F-28 evals 형식이 원문과 다른데 어디에도 명시되지 않음 | P2 | **FIXED** | `templates/en\|ko/evals-README.md:18` 의 "Format note" 한 문단(차이·이유·원문 보존 위치·주기 실행 변형)과 `sdlc-evals.yml:7-9` 헤더 |

집계: **FIXED 24 · PARTIALLY FIXED 1(F-27) · OPEN 3(F-17 미검증, F-22 외부 의존으로 연기)**.
P0 두 건(F-01·F-02)은 모두 FIXED 이고 실행으로 확인했다.

---

## D. 회귀 점검

크게 손댄 파일(`README.md`, `skills/plan`, `skills/init`, `init.sh`, `doctor.sh`, `guard-edit.sh`, `config.json`)에 걸린
1차 COVERED 행 15건을 다시 확인했다. **요구사항 회귀는 0건**이다.

| 확인 | 결과 |
|---|---|
| R-3.1.* 플랜 모드 4절 | `skills/plan/SKILL.md:29,35-39` EnterPlanMode + Files that change / Order of work / Risks / Proof 유지 |
| R-H.ART.1 각 단계가 커밋으로 끝남 | intent·spec·plan 세 스킬 모두 커밋 단계 유지 |
| R-H.ART.4·5·6 (승인=커밋 · 승인자 열 · 트리거 맵) | README 절 위치만 이동(424-428 · 203-221 · 412-420), 내용 유지 |
| R-H.SHIFT.7 채택 스펙트럼 | README:422 유지 |
| R-3.3.* CLAUDE.md 5섹션 | `scripts-extra.sh:90` `SDLC_CLAUDE_SECTIONS` 5개 유지, doctor 가 5/5 보고 |
| R-3.5.* guard-edit frozen/generated·비밀 | ticketed 분기를 (1b) 로 삽입했음에도 훅 테스트 185건 전부 통과 |
| R-H.PERM.* 권한 템플릿 | `settings.project.json` allow 11 / deny 6 유지, 기존 프로젝트 병합 테스트(`tests/scripts.sh:88`) 통과 |
| R-H.OTEL.* | `templates/config/otel.env` 메트릭·이벤트·관리형 등가 블록 유지 |
| R-6.1.* Western Electric | `monitor.py:28-35` R1~R4 정의 유지, selftest 7/7 |
| R-5.2.* 릴리스 게이트 | `skills/release/SKILL.md:25,46` `approval_env`/`RELEASE_APPROVAL` 유지 |
| 신규 config 키 3종 | `doctor.sh` 스키마 검사가 `per_artifact`·`ticketed_paths`·`lint_file`·`max_lines` 에 경고 없음 |
| `init.sh` 비파괴성 | 기존 파일 SKIPPED·CLAUDE.md 섹션 append·settings 합집합·gitignore 부분 추가 전부 테스트 통과 |
| `status.sh`·`metrics.py` | 신규 프로젝트에서 정상 종료, Stage 1~6 표 출력 |
| 매니페스트 | 플러그인·마켓플레이스 `--strict` 양쪽 통과 |
| 스킬 수 | 17종 유지(마켓플레이스 README:13 표기와 일치) |

수정 과정에서 새로 생긴 **문서 부스러기 5건**(요구사항 판정에는 영향 없음, 배포 전 정리 권장):

1. `plugins/sdlc/README.md:542` FAQ 가 아직 "훅 매트릭스(172건) · 스크립트 스위트(71건)" 라고 적는다. 실제·CHANGELOG 는 **185/85**.
2. `plugins/sdlc/README.md:37-54` 목차 번호가 5,6,7,6,7,8… 로 중복된다(항목 2개를 삽입하며 재번호를 놓침).
3. `plugins/sdlc/skills/intent/SKILL.md:17` 과 `:19` 에 `Prerequisites:` 줄이 **두 번** 있다(17행 "none (the first play in the dependency graph)", 19행 "None.").
4. `plugins/sdlc/skills/plan/SKILL.md:85-86` 의 auto mode 한 줄이 새로 쓴 73-78 체크리스트와 중복된다.
5. 마켓플레이스 `README.md:3` 에는 저자·감사 표기가 없다(플러그인 `README.md:3` 에만 있다). CODEOWNERS 의 `spec/` 행은 `tech_lead` 와 `product_owner` 가 같은 기본값이라 오너가 두 번 적힌다(문법상 무해).

---

## E. 결론 — "블로그 전수 충족" 을 주장할 수 있는가

**주장할 수 있다. 단서는 두 줄이다.** 349행 중 340행이 COVERED, 7행은 Anthropic 호스팅 제품이라 플러그인이 구현할 수 없고 절차와 로컬 대체로 안내하며(EXTERNAL-DOCUMENTED), MISSING 은 0 이다. 전수 충족을 막던 P0 두 건은 실행으로 확인해 닫혔다 — 원문 코드 블록 14종은 `templates/examples/` 에 문자 단위로 보존되고(원문 텍스트와 직접 diff), `--github` 로 설치되는 evals·monitor·triage 워크플로와 CODEOWNERS 는 미치환 플레이스홀더 없이 렌더된다. P1 여섯 건(게이트 역순 패턴, monitor 의 PR 경로, 채택 순서 단일화, verify 이벤트 어휘, eli5 조직 특정 내용, GNU date)도 모두 닫혔고 각각 회귀 테스트가 붙었다. 남은 PARTIAL 2건은 결함이 아니라 **문서화된 편차**다 — What changes 블록이 훅·CLAUDE.md·Skills·evals 다섯 플레이에서 스킬 본문 대신 README 전환표로 대체되어 있고(R-H.ART.8), eval 체커 시그니처가 원문의 스위트 단위 대신 케이스 단위다(R-4.2.12, 이유와 원문 보존 위치를 `evals-README.md` 가 명시). 남은 실질 위험은 감사로 좁힐 수 없는 것 하나뿐이다: 스킬 frontmatter 의 `${CLAUDE_PLUGIN_ROOT}` 치환(F-17)은 실세션에서만 확인되고, 실패해도 권한 프롬프트가 한 번 더 뜨는 수준이다. 배포 전에 할 일은 R-H.ART.8 의 What changes 다섯 줄, F-27 의 plan 제목 주석 한 줄, 그리고 D 절의 문서 부스러기 5건 — 전부 서술 수정이며 코드 변경은 없다.
