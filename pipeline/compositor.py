"""
Video Compositor - Enhanced with dynamic panel timing, scene-flow mode,
Ken Burns animations, vertical-scroll for tall panels, BGM, watermark.

Panel Timing (dynamic based on panel_type):
  action   -> 1.0-2.0s (rapid cuts, fight sequences)
  standard -> 2.5-3.5s (dialogue, exposition)
  epic     -> 4.0-6.0s (reveals, climax, slow zoom)

Scene Mode (YouTube-style):
  Panels are grouped into scenes. One audio plays across the whole scene
  while panels cut beneath it at type-based timing.
"""
import os, math, subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from moviepy import (ImageClip, AudioFileClip, CompositeVideoClip,
    concatenate_videoclips, concatenate_audioclips, ColorClip, TextClip,
    CompositeAudioClip, VideoClip, VideoFileClip)
import config

RESOLUTION_MAP = {
    "1080p": (1920, 1080),   # 16:9 landscape (YouTube standard)
    "1080p_landscape": (1920, 1080),  # 16:9 landscape (explicit)
    "2k":    (2560, 1440),
    "2k_landscape": (2560, 1440),
    "4k":    (3840, 2160),
    "4k_landscape": (3840, 2160),
    "9:16":  (1080, 1920),   # portrait / TikTok / Shorts
    "portrait": (1080, 1920),
}
FPS_OPTIONS = [30, 60]

# ⚡ SPEED MODE: cached probe result for the macOS hardware encoder
_VT_PROBE = None
# ⚡ SPEED MODE: vignette arrays depend only on (width, height) — cache them
_VIGNETTE_CACHE = {}

VIDEO_EXTS = ('.mp4', '.mov', '.webm', '.mkv', '.avi', '.m4v')


def _is_video_file(path):
    return bool(path) and path.lower().endswith(VIDEO_EXTS)


def _hex_to_rgb(hex_color, fallback=(10, 10, 15)):
    try:
        c = (hex_color or '#000000').lstrip('#')
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _apply_bg_mode(img, video_w, video_h, bg_mode, custom_bg_path):
    """✅ SYNC: single source of truth for background modes — used by BOTH the
    moviepy and ffmpeg-native renderers so the UI setting always matches output.
    Modes: blur (default), color, blur_color, custom (image; video handled
    separately by the ffmpeg renderer)."""
    if bg_mode == "color":
        rgb = _hex_to_rgb(getattr(config, 'BACKGROUND_COLOR_HEX', '#0a0a0f'))
        return np.full((video_h, video_w, 3), rgb, dtype=np.uint8)

    if bg_mode == "blur_color":
        bg_img = img.resize((video_w, video_h), Image.LANCZOS)
        bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=config.BACKGROUND_BLUR_RADIUS))
        bg_array = np.array(bg_img).astype(np.float32)
        rgb = _hex_to_rgb(getattr(config, 'BACKGROUND_COLOR_HEX', '#0a0a0f'))
        opacity = float(getattr(config, 'COLOR_OVERLAY_OPACITY', 0.5))
        overlay = np.full((video_h, video_w, 3), rgb, dtype=np.float32)
        return (bg_array * (1 - opacity) + overlay * opacity).astype(np.uint8)

    if bg_mode == "custom" and custom_bg_path and os.path.exists(custom_bg_path) \
            and not _is_video_file(custom_bg_path):
        try:
            return np.array(Image.open(custom_bg_path).convert('RGB')
                            .resize((video_w, video_h), Image.LANCZOS))
        except Exception as e:
            print(f"[Compositor] Could not load custom background ({e}) — using blur")

    # default / fallback: blurred panel
    bg_img = img.resize((video_w, video_h), Image.LANCZOS)
    bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=config.BACKGROUND_BLUR_RADIUS))
    return np.array(bg_img)

# Default durations (seconds) by panel type when no audio
TYPE_DEFAULT_DURATION = {
    'action':   1.5,
    'standard': 2.0,  # Changed from 3.0 to 2.0 for panels without text
    'epic':     5.0,
}
# Minimum durations — never go below these regardless of audio
TYPE_MIN_DURATION = {
    'action':   1.0,
    'standard': 2.0,  # Changed from 2.5 to 2.0
    'epic':     4.0,
}

def _scale_durations_to_audio(panel_durations: list, audio_dur: float) -> list:
    """
    Proportionally scale panel durations so they sum to >= audio_dur.
    Each panel grows by the same ratio, preserving relative pacing.
    Always adds a small 0.2s tail after audio ends.
    """
    total = sum(panel_durations)
    target = audio_dur + 0.2   # slight tail after audio
    if total >= target:
        return panel_durations  # no scaling needed
    scale = target / max(total, 0.001)
    return [d * scale for d in panel_durations]


_FFMPEG_BIN = None


def _get_ffmpeg_exe():
    """Resolve the bundled ffmpeg binary (same one moviepy/imageio uses)."""
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        try:
            import imageio_ffmpeg
            _FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            _FFMPEG_BIN = 'ffmpeg'
    return _FFMPEG_BIN


def _prepare_landscape_base(image_path, video_w, video_h, animation_style, panel_index,
                            tmp_dir, panel_tag, bg_mode='blur', custom_bg_path=None):
    """⚡ SPEED MODE (ffmpeg-native): pre-composite the panel onto its background
    (honoring bg_mode) and enlarge it — the exact same base the moviepy path
    builds — then save it as a PNG for the zoompan filter.
    A custom VIDEO background returns a bg-video render plan instead
    (panel composited statically over the looping video — no Ken Burns).
    Returns None when this panel needs the moviepy path (portrait video)."""
    img = Image.open(image_path).convert('RGB')
    img_w, img_h = img.size
    is_landscape_video = video_w > video_h
    if not is_landscape_video:
        return None  # portrait mode keeps the moviepy code path

    # ── identical compositing to _create_panel_clip (landscape branch) ──
    panel_scale = video_h / img_h
    fitted_w = int(img_w * panel_scale)
    fitted_h = video_h
    if fitted_w > video_w:
        panel_scale = video_w / img_w
        fitted_w = video_w
        fitted_h = int(img_h * panel_scale)
    img_fitted = img.resize((fitted_w, fitted_h), Image.LANCZOS)
    ox = (video_w - fitted_w) // 2
    oy = (video_h - fitted_h) // 2

    # ── ✅ SYNC: custom VIDEO background — panel renders statically over the
    #    looping video (the moving bg provides the motion; Ken Burns is off)
    if bg_mode == 'custom' and _is_video_file(custom_bg_path) and os.path.exists(custom_bg_path):
        os.makedirs(tmp_dir, exist_ok=True)
        canvas = Image.new('RGBA', (video_w, video_h), (0, 0, 0, 0))
        panel_rgba = img.convert('RGBA').resize((fitted_w, fitted_h), Image.LANCZOS)
        canvas.paste(panel_rgba, (ox, oy))
        panel_png = os.path.join(tmp_dir, f"_panelfg_{panel_tag}.png")
        canvas.save(panel_png)
        return {'bg_video': custom_bg_path, 'panel_rgba': panel_png,
                'ox': ox, 'oy': oy, 'static': False}

    bg_array = _apply_bg_mode(img, video_w, video_h, bg_mode, custom_bg_path)
    panel_arr = np.array(img_fitted)
    bg_array[oy:oy + fitted_h, ox:ox + fitted_w] = panel_arr

    headroom_scale = 1.05
    zoom_min, zoom_max = config.KEN_BURNS_ZOOM_RANGE
    base_frame = Image.fromarray(bg_array)

    if animation_style == 'none':
        out_path = os.path.join(tmp_dir, f"_base_{panel_tag}.png")
        base_frame.save(out_path)
        return {'base': out_path, 'static': True, 'big_w': video_w, 'big_h': video_h}

    if animation_style == 'random':
        import random
        animation_style = random.choice(
            ["zoom_in", "zoom_out", "slide_left", "slide_right", "slide_up", "slide_down"])

    if animation_style == 'auto':
        effect = panel_index % 6
    else:
        effect = {'zoom_in': 0, 'zoom_out': 1, 'slide_left': 2,
                  'slide_right': 3, 'slide_up': 4, 'slide_down': 5}.get(animation_style, 0)

    base_big = base_frame.resize(
        (int(video_w * zoom_max * headroom_scale),
         int(video_h * zoom_max * headroom_scale)), Image.LANCZOS)
    big_w, big_h = base_big.size

    os.makedirs(tmp_dir, exist_ok=True)
    out_path = os.path.join(tmp_dir, f"_base_{panel_tag}.png")
    base_big.save(out_path)
    return {'base': out_path, 'static': False, 'effect': effect,
            'zoom_min': zoom_min, 'zoom_max': zoom_max,
            'big_w': big_w, 'big_h': big_h}


