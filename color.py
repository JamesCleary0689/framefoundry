"""Input color profiles and conversion to Rec.709 for previews, scoring and export.

Every conversion is a fixed, versioned technical normalization, recorded in export
provenance. It is not a creative grade and not a manufacturer's display LUT:

  log/HDR signal -> scene-linear (published camera/BT.2100 curves)
  -> Rec.709 primaries (3x3 matrix from published primaries, all D65)
  -> soft highlight roll-off above a knee -> Rec.709 OETF

Custom .cube LUTs are applied as-is (log code values in, Rec.709 display out) for
profiles not built in. Detection uses only explicit metadata; flat-looking footage is
reported as "possibly log" but never converted without a person choosing a profile.
"""
import hashlib
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

VERSION = 1
D65 = (0.3127, 0.3290)
REC709 = ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060))
BT2020 = ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046))
V_GAMUT = ((0.730, 0.280), (0.165, 0.840), (0.100, -0.030))
S_GAMUT3 = ((0.730, 0.280), (0.140, 0.855), (0.100, -0.050))
S_GAMUT3_CINE = ((0.766, 0.275), (0.225, 0.800), (0.089, -0.087))
AWG3 = ((0.6840, 0.3130), (0.2210, 0.8480), (0.0861, -0.1020))

PROFILES = {
    'rec709': 'Rec.709 (no conversion)',
    'vlog': 'Panasonic V-Log / V-Gamut',
    'slog3-cine': 'Sony S-Log3 / S-Gamut3.Cine',
    'slog3': 'Sony S-Log3 / S-Gamut3',
    'logc3': 'ARRI LogC3 (EI 800) / ARRI Wide Gamut 3',
    'hlg': 'HLG (BT.2100, BT.2020 primaries)',
    'pq': 'PQ / HDR10 (BT.2100, BT.2020 primaries)',
}
CHOICES = ['auto'] + list(PROFILES) + ['lut']
KNEE = 0.6  # display-linear level above which highlights roll off smoothly toward 1.0


def label(profile):
    if profile == 'auto':
        return 'Auto (metadata)'
    if profile == 'lut':
        return 'Custom LUT (.cube)'
    if str(profile).startswith('lut:'):
        return f'LUT {Path(profile[4:]).name}'
    return PROFILES.get(profile, profile)


def validate(profile):
    """Return a normalized profile string or raise ValueError."""
    profile = str(profile or 'auto').strip()
    if profile in ('auto',) or profile in PROFILES:
        return profile
    if profile.startswith('lut:'):
        path = Path(profile[4:]).expanduser().resolve()
        load_cube(str(path), path.stat().st_mtime_ns)  # raises if unreadable or malformed
        return f'lut:{path}'
    raise ValueError(f'Unknown color profile {profile!r}.')


def resolve(clip_profile, source_profile, detected):
    """Effective profile and the reason it applies."""
    if clip_profile and clip_profile != 'auto':
        return clip_profile, 'set on this clip'
    if source_profile and source_profile != 'auto':
        return source_profile, 'set on this source'
    if detected:
        return detected, 'detected from metadata'
    return 'rec709', 'default (no log/HDR metadata found)'


def cache_key(profile):
    """Short filesystem-safe key that changes when the transform would change."""
    if profile.startswith('lut:'):
        path = Path(profile[4:])
        stamp = path.stat().st_mtime_ns if path.exists() else 0
        return 'lut-' + hashlib.sha256(f'{path}|{stamp}'.encode()).hexdigest()[:12]
    return f'{profile}-v{VERSION}'


def describe(profile):
    """Provenance record for an export sidecar."""
    if profile == 'rec709':
        method = 'No color conversion (decoded Rec.709 signal)'
    elif profile.startswith('lut:'):
        method = 'User-supplied 3D LUT applied to log code values (trilinear)'
    else:
        method = ('Published log/HDR curve to scene-linear, 3x3 primaries matrix to Rec.709 '
                  f'(D65), soft highlight roll-off above {KNEE}, Rec.709 OETF')
    return {'input_profile': label(profile), 'profile_id': profile, 'method': method,
            'transform_version': VERSION,
            'note': 'Technical normalization for review; not a creative grade.'}


# -- detection ------------------------------------------------------------------
VENDOR_HINTS = [(re.compile(r'v[-_ ]?log', re.I), 'vlog'),
                (re.compile(r's[-_ ]?log ?3', re.I), 'slog3-cine'),
                (re.compile(r'log ?c', re.I), 'logc3')]


def detect(metadata):
    """(profile, reason) from explicit metadata only, or (None, None)."""
    transfer = metadata.get('color_transfer', 'unknown')
    if transfer == 'arib-std-b67':
        return 'hlg', 'transfer characteristic tagged arib-std-b67 (HLG)'
    if transfer == 'smpte2084':
        return 'pq', 'transfer characteristic tagged smpte2084 (PQ)'
    for key, value in sorted((metadata.get('tags') or {}).items()):
        for pattern, profile in VENDOR_HINTS:
            if pattern.search(str(value)):
                note = ' (gamut assumed S-Gamut3.Cine)' if profile == 'slog3-cine' else ''
                return profile, f'metadata tag {key}={str(value)[:40]!r}{note}'
    return None, None


def looks_flat(images):
    """True if sampled previews have the low contrast typical of undeclared log footage.
    A hint for the reviewer only; it never triggers a conversion."""
    lows, highs = [], []
    for img in images:
        luma = np.asarray(img.convert('L'), dtype=np.float32) / 255
        lows.append(np.percentile(luma, 1))
        highs.append(np.percentile(luma, 99))
    return bool(images) and float(np.median(lows)) > 0.06 and float(np.median(highs)) < 0.80


