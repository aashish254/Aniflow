"""
Professional Scene Narrator — Decoupled Pipeline v4.0

Architecture (DECOUPLED mode — default):
  Phase 1 (Vision Parser):  qwen2.5vl:3b extracts visual beats from panel images
  Phase 2 (Text Storyteller): qwen2.5:14b writes cinematic narration from text-only input

  The text model NEVER sees images. 100% of its parameters focus on creative writing.
  This eliminates "in this panel" artifacts and produces viral YouTube-quality prose.

Architecture (LEGACY mode — fallback):
  Single vision model sees images + writes narration directly (old approach).

Flow:
  classify panels → group into scenes → Phase 1: extract visual beats
  → Phase 2: text model writes narration with few-shot cinematic prompt
  → forbidden guard → save → return
"""

import base64
import io
import json
import os
import re
import requests
from PIL import Image
import config


def _fast_json(payload: dict) -> dict:
    """⚡ SPEED MODE: keep the Ollama model loaded between panels — avoids a
    10-30s model reload whenever Ollama would otherwise unload it. No effect
    on the model output; classic mode sends the payload unchanged."""
    if getattr(config, 'SPEED_MODE', False) and 'keep_alive' not in payload:
        payload = {**payload, 'keep_alive': '30m'}
    return payload


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

SCENE_SIZE_DEFAULT = 1    # per-panel narration (each panel = its own scene)
PER_PANEL_DURATION = 6.5  # seconds per panel (user wants 6-7s each)
MAX_IMG_SIDE_ALL   = 640  # Option A: all images — compressed tightly
MAX_IMG_SIDE_PAIR  = 800  # Option B fallback: first+last — higher quality

# ── The System Prompt (Phase 2 — Text Storyteller) ────────────────────────────

SYSTEM_PROMPT_DECOUPLED = """You are the scriptwriter for a viral manhwa/anime recap YouTube channel.
Your voice is dry, punchy, and ironic — like a witty friend recapping the story, not a dramatic audiobook narrator.

ABSOLUTE RULES:
1. WRITE 1 SHORT SENTENCE PER PANEL. Maximum 25 words. Be ruthless about cutting words.
2. NEVER describe images. NEVER say 'in this panel', 'we see', 'the image shows'. You're telling a story, not captioning a photo.
3. USE THE DIALOGUE. When characters speak, weave their words into your sentence naturally. Don't ignore it.
4. DRY WIT OVER DRAMA. Humor and irony land harder than melodrama. Match the internet-aware tone of modern manhwa.
5. FLOW from the previous line. Each sentence must connect naturally — sometimes start mid-thought, like a continuation.
6. PRESENT TENSE, THIRD PERSON. Active voice only.
7. NEVER INVENT CHARACTER NAMES. Only use names that appear in the Dialogue or KNOWN CHARACTERS list. If you don't know a character's name, say 'he', 'she', 'the guy', 'our MC', etc. NEVER guess or hallucinate a name."""

# ── Legacy System Prompt (for DECOUPLED_NARRATION=False) ──────────────────────

SYSTEM_PROMPT_LEGACY = """You are a manhwa recap narrator for YouTube. You narrate what happens in each panel as if telling a story to a friend.

YOUR JOB:
1. Look at the panel image and describe what's visually happening (characters, setting, action, mood).
2. If there is dialogue, paraphrase it naturally into the narration. Don't quote it word-for-word, weave it into the story.
3. Use character names from the chapter context.
4. Keep it short but complete — one to three sentences. Say what needs to be said, no more.
5. Write in third person, present tense. Flow naturally from the previous narration.
6. NEVER say "In this panel", "We see", "The image shows", or reference panel numbers.
7. NEVER invent characters or events not shown in the image or dialogue."""

# ── Forbidden phrases that break the 4th wall ────────────────────────────────

FORBIDDEN_PHRASES = [
    'panels [', 'panel [', 'in panels', 'panels capture',
    'in this panel', 'in this scene', 'in this image', 'this panel shows',
    'these panels', 'in the epic', 'in a burst of energy',
    'in a burst of flames', 'we see', 'the viewer',
    'as we can see', 'the image shows', 'the scene shows',
    'the camera', 'camera zooms', 'camera pans', 'camera cuts',
    'narration:', 'scene:', 'panel narration', 'image depicts',
    'the artwork', 'the illustration', 'the drawing',
]


# ═══════════════════════════════════════════════════════════════════════════════
# IMAGE ENCODING
# ═══════════════════════════════════════════════════════════════════════════════

