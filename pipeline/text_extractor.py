"""
Two-Step Text Extractor for Manhwa/Webtoon Panels
==================================================

Engine selection (OCR_ENGINE config):
  qwen       - default. qwen2.5vl:3b vision model — per-panel text extraction + scene description
  magi       - Magi v2 (ragavsachdeva/magiv2) — chapter-wide transcription of RAW pages
  tesseract  - per-panel OCR via Tesseract (binary required)
  manga-ocr  - per-panel OCR tuned for manga lettering

Step 1 (qwen): → qwen2.5vl:3b (vision model)
  - Sees the panel image
  - Extracts all visible text exactly
  - Describes character expressions and scene mood

Step 1 (magi): → Magi v2 (full chapter, from raw pages)
  - Runs chapter-wide speaker-aware transcription

Falls back to LLaVA if qwen2.5vl is not available.
"""
import os
import json
import base64
import logging
import re
import requests
from pathlib import Path
import config

log = logging.getLogger("pipeline.text_extractor")


def _fast_json(payload: dict) -> dict:
    """⚡ SPEED MODE: keep the Ollama model loaded between panels (avoids
    model-reload stalls). No effect on output; classic mode is unchanged."""
    if getattr(config, 'SPEED_MODE', False) and 'keep_alive' not in payload:
        payload = {**payload, 'keep_alive': '30m'}
    return payload

# ─── Model constants ──────────────────────────────────────────────
VISION_MODEL  = "qwen2.5vl:3b"   # The Eyes  – sees the image
WRITER_MODEL  = "qwen2.5vl:3b"  # kept here for reference (used by narrator)
FALLBACK_MODEL = "qwen2.5vl:7b"

# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def extract_text_all_panels(panels: list[dict], state,
                            model: str = None, host: str = None,
                            progress_callback=None, engine: str = None,
                            stitched_boxes: list[dict] = None) -> list[dict]:
    """
    Extract text spatially per panel using a padded bounding box from the stitched image.
    Saves to: manhwa_text/{manhwa_name}/Chapter_{num:05d}/extracted_text.json
    """
    engine = (engine or config.OCR_ENGINE).lower()
    host = (host or config.OLLAMA_HOST).rstrip('/')
    vision_model = _resolve_vision_model(host)
    print(f"[TextExtractor] Using engine={engine}, vision_model={vision_model} for spatial panel extraction")

    total = len(panels)
    txt_lines = []
    
    import cv2
    import os
    import json
    
    # Map stitched_boxes by panel ID if available
    box_map = {}
    if stitched_boxes:
        for idx, box in enumerate(stitched_boxes):
            box_map[idx + 1] = box

    extracted_data = []
    for i, panel in enumerate(panels):
        if progress_callback:
            progress_callback(i + 1, total, f"Extracting dialogue from Panel {i + 1}/{total}...")

        text = ""
        visual_desc = ""
        panel_id = panel.get('panel_id', i + 1)
        
        try:
            # Use the pre-generated padded crop for AI context
            img_to_send = panel.get('padded_path') or panel.get('path')
            
            if img_to_send and os.path.exists(img_to_send):
                text, visual_desc = _extract_text_and_description(img_to_send, vision_model, host, engine)
                
        except Exception as e:
            print(f"[TextExtractor] Error on Panel {panel_id}: {e}")

        # Store in panel and chapter script
        panel['text'] = text.strip() if text.strip() else ""
        panel['visual_description'] = visual_desc.strip() if visual_desc.strip() else ""
        
        txt_lines.append(f"[PANEL {panel_id}]")
        txt_lines.append(panel['text'] if panel['text'] else "(no dialogue)")
        txt_lines.append("")
        
        extracted_data.append({
            'panel_id': panel_id,
            'text': panel['text'],
            'visual_description': panel['visual_description'],
            'panel_path': panel.get('path', ''),
        })

    # Save to new organized structure: manhwa_text/{name}/Chapter_{num:05d}/
    os.makedirs(state.text_dir, exist_ok=True)
    
    # Save as plain text
    txt_path = os.path.join(state.text_dir, "extracted_text.txt")
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    
    # Save as JSON with full metadata
    json_path = os.path.join(state.text_dir, "extracted_text.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'total_panels': len(extracted_data),
            'extraction_engine': engine,
            'vision_model': vision_model,
            'panels': extracted_data,
        }, f, indent=2, ensure_ascii=False)

    if progress_callback:
        progress_callback(total, total, "Spatial text extraction complete")

    print(f"[TextExtractor] Saved text to {txt_path} and {json_path}")
    return panels


# ═══════════════════════════════════════════════════════════════════
# Magi v2 — Chapter-wide OCR (port from YTV3 pipeline/ocr_extractor.py)
# ═══════════════════════════════════════════════════════════════════

