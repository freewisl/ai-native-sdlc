"""Models and efforts per phase: how the config resolves them, and what the loop passes to `claude`."""
import json
import os
import shutil
import tempfile
import unittest

from helpers import RepoCase

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
    """What the loop passes to `claude`, read from the stub's call log."""

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
    config = {"loop": {"self_check": True}}

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


if __name__ == "__main__":
    unittest.main()
