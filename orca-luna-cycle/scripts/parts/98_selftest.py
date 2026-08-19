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
    assert ROLE_LAUNCHES["planreviewer"] == ("sol-xhigh",)
    assert "planreviewer" in REVIEW_ROLES
    assert "luna-fast" in ROLE_LAUNCHES["implementer"]
    assert all(launches[0] != "luna-fast" for launches in ROLE_LAUNCHES.values())
    assert not any(name.startswith("orchestration") for name in REQUIRED_ORCA_COMMANDS)
    assert REQUIRED_ORCA_COMMANDS["terminal create"] == {
        "worktree",
        "title",
        "command",
        "json",
    }
    assert spawn_command(LAUNCH_SPECS["luna-max"]) == (
        "codex --dangerously-bypass-approvals-and-sandbox "
        "-m gpt-5.6-luna -c model_reasoning_effort=max"
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
    missing_base = {
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
        validate_manifest(missing_base)
    except HelperError as exc:
        assert "baseBranch" in str(exc)
    else:
        raise AssertionError("new worktree without baseBranch was accepted")
    orphan_worktree = {
        **overridden,
        "workers": [
            {
                **overridden["workers"][0],
                "worktree": "new-child",
                "name": "impl-shard",
                "baseBranch": "main",
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
    plan_variant = {**reviewer_variant, "id": "plan", "role": "planreviewer"}
    _, plan_workers, plan_prompts = validate_manifest(
        {**manifest, "workers": [plan_variant]}
    )
    assert plan_workers[0]["launch"] == "sol-xhigh"
    assert "mislead an implementer" in plan_prompts[0]
    same_goal = {
        **manifest,
        "workers": [
            {**manifest["workers"][0], "goal": manifest["envelope"]["goal"]}
        ],
    }
    _, _, same_goal_prompts = validate_manifest(same_goal)
    assert "\nGOAL\n" not in same_goal_prompts[0]
    assert "MISSION\n" in same_goal_prompts[0]
    assert "Verification gaps" in plan_prompts[0]
    assert "scope creep and YAGNI" in plan_prompts[0]
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
    # Source of truth is scripts/parts/; the single-file bundle is generated.
    # Archived per-wave copies run without parts/ and skip this check.
    parts_dir = Path(__file__).resolve().parent / "parts"
    if parts_dir.is_dir():
        pieces: list[str] = []
        for part in sorted(parts_dir.glob("*.py")):
            text = part.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                text += "\n"
            if pieces:
                pieces.append(f"# ==== part: {part.name} ====\n")
            pieces.append(text)
        if "".join(pieces) != Path(__file__).read_text(encoding="utf-8"):
            raise AssertionError(
                "bundle differs from parts/; run scripts/build_helper.py"
            )
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


