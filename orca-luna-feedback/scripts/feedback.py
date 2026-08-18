#!/usr/bin/env python3
"""Scaffold, archive, and list evidence-grounded Orca Luna wave feedback."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parents[1]
NOTE_NAME = "feedback.md"
PLACEHOLDER = re.compile(r"<fill:[^>]*>")
PATTERNS_FILE = "patterns.json"
SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
GAP_KINDS = {"prompt", "decomposition", "judgment", "test", "tooling"}
MAX_ACTIVE_RULES = 24
MAX_RULE_LINE_CHARS = 240


class HelperError(RuntimeError):
    pass


def log_dir() -> Path:
    configured = os.environ.get("ORCA_LUNA_FEEDBACK_LOG_DIR", "").strip()
    return Path(configured) if configured else SKILL_ROOT / "log"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelperError(f"cannot read JSON {path}: {exc}") from exc


def receipt_dir(requested: str | None) -> Path:
    if not requested:
        raise HelperError("feedback commands require an explicit --receipt-dir")
    path = Path(requested).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise HelperError(f"receipt-dir must be an existing absolute path: {path}")
    return path.resolve()


def worker_reports(directory: Path) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    reports_path = directory / "reports"
    if not reports_path.is_dir():
        return reports
    for path in sorted(reports_path.glob("*.json")):
        payload = load_json(path)
        report = payload.get("report") if isinstance(payload, dict) else None
        reports[path.stem] = report if isinstance(report, dict) else {}
    return reports


def patterns_path() -> Path:
    return log_dir() / PATTERNS_FILE


def load_patterns() -> dict[str, Any]:
    path = patterns_path()
    if not path.exists():
        return {"patternsSchemaVersion": 1, "patterns": [], "gapCounts": {}}
    data = load_json(path)
    if not isinstance(data, dict) or data.get("patternsSchemaVersion") != 1:
        raise HelperError(f"unsupported patterns file: {path}")
    return data


def save_patterns(data: dict[str, Any]) -> None:
    path = patterns_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def pattern_id(failure_class: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", failure_class.lower()).strip("-")
    return slug[:48] or "unnamed"


def ingest_wave(directory: Path) -> dict[str, Any]:
    """Fold the wave's promptFeedback and ruleFeedback into patterns.json."""
    run_id = str(load_json(directory / "final.json").get("runId", "unknown-run"))
    data = load_patterns()
    by_id = {record["id"]: record for record in data["patterns"]}
    now = time.time()
    summary: dict[str, Any] = {
        "newRules": 0,
        "confirmations": 0,
        "activated": 0,
        "gapNotes": 0,
        "ruleFeedback": 0,
        "capBlocked": [],
    }
    for worker_id, report in worker_reports(directory).items():
        entries = report.get("promptFeedback")
        for entry in entries[:3] if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            failure_class = entry.get("failureClass")
            rule = entry.get("rule")
            if not isinstance(failure_class, str) or not failure_class.strip():
                continue
            if not isinstance(rule, str) or not rule.strip() or len(rule) > 200:
                continue
            gap = entry.get("gap", "prompt")
            if gap != "prompt":
                if gap in GAP_KINDS:
                    data["gapCounts"][gap] = data["gapCounts"].get(gap, 0) + 1
                    summary["gapNotes"] += 1
                continue
            severity = entry.get("severity")
            if severity not in SEVERITY_RANK:
                severity = "medium"
            scopes = [
                scope.strip()
                for scope in entry.get("scopes", [])
                if isinstance(scope, str) and scope.strip()
            ]
            identifier = pattern_id(failure_class)
            record = by_id.get(identifier)
            if record is None:
                record = {
                    "id": identifier,
                    "failureClass": failure_class.strip(),
                    "rule": rule.strip(),
                    "severity": severity,
                    "scopes": sorted(set(scopes))[:8],
                    "status": "candidate",
                    "count": 0,
                    "helped": 0,
                    "violated": 0,
                    "sources": [],
                    "firstSeen": now,
                }
                by_id[identifier] = record
                summary["newRules"] += 1
            else:
                summary["confirmations"] += 1
                if SEVERITY_RANK[severity] > SEVERITY_RANK.get(
                    record.get("severity"), 0
                ):
                    record["severity"] = severity
                record["scopes"] = sorted(
                    set(record.get("scopes", [])) | set(scopes)
                )[:8]
            record["count"] += 1
            record["lastSeen"] = now
            if all(source.get("runId") != run_id for source in record["sources"]):
                record["sources"] = (
                    record["sources"] + [{"runId": run_id, "workerId": worker_id}]
                )[-5:]
            if record["status"] == "candidate":
                needed = 1 if record["severity"] in {"critical", "high"} else 2
                distinct_runs = {source["runId"] for source in record["sources"]}
                if len(distinct_runs) >= needed:
                    active = sum(
                        1
                        for item in by_id.values()
                        if item.get("status") == "active"
                    )
                    if active < MAX_ACTIVE_RULES:
                        record["status"] = "active"
                        summary["activated"] += 1
                    else:
                        summary["capBlocked"].append(identifier)
        feedback_entries = report.get("ruleFeedback")
        for entry in feedback_entries if isinstance(feedback_entries, list) else []:
            if not isinstance(entry, dict):
                continue
            record = by_id.get(entry.get("id"))
            status = entry.get("status")
            if record is None or status not in {"violated", "helped", "retire"}:
                continue
            if status == "helped":
                record["helped"] = record.get("helped", 0) + 1
            elif status == "violated":
                record["violated"] = record.get("violated", 0) + 1
                record["lastSeen"] = now
            else:
                record["retireVotes"] = record.get("retireVotes", 0) + 1
            summary["ruleFeedback"] += 1
    data["patterns"] = sorted(
        by_id.values(),
        key=lambda record: (
            -SEVERITY_RANK.get(record.get("severity"), 0),
            -record.get("count", 0),
        ),
    )
    save_patterns(data)
    summary["patterns"] = str(patterns_path())
    return summary


