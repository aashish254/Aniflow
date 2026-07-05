"""
Configuration settings for Manhwa Auto Recap Maker
Full-featured local pipeline for manhwa/webtoon recap videos.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── ⚡ Speed Mode (runtime toggle, set from the Source page) ──────
# When False (default), every pipeline behaves exactly as it always has.
# When True (user enables "Speed Mode"), the pipeline switches to:
#   - VideoToolbox hardware H.264 encoding (macOS) instead of libx264 medium
#   - Parallel clip encoding workers + clip caching (mtime-based invalidation)
#   - Concurrent TTS (Edge: 6 workers, ElevenLabs: 2; Kokoro: MPS GPU)
#   - Ollama keep_alive so the LLM stays loaded between panels
# Individual steps read this flag at runtime; the toggle is stored per project
# in PipelineState.settings['fast_mode'] and propagated here by the orchestrator.
SPEED_MODE = False

# ⚡ Speed Mode tunables (resolved from state.settings by the orchestrator).
# Keys: clip_workers, encoder, edge_workers, elevenlabs_workers, kokoro_gpu
SPEED_SETTINGS = {}

# ─── Top-level organisation ──────────────────────────────────────
INPUT_DIR  = os.path.join(BASE_DIR, "input")   # raw source images
OUTPUT_DIR = os.path.join(BASE_DIR, "output")  # all generated artefacts
MODELS_DIR = os.path.join(BASE_DIR, "models")  # YOLO / ML model files
TEMP_DIR   = os.path.join(BASE_DIR, "temp")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
CAST_DIR   = os.path.join(BASE_DIR, "casts")
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")  # legacy (kept for compat)

# ─── New Organized Folder Structure ──────────────────────────────
# Each pipeline stage has its own top-level folder for clean organization
MANHWA_DOWNLOAD_DIR = os.path.join(BASE_DIR, "manhwa_download")  # Raw downloaded images
MANHWA_STITCH_DIR   = os.path.join(BASE_DIR, "manhwa_stitch")    # Stitched full pages
MANHWA_DETECT_DIR   = os.path.join(BASE_DIR, "manhwa_detect")    # Detection metadata
MANHWA_CROP_DIR     = os.path.join(BASE_DIR, "manhwa_crop")      # Cropped panels
MANHWA_TEXT_DIR     = os.path.join(BASE_DIR, "manhwa_text")      # Extracted text
MANHWA_AUDIO_DIR    = os.path.join(BASE_DIR, "manhwa_audio")     # Generated audio
MANHWA_CLIPS_DIR    = os.path.join(BASE_DIR, "manhwa_clips")     # Final video clips

# Legacy folders (kept for backward compatibility)
RAW_MANHWA_DIR = os.path.join(INPUT_DIR, "raw_manhwa")
MANHWA_PANEL_DIR             = os.path.join(OUTPUT_DIR, "manhwa_panel")
MANHWA_PANEL_WITH_BUBBLE_DIR = os.path.join(OUTPUT_DIR, "Manhwa_panel_with_bubble")
PANEL_BUBBLE_DIR             = os.path.join(OUTPUT_DIR, "panel_bubble")
EXTRACTED_TXT_DIR            = os.path.join(OUTPUT_DIR, "extracted_txt")
NARRATION_DIR                = os.path.join(OUTPUT_DIR, "narration")
AUDIO_OUTPUT_DIR             = os.path.join(OUTPUT_DIR, "audio")
VIDEO_OUTPUT_DIR             = os.path.join(OUTPUT_DIR, "video")

# Ensure directories exist
for d in [INPUT_DIR, OUTPUT_DIR, MODELS_DIR, TEMP_DIR, ASSETS_DIR, CAST_DIR,
          PROJECTS_DIR,
          # New organized folders
          MANHWA_DOWNLOAD_DIR, MANHWA_STITCH_DIR, MANHWA_DETECT_DIR,
          MANHWA_CROP_DIR, MANHWA_TEXT_DIR, MANHWA_AUDIO_DIR, MANHWA_CLIPS_DIR,
          # Legacy folders
          RAW_MANHWA_DIR,
          MANHWA_PANEL_DIR, MANHWA_PANEL_WITH_BUBBLE_DIR,
          PANEL_BUBBLE_DIR, EXTRACTED_TXT_DIR, NARRATION_DIR,
          AUDIO_OUTPUT_DIR, VIDEO_OUTPUT_DIR]:
    os.makedirs(d, exist_ok=True)

# Sub-asset directories
BGM_DIR = os.path.join(ASSETS_DIR, "bgm")
WATERMARK_DIR = os.path.join(ASSETS_DIR, "watermarks")
BACKGROUNDS_DIR = os.path.join(ASSETS_DIR, "backgrounds")
INTROS_DIR = os.path.join(ASSETS_DIR, "intros")
OUTROS_DIR = os.path.join(ASSETS_DIR, "outros")

for d in [BGM_DIR, WATERMARK_DIR, BACKGROUNDS_DIR, INTROS_DIR, OUTROS_DIR]:
    os.makedirs(d, exist_ok=True)

# ─── YOLO Panel Detection ─────────────────────────────────────────
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH",
    os.path.join(MODELS_DIR, "manhwa_panel_cropped.pt"))
PANEL_WITH_BUBBLE_MODEL_PATH = os.getenv("PANEL_WITH_BUBBLE_MODEL_PATH",
    os.path.join(MODELS_DIR, "panel_with_bubble_text.pt"))
PANEL_CONF_THRESHOLD = float(os.getenv("PANEL_CONF_THRESHOLD", "0.25"))
PANEL_IMG_SIZE = int(os.getenv("PANEL_IMG_SIZE", "1024"))
MIN_PANEL_SIDE = int(os.getenv("MIN_PANEL_SIDE", "60"))
DEFAULT_PANEL_EXTRACTION_METHOD = "yolo"

# ─── Magi v2 OCR (port from YTV3) ─────────────────────────────────
OCR_ENGINE = os.getenv("OCR_ENGINE", "qwen")  # "qwen" | "magi" | "tesseract" | "manga-ocr"
MAGI_MODEL_ID = os.getenv("MAGI_MODEL_ID",
    os.path.join(BASE_DIR, "magiv2"))
DEFAULT_TEXT_EXTRACTION_METHOD = "magi"

# ─── Ollama Settings ──────────────────────────────────────────────
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5vl:7b"               # Legacy fallback
VISION_MODEL  = "qwen2.5vl:3b"       # Step 1 — The Eyes (text extraction + scene description)
WRITER_MODEL  = "qwen2.5vl:3b" # Step 2 — The Writer (dramatic narration) [legacy, used when DECOUPLED_NARRATION=False]
TEXT_WRITER_MODEL = "qwen2.5:14b"  # Step 2b — Pure-text storyteller (decoupled pipeline)
DECOUPLED_NARRATION = True         # True = Vision Parser → Text Storyteller (recommended)
OLLAMA_TIMEOUT = 180  # seconds per panel
LLM_IMAGE_MAX_SIDE = int(os.getenv("LLM_IMAGE_MAX_SIDE", "1400"))

# ─── Narration Prompt ─────────────────────────────────────────────
NARRATION_SYSTEM_PROMPT = """You are a professional manhwa/webtoon narrator creating dramatic recap narrations.
Your narration style is:
- Third-person dramatic narration (like a YouTube recap video)
- Present tense for immediacy
- Concise but vivid (2-4 sentences per panel)
- Focus on action, emotion, and tension
- Never mention "panel", "page", "manga", or "manhwa" - narrate as if telling a story
- Don't describe art style or quality - focus on the story
- Use dramatic language to build engagement

