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
        "taskStatus": "<done|failed|blocked>",
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
    ]
    if worker["goal"].strip() != envelope["goal"].strip():
        parts.append(f"GOAL\n{worker['goal']}")
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
    failure_modes = worker.get("knownFailureModes") or envelope.get(
        "knownFailureModes"
    )
    if failure_modes:
        parts.append(
            "KNOWN FAILURE MODES\n"
            "Learned rules from earlier waves; follow each one:\n"
            + "\n".join(f"- {rule}" for rule in failure_modes)
        )
    parts.append(f"RULES\n{ROLE_RULES[role]}")
    lens = str(worker.get("lens", "")).lower()
    if role == "reviewer" and "antislop" in lens:
        parts.append(
            "ANTI-SLOP LENS\nAlso verify concrete cuts for duplication, unnecessary "
            "abstraction, silent fallback, speculative compatibility/generality, wrong "
            "implementation depth, reinvention, wrapper/comment slop, and wasted work."
        )
    example = report_example(role)
    if role in REVIEW_ROLES:
        example["promptFeedback"] = [
            {
                "failureClass": "<short class>",
                "rule": "<one imperative, checkable instruction, <=200 chars>",
                "severity": "<critical|high|medium|low>",
                "scopes": ["<area tag>"],
                "gap": "<prompt|decomposition|judgment|test|tooling>",
            }
        ]
        if failure_modes:
            example["ruleFeedback"] = [
                {
                    "id": "<rule id from KNOWN FAILURE MODES>",
                    "status": "<violated|helped|retire>",
                }
            ]
    parts.append(
        "REPORT\nThe report file is your only channel to the controller. "
        "Write it once, when the work is complete or truly blocked — never a "
        "draft or probe. Use the Write tool: compact JSON, <=3000 chars, to "
        "the report file named in RUNTIME. Replace every <...> placeholder; "
        "use an empty array for a category with no material items:\n"
        + compact_json(example)
    )
    parts.append(
        'If you are blocked: set taskStatus "blocked", add a one-sentence '
        "question field, write the file, run the wake command, and stay idle "
        "here; the answer arrives in this terminal — finish the task and "
        "write the final report. Questions go only through the report file; "
        "never use AskUserQuestion. After the final report, run the wake "
        "command from RUNTIME and stop."
    )
    if role in REVIEW_ROLES:
        parts.append(
            "Severity scale: critical = data loss, security break, or crash in "
            "production; high = significant bug, race condition, or missing "
            "error handling; medium = logic, performance, or architecture "
            "concern; low = minor issue or edge case. "
            'taskStatus is "done" even when the verdict is FAIL or UNKNOWN; '
            "the verdict judges the work under review, not your job. When the evidence "
            "cannot prove or refute a claim, use verdict UNKNOWN and list the "
            "unprovable claims in risks; never turn an evidence gap into PASS "
            "or into a FAIL finding without a defect. promptFeedback is for "
            "failure classes a better worker prompt would have prevented; a "
            'rule names an exact check — "be careful" is not a rule.'
        )
    if role == "fixer":
        parts.append(
            'In "fixed", account for every declared finding: its title plus '
            "the evidence — path:line, test, or check — that closes it at "
            'root cause. A finding you could not close stays out of "fixed" '
            "and goes into risks with the exact blocker."
        )
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
        "After the wake, stop and stay idle in this terminal."
    )
    rendered = prompt.rstrip() + "\n\n" + block + "\n"
    if len(rendered) > MAX_PROMPT_CHARS:
        raise HelperError(
            f"live rendered prompt is {len(rendered)} chars; cap is {MAX_PROMPT_CHARS}"
        )
    return rendered


