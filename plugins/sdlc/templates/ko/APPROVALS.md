# 승인 게이트 등록부 (Approval gates)

변경 프로세스에 반드시 남아야 하는 사람의 승인 목록입니다(플레이북 Stage 5 "Hooks as approval gates").
각 게이트는 무엇이 승인인지, 누가 승인하는지, 무엇이 강제하는지, 결정이 어디에 기록되는지를 적습니다.
엔지니어링 리더십·변경관리·컴플라이언스가 이 목록을 소유하고, 플랫폼 엔지니어가 각 행을 훅으로 표현합니다.

| 게이트 | 승인으로 인정되는 것 | 승인자 | 강제 수단 | 기록 위치 |
|---|---|---|---|---|
| 릴리스 인가 (production) | 세션/파이프라인 환경에 `RELEASE_APPROVAL=<티켓 또는 승인 ID>` 존재 | 릴리스 매니저 | `guard-bash.sh` 배포 게이트 — `environments.production`(autonomy `gated`, `approval_env`) | `.sdlc/logs/gate.log`, `events.jsonl`(`hook.gate`) |
| 변경관리 승인 (마이그레이션·인프라) | `CHANGE_TICKET=<승인된 티켓>` 존재 | 변경자문위/서비스 오너 | `guard-edit.sh` — `protect.ticketed_paths`(예 `migrations/`, `infra/`), `protect.ticket_env` | `events.jsonl`(`hook.guard`, 규칙 `ticketed_path`) |
| 보호 경로 (생성 코드·동결 패키지) | 목록 자체를 코드처럼 리뷰해 변경 | 해당 경로의 코드 오너 | `guard-edit.sh` — `.sdlc/frozen-paths.txt`, `protect.generated_paths` | `events.jsonl`(`hook.guard`) |
| 코드 승인 (모든 PR) | PR 의 코드 오너 리뷰 | 코드 오너(CODEOWNERS) | 브랜치 보호. 에이전트는 승인 불가 | PR 이력 |
| staging 배포 | 사람이 `ask` 프롬프트를 확인 | 온콜/엔지니어 | `guard-bash.sh` — `environments.staging`(autonomy `constrained`) | `gate.log`(`ask`) |

팀 훅은 `.claude/settings.json`(git)에, 타협 불가 게이트는 플랫폼/IT 관리자가 소유하는 관리형 설정에 둡니다.
차단은 항상 스스로를 설명합니다: 이유와 승인 경로가 Claude 의 출력에 나타납니다.
