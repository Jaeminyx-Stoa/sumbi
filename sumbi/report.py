"""Allow-listed collection reports; free text belongs only in local review."""

from __future__ import annotations

from pathlib import Path

from sumbi import SCHEMA_VERSION
from sumbi.adapters import claude_code, codex
from sumbi.model import Attributor, COUNT_KINDS, Coverage, ProjectRule, RepositoryAttributor, TOKEN_KINDS, Window
from sumbi.privacy import current_key, pseudonym_key, read_salt

ADAPTERS = {"claude-code": claude_code, "codex": codex}


def totals(sessions: list[dict], idle_minutes: float = 5) -> dict:
    tokens = {}
    missing = {}
    for kind in TOKEN_KINDS:
        values = [s["tokens"][kind] for s in sessions]
        reported = [value for value in values if value is not None]
        tokens[kind] = sum(reported) if reported else None
        missing[kind] = len(values) - len(reported)
    tokens.update(total=sum(s["tokens"]["total"] for s in sessions), measurement="observed",
                  not_reported_sessions=missing,
                  total_basis="reported_components_excluding_reasoning_subset")
    thresholds = sorted({"2", "5", "10", format(idle_minutes, "g")}, key=float)
    sensitivity = {t: sum(s["time"]["active"]["sensitivity_seconds"][t] for s in sessions) for t in thresholds}
    time = {"wall_span_seconds": {"measurement": "observed", "value": sum(
        s["time"]["wall_span"]["seconds"] for s in sessions)},
        "active_seconds": {"measurement": "estimated", "value": sum(
            s["time"]["active"]["seconds"] for s in sessions), "idle_minutes": idle_minutes},
        "active_sensitivity_seconds": {"measurement": "estimated", "values": sensitivity},
        "durations": {kind: {"measurement": "observed", **{key: sum(
            s["time"]["durations"][kind][key] for s in sessions)
            for key in ("seconds", "paired_intervals", "unpaired_starts", "unpaired_ends")}}
                      for kind in ("tool", "request")},
        "parallel_session_time": True}
    return {"sessions": len(sessions), "tokens": tokens,
            "counts": {k: sum(s["counts"][k] for s in sessions) for k in COUNT_KINDS}, "time": time}


def collect(home: Path, window: Window, *, agents: list[str] | None = None,
            rules: list[ProjectRule] | None = None, idle_minutes: float = 5,
            local_review: bool = False, salt: bytes | None = None,
            repository: Path | None = None) -> tuple[dict, str]:
    with pseudonym_key(salt if salt is not None else read_salt()):
        return _collect(home, window, agents=agents, rules=rules, idle_minutes=idle_minutes,
                        local_review=local_review, repository=repository)


