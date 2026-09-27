"""
Decoupled AI Narration Generator for Manhwa Recap
==================================================

Phase 1 (The Eyes / Structural Parser) → qwen2.5vl:3b  [vision model]
  - Sees the panel image
  - Extracts a raw "visual beat" — factual description of what is happening
  - Does NOT write narration, just metadata

Phase 2 (The Writer / Recap Storyteller) → gemma3:27b  [pure text model]
  - Receives ONLY text: visual beat + extracted dialogue + story-so-far
  - Writes immersive, dramatic third-person recap narration in the style of
    popular manhwa recap channels — sincere storytelling, no sarcasm, no
    mocking the characters
  - 100% of model weights focused on creative writing (no image processing)

Toggle: config.DECOUPLED_NARRATION
  True  → Phase 1 (vision) → Phase 2 (text) — recommended
  False → Legacy single-model approach (vision model does everything)
"""
import base64
import io
import json
import os
import re
import requests
from typing import Optional
from PIL import Image
import config

# ─── Model constants ──────────────────────────────────────────────
VISION_MODEL   = "qwen2.5vl:3b"          # Phase 1: vision parser (extracts visual beats)
WRITER_MODEL   = "qwen2.5vl:3b"          # Legacy: single vision-model narration
TEXT_WRITER    = getattr(config, 'TEXT_WRITER_MODEL', 'gemma3:27b')  # Phase 2: pure-text storyteller
FALLBACK_MODEL = "qwen2.5vl:7b"

# Bump when the narration prompt changes meaningfully — cached narrations made
# with an older prompt are treated as stale and get regenerated on re-run.
NARRATION_PROMPT_VERSION = 6


