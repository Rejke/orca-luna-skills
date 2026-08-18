#!/usr/bin/env python3
"""Render compact Luna task prompts and dispatch Orca worker waves."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback keeps atomic writes.
    fcntl = None


LAUNCH_SPECS = {
    "luna-max": {"agent": "codex", "model": "gpt-5.6-luna", "effort": "max"},
    "luna-fast": {
        "agent": "codex",
        "model": "gpt-5.6-luna",
        "effort": "max",
        "speedTier": "fast",
    },
    "sol-xhigh": {"agent": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
    "fable-high": {"agent": "claude", "model": "claude-fable-5", "effort": "high"},
}
# First alias is the role default; review roles are pinned and reject overrides.
# luna-fast is Luna max reasoning on the 1.5x fast service tier; legal only when
# the user explicitly asked for fast mode.
ROLE_LAUNCHES = {
    "scout": ("luna-max", "luna-fast", "fable-high"),
    "implementer": ("luna-max", "luna-fast", "fable-high"),
    "integrator": ("luna-max", "luna-fast", "fable-high"),
    "fixer": ("luna-max", "luna-fast", "fable-high"),
    "reviewer": ("sol-xhigh",),
    "antislop": ("sol-xhigh",),
}
MAX_WORKERS = 10
MAX_PROMPT_CHARS = 16_000
PROMPT_BUDGET_CHARS = 8_000
MAX_BODY_OUTPUT_CHARS = 3_000
STATE_FILE = "wave-state.json"
CANCEL_FILE = "cancel.requested.json"
NOTIFICATION_FILE = "controller-notification.pending.json"
AGENT_BOOT_TIMEOUT_MS = 120_000
STATE_VERSION = 4
MANIFEST_VERSION = 2
REPORT_VERSION = 1
MIN_ORCA_VERSION = "1.4.184"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = SKILL_ROOT / "references"
MODES = {"implementation", "audit", "benchmark"}
MUTATOR_ROLES = {"implementer", "integrator", "fixer"}
REVIEW_ROLES = {"reviewer", "antislop"}
# v2 dispatch runs on plain Orca terminals and worktrees; the orchestration
# layer (runs, tasks, dispatches, mail) is not used. Workers get their prompt
# from a file, write their report to a file, and ping Sol with the wake hook.
REQUIRED_ORCA_COMMANDS = {
    "agent-context": {"json"},
    "worktree current": {"json"},
    "worktree show": {"worktree", "json"},
    "worktree create": {"name", "setup", "json"},
    "terminal create": {"worktree", "title", "command", "json"},
    "terminal send": {"terminal", "text", "enter", "json"},
    "terminal read": {"terminal", "cursor", "limit", "json"},
    "terminal wait": {"terminal", "for", "timeout-ms", "json"},
    "terminal rename": {"terminal", "title", "json"},
    "terminal close": {"terminal", "json"},
}
TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "mode",
    "objective",
    "envelope",
    "defaults",
    "workers",
}


def spawn_command(spec: dict[str, Any]) -> str:
    """Shell command that starts the worker agent with its exact launch spec.

    No quotes anywhere: the string crosses the Windows PowerShell bridge once,
    and codex parses a bare -c value as a literal string.
    """
    if spec["agent"] == "codex":
        command = f"codex -m {spec['model']} -c model_reasoning_effort={spec['effort']}"
        if spec.get("speedTier") == "fast":
            command += " -c service_tier=priority"
        return command
    return f"claude --model {spec['model']}"
ENVELOPE_FIELDS = {
    "goal",
    "nonGoals",
    "acceptanceCriteria",
    "constraints",
    "baseAnchor",
    "dirtyState",
    "integrationDestination",
    "repairBudget",
    "benchmarkReason",
    "reviewedAnchor",
    "reviewOverride",
    "knownFailureModes",
}
MAX_KNOWN_FAILURE_MODES = 6
MAX_KNOWN_FAILURE_MODE_CHARS = 240
MAX_KNOWN_FAILURE_MODES_TOTAL = 1000
DEFAULT_FIELDS = {"worktree", "mutation", "setup", "checks"}
WORKER_FIELDS = {
    "id",
    "role",
    "goal",
    "scope",
    "criteria",
    "constraints",
    "checks",
    "ownership",
    "context",
    "findings",
    "handoffs",
    "lens",
    "launch",
    "worktree",
    "name",
    "displayName",
    "mutation",
    "setup",
}

ROLE_RULES = {
    "scout": (
        "Read only. Answer the bounded question with repository evidence. Propose disjoint "
        "shards only when useful; do not implement or decide the DAG."
    ),
    "implementer": (
        "Own only the declared shard. Inspect surrounding code first; make the smallest "
        "coherent diff. Reuse existing mechanisms, keep failures explicit, avoid speculative "
        "abstractions/compatibility, and run the required checks. Preserve unrelated changes."
    ),
    "integrator": (
        "Be the only writer in the integration worktree. Inspect and integrate each declared "
        "handoff exactly once. Resolve mechanical conflicts only; ask Sol about semantic "
        "conflicts. Run cross-shard checks and report the final state anchor."
    ),
    # The reviewer and antislop charters are adapted from 1F47E/rival
    # (bug-hunter, arch-security, code-quality role prompts and
    # AntislopCodePrompt), merged with this skill's report contract.
    "reviewer": (
        "Read only. Review the raw integrated state through your assigned lens; do "
        "not trust implementer conclusions and do not edit. Find concrete defects "
        "with high confidence, including where the implementer did not look: "
        "logic bugs, broken state transitions, wrong assumptions, missing "
        "edge cases, wrong wiring between layers, race conditions, data-loss risks, "
        "architectural regressions, incomplete refactors, broken flows across "
        "files, security and permission problems, and error handling that fails "
        "silently.\n"
        "Check the known patterns of generated code explicitly:\n"
        "- every import exists in the project's dependency tree;\n"
        "- every external call (DB, API, filesystem) handles null, empty, error, "
        "and timeout — not only the happy path;\n"
        "- no DB or API calls inside loops, no unbounded list queries;\n"
        "- tests assert specific values, not truthiness or no-throw;\n"
        "- no string interpolation in SQL or shell, no secrets in code, no missing "
        "auth on new routes;\n"
        "- new abstraction layers are justified by the task, not by habit;\n"
        "- no files changed outside the declared scope.\n"
        "Do not spend time on style, formatting, or speculative architecture "
        "opinions. Map every acceptance criterion to evidence. Report only "
        "findings you verified against the code, each with exact path:line; prefer "
        "fewer, stronger findings; if you are not confident, leave the finding out "
        "or cap it at medium. Sol issues the final verdict from your report — "
        "optimize for true positives, not completeness."
    ),
    "antislop": (
        "Read only; quality, not bugs. Do not report correctness or security "
        "issues, and skip style nitpicks. Work through every angle and name the "
        "concrete cut or replacement for each finding:\n"
        "1. Reuse and DRY — new code that re-implements what the codebase already "
        "has; duplicated logic is a finding even when each copy works. Name the "
        "existing helper, or the single home the copies should share.\n"
        "2. Simplification — redundant or derivable state, copy-paste with small "
        "variations, deep nesting, dead code. Name the simpler form.\n"
        "3. Efficiency — repeated computation or I/O, independent operations run "
        "sequentially, blocking work on startup or hot paths, closures that keep a "
        "whole scope alive. Name the cheaper form.\n"
        "4. Altitude — special cases layered on shared infrastructure mean the fix "
        "is too shallow; prefer generalizing the mechanism underneath.\n"
        "5. Backward-compat hoarding — shims, legacy fallbacks, versioned "
        "duplicates, re-exports kept just in case. Keep compat only for a named "
        "external consumer (published API, on-disk format, wire protocol); name "
        "that consumer or recommend the cut.\n"
        "6. Library reinvention — hand-rolled parsers, retry logic, date math, "
        "globbing. Prefer the stdlib and the project's existing dependencies; name "
        "the exact replacement.\n"
        "7. Slop signatures — comments narrating the obvious; silent fallbacks "
        'nobody asked for (ask "where was this behavior specified?" instead of '
        "guessing the intent); single-call wrappers, one-implementation "
        "interfaces, helper modules that collect unrelated functions; options nobody passes and "
        "generality nobody uses — verify by call-site search before reporting.\n"
        "Each finding carries a severity and a matching entry in cuts; leanness "
        "1-10 is information only. If the code is already lean, say so, rate it "
        "high, and return few or zero findings. Do not invent problems."
    ),
    "fixer": (
        "Own only the declared findings. Reproduce them when practical, fix root causes with "
        "the smallest diff, preserve unrelated changes, and rerun relevant checks. Do not "
        "weaken tests/types/lint or add fallback behavior to hide failures."
    ),
}

ROLE_REPORT_FIELDS = {
    "scout": {"shards": []},
    "implementer": {"commit": None},
    "integrator": {"integrated": [], "anchor": "", "conflicts": []},
    "reviewer": {"verdict": "PASS", "criteria": {}},
    "antislop": {
        "verdict": "PASS",
        "criteria": {},
        "cuts": [],
        "leanness": 1,
    },
    "fixer": {"fixed": [], "commit": None},
}


class HelperError(RuntimeError):
    pass


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_finding(item: Any) -> str:
    """Render one finding for a worker prompt; raw JSON is hard to read."""
    if isinstance(item, str):
        return f"- {item}"
    if not isinstance(item, dict) or not isinstance(item.get("title"), str):
        return f"- {compact_json(item)}"
    severity = item.get("severity")
    header = f"[{severity}] " if isinstance(severity, str) else ""
    lines = [f"- {header}{item['title'].strip()}"]
    for key in ("evidence", "location", "recommendation"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            prefix = "" if key == "evidence" else f"{key}: "
            lines.append(f"  {prefix}{value.strip()}")
    extras = {
        key: value
        for key, value in item.items()
        if key not in {"severity", "title", "evidence", "location", "recommendation"}
    }
    if extras:
        lines.append(f"  {compact_json(extras)}")
    return "\n".join(lines)


def render_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(
            f"- {item if isinstance(item, str) else compact_json(item)}"
            for item in value
        )
    if isinstance(value, dict):
        return "\n".join(
            f"- {key}: {item if isinstance(item, str) else compact_json(item)}"
            for key, item in value.items()
        )
    return compact_json(value)


def require_object(value: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HelperError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise HelperError(f"unknown {name} field(s): {', '.join(sorted(unknown))}")
    return value


def require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HelperError(f"{name} must be a non-empty string")
    return value.strip()


def string_list(
    value: Any, name: str, *, preserve_whitespace: bool = False
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise HelperError(f"{name} must be a list of non-empty strings")
    return list(value) if preserve_whitespace else [item.strip() for item in value]


def report_example(role: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "reportSchemaVersion": REPORT_VERSION,
        "taskStatus": "done",
        "summary": "<concise material result>",
        "evidence": ["<path:line or receipt fact>"],
        "findings": [
            {
                "severity": "medium",
                "title": "<material finding>",
                "evidence": "<reproducible evidence>",
            }
        ],
        "risks": ["<remaining material risk>"],
        "checks": ["<command or check> -> <result>"],
        "filesModified": [],
    }
    common.update(ROLE_REPORT_FIELDS[role])
    return common


def render_prompt(worker: dict[str, Any], envelope: dict[str, Any], mode: str) -> str:
    role = worker["role"]
    parts = [
        f"ROLE: {role.upper()}",
        f"MODE\n{mode}",
        f"MISSION\n{envelope['goal']}",
        f"GOAL\n{worker['goal']}",
    ]
    for key, label in (
        ("scope", "SCOPE"),
        ("ownership", "OWNERSHIP"),
        ("criteriaDefinitions", "ACCEPTANCE"),
        ("constraints", "CONSTRAINTS"),
        ("checks", "CHECKS"),
        ("lens", "LENS"),
        ("findings", "FINDINGS"),
        ("handoffs", "HANDOFFS"),
        ("context", "CONTEXT"),
    ):
        if worker.get(key) not in (None, "", [], {}):
            if key == "findings":
                parts.append(
                    "FINDINGS\n"
                    + "\n".join(render_finding(item) for item in worker["findings"])
                )
            else:
                parts.append(f"{label}\n{render_value(worker[key])}")
    if envelope.get("nonGoals"):
        parts.append(f"NON-GOALS\n{render_value(envelope['nonGoals'])}")
    state = {
        key: envelope[key]
        for key in (
            "baseAnchor",
            "dirtyState",
            "integrationDestination",
            "repairBudget",
        )
        if envelope.get(key) not in (None, "", [])
    }
    if state:
        parts.append(f"STATE\n{render_value(state)}")
    if envelope.get("knownFailureModes"):
        parts.append(
            "KNOWN FAILURE MODES RELEVANT TO THIS SCOPE\n"
            "Earlier waves failed in these ways. Follow each rule; it applies to "
            "your scope:\n"
            + "\n".join(f"- {rule}" for rule in envelope["knownFailureModes"])
        )
    parts.append(f"RULES\n{ROLE_RULES[role]}")
    lens = str(worker.get("lens", "")).lower()
    if role == "reviewer" and "antislop" in lens:
        parts.append(
            "ANTI-SLOP LENS\nAlso verify concrete cuts for duplication, unnecessary "
            "abstraction, silent fallback, speculative compatibility/generality, wrong "
            "implementation depth, reinvention, wrapper/comment slop, and wasted work."
        )
    parts.append(
        "REPORT\nWhen the job is finished, write the report as compact JSON "
        "(material evidence only, <=3000 chars) to the report file named in "
        "the RUNTIME section, with the Write tool. The report must match this "
        "contract. Replace every <...> placeholder; use an empty array when a "
        "category has no material items:\n" + compact_json(report_example(role))
    )
    parts.append(
        "The report file is your only channel to the controller. Write it "
        "once, when your work is complete or truly blocked — never as a draft "
        "or probe. If you are blocked, set taskStatus to \"blocked\", add a "
        "one-sentence question field, write the file, run the wake command, "
        "and stay idle in this terminal: the controller answers into this "
        "same session, and you then continue the task and write the final "
        "report. Never ask questions any other way; never use "
        "AskUserQuestion. After the final report, run the wake command from "
        "the RUNTIME section and stop."
    )
    if role in {"reviewer", "antislop"}:
        parts.append(
            'A finished review has taskStatus "done" even when the verdict is '
            "FAIL or UNKNOWN; the verdict judges the code, not your job. "
            "When the allowed evidence cannot prove or refute a claim, return verdict "
            "UNKNOWN and list the exact unprovable claims in risks; never stretch an "
            "evidence gap into PASS or into a FAIL finding without a defect."
        )
        learning = (
            "LEARNED-RULE FEEDBACK\nWhen a finding shows a failure class that a "
            "better worker prompt would have prevented, add an optional report field "
            'promptFeedback (max 3 entries): [{"failureClass":"<short class>",'
            '"rule":"<one imperative, checkable instruction, <=200 chars>",'
            '"severity":"critical|high|medium|low","scopes":["<area tags>"],'
            '"gap":"prompt|decomposition|judgment|test|tooling"}]. '
            '"Be careful" is not a rule; name the exact check to run.'
        )
        if envelope.get("knownFailureModes"):
            learning += (
                " For the KNOWN FAILURE MODES above, also add ruleFeedback: "
                '[{"id":"<id in brackets>","status":"violated|helped|retire"}] '
                "when you have evidence."
            )
        parts.append(learning)
    prompt = "\n\n".join(parts).strip() + "\n"
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HelperError(
            f"rendered prompt is {len(prompt)} chars; cap is {MAX_PROMPT_CHARS}"
        )
    return prompt


def worker_prompt_path(directory: Path, worker_id: str) -> Path:
    return directory / "prompts" / f"{worker_id}.txt"


def worker_report_path(directory: Path, worker_id: str) -> Path:
    return directory / "reports" / "incoming" / f"{worker_id}.json"


def runtime_prompt(prompt: str, directory: Path, worker_id: str) -> str:
    """Append the concrete runtime paths: report file and wake command."""
    notify_command = shlex.join(
        [
            "uv",
            "run",
            "--no-project",
            str(archived_helper(directory)),
            "notify-controller",
            "--receipt-dir",
            str(directory),
        ]
    )
    block = (
        "RUNTIME\n"
        f"Report file: {worker_report_path(directory, worker_id)}\n"
        "Wake command (run it in the shell after you write the report file):\n"
        f"{notify_command}\n"
        "The wake only tells the controller to look; the report file carries "
        "everything. After the wake, stop and stay idle in this terminal."
    )
    rendered = prompt.rstrip() + "\n\n" + block + "\n"
    if len(rendered) > MAX_PROMPT_CHARS:
        raise HelperError(
            f"live rendered prompt is {len(rendered)} chars; cap is {MAX_PROMPT_CHARS}"
        )
    return rendered


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelperError(f"cannot read JSON {path}: {exc}") from exc


def validate_manifest(
    manifest: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    manifest = require_object(manifest, "manifest", TOP_LEVEL_FIELDS)
    if manifest.get("schemaVersion") != MANIFEST_VERSION:
        raise HelperError(
            f"manifest.schemaVersion must be {MANIFEST_VERSION}; legacy manifests are not accepted"
        )
    mode = manifest.get("mode")
    if mode not in MODES:
        raise HelperError(f"manifest.mode must be one of: {', '.join(sorted(MODES))}")
    objective = require_string(manifest.get("objective"), "manifest.objective")
    envelope = require_object(
        manifest.get("envelope"), "manifest.envelope", ENVELOPE_FIELDS
    )
    envelope_goal = require_string(envelope.get("goal"), "envelope.goal")
    non_goals = string_list(envelope.get("nonGoals"), "envelope.nonGoals")
    constraints = string_list(envelope.get("constraints"), "envelope.constraints")
    dirty_state = string_list(
        envelope.get("dirtyState"),
        "envelope.dirtyState",
        preserve_whitespace=True,
    )
    criteria = envelope.get("acceptanceCriteria")
    if not isinstance(criteria, dict) or not criteria:
        raise HelperError("envelope.acceptanceCriteria must be a non-empty object")
    normalized_criteria: dict[str, str] = {}
    for criterion_id, definition in criteria.items():
        if not isinstance(criterion_id, str) or not re.match(
            r"^AC[0-9]+$", criterion_id
        ):
            raise HelperError(f"invalid acceptance criterion ID: {criterion_id!r}")
        normalized_criteria[criterion_id] = require_string(
            definition, f"acceptanceCriteria.{criterion_id}"
        )
    repair_budget = envelope.get("repairBudget")
    if not isinstance(repair_budget, int) or not 0 <= repair_budget <= 10:
        raise HelperError("envelope.repairBudget must be an integer between 0 and 10")
    if mode == "benchmark" and not envelope.get("benchmarkReason"):
        raise HelperError("benchmark mode requires envelope.benchmarkReason")
    reviewed_anchor = envelope.get("reviewedAnchor")
    if reviewed_anchor is not None:
        reviewed_anchor = require_string(reviewed_anchor, "envelope.reviewedAnchor")
    review_override = envelope.get("reviewOverride")
    if review_override is not None:
        review_override = require_string(review_override, "envelope.reviewOverride")
    if reviewed_anchor and review_override:
        raise HelperError(
            "declare either envelope.reviewedAnchor or envelope.reviewOverride, not both"
        )
    if reviewed_anchor:
        base_anchor = envelope.get("baseAnchor")
        if not base_anchor or reviewed_anchor != base_anchor:
            raise HelperError(
                "envelope.reviewedAnchor must equal envelope.baseAnchor"
            )
    known_failure_modes = string_list(
        envelope.get("knownFailureModes"), "envelope.knownFailureModes"
    )
    if len(known_failure_modes) > MAX_KNOWN_FAILURE_MODES:
        raise HelperError(
            f"envelope.knownFailureModes allows at most {MAX_KNOWN_FAILURE_MODES} rules"
        )
    if any(len(rule) > MAX_KNOWN_FAILURE_MODE_CHARS for rule in known_failure_modes):
        raise HelperError(
            "each known failure mode must be at most "
            f"{MAX_KNOWN_FAILURE_MODE_CHARS} characters"
        )
    if sum(len(rule) for rule in known_failure_modes) > MAX_KNOWN_FAILURE_MODES_TOTAL:
        raise HelperError(
            "knownFailureModes together must be at most "
            f"{MAX_KNOWN_FAILURE_MODES_TOTAL} characters"
        )

    defaults = require_object(
        manifest.get("defaults", {}), "manifest.defaults", DEFAULT_FIELDS
    )
    default_worktree = require_string(
        defaults.get("worktree", "current"), "defaults.worktree"
    )
    default_mutation = defaults.get(
        "mutation", "forbidden" if mode in {"audit", "benchmark"} else "allowed"
    )
    if default_mutation not in {"allowed", "forbidden"}:
        raise HelperError("defaults.mutation must be allowed or forbidden")
    if mode in {"audit", "benchmark"} and default_mutation != "forbidden":
        raise HelperError(f"{mode} defaults.mutation must be forbidden")
    default_setup = defaults.get("setup", "run")
    if default_setup not in {"run", "skip", "inherit"}:
        raise HelperError("defaults.setup must be run, skip, or inherit")
    default_checks = string_list(defaults.get("checks"), "defaults.checks")

    workers = manifest.get("workers")
    if not isinstance(workers, list) or not workers or len(workers) > MAX_WORKERS:
        raise HelperError(f"workers must contain 1..{MAX_WORKERS} entries")
    normalized_workers: list[dict[str, Any]] = []
    manifest_workers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_worktree_names: set[str] = set()
    mutator_locations: set[str] = set()
    for index, raw_worker in enumerate(workers, start=1):
        worker = require_object(raw_worker, f"workers[{index}]", WORKER_FIELDS)
        worker_id = require_string(worker.get("id"), f"workers[{index}].id")
        if not re.match(r"^[a-z][a-z0-9_-]{0,39}$", worker_id):
            raise HelperError(f"invalid worker ID: {worker_id!r}")
        if worker_id in seen_ids:
            raise HelperError(f"duplicate worker ID: {worker_id}")
        seen_ids.add(worker_id)
        role = worker.get("role")
        if role not in ROLE_RULES:
            raise HelperError(f"invalid role {role!r}; choose: {', '.join(ROLE_RULES)}")
        launch = worker.get("launch", ROLE_LAUNCHES[role][0])
        if launch not in ROLE_LAUNCHES[role]:
            raise HelperError(
                f"workers[{index}].launch must be one of "
                f"{', '.join(ROLE_LAUNCHES[role])} for role {role}"
            )
        goal = require_string(worker.get("goal"), f"workers[{index}].goal")
        criterion_refs = string_list(
            worker.get("criteria"), f"workers[{index}].criteria"
        )
        if not criterion_refs:
            raise HelperError(f"workers[{index}].criteria must not be empty")
        if len(criterion_refs) != len(set(criterion_refs)):
            raise HelperError(f"workers[{index}].criteria must not contain duplicates")
        unknown_criteria = set(criterion_refs) - set(normalized_criteria)
        if unknown_criteria:
            raise HelperError(
                f"worker {worker_id} references unknown criteria: "
                + ", ".join(sorted(unknown_criteria))
            )
        worktree = require_string(
            worker.get("worktree", default_worktree), f"workers[{index}].worktree"
        )
        mutation = worker.get("mutation", default_mutation)
        if mutation not in {"allowed", "forbidden"}:
            raise HelperError(f"workers[{index}].mutation must be allowed or forbidden")
        setup = worker.get("setup", default_setup)
        if setup not in {"run", "skip", "inherit"}:
            raise HelperError(f"workers[{index}].setup must be run, skip, or inherit")
        name = worker.get("name")
        if worktree in {"new-child", "new-top-level"}:
            name = require_string(name, f"workers[{index}].name")
            if name in seen_worktree_names:
                raise HelperError(f"duplicate new worktree name: {name}")
            seen_worktree_names.add(name)
        elif name is not None:
            raise HelperError("worker name applies only to new-child or new-top-level")
        if mode in {"audit", "benchmark"} and (
            mutation != "forbidden" or role in MUTATOR_ROLES
        ):
            raise HelperError(f"{mode} workers must be strictly read-only")
        if role in {"scout", *REVIEW_ROLES} and mutation != "forbidden":
            raise HelperError(f"read-only role {role} requires mutation=forbidden")
        if role in MUTATOR_ROLES and mutation != "allowed":
            raise HelperError(f"mutator role {role} requires mutation=allowed")
        if role in MUTATOR_ROLES and worktree not in {"new-child", "new-top-level"}:
            if worktree in mutator_locations:
                raise HelperError(
                    f"parallel mutators cannot share worktree selector {worktree!r}"
                )
            mutator_locations.add(worktree)
        worker_constraints = string_list(
            worker.get("constraints"), f"workers[{index}].constraints"
        )
        worker_checks = string_list(worker.get("checks"), f"workers[{index}].checks")
        normalized_worker = {
            **worker,
            "id": worker_id,
            "role": role,
            "launch": launch,
            "goal": goal,
            "criteria": criterion_refs,
            "criteriaDefinitions": {
                criterion_id: normalized_criteria[criterion_id]
                for criterion_id in criterion_refs
            },
            "scope": string_list(worker.get("scope"), f"workers[{index}].scope"),
            "ownership": string_list(
                worker.get("ownership"), f"workers[{index}].ownership"
            ),
            "constraints": constraints + worker_constraints,
            "checks": default_checks + worker_checks,
            "worktree": worktree,
            "mutation": mutation,
            "setup": setup,
        }
        for field in ("context", "lens"):
            if field in worker and not isinstance(worker[field], str):
                raise HelperError(f"workers[{index}].{field} must be a string")
        for field in ("findings", "handoffs"):
            if field in worker and not isinstance(worker[field], list):
                raise HelperError(f"workers[{index}].{field} must be an array")
        if name is not None:
            normalized_worker["name"] = name
        display_name = worker.get("displayName")
        if display_name is not None:
            normalized_worker["displayName"] = require_string(
                display_name, f"workers[{index}].displayName"
            )
        normalized_workers.append(normalized_worker)
        manifest_workers.append(
            {
                key: value
                for key, value in {
                    **normalized_worker,
                    "constraints": worker_constraints,
                    "checks": worker_checks,
                }.items()
                if key != "criteriaDefinitions"
            }
        )

    has_mutators = any(
        worker["role"] in MUTATOR_ROLES for worker in normalized_workers
    )
    if mode == "implementation" and not has_mutators:
        raise HelperError(
            "implementation waves require at least one mutator; "
            "pure review or scout waves must use audit or benchmark mode"
        )
    new_worktree_mutators = [
        worker["id"]
        for worker in normalized_workers
        if worker["role"] in {"implementer", "fixer"}
        and worker["worktree"] in {"new-child", "new-top-level"}
    ]
    has_integrator = any(
        worker["role"] == "integrator" for worker in normalized_workers
    )
    if new_worktree_mutators and not has_integrator:
        raise HelperError(
            "mutators in new worktrees ("
            + ", ".join(new_worktree_mutators)
            + ") require an integrator in the same wave; run a single mutator in "
            "current instead of accumulating unmerged worktrees"
        )
    if has_mutators and not reviewed_anchor and not review_override:
        raise HelperError(
            "mutating waves require envelope.reviewedAnchor (a baseAnchor that a "
            "fresh reviewer PASSed) or an explicit envelope.reviewOverride reason; "
            "silent mutation on an unreviewed anchor is forbidden"
        )

    normalized_envelope = {
        **envelope,
        "goal": envelope_goal,
        "nonGoals": non_goals,
        "acceptanceCriteria": normalized_criteria,
        "constraints": constraints,
        "dirtyState": dirty_state,
        "repairBudget": repair_budget,
    }
    if reviewed_anchor:
        normalized_envelope["reviewedAnchor"] = reviewed_anchor
    if review_override:
        normalized_envelope["reviewOverride"] = review_override
    if known_failure_modes:
        normalized_envelope["knownFailureModes"] = known_failure_modes
    normalized_manifest = {
        "schemaVersion": MANIFEST_VERSION,
        "mode": mode,
        "objective": objective,
        "envelope": normalized_envelope,
        "defaults": {
            "worktree": default_worktree,
            "mutation": default_mutation,
            "setup": default_setup,
            "checks": default_checks,
        },
        "workers": manifest_workers,
    }
    prompts = [
        render_prompt(worker, normalized_envelope, mode)
        for worker in normalized_workers
    ]
    return normalized_manifest, normalized_workers, prompts


def resolve_orca() -> list[str]:
    configured = os.environ.get("ORCA_CLI_COMMAND", "").strip()
    if configured:
        command = shlex.split(configured)
        if not command:
            raise HelperError("ORCA_CLI_COMMAND is empty after parsing")
        return command
    if os.environ.get("ORCA_DEV_REPO_ROOT"):
        return ["orca-dev"]
    if sys.platform.startswith("linux"):
        return ["orca-ide"]
    return ["orca"]


def call_orca(arguments: list[str]) -> tuple[int, Any | None, str]:
    command = [*resolve_orca(), *arguments]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise HelperError(
            f"cannot run selected Orca CLI {command[0]!r}: {exc}"
        ) from exc
    payload: Any | None = None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        pass
    detail = (result.stderr or result.stdout).strip()[-2000:]
    return result.returncode, payload, detail


def run_orca(arguments: list[str]) -> Any:
    returncode, payload, detail = call_orca(arguments)
    if returncode != 0:
        raise HelperError(f"Orca exited {returncode}: {detail}")
    if payload is None:
        raise HelperError(f"Orca returned non-JSON output: {detail}")
    return payload


def normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def find_entity_id(value: Any, entity: str) -> str | None:
    alias = normalized_key(f"{entity}_id")
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if normalized_key(key) == alias and isinstance(child, str):
                return child
        nested = node.get(entity)
        if isinstance(nested, dict) and isinstance(nested.get("id"), str):
            return nested["id"]
        identifier = node.get("id")
        if isinstance(identifier, str) and re.match(
            rf"^{re.escape(entity)}[_-]", identifier
        ):
            return identifier
    return None


def find_terminal_handle(value: Any) -> str | None:
    preferred = {"agentterminalhandle", "terminalhandle"}
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if (
                normalized_key(key) in preferred
                and isinstance(child, str)
                and child.startswith("term_")
            ):
                return child
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key in ("handle", "id"):
            handle = node.get(key)
            if isinstance(handle, str) and handle.startswith("term_"):
                return handle
    return None


def receipt_dir(requested: str | None) -> Path:
    if not requested:
        raise HelperError(
            "live wave commands require an explicit absolute --receipt-dir"
        )
    raw = Path(requested).expanduser()
    if not raw.is_absolute():
        raise HelperError(
            "receipt-dir must be an absolute path outside the repository"
        )
    path = raw.resolve()
    try:
        probe = subprocess.run(
            ["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        probe = None
    if probe is not None and probe.returncode == 0:
        repo_root = Path(probe.stdout.strip()).resolve()
        if path == repo_root or repo_root in path.parents:
            raise HelperError(
                "receipt-dir must not be inside the active Git repository"
            )
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def wave_lock(directory: Path):
    with (directory / "wave-state.lock").open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_wave_state(directory: Path) -> dict[str, Any]:
    state = load_json(directory / STATE_FILE)
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise HelperError(
            f"invalid or unsupported wave state in {directory / STATE_FILE}"
        )
    expected_helper = state.get("helper_sha256")
    if expected_helper and expected_helper != helper_digest():
        raise HelperError(
            "this wave was dispatched by a different helper build; run the "
            f"archived copy instead: {archived_helper(directory)}"
        )
    if state.get("launch_specs") != LAUNCH_SPECS:
        raise HelperError(
            "wave was journaled under a different launch policy; use the "
            f"archived helper copy: {archived_helper(directory)}"
        )
    return state


def mutate_wave_state(
    directory: Path, update: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    with wave_lock(directory):
        state = read_wave_state(directory)
        update(state)
        state["updated_at"] = time.time()
        save_json(directory / STATE_FILE, state)
        return state


def update_worker_state(directory: Path, index: int, **changes: Any) -> dict[str, Any]:
    def update(state: dict[str, Any]) -> None:
        for record in state.get("workers", []):
            if record.get("index") == index:
                record.update(changes)
                return
        raise HelperError(f"wave state has no worker index {index}")

    return mutate_wave_state(directory, update)


def set_wave_phase(directory: Path, phase: str, *, error: str | None = None) -> None:
    def update(state: dict[str, Any]) -> None:
        state["phase"] = phase
        if error is None:
            state.pop("error", None)
        else:
            state["error"] = error

    mutate_wave_state(directory, update)


def cancel_requested(directory: Path) -> bool:
    return (directory / CANCEL_FILE).exists()


def request_cancel(directory: Path) -> None:
    save_json(
        directory / CANCEL_FILE,
        {"requested": True, "requested_at": time.time(), "requesting_pid": os.getpid()},
    )

    def update(state: dict[str, Any]) -> None:
        state["phase"] = "cancelling"
        state["cancel_requested"] = True

    mutate_wave_state(directory, update)


def worker_label(worker: dict[str, Any], index: int) -> str:
    raw = worker.get("displayName") or worker.get("name") or worker["goal"]
    concise = re.sub(r"\s+", " ", str(raw)).strip().rstrip(".")
    prefix = f"{index:02d} {worker['role']} · "
    available = max(12, 72 - len(prefix))
    if len(concise) > available:
        concise = concise[: available - 1].rstrip() + "…"
    return prefix + concise


def manifest_digest(manifest: dict[str, Any]) -> str:
    encoded = compact_json(manifest).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def helper_digest() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def archived_helper(directory: Path) -> Path:
    return directory / "runtime" / "helper.py"


def archive_helper(directory: Path) -> Path:
    """Freeze the dispatching helper so mid-wave commands survive skill upgrades."""
    destination = archived_helper(directory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), destination)
    return destination


def ensure_journal(directory: Path) -> None:
    for name in (
        "prompts",
        "worktrees",
        "terminals",
        "reports",
        "reports/incoming",
        "answers",
        "notifications",
        "runtime",
    ):
        (directory / name).mkdir(parents=True, exist_ok=True)


def version_key(version: str) -> tuple[int, ...]:
    numbers = [int(item) for item in re.findall(r"[0-9]+", version)]
    return tuple((numbers + [0, 0, 0, 0])[:4])


def first_named(value: Any, name: str) -> Any:
    wanted = normalized_key(name)
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if normalized_key(key) == wanted:
                return child
    return None


def runtime_identity(status: Any) -> dict[str, Any]:
    version = first_named(status, "appVersion")
    runtime_id = first_named(status, "runtimeId")
    capabilities = first_named(status, "capabilities")
    state = first_named(status, "state")
    return {
        "appVersion": version,
        "runtimeId": runtime_id,
        "state": state,
        "capabilities": capabilities if isinstance(capabilities, list) else [],
    }


def command_registry(context: Any) -> dict[str, dict[str, Any]]:
    commands = first_named(context, "commands")
    if not isinstance(commands, list):
        raise HelperError("agent-context did not contain a commands array")
    registry: dict[str, dict[str, Any]] = {}
    for command in commands:
        if not isinstance(command, dict):
            continue
        name = command.get("command")
        if isinstance(name, str):
            registry[name] = command
    return registry


def command_contract_check(
    context: Any,
) -> tuple[list[dict[str, Any]], str]:
    registry = command_registry(context)
    checks: list[dict[str, Any]] = []
    relevant: dict[str, Any] = {}
    for command, required_flags in REQUIRED_ORCA_COMMANDS.items():
        required_flags = set(required_flags)
        spec = registry.get(command)
        flags = set(spec.get("flags", [])) if isinstance(spec, dict) else set()
        missing = sorted(required_flags - flags)
        argument_mode = spec.get("argumentMode") if isinstance(spec, dict) else None
        passed = spec is not None and argument_mode == "parsed" and not missing
        checks.append(
            {
                "name": f"command:{command}",
                "passed": passed,
                "argumentMode": argument_mode,
                "missingFlags": missing,
            }
        )
        if spec is not None:
            relevant[command] = {
                "argumentMode": spec.get("argumentMode"),
                "usage": spec.get("usage"),
                "flags": sorted(flags),
            }
    contract_hash = hashlib.sha256(compact_json(relevant).encode("utf-8")).hexdigest()
    return checks, contract_hash


def codex_model_catalog() -> tuple[dict[str, dict[str, set[str]]], str | None]:
    configured = os.environ.get("ORCA_LUNA_CODEX_COMMAND", "codex").strip()
    command = shlex.split(configured)
    if not command:
        return {}, "ORCA_LUNA_CODEX_COMMAND is empty after parsing"
    if shutil.which(command[0]) is None:
        return {}, f"{command[0]!r} is not on PATH"
    try:
        result = subprocess.run(
            [*command, "debug", "models"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {}, str(exc)
    if result.returncode != 0:
        return {}, (result.stderr or result.stdout).strip()[-1000:]
    try:
        catalog = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}, "codex debug models returned non-JSON output"
    models = catalog.get("models", []) if isinstance(catalog, dict) else []
    supported: dict[str, dict[str, set[str]]] = {}
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            continue
        supported[item["slug"]] = {
            "efforts": {
                level.get("effort")
                for level in item.get("supported_reasoning_levels", [])
                if isinstance(level, dict) and isinstance(level.get("effort"), str)
            },
            "speedTiers": {
                tier
                for tier in item.get("additional_speed_tiers", [])
                if isinstance(tier, str)
            },
        }
    return supported, None


def launch_checks(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verify every launch spec the wave uses; the codex catalog is read once."""
    checks: list[dict[str, Any]] = []
    codex_catalog: dict[str, dict[str, set[str]]] | None = None
    codex_error: str | None = None
    for alias in sorted({worker["launch"] for worker in workers}):
        spec = LAUNCH_SPECS[alias]
        name = f"launch:{alias}"
        if spec["agent"] == "codex":
            if codex_catalog is None and codex_error is None:
                codex_catalog, codex_error = codex_model_catalog()
            if codex_error is not None:
                checks.append({"name": name, "passed": False, "error": codex_error})
                continue
            speed_tier = spec.get("speedTier")
            entry = (codex_catalog or {}).get(spec["model"], {})
            efforts = entry.get("efforts", set())
            speed_tiers = entry.get("speedTiers", set())
            # The check uses spec["model"]/spec["effort"] verbatim — the same
            # strings worker-start sends. A composed id (for example a "[fast]"
            # suffix) is not in the catalog and fails here instead of at launch.
            checks.append(
                {
                    "name": name,
                    "passed": "[" not in spec["model"]
                    and spec["effort"] in efforts
                    and (speed_tier is None or speed_tier in speed_tiers),
                    "model": spec["model"],
                    "effort": spec["effort"],
                    "speedTier": speed_tier,
                    "supportedEfforts": sorted(efforts),
                    "supportedSpeedTiers": sorted(speed_tiers),
                }
            )
        else:
            configured = os.environ.get("ORCA_LUNA_CLAUDE_COMMAND", "claude").strip()
            command = shlex.split(configured)
            executable = shutil.which(command[0]) if command else None
            checks.append(
                {
                    "name": name,
                    "passed": executable is not None,
                    "model": spec["model"],
                    "effort": spec["effort"],
                    **(
                        {"executable": str(Path(executable).resolve())}
                        if executable
                        else {"error": f"{configured!r} is not on PATH"}
                    ),
                }
            )
    return checks


