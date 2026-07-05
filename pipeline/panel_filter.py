"""
AI Panel Filter — qwen2.5vl:3b Content Classifier
====================================================

Uses the vision model to decide whether a cropped panel is:
  ✅ STORY  — actual manhwa content (keep it)
  ❌ SKIP   — promotional / watermark / cover / non-story (discard)

Categories that are automatically SKIPPED:
  - Publisher / scanlation watermark pages  (e.g. "Read at VioletScans.org")
  - Recruitment ads                          ("We are Hiring! Editors, KR Translators...")
  - Chapter cover / title pages              (big logo + author credits, no story)
  - Publisher credits pages                  (DAON, Kakao, Webtoon credits)
  - "End of chapter" / "Next chapter" cards
  - Advertisement banners / promo images
  - Blank / near-blank separator pages

Heuristic fast-pass runs first (milliseconds).
AI call only happens for ambiguous panels the heuristics aren't sure about.
This keeps average processing time low (≈1–2s per ambiguous panel only).
"""
import os
import base64
import json
import re
import cv2
import numpy as np
import requests
from typing import Optional

import config

# ─── Strings that always mean SKIP (case-insensitive substring match) ──────────
_SKIP_KEYWORDS = [
    # Watermarks / scanlation groups
    "violetscans", "violet scans", "read this series", "read only at",
    "manhwascan", "asura scans", "flame scans", "reaperscans",
    "manhuaplus", "webtoonxyz", "asuratoon", "kunmanga",
    "lumoscomic", "toonily", "manganato", "mangakakalot",
    "mangadex", "hivescans", "realm scans", "ixigua",
    "read at", "only at", "for more series",
    # Publisher / credits
    "daon creative", "kakao", "webtoon canvas", "tapas media",
    "lezhin comics", "toptoon", "bomtoon",
    # Recruitment / promotional
    "we are hiring", "join our team", "hiring editors",
    "kr translator", "js translator", "proofreader", "typesetter",
    "wanna girl", "free comic", "promo code", "coupon",
    "how to apply", "recruitment",
    # Chapter markers (non-story)
    "end of chapter", "end of episode", "next episode",
    "thank you for reading", "like and subscribe",
    # Patreon / social
    "patreon.com", "discord.gg", "instagram.com/",
]

