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
- Verdict first: one sentence the reader can act on without other context.
- Every claim names its source: a receipt file, a report field, or path:line. Cut
  claims without a source before archiving.
- Keep two judgments apart: did the orchestration work, and is the code good. A
  healthy swarm with a `FAIL` review is mechanics PASS and content FAIL.
- Per worker: one keep and one change, tied to that worker's report. If there is
  nothing concrete to say, delete the section. Praise without an action item is
  noise.
- Every next-wave suggestion must be a change someone can make in the next manifest:
  a shard boundary, AC wording, launch choice, repair budget, or worktree layout.
  "Communicate better" is not a change.
- Judge the worker prompts too. Read the task specs in `tasks/` and say which
  goal/scope/AC/context wording helped or misled each worker. A prompt suggestion
  must be a concrete rewording or scope cut, cited against `tasks/<worker>.json`.
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
this skill's `log/`, because receipt directories in `/tmp` do not survive:

```text
uv run --no-project <skill>/scripts/feedback.py archive \
  --receipt-dir /tmp/<wave>-receipts
```

Before changing orca-luna-cycle, read the collected log:

```text
uv run --no-project <skill>/scripts/feedback.py log --tail 5
```

After changing this skill, run:

```text
uv run --no-project <skill>/scripts/feedback.py self-test
```
