# evals/ — 에이전트 설정의 회귀 테스트

CLAUDE.md·스킬·훅이 에이전트를 조종하므로, 코드가 받는 회귀 테스트를 이 설정도 똑같이 받습니다.
이 설정이 바뀔 때마다 스위트를 실행하고(CI 가 풀 리퀘스트와 야간 스케줄에서 실행합니다) 결과로 변경을 게이트합니다.

각 케이스는 `cases/` 아래의 디렉터리 하나입니다.
- `prompt.md`   — 실제 엔지니어가 요청하는 말투로 쓴 과제
- `fixture/`    — 과제가 다루는 파일(실행마다 임시 디렉터리로 복사됩니다)
- `check.sh`    — 결정론적 합격 판정. exit 0 = 통과. 작업 디렉터리를 `$1` 로 받습니다
- `case.json`   — 선택: `{"allowed_tools": "...", "max_turns": 20, "tags": ["security"]}`

실행: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/run-evals.sh"` 또는 `/sdlc:evals run`.
추가: `/sdlc:evals add` — 최근 작업·리뷰 지적·운영 인시던트를 케이스로 바꿉니다.
규칙: 모든 운영 인시던트는 eval 이 되어 스위트에 영구히 남습니다.


형식 메모: 원문 `agent-evals.yml` 은 `evals/*.json` 을 순회하며 스위트 수준의 `./evals/check.sh <eval.json> result.json` 을 호출합니다. 이 플러그인은 케이스마다 디렉터리(`prompt.md` + `fixture/` + `check.sh <workdir>`)를 쓰는데, fixture 를 언어 무관하게 두고 플러그인 훅이 실제로 발동하는 임시 git 저장소 안에서 실행하기 위해서입니다. `run-evals.sh` 가 판정을 `results/<ts>/results.json` 으로 모읍니다. 원문 워크플로는 `.sdlc/examples/agent-evals.yml` 에 그대로 있습니다. 매 변경 대신 정해진 주기로만 돌리려면 `sdlc-evals.yml` 에서 `schedule` 트리거만 남기십시오.
