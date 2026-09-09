# README 그림

| 파일 | 원본 | 다시 만드는 법 |
|---|---|---|
| `01-two-commands.png` … `05-architecture.png` | `src/*.html` (ELI5 와 같은 그림 문법, CSS 는 `src/_style.css`) | `bash docs/img/render.sh` — 헤드리스 Chrome 으로 1200px 폭, 2배 해상도 |
| `06-archify-architecture.png` | `archify/sdlc-architecture.json` ([Archify](https://github.com/tt-a1i/archify) architecture 스펙) | 아래 세 명령. 산출 HTML 은 `docs/archify-architecture.html` 로 GitHub Pages 가 서빙 |

```bash
A=~/.agents/skills/archify/bin/archify.mjs   # Archify 스킬 위치
node "$A" validate architecture docs/img/archify/sdlc-architecture.json --quality showcase --json
node "$A" deliver  architecture docs/img/archify/sdlc-architecture.json docs/archify-architecture.html --quality showcase --json
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --window-size=1440,900 --force-device-scale-factor=2 --virtual-time-budget=6000 \
  --screenshot="$PWD/docs/img/06-archify-architecture.png" "file://$PWD/docs/archify-architecture.html"
```

Archify 스펙은 한국어로 썼으므로 `meta.locale` 을 비웠고, 뷰어 UI(Light/Present/Export 등)와 `<html lang>` 은 영어로 떨어집니다.
마지막 검증 영수증은 `archify/sdlc-architecture.visual-check.json` 입니다.
