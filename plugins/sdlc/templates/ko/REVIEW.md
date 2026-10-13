# 리뷰 지침 (Review instructions)

## 패스 (Passes)
세 번의 패스를 실행하고 모든 지적에 해당 패스를 태그합니다.
- Bugs(버그): 논리 오류, 깨진 엣지 케이스, 미묘한 회귀
- Security(보안): 인젝션 위험, 인증 누락, 로그 속 PII, diff 에 포함된 비밀값
- Compliance(정합성): 변경이 작업 문서(인수 조건·계획)와 우리 설계 원칙을 충족하는지, CLAUDE.md 규약을 따르는지

## 여기서 Important 가 뜻하는 것 (What Important means here)
Important 는 동작을 깨뜨리거나, 데이터를 유출하거나, 정책을 위반할 지적에만 사용합니다. 스타일과 이름 짓기는 nit 입니다.
Important 지적은 구체적인 실패를 적습니다: 이 입력이나 상태 → 이 잘못된 결과. 각 지적은 새 컨텍스트에서 한 번 더
반박을 시도하고, 살아남은 것만 셉니다.

## 새로 생긴 것과 원래 있던 것 (New or pre-existing)
이 변경이 만들었거나 악화시킨 것만 지적으로 보고합니다. 원래 있던 문제는 pre-existing 으로 따로 적습니다 —
이것들은 proposed 작업 항목이 되고, 이 변경을 막지 않습니다.

## nit 상한 (Cap the nits)
리뷰 하나에 nit 은 최대 다섯 건까지만 보고하고, 나머지는 건수로 요약합니다.

## 보고하지 않는 것 (Do not report)
{{generated_paths}} 아래의 생성 파일과 게이트가 이미 강제하는 것(빌드, 테스트, 린트, 포매팅).

## 집계 (Tally)
마지막 줄은 `SDLC_REVIEW_TALLY important=<n> nits=<n> validated=yes` 입니다. 작업 문서의 `## Review` 에 들어가고,
머지 게이트가 읽습니다. 열린 Important 지적이 있는 변경은 머지되지 않습니다.

## CLAUDE.md 로의 피드백 (Feedback into CLAUDE.md)
리뷰가 같은 실수를 두 번째로 지적하면, 그 변경의 일부로 CLAUDE.md 의 "Things Claude gets wrong" 섹션에 교정 내용을
추가합니다. 변경으로 CLAUDE.md 가 낡아졌다면 그것도 지적합니다.