def _zoompan_filter(base_info, duration, fps, video_w, video_h):
    """Build the zoompan filter string replicating make_frame_16_9's math:
    crop window = video/zoom positioned by eased progress over the big canvas."""
    frames = max(2, int(math.ceil(duration * fps)))
    big_w, big_h = base_info['big_w'], base_info['big_h']
    F = big_w / video_w  # base canvas is F× the video width
    ZMIN = base_info['zoom_min'] * F
    ZMAX = base_info['zoom_max'] * F

    p = f"(on/{frames})"
    e = f"({p}*{p}*(3-2*{p}))"  # smoothstep — matches _ease_in_out

    effect = base_info.get('effect', 0)
    if effect == 0:    z, cx, cy = f"{ZMIN}+({ZMAX}-{ZMIN})*{e}", "0.5", "0.5"
    elif effect == 1:  z, cx, cy = f"{ZMAX}-({ZMAX}-{ZMIN})*{e}", "0.5", "0.5"
    elif effect == 2:  z, cx, cy = f"{ZMIN}", f"(1-{e})", "0.5"
    elif effect == 3:  z, cx, cy = f"{ZMIN}", f"({e})", "0.5"
    elif effect == 4:  z, cx, cy = f"{ZMIN}", "0.5", f"(1-{e})"
    else:              z, cx, cy = f"{ZMIN}", "0.5", f"({e})"

    x = f"clip({cx}*(iw-iw/({z})),0,iw-iw/({z}))"
    y = f"clip({cy}*(ih-ih/({z})),0,ih-ih/({z}))"
    return (f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={video_w}x{video_h}:fps={fps}",
            frames)


def _render_clip_ffmpeg(base_info, audio_path, clip_path, duration, fps,
                        video_w, video_h, watermark_path,
                        use_videotoolbox, bitrate):
    """⚡ SPEED MODE (ffmpeg-native): render the clip with a single ffmpeg call —
    zoompan does the Ken Burns motion, overlays add vignette/watermark — so no
    frame is ever piped through Python. Returns True on success."""
    try:
        ff = _get_ffmpeg_exe()
        tmp_dir = os.path.dirname(clip_path)
        panel_tag = os.path.splitext(os.path.basename(clip_path))[0]

        inputs = []
        n_in = 0
        if base_info.get('bg_video'):
            # ✅ SYNC: custom VIDEO background — looping bg under a static panel
            inputs += ['-stream_loop', '-1', '-i', base_info['bg_video']]
            n_in += 1
            chain = (f"[0:v]scale={video_w}:{video_h}:force_original_aspect_ratio=increase,"
                     f"crop={video_w}:{video_h},fps={fps},format=yuv420p[bg]")
            inputs += ['-i', base_info['panel_rgba']]
            n_in += 1
            chain += f";[bg][1:v]overlay={base_info['ox']}:{base_info['oy']}[bv]"
            nframes = None
        elif base_info.get('static'):
            inputs = ['-loop', '1', '-framerate', str(fps), '-i', base_info['base']]
            n_in = 1
            chain = f"[0:v]fps={fps},format=yuv420p[bv]"
            nframes = None
        else:
            inputs = ['-i', base_info['base']]
            n_in = 1
            zf, nframes = _zoompan_filter(base_info, duration, fps, video_w, video_h)
            chain = f"[0:v]{zf},format=yuv420p[bv]"

        idx = n_in  # index of the next input
        # Vignette overlay (static, cached per size)
        vkey = (video_w, video_h)
        if vkey not in _VIGNETTE_CACHE:
            _create_vignette(video_w, video_h, 1.0)  # populates _VIGNETTE_CACHE
        vig_png = os.path.join(tmp_dir, f"_vignette_{video_w}x{video_h}.png")
        if not os.path.exists(vig_png):
            Image.fromarray(_VIGNETTE_CACHE[vkey]).save(vig_png)
        inputs += ['-i', vig_png]
        vig_idx = idx
        idx += 1
        # moviepy applies the vignette as a uniform 20%-opacity overlay
        chain += f";[{vig_idx}:v]format=rgba,colorchannelmixer=aa=0.2[vig];[bv][vig]overlay=0:0[vw]"
        prev = 'vw'

        # Watermark overlay (right/top, 50% opacity — matches moviepy)
        if watermark_path and os.path.exists(watermark_path):
            inputs += ['-i', watermark_path]
            wm_idx = idx
            idx += 1
            chain += (f";[{wm_idx}:v]scale=-1:{video_h // 15},format=rgba,"
                      f"colorchannelmixer=aa=0.5[wm]"
                      f";[{prev}][wm]overlay=main_w-overlay_w:0[vf]")
            prev = 'vf'
        chain += f";[{prev}]format=yuv420p[vout]"

        cmd = [ff, '-y', '-loglevel', 'error', *inputs,
               '-filter_complex', chain, '-map', '[vout]']
        if audio_path and os.path.exists(audio_path):
            inputs += ['-i', audio_path]
            audio_idx = idx
            cmd += ['-map', f'{audio_idx}:a', '-c:a', 'aac', '-b:a', '160k']
        else:
            cmd += ['-an']
        if nframes:
            cmd += ['-frames:v', str(nframes)]
        else:
            cmd += ['-t', f"{duration:.3f}"]
        if use_videotoolbox:
            cmd += ['-c:v', 'h264_videotoolbox', '-b:v', bitrate]
        else:
            cmd += ['-c:v', 'libx264', '-b:v', bitrate, '-preset', 'veryfast']
        cmd += ['-pix_fmt', 'yuv420p', clip_path]

        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0 or not os.path.exists(clip_path) or os.path.getsize(clip_path) < 10000:
            err = (r.stderr or b'').decode(errors='ignore')[-300:]
            print(f"[Compositor] ffmpeg-native render failed: {err}")
            return False
        return True
    except Exception as e:
        print(f"[Compositor] ffmpeg-native render error: {e}")
        return False


