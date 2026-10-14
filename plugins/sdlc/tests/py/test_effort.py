"""Effort per session (loop.auto_effort): the pure functions in effort.py. What the engine does with them is in test_models.py."""
import unittest

import helpers  # noqa: F401 — puts scripts/ on sys.path

from sdlclib import effort as E
from sdlclib import workdoc as W


class Shift(unittest.TestCase):
    def test_a_level_moves_along_the_scale_in_both_directions(self):
        self.assertEqual([E.shift(l, 1) for l in ("low", "medium", "high")], ["medium", "high", "xhigh"])
        self.assertEqual([E.shift(l, -1) for l in ("medium", "high", "xhigh")], ["low", "medium", "high"])
        self.assertEqual([E.shift(l, 0) for l in ("low", "medium", "high", "xhigh")], ["low", "medium", "high", "xhigh"])
        self.assertEqual((E.shift("low", 2), E.shift("xhigh", -2), E.shift("medium", 2), E.shift("high", -2)),
                         ("high", "medium", "xhigh", "low"))

    def test_it_stops_at_low_and_at_xhigh(self):
        self.assertEqual((E.shift("low", -1), E.shift("low", -9), E.shift("medium", -9)), ("low", "low", "low"))
        self.assertEqual((E.shift("xhigh", 1), E.shift("xhigh", 9), E.shift("high", 9)), ("xhigh", "xhigh", "xhigh"))

    def test_nothing_the_loop_moves_reaches_max(self):
        self.assertEqual(E.LEVELS[-1], "max")
        self.assertEqual({E.shift(l, s) for l in E.LEVELS[:-1] for s in range(-6, 7)}, set(E.LEVELS[:-1]))

    def test_an_empty_max_or_unknown_level_is_somebodys_choice_and_stays(self):
        for level in ("", "max", "turbo", "High", " high"):
            for steps in (-2, -1, 0, 1, 2):
                self.assertEqual(E.shift(level, steps), level, (level, steps))


def doc(size=None, criteria="", plan=None, extra=""):
    """A work doc with the given front matter size (None: no field), acceptance criteria text and Plan text (None: no Plan)."""
    front = "slug: x\ntitle: x\nstatus: ready\n" + ("size: %s\n" % size if size is not None else "") + extra
    body = "## Goal\ng\n\n## Acceptance criteria\n%s\n" % criteria
    if plan is not None:
        body += "\n## Plan\n%s\n" % plan
    return W.WorkDoc.parse("---\n%s---\n\n%s" % (front, body))


def criteria(n):
    return "\n".join("R%d. WHEN a THE SYSTEM SHALL b%d." % (i, i) for i in range(1, n + 1))


def steps(n):
    return "\n".join("%d. step %d — checked by a test." % (i, i) for i in range(1, n + 1))


class ExplicitSize(unittest.TestCase):
    def test_s_m_and_l_are_taken_as_the_doc_says_them(self):
        for given in "sml":
            self.assertEqual(E.doc_size(doc(size=given)), (given, "size %s" % given))

    def test_an_explicit_size_wins_over_what_the_plan_would_say_and_needs_no_plan(self):
        big = doc(size="s", criteria=criteria(9), plan=steps(12))
        self.assertEqual(E.doc_size(big), ("s", "size s"))
        self.assertEqual(E.doc_size(doc(size="l", criteria=criteria(1), plan=steps(1))), ("l", "size l"))
        self.assertEqual(E.doc_size(doc(size="l")), ("l", "size l"))

    def test_it_counts_for_a_design_too_where_nothing_is_estimated(self):
        self.assertEqual(E.doc_size(doc(size="l"), estimate=False), ("l", "size l"))

    def test_aliases_and_capitals_are_read(self):
        for given, want in (("small", "s"), ("Small", "s"), ("SMALL", "s"), ("xs", "s"), ("XS", "s"), ("medium", "m"),
                            ("Medium", "m"), ("large", "l"), ("LARGE", "l"), ("xl", "l"), ("XL", "l"), ("S", "s"), ("L", "l"),
                            ('"L"', "l"), ("'small'", "s"), ("  m  ", "m")):
            self.assertEqual(E.doc_size(doc(size=given)), (want, "size %s" % want), given)

    def test_a_value_that_is_none_of_them_counts_as_missing(self):
        for given in ("huge", "3", "xxl", "s m", "", "[s]"):
            self.assertEqual(E.doc_size(doc(size=given)), ("m", ""), given)                          # no Plan: nothing to count
            self.assertEqual(E.doc_size(doc(size=given, criteria=criteria(1), plan=steps(2))),
                             ("s", "estimated s (1 criterion, 2 steps)"), given)
            self.assertEqual(E.doc_size(doc(size=given, criteria=criteria(1), plan=steps(2)), estimate=False), ("m", ""), given)


