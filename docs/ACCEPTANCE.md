# Independent review and pressure-test matrix

| ID | Requirement / test | Foundation status |
|---|---|---|
| A01 | Originals byte-identical after processing/export | Synthetic integration test |
| A02 | Paths with spaces; duplicate camera basenames remain distinct | Spaces tested; multi-camera extension needed |
| A03 | Source changes invalidate cache | Tested |
| A04 | Corrupt media produces per-clip error without halting batch | Per-clip UI error only; batch pending |
| A05 | Missing drive, read-only folder, full disk, disconnect mid-export | Missing parent tested; others pending |
| A06 | Cancel/resume without duplicate records or orphan jobs | Pending |
| A07 | Timestamp correctness for CFR, VFR, rotated footage, long GOP | Pending; requested seconds recorded only |
| A08 | HDR/log transforms explicit, repeatable and recorded | Manual gate only; transforms pending |
| A09 | Noise not mislabeled as focus; theatrical color not auto-rejected | Honest heuristic labels; calibration pending |
| A10 | Motion measured temporally; creative scores separated from metrics | Manual notes only |
| A11 | Talking-head exclusions reversible; false positives reviewable | Manual exclusion implemented |
| A12 | Multiple useful frames per clip, near-duplicate suppression | Multi-frame sampling; dedup pending |
| A13 | Subject detection with confidence/unknown states; human correction | Manual configurable tags only |
| A14 | Kept stills only; no export overwrite; full-resolution output | Synthetic integration test |
| A15 | Index links every exported still to original and time | Sidecars/CSV tested; precise PTS pending |
| A16 | General project names and tags; no client hard-coding | Implemented; seasonal event optional preset |
| A17 | One-hour 4K clips / large inventory: bounded CPU/RAM/cache | Pending benchmark |
| A18 | No media/credentials in Git; no network upload by default | Local design and ignores; release audit needed |
| A19 | UI restart retains decisions; export manifest recovers from crash | Basic JSON persistence; transaction hardening pending |
| A20 | Social crops preserve full master and subject composition | Pending |

Before production: test small representative day/night/drone/handheld/log/HDR
samples on the actual Mac, compare against manually selected reference stills,
measure time and disk consumption, and record disagreements. A passing synthetic
test does not establish creative quality or compatibility with all camera codecs.