def _probe_videotoolbox(fps=30):
    """⚡ SPEED MODE: one-time check that the macOS hardware H.264 encoder works
    with the bundled ffmpeg. Result is cached for the process lifetime."""
    global _VT_PROBE
    if _VT_PROBE is not None:
        return _VT_PROBE
    import subprocess, tempfile
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        tf = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
        tf.close()
        out = tf.name
        r = subprocess.run(
            [ff, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
             '-i', f'testsrc=duration=0.3:size=320x240:rate={min(fps, 30)}',
             '-c:v', 'h264_videotoolbox', '-pix_fmt', 'yuv420p', '-y', out],
            capture_output=True, timeout=20)
        _VT_PROBE = (r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0)
        try:
            os.remove(out)
        except Exception:
            pass
    except Exception:
        _VT_PROBE = False
    if not _VT_PROBE:
        print("[Compositor] VideoToolbox not available — Speed Mode uses libx264 veryfast")
    return _VT_PROBE


def generate_individual_clips(panels, state, progress_callback=None,
                              resolution="1080p", fps=30, quality="medium",
                              bg_mode="blur", custom_bg_path=None, 
                              animation_style="auto", watermark_path=None):
    """
    Generate individual video clips for each panel without concatenating.
    Saves to: manhwa_clips/{manhwa_name}/Chapter_{num:05d}/scene_00001.mp4

    Returns list of dicts with clip paths and metadata.
    """
    w, h = RESOLUTION_MAP.get(resolution, (1920, 1080))  # Default to 1080p landscape
    fps = fps if fps in FPS_OPTIONS else 30
    
    clips_dir = state.clips_dir
    os.makedirs(clips_dir, exist_ok=True)
    
    total_panels = len(panels)
    clip_metadata = []
    
    bitrate_map = {'low': '2000k', 'medium': '5000k', 'high': '8000k'}
    if resolution in ["2k", "2k_landscape"]:
        bitrate_map = {'low': '4000k', 'medium': '8000k', 'high': '12000k'}
    elif resolution in ["4k", "4k_landscape"]:
        bitrate_map = {'low': '8000k', 'medium': '15000k', 'high': '25000k'}
    bitrate = bitrate_map.get(quality, '5000k')

    # ⚡ SPEED MODE: hardware encoder + parallel workers + clip caching.
    # With fast_mode off, everything below behaves exactly as before.
    # All tunables come from state.settings so users can match their hardware:
    #   clip_workers (int), encoder ('auto' | 'videotoolbox' | 'libx264')
    fast_mode = bool(state.settings.get('fast_mode', False))

    def _clamp_int(val, lo, hi, default):
        try:
            v = int(val)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    clip_workers = _clamp_int(state.settings.get('clip_workers'),
                              1, 16, max(2, min(4, (os.cpu_count() or 4) - 2)))
    encoder_choice = (state.settings.get('encoder') or 'auto') if fast_mode else 'libx264'
    use_videotoolbox = False
    if fast_mode:
        if encoder_choice in ('auto', 'videotoolbox'):
            use_videotoolbox = _probe_videotoolbox(fps)
            if encoder_choice == 'videotoolbox' and not use_videotoolbox:
                print("[Compositor] Speed Mode: hardware encoder requested but "
                      "VideoToolbox is unavailable — falling back to libx264")
        if encoder_choice == 'libx264':
            use_videotoolbox = False

    def _write_clip(composed, clip_path, temp_audio):
        """Encode via VideoToolbox in Speed Mode (libx264 veryfast fallback);
        classic mode always uses libx264 medium exactly as before."""
        if use_videotoolbox:
            try:
                composed.write_videofile(
                    clip_path, fps=fps, codec='h264_videotoolbox', audio_codec='aac',
                    bitrate=bitrate, preset='medium', threads=4, logger=None,
                    temp_audiofile=temp_audio,
                    ffmpeg_params=['-pix_fmt', 'yuv420p'],
                )
                return
            except Exception as e:
                print(f"[Compositor] VideoToolbox failed for {os.path.basename(clip_path)} "
                      f"({e}) — retrying with libx264 veryfast")
        composed.write_videofile(
            clip_path, fps=fps, codec='libx264', audio_codec='aac',
            bitrate=bitrate, preset='veryfast' if fast_mode else 'medium', threads=4, logger=None,
            temp_audiofile=temp_audio,
            ffmpeg_params=['-pix_fmt', 'yuv420p'],
        )

    def _build_one_clip(panel, i):
        """Build + encode a single panel clip. Returns metadata dict or None."""
        panel_id = panel.get('panel_id', i + 1)
        clip_filename = f"scene_{panel_id:05d}.mp4"
        clip_path = os.path.join(clips_dir, clip_filename)
        audio_path = panel.get('audio_path', '')

        # ⚡ SPEED MODE: reuse an existing clip unless a source file is newer
        # (panel image re-cropped / audio regenerated → mtime bumps → re-encode)
        if fast_mode and os.path.exists(clip_path) and os.path.getsize(clip_path) > 10000:
            try:
                clip_mtime = os.path.getmtime(clip_path)
                src_newer = False
                img_path = panel.get('path', '')
                if img_path and os.path.exists(img_path) and os.path.getmtime(img_path) > clip_mtime:
                    src_newer = True
                if audio_path and os.path.exists(audio_path) and os.path.getmtime(audio_path) > clip_mtime:
                    src_newer = True
                if not src_newer:
                    audio_duration = None
                    if audio_path and os.path.exists(audio_path):
                        try:
                            ac = AudioFileClip(audio_path)
                            audio_duration = ac.duration
                            ac.close()
                        except Exception:
                            pass
                    return {
                        'panel_id': panel_id,
                        'clip_path': clip_path,
                        'clip_filename': clip_filename,
                        'duration': _get_dynamic_duration(panel, audio_duration),
                        'text': panel.get('text', panel.get('narration', '')),
                        'audio_path': audio_path,
                        'resolution': f"{w}x{h}",
                        'fps': fps,
                        'cached': True,
                    }
            except Exception:
                pass  # fall through to full regeneration

        # Get audio duration
        audio_duration = None
        if audio_path and os.path.exists(audio_path):
            try:
                audio_clip = AudioFileClip(audio_path)
                audio_duration = audio_clip.duration
                audio_clip.close()
            except Exception:
                pass
        
        # Calculate clip duration
        clip_duration = _get_dynamic_duration(panel, audio_duration)

        # ⚡ SPEED MODE v2: ffmpeg-native rendering — the Ken Burns motion runs
        # inside ffmpeg (zoompan), so no frame is piped through Python. Any
        # failure falls back to the moviepy path below.
        panel_path = panel.get('path', '')
        if fast_mode and panel_path and os.path.exists(panel_path):
            try:
                prepared = _prepare_landscape_base(
                    panel_path, w, h, animation_style, i, clips_dir, f"{panel_id:05d}",
                    bg_mode=bg_mode, custom_bg_path=custom_bg_path)
                if prepared and _render_clip_ffmpeg(
                        prepared, audio_path, clip_path, clip_duration, fps,
                        w, h, watermark_path, use_videotoolbox, bitrate):
                    try:
                        if prepared.get('base'):
                            os.remove(prepared['base'])  # temp base PNG no longer needed
                        if prepared.get('panel_rgba'):
                            os.remove(prepared['panel_rgba'])
                    except OSError:
                        pass
                    return {
                        'panel_id': panel_id,
                        'clip_path': clip_path,
                        'clip_filename': clip_filename,
                        'duration': clip_duration,
                        'text': panel.get('text', panel.get('narration', '')),
                        'audio_path': audio_path,
                        'resolution': f"{w}x{h}",
                        'fps': fps,
                    }
            except Exception as e:
                print(f"[Compositor] ffmpeg-native path error for panel {panel_id}: {e} — using moviepy")
        
        # Create panel clip
        panel_clip = _make_panel_clip(panel, w, h, clip_duration, fps, i,
                                      bg_mode, custom_bg_path, animation_style)
        
        # Add vignette
        vignette = _create_vignette(w, h, clip_duration)
        composed = CompositeVideoClip([panel_clip, vignette]).with_duration(clip_duration)
        
        # Add audio
        if audio_path and os.path.exists(audio_path):
            try:
                audio_clip = AudioFileClip(audio_path)
                composed = composed.with_audio(audio_clip)
            except Exception as e:
                print(f"[Compositor] Audio error for panel {panel_id}: {e}")
        
        # Add watermark if provided
        if watermark_path and os.path.exists(watermark_path):
            try:
                watermark_img = ImageClip(watermark_path).resized(height=h // 15)
                watermark_img = watermark_img.with_opacity(0.5).with_duration(clip_duration)
                watermark_img = watermark_img.with_position(('right', 'top'))
                composed = CompositeVideoClip([composed, watermark_img])
            except Exception:
                pass
        
        # Unique temp audio file per clip
        temp_audio = os.path.join(clips_dir, f"TEMP_audio_{panel_id:05d}.mp4")
        
        try:
            # Write individual clip
            _write_clip(composed, clip_path, temp_audio)
            
            # Store metadata
            return {
                'panel_id': panel_id,
                'clip_path': clip_path,
                'clip_filename': clip_filename,
                'duration': clip_duration,
                'text': panel.get('text', panel.get('narration', '')),
                'audio_path': audio_path,
                'resolution': f"{w}x{h}",
                'fps': fps,
            }
            
        except Exception as e:
            print(f"[Compositor] Failed to generate clip for panel {panel_id}: {e}")
            return None
        finally:
            # Clean up
            composed.close()
            if os.path.exists(temp_audio):
                try:
                    os.remove(temp_audio)
                except Exception:
                    pass

    if fast_mode and total_panels > 1 and clip_workers > 1:
        # ⚡ SPEED MODE: clips are independent — encode several at once
        from concurrent.futures import ThreadPoolExecutor, as_completed
        max_workers = clip_workers
        print(f"[Compositor] Speed Mode: {total_panels} clips, {max_workers} parallel workers"
              + (" (VideoToolbox hardware encoder)" if use_videotoolbox else " (libx264 veryfast)"))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_build_one_clip, panel, i): i for i, panel in enumerate(panels)}
            done = 0
            for fut in as_completed(futures):
                done += 1
                if progress_callback:
                    progress_callback(done, total_panels, f"Generating clip {done}/{total_panels}...")
                try:
                    meta = fut.result()
                except Exception as e:
                    print(f"[Compositor] Worker error: {e}")
                    meta = None
                if meta:
                    clip_metadata.append(meta)
        clip_metadata.sort(key=lambda m: m.get('panel_id', 0))
    else:
        for i, panel in enumerate(panels):
            if progress_callback:
                progress_callback(i + 1, total_panels, f"Generating clip {panel.get('panel_id', i + 1)}/{total_panels}...")
            meta = _build_one_clip(panel, i)
            if meta:
                clip_metadata.append(meta)
    
    # Save clips metadata
    import json
    metadata_path = os.path.join(clips_dir, 'clips_metadata.json')
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump({
            'total_clips': len(clip_metadata),
            'resolution': resolution,
            'fps': fps,
            'quality': quality,
            'animation_style': animation_style,
            'clips': clip_metadata,
        }, f, indent=2, ensure_ascii=False)
    print(f"[Compositor] Saved clips metadata to {metadata_path}")
    
    return clip_metadata


