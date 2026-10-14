"""Models and efforts per phase: how the config resolves them, and what the loop passes to `claude` — the efforts as
configured, and as the loop moves them for a session (loop.auto_effort)."""
import json
import os
import re
import shutil
import tempfile
import time
import unittest

from helpers import RepoCase, STUBS

from sdlclib import config as C
from sdlclib import state as st
from sdlclib.util import epoch

DEFAULT_MODELS = {"design": "opus", "build": "sonnet", "fix": "sonnet", "self_check": "opus"}
DEFAULT_EFFORTS = {"design": "high", "build": "high", "fix": "medium", "self_check": "high"}


class ConfigDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sdlc-models-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def config(self, data):
        os.makedirs(os.path.join(self.dir, ".sdlc"), exist_ok=True)
        with open(os.path.join(self.dir, ".sdlc", "config.json"), "w") as fh:
            json.dump(data, fh)
        return C.Config(self.dir)

    def models(self, cfg):
        return {p: cfg.model(p) for p in C.PHASES}

    def efforts(self, cfg):
        return {p: cfg.effort(p) for p in C.PHASES}


class Resolution(ConfigDir):
    def test_defaults_run_design_and_self_check_on_opus_and_build_and_fix_on_sonnet(self):
        cfg = self.config({"version": 2})
        self.assertEqual(self.models(cfg), DEFAULT_MODELS)
        self.assertEqual(self.efforts(cfg), DEFAULT_EFFORTS)
        self.assertEqual(cfg.escalate_model, "opus")
        for old in ("model", "effort", "fix_model"):
            self.assertNotIn(old, C.DEFAULTS["loop"])
            self.assertNotIn(old, cfg.data["loop"])

    def test_the_loop_keys_of_1_1_have_their_defaults(self):
        cfg = self.config({"version": 2})
        self.assertEqual((cfg.get("loop.auto_effort"), cfg.get("loop.self_check_min_items"), cfg.get("loop.isolate_sessions")),
                         (True, 2, False))
        cfg = self.config({"version": 2, "loop": {"auto_effort": False, "self_check_min_items": 1, "isolate_sessions": True}})
        self.assertEqual((cfg.get("loop.auto_effort"), cfg.get("loop.self_check_min_items"), cfg.get("loop.isolate_sessions")),
                         (False, 1, True))
        self.assertEqual(self.efforts(cfg), DEFAULT_EFFORTS)           # the switch leaves the table alone

    def test_a_partial_table_changes_only_the_phases_it_names(self):
        cfg = self.config({"version": 2, "loop": {"models": {"build": "haiku"}, "efforts": {"fix": "low", "design": "max"}}})
        self.assertEqual(self.models(cfg), dict(DEFAULT_MODELS, build="haiku"))
        self.assertEqual(self.efforts(cfg), dict(DEFAULT_EFFORTS, fix="low", design="max"))

    def test_the_old_keys_still_count(self):
        # loop.model → design, build and self_check; loop.fix_model → fix; loop.effort → every phase
        cfg = self.config({"version": 2, "loop": {"model": "haiku", "fix_model": "opus", "effort": "low"}})
        self.assertEqual(self.models(cfg), {"design": "haiku", "build": "haiku", "fix": "opus", "self_check": "haiku"})
        self.assertEqual(self.efforts(cfg), {p: "low" for p in C.PHASES})

    def test_a_phase_named_in_the_new_tables_beats_the_old_keys(self):
        cfg = self.config({"version": 2, "loop": {"model": "haiku", "fix_model": "opus", "effort": "low",
                                                  "models": {"build": "sonnet", "fix": "haiku"}, "efforts": {"build": "xhigh"}}})
        self.assertEqual(self.models(cfg), {"design": "haiku", "build": "sonnet", "fix": "haiku", "self_check": "haiku"})
        self.assertEqual(self.efforts(cfg), dict({p: "low" for p in C.PHASES}, build="xhigh"))

    def test_the_old_model_key_leaves_the_fix_model_at_its_default(self):
        cfg = self.config({"version": 2, "loop": {"model": "haiku"}})
        self.assertEqual(cfg.model("fix"), "sonnet")

    def test_an_empty_value_passes_nothing(self):
        cfg = self.config({"version": 2, "loop": {"models": {"fix": ""}, "efforts": {"build": ""}}})
        self.assertEqual((cfg.model("fix"), cfg.effort("build")), ("", ""))
        # 0.x: an empty loop.effort meant "do not pass --effort"; an empty loop.model meant "the default"
        cfg = self.config({"version": 2, "loop": {"effort": "", "model": ""}})
        self.assertEqual(self.efforts(cfg), {p: "" for p in C.PHASES})
        self.assertEqual(self.models(cfg), DEFAULT_MODELS)

    def test_an_empty_escalate_model_turns_escalation_off(self):
        self.assertEqual(self.config({"version": 2, "loop": {"escalate_model": ""}}).escalate_model, "")

    def test_odd_values_do_not_break_the_config(self):
        self.assertEqual(self.models(self.config({"version": 2, "loop": "off"})), DEFAULT_MODELS)
        self.assertEqual(self.models(self.config({"version": 2, "loop": {"models": "haiku"}})), {p: "haiku" for p in C.PHASES})
        self.assertEqual(self.efforts(self.config({"version": 2, "loop": {"efforts": ["high"], "effort": None}})), DEFAULT_EFFORTS)
        self.assertEqual(self.models(self.config({"version": 2, "loop": {"models": {"build": None}, "model": 5}})), DEFAULT_MODELS)

    def test_a_version_that_is_not_a_number_reads_as_version_1(self):
        self.assertEqual([C.config_version(v) for v in ({}, {"version": 2}, {"version": "2"}, {"version": "1.0"},
                                                          {"version": "two"}, {"version": None}, [], None)], [1, 2, 2, 1, 1, 1, 1, 1])
        self.assertTrue(self.config({"version": "one"}).legacy)


