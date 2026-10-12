# Independent review and pressure-test matrix

| ID | Requirement / test | Status (after milestone 1) |
|---|---|---|
| A01 | Originals byte-identical after processing/export | Synthetic integration test |
| A02 | Paths with spaces; duplicate camera basenames remain distinct | Spaces and duplicate basenames across sources tested (store + export) |
| A03 | Source changes invalidate cache | Tested; rescans hide stale candidates and block their export |
| A04 | Corrupt media produces per-clip error without halting batch | Per-clip job failure; batch continues (synthetic corrupt clip tested) |
| A05 | Missing drive, read-only folder, full disk, disconnect mid-export | Missing parent, offline source keeps decisions, low disk space tested; read-only, disconnect mid-export pending |
| A06 | Cancel/resume without duplicate records or orphan jobs | Cancel, resume, interruption, crash recovery, poison limit, no duplicates tested |
| A07 | Timestamp correctness for CFR, VFR, rotated footage, long GOP | Pending; requested seconds recorded only |
| A08 | HDR/log transforms explicit, repeatable and recorded | Manual gate only; transforms pending |
| A09 | Noise not mislabeled as focus; theatrical color not auto-rejected | Score ranks sharpness within each clip; near-black tolerated up to 25% and labelled "may be intentional"; calibration pending |
| A10 | Motion measured temporally; creative scores separated from metrics | Technical score shown separately from human creative rank; temporal motion not measured |
| A11 | Talking-head exclusions reversible; false positives reviewable | Reversible exclusions with audit trail, skipped by queue; detection pending |
| A12 | Multiple useful frames per clip, near-duplicate suppression | Multi-frame sampling; top-N suggestions with preview-hash near-duplicate suppression (heuristic) |
| A13 | Subject detection with confidence/unknown states; human correction | Manual configurable tags only |
| A14 | Kept stills only; no export overwrite; full-resolution output | Synthetic integration test |
| A15 | Index links every exported still to original and time | Sidecars/CSV tested; precise PTS pending |
| A16 | General project names and tags; no client hard-coding | Per-project tags and export prefix; presets optional |
| A17 | One-hour 4K clips / large inventory: bounded CPU/RAM/cache | Pending benchmark |
| A18 | No media/credentials in Git; no network upload by default | Local design and ignores; release audit needed |
| A19 | UI restart retains decisions; export manifest recovers from crash | SQLite transactional decisions/exclusions/jobs survive restart; export manifest crash recovery pending |
| A20 | Social crops preserve full master and subject composition | Pending |

Before production: test small representative day/night/drone/handheld/log/HDR
samples on the actual Mac, compare against manually selected reference stills,
measure time and disk consumption, and record disagreements. A passing synthetic
test does not establish creative quality or compatibility with all camera codecs.
