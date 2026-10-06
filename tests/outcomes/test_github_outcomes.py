"""Offline live-adapter tests over authored, filtered REST recordings."""

import contextlib
import copy
import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
from support import IsolatedTemporaryDirectory, isolate_github_destinations
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from sumbi.cli import main
from sumbi.outcomes.github.deliver import deliverable_basis, judge
from sumbi.outcomes.github.live import GitHubOutcomes, NoRedirect, PLAN_UNAVAILABLE, github_token
from sumbi.outcomes.github.ledger import COLUMNS, Deliverable, read_ledger
from sumbi.outcomes.github.recorded import FixtureOutcomes
from outcomes.test_deliver import START, WINDOW, REPO, time, pull

END = datetime.fromisoformat(time(32).replace("Z", "+00:00"))
FIXTURE = Path(__file__).parents[1] / "fixtures/github/responses.json"


class GitHubTests(unittest.TestCase):
    def setUp(self):
        directory = IsolatedTemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        isolate_github_destinations(self)
        self.routes = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.calls = []
        self.env = patch.dict(os.environ, {"GITHUB_TOKEN": "synthetic-token-canary", "GH_TOKEN": "synthetic-secondary"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for target in ("socket.socket", "socket.getaddrinfo", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("Tests cannot use the network"))
            guard.start()
            self.addCleanup(guard.stop)
        opener = patch("urllib.request.OpenerDirector.open", side_effect=self.respond)
        self.open = opener.start()
        self.addCleanup(opener.stop)
        clock = patch("sumbi.outcomes.github.live.datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.return_value = END
        self.addCleanup(clock.stop)
        epoch = patch("sumbi.outcomes.github.live.time.time", return_value=END.timestamp())
        epoch.start()
        self.addCleanup(epoch.stop)
        salt = patch("sumbi.outcomes.github.deliver.read_salt", return_value=None)
        salt.start()
        self.addCleanup(salt.stop)

    def respond(self, request, **kwargs):
        self.calls.append(request)
        self.assertEqual(request.method, "GET")
        self.assertEqual(urllib.parse.urlsplit(request.full_url).netloc, "api.github.com")
        self.assertNotIn("synthetic-token", request.full_url)
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertIn(parsed.path, self.routes)
        return io.BytesIO(json.dumps(self.routes[parsed.path]).encode())

    def ledger(self, numbers=(1,), roles=()):
        return [Deliverable("D1", START, (REPO,), tuple(REPO + "#" + str(n) for n in numbers), (), pr_roles=roles)]

    def adapter(self, numbers=(1,), **kwargs):
        return GitHubOutcomes(self.ledger(numbers), cache=self.root / "cache", **kwargs)

    def test_recorded_pages_supply_merge_and_head(self):
        adapter = self.adapter()
        pr = adapter.pull(REPO + "#1")
        self.assertEqual(pr.head_sha, "1".zfill(40))
        self.assertEqual(pr.merged_at.isoformat(), time(2).replace("Z", "+00:00"))
        self.assertEqual(pr.state, "closed")
        self.assertTrue(adapter.observation(REPO).pulls_complete)

    def test_token_precedence_environment(self):
        with patch("shutil.which", side_effect=AssertionError("No CLI lookup needed")):
            self.assertEqual(github_token(), "synthetic-token-canary")
            os.environ.pop("GITHUB_TOKEN")
            self.assertEqual(github_token(), "synthetic-secondary")

    def test_token_falls_back_to_gh_without_printing(self):
        os.environ.pop("GITHUB_TOKEN")
        os.environ.pop("GH_TOKEN")
        result = subprocess.CompletedProcess([], 0, "synthetic-cli-credential\n", "")
        with patch("shutil.which", return_value="gh"), patch("subprocess.run", return_value=result) as run:
            self.assertEqual(github_token(), "synthetic-cli-credential")
            self.assertEqual(run.call_args.args[0], ["gh", "auth", "token"])
            self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_no_token_error_is_clear(self):
        os.environ.pop("GITHUB_TOKEN")
        os.environ.pop("GH_TOKEN")
        with patch("shutil.which", return_value=None), self.assertRaisesRegex(ValueError, "authentication required.*GITHUB_TOKEN.*GH_TOKEN"):
            self.adapter()
        self.open.assert_not_called()

    def test_gh_failure_does_not_echo_output(self):
        os.environ.pop("GITHUB_TOKEN")
        os.environ.pop("GH_TOKEN")
        with patch("shutil.which", return_value="gh"), patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "private", "private")):
            with self.assertRaises(ValueError) as result:
                github_token()
        self.assertNotIn("private", str(result.exception))

    def test_blank_tokens_and_gh_timeout(self):
        os.environ.update(GITHUB_TOKEN=" ", GH_TOKEN="")
        with patch("shutil.which", return_value="gh"), patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
            with self.assertRaisesRegex(ValueError, "authentication required"):
                github_token()

    def test_credentials_only_in_header(self):
        self.adapter()
        self.assertTrue(self.calls)
        for request in self.calls:
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token-canary")
            self.assertNotIn("synthetic-token-canary", request.full_url)

    def test_rate_limit_backoff_from_reset(self):
        error = urllib.error.HTTPError("https://api.github.com", 403, "private", {
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(END.timestamp() + 2)}, io.BytesIO(b"private"))
        count = 0

        def limited(request, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise error
            return self.respond(request, **kwargs)

        self.open.side_effect = limited
        with patch("sumbi.outcomes.github.live.time.sleep") as sleep:
            self.adapter()
        sleep.assert_called_once_with(3)

    def test_rate_limit_429_retry_after(self):
        error = urllib.error.HTTPError("https://api.github.com", 429, "private", {"Retry-After": "2"}, io.BytesIO())
        self.open.side_effect = [error, *[io.BytesIO(json.dumps(self.routes[p]).encode()) for p in (
            "/repos/example/sample/pulls", "/repos/example/sample/commits", "/repos/example/sample/commits")]]
        with patch("sumbi.outcomes.github.live.time.sleep") as sleep:
            self.adapter()
        sleep.assert_called_once_with(3)

    def test_rate_limit_retries_are_bounded(self):
        self.open.side_effect = lambda *a, **k: (_ for _ in ()).throw(urllib.error.HTTPError(
            "https://api.github.com", 429, "private", {"Retry-After": "0"}, io.BytesIO()))
        with patch("sumbi.outcomes.github.live.time.sleep"), self.assertRaisesRegex(ValueError, "HTTP 429"):
            self.adapter()
        self.assertEqual(self.open.call_count, 4)

    def test_permission_403_does_not_wait_for_reset(self):
        self.open.side_effect = urllib.error.HTTPError("https://api.github.com", 403, "private", {
            "X-RateLimit-Remaining": "99", "X-RateLimit-Reset": str(END.timestamp() + 100)}, io.BytesIO(b"access denied"))
        with patch("sumbi.outcomes.github.live.time.sleep") as sleep, self.assertRaisesRegex(ValueError, "HTTP 403"):
            self.adapter()
        sleep.assert_not_called()

    def test_exhausted_policy_rate_limit_403_is_never_cached(self):
        adapter = self.adapter()
        before = {p.name for p in (self.root / "cache").glob("*.json")}
        self.open.reset_mock()
        self.open.side_effect = lambda *a, **k: (_ for _ in ()).throw(urllib.error.HTTPError(
            "https://api.github.com", 403, "private", {"X-RateLimit-Remaining": "0"},
            io.BytesIO(b'{"message":"API rate limit exceeded"}')))
        with patch("sumbi.outcomes.github.live.time.sleep"), self.assertRaisesRegex(ValueError, "HTTP 403"):
            adapter._get("/repos/example/sample/rules/branches/main", policy=True)
        self.assertEqual(self.open.call_count, 4)
        self.assertEqual({p.name for p in (self.root / "cache").glob("*.json")}, before)

    def test_cache_hit_is_offline_and_keeps_observation_time(self):
        first = self.adapter()
        first.pull(REPO + "#1")
        self.open.side_effect = AssertionError("Cache hits cannot request the network")
        self.clock.now.return_value = END.replace(minute=1)
        second = self.adapter()
        self.assertEqual(second.pull(REPO + "#1"), first.pull(REPO + "#1"))
        self.assertEqual(second.observation(REPO), first.observation(REPO))
        self.assertGreater(second.cache_hits, 0)
        self.assertEqual(second.requests, 0)

    def test_expired_cache_is_refetched(self):
        self.adapter()
        self.open.reset_mock()
        with patch("sumbi.outcomes.github.live.time.time", return_value=END.timestamp() + 301):
            self.adapter()
        self.assertGreater(self.open.call_count, 0)

    def test_corrupt_cache_is_refetched(self):
        self.adapter()
        for p in (self.root / "cache").glob("*.json"):
            p.write_text("{}", encoding="utf-8")
        self.open.reset_mock()
        self.adapter()
        self.assertGreater(self.open.call_count, 0)

    def test_no_required_policy_uses_all_visible(self):
        pr = self.adapter().pull(REPO + "#1")
        self.assertEqual(pr.checks, "green")
        self.assertEqual(pr.checks_basis, "all_visible")
        self.assertEqual(pr.observed_checks, "green")
        self.assertTrue(any("rules/branches" in r.full_url for r in self.calls))

    def test_check_runs_and_statuses_green_red_and_missing(self):
        for conclusion, state, expected in (("success", "success", "green"), ("failure", "success", "red"), ("success", "failure", "red")):
            with self.subTest(conclusion=conclusion, state=state):
                with IsolatedTemporaryDirectory() as cache:
                    path = "/repos/example/sample/commits/" + f"{101:040x}"
                    self.routes[path + "/check-runs"]["check_runs"][0]["conclusion"] = conclusion
                    self.routes[path + "/statuses"] = [{"context": "test", "updated_at": time(2), "state": state}]
                    adapter = GitHubOutcomes(self.ledger(), cache=Path(cache))
                    self.assertEqual(adapter.pull(REPO + "#1").observed_checks, expected)
        for path in self.routes:
            if path.endswith("/check-runs"):
                self.routes[path] = {"total_count": 0, "check_runs": []}
            if path.endswith("/statuses"):
                self.routes[path] = []
        self.assertEqual(self.adapter().pull(REPO + "#1").observed_checks, "unknown")

    def test_required_historical_fixtures_green_red_missing(self):
        for value, expected in (("success", "green"), ("failure", "red"), (None, "unknown")):
            entry = pull(1, checks=value)
            if value is None:
                entry["checks_at_merge"]["check_runs"] = []
            self.assertEqual(FixtureOutcomes._checks(entry["checks_at_merge"], entry["response"]["head"]["sha"],
                             datetime.fromisoformat(time(2).replace("Z", "+00:00"))), expected)

    def test_completion_after_merge_cannot_supply_green(self):
        path = "/repos/example/sample/commits/" + f"{101:040x}" + "/check-runs"
        self.routes[path]["check_runs"][0]["completed_at"] = time(3)
        self.assertEqual(self.adapter().pull(REPO + "#1").observed_checks, "red")

    def test_checks_started_after_merge_are_ignored(self):
        path = "/repos/example/sample/commits/" + f"{101:040x}" + "/check-runs"
        self.routes[path]["check_runs"][0].update(started_at=time(3), completed_at=time(3))
        self.assertEqual(self.adapter().pull(REPO + "#1").observed_checks, "green")
        self.assertTrue(any(f"{1:040x}/check-runs" in r.full_url for r in self.calls))

    def test_latest_attempt_at_merge_wins_over_older_green(self):
        path = "/repos/example/sample/commits/" + f"{101:040x}" + "/check-runs"
        newer = copy.deepcopy(self.routes[path]["check_runs"][0])
        newer.update(started_at=time(1, 60), conclusion="failure")
        self.routes[path]["check_runs"].append(newer)
        self.routes[path]["total_count"] = 2
        self.assertEqual(self.adapter().pull(REPO + "#1").observed_checks, "red")

    def test_followup_and_revert_reference_patterns(self):
        adapter = self.adapter()
        follows, reverts = adapter.disturbances(adapter.pull(REPO + "#1"), 7)
        self.assertEqual([p.id for p in follows], [REPO + "#2"])
        self.assertEqual(reverts, [datetime.fromisoformat(time(3).replace("Z", "+00:00"))])

    def test_revert_number_reference(self):
        self.routes["/repos/example/sample/commits"][0]["commit"]["message"] = "Revert PR 1"
        adapter = self.adapter()
        self.assertEqual(len(adapter.disturbances(adapter.pull(REPO + "#1"), 7)[1]), 1)

    def test_fix_commit_reference_is_detected_without_being_a_revert(self):
        self.routes["/repos/example/sample/commits"][0]["commit"]["message"] = "Fix regression #1"
        adapter = self.adapter()
        self.assertEqual(adapter.commit_fixes(adapter.pull(REPO + "#1"), 7),
                         [datetime.fromisoformat(time(3).replace("Z", "+00:00"))])
        self.assertEqual(adapter.disturbances(adapter.pull(REPO + "#1"), 7)[1], [])

    def test_wrong_repo_number_or_window_does_not_match(self):
        for text in ("Fix #10", "Fix example/other#1", "Fix https://github.com/example/other/pull/1", "Fix #1abc", "Fix #1_suffix"):
            self.routes["/repos/example/sample/pulls"][1]["title"] = text
            with IsolatedTemporaryDirectory() as cache:
                adapter = GitHubOutcomes(self.ledger(), cache=Path(cache))
                self.assertEqual(adapter.disturbances(adapter.pull(REPO + "#1"), 7)[0], [])
        self.routes["/repos/example/sample/pulls"][1].update(title="Fix #1", created_at=time(9), merged_at=time(10), closed_at=time(10))
        adapter = self.adapter()
        self.assertEqual(adapter.disturbances(adapter.pull(REPO + "#1"), 7)[0], [])

    def test_closed_unmerged(self):
        pr = self.adapter((3,)).pull(REPO + "#3")
        self.assertEqual(pr.state, "closed")
        self.assertIsNone(pr.merged_at)
        self.assertEqual(judge(self.ledger((3,))[0], self.adapter((3,)))["state"], "failed")

    def test_record_replays_without_network_and_omits_personal_fields(self):
        response = self.routes["/repos/example/sample/pulls"][0]
        response.update(user={"login": "synthetic-person"}, email="synthetic@example.test", url="private-url", title="Synthetic change synthetic-token-canary github_pat_fakecredential")
        adapter = self.adapter(record=self.root / "record")
        adapter.pull(REPO + "#1")
        expected_reverts = adapter.disturbances(adapter.pull(REPO + "#1"), 7)[1]
        text = "".join(p.read_text(encoding="utf-8") for p in self.root.rglob("*.json"))
        for private in ("synthetic-token-canary", "synthetic-person", "synthetic@example.test", "private-url", "github_pat_fakecredential"):
            self.assertNotIn(private, text)
        self.open.side_effect = AssertionError("Replay cannot use the network")
        replay = FixtureOutcomes(self.root / "record")
        self.assertEqual(replay.pull(REPO + "#1"), adapter.pull(REPO + "#1"))
        self.assertEqual(replay.disturbances(replay.pull(REPO + "#1"), 7)[1], expected_reverts)

    def test_no_token_or_prose_in_cli_output(self):
        ledger = self.root / "ledger.csv"
        with ledger.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerow(["D1", time(1), "private-acceptance", REPO, REPO + "#1", "", "", "", "private-notes"])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["deliver", "--ledger", str(ledger), "--outcomes", "github", "--cache", str(self.root / "cache"),
                           "--home", str(self.root / "home"), "--since", time(1), "--until", time(10), "--json", "-"])
        self.assertEqual(result, 0, stderr.getvalue())
        report = json.loads(stdout.getvalue())
        row = report["deliverables"][0]
        self.assertEqual(row["pr_outcomes"][0]["checks_at_merge"], "green")
        self.assertEqual(row["pr_outcomes"][0]["checks_basis"], "all_visible")
        self.assertEqual(row["checks_basis"], "all_visible")
        self.assertEqual(report["checks_basis_counts"], {"historical": 0, "current_policy": 0, "all_visible": 1, "unknown": 0})
        self.assertIn("all_visible 1", stderr.getvalue())
        for value in ("synthetic-token-canary", "Synthetic", "Fix #1", "private-acceptance", "private-notes", REPO, str(self.root)):
            self.assertNotIn(value, stdout.getvalue() + stderr.getvalue())

    def test_errors_do_not_include_response_text_or_credentials(self):
        self.open.side_effect = urllib.error.URLError("private synthetic-token-canary")
        with self.assertRaises(ValueError) as result:
            self.adapter()
        self.assertNotIn("private", str(result.exception))
        self.assertNotIn("synthetic-token-canary", str(result.exception))

    def test_oversize_text_is_rejected_instead_of_truncated(self):
        self.routes["/repos/example/sample/pulls"][0]["body"] = "x" * 65537
        with self.assertRaisesRegex(ValueError, "text exceeds the scan limit"):
            self.adapter()

    def test_malformed_response_has_a_sanitized_error(self):
        del self.routes["/repos/example/sample/pulls"][0]["head"]
        with self.assertRaisesRegex(ValueError, "unavailable or malformed"):
            self.adapter()

    def test_missing_listed_pr_remains_missing(self):
        self.open.side_effect = lambda request, **kw: (_ for _ in ()).throw(urllib.error.HTTPError(
            request.full_url, 404, "private", {}, io.BytesIO())) if request.full_url.endswith("/pulls/4") else self.respond(request, **kw)
        adapter = self.adapter((4,))
        self.assertIsNone(adapter.pull(REPO + "#4"))
        self.assertEqual(judge(self.ledger((4,))[0], adapter)["reason"], "missing_pr")

    def test_credential_control_characters_are_rejected_before_network(self):
        os.environ["GITHUB_TOKEN"] = "synthetic\ninjection"
        with self.assertRaisesRegex(ValueError, "credential has an invalid format"):
            self.adapter()
        self.open.assert_not_called()

    def test_cache_and_record_cannot_be_in_any_repository(self):
        repo = self.root / "repository"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: synthetic", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside Git repositories"):
            GitHubOutcomes(self.ledger(), cache=repo / "cache")
        with self.assertRaisesRegex(ValueError, "outside Git repositories"):
            GitHubOutcomes(self.ledger(), cache=self.root / "cache", record=repo / "record")
        self.open.assert_not_called()

    def test_cache_record_overlap_rejected(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            self.adapter(record=self.root / "cache/record")

    def test_redirect_never_forwards_credentials(self):
        with self.assertRaisesRegex(ValueError, "credentials were not forwarded"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.test")

    def test_pagination_collects_second_page(self):
        original = self.respond
        page = self.routes["/repos/example/sample/pulls"][0]

        def response(request, **kwargs):
            parsed = urllib.parse.urlsplit(request.full_url)
            if parsed.path.endswith("/pulls"):
                if urllib.parse.parse_qs(parsed.query)["page"] == ["1"]:
                    # Unique synthetic IDs preserve fixture validation.
                    rows = [{**page, "number": n} for n in range(100, 200)]
                    return io.BytesIO(json.dumps(rows).encode())
                self.calls.append(request)
            return original(request, **kwargs)

        self.open.side_effect = response
        adapter = self.adapter()
        self.assertIsNotNone(adapter.pull(REPO + "#3"))
        self.assertEqual(len(adapter.pulls), 103)

    def test_incomplete_check_capture_is_rejected(self):
        path = "/repos/example/sample/commits/" + f"{101:040x}" + "/check-runs"
        self.routes[path]["total_count"] = 101
        with self.assertRaisesRegex(ValueError, "capture is incomplete"):
            self.adapter().pull(REPO + "#1")

    def policy_case(self, contexts=(), checks=(), rules=(), enabled=True, enforcement="non_admins"):
        self.routes["/repos/example/sample/branches/main"] = {"protected": enabled, "protection": {
            "enabled": enabled, "required_status_checks": {"enforcement_level": enforcement,
            "contexts": list(contexts), "checks": list(checks)}}}
        self.routes["/repos/example/sample/rules/branches/main"] = list(rules)
        self.routes["/repos/example/sample/commits"] = []
        self.routes["/repos/example/sample/pulls"] = self.routes["/repos/example/sample/pulls"][:1]

    def policy_pull(self):
        cache = self.root / ("case-" + str(len(list(self.root.iterdir()))))
        adapter = GitHubOutcomes(self.ledger(), cache=cache)
        return adapter, adapter.pull(REPO + "#1")

    def run_at_merge(self, **changes):
        route = "/repos/example/sample/commits/" + f"{101:040x}" + "/check-runs"
        run = self.routes[route]["check_runs"][0]
        run.update(changes)
        return run

    def rule(self, context, integration_id=None):
        return {"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": context, "integration_id": integration_id}]}}

    def test_classic_optional_red_is_ignored(self):
        self.policy_case(contexts=["test"])
        route = "/repos/example/sample/commits/" + f"{101:040x}" + "/check-runs"
        self.routes[route]["check_runs"].append({**self.run_at_merge(), "name": "optional", "conclusion": "failure"})
        self.routes[route]["total_count"] = 2
        adapter, pr = self.policy_pull()
        self.assertEqual((pr.checks, pr.checks_basis, pr.observed_checks), ("green", "current_policy", "red"))
        self.assertEqual(judge(self.ledger()[0], adapter)["state"], "success")

    def test_classic_required_red_pending_missing(self):
        for changes, expected, reason in (({"conclusion": "failure"}, "red", "checks_red"),
                ({"completed_at": time(3)}, "red", "checks_red"),
                ({"status": "in_progress", "completed_at": None, "conclusion": None}, "red", "checks_red"),
                ({"name": "optional"}, "unknown", "checks_missing_required")):
            with self.subTest(changes=changes):
                self.policy_case(contexts=["test"])
                self.run_at_merge(name="test", status="completed", completed_at=time(2), conclusion="success")
                self.run_at_merge(**changes)
                adapter, pr = self.policy_pull()
                self.assertEqual((pr.checks, pr.checks_basis, pr.checks_reason), (expected, "current_policy", reason))
                self.assertEqual(judge(self.ledger()[0], adapter)["reason"], reason)

    def test_ruleset_requirements_and_classic_union(self):
        self.policy_case(rules=[self.rule("test")])
        self.assertEqual(self.policy_pull()[1].checks, "green")
        self.policy_case(contexts=["classic"], rules=[self.rule("test")])
        self.assertEqual(self.policy_pull()[1].checks_reason, "checks_missing_required")
        self.run_at_merge(conclusion="failure")
        self.assertEqual(self.policy_pull()[1].checks, "red")

    def test_classic_check_objects_without_contexts(self):
        self.policy_case(checks=[{"context": "test", "app_id": None}])
        self.assertEqual(self.policy_pull()[1].checks_basis, "current_policy")
        self.assertEqual(self.policy_pull()[1].checks, "green")

    def test_app_pinned_requirements(self):
        for source in ("classic", "ruleset"):
            with self.subTest(source=source):
                kwargs = {"contexts": ["test"], "checks": [{"context": "test", "app_id": 7}]} if source == "classic" else {"rules": [self.rule("test", 7)]}
                self.policy_case(**kwargs)
                self.run_at_merge(app={"id": 8})
                self.assertEqual(self.policy_pull()[1].checks_reason, "checks_missing_required")
                self.routes["/repos/example/sample/commits/" + f"{101:040x}" + "/statuses"] = [
                    {"context": "test", "updated_at": time(2), "state": "success"}]
                self.assertEqual(self.policy_pull()[1].checks, "unknown")
                self.run_at_merge(app={"id": 7})
                self.assertEqual(self.policy_pull()[1].checks, "green")
                self.run_at_merge(conclusion="failure")
                self.assertEqual(self.policy_pull()[1].checks, "red")
                self.run_at_merge(conclusion="success")

    def test_pinned_green_ignores_other_app_and_status_red(self):
        self.policy_case(contexts=["test"], checks=[{"context": "test", "app_id": 7}])
        route = "/repos/example/sample/commits/" + f"{101:040x}"
        self.run_at_merge(app={"id": 7})
        self.routes[route + "/check-runs"]["check_runs"].append({**self.run_at_merge(), "app": {"id": 8}, "conclusion": "failure"})
        self.routes[route + "/check-runs"]["total_count"] = 2
        self.routes[route + "/statuses"] = [{"context": "test", "state": "failure", "updated_at": time(2)}]
        pr = self.policy_pull()[1]
        self.assertEqual((pr.checks, pr.observed_checks), ("green", "red"))

    def test_unpinned_same_name_sources_must_all_pass(self):
        self.policy_case(contexts=["test"])
        self.run_at_merge(app={"id": 7})
        self.routes["/repos/example/sample/commits/" + f"{101:040x}" + "/statuses"] = [
            {"context": "test", "state": "pending", "updated_at": time(2)}]
        self.assertEqual(self.policy_pull()[1].checks, "red")

    def test_any_app_sentinel_and_exact_name_matching(self):
        self.policy_case(checks=[{"context": "test", "app_id": -1}])
        self.assertEqual(self.policy_pull()[1].checks, "green")
        for name in ("Test", "test ", "workflow / test"):
            self.run_at_merge(name=name)
            self.assertEqual(self.policy_pull()[1].checks, "unknown")

    def test_enforcement_off_all_visible_green_red_none(self):
        self.policy_case(contexts=["missing"], enforcement="off")
        self.assertEqual((self.policy_pull()[1].checks, self.policy_pull()[1].checks_basis), ("green", "all_visible"))
        self.run_at_merge(conclusion="failure")
        self.assertEqual(self.policy_pull()[1].checks, "red")
        for path in self.routes:
            if path.endswith("/check-runs"):
                self.routes[path] = {"total_count": 0, "check_runs": []}
            if path.endswith("/statuses"):
                self.routes[path] = []
        adapter, pr = self.policy_pull()
        self.assertEqual((pr.checks, pr.checks_basis, pr.checks_reason), ("unknown", "all_visible", "no_checks"))
        self.assertEqual(judge(self.ledger()[0], adapter)["reason"], "checks_none")

    def test_disabled_classic_and_active_rules(self):
        self.policy_case(contexts=["missing"], enabled=False, rules=[self.rule("test")])
        self.assertEqual((self.policy_pull()[1].checks, self.policy_pull()[1].checks_basis), ("green", "current_policy"))

    def test_documented_branch_shape_without_enabled(self):
        self.policy_case(contexts=["test"])
        del self.routes["/repos/example/sample/branches/main"]["protection"]["enabled"]
        self.assertEqual(self.policy_pull()[1].checks_basis, "current_policy")

    def test_policy_http_errors_are_narrow_and_cached(self):
        cases = (("rules", 403, PLAN_UNAVAILABLE, "all_visible"),
                 ("rules", 403, PLAN_UNAVAILABLE + " extra", "unknown"),
                 ("rules", 403, "Resource not accessible by integration", "unknown"),
                 ("rules", 404, PLAN_UNAVAILABLE, "unknown"),
                 ("branches", 403, PLAN_UNAVAILABLE, "unknown"),
                 ("branches", 404, "Not Found", "unknown"))
        for endpoint, code, message, basis in cases:
            with self.subTest(endpoint=endpoint, code=code, message=message):
                self.policy_case(enabled=False, enforcement="off")
                match = "/rules/branches/" if endpoint == "rules" else "/branches/"

                def response(request, **kwargs):
                    path = urllib.parse.urlsplit(request.full_url).path
                    if match in path and (endpoint == "rules" or "/rules/" not in path):
                        raise urllib.error.HTTPError(request.full_url, code, "private", {},
                                                     io.BytesIO(json.dumps({"message": message}).encode()))
                    return self.respond(request, **kwargs)

                self.open.side_effect = response
                adapter, pr = self.policy_pull()
                self.assertEqual(pr.checks_basis, basis)
                self.assertEqual(pr.checks, "green" if basis == "all_visible" else "unknown")
                if basis == "unknown":
                    self.assertEqual(judge(self.ledger()[0], adapter)["reason"], "checks_policy_unreadable")
                self.open.side_effect = AssertionError("Policy error cache must be offline")
                replay = GitHubOutcomes(self.ledger(), cache=adapter.cache)
                self.assertEqual(replay.pull(REPO + "#1"), pr)
                self.assertEqual(replay.requests, 0)

    def test_policy_endpoint_cache_and_record_allowlist(self):
        self.policy_case(contexts=["test"], rules=[self.rule("test")])
        self.routes["/repos/example/sample/branches/main"]["name"] = "private-branch"
        self.routes["/repos/example/sample/rules/branches/main"][0]["ruleset_source"] = "private-source"
        self.run_at_merge(app={"id": 7, "name": "private-app"})
        first = self.adapter(record=self.root / "record")
        pr = first.pull(REPO + "#1")
        text = "".join(p.read_text(encoding="utf-8") for p in self.root.rglob("*.json"))
        for private in ("private-branch", "private-source", "private-app"):
            self.assertNotIn(private, text)
        self.assertTrue(any("/branches/main" in r.full_url for r in self.calls))
        self.assertTrue(any("/rules/branches/main" in r.full_url for r in self.calls))
        self.open.side_effect = AssertionError("Replay must be offline")
        self.assertEqual(FixtureOutcomes(self.root / "record").pull(REPO + "#1"), pr)
        second = self.adapter()
        self.assertEqual(second.pull(REPO + "#1"), pr)
        self.assertEqual(second.requests, 0)

    def test_historical_snapshot_wins_over_current_policy(self):
        self.policy_case(contexts=["missing"])
        adapter = self.adapter()
        entry = adapter._entries[REPO + "#1"]
        entry["checks_at_merge"] = pull(1)["checks_at_merge"]
        # A recorded historical snapshot must also override contradictory policy evidence.
        entry["current_policy_evidence"] = {"required": [{"context": "missing", "app_id": None}], "results": None}
        from dataclasses import replace
        adapter.pulls[REPO + "#1"] = replace(adapter.pulls[REPO + "#1"], checks="green", checks_basis="historical", checks_reason="")
        pr = adapter.pull(REPO + "#1")
        self.assertEqual((pr.checks, pr.checks_basis), ("green", "historical"))
        self.assertFalse(any("/branches/" in r.full_url for r in self.calls))
        adapter._recordings[REPO]["pulls"] = [entry]
        recorded = self.root / "historical"
        recorded.mkdir()
        (recorded / "fixture.json").write_text(json.dumps(adapter._recordings[REPO]), encoding="utf-8")
        self.assertEqual(FixtureOutcomes(recorded).pull(REPO + "#1"), pr)

    def test_deliverable_weakest_basis_includes_repairs(self):
        self.policy_case(contexts=["test"])
        adapter, pr = self.policy_pull()
        from dataclasses import replace
        fixture = FixtureOutcomes.__new__(FixtureOutcomes)
        fixture.pulls = dict(adapter.pulls)
        for basis, expected in (("historical", "current_policy"), ("all_visible", "all_visible"), ("unknown", "unknown")):
            fixture.pulls[REPO + "#2"] = replace(pr, id=REPO + "#2", checks_basis=basis)
            self.assertEqual(deliverable_basis([REPO + "#1", REPO + "#2"], fixture), expected)
        fixture.pulls[REPO + "#2"] = replace(pr, id=REPO + "#2", merged_at=None, checks_basis="unknown")
        self.assertEqual(deliverable_basis([REPO + "#1", REPO + "#2"], fixture), "current_policy")
        self.assertEqual(deliverable_basis([], fixture), "unknown")
        self.assertEqual(deliverable_basis([REPO + "#1", REPO + "#3"], fixture), "unknown")

    def test_rules_pagination_preserves_other_rule_counts(self):
        self.policy_case()

        def response(request, **kwargs):
            parsed = urllib.parse.urlsplit(request.full_url)
            if "/rules/branches/" in parsed.path:
                rows = [{"type": "deletion", "ruleset_source": "private"}] * 100 if urllib.parse.parse_qs(parsed.query)["page"] == ["1"] else [self.rule("test")]
                return io.BytesIO(json.dumps(rows).encode())
            return self.respond(request, **kwargs)

        self.open.side_effect = response
        self.assertEqual(self.policy_pull()[1].checks_basis, "current_policy")

    def test_malformed_policy_cannot_become_all_visible(self):
        for changes in ({"enforcement_level": "unsupported"}, {"enforcement_level": None}, {"contexts": None}):
            self.policy_case(contexts=["test"])
            checks = self.routes["/repos/example/sample/branches/main"]["protection"]["required_status_checks"]
            checks.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.policy_pull()

    def test_ruleset_negative_app_id_cannot_remove_pin(self):
        self.policy_case(rules=[self.rule("test", -1)])
        with self.assertRaisesRegex(ValueError, "required check was malformed"):
            self.policy_pull()

    def test_live_policy_names_and_ids_stay_out_of_output(self):
        self.policy_case(contexts=["private-check-canary"])
        self.run_at_merge(name="private-check-canary", app={"id": 700007})
        ledger = self.root / "ledger.csv"
        with ledger.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerow(["D1", time(1), "private-acceptance", REPO, REPO + "#1", "", "", "", ""])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["deliver", "--ledger", str(ledger), "--outcomes", "github", "--cache", str(self.root / "cache"),
                           "--home", str(self.root / "home"), "--since", time(1), "--until", time(10), "--json", "-"])
        self.assertEqual(result, 0)
        output = stdout.getvalue() + stderr.getvalue()
        for secret in ("private-check-canary", "700007", "synthetic-token-canary", REPO, str(self.root)):
            self.assertNotIn(secret, output)
        self.assertEqual(json.loads(stdout.getvalue())["deliverables"][0]["checks_basis"], "current_policy")


