<!-- sdlc:begin commands -->
## Commands (명령어)
- Build(빌드): {{build_cmd}}
- Test(테스트): {{test_cmd}}
- Lint(린트): {{lint_cmd}}
- Run(실행): {{run_cmd}}
<!-- sdlc:end commands -->

<!-- sdlc:begin conventions -->
## Conventions (규약)
- {{conventions}}
<!-- sdlc:end conventions -->

<!-- sdlc:begin architecture -->
## Architecture (아키텍처)
- {{architecture}}
<!-- sdlc:end architecture -->

<!-- sdlc:begin mistakes -->
## Things Claude gets wrong (Claude 가 반복하는 실수)
- (같은 실수가 두 번째로 나타나면 여기에 한 줄을 추가한다. `/sdlc:lesson` 이 이 일을 한다)
<!-- sdlc:end mistakes -->

<!-- sdlc:begin verifying -->
## Verifying your work (작업 검증)
- Build(빌드): {{build_cmd}} (오류 없이 끝나야 한다)
- Test(테스트): {{test_cmd}} (전부 통과. 실패하는 테스트를 건너뛰거나 삭제하지 않는다)
- Lint(린트): {{lint_cmd}} (경고 0건)

작업 완료를 보고하기 전에 위 세 가지를 모두 실행하고 출력을 그대로 붙인다.
테스트가 실패하면 테스트가 아니라 코드를 고친다.
승인된 계획 없이는 아무것도 구현하지 않는다. 플랜 모드로 시작해 plan.md 를 커밋한 뒤 구현한다.
<!-- sdlc:end verifying -->
