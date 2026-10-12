---
slug: allowed
title: Item that changes an existing test
status: planned
depends_on: []
test_changes: allowed   # the design decided: the old behavior is replaced
created: 2026-10-01T09:00:00Z
source: user
---

## Goal
Fixture: the design allows existing tests to change.

## Acceptance criteria
R1. WHEN a is added to b THE SYSTEM SHALL return the sum rounded to one decimal.

## Plan
1. Change add() in src/app.py; update tests/x_test.py, which pins the old result.

## Proof
| R | test or command that proves it | kind |
|---|---|---|
| R1 | tests/x_test.py::test_add | unit |
