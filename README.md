# FrameFoundry

A local footage-review and still-selection workbench for any production.
Project-specific presets sit on top of a generic media workflow.

## Status

Working foundation for Claude to extend, not a finished automatic curator.
Core decoding/export integration tests are included. The Streamlit UI has not
yet been exercised on macOS or with the user's production media.

Implemented: SQLite project database (projects, sources, clips, candidates,
selections, jobs, exports); multi-source inventory with per-source labels, so
duplicate camera basenames stay distinct; read-only rescans that detect changed,
missing and offline (unmounted) sources without losing decisions; reversible clip
exclusions with an audit trail; a bounded background worker queue that persists in
the database and supports cancel, resume/retry, crash recovery and per-clip errors;
a contact-sheet review UI with immediately persisted Keep/Reject/Review decisions,
human rank/subject/period/notes and a color-verified gate; configurable project
tags and export prefixes; FFprobe metadata, uniform sparse candidate sampling and
heuristic technical screening metrics; full-resolution PNG export with provenance
sidecars and CSV index. Source media stays local and is never modified.

Not implemented: autonomous semantic review, reliable talking-head detection,
scene-aware sampling, temporal motion analysis, fine frame navigation, perceptual
deduplication, automatic color management, batch export queue, contact-sheet
exports, social crops, native packaging or automated Claude orchestration.
DNG image sequences are not ingested. Review decisions saved by the earlier
JSON-based foundation (`reviews.json`) are not migrated into the database.

## Run locally on your Mac

Requires Python 3.11+ and FFmpeg/FFprobe on PATH. If Homebrew is already installed,
`brew install ffmpeg` installs the media tools.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

Create a project (name, export prefix, starting subject tags), add one source folder
per camera or card, scan, then queue analysis from the Queue tab. Review candidates
on the contact sheet, exclude irrelevant clips in the Clips tab, and export kept
frames from Selections & export. The output parent must already exist to avoid
silently writing to a missing external-drive mount.

The project database and preview cache live in `~/.framefoundry`
(`framefoundry.sqlite`, `cache/`); set `FRAMEFOUNDRY_HOME` to move them.
`FRAMEFOUNDRY_WORKERS` (default 2, max 8) bounds concurrent decodes and
`FRAMEFOUNDRY_MIN_FREE_MB` (default 2048) stops analysis before the cache disk fills.
Queued work resumes when the app restarts; closing the app returns running jobs to
the queue, and jobs orphaned by a crash are recovered after their heartbeat lapses.
Cancellation takes effect between decoded frames, so a very slow single decode
finishes first. Run one app instance per data folder.

Headless equivalent, useful for validating on a small representative folder first:

```bash
python cli.py project create "My Shoot" --prefix MyShoot --preset General
python cli.py source add my-shoot "/Volumes/CardA/Footage" --label "Camera A"
python cli.py scan my-shoot
python cli.py enqueue my-shoot --samples 12
python cli.py work --workers 2
python cli.py status my-shoot
```

Do not point a first run at entire drives; validate on a small folder.

Export structure: `<project>_Still_Selects/`, full-resolution PNGs, one JSON
sidecar per still, and `stills_index.csv`. Names include project, period, subject,
source fingerprint, clip-relative microsecond timestamp and creative rank.
The fingerprint uses path/size/mtime; it is not a content checksum or frame number.

Dark/bright pixel fractions and edge variance are heuristics, not calibrated
quality scores. Colorful lighting is not automatically a white-balance defect.
FFmpeg's default conversion is not a validated camera-log or HDR workflow.
Verify display color before exporting; a proper transform pipeline is a next milestone.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests generate synthetic clips with FFmpeg (two "cameras" with duplicate basenames,
a path with spaces, a corrupt file). Recreate the fixture for manual checks with
`python tests/fixtures.py /tmp/ff-fixture`. The Streamlit smoke test is skipped when
Streamlit is not installed; FFmpeg-dependent tests are skipped without FFmpeg.

## GitHub / Claude handoff

Repository: https://github.com/JamesCleary0689/framefoundry
Foundation branch: `codex/framefoundry-foundation`.

In Claude Code, connect/open that repository and use the prompt in
`docs/CLAUDE_START.md`. The local checkout must run on the computer with the media
drives for real-media validation. A cloud-only Claude session cannot read them.
Do not commit footage, thumbnails, local paths, project databases or credentials.

The supervising assistant reviews supplied PRs/commits against `docs/ACCEPTANCE.md`.
No background monitoring or direct Claude control is configured by this package.
