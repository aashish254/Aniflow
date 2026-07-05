"""
Script Rewriter - AI-powered narration script rewriting using Ollama.
Supports multiple styles: dramatic, casual, epic, horror, comedy, hype.
"""
import requests
import config


class ScriptRewriter:
    """Rewrites narration scripts using local Ollama models."""

    def __init__(self, model: str = None, host: str = None):
        self.model = model or config.OLLAMA_MODEL
        self.host = (host or config.OLLAMA_HOST).rstrip('/')

    def rewrite(self, narration: str, style: str = "dramatic",
                custom_instructions: str = None) -> str:
        """
        Rewrite a narration script in a different style.

        Args:
            narration: Original narration text
            style: Rewriting style key from REWRITER_STYLES
            custom_instructions: Optional custom instructions

        Returns:
            Rewritten narration text
        """
        if custom_instructions:
            style_prompt = custom_instructions
        else:
            style_prompt = config.REWRITER_STYLES.get(
                style,
                config.REWRITER_STYLES["dramatic"]
            )

        system_prompt = """You are a professional script rewriter for manhwa/webtoon recap videos.
You will receive a narration and rewrite it according to the given style instructions.
Rules:
- Keep it the same length (2-4 sentences)
- Maintain the core story events and meaning
- Never add information not in the original
- Write ONLY the rewritten narration, nothing else
- No meta commentary, no quotation marks wrapping the text"""

        user_prompt = f"""Style Instructions: {style_prompt}

Original narration:
{narration}

Rewrite the narration in the specified style:"""

        payload = {
            "model": self.model,
            "prompt": f"{system_prompt}\n\n{user_prompt}",
            "stream": False,
            "options": {
                "temperature": 0.8,
                "top_p": 0.9,
                "num_predict": 250,
            }
        }

        try:
            resp = requests.post(
                f"{self.host}/api/generate",
                json=payload,
                timeout=config.OLLAMA_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get('response', '').strip()

            # Clean up
            if result.startswith('"') and result.endswith('"'):
                result = result[1:-1]

            return result or narration

        except Exception as e:
            return f"[Rewrite failed: {str(e)}]"

    def rewrite_all(self, panels: list[dict], style: str = "dramatic",
                    progress_callback=None) -> list[dict]:
        """Rewrite all panel narrations in a given style."""
        total = len(panels)
        for i, panel in enumerate(panels):
            if progress_callback:
                progress_callback(i + 1, total, f"Rewriting panel {i+1}/{total}")

            narration = panel.get('narration', '')
            if narration and not narration.startswith('['):
                panel['narration'] = self.rewrite(narration, style)

        return panels

    @staticmethod
    def get_styles() -> list[dict]:
        """Get available rewriting styles."""
        styles = []
        for key, description in config.REWRITER_STYLES.items():
            styles.append({
                "key": key,
                "name": key.capitalize(),
                "description": description,
            })
        return styles
