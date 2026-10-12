# Paste into Claude Code with this repository open

Build FrameFoundry, a general-purpose LOCAL footage integrity and still-selection
app. Read CLAUDE.md and all docs first. The existing code is a tested core
foundation, not a finished product. Audit it and identify gaps before extending.

For the first PR, implement a durable SQLite project/job model; multi-source
inventory; a resumable, cancellable bounded worker queue; and a contact-sheet
review UI with persisted clip exclusions and frame selections. Preserve source
files. Use configurable project tags and export prefixes. Do not process the
entire user's drives as a smoke test; validate on synthetic fixtures and a small
user-chosen representative folder first.

Subsequent PRs: scene-aware temporal sampling and precise timestamp navigation;
explainable technical metrics and optional semantic suggestions; color-managed
full-resolution batch export with provenance; social crops and contact sheets;
then macOS packaging and performance tuning. Do not claim automated white
balance, composition, motion or subject review is implemented unless it really is.

For each PR, include requirement IDs from docs/ACCEPTANCE.md, test commands and
results, before/after behavior, remaining limitations, and a small reproducible
validation fixture. Keep PRs small enough for independent review. Do not merge.
The supervising ChatGPT/Codex agent will review your diffs and pressure-test the
implementation against the acceptance matrix. Ask it to review by providing
the PR URL and commit SHA through the user; no direct agent link is configured.
