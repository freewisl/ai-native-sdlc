# intent/ — 아이디어가 루프에 들어오는 곳

모든 변경은 여기의 파일 하나에서 시작합니다: `intent/<slug>.md`. 발안자 본인의 말로,
Claude 의 도움을 받아 작성합니다(`/sdlc:intent`). 작성자와 시각은 git 커밋에서 나오고, 승인은
프로덕트 오너가 `status: approved` 를 커밋하는 것(또는 PR 머지)입니다.

- `TEMPLATE.md` — 조직의 intent 템플릿. 자유롭게 수정하십시오. 스킬이 이 파일을 읽습니다.
- `triage/` — 자동화(모니터링·스캔·온콜)가 쓴 intent. 서비스 오너가 `/sdlc:triage` 로
  분류합니다: 지금 고친다(fix now), 일정에 넣는다(schedule), 사유를 적고 기각한다(dismiss).

엔지니어가 아닌 분은 git 을 직접 쓰지 않아도 됩니다. claude.ai 또는 Cowork 를 저장소에
연결(GitHub 커넥터)하고 Claude 에게 파일 커밋을 요청하면 됩니다.

다음 단계: 승인된 intent 는 `/sdlc:spec` 을 트리거하고, 그 결과가 `spec/<slug>.md` 로 남습니다.


## intent 의 집

단일 제품이라면 제품 저장소의 이 `intent/` 폴더가 가장 단순합니다 — 아티팩트 체인이 그로부터 파생된 코드 옆에 남습니다. 전용 intent 저장소는 intent 가 여러 저장소에 걸칠 때만 가치가 있고, 모노레포에서는 디렉터리 하나입니다. 집을 세우는 일은 플랫폼/엔지니어링 팀의 일회성 작업이며, 기여자가 조직 전체에서 오므로 누가 쓸 수 있는지(저장소 권한, CODEOWNERS 의 `intent/` 행)도 그때 정합니다. `TEMPLATE.md` 는 기술 팀원이 만들고 리드가 서명하며, 변경은 PR 리뷰를 거칩니다.
