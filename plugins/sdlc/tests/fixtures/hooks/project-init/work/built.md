---
slug: built
title: Built item
status: built
depends_on: []
test_changes: none
created: 2026-10-01T09:00:00Z
source: user
---

## Goal
Fixture: the build is finished, review and gate are next.

## Acceptance criteria
R1. WHEN a is added to b THE SYSTEM SHALL return the sum.

## Plan
1. Keep add() in src/app.py.

## Proof
| R | test or command that proves it | kind |
|---|---|---|
| R1 | tests/x_test.py::test_add | unit |
