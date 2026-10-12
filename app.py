"""FrameFoundry — local review workbench. Run: streamlit run app.py"""
import hashlib
import json
from pathlib import Path
import streamlit as st
from core import (TAGS, analyze, atomic_json, dependencies, export_still, inventory)

st.set_page_config(page_title='FrameFoundry', page_icon='🎞️', layout='wide')
st.title('FrameFoundry')
st.caption('Your footage. The frames worth keeping. • Local review workbench')
missing = dependencies()
if missing:
    st.error('Install FFmpeg before continuing. Missing: ' + ', '.join(missing))
    st.stop()

with st.sidebar:
    st.header('Project')
    project = st.text_input('Project name / export prefix', 'MyProject')
    preset = st.selectbox('Subject preset', ['General', 'Seasonal event'])
    source = st.text_input('Source folder', placeholder='/Volumes/YourDrive/Footage')
    output = st.text_input('Existing export parent', placeholder='/Volumes/BackupDrive/Project')
    tags = st.text_input('Subject tags, comma separated',
        ', '.join(TAGS) if preset == 'Seasonal event' else
        'Portrait, Landscape, Detail, Action, BehindTheScenes, Ambiance, Other', key=preset)
    count = st.slider('Samples per clip', 4, 48, 12, 4)
    st.caption('Sparse sampling can miss brief moments. Manual fine selection and scene detection are planned.')

key = hashlib.sha256((str(Path(source).expanduser()) + '|' + project).encode()).hexdigest()[:16]
cache = Path.home() / '.framefoundry' / key
state_file = cache / 'reviews.json'
if st.session_state.get('project_key') != key:
    st.session_state.pop('clips', None)
    st.session_state.pop('analysis', None)
    st.session_state['project_key'] = key
    st.session_state['reviews'] = json.loads(state_file.read_text()) if state_file.exists() else {}
reviews = st.session_state['reviews']

if st.button('Scan source folder', type='primary'):
    try:
        st.session_state['clips'] = inventory(source)
    except Exception as e:
        st.error(str(e))
clips = st.session_state.get('clips', [])
st.subheader('01 / Source inventory')
st.write(f'{len(clips)} video files • Originals remain untouched')
if not clips:
    st.info('Enter a source folder on this computer, then scan it. No media is uploaded.')
    st.stop()
clip = st.selectbox('Clip', clips, format_func=lambda c: c['name'])
ex_key = 'exclude-' + clip['id']
excluded = st.checkbox('Exclude this clip (talking head / irrelevant)',
                       value=reviews.get(ex_key, False), key=ex_key)
reviews[ex_key] = excluded
atomic_json(state_file, reviews)
if excluded:
    st.info('Excluded from candidate review. You can re-enable it here.')
    st.stop()

if st.button('Analyze selected clip'):
    try:
        with st.spinner('Decoding preview samples…'):
            st.session_state['analysis'] = analyze(clip, cache, count)
    except Exception as e:
        st.error(str(e))
analysis = st.session_state.get('analysis')
if not analysis or analysis['clip']['id'] != clip['id']:
    st.stop()
st.subheader('02 / Candidate review')
st.json(analysis['metadata'], expanded=False)
st.warning('Metrics are uncalibrated screening signals. Edge detail can be noise; darkness can be intentional. White balance, composition, motion quality and subject relevance require visual review in this version. Log/HDR footage needs a verified viewing transform before delivery.')
order = st.selectbox('Browse order', ['Time', 'Edge detail (heuristic)'])
frames = sorted(analysis['frames'], key=lambda f: f['seconds'] if order == 'Time' else -f['edge_variance'])
frame = st.selectbox('Candidate', frames, format_func=lambda f: f"{f['seconds']:.3f}s · edge {f['edge_variance']}")
left, right = st.columns([2, 1])
with left:
    st.image(frame['preview'], use_container_width=True)
    st.caption(f"Clip-relative timestamp {frame['seconds']:.6f}s · preview reduced to 960px")
with right:
    st.write({k: frame[k] for k in ['edge_variance', 'dark_fraction', 'bright_fraction']})
    previous = reviews.get(frame['id'], {})
    with st.form(frame['id']):
        def choice(label, items, field, default):
            value = previous.get(field, default)
            return st.selectbox(label, items, index=items.index(value) if value in items else 0)
        decision = choice('Decision', ['Review', 'Keep', 'Reject'], 'decision', 'Review')
        rank = choice('Creative rank', ['A', 'B', 'C'], 'rank', 'B')
        period = choice('Lighting / time', ['Day', 'Dusk', 'Night', 'Interior', 'Unknown'], 'period', 'Unknown')
        subjects = list(dict.fromkeys([x.strip() for x in tags.split(',') if x.strip()] + ['Other']))
        tag = choice('Subject', subjects, 'tag', 'Other')
        notes = st.text_area('Focus, motion, color, composition and subject notes', previous.get('notes', ''))
        color = st.checkbox('Display color verified (no unresolved log/HDR conversion)', value=previous.get('color_verified', False))
        if st.form_submit_button('Save review'):
            reviews[frame['id']] = dict(decision=decision, rank=rank, period=period,
                tag=tag, notes=notes, color_verified=color)
            atomic_json(state_file, reviews)
            st.success('Saved')

st.subheader('03 / Export current selection')
st.caption('Full-resolution PNG plus provenance JSON and a cumulative CSV index. Exports use the decoded source frame without a creative grade.')
if st.button('Export this kept frame', type='primary'):
    try:
        target = export_still(analysis, frame, reviews.get(frame['id'], {}), output, project)
        st.success(str(target))
    except FileExistsError:
        st.info('This named selection already exists. It was not overwritten.')
    except Exception as e:
        st.error(str(e))