def _encode_image_small(path: str, max_side: int = MAX_IMG_SIDE_ALL) -> str:
    """Encode an image to base64 JPEG, compressed for multi-image API calls."""
    try:
        with Image.open(path) as img:
            img = img.convert('RGB')
            img.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            quality = 68 if max_side <= MAX_IMG_SIDE_ALL else 76
            img.save(buf, format='JPEG', quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[SceneNarrator] Image encode error: {e}")
        return ''


def _select_images_all(scene_panels: list, max_side: int = MAX_IMG_SIDE_ALL) -> list:
    """Option A: Encode ALL panel images in the scene."""
    images = []
    for p in scene_panels:
        img_path = p.get('path', '')
        if img_path and os.path.exists(img_path):
            b64 = _encode_image_small(img_path, max_side)
            if b64:
                images.append(b64)
    return images


def _select_images_fallback(scene_panels: list) -> list:
    """Option B: Only first and last panel images (faster, works on smaller models)."""
    chosen = [scene_panels[0]]
    if len(scene_panels) > 1:
        chosen.append(scene_panels[-1])
    images = []
    for p in chosen:
        img_path = p.get('path', '')
        if img_path and os.path.exists(img_path):
            b64 = _encode_image_small(img_path, MAX_IMG_SIDE_PAIR)
            if b64:
                images.append(b64)
    return images


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: VISION PARSER — Extract Visual Beats
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_visual_beat_for_panel(panel: dict, host: str, vision_model: str,
                                    timeout: int = 180) -> str:
    """Phase 1: Send a single panel image to the vision model for a raw visual beat."""
    img_path = panel.get('path', '')
    if not img_path or not os.path.exists(img_path):
        return ''

    b64 = _encode_image_small(img_path, MAX_IMG_SIDE_ALL)
    if not b64:
        return ''

    prompt = (
        "Describe this manhwa/webtoon panel in 1-2 short sentences.\n"
        "Focus on: what the characters are DOING, their expressions, the setting.\n\n"
        "CRITICAL RULES:\n"
        "- NEVER name characters. Do NOT guess or invent names. Say 'a man', 'a girl', 'the dark-haired guy', etc.\n"
        "- Be factual. Describe actions and setting only.\n"
        "- Do NOT write narration or story. Just describe what you see.\n"
        "- Do NOT say 'The image shows' or 'In this panel'.\n\n"
        "Describe now:"
    )

    try:
        resp = requests.post(
            f"{host}/api/chat",
            json=_fast_json({
                "model": vision_model,
                "stream": False,
                "messages": [
                    {"role": "user", "content": prompt, "images": [b64]},
                ],
                "options": {
                    "temperature": 0.3,
                    "top_p": 0.85,
                    "num_predict": 150,
                },
            }),
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        # Clean preamble
        for prefix in ['here is', "here's", 'sure,', 'the panel', 'visual beat:']:
            if raw.lower().startswith(prefix):
                raw = raw.split(':', 1)[-1].strip() if ':' in raw else raw
        return raw
    except Exception as e:
        print(f"[SceneNarrator] Phase 1 visual beat failed for panel {panel.get('panel_id', '?')}: {e}")
        return ''


def _extract_visual_beats_for_scene(
    scene_panels: list,
    host: str,
    vision_model: str,
    timeout: int = 180,
    cancel_check=None,
) -> str:
    """Phase 1: Extract visual beats for all panels in a scene, return combined text."""
    beats = []
    for p in scene_panels:
        if cancel_check and cancel_check():
            break
        beat = _extract_visual_beat_for_panel(p, host, vision_model, timeout)
        if beat:
            beats.append(beat)
            print(f"[SceneNarrator] Phase 1 panel {p.get('panel_id', '?')}: {beat[:60]}...")
    return ' '.join(beats) if beats else ''


# ═══════════════════════════════════════════════════════════════════════════════
# SCENE GROUPING
# ═══════════════════════════════════════════════════════════════════════════════

def group_into_scenes(panels: list, scene_size: int = SCENE_SIZE_DEFAULT) -> list:
    """
    Group panels into scenes (list of lists).
    With scene_size=1, each panel becomes its own scene.
    """
    if not panels:
        return []

    if scene_size <= 1:
        return [[p] for p in panels]

    scenes, current, action_streak = [], [], 0

    for panel in panels:
        ptype = panel.get('panel_type', 'standard')

        # Epic panel forces a break BEFORE it — it starts fresh
        if ptype == 'epic' and current:
            scenes.append(current)
            current = []
            action_streak = 0

        # End of action burst
        if ptype != 'action' and action_streak >= 3 and current:
            scenes.append(current)
            current = []
            action_streak = 0

        current.append(panel)
        action_streak = (action_streak + 1) if ptype == 'action' else 0

        if len(current) >= scene_size:
            scenes.append(current)
            current = []
            action_streak = 0

    if current:
        scenes.append(current)

    return scenes


# ═══════════════════════════════════════════════════════════════════════════════
# DURATION & WORD BUDGET
# ═══════════════════════════════════════════════════════════════════════════════

def _target_duration_for_scene(scene_panels: list) -> float:
    """Per-panel mode: fixed 6.5s per panel for consistent pacing."""
    return PER_PANEL_DURATION * len(scene_panels)


# ═══════════════════════════════════════════════════════════════════════════════
# QUALITY VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def _check_forbidden(text: str):
    """Returns (is_bad: bool, reason: str)."""
    if not text:
        return True, "empty"
    lower = text.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in lower:
            return True, f"contains forbidden: {phrase!r}"
    if re.search(r'\[\d+[-\u2013]\d+\]', text):
        return True, "contains panel range reference like [9-12]"
    if re.search(r'\bpanel\s+\d+\b', lower):
        return True, "mentions panel number"
    return False, ""


def _check_repetition(new_text: str, story_so_far: list) -> bool:
    """Check if the new narration substantially repeats recent output."""
    if not story_so_far or not new_text:
        return False
    new_lower = new_text.lower().strip()
    new_words = new_lower.split()
    for prev in story_so_far[-3:]:
        prev_lower = prev.lower().strip()
        # Check for exact sentence overlap (>60% of new text matches previous)
        if len(new_lower) > 20 and new_lower in prev_lower:
            return True
        if len(prev_lower) > 20 and prev_lower in new_lower:
            return True
        # Check 6-word sliding window overlap
        prev_words = prev_lower.split()
        for i in range(len(new_words) - 5):
            chunk = ' '.join(new_words[i:i+6])
            if chunk in prev_lower:
                return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# POST-PROCESSING
# ═══════════════════════════════════════════════════════════════════════════════

def _truncate_to_sentences(text: str, max_words: int = 60) -> str:
    """Truncate at nearest sentence boundary if over max_words. No hard cutoff mid-sentence."""
    if not text:
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    # Find the last sentence-ending punctuation within limit
    truncated = ' '.join(words[:max_words])
    for punct in '.!?':
        idx = truncated.rfind(punct)
        if idx > len(truncated) * 0.4:
            return truncated[:idx + 1].strip()
    # If no sentence boundary found, just use the full truncated text + period
    return truncated.rstrip(',;:—') + '.'


def _clean_narration(text: str) -> str:
    """Remove AI boilerplate, meta-commentary, and mechanical panel references."""
    if not text:
        return text

    skip_prefixes = [
        'here is', "here's", 'sure,', 'certainly', 'narration:', 'scene:',
        'this panel', 'in this scene', 'based on', 'panel narration:',
        'the following', 'as requested', 'below is', 'of course',
        'here you go', 'absolutely', 'great,', 'okay,',
    ]
    lines = text.strip().split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(lower.startswith(p) for p in skip_prefixes):
            for sep in [':', '-', '\u2014']:
                if sep in stripped:
                    remainder = stripped.split(sep, 1)[1].strip()
                    if len(remainder) > 10:
                        cleaned.append(remainder)
                    break
            continue
        cleaned.append(stripped)

    result = ' '.join(cleaned)

    # Strip surrounding quotes
    if result.startswith('"') and result.endswith('"'):
        result = result[1:-1]

    # Remove panel references
    result = re.sub(r'Panels?\s*\[\d+[-\u2013]\d+\]\s*:?\s*', '', result)
    result = re.sub(r'\b[Ii]n\s+[Pp]anels?\s*\[\d+[-\u2013]\d+\]', '', result)
    result = re.sub(r'\b[Ii]n\s+this\s+(panel|scene|image)\b', '', result, flags=re.IGNORECASE)

    return result.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# PROMPT ARCHITECTURE
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_panel_dialogue(panel: dict) -> str:
    """Extract clean dialogue for a single panel, stripping labels."""
    dlg = (panel.get('text') or panel.get('dialogue', '')).strip()
    if not dlg or len(dlg) < 4:
        return ''
    # Remove structural labels
    dlg = re.sub(r'^(Speaker\s*\d*|Panel\s*\d+|Inventory)\s*:\s*', '', dlg,
                 flags=re.IGNORECASE).strip()
    # Remove watermarks
    dlg = re.sub(r'ASURASCANS\.COM', '', dlg, flags=re.IGNORECASE).strip()
    # Skip junk
    if re.match(r'^(WWW\.|HTTP|ASURASCANS|\[Sound)', dlg, re.IGNORECASE):
        return ''
    return dlg.strip() if len(dlg) >= 4 else ''


def _build_decoupled_prompt(
    scene_panels: list,
    story_so_far: list,
    visual_beats: str,
    chapter_context: str = None,
    cast_characters: list = None,
) -> tuple:
    """
    Build the DECOUPLED narration prompt (Phase 2 — text-only).
    Returns (system_prompt, user_content, soft_max_words).

    NO images are referenced. The visual context comes from the Phase 1
    visual beats text, which was extracted by the vision model.
    """
    soft_max = 25

    # ── Cast characters ──────────────────────────────────────────────────────
    cast_section = ""
    if cast_characters:
        clean_names = [n for n in cast_characters if n and isinstance(n, str)]
        if clean_names:
            cast_section = f"\nKNOWN CHARACTERS: {', '.join(clean_names)}\n"

    # ── Global context (full chapter) ────────────────────────────────────────
    global_ctx = ""
    if chapter_context:
        excerpt = chapter_context[:4000].rstrip()
        if len(chapter_context) > 4000:
            excerpt += '\n[... chapter continues ...]'
        global_ctx = (
            "FULL CHAPTER DIALOGUE (for character/story awareness ONLY):\n"
            f"{excerpt}\n"
        )

    # ── Story so far (last 3 narrations for flow) ────────────────────────────
    if story_so_far:
        recent = ' '.join(story_so_far[-3:])
        continuity = f"\nSTORY SO FAR (flow naturally from this):\n{recent}\n"
    else:
        continuity = "\nThis is panel 1. Hook the viewer immediately.\n"

    # ── This panel's dialogue ────────────────────────────────────────────────
    all_dialogue = []
    for p in scene_panels:
        dlg = _extract_panel_dialogue(p)
        if dlg:
            all_dialogue.append(dlg)

    dialogue_text = ' / '.join(all_dialogue) if all_dialogue else '(none)'

    # ── Build the user content with few-shot examples ────────────────────────
    user_content = (
        f"{global_ctx}"
        f"{cast_section}"
        "\n=========================================\n"
        "FEW-SHOT EXAMPLES (MATCH THIS STYLE EXACTLY)\n"
        "=========================================\n"
        "Visual Beat: Guy gathering food in forest with a small girl.\n"
        "Dialogue: (none)\n"
        "Output: That leaves Ian gathering food with his little sister instead of enjoying the usual isekai starter pack.\n\n"
        "Visual Beat: Two kids standing on a hill, one pointing at the other angrily.\n"
        "Dialogue: 'gross crow'\n"
        "Output: The village kids also call him a gross crow because black hair is apparently a crime here.\n\n"
        "Visual Beat: Close-up of a character looking at a status window that says NOBODY.\n"
        "Dialogue: 'NOBODY'\n"
        "Output: Even his long-awaited status window takes one look at him and declares him a NOBODY.\n\n"
        "Visual Beat: Man sitting at a desk looking at a phone/laptop.\n"
        "Dialogue: 'computer engineering'\n"
        "Output: Ritsuki Kadoyama had just escaped his trash life by entering computer engineering.\n\n"
        "Visual Beat: Train hitting something at night.\n"
        "Dialogue: (none)\n"
        "Output: until Train-kun canceled university permanently.\n\n"
        "Visual Beat: Man waking up confused in a fantasy setting.\n"
        "Dialogue: 'I BEAT IT!!!'\n"
        "Output: He clears the 100th floor after ten years — then the game turns real.\n\n"
        "=========================================\n"
        "YOUR TURN\n"
        "=========================================\n"
        f"{continuity}\n"
        f"Visual Beat: {visual_beats if visual_beats else '(no image description available)'}\n"
        f"Dialogue: {dialogue_text}\n\n"
        "Output (1 sentence, max 25 words, match the example style above):"
    )

    return SYSTEM_PROMPT_DECOUPLED, user_content, soft_max


def _build_legacy_prompt(
    scene_panels: list,
    story_so_far: list,
    chapter_context: str = None,
    cast_characters: list = None,
    n_images_sent: int = None,
) -> tuple:
    """
    Build the LEGACY narration prompt (sends images directly to model).
    Returns (system_prompt, user_content, soft_max_words).
    """
    soft_max = 60

    if chapter_context:
        excerpt = chapter_context[:4000].rstrip()
        if len(chapter_context) > 4000:
            excerpt += '\n[... chapter continues ...]'
        global_ctx = (
            "FULL CHAPTER DIALOGUE (read this to know all characters, their names, "
            "and the complete story arc):\n"
            f"{excerpt}\n"
        )
    else:
        global_ctx = ""

    if story_so_far:
        recent = ' '.join(story_so_far[-3:])
        continuity = f"\nPREVIOUS NARRATION (continue flowing from this):\n{recent}\n"
    else:
        continuity = "\nThis is the first panel of the chapter. Set the scene.\n"

    all_dialogue = []
    for p in scene_panels:
        dlg = _extract_panel_dialogue(p)
        if dlg:
            all_dialogue.append(dlg)

    if all_dialogue:
        dialogue_block = (
            "\nTHIS PANEL'S DIALOGUE (paraphrase this into your narration naturally):\n"
            + '\n'.join(f'  "{d}"' for d in all_dialogue) + "\n"
        )
    else:
        dialogue_block = (
            "\nTHIS PANEL HAS NO DIALOGUE. Describe what you see in the image — "
            "the setting, characters, action, and mood.\n"
        )

    user_content = (
        f"{global_ctx}"
        f"{continuity}"
        f"{dialogue_block}\n"
        "Now look at the attached panel image and write 1-3 sentences narrating "
        "what happens. Describe the visual scene briefly, then weave in the dialogue. "
        "Keep it natural and flowing, like telling a story.\n"
    )

    return SYSTEM_PROMPT_LEGACY, user_content, soft_max


# ═══════════════════════════════════════════════════════════════════════════════
# SCENE NARRATION (Single Scene)
# ═══════════════════════════════════════════════════════════════════════════════

def narrate_scene(
    scene_panels: list,
    host: str,
    model: str,
    story_so_far: list,
    cast_characters: list = None,
    timeout: int = 180,
    cancel_check=None,
    chapter_context: str = None,
    vision_model: str = None,
) -> str:
    """
    Generate one flowing narration for a group of panels (one scene).

    DECOUPLED mode (default):
      Phase 1: Extract visual beats from panel images using vision model
      Phase 2: Send ONLY text (visual beats + dialogue + context) to text model
      No images are sent to the text storyteller model.

    LEGACY mode (fallback):
      Send images directly to the model for narration.
    """
    decoupled = getattr(config, 'DECOUPLED_NARRATION', True)
    p_ids = [p['panel_id'] for p in scene_panels]

    if cancel_check and cancel_check():
        return "[cancelled]"

    if decoupled:
        return _narrate_scene_decoupled(
            scene_panels, host, model, story_so_far,
            cast_characters, timeout, cancel_check,
            chapter_context, vision_model,
        )
    else:
        return _narrate_scene_legacy(
            scene_panels, host, model, story_so_far,
            cast_characters, timeout, cancel_check,
            chapter_context,
        )


def _narrate_scene_decoupled(
    scene_panels: list,
    host: str,
    text_model: str,
    story_so_far: list,
    cast_characters: list = None,
    timeout: int = 180,
    cancel_check=None,
    chapter_context: str = None,
    vision_model: str = None,
) -> str:
    """
    DECOUPLED: Phase 1 (vision → visual beats) → Phase 2 (text → narration).
    The text model NEVER sees images.
    """
    p_ids = [p['panel_id'] for p in scene_panels]
    v_model = vision_model or getattr(config, 'VISION_MODEL', 'qwen2.5vl:3b')

    # ── Phase 1: Extract visual beats ────────────────────────────────────────
    print(f"[SceneNarrator] Panels {p_ids[0]}-{p_ids[-1]}: "
          f"Phase 1 → extracting visual beats ({v_model})...")

    visual_beats = _extract_visual_beats_for_scene(
        scene_panels, host, v_model, timeout, cancel_check
    )
    if cancel_check and cancel_check():
        return "[cancelled]"

    if not visual_beats:
        print(f"[SceneNarrator] ⚠️  Phase 1 returned empty visual beats — proceeding anyway")

    # ── Phase 2: Text-only narration ─────────────────────────────────────────
    sys_prompt, user_content, soft_max = _build_decoupled_prompt(
        scene_panels, story_so_far, visual_beats,
        chapter_context, cast_characters,
    )
    num_predict = max(80, int(soft_max * 1.5))

    print(f"[SceneNarrator] Panels {p_ids[0]}-{p_ids[-1]}: "
          f"Phase 2 → writing narration ({text_model}, NO images, budget={soft_max}w)")

    for attempt in range(3):
        if cancel_check and cancel_check():
            return "[cancelled]"
        try:
            # Pure text call — NO images sent to the text model
            resp = requests.post(
                f"{host}/api/chat",
                json=_fast_json({
                    "model": text_model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "options": {
                        "temperature": 0.72 + attempt * 0.06,
                        "top_p": 0.88,
                        "num_predict": num_predict,
                        "repeat_penalty": 1.18 + attempt * 0.04,
                        "top_k": 40,
                    },
                }),
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json()["message"]["content"].strip()
            result = _truncate_to_sentences(_clean_narration(raw), soft_max)

            if not result or result.startswith('['):
                raise ValueError(f"Empty/invalid: {raw[:60]!r}")

            bad, reason = _check_forbidden(result)
            if bad:
                print(f"[SceneNarrator] ⚠️  Attempt {attempt+1} rejected ({reason})")
                continue

            if _check_repetition(result, story_so_far):
                print(f"[SceneNarrator] ⚠️  Attempt {attempt+1} rejected (repetition)")
                continue

            print(f"[SceneNarrator] ✅ Decoupled (attempt {attempt+1}) — {len(result.split())}w")
            return result

        except Exception as e:
            if cancel_check and cancel_check():
                return "[cancelled]"
            print(f"[SceneNarrator] Attempt {attempt+1} failed: {e}")

    return f"[Scene narration failed for panels {p_ids}]"


def _narrate_scene_legacy(
    scene_panels: list,
    host: str,
    model: str,
    story_so_far: list,
    cast_characters: list = None,
    timeout: int = 180,
    cancel_check=None,
    chapter_context: str = None,
) -> str:
    """
    LEGACY: Send images directly to the model for narration.
    Kept for backward compatibility when DECOUPLED_NARRATION = False.
    """
    p_ids = [p['panel_id'] for p in scene_panels]

    # ── Option A: ALL images ─────────────────────────────────────────────────
    images_all = _select_images_all(scene_panels)
    if not images_all:
        return f"[Scene panels {p_ids}: no images found on disk]"

    sys_prompt, user_content, soft_max = _build_legacy_prompt(
        scene_panels, story_so_far, chapter_context,
        cast_characters, n_images_sent=len(images_all)
    )
    num_predict = max(80, int(soft_max * 1.5))

    print(f"[SceneNarrator] Panels {p_ids[0]}-{p_ids[-1]}: "
          f"Legacy A ({len(images_all)} imgs, budget={soft_max}w)")

    for attempt in range(3):
        if cancel_check and cancel_check():
            return "[cancelled]"
        try:
            resp = requests.post(
                f"{host}/api/chat",
                json=_fast_json({
                    "model": model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content, "images": images_all},
                    ],
                    "options": {
                        "temperature": 0.72 + attempt * 0.06,
                        "top_p": 0.88,
                        "num_predict": num_predict,
                        "repeat_penalty": 1.18 + attempt * 0.04,
                        "top_k": 40,
                    },
                }),
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json()["message"]["content"].strip()
            result = _truncate_to_sentences(_clean_narration(raw), soft_max)

            if not result or result.startswith('['):
                raise ValueError(f"Empty/invalid: {raw[:60]!r}")

            bad, reason = _check_forbidden(result)
            if bad:
                print(f"[SceneNarrator] ⚠️  A{attempt+1} rejected ({reason})")
                continue

            if _check_repetition(result, story_so_far):
                print(f"[SceneNarrator] ⚠️  A{attempt+1} rejected (repetition)")
                continue

            print(f"[SceneNarrator] ✅ Legacy A (attempt {attempt+1}) — {len(result.split())}w")
            return result

        except Exception as e:
            if cancel_check and cancel_check():
                return "[cancelled]"
            print(f"[SceneNarrator] A{attempt+1} failed: {e}")

    print(f"[SceneNarrator] Legacy A exhausted → Legacy B")

    # ── Option B fallback: first + last images ───────────────────────────────
    images_fb = _select_images_fallback(scene_panels)
    if not images_fb:
        return f"[Scene narration failed: no images available]"

    sys_prompt_b, user_content_b, soft_max_b = _build_legacy_prompt(
        scene_panels, story_so_far, chapter_context,
        cast_characters, n_images_sent=len(images_fb)
    )
    num_predict_b = max(80, int(soft_max_b * 1.5))

    for attempt in range(2):
        if cancel_check and cancel_check():
            return "[cancelled]"
        try:
            resp = requests.post(
                f"{host}/api/chat",
                json=_fast_json({
                    "model": model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": sys_prompt_b},
                        {"role": "user", "content": user_content_b, "images": images_fb},
                    ],
                    "options": {
                        "temperature": 0.74 + attempt * 0.06,
                        "top_p": 0.88,
                        "num_predict": num_predict_b,
                        "repeat_penalty": 1.20 + attempt * 0.05,
                        "top_k": 40,
                    },
                }),
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json()["message"]["content"].strip()
            result = _truncate_to_sentences(_clean_narration(raw), soft_max_b)

            bad, reason = _check_forbidden(result)
            if bad:
                print(f"[SceneNarrator] ⚠️  B{attempt+1} rejected ({reason})")
                continue
            if _check_repetition(result, story_so_far):
                print(f"[SceneNarrator] ⚠️  B{attempt+1} rejected (repetition)")
                continue

            print(f"[SceneNarrator] ✅ Legacy B (attempt {attempt+1}) — {len(result.split())}w")
            return result

        except Exception as e:
            if cancel_check and cancel_check():
                return "[cancelled]"
            print(f"[SceneNarrator] B{attempt+1} failed: {e}")

    return f"[Scene narration failed for panels {p_ids}]"


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

def _load_chapter_text(project_dir: str) -> str:
    """Load full chapter text from disk for global context injection."""
    txt_candidates = [
        os.path.join(project_dir, 'chapter_text.txt'),
    ]
    try:
        import config as _cfg
        title_dir = os.path.basename(os.path.dirname(project_dir))
        chap_name = os.path.basename(project_dir)
        txt_candidates.append(
            os.path.join(getattr(_cfg, 'EXTRACTED_TEXT_DIR', ''), title_dir, f"{chap_name}.txt")
        )
    except Exception:
        pass

    for candidate in txt_candidates:
        if os.path.exists(candidate) and os.path.getsize(candidate) > 10:
            with open(candidate, 'r', encoding='utf-8') as f:
                text = f.read().strip()
            print(f"[SceneNarrator] Loaded chapter text ({len(text)} chars) "
                  f"from {os.path.basename(candidate)}")
            return text

    print("[SceneNarrator] ⚠️  No chapter text found — narrating from images + dialogue only")
    return None


def narrate_all_scenes(
    panels: list,
    project_dir: str,
    host: str,
    model: str,
    scene_size: int = SCENE_SIZE_DEFAULT,
    cast_characters: list = None,
    progress_callback=None,
    save_path: str = None,
    cancel_check=None,
    vision_model: str = None,
) -> list:
    """
    Main entry point: narrate all panels using scene-grouped narration.

    Architecture:
      1. Load global context (full chapter text) up-front
      2. Classify panels into types (action/standard/epic) from SOURCE text only
      3. Group into scenes based on type boundaries
      4. Loop through scenes with cancel_check at every boundary
      5. Build System + User prompt matrix per scene
      6. Validate output against forbidden guard + repetition detector
      7. Save after every scene (crash-safe, resumable)
    """
    from pipeline.panel_classifier import classify_all_panels

    # Classify from SOURCE text only — force=True to avoid stale narration feedback loop
    panels = classify_all_panels(panels, force=True)

    scenes = group_into_scenes(panels, scene_size=scene_size)
    total  = len(scenes)

    save_path = save_path or os.path.join(project_dir, 'scene_narrations.json')

    # ── Load existing narrations for crash-safe resume ────────────────────────
    existing_narrations = {}
    if os.path.exists(save_path):
        try:
            with open(save_path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            existing_narrations = {
                e['scene_id']: e['narration']
                for e in saved
                if e.get('narration') and not e['narration'].startswith('[')
            }
            if existing_narrations:
                print(f"[SceneNarrator] Resuming — {len(existing_narrations)} cached scenes")
        except Exception as e:
            print(f"[SceneNarrator] Cache load warning: {e}")

    # ── Load full chapter text (Global Context) ──────────────────────────────
    chapter_context = _load_chapter_text(project_dir)

    # ── Main narration loop ──────────────────────────────────────────────────
    story_so_far  = []     # rolling memory (last 3 narrations)
    scene_results = []

    for scene_idx, scene_panels in enumerate(scenes):
        # ── Cancel gate ──────────────────────────────────────────────────────
        if cancel_check and cancel_check():
            print(f"[SceneNarrator] ✋ Cancelled after {scene_idx}/{total} scenes")
            break

        scene_id  = f"scene_{scene_idx + 1:03d}"
        panel_ids = [p['panel_id'] for p in scene_panels]
        ptypes    = [p.get('panel_type', 'standard') for p in scene_panels]

        if progress_callback:
            dominant = max(set(ptypes), key=ptypes.count)
            progress_callback(
                scene_idx + 1, total,
                f"Scene {scene_idx+1}/{total} "
                f"(panels {panel_ids[0]}-{panel_ids[-1]}, {dominant})"
            )

        # ── Resume from cache if available ───────────────────────────────────
        if scene_id in existing_narrations:
            narration = existing_narrations[scene_id]
            print(f"[SceneNarrator] ⏭  Reusing cached {scene_id}")
        else:
            narration = narrate_scene(
                scene_panels, host, model,
                story_so_far, cast_characters,
                cancel_check=cancel_check,
                chapter_context=chapter_context,
                vision_model=vision_model,
            )

        if narration == '[cancelled]':
            print(f"[SceneNarrator] ✋ Cancelled mid-scene at {scene_id}")
            break

        # ── Update rolling memory (sliding window of 3) ─────────────────────
        story_so_far.append(narration)
        if len(story_so_far) > 3:
            story_so_far.pop(0)

        # ── Tag every panel in the scene ─────────────────────────────────────
        for j, panel in enumerate(scene_panels):
            panel['scene_id']          = scene_id
            panel['scene_narration']   = narration
            panel['is_scene_lead']     = (j == 0)
            panel['scene_panel_count'] = len(scene_panels)

        scene_results.append({
            'scene_id':    scene_id,
            'panel_ids':   panel_ids,
            'panel_types': ptypes,
            'narration':   narration,
        })

        # ── Save after every scene (crash-safe) ─────────────────────────────
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(scene_results, f, indent=2, ensure_ascii=False)

    print(f"[SceneNarrator] Done — {len(scene_results)}/{total} scenes narrated, "
          f"{len(panels)} panels tagged")
    return panels


# ═══════════════════════════════════════════════════════════════════════════════
# SCENE AUDIO GENERATION (unchanged from working version)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_scene_audio(
    panels: list,
    project_dir: str,
    voice: str = None,
    rate: str = None,
    engine: str = None,
    speed: float = 1.0,
    progress_callback=None,
) -> list:
    """
    Generate TTS audio for scene narrations.
    Only the LEAD panel of each scene gets audio generated.
    Sets panel['scene_audio_path'] and panel['scene_audio_duration']
    on lead panels.
    """
    from pipeline.tts_engine import generate_speech, _get_audio_duration
    import config as cfg

    engine = engine or cfg.TTS_ENGINE
    audio_dir = os.path.join(project_dir, 'scene_audio')
    os.makedirs(audio_dir, exist_ok=True)

    # Collect unique scenes
    seen_scenes = set()
    scene_leads = [p for p in panels if p.get('is_scene_lead')]
    total = len(scene_leads)

    for i, panel in enumerate(scene_leads):
        scene_id  = panel.get('scene_id', f"scene_{i+1:03d}")
        narration = panel.get('scene_narration', '').strip()

        if progress_callback:
            progress_callback(i + 1, total, f"Audio: {scene_id} ({i+1}/{total})")

        if not narration or narration.startswith('['):
            panel['scene_audio_path']     = None
            panel['scene_audio_duration'] = 3.0
            continue

        audio_path = os.path.join(audio_dir, f"{scene_id}.mp3")

        # Smart resume: skip if file already exists
        if os.path.exists(audio_path):
            panel['scene_audio_path']     = audio_path
            panel['scene_audio_duration'] = _get_audio_duration(audio_path)
            continue

        try:
            result = generate_speech(
                text=narration,
                output_path=audio_path,
                voice=voice,
                rate=rate,
                engine=engine,
                speed=speed,
            )
            panel['scene_audio_path']     = result['audio_path']
            panel['scene_audio_duration'] = result['duration']
        except Exception as e:
            print(f"[SceneAudio] Error for {scene_id}: {e}")
            panel['scene_audio_path']     = None
            panel['scene_audio_duration'] = 3.0

    # Propagate scene audio info to all panels in the scene
    scene_audio_map = {
        p['scene_id']: (p.get('scene_audio_path'), p.get('scene_audio_duration', 3.0))
        for p in panels
        if p.get('is_scene_lead') and p.get('scene_id')
    }
    for panel in panels:
        sid = panel.get('scene_id')
        if sid and sid in scene_audio_map:
            ap, ad = scene_audio_map[sid]
            panel.setdefault('scene_audio_path', ap)
            panel.setdefault('scene_audio_duration', ad)

    return panels
