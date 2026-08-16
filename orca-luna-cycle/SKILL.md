---
name: orca-luna-cycle
description: >-
  Coordinate an adaptive supervised Orca swarm with one GPT-5.6 Sol xhigh
  controller and up to 10 fresh workers under a pinned launch policy: GPT-5.6
  Luna max implements frontend business logic and all backend work, Claude
  Fable 5 high builds frontend UI, and GPT-5.6 Sol xhigh reviews every change.
  Use for parallel implementation in Orca worktrees, independent audits or
  benchmarks, anti-slop review, repair/re-review loops, and compact proof. A
  durable Python helper handles preflight, dispatch, lifecycle receipts, and
  final mechanical reconciliation; workers never load this orchestrator skill.
  Do not use for an unsupervised full handoff.
---

# Orca Luna Cycle

Sol owns policy, decomposition, decisions, and the user-facing verdict. Keep deterministic
mechanics in `scripts/orca_luna_worker.py`; juniors receive only Orca's lifecycle preamble
and their compact task/report contract.

## Hard invariants

- Controller: `gpt-5.6-sol` at `xhigh`. Every worker launches fresh at its exact
  pinned launch spec (see Launch policy); reviewer and anti-slop workers always run
  `gpt-5.6-sol` at `xhigh`. Never silently downgrade or substitute.
- Maximum 10 live worker Dispatches. Fresh session for every role transition, fixer,
  and re-review.
- Workers never dispatch, load this skill, expand scope, or decide the final DAG/verdict.
- Parallel mutators require disjoint ownership and separate Orca worktrees. Multiple
  strictly read-only workers may share `current`; they must not build, format, install,
  generate caches, or mutate files/services.
- Preserve user state. Never reset, clean, stash, discard, push, publish, or open a PR.
- Never mutate an unreviewed anchor silently: a wave with mutators must declare
  `envelope.reviewedAnchor` (equal to `baseAnchor`, PASSed by a fresh reviewer) or an
  explicit `envelope.reviewOverride` reason; the helper refuses otherwise.
- Orca owns lifecycle authority. Task/Dispatch IDs and launch provenance come from Orca
  receipts; terminal handles are routing only.
- Never poll the coordinator inbox. Workers queue a wake-only continuation after accepted
  lifecycle mail; Sol drains only after that real input arrives.
- Never patch the helper while its Run has Tasks. A rejected exact launch is a verified
  blocker for that wave, not permission to guess or downgrade.

## Load the live contract once

Before the first Orca operation in a controller turn, completely load the `orca-cli` and
`orchestration` skills, resolve one CLI executable, and read both version-matched live
guides with `skills get`. Cache that understanding for the tuple:

`CLI executable + runtimeId + appVersion + orchestration contract hash`.

Do not reread unchanged guides after every delivery/retry. Reread after runtime restart,
executable/version/contract change, or an unsupported-command receipt. Live command
availability, receipt semantics, and safety constraints override this document, never the
requested model/effort. Generic coordinator-loop recipes do not override this skill's
push-only policy.

The live guide's rolling `check --wait` loop is the generic manual-supervision path. This
skill deliberately uses a narrower push adapter: structured lifecycle data remains in the
Run mailbox, while `terminal send` queues only a coalesced wake prompt to Sol. This does
not turn terminal input into lifecycle authority. Never use `check --wait`, timeout windows,
sleep, or model-visible polling in this skill's normal flow.

## Choose mode and swarm size

- `implementation`: mutators, integration, fresh review, bounded repair/re-review. The
  helper rejects an implementation wave without mutators.
- `audit`: read-only evidence gathering, including pure review and scout waves.
  Reviewer `FAIL` is a content result; never launch a fixer automatically.
- `benchmark`: explicitly requested independent read-only lenses/count. It may use all 10
  even when a smaller production swarm would suffice; record `benchmarkReason`.

For normal work choose the smallest useful wave: 1 for a tight shard, 2-3 for medium,
4-6 for genuinely independent surfaces, and 7-10 only for repo-scale decomposition or an
explicit benchmark. Use scouts only when Sol lacks enough evidence to decompose safely.

## Launch policy

Each worker's optional `launch` field selects one pinned spec; the helper rejects
anything else and finalize proves the exact requested/effective launch from Orca
receipts:

- `luna-max` — agent `codex`, `gpt-5.6-luna` at `max`. Default for scout,
  implementer, integrator, and fixer. Use it for frontend business logic and all
  backend work.
- `terra-xhigh` — agent `codex`, `gpt-5.6-terra` at `xhigh`. Opt-in balanced
  speed/cost/intelligence tier for implementation-side roles: harder shards that
  outgrow Luna but do not need Sol.
- `fable-high` — agent `claude`, `claude-fable-5` at `high`. Opt-in for frontend UI
  development shards (components, layout, styling, interaction); valid for the same
  implementation-side roles.
