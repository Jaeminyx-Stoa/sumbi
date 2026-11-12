"""Synthetic deleted-ref failures remain incomplete evidence, never success."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from outcomes import test_github_outcomes as live_tests
from outcomes.test_deliver import REPO, judge
from sumbi.cli import main
from sumbi.outcomes.github.live import GitHubOutcomes, RepositoryUnreadable
from sumbi.outcomes.github.recorded import FixtureOutcomes, branch_request_repo
from worker_github_fixtures import build_round

FIXTURES = Path(__file__).parents[1] / "fixtures/github/deleted-branch"
PREFIX = "/repos/" + REPO


class DeletedBranchTests(unittest.TestCase):
    setUp = live_tests.GitHubTests.setUp
    respond = live_tests.GitHubTests.respond
    ledger = live_tests.GitHubTests.ledger
    adapter = live_tests.GitHubTests.adapter

    def failure(self, predicate, code=404):
        def response(request, **kwargs):
            if predicate(urllib.parse.urlsplit(request.full_url)):
                self.calls.append(request)
                raise urllib.error.HTTPError(request.full_url, code,
                    "Synthetic unavailable", {}, io.BytesIO())
            return self.respond(request, **kwargs)
        self.open.side_effect = response

    def prepare(self, name):
        raw = json.loads((FIXTURES / name / "repository.json").read_text(encoding="utf-8"))
        response = copy.deepcopy(raw["pulls"][0]["response"])
        response["base"] = {"ref": "synthetic-removed"}
        self.routes[PREFIX + "/pulls"] = [response]
        self.routes[PREFIX + "/commits"] = []
        self.routes[PREFIX] = {"private": "synthetic-metadata"}
        for endpoint in ("/branches/", "/rules/branches/"):
            self.routes[PREFIX + endpoint + "synthetic-removed"] = self.routes[
                PREFIX + endpoint + "main"]
        self.failure(lambda parsed: parsed.path == PREFIX + "/commits"
            and "sha" in urllib.parse.parse_qs(parsed.query))
        return raw

    def test_deleted_branch_merged_and_open_replay_and_cache(self):
        for name, state in (("merged", "immature"), ("open", "in_progress")):
            with self.subTest(name=name):
                self.prepare(name)
                # Use distinct cache roots so each scenario captures its own PR state.
                adapter = GitHubOutcomes(self.ledger(), head_refs=True,
                    record=self.root / (name + "-record"), cache=self.root / (name + "-cache"))
                expected = judge(self.ledger()[0], adapter)
                self.assertEqual(expected["state"], state)
                self.assertFalse(adapter.observation(REPO).commits_complete)
                self.assertEqual(adapter.evidence_gaps, {"branch_unavailable": 1})
                self.assertTrue(any(r.full_url.endswith(PREFIX) for r in self.calls))
                for path in (self.root / (name + "-cache")).glob("*.json"):
                    self.assertNotIn("synthetic-metadata", path.read_text(encoding="utf-8"))
                self.open.side_effect = AssertionError("Replay must remain offline")
                replay = FixtureOutcomes(self.root / (name + "-record"))
                cached = GitHubOutcomes(self.ledger(), head_refs=True,
                    cache=self.root / (name + "-cache"))
                authored = FixtureOutcomes(FIXTURES / name)
                for outcomes in (replay, cached, authored):
                    self.assertEqual(judge(self.ledger()[0], outcomes)["state"], state)
                    self.assertEqual(outcomes.evidence_gaps, adapter.evidence_gaps)
                    self.assertFalse(outcomes.observation(REPO).commits_complete)
                for outcomes in (replay, cached):
                    self.assertEqual(judge(self.ledger()[0], outcomes), expected)
                self.assertEqual(cached.requests, 0)

    def test_surviving_branch_is_captured_after_missing_branch(self):
        self.prepare("merged")
        surviving = copy.deepcopy(self.routes[PREFIX + "/pulls"][0])
        surviving.update(number=2, state="open", closed_at=None, merged_at=None,
            merged=False, merge_commit_sha=None)
        surviving["head"] = {"sha": "2".zfill(40), "ref": "synthetic-other-worker"}
        surviving["base"] = {"ref": "synthetic-surviving"}
        self.routes[PREFIX + "/pulls"].append(surviving)
        previous = self.open.side_effect
        def response(request, **kwargs):
            parsed = urllib.parse.urlsplit(request.full_url)
            if urllib.parse.parse_qs(parsed.query).get("sha") == ["synthetic-surviving"]:
                self.calls.append(request)
                return io.BytesIO(json.dumps([{"sha": "f" * 40, "commit": {
                    "message": "Synthetic unrelated commit", "committer": {
                        "date": "2030-01-03T00:00:00Z"}}}]).encode())
            return previous(request, **kwargs)
        self.open.side_effect = response
        adapter = self.adapter(head_refs=True)
        self.assertEqual(len(adapter.commits[REPO]), 1)
        self.assertIsNotNone(adapter.pull(REPO + "#2"))
        self.assertEqual(adapter.evidence_gaps["branch_unavailable"], 1)
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            for request in self.calls]
        self.assertLess(next(i for i, query in enumerate(queries)
            if query.get("sha") == ["synthetic-removed"]), next(i for i, query in
                enumerate(queries) if query.get("sha") == ["synthetic-surviving"]))

    def test_legacy_null_branch_cache_is_refetched_and_classified(self):
        adapter = self.adapter()
        path = PREFIX + "/commits/synthetic-removed/check-runs?filter=all"
        destination = self.root / "cache" / (hashlib.sha256(path.encode()).hexdigest() + ".json")
        destination.write_text(json.dumps({"response": None,
            "fetched_at": live_tests.END.timestamp(), "observed_at": live_tests.END.timestamp()}),
            encoding="utf-8")
        self.routes[PREFIX] = {}
        self.failure(lambda parsed: "synthetic-removed" in parsed.path)
        self.assertIsNone(adapter._get(path, policy=True))
        self.assertEqual(adapter.evidence_gaps["branch_unavailable"], 1)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["gap"],
            "branch_unavailable")

    def test_repository_level_and_non_branch_failures_still_abort(self):
        cases = json.loads((FIXTURES / "failures.json").read_text(encoding="utf-8"))
        for case in cases:
            for code in (403, case["status"]):
                with self.subTest(case=case["kind"], code=code):
                    self.prepare("merged")
                    if case["kind"] == "repository":
                        def response(request, **kwargs):
                            parsed = urllib.parse.urlsplit(request.full_url)
                            if parsed.path == PREFIX or (parsed.path == PREFIX + "/commits"
                                and "sha" in urllib.parse.parse_qs(parsed.query)):
                                raise urllib.error.HTTPError(request.full_url,
                                    code if parsed.path == PREFIX else 404,
                                    "Synthetic unavailable", {}, io.BytesIO())
                            return self.respond(request, **kwargs)
                        self.open.side_effect = response
                    else:
                        self.failure(lambda parsed: parsed.path == PREFIX + case["endpoint"], code)
                    with self.assertRaises(RepositoryUnreadable):
                        GitHubOutcomes(self.ledger(), cache=self.root /
                            (case["kind"] + "-" + str(code)))

    def test_branch_checks_and_compare_404s_are_cached_gaps(self):
        self.routes[PREFIX] = {}
        adapter = self.adapter(record=self.root / "record")
        paths = [PREFIX + "/commits/synthetic-removed/check-runs?filter=all",
            PREFIX + "/commits/synthetic-removed/statuses",
            PREFIX + "/compare/main...synthetic-removed"]
        self.failure(lambda parsed: "synthetic-removed" in parsed.path)
        for path in paths:
            self.assertIsNone(adapter._get(path))
        self.assertEqual(adapter.evidence_gaps["branch_unavailable"], len(paths))
        self.assertEqual(adapter._pages(paths[0], "check_runs", evidence=True), [])
        self.assertIn(paths[0], adapter._capture_incomplete)
        self.open.side_effect = AssertionError("Cached gaps must remain offline")
        for path in paths:
            self.assertIsNone(adapter._get(path))
        self.assertEqual(adapter.evidence_gaps["branch_unavailable"], len(paths) + 1)
        self.assertFalse(adapter.observation(REPO).commits_complete)
        adapter._record_repo(REPO)
        replay = FixtureOutcomes(self.root / "record")
        self.assertEqual(replay.evidence_gaps["branch_unavailable"], len(paths) + 1)
        self.assertFalse(replay.observation(REPO).commits_complete)

    def test_branch_scope_excludes_sha_and_unscoped_requests(self):
        for path in (PREFIX + "/commits", PREFIX + "/pulls", PREFIX + "/rules/branches/main",
            PREFIX + "/branches/main",
            PREFIX + "/commits?sha=" + "a" * 40,
            PREFIX + "/commits/" + "a" * 40 + "/check-runs",
            PREFIX + "/compare/" + "a" * 40 + "..." + "b" * 40):
            self.assertIsNone(branch_request_repo(path))
        self.assertEqual(branch_request_repo(PREFIX + "/commits?sha=synthetic%2Fremoved"), REPO)

    def test_recorded_gap_validation_and_completeness(self):
        raw = json.loads((FIXTURES / "merged/repository.json").read_text(encoding="utf-8"))
        raw["commits_complete"] = True
        directory = self.root / "fixture"
        directory.mkdir()
        path = directory / "repository.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertFalse(FixtureOutcomes(directory).observation(REPO).commits_complete)
        for gaps in (None, [PREFIX + "/pulls"], ["/repos/example/other/commits?sha=main"]):
            raw["branch_unavailable"] = gaps
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid branch request gaps"):
                FixtureOutcomes(directory)

    def test_worker_cli_continues_and_comparison_uses_capture_gate(self):
        home, _, directory, registration = build_round(self.root / "round", count=1)
        raw = json.loads((directory / "repository.json").read_text(encoding="utf-8"))
        responses = []
        for entry in raw["pulls"]:
            response = copy.deepcopy(entry["response"])
            response["base"] = {"ref": "synthetic-removed" if not responses else "main"}
            responses.append(response)
            for sha in (response["head"]["sha"], response["merge_commit_sha"]):
                self.routes[PREFIX + "/commits/" + sha + "/check-runs"] = {
                    "total_count": 1, "check_runs": [{"name": "test", "head_sha": sha,
                        "started_at": response["created_at"],
                        "completed_at": response["merged_at"],
                        "status": "completed", "conclusion": "success"}]}
                self.routes[PREFIX + "/commits/" + sha + "/statuses"] = []
        self.routes[PREFIX + "/pulls"] = responses
        self.routes[PREFIX + "/commits"] = []
        self.routes[PREFIX] = {}
        for endpoint in ("/branches/", "/rules/branches/"):
            self.routes[PREFIX + endpoint + "synthetic-removed"] = self.routes[
                PREFIX + endpoint + "main"]
        self.failure(lambda parsed: parsed.path == PREFIX + "/commits" and
            urllib.parse.parse_qs(parsed.query).get("sha") == ["synthetic-removed"])
        for command in ("deliver", "compare"):
            args = [command, "--outcome-source", "worker-github", "--repo", REPO,
                "--outcomes", "github", "--home", str(home), "--json", "-",
                "--cache", str(self.root / "cli-cache")]
            args += (["--since", "2030-01-01T00:00:00Z", "--until", "2030-01-15T00:00:00Z"]
                if command == "deliver" else ["--registration", str(registration),
                    "--resamples", "100"])
            output, errors = io.StringIO(), io.StringIO()
            with patch("sumbi.cli.worker_github.read_salt", return_value=b"synthetic-key"), \
                redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(main(args), 0, errors.getvalue())
            report = json.loads(output.getvalue())
            self.assertEqual(report["coverage"]["evidence_gaps"], {"branch_unavailable": 1})
            self.assertEqual(report["coverage"]["excluded_scope"], {})
            if command == "deliver":
                self.assertEqual(report["states"]["success"], 0)
                self.assertEqual(report["states"]["immature"], len(responses))
            else:
                self.assertEqual(report["verdict"]["proposal"], "withhold")
                self.assertIn("outcome_coverage_incomplete", report["verdict"]["reasons"])
                self.assertNotIn("worker_evidence_incomplete", report["verdict"]["reasons"])
            for private in (REPO, "synthetic-removed", "synthetic-token", str(self.root)):
                self.assertNotIn(private, output.getvalue() + errors.getvalue())
