#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  AniFlow — One-Command Setup
#  Run this after cloning the repository:
#    chmod +x setup.sh && ./setup.sh
# ═══════════════════════════════════════════════════════════════════
set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Move to the script's directory
cd "$(dirname "$0")"

echo ""
echo -e "${CYAN}${BOLD}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║           AniFlow Setup Script                ║${NC}"
echo -e "${CYAN}${BOLD}╚═══════════════════════════════════════════════╝${NC}"
echo ""

ERRORS=0

# ─── 1. Check Python ─────────────────────────────────────────────
echo -e "${BOLD}[1/7] Checking Python...${NC}"
if command -v python3 &> /dev/null; then
    PY_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 10 ] && [ "$PY_MINOR" -le 12 ]; then
        echo -e "  ${GREEN}✅ Python $PY_VERSION${NC}"
    else
        echo -e "  ${YELLOW}⚠️  Python $PY_VERSION detected (3.10-3.12 recommended)${NC}"
    fi
else
    echo -e "  ${RED}❌ Python 3 not found. Install from https://www.python.org/downloads/${NC}"
    ERRORS=$((ERRORS + 1))
fi

# ─── 2. Check FFmpeg ─────────────────────────────────────────────
echo -e "${BOLD}[2/7] Checking FFmpeg...${NC}"
if command -v ffmpeg &> /dev/null; then
    FF_VERSION=$(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')
    echo -e "  ${GREEN}✅ FFmpeg $FF_VERSION${NC}"
else
    echo -e "  ${RED}❌ FFmpeg not found.${NC}"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo -e "     Install with: ${CYAN}brew install ffmpeg${NC}"
    elif [[ "$OSTYPE" == "linux"* ]]; then
        echo -e "     Install with: ${CYAN}sudo apt install ffmpeg${NC}"
    fi
    ERRORS=$((ERRORS + 1))
fi

# ─── 3. Check Ollama ─────────────────────────────────────────────
echo -e "${BOLD}[3/7] Checking Ollama...${NC}"
OLLAMA_OK=false
if command -v ollama &> /dev/null; then
    echo -e "  ${GREEN}✅ Ollama found${NC}"
    OLLAMA_OK=true
else
    echo -e "  ${YELLOW}⚠️  Ollama not found — install from https://ollama.com${NC}"
    echo -e "     (Required for local AI narration. Cloud APIs work without it.)"
fi

# Exit early if critical dependencies are missing
if [ "$ERRORS" -gt 0 ]; then
    echo ""
    echo -e "${RED}${BOLD}❌ Missing critical dependencies. Please install them and re-run this script.${NC}"
    exit 1
fi

# ─── 4. Create Virtual Environment ──────────────────────────────
echo -e "${BOLD}[4/7] Setting up Python virtual environment...${NC}"
if [ -d "venv" ]; then
    echo -e "  ${GREEN}✅ Virtual environment already exists${NC}"
    source venv/bin/activate
else
    echo -e "  📦 Creating virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    echo -e "  ${GREEN}✅ Virtual environment created${NC}"
fi

# ─── 5. Install Dependencies ────────────────────────────────────
echo -e "${BOLD}[5/7] Installing Python dependencies...${NC}"
echo -e "  (This may take 10-20 minutes on first run — PyTorch is large)"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet 2>&1 | tail -5
echo -e "  ${GREEN}✅ Dependencies installed${NC}"

# ─── 6. Pull Ollama Models ──────────────────────────────────────
echo -e "${BOLD}[6/7] Pulling Ollama models...${NC}"
if [ "$OLLAMA_OK" = true ]; then
    # Check if Ollama is actually running
    if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        for MODEL in "qwen2.5vl:3b" "qwen2.5:14b"; do
            if ollama list 2>/dev/null | grep -q "$MODEL"; then
                echo -e "  ${GREEN}✅ $MODEL already pulled${NC}"
            else
                echo -e "  📥 Pulling $MODEL (this may take a few minutes)..."
                ollama pull "$MODEL"
                echo -e "  ${GREEN}✅ $MODEL ready${NC}"
            fi
        done
    else
        echo -e "  ${YELLOW}⚠️  Ollama is installed but not running.${NC}"
        echo -e "     Start it with: ${CYAN}ollama serve${NC}"
        echo -e "     Then pull models: ${CYAN}ollama pull qwen2.5vl:3b && ollama pull qwen2.5:14b${NC}"
    fi
else
    echo -e "  ${YELLOW}⏭️  Skipped (Ollama not installed)${NC}"
fi

# ─── 7. Setup .env File ─────────────────────────────────────────
echo -e "${BOLD}[7/7] Setting up configuration...${NC}"
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo -e "  ${GREEN}✅ Created .env from template${NC}"
    echo -e "  ${YELLOW}📝 Edit .env to add API keys (optional — only for cloud services)${NC}"
else
    echo -e "  ${GREEN}✅ .env already exists${NC}"
fi

# ─── Create directories ─────────────────────────────────────────
mkdir -p input/raw_manhwa output models temp assets/{bgm,watermarks,backgrounds,intros,outros} \
    casts projects manhwa_download manhwa_stitch manhwa_detect manhwa_crop \
    manhwa_text manhwa_audio manhwa_clips

# ─── Done! ───────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔═══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║           ✅ Setup Complete!                  ║${NC}"
echo -e "${GREEN}${BOLD}╚═══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Start AniFlow:${NC}"
echo -e "    ${CYAN}source venv/bin/activate${NC}"
echo -e "    ${CYAN}python app.py${NC}"
echo ""
echo -e "  ${BOLD}Or use the quick-start script:${NC}"
echo -e "    ${CYAN}./start.sh${NC}"
echo ""
echo -e "  Then open ${BLUE}${BOLD}http://localhost:8080${NC} in your browser."
echo ""
echo -e "  ${BOLD}Optional:${NC}"
echo -e "    • Edit ${CYAN}.env${NC} to add Gemini/Claude/ElevenLabs API keys"
echo -e "    • Place BGM files in ${CYAN}assets/bgm/${NC}"
echo -e "    • Place watermarks in ${CYAN}assets/watermarks/${NC}"
echo ""
