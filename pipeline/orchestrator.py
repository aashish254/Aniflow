"""
Pipeline Orchestrator - Coordinates the full recap generation pipeline.
Enhanced with multi-resolution, cast system, script rewriting, CapCut export.

Pipeline stages:
  init → loaded → stitched → detected1 → cropped1 → stitched2 →
  detected2 → cropped2 → text_extracted → text_removed → narrated →
  audio_done → video_done

Smart Resume: every step checks whether its outputs already exist on disk.
If they do (and boxes haven't changed), the step is skipped instantly.
"""
import hashlib, os, json, time, shutil
from datetime import datetime
import config
from pipeline.downloader import load_from_folder, validate_images
from pipeline.stitcher import stitch_images, export_dataset_stitched
from pipeline.cropper import crop_panels
from pipeline.panel_extractor_yolo import YoloPanelExtractor
from pipeline.text_extractor import extract_text_all_panels
from pipeline.text_remover import remove_text_boxes
from pipeline.narrator import Narrator
from pipeline.tts_engine import generate_all_audio
from pipeline.compositor import compose_video, get_video_info
from pipeline.script_rewriter import ScriptRewriter
from pipeline.capcut_director import CapCutDirector
from pipeline.cast_system import Cast, export_project_cast, list_casts


def _resolve_existing_project_name(project_name: str) -> str:
    """
    Map a display project name onto the manhwa folder that actually exists on
    disk. The wizard/batch accept free-typed names ("Nan Hao Shang Feng") while
    download folders are hyphenated ("Nan-Hao-Shang-Feng"); without this
    resolution PipelineState would silently create a phantom empty project and
    every step/batch would run against nothing.

    Order:
      1. exact folder match                → use as-is
      2. sanitized (hyphens) folder exists → use sanitized
      3. case/punctuation-insensitive hit  → use the real folder name
      4. no match                          → sanitized name (new project)
    """
    if not project_name or "__Chapter" in project_name:
        return project_name
    import re as _re
    root = config.MANHWA_DOWNLOAD_DIR
    try:
        if os.path.isdir(os.path.join(root, project_name)):
            return project_name
    except OSError:
        return project_name
    sanitized = config.sanitize_manhwa_name(project_name)
    if sanitized and os.path.isdir(os.path.join(root, sanitized)):
        return sanitized
    norm = _re.sub(r'[^a-z0-9]', '', (project_name or '').lower())
    if norm and os.path.isdir(root):
        for name in os.listdir(root):
            if _re.sub(r'[^a-z0-9]', '', name.lower()) == norm:
                return name
    return sanitized or project_name


