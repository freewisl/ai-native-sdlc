"""Effort per session: the loop starts from `loop.efforts` and moves it by what it knows — how hard the item is (the work
doc's `difficulty:`), an earlier failure, a second fix round. The amount of work is not a factor: a rename across forty files
is large and needs little thought, a five-line concurrency fix is small and needs a lot. Pure functions; `Engine.effort_for`
decides which of them apply to a session.
"""
from __future__ import annotations

from typing import Optional

from .workdoc import WorkDoc

LEVELS = ("low", "medium", "high", "xhigh", "max")
TOP = LEVELS.index("xhigh")      # the loop never moves a session up to `max`: that is a person's choice
DIFFICULTIES = ("easy", "normal", "hard")


def shift(level: str, steps: int) -> str:
    """`level` moved `steps` along LEVELS, kept within low..xhigh. "" (pass no --effort), "max" and a value that is not a
    level are somebody's choice and come back as they were. claude quietly lowers xhigh on a model without it, so passing
    xhigh is safe."""
    if level not in LEVELS or level == "max":
        return level
    return LEVELS[max(0, min(TOP, LEVELS.index(level) + steps))]


def doc_difficulty(doc: Optional[WorkDoc]) -> str:
    """`easy`, `normal` or `hard`: the `difficulty:` of a work item, read stripped and lower-cased. Anything else, no field
    and no doc are `normal`, which moves nothing."""
    if doc is None:
        return "normal"
    given = str(doc.meta.get("difficulty") or "").strip().lower()
    return given if given in DIFFICULTIES else "normal"