def compose_video(panels, project_dir, output_path=None, progress_callback=None,
                  resolution="1080p", fps=30, quality="medium",
                  bgm_path=None, watermark_path=None, bg_mode="blur",
                  custom_bg_path=None, intro_path=None, outro_path=None,
                  export_clips=False, animation_style="auto",
                  scene_mode=False):
    """
    Compose the recap video.

    scene_mode=True: uses scene_narration audio + panel_type timing.
    scene_mode=False (default): legacy per-panel audio mode.
    """
    w, h = RESOLUTION_MAP.get(resolution, (1920, 1080))  # Default to 1080p landscape
    fps = fps if fps in FPS_OPTIONS else 30
    if output_path is None:
        out_dir = os.path.join(config.VIDEO_OUTPUT_DIR, os.path.basename(project_dir))
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, os.path.basename(project_dir) + "_recap.mp4")
    else:
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

    total_panels = len(panels)
    clips = []
    individual_clips = []

    # Intro
    if intro_path and os.path.exists(intro_path):
        try:
            intro = VideoFileClip(intro_path).resized((w, h))
            clips.append(intro)
        except Exception:
            clips.append(ColorClip(size=(w, h), color=(10,10,15), duration=config.INTRO_DURATION))
    elif config.INTRO_DURATION > 0:
        clips.append(ColorClip(size=(w, h), color=(10,10,15), duration=config.INTRO_DURATION))

    clips_dir = os.path.join(project_dir, "clips") if export_clips else None
    if clips_dir:
        os.makedirs(clips_dir, exist_ok=True)

    if scene_mode:
        clips += _compose_scene_clips(panels, w, h, fps, bg_mode, custom_bg_path,
                                      animation_style, watermark_path,
                                      progress_callback, total_panels)
    else:
        clips += _compose_perPanel_clips(panels, w, h, fps, bg_mode, custom_bg_path,
                                         animation_style, watermark_path,
                                         progress_callback, total_panels,
                                         individual_clips)

    # Outro
    if outro_path and os.path.exists(outro_path):
        try:
            outro = VideoFileClip(outro_path).resized((w, h))
            clips.append(outro)
        except Exception:
            clips.append(ColorClip(size=(w, h), color=(10,10,15), duration=config.OUTRO_DURATION))
    elif config.OUTRO_DURATION > 0:
        clips.append(ColorClip(size=(w, h), color=(10,10,15), duration=config.OUTRO_DURATION))

    if progress_callback:
        progress_callback(total_panels, total_panels, "Rendering final video...")

    final = concatenate_videoclips(clips, method="compose")

    # BGM
    if bgm_path and os.path.exists(bgm_path):
        try:
            bgm = AudioFileClip(bgm_path)
            if bgm.duration < final.duration:
                loops = int(final.duration / bgm.duration) + 1
                bgm = concatenate_audioclips([bgm] * loops)
            bgm = bgm.subclipped(0, final.duration)
            bgm = bgm.with_volume_scaled(config.BGM_VOLUME)
            if final.audio:
                final = final.with_audio(CompositeAudioClip([final.audio, bgm]))
            else:
                final = final.with_audio(bgm)
        except Exception as e:
            print(f"[Compositor] BGM error: {e}")

    bitrate_map = {'low': '2000k', 'medium': '5000k', 'high': '8000k'}
    if resolution == "2k":
        bitrate_map = {'low': '4000k', 'medium': '8000k', 'high': '12000k'}
    elif resolution == "4k":
        bitrate_map = {'low': '8000k', 'medium': '15000k', 'high': '25000k'}
    bitrate = bitrate_map.get(quality, '5000k')

    # Unique temp audio file per project to avoid clash if two renders run
    temp_audio = os.path.join(out_dir, f"{os.path.basename(output_path)}_TEMP_audio.mp4")

    final.write_videofile(
        output_path, fps=fps, codec='libx264', audio_codec='aac',
        bitrate=bitrate, preset='medium', threads=4, logger=None,
        temp_audiofile=temp_audio,
        ffmpeg_params=['-pix_fmt', 'yuv420p'],   # CRITICAL: avoid yuva420p slowness
    )

    # Export individual clips (legacy per-panel mode only)
    if export_clips and individual_clips:
        for clip, pid in individual_clips:
            try:
                cpath = os.path.join(clips_dir, f"clip_{pid:04d}.mp4")
                clip.write_videofile(
                    cpath, fps=fps, codec='libx264', audio_codec='aac',
                    bitrate='3000k', preset='fast', threads=2, logger=None,
                    ffmpeg_params=['-pix_fmt', 'yuv420p'],
                )
            except Exception:
                pass

    final.close()
    for clip in clips:
        try: clip.close()
        except Exception: pass

    return output_path


