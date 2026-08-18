---
name: orca-luna-cycle
description: >-
  Run up to 10 Orca worker agents under one GPT-5.6 Sol xhigh controller.
  Each role has a fixed model: Luna max writes backend code and business
  logic, Claude Fable 5 high builds frontend UI, and Sol xhigh reviews every
  change. A Python helper checks the setup before launch, starts the workers,
  collects their reports, and writes every step to files. Use for parallel
  implementation in Orca worktrees, independent audits or benchmarks,
  anti-slop review, and repair/re-review loops. Do not use for an
  unsupervised full handoff.
---

# Orca Luna Cycle

Sol (the controller) makes the decisions: how to split the work, what each worker
does, and the final verdict. The helper script `scripts/orca_luna_worker.py` does the
repeatable steps. Workers get only Orca's lifecycle preamble and a short task prompt.

## Rules

- The controller is `gpt-5.6-sol` at `xhigh`. Each worker starts fresh with the exact
  model and effort from its launch spec (see Launch policy). Reviewer and anti-slop
  workers always run `gpt-5.6-sol` at `xhigh`. Never swap in a weaker model or effort
  without telling the user.
- At most 10 workers at the same time. Start a fresh session for every role change,
  fixer, and re-review.
- Workers never start other workers, never load this skill, never widen their own
  scope, and never decide the final verdict.
- Workers that change files in parallel must own separate files and separate Orca
  worktrees. Read-only workers may share `current`, but they must not build, format,
  install, write caches, or change files or services.
- Worktree rules: a wave with one mutator works in `current` by default. A mutator in
  a new worktree needs an integrator in the same wave; the helper rejects the manifest
  otherwise. After the Sol gate, merge each wave worktree exactly once and delete it.
  A leftover worktree is a bug. `finalize-wave` lists them in
  `final.json#createdWorktrees`.
- Keep the user's files as they are. Never reset, clean, stash, discard, push,
  publish, or open a PR.
- Do not change code on top of an unreviewed commit without saying so: a wave with
  mutators must set `envelope.reviewedAnchor` (equal to `baseAnchor`, passed by a
  fresh reviewer) or give a reason in `envelope.reviewOverride`. The helper rejects
  the manifest otherwise.
- Orca decides what counts as started and finished. Take Task/Dispatch IDs and model
  facts only from Orca's JSON output. A terminal handle is only an address.
- Do not check for mail in a loop. A worker pings Sol once after Orca accepts its
  completion. Sol reads mail only after a ping.
- Preflight copies the helper into `<receipts>/runtime/helper.py` and stores its
  SHA-256 in the journal. Every later command for that wave (drain, notify, stop,
  resume, finalize) runs that copy; the worker ping already points to it. So a skill
  upgrade cannot break a running wave. The live helper refuses a wave from a
  different build and prints the path of the copy. A rejected launch is a blocker for
  that wave. Do not guess and do not downgrade.

## Load the live contract once

Before the first Orca call in a controller turn, load the `orca-cli` and
`orchestration` skills, pick one CLI executable, and read both live guides with
`skills get`. Remember the result for this tuple:

`CLI executable + runtimeId + appVersion + orchestration contract hash`.

Do not reread unchanged guides after every delivery or retry. Reread after a runtime
restart, a changed executable/version/contract, or an unsupported-command response.
The live guides win on command availability, response fields, and safety rules. They
never change the required model or effort. Generic coordinator-loop recipes do not
override this skill's no-polling rule.

The live guide describes a `check --wait` loop for manual supervision. This skill does
not use it: reports stay in the Run mailbox, and `terminal send` only sends Sol one
short ping. A ping is not proof that a task finished. Never use `check --wait`,
timeouts, sleep, or any polling in this skill's normal flow.

## Choose mode and swarm size

- `implementation`: workers change files, integrate, review, and repair. The helper
  rejects an implementation wave without mutators.
- `audit`: read-only work, including pure review and scout waves. A reviewer `FAIL`
  is a statement about the code. Never start a fixer automatically.
- `benchmark`: the user asked for a fixed number of independent read-only workers. It
  may use all 10. Record the reason in `benchmarkReason`.

Pick the smallest wave that works: 1 for a small task, 2-3 for a medium one, 4-6 for
truly independent parts, 7-10 only for repo-wide work or a benchmark. Use scouts only
when Sol does not know enough to split the work safely.

## Launch policy

The optional worker field `launch` picks one fixed spec. The helper rejects other
values. `finalize-wave` proves the requested and actual launch from Orca's output:

- `luna-max` — agent `codex`, `gpt-5.6-luna` at `max`. The default for scout,
  implementer, integrator, and fixer. Use it for frontend business logic and all
  backend work.
