"""Durable SQLite project model: projects, sources, clips, candidates, selections and jobs.

Every connection uses WAL, foreign keys and explicit transactions so a crash leaves
the database at the last committed state. Source media is only ever read (stat and
decode); nothing here writes to a source folder.
"""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import color
import core
from presets import PRESETS, parse_tags

SCHEMA_VERSION = 4
ACTIVE_JOB_STATES = ('queued', 'running', 'cancel_requested')
DECISIONS = ('Review', 'Keep', 'Reject')

# Version 1 schema; later versions are reached by applying MIGRATIONS in order, so new
# and upgraded databases always end up identical.
SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE projects(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  export_prefix TEXT NOT NULL,
  tags TEXT NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 12 CHECK(sample_count BETWEEN 1 AND 48),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE sources(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  label TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'online', 'offline')),
  last_scan_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(project_id, path));
CREATE TABLE clips(
  id TEXT PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  rel_path TEXT NOT NULL,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'present' CHECK(status IN ('present', 'missing')),
  probe TEXT,
  error TEXT,
  analyzed_fingerprint TEXT,
  excluded INTEGER NOT NULL DEFAULT 0,
  exclusion_reason TEXT NOT NULL DEFAULT '',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE(source_id, rel_path));
CREATE INDEX clips_project ON clips(project_id, source_id, rel_path);
CREATE TABLE exclusion_events(
  id INTEGER PRIMARY KEY,
  clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  excluded INTEGER NOT NULL,
  reason TEXT NOT NULL,
  at TEXT NOT NULL);
