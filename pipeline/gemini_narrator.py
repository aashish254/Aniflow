"""
Gemini Multi-Key Narrator
=========================

Cloud narration backend for the Narrated Recap pipeline — an ALTERNATIVE to
the local Ollama decoupled narrator. Designed for users without a powerful
enough PC to run the local vision + writer models.

Key features:
  - MULTIPLE API KEYS: paste several Gemini keys; they are tried in order.
  - AUTOMATIC ROTATION: when a key hits its quota (HTTP 429), the narrator
    instantly switches to the next key and retries — no user action needed.
  - Bad/invalid keys (HTTP 400) are skipped the same way.
  - Transient server errors (500/503) are retried on the SAME key with a
    short backoff before rotating.
  - The prompt mirrors the local pipeline's style: dry, witty, one punchy
    sentence per panel, flowing from the previous narration.

Uses the plain Gemini REST API (no extra pip dependency).
"""
import base64
import json
import time
import requests

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Style guide shared with the local narrator so local and cloud narrations
# are interchangeable in the final video.
NARRATION_STYLE_RULES = (
    "You are narrating a manhwa recap video, one panel at a time — like the best recap channels on YouTube. "
    "The viewer cannot read the panel; your narration IS the story for them.\n\n"
    "RULES:\n"
    "1. Write 1-2 SHORT sentences for this panel. Maximum 25 words. Plain, natural storytelling.\n"
    "2. Tell the STORY in order. Your line must read as the next beat of one continuous story, "
    "picking up where the previous narration stopped. Do not restart or summarize.\n"
    "3. PRESENT TENSE, THIRD PERSON. Active voice.\n"
    "4. When the panel has dialogue, cover what was said naturally — paraphrase or quote briefly, "
    "whichever sounds better spoken aloud. Never ignore the dialogue.\n"
    "5. Write like a human narrator, NOT a joke machine: NO forced irony, NO 'because even X needs Y' "
    "constructions, NO repeated sentence patterns between panels, NO sarcasm unless the scene itself is sarcastic.\n"
    "6. NEVER describe images ('in this panel', 'we see') and NEVER comment on art.\n"
    "7. NEVER invent character names. Use only names from the Dialogue or KNOWN CHARACTERS list; "
    "otherwise say 'he', 'she', 'the guy', etc.\n"
    "8. Vary sentence rhythm naturally. Quiet panels get quiet lines; dramatic panels get punch.\n"
)


class GeminiExhaustedError(Exception):
    """All provided API keys are dead/exhausted."""