def export_scene_clips(panels, project_dir, output_dir=None,
                       resolution="1080p", fps=30, quality="medium",
                       bg_mode="blur", animation_style="auto",
                       progress_callback=None):
    """
    Export one MP4 clip per scene (scene_001.mp4, scene_002.mp4 ...).
    Much faster than full render — each clip is only 10-30s.
    Great for importing individual clips into CapCut.

    Returns list of dicts: [{scene_id, path, duration, panel_count}, ...]
    """
    w, h = RESOLUTION_MAP.get(resolution, (1920, 1080))  # Default to 1080p landscape

    # Output directory
    if output_dir is None:
        output_dir = os.path.join(
            config.VIDEO_OUTPUT_DIR,
            os.path.basename(project_dir),
            'scene_clips'
        )
    os.makedirs(output_dir, exist_ok=True)

    bitrate_map = {'low': '2000k', 'medium': '4000k', 'high': '7000k'}
    bitrate = bitrate_map.get(quality, '4000k')

    # Group panels by scene_id (preserving order)
    scenes_ordered = []
    scene_map = {}
    for panel in panels:
        sid = panel.get('scene_id', f"scene_{panel.get('panel_id', 0):03d}")
        if sid not in scene_map:
            scene_map[sid] = []
            scenes_ordered.append(sid)
        scene_map[sid].append(panel)

    total = len(scenes_ordered)
    results = []

    for scene_idx, sid in enumerate(scenes_ordered):
        scene_panels = scene_map[sid]
        out_path = os.path.join(output_dir, f"{sid}.mp4")

        # Skip already-rendered clips
        if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
            if progress_callback:
                progress_callback(scene_idx + 1, total,
                    f"Skip (cached): {sid} ({scene_idx+1}/{total})")
            dur = sum(
                float(p.get('target_duration') or
                      TYPE_DEFAULT_DURATION.get(p.get('panel_type','standard'), 3.0))
                for p in scene_panels
            )
            results.append({'scene_id': sid, 'path': out_path,
                           'duration': dur, 'panel_count': len(scene_panels)})
            continue

        if progress_callback:
            progress_callback(scene_idx + 1, total,
                f"Rendering {sid} ({scene_idx+1}/{total}, {len(scene_panels)} panels)...")

        try:
            # Build panel clips for this scene
            panel_durations = []
            for panel in scene_panels:
                ptype = panel.get('panel_type', 'standard')
                dur = float(panel.get('target_duration') or
                            TYPE_DEFAULT_DURATION.get(ptype, 3.0))
                dur = max(TYPE_MIN_DURATION.get(ptype, 1.0), dur)
                panel_durations.append(dur)

            total_panel_dur = sum(panel_durations)

            # Load scene audio
            lead = next((p for p in scene_panels if p.get('is_scene_lead')), scene_panels[0])
            audio_path = lead.get('scene_audio_path')
            audio_clip = None
            audio_dur = 0.0
            if audio_path and os.path.exists(audio_path):
                try:
                    audio_clip = AudioFileClip(audio_path)
                    audio_dur = audio_clip.duration
                except Exception as ae:
                    print(f"[SceneClip] Audio error {sid}: {ae}")

            # Scale ALL panels proportionally if audio is longer
            if audio_dur > total_panel_dur and panel_durations:
                panel_durations = _scale_durations_to_audio(panel_durations, audio_dur)
                total_panel_dur = sum(panel_durations)

            # Build panel video clips
            panel_clips = []
            for j, (panel, dur) in enumerate(zip(scene_panels, panel_durations)):
                pc = _make_panel_clip(panel, w, h, dur, fps, j,
                                      bg_mode, None, animation_style)
                vignette = _create_vignette(w, h, dur)
                composed = CompositeVideoClip([pc, vignette]).with_duration(dur)
                panel_clips.append(composed)

            scene_video = concatenate_videoclips(panel_clips, method='compose')

            # Attach audio
            if audio_clip is not None:
                if audio_clip.duration > scene_video.duration:
                    audio_clip = audio_clip.subclipped(0, scene_video.duration)
                scene_video = scene_video.with_audio(audio_clip)

            # Write clip
            temp_audio = out_path + '_TEMP_audio.mp4'
            scene_video.write_videofile(
                out_path, fps=fps, codec='libx264', audio_codec='aac',
                bitrate=bitrate, preset='fast', threads=4, logger=None,
                temp_audiofile=temp_audio,
                ffmpeg_params=['-pix_fmt', 'yuv420p'],
            )

            clip_dur = scene_video.duration
            scene_video.close()
            for pc in panel_clips:
                try: pc.close()
                except Exception: pass
            if audio_clip:
                try: audio_clip.close()
                except Exception: pass

            # Clean temp file
            if os.path.exists(temp_audio):
                os.remove(temp_audio)

            results.append({'scene_id': sid, 'path': out_path,
                           'duration': clip_dur, 'panel_count': len(scene_panels)})
            print(f"[SceneClip] ✅ {sid} → {clip_dur:.1f}s — {out_path}")

        except Exception as e:
            print(f"[SceneClip] ❌ {sid}: {e}")
            results.append({'scene_id': sid, 'path': None,
                           'duration': 0, 'panel_count': len(scene_panels),
                           'error': str(e)})

    print(f"[SceneClip] Done — {len(results)} clips in {output_dir}")
    return results


