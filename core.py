"""Local, non-destructive video inventory and frame extraction foundation."""
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

import color

EXTENSIONS = {'.mov', '.mp4', '.mxf', '.mkv', '.avi', '.m4v', '.mts', '.m2ts'}


def run(args, timeout=120):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout, check=False)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors='replace')[-1800:])
    return p.stdout


def dependencies():
    return [x for x in ('ffmpeg', 'ffprobe') if not shutil.which(x)]


def is_hidden(relative):
    """Hidden files and folders, including macOS AppleDouble '._name' companions that
    exFAT/FAT drives get for every file (they keep the video extension but are not video)
    and system folders such as .Trashes, .Spotlight-V100 and .fseventsd."""
    return any(part.startswith('.') for part in Path(relative).parts)


def inventory(root):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Source must be an existing folder.')
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        if is_hidden(path.relative_to(root)):
            continue
        stat = path.stat()
        signature = f'{path}|{stat.st_size}|{stat.st_mtime_ns}'
        result.append({'id': hashlib.sha256(signature.encode()).hexdigest()[:16],
                       'path': str(path), 'name': str(path.relative_to(root)),
                       'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
    return result


def verify_source(clip):
    s = Path(clip['path']).stat()
    if s.st_size != clip['size'] or s.st_mtime_ns != clip['mtime_ns']:
        raise ValueError('Source changed. Rescan before continuing.')


def probe(path):
    raw = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                          '-of', 'json', str(path)], timeout=45))
    streams = [s for s in raw.get('streams', []) if s.get('codec_type') == 'video'
               and not s.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise ValueError('No video stream found.')
    s = streams[0]
    duration = float(s.get('duration') or raw.get('format', {}).get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Missing or invalid duration.')
    return {'duration': duration, 'width': s['width'], 'height': s['height'],
            'stream_index': s['index'], 'codec': s.get('codec_name'),
            'fps': s.get('avg_frame_rate'), 'color_transfer': s.get('color_transfer', 'unknown'),
            'color_primaries': s.get('color_primaries', 'unknown'),
            'color_space': s.get('color_space', 'unknown'),
            'color_range': s.get('color_range', 'unknown'), 'pix_fmt': s.get('pix_fmt'),
            'tags': {**{f'format.{k}': v for k, v in raw.get('format', {}).get('tags', {}).items()},
                     **{f'stream.{k}': v for k, v in s.get('tags', {}).items()}}}


def sample_times(duration, count):
    if not math.isfinite(duration) or duration <= 0 or not 1 <= count <= 48:
        raise ValueError('Invalid duration or sample count (1–48).')
    return [duration * (i + .5) / count for i in range(count)]


YUV_MATRICES = {'bt709': 'bt709', 'bt2020nc': 'bt2020', 'bt2020c': 'bt2020',
                'smpte170m': 'bt601', 'bt470bg': 'bt601', 'fcc': 'fcc', 'smpte240m': 'smpte240m'}


def decode_settings(metadata):
    """YUV->RGB matrix and signal range used to decode. Untagged HD+ footage is decoded
    as BT.709 (FFmpeg would otherwise assume BT.601 and shift colors)."""
    metadata = metadata or {}
    matrix = YUV_MATRICES.get(metadata.get('color_space')) or (
        'bt709' if metadata.get('height', 1080) >= 720 else 'bt601')
    full = metadata.get('color_range') == 'pc' or str(metadata.get('pix_fmt')).startswith('yuvj')
    return matrix, 'pc' if full else 'tv'


def decode_command(path, seconds, stream_index=0, preview=True, metadata=None,
                   input_seek=True):
    # Input seeking (-ss before -i) jumps to the nearest earlier keyframe and decodes
    # forward, instead of decoding the clip from the start. When re-encoding, FFmpeg
    # discards frames before the target, so the frame matches output seeking
    # (verified by test_core) at a fraction of the cost on long clips.
    matrix, signal_range = decode_settings(metadata)
    size = '960:-2' if preview else 'iw:ih'
    vf = (f'scale={size}:in_color_matrix={matrix}:in_range={signal_range}:out_range=pc:'
          'flags=accurate_rnd+full_chroma_int,format=rgb48be')
    seek = ['-ss', f'{seconds:.6f}']
    return (['ffmpeg', '-v', 'error'] + (seek if input_seek else []) + ['-i', str(path)] +
            ([] if input_seek else seek) +
            ['-map', f'0:{stream_index}', '-frames:v', '1', '-vf', vf,
             '-f', 'image2pipe', '-vcodec', 'ppm', '-'])


def parse_ppm16(data):
    """Decode a binary 16-bit PPM (P6, maxval 65535) to float32 RGB in 0..1."""
    parts = data.split(maxsplit=4)
    if len(parts) < 5 or parts[0] != b'P6':
        raise ValueError('No frame decoded at this timestamp.')
    width, height, maxval = int(parts[1]), int(parts[2]), int(parts[3])
    if maxval != 65535:
        raise ValueError(f'Unexpected PPM maxval {maxval}.')
    pixels = data[len(data) - width * height * 6:]
    return (np.frombuffer(pixels, dtype='>u2').reshape(height, width, 3)
            .astype(np.float32) / 65535)


def frame_interval(metadata):
    """Nominal seconds per frame from the average frame rate (1/24 if unknown)."""
    try:
        num, den = (float(x) for x in str((metadata or {}).get('fps') or '24/1').split('/'))
        return den / num if num > 0 and den > 0 else 1 / 24
    except ValueError:
        return 1 / 24


def last_decodable(seconds, metadata):
    """Clamp a time so it does not fall after the start of the clip's last frame, where
    a seek returns nothing (container duration includes the last frame's length)."""
    duration = (metadata or {}).get('duration')
    if not duration:
        return seconds
    return max(0.0, min(seconds, duration - 1.5 * frame_interval(metadata)))


def decode_rgb(path, seconds, stream_index=0, preview=True, metadata=None):
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('Invalid timestamp.')
    data = run(decode_command(path, seconds, stream_index, preview, metadata), timeout=180)
    step = frame_interval(metadata)
    for attempt in range(1, 4):  # near the very end, step back a frame at a time
        if data or seconds - attempt * step < 0:
            break
        data = run(decode_command(path, seconds - attempt * step, stream_index, preview,
                                  metadata), timeout=180)
    if not data:
        raise ValueError('No frame decoded at this timestamp.')
    return parse_ppm16(data)


def frame(path, seconds, stream_index=0, preview=True, profile='rec709', metadata=None):
    """Decode one frame and convert it to Rec.709 using the clip's color profile."""
    rgb = decode_rgb(path, seconds, stream_index, preview, metadata)
    out = color.to_rec709(rgb, profile, limited_range=decode_settings(metadata)[1] == 'tv')
    return Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), 'RGB')