class GeminiNarrator:
    """Gemini narration with automatic multi-key quota rotation."""

    def __init__(self, api_keys, model: str = "gemini-2.5-flash",
                 status_callback=None):
        # Accept a list, a newline/comma-separated string, or a single key
        if isinstance(api_keys, str):
            api_keys = [k.strip() for k in api_keys.replace(",", "\n").split("\n")]
        self.keys = [k for k in (api_keys or []) if k and k.strip()]
        self.keys = list(dict.fromkeys(self.keys))  # dedupe, keep order
        self.model = model or "gemini-2.5-flash"
        self.status_callback = status_callback or (lambda msg: None)
        self.active_index = 0
        self.dead_keys = set()   # indices known dead (quota/invalid) this session
        self.used_keys = set()   # indices that successfully served at least once

    # ── Properties ────────────────────────────────────────────────
    @property
    def active_key(self):
        return self.keys[self.active_index] if self.keys else None

    @property
    def alive_keys(self):
        return [k for i, k in enumerate(self.keys) if i not in self.dead_keys]

    def keys_status(self):
        """Per-key status list for UI display: [{key_masked, status}]"""
        out = []
        for i, k in enumerate(self.keys):
            if i in self.dead_keys:
                status = "exhausted"
            elif i in self.used_keys:
                status = "active" if i == self.active_index else "ready"
            else:
                status = "ready"
            out.append({"index": i, "key_masked": _mask(k), "status": status})
        return out

    # ── Core call with rotation ───────────────────────────────────
    def generate(self, prompt: str, image_paths: list = None,
                 max_retries_per_key: int = 2) -> str:
        """Generate text, rotating keys on quota/auth errors.

        Raises GeminiExhaustedError when every key is dead.
        """
        if not self.keys:
            raise GeminiExhaustedError("No Gemini API keys provided.")

        parts = [{"text": prompt}]
        for p in (image_paths or []):
            b64 = _encode_image(p)
            if b64:
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.78,
                "topP": 0.90,
                "maxOutputTokens": 200,
                # 2.5-flash "thinking" off → fast, cheap recap lines
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        url = f"{GEMINI_API_BASE}/models/{self.model}:generateContent"

        tried_alive = 0
        while tried_alive <= len(self.keys):
            if self.active_index in self.dead_keys:
                if not self._advance_key():
                    break
                continue
            key = self.keys[self.active_index]
            self.status_callback(f"Using Gemini key {self.active_index + 1}/{len(self.keys)}")

            retries = 0
            while True:
                try:
                    resp = requests.post(
                        f"{url}?key={key}", json=body, timeout=120)
                except requests.exceptions.RequestException as e:
                    if retries < max_retries_per_key:
                        retries += 1
                        time.sleep(1.5 * retries)
                        continue
                    self.status_callback(
                        f"Key {self.active_index + 1} network error ({e}) — switching key")
                    if not self._advance_key():
                        raise GeminiExhaustedError(f"All keys failed on network errors (last: {e})")
                    tried_alive += 1
                    break

                if resp.status_code == 200:
                    self.used_keys.add(self.active_index)
                    return _extract_text(resp)

                kind = _classify_error(resp)
                if kind == "transient" and retries < max_retries_per_key:
                    retries += 1
                    time.sleep(1.5 * retries)
                    continue
                if kind in ("quota", "auth"):
                    self.dead_keys.add(self.active_index)
                    self.status_callback(
                        f"Key {self.active_index + 1} {kind} error — switching to next key")
                    if not self._advance_key():
                        raise GeminiExhaustedError(
                            f"All {len(self.keys)} Gemini key(s) are exhausted or invalid. "
                            f"Add more keys and re-run.")
                    tried_alive += 1
                    break
                # Other client errors (bad request etc.) — fail with detail
                raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
        raise GeminiExhaustedError(
            f"All {len(self.keys)} Gemini key(s) are exhausted or invalid. "
            f"Add more keys and re-run.")

    def _advance_key(self) -> bool:
        """Move to the next alive key. Returns False if none remain."""
        for step in range(1, len(self.keys) + 1):
            idx = (self.active_index + step) % len(self.keys)
            if idx not in self.dead_keys:
                self.active_index = idx
                return True
        return False

    # ── Panel narration (mirrors the local mapped flow) ──────────
    def narrate_panel(self, art_panel_path: str, bubble_panel_path: str = None,
                      dialogue_window: str = "", cast_characters=None,
                      story_so_far: str = "", panel_id=None,
                      cast_reference_images: list = None) -> str:
        """Narrate one panel.

        Sends, in order:
          - cast member reference faces (so Gemini recognises WHO is who)
          - the bubble panel (reading context), when available
          - the clean art panel (the one to narrate) — LAST
        plus the sliding dialogue window and cast profiles (name/gender/role),
        matching the local narrator's inputs so output style stays consistent.
        """
        cast_section = ""
        if cast_characters:
            cast_section = f"KNOWN CHARACTERS: {'; '.join(cast_characters)}\n"
        continuity = ""
        if story_so_far:
            continuity = f"STORY SO FAR (continue directly from this):\n{story_so_far}\n"
        else:
            continuity = "This is the opening of the chapter. Set the scene and hook the viewer.\n"

        cast_reference_images = cast_reference_images or []
        labels = []
        for i, ref in enumerate(cast_reference_images):
            g = ref.get('gender') or ''
            gtxt = f", {g}" if g and g != 'unknown' else ''
            labels.append(f"Image {i + 1}: cast member {ref['name']}{gtxt} — if this person appears in the panel, use their name")

        prompt = (
            f"{NARRATION_STYLE_RULES}\n"
            f"{cast_section}{continuity}\n"
        )
        body_lines = []
        if labels:
            body_lines.append("\n".join(labels))
            body_lines.append("If a cast member appears in the target panel, use their name and pronouns.")
        body_lines.append("The LAST image is the clean panel you must narrate."
                          + (" An earlier image is the same scene with speech bubbles — reading context only."
                             if bubble_panel_path else ""))
        prompt += "\n".join(body_lines) + "\n\n"
        prompt += f"DIALOGUE:\n{dialogue_window or '(none)'}\n\n"
        prompt += "Output (1 sentence, max 25 words, match the style above):"

        images = []
        for ref in cast_reference_images:
            if ref.get('image_path'):
                images.append(ref['image_path'])
        if bubble_panel_path and bubble_panel_path != art_panel_path:
            images.append(bubble_panel_path)
        images.append(art_panel_path)

        return self.generate(prompt, image_paths=images).strip()


