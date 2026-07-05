"""
Cast System - Save and load character voice profiles for consistent TTS across projects.
Cast files (.cst) are JSON files containing voice assignments for named characters.
"""
import os
import json
from datetime import datetime
import config


class CastProfile:
    """A character voice profile within a cast."""
    def __init__(self, name: str, voice: str = None, engine: str = "edge",
                 rate: str = None, pitch: str = None, speed: float = 1.0):
        self.name = name
        self.voice = voice or config.TTS_VOICE
        self.engine = engine  # "edge" or "kokoro"
        self.rate = rate or config.TTS_RATE
        self.pitch = pitch or config.TTS_PITCH
        self.speed = speed  # For Kokoro TTS

    def to_dict(self):
        return {
            "name": self.name,
            "voice": self.voice,
            "engine": self.engine,
            "rate": self.rate,
            "pitch": self.pitch,
            "speed": self.speed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'CastProfile':
        return cls(
            name=data.get("name", "Unknown"),
            voice=data.get("voice"),
            engine=data.get("engine", "edge"),
            rate=data.get("rate"),
            pitch=data.get("pitch"),
            speed=data.get("speed", 1.0),
        )


class Cast:
    """A collection of character voice profiles."""

    def __init__(self, name: str = "Default Cast"):
        self.name = name
        self.narrator = CastProfile("Narrator")
        self.characters: dict[str, CastProfile] = {}
        self.created_at = datetime.now().isoformat()
        self.updated_at = self.created_at

    def add_character(self, name: str, voice: str = None, engine: str = "edge",
                      rate: str = None, pitch: str = None, speed: float = 1.0):
        """Add or update a character profile."""
        self.characters[name] = CastProfile(name, voice, engine, rate, pitch, speed)
        self.updated_at = datetime.now().isoformat()

    def remove_character(self, name: str):
        """Remove a character profile."""
        if name in self.characters:
            del self.characters[name]
            self.updated_at = datetime.now().isoformat()

    def get_voice_for(self, character_name: str = None) -> CastProfile:
        """Get voice profile for a character, or narrator if not found."""
        if character_name and character_name in self.characters:
            return self.characters[character_name]
        return self.narrator

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "narrator": self.narrator.to_dict(),
            "characters": {k: v.to_dict() for k, v in self.characters.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Cast':
        cast = cls(data.get("name", "Default Cast"))
        if "narrator" in data:
            cast.narrator = CastProfile.from_dict(data["narrator"])
        for name, char_data in data.get("characters", {}).items():
            cast.characters[name] = CastProfile.from_dict(char_data)
        cast.created_at = data.get("created_at", cast.created_at)
        cast.updated_at = data.get("updated_at", cast.updated_at)
        return cast

    def save(self, filepath: str = None):
        """Export cast to a .cst file."""
        if filepath is None:
            filepath = os.path.join(config.CAST_DIR, f"{self.name}.cst")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
        return filepath

    @classmethod
    def load(cls, filepath: str) -> 'Cast':
        """Import cast from a .cst file."""
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls.from_dict(data)


def list_casts() -> list[dict]:
    """List all saved cast files."""
    casts = []
    for f in sorted(os.listdir(config.CAST_DIR)):
        if f.endswith('.cst'):
            filepath = os.path.join(config.CAST_DIR, f)
            try:
                cast = Cast.load(filepath)
                casts.append({
                    "name": cast.name,
                    "file": f,
                    "characters": len(cast.characters),
                    "narrator_voice": cast.narrator.voice,
                    "updated_at": cast.updated_at,
                })
            except Exception:
                pass
    return casts


def export_project_cast(project_name: str) -> str:
    """Export the cast from a project's panel narrations."""
    state_file = os.path.join(config.PROJECTS_DIR, project_name, "state.json")
    if not os.path.exists(state_file):
        raise FileNotFoundError(f"Project {project_name} not found")

    with open(state_file) as f:
        state = json.load(f)

    cast = Cast(name=f"{project_name}_cast")

    # Set narrator from project settings
    settings = state.get("settings", {})
    cast.narrator = CastProfile(
        name="Narrator",
        voice=settings.get("tts_voice", config.TTS_VOICE),
        engine=settings.get("tts_engine", "edge"),
        rate=settings.get("tts_rate", config.TTS_RATE),
    )

    filepath = cast.save()
    return filepath
