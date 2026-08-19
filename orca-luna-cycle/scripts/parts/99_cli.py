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

    brief = commands.add_parser(
        "plan-brief", help="print the authored plan content for a planreviewer"
    )
    brief.add_argument("--manifest", type=Path, required=True)
    brief.set_defaults(func=command_plan_brief)

    plan_review = commands.add_parser(
        "plan-review-manifest",
        help="emit a plan-review wave manifest with the fixed mission and ACs",
    )
    plan_review.add_argument("--plan", required=True, help="plan file under review")
    plan_review.add_argument(
        "--prior",
        action="append",
        help="prior review report to re-check; repeatable",
    )
    plan_review.add_argument(
        "--prior-plan",
        action="append",
        help="the plan version the prior report reviewed (from that wave's "
        "receipts scope/); repeatable",
    )
    plan_review.add_argument(
        "--hint",
        action="append",
        help="optional focus hint for worker context; never an AC; repeatable",
    )
    plan_review.add_argument("--worker-id", default="plan_reviewer")
    plan_review.set_defaults(func=command_plan_review_manifest)

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

    status = commands.add_parser(
        "status", help="diagnose worker liveness on demand; changes nothing"
    )
    status.add_argument("--receipt-dir", required=True)
    status.set_defaults(func=command_status)

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

    rebind = commands.add_parser(
        "rebind-controller",
        help="controller-only: retarget wakes after a controller restart",
    )
    rebind.add_argument("--receipt-dir", required=True)
    rebind.set_defaults(func=command_rebind_controller)

    finalize = commands.add_parser(
        "finalize-wave",
        help="reconcile the durable journal and emit the Sol gate receipt",
    )
    finalize.add_argument("--receipt-dir", required=True)
    finalize.set_defaults(func=command_finalize_wave)

    usage = commands.add_parser(
        "usage",
        help="collect per-worker token usage from agent session logs",
    )
    usage.add_argument("--receipt-dir", required=True)
    usage.set_defaults(func=command_usage)

    cleanup = commands.add_parser(
        "cleanup-worktrees",
        help="remove the wave's created worktrees once integrated",
    )
    cleanup.add_argument("--receipt-dir", required=True)
    cleanup.add_argument(
        "--force",
        action="store_true",
        help="remove even dirty or unmerged worktrees",
    )
    cleanup.set_defaults(func=command_cleanup_worktrees)

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
