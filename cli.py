"""Headless FrameFoundry commands for validation on a small, user-chosen folder.

    python cli.py project create "My Shoot" --prefix MyShoot --preset General
    python cli.py source add my-shoot /path/to/CameraA --label "Camera A"
    python cli.py scan my-shoot
    python cli.py enqueue my-shoot --samples 12
    python cli.py work --workers 2
    python cli.py autopick my-shoot --top 5
    python cli.py status my-shoot

Data lives in $FRAMEFOUNDRY_HOME (default ~/.framefoundry). Nothing is uploaded.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import scoring
from presets import PRESETS, parse_tags
from store import Store
from worker import DEFAULT_MIN_FREE_BYTES, WorkerPool


def home():
    return Path(os.environ.get('FRAMEFOUNDRY_HOME', Path.home() / '.framefoundry')).expanduser()


def open_store(base=None):
    base = Path(base) if base else home()
    return Store(base / 'framefoundry.sqlite'), base / 'cache'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='framefoundry')
    parser.add_argument('--home', help='Data folder (default $FRAMEFOUNDRY_HOME or ~/.framefoundry)')
    sub = parser.add_subparsers(dest='command', required=True)

    project = sub.add_parser('project').add_subparsers(dest='action', required=True)
    create = project.add_parser('create')
    create.add_argument('name')
    create.add_argument('--prefix')
    create.add_argument('--preset', choices=sorted(PRESETS), default='General')
    create.add_argument('--tags', help='Comma-separated tags (overrides --preset)')
    create.add_argument('--samples', type=int, default=12)
    project.add_parser('list')

    source = sub.add_parser('source').add_subparsers(dest='action', required=True)
    add = source.add_parser('add')
    add.add_argument('project')
    add.add_argument('path')
    add.add_argument('--label')

    for name in ('scan', 'status', 'cancel', 'resume'):
        sub.add_parser(name).add_argument('project')
    enqueue = sub.add_parser('enqueue')
    enqueue.add_argument('project')
    enqueue.add_argument('--samples', type=int)
    enqueue.add_argument('--force', action='store_true')

    exclude = sub.add_parser('exclude')
    exclude.add_argument('project')
    exclude.add_argument('rel_path')
    exclude.add_argument('--reason', default='')
    exclude.add_argument('--undo', action='store_true')

    autopick = sub.add_parser('autopick', help='Keep the top-N technical suggestions per clip')
    autopick.add_argument('project')
    autopick.add_argument('--top', type=int, default=5)
    autopick.add_argument('--reject-rest', action='store_true')

    work = sub.add_parser('work', help='Process queued jobs until the queue is empty')
    work.add_argument('--workers', type=int, default=2)
    work.add_argument('--min-free-mb', type=int, default=DEFAULT_MIN_FREE_BYTES // 1024 ** 2)

    args = parser.parse_args(argv)
    store, cache = open_store(args.home)
    out = lambda data: print(json.dumps(data, indent=2, default=str))

    if args.command == 'project' and args.action == 'create':
        tags = parse_tags(args.tags) if args.tags else PRESETS[args.preset]
        pid = store.create_project(args.name, args.prefix, tags, args.samples)
        out(store.project(pid))
    elif args.command == 'project':
        out([{k: p[k] for k in ('slug', 'name', 'export_prefix')} for p in store.projects()])
    elif args.command == 'source':
        p = store.project_by_slug(args.project)
        out({'source_id': store.add_source(p['id'], args.path, args.label)})
    elif args.command == 'scan':
        out(store.scan_project(store.project_by_slug(args.project)['id']))
    elif args.command == 'enqueue':
        p = store.project_by_slug(args.project)
        out({'queued': store.enqueue_analysis(p['id'], sample_count=args.samples,
                                              force=args.force)})
    elif args.command == 'exclude':
        p = store.project_by_slug(args.project)
        match = [c for c in store.clips(p['id']) if c['rel_path'] == args.rel_path]
        if len(match) != 1:
            sys.exit(f'{len(match)} clips match {args.rel_path!r}; expected exactly one.')
        out({'changed': store.set_excluded(match[0]['id'], not args.undo, args.reason)})
    elif args.command == 'cancel':
        out({'cancelled': store.cancel_jobs(store.project_by_slug(args.project)['id'])})
    elif args.command == 'resume':
        out({'resumed': store.resume_jobs(store.project_by_slug(args.project)['id'])})
    elif args.command == 'autopick':
        p = store.project_by_slug(args.project)
        names = {c['id']: f"{c['source_label']}/{c['rel_path']}" for c in store.clips(p['id'])}
        changed = scoring.autopick(store, p['id'], args.top, args.reject_rest)
        out({'frames_changed_per_clip': {names[k]: v for k, v in changed.items()}})
    elif args.command == 'work':
        pool = WorkerPool(store, cache, args.workers, min_free_bytes=args.min_free_mb * 1024 ** 2)
        try:
            pool.run_until_idle()
        except KeyboardInterrupt:
            pool.stop()
            print('Stopped; running jobs were returned to the queue.', file=sys.stderr)
        out({'recovered_at_start': pool.recovered})
    elif args.command == 'status':
        p = store.project_by_slug(args.project)
        clips = store.clips(p['id'])
        out({'project': p['name'], 'sources': [
                {k: s[k] for k in ('label', 'path', 'status', 'last_scan_at', 'last_error')}
                for s in store.sources(p['id'])],
             'clips': len(clips), 'excluded': sum(c['excluded'] for c in clips),
             'missing': sum(c['status'] == 'missing' for c in clips),
             'analyzed': sum(c['candidate_count'] > 0 for c in clips),
             'errors': {c['rel_path']: c['error'] for c in clips if c['error']},
             'jobs': store.job_counts(p['id']), 'kept': len(store.kept(p['id']))})


if __name__ == '__main__':
    main()
