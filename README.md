# ai-native-sdlc — Claude Code 플러그인 마켓플레이스

이 마켓플레이스에는 플러그인이 하나 있습니다. `sdlc` 는 말로 시키면 변경 하나가 작업 문서 한 장으로 구현·리뷰를 거쳐 머지까지 가게 하고, 여러 건은 백그라운드 루프가 큐로 처리하게 합니다. 합격은 세션의 보고가 아니라 셸 머지 게이트가 정합니다.
Anthropic 의 블로그 글 [The AI-native SDLC playbook](https://claude.com/blog/the-ai-native-sdlc-playbook)(Louis Claxton, 2026-08-21; acknowledgments Jim Blackhurst, Will Steuk, Jamal Arif)에서 루프 발상을 따왔습니다. 원문의 저작권은 Anthropic 에 있습니다.

<p align="center"><img src="docs/img/01-usage.png" alt="말로 시키면 됩니다: init 은 저장소마다 한 번, go 는 한 건을 이 세션에서, run 은 여러 건을 백그라운드로, status 는 언제든. 사람이 답하는 곳은 질문, 사람 확인이 필요한 PR, 팀 저장소의 설계 승인과 머지" width="1000"></p>

| 플러그인 | 설명 | 설명서 |
|---|---|---|
| `sdlc` | 말로 시키면 변경 하나가 작업 문서 한 장으로 구현·리뷰를 거쳐 머지까지 갑니다. 스킬은 init·go·run·status 네 개. 합격은 세션의 보고가 아니라 셸 머지 게이트가 정하고, 여러 건은 백그라운드 루프가 큐로 처리하며, 사람은 판단이 필요한 곳에서만 답합니다. 훅 7종이 보호 경로·테스트 잠금·비밀·기본 브랜치 직접 push 를 막습니다. | [plugins/sdlc/README.md](plugins/sdlc/README.md) |

## 설치

```bash
claude plugin marketplace add freewisl/ai-native-sdlc
claude plugin install sdlc@ai-native-sdlc
# 아무 저장소에서 claude 를 열고:  /sdlc:init   →   /sdlc:go "요청 한 줄"   또는   /sdlc:run "목표"
```

저장소마다 `/sdlc:init` 을 한 번 실행합니다. 0.x 를 쓰던 저장소도 같습니다([0.x 에서 올리기](plugins/sdlc/README.md#0x-에서-올리기)).

## 설치 없이 시험

```bash
claude --plugin-dir /path/to/ai-native-sdlc/plugins/sdlc
```

플러그인을 고치면서 그 세션에서 바로 확인할 때만 쓰는 방법입니다. 다른 저장소는 이 체크아웃이 아니라 위에서 설치한 릴리스본을 씁니다. 고친 것을 거기에 반영하려면 아래 릴리스 절차로 올립니다.

## 검증

```bash
claude plugin validate plugins/sdlc --strict
claude plugin validate . --strict          # 마켓플레이스(.claude-plugin/marketplace.json)
bash plugins/sdlc/tests/run.sh             # 훅 매트릭스 · 구조·비밀 스캔 검사 · 엔진 단위 테스트
```

## 문서

| 문서 | 내용 |
|---|---|
| [plugins/sdlc/README.md](plugins/sdlc/README.md) | 플러그인 사용 설명서: 설치·업데이트, 빠른 시작, 루프, 머지 게이트, 훅, 설정, 0.x 에서 올리기 |
| [그림으로 보는 플레이북](https://freewisl.github.io/ai-native-sdlc/eli5.html) | 플레이북을 쉬운 말로 풀어 쓴 페이지 (원본 `docs/eli5.html`) |
| [docs/redesign-1.0.md](docs/redesign-1.0.md) | 1.0 설계 기록: 사용법이 너무 복잡하다는 지적에서 스킬 19개를 4개로, 단계 파일을 작업 문서 한 장으로 줄인 과정, 그리고 1.1 의 결정(자동 effort·PR 체크 대기·세션 격리 등) |
| [plugins/sdlc/CHANGELOG.md](plugins/sdlc/CHANGELOG.md) | 변경 이력 |

## 저장소 구성

```text
.claude-plugin/marketplace.json   마켓플레이스 매니페스트 (name: ai-native-sdlc)
plugins/sdlc/                     플러그인 루트 — skills/ agents/ hooks/ scripts/ templates/ evals/ tests/
docs/                             redesign-1.0.md · eli5.html · img/ (README 그림과 원본)
_workspace/                       0.x 개발 때의 작업 노트 (요구사항·설계·감사 기록)
```

## 릴리스

설치본은 마켓플레이스에서 복사한 스냅숏(`~/.claude/plugins/cache/ai-native-sdlc/sdlc/<버전>/`)이라 소스를 고쳐도 자동 반영되지 않습니다. 고친 뒤에는:

1. `plugins/sdlc/.claude-plugin/plugin.json` 의 `version` 과 `.claude-plugin/marketplace.json` 의 `metadata.version` 을 함께 올리고 `plugins/sdlc/CHANGELOG.md` 에 항목을 더합니다. 버전이 같으면 update 가 "already latest"라고 합니다.
2. `bash plugins/sdlc/tests/run.sh` 와 위의 `claude plugin validate` 두 줄을 통과시킵니다.
3. push 합니다.
4. 플러그인을 쓰는 곳마다(기기, 팀원, 루프를 돌리는 서버) 업데이트합니다.

```bash
claude plugin marketplace update ai-native-sdlc
claude plugin update sdlc@ai-native-sdlc      # 열려 있는 세션은 /reload-plugins 또는 재시작
```

라이선스: MIT ([plugins/sdlc/LICENSE](plugins/sdlc/LICENSE)).
