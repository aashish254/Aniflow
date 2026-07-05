"""
CapCut Director - Export project data in CapCut-compatible format.
Generates JSON keyframe data, subtitle tracks, and animation configs
that can be imported into CapCut for further editing.
"""
import os
import json
from typing import Optional
from datetime import datetime
import config


class CapCutDirector:
    """Generate CapCut-compatible project exports."""

    # Animation presets
    IN_ANIMATIONS = {
        "fade": {"type": "opacity", "start": 0, "end": 1, "duration": 0.3, "easing": "ease-out"},
        "slide_up": {"type": "translateY", "start": 100, "end": 0, "duration": 0.4, "easing": "ease-out"},
        "slide_left": {"type": "translateX", "start": 100, "end": 0, "duration": 0.4, "easing": "ease-out"},
        "slide_right": {"type": "translateX", "start": -100, "end": 0, "duration": 0.4, "easing": "ease-out"},
        "zoom": {"type": "scale", "start": 0.5, "end": 1, "duration": 0.4, "easing": "ease-out"},
        "bounce": {"type": "scale", "start": 0, "end": 1, "duration": 0.5, "easing": "bounce"},
        "rotate_in": {"type": "rotate", "start": -15, "end": 0, "duration": 0.4, "easing": "ease-out"},
        "blur_in": {"type": "blur", "start": 20, "end": 0, "duration": 0.3, "easing": "ease-out"},
    }

    OUT_ANIMATIONS = {
        "fade": {"type": "opacity", "start": 1, "end": 0, "duration": 0.3, "easing": "ease-in"},
        "slide_down": {"type": "translateY", "start": 0, "end": 100, "duration": 0.4, "easing": "ease-in"},
        "slide_left": {"type": "translateX", "start": 0, "end": -100, "duration": 0.4, "easing": "ease-in"},
        "zoom_out": {"type": "scale", "start": 1, "end": 0.5, "duration": 0.4, "easing": "ease-in"},
        "blur_out": {"type": "blur", "start": 0, "end": 20, "duration": 0.3, "easing": "ease-in"},
    }

    EASING_TYPES = ["linear", "ease-in", "ease-out", "ease-in-out", "bounce", "elastic"]

    def __init__(self):
        self.subtitle_settings = {
            "font": config.CAPCUT_SUBTITLE_FONT,
            "size": config.CAPCUT_SUBTITLE_SIZE,
            "color": config.CAPCUT_SUBTITLE_COLOR,
            "background": config.CAPCUT_SUBTITLE_BG,
            "position": "bottom",
            "margin_bottom": 120,
            "word_by_word": True,
            "highlight_color": "#FFD700",
        }

    def generate_project(self, panels: list[dict], project_dir: str,
                         settings: dict = None) -> dict:
        """
        Generate a CapCut-compatible project structure.

        Args:
            panels: Panel data with narration, audio paths, etc.
            project_dir: Project working directory
            settings: Optional override settings

        Returns:
            Project data dict (also saved to project_dir/capcut_project.json)
        """
        if settings:
            self.subtitle_settings.update(settings.get("subtitles", {}))

        in_anim = settings.get("in_animation", config.CAPCUT_IN_ANIMATION) if settings else config.CAPCUT_IN_ANIMATION
        out_anim = settings.get("out_animation", config.CAPCUT_OUT_ANIMATION) if settings else config.CAPCUT_OUT_ANIMATION

        timeline = []
        current_time = 0.0

        # Add intro if exists
        intro_track = self._get_intro_track(settings)
        if intro_track:
            intro_track["start_time"] = 0
            timeline.append(intro_track)
            current_time += intro_track["duration"]

        for i, panel in enumerate(panels):
            duration = panel.get("audio_duration", 3.0) + config.PANEL_PADDING

            track = {
                "type": "panel",
                "index": i,
                "panel_id": panel.get("panel_id", i + 1),
                "start_time": current_time,
                "duration": duration,
                "image_path": panel.get("path", ""),
                "audio_path": panel.get("audio_path", ""),
                "narration": panel.get("narration", ""),
                "subtitle_path": panel.get("subtitle_path", ""),

                # Animations
                "in_animation": self.IN_ANIMATIONS.get(in_anim, self.IN_ANIMATIONS["fade"]),
                "out_animation": self.OUT_ANIMATIONS.get(out_anim, self.OUT_ANIMATIONS["fade"]),

                # Keyframes
                "keyframes": self._generate_keyframes(i, duration, settings),

                # Subtitle config
                "subtitle": {
                    **self.subtitle_settings,
                    "text": panel.get("narration", ""),
                    "start_time": current_time,
                    "end_time": current_time + duration - config.PANEL_PADDING,
                },
            }

            timeline.append(track)
            current_time += duration

        # Add outro if exists
        outro_track = self._get_outro_track(settings)
        if outro_track:
            outro_track["start_time"] = current_time
            timeline.append(outro_track)
            current_time += outro_track["duration"]

        project = {
            "version": "1.0",
            "format": "capcut_director",
            "created_at": datetime.now().isoformat(),
            "total_duration": current_time,
            "resolution": {
                "width": config.VIDEO_WIDTH,
                "height": config.VIDEO_HEIGHT,
            },
            "fps": config.VIDEO_FPS,
            "timeline": timeline,
            "subtitle_settings": self.subtitle_settings,
            "in_animation_type": in_anim,
            "out_animation_type": out_anim,
        }

        # Save to project directory
        output_path = os.path.join(project_dir, "capcut_project.json")
        with open(output_path, 'w') as f:
            json.dump(project, f, indent=2)

        return project

    def _generate_keyframes(self, panel_index: int, duration: float,
                           settings: dict = None) -> list[dict]:
        """Generate keyframe animation data for a panel."""
        effect = panel_index % 4
        keyframes = []

        if effect == 0:
            # Slow zoom in
            keyframes = [
                {"time": 0, "scale": 1.0, "x": 0, "y": 0, "rotation": 0, "opacity": 1},
                {"time": duration * 0.5, "scale": 1.08, "x": 0, "y": -5, "rotation": 0, "opacity": 1},
                {"time": duration, "scale": 1.15, "x": 0, "y": -10, "rotation": 0, "opacity": 1},
            ]
        elif effect == 1:
            # Slow zoom out
            keyframes = [
                {"time": 0, "scale": 1.15, "x": 0, "y": 0, "rotation": 0, "opacity": 1},
                {"time": duration, "scale": 1.0, "x": 0, "y": 0, "rotation": 0, "opacity": 1},
            ]
        elif effect == 2:
            # Pan left to right
            keyframes = [
                {"time": 0, "scale": 1.1, "x": -5, "y": 0, "rotation": 0, "opacity": 1},
                {"time": duration, "scale": 1.1, "x": 5, "y": 0, "rotation": 0, "opacity": 1},
            ]
        else:
            # Pan right to left with slight zoom
            keyframes = [
                {"time": 0, "scale": 1.05, "x": 5, "y": 0, "rotation": 0, "opacity": 1},
                {"time": duration * 0.5, "scale": 1.1, "x": 0, "y": 0, "rotation": 0, "opacity": 1},
                {"time": duration, "scale": 1.05, "x": -5, "y": 0, "rotation": 0, "opacity": 1},
            ]

        # Allow custom keyframes from settings
        if settings and "custom_keyframes" in settings:
            custom = settings["custom_keyframes"].get(str(panel_index))
            if custom:
                keyframes = custom

        return keyframes

    def _get_intro_track(self, settings: dict = None) -> Optional[dict]:
        """Get intro clip track if configured."""
        intro_path = None
        if settings and settings.get("intro_clip"):
            intro_path = settings["intro_clip"]
        elif os.listdir(config.INTROS_DIR):
            files = sorted(os.listdir(config.INTROS_DIR))
            if files:
                intro_path = os.path.join(config.INTROS_DIR, files[0])

        if intro_path and os.path.exists(intro_path):
            return {
                "type": "intro",
                "path": intro_path,
                "duration": config.INTRO_DURATION,
            }
        return None

    def _get_outro_track(self, settings: dict = None) -> Optional[dict]:
        """Get outro clip track if configured."""
        outro_path = None
        if settings and settings.get("outro_clip"):
            outro_path = settings["outro_clip"]
        elif os.listdir(config.OUTROS_DIR):
            files = sorted(os.listdir(config.OUTROS_DIR))
            if files:
                outro_path = os.path.join(config.OUTROS_DIR, files[0])

        if outro_path and os.path.exists(outro_path):
            return {
                "type": "outro",
                "path": outro_path,
                "duration": config.OUTRO_DURATION,
            }
        return None

    def update_subtitle_settings(self, settings: dict):
        """Update subtitle display settings."""
        self.subtitle_settings.update(settings)

    def get_subtitle_srt(self, panels: list[dict], project_dir: str) -> str:
        """Generate a combined SRT subtitle file for the full video."""
        srt_lines = []
        current_time = config.INTRO_DURATION
        idx = 1

        for panel in panels:
            narration = panel.get("narration", "")
            if not narration or narration.startswith("["):
                current_time += panel.get("audio_duration", 3.0) + config.PANEL_PADDING
                continue

            duration = panel.get("audio_duration", 3.0)
            start = _format_srt_time(current_time)
            end = _format_srt_time(current_time + duration)

            srt_lines.append(f"{idx}")
            srt_lines.append(f"{start} --> {end}")
            srt_lines.append(narration)
            srt_lines.append("")

            idx += 1
            current_time += duration + config.PANEL_PADDING

        srt_content = "\n".join(srt_lines)

        srt_path = os.path.join(project_dir, "subtitles.srt")
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(srt_content)

        return srt_path

    @classmethod
    def get_animation_presets(cls) -> dict:
        """Get all available animation presets."""
        return {
            "in_animations": list(cls.IN_ANIMATIONS.keys()),
            "out_animations": list(cls.OUT_ANIMATIONS.keys()),
            "easing_types": cls.EASING_TYPES,
        }


def _format_srt_time(seconds: float) -> str:
    """Format seconds into SRT time format (HH:MM:SS,mmm)."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
