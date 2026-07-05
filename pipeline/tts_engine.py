"""
Text-to-Speech Engine - Unified TTS supporting Edge-TTS, Kokoro TTS, and ElevenLabs.
"""
import os, asyncio, json
import edge_tts
import config
from pipeline.kokoro_tts import get_kokoro_tts, KOKORO_AVAILABLE
from pipeline.elevenlabs_tts import get_elevenlabs_tts, ELEVENLABS_AVAILABLE

async def _generate_speech_edge(text, output_path, voice=None, rate=None, pitch=None):
    voice = voice or config.TTS_VOICE
    rate = rate or config.TTS_RATE
    pitch = pitch or config.TTS_PITCH
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await comm.save(output_path)
    subtitle_path = output_path.rsplit('.', 1)[0] + '.srt'
    try:
        submaker = edge_tts.SubMaker()
        async for chunk in edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch).stream():
            if chunk["type"] == "WordBoundary":
                submaker.feed(chunk)
        srt_text = submaker.get_srt() if hasattr(submaker, 'get_srt') else submaker.generate_subs()
        with open(subtitle_path, 'w', encoding='utf-8') as f:
            f.write(srt_text)
    except Exception as e:
        print(f"[TTS] Subtitle generation warning: {e}")
        subtitle_path = None
    duration = _get_audio_duration(output_path)
    return {'audio_path': output_path, 'subtitle_path': subtitle_path, 'duration': duration}


async def _generate_speech_edge_single_stream(text, output_path, voice=None, rate=None, pitch=None):
    """⚡ SPEED MODE variant: audio AND subtitles from ONE stream instead of
    two separate network requests. Same output, half the transfers."""
    voice = voice or config.TTS_VOICE
    rate = rate or config.TTS_RATE
    pitch = pitch or config.TTS_PITCH
    subtitle_path = output_path.rsplit('.', 1)[0] + '.srt'
    try:
        submaker = edge_tts.SubMaker()
        with open(output_path, 'wb') as f:
            async for chunk in edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch).stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    submaker.feed(chunk)
        srt_text = submaker.get_srt() if hasattr(submaker, 'get_srt') else submaker.generate_subs()
        with open(subtitle_path, 'w', encoding='utf-8') as f:
            f.write(srt_text)
    except Exception as e:
        print(f"[TTS] Subtitle generation warning: {e}")
        subtitle_path = None
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise  # the audio itself failed — let the caller handle it
    duration = _get_audio_duration(output_path)
    return {'audio_path': output_path, 'subtitle_path': subtitle_path, 'duration': duration}