def _magi_from_images(image_paths: list[str], title: str, chapter: str) -> str:
    """Chapter-wide transcription of provided images with Magi v2.

    Returns path to the saved text file.
    """
    try:
        import numpy as np
        import torch
        from PIL import Image
        from transformers import AutoModel
    except ImportError as exc:
        raise RuntimeError(
            f"Magi v2 dependencies missing ({exc}). Install requirements.txt "
            "or set OCR_ENGINE=qwen to use the built-in vision model."
        )

    if not image_paths:
        raise RuntimeError("No images provided for Magi transcription.")

    log.info("Loading Magi v2 (%s) - first run downloads the model ...",
             config.MAGI_MODEL_ID)
    device = _best_device()
    if device == "mps":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    log.info("Magi v2 running on device: %s", device)
    try:
        model = (
            AutoModel.from_pretrained(config.MAGI_MODEL_ID, trust_remote_code=True)
            .to(device)
            .eval()
        )
    except Exception as exc:
        log.warning("Could not load Magi on %s (%s) - retrying on CPU", device, exc)
        device = "cpu"
        model = (
            AutoModel.from_pretrained(config.MAGI_MODEL_ID, trust_remote_code=True)
            .to(device)
            .eval()
        )

    images = []
    for p in image_paths:
        with Image.open(p) as im:
            images.append(np.array(im.convert("L").convert("RGB")))

    log.info("Transcribing %d pages with Magi v2 (%s) ...", len(image_paths), device)
    character_bank = {"images": [], "names": []}
    with torch.no_grad():
        per_page = model.do_chapter_wide_prediction(
            images, character_bank, use_tqdm=True, do_ocr=True
        )

    lines = []
    for img_path, result in zip(image_paths, per_page):
        page_name = os.path.basename(img_path)
        lines.append(f"[page {page_name}]")
        speaker = {
            t: result["character_names"][c]
            for t, c in result.get("text_character_associations", [])
        }
        essential = result.get("is_essential_text")
        for j, text in enumerate(result.get("ocr", [])):
            if essential is not None and j < len(essential) and not essential[j]:
                continue
            name = speaker.get(j, "unknown")
            lines.append(f"<{name}>: {text}")
        lines.append("")

    # Save to extracted_txt/title/chapter.txt
    safe_title = title if title else "Unknown_Series"
    safe_chapter = chapter if chapter else "Unknown_Chapter"
    series_dir = os.path.join(config.EXTRACTED_TXT_DIR, safe_title)
    os.makedirs(series_dir, exist_ok=True)
    text_path = os.path.join(series_dir, f"{safe_chapter}.txt")

    with open(text_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info("Magi v2 transcript written to %s", text_path)
    return text_path


def _assign_magi_text_to_panels(panels: list[dict], text_file: str):
    """Read Magi v2 output and assign text to each panel by page reference."""
    with open(text_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Simple assignment: store the full chapter text as reference for each panel
    # The actual per-panel matching is done in the narrator step with three-input context
    for panel in panels:
        panel['text'] = "[Chapter text extracted]"
        panel['visual_description'] = ""


# ═══════════════════════════════════════════════════════════════════
# Step 1: Vision model — extract text + describe scene
# ═══════════════════════════════════════════════════════════════════

def _extract_text_and_description(image_path: str, vision_model: str,
                                  host: str, engine: str = "qwen") -> tuple[str, str]:
    """
    Call the vision model on a panel image.

    Returns (extracted_text, visual_description).
    """
    if engine == "tesseract":
        return _tesseract_ocr(image_path), ""
    if engine == "manga-ocr":
        return _manga_ocr(image_path), ""

    with open(image_path, 'rb') as f:
        image_b64 = base64.b64encode(f.read()).decode('utf-8')

    prompt = (
        "TASK: Extract ONLY English dialogue and narration from this manhwa panel.\n\n"
        "CRITICAL RULES:\n"
        "✓ ONLY extract text that is in ENGLISH LANGUAGE\n"
        "✗ NEVER extract Korean characters (한글/hangul)\n"
        "✗ NEVER extract Chinese characters (汉字/hanzi)\n"
        "✗ NEVER extract Japanese characters (漢字/kanji, ひらがな/hiragana, カタカナ/katakana)\n"
        "✗ NEVER extract any non-Latin script characters\n\n"
        "INCLUDE:\n"
        "- Text inside speech bubbles (white/colored rounded shapes characters speak through)\n"
        "- Text inside thought bubbles\n"
        "- Text inside narration boxes (rectangular boxes with story narration)\n"
        "- ONLY if the text uses English alphabet (A-Z, a-z)\n\n"
        "EXCLUDE (do NOT extract):\n"
        "- ANY text with Korean/Chinese/Japanese/non-Latin characters\n"
        "- Signs, posters, and labels in the scene (e.g. shop names, door signs)\n"
        "- Sound effects and onomatopoeia (e.g. CRASH, BANG, sfx text)\n"
        "- Curse/swear symbols (e.g. #@$!!, %&?!, ****)\n"
        "- Title cards, chapter headers, timestamps\n"
        "- Background text, watermarks, credits\n\n"
        "FORMAT:\n"
        "- One line per bubble/box, exactly as written\n"
        "- Do NOT add speaker labels or prefixes\n"
        "- If there is no ENGLISH text to extract, output: NONE\n"
        "- Do NOT invent or hallucinate text\n\n"
        "Extract ENGLISH text only now:"
    )

    try:
        resp = requests.post(
            f"{host}/api/generate",
            json=_fast_json({
                "model": vision_model,
                "prompt": prompt,
                "images": [image_b64],
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 400,
                }
            }),
            timeout=config.OLLAMA_TIMEOUT
        )
        resp.raise_for_status()
        raw = resp.json().get('response', '').strip()
        return _parse_vision_response(raw)

    except requests.exceptions.ConnectionError:
        return "[Ollama not running]", ""
    except requests.exceptions.Timeout:
        return "[Timeout]", ""
    except Exception as e:
        return f"[Error: {e}]", ""


# ═══════════════════════════════════════════════════════════════════
# Tesseract / Manga-OCR fallbacks (port from YTV3)
# ═══════════════════════════════════════════════════════════════════

def _tesseract_ocr(image_path: str) -> str:
    import pytesseract
    from PIL import Image
    with Image.open(image_path) as img:
        return pytesseract.image_to_string(img)


def _manga_ocr(image_path: str) -> str:
    from manga_ocr import MangaOcr
    _mocr = MangaOcr()
    return _mocr(image_path)


def _parse_vision_response(raw: str) -> tuple[str, str]:
    """Parse the response from the vision model."""
    text_part = raw
    visual_part = ""

    text_lines = []
    for line in text_part.split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.upper() in ('NONE', 'N/A', 'NO TEXT', 'NO TEXT DETECTED'):
            continue
        skip = ['here is', 'the text', 'i can see', 'visible text',
                'this panel', 'in this image', 'the panel shows']
        if any(line.lower().startswith(s) for s in skip):
            continue
        # Strip speaker prefixes (e.g. "Speaker: TEXT" → "TEXT")
        # The vision model labels everything as "Speaker:" which would be read aloud by TTS
        line = re.sub(r'^(?:Speaker|Narrator|Unknown|Character)\s*[:：]\s*', '', line, flags=re.IGNORECASE)
        if not line:
            continue
        
        # FILTER OUT NON-ENGLISH CHARACTERS
        # Remove any Korean (Hangul) characters: U+AC00 to U+D7AF
        # Remove any Chinese/Japanese (CJK Unified Ideographs): U+4E00 to U+9FFF
        # Remove Japanese Hiragana: U+3040 to U+309F
        # Remove Japanese Katakana: U+30A0 to U+30FF
        line = re.sub(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]+', '', line)
        line = line.strip()
        
        # Skip if line is now empty after filtering
        if not line:
            continue
            
        # Skip lines that are mostly symbols/punctuation (curse symbols, sfx)
        # e.g. "#@$!!", "%&?!", "***", "!!!", "..."
        alpha_chars = sum(1 for c in line if c.isalpha())
        if alpha_chars < 2:
            continue
            
        # Additional check: ensure at least 50% of alphabetic characters are Latin (English)
        latin_chars = sum(1 for c in line if c.isalpha() and ord(c) < 0x0370)  # Latin range
        if alpha_chars > 0 and latin_chars / alpha_chars < 0.5:
            continue
            
        text_lines.append(line)

    return '\n'.join(text_lines), visual_part.strip()


# ═══════════════════════════════════════════════════════════════════
# Model resolution
# ═══════════════════════════════════════════════════════════════════

def _resolve_vision_model(host: str) -> str:
    """Pick the best available vision model (qwen2.5vl > llava > any)."""
    try:
        resp = requests.get(f"{host}/api/tags", timeout=5)
        if resp.status_code == 200:
            models = [m['name'] for m in resp.json().get('models', [])]
            for m in models:
                if 'qwen' in m.lower() and ('vl' in m.lower() or 'vision' in m.lower()):
                    return m
            for m in models:
                if 'llava' in m.lower():
                    return m
            for m in models:
                if any(k in m.lower() for k in ['vision', 'vl', 'visual']):
                    return m
            if models:
                return models[0]
    except Exception:
        pass
    return FALLBACK_MODEL


def _best_device() -> str:
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"
