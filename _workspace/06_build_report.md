# 06 — 빌드·검증 보고 (리더 직접 수행분 포함) — 2026-09-07

## 경위
- 09-06 밤 팀 에이전트 5개(builder-hooks/scripts/skills/docs, design-auditor)가 병렬 착수 → 전원 "session limit" 으로 중단. 디스크에 남은 산출물: 훅 7종+테스트 하네스, init/doctor/status, 스킬 10종, README·ko 템플릿·CHANGELOG·LICENSE.
- 09-07 05:28 재개, 리더가 나머지를 직접 구현: run-evals.sh, monitor.py, metrics.py, GitHub 워크플로 6종+CODEOWNERS+otel.env, 스킬 7종(status·metrics·monitor·triage·scan·postmortem·policy), 에이전트 5종, tests/scripts.sh, docs/ENTERPRISE.md, 플러그인 evals 4건. 커버리지 감사는 에이전트(coverage-auditor) 1개만 투입.

## 발견·수정한 결함
| # | 위치 | 결함 | 수정 |
|---|---|---|---|
| 1 | scripts/lib/common.sh `json_get` | jq `// empty` 가 `false` 를 null 취급 → `protect_tests: false` 등 false 설정을 관찰 불가 | `if . == null then empty else . end` 로 교체 |
| 2 | tests/hooks.sh `fresh()` | `$(fresh)` 서브셸에서 카운터 증가 → 모든 케이스가 `p1` 공유, 상태 누적으로 23~25건 오탐 | `mktemp -d` 로 케이스별 고유 디렉터리 |
| 3 | templates/config/config.json | staging 패턴이 deploy/release 동사만 → `helm upgrade … staging` 미인식 | kubectl/helm/terraform 패턴을 production 과 대칭으로 추가 |
| 4 | tests/hooks.sh | 포매터 타임아웃 케이스 `sleep 5 <file>` 이 즉시 실패, 타이밍 검사가 의도적 1초 케이스를 셈, python 폴백 검사가 셸 내장 `command -v` 의 PATH 임시 대입에 의존 | `sh -c "sleep 5" x`, 타임아웃 케이스 제외, `env PATH=… bash -c` 로 검사 |
| 5 | scripts/monitor.py | 히스토리 파일명을 slugify(`eval-pass-rate`) → run-evals 가 쓰는 `eval_pass_rate.jsonl` 과 불일치 | 메트릭 이름 그대로(경로 불가 문자만 치환) |
| 6 | scripts/metrics.py | `git log --follow` 가 내용이 비슷한 작은 아티팩트를 rename 으로 오인 → intent→spec 간격 0h | `--follow` 제거 |
| 7 | scripts/run-evals.sh `wait_capped` | `local a=$1 b=$((a*2))` — 산술이 대입 전에 확장되어 unbound variable | 대입과 산술 분리 |
| 8 | 중첩 `claude -p` | stdin 미지정 시 3초 대기 경고 | `< /dev/null` / `stdin=DEVNULL` |

## 검증 증거
| 검증 | 결과 |
|---|---|
| `claude plugin validate --strict` (플러그인·마켓플레이스) | 통과 |
| `bash tests/hooks.sh` — 훅 7종 × 시나리오(frozen/generated/test-lock/plan-required/secret/gate/commit-scan/uninitialized/post-edit/post-bash/stop/session/garbage stdin/python 폴백/타이밍) | 172/172 PASS (PATH bash 5.x 및 `/bin/bash` 3.2.57 양쪽) |
| `bash tests/scripts.sh` — 구조·init(빈 디렉터리 idempotent, 기존 프로젝트 비파괴 병합, dry-run, ko)·doctor·status·metrics·run-evals dry·monitor selftest/dry | 71/71 PASS |
| `python3 scripts/monitor.py --selftest` — Western Electric R1~R4·none·기준선 부족·zero-variance | 7/7 PASS |
| 실제 `claude --plugin-dir … -p "/sdlc:status"` (sonnet) — 스킬 로드·`${CLAUDE_PLUGIN_ROOT}` 치환·상태표 생성 | 성공, $0.06 |
| 실제 `run-evals.sh --case respect-*` (sonnet, 플러그인 훅 적용) — 중첩 에이전트가 frozen 경로를 편집하지 않음 | PASS 62s $0.17, results/summary/events/history 생성 |
| CI 템플릿 6종 YAML 구문 (ruby yaml) | 전건 파싱 OK |
| `claude plugin eval` (플러그인 트리거 평가 4건) | **실행 불가 — 이 계정에서 early access**. case.yaml 은 CLI 바이너리에서 추출한 스키마를 따름 |