# ─── Visual signatures that almost always mean SKIP ─────────────────────────
_SKIP_URL_PATTERN = re.compile(
    r"(https?://|\.org|\.com|\.net|\.io)\b",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def should_skip_panel(
    panel_img: np.ndarray,
    panel_index: int,
    total_panels: int,
    host: str = None,
    use_ai: bool = True,
) -> tuple[bool, str]:
    """
    Decide whether a panel should be kept or skipped.

    Returns (skip: bool, reason: str).

    Runs heuristics first; only calls the AI if heuristics are uncertain.
    """
    host = (host or config.OLLAMA_HOST).rstrip("/")

    # --- Fast heuristic pass (no AI) ---
    skip, reason = _heuristic_filter(panel_img, panel_index, total_panels)
    if skip:
        return True, reason
    if reason == "DEFINITELY_KEEP":
        return False, "story content"

    # --- AI classification for ambiguous panels ---
    if use_ai:
        skip, ai_reason = _ai_classify(panel_img, host)
        return skip, ai_reason

    return False, "heuristic uncertain, AI disabled"


def filter_panels_batch(
    panels: list[np.ndarray],
    panel_start_index: int = 0,
    total_in_chapter: int = None,
    host: str = None,
    use_ai: bool = True,
    progress_callback=None,
) -> tuple[list[np.ndarray], list[dict]]:
    """
    Filter a list of panel images, returning only story panels.

    Returns:
        kept_panels: list of np.ndarray that passed the filter
        filter_log:  list of dicts describing the decision for each panel
    """
    total = total_in_chapter or len(panels)
    kept = []
    log = []

    for i, panel in enumerate(panels):
        abs_idx = panel_start_index + i

        if progress_callback:
            progress_callback(
                i + 1, len(panels),
                f"Classifying panel {i + 1}/{len(panels)}..."
            )

        skip, reason = should_skip_panel(panel, abs_idx, total, host, use_ai)
        log.append({
            "index": abs_idx,
            "shape": f"{panel.shape[1]}x{panel.shape[0]}",
            "decision": "SKIP" if skip else "KEEP",
            "reason": reason,
        })

        if skip:
            print(f"[Filter] SKIP panel {abs_idx + 1} — {reason}")
        else:
            kept.append(panel)

    return kept, log


# ═══════════════════════════════════════════════════════════════════════════════
# Heuristic Filter (fast, no AI)
# ═══════════════════════════════════════════════════════════════════════════════

def _heuristic_filter(
    img: np.ndarray,
    panel_index: int,
    total_panels: int,
) -> tuple[bool, str]:
    """
    Returns (should_skip, reason).
    reason == "UNCERTAIN" means the AI should take over.
    reason == "DEFINITELY_KEEP" short-circuits the AI call.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    total_px = gray.size

    # 1. Near-entirely blank panels (shouldn't reach here, but safety net)
    white_ratio = np.sum(gray > 245) / total_px
    black_ratio = np.sum(gray < 10) / total_px
    if white_ratio + black_ratio > 0.92:
        return True, "blank panel"

    # 2. Very short panels with very low information content
    if h < 150 and np.std(gray) < 20:
        return True, "tiny low-content panel"

    # 3. Suspect banner: very wide/short aspect ratio (typical watermark strip)
    aspect = w / max(h, 1)
    if aspect > 5 and h < 120:
        return True, "horizontal watermark strip"

    # 4. Webtoon cover pages: typically the first 1-3 panels of a chapter,
    #    mostly white/light background with a title logo
    if panel_index < 3 and white_ratio > 0.65 and h > 400:
        # Could be a title page — mark as uncertain for AI
        return False, "UNCERTAIN"

    # 5. Last panels of a chapter are often credits / "end of chapter"
    if panel_index >= total_panels - 3:
        # Mark uncertain for AI
        return False, "UNCERTAIN"

    # 6. Panels in the middle with high white ratio but small size
    #    could be speech-only panels (keep) or promo boxes (skip)
    if white_ratio > 0.75 and h < 600:
        return False, "UNCERTAIN"

    # 7. Dark panels are almost always story content in dark manhwa
    mean_brightness = np.mean(gray)
    if mean_brightness < 120 and np.std(gray) > 25:
        return False, "DEFINITELY_KEEP"

    # 8. Edge-rich panels (lots of linework = story art)
    edges = cv2.Canny(gray.astype(np.uint8), 50, 150)
    edge_density = np.sum(edges > 0) / total_px
    if edge_density > 0.06:
        return False, "DEFINITELY_KEEP"

    return False, "UNCERTAIN"


# ═══════════════════════════════════════════════════════════════════════════════
# AI Classifier (qwen2.5vl:3b)
# ═══════════════════════════════════════════════════════════════════════════════

_CLASSIFY_PROMPT = """\
Look at this image carefully. It is a panel from a manhwa/webtoon chapter.

Answer ONE word only: STORY or SKIP

Answer STORY if it shows:
- Actual characters talking, fighting, thinking, or interacting
- Story scenes: backgrounds with characters, action sequences
- Dialogue-only panels that are part of the narrative

Answer SKIP if it shows:
- Promotional or recruitment advertisements ("We are Hiring", "Join our Discord")
- Website watermarks or scanlation group banners ("Read at VioletScans.org")
- Chapter title / cover page (big logo, title text, author credits, no story)
- Publisher credit pages (Kakao, Webtoon, DAON, Tapas branding)
- "End of chapter" or "Thank you for reading" cards
- Social media promotion (Patreon, Instagram, Discord links)

Answer ONE word only: STORY or SKIP"""


def _ai_classify(img: np.ndarray, host: str) -> tuple[bool, str]:
    """
    Ask qwen2.5vl:3b whether this panel should be kept or skipped.
    Returns (should_skip, reason_string).
    """
    # Downscale for fast inference — 480px wide is plenty for classification
    h, w = img.shape[:2]
    if w > 480:
        scale = 480 / w
        new_w, new_h = 480, int(h * scale)
        # Cap height too — tall stitched panels don't need full res
        if new_h > 960:
            new_h = 960
        thumb = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    else:
        thumb = img

    # Encode to JPEG for the API
    ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        return False, "encode failed"
    image_b64 = base64.b64encode(buf.tobytes()).decode("utf-8")

    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={
                "model": config.VISION_MODEL,  # qwen2.5vl:3b
                "prompt": _CLASSIFY_PROMPT,
                "images": [image_b64],
                "stream": False,
                "options": {
                    "temperature": 0.0,   # deterministic
                    "num_predict": 5,     # just one word: STORY or SKIP
                    "top_p": 1.0,
                },
            },
            timeout=30,  # short timeout — this is just classification
        )
        resp.raise_for_status()
        answer = resp.json().get("response", "").strip().upper()

        # Parse the answer — look for SKIP anywhere in the response
        if "SKIP" in answer:
            # Double-check: run keyword scan on the full text to avoid false negatives
            return True, f"AI classified as non-story ({answer[:30]})"
        else:
            return False, "AI confirmed story content"

    except requests.exceptions.Timeout:
        print("[Filter] AI classify timeout — keeping panel by default")
        return False, "AI timeout, kept"
    except requests.exceptions.ConnectionError:
        print("[Filter] Ollama not reachable — keeping panel by default")
        return False, "Ollama offline, kept"
    except Exception as e:
        print(f"[Filter] AI classify error: {e} — keeping panel")
        return False, f"AI error, kept"


def check_text_for_skip_keywords(text: str) -> Optional[str]:
    """
    Scan extracted text for known skip keywords.
    Called by text_extractor after OCR to catch promo panels that slipped through.
    Returns the matched keyword if skip, else None.
    """
    text_lower = text.lower()
    for kw in _SKIP_KEYWORDS:
        if kw in text_lower:
            return kw
    if _SKIP_URL_PATTERN.search(text):
        return "URL found"
    return None