class Estimate(unittest.TestCase):
    def size(self, n_criteria, n_steps):
        return E.doc_size(doc(criteria=criteria(n_criteria), plan=steps(n_steps)))

    def test_a_handful_of_criteria_and_steps_is_small(self):
        self.assertEqual(self.size(2, 3), ("s", "estimated s (2 criteria, 3 steps)"))
        self.assertEqual(self.size(1, 1), ("s", "estimated s (1 criterion, 1 step)"))
        self.assertEqual(self.size(1, 3)[0], "s")

    def test_in_between_is_medium(self):
        self.assertEqual(self.size(3, 3), ("m", "estimated m (3 criteria, 3 steps)"))
        self.assertEqual(self.size(2, 4)[0], "m")                    # four steps are more than a small item has
        self.assertEqual(self.size(6, 7)[0], "m")
        self.assertEqual(self.size(6, 3)[0], "m")

    def test_many_criteria_or_many_steps_is_large(self):
        self.assertEqual(self.size(8, 9), ("l", "estimated l (8 criteria, 9 steps)"))
        self.assertEqual(self.size(7, 2)[0], "l")                    # seven criteria are enough
        self.assertEqual(self.size(1, 8), ("l", "estimated l (1 criterion, 8 steps)"))  # and so are eight steps
        self.assertEqual(self.size(3, 7)[0], "m")

    def test_no_numbered_plan_step_is_nothing_to_estimate_from(self):
        self.assertEqual(E.doc_size(doc(criteria=criteria(2))), ("m", ""))                       # no Plan section
        self.assertEqual(E.doc_size(doc(criteria=criteria(9), plan="")), ("m", ""))               # an empty one
        self.assertEqual(E.doc_size(doc(criteria=criteria(9), plan="- a bullet\n- another")), ("m", ""))
        self.assertEqual(E.doc_size(doc(criteria=criteria(9), plan="<!-- Filled by the design session: numbered steps. -->")),
                         ("m", ""))

    def test_a_design_runs_without_an_estimate(self):
        small = doc(criteria=criteria(1), plan=steps(2))
        self.assertEqual(E.doc_size(small, estimate=False), ("m", ""))
        self.assertEqual(E.doc_size(small, estimate=True)[0], "s")

    def test_no_doc_is_medium(self):
        self.assertEqual(E.doc_size(None), ("m", ""))
        self.assertEqual(E.doc_size(None, estimate=False), ("m", ""))

    def test_criteria_are_the_lines_that_start_with_r_and_a_number_bullets_and_bold_allowed(self):
        text = "\n".join(["R1. plain", "- R2. bulleted", "* R3. starred", "**R4.** bold", "* **R5.** starred and bold",
                           "  R6: indented", "R7) paren", "R10. two digits",
                           "The R8 criterion", "R-rated", "RFC 2119 says", "  - see R9 above", "-R11 no space after the bullet"])
        got = E.doc_size(doc(criteria=text, plan=steps(1)))
        self.assertEqual(got, ("l", "estimated l (8 criteria, 1 step)"))

    def test_the_templates_hint_comment_is_not_a_criterion_and_not_a_step(self):
        hint = "<!-- Numbered, testable.\nR1. WHEN <situation> THE SYSTEM SHALL <behavior>. -->"
        self.assertEqual(E.doc_size(doc(criteria=hint, plan=steps(1))), ("s", "estimated s (0 criteria, 1 step)"))
        plan = "<!-- numbered steps:\n1. first\n2. second\n3. third\n4. fourth -->\n1. the only real one"
        self.assertEqual(E.doc_size(doc(criteria=criteria(1), plan=plan)), ("s", "estimated s (1 criterion, 1 step)"))

    def test_steps_are_numbered_lines_in_the_plan_only(self):
        plan = "\n".join(["1. dot", "2) paren", "   3. indented", "10. two digits", "- not numbered", "1.5 ms is not a step",
                          "step 4. nor this", "5.", "Plan notes: 6. nor this"])
        self.assertEqual(E.doc_size(doc(criteria=criteria(2), plan=plan)), ("m", "estimated m (2 criteria, 4 steps)"))
        # numbered lines outside the Plan are nobody's steps
        text = doc(criteria="R1. a\n1. b\n2. c\n3. d\n4. e\n5. f", plan=steps(2))
        self.assertEqual(E.doc_size(text), ("s", "estimated s (1 criterion, 2 steps)"))

    def test_the_sections_are_found_whatever_the_capitals(self):
        d = W.WorkDoc.parse("---\nslug: x\n---\n\n## acceptance CRITERIA\n%s\n\n## plan\n%s\n" % (criteria(8), steps(2)))
        self.assertEqual(E.doc_size(d), ("l", "estimated l (8 criteria, 2 steps)"))


class WorkDocField(unittest.TestCase):
    TEXT = ("---\nslug: a\ntitle: A\nstatus: ready\ndepends_on: []\ntest_changes: none\nsize: s\ncreated: 2026-10-14T00:00:00Z\n"
            "source: user\n---\n\n## Goal\ng\n")

    def test_size_stays_between_test_changes_and_created_when_a_doc_is_written_back(self):
        self.assertEqual(W.FIELD_ORDER, ("slug", "title", "status", "depends_on", "test_changes", "size", "created", "source"))
        d = W.WorkDoc.parse(self.TEXT)
        d.meta["size"] = "l"                               # a design session revising its guess
        self.assertEqual(W.WorkDoc.parse(d.dumps()).meta["size"], "l")
        keys = [l.split(":")[0] for l in d.dumps().split("---")[1].strip().splitlines()]
        self.assertEqual(keys, ["slug", "title", "status", "depends_on", "test_changes", "size", "created", "source"])
        late = W.WorkDoc({"source": "user", "size": "m", "slug": "a", "created": "x"}, "## Goal\ng\n")
        self.assertEqual([l.split(":")[0] for l in late.dumps().split("---")[1].strip().splitlines()],
                         ["slug", "size", "created", "source"])

    def test_the_size_is_never_validated(self):
        for given in ("huge", "", "7", "[s]"):
            d = W.WorkDoc.parse(self.TEXT.replace("size: s", "size: %s" % given))
            self.assertEqual(W.problems(d, "a"), [], given)


if __name__ == "__main__":
    unittest.main()