## 미해결·주의
- 플러그인 evals 는 얼리 액세스 해제 후 실행 필요.
- `claude-code-action@v1` 워크플로·Claude Security·Claude Tag 는 GitHub/Slack/Enterprise 계정 전제 — 로컬에서 실행 검증 불가(문법 검증만).
- 팀 에이전트 정체/한도 중단 전례 3회(09-06 ×2, 09-07 ×1) — 대량 작업은 리더 직접 수행이 안정적.

## 1차 커버리지 감사(05) 반영 — 2026-09-07

집계: COVERED 261 · PARTIAL 70 · MISSING 11 · EXTERNAL 7 (349). P0 2 · P1 9 · P2 17 · 문서 불일치 16 → **전건 반영**.

| 항목 | 조치 |
|---|---|
| F-01 원문 코드 블록 미재현 | `templates/examples/` 14종(intent·plan·CLAUDE.md·verifier·검증 블록·REVIEW·settings.hooks·production-gate.sh·agent-evals.yml·triage 스텝·bands.yaml·Stage2 프롬프트·secure-api-review·관리형 설정) 원문 그대로 보존, `init` 이 `.sdlc/examples/` 로 복사, README 표로 일반화 대응 관계 명시 |
| F-02 CI 플레이스홀더 미치환 | `init.sh --marketplace/--ci-workflow/--team`, CI 워크플로 `name:` 자동 감지, CODEOWNERS 4종 오너 렌더, 미치환 시 NOTE, doctor 경고, 테스트 4건 |
| F-03 production 역순 | 역순 패턴 추가(템플릿·픽스처), doctor probe `./production-deploy.sh`, 훅 테스트 4명령 |
| F-04 monitor 직접 푸시 | `create-pull-request` 로 PR 생성 |
| F-05 채택 순서 3종 불일치 | `sdlc_next_steps` 를 그림의 0~5층으로 재작성(단일 출처), init/doctor 스킬은 스크립트 출력을 그대로 표시, README "채택 순서" 절 + 텍스트/그림 불일치 4건 병기 |
| F-06 verify 이벤트 이중 발행 | 스킬 `pass|fail` 로 통일, metrics 는 pass/allow 모두 성공 처리, 테스트 |
| F-07 eli5 조직 특정 | "내 주방"·"오늘 할 일" 절 제거 → 범용 안내로 교체(경로·Nexus·JDK·JSP 0건) |
| F-08 `date -r` | GNU `date -d @` 폴백, 테스트 |
| F-09/F-10 | `tests/run.sh` 실재, README gate.mode `ask` 반영, 이벤트 전체 목록 |
| F-11 문서 묶음 | README: 저자, 서론 3문장+동의어, 전환표 6행, 요구 사항 표, 병렬 세션 절, 훅 3층, 닫힌 루프 절(체인·신뢰 게이트·예시 3), 더 읽기(참조 2건), 스킬 인자 재생성; 스킬 9종 What changes, 13종 Prerequisites/Infrastructure; scan 의 Claude Security 체크리스트·내보내기·보안 병목 근거 |
| F-12~F-28 | 훅 해제 안내 순서, 120줄 임계치(`claude_md.max_lines`), review `policy_findings`+metrics `repeat_finding`, ko 스텁 제외, spec-on-intent 경로 플레이스홀더·커미터 신원, README 인자, python3 필수 표기, 테스트 mkdir, policy 플레이스홀더 치환, evals 형식 차이 명시, 죽은 분기, 섹션 탐지 완화, PostToolUse NotebookEdit, 롤백 앵커 매치, 템플릿 주석 |
| B 절 추가 | `APPROVALS.md`(en/ko) 승인 등록부, `protect.ticketed_paths`+`CHANGE_TICKET` 게이트(훅 테스트 7건), `source_of_truth.per_artifact`, `commands.lint_file`, bands 예시 2종(`enabled:false`)+`report` 라우트, `review-gate.sh`, ci-triage 선택 잡(flaky·changelog), metrics 신규 행 |

재검증: 훅 185/185 · 스크립트 85/85 · selftest · validate 통과. 2차 감사(coverage-auditor-2) 진행 중 → `07_audit_coverage_2.md`.

## 2차 커버리지 감사(07, Opus 재시도) — 2026-09-07
집계: COVERED 340 → 잔여 반영 후 **341** · PARTIAL 1(R-4.2.12, 문서화된 의도적 편차: eval 체커가 케이스 단위) · MISSING 0 · EXTERNAL-DOCUMENTED 7(호스팅 제품). 결함 F-01~F-28: FIXED 24 → 잔여(R-H.ART.8 What changes 5곳, F-27 plan 제목 주석, 문서 부스러기 5건) 반영 완료. 미검증 2건: F-17(스킬 frontmatter `allowed-tools` 의 `${CLAUDE_PLUGIN_ROOT}` 치환 — 실세션에서만 확인 가능, 실패 시 권한 프롬프트 1회 추가), F-22(`claude plugin eval` early access). 회귀 0. 최종 스위트: 훅 185/185 · 스크립트 85/85 · selftest 7/7 · validate 통과.