# ──────────────────────────────────────────────────────────────────────────────
def _get_dynamic_duration(panel: dict, audio_duration: float) -> float:
    """
    Compute display duration for a panel using panel_type + audio_duration.
    Audio is never cut short. Epic panels always get >= 4s.
    """
    ptype   = panel.get('panel_type', 'standard')
    min_d   = TYPE_MIN_DURATION.get(ptype, 2.5)
    default = TYPE_DEFAULT_DURATION.get(ptype, 3.0)
    audio   = max(0.0, audio_duration or 0.0)

    # If a target_duration was pre-computed by classifier, respect it
    if panel.get('target_duration', 0) > 0:
        return max(min_d, panel['target_duration'])

    if audio <= 0:
        return default

    if ptype == 'action':
        return max(min_d, audio + 0.15)
    elif ptype == 'epic':
        return max(min_d, audio + 0.6)
    else:
        return max(min_d, audio + 0.3)


def _compose_perPanel_clips(panels, w, h, fps, bg_mode, custom_bg_path,
                            animation_style, watermark_path,
                            progress_callback, total_panels, individual_clips):
    """Legacy per-panel mode: each panel has its own audio clip."""
    clips = []
    for i, panel in enumerate(panels):
        if progress_callback:
            progress_callback(i + 1, total_panels, f"Composing panel {i+1}/{total_panels}")

        audio_path     = panel.get('audio_path')
        audio_duration = float(panel.get('audio_duration') or 0)
        audio_clip     = None

        if audio_path and os.path.exists(audio_path):
            try:
                audio_clip     = AudioFileClip(audio_path)
                audio_duration = audio_clip.duration
            except Exception as e:
                print(f"[Compositor] Audio load error panel {panel.get('panel_id')}: {e}")

        clip_duration = _get_dynamic_duration(panel, audio_duration)
        panel_clip    = _make_panel_clip(panel, w, h, clip_duration, fps, i,
                                         bg_mode, custom_bg_path, animation_style)
        vignette      = _create_vignette(w, h, clip_duration)
        composed      = CompositeVideoClip([panel_clip, vignette])

        if watermark_path and os.path.exists(watermark_path):
            try:
                wm      = _create_watermark(watermark_path, w, h, clip_duration)
                composed = CompositeVideoClip([composed, wm])
            except Exception:
                pass

        if audio_clip is not None:
            composed = composed.with_audio(audio_clip)
        composed = composed.with_duration(clip_duration)
        clips.append(composed)
        individual_clips.append((composed, panel.get('panel_id', i + 1)))

    return clips


def _compose_scene_clips(panels, w, h, fps, bg_mode, custom_bg_path,
                         animation_style, watermark_path,
                         progress_callback, total_panels):
    """
    Scene-flow mode: panels in the same scene share one continuous audio track.
    The scene audio (from is_scene_lead panel) plays over all panels in the group.
    Each panel is shown for target_duration or type-default.
    """
    # Group panels by scene_id
    from itertools import groupby
    scenes_ordered = []
    scene_map = {}
    for panel in panels:
        sid = panel.get('scene_id', f'scene_{panel["panel_id"]:03d}')
        if sid not in scene_map:
            scene_map[sid] = []
            scenes_ordered.append(sid)
        scene_map[sid].append(panel)

    clips = []
    panel_idx = 0

    for sid in scenes_ordered:
        scene_panels = scene_map[sid]

        # Compute duration per panel in the scene
        panel_durations = []
        for panel in scene_panels:
            ptype    = panel.get('panel_type', 'standard')
            base_dur = TYPE_DEFAULT_DURATION.get(ptype, 3.0)
            dur      = panel.get('target_duration') or base_dur
            panel_durations.append(max(TYPE_MIN_DURATION.get(ptype, 1.0), float(dur)))

        total_scene_dur = sum(panel_durations)

        # Load scene audio (from lead panel)
        lead = next((p for p in scene_panels if p.get('is_scene_lead')), scene_panels[0])
        scene_audio_path = lead.get('scene_audio_path')
        scene_audio_dur  = float(lead.get('scene_audio_duration') or 0)
        scene_audio_clip = None

        if scene_audio_path and os.path.exists(scene_audio_path):
            try:
                scene_audio_clip = AudioFileClip(scene_audio_path)
                scene_audio_dur  = scene_audio_clip.duration
            except Exception as e:
                print(f"[Compositor] Scene audio error {sid}: {e}")

        # If audio is longer than total panel time → scale ALL panels proportionally
        if scene_audio_dur > total_scene_dur and panel_durations:
            panel_durations = _scale_durations_to_audio(panel_durations, scene_audio_dur)
            total_scene_dur = sum(panel_durations)

        # Build individual panel clips for this scene
        scene_clips = []
        for j, (panel, dur) in enumerate(zip(scene_panels, panel_durations)):
            if progress_callback:
                progress_callback(
                    panel_idx + 1, total_panels,
                    f"Composing panel {panel.get('panel_id')} ({sid})"
                )
            panel_clip = _make_panel_clip(panel, w, h, dur, fps, panel_idx,
                                          bg_mode, custom_bg_path, animation_style)
            vignette   = _create_vignette(w, h, dur)
            composed   = CompositeVideoClip([panel_clip, vignette])

            if watermark_path and os.path.exists(watermark_path):
                try:
                    wm      = _create_watermark(watermark_path, w, h, dur)
                    composed = CompositeVideoClip([composed, wm])
                except Exception:
                    pass

            scene_clips.append(composed.with_duration(dur))
            panel_idx += 1

        # Concatenate scene panels into one clip
        scene_video = concatenate_videoclips(scene_clips, method='compose')

        # Attach scene audio to the concatenated scene
        if scene_audio_clip is not None:
            # Trim or pad audio to match scene video duration
            if scene_audio_clip.duration > scene_video.duration:
                scene_audio_clip = scene_audio_clip.subclipped(0, scene_video.duration)
            scene_video = scene_video.with_audio(scene_audio_clip)

        clips.append(scene_video)

    return clips


def _make_panel_clip(panel: dict, video_w: int, video_h: int, duration: float,
                     fps: int, panel_index: int, bg_mode: str = "blur",
                     custom_bg_path: str = None, animation_style: str = "auto"):
    """Safe wrapper: routes to _create_panel_clip or returns a ColorClip fallback."""
    panel_path = panel.get('path', '')
    ptype = panel.get('panel_type', 'standard')
    if not panel_path or not os.path.exists(panel_path):
        print(f"[Compositor] ⚠ Panel {panel.get('panel_id','?')} image missing: {panel_path!r}")
        return ColorClip(size=(video_w, video_h), color=(10, 10, 15), duration=duration)
    try:
        return _create_panel_clip(panel_path, video_w, video_h, duration, fps,
                                  panel_index, bg_mode, custom_bg_path,
                                  animation_style, panel_type=ptype)
    except Exception as e:
        print(f"[Compositor] Error panel {panel.get('panel_id')}: {e}")
        return ColorClip(size=(video_w, video_h), color=(10, 10, 15), duration=duration)


