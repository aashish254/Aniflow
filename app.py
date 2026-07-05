"""
AniFlow - Automated Manhwa Video Creation
Full-featured Flask app with all pipeline controls.
"""
import os, json, threading, shutil, re, sys

# Line-buffer stdout/stderr so redirected logs (server.log) update live
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
from flask import (Flask, render_template, request, jsonify, send_from_directory, send_file)
from flask_socketio import SocketIO, emit
import config
from pipeline.orchestrator import Orchestrator, PipelineState
from pipeline.narrator import Narrator
from pipeline.tts_engine import get_available_voices, get_tts_status
from pipeline.cast_system import Cast, list_casts
from pipeline.script_rewriter import ScriptRewriter
from pipeline.capcut_director import CapCutDirector
from pipeline.kokoro_tts import KOKORO_AVAILABLE
from pipeline.elevenlabs_tts import get_elevenlabs_tts, ELEVENLABS_AVAILABLE
from pipeline.panel_extractor_yolo import get_global_extractor, get_global_bubble_extractor
from pipeline.web_scraper import analyze_url, download_chapters, list_downloaded_manga, get_download_root
from pipeline import character_builder as charbuilder

app = Flask(__name__)
# Re-read templates from disk on every request so HTML edits (e.g. logo
# changes) show up without a server restart — Jinja caches compiled
# templates when debug is off, which made stale headers stick around
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'aniflow-dev-key')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB upload limit
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
orchestrator = Orchestrator()
narrator = Narrator()
pipeline_thread = None

def progress_callback(stage, current, total, message):
    socketio.emit('progress', {'stage': stage, 'current': current, 'total': total,
                               'message': message, 'percent': int((current / max(total, 1)) * 100)})

orchestrator.add_progress_callback(progress_callback)

# ─── Page Routes ─────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

# ─── API Routes ──────────────────────────────────────────────────
@app.route('/api/status', methods=['GET'])
def get_status():
    deps = orchestrator.check_dependencies()
    deps['tts'] = get_tts_status()
    return jsonify(deps)

@app.route('/api/projects', methods=['GET'])
def list_projects():
    return jsonify(orchestrator.get_projects())

@app.route('/api/projects/<name>', methods=['GET'])
def get_project(name):
    state = PipelineState.load(name)
    
    # IMMEDIATE FIX: Detect batch mode manually by checking folder structure
    # This bypasses the broken state loading logic
    # NOTE: use state.project_name (resolved to the real on-disk folder) —
    # the request name may be a display name like "Nan Hao Shang Feng"
    project_root = os.path.join(config.MANHWA_DOWNLOAD_DIR, state.project_name)  # resolved name
    manual_batch_check = False
    manual_chapter_list = [state.current_chapter]
    
    # DEBUG: Log exactly what we're checking
    print(f"[DEBUG] Checking path: {project_root}")
    print(f"[DEBUG] Path exists: {os.path.exists(project_root)}")
    if os.path.exists(project_root):
        all_items = os.listdir(project_root)
        print(f"[DEBUG] Contents: {all_items}")
        
        # Find Chapter_ folders that have actual images (not empty)
        chapter_dirs = []
        for d in all_items:
            if os.path.isdir(os.path.join(project_root, d)) and d.startswith('Chapter_'):
                chapter_path = os.path.join(project_root, d)
                # Check if chapter has images
                has_images = any(
                    f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))
                    for f in os.listdir(chapter_path)
                )
                if has_images:
                    chapter_dirs.append(d)
        
        print(f"[DEBUG] Chapter dirs with images: {chapter_dirs}")
        if len(chapter_dirs) > 1:
            manual_batch_check = True
            manual_chapter_list = sorted([
                int(d.replace('Chapter_', '')) for d in chapter_dirs
            ])
            print(f"[MANUAL BATCH FIX] ✅ Found {len(chapter_dirs)} chapters: {manual_chapter_list}")
        else:
            print(f"[DEBUG] Only {len(chapter_dirs)} chapter dirs found, not batch mode")
    else:
        print(f"[DEBUG] Project root doesn't exist")
    
    # Override state values with manual detection
    actual_is_batch = manual_batch_check
    actual_chapter_list = manual_chapter_list
    actual_manhwa_name = state.project_name.replace('-', ' ').replace('Dragonslayers', "Dragonslayer's")  # Fix display name
    
    return jsonify({
        'name': state.project_name, 'stage': state.stage, 'panels': state.panels,
        'raw_pages': [os.path.basename(p) for p in state.raw_pages],
        'stitched_parts': state.stitched_parts,
        'stitched_boxes': state.stitched_boxes,
        'stitched_boxes_1': state.stitched_boxes_1,
        'stitched_boxes_2': state.stitched_boxes_2,
        'stitched_parts_2': state.stitched_parts_2,
        'panels_with_text': state.panels_with_text,
        'panels_mapping': state.panels_mapping,
        'error': state.error, 'video_path': state.video_path,
        'settings': state.settings, 'created_at': state.created_at, 'updated_at': state.updated_at,
        # Chapter navigation info - USE MANUAL DETECTION
        'manhwa_name': actual_manhwa_name,
        'current_chapter': state.current_chapter,
        'is_batch': actual_is_batch,
        'chapter_list': actual_chapter_list,
    })

@app.route('/api/projects/<name>', methods=['DELETE'])
def delete_project(name):
    orchestrator.delete_project(name)
    return jsonify({'status': 'deleted'})

@app.route('/api/projects/<name>/settings', methods=['POST'])
def update_settings(name):
    state = PipelineState.load(name)
    state.settings.update(request.json)
    state.save()
    return jsonify({'status': 'updated', 'settings': state.settings})

@app.route('/api/hardware', methods=['GET'])
def hardware_info():
    """Hardware info so the Speed Mode panel can suggest sensible defaults."""
    import platform as _platform
    videotoolbox = False
    try:
        from pipeline.compositor import _probe_videotoolbox
        videotoolbox = _probe_videotoolbox(30)
    except Exception:
        videotoolbox = False
    return jsonify({
        'cpu_count': os.cpu_count() or 4,
        'platform': _platform.system(),
        'videotoolbox': videotoolbox,
    })

@app.route('/api/switch-chapter', methods=['POST'])
def switch_chapter():
    """Switch to a different chapter in batch processing."""
    data = request.json
    project_name = data.get('project_name')
    chapter_num = data.get('chapter_num')
    
    if not project_name or chapter_num is None:
        return jsonify({'error': 'project_name and chapter_num required'}), 400
    
    try:
        state = PipelineState.load(project_name)

        # MANUAL BATCH CHECK (same as get_project endpoint) — use the resolved
        # project name so display names with spaces still find the real folder
        project_root = os.path.join(config.MANHWA_DOWNLOAD_DIR, state.project_name)
        manual_batch_check = False
        manual_chapter_list = [state.current_chapter]
        
        if os.path.exists(project_root):
            # Find Chapter_ folders that have actual images (not empty)
            chapter_dirs = []
            for d in os.listdir(project_root):
                if os.path.isdir(os.path.join(project_root, d)) and d.startswith('Chapter_'):
                    chapter_path = os.path.join(project_root, d)
                    # Check if chapter has images
                    has_images = any(
                        f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))
                        for f in os.listdir(chapter_path)
                    )
                    if has_images:
                        chapter_dirs.append(d)
            
            if len(chapter_dirs) > 1:
                manual_batch_check = True
                manual_chapter_list = sorted([
                    int(d.replace('Chapter_', '')) for d in chapter_dirs
                ])
        
        # Use manual batch detection
        if not manual_batch_check:
            return jsonify({'error': 'Project is not a batch project'}), 400
        
        if chapter_num not in manual_chapter_list:
            return jsonify({'error': f'Chapter {chapter_num} not in chapter list'}), 400
        
        # Update state's chapter list with manual detection before switching
        state.is_batch = manual_batch_check
        state.chapter_list = manual_chapter_list
        
        state.switch_to_chapter(chapter_num)
        state.save()
        
        # Return full chapter state so frontend can update UI
        return jsonify({
            'status': 'switched',
            'current_chapter': state.current_chapter,
            'manhwa_name': state.manhwa_name,
            'chapter_list': state.chapter_list,
            'is_batch': state.is_batch,
            'stage': state.stage,
            'panels': state.panels,
            'panels_with_text': state.panels_with_text,
            'stitched_parts': state.stitched_parts,
            'stitched_boxes': state.stitched_boxes,
            'error': state.error,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/browse-folder', methods=['POST'])
def browse_folder():
    """Open native folder picker dialog."""
    try:
        import platform
        import subprocess
        
        if platform.system() == 'Darwin':  # macOS
            # Use AppleScript for folder picker on macOS
            script = '''
            tell application "System Events"
                activate
                set folderPath to choose folder with prompt "Select Output Folder for Sorted Clips"
                return POSIX path of folderPath
            end tell
            '''
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                folder_path = result.stdout.strip()
                if folder_path:
                    return jsonify({'path': folder_path})
                else:
                    return jsonify({'cancelled': True}), 200
            elif 'User canceled' in result.stderr:
                return jsonify({'cancelled': True}), 200
            else:
                return jsonify({'error': 'Folder selection failed'}), 400
        else:
            # Use tkinter for other platforms
            import tkinter as tk
            from tkinter import filedialog
            
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            
            folder_path = filedialog.askdirectory(
                title='Select Output Folder for Sorted Clips',
                mustexist=True
            )
            
            root.destroy()
            
            if folder_path:
                return jsonify({'path': folder_path})
            else:
                return jsonify({'cancelled': True}), 200
            
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Folder selection timeout'}), 500
    except ImportError:
        return jsonify({'error': 'tkinter not available - please enter path manually'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/projects/<name>/state', methods=['GET'])
def get_project_state(name):
    """Lightweight state read — just settings + panel metadata (no heavy image data)."""
    state = PipelineState.load(name)
    # Return a condensed panels list with only the fields the UI needs
    panels_slim = [
        {'panel_id': p.get('panel_id'), 'scene_id': p.get('scene_id')}
        for p in state.panels
    ]
    return jsonify({
        'stage': state.stage,
        'settings': state.settings,
        'panels': panels_slim,
        'video_path': state.video_path,
    })

# ─── Pipeline Execution ─────────────────────────────────────────
@app.route('/api/run/full', methods=['POST'])
def run_full_pipeline():
    global pipeline_thread
    data = request.json
    project_name = data.get('project_name', '').strip()
    source_folder = data.get('source_folder', '').strip()
    settings = data.get('settings', {})
    if not project_name:
        return jsonify({'error': 'Project name is required'}), 400
    if not source_folder or not os.path.isdir(source_folder):
        return jsonify({'error': 'Valid source folder path is required'}), 400
    def run():
        socketio.emit('pipeline_started', {'project': project_name})
        state = orchestrator.run_full_pipeline(project_name, source_folder, settings)
        socketio.emit('pipeline_complete', {'project': project_name, 'stage': state.stage,
                                            'error': state.error, 'video_path': state.video_path})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'project': project_name})

@app.route('/api/run/auto-pipeline', methods=['POST'])
def run_auto_pipeline():
    """Simplified auto pipeline: Load → Stitch → Detect 1 → Crop 1 →
    Extract Text → Audio (from text) → Video. No narration step."""
    global pipeline_thread
    if pipeline_thread and pipeline_thread.is_alive():
        return jsonify({'error': 'A pipeline is already running. Wait for it to finish.', 'status': 'busy'}), 409
    data = request.json
    project_name = data.get('project_name', '').strip()
    settings = data.get('settings', {})
    if not project_name:
        return jsonify({'error': 'Project name is required'}), 400
    def run():
        socketio.emit('pipeline_started', {'project': project_name, 'mode': 'auto'})
        state = orchestrator.step_auto_pipeline(project_name, settings=settings)
        socketio.emit('pipeline_complete', {'project': project_name, 'stage': state.stage,
                                            'error': state.error, 'video_path': state.video_path,
                                            'mode': 'auto'})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'project': project_name, 'mode': 'auto'})

