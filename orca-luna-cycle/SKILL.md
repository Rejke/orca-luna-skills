---
name: orca-luna-cycle
description: >-
  Run up to 10 worker agents in Orca terminals under one GPT-5.6 Sol xhigh
  controller. Each role has a fixed model: Luna max writes backend code and
  business logic, Claude Fable 5 high builds frontend UI, and Sol xhigh
  reviews every change. A Python helper spawns the workers itself, gives each
  a prompt file, collects report files, and writes every step to the receipt
  journal. Use for parallel implementation in Orca worktrees, independent
  audits or benchmarks, anti-slop review, and repair/re-review loops. Do not
  use for an unsupervised full handoff.
---

# Orca Luna Cycle

Sol (the controller) makes the decisions: how to split the work, what each worker
does, and the final verdict. The helper script `scripts/orca_luna_worker.py` does the
repeatable steps. The helper spawns each worker itself in a plain Orca terminal —
there is no orchestration layer, no injected preamble, and no mail. Workers read
their prompt from a file and write their report to a file; only short one-line
commands ever cross the Windows PowerShell bridge.

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
- The helper is the launch authority: it records the exact spawn command for every
  worker, and `finalize-wave` proves the launch by comparing that record to the spec.
  The boot banner captured from the terminal is secondary evidence.
- Do not poll workers. A worker pings Sol once after it writes its report file. Sol
  runs `collect-reports` only after a ping. Never watch terminals in a loop.
- Preflight copies the helper into `<receipts>/runtime/helper.py` and stores its
  SHA-256 in the journal. Every later command for that wave (collect, answer, notify,
  stop, resume, finalize) runs that copy; the worker's wake command already points to
  it. So a skill upgrade cannot break a running wave. The live helper refuses a wave
  from a different build and prints the path of the copy.

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
values and spawns each agent with that exact command. Workers run without the
agent's own sandbox and approval prompts (`--dangerously-bypass-approvals-and-sandbox`
for codex, `--dangerously-skip-permissions` for claude): nobody watches a worker
terminal to click an approval, so a sandbox prompt would hang the worker forever.
The worktree split and the role rules are the safety boundary.

- `luna-max` — codex `gpt-5.6-luna` at `model_reasoning_effort=max`. The default
  for scout, implementer, integrator, and fixer. Use it for frontend business logic
  and all backend work.
- `luna-fast` — the same Luna `max` plus `-c service_tier=priority` (the 1.5x speed
  tier; costs more usage). Use it only when the user asked for fast mode. Valid for
  the same roles as `luna-max`.
- `fable-high` — claude `claude-fable-5`. Use it for frontend UI work (components,
  layout, styling, interaction). Valid for the same roles as `luna-max`.
- `sol-xhigh` — codex `gpt-5.6-sol` at `model_reasoning_effort=xhigh`. Fixed for
  every reviewer and anti-slop worker. No override. All review runs on Sol, never on
  Luna.

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

Optional `envelope.knownFailureModes`: up to 6 learned rules (240 characters each,
1000 total). Get them from the feedback skill's `rules` command. The renderer adds
them to every worker prompt as a KNOWN FAILURE MODES section. Rules travel through
the manifest, so a prompt can always be rebuilt from the manifest alone.

