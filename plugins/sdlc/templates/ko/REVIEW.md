# 리뷰 지침 (Review instructions)

## 패스 (Passes)
세 번의 패스를 실행하고 모든 지적에 해당 패스를 태그합니다.
- Bugs(버그): 논리 오류, 깨진 엣지 케이스, 미묘한 회귀
- Security(보안): 인젝션 위험, 인증 누락, 로그 속 PII, diff 에 포함된 비밀값
- Compliance(정합성): 변경이 spec.md·plan.md·우리 설계 원칙과 일치하는지, CLAUDE.md 규약을 따르는지

## 여기서 Important 가 뜻하는 것 (What Important means here)
Important 는 동작을 깨뜨리거나, 데이터를 유출하거나, 정책을 위반할 지적에만 사용합니다.
스타일과 이름 짓기는 nit 입니다.

## nit 상한 (Cap the nits)
리뷰 하나에 nit 은 최대 다섯 건까지만 보고하고, 나머지는 건수로 요약합니다.

## 보고하지 않는 것 (Do not report)
{{generated_paths}} 아래의 생성 파일과 CI 가 이미 강제하는 것(포매팅, 린트).

## CLAUDE.md 로의 피드백 (Feedback into CLAUDE.md)
리뷰가 같은 실수를 두 번째로 지적하면, 그 리뷰의 일부로 CLAUDE.md 의
"Things Claude gets wrong" 섹션에 교정 내용을 추가합니다.
변경으로 CLAUDE.md 가 낡아졌다면 그것도 지적합니다.

## 승인 (Approval)
지적 사항만으로 PR 이 승인되거나 차단되지 않습니다. 승인은 브랜치 보호를 통해
코드 오너가 합니다. 코드를 작성한 에이전트는 그 코드를 승인할 수 없습니다.