# -- math -------------------------------------------------------------------------
def rgb_to_xyz(primaries, white=D65):
    xy = np.array(primaries, dtype=np.float64)
    xyz = np.column_stack([xy[:, 0] / xy[:, 1], np.ones(3), (1 - xy.sum(1)) / xy[:, 1]]).T
    w = np.array([white[0] / white[1], 1.0, (1 - sum(white)) / white[1]])
    return xyz * np.linalg.solve(xyz, w)


def gamut_matrix(source):
    return np.linalg.inv(rgb_to_xyz(REC709)) @ rgb_to_xyz(source)


def legal_to_code(v):
    """Full-range RGB decoded from limited-range video back to 10-bit code value / 1023,
    the domain in which camera log curves are specified."""
    return (v * 876 + 64) / 1023


def vlog_to_linear(x):
    b, c, d = 0.00873, 0.241514, 0.598206
    return np.where(x < 0.181, (x - 0.125) / 5.6, 10 ** ((x - d) / c) - b)


def slog3_to_linear(x):
    cv = x * 1023
    return np.where(cv >= 171.2102946929, 10 ** ((cv - 420) / 261.5) * 0.19 - 0.01,
                    (cv - 95) * 0.01125 / (171.2102946929 - 95))


def logc3_to_linear(x):
    cut, a, b, c, d, e, f = 0.010591, 5.555556, 0.052272, 0.247190, 0.385537, 5.367655, 0.092809
    return np.where(x > e * cut + f, (10 ** ((x - d) / c) - b) / a, (x - f) / e)


def hlg_to_linear(x):
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    e = np.where(x <= 0.5, x ** 2 / 3, (np.exp((x - c) / a) + b) / 12)
    return e * (0.18 / (0.38 ** 2 / 3))  # BT.2408: 38% HLG signal ~ 18% grey


def pq_to_linear(x):
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    p = np.clip(x, 0, 1) ** (1 / m2)
    nits = 10000 * (np.maximum(p - c1, 0) / (c2 - c3 * p)) ** (1 / m1)
    return nits / 203  # BT.2408 reference (diffuse) white = 203 nits -> 1.0


def roll_off(lin):
    """Identity below KNEE, then a smooth (C1) exponential approach to 1.0."""
    lin = np.maximum(lin, 0)
    span = 1 - KNEE
    return np.where(lin <= KNEE, lin, KNEE + span * (1 - np.exp(-(lin - KNEE) / span)))


def rec709_oetf(lin):
    lin = np.clip(lin, 0, 1)
    return np.where(lin < 0.018, 4.5 * lin, 1.099 * lin ** 0.45 - 0.099)


LOG_CURVES = {'vlog': (vlog_to_linear, V_GAMUT, True), 'slog3': (slog3_to_linear, S_GAMUT3, True),
              'slog3-cine': (slog3_to_linear, S_GAMUT3_CINE, True),
              'logc3': (logc3_to_linear, AWG3, True), 'hlg': (hlg_to_linear, BT2020, False),
              'pq': (pq_to_linear, BT2020, False)}


@lru_cache(maxsize=16)
def load_cube(path, mtime_ns):
    size, lo, hi, rows = None, np.zeros(3), np.ones(3), []
    for line in Path(path).read_text(errors='replace').splitlines():
        line = line.split('#')[0].strip()
        if not line:
            continue
        head = line.split()
        if head[0] == 'LUT_3D_SIZE':
            size = int(head[1])
        elif head[0] == 'DOMAIN_MIN':
            lo = np.array(head[1:4], dtype=np.float64)
        elif head[0] == 'DOMAIN_MAX':
            hi = np.array(head[1:4], dtype=np.float64)
        elif head[0] == 'LUT_1D_SIZE':
            raise ValueError('1D LUTs are not supported; use a 3D .cube LUT.')
        elif re.match(r'^[-+0-9.eE]+$', head[0]) and len(head) >= 3:
            rows.append([float(v) for v in head[:3]])
    if not size or len(rows) != size ** 3:
        raise ValueError(f'Not a valid 3D .cube LUT: expected {size}^3 entries, '
                         f'found {len(rows)}.')
    table = np.array(rows, dtype=np.float32).reshape(size, size, size, 3)  # [b][g][r]
    return table, lo, hi


def apply_cube(rgb, table, lo, hi):
    n = table.shape[0]
    pos = np.clip((rgb - lo) / (hi - lo), 0, 1) * (n - 1)
    i0 = np.clip(np.floor(pos).astype(np.int32), 0, n - 2)
    f = pos - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    out = 0
    for db, wb in ((0, 1 - fb), (1, fb)):
        for dg, wg in ((0, 1 - fg), (1, fg)):
            for dr, wr in ((0, 1 - fr), (1, fr)):
                out = out + table[b0 + db, g0 + dg, r0 + dr] * (wb * wg * wr)
    return out


def to_rec709(rgb, profile, limited_range=True):
    """Convert decoded full-range RGB (float, 0..1) to Rec.709-encoded RGB (0..1)."""
    if profile == 'rec709':
        return rgb
    code = legal_to_code(rgb) if limited_range else rgb
    if profile.startswith('lut:'):
        path = Path(profile[4:])
        return np.clip(apply_cube(code, *load_cube(str(path), path.stat().st_mtime_ns)), 0, 1)
    curve, primaries, uses_code = LOG_CURVES[profile]
    lin = curve(code if uses_code else rgb)
    lin = lin @ gamut_matrix(primaries).T.astype(np.float32)
    return rec709_oetf(roll_off(lin)).astype(np.float32)
