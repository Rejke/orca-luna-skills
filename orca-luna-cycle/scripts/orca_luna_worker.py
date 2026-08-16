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
    "terra-xhigh": {"agent": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
    "sol-xhigh": {"agent": "codex", "model": "gpt-5.6-sol", "effort": "xhigh"},
    "fable-high": {"agent": "claude", "model": "claude-fable-5", "effort": "high"},
}
# First alias is the role default; review roles are pinned and reject overrides.
ROLE_LAUNCHES = {
    "scout": ("luna-max", "terra-xhigh", "fable-high"),
    "implementer": ("luna-max", "terra-xhigh", "fable-high"),
    "integrator": ("luna-max", "terra-xhigh", "fable-high"),
    "fixer": ("luna-max", "terra-xhigh", "fable-high"),
    "reviewer": ("sol-xhigh",),
    "antislop": ("sol-xhigh",),
}
MAX_WORKERS = 10
MAX_PROMPT_CHARS = 16_000
MAX_BODY_OUTPUT_CHARS = 3_000
MESSAGE_TYPES = {"worker_done", "question", "escalation", "heartbeat", "status"}
STATE_FILE = "wave-state.json"
CANCEL_FILE = "cancel.requested.json"
NOTIFICATION_FILE = "controller-notification.pending.json"
STATE_VERSION = 3
MANIFEST_VERSION = 2
REPORT_VERSION = 1
MIN_ORCA_VERSION = "1.4.184-fix.1"
SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES = SKILL_ROOT / "references"
MODES = {"implementation", "audit", "benchmark"}
MUTATOR_ROLES = {"implementer", "integrator", "fixer"}
REVIEW_ROLES = {"reviewer", "antislop"}
REQUIRED_ORCA_COMMANDS = {
    "agent-context": {"json"},
    "worktree current": {"json"},
    "worktree show": {"worktree", "json"},
    "terminal rename": {"terminal", "title", "json"},
    "terminal send": {"terminal", "text", "enter", "json"},
    "orchestration run-create": {"objective", "json"},
    "orchestration run-show": {"id", "json"},
    "orchestration run-use": {"id", "json"},
    "orchestration task-create": {
        "spec",
        "task-title",
        "display-name",
        "run",
        "json",
    },
    "orchestration task-list": {"run", "brief", "json"},
    "orchestration task-update": {"id", "status", "result", "json"},
    "orchestration worker-start": {
        "task",
        "worktree",
        "agent",
        "model",
        "effort",
        "name",
        "display-name",
        "setup",
        "run",
        "json",
    },
    "orchestration worker-show": {"dispatch", "json"},
    "orchestration worker-stop": {"dispatch", "json"},
    "orchestration worker-release": {"dispatch", "json"},
    "orchestration check": {
        "run",
        "ack",
        "json",
    },
    "orchestration send": {
        "from",
        "dispatch-capability",
        "type",
        "subject",
        "body",
        "payload",
        "task-id",
        "dispatch-id",
        "outcome",
        "files-modified",
        "report-path",
        "phase",
        "json",
    },
}
TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "mode",
    "objective",
    "runId",
    "envelope",
    "defaults",
    "workers",
}
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
}
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
    "reviewer": (
        "Read only. Review raw integrated state using the assigned lens. Map acceptance "
        "criteria to evidence; report only reproducible material findings with exact path:line. "
        "Do not trust implementer conclusions and do not edit."
    ),
    "antislop": (
        "Read only and review quality, not bugs. Verify concrete cuts for duplication, "
        "unnecessary abstraction, silent fallback, speculative compatibility/generality, "
        "wrong depth, reinvention, wrapper/comment slop, or wasted work. Search call sites; "
        "do not invent cuts. Leanness is informational."
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


def string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise HelperError(f"{name} must be a list of non-empty strings")
    return [item.strip() for item in value]


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
    parts.append(f"RULES\n{ROLE_RULES[role]}")
    lens = str(worker.get("lens", "")).lower()
    if role == "reviewer" and "antislop" in lens:
        parts.append(
            "ANTI-SLOP LENS\nAlso verify concrete cuts for duplication, unnecessary "
            "abstraction, silent fallback, speculative compatibility/generality, wrong "
            "implementation depth, reinvention, wrapper/comment slop, and wasted work."
        )
    parts.append(
        "REPORT\nFollow Orca's injected lifecycle command exactly. Send exactly one "
        "worker_done, only when the job is finished — never a probe, test, or partial "
        "completion, because the first accepted completion becomes your report of "
        "record. Never put the report into a status or question message; only the "
        "single worker_done --payload is validated. Copy lifecycle IDs verbatim from "
        "the injected command; never retype or reconstruct them. Keep --body to exactly "
        "three concise executive-summary sentences. Add one --payload argument containing "
        "compact JSON matching this contract (material evidence only, <=3000 chars). "
        "Replace every <...> placeholder; use an empty array when that category has no "
        "material items:\n" + compact_json(report_example(role))
    )
    if role in {"reviewer", "antislop"}:
        parts.append(
            "A completed review uses lifecycle outcome succeeded even when the verdict is "
            "FAIL, UNKNOWN, or BLOCKED; failed means the review job itself failed. "
            "When the allowed evidence cannot prove or refute a claim, return verdict "
            "UNKNOWN and list the exact unprovable claims in risks; never stretch an "
            "evidence gap into PASS or into a FAIL finding without a defect."
        )
    prompt = "\n\n".join(parts).strip() + "\n"
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HelperError(
            f"rendered prompt is {len(prompt)} chars; cap is {MAX_PROMPT_CHARS}"
        )
    return prompt


def runtime_prompt(prompt: str, directory: Path) -> str:
    """Append the wake-only completion hook once live journal identity exists."""
    notify_command = shlex.join(
        [
            "uv",
            "run",
            "--no-project",
            str(Path(__file__).resolve()),
            "notify-controller",
            "--receipt-dir",
            str(directory),
        ]
    )
    block = (
        "CONTROLLER WAKE\n"
        "Do not poll, call ask, or wait for the coordinator. If a missing decision truly "
        "blocks the task, complete with taskStatus=blocked plus verified evidence; Sol can "
        "then answer and dispatch a fresh continuation. In the same shell tool call as your "
        "exact injected worker_done command, append the following with && so it runs "
        "only after Orca accepts worker_done:\n"
        f"{notify_command}\n"
        "This queues one wake-only continuation for Sol; it carries no lifecycle "
        "authority. Then stop exactly as Orca's preamble requires."
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
    run_id = manifest.get("runId")
    if run_id is not None:
        run_id = require_string(run_id, "manifest.runId")
        if not re.match(r"^run[_-]", run_id):
            raise HelperError("manifest.runId must be an Orca Run ID")
    envelope = require_object(
        manifest.get("envelope"), "manifest.envelope", ENVELOPE_FIELDS
    )
    envelope_goal = require_string(envelope.get("goal"), "envelope.goal")
    non_goals = string_list(envelope.get("nonGoals"), "envelope.nonGoals")
    constraints = string_list(envelope.get("constraints"), "envelope.constraints")
    dirty_state = string_list(envelope.get("dirtyState"), "envelope.dirtyState")
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
    if run_id:
        normalized_manifest["runId"] = run_id
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
    if state.get("launch_specs") != LAUNCH_SPECS:
        raise HelperError(
            "wave was journaled under a different launch policy; "
            "never patch the helper while its Run has Tasks"
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


def worker_start_args(
    worker: dict[str, Any], task_id: str, label: str, run_id: str
) -> list[str]:
    worktree = worker.get("worktree", "current")
    spec = LAUNCH_SPECS[worker["launch"]]
    args = [
        "orchestration",
        "worker-start",
        "--task",
        task_id,
        "--worktree",
        worktree,
        "--agent",
        spec["agent"],
        "--model",
        spec["model"],
        "--effort",
        spec["effort"],
        "--run",
        run_id,
    ]
    if worktree in {"new-child", "new-top-level"}:
        args.extend(
            [
                "--name",
                worker["name"],
                "--display-name",
                label,
                "--setup",
                worker.get("setup", "run"),
            ]
        )
    args.append("--json")
    return args


def manifest_digest(manifest: dict[str, Any]) -> str:
    encoded = compact_json(manifest).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_journal(directory: Path) -> None:
    for name in (
        "tasks",
        "dispatches",
        "deliveries",
        "reports",
        "questions",
        "releases",
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


def command_contract_check(context: Any) -> tuple[list[dict[str, Any]], str]:
    registry = command_registry(context)
    checks: list[dict[str, Any]] = []
    relevant: dict[str, Any] = {}
    for command, required_flags in REQUIRED_ORCA_COMMANDS.items():
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


def codex_model_catalog() -> tuple[dict[str, set[str]], str | None]:
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
    supported: dict[str, set[str]] = {}
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            continue
        supported[item["slug"]] = {
            level.get("effort")
            for level in item.get("supported_reasoning_levels", [])
            if isinstance(level, dict) and isinstance(level.get("effort"), str)
        }
    return supported, None


def launch_checks(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verify every launch spec the wave uses; the codex catalog is read once."""
    checks: list[dict[str, Any]] = []
    codex_catalog: dict[str, set[str]] | None = None
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
            efforts = (codex_catalog or {}).get(spec["model"], set())
            checks.append(
                {
                    "name": name,
                    "passed": spec["effort"] in efforts,
                    "model": spec["model"],
                    "effort": spec["effort"],
                    "supportedEfforts": sorted(efforts),
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
    capabilities = set(runtime.get("capabilities", []))
    required_capabilities = {
        "orchestration.contract.v1",
        "orchestration.worker-launch-preferences.v1",
    }
    checks.append(
        {
            "name": "runtime-capabilities",
            "passed": required_capabilities <= capabilities,
            "missing": sorted(required_capabilities - capabilities),
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

    run_id = manifest.get("runId")
    if run_id:
        returncode, payload, detail = call_orca(
            ["orchestration", "run-show", "--id", run_id, "--json"]
        )
        checks.append(
            {
                "name": f"run:{run_id}",
                "passed": returncode == 0 and payload is not None,
                **({} if returncode == 0 else {"error": detail}),
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
    save_json(directory / "manifest.json", manifest)
    live_prompts = [runtime_prompt(prompt, directory) for prompt in prompts]
    receipt = preflight_manifest(manifest, workers)
    receipt["workers"] = [
        {
            "id": worker["id"],
            "role": worker["role"],
            "launch": worker["launch"],
            "worktree": worker["worktree"],
            "mutation": worker["mutation"],
            "label": worker_label(worker, index),
            "promptChars": len(prompt),
        }
        for index, (worker, prompt) in enumerate(zip(workers, live_prompts), start=1)
    ]
    save_json(directory / "preflight.json", receipt)
    failed = [check["name"] for check in receipt["checks"] if not check.get("passed")]
    print(
        compact_json(
            {
                "status": receipt["status"],
                "launches": sorted({worker["launch"] for worker in workers}),
                "runtime": receipt.get("orca"),
                "failedChecks": failed,
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
    for prompt in prompts:
        runtime_prompt(prompt, directory)

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
        "task_id",
        "dispatch_id",
        "worktree_id",
        "terminal_handle",
        "tab_title_status",
        "tab_title_error",
        "start_status",
        "lifecycle_status",
        "completion_accepted",
        "task_outcome",
        "orca_task_status",
        "report_status",
        "verdict",
        "release_status",
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


def launch_preferences(payload: Any) -> dict[str, Any]:
    launch = first_named(payload, "launch")
    if not isinstance(launch, dict):
        return {}
    requested = launch.get("requested")
    effective = launch.get("effective")
    return {
        "launch_requested": requested if isinstance(requested, dict) else None,
        "launch_effective": effective if isinstance(effective, dict) else None,
    }


def create_pending_tasks(
    directory: Path, workers: list[dict[str, Any]], prompts: list[str]
) -> None:
    run_id = require_string(read_wave_state(directory).get("run_id"), "wave.run_id")
    for index, (worker, prompt) in enumerate(zip(workers, prompts), start=1):
        if cancel_requested(directory):
            return
        record = read_wave_state(directory)["workers"][index - 1]
        if record.get("task_id"):
            continue
        if record.get("start_status") != "pending":
            raise HelperError(
                f"worker {index} is {record.get('start_status')}; refusing an ambiguous Task retry"
            )
        update_worker_state(directory, index, start_status="creating_task")
        label = record["label"]
        task_args = [
            "orchestration",
            "task-create",
            "--spec",
            runtime_prompt(prompt, directory),
            "--task-title",
            label,
            "--display-name",
            label,
            "--run",
            run_id,
            "--json",
        ]
        returncode, receipt, detail = call_orca(task_args)
        save_json(
            directory / "tasks" / f"{worker['id']}.json",
            receipt
            if receipt is not None
            else {"returncode": returncode, "detail": detail},
        )
        task_id = find_entity_id(receipt, "task") if receipt else None
        if returncode != 0 or not task_id:
            error = f"task-create exited {returncode}: {detail}"
            classification = classify_failure(receipt, known_effect_id=task_id)
            update_worker_state(
                directory,
                index,
                task_id=task_id,
                start_status=f"task_{classification}",
                error=error,
            )
            raise HelperError(f"{error}; receipts: {directory}")
        update_worker_state(
            directory, index, task_id=task_id, start_status="task_created", error=None
        )


def start_pending_workers(directory: Path, workers: list[dict[str, Any]]) -> None:
    run_id = require_string(read_wave_state(directory).get("run_id"), "wave.run_id")
    for index, worker in enumerate(workers, start=1):
        if cancel_requested(directory):
            return
        record = read_wave_state(directory)["workers"][index - 1]
        if record.get("dispatch_id"):
            continue
        if record.get("start_status") != "task_created":
            raise HelperError(
                f"worker {index} is {record.get('start_status')}; refusing an ambiguous worker retry"
            )
        task_id = record.get("task_id")
        if not task_id:
            raise HelperError(f"worker {index} has no Task ID")
        update_worker_state(directory, index, start_status="starting_worker")
        returncode, receipt, detail = call_orca(
            worker_start_args(worker, task_id, record["label"], run_id)
        )
        save_json(
            directory / "dispatches" / f"{worker['id']}.json",
            receipt
            if receipt is not None
            else {"returncode": returncode, "detail": detail},
        )
        dispatch_id = find_entity_id(receipt, "dispatch") if receipt else None
        worktree_id = find_entity_id(receipt, "worktree") if receipt else None
        terminal_handle = find_terminal_handle(receipt) if receipt else None
        changes: dict[str, Any] = {
            "dispatch_id": dispatch_id,
            "worktree_id": worktree_id,
            "terminal_handle": terminal_handle,
            **launch_preferences(receipt),
        }
        if returncode != 0 or not dispatch_id:
            classification = classify_failure(receipt, known_effect_id=dispatch_id)
            changes.update(
                start_status=f"worker_{classification}",
                error=f"worker-start exited {returncode}: {detail}",
            )
            update_worker_state(directory, index, **changes)
            if cancel_requested(directory):
                return
            guidance = (
                "outcome is ambiguous; do not retry"
                if classification == "outcome_unknown"
                else "outcome is classified; do not replay this worker-start in the same wave"
            )
            spec = LAUNCH_SPECS[worker["launch"]]
            raise HelperError(
                f"worker-start {index} {classification} at exact "
                f"{spec['model']} {spec['effort']}; "
                f"{guidance}: {detail}; receipts: {directory}"
            )
        changes.update(
            start_status="running",
            tab_title_status="pending" if terminal_handle else "unavailable",
            error=None,
        )
        update_worker_state(directory, index, **changes)
        if cancel_requested(directory):
            return
        if terminal_handle:
            rename_code, rename_receipt, rename_detail = call_orca(
                [
                    "terminal",
                    "rename",
                    "--terminal",
                    terminal_handle,
                    "--title",
                    record["label"],
                    "--json",
                ]
            )
            save_json(
                directory / "runtime" / f"tab-title-{worker['id']}.json",
                rename_receipt
                if rename_receipt is not None
                else {"returncode": rename_code, "detail": rename_detail},
            )
            if rename_code == 0:
                update_worker_state(directory, index, tab_title_status="renamed")
            else:
                update_worker_state(
                    directory,
                    index,
                    tab_title_status="rename_failed",
                    tab_title_error=rename_detail,
                )
        if cancel_requested(directory):
            return


def reconcile_stop_wave(directory: Path) -> dict[str, Any]:
    request_cancel(directory)
    errors: list[str] = []
    snapshot = read_wave_state(directory)
    for record in snapshot["workers"]:
        index = record["index"]
        dispatch_id = record.get("dispatch_id")
        task_id = record.get("task_id")
        if record.get("stop_status") == "stopped":
            continue
        if (
            record.get("stop_status") == "not_created"
            and not dispatch_id
            and not task_id
        ):
            continue
        if record.get("stop_status") == "task_blocked" and not dispatch_id:
            continue
        if dispatch_id:
            returncode, receipt, detail = call_orca(
                ["orchestration", "worker-stop", "--dispatch", dispatch_id, "--json"]
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
        elif task_id:
            returncode, receipt, detail = call_orca(
                [
                    "orchestration",
                    "task-update",
                    "--id",
                    task_id,
                    "--status",
                    "blocked",
                    "--result",
                    compact_json({"reason": "wave_cancelled"}),
                    "--json",
                ]
            )
            save_json(
                directory / "runtime" / f"block-{record['worker_id']}.json",
                receipt
                if receipt is not None
                else {"returncode": returncode, "detail": detail},
            )
            if returncode == 0:
                update_worker_state(directory, index, stop_status="task_blocked")
            else:
                errors.append(f"task {index}: {detail}")
                update_worker_state(
                    directory, index, stop_status="block_failed", error=detail
                )
        else:
            update_worker_state(
                directory,
                index,
                start_status="cancelled",
                stop_status="not_created",
                tab_title_status="not_created",
            )
    latest = read_wave_state(directory)
    unresolved = [
        record["index"]
        for record in latest["workers"]
        if record.get("start_status")
        in {
            "creating_task",
            "starting_worker",
            "task_outcome_unknown",
            "worker_outcome_unknown",
        }
        or record.get("stop_status") in {"stop_failed", "block_failed"}
        or (record.get("dispatch_id") and record.get("stop_status") != "stopped")
    ]
    if latest.get("run_status") in {
        "creating_run",
        "binding_run",
        "outcome_unknown",
    }:
        unresolved.insert(0, "run")
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
    set_wave_phase(directory, "creating_tasks")
    create_pending_tasks(directory, workers, prompts)
    if cancel_requested(directory):
        print(compact_json(reconcile_stop_wave(directory)), flush=True)
        return 0
    set_wave_phase(directory, "starting_workers")
    start_pending_workers(directory, workers)
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
            "mode": manifest["mode"],
            "objective": manifest["objective"],
            "launch_specs": LAUNCH_SPECS,
            "controller_terminal_handle": require_string(
                os.environ.get("ORCA_TERMINAL_HANDLE"), "ORCA_TERMINAL_HANDLE"
            ),
            "run_id": None,
            "run_status": "pending",
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
                    "task_id": None,
                    "dispatch_id": None,
                    "worktree_id": None,
                    "terminal_handle": None,
                    "tab_title_status": "pending",
                    "tab_title_error": None,
                    "start_status": "pending",
                    "lifecycle_status": "pending",
                    "completion_accepted": None,
                    "task_outcome": None,
                    "report_status": "pending",
                    "verdict": None,
                    "release_status": "pending",
                    "notification_status": "pending",
                    "stop_status": None,
                    "error": None,
                }
                for index, worker in enumerate(workers, start=1)
            ],
        },
    )


def create_or_bind_run(directory: Path, manifest: dict[str, Any]) -> str:
    requested = manifest.get("runId")
    operation = "binding_run" if requested else "creating_run"

    def mark_started(state: dict[str, Any]) -> None:
        state["run_status"] = operation
        state["phase"] = operation

    mutate_wave_state(directory, mark_started)
    if requested:
        arguments = ["orchestration", "run-use", "--id", requested, "--json"]
    else:
        arguments = [
            "orchestration",
            "run-create",
            "--objective",
            manifest["objective"],
            "--json",
        ]
    returncode, receipt, detail = call_orca(arguments)
    save_json(
        directory / "run.json",
        receipt
        if receipt is not None
        else {"returncode": returncode, "detail": detail},
    )
    observed_run_id = find_entity_id(receipt, "run") if receipt else None
    run_id = requested or observed_run_id
    if returncode != 0 or not run_id:
        classification = classify_failure(receipt, known_effect_id=observed_run_id)

        def mark_failed(state: dict[str, Any]) -> None:
            state["run_id"] = run_id
            state["run_status"] = classification

        mutate_wave_state(directory, mark_failed)
        raise HelperError(
            f"run setup {classification}: Orca exited {returncode}: {detail}"
        )

    def update(state: dict[str, Any]) -> None:
        state["run_id"] = run_id
        state["run_status"] = "ready"
        state["phase"] = "run_ready"

    mutate_wave_state(directory, update)
    return run_id


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
        create_or_bind_run(directory, manifest)
        if cancel_requested(directory):
            print(compact_json(reconcile_stop_wave(directory)), flush=True)
            return 0
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
        if record.get("start_status")
        in {
            "creating_task",
            "starting_worker",
            "task_outcome_unknown",
            "worker_outcome_unknown",
        }
    ]
    if ambiguous:
        raise HelperError(
            f"cannot safely resume ambiguous worker indices {ambiguous}; inspect receipts and stop the wave"
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


def message_value(message: dict[str, Any], *names: str) -> Any:
    wanted = {normalized_key(name) for name in names}
    for key, value in message.items():
        if normalized_key(key) in wanted:
            return value
    return None


def decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def decoded_message_payload(message: dict[str, Any]) -> dict[str, Any]:
    payload = decode_json(message_value(message, "payload"))
    return payload if isinstance(payload, dict) else {}


def message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    """Decode lifecycle metadata without coupling it to the report schema."""
    payload = decoded_message_payload(message)
    aliases = {
        "taskId": ("task_id", "taskId"),
        "dispatchId": ("dispatch_id", "dispatchId"),
        "outcome": ("outcome",),
        "accepted": ("accepted",),
        "rejectionCode": ("rejection_code", "rejectionCode"),
        "phase": ("phase",),
    }
    result: dict[str, Any] = {}
    conflicts: list[str] = []
    for output_name, names in aliases.items():
        top_level = message_value(message, *names)
        nested = message_value(payload, *names)
        if top_level is not None and nested is not None and top_level != nested:
            conflicts.append(output_name)
        value = nested if nested is not None else top_level
        if value is not None:
            result[output_name] = value
    identity_conflicts = [
        field for field in conflicts if field in {"taskId", "dispatchId"}
    ]
    if identity_conflicts:
        result["conflict"] = "identity_conflict"
        result["conflictFields"] = identity_conflicts
    elif "outcome" in conflicts:
        result["conflict"] = "outcome_conflict"
        result["conflictFields"] = ["outcome"]
    return result


def safe_receipt_name(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
        if safe:
            return safe[:120]
    return fallback


def message_nodes(value: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    found_batch = False
    for node in walk(value):
        if not isinstance(node, dict):
            continue
        for key, child in node.items():
            if normalized_key(key) != "messages" or not isinstance(child, list):
                continue
            found_batch = True
            candidates.extend(item for item in child if isinstance(item, dict))
            break
        if found_batch:
            break
    if not found_batch:
        candidates = [
            node
            for node in walk(value)
            if isinstance(node, dict)
            and message_value(node, "type", "message_type") in MESSAGE_TYPES
        ]

    messages: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for node in candidates:
        kind = message_value(node, "type", "message_type")
        metadata = message_metadata(node)
        message_id = message_value(node, "message_id", "id")
        task_marker = metadata.get("taskId")
        dispatch_marker = metadata.get("dispatchId")
        signature = (
            kind,
            message_id,
            compact_json(task_marker)
            if isinstance(task_marker, (dict, list))
            else task_marker,
            compact_json(dispatch_marker)
            if isinstance(dispatch_marker, (dict, list))
            else dispatch_marker,
            None if message_id is not None else compact_json(node),
        )
        if signature in seen:
            continue
        seen.add(signature)
        messages.append(node)
    return messages


def worker_for_message(
    state: dict[str, Any], task_id: Any, dispatch_id: Any
) -> dict[str, Any] | None:
    if isinstance(dispatch_id, str):
        for record in state.get("workers", []):
            if record.get("dispatch_id") == dispatch_id:
                return record
    if isinstance(task_id, str):
        for record in state.get("workers", []):
            if record.get("task_id") == task_id:
                return record
    return None


def extract_report(node: dict[str, Any]) -> Any:
    payload = decoded_message_payload(node)
    nested = decode_json(payload.get("report"))
    if isinstance(nested, dict):
        return nested
    if "reportSchemaVersion" in payload:
        return payload
    body = decode_json(message_value(node, "body"))
    if isinstance(body, dict) and "reportSchemaVersion" in body:
        return body
    return None


def validate_report(report: Any, role: str) -> list[str]:
    if not isinstance(report, dict):
        return ["missing structured report payload"]
    errors: list[str] = []
    if report.get("reportSchemaVersion") != REPORT_VERSION:
        errors.append(f"reportSchemaVersion must be {REPORT_VERSION}")
    if report.get("taskStatus") not in {"done", "blocked"}:
        errors.append("taskStatus must be done or blocked")
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
        errors.append("summary must be a non-empty string <=500 chars")
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
    return errors


def completion_acceptance(
    metadata: dict[str, Any], record: dict[str, Any] | None
) -> tuple[bool, str | None]:
    explicit = metadata.get("accepted")
    rejection = metadata.get("rejectionCode")
    if explicit is False or isinstance(rejection, str):
        return False, rejection if isinstance(rejection, str) else "rejected"
    conflict = metadata.get("conflict")
    if isinstance(conflict, str):
        return False, conflict
    if record is None:
        return False, "unknown_dispatch"
    task_id = metadata.get("taskId")
    dispatch_id = metadata.get("dispatchId")
    if task_id != record.get("task_id"):
        return False, "task_identity_mismatch"
    if dispatch_id != record.get("dispatch_id"):
        return False, "dispatch_identity_mismatch"
    if metadata.get("outcome") not in {"succeeded", "failed"}:
        return False, "invalid_outcome"
    return True, None


def release_worker(directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    if record.get("release_status") == "released":
        return {"status": "already_released"}
    dispatch_id = record.get("dispatch_id")
    if not dispatch_id:
        update_worker_state(directory, record["index"], release_status="release_failed")
        return {"status": "release_failed", "error": "missing dispatch ID"}
    returncode, receipt, detail = call_orca(
        ["orchestration", "worker-release", "--dispatch", dispatch_id, "--json"]
    )
    path = directory / "releases" / f"{record['worker_id']}.json"
    save_json(
        path,
        receipt
        if receipt is not None
        else {"returncode": returncode, "detail": detail},
    )
    status = "released" if returncode == 0 else "release_failed"
    update_worker_state(
        directory,
        record["index"],
        release_status=status,
        **({} if returncode == 0 else {"error": detail}),
    )
    return {
        "status": status,
        "receipt": str(path),
        **({} if returncode == 0 else {"error": detail}),
    }


def normalize_message(
    directory: Path, node: dict[str, Any], ordinal: int
) -> dict[str, Any]:
    state = read_wave_state(directory)
    kind = message_value(node, "type", "message_type") or "unknown"
    message_id = message_value(node, "message_id", "id")
    metadata = message_metadata(node)
    task_id = metadata.get("taskId")
    dispatch_id = metadata.get("dispatchId")
    record = worker_for_message(state, task_id, dispatch_id)
    body = message_value(node, "body")
    body_text = (
        body
        if isinstance(body, str)
        else compact_json(body)
        if body is not None
        else ""
    )
    truncated = len(body_text) > MAX_BODY_OUTPUT_CHARS
    summary = body_text[:MAX_BODY_OUTPUT_CHARS]
    report_path: str | None = None
    verdict: str | None = None
    task_status: str | None = None
    accepted: bool | None = None
    rejection_code: str | None = None
    report_errors: list[str] = []
    release: dict[str, Any] | None = None
    duplicate = False
    repaired = False
    misdirected = False

    if kind == "worker_done":
        accepted, rejection_code = completion_acceptance(metadata, record)
        # A completion for an already-accepted worker must never overwrite the
        # journaled report or state; re-delivered mail is normalized read-only.
        # The one exception is report repair: matching identity and outcome with
        # a valid report may replace an invalid journaled report, because the
        # first accepted completion can be a malformed probe (wave13).
        duplicate = bool(record) and record.get("completion_accepted") is True
        report = extract_report(node)
        role = record.get("role") if record else ""
        report_errors = validate_report(report, role)
        repaired = (
            duplicate
            and accepted
            and not report_errors
            and record.get("report_status") == "invalid"
            and metadata.get("outcome") == record.get("task_outcome")
        )
        if isinstance(report, dict):
            task_status_value = report.get("taskStatus")
            task_status = (
                task_status_value if isinstance(task_status_value, str) else None
            )
            verdict_value = report.get("verdict")
            verdict = verdict_value if isinstance(verdict_value, str) else None
            report_summary = report.get("summary")
            if isinstance(report_summary, str):
                truncated = len(report_summary) > MAX_BODY_OUTPUT_CHARS
                summary = report_summary[:MAX_BODY_OUTPUT_CHARS]
            if not duplicate or repaired:
                if record:
                    report_file = directory / "reports" / f"{record['worker_id']}.json"
                else:
                    report_file = (
                        directory
                        / "reports"
                        / (
                            safe_receipt_name(message_id, f"unknown-{ordinal}")
                            + ".json"
                        )
                    )
                save_json(
                    report_file,
                    {
                        "accepted": accepted,
                        "validationErrors": report_errors,
                        "report": report,
                    },
                )
                report_path = str(report_file)
        if record and not duplicate:
            outcome = metadata.get("outcome")
            changes = {
                "start_status": "completed" if accepted else record.get("start_status"),
                "lifecycle_status": outcome if accepted else "rejected",
                "completion_accepted": accepted,
                "task_outcome": outcome,
                "report_status": "valid" if not report_errors else "invalid",
                "verdict": verdict,
            }
            update_worker_state(directory, record["index"], **changes)
            if accepted:
                record = read_wave_state(directory)["workers"][record["index"] - 1]
                release = release_worker(directory, record)
                if wave_settled(read_wave_state(directory)):
                    set_wave_phase(directory, "awaiting_finalize")
        elif record and repaired:
            update_worker_state(
                directory,
                record["index"],
                report_status="valid",
                verdict=verdict,
            )
    elif kind == "status":
        # A structured report inside status mail is a junior contract violation
        # (wave14): journal it as side evidence and surface it, but it never
        # becomes validated completion evidence.
        side_report = extract_report(node)
        if isinstance(side_report, dict):
            misdirected = True
            owner = (
                record["worker_id"]
                if record
                else safe_receipt_name(message_id, f"status-{ordinal}")
            )
            side_path = directory / "reports" / f"{owner}.status-{ordinal}.json"
            save_json(side_path, {"source": "status_message", "report": side_report})
            report_path = str(side_path)
    elif kind == "question":
        question_file = (
            directory
            / "questions"
            / (safe_receipt_name(message_id, f"question-{ordinal}") + ".json")
        )
        save_json(question_file, node)

    normalized = {
        "type": kind,
        "messageId": message_id,
        "taskId": task_id,
        "dispatchId": dispatch_id,
        "outcome": metadata.get("outcome"),
        "phase": metadata.get("phase"),
        "accepted": accepted,
        "rejectionCode": rejection_code,
        "expectedTaskId": record.get("task_id") if record else None,
        "expectedDispatchId": record.get("dispatch_id") if record else None,
        "verdict": verdict,
        "taskStatus": task_status,
        "summary": summary,
        "truncated": truncated,
        "duplicate": duplicate or None,
        "repairedReport": repaired or None,
        "misdirectedReport": misdirected or None,
        "reportPath": report_path,
        "reportErrors": report_errors if kind == "worker_done" else None,
        "release": release,
    }
    return {key: value for key, value in normalized.items() if value is not None}


def process_delivery(directory: Path, receipt: Any, source: str) -> dict[str, Any]:
    ensure_journal(directory)
    delivery_id = find_entity_id(receipt, "delivery")
    fallback = f"{source}-{time.time_ns()}"
    receipt_name = safe_receipt_name(delivery_id, fallback)
    raw_path = directory / "deliveries" / f"{receipt_name}.raw.json"
    save_json(raw_path, receipt)
    messages = [
        normalize_message(directory, node, ordinal)
        for ordinal, node in enumerate(message_nodes(receipt), start=1)
    ]
    normalized = {
        "deliveryId": delivery_id,
        "source": source,
        "count": len(messages),
        "messages": messages,
        "rawReceipt": str(raw_path),
    }
    normalized_path = directory / "deliveries" / f"{receipt_name}.json"
    save_json(normalized_path, normalized)
    normalized["receipt"] = str(normalized_path)
    return normalized


def delivery_actions(delivery: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only messages that require Sol policy before acknowledgment."""
    actions: list[dict[str, Any]] = []
    for message in delivery.get("messages", []):
        kind = message.get("type")
        if kind in {"question", "escalation"}:
            actions.append(message)
            continue
        if kind == "worker_done":
            lifecycle_failed = message.get("outcome") == "failed"
            rejected = message.get("accepted") is not True
            invalid_report = bool(message.get("reportErrors"))
            release_failed = isinstance(message.get("release"), dict) and message[
                "release"
            ].get("status") not in {
                "released",
                "already_released",
            }
            material_verdict = message.get("verdict") in {
                "FAIL",
                "UNKNOWN",
                "BLOCKED",
            }
            content_blocked = message.get("taskStatus") == "blocked"
            if any(
                (
                    lifecycle_failed,
                    rejected,
                    invalid_report,
                    release_failed,
                    material_verdict,
                    content_blocked,
                )
            ):
                actions.append(message)
            continue
        if kind == "status" and message.get("misdirectedReport"):
            actions.append(message)
            continue
        if kind not in {"heartbeat", "status"}:
            actions.append(message)
    return actions


def wave_settled(state: dict[str, Any]) -> bool:
    workers = state.get("workers", [])
    return bool(workers) and all(
        worker.get("completion_accepted") is True
        and worker.get("release_status") == "released"
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


def command_drain_deliveries(args: argparse.Namespace) -> int:
    """Drain currently queued Deliveries without waiting for future messages."""
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    run_id = require_string(state.get("run_id"), "wave.run_id")
    pending_ack = getattr(args, "ack", None)
    batches = 0
    processed: list[dict[str, Any]] = []
    race_close_needed = notification_path(directory).exists()

    while batches < 100:
        arguments = ["orchestration", "check", "--run", run_id]
        if pending_ack:
            arguments.extend(["--ack", pending_ack])
        arguments.append("--json")
        receipt = run_orca(arguments)
        delivery = process_delivery(directory, receipt, "drain")
        batches += 1
        pending_ack = None

        if delivery["count"] == 0:
            if race_close_needed:
                clear_notification(directory)
                race_close_needed = False
                # Close the coalescing race once: a worker may have published after
                # the empty check while the notification marker still existed.
                continue
            latest = read_wave_state(directory)
            settled = wave_settled(latest)
            print(
                compact_json(
                    {
                        "status": "wave_settled" if settled else "idle_push_mode",
                        "batches": batches,
                        "messages": processed,
                        "workers": wave_records(latest),
                        "next": (
                            "run finalize-wave"
                            if settled
                            else "return to idle; do not call wait or drain again until Orca queues a new controller notification"
                        ),
                        "receipts": str(directory),
                    }
                )
            )
            return 0

        processed.extend(delivery["messages"])
        actions = delivery_actions(delivery)
        if actions:
            print(
                compact_json(
                    {
                        "status": "action_required",
                        "deliveryId": delivery.get("deliveryId"),
                        "ackRequired": bool(delivery.get("deliveryId")),
                        "messages": actions,
                        "receipt": delivery.get("receipt"),
                        "next": "handle every actionable message, then run drain-deliveries --ack <deliveryId>; return to push-idle afterward",
                    }
                )
            )
            return 0

        delivery_id = delivery.get("deliveryId")
        if not delivery_id:
            raise HelperError(
                "non-empty Delivery omitted deliveryId; refusing to acknowledge"
            )
        pending_ack = delivery_id

    raise HelperError("drain exceeded 100 immediately available Delivery batches")


def command_ack_delivery(args: argparse.Namespace) -> int:
    directory = receipt_dir(args.receipt_dir)
    state = read_wave_state(directory)
    run_id = require_string(state.get("run_id"), "wave.run_id")
    arguments = [
        "orchestration",
        "check",
        "--run",
        run_id,
        "--ack",
        args.delivery,
    ]
    arguments.append("--json")
    receipt = run_orca(arguments)
    print(compact_json(process_delivery(directory, receipt, "ack")))
    return 0


def task_status_map(receipt: Any) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for node in walk(receipt):
        if not isinstance(node, dict):
            continue
        task_id = message_value(node, "task_id", "id")
        status = message_value(node, "status", "state")
        if (
            isinstance(task_id, str)
            and task_id.startswith("task_")
            and isinstance(status, str)
        ):
            statuses[task_id] = status
    return statuses


def claim_controller_notification(
    directory: Path, worker: dict[str, Any], controller_handle: str
) -> bool:
    path = notification_path(directory)
    payload = (
        compact_json(
            {
                "workerId": worker["worker_id"],
                "taskId": worker.get("task_id"),
                "dispatchId": worker.get("dispatch_id"),
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
    """Queue one coalesced wake prompt after a worker's lifecycle send settles."""
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

    run_id = require_string(state.get("run_id"), "wave.run_id")
    task_id = require_string(record.get("task_id"), "worker.task_id")
    task_receipt = run_orca(
        ["orchestration", "task-list", "--run", run_id, "--brief", "--json"]
    )
    task_receipt_path = (
        directory / "runtime" / f"notify-task-{record['worker_id']}.json"
    )
    save_json(task_receipt_path, task_receipt)
    task_status = task_status_map(task_receipt).get(task_id)
    if task_status not in {"completed", "failed"}:
        raise HelperError(
            "notify-controller must run only after Orca accepts worker_done; "
            f"Task {task_id} is {task_status or 'unknown'}"
        )

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

    drain_command = shlex.join(
        [
            "uv",
            "run",
            "--no-project",
            str(Path(__file__).resolve()),
            "drain-deliveries",
            "--receipt-dir",
            str(directory),
        ]
    )
    text = (
        "[ORCA LUNA CYCLE: DELIVERY READY]\n"
        "A worker queued structured lifecycle mail. Run this once without wait/timeout, "
        "then follow its `next` field:\n"
        f"{drain_command}"
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
        # later completion into a wake that may never arrive and silently stall the
        # wave; a duplicate wake is harmless because the drain is idempotent.
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
    expected = LAUNCH_SPECS.get(record.get("launch"))
    if expected is None:
        return False
    requested = record.get("launch_requested")
    effective = record.get("launch_effective")
    return all(
        isinstance(value, dict)
        and all(value.get(key) == item for key, item in expected.items())
        for value in (requested, effective)
    )


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
    run_id = require_string(state.get("run_id"), "wave.run_id")
    task_receipt = run_orca(
        ["orchestration", "task-list", "--run", run_id, "--brief", "--json"]
    )
    save_json(directory / "runtime" / "task-list-final.json", task_receipt)
    statuses = task_status_map(task_receipt)
    for record in state.get("workers", []):
        dispatch_id = record.get("dispatch_id")
        if dispatch_id:
            show_receipt = run_orca(
                ["orchestration", "worker-show", "--dispatch", dispatch_id, "--json"]
            )
            save_json(
                directory / "runtime" / f"worker-{record['worker_id']}.json",
                show_receipt,
            )
        task_id = record.get("task_id")
        if task_id in statuses:
            update_worker_state(
                directory, record["index"], orca_task_status=statuses[task_id]
            )

    state = read_wave_state(directory)
    workers = state.get("workers", [])
    ambiguous_statuses = {
        "creating_task",
        "starting_worker",
        "task_outcome_unknown",
        "worker_outcome_unknown",
    }
    unresolved = [
        worker["worker_id"]
        for worker in workers
        if worker.get("start_status") in ambiguous_statuses
    ]
    checks = {
        "preflightPassed": load_json(directory / "preflight.json").get("status")
        == "passed",
        "runBound": bool(run_id) and state.get("run_status") == "ready",
        "allWorkersStarted": all(worker.get("dispatch_id") for worker in workers),
        "allTasksCompleted": all(
            worker.get("orca_task_status") == "completed" for worker in workers
        ),
        "allCompletionsAccepted": all(
            worker.get("completion_accepted") is True for worker in workers
        ),
        "allLifecycleSucceeded": all(
            worker.get("lifecycle_status") == "succeeded" for worker in workers
        ),
        "allReportsValid": all(
            worker.get("report_status") == "valid" for worker in workers
        ),
        "allWorkersReleased": all(
            worker.get("release_status") == "released" for worker in workers
        ),
        "noPendingControllerWake": not notification_path(directory).exists(),
        "exactLaunchProven": all(exact_launch_proven(worker) for worker in workers),
        "noAmbiguousEffects": not unresolved,
    }
    anchor = anchor_check(manifest, state)
    checks["anchorPreservedOrDelegated"] = anchor.get("passed") is True
    mechanical_ok = all(checks.values())
    set_wave_phase(directory, "finalized" if mechanical_ok else "finalize_incomplete")
    state = read_wave_state(directory)
    final = {
        "finalSchemaVersion": 1,
        "runId": run_id,
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
        "note": "Content verdicts remain inputs to the Sol gate; audit FAIL does not mean orchestration failed.",
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
    assert LAUNCH_SPECS["terra-xhigh"] == {
        "agent": "codex",
        "model": "gpt-5.6-terra",
        "effort": "xhigh",
    }
    assert ROLE_LAUNCHES["reviewer"] == ("sol-xhigh",)
    assert ROLE_LAUNCHES["antislop"] == ("sol-xhigh",)
    assert "terra-xhigh" in ROLE_LAUNCHES["implementer"]
    assert REQUIRED_ORCA_COMMANDS["orchestration check"] == {"run", "ack", "json"}
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
    start_args = worker_start_args(fable_workers[0], "task_1", "01 scout · x", "run_1")
    assert start_args[start_args.index("--agent") + 1] == "claude"
    assert start_args[start_args.index("--model") + 1] == "claude-fable-5"
    assert start_args[start_args.index("--effort") + 1] == "high"
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
                "launch": "terra-xhigh",
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
    assert overridden_workers[0]["launch"] == "terra-xhigh"
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
    assert (
        classify_failure({"error": {"code": "invalid_argument"}})
        == "rejected_no_effects"
    )
    with tempfile.TemporaryDirectory(prefix="orca-luna-self-test-") as temporary:
        directory = Path(temporary)
        ensure_journal(directory)
        initialize_wave_state(directory, manifest, workers)
        update_worker_state(directory, 1, task_id="task_1", start_status="task_created")
        assert read_wave_state(directory)["workers"][0]["task_id"] == "task_1"
        update_worker_state(directory, 1, task_id=None, start_status="pending")
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
        "dispatch-wave", help="create all Tasks, then start all workers"
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

    drain = commands.add_parser(
        "drain-deliveries",
        help="nonblocking drain after an Orca terminal-queue notification",
    )
    drain.add_argument("--receipt-dir", required=True)
    drain.add_argument("--ack", help="ack a processed actionable Delivery first")
    drain.set_defaults(func=command_drain_deliveries)

    notify = commands.add_parser(
        "notify-controller",
        help="worker-only exactly-once wake after accepted worker_done",
    )
    notify.add_argument("--receipt-dir", required=True)
    notify.set_defaults(func=command_notify_controller)

    ack = commands.add_parser(
        "ack-delivery", help="ack one Delivery and normalize any returned next batch"
    )
    ack.add_argument("--receipt-dir", required=True)
    ack.add_argument("--delivery", required=True)
    ack.set_defaults(func=command_ack_delivery)

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
