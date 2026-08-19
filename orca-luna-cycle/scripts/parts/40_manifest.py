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

    def validated_failure_modes(value: Any, label: str) -> list[str]:
        rules = string_list(value, label)
        if len(rules) > MAX_KNOWN_FAILURE_MODES:
            raise HelperError(
                f"{label} allows at most {MAX_KNOWN_FAILURE_MODES} rules"
            )
        if any(len(rule) > MAX_KNOWN_FAILURE_MODE_CHARS for rule in rules):
            raise HelperError(
                f"each rule in {label} must be at most "
                f"{MAX_KNOWN_FAILURE_MODE_CHARS} characters"
            )
        return rules

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
    mutator_locations: dict[str, list[str]] = {}
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
        base_branch = worker.get("baseBranch")
        if worktree in {"new-child", "new-top-level"}:
            name = require_string(name, f"workers[{index}].name")
            if name in seen_worktree_names:
                raise HelperError(f"duplicate new worktree name: {name}")
            seen_worktree_names.add(name)
            base_branch = require_string(base_branch, f"workers[{index}].baseBranch")
        elif name is not None:
            raise HelperError("worker name applies only to new-child or new-top-level")
        elif base_branch is not None:
            raise HelperError(
                "worker baseBranch applies only to new-child or new-top-level"
            )
        if mode in {"audit", "benchmark"} and (
            mutation != "forbidden" or role in MUTATOR_ROLES
        ):
            raise HelperError(f"{mode} workers must be strictly read-only")
        if role in {"scout", *REVIEW_ROLES} and mutation != "forbidden":
            raise HelperError(f"read-only role {role} requires mutation=forbidden")
        if role in MUTATOR_ROLES and mutation != "allowed":
            raise HelperError(f"mutator role {role} requires mutation=allowed")
        if role in MUTATOR_ROLES and worktree not in {"new-child", "new-top-level"}:
            mutator_locations.setdefault(worktree, []).append(worker_id)
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
        if "knownFailureModes" in worker:
            normalized_worker["knownFailureModes"] = validated_failure_modes(
                worker["knownFailureModes"],
                f"workers[{index}].knownFailureModes",
            )
        if name is not None:
            normalized_worker["name"] = name
        if base_branch is not None:
            normalized_worker["baseBranch"] = base_branch
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
    worker_ids = {worker["id"] for worker in normalized_workers}
    dependency_graph: dict[str, list[str]] = {}
    for position, worker in enumerate(normalized_workers, start=1):
        depends_on = string_list(
            worker.get("dependsOn"), f"workers[{position}].dependsOn"
        )
        for dependency in depends_on:
            if dependency not in worker_ids:
                raise HelperError(
                    f"workers[{position}].dependsOn references unknown worker "
                    f"{dependency!r}"
                )
            if dependency == worker["id"]:
                raise HelperError(f"worker {worker['id']!r} cannot depend on itself")
        if depends_on:
            worker["dependsOn"] = depends_on
        else:
            worker.pop("dependsOn", None)
        dependency_graph[worker["id"]] = depends_on
    for start in dependency_graph:
        trail: list[str] = []
        seen: set[str] = set()

        def walk_deps(node: str) -> None:
            if node in trail:
                raise HelperError(
                    "dependsOn cycle: " + " -> ".join([*trail, node])
                )
            if node in seen:
                return
            trail.append(node)
            for child in dependency_graph.get(node, []):
                walk_deps(child)
            trail.pop()
            seen.add(node)

        walk_deps(start)

    def reaches(source: str, target: str) -> bool:
        pending, visited = [source], set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node in visited:
                continue
            visited.add(node)
            pending.extend(dependency_graph.get(node, []))
        return False

    for selector, sharers in mutator_locations.items():
        for first_index in range(len(sharers)):
            for second_index in range(first_index + 1, len(sharers)):
                first, second = sharers[first_index], sharers[second_index]
                if not (reaches(first, second) or reaches(second, first)):
                    raise HelperError(
                        f"mutators {first!r} and {second!r} share worktree "
                        f"{selector!r} without a dependsOn ordering; parallel "
                        "mutators need separate worktrees"
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
            "current"
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


