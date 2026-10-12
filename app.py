"""FrameFoundry — local review workbench. Run: streamlit run app.py --server.address 127.0.0.1"""
import os

import pandas as pd
import streamlit as st

import picker
import scoring
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


def quick(action, scored, clip_id):
    """Per-clip quick actions. Automatic picks never override a person's decision."""
    picks = [k['id'] for k in scoring.suggestions(scored)]
    rest = [k['id'] for k in scored if k['id'] not in picks]
    if action == 'keep':
        n = store.bulk_decide(picks, 'Keep')
    elif action == 'keep-reject':
        n = store.bulk_decide(picks, 'Keep') + store.bulk_decide(rest, 'Reject')
    elif action == 'keep-one':
        n = store.bulk_decide(picks[:1], 'Keep')
    elif action == 'undo':
        n = store.undo_auto(clip_id=clip_id)
    elif action == 'verify':
        n = store.mark_color_verified([k['id'] for k in scored if k['decision'] == 'Keep'])
    st.session_state['sheet-message'] = f'{n} frame(s) updated.'


def autopick_all(top_n, reject_rest):
    touched = sum(bool(n) for n in scoring.autopick(store, pid, top_n, reject_rest).values())
    st.session_state['sheet-message'] = f'Auto-picked top {top_n} in {touched} clip(s).'


def step_clip(order, offset):
    """Move to the previous/next clip in list order (no wrap-around)."""
    current = st.session_state.get('sheet-clip')
    i = order.index(current) if current in order else 0
    st.session_state['sheet-clip'] = order[max(0, min(len(order) - 1, i + offset))]


def next_to_review(order, progress):
    """Jump forward to the next clip that still has frames awaiting a decision."""
    current = st.session_state.get('sheet-clip')
    after = order[order.index(current) + 1:] if current in order else order
    target = next((c for c in after if progress.get(c, {}).get('Review', 0)), None)
    if target:
        st.session_state['sheet-clip'] = target
    else:
        st.session_state['sheet-message'] = 'No later clip has frames left to review.'


def clip_nav(order, progress, where):
    i = order.index(st.session_state['sheet-clip'])
    nav = st.columns(3)
    nav[0].button('‹ Back', key=f'back-{where}', on_click=step_clip, args=(order, -1),
                  disabled=i == 0, width='stretch')
    nav[1].button('Next ›', key=f'next-{where}', on_click=step_clip, args=(order, 1),
                  disabled=i == len(order) - 1, width='stretch')
    nav[2].button('To review ›', key=f'review-{where}', on_click=next_to_review,
                  args=(order, progress), width='stretch',
                  help='Skip ahead to the next clip that still has frames marked Review')
    st.caption(f'Clip {i + 1} of {len(order)}')


