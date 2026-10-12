"""Explainable technical screening score and per-clip suggestions.

This is a heuristic ranking of measured signals, not a creative judgment. It looks
only at relative edge detail (a sharpness proxy that noise can inflate) and at
clipped highlights / near-black area. It knows nothing about composition, subject,
motion or white balance. Sharpness is ranked within each clip, so a night clip is
not penalised for being darker or softer than a daylight one.
"""
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

SHARPNESS_WEIGHT = 0.75
EXPOSURE_WEIGHT = 0.25
DARK_TOLERANCE = 0.25      # near-black area tolerated before any penalty (night, theatrical)
DUPLICATE_DISTANCE = 6     # max differing bits (of 64) for two frames to count as near-duplicates


def percentile_ranks(values):
    """0..1 rank of each value among the others; ties share the average rank."""
    n = len(values)
    if n == 1:
        return [1.0]
    return [(sum(v < x for v in values) + (sum(v == x for v in values) - 1) / 2) / (n - 1)
            for x in values]


def exposure_quality(metrics):
    highlights = metrics.get('bright_fraction', 0)
    shadows = max(0.0, metrics.get('dark_fraction', 0) - DARK_TOLERANCE)
    return max(0.0, 1 - min(1.0, highlights * 5 + shadows))


@lru_cache(maxsize=4096)
def _dhash(path, mtime_ns):
    px = np.asarray(Image.open(path).convert('L').resize((9, 8)), dtype=np.int16)
    bits = 0
    for bit in (px[:, :-1] > px[:, 1:]).flatten():
        bits = (bits << 1) | int(bit)
    return bits


def dhash(path):
    """64-bit difference hash of an image file, or None if it cannot be read."""
    try:
        return _dhash(str(path), Path(path).stat().st_mtime_ns)
    except OSError:
        return None


def hamming(a, b):
    return bin(a ^ b).count('1')


def score_candidates(candidates, top_n=5):
    """Return copies of candidates with 'score', 'reasons', 'suggested_rank' and
    'duplicate_of' added. The top_n suggestions are the highest-scoring frames after
    skipping near-duplicates of frames already suggested."""
    if not candidates:
        return []
    sharp = percentile_ranks([k['metrics'].get('edge_variance', 0) for k in candidates])
    scored = []
    for k, s in zip(candidates, sharp):
        m = k['metrics']
        exposure = exposure_quality(m)
        reasons = []
        if len(candidates) > 1 and s >= 0.8:
            reasons.append('among the sharpest in this clip')
        elif len(candidates) > 1 and s <= 0.2:
            reasons.append('softer than most of this clip')
        if m.get('bright_fraction', 0) >= 0.01:
            reasons.append(f"{m['bright_fraction']:.0%} clipped highlights")
        if m.get('dark_fraction', 0) > DARK_TOLERANCE:
            reasons.append(f"{m['dark_fraction']:.0%} near-black (may be intentional)")
        scored.append({**k, 'score': round(100 * (SHARPNESS_WEIGHT * s +
                                                  EXPOSURE_WEIGHT * exposure)),
                       'reasons': reasons, 'suggested_rank': None, 'duplicate_of': None})
    picked = []  # (rank, hash)
    for k in sorted(scored, key=lambda x: (-x['score'], x['seconds'])):
        h = dhash(k['preview'])
        twin = next((r for r, ph in picked if h is not None and ph is not None
                     and hamming(h, ph) <= DUPLICATE_DISTANCE), None)
        if twin is not None:
            k['duplicate_of'] = twin
            k['reasons'] = k['reasons'] + [f'near-duplicate of suggestion #{twin}']
        elif len(picked) < top_n:
            k['suggested_rank'] = len(picked) + 1
            picked.append((k['suggested_rank'], h))
    return scored


def suggestions(scored):
    return sorted((k for k in scored if k['suggested_rank']), key=lambda k: k['suggested_rank'])


def autopick(store, project_id, top_n=5, reject_rest=False):
    """Keep the top-N suggestions in every analyzed clip nobody has started reviewing.
    Decisions are labelled automatic and never override a person's. Returns the number
    of frames changed per clip id."""
    changed = {}
    for clip in store.clips(project_id, include_excluded=False):
        candidates = store.candidates(clip['id']) if clip['candidate_count'] else []
        if not candidates or any(k['decision'] != 'Review' and k['decided_by'] == 'human'
                                 for k in candidates):
            continue
        scored = score_candidates(candidates, top_n)
        picks = [k['id'] for k in suggestions(scored)]
        n = store.bulk_decide(picks, 'Keep')
        if reject_rest:
            n += store.bulk_decide([k['id'] for k in scored if k['id'] not in picks], 'Reject')
        changed[clip['id']] = n
    return changed
