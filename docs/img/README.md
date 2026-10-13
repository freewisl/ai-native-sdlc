# README 그림

README 의 그림 다섯 장(PNG)과 원본입니다. 원본은 `src/*.html` 이고, ELI5 페이지와 같은 그림 문법(공통 CSS 는 `src/_style.css`, 인라인 SVG 아이콘, Jua · Gowun Dodum 웹폰트)으로 그렸습니다.

| 그림 | 원본 | 내용 |
|---|---|---|
| `01-usage.png` | `src/01-usage.html` | 말로 시키면 됩니다 — `init` · `go` · `run` · `status` 네 스킬과 사람이 답하는 곳 |
| `02-go-flow.png` | `src/02-go-flow.html` | `/sdlc:go` 한 건이 머지되기까지 — 작업 문서 상태, 셸 게이트의 7가지 검사 |
| `03-run-loop.png` | `src/03-run-loop.html` | `/sdlc:run` 루프 — 큐, 항목마다 새 세션, 자기 점검, 멈추는 때 |
| `04-hooks.png` | `src/04-hooks.html` | 훅 7종 — `guard-edit` · `guard-bash` 가 막는 것과 fail closed |
| `05-architecture.png` | `src/05-architecture.html` | 구성 — 플러그인(스킬 · 에이전트 · 훅) → 엔진 `sdlc.py` → 저장소 → GitHub, 단계별 모델 |

## 다시 만드는 법

```bash
bash docs/img/render.sh                 # 다섯 장 모두
bash docs/img/render.sh 02-go-flow      # 한 장만
```

헤드리스 Chrome 으로 폭 1200px, 2배 해상도(2400px)로 찍습니다. macOS 의 Google Chrome 이 기본이고, 다른 Chromium 은 `CHROME=/path/to/chrome bash docs/img/render.sh` 로 지정합니다. 웹폰트를 Google Fonts 에서 받으므로 네트워크가 필요합니다.

- 그림마다 높이가 다릅니다. 높이는 `render.sh` 의 창 높이이자 원본의 `body{height:...px}` 이고, 둘이 다르면 `render.sh` 가 경고합니다. 글을 고쳐서 내용이 넘치거나 남으면 두 곳을 함께 조정하세요. 본문은 `overflow:hidden` 이라 넘친 부분은 잘립니다.
- 새 그림을 더하려면 `src/` 에 HTML 을 만들고 `render.sh` 의 `case` 와 기본 목록에 높이와 함께 한 줄 추가합니다.

## 같이 보는 페이지

`docs/eli5.html` 은 플레이북 6단계를 그린 페이지로 그대로 둡니다. `docs/archify-architecture.html` 과 `docs/eli5-plugin.html` 은 0.x 를 설명하던 페이지였고, 지금은 1.0 이 README 에 있다고 알려 주는 짧은 안내 페이지입니다. 예전 링크가 끊어지지 않도록 주소만 유지합니다.