class RoleTests(unittest.TestCase):
    def setUp(self):
        root = IsolatedTemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)

    def row(self, entries):
        ledger = self.root / "ledger.csv"
        with ledger.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerow(["D1", time(1), "Synthetic acceptance", REPO, entries, "", "", "", ""])
        return read_ledger(ledger)[0]

    def outcomes(self, pulls, commits=()):
        directory = self.root / "outcomes"
        directory.mkdir(exist_ok=True)
        raw = {"repository": REPO, "coverage_start": time(1), "observed_at": time(32),
               "pulls_complete": True, "commits_complete": True, "pulls": pulls, "commits": list(commits)}
        (directory / "repository.json").write_text(json.dumps(raw), encoding="utf-8")
        return FixtureOutcomes(directory)

    def test_default_and_explicit_constituent(self):
        d = self.row(REPO + "#1;" + REPO + "#2:constituent")
        self.assertEqual(d.pr_roles, ("constituent", "constituent"))
        self.assertEqual(judge(d, self.outcomes([pull(1), pull(2)]))["state"], "success")
        self.assertEqual(judge(d, self.outcomes([pull(1), pull(2, None)]))["state"], "failed")

    def test_closed_retry_replaced_by_later_constituent_is_eventual_success(self):
        d = self.row(REPO + "#1:retry;" + REPO + "#2")
        result = judge(d, self.outcomes([pull(1, None), pull(2, 4, 3)]))
        self.assertEqual(result["state"], "success")
        self.assertFalse(result["first_pass_success"])
        self.assertEqual(result["attempt_prs"], [REPO + "#1", REPO + "#2"])

    def test_closed_retry_without_later_merge_is_failed(self):
        d = self.row(REPO + "#1:retry;" + REPO + "#2")
        self.assertEqual(judge(d, self.outcomes([pull(1, None), pull(2, None, state="open")]))["state"], "failed")

    def test_earlier_constituent_does_not_excuse_later_closed_retry(self):
        d = self.row(REPO + "#1;" + REPO + "#2:retry")
        self.assertEqual(judge(d, self.outcomes([pull(1), pull(2, None, 3)]))["state"], "failed")

    def test_open_retry_is_pending(self):
        d = self.row(REPO + "#1:retry;" + REPO + "#2")
        self.assertEqual(judge(d, self.outcomes([pull(1, None, state="open"), pull(2)]))["state"], "in_progress")

    def test_explicit_followup_needs_no_prose_reference(self):
        d = self.row(REPO + "#1;" + REPO + "#2:followup")
        result = judge(d, self.outcomes([pull(1), pull(2, 4, 3)]))
        self.assertEqual(result["state"], "success")
        self.assertFalse(result["first_pass_success"])
        self.assertEqual(result["closed_at"].isoformat(), time(4).replace("Z", "+00:00"))

    def test_followup_window_must_close(self):
        d = self.row(REPO + "#1;" + REPO + "#2:followup")
        result = judge(d, self.outcomes([pull(1, 25), pull(2, 29, 26)]))
        self.assertEqual(result["state"], "immature")

    def test_followup_outside_window_does_not_break_first_pass(self):
        d = self.row(REPO + "#1;" + REPO + "#2:followup")
        self.assertTrue(judge(d, self.outcomes([pull(1), pull(2, 10, 9)]))["first_pass_success"])

    def test_followup_at_window_boundary_is_excluded(self):
        d = self.row(REPO + "#1;" + REPO + "#2:followup")
        self.assertTrue(judge(d, self.outcomes([pull(1), pull(2, 9, 8)]))["first_pass_success"])

    def test_explicit_followup_can_recover_revert(self):
        d = self.row(REPO + "#1;" + REPO + "#2:followup")
        commits = [{"sha": "f" * 40, "commit": {"message": "Revert #1", "committer": {"date": time(3)}}}]
        self.assertEqual(judge(d, self.outcomes([pull(1), pull(2, 5, 4)], commits))["state"], "success")

    def test_retry_or_followup_alone_cannot_declare_success(self):
        for role in ("retry", "followup"):
            self.assertEqual(judge(self.row(REPO + "#1:" + role), self.outcomes([pull(1)]))["reason"], "no_constituent")

    def test_invalid_suffix_is_sanitized(self):
        for suffix in (":owner", ":Retry", ":retry:followup", ":", ":follow-up"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "invalid or repeated prs entry"):
                self.row(REPO + "#1" + suffix)

    def test_same_pr_cannot_have_two_roles(self):
        with self.assertRaisesRegex(ValueError, "only one deliverable"):
            self.row(REPO + "#1;" + REPO + "#1:retry")

    def test_missing_followup_prevents_success(self):
        d = self.row(REPO + "#1;" + REPO + "#2:followup")
        self.assertEqual(judge(d, self.outcomes([pull(1)]))["reason"], "missing_pr")

    def test_commit_fix_breaks_first_pass_and_extends_observation(self):
        d = self.row(REPO + "#1")
        commits = [{"sha": "f" * 40, "commit": {"message": "Fix #1", "committer": {"date": time(3)}}}]
        result = judge(d, self.outcomes([pull(1)], commits))
        self.assertEqual(result["state"], "success")
        self.assertFalse(result["first_pass_success"])
        result = judge(d, self.outcomes([pull(1, 24)], [
            {"sha": "e" * 40, "commit": {"message": "Fix #1", "committer": {"date": time(26)}}}]))
        self.assertEqual(result["state"], "immature")

    def test_superseded_retry_discovered_as_a_fix_does_not_fail(self):
        d = self.row(REPO + "#1;" + REPO + "#2:retry;" + REPO + "#3")
        result = judge(d, self.outcomes([pull(1), pull(2, None, 3, title="Fix #1"), pull(3, 6, 5)]))
        self.assertEqual(result["state"], "success")
        self.assertFalse(result["first_pass_success"])

    def test_closed_retry_without_constituents_does_not_hide_failure(self):
        d = self.row(REPO + "#1:retry")
        self.assertEqual(judge(d, self.outcomes([pull(1, None)]))["state"], "failed")

    def other_repo(self, adapter, entry):
        adapter._load({"repository": "example/other", "coverage_start": time(1), "observed_at": time(32),
                       "pulls_complete": True, "commits_complete": True, "pulls": [entry], "commits": []})
        return adapter

    def test_explicit_followup_in_another_ledger_repository(self):
        d = Deliverable("D1", START, (REPO, "example/other"), (REPO + "#1", "example/other#2"), (),
                        pr_roles=("constituent", "followup"))
        outcomes = self.other_repo(self.outcomes([pull(1)]), pull(2, 4, 3))
        result = judge(d, outcomes)
        self.assertEqual(result["state"], "success")
        self.assertFalse(result["first_pass_success"])

    def test_unrelated_repository_constituent_cannot_supersede_retry(self):
        d = Deliverable("D1", START, (REPO, "example/other"), (REPO + "#1", "example/other#2"), (),
                        pr_roles=("retry", "constituent"))
        outcomes = self.other_repo(self.outcomes([pull(1, None)]), pull(2, 4, 3))
        self.assertEqual(judge(d, outcomes)["state"], "failed")
