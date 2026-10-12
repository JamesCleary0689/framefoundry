"""FrameFoundry — local review workbench. Run: streamlit run app.py --server.address 127.0.0.1"""
import os

import pandas as pd
import streamlit as st

import picker
from cli import home
from core import dependencies
from presets import PRESETS, parse_tags
from store import DECISIONS, Store
from worker import DEFAULT_MIN_FREE_BYTES, WorkerPool

st.set_page_config(page_title='FrameFoundry', page_icon='🎞️', layout='wide')
st.title('FrameFoundry')
st.caption('Your footage. The frames worth keeping. • Local review workbench — no media is uploaded')
missing = dependencies()
if missing:
    st.error('Install FFmpeg before continuing. Missing: ' + ', '.join(missing))
    st.stop()


@st.cache_resource
def services(base):
    """One store and one bounded worker pool per app process. Queued jobs left over from a
    previous session resume automatically; orphaned running jobs are recovered first."""
    store = Store(os.path.join(base, 'framefoundry.sqlite'))
    pool = WorkerPool(store, os.path.join(base, 'cache'),
                      max_workers=int(os.environ.get('FRAMEFOUNDRY_WORKERS', 2)),
                      min_free_bytes=int(os.environ.get('FRAMEFOUNDRY_MIN_FREE_MB',
                                                        DEFAULT_MIN_FREE_BYTES // 1024 ** 2))
                      * 1024 ** 2)
    pool.start()
    return store, pool


store, pool = services(str(home()))
NEW = 0


def browse(key, prompt):
    """Open the computer's native folder window and put the choice into a text field."""
    try:
        chosen = picker.pick_folder(prompt, st.session_state.get(key) or None)
    except Exception as e:
        st.session_state[f'{key}-error'] = str(e)
        return
    if chosen:
        st.session_state[key] = chosen


def folder_field(label, key, prompt, placeholder=''):
    """Path text field with a Browse… button that opens a native folder window."""
    left, right = st.columns([5, 1], vertical_alignment='bottom')
    value = left.text_input(label, key=key, placeholder=placeholder)
    right.button('Browse…', key=f'{key}-browse', on_click=browse, args=(key, prompt),
                 disabled=not picker.available(), width='stretch',
                 help='Choose a folder on this computer' if picker.available() else
                      'No folder window is available on this system; type the path.')
    error = st.session_state.pop(f'{key}-error', None)
    if error:
        st.error(error)
    return value

# -- project ----------------------------------------------------------------
with st.sidebar:
    st.header('Project')
    projects = {p['id']: p for p in store.projects()}
    options = list(projects) + [NEW]
    default = st.session_state.get('project_id', options[0])
    pid = st.selectbox('Project', options, index=options.index(default) if default in options else 0,
                       format_func=lambda i: projects[i]['name'] if i else '+ New project')
    st.session_state['project_id'] = pid
    if pid == NEW:
        name = st.text_input('Project name')
        prefix = st.text_input('Export prefix', placeholder='Defaults to the project name')
        preset = st.selectbox('Starting subject tags', list(PRESETS))
        tags = st.text_area('Subject tags, comma separated', ', '.join(PRESETS[preset]),
                            key=f'new-tags-{preset}')
        samples = st.slider('Samples per clip', 4, 48, 12, 4)
        if st.button('Create project', type='primary'):
            try:
                st.session_state['project_id'] = store.create_project(
                    name, prefix or None, parse_tags(tags), samples)
                st.rerun()
            except Exception as e:
                st.error(str(e))
        st.stop()
    project = projects[pid]
    with st.expander('Project settings'):
        with st.form(f'settings-{pid}'):
            prefix = st.text_input('Export prefix', project['export_prefix'])
            tags = st.text_area('Subject tags, comma separated', ', '.join(project['tags']))
            samples = st.slider('Samples per clip', 4, 48, project['sample_count'], 4)
            if st.form_submit_button('Save settings'):
                store.update_project(pid, prefix, parse_tags(tags), samples)
                st.rerun()
    st.caption(f"Data: `{home()}` · {pool.max_workers} worker(s) "
               f"({'running' if pool.running else 'stopped'})")
    if pool.recovered and any(pool.recovered.values()):
        st.caption(f'Recovered at start-up: {pool.recovered}')
    st.caption('Uniform sparse sampling can miss brief moments. Scene-aware sampling '
               'and exact frame navigation are planned.')

sources_tab, clips_tab, queue_tab, sheet_tab, export_tab = st.tabs(
    ['Sources', 'Clips', 'Queue', 'Contact sheet', 'Selections & export'])

# -- sources ------------------------------------------------------------------
with sources_tab:
    def add_source():
        try:
            store.add_source(pid, st.session_state.get('source-path', '').strip(),
                             st.session_state.get('source-label', '').strip() or None)
            st.session_state['source-path'] = st.session_state['source-label'] = ''
            st.session_state['source-message'] = ('success', 'Added. Scan to inventory it.')
        except Exception as e:
            st.session_state['source-message'] = ('error', str(e))

    folder_field('Source folder on this computer', 'source-path',
                 'Choose a folder of video clips (e.g. one camera or card)',
                 '/Volumes/CardA/Footage')
    left, right = st.columns([5, 1], vertical_alignment='bottom')
    left.text_input('Label (optional)', key='source-label', placeholder='Camera A')
    right.button('Add source', on_click=add_source, width='stretch',
                 disabled=not st.session_state.get('source-path', '').strip())
    kind, message = st.session_state.pop('source-message', (None, None))
    if kind:
        getattr(st, kind)(message)
    sources = store.sources(pid)
    if sources:
        if st.button('Scan all sources', type='primary'):
            with st.spinner('Listing video files (read-only)…'):
                for sid, result in store.scan_project(pid).items():
                    label = next(s['label'] for s in sources if s['id'] == sid)
                    if result['offline']:
                        st.warning(f"{label}: offline — {result['error']}. Clips and decisions "
                                   'are kept; rescan once the drive is mounted.')
                    else:
                        st.write(f"{label}: {result['added']} new, {result['changed']} changed, "
                                 f"{result['missing']} missing, {result['unchanged']} unchanged")
            sources = store.sources(pid)
        st.dataframe(pd.DataFrame(sources)[['label', 'path', 'status', 'last_scan_at',
                                            'last_error']], hide_index=True)
    else:
        st.info('Add one or more source folders (e.g. one per camera or card).')

# -- clips --------------------------------------------------------------------
with clips_tab:
    clips = store.clips(pid)
    if not clips:
        st.info('No clips yet. Add and scan a source.')
    else:
        st.write(f"{len(clips)} clips · {sum(c['excluded'] for c in clips)} excluded · "
                 f"{sum(c['status'] == 'missing' for c in clips)} missing · originals untouched")
        rows = [{'id': c['id'], 'exclude': c['excluded'], 'reason': c['exclusion_reason'],
                 'source': c['source_label'], 'clip': c['rel_path'],
                 'MB': round(c['size'] / 1e6, 1),
                 'status': c['status'] if c['source_status'] != 'offline' else 'source offline',
                 'analysis': c['job_state'] or ('done' if c['candidate_count'] else '—'),
                 'candidates': c['candidate_count'],
                 'codec': (c['probe'] or {}).get('codec'),
                 'size': f"{c['probe']['width']}×{c['probe']['height']}" if c['probe'] else None,
                 'seconds': round(c['probe']['duration'], 2) if c['probe'] else None,
                 'error': c['error']} for c in clips]
        version = st.session_state.setdefault('clip-editor-version', 0)
        edited = st.data_editor(
            pd.DataFrame(rows), hide_index=True, key=f'clips-{pid}-{version}',
            column_order=[k for k in rows[0] if k != 'id'],
            disabled=[k for k in rows[0] if k not in ('exclude', 'reason')],
            column_config={'exclude': st.column_config.CheckboxColumn(
                'Exclude', help='Reversible. Excluded clips are skipped by analysis and '
                                'hidden from the contact sheet; footage is never deleted.')})
        changed = 0
        for before, after in zip(rows, edited.to_dict('records')):
            if (before['exclude'], before['reason']) != (bool(after['exclude']),
                                                         after['reason'] or ''):
                changed += store.set_excluded(before['id'], after['exclude'], after['reason'])
        if changed:
            st.session_state['clip-editor-version'] = version + 1
            st.rerun()
        st.caption('Codec, size and duration appear after analysis. Errors are per clip '
                   'and never stop the rest of the batch.')

# -- queue --------------------------------------------------------------------
with queue_tab:
    @st.fragment(run_every=2)
    def queue_panel():
        counts = store.job_counts(pid)
        cols = st.columns(6)
        for col, state in zip(cols, ['queued', 'running', 'cancel_requested', 'done',
                                     'failed', 'cancelled']):
            col.metric(state.replace('_', ' '), counts.get(state, 0))
        a, b, c = st.columns(3)
        if a.button(f"Analyze pending clips ({project['sample_count']} samples)", type='primary'):
            st.toast(f'Queued {store.enqueue_analysis(pid)} clip(s).')
        if b.button('Cancel queued and running'):
            st.toast(f'Cancelling {store.cancel_jobs(pid)} job(s).')
        if c.button('Resume cancelled / retry failed'):
            st.toast(f'Re-queued {store.resume_jobs(pid)} job(s).')
        jobs = store.jobs(pid, 100)
        total = sum(counts.values())
        if total:
            finished = sum(counts.get(s, 0) for s in ('done', 'failed', 'cancelled'))
            st.progress(finished / total, f'{finished} of {total} jobs finished')
        if jobs:
            st.dataframe(pd.DataFrame(jobs)[['id', 'source_label', 'rel_path', 'state',
                                             'progress', 'attempts', 'error', 'started_at',
                                             'finished_at']], hide_index=True,
                         column_config={'progress': st.column_config.ProgressColumn(
                             'progress', min_value=0, max_value=1)})
        st.caption('Jobs persist in the project database. Closing the app returns running '
                   'jobs to the queue; cancellation takes effect between decoded frames.')

    queue_panel()

# -- contact sheet --------------------------------------------------------------
DECISION_ICONS = {'Review': '○ Review', 'Keep': '✓ Keep', 'Reject': '✕ Reject'}


def save_decision(candidate_id, key):
    value = st.session_state.get(key)
    if value in DECISIONS:
        store.save_selection(candidate_id, decision=value)


with sheet_tab:
    analyzed = [c for c in store.clips(pid, include_excluded=False) if c['candidate_count']]
    if not analyzed:
        st.info('No analyzed clips yet. Queue analysis from the Queue tab.')
    else:
        top = st.columns([3, 2, 1])
        clip = top[0].selectbox('Clip', analyzed,
                                format_func=lambda c: f"{c['source_label']} / {c['rel_path']}")
        show = top[1].pills('Show', ['All'] + list(DECISIONS), default='All', key='sheet-filter')
        per_row = top[2].number_input('Per row', 2, 8, 4)
        candidates = store.candidates(clip['id'])
        visible = [k for k in candidates if show in (None, 'All') or k['decision'] == show]
        tally = {d: sum(k['decision'] == d for k in candidates) for d in DECISIONS}
        st.caption(f"{len(candidates)} candidates · " +
                   ' · '.join(f'{n} {d}' for d, n in tally.items()) +
                   ' · decisions save immediately')
        st.warning('Metrics are uncalibrated screening signals. Edge detail can be noise; '
                   'darkness can be intentional. White balance, composition, motion and '
                   'subject relevance require visual review. Log/HDR footage needs a verified '
                   'viewing transform before delivery.')
        for start in range(0, len(visible), per_row):
            for col, k in zip(st.columns(per_row), visible[start:start + per_row]):
                with col.container(border=True):
                    st.image(k['preview'], width='stretch')
                    m = k['metrics']
                    st.caption(f"{k['seconds']:.3f}s · edge {m['edge_variance']} · "
                               f"dark {m['dark_fraction']:.2f} · bright {m['bright_fraction']:.2f}")
                    key = f"decision-{k['id']}"
                    st.session_state.setdefault(key, k['decision'])
                    st.segmented_control('Decision', list(DECISIONS), key=key,
                                         format_func=DECISION_ICONS.get,
                                         label_visibility='collapsed',
                                         on_change=save_decision, args=(k['id'], key))
                    if st.button('Details', key=f"focus-{k['id']}"):
                        st.session_state['focus'] = k['id']
        focus = next((k for k in candidates if k['id'] == st.session_state.get('focus')), None)
        if focus:
            st.divider()
            left, right = st.columns([2, 1])
            left.image(focus['preview'], width='stretch',
                       caption=f"Clip-relative {focus['seconds']:.6f}s (requested seek time; "
                               'not a verified frame number) · preview reduced to 960px')
            with right, st.form(f"detail-{focus['id']}"):
                def choice(label, items, current):
                    return st.selectbox(label, items,
                                        index=items.index(current) if current in items else 0)
                decision = choice('Decision', list(DECISIONS), focus['decision'])
                rank = choice('Creative rank (human)', ['A', 'B', 'C'], focus['rank'] or 'B')
                period = choice('Lighting / time', ['Day', 'Dusk', 'Night', 'Interior', 'Unknown'],
                                focus['period'] or 'Unknown')
                tag = choice('Subject (human)', project['tags'], focus['tag'] or 'Other')
                notes = st.text_area('Focus, motion, color, composition and subject notes',
                                     focus['notes'] or '')
                color = st.checkbox('Display color verified (no unresolved log/HDR conversion)',
                                    value=focus['color_verified'])
                if st.form_submit_button('Save review'):
                    store.save_selection(focus['id'], decision=decision, rank=rank,
                                         period=period, tag=tag, notes=notes,
                                         color_verified=color)
                    st.session_state[f"decision-{focus['id']}"] = decision
                    st.success('Saved')

# -- selections and export ------------------------------------------------------
with export_tab:
    kept = store.kept(pid)
    output = folder_field('Existing export parent folder', f'output-{pid}',
                          'Choose the folder to export stills into',
                          '/Volumes/BackupDrive/Project')
    st.caption(f"Exports go to `<parent>/{project['export_prefix']}_Still_Selects/` as "
               'full-resolution PNG + provenance JSON + CSV index. Existing files are never '
               'overwritten. FFmpeg default decode; no creative grade or color transform.')
    if not kept:
        st.info('No kept frames yet. Mark frames Keep on the contact sheet.')
    else:
        table = pd.DataFrame(kept)[['source_label', 'rel_path', 'seconds', 'rank', 'tag',
                                    'period', 'color_verified', 'exported_file']]
        picked = st.dataframe(table, hide_index=True, on_select='rerun',
                              selection_mode='single-row', key=f'kept-{pid}')
        rows = picked.selection.rows
        if rows and st.button('Export selected kept frame', type='primary'):
            try:
                st.success(str(store.export_candidate(kept[rows[0]]['id'], output)))
            except FileExistsError:
                st.info('This named selection already exists. It was not overwritten.')
            except Exception as e:
                st.error(str(e))
        st.caption('Batch export, contact-sheet PDFs and social crops are later milestones.')