class Migration(ConfigDir):
    def test_the_0x_defaults_are_not_carried(self):
        new, notes = C.migrate_v1({"version": 1, "loop": {"model": "opus", "fix_model": "sonnet", "effort": "high"}})
        self.assertEqual(new["loop"], {})
        self.assertFalse([n for n in notes if "loop.model" in n or "loop.effort" in n])

    def test_values_that_differ_from_the_0x_defaults_are_carried(self):
        new, notes = C.migrate_v1({"version": 1, "loop": {"model": "haiku", "fix_model": "opus", "effort": "medium"}})
        self.assertEqual(new["loop"], {"model": "haiku", "fix_model": "opus", "effort": "medium"})
        self.assertEqual(len([n for n in notes if "differs from the 0.x default" in n]), 3)

    def test_each_key_is_judged_on_its_own(self):
        new, _ = C.migrate_v1({"version": 1, "loop": {"model": "opus", "fix_model": "haiku", "effort": "high"}})
        self.assertEqual(new["loop"], {"fix_model": "haiku"})

    def test_a_version_1_file_resolves_the_same_way_before_init_rewrites_it(self):
        cfg = self.config({"version": 1, "loop": {"model": "haiku", "effort": "low", "item_max_minutes": 90}})
        self.assertTrue(cfg.legacy)
        self.assertEqual(self.models(cfg), {"design": "haiku", "build": "haiku", "fix": "sonnet", "self_check": "haiku"})
        self.assertEqual(self.efforts(cfg), {p: "low" for p in C.PHASES})
        cfg = self.config({"version": 1, "loop": {"model": "opus"}})            # the 0.x default: 1.0 chooses per phase
        self.assertEqual(self.models(cfg), DEFAULT_MODELS)