Keep the manifest lean. The rendered prompt already carries the role charter, the
report contract, the verdict rules, and the learning arrays — do not restate any of
them in acceptance criteria, constraints, or checks. A paraphrase drifts and
becomes a second, conflicting contract. One acceptance criterion is one testable
statement, not a paragraph. To protect earlier work, write one line — "Preserve
all behavior accepted at <reviewedAnchor>" — instead of listing past wins; that
list grows every wave and never shrinks. FINDINGS is the canonical list: refer to
findings by number in acceptance criteria and checks ("AC1: finding 1 is
root-cause fixed with a production-entrypoint regression test"); do not retell a
finding's content there, and do not repeat the owned file list outside SCOPE and
OWNERSHIP. Aim for under 6000 characters per worker spec; preflight and
`--dry-run` flag any spec over 8000.

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

The helper renders a full built-in charter for every reviewer and anti-slop worker
(adapted from 1F47E/rival): defect focus areas, generated-code checks, and the
seven anti-slop angles. The manifest adds only lens, scope, and criteria — do not
copy charter text into the manifest.

## Preflight: required, changes nothing

Run preflight before dispatch:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py preflight \
  --manifest /tmp/<wave>.json --receipt-dir /tmp/<wave>-receipts
```

Preflight changes nothing in Orca. It checks the manifest and report contracts, the
minimum Orca version, the exact command and flag grammar for the terminal and
worktree commands, the CLI identity, every launch spec the wave uses (Codex catalog
for codex specs; the `claude` and `uv` executables), and every worktree selector and
setup value. It writes `preflight.json` and archives the helper.

Dispatch refuses a missing, failed, or stale preflight. Right before its first change
it repeats the read-only checks and stops if the executable, runtime, version,
contract, or manifest hash changed.

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

For each worker the helper: creates the worktree when the manifest asks for one
(`worktree create`, with setup); creates a visible Orca terminal tab that starts the
agent with the exact spawn command (`terminal create --command`); waits for the
agent to reach idle; captures the boot banner as launch evidence; writes the full
prompt to `prompts/<worker>.txt`; and sends the terminal one short line — "Read the
file <path> and do exactly what it says." The prompt itself never crosses the
PowerShell bridge. `receipt-dir` is the only journal and the only resume handle:

```text
preflight.json
manifest.json
wave-state.json
wave-state.lock
cancel.requested.json                 # present only after a stop request
prompts/
worktrees/
terminals/
reports/                              # accepted reports, one per worker
reports/incoming/                     # the files workers write
answers/
notifications/
runtime/                              # archived helper, boot/banner/send receipts
controller-notification.pending.json  # present only while one wake is in flight
final.json
```

The helper sorts failures into `rejected_no_effects`, `failed_with_known_effects`,
and `outcome_unknown`. Only the last one is unclear by nature.

## Collect reports

After dispatch, return Sol to an idle prompt. Do not start a wait command. Each
worker's prompt tells it to write its report to `reports/incoming/<worker>.json` and
then run the helper's `notify-controller` wake command. The wake sends Sol one short
prompt; the report file carries everything.

When `[ORCA LUNA CYCLE: REPORTS READY]` arrives, run one collect:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py collect-reports \
  --receipt-dir /tmp/<wave>-receipts
```

Collect reads every new or changed report file, validates it against schema v1,
stores the accepted copy in `reports/`, and updates the wave state. It never waits.
A report from a worker that already finished never replaces the accepted copy; the
change is surfaced instead (`changedAfterDone`). A valid rewrite may replace an
invalid report. `taskStatus` values: `done` and `failed` settle the worker; a
reviewer's `FAIL` verdict is a statement about the code, and `failed` means the job
itself failed.

Follow the `next` field. `action_required` lists invalid reports, failures,
material verdicts, and questions. Handle them, then return to idle until the next
wake. Never run collect again just because nothing arrived.

## Questions: blocked workers stay warm

A worker that truly needs a decision writes a report with `taskStatus: "blocked"`
and a one-sentence `question` field, pings, and stays idle in its terminal. Its
session and context survive. Write your answer to a file with the Write tool, then:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py answer \
  --receipt-dir /tmp/<wave>-receipts --worker <worker-id> --file /tmp/<answer>.txt
```

The helper sends the worker one short line pointing at the answer file. The worker
continues the same task with full context — no fresh session, no lost work — and
writes its final report, which replaces the blocked one. Workers have no other
question channel; there is no ask and no mail.

## Finalize before Sol's verdict

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py finalize-wave \
  --receipt-dir /tmp/<wave>-receipts
```

`finalize-wave` checks: preflight passed, every worker spawned, every worker settled
(`done` or `failed`), every report valid, no open questions, no pending wake, every
spawn command equal to its launch spec, no ambiguous effects, and — when it can
check this itself — that a read-only wave left `current` unchanged. It writes
`final.json` and returns `ready_for_sol_gate` only when all of that holds. For waves
that changed files or used several worktrees, Sol must check the integration anchor.

## Stop and resume

On user stop: interrupt the kept launcher session, then run at once:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py stop-wave \
  --receipt-dir /tmp/<wave>-receipts
```

The helper writes the cancel marker before it calls Orca, then closes every spawned
worker terminal. If the result is `cancel_pending`, let the interrupted call finish
and run the same command again; it is safe to repeat. Never resume a cancelled wave.

Resume only steps that are clearly safe to repeat, from the same journal. If the
skill changed since dispatch, use `<receipts>/runtime/helper.py` instead of the
skill path:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py resume-wave \
  --receipt-dir /tmp/<wave>-receipts
```

Never repeat a worktree create or terminal create whose outcome is unknown.

## Implementation loop

```text
IMPLEMENT -> INTEGRATE (only if multiple mutators) -> FRESH REVIEW
  PASS             -> Sol sanity gate
  FAIL / UNKNOWN   -> fresh bounded fixer -> integrate -> different fresh reviewer
  BLOCKED          -> answer into the same terminal -> final report
```

A fixer gets the original envelope plus the exact findings, not the full history.
Stop automatic repair after `repairBudget` tries; running out of tries means
`UNKNOWN`, not `BLOCKED`. The reviewed-anchor rule spans waves: after a fixer commit,
the next wave that changes files on that anchor needs a fresh `PASS` review recorded
as `reviewedAnchor`, or a `reviewOverride` that names the reason.

Use several review lenses only when the change warrants it: acceptance/bugs,
architecture/security, tests/DX/performance/accessibility, and anti-slop (quality
only). Anti-slop reports concrete cuts; the leanness score is information only; the
findings decide the verdict.

Reviewers also close the learning loop. `promptFeedback` (max 3 entries per report)
names a failure class and one checkable rule that would have prevented it, with
severity, scope tags, and a `gap` kind — not every mistake is fixed by a prompt.
`ruleFeedback` says which of the wave's known failure modes were violated, helped,
or should be retired. The feedback skill folds both into its rule registry.

## Final Sol gate

Read `final.json` and only the reports you need. Check: every AC is covered, the
reviewer was fresh, each shard was integrated once, nothing changed after review, the
anchor was reviewed, and which findings and risks remain. Return `PASS`, `BLOCKED`,
or `UNKNOWN` with short proof. State model and effort only from the journal's spawn
records.

After the verdict, if the user wants a retro or the wave taught something, write and
archive feedback with the `orca-luna-feedback` skill. Read its log first when you
change this skill.

After changing this skill, run:

```text
uv run --no-project <skill>/scripts/orca_luna_worker.py self-test
uv run --no-project <skill>/tests/test_helper.py
```
