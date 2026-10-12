# FrameFoundry

A local footage-review and still-selection workbench for any production.
Project-specific presets sit on top of a generic media workflow.

## Status

Working foundation for Claude to extend, not a finished automatic curator.
Core decoding/export integration tests are included. The Streamlit UI has not
yet been exercised on macOS or with the user's production media.

Implemented: recursive video inventory, source-change detection, FFprobe metadata,
sparse candidate sampling, cached previews, technical screening metrics, manual
clip exclusions, creative ranking and subject tags, persistent review decisions,
full-resolution PNG export, provenance sidecars and CSV index. Source media stays local.

Not implemented: autonomous semantic review, reliable talking-head detection,
scene-aware sampling, temporal motion analysis, fine frame navigation, perceptual
deduplication, automatic color management, batch jobs, cancellation, batch exports,
contact sheets, social crops, native packaging or automated Claude orchestration.
DNG image sequences are not ingested in this foundation.

## Run locally on your Mac

Requires Python 3.11+ and FFmpeg/FFprobe on PATH. If Homebrew is already installed,
`brew install ffmpeg` installs the media tools.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.address 127.0.0.1
```

Enter the existing source and output folders. Scan, choose a clip, analyze it,
save Keep/Reject decisions, then export a kept frame. The output parent must
already exist to avoid silently writing to a missing external-drive mount.
Previews and reviews live in `~/.framefoundry`. Run one app instance per project.

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

## GitHub / Claude handoff

Repository: https://github.com/JamesCleary0689/framefoundry
Foundation branch: `codex/framefoundry-foundation`.

In Claude Code, connect/open that repository and use the prompt in
`docs/CLAUDE_START.md`. The local checkout must run on the computer with the media
drives for real-media validation. A cloud-only Claude session cannot read them.
Do not commit footage, thumbnails, local paths, review databases or credentials.

The supervising assistant reviews supplied PRs/commits against `docs/ACCEPTANCE.md`.
No background monitoring or direct Claude control is configured by this package.
