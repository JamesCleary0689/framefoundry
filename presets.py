"""Optional subject-tag presets. Projects copy a preset's tags and may edit them freely;
nothing in the data model or exports depends on a particular preset."""

PRESETS = {
    'General': ['Portrait', 'Landscape', 'Detail', 'Action', 'BehindTheScenes',
                'Ambiance', 'Other'],
    'Seasonal event': ['Market', 'Setup', 'Screens', 'Projectors', 'Lasers',
                       'BounceHouse', 'PumpkinPatch', 'Animatronics',
                       'CostumedCharacters', 'Performance', 'Portrait', 'Aerial',
                       'Ambiance', 'Other'],
}


def parse_tags(text):
    """Comma-separated tags, de-duplicated in order; 'Other' is always available."""
    tags = [t.strip() for t in str(text).split(',') if t.strip()]
    return list(dict.fromkeys(tags + ['Other']))
