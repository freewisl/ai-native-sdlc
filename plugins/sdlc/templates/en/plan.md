---
type: plan
slug: {{slug}}
title: {{title}}
status: draft
author: {{author}}
created: {{date}}
record_id: ""
links: { intent: "{{intent_path}}", spec: "{{spec_path}}" }
test_changes: forbidden
approved_by: ""
---
<!-- The article's example title reads "Plan: … (from intent.md <date>)"; this template cites the spec path instead because the plan is derived from the spec when one exists — the intent is linked in the frontmatter. -->
# Plan: {{title}} (from {{spec_path}})

## Files that change
<!-- Every file, marked (new) or (modified). Group by component. -->

## Order of work
1.
2.
3.

## Risks
<!-- What the change could break, which step is most risky, and what was considered and not chosen. -->

## Proof
<!-- The tests that prove it and any visual check: "test_status.py covers the four claim states; screenshot matches the approved mock." -->

## Departures from plan
<!-- Filled in during implementation. When implementation departs from the plan, update this file in the same commit. -->
