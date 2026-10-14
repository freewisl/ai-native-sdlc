"""Effort per session: the loop starts from `loop.efforts` and moves it by what it knows — the size of the item, an earlier
failure, a second fix round, the size of the range a self-check reads. Pure functions; `Engine.effort_for` decides which of
them apply to a session.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from .workdoc import WorkDoc

LEVELS = ("low", "medium", "high", "xhigh", "max")
TOP = LEVELS.index("xhigh")      # the loop never moves a session up to `max`: that is a person's choice
SIZES = {"s": "s", "small": "s", "xs": "s", "m": "m", "medium": "m", "l": "l", "large": "l", "xl": "l"}
CRITERION = re.compile(r"\s*(?:[-*]\s+)?(?:\*\*)?R\d+")      # R1. …   - R1. …   * **R1.** …
STEP = re.compile(r"\s*\d+[.)]\s")                           # 1. …   2) …
COMMENT = re.compile(r"<!--.*?-->", re.S)                    # the template's hints are neither criteria nor steps


def shift(level: str, steps: int) -> str:
    """`level` moved `steps` along LEVELS, kept within low..xhigh. "" (pass no --effort), "max" and a value that is not a
    level are somebody's choice and come back as they were. claude quietly lowers xhigh on a model without it, so passing
    xhigh is safe."""
    if level not in LEVELS or level == "max":
        return level
    return LEVELS[max(0, min(TOP, LEVELS.index(level) + steps))]


def _count(rx: "re.Pattern[str]", text: str) -> int:
    return sum(1 for line in COMMENT.sub("", text or "").splitlines() if rx.match(line))


def doc_size(doc: Optional[WorkDoc], estimate: bool = True) -> Tuple[str, str]:
    """(size, how) of a work item: `s`, `m` or `l`, and where that came from, for the log — "size s" when the doc says so
    (`size:` in its front matter; small/medium/large and xs/xl are read too, anything else counts as missing), "estimated s
    (2 criteria, 3 steps)" when it is counted from the acceptance criteria and the plan, and "" when there is nothing to
    go on (no doc, no estimate wanted, no numbered Plan step yet): `m`, which moves nothing."""
    if doc is None:
        return "m", ""
    given = SIZES.get(str(doc.meta.get("size") or "").strip().lower())
    if given:
        return given, "size %s" % given
    if not estimate:
        return "m", ""
    steps = _count(STEP, doc.section("Plan"))
    if not steps:
        return "m", ""
    criteria = _count(CRITERION, doc.section("Acceptance criteria"))
    size = "s" if criteria <= 2 and steps <= 3 else "l" if criteria >= 7 or steps >= 8 else "m"
    return size, "estimated %s (%d %s, %d %s)" % (size, criteria, "criterion" if criteria == 1 else "criteria",
                                                  steps, "step" if steps == 1 else "steps")