@app.route('/api/run/load-and-process', methods=['POST'])
def run_load_and_process():
    """Run Load → Stitch → Crop → Extract Text → Remove Text in a single thread."""
    global pipeline_thread
    data = request.json
    project_name = data.get('project_name', '').strip()
    source_folder = data.get('source_folder', '').strip()
    settings = data.get('settings', {})
    steps = data.get('steps', ['stitch', 'crop', 'extract_text', 'remove_text'])
    if not project_name:
        return jsonify({'error': 'Project name is required'}), 400
    if not source_folder or not os.path.isdir(source_folder):
        return jsonify({'error': 'Valid source folder path is required'}), 400
    def run():
        # Step 1: Load
        socketio.emit('step_started', {'step': 'load', 'project': project_name})
        state = orchestrator.step_load_images(project_name, source_folder)
        if settings:
            state.settings.update(settings)
            state.save()
        socketio.emit('step_complete', {'step': 'load', 'project': project_name,
            'stage': state.stage, 'error': state.error,
            'panels_count': len(state.panels), 'video_path': state.video_path})
        if state.error:
            return

        # Step 2: Stitch
        if 'stitch' in steps:
            socketio.emit('step_started', {'step': 'stitch', 'project': project_name})
            state = orchestrator.step_stitch(state)
            socketio.emit('step_complete', {'step': 'stitch', 'project': project_name,
                'stage': state.stage, 'error': state.error,
                'panels_count': len(state.panels), 'video_path': state.video_path})
            if state.error:
                return

        # Step 3: Detect
        if 'detect' in steps:
            socketio.emit('step_started', {'step': 'detect', 'project': project_name})
            state = orchestrator.step_detect_panels_yolo(state)
            socketio.emit('step_complete', {'step': 'detect', 'project': project_name,
                'stage': state.stage, 'error': state.error,
                'panels_count': len(state.panels), 'video_path': state.video_path})
            if state.error:
                return

        # Step 4: Crop
        if 'crop' in steps:
            socketio.emit('step_started', {'step': 'crop', 'project': project_name})
            state = orchestrator.step_crop_extracted_panels(state)
            socketio.emit('step_complete', {'step': 'crop', 'project': project_name,
                'stage': state.stage, 'error': state.error,
                'panels_count': len(state.panels), 'video_path': state.video_path})
            if state.error:
                return

        # Step 5: Extract Text
        if 'extract_text' in steps:
            socketio.emit('step_started', {'step': 'extract_text', 'project': project_name})
            state = orchestrator.step_extract_text(state)
            socketio.emit('step_complete', {'step': 'extract_text', 'project': project_name,
                'stage': state.stage, 'error': state.error,
                'panels_count': len(state.panels), 'video_path': state.video_path})
            if state.error:
                return

        # Step 5: Remove Text Boxes
        if 'remove_text' in steps:
            socketio.emit('step_started', {'step': 'remove_text', 'project': project_name})
            state = orchestrator.step_remove_text_boxes(state)
            socketio.emit('step_complete', {'step': 'remove_text', 'project': project_name,
                'stage': state.stage, 'error': state.error,
                'panels_count': len(state.panels), 'video_path': state.video_path})

    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'project': project_name})

def _run_with_settings(step_fn, project_name, data):
    """Load state, patch settings from request data, then run step_fn."""
    state = PipelineState.load(project_name)
    settings_patch = data.get('settings', {})
    if settings_patch:
        state.settings.update(settings_patch)
        state.save()
    # Also accept top-level convenience keys
    for key in ('scene_size', 'scene_mode', 'ollama_model', 'tts_voice',
                'tts_engine', 'tts_speed', 'pipeline_mode'):
        if key in data:
            state.settings[key] = data[key]
    return step_fn(state)


def _load_with_settings(project_name, source_folder, settings):
    """Load images and persist any settings sent alongside (incl. ⚡ Speed Mode)."""
    state = orchestrator.step_load_images(project_name, source_folder)
    if settings:
        state.settings.update(settings)
        state.save()
    return state


@app.route('/api/run/step/<step_name>', methods=['POST'])
def run_step(step_name):
    global pipeline_thread
    # ✅ GUARD: never start any step while another one is running — overlapping
    # threads corrupt per-chapter state (this caused the batch-mode bugs)
    if pipeline_thread and pipeline_thread.is_alive():
        return jsonify({
            'error': f'A pipeline step is already running. Wait for it to finish before starting {step_name}.',
            'status': 'busy'
        }), 409
    data = request.json
    project_name = data.get('project_name', '')
    step_map = {
        'load': lambda: _load_with_settings(project_name, data.get('source_folder', ''), data.get('settings', {})),
        'stitch': lambda: orchestrator.step_stitch(PipelineState.load(project_name)),
        'detect': lambda: orchestrator.step_detect_panels_yolo(PipelineState.load(project_name)),
        'detect1': lambda: orchestrator.step_detect_bubble_text(PipelineState.load(project_name)),
        'crop1': lambda: orchestrator.step_crop_bubble_text_panels(PipelineState.load(project_name)),
        'stitch2': lambda: orchestrator.step_stitch_bubble_panels(PipelineState.load(project_name)),
        'detect2': lambda: orchestrator.step_detect_art_panels(PipelineState.load(project_name)),
        'crop2': lambda: orchestrator.step_crop_art_panels(PipelineState.load(project_name)),
        'crop': lambda: orchestrator.step_crop_extracted_panels(PipelineState.load(project_name)),
        'extract_text': lambda: orchestrator.step_extract_dialogue_text(PipelineState.load(project_name)),
        'extract_dialogue': lambda: orchestrator.step_extract_dialogue_text(PipelineState.load(project_name)),
        'remove_text': lambda: orchestrator.step_remove_text_boxes(PipelineState.load(project_name)),
        'narrate': lambda: orchestrator.step_narrate(PipelineState.load(project_name)),
        'narrate_mapped': lambda: _run_with_settings(orchestrator.step_narrate_mapped, project_name, data),
        'audio': lambda: _run_with_settings(orchestrator.step_generate_audio, project_name, data),
        'video': lambda: _run_with_settings(orchestrator.step_compose_video, project_name, data),
        # Scene Flow (YouTube-style)
        'classify_panels': lambda: orchestrator.step_classify_panels(PipelineState.load(project_name)),
        'narrate_scenes': lambda: _run_with_settings(orchestrator.step_narrate_scenes, project_name, data),
        'scene_audio': lambda: _run_with_settings(orchestrator.step_generate_scene_audio, project_name, data),
        'scene_clips': lambda: _run_with_settings(orchestrator.step_export_scene_clips, project_name, data),
    }
    if step_name not in step_map:
        return jsonify({'error': f'Unknown step: {step_name}'}), 400
    def run():
        orchestrator._should_cancel = False
        socketio.emit('step_started', {'step': step_name, 'project': project_name})
        state = step_map[step_name]()
        socketio.emit('step_complete', {'step': step_name, 'project': project_name,
            'stage': state.stage, 'error': state.error,
            'panels_count': len(state.panels), 'video_path': state.video_path})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'step': step_name})

# ─── 🚀 Full Batch Pipeline (one-click, all steps × all chapters) ───────

BATCH_STEP_FNS = {
    'stitch': 'step_stitch',
    'detect1': 'step_detect_bubble_text',
    'crop1': 'step_crop_bubble_text_panels',
    'stitch2': 'step_stitch_bubble_panels',
    'detect2': 'step_detect_art_panels',
    'crop2': 'step_crop_art_panels',
    'extract_dialogue': 'step_extract_dialogue_text',
    'remove_text': 'step_remove_text_boxes',
    'narrate_mapped': 'step_narrate_mapped',
    'audio': 'step_generate_audio',
    'video': 'step_compose_video',
}


