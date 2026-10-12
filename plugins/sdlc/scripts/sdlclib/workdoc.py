"""Work documents — `work/<slug>.md`, one file per change: frontmatter + sections.

    ---
    slug: entry-crud
    title: 수입·지출 입력/수정/삭제
    status: ready
    depends_on: []
    test_changes: none
    created: 2026-10-12T09:30:00Z
    source: user
    ---
    ## Goal
    ## Acceptance criteria
    ## Plan
    ## Proof
    ## Review
    ## Departures
    ## Learned
    ## Questions
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from .util import read_text, write_text

STATUSES = ("proposed", "ready", "planned", "built", "done", "dropped", "needs-input")
FIELD_ORDER = ("slug", "title", "status", "depends_on", "test_changes", "created", "source")
SECTION_ORDER = ("Goal", "Acceptance criteria", "Plan", "Proof", "Review", "Departures", "Learned", "Questions")
SKIP_FILES = {"README.md", "TEMPLATE.md"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
TALLY_RE = re.compile(r"SDLC_REVIEW_TALLY\s+important=(\d+)\s+nits=(\d+)(?:\s+validated=(\w+))?")


def _parse_value(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip().strip("'\"")] if inner else []
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _dump_value(v) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]"
    s = str(v)
    if s and (s[0] in "[{&*!|>%@`'\"" or ": " in s or s.endswith(":") or " #" in s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def split_frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    meta: Dict[str, object] = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s?(.*)$", line)
        if m:
            meta[m.group(1)] = _parse_value(m.group(2))
    return {}, text


class WorkDoc:
    def __init__(self, meta: Dict[str, object], body: str, path: str = ""):
        self.meta = meta
        self.path = path
        self.preamble, self.sections = self._split(body)

    # ---------- parsing ----------
    @staticmethod
    def _split(body: str) -> Tuple[str, List[List[str]]]:
        pre: List[str] = []
        secs: List[List[str]] = []
        for line in body.split("\n"):
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m:
                secs.append([m.group(1), ""])
            elif secs:
                secs[-1][1] += line + "\n"
            else:
                pre.append(line)
        return "\n".join(pre).strip("\n"), secs

    @classmethod
    def parse(cls, text: str, path: str = "") -> "WorkDoc":
        meta, body = split_frontmatter(text or "")
        return cls(meta, body, path)

    @classmethod
    def load(cls, path: str) -> Optional["WorkDoc"]:
        text = read_text(path)
        return None if text is None else cls.parse(text, path)

    # ---------- fields ----------
    @property
    def slug(self) -> str:
        s = str(self.meta.get("slug") or "")
        return s or (os.path.splitext(os.path.basename(self.path))[0] if self.path else "")

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.slug)

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "ready")

    @status.setter
    def status(self, v: str) -> None:
        self.meta["status"] = v

    @property
    def depends_on(self) -> List[str]:
        d = self.meta.get("depends_on") or []
        return [str(x) for x in d] if isinstance(d, list) else [x.strip() for x in str(d).split(",") if x.strip()]

    @property
    def created(self) -> str:
        return str(self.meta.get("created") or "")

    # ---------- sections ----------
    def section(self, name: str) -> str:
        for title, body in self.sections:
            if title.lower() == name.lower():
                return body.strip("\n")
        return ""

    def set_section(self, name: str, body: str) -> None:
        body = body.strip("\n") + "\n"
        for sec in self.sections:
            if sec[0].lower() == name.lower():
                sec[1] = "\n" + body + "\n"
                return
        # insert in canonical order
        order = {t.lower(): i for i, t in enumerate(SECTION_ORDER)}
        pos = len(self.sections)
        mine = order.get(name.lower(), len(SECTION_ORDER))
        for i, (t, _) in enumerate(self.sections):
            if order.get(t.lower(), len(SECTION_ORDER)) > mine:
                pos = i
                break
        self.sections.insert(pos, [name, "\n" + body + "\n"])

    def review_tally(self) -> Optional[Tuple[int, int, str]]:
        m = None
        for m in TALLY_RE.finditer(self.section("Review")):
            pass
        if not m:
            return None
        return int(m.group(1)), int(m.group(2)), (m.group(3) or "")

    # ---------- output ----------
    def dumps(self) -> str:
        keys = [k for k in FIELD_ORDER if k in self.meta] + [k for k in self.meta if k not in FIELD_ORDER]
        out = ["---"] + ["%s: %s" % (k, _dump_value(self.meta[k])) for k in keys] + ["---"]
        if self.preamble:
            out.append(self.preamble)
        text = "\n".join(out) + "\n"
        for title, body in self.sections:
            text += "\n## %s\n" % title + body.strip("\n") + "\n"
        return text

    def save(self, path: str = "") -> None:
        write_text(path or self.path, self.dumps())


def problems(doc: WorkDoc, file_slug: str = "") -> List[str]:
    """What is wrong with a document the loop is about to queue."""
    p = []
    if not SLUG_RE.match(doc.slug):
        p.append("slug '%s' must be kebab-case ascii (a-z 0-9 - . _)" % doc.slug)
    if file_slug and doc.slug != file_slug:
        p.append("slug '%s' does not match the file name %s.md" % (doc.slug, file_slug))
    if doc.status not in STATUSES:
        p.append("status '%s' is not one of %s" % (doc.status, ", ".join(STATUSES)))
    if not doc.section("Goal"):
        p.append("no Goal section")
    return p


def list_docs(work_dir: str) -> List[WorkDoc]:
    out = []
    try:
        names = sorted(os.listdir(work_dir))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".md") or n in SKIP_FILES or n.startswith("."):
            continue
        d = WorkDoc.load(os.path.join(work_dir, n))
        if d is not None:
            out.append(d)
    return out