Example: "The boy's eyes widen in shock as the shadow looms behind him. Before he can react, a cold blade presses against his neck. His heart races — this was supposed to be a safe zone."
"""

NARRATION_USER_PROMPT = """Analyze this manhwa panel and write a dramatic narration (2-4 sentences).
Focus on: what's happening, character emotions, and any tension or action.
If there are speech bubbles, incorporate the dialogue naturally into the narration.
Write ONLY the narration text, nothing else."""

# ─── TTS Settings ────────────────────────────────────────────────
TTS_ENGINE = "edge"  # "edge", "kokoro", or "elevenlabs"
TTS_VOICE = "en-US-ChristopherNeural"  # Deep male narrator voice
TTS_RATE = "-5%"  # Slightly slower for drama
TTS_PITCH = "-2Hz"

# Kokoro TTS settings
KOKORO_MODEL_DIR = os.path.join(BASE_DIR, "models", "kokoro")
KOKORO_VOICE = "af_bella"  # Default Kokoro voice
KOKORO_SPEED = 1.0

# ElevenLabs TTS settings
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")  # Set via environment or UI
ELEVENLABS_MODEL = "eleven_monolingual_v1"  # or "eleven_multilingual_v2"
ELEVENLABS_VOICE_ID = ""  # Will be set by user in UI
ELEVENLABS_STABILITY = 0.5  # 0.0-1.0, lower = more variable
ELEVENLABS_SIMILARITY = 0.75  # 0.0-1.0, higher = closer to original voice

# Available narrator voices (good for recaps):
# en-US-ChristopherNeural - Deep male (recommended)
# en-US-GuyNeural - Casual male
# en-US-DavisNeural - Authoritative male
# en-US-JennyNeural - Clear female
# en-GB-RyanNeural - British male
# en-AU-WilliamNeural - Australian male

# ─── Video Composition ────────────────────────────────────────────
# Resolution presets
RESOLUTION_PRESETS = {
    # Portrait (9:16) - for shorts/reels
    "1080p": {"width": 1080, "height": 1920},
    "2k":    {"width": 1440, "height": 2560},
    "4k":    {"width": 2160, "height": 3840},
    # Landscape (16:9) - for YouTube videos
    "1080p_landscape": {"width": 1920, "height": 1080},
    "2k_landscape":    {"width": 2560, "height": 1440},
    "4k_landscape":    {"width": 3840, "height": 2160},
}

VIDEO_RESOLUTION = "1080p_landscape"  # Changed to landscape 16:9 by default
VIDEO_WIDTH = 1920  # 16:9 landscape for YouTube
VIDEO_HEIGHT = 1080
VIDEO_FPS = 30
VIDEO_QUALITY = "medium"  # low, medium, high

# Ken Burns effect settings
KEN_BURNS_ZOOM_RANGE = (1.0, 1.15)  # zoom from 100% to 115%
PAN_SPEED = 0.02  # pan speed factor

# Scene animation types
SCENE_ANIMATIONS = [
    "zoom_in", "zoom_out", "pan_left", "pan_right",
    "pan_up", "pan_down", "fade_in", "slide_left",
    "slide_right", "slide_up", "ken_burns", "parallax"
]

# Padding between panels (seconds)
PANEL_PADDING = 0.3  # silence between panels
INTRO_DURATION = 1.5  # black screen intro
OUTRO_DURATION = 2.0  # black screen outro

# Background settings
BACKGROUND_MODE = "blur"  # "blur", "color", "blur_color", "custom"
BACKGROUND_BLUR_RADIUS = 30
BACKGROUND_COLOR = (10, 10, 15)
BACKGROUND_COLOR_HEX = "#000000"  # For color and blur_color modes
COLOR_OVERLAY_OPACITY = 0.5  # For blur_color mode (0.0-1.0)

# BGM settings
BGM_VOLUME = 0.15  # Background music volume (0-1)
BGM_FADE_IN = 2.0  # seconds
BGM_FADE_OUT = 3.0

# Watermark
WATERMARK_OPACITY = 0.3
WATERMARK_POSITION = "bottom_right"  # top_left, top_right, bottom_left, bottom_right, center
WATERMARK_SCALE = 0.15  # relative to video width

# ─── Panel Cropper Settings ──────────────────────────────────────
MIN_PANEL_HEIGHT = 100  # Minimum panel height in pixels
MAX_PANEL_HEIGHT = 3000  # Maximum panel height
GAP_THRESHOLD = 15  # Min white gap between panels (pixels)
BORDER_COLOR_THRESHOLD = 240  # Consider pixels above this as "white/gap"
MIN_PANEL_ASPECT_RATIO = 0.2  # width/height minimum

# ─── Auto Pipeline Settings ──────────────────────────────────────
AUTO_PIPELINE_EXPORT_CLIPS = True   # Export per-panel clips in auto mode
AUTO_PIPELINE_SILENT_DURATION = 2.0 # Duration (seconds) for panels with no dialogue

# ─── CapCut Export Settings ──────────────────────────────────────
CAPCUT_SUBTITLE_FONT = "Inter"
CAPCUT_SUBTITLE_SIZE = 40
CAPCUT_SUBTITLE_COLOR = "#FFFFFF"
CAPCUT_SUBTITLE_BG = "rgba(0,0,0,0.6)"
CAPCUT_IN_ANIMATION = "fade"  # fade, slide_up, zoom, bounce
CAPCUT_OUT_ANIMATION = "fade"

# ─── Script Rewriter ─────────────────────────────────────────────
REWRITER_STYLES = {
    "dramatic": "Rewrite with more dramatic tension and urgency. Use vivid action words.",
    "casual": "Rewrite in a casual, conversational YouTube narrator style.",
    "epic": "Rewrite like an epic fantasy novel narration with grand language.",
    "horror": "Rewrite with a dark, ominous horror tone. Build dread and suspense.",
    "comedy": "Rewrite with humor and wit. Add funny observations.",
    "hype": "Rewrite like an excited anime reactor. High energy and hype.",
}


# ─── Path Helper Functions ───────────────────────────────────────

def get_manhwa_paths(manhwa_name: str, chapter_num: int) -> dict:
    """
    Get all paths for a manhwa chapter in the new organized structure.
    
    Args:
        manhwa_name: Name of the manhwa (e.g., "Sword-Devouring-Swordmaster")
        chapter_num: Chapter number (e.g., 1, 2, 3...)
    
    Returns:
        Dictionary with all paths for this chapter:
        {
            'download': 'manhwa_download/Sword-Devouring-Swordmaster/Chapter_00001/',
            'stitch': 'manhwa_stitch/Sword-Devouring-Swordmaster/Chapter_00001/',
            'detect': 'manhwa_detect/Sword-Devouring-Swordmaster/Chapter_00001/',
            'crop': 'manhwa_crop/Sword-Devouring-Swordmaster/Chapter_00001/',
            'text': 'manhwa_text/Sword-Devouring-Swordmaster/Chapter_00001/',
            'audio': 'manhwa_audio/Sword-Devouring-Swordmaster/Chapter_00001/',
            'clips': 'manhwa_clips/Sword-Devouring-Swordmaster/Chapter_00001/',
            'state': 'manhwa_download/Sword-Devouring-Swordmaster/Chapter_00001/state.json'
        }
    """
    chapter_folder = f"Chapter_{chapter_num:05d}"  # 5 digits for 1000+ chapter support
    
    paths = {
        'download': os.path.join(MANHWA_DOWNLOAD_DIR, manhwa_name, chapter_folder),
        'stitch': os.path.join(MANHWA_STITCH_DIR, manhwa_name, chapter_folder),
        'detect': os.path.join(MANHWA_DETECT_DIR, manhwa_name, chapter_folder),
        'crop': os.path.join(MANHWA_CROP_DIR, manhwa_name, chapter_folder),
        'text': os.path.join(MANHWA_TEXT_DIR, manhwa_name, chapter_folder),
        'audio': os.path.join(MANHWA_AUDIO_DIR, manhwa_name, chapter_folder),
        'clips': os.path.join(MANHWA_CLIPS_DIR, manhwa_name, chapter_folder),
    }
    
    # Create all directories
    for path in paths.values():
        os.makedirs(path, exist_ok=True)
    
    # Add state file path (stored in download folder)
    paths['state'] = os.path.join(paths['download'], 'state.json')
    
    return paths


def get_batch_project_name(manhwa_name: str, start_chapter: int, end_chapter: int) -> str:
    """
    Generate a project name for batch processing.
    
    Examples:
        get_batch_project_name("Sword-Devouring-Swordmaster", 1, 1) 
        → "Sword-Devouring-Swordmaster__Chapter_00001"
        
        get_batch_project_name("Sword-Devouring-Swordmaster", 1, 5) 
        → "Sword-Devouring-Swordmaster__Chapters_00001-00005"
        
        get_batch_project_name("One Piece", 1, 1500)
        → "One-Piece__Chapters_00001-01500"
    """
    if start_chapter == end_chapter:
        return f"{manhwa_name}__Chapter_{start_chapter:05d}"
    else:
        return f"{manhwa_name}__Chapters_{start_chapter:05d}-{end_chapter:05d}"


def sanitize_manhwa_name(name: str) -> str:
    """
    Sanitize manhwa name for use in folder paths.
    
    Examples:
        "Sword-Devouring Swordmaster" → "Sword-Devouring-Swordmaster"
        "Urban: System" → "Urban-System"
        "The 100% Hero!" → "The-100-Hero"
    """
    import re
    # Replace spaces and special chars with hyphens
    sanitized = re.sub(r'[^\w\s-]', '', name)
    sanitized = re.sub(r'[-\s]+', '-', sanitized)
    return sanitized.strip('-')


# ─── Chapter Paths Helper (legacy - kept for backward compatibility) ───

def chapter_paths(series: str, chapter: str) -> dict:
    """
    Legacy function - redirects to new organized structure.
    
    Folder structure matches the user's plan:
      manhwa_download/<series>/<chapter>/001.img, 002.img ...
      manhwa_crop/<series>/<chapter>/panel_001.png ...
      manhwa_text/<series>/<chapter>/extracted_text.json
      manhwa_audio/<series>/<chapter>/panel_001.mp3 ...
      manhwa_clips/<series>/<chapter>/scene_001.mp4 ...
    """
    # Parse chapter number from string (e.g., "Chapter_001" -> 1)
    import re
    match = re.search(r'(\d+)', chapter)
    chapter_num = int(match.group(1)) if match else 1
    
    return get_manhwa_paths(series, chapter_num)