CREATE TABLE candidates(
  id TEXT PRIMARY KEY,
  clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  sample_set TEXT NOT NULL,
  seconds REAL NOT NULL,
  preview TEXT NOT NULL,
  metrics TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE INDEX candidates_clip ON candidates(clip_id, fingerprint, seconds);
CREATE TABLE selections(
  candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
  decision TEXT NOT NULL CHECK(decision IN ('Review', 'Keep', 'Reject')),
  rank TEXT NOT NULL DEFAULT 'B',
  tag TEXT NOT NULL DEFAULT 'Other',
  period TEXT NOT NULL DEFAULT 'Unknown',
  notes TEXT NOT NULL DEFAULT '',
  color_verified INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL);
CREATE TABLE jobs(
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  clip_id TEXT REFERENCES clips(id) ON DELETE CASCADE,
  params TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL CHECK(state IN
    ('queued', 'running', 'cancel_requested', 'done', 'failed', 'cancelled')),
  attempts INTEGER NOT NULL DEFAULT 0,
  progress REAL NOT NULL DEFAULT 0,
  error TEXT,
  worker TEXT,
  heartbeat REAL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT);
-- At most one active job per clip and kind: re-enqueueing or resuming never duplicates.
CREATE UNIQUE INDEX jobs_active ON jobs(kind, clip_id)
  WHERE state IN ('queued', 'running', 'cancel_requested');
CREATE INDEX jobs_state ON jobs(state, id);
CREATE TABLE exports(
  id INTEGER PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(id),
  file TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL);
"""


MIGRATIONS = {
    # Who made a selection decision: a person, or an automatic suggestion they triggered.
    2: ["ALTER TABLE selections ADD COLUMN decided_by TEXT NOT NULL DEFAULT 'human' "
        "CHECK(decided_by IN ('human', 'auto'))"],
    # Export jobs: one active job per candidate (several per clip), analysis unchanged.
    3: ['DROP INDEX jobs_active',
        "CREATE UNIQUE INDEX jobs_active ON jobs(kind, clip_id, "
        "COALESCE(json_extract(params, '$.candidate_id'), '')) "
        "WHERE state IN ('queued', 'running', 'cancel_requested')"],
    # Input color profiles (log/HDR -> Rec.709) per source and per clip.
    4: ["ALTER TABLE sources ADD COLUMN color_profile TEXT NOT NULL DEFAULT 'auto'",
        "ALTER TABLE clips ADD COLUMN color_profile TEXT NOT NULL DEFAULT 'auto'",
        'ALTER TABLE clips ADD COLUMN detected_profile TEXT',
        'ALTER TABLE clips ADD COLUMN detection_note TEXT',
        'ALTER TABLE clips ADD COLUMN color_note TEXT',
        'ALTER TABLE clips ADD COLUMN analyzed_profile TEXT',
        # Earlier analyses decoded without conversion.
        "UPDATE clips SET analyzed_profile='rec709' WHERE analyzed_fingerprint IS NOT NULL"],
}
JOB_TARGET = "COALESCE(json_extract({t}.params, '$.candidate_id'), '')"


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def slugify(text):
    return core.safe_label(text).lower()


def _is_within(a, b):
    return a == b or b in a.parents


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
                for statement in SCHEMA.split(';'):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO meta VALUES('schema_version', '1')")
            version = int(db.execute("SELECT value FROM meta WHERE key='schema_version'")
                          .fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f'Database schema version {version} is newer than this '
                                   'version of FrameFoundry supports.')
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[target]:
                    db.execute(statement)
                db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(target),))

    # -- connection helpers -------------------------------------------------
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                             check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        return db

    @contextmanager
    def tx(self):
        """One IMMEDIATE transaction: all writes commit together or not at all."""
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.execute('ROLLBACK')
                raise
            db.execute('COMMIT')

    def query(self, sql, args=()):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    # -- projects ---------------------------------------------------------
    def create_project(self, name, export_prefix=None, tags=None, sample_count=12):
        name = str(name).strip()
        if not name:
            raise ValueError('Project name is required.')
        tags = parse_tags(','.join(tags if tags is not None else PRESETS['General']))
        prefix = core.safe_label(export_prefix or name)
        stamp = now()
        with self.tx() as db:
            cur = db.execute(
                'INSERT INTO projects(name, slug, export_prefix, tags, sample_count, '
                'created_at, updated_at) VALUES(?,?,?,?,?,?,?)',
                (name, slugify(name), prefix, json.dumps(tags), int(sample_count), stamp, stamp))
            return cur.lastrowid

    def update_project(self, project_id, export_prefix=None, tags=None, sample_count=None):
        project = self.project(project_id)
        prefix = core.safe_label(export_prefix) if export_prefix is not None else project['export_prefix']
        tags = parse_tags(','.join(tags)) if tags is not None else project['tags']
        count = int(sample_count) if sample_count is not None else project['sample_count']
        with self.tx() as db:
            db.execute('UPDATE projects SET export_prefix=?, tags=?, sample_count=?, updated_at=? '
                       'WHERE id=?', (prefix, json.dumps(tags), count, now(), project_id))

    def project(self, project_id):
        row = self.one('SELECT * FROM projects WHERE id=?', (project_id,))
        if not row:
            raise KeyError(f'No project {project_id}')
        row['tags'] = json.loads(row['tags'])
        return row

    def project_by_slug(self, slug):
        row = self.one('SELECT id FROM projects WHERE slug=?', (slugify(slug),))
        if not row:
            raise KeyError(f'No project named {slug!r}')
        return self.project(row['id'])

    def projects(self):
        return [self.project(r['id']) for r in self.query('SELECT id FROM projects ORDER BY name')]

    # -- sources and inventory --------------------------------------------
    def add_source(self, project_id, path, label=None):
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError('Source must be an existing folder.')
        for other in self.sources(project_id):
            existing = Path(other['path'])
            if _is_within(root, existing) or _is_within(existing, root):
                raise ValueError(f'Overlaps existing source {other["label"]!r}; '
                                 'nested sources would list clips twice.')
        label = str(label or root.name).strip() or root.name
        with self.tx() as db:
            cur = db.execute('INSERT INTO sources(project_id, path, label, created_at) '
                             'VALUES(?,?,?,?)', (project_id, str(root), label, now()))
            return cur.lastrowid

    def sources(self, project_id):
        return self.query('SELECT * FROM sources WHERE project_id=? ORDER BY id', (project_id,))

    def scan_source(self, source_id):
        """Inventory one source. An unreachable root marks the source offline and leaves its
        clips (and their decisions) untouched so a remount recovers them."""
        source = self.one('SELECT * FROM sources WHERE id=?', (source_id,))
        stamp = now()
        try:
            found = core.inventory(source['path'])
        except (OSError, ValueError) as error:
            with self.tx() as db:
                db.execute("UPDATE sources SET status='offline', last_error=?, last_scan_at=? "
                           'WHERE id=?', (str(error), stamp, source_id))
            return {'offline': True, 'error': str(error), 'added': 0, 'changed': 0,
                    'missing': 0, 'unchanged': 0}
        counts = dict(offline=False, error=None, added=0, changed=0, missing=0, unchanged=0)
        with self.tx() as db:
            known = {r['rel_path']: dict(r) for r in db.execute(
                'SELECT * FROM clips WHERE source_id=?', (source_id,))}
            for item in found:
                rel = item['name']
                old = known.pop(rel, None)
                if old is None:
                    clip_id = hashlib.sha256(
                        f"{source['project_id']}|{source_id}|{rel}".encode()).hexdigest()[:16]
                    db.execute(
                        'INSERT INTO clips(id, project_id, source_id, rel_path, path, size, '
                        'mtime_ns, fingerprint, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (clip_id, source['project_id'], source_id, rel, item['path'],
                         item['size'], item['mtime_ns'], item['id'], stamp, stamp))
                    counts['added'] += 1
                elif old['fingerprint'] != item['id'] or old['status'] != 'present':
                    # A changed file needs re-analysis; stale candidates stay for history but
                    # are hidden and cannot be exported (export re-verifies size/mtime).
                    changed = old['fingerprint'] != item['id']
                    db.execute("UPDATE clips SET path=?, size=?, mtime_ns=?, fingerprint=?, "
                               "status='present', last_seen=?, error=CASE WHEN ? THEN NULL "
                               'ELSE error END WHERE id=?',
                               (item['path'], item['size'], item['mtime_ns'], item['id'],
                                stamp, changed, old['id']))
                    counts['changed' if changed else 'unchanged'] += 1
                else:
                    db.execute('UPDATE clips SET last_seen=? WHERE id=?', (stamp, old['id']))
                    counts['unchanged'] += 1
            counts['removed'] = 0
            for old in known.values():
                has_candidates = db.execute('SELECT 1 FROM candidates WHERE clip_id=? LIMIT 1',
                                            (old['id'],)).fetchone()
                if core.is_hidden(old['rel_path']) and not has_candidates:
                    # Hidden/AppleDouble entries listed by earlier versions: never real video.
                    db.execute('DELETE FROM clips WHERE id=?', (old['id'],))
                    counts['removed'] += 1
                    continue
                if old['status'] != 'missing':
                    db.execute("UPDATE clips SET status='missing' WHERE id=?", (old['id'],))
                counts['missing'] += 1
            db.execute("UPDATE sources SET status='online', last_error=NULL, last_scan_at=? "
                       'WHERE id=?', (stamp, source_id))
        return counts

    def scan_project(self, project_id):
        return {s['id']: self.scan_source(s['id']) for s in self.sources(project_id)}

    def clips(self, project_id, include_excluded=True):
        rows = self.query(
            'SELECT c.*, s.label AS source_label, s.status AS source_status, '
            's.color_profile AS source_profile, '
            '(SELECT COUNT(*) FROM candidates k WHERE k.clip_id=c.id '
            ' AND k.fingerprint=c.fingerprint) AS candidate_count, '
            "(SELECT j.state FROM jobs j WHERE j.clip_id=c.id AND j.kind='analyze' "
            ' ORDER BY j.id DESC LIMIT 1) '
            ' AS job_state '
            'FROM clips c JOIN sources s ON s.id=c.source_id WHERE c.project_id=? '
            + ('' if include_excluded else 'AND c.excluded=0 ') +
            'ORDER BY s.label, c.rel_path', (project_id,))
        for row in rows:
            self._decorate(row)
        return rows

    @staticmethod
    def _decorate(row):
        row['probe'] = json.loads(row['probe']) if row['probe'] else None
        row['excluded'] = bool(row['excluded'])
        row['profile'], row['profile_reason'] = color.resolve(
            row['color_profile'], row['source_profile'], row['detected_profile'])
        return row

    def clip(self, clip_id):
        row = self.one('SELECT c.*, s.color_profile AS source_profile FROM clips c '
                       'JOIN sources s ON s.id=c.source_id WHERE c.id=?', (clip_id,))
        if not row:
            raise KeyError(f'No clip {clip_id}')
        return self._decorate(row)

    def set_source_profile(self, source_id, profile):
        profile = color.validate(profile)
        with self.tx() as db:
            db.execute('UPDATE sources SET color_profile=? WHERE id=?', (profile, source_id))
        return profile

    def set_clip_profile(self, clip_id, profile):
        profile = color.validate(profile)
        with self.tx() as db:
            db.execute('UPDATE clips SET color_profile=? WHERE id=?', (profile, clip_id))
        return profile

    def record_detection(self, clip_id, detected, note):
        with self.tx() as db:
            db.execute('UPDATE clips SET detected_profile=?, detection_note=? WHERE id=?',
                       (detected, note, clip_id))

    def set_excluded(self, clip_id, excluded, reason=''):
        """Reversible exclusion; every change is appended to an audit trail."""
        excluded = bool(excluded)
        reason = str(reason or '').strip()
        with self.tx() as db:
            row = db.execute('SELECT excluded, exclusion_reason FROM clips WHERE id=?',
                             (clip_id,)).fetchone()
            if row is None:
                raise KeyError(f'No clip {clip_id}')
            if bool(row['excluded']) == excluded and row['exclusion_reason'] == reason:
                return False
            db.execute('UPDATE clips SET excluded=?, exclusion_reason=? WHERE id=?',
                       (int(excluded), reason, clip_id))
            db.execute('INSERT INTO exclusion_events(clip_id, excluded, reason, at) '
                       'VALUES(?,?,?,?)', (clip_id, int(excluded), reason, now()))
            return True

    def exclusion_history(self, clip_id):
        return self.query('SELECT * FROM exclusion_events WHERE clip_id=? ORDER BY id', (clip_id,))

    # -- candidates and selections ----------------------------------------
    @staticmethod
    def candidate_id(clip_id, fingerprint, seconds):
        base = hashlib.sha256(f'{clip_id}|{fingerprint}'.encode()).hexdigest()[:16]
        return f'{base}-{round(seconds * 1_000_000):012}'

    def complete_analysis(self, clip_id, fingerprint, probe, sample_set, frames,
                          profile='rec709', color_note=None):
        """Atomically record candidates for one analysis. Upserts keep existing selections."""
        stamp = now()
        with self.tx() as db:
            for f in frames:
                cid = self.candidate_id(clip_id, fingerprint, f['seconds'])
                db.execute(
                    'INSERT INTO candidates(id, clip_id, fingerprint, sample_set, seconds, '
                    'preview, metrics, created_at) VALUES(?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET preview=excluded.preview, '
                    'metrics=excluded.metrics',
                    (cid, clip_id, fingerprint, sample_set, f['seconds'], f['preview'],
                     json.dumps(f['metrics']), stamp))
            # Only mark analyzed if the clip was not rescanned as changed meanwhile.
            db.execute('UPDATE clips SET probe=?, error=NULL, analyzed_fingerprint=?, '
                       'analyzed_profile=?, color_note=? WHERE id=? AND fingerprint=?',
                       (json.dumps(probe), fingerprint, profile, color_note, clip_id,
                        fingerprint))

    def record_clip_error(self, clip_id, error):
        with self.tx() as db:
            db.execute('UPDATE clips SET error=? WHERE id=?', (str(error)[-1800:], clip_id))

    def candidates(self, clip_id):
        """Candidates for the clip's current source state, in time order, with selections."""
        rows = self.query(
            'SELECT k.*, s.decision, s.rank, s.tag, s.period, s.notes, s.color_verified, '
            's.decided_by, s.updated_at AS reviewed_at FROM candidates k JOIN clips c ON c.id=k.clip_id '
            'LEFT JOIN selections s ON s.candidate_id=k.id '
            'WHERE k.clip_id=? AND k.fingerprint=c.fingerprint ORDER BY k.seconds', (clip_id,))
        for row in rows:
            row['metrics'] = json.loads(row['metrics'])
            row['decision'] = row['decision'] or 'Review'
            row['color_verified'] = bool(row['color_verified'])
        return rows

    def candidate(self, candidate_id):
        row = self.one('SELECT * FROM candidates WHERE id=?', (candidate_id,))
        if not row:
            raise KeyError(f'No candidate {candidate_id}')
        row['metrics'] = json.loads(row['metrics'])
        return row

    def selection(self, candidate_id):
        row = self.one('SELECT * FROM selections WHERE candidate_id=?', (candidate_id,))
        if row:
            row['color_verified'] = bool(row['color_verified'])
        return row

    def save_selection(self, candidate_id, decided_by='human', **fields):
        """Merge the given review fields into the candidate's persisted selection."""
        if decided_by not in ('human', 'auto'):
            raise ValueError("decided_by must be 'human' or 'auto'.")
        allowed = {'decision', 'rank', 'tag', 'period', 'notes', 'color_verified'}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f'Unknown selection fields: {sorted(unknown)}')
        if 'decision' in fields and fields['decision'] not in DECISIONS:
            raise ValueError(f'Decision must be one of {DECISIONS}.')
        current = self.selection(candidate_id) or {
            'decision': 'Review', 'rank': 'B', 'tag': 'Other', 'period': 'Unknown',
            'notes': '', 'color_verified': False}
        current.update(fields)
        with self.tx() as db:
            db.execute(
                'INSERT INTO selections(candidate_id, decision, rank, tag, period, notes, '
                'color_verified, decided_by, updated_at) VALUES(?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(candidate_id) DO UPDATE SET decision=excluded.decision, '
                'rank=excluded.rank, tag=excluded.tag, period=excluded.period, '
                'notes=excluded.notes, color_verified=excluded.color_verified, '
                'decided_by=excluded.decided_by, updated_at=excluded.updated_at',
                (candidate_id, current['decision'], current['rank'], current['tag'],
                 current['period'], current['notes'], int(bool(current['color_verified'])),
                 decided_by, now()))

    def bulk_decide(self, candidate_ids, decision, decided_by='auto', overwrite_auto=True):
        """Set one decision on many candidates in a single transaction. Never overrides a
        person's decision: only undecided frames (and, if overwrite_auto, earlier automatic
        decisions) change. Returns the number of frames changed."""
        if decision not in DECISIONS:
            raise ValueError(f'Decision must be one of {DECISIONS}.')
        changed, stamp = 0, now()
        with self.tx() as db:
            for cid in candidate_ids:
                row = db.execute('SELECT decision, decided_by FROM selections '
                                 'WHERE candidate_id=?', (cid,)).fetchone()
                if row is None:
                    db.execute('INSERT INTO selections(candidate_id, decision, decided_by, '
                               'updated_at) VALUES(?,?,?,?)', (cid, decision, decided_by, stamp))
                    changed += 1
                    continue
                is_open = row['decision'] == 'Review' or (
                    overwrite_auto and row['decided_by'] == 'auto')
                if is_open and (row['decision'], row['decided_by']) != (decision, decided_by):
                    db.execute('UPDATE selections SET decision=?, decided_by=?, updated_at=? '
                               'WHERE candidate_id=?', (decision, decided_by, stamp, cid))
                    changed += 1
        return changed

    def undo_auto(self, clip_id=None, project_id=None):
        """Return automatic decisions to Review (for one clip or a whole project)."""
        where, args = ('k.clip_id=?', [clip_id]) if clip_id else ('c.project_id=?', [project_id])
        with self.tx() as db:
            return db.execute(
                "UPDATE selections SET decision='Review', updated_at=? WHERE decided_by='auto' "
                "AND decision!='Review' AND candidate_id IN (SELECT k.id FROM candidates k "
                f"JOIN clips c ON c.id=k.clip_id WHERE {where})", [now()] + args).rowcount

    def mark_color_verified(self, candidate_ids):
        """Record the reviewer's attestation that display color was checked."""
        with self.tx() as db:
            return sum(db.execute('UPDATE selections SET color_verified=1, updated_at=? '
                                  'WHERE candidate_id=? AND color_verified=0',
                                  (now(), cid)).rowcount for cid in candidate_ids)

    def verify_all_kept(self, project_id):
        """Record the reviewer's attestation that display color was checked for every
        kept frame in the project."""
        return self.mark_color_verified([r['id'] for r in self.kept(project_id)])

    def clip_progress(self, project_id):
        """Per-clip decision counts for the clip's current candidates."""
        rows = self.query(
            "SELECT k.clip_id, COALESCE(s.decision, 'Review') AS decision, COUNT(*) AS n "
            "FROM candidates k JOIN clips c ON c.id=k.clip_id AND k.fingerprint=c.fingerprint "
            "LEFT JOIN selections s ON s.candidate_id=k.id WHERE c.project_id=? "
            "GROUP BY k.clip_id, COALESCE(s.decision, 'Review')", (project_id,))
        progress = {}
        for r in rows:
            progress.setdefault(r['clip_id'], dict.fromkeys(DECISIONS, 0))[r['decision']] = r['n']
        return progress

    def kept(self, project_id):
        return self.query(
            "SELECT k.*, c.rel_path, src.label AS source_label, s.rank, s.tag, s.period, "
            "s.color_verified, s.decided_by, (SELECT file FROM exports e WHERE e.candidate_id=k.id "
            " ORDER BY e.id DESC LIMIT 1) AS exported_file "
            "FROM selections s JOIN candidates k ON k.id=s.candidate_id "
            "JOIN clips c ON c.id=k.clip_id JOIN sources src ON src.id=c.source_id "
            "WHERE c.project_id=? AND s.decision='Keep' AND k.fingerprint=c.fingerprint "
            "ORDER BY src.label, c.rel_path, k.seconds", (project_id,))

    def export_candidate(self, candidate_id, output):
        """Export one kept frame at full resolution using the project's export prefix."""
        k = self.candidate(candidate_id)
        clip = self.clip(k['clip_id'])
        if k['fingerprint'] != clip['fingerprint']:
            raise ValueError('Source changed since this frame was sampled. Re-analyze first.')
        if clip['probe'] is None:
            raise ValueError('Clip has not been analyzed.')
        if clip['analyzed_profile'] != clip['profile']:
            raise ValueError('Color profile changed since analysis. Re-analyze this clip '
                             'before exporting.')
        review = self.selection(candidate_id) or {}
        review = {key: review.get(key) for key in
                  ('decision', 'decided_by', 'rank', 'tag', 'period', 'notes',
                   'color_verified')}
        project = self.project(clip['project_id'])
        analysis = {'clip': {'id': clip['fingerprint'], 'path': clip['path'],
                             'size': clip['size'], 'mtime_ns': clip['mtime_ns']},
                    'metadata': clip['probe'], 'profile': clip['profile']}
        target = core.export_still(analysis, {'id': candidate_id, 'seconds': k['seconds']},
                                   review, output, project['export_prefix'])
        with self.tx() as db:
            db.execute('INSERT INTO exports(candidate_id, file, created_at) VALUES(?,?,?)',
                       (candidate_id, str(target), now()))
        return target

    # -- jobs -----------------------------------------------------------------
    def enqueue_analysis(self, project_id, clip_ids=None, sample_count=None, force=False):
        """Queue analysis for present, non-excluded clips lacking current candidates.
        Returns the number of jobs added. Clips with an active job are skipped, as are
        clips whose last job for the same source state failed or was cancelled (retry
        those with resume_jobs, or pass force=True)."""
        project = self.project(project_id)
        count = int(sample_count or project['sample_count'])
        clips = [c for c in self.clips(project_id, include_excluded=False)
                 if c['status'] == 'present' and (clip_ids is None or c['id'] in clip_ids)]
        stamp = now()
        added = 0
        with self.tx() as db:
            for c in clips:
                if not force and c['analyzed_fingerprint'] == c['fingerprint'] and \
                        c['analyzed_profile'] == c['profile'] and db.execute(
                        'SELECT 1 FROM candidates WHERE clip_id=? AND fingerprint=? '
                        'AND sample_set=?', (c['id'], c['fingerprint'], f'uniform-{count}')
                ).fetchone():
                    continue
                last = db.execute("SELECT state, params FROM jobs WHERE kind='analyze' AND "
                                  'clip_id=? ORDER BY id DESC LIMIT 1', (c['id'],)).fetchone()
                if (not force and last and last['state'] in ('failed', 'cancelled') and
                        json.loads(last['params']).get('fingerprint') == c['fingerprint'] and
                        json.loads(last['params']).get('profile', c['profile']) == c['profile']):
                    continue
                params = json.dumps({'sample_count': count, 'fingerprint': c['fingerprint'],
                                     'profile': c['profile']})
                cur = db.execute(
                    "INSERT INTO jobs(project_id, kind, clip_id, params, state, created_at) "
                    "VALUES(?, 'analyze', ?, ?, 'queued', ?) ON CONFLICT DO NOTHING",
                    (project_id, c['id'], params, stamp))
                added += cur.rowcount
        return added

    def enqueue_exports(self, project_id, candidate_ids, output):
        """Queue background exports of kept frames to an existing output parent folder.
        Returns the number of jobs added (frames already queued are skipped)."""
        root = Path(output).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError('Output parent must be an existing folder.')
        stamp, added = now(), 0
        with self.tx() as db:
            for cid in candidate_ids:
                row = db.execute('SELECT k.clip_id FROM candidates k JOIN clips c ON '
                                 'c.id=k.clip_id WHERE k.id=? AND c.project_id=?',
                                 (cid, project_id)).fetchone()
                if row is None:
                    continue
                params = json.dumps({'candidate_id': cid, 'output': str(root)})
                added += db.execute(
                    "INSERT INTO jobs(project_id, kind, clip_id, params, state, created_at) "
                    "VALUES(?, 'export', ?, ?, 'queued', ?) ON CONFLICT DO NOTHING",
                    (project_id, row['clip_id'], params, stamp)).rowcount
        return added

    def job_counts_by_kind(self, project_id, kind):
        rows = self.query('SELECT state, COUNT(*) AS n FROM jobs WHERE project_id=? AND kind=? '
                          'GROUP BY state', (project_id, kind))
        return {r['state']: r['n'] for r in rows}

    def claim_job(self, worker):
        with self.tx() as db:
            row = db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1"
                             ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running', worker=?, attempts=attempts+1, "
                       "heartbeat=?, started_at=?, error=NULL, progress=0 WHERE id=?",
                       (worker, time.time(), now(), row['id']))
            job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
        job['params'] = json.loads(job['params'])
        return job

    def heartbeat(self, job_id, progress=None):
        """Refresh the lease and return the job's current state (to observe cancellation)."""
        with self.tx() as db:
            if progress is None:
                db.execute('UPDATE jobs SET heartbeat=? WHERE id=?', (time.time(), job_id))
            else:
                db.execute('UPDATE jobs SET heartbeat=?, progress=? WHERE id=?',
                           (time.time(), float(progress), job_id))
            return db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()['state']

    def finish_job(self, job_id, state, error=None):
        if state not in ('done', 'failed', 'cancelled', 'queued'):
            raise ValueError(state)
        with self.tx() as db:
            db.execute("UPDATE jobs SET state=?, error=?, worker=NULL, "
                       "finished_at=CASE WHEN ?='queued' THEN NULL ELSE ? END, "
                       "progress=CASE WHEN ?='done' THEN 1 ELSE progress END "
                       "WHERE id=? AND state IN ('running', 'cancel_requested')",
                       (state, error, state, now(), state, job_id))

    def cancel_jobs(self, project_id, job_ids=None):
        """Queued jobs cancel immediately; running ones stop at the next checkpoint."""
        where, args = 'project_id=?', [project_id]
        if job_ids is not None:
            where += f" AND id IN ({','.join('?' * len(job_ids))})"
            args += list(job_ids)
        with self.tx() as db:
            a = db.execute(f"UPDATE jobs SET state='cancelled', finished_at=? "
                           f"WHERE {where} AND state='queued'", [now()] + args).rowcount
            b = db.execute(f"UPDATE jobs SET state='cancel_requested' "
                           f"WHERE {where} AND state='running'", args).rowcount
        return a + b

    def resume_jobs(self, project_id, states=('cancelled', 'failed')):
        """Re-queue the latest cancelled/failed job per clip, reusing the same row."""
        resumed = 0
        with self.tx() as db:
            jt, xt = JOB_TARGET.format(t='j'), JOB_TARGET.format(t='x')
            rows = db.execute(
                f"SELECT j.id, j.kind, j.clip_id, {jt} AS target FROM jobs j "
                f"WHERE j.project_id=? AND j.state IN ({','.join('?' * len(states))}) "
                f"AND j.id=(SELECT MAX(id) FROM jobs x WHERE x.kind=j.kind "
                f"AND x.clip_id IS j.clip_id AND {xt}={jt})",
                [project_id] + list(states)).fetchall()
            for row in rows:
                clip = db.execute('SELECT excluded, status FROM clips WHERE id=?',
                                  (row['clip_id'],)).fetchone()
                if clip is None or clip['status'] != 'present' or (
                        clip['excluded'] and row['kind'] == 'analyze'):
                    continue
                if db.execute(f"SELECT 1 FROM jobs x WHERE kind=? AND clip_id=? AND {xt}=? "
                              "AND state IN ('queued','running','cancel_requested')",
                              (row['kind'], row['clip_id'], row['target'])).fetchone():
                    continue
                db.execute("UPDATE jobs SET state='queued', attempts=0, error=NULL, "
                           "progress=0, finished_at=NULL WHERE id=?", (row['id'],))
                resumed += 1
        return resumed

    def recover_jobs(self, stale_after=30.0, max_attempts=3):
        """Return jobs orphaned by a crashed or killed worker to the queue."""
        cutoff = time.time() - stale_after
        counts = {'requeued': 0, 'failed': 0, 'cancelled': 0}
        with self.tx() as db:
            stale = "state=? AND (heartbeat IS NULL OR heartbeat < ?)"
            counts['cancelled'] = db.execute(
                f"UPDATE jobs SET state='cancelled', worker=NULL, finished_at=? WHERE {stale}",
                (now(), 'cancel_requested', cutoff)).rowcount
            counts['failed'] = db.execute(
                f"UPDATE jobs SET state='failed', worker=NULL, finished_at=?, error="
                f"'Interrupted ' || attempts || ' times; not retried automatically.' "
                f"WHERE {stale} AND attempts>=?", (now(), 'running', cutoff, max_attempts)
            ).rowcount
            counts['requeued'] = db.execute(
                f"UPDATE jobs SET state='queued', worker=NULL WHERE {stale}",
                ('running', cutoff)).rowcount
        return counts

    def job(self, job_id):
        row = self.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        if row:
            row['params'] = json.loads(row['params'])
        return row

    def jobs(self, project_id, limit=200):
        return self.query(
            'SELECT j.*, c.rel_path, s.label AS source_label FROM jobs j '
            'LEFT JOIN clips c ON c.id=j.clip_id LEFT JOIN sources s ON s.id=c.source_id '
            'WHERE j.project_id=? ORDER BY j.id DESC LIMIT ?', (project_id, limit))

    def failure_summary(self, project_id):
        """Failed jobs grouped by the first line of their error, largest group first."""
        rows = self.query(
            "SELECT j.id, j.kind, j.error, c.rel_path FROM jobs j LEFT JOIN clips c "
            "ON c.id=j.clip_id WHERE j.project_id=? AND j.state='failed' ORDER BY j.id",
            (project_id,))
        groups = {}
        for r in rows:
            first_line = (r['error'] or '').strip().splitlines()[:1]
            # FFmpeg prefixes lines with a varying '[demuxer @ 0x...]' address; drop it.
            key = (r['kind'], re.sub(r' @ 0x[0-9a-f]+', '', first_line[0] if first_line else ''))
            g = groups.setdefault(key, {'kind': r['kind'], 'error': (r['error'] or '')[-600:],
                                        'n': 0, 'first': r['id'], 'last': r['id'],
                                        'example': r['rel_path']})
            g['n'] += 1
            g['last'] = r['id']
        return sorted(groups.values(), key=lambda g: -g['n'])

    def job_counts(self, project_id):
        rows = self.query('SELECT state, COUNT(*) AS n FROM jobs WHERE project_id=? '
                          'GROUP BY state', (project_id,))
        return {r['state']: r['n'] for r in rows}

    def active_job_count(self, project_id=None):
        sql = ("SELECT COUNT(*) AS n FROM jobs WHERE state IN "
               "('queued','running','cancel_requested')")
        if project_id is None:
            return self.one(sql)['n']
        return self.one(sql + ' AND project_id=?', (project_id,))['n']