class ModelFor(RepoCase):
    """Engine.model_for: the configured model, escalation after a failure, and giving way to a limited family."""

    def engine(self, loop=None):
        if loop is not None:
            cfg = json.loads(self.repo.read(".sdlc/config.json"))
            cfg["loop"].update(loop)
            self.repo.write(".sdlc/config.json", json.dumps(cfg))
        return self.repo.engine()

    def block(self, e, family, hours=5):
        e.blocked_family[family] = epoch() + hours * 3600

    def test_each_phase_has_its_model(self):
        e = self.engine()
        self.assertEqual({p: e.model_for(p, "a-first") for p in C.PHASES}, DEFAULT_MODELS)

    def test_a_build_after_a_failure_runs_on_the_escalation_model(self):
        e = self.engine()
        self.assertEqual(e.model_for("build", "a-first"), "sonnet")
        st.update(self.repo.root, "a-first", failures=1)
        self.assertEqual(e.model_for("build", "a-first"), "opus")
        self.assertEqual(e.model_for("build", "other"), "sonnet")                    # another item is not affected
        self.assertEqual([e.model_for(p, "a-first") for p in ("design", "fix", "self_check")], ["opus", "sonnet", "opus"])
        self.assertEqual(e.model_for("build"), "sonnet")                             # no item, no escalation
        st.update(self.repo.root, "a-first", failures=None)                          # `retry` clears the record
        self.assertEqual(e.model_for("build", "a-first"), "sonnet")

    def test_an_empty_escalation_model_keeps_the_build_model(self):
        e = self.engine({"escalate_model": ""})
        st.update(self.repo.root, "a-first", failures=2)
        self.assertEqual(e.model_for("build", "a-first"), "sonnet")

    def test_a_limited_family_gives_way_to_a_configured_model_of_another_family(self):
        e = self.engine()
        self.block(e, "sonnet")
        self.assertEqual([e.model_for(p, "a-first") for p in C.PHASES], ["opus", "opus", "opus", "opus"])
        e = self.engine()
        self.block(e, "opus")
        self.assertEqual([e.model_for(p, "a-first") for p in C.PHASES], ["sonnet"] * 4)

    def test_an_escalated_build_gives_way_too(self):
        e = self.engine()
        st.update(self.repo.root, "a-first", failures=1)
        self.block(e, "opus")
        self.assertEqual(e.model_for("build", "a-first"), "sonnet")

    def test_with_no_other_family_configured_it_falls_back_to_sonnet_then_opus(self):
        e = self.engine({"models": {p: "opus" for p in C.PHASES}, "escalate_model": ""})
        self.block(e, "opus")
        self.assertEqual(e.model_for("design", "a-first"), "sonnet")
        e = self.engine({"models": {p: "sonnet" for p in C.PHASES}})
        self.block(e, "sonnet")
        self.assertEqual(e.model_for("build", "a-first"), "opus")                    # escalate_model is opus; and so is the last resort
        e = self.engine({"models": {p: "sonnet" for p in C.PHASES}, "escalate_model": ""})
        self.block(e, "sonnet")
        self.assertEqual(e.model_for("build", "a-first"), "opus")

    def test_a_family_that_is_limited_beside_everything_else_keeps_its_model(self):
        e = self.engine()
        self.block(e, "opus")
        self.block(e, "sonnet")
        self.assertEqual(e.model_for("build", "a-first"), "sonnet")                  # nothing to switch to: the limit handler waits

    def test_a_limit_that_has_reset_no_longer_counts(self):
        e = self.engine()
        e.blocked_family["sonnet"] = epoch() - 1
        self.assertEqual(e.model_for("build", "a-first"), "sonnet")

    def test_full_model_ids_are_matched_by_family(self):
        e = self.engine({"models": {"build": "claude-sonnet-5-5", "design": "claude-opus-5-5"}})
        self.block(e, "sonnet")
        self.assertEqual(e.model_for("build", "a-first"), "claude-opus-5-5")


class Sessions(RepoCase):
    """What the loop passes to `claude`, read from the stub's call log — with auto effort off: the table as written."""
    config = {"loop": {"auto_effort": False}}

    def call(self, key):
        return [c for c in self.repo.calls() if c["key"] == key]

    def test_design_runs_on_opus_and_build_on_sonnet_each_with_its_effort(self):
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([(c["key"], c["model"], c["effort"]) for c in self.repo.calls()],
                         [("a-first:design", "opus", "high"), ("a-first:build", "sonnet", "high")])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_fix_session_runs_on_sonnet_at_medium_effort(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["fail_gate"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([(c["model"], c["effort"]) for c in self.call("a-first:fix")], [("sonnet", "medium")])

    def test_a_build_after_a_failed_run_is_escalated_to_opus(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["gave_up", "ok"]})
        self.assertEqual(self.repo.engine().run(), 1)
        self.assertEqual(self.repo.items_state()["a-first"]["failures"], 1)
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([(c["model"], c["effort"]) for c in self.call("a-first:build")], [("sonnet", "high"), ("opus", "high")])
        self.assertEqual([c["model"] for c in self.call("a-first:design")], ["opus"])        # designed once, not redone
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_the_old_keys_in_the_config_are_honoured(self):
        self.repo.add_items("a-first")
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["loop"].update({"model": "haiku", "effort": "low", "fix_model": "opus"})
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "old keys")
        self.repo.scenario({"a-first:build": ["fail_gate"]})
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([(c["key"], c["model"], c["effort"]) for c in self.repo.calls()],
                         [("a-first:design", "haiku", "low"), ("a-first:build", "haiku", "low"), ("a-first:fix", "opus", "low")])

    def test_a_blocked_family_switches_the_next_session_and_the_effort_stays_the_phases(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:design": ["limit_opus", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        design = self.call("a-first:design")
        self.assertEqual([(c["model"], c["effort"], c["resumed"]) for c in design], [("opus", "high", False), ("sonnet", "high", False)])
        self.assertEqual([c["model"] for c in self.call("a-first:build")], ["sonnet"])
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_sonnet_limit_in_the_build_moves_it_to_opus(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_sonnet", "ok"]})
        self.assertEqual(self.repo.engine().run(), 0)
        builds = self.call("a-first:build")
        self.assertEqual([(c["model"], c["resumed"]) for c in builds], [("sonnet", False), ("opus", False)])   # another model: no resume
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_stop_resumes_only_on_the_model_it_ran_on(self):
        self.repo.add_items("a-first")
        self.repo.scenario({"a-first:build": ["limit_session:7200", "ok"]})
        e = self.repo.engine()
        e.limit_wait = 60
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.repo.items_state()["a-first"]["stop"]["model"], "sonnet")
        self.assertEqual(self.repo.engine().run(), 0)
        builds = self.call("a-first:build")
        self.assertEqual([(c["model"], c["resumed"]) for c in builds], [("sonnet", False), ("sonnet", True)])


class SelfCheck(RepoCase):
    config = {"loop": {"self_check": True, "self_check_min_items": 1, "auto_effort": False}}

    def test_the_self_check_runs_on_its_own_model_and_effort(self):
        self.repo.add_items("a-first")
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["loop"].update({"models": {"self_check": "haiku"}, "efforts": {"self_check": "low"}})
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "self-check model")
        self.assertEqual(self.repo.engine().run(), 0)
        checks = [(c["model"], c["effort"]) for c in self.repo.calls() if c["key"] == "self-check"]
        self.assertEqual(checks, [("haiku", "low")])
        self.assertEqual([(c["model"], c["effort"]) for c in self.repo.calls() if c["key"] == "a-first:build"], [("sonnet", "high")])


