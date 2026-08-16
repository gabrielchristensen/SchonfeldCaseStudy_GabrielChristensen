---
name: sync-documentation
description: Use after making any change to src/, tests/, requirements.txt, notebooks/, or pipeline behavior in this repository, to keep docs/full_project_documentation.md in sync with the code -- not just at the end of a large task, every time. Triggers on adding/changing a function or module, a dependency, CLI behavior/defaults, a real bug fix, a new notebook, or a new data artifact.
---

# Keep documentation in sync with code changes

`docs/full_project_documentation.md` is this repo's evaluator-facing
technical reference — a running log of what was built, why, and what
was verified. It has gone stale before after a code change landed
without a matching documentation update in the same turn. This skill
exists to stop that from happening again.

## When to apply

Any time you:
- Add, remove, or meaningfully change a function/module in `src/`.
- Add or change a dependency in `requirements.txt`.
- Change CLI behavior, defaults, or output format of any `src/*.py`
  entry point.
- Fix a real bug (not a typo) — especially one found via real testing.
- Add a new notebook, new test file, or new committed data artifact.

## What to do

1. **Before considering the task done**, check whether
   `docs/full_project_documentation.md` needs an update:
   - New `###`-level subsection under "2. Chronological Narrative" for
     genuinely new work — mirror the existing style: state what was
     found/built, why, and what was verified, not just what changed.
   - Edit an existing section if this change corrects or extends
     something already documented there, instead of leaving the old
     text to silently contradict the new code.
   - A new row in the "Parameters at a glance" table (§1.3) if a
     parameter's value or meaning changed.
   - A new row in the "Defense Quick-Reference Index" (§3) if the
     change bears on one of the case study prompt's stated evaluation
     criteria.
2. **Update the Table of Contents** if a new `###`-level heading was
   added.
3. Match the file's existing tone: state what was *verified* (real
   data, real tests, real re-runs, real numbers) — not just what was
   implemented. This file's whole value is that every claim in it is
   backed by something checked, not asserted.
4. Commit the documentation update in the same commit as the code
   change where practical, or as an immediate follow-up commit
   otherwise — don't let a turn end with code changed and docs stale.
5. Use judgment on triviality: a comment fix, a lint fix, or reverting
   a no-op timestamp diff doesn't need a documentation entry. Don't pad
   the file with noise.

## Note on enforcement

This is a self-applied checklist, not a hook — nothing blocks a commit
if it's skipped. For harness-enforced guarantees (e.g. blocking a
commit that touches `src/` without a matching `docs/` change in the
same diff), a git pre-commit hook or a Claude Code hook in
`settings.json` would be needed instead — ask if that stronger
enforcement is wanted.