def metrics(image):
    gray = np.asarray(image.resize((320, 180)).convert('L'), dtype=float)
    lap = (gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] +
           gray[1:-1, 2:] - 4 * gray[1:-1, 1:-1])
    return {'edge_variance': round(float(lap.var()), 2),
            'dark_fraction': round(float((gray < 8).mean()), 4),
            'bright_fraction': round(float((gray > 247).mean()), 4)}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(path)


def analyze(clip, cache, count=12):
    verify_source(clip)
    cache = Path(cache) / clip['id'] / f'samples-{count}'
    cache.mkdir(parents=True, exist_ok=True)
    manifest = cache / 'analysis.json'
    if manifest.exists():
        old = json.loads(manifest.read_text())
        if all(Path(f['preview']).exists() for f in old['frames']):
            return old
    metadata = probe(clip['path'])
    frames = []
    for i, t in enumerate(sample_times(metadata['duration'], count)):
        img = frame(clip['path'], t, metadata['stream_index'], metadata=metadata)
        target = cache / f'{i:03}.png'
        img.save(target)
        frames.append({'id': f"{clip['id']}-{round(t*1000000):012}",
                       'seconds': t, 'preview': str(target), **metrics(img)})
    result = {'clip': clip, 'metadata': metadata, 'frames': frames,
              'sampling': 'Uniform sparse samples; not a full clip review.'}
    atomic_json(manifest, result)
    return result


def safe_label(text):
    return re.sub(r'[^A-Za-z0-9_-]+', '-', text).strip('-')[:48] or 'Other'


def csv_safe(value):
    text = str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


def export_still(analysis, candidate, review, output, project='Project'):
    if review.get('decision') != 'Keep':
        raise ValueError('Only explicitly kept frames may be exported.')
    if review.get('color_verified') is not True:
        raise ValueError('Confirm display color before export.')
    clip = analysis['clip']
    verify_source(clip)
    root = Path(output).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Output parent must already exist; check the drive is mounted.')
    folder = root / (safe_label(project) + '_Still_Selects')
    folder.mkdir(exist_ok=True)
    if folder.is_symlink():
        raise ValueError('Export folder cannot be a symlink.')
    stem = (f"{safe_label(project)}_{safe_label(review.get('period', 'Unknown'))}_"
            f"{safe_label(review.get('tag', 'Other'))}_{candidate['id']}_"
            f"{safe_label(review.get('rank', 'B'))}")
    target = folder / (stem + '.png')
    # Exclusive creation prevents overwrites, including reruns.
    profile = analysis.get('profile', 'rec709')
    img = frame(clip['path'], candidate['seconds'], analysis['metadata']['stream_index'], False,
                profile, analysis['metadata'])
    with target.open('xb') as out:
        img.save(out, format='PNG')
    record = {'file': target.name, 'source': clip['path'], 'clip_id': clip['id'],
              'seconds': candidate['seconds'], 'timestamp_kind': 'seconds from clip start',
              'review': review, 'metadata': analysis['metadata'],
              'dimensions': list(img.size), 'transform': {**color.describe(profile),
                            'decode': dict(zip(('yuv_matrix', 'signal_range'),
                                               decode_settings(analysis['metadata']))),
                            'bit_depth': '16-bit decode, 8-bit PNG output'}}
    atomic_json(target.with_suffix('.json'), record)
    # Rebuild from sidecars to keep the index recoverable.
    records = [json.loads(p.read_text()) for p in sorted(folder.glob('*.json'))]
    tmp = folder / 'stills_index.csv.tmp'
    with tmp.open('w', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['file', 'source', 'seconds', 'rank', 'period', 'subject', 'notes'])
        for row in records:
            r = row['review']
            writer.writerow([csv_safe(x) for x in [row['file'], row['source'], row['seconds'],
                              r.get('rank'), r.get('period'), r.get('tag'), r.get('notes', '')]])
    tmp.replace(folder / 'stills_index.csv')
    return target
