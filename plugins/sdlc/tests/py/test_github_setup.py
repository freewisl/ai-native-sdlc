"""scripts/github-setup.sh against the stub gh: what it asks GitHub for, the ruleset it builds, and that no credential is
ever handled — dry runs and real runs, a team repository and a solo one."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import BASE_ENV, PLUGIN, STUBS

SCRIPT = os.path.join(PLUGIN, "scripts", "github-setup.sh")
# not real credentials: what the environment of a developer's machine might hold, to see that none of it is touched
FAKE_TOKENS = {"GH_TOKEN": "ghp_example_not_a_real_token_0001", "GITHUB_TOKEN": "ghs_example_not_a_real_token_0002",
               "ANTHROPIC_API_KEY": "sk-ant-example-not-a-real-key-0003", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-example-not-a-real-token-0004"}


class Setup(unittest.TestCase):
    remote = "git@github.com:acme/widgets.git"
    config = None                       # None: no .sdlc/config.json

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sdlc-ghsetup-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.tmp, "home"))
        os.makedirs(self.root)
        self.db = os.path.join(self.tmp, "gh-db.json")
        self.git("init", "-q", "-b", "main")
        if self.remote:
            self.git("remote", "add", "origin", self.remote)
        if self.config is not None:
            os.makedirs(os.path.join(self.root, ".sdlc"))
            with open(os.path.join(self.root, ".sdlc", "config.json"), "w") as fh:
                json.dump(self.config, fh)

    def env(self, **extra):
        e = dict(os.environ)
        e.update(BASE_ENV)
        for k in ("CLAUDE_PROJECT_DIR", "GH_HOST", "STUB_GH_COLLAB", "STUB_GH_LOGGED_OUT", "STUB_GH_RULESET_ID", "STUB_GH_RULESET_FAIL",
                  "STUB_GH_PATCH_FAIL", "STUB_GH_NO_REPO", "STUB_GH_LOGIN_FAIL", "SHIM_LOOKUP_BODY", "SHIM_LOOKUP_RC") + tuple(FAKE_TOKENS):
            e.pop(k, None)
        e.update({"STUB_GH_DB": self.db, "HOME": os.path.join(self.tmp, "home"), "PATH": STUBS + os.pathsep + e.get("PATH", "")})
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def git(self, *args):
        r = subprocess.run(["git", "-C", self.root] + list(args), capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def run_script(self, *args, cwd=None, **env):
        return subprocess.run(["bash", SCRIPT] + list(args), capture_output=True, text=True, cwd=cwd or self.root,
                              env=self.env(**env), timeout=120)

    # ---------- what the stub saw ----------
    def gh_db(self):
        try:
            with open(self.db) as fh:
                return json.load(fh)
        except OSError:
            return {}

    def calls(self):
        return [" ".join(c) for c in self.gh_db().get("calls", [])]

    def inputs(self):
        return self.gh_db().get("inputs", [])

    def payloads(self):
        return [json.loads(i["stdin"]) for i in self.inputs()]

    def dry_payload(self, out):
        """The JSON block a dry run prints after the RULESET line (indented by 8)."""
        lines = out.split("RULESET would", 1)[1].split("\n")[1:]
        return json.loads("\n".join(l[8:] for l in lines if l.startswith("        ")))

    @staticmethod
    def rule(payload, kind):
        return next((r for r in payload["rules"] if r["type"] == kind), None)

    def assertNoCredentials(self, *texts):
        everything = "\n".join(texts) + json.dumps(self.gh_db())
        for name, value in FAKE_TOKENS.items():
            self.assertNotIn(value, everything, name)


# =====================================================================================================================
class Static(unittest.TestCase):
    def test_the_script_is_valid_bash(self):
        for bash in ("bash", "/bin/bash"):                    # /bin/bash is 3.2 on a Mac: the script has to parse there too
            if shutil.which(bash):
                r = subprocess.run([bash, "-n", SCRIPT], capture_output=True, text=True)
                self.assertEqual((r.returncode, r.stderr), (0, ""), bash)

    def test_nothing_of_the_ci_secret_and_evals_machinery_is_left(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        for gone in ("gh secret", "keychain", "security find", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ci.mode", "ci.auth",
                     "sdlc-evals", 'checks="evals"', "token_file", "SDLC_CI_TOKEN_FILE", "save_token", "secret_name"):
            self.assertNotIn(gone, text)
        for option in ("--token", "--auth"):             # only the refusal of the removed options may still name them
            self.assertEqual([l for l in text.splitlines() if option in l and "was removed in 1.0" not in l and "--auth|" not in l], [],
                             option)
        self.assertTrue(text.startswith("#!/bin/bash\n# github-setup.sh — the GitHub side of an sdlc repository"))

    def test_the_helper_libraries_are_still_sourced(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            self.assertIn('. "$here/lib/common.sh"; . "$here/lib/scripts-extra.sh"', fh.read())


class Usage(Setup):
    def test_help_prints_the_header_and_exits_0(self):
        r = self.run_script("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("github-setup.sh — the GitHub side of an sdlc repository"), r.stdout[:80])
        for opt in ("--login", "--repo", "--approvals", "--checks", "--bypass", "--no-bypass", "--ruleset-name", "--dry-run", "--dir"):
            self.assertIn(opt, r.stdout)
        self.assertNotIn("set -u", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_removed_options_are_refused_with_a_reason(self):
        for args in (["--auth", "oauth"], ["--token-stdin"], ["--token-env", "X"], ["--save-token"], ["--save-token-file"]):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("%s was removed in 1.0" % args[0], r.stderr)
            self.assertIn("no CI secret to set", r.stderr)
            self.assertEqual(r.stdout, "")
        self.assertEqual(self.calls(), [])

    def test_an_unknown_option_and_a_missing_value_exit_2(self):
        r = self.run_script("--frobnicate")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option: --frobnicate", r.stderr)
        self.assertIn("Usage: github-setup.sh", r.stderr)
        for opt in ("--repo", "--approvals", "--checks", "--ruleset-name", "--dir"):
            r = self.run_script(opt)
            self.assertEqual(r.returncode, 2, opt)
            self.assertIn("%s needs a value" % opt, r.stderr)

    def test_approvals_must_be_a_number_from_0_to_10(self):
        for bad in ("x", "-1", "11", "1,2", "1 }"):
            r = self.run_script("--approvals", bad, "--dry-run")
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("--approvals must be a number from 0 to 10", r.stderr)
        for good in ("0", "1", "10"):
            self.assertEqual(self.run_script("--approvals", good, "--dry-run").returncode, 0, good)

    def test_repo_must_look_like_owner_name(self):
        r = self.run_script("--repo", "widgets", "--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--repo must be owner/name", r.stderr)

    def test_a_dir_that_is_not_a_directory_is_refused(self):
        r = self.run_script("--dir", os.path.join(self.tmp, "nowhere"), "--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not a directory", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_without_gh_it_explains_what_to_do_by_hand(self):
        if shutil.which("gh", path="/usr/bin:/bin"):
            self.skipTest("a real gh lives in /usr/bin or /bin")
        r = self.run_script(PATH="/usr/bin:/bin")
        self.assertEqual(r.returncode, 2)
        self.assertIn("gh (GitHub CLI) is not installed", r.stderr)
        self.assertIn("Rulesets → New branch ruleset", r.stderr)
        self.assertNotIn("Secrets", r.stderr)
        self.assertNotIn("setup-token", r.stderr)


class DryRun(Setup):
    def test_a_team_repository_gets_the_strict_ruleset_and_nothing_is_changed(self):
        r = self.run_script("--dry-run", STUB_GH_COLLAB="4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("== github-setup  repo=acme/widgets  default=main  private=true  collaborators=4  solo=0 (team)  (dry run)", r.stdout)
        self.assertIn("REPO    would set allow_auto_merge=true, delete_branch_on_merge=true", r.stdout)
        self.assertIn("RULESET would create 'sdlc: protect default branch' on main (code-owner review: yes):", r.stdout)
        p = self.dry_payload(r.stdout)
        self.assertEqual((p["name"], p["target"], p["enforcement"]), ("sdlc: protect default branch", "branch", "active"))
        self.assertEqual(p["conditions"], {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}})
        self.assertEqual([x["type"] for x in p["rules"]], ["deletion", "non_fast_forward", "pull_request"])
        pr = self.rule(p, "pull_request")["parameters"]
        self.assertEqual(pr, {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True, "require_code_owner_review": True,
                              "require_last_push_approval": False, "required_review_thread_resolution": True})
        self.assertEqual(p["bypass_actors"], [])
        self.assertIsNone(self.rule(p, "required_status_checks"))                   # no `evals`, no check at all by default
        self.assertNotIn("evals", r.stdout)
        self.assertEqual([c for c in self.calls() if "-X" in c.split()], [])        # nothing was written
        self.assertEqual(self.inputs(), [])
        self.assertNotIn("secret", " ".join(self.calls()))

    def test_a_solo_repository_by_config_needs_no_approval_and_no_code_owner(self):
        self.config = {"version": 2, "mode": "solo"}
        self.setUp()
        r = self.run_script("--dry-run", STUB_GH_COLLAB="4")                         # the config wins over the head count
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("collaborators=4  solo=1 (mode solo)", r.stdout)
        self.assertIn("(code-owner review: no)", r.stdout)
        pr = self.rule(self.dry_payload(r.stdout), "pull_request")["parameters"]
        self.assertEqual((pr["required_approving_review_count"], pr["require_code_owner_review"]), (0, False))

    def test_a_repository_with_one_collaborator_is_solo(self):
        r = self.run_script("--dry-run", STUB_GH_COLLAB="1")
        self.assertIn("collaborators=1  solo=1 (one collaborator)", r.stdout)
        self.assertEqual(self.rule(self.dry_payload(r.stdout), "pull_request")["parameters"]["required_approving_review_count"], 0)

    def test_a_team_config_with_one_collaborator_gets_the_solo_ruleset_until_someone_joins(self):
        self.config = {"version": 2, "mode": "team"}
        self.setUp()
        r = self.run_script("--dry-run", STUB_GH_COLLAB="1")
        self.assertIn("solo=1 (one collaborator)", r.stdout)                          # approvals nobody can give would block every merge

    def test_collaborators_gh_cannot_count_are_a_team(self):
        r = self.run_script("--dry-run", STUB_GH_COLLAB="err")
        self.assertIn("collaborators=unknown  solo=0 (team)", r.stdout)
        self.assertEqual(self.rule(self.dry_payload(r.stdout), "pull_request")["parameters"]["required_approving_review_count"], 1)

    def test_only_the_mode_key_makes_a_repository_solo(self):
        self.config = {"version": 2, "mode": "solo"}
        self.setUp()
        self.assertIn("solo=1 (mode solo)", self.run_script("--dry-run", STUB_GH_COLLAB="4").stdout)
        self.config = {"version": 1, "roles": {"solo": True}}                        # a 0.x file is read by init, not here
        self.setUp()
        self.assertIn("solo=0 (team)", self.run_script("--dry-run", STUB_GH_COLLAB="4").stdout)

    def test_approvals_and_checks_and_the_ruleset_name_are_taken_over(self):
        r = self.run_script("--dry-run", "--approvals", "2", "--checks", "ci, lint ,", "--ruleset-name", 'my "rules"', STUB_GH_COLLAB="4")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.dry_payload(r.stdout)
        self.assertEqual(p["name"], 'my "rules"')
        self.assertEqual(self.rule(p, "pull_request")["parameters"]["required_approving_review_count"], 2)
        rsc = self.rule(p, "required_status_checks")["parameters"]
        self.assertEqual(rsc["required_status_checks"], [{"context": "ci"}, {"context": "lint"}])
        self.assertFalse(rsc["strict_required_status_checks_policy"])
        self.assertIn("NOTE    a required check that no workflow reports blocks every merge", r.stdout)
        jq = [c for c in self.calls() if "/rulesets" in c and "--jq" in c][0]
        self.assertIn('select(.name=="my \\"rules\\"")', jq)                            # the name reaches jq as a JSON string

    def test_an_existing_ruleset_of_that_name_would_be_updated(self):
        r = self.run_script("--dry-run", STUB_GH_RULESET_ID="77")
        self.assertIn("RULESET would update #77 'sdlc: protect default branch'", r.stdout)

    def test_the_admin_bypass_is_off_unless_asked_for_and_no_bypass_always_wins(self):
        want = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}]
        for args, bypass in (([], []), (["--bypass"], want), (["--no-bypass"], []), (["--bypass", "--no-bypass"], []),
                             (["--no-bypass", "--bypass"], [])):
            r = self.run_script("--dry-run", *args)
            self.assertEqual(self.dry_payload(r.stdout)["bypass_actors"], bypass, args)

    def test_a_solo_repository_gets_no_bypass_by_default_either(self):
        self.config = {"version": 2, "mode": "solo"}
        self.setUp()
        self.assertEqual(self.dry_payload(self.run_script("--dry-run").stdout)["bypass_actors"], [])

    def test_dir_points_at_the_checkout_from_anywhere(self):
        self.config = {"version": 2, "mode": "solo"}
        self.setUp()
        r = self.run_script("--dry-run", "--dir", self.root, cwd=self.tmp, STUB_GH_COLLAB="4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("solo=1 (mode solo)", r.stdout)


class Apply(Setup):
    PATCH = "api -X PATCH repos/acme/widgets -F allow_auto_merge=true -F delete_branch_on_merge=true"
    POST = "api -X POST repos/acme/widgets/rulesets --input -"

    def test_a_team_repository_gets_its_settings_and_ruleset(self):
        r = self.run_script(STUB_GH_COLLAB="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("REPO    allow_auto_merge=true, delete_branch_on_merge=true\n", r.stdout)
        self.assertIn("RULESET 'sdlc: protect default branch' applied: PR required (approvals 1) + code-owner review + "
                      "review-thread resolution + no force-push/deletion\n", r.stdout)
        calls = self.calls()
        self.assertIn(self.PATCH, calls)
        self.assertIn(self.POST, calls)
        self.assertLess(calls.index(self.PATCH), calls.index(self.POST))               # the settings come first
        body = self.payloads()
        self.assertEqual(len(body), 1)
        self.assertEqual(self.rule(body[0], "pull_request")["parameters"]["require_code_owner_review"], True)
        self.assertNotIn("secret", " ".join(calls))

    def test_the_payload_that_is_sent_is_the_one_a_dry_run_shows(self):
        dry = self.dry_payload(self.run_script("--dry-run", "--checks", "ci", "--approvals", "2", STUB_GH_COLLAB="3").stdout)
        self.run_script("--checks", "ci", "--approvals", "2", STUB_GH_COLLAB="3")
        self.assertEqual(self.payloads(), [dry])

    def test_a_solo_repository_with_checks_and_bypass(self):
        r = self.run_script("--checks", "ci", "--bypass", STUB_GH_COLLAB="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("applied: PR required (approvals 0) + review-thread resolution + status checks 'ci' + no force-push/deletion + "
                      "admin bypass for PRs (--bypass)", r.stdout)
        self.assertNotIn("code-owner", r.stdout)
        p = self.payloads()[0]
        self.assertEqual(p["bypass_actors"][0]["actor_id"], 5)
        self.assertEqual(self.rule(p, "pull_request")["parameters"]["require_code_owner_review"], False)

    def test_an_existing_ruleset_is_updated_in_place(self):
        r = self.run_script(STUB_GH_RULESET_ID="77")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("api -X PUT repos/acme/widgets/rulesets/77 --input -", self.calls())
        self.assertNotIn(self.POST, self.calls())

    def test_a_plan_without_code_owner_review_gets_the_ruleset_without_it(self):
        r = self.run_script(STUB_GH_COLLAB="3", STUB_GH_RULESET_FAIL="codeowner")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("applied WITHOUT code-owner review (not available for this repository plan/owner type): PR required (approvals 1)",
                      r.stdout)
        owners = [self.rule(p, "pull_request")["parameters"]["require_code_owner_review"] for p in self.payloads()]
        self.assertEqual(owners, [True, False])                                       # tried with, then without

    def test_a_refused_ruleset_exits_1_and_says_why(self):
        r = self.run_script(STUB_GH_COLLAB="3", STUB_GH_RULESET_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("RULESET failed:", r.stderr)
        self.assertIn("Upgrade to GitHub Pro", r.stderr)
        self.assertIn("Rulesets on private repositories need GitHub Pro/Team/Enterprise", r.stderr)
        self.assertEqual(len(self.payloads()), 2)                                      # team: with code-owner review, then without
        self.assertIn("REPO    allow_auto_merge=true", r.stdout)                       # the settings were done before

    def test_a_refused_ruleset_for_a_solo_repository_is_tried_once(self):
        r = self.run_script(STUB_GH_COLLAB="1", STUB_GH_RULESET_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("The plugin's push guard (protect.block_direct_push) still keeps the agent off the default branch.", r.stderr)
        self.assertEqual(len(self.payloads()), 1)

    def test_settings_the_token_may_not_change_are_reported_and_the_ruleset_still_goes_through(self):
        r = self.run_script(STUB_GH_COLLAB="3", STUB_GH_PATCH_FAIL="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("could not enable allow_auto_merge / delete_branch_on_merge (permission)", r.stderr)
        self.assertIn("RULESET 'sdlc: protect default branch' applied", r.stdout)

    def test_a_github_enterprise_host_is_used_for_every_call(self):
        self.remote = "git@git.corp.example.com:acme/widgets.git"
        self.setUp()
        r = self.run_script(STUB_GH_COLLAB="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("auth status --hostname git.corp.example.com", self.calls())
        self.assertEqual(set(self.gh_db()["hosts"]), {"git.corp.example.com"})


class RulesetLookup(Setup):
    """The lookup of an existing ruleset (GET repos/…/rulesets): what gh answers decides between create, update and "unavailable".
    The stub gh cannot refuse that call, so a shim in front of it answers the lookup with a given stdout and exit status (what gh
    does when the API refuses: the error body on stdout and a non-zero exit) and hands every other call to the stub."""
    PATCH = "api -X PATCH repos/acme/widgets -F allow_auto_merge=true -F delete_branch_on_merge=true"
    FORBIDDEN = ('{"message":"Upgrade to GitHub Pro or make this repository public to enable this feature.",'
                 '"documentation_url":"https://docs.github.com/rest/repos/rules#get-all-repository-rulesets","status":"403"}')
    REASON = "Upgrade to GitHub Pro or make this repository public to enable this feature."
    HINT = "Rulesets on private repositories need GitHub Pro/Team/Enterprise (public repositories: free)."
    GUARD = "The plugin's push guard (protect.block_direct_push) still keeps the agent off the default branch."

    def lookup(self, body, rc=0):
        """The environment of a run whose ruleset lookup prints `body` and exits `rc`."""
        shim = os.path.join(self.tmp, "shim")
        os.makedirs(shim, exist_ok=True)
        real = os.path.join(STUBS, "gh")
        with open(os.path.join(shim, "gh"), "w") as fh:
            fh.write('#!/bin/bash\ncase "$*" in\n'
                     '  "api repos/"*"/rulesets --jq "*) %(real)s "$@" >/dev/null 2>&1; printf \'%%s\' "$SHIM_LOOKUP_BODY"; '
                     'exit "$SHIM_LOOKUP_RC";;\nesac\nexec %(real)s "$@"\n' % {"real": real})
        os.chmod(os.path.join(shim, "gh"), 0o755)
        return {"PATH": os.pathsep.join([shim, STUBS, os.environ.get("PATH", "")]), "SHIM_LOOKUP_BODY": body, "SHIM_LOOKUP_RC": str(rc)}

    def writes(self):
        """The calls that would create or change a ruleset."""
        return [c for c in self.calls() if "/rulesets" in c and "-X" in c.split()]

    def test_a_refused_lookup_is_unavailable_in_a_dry_run_and_never_an_id(self):
        r = self.run_script("--dry-run", STUB_GH_COLLAB="3", **self.lookup(self.FORBIDDEN, 1))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RULESET unavailable — %s\n" % self.REASON, r.stdout)
        self.assertIn(self.HINT, r.stdout)
        self.assertIn(self.GUARD, r.stdout)
        for bogus in ("would update", "would create", "#{", '"message"'):
            self.assertNotIn(bogus, r.stdout.replace(self.REASON, ""), bogus)
        self.assertEqual((self.writes(), self.inputs()), ([], []))

    def test_a_refused_lookup_stops_a_real_run_before_any_ruleset_call_and_exits_1(self):
        r = self.run_script(STUB_GH_COLLAB="3", **self.lookup(self.FORBIDDEN, 1))
        self.assertEqual(r.returncode, 1)
        self.assertIn("RULESET unavailable — %s\n" % self.REASON, r.stderr)
        self.assertIn(self.HINT, r.stderr)
        self.assertIn(self.GUARD, r.stderr)
        self.assertNotIn("RULESET failed", r.stderr)
        self.assertNotIn("RULESET", r.stdout)
        self.assertEqual((self.writes(), self.inputs()), ([], []))                    # no PUT to rulesets/{"message":…}, no POST
        self.assertFalse([c for c in self.calls() if "rulesets/" in c])
        self.assertIn(self.PATCH, self.calls())                                         # the repository settings ran before
        self.assertIn("REPO    allow_auto_merge=true", r.stdout)

    def test_a_lookup_that_fails_without_a_message_gives_a_generic_reason(self):
        for body in ("", "gh: could not connect to api.github.com", '{"status":"502"}', "[]", "not json {"):
            with self.subTest(body=body):
                r = self.run_script("--dry-run", **self.lookup(body, 1))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("RULESET unavailable — the rulesets API did not answer\n", r.stdout)
                self.assertNotIn("would update", r.stdout)
                self.assertNotIn("would create", r.stdout)
                r = self.run_script(**self.lookup(body, 1))
                self.assertEqual(r.returncode, 1)
                self.assertIn("RULESET unavailable — the rulesets API did not answer\n", r.stderr)
                self.assertEqual(self.writes(), [])

    def test_a_not_found_is_a_failure_too_and_keeps_the_message_of_the_body(self):
        body = '{"message":"Not Found","documentation_url":"https://docs.github.com/rest","status":"404"}'
        r = self.run_script(**self.lookup(body, 1))
        self.assertEqual(r.returncode, 1)
        self.assertIn("RULESET unavailable — Not Found\n", r.stderr)
        self.assertEqual(self.writes(), [])

    def test_an_empty_list_is_a_success_and_the_ruleset_is_created(self):
        r = self.run_script("--dry-run", **self.lookup("", 0))
        self.assertIn("RULESET would create 'sdlc: protect default branch'", r.stdout)
        self.assertNotIn("unavailable", r.stdout)
        r = self.run_script(STUB_GH_COLLAB="3", **self.lookup("", 0))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.writes(), ["api -X POST repos/acme/widgets/rulesets --input -"])

    def test_a_match_is_updated_by_its_id(self):
        r = self.run_script("--dry-run", **self.lookup("77\n", 0))
        self.assertIn("RULESET would update #77 'sdlc: protect default branch'", r.stdout)
        r = self.run_script(STUB_GH_COLLAB="3", **self.lookup("77\n", 0))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.writes(), ["api -X PUT repos/acme/widgets/rulesets/77 --input -"])

    def test_the_first_of_several_matches_is_the_one_updated(self):
        r = self.run_script("--dry-run", **self.lookup("77\n88\n", 0))
        self.assertIn("RULESET would update #77 ", r.stdout)

    def test_an_answer_that_is_not_an_id_is_not_used_as_one(self):
        for body in ('{"message":"surprise"}', "77abc", "abc", "#77", "-1", "7 7", "null"):
            with self.subTest(body=body):
                r = self.run_script("--dry-run", **self.lookup(body + "\n", 0))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("RULESET would create ", r.stdout)
                self.assertNotIn("update #", r.stdout)
                r = self.run_script(STUB_GH_COLLAB="3", **self.lookup(body + "\n", 0))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.writes()[-1], "api -X POST repos/acme/widgets/rulesets --input -")
                self.assertFalse([c for c in self.calls() if "-X PUT" in c])


class Repository(Setup):
    def test_the_repository_is_read_through_gh_unless_given(self):
        r = self.run_script("--dry-run")
        self.assertIn("repo=acme/widgets", r.stdout)
        self.assertIn("repo view --json nameWithOwner --jq .nameWithOwner", self.calls())
        r = self.run_script("--dry-run", "--repo", "other/thing")
        self.assertIn("repo=other/thing", r.stdout)
        self.assertIn("repos/other/thing/rulesets", " ".join(self.calls()))

    def test_without_an_origin_remote_it_says_there_is_nothing_to_set_up(self):
        self.remote = ""
        self.setUp()
        r = self.run_script("--dry-run", STUB_GH_NO_REPO="1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("this checkout has no origin remote: the loop merges on this machine and there is nothing to set up on GitHub",
                      r.stderr)
        self.assertEqual(self.inputs(), [])

    def test_an_origin_gh_does_not_know_asks_for_repo(self):
        r = self.run_script("--dry-run", STUB_GH_NO_REPO="1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot determine the repository — pass --repo owner/name (is origin a GitHub repository?)", r.stderr)

    def test_repo_works_without_any_remote(self):
        self.remote = ""
        self.setUp()
        r = self.run_script("--dry-run", "--repo", "acme/widgets", STUB_GH_NO_REPO="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("repo=acme/widgets", r.stdout)


class Login(Setup):
    def test_not_logged_in_stops_before_touching_anything(self):
        r = self.run_script(STUB_GH_LOGGED_OUT="github.com")
        self.assertEqual(r.returncode, 2)
        self.assertIn("gh is not logged in to github.com — run: gh auth login --hostname github.com", r.stderr)
        self.assertIn("--login", r.stderr)
        self.assertEqual([c for c in self.calls() if not c.startswith("auth status")], [])

    def test_a_dry_run_while_logged_out_needs_repo(self):
        r = self.run_script("--dry-run", STUB_GH_LOGGED_OUT="github.com")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pass --repo owner/name", r.stderr)
        r = self.run_script("--dry-run", "--repo", "acme/widgets", STUB_GH_LOGGED_OUT="github.com")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gh is not authenticated — dry run shows the payload only", r.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("auth login")])      # a dry run never logs in

    def test_login_runs_the_device_flow_and_then_carries_on(self):
        r = self.run_script("--login", STUB_GH_LOGGED_OUT="github.com", STUB_GH_COLLAB="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LOGIN   GitHub device flow started. Open https://github.com/login/device and enter the code: ABCD-1234", r.stdout)
        self.assertIn("LOGIN   SDLC_DEVICE_CODE=ABCD-1234  SDLC_DEVICE_URL=https://github.com/login/device", r.stdout)
        self.assertIn("LOGIN   gh authenticated (github.com)", r.stdout)
        self.assertIn("RULESET 'sdlc: protect default branch' applied", r.stdout)
        self.assertIn("auth login --web -h github.com --git-protocol https --skip-ssh-key", self.calls())

    def test_a_login_that_is_not_completed_exits_2_with_the_end_of_gh_s_output(self):
        r = self.run_script("--login", STUB_GH_LOGGED_OUT="github.com", STUB_GH_LOGIN_FAIL="1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("LOGIN   not completed — rerun with --login when ready", r.stderr)
        self.assertIn("the code expired", r.stderr)
        self.assertEqual(self.inputs(), [])

    def test_login_is_not_started_when_gh_is_already_logged_in(self):
        r = self.run_script("--login", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("LOGIN", r.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("auth login")])


class NoCredentials(Setup):
    """The script never reads a token, stores one or hands one to anything — whatever the environment holds."""

    def test_no_run_prints_or_passes_on_a_token_from_the_environment(self):
        for args, env in (([], {"STUB_GH_COLLAB": "3"}), (["--dry-run"], {"STUB_GH_COLLAB": "3"}), (["--bypass", "--checks", "ci"], {}),
                          (["--login"], {"STUB_GH_LOGGED_OUT": "github.com"}), (["--auth", "api"], {}), (["--token-stdin"], {}),
                          ([], {"STUB_GH_RULESET_FAIL": "1"}), ([], {"STUB_GH_LOGGED_OUT": "github.com"})):
            r = self.run_script(*args, **dict(env, **FAKE_TOKENS))
            self.assertNoCredentials(r.stdout, r.stderr)
        self.assertFalse([c for c in self.calls() if "secret" in c])

    def test_no_files_are_written_outside_the_repository_and_no_keychain_is_asked(self):
        before = set(os.listdir(os.path.join(self.tmp, "home")))
        self.run_script(STUB_GH_COLLAB="3", **FAKE_TOKENS)
        self.assertEqual(set(os.listdir(os.path.join(self.tmp, "home"))), before)         # no ~/.config/sdlc/ci-token
        self.assertEqual(os.listdir(self.root), [".git"])                                   # nor anything in the checkout

    def test_standard_input_is_not_read(self):
        r = subprocess.run(["bash", SCRIPT, "--dry-run"], input="token-example-from-stdin\n", capture_output=True, text=True,
                           cwd=self.root, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("token-example-from-stdin", r.stdout + r.stderr + json.dumps(self.gh_db()))


if __name__ == "__main__":
    unittest.main()