- `luna-fast` — the same Luna `max`, started with `worker-start --fast` (the 1.5x
  speed tier; costs more usage). Use it only when the user asked for fast mode.
  Valid for the same roles as `luna-max`. Never put the tier into the model name:
  the API rejects a `[fast]` suffix. In codex itself the tier is the config value
  `service_tier = "priority"`. Preflight requires the Orca capability
  `orchestration.worker-fast-mode.v1`; without it, a `luna-fast` wave stops at
  preflight.
- `fable-high` — agent `claude`, `claude-fable-5` at `high`. Use it for frontend UI
  work (components, layout, styling, interaction). Valid for the same roles as
  `luna-max`.
- `sol-xhigh` — agent `codex`, `gpt-5.6-sol` at `xhigh`. Fixed for every reviewer and
  anti-slop worker. No override. All review runs on Sol, never on Luna.

Preflight checks codex specs against the Codex model catalog. It also checks that the
`claude` executable (set `ORCA_LUNA_CLAUDE_COMMAND` to override) and `uv` are on
PATH.

## Build manifest v2

Create the manifest and the receipt directory outside the repository. Put shared
facts once into `envelope`: goal, non-goals, `AC<number>` definitions, constraints,
base/dirty anchor, integration destination, repair budget, and — for any wave with
mutators — `reviewedAnchor` or `reviewOverride`. Each worker lists AC IDs; it does
not copy their text. The helper rejects unknown fields, duplicate worker IDs or
worktree names, unknown ACs, unsafe shared mutators, and wrong modes before it
touches Orca.

Print the JSON Schema and a valid example; do not read the helper source:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py schema --kind all
```

Print one generated worker prompt without contacting Orca:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py prompt \
  --manifest /tmp/<wave>.json --worker <worker-id>
```

Roles are `scout`, `implementer`, `integrator`, `reviewer`, `antislop`, and `fixer`.
The optional `launch` field picks a spec from the Launch policy; review roles reject
overrides. In a review wave, give every commit range between the reviewed diff and
`baseAnchor` to a named reviewer. A range with no owner is a hole in coverage: a
later commit can break what an earlier commit fixed. Use `displayName` for a short
tab title; otherwise the helper builds one from the worktree `name` or the goal, with
index and role in front.

## Preflight: required, changes nothing

Run preflight before dispatch:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py preflight \
  --manifest /tmp/<wave>.json --receipt-dir /tmp/<wave>-receipts
```

Preflight changes nothing in Orca. It checks the manifest and report contracts, the
minimum Orca version, runtime capabilities, the exact command and flag grammar, the
CLI identity, every launch spec the wave uses (Codex catalog for
`luna-max`/`sol-xhigh`; the `claude` and `uv` executables for `fable-high` and the
ping hook), and every worktree selector and setup value. It writes `preflight.json`.

Dispatch refuses a missing, failed, or stale preflight. Right before its first change
it repeats the read-only checks and stops if the executable, runtime, version,
contract, or manifest hash changed. So a known problem cannot leave half-created Runs
or Tasks behind.

## Dispatch and journal

Validate offline first:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py dispatch-wave \
  --manifest /tmp/<wave>.json --dry-run
```

Then launch from a shell session that returns promptly, and keep that session handle
so the user can interrupt it:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py dispatch-wave \
  --manifest /tmp/<wave>.json --receipt-dir /tmp/<wave>-receipts
```

After preflight the helper creates or binds the Run, creates all Tasks, starts all
workers with their exact launch specs, and renames the tabs. It passes argv arrays;
it never builds shell strings. `receipt-dir` is the only journal and the only resume
handle:

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

The helper sorts failures into `rejected_no_effects`, `failed_with_known_effects`,
and `outcome_unknown`. Only the last one is unclear by nature.

## Process deliveries

After dispatch, return Sol to an idle prompt. Do not start a wait command. Each
worker's task prompt appends the helper's `notify-controller` hook to the injected
`worker_done` command with `&&`. So Orca accepts the completion first; then the
helper merges parallel pings into one and sends Sol one short prompt. The ping
carries no report and no verdict, and it cannot finish a Task. If the ping fails to
send, the helper clears the merge marker, and the next completion pings again. The
user can also run `drain-deliveries` by hand at any time.

Do not use blocking `ask`: it records mail and then leaves the worker hanging without
a ping. A worker that is truly blocked completes once with `taskStatus: "blocked"`
and its evidence, using the normal hook. Sol answers and starts a fresh follow-up
worker. This costs one extra worker, and only on a real blocker.

When Orca puts `[ORCA LUNA CYCLE: DELIVERY READY]` into Sol's input, run one drain:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py drain-deliveries \
  --receipt-dir /tmp/<wave>-receipts
```