def preflight_manifest(
    manifest: dict[str, Any], workers: list[dict[str, Any]]
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    controller_terminal_handle = os.environ.get("ORCA_TERMINAL_HANDLE", "").strip()
    checks.append(
        {
            "name": "controller-terminal-handle",
            "passed": bool(
                re.fullmatch(r"term_[A-Za-z0-9_-]+", controller_terminal_handle)
            ),
            "terminalHandle": controller_terminal_handle or None,
        }
    )
    status_code, status, status_detail = call_orca(["status", "--json"])
    if status_code != 0 or status is None:
        return {
            "status": "failed",
            "manifestSha256": manifest_digest(manifest),
            "checks": [
                {
                    "name": "orca-status",
                    "passed": False,
                    "error": status_detail,
                }
            ],
        }
    runtime = runtime_identity(status)
    app_version = runtime.get("appVersion")
    runtime_ready = runtime.get("state") == "ready"
    checks.append({"name": "runtime-ready", "passed": runtime_ready})
    checks.append(
        {
            "name": "minimum-orca-version",
            "passed": isinstance(app_version, str)
            and version_key(app_version) >= version_key(MIN_ORCA_VERSION),
            "actual": app_version,
            "minimum": MIN_ORCA_VERSION,
        }
    )

    context_code, context, context_detail = call_orca(["agent-context", "--json"])
    contract_hash = None
    contract_version = None
    if context_code == 0 and context is not None:
        contract_version = (
            context.get("schemaVersion") if isinstance(context, dict) else None
        )
        try:
            command_checks, contract_hash = command_contract_check(context)
        except HelperError as exc:
            checks.append(
                {
                    "name": "agent-context-contract",
                    "passed": False,
                    "error": str(exc),
                }
            )
        else:
            checks.extend(command_checks)
    else:
        checks.append(
            {
                "name": "agent-context",
                "passed": False,
                "error": context_detail,
            }
        )
    checks.extend(launch_checks(workers))
    checks.append(
        {
            "name": "uv-on-path",
            "passed": shutil.which("uv") is not None,
        }
    )


    selectors = sorted(
        {
            worker["worktree"]
            for worker in workers
            if worker["worktree"] not in {"new-child", "new-top-level"}
        }
    )
    needs_current = any(
        worker["worktree"] in {"current", "new-child", "new-top-level"}
        for worker in workers
    )
    resolved_worktrees: dict[str, str] = {}
    if needs_current:
        returncode, payload, detail = call_orca(["worktree", "current", "--json"])
        worktree_id = (
            find_entity_id(payload, "worktree") if payload is not None else None
        )
        passed = returncode == 0 and payload is not None and worktree_id is not None
        checks.append(
            {
                "name": "worktree:current",
                "passed": passed,
                "worktreeId": worktree_id,
                **(
                    {}
                    if passed
                    else {
                        "error": detail
                        or "worktree current receipt omitted the worktree ID"
                    }
                ),
            }
        )
        if worktree_id is not None:
            resolved_worktrees["current"] = worktree_id
    for selector in selectors:
        if selector == "current":
            continue
        returncode, payload, detail = call_orca(
            ["worktree", "show", "--worktree", selector, "--json"]
        )
        worktree_id = (
            find_entity_id(payload, "worktree") if payload is not None else None
        )
        passed = returncode == 0 and payload is not None and worktree_id is not None
        checks.append(
            {
                "name": f"worktree:{selector}",
                "passed": passed,
                "worktreeId": worktree_id,
                **(
                    {}
                    if passed
                    else {
                        "error": detail
                        or "worktree show receipt omitted the worktree ID"
                    }
                ),
            }
        )
        if worktree_id is not None:
            resolved_worktrees[selector] = worktree_id

    mutator_targets: list[str] = []
    unresolved_mutators: list[str] = []
    for worker in workers:
        if worker["mutation"] != "allowed":
            continue
        selector = worker["worktree"]
        if selector in {"new-child", "new-top-level"}:
            mutator_targets.append(f"new:{worker['id']}")
            continue
        target = resolved_worktrees.get(selector)
        if target is None:
            unresolved_mutators.append(worker["id"])
        else:
            mutator_targets.append(target)
    duplicate_mutator_targets = sorted(
        {target for target in mutator_targets if mutator_targets.count(target) > 1}
    )
    checks.append(
        {
            "name": "mutator-worktree-isolation",
            "passed": not unresolved_mutators and not duplicate_mutator_targets,
            "unresolvedWorkers": unresolved_mutators,
            "duplicateWorktreeIds": duplicate_mutator_targets,
        }
    )

    resolved_orca = resolve_orca()
    executable = shutil.which(resolved_orca[0]) or resolved_orca[0]
    passed = all(check.get("passed") is True for check in checks)
    return {
        "status": "passed" if passed else "failed",
        "manifestSha256": manifest_digest(manifest),
        "createdAt": time.time(),
        "launchSpecs": LAUNCH_SPECS,
        "controllerTerminalHandle": controller_terminal_handle,
        "orca": {
            "command": resolved_orca,
            "executable": str(Path(executable).resolve())
            if Path(executable).exists()
            else executable,
            "appVersion": app_version,
            "runtimeId": runtime.get("runtimeId"),
            "contractVersion": contract_version,
            "contractHash": contract_hash,
        },
        "checks": checks,
    }


def command_preflight(args: argparse.Namespace) -> int:
    manifest, workers, prompts = validate_manifest(load_json(args.manifest))
    directory = receipt_dir(args.receipt_dir)
    if (directory / STATE_FILE).exists():
        raise HelperError("cannot preflight over an existing wave state")
    ensure_journal(directory)
    archived = archive_helper(directory)
    save_json(directory / "manifest.json", manifest)
    live_prompts = [
        runtime_prompt(prompt, directory, worker["id"])
        for worker, prompt in zip(workers, prompts)
    ]
    receipt = preflight_manifest(manifest, workers)
    receipt["helper"] = {"sha256": helper_digest(), "archived": str(archived)}
    receipt["workers"] = [
        {
            "id": worker["id"],
            "role": worker["role"],
            "launch": worker["launch"],
            "worktree": worker["worktree"],
            "mutation": worker["mutation"],
            "label": worker_label(worker, index),
            "promptChars": len(prompt),
            "overBudget": len(prompt) > PROMPT_BUDGET_CHARS,
        }
        for index, (worker, prompt) in enumerate(zip(workers, live_prompts), start=1)
    ]
    save_json(directory / "preflight.json", receipt)
    failed = [check["name"] for check in receipt["checks"] if not check.get("passed")]
    oversized = [
        entry["id"] for entry in receipt["workers"] if entry["overBudget"]
    ]
    print(
        compact_json(
            {
                "status": receipt["status"],
                "launches": sorted({worker["launch"] for worker in workers}),
                "runtime": receipt.get("orca"),
                "failedChecks": failed,
                **(
                    {
                        "oversizedPrompts": oversized,
                        "note": f"trim manifest prose; budget is {PROMPT_BUDGET_CHARS} chars per spec",
                    }
                    if oversized
                    else {}
                ),
                "receipts": str(directory),
            }
        )
    )
    return 0 if receipt["status"] == "passed" else 2


def verify_preflight(
    directory: Path,
    manifest: dict[str, Any],
    workers: list[dict[str, Any]],
    prompts: list[str],
) -> dict[str, Any]:
    path = directory / "preflight.json"
    if not path.exists():
        raise HelperError(f"dispatch requires a successful preflight receipt: {path}")
    previous = load_json(path)
    if not isinstance(previous, dict) or previous.get("status") != "passed":
        raise HelperError("preflight receipt is not successful")
    digest = manifest_digest(manifest)
    if previous.get("manifestSha256") != digest:
        raise HelperError("manifest changed after preflight; run preflight again")
    previous_helper = previous.get("helper", {})
    if previous_helper.get("sha256") != helper_digest():
        raise HelperError("helper changed after preflight; run preflight again")
    if not archived_helper(directory).exists():
        archive_helper(directory)
    for prompt in prompts:
        runtime_prompt(prompt, directory, "size-check")

    current = preflight_manifest(manifest, workers)
    save_json(directory / "runtime" / "pre-dispatch.json", current)
    if current.get("status") != "passed":
        failed = [
            check.get("name")
            for check in current.get("checks", [])
            if isinstance(check, dict) and check.get("passed") is not True
        ]
        raise HelperError(f"preflight is no longer valid; failed checks: {failed}")
    previous_orca = previous.get("orca", {})
    current_orca = current.get("orca", {})
    identity_fields = (
        "command",
        "executable",
        "appVersion",
        "runtimeId",
        "contractVersion",
        "contractHash",
    )
    changed = [
        field
        for field in identity_fields
        if previous_orca.get(field) != current_orca.get(field)
    ]
    if changed:
        raise HelperError(
            "Orca identity/contract changed after preflight "
            f"({', '.join(changed)}); run preflight again"
        )
    if previous.get("controllerTerminalHandle") != current.get(
        "controllerTerminalHandle"
    ):
        raise HelperError(
            "controller terminal changed after preflight; run preflight again from "
            "the terminal that will own the wave"
        )
    return current


def command_prompt(args: argparse.Namespace) -> int:
    _, workers, prompts = validate_manifest(load_json(args.manifest))
    if args.worker:
        try:
            index = next(
                i for i, worker in enumerate(workers) if worker["id"] == args.worker
            )
        except StopIteration as exc:
            raise HelperError(f"manifest has no worker {args.worker!r}") from exc
    else:
        index = 0
    print(prompts[index], end="")
    return 0


def command_schema(args: argparse.Namespace) -> int:
    resources = {
        "manifest": load_json(REFERENCES / "manifest-v2.schema.json"),
        "report": load_json(REFERENCES / "report-v1.schema.json"),
        "example": load_json(REFERENCES / "manifest-v2.example.json"),
    }
    value = resources if args.kind == "all" else resources[args.kind]
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def wave_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "index",
        "worker_id",
        "role",
        "launch",
        "mutation",
        "label",
        "worktree_id",
        "terminal_handle",
        "spawn_command",
        "banner_proof",
        "start_status",
        "report_status",
        "task_status",
        "verdict",
        "question",
        "answers",
        "notification_status",
        "stop_status",
        "error",
    )
    return [
        {key: record[key] for key in keys if record.get(key) is not None}
        for record in state.get("workers", [])
    ]