def _notify_done(title, message):
    """Best-effort macOS notification + sound so long batches can run unattended."""
    try:
        import subprocess
        if sys.platform == 'darwin':
            safe_msg = message.replace('"', "'").replace('\\', '')[:200]
            safe_title = title.replace('"', "'")
            subprocess.run(
                ['osascript', '-e',
                 f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'],
                capture_output=True, timeout=10)
    except Exception as e:
        print(f"[Notify] Could not send notification: {e}")


def _infer_chapter_stage_light(manhwa_dir, ch_folder):
    """Read a chapter's progress WITHOUT creating any directories (used by the
    dashboard — safe to scan projects that were never opened). Prefers the
    authoritative state.json stage; falls back to scanning output folders."""
    manhwa_name = os.path.basename(manhwa_dir)
    state_path = os.path.join(manhwa_dir, ch_folder, 'state.json')
    saved_error = None
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                data = json.load(f)
            if data.get('stage'):
                return data['stage'], data.get('error')
            saved_error = data.get('error')
        except Exception:
            pass

    def count(dirpath, pred):
        try:
            return sum(1 for f in os.listdir(dirpath) if pred(f))
        except OSError:
            return 0

    img_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.img')
    if count(os.path.join(config.MANHWA_CLIPS_DIR, manhwa_name, ch_folder), lambda f: f.endswith('.mp4')):
        return 'clips_done', saved_error
    if count(os.path.join(config.MANHWA_AUDIO_DIR, manhwa_name, ch_folder), lambda f: f.endswith('.mp3')):
        return 'audio_done', saved_error
    if count(os.path.join(config.MANHWA_TEXT_DIR, manhwa_name, ch_folder), lambda f: f.endswith(('.txt', '.json'))):
        return 'text_extracted', saved_error
    if count(os.path.join(config.MANHWA_CROP_DIR, manhwa_name, ch_folder), lambda f: f.startswith('panel_') and f.endswith(('.jpg', '.png'))):
        return 'cropped1', saved_error
    if os.path.exists(os.path.join(config.MANHWA_DETECT_DIR, manhwa_name, ch_folder, 'detection_boxes.json')):
        return 'detected1', saved_error
    if count(os.path.join(config.MANHWA_STITCH_DIR, manhwa_name, ch_folder), lambda f: f.startswith('stitched_')):
        return 'stitched', saved_error
    if count(os.path.join(manhwa_dir, ch_folder), lambda f: f.lower().endswith(img_exts)):
        return 'loaded', saved_error
    return 'init', saved_error


@app.route('/api/batch-projects', methods=['GET'])
def batch_projects():
    """Batch dashboard: every manhwa in manhwa_download with per-chapter stages."""
    root = config.MANHWA_DOWNLOAD_DIR
    result = []
    if not os.path.isdir(root):
        return jsonify(result)
    for manhwa in sorted(os.listdir(root)):
        manhwa_dir = os.path.join(root, manhwa)
        if not os.path.isdir(manhwa_dir):
            continue
        ch_folders = sorted(
            d for d in os.listdir(manhwa_dir)
            if d.startswith('Chapter_') and os.path.isdir(os.path.join(manhwa_dir, d))
        )
        chapters = []
        saved_settings = None
        for chf in ch_folders:
            stage, saved_error = _infer_chapter_stage_light(manhwa_dir, chf)
            num_match = re.search(r'(\d+)', chf)
            images = sum(1 for f in os.listdir(os.path.join(manhwa_dir, chf))
                         if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.img'))) \
                if os.path.isdir(os.path.join(manhwa_dir, chf)) else 0
            # Pull saved pipeline settings from the most complete chapter
            if saved_settings is None:
                state_path = os.path.join(manhwa_dir, chf, 'state.json')
                if os.path.exists(state_path):
                    try:
                        with open(state_path) as f:
                            data = json.load(f)
                        if data.get('settings'):
                            saved_settings = data['settings']
                    except Exception:
                        pass
            chapters.append({
                'chapter': int(num_match.group(1)) if num_match else 0,
                'folder': chf,
                'stage': stage,
                'error': saved_error,
                'images': images,
            })
        if not chapters and not os.path.exists(os.path.join(manhwa_dir, 'manga_info.json')):
            continue  # skip unrelated folders with no chapter data at all
        # Hide phantom chapters (e.g. an auto-created empty Chapter_00000)
        chapters = [c for c in chapters if not (c['stage'] == 'init' and c['images'] == 0)]
        result.append({
            'name': manhwa,
            'display_name': manhwa.replace('-', ' '),
            'chapters': chapters,
            'total': len(chapters),
            'with_images': sum(1 for c in chapters if c['stage'] != 'init'),
            'settings': saved_settings or {},
        })
    return jsonify(result)


@app.route('/api/batch-projects/<name>', methods=['DELETE'])
def delete_batch_project(name):
    """🗑️ Wipe ALL pipeline data for a manhwa: downloads, stitched strips,
    detections, crops, text, audio and clips. Irreversible."""
    global pipeline_thread
    if pipeline_thread and pipeline_thread.is_alive():
        return jsonify({'error': 'A pipeline step is running — wait for it to finish before deleting.',
                        'status': 'busy'}), 409
    # Safety: reject path traversal and hidden/invalid names
    if (not name or '/' in name or '\\' in name or '..' in name
            or name.startswith('.') or name != name.strip()):
        return jsonify({'error': 'Invalid project name'}), 400

    roots = [config.MANHWA_DOWNLOAD_DIR, config.MANHWA_STITCH_DIR, config.MANHWA_DETECT_DIR,
             config.MANHWA_CROP_DIR, config.MANHWA_TEXT_DIR, config.MANHWA_AUDIO_DIR,
             config.MANHWA_CLIPS_DIR]
    removed = []
    for root in roots:
        target = os.path.join(root, name)
        # Double-check the target is a DIRECT child of the root (no traversal)
        if os.path.isdir(target) and os.path.dirname(os.path.realpath(target)) == os.path.realpath(root):
            shutil.rmtree(target, ignore_errors=True)
            removed.append(target)
    if not removed:
        return jsonify({'error': 'Project not found'}), 404
    print(f"[Delete] Wiped project '{name}' from {len(removed)} folders")
    return jsonify({'status': 'deleted', 'removed': len(removed)})


@app.route('/api/run/full-batch', methods=['POST'])
def run_full_batch():
    """🚀 One-click full pipeline: chains Load → Stitch → Detect → Crop →
    Extract → Audio → Clips across ALL chapters, with live progress events
    and a per-chapter summary at the end."""
    global pipeline_thread
    if pipeline_thread and pipeline_thread.is_alive():
        return jsonify({'error': 'A pipeline step is already running. Wait for it to finish.', 'status': 'busy'}), 409
    data = request.json or {}
    project_name = (data.get('project_name') or '').strip()
    settings = data.get('settings', {})
    source_folder = (data.get('source_folder') or '').strip()
    steps = data.get('steps') or ['stitch', 'detect1', 'crop1', 'extract_dialogue', 'audio', 'video']
    if not project_name:
        return jsonify({'error': 'project_name is required'}), 400

    def run():
        orchestrator._should_cancel = False
        state = PipelineState.load(project_name)
        if settings:
            state.settings.update(settings)
            state.save()
        config.SPEED_MODE = bool(state.settings.get('fast_mode', False))
        config.SPEED_SETTINGS = {'kokoro_gpu': bool(state.settings.get('kokoro_gpu', True))}

        total_chapters = len(state.chapter_list) if state.is_batch else 1
        socketio.emit('batch_pipeline_started', {
            'project': project_name, 'steps': steps, 'total_chapters': total_chapters})

        # Optional load step (Source page one-click flow)
        if source_folder:
            if orchestrator._should_cancel:
                socketio.emit('batch_pipeline_complete', {'project': project_name, 'cancelled': True})
                return
            socketio.emit('batch_step_started', {'step': 'load', 'project': project_name,
                                                 'total_chapters': total_chapters})
            state = orchestrator.step_load_images(project_name, source_folder)
            if settings:
                state.settings.update(settings)
                state.save()
            if getattr(state, 'error', None):
                socketio.emit('batch_pipeline_complete',
                              {'project': project_name, 'error': f"load: {state.error}"})
                _notify_done('Manhwa Batch Failed', f"{project_name}: load failed")
                return

        for step_name in steps:
            fn_name = BATCH_STEP_FNS.get(step_name)
            if not fn_name:
                continue
            if orchestrator._should_cancel:
                break
            socketio.emit('batch_step_started', {'step': step_name, 'project': project_name,
                                                 'total_chapters': total_chapters})
            state = PipelineState.load(project_name)
            state = getattr(orchestrator, fn_name)(state)
            if getattr(state, 'error', None):
                socketio.emit('batch_pipeline_complete',
                              {'project': project_name, 'error': f"{step_name}: {state.error}"})
                _notify_done('Manhwa Batch Failed', f"{project_name}: {step_name} failed")
                return

        cancelled = orchestrator._should_cancel

        # Per-chapter summary straight from disk (stage + error per chapter)
        chapters_summary = []
        if state.is_batch:
            for ch in state.chapter_list:
                ch_state = PipelineState.load_chapter(state.manhwa_name, ch)
                chapters_summary.append({'chapter': ch, 'stage': ch_state.stage,
                                         'error': ch_state.error})
        else:
            chapters_summary.append({'chapter': state.current_chapter,
                                     'stage': state.stage, 'error': state.error})

        done = sum(1 for c in chapters_summary if c['stage'] in ('clips_done', 'video_done'))
        if cancelled:
            socketio.emit('batch_pipeline_complete', {'project': project_name,
                                                      'cancelled': True, 'chapters': chapters_summary})
        else:
            socketio.emit('batch_pipeline_complete', {'project': project_name,
                                                      'chapters': chapters_summary,
                                                      'clips_ready': done})
            _notify_done('Manhwa Batch Complete',
                         f"{project_name}: {done}/{len(chapters_summary)} chapters ready")

    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'project': project_name})


@app.route('/api/browse-file', methods=['POST'])
def browse_file():
    """Open a native FILE picker (for custom background / watermark paths)."""
    try:
        import platform
        import subprocess
        if platform.system() == 'Darwin':
            script = '''
            tell application "System Events"
                activate
                set filePath to choose file with prompt "Select image"
                return POSIX path of filePath
            end tell
            '''
            result = subprocess.run(['osascript', '-e', script],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                return jsonify({'path': result.stdout.strip()})
            elif 'User canceled' in result.stderr:
                return jsonify({'cancelled': True}), 200
            return jsonify({'error': 'File selection failed'}), 400
        return jsonify({'error': 'File browser only supported on macOS — paste the path manually'}), 400
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'File selection timeout'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/run/batch', methods=['POST'])
def run_batch():
    global pipeline_thread
    if pipeline_thread and pipeline_thread.is_alive():
        pipeline_thread.join(timeout=2)
    data = request.json
    projects = data.get('projects', [])
    steps = data.get('steps', [])
    
    if not projects or not steps:
        return jsonify({'error': 'No projects or steps provided'}), 400
        
    def run():
        orchestrator._should_cancel = False
        socketio.emit('batch_started', {'steps': steps, 'total': len(projects)})
        for idx, p in enumerate(projects):
            if orchestrator._should_cancel:
                break
                
            project_name = p.get('name') if isinstance(p, dict) else p
            folder = p.get('folder') if isinstance(p, dict) else ''
            
            socketio.emit('batch_progress', {
                'current_idx': idx + 1, 'total': len(projects), 'project': project_name
            })
            
            # Create/Load state
            state = PipelineState.load(project_name)
            
            STAGE_ORDER = [
                None, 'new', 'loaded', 'stitched', 'detected1', 'cropped1', 
                'stitched2', 'detected2', 'cropped2', 'text_extracted', 'narrated',
                'audio_generated', 'video_generated'
            ]
            STEP_TO_STAGE = {
                'load': 'loaded',
                'stitch': 'stitched',
                'detect1': 'detected1',
                'crop1': 'cropped1',
                'stitch2': 'stitched2',
                'detect2': 'detected2',
                'crop2': 'cropped2',
                'extract_dialogue': 'text_extracted',
                'narrate_mapped': 'narrated'
            }
            
            for step_name in steps:
                if orchestrator._should_cancel: break
                
                # Skip if already done
                if state:
                    current_stage_idx = STAGE_ORDER.index(state.stage) if state.stage in STAGE_ORDER else 0
                    target_stage = STEP_TO_STAGE.get(step_name)
                    target_stage_idx = STAGE_ORDER.index(target_stage) if target_stage in STAGE_ORDER else 999
                    
                    if current_stage_idx >= target_stage_idx:
                        print(f"[Batch] Skipping '{step_name}' - project already at '{state.stage}'")
                        continue
                        
                socketio.emit('step_started', {'step': step_name, 'project': project_name})
                
                if step_name == 'load':
                    state = orchestrator.step_load_images(project_name, folder)
                elif step_name == 'stitch':
                    state = orchestrator.step_stitch(state)
                elif step_name == 'detect':
                    state = orchestrator.step_detect_panels_yolo(state)
                elif step_name == 'detect1':
                    state = orchestrator.step_detect_bubble_text(state)
                elif step_name == 'crop1':
                    state = orchestrator.step_crop_bubble_text_panels(state)
                elif step_name == 'stitch2':
                    state = orchestrator.step_stitch_bubble_panels(state)
                elif step_name == 'detect2':
                    state = orchestrator.step_detect_art_panels(state)
                elif step_name == 'crop2':
                    state = orchestrator.step_crop_art_panels(state)
                elif step_name == 'crop':
                    state = orchestrator.step_crop_extracted_panels(state)
                elif step_name == 'extract_text':
                    state = orchestrator.step_extract_dialogue_text(state)
                elif step_name == 'extract_dialogue':
                    state = orchestrator.step_extract_dialogue_text(state)
                elif step_name == 'remove_text':
                    state = orchestrator.step_remove_text_boxes(state)
                elif step_name == 'narrate':
                    state = orchestrator.step_narrate(state)
                elif step_name == 'narrate_mapped':
                    state = orchestrator.step_narrate_mapped(state)
                # handle error
                if getattr(state, 'error', None):
                    break
                    
        socketio.emit('batch_complete', {'steps': steps})
        
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'batch_started', 'count': len(projects)})