The drain reads only mail that is already queued. It writes every raw Delivery,
checks the Task/Dispatch identity, validates the report against schema v1, stores the
report, updates `wave-state.json`, and calls `worker-release` for each accepted
`worker_done`. Deliveries carry the lifecycle IDs and the outcome inside the JSON
payload. The helper decodes them separately from the report and rejects identity or
outcome conflicts with the top-level fields or with the stored launch data. A
completion for a worker that already completed is stored as a duplicate and changes
nothing — with one exception: if the stored report was invalid, a duplicate with the
same identity and outcome and a valid report replaces it (`repairedReport`). A report
inside a `status` message is stored as side evidence and flagged
(`misdirectedReport`); it never counts as the completion report. A rejected
completion shows `accepted: false`, a rejection code, and the expected IDs.
Lifecycle `succeeded` means the worker finished its job; a reviewer can still report
`FAIL`, `UNKNOWN`, or `BLOCKED`.

Workers use the lifecycle command and IDs that Orca injected, and add one `--payload`
with the report. The whole report goes into that one `--payload`; it cannot be
combined with flags like `--files-modified`. As a fallback the drain also accepts a
report as JSON in `--body` or in a file named by `--report-path` (absolute path, size
limit). "Exactly one completion" counts accepted completions: if the CLI rejects a
send with no effects, fix the command from the error text and send once more. Do not
read CLI internals to debug it. Do not paste or rebuild Task/Dispatch IDs in the task
text.

If the drain finds a question, an escalation, a rejected completion, an invalid
report, a failed release, a failed lifecycle, or a review verdict that needs action,
it returns `action_required` and does not acknowledge. Handle each message, then
continue with:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py drain-deliveries \
  --receipt-dir /tmp/<wave>-receipts --ack <delivery-id>
```

The helper acknowledges, drains every batch that is already available (including
unknown message types), removes the merge marker, and does one final read to close
the race. If the wave is not settled, follow the `next` field and go back to idle
until the next ping. Never run the drain again just because there was no mail. The
helper has no wait command on purpose; `ack-delivery` is a one-shot debug command and
cannot wait.

## Finalize before Sol's verdict

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py finalize-wave \
  --receipt-dir /tmp/<wave>-receipts
```

`finalize-wave` checks Task and Dispatch state, accepted completions, valid reports,
releases, the exact requested and actual launch (agent, model, effort), open
questions, and — when it can check this itself — that a read-only wave left `current`
unchanged. It writes `final.json` and returns `ready_for_sol_gate` only when all of
that holds. A reviewer's `FAIL` means the code is bad, not that the wave broke; keep
the two judgments apart. For waves that changed files or used several worktrees, Sol
must check the integration anchor.

## Stop and resume

On user stop: interrupt the kept launcher session, then run at once:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py stop-wave \
  --receipt-dir /tmp/<wave>-receipts
```

The helper writes the cancel marker before it calls Orca. It stops known Dispatches
and blocks Tasks that were created but not started. If the result is
`cancel_pending`, let the interrupted call finish and run the same command again; it
is safe to repeat. Never resume a cancelled wave.

Resume only steps that are clearly safe to repeat, from the same journal. If the
skill changed since dispatch, use `<receipts>/runtime/helper.py` instead of the skill
path:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py resume-wave \
  --receipt-dir /tmp/<wave>-receipts
```

Never rebuild IDs in a side file. Never repeat a create or start whose outcome is
unknown.

## Implementation loop

```text
IMPLEMENT -> INTEGRATE (only if multiple mutators) -> FRESH REVIEW
  PASS             -> Sol sanity gate
  FAIL / UNKNOWN   -> fresh bounded fixer -> integrate -> different fresh reviewer
  BLOCKED          -> verify blocker -> Sol/user
```

A fixer gets the original envelope plus the exact findings, not the full history.
Stop automatic repair after `repairBudget` tries; running out of tries means
`UNKNOWN`, not `BLOCKED`. The reviewed-anchor rule spans waves: after a fixer commit,
the next wave that changes files on that anchor needs a fresh `PASS` review recorded
as `reviewedAnchor`, or a `reviewOverride` that names the reason.

Use several review lenses only when the change warrants it: acceptance/bugs,
architecture/security, tests/DX/performance/accessibility, and anti-slop (quality
only). Anti-slop reports concrete cuts: duplication, needless abstraction, silent
fallback, speculative generality or compatibility, wrong depth, reinvention, comment
or wrapper filler, and wasted work. The leanness score is information only; the
findings decide the verdict.

## Final Sol gate

Read `final.json` and only the reports you need. Check: every AC is covered, the
reviewer was fresh, each shard was integrated once, nothing changed after review, the
anchor was reviewed, and which findings and risks remain. Return `PASS`, `BLOCKED`,
or `UNKNOWN` with short proof. State model and effort only from Orca's launch output.

After the verdict, if the user wants a retro or the wave taught something, write and
archive feedback with the `orca-luna-feedback` skill. Read its log first when you
change this skill.

After changing this skill, run:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py self-test
uv run --no-project <skill>/tests/test_helper.py
```
