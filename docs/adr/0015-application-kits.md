# 15. Application kits: cite-and-check, facts from the database, documents that survive failures

- Status: Accepted
- Date: 2026-10-23

## Context

Phase 7 produces a CV, cover letter, interview prep and screening answers per job (spec §12). The
CV is sent to employers, so an invented number or skill is the worst failure. The pipeline also
spends money in four model calls, so it should not throw work away when one late step fails.

## Decision

- **The model chooses, orders and rephrases; it never supplies facts.** `CVData` holds only item IDs
  (`EXP-12`, `BUL-88`, `SKL-3`, ...) and rephrased text. Names, contact details, companies, titles,
  dates and institutions are read from the database by ID at render time (`documents.py`), for the
  owner of the kit only. A deleted or foreign ID simply does not render.
- **Every claim cites its sources, and the validator checks the citations** (`validation.py`, pure):
  the IDs must exist and be of the right kind and the user's own; every number must appear in a
  cited item; a skill the *job* asks for may be named only if the user has it or a cited item says
  so (stemmed, whole-word match); no banned phrases or em dashes; length limits. Skills are checked
  against the job's extracted skill list rather than any free-text guess, because the real risk is
  keyword-stuffing for the job.
- **Retry once, then repair or drop.** Problems are fed back to the model once. If the second
  attempt is still wrong, em dashes are replaced by commas (a mechanical repair) and anything else
  that fails is removed, never "fixed" by guesswork. A bad headline or summary falls back to the
  user's own profile wording; a CV left with no experience falls back to the user's own bullets.
  Everything repaired or removed is listed in `validation_report`, and the UI says so.
- **The retry is skipped, not failed, when the allowance is gone.** A kit counts once, as its
  `generate_cv` call (ADR 0010). The validation retry uses the same task, so in the rare case it is
  made it counts as a second use; if that would exceed the allowance the first attempt is repaired
  instead.
- **Required parts versus best effort.** The CV and cover letter must succeed or the kit fails with a
  short safe message. Interview prep and screening answers are best effort: if one fails, the kit is
  still ready and says what is missing.
- **Rendering failures keep the text.** Each of the four files is rendered on its own. If the PDF
  engine is unavailable (WeasyPrint needs the Pango libraries), the Word documents are still saved,
  the kit is `ready` with an explanation, and "Re-render documents" rebuilds the files later with no
  model call and no allowance used.
- **Edits never call the model.** Edited text is stored with the same source IDs, checked for style
  only as warnings, and the files are re-rendered. `user_edited` is set.
- **Versions.** A regeneration is a new `Kit` row (`version` per user and job, unique); older
  versions stay downloadable. At most one kit per user and job is queued or generating at a time.
  A kit stuck in progress for ten minutes is marked failed when next viewed.
- **Files** live under `PRIVATE_MEDIA_ROOT/kits/` with random names and are served only by
  `kits.views.download` after an ownership check, as attachments named
  `<First>_<Last>_CV_<Company>.pdf` (ASCII only).
- **Word templates are generated, then committed.** `docx_templates.py` builds `cv_classic.docx`
  and `cover_letter.docx` with docxtpl tags; `build_doc_templates` regenerates them and a test fails
  if the committed files drift from the builder.
- **Auto-kit** (`matching.hooks.after_score`) starts a kit at the user's `auto_kit_threshold` when
  the allowance has room and no kit exists; it can never break scoring. `kits.hooks.after_generate`
  is the seam for Phase 8's "kit ready" message.
- **Font:** Source Sans 3 (SIL OFL) is bundled in `apps/kits/templates_docs/fonts/` with its licence.

## Consequences

A CV can be shorter than the model first wrote it, and sometimes uses the user's own wording; that is
the intended trade for never publishing an unverifiable claim. The skill check only knows the skills
a job lists, so a made-up skill the job does not mention is caught by the ID and number checks, not
by name. Number words ("three") are not checked. PDF rendering needs Pango on the host (already in
the Docker image); on a Windows development machine the GTK runtime must be installed.