@app.route('/api/debug', methods=['GET'])
def debug_info():
    return jsonify({
        'should_cancel': getattr(orchestrator, '_should_cancel', False),
        'thread_alive': pipeline_thread.is_alive() if pipeline_thread else False
    })

@app.route('/api/run/resume/<project_name>', methods=['POST'])
def resume_pipeline(project_name):
    global pipeline_thread
    def run():
        socketio.emit('pipeline_started', {'project': project_name})
        state = orchestrator.resume_pipeline(project_name)
        socketio.emit('pipeline_complete', {'project': project_name, 'stage': state.stage,
                                            'error': state.error, 'video_path': state.video_path})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'resumed', 'project': project_name})

@app.route('/api/cancel', methods=['POST'])
def cancel_pipeline():
    orchestrator.cancel()
    return jsonify({'status': 'cancelling'})

# ─── AI Narration Backends ───────────────────────────────────────
@app.route('/api/gemini/validate-keys', methods=['POST'])
def gemini_validate_keys():
    """Validate each Gemini API key (multi-key support for cloud narration)."""
    data = request.json or {}
    keys = data.get('keys') or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.replace(',', '\n').split('\n')]
    from pipeline.gemini_narrator import validate_keys
    return jsonify({'results': validate_keys(keys)})


@app.route('/api/ai-backends', methods=['GET'])
def get_ai_backends():
    """Return available AI backends and their status."""
    backends = {
        'ollama': {'available': False, 'models': []},
        'gemini': {'available': True, 'models': ['gemini-2.5-flash', 'gemini-2.5-pro']},
        'claude': {'available': True, 'models': ['claude-sonnet-4-20250514', 'claude-opus-4-20250514']},
    }
    try:
        ollama_info = narrator.check_ollama()
        backends['ollama']['available'] = ollama_info.get('available', False)
        backends['ollama']['models'] = ollama_info.get('models', [])
    except Exception:
        pass
    return jsonify(backends)

@app.route('/api/narrate/gemini', methods=['POST'])
def narrate_with_gemini():
    """Generate narration using Google Gemini API."""
    global pipeline_thread
    data = request.json
    project_name = data.get('project_name', '')
    api_key = data.get('api_key', '')
    model = data.get('model', 'gemini-2.5-flash')

    if not project_name:
        return jsonify({'error': 'Project name is required'}), 400
    if not api_key:
        return jsonify({'error': 'Gemini API key is required'}), 400

    def run():
        socketio.emit('step_started', {'step': 'narrate', 'project': project_name})
        state = PipelineState.load(project_name)
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            gmodel = genai.GenerativeModel(model)

            total = len(state.panels)
            for i, panel in enumerate(state.panels):
                socketio.emit('progress', {
                    'stage': 'narrate', 'current': i + 1, 'total': total,
                    'message': f'Narrating panel {i+1}/{total}...',
                    'percent': int(((i + 1) / max(total, 1)) * 100)
                })
                text = panel.get('text', '') or panel.get('dialogue', '')
                if not text:
                    panel['narration'] = ''
                    continue
                prompt = f"""You are a professional manhwa narrator. Rewrite this dialogue into dramatic 3rd-person narration (2-4 sentences). Present tense, vivid, cinematic.

Dialogue: {text}

Write ONLY the narration, nothing else."""
                try:
                    response = gmodel.generate_content(prompt)
                    panel['narration'] = response.text.strip()
                except Exception as e:
                    panel['narration'] = text  # fallback to original text

            state.stage = 'narrated'
            state.error = None
            state.save()
            socketio.emit('step_complete', {'step': 'narrate', 'project': project_name,
                'stage': state.stage, 'error': None, 'panels_count': len(state.panels)})
        except ImportError:
            state.error = 'google-generativeai package not installed. Run: pip install google-generativeai'
            state.save()
            socketio.emit('step_complete', {'step': 'narrate', 'project': project_name,
                'stage': state.stage, 'error': state.error, 'panels_count': len(state.panels)})
        except Exception as e:
            state.error = f'Gemini narration failed: {e}'
            state.save()
            socketio.emit('step_complete', {'step': 'narrate', 'project': project_name,
                'stage': state.stage, 'error': state.error, 'panels_count': len(state.panels)})

    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'backend': 'gemini'})


@app.route('/api/narrate/claude', methods=['POST'])
def narrate_with_claude():
    """Generate narration using Anthropic Claude API."""
    global pipeline_thread
    data = request.json
    project_name = data.get('project_name', '')
    api_key = data.get('api_key', '')
    model = data.get('model', 'claude-sonnet-4-20250514')

    if not project_name:
        return jsonify({'error': 'Project name is required'}), 400
    if not api_key:
        return jsonify({'error': 'Claude API key is required'}), 400

    def run():
        socketio.emit('step_started', {'step': 'narrate', 'project': project_name})
        state = PipelineState.load(project_name)
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)

            total = len(state.panels)
            for i, panel in enumerate(state.panels):
                socketio.emit('progress', {
                    'stage': 'narrate', 'current': i + 1, 'total': total,
                    'message': f'Narrating panel {i+1}/{total}...',
                    'percent': int(((i + 1) / max(total, 1)) * 100)
                })
                text = panel.get('text', '') or panel.get('dialogue', '')
                if not text:
                    panel['narration'] = ''
                    continue
                try:
                    msg = client.messages.create(
                        model=model,
                        max_tokens=300,
                        messages=[{
                            'role': 'user',
                            'content': f"""You are a professional manhwa narrator. Rewrite this dialogue into dramatic 3rd-person narration (2-4 sentences). Present tense, vivid, cinematic.

Dialogue: {text}

Write ONLY the narration, nothing else."""
                        }]
                    )
                    panel['narration'] = msg.content[0].text.strip()
                except Exception as e:
                    panel['narration'] = text  # fallback

            state.stage = 'narrated'
            state.error = None
            state.save()
            socketio.emit('step_complete', {'step': 'narrate', 'project': project_name,
                'stage': state.stage, 'error': None, 'panels_count': len(state.panels)})
        except ImportError:
            state.error = 'anthropic package not installed. Run: pip install anthropic'
            state.save()
            socketio.emit('step_complete', {'step': 'narrate', 'project': project_name,
                'stage': state.stage, 'error': state.error, 'panels_count': len(state.panels)})
        except Exception as e:
            state.error = f'Claude narration failed: {e}'
            state.save()
            socketio.emit('step_complete', {'step': 'narrate', 'project': project_name,
                'stage': state.stage, 'error': state.error, 'panels_count': len(state.panels)})

    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'backend': 'claude'})


# ─── Panel Management ────────────────────────────────────────────
@app.route('/api/projects/<project_name>/boxes', methods=['POST'])
def save_stitched_boxes(project_name):
    state = PipelineState.load(project_name)
    boxes = request.json.get('boxes', [])
    state.stitched_boxes = boxes
    state.stitched_boxes_1 = boxes  # Also update boxes_1 for two-stage compat
    state.save()
    return jsonify({'status': 'saved'})

@app.route('/api/projects/<project_name>/boxes2', methods=['POST'])
def save_stitched_boxes_2(project_name):
    state = PipelineState.load(project_name)
    boxes = request.json.get('boxes', [])
    state.stitched_boxes_2 = boxes
    state.save()
    return jsonify({'status': 'saved'})

@app.route('/api/panels/<project_name>/<int:panel_id>/text', methods=['PUT'])
def update_panel_text(project_name, panel_id):
    """Update the EXTRACTED DIALOGUE text of a bubble panel (dialogue-mode
    review step). Keeps everything downstream in sync:
      - panels_with_text[i]['text']          (used by dialogue-mode TTS)
      - art panels' inherited 'text'         (narration context)
      - output/extracted_txt/... .txt cache  ([PANEL N] blocks)
    """
    state = PipelineState.load(project_name)
    new_text = (request.json.get('text') or '').strip()

    target = next((p for p in state.panels_with_text if p.get('panel_id') == panel_id), None)
    if not target:
        # Fall back: maybe the id refers to an art panel whose parent we edit
        art = next((p for p in state.panels if p.get('panel_id') == panel_id), None)
        parent_id = art.get('parent_panel_id') if art else None
        target = next((p for p in state.panels_with_text if p.get('panel_id') == parent_id), None)
    if not target:
        return jsonify({'error': f'Panel {panel_id} not found'}), 404

    target['text'] = new_text
    target['dialogue'] = new_text

    # Keep mapped art panels' inherited text in sync
    for p in state.panels:
        if p.get('parent_panel_id') == target['panel_id']:
            p['text'] = new_text

    # Rewrite the [PANEL N] blocks in the extracted-txt cache so narration
    # and any cached re-runs see the corrected dialogue
    try:
        txt_path = os.path.join(config.EXTRACTED_TXT_DIR, state.manhwa_name,
                                f"Chapter_{state.current_chapter:05d}.txt")
        if os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8') as f:
                raw = f.read()
            import re as _re
            blocks = _re.split(r'(\[PANEL \d+\])', raw)
            out, replaced = [], False
            for chunk in blocks:
                if chunk.startswith('[PANEL') and chunk.rstrip(']').lstrip('[PANEL ').isdigit():
                    pid = int(chunk.rstrip(']').lstrip('[PANEL '))
                    out.append(chunk)
                    replaced = (pid == target['panel_id'])
                elif replaced:
                    out.append('\n' + (new_text or '(no dialogue)') + '\n')
                    replaced = False
                else:
                    out.append(chunk)
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write(''.join(out))
    except Exception as e:
        print(f"[TextEdit] Extracted-txt sync failed: {e}")

    state.save()
    return jsonify({'status': 'updated', 'panel_id': panel_id, 'text': new_text})