def _create_panel_clip(image_path, video_w, video_h, duration, fps, panel_index,
                       bg_mode="blur", custom_bg_path=None, animation_style="auto",
                       panel_type="standard"):
    """Core panel clip creator with Ken Burns or vertical-scroll animation."""

    img = Image.open(image_path).convert('RGB')
    img_w, img_h = img.size
    is_vertical = (img_h / max(img_w, 1)) > 2.5   # tall/narrow manhwa strip

    # ── 16:9 layout: panel centered with blurred sides ──────────────
    # Scale panel so it fits within the video height, then center horizontally.
    # The blurred background fills the full 16:9 frame.
    is_landscape_video = video_w > video_h  # True for 1920×1080, False for 1080×1920

    if is_landscape_video:
        # Fit panel to full height, width fills proportionally
        panel_scale = video_h / img_h
        fitted_w = int(img_w * panel_scale)
        fitted_h = video_h
        # If panel is wider than video, fit to width instead
        if fitted_w > video_w:
            panel_scale = video_w / img_w
            fitted_w = video_w
            fitted_h = int(img_h * panel_scale)
        img_fitted = img.resize((fitted_w, fitted_h), Image.LANCZOS)
        # ✅ SYNC: honor the user's Background Mode (was hardcoded to blur)
        bg_array = _apply_bg_mode(img, video_w, video_h, bg_mode, custom_bg_path)
        # Composite: panel centered on background
        panel_arr = np.array(img_fitted)
        ox = (video_w - fitted_w) // 2
        oy = (video_h - fitted_h) // 2
        bg_array[oy:oy+fitted_h, ox:ox+fitted_w] = panel_arr

        # Ken Burns on the composited frame
        headroom_scale = 1.05
        zoom_min, zoom_max = config.KEN_BURNS_ZOOM_RANGE
        base_frame = Image.fromarray(bg_array)
        base_big = base_frame.resize(
            (int(video_w * zoom_max * headroom_scale),
             int(video_h * zoom_max * headroom_scale)), Image.LANCZOS)
        base_arr = np.array(base_big)
        big_w, big_h = base_big.size

        import random
        
        # Handle "none" - static image
        if animation_style == "none":
            return ImageClip(bg_array, duration=duration)
        
        # Handle "random" - pick a random animation
        if animation_style == "random":
            available_styles = ["zoom_in", "zoom_out", "slide_left", "slide_right", "slide_up", "slide_down"]
            animation_style = random.choice(available_styles)
        
        # Map animation_style to effect type
        if animation_style == "auto":
            effect = panel_index % 6
        elif animation_style == "zoom_in":
            effect = 0
        elif animation_style == "zoom_out":
            effect = 1
        elif animation_style == "slide_left":
            effect = 2
        elif animation_style == "slide_right":
            effect = 3
        elif animation_style == "slide_up":
            effect = 4
        elif animation_style == "slide_down":
            effect = 5
        else:
            effect = 0

        def make_frame_16_9(t):
            progress = t / max(duration, 0.01)
            eased = _ease_in_out(progress)
            
            if effect == 0:  # zoom_in
                current_zoom = zoom_min + (zoom_max - zoom_min) * eased
                cx_progress, cy_progress = 0.5, 0.5
            elif effect == 1:  # zoom_out
                current_zoom = zoom_max - (zoom_max - zoom_min) * eased
                cx_progress, cy_progress = 0.5, 0.5
            elif effect == 2:  # slide_left (pan from right to left)
                current_zoom = zoom_min
                cx_progress = 1.0 - eased
                cy_progress = 0.5
            elif effect == 3:  # slide_right (pan from left to right)
                current_zoom = zoom_min
                cx_progress = eased
                cy_progress = 0.5
            elif effect == 4:  # slide_up (pan from bottom to top)
                current_zoom = zoom_min
                cx_progress = 0.5
                cy_progress = 1.0 - eased
            elif effect == 5:  # slide_down (pan from top to bottom)
                current_zoom = zoom_min
                cx_progress = 0.5
                cy_progress = eased
            else:
                current_zoom = zoom_min
                cx_progress, cy_progress = 0.5, 0.5
            
            cw = int(video_w / current_zoom)
            ch = int(video_h / current_zoom)
            max_x = max(big_w - cw, 0)
            max_y = max(big_h - ch, 0)
            
            cx = int(max_x * cx_progress)
            cy = int(max_y * cy_progress)
            
            cx = max(0, min(cx, max_x))
            cy = max(0, min(cy, max_y))
            crop = base_arr[cy:cy+ch, cx:cx+cw]
            return np.array(Image.fromarray(crop).resize((video_w, video_h), Image.LANCZOS))

        return VideoClip(make_frame_16_9, duration=duration)
    # ── end 16:9 layout ─────────────────────────────────────────────

    scale_w = video_w / img_w
    scale_h = video_h / img_h
    base_scale = max(scale_w, scale_h)
    zoom_min, zoom_max = config.KEN_BURNS_ZOOM_RANGE
    headroom_scale = base_scale * zoom_max * 1.05
    new_w = int(img_w * headroom_scale)
    new_h = int(img_h * headroom_scale)
    img_resized = img.resize((new_w, new_h), Image.LANCZOS)

    # Background
    if bg_mode == "blur":
        bg_img = img.resize((video_w, video_h), Image.LANCZOS)
        bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=config.BACKGROUND_BLUR_RADIUS))
        bg_array = np.array(bg_img)
    elif bg_mode == "color":
        # Solid color background (hex color from settings)
        color = getattr(config, 'BACKGROUND_COLOR_HEX', '#000000')
        # Convert hex to RGB tuple
        color = color.lstrip('#')
        rgb = tuple(int(color[i:i+2], 16) for i in (0, 2, 4))
        bg_array = np.full((video_h, video_w, 3), rgb, dtype=np.uint8)
    elif bg_mode == "blur_color":
        # Blurred background with color overlay
        bg_img = img.resize((video_w, video_h), Image.LANCZOS)
        bg_img = bg_img.filter(ImageFilter.GaussianBlur(radius=config.BACKGROUND_BLUR_RADIUS))
        bg_array = np.array(bg_img)
        # Apply color overlay
        color = getattr(config, 'BACKGROUND_COLOR_HEX', '#000000')
        color = color.lstrip('#')
        rgb = tuple(int(color[i:i+2], 16) for i in (0, 2, 4))
        opacity = getattr(config, 'COLOR_OVERLAY_OPACITY', 0.5)
        color_overlay = np.full((video_h, video_w, 3), rgb, dtype=np.uint8)
        bg_array = (bg_array * (1 - opacity) + color_overlay * opacity).astype(np.uint8)
    elif bg_mode == "custom" and custom_bg_path and os.path.exists(custom_bg_path):
        bg_img = Image.open(custom_bg_path).convert('RGB').resize((video_w, video_h), Image.LANCZOS)
        bg_array = np.array(bg_img)
    else:
        bg_array = np.full((video_h, video_w, 3), config.BACKGROUND_COLOR, dtype=np.uint8)

    img_array = np.array(img_resized)

    # Handle "none" animation_style early - before vertical scroll check
    if animation_style == "none":
        # Static image - just fit and center on background
        scale_to_fit = min(video_w / img_w, video_h / img_h)
        fitted_w = int(img_w * scale_to_fit)
        fitted_h = int(img_h * scale_to_fit)
        img_fitted = img.resize((fitted_w, fitted_h), Image.LANCZOS)
        # Composite on background
        result = bg_array.copy()
        ox = (video_w - fitted_w) // 2
        oy = (video_h - fitted_h) // 2
        fitted_arr = np.array(img_fitted)
        result[oy:oy+fitted_h, ox:ox+fitted_w] = fitted_arr
        return ImageClip(result, duration=duration)

    # ── VERTICAL SCROLL: for tall strips, scroll from top to bottom ──
    if is_vertical:
        # img_resized is already scaled to fit width; scroll vertically
        scroll_h = new_h  # total height of the scaled image
        if scroll_h <= video_h:
            # Image fits in frame — use Ken Burns instead
            is_vertical = False
        else:
            max_scroll = scroll_h - video_h

            def make_scroll_frame(t):
                progress = t / max(duration, 0.01)
                eased_progress = _ease_in_out(min(progress, 1.0))
                # Add slight pause at start and end
                if progress < 0.1:
                    eased_progress = 0.0
                elif progress > 0.9:
                    eased_progress = 1.0
                else:
                    eased_progress = _ease_in_out((progress - 0.1) / 0.8)

                scroll_y = int(eased_progress * max_scroll)
                scroll_y = max(0, min(scroll_y, max_scroll))
                # Crop the scroll window
                strip = img_array[scroll_y:scroll_y + video_h, :video_w]
                if strip.shape[0] < video_h:
                    # Pad bottom if needed
                    pad = np.zeros((video_h - strip.shape[0], video_w, 3), dtype=np.uint8)
                    strip = np.vstack([strip, pad])
                # Composite over background
                result = bg_array.copy()
                pw = min(strip.shape[1], video_w)
                ox = (video_w - pw) // 2
                result[:, ox:ox + pw] = strip[:, :pw]
                return result

            return VideoClip(make_scroll_frame, duration=duration)

    # Choose animation
    import random
    
    # Handle "none" - static image
    if animation_style == "none":
        # Static image - just fit and center
        img_fitted = img.resize((video_w, video_h), Image.LANCZOS)
        return ImageClip(np.array(img_fitted), duration=duration)
    
    # Handle "random" - pick a random animation
    if animation_style == "random":
        available_styles = ["zoom_in", "zoom_out", "slide_left", "slide_right", "slide_up", "slide_down"]
        animation_style = random.choice(available_styles)
    
    # Map animation_style to effect type
    if animation_style == "auto":
        effect = panel_index % 6  # Cycle through effects
    elif animation_style == "zoom_in":
        effect = 0
    elif animation_style == "zoom_out":
        effect = 1
    elif animation_style == "slide_left":
        effect = 2
    elif animation_style == "slide_right":
        effect = 3
    elif animation_style == "slide_up":
        effect = 4
    elif animation_style == "slide_down":
        effect = 5
    else:
        effect = 0  # default to zoom_in

    def make_frame(t):
        progress = t / max(duration, 0.01)
        eased = _ease_in_out(progress)
        
        if effect == 0:  # zoom_in
            current_zoom = zoom_min + (zoom_max - zoom_min) * eased
            cx_progress, cy_progress = 0.5, 0.5
        elif effect == 1:  # zoom_out
            current_zoom = zoom_max - (zoom_max - zoom_min) * eased
            cx_progress, cy_progress = 0.5, 0.5
        elif effect == 2:  # slide_left (pan from right to left)
            current_zoom = zoom_min
            cx_progress = 1.0 - eased  # Start right (1.0), move to left (0.0)
            cy_progress = 0.5
        elif effect == 3:  # slide_right (pan from left to right)
            current_zoom = zoom_min
            cx_progress = eased  # Start left (0.0), move to right (1.0)
            cy_progress = 0.5
        elif effect == 4:  # slide_up (pan from bottom to top)
            current_zoom = zoom_min
            cx_progress = 0.5
            cy_progress = 1.0 - eased  # Start bottom (1.0), move to top (0.0)
        elif effect == 5:  # slide_down (pan from top to bottom)
            current_zoom = zoom_min
            cx_progress = 0.5
            cy_progress = eased  # Start top (0.0), move to bottom (1.0)
        else:
            current_zoom = zoom_min
            cx_progress, cy_progress = 0.5, 0.5

        crop_w = min(int(video_w / (base_scale * current_zoom) * headroom_scale), new_w)
        crop_h = min(int(video_h / (base_scale * current_zoom) * headroom_scale), new_h)
        max_x = max(new_w - crop_w, 0)
        max_y = max(new_h - crop_h, 0)
        
        cx = int(max_x * cx_progress)
        cy = int(max_y * cy_progress)
        
        cx = max(0, min(cx, max_x))
        cy = max(0, min(cy, max_y))
        frame = img_array[cy:cy+crop_h, cx:cx+crop_w]
        frame_img = Image.fromarray(frame).resize((video_w, video_h), Image.LANCZOS)
        return np.array(frame_img)

    return VideoClip(make_frame, duration=duration)


