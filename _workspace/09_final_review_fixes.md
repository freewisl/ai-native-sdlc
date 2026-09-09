# 09 — 최종 검토(08) 반영 결과 · 0.3.1

| 항목 | 조치 | 검증 |
|---|---|---|
| F-01 시크릿 이름 불일치 | `init` 이 `ci.auth` 를 config 에 기록(신규·기존 모두), `github-setup` 이 그 값을 기본으로 읽음(플래그 > env > config > api) | scripts: "new config records ci.auth", "github-setup takes auth from config" |
| F-02 협업자 수 fail-open | 빈 값 → "unknown, 팀 게이트 유지" (init·github-setup 양쪽) | "unknown collaborator count keeps team gates" |
| F-03 자동 solo 가 명시 false 덮어씀 | `SOLO_EXPLICIT`: 감지는 키가 없을 때만, `--solo` 만 덮어씀 | "auto-detected solo does not override", "explicit --solo overrides" |
| F-04 default_branch 미기록 | 감지값을 `protect.default_branch` 로 기록, 기존 config 은 빈 경우만 채움 | "records default_branch", 훅 "configured default_branch master is protected" |
| F-05 푸시 가드 우회 | 절 분리(&& ; \| \|\|) + `git [-C d] push` 토큰 파서 + 리프스펙(`+`, `refs/heads/`, `src:dst`) + `--all/--mirror` + `--delete <기본>` | 훅 13건(우회 7·`--all`·오탐 3·master 2) |
| F-06 auto-merge 불가 | 룰셋 뒤 `PATCH repos/{r} allow_auto_merge=true delete_branch_on_merge=true`, dry-run 표기 | "announces auto-merge setting" |
| F-07 solo bypass 가 필수 체크 무력화 | 기본 bypass 없음(승인 0 만), `--bypass` 옵트인 + 권고화 경고 | "no admin bypass without --bypass", "--bypass adds" |
| F-08 기본 브랜치에 아티팩트 커밋 | go 0단계에서 `git checkout -b sdlc/<slug>` | 스킬 문안 |
| F-09 README 훅 표 | 4행 ③ 직접 푸시 항목 + 키 2개 + 해제 방법 | 문서 |
| F-10~F-14 | approved_by/approval_basis + plan.approve 이벤트, doctor auto_mode 조건, allowed-tools 확대, 정지조건 표 6행 통일, intent/spec 상호참조 | 스킬 문안 |
| F-15 | doctor `SDLC_SKIP_GH_DETECT` 준수 | 코드 |
| F-16~F-19 | usage 동적 범위, 키체인 노출 주석+`--save-token-file`, 미인증 dry-run 안내, owner_type 호출 제거 | 코드 |
| F-20 | 잘못된 JSON → `SKIPPED (invalid)` | "invalid config JSON is reported" |
| F-21~F-23 | init argument-hint·6단계, README 건수 206/120, 손길 표 문구 | 문서 |
| F-24 | `go.run detail.duration_min` → metrics "Request → PR via /sdlc:go" 행 | "metrics shows request→PR median" |
| F-25 | PLAYBOOK-MAPPING R-3.1.11·R-3.2.2 주석 | 문서 |
| F-26 | run-evals 가 `<work>.meta/copied.txt` 기록, check.sh 는 그 파일만 제외 | check.sh 3건 |
| F-27~F-30 | test_unlock 이벤트, 모델 자동호출 시 autopilot 금지, CHANGELOG 0.3.0 누락분+0.3.1, `github.*` 호스트 | 문서·코드·"GitHub remote → workflows installed" |

테스트: 훅 206/206 · 스크립트 120/120 · `claude plugin validate --strict` PASS. 미검증(실 GitHub 필요): 룰셋·auto_merge PATCH 의 실제 API 응답, `gh pr merge --auto` 동작 — 파일럿 저장소에서 `github-setup.sh` 재실행으로 확인 필요.

## 후속 (0.3.2) — 파일럿 저장소 `freewisl/Gilnnon` 실상태 점검에서 나온 5건

| 항목 | 조치 | 검증 |
|---|---|---|
| F-31 룰셋 403(Free 요금제 비공개) 시 auto-merge 설정 미실행 | 저장소 설정 PATCH 를 룰셋 앞으로 이동 | gh 스텁: "auto-merge setting applied despite ruleset 403", 순서 검사 |
| F-32 보호 없는 브랜치에서 `gh pr merge --auto` 거부 | go 7단계: `gh pr checks --watch` 후 직접 squash 머지, 실패/진행 중 체크 있으면 머지 금지 | 스킬 문안 |
| F-33 기존 `sdlc-*.yml` 의 인증 줄이 `ci.auth` 와 다를 때 방치 | init 이 그 줄만 교체(MERGED), 다른 워크플로·주석은 무시(정확 일치) | "auth line switched", "foreign workflow untouched", 왕복 |
| F-34 보관된 구독 토큰을 `ci.auth=api` 저장소에 API 키로 등록 | oauth 일 때만 키체인/파일 토큰 사용, api 면 NOTE | "not registered as an API key" |
| F-35 init 채택 커밋이 기본 브랜치에 남고 푸시가 가드에 막힘 | init 스킬 8단계: 브랜치 → 커밋 → 푸시 → PR → (solo) 초록 후 머지; github-setup 은 워크플로가 있으면 항상 실행 | 스킬 문안 |

doctor 행 추가: `CI auth`(config↔워크플로, 오프라인) · `CI auth secret`(기대 시크릿 이름 명시) · `auto-merge`. 테스트 훅 206 · 스크립트 134. 양 프로파일 0.3.2.