@app.route('/api/panels/<project_name>/<int:panel_id>/narration', methods=['PUT'])
def update_narration(project_name, panel_id):
    state = PipelineState.load(project_name)
    new_narration = request.json.get('narration', '')
    state.panels = narrator.update_narration(state.panels, panel_id, new_narration, state.project_dir)
    # Keep the mapped-narration cache (output/narration/...) in sync so a
    # later re-run of Narrate doesn't clobber this manual edit
    try:
        narration_json = os.path.join(config.NARRATION_DIR, state.manhwa_name,
                                      f"Chapter_{state.current_chapter:05d}", "narration.json")
        if os.path.exists(narration_json):
            with open(narration_json, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            for entry in cached:
                if entry.get('panel_id') == panel_id:
                    entry['narration'] = new_narration
            with open(narration_json, 'w', encoding='utf-8') as f:
                json.dump(cached, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[NarrationEdit] Cache sync failed: {e}")
    state.save()
    return jsonify({'status': 'updated', 'panel_id': panel_id})

@app.route('/api/panels/<project_name>/<int:panel_id>/regenerate', methods=['POST'])
def regenerate_narration(project_name, panel_id):
    state = PipelineState.load(project_name)
    panel = next((p for p in state.panels if p['panel_id'] == panel_id), None)
    if not panel:
        return jsonify({'error': 'Panel not found'}), 404
    custom_prompt = request.json.get('prompt')

    # Re-narrate with the SAME context the batch narration used: cast profiles,
    # cast reference faces, the panel's dialogue and a sliding window over
    # neighbouring dialogue so the rewritten line still flows with the chapter.
    cast_characters = orchestrator._load_cast_characters(state)
    cast_reference_images = orchestrator._load_cast_reference_images(state)

    panels = state.panels
    idx = next((i for i, p in enumerate(panels) if p['panel_id'] == panel_id), -1)

    def _txt(i):
        if 0 <= i < len(panels):
            return panels[i].get('text', '') or ''
        return ''

    sliding_window = (
        f"--- PREVIOUS PANEL DIALOGUE ---\n{_txt(idx - 1)}\n\n"
        f"--- CURRENT PANEL DIALOGUE ---\n{_txt(idx)}\n\n"
        f"--- NEXT PANEL DIALOGUE ---\n{_txt(idx + 1)}"
    )

    new_narration = narrator.regenerate_panel(
        panel, custom_prompt,
        cast_characters=cast_characters,
        chapter_text=sliding_window,
        cast_reference_images=cast_reference_images)
    panel['narration'] = new_narration
    panel['scene_narration'] = new_narration
    state.save()
    return jsonify({'status': 'regenerated', 'narration': new_narration})

@app.route('/api/panels/<project_name>/<int:panel_id>', methods=['DELETE'])
def delete_panel(project_name, panel_id):
    """Delete a panel from the project — removes from state and deletes image file."""
    state = PipelineState.load(project_name)

    # Find and remove from panels list (check both panels and panels_with_text)
    panel = None
    for p in state.panels:
        if p.get('panel_id') == panel_id:
            panel = p
            break
    
    # Also check panels_with_text if not found in panels
    if not panel and state.panels_with_text:
        for p in state.panels_with_text:
            if p.get('panel_id') == panel_id:
                panel = p
                break

    if not panel:
        return jsonify({'error': f'Panel {panel_id} not found in panels or panels_with_text'}), 404

    # Delete image files from disk
    for key in ['path', 'clean_path', 'padded_path']:
        fpath = panel.get(key)
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
                print(f"[DeletePanel] Deleted {fpath}")
            except Exception as e:
                print(f"[DeletePanel] Could not remove {fpath}: {e}")

    # Remove from panels list
    deleted_art_id = panel_id if any(p.get('panel_id') == panel_id for p in state.panels) else None
    state.panels = [p for p in state.panels if p.get('panel_id') != panel_id]

    # Also remove from panels_with_text if present
    deleted_pwt_id = None
    if state.panels_with_text:
        deleted_pwt_id = panel_id if any(p.get('panel_id') == panel_id for p in state.panels_with_text) else None
        state.panels_with_text = [p for p in state.panels_with_text if p.get('panel_id') != panel_id]

    # Re-number remaining panels sequentially
    old_art_ids = {}   # new_id -> old_id
    for i, p in enumerate(state.panels):
        old_art_ids[i + 1] = p['panel_id']
        p['panel_id'] = i + 1
    if state.panels_with_text:
        pwt_old_to_new = {}  # old_id -> new_id
        for i, p in enumerate(state.panels_with_text):
            pwt_old_to_new[p['panel_id']] = i + 1
            p['panel_id'] = i + 1
        # Remap parent_panel_id references onto the new bubble-panel ids
        for p in state.panels:
            old_parent = p.get('parent_panel_id')
            if old_parent is not None:
                p['parent_panel_id'] = pwt_old_to_new.get(old_parent, old_parent)

    # Keep panels_mapping keys in sync with the re-numbered art panels
    if state.panels_mapping is not None and deleted_art_id is not None:
        new_mapping = {}
        for p in state.panels:
            old_id = old_art_ids.get(p['panel_id'], p['panel_id'])
            entry = state.panels_mapping.get(str(old_id))
            if entry:
                new_mapping[str(p['panel_id'])] = entry
        state.panels_mapping = new_mapping

    state.save()
    print(f"[DeletePanel] Panel {panel_id} deleted. Remaining: {len(state.panels)} panels, {len(state.panels_with_text or [])} panels_with_text")
    return jsonify({'status': 'deleted', 'remaining': len(state.panels) + len(state.panels_with_text or [])})

@app.route('/api/panels/<project_name>/<int:panel_id>/crop', methods=['POST'])
def crop_panel(project_name, panel_id):
    """Crop a panel image to the specified region. Expects JSON { x, y, w, h } in pixels."""
    from PIL import Image as PILImage
    state = PipelineState.load(project_name)

    # Check both panels and panels_with_text
    panel = next((p for p in state.panels if p.get('panel_id') == panel_id), None)
    if not panel and state.panels_with_text:
        panel = next((p for p in state.panels_with_text if p.get('panel_id') == panel_id), None)
    
    if not panel:
        return jsonify({'error': f'Panel {panel_id} not found. Total panels: {len(state.panels)}, panels_with_text: {len(state.panels_with_text or [])}'}), 404

    data = request.json
    x = int(data.get('x', 0))
    y = int(data.get('y', 0))
    w = int(data.get('w', 0))
    h = int(data.get('h', 0))

    if w <= 0 or h <= 0:
        return jsonify({'error': 'Invalid crop dimensions'}), 400

    # Crop the main panel image
    img_path = panel.get('path')
    if not img_path or not os.path.exists(img_path):
        return jsonify({'error': f'Panel image not found at {img_path}'}), 404

    try:
        img = PILImage.open(img_path)
        orig_w, orig_h = img.size
        # Clamp crop region to image bounds
        x2 = min(x + w, orig_w)
        y2 = min(y + h, orig_h)
        cropped = img.crop((max(0, x), max(0, y), x2, y2))
        cropped.save(img_path)
        new_w, new_h = cropped.size
        img.close()
        cropped.close()

        print(f"[CropPanel] Panel {panel_id} cropped from ({orig_w}x{orig_h}) to ({new_w}x{new_h})")

        # Also crop clean_path and padded_path if they exist
        for key in ['clean_path', 'padded_path']:
            fpath = panel.get(key)
            if fpath and os.path.exists(fpath):
                try:
                    alt_img = PILImage.open(fpath)
                    alt_cropped = alt_img.crop((max(0, x), max(0, y), x2, y2))
                    alt_cropped.save(fpath)
                    alt_img.close()
                    alt_cropped.close()
                except Exception as e:
                    print(f"[CropPanel] Could not crop {key}: {e}")

        state.save()
        return jsonify({'status': 'cropped', 'new_width': new_w, 'new_height': new_h})

    except Exception as e:
        return jsonify({'error': f'Crop failed: {str(e)}'}), 500


# ─── Script Rewriter ─────────────────────────────────────────────
@app.route('/api/rewrite/<project_name>', methods=['POST'])
def rewrite_script(project_name):
    global pipeline_thread
    data = request.json
    style = data.get('style', 'dramatic')
    custom = data.get('custom_instructions')
    def run():
        state = PipelineState.load(project_name)
        socketio.emit('step_started', {'step': 'rewrite', 'project': project_name})
        state = orchestrator.rewrite_script(state, style, custom)
        socketio.emit('step_complete', {'step': 'rewrite', 'project': project_name,
            'stage': state.stage, 'error': state.error, 'panels_count': len(state.panels)})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started', 'style': style})

@app.route('/api/rewrite/styles', methods=['GET'])
def get_rewrite_styles():
    return jsonify(ScriptRewriter.get_styles())

@app.route('/api/preview-audio/<path:filename>')
def serve_preview_audio(filename):
    """Serve preview audio files (supports subdirectories like kokoro/)."""
    preview_dir = os.path.join(config.PROJECTS_DIR, '_previews')
    return send_from_directory(preview_dir, filename)

@app.route('/api/preview-detection/<project_name>/<path:page_filename>', methods=['GET'])
def preview_detection(project_name, page_filename):
    state = PipelineState.load(project_name)
    raw_dir = os.path.join(state.project_dir, "raw_pages")
    image_path = os.path.join(raw_dir, page_filename)
    if not os.path.exists(image_path):
        return jsonify({'error': 'Image not found'}), 404
        
    try:
        extractor = get_global_extractor()
        jpeg_bytes = extractor.preview_page(image_path)
        return Response(jpeg_bytes, mimetype='image/jpeg')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Cast System ─────────────────────────────────────────────────
@app.route('/api/casts', methods=['GET'])
def get_casts():
    return jsonify(list_casts())

@app.route('/api/casts/export/<project_name>', methods=['POST'])
def export_cast(project_name):
    try:
        filepath = orchestrator.export_cast(project_name)
        return jsonify({'status': 'exported', 'path': filepath})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/casts/import', methods=['POST'])
def import_cast():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    filepath = os.path.join(config.CAST_DIR, f.filename)
    f.save(filepath)
    try:
        cast = Cast.load(filepath)
        return jsonify({'status': 'imported', 'cast': cast.to_dict()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/casts/<name>', methods=['GET'])
def get_cast(name):
    filepath = os.path.join(config.CAST_DIR, f"{name}.cst")
    if not os.path.exists(filepath):
        return jsonify({'error': 'Cast not found'}), 404
    cast = Cast.load(filepath)
    return jsonify(cast.to_dict())

@app.route('/api/casts/<name>', methods=['PUT'])
def update_cast(name):
    filepath = os.path.join(config.CAST_DIR, f"{name}.cst")
    data = request.json
    cast = Cast.from_dict(data)
    cast.save(filepath)
    return jsonify({'status': 'updated'})

@app.route('/api/casts/<name>', methods=['DELETE'])
def delete_cast(name):
    filepath = os.path.join(config.CAST_DIR, f"{name}.cst")
    if os.path.exists(filepath):
        os.remove(filepath)
    return jsonify({'status': 'deleted'})

@app.route('/api/casts/download/<name>')
def download_cast(name):
    filepath = os.path.join(config.CAST_DIR, f"{name}.cst")
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=f"{name}.cst")
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/casts/scan-manga', methods=['GET'])
def scan_manga_for_cast():
    """Scan downloaded manga directory and return available manga with chapter info."""
    root = get_download_root()
    if not os.path.exists(root):
        return jsonify([])
    manga_list = []
    for name in sorted(os.listdir(root)):
        manga_dir = os.path.join(root, name)
        if not os.path.isdir(manga_dir):
            continue
        chapters = []
        for ch_name in sorted(os.listdir(manga_dir)):
            ch_dir = os.path.join(manga_dir, ch_name)
            if not os.path.isdir(ch_dir) or not ch_name.startswith('Chapter'):
                continue
            images = [f for f in os.listdir(ch_dir) if f.lower().endswith(('.jpg','.jpeg','.png','.webp','.gif'))]
            chapters.append({'name': ch_name, 'image_count': len(images)})
        if chapters:
            # Load cover from metadata if available
            cover = None
            meta_path = os.path.join(manga_dir, 'manga_info.json')
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                        cover = meta.get('cover_url')
                except Exception:
                    pass
            manga_list.append({
                'name': name,
                'path': manga_dir,
                'chapters': chapters,
                'total_chapters': len(chapters),
                'total_images': sum(c['image_count'] for c in chapters),
                'cover_url': cover,
            })
    return jsonify(manga_list)

@app.route('/api/casts/build-from-manga', methods=['POST'])
def build_cast_from_manga():
    """Build character cast by scanning images from selected manga chapters.
    Sends images to Ollama vision model to detect characters."""
    global pipeline_thread
    data = request.json
    manga_path = data.get('manga_path', '')
    chapter_from = data.get('chapter_from', 1)
    chapter_to = data.get('chapter_to', 1)
    cast_name = data.get('cast_name', 'auto-cast')
    if not manga_path or not os.path.isdir(manga_path):
        return jsonify({'error': 'Invalid manga path'}), 400
    def run():
        socketio.emit('step_started', {'step': 'build_cast', 'project': 'cast'})
        try:
            # Collect all images from the selected chapter range
            all_images = []
            for ch_name in sorted(os.listdir(manga_path)):
                if not ch_name.startswith('Chapter') or not os.path.isdir(os.path.join(manga_path, ch_name)):
                    continue
                # Extract chapter number
                import re
                num_match = re.search(r'(\d+)', ch_name)
                if not num_match:
                    continue
                ch_num = int(num_match.group(1))
                if ch_num < chapter_from or ch_num > chapter_to:
                    continue
                ch_dir = os.path.join(manga_path, ch_name)
                for img_name in sorted(os.listdir(ch_dir)):
                    if img_name.lower().endswith(('.jpg','.jpeg','.png','.webp','.gif')):
                        all_images.append(os.path.join(ch_dir, img_name))

            if not all_images:
                socketio.emit('step_complete', {'step': 'build_cast', 'project': 'cast',
                    'stage': 'error', 'error': 'No images found in selected chapters'})
                return

            socketio.emit('progress', {'stage': 'build_cast', 'current': 0, 'total': len(all_images),
                'message': f'Scanning {len(all_images)} images for characters...', 'percent': 0})

            # Use Ollama to detect characters from a sample of images
            detected_chars = {}
            sample_size = min(len(all_images), 30)  # Analyse up to 30 images
            import random
            sample = sorted(random.sample(all_images, sample_size)) if len(all_images) > sample_size else all_images

            for idx, img_path in enumerate(sample):
                socketio.emit('progress', {'stage': 'build_cast', 'current': idx + 1, 'total': len(sample),
                    'message': f'Analyzing image {idx+1}/{len(sample)}...', 'percent': int(((idx+1)/len(sample))*100)})
                try:
                    chars = narrator.detect_characters(img_path)
                    for ch in chars:
                        name = ch.get('name', '').strip()
                        if name and name.lower() not in ('unknown', 'narrator', 'n/a', 'none', ''):
                            if name not in detected_chars:
                                detected_chars[name] = {'count': 0, 'description': ch.get('description', '')}
                            detected_chars[name]['count'] += 1
                except Exception as e:
                    print(f"[Cast] Error analyzing {img_path}: {e}")

            # Build the cast from detected characters
            cast = Cast(name=cast_name)
            voices = ['en-US-ChristopherNeural', 'en-US-GuyNeural', 'en-US-DavisNeural',
                       'en-US-JennyNeural', 'en-GB-RyanNeural', 'en-US-AriaNeural',
                       'en-US-JasonNeural', 'en-US-SaraNeural']
            for i, (char_name, info) in enumerate(sorted(detected_chars.items(), key=lambda x: -x[1]['count'])):
                voice = voices[i % len(voices)]
                cast.add_character(char_name, voice=voice)

            filepath = cast.save()
            socketio.emit('step_complete', {'step': 'build_cast', 'project': 'cast',
                'stage': 'done', 'error': None,
                'result': {
                    'cast_name': cast_name,
                    'characters': len(detected_chars),
                    'images_scanned': len(sample),
                    'detected': {k: v['count'] for k, v in detected_chars.items()},
                    'path': filepath,
                }})
        except Exception as e:
            socketio.emit('step_complete', {'step': 'build_cast', 'project': 'cast',
                'stage': 'error', 'error': str(e)})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started'})

# ─── Character Builder ───────────────────────────────────────────
@app.route('/api/charbuilder/scan', methods=['POST'])
def charbuilder_scan():
    """Scan manga chapters and extract face crops."""
    global pipeline_thread
    data = request.json
    manga_path = data.get('manga_path', '')
    ch_from = data.get('chapter_from', 1)
    ch_to = data.get('chapter_to', 3)
    if not manga_path or not os.path.isdir(manga_path):
        return jsonify({'error': 'Invalid manga path'}), 400
    def run():
        socketio.emit('progress', {'stage': 'char_scan', 'current': 0, 'total': 1, 'message': 'Scanning faces...', 'percent': 0})
        try:
            sid, faces = charbuilder.scan_faces(manga_path, ch_from, ch_to)
            socketio.emit('step_complete', {'step': 'char_scan', 'error': None,
                'result': {'session_id': sid, 'faces': faces, 'total': len(faces)}})
        except Exception as e:
            socketio.emit('step_complete', {'step': 'char_scan', 'error': str(e)})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started'})

@app.route('/api/charbuilder/face/<session_id>/<filename>')
def serve_scan_face(session_id, filename):
    p = charbuilder.get_face_image_path(session_id, filename)
    if os.path.exists(p):
        return send_file(p)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/charbuilder/charface/<cast_name>/<filename>')
def serve_char_face(cast_name, filename):
    p = charbuilder.get_char_face_path(cast_name, filename)
    if os.path.exists(p):
        return send_file(p)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/charbuilder/save', methods=['POST'])
def charbuilder_save():
    data = request.json
    char = charbuilder.save_character(
        data['cast_name'], data['name'], data['gender'],
        data['face_indices'], data['session_id'])
    return jsonify({'status': 'saved', 'character': char})

@app.route('/api/charbuilder/characters/<cast_name>')
def charbuilder_list(cast_name):
    return jsonify(charbuilder.load_characters(cast_name))

@app.route('/api/charbuilder/character/<cast_name>/<char_id>', methods=['DELETE'])
def charbuilder_delete(cast_name, char_id):
    charbuilder.delete_character(cast_name, char_id)
    return jsonify({'status': 'deleted'})


@app.route('/api/charbuilder/export/<cast_name>')
def charbuilder_export(cast_name):
    """Download a cast as a .cast.zip bundle (characters.json + face images)."""
    import zipfile
    # Safety: reject path traversal
    if (not cast_name or '/' in cast_name or '\\' in cast_name or '..' in cast_name):
        return jsonify({'error': 'Invalid cast name'}), 400
    cast_dir = os.path.join(charbuilder.CAST_DATA_DIR, cast_name)
    if not os.path.isdir(cast_dir):
        return jsonify({'error': 'Cast not found'}), 404
    zip_path = os.path.join(config.TEMP_DIR, f"{cast_name}.cast.zip")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(cast_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                zf.write(fpath, os.path.relpath(fpath, cast_dir))
    return send_file(zip_path, as_attachment=True, download_name=f"{cast_name}.cast.zip")


@app.route('/api/charbuilder/import/<cast_name>', methods=['POST'])
def charbuilder_import(cast_name):
    """Import a .cast.zip bundle produced by the export endpoint (merge into cast)."""
    import zipfile
    if (not cast_name or '/' in cast_name or '\\' in cast_name or '..' in cast_name):
        return jsonify({'error': 'Invalid cast name'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    cast_dir = os.path.join(charbuilder.CAST_DATA_DIR, cast_name)
    os.makedirs(cast_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(f) as zf:
            for member in zf.namelist():
                # Guard against zip-slip path traversal
                clean = os.path.normpath(member).lstrip('/\\')
                if clean.startswith('..') or ':' in clean:
                    continue
                target = os.path.join(cast_dir, clean)
                if member.endswith('/'):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
        chars = charbuilder.load_characters(cast_name)
        return jsonify({'status': 'imported', 'characters': len(chars)})
    except zipfile.BadZipFile:
        return jsonify({'error': 'Not a valid cast archive (.cast.zip)'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/charbuilder/character/<cast_name>/<char_id>', methods=['PUT'])
def charbuilder_update(cast_name, char_id):
    data = request.json
    char = charbuilder.update_character(cast_name, char_id, data.get('name'), data.get('gender'))
    return jsonify({'status': 'updated', 'character': char})

@app.route('/api/charbuilder/find-similar', methods=['POST'])
def charbuilder_find_similar():
    """Find faces similar to a reference face."""
    data = request.json
    session_id = data.get('session_id')
    ref_index = data.get('reference_index')
    all_faces = data.get('faces', [])
    threshold = data.get('threshold', 0.55)
    similar = charbuilder.find_similar_faces(session_id, ref_index, all_faces, threshold)
    return jsonify({'similar_indices': similar, 'count': len(similar)})

@app.route('/api/charbuilder/mal-search')
def charbuilder_mal_search():
    """Search MyAnimeList manga by name. Returns the first matches."""
    from pipeline.mal_character_importer import search_mal_manga
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'error': 'q is required'}), 400
    return jsonify({'results': search_mal_manga(q, limit=5)})


@app.route('/api/charbuilder/import-from-mal', methods=['POST'])
def charbuilder_import_from_mal():
    """Import characters from MyAnimeList into a cast."""
    try:
        from pipeline.mal_character_importer import scrape_characters_from_mal, import_characters_to_cast

        data = request.json
        mal_url = data.get('mal_url', '').strip()
        cast_name = data.get('cast_name', '').strip()
        max_characters = data.get('max_characters', 20)
        download_images = data.get('download_images', True)

        if not mal_url or not cast_name:
            return jsonify({'error': 'mal_url and cast_name are required'}), 400

        characters = scrape_characters_from_mal(mal_url, max_characters=max_characters)

        if not characters:
            return jsonify({'error': 'No characters found at that URL — check it opens in a browser'}), 404

        result = import_characters_to_cast(characters, cast_name, download_images=download_images)

        return jsonify({
            'status': 'success',
            'imported': result['imported'],
            'total': result['total'],
            'characters': [{'name': c['name'], 'role': c['role'],
                            'image': bool(c['reference_images'])}
                           for c in result['characters']],
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/charbuilder/add-bulk', methods=['POST'])
def charbuilder_add_bulk():
    """Manually add character names (comma/newline separated) to a cast."""
    try:
        from pipeline.mal_character_importer import add_bulk_characters
        data = request.json or {}
        cast_name = data.get('cast_name', '').strip()
        raw = data.get('names', '')
        if not cast_name:
            return jsonify({'error': 'cast_name is required'}), 400
        names = [n for n in re.split(r'[,\n]+', raw) if n.strip()]
        if not names:
            return jsonify({'error': 'No names provided'}), 400
        result = add_bulk_characters(names, cast_name)
        return jsonify({'status': 'success', 'added': result['added'],
                        'total': result['total']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── CapCut Director ─────────────────────────────────────────────
@app.route('/api/capcut/export/<project_name>', methods=['POST'])
def export_capcut(project_name):
    state = PipelineState.load(project_name)
    settings = request.json or {}
    project = orchestrator.export_capcut(state, settings)
    return jsonify({'status': 'exported', 'project': project})

@app.route('/api/capcut/subtitles/<project_name>', methods=['POST'])
def export_subtitles(project_name):
    state = PipelineState.load(project_name)
    srt_path = orchestrator.export_subtitles(state)
    return jsonify({'status': 'exported', 'path': srt_path})

@app.route('/api/capcut/animations', methods=['GET'])
def get_animations():
    return jsonify(CapCutDirector.get_animation_presets())

# ─── Export Endpoints ─────────────────────────────────────────────
@app.route('/api/export/narration/<project_name>', methods=['GET'])
def export_narration(project_name):
    state = PipelineState.load(project_name)
    narrations = [{'panel_id': p['panel_id'], 'narration': p.get('narration', ''),
                   'source_page': p.get('source_page', '')} for p in state.panels]
    output = os.path.join(state.project_dir, "narrations_export.json")
    with open(output, 'w') as f:
        json.dump(narrations, f, indent=2)
    return send_file(output, as_attachment=True, download_name=f"{project_name}_narrations.json")

@app.route('/api/export/panels/<project_name>', methods=['GET'])
def export_panels_zip(project_name):
    import zipfile
    state = PipelineState.load(project_name)
    zip_path = os.path.join(config.TEMP_DIR, f"{project_name}_panels.zip")
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for p in state.panels:
            if os.path.exists(p['path']):
                zf.write(p['path'], os.path.basename(p['path']))
    return send_file(zip_path, as_attachment=True, download_name=f"{project_name}_panels.zip")

# ─── File Serving ────────────────────────────────────────────────
@app.route('/api/panel-image/<project_name>/<filename>')
def serve_panel_image(project_name, filename):
    state = PipelineState.load(project_name)
    # Search all panel lists: panels, panels_with_text
    all_panels = (state.panels or []) + (state.panels_with_text or [])
    for p in all_panels:
        if os.path.basename(p.get('path', '')) == filename:
            if os.path.exists(p['path']):
                return send_file(p['path'])
    # Fallback: check crop_dir and project_dir directly
    for search_dir in [state.crop_dir, state.project_dir]:
        direct_path = os.path.join(search_dir, filename)
        if os.path.exists(direct_path):
            return send_file(direct_path)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/clean-panel-image/<project_name>/<filename>')
def serve_clean_panel_image(project_name, filename):
    """Serve a panel image with text boxes removed."""
    state = PipelineState.load(project_name)
    # Search all panel lists: panels, panels_with_text
    all_panels = (state.panels or []) + (state.panels_with_text or [])
    for p in all_panels:
        if os.path.basename(p.get('path', '')) == filename:
            clean_path = p.get('clean_path')
            if clean_path and os.path.exists(clean_path):
                return send_file(clean_path)
            # Fallback to original
            if os.path.exists(p['path']):
                return send_file(p['path'])
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/page-image/<project_name>/<filename>')
def serve_page_image(project_name, filename):
    return send_from_directory(os.path.join(config.PROJECTS_DIR, project_name, "raw_pages"), filename)

@app.route('/api/raw-page-image/<project_name>/<filename>')
def serve_raw_page_image(project_name, filename):
    """Alias for page-image — serves raw pages for the stitch preview thumbnail strip."""
    return send_from_directory(os.path.join(config.PROJECTS_DIR, project_name, "raw_pages"), filename)

@app.route('/api/panel-text/<project_name>')
def serve_panel_texts(project_name):
    """Serve the extracted panel texts file."""
    txt_path = os.path.join(config.PROJECTS_DIR, project_name, "panel_texts.txt")
    if os.path.exists(txt_path):
        return send_file(txt_path, mimetype='text/plain')
    return jsonify({'error': 'Panel texts not found'}), 404

@app.route('/api/audio/<project_name>/<filename>')
def serve_audio(project_name, filename):
    state = PipelineState.load(project_name)
    audio_dir = os.path.join(state.project_dir, "audio")
    return send_from_directory(audio_dir, filename)

@app.route('/api/stitched-image/<project_name>', methods=['GET', 'HEAD'])
def get_stitched_image(project_name):
    state = PipelineState.load(project_name)
    print(f"[get_stitched_image] Loaded chapter {state.current_chapter}, is_batch={state.is_batch}")
    print(f"[get_stitched_image] stitched_parts: {state.stitched_parts[:2] if state.stitched_parts else 'None'}")
    
    if hasattr(state, 'stitched_path') and state.stitched_path and os.path.exists(state.stitched_path):
        return jsonify({'parts': [f"/api/raw-image?path={urllib.parse.quote(state.stitched_path)}"]})
    if state.stitched_parts:
        import urllib.parse
        # ✅ PERFORMANCE: version the URLs by content mtime — the browser caches
        # chapter images across switches, and re-stitching (new mtime) busts it.
        try:
            version = int(max(os.path.getmtime(p) for p in state.stitched_parts
                              if os.path.exists(p)) * 1000)
        except Exception:
            version = 0
        parts_urls = [f"/api/raw-image?path={urllib.parse.quote(p)}&v={version}"
                      for p in state.stitched_parts]
        print(f"[get_stitched_image] Returning {len(parts_urls)} parts for chapter {state.current_chapter}")
        return jsonify({'parts': parts_urls})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/stitched-image-2/<project_name>', methods=['GET', 'HEAD'])
def get_stitched_image_2(project_name):
    """Serve Stitched Image 2 parts (re-stitched panels-with-text)."""
    state = PipelineState.load(project_name)
    if state.stitched_parts_2:
        import urllib.parse
        parts_urls = [f"/api/raw-image?path={urllib.parse.quote(p)}" for p in state.stitched_parts_2]
        return jsonify({'parts': parts_urls})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/raw-image')
def serve_raw_image():
    import urllib.parse
    path = request.args.get('path')
    if path and os.path.exists(path):
        return send_file(path)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/stitched-detected/<project_name>', methods=['GET', 'HEAD'])
def get_stitched_detected_image(project_name):
    state = PipelineState.load(project_name)
    if hasattr(state, 'stitched_preview_path') and state.stitched_preview_path and os.path.exists(state.stitched_preview_path):
        return send_file(state.stitched_preview_path)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/video/<project_name>')
def serve_video(project_name):
    state = PipelineState.load(project_name)
    if state.video_path and os.path.exists(state.video_path):
        return send_file(state.video_path, mimetype='video/mp4')
    return jsonify({'error': 'Video not found'}), 404

@app.route('/api/download/<project_name>')
def download_video(project_name):
    state = PipelineState.load(project_name)
    if state.video_path and os.path.exists(state.video_path):
        return send_file(state.video_path, as_attachment=True, download_name=f"{project_name}_recap.mp4")
    return jsonify({'error': 'Video not found'}), 404

@app.route('/api/clips/<project_name>')
def get_clips(project_name):
    """Get list of generated clips for a project"""
    try:
        import re
        
        state = PipelineState.load(project_name)
        
        # Use the state's clips directory (new organized structure)
        clips_dir = state.clips_dir
        
        # Fallback to old locations if clips_dir doesn't exist
        if not os.path.exists(clips_dir):
            # Try old location: project_dir/clips
            old_clips_dir = os.path.join(state.project_dir, 'clips')
            if os.path.exists(old_clips_dir):
                clips_dir = old_clips_dir
            else:
                # Try alternative location: output/manhwa_panel/{project}/clips
                alt_clips_dir = os.path.join(config.OUTPUT_DIR, project_name, 'clips')
                if os.path.exists(alt_clips_dir):
                    clips_dir = alt_clips_dir
        
        # Load panels to get text
        panels_map = {}
        if state.panels_with_text:
            for p in state.panels_with_text:
                pid = p.get('panel_id')
                if pid:
                    panels_map[pid] = p.get('narration') or p.get('text') or p.get('dialogue') or ''
        
        # Scan directory for .mp4 files
        clips = []
        if os.path.exists(clips_dir):
            for filename in sorted(os.listdir(clips_dir)):
                if filename.endswith('.mp4'):
                    filepath = os.path.join(clips_dir, filename)
                    
                    # Extract panel_id from filename (e.g., "scene_0001.mp4" -> 1)
                    match = re.search(r'scene_(\d+)', filename)
                    panel_id = int(match.group(1)) if match else 0
                    
                    # Get duration from video file (optional, skip if moviepy not available)
                    duration = 0
                    try:
                        from moviepy.editor import VideoFileClip
                        clip = VideoFileClip(filepath)
                        duration = clip.duration
                        clip.close()
                    except Exception as e:
                        # If moviepy not available or fails, estimate from file size
                        # Average bitrate assumption: ~1MB per 6 seconds
                        file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
                        duration = file_size_mb * 6  # Rough estimate
                    
                    clips.append({
                        'clip_filename': filename,
                        'filename': filename,  # For backward compatibility
                        'panel_id': panel_id,
                        'size': os.path.getsize(filepath),
                        'duration': duration,
                        'text': panels_map.get(panel_id, ''),
                        'url': f'/api/clips/{project_name}/{filename}'
                    })
        
        return jsonify({
            'clips': clips,
            'total': len(clips),
            'clips_dir': clips_dir
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/clips/<project_name>/<clip_filename>')
def serve_clip(project_name, clip_filename):
    """Serve an individual clip file"""
    try:
        state = PipelineState.load(project_name)
        
        # Use the new organized structure
        clip_path = os.path.join(state.clips_dir, clip_filename)
        if os.path.exists(clip_path):
            return send_file(clip_path, mimetype='video/mp4')
        
        # Fallback to old structure
        possible_dirs = [
            os.path.join(state.project_dir, 'clips'),
            os.path.join(config.OUTPUT_DIR, project_name, 'clips')
        ]
        
        for clips_dir in possible_dirs:
            clip_path = os.path.join(clips_dir, clip_filename)
            if os.path.exists(clip_path):
                return send_file(clip_path, mimetype='video/mp4')
        
        return jsonify({'error': 'Clip not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Sort Clips ──────────────────────────────────────────────────
@app.route('/api/sort-clips', methods=['POST'])
def sort_clips():
    """
    Copy and rename clips from multiple chapters into a single folder with sequential numbering.
    
    Request body:
    {
        "output_folder": "/path/to/output",
        "start_chapter": 1,
        "end_chapter": 5
    }
    """
    try:
        import re
        import glob
        from pathlib import Path
        
        data = request.json
        output_folder = data.get('output_folder', '').strip()
        start_chapter = int(data.get('start_chapter', 1))
        end_chapter = int(data.get('end_chapter', 1))
        
        if not output_folder:
            return jsonify({'error': 'Output folder is required'}), 400
        
        if start_chapter > end_chapter:
            return jsonify({'error': 'Start chapter must be <= end chapter'}), 400
        
        # Create output folder
        output_path = Path(output_folder)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Find all project folders that match the chapter range
        projects_dir = Path(config.PROJECTS_DIR)
        clips_dir = Path(config.MANHWA_CLIPS_DIR)
        
        all_clips = []
        clip_counter = 1
        
        for chapter_num in range(start_chapter, end_chapter + 1):
            # Look for clips matching this chapter number
            # Format: Chapter_00000, Chapter_00001, etc.
            chapter_folder_name = f"Chapter_{chapter_num:05d}"
            
            found_clips = False
            
            # Search in manhwa_clips directory for all projects
            for project_dir in clips_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                    
                # Look for the chapter folder inside this project
                chapter_path = project_dir / chapter_folder_name
                
                if chapter_path.exists() and chapter_path.is_dir():
                    # Get all .mp4 files sorted by name
                    mp4_files = sorted(chapter_path.glob("*.mp4"))
                    
                    for mp4_file in mp4_files:
                        # Copy with sequential naming
                        new_filename = f"{clip_counter:04d}.mp4"
                        new_path = output_path / new_filename
                        
                        # Copy the file
                        shutil.copy2(str(mp4_file), str(new_path))
                        
                        all_clips.append({
                            'original': str(mp4_file),
                            'new': str(new_path),
                            'chapter': chapter_num,
                            'sequence': clip_counter
                        })
                        
                        clip_counter += 1
                    
                    found_clips = True
            
            if not found_clips:
                print(f"[Sort] No clips found for chapter {chapter_num} (looking for {chapter_folder_name})")
        
        if len(all_clips) == 0:
            return jsonify({
                'error': f'No clips found for chapters {start_chapter}-{end_chapter}. Make sure you have generated video clips first (step 7-9).',
                'searched_in': str(clips_dir),
                'expected_format': f'Chapter_{start_chapter:05d}'
            }), 404
        
        return jsonify({
            'success': True,
            'total_clips': len(all_clips),
            'copied_clips': len(all_clips),
            'output_folder': str(output_path),
            'chapter_range': f'{start_chapter}-{end_chapter}',
            'clips': all_clips[:10]  # Return first 10 for reference
        })
        
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500



# ─── TTS Voices ──────────────────────────────────────────────────
@app.route('/api/voices', methods=['GET'])
def get_voices():
    try:
        return jsonify(get_available_voices())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tts/status', methods=['GET'])
def get_tts_status_route():
    """Get status of all TTS engines."""
    try:
        return jsonify(get_tts_status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/elevenlabs/set-key', methods=['POST'])
def set_elevenlabs_key():
    """Set ElevenLabs API key."""
    try:
        from pipeline.elevenlabs_tts import get_elevenlabs_tts
        data = request.json
        api_key = data.get('api_key', '').strip()
        
        if not api_key:
            return jsonify({'error': 'API key is required'}), 400
        
        elevenlabs = get_elevenlabs_tts()
        elevenlabs.set_api_key(api_key)
        
        # Test the API key
        success, message = elevenlabs.test_api_key()
        if success:
            return jsonify({'success': True, 'message': message})
        else:
            return jsonify({'success': False, 'error': message}), 400
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/elevenlabs/voices', methods=['GET'])
def get_elevenlabs_voices():
    """Get ElevenLabs voices (requires API key to be set first)."""
    try:
        from pipeline.elevenlabs_tts import get_elevenlabs_tts
        elevenlabs = get_elevenlabs_tts()
        
        if not elevenlabs.available:
            return jsonify({'error': 'ElevenLabs API key not configured'}), 400
        
        voices = elevenlabs.get_voices(force_refresh=True)
        return jsonify(voices)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/elevenlabs/preview/<voice_id>', methods=['GET'])
def get_elevenlabs_preview(voice_id):
    """Get preview URL for an ElevenLabs voice."""
    try:
        from pipeline.elevenlabs_tts import get_elevenlabs_tts
        elevenlabs = get_elevenlabs_tts()
        
        if not elevenlabs.available:
            return jsonify({'error': 'ElevenLabs API key not configured'}), 400
        
        voices = elevenlabs.get_voices()
        voice = next((v for v in voices if v['voice_id'] == voice_id), None)
        
        if voice and voice.get('preview_url'):
            return jsonify({'preview_url': voice['preview_url']})
        else:
            return jsonify({'error': 'Preview not available for this voice'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tts/preview', methods=['POST'])
def generate_voice_preview():
    """Generate a preview audio sample for any TTS voice."""
    try:
        data = request.json
        engine = data.get('engine', 'edge')
        voice = data.get('voice')
        text = data.get('text', 'Hello! This is a preview of how I sound.')
        
        print(f"[Preview] Request: engine={engine}, voice={voice}")
        
        if not voice:
            return jsonify({'error': 'Voice ID is required'}), 400
        
        # For Kokoro, use pre-generated previews to avoid blocking
        if engine == 'kokoro':
            print(f"[Preview] Using pre-generated Kokoro preview...")
            kokoro_preview_dir = os.path.join(config.PROJECTS_DIR, '_previews', 'kokoro')
            kokoro_preview_path = os.path.join(kokoro_preview_dir, f"{voice}.mp3")
            
            if os.path.exists(kokoro_preview_path):
                # Return pre-generated preview
                return jsonify({
                    'success': True,
                    'preview_url': f'/api/preview-audio/kokoro/{voice}.mp3'
                })
            else:
                print(f"[Preview] Pre-generated preview not found: {kokoro_preview_path}")
                return jsonify({'error': 'Preview not available for this Kokoro voice. Please regenerate previews.'}), 404
        
        # For Edge TTS, generate on-demand (it's async and doesn't block)
        preview_dir = os.path.join(config.PROJECTS_DIR, '_previews')
        os.makedirs(preview_dir, exist_ok=True)
        
        # Generate preview filename
        import hashlib
        voice_hash = hashlib.md5(f"{engine}_{voice}".encode()).hexdigest()[:8]
        preview_path = os.path.join(preview_dir, f"preview_{voice_hash}.mp3")
        
        # Check if preview already exists
        if os.path.exists(preview_path):
            print(f"[Preview] Using cached preview: {preview_path}")
            filename = os.path.basename(preview_path)
            return jsonify({
                'success': True,
                'preview_url': f'/api/preview-audio/{filename}'
            })
        
        # Generate preview audio (Edge TTS only at this point)
        print(f"[Preview] Generating new Edge TTS preview...")
        from pipeline.tts_engine import generate_speech
        
        try:
            if engine == 'elevenlabs':
                from pipeline.elevenlabs_tts import get_elevenlabs_tts
                elevenlabs = get_elevenlabs_tts()
                if not elevenlabs.available:
                    return jsonify({'error': 'ElevenLabs API key not configured'}), 400
                result = elevenlabs.generate_speech(text, preview_path, voice=voice)
            else:  # edge
                result = generate_speech(text, preview_path, voice=voice, engine='edge')
                print(f"[Preview] Edge generation complete!")
        except Exception as gen_error:
            print(f"[Preview] Generation error: {gen_error}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': f'Failed to generate preview: {str(gen_error)}'}), 500
        
        print(f"[Preview] Preview generated successfully at {result['audio_path']}")
        filename = os.path.basename(result['audio_path'])
        return jsonify({
            'success': True,
            'preview_url': f'/api/preview-audio/{filename}',
            'duration': result.get('duration', 0)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Ollama Models ───────────────────────────────────────────────
@app.route('/api/ollama/models', methods=['GET'])
def get_ollama_models():
    return jsonify(narrator.check_ollama())

# ─── Asset Management ────────────────────────────────────────────
@app.route('/api/assets/<asset_type>', methods=['GET'])
def list_assets(asset_type):
    dir_map = {'bgm': config.BGM_DIR, 'watermarks': config.WATERMARK_DIR,
               'backgrounds': config.BACKGROUNDS_DIR, 'intros': config.INTROS_DIR, 'outros': config.OUTROS_DIR}
    asset_dir = dir_map.get(asset_type)
    if not asset_dir:
        return jsonify({'error': 'Invalid asset type'}), 400
    files = []
    for f in sorted(os.listdir(asset_dir)):
        fp = os.path.join(asset_dir, f)
        if os.path.isfile(fp):
            files.append({'name': f, 'path': fp, 'size': os.path.getsize(fp)})
    return jsonify(files)

@app.route('/api/assets/<asset_type>/upload', methods=['POST'])
def upload_asset(asset_type):
    dir_map = {'bgm': config.BGM_DIR, 'watermarks': config.WATERMARK_DIR,
               'backgrounds': config.BACKGROUNDS_DIR, 'intros': config.INTROS_DIR, 'outros': config.OUTROS_DIR}
    asset_dir = dir_map.get(asset_type)
    if not asset_dir:
        return jsonify({'error': 'Invalid asset type'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    filepath = os.path.join(asset_dir, f.filename)
    f.save(filepath)
    return jsonify({'status': 'uploaded', 'path': filepath, 'name': f.filename})

# ─── Manhwa Chapter Downloader ───────────────────────────────────
@app.route('/api/scraper/analyze', methods=['POST'])
def scraper_analyze():
    """Analyze a manhwa URL with a real timeout.

    NOTE: must NOT use `with ThreadPoolExecutor()` here — its __exit__ waits
    for the submitted thread to finish, so a hung site fetch would block the
    request forever even after the timeout fired (the 4-minute 'Analyzing'
    hang). A detached daemon thread lets the response return on time.
    """
    url = request.json.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400

    result_holder = {}

    def _work():
        try:
            result_holder['result'] = analyze_url(url)
        except Exception as e:
            result_holder['error'] = str(e)

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout=90)

    if th.is_alive():
        return jsonify({
            'error': 'The site took too long to respond (90s). It may be down or '
                     'blocking us - try again in a moment, or use a MangaDex URL instead.',
            'timeout': True
        }), 504
    if 'error' in result_holder:
        return jsonify({'error': result_holder['error']}), 500
    return jsonify(result_holder['result'])

@app.route('/api/scraper/download', methods=['POST'])
def scraper_download():
    global pipeline_thread
    # ✅ GUARD: don't stack downloads — same thread slot as pipeline steps
    if pipeline_thread and pipeline_thread.is_alive():
        return jsonify({'error': 'A task is already running. Wait for it to finish.', 'status': 'busy'}), 409
    data = request.json
    manga_url = data.get('url', '').strip()
    chapters = data.get('chapters', [])
    if not manga_url or not chapters:
        return jsonify({'error': 'URL and chapters are required'}), 400
    def run():
        socketio.emit('step_started', {'step': 'download_chapters', 'project': 'scraper'})
        def cb(current, total, msg):
            socketio.emit('progress', {'stage': 'download', 'current': current, 'total': total,
                                       'message': msg, 'percent': int((current / max(total, 1)) * 100)})
        try:
            result = download_chapters(manga_url, chapters, progress_callback=cb)
            socketio.emit('step_complete', {'step': 'download_chapters', 'project': 'scraper',
                'stage': 'done', 'error': None, 'result': result})
        except Exception as e:
            socketio.emit('step_complete', {'step': 'download_chapters', 'project': 'scraper',
                'stage': 'error', 'error': str(e)})
    pipeline_thread = threading.Thread(target=run, daemon=True)
    pipeline_thread.start()
    return jsonify({'status': 'started'})

@app.route('/api/scraper/downloaded', methods=['GET'])
def scraper_list_downloaded():
    return jsonify(list_downloaded_manga())

@app.route('/api/scraper/download-root', methods=['GET'])
def scraper_download_root():
    root = get_download_root()
    os.makedirs(root, exist_ok=True)
    return jsonify({'path': root})

@app.route('/api/scraper/chapter-image/<path:filepath>')
def serve_scraped_image(filepath):
    """Serve downloaded chapter images for preview."""
    root = get_download_root()
    full = os.path.join(root, filepath)
    if os.path.exists(full):
        return send_file(full)
    return jsonify({'error': 'Not found'}), 404


@app.route('/api/raw-json')
def serve_raw_json():
    """Serve a manga_info.json from the download root (path-validated)."""
    path = request.args.get('path', '')
    root = os.path.realpath(get_download_root())
    real = os.path.realpath(path) if path else ''
    if not real or not real.startswith(root + os.sep) or not os.path.isfile(real):
        return jsonify({'error': 'Invalid path'}), 400
    if not real.endswith('manga_info.json'):
        return jsonify({'error': 'Only manga_info.json allowed'}), 400
    try:
        with open(real, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── WebSocket Events ────────────────────────────────────────────
@socketio.on('connect')
def handle_connect():
    emit('connected', {'status': 'ok'})

@socketio.on('disconnect')
def handle_disconnect():
    pass

if __name__ == '__main__':
    print("\n" + "="*60)
    print("  🎬 AniFlow - Automated Manhwa Video Creation")
    print("  Open http://localhost:8080 in your browser")
    print("="*60 + "\n")
    socketio.run(app, host='0.0.0.0', port=8080, debug=False, allow_unsafe_werkzeug=True)