- `sol-xhigh` — agent `codex`, `gpt-5.6-sol` at `xhigh`. Pinned for every reviewer
  and anti-slop worker with no override, so all change review runs on Sol, never Luna.

Preflight verifies codex-agent specs against the Codex model catalog, and checks the
`claude` executable (override with `ORCA_LUNA_CLAUDE_COMMAND`) and `uv` on PATH.

## Build manifest v2

Create the manifest and receipt directory outside the repository. Define shared state once
in `envelope`: goal, non-goals, `AC<number>` definitions, constraints, base/dirty anchor,
integration destination, repair budget, and — for any wave with mutators — either
`reviewedAnchor` or `reviewOverride`. Each worker references AC IDs instead of
copying their prose. Unknown fields, duplicate worker IDs/worktree names, unknown ACs,
unsafe shared mutators, and mode violations fail before Orca mutation.

Get the full JSON Schema and a valid example; do not inspect helper source:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py schema --kind all
```

Inspect one generated junior prompt without contacting Orca:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py prompt \
  --manifest /tmp/<wave>.json --worker <worker-id>
```

Roles are `scout`, `implementer`, `integrator`, `reviewer`, `antislop`, and `fixer`.
Optional worker `launch` picks a pinned spec from the Launch policy; review roles
reject overrides. In review waves, give every commit range between the reviewed diff
and `baseAnchor` an explicit owning lens — an unowned intermediate range is a coverage
gap, because a later commit can re-break what an earlier reviewed commit fixed. Use `displayName` for a short human-readable tab title; otherwise the helper derives one
from worktree `name` or goal and prefixes index + role.

## Mandatory mutation-free preflight

Run preflight before live dispatch:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py preflight \
  --manifest /tmp/<wave>.json --receipt-dir /tmp/<wave>-receipts
```

It performs no Orca mutations. It checks manifest/report contracts, minimum Orca version,
runtime capabilities, exact argv command/flag grammar, CLI identity, every launch spec
the wave uses (Codex catalog for `luna-max`/`sol-xhigh`, `claude` and `uv` executables
for `fable-high` and the wake hook), and every worktree selector/setup shape. It writes `preflight.json`.

Live dispatch refuses a missing/failed/stale receipt. Immediately before mutation it
repeats the read-only checks and rejects changed executable/runtime/version/contract or
manifest hash. This prevents a bad launch from leaving Runs or Tasks merely to discover a
known incompatibility.

## Dispatch and journal

Validate offline first:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py dispatch-wave \
  --manifest /tmp/<wave>.json --dry-run
```

Then launch in a shell session that yields promptly and retain its session handle so a
user cancellation can interrupt it:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py dispatch-wave \
  --manifest /tmp/<wave>.json --receipt-dir /tmp/<wave>-receipts
```

After preflight, the helper creates or binds the Run, creates all Tasks, starts workers at
their exact pinned launch specs, and renames tabs. It uses argv arrays, never shell reconstruction.
`receipt-dir` is the sole durable journal and resume handle:

```text
preflight.json
manifest.json
run.json
wave-state.json
wave-state.lock
cancel.requested.json                 # present only after a stop request
tasks/
dispatches/
deliveries/
reports/
questions/
releases/
notifications/
runtime/
controller-notification.pending.json  # present only while one wake is in flight
final.json
```

Failure state distinguishes `rejected_no_effects`, `failed_with_known_effects`, and true
`outcome_unknown`. Only the last class is intrinsically ambiguous/unresolved.

## Process deliveries

After dispatch, return Sol to an idle prompt. Do not start a wait command. Each junior's
live Task prompt appends the helper's `notify-controller` hook to the exact injected
`worker_done` shell command with `&&`: Orca must accept lifecycle completion first, then
the helper atomically coalesces concurrent wakes and queues one short continuation into
Sol's terminal. The wake carries no report or verdict and cannot settle a Task. A failed
wake send clears the coalescing marker so a later completion can re-queue the wake; the
user may also run `drain-deliveries` manually at any time.

Do not use blocking `ask` in this push v1: it records mail and then strands the worker
without a reliable wake. A genuinely blocked junior completes once with
`taskStatus: "blocked"`, verified evidence, and the normal wake hook. Sol resolves the
decision and starts a fresh bounded continuation. This costs a fresh worker only on a
real blocker and keeps the no-polling invariant honest.

When Orca queues `[ORCA LUNA CYCLE: DELIVERY READY]` as new input, run exactly one
nonblocking drain:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py drain-deliveries \
  --receipt-dir /tmp/<wave>-receipts
```

