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
        "<fill: judge the rendered task prompts in tasks/ — did goal, scope, AC"
        " wording, context, and findings framing set each worker up or mislead"
        " it; changes must be prompt-executable (reword goal, tighten scope, add"
        " context or lens), citing tasks/<worker>.json — or 'None.'>",
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
                "next": "replace every <fill: ...> with journal-cited judgment,"
                " then run archive",
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
            f"{len(remaining)} unfilled placeholder(s) remain; judgment is not optional"
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
    print(compact({"status": "archived", "log": str(destination)}))
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
                        ]
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
