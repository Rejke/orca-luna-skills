
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
    "planreviewer": ("sol-xhigh",),
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
REVIEW_ROLES = {"reviewer", "antislop", "planreviewer"}
# v2 dispatch runs on plain Orca terminals and worktrees; the orchestration
# layer (runs, tasks, dispatches, mail) is not used. Workers get their prompt
# from a file, write their report to a file, and ping Sol with the wake hook.
REQUIRED_ORCA_COMMANDS = {
    "agent-context": {"json"},
    "worktree current": {"json"},
    "worktree show": {"worktree", "json"},
    "worktree create": {"name", "base-branch", "setup", "json"},
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
    and codex parses a bare -c value as a literal string. Agent sandbox and
    approvals are off: an unattended terminal cannot answer a prompt.
    """
    if spec["agent"] == "codex":
        command = (
            "codex --dangerously-bypass-approvals-and-sandbox "
            f"-m {spec['model']} -c model_reasoning_effort={spec['effort']}"
        )
        if spec.get("speedTier") == "fast":
            command += " -c service_tier=priority"
        return command
    return f"claude --dangerously-skip-permissions --model {spec['model']}"
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
    "knownFailureModes",
    "launch",
    "worktree",
    "name",
    "displayName",
    "baseBranch",
    "mutation",
    "setup",
}