The helper reads only mail already queued, writes every raw Delivery, verifies exact
Task/Dispatch identity, validates report schema v1, stores the full report, updates
`wave-state.json`, and automatically calls `worker-release` for an accepted
`worker_done`. Current Orca Deliveries carry lifecycle IDs and outcome inside the JSON
payload; the helper decodes them independently from the report contract and rejects
identity/outcome conflicts with top-level metadata or retained launch receipts. A
completion for an already-accepted worker is journaled as a duplicate and never mutates
accepted state, with one exception: a duplicate with matching identity and outcome and
a valid report repairs an invalid journaled report (`repairedReport`). A structured
report found inside a `status` message is journaled as side evidence and flagged
actionable (`misdirectedReport`) but never becomes validated completion evidence. A
rejected completion is explicit with `accepted: false`, rejection code, and expected
IDs. Lifecycle `succeeded`
means the assigned job completed; a reviewer may still report verdict `FAIL`, `UNKNOWN`,
or `BLOCKED`.

Workers use Orca's already-injected opaque lifecycle command/IDs and add only a compact
`--payload` report. Do not paste or reconstruct Task/Dispatch IDs in task prose. A
terminal-authority `worker-done` shorthand belongs in Orca itself, not this skill.

For a question, escalation, rejected completion, invalid report, failed release, failed
lifecycle, or material review verdict, the drain returns `action_required` without
acknowledging. Handle every message through the live Orca contract, then continue with:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py drain-deliveries \
  --receipt-dir /tmp/<wave>-receipts --ack <delivery-id>
```

The helper acknowledges and drains every immediately available next batch, including
unknown message types, then removes the coalescing marker and closes the publication race
with one final nonblocking read. If the wave is not settled, obey its `next` field and
return to idle until another queued wake arrives. Never call `wait` again because no mail
was available. The helper deliberately exposes no polling/wait command; `ack-delivery`
remains a diagnostic one-shot only and cannot wait.

## Finalize before Sol's verdict

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py finalize-wave \
  --receipt-dir /tmp/<wave>-receipts
```

`finalize-wave` reconciles Task/Dispatch state, accepted completions, valid reports,
release receipts, exact requested/effective launch (agent+model+effort), ambiguity, and the read-only
current-worktree anchor when mechanically verifiable. It writes `final.json` and returns
`ready_for_sol_gate` only when the orchestration mechanics are complete. Content verdicts
remain separate inputs; audit `FAIL` does not turn a healthy swarm into an orchestration
failure. For mutating/multi-worktree waves Sol must verify the integration anchor.

## Stop and resume safely

On user stop: first interrupt the retained launcher session, then immediately run:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py stop-wave \
  --receipt-dir /tmp/<wave>-receipts
```

The cancel marker is written before Orca calls. Known Dispatches are stopped and created
but undispatched Tasks are blocked. If a race returns `cancel_pending`, allow the interrupted
call to settle and run the same idempotent command again. Never resume a cancelled wave.

Resume only proven non-ambiguous pending steps from the same journal:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py resume-wave \
  --receipt-dir /tmp/<wave>-receipts
```

Never reconstruct IDs in a side file or replay an ambiguous create/start.

## Supervised implementation loop

```text
IMPLEMENT -> INTEGRATE (only if multiple mutators) -> FRESH REVIEW
  PASS             -> Sol sanity gate
  FAIL / UNKNOWN   -> fresh bounded fixer -> integrate -> different fresh reviewer
  BLOCKED          -> verify blocker -> Sol/user
```

Fixers receive the original envelope plus exact findings, not the orchestration history.
Stop automatic repair after `repairBudget`; exhaustion means `UNKNOWN`, not `BLOCKED`.
The reviewed-anchor gate spans waves: after a fixer commit, the next mutating wave on
that anchor needs a fresh PASS review recorded as `reviewedAnchor`, or a deliberate
`reviewOverride` that names why review is being skipped.

Use distinct review lenses only when material: acceptance/bugs, architecture/security,
tests/DX/performance/accessibility, and quality-only anti-slop. Anti-slop reports evidenced
cuts for duplication, unnecessary abstraction, silent fallback, speculative generality or
compatibility, wrong depth, reinvention, comment/wrapper slop, and wasted work. A high
leanness score is informational; concrete findings decide the verdict.

## Final Sol gate

Read `final.json` and the minimum needed reports. Verify criterion coverage, fresh reviewer
provenance, exactly-once integration, no mutation after review, reviewed anchor, remaining
findings, and residual risk. Return `PASS`, `BLOCKED`, or `UNKNOWN` with compact proof;
claim model/effort only from launch receipts.

After the verdict, when the user wants a retro or the wave produced material lessons,
write and archive feedback with the `orca-luna-feedback` skill; its durable log is also
the first read before maintaining this skill.

After maintaining the skill, run:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py self-test
uv run --no-project <skill>/tests/test_helper.py
```