def _create_watermark(wm_path, video_w, video_h, duration):
    wm = Image.open(wm_path).convert('RGBA')
    wm_w = int(video_w * config.WATERMARK_SCALE)
    wm_h = int(wm_w * wm.height / wm.width)
    wm = wm.resize((wm_w, wm_h), Image.LANCZOS)
    wm_array = np.array(wm)
    clip = ImageClip(wm_array[:,:,:3], duration=duration)
    clip = clip.with_opacity(config.WATERMARK_OPACITY)
    pos_map = {
        "top_left": (20, 20), "top_right": (video_w - wm_w - 20, 20),
        "bottom_left": (20, video_h - wm_h - 20),
        "bottom_right": (video_w - wm_w - 20, video_h - wm_h - 20),
        "center": ((video_w - wm_w) // 2, (video_h - wm_h) // 2),
    }
    pos = pos_map.get(config.WATERMARK_POSITION, pos_map["bottom_right"])
    return clip.with_position(pos)


def _create_vignette(width, height, duration):
    """Create a subtle vignette effect - very light darkening at edges.
    ⚡ SPEED MODE: the pixel array depends only on (width, height) — compute
    it once and reuse across clips (safe: ImageClip treats it as read-only)."""
    key = (width, height)
    if key not in _VIGNETTE_CACHE:
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        cx, cy = width // 2, height // 2
        for i in range(min(width, height) // 2, 0, -2):
            ratio = i / (min(width, height) // 2)
            # Reduced from 80 to 30 for much lighter vignette
            alpha = int(30 * (1 - ratio)**2)
            draw.ellipse([cx - int(i * width / height), cy - i, cx + int(i * width / height), cy + i],
                         fill=(0, 0, 0, alpha))
        arr = np.array(img)
        rgb = arr[:, :, :3]
        a = arr[:, :, 3:4] / 255.0
        _VIGNETTE_CACHE[key] = (rgb * a).astype(np.uint8)
    clip = ImageClip(_VIGNETTE_CACHE[key], duration=duration)
    # Reduced from 0.5 to 0.2 for even lighter effect
    return clip.with_opacity(0.2)


def _ease_in_out(t):
    return t * t * (3 - 2 * t)


def get_video_info(video_path):
    try:
        clip = VideoFileClip(video_path)
        info = {'duration': clip.duration, 'width': clip.w, 'height': clip.h,
                'fps': clip.fps, 'size_mb': os.path.getsize(video_path) / (1024 * 1024)}
        clip.close()
        return info
    except Exception as e:
        return {'error': str(e)}
