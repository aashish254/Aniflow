"""
Panel Classifier - Classifies each panel as 'action', 'standard', or 'epic'
based on aspect ratio, narration keywords, and dialogue density.

Panel types and target durations:
  action   -> 1.0-2.0s  (fight scenes, rapid cuts, explosion panels)
  standard -> 2.5-3.5s  (dialogue, exposition, normal moments)
  epic     -> 4.0-6.0s  (reveals, climax, spreads, boss appearances)
"""

import re

# Keywords that suggest fast action panels
ACTION_KEYWORDS = {
    'slash', 'strike', 'attack', 'punch', 'kick', 'dodge', 'blast', 'explosion',
    'explodes', 'dash', 'charge', 'clash', 'impact', 'smash', 'cut', 'stab',
    'pierce', 'block', 'parry', 'evade', 'crack', 'shatter', 'burst', 'collide',
    'detonates', 'unleash', 'lunges', 'rips', 'tears', 'shreds', 'burns',
    'electrifies', 'freezes', 'crushes', 'drives', 'hurls', 'launches',
    'slices', 'decimating', 'obliterate', 'annihilate', 'massacre', 'rupture',
    'shock', 'shatters', 'surges', 'fires', 'barrages', 'blasts', 'detonates',
    'cracks', 'crashes', 'collides', 'erupts', 'flashes', 'shoots', 'leaps',
}

# Keywords that suggest epic/climax panels
EPIC_KEYWORDS = {
    'reveals', 'awakens', 'awakening', 'transformation', 'transforms',
    'massive', 'colossal', 'enormous', 'gigantic', 'final', 'ultimate',
    'legendary', 'mythical', 'divine', 'godlike', 'breakthrough', 'ascends',
    'ascension', 'evolves', 'evolution', 'summoned', 'appears', 'emerges',
    'manifests', 'reborn', 'rebirth', 'awakened', 'unlocks', 'unlocked',
    'catastrophic', 'cataclysmic', 'overwhelming', 'cleared', 'victory',
    'triumph', 'defeated', 'destroyed', 'speechless', 'awe', 'impossible',
    'unbelievable', 'legendary', 'mythical', 'transcendent', 'unfathomable',
    'system', 'grade', 'rank', 'level', 'boss', 'dragon', 'god', 'demon',
    'monster', 'beast', 'creature', 'ancient', 'primordial', 'awakening',
}

# Target duration ranges per type (min_s, default_s, max_s)
DURATION_MAP = {
    'action':   (1.0, 1.5, 2.0),
    'standard': (2.5, 3.0, 3.5),
    'epic':     (4.0, 5.0, 6.0),
}


def classify_panel(panel: dict) -> str:
    """
    Classify a single panel as 'action', 'standard', or 'epic'.
    Priority: aspect-ratio > keyword scoring > fallback to standard.
    """
    # 1. Aspect ratio is the most reliable signal
    w = panel.get('width', 0)
    h = panel.get('height', 0)
    if w > 0 and h > 0:
        ratio = h / w
        if ratio < 0.55:        # very wide/landscape spread
            return 'epic'
        if ratio > 2.8:         # very tall/narrow action strip
            return 'action'

    # 2. Keyword scoring — ONLY from SOURCE text (OCR-extracted), never from AI narration
    #    Reading 'narration' or 'scene_narration' creates a feedback loop where
    #    AI words like 'emerges', 'summoned', 'appears' make everything 'epic'
    text_blob = ' '.join([
        panel.get('text', ''),
        panel.get('dialogue', ''),
    ]).lower()
    words = set(re.findall(r'\b\w+\b', text_blob))

    action_score = len(words & ACTION_KEYWORDS)
    epic_score   = len(words & EPIC_KEYWORDS)

    # Epic beats action (bigger visual moment = stay longer)
    if epic_score >= 2:
        return 'epic'
    if action_score >= 3:
        return 'action'
    if epic_score == 1 and action_score <= 1:
        return 'epic'
    if action_score == 2 and epic_score == 0:
        return 'action'

    # 3. Very short/no dialogue in a non-wide panel → likely a silent action beat
    dialogue = (panel.get('text') or panel.get('dialogue', '')).strip()
    if not dialogue or len(dialogue) < 8:
        narration_words = set(re.findall(r'\b\w+\b', panel.get('narration', '').lower()))
        if narration_words & ACTION_KEYWORDS:
            return 'action'

    return 'standard'


def get_target_duration(panel_type: str, audio_duration: float = 0.0) -> float:
    """
    Calculate display duration for a panel.
    - Audio is never cut short (audio_duration is a floor).
    - Epic panels get a minimum of 4s even if audio is shorter.
    - Action panels are capped to maintain pace, but audio still plays fully.
    """
    min_d, default_d, _ = DURATION_MAP.get(panel_type, DURATION_MAP['standard'])
    audio = max(0.0, audio_duration or 0.0)

    if panel_type == 'action':
        # Keep pace fast but never cut audio
        return max(min_d, audio + 0.15)

    elif panel_type == 'epic':
        # Always show for at least 4s — dramatic pause matters
        return max(min_d, audio + 0.6)

    else:  # standard
        return max(min_d, audio + 0.3)


def classify_all_panels(panels: list, force: bool = False) -> list:
    """
    Classify all panels and add/update panel_type + target_duration.
    Set force=True to re-classify already-classified panels.
    Returns the panels list (modified in place).
    """
    for panel in panels:
        if force or 'panel_type' not in panel:
            panel['panel_type'] = classify_panel(panel)
        audio_dur = float(panel.get('audio_duration') or 0.0)
        if force or 'target_duration' not in panel:
            panel['target_duration'] = get_target_duration(
                panel['panel_type'], audio_dur
            )
    return panels


def get_classification_summary(panels: list) -> dict:
    """Return a summary count of panel type classifications."""
    counts = {'action': 0, 'standard': 0, 'epic': 0}
    for p in panels:
        pt = p.get('panel_type', 'standard')
        counts[pt] = counts.get(pt, 0) + 1
    return counts
