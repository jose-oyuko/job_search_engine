# 11. Profile editor and CV import: service-layer versioning, generic HTMX sections, guarded parsing

- Status: Accepted
- Date: 2026-10-22

## Context

Phase 3 adds the master profile, tracks, an HTMX editor and CV import. The spec leaves open how
`profile.version` is bumped, how the six editable sections share code, how far the review screen
must be editable, and how to keep the parser from inventing facts.

## Decision

- **Versioning in the service layer, not signals.** Every mutation goes through `services.py`
  (`save_item`, `delete_item`, `move_item`, `save_track`, `apply_import`, ...), each of which
  bumps `Profile.version` once inside its transaction with an atomic `F("version") + 1`.
  Views never write profile rows directly, so no change can skip the bump.
- **One generic set of views over a section registry.** `profiles/sections.py` lists the six inline
  sections (experience, bullet, skill, project, education, certification) with their form, model and
  ordering. One set of views and partials serves them all, which keeps the isolation surface small:
  every object lookup is `get_owned_or_404`, and related-object form fields are restricted to the
  acting user's rows.
- **Reordering with up/down buttons.** They work on phones and with a keyboard and need no extra
  JavaScript. SortableJS arrives with the Phase 9 kanban board. `order` was added to Project,
  Education and Certification (the spec lists it only for Experience and bullets) so every list
  can be reordered.
- **Database-enforced invariants.** One default track per user is a conditional unique constraint;
  skill names are unique per user, case-insensitively.
- **CV files are untrusted.** Extension and magic bytes must agree, the size is capped, DOCX archives
  are checked for decompression bombs, PDF reading is bounded, and files are stored under random
  names in private storage, downloadable only by their owner. Scanned or password-protected files
  are refused at upload with a plain message, before any LLM spend.
- **Parsing runs in a Huey task** (`parse_cv_import`, enqueued with `transaction.on_commit`) and is
  idempotent: only an `uploaded` import is claimed. The review screen polls until it is ready.
- **The parser may only transcribe.** After the LLM answers, `sanitise_parsed` drops any metric whose
  numbers are not in the CV text and any skill not mentioned in it. This backs the prompt rule with
  a check that does not trust the model (spec §12.5 intent).
- **Review screen:** the basic fields are editable and every parsed item can be ticked or unticked.
  Fine-grained editing of items happens afterwards in the inline editor, which already exists. Full
  nested editable forms on the review page would duplicate that editor.
- **Replace or merge.** Replace clears experience, projects, education, certifications and skills;
  merge adds only items that are new (matched on title+company+start, project name,
  institution+qualification, certification name, skill name) and only fills empty basics.
- **Onboarding** is a registry of steps (`onboarding.STEPS`). Later phases append steps; the wizard
  computes "next" from the list. The dashboard redirects to the wizard only for users that have a
  settings row with `onboarding_completed=False`.

## Consequences

Adding a profile section means one registry entry, one form and two small templates. A direct
`Model.objects.create` elsewhere would skip the version bump, so later phases must use the services.
Reviewers should check that no view or task writes profile models directly.
