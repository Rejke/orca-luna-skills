PLAN_REVIEW_MISSION = (
    "Review the attached plan as written; find what would make it fail, "
    "mislead an implementer, or ship the wrong thing."
)
# ACs describe the artifact under review, never reviewer conduct — conduct
# lives in the planreviewer charter.
PLAN_REVIEW_AC_EXECUTABLE = (
    "The plan as written is executable by an implementer without inventing "
    "missing product or migration semantics."
)
PLAN_REVIEW_AC_PRIORS = (
    "The revised plan closes every prior finding at its root cause or names "
    "it as an open blocker."
)


def command_plan_review_manifest(args: argparse.Namespace) -> int:
    """Emit a complete plan-review wave manifest.

    The controller hands over only the artifact under review. Anything it wants
    to highlight goes into worker context via --hint; hints widen the search,
    they never define acceptance.
    """
    plan = Path(args.plan).resolve()
    if not plan.is_file():
        raise HelperError(f"plan file not found: {plan}")
    priors = [str(Path(prior).resolve()) for prior in args.prior or []]
    for prior in priors:
        if not Path(prior).is_file():
            raise HelperError(f"prior report not found: {prior}")
    prior_plans = [
        str(Path(prior_plan).resolve()) for prior_plan in args.prior_plan or []
    ]
    for prior_plan in prior_plans:
        if not Path(prior_plan).is_file():
            raise HelperError(f"prior plan version not found: {prior_plan}")
    if prior_plans and not priors:
        raise HelperError("--prior-plan requires the matching --prior report")
    criteria = {"AC1": PLAN_REVIEW_AC_EXECUTABLE}
    if priors:
        criteria["AC2"] = PLAN_REVIEW_AC_PRIORS
    worker: dict[str, Any] = {
        "id": args.worker_id,
        "role": "planreviewer",
        "displayName": "plan review",
        "goal": "Apply the plan-review charter to the plan file in scope.",
        "criteria": list(criteria),
        "scope": [str(plan), *prior_plans, *priors],
        "launch": "sol-xhigh",
    }
    if args.hint:
        worker["context"] = "Hints, not acceptance: " + "; ".join(args.hint)
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mode": "audit",
        "objective": (
            f"Independent review of the plan {plan.name} (sha256 {digest[:12]})."
        ),
        "envelope": {
            "goal": PLAN_REVIEW_MISSION,
            "acceptanceCriteria": criteria,
            "repairBudget": 0,
        },
        "defaults": {"worktree": "current", "mutation": "forbidden"},
        "workers": [worker],
    }
    validate_manifest(manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def command_plan_brief(args: argparse.Namespace) -> int:
    """Print only the authored plan content; mechanical fields stay out."""
    manifest, _, _ = validate_manifest(load_json(args.manifest))
    envelope = manifest["envelope"]
    parts = [
        f"OBJECTIVE\n{manifest['objective']}",
        f"GOAL\n{envelope['goal']}",
    ]
    for key, label in (
        ("nonGoals", "NON-GOALS"),
        ("acceptanceCriteria", "ACCEPTANCE"),
        ("constraints", "CONSTRAINTS"),
        ("reviewOverride", "REVIEW OVERRIDE"),
        ("knownFailureModes", "KNOWN FAILURE MODES"),
    ):
        if envelope.get(key) not in (None, "", [], {}):
            parts.append(f"{label}\n{render_value(envelope[key])}")
    for worker in manifest["workers"]:
        lines = [f"WORKER {worker['id']} ({worker['role']})"]
        for key in (
            "goal",
            "criteria",
            "scope",
            "ownership",
            "lens",
            "context",
            "constraints",
            "checks",
            "findings",
            "handoffs",
        ):
            if worker.get(key) not in (None, "", [], {}):
                lines.append(f"{key}: {render_value(worker[key])}")
        parts.append("\n".join(lines))
    print("\n\n".join(parts))
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


