---
name: orca-luna-feedback
description: >-
  Write feedback after an orca-luna-cycle wave finishes. A helper script fills
  in the facts from the wave's receipt files; Sol adds the verdict, one keep
  and one change per worker, concrete changes for the next manifest, and any
  skill problems. Archiving fails while blanks remain, then copies the note to
  this skill's log. Read that log before changing orca-luna-cycle. Use after
  finalize-wave or when the user asks for wave feedback or a retro. Not a
  substitute for the Sol gate.
---

# Orca Luna Feedback

Base feedback on the wave's files, not on impressions. The helper extracts the facts;
Sol writes every opinion. If a note could have been written without reading the
journal, delete it instead of archiving it.

## Rules

- Scaffold only after `finalize-wave` wrote `final.json`; the helper refuses earlier.
  The helper never calls Orca and never changes the journal, except to add
  `feedback.md`.
- Every claim names its source: a receipt file, a report field, or path:line. Cut
  claims without a source before archiving.
- Keep two judgments apart: did the orchestration work, and is the code good. A
  healthy swarm with a `FAIL` review is mechanics PASS and content FAIL.
- Every next-wave suggestion must be a change someone can make in the next manifest:
  a shard boundary, AC wording, launch choice, repair budget, or worktree layout.
  "Communicate better" is not a change.
- Judge the worker prompts too. Read the rendered prompts in `prompts/` and say
  which goal/scope/AC/context wording helped or misled each worker. A prompt
  suggestion must be a concrete rewording or scope cut, cited against
  `prompts/<worker>.txt`.
- Skill problems go into their own section; they feed orca-luna-cycle maintenance.
  List contract friction, helper errors, and rules that got in the way of the task —
  each with the exact receipt path.
- Use only numbers the journal holds (workers, verdicts, findings by severity,
  repair rounds). Never invent a number.

## Workflow

Scaffold the facts. The helper refuses to overwrite an existing note:

```text
uv run --no-project <skill>/scripts/feedback.py scaffold \
  --receipt-dir /tmp/<wave>-receipts
```

Edit `feedback.md` in the receipt directory. Replace every `<fill: ...>` placeholder
with your findings, based on `final.json` and `reports/`. Delete optional sections
that have nothing concrete.

Archive the note. This fails while any placeholder remains, then copies the note into
this skill's `log/`:

```text
uv run --no-project <skill>/scripts/feedback.py archive \
  --receipt-dir /tmp/<wave>-receipts
```

Before changing orca-luna-cycle, read the collected log:

```text
uv run --no-project <skill>/scripts/feedback.py log --tail 5
```

## Learned rules

`archive` also folds each report's `promptFeedback` into `log/patterns.json`:

- A critical or high rule activates after one confirmation. A medium or low rule
  activates after two waves with different Run IDs.
- Entries whose `gap` is not `prompt` are counted per gap kind, not turned into
  rules.
- The same failure class merges into one rule. The registry keeps severity,
  scopes, counts, sources, and first/last seen.
- At most 24 rules can be active. When the cap blocks an activation, retire
  something first.

Pick rules for the next wave manifest:

```text
uv run --no-project <skill>/scripts/feedback.py rules --scopes native,publication
```

Put the returned `forManifest` lines into `envelope.knownFailureModes`. Retire a
rule when reviewers report it obsolete:

```text
uv run --no-project <skill>/scripts/feedback.py rules --retire <id> --reason "..."
```

A rule must be one instruction a worker can follow and a reviewer can check.
"Be careful with URLs" is not a rule. These are:

- Trace producer -> proxy -> consumer for each URL and use one shared fixture.
- Treat the signed manifest as the source of truth; a receipt cannot shrink the
  required inventory.
- Show a production call site; a test-only function proves nothing.
- Join every spawn_blocking task before the lease is released or returned.

After changing this skill, run:

```text
uv run --no-project <skill>/scripts/feedback.py self-test
```