class Narrator:
    """
    Decoupled narrator with two phases:
      Phase 1 (Vision Parser):       qwen2.5vl:3b — extracts visual beats from panel images
      Phase 2 (Casual Storyteller):  gemma3:27b — writes narration from text-only input

    Toggle: config.DECOUPLED_NARRATION (True = decoupled, False = legacy single-model)
    """

    def __init__(self, model: str = None, host: str = None):
        self.host = (host or config.OLLAMA_HOST).rstrip('/')
        self._context_buffer = []
        self._max_context = 10
        self.decoupled = getattr(config, 'DECOUPLED_NARRATION', True)

        self.vision_model = self._resolve_model(VISION_MODEL)
        self.writer_model = self._resolve_model(WRITER_MODEL)
        self.text_writer_model = self._resolve_model(
            getattr(config, 'TEXT_WRITER_MODEL', TEXT_WRITER))
        self.model = self.writer_model

        mode = "DECOUPLED" if self.decoupled else "LEGACY"
        print(f"[Narrator] Mode: {mode}")
        print(f"[Narrator] Vision Parser  : {self.vision_model}")
        if self.decoupled:
            print(f"[Narrator] Text Storyteller: {self.text_writer_model}")
        else:
            print(f"[Narrator] Writer (legacy) : {self.writer_model}")

    @staticmethod
    def _model_supports_vision(model: str) -> bool:
        """True if the Ollama model name indicates vision capability
        (qwen2.5vl, llama3.2-vision, llava, minicpm-v, ...)."""
        m = (model or '').lower()
        return any(t in m for t in ('vl', 'vision', 'llava', 'minicpm-v', 'bakllava', 'moondream'))

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────

    def _resolve_model_name(self):
        """Update model resolution after self.model is changed by orchestrator."""
        self.vision_model = self._resolve_model(VISION_MODEL)
        self.writer_model = self._resolve_model(self.model if self.model else WRITER_MODEL)
        self.text_writer_model = self._resolve_model(
            getattr(config, 'TEXT_WRITER_MODEL', TEXT_WRITER))


    def _get_cast_reference_images(self, cast_characters: list[str]) -> list[dict]:
        """Returns a list of dicts: {'name': '...', 'image_path': '...'}"""
        if not cast_characters:
            return []
        
        cast_data_dir = os.path.expanduser("~/RECAP/Cast Data")
        if not os.path.exists(cast_data_dir):
            return []
            
        refs = []
        import json
        for cast_dir in os.listdir(cast_data_dir):
            chars_file = os.path.join(cast_data_dir, cast_dir, "characters.json")
            if os.path.exists(chars_file):
                try:
                    with open(chars_file, 'r') as f:
                        chars = json.load(f)
                    for char_id, char_data in chars.items():
                        name = char_data.get('name')
                        if name in cast_characters and char_data.get('reference_images'):
                            img_name = char_data['reference_images'][0]
                            img_path = os.path.join(cast_data_dir, cast_dir, "faces", img_name)
                            if os.path.exists(img_path):
                                refs.append({'name': name, 'image_path': img_path})
                except Exception as e:
                    pass
        
        # Deduplicate by name
        unique_refs = {}
        for r in refs:
            unique_refs[r['name']] = r
        return list(unique_refs.values())

    def narrate_panel_with_context(self, panel: dict,
                                   cast_characters: list[str] = None,
                                   chapter_text: str = "",
                                   raw_dir: str = None,
                                   manifest: dict = None,
                                   cast_reference_images: list = None) -> str:
        """
        Generate narration with three-input context (port from YTV3).

        Passes to the LLM:
          1. full chapter transcript
          2. the raw source page (full-page visual context)
          3. the panel itself
        """
        # The CLEAN art panel (no bubbles) is the primary image to narrate;
        # the bubble panel travels along as reading context when available.
        clean_path = panel.get('clean_path') if panel.get('clean_path') and os.path.exists(panel.get('clean_path')) else None
        primary_path = clean_path or panel['path']
        bubble_path = panel.get('padded_path')
        if bubble_path and bubble_path == primary_path:
            bubble_path = None
        extracted_text = panel.get('text', '').strip()
        visual_desc    = panel.get('visual_description', '').strip()
        cast_characters = cast_characters or []

        # Cast reference faces (who is who) — only useful to vision models
        refs = cast_reference_images or []

        images = []
        labels = []
        for r in refs:
            b64 = self._encode_image(r['image_path'])
            if b64:
                images.append(b64)
                g = r.get('gender') or ''
                gtxt = f", {g}" if g and g != 'unknown' else ''
                labels.append(f"Image {len(images)}: cast member {r['name']}{gtxt} — if this person appears in the panel, use their name")
        bubble_idx = None
        if bubble_path:
            b64 = self._encode_image(bubble_path)
            if b64:
                images.append(b64)
                bubble_idx = len(images)
                labels.append(f"Image {bubble_idx}: the same scene WITH speech bubbles — reading context only")
        b64 = self._encode_image(primary_path)
        panel_idx = len(images) + 1
        if b64:
            images.append(b64)
        labels.append(f"Image {panel_idx}: the SPECIFIC clean panel you must narrate")

        refs_text = ""
        if labels:
            refs_text = "\n".join(labels) + "\n\n"
            refs_text += "If a cast member appears in the target panel, use their name and pronouns.\n\n"

        narration = self._write_narration(
            images=images,
            chapter_text=chapter_text,
            extracted_text=extracted_text,
            visual_desc=visual_desc,
            cast_characters=[refs_text] if refs_text else cast_characters,
            panel_id=panel.get('panel_id', ''),
            cast_reference_images=refs,
        )

        if narration:
            self._context_buffer.append(narration)
            if len(self._context_buffer) > self._max_context:
                self._context_buffer.pop(0)

        return narration

    def narrate_all_panels(self, panels: list[dict], project_dir: str,
                           progress_callback=None,
                           cast_characters: list[str] = None,
                           raw_dir: str = None,
                           text_file: str = None,
                           manifest_file: str = None) -> list[dict]:
        """
        Generate narration for all panels and save.

        Args:
            raw_dir:      Path to raw chapter pages (for full-page context)
            text_file:    Path to chapter text/transcript file
            manifest_file: Path to panels_manifest.json (panel -> source page mapping)
        """
        self._context_buffer = []
        total = len(panels)
        narrations_file = os.path.join(project_dir, "narrations.json")

        # Load chapter transcript
        chapter_text = ""
        if text_file and os.path.exists(text_file):
            with open(text_file, 'r', encoding='utf-8') as f:
                chapter_text = f.read()

        # Load manifest (panel -> source page mapping)
        manifest = {}
        if manifest_file and os.path.exists(manifest_file):
            with open(manifest_file, 'r', encoding='utf-8') as f:
                manifest = json.load(f)

        # Load existing narrations for resume support
        existing = {}
        if os.path.exists(narrations_file):
            with open(narrations_file, 'r') as f:
                existing = {n['panel_id']: n['narration'] for n in json.load(f)}

        for i, panel in enumerate(panels):
            panel_id = panel['panel_id']
            if progress_callback:
                progress_callback(i + 1, total, f"Narrating panel {i + 1}/{total}")

            if panel_id in existing and existing[panel_id]:
                panel['narration'] = existing[panel_id]
                self._context_buffer.append(panel['narration'])
                if len(self._context_buffer) > self._max_context:
                    self._context_buffer.pop(0)
                continue

            prev_txt = panels[i-1]['text'] if i > 0 and 'text' in panels[i-1] else ""
            curr_txt = panel.get('text', '')
            next_txt = panels[i+1]['text'] if i < len(panels)-1 and 'text' in panels[i+1] else ""
            
            sliding_window = f"--- PREVIOUS PANEL DIALOGUE ---\n{prev_txt}\n\n--- CURRENT PANEL DIALOGUE ---\n{curr_txt}\n\n--- NEXT PANEL DIALOGUE ---\n{next_txt}"

            panel['narration'] = self.narrate_panel_with_context(
                panel=panel,
                cast_characters=cast_characters,
                chapter_text=sliding_window,
                raw_dir=raw_dir,
                manifest=manifest,
            )

            # Persist after every panel (crash-safe resume, port from YTV3)
            self._save_narrations(panels, narrations_file)

        self._save_narrations(panels, narrations_file)
        return panels

    def narrate_panel(self, panel_path: str, panel_id: int = 0,
                      custom_prompt: str = None) -> str:
        """Legacy single-panel narration."""
        images = [self._encode_image(panel_path)]
        return self._write_narration(
            images=images, chapter_text="",
            extracted_text="", visual_desc="",
            cast_characters=[], panel_id=str(panel_id),
        )

    def regenerate_panel(self, panel: dict, custom_prompt: str = None,
                         cast_characters: list[str] = None,
                         chapter_text: str = "",
                         cast_reference_images: list = None) -> str:
        """Force-regenerate narration for a specific panel.
        Accepts the same context inputs as batch narration (cast profiles +
        reference faces + sliding window chapter text) so the rewritten line
        keeps its flow."""
        self._context_buffer = []
        return self.narrate_panel_with_context(
            panel,
            cast_characters=cast_characters,
            chapter_text=chapter_text,
            cast_reference_images=cast_reference_images,
        )

    def check_ollama(self) -> dict:
        """Check Ollama status and available models."""
        result = {'running': False, 'model_available': False, 'models': []}
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=5)
            if resp.status_code == 200:
                result['running'] = True
                models = [m['name'] for m in resp.json().get('models', [])]
                result['models'] = models
                result['model_available'] = bool(self.writer_model)
                result['vision_model'] = self.vision_model
                result['writer_model'] = self.writer_model
        except Exception:
            pass
        return result

    def detect_characters(self, image_path: str) -> list[dict]:
        """Detect characters in a panel using the writer vision model."""
        with open(image_path, 'rb') as f:
            image_b64 = base64.b64encode(f.read()).decode('utf-8')

        prompt = (
            "Look at this manhwa panel. List all distinct characters you can see.\n"
            "For each character provide:\n"
            "1. Name or visual identifier (e.g. 'Silver-haired Knight')\n"
            "2. Brief physical description\n\n"
            "Respond ONLY with a valid JSON array:\n"
            '[{"name": "...", "description": "..."}]\n'
            "If no characters are visible, return: []"
        )

        try:
            resp = requests.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.writer_model,
                    "prompt": prompt,
                    "images": [image_b64],
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 500}
                },
                timeout=config.OLLAMA_TIMEOUT
            )
            resp.raise_for_status()
            content = resp.json().get('response', '').strip()
            if '```' in content:
                m = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
                if m:
                    content = m.group(1).strip()
            start, end = content.find('['), content.rfind(']')
            if start != -1 and end != -1:
                content = content[start:end + 1]
            chars = json.loads(content)
            if isinstance(chars, list):
                return [c for c in chars if isinstance(c, dict) and c.get('name')]
        except Exception as e:
            print(f"[Narrator] Character detection error: {e}")
        return []

    def update_narration(self, panels: list[dict], panel_id: int,
                          new_narration: str, project_dir: str) -> list[dict]:
        """Update narration for a specific panel and save."""
        for panel in panels:
            if panel['panel_id'] == panel_id:
                panel['narration'] = new_narration
                break
        self._save_narrations(panels, os.path.join(project_dir, "narrations.json"))
        return panels

    # ─────────────────────────────────────────────────────────────
    # Phase 1: Vision Parser — extract visual beat from panel image
    # ─────────────────────────────────────────────────────────────

    def _extract_visual_beat(self, images: list) -> str:
        """
        Phase 1: Send panel image to vision model and extract a raw visual beat.
        Returns a factual 1-2 sentence description of what's visually happening.
        The vision model does NOT narrate — it only describes.
        """
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
            # Only the LAST image is the panel to describe. The cast reference
            # faces earlier in the list are for the writer's name recognition —
            # sending all 12+ here ballooned every call to ~15k vision tokens
            # (10s/panel, GPU heat) for a model that is told NOT to name anyone.
            panel_image = images[-1] if images else None
            if isinstance(panel_image, tuple):
                panel_image = panel_image[0]
            resp = requests.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.vision_model,
                    "stream": False,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                            "images": [panel_image] if panel_image else [],
                        },
                    ],
                    "options": {
                        "temperature": 0.3,
                        "top_p": 0.85,
                        "num_predict": 150,
                    },
                },
                timeout=config.OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
            raw = resp.json()["message"]["content"].strip()
            # Clean up any preamble
            for prefix in ['here is', "here's", 'sure,', 'the panel', 'visual beat:']:
                if raw.lower().startswith(prefix):
                    raw = raw.split(':', 1)[-1].strip() if ':' in raw else raw
            return raw

        except Exception as e:
            print(f"[Narrator] Phase 1 visual beat extraction failed: {e}")
            return ""

    # ─────────────────────────────────────────────────────────────
    # Phase 2: Cinematic Storyteller — pure text narration
    # ─────────────────────────────────────────────────────────────

    def _write_cinematic_narration(self, visual_beat: str, key_dialogue: str,
                                    story_so_far: str, cast_characters: list[str],
                                    panel_image_b64: str = None,
                                    cast_reference_images: list = None) -> str:
        """
        Phase 2: writes cinematic narration.
        Text-only writer models (e.g. qwen2.5:14b) receive ONLY text tokens —
        the visual beat from Phase 1 stands in for the image. When the chosen
        writer IS a vision model (e.g. qwen2.5vl:7b), the panel image is
        attached too, so the writer sees the art directly.
        """
        # ── Build the cast section ────────────────────────────────
        cast_section = ""
        if cast_characters:
            clean_names = [n for n in cast_characters if n and "Image " not in n]
            if clean_names:
                cast_section = f"\nKNOWN CHARACTERS: {', '.join(clean_names)}\n"

        # ── Build story-so-far section ────────────────────────────
        continuity_section = ""
        if story_so_far:
            continuity_section = f"\nSTORY SO FAR (continue directly from this):\n{story_so_far}\n"
        else:
            continuity_section = "\nThis is the opening of the chapter. Set the scene and hook the viewer.\n"

        # ── Build the full prompt ─────────────────────────────────
        prompt = (
            "You are the voice of a popular manhwa recap YouTube channel. You tell the story "
            "straight: immersive, dramatic third-person narration that pulls the viewer in. "
            "You take the characters and their emotions seriously — you are NEVER sarcastic, "
            "never mock them, never break the fourth wall, and never use slang like 'bro' or "
            "'dude' or winks at the audience. The viewer can SEE the art; your narration is the "
            "story being told.\n\n"
            "RULES:\n"
            "1. This recap moves through HUNDREDS of panels — you get ONE line of voice-over per "
            "panel. Write ONE sentence (two very short ones at most), 15-25 words total. Pick the "
            "single most important thing this beat adds — emotion, motivation, or stakes — and say "
            "only that. If it doesn't fit, cut content, never words. No padding, no description "
            "that repeats the sentence you just wrote.\n"
            "2. NEVER describe what's visually obvious. Narrate the inner story — what the "
            "character feels, decides, risks — not what the drawing shows.\n"
            "3. FLOW: your line continues the previous narration as ONE unbroken story. Use "
            "quiet transitions ('and', 'so', 'but', 'moments later'). Never restart, never summarize.\n"
            "4. PRESENT TENSE, THIRD PERSON, active voice. Smooth, novel-like spoken sentences.\n"
            "5. Dialogue: weave in what was said naturally — paraphrase the meaning, or quote a "
            "few words at most, always clear who is speaking. Never ignore the dialogue.\n"
            "6. If this is the opening of the chapter, your line is the HOOK: open on the most "
            "gripping question, tension, or turning point. NEVER start with 'This story begins' "
            "or 'Meet our protagonist'.\n"
            "7. NEVER mention panels, images, art, or the medium itself.\n"
            "8. NEVER invent character names. Use only names from the Dialogue or KNOWN "
            "CHARACTERS list; otherwise 'he', 'she', 'the young man', 'the girl'.\n"
            "9. Output PLAIN TEXT ONLY — no markdown, no asterisks, no quotes around words, no "
            "bold, no emojis. Your text is read aloud by a text-to-speech voice.\n"
            "10. Match the rhythm to the moment: quiet scenes get gentle lines; turning points "
            "get a short punch.\n\n"
            "=========================================\n"
            "TONE EXAMPLES (this is the voice — dramatic, sincere, ONE short line per panel)\n"
            "=========================================\n"
            "Visual Beat: Guy holding a sealed love letter at his desk.\n"
            "Dialogue: 'This love letter... a whole semester in the making.'\n"
            "Output: One letter, rewritten all semester — and today he finally hands it over.\n\n"
            "Visual Beat: Same guy turns around in class, letter in hand.\n"
            "Dialogue: 'Today I'm gonna give it to her.'\n"
            "Output: His heart is pounding. No more rehearsals — he tells her today.\n\n"
            "Visual Beat: A girl sits by the window, sneaking glances at him.\n"
            "Dialogue: 'Because she occasionally peeks at me.'\n"
            "Output: And he has proof: across the room, she keeps stealing glances at him.\n\n"
            "Visual Beat: Phone screen showing a university admission result.\n"
            "Dialogue: 'computer engineering'\n"
            "Output: Ritsuki clawed his way out of nothing the day that admission notice lit up his screen.\n\n"
            "Visual Beat: A truck slamming into someone at night.\n"
            "Dialogue: (none)\n"
            "Output: He never made it home. One instant of headlights, and it was over.\n\n"
            "Visual Beat: The same guy waking up as a baby in a fantasy world.\n"
            "Dialogue: (none)\n"
            "Output: Then his eyes opened again — as a newborn, in a world he did not recognize.\n\n"
            "=========================================\n"
            "YOUR TURN\n"
            "=========================================\n"
            f"{cast_section}"
            f"{continuity_section}\n"
            f"Visual Beat: {visual_beat if visual_beat else '(no image description available)'}\n"
            f"Dialogue: {key_dialogue if key_dialogue else '(none)'}\n\n"
            "Output (ONE short sentence, 15-25 words, plain text, match the example voice above):"
        )

        try:
            # Vision-capable writers (qwen2.5vl:7b, llama3.2-vision, ...) get
            # the cast faces + the clean panel image directly; text-only
            # writers (qwen2.5:14b) rely on the Phase 1 visual beat instead.
            if self._model_supports_vision(self.text_writer_model):
                ollama_images = []
                for ref in (cast_reference_images or []):
                    b64 = self._encode_image(ref.get('image_path', ''))
                    if b64:
                        ollama_images.append(b64[0] if isinstance(b64, tuple) else b64)
                if panel_image_b64:
                    ollama_images.append(panel_image_b64[0] if isinstance(panel_image_b64, tuple) else panel_image_b64)
                if ollama_images:
                    prompt += (
                        "\n\nAttached images, in order: cast member reference faces first "
                        "(use them to recognise who is in the panel), then the clean panel "
                        "to narrate."
                    )
                    print(f"[Narrator] Phase 2: attaching {len(ollama_images)} image(s) to vision writer")
                    return self._chat_with_retries(prompt, ollama_images) or \
                        "[Narration failed — writer model returned empty. Re-run this step to retry.]"
            return self._chat_with_retries(prompt, None) or \
                "[Narration failed — writer model returned empty. Re-run this step to retry.]"

        except requests.exceptions.Timeout:
            return "[Narration timed out — text writer model may still be loading.]"
        except Exception as e:
            return f"[Phase 2 error: {e}]"

    def _chat_with_retries(self, prompt: str, images: list = None) -> str:
        """Ask the writer model for narration, retrying empty generations.

        gemma3 under Ollama sometimes returns an instant empty completion
        (immediate end-of-turn) — panels would silently save as ''. A small
        temperature bump clears it; if it still comes back empty, a second
        installed text model takes the pen so a chapter never half-narrates.
        """
        attempts = [
            (self.text_writer_model, 0.78),
            (self.text_writer_model, 0.95),
        ]
        fallback = self._fallback_writer()
        if fallback:
            attempts.append((fallback, 0.8))

        for model, temp in attempts:
            message = {"role": "user", "content": prompt}
            if images:
                message["images"] = images
            resp = requests.post(
                f"{self.host}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "messages": [message],
                    "options": {
                        "temperature": temp,
                        "top_p": 0.90,
                        # 15-25 word target with headroom — a hard cap so the
                        # model physically cannot run long on a single panel.
                        "num_predict": 90,
                        "repeat_penalty": 1.20,
                    },
                },
                timeout=config.OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
            narration = self._clean_narration(resp.json()["message"]["content"].strip())
            if narration:
                if model != self.text_writer_model:
                    print(f"[Narrator] Phase 2: primary writer returned empty — {model} wrote this line")
                return narration
            print(f"[Narrator] Phase 2: {model} generated empty narration, retrying...")
        return ""

    def _fallback_writer(self):
        """Another installed text model to lean on if the primary writer fails."""
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=5)
            if resp.status_code != 200:
                return None
            names = [m['name'] for m in resp.json().get('models', [])]
            for cand in names:
                if cand != self.text_writer_model and not self._model_supports_vision(cand):
                    return cand
        except Exception:
            pass
        return None

    # ─────────────────────────────────────────────────────────────
    # Core narration writer — orchestrates Phase 1 → Phase 2
    # ─────────────────────────────────────────────────────────────

    def _write_narration(self, images: list, chapter_text: str,
                          extracted_text: str, visual_desc: str,
                          cast_characters: list[str], panel_id: str,
                          cast_reference_images: list = None) -> str:
        """
        Orchestrate narration generation.

        DECOUPLED mode (recommended):
          Phase 1: Vision model extracts visual beat from panel image
          Phase 2: Writer writes the narration line — with the panel image
                   attached too when the chosen writer model has vision

        LEGACY mode:
          Single vision model sees image + writes narration directly
        """
        cast_reference_images = cast_reference_images or []
        if self.decoupled:
            return self._write_narration_decoupled(
                images, chapter_text, extracted_text, visual_desc,
                cast_characters, panel_id, cast_reference_images)
        else:
            return self._write_narration_legacy(
                images, chapter_text, extracted_text, visual_desc,
                cast_characters, panel_id)

    def _write_narration_decoupled(self, images: list, chapter_text: str,
                                     extracted_text: str, visual_desc: str,
                                     cast_characters: list[str], panel_id: str,
                                     cast_reference_images: list = None) -> str:
        """
        DECOUPLED: Phase 1 (vision → visual beat) → Phase 2 (text → narration).
        """
        # ── Phase 1: Extract visual beat ──────────────────────────
        visual_beat = visual_desc  # Use pre-existing visual description if available
        if not visual_beat or len(visual_beat) < 10:
            if images:
                print(f"[Narrator] Panel {panel_id}: Phase 1 → extracting visual beat...")
                visual_beat = self._extract_visual_beat(images)
                if visual_beat:
                    print(f"[Narrator] Panel {panel_id}: Visual beat: {visual_beat[:80]}...")

        # ── Determine key dialogue ────────────────────────────────
        key_dialogue = ""
        if chapter_text:
            key_dialogue = chapter_text
        elif extracted_text and extracted_text != "[Chapter text extracted]":
            key_dialogue = extracted_text

        # ── Build story-so-far from context buffer ────────────────
        story_so_far = ""
        if self._context_buffer:
            story_so_far = " ".join(self._context_buffer[-6:])

        # ── Clean cast characters ─────────────────────────────────
        clean_cast = []
        if cast_characters:
            for c in cast_characters:
                if isinstance(c, str) and "Image " not in c:
                    clean_cast.append(c)

        # ── Phase 2: Write cinematic narration ────────────────────
        print(f"[Narrator] Panel {panel_id}: Phase 2 → writing cinematic narration...")
        # Last image in the list is the clean panel being narrated
        panel_image_b64 = images[-1] if images else None
        return self._write_cinematic_narration(
            visual_beat=visual_beat,
            key_dialogue=key_dialogue,
            story_so_far=story_so_far,
            cast_characters=clean_cast,
            panel_image_b64=panel_image_b64,
            cast_reference_images=cast_reference_images or [],
        )

    def _write_narration_legacy(self, images: list, chapter_text: str,
                                  extracted_text: str, visual_desc: str,
                                  cast_characters: list[str], panel_id: str) -> str:
        """
        LEGACY: Single vision model sees image + writes narration directly.
        Kept for backward compatibility when DECOUPLED_NARRATION = False.
        """
        image_note = ""
        if len(images) > 1:
            image_note = "Multiple images are attached. See character references above.\n\n"
        else:
            image_note = "One image attached: the panel to narrate.\n\n"

        text_section = ""
        if chapter_text:
            text_section = f"\n\n### STORYLINE TIMELINE CONTEXT:\n{chapter_text}"
        elif extracted_text and extracted_text != "[Chapter text extracted]":
            text_section = f"\n\nDialogue extracted from this panel:\n{extracted_text}"
        else:
            text_section = "\n\n(No specific dialogue extracted for this panel.)"

        desc_section = ""
        if visual_desc:
            desc_section = f"\n\nScene context: {visual_desc}"

        cast_section = ""
        if cast_characters:
            if "Image " in cast_characters[0]:
                cast_section = f"\n\n{cast_characters[0]}"
            else:
                cast_section = f"\n\nKnown characters in this story: {', '.join(cast_characters)}"

        context_section = ""
        if self._context_buffer:
            recent = self._context_buffer[-6:]
            context_section = "\n\n### STORY SO FAR (the narration you have already written — continue directly from this):\n"
            context_section += " ".join(recent)

        prompt = (
            "You are the voice of a popular manhwa recap YouTube channel. Tell the story straight: "
            "immersive, dramatic third-person narration. Take the characters seriously — NEVER be "
            "sarcastic, never mock them, never use slang like 'bro' or break the fourth wall. "
            "The viewer can see the art; your narration is the story being told.\n\n"
            f"{image_note}"
            f"{text_section}"
            f"{desc_section}"
            f"{cast_section}"
            f"{context_section}\n\n"
            "YOUR TASK: Add ONE short line of narration (15-25 words, two very short sentences at "
            "most) covering the CURRENT PANEL. This recap moves through hundreds of panels, so keep "
            "it to the single most important thing this beat adds. "
            "Seamlessly pick up from the previous narration. Do NOT re-summarise what already happened.\n\n"
            "STYLE RULES:\n"
            "1. VOICE: Third-person, present tense, active voice. Natural spoken language — contractions are good. "
            "You're a friend telling the story, not an audiobook narrator.\n"
            "2. FLOW: Your sentences must flow naturally from the previous narration. Connect beats with transitions "
            "('so', 'and that's when', 'meanwhile'). The full chapter should read as one unbroken story.\n"
            "3. DON'T STATE THE OBVIOUS: the viewer can see the art. Add the context, motivation, and transitions "
            "the images can't show — don't caption what's on screen.\n"
            "4. SINCERITY: take every moment seriously — no sarcasm, no mocking the characters, "
            "no 'bro' slang, no breaking the fourth wall.\n"
            "5. HOOK: If this is the first narration of the chapter, open with the most interesting twist or "
            "question. Never start with 'This story begins' or 'Meet our protagonist'.\n"
            "6. DIALOGUE: When a character speaks, weave their words into the narration naturally — paraphrase or "
            "quote briefly, whichever sounds better spoken aloud.\n"
            "7. CHARACTER NAMES: Use names when known. Never say 'Speaker 1' or 'The character'. For unknowns, "
            "use simple references ('the guy', 'our MC', 'the silver-haired girl').\n"
            "8. GENDER: Look at the image. Default to male pronouns ('he/him') for ambiguous art styles.\n"
            "9. NO META LANGUAGE: NEVER say 'In this panel', 'The image shows', 'We see', or 'This scene'.\n"
            "10. NO PADDING: Do not start with filler like 'Meanwhile', 'Suddenly' every single time. Vary your openers.\n"
            "11. REFERENCE EXAMPLE STYLE (match this energy — but half the length):\n"
            "   'And nobody expects much from him — just a low-rank nobody everyone laughs at. "
            "But what nobody knows is that his abilities have completely mutated.'\n\n"
            "Write ONLY the narration text. No labels, no explanations, no preamble.\n\n"
            "Continue the story now:"
        )

        try:
            resp = requests.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.writer_model,
                    "stream": False,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                            "images": [b64 for b64, _ in images],
                        },
                    ],
                    "options": {
                        "temperature": 0.82,
                        "top_p": 0.92,
                        "num_predict": 90,
                    },
                },
                timeout=config.OLLAMA_TIMEOUT,
            )
            resp.raise_for_status()
            narration = resp.json()["message"]["content"].strip()
            return self._clean_narration(narration)

        except requests.exceptions.ConnectionError:
            try:
                with open(self._temp_path(images[0] if images else ""), 'rb') as f:
                    image_b64 = base64.b64encode(f.read()).decode('utf-8')
                resp = requests.post(
                    f"{self.host}/api/generate",
                    json={
                        "model": self.writer_model,
                        "prompt": prompt,
                        "images": [image_b64],
                        "stream": False,
                        "options": {
                            "temperature": 0.82,
                            "top_p": 0.92,
                            "num_predict": 90,
                        },
                    },
                    timeout=config.OLLAMA_TIMEOUT,
                )
                resp.raise_for_status()
                narration = resp.json().get('response', '').strip()
                return self._clean_narration(narration)
            except Exception:
                return "[Ollama not running. Please start Ollama first.]"
        except requests.exceptions.Timeout:
            return "[Narration timed out — model may still be loading.]"
        except Exception as e:
            return f"[Error: {e}]"

    @staticmethod
    def _temp_path(image_data) -> str:
        """Write base64 image to temp file for fallback API call."""
        import tempfile
        if isinstance(image_data, tuple):
            b64_data = image_data[0] if isinstance(image_data, tuple) else ""
            tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
            tmp.write(base64.b64decode(b64_data))
            tmp.close()
            return tmp.name
        return ""

    # ─────────────────────────────────────────────────────────────
    # Image encoding helper (port from YTV3 script_generator.py)
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _encode_image(path: str) -> tuple:
        """Downscale + JPEG-encode an image for the LLM API (b64, media_type)."""
        with Image.open(path) as img:
            img = img.convert("RGB")
            max_side = getattr(config, 'LLM_IMAGE_MAX_SIDE', 1400)
            img.thumbnail((max_side, max_side), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"

    # ─────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────

    def _resolve_model(self, preferred: str) -> str:
        """Find the best matching installed model for a preferred name."""
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = [m['name'] for m in resp.json().get('models', [])]
                if not models:
                    return preferred
                # Exact match first
                if preferred in models:
                    return preferred
                # Family-aware matching: qwen2.5 should NOT match qwen2.5vl
                base = preferred.split(':')[0]
                is_vision_request = any(k in base.lower() for k in ['vl', 'vision', 'llava'])
                family_matches = []
                for m in models:
                    m_base = m.split(':')[0]
                    m_is_vision = any(k in m_base.lower() for k in ['vl', 'vision', 'llava'])
                    # Only match same family AND same type (vision↔vision, text↔text)
                    if m_base == base:
                        family_matches.append(m)
                    elif m_base.startswith(base) and is_vision_request == m_is_vision:
                        family_matches.append(m)
                if family_matches:
                    return family_matches[0]
                # Broader fallback: any model of the same type
                if is_vision_request:
                    vision_models = [m for m in models if any(k in m.lower() for k in ['vl', 'vision', 'llava'])]
                    if vision_models:
                        return vision_models[0]
                else:
                    # For text models, prefer non-vision models
                    text_models = [m for m in models if not any(k in m.lower() for k in ['vl', 'vision', 'llava'])]
                    if text_models:
                        return text_models[0]
                if models:
                    return models[0]
        except Exception:
            pass
        return preferred

    def _clean_narration(self, text: str) -> str:
        """Strip AI preamble and artifacts from narration output."""
        if not text:
            return text
        lines = text.strip().split('\n')
        cleaned = []
        skip_starts = [
            'here is', "here's", 'sure,', 'certainly', 'of course',
            'narration:', 'script:', 'the narration', 'panel shows',
            'this panel', 'in this image', 'based on', 'looking at',
        ]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if any(line.lower().startswith(p) for p in skip_starts):
                for sep in [':', '-', '—']:
                    if sep in line:
                        remainder = line.split(sep, 1)[1].strip()
                        if remainder:
                            cleaned.append(remainder)
                            break
                continue
            cleaned.append(line)
        result = ' '.join(cleaned)
        # Markdown emphasis never belongs in text a TTS voice reads aloud
        result = re.sub(r'[*_]{1,2}([^*_]+)[*_]{1,2}', r'\1', result)
        result = result.replace('*', '')
        if result.startswith('"') and result.endswith('"'):
            result = result[1:-1]
        return self._trim_narration(result.strip())

    @staticmethod
    def _trim_narration(text: str, max_words: int = 30) -> str:
        """Recap pacing: one panel gets one short line, even if the model rambles.

        Drops trailing sentences until the line fits the budget; if a single
        sentence is still over, cut it at the last clause break before the
        limit. Never touches bracketed failure markers.
        """
        if not text or text.startswith('['):
            return text
        words = text.split()
        if len(words) <= max_words:
            return text
        sentences = re.split(r'(?<=[.!?])\s+', text)
        out = []
        count = 0
        for s in sentences:
            c = len(s.split())
            if out and count + c > max_words:
                break
            out.append(s)
            count += c
        result = ' '.join(out).strip()
        if count > max_words and len(out) == 1:
            head = ' '.join(words[:max_words])
            for sep in (' — ', ' – ', ', ', '; '):
                cut = head.rfind(sep)
                if cut > max_words * 3:
                    result = head[:cut].rstrip(',;') + '.'
                    break
            else:
                result = head.rstrip(',-—–') + '.'
        return result

    def _save_narrations(self, panels: list[dict], filepath: str):
        """Save narrations to JSON."""
        data = [{
            'panel_id': p['panel_id'],
            'source_page': p.get('source_page', ''),
            'narration': p.get('narration', ''),
        } for p in panels]
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