## 설치 검증 — 2026-09-07
- `claude plugin marketplace add /Users/brad/project/mma/sdlc` + `claude plugin install sdlc@ai-native-sdlc` (프로필 `~/.claude-work`, `~/.claude` 양쪽).
- **결함 발견·수정**: `plugin.json` 의 `"hooks": "./hooks/hooks.json"` 이 기본 자동 로드와 중복되어 "Duplicate hooks file detected … failed to load" — `validate --strict` 와 `--plugin-dir` 스모크는 잡지 못했고 설치 후 `plugin list` 에서 드러남. 키 제거 후 `Status: ✔ enabled`. 회귀 테스트 추가(tests/scripts.sh).
- **정정**: 로컬 경로 마켓플레이스도 설치본은 `plugins/cache/ai-native-sdlc/sdlc/<version>/` 스냅숏이다. 소스 변경은 **버전을 올리고** `claude plugin marketplace update` + `claude plugin update` 를 거쳐야 반영된다(0.1.0 → 0.2.0 으로 실측). 열린 세션은 `/reload-plugins`.
- **F-17 해소(실측)**: doctor 스킬에 `allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/*")` 를 추가하고 설치본으로 `claude -p "/sdlc:doctor"` 실행 → 권한 프롬프트 없이 스크립트 실행(`permission_denials: []`, $0.08). frontmatter 에서도 `${CLAUDE_PLUGIN_ROOT}` 가 치환됨을 확인. init·evals 스킬에도 동일 선언 적용(status·metrics·monitor 는 기존). 최종 스위트: 훅 185 · 스크립트 86 · selftest · validate 통과, 플러그인 `Status: ✔ enabled`(두 프로필).

## 사용자 피드백 반영 — GitHub 인프라 자동화 (2026-09-07)
- 사용자 지적: 시크릿·브랜치 보호를 손으로 클릭하는 것이 번거롭다 → `scripts/github-setup.sh` 신설(gh api 로 secret set + ruleset POST/PUT, 코드 오너 리뷰 미지원 플랜이면 자동 재시도, 1인 저장소 admin bypass), `init.sh --auth api|oauth`(워크플로 인증 시크릿 렌더), init 스킬 연결, doctor 행 추가, README 반영. 블로그는 이 부분을 인프라 전제로만 기술(자동화 없음).

## 0.2.0 배포 — 2026-09-07
- github-setup.sh(--login/--save-token/토큰 자동 해석), init --auth/--solo, solo 모드 스킬, allowed-tools, doctor ruleset 행. 두 프로필 모두 0.2.0 으로 갱신·enabled 확인, 캐시에 새 파일 존재 확인. 스위트: 훅 185 · 스크립트 95 · validate 통과.


## 0.3.1 (2026-09-07)
최종 검토 08 의 30건 전부 반영(상세 `09_final_review_fixes.md`). 훅 206 · 스크립트 120 · validate PASS. 양 프로파일 0.3.1 배포.

## 0.3.2 (2026-09-07)
파일럿 저장소 실상태 점검 후속 5건(09 후속 표). 훅 206 · 스크립트 134 · validate PASS. 양 프로파일 0.3.2 배포.

## 0.3.3 (2026-09-07)
문서 전면 재정렬 — "init 한 번, go 한 줄" 중심. 마켓플레이스·플러그인 설명문(한국어), 플러그인 README 빠른 시작 재작성·설치 명령(<조직>/ai-native-sdlc)·CI 인증(ci.auth)·github-setup 문단·FAQ·제한, 루트 README, ENTERPRISE, PLAYBOOK-MAPPING(206/134), ELI5 두 페이지, doctor 다음 할 일 1항. 테스트 206/134, 양 프로파일 0.3.3.

## 0.4.0 (2026-09-09)
`/sdlc:run` 무인 루프 신설 — run-loop.sh(큐→새 세션 go→머지→자기 점검, 상한·정지 파일·slug 차단), loop.* 설정, sdlc-autopilot.yml, doctor/metrics 행, go 의 plan implemented 기록. sdlc-evals.yml 을 모든 PR 에서 보고하도록 수정(필수 체크 영구 대기 결함). 테스트 훅 206 · 스크립트 163. 양 프로파일 0.4.0.

## 0.4.1 (2026-09-09)
gh 로그인 확인을 저장소 원격 호스트로 한정(git_remote_host/gh_ready, GH_HOST 내보내기) — 파일럿 세션에서 사내 GHE 토큰 만료로 run-loop 가 오탐 차단된 사고 반영. run-loop 의 gh 호출은 저장소 안에서 실행. 테스트 206/163, 양 프로파일 0.4.1.
