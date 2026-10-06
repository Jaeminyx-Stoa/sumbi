"""Independent synthetic collect, local delivery and comparison flow contracts."""

import contextlib
from copy import deepcopy
import csv
from datetime import timedelta
import io
import json
from pathlib import Path
from support import IsolatedTemporaryDirectory
import unittest

from sumbi.cli import main
from sumbi.deliver import deliver
from sumbi.ledger import COLUMNS
from sumbi.local_compare import compare_local, text_summary as compare_summary
from sumbi.local_outcomes import deliver_local, text_summary as deliver_summary
from sumbi.model import ProjectRule, Window, timestamp
from sumbi.outcomes import FixtureOutcomes
from sumbi.report import collect, text_summary as collect_summary

FIXTURES = Path(__file__).parent / "fixtures/open_events"
SCRIPT = "scripts/check.sh"
SALT = b"synthetic-flow-key"


class OpenEventsFlows(unittest.TestCase):
    def setUp(self):
        self.temporary = IsolatedTemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.events = self.home / ".sumbi/events"
        self.events.mkdir(parents=True)
        self.window = Window(timestamp("2030-01-01T00:00:00Z"), timestamp("2030-01-03T00:00:00Z"))

    def save(self, name, records):
        path = self.events / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    def fixture(self, name):
        rows = [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()]
        for row in rows:
            if row.get("cwd") == "/fixture/workspace":
                row["cwd"] = str(self.repository.resolve())
        self.save(name, rows)
        return rows

    def worker(self, identity, *, day=1, agent="synthetic executor", role="worker", edits=True,
               code=0, verify=True, finish=True, spend=100, metadata=True, parent=None):
        at = timestamp(f"2030-01-0{day}T01:00:00Z")
        def event(kind, seconds, **fields):
            return {"schema_version": 1, "type": kind, "event_id": identity + "-" + kind,
                    "session_id": identity, "timestamp": (at + timedelta(seconds=seconds)).isoformat(), **fields}
        fields = {"agent": agent, "role": role, "cwd": str(self.repository.resolve()), "parent_session_id": parent}
        if metadata:
            fields.update(model="machine-model", effort="machine-effort", cli_version="machine-version")
        rows = [event("session_start", 0, **fields)]
        if edits:
            rows.append(event("file_edit", 2))
        if verify:
            rows.append(event("command_execution", 4, started_at=(at + timedelta(seconds=3)).isoformat(),
                              command=["bash", SCRIPT], cwd=str(self.repository.resolve()), exit_code=code))
        rows.append(event("token_usage", 5, cwd=str(self.repository.resolve()), tokens={
            "new_input": spend, "cache_write": 0, "cache_read": 0, "output": 0, "reasoning_output": None}))
        if finish:
            rows.append(event("session_end", 6))
        self.save(identity + ".jsonl", rows)
        return rows

    def registration(self, *, size=1):
        data = {"intervention_id": "synthetic-round", "outcome_source": "local-verify",
                "registered_at": "2029-12-31T00:00:00Z", "applied_at": "2030-01-02T00:00:00Z",
                "predictions": [{"metric": "tokens_per_success", "direction": "decrease", "rough_size_percent": 50}],
                "non_inferiority_margin_pp": 50, "sample_size_per_arm": size, "follow_up_days": 7,
                "before": {"since": "2030-01-01T00:00:00Z", "until": "2030-01-02T00:00:00Z"},
                "after": {"since": "2030-01-02T00:00:00Z", "until": "2030-01-03T00:00:00Z"}, "confounders": []}
        path = self.root / "registration.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def delivery(self, **kwargs):
        return deliver_local(self.home, self.window, self.repository, agents=["sumbi-events"],
                             verify=[SCRIPT], salt=SALT, **kwargs)

    def comparison(self, *, size=1, **kwargs):
        return compare_local(self.home, self.repository, self.registration(size=size),
                             agents=["sumbi-events"], verify=[SCRIPT], salt=SALT, resamples=100, **kwargs)

    def collection(self, **kwargs):
        return collect(self.home, self.window, agents=["sumbi-events"], salt=SALT, **kwargs)[0]

    def invoke(self, arguments):
        output, diagnostic = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
            code = main(arguments)
        return code, output.getvalue(), diagnostic.getvalue()

    def test_opt_in_recursive_sources_and_hand_calculated_resume(self):
        self.fixture("valid.jsonl")
        resumed = self.fixture("resumed.jsonl")
        (self.events / "resumed.jsonl").unlink()
        self.save("nested/resumed.jsonl", resumed)
        ignored, _ = collect(self.home, self.window, salt=SALT)
        self.assertEqual(ignored["summary"]["sessions"], 0)
        self.assertNotIn("sumbi-events", ignored["coverage"]["adapters"])
        report = self.collection()
        self.assertEqual(report["summary"]["sessions"], 2)
        self.assertEqual(report["summary"]["tokens"]["total"], 170)
        self.assertEqual(report["summary"]["counts"]["tool_calls"], 1)
        self.assertEqual(report["summary"]["counts"]["tool_results"], 1)
        self.assertEqual(report["summary"]["time"]["durations"]["tool"]["seconds"], 1)
        coverage = report["coverage"]["adapters"]["sumbi-events"]
        self.assertEqual(coverage["files_scanned"], 2)
        self.assertEqual(coverage["duplicate_events"], 2)
        self.assertEqual(coverage["sessions_in_window"], 2)
        self.assertEqual(len(report["by_agent"]), 2)
        self.assertEqual(sum(row["sessions"] for row in report["by_agent"].values()), 2)
        worker = next(row for row in report["sessions"] if row["parent_id"])
        parent = next(row for row in report["sessions"] if not row["parent_id"])
        self.assertEqual(worker["parent_id"], parent["id"])
        self.assertNotEqual(worker["agent"], parent["agent"])
        self.assertEqual(worker["tokens"]["complete_total"], 130)

    def test_private_strings_stay_out_of_all_reports_and_text(self):
        rows = self.fixture("valid.jsonl")
        rows[0]["agent"] = "CoordinatorPrivateMarker"
        rows[3].update(agent="ExecutorPrivateMarker", model="ModelPrivateMarker",
                       effort="EffortPrivateMarker", cli_version="VersionPrivateMarker")
        secrets = ["private prompt sentinel", "private path sentinel", "private output sentinel", "private command sentinel"]
        rows[3].update(prompt=secrets[0], path=secrets[1], stdout=secrets[2], command_arguments=secrets[3])
        self.save("valid.jsonl", rows)
        collect_report = self.collection()
        local_report = self.delivery()
        encoded = json.dumps([collect_report, local_report]) + collect_summary(collect_report) + deliver_summary(local_report)
        for value in [*secrets, SCRIPT, str(self.repository), "synthetic coordinator", "synthetic executor",
                      "synthetic model", "synthetic effort", "synthetic version", "root-session", "worker-session",
                      "CoordinatorPrivateMarker", "ExecutorPrivateMarker", "ModelPrivateMarker",
                      "EffortPrivateMarker", "VersionPrivateMarker"]:
            self.assertNotIn(value, encoded)
        self.assertTrue(all(row["agent"].startswith("sumbi-events:agent_") for row in collect_report["sessions"]))
        self.assertTrue(all(value.startswith("model_") for row in collect_report["sessions"] for value in row["models"]))
        self.assertEqual(local_report["units"][0]["parent_id"], local_report["dispatch_overhead"]["rows"][0]["id"])

    def test_five_fixed_worker_states_and_parent_overhead(self):
        self.worker("success")
        self.worker("failure", code=3)
        self.worker("unverified", verify=False)
        active = self.worker("active", verify=False, finish=False)
        active[-1]["timestamp"] = "2030-01-02T23:59:00Z"
        self.save("active.jsonl", active)
        self.worker("no-change", edits=False)
        self.worker("parent", role="orchestrator", edits=False, spend=40)
        report = self.delivery()
        self.assertEqual(report["states"], {"success": 1, "failed": 1, "unverified": 1, "in_progress": 1, "no_change": 1})
        self.assertEqual(report["success_rate"]["numerator"], 1)
        self.assertEqual(report["success_rate"]["denominator"], 3)
        self.assertEqual(report["cost"]["per_success"]["total"]["numerator"], 300)
        self.assertEqual(report["excluded_worker_spend"]["observed_total"], 200)
        self.assertEqual(report["dispatch_overhead"]["observed_total"], 40)
        self.assertEqual(report["dispatch_overhead"]["sessions"], 1)
        self.assertTrue(all(row["parent_id"] is None for row in report["units"]))

    def test_partial_tokens_and_reported_zero_remain_distinct(self):
        for kind in ("new_input", "cache_write", "cache_read", "output"):
            missing = self.worker("missing-" + kind, agent="codex", spend=0)
            missing[-2]["tokens"].pop(kind)
            self.save("missing-" + kind + ".jsonl", missing)
        unknown = self.worker("all-unknown", spend=0)
        unknown[-2]["tokens"] = dict.fromkeys(unknown[-2]["tokens"])
        self.save("all-unknown.jsonl", unknown)
        self.worker("zero", spend=0)
        report = self.delivery()
        self.assertEqual(sorted(row["tokens_complete"] for row in report["units"]), [False] * 5 + [True])
        self.assertEqual(sum(row["tokens"]["total"] == 0 for row in report["units"]), 1)
        self.assertEqual(sum(row["tokens"]["total"] is None for row in report["units"]), 5)
        collected = self.collection()
        self.assertEqual(collected["summary"]["tokens"]["total"], 0)
        partial = next(row for row in collected["sessions"] if row["tokens"]["cache_write"] is None)
        self.assertIsNone(partial["tokens"]["cache_write"])
        self.assertEqual(partial["tokens"]["not_reported_events"]["cache_write"], 1)

    def test_each_usage_event_must_be_complete_not_just_aggregate(self):
        rows = self.worker("partial")
        usage = deepcopy(rows[-2])
        rows[-2]["tokens"].pop("cache_write")
        usage.update(event_id="partial-second-usage", timestamp="2030-01-01T01:00:05.5Z")
        usage["tokens"] = {"new_input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        rows.insert(-1, usage)
        self.save("partial.jsonl", rows)
        unit = self.delivery()["units"][0]
        self.assertFalse(unit["tokens_complete"])
        self.assertIsNone(unit["tokens"]["total"])
        self.assertIsNone(self.collection()["sessions"][0]["tokens"]["complete_total"])

    def test_discarded_invalid_delta_keeps_observed_partial_but_not_complete_cost(self):
        rows = self.worker("valid-plus-invalid", spend=100)
        invalid = deepcopy(rows[-2])
        invalid.update(event_id="discarded-negative-delta", timestamp="2030-01-01T01:00:07Z")
        invalid["tokens"]["new_input"] = -1
        rows.append(invalid)
        self.save("valid-plus-invalid.jsonl", rows)
        session = self.collection()["sessions"][0]
        self.assertEqual(session["tokens"]["total"], 100)
        self.assertIsNone(session["tokens"]["complete_total"])
        unit = self.delivery()["units"][0]
        self.assertEqual(unit["observed_total"], 100)
        self.assertFalse(unit["tokens_complete"])
        self.assertIsNone(unit["tokens"]["total"])
        # A command-only evidence gap cannot erase already complete token cost.
        rows.pop()
        unknown_command = deepcopy(next(row for row in rows if row["type"] == "command_execution"))
        unknown_command.update(event_id="unknown-command-only", timestamp="2030-01-01T01:00:07Z", command=None)
        rows.append(unknown_command)
        self.save("valid-plus-invalid.jsonl", rows)
        self.assertEqual(self.collection()["sessions"][0]["tokens"]["complete_total"], 100)
        self.assertTrue(self.delivery()["units"][0]["tokens_complete"])

    def test_conflicting_global_event_id_invalidates_both_sessions(self):
        before = self.worker("before")
        after = self.worker("after", day=2)
        after[-2]["event_id"] = before[-2]["event_id"]
        self.save("after.jsonl", after)
        report = self.comparison()
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        self.assertIn("session_coverage_errors", report["verdict"]["reasons"])
        self.assertEqual(report["arms"]["before"]["candidate_states"]["unverified"], 1)
        self.assertEqual(report["arms"]["after"]["candidate_states"]["unverified"], 1)
        self.assertTrue(report["coverage"]["adapters"]["sumbi-events"]["unknown_record_types"])

    def test_record_errors_withhold_despite_otherwise_successful_units(self):
        for identity, change in (("version", "version"), ("unknown", "type"), ("boolean", "tokens"), ("reasoning", "reasoning")):
            with self.subTest(change=change):
                for path in self.events.rglob("*.jsonl"):
                    path.unlink()
                self.worker("before")
                rows = self.worker("after", day=2)
                malformed = deepcopy(rows[-2])
                malformed["event_id"] = identity
                if change == "version":
                    malformed["schema_version"] = True
                elif change == "type":
                    malformed["type"] = "private unsupported type"
                elif change == "tokens":
                    malformed["tokens"]["new_input"] = True
                else:
                    malformed["tokens"]["reasoning_output"] = 1
                rows.append(malformed)
                self.save("after.jsonl", rows)
                report = self.comparison()
                self.assertEqual(report["verdict"]["proposal"], "withhold")
                self.assertIn("session_coverage_errors", report["verdict"]["reasons"])
                self.assertNotIn("private unsupported type", json.dumps(report))

    def test_bad_json_keeps_later_records_visible_and_blocks_comparison(self):
        self.worker("before")
        self.worker("after", day=2)
        path = self.events / "after.jsonl"
        data = path.read_bytes()
        path.write_bytes(b"{broken synthetic line\n" + data)
        report = self.comparison()
        self.assertEqual(report["arms"]["after"]["n"], 1)
        self.assertEqual(report["coverage"]["adapters"]["sumbi-events"]["broken_lines"], 1)
        self.assertIn("session_coverage_errors", report["verdict"]["reasons"])

    def test_conflicting_dispatch_cannot_select_favorable_cohort(self):
        self.worker("before")
        rows = self.worker("after", day=2)
        duplicate_start = deepcopy(rows[0])
        duplicate_start.update(event_id="contradictory-dispatch", timestamp="2030-01-01T01:00:00Z")
        rows.append(duplicate_start)
        self.save("after.jsonl", rows)
        report = self.comparison()
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        self.assertIn("session_coverage_errors", report["verdict"]["reasons"])
        self.assertEqual(report["arms"]["after"]["n"], 0)
        self.assertEqual(report["arms"]["before"]["n"], 1)

    def test_emitter_change_cannot_hide_behind_same_adapter(self):
        self.worker("before", agent="emitter one")
        self.worker("after", day=2, agent="emitter two")
        report = self.comparison()
        self.assertEqual(report["verdict"]["proposal"], "withhold")
        self.assertIn("agent_mix_shift", report["verdict"]["reasons"])
        self.assertEqual(report["mixes"]["agent"]["total_variation_distance"], 1)
        self.assertNotIn("emitter one", json.dumps(report) + compare_summary(report))

    def test_metadata_absence_informational_but_partial_and_asymmetric_block(self):
        self.worker("before", metadata=False)
        self.worker("after", day=2, metadata=False)
        report = self.comparison()
        flags = {row["name"]: row for row in report["flags"]}
        for kind in ("model", "effort", "cli_version"):
            self.assertFalse(flags[kind + "_unobservable"]["blocking"])
        self.worker("after-known", day=2)
        report = self.comparison()
        blocking = {row["name"] for row in report["flags"] if row["blocking"]}
        for kind in ("model", "effort", "cli_version"):
            self.assertIn(kind + "_metadata_partial", blocking)
            self.assertIn(kind + "_metadata_asymmetric", blocking)
        for value in ("machine-model", "machine-effort", "machine-version"):
            self.assertNotIn(value, json.dumps(report) + compare_summary(report))

    def test_retained_worker_cost_comparison_can_adopt(self):
        for index in range(12):
            self.worker("before-" + str(index), spend=100)
            self.worker("after-" + str(index), day=2, spend=50)
        parent = self.worker("parent", role="orchestrator", spend=200, edits=False)
        parent[-1]["timestamp"] = "2030-01-02T04:00:00Z"
        self.save("parent.jsonl", parent)
        report = self.comparison(size=12)
        self.assertEqual(report["verdict"]["proposal"], "adopt")
        self.assertEqual(report["ratios"]["total"]["value"], 0.5)
        self.assertEqual(report["arms"]["after"]["n"], 12)
        self.assertEqual(report["cost_basis"], "retained_worker_lifetime_only")
        self.assertEqual(report["dispatch_overhead"]["sessions"], 1)

    def test_verifier_boundaries_never_preserve_false_success(self):
        changes = {
            "help": {"command": ["bash", SCRIPT, "--help"]},
            "read": {"command": ["cat", SCRIPT]},
            "background": {"command": "bash " + SCRIPT + " &"},
            "pre-edit": {"started_at": "2030-01-01T01:00:01Z"},
            "tied-edit": {"started_at": "2030-01-01T01:00:02Z"},
            "unknown-start": {"started_at": None},
            "unknown-cwd": {"cwd": None},
            "wrong-root": {"cwd": str(self.repository / "other")},
            "boolean-exit": {"exit_code": True},
        }
        for identity, fields in changes.items():
            rows = self.worker(identity)
            next(row for row in rows if row["type"] == "command_execution").update(fields)
            self.save(identity + ".jsonl", rows)
        for identity, field in (("later-unknown", "exit_code"), ("later-command-unknown", "command")):
            rows = self.worker(identity)
            check = deepcopy(next(row for row in rows if row["type"] == "command_execution"))
            check.update(event_id=identity + "-last", timestamp="2030-01-01T01:00:07Z", started_at="2030-01-01T01:00:06Z")
            check[field] = None
            rows.extend([check, {**rows[-1], "event_id": identity + "-end", "timestamp": "2030-01-01T01:00:08Z"}])
            self.save(identity + ".jsonl", rows)
        report = self.delivery()
        self.assertEqual(report["states"]["success"], 0)
        self.assertEqual(report["states"]["unverified"], 11)
        self.assertGreater(report["coverage"]["commands"].get("unmatched_shape", 0), 0)

    def test_window_bounds_and_dispatch_selection_remain_fixed(self):
        self.worker("inside")
        old = self.worker("outside")
        old[0]["timestamp"] = "2029-12-31T23:59:59Z"
        self.save("outside.jsonl", old)
        boundary = self.worker("upper")
        boundary[0]["timestamp"] = "2030-01-03T00:00:00Z"
        self.save("upper.jsonl", boundary)
        report = self.delivery()
        self.assertEqual(len(report["units"]), 1)
        self.assertEqual(report["states"]["success"], 1)

    def test_event_project_allocations_and_repository_selected_counts(self):
        rows = self.worker("multi-project")
        usage = deepcopy(rows[-2])
        usage.update(event_id="other-usage", timestamp="2030-01-01T01:00:05.5Z", cwd=str(self.root / "other"))
        usage["tokens"]["new_input"] = 50
        rows.insert(-1, usage)
        self.save("multi-project.jsonl", rows)
        rules = [ProjectRule("selected", paths=[str(self.repository)]), ProjectRule("other", paths=[str(self.root / "other")])]
        report = self.collection(rules=rules)
        self.assertEqual({row["rule"]: row["tokens"]["total"] for row in report["sessions"][0]["allocations"]}, {"selected": 100, "other": 50})
        scoped = self.collection(repository=self.repository)
        self.assertEqual(scoped["summary"]["tokens"]["total"], 100)
        self.assertEqual(scoped["sessions"][0]["tokens"]["complete_total"], 100)
        self.assertEqual(next(iter(scoped["by_agent"].values()))["tokens"]["complete_total"], 100)
        self.assertEqual(scoped["coverage"]["adapters"]["sumbi-events"]["sessions_selected"], 1)

    def test_null_and_conflicting_contexts_do_not_reuse_previous_project(self):
        rows = self.worker("context-worker")
        usage = deepcopy(rows[-2])
        usage.update(event_id="usage-after-barrier", timestamp="2030-01-01T01:00:05.5Z")
        usage.pop("cwd")
        usage["tokens"]["new_input"] = 50
        rows.insert(-1, usage)
        self.save("context-worker.jsonl", rows)
        null_context = {"schema_version": 1, "type": "context", "event_id": "null-cwd",
                        "session_id": "context-worker", "timestamp": "2030-01-01T01:00:05.25Z", "cwd": None}
        known_context = {**null_context, "event_id": "known-cwd", "cwd": str(self.repository.resolve())}
        for first, second in ((null_context, None), (known_context, null_context), (null_context, known_context)):
            with self.subTest(first=first["event_id"], conflict=second is not None):
                self.save("a-context.jsonl", [first])
                if second is None:
                    (self.events / "z-context.jsonl").unlink(missing_ok=True)
                else:
                    self.save("z-context.jsonl", [second])
                report = self.collection()
                allocations = {row["bucket"]: row["tokens"]["total"] for row in report["sessions"][0]["allocations"]}
                self.assertEqual(allocations, {"project": 100, "unassigned": 50})
                self.assertEqual(report["coverage"]["evidence_tokens"]["previous_event"], 0)

    def test_generic_github_spend_cannot_link_from_directory_or_project_time(self):
        rows = self.worker("generic-github", parent="D1")
        candidate = self.repository / "D1"
        candidate.mkdir()
        for row in rows:
            if "cwd" in row:
                row["cwd"] = str(candidate.resolve())
        self.save("generic-github.jsonl", rows)
        ledger = self.root / "deliverables.csv"
        with ledger.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerow(["D1", "2030-01-01T00:00:00Z", "Synthetic acceptance", "example/sample", "",
                             "work/D1", "abandoned@2030-01-02T00:00:00Z", "", ""])
        outcomes = self.root / "outcomes"
        outcomes.mkdir()
        (outcomes / "repository.json").write_text(json.dumps({
            "repository": "example/sample", "coverage_start": "2030-01-01T00:00:00Z",
            "observed_at": "2030-01-10T00:00:00Z", "pulls_complete": True,
            "commits_complete": True, "pulls": [], "commits": []}), encoding="utf-8")
        report = deliver(self.home, self.window, ledger, FixtureOutcomes(outcomes), agents=["sumbi-events"],
                         rules=[ProjectRule("sample", paths=[str(self.repository) + "*"])], salt=SALT)
        self.assertEqual(report["cost"]["period_spend"]["linked"]["total"], 0)
        self.assertEqual(report["cost"]["period_spend"]["unallocated"]["total"], 100)
        self.assertEqual(report["coverage"]["evidence_tokens"]["deliverable_id"], 0)
        self.assertEqual(report["coverage"]["evidence_tokens"]["project_time_weak"], 0)

    def test_mixed_native_and_open_events_keep_separate_identities(self):
        self.worker("shared-id", agent="codex")
        path = self.home / ".codex/sessions/rollout-synthetic.jsonl"
        path.parent.mkdir(parents=True)
        rows = [{"type": "session_meta", "timestamp": "2030-01-01T02:00:00Z", "payload": {"id": "shared-id", "cwd": str(self.repository)}},
                {"type": "event_msg", "timestamp": "2030-01-01T02:00:01Z", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report, _ = collect(self.home, self.window, agents=["codex", "sumbi-events"], salt=SALT)
        self.assertEqual(report["summary"]["sessions"], 2)
        self.assertEqual(report["summary"]["tokens"]["total"], 112)
        self.assertEqual(len({row["id"] for row in report["sessions"]}), 2)
        self.assertEqual(len(report["by_agent"]), 2)

    def test_cli_collect_delivery_comparison_and_protected_inputs(self):
        self.worker("before")
        self.worker("after", day=2)
        registration = self.registration()
        config = self.repository / ".sumbi/config.toml"
        config.parent.mkdir()
        config.write_text('verify = ["scripts/check.sh"]\n', encoding="utf-8")
        salt = self.root / "salt"
        salt.write_bytes(SALT)
        common = ["--home", str(self.home), "--agents", "sumbi-events", "--salt-file", str(salt)]
        bounds = ["--since", self.window.since.isoformat(), "--until", self.window.until.isoformat()]
        for arguments, kind in ((["collect", *bounds, *common], "collect"),
                                (["deliver", *bounds, *common, "--outcome-source", "local-verify", "--repository", str(self.repository)], "deliver"),
                                (["compare", *common, "--registration", str(registration), "--repository", str(self.repository), "--resamples", "100"], "compare")):
            code, output, diagnostic = self.invoke([*arguments, "--json", "-"])
            self.assertEqual(code, 0)
            report = json.loads(output)
            self.assertTrue(report["pseudonyms"]["salted"])
            self.assertNotIn(SCRIPT, output + diagnostic)
            if kind != "collect":
                self.assertEqual(report["outcome_source"], "local-verify")
        protected = [self.events / "before.jsonl", self.events / "nested/out.json", salt]
        for destination in protected:
            before = destination.read_bytes() if destination.exists() else None
            with self.subTest(destination=destination.name), self.assertRaises(SystemExit):
                self.invoke(["collect", *bounds, *common, "--json", str(destination)])
            self.assertEqual(destination.read_bytes() if destination.exists() else None, before)
        for destination in (config, registration):
            before = destination.read_bytes()
            with self.subTest(destination=destination.name), self.assertRaises(SystemExit):
                self.invoke(["compare", *common, "--registration", str(registration), "--repository", str(self.repository), "--json", str(destination)])
            self.assertEqual(destination.read_bytes(), before)
        with self.assertRaises(SystemExit):
            self.invoke(["compare", *common, "--registration", str(registration), "--outcome-source", "github", "--json", "-"])


if __name__ == "__main__":
    unittest.main()
