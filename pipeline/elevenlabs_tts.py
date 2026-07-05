"""
ElevenLabs TTS Engine - Cloud-based high-quality text-to-speech.
Requires an ElevenLabs API key.
"""
import os
import requests
import json
import config

ELEVENLABS_AVAILABLE = True  # Available if user provides API key


class ElevenLabsTTS:
    """ElevenLabs TTS engine for high-quality voice synthesis."""

    def __init__(self):
        self.api_key = None
        self.base_url = "https://api.elevenlabs.io/v1"
        self._voices_cache = None

    def set_api_key(self, api_key: str):
        """Set the ElevenLabs API key."""
        self.api_key = api_key

    @property
    def available(self) -> bool:
        """Check if ElevenLabs is available (has API key)."""
        return bool(self.api_key)

    def generate_speech(self, text: str, output_path: str,
                        voice: str = None, model: str = "eleven_monolingual_v1",
                        stability: float = 0.5, similarity_boost: float = 0.75) -> dict:
        """
        Generate speech audio using ElevenLabs API.

        Args:
            text: Text to synthesize
            output_path: Path to save the audio file
            voice: ElevenLabs voice ID
            model: Model ID (eleven_monolingual_v1, eleven_multilingual_v2, etc.)
            stability: Stability setting (0.0-1.0)
            similarity_boost: Clarity setting (0.0-1.0)

        Returns:
            dict with audio_path, duration
        """
        if not self.api_key:
            raise RuntimeError("ElevenLabs API key not set. Please configure your API key in settings.")

        if not voice:
            # Default to first available voice
            voices = self.get_voices()
            if not voices:
                raise RuntimeError("No ElevenLabs voices available")
            voice = voices[0]['voice_id']

        # ElevenLabs API endpoint
        url = f"{self.base_url}/text-to-speech/{voice}"

        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": self.api_key
        }

        data = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost
            }
        }

        try:
            response = requests.post(url, json=data, headers=headers, timeout=60)
            response.raise_for_status()

            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Save audio
            mp3_path = output_path.rsplit('.', 1)[0] + '.mp3'
            with open(mp3_path, 'wb') as f:
                f.write(response.content)

            # Get duration
            duration = self._get_audio_duration(mp3_path)

            return {
                "audio_path": mp3_path,
                "duration": duration,
            }

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                raise RuntimeError("Invalid ElevenLabs API key")
            elif e.response.status_code == 403:
                raise RuntimeError("ElevenLabs API quota exceeded or access denied")
            elif e.response.status_code == 422:
                raise RuntimeError(f"Invalid request to ElevenLabs API: {e.response.text}")
            else:
                raise RuntimeError(f"ElevenLabs API error: {e}")
        except requests.exceptions.Timeout:
            raise RuntimeError("ElevenLabs API request timed out")
        except Exception as e:
            raise RuntimeError(f"ElevenLabs TTS error: {e}")

    def get_voices(self, force_refresh: bool = False) -> list[dict]:
        """
        Get available ElevenLabs voices.
        
        Returns list of voice dicts with: voice_id, name, preview_url, labels, etc.
        """
        if not self.api_key:
            return []

        # Use cache unless force refresh
        if self._voices_cache is not None and not force_refresh:
            return self._voices_cache

        url = f"{self.base_url}/voices"
        headers = {"xi-api-key": self.api_key}

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()

            voices = []
            for v in data.get('voices', []):
                voices.append({
                    "voice_id": v['voice_id'],
                    "name": v['name'],
                    "preview_url": v.get('preview_url'),
                    "category": v.get('category', 'generated'),
                    "labels": v.get('labels', {}),
                    "description": v.get('description', ''),
                    "engine": "elevenlabs",
                })

            self._voices_cache = voices
            return voices

        except Exception as e:
            print(f"[ElevenLabs] Error fetching voices: {e}")
            return []

    def _get_audio_duration(self, audio_path: str) -> float:
        """Get audio duration in seconds."""
        try:
            from moviepy.editor import AudioFileClip
            clip = AudioFileClip(audio_path)
            duration = clip.duration
            clip.close()
            return duration
        except Exception:
            # Fallback: estimate from file size (rough approximation)
            try:
                size = os.path.getsize(audio_path)
                # MP3 at ~128kbps: 1 second ≈ 16KB
                return max(1.0, size / 16000)
            except Exception:
                return 2.0

    def test_api_key(self) -> tuple[bool, str]:
        """
        Test if the API key is valid.
        
        Returns:
            (success: bool, message: str)
        """
        if not self.api_key:
            return False, "No API key provided"

        try:
            voices = self.get_voices(force_refresh=True)
            if voices:
                return True, f"API key valid. {len(voices)} voices available."
            else:
                return False, "API key valid but no voices found"
        except Exception as e:
            return False, str(e)


# Singleton
_elevenlabs_tts = None


def get_elevenlabs_tts() -> ElevenLabsTTS:
    """Get the global ElevenLabs TTS instance."""
    global _elevenlabs_tts
    if _elevenlabs_tts is None:
        _elevenlabs_tts = ElevenLabsTTS()
    return _elevenlabs_tts
