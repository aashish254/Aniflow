"""
Kokoro TTS Engine - Advanced local text-to-speech using Kokoro models.
Falls back gracefully if kokoro is not installed.
Runs 100% locally - no API calls needed.
"""
import os
import json
import numpy as np
import config

# Patch numpy.load to allow pickle for Kokoro
_original_np_load = None

def _patch_numpy_load():
    """Temporarily patch numpy.load to allow pickle (needed for Kokoro voices)"""
    global _original_np_load
    if _original_np_load is None:
        _original_np_load = np.load
        def patched_load(file, *args, **kwargs):
            kwargs['allow_pickle'] = True
            result = _original_np_load(file, *args, **kwargs)
            # If it's a 0-d array with object dtype, extract the actual object
            if isinstance(result, np.ndarray) and result.dtype == object and result.shape == ():
                return result.item()
            return result
        np.load = patched_load

def _unpatch_numpy_load():
    """Restore original numpy.load"""
    global _original_np_load
    if _original_np_load is not None:
        np.load = _original_np_load
        _original_np_load = None

# Try to import kokoro
KOKORO_AVAILABLE = False
KOKORO_MODEL_PATH = None
KOKORO_VOICES_PATH = None

try:
    from kokoro_onnx import Kokoro
    import soundfile as sf
    
    # Check for model and voices files
    model_dir = os.path.expanduser("~/.local/share/kokoro")
    model_file = os.path.join(model_dir, "kokoro-v1_0.onnx")
    voices_file = os.path.join(model_dir, "voices.npy")
    
    if os.path.exists(model_file) and os.path.exists(voices_file):
        KOKORO_MODEL_PATH = model_file
        KOKORO_VOICES_PATH = voices_file
        KOKORO_AVAILABLE = True
    else:
        KOKORO_AVAILABLE = False
except ImportError:
    pass