def queue_doc(repo, slug, size=None, criteria=1, plan=0):
    """Commit a work doc to main with `size:` in its front matter (None: the field is left out), `criteria` acceptance
    criteria and a Plan of `plan` numbered steps (0: no Plan section, as the loop finds a doc before its design)."""
    text = repo.doc_text(slug, acceptance="\n".join("R%d. WHEN a THE SYSTEM SHALL b." % (i + 1) for i in range(criteria)))
    if size is not None:
        text = text.replace("\ncreated:", "\nsize: %s\ncreated:" % size, 1)
    if plan:
        text += "\n## Plan\n" + "\n".join("%d. step %d" % (i + 1, i + 1) for i in range(plan)) + "\n"
    repo.write("work/%s.md" % slug, text)
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "queue")


class EffortFor(RepoCase):
    """Engine.effort_for: `loop.efforts` moved by the item's size, earlier failures, fix rounds and the size of the range a
    self-check reads. The doc is read as it is in the working tree when the question is asked."""

    def engine(self, loop=None):
        if loop is not None:
            cfg = json.loads(self.repo.read(".sdlc/config.json"))
            cfg["loop"].update(loop)
            self.repo.write(".sdlc/config.json", json.dumps(cfg))
        return self.repo.engine()

    def efforts(self, e, slug="a-first", **kw):
        return {p: e.effort_for(p, slug, **kw) for p in ("design", "build", "fix")}

    def test_a_small_item_lowers_design_and_build_by_one_and_leaves_the_fix(self):
        queue_doc(self.repo, "a-first", size="s")
        self.assertEqual(self.efforts(self.engine()), {"design": ("medium", "size s"), "build": ("medium", "size s"),
                                                      "fix": ("medium", "")})     # medium is the fix's own: not lowered to low

    def test_a_large_item_raises_all_three_by_one(self):
        queue_doc(self.repo, "a-first", size="l")
        self.assertEqual(self.efforts(self.engine()), {"design": ("xhigh", "size l"), "build": ("xhigh", "size l"),
                                                      "fix": ("high", "size l")})

    def test_a_medium_item_moves_nothing(self):
        queue_doc(self.repo, "a-first", size="m")
        self.assertEqual(self.efforts(self.engine()), {"design": ("high", ""), "build": ("high", ""), "fix": ("medium", "")})

    def test_without_a_size_a_design_runs_at_its_base_and_build_and_fix_go_by_the_estimate(self):
        queue_doc(self.repo, "a-first", criteria=8, plan=9)
        self.assertEqual(self.efforts(self.engine()), {"design": ("high", ""),
                                                      "build": ("xhigh", "estimated l (8 criteria, 9 steps)"),
                                                      "fix": ("high", "estimated l (8 criteria, 9 steps)")})
        queue_doc(self.repo, "b-second", criteria=1, plan=2)
        e = self.engine()
        self.assertEqual(self.efforts(e, "b-second"), {"design": ("high", ""),
                                                       "build": ("medium", "estimated s (1 criterion, 2 steps)"),
                                                       "fix": ("medium", "")})
        queue_doc(self.repo, "c-third", criteria=3, plan=5)                  # medium: nothing to say
        self.assertEqual(self.efforts(e, "c-third"), {"design": ("high", ""), "build": ("high", ""), "fix": ("medium", "")})

    def test_a_doc_before_its_design_has_no_plan_to_count_and_runs_at_the_base(self):
        queue_doc(self.repo, "a-first", criteria=9)
        self.assertEqual(self.efforts(self.engine()), {"design": ("high", ""), "build": ("high", ""), "fix": ("medium", "")})

    def test_the_explicit_size_beats_the_estimate_and_counts_in_a_design(self):
        queue_doc(self.repo, "a-first", size="l", criteria=1, plan=2)         # the design session guessed larger than the plan
        e = self.engine()
        self.assertEqual(e.effort_for("design", "a-first"), ("xhigh", "size l"))
        self.assertEqual(e.effort_for("build", "a-first"), ("xhigh", "size l"))

    def test_an_item_that_failed_before_gets_one_more_in_every_phase(self):
        queue_doc(self.repo, "a-first", size="m")
        e = self.engine()
        st.update(self.repo.root, "a-first", failures=1)
        self.assertEqual(self.efforts(e), {"design": ("xhigh", "failed before"), "build": ("xhigh", "failed before"),
                                          "fix": ("high", "failed before")})
        self.assertEqual(self.efforts(e, "other"), {p: (e.cfg.effort(p), "") for p in ("design", "build", "fix")})
        st.update(self.repo.root, "a-first", failures=None)                   # `retry` clears the record
        self.assertEqual(e.effort_for("build", "a-first"), ("high", ""))
        st.update(self.repo.root, "a-first", failures=0)
        self.assertEqual(e.effort_for("build", "a-first"), ("high", ""))

    def test_a_second_fix_round_and_every_one_after_it_get_one_more_not_one_more_each(self):
        queue_doc(self.repo, "a-first", size="m")
        e = self.engine()
        self.assertEqual([e.effort_for("fix", "a-first", fix_round=r) for r in (0, 1, 2, 3, 4)],
                         [("medium", ""), ("medium", ""), ("high", "fix round 2"), ("high", "fix round 3"),
                          ("high", "fix round 4")])

    def test_the_parts_add_up_and_the_reason_lists_each_in_turn(self):
        queue_doc(self.repo, "a-first", criteria=8, plan=9)
        e = self.engine({"efforts": {"build": "low", "fix": "low"}})
        st.update(self.repo.root, "a-first", failures=2)
        # +1 for the size, +1 for the failures; the second fix round makes it three
        self.assertEqual(e.effort_for("build", "a-first"),
                         ("high", "estimated l (8 criteria, 9 steps), failed before"))        # the example in the docs
        self.assertEqual(e.effort_for("fix", "a-first", fix_round=2),
                         ("xhigh", "estimated l (8 criteria, 9 steps), failed before, fix round 2"))

    def test_a_small_item_that_failed_is_back_at_its_base(self):
        queue_doc(self.repo, "a-first", size="s")
        e = self.engine()
        st.update(self.repo.root, "a-first", failures=1)
        self.assertEqual(e.effort_for("build", "a-first"), ("high", "size s, failed before"))     # −1 and +1: the rules still show

    def test_the_effort_stops_at_low_and_xhigh_and_the_reason_still_names_what_was_tried(self):
        queue_doc(self.repo, "a-first", size="s")
        e = self.engine({"efforts": {"design": "low", "build": "xhigh"}})
        self.assertEqual(e.effort_for("design", "a-first"), ("low", "size s"))
        queue_doc(self.repo, "a-first", size="l")
        self.assertEqual(e.effort_for("build", "a-first"), ("xhigh", "size l"))

    def test_an_effort_that_is_empty_max_or_not_a_level_is_left_alone_with_no_reason(self):
        queue_doc(self.repo, "a-first", size="l")
        st.update(self.repo.root, "a-first", failures=3)
        e = self.engine({"efforts": {"design": "", "build": "max", "fix": "turbo"}})
        self.assertEqual(self.efforts(e, fix_round=2), {"design": ("", ""), "build": ("max", ""), "fix": ("turbo", "")})

    def test_the_doc_is_read_each_time_it_is_asked(self):
        queue_doc(self.repo, "a-first", size="s")
        e = self.engine()
        self.assertEqual(e.effort_for("build", "a-first"), ("medium", "size s"))
        self.repo.write("work/a-first.md", self.repo.read("work/a-first.md").replace("size: s", "size: l"))   # the design revised it
        self.assertEqual(e.effort_for("build", "a-first"), ("xhigh", "size l"))
        self.repo.write("work/a-first.md", self.repo.read("work/a-first.md").replace("size: l\n", ""))
        self.assertEqual(e.effort_for("build", "a-first"), ("high", ""))                     # no size, no plan: the base

    def test_no_slug_or_no_doc_is_the_base(self):
        e = self.engine()
        self.assertEqual(e.effort_for("build"), ("high", ""))
        self.assertEqual(e.effort_for("design", "nothing-here"), ("high", ""))

    def test_a_self_check_goes_by_the_size_of_the_range(self):
        e = self.engine()
        self.assertEqual([e.effort_for("self_check", diff_lines=n) for n in (None, 0, 1, 299, 300, 1999, 2000, 50000)],
                         [("high", ""), ("medium", "small range (0 lines)"), ("medium", "small range (1 line)"),
                          ("medium", "small range (299 lines)"), ("high", ""), ("high", ""),
                          ("xhigh", "large range (2000 lines)"), ("xhigh", "large range (50000 lines)")])
        e = self.engine({"efforts": {"self_check": "max"}})
        self.assertEqual(e.effort_for("self_check", diff_lines=10), ("max", ""))

    def test_a_range_says_nothing_to_the_item_phases_and_an_item_nothing_to_the_self_check(self):
        queue_doc(self.repo, "a-first", size="l")
        st.update(self.repo.root, "a-first", failures=1)
        e = self.engine()
        self.assertEqual(e.effort_for("build", "a-first", diff_lines=10), ("xhigh", "size l, failed before"))
        self.assertEqual(e.effort_for("self_check", "a-first", fix_round=3), ("high", ""))

    def test_with_auto_effort_off_the_table_is_passed_as_written(self):
        queue_doc(self.repo, "a-first", size="l")
        st.update(self.repo.root, "a-first", failures=2)
        e = self.engine({"auto_effort": False})
        self.assertEqual(self.efforts(e, fix_round=3), {"design": ("high", ""), "build": ("high", ""), "fix": ("medium", "")})
        self.assertEqual(e.effort_for("self_check", diff_lines=5), ("high", ""))

    def test_the_range_is_lines_added_plus_removed(self):
        e = self.engine()
        first = self.repo.git("rev-parse", "HEAD")
        self.repo.write("x.txt", "one\ntwo\nthree\nfour\n")                               # +4
        self.repo.write("tests/test_base.py", "def test_base():\n    assert True\n")      # one line replaced: +1 -1
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "y")
        head = self.repo.git("rev-parse", "HEAD")
        self.assertEqual(e.range_lines(first, head), 6)
        self.assertEqual(e.range_lines(head, head), 0)                                      # nothing between: not "unknown"
        self.assertIsNone(e.range_lines("0" * 40, head))                                    # git cannot say: no adjustment