def _collect(home: Path, window: Window, *, agents, rules, idle_minutes,
             local_review, repository) -> tuple[dict, str]:
    attributor = RepositoryAttributor(repository) if repository is not None else Attributor(rules or [])
    sessions = []
    coverage = {}
    reviews = []
    for agent in agents if agents is not None else ADAPTERS:
        measured = Coverage()
        found = ADAPTERS[agent].collect(home, window, measured, local_review=local_review)
        included = [s for s in found if s.in_window(window)]
        coverage[agent] = {**measured.as_dict(), "sessions_read": len(found), "sessions_in_window": len(included)}
        for session in included:
            row = session.as_dict(window, attributor, idle_minutes)
            if repository is not None and row["project"]["bucket"] != "project":
                continue
            sessions.append(row)
            reviews.extend(session.id() + " " + text for text in sorted(session.review))
        if repository is not None:
            coverage[agent]["sessions_selected"] = sum(s["agent"] == agent for s in sessions)
    sessions.sort(key=lambda s: (s["agent"], s["id"]))
    summary = totals(sessions, idle_minutes)
    spend = {}
    for session in sessions:
        link = session["project"]
        identity = link["bucket"], link["project_key"] if link["bucket"] == "project" else None, link["rule"]
        row = spend.setdefault(identity, {"bucket": link["bucket"], "project_key": identity[1],
                                          "rule": link["rule"], "sessions": 0, "tokens": 0})
        row["sessions"] += 1
        row["tokens"] += session["tokens"]["total"]
    # Keep zero-spend coverage categories visible even for an empty home.
    for bucket in ("unassigned", "other"):
        spend.setdefault((bucket, None, None), {"bucket": bucket, "project_key": None,
                                               "rule": None, "sessions": 0, "tokens": 0})
    all_tokens = summary["tokens"]["total"]
    for row in spend.values():
        row["share"] = row["tokens"] / all_tokens if all_tokens else 0.0
        row["measurement"] = "observed"
    unattributed = sum(row["tokens"] for row in spend.values() if row["bucket"] != "project")
    salted = current_key() is not None
    report = {"schema_version": SCHEMA_VERSION,
              "pseudonyms": {"salted": salted, "algorithm": "hmac-sha256" if salted else "sha256"},
              "window": {"since": window.since.isoformat().replace("+00:00", "Z"),
                         "until": window.until.isoformat().replace("+00:00", "Z"), "bounds": "[since,until)"},
              "summary": summary, "by_agent": {agent: totals([s for s in sessions if s["agent"] == agent], idle_minutes)
                                                for agent in coverage},
              "coverage": {"adapters": coverage, "sessions_in_window": len(sessions),
                           "spend": sorted(spend.values(), key=lambda r: (r["bucket"], r["project_key"] or "")),
                           "unattributed_share": unattributed / all_tokens if all_tokens else 0.0,
                           "spend_basis": "reported_tokens", "verdict": "not_evaluated"},
              "sessions": sessions}
    if repository is not None:
        scanned = sum(c["sessions_in_window"] for c in coverage.values())
        report["scope"] = {"kind": "repository", "sessions_in_window": scanned,
                           "sessions_selected": len(sessions), "sessions_excluded": scanned - len(sessions)}
    return report, "\n".join(reviews) + ("\n" if reviews else "")


def text_summary(report: dict) -> str:
    """All labels come from schema categories, adapter fields or configured rules."""
    summary = report["summary"]
    lines = ["sumbi collect", "Window: " + report["window"]["since"] + " to " + report["window"]["until"] + " [since,until)",
             f"Sessions in window: {summary['sessions']}",
             f"Reported tokens (observed): {summary['tokens']['total']}"]
    lines.append("Pseudonym keys: " + ("salted (HMAC-SHA256)" if report["pseudonyms"]["salted"]
                                      else "unsalted (SHA-256)"))
    for agent, values in report["by_agent"].items():
        tokens = values["tokens"]
        lines.append(f"{agent}: sessions {values['sessions']}; tokens {tokens['total']} (observed)")
        lines.append("  " + "; ".join(kind + " " + ("not reported" if tokens[kind] is None else str(tokens[kind]))
                                      for kind in TOKEN_KINDS))
        lines.append("  Counts (observed): " + "; ".join(f"{key} {value}" for key, value in values["counts"].items()))
    lines.append(f"Summed wall span (observed): {summary['time']['wall_span_seconds']['value']:g} seconds")
    lines.append(f"Summed active time (estimated, idle {summary['time']['active_seconds']['idle_minutes']:g} minutes): "
                 f"{summary['time']['active_seconds']['value']:g} seconds")
    lines.append("Active sensitivity (estimated seconds): " + "; ".join(f"{key} minutes {value:g}" for key, value in
                 summary["time"]["active_sensitivity_seconds"]["values"].items()))
    for kind, values in summary["time"]["durations"].items():
        lines.append(f"Paired {kind} durations (observed): {values['seconds']:g} seconds; "
                     f"pairs {values['paired_intervals']}; unpaired starts {values['unpaired_starts']}; "
                     f"unpaired ends {values['unpaired_ends']}")
    lines.append("Parallel session times are summed; they are not human waiting time.")
    for agent, cov in report["coverage"]["adapters"].items():
        lines.append(f"Coverage {agent}: " + "; ".join(f"{key} {value}" for key, value in cov.items()
                                                      if key != "unknown_record_types"))
        lines.append("  Unknown record types: " + (", ".join(f"{key} {value}" for key, value in
                                                            cov["unknown_record_types"].items()) or "none"))
    for row in report["coverage"]["spend"]:
        name = row["bucket"] + (" " + row["rule"] + " " + row["project_key"] if row["rule"] else "")
        lines.append(f"Spend {name}: {row['tokens']} tokens (observed); share {row['share']:.6f}")
    lines.append(f"Unattributed token share: {report['coverage']['unattributed_share']:.6f}")
    return "\n".join(lines)
