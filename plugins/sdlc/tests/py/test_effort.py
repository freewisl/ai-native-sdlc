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


def doc(difficulty=None, extra="", body="## Goal\ng\n"):
    """A work doc with the given front matter difficulty (None: no field), more front matter lines and a body."""
    front = "slug: x\ntitle: x\nstatus: ready\n" + ("difficulty: %s\n" % difficulty if difficulty is not None else "") + extra
    return W.WorkDoc.parse("---\n%s---\n\n%s" % (front, body))


class DocDifficulty(unittest.TestCase):
    def test_the_scale_is_easy_normal_hard(self):
        self.assertEqual(E.DIFFICULTIES, ("easy", "normal", "hard"))

    def test_easy_normal_and_hard_are_taken_as_the_doc_says_them(self):
        for given in E.DIFFICULTIES:
            self.assertEqual(E.doc_difficulty(doc(given)), given)

    def test_it_is_read_stripped_and_lower_cased(self):
        for given, want in (("Easy", "easy"), ("EASY", "easy"), ("Hard", "hard"), ("HARD", "hard"), ("  hard  ", "hard"),
                            ("Normal", "normal"), (" NORMAL ", "normal"), ('"Hard"', "hard"), ("'easy'", "easy"),
                            ('"  Easy "', "easy")):
            self.assertEqual(E.doc_difficulty(doc(given)), want, given)

    def test_any_other_value_is_normal(self):
        for given in ("difficult", "medium", "very hard", "easy hard", "hard, easy", "hardest", "eas", "3", "s", "l", "small", "large",
                      "xl", "", "[hard]", "[easy, hard]", "hard!", "-hard"):
            self.assertEqual(E.doc_difficulty(doc(given)), "normal", given)

    def test_no_field_and_no_doc_are_normal(self):
        self.assertEqual(E.doc_difficulty(doc()), "normal")
        self.assertEqual(E.doc_difficulty(None), "normal")
        self.assertEqual(E.doc_difficulty(W.WorkDoc.parse("")), "normal")                            # not even front matter
        self.assertEqual(E.doc_difficulty(W.WorkDoc.parse("## Goal\ng\n")), "normal")

    def test_the_field_is_found_among_the_others_in_any_position(self):
        text = "---\ncreated: 2026-10-14T00:00:00Z\ndifficulty: hard\nslug: x\nsource: user\n---\n\n## Goal\ng\n"
        self.assertEqual(E.doc_difficulty(W.WorkDoc.parse(text)), "hard")

    def test_how_much_there_is_to_do_says_nothing(self):
        criteria = "\n".join("R%d. WHEN a THE SYSTEM SHALL b%d." % (i, i) for i in range(1, 10))
        plan = "\n".join("%d. step %d — checked by a test." % (i, i) for i in range(1, 13))
        big = "## Goal\ng\n\n## Acceptance criteria\n%s\n\n## Plan\n%s\n" % (criteria, plan)
        tiny = "## Goal\ng\n\n## Acceptance criteria\nR1. WHEN a THE SYSTEM SHALL b.\n\n## Plan\n1. one step\n"
        for body in (big, tiny):
            self.assertEqual(E.doc_difficulty(doc(body=body)), "normal")                              # nothing to count
            self.assertEqual(E.doc_difficulty(doc("easy", body=body)), "easy")                        # a rename across forty files
            self.assertEqual(E.doc_difficulty(doc("hard", body=body)), "hard")                        # a five-line concurrency fix

    def test_the_old_size_field_counts_for_nothing(self):
        for given in ("s", "m", "l", "small", "large", "xl"):
            self.assertEqual(E.doc_difficulty(doc(extra="size: %s\n" % given)), "normal", given)
        self.assertEqual(E.doc_difficulty(doc("easy", extra="size: l\n")), "easy")                    # and does not outvote it


class WorkDocField(unittest.TestCase):
    TEXT = ("---\nslug: a\ntitle: A\nstatus: ready\ndepends_on: []\ntest_changes: none\ndifficulty: easy\n"
            "created: 2026-10-14T00:00:00Z\nsource: user\n---\n\n## Goal\ng\n")

    def keys(self, doc_):
        return [l.split(":")[0] for l in doc_.dumps().split("---")[1].strip().splitlines()]

    def test_difficulty_stays_between_test_changes_and_created_when_a_doc_is_written_back(self):
        self.assertEqual(W.FIELD_ORDER, ("slug", "title", "status", "depends_on", "test_changes", "difficulty", "created", "source"))
        d = W.WorkDoc.parse(self.TEXT)
        d.meta["difficulty"] = "hard"                      # a design session revising its guess
        self.assertEqual(W.WorkDoc.parse(d.dumps()).meta["difficulty"], "hard")
        self.assertEqual(self.keys(d), ["slug", "title", "status", "depends_on", "test_changes", "difficulty", "created", "source"])
        late = W.WorkDoc({"source": "user", "difficulty": "normal", "slug": "a", "created": "x"}, "## Goal\ng\n")
        self.assertEqual(self.keys(late), ["slug", "difficulty", "created", "source"])

    def test_the_difficulty_is_never_validated(self):
        for given in ("impossible", "", "7", "[hard]", "HARD", "s"):
            d = W.WorkDoc.parse(self.TEXT.replace("difficulty: easy", "difficulty: %s" % given))
            self.assertEqual(W.problems(d, "a"), [], given)
        self.assertEqual(W.problems(W.WorkDoc.parse(self.TEXT.replace("difficulty: easy\n", "")), "a"), [])     # and may be left out

    def test_an_old_size_line_is_kept_after_the_known_fields_and_is_no_problem(self):
        d = W.WorkDoc.parse(self.TEXT.replace("difficulty: easy", "size: l"))
        self.assertEqual(W.problems(d, "a"), [])
        self.assertEqual(self.keys(d), ["slug", "title", "status", "depends_on", "test_changes", "created", "source", "size"])
        self.assertEqual(W.WorkDoc.parse(d.dumps()).meta["size"], "l")


if __name__ == "__main__":
    unittest.main()