class PipelineState:
    """
    State manager for batch manhwa chapter processing.
    Supports both single chapter and multi-chapter batch operations.
    
    New organized folder structure:
      manhwa_download/{manhwa_name}/Chapter_{num:05d}/
      manhwa_stitch/{manhwa_name}/Chapter_{num:05d}/
      manhwa_detect/{manhwa_name}/Chapter_{num:05d}/
      manhwa_crop/{manhwa_name}/Chapter_{num:05d}/
      manhwa_text/{manhwa_name}/Chapter_{num:05d}/
      manhwa_audio/{manhwa_name}/Chapter_{num:05d}/
      manhwa_clips/{manhwa_name}/Chapter_{num:05d}/
    """
    
    STAGES = ['init', 'loaded', 'stitched', 'detected1', 'cropped1', 'stitched2',
              'detected2', 'cropped2', 'text_extracted',
              'text_removed', 'narrated', 'audio_done', 'video_done', 'clips_done']

    def __init__(self, project_name):
        # ✅ NAME RESOLUTION: map free-typed names onto the real download
        # folder (spaces vs hyphens, case) so steps never run against a
        # phantom empty project.
        project_name = _resolve_existing_project_name(project_name)
        self.project_name = project_name
        
        # Parse project name: "Manhwa-Name__Chapter_00001" or "Manhwa-Name__Chapters_00001-00005"
        if "__Chapter" in project_name:
            parts = project_name.split("__")
            self.manhwa_name = parts[0]
            chapter_part = parts[1]
            
            if chapter_part.startswith("Chapters_"):
                # Batch mode: "Chapters_00001-00005"
                range_str = chapter_part.replace("Chapters_", "")
                start, end = range_str.split("-")
                self.start_chapter = int(start)
                self.end_chapter = int(end)
                self.current_chapter = self.start_chapter
                self.is_batch = True
                self.chapter_list = list(range(self.start_chapter, self.end_chapter + 1))
            else:
                # Single chapter: "Chapter_00001"
                chapter_num = int(chapter_part.replace("Chapter_", ""))
                self.start_chapter = chapter_num
                self.end_chapter = chapter_num
                self.current_chapter = chapter_num
                self.is_batch = False
                self.chapter_list = [chapter_num]
        else:
            # No explicit chapter format in project name
            # Try to extract from source_folder path if available
            # Expected format: .../manhwa_download/{ManhwaName}/Chapter_XXXXX/
            self.manhwa_name = project_name  # Use project name as manhwa title
            
            # Will be updated when source_folder is set via set_source()
            self.current_chapter = 0
            self.start_chapter = 0
            self.end_chapter = 0
            self.is_batch = False
            self.chapter_list = [0]
        
        # Get paths for current chapter using new organized structure
        self.paths = config.get_manhwa_paths(self.manhwa_name, self.current_chapter)
        
        # Primary directories (new structure)
        self.download_dir = self.paths['download']
        self.stitch_dir = self.paths['stitch']
        self.detect_dir = self.paths['detect']
        self.crop_dir = self.paths['crop']
        self.text_dir = self.paths['text']
        self.audio_dir = self.paths['audio']
        self.clips_dir = self.paths['clips']
        self.state_file = self.paths['state']
        
        # Legacy compatibility paths
        self.project_dir = self.crop_dir  # Points to manhwa_crop/...
        self.raw_dir = self.download_dir  # Points to manhwa_download/...
        self.stitched_dir = self.stitch_dir
        
        # Per-chapter state
        self.stage = 'init'
        self.raw_pages = []
        self.stitched_parts = []
        self.source_folder = None
        self.panels = []
        self.stitched_boxes = []
        self.stitched_boxes_1 = []
        self.stitched_boxes_2 = []
        self.stitched_parts_2 = []
        self.panels_with_text = []
        self.panels_mapping = {}
        self.created_at = datetime.now().isoformat()
        self.updated_at = self.created_at
        self.error = None
        self.video_path = None
        self.video_clips = []  # List of generated clip metadata
        self.boxes1_cropped_hash = ""
        self.boxes2_cropped_hash = ""
        
        # Batch processing state
        self.batch_progress = {}  # {chapter_num: {'stage': 'done', 'error': None}}
        
        self.settings = {
            'tts_voice': config.TTS_VOICE,
            'tts_rate': config.TTS_RATE,
            'tts_pitch': config.TTS_PITCH,
            'tts_engine': config.TTS_ENGINE,
            'tts_speed': config.KOKORO_SPEED,
            'ollama_model': config.OLLAMA_MODEL,
            'video_quality': config.VIDEO_QUALITY,
            'video_resolution': config.VIDEO_RESOLUTION,
            'video_fps': config.VIDEO_FPS,
            'bg_mode': config.BACKGROUND_MODE,
            'bgm_path': '',
            'bgm_volume': config.BGM_VOLUME,
            'watermark_path': '',
            'custom_bg_path': '',
            'intro_path': '',
            'outro_path': '',
            'export_clips': False,
            'animation_style': 'auto',
            'cast_file': '',
            'panel_method': getattr(config, 'DEFAULT_PANEL_EXTRACTION_METHOD', 'yolo'),
            'ocr_engine': getattr(config, 'DEFAULT_TEXT_EXTRACTION_METHOD', 'magi'),
        }
        
        # Create directories
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.stitch_dir, exist_ok=True)
        os.makedirs(self.detect_dir, exist_ok=True)
        os.makedirs(self.crop_dir, exist_ok=True)
        os.makedirs(self.text_dir, exist_ok=True)
        os.makedirs(self.audio_dir, exist_ok=True)
        os.makedirs(self.clips_dir, exist_ok=True)
    
    def set_source(self, source_folder: str):
        """Set source folder and auto-detect manhwa name, chapter number, and batch mode."""
        self.source_folder = source_folder
        
        # Try to extract chapter info from path
        # Expected: .../manhwa_download/{ManhwaName}/Chapter_XXXXX/
        import re
        from pathlib import Path
        
        path = Path(source_folder)
        
        # Look for Chapter_XXXXX pattern in path
        chapter_match = re.search(r'Chapter_(\d+)', str(path))
        if chapter_match:
            detected_chapter = int(chapter_match.group(1))
            self.current_chapter = detected_chapter
            self.start_chapter = detected_chapter
            self.end_chapter = detected_chapter
            self.chapter_list = [detected_chapter]
            
            # Try to find manhwa name (parent of Chapter_XXX folder)
            if 'Chapter_' in path.name:
                # path is the Chapter folder itself
                manhwa_folder = path.parent
            else:
                # path might be inside Chapter folder
                for parent in path.parents:
                    if 'Chapter_' in parent.name:
                        manhwa_folder = parent.parent
                        break
                else:
                    manhwa_folder = path.parent
            
            # Extract clean manhwa name from folder
            detected_name = manhwa_folder.name
            if detected_name and detected_name != 'manhwa_download':
                # Clean up the name (remove invalid chars)
                self.manhwa_name = config.sanitize_manhwa_name(detected_name)
            
            # Check for batch mode: are there multiple Chapter_XXXXX folders?
            # ✅ BATCH FIX: only count folders that actually contain images.
            try:
                _IMG_EXTS_SRC = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".img"}
                chapter_folders = sorted([
                    d for d in manhwa_folder.iterdir()
                    if d.is_dir() and re.match(r'Chapter_\d{5}', d.name)
                    and any(p.suffix.lower() in _IMG_EXTS_SRC for p in d.iterdir() if p.is_file())
                ])
                print(f"[PipelineState] Found {len(chapter_folders)} chapter folders in {manhwa_folder}")
                
                if len(chapter_folders) > 1:
                    self.is_batch = True
                    self.chapter_list = [
                        int(re.search(r'Chapter_(\d+)', d.name).group(1))
                        for d in chapter_folders
                    ]
                    self.start_chapter = min(self.chapter_list)
                    self.end_chapter = max(self.chapter_list)
                    print(f"[PipelineState] ✅ BATCH MODE ENABLED: {len(self.chapter_list)} chapters detected")
                    print(f"[PipelineState] Chapter list: {self.chapter_list}")
                    
                    # DEBUG: Verify batch state is set
                    print(f"[DEBUG] After batch detection: is_batch={self.is_batch}, chapter_list={self.chapter_list}")
                else:
                    print(f"[PipelineState] Single chapter mode (only 1 folder found)")
            except Exception as e:
                print(f"[PipelineState] ⚠️  Could not detect batch mode: {e}")
                import traceback
                traceback.print_exc()
                pass  # Stick with single chapter mode
        else:
            # No Chapter_ pattern in path - might be legacy project
            # Keep existing manhwa_name and chapter from initialization
            print(f"[PipelineState] No Chapter_ pattern found in path: {source_folder}")
            print(f"[PipelineState] Using manhwa_name='{self.manhwa_name}', chapter={self.current_chapter}")
        
        # Refresh paths with detected chapter
        self.paths = config.get_manhwa_paths(self.manhwa_name, self.current_chapter)
        self._update_paths()
        
        # Save the updated batch state
        self.save()
    
    def _update_paths(self):
        """Update all directory paths based on current manhwa name and chapter."""
        self.download_dir = self.paths['download']
        self.stitch_dir = self.paths['stitch']
        self.detect_dir = self.paths['detect']
        self.crop_dir = self.paths['crop']
        self.text_dir = self.paths['text']
        self.audio_dir = self.paths['audio']
        self.clips_dir = self.paths['clips']
        self.state_file = self.paths['state']
        
        # Legacy compatibility
        self.project_dir = self.crop_dir
        self.raw_dir = self.download_dir
        self.stitched_dir = self.stitch_dir
        
        # Create directories if they don't exist
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.stitch_dir, exist_ok=True)
        os.makedirs(self.detect_dir, exist_ok=True)
        os.makedirs(self.crop_dir, exist_ok=True)
        os.makedirs(self.text_dir, exist_ok=True)
        os.makedirs(self.audio_dir, exist_ok=True)
        os.makedirs(self.clips_dir, exist_ok=True)
    
    def switch_to_chapter(self, chapter_num: int):
        """Switch context to a different chapter in batch processing."""
        if chapter_num not in self.chapter_list:
            raise ValueError(f"Chapter {chapter_num} not in batch range {self.chapter_list}")

        # ✅ BATCH FIX: Do NOT save() here. Saving would push this chapter's
        # metadata into other chapters' files mid-switch, and the caller is
        # responsible for saving before/after switching anyway.

        # Keep batch identity stable — _load_chapter_data() overwrites these
        # from the target file, which may be stale or missing.
        batch_identity = {
            'is_batch': self.is_batch,
            'chapter_list': list(self.chapter_list),
            'manhwa_name': self.manhwa_name,
            'project_name': self.project_name,
        }

        # Switch to new chapter
        self.current_chapter = chapter_num
        self.paths = config.get_manhwa_paths(self.manhwa_name, chapter_num)
        
        # Update directories
        self.download_dir = self.paths['download']
        self.stitch_dir = self.paths['stitch']
        self.detect_dir = self.paths['detect']
        self.crop_dir = self.paths['crop']
        self.text_dir = self.paths['text']
        self.audio_dir = self.paths['audio']
        self.clips_dir = self.paths['clips']
        self.state_file = self.paths['state']
        
        # Update legacy paths
        self.project_dir = self.crop_dir
        self.raw_dir = self.download_dir
        self.stitched_dir = self.stitch_dir
        
        # Load chapter-specific state
        if os.path.exists(self.state_file):
            data = self._read_state_json(self.state_file)
            if data is not None:
                self._load_chapter_data(data)
                # ✅ SETTINGS FIX: load THIS chapter's saved settings too —
                # otherwise the previous chapter's in-memory settings (e.g. the
                # narration_prompt_version / last_narration_backend stamps)
                # bleed into this chapter's processing and wrongly skip work.
                saved_settings = data.get('settings', {})
                if saved_settings:
                    self.settings.update(saved_settings)
            else:
                print(f"[switch_to_chapter] ⚠️ Unreadable state file for chapter "
                      f"{chapter_num} — starting fresh")
                self.stage = 'init'
                self.stitched_parts = []
                self.panels = []
                self.panels_with_text = []
                self.error = None
                self.raw_pages = []
        else:
            # Reset to init for new chapter
            self.stage = 'init'
            self.stitched_parts = []
            self.panels = []
            self.panels_with_text = []
            self.error = None

            # ✅ FIX: Scan download directory for images if state file is missing
            if os.path.isdir(self.download_dir):
                _IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".img"}
                self.raw_pages = sorted([
                    os.path.join(self.download_dir, f)
                    for f in os.listdir(self.download_dir)
                    if os.path.splitext(f.lower())[1] in _IMG_EXTS
                ])
                if self.raw_pages:
                    self.stage = 'loaded'  # Auto-set stage to 'loaded' if images found
                print(f"[switch_to_chapter] Chapter {self.current_chapter}: Found {len(self.raw_pages)} images on disk")
            else:
                self.raw_pages = []

        # ✅ BATCH FIX: Restore batch identity that the chapter file may have clobbered
        self.is_batch = batch_identity['is_batch']
        self.chapter_list = batch_identity['chapter_list']
        self.manhwa_name = batch_identity['manhwa_name']
        self.project_name = batch_identity['project_name']
        if self.is_batch and len(self.chapter_list) > 1:
            self.start_chapter = min(self.chapter_list)
            self.end_chapter = max(self.chapter_list)

        # ✅ BATCH FIX: Self-heal artifacts (repairs state files clobbered by the
        # old save-propagation bug — e.g. stitched_parts pointing at another chapter)
        self.refresh_chapter_artifacts()

    def refresh_chapter_artifacts(self) -> bool:
        """
        BATCH SELF-HEAL: verify that this chapter's per-chapter artifacts
        (raw_pages, stitched_parts, stitched_parts_2) actually belong to THIS
        chapter and exist on disk. Repairs state files that were clobbered by
        the old save-propagation bug, using the per-chapter folders on disk
        (which were always written correctly).

        Returns True if anything was repaired.
        """
        if not self.is_batch:
            return False

        import glob as _glob
        import re as _re

        def natural_sort_key(s):
            return [int(t) if t.isdigit() else t.lower() for t in _re.split(r'(\d+)', s)]

        ch_tag = f"Chapter_{self.current_chapter:05d}"
        changed = False

        # ── 1. raw_pages: rescan if empty or pointing at another chapter ──
        def _foreign(paths):
            return any(p and ch_tag not in p for p in paths)

        if not self.raw_pages or _foreign(self.raw_pages):
            if os.path.isdir(self.download_dir):
                _IMG_EXTS = {".img", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
                self.raw_pages = sorted([
                    os.path.join(self.download_dir, f) for f in os.listdir(self.download_dir)
                    if os.path.splitext(f.lower())[1] in _IMG_EXTS
                ], key=lambda x: natural_sort_key(os.path.basename(x)))
            else:
                self.raw_pages = []
            changed = True
            print(f"[refresh_chapter_artifacts] Chapter {ch_tag}: raw_pages re-scanned "
                  f"({len(self.raw_pages)} images)")

        # ── 2. stitched_parts: re-glob if empty, foreign, or files missing ──
        parts_bad = (not self.stitched_parts or _foreign(self.stitched_parts) or
                     not all(os.path.exists(p) for p in self.stitched_parts))
        if parts_bad:
            reglob = []
            for pattern in ("stitched_part*.jpg", "stitched_dataset.jpg", "stitched_full.*"):
                reglob = sorted(_glob.glob(os.path.join(self.stitch_dir, pattern)), key=natural_sort_key)
                if reglob:
                    break
            was_foreign = bool(self.stitched_parts) and _foreign(self.stitched_parts)
            if reglob or self.stitched_parts:
                self.stitched_parts = reglob
                changed = True
                print(f"[refresh_chapter_artifacts] Chapter {ch_tag}: stitched_parts "
                      f"re-globbed → {len(reglob)} parts (was foreign={was_foreign})")

            # ── 3. If stitched_parts belonged to ANOTHER chapter, the whole saved
            #      blob (boxes, panels) is foreign too — clear it and re-infer stage.
            if was_foreign:
                self.stitched_boxes = []
                self.stitched_boxes_1 = []
                self.stitched_boxes_2 = []
                self.panels = []
                self.panels_with_text = []
                self.panels_mapping = {}
                self.stitched_parts_2 = []
                self.error = None
                disk_stage = self._infer_stage_from_disk()
                print(f"[refresh_chapter_artifacts] Chapter {ch_tag}: cleared foreign "
                      f"boxes/panels (stage {self.stage!r} → {disk_stage!r} from disk)")
                self.stage = disk_stage

        # ── 4. stitched_parts_2: re-glob from {crop_dir}/stitch2/ if foreign/missing ──
        if self.stitched_parts_2 and (_foreign(self.stitched_parts_2) or
                                      not all(os.path.exists(p) for p in self.stitched_parts_2)):
            reglob2 = sorted(_glob.glob(os.path.join(self.crop_dir, "stitch2", "stitch2*.jpg")),
                             key=natural_sort_key)
            self.stitched_parts_2 = reglob2
            changed = True
            print(f"[refresh_chapter_artifacts] Chapter {ch_tag}: stitched_parts_2 "
                  f"re-globbed → {len(reglob2)} parts")

        return changed

    def _load_chapter_data(self, data: dict):
        """Load chapter-specific data from JSON."""
        self.stage = data.get('stage', 'init')
        self.raw_pages = data.get('raw_pages', [])
        self.stitched_parts = data.get('stitched_parts', [])
        self.source_folder = data.get('source_folder')
        self.panels = data.get('panels', [])
        self.stitched_boxes = data.get('stitched_boxes', [])
        self.stitched_boxes_1 = data.get('stitched_boxes_1', [])
        self.stitched_boxes_2 = data.get('stitched_boxes_2', [])
        self.stitched_parts_2 = data.get('stitched_parts_2', [])
        self.panels_with_text = data.get('panels_with_text', [])
        self.panels_mapping = data.get('panels_mapping', {})
        self.created_at = data.get('created_at', self.created_at)
        self.error = data.get('error')
        self.video_path = data.get('video_path')
        self.video_clips = data.get('video_clips', [])
        self.boxes1_cropped_hash = data.get('boxes1_cropped_hash', '')
        self.boxes2_cropped_hash = data.get('boxes2_cropped_hash', '')

        # ✅ FIX: Load batch information too!
        self.is_batch = data.get('is_batch', False)
        self.chapter_list = data.get('chapter_list', [self.current_chapter])
        self.manhwa_name = data.get('manhwa_name', self.manhwa_name)
        
        # If batch info was loaded, set start/end chapters
        if self.is_batch and len(self.chapter_list) > 1:
            self.start_chapter = min(self.chapter_list)
            self.end_chapter = max(self.chapter_list)

    def save(self):
        """Save current chapter state to its state file."""
        self.updated_at = datetime.now().isoformat()
        
        # DEBUG: Log what we're about to save
        print(f"[DEBUG] save() called: is_batch={self.is_batch}, chapter_list={self.chapter_list}")

        # ✅ RELIABILITY: rotate the previous good state to .bak before writing.
        # If a future bug corrupts state.json, load() recovers from .bak in
        # seconds instead of re-processing the chapter.
        try:
            if os.path.exists(self.state_file):
                shutil.copyfile(self.state_file, self.state_file + '.bak')
        except Exception as e:
            print(f"[save] Could not back up state file: {e}")

        # Always save batch info to ALL chapter state files when in batch mode —
        # ✅ PERFORMANCE: but only when the metadata actually changed. With 89
        # chapters, re-writing every state file on every save was O(N²) file I/O.
        meta_sig = None
        if self.is_batch:
            try:
                meta_sig = hashlib.md5(json.dumps(
                    {'p': self.project_name, 'm': self.manhwa_name, 'b': self.is_batch,
                     'c': self.chapter_list, 's': self.settings},
                    sort_keys=True, default=str).encode()).hexdigest()
            except Exception:
                meta_sig = None
        if meta_sig is not None and meta_sig == getattr(self, '_meta_sync_sig', None):
            chapters_to_save = [self.current_chapter]  # nothing changed — skip the N-way sync
        else:
            self._meta_sync_sig = meta_sig
            chapters_to_save = self.chapter_list if self.is_batch else [self.current_chapter]
        
        data = {
            'project_name': self.project_name,
            'manhwa_name': self.manhwa_name,
            'current_chapter': self.current_chapter,
            'is_batch': self.is_batch,
            'chapter_list': self.chapter_list,
            'stage': self.stage,
            'raw_pages': self.raw_pages,
            'stitched_parts': self.stitched_parts,
            'source_folder': self.source_folder,
            'panels': self.panels,
            'stitched_boxes': self.stitched_boxes,
            'stitched_boxes_1': self.stitched_boxes_1,
            'stitched_boxes_2': self.stitched_boxes_2,
            'stitched_parts_2': self.stitched_parts_2,
            'panels_with_text': self.panels_with_text,
            'panels_mapping': self.panels_mapping,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'error': self.error,
            'video_path': self.video_path,
            'video_clips': self.video_clips,
            'settings': self.settings,
            'boxes1_cropped_hash': self.boxes1_cropped_hash,
            'boxes2_cropped_hash': self.boxes2_cropped_hash,
            'batch_progress': self.batch_progress,
        }
        
        # Save current chapter's state file
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str, ensure_ascii=False)
        
        # ✅ BATCH MODE FIX: Save master state file with current viewing chapter
        if self.is_batch:
            master_state_file = os.path.join(config.MANHWA_DOWNLOAD_DIR, self.manhwa_name, '.batch_state.json')
            # ✅ RELIABILITY: back up the previous master before overwriting
            try:
                if os.path.exists(master_state_file):
                    shutil.copyfile(master_state_file, master_state_file + '.bak')
            except Exception:
                pass
            master_data = {
                'current_viewing_chapter': self.current_chapter,
                'manhwa_name': self.manhwa_name,
                'chapter_list': self.chapter_list,
                'is_batch': self.is_batch,
                'updated_at': self.updated_at
            }
            os.makedirs(os.path.dirname(master_state_file), exist_ok=True)
            with open(master_state_file, 'w', encoding='utf-8') as f:
                json.dump(master_data, f, indent=2)
        
        # ✅ BATCH FIX: Sync ONLY batch metadata to other chapters' state files.
        # The old code copied the entire data blob (stitched_parts, panels, boxes,
        # stage...) into every chapter's file, so all chapters ended up pointing
        # at the LAST processed chapter's images. Per-chapter artifacts must stay
        # per-chapter — we read-modify-write so each chapter keeps its own results.
        if self.is_batch and len(chapters_to_save) > 1:
            metadata_sync = {
                'project_name': self.project_name,
                'manhwa_name': self.manhwa_name,
                'is_batch': self.is_batch,
                'chapter_list': self.chapter_list,
                'settings': self.settings,
            }
            for chapter_num in chapters_to_save:
                if chapter_num == self.current_chapter:
                    continue  # Current chapter already saved above with full data
                chapter_state_file = self._get_chapter_state_file(chapter_num)
                if not os.path.exists(chapter_state_file):
                    continue  # Chapter not processed yet — leave it alone
                try:
                    with open(chapter_state_file, 'r', encoding='utf-8') as f:
                        chapter_data = json.load(f)
                    chapter_data.update(metadata_sync)
                    with open(chapter_state_file, 'w', encoding='utf-8') as f:
                        json.dump(chapter_data, f, indent=2, default=str, ensure_ascii=False)
                except Exception as e:
                    print(f"[save] Could not sync metadata to chapter {chapter_num}: {e}")

    def _get_chapter_state_file(self, chapter_num):
        """Get state file path for a specific chapter."""
        ch_paths = config.get_manhwa_paths(self.manhwa_name, chapter_num)
        return ch_paths['state']

    @staticmethod
    def _read_state_json(path):
        """✅ RELIABILITY: read a state JSON with corruption recovery.
        If the file is unreadable, falls back to the .bak rotation copy
        written on the previous save. Returns dict or None."""
        def _read(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return None

        data = _read(path)
        if data is not None:
            return data
        bak = path + '.bak'
        if os.path.exists(bak):
            data = _read(bak)
            if data is not None:
                print(f"[PipelineState] ⚠️ {os.path.basename(path)} was corrupt — "
                      f"recovered from .bak ({os.path.basename(path)})")
                return data
        return None

    @classmethod
    def load(cls, project_name):
        """Load project state. For batch projects, loads the current viewing chapter from master state."""
        state = cls(project_name)

        # ✅ BATCH FIX: plain project names (the manhwa folder itself) default to
        # Chapter_00000, which has no images when chapters are numbered 1..N.
        # Resolve the real start chapter from the download folder on disk.
        # NOTE: use state.project_name (post name-resolution), not the raw
        # argument — a display name like "Nan Hao Shang Feng" maps to the
        # hyphenated folder "Nan-Hao-Shang-Feng".
        if not state.is_batch and state.current_chapter == 0 and "__Chapter" not in state.project_name:
            try:
                import re as _re
                root = os.path.join(config.MANHWA_DOWNLOAD_DIR, state.project_name)
                if os.path.isdir(root):
                    _img_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.img')

                    def _has_imgs(d):
                        try:
                            return any(f.lower().endswith(_img_exts)
                                       for f in os.listdir(d)
                                       if os.path.isfile(os.path.join(d, f)))
                        except OSError:
                            return False

                    ch_dirs = sorted(
                        d for d in os.listdir(root)
                        if _re.match(r'Chapter_\d{5}$', d) and _has_imgs(os.path.join(root, d))
                    )
                    if ch_dirs:
                        nums = [int(_re.search(r'(\d+)', d).group(1)) for d in ch_dirs]
                        state.chapter_list = nums
                        state.is_batch = len(nums) > 1
                        state.start_chapter = min(nums)
                        state.end_chapter = max(nums)
                        target = min(nums)
                        if target != state.current_chapter:
                            state.current_chapter = target
                            state.paths = config.get_manhwa_paths(state.manhwa_name, target)
                            state._update_paths()
                        print(f"[PipelineState.load] Resolved start chapter {target} "
                              f"from disk ({len(nums)} chapters with images)")
            except Exception as e:
                print(f"[PipelineState.load] Chapter resolution failed: {e}")


        # ✅ BATCH MODE FIX: Check for master state file to get current viewing chapter
        if state.is_batch:
            master_state_file = os.path.join(config.MANHWA_DOWNLOAD_DIR, state.manhwa_name, '.batch_state.json')
            if os.path.exists(master_state_file):
                try:
                    with open(master_state_file, 'r', encoding='utf-8') as f:
                        master_data = json.load(f)
                    viewing_chapter = master_data.get('current_viewing_chapter')
                    if viewing_chapter is not None and viewing_chapter in state.chapter_list:
                        # Switch to the chapter the user was last viewing
                        state.current_chapter = viewing_chapter
                        state.paths = config.get_manhwa_paths(state.manhwa_name, viewing_chapter)
                        state._update_paths()
                        print(f"[PipelineState.load] Batch mode: Loading chapter {viewing_chapter} from master state")
                except Exception as e:
                    print(f"[PipelineState.load] Could not load master state: {e}")
        
        if os.path.exists(state.state_file):
            data = cls._read_state_json(state.state_file)
            if data is not None:
                state._load_chapter_data(data)

                # Load batch-specific data
                state.batch_progress = data.get('batch_progress', {})

                # Load settings
                saved_settings = data.get('settings', {})
                state.settings.update(saved_settings)
            else:
                print(f"[PipelineState.load] ⚠️ Unreadable state file for chapter "
                      f"{state.current_chapter} — starting fresh")

        # ✅ BATCH FIX: chapter_list may only be known now (from the state file) —
        # re-check the master viewing chapter with the real chapter list.
        if state.is_batch and len(state.chapter_list) > 1:
            master_state_file = os.path.join(config.MANHWA_DOWNLOAD_DIR, state.manhwa_name, '.batch_state.json')
            if os.path.exists(master_state_file):
                try:
                    with open(master_state_file, 'r', encoding='utf-8') as f:
                        viewing = json.load(f).get('current_viewing_chapter')
                    if viewing is not None and viewing in state.chapter_list and viewing != state.current_chapter:
                        print(f"[PipelineState.load] Batch mode: switching to viewing chapter {viewing}")
                        state.switch_to_chapter(viewing)
                except Exception as e:
                    print(f"[PipelineState.load] Master state re-check failed: {e}")

        # Auto-repair: if saved stage is stale/lower than what's on disk, fix it.
        # NOTE: only UPGRADE here. The narrated two-stage flow legitimately
        # passes through detected2 AFTER cropped1 (Crop 1 → Stitch 2 → Detect 2),
        # so downgrading on a lower disk stage would wipe Detect 2 results every
        # time the state reloads. _infer_stage_from_disk() is two-stage aware.
        disk_stage = state._infer_stage_from_disk()
        STAGE_RANK = {s: i for i, s in enumerate(cls.STAGES)}

        if STAGE_RANK.get(disk_stage, 0) > STAGE_RANK.get(state.stage, 0):
            print(f"[PipelineState] Auto-repairing stage: {state.stage!r} → {disk_stage!r} "
                  f"(detected from disk for Chapter {state.current_chapter})")
            state.stage = disk_stage
            state.error = None
            state.save()

        # ✅ BATCH FIX: self-heal per-chapter artifacts clobbered by the old
        # save-propagation bug (stitched_parts pointing at another chapter, etc.)
        if state.refresh_chapter_artifacts():
            state.save()

        return state
    
    @classmethod
    def load_chapter(cls, manhwa_name: str, chapter_num: int):
        """Load a specific chapter directly."""
        project_name = config.get_batch_project_name(manhwa_name, chapter_num, chapter_num)
        state = cls(project_name)
        state.current_chapter = chapter_num
        
        if os.path.exists(state.state_file):
            data = cls._read_state_json(state.state_file)
            if data is not None:
                state._load_chapter_data(data)
                saved_settings = data.get('settings', {})
                state.settings.update(saved_settings)

        return state

    def _infer_stage_from_disk(self) -> str:
        """
        Scan output directories to determine the highest completed pipeline stage.
        Uses new organized folder structure.
        """
        # ── clips_done: video clips exist in manhwa_clips/ ──────────────────
        if os.path.isdir(self.clips_dir):
            clips = [f for f in os.listdir(self.clips_dir) 
                    if f.endswith('.mp4') and os.path.getsize(os.path.join(self.clips_dir, f)) > 10000]
            if clips:
                return 'clips_done'
        
        # ── video_done: legacy video path or clips folder ───────────────────
        if self.video_path and os.path.exists(self.video_path):
            return 'video_done'

        # ── audio_done: audio files exist in manhwa_audio/ ──────────────────
        if os.path.isdir(self.audio_dir):
            audio_files = [f for f in os.listdir(self.audio_dir) 
                          if f.endswith('.mp3') and os.path.getsize(os.path.join(self.audio_dir, f)) > 1000]
            if audio_files:
                return 'audio_done'

        # ── narrated: panels have narration text ────────────────────────────
        if any(p.get('narration') or p.get('scene_narration') for p in self.panels):
            return 'narrated'

        # ── text_extracted: text files exist in manhwa_text/ ────────────────
        if os.path.isdir(self.text_dir):
            text_files = [f for f in os.listdir(self.text_dir) 
                         if f.endswith(('.txt', '.json')) and os.path.getsize(os.path.join(self.text_dir, f)) > 10]
            if text_files:
                return 'text_extracted'
        
        # Also check if panels have text
        if any(p.get('text') or p.get('dialogue') for p in self.panels):
            return 'text_extracted'

        # ── cropped2: art panels exist (Crop 2 writes panel_N.jpg into a
        #    per-part subdirectory of crop_dir; legacy flows wrote them
        #    directly into crop_dir) ──────────────────────────────────────
        if os.path.isdir(self.crop_dir):
            art_panel_files = []
            try:
                for entry in os.listdir(self.crop_dir):
                    entry_path = os.path.join(self.crop_dir, entry)
                    if os.path.isdir(entry_path):
                        art_panel_files += [f for f in os.listdir(entry_path)
                                            if f.startswith('panel_') and f.endswith(('.jpg', '.png'))]
                    elif entry.startswith('panel_') and entry.endswith(('.jpg', '.png')):
                        art_panel_files.append(entry)
            except OSError:
                pass
            if art_panel_files:
                return 'cropped2'

        # Also check if panels array has existing files
        if self.panels:
            existing = sum(1 for p in self.panels if os.path.exists(p.get('path', '')))
            if existing >= len(self.panels) * 0.8:
                return 'cropped2'

        # ── stitched2: re-stitched bubble panels exist in {crop_dir}/stitch2/ ──
        if os.path.isdir(os.path.join(self.crop_dir, "stitch2")):
            stitch2_files = [f for f in os.listdir(os.path.join(self.crop_dir, "stitch2"))
                             if f.startswith('stitch2') and f.endswith(('.jpg', '.png'))]
            if stitch2_files:
                return 'stitched2'

        # ── cropped1: bubble-text panels exist in manhwa_crop/ (numbered files) ───
        if os.path.isdir(self.crop_dir):
            numbered_files = [f for f in os.listdir(self.crop_dir)
                             if f[0].isdigit() and f.endswith(('.jpg', '.png'))]
            if numbered_files:
                return 'cropped1'

        # Also check if panels_with_text array has existing files
        if self.panels_with_text:
            existing = sum(1 for p in self.panels_with_text if os.path.exists(p.get('path', '')))
            if existing >= len(self.panels_with_text) * 0.8:
                return 'cropped1'

        # ── detected2 / detected1: TWO-STAGE FIX ──────────────────────────
        # stitched_boxes_2 is only populated by Detect 2 (art panels on
        # Stitched Image 2). The legacy stitched_boxes field is ALSO written
        # by Detect 1 (bubble panels), so it must only ever imply detected1 —
        # treating it as detected2 made the stage jump straight to detected2
        # after Detect 1 and broke the two-stage narrated flow.
        if self.stitched_boxes_2:
            return 'detected2'
        detect_file = os.path.join(self.detect_dir, 'detection_boxes.json')
        if os.path.exists(detect_file):
            return 'detected2'

        # Detect 1 wrote bubble-panel boxes (stitched_boxes_1, mirrored into
        # the legacy stitched_boxes field) but Crop 1 hasn't run yet
        if self.stitched_boxes_1 or self.stitched_boxes:
            return 'detected1'

        # ── stitched: stitched images exist in manhwa_stitch/ ───────────────
        if os.path.isdir(self.stitch_dir):
            stitch_files = [f for f in os.listdir(self.stitch_dir) 
                           if f.startswith('stitched_') and f.endswith(('.jpg', '.png'))]
            if stitch_files:
                return 'stitched'
        
        if self.stitched_parts:
            return 'stitched'

        # ── loaded: raw images exist in manhwa_download/ ────────────────────
        if os.path.isdir(self.download_dir):
            raw_files = [f for f in os.listdir(self.download_dir) 
                        if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
            if raw_files:
                return 'loaded'
        
        if self.raw_pages:
            return 'loaded'

        return 'init'


class Orchestrator:
    def __init__(self):
        self.narrator = Narrator()
        self.rewriter = ScriptRewriter()
        self.capcut = CapCutDirector()
        self._progress_callbacks = []
        self._running = False
        self._should_cancel = False

    def add_progress_callback(self, callback):
        self._progress_callbacks.append(callback)

    def _emit_progress(self, stage, current, total, message):
        for cb in self._progress_callbacks:
            try: cb(stage, current, total, message)
            except Exception: pass

    def cancel(self):
        self._should_cancel = True

    # ─── Helpers ─────────────────────────────────────────────────
    @staticmethod
    def _boxes_hash(boxes: list) -> str:
        """Stable hash of a list of detection boxes for change detection."""
        if not boxes:
            return ""
        sig = json.dumps(
            [{k: v for k, v in sorted(b.items()) if k in ('x','y','w','h','id')} for b in boxes],
            sort_keys=True
        )
        return hashlib.md5(sig.encode()).hexdigest()

    _STAGE_RANK = {
        s: i for i, s in enumerate([
            'init','loaded','stitched','detected1','cropped1',
            'stitched2','detected2','cropped2','text_extracted',
            'text_removed','narrated','audio_done','video_done','clips_done'
        ])
    }

    def _stage_gte(self, state, stage: str) -> bool:
        """Return True if state.stage is at or past the given stage."""
        return (self._STAGE_RANK.get(state.stage, 0) >=
                self._STAGE_RANK.get(stage, 999))

    def get_projects(self):
        projects = []
        if not os.path.exists(config.RAW_MANHWA_DIR):
            return projects
        for title in sorted(os.listdir(config.RAW_MANHWA_DIR)):
            title_dir = os.path.join(config.RAW_MANHWA_DIR, title)
            if not os.path.isdir(title_dir):
                continue
            for chapter in sorted(os.listdir(title_dir)):
                chapter_dir = os.path.join(title_dir, chapter)
                if not os.path.isdir(chapter_dir) or chapter == "stitched":
                    continue
                
                project_name = f"{title}___{chapter}"
                
                state_file = os.path.join(config.MANHWA_PANEL_DIR, title, chapter, "state.json")
                stage = 'init'
                panels_count = 0
                created_at = ''
                updated_at = ''
                video_path = None
                settings = {}
                
                if os.path.exists(state_file):
                    try:
                        with open(state_file) as f:
                            data = json.load(f)
                        stage = data.get('stage', 'init')
                        panels_count = len(data.get('panels', []))
                        created_at = data.get('created_at', '')
                        updated_at = data.get('updated_at', '')
                        video_path = data.get('video_path')
                        settings = data.get('settings', {})
                    except Exception: pass
                
                projects.append({
                    'name': project_name,
                    'stage': stage,
                    'panels_count': panels_count,
                    'created_at': created_at,
                    'updated_at': updated_at,
                    'video_path': video_path,
                    'settings': settings,
                })
        return projects

    def delete_project(self, project_name):
        state = PipelineState(project_name)
        if os.path.exists(state.project_dir):
            shutil.rmtree(state.project_dir)

    def check_dependencies(self):
        return {'ollama': self.narrator.check_ollama(), 'ffmpeg': _check_ffmpeg()}

    # ─── Pipeline Steps ──────────────────────────────────────────
    
    def _reset_viewing_to_first_chapter(self, state):
        """After a batch step finishes, return the viewing context to the first
        chapter so the UI opens on Chapter 1 instead of the last processed one."""
        if state.is_batch and len(state.chapter_list) > 1:
            first = min(state.chapter_list)
            if state.current_chapter != first:
                state.switch_to_chapter(first)
                state.save()

    def _run_batch_or_single(self, state, step_name, single_step_func):
        """
        Helper to run a step in either batch or single mode.
        In batch mode, loops through all chapters and calls the single step function for each.
        In single mode, just calls the single step function once.
        """
        # ⚡ SPEED MODE: propagate the project's fast_mode setting to the deep
        # modules (scene_narrator / text_extractor / kokoro read config.SPEED_MODE).
        # Runs for both batch and single-chapter flows — every step lands here.
        config.SPEED_MODE = bool(state.settings.get('fast_mode', False))
        config.SPEED_SETTINGS = {
            'kokoro_gpu': bool(state.settings.get('kokoro_gpu', True)),
        }
        if state.is_batch and len(state.chapter_list) > 1:
            print(f"[{step_name}] BATCH MODE: Processing {len(state.chapter_list)} chapters: {state.chapter_list}")
            
            for idx, chapter_num in enumerate(state.chapter_list):
                if self._should_cancel:
                    break
                
                self._emit_progress(step_name, idx, len(state.chapter_list), 
                    f"Processing Chapter {chapter_num} ({idx + 1}/{len(state.chapter_list)})...")
                
                # Switch to this chapter
                state.switch_to_chapter(chapter_num)
                
                # Process this chapter
                single_step_func(state)
                
                if state.error:
                    print(f"[{step_name}] Error in chapter {chapter_num}: {state.error}")
                    # Continue with next chapter instead of stopping
                    state.error = None
            
            self._emit_progress(step_name, len(state.chapter_list), len(state.chapter_list), 
                f"Batch {step_name} complete for {len(state.chapter_list)} chapters")
            self._reset_viewing_to_first_chapter(state)
            return state
        
        # Single mode: just run the function once
        return single_step_func(state)
    
    def step_load_images(self, project_name, source_folder):
        state = PipelineState(project_name)
        self._emit_progress('load', 0, 1, "Loading images...")
        try:
            # First, detect batch mode by checking the source folder structure
            import json
            from pathlib import Path
            import re
            
            load_dir = source_folder if source_folder and os.path.exists(source_folder) else state.raw_dir
            
            if not os.path.exists(load_dir):
                state.error = f"Raw directory not found: {load_dir}"
                state.save()
                return state
            
            # Check for batch mode: multiple Chapter_XXXXX folders?
            # ✅ BATCH FIX: only count folders that actually contain images —
            # empty folders (e.g. a stray Chapter_00000) must not become chapters.
            _IMG_EXTS = {".img", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
            manhwa_folder = Path(load_dir)
            chapter_folders = sorted([
                d for d in manhwa_folder.iterdir()
                if d.is_dir() and re.match(r'Chapter_\d{5}', d.name)
                and any(p.suffix.lower() in _IMG_EXTS for p in d.iterdir() if p.is_file())
            ])
            
            is_batch_mode = len(chapter_folders) > 1
            # ✅ BATCH FIX: a manhwa root with chapter subfolders (even just one
            # ready so far, e.g. during a partial download) must load those
            # chapters — not fail with "No valid images found".
            has_chapter_folders = len(chapter_folders) >= 1 and not any(
                p.suffix.lower() in _IMG_EXTS for p in manhwa_folder.iterdir() if p.is_file()
            )

            if is_batch_mode or has_chapter_folders:
                # BATCH MODE: Manually set batch info
                print(f"[Load] CHAPTER MODE: Found {len(chapter_folders)} chapter(s) with images")

                state.manhwa_name = manhwa_folder.name
                state.is_batch = is_batch_mode
                state.chapter_list = [
                    int(re.search(r'Chapter_(\d+)', d.name).group(1))
                    for d in chapter_folders
                ]
                state.start_chapter = min(state.chapter_list)
                state.end_chapter = max(state.chapter_list)
                state.current_chapter = state.start_chapter
                
                print(f"[Load] Batch info: manhwa='{state.manhwa_name}', chapters={state.chapter_list}")
                
                # Load images for each chapter
                for idx, chapter_num in enumerate(state.chapter_list):
                    self._emit_progress('load', idx, len(state.chapter_list), 
                        f"Loading Chapter {chapter_num} ({idx + 1}/{len(state.chapter_list)})...")
                    
                    state.switch_to_chapter(chapter_num)
                    
                    # Load images for this chapter
                    chapter_dir = manhwa_folder / f"Chapter_{chapter_num:05d}"
                    _IMG_EXTS = {".img", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
                    images = sorted(
                        str(p) for p in chapter_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in _IMG_EXTS
                    )
                    
                    state.raw_pages = images
                    state.stage = 'loaded'
                    state.error = None
                    state.save()
                    
                    print(f"[Load] Chapter {chapter_num}: Loaded {len(images)} images")
                
                self._emit_progress('load', len(state.chapter_list), len(state.chapter_list), 
                    f"Loaded {len(state.chapter_list)} chapters")
                self._reset_viewing_to_first_chapter(state)
            else:
                # SINGLE MODE: Load images from the folder
                _IMG_EXTS = {".img", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
                images = sorted(
                    str(p) for p in Path(load_dir).iterdir()
                    if p.is_file() and p.suffix.lower() in _IMG_EXTS
                )
                
                if not images:
                    state.error = "No valid images found."
                    state.save()
                    return state
                    
                state.raw_pages = images
                state.set_source(load_dir)
                state.stage = 'loaded'
                state.error = None
                state.save()
                self._emit_progress('load', 1, 1, f"Loaded {len(images)} pages")
                
        except Exception as e:
            state.error = str(e)
            import traceback
            traceback.print_exc()
            state.save()
        return state

    def step_stitch(self, state):
        # ✅ FALLBACK: auto-scan raw pages if missing (e.g. stitch clicked right
        # after load) — the chapter's download folder already has the images.
        if not state.raw_pages and os.path.isdir(state.download_dir):
            _IMG_EXTS = {".img", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
            state.raw_pages = sorted([
                os.path.join(state.download_dir, f)
                for f in os.listdir(state.download_dir)
                if os.path.splitext(f.lower())[1] in _IMG_EXTS
            ])

        # Check if we have images - either stage is 'loaded' OR raw_pages is not empty
        has_images = (state.stage in ['loaded', 'stitched', 'detected1', 'cropped1', 'stitched2',
                                       'detected2', 'cropped2', 'text_extracted', 'text_removed',
                                       'narrated', 'audio_done', 'video_done']) or len(state.raw_pages) > 0
        
        if not has_images:
            state.error = "Must load images first."
            return state
        
        # ── BATCH MODE: Process all chapters ──
        if state.is_batch and len(state.chapter_list) > 1:
            print(f"[Stitch] BATCH MODE: Processing {len(state.chapter_list)} chapters: {state.chapter_list}")
            
            for idx, chapter_num in enumerate(state.chapter_list):
                if self._should_cancel:
                    break
                
                self._emit_progress('stitch', idx, len(state.chapter_list), 
                    f"Processing Chapter {chapter_num} ({idx + 1}/{len(state.chapter_list)})...")
                
                # Switch to this chapter
                state.switch_to_chapter(chapter_num)
                
                # Check if this chapter has images — auto-scan the download
                # folder first before giving up on this chapter
                if not state.raw_pages or len(state.raw_pages) == 0:
                    _IMG_EXTS_CH = {".img", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
                    if os.path.isdir(state.download_dir):
                        state.raw_pages = sorted([
                            os.path.join(state.download_dir, f)
                            for f in os.listdir(state.download_dir)
                            if os.path.splitext(f.lower())[1] in _IMG_EXTS_CH
                        ])
                if not state.raw_pages or len(state.raw_pages) == 0:
                    print(f"[Stitch] Warning: Chapter {chapter_num} has no images, skipping...")
                    continue
                
                # Process this chapter
                self._stitch_single_chapter(state)
                
                if state.error:
                    print(f"[Stitch] Error in chapter {chapter_num}: {state.error}")
                    # Continue with next chapter instead of stopping
                    state.error = None
            
            self._emit_progress('stitch', len(state.chapter_list), len(state.chapter_list), 
                f"Batch stitching complete for {len(state.chapter_list)} chapters")
            self._reset_viewing_to_first_chapter(state)
            return state
        
        # ── SINGLE MODE: Process current chapter only ──
        return self._stitch_single_chapter(state)
    
    def _stitch_single_chapter(self, state):
        """Stitch a single chapter (used by both single and batch mode)"""
        self._emit_progress('stitch', 0, 1, "Checking for stitched parts...")
        import glob, re
        
        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
            
        # Check for existing stitched parts (match actual filenames from export_dataset_stitched)
        existing_parts = sorted(glob.glob(os.path.join(state.stitched_dir, "stitched_part*.jpg")), key=natural_sort_key)
        if not existing_parts:
            existing_parts = sorted(glob.glob(os.path.join(state.stitched_dir, "stitched_dataset.jpg")), key=natural_sort_key)
        if not existing_parts:
            existing_parts = sorted(glob.glob(os.path.join(state.stitched_dir, "stitched_full.*")), key=natural_sort_key)
            
        if existing_parts:
            state.stitched_parts = existing_parts
            if not self._stage_gte(state, 'stitched'):
                state.stage = 'stitched'
            state.error = None
            state.save()
            self._emit_progress('stitch', 1, 1, f"Found {len(existing_parts)} existing stitched parts.")
            return state

        self._emit_progress('stitch', 0, len(state.raw_pages), "Stitching new images...")
        try:
            temp_stitched = stitch_images(
                state.raw_pages, state,
                progress_callback=lambda c, t, m: self._emit_progress('stitch', c, t, m))
            
            import re
            ch_num_match = re.search(r'(\d+)', str(state.current_chapter))
            ch_num = int(ch_num_match.group(1)) if ch_num_match else 1
            
            self._emit_progress('stitch', 1, 1, f"Splitting into parts for LLM safety...")
            
            saved_parts = export_dataset_stitched(
                temp_stitched, 
                state,
                progress_callback=lambda c, t, m: self._emit_progress('stitch', c, t, m))
                
            state.stitched_parts = saved_parts
            state.stage = 'stitched'
            state.error = None
            state.save()
            
            if os.path.exists(temp_stitched):
                os.remove(temp_stitched)
                
            self._emit_progress('stitch', 1, 1, "Stitching complete")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_crop_panels(self, state):
        if not self._stage_gte(state, 'loaded'):
            state.error = "Must load/stitch images first."
            return state
            
        # Use stitched parts if available, otherwise use raw pages
        if state.stitched_parts and len(state.stitched_parts) > 0:
            # Use stitched parts (new structure)
            input_images = state.stitched_parts
            msg_prefix = "Cropping panels from stitched parts"
        else:
            # Fallback to raw pages
            input_images = state.raw_pages
            msg_prefix = "Cropping panels from raw pages"
            
        if not input_images:
            state.error = "No images to crop from"
            state.save()
            return state
            
        self._emit_progress('crop', 0, len(input_images), f"{msg_prefix}...")
        try:
            panels = crop_panels(input_images, state,
                progress_callback=lambda c, t, m: self._emit_progress('crop', c, t, m))
            state.panels = panels
            state.stage = 'cropped'
            state.error = None
            state.save()
            self._emit_progress('crop', len(panels), len(panels), f"Detected {len(panels)} panels")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_detect_panels_yolo(self, state):
        """Legacy detect — runs existing KAGGLE model on Stitched Image 1."""
        if not self._stage_gte(state, 'stitched'):
            state.error = "Must stitch images first."
            return state
        
        # Re-running detection invalidates downstream two-stage detection
        state.stitched_boxes_2 = []
        state.stitched_parts_2 = []
        state.boxes1_cropped_hash = ""
        state.boxes2_cropped_hash = ""
        
        self._emit_progress('detect', 0, 1, "Running YOLO panel detection on stitched parts...")
        try:
            from pipeline.panel_extractor_yolo import get_global_extractor
            extractor = get_global_extractor()
            
            total_boxes = 0
            stitched_boxes = []
            
            from PIL import Image
            y_offset = 0
            
            for part_path in state.stitched_parts:
                part_boxes = extractor.detect_image_boxes(part_path)
                
                with Image.open(part_path) as img:
                    part_h = img.height
                
                for box in part_boxes:
                    stitched_boxes.append({
                        "id": total_boxes + 1,
                        "x": box['x'], 
                        "y": box['y'] + y_offset,
                        "w": box['w'], 
                        "h": box['h'],
                        "part_path": part_path,
                        "part_y": box['y'],
                    })
                    total_boxes += 1
                
                y_offset += part_h
            
            state.stitched_boxes = stitched_boxes
            state.stitched_boxes_1 = stitched_boxes  # keep UI/two-stage field in sync
            state.stage = 'detected1'
            state.error = None
            state.save()
            self._emit_progress('detect', 1, 1, f"YOLO detected {total_boxes} panels")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_crop_extracted_panels(self, state):
        """Legacy crop — crops boxes from stitched image."""
        if not self._stage_gte(state, 'detected1'):
            state.error = "Must detect panels first."
            return state
            
        self._emit_progress('crop', 0, 1, "Cropping detected panels from stitched parts...")
        try:
            import cv2
            import json
            
            saved = []
            state.panels = []
            idx = 1
            
            boxes_by_part = {}
            for box in state.stitched_boxes:
                part_path = box.get('part_path', getattr(state, 'stitched_path', None))
                if not part_path: continue
                if part_path not in boxes_by_part:
                    boxes_by_part[part_path] = []
                boxes_by_part[part_path].append(box)
            
            for part_path, boxes in boxes_by_part.items():
                stitched_img = cv2.imread(part_path)
                if stitched_img is None:
                    continue
                
                part_name = os.path.splitext(os.path.basename(part_path))[0]
                part_dir = os.path.join(state.project_dir, part_name)
                os.makedirs(part_dir, exist_ok=True)
                
                manifest = {}
                for box in boxes:
                    x, w, h = int(float(box['x'])), int(float(box['w'])), int(float(box['h']))
                    y = int(float(box.get('part_y', box['y'])))
                    
                    crop = stitched_img[max(y, 0):max(y+h, 0), max(x, 0):max(x+w, 0)]
                    if crop.size == 0: continue
                    
                    out = os.path.join(part_dir, f"panel_{idx}.jpg")
                    cv2.imwrite(out, crop)
                    
                    manifest[f"panel_{idx}"] = "stitched"
                    saved.append(out)
                    
                    state.panels.append({
                        'panel_id': idx,
                        'path': out,
                        'padded_path': out,
                        'source_page': part_path,
                        'width': w,
                        'height': h,
                        'x': x,
                        'y': y
                    })
                    idx += 1
                
                manifest_path = os.path.join(part_dir, "panels_manifest.json")
                with open(manifest_path, 'w', encoding='utf-8') as f:
                    json.dump(manifest, f, indent=2)
                
            state.stage = 'cropped2'
            state.error = None
            state.save()
            self._emit_progress('crop', len(state.panels), len(state.panels), f"Cropped {len(state.panels)} panels")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    # ─── Two-Stage Detection Steps ───────────────────────────────

    def step_detect_bubble_text(self, state):
        """Detect 1: Run panel_with_bubble_text model on Stitched Image 1."""
        if state.stage not in ('clips_done', 'stitched', 'detected1', 'cropped1', 'stitched2',
                               'detected2', 'cropped2', 'text_extracted',
                               'text_removed', 'narrated', 'audio_done', 'video_done'):
            state.error = "Must stitch images first."
            return state
        
        return self._run_batch_or_single(state, 'detect1', self._detect_bubble_text_single)
    
    def _detect_bubble_text_single(self, state):
        """Detect bubble text for a single chapter"""

        # ── SMART RESUME: load cached detections from state if they exist ──
        if self._stage_gte(state, 'detected1') and state.stitched_boxes_1:
            n = len(state.stitched_boxes_1)
            self._emit_progress('detect1', 1, 1,
                f"✅ Detect 1: loaded {n} cached boxes (skipping re-detection)")
            return state

        
        self._emit_progress('detect1', 0, 1, "Running Detect 1 (bubble text model) on stitched parts...")
        try:
            from pipeline.panel_extractor_yolo import get_global_bubble_extractor
            extractor = get_global_bubble_extractor()
            
            total_boxes = 0
            boxes_1 = []
            
            from PIL import Image
            y_offset = 0
            
            for part_path in state.stitched_parts:
                part_boxes = extractor.detect_image_boxes(part_path)
                
                with Image.open(part_path) as img:
                    part_h = img.height
                
                for box in part_boxes:
                    boxes_1.append({
                        "id": total_boxes + 1,
                        "x": box['x'], 
                        "y": box['y'] + y_offset,
                        "w": box['w'], 
                        "h": box['h'],
                        "part_path": part_path,
                        "part_y": box['y'],
                    })
                    total_boxes += 1
                
                y_offset += part_h
            
            state.stitched_boxes_1 = boxes_1
            state.stitched_boxes = boxes_1  # Also set legacy field for UI compat
            state.stage = 'detected1'
            state.error = None
            state.save()
            self._emit_progress('detect1', 1, 1, f"Detect 1 found {total_boxes} panels with text")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_crop_bubble_text_panels(self, state):
        """Crop 1: Crop Detect 1 results into Manhwa_panel_with_bubble/title/chapter/0001.jpg."""
        if state.stage not in ('clips_done', 'detected1', 'cropped1', 'stitched2', 'detected2',
                               'cropped2', 'text_extracted', 'text_removed',
                               'narrated', 'audio_done', 'video_done'):
            state.error = "Must run Detect 1 first."
            return state
        
        return self._run_batch_or_single(state, 'crop1', self._crop_bubble_text_single)
    
    def _crop_bubble_text_single(self, state):
        """Crop bubble text panels for a single chapter"""

        # ── SMART RESUME: skip if crop files exist AND boxes unchanged ──
        current_hash = self._boxes_hash(state.stitched_boxes_1 or state.stitched_boxes)
        files_ok = (state.panels_with_text and
                    all(os.path.exists(p['path']) for p in state.panels_with_text))
        if self._stage_gte(state, 'cropped1') and files_ok and (
                getattr(state, 'boxes1_cropped_hash', None) == current_hash):
            n = len(state.panels_with_text)
            self._emit_progress('crop1', 1, 1,
                f"✅ Crop 1: loaded {n} cached panels (boxes unchanged, skipping re-crop)")
            return state

        
        self._emit_progress('crop1', 0, 1, "Cropping panels-with-text from stitched parts...")
        try:
            import cv2
            
            # Use stitched_boxes_1, fallback to stitched_boxes for legacy
            boxes = state.stitched_boxes_1 if state.stitched_boxes_1 else state.stitched_boxes
            
            # Build a part height map so we can resolve part_path for user-drawn/modified boxes
            from PIL import Image as PILImage
            part_map = []
            y_running_parts = 0
            for pp in state.stitched_parts:
                with PILImage.open(pp) as pimg:
                    ph = pimg.height
                part_map.append({
                    'path': pp,
                    'y_start': y_running_parts,
                    'y_end': y_running_parts + ph,
                    'height': ph,
                })
                y_running_parts += ph
            
            state.panels_with_text = []
            idx = 1
            
            for box in boxes:
                x = int(float(box['x']))
                y = int(float(box['y']))
                w = int(float(box['w']))
                h = int(float(box['h']))
                
                # Dynamically crop across multiple parts if the box spans a boundary
                crops = []
                current_y = y
                remaining_h = h
                
                for pm in part_map:
                    if remaining_h <= 0:
                        break
                    # If the current Y falls within this part
                    if pm['y_start'] <= current_y < pm['y_end']:
                        local_y = current_y - pm['y_start']
                        take_h = min(remaining_h, pm['height'] - local_y)
                        
                        img = cv2.imread(pm['path'])
                        if img is not None:
                            c = img[max(local_y, 0):local_y + take_h, max(x, 0):max(x+w, 0)]
                            if c.size > 0:
                                crops.append(c)
                        
                        current_y += take_h
                        remaining_h -= take_h
                
                if not crops:
                    continue
                    
                if len(crops) == 1:
                    final_crop = crops[0]
                else:
                    final_crop = cv2.vconcat(crops)
                    
                if final_crop.size == 0:
                    continue
                    
                out_name = f"{idx:04d}.jpg"
                out_path = os.path.join(state.crop_dir, out_name)
                cv2.imwrite(out_path, final_crop)
                
                # Determine main source page for reference
                main_part = part_map[0]['path'] if part_map else ""
                for pm in part_map:
                    if pm['y_start'] <= y < pm['y_end']:
                        main_part = pm['path']
                        break
                        
                state.panels_with_text.append({
                    'panel_id': idx,
                    'path': out_path,
                    'source_page': main_part,
                    'width': w,
                    'height': h,
                    'x': x,
                    'y': y,
                    'box_id': box.get('id', idx),
                })
                idx += 1
            
            state.boxes1_cropped_hash = current_hash
            state.stage = 'cropped1'
            state.error = None
            state.save()
            self._emit_progress('crop1', 1, 1, f"Cropped {len(state.panels_with_text)} panels-with-text")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_stitch_bubble_panels(self, state):
        """Stitch 2: Re-stitch the extracted panels-with-text into Stitched Image 2."""
        # ✅ FIX: Check prerequisites for BOTH single AND batch mode
        if state.stage not in ('clips_done', 'cropped1', 'stitched2', 'detected2', 'cropped2',
                               'text_extracted', 'text_removed', 'narrated',
                               'audio_done', 'video_done'):
            state.error = "Must crop panels-with-text first (Crop 1)."
            return state
        return self._run_batch_or_single(state, 'stitch2', self._stitch_bubble_panels_single)
    
    def _stitch_bubble_panels_single(self, state):
        """Stitch bubble panels for a single chapter."""
        # Check stage prerequisite for this chapter
        if state.stage not in ('clips_done', 'cropped1', 'stitched2', 'detected2', 'cropped2',
                               'text_extracted', 'text_removed', 'narrated',
                               'audio_done', 'video_done'):
            print(f"[stitch2] Skipping chapter {state.current_chapter} - stage is '{state.stage}'")
            return state

        # ── SMART RESUME: skip if stitch2 parts already exist on disk ──
        if (self._stage_gte(state, 'stitched2') and state.stitched_parts_2 and
                all(os.path.exists(p) for p in state.stitched_parts_2)):
            n = len(state.stitched_parts_2)
            self._emit_progress('stitch2', 1, 1,
                f"✅ Stitch 2: loaded {n} cached parts (skipping re-stitch)")
            return state

        
        self._emit_progress('stitch2', 0, 1, "Stitching panels-with-text into Stitched Image 2...")
        try:
            panel_paths = [p['path'] for p in state.panels_with_text if os.path.exists(p['path'])]
            if not panel_paths:
                state.error = "No panels-with-text to stitch."
                state.save()
                return state
            
            # Create a stitch2 subdirectory in the project dir
            stitch2_dir = os.path.join(state.project_dir, "stitch2")
            os.makedirs(stitch2_dir, exist_ok=True)
            
            # Wrap stitch2_dir in a namespace so stitch_images() can access .stitch_dir
            class _Stitch2State:
                def __init__(self, d):
                    self.stitch_dir = d
            stitch2_state = _Stitch2State(stitch2_dir)
            
            # Use the existing stitcher to join them vertically
            temp_stitched = stitch_images(
                panel_paths, stitch2_state,
                progress_callback=lambda c, t, m: self._emit_progress('stitch2', c, t, m))
            
            # Split into dataset-safe parts
            from pipeline.stitcher import _split_image_vertically, _save_under_size_limit, DATASET_MAX_HEIGHT
            from PIL import Image
            
            img = Image.open(temp_stitched).convert('RGB')
            slices = _split_image_vertically(img, DATASET_MAX_HEIGHT)
            img.close()
            
            saved_parts = []
            for part_idx, slice_img in enumerate(slices):
                if len(slices) == 1:
                    filename = "stitch2.jpg"
                else:
                    filename = f"stitch2_part{part_idx + 1}.jpg"
                out_path = os.path.join(stitch2_dir, filename)
                _save_under_size_limit(slice_img, out_path, 20)
                saved_parts.append(out_path)
                slice_img.close()
            
            # Clean up temp stitched file if different from output
            if temp_stitched not in saved_parts and os.path.exists(temp_stitched):
                os.remove(temp_stitched)
            
            state.stitched_parts_2 = saved_parts
            state.stage = 'stitched2'
            state.error = None
            state.save()
            self._emit_progress('stitch2', 1, 1, f"Stitched Image 2 created ({len(saved_parts)} parts)")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_detect_art_panels(self, state):
        """Detect 2: Run KAGGLE panel model on Stitched Image 2."""
        if not state.is_batch:
            if state.stage not in ('clips_done', 'stitched2', 'detected2', 'cropped2', 'text_extracted',
                                   'text_removed', 'narrated', 'audio_done', 'video_done'):
                state.error = "Must stitch panels-with-text first (Stitch 2)."
                return state
        return self._run_batch_or_single(state, 'detect2', self._detect_art_panels_single)
    
    def _detect_art_panels_single(self, state):
        """Detect art panels for a single chapter."""
        if state.stage not in ('clips_done', 'stitched2', 'detected2', 'cropped2', 'text_extracted',
                               'text_removed', 'narrated', 'audio_done', 'video_done'):
            print(f"[detect2] Skipping chapter {state.current_chapter} - stage is '{state.stage}'")
            return state

        # ── SMART RESUME: load cached detections from state if they exist ──
        if self._stage_gte(state, 'detected2') and state.stitched_boxes_2:
            n = len(state.stitched_boxes_2)
            self._emit_progress('detect2', 1, 1,
                f"✅ Detect 2: loaded {n} cached boxes (skipping re-detection)")
            return state

        
        self._emit_progress('detect2', 0, 1, "Running Detect 2 (art panel model) on Stitched Image 2...")
        try:
            from pipeline.panel_extractor_yolo import get_global_extractor
            extractor = get_global_extractor()
            
            total_boxes = 0
            boxes_2 = []
            
            from PIL import Image
            y_offset = 0
            
            for part_path in state.stitched_parts_2:
                part_boxes = extractor.detect_image_boxes(part_path)
                
                with Image.open(part_path) as img:
                    part_h = img.height
                
                for box in part_boxes:
                    boxes_2.append({
                        "id": total_boxes + 1,
                        "x": box['x'], 
                        "y": box['y'] + y_offset,
                        "w": box['w'], 
                        "h": box['h'],
                        "part_path": part_path,
                        "part_y": box['y'],
                    })
                    total_boxes += 1
                
                y_offset += part_h
            
            state.stitched_boxes_2 = boxes_2
            state.stage = 'detected2'
            state.error = None
            state.save()
            self._emit_progress('detect2', 1, 1, f"Detect 2 found {total_boxes} art panels")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_crop_art_panels(self, state):
        """Crop 2: Crop Detect 2 boxes from Stitched Image 2 and map to Detect 1 parents.

        Each art panel is also bubble-cleaned (EasyOCR inpaint) so the narrated
        video shows panels WITHOUT speech-bubble text. Set settings
        ['clean_art_panels'] = False to keep the raw crops."""
        if not state.is_batch:
            if state.stage not in ('clips_done', 'detected2', 'cropped2', 'text_extracted', 'text_removed',
                                   'narrated', 'audio_done', 'video_done'):
                state.error = "Must run Detect 2 first."
                return state
        return self._run_batch_or_single(state, 'crop2', self._crop_art_panels_single)
    
    def _crop_art_panels_single(self, state):
        """Crop art panels for a single chapter."""
        if state.stage not in ('clips_done', 'detected2', 'cropped2', 'text_extracted', 'text_removed',
                               'narrated', 'audio_done', 'video_done'):
            print(f"[crop2] Skipping chapter {state.current_chapter} - stage is '{state.stage}'")
            return state

        # ── SMART RESUME: skip if crop files exist AND boxes unchanged ──
        current_hash2 = self._boxes_hash(state.stitched_boxes_2)
        files_ok2 = (state.panels and
                     all(os.path.exists(p['path']) for p in state.panels))
        if self._stage_gte(state, 'cropped2') and files_ok2 and (
                getattr(state, 'boxes2_cropped_hash', None) == current_hash2):
            n = len(state.panels)
            self._emit_progress('crop2', 1, 1,
                f"✅ Crop 2: loaded {n} cached art panels (boxes unchanged, skipping re-crop)")
            return state

        
        self._emit_progress('crop2', 0, 1, "Cropping art panels from Stitched Image 2...")
        try:
            import cv2
            import json

            # ── Bubble cleaning (narrated mode): each art panel gets its
            #    speech-bubble text inpainted away so the final video shows
            #    clean artwork. The reader loads once and is reused. ──
            clean_panels = bool(state.settings.get('clean_art_panels', True))
            text_remover = None
            if clean_panels:
                try:
                    from pipeline import text_remover as _tr
                    text_remover = _tr
                    reader = text_remover._get_ocr_reader()
                    if reader is None:
                        text_remover = None  # fallback contour mode still works
                except Exception as _tre:
                    print(f"[crop2] Text remover unavailable: {_tre}")
                    text_remover = None

            boxes = state.stitched_boxes_2
            
            # Build a part height map so we can resolve part_path for user-drawn boxes
            from PIL import Image as PILImage
            part_map = []  # [{path, y_start, y_end, height}]
            y_running_parts = 0
            for pp in state.stitched_parts_2:
                with PILImage.open(pp) as pimg:
                    ph = pimg.height
                part_map.append({
                    'path': pp,
                    'y_start': y_running_parts,
                    'y_end': y_running_parts + ph,
                    'height': ph,
                })
                y_running_parts += ph
            
            state.panels = []
            state.panels_mapping = {}
            idx = 1
            
            manifests_by_part = {}
            
            # Build a vertical position map of panels_with_text for mapping
            # Each panel_with_text occupies a vertical slice in Stitched Image 2
            pwt_ranges = []
            y_running = 0
            for pwt in state.panels_with_text:
                pwt_h = pwt['height']
                pwt_ranges.append({
                    'panel_id': pwt['panel_id'],
                    'y_start': y_running,
                    'y_end': y_running + pwt_h,
                    'path': pwt['path'],
                })
                y_running += pwt_h
            
            for box in boxes:
                x = int(float(box['x']))
                y = int(float(box['y']))
                w = int(float(box['w']))
                h = int(float(box['h']))
                
                # Dynamically crop across multiple parts if the box spans a boundary
                crops = []
                current_y = y
                remaining_h = h
                
                for pm in part_map:
                    if remaining_h <= 0:
                        break
                    # If the current Y falls within this part
                    if pm['y_start'] <= current_y < pm['y_end']:
                        local_y = current_y - pm['y_start']
                        take_h = min(remaining_h, pm['height'] - local_y)
                        
                        img = cv2.imread(pm['path'])
                        if img is not None:
                            c = img[max(local_y, 0):local_y + take_h, max(x, 0):max(x+w, 0)]
                            if c.size > 0:
                                crops.append(c)
                        
                        current_y += take_h
                        remaining_h -= take_h
                
                if not crops:
                    continue
                    
                if len(crops) == 1:
                    final_crop = crops[0]
                else:
                    final_crop = cv2.vconcat(crops)
                    
                if final_crop.size == 0:
                    continue
                    
                # Determine main part for directory
                main_part = part_map[0]['path'] if part_map else ""
                for pm in part_map:
                    if pm['y_start'] <= y < pm['y_end']:
                        main_part = pm['path']
                        break
                        
                part_name = os.path.splitext(os.path.basename(main_part))[0]
                part_dir = os.path.join(state.project_dir, part_name)
                os.makedirs(part_dir, exist_ok=True)
                
                if part_dir not in manifests_by_part:
                    manifests_by_part[part_dir] = {}
                manifests_by_part[part_dir][f"panel_{idx}"] = "stitched2"
                
                out_raw = os.path.join(part_dir, f"panel_{idx}_raw.jpg")
                cv2.imwrite(out_raw, final_crop)

                # ── Bubble-clean this art panel ( narrated mode ) ──
                out = os.path.join(part_dir, f"panel_{idx}.jpg")
                cleaned_ok = False
                if text_remover is not None:
                    try:
                        img_bgr = final_crop
                        if text_remover._ocr_reader is not None:
                            mask, n_regions = text_remover._detect_via_ocr(img_bgr, text_remover._ocr_reader)
                        else:
                            mask = text_remover._detect_speech_bubbles_fallback(img_bgr)
                            n_regions = 1 if mask.any() else 0
                        if mask is not None and mask.any():
                            cleaned = cv2.inpaint(img_bgr, mask, inpaintRadius=5,
                                                  flags=cv2.INPAINT_TELEA)
                            cv2.imwrite(out, cleaned, [cv2.IMWRITE_JPEG_QUALITY, 95])
                            cleaned_ok = True
                            print(f"[crop2] panel_{idx}: cleaned {n_regions} bubble region(s)")
                    except Exception as _ce:
                        print(f"[crop2] panel_{idx}: cleaning failed ({_ce}) — keeping raw crop")
                if not cleaned_ok:
                    cv2.imwrite(out, final_crop)
                
                # Map to parent panel_with_text by finding which pwt range contains this box center
                center_y = y + h // 2
                parent_pwt = None
                for pwt_range in pwt_ranges:
                    if pwt_range['y_start'] <= center_y < pwt_range['y_end']:
                        parent_pwt = pwt_range
                        break
                
                parent_id = parent_pwt['panel_id'] if parent_pwt else None
                parent_path = parent_pwt['path'] if parent_pwt else None
                
                state.panels_mapping[str(idx)] = {
                    'parent_panel_id': parent_id,
                    'parent_path': parent_path,
                }
                
                state.panels.append({
                    'panel_id': idx,
                    'path': out,
                    'raw_path': out_raw,  # pre-cleaning crop (bubbles intact)
                    'padded_path': parent_path or out,  # Use parent panel-with-text as context
                    'source_page': main_part,
                    'width': w,
                    'height': h,
                    'x': x,
                    'y': y,
                    'parent_panel_id': parent_id,
                    'box_id': box.get('id', idx),
                })
                idx += 1
                
            for part_dir, manifest in manifests_by_part.items():
                manifest_path = os.path.join(part_dir, "panels_manifest.json")
                with open(manifest_path, 'w', encoding='utf-8') as f:
                    json.dump(manifest, f, indent=2)
            
            # Save mapping file
            mapping_path = os.path.join(state.project_dir, "panels_mapping.json")
            with open(mapping_path, 'w', encoding='utf-8') as f:
                json.dump(state.panels_mapping, f, indent=2)
            
            state.boxes2_cropped_hash = current_hash2
            state.stage = 'cropped2'
            state.error = None
            state.save()
            self._emit_progress('crop2', 1, 1, f"Cropped {len(state.panels)} art panels")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_extract_dialogue_text(self, state):
        """Extract dialogue text from panels-with-text using vision LLM, save to .txt."""
        if state.stage not in ('clips_done', 'cropped1', 'cropped2', 'stitched2', 'text_extracted',
                               'text_removed', 'narrated', 'audio_done', 'video_done'):
            state.error = "Must crop panels-with-text first."
            return state
        
        return self._run_batch_or_single(state, 'extract_dialogue', self._extract_dialogue_single)
    
    def _extract_dialogue_single(self, state):
        """Extract dialogue for a single chapter"""
        
        if not state.panels_with_text:
            state.error = "No panels-with-text available for text extraction."
            state.save()
            return state

        # ── SMART RESUME: skip if .txt already exists on disk ──
        txt_dir = os.path.join(config.EXTRACTED_TXT_DIR, state.manhwa_name)
        txt_path = os.path.join(txt_dir, f"Chapter_{state.current_chapter:05d}.txt")
        if self._stage_gte(state, 'text_extracted') and os.path.exists(txt_path):
            # Re-load text into panels_with_text so downstream steps have it
            with open(txt_path, 'r', encoding='utf-8') as f:
                raw = f.read()
            # Parse [PANEL N] blocks back into panels_with_text
            import re as _re
            blocks = _re.split(r'\[PANEL (\d+)\]', raw)
            text_by_id = {}
            for j in range(1, len(blocks) - 1, 2):
                pid = int(blocks[j])
                text_by_id[pid] = blocks[j+1].strip()
            for pwt in state.panels_with_text:
                pwt['text'] = text_by_id.get(pwt['panel_id'], '')  # Force update
            text_by_parent = {pwt['panel_id']: pwt.get('text', '') for pwt in state.panels_with_text}
            for panel in state.panels:
                pid = panel.get('parent_panel_id')
                if pid and pid in text_by_parent:
                    panel['text'] = text_by_parent[pid]  # Force update
            state.save()  # Save updated panels with text
            self._emit_progress('extract_dialogue', 1, 1,
                f"✅ Extract Text: loaded from cached file (skipping re-extraction)")
            return state
        
        model = state.settings.get('ollama_model', config.OLLAMA_MODEL)
        engine = state.settings.get('ocr_engine', 'qwen')
        
        self._emit_progress('extract_dialogue', 0, len(state.panels_with_text), "Extracting dialogue from panels-with-text...")
        try:
            print(f"[DEBUG] Starting extract_dialogue with {len(state.panels_with_text)} panels, engine={engine}")
            panels_for_extract = [{
                'panel_id': p['panel_id'],
                'path': p['path'],
                'padded_path': p['path'],  # The panel IS the padded context
            } for p in state.panels_with_text]
            
            print(f"[DEBUG] Calling extract_text_all_panels...")
            panels_result = extract_text_all_panels(
                panels_for_extract, state,
                model=model,
                engine=engine,
                progress_callback=lambda c, t, m: self._emit_progress('extract_dialogue', c, t, m))
            
            print(f"[DEBUG] extract_text_all_panels returned {len(panels_result)} panels")
            
            # Save extracted text to a .txt file in extracted_txt/title/chapter.txt
            os.makedirs(txt_dir, exist_ok=True)
            
            lines = []
            for p in panels_result:
                lines.append(f"[PANEL {p['panel_id']}]")
                lines.append(p.get('text', '(no dialogue)').strip() or '(no dialogue)')
                lines.append('')
            
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            
            print(f"[DEBUG] Saved text to {txt_path}")
            
            # Update panels_with_text with extracted text
            for i, p in enumerate(panels_result):
                if i < len(state.panels_with_text):
                    state.panels_with_text[i]['text'] = p.get('text', '')
                    state.panels_with_text[i]['visual_description'] = p.get('visual_description', '')
            
            # Also assign extracted text to art panels via mapping
            text_by_parent = {}
            for pwt in state.panels_with_text:
                text_by_parent[pwt['panel_id']] = pwt.get('text', '')
            
            for panel in state.panels:
                parent_id = panel.get('parent_panel_id')
                if parent_id and parent_id in text_by_parent:
                    panel['text'] = text_by_parent[parent_id]
            
            state.stage = 'text_extracted'
            state.error = None
            state.save()
            print(f"[DEBUG] extract_dialogue completed successfully")
        except Exception as e:
            print(f"[ERROR] extract_dialogue failed: {e}")
            import traceback
            traceback.print_exc()
            state.error = str(e)
            state.save()
            self._emit_progress('extract_dialogue', len(state.panels_with_text), len(state.panels_with_text), "Dialogue extraction complete")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_narrate_mapped(self, state):
        """Generate narration using Stitch 1 panels + Stitch 2 panels + extracted text.

        Sends to LLM:
          - Panel-with-text image (from Crop 1 / Stitch 1)
          - Art panel image (from Crop 2 / Stitch 2)
          - Extracted dialogue text

        Saves narration to: output/narration/Title/Chapter/
        Each narration entry is mapped to a Stitch 2 (art) panel.
        """
        if state.stage not in ('clips_done', 'text_extracted', 'cropped2', 'narrated',
                               'audio_done', 'video_done'):
            state.error = "Must extract text first."
            return state
        
        # Capture the per-request force flag BEFORE the batch loop —
        # switch_to_chapter() reloads per-chapter settings which would wipe it
        self._force_renarrate = bool(state.settings.pop('force_renarrate', False))
        try:
            return self._run_batch_or_single(state, 'narrate', self._narrate_mapped_single)
        finally:
            self._force_renarrate = False
    
    def _narrate_mapped_single(self, state):
        """Generate narration for a single chapter"""

        if not state.panels:
            state.error = "No art panels (Crop 2) available."
            state.save()
            return state

        narration_dir = os.path.join(config.NARRATION_DIR, state.manhwa_name, f"Chapter_{state.current_chapter:05d}")
        narration_json_path = os.path.join(narration_dir, "narration.json")

        # ── SMART RESUME: load narration.json — complete OR PARTIAL ──
        # A partial cache (e.g. Gemini quota ran out mid-chapter) is restored
        # so a re-run only narrates the MISSING panels instead of restarting.
        restored_partial = False
        # Explicit "Generate Narration" click → regenerate everything, no cache
        force_renarrate = getattr(self, '_force_renarrate', False)
        if force_renarrate:
            print("[NarrateMapped] Force re-narration requested — regenerating all panels")
            for panel in state.panels:
                panel['narration'] = ''
                panel['scene_narration'] = ''
        elif self._stage_gte(state, 'narrated') and os.path.exists(narration_json_path):
            try:
                with open(narration_json_path, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if cached:
                    narr_by_id = {e['panel_id']: (e.get('narration') or '') for e in cached}
                    dlg_by_id = {e['panel_id']: e.get('dialogue', '') for e in cached}
                    for panel in state.panels:
                        pid = panel['panel_id']
                        if pid in narr_by_id and narr_by_id[pid] and not narr_by_id[pid].startswith('['):
                            if not panel.get('narration'):
                                panel['narration'] = narr_by_id[pid]
                                panel['scene_narration'] = narr_by_id[pid]
                            if not panel.get('text'):
                                panel['text'] = dlg_by_id.get(pid, '')
                    missing = [p for p in state.panels if not (p.get('narration') or '').strip()
                               or (p.get('narration') or '').startswith('[')]
                    # Same backend + same prompt version + complete cache → skip.
                    # Backend switched (local ↔ gemini) OR the narration prompt
                    # was upgraded → re-narrate everything.
                    from pipeline.narrator import NARRATION_PROMPT_VERSION
                    same_style = (
                        (state.settings.get('narration_backend') or 'ollama') ==
                        (state.settings.get('last_narration_backend') or 'ollama') and
                        int(state.settings.get('narration_prompt_version') or 1) == NARRATION_PROMPT_VERSION
                    )
                    if not missing:
                        if same_style:
                            self._emit_progress('narrate_mapped', 1, 1,
                                f"✅ Narrate: loaded {len(cached)} cached narrations (skipping re-narration)")
                            return state
                        print("[NarrateMapped] Narration backend/prompt changed — re-narrating all panels")
                        for panel in state.panels:
                            panel['narration'] = ''
                            panel['scene_narration'] = ''
                    elif len(missing) < len(state.panels):
                        restored_partial = True
                        print(f"[NarrateMapped] Partial cache: {len(state.panels) - len(missing)} "
                              f"cached, re-narrating {len(missing)} missing panel(s)")
            except Exception as e:
                print(f"[NarrateMapped] Cache load failed: {e} — will re-narrate")
        
        # Load extracted dialogue text
        narration_dir = os.path.join(config.NARRATION_DIR, state.manhwa_name, f"Chapter_{state.current_chapter:05d}")
        os.makedirs(narration_dir, exist_ok=True)

        txt_dir = os.path.join(config.EXTRACTED_TXT_DIR, state.manhwa_name)
        txt_path = os.path.join(txt_dir, f"Chapter_{state.current_chapter:05d}.txt")
        dialogue_text = ""
        if os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8') as f:
                dialogue_text = f.read()
            print(f"[NarrateMapped] Loaded dialogue from {txt_path} ({len(dialogue_text)} chars)")
        else:
            print(f"[NarrateMapped] No dialogue file found at {txt_path}, proceeding without text")
        
        # Build text lookup by parent panel ID
        text_by_parent = {}
        for pwt in state.panels_with_text:
            text_by_parent[pwt['panel_id']] = pwt.get('text', '')
        
        # Parse chapter text into per-panel dialogue by [PANEL N] markers
        # This is the FALLBACK when parent_panel_id mapping is unavailable
        text_by_panel_index = {}
        if dialogue_text:
            import re
            panel_blocks = re.split(r'\[PANEL\s+(\d+)\]', dialogue_text)
            # panel_blocks = ['', '1', '\ncontent1\n', '2', '\ncontent2\n', ...]
            for j in range(1, len(panel_blocks) - 1, 2):
                try:
                    panel_num = int(panel_blocks[j])
                    content = panel_blocks[j + 1].strip()
                    if content and content != '(no dialogue)':
                        text_by_panel_index[panel_num] = content
                except (ValueError, IndexError):
                    pass
            if text_by_panel_index:
                print(f"[NarrateMapped] Parsed {len(text_by_panel_index)} panel dialogue blocks from chapter text")
        
        self._emit_progress('narrate_mapped', 0, len(state.panels), "Starting mapped narration...")

        try:
            # ── Narration backend choice ─────────────────────────────
            # 'ollama' (default) → local decoupled vision+writer models
            # 'gemini'           → cloud Gemini with MULTI-KEY quota rotation
            # The local path is untouched — Gemini is an additive option for
            # users whose PC can't run the local models comfortably.
            narration_backend = (state.settings.get('narration_backend') or 'ollama').lower()
            gemini_narr = None
            if narration_backend == 'gemini':
                from pipeline.gemini_narrator import GeminiNarrator, GeminiExhaustedError
                keys = state.settings.get('gemini_api_keys') or []
                gemini_model = state.settings.get('gemini_model', 'gemini-2.5-flash')
                gemini_narr = GeminiNarrator(
                    keys, model=gemini_model,
                    status_callback=lambda m: self._emit_progress(
                        'narrate_mapped', 0, len(state.panels), m))
                if not gemini_narr.keys:
                    state.error = ("Gemini narration selected but no API keys provided. "
                                   "Add one or more keys in the Narrate step.")
                    state.save()
                    return state
                print(f"[NarrateMapped] Backend: GEMINI ({gemini_model}) with "
                      f"{len(gemini_narr.keys)} key(s)")
            else:
                self.narrator.model = state.settings.get('ollama_model', config.OLLAMA_MODEL)
                self.narrator._resolve_model_name()
                print("[NarrateMapped] Backend: LOCAL OLLAMA")

            # Load character names from cast if a cast file is set
            cast_characters = self._load_cast_characters(state)
            cast_reference_images = self._load_cast_reference_images(state)
            if cast_reference_images:
                print(f"[NarrateMapped] Cast: {len(cast_characters)} characters, {len(cast_reference_images)} reference faces loaded")
            
            narration_entries = []
            
            for i, panel in enumerate(state.panels):
                if self._should_cancel: return state

                self._emit_progress('narrate_mapped', i + 1, len(state.panels),
                    f"Narrating panel {i + 1}/{len(state.panels)}...")

                # Partial-resume: keep narrations restored from cache, only
                # generate the missing/failed ones
                existing_narr = (panel.get('narration') or '').strip()
                if existing_narr and not existing_narr.startswith('['):
                    narration_entries.append({
                        'panel_id': panel['panel_id'],
                        'art_panel_path': panel['path'],
                        'parent_panel_path': (state.panels_mapping.get(str(panel['panel_id']), {}) or {}).get('parent_path'),
                        'dialogue': panel.get('text', ''),
                        'narration': existing_narr,
                        'backend': narration_backend,
                    })
                    continue

                # Get the parent panel-with-text for dialogue context
                parent_id = panel.get('parent_panel_id')
                parent_path = None
                panel_dialogue = ""
                
                if parent_id:
                    mapping = state.panels_mapping.get(str(panel['panel_id']), {})
                    parent_path = mapping.get('parent_path')
                    panel_dialogue = text_by_parent.get(parent_id, '')
                
                # FALLBACK: If no parent mapping, use the parsed chapter text by panel index
                if not panel_dialogue:
                    panel_dialogue = text_by_panel_index.get(panel['panel_id'], '')
                
                # Build the narration prompt with both images and text context
                art_panel_path = panel['path']
                # Build sliding window text context (Prev, Curr, Next)
                prev_txt = ""
                curr_txt = panel_dialogue
                next_txt = ""
                
                if i > 0:
                    prev_parent_id = state.panels[i-1].get('parent_panel_id')
                    if prev_parent_id:
                        prev_txt = text_by_parent.get(prev_parent_id, '')
                    if not prev_txt:
                        prev_txt = text_by_panel_index.get(state.panels[i-1]['panel_id'], '')
                if i < len(state.panels) - 1:
                    next_parent_id = state.panels[i+1].get('parent_panel_id')
                    if next_parent_id:
                        next_txt = text_by_parent.get(next_parent_id, '')
                    if not next_txt:
                        next_txt = text_by_panel_index.get(state.panels[i+1]['panel_id'], '')
                        
                sliding_window = f"--- PREVIOUS PANEL DIALOGUE ---\n{prev_txt}\n\n--- CURRENT PANEL DIALOGUE ---\n{curr_txt}\n\n--- NEXT PANEL DIALOGUE ---\n{next_txt}"
                
                try:
                    if gemini_narr is not None:
                        story_so_far = " ".join(
                            e['narration'] for e in narration_entries[-6:]
                            if not e['narration'].startswith('['))
                        narration = gemini_narr.narrate_panel(
                            art_panel_path=art_panel_path,
                            bubble_panel_path=parent_path,
                            dialogue_window=sliding_window,
                            cast_characters=cast_characters,
                            story_so_far=story_so_far,
                            panel_id=panel['panel_id'],
                            cast_reference_images=cast_reference_images,
                        )
                    else:
                        mock_panel = {
                            'path': art_panel_path,
                            'padded_path': parent_path or art_panel_path,
                            'panel_id': panel['panel_id'],
                            'text': panel_dialogue
                        }
                        narration = self.narrator.narrate_panel_with_context(
                            panel=mock_panel,
                            cast_characters=cast_characters,
                            chapter_text=sliding_window,
                            cast_reference_images=cast_reference_images,
                        )
                except Exception as e:
                    # All Gemini keys exhausted → stop with a clear message so
                    # the user can add more keys; nothing is lost (progress is
                    # saved after every panel).
                    from pipeline.gemini_narrator import GeminiExhaustedError
                    if isinstance(e, GeminiExhaustedError):
                        state.error = str(e)
                        state.save()
                        self._emit_progress('narrate_mapped', i, len(state.panels),
                                            f"⛔ {e}")
                        return state
                    print(f"[NarrateMapped] Error on panel {panel['panel_id']}: {e}")
                    narration = f"[Panel {panel['panel_id']}] (narration failed)"
                
                entry = {
                    'panel_id': panel['panel_id'],
                    'art_panel_path': art_panel_path,
                    'parent_panel_path': parent_path,
                    'dialogue': panel_dialogue,
                    'narration': narration,
                    'backend': narration_backend,
                }
                narration_entries.append(entry)
                
                # Also update the panel's narration field for downstream use
                panel['narration'] = narration
                panel['scene_narration'] = narration  # UI prefers scene_narration
                panel['text'] = panel_dialogue

                # Save continuously so the user can see live updates and not lose data if cancelled
                narration_json_path = os.path.join(narration_dir, "narration.json")
                with open(narration_json_path, 'w', encoding='utf-8') as f:
                    json.dump(narration_entries, f, indent=2, ensure_ascii=False)
                
                narration_txt_path = os.path.join(narration_dir, "narration.txt")
                lines = []
                for e in narration_entries:
                    lines.append(f"=== Panel {e['panel_id']} ===")
                    if e['dialogue']:
                        lines.append(f"[Dialogue] {e['dialogue']}")
                    lines.append(f"[Narration] {e['narration']}")
                    lines.append("")
                with open(narration_txt_path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(lines))
                
                script_path = os.path.join(state.project_dir, "narration_script.json")
                with open(script_path, 'w', encoding='utf-8') as f:
                    json.dump(narration_entries, f, indent=2, ensure_ascii=False)
            
            state.stage = 'narrated'
            state.error = None
            # Remember which backend produced these narrations so a re-run
            # with the SAME backend resumes from cache, while switching
            # backends (local ↔ gemini) re-narrates.
            state.settings['last_narration_backend'] = narration_backend
            from pipeline.narrator import NARRATION_PROMPT_VERSION
            state.settings['narration_prompt_version'] = NARRATION_PROMPT_VERSION
            state.save()
            print(f"[NarrateMapped] Saved narration for {len(narration_entries)} panels to {narration_dir}")
            self._emit_progress('narrate_mapped', len(state.panels), len(state.panels),
                f"Narration complete for {len(narration_entries)} panels")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_extract_text(self, state):
        """Extract text from each cropped panel using the vision LLM / Magi v2."""
        if state.stage not in ('clips_done', 'cropped1', 'cropped2', 'text_extracted'):
            state.error = "Must crop panels first."
            return state
        model = state.settings.get('ollama_model', config.OLLAMA_MODEL)
        engine = state.settings.get('ocr_engine', 'qwen')
        
        if not state.stitched_parts:
            state.error = "No stitched parts found for text extraction."
            return state
            
        self._emit_progress('extract_text', 0, len(state.panels), "Starting text extraction...")
        try:
            panels = extract_text_all_panels(
                state.panels, state,
                model=model,
                engine=engine,
                stitched_boxes=getattr(state, 'stitched_boxes', []),
                progress_callback=lambda c, t, m: self._emit_progress('extract_text', c, t, m))
            state.panels = panels
            state.stage = 'text_extracted'
            state.error = None
            state.save()
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_remove_text_boxes(self, state):
        """Remove text boxes from each panel and save clean versions."""
        if not state.is_batch:
            if state.stage not in ('clips_done', 'text_extracted', 'text_removed', 'cropped1', 'cropped2'):
                state.error = "Must extract text first (or at least crop panels)."
                return state
        return self._run_batch_or_single(state, 'remove_text', self._remove_text_boxes_single)
    
    def _remove_text_boxes_single(self, state):
        """Remove text boxes for a single chapter."""
        if state.stage not in ('clips_done', 'text_extracted', 'text_removed', 'cropped1', 'cropped2'):
            print(f"[remove_text] Skipping chapter {state.current_chapter} - stage is '{state.stage}'")
            return state
        self._emit_progress('remove_text', 0, len(state.panels), "Starting text box removal...")
        try:
            panels = remove_text_boxes(
                state.panels, state.project_dir,
                progress_callback=lambda c, t, m: self._emit_progress('remove_text', c, t, m))
            state.panels = panels
            state.stage = 'text_removed'
            state.error = None
            state.save()
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_narrate(self, state):
        if state.stage not in ('clips_done', 'cropped1', 'cropped2', 'text_extracted', 'text_removed', 'narrated'):
            state.error = "Must crop panels first."
            return state
        self.narrator.model = state.settings.get('ollama_model', config.OLLAMA_MODEL)
        self.narrator._resolve_model_name()
        self._emit_progress('narrate', 0, len(state.panels), "Starting AI narration...")

        # Load character names from cast if a cast file is set
        cast_characters = self._load_cast_characters(state)

        # Three-input context (port from YTV3):
        #   raw_dir       — full raw pages for visual context (Now uses source_page directly)
        #   text_file     — chapter transcript for story/dialogue context
        
        # Extracted text is completely bypassed. The visual narrator will read directly from the image.
        text_file = None

        try:
            if self._should_cancel: return state
            panels = self.narrator.narrate_all_panels(
                state.panels, state.project_dir,
                progress_callback=lambda c, t, m: self._emit_progress('narrate', c, t, m),
                cast_characters=cast_characters,
                raw_dir=None,
                text_file=text_file,
                manifest_file=None)
            state.panels = panels
            state.stage = 'narrated'
            state.error = None
            state.save()
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_generate_audio(self, state):
        if state.stage not in ('clips_done', 'narrated', 'audio_done', 'video_done', 'text_extracted', 'text_removed'):
            state.error = "Must generate narrations or extract text first."
            return state
        
        return self._run_batch_or_single(state, 'audio', self._generate_audio_single)
    
    def _generate_audio_single(self, state):
        """Generate audio for a single chapter"""

        # ── SMART RESUME: if audio_metadata.json exists, restore audio_path into panels ──
        # Metadata is written to state.audio_dir by tts_engine._save_audio_metadata;
        # keep the legacy project_dir path as a fallback for older projects.
        audio_meta_path = os.path.join(state.audio_dir, "audio_metadata.json")
        if not os.path.exists(audio_meta_path):
            audio_meta_path = os.path.join(state.project_dir, "audio_metadata.json")
        if self._stage_gte(state, 'audio_done') and os.path.exists(audio_meta_path):
            try:
                with open(audio_meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                meta = meta.get('panels', meta) if isinstance(meta, dict) else meta
                meta_by_id = {m['panel_id']: m for m in meta}
                all_present = all(
                    m.get('audio_path') and os.path.exists(m['audio_path'])
                    for m in meta if m.get('audio_path')
                )
                if all_present:
                    # Restore into whichever panel list audio was generated for
                    for panel in (state.panels + state.panels_with_text):
                        m = meta_by_id.get(panel['panel_id'], {})
                        if m.get('audio_path'):
                            panel['audio_path'] = m.get('audio_path')
                            panel['audio_duration'] = m.get('audio_duration', 2.0)
                            panel['subtitle_path'] = m.get('subtitle_path')
                    self._emit_progress('tts', 1, 1,
                        f"✅ Audio: loaded {len(meta)} cached files (skipping re-generation)")
                    return state
            except Exception as e:
                print(f"[TTS] Cache restore failed: {e} — will re-generate")

        # ── Pick the panel list + text field for THIS pipeline mode ──
        # narrated  → art panels (Crop 2) carry the AI narration
        # dialogue  → bubble panels (Crop 1) carry the extracted dialogue text
        mode = state.settings.get('pipeline_mode', '')
        panels_have_narration = bool(state.panels) and any(
            p.get('narration') for p in state.panels)

        if mode == 'narrated' or (not mode and panels_have_narration):
            text_field = 'narration'
            panels_for_audio = state.panels or state.panels_with_text
        elif mode == 'dialogue':
            text_field = 'text'
            panels_for_audio = state.panels_with_text or state.panels
        else:
            # Legacy data-driven fallback (no mode recorded)
            panels_to_check = state.panels_with_text if state.panels_with_text else state.panels
            text_field = 'narration'
            if panels_to_check and not panels_to_check[0].get('narration'):
                text_field = 'text'
            panels_for_audio = state.panels_with_text if state.panels_with_text and text_field == 'text' else state.panels

        self._emit_progress('tts', 0, len(panels_for_audio), "Starting TTS generation...")
        try:
            panels = generate_all_audio(panels_for_audio, state,
                voice=state.settings.get('tts_voice'),
                rate=state.settings.get('tts_rate'),
                pitch=state.settings.get('tts_pitch'),
                engine=state.settings.get('tts_engine', 'edge'),
                speed=state.settings.get('tts_speed', 1.0),
                text_field=text_field,
                progress_callback=lambda c, t, m: self._emit_progress('tts', c, t, m))

            # Update the correct panels array
            if panels_for_audio is state.panels_with_text:
                state.panels_with_text = panels
            else:
                state.panels = panels

            state.stage = 'audio_done'
            state.error = None
            state.save()
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_compose_video(self, state):
        if state.stage not in ('clips_done', 'audio_done', 'video_done', 'narrated'):
            state.error = "Must generate audio first."
            return state
        
        return self._run_batch_or_single(state, 'video', self._compose_video_single)
    
    def _compose_video_single(self, state):
        """Compose video for a single chapter"""

        # ── Pick the panel list for THIS pipeline mode ──
        # narrated  → clean art panels (Crop 2) with narration audio
        # dialogue  → bubble panels (Crop 1) with dialogue audio
        mode = state.settings.get('pipeline_mode', '')
        if mode == 'dialogue':
            panels_to_use = state.panels_with_text or state.panels
        elif mode == 'narrated':
            panels_to_use = state.panels or state.panels_with_text
        elif state.panels and any(p.get('narration') or p.get('audio_path') for p in state.panels):
            panels_to_use = state.panels  # narrated artifacts present
        else:
            panels_to_use = state.panels if state.panels else state.panels_with_text
        if not panels_to_use:
            state.error = "No panels available for video composition."
            state.save()
            return state

        # ── RESTORE AUDIO METADATA: Load audio_path into panels_to_use ──
        # Metadata lives in state.audio_dir (legacy: project_dir)
        audio_meta_path = os.path.join(state.audio_dir, "audio_metadata.json")
        if not os.path.exists(audio_meta_path):
            audio_meta_path = os.path.join(state.project_dir, "audio_metadata.json")
        if os.path.exists(audio_meta_path):
            try:
                with open(audio_meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                meta = meta.get('panels', meta) if isinstance(meta, dict) else meta
                meta_by_id = {m['panel_id']: m for m in meta}
                for panel in panels_to_use:
                    m = meta_by_id.get(panel['panel_id'], {})
                    if m.get('audio_path'):
                        panel['audio_path'] = m.get('audio_path')
                        panel['audio_duration'] = m.get('audio_duration', 2.0)
                        panel['subtitle_path'] = m.get('subtitle_path')
            except Exception as e:
                print(f"[Video] Failed to restore audio metadata: {e}")
            
        self._emit_progress('video', 0, len(panels_to_use), "Starting individual clip generation...")
        try:
            # ✅ RELIABILITY: pre-flight disk space check — failing at chapter 60
            # of a batch is far worse than stopping now with a clear message.
            try:
                needed_gb = max(2.0, len(panels_to_use) * 0.03)  # ~30MB per clip incl. temp files
                free_gb = shutil.disk_usage(state.clips_dir).free / (1024 ** 3)
                if free_gb < needed_gb:
                    state.error = (
                        f"Not enough disk space before generating clips: "
                        f"{free_gb:.1f} GB free, need ~{needed_gb:.1f} GB "
                        f"(for {len(panels_to_use)} clips + temp files). "
                        f"Free up space and re-run — finished chapters are cached "
                        f"and will be skipped.")
                    state.save()
                    return state
            except Exception:
                pass  # disk check is best-effort; never block rendering on it

            s = state.settings
            
            # Set background color settings in config for compositor to use
            if s.get('bg_color'):
                config.BACKGROUND_COLOR_HEX = s.get('bg_color')
            if s.get('color_opacity') is not None:
                config.COLOR_OVERLAY_OPACITY = s.get('color_opacity')
            
            # Handle custom background path
            custom_bg = s.get('custom_bg_path', '')
            if custom_bg and not os.path.isabs(custom_bg):
                custom_bg = os.path.join(config.BACKGROUNDS_DIR, custom_bg)
            
            # Generate individual clips instead of concatenating
            from pipeline.compositor import generate_individual_clips
            clip_metadata = generate_individual_clips(
                panels_to_use, state,
                resolution=s.get('video_resolution', '1080p'),
                fps=s.get('video_fps', 30),
                quality=s.get('video_quality', 'medium'),
                bg_mode=s.get('bg_mode', 'blur'),
                custom_bg_path=custom_bg,
                animation_style=s.get('animation_style', 'auto'),
                watermark_path=s.get('watermark_path', ''),
                progress_callback=lambda c, t, m: self._emit_progress('video', c, t, m))
            
            # Store clip metadata in state
            state.video_clips = clip_metadata
            state.stage = 'clips_done'
            state.error = None
            state.save()
            
            total_duration = sum(clip['duration'] for clip in clip_metadata)
            self._emit_progress('video', 1, 1,
                f"Generated {len(clip_metadata)} clips! Total duration: {total_duration:.1f}s")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    # ─── Scene Flow Steps (YouTube-style) ────────────────────────────

    def step_classify_panels(self, state):
        """Classify every panel as action / standard / epic and set target_duration."""
        valid = ('clips_done', 'cropped2', 'text_extracted', 'text_removed', 'narrated',
                 'audio_done', 'video_done')
        if state.stage not in valid:
            state.error = "Must crop art panels first (Crop 2)."
            return state
        if not state.panels:
            state.error = "No panels to classify."
            state.save()
            return state

        self._emit_progress('classify_panels', 0, 1, "Classifying panels (action / standard / epic)...")
        try:
            from pipeline.panel_classifier import classify_all_panels, get_classification_summary
            state.panels = classify_all_panels(state.panels, force=True)
            summary = get_classification_summary(state.panels)
            state.error = None
            state.save()
            msg = (f"\u2705 Classified {len(state.panels)} panels — "
                   f"action:{summary['action']} standard:{summary['standard']} epic:{summary['epic']}")
            self._emit_progress('classify_panels', 1, 1, msg)
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_narrate_scenes(self, state):
        """Generate YouTube-style scene-grouped narrations (1 narration per 3-5 panels)."""
        valid = ('clips_done', 'cropped2', 'text_extracted', 'text_removed', 'narrated',
                 'audio_done', 'video_done')
        if state.stage not in valid:
            state.error = "Must crop art panels first (Crop 2)."
            return state
        if not state.panels:
            state.error = "No panels available."
            state.save()
            return state

        s = state.settings
        host    = config.OLLAMA_HOST
        from pipeline.scene_narrator import SCENE_SIZE_DEFAULT
        scene_size = int(s.get('scene_size', SCENE_SIZE_DEFAULT))

        # Determine models for decoupled pipeline
        decoupled = getattr(config, 'DECOUPLED_NARRATION', True)
        if decoupled:
            # Phase 2 text model — use the dedicated text writer
            text_model = getattr(config, 'TEXT_WRITER_MODEL', 'qwen2.5:14b')
            # Resolve it against installed models
            text_model = self.narrator._resolve_model(text_model)
            # Phase 1 vision model
            vision_model = self.narrator.vision_model
            print(f"[NarrateScenes] DECOUPLED: Vision={vision_model}, Text={text_model}")
        else:
            text_model = s.get('ollama_model', config.OLLAMA_MODEL)
            vision_model = None
            print(f"[NarrateScenes] LEGACY: Model={text_model}")

        self._emit_progress('narrate_scenes', 0, 1, "Starting scene narration...")
        try:
            from pipeline.scene_narrator import narrate_all_scenes
            cast_characters = self._load_cast_characters(state)

            # Pre-populate panel dialogue from extracted chapter text
            # so scene_narrator._extract_panel_dialogue() has data to work with
            txt_dir = os.path.join(config.EXTRACTED_TXT_DIR, state.manhwa_name)
            txt_path = os.path.join(txt_dir, f"Chapter_{state.current_chapter:05d}.txt")
            if os.path.exists(txt_path):
                import re as _re
                with open(txt_path, 'r', encoding='utf-8') as f:
                    dialogue_text = f.read()
                panel_blocks = _re.split(r'\[PANEL\s+(\d+)\]', dialogue_text)
                text_by_idx = {}
                for j in range(1, len(panel_blocks) - 1, 2):
                    try:
                        pnum = int(panel_blocks[j])
                        content = panel_blocks[j + 1].strip()
                        if content and content != '(no dialogue)':
                            text_by_idx[pnum] = content
                    except (ValueError, IndexError):
                        pass
                assigned = 0
                for panel in state.panels:
                    if not panel.get('text'):
                        dlg = text_by_idx.get(panel['panel_id'], '')
                        if dlg:
                            panel['text'] = dlg
                            assigned += 1
                if assigned:
                    print(f"[NarrateScenes] Pre-populated dialogue for {assigned}/{len(state.panels)} panels from chapter text")

            state.panels = narrate_all_scenes(
                panels=state.panels,
                project_dir=state.project_dir,
                host=host,
                model=text_model,
                scene_size=scene_size,
                cast_characters=cast_characters,
                progress_callback=lambda c, t, m: self._emit_progress('narrate_scenes', c, t, m),
                cancel_check=lambda: self._should_cancel,
                vision_model=vision_model,
            )
            # Check if it returned early due to cancellation
            if self._should_cancel:
                self._emit_progress('narrate_scenes', 0, 1, "Cancelled scene narration.")
                return state
            # Always copy scene_narration → narration so the UI displays the latest
            for panel in state.panels:
                if panel.get('scene_narration'):
                    panel['narration'] = panel['scene_narration']
            state.stage = 'narrated'
            state.error = None
            state.settings['scene_mode'] = True
            state.save()
            n_scenes = len(set(p.get('scene_id','') for p in state.panels if p.get('scene_id')))
            self._emit_progress('narrate_scenes', 1, 1,
                f"\u2705 Scene narration done — {n_scenes} scenes, {len(state.panels)} panels tagged")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_generate_scene_audio(self, state):
        """Generate TTS for scene narrations (one audio file per scene)."""
        valid = ('clips_done', 'narrated', 'audio_done', 'video_done')
        if state.stage not in valid:
            state.error = "Must run Scene Narrate first."
            return state
        # Check panels have scene narrations
        has_scene = any(p.get('scene_narration') for p in state.panels)
        if not has_scene:
            state.error = "No scene narrations found. Run Scene Narrate first."
            state.save()
            return state

        self._emit_progress('scene_audio', 0, 1, "Generating scene audio...")
        try:
            from pipeline.scene_narrator import generate_scene_audio
            s = state.settings
            state.panels = generate_scene_audio(
                panels=state.panels,
                project_dir=state.project_dir,
                voice=s.get('tts_voice'),
                rate=s.get('tts_rate'),
                engine=s.get('tts_engine', 'edge'),
                speed=float(s.get('tts_speed', 1.0)),
                progress_callback=lambda c, t, m: self._emit_progress('scene_audio', c, t, m),
            )
            state.stage = 'audio_done'
            state.settings['scene_mode'] = True
            state.error = None
            state.save()
            n_audio = sum(1 for p in state.panels if p.get('scene_audio_path'))
            self._emit_progress('scene_audio', 1, 1,
                f"\u2705 Scene audio done — {n_audio} scene audio files generated")
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def step_export_scene_clips(self, state):
        """Render one MP4 per scene — fast, resumable, CapCut-ready."""
        valid = ('clips_done', 'narrated', 'audio_done', 'video_done')
        if state.stage not in valid:
            state.error = "Must generate scene audio first (C. Scene Audio)."
            return state
        has_scenes = any(p.get('scene_id') for p in state.panels)
        if not has_scenes:
            state.error = "No scene data found. Run A→B→C steps first."
            state.save()
            return state

        self._emit_progress('scene_clips', 0, 1, "Starting per-scene clip export...")
        try:
            from pipeline.compositor import export_scene_clips
            s = state.settings
            results = export_scene_clips(
                panels=state.panels,
                project_dir=state.project_dir,
                resolution=s.get('video_resolution', '1080p'),
                fps=s.get('video_fps', 30),
                quality=s.get('video_quality', 'medium'),
                bg_mode=s.get('bg_mode', 'blur'),
                animation_style=s.get('animation_style', 'auto'),
                progress_callback=lambda c, t, m: self._emit_progress('scene_clips', c, t, m),
            )
            done  = [r for r in results if r.get('path') and not r.get('error')]
            errs  = [r for r in results if r.get('error')]
            total_dur = sum(r.get('duration', 0) for r in done)
            state.stage = 'video_done'
            state.error = None
            state.save()
            msg = (f"\u2705 Scene clips done — {len(done)}/{len(results)} clips, "
                   f"total {total_dur/60:.1f} min")
            if errs:
                msg += f" ({len(errs)} failed)"
            self._emit_progress('scene_clips', len(results), len(results), msg)
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    def rewrite_script(self, state, style="dramatic", custom_instructions=None):
        self._emit_progress('rewrite', 0, len(state.panels), f"Rewriting in {style} style...")
        self.rewriter.model = state.settings.get('ollama_model', config.OLLAMA_MODEL)
        try:
            state.panels = self.rewriter.rewrite_all(state.panels, style,
                progress_callback=lambda c, t, m: self._emit_progress('rewrite', c, t, m))
            state.save()
        except Exception as e:
            state.error = str(e)
            state.save()
        return state

    # ─── CapCut Export ────────────────────────────────────────────
    def export_capcut(self, state, settings=None):
        return self.capcut.generate_project(state.panels, state.project_dir, settings)

    def export_subtitles(self, state):
        return self.capcut.get_subtitle_srt(state.panels, state.project_dir)

    # ─── Cast System ─────────────────────────────────────────────
    def export_cast(self, project_name):
        return export_project_cast(project_name)

    def _resolve_cast_file(self, state) -> str:
        """Cast name for this project (Character Builder folder under Cast Data)."""
        cast_file = state.settings.get('cast_file', '').strip()
        if not cast_file:
            title = state.project_name.split('___')[0]
            cast_file = title.lower().replace(' ', '-') + "-cast"
        return cast_file

    def _load_cast_reference_images(self, state) -> list:
        """[{name, gender, image_path}] — one reference face per cast member,
        used by vision narrators to recognise WHO is in a panel."""
        cast_file = self._resolve_cast_file(state)
        cast_data_dir = os.path.expanduser("~/RECAP/Cast Data")
        chars_file = os.path.join(cast_data_dir, cast_file, "characters.json")
        refs = []
        if os.path.exists(chars_file):
            try:
                import json
                with open(chars_file, 'r', encoding='utf-8') as f:
                    chars = json.load(f)
                if isinstance(chars, list):
                    chars = {c.get('id', f"char_{i}"): c for i, c in enumerate(chars)}
                for c in chars.values():
                    if not c.get('name'):
                        continue
                    for fname in (c.get('reference_images') or [])[:1]:
                        p = os.path.join(cast_data_dir, cast_file, 'faces', fname)
                        if os.path.exists(p):
                            refs.append({'name': c['name'],
                                         'gender': c.get('gender') or 'unknown',
                                         'image_path': p})
                            break
            except Exception as e:
                print(f"[Orchestrator] Error loading cast reference images: {e}")
        return refs

    def _load_cast_characters(self, state) -> list[str]:
        """Load character profiles from the cast file specified in project settings.

        Returns rich descriptive strings ('Ken Shadow (male, Main)') — not bare
        names — so every narration backend (local text writer, local vision
        writer, Gemini, Claude) knows who the character is and can pick the
        right pronouns."""
        cast_file = self._resolve_cast_file(state)

        profiles = {}   # name -> (gender, role) — dedupe by name
        # Check Character Builder characters.json first
        cast_data_dir = os.path.expanduser("~/RECAP/Cast Data")
        chars_file = os.path.join(cast_data_dir, cast_file, "characters.json")
        if os.path.exists(chars_file):
            try:
                import json
                with open(chars_file, 'r') as f:
                    chars = json.load(f)
                if isinstance(chars, list):
                    chars = {c.get('id', f"char_{i}"): c for i, c in enumerate(chars)}
                for char_data in chars.values():
                    name = char_data.get('name')
                    if not name:
                        continue
                    gender = (char_data.get('gender') or 'unknown').strip()
                    role = (char_data.get('role') or '').strip()
                    profiles[name] = (gender, role)
            except Exception as e:
                print(f"[Orchestrator] Error loading characters.json: {e}")

        try:
            # Try loading from cast directory as fallback/additive
            filepath = os.path.join(config.CAST_DIR, f"{cast_file}.cst")
            if not os.path.exists(filepath):
                filepath = cast_file  # Maybe it's a full path
            if os.path.exists(filepath):
                from pipeline.cast_system import Cast
                cast = Cast.load(filepath)
                for name in cast.characters:
                    if name not in profiles:
                        profiles[name] = ('unknown', '')
        except Exception as e:
            print(f"[Orchestrator] Error loading cast: {e}")

        out = []
        for name, (gender, role) in profiles.items():
            bits = [b for b in ((gender if gender and gender != 'unknown' else ''), role) if b]
            out.append(f"{name} ({', '.join(bits)})" if bits else name)
        return out

    # ─── Simplified Auto Pipeline (No Narration) ────────────────────
    def step_auto_pipeline(self, project_name, source_folder=None, settings=None):
        """
        Fully automatic pipeline: Load → Stitch → Detect 1 → Crop 1 →
        Extract Text → Audio (from text) → Video.

        No narration step. Extracted dialogue text goes directly to TTS.
        Output structure per chapter:
          texts/p{N}.txt    — extracted dialogue
          audio/p{N}.mp3    — TTS audio
          clips/clip_{N}.mp4 — per-panel video clip
          <Chapter>_recap.mp4 — final combined video
        """
        self._running = True
        self._should_cancel = False

        # Step 1: Load
        self._emit_progress('auto_pipeline', 0, 7, "Step 1/7: Loading images...")
        state = self.step_load_images(project_name, source_folder or '')
        if state.error or self._should_cancel:
            self._running = False
            return state
        if settings:
            state.settings.update(settings)
            state.save()

        # Step 2: Stitch
        self._emit_progress('auto_pipeline', 1, 7, "Step 2/7: Stitching images...")
        state = self.step_stitch(state)
        if state.error or self._should_cancel:
            self._running = False
            return state

        # Step 3: Detect 1 (bubble text model)
        self._emit_progress('auto_pipeline', 2, 7, "Step 3/7: Detecting panels (bubble text model)...")
        state = self.step_detect_bubble_text(state)
        if state.error or self._should_cancel:
            self._running = False
            return state

        # Step 4: Crop 1
        self._emit_progress('auto_pipeline', 3, 7, "Step 4/7: Cropping detected panels...")
        state = self.step_crop_bubble_text_panels(state)
        if state.error or self._should_cancel:
            self._running = False
            return state

        # Step 5: Extract dialogue text from panels using qwen2.5vl:3b
        self._emit_progress('auto_pipeline', 4, 7, "Step 5/7: Extracting text from panels (qwen2.5vl:3b)...")
        state = self.step_extract_dialogue_text(state)
        if state.error or self._should_cancel:
            self._running = False
            return state

        # Save per-panel text files
        texts_dir = os.path.join(state.project_dir, "texts")
        os.makedirs(texts_dir, exist_ok=True)
        for pwt in state.panels_with_text:
            pid = pwt['panel_id']
            txt_content = pwt.get('text', '(no dialogue)')
            txt_path = os.path.join(texts_dir, f"p{pid}.txt")
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write(txt_content)
        print(f"[AutoPipeline] Saved {len(state.panels_with_text)} text files to {texts_dir}")

        # Step 6: Generate audio from extracted text (not narration)
        self._emit_progress('auto_pipeline', 5, 7, "Step 6/7: Generating audio from extracted text...")
        try:
            from pipeline.tts_engine import generate_all_audio
            s = state.settings
            state.panels_with_text = generate_all_audio(
                state.panels_with_text, state.project_dir,
                voice=s.get('tts_voice'),
                rate=s.get('tts_rate'),
                engine=s.get('tts_engine', 'edge'),
                speed=s.get('tts_speed', 1.0),
                progress_callback=lambda c, t, m: self._emit_progress('tts', c, t, m),
                text_field='text',
            )
            state.stage = 'audio_done'
            state.error = None
            state.save()
        except Exception as e:
            state.error = f"Audio generation failed: {e}"
            state.save()
            self._running = False
            return state

        if self._should_cancel:
            self._running = False
            return state

        # Step 7: Compose video — per-panel clips + final combined video
        self._emit_progress('auto_pipeline', 6, 7, "Step 7/7: Composing video...")
        try:
            from pipeline.compositor import compose_video
            s = state.settings

            # Use panels_with_text as the visual/audio source
            video_panels = state.panels_with_text

            output_path = compose_video(
                video_panels, state.project_dir,
                resolution=s.get('video_resolution', '1080p'),
                fps=s.get('video_fps', 30),
                quality=s.get('video_quality', 'medium'),
                bgm_path=s.get('bgm_path', ''),
                watermark_path=s.get('watermark_path', ''),
                bg_mode=s.get('bg_mode', 'blur'),
                custom_bg_path=s.get('custom_bg_path', ''),
                intro_path=s.get('intro_path', ''),
                outro_path=s.get('outro_path', ''),
                export_clips=config.AUTO_PIPELINE_EXPORT_CLIPS,
                animation_style=s.get('animation_style', 'auto'),
                scene_mode=False,
                progress_callback=lambda c, t, m: self._emit_progress('video', c, t, m),
            )
            state.video_path = output_path
            state.stage = 'video_done'
            state.error = None
            state.save()

            from pipeline.compositor import get_video_info
            info = get_video_info(output_path)
            print(f"[AutoPipeline] Video complete: {output_path}")
            print(f"[AutoPipeline] Duration: {info.get('duration', 0):.1f}s, Size: {info.get('size_mb', 0):.1f}MB")
        except Exception as e:
            state.error = f"Video composition failed: {e}"
            state.save()

        self._running = False
        self._emit_progress('auto_pipeline', 7, 7, "✅ Auto pipeline complete!")
        return state

    # ─── Full Auto Pipeline (Legacy with Narration) ───────────────
    def run_full_pipeline(self, project_name, source_folder, settings=None):
        self._running = True
        self._should_cancel = False
        state = self.step_load_images(project_name, source_folder)
        if state.error or self._should_cancel:
            self._running = False
            return state
        if settings:
            state.settings.update(settings)
            state.save()
        state = self.step_stitch(state)
        if state.error or self._should_cancel:
            self._running = False
            return state
        panel_method = state.settings.get('panel_method', 'geometric')
        if panel_method == 'yolo':
            state = self.step_crop_panels_yolo(state)
        else:
            state = self.step_crop_panels(state)
        if state.error or self._should_cancel:
            self._running = False
            return state
        state = self.step_extract_text(state)
        if state.error or self._should_cancel:
            self._running = False
            return state
        state = self.step_remove_text_boxes(state)
        if state.error or self._should_cancel:
            self._running = False
            return state
        state = self.step_narrate(state)
        if state.error or self._should_cancel:
            self._running = False
            return state
        state = self.step_generate_audio(state)
        if state.error or self._should_cancel:
            self._running = False
            return state
        state = self.step_compose_video(state)
        self._running = False
        return state

    def resume_pipeline(self, project_name):
        state = PipelineState.load(project_name)
        self._running = True
        self._should_cancel = False
        stage_steps = {
            'loaded': [self.step_stitch, self._route_crop_panels, self.step_extract_text,
                       self.step_remove_text_boxes, self.step_narrate,
                       self.step_generate_audio, self.step_compose_video],
            'stitched': [self._route_crop_panels, self.step_extract_text,
                         self.step_remove_text_boxes, self.step_narrate,
                         self.step_generate_audio, self.step_compose_video],
            'cropped': [self.step_extract_text, self.step_remove_text_boxes,
                        self.step_narrate, self.step_generate_audio,
                        self.step_compose_video],
            'text_extracted': [self.step_remove_text_boxes, self.step_narrate,
                               self.step_generate_audio, self.step_compose_video],
            'text_removed': [self.step_narrate, self.step_generate_audio,
                             self.step_compose_video],
            'narrated': [self.step_generate_audio, self.step_compose_video],
            'audio_done': [self.step_compose_video],
        }
        for step_fn in stage_steps.get(state.stage, []):
            if self._should_cancel: break
            state = step_fn(state)
            if state.error: break
        self._running = False
        return state

    def _route_crop_panels(self, state):
        panel_method = state.settings.get('panel_method', 'geometric')
        if panel_method == 'yolo':
            return self.step_crop_panels_yolo(state)
        else:
            return self.step_crop_panels(state)


def _check_ffmpeg():
    import subprocess
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        version_line = result.stdout.split('\n')[0] if result.stdout else 'unknown'
        return {'available': True, 'version': version_line}
    except Exception:
        return {'available': False, 'version': None}