async def _generate_edge_batch(jobs, concurrency=6):
    """⚡ SPEED MODE: run Edge-TTS jobs concurrently on one shared event loop —
    several requests in flight instead of strictly one-at-a-time.
    Returns {panel_id: result_dict | None}."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(job):
        async with sem:
            try:
                result = await _generate_speech_edge_single_stream(
                    job['text'], job['output_path'], job['voice'], job['rate'], job['pitch'])
                return job['panel_id'], result
            except Exception as e:
                print(f"[TTS] Error for panel {job['panel_id']}: {e}")
                return job['panel_id'], None

    return dict(await asyncio.gather(*[_one(j) for j in jobs]))


def generate_speech(text, output_path, voice=None, rate=None, pitch=None, engine=None, speed=1.0):
    engine = engine or config.TTS_ENGINE
    if engine == "kokoro" and KOKORO_AVAILABLE:
        return get_kokoro_tts().generate_speech(text, output_path, voice=voice, speed=speed)
    elif engine == "elevenlabs" and ELEVENLABS_AVAILABLE:
        elevenlabs = get_elevenlabs_tts()
        if not elevenlabs.available:
            raise RuntimeError("ElevenLabs API key not configured. Please set your API key in settings.")
        return elevenlabs.generate_speech(text, output_path, voice=voice)
    return asyncio.run(_generate_speech_edge(text, output_path, voice, rate, pitch))

def generate_all_audio(panels, state, voice=None, rate=None, pitch=None, engine=None, speed=1.0,
                       progress_callback=None, text_field='narration'):
    """Generate TTS audio for all panels.
    Saves to: manhwa_audio/{manhwa_name}/Chapter_{num:05d}/panel_00001.mp3

    ⚡ SPEED MODE (state.settings['fast_mode']): Edge and ElevenLabs generate
    concurrently; Kokoro runs on the GPU (MPS). With fast_mode off, this is
    the exact sequential behavior as before. Per-panel file caching applies
    in both modes.

    Args:
        panels: List of panel dicts with text/narration
        state: PipelineState instance with audio_dir path
        text_field: which panel dict key to read text from.
                    'narration' (default) for narrated recap pipeline,
                    'text' for direct dialogue-to-audio auto-pipeline.
    """
    audio_dir = state.audio_dir
    os.makedirs(audio_dir, exist_ok=True)
    engine = engine or config.TTS_ENGINE
    total = len(panels)
    fast_mode = bool(state.settings.get('fast_mode', False))

    import re
    audio_metadata = []
    jobs = []  # panels that need actual generation

    for i, panel in enumerate(panels):
        if progress_callback:
            progress_callback(i + 1, total, f"Generating audio {i + 1}/{total}")

        panel_id = panel.get('panel_id', i + 1)
        text_content = panel.get(text_field, '')

        if not text_content or text_content.startswith('[') or text_content == '(no dialogue)':
            panel['audio_path'] = None
            panel['audio_duration'] = getattr(config, 'AUTO_PIPELINE_SILENT_DURATION', config.PANEL_PADDING)
            audio_metadata.append({
                'panel_id': panel_id,
                'audio_path': None,
                'audio_duration': panel['audio_duration'],
                'text_content': text_content,
                'skipped': True,
            })
            continue

        # Strip any "Speaker:" / "Narrator:" prefixes so TTS only reads dialogue
        text_content = re.sub(r'^(?:Speaker|Narrator|Unknown|Character)\s*[:：]\s*', '', text_content, flags=re.IGNORECASE | re.MULTILINE)

        # Use 5-digit panel numbering: panel_00001.mp3
        audio_path = os.path.join(audio_dir, f"panel_{panel_id:05d}.mp3")

        if os.path.exists(audio_path):
            panel['audio_path'] = audio_path
            panel['audio_duration'] = _get_audio_duration(audio_path)
            audio_metadata.append({
                'panel_id': panel_id,
                'audio_path': audio_path,
                'audio_duration': panel['audio_duration'],
                'text_content': text_content,
                'cached': True,
            })
            continue

        jobs.append((i, panel, panel_id, text_content, audio_path))

    def _apply_result(panel, panel_id, text_content, result):
        panel['audio_path'] = result['audio_path']
        panel['audio_duration'] = result['duration']
        panel['subtitle_path'] = result.get('subtitle_path')
        audio_metadata.append({
            'panel_id': panel_id,
            'audio_path': result['audio_path'],
            'audio_duration': result['duration'],
            'subtitle_path': result.get('subtitle_path'),
            'text_content': text_content,
            'engine': engine,
        })

    def _apply_failure(panel, panel_id, text_content, e):
        print(f"[TTS] Error for panel {panel_id}: {e}")
        panel['audio_path'] = None
        panel['audio_duration'] = 2.0
        audio_metadata.append({
            'panel_id': panel_id,
            'audio_path': None,
            'audio_duration': 2.0,
            'text_content': text_content,
            'error': str(e),
        })

    if jobs and fast_mode and engine in ('edge', 'elevenlabs'):
        # ⚡ SPEED MODE: concurrent generation. Worker counts are user-tunable
        # (Speed Mode panel). ElevenLabs defaults stay low — paid API, rate limits.
        def _clamp_workers(val, default, hi):
            try:
                v = int(val)
            except (TypeError, ValueError):
                return default
            return max(1, min(hi, v))

        if engine == 'edge':
            workers = _clamp_workers(state.settings.get('edge_workers'), 6, 12)
        else:
            workers = _clamp_workers(state.settings.get('elevenlabs_workers'), 2, 4)
        print(f"[TTS] Speed Mode: generating {len(jobs)} audio files "
              f"with {workers} concurrent workers ({engine})")

        if engine == 'edge':
            edge_jobs = [
                {'panel_id': pid, 'text': tc, 'output_path': ap,
                 'voice': voice, 'rate': rate, 'pitch': pitch}
                for (i, panel, pid, tc, ap) in jobs
            ]
            results = asyncio.run(_generate_edge_batch(edge_jobs, concurrency=workers))
            for (i, panel, pid, tc, ap) in jobs:
                result = results.get(pid)
                if result:
                    _apply_result(panel, pid, tc, result)
                else:
                    _apply_failure(panel, pid, tc, 'generation failed')
        else:  # elevenlabs
            from concurrent.futures import ThreadPoolExecutor, as_completed
            el = get_elevenlabs_tts()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(el.generate_speech, tc, ap, voice): (panel, pid, tc)
                    for (i, panel, pid, tc, ap) in jobs
                }
                done = 0
                for fut in as_completed(futures):
                    panel, pid, tc = futures[fut]
                    done += 1
                    if progress_callback:
                        progress_callback(done, total, f"Generating audio {done}/{total}")
                    try:
                        _apply_result(panel, pid, tc, fut.result())
                    except Exception as e:
                        _apply_failure(panel, pid, tc, e)
        if progress_callback:
            progress_callback(total, total, f"Generated {total} audio files")
    else:
        # CLASSIC MODE: sequential generation (unchanged behavior)
        for (i, panel, pid, tc, ap) in jobs:
            if progress_callback:
                progress_callback(i + 1, total, f"Generating audio {i + 1}/{total}")
            try:
                result = generate_speech(text=tc, output_path=ap, voice=voice, rate=rate, pitch=pitch, engine=engine, speed=speed)
                _apply_result(panel, pid, tc, result)
            except Exception as e:
                _apply_failure(panel, pid, tc, e)

    _save_audio_metadata(audio_metadata, audio_dir, engine)
    return panels

def _get_audio_duration(audio_path):
    try:
        from moviepy import AudioFileClip
        clip = AudioFileClip(audio_path)
        d = clip.duration
        clip.close()
        return d
    except Exception:
        try:
            return max(1.0, os.path.getsize(audio_path) / 16000)
        except Exception:
            return 2.0

async def list_voices(language_filter="en"):
    try:
        # Add timeout to prevent hanging
        voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=5.0)
        return [{'name': v['ShortName'], 'gender': v['Gender'], 'locale': v['Locale'], 'engine': 'edge'}
                for v in voices if not language_filter or v['Locale'].startswith(language_filter)]
    except (asyncio.TimeoutError, Exception) as e:
        print(f"[TTS] Failed to fetch Edge TTS voices: {e}")
        # Return empty list on failure - Kokoro voices will still be available
        return []

def get_available_voices(language_filter="en"):
    try:
        voices = asyncio.run(list_voices(language_filter))
    except Exception as e:
        print(f"[TTS] Error getting Edge voices: {e}")
        voices = []

    # Always add Kokoro voices (they'll have 'available' flag)
    voices.extend(get_kokoro_tts().get_voices())
    if ELEVENLABS_AVAILABLE:
        elevenlabs = get_elevenlabs_tts()
        if elevenlabs.available:
            # ✅ SYNC: normalize ElevenLabs voices so the UI can filter by engine
            for v in elevenlabs.get_voices():
                voices.append({
                    'name': v.get('voice_id', ''),
                    'display_name': v.get('name', ''),
                    'gender': (v.get('labels') or {}).get('gender', ''),
                    'locale': '',
                    'engine': 'elevenlabs',
                })
    return voices

def get_tts_status():
    elevenlabs = get_elevenlabs_tts()
    return {
        "edge_tts": True,
        "kokoro_tts": KOKORO_AVAILABLE,
        "elevenlabs_tts": ELEVENLABS_AVAILABLE and elevenlabs.available
    }

def _save_audio_metadata(audio_metadata, audio_dir, engine):
    """Save audio generation metadata to manhwa_audio/{name}/Chapter_{num:05d}/audio_metadata.json"""
    metadata_path = os.path.join(audio_dir, "audio_metadata.json")
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump({
            'total_panels': len(audio_metadata),
            'tts_engine': engine,
            'panels': audio_metadata,
        }, f, indent=2, ensure_ascii=False)
    print(f"[TTS] Saved audio metadata to {metadata_path}")