def has_material_effects(value: Any) -> bool:
    if value in (None, {}, []):
        return False
    if isinstance(value, dict):
        return any(has_material_effects(child) for child in value.values())
    if isinstance(value, list):
        return any(has_material_effects(child) for child in value)
    return bool(value)


def classify_failure(payload: Any, *, known_effect_id: str | None = None) -> str:
    code = first_named(payload, "code") if payload is not None else None
    effects = first_named(payload, "effects") if payload is not None else None
    residual = (
        first_named(payload, "residualResources") if payload is not None else None
    )
    states = {
        str(child).lower()
        for node in walk(payload)
        if isinstance(node, dict)
        for key, child in node.items()
        if normalized_key(key) in {"state", "status", "outcome"}
        and isinstance(child, str)
    }
    if "outcome_unknown" in states or "unknown" in states:
        return "outcome_unknown"
    if (
        known_effect_id
        or has_material_effects(effects)
        or has_material_effects(residual)
    ):
        return "failed_with_known_effects"
    if isinstance(code, str) or first_named(payload, "ok") is False:
        return "rejected_no_effects"
    return "outcome_unknown"


AMBIGUOUS_START_STATUSES = {
    "creating_worktree",
    "spawning",
    "booting",
    "worktree_outcome_unknown",
    "terminal_outcome_unknown",
}