class KokoroTTS:
    """Local Kokoro TTS engine for high-quality voice synthesis."""

    # Available Kokoro voices with new API
    VOICES = {
        "af": {"name": "American Female", "gender": "Female", "lang": "en-us", "style": "Warm & Clear", "display_name": "American Female"},
        "af_bella": {"name": "Bella (Female)", "gender": "Female", "lang": "en-us", "style": "Warm & Clear", "display_name": "American Female - Bella"},
        "af_nicole": {"name": "Nicole (Female)", "gender": "Female", "lang": "en-us", "style": "Professional", "display_name": "American Female - Nicole"},
        "af_sarah": {"name": "Sarah (Female)", "gender": "Female", "lang": "en-us", "style": "Friendly", "display_name": "American Female - Sarah"},
        "af_sky": {"name": "Sky (Female)", "gender": "Female", "lang": "en-us", "style": "Energetic", "display_name": "American Female - Sky"},
        "am_adam": {"name": "Adam (Male)", "gender": "Male", "lang": "en-us", "style": "Deep & Authoritative", "display_name": "American Male - Adam"},
        "am_michael": {"name": "Michael (Male)", "gender": "Male", "lang": "en-us", "style": "Confident", "display_name": "American Male - Michael"},
        "bf_emma": {"name": "Emma (Female)", "gender": "Female", "lang": "en-gb", "style": "British Female", "display_name": "British Female - Emma"},
        "bf_isabella": {"name": "Isabella (Female)", "gender": "Female", "lang": "en-gb", "style": "Elegant", "display_name": "British Female - Isabella"},
        "bm_george": {"name": "George (Male)", "gender": "Male", "lang": "en-gb", "style": "British Male", "display_name": "British Male - George"},
        "bm_lewis": {"name": "Lewis (Male)", "gender": "Male", "lang": "en-gb", "style": "Distinguished", "display_name": "British Male - Lewis"},
    }

    def __init__(self):
        self._kokoro_instance = None
        self._on_mps = False  # ⚡ SPEED MODE: True when the model runs on Apple GPU

    @property
    def available(self) -> bool:
        return KOKORO_AVAILABLE and KOKORO_MODEL_PATH is not None and KOKORO_VOICES_PATH is not None

    def _get_kokoro(self, voice_code: str = "af"):
        """Get or create a Kokoro instance."""
        if not self.available:
            raise RuntimeError("Kokoro TTS not available. Model or voices files may not be downloaded.")

        if self._kokoro_instance is None:
            # Patch numpy.load to allow pickle (Kokoro voices require it)
            _patch_numpy_load()
            try:
                self._kokoro_instance = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
                
                # Monkey-patch _style_for to return 2D arrays (model expects rank 2)
                original_style_for = self._kokoro_instance._style_for
                def patched_style_for(voice, length):
                    style = original_style_for(voice, length)
                    # Ensure 2D shape (1, style_dim) instead of 1D (style_dim,)
                    if style.ndim == 1:
                        style = style.reshape(1, -1)
                    return style
                self._kokoro_instance._style_for = patched_style_for
                
            finally:
                _unpatch_numpy_load()

            # ⚡ SPEED MODE: move the model to Apple Silicon GPU (MPS) so each
            # panel synthesizes faster. If MPS execution fails on first use,
            # generate_speech() falls back to CPU automatically.
            # Can be disabled via the Speed Mode panel (kokoro_gpu=False).
            if getattr(config, 'SPEED_MODE', False) and \
                    getattr(config, 'SPEED_SETTINGS', {}).get('kokoro_gpu', True):
                self._try_move_to_mps()
                
        return self._kokoro_instance

    def _try_move_to_mps(self):
        """⚡ SPEED MODE: best-effort move of the torch model to MPS."""
        try:
            import torch
            if not getattr(torch.backends, 'mps', None) or not torch.backends.mps.is_available():
                print("[Kokoro] Speed Mode: MPS not available — staying on CPU")
                return
            model = getattr(self._kokoro_instance, 'model', None)
            if model is not None:
                model.to('mps').eval()
                self._on_mps = True
                print("[Kokoro] Speed Mode: model moved to MPS (Apple GPU)")
        except Exception as e:
            self._on_mps = False
            print(f"[Kokoro] Speed Mode: could not move model to MPS ({e}) — staying on CPU")

    def _move_back_to_cpu(self):
        """⚡ SPEED MODE: revert to CPU after an MPS failure, keeping the instance."""
        self._on_mps = False
        try:
            model = getattr(self._kokoro_instance, 'model', None)
            if model is not None:
                model.to('cpu')
            return True
        except Exception as e:
            print(f"[Kokoro] CPU revert failed ({e}) — will recreate instance")
            self._kokoro_instance = None
            return False

    def generate_speech(self, text: str, output_path: str,
                        voice: str = None, speed: float = 1.0) -> dict:
        """
        Generate speech audio using Kokoro TTS.

        Args:
            text: Text to synthesize
            output_path: Path to save the audio file
            voice: Kokoro voice ID (e.g., "af", "am", "bf")
            speed: Speech speed multiplier (0.5-2.0)

        Returns:
            dict with audio_path, duration
        """
        voice = voice or "af"  # Default to American Female

        kokoro = self._get_kokoro(voice)

        # Generate audio (⚡ SPEED MODE: retry on CPU if the MPS run fails)
        try:
            audio, sample_rate = kokoro.create(text, voice=voice, speed=speed)
        except Exception as e:
            if not self._on_mps:
                raise
            print(f"[Kokoro] Speed Mode: MPS generation failed ({e}) — retrying on CPU")
            self._move_back_to_cpu()
            kokoro = self._get_kokoro(voice)
            audio, sample_rate = kokoro.create(text, voice=voice, speed=speed)

        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Save as WAV first
        wav_path = output_path.rsplit('.', 1)[0] + '.wav'
        sf.write(wav_path, audio, sample_rate)

        # Convert to MP3 using ffmpeg for smaller file size
        mp3_path = output_path.rsplit('.', 1)[0] + '.mp3'
        import subprocess
        try:
            result = subprocess.run([
                'ffmpeg', '-y', '-i', wav_path,
                '-codec:a', 'libmp3lame', '-qscale:a', '2',
                mp3_path
            ], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
            
            # Check if conversion succeeded
            if result.returncode == 0 and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
                # Clean up WAV
                os.remove(wav_path)
                final_path = mp3_path
            else:
                # Log error and use WAV as fallback
                print(f"[Kokoro] ffmpeg conversion failed (exit code {result.returncode})")
                if result.stderr:
                    print(f"[Kokoro] ffmpeg stderr: {result.stderr.decode('utf-8', errors='ignore')[:200]}")
                final_path = wav_path
        except subprocess.TimeoutExpired:
            print(f"[Kokoro] ffmpeg conversion timeout - using WAV fallback")
            final_path = wav_path
        except Exception as e:
            print(f"[Kokoro] ffmpeg conversion error: {e} - using WAV fallback")
            final_path = wav_path

        # Get duration
        duration = len(audio) / sample_rate

        return {
            "audio_path": final_path,
            "duration": duration,
        }

    def get_voices(self) -> list[dict]:
        """Get available Kokoro voices."""
        voices = []
        for voice_id, info in self.VOICES.items():
            voices.append({
                "name": voice_id,
                "display_name": info["name"],
                "gender": info["gender"],
                "locale": info["lang"],
                "style": info["style"],
                "engine": "kokoro",
                "available": KOKORO_AVAILABLE,  # Indicate if actually usable
            })
        return voices


# Singleton
_kokoro_tts = None


def get_kokoro_tts() -> KokoroTTS:
    global _kokoro_tts
    if _kokoro_tts is None:
        _kokoro_tts = KokoroTTS()
    return _kokoro_tts
