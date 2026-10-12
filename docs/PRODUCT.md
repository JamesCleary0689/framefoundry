# Product specification

## Purpose
Turn large local shoots into a searchable, ranked library of useful stills while
preserving a reversible link to source footage. General-purpose first; farm,
events, corporate, real estate and other shoots are project configurations.

## Two separate passes
1. Asset integrity index: ingest every supported clip, capture codec, dimensions,
   duration, frame-rate/timebase and color metadata; report corrupt/unreadable
   media. Assess focus consistency, exposure, motion stability, white balance,
   composition and relevance. Separate measured data, model suggestions and
   human review. Record exclusions without deleting footage.
2. Still selection index: sample candidate moments across shots, refine within
   promising windows, suppress near-duplicates and preserve multiple distinctive
   frames from the same source. Rank focus, lighting, composition and subject
   interest independently with explainable reasons. Support human overrides.

## Workspace
Project setup → ingest queue → clip integrity table → candidate contact sheet →
large frame viewer and exact navigation → keep/reject/rank/tag → export queue.
Show source location, timestamp, confidence, review status, progress and errors.
Filters: source/date/camera, subject, day/dusk/night, rank, review status.
Side-by-side comparisons and 4:5 / 1:1 / 9:16 crop previews with manual control.
Always retain the full-resolution uncropped master; crops are additional files.

## Architecture direction
Local Python media worker with FFmpeg/FFprobe; local browser UI initially.
SQLite for transactional project/clip/candidate/review/job/export records.
Version processing settings and transforms. Resume jobs after interruption;
bounded concurrency, disk-space checks, cache limits and remount recovery.
Evaluate performance before replacing Streamlit or selecting a desktop shell.
Optional model adapters must be explicit, configurable, and disclose media egress.
Do not invent semantic labels when no model/human has evaluated a frame.

## Example project preset: seasonal event
Sources and export destinations are chosen locally. Never commit local drive paths
or client-specific briefs. Reversibly exclude interviews and irrelevant footage.
Configurable subjects may include markets, setup, screens, projectors, lasers,
day-to-night transitions, attractions, costumed characters, portraits and aerials.
Require visual confirmation of character tags; do not infer real identities.
Prioritize story coverage as well as individual beauty. Dark scenes and theatrical
color should not be rejected automatically by generic technical measurements.

## Delivery
Configurable output parent, project slug and naming template. Export approved
full-resolution stills, provenance JSON, clip and still CSV indexes, contact
sheets, and optional social crops. Never silently replace existing outputs.
Use presentation timestamps/timebase for exact VFR provenance; current scaffold
only records requested clip-relative seconds and does not guarantee frame ID.
