<!-- sdlc:work -->
# work/

변경 하나에 파일 하나: `work/<slug>.md` — 목표, 인수 조건, 계획, 증명, 리뷰. sdlc 플러그인이 읽고 쓰며,
사람이 직접 고쳐도 됩니다.

| status | 뜻 |
|---|---|
| `proposed` | 아직 누군가 받아들여야 하는 제안 — /sdlc:status 에서 "받아"라고 하면 `sdlc.py accept <slug>` 를 실행합니다 (직접 하려면 `ready` 로 바꾸고 `sdlc.py enqueue`) |
| `ready` | 큐에 있음. 루프가 다음에 설계 |
| `planned` | 설계됨 (`sdlc/<slug>` 브랜치). 루프가 다음에 구현 |
| `built` | 구현·리뷰됨. 다음은 머지 게이트 |
| `done` | 머지됨 |
| `needs-input` | 답을 기다림 — `## Questions` 참고 |
| `dropped` | 하지 않기로 함 — `## Departures` 참고 |

- 말로 시키면 됩니다: "X 해줘" (지금 변경 하나) 또는 "X 만들어, 알아서 채우고 돌려" (작업으로 나눠 루프 실행).
- "어디까지 됐어?" 로 진행 상황과 사람이 할 일을 봅니다.
- `depends_on: [slug]` 는 그 항목이 머지될 때까지 기다리게 합니다.
- `test_changes: allowed` 면 기존 테스트 수정을 허용합니다 (새 테스트는 항상 허용).
- `difficulty: easy|normal|hard` 는 루프가 세션마다 얼마나 깊이 생각할지 알려 줍니다 (설계가 정함).