with sheet_tab:
    analyzed = [c for c in store.clips(pid, include_excluded=False) if c['candidate_count']]
    counts = store.job_counts(pid)
    pending = counts.get('queued', 0) + counts.get('running', 0)
    if pending:
        note, refresh = st.columns([5, 1], vertical_alignment='center')
        note.info(f"{len(analyzed)} clip(s) ready · {counts.get('running', 0)} analyzing · "
                  f"{counts.get('queued', 0)} queued. Each clip appears here as soon as its own "
                  'analysis finishes; you do not need to wait for the whole queue.')
        refresh.button('Refresh', key='sheet-refresh', width='stretch')
    if not analyzed:
        if not pending:
            st.info('No analyzed clips yet. Queue analysis from the Queue tab.')
    else:
        progress = store.clip_progress(pid)
        by_id = {c['id']: c for c in analyzed}
        order = list(by_id)

        def clip_label(cid):
            c, p = by_id[cid], progress.get(cid, {})
            mark = '●' if p.get('Keep', 0) + p.get('Reject', 0) else '○'
            return (f"{mark} {c['source_label']} / {c['rel_path']} — {p.get('Keep', 0)} kept · "
                    f"{p.get('Review', 0)} to review")

        with st.expander('Automate all clips'):
            a, b, c = st.columns([1, 2, 2], vertical_alignment='bottom')
            top_all = a.number_input('Top N', 1, 10, 5, key='auto-top')
            reject_rest = b.checkbox('Also reject the rest', key='auto-reject')
            c.button(f'Auto-pick top {top_all} in untouched clips', type='primary',
                     on_click=autopick_all, args=(top_all, reject_rest), width='stretch')
            st.caption('Applies only to clips where nobody has made a decision yet. Picks are '
                       'labelled automatic, can be undone per clip, and never change your own '
                       'decisions. The score is a technical heuristic (relative sharpness and '
                       'clipping), not a judgment of composition or subject.')

        if st.session_state.get('sheet-clip') not in by_id:
            st.session_state['sheet-clip'] = order[0]
        pick, nav_area = st.columns([3, 2], vertical_alignment='bottom')
        pick.selectbox('Clip', order, key='sheet-clip', format_func=clip_label)
        with nav_area:
            clip_nav(order, progress, 'top')
        top = st.columns([2, 2, 1], vertical_alignment='bottom')
        show = top[0].pills('Show', ['All'] + list(DECISIONS), default='All', key='sheet-filter')
        sort = top[1].pills('Order', ['Score', 'Time'], default='Score', key='sheet-order')
        per_row = top[2].number_input('Per row', 2, 8, 4)
        clip = by_id[st.session_state['sheet-clip']]

        top_n = st.session_state.get('auto-top', 5)
        scored = scoring.score_candidates(store.candidates(clip['id']), top_n)
        picks = scoring.suggestions(scored)
        with st.container(border=True):
            st.markdown(f'**Suggested top {len(picks)}** · technical screening — verify visually'
                        + ('' if len(picks) == top_n else
                           f' · only {len(picks)} distinct frame(s) after removing near-duplicates'))
            q = st.columns(5)
            q[0].button(f'✓ Keep top {len(picks)}', on_click=quick,
                        args=('keep', scored, clip['id']), width='stretch', type='primary')
            q[1].button('✓ Keep top, ✕ reject rest', on_click=quick,
                        args=('keep-reject', scored, clip['id']), width='stretch')
            q[2].button('✓ Keep #1 only', on_click=quick,
                        args=('keep-one', scored, clip['id']), width='stretch')
            q[3].button('↺ Undo auto-picks', on_click=quick,
                        args=('undo', scored, clip['id']), width='stretch')
            q[4].button('Color checked for kept', on_click=quick,
                        args=('verify', scored, clip['id']), width='stretch',
                        help='Record that you have checked display color for every kept frame '
                             'in this clip (required before export).')
        message = st.session_state.pop('sheet-message', None)
        if message:
            st.toast(message)

        visible = [k for k in scored if show in (None, 'All') or k['decision'] == show]
        if sort != 'Time':
            visible.sort(key=lambda k: (k['suggested_rank'] or 99, -k['score'], k['seconds']))
        tally = {d: sum(k['decision'] == d for k in scored) for d in DECISIONS}
        st.caption(f"{len(scored)} candidates · " +
                   ' · '.join(f'{n} {d}' for d, n in tally.items()) +
                   ' · decisions save immediately · "auto" marks automatic picks')
        st.warning('Scores and metrics are uncalibrated screening signals. Edge detail can be '
                   'noise; darkness can be intentional. White balance, composition, motion and '
                   'subject relevance require visual review. Log/HDR footage needs a verified '
                   'viewing transform before delivery.')
        for start in range(0, len(visible), per_row):
            for col, k in zip(st.columns(per_row), visible[start:start + per_row]):
                with col.container(border=True):
                    st.image(k['preview'], width='stretch')
                    badge = f"★ #{k['suggested_rank']} · " if k['suggested_rank'] else ''
                    auto = ' · auto' if k['decided_by'] == 'auto' and k['decision'] != 'Review' else ''
                    st.markdown(f"**{badge}score {k['score']}** · {k['seconds']:.2f}s{auto}")
                    if k['reasons']:
                        st.caption(' · '.join(k['reasons']))
                    key = f"decision-{k['id']}"
                    st.session_state[key] = k['decision']  # always reflect the database
                    st.segmented_control('Decision', list(DECISIONS), key=key,
                                         format_func=DECISION_ICONS.get,
                                         label_visibility='collapsed',
                                         on_change=save_decision, args=(k['id'], key))
                    if st.button('Details', key=f"focus-{k['id']}"):
                        st.session_state['focus'] = k['id']
        st.divider()
        clip_nav(order, progress, 'bottom')
        focus = next((k for k in scored if k['id'] == st.session_state.get('focus')), None)
        if focus:
            st.divider()
            left, right = st.columns([2, 1])
            left.image(focus['preview'], width='stretch',
                       caption=f"Clip-relative {focus['seconds']:.6f}s (requested seek time; "
                               'not a verified frame number) · preview reduced to 960px')
            m = focus['metrics']
            left.caption(f"Technical score {focus['score']} · edge {m['edge_variance']} · "
                         f"near-black {m['dark_fraction']:.1%} · clipped {m['bright_fraction']:.1%}")
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
                    st.success('Saved')


# -- selections and export ------------------------------------------------------
def export_all(rows, output):
    done, skipped, errors = 0, 0, []
    for row in rows:
        try:
            store.export_candidate(row['id'], output)
            done += 1
        except FileExistsError:
            skipped += 1
        except Exception as e:
            errors.append(f"{row['rel_path']} @ {row['seconds']:.2f}s: {e}")
    st.session_state['export-result'] = (done, skipped, errors)


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
        ready = [r for r in kept if r['color_verified'] and not r['exported_file']]
        unverified = sum(not r['color_verified'] for r in kept)
        a, b = st.columns([2, 3], vertical_alignment='center')
        a.button(f'Export all {len(ready)} ready frame(s)', type='primary',
                 disabled=not ready or not output, on_click=export_all, args=(ready, output),
                 width='stretch')
        b.caption(f'{len(kept)} kept · {len(ready)} ready to export · {unverified} awaiting '
                  'color check (use "Color checked for kept" on the contact sheet)')
        result = st.session_state.pop('export-result', None)
        if result:
            done, skipped, errors = result
            st.success(f'Exported {done} frame(s); {skipped} already existed and were not '
                       'overwritten.')
            for e in errors:
                st.error(e)
        table = pd.DataFrame(kept)[['source_label', 'rel_path', 'seconds', 'decided_by', 'rank',
                                    'tag', 'period', 'color_verified', 'exported_file']]
        picked = st.dataframe(table, hide_index=True, on_select='rerun',
                              selection_mode='single-row', key=f'kept-{pid}')
        rows = picked.selection.rows
        if rows and st.button('Export selected kept frame'):
            try:
                st.success(str(store.export_candidate(kept[rows[0]]['id'], output)))
            except FileExistsError:
                st.info('This named selection already exists. It was not overwritten.')
            except Exception as e:
                st.error(str(e))
        st.caption('Contact-sheet PDFs and social crops are later milestones.')
