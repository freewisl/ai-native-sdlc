# README 그림

README 의 그림 여섯 장(PNG)과 원본입니다. 손으로 그린 세 장과 [Archify](https://github.com/tt-a1i/archify) 로 그린 세 장이 있습니다.

| 그림 | 원본 | 내용 |
|---|---|---|
| `01-usage.png` | `src/01-usage.html` | 말로 시키면 됩니다 — `init` · `go` · `run` · `status` 네 스킬과 사람이 답하는 곳 |
| `02-go-flow.png` | `src/02-go-flow.html` | `/sdlc:go` 한 건이 머지되기까지 — 작업 문서 상태, 셸 게이트의 7가지 검사 |
| `04-hooks.png` | `src/04-hooks.html` | 훅 7종 — `guard-edit` · `guard-bash` 가 막는 것과 fail closed |
| `archify-lifecycle.png` | `archify/sdlc-lifecycle.json` → `docs/archify-lifecycle.html` | 작업 문서 한 건의 일생 — `status` 흐름, 되돌림·대기, 끝 |
| `archify-loop.png` | `archify/sdlc-loop.json` → `docs/archify-loop.html` | `/sdlc:run` 루프 — 큐에서 머지까지, 수정 세션·사람 몫·멈춤 |
| `archify-architecture.png` | `archify/sdlc-architecture.json` → `docs/archify-architecture.html` | 구성 — 사람 → 스킬 → 엔진 `sdlc.py` → 루프 세션 → 저장소, GitHub |

## 손그림 다시 만들기

```bash
bash docs/img/render.sh                 # 세 장 모두
bash docs/img/render.sh 02-go-flow      # 한 장만
```

원본은 `src/*.html` 이고, ELI5 페이지와 같은 그림 문법(공통 CSS 는 `src/_style.css`, 인라인 SVG 아이콘, Jua · Gowun Dodum 웹폰트)으로 그렸습니다. 헤드리스 Chrome 으로 폭 1200px, 2배 해상도(2400px)로 찍습니다. macOS 의 Google Chrome 이 기본이고, 다른 Chromium 은 `CHROME=/path/to/chrome bash docs/img/render.sh` 로 지정합니다. 웹폰트를 Google Fonts 에서 받으므로 네트워크가 필요합니다.

- 그림마다 높이가 다릅니다. 높이는 `render.sh` 의 창 높이이자 원본의 `body{height:...px}` 이고, 둘이 다르면 `render.sh` 가 경고합니다. 글을 고쳐서 내용이 넘치거나 남으면 두 곳을 함께 조정하세요. 본문은 `overflow:hidden` 이라 넘친 부분은 잘립니다.
- 새 그림을 더하려면 `src/` 에 HTML 을 만들고 `render.sh` 의 `case` 와 기본 목록에 높이와 함께 한 줄 추가합니다.

## Archify 그림 다시 만들기

원본은 `archify/*.json`(Archify 스펙)이고, 산출 HTML 은 `docs/archify-*.html` 로 GitHub Pages 가 서비스합니다(확대·검색·경로 추적이 되는 페이지). Archify 스킬이 설치돼 있어야 합니다(설치는 Archify 저장소의 안내대로, 아래 명령은 `~/.agents/skills/archify` 기준). 스펙을 고친 뒤 그림마다 아래 세 단계를 돌립니다(`lifecycle` 의 예 — `loop` 은 type `workflow`, `architecture` 는 `architecture`).

```bash
A=~/.agents/skills/archify/bin/archify.mjs
node "$A" validate lifecycle docs/img/archify/sdlc-lifecycle.json --quality showcase --json     # 9/9, 오류·경고 0
node "$A" deliver  lifecycle docs/img/archify/sdlc-lifecycle.json docs/archify-lifecycle.html --quality showcase --json
node docs/img/archify/readme-shot.mjs docs/archify-lifecycle.html docs/img/archify-lifecycle.png
```

- `readme-shot.mjs` 는 Archify 의 헤드리스 Chrome 헬퍼로 페이지를 열고, 뷰어의 버튼·도구 막대·범례를 숨긴 채 그림이 그려진 부분만 2배 해상도로 잘라 찍습니다. 산출 HTML 은 바꾸지 않습니다.
- `deliver` 뒤에 `node "$A" visual-check docs/archify-lifecycle.html --json` 으로 실제 브라우저에서 넘침·글자 크기를 확인할 수 있습니다. 영수증과 스크린샷은 같은 폴더에 생기며 커밋하지 않습니다(로컬 경로가 들어 있음).
- 스펙은 한국어로 써서 `meta.locale` 을 비웠습니다. 뷰어 UI(Light/Present/Export 등)와 `<html lang>` 은 영어로 나옵니다.

## 같이 보는 페이지

`docs/eli5.html` 은 플레이북 6단계를 그린 페이지로 그대로 둡니다. `docs/eli5-plugin.html` 은 0.x 를 설명하던 페이지였고, 지금은 1.x 가 README 에 있다고 알려 주는 짧은 안내 페이지입니다. 예전 링크가 끊어지지 않도록 주소만 유지합니다. `docs/archify-architecture.html` 은 같은 주소에 1.2 구성도를 다시 그렸습니다.