def select_rules(
    data: dict[str, Any], scopes: set[str], limit: int, max_chars: int
) -> list[str]:
    candidates = [
        record
        for record in data["patterns"]
        if record.get("status") == "active"
        and (not scopes or not record.get("scopes") or scopes & set(record["scopes"]))
    ]
    candidates.sort(
        key=lambda record: (
            -SEVERITY_RANK.get(record.get("severity"), 0),
            -record.get("count", 0),
            -(record.get("lastSeen") or 0),
        )
    )
    selected: list[str] = []
    used = 0
    for record in candidates:
        line = f"[{record['id']}] {record['rule']}"
        if len(line) > MAX_RULE_LINE_CHARS:
            continue
        if len(selected) >= limit or used + len(line) > max_chars:
            break
        selected.append(line)
        used += len(line)
    return selected


def severity_summary(report: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for finding in report.get("findings") or []:
        if isinstance(finding, dict):
            severity = str(finding.get("severity", "?"))
            counts[severity] = counts.get(severity, 0) + 1
    return ", ".join(f"{name}:{count}" for name, count in sorted(counts.items())) or "none"


def scaffold_text(directory: Path) -> str:
    final_path = directory / "final.json"
    if not final_path.exists():
        raise HelperError(
            f"no {final_path.name} in {directory}; run finalize-wave before feedback"
        )
    final = load_json(final_path)
    manifest = load_json(directory / "manifest.json")
    reports = worker_reports(directory)
    checks = final.get("checks", {})
    failed = sorted(
        name for name, passed in checks.items() if passed is not True
    ) if isinstance(checks, dict) else []
    verdicts = ", ".join(
        f"{item.get('workerId')}={item.get('verdict')}"
        for item in final.get("contentVerdicts", [])
        if isinstance(item, dict)
    )
    lines = [
        f"# Wave feedback — {final.get('runId', 'unknown-run')}",
        "",
        f"- mode: {final.get('mode')}",
        f"- objective: {manifest.get('objective')}",
        f"- orchestration: {final.get('orchestrationHealth')}"
        f" ({final.get('mechanicalStatus')})",
        f"- failed checks: {', '.join(failed) or 'none'}",
        f"- unresolved: {', '.join(final.get('unresolved') or []) or 'none'}",
        f"- content verdicts: {verdicts or 'none'}",
    ]
    created_worktrees = final.get("createdWorktrees") or []
    if created_worktrees:
        lines.append(
            f"- created worktrees: {len(created_worktrees)} — verify each was"
            " integrated exactly once and removed"
        )
    lines += [
        "",
        "## Verdict",
        "",
        "<fill: one actionable sentence — the outcome and the single most"
        " important next step>",
        "",
        "## Mechanics vs content",
        "",
        "<fill: what the mechanics/content split above means for this wave,"
        " citing final.json checks and the deciding report fields>",
        "",
        "## Per-worker",
        "",
    ]
    for worker in final.get("workers", []):
        if not isinstance(worker, dict):
            continue
        worker_id = worker.get("worker_id", "?")
        report = reports.get(str(worker_id), {})
        lines += [
            f"### {worker_id} ({worker.get('role')}, {worker.get('launch')},"
            f" verdict {worker.get('verdict') or '-'},"
            f" findings {severity_summary(report)})",
            "",
            "<fill: one keep and one change tied to this worker's report, or"
            " delete this section>",
            "",
        ]
    lines += [
        "## Subagent prompts",
        "",
        "<fill: judge the rendered worker prompts in prompts/ — did goal, scope,"
        " AC wording, context, and findings framing set each worker up or mislead"
        " it; changes must be prompt-executable (reword goal, tighten scope, add"
        " context or lens), citing prompts/<worker>.txt — or 'None.'>",
        "",
        "## Next-wave manifest adjustments",
        "",
        "<fill: manifest-executable deltas — shard boundaries, AC wording,"
        " launch choice, repair budget, worktree layout — or 'None.'>",
        "",
        "## Skill gaps",
        "",
        "<fill: contract friction or helper errors with exact receipt paths,"
        " or 'None observed.'>",
        "",
    ]
    return "\n".join(lines)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def command_scaffold(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    note = directory / NOTE_NAME
    if note.exists():
        raise HelperError(f"{note} already exists; edit it instead of re-scaffolding")
    text = scaffold_text(directory)
    note.write_text(text, encoding="utf-8")
    print(
        compact(
            {
                "status": "scaffolded",
                "note": str(note),
                "placeholders": len(PLACEHOLDER.findall(text)),
                "next": "replace every <fill: ...> with findings cited from the"
                " journal, then run archive",
            }
        )
    )
    return 0


def command_archive(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    note = directory / NOTE_NAME
    if not note.exists():
        raise HelperError(f"no {NOTE_NAME} in {directory}; run scaffold first")
    text = note.read_text(encoding="utf-8")
    remaining = PLACEHOLDER.findall(text)
    if remaining:
        raise HelperError(
            f"{len(remaining)} placeholder(s) still unfilled; "
            "fill each one or delete its section"
        )
    run_id = load_json(directory / "final.json").get("runId", "unknown-run")
    safe_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(run_id))
    destination_dir = log_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    destination = destination_dir / f"{stamp}-{safe_run}.md"
    counter = 1
    while destination.exists():
        counter += 1
        destination = destination_dir / f"{stamp}-{safe_run}-{counter}.md"
    destination.write_text(text, encoding="utf-8")
    learned = ingest_wave(directory)
    print(compact({"status": "archived", "log": str(destination), "learned": learned}))
    return 0


def command_rules(args: argparse.Namespace) -> int:
    data = load_patterns()
    if args.retire:
        record = next(
            (item for item in data["patterns"] if item["id"] == args.retire), None
        )
        if record is None:
            raise HelperError(f"no rule with id {args.retire!r}")
        record["status"] = "retired"
        record["retiredAt"] = time.time()
        if args.reason:
            record["retiredReason"] = args.reason
        save_patterns(data)
        print(compact({"status": "retired", "id": record["id"]}))
        return 0
    scopes = {part.strip() for part in (args.scopes or "").split(",") if part.strip()}
    selected = select_rules(data, scopes, args.limit, args.max_chars)
    print(
        compact(
            {
                "status": "ok",
                "active": sum(
                    1 for item in data["patterns"] if item.get("status") == "active"
                ),
                "candidates": sum(
                    1 for item in data["patterns"] if item.get("status") == "candidate"
                ),
                "forManifest": selected,
                "chars": sum(len(line) for line in selected),
                "patterns": str(patterns_path()),
                "next": "put forManifest into envelope.knownFailureModes of the"
                " next wave manifest",
            }
        )
    )
    return 0


def note_verdict(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("## Verdict") + 1
    except ValueError:
        return ""
    for line in lines[start:]:
        if line.strip() and not line.startswith("#"):
            return line.strip()
        if line.startswith("#"):
            break
    return ""


def command_log(args: argparse.Namespace) -> int:
    entries = sorted(log_dir().glob("*.md"))
    tail = entries[-args.tail :] if args.tail else entries
    print(
        compact(
            {
                "status": "ok",
                "total": len(entries),
                "entries": [
                    {"note": str(path), "verdict": note_verdict(path)}
                    for path in tail
                ],
            }
        )
    )
    return 0


def command_self_test(_: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="orca-luna-feedback-") as temporary:
        directory = Path(temporary) / "receipts"
        (directory / "reports").mkdir(parents=True)
        (directory / "final.json").write_text(
            json.dumps(
                {
                    "runId": "run_test",
                    "mode": "audit",
                    "orchestrationHealth": "PASS",
                    "mechanicalStatus": "ready_for_sol_gate",
                    "checks": {"runBound": True, "allReportsValid": False},
                    "unresolved": [],
                    "contentVerdicts": [{"workerId": "w1", "verdict": "FAIL"}],
                    "workers": [
                        {
                            "worker_id": "w1",
                            "role": "reviewer",
                            "launch": "sol-xhigh",
                            "verdict": "FAIL",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (directory / "manifest.json").write_text(
            json.dumps({"objective": "test objective"}), encoding="utf-8"
        )
        (directory / "reports" / "w1.json").write_text(
            json.dumps(
                {
                    "report": {
                        "findings": [
                            {"severity": "high", "title": "t", "evidence": "e"}
                        ],
                        "promptFeedback": [
                            {
                                "failureClass": "mutable receipt as truth",
                                "rule": "Treat the signed manifest as the source of truth; a receipt cannot shrink the required inventory.",
                                "severity": "critical",
                                "scopes": ["publication"],
                            },
                            {
                                "failureClass": "per-object fsync",
                                "rule": "Check the algorithm and fsync cadence against thousands of small files.",
                                "severity": "medium",
                                "scopes": ["native", "distribution"],
                            },
                            {
                                "failureClass": "cfg(test) bypass",
                                "rule": "Show a production call site; a test-only function proves nothing.",
                                "severity": "high",
                                "scopes": ["tests"],
                                "gap": "test",
                            },
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        os.environ["ORCA_LUNA_FEEDBACK_LOG_DIR"] = str(Path(temporary) / "log")
        try:
            assert command_scaffold(argparse.Namespace(receipt_dir=str(directory))) == 0
            text = (directory / NOTE_NAME).read_text(encoding="utf-8")
            assert "failed checks: allReportsValid" in text
            assert "findings high:1" in text
            assert PLACEHOLDER.search(text)
            try:
                command_archive(argparse.Namespace(receipt_dir=str(directory)))
            except HelperError as exc:
                assert "placeholder" in str(exc)
            else:
                raise AssertionError("archive accepted unfilled placeholders")
            (directory / NOTE_NAME).write_text(
                PLACEHOLDER.sub("Filled with cited judgment.", text),
                encoding="utf-8",
            )
            assert command_archive(argparse.Namespace(receipt_dir=str(directory))) == 0
            archived = list((Path(temporary) / "log").glob("*run_test*.md"))
            assert len(archived) == 1
            assert note_verdict(archived[0]) == "Filled with cited judgment."
            assert command_log(argparse.Namespace(tail=5)) == 0

            data = load_patterns()
            by_id = {record["id"]: record for record in data["patterns"]}
            assert by_id["mutable-receipt-as-truth"]["status"] == "active"
            assert by_id["per-object-fsync"]["status"] == "candidate"
            assert "cfg-test-bypass" not in by_id
            assert data["gapCounts"] == {"test": 1}

            second = Path(temporary) / "receipts2"
            (second / "reports").mkdir(parents=True)
            (second / "final.json").write_text(
                json.dumps({"runId": "run_test2"}), encoding="utf-8"
            )
            (second / "reports" / "w2.json").write_text(
                json.dumps(
                    {
                        "report": {
                            "promptFeedback": [
                                {
                                    "failureClass": "per-object fsync",
                                    "rule": "Check the algorithm and fsync cadence against thousands of small files.",
                                    "severity": "medium",
                                    "scopes": ["native"],
                                }
                            ],
                            "ruleFeedback": [
                                {
                                    "id": "mutable-receipt-as-truth",
                                    "status": "helped",
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            summary = ingest_wave(second)
            assert summary["activated"] == 1 and summary["ruleFeedback"] == 1
            data = load_patterns()
            by_id = {record["id"]: record for record in data["patterns"]}
            assert by_id["per-object-fsync"]["status"] == "active"
            assert by_id["per-object-fsync"]["count"] == 2
            assert by_id["mutable-receipt-as-truth"]["helped"] == 1

            selected = select_rules(data, {"native"}, 5, 900)
            assert selected == [
                "[per-object-fsync] Check the algorithm and fsync cadence"
                " against thousands of small files."
            ]
            everything = select_rules(data, set(), 5, 900)
            assert len(everything) == 2
            assert select_rules(data, set(), 5, 140) == everything[:1]

            capped = load_patterns()
            for index in range(MAX_ACTIVE_RULES):
                capped["patterns"].append(
                    {
                        "id": f"filler-{index}",
                        "failureClass": f"filler {index}",
                        "rule": "Filler rule.",
                        "severity": "high",
                        "scopes": [],
                        "status": "active",
                        "count": 1,
                        "helped": 0,
                        "violated": 0,
                        "sources": [{"runId": "run_fill"}],
                        "firstSeen": 0,
                    }
                )
            save_patterns(capped)
            third = Path(temporary) / "receipts3"
            (third / "reports").mkdir(parents=True)
            (third / "final.json").write_text(
                json.dumps({"runId": "run_test3"}), encoding="utf-8"
            )
            (third / "reports" / "w3.json").write_text(
                json.dumps(
                    {
                        "report": {
                            "promptFeedback": [
                                {
                                    "failureClass": "detached blocking work",
                                    "rule": "Join every spawn_blocking task before the lease is released or returned.",
                                    "severity": "critical",
                                    "scopes": ["native"],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            summary = ingest_wave(third)
            assert summary["capBlocked"] == ["detached-blocking-work"]
            data = load_patterns()
            by_id = {record["id"]: record for record in data["patterns"]}
            assert by_id["detached-blocking-work"]["status"] == "candidate"

            command_rules(
                argparse.Namespace(
                    scopes=None,
                    limit=5,
                    max_chars=900,
                    retire="per-object-fsync",
                    reason="superseded",
                )
            )
            data = load_patterns()
            by_id = {record["id"]: record for record in data["patterns"]}
            assert by_id["per-object-fsync"]["status"] == "retired"
        finally:
            os.environ.pop("ORCA_LUNA_FEEDBACK_LOG_DIR", None)
    print(compact({"status": "ok"}))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    scaffold = commands.add_parser(
        "scaffold", help="write a fact-filled feedback.md into the receipt dir"
    )
    scaffold.add_argument("--receipt-dir", required=True)
    scaffold.set_defaults(func=command_scaffold)

    archive = commands.add_parser(
        "archive", help="validate the filled note and copy it to the durable log"
    )
    archive.add_argument("--receipt-dir", required=True)
    archive.set_defaults(func=command_archive)

    log = commands.add_parser("log", help="list archived feedback notes")
    log.add_argument("--tail", type=int, default=0)
    log.set_defaults(func=command_log)

    rules = commands.add_parser(
        "rules", help="select learned rules for the next manifest, or retire one"
    )
    rules.add_argument("--scopes", help="comma-separated scope tags to match")
    rules.add_argument("--limit", type=int, default=5)
    rules.add_argument("--max-chars", type=int, default=900)
    rules.add_argument("--retire", help="rule id to retire")
    rules.add_argument("--reason", help="why the rule is retired")
    rules.set_defaults(func=command_rules)

    self_test = commands.add_parser("self-test", help="run offline tests")
    self_test.set_defaults(func=command_self_test)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except HelperError as exc:
        print(compact({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