# ── Helpers ───────────────────────────────────────────────────────
def _mask(key: str) -> str:
    return (key[:6] + "…" + key[-4:]) if len(key) > 12 else "•••"


def _encode_image(path: str):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None


def _extract_text(resp) -> str:
    try:
        data = resp.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = ((cand.get("content") or {}).get("parts") or [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if text:
            return text
        # Empty candidates can mean safety block — surface reason
        reason = cand.get("finishReason") or data.get("promptFeedback", {}).get("blockReason")
        return f"[Gemini returned no text ({reason or 'unknown reason'})]"
    except Exception as e:
        return f"[Gemini response parse error: {e}]"


def _classify_error(resp) -> str:
    """'quota' | 'auth' | 'transient' | 'other'"""
    body = ""
    try:
        body = resp.text[:500].lower()
    except Exception:
        pass
    if resp.status_code == 429 or "quota" in body or "resource_exhausted" in body:
        return "quota"
    if resp.status_code == 400 and ("api key not valid" in body or "api_key_invalid" in body):
        return "auth"
    if resp.status_code in (401, 403):
        return "auth"
    if resp.status_code in (500, 503, 504):
        return "transient"
    return "other"


def validate_keys(api_keys) -> list:
    """Cheaply validate each key with a minimal generateContent request.

    Returns [{'key_masked', 'valid', 'reason'}] — never raises.
    """
    results = []
    if isinstance(api_keys, str):
        api_keys = [k.strip() for k in api_keys.replace(",", "\n").split("\n")]
    for k in (api_keys or []):
        k = (k or "").strip()
        if not k:
            continue
        try:
            resp = requests.post(
                f"{GEMINI_API_BASE}/models/gemini-2.5-flash:generateContent?key={k}",
                json={"contents": [{"parts": [{"text": "Reply with the single word: OK"}]}],
                      "generationConfig": {"maxOutputTokens": 5,
                                           "thinkingConfig": {"thinkingBudget": 0}}},
                timeout=20)
            kind = _classify_error(resp)
            if resp.status_code == 200:
                results.append({"key_masked": _mask(k), "valid": True, "reason": "ok"})
            elif kind == "quota":
                # Key is VALID but currently out of quota — still usable later
                results.append({"key_masked": _mask(k), "valid": True,
                                "reason": "valid but quota exhausted right now"})
            elif kind == "auth":
                results.append({"key_masked": _mask(k), "valid": False, "reason": "invalid key"})
            else:
                results.append({"key_masked": _mask(k), "valid": False,
                                "reason": f"HTTP {resp.status_code}"})
        except requests.exceptions.RequestException as e:
            results.append({"key_masked": _mask(k), "valid": False, "reason": f"network: {e}"})
    return results
