---
description: A plain code question that shares words with the skills ("루프", "해줘") must call no sdlc skill.
tags: [trigger, negative]
max_turns: 4
timeout_seconds: 120
allowed_tools: [Read, Glob, Grep, Skill]
---

이 for 루프가 왜 느린지 설명해줘. 코드는 건드리지 말고 이유만 알려줘.

```python
result = []
for x in items:
    result = result + [x * 2]
```