class AutoEffort(RepoCase):
    """What the loop passes to `claude` with auto effort on (the default): the table moved for this item, session by session."""

    def call(self, key):
        return [c for c in self.repo.calls() if c["key"] == key]

    def pairs(self, key):
        return [(c["model"], c["effort"]) for c in self.call(key)]

    def log(self, e):
        with open(e.log_path) as fh:
            return fh.read()

    def session_events(self):
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            return [e["detail"] for e in map(json.loads, fh) if e["event"] == "loop.session"]

    def set_loop(self, **loop):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        for k, v in loop.items():
            if isinstance(v, dict) and isinstance(cfg["loop"].get(k), dict):
                cfg["loop"][k].update(v)
            else:
                cfg["loop"][k] = v
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "loop config")

    def test_a_small_item_runs_design_and_build_one_level_lower_not_the_fix_and_the_log_says_why(self):
        queue_doc(self.repo, "a-first", size="s")
        self.repo.scenario({"a-first:build": ["fail_gate"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.pairs("a-first:design"), [("opus", "medium")])
        self.assertEqual(self.pairs("a-first:build"), [("sonnet", "medium")])
        self.assertEqual(self.pairs("a-first:fix"), [("sonnet", "medium")])              # the fix's own medium, not lowered
        log = self.log(e)
        self.assertIn("DESIGN  a-first — session (model opus, effort medium — size s)", log)
        self.assertIn("BUILD   a-first — session (model sonnet, effort medium — size s)", log)
        self.assertIn("FIX     a-first — session (model sonnet, effort medium)\n", log)       # nothing moved it: no reason
        self.assertEqual(self.repo.status_of("a-first"), "done")

    def test_a_large_item_runs_all_three_one_level_higher(self):
        queue_doc(self.repo, "a-first", size="l")
        self.repo.scenario({"a-first:build": ["fail_gate"]})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([self.pairs("a-first:design"), self.pairs("a-first:build"), self.pairs("a-first:fix")],
                         [[("opus", "xhigh")], [("sonnet", "xhigh")], [("sonnet", "high")]])
        self.assertIn("BUILD   a-first — session (model sonnet, effort xhigh — size l)", self.log(e))
        self.assertIn("FIX     a-first — session (model sonnet, effort high — size l)", self.log(e))

    def test_an_item_without_a_size_is_estimated_from_its_plan_by_the_build_and_the_log_shows_the_count(self):
        self.repo.add_items("a-first")                                  # one criterion, and the stub designs a one-step plan
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.pairs("a-first:design"), [("opus", "high")])                # a design has no plan to count yet
        self.assertEqual(self.pairs("a-first:build"), [("sonnet", "medium")])
        self.assertIn("BUILD   a-first — session (model sonnet, effort medium — estimated s (1 criterion, 1 step))", self.log(e))

    def test_an_item_that_failed_before_gets_one_more_on_top_of_the_escalated_model(self):
        queue_doc(self.repo, "a-first", size="m")
        self.repo.scenario({"a-first:build": ["gave_up", "ok"]})
        self.assertEqual(self.repo.engine().run(), 1)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.pairs("a-first:build"), [("sonnet", "high"), ("opus", "xhigh")])
        self.assertEqual(self.pairs("a-first:design"), [("opus", "high")])                # designed once, not redone
        self.assertIn("BUILD   a-first — session (model opus, effort xhigh — failed before)", self.log(e))

    def test_the_second_fix_round_gets_one_more(self):
        queue_doc(self.repo, "a-first", size="m")
        self.repo.scenario({"a-first:build": ["fail_gate"], "a-first:fix": ["gave_up", "ok"]})     # round 1 changes nothing
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual(self.pairs("a-first:fix"), [("sonnet", "medium"), ("sonnet", "high")])
        log = self.log(e)
        self.assertIn("FIX     a-first — session (model sonnet, effort medium)\n", log)
        self.assertIn("FIX     a-first — session (model sonnet, effort high — fix round 2)", log)
        self.assertEqual([d["effort_reason"] for d in self.session_events() if d["phase"] == "fix"], ["", "fix round 2"])

    def test_an_empty_effort_passes_no_flag_and_max_stays_max(self):
        self.set_loop(efforts={"design": "", "build": "max"})
        queue_doc(self.repo, "a-first", size="l")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        design, build = self.call("a-first:design")[0], self.call("a-first:build")[0]
        self.assertEqual((design["effort"], "--effort" in design["args"]), ("", False))
        self.assertEqual(build["effort"], "max")
        self.assertEqual(build["args"][build["args"].index("--effort") + 1], "max")
        self.assertIn("DESIGN  a-first — session (model opus)\n", self.log(e))                  # no effort, nothing to explain
        self.assertIn("BUILD   a-first — session (model sonnet, effort max)\n", self.log(e))     # a person's choice, no reason

    def test_with_auto_effort_off_the_table_goes_through_as_written_and_the_header_does_not_say_auto(self):
        self.set_loop(auto_effort=False)
        queue_doc(self.repo, "a-first", size="l")
        self.repo.scenario({"a-first:build": ["gave_up", "ok"]})
        self.assertEqual(self.repo.engine().run(), 1)
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([c["effort"] for c in self.call("a-first:design") + self.call("a-first:build")], ["high", "high", "high"])
        self.assertEqual({d["effort_reason"] for d in self.session_events()}, {""})
        log = self.log(e)
        self.assertIn("efforts=design:high/build:high/fix:medium\n", log)
        self.assertNotIn("(auto)", log)
        self.assertNotIn("size l", log)

    def test_the_run_header_names_the_efforts_and_says_auto(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        header = self.log(e).splitlines()[0]
        self.assertRegex(header, r"^\d\d:\d\d:\d\d == sdlc run  .*  models=design:opus/build:sonnet/fix:sonnet  "
                                 r"efforts=design:high/build:high/fix:medium \(auto\)$")
        self.set_loop(efforts={"fix": ""})
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertTrue(self.log(e).splitlines()[0].endswith("efforts=design:high/build:high/fix:default (auto)"))

    def test_the_session_event_carries_the_effort_and_why(self):
        queue_doc(self.repo, "a-first", size="s")
        self.assertEqual(self.repo.engine().run(), 0)
        sessions = {d["phase"]: d for d in self.session_events()}
        self.assertEqual((sessions["design"]["effort"], sessions["design"]["effort_reason"]), ("medium", "size s"))
        self.assertEqual((sessions["build"]["effort"], sessions["build"]["effort_reason"]), ("medium", "size s"))
        self.assertEqual([d["continued"] for d in sessions.values()], [False, False])

    def test_a_continuation_works_out_its_effort_again_and_passes_it_on_the_resume(self):
        queue_doc(self.repo, "a-first", size="s")
        self.repo.scenario({"a-first:build": ["max_turns", "ok"]})
        e = self.repo.engine()
        asked = []
        orig = e.effort_for
        e.effort_for = lambda *a, **k: (asked.append(a[0]), orig(*a, **k))[1]
        self.assertEqual(e.run(), 0)
        self.assertEqual(asked, ["design", "build", "build"])                           # one answer per window, not per phase
        builds = self.call("a-first:build")
        self.assertEqual([(c["resumed"], c["effort"]) for c in builds], [(False, "medium"), (True, "medium")])
        self.assertIn("--effort", builds[1]["args"])
        log = self.log(e)
        self.assertIn("BUILD   a-first — resuming session (model sonnet, effort medium — size s)", log)

    def test_the_status_file_and_the_status_command_name_the_running_sessions_model_and_effort(self):
        queue_doc(self.repo, "a-first", size="l")
        self.repo.scenario({"a-first:build": ["sleep:60", "ok"]})
        r = self.repo.cli("run", "--detach", "--claude-bin", STUBS + "/claude")
        self.addCleanup(self.repo.cli, "stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for _ in range(300):
            if any(c["key"] == "a-first:build" for c in self.repo.calls()):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        with open(self.repo.path(".sdlc/state/loop-status.json")) as fh:
            status = json.load(fh)
        self.assertEqual((status["phase"], status["model"], status["effort"]), ("build", "sonnet", "xhigh"))
        self.assertRegex(self.repo.cli("status").stdout, r"loop: running \(pid \d+\) — build a-first for \d+s \(sonnet · xhigh\) · this run \$")
        out = json.loads(self.repo.cli("status", "--json").stdout)["loop"]
        self.assertEqual((out["running"], out["phase"], out["model"], out["effort"]), (True, "build", "sonnet", "xhigh"))


class SelfCheckEffort(RepoCase):
    """The self-check reads what merged: a small range with less effort than configured, a large one with more."""
    config = {"loop": {"self_check": True, "self_check_min_items": 1}}

    def checks(self):
        return [c for c in self.repo.calls() if c["key"] == "self-check"]

    def test_a_small_range_is_read_with_one_level_less_and_the_run_says_why(self):
        self.repo.add_items("a-first")
        e = self.repo.engine()
        self.assertEqual(e.run(), 0)
        self.assertEqual([(c["model"], c["effort"]) for c in self.checks()], [("opus", "medium")])
        with open(e.log_path) as fh:
            log = fh.read()
        self.assertRegex(log, r"CHECK   reviewing what merged \(\w+\.\.\w+\) \(model opus, effort medium — small range \(\d+ lines\)\)")
        with open(self.repo.path(".sdlc/logs/events.jsonl")) as fh:
            ev = [json.loads(l)["detail"] for l in fh if '"loop.session"' in l and '"self-check"' in l][0]
        self.assertEqual(ev["effort"], "medium")
        self.assertRegex(ev["effort_reason"], r"^small range \(\d+ lines\)$")
        self.assertIs(ev["continued"], False)

    def test_a_large_range_is_read_with_one_level_more(self):
        e = self.repo.engine()
        e.from_sha = self.repo.git("rev-parse", "HEAD")
        self.repo.write("generated.txt", "x\n" * 2100)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "a big merge")
        e.merged_since_check = ["a-first"]
        e.self_check()
        self.assertEqual([c["effort"] for c in self.checks()], ["xhigh"])
        self.assertIn("effort xhigh — large range (2100 lines)", open(e.log_path).read())

    def test_with_auto_effort_off_it_runs_at_the_configured_effort(self):
        cfg = json.loads(self.repo.read(".sdlc/config.json"))
        cfg["loop"]["auto_effort"] = False
        self.repo.write(".sdlc/config.json", json.dumps(cfg))
        self.repo.git("commit", "-qam", "auto effort off")
        self.repo.add_items("a-first")
        self.assertEqual(self.repo.engine().run(), 0)
        self.assertEqual([c["effort"] for c in self.checks()], ["high"])


if __name__ == "__main__":
    unittest.main()
