#!/bin/bash
# ═══════════════════════════════════════════════════════
#  AniFlow — start script
#  Usage:  ./start.sh   (or: source venv/bin/activate && python app.py)
# ═══════════════════════════════════════════════════════
cd "$(dirname "$0")"

# 1. FFmpeg check
if ! command -v ffmpeg &> /dev/null; then
    echo "❌ FFmpeg not found. Install with: brew install ffmpeg"
    exit 1
fi
echo "✅ FFmpeg found"

# 2. Ollama check
if command -v ollama &> /dev/null; then
    echo "✅ Ollama found"
    for m in qwen2.5vl:3b qwen2.5vl:7b; do
        if ollama list 2>/dev/null | grep -q "$m"; then
            echo "✅ Model $m ready"
        else
            echo "⚠️  Model $m missing — pull with: ollama pull $m"
        fi
    done
else
    echo "❌ Ollama not found. Install from https://ollama.com"
fi

# 3. Virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "✅ Virtual environment activated"
else
    echo "📦 Creating virtual environment…"
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
fi

echo ""
echo "🚀 Starting AniFlow at http://localhost:8080"
echo ""
python app.py