def write_prompts(
    directory: Path, workers: list[dict[str, Any]], prompts: list[str]
) -> None:
    for worker, prompt in zip(workers, prompts):
        path = worker_prompt_path(directory, worker["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            runtime_prompt(prompt, directory, worker["id"]), encoding="utf-8"
        )


def spawn_pending_workers(directory: Path, workers: list[dict[str, Any]]) -> None:
    for index, worker in enumerate(workers, start=1):
        if cancel_requested(directory):
            return
        record = read_wave_state(directory)["workers"][index - 1]
        if record.get("terminal_handle") and record.get("start_status") == "running":
            continue
        if record.get("start_status") != "pending":
            raise HelperError(
                f"worker {index} is {record.get('start_status')}; "
                "refusing an ambiguous spawn retry"
            )
        spec = LAUNCH_SPECS[worker["launch"]]
        worktree = worker["worktree"]
        if worktree in {"new-child", "new-top-level"}:
            update_worker_state(directory, index, start_status="creating_worktree")
            returncode, receipt, detail = call_orca(
                [
                    "worktree",
                    "create",
                    "--name",
                    worker["name"],
                    "--setup",
                    worker.get("setup", "run"),
                    "--json",
                ]
            )
            save_json(
                directory / "worktrees" / f"{worker['id']}.json",
                receipt
                if receipt is not None
                else {"returncode": returncode, "detail": detail},
            )
            worktree_id = find_entity_id(receipt, "worktree") if receipt else None
            if returncode != 0 or not worktree_id:
                classification = classify_failure(receipt, known_effect_id=worktree_id)
                update_worker_state(
                    directory,
                    index,
                    worktree_id=worktree_id,
                    start_status=f"worktree_{classification}",
                    error=f"worktree create exited {returncode}: {detail}",
                )
                raise HelperError(
                    f"worktree create for worker {index} {classification}: "
                    f"{detail}; receipts: {directory}"
                )
            update_worker_state(directory, index, worktree_id=worktree_id)
            selector = f"id:{worktree_id}"
        else:
            selector = worktree
        if cancel_requested(directory):
            return
        update_worker_state(directory, index, start_status="spawning")
        command = spawn_command(spec)
        returncode, receipt, detail = call_orca(
            [
                "terminal",
                "create",
                "--worktree",
                selector,
                "--title",
                record["label"],
                "--command",
                command,
                "--json",
            ]
        )
        save_json(
            directory / "terminals" / f"{worker['id']}.json",
            receipt
            if receipt is not None
            else {"returncode": returncode, "detail": detail},
        )
        handle = find_terminal_handle(receipt) if receipt else None
        if returncode != 0 or not handle:
            classification = classify_failure(receipt, known_effect_id=handle)
            update_worker_state(
                directory,
                index,
                terminal_handle=handle,
                start_status=f"terminal_{classification}",
                error=f"terminal create exited {returncode}: {detail}",
            )
            raise HelperError(
                f"terminal create for worker {index} {classification} at exact "
                f"{spec['model']} {spec['effort']}: {detail}; receipts: {directory}"
            )
        update_worker_state(
            directory,
            index,
            terminal_handle=handle,
            spawn_command=command,
            start_status="booting",
            error=None,
        )
        wait_code, wait_receipt, wait_detail = call_orca(
            [
                "terminal",
                "wait",
                "--terminal",
                handle,
                "--for",
                "tui-idle",
                "--timeout-ms",
                str(AGENT_BOOT_TIMEOUT_MS),
                "--json",
            ]
        )
        save_json(
            directory / "runtime" / f"boot-{worker['id']}.json",
            wait_receipt
            if wait_receipt is not None
            else {"returncode": wait_code, "detail": wait_detail},
        )
        if wait_code != 0:
            update_worker_state(
                directory, index, start_status="boot_failed", error=wait_detail
            )
            raise HelperError(
                f"worker {index} agent did not reach idle in "
                f"{AGENT_BOOT_TIMEOUT_MS} ms: {wait_detail}; receipts: {directory}"
            )
        # The boot banner is secondary launch evidence; the primary proof is the
        # spawn command this helper recorded itself.
        banner_proof = False
        read_code, read_receipt, _ = call_orca(
            ["terminal", "read", "--terminal", handle, "--limit", "60", "--json"]
        )
        if read_code == 0 and read_receipt is not None:
            save_json(directory / "runtime" / f"banner-{worker['id']}.json", read_receipt)
            banner_proof = spec["model"] in compact_json(read_receipt)
        pointer = (
            f"Read the file {worker_prompt_path(directory, worker['id'])} "
            "and do exactly what it says."
        )
        send_code, send_receipt, send_detail = call_orca(
            [
                "terminal",
                "send",
                "--terminal",
                handle,
                "--text",
                pointer,
                "--enter",
                "--json",
            ]
        )
        save_json(
            directory / "runtime" / f"prompt-send-{worker['id']}.json",
            send_receipt
            if send_receipt is not None
            else {"returncode": send_code, "detail": send_detail},
        )
        if send_code != 0:
            update_worker_state(
                directory,
                index,
                start_status="prompt_send_failed",
                banner_proof=banner_proof,
                error=send_detail,
            )
            raise HelperError(
                f"prompt delivery to worker {index} failed: {send_detail}; "
                f"receipts: {directory}"
            )
        update_worker_state(
            directory,
            index,
            start_status="running",
            banner_proof=banner_proof,
            error=None,
        )
        if cancel_requested(directory):
            return


def reconcile_stop_wave(directory: Path) -> dict[str, Any]:
    request_cancel(directory)
    errors: list[str] = []
    snapshot = read_wave_state(directory)
    for record in snapshot["workers"]:
        index = record["index"]
        handle = record.get("terminal_handle")
        if record.get("stop_status") == "stopped":
            continue
        if not handle:
            update_worker_state(
                directory,
                index,
                start_status="cancelled"
                if record.get("start_status") == "pending"
                else record.get("start_status"),
                stop_status="not_created",
            )
            continue
        returncode, receipt, detail = call_orca(
            ["terminal", "close", "--terminal", handle, "--json"]
        )
        save_json(
            directory / "runtime" / f"stop-{record['worker_id']}.json",
            receipt
            if receipt is not None
            else {"returncode": returncode, "detail": detail},
        )
        if returncode == 0:
            update_worker_state(directory, index, stop_status="stopped")
        else:
            errors.append(f"worker {index}: {detail}")
            update_worker_state(
                directory, index, stop_status="stop_failed", error=detail
            )
    latest = read_wave_state(directory)
    unresolved = [
        record["index"]
        for record in latest["workers"]
        if record.get("start_status") in AMBIGUOUS_START_STATUSES
        or record.get("stop_status") == "stop_failed"
    ]
    phase = "cancel_pending" if errors or unresolved else "cancelled"
    set_wave_phase(directory, phase, error="; ".join(errors) if errors else None)
    final = read_wave_state(directory)
    return {
        "status": phase,
        "unresolved": unresolved,
        "workers": wave_records(final),
        "receipts": str(directory),
    }


def continue_wave(directory: Path, *, resumed: bool) -> int:
    manifest = load_json(directory / "manifest.json")
    normalized_manifest, workers, prompts = validate_manifest(manifest)
    state = read_wave_state(directory)
    if state.get("manifest_sha256") != manifest_digest(normalized_manifest):
        raise HelperError(
            "journal manifest changed after dispatch; resume is forbidden"
        )
    if cancel_requested(directory) or state.get("cancel_requested"):
        raise HelperError("wave is cancelled; resume is forbidden")
    set_wave_phase(directory, "writing_prompts")
    write_prompts(directory, workers, prompts)
    set_wave_phase(directory, "spawning_workers")
    spawn_pending_workers(directory, workers)
    if cancel_requested(directory):
        print(compact_json(reconcile_stop_wave(directory)), flush=True)
        return 0
    set_wave_phase(directory, "running")
    final = read_wave_state(directory)
    print(
        compact_json(
            {
                "status": "resumed" if resumed else "dispatched",
                "workers": wave_records(final),
                "receipts": str(directory),
            }
        ),
        flush=True,
    )
    return 0


def initialize_wave_state(
    directory: Path, manifest: dict[str, Any], workers: list[dict[str, Any]]
) -> None:
    now = time.time()
    save_json(
        directory / STATE_FILE,
        {
            "version": STATE_VERSION,
            "manifest_sha256": manifest_digest(manifest),
            "helper_sha256": helper_digest(),
            "mode": manifest["mode"],
            "objective": manifest["objective"],
            "launch_specs": LAUNCH_SPECS,
            "controller_terminal_handle": require_string(
                os.environ.get("ORCA_TERMINAL_HANDLE"), "ORCA_TERMINAL_HANDLE"
            ),
            "phase": "initialized",
            "cancel_requested": False,
            "created_at": now,
            "updated_at": now,
            "workers": [
                {
                    "index": index,
                    "worker_id": worker["id"],
                    "role": worker["role"],
                    "launch": worker["launch"],
                    "mutation": worker["mutation"],
                    "criteria": worker["criteria"],
                    "label": worker_label(worker, index),
                    "worktree_id": None,
                    "terminal_handle": None,
                    "spawn_command": None,
                    "banner_proof": None,
                    "start_status": "pending",
                    "report_sha": None,
                    "report_status": "pending",
                    "task_status": None,
                    "verdict": None,
                    "question": None,
                    "answers": 0,
                    "notification_status": "pending",
                    "stop_status": None,
                    "error": None,
                }
                for index, worker in enumerate(workers, start=1)
            ],
        },
    )


def command_dispatch_wave(args: argparse.Namespace) -> int:
    manifest, workers, prompts = validate_manifest(load_json(args.manifest))
    if args.dry_run:
        print(
            compact_json(
                {
                    "status": "valid",
                    "schemaVersion": MANIFEST_VERSION,
                    "mode": manifest["mode"],
                    "workers": [
                        {
                            "index": index,
                            "role": worker["role"],
                            "launch": worker["launch"],
                            "label": worker_label(worker, index),
                            "prompt_chars": len(prompt),
                            "overBudget": len(prompt) > PROMPT_BUDGET_CHARS,
                        }
                        for index, (worker, prompt) in enumerate(
                            zip(workers, prompts), start=1
                        )
                    ],
                }
            )
        )
        return 0

    directory = receipt_dir(args.receipt_dir)
    if (directory / STATE_FILE).exists():
        raise HelperError(f"wave state already exists; use resume-wave: {directory}")
    ensure_journal(directory)
    verify_preflight(directory, manifest, workers, prompts)
    save_json(directory / "manifest.json", manifest)
    initialize_wave_state(directory, manifest, workers)
    print(
        compact_json(
            {
                "status": "starting",
                "launches": {
                    alias: sum(1 for worker in workers if worker["launch"] == alias)
                    for alias in sorted({worker["launch"] for worker in workers})
                },
                "count": len(workers),
                "receipts": str(directory),
            }
        ),
        flush=True,
    )
    try:
        return continue_wave(directory, resumed=False)
    except KeyboardInterrupt:
        print(compact_json(reconcile_stop_wave(directory)), flush=True)
        return 130
    except HelperError as exc:
        if cancel_requested(directory):
            print(compact_json(reconcile_stop_wave(directory)), flush=True)
            return 0
        set_wave_phase(directory, "error", error=str(exc))
        raise


def command_resume_wave(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    ambiguous = [
        record["index"]
        for record in state["workers"]
        if record.get("start_status") in AMBIGUOUS_START_STATUSES
    ]
    if ambiguous:
        raise HelperError(
            f"cannot safely resume ambiguous worker indices {ambiguous}; "
            "inspect receipts and stop the wave"
        )
    try:
        return continue_wave(directory, resumed=True)
    except KeyboardInterrupt:
        print(compact_json(reconcile_stop_wave(directory)), flush=True)
        return 130


def command_stop_wave(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    print(compact_json(reconcile_stop_wave(directory)), flush=True)
    return 0


MAX_REPORT_FILE_BYTES = 262_144


def read_incoming_report(path: Path) -> tuple[Any, str | None]:
    try:
        if path.stat().st_size > MAX_REPORT_FILE_BYTES:
            return None, f"report file exceeds {MAX_REPORT_FILE_BYTES} bytes"
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, str(exc)
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def validate_report(report: Any, role: str) -> list[str]:
    if not isinstance(report, dict):
        return ["missing structured report payload"]
    errors: list[str] = []
    if report.get("reportSchemaVersion") != REPORT_VERSION:
        errors.append(f"reportSchemaVersion must be {REPORT_VERSION}")
    if report.get("taskStatus") not in {"done", "failed", "blocked"}:
        errors.append("taskStatus must be done, failed, or blocked")
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
    for field in ("evidence", "risks", "checks", "filesModified"):
        value = report.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            errors.append(f"{field} must be an array of strings")
    findings = report.get("findings")
    if not isinstance(findings, list) or not all(
        isinstance(item, dict) for item in findings
    ):
        errors.append("findings must be an array of objects")
    elif any(
        item.get("severity") not in {"critical", "high", "medium", "low"}
        or not isinstance(item.get("title"), str)
        or not isinstance(item.get("evidence"), str)
        for item in findings
    ):
        errors.append("each finding requires severity, title, and evidence")
    for field in ROLE_REPORT_FIELDS.get(role, {}):
        if field not in report:
            errors.append(f"missing role field: {field}")
    if role in REVIEW_ROLES and report.get("verdict") not in {
        "PASS",
        "FAIL",
        "UNKNOWN",
        "BLOCKED",
    }:
        errors.append("verdict must be PASS, FAIL, UNKNOWN, or BLOCKED")
    if (
        role in REVIEW_ROLES
        and report.get("taskStatus") == "blocked"
        and report.get("verdict") != "BLOCKED"
    ):
        errors.append("blocked reviews must use verdict BLOCKED")
    if role == "scout" and not isinstance(report.get("shards"), list):
        errors.append("shards must be an array")
    if role in {"implementer", "fixer"} and not (
        report.get("commit") is None or isinstance(report.get("commit"), str)
    ):
        errors.append("commit must be a string or null")
    if role == "integrator":
        for field in ("integrated", "conflicts"):
            value = report.get(field)
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                errors.append(f"{field} must be an array of strings")
        if not isinstance(report.get("anchor"), str):
            errors.append("anchor must be a string")
    if role in REVIEW_ROLES and not isinstance(report.get("criteria"), dict):
        errors.append("criteria must be an object")
    if role == "antislop":
        if not isinstance(report.get("cuts"), list):
            errors.append("cuts must be an array")
        if (
            not isinstance(report.get("leanness"), int)
            or not 1 <= report["leanness"] <= 10
        ):
            errors.append("leanness must be an integer from 1 to 10")
    if role == "fixer" and not isinstance(report.get("fixed"), list):
        errors.append("fixed must be an array")
    question = report.get("question")
    if question is not None and (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > 500
    ):
        errors.append("question must be a non-empty string of at most 500 characters")
    if report.get("taskStatus") == "blocked" and not question:
        errors.append("a blocked report needs a question")
    prompt_feedback = report.get("promptFeedback")
    if prompt_feedback is not None:
        if not isinstance(prompt_feedback, list) or len(prompt_feedback) > 3:
            errors.append("promptFeedback must be an array of at most 3 entries")
        else:
            for entry in prompt_feedback:
                if not isinstance(entry, dict):
                    errors.append("each promptFeedback entry must be an object")
                    break
                rule = entry.get("rule")
                if (
                    not isinstance(entry.get("failureClass"), str)
                    or not entry["failureClass"].strip()
                    or not isinstance(rule, str)
                    or not rule.strip()
                    or len(rule) > 200
                    or entry.get("severity")
                    not in {"critical", "high", "medium", "low"}
                    or not isinstance(entry.get("scopes"), list)
                    or not entry["scopes"]
                    or not all(
                        isinstance(scope, str) and scope.strip()
                        for scope in entry["scopes"]
                    )
                    or entry.get("gap", "prompt")
                    not in {"prompt", "decomposition", "judgment", "test", "tooling"}
                ):
                    errors.append(
                        "each promptFeedback entry needs failureClass, a rule of at "
                        "most 200 chars, severity, non-empty scopes, and a valid gap"
                    )
                    break
    rule_feedback = report.get("ruleFeedback")
    if rule_feedback is not None:
        if not isinstance(rule_feedback, list) or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and entry.get("status") in {"violated", "helped", "retire"}
            for entry in rule_feedback
        ):
            errors.append(
                "each ruleFeedback entry needs id and status violated, helped, or retire"
            )
    return errors


def scan_incoming_reports(directory: Path) -> tuple[list[dict[str, Any]], int]:
    """Process new or changed report files; return (messages, actionable count)."""
    state = read_wave_state(directory)
    messages: list[dict[str, Any]] = []
    actions = 0
    for record in state["workers"]:
        worker_id = record["worker_id"]
        index = record["index"]
        path = worker_report_path(directory, worker_id)
        if not path.exists():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            messages.append({"workerId": worker_id, "error": str(exc)})
            actions += 1
            continue
        if digest == record.get("report_sha"):
            continue
        report, read_error = read_incoming_report(path)
        message: dict[str, Any] = {"workerId": worker_id, "reportFile": str(path)}
        if read_error or not isinstance(report, dict):
            update_worker_state(
                directory,
                index,
                report_sha=digest,
                report_status="invalid",
                error=read_error or "report is not a JSON object",
            )
            message.update(
                {
                    "reportStatus": "invalid",
                    "error": read_error or "report is not a JSON object",
                }
            )
            actions += 1
            messages.append(message)
            continue
        settled_before = (
            record.get("task_status") in {"done", "failed"}
            and record.get("report_status") == "valid"
        )
        if settled_before:
            # A finished worker's file changed. Never replace the accepted
            # report; surface the change for Sol instead.
            update_worker_state(directory, index, report_sha=digest)
            message.update({"changedAfterDone": True})
            actions += 1
            messages.append(message)
            continue
        errors = validate_report(report, record.get("role", ""))
        raw_task_status = report.get("taskStatus")
        task_status = raw_task_status if isinstance(raw_task_status, str) else None
        raw_verdict = report.get("verdict")
        verdict = raw_verdict if isinstance(raw_verdict, str) else None
        raw_question = report.get("question")
        question = raw_question if isinstance(raw_question, str) else None
        raw_summary = report.get("summary")
        summary = raw_summary if isinstance(raw_summary, str) else ""
        stored = directory / "reports" / f"{worker_id}.json"
        save_json(
            stored,
            {
                "accepted": not errors,
                "validationErrors": errors,
                "report": report,
            },
        )
        update_worker_state(
            directory,
            index,
            report_sha=digest,
            report_status="valid" if not errors else "invalid",
            task_status=task_status,
            verdict=verdict,
            question=question,
            start_status="completed"
            if task_status in {"done", "failed"}
            else ("blocked" if task_status == "blocked" else record.get("start_status")),
            error=None,
        )
        message.update(
            {
                "taskStatus": task_status,
                "verdict": verdict,
                "question": question,
                "summary": summary[:MAX_BODY_OUTPUT_CHARS],
                "truncated": len(summary) > MAX_BODY_OUTPUT_CHARS,
                "reportErrors": errors,
                "report": str(stored),
            }
        )
        if (
            errors
            or task_status in {"blocked", "failed"}
            or question
            or verdict in {"FAIL", "UNKNOWN", "BLOCKED"}
        ):
            actions += 1
        messages.append({k: v for k, v in message.items() if v is not None})
    return messages, actions


def command_collect_reports(args: argparse.Namespace) -> int:
    """Read every new or changed report file; never wait for future ones."""
    directory = receipt_dir(args.receipt_dir)
    read_wave_state(directory)
    messages, actions = scan_incoming_reports(directory)
    # Close the publication race: a worker may have written its file after the
    # scan while the wake marker still existed.
    clear_notification(directory)
    late_messages, late_actions = scan_incoming_reports(directory)
    messages.extend(late_messages)
    actions += late_actions
    latest = read_wave_state(directory)
    settled = wave_settled(latest)
    status = (
        "wave_settled"
        if settled
        else ("action_required" if actions else "idle_push_mode")
    )
    print(
        compact_json(
            {
                "status": status,
                "messages": messages,
                "workers": wave_records(latest),
                "next": (
                    "run finalize-wave"
                    if settled
                    else (
                        "handle each actionable message (answer questions with "
                        "the answer command), then return to idle"
                        if actions
                        else "return to idle until the next wake"
                    )
                ),
                "receipts": str(directory),
            }
        )
    )
    return 0


def command_answer(args: argparse.Namespace) -> int:
    """Send Sol's answer into the blocked worker's own terminal session."""
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    record = next(
        (
            worker
            for worker in state["workers"]
            if worker["worker_id"] == args.worker
        ),
        None,
    )
    if record is None:
        raise HelperError(f"no worker {args.worker!r} in this wave")
    handle = record.get("terminal_handle")
    if not handle:
        raise HelperError(f"worker {args.worker} has no terminal")
    answer_file = Path(args.file).expanduser()
    if not answer_file.is_absolute() or not answer_file.exists():
        raise HelperError("answer --file must be an existing absolute path")
    pointer = (
        f"Your question was answered. Read the file {answer_file} and continue "
        "the same task. When finished, write the final report to your report "
        "file and run the wake command again."
    )
    returncode, receipt, detail = call_orca(
        [
            "terminal",
            "send",
            "--terminal",
            handle,
            "--text",
            pointer,
            "--enter",
            "--json",
        ]
    )
    save_json(
        directory / "answers" / f"{time.time_ns()}-{record['worker_id']}.json",
        receipt
        if receipt is not None
        else {"returncode": returncode, "detail": detail},
    )
    if returncode != 0:
        raise HelperError(f"answer delivery failed: {detail}")
    update_worker_state(
        directory,
        record["index"],
        start_status="running",
        task_status=None,
        question=None,
        answers=record.get("answers", 0) + 1,
        notification_status="pending",
    )
    print(
        compact_json(
            {
                "status": "answered",
                "workerId": record["worker_id"],
                "next": "return to idle; the worker wakes you with its final report",
            }
        )
    )
    return 0


def wave_settled(state: dict[str, Any]) -> bool:
    workers = state.get("workers", [])
    return bool(workers) and all(
        worker.get("task_status") in {"done", "failed"}
        and worker.get("report_status") == "valid"
        for worker in workers
    )


def notification_path(directory: Path) -> Path:
    return directory / NOTIFICATION_FILE


def clear_notification(directory: Path) -> bool:
    try:
        notification_path(directory).unlink()
    except FileNotFoundError:
        return False
    return True


def claim_controller_notification(
    directory: Path, worker: dict[str, Any], controller_handle: str
) -> bool:
    path = notification_path(directory)
    payload = (
        compact_json(
            {
                "workerId": worker["worker_id"],
                "workerTerminalHandle": worker.get("terminal_handle"),
                "controllerTerminalHandle": controller_handle,
                "createdAt": time.time(),
            }
        )
        + "\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def command_notify_controller(args: argparse.Namespace) -> int:
    """Worker-only, once per report: queue one coalesced wake prompt for Sol."""
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    caller = require_string(
        os.environ.get("ORCA_TERMINAL_HANDLE"), "ORCA_TERMINAL_HANDLE"
    )
    record = next(
        (
            worker
            for worker in state.get("workers", [])
            if worker.get("terminal_handle") == caller
        ),
        None,
    )
    if record is None:
        raise HelperError(
            "notify-controller caller is not a current worker terminal in this wave"
        )
    report_file = worker_report_path(directory, record["worker_id"])
    if not report_file.exists():
        raise HelperError(
            f"write your report to {report_file} before running notify-controller"
        )
    if record.get("notification_status") in {"queued", "coalesced"}:
        print(
            compact_json(
                {
                    "status": "already_notified",
                    "workerId": record["worker_id"],
                    "next": "stop; never send a duplicate wake",
                }
            )
        )
        return 0

    controller_handle = require_string(
        state.get("controller_terminal_handle"), "wave.controller_terminal_handle"
    )
    if not claim_controller_notification(directory, record, controller_handle):
        update_worker_state(directory, record["index"], notification_status="coalesced")
        print(
            compact_json(
                {
                    "status": "coalesced",
                    "workerId": record["worker_id"],
                    "next": "stop; another worker already queued the controller wake",
                }
            )
        )
        return 0

    collect_command = shlex.join(
        [
            "uv",
            "run",
            "--no-project",
            str(Path(__file__).resolve()),
            "collect-reports",
            "--receipt-dir",
            str(directory),
        ]
    )
    text = (
        "[ORCA LUNA CYCLE: REPORTS READY]\n"
        "A worker wrote its report file. Run this once, then follow its "
        "next field:\n"
        f"{collect_command}"
    )
    returncode, receipt, detail = call_orca(
        [
            "terminal",
            "send",
            "--terminal",
            controller_handle,
            "--text",
            text,
            "--enter",
            "--json",
        ]
    )
    notification_receipt = (
        directory / "notifications" / f"{time.time_ns()}-{record['worker_id']}.json"
    )
    save_json(
        notification_receipt,
        receipt
        if receipt is not None
        else {"returncode": returncode, "detail": detail},
    )
    if returncode == 0:
        status = "queued"
    else:
        status = classify_failure(receipt)
        # A retained marker after a failed or ambiguous send would coalesce every
        # later wake into a wake that may never arrive and silently stall the
        # wave; a duplicate wake is harmless because collect is idempotent.
        clear_notification(directory)
    update_worker_state(directory, record["index"], notification_status=status)
    print(
        compact_json(
            {
                "status": status,
                "workerId": record["worker_id"],
                "controllerTerminalHandle": controller_handle,
                "receipt": str(notification_receipt),
                "next": "stop; the wake attempt is exactly-once and must not be retried",
                **({} if returncode == 0 else {"error": detail}),
            }
        )
    )
    return 0


def exact_launch_proven(record: dict[str, Any]) -> bool:
    """The helper spawned the agent itself, so the recorded spawn command is
    the primary launch proof; the boot banner is secondary evidence only."""
    spec = LAUNCH_SPECS.get(record.get("launch"))
    if spec is None:
        return False
    return record.get("spawn_command") == spawn_command(spec)


def git_snapshot() -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--short"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {"status": "error", "error": str(exc)}
    if head.returncode != 0 or status.returncode != 0:
        return {
            "status": "error",
            "error": (
                head.stderr or status.stderr or head.stdout or status.stdout
            ).strip(),
        }
    return {
        "status": "captured",
        "head": head.stdout.strip(),
        "dirtyState": [line for line in status.stdout.splitlines() if line],
    }


def anchor_check(manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    workers = state.get("workers", [])
    helper_can_verify = (
        bool(workers)
        and all(worker.get("mutation") == "forbidden" for worker in workers)
        and all(
            normalized.get("worktree") == "current"
            for normalized in manifest.get("workers", [])
        )
    )
    if not helper_can_verify:
        return {
            "status": "controller_required",
            "passed": True,
            "reason": "mutating or non-current worktrees require Sol to verify the integration anchor",
        }
    snapshot = git_snapshot()
    if snapshot.get("status") != "captured":
        return {**snapshot, "passed": False}
    envelope = manifest["envelope"]
    expected_head = envelope.get("baseAnchor")
    expected_dirty = envelope.get("dirtyState", [])
    head_ok = expected_head in (None, "") or snapshot.get("head") == expected_head
    dirty_ok = snapshot.get("dirtyState") == expected_dirty
    return {
        **snapshot,
        "status": "verified" if head_ok and dirty_ok else "changed",
        "passed": head_ok and dirty_ok,
        "expectedHead": expected_head,
        "expectedDirtyState": expected_dirty,
    }


def command_finalize_wave(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    manifest, _, _ = validate_manifest(load_json(directory / "manifest.json"))
    state = read_wave_state(directory)
    if state.get("manifest_sha256") != manifest_digest(manifest):
        raise HelperError(
            "journal manifest changed after dispatch; finalization is unsafe"
        )
    workers = state.get("workers", [])
    unresolved = [
        worker["worker_id"]
        for worker in workers
        if worker.get("start_status") in AMBIGUOUS_START_STATUSES
    ]
    checks = {
        "preflightPassed": load_json(directory / "preflight.json").get("status")
        == "passed",
        "allWorkersSpawned": all(worker.get("terminal_handle") for worker in workers),
        "allSettled": all(
            worker.get("task_status") in {"done", "failed"} for worker in workers
        ),
        "allReportsValid": all(
            worker.get("report_status") == "valid" for worker in workers
        ),
        "noOpenQuestions": all(not worker.get("question") for worker in workers),
        "noPendingControllerWake": not notification_path(directory).exists(),
        "exactLaunchProven": all(exact_launch_proven(worker) for worker in workers),
        "noAmbiguousEffects": not unresolved,
    }
    anchor = anchor_check(manifest, state)
    checks["anchorPreservedOrDelegated"] = anchor.get("passed") is True
    created_worktrees = [
        {
            "workerId": record.get("worker_id"),
            "worktreeId": record.get("worktree_id"),
            "selector": spec.get("worktree"),
        }
        for record, spec in zip(workers, manifest.get("workers", []))
        if spec.get("worktree") in {"new-child", "new-top-level"}
    ]
    mechanical_ok = all(checks.values())
    set_wave_phase(directory, "finalized" if mechanical_ok else "finalize_incomplete")
    state = read_wave_state(directory)
    final = {
        "finalSchemaVersion": 2,
        "runId": directory.name,
        "mode": manifest["mode"],
        "launchSpecs": LAUNCH_SPECS,
        "orchestrationHealth": "PASS" if mechanical_ok else "FAIL",
        "mechanicalStatus": "ready_for_sol_gate" if mechanical_ok else "incomplete",
        "checks": checks,
        "unresolved": unresolved,
        "anchor": anchor,
        "contentVerdicts": [
            {
                "workerId": worker["worker_id"],
                "role": worker["role"],
                "verdict": worker.get("verdict"),
            }
            for worker in workers
            if worker.get("verdict") is not None
        ],
        "workers": wave_records(state),
        "createdWorktrees": created_worktrees,
        "note": "Content verdicts remain inputs to the Sol gate; audit FAIL does not mean orchestration failed. Created worktrees must be integrated and removed before the next wave.",
        "createdAt": time.time(),
    }
    save_json(directory / "final.json", final)
    print(
        compact_json(
            {
                "orchestrationHealth": final["orchestrationHealth"],
                "mechanicalStatus": final["mechanicalStatus"],
                "checks": checks,
                "unresolved": unresolved,
                "contentVerdicts": final["contentVerdicts"],
                "createdWorktrees": created_worktrees,
                "finalReceipt": str(directory / "final.json"),
            }
        )
    )
    return 0 if mechanical_ok else 2


def command_self_test(_: argparse.Namespace) -> int:
    assert LAUNCH_SPECS["luna-max"] == {
        "agent": "codex",
        "model": "gpt-5.6-luna",
        "effort": "max",
    }
    assert LAUNCH_SPECS["sol-xhigh"] == {
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
    }
    assert LAUNCH_SPECS["fable-high"] == {
        "agent": "claude",
        "model": "claude-fable-5",
        "effort": "high",
    }
    assert LAUNCH_SPECS["luna-fast"] == {
        "agent": "codex",
        "model": "gpt-5.6-luna",
        "effort": "max",
        "speedTier": "fast",
    }
    assert all("[" not in spec["model"] for spec in LAUNCH_SPECS.values())
    assert ROLE_LAUNCHES["reviewer"] == ("sol-xhigh",)
    assert ROLE_LAUNCHES["antislop"] == ("sol-xhigh",)
    assert "luna-fast" in ROLE_LAUNCHES["implementer"]
    assert all(launches[0] != "luna-fast" for launches in ROLE_LAUNCHES.values())
    assert not any(name.startswith("orchestration") for name in REQUIRED_ORCA_COMMANDS)
    assert REQUIRED_ORCA_COMMANDS["terminal create"] == {
        "worktree",
        "title",
        "command",
        "json",
    }
    assert (
        spawn_command(LAUNCH_SPECS["luna-max"])
        == "codex -m gpt-5.6-luna -c model_reasoning_effort=max"
    )
    assert '"' not in spawn_command(LAUNCH_SPECS["luna-fast"])
    manifest, workers, prompts = validate_manifest(
        load_json(REFERENCES / "manifest-v2.example.json")
    )
    renormalized, _, _ = validate_manifest(manifest)
    assert manifest_digest(manifest) == manifest_digest(renormalized)
    assert manifest["schemaVersion"] == MANIFEST_VERSION
    assert len(workers) == len(prompts) == 1
    assert all("reportSchemaVersion" in prompt for prompt in prompts)
    assert all(len(prompt) < 5_000 for prompt in prompts)
    assert worker_label(workers[0], 1).startswith("01 scout · ")
    assert workers[0]["launch"] == "luna-max"
    fable_manifest = {
        **manifest,
        "workers": [{**manifest["workers"][0], "launch": "fable-high"}],
    }
    _, fable_workers, _ = validate_manifest(fable_manifest)
    assert (
        spawn_command(LAUNCH_SPECS[fable_workers[0]["launch"]])
        == "claude --model claude-fable-5"
    )
    try:
        validate_manifest(
            {
                **manifest,
                "workers": [{**manifest["workers"][0], "launch": "sol-xhigh"}],
            }
        )
    except HelperError as exc:
        assert "launch" in str(exc)
    else:
        raise AssertionError("scout launch sol-xhigh was accepted")
    mutating_manifest = {
        **manifest,
        "mode": "implementation",
        "defaults": {**manifest["defaults"], "mutation": "allowed"},
        "workers": [
            {
                **manifest["workers"][0],
                "id": "impl",
                "role": "implementer",
                "launch": "luna-fast",
                "mutation": "allowed",
            }
        ],
    }
    try:
        validate_manifest(mutating_manifest)
    except HelperError as exc:
        assert "reviewedAnchor" in str(exc)
    else:
        raise AssertionError("silent mutation on an unreviewed anchor was accepted")
    review_only_implementation = {
        **manifest,
        "mode": "implementation",
        "workers": [
            {
                key: value
                for key, value in {
                    **manifest["workers"][0],
                    "id": "rev",
                    "role": "reviewer",
                }.items()
                if key != "launch"
            }
        ],
    }
    try:
        validate_manifest(review_only_implementation)
    except HelperError as exc:
        assert "audit" in str(exc)
    else:
        raise AssertionError("reviewer-only implementation wave was accepted")
    overridden = {
        **mutating_manifest,
        "envelope": {
            **mutating_manifest["envelope"],
            "reviewOverride": "bootstrap wave; no prior review exists",
        },
    }
    _, overridden_workers, _ = validate_manifest(overridden)
    assert overridden_workers[0]["launch"] == "luna-fast"
    fast_command = spawn_command(LAUNCH_SPECS[overridden_workers[0]["launch"]])
    assert "gpt-5.6-luna" in fast_command
    assert "model_reasoning_effort=max" in fast_command
    assert "service_tier=priority" in fast_command
    orphan_worktree = {
        **overridden,
        "workers": [
            {
                **overridden["workers"][0],
                "worktree": "new-child",
                "name": "impl-shard",
            }
        ],
    }
    try:
        validate_manifest(orphan_worktree)
    except HelperError as exc:
        assert "integrator" in str(exc)
    else:
        raise AssertionError("new-worktree mutator without integrator was accepted")
    paired = {
        **orphan_worktree,
        "workers": [
            *orphan_worktree["workers"],
            {
                "id": "merge",
                "role": "integrator",
                "goal": "Integrate the shard exactly once.",
                "criteria": orphan_worktree["workers"][0]["criteria"],
                "mutation": "allowed",
            },
        ],
    }
    _, paired_workers, _ = validate_manifest(paired)
    assert [worker["role"] for worker in paired_workers] == [
        "implementer",
        "integrator",
    ]
    both_declared = {
        **overridden,
        "envelope": {
            **overridden["envelope"],
            "reviewedAnchor": overridden["envelope"]["baseAnchor"],
        },
    }
    try:
        validate_manifest(both_declared)
    except HelperError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("reviewedAnchor plus reviewOverride was accepted")
    ten_manifest = {
        **manifest,
        "workers": [
            {
                **manifest["workers"][0],
                "id": f"reader-{index}",
                "displayName": f"Read-only lens {index}",
            }
            for index in range(1, 11)
        ],
    }
    _, ten_workers, ten_prompts = validate_manifest(ten_manifest)
    assert len(ten_workers) == len(ten_prompts) == MAX_WORKERS
    bad_manifest = {
        **manifest,
        "workers": [{**manifest["workers"][0], "criteria": ["AC999"]}],
    }
    try:
        validate_manifest(bad_manifest)
    except HelperError as exc:
        assert "unknown criteria" in str(exc)
    else:
        raise AssertionError("unknown AC reference was accepted")
    assert find_entity_id({"task": {"id": "task_1"}}, "task") == "task_1"
    assert find_entity_id({"dispatchId": "dispatch_1"}, "dispatch") == "dispatch_1"
    assert (
        find_terminal_handle({"worker": {"agentTerminalHandle": "term_1"}}) == "term_1"
    )
    assert (
        find_entity_id({"worker": {"worktree": {"id": "repo::/tmp/w"}}}, "worktree")
        == "repo::/tmp/w"
    )
    assert find_terminal_handle({"effects": [{"id": "term_2"}]}) == "term_2"
    valid_report = report_example("reviewer")
    valid_report.update(
        {
            "taskStatus": "done",
            "summary": "Reviewed the bounded surface.",
            "verdict": "PASS",
        }
    )
    assert validate_report(valid_report, "reviewer") == []
    learned_report = dict(valid_report)
    learned_report["promptFeedback"] = [
        {
            "failureClass": "producer-proxy-consumer contract divergence",
            "rule": "Trace producer -> proxy -> consumer for each URL and use one shared fixture.",
            "severity": "high",
            "scopes": ["publication", "routing"],
        }
    ]
    learned_report["ruleFeedback"] = [{"id": "per-object-fsync", "status": "helped"}]
    assert validate_report(learned_report, "reviewer") == []
    bad_learned = dict(valid_report)
    bad_learned["promptFeedback"] = [{"failureClass": "x", "rule": "y" * 300}]
    assert validate_report(bad_learned, "reviewer") != []
    rules_manifest = {
        **manifest,
        "envelope": {
            **manifest["envelope"],
            "knownFailureModes": [
                "[producer-proxy] Trace producer -> proxy -> consumer for each URL."
            ],
        },
    }
    _, _, rules_prompts = validate_manifest(rules_manifest)
    assert "KNOWN FAILURE MODES RELEVANT TO THIS SCOPE" in rules_prompts[0]
    assert "[producer-proxy]" in rules_prompts[0]
    reviewer_variant = {
        key: value
        for key, value in {
            **manifest["workers"][0],
            "id": "rev",
            "role": "reviewer",
        }.items()
        if key != "launch"
    }
    _, _, charter_prompts = validate_manifest(
        {**manifest, "workers": [reviewer_variant]}
    )
    assert "optimize for true positives" in charter_prompts[0]
    assert "handles null, empty, error" in charter_prompts[0]
    antislop_variant = {**reviewer_variant, "id": "slop", "role": "antislop"}
    _, _, antislop_prompts = validate_manifest(
        {**manifest, "workers": [antislop_variant]}
    )
    assert "Library reinvention" in antislop_prompts[0]
    assert "Backward-compat hoarding" in antislop_prompts[0]
    assert "Do not invent problems" in antislop_prompts[0]
    try:
        validate_manifest(
            {
                **manifest,
                "envelope": {
                    **manifest["envelope"],
                    "knownFailureModes": ["r" * 220 for _ in range(5)],
                },
            }
        )
    except HelperError as exc:
        assert "1000" in str(exc)
    else:
        raise AssertionError("oversized knownFailureModes was accepted")
    assert (
        classify_failure({"error": {"code": "invalid_argument"}})
        == "rejected_no_effects"
    )
    with tempfile.TemporaryDirectory(prefix="orca-luna-self-test-") as temporary:
        directory = Path(temporary)
        ensure_journal(directory)
        initialize_wave_state(directory, manifest, workers)
        update_worker_state(
            directory, 1, terminal_handle="term_1", start_status="running"
        )
        assert read_wave_state(directory)["workers"][0]["terminal_handle"] == "term_1"
        live = runtime_prompt(prompts[0], directory, workers[0]["id"])
        assert str(worker_report_path(directory, workers[0]["id"])) in live
        assert "notify-controller" in live
        update_worker_state(
            directory, 1, terminal_handle=None, start_status="pending"
        )
        request_cancel(directory)
        assert cancel_requested(directory)
        assert read_wave_state(directory)["cancel_requested"] is True
    print(
        compact_json(
            {
                "status": "ok",
                "promptChars": [len(prompt) for prompt in prompts],
                "max": max(len(prompt) for prompt in prompts),
            }
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    schema = commands.add_parser("schema", help="print JSON schemas and example")
    schema.add_argument(
        "--kind", choices=("all", "manifest", "report", "example"), default="all"
    )
    schema.set_defaults(func=command_schema)

    prompt = commands.add_parser("prompt", help="render one worker prompt without Orca")
    prompt.add_argument("--manifest", type=Path, required=True)
    prompt.add_argument("--worker", help="worker ID; defaults to the first worker")
    prompt.set_defaults(func=command_prompt)

    preflight = commands.add_parser(
        "preflight", help="validate runtime/contract/model/worktrees without mutations"
    )
    preflight.add_argument("--manifest", type=Path, required=True)
    preflight.add_argument("--receipt-dir", required=True)
    preflight.set_defaults(func=command_preflight)

    dispatch = commands.add_parser(
        "dispatch-wave", help="write prompts and spawn all worker terminals"
    )
    dispatch.add_argument("--manifest", type=Path, required=True)
    dispatch.add_argument("--receipt-dir", required=False)
    dispatch.add_argument("--dry-run", action="store_true")
    dispatch.set_defaults(func=command_dispatch_wave)

    resume = commands.add_parser(
        "resume-wave", help="continue a non-ambiguous wave from its durable state"
    )
    resume.add_argument("--receipt-dir", required=True)
    resume.set_defaults(func=command_resume_wave)

    stop = commands.add_parser(
        "stop-wave", help="cancel starts and stop all known workers"
    )
    stop.add_argument("--receipt-dir", required=True)
    stop.set_defaults(func=command_stop_wave)

    collect = commands.add_parser(
        "collect-reports",
        help="read new or changed worker report files; never waits",
    )
    collect.add_argument("--receipt-dir", required=True)
    collect.set_defaults(func=command_collect_reports)

    answer = commands.add_parser(
        "answer", help="send Sol's answer into a blocked worker's terminal"
    )
    answer.add_argument("--receipt-dir", required=True)
    answer.add_argument("--worker", required=True, help="worker ID from the manifest")
    answer.add_argument(
        "--file", required=True, help="absolute path to a file with the answer text"
    )
    answer.set_defaults(func=command_answer)

    notify = commands.add_parser(
        "notify-controller",
        help="worker-only exactly-once wake after writing the report file",
    )
    notify.add_argument("--receipt-dir", required=True)
    notify.set_defaults(func=command_notify_controller)

    finalize = commands.add_parser(
        "finalize-wave",
        help="reconcile the durable journal and emit the Sol gate receipt",
    )
    finalize.add_argument("--receipt-dir", required=True)
    finalize.set_defaults(func=command_finalize_wave)

    self_test = commands.add_parser(
        "self-test", help="run offline renderer/parser tests"
    )
    self_test.set_defaults(func=command_self_test)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except HelperError as exc:
        print(compact_json({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
